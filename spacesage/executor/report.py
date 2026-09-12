"""What an apply/undo run reports back (the executor's result types).

The report is the machine-readable twin of the journal: same facts, one line per
action instead of per filesystem operation.  ``spacesage apply --json`` prints
:meth:`ApplyReport.to_dict`, ``spacesage undo --json`` prints
:meth:`UndoReport.to_dict`, and the GUI reads the same objects.

Outcomes
--------

An **apply** op is one of:

``planned``
    dry-run only: the op was resolved and would run (nothing was touched).
``done``
    the filesystem operation completed *and* verified.
``skipped``
    nothing was done on purpose (the source is gone, a lock, an advisory item,
    a link the platform does not have) -- always with a reason.
``refused``
    re-validation says this must never run (T3, a protected path, a link where
    none is intended): the plan is not executed further for that op.
``failed``
    the operation was attempted and did not complete (or did not verify).

An **undo** op is one of ``done`` / ``skipped`` / ``blocked`` / ``failed``;
``blocked`` is the retryable skip (the original path is occupied), so a later
``spacesage undo`` picks it up again.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass

from spacesage.stats import format_bytes

from .backend import TreeDigest

APPLY_OUTCOMES: tuple[str, ...] = ("planned", "done", "skipped", "refused", "failed")
"""Every outcome an apply op can report, in increasing severity."""

UNDO_OUTCOMES: tuple[str, ...] = ("done", "skipped", "blocked", "failed")
"""Every outcome an undo op can report."""

_LINK_LABELS: Mapping[str, str] = {
    "JUNCTION": "junction",
    "SYMLINK": "symlink",
    "HARDLINK": "hard link",
    "NONE": "no link",
}

_VERIFY_LABELS: Mapping[str, str] = {
    "verified": "verified",
    "mismatch": "MISMATCH",
    "unavailable": "unverified (payload too large to hash)",
    "not-run": "not run",
    "skipped": "skipped",
}


def link_label(kind: str | None) -> str:
    """Human wording for a ``link_after`` value."""
    if kind is None:
        return ""
    return _LINK_LABELS.get(kind, kind.lower())


def verify_label(state: str) -> str:
    """Human wording for a verification state."""
    return _VERIFY_LABELS.get(state, state)


def worst_outcome(outcomes: Sequence[str]) -> str:
    """The most severe outcome of a sequence (``failed`` > ``refused`` > ...)."""
    ranked = {name: index for index, name in enumerate(APPLY_OUTCOMES)}
    return min(outcomes, key=lambda name: ranked.get(name, 0), default="skipped")


@dataclass(frozen=True)
class Step:
    """One filesystem operation (a move, the link that follows it, ...)."""

    op: str
    """``quarantine`` / ``move`` / ``link`` / ``compress`` / ``advisory``."""

    outcome: str
    reason: str
    src: str
    dest: str | None = None
    link: str | None = None
    bytes: int = 0
    verify: str = "not-run"
    command: tuple[str, ...] = ()
    notes: tuple[str, ...] = ()
    before: TreeDigest | None = None
    after: TreeDigest | None = None

    def to_dict(self) -> dict[str, object]:
        """JSON-ready mapping."""
        return {
            "op": self.op,
            "outcome": self.outcome,
            "reason": self.reason,
            "src": self.src,
            "dest": self.dest,
            "link": self.link,
            "bytes": self.bytes,
            "verify": self.verify,
            "command": list(self.command),
            "notes": list(self.notes),
            "before": self.before.to_dict() if self.before is not None else None,
            "after": self.after.to_dict() if self.after is not None else None,
        }

    def line(self) -> str:
        """One report line (the op plus, when there is one, its outcome)."""
        where = self.src if self.dest is None else f"{self.src} -> {self.dest}"
        detail = f"{format_bytes(self.bytes)} " if self.bytes else ""
        parts = [f"    step {self.op:<10} {self.outcome:<8} {detail}{where}"]
        if self.reason:
            parts.append(f" ({self.reason})")
        return "".join(parts)


@dataclass(frozen=True)
class OpResult:
    """The report row of one approved plan action."""

    action_id: str
    type: str
    tier: str
    path: str
    bytes: int
    outcome: str
    reason: str
    steps: tuple[Step, ...] = ()
    advisory: bool = False
    """True for ``REVIEW``/``NATIVE``: shown, never executed."""

    def to_dict(self) -> dict[str, object]:
        """JSON-ready mapping."""
        return {
            "id": self.action_id,
            "type": self.type,
            "tier": self.tier,
            "path": self.path,
            "bytes": self.bytes,
            "outcome": self.outcome,
            "reason": self.reason,
            "advisory": self.advisory,
            "steps": [step.to_dict() for step in self.steps],
        }

    def line(self) -> str:
        """One report line: id, type, path, size, outcome."""
        detail = f"{format_bytes(self.bytes):>10} " if self.bytes else f"{'-':>10} "
        return (
            f"  {self.action_id:<4} {self.type:<18} {detail}{self.path}\n"
            f"      {self.outcome}: {self.reason}"
        )


def count_outcomes(ops: Sequence[OpResult], names: Sequence[str]) -> dict[str, int]:
    """``{outcome: count}`` with every outcome present (zeros included)."""
    counts = dict.fromkeys(names, 0)
    for op in ops:
        counts[op.outcome] = counts.get(op.outcome, 0) + 1
    return counts


@dataclass(frozen=True)
class ApplyReport:
    """The result of one ``apply`` run (dry-run or executed)."""

    plan_id: str
    dry_run: bool
    backend: str
    started: str
    elapsed_s: float
    plan_path: str | None = None
    manifest_path: str | None = None
    journal_path: str | None = None
    quarantine_root: str | None = None
    ops: tuple[OpResult, ...] = ()
    total_actions: int = 0
    """How many actions the plan holds in total (the manifest's share of it)."""

    def counts(self) -> dict[str, int]:
        """``{outcome: count}`` over every op."""
        return count_outcomes(self.ops, APPLY_OUTCOMES)

    def ok(self) -> bool:
        """True when nothing failed and nothing was refused."""
        return not any(op.outcome in ("failed", "refused") for op in self.ops)

    def reclaimed_bytes(self) -> int:
        """Bytes the ops move (quarantines and moves that ran, or would run)."""
        return sum(
            step.bytes
            for op in self.ops
            for step in op.steps
            if step.op in ("quarantine", "move") and step.outcome in ("done", "planned")
        )

    def to_dict(self) -> dict[str, object]:
        """JSON-ready report (``spacesage.executor/v1``)."""
        return {
            "schema": "spacesage.executor/v1",
            "plan_id": self.plan_id,
            "mode": "dry-run" if self.dry_run else "execute",
            "backend": self.backend,
            "started": self.started,
            "elapsed_s": round(self.elapsed_s, 3),
            "plan": self.plan_path,
            "manifest": self.manifest_path,
            "journal": self.journal_path,
            "quarantine_root": self.quarantine_root,
            "total_actions": self.total_actions,
            "counts": self.counts(),
            "reclaimed_bytes": self.reclaimed_bytes(),
            "ok": self.ok(),
            "ops": [op.to_dict() for op in self.ops],
        }

    def render_text(self) -> str:
        """The human report ``spacesage apply`` prints."""
        counts = self.counts()
        lines = [
            f"SpaceSage apply -- plan {self.plan_id}"
            + (" (dry run)" if self.dry_run else " (executing)"),
            f"  plan:      {self.plan_path or '(passed in memory)'}",
            f"  manifest:  {self.manifest_path or '(passed in memory)'}",
            f"  backend:   {self.backend}",
        ]
        if self.journal_path:
            lines.append(f"  journal:   {self.journal_path}")
        if self.quarantine_root:
            lines.append(f"  quarantine:{self.quarantine_root}")
        lines.append(f"  approved:  {len(self.ops)} of {self.total_actions} actions")
        lines.append("")
        if not self.ops:
            lines.append("  nothing to do: the manifest approves no action of this plan")
        for op in self.ops:
            lines.append(op.line())
            for step in op.steps:
                lines.append(step.line())
        lines.append("")
        lines.append(
            "summary: "
            f"{counts['planned']} planned, {counts['done']} done, {counts['skipped']} skipped, "
            f"{counts['refused']} refused, {counts['failed']} failed -- "
            f"{format_bytes(self.reclaimed_bytes())}"
            + (" to reclaim" if self.dry_run else " moved to quarantine/the target drives")
        )
        if self.dry_run:
            lines.append("dry run: nothing was changed; re-run with --execute to perform these ops")
        return "\n".join(lines) + "\n"


@dataclass(frozen=True)
class UndoStep:
    """One reversed filesystem operation."""

    op: str
    outcome: str
    reason: str
    src: str
    dest: str | None = None
    verify: str = "not-run"
    notes: tuple[str, ...] = ()
    before: TreeDigest | None = None
    after: TreeDigest | None = None

    def to_dict(self) -> dict[str, object]:
        """JSON-ready mapping."""
        return {
            "op": self.op,
            "outcome": self.outcome,
            "reason": self.reason,
            "src": self.src,
            "dest": self.dest,
            "verify": self.verify,
            "notes": list(self.notes),
            "before": self.before.to_dict() if self.before is not None else None,
            "after": self.after.to_dict() if self.after is not None else None,
        }

    def line(self) -> str:
        """One report line."""
        where = self.src if self.dest is None else f"{self.src} -> {self.dest}"
        return f"    step {self.op:<12} {self.outcome:<8} {where} ({self.reason})"


@dataclass(frozen=True)
class UndoResult:
    """The report row of one journal operation being reversed."""

    op_ref: int
    """The journal ``seq`` of the operation (or the first one of the action)."""

    action_id: str
    op: str
    outcome: str
    reason: str
    steps: tuple[UndoStep, ...] = ()

    def to_dict(self) -> dict[str, object]:
        """JSON-ready mapping."""
        return {
            "op_ref": self.op_ref,
            "action_id": self.action_id,
            "op": self.op,
            "outcome": self.outcome,
            "reason": self.reason,
            "steps": [step.to_dict() for step in self.steps],
        }

    def line(self) -> str:
        """One report line: journal reference, action, inverse op, outcome."""
        return (
            f"  #{self.op_ref:<4} ({self.action_id}) {self.op:<12} {self.outcome:<8} {self.reason}"
        )


@dataclass(frozen=True)
class UndoReport:
    """The result of one ``undo`` run."""

    journal_path: str
    backend: str
    started: str
    elapsed_s: float
    ops: tuple[UndoResult, ...] = ()
    already_undone: int = 0
    """Operations the journal already shows as reversed."""

    def counts(self) -> dict[str, int]:
        """``{outcome: count}`` over every undo op."""
        counts = dict.fromkeys(UNDO_OUTCOMES, 0)
        for op in self.ops:
            counts[op.outcome] = counts.get(op.outcome, 0) + 1
        return counts

    def ok(self) -> bool:
        """True when nothing failed and nothing is still blocked."""
        return not any(op.outcome in ("failed", "blocked") for op in self.ops)

    def restored_bytes(self) -> int:
        """Bytes moved back by the reversed quarantines and moves."""
        return sum(
            step.before.bytes
            for op in self.ops
            for step in op.steps
            if step.op == "move_back" and step.outcome == "done" and step.before is not None
        )

    def to_dict(self) -> dict[str, object]:
        """JSON-ready report (``spacesage.undo/v1``)."""
        return {
            "schema": "spacesage.undo/v1",
            "journal": self.journal_path,
            "backend": self.backend,
            "started": self.started,
            "elapsed_s": round(self.elapsed_s, 3),
            "already_undone": self.already_undone,
            "counts": self.counts(),
            "restored_bytes": self.restored_bytes(),
            "ok": self.ok(),
            "ops": [op.to_dict() for op in self.ops],
        }

    def render_text(self) -> str:
        """The human report ``spacesage undo`` prints."""
        counts = self.counts()
        lines = [
            f"SpaceSage undo -- {self.journal_path}",
            f"  backend:   {self.backend}",
            f"  pending:   {len(self.ops)} operation(s), newest first"
            + (f" ({self.already_undone} already reversed)" if self.already_undone else ""),
            "",
        ]
        if not self.ops:
            lines.append("  nothing to undo: every operation in this journal is already reversed")
        for op in self.ops:
            lines.append(op.line())
            for step in op.steps:
                lines.append(step.line())
        lines.append("")
        lines.append(
            "summary: "
            f"{counts['done']} reversed, {counts['skipped']} skipped, "
            f"{counts['blocked']} blocked, {counts['failed']} failed -- "
            f"{format_bytes(self.restored_bytes())} restored"
        )
        return "\n".join(lines) + "\n"
