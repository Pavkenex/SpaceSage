"""The executor: approve, re-validate, execute, journal, undo.

``docs/design.md`` sections 2 and 8: analysis is read-only, execution happens
only on an itemized, manifest-bound approval, every operation is re-validated
against the live filesystem *immediately before it runs*, and everything that
happens is journaled so it can be undone.

The flow of :func:`apply_plan`:

1. **Validate the plan** -- a document that is not a valid ``spacesage.plan/v1``
   (wrong schema, ids that do not recompute, a T3 path in an executable slot) is
   refused, never "repaired".
2. **Bind the manifest** -- the approval manifest must name the plan's own
   ``plan_id`` and only action ids that plan actually has (:class:`Manifest`).
   Without ``--execute`` the run is a **dry run**: the resolved per-op plan is
   printed and the filesystem is not touched at all (not even a quarantine
   directory is created).
3. **Resolve** every approved action into concrete operations: the quarantine
   destination inside ``_spacesage_quarantine/<plan token>/`` on the source's own
   volume, the move destination, the link that follows the move.  Refusals
   (report-only tier, protected path, wildcard, a link the platform cannot
   create, a hard link that would cross volumes, a path outside ``within``)
   surface here, before anything runs.
4. **Re-validate and execute** each op in plan order: the source must still
   exist, must not have become a reparse point, must not be locked, and the
   destination must not exist yet.  A payload that is gone is *skipped* and
   reported -- never guessed about -- and every move is verified by digest when
   it lands (entry count, size and content hash of the payload).
5. **Journal** each step before and after it runs
   (:mod:`spacesage.executor.journal`), so :func:`undo_journal` can reverse the
   run in reverse order and verify every payload on the way back.

The same rules apply on the way back: :func:`undo_journal` re-checks each
payload against the digest the journal recorded when it was quarantined and
refuses to clobber a path that has been taken again.
"""

from __future__ import annotations

import json
import os
import re
import time
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, replace
from pathlib import Path

from spacesage import planner

from . import posix, win
from .backend import (
    DEFAULT_CONTENT_LIMIT,
    QUARANTINE_DIRNAME,
    Backend,
    ExecutorError,
    PrimResult,
    TreeDigest,
    digest,
    is_compressed,
    is_under,
    is_windows_path,
    join_path,
    link_points_at,
    plan_token,
    protected_reason,
    quarantine_relative,
    reparse_kind,
    same_volume,
    verify,
)
from .journal import (
    ATTEMPTED,
    DEFAULT_JOURNAL_NAME,
    INVERSE_OPS,
    JOURNAL_SCHEMA,
    Journal,
    JournalOp,
    JournalWriter,
    default_journal_path,
    elapsed,
    now_iso,
    read_journal,
)
from .journal import (
    undo as reverse_pending,
)
from .report import (
    APPLY_OUTCOMES,
    UNDO_OUTCOMES,
    ApplyReport,
    OpResult,
    Step,
    UndoReport,
    UndoResult,
    count_outcomes,
    link_label,
    worst_outcome,
)

__all__ = [
    "APPLY_OUTCOMES",
    "ATTEMPTED",
    "DEFAULT_CONTENT_LIMIT",
    "DEFAULT_JOURNAL_NAME",
    "INVERSE_OPS",
    "JOURNAL_SCHEMA",
    "MANIFEST_SCHEMA",
    "QUARANTINE_DIRNAME",
    "UNDO_OUTCOMES",
    "ApplyReport",
    "Backend",
    "ExecutorError",
    "Journal",
    "JournalOp",
    "JournalWriter",
    "Manifest",
    "OpResult",
    "Step",
    "UndoReport",
    "UndoResult",
    "apply_plan",
    "current_backend",
    "default_journal_path",
    "load_manifest",
    "load_plan",
    "make_manifest",
    "parse_manifest",
    "plan_ops",
    "read_journal",
    "undo_journal",
    "write_manifest",
]

MANIFEST_SCHEMA = "spacesage.approved/v1"
"""Schema string of the approval manifest (``approved.json``)."""

QUARANTINE_MANIFEST = "manifest.json"
"""The audit file written into every quarantine store (design section 8)."""

QUARANTINE_SCHEMA = "spacesage.quarantine/v1"
"""Schema string of that audit file."""

_ACTION_ID_RE = re.compile(r"^a[0-9]+$")


# --------------------------------------------------------------------------- #
# The approval manifest
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class Manifest:
    """The subset of a plan's action ids a human approved (design section 7)."""

    plan_id: str
    approved: tuple[str, ...]
    schema: str = MANIFEST_SCHEMA
    created: str | None = None
    rejected: tuple[str, ...] = ()
    note: str | None = None

    def to_dict(self) -> dict[str, object]:
        """JSON-ready manifest (``spacesage.approved/v1``)."""
        return {
            "schema": self.schema,
            "plan_id": self.plan_id,
            "created": self.created,
            "approved": list(self.approved),
            "rejected": list(self.rejected),
            "note": self.note,
        }


def parse_manifest(data: object) -> Manifest:
    """Validate a parsed ``approved.json``; raises :class:`ExecutorError`."""
    if not isinstance(data, Mapping):
        raise ExecutorError(f"the manifest must be an object, got {type(data).__name__}")
    schema = data.get("schema")
    if schema != MANIFEST_SCHEMA:
        raise ExecutorError(f"the manifest schema must be {MANIFEST_SCHEMA!r} (got {schema!r})")
    plan_id = data.get("plan_id")
    if not isinstance(plan_id, str) or not plan_id:
        raise ExecutorError("the manifest must name the 'plan_id' it approves")
    created = data.get("created")
    if created is not None and not isinstance(created, str):
        raise ExecutorError(f"manifest.created must be a string or null (got {created!r})")
    note = data.get("note")
    if note is not None and not isinstance(note, str):
        raise ExecutorError(f"manifest.note must be a string or null (got {note!r})")
    return Manifest(
        plan_id=plan_id,
        approved=_manifest_ids(data.get("approved"), key="approved"),
        created=created,
        rejected=_manifest_ids(data.get("rejected"), key="rejected", required=False),
        note=note,
    )


def _manifest_ids(value: object, *, key: str, required: bool = True) -> tuple[str, ...]:
    """Deduplicated, validated action ids from a manifest field."""
    if value is None and not required:
        return ()
    if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
        raise ExecutorError(f"manifest.{key} must be a list of action ids (got {value!r})")
    seen: dict[str, None] = {}
    for item in value:
        if _ACTION_ID_RE.match(item) is None:
            raise ExecutorError(f"manifest.{key} contains {item!r}, which is not an action id")
        seen[item] = None
    return tuple(seen)


def load_manifest(path: str | os.PathLike[str]) -> Manifest:
    """Read and validate an ``approved.json`` from disk."""
    manifest_path = Path(path)
    if not manifest_path.is_file():
        raise ExecutorError(f"no approval manifest at {manifest_path}")
    try:
        data = json.loads(manifest_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise ExecutorError(f"{manifest_path}: not JSON ({exc.msg})") from None
    return parse_manifest(data)


def make_manifest(
    plan_id: str,
    approved: Sequence[str],
    *,
    rejected: Sequence[str] = (),
    note: str | None = None,
    created: str | None = None,
) -> Manifest:
    """Build a manifest for the given action ids (what an approval UI writes)."""
    return Manifest(
        plan_id=plan_id,
        approved=_manifest_ids(list(approved), key="approved"),
        created=created or now_iso(),
        rejected=_manifest_ids(list(rejected), key="rejected", required=False),
        note=note,
    )


def write_manifest(path: str | os.PathLike[str], manifest: Manifest) -> Path:
    """Write ``approved.json`` (pretty-printed, newline-terminated)."""
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(manifest.to_dict(), indent=2) + "\n", encoding="utf-8")
    return target


def load_plan(path: str | os.PathLike[str]) -> Mapping[str, object]:
    """Read ``plan.json`` from disk (validated by :func:`apply_plan`)."""
    plan_path = Path(path)
    if not plan_path.is_file():
        raise ExecutorError(f"no plan at {plan_path}")
    try:
        data = json.loads(plan_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise ExecutorError(f"{plan_path}: not JSON ({exc.msg})") from None
    if not isinstance(data, Mapping):
        raise ExecutorError(f"{plan_path}: the plan must be a JSON object")
    return {str(key): value for key, value in data.items()}


def current_backend() -> Backend:
    """The backend of the running platform (``win`` on Windows, else ``posix``)."""
    return win.BACKEND if os.name == "nt" else posix.BACKEND


# --------------------------------------------------------------------------- #
# Reading a validated plan
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class _Action:
    """One plan action, as the executor reads it."""

    id: str
    type: str
    path: str
    bytes: int
    tier: str
    dest: str | None
    link: str | None
    why: str
    advisory: bool


def _text(entry: Mapping[str, object], key: str, *, where: str) -> str:
    value = entry.get(key)
    if not isinstance(value, str) or not value:
        raise ExecutorError(f"{where}: {key!r} must be a non-empty string (got {value!r})")
    return value


def _maybe_text(entry: Mapping[str, object], key: str, *, where: str) -> str | None:
    value = entry.get(key)
    if value is None:
        return None
    if not isinstance(value, str) or not value:
        raise ExecutorError(f"{where}: {key!r} must be a non-empty string or null (got {value!r})")
    return value


def _plan_mapping(plan: planner.Plan | Mapping[str, object]) -> Mapping[str, object]:
    if isinstance(plan, planner.Plan):
        return plan.to_dict()
    if not isinstance(plan, Mapping):
        raise ExecutorError(
            f"the plan must be a spacesage.plan/v1 mapping or a planner.Plan "
            f"(got {type(plan).__name__})"
        )
    return {str(key): value for key, value in plan.items()}


def _plan_actions(plan_data: Mapping[str, object]) -> tuple[_Action, ...]:
    """Every action of a plan that :func:`planner.validate_plan` already accepted."""
    raw = plan_data.get("actions")
    if not isinstance(raw, list):
        raise ExecutorError("the plan has no actions list")
    actions: list[_Action] = []
    for index, entry in enumerate(raw, start=1):
        if not isinstance(entry, Mapping):
            raise ExecutorError(f"actions[{index}] must be an object")
        where = f"actions[{index}]"
        action_type = _text(entry, "type", where=where)
        if action_type not in planner.TYPES:
            raise ExecutorError(f"{where}: unknown action type {action_type!r}")
        size = entry.get("bytes")
        if isinstance(size, bool) or not isinstance(size, int):
            raise ExecutorError(f"{where}: 'bytes' must be an integer (got {size!r})")
        actions.append(
            _Action(
                id=_text(entry, "id", where=where),
                type=action_type,
                path=_text(entry, "path", where=where),
                bytes=size,
                tier=_text(entry, "tier", where=where),
                dest=_maybe_text(entry, "dest", where=where),
                link=_maybe_text(entry, "link_after", where=where),
                why=_maybe_text(entry, "why", where=where) or "",
                advisory=action_type in planner.ADVISORY_TYPES,
            )
        )
    return tuple(actions)


def plan_ops(plan: planner.Plan | Mapping[str, object]) -> tuple[str, tuple[_Action, ...]]:
    """``(plan_id, actions)`` of a plan the executor could run (fully validated)."""
    plan_data = _plan_mapping(plan)
    try:
        planner.validate_plan(plan_data)
    except planner.PlannerError as exc:
        raise ExecutorError(f"the plan is not a valid spacesage.plan/v1 document: {exc}") from None
    return str(plan_data["plan_id"]), _plan_actions(plan_data)


# --------------------------------------------------------------------------- #
# Resolution and re-validation
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class _Resolved:
    """One approved action, resolved into the operations it would perform."""

    action: _Action
    op: str
    """``quarantine`` / ``move`` / ``compress`` / ``advisory``."""

    dest: str | None
    link: str | None
    quarantine_root: str | None
    problems: tuple[str, ...] = ()
    """Re-validation refusals (empty when the op may run)."""


def _resolve(
    action: _Action,
    *,
    plan_data: Mapping[str, object],
    be: Backend,
    quarantine_root: str | None,
    within: Sequence[str],
) -> _Resolved:
    """Turn one approved action into the op(s) it would run, with its refusals."""
    if action.advisory:
        return _Resolved(action=action, op="advisory", dest=None, link=None, quarantine_root=None)
    token = plan_token(str(plan_data.get("plan_id") or ""))
    dest: str | None = None
    link: str | None = None
    root: str | None = None
    if action.type == "DELETE_QUARANTINE":
        op = "quarantine"
        root = quarantine_root or be.default_quarantine_root(action.path)
        dest = join_path(root, token, *quarantine_relative(action.path))
    elif action.type == "MOVE":
        op = "move"
        dest = action.dest
        link = action.link or "NONE"
    else:
        op = "compress"
    problems = _revalidate(action, op=op, dest=dest, link=link, plan_data=plan_data, within=within)
    return _Resolved(
        action=action, op=op, dest=dest, link=link, quarantine_root=root, problems=problems
    )


def _revalidate(
    action: _Action,
    *,
    op: str,
    dest: str | None,
    link: str | None,
    plan_data: Mapping[str, object],
    within: Sequence[str],
) -> tuple[str, ...]:
    """Everything that refuses an op *before* it runs (design section 2).

    The plan contract was checked once (:func:`plan_ops`); this is the second
    look, against what the op would actually do: the tier, the path shape, the
    protected roots, the plan's own target drives, the optional ``within``
    guard, and cross-volume hard links.
    """
    problems: list[str] = []
    if action.tier == planner.REPORT_TIER:
        problems.append(f"tier {action.tier} is report-only; {action.type} may not act on it")
    for label, path in (("the path", action.path), ("the destination", dest)):
        if path is None:
            continue
        reason = protected_reason(path)
        if reason:
            problems.append(f"{label} {path}: {reason}")
    if action.path and not _is_absolute(action.path) and not action.advisory:
        problems.append(f"the path {action.path!r} is not absolute")
    if op == "quarantine" and dest is None:
        problems.append("no quarantine destination could be resolved")
    if op == "move":
        if dest is None:
            problems.append("a MOVE action without a destination cannot run")
        else:
            if not _under_targets(dest, plan_data):
                problems.append(f"the destination {dest} is not under any declared target drive")
            if is_under(dest, action.path):
                problems.append(f"the destination {dest} sits inside the source {action.path}")
    if link == "HARDLINK" and dest is not None and not same_volume(action.path, dest):
        problems.append(f"a hard link cannot cross volumes ({action.path} -> {dest})")
    if within and not any(is_under(action.path, root) for root in within):
        problems.append(
            f"{action.path} is outside the roots this run is confined to ({', '.join(within)})"
        )
    return tuple(problems)


def _is_absolute(path: str) -> bool:
    return os.path.isabs(path) or bool(
        re.match(r"^[A-Za-z]:[\\/]", path) or path.startswith("\\\\")
    )


def _absolute(path: str | os.PathLike[str]) -> str:
    """A root the user named, as the OS sees it (a relative one means the cwd's).

    Windows-shaped paths are left exactly as written, so a plan from another
    machine can still be resolved and previewed on a POSIX box.
    """
    text = os.fspath(path)
    if is_windows_path(text):
        return text
    return os.path.abspath(text)


def _under_targets(dest: str, plan_data: Mapping[str, object]) -> bool:
    """Does the destination live below one of the plan's declared targets?"""
    targets = plan_data.get("targets")
    if not isinstance(targets, Mapping):
        return False
    return any(
        isinstance(name, str) and is_under(dest, planner.target_root(name)) for name in targets
    )


def _step_summary(steps: Sequence[Step]) -> str:
    """The sentence the report shows for the action (the worst step speaks).

    When every step is fine the reasons are joined, so a move that also created
    the link reads as "renamed (same volume); symlink created -> D:\\Moved\\x".
    """
    trouble = [step for step in steps if step.outcome not in ("done", "planned")]
    if trouble:
        return trouble[0].reason
    return "; ".join(dict.fromkeys(step.reason for step in steps if step.reason))


# --------------------------------------------------------------------------- #
# Execution
# --------------------------------------------------------------------------- #


class _Runner:
    """Runs (or plans) the resolved operations of one apply run."""

    def __init__(
        self,
        *,
        backend: Backend,
        writer: JournalWriter | None,
        run: int,
        execute: bool,
        content_limit: int,
        quarantine_root: str | None,
        plan_id: str,
    ) -> None:
        self.backend = backend
        self.writer = writer
        self.run_number = run
        self.execute = execute
        self.content_limit = content_limit
        self.quarantine_root = quarantine_root
        self.plan_id = plan_id

    # -- entry point ------------------------------------------------------- #

    def run(self, resolved: _Resolved) -> OpResult:
        """Plan or execute one approved action into a report row."""
        action = resolved.action
        if action.advisory:
            reason = "advisory action (review/native): nothing to execute"
            step = Step(
                op="advisory",
                outcome="skipped",
                reason=reason,
                src=action.path,
                bytes=action.bytes,
            )
            return self._result(action, "skipped", reason, (step,), advisory=True)
        if resolved.problems:
            reason = "; ".join(resolved.problems)
            step = Step(
                op=resolved.op,
                outcome="refused",
                reason=reason,
                src=action.path,
                dest=resolved.dest,
                link=resolved.link,
                bytes=action.bytes,
                verify="skipped",
            )
            if self.execute:
                self._journal_stop(resolved, step=step)
            return self._result(action, "refused", reason, (step,))
        allowed, why = self._platform_allows(resolved)
        if not allowed:
            step = Step(
                op=resolved.op,
                outcome="skipped",
                reason=why,
                src=action.path,
                dest=resolved.dest,
                link=resolved.link,
                bytes=action.bytes,
                verify="skipped",
            )
            if self.execute:
                self._journal_stop(resolved, step=step)
            return self._result(action, "skipped", why, (step,))
        steps = self._execute(resolved) if self.execute else self._plan(resolved)
        outcome = worst_outcome([step.outcome for step in steps])
        return self._result(action, outcome, _step_summary(steps), steps)

    def _result(
        self,
        action: _Action,
        outcome: str,
        reason: str,
        steps: tuple[Step, ...],
        *,
        advisory: bool = False,
    ) -> OpResult:
        return OpResult(
            action_id=action.id,
            type=action.type,
            tier=action.tier,
            path=action.path,
            bytes=action.bytes,
            outcome=outcome,
            reason=reason,
            steps=steps,
            advisory=advisory,
        )

    def _platform_allows(self, resolved: _Resolved) -> tuple[bool, str]:
        """Can this platform perform the op at all (links, NTFS compression)?"""
        if resolved.op == "compress":
            return self.backend.can_compress()
        if resolved.op == "move" and resolved.link and resolved.link != "NONE":
            exists = os.path.lexists(self._os(resolved.action.path))
            is_dir = os.path.isdir(self._os(resolved.action.path)) if exists else True
            return self.backend.can_link(resolved.link, is_dir=is_dir)
        return True, ""

    # -- dry run ----------------------------------------------------------- #

    def _plan(self, resolved: _Resolved) -> tuple[Step, ...]:
        """Resolve the op for the preview without touching anything."""
        action = resolved.action
        steps = [
            Step(
                op=resolved.op,
                outcome="planned",
                reason=self._plan_reason(resolved),
                src=action.path,
                dest=resolved.dest,
                link=resolved.link,
                bytes=action.bytes,
            )
        ]
        if resolved.op == "move" and resolved.link and resolved.link != "NONE":
            steps.append(
                Step(
                    op="link",
                    outcome="planned",
                    reason=(
                        f"link the original path back as a {link_label(resolved.link)}: "
                        f"{action.path} -> {resolved.dest}"
                    ),
                    src=action.path,
                    dest=resolved.dest,
                    link=resolved.link,
                )
            )
        return tuple(steps)

    def _plan_reason(self, resolved: _Resolved) -> str:
        action = resolved.action
        if resolved.op == "quarantine":
            return f"quarantine {action.path} -> {resolved.dest}"
        if resolved.op == "move":
            return f"move {action.path} -> {resolved.dest}"
        if resolved.op == "compress":
            return f"compress {action.path} in place (NTFS)"
        return f"nothing to execute for {action.path}"

    # -- execution --------------------------------------------------------- #

    def _execute(self, resolved: _Resolved) -> tuple[Step, ...]:
        """Perform one approved op (and the link that may follow it)."""
        if resolved.op == "quarantine":
            return (self._run_quarantine(resolved),)
        if resolved.op == "move":
            return self._run_move(resolved)
        if resolved.op == "compress":
            return (self._run_compress(resolved),)
        raise ExecutorError(f"cannot execute a {resolved.op!r} operation")

    def _run_quarantine(self, resolved: _Resolved) -> Step:
        dest = resolved.dest
        if dest is None:
            return self._fail(resolved, "no quarantine destination was resolved")
        blocked = self._preflight(resolved)
        if blocked is not None:
            return blocked
        store = resolved.quarantine_root
        if store is not None:
            error = self._prepare_store(store)
            if error is not None:
                return self._fail(resolved, f"cannot create the quarantine store {store}: {error}")
        before = self._digest(resolved.action.path)
        seq = self._start_op(
            resolved, op="quarantine", reason=f"quarantine {resolved.action.path}", before=before
        )
        primitive = self.backend.move(self._os(resolved.action.path), self._os(dest))
        step = self._finish_move(
            resolved, seq=seq, op="quarantine", before=before, primitive=primitive
        )
        if step.outcome == "done" and store is not None:
            note = self._record_in_store(resolved, before=before)
            if note is not None:
                step = replace(step, notes=(*step.notes, note))
        return step

    def _run_move(self, resolved: _Resolved) -> tuple[Step, ...]:
        dest = resolved.dest
        if dest is None:
            return (self._fail(resolved, "no destination was resolved"),)
        blocked = self._preflight(resolved)
        if blocked is not None:
            return (blocked,)
        link = resolved.link or "NONE"
        before = self._digest(resolved.action.path)
        extra_notes = (
            ("the original path is not linked back: apps that expect it will not find it",)
            if link == "NONE"
            else ()
        )
        seq = self._start_op(
            resolved, op="move", reason=f"move {resolved.action.path} -> {dest}", before=before
        )
        primitive = self.backend.move(self._os(resolved.action.path), self._os(dest))
        move_step = self._finish_move(
            resolved,
            seq=seq,
            op="move",
            before=before,
            primitive=primitive,
            extra_notes=extra_notes,
        )
        if move_step.outcome != "done" or link == "NONE":
            return (move_step,)
        return (move_step, self._run_link(resolved, before=before))

    def _run_link(self, resolved: _Resolved, *, before: TreeDigest | None) -> Step:
        """Create the link the plan promised at the original path."""
        action = resolved.action
        dest = resolved.dest
        link = resolved.link or "NONE"
        is_dir = before.is_dir if before is not None else os.path.isdir(self._os(action.path))
        seq = self._start_op(
            resolved, op="link", reason=f"create a {link_label(link)} at {action.path}"
        )
        primitive = self.backend.create_link(
            path=self._os(action.path),
            target=self._os(dest or ""),
            kind=link,
            is_dir=is_dir,
        )
        points = False
        detail = primitive.detail
        if primitive.ok and dest is not None:
            points, detail = link_points_at(self._os(action.path), self._os(dest), link)
        outcome = "done" if (primitive.ok and points) else "failed"
        reason = primitive.detail if primitive.ok else f"the link could not be created: {detail}"
        if primitive.ok and not points:
            reason = f"the link was created but does not point at the destination ({detail})"
        notes = tuple(note for note in (primitive.note,) if note)
        step = Step(
            op="link",
            outcome=outcome,
            reason=reason,
            src=action.path,
            dest=dest,
            link=link,
            verify="verified" if points else ("skipped" if not primitive.ok else "mismatch"),
            command=primitive.command,
            notes=notes,
        )
        self._finish_op(resolved, seq=seq, step=step)
        return step

    def _run_compress(self, resolved: _Resolved) -> Step:
        action = resolved.action
        blocked = self._preflight(resolved)
        if blocked is not None:
            return blocked
        if is_compressed(self._os(action.path)):
            return self._skip(resolved, f"{action.path} is already compressed")
        seq = self._start_op(resolved, op="compress", reason=f"compress {action.path} in place")
        primitive = self.backend.compress(self._os(action.path))
        compressed = is_compressed(self._os(action.path))
        outcome = "done" if (primitive.ok and compressed) else "failed"
        reason = primitive.detail or "compressed in place"
        if primitive.ok and not compressed:
            reason = "compact reported success but the entry is still not compressed"
        step = Step(
            op="compress",
            outcome=outcome,
            reason=reason,
            src=action.path,
            bytes=action.bytes,
            verify="verified" if compressed else ("skipped" if not primitive.ok else "mismatch"),
            command=primitive.command,
            notes=tuple(note for note in (primitive.note,) if note),
        )
        self._finish_op(resolved, seq=seq, step=step)
        return step

    # -- helpers ----------------------------------------------------------- #

    def _os(self, path: str) -> str:
        """The path as this platform's OS wants it (Windows long paths)."""
        return self.backend.path_for_os(path)

    def _digest(self, path: str) -> TreeDigest:
        """Digest a payload; the report keeps the path the plan uses."""
        result = digest(self._os(path), content_limit=self.content_limit)
        return result if result.path == path else replace(result, path=path)

    def _preflight(self, resolved: _Resolved) -> Step | None:
        """Re-validate against the live filesystem; a Step means "do not run this"."""
        action = resolved.action
        path = action.path
        if not os.path.lexists(self._os(path)):
            if resolved.dest is not None and os.path.lexists(self._os(resolved.dest)):
                return self._skip(
                    resolved,
                    f"{path} is gone and the destination {resolved.dest} already exists",
                )
            return self._skip(resolved, f"{path} no longer exists")
        kind = reparse_kind(self._os(path))
        if kind is not None:
            return self._refuse(resolved, f"{path} is a {kind} now; refusing to act through it")
        locked, why = self.backend.is_locked(self._os(path))
        if locked:
            return self._skip(resolved, f"{path} is in use: {why}")
        if resolved.dest is not None and os.path.lexists(self._os(resolved.dest)):
            return self._skip(resolved, f"the destination {resolved.dest} already exists")
        return None

    def _prepare_store(self, store: str) -> str | None:
        """Create the quarantine store; returns the error text (or ``None``)."""
        try:
            os.makedirs(self._os(store), exist_ok=True)
        except OSError as exc:
            return str(exc)
        return None

    def _record_in_store(self, resolved: _Resolved, *, before: TreeDigest | None) -> str | None:
        """Append this payload to the quarantine store's audit manifest."""
        store = resolved.quarantine_root
        if store is None or resolved.dest is None:
            return None
        token_dir = join_path(store, plan_token(self.plan_id))
        manifest_path = Path(self._os(join_path(token_dir, QUARANTINE_MANIFEST)))
        try:
            entries: list[object] = []
            if manifest_path.is_file():
                existing = json.loads(manifest_path.read_text(encoding="utf-8"))
                if isinstance(existing, Mapping):
                    raw = existing.get("entries")
                    if isinstance(raw, list):
                        entries = list(raw)
            entries.append(
                {
                    "action_id": resolved.action.id,
                    "path": resolved.action.path,
                    "quarantine_path": resolved.dest,
                    "bytes": before.bytes if before is not None else resolved.action.bytes,
                    "is_dir": bool(before.is_dir) if before is not None else False,
                    "tree_sha256": before.tree_sha256 if before is not None else None,
                    "at": now_iso(),
                }
            )
            payload = {
                "schema": QUARANTINE_SCHEMA,
                "plan_id": self.plan_id,
                "created": now_iso(),
                "entries": entries,
            }
            manifest_path.parent.mkdir(parents=True, exist_ok=True)
            manifest_path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
        except (OSError, ValueError) as exc:
            return f"the quarantine store manifest could not be updated ({exc})"
        return None

    def _skip(self, resolved: _Resolved, reason: str) -> Step:
        return self._stop(resolved, outcome="skipped", reason=reason)

    def _refuse(self, resolved: _Resolved, reason: str) -> Step:
        return self._stop(resolved, outcome="refused", reason=reason)

    def _fail(self, resolved: _Resolved, reason: str) -> Step:
        return self._stop(resolved, outcome="failed", reason=reason)

    def _stop(self, resolved: _Resolved, *, outcome: str, reason: str) -> Step:
        """A decision not to act: reported and (when executing) journaled too.

        Skips and refusals never touch the filesystem, so a single finished
        record is enough -- but they are recorded: "no silent partial success"
        means a reader can tell an approved action was deliberately left alone.
        """
        step = Step(
            op=resolved.op,
            outcome=outcome,
            reason=reason,
            src=resolved.action.path,
            dest=resolved.dest,
            link=resolved.link,
            bytes=resolved.action.bytes,
            verify="skipped",
        )
        self._journal_stop(resolved, step=step)
        return step

    def _journal_stop(self, resolved: _Resolved, *, step: Step) -> None:
        """Record a step that was decided without touching anything."""
        seq = self._start_op(resolved, op=step.op, reason=step.reason)
        if seq is None or self.writer is None:
            return
        self.writer.finish_op(
            run=self.run_number,
            seq=seq,
            action_id=resolved.action.id,
            action_type=resolved.action.type,
            op=step.op,
            outcome=step.outcome,
            reason=step.reason,
            bytes_=step.bytes,
            src=step.src,
            dest=step.dest,
            link=step.link,
            verify=step.verify,
        )

    def _finish_move(
        self,
        resolved: _Resolved,
        *,
        seq: int | None,
        op: str,
        before: TreeDigest | None,
        primitive: PrimResult,
        extra_notes: Sequence[str] = (),
    ) -> Step:
        """Verify a move/quarantine and journal how it ended."""
        action = resolved.action
        dest = resolved.dest
        after = self._digest(dest) if (primitive.ok and dest is not None) else None
        state = verify(before, after) if (before is not None and after is not None) else "skipped"
        notes = [note for note in (primitive.note, *extra_notes) if note]
        outcome = "done"
        reason = primitive.detail or "moved"
        if not primitive.ok:
            outcome = "failed"
            reason = primitive.detail or "the operation did not complete"
        elif state == "mismatch":
            outcome = "failed"
            reason = "the payload arrived but does not match what was there"
            notes.append("the payload is at the destination; spacesage undo can move it back")
        elif os.path.lexists(self._os(action.path)):
            outcome = "failed"
            reason = f"the move did not remove the source {action.path}"
        step = Step(
            op=op,
            outcome=outcome,
            reason=reason,
            src=action.path,
            dest=dest,
            link=resolved.link,
            bytes=before.bytes if before is not None else action.bytes,
            verify=state,
            command=primitive.command,
            notes=tuple(notes),
            before=before,
            after=after,
        )
        self._finish_op(resolved, seq=seq, step=step)
        return step

    def _start_op(
        self, resolved: _Resolved, *, op: str, reason: str, before: TreeDigest | None = None
    ) -> int | None:
        if self.writer is None or not self.execute:
            return None
        return self.writer.start_op(
            run=self.run_number,
            action_id=resolved.action.id,
            action_type=resolved.action.type,
            op=op,
            reason=reason,
            bytes_=resolved.action.bytes,
            src=resolved.action.path,
            dest=resolved.dest,
            link=resolved.link,
            before=before,
        )

    def _finish_op(self, resolved: _Resolved, *, seq: int | None, step: Step) -> None:
        if self.writer is None or seq is None:
            return
        self.writer.finish_op(
            run=self.run_number,
            seq=seq,
            action_id=resolved.action.id,
            action_type=resolved.action.type,
            op=step.op,
            outcome=step.outcome,
            reason=step.reason,
            bytes_=step.bytes,
            src=step.src,
            dest=step.dest,
            link=step.link,
            verify=step.verify,
            after=step.after,
            notes=step.notes,
            command=step.command,
        )


# --------------------------------------------------------------------------- #
# The two entry points
# --------------------------------------------------------------------------- #


def apply_plan(
    plan: planner.Plan | Mapping[str, object],
    manifest: Manifest,
    *,
    execute: bool = False,
    journal: str | os.PathLike[str] | None = None,
    quarantine_root: str | os.PathLike[str] | None = None,
    within: Sequence[str] = (),
    backend: Backend | None = None,
    content_limit: int = DEFAULT_CONTENT_LIMIT,
    plan_path: str | os.PathLike[str] | None = None,
    manifest_path: str | os.PathLike[str] | None = None,
) -> ApplyReport:
    """Apply the approved subset of ``plan`` (a dry run unless ``execute``).

    Refuses (raises :class:`ExecutorError`) when the plan is invalid, when the
    manifest does not bind to this very plan, when it names action ids the plan
    does not have, or when an execution run has nowhere to journal to.
    """
    started = now_iso()
    clock = time.monotonic()
    plan_data = _plan_mapping(plan)
    plan_id, actions = plan_ops(plan_data)
    _check_manifest(manifest, plan_id, actions)
    be = backend if backend is not None else current_backend()
    root = _absolute(quarantine_root) if quarantine_root is not None else None
    approved = set(manifest.approved)
    selected = tuple(action for action in actions if action.id in approved)
    resolved = tuple(
        _resolve(
            action,
            plan_data=plan_data,
            be=be,
            quarantine_root=root,
            within=tuple(_absolute(entry) for entry in within),
        )
        for action in selected
    )

    journal_target: Path | None = None
    writer: JournalWriter | None = None
    run = 0
    if execute:
        if journal is not None:
            journal_target = Path(journal)
        elif plan_path is not None:
            journal_target = default_journal_path(plan_path)
        if journal_target is None:
            raise ExecutorError(
                "refusing to execute without a journal: pass journal=<path> "
                "(or plan_path=...) so every operation can be undone"
            )
        writer = JournalWriter(journal_target)
        run = writer.start_run(
            mode="execute",
            backend=be.name,
            plan_id=plan_id,
            plan=str(plan_path) if plan_path is not None else None,
            manifest=str(manifest_path) if manifest_path is not None else None,
            quarantine_root=root,
        )
    elif journal is not None:
        writer = JournalWriter(journal)
        journal_target = writer.path
        run = writer.start_run(
            mode="dry-run",
            backend=be.name,
            plan_id=plan_id,
            plan=str(plan_path) if plan_path is not None else None,
            manifest=str(manifest_path) if manifest_path is not None else None,
            quarantine_root=root,
        )

    runner = _Runner(
        backend=be,
        writer=writer,
        run=run,
        execute=execute,
        content_limit=content_limit,
        quarantine_root=root,
        plan_id=plan_id,
    )
    ops = tuple(runner.run(item) for item in resolved)
    if writer is not None:
        writer.end_run(
            run=run,
            mode="execute" if execute else "dry-run",
            counts=count_outcomes(ops, APPLY_OUTCOMES),
            bytes_=sum(
                step.bytes
                for op in ops
                for step in op.steps
                if step.op in ("quarantine", "move") and step.outcome == "done"
            ),
            ok=not any(op.outcome in ("failed", "refused") for op in ops),
        )
    return ApplyReport(
        plan_id=plan_id,
        dry_run=not execute,
        backend=be.name,
        started=started,
        elapsed_s=elapsed(clock),
        plan_path=str(plan_path) if plan_path is not None else None,
        manifest_path=str(manifest_path) if manifest_path is not None else None,
        journal_path=str(journal_target) if journal_target is not None else None,
        quarantine_root=root,
        ops=ops,
        total_actions=len(actions),
    )


def _check_manifest(manifest: Manifest, plan_id: str, actions: Sequence[_Action]) -> None:
    """The manifest gates exactly which items may run (design section 2)."""
    if not isinstance(manifest, Manifest):
        raise ExecutorError(f"the manifest must be an executor.Manifest (got {type(manifest)!r})")
    if manifest.plan_id != plan_id:
        raise ExecutorError(
            f"the manifest approves plan {manifest.plan_id} but this plan is {plan_id}: "
            "re-approve the current plan"
        )
    known = {action.id for action in actions}
    unknown = sorted(set(manifest.approved) - known, key=_action_key)
    if unknown:
        raise ExecutorError(
            f"the manifest approves action id(s) this plan does not have: {', '.join(unknown)}"
        )


def _action_key(action_id: str) -> tuple[int, str]:
    """``a2`` sorts before ``a10``."""
    return (int(action_id[1:]) if action_id[1:].isdigit() else 0, action_id)


def undo_journal(
    journal: str | os.PathLike[str],
    *,
    backend: Backend | None = None,
    content_limit: int = DEFAULT_CONTENT_LIMIT,
) -> UndoReport:
    """Reverse every pending operation of a journal, newest first, verifying each.

    Running it twice is safe: the second run finds nothing pending and says so.
    """
    started = now_iso()
    clock = time.monotonic()
    be = backend if backend is not None else current_backend()
    parsed = read_journal(journal)
    already = sum(1 for op in parsed.ops if op.inverse and parsed.resolved(op) is not None)
    results = reverse_pending(parsed, backend=be, content_limit=content_limit)
    return UndoReport(
        journal_path=str(parsed.path),
        backend=be.name,
        started=started,
        elapsed_s=elapsed(clock),
        ops=results,
        already_undone=already,
    )
