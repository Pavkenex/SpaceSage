"""Candidate generation and scoring: the ranked opportunities list.

The ``candidates`` stage turns the classifier's verdicts into the ranked list of
*things to do* that powers the app's **Opportunities** view (``docs/design.md``
sections 6, 7 and 9): for every action kind, the biggest estimated wins first,
each row carrying a suggested solution, the reason for it, and an explainable
score.

Kinds
-----

``delete``
    Entries the rules marked ``DELETE_QUARANTINE`` (T1 and T2 only): caches,
    crash data, stale installers, backup copies.  Delete always means
    *quarantine* in SpaceSage.
``move``
    Entries the classifier told to relocate (action ``MOVE`` for media,
    ``NATIVE`` for launcher-managed game libraries): the fix is "move it to your
    data drive", not "throw it away".  Individual files are grouped by their
    folder so a media directory shows up as one row, not as 500.
``stale``
    Big entries (>= ``min_size``) nothing has written to for at least
    ``stale_after_days`` (default one year) whose tier is ``T2`` or unknown --
    the "why do I still have this?" list.  Always advice-only (``REVIEW``).
``dupes-weak``
    Files that share a name *and* a size with another file.  This is a
    deliberately **weak** signal (the bytes may differ) -- every candidate is
    flagged ``weak``, carries confidence 0.3 and asks for confirmation; the
    hash-verified material is the deep-scan slice, not this one.
``app``
    The largest application footprints, one row per app, carrying the advice of
    the app's biggest folder (or "no rule matched" when nothing did).

Scoring
-------

``score = bytes x tier weight x confidence x recency factor``

* **tier weight** -- ``T1`` 1.0, ``T2`` 0.6, ``T3`` 0.2: a disposable cache
  outranks app-owned data of the same size.
* **confidence** -- the classifier's own confidence in the advice.
* **recency factor** -- 0.5 for data touched within ``FRESH_DAYS``, 0.5 -> 1.0
  linearly up to ``COLD_DAYS``, 1.0 beyond, 0.75 when the timestamp is unknown:
  recently used data is a worse cleanup candidate than cold data.

Every :class:`Candidate` carries the four factors (and the age they were
computed from) in :class:`Score`, so the ranking is reproducible and
explainable without re-running the engine.

No double counting
------------------

Folder rows aggregate their descendants: a candidate inside another candidate of
the same kind (a file under a folder that is itself a candidate) is dropped, and
a path claimed by a higher-priority kind (delete > move > stale > dupes-weak >
app) is not listed again by a lower one.  The report counts what was suppressed
so the numbers stay honest.  Claiming happens between the kinds that were
*requested* -- ``spacesage candidates --kind stale`` shows the stale candidates
on their own, without the move/delete lists that would otherwise claim them.

Scale
-----

One streaming pass over ``entries`` (:func:`spacesage.rules.iter_classifications`)
plus one folder pass for the app footprints when that kind is requested.  The
best ``top x OVERKEEP`` candidates of every kind are kept for ranking (``top``
times :data:`OVERKEEP`, which leaves room for the collapsing to fill the list
again), so memory stays bounded on a 20M-row export; ``top=0`` keeps every
candidate.  The report says how many candidates were found below that buffer
instead of pretending they were suppressed.  Nothing is ever written to the
index.
"""

from __future__ import annotations

import heapq
import json
import re
import sqlite3
import time
from collections.abc import Collection, Iterator, Mapping, Sequence
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime

from spacesage import db, rules, stats

KINDS: tuple[str, ...] = ("delete", "move", "stale", "dupes-weak", "app")
"""Candidate kinds, in claim order (higher kinds win a contested path)."""

DEFAULT_MIN_SIZE = 100 * 1024**2
"""Smallest entry size considered by default (``--min-size``, 100 MiB)."""

DEFAULT_TOP = 20
"""Candidates listed per kind by default (``--top``); ``0`` means no limit."""

DEFAULT_STALE_DAYS = 365.0
"""Default age (days) after which a big entry counts as stale."""

DEFAULT_DUPES_MIN_COPIES = 2
"""Smallest same-name/same-size group that counts as a weak duplicate cluster."""

DEFAULT_DUPES_FLOOR = 1024**2
"""Files below this size are not grouped for duplicate hunting (noise floor)."""

DEFAULT_MAX_MEMBERS = 10
"""Most sibling paths a grouped candidate carries in ``members``."""

DEFAULT_MOVE_ACTIONS: tuple[str, ...] = ("MOVE", "NATIVE")
"""Classifier actions that make an entry a relocation candidate."""

DELETE_ACTION = "DELETE_QUARANTINE"
"""The one action that feeds the ``delete`` kind."""

DELETE_TIERS: tuple[str, ...] = ("T1", "T2")
"""Tiers allowed into the ``delete`` kind (T3 is report-only)."""

MOVE_TIERS: tuple[str, ...] = ("T1", "T2")
"""Tiers allowed into the ``move`` kind (T3 is report-only)."""

TIER_WEIGHTS: Mapping[str, float] = {"T1": 1.0, "T2": 0.6, "T3": 0.2}
"""Risk-tier weights of the score: disposable data outranks app-owned data."""

FRESH_DAYS = 30.0
"""Age (days) below which the recency factor is at its minimum."""

COLD_DAYS = 365.0
"""Age (days) at which the recency factor reaches 1.0."""

FRESH_RECENCY = 0.5
"""Recency factor for freshly touched data."""

UNKNOWN_AGE_RECENCY = 0.75
"""Recency factor when the entry has no timestamp at all."""

STALE_UNKNOWN_CONFIDENCE = 0.4
"""Confidence of a stale candidate that no rule matched."""

DUPES_CONFIDENCE = 0.3
"""Confidence of a weak (name + size) duplicate cluster."""

DUPES_CATEGORY = "weak-duplicate"
"""Category carried by weak duplicate candidates."""

APP_UNKNOWN_CONFIDENCE = 0.3
"""Confidence of an app candidate whose folders no rule matched."""

APP_REVIEW_ACTION = "REVIEW"
"""Action of an app candidate whose folders no rule matched."""

GROUP_FOLDER = "folder"
"""``group`` marker of a directory-level move group."""

GROUP_DUPLICATES = "duplicates"
"""``group`` marker of a weak duplicate cluster."""

OVERKEEP = 10
"""Candidates kept per kind per requested row, so collapsing cannot starve a list."""

ACTION_LABELS: Mapping[str, str] = {
    "DELETE_QUARANTINE": "Delete (quarantine)",
    "MOVE": "Move to another drive",
    "COMPRESS_NTFS": "Compress (NTFS)",
    "NATIVE": "Use the native tool",
    "REVIEW": "Review",
    "KEEP": "No action",
}
"""Plain-language name of every action in the vocabulary."""

_DAY = 86_400
_WINDOWS_PREFIX_RE = re.compile(r"^(?:[A-Za-z]:[\\/]|[A-Za-z]:$|\\\\)")
_ROOT_IDS_SQL = "SELECT id FROM entries WHERE depth = 0"
_DUPES_SQL = """
SELECT lower(name) AS dup_name, size, COUNT(*) AS copies, SUM(size) AS total
FROM entries
WHERE is_dir = 0 AND hardlink_flag = 0 AND size >= :floor
GROUP BY dup_name, size
HAVING COUNT(*) >= :copies AND SUM(size) - size >= :min_size
ORDER BY SUM(size) - size DESC, size DESC, dup_name ASC
"""
_DUPES_PATHS_SQL = """
SELECT path, name, mtime
FROM entries
WHERE is_dir = 0 AND hardlink_flag = 0 AND size = ? AND lower(name) = ?
ORDER BY path ASC
LIMIT ?
"""


class CandidatesError(RuntimeError):
    """Raised when candidates cannot be generated for the given index."""


# --------------------------------------------------------------------------- #
# Scoring
# --------------------------------------------------------------------------- #


def recency_factor(age_days: float | None) -> float:
    """Deterministic coldness factor: fresh data scores lower than cold data.

    ``0.5`` at 0-``FRESH_DAYS`` days, rising linearly to ``1.0`` at
    ``COLD_DAYS`` days and staying there; ``UNKNOWN_AGE_RECENCY`` when the age
    is unknown (a missing timestamp is neither fresh nor provably cold).
    """
    if age_days is None:
        return UNKNOWN_AGE_RECENCY
    if age_days <= FRESH_DAYS:
        return FRESH_RECENCY
    if age_days >= COLD_DAYS:
        return 1.0
    span = COLD_DAYS - FRESH_DAYS
    return FRESH_RECENCY + (1.0 - FRESH_RECENCY) * (age_days - FRESH_DAYS) / span


@dataclass(frozen=True)
class Score:
    """The explainable score of one candidate (``bytes`` scaled by three factors)."""

    value: float
    """``bytes * tier_weight * confidence * recency_factor``."""

    bytes: int
    tier_weight: float
    confidence: float
    recency_factor: float
    age_days: int | None

    def explain(self) -> str:
        """Compact rendering of the multiplication behind ``value``."""
        age = f"age {self.age_days}d" if self.age_days is not None else "age unknown"
        return (
            f"{stats.format_bytes(self.bytes)} x {self.tier_weight:.2f} tier "
            f"x {self.confidence:.2f} conf x {self.recency_factor:.2f} recency ({age})"
        )

    def to_dict(self) -> dict[str, object]:
        """JSON-ready mapping."""
        return {
            "value": self.value,
            "bytes": self.bytes,
            "tier_weight": self.tier_weight,
            "confidence": self.confidence,
            "recency_factor": self.recency_factor,
            "age_days": self.age_days,
        }


def score_candidate(size: int, tier: str, confidence: float, age_days: float | None) -> Score:
    """Compute the deterministic score of one candidate.

    ``size`` is the bytes the suggestion acts on, ``tier``/``confidence`` come
    from the classification (or from the kind's own constants) and ``age_days``
    is the entry's age in days (``None`` when its timestamp is unknown).
    """
    if size < 0:
        raise CandidatesError(f"size must not be negative, got {size}")
    weight = TIER_WEIGHTS.get(tier)
    if weight is None:
        raise CandidatesError(
            f"unknown tier {tier!r}; use one of {', '.join(sorted(TIER_WEIGHTS))}"
        )
    if not 0.0 <= confidence <= 1.0:
        raise CandidatesError(f"confidence must be between 0 and 1, got {confidence}")
    days = None if age_days is None else int(age_days)
    factor = recency_factor(days)
    return Score(
        value=size * weight * confidence * factor,
        bytes=size,
        tier_weight=weight,
        confidence=confidence,
        recency_factor=factor,
        age_days=days,
    )


# --------------------------------------------------------------------------- #
# Report data
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class Candidate:
    """One ranked opportunity: a path, a suggested solution and its reasons."""

    kind: str
    """``delete`` | ``move`` | ``stale`` | ``dupes-weak`` | ``app``."""

    action: str
    """Action from the rule vocabulary (``KEEP`` renders as "No action")."""

    path: str
    """The entry (or, for grouped candidates, the folder they live in)."""

    is_dir: bool
    bytes: int
    """Bytes the suggestion acts on (dupes: the recoverable copies)."""

    tier: str
    category: str
    confidence: float
    rationale: str
    why: str
    """One-line suggested solution plus its reason (the GUI column)."""

    score: Score
    rule_id: str | None = None
    pack: str | None = None
    native: str | None = None
    mtime: int | None = None
    age_days: int | None = None
    weak: bool = False
    """True for candidates that need confirmation before anything happens."""

    group: str | None = None
    """``folder`` (move group), ``duplicates``, or the application name."""

    members: tuple[str, ...] = ()
    """Sibling paths the candidate covers (largest first, capped)."""

    member_count: int = 0
    """Total number of covered members (may exceed ``len(members)``)."""

    member_bytes: int = 0
    """Bytes across every member (dupes: all copies, not just the recoverable)."""

    def to_dict(self) -> dict[str, object]:
        """JSON-ready mapping (``spacesage.candidates/v1`` items)."""
        return {
            "kind": self.kind,
            "action": self.action,
            "path": self.path,
            "is_dir": self.is_dir,
            "bytes": self.bytes,
            "tier": self.tier,
            "category": self.category,
            "confidence": self.confidence,
            "rationale": self.rationale,
            "why": self.why,
            "rule_id": self.rule_id,
            "pack": self.pack,
            "native": self.native,
            "mtime": self.mtime,
            "age_days": self.age_days,
            "weak": self.weak,
            "group": self.group,
            "members": list(self.members),
            "member_count": self.member_count,
            "member_bytes": self.member_bytes,
            "score": self.score.to_dict(),
        }


@dataclass(frozen=True)
class KindSummary:
    """One kind's ranked candidates plus its totals."""

    kind: str
    candidates: tuple[Candidate, ...]
    found: int
    """Candidates the filters let through (before every buffer and collapse)."""

    total: int
    """Candidates kept for ranking (``found`` capped by the selection buffer)."""

    suppressed: int
    """Of those, the ones dropped as already covered by another candidate."""

    bytes: int
    """Bytes across the listed candidates."""

    total_bytes: int
    """Bytes across every candidate found."""

    @property
    def beyond_buffer(self) -> int:
        """Candidates the selection buffer dropped before collapsing (``found - total``)."""
        return self.found - self.total

    def to_dict(self) -> dict[str, object]:
        """JSON-ready mapping."""
        return {
            "kind": self.kind,
            "listed": len(self.candidates),
            "total": self.total,
            "found": self.found,
            "suppressed": self.suppressed,
            "bytes": self.bytes,
            "total_bytes": self.total_bytes,
            "best_score": self.candidates[0].score.value if self.candidates else 0.0,
            "items": [candidate.to_dict() for candidate in self.candidates],
        }


@dataclass(frozen=True)
class CandidateReport:
    """Every view the ``candidates`` CLI prints, in one object."""

    generated: datetime
    db_path: str | None
    schema_version: int
    rules: rules.RuleSummary
    as_of: int
    min_size: int
    top: int
    stale_after_days: float
    dupes_min_copies: int
    dupes_floor: int
    move_actions: tuple[str, ...]
    considered: int
    """Candidates found across every requested kind."""

    suppressed_nested: int
    """Candidates dropped because another candidate of the same kind covers them."""

    suppressed_duplicate: int
    """Candidates dropped because a higher-priority kind already claimed the path."""

    kinds: tuple[KindSummary, ...]

    @property
    def candidates(self) -> tuple[Candidate, ...]:
        """Every listed candidate, kinds in :data:`KINDS` order."""
        return tuple(item for block in self.kinds for item in block.candidates)

    def kind(self, name: str) -> KindSummary | None:
        """The block for one kind, or ``None`` when it was not requested."""
        for block in self.kinds:
            if block.kind == name:
                return block
        return None

    def to_dict(self) -> dict[str, object]:
        """JSON-ready mapping (``spacesage.candidates/v1``)."""
        listed = sum(len(block.candidates) for block in self.kinds)
        return {
            "schema": "spacesage.candidates/v1",
            "generated": _iso(self.generated),
            "index": {"db": self.db_path, "schema_version": self.schema_version},
            "rules": {
                "count": self.rules.rules,
                "builtin_packs": list(self.rules.builtin_packs),
                "user_packs": list(self.rules.user_packs),
                "shadowed": list(self.rules.shadowed),
                "fingerprint": self.rules.fingerprint,
            },
            "as_of": _iso(datetime.fromtimestamp(self.as_of, tz=UTC)),
            "thresholds": {
                "min_size": self.min_size,
                "stale_after_days": self.stale_after_days,
                "dupes_min_copies": self.dupes_min_copies,
                "dupes_floor": self.dupes_floor,
                "move_actions": list(self.move_actions),
            },
            "top": self.top,
            "summary": {
                "considered": self.considered,
                "listed": listed,
                "suppressed_nested": self.suppressed_nested,
                "suppressed_duplicate": self.suppressed_duplicate,
            },
            "kinds": [block.to_dict() for block in self.kinds],
        }


def _iso(value: datetime) -> str:
    return value.astimezone(UTC).isoformat(timespec="seconds")


# --------------------------------------------------------------------------- #
# Path helpers
# --------------------------------------------------------------------------- #


def path_key(path: str) -> str:
    """Comparison key of a path: single separator, case-folded for Windows paths.

    Shared with :mod:`spacesage.planner`: the plan's "is this path inside that
    one" questions must fold case exactly the way the ranking does.
    """
    normalised = path.replace("/", "\\").rstrip("\\")
    return normalised.lower() if _WINDOWS_PREFIX_RE.match(path) else normalised


def _ancestors(key: str) -> Iterator[str]:
    """Yield every strict ancestor key of ``key``, nearest first."""
    index = key.rfind("\\")
    while index > 0:
        key = key[:index]
        yield key
        index = key.rfind("\\")


def under(path_key_: str, dirs: Collection[str]) -> bool:
    """True when one of ``dirs`` (a set of *keys*) is a strict ancestor of ``path_key_``."""
    return any(ancestor in dirs for ancestor in _ancestors(path_key_))


def parent_path(path: str) -> str | None:
    """Display path of the folder holding ``path`` (``None`` for a root)."""
    index = max(path.rfind("\\"), path.rfind("/"))
    if index <= 0:
        return None
    parent = path[:index]
    if parent.endswith(":"):
        parent += "\\"
    return parent


# Module-internal aliases: the call sites below predate the public names.
_path_key = path_key
_under_any = under
_parent_path = parent_path


def _plural(count: int, singular: str, plural: str | None = None) -> str:
    return f"{count} {singular if count == 1 else (plural or singular + 's')}"


def action_label(action: str) -> str:
    """Plain-language name of an action (``DELETE_QUARANTINE`` -> "Delete (quarantine)")."""
    return ACTION_LABELS.get(action, action)


def _age_days(mtime: int | None, moment: int) -> int | None:
    if mtime is None:
        return None
    return int((moment - mtime) // _DAY)


def _solution(action: str, rationale: str) -> str:
    return f"{action_label(action)}: {rationale}"


# --------------------------------------------------------------------------- #
# Bounded selection
# --------------------------------------------------------------------------- #


def _rank_key(candidate: Candidate) -> tuple[float, int, str]:
    """Ranking key: score desc, then bytes desc, then path asc (deterministic)."""
    return (-candidate.score.value, -candidate.bytes, candidate.path)


class _TopCandidates:
    """Bounded selection of the best candidates of one kind (streaming)."""

    def __init__(self, limit: int) -> None:
        self._limit = limit
        self._heap: list[tuple[float, int, int, Candidate]] = []
        self._sequence = 0
        self.seen = 0
        self.bytes = 0

    def offer(self, candidate: Candidate) -> None:
        """Consider one candidate; memory stays at ``limit`` when bounded."""
        self.seen += 1
        self.bytes += candidate.bytes
        if self._limit <= 0:
            self._heap.append((candidate.score.value, candidate.bytes, self._sequence, candidate))
            self._sequence += 1
            return
        item = (candidate.score.value, candidate.bytes, self._sequence, candidate)
        self._sequence += 1
        if len(self._heap) < self._limit:
            heapq.heappush(self._heap, item)
        elif item > self._heap[0]:
            # Ties keep the earlier candidate (the stream order is deterministic).
            heapq.heapreplace(self._heap, item)

    @property
    def items(self) -> tuple[Candidate, ...]:
        """The kept candidates, best first."""
        return tuple(sorted((item[3] for item in self._heap), key=_rank_key))


# --------------------------------------------------------------------------- #
# Candidate builders
# --------------------------------------------------------------------------- #


def _clean_candidate(kind: str, classification: rules.Classification, moment: int) -> Candidate:
    """A candidate that mirrors the classifier's verdict as-is."""
    age = _age_days(classification.mtime, moment)
    return Candidate(
        kind=kind,
        action=classification.action,
        path=classification.path,
        is_dir=classification.is_dir,
        bytes=classification.size,
        tier=classification.tier,
        category=classification.category,
        confidence=classification.confidence,
        rationale=classification.rationale,
        why=_solution(classification.action, classification.rationale),
        score=score_candidate(
            classification.size, classification.tier, classification.confidence, age
        ),
        rule_id=classification.rule_id,
        pack=classification.pack,
        native=classification.native,
        mtime=classification.mtime,
        age_days=age,
    )


def _stale_candidate(classification: rules.Classification, moment: int) -> Candidate:
    """A big, cold entry nothing has touched for a long time (advice-only)."""
    age = _age_days(classification.mtime, moment)
    matched = classification.rule_id is not None
    confidence = classification.confidence if matched else STALE_UNKNOWN_CONFIDENCE
    if matched:
        rationale = f"Nothing has written to it for {age} days. {classification.rationale}"
    else:
        rationale = (
            f"Nothing has written to it for {age} days and no rule claims it -- decide whether "
            "to archive, move or delete it."
        )
    return Candidate(
        kind="stale",
        action="REVIEW",
        path=classification.path,
        is_dir=classification.is_dir,
        bytes=classification.size,
        tier=classification.tier,
        category=classification.category,
        confidence=confidence,
        rationale=rationale,
        why=_solution("REVIEW", rationale),
        score=score_candidate(classification.size, classification.tier, confidence, age),
        rule_id=classification.rule_id,
        pack=classification.pack,
        native=classification.native,
        mtime=classification.mtime,
        age_days=age,
    )


@dataclass(slots=True)
class _GroupAdvice:
    """Running totals for one ``(category, tier, action)`` inside a move group."""

    key: tuple[str, str, str]
    bytes: int = 0
    confidence: float = 1.0
    rule_id: str | None = None
    pack: str | None = None
    rationale: str = ""
    native: str | None = None

    def add(self, classification: rules.Classification) -> None:
        """Fold one matching file into this advice bucket."""
        self.bytes += classification.size
        self.confidence = min(self.confidence, classification.confidence)
        if classification.rule_id is not None and (
            self.rule_id is None or classification.rule_id < self.rule_id
        ):
            # The alphabetically first rule id speaks for the bucket: stable
            # regardless of the order the index happens to return rows in.
            self.rule_id = classification.rule_id
            self.pack = classification.pack
            self.rationale = classification.rationale
            self.native = classification.native


@dataclass(slots=True)
class _MoveGroup:
    """Files of one folder that each deserve relocation, kept as one row."""

    path: str
    key: str
    bytes: int = 0
    member_count: int = 0
    newest_mtime: int | None = None
    members: list[tuple[int, str]] = field(default_factory=list)
    advice: dict[tuple[str, str, str], _GroupAdvice] = field(default_factory=dict)

    def add(self, classification: rules.Classification, *, cap: int) -> None:
        """Fold one matching file into the group."""
        self.bytes += classification.size
        self.member_count += 1
        if classification.mtime is not None and (
            self.newest_mtime is None or classification.mtime > self.newest_mtime
        ):
            self.newest_mtime = classification.mtime
        bucket = classification.category, classification.tier, classification.action
        accumulator = self.advice.get(bucket)
        if accumulator is None:
            accumulator = _GroupAdvice(key=bucket)
            self.advice[bucket] = accumulator
        accumulator.add(classification)
        item = (classification.size, classification.path)
        if len(self.members) < cap:
            heapq.heappush(self.members, item)
        elif item > self.members[0]:
            heapq.heapreplace(self.members, item)

    def candidate(self, *, moment: int, max_members: int) -> Candidate:
        """Turn the accumulated group into a directory-level candidate."""
        advice = sorted(self.advice.values(), key=lambda item: (-item.bytes, item.key))[0]
        category, _tier, action = advice.key
        tier = max(
            (item.key[1] for item in self.advice.values()),
            key=lambda value: rules.TIERS.index(value),
        )
        confidence = min(item.confidence for item in self.advice.values())
        age = _age_days(self.newest_mtime, moment)
        rationale = (
            f"{_plural(self.member_count, 'file')} here ({stats.format_bytes(self.bytes)}) "
            f"match the {category} advice: {advice.rationale}"
        )
        members = tuple(path for _size, path in sorted(self.members, reverse=True))
        return Candidate(
            kind="move",
            action=action,
            path=self.path,
            is_dir=True,
            bytes=self.bytes,
            tier=tier,
            category=category,
            confidence=confidence,
            rationale=rationale,
            why=_solution(action, rationale),
            score=score_candidate(self.bytes, tier, confidence, age),
            rule_id=advice.rule_id,
            pack=advice.pack,
            native=advice.native,
            mtime=self.newest_mtime,
            age_days=age,
            group=GROUP_FOLDER,
            members=members[:max_members],
            member_count=self.member_count,
            member_bytes=self.bytes,
        )


def _app_keys(roots: Mapping[int, str]) -> dict[str, str]:
    """Map every app-root path key to its root path (for ancestor walks)."""
    return {_path_key(path): path for path in roots.values()}


def _app_key(path_key: str, root_keys: Mapping[str, str]) -> str | None:
    """Lower-cased app name a path belongs to (``None`` when it is not in an app).

    Applications are the direct children of the app roots
    (:data:`spacesage.stats.APP_ROOT_COMPONENTS`), so the component right after
    the nearest root ancestor names the app -- the same spelling-insensitive
    rule the stats stage merges footprints by.
    """
    for ancestor in _ancestors(path_key):
        if ancestor in root_keys:
            rest = path_key[len(ancestor) + 1 :]
            component = rest.split("\\", 1)[0]
            return component or None
    return None


def _app_advice(
    footprint: stats.AppFootprint, advice: Mapping[str, rules.Classification], moment: int
) -> Candidate:
    """One application footprint, carrying the advice of its biggest match.

    The app's folders are summarised by the largest entry inside them that a
    rule matched -- a Chrome cache speaks for ``Google``, the ``Program Files``
    catch-all (``KEEP``) speaks for an installed program -- and fall back to an
    explicit "no rule matched" review when nothing inside matched at all.
    """
    largest = footprint.roots[0]
    matched = advice.get(footprint.app.lower())
    if matched is None:
        action = APP_REVIEW_ACTION
        tier = rules.UNKNOWN_TIER
        confidence = APP_UNKNOWN_CONFIDENCE
        category = rules.UNKNOWN_CATEGORY
        reason = (
            f"{footprint.app} uses {stats.format_bytes(footprint.bytes)} across "
            f"{_plural(len(footprint.roots), 'folder')}, largest {largest}; no rule matched any "
            "of them, so SpaceSage has no advice for it."
        )
        native = None
        rule_id = None
        pack = None
        mtime = None
    else:
        action = matched.action
        tier = matched.tier
        confidence = matched.confidence
        category = matched.category
        reason = (
            f"{footprint.app} uses {stats.format_bytes(footprint.bytes)} across "
            f"{_plural(len(footprint.roots), 'folder')}, largest {largest}; the biggest match "
            f"inside is {matched.path} ({category}): {matched.rationale}"
        )
        native = matched.native
        rule_id = matched.rule_id
        pack = matched.pack
        mtime = matched.mtime
    age = _age_days(mtime, moment)
    return Candidate(
        kind="app",
        action=action,
        path=largest,
        is_dir=True,
        bytes=footprint.bytes,
        tier=tier,
        category=category,
        confidence=confidence,
        rationale=reason,
        why=_solution(action, reason),
        score=score_candidate(footprint.bytes, tier, confidence, age),
        rule_id=rule_id,
        pack=pack,
        native=native,
        mtime=mtime,
        age_days=age,
        group=footprint.app,
        members=footprint.roots,
        member_count=len(footprint.roots),
        member_bytes=footprint.bytes,
    )


def _dupes_candidates(
    conn: sqlite3.Connection,
    *,
    min_size: int,
    min_copies: int,
    floor: int,
    moment: int,
    max_members: int,
    keep: int,
) -> _TopCandidates:
    """Weak (same name + same size) duplicate clusters, recoverable bytes first."""
    bucket = _TopCandidates(keep)
    parameters = {"floor": floor, "copies": min_copies, "min_size": min_size}
    for name, size, copies, total in conn.execute(_DUPES_SQL, parameters):
        rows = conn.execute(
            _DUPES_PATHS_SQL, (int(size), str(name), max(max_members, 1))
        ).fetchall()
        paths = tuple(str(row[0]) for row in rows)
        if not paths:
            continue
        sample = str(rows[0][1])
        mtimes = [int(row[2]) for row in rows if row[2] is not None]
        mtime = max(mtimes) if mtimes else None
        recoverable = int(total) - int(size)
        age = _age_days(mtime, moment)
        copy_count = int(copies)
        rationale = (
            f"same name and size as {_plural(copy_count - 1, 'other file')} ({sample}, "
            f"{stats.format_bytes(int(size))} each, {stats.format_bytes(int(total))} combined); "
            "the bytes are not verified -- confirm the copies are identical before deleting or "
            "linking any of them."
        )
        bucket.offer(
            Candidate(
                kind="dupes-weak",
                action="REVIEW",
                path=paths[0],
                is_dir=False,
                bytes=recoverable,
                tier="T2",
                category=DUPES_CATEGORY,
                confidence=DUPES_CONFIDENCE,
                rationale=rationale,
                why=_solution("REVIEW", rationale),
                score=score_candidate(recoverable, "T2", DUPES_CONFIDENCE, age),
                mtime=mtime,
                age_days=age,
                weak=True,
                group=GROUP_DUPLICATES,
                members=paths,
                member_count=copy_count,
                member_bytes=int(total),
            )
        )
    return bucket


# --------------------------------------------------------------------------- #
# Generation
# --------------------------------------------------------------------------- #


def _validate_kinds(kinds: Sequence[str]) -> tuple[str, ...]:
    selected: list[str] = []
    for kind in kinds:
        if kind not in KINDS:
            raise CandidatesError(f"unknown kind {kind!r}; pick from {', '.join(KINDS)}")
        if kind not in selected:
            selected.append(kind)
    if not selected:
        raise CandidatesError(f"at least one kind is required ({', '.join(KINDS)})")
    return tuple(selected)


def _collapse_nested(items: Sequence[Candidate]) -> tuple[list[Candidate], int]:
    """Drop candidates already inside another candidate of the same kind."""
    dirs = {_path_key(item.path) for item in items if item.is_dir}
    kept: list[Candidate] = []
    suppressed = 0
    for item in items:
        if _under_any(_path_key(item.path), dirs):
            suppressed += 1
            continue
        kept.append(item)
    return kept, suppressed


def _free_member(candidate: Candidate, claimed: set[str], claimed_dirs: set[str]) -> str | None:
    """First cluster member no higher-priority kind has claimed (dupes only)."""
    for member in candidate.members:
        key = _path_key(member)
        if key in claimed or _under_any(key, claimed_dirs):
            continue
        return member
    return None


def candidate_report(
    conn: sqlite3.Connection,
    ruleset: rules.RuleSet,
    *,
    kinds: Sequence[str] = KINDS,
    min_size: int = DEFAULT_MIN_SIZE,
    top: int = DEFAULT_TOP,
    now: float | None = None,
    stale_after_days: float = DEFAULT_STALE_DAYS,
    dupes_min_copies: int = DEFAULT_DUPES_MIN_COPIES,
    dupes_floor: int = DEFAULT_DUPES_FLOOR,
    move_actions: Sequence[str] = DEFAULT_MOVE_ACTIONS,
    max_members: int = DEFAULT_MAX_MEMBERS,
    db_path: str | None = None,
) -> CandidateReport:
    """Rank the opportunities of an index per action kind.

    ``min_size`` is the threshold every *candidate* has to clear: for ``delete``,
    ``stale`` and ``dupes-weak`` that is the entry itself (a cluster compares its
    recoverable bytes), for ``move`` it is the folder or the group total -- so a
    folder of twenty 80 MiB videos is one 1.6 GiB candidate, not twenty rows.
    ``top`` bounds the listed rows per kind (``0`` = no limit) and ``now``
    (epoch seconds) is the reference point for age and recency.  Nothing is
    written to the index.
    """
    selected = _validate_kinds(kinds)
    if min_size < 0:
        raise CandidatesError(f"--min-size must not be negative, got {min_size}")
    if top < 0:
        raise CandidatesError(f"--top must be >= 0 (0 = no limit), got {top}")
    if stale_after_days < 0:
        raise CandidatesError(f"--stale-after-days must not be negative, got {stale_after_days}")
    if dupes_min_copies < 2:
        raise CandidatesError(f"--dupes-min-copies must be >= 2, got {dupes_min_copies}")
    if dupes_floor < 0:
        raise CandidatesError(f"--dupes-floor must not be negative, got {dupes_floor}")
    if max_members < 1:
        raise CandidatesError(f"--max-members must be >= 1, got {max_members}")
    if db.entry_count(conn) == 0:
        raise CandidatesError("the index is empty; ingest a WizTree export first")

    moment = int(now if now is not None else time.time())
    keep = top * OVERKEEP if top > 0 else 0
    buckets = {kind: _TopCandidates(keep) for kind in selected}
    move_actions_set = frozenset(move_actions)
    root_keys = _app_keys(stats.app_roots(conn)) if "app" in buckets else {}
    app_advice: dict[str, rules.Classification] = {}
    root_entry_ids = {int(row[0]) for row in conn.execute(_ROOT_IDS_SQL)}
    move_dirs: dict[str, Candidate] = {}
    groups: dict[str, _MoveGroup] = {}

    for classification in rules.iter_classifications(conn, ruleset, now=moment):
        if classification.entry_id in root_entry_ids:
            # The exported roots (C:\ and friends) are the whole drive, never a
            # candidate -- every path in the index lives under one of them.
            continue
        if root_keys and classification.rule_id is not None:
            # The biggest matching entry inside an app speaks for that app
            # (ties prefer the shorter, shallower path).
            app = _app_key(_path_key(classification.path), root_keys)
            if app is not None:
                current = app_advice.get(app)
                if current is None or (classification.size, -len(classification.path)) > (
                    current.size,
                    -len(current.path),
                ):
                    app_advice[app] = classification
        if classification.hardlink and not classification.is_dir:
            # A hard-linked copy frees nothing on its own; the payload is
            # already credited to the unflagged source entry (design.md 5.1).
            continue
        size = classification.size
        big = size >= min_size
        if (
            "delete" in buckets
            and big
            and classification.action == DELETE_ACTION
            and classification.tier in DELETE_TIERS
        ):
            buckets["delete"].offer(_clean_candidate("delete", classification, moment))
        if (
            "move" in buckets
            and classification.action in move_actions_set
            and (classification.tier in MOVE_TIERS)
        ):
            # Files are grouped per folder first: the *group* is the candidate,
            # so it is the group's total that has to clear --min-size, not every
            # single file in it.
            if classification.is_dir:
                if big:
                    move_dirs[_path_key(classification.path)] = _clean_candidate(
                        "move", classification, moment
                    )
            else:
                parent = _parent_path(classification.path)
                if parent is not None:
                    parent_key = _path_key(parent)
                    group = groups.get(parent_key)
                    if group is None:
                        group = _MoveGroup(path=parent, key=parent_key)
                        groups[parent_key] = group
                    group.add(classification, cap=max_members)
        if (
            "stale" in buckets
            and big
            and (classification.tier == "T2" or classification.rule_id is None)
            and classification.mtime is not None
            and moment - classification.mtime >= stale_after_days * _DAY
        ):
            buckets["stale"].offer(_stale_candidate(classification, moment))

    if "move" in buckets:
        for group in groups.values():
            if group.bytes < min_size:
                continue  # a folder of a few small files is not worth a move
            if group.key in move_dirs or _under_any(group.key, set(move_dirs)):
                continue  # a matching folder candidate already covers these files
            buckets["move"].offer(group.candidate(moment=moment, max_members=max_members))
        for candidate in move_dirs.values():
            buckets["move"].offer(candidate)

    if "app" in buckets:
        for footprint in stats.app_footprints(conn):
            if footprint.bytes < min_size or not footprint.roots:
                continue
            buckets["app"].offer(_app_advice(footprint, app_advice, moment))

    if "dupes-weak" in buckets:
        dupes = _dupes_candidates(
            conn,
            min_size=min_size,
            min_copies=dupes_min_copies,
            floor=dupes_floor,
            moment=moment,
            max_members=max_members,
            keep=keep,
        )
        for candidate in dupes.items:
            buckets["dupes-weak"].offer(candidate)

    # --- no double counting ------------------------------------------------- #
    collapsed: dict[str, tuple[Candidate, ...]] = {}
    ranked_counts: dict[str, int] = {}
    suppressed_nested = 0
    suppressed_by_kind: dict[str, int] = {}
    for kind in selected:
        items = buckets[kind].items
        ranked_counts[kind] = len(items)
        kept, dropped = _collapse_nested(items)
        collapsed[kind] = tuple(kept)
        suppressed_nested += dropped
        suppressed_by_kind[kind] = dropped

    claimed: set[str] = set()
    claimed_dirs: set[str] = set()
    suppressed_duplicate = 0
    final: dict[str, list[Candidate]] = {}
    for kind in KINDS:  # claim order: delete, then move, ...
        if kind not in collapsed:
            continue
        kept = []
        for item in collapsed[kind]:
            candidate = item
            if kind == "dupes-weak":
                member = _free_member(item, claimed, claimed_dirs)
                if member is None:
                    suppressed_duplicate += 1
                    suppressed_by_kind[kind] += 1
                    continue
                if member != item.path:
                    candidate = replace(item, path=member)
            key = _path_key(candidate.path)
            if key in claimed or _under_any(key, claimed_dirs):
                suppressed_duplicate += 1
                suppressed_by_kind[kind] += 1
                continue
            claimed.add(key)
            if candidate.is_dir:
                claimed_dirs.add(key)
            kept.append(candidate)
        final[kind] = kept

    blocks: list[KindSummary] = []
    for kind in selected:
        kept = final.get(kind, [])
        ranked = sorted(kept, key=_rank_key)
        listed = tuple(ranked[:top]) if top > 0 else tuple(ranked)
        blocks.append(
            KindSummary(
                kind=kind,
                candidates=listed,
                found=buckets[kind].seen,
                total=ranked_counts[kind],
                suppressed=suppressed_by_kind[kind],
                bytes=sum(item.bytes for item in listed),
                total_bytes=buckets[kind].bytes,
            )
        )

    return CandidateReport(
        generated=datetime.now(tz=UTC),
        db_path=db_path,
        schema_version=db.schema_version(conn),
        rules=ruleset.summary(),
        as_of=moment,
        min_size=min_size,
        top=top,
        stale_after_days=stale_after_days,
        dupes_min_copies=dupes_min_copies,
        dupes_floor=dupes_floor,
        move_actions=tuple(move_actions),
        considered=sum(bucket.seen for bucket in buckets.values()),
        suppressed_nested=suppressed_nested,
        suppressed_duplicate=suppressed_duplicate,
        kinds=tuple(blocks),
    )


# --------------------------------------------------------------------------- #
# Rendering
# --------------------------------------------------------------------------- #


def _section(title: str) -> str:
    return f"\n{title}:"


def _member_marker(candidate: Candidate) -> str:
    """Short parenthetical describing what a grouped candidate covers."""
    if candidate.group == GROUP_DUPLICATES:
        return (
            f"  ({candidate.member_count} copies, "
            f"{stats.format_bytes(candidate.member_bytes)} combined)"
        )
    if candidate.group == GROUP_FOLDER:
        return f"  ({_plural(candidate.member_count, 'file')} in this folder)"
    if candidate.kind == "app":
        return f"  [{candidate.group}, {_plural(candidate.member_count, 'root')}]"
    return ""


def render_text(report: CandidateReport) -> str:
    """Render the report as compact plain text, one block per kind."""
    listed = sum(len(block.candidates) for block in report.kinds)
    lines = [
        f"index: {report.db_path or '(index)'} (schema v{report.schema_version})",
        f"rules: {_plural(report.rules.rules, 'rule')}, "
        f"fingerprint {report.rules.fingerprint[:12]}",
        f"as of: {_iso(datetime.fromtimestamp(report.as_of, tz=UTC))}, "
        f"min size {stats.format_bytes(report.min_size)}, top {report.top} per kind",
        f"candidates: {report.considered} found, {listed} listed "
        f"(suppressed {report.suppressed_nested} nested, "
        f"{report.suppressed_duplicate} claimed by another kind)",
    ]
    for block in report.kinds:
        header = (
            f"{block.kind} ({len(block.candidates)} listed of {block.total} ranked, "
            f"{stats.format_bytes(block.bytes)})"
        )
        if block.beyond_buffer:
            header += f" -- {block.found} found, {block.beyond_buffer} below the top buffer"
        if block.suppressed:
            header += f" -- {block.suppressed} covered by bigger candidates"
        lines.append(_section(header))
        for candidate in block.candidates:
            lines.append(
                f"  {stats.format_bytes(candidate.bytes):>10}  {candidate.tier:<3} "
                f"conf {candidate.confidence:.2f}  score "
                f"{stats.format_bytes(int(candidate.score.value)):>10}  "
                f"{candidate.path}{_member_marker(candidate)}"
            )
            lines.append(f"      {candidate.why}")
            if candidate.native:
                lines.append(f"      native: {candidate.native}")
            lines.append(f"      score: {candidate.score.explain()}")
            if candidate.group == GROUP_DUPLICATES and candidate.members:
                extra = candidate.member_count - len(candidate.members)
                suffix = f", +{extra} more" if extra > 0 else ""
                lines.append(f"      copies: {', '.join(candidate.members[:3])}{suffix}")
            if candidate.kind == "app" and len(candidate.members) > 1:
                lines.append(f"      roots: {', '.join(candidate.members[:3])}")
    return "\n".join(lines) + "\n"


def render_json(report: CandidateReport) -> str:
    """Render the complete report as pretty-printed JSON."""
    return json.dumps(report.to_dict(), indent=2) + "\n"


__all__ = [
    "ACTION_LABELS",
    "APP_UNKNOWN_CONFIDENCE",
    "COLD_DAYS",
    "DEFAULT_DUPES_FLOOR",
    "DEFAULT_DUPES_MIN_COPIES",
    "DEFAULT_MAX_MEMBERS",
    "DEFAULT_MIN_SIZE",
    "DEFAULT_MOVE_ACTIONS",
    "DEFAULT_STALE_DAYS",
    "DEFAULT_TOP",
    "DELETE_ACTION",
    "DELETE_TIERS",
    "DUPES_CATEGORY",
    "DUPES_CONFIDENCE",
    "FRESH_DAYS",
    "FRESH_RECENCY",
    "GROUP_DUPLICATES",
    "GROUP_FOLDER",
    "KINDS",
    "MOVE_TIERS",
    "STALE_UNKNOWN_CONFIDENCE",
    "TIER_WEIGHTS",
    "UNKNOWN_AGE_RECENCY",
    "Candidate",
    "CandidateReport",
    "CandidatesError",
    "KindSummary",
    "Score",
    "action_label",
    "candidate_report",
    "parent_path",
    "path_key",
    "recency_factor",
    "render_json",
    "render_text",
    "score_candidate",
    "under",
]
