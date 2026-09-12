"""The ranked Opportunities list -- the core screen's view-model (design §9).

The desktop app's second screen is *one* table of files and folders, biggest
estimated gain first, every row carrying a suggested solution.  This module is
that table's data layer: pure stdlib, no Qt, no SQL in the widgets.  The
engine's ranked candidates (:mod:`spacesage.candidates`) supply the rows the
rules can decide; a bounded scan of the biggest entries the rules explicitly
left alone (``KEEP``) or never matched ("undecided") supplies the rows that
make the list honest about what SpaceSage will *not* touch.

Vocabulary (design §9, screen 2):

``state``
    One of the three filter states the design asks for:

    * :data:`STATE_ACTION` -- the suggestion is something SpaceSage carries out
      (delete/quarantine, move, compress) or hands to the vendor tool
      (``NATIVE``); the plan makes it executable.
    * :data:`STATE_NO_ACTION` -- an explicit *No action* with its reason (the
      classifier's ``KEEP``): system and application data read as a deliberate
      decision instead of an unknown.  These rows are never hidden by default.
    * :data:`STATE_UNDECIDED` -- advice-only: stale entries, unverified
      duplicate clusters, application footprints and entries no rule matched.
      The plan demotes all of them to ``REVIEW`` (design §7.1), so the row is a
      decision for the user, not an action SpaceSage will take.

``gain``
    Bytes the *suggested* solution frees on the current drive -- the plan's own
    arithmetic: quarantines and moves free the full size, compression is an
    upper bound, and an undecided row carries its size as an upper bound that
    says "nothing changes until you decide".  A *No action* row frees nothing
    and shows ``--``.

**No double counting.**  A folder row aggregates its descendants (the engine's
file-row-only subtree bytes), so a folder and its children can both be listed.
:class:`Selection` keeps at most one row per branch: a selected folder *covers*
its descendants, which is what the list's checkbox cascade renders, and the
summary strip counts only rows no other listed row already covers.

Everything here is read-only and deterministic; ``--as-of`` (``now``) only
affects ages and the stale/recency factors, exactly as in the engine.
"""

from __future__ import annotations

import heapq
import sqlite3
import sys
import time
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from spacesage import candidates, ingest, planner, rules, stats

SCHEMA = "spacesage.opportunities/v1"
"""Schema name of the JSON twin (:func:`OpportunityList.to_dict`)."""

STATE_ACTION = "action"
STATE_NO_ACTION = "no_action"
STATE_UNDECIDED = "undecided"

STATES: tuple[str, ...] = (STATE_ACTION, STATE_NO_ACTION, STATE_UNDECIDED)
"""The three filter states, in list order."""

STATE_LABELS: Mapping[str, str] = {
    STATE_ACTION: "Has action",
    STATE_NO_ACTION: "No action",
    STATE_UNDECIDED: "Undecided",
}
"""Plain-language name of each state (the filter widget renders these)."""

EXECUTABLE_ACTIONS: tuple[str, ...] = ("DELETE_QUARANTINE", "MOVE", "COMPRESS_NTFS")
"""Actions the plan carries out itself (design §7.1)."""

ADVISORY_KINDS: tuple[str, ...] = ("stale", "dupes-weak", "app")
"""Candidate kinds the plan always demotes to ``REVIEW`` (design §7.1)."""

KIND_LABELS: Mapping[str, str] = {
    "delete": "Disposable",
    "move": "Relocatable",
    "stale": "Large and cold",
    "dupes-weak": "Duplicate cluster (unverified)",
    "app": "Application footprint",
}
"""Plain-language name of each candidate kind."""

GAIN_FULL = "full size"
GAIN_COMPRESS = "compression upper bound"
GAIN_UPPER = "upper bound -- nothing is freed until you decide"
GAIN_NONE = "nothing is freed"

DEFAULT_LIST_TOP = 500
"""Candidates listed per kind by default (``top`` of :func:`build_opportunities`)."""

DEFAULT_EXPLICIT = 250
"""Biggest ``KEEP``/unmatched entries added per side (files, folders)."""

DEFAULT_MIN_SIZE = candidates.DEFAULT_MIN_SIZE
"""Smallest entry the list considers by default (100 MiB)."""

SORT_COLUMNS: tuple[str, ...] = ("path", "size", "gain", "solution", "tier", "confidence")
"""Columns the table can order by (header clicks, ``sort_rows``)."""


class OpportunitiesError(RuntimeError):
    """Raised when the list cannot be built for the given index."""


def age_days(mtime: int | None, moment: int) -> int | None:
    """Whole days between ``mtime`` and ``moment`` (``None`` without a stamp)."""
    if mtime is None:
        return None
    return int((moment - mtime) // 86_400)


def path_key(path: str) -> str:
    """Normalised key of a path (the engine's, so relationships line up)."""
    return candidates.path_key(path)


def parent_key(key: str) -> str | None:
    """Normalised key of ``key``'s parent (``None`` for a volume root)."""
    index = key.rfind("\\")
    return key[:index] if index > 0 else None


def ancestor_keys(key: str) -> tuple[str, ...]:
    """Every ancestor key of ``key``, nearest first."""
    out: list[str] = []
    current = parent_key(key)
    while current is not None:
        out.append(current)
        current = parent_key(current)
    return tuple(out)


def descendant_prefix(key: str) -> str:
    """Prefix every descendant key of ``key`` starts with (``c:\\a\\``)."""
    return key.rstrip("\\") + "\\"


# --------------------------------------------------------------------------- #
# Rows
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class Opportunity:
    """One row of the ranked list: a path, its suggested solution and its rank."""

    key: str
    """Normalised path key (relationship arithmetic only, never displayed)."""

    path: str
    """The entry as exported (``C:\\Users\\...``)."""

    is_dir: bool
    size: int
    """Logical bytes: a file's own size, a folder's file-row subtree total."""

    gain: int
    """Estimated bytes the suggestion frees (``0`` for *No action*)."""

    gain_basis: str
    """Plain words for where ``gain`` comes from (full size, upper bound, ...)."""

    state: str
    """One of :data:`STATES` -- the filter's vocabulary."""

    action: str
    """The classifier's action (``DELETE_QUARANTINE``, ``MOVE``, ``KEEP``, ...)."""

    tier: str
    confidence: float
    category: str
    rationale: str
    why: str
    """One-line suggested solution plus its reason (the list's solution column)."""

    score: float
    """The engine's rank score (``bytes x tier x confidence x recency``)."""

    kind: str | None = None
    """Candidate kind when a ranked candidate produced this row."""

    rule_id: str | None = None
    pack: str | None = None
    native: str | None = None
    """Vendor command the rules name as the preferred alternative."""

    mtime: int | None = None
    age_days: int | None = None
    weak: bool = False
    """True when the evidence is name+size agreement, not verified bytes."""

    advisory: bool = False
    """True when the plan will demote the row to ``REVIEW`` (design §7.1)."""

    group: str | None = None
    """``folder``, ``duplicates`` or the application name of a grouped row."""

    members: tuple[str, ...] = ()
    """Paths the row covers (largest first, capped by the engine)."""

    member_count: int = 0
    member_bytes: int = 0

    explicit_scan: bool = False
    """True for rows the bounded *biggest entries* scan added, not a candidate."""

    @property
    def solution(self) -> str:
        """Plain-language name of the suggested solution."""
        return candidates.action_label(self.action)

    @property
    def state_label(self) -> str:
        """Plain-language name of the row's state."""
        return STATE_LABELS.get(self.state, self.state)

    @property
    def kind_label(self) -> str | None:
        """Plain-language name of the candidate kind (``None`` for scan rows)."""
        return KIND_LABELS.get(self.kind) if self.kind is not None else None

    @property
    def volume(self) -> str:
        """Drive (or POSIX mount hint) the row lives on."""
        return planner.volume_of(self.path)

    @property
    def gain_label(self) -> str:
        """The gain column's text: ``--``, ``1.2 GB`` or ``up to 1.2 GB``."""
        if self.gain <= 0:
            return "--"
        if self.state == STATE_ACTION and self.action != "COMPRESS_NTFS":
            return stats.format_bytes(self.gain)
        return f"up to {stats.format_bytes(self.gain)}"

    @property
    def covers_descendants(self) -> bool:
        """True when selecting this row covers everything below it."""
        return self.is_dir

    def to_dict(self) -> dict[str, object]:
        """JSON-ready mapping (``spacesage.opportunities/v1`` rows)."""
        return {
            "path": self.path,
            "is_dir": self.is_dir,
            "size": self.size,
            "gain": self.gain,
            "gain_label": self.gain_label,
            "gain_basis": self.gain_basis,
            "state": self.state,
            "action": self.action,
            "solution": self.solution,
            "tier": self.tier,
            "confidence": self.confidence,
            "category": self.category,
            "rationale": self.rationale,
            "why": self.why,
            "score": self.score,
            "kind": self.kind,
            "rule_id": self.rule_id,
            "pack": self.pack,
            "native": self.native,
            "mtime": self.mtime,
            "age_days": self.age_days,
            "weak": self.weak,
            "advisory": self.advisory,
            "group": self.group,
            "members": list(self.members),
            "member_count": self.member_count,
            "member_bytes": self.member_bytes,
            "explicit_scan": self.explicit_scan,
        }


def _solution_state(kind: str | None, action: str, rule_id: str | None) -> str:
    """Which of the three states a suggested solution puts a row in."""
    if action == "KEEP":
        return STATE_NO_ACTION
    if kind in ADVISORY_KINDS:
        # The plan demotes stale entries, unverified duplicate clusters and app
        # footprints to REVIEW (design 7.1): the row wants a human decision.
        return STATE_UNDECIDED
    if rule_id is None and kind is None:
        return STATE_UNDECIDED
    if action in EXECUTABLE_ACTIONS or action == "NATIVE":
        return STATE_ACTION
    return STATE_UNDECIDED


def _gain_basis(action: str, state: str) -> str:
    """Plain words for the source of a row's gain estimate."""
    if state == STATE_NO_ACTION:
        return GAIN_NONE
    if state == STATE_UNDECIDED:
        return GAIN_UPPER
    if action == "COMPRESS_NTFS":
        return GAIN_COMPRESS
    return GAIN_FULL


def _gain_for(action: str, state: str, size: int) -> int:
    """Bytes the suggested solution is estimated to free."""
    if state == STATE_NO_ACTION:
        return 0
    return size


def _is_advisory(kind: str | None, action: str) -> bool:
    """True when the plan will turn the row into a ``REVIEW`` item."""
    if kind in ADVISORY_KINDS:
        return True
    return action not in EXECUTABLE_ACTIONS and action != "NATIVE"


def _from_candidate(candidate: candidates.Candidate) -> Opportunity:
    """Build a row from one ranked engine candidate."""
    state = _solution_state(candidate.kind, candidate.action, candidate.rule_id)
    return Opportunity(
        key=path_key(candidate.path),
        path=candidate.path,
        is_dir=candidate.is_dir,
        size=candidate.bytes,
        gain=_gain_for(candidate.action, state, candidate.bytes),
        gain_basis=_gain_basis(candidate.action, state),
        state=state,
        action=candidate.action,
        tier=candidate.tier,
        confidence=candidate.confidence,
        category=candidate.category,
        rationale=candidate.rationale,
        why=candidate.why,
        score=candidate.score.value,
        kind=candidate.kind,
        rule_id=candidate.rule_id,
        pack=candidate.pack,
        native=candidate.native,
        mtime=candidate.mtime,
        age_days=candidate.age_days,
        weak=candidate.weak,
        advisory=_is_advisory(candidate.kind, candidate.action),
        group=candidate.group,
        members=candidate.members,
        member_count=candidate.member_count,
        member_bytes=candidate.member_bytes,
    )


def _from_classification(
    classification: rules.Classification, moment: int, *, explicit_scan: bool
) -> Opportunity:
    """Build a row from a plain classifier verdict (scan or single entry)."""
    state = _solution_state(None, classification.action, classification.rule_id)
    age = age_days(classification.mtime, moment)
    size = classification.size
    why = f"{candidates.action_label(classification.action)}: {classification.rationale}"
    return Opportunity(
        key=path_key(classification.path),
        path=classification.path,
        is_dir=classification.is_dir,
        size=size,
        gain=_gain_for(classification.action, state, size),
        gain_basis=_gain_basis(classification.action, state),
        state=state,
        action=classification.action,
        tier=classification.tier,
        confidence=classification.confidence,
        category=classification.category,
        rationale=classification.rationale,
        why=why,
        score=candidates.score_candidate(
            size, classification.tier, classification.confidence, age
        ).value,
        rule_id=classification.rule_id,
        pack=classification.pack,
        native=classification.native,
        mtime=classification.mtime,
        age_days=age,
        advisory=_is_advisory(None, classification.action),
        explicit_scan=explicit_scan,
    )


# --------------------------------------------------------------------------- #
# The bounded "biggest entries" scan
# --------------------------------------------------------------------------- #


def _top_dir_sizes(conn: sqlite3.Connection, limit: int) -> tuple[stats.DirSize, ...]:
    """The ``limit`` largest folders by file-row subtree size (streaming)."""
    if limit <= 0:
        return ()
    heap: list[tuple[int, int, stats.DirSize]] = []
    for index, size in enumerate(stats.iter_dir_sizes(conn)):
        item = (size.bytes, index, size)
        if len(heap) < limit:
            heapq.heappush(heap, item)
        elif item > heap[0]:
            heapq.heapreplace(heap, item)
    return tuple(item[2] for item in sorted(heap, key=lambda item: (-item[0], item[1])))


def _explicit_rows(
    conn: sqlite3.Connection,
    ruleset: rules.RuleSet,
    *,
    limit: int,
    moment: int,
    seen: set[str],
    min_size: int,
) -> list[Opportunity]:
    """Biggest entries the ranked candidates did not already list.

    These are the rows that tell the truth about the rest of the disk: the
    deliberate *No action* verdicts (system and application data) and the
    entries no rule matched.  Both sides -- files and folders -- are capped and
    the folders come from the engine's own subtree arithmetic, so a scan row
    aggregates its descendants exactly like a candidate row does.
    """
    rows: list[Opportunity] = []
    for file_row in stats.top_files(conn, limit):
        key = path_key(file_row.path)
        if key in seen or file_row.size < min_size:
            continue
        facts = rules.EntryFacts(
            path=file_row.path,
            name=ingest.extract_name(file_row.path, is_dir=False),
            is_dir=False,
            size=file_row.size,
            ext=file_row.ext,
            mtime=file_row.mtime,
        )
        classification = ruleset.classify(facts, now=float(moment))
        rows.append(_from_classification(classification, moment, explicit_scan=True))
        seen.add(key)

    for dir_size in _top_dir_sizes(conn, limit):
        if dir_size.depth == 0:
            continue  # an exported drive root is never an opportunity
        key = path_key(dir_size.path)
        if key in seen or dir_size.bytes < min_size:
            continue
        facts = rules.EntryFacts(
            path=dir_size.path,
            name=dir_size.name,
            is_dir=True,
            size=dir_size.bytes,
            ext=None,
            mtime=dir_size.mtime,
        )
        classification = ruleset.classify(facts, now=float(moment))
        rows.append(_from_classification(classification, moment, explicit_scan=True))
        seen.add(key)
    return rows


# --------------------------------------------------------------------------- #
# Summary, filtering, sorting
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class StateTotal:
    """Rows and estimated gain of one state (the summary strip's chips)."""

    state: str
    rows: int
    files: int
    dirs: int
    gain: int


@dataclass(frozen=True)
class CategoryTotal:
    """Rows and gain of one rule category."""

    category: str
    rows: int
    gain: int
    actionable: int


@dataclass(frozen=True)
class KindTotal:
    """Rows and gain of one candidate kind."""

    kind: str
    rows: int
    gain: int


@dataclass(frozen=True)
class VolumeTotal:
    """Rows and gain per drive -- the "where do the bytes live" line."""

    volume: str
    rows: int
    gain: int


@dataclass(frozen=True)
class OpportunitiesSummary:
    """Everything the summary strip shows, computed from the rows themselves."""

    rows: int
    files: int
    dirs: int
    actionable: int
    no_action: int
    undecided: int
    effective_rows: int
    """Rows no other listed row covers (the deduplicated figures below)."""

    gain_bytes: int
    """Estimated gain, each byte counted once (nested rows are not re-added)."""

    size_bytes: int
    """Size of the listed entries, nested rows counted once."""

    potential_bytes: int
    """Upper bound carried by undecided rows: nothing is freed until decided."""

    states: tuple[StateTotal, ...]
    categories: tuple[CategoryTotal, ...]
    kinds: tuple[KindTotal, ...]
    volumes: tuple[VolumeTotal, ...]

    @property
    def gain_label(self) -> str:
        """Human-readable estimated gain."""
        return stats.format_bytes(self.gain_bytes)

    def state_total(self, state: str) -> StateTotal:
        """The totals of one state (the summary strip's chips)."""
        for item in self.states:
            if item.state == state:
                return item
        return StateTotal(state=state, rows=0, files=0, dirs=0, gain=0)

    @property
    def size_label(self) -> str:
        """Human-readable covered size."""
        return stats.format_bytes(self.size_bytes)

    def to_dict(self) -> dict[str, object]:
        """JSON-ready mapping."""
        return {
            "rows": self.rows,
            "files": self.files,
            "dirs": self.dirs,
            "actionable": self.actionable,
            "no_action": self.no_action,
            "undecided": self.undecided,
            "effective_rows": self.effective_rows,
            "gain_bytes": self.gain_bytes,
            "size_bytes": self.size_bytes,
            "potential_bytes": self.potential_bytes,
            "states": [
                {
                    "state": item.state,
                    "rows": item.rows,
                    "files": item.files,
                    "dirs": item.dirs,
                    "gain": item.gain,
                }
                for item in self.states
            ],
            "categories": [
                {
                    "category": item.category,
                    "rows": item.rows,
                    "gain": item.gain,
                    "actionable": item.actionable,
                }
                for item in self.categories
            ],
            "kinds": [
                {"kind": item.kind, "rows": item.rows, "gain": item.gain} for item in self.kinds
            ],
            "volumes": [
                {"volume": item.volume, "rows": item.rows, "gain": item.gain}
                for item in self.volumes
            ],
        }


def top_level_rows(rows: Sequence[Opportunity]) -> tuple[Opportunity, ...]:
    """The rows no other listed row contains (the deduplicated view)."""
    ordered = sorted(rows, key=lambda row: (row.key.count("\\"), row.key))
    dirs: set[str] = set()
    out: list[Opportunity] = []
    for row in ordered:
        if any(ancestor in dirs for ancestor in ancestor_keys(row.key)):
            continue
        out.append(row)
        if row.is_dir:
            dirs.add(row.key)
    return tuple(out)


def _container_keys(rows: Sequence[Opportunity]) -> set[str]:
    """Keys of listed folders that hold another listed row.

    A folder row is the aggregate of its descendants (design §9), so when the
    list already holds something inside it, its number is just that child's
    number again -- the list stays honest by keeping one of the two.
    """
    dirs = {row.key for row in rows if row.is_dir}
    containers: set[str] = set()
    for row in rows:
        for ancestor in ancestor_keys(row.key):
            if ancestor in dirs:
                containers.add(ancestor)
    return containers


def summarise(rows: Sequence[Opportunity]) -> OpportunitiesSummary:
    """Aggregate a row list into the summary strip's figures.

    Row counts are per row, every *gain* figure counts each byte once: the
    strip never adds a child to the folder row that already covers it.
    """
    effective = top_level_rows(rows)
    effective_by_state: dict[str, int] = {}
    effective_by_category: dict[str, int] = {}
    effective_by_kind: dict[str, int] = {}
    effective_by_volume: dict[str, int] = {}
    for row in effective:
        effective_by_state[row.state] = effective_by_state.get(row.state, 0) + row.gain
        effective_by_category[row.category] = effective_by_category.get(row.category, 0) + row.gain
        effective_by_volume[row.volume] = effective_by_volume.get(row.volume, 0) + row.gain
        if row.kind is not None:
            effective_by_kind[row.kind] = effective_by_kind.get(row.kind, 0) + row.gain

    states: dict[str, list[int]] = {state: [0, 0, 0] for state in STATES}
    categories: dict[str, list[int]] = {}
    kinds: dict[str, list[int]] = {}
    volumes: dict[str, list[int]] = {}
    files = dirs = 0
    for row in rows:
        files += 1 if not row.is_dir else 0
        dirs += 1 if row.is_dir else 0
        bucket = states.setdefault(row.state, [0, 0, 0])
        bucket[0] += 1
        bucket[1] += 1 if not row.is_dir else 0
        bucket[2] += 1 if row.is_dir else 0
        category = categories.setdefault(row.category, [0, 0])
        category[0] += 1
        category[1] += 1 if row.state == STATE_ACTION else 0
        if row.kind is not None:
            kinds.setdefault(row.kind, [0])[0] += 1
        volumes.setdefault(row.volume, [0])[0] += 1

    return OpportunitiesSummary(
        rows=len(rows),
        files=files,
        dirs=dirs,
        actionable=states.get(STATE_ACTION, [0])[0],
        no_action=states.get(STATE_NO_ACTION, [0])[0],
        undecided=states.get(STATE_UNDECIDED, [0])[0],
        effective_rows=len(effective),
        gain_bytes=sum(row.gain for row in effective),
        size_bytes=sum(row.size for row in effective),
        potential_bytes=sum(row.gain for row in effective if row.state == STATE_UNDECIDED),
        states=tuple(
            StateTotal(
                state=state,
                rows=states.get(state, [0, 0, 0])[0],
                files=states.get(state, [0, 0, 0])[1],
                dirs=states.get(state, [0, 0, 0])[2],
                gain=effective_by_state.get(state, 0),
            )
            for state in sorted(set(STATES) | set(states))
        ),
        categories=tuple(
            CategoryTotal(
                category=name,
                rows=item[0],
                gain=effective_by_category.get(name, 0),
                actionable=item[1],
            )
            for name, item in sorted(
                categories.items(),
                key=lambda pair: (-effective_by_category.get(pair[0], 0), pair[0]),
            )
        ),
        kinds=tuple(
            KindTotal(kind=name, rows=item[0], gain=effective_by_kind.get(name, 0))
            for name, item in sorted(
                kinds.items(), key=lambda pair: (-effective_by_kind.get(pair[0], 0), pair[0])
            )
        ),
        volumes=tuple(
            VolumeTotal(volume=name, rows=item[0], gain=effective_by_volume.get(name, 0))
            for name, item in sorted(
                volumes.items(),
                key=lambda pair: (-effective_by_volume.get(pair[0], 0), pair[0]),
            )
        ),
    )


@dataclass(frozen=True)
class OpportunityFilter:
    """The filter bar's state (size, category, tier, state and the search box)."""

    min_size: int = 0
    category: str | None = None
    tier: str | None = None
    state: str | None = None
    text: str = ""

    @property
    def is_default(self) -> bool:
        """True when nothing is filtered out."""
        return (
            self.min_size <= 0
            and self.category is None
            and self.tier is None
            and self.state is None
            and not self.text.strip()
        )

    def to_dict(self) -> dict[str, object]:
        """JSON-ready mapping."""
        return {
            "min_size": self.min_size,
            "category": self.category,
            "tier": self.tier,
            "state": self.state,
            "text": self.text,
        }


def _search_haystack(row: Opportunity) -> str:
    """Everything the search box looks at (paths, solution, reasons, category)."""
    return " ".join(
        part
        for part in (
            row.path,
            row.solution,
            row.why,
            row.rationale,
            row.category,
            row.rule_id or "",
            row.kind_label or "",
            row.group or "",
        )
        if part
    ).casefold()


def matches(row: Opportunity, filter_: OpportunityFilter) -> bool:
    """True when ``row`` passes every filter that is set."""
    if row.size < filter_.min_size:
        return False
    if filter_.category is not None and row.category != filter_.category:
        return False
    if filter_.tier is not None and row.tier != filter_.tier:
        return False
    if filter_.state is not None and row.state != filter_.state:
        return False
    needle = filter_.text.strip().casefold()
    return not needle or needle in _search_haystack(row)


def apply_filter(
    rows: Sequence[Opportunity], filter_: OpportunityFilter
) -> tuple[Opportunity, ...]:
    """The rows ``filter_`` lets through, in the order given."""
    if filter_.is_default:
        return tuple(rows)
    return tuple(row for row in rows if matches(row, filter_))


_NUMERIC_SORT_COLUMNS: frozenset[str] = frozenset({"size", "gain", "confidence"})


def _sort_primary(column: str, row: Opportunity) -> float | str:
    """The primary sort value of one row for ``column``."""
    if column == "size":
        return row.size
    if column == "gain":
        return row.gain
    if column == "confidence":
        return row.confidence
    if column == "solution":
        return row.solution
    if column == "tier":
        return row.tier
    if column == "path":
        return row.key
    raise OpportunitiesError(
        f"unknown sort column {column!r}; use one of {', '.join(SORT_COLUMNS)}"
    )


def sort_rows(
    rows: Sequence[Opportunity], column: str = "gain", *, descending: bool = True
) -> tuple[Opportunity, ...]:
    """Order ``rows`` by ``column`` (``gain`` desc is the list's default).

    Numeric columns order biggest-first when ``descending``; every order keeps a
    deterministic path tiebreak, so the same index always lists the same way.
    """
    if column not in SORT_COLUMNS:
        raise OpportunitiesError(
            f"unknown sort column {column!r}; use one of {', '.join(SORT_COLUMNS)}"
        )
    if column in _NUMERIC_SORT_COLUMNS:
        if descending:
            return tuple(
                sorted(rows, key=lambda row: (-float(_sort_primary(column, row)), row.key))
            )
        return tuple(sorted(rows, key=lambda row: (float(_sort_primary(column, row)), row.key)))
    if descending:
        return tuple(sorted(rows, key=lambda row: str(_sort_primary(column, row)), reverse=True))
    return tuple(sorted(rows, key=lambda row: (str(_sort_primary(column, row)), row.key)))


def option_values(rows: Sequence[Opportunity], field: str) -> tuple[str, ...]:
    """Distinct non-empty values of ``category``/``tier``/``kind``, sorted."""
    if field == "category":
        values = {row.category for row in rows}
    elif field == "tier":
        values = {row.tier for row in rows}
    elif field == "kind":
        values = {row.kind for row in rows if row.kind is not None}
    elif field == "state":
        values = {row.state for row in rows}
    else:
        raise OpportunitiesError(f"unknown option field {field!r}")
    return tuple(sorted(value for value in values if value))


# --------------------------------------------------------------------------- #
# Selection (the checkbox cascade)
# --------------------------------------------------------------------------- #


class Selection:
    """Which rows are checked -- one row per branch, never a folder *and* its child.

    The engine treats a folder row as the aggregate of its descendants, so
    selecting both would count the same bytes twice.  The rules are:

    * checking a folder covers its descendants (they render as covered and
      cannot be checked themselves);
    * checking a covered row moves the selection down to it -- the covering
      ancestor is unchecked, because the click says "this one";
    * unchecking a row releases whatever it covered.

    The invariant is that no checked row has a checked ancestor, which is what
    makes :meth:`total_gain` a plain sum.
    """

    def __init__(self, rows: Sequence[Opportunity]) -> None:
        self._rows: dict[str, Opportunity] = {row.key: row for row in rows}
        self._ancestors: dict[str, tuple[str, ...]] = {
            key: ancestor_keys(key) for key in self._rows
        }
        self._selected: set[str] = set()

    # -- queries ---------------------------------------------------------- #

    def __len__(self) -> int:
        return len(self._selected)

    def __contains__(self, key: object) -> bool:
        return key in self._selected

    @property
    def keys(self) -> tuple[str, ...]:
        """Checked keys, in row order."""
        return tuple(row.key for row in self._rows.values() if row.key in self._selected)

    @property
    def rows(self) -> tuple[Opportunity, ...]:
        """The checked rows themselves."""
        return tuple(row for row in self._rows.values() if row.key in self._selected)

    def is_selected(self, key: str) -> bool:
        """True when ``key`` itself is checked."""
        return key in self._selected

    def covering_ancestor(self, key: str) -> str | None:
        """The nearest checked ancestor of ``key``, if any."""
        for ancestor in self._ancestors.get(key, ()):
            if ancestor in self._selected:
                return ancestor
        return None

    def is_covered(self, key: str) -> bool:
        """True when a checked ancestor already counts ``key``'s bytes."""
        return self.covering_ancestor(key) is not None

    def has_selected_descendant(self, key: str) -> bool:
        """True when something below ``key`` is checked (a partial folder)."""
        prefix = descendant_prefix(key)
        return any(selected.startswith(prefix) for selected in self._selected)

    def state(self, key: str) -> str:
        """``checked``, ``partial`` or ``unchecked`` -- what the box renders."""
        if key in self._selected:
            return "checked"
        if self.is_covered(key):
            return "covered"
        if self.has_selected_descendant(key):
            return "partial"
        return "unchecked"

    @property
    def gain(self) -> int:
        """Estimated gain of the checked rows (no branch is counted twice)."""
        return sum(row.gain for row in self.rows)

    @property
    def size(self) -> int:
        """Size of the checked rows."""
        return sum(row.size for row in self.rows)

    @property
    def actionable(self) -> int:
        """How many checked rows carry an action SpaceSage can take."""
        return sum(1 for row in self.rows if row.state == STATE_ACTION)

    # -- mutation --------------------------------------------------------- #

    def _release_descendants(self, key: str, affected: set[str]) -> None:
        """Uncheck everything below ``key`` (a folder was just checked)."""
        prefix = descendant_prefix(key)
        for selected in [item for item in self._selected if item.startswith(prefix)]:
            self._selected.discard(selected)
            affected.add(selected)

    def _release_ancestors(self, key: str, affected: set[str]) -> None:
        """Uncheck the checked ancestors of ``key`` (the selection moved down)."""
        for ancestor in self._ancestors.get(key, ()):
            if ancestor in self._selected:
                self._selected.discard(ancestor)
                affected.add(ancestor)

    def _touch(self, key: str) -> set[str]:
        """Keys whose checkbox may have changed because ``key`` did."""
        touched = {key}
        touched.update(self._ancestors.get(key, ()))
        row = self._rows.get(key)
        if row is not None and row.is_dir:
            prefix = descendant_prefix(key)
            touched.update(item for item in self._rows if item.startswith(prefix))
        return touched

    def toggle(self, key: str) -> tuple[str, ...]:
        """Flip ``key``'s checkbox and return the keys whose state may have changed."""
        if key not in self._rows:
            return ()
        touched = self._touch(key)
        if key in self._selected:
            self._selected.discard(key)
        else:
            self._release_ancestors(key, touched)
            self._release_descendants(key, touched)
            self._selected.add(key)
        return tuple(sorted(touched))

    def set_selected(self, key: str, selected: bool) -> tuple[str, ...]:
        """Check or uncheck ``key`` explicitly."""
        if key not in self._rows or (key in self._selected) == selected:
            return ()
        return self.toggle(key)

    def select_all(self, keys: Iterable[str] | None = None) -> tuple[str, ...]:
        """Check every candidate key, folders first (their children stay covered).

        ``keys`` defaults to every row; keys already covered by a checked folder
        are skipped rather than listed separately.
        """
        pool = list(self._rows) if keys is None else [key for key in keys if key in self._rows]
        touched: set[str] = set()
        for key in sorted(pool, key=lambda item: (item.count("\\"), item)):
            if key in self._selected or self.is_covered(key):
                continue
            touched.add(key)
            touched.update(self._touch(key))
            self._selected.add(key)
        return tuple(sorted(touched))

    def clear(self) -> tuple[str, ...]:
        """Uncheck everything and return the keys that changed."""
        touched = tuple(sorted(self._selected))
        self._selected.clear()
        return touched


# --------------------------------------------------------------------------- #
# Details pane: alternatives, effects, destinations
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class Alternative:
    """One way the details pane can suggest handling an entry."""

    action: str
    """Plan action type the alternative would become."""

    label: str
    gain: int
    """Bytes the alternative would free (``0`` for advice-only options)."""

    note: str
    available: bool = True
    reason: str = ""
    """Why the alternative is not on the table (empty when it is)."""

    @property
    def gain_label(self) -> str:
        """Gain of the alternative, or ``--`` when it frees nothing."""
        return "--" if self.gain <= 0 else stats.format_bytes(self.gain)


def alternatives(
    row: Opportunity, *, target: str | None = None, platform: str | None = None
) -> tuple[Alternative, ...]:
    """The courses of action the details pane offers for one row.

    Always the same list, in the design's order -- delete, move, compress,
    native, link/dedupe, review -- with availability and the reason it is (or is
    not) on the table, so the pane never invents an option the engine refuses.
    """
    system = platform if platform is not None else sys.platform
    out: list[Alternative] = []

    report_only = row.tier == "T3"
    out.append(
        Alternative(
            action="DELETE_QUARANTINE",
            label="Delete (quarantine)",
            gain=row.size,
            note=planner.side_effects_of("DELETE_QUARANTINE") or "",
            available=not report_only,
            reason="report-only tier: T3 data is never deleted by SpaceSage" if report_only else "",
        )
    )

    if row.native is not None:
        out.append(
            Alternative(
                action="NATIVE",
                label="Use the native tool",
                gain=row.size,
                note=f"{planner.side_effects_of('NATIVE') or ''} Command: {row.native}",
            )
        )

    if target is None:
        out.append(
            Alternative(
                action="MOVE",
                label="Move to another drive",
                gain=row.size,
                note="Destinations are planned against a target drive.",
                available=False,
                reason="no target drive chosen (pick one on Import)",
            )
        )
    else:
        try:
            dest = planner.destination(row.path, target=target)
            same = planner.same_volume(row.path, target)
            link, elevated = planner.link_policy(row.is_dir, same_volume_=same)
            note = f"Lands in {dest}; the original path becomes a {link.lower()}"
            note += " (needs elevation on Windows)." if elevated else "."
            out.append(
                Alternative(
                    action="MOVE",
                    label=f"Move to {target}",
                    gain=row.size,
                    note=note,
                    available=not same,
                    reason="the target is on the same volume, so nothing is freed" if same else "",
                )
            )
        except planner.PlannerError as exc:
            out.append(
                Alternative(
                    action="MOVE",
                    label=f"Move to {target}",
                    gain=row.size,
                    note="",
                    available=False,
                    reason=str(exc),
                )
            )

    windows = system.startswith("win")
    compress_note = planner.side_effects_of("COMPRESS_NTFS") or ""
    out.append(
        Alternative(
            action="COMPRESS_NTFS",
            label="Compress (NTFS)",
            gain=row.size,
            note=f"{compress_note} Upper bound: media and archives do not shrink.",
            available=windows and not row.is_dir,
            reason=""
            if windows and not row.is_dir
            else (
                "NTFS compression needs Windows"
                if not windows
                else "folders are moved, not compressed"
            ),
        )
    )

    if row.weak:
        out.append(
            Alternative(
                action="REVIEW",
                label="Link or dedupe",
                gain=row.gain,
                note=(
                    "Name and size agree but the bytes are unverified; the deep scan proves "
                    "which copies are identical before anything is planned."
                ),
                available=False,
                reason="run the deep scan (spacesage deepscan) to verify the duplicates",
            )
        )

    out.append(
        Alternative(
            action="REVIEW",
            label="Review",
            gain=0,
            note=planner.side_effects_of("REVIEW") or "",
        )
    )
    return tuple(out)


def side_effects(row: Opportunity, *, target: str | None = None) -> str:
    """The sentence the plan would carry for this row's action.

    Delete/compress/native wording is the plan's own (``planner.side_effects_of``);
    a move is composed here because it depends on the destination the user picks.
    """
    if row.action == "MOVE" and row.state == STATE_ACTION:
        if target is None:
            return (
                "Moved to the target drive you pick; the original path is linked back so "
                "apps keep finding it."
            )
        dest = destination_for(row, target)
        link, elevated = link_for(row, target)
        if link != "NONE":
            text = (
                f"Moved to {dest} and linked back with a {planner.link_label(link)}; "
                "the original path keeps resolving."
            )
        else:
            text = f"Moved to {dest}; the original path disappears."
        if elevated:
            text += " Creating the link needs elevation (or Developer Mode) on Windows."
        return text
    plan_text = planner.side_effects_of(row.action)
    if plan_text is not None:
        return plan_text
    if row.state == STATE_NO_ACTION:
        return "Nothing happens: the rules say this entry should be left alone."
    return "Nothing changes until you decide; the row only records what to look at."


def advisory_note(row: Opportunity) -> str | None:
    """Why an advisory row is *undecided* even when it names an action.

    The plan demotes these rows to ``REVIEW`` (design §7.1), so the pane has to
    say what the suggestion does and does not cover.
    """
    if not row.advisory:
        return None
    if row.kind == "app":
        return (
            "SpaceSage never acts on a whole application footprint: the plan lists it for "
            "review, and the advice above is for the biggest entry inside it."
        )
    if row.kind == "dupes-weak":
        return (
            "Name and size agree but the bytes are unverified; the deep scan proves which "
            "copies are identical before anything is planned."
        )
    if row.kind == "stale":
        return (
            "Nothing has written to this entry for a long time and no rule claims it: "
            "archiving, moving or deleting it is your call."
        )
    if row.state == STATE_ACTION:
        return "The plan hands this to the vendor tool; SpaceSage itself changes nothing."
    return "The rules flag this for a human decision rather than suggesting an action."


def destination_for(row: Opportunity, target: str) -> str:
    """Planned destination of a movable row, or ``""`` when it is not movable."""
    try:
        return planner.destination(row.path, target=target)
    except planner.PlannerError:
        return ""


def link_for(row: Opportunity, target: str) -> tuple[str, bool]:
    """``(link_after, elevation_required)`` for a move of ``row`` to ``target``."""
    same = planner.same_volume(row.path, target)
    return planner.link_policy(row.is_dir, same_volume_=same)


# --------------------------------------------------------------------------- #
# The list itself
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class OpportunityList:
    """The Opportunities table's data: rows, their summary and their provenance."""

    generated: datetime
    rows: tuple[Opportunity, ...]
    summary: OpportunitiesSummary
    db_path: str | None
    as_of: int
    min_size: int
    list_top: int
    explicit: int
    kinds: tuple[str, ...]
    candidates_considered: int
    """Ranked candidates the engine produced across the requested kinds."""

    explicit_added: int
    """Rows the bounded *biggest entries* scan contributed."""

    def __len__(self) -> int:
        return len(self.rows)

    def row(self, path: str) -> Opportunity | None:
        """The listed row for ``path`` (``None`` when it is not on the list)."""
        key = path_key(path)
        for item in self.rows:
            if item.key == key:
                return item
        return None

    def heading(self) -> str:
        """One-line description of the dataset (the status bar's left side)."""
        return (
            f"{len(self.rows)} opportunities · {self.summary.gain_label} estimated gain · "
            f"as of {datetime.fromtimestamp(self.as_of, tz=UTC):%Y-%m-%d %H:%M} UTC"
        )

    def to_dict(self) -> dict[str, object]:
        """JSON-ready document (``spacesage.opportunities/v1``)."""
        return {
            "schema": SCHEMA,
            "generated": self.generated.isoformat(),
            "db": self.db_path,
            "as_of": self.as_of,
            "filters": {"min_size": self.min_size, "list_top": self.list_top},
            "kinds": list(self.kinds),
            "candidates_considered": self.candidates_considered,
            "explicit_added": self.explicit_added,
            "summary": self.summary.to_dict(),
            "items": [row.to_dict() for row in self.rows],
        }


def build_opportunities(
    conn: sqlite3.Connection,
    ruleset: rules.RuleSet,
    *,
    kinds: Sequence[str] = candidates.KINDS,
    min_size: int = DEFAULT_MIN_SIZE,
    list_top: int = DEFAULT_LIST_TOP,
    explicit: int = DEFAULT_EXPLICIT,
    now: float | None = None,
    stale_after_days: float = candidates.DEFAULT_STALE_DAYS,
    dupes_min_copies: int = candidates.DEFAULT_DUPES_MIN_COPIES,
    dupes_floor: int = candidates.DEFAULT_DUPES_FLOOR,
    db_path: str | None = None,
) -> OpportunityList:
    """Build the ranked list for an index: candidates first, then the rest.

    The ranked candidates (design §6.2) are the list's spine; the bounded scan
    adds the biggest entries they did not cover so the screen can show the
    explicit *No action* rows and the undecided ones as well.  Read-only: no
    table is written, no file is touched.
    """
    if min_size < 0:
        raise OpportunitiesError(f"min_size must not be negative, got {min_size}")
    if list_top < 0:
        raise OpportunitiesError(f"list_top must be >= 0 (0 = no limit), got {list_top}")
    if explicit < 0:
        raise OpportunitiesError(f"explicit must not be negative, got {explicit}")

    moment = int(now if now is not None else time.time())
    report = candidates.candidate_report(
        conn,
        ruleset,
        kinds=kinds,
        min_size=min_size,
        top=list_top,
        now=moment,
        stale_after_days=stale_after_days,
        dupes_min_copies=dupes_min_copies,
        dupes_floor=dupes_floor,
        db_path=db_path,
    )

    rows: list[Opportunity] = []
    seen: set[str] = set()
    for block in report.kinds:
        for candidate in block.candidates:
            key = path_key(candidate.path)
            if key in seen:
                continue
            rows.append(_from_candidate(candidate))
            seen.add(key)

    scan_rows = _explicit_rows(
        conn, ruleset, limit=explicit, moment=moment, seen=seen, min_size=min_size
    )
    # A scan row that merely contains rows already on the list would repeat
    # their numbers; the ranked candidates are exempt (the engine's own
    # suppression policy already decided what a claimed folder means), and so
    # are the explicit *No action* rows -- they free nothing, so they cannot
    # double count, and they are the proof that a subtree was left alone.
    containers = _container_keys([*rows, *scan_rows])
    kept_scan = [
        row for row in scan_rows if row.key not in containers or row.state == STATE_NO_ACTION
    ]
    rows.extend(kept_scan)
    ranked = sort_rows(rows, "gain", descending=True)

    return OpportunityList(
        generated=datetime.now(tz=UTC),
        rows=ranked,
        summary=summarise(ranked),
        db_path=db_path,
        as_of=moment,
        min_size=min_size,
        list_top=list_top,
        explicit=explicit,
        kinds=tuple(block.kind for block in report.kinds),
        candidates_considered=report.considered,
        explicit_added=len(kept_scan),
    )


def rows_from_dicts(items: Iterable[Mapping[str, Any]]) -> tuple[Opportunity, ...]:
    """Rebuild rows from :meth:`Opportunity.to_dict` output (tests, fixtures)."""
    fields = set(Opportunity.__dataclass_fields__)
    out: list[Opportunity] = []
    for item in items:
        payload = {key: value for key, value in item.items() if key in fields}
        payload["members"] = tuple(payload.get("members") or ())
        payload.setdefault("key", path_key(str(payload["path"])))
        out.append(Opportunity(**payload))
    return tuple(out)


__all__ = [
    "ADVISORY_KINDS",
    "DEFAULT_EXPLICIT",
    "DEFAULT_LIST_TOP",
    "DEFAULT_MIN_SIZE",
    "EXECUTABLE_ACTIONS",
    "GAIN_COMPRESS",
    "GAIN_FULL",
    "GAIN_NONE",
    "GAIN_UPPER",
    "KIND_LABELS",
    "SCHEMA",
    "SORT_COLUMNS",
    "STATES",
    "STATE_ACTION",
    "STATE_LABELS",
    "STATE_NO_ACTION",
    "STATE_UNDECIDED",
    "Alternative",
    "CategoryTotal",
    "KindTotal",
    "OpportunitiesError",
    "OpportunitiesSummary",
    "Opportunity",
    "OpportunityFilter",
    "OpportunityList",
    "Selection",
    "StateTotal",
    "VolumeTotal",
    "advisory_note",
    "age_days",
    "alternatives",
    "ancestor_keys",
    "apply_filter",
    "build_opportunities",
    "destination_for",
    "link_for",
    "matches",
    "option_values",
    "parent_key",
    "path_key",
    "rows_from_dicts",
    "side_effects",
    "sort_rows",
    "summarise",
    "top_level_rows",
]
