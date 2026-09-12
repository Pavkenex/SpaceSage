"""Plan generation: the course of action (``plan.json`` v1).

The ``plan`` stage composes the ranked candidates of :mod:`spacesage.candidates`
into **one ordered, itemized plan** -- the contract between the analysis and the
executor (``docs/design.md`` section 7).  Everything the app shows before a byte
moves comes from here: which items are executable, where each move goes, which
link keeps the old path alive, what each action is worth, and why an item that
looks actionable is only a *review* item instead.

The contract
------------

``plan.json`` (schema ``spacesage.plan/v1``) holds:

``schema``
    ``spacesage.plan/v1``.
``plan_id``
    ``sha256:<hex>`` over the canonical action list and the source metadata
    (:func:`compute_plan_id`).  It is a function of the *inputs*, not of the
    wall clock: two runs over the same index, rules, targets and reference time
    produce the same id.  The approval manifest binds to it (design section 7).
``created``
    wall-clock timestamp of the run (volatile, deliberately outside the id).
``source``
    ``csv`` / ``machine`` / ``exported`` -- where the index came from.
``targets``
    the destination drives the user picked: ``{name: {free_bytes, reserve_bytes}}``.
``provenance``
    the engine's own inputs (index, schema version, reference time, rules
    fingerprint, thresholds) so a plan can be traced back to a run.
``summary``
    per-outcome byte totals, the planned total, what was discounted and what
    was dropped (see :class:`PlanSummary`).
``actions``
    the ordered list; ``a1``, ``a2``, ... are assigned after ordering, so the
    same inputs always produce the same ids.

Action vocabulary
-----------------

===================== =====================================================
type                  meaning
===================== =====================================================
``DELETE_QUARANTINE`` quarantine the entry (delete always means quarantine)
``MOVE``              move it to a target drive; ``link_after`` keeps the
                      original path alive (``JUNCTION`` / ``SYMLINK`` /
                      ``HARDLINK`` / ``NONE``; symlinks need elevation and say
                      so via ``elevation_required``)
``COMPRESS_NTFS``     compress in place
``REVIEW``            nothing happens until a human decides
``NATIVE``            the fix is a command for the vendor's own tool; SpaceSage
                      never runs it
===================== =====================================================

Every action carries the same keys (``null`` where a type has no use for one),
so the executor and the GUI can read the contract without special cases:
``id``, ``type``, ``kind``, ``path``, ``bytes``, ``category``, ``tier``,
``confidence``, ``rationale``, ``why``, ``side_effects``, ``native_alt``,
``dest``, ``link_after``, ``elevation_required``, ``command``, ``covered_bytes``
and ``weak``.  ``why`` is the one-line decision ("Delete (quarantine): ...",
"Review: no target drive has room ..."), ``rationale`` the rule's own reason.

What becomes executable
-----------------------

Only candidates the classifier actually *told* us to act on, of tier T1/T2, and
never the footprint summaries:

* ``delete`` candidates (advice ``DELETE_QUARANTINE``) -> ``DELETE_QUARANTINE``;
* ``move`` candidates (advice ``MOVE``) -> ``MOVE`` when a target drive is
  available and has room under its reserve; otherwise a ``REVIEW`` that says
  exactly what is missing;
* ``move`` candidates whose advice is ``NATIVE`` -> ``NATIVE`` with the vendor
  command (a launcher-managed game library is moved by its launcher, not by us);
* anything else -- ``stale``, ``dupes-weak`` (bytes unverified until the deep
  scan), ``app`` footprints, ``KEEP``/``REVIEW`` advice, or any T3 path --
  becomes ``REVIEW``.  **T3 is report-only**: no executable action may carry it,
  and :func:`validate_plan` enforces that.

Move planning
-------------

Directories are moved whole; a folder that only *grouped* its matching files is
resolved back to those files against the index, so the ``max_members`` cap of
the ranked list never truncates a plan.  Each member becomes its own ``MOVE``
action and the move half of the group has to fit the target budget.  A moved
file that the rules want handled by a launcher keeps its ``NATIVE`` action.
Destinations preserve the source's relative structure below
``<target>\\Moved`` (design section 7): ``C:\\Users\\Alice\\Videos`` ->
``D:\\Moved\\Users\\Alice\\Videos``.

Budgets are honoured strictly: a target can take moves while
``bytes <= free_bytes - reserve_bytes``.  Targets are tried in the order they
were given, a target on the source's own volume is never used ("a move there
frees nothing"), and a move that fits nowhere is demoted to ``REVIEW`` rather
than silently dropped or over-committed.

No double counting
------------------

The ranked list may hold a folder *and* one of its claimed children (a media
folder whose cache folder is on the quarantine list).  The plan reconciles them
in action order: an action's ``bytes`` exclude everything already carried by an
earlier planned action inside it (``covered_bytes`` records the difference), and
a candidate left with nothing to act on is dropped and counted in
``summary.dropped``.  Byte totals therefore never sum the same bytes twice.

Ordering
--------

Phase order, per design section 7: T1/T2 quarantines (T1 first, biggest gain
first) -> moves (biggest first) -> compressions -> review and native actions
(reviews first, biggest first).
"""

from __future__ import annotations

import hashlib
import json
import re
import shutil
import sqlite3
import time
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime

from spacesage import candidates, db, rules, stats

SCHEMA = "spacesage.plan/v1"
"""Schema string of the plan document (:func:`render_json`)."""

TYPES: tuple[str, ...] = (
    "DELETE_QUARANTINE",
    "MOVE",
    "COMPRESS_NTFS",
    "REVIEW",
    "NATIVE",
)
"""Action types of plan v0.1 (design section 7)."""

EXECUTABLE_TYPES: tuple[str, ...] = ("DELETE_QUARANTINE", "MOVE", "COMPRESS_NTFS")
"""Types the executor performs; ``REVIEW``/``NATIVE`` are advisory only."""

LINKS: tuple[str, ...] = ("JUNCTION", "SYMLINK", "HARDLINK", "NONE")
"""``link_after`` vocabulary of a ``MOVE`` action."""

ELEVATED_LINKS: tuple[str, ...] = ("SYMLINK",)
"""Links Windows only creates with ``SeCreateSymbolicLinkPrivilege`` (or Developer Mode)."""

MOVE_ROOT = "Moved"
"""Folder created under every target drive; destinations mirror the source below it."""

DEFAULT_RESERVE = 0
"""Bytes kept free on a target unless ``--reserve`` says otherwise."""

REPORT_TIER = "T3"
"""Report-only tier: never touches an executable action."""

ADVISORY_TYPES: tuple[str, ...] = ("REVIEW", "NATIVE")
"""Types that never reclaim bytes on their own."""

PHASES: tuple[tuple[str, ...], ...] = (
    ("DELETE_QUARANTINE",),
    ("MOVE",),
    ("COMPRESS_NTFS",),
    ADVISORY_TYPES,
)
"""Execution phases, in order; the plan's action list follows it."""

HEADINGS: Mapping[str, str] = {
    "DELETE_QUARANTINE": "Delete (quarantine)",
    "MOVE": "Move",
    "COMPRESS_NTFS": "Compress (NTFS)",
    "REVIEW": "Review",
    "NATIVE": "Native tool",
}
"""Section titles of the Markdown summary, per action type."""

_PLAN_ID_RE = re.compile(r"^sha256:[0-9a-f]{64}$")
_ACTION_ID_RE = re.compile(r"^a[1-9][0-9]*$")
_WINDOWS_SPEC_RE = re.compile(r"^[A-Za-z]:[\\/]?$")
_WINDOWS_PREFIX_RE = re.compile(r"^(?:[A-Za-z]:[\\/]|[A-Za-z]:$|\\\\)")
_GROUP_FILES_SQL = """
SELECT path, name, size, ext, mtime, hardlink_flag
FROM entries
WHERE is_dir = 0 AND parent_id = (SELECT id FROM entries WHERE path = ?)
ORDER BY path ASC
"""

_SIDE_EFFECTS: Mapping[str, str] = {
    # One sentence per action type, also rendered by the desktop app (screen 2):
    # the GUI must not invent its own wording for what an action changes.
    "DELETE_QUARANTINE": (
        "Quarantined, never erased: the entry moves into the quarantine store and can be "
        "moved back until the store is purged."
    ),
    "COMPRESS_NTFS": (
        "Compressed in place; reads and writes keep working, at the cost of some CPU."
    ),
    "REVIEW": "Nothing changes until you decide; this action only records what to look at.",
    "NATIVE": "SpaceSage does not run this; the vendor tool performs the change outside the app.",
}


class PlannerError(RuntimeError):
    """Raised when a plan cannot be composed or a document fails validation."""


# --------------------------------------------------------------------------- #
# Targets and paths
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class PlanTarget:
    """One destination drive with its live free space and the reserve kept free."""

    name: str
    """The drive or path the user picked (``D:``), as written in ``targets``."""

    free_bytes: int
    reserve_bytes: int = DEFAULT_RESERVE

    @property
    def budget(self) -> int:
        """Bytes the plan may fill: ``free_bytes - reserve_bytes`` (never negative)."""
        return max(0, self.free_bytes - self.reserve_bytes)

    def to_dict(self) -> dict[str, object]:
        """JSON-ready mapping (one ``targets`` entry)."""
        return {"free_bytes": self.free_bytes, "reserve_bytes": self.reserve_bytes}


def target_root(name: str) -> str:
    """Normalised root of a target spec: ``D:`` -> ``D:\\``, a path keeps its shape."""
    value = name.strip()
    if not value:
        raise PlannerError("target name must not be empty")
    if _WINDOWS_SPEC_RE.match(value):
        return value[:2] + "\\"
    if value.startswith("\\\\"):
        return value.rstrip("\\/")
    return value.rstrip("\\/") or "/"


def measure_free_space(name: str) -> int:
    """Free bytes of a target on this machine (``shutil.disk_usage``).

    Used when the user did not state the value with ``--free``; a target that
    cannot be measured (an unmounted drive, an export from another machine) is a
    hard error naming the flag that fixes it.
    """
    root = target_root(name)
    try:
        return int(shutil.disk_usage(root).free)
    except OSError as exc:
        raise PlannerError(
            f"cannot measure the free space of target {name!r} ({root}): {exc}; "
            "pass --free to state it"
        ) from None


def target_from_spec(
    spec: str, *, reserve: int = DEFAULT_RESERVE, free: int | None = None
) -> PlanTarget:
    """Build a :class:`PlanTarget` from a CLI-style spec.

    ``free`` states the free space explicitly (scripting, tests, and drives this
    machine cannot measure); without it the value is measured on the live system.
    """
    if reserve < 0:
        raise PlannerError(f"reserve must not be negative, got {reserve}")
    if free is not None and free < 0:
        raise PlannerError(f"free space must not be negative, got {free}")
    return PlanTarget(
        name=spec.strip(),
        free_bytes=measure_free_space(spec) if free is None else free,
        reserve_bytes=reserve,
    )


def _volume_of(path: str) -> str:
    """Volume key of a path: ``c:``, ``\\\\server\\share`` or the POSIX first component.

    On POSIX the first path component stands in for a mount point -- enough for
    the planner's *same drive* check, which only has to be certain about the
    Windows layouts the product targets.
    """
    if _WINDOWS_SPEC_RE.match(path) or path.endswith(":"):
        return path[:2].lower()
    if path.startswith("\\\\"):
        parts = path.replace("/", "\\").split("\\")
        return "\\".join(parts[:4]).lower().rstrip("\\")
    if _WINDOWS_PREFIX_RE.match(path):
        return path[:2].lower()
    stripped = path.lstrip("/")
    return "/" + (stripped.split("/", 1)[0] if stripped else "")


def volume_of(path: str) -> str:
    """Volume key of a path, as §7 plans it (``c:``, ``\\\\server\\share``, ``/home``).

    Public face of the *same volume* arithmetic :func:`same_volume` uses, so the
    desktop app can group and label a list by drive without a second rule.
    """
    return _volume_of(path)


def side_effects_of(action_type: str) -> str | None:
    """One plain sentence about what ``action_type`` changes, or ``None``.

    The desktop app renders the same wording the plan document carries
    (design §9.1: every control earns its place -- one source of truth).  A
    ``MOVE`` is destination-dependent, so it is composed by
    :func:`spacesage.opportunities.side_effects`; ``KEEP`` has no plan action
    and therefore no entry here.
    """
    return _SIDE_EFFECTS.get(action_type)


def link_label(link: str) -> str:
    """Plain-language name of a link type (``JUNCTION`` -> "directory junction")."""
    return _link_label(link)


def same_volume(path: str, target: str) -> bool:
    """True when ``path`` and the target root live on the same volume."""
    return _volume_of(path) == _volume_of(target_root(target))


def relative_parts(path: str) -> tuple[str, ...]:
    """Components of ``path`` below its root (``C:\\a\\b`` -> ``('a', 'b')``)."""
    if _WINDOWS_SPEC_RE.match(path) or path.endswith(":"):
        raise PlannerError(f"{path!r} is a drive root, not a movable entry")
    if _WINDOWS_PREFIX_RE.match(path):
        parts = path.replace("/", "\\").split("\\")
        rest = parts[4:] if path.startswith("\\\\") else parts[1:]
        return tuple(part for part in rest if part)
    parts = path.replace("\\", "/").strip("/").split("/")
    return tuple(part for part in parts if part)


def destination(path: str, *, target: str, move_root: str = MOVE_ROOT) -> str:
    """Destination of ``path`` on ``target``, mirroring the source's structure.

    ``C:\\Users\\Alice\\Videos`` moved to ``D:`` lands in
    ``D:\\Moved\\Users\\Alice\\Videos``: the part below the source root is kept
    component by component, re-joined in the target's separator style.
    """
    root = target_root(target)
    parts = relative_parts(path)
    if not parts:
        raise PlannerError(f"cannot move {path!r}: it has no structure below its root")
    if _WINDOWS_PREFIX_RE.match(root):
        # Windows-style target: "D:" -> "D:\Moved\..."; UNC shares keep their shape.
        base = root.rstrip("\\/")
        return f"{base}\\" + "\\".join([move_root, *parts])
    return f"{root.rstrip('/')}/{move_root}/" + "/".join(parts)


def link_policy(is_dir: bool, *, same_volume_: bool, links: bool = True) -> tuple[str, bool]:
    """``(link_after, elevation_required)`` for a moved entry (design section 7).

    Directories get a ``JUNCTION`` (no admin needed); files get a ``SYMLINK``
    when they leave the volume -- flagged, because Windows needs
    ``SeCreateSymbolicLinkPrivilege`` or Developer Mode for those -- and a
    ``HARDLINK`` when they stay on it (hard links only ever link within one
    volume); ``--no-links`` turns linking off entirely.
    """
    if not links:
        return ("NONE", False)
    if is_dir:
        return ("JUNCTION", False)
    if same_volume_:
        return ("HARDLINK", False)
    return ("SYMLINK", True)


# --------------------------------------------------------------------------- #
# Plan documents
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class PlanAction:
    """One itemized step of the course of action."""

    id: str
    """``a1``, ``a2``, ... in plan order (assigned after ordering)."""

    type: str
    """One of :data:`TYPES`."""

    kind: str
    """The candidate kind this action came from (``delete``, ``move``, ...)."""

    path: str
    bytes: int
    """Bytes the action reclaims -- net of :attr:`covered_bytes`, planned elsewhere."""

    category: str
    tier: str
    confidence: float
    rationale: str
    """The rule's own reasoning (or the fallback's)."""

    why: str
    """The one-line decision shown in the UI."""

    weak: bool = False
    """True on review items whose bytes are unverified (weak duplicates)."""

    side_effects: str | None = None
    native_alt: str | None = None
    """The vendor tool from the rule; ``None`` on ``NATIVE``, which carries ``command``."""

    dest: str | None = None
    link_after: str | None = None
    elevation_required: bool = False
    command: str | None = None
    covered_bytes: int = 0
    """Bytes of this entry already carried by an earlier planned action inside it."""

    def to_dict(self) -> dict[str, object]:
        """JSON-ready mapping; every key is present (``null`` where unused)."""
        return {
            "id": self.id,
            "type": self.type,
            "kind": self.kind,
            "path": self.path,
            "bytes": self.bytes,
            "category": self.category,
            "tier": self.tier,
            "confidence": self.confidence,
            "rationale": self.rationale,
            "why": self.why,
            "side_effects": self.side_effects,
            "native_alt": self.native_alt,
            "dest": self.dest,
            "link_after": self.link_after,
            "elevation_required": self.elevation_required,
            "command": self.command,
            "covered_bytes": self.covered_bytes,
            "weak": self.weak,
        }


@dataclass(frozen=True)
class PlanSummary:
    """Per-outcome totals of one plan (every byte value is logical bytes)."""

    actions: int
    delete_bytes: int
    """Reclaimed by quarantines."""

    move_bytes: int
    """Reclaimed by moves (net of what a quarantine already took out of them)."""

    compress_bytes: int
    """Upper-bound savings of in-place compression (the entry's own size)."""

    review_bytes: int
    """Bytes at stake in ``REVIEW`` and ``NATIVE`` actions -- not reclaimed by SpaceSage."""

    native_bytes: int
    """The ``NATIVE`` part of :attr:`review_bytes`."""

    planned_bytes: int
    """``delete_bytes + move_bytes + compress_bytes``."""

    covered_bytes: int
    """Bytes discounted from actions because an earlier action already carries them."""

    dropped: int
    """Candidates dropped entirely: every byte they had is already planned elsewhere."""

    by_type: Mapping[str, Mapping[str, int]] = field(default_factory=dict)
    """Per type: ``{"actions": n, "bytes": n}``."""

    def to_dict(self) -> dict[str, object]:
        """JSON-ready mapping."""
        return {
            "actions": self.actions,
            "delete_bytes": self.delete_bytes,
            "move_bytes": self.move_bytes,
            "compress_bytes": self.compress_bytes,
            "review_bytes": self.review_bytes,
            "native_bytes": self.native_bytes,
            "planned_bytes": self.planned_bytes,
            "covered_bytes": self.covered_bytes,
            "dropped": self.dropped,
            "by_type": {key: dict(value) for key, value in self.by_type.items()},
        }


@dataclass(frozen=True)
class Plan:
    """A composed course of action (``plan.json`` v1)."""

    plan_id: str
    created: datetime
    source: Mapping[str, str | None]
    """``csv`` / ``machine`` / ``exported`` of the index the plan was built from."""

    targets: tuple[PlanTarget, ...]
    actions: tuple[PlanAction, ...]
    summary: PlanSummary
    provenance: Mapping[str, object] = field(default_factory=dict)
    """Engine inputs (index, thresholds, rules fingerprint) for traceability."""

    def to_dict(self) -> dict[str, object]:
        """JSON-ready plan (``spacesage.plan/v1``)."""
        return {
            "schema": SCHEMA,
            "plan_id": self.plan_id,
            "created": _iso(self.created),
            "source": dict(self.source),
            "targets": {target.name: target.to_dict() for target in self.targets},
            "provenance": dict(self.provenance),
            "summary": self.summary.to_dict(),
            "actions": [action.to_dict() for action in self.actions],
        }

    def type_actions(self, action_type: str) -> tuple[PlanAction, ...]:
        """Actions of one type, in plan order."""
        return tuple(action for action in self.actions if action.type == action_type)


def _iso(value: datetime) -> str:
    return value.astimezone(UTC).isoformat(timespec="seconds")


def compute_plan_id(source: Mapping[str, str | None], actions: Sequence[PlanAction]) -> str:
    """``sha256:`` id over the canonical action list and the source metadata.

    The canonical form is the JSON of ``{schema, source, actions}`` with sorted
    keys and no whitespace, so the id depends on *what the plan says* and not on
    the run (no timestamps, no mapping order).
    """
    payload = {
        "schema": SCHEMA,
        "source": {str(key): value for key, value in source.items()},
        "actions": [action.to_dict() for action in actions],
    }
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return "sha256:" + hashlib.sha256(canonical.encode("utf-8")).hexdigest()


# --------------------------------------------------------------------------- #
# Composition
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class _Spec:
    """One action before its id, target and coverage are known."""

    path: str
    is_dir: bool
    size: int
    category: str
    tier: str
    confidence: float
    rationale: str
    why: str
    native_alt: str | None
    kind: str
    """Candidate kind this entry came from (carried into the action)."""

    weak: bool = False
    command: str | None = None
    """Set when the rules want this entry handled by a vendor tool (``NATIVE`` advice)."""

    @property
    def relocatable(self) -> bool:
        """True when SpaceSage itself can move this entry."""
        return self.command is None


@dataclass(slots=True)
class _Unit:
    """One candidate, ready to be placed (a move group holds one spec per file)."""

    candidate: candidates.Candidate
    type: str
    """The action type that decides the unit's phase (``MOVE`` also issues ``NATIVE`` actions)."""

    specs: list[_Spec]
    bytes: int
    """The candidate's own bytes; the sort key discounts what is already planned."""

    @property
    def move_bytes(self) -> int:
        """Bytes the unit's target must have room for."""
        return sum(spec.size for spec in self.specs if spec.relocatable)


def _covered_bytes(key: str, regions: Sequence[tuple[str, int]]) -> int:
    """Bytes of the entry at ``key`` that earlier planned actions inside it carry."""
    return sum(size for region, size in regions if candidates.under(region, {key}))


class _Composer:
    """Turns a :class:`~spacesage.candidates.CandidateReport` into a :class:`Plan`."""

    def __init__(
        self,
        conn: sqlite3.Connection | None,
        report: candidates.CandidateReport,
        ruleset: rules.RuleSet,
        *,
        targets: Sequence[PlanTarget],
        links: bool,
        move_root: str,
        now: int,
        source: Mapping[str, str | None],
    ) -> None:
        self._conn = conn
        self._report = report
        self._ruleset = ruleset
        self._targets = list(targets)
        self._remaining = {target.name: target.budget for target in self._targets}
        self._links = links
        self._move_root = move_root
        self._now = now
        self._source = source
        self._actions: list[PlanAction] = []
        self._regions: list[tuple[str, int]] = []
        self._covered_total = 0
        self._dropped = 0

    # -- candidate -> unit -------------------------------------------------- #

    def _action_type(self, candidate: candidates.Candidate) -> str:
        """The action type a candidate becomes (never executable without advice)."""
        if candidate.kind in ("app", "stale", "dupes-weak"):
            # Footprints, cold entries and unverified duplicates are decisions,
            # not operations: they are review items by construction.
            return "REVIEW"
        if candidate.weak or candidate.action in ("KEEP", "REVIEW"):
            return "REVIEW"
        if candidate.tier == REPORT_TIER:
            # T3 is report-only (design section 2): even a launcher or an
            # OS-managed store stays a review item here.
            return "REVIEW"
        if candidate.action in ("DELETE_QUARANTINE", "MOVE", "COMPRESS_NTFS"):
            return candidate.action
        if candidate.action == "NATIVE":
            return "NATIVE" if candidate.native else "REVIEW"
        return "REVIEW"

    def _spec(self, candidate: candidates.Candidate) -> _Spec:
        """The trivial case: one candidate, one path, one action."""
        return _Spec(
            path=candidate.path,
            is_dir=candidate.is_dir,
            size=candidate.bytes,
            category=candidate.category,
            tier=candidate.tier,
            confidence=candidate.confidence,
            rationale=candidate.rationale,
            why=candidate.why,
            native_alt=candidate.native,
            kind=candidate.kind,
            weak=candidate.weak,
        )

    def _unit(self, candidate: candidates.Candidate) -> _Unit:
        """Map one candidate onto the action(s) it becomes."""
        chosen = self._action_type(candidate)
        if chosen in ("MOVE", "NATIVE"):
            if candidate.group == candidates.GROUP_FOLDER:
                # A file group: resolve the files the classifier approved and
                # let each keep its own advice (media files move; a launcher's
                # library follows its launcher, and both can live in one group).
                specs = self._group_specs(candidate)
                if not specs:
                    specs = [self._spec(candidate)]
                if all(not spec.relocatable for spec in specs):
                    chosen = "NATIVE"
            elif chosen == "NATIVE":
                spec = replace(self._spec(candidate), native_alt=None, command=candidate.native)
                specs = [spec]
            else:
                specs = [self._spec(candidate)]
        else:
            specs = [self._spec(candidate)]
        return _Unit(candidate=candidate, type=chosen, specs=specs, bytes=candidate.bytes)

    def _group_specs(self, candidate: candidates.Candidate) -> list[_Spec]:
        """Resolve a folder-level move group to the files the classifier approved.

        The ranked list caps ``members`` so the UI stays readable; the plan must
        not, or a folder of 200 videos would plan only the first ten.  The group
        is re-read from the index with the same predicate the candidates stage
        used (relocation advice, T1/T2, no hard-linked copies).
        """
        if self._conn is None:  # pragma: no cover - groups always come with an index
            return []
        specs: list[_Spec] = []
        for path, name, size, ext, mtime, hardlink in self._conn.execute(
            _GROUP_FILES_SQL, (candidate.path,)
        ):
            if hardlink:
                continue
            verdict = self._ruleset.classify(
                rules.EntryFacts(
                    path=str(path),
                    name=str(name),
                    is_dir=False,
                    size=int(size),
                    ext=str(ext) if ext is not None else None,
                    mtime=int(mtime) if mtime is not None else None,
                ),
                now=self._now,
            )
            if verdict.action not in self._report.move_actions:
                continue
            if verdict.tier not in candidates.MOVE_TIERS:
                continue
            if verdict.action == "NATIVE" and not verdict.native:
                continue
            specs.append(
                _Spec(
                    path=verdict.path,
                    is_dir=False,
                    size=verdict.size,
                    category=verdict.category,
                    tier=verdict.tier,
                    confidence=verdict.confidence,
                    rationale=verdict.rationale,
                    why=f"{candidates.action_label(verdict.action)}: {verdict.rationale}",
                    native_alt=verdict.native if verdict.action != "NATIVE" else None,
                    kind=candidate.kind,
                    command=verdict.native if verdict.action == "NATIVE" else None,
                )
            )
        specs.sort(key=lambda spec: (-spec.size, spec.path))
        return specs

    def _sort_key(self, unit: _Unit) -> tuple[int, int, int, str]:
        """Ordering key of a unit: phase, then tier (quarantines) or type, then gain.

        The gain is the unit's bytes *net of everything already planned inside
        it*, so the action list is monotone in the numbers it prints -- a folder
        that lost a nested quarantine to a previous phase ranks by what is left
        of it.
        """
        chosen = unit.type
        phase = next((index for index, types in enumerate(PHASES) if chosen in types), len(PHASES))
        rank = rules.TIERS.index(unit.candidate.tier) if phase == 0 else PHASES[phase].index(chosen)
        gain = max(0, unit.bytes - self._coverage(unit.candidate.path))
        return (phase, rank, -gain, unit.candidate.path)

    # -- placement ---------------------------------------------------------- #

    def _pick_target(self, source: str, need: int) -> tuple[PlanTarget | None, int]:
        """First target with room for ``need``; also reports the best remaining budget."""
        best = 0
        for target in self._targets:
            if same_volume(source, target.name):
                continue
            remaining = self._remaining[target.name]
            if remaining >= need:
                return target, remaining
            best = max(best, remaining)
        return None, best

    def _no_target_reason(self, source: str, need: int) -> str:
        """Why a move could not be planned (the reason the review item carries)."""
        if not self._targets:
            return "no target drive selected -- pass --to <drive> to plan the move"
        usable = [target for target in self._targets if not same_volume(source, target.name)]
        if not usable:
            return (
                f"the selected target is the same drive as the source ({_volume_of(source)}); "
                "a move there frees nothing"
            )
        _target, best = self._pick_target(source, need)
        return (
            f"no target drive has room under the reserve (needs {stats.format_bytes(need)}, "
            f"most free budget {stats.format_bytes(best)})"
        )

    def _append(self, action: PlanAction) -> None:
        """Keep one action and register the bytes it now carries."""
        self._actions.append(action)
        self._regions.append((candidates.path_key(action.path), action.bytes))

    def _coverage(self, path: str) -> int:
        """Bytes of ``path`` an earlier planned action *inside* it already carries.

        The ranked list may hold a folder and one of its claimed children at the
        same time (a media folder with a cache folder on the quarantine list);
        the child is planned first, so the folder's own bytes are reduced by it
        instead of being counted twice.
        """
        return _covered_bytes(candidates.path_key(path), self._regions)

    def _base(self, spec: _Spec, action_type: str, *, covered: int) -> dict[str, object]:
        """The fields every action of a spec shares."""
        return {
            "kind": spec.kind,
            "path": spec.path,
            "bytes": max(0, spec.size - covered),
            "category": spec.category,
            "tier": spec.tier,
            "confidence": spec.confidence,
            "rationale": spec.rationale,
            "why": spec.why,
            "weak": spec.weak,
            "native_alt": spec.native_alt,
            "covered_bytes": covered,
            "side_effects": _SIDE_EFFECTS.get(action_type),
        }

    def _emit_review(
        self,
        specs: Sequence[tuple[_Spec, int]],
        *,
        why: str | None = None,
    ) -> None:
        """Emit the review item(s) a non-executable candidate becomes."""
        for spec, covered in specs:
            fields = self._base(spec, "REVIEW", covered=covered)
            if why is not None:
                fields["why"] = why
            elif spec.kind == "app" and not spec.why.startswith(("Review:", "No action:")):
                # An app row carries the advice of the biggest entry inside it;
                # acting on the whole footprint is a human decision, so the
                # action says "Review" and the rationale keeps the advice.
                fields["why"] = f"Review: {spec.rationale}"
            self._append(PlanAction(id="", type="REVIEW", **fields))  # type: ignore[arg-type]

    def _emit_move(self, specs: Sequence[tuple[_Spec, int]], target: PlanTarget) -> None:
        """Emit the move actions of one unit on the chosen target."""
        self._remaining[target.name] -= sum(spec.size - covered for spec, covered in specs)
        for spec, covered in specs:
            link, elevation = link_policy(
                spec.is_dir,
                same_volume_=same_volume(spec.path, target.name),
                links=self._links,
            )
            dest = destination(spec.path, target=target.name, move_root=self._move_root)
            fields = self._base(spec, "MOVE", covered=covered)
            fields.update(
                {
                    "dest": dest,
                    "link_after": link,
                    "elevation_required": elevation,
                    "side_effects": (
                        f"Moved to {dest} and linked back with a {_link_label(link)}; the "
                        "original path keeps resolving."
                        if link != "NONE"
                        else f"Moved to {dest}; the original path disappears."
                    ),
                }
            )
            self._append(PlanAction(id="", type="MOVE", **fields))  # type: ignore[arg-type]

    def _emit_native(self, specs: Sequence[_Spec]) -> None:
        """Emit the vendor-tool advice (SpaceSage never runs these commands)."""
        for spec in specs:
            fields = self._base(spec, "NATIVE", covered=0)
            fields.update({"native_alt": None, "command": spec.command})
            self._append(PlanAction(id="", type="NATIVE", **fields))  # type: ignore[arg-type]

    def _emit_plain(self, specs: Sequence[_Spec], *, action_type: str) -> None:
        """Quarantines and compressions: one action per spec, coverage applied."""
        for spec in specs:
            covered = self._coverage(spec.path)
            self._covered_total += covered
            fields = self._base(spec, action_type, covered=covered)
            self._append(PlanAction(id="", type=action_type, **fields))  # type: ignore[arg-type]

    def _reviewable(self, specs: Sequence[_Spec]) -> list[tuple[_Spec, int]]:
        """The review specs that still have bytes left, with their coverage."""
        kept: list[tuple[_Spec, int]] = []
        for spec in specs:
            covered = self._coverage(spec.path)
            if covered >= spec.size:
                # Every byte is already planned by an action inside this entry
                # (a stale folder whose single file is being quarantined): the
                # candidate itself is dropped and counted, nothing to act on.
                continue
            self._covered_total += covered
            kept.append((spec, covered))
        return kept

    def place(self, unit: _Unit) -> None:
        """Dispatch one unit to its placement rule, honouring coverage."""
        if unit.type == "MOVE":
            advisory = [spec for spec in unit.specs if not spec.relocatable]
            moving = self._reviewable([spec for spec in unit.specs if spec.relocatable])
            if moving:
                need = sum(spec.size - covered for spec, covered in moving)
                target, _best = self._pick_target(unit.candidate.path, need)
                if target is None:
                    reason = self._no_target_reason(unit.candidate.path, need)
                    self._emit_review(moving, why=f"Review: {reason}")
                else:
                    self._emit_move(moving, target)
            elif not advisory:
                self._dropped += 1
            if advisory:
                self._emit_native(advisory)
            return
        if unit.type == "NATIVE":
            self._emit_native(unit.specs)
            return
        if unit.type == "REVIEW":
            reviewable = self._reviewable(unit.specs)
            if not reviewable:
                self._dropped += 1
                return
            self._emit_review(reviewable)
            return
        self._emit_plain(unit.specs, action_type=unit.type)

    # -- the whole plan ----------------------------------------------------- #

    def compose(self) -> Plan:
        """Compose, order, id and summarize the plan.

        Phases are placed one after another: within a phase the units are sorted
        by their remaining gain, and later phases see the bytes the earlier ones
        already claimed (that is where the covered-by-another-action discount
        comes from).
        """
        units = [self._unit(candidate) for candidate in self._report.candidates]
        for phase in PHASES:
            batch = [unit for unit in units if unit.type in phase]
            batch.sort(key=self._sort_key)
            for unit in batch:
                self.place(unit)
        actions = tuple(
            replace(action, id=f"a{index}") for index, action in enumerate(self._actions, start=1)
        )
        return Plan(
            plan_id=compute_plan_id(self._source, actions),
            created=datetime.now(tz=UTC),
            source=dict(self._source),
            targets=tuple(self._targets),
            actions=actions,
            summary=_summarize(actions, covered=self._covered_total, dropped=self._dropped),
            provenance=self._provenance(),
        )

    def _provenance(self) -> dict[str, object]:
        """The engine inputs behind this plan (traceability, outside the id)."""
        return {
            "db": self._report.db_path,
            "schema_version": self._report.schema_version,
            "as_of": _iso(datetime.fromtimestamp(self._report.as_of, tz=UTC)),
            "rules_fingerprint": self._report.rules.fingerprint,
            "min_size": self._report.min_size,
            "top": self._report.top,
            "links": self._links,
            "move_root": self._move_root,
        }


def _link_label(link: str) -> str:
    """Plain-language name of a link type."""
    return {
        "JUNCTION": "directory junction",
        "SYMLINK": "symbolic link (needs elevation or Developer Mode)",
        "HARDLINK": "hard link",
        "NONE": "no link",
    }.get(link, link)


def _summarize(actions: Sequence[PlanAction], *, covered: int, dropped: int) -> PlanSummary:
    """Roll the action list up into the plan's summary."""
    by_type: dict[str, dict[str, int]] = {
        action_type: {"actions": 0, "bytes": 0} for action_type in TYPES
    }
    totals: dict[str, int] = dict.fromkeys(TYPES, 0)
    for action in actions:
        block = by_type[action.type]
        block["actions"] += 1
        block["bytes"] += action.bytes
        totals[action.type] += action.bytes
    return PlanSummary(
        actions=len(actions),
        delete_bytes=totals["DELETE_QUARANTINE"],
        move_bytes=totals["MOVE"],
        compress_bytes=totals["COMPRESS_NTFS"],
        review_bytes=totals["REVIEW"] + totals["NATIVE"],
        native_bytes=totals["NATIVE"],
        planned_bytes=totals["DELETE_QUARANTINE"] + totals["MOVE"] + totals["COMPRESS_NTFS"],
        covered_bytes=covered,
        dropped=dropped,
        by_type=by_type,
    )


def _source_metadata(conn: sqlite3.Connection | None) -> dict[str, str | None]:
    """The index identity the plan is built from (``plan.source``)."""
    if conn is None:
        return {"csv": None, "machine": None, "exported": None}
    return {
        "csv": db.meta_get(conn, "source.csv"),
        "machine": db.meta_get(conn, "source.machine"),
        "exported": db.meta_get(conn, "source.exported"),
    }


def compose_plan(
    conn: sqlite3.Connection | None,
    report: candidates.CandidateReport,
    ruleset: rules.RuleSet,
    *,
    targets: Sequence[PlanTarget] = (),
    links: bool = True,
    move_root: str = MOVE_ROOT,
    source: Mapping[str, str | None] | None = None,
) -> Plan:
    """Compose a plan from an already-built candidate report.

    Splitting this out of :func:`build_plan` keeps the composition testable
    without re-ranking an index; ``conn`` is only needed to resolve folder-level
    move groups (their member list is capped in the ranked list).  ``source``
    overrides the index metadata the id is computed with (fixtures pin it).
    """
    if any(target.reserve_bytes < 0 for target in targets):
        raise PlannerError("reserve must not be negative")
    composer = _Composer(
        conn,
        report,
        ruleset,
        targets=targets,
        links=links,
        move_root=move_root,
        now=report.as_of,
        source=_source_metadata(conn) if source is None else dict(source),
    )
    return composer.compose()


def build_plan(
    conn: sqlite3.Connection,
    ruleset: rules.RuleSet,
    *,
    targets: Sequence[PlanTarget] = (),
    min_size: int = candidates.DEFAULT_MIN_SIZE,
    top: int = candidates.DEFAULT_TOP,
    now: float | None = None,
    stale_after_days: float = candidates.DEFAULT_STALE_DAYS,
    dupes_min_copies: int = candidates.DEFAULT_DUPES_MIN_COPIES,
    kinds: Sequence[str] = candidates.KINDS,
    links: bool = True,
    move_root: str = MOVE_ROOT,
    db_path: str | None = None,
) -> Plan:
    """Rank the opportunities and compose them into the course of action.

    The candidates are generated with the arguments the ``candidates`` stage
    uses (``min_size``; ``top`` bounds the listed rows per kind) and composed
    into the plan; nothing is written to the index.
    """
    moment = int(now if now is not None else time.time())
    report = candidates.candidate_report(
        conn,
        ruleset,
        kinds=kinds,
        min_size=min_size,
        top=top,
        now=moment,
        stale_after_days=stale_after_days,
        dupes_min_copies=dupes_min_copies,
        db_path=db_path,
    )
    return compose_plan(conn, report, ruleset, targets=targets, links=links, move_root=move_root)


# --------------------------------------------------------------------------- #
# Validation
# --------------------------------------------------------------------------- #


def _require(condition: bool, message: str) -> None:
    if not condition:
        raise PlannerError(message)


def _int_field(entry: Mapping[str, object], key: str, *, where: str, minimum: int = 0) -> int:
    value = entry.get(key)
    if isinstance(value, bool) or not isinstance(value, int):
        raise PlannerError(f"{where}: {key!r} must be an integer (got {value!r})")
    if value < minimum:
        raise PlannerError(f"{where}: {key!r} must be >= {minimum} (got {value})")
    return value


def _bool_field(entry: Mapping[str, object], key: str, *, where: str) -> bool:
    value = entry.get(key)
    if not isinstance(value, bool):
        raise PlannerError(f"{where}: {key!r} must be a boolean (got {value!r})")
    return value


def _str_field(entry: Mapping[str, object], key: str, *, where: str) -> str:
    value = entry.get(key)
    if not isinstance(value, str) or not value:
        raise PlannerError(f"{where}: {key!r} must be a non-empty string (got {value!r})")
    return value


def _optional_str(entry: Mapping[str, object], key: str, *, where: str) -> str | None:
    value = entry.get(key)
    if value is None:
        return None
    if not isinstance(value, str) or not value:
        raise PlannerError(f"{where}: {key!r} must be a non-empty string or null (got {value!r})")
    return value


def _validate_action(action: Mapping[str, object], *, where: str) -> None:
    """Check one parsed action against the v1 contract."""
    action_id = _str_field(action, "id", where=where)
    _require(
        _ACTION_ID_RE.match(action_id) is not None,
        f"{where}: action id {action_id!r} must look like 'a1'",
    )
    action_type = _str_field(action, "type", where=where)
    _require(action_type in TYPES, f"{where}: unknown action type {action_type!r}")
    kind = _str_field(action, "kind", where=where)
    _require(kind in candidates.KINDS, f"{where}: unknown candidate kind {kind!r}")
    path = _str_field(action, "path", where=where)
    _int_field(action, "bytes", where=where)
    _str_field(action, "category", where=where)
    tier = _str_field(action, "tier", where=where)
    _require(tier in rules.TIERS, f"{where}: unknown tier {tier!r}")
    confidence = action.get("confidence")
    if isinstance(confidence, bool) or not isinstance(confidence, (int, float)):
        raise PlannerError(f"{where}: 'confidence' must be a number (got {confidence!r})")
    _require(0.0 <= float(confidence) <= 1.0, f"{where}: 'confidence' must be between 0 and 1")
    _str_field(action, "rationale", where=where)
    _str_field(action, "why", where=where)
    weak = _bool_field(action, "weak", where=where)
    _int_field(action, "covered_bytes", where=where)
    elevation = _bool_field(action, "elevation_required", where=where)
    dest = _optional_str(action, "dest", where=where)
    link = _optional_str(action, "link_after", where=where)
    command = _optional_str(action, "command", where=where)
    _optional_str(action, "side_effects", where=where)
    _optional_str(action, "native_alt", where=where)

    if tier == REPORT_TIER:
        _require(
            action_type not in EXECUTABLE_TYPES,
            f"{where}: T3 is report-only; {action_type} may not act on {path}",
        )
        _require(command is None, f"{where}: T3 actions never carry a native command")
    if action_type in EXECUTABLE_TYPES:
        _require(not weak, f"{where}: an unverified (weak) candidate may not be executable")

    if action_type == "MOVE":
        _require(dest is not None, f"{where}: a MOVE action needs a 'dest'")
        _require(link is not None, f"{where}: a MOVE action needs 'link_after'")
        _require(
            link is None or link in LINKS,
            f"{where}: 'link_after' must be one of {', '.join(LINKS)}",
        )
        _require(command is None, f"{where}: a MOVE action never carries a command")
        _require(
            elevation == (link in ELEVATED_LINKS),
            f"{where}: 'elevation_required' must be true exactly for {', '.join(ELEVATED_LINKS)}",
        )
        _require(
            dest is None
            or (dest != path and not dest.lower().startswith((path.rstrip("\\/") + "\\").lower())),
            f"{where}: destination {dest!r} must not sit inside the source {path!r}",
        )
    elif action_type == "NATIVE":
        _require(command is not None, f"{where}: a NATIVE action needs a 'command'")
        _require(dest is None and link is None, f"{where}: a NATIVE action has no destination")
    else:
        _require(
            dest is None and link is None and command is None,
            f"{where}: {action_type} actions carry no destination, link or command",
        )
        _require(
            not elevation,
            f"{where}: only a MOVE action can require elevation",
        )


def _validate_summary(
    summary: Mapping[str, object], actions: Sequence[Mapping[str, object]]
) -> None:
    """The summary must be exactly what the actions imply (``dropped`` excepted)."""
    expected = _recompute_summary(actions)
    for key, value in expected.items():
        _require(
            summary.get(key) == value,
            f"summary.{key} says {summary.get(key)!r} but the actions say {value!r}",
        )
    _int_field(summary, "dropped", where="summary")


def validate_plan(data: object) -> None:
    """Validate a parsed ``plan.json`` against the v1 contract.

    Raises :class:`PlannerError` on the first violation: wrong schema, ids that
    do not recompute from the actions, missing or mistyped fields, executable
    actions on T3 paths, weak candidates in executable slots, moves without a
    destination or with a destination outside every declared target, and a
    summary that does not match the actions.
    """
    if not isinstance(data, Mapping):
        raise PlannerError(f"plan must be an object, got {type(data).__name__}")
    _require(data.get("schema") == SCHEMA, f"plan schema must be {SCHEMA!r}")
    _str_field(data, "plan_id", where="plan")
    plan_id = _str_field(data, "plan_id", where="plan")
    _require(_PLAN_ID_RE.match(plan_id) is not None, f"plan_id {plan_id!r} must be sha256:<hex>")
    created = _str_field(data, "created", where="plan")
    try:
        datetime.fromisoformat(created)
    except ValueError:
        raise PlannerError(f"created {created!r} is not an ISO timestamp") from None

    source = data.get("source")
    if not isinstance(source, Mapping):
        raise PlannerError("source must be an object")
    for key in ("csv", "machine", "exported"):
        _optional_str(source, key, where="source")

    targets = data.get("targets")
    if not isinstance(targets, Mapping):
        raise PlannerError("targets must be an object")
    roots: list[str] = []
    for name, raw in targets.items():
        if not isinstance(name, str) or not isinstance(raw, Mapping):
            raise PlannerError(f"targets[{name!r}] must be an object")
        _int_field(raw, "free_bytes", where=f"targets[{name!r}]")
        _int_field(raw, "reserve_bytes", where=f"targets[{name!r}]")
        roots.append(target_root(name))

    raw_actions = data.get("actions")
    if not isinstance(raw_actions, list):
        raise PlannerError("actions must be a list")
    actions: list[Mapping[str, object]] = []
    for index, raw in enumerate(raw_actions, start=1):
        if not isinstance(raw, Mapping):
            raise PlannerError(f"actions[{index}] must be an object")
        _validate_action(raw, where=f"actions[{index}]")
        _require(
            raw.get("id") == f"a{index}",
            f"actions[{index}]: ids must be sequential from a1 (got {raw.get('id')!r})",
        )
        dest = raw.get("dest")
        if isinstance(dest, str) and raw.get("type") == "MOVE":
            lowered = dest.lower()
            inside = any(
                lowered.startswith(root.rstrip("\\/").lower() + sep)
                for root in roots
                for sep in ("\\", "/")
            )
            _require(inside, f"actions[{index}]: dest {dest!r} is not under any declared target")
        actions.append(raw)

    summary = data.get("summary")
    if not isinstance(summary, Mapping):
        raise PlannerError("summary must be an object")
    _validate_summary(summary, actions)

    if plan_id != _recompute_plan_id(source, actions):
        raise PlannerError("plan_id does not recompute from the action list and source metadata")


def _recompute_summary(actions: Sequence[Mapping[str, object]]) -> dict[str, object]:
    """The summary a list of parsed actions implies."""
    totals: dict[str, int] = dict.fromkeys(TYPES, 0)
    counts: dict[str, int] = dict.fromkeys(TYPES, 0)
    covered = 0
    for action in actions:
        action_type = str(action["type"])
        size = action["bytes"]
        covered += int(action["covered_bytes"])  # type: ignore[call-overload]
        totals[action_type] += int(size)  # type: ignore[call-overload]
        counts[action_type] += 1
    return {
        "actions": len(actions),
        "delete_bytes": totals["DELETE_QUARANTINE"],
        "move_bytes": totals["MOVE"],
        "compress_bytes": totals["COMPRESS_NTFS"],
        "review_bytes": totals["REVIEW"] + totals["NATIVE"],
        "native_bytes": totals["NATIVE"],
        "planned_bytes": totals["DELETE_QUARANTINE"] + totals["MOVE"] + totals["COMPRESS_NTFS"],
        "covered_bytes": covered,
        "by_type": {
            action_type: {"actions": counts[action_type], "bytes": totals[action_type]}
            for action_type in TYPES
        },
    }


def _recompute_plan_id(
    source: Mapping[object, object], actions: Sequence[Mapping[str, object]]
) -> str:
    """The plan id a parsed document implies (same canonical form as the engine)."""
    payload = {
        "schema": SCHEMA,
        "source": {str(key): value for key, value in source.items()},
        "actions": [dict(action) for action in actions],
    }
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return "sha256:" + hashlib.sha256(canonical.encode("utf-8")).hexdigest()


# --------------------------------------------------------------------------- #
# Rendering
# --------------------------------------------------------------------------- #


def render_json(plan: Plan) -> str:
    """Render the plan as pretty-printed JSON (``plan.json``)."""
    return json.dumps(plan.to_dict(), indent=2) + "\n"


def _summary_lines(plan: Plan) -> list[str]:
    """The bullet list under the Markdown summary heading."""
    summary = plan.summary
    lines = [
        "",
        "## Summary",
        "",
        f"- Delete (quarantine): "
        f"{_plural(summary.by_type['DELETE_QUARANTINE']['actions'], 'action', 'actions')}, "
        f"{stats.format_bytes(summary.delete_bytes)} reclaimed",
        f"- Move: {_plural(summary.by_type['MOVE']['actions'], 'action', 'actions')}, "
        f"{stats.format_bytes(summary.move_bytes)} reclaimed",
    ]
    if summary.by_type["COMPRESS_NTFS"]["actions"]:
        lines.append(
            f"- Compress (NTFS): "
            f"{_plural(summary.by_type['COMPRESS_NTFS']['actions'], 'action', 'actions')}, "
            f"up to {stats.format_bytes(summary.compress_bytes)} saved"
        )
    if summary.by_type["REVIEW"]["actions"]:
        lines.append(
            f"- Review: {_plural(summary.by_type['REVIEW']['actions'], 'action', 'actions')}, "
            f"{stats.format_bytes(summary.by_type['REVIEW']['bytes'])} at stake"
        )
    if summary.by_type["NATIVE"]["actions"]:
        lines.append(
            f"- Native tool: "
            f"{_plural(summary.by_type['NATIVE']['actions'], 'action', 'actions')}, "
            f"{stats.format_bytes(summary.native_bytes)} at stake"
        )
    lines.append(
        f"- **Planned: {stats.format_bytes(summary.planned_bytes)} reclaimed** by "
        f"{_plural(summary.actions, 'action', 'actions')}"
    )
    if summary.covered_bytes:
        lines.append(
            f"- {stats.format_bytes(summary.covered_bytes)} carried by more than one candidate "
            "were counted once."
        )
    if summary.dropped:
        lines.append(
            f"- {_plural(summary.dropped, 'candidate was', 'candidates were')} dropped: "
            "everything they covered is already planned by another action."
        )
    return lines


def _plural(count: int, singular: str, plural: str) -> str:
    """``1 candidate was`` / ``3 candidates were``."""
    return f"{count} {singular if count == 1 else plural}"


def render_markdown(plan: Plan) -> str:
    """Render the plan as a Markdown summary (reports and chat)."""
    source = plan.source
    where = source.get("csv") or "(unknown export)"
    if source.get("machine"):
        where += f" (machine {source['machine']}"
        if source.get("exported"):
            where += f", exported {source['exported']}"
        where += ")"
    summary = plan.summary
    lines = [
        f"# SpaceSage plan -- {stats.format_bytes(summary.planned_bytes)} to reclaim, "
        f"{summary.actions} actions",
        "",
        f"- **Plan id:** `{plan.plan_id}`",
        f"- **Created:** {_iso(plan.created)}",
        f"- **Source:** {where}",
    ]
    for target in plan.targets:
        lines.append(
            f"- **Target {target.name}** -- {stats.format_bytes(target.free_bytes)} free, "
            f"{stats.format_bytes(target.reserve_bytes)} reserve "
            f"({stats.format_bytes(target.budget)} usable)"
        )
    if not plan.targets:
        lines.append("- **Targets:** none selected -- every move candidate is a review item")
    lines.extend(_summary_lines(plan))
    for action_type in TYPES:
        block = plan.type_actions(action_type)
        if not block:
            continue
        total = sum(action.bytes for action in block)
        lines.append("")
        lines.append(
            f"## {HEADINGS[action_type]} -- "
            f"{_plural(len(block), 'action', 'actions')}, {stats.format_bytes(total)}"
        )
        lines.append("")
        for action in block:
            lines.append(f"- `{action.id}` {_action_line(action)}")
            detail = action.why
            if action.native_alt:
                detail += f"  Native alternative: {action.native_alt}"
            if action.covered_bytes:
                detail += (
                    f"  ({stats.format_bytes(action.covered_bytes)} of it is already planned "
                    "by another action)"
                )
            lines.append(f"  {detail}")
    return "\n".join(lines) + "\n"


def _action_line(action: PlanAction) -> str:
    """One action as a compact Markdown line (path, destination, size, tier)."""
    bits = [f"**{action.path}**"]
    if action.dest:
        suffix = ", needs elevation" if action.elevation_required else ""
        bits.append(f"-> `{action.dest}` ({(action.link_after or 'NONE').lower()}{suffix})")
    elif action.command:
        bits.append(f"-> `{action.command}`")
    bits.append(
        f"-- {stats.format_bytes(action.bytes)} ({action.tier}, confidence {action.confidence:.2f})"
    )
    return " ".join(bits)


__all__ = [
    "ADVISORY_TYPES",
    "DEFAULT_RESERVE",
    "ELEVATED_LINKS",
    "EXECUTABLE_TYPES",
    "HEADINGS",
    "LINKS",
    "MOVE_ROOT",
    "PHASES",
    "REPORT_TIER",
    "SCHEMA",
    "TYPES",
    "Plan",
    "PlanAction",
    "PlanSummary",
    "PlanTarget",
    "PlannerError",
    "build_plan",
    "compose_plan",
    "compute_plan_id",
    "destination",
    "link_label",
    "link_policy",
    "measure_free_space",
    "relative_parts",
    "render_json",
    "render_markdown",
    "same_volume",
    "side_effects_of",
    "target_from_spec",
    "target_root",
    "validate_plan",
    "volume_of",
]
