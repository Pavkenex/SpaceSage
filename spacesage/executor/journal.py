"""The append-only journal and the undo engine.

Every filesystem operation the executor performs is appended to a JSONL file
*as it happens* (``spacesage.journal/v1``), in two phases: an ``op`` record with
``phase: "start"`` is written **before** the primitive runs, the ``phase: "end"``
record with the outcome, the verification and the digests lands right after.
That order is what makes an interrupted run recoverable: a start record without
its end record means "this may or may not have happened", and :func:`undo`
settles it against the live filesystem instead of guessing.

:func:`undo` reverses the surviving operations **in reverse order** -- the link
before the move that created it, the move before the quarantine it followed --
and verifies every payload against the digest the journal recorded on the way
in.  Undo is itself journaled, so a second ``spacesage undo`` reports "nothing to
undo" instead of moving things twice; an operation it could not reverse (the
original path is occupied again) stays pending and is retried next time.
"""

from __future__ import annotations

import json
import os
import time
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from spacesage import __version__

from .backend import (
    DEFAULT_CONTENT_LIMIT,
    Backend,
    ExecutorError,
    TreeDigest,
    digest,
    is_compressed,
    reparse_kind,
    verify,
)
from .report import UndoResult, UndoStep

JOURNAL_SCHEMA = "spacesage.journal/v1"
"""Schema string of every record in the file."""

DEFAULT_JOURNAL_NAME = "spacesage.journal.jsonl"
"""Journal file ``spacesage apply`` writes next to the plan it executes."""

KIND_RUN = "run"
KIND_OP = "op"
KIND_UNDO = "undo"
KIND_END = "end"

PHASE_START = "start"
PHASE_END = "end"

ATTEMPTED: tuple[str, ...] = ("started", "done", "failed")
"""Op outcomes that mean "the filesystem may have changed" (undo candidates)."""

RESOLVED_BY_UNDO: tuple[str, ...] = ("done", "skipped")
"""Undo outcomes that close an operation; ``blocked``/``failed`` are retried."""

INVERSE_OPS: Mapping[str, str] = {
    "quarantine": "move_back",
    "move": "move_back",
    "link": "remove_link",
    "compress": "uncompress",
}
"""The operation that reverses each journaled op."""


@dataclass(frozen=True)
class JournalRun:
    """One ``apply``/``undo`` invocation."""

    run: int
    at: str
    mode: str
    plan_id: str | None
    plan: str | None
    manifest: str | None
    backend: str
    quarantined_to: str | None

    def to_dict(self) -> dict[str, object]:
        """JSON-ready mapping."""
        return {
            "run": self.run,
            "at": self.at,
            "mode": self.mode,
            "plan_id": self.plan_id,
            "plan": self.plan,
            "manifest": self.manifest,
            "backend": self.backend,
            "quarantine_root": self.quarantined_to,
        }


@dataclass(frozen=True)
class JournalOp:
    """One filesystem operation (start + end records merged into one view)."""

    seq: int
    run: int
    at: str
    action_id: str
    type: str
    op: str
    outcome: str
    reason: str
    bytes: int
    src: str
    dest: str | None
    link: str | None
    verify: str
    notes: tuple[str, ...]
    before: TreeDigest | None
    after: TreeDigest | None
    finished: bool = True

    @property
    def inverse(self) -> str:
        """The operation that reverses this one (``""`` when there is none)."""
        return INVERSE_OPS.get(self.op, "")

    @property
    def touched_filesystem(self) -> bool:
        """Might this operation have changed something (started but unfinished counts)?"""
        return not self.finished or self.outcome in ("done", "failed")

    def to_dict(self) -> dict[str, object]:
        """JSON-ready mapping."""
        return {
            "seq": self.seq,
            "run": self.run,
            "at": self.at,
            "action_id": self.action_id,
            "type": self.type,
            "op": self.op,
            "outcome": self.outcome,
            "reason": self.reason,
            "bytes": self.bytes,
            "src": self.src,
            "dest": self.dest,
            "link": self.link,
            "verify": self.verify,
            "notes": list(self.notes),
            "before": self.before.to_dict() if self.before is not None else None,
            "after": self.after.to_dict() if self.after is not None else None,
            "finished": self.finished,
        }


@dataclass(frozen=True)
class JournalUndo:
    """One reversal attempt of a journaled operation."""

    seq: int
    run: int
    at: str
    op_ref: int
    action_id: str
    op: str
    outcome: str
    reason: str
    src: str
    dest: str | None
    verify: str
    notes: tuple[str, ...]
    finished: bool = True

    def to_dict(self) -> dict[str, object]:
        """JSON-ready mapping."""
        return {
            "seq": self.seq,
            "run": self.run,
            "at": self.at,
            "op_ref": self.op_ref,
            "action_id": self.action_id,
            "op": self.op,
            "outcome": self.outcome,
            "reason": self.reason,
            "src": self.src,
            "dest": self.dest,
            "verify": self.verify,
            "notes": list(self.notes),
            "finished": self.finished,
        }


@dataclass(frozen=True)
class Journal:
    """A parsed journal: the runs, the operations and the undo attempts."""

    path: Path
    runs: tuple[JournalRun, ...]
    ops: tuple[JournalOp, ...]
    undos: tuple[JournalUndo, ...]

    def next_run(self) -> int:
        """The number the next run gets (1 when the file is fresh)."""
        return max((run.run for run in self.runs), default=0) + 1

    def next_seq(self) -> int:
        """The next free operation sequence number."""
        highest = max((op.seq for op in self.ops), default=0)
        return max(highest, max((undo.seq for undo in self.undos), default=0)) + 1

    def by_seq(self, seq: int) -> JournalOp | None:
        """The operation with this sequence number."""
        for op in self.ops:
            if op.seq == seq:
                return op
        return None

    def resolved(self, op: JournalOp) -> JournalUndo | None:
        """The undo record that closed this operation, if there is one."""
        for undo in reversed(self.undos):
            if undo.op_ref == op.seq and undo.finished and undo.outcome in RESOLVED_BY_UNDO:
                return undo
        return None

    def pending(self) -> tuple[JournalOp, ...]:
        """Operations that still need reversing, newest first (reverse order)."""
        candidates = [
            op
            for op in self.ops
            if op.touched_filesystem and op.inverse and self.resolved(op) is None
        ]
        return tuple(sorted(candidates, key=lambda op: op.seq, reverse=True))

    def to_dict(self) -> dict[str, object]:
        """JSON-ready summary of the file (``spacesage apply --json`` context)."""
        return {
            "schema": JOURNAL_SCHEMA,
            "journal": str(self.path),
            "runs": [run.to_dict() for run in self.runs],
            "ops": [op.to_dict() for op in self.ops],
            "undos": [undo.to_dict() for undo in self.undos],
            "pending": [op.seq for op in self.pending()],
        }


# --------------------------------------------------------------------------- #
# Writing
# --------------------------------------------------------------------------- #


class JournalWriter:
    """Append-only JSONL writer: one record per line, flushed and fsynced."""

    def __init__(self, path: str | os.PathLike[str]) -> None:
        self.path = Path(path)
        existing = read_journal(self.path) if self.path.is_file() else None
        self._next_run = existing.next_run() if existing is not None else 1
        self._next_seq = existing.next_seq() if existing is not None else 1

    def start_run(
        self,
        *,
        mode: str,
        backend: str,
        plan_id: str | None = None,
        plan: str | None = None,
        manifest: str | None = None,
        quarantine_root: str | None = None,
    ) -> int:
        """Record the beginning of a run; returns its run number."""
        run = self._next_run
        self._next_run += 1
        self._append(
            {
                "kind": KIND_RUN,
                "phase": PHASE_START,
                "run": run,
                "at": _now(),
                "mode": mode,
                "plan_id": plan_id,
                "plan": plan,
                "manifest": manifest,
                "backend": backend,
                "quarantine_root": quarantine_root,
                "tool": f"spacesage {__version__}",
            }
        )
        return run

    def start_op(
        self,
        *,
        run: int,
        action_id: str,
        action_type: str,
        op: str,
        reason: str,
        bytes_: int,
        src: str,
        dest: str | None,
        link: str | None,
        before: TreeDigest | None = None,
    ) -> int:
        """Record an operation *before* it runs; returns its sequence number."""
        seq = self._next_seq
        self._next_seq += 1
        self._append(
            {
                "kind": KIND_OP,
                "phase": PHASE_START,
                "seq": seq,
                "run": run,
                "at": _now(),
                "action_id": action_id,
                "type": action_type,
                "op": op,
                "outcome": "started",
                "reason": reason,
                "bytes": bytes_,
                "src": src,
                "dest": dest,
                "link": link,
                "verify": "",
                "notes": [],
                "before": before.to_dict() if before is not None else None,
            }
        )
        return seq

    def finish_op(
        self,
        *,
        run: int,
        seq: int,
        action_id: str,
        action_type: str,
        op: str,
        outcome: str,
        reason: str,
        bytes_: int,
        src: str,
        dest: str | None,
        link: str | None,
        verify: str,
        after: TreeDigest | None = None,
        notes: Sequence[str] = (),
        command: Sequence[str] = (),
    ) -> int:
        """Record how an operation ended (same ``seq``: the two records merge)."""
        self._append(
            {
                "kind": KIND_OP,
                "phase": PHASE_END,
                "seq": seq,
                "run": run,
                "at": _now(),
                "action_id": action_id,
                "type": action_type,
                "op": op,
                "outcome": outcome,
                "reason": reason,
                "bytes": bytes_,
                "src": src,
                "dest": dest,
                "link": link,
                "verify": verify,
                "notes": list(notes),
                "command": list(command),
                "after": after.to_dict() if after is not None else None,
            }
        )
        return seq

    def start_undo(
        self,
        *,
        run: int,
        op_ref: int,
        action_id: str,
        op: str,
        reason: str,
        src: str,
        dest: str | None,
    ) -> int:
        """Record a reversal *before* it runs."""
        seq = self._next_seq
        self._next_seq += 1
        self._append(
            {
                "kind": KIND_UNDO,
                "phase": PHASE_START,
                "seq": seq,
                "run": run,
                "at": _now(),
                "op_ref": op_ref,
                "action_id": action_id,
                "op": op,
                "outcome": "started",
                "reason": reason,
                "src": src,
                "dest": dest,
                "verify": "",
                "notes": [],
            }
        )
        return seq

    def finish_undo(
        self,
        *,
        run: int,
        seq: int,
        op_ref: int,
        action_id: str,
        op: str,
        outcome: str,
        reason: str,
        src: str,
        dest: str | None,
        verify: str,
        notes: Sequence[str] = (),
    ) -> int:
        """Record how a reversal ended."""
        self._append(
            {
                "kind": KIND_UNDO,
                "phase": PHASE_END,
                "seq": seq,
                "run": run,
                "at": _now(),
                "op_ref": op_ref,
                "action_id": action_id,
                "op": op,
                "outcome": outcome,
                "reason": reason,
                "src": src,
                "dest": dest,
                "verify": verify,
                "notes": list(notes),
            }
        )
        return seq

    def end_run(
        self,
        *,
        run: int,
        mode: str,
        counts: Mapping[str, int],
        bytes_: int,
        ok: bool,
    ) -> None:
        """Close a run with its outcome counts (what a reader sees at a glance)."""
        self._append(
            {
                "kind": KIND_END,
                "phase": PHASE_END,
                "run": run,
                "at": _now(),
                "mode": mode,
                "outcomes": dict(counts),
                "bytes": bytes_,
                "ok": ok,
            }
        )

    def _append(self, record: Mapping[str, object]) -> None:
        payload = dict(record)
        payload["schema"] = JOURNAL_SCHEMA
        line = json.dumps(payload, sort_keys=True, ensure_ascii=False)
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with self.path.open("a", encoding="utf-8") as handle:
                handle.write(line + "\n")
                handle.flush()
                os.fsync(handle.fileno())
        except OSError as exc:
            # An operation that cannot be journaled cannot be undone: refuse the
            # run instead of doing something nobody can take back.
            raise ExecutorError(f"cannot write the journal at {self.path}: {exc}") from None


# --------------------------------------------------------------------------- #
# Reading
# --------------------------------------------------------------------------- #


def read_journal(path: str | os.PathLike[str]) -> Journal:
    """Parse a journal file (``spacesage.journal/v1``).

    Tolerant about blank lines, strict about everything else: a malformed record
    is an :class:`~spacesage.executor.backend.ExecutorError` naming the line, so a
    corrupted journal can never be silently "repaired" into a wrong undo.
    """
    journal_path = Path(path)
    if not journal_path.is_file():
        raise ExecutorError(f"no journal at {journal_path}")
    runs: dict[int, dict[str, object]] = {}
    ops: dict[int, dict[str, object]] = {}
    undos: dict[int, dict[str, object]] = {}
    for number, line in enumerate(journal_path.read_text(encoding="utf-8").splitlines(), start=1):
        if not line.strip():
            continue
        try:
            record = json.loads(line)
        except json.JSONDecodeError as exc:
            raise ExecutorError(f"{journal_path}:{number}: not JSON ({exc.msg})") from None
        if not isinstance(record, dict):
            raise ExecutorError(f"{journal_path}:{number}: record must be an object")
        where = f"{journal_path}:{number}"
        if _text(record, "schema", where=where) != JOURNAL_SCHEMA:
            raise ExecutorError(f"{where}: schema must be {JOURNAL_SCHEMA!r}")
        kind = _text(record, "kind", where=where)
        if kind == KIND_RUN:
            runs[_int(record, "run", where=where)] = record
        elif kind == KIND_OP:
            _merge(ops, record, where=where)
        elif kind == KIND_UNDO:
            _merge(undos, record, where=where)
        elif kind != KIND_END:
            raise ExecutorError(f"{where}: unknown record kind {kind!r}")
    return Journal(
        path=journal_path,
        runs=tuple(_run(runs[key]) for key in sorted(runs)),
        ops=tuple(_op(key, ops[key]) for key in sorted(ops)),
        undos=tuple(_undo(key, undos[key]) for key in sorted(undos)),
    )


def _merge(into: dict[int, dict[str, object]], record: Mapping[str, object], *, where: str) -> None:
    """Merge a start/end pair of records under one sequence number."""
    seq = _int(record, "seq", where=where)
    phase = _text(record, "phase", where=where)
    if phase not in (PHASE_START, PHASE_END):
        raise ExecutorError(f"{where}: phase must be 'start' or 'end' (got {phase!r})")
    merged = into.setdefault(seq, {})
    if phase == PHASE_END:
        merged.update(record)
        merged["finished"] = True
    else:
        merged["finished"] = False
        for key, value in record.items():
            merged.setdefault(key, value)


def _run(record: Mapping[str, object]) -> JournalRun:
    where = "run record"
    return JournalRun(
        run=_int(record, "run", where=where),
        at=_text(record, "at", where=where),
        mode=_text(record, "mode", where=where),
        plan_id=_optional_text(record, "plan_id", where=where),
        plan=_optional_text(record, "plan", where=where),
        manifest=_optional_text(record, "manifest", where=where),
        backend=_optional_text(record, "backend", where=where) or "",
        quarantined_to=_optional_text(record, "quarantine_root", where=where),
    )


def _op(seq: int, record: Mapping[str, object]) -> JournalOp:
    where = f"op {seq}"
    return JournalOp(
        seq=seq,
        run=_int(record, "run", where=where),
        at=_text(record, "at", where=where),
        action_id=_text(record, "action_id", where=where),
        type=_optional_text(record, "type", where=where) or "",
        op=_text(record, "op", where=where),
        outcome=_text(record, "outcome", where=where),
        reason=_maybe_blank(record, "reason", where=where),
        bytes=_int(record, "bytes", where=where),
        src=_text(record, "src", where=where),
        dest=_optional_text(record, "dest", where=where),
        link=_optional_text(record, "link", where=where),
        verify=_maybe_blank(record, "verify", where=where),
        notes=_texts(record, "notes", where=where),
        before=_digest(record, "before", where=where),
        after=_digest(record, "after", where=where),
        finished=bool(record.get("finished", True)),
    )


def _undo(seq: int, record: Mapping[str, object]) -> JournalUndo:
    where = f"undo {seq}"
    return JournalUndo(
        seq=seq,
        run=_int(record, "run", where=where),
        at=_text(record, "at", where=where),
        op_ref=_int(record, "op_ref", where=where),
        action_id=_text(record, "action_id", where=where),
        op=_text(record, "op", where=where),
        outcome=_text(record, "outcome", where=where),
        reason=_maybe_blank(record, "reason", where=where),
        src=_text(record, "src", where=where),
        dest=_optional_text(record, "dest", where=where),
        verify=_maybe_blank(record, "verify", where=where),
        notes=_texts(record, "notes", where=where),
        finished=bool(record.get("finished", True)),
    )


def _text(record: Mapping[str, object], key: str, *, where: str) -> str:
    value = record.get(key)
    if not isinstance(value, str) or not value:
        raise ExecutorError(f"{where}: {key!r} must be a non-empty string (got {value!r})")
    return value


def _optional_text(record: Mapping[str, object], key: str, *, where: str) -> str | None:
    value = record.get(key)
    if value is None:
        return None
    if not isinstance(value, str) or not value:
        raise ExecutorError(f"{where}: {key!r} must be a non-empty string or null (got {value!r})")
    return value


def _maybe_blank(record: Mapping[str, object], key: str, *, where: str) -> str:
    """A string field that may be empty (``verify`` starts empty, ``reason`` may be)."""
    value = record.get(key)
    if value is None:
        return ""
    if not isinstance(value, str):
        raise ExecutorError(f"{where}: {key!r} must be a string (got {value!r})")
    return value


def _int(record: Mapping[str, object], key: str, *, where: str) -> int:
    value = record.get(key)
    if isinstance(value, bool) or not isinstance(value, int):
        raise ExecutorError(f"{where}: {key!r} must be an integer (got {value!r})")
    return value


def _texts(record: Mapping[str, object], key: str, *, where: str) -> tuple[str, ...]:
    value = record.get(key) or []
    if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
        raise ExecutorError(f"{where}: {key!r} must be a list of strings (got {value!r})")
    return tuple(value)


def _digest(record: Mapping[str, object], key: str, *, where: str) -> TreeDigest | None:
    value = record.get(key)
    if value is None:
        return None
    if not isinstance(value, Mapping):
        raise ExecutorError(f"{where}: {key!r} must be an object or null (got {value!r})")
    return TreeDigest.from_dict(value)


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


# --------------------------------------------------------------------------- #
# Undo
# --------------------------------------------------------------------------- #


def undo(
    journal: Journal,
    *,
    backend: Backend,
    content_limit: int = DEFAULT_CONTENT_LIMIT,
    writer: JournalWriter | None = None,
) -> tuple[UndoResult, ...]:
    """Reverse every pending operation, newest first, verifying each payload.

    Returns one :class:`~spacesage.executor.report.UndoResult` per operation, in
    the order they were reversed.  The run is journaled as it happens, so a crash
    mid-undo leaves the remaining operations pending for the next attempt.
    """
    pending = journal.pending()
    if not pending:
        return ()
    active = JournalWriter(journal.path) if writer is None else writer
    last_run = journal.runs[-1] if journal.runs else None
    run = active.start_run(
        mode="undo",
        backend=backend.name,
        plan_id=last_run.plan_id if last_run is not None else None,
        plan=last_run.plan if last_run is not None else None,
        manifest=last_run.manifest if last_run is not None else None,
    )
    results: list[UndoResult] = []
    for op in pending:
        results.append(
            _reverse(op, backend=backend, writer=active, run=run, content_limit=content_limit)
        )
    counts: dict[str, int] = dict.fromkeys(("done", "skipped", "blocked", "failed"), 0)
    for result in results:
        counts[result.outcome] = counts.get(result.outcome, 0) + 1
    active.end_run(
        run=run,
        mode="undo",
        counts=counts,
        bytes_=sum(
            step.before.bytes
            for result in results
            for step in result.steps
            if step.op == "move_back" and step.outcome == "done" and step.before is not None
        ),
        ok=not any(result.outcome in ("failed", "blocked") for result in results),
    )
    return tuple(results)


def _reverse(
    op: JournalOp,
    *,
    backend: Backend,
    writer: JournalWriter,
    run: int,
    content_limit: int,
) -> UndoResult:
    """Reverse one operation (see the module docstring for the outcome vocabulary)."""
    inverse = op.inverse
    reason = _reversal_reason(op)
    src, dest = (op.dest or op.src, op.src) if inverse == "move_back" else (op.src, None)
    seq = writer.start_undo(
        run=run,
        op_ref=op.seq,
        action_id=op.action_id,
        op=inverse,
        reason=reason,
        src=src,
        dest=dest,
    )
    outcome, detail, verify_state, notes, before, after = _perform_reversal(
        op, inverse, backend=backend, content_limit=content_limit
    )
    if not op.finished:
        detail = f"settled an interrupted {op.op}: {detail}"
    writer.finish_undo(
        run=run,
        seq=seq,
        op_ref=op.seq,
        action_id=op.action_id,
        op=inverse,
        outcome=outcome,
        reason=detail,
        src=src,
        dest=dest,
        verify=verify_state,
        notes=notes,
    )
    step = UndoStep(
        op=inverse,
        outcome=outcome,
        reason=detail,
        src=src,
        dest=dest,
        verify=verify_state,
        notes=notes,
        before=before,
        after=after,
    )
    return UndoResult(
        op_ref=op.seq,
        action_id=op.action_id,
        op=inverse,
        outcome=outcome,
        reason=detail,
        steps=(step,),
    )


def _reversal_reason(op: JournalOp) -> str:
    if not op.finished:
        return f"an interrupted {op.op}: checking the filesystem before reversing it"
    return f"reversing {op.op} #{op.seq} ({op.reason or op.outcome})"


def _perform_reversal(
    op: JournalOp,
    inverse: str,
    *,
    backend: Backend,
    content_limit: int,
) -> tuple[str, str, str, tuple[str, ...], TreeDigest | None, TreeDigest | None]:
    """Do the reversal; returns ``(outcome, reason, verify, notes, before, after)``."""
    if inverse == "move_back":
        return _reverse_move(op, backend=backend, content_limit=content_limit)
    if inverse == "remove_link":
        return _reverse_link(op, backend=backend)
    if inverse == "uncompress":
        return _reverse_compress(op, backend=backend)
    return "skipped", f"no way to reverse a {op.op!r} operation", "skipped", (), None, None


def _reverse_move(
    op: JournalOp, *, backend: Backend, content_limit: int
) -> tuple[str, str, str, tuple[str, ...], TreeDigest | None, TreeDigest | None]:
    """Move a quarantined/moved payload back where it came from."""
    there = op.dest
    if there is None:
        return (
            "skipped",
            "the journal does not say where the payload went",
            "skipped",
            (),
            None,
            None,
        )
    here = op.src
    if not os.path.lexists(there):
        if os.path.lexists(here):
            return (
                "skipped",
                f"nothing to reverse: {here} is already in place",
                "skipped",
                (),
                None,
                None,
            )
        return (
            "skipped",
            f"the payload is gone from {there} (purged or moved by hand)",
            "skipped",
            (),
            None,
            None,
        )
    if os.path.lexists(here):
        return (
            "blocked",
            f"{here} is occupied again; the payload stays at {there}",
            "skipped",
            ("clear the path, then run spacesage undo again",),
            None,
            None,
        )
    before = op.before
    primitive = backend.move(there, here)
    after = digest(backend.path_for_os(here), content_limit=content_limit) if primitive.ok else None
    state = verify(before, after) if before is not None and after is not None else "unavailable"
    if not primitive.ok:
        return "failed", primitive.detail, state, (), before, after
    if state == "mismatch":
        return (
            "failed",
            f"moved back, but the payload does not match what was quarantined ({primitive.detail})",
            state,
            (),
            before,
            after,
        )
    return "done", primitive.detail, state, (), before, after


def _reverse_link(
    op: JournalOp, *, backend: Backend
) -> tuple[str, str, str, tuple[str, ...], TreeDigest | None, TreeDigest | None]:
    """Remove the link the move left behind (the target's contents stay)."""
    path = op.src
    if not os.path.lexists(path):
        return "skipped", f"the link at {path} is already gone", "skipped", (), None, None
    if reparse_kind(path) is None and not _shares_its_payload(path):
        return (
            "skipped",
            f"{path} is no longer a link; left untouched",
            "skipped",
            (),
            None,
            None,
        )
    primitive = backend.remove_link(path)
    if not primitive.ok:
        return "failed", primitive.detail, "skipped", (), None, None
    gone = reparse_kind(path) is None and not _shares_its_payload(path)
    return "done", primitive.detail, "verified" if gone else "mismatch", (), None, None


def _shares_its_payload(path: str) -> bool:
    """True when another directory entry points at the same payload (hard link)."""
    try:
        return os.stat(path).st_nlink > 1
    except OSError:
        return False


def _reverse_compress(
    op: JournalOp, *, backend: Backend
) -> tuple[str, str, str, tuple[str, ...], TreeDigest | None, TreeDigest | None]:
    """Undo in-place compression (the file's bytes never changed)."""
    if not os.path.lexists(op.src):
        return "skipped", f"{op.src} is gone", "skipped", (), None, None
    if not is_compressed(op.src):
        return "skipped", f"{op.src} is not compressed (nothing to undo)", "skipped", (), None, None
    primitive = backend.uncompress(op.src)
    state = (
        "skipped" if not primitive.ok else ("verified" if not is_compressed(op.src) else "mismatch")
    )
    if not primitive.ok:
        return "failed", primitive.detail, state, (), None, None
    return "done", primitive.detail, state, (), None, None


def default_journal_path(plan_path: str | os.PathLike[str]) -> Path:
    """Where ``spacesage apply`` journals a plan by default (next to the plan)."""
    return Path(plan_path).expanduser().resolve().parent / DEFAULT_JOURNAL_NAME


def now_iso() -> str:
    """The timestamp format every record uses."""
    return _now()


def elapsed(start: float) -> float:
    """Seconds since ``start`` (``time.monotonic``), rounded for reports."""
    return max(0.0, time.monotonic() - start)
