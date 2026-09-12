"""From selected opportunities to an approved, executable, reversible plan (S9).

The screens never call the planner and the executor directly: this module is the
seam between a *selection* in the ranked list and the three things the design
promises (``docs/design.md`` §9, screen 3):

1. **A draft** -- :func:`draft_plan` composes a ``spacesage.plan/v1`` document
   from exactly the rows the user checked (folders cover their contents, as in
   the list), and reports what the composer decided per item: what each action
   resolves to, which selected rows produced nothing, and the warnings the plan
   itself does not carry (a move demoted for budget, a selection that nests, no
   target drive at all).
2. **An approval** -- :func:`write_draft` persists ``plan.json`` and a
   ``plan_id``-bound ``approved.json`` in a per-plan workspace under the app's
   data directory; :func:`save_approval` rewrites the manifest as the user
   approves or rejects item by item.  The manifest is the executor's gate: a
   plan whose id changed can never be run against a stale approval.
3. **A run** -- :func:`preview` is the dry run (resolve every approved action,
   touch nothing), :func:`execute` performs it with a per-item callback, and
   :func:`journal_history` / :func:`revert` are the undo view's data and action:
   the journal as a list of per-item statuses, and a reversal of all or of a
   chosen subset, verifying every payload on the way back.

Nothing here knows about Qt, and nothing here re-implements an engine rule: the
plan comes from :mod:`spacesage.planner`, the operations from
:mod:`spacesage.executor`.
"""

from __future__ import annotations

import json
import os
import sqlite3
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, replace
from pathlib import Path

from spacesage import candidates, db, executor, opportunities, planner, rules, stats
from spacesage.executor import backend

PLANS_DIRNAME = "plans"
"""Folder under the app's data directory holding one workspace per plan."""

PLAN_FILE = "plan.json"
APPROVAL_FILE = "approved.json"
JOURNAL_FILE = "spacesage.journal.jsonl"

SEVERITIES: tuple[str, ...] = ("blocker", "budget", "conflict", "info")
"""Warning kinds, most severe first (the plan screen styles them by this)."""

SEVERITY_LABELS: Mapping[str, str] = {
    "blocker": "Cannot run",
    "budget": "No room",
    "conflict": "Conflict",
    "info": "Note",
}

ITEM_STATUSES: tuple[str, ...] = ("ready", "refused", "advice")
"""What the *draft* already knows about an item: runnable, refused, or advice."""

JOURNAL_STATUSES: tuple[str, ...] = ("pending", "reversed", "blocked", "failed", "interrupted")
"""Per-item status the undo view shows for a journaled operation."""


class PlanningError(RuntimeError):
    """Raised when a plan cannot be drafted or a workspace cannot be used."""


# --------------------------------------------------------------------------- #
# The request
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class PlanRequest:
    """What a plan is built from: the checked rows and the run's parameters."""

    paths: tuple[str, ...]
    """The selected opportunities, as the export spells them."""

    targets: tuple[planner.PlanTarget, ...] = ()
    """Destination drives for moves, with their free space and reserve."""

    links: bool = True
    """Plan the link that keeps a moved path resolving (``--no-links`` turns it off)."""

    min_size: int = opportunities.DEFAULT_MIN_SIZE
    list_top: int = opportunities.DEFAULT_LIST_TOP
    kinds: tuple[str, ...] = candidates.KINDS
    stale_after_days: float = candidates.DEFAULT_STALE_DAYS
    dupes_min_copies: int = candidates.DEFAULT_DUPES_MIN_COPIES
    now: float | None = None
    db_path: str | None = None

    @classmethod
    def from_listing(
        cls,
        listing: opportunities.OpportunityList,
        paths: Sequence[str],
        *,
        targets: Sequence[planner.PlanTarget] = (),
        links: bool = True,
    ) -> PlanRequest:
        """The candidates the screen showed, restricted to ``paths``.

        Re-using the list's own thresholds (and its reference moment) is what
        makes "the plan for these rows" reproducible: the selected candidates
        are exactly the ones the ranked list drew from, so a row can never
        disappear between the list and the plan.
        """
        return cls(
            paths=tuple(paths),
            targets=tuple(targets),
            links=links,
            min_size=listing.min_size,
            list_top=listing.list_top,
            kinds=tuple(listing.kinds),
            now=float(listing.as_of),
            db_path=listing.db_path,
        )


# --------------------------------------------------------------------------- #
# The draft
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class PlanWarning:
    """One warning the plan screen shows distinctly (design §9, screen 3)."""

    severity: str
    """One of :data:`SEVERITIES`."""

    message: str
    paths: tuple[str, ...] = ()

    @property
    def label(self) -> str:
        """The badge text of this severity."""
        return SEVERITY_LABELS.get(self.severity, self.severity)

    def to_dict(self) -> dict[str, object]:
        """JSON-ready mapping."""
        return {
            "severity": self.severity,
            "label": self.label,
            "message": self.message,
            "paths": list(self.paths),
        }


@dataclass(frozen=True)
class PlanItem:
    """One row of the plan screen: one action, where it came from, what it does."""

    action: planner.PlanAction
    origin: str | None
    """The selected path this action came from (``None`` for an entry below one)."""

    executable: bool
    """False for ``REVIEW``/``NATIVE``: advice the human acts on, not an operation."""

    detail: str
    """What the action resolves to -- the destination, the link, or the advice."""

    status: str = "ready"
    """One of :data:`ITEM_STATUSES` as the draft sees it (before re-validation)."""

    reason: str = ""
    """The refusal or advice the draft already knows about."""

    def to_dict(self) -> dict[str, object]:
        """JSON-ready mapping (the plan screen's rows)."""
        return {
            "action": self.action.to_dict(),
            "origin": self.origin,
            "executable": self.executable,
            "detail": self.detail,
            "status": self.status,
            "reason": self.reason,
        }


@dataclass(frozen=True)
class PlanDraft:
    """A plan composed from a selection, with everything the screen needs."""

    plan: planner.Plan
    request: PlanRequest
    items: tuple[PlanItem, ...]
    warnings: tuple[PlanWarning, ...]
    unplanned: tuple[str, ...]
    """Selected paths that produced no action at all (with the warning saying so)."""

    preview: executor.ApplyReport | None = None
    """The dry run of the executable subset (``None`` only for an empty plan)."""

    # -- queries ---------------------------------------------------------- #

    @property
    def plan_id(self) -> str:
        """The plan's own id (what an approval binds to)."""
        return self.plan.plan_id

    def item(self, action_id: str) -> PlanItem | None:
        """The item of one action id."""
        for item in self.items:
            if item.action.id == action_id:
                return item
        return None

    def items_for(self, path: str) -> tuple[PlanItem, ...]:
        """Every item whose action is the path or lives below it."""
        key = opportunities.path_key(path)
        return tuple(
            item
            for item in self.items
            if opportunities.path_key(item.action.path) == key
            or candidates.under(opportunities.path_key(item.action.path), {key})
        )

    def executable_ids(self) -> tuple[str, ...]:
        """The ids an approval can name (advice items are never approved)."""
        return tuple(item.action.id for item in self.items if item.executable)

    def advisory_ids(self) -> tuple[str, ...]:
        """The ids of the review/native items (listed, never executed)."""
        return tuple(item.action.id for item in self.items if not item.executable)

    def refused_ids(self) -> tuple[str, ...]:
        """The ids the draft's dry run already refuses."""
        return tuple(item.action.id for item in self.items if item.status == "refused")

    def warnings_of(self, *severities: str) -> tuple[PlanWarning, ...]:
        """The warnings of the named severities (all of them with no argument)."""
        if not severities:
            return self.warnings
        return tuple(item for item in self.warnings if item.severity in severities)

    def blocks_execution(self) -> bool:
        """True when the draft found a blocker (nothing executable, or refusals)."""
        return any(warning.severity == "blocker" for warning in self.warnings)

    def summary_line(self) -> str:
        """One line about what the plan would do (the screen's subtitle)."""
        executable = len(self.executable_ids())
        advice = len(self.advisory_ids())
        parts = [
            f"{executable} executable action{'' if executable == 1 else 's'}",
            f"{stats.format_bytes(self.plan.summary.planned_bytes)} reclaimed",
        ]
        if advice:
            parts.append(f"{advice} advice item{'' if advice == 1 else 's'}")
        return " · ".join(parts)

    def to_dict(self) -> dict[str, object]:
        """JSON-ready mapping (the draft, not the plan document itself)."""
        return {
            "plan": self.plan.to_dict(),
            "items": [item.to_dict() for item in self.items],
            "warnings": [warning.to_dict() for warning in self.warnings],
            "unplanned": list(self.unplanned),
            "selection": list(self.request.paths),
            "preview": self.preview.to_dict() if self.preview is not None else None,
        }


# --------------------------------------------------------------------------- #
# Drafting
# --------------------------------------------------------------------------- #


def _restricted(
    report: candidates.CandidateReport, kept: Sequence[candidates.Candidate]
) -> candidates.CandidateReport:
    """The same report, holding only the candidates of ``kept`` (kinds in order)."""
    wanted = {id(candidate) for candidate in kept}
    return replace(
        report,
        kinds=tuple(
            replace(block, candidates=tuple(c for c in block.candidates if id(c) in wanted))
            for block in report.kinds
        ),
    )


def _selected_candidates(
    report: candidates.CandidateReport, keys: Sequence[str]
) -> tuple[candidates.Candidate, ...]:
    """The report's candidates a selection covers (a checked folder covers its rows)."""
    chosen: list[candidates.Candidate] = []
    for candidate in report.candidates:
        candidate_key = opportunities.path_key(candidate.path)
        if any(candidate_key == key or candidates.under(candidate_key, {key}) for key in keys):
            chosen.append(candidate)
    return tuple(chosen)


def _origin_of(path: str, selection: Sequence[tuple[str, str]]) -> str | None:
    """The selected path an action belongs to (narrowest match wins)."""
    key = opportunities.path_key(path)
    best: tuple[int, str] | None = None
    for original, selected_key in selection:
        if key == selected_key or candidates.under(key, {selected_key}):
            depth = len(selected_key)
            if best is None or depth > best[0]:
                best = (depth, original)
    return best[1] if best is not None else None


def _detail_of(action: planner.PlanAction) -> str:
    """What one action resolves to, in the plan screen's words."""
    if action.type == "DELETE_QUARANTINE":
        return "moves to the quarantine store (reversible)"
    if action.type == "MOVE":
        link = planner.link_label(action.link_after) if action.link_after else "no link"
        destination = action.dest or "?"
        extra = " · needs elevation" if action.elevation_required else ""
        return f"moves to {destination} · still reachable as a {link}{extra}"
    if action.type == "COMPRESS_NTFS":
        return "compressed in place (NTFS)"
    if action.type == "NATIVE":
        return action.command or "run the vendor's own tool"
    return action.why or action.rationale


_MOVE_REASONS: tuple[tuple[str, str, str], ...] = (
    (
        "no target drive has room",
        "budget",
        "No target drive has room under the reserve, so this move stays a review item for now.",
    ),
    (
        "same drive as the source",
        "conflict",
        "The target is the source's own drive, so a move there frees nothing.",
    ),
    (
        "no target drive selected",
        "conflict",
        "No target drive is set: moves stay review items until one is picked on Import.",
    ),
)
"""Composer reason -> ``(severity, screen wording)`` for a demoted move.

The composer's sentence is the CLI's (``pass --to <drive>``); the screen says
the same thing in the product's words, keeping the engine's own numbers.  The
test suite pins this mapping against real composer output, so a wording change
can never silently downgrade a budget problem to a note.
"""


def _move_warning_for(reason: str) -> tuple[str, str]:
    """``(severity, message)`` for one demoted move, from the composer's reason."""
    for needle, severity, wording in _MOVE_REASONS:
        if needle not in reason:
            continue
        numbers = reason[reason.find("(") :].strip("() ") if "(" in reason else ""
        message = f"{wording} ({numbers})" if numbers and severity == "budget" else wording
        return severity, message
    return "info", reason


def _move_warnings(
    draft_items: Sequence[PlanItem],
    chosen: Sequence[candidates.Candidate],
) -> list[PlanWarning]:
    """Moves the composer demoted, told apart by *why* they were demoted.

    The composer writes the reason into the review item it emits
    (:meth:`spacesage.planner._Composer._no_target_reason`); this maps that
    wording onto the two kinds of trouble a reader cares about -- the target is
    on the source's own drive (a conflict) or has no room under the reserve (a
    budget problem).
    """
    out: list[PlanWarning] = []
    for candidate in chosen:
        if (
            candidate.kind in ("app", "stale", "dupes-weak")
            or candidate.tier == planner.REPORT_TIER
        ):
            continue
        if candidate.weak or candidate.action != "MOVE":
            continue
        key = opportunities.path_key(candidate.path)
        plans_a_move = any(
            item.action.type == "MOVE" and opportunities.path_key(item.action.path) == key
            for item in draft_items
        )
        if plans_a_move:
            continue
        for item in draft_items:
            if opportunities.path_key(item.action.path) != key:
                continue
            reason = item.action.why
            if reason.startswith("Review: "):
                reason = reason[len("Review: ") :]
            severity, message = _move_warning_for(reason)
            out.append(PlanWarning(severity=severity, message=message, paths=(candidate.path,)))
            break
    return out


def _nested_warnings(selection: Sequence[str]) -> list[PlanWarning]:
    """Selected rows that live inside another selected row (counted once)."""
    out: list[PlanWarning] = []
    keys = [(path, opportunities.path_key(path)) for path in selection]
    for path, key in keys:
        parents = [
            other
            for other, other_key in keys
            if other_key != key and candidates.under(key, {other_key})
        ]
        if parents:
            out.append(
                PlanWarning(
                    severity="conflict",
                    message=(
                        f"{path} sits inside {parents[0]} -- its bytes are counted once, "
                        "in the outer row."
                    ),
                    paths=(path, parents[0]),
                )
            )
    return out


def warnings_from_preview(
    items: Sequence[PlanItem], report: executor.ApplyReport
) -> list[PlanWarning]:
    """The blocker warnings a dry-run report contributes (one per refusal).

    A refusal is the executor's second look finding that an action must never
    run: a report-only tier, a protected path, a destination outside the plan's
    targets.  The plan screen shows these *before* anything is approved, which
    is the point of previewing at draft time.
    """
    out: list[PlanWarning] = []
    for op in report.ops:
        if op.outcome != "refused":
            continue
        item = next((entry for entry in items if entry.action.id == op.action_id), None)
        path = item.action.path if item is not None else op.path
        out.append(PlanWarning(severity="blocker", message=f"{path}: {op.reason}", paths=(path,)))
    return out


def draft_plan(
    conn: sqlite3.Connection,
    ruleset: rules.RuleSet,
    request: PlanRequest,
    *,
    quarantine_root: str | os.PathLike[str] | None = None,
) -> PlanDraft:
    """Compose the plan of one selection, and say what it will really do.

    The candidates are re-generated exactly as the ranked list generated them
    (same thresholds, same reference moment) and restricted to the selection, so
    the plan holds the checked rows and nothing else.  A dry run of the
    executable subset then resolves every action (destinations, links, refusals)
    without touching the filesystem -- that resolution is what the plan screen
    shows before anything is approved.
    """
    if not request.paths:
        raise PlanningError("no items selected: a plan needs at least one opportunity")
    selection = tuple((path, opportunities.path_key(path)) for path in request.paths)
    report = candidates.candidate_report(
        conn,
        ruleset,
        kinds=request.kinds,
        min_size=request.min_size,
        top=request.list_top,
        now=request.now,
        stale_after_days=request.stale_after_days,
        dupes_min_copies=request.dupes_min_copies,
        db_path=request.db_path,
    )
    chosen = _selected_candidates(report, [key for _path, key in selection])
    plan = planner.compose_plan(
        conn,
        _restricted(report, chosen),
        ruleset,
        targets=request.targets,
        links=request.links,
    )

    executable = [action for action in plan.actions if action.type not in planner.ADVISORY_TYPES]
    preview: executor.ApplyReport | None = None
    if executable:
        preview = preview_plan(
            plan, [action.id for action in executable], quarantine_root=quarantine_root
        )
    refused = {
        op.action_id: op.reason
        for op in (preview.ops if preview is not None else ())
        if op.outcome == "refused"
    }

    items: list[PlanItem] = []
    for action in plan.actions:
        is_executable = action.type not in planner.ADVISORY_TYPES
        status = "advice" if not is_executable else ("refused" if action.id in refused else "ready")
        items.append(
            PlanItem(
                action=action,
                origin=_origin_of(action.path, selection),
                executable=is_executable,
                detail=_detail_of(action),
                status=status,
                reason="" if is_executable else action.why,
            )
        )

    planned_keys = {opportunities.path_key(item.action.path): None for item in items}
    unplanned = tuple(
        path
        for path, key in selection
        if key not in planned_keys
        and not any(candidates.under(action_key, {key}) for action_key in planned_keys)
    )

    warnings: list[PlanWarning] = []
    warnings.extend(_move_warnings(items, chosen))
    warnings.extend(_nested_warnings(request.paths))
    if preview is not None:
        warnings.extend(warnings_from_preview(items, preview))
    if unplanned:
        warnings.append(
            PlanWarning(
                severity="info",
                message=(
                    "Selected entries the plan holds no action for: " + ", ".join(unplanned) + "."
                ),
                paths=unplanned,
            )
        )
    if not items:
        warnings.append(
            PlanWarning(
                severity="blocker",
                message=("Nothing to plan: the rules propose no action for the selected entries."),
                paths=request.paths,
            )
        )
    elif not any(item.executable for item in items):
        warnings.append(
            PlanWarning(
                severity="blocker",
                message=(
                    "The selection only holds advice: there is nothing SpaceSage can execute. "
                    "The plan is still worth exporting as a record."
                ),
                paths=request.paths,
            )
        )

    ordered = tuple(sorted(warnings, key=lambda warning: SEVERITIES.index(warning.severity)))
    return PlanDraft(
        plan=plan,
        request=request,
        items=tuple(items),
        warnings=ordered,
        unplanned=unplanned,
        preview=preview,
    )


# --------------------------------------------------------------------------- #
# Workspaces: plan.json + approved.json behind the scenes
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class PlanWorkspace:
    """One plan's files on disk: the document, the approval, the journal."""

    plan_id: str
    directory: Path

    @property
    def plan_path(self) -> Path:
        """``plan.json`` -- the contract the executor reads."""
        return self.directory / PLAN_FILE

    @property
    def manifest_path(self) -> Path:
        """``approved.json`` -- bound to :attr:`plan_id`."""
        return self.directory / APPROVAL_FILE

    @property
    def journal_path(self) -> Path:
        """The journal every run of this plan appends to."""
        return self.directory / JOURNAL_FILE

    def has_journal(self) -> bool:
        """True when something was executed (or previewed) with a journal."""
        return self.journal_path.is_file()

    def to_dict(self) -> dict[str, object]:
        """JSON-ready mapping."""
        return {
            "plan_id": self.plan_id,
            "directory": str(self.directory),
            "plan": str(self.plan_path),
            "manifest": str(self.manifest_path),
            "journal": str(self.journal_path),
        }


def workspace_directory(root: str | os.PathLike[str], plan_id: str) -> Path:
    """``<root>/plans/<token>`` -- one directory per plan (the token is its share)."""
    return Path(root) / PLANS_DIRNAME / backend.plan_token(plan_id)


def write_draft(
    draft: PlanDraft,
    root: str | os.PathLike[str],
    *,
    approved: Sequence[str] | None = None,
    rejected: Sequence[str] | None = None,
    note: str | None = None,
) -> PlanWorkspace:
    """Persist ``plan.json`` and a first ``approved.json`` for a draft.

    Approving everything runnable is the default: the plan screen then takes
    items *out* of the approval before executing, which is the direction that
    fails safe (an action nobody looked at is not silently executed).  The
    advice items (``REVIEW``/``NATIVE``) are recorded as rejected -- they are
    never run, and saying so is more honest than leaving them unnamed.
    """
    workspace = PlanWorkspace(
        plan_id=draft.plan_id, directory=workspace_directory(root, draft.plan_id)
    )
    workspace.directory.mkdir(parents=True, exist_ok=True)
    workspace.plan_path.write_text(
        json.dumps(draft.plan.to_dict(), indent=2) + "\n", encoding="utf-8"
    )
    ids = tuple(approved) if approved is not None else draft.executable_ids()
    dropped = tuple(rejected) if rejected is not None else draft.advisory_ids()
    save_approval(workspace, draft, ids, rejected=dropped, note=note)
    return workspace


def save_approval(
    workspace: PlanWorkspace,
    draft: PlanDraft,
    approved: Sequence[str],
    *,
    rejected: Sequence[str] = (),
    note: str | None = None,
) -> Path:
    """Rewrite ``approved.json`` for the plan the workspace holds.

    The manifest is built from the plan's own id, so an approval can only ever
    name the actions of this exact plan document (design §7, §8).
    """
    if workspace.plan_id != draft.plan_id:
        raise PlanningError(
            f"the workspace holds plan {workspace.plan_id} but the draft is {draft.plan_id}"
        )
    known = {item.action.id for item in draft.items}
    unknown = sorted(set(approved) - known)
    if unknown:
        raise PlanningError(
            f"the approval names action id(s) this plan does not have: {', '.join(unknown)}"
        )
    advisory = sorted(set(approved) & set(draft.advisory_ids()))
    if advisory:
        raise PlanningError(
            f"action id(s) {', '.join(advisory)} are advice items: nothing can execute them"
        )
    manifest = executor.make_manifest(
        draft.plan_id, list(approved), rejected=list(rejected), note=note
    )
    return executor.write_manifest(workspace.manifest_path, manifest)


def load_approval(workspace: PlanWorkspace) -> executor.Manifest | None:
    """The persisted approval of a workspace (``None`` when there is none yet)."""
    if not workspace.manifest_path.is_file():
        return None
    return executor.load_manifest(workspace.manifest_path)


def plan_directories(root: str | os.PathLike[str]) -> tuple[Path, ...]:
    """Every workspace directory under ``root``, newest first (broken ones last)."""
    base = Path(root) / PLANS_DIRNAME
    if not base.is_dir():
        return ()
    found = [entry for entry in base.iterdir() if entry.is_dir()]
    return tuple(sorted(found, key=lambda path: (-_mtime(path), path.name)))


def _mtime(path: Path) -> float:
    try:
        return path.stat().st_mtime
    except OSError:  # pragma: no cover - a directory that vanished mid-listing
        return 0.0


# --------------------------------------------------------------------------- #
# Dry run and execution
# --------------------------------------------------------------------------- #


def _plan_id_of(plan: planner.Plan | Mapping[str, object]) -> str:
    """The ``plan_id`` of a plan, however the caller holds it."""
    if isinstance(plan, planner.Plan):
        return plan.plan_id
    value = plan.get("plan_id")
    if not isinstance(value, str) or not value:
        raise PlanningError("the plan document does not name a plan_id")
    return value


def preview_plan(
    plan: planner.Plan | Mapping[str, object],
    approved: Sequence[str],
    *,
    quarantine_root: str | os.PathLike[str] | None = None,
    within: Sequence[str] = (),
) -> executor.ApplyReport:
    """Resolve the approved actions and touch nothing (the dry-run preview).

    This is :func:`spacesage.executor.apply_plan` without ``execute``: every
    approved item becomes the exact operation it would perform -- quarantine
    destination, move destination, the link that follows, or the refusal that
    stops it -- and no quarantine directory, no journal and no filesystem change
    happens.
    """
    return executor.apply_plan(
        plan,
        executor.make_manifest(_plan_id_of(plan), list(approved)),
        execute=False,
        quarantine_root=quarantine_root,
        within=within,
    )


def preview(
    draft: PlanDraft,
    approved: Sequence[str],
    *,
    quarantine_root: str | os.PathLike[str] | None = None,
    within: Sequence[str] = (),
) -> executor.ApplyReport:
    """Dry run of a draft's approved subset (what the preview dialog shows)."""
    return preview_plan(draft.plan, approved, quarantine_root=quarantine_root, within=within)


def execute(
    draft: PlanDraft,
    workspace: PlanWorkspace,
    approved: Sequence[str],
    *,
    quarantine_root: str | os.PathLike[str] | None = None,
    within: Sequence[str] = (),
    journal: str | os.PathLike[str] | None = None,
    on_op: executor.OnOp | None = None,
) -> executor.ApplyReport:
    """Execute the approved subset of a draft, journaled into the workspace.

    The approval is re-persisted first, so the manifest on disk always matches
    what is running; the journal defaults to the workspace's own, which is what
    makes the run undoable from the Undo screen.  ``on_op`` receives
    ``(result, index, total)`` after every action (live per-item progress).
    """
    if workspace.plan_id != draft.plan_id:
        raise PlanningError(
            f"the workspace holds plan {workspace.plan_id} but the draft is {draft.plan_id}"
        )
    save_approval(workspace, draft, approved, rejected=draft.advisory_ids())
    manifest = executor.make_manifest(draft.plan_id, list(approved), rejected=draft.advisory_ids())
    target = Path(journal) if journal is not None else workspace.journal_path
    return executor.apply_plan(
        draft.plan,
        manifest,
        execute=True,
        journal=target,
        quarantine_root=quarantine_root,
        within=within,
        plan_path=workspace.plan_path,
        manifest_path=workspace.manifest_path,
        on_op=on_op,
    )


# --------------------------------------------------------------------------- #
# The session: one draft and the workspace it lives in
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class PlanSession:
    """A draft plus the workspace its plan and approval are persisted in.

    Every step after drafting goes through here, so the two can never drift
    apart: the approval is written into the same workspace the plan came from,
    and the executor is handed that workspace's paths.
    """

    draft: PlanDraft
    workspace: PlanWorkspace

    @property
    def plan_id(self) -> str:
        """The plan's own id."""
        return self.draft.plan_id

    @property
    def plan(self) -> planner.Plan:
        """The composed plan."""
        return self.draft.plan

    def approve(self, approved: Sequence[str], *, rejected: Sequence[str] = ()) -> Path:
        """Persist the approved/rejected sets (the manifest on disk follows)."""
        return save_approval(self.workspace, self.draft, approved, rejected=rejected)

    def manifest(self) -> executor.Manifest | None:
        """The approval currently on disk (``None`` before the first write)."""
        return load_approval(self.workspace)

    def preview(
        self,
        approved: Sequence[str],
        *,
        quarantine_root: str | os.PathLike[str] | None = None,
        within: Sequence[str] = (),
    ) -> executor.ApplyReport:
        """Dry run of the approved subset (what the preview dialog renders)."""
        return preview(self.draft, approved, quarantine_root=quarantine_root, within=within)

    def execute(
        self,
        approved: Sequence[str],
        *,
        quarantine_root: str | os.PathLike[str] | None = None,
        within: Sequence[str] = (),
        on_op: executor.OnOp | None = None,
    ) -> executor.ApplyReport:
        """Execute the approved subset, journaled into this workspace."""
        return execute(
            self.draft,
            self.workspace,
            approved,
            quarantine_root=quarantine_root,
            within=within,
            on_op=on_op,
        )


def open_session(
    db_path: str | os.PathLike[str],
    ruleset: rules.RuleSet,
    request: PlanRequest,
    *,
    root: str | os.PathLike[str],
    quarantine_root: str | os.PathLike[str] | None = None,
) -> PlanSession:
    """Draft a selection against an index and persist its workspace.

    The one call the app makes when the user presses *Build plan*: it opens the
    index (read-only), composes the plan for exactly the checked rows, resolves
    what every action would do, and writes ``plan.json`` + ``approved.json``
    into ``root``.  Nothing on the analysed drives is touched.
    """
    conn = db.open_db(Path(db_path))
    try:
        draft = draft_plan(conn, ruleset, request, quarantine_root=quarantine_root)
    finally:
        conn.close()
    return PlanSession(draft=draft, workspace=write_draft(draft, root))


# --------------------------------------------------------------------------- #
# History and undo
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class JournalItem:
    """One journaled operation, with the status the undo view shows."""

    seq: int
    run: int
    at: str
    action_id: str
    type: str
    op: str
    inverse: str
    path: str
    dest: str | None
    bytes: int
    status: str
    """One of :data:`JOURNAL_STATUSES`."""

    reason: str
    verify: str
    reversible: bool

    @property
    def reclaims(self) -> bool:
        """True for the operations that moved bytes (quarantine/move)."""
        return self.op in ("quarantine", "move")

    def to_dict(self) -> dict[str, object]:
        """JSON-ready mapping."""
        return {
            "seq": self.seq,
            "run": self.run,
            "at": self.at,
            "action_id": self.action_id,
            "type": self.type,
            "op": self.op,
            "inverse": self.inverse,
            "path": self.path,
            "dest": self.dest,
            "bytes": self.bytes,
            "status": self.status,
            "reason": self.reason,
            "verify": self.verify,
            "reversible": self.reversible,
        }


@dataclass(frozen=True)
class JournalHistory:
    """One journal file: the runs, the operations and their undo state."""

    path: Path
    plan_id: str | None
    plan_path: str | None
    items: tuple[JournalItem, ...]
    run_modes: tuple[str, ...] = ()
    error: str = ""
    """Why this journal could not be read (empty when it could)."""

    @property
    def name(self) -> str:
        """The workspace token (or the file's own name) for the picker."""
        return self.path.parent.name if self.path.name == JOURNAL_FILE else self.path.name

    def pending(self) -> tuple[JournalItem, ...]:
        """The operations still waiting for a reversal."""
        return tuple(item for item in self.items if item.status == "pending")

    def counts(self) -> dict[str, int]:
        """``{status: count}`` over every item (zeros included)."""
        counts = dict.fromkeys(JOURNAL_STATUSES, 0)
        for item in self.items:
            counts[item.status] = counts.get(item.status, 0) + 1
        return counts

    def reclaimed_bytes(self) -> int:
        """Bytes this journal moved out of place (all of its operations)."""
        return sum(item.bytes for item in self.items if item.reclaims)

    def restored_bytes(self) -> int:
        """Bytes already moved back."""
        return sum(item.bytes for item in self.items if item.status == "reversed" and item.reclaims)

    def oldest(self) -> str:
        """The first record's timestamp (``""`` when the journal is empty)."""
        return self.items[0].at if self.items else ""

    def newest(self) -> str:
        """The last record's timestamp (``""`` when the journal is empty)."""
        return self.items[-1].at if self.items else ""

    def to_dict(self) -> dict[str, object]:
        """JSON-ready mapping (the undo screen's document)."""
        return {
            "journal": str(self.path),
            "plan_id": self.plan_id,
            "plan": self.plan_path,
            "error": self.error,
            "counts": self.counts(),
            "reclaimed_bytes": self.reclaimed_bytes(),
            "restored_bytes": self.restored_bytes(),
            "items": [item.to_dict() for item in self.items],
        }


def journal_history(path: str | os.PathLike[str]) -> JournalHistory:
    """Read one journal into the undo view's shape.

    A journal that cannot be read is returned with :attr:`JournalHistory.error`
    set rather than raising: the picker must be able to *show* the broken file,
    so the user learns which one it was.
    """
    journal_path = Path(path)
    try:
        parsed = executor.read_journal(journal_path)
    except executor.ExecutorError as exc:
        return JournalHistory(
            path=journal_path, plan_id=None, plan_path=None, items=(), error=str(exc)
        )
    items: list[JournalItem] = []
    for op in parsed.ops:
        resolved = parsed.resolved(op)
        if resolved is not None:
            status = "reversed" if resolved.outcome == "done" else "skipped"
            reason = resolved.reason
            verify = resolved.verify
        elif not op.finished:
            status = "interrupted"
            reason = op.reason or "the run stopped before this operation finished"
            verify = ""
        elif op.inverse:
            status = "pending"
            reason = op.reason or op.outcome
            verify = op.verify
        else:
            status = "skipped"
            reason = op.reason or "no way to reverse this operation"
            verify = op.verify
        items.append(
            JournalItem(
                seq=op.seq,
                run=op.run,
                at=op.at,
                action_id=op.action_id,
                type=op.type,
                op=op.op,
                inverse=op.inverse,
                path=op.src,
                dest=op.dest,
                bytes=op.bytes,
                status=status,
                reason=reason,
                verify=verify,
                reversible=bool(op.inverse) and status in ("pending", "interrupted"),
            )
        )
    last_run = parsed.runs[-1] if parsed.runs else None
    return JournalHistory(
        path=parsed.path,
        plan_id=last_run.plan_id if last_run is not None else None,
        plan_path=last_run.plan if last_run is not None else None,
        items=tuple(items),
        run_modes=tuple(run.mode for run in parsed.runs),
    )


def histories(root: str | os.PathLike[str]) -> tuple[JournalHistory, ...]:
    """Every journal of the app's plan workspaces, newest workspace first."""
    found: list[JournalHistory] = []
    for directory in plan_directories(root):
        journal = directory / JOURNAL_FILE
        if journal.is_file():
            found.append(journal_history(journal))
    return tuple(found)


def revert(
    history: JournalHistory | str | os.PathLike[str],
    *,
    only: Sequence[int] | None = None,
    on_op: executor.OnUndoOp | None = None,
) -> executor.UndoReport:
    """Reverse a journal (all of it, or the named ``seq`` numbers), verifying each.

    The executor owns the rules -- reverse order, digest verification, a path
    that is occupied again is *blocked*, never clobbered; this is the screen's
    entry point into them.
    """
    path = history.path if isinstance(history, JournalHistory) else Path(history)
    return executor.undo_journal(path, only=only, on_op=on_op)


def summarize_undo(report: executor.UndoReport) -> str:
    """One line about an undo run (the toast the screen shows)."""
    counts = report.counts()
    reversed_ = counts["done"]
    parts = [f"{reversed_} reversed"]
    for name, label in (("skipped", "skipped"), ("blocked", "blocked"), ("failed", "failed")):
        if counts[name]:
            parts.append(f"{counts[name]} {label}")
    return " · ".join(parts) + f" · {stats.format_bytes(report.restored_bytes())} restored"


def summarize_apply(report: executor.ApplyReport) -> str:
    """One line about an apply run (the toast the screen shows)."""
    counts = report.counts()
    if report.dry_run:
        return (
            f"{counts['planned']} operations resolved, nothing was touched "
            f"({stats.format_bytes(report.reclaimed_bytes())} to reclaim)"
        )
    parts = [f"{counts['done']} done"]
    for name in ("skipped", "refused", "failed"):
        if counts[name]:
            parts.append(f"{counts[name]} {name}")
    return " · ".join(parts) + f" · {stats.format_bytes(report.reclaimed_bytes())} reclaimed"
