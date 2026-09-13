"""What an AI call returns: outcomes, status and batch bookkeeping.

Every use case returns one frozen outcome object - never a bare dict, never an
exception for the expected failures (an unreachable provider is an *outcome* with
``ok=False`` and a coded error, because the UI has to render it inline and the
batch runner has to keep going for the batches that may still work).  The
outcomes carry the accounting the UI shows: provider, model, latency, tokens,
cost, whether the answer came from the cache, how many calls it took, and which
items were refused by the dataset lock.
"""

from __future__ import annotations

import time
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

from spacesage.ai.client import ModelInfo, Usage
from spacesage.ai.errors import AIError
from spacesage.ai.prompts import (
    Annotation,
    Classification,
    Explanation,
    PlanSummary,
    Suggestion,
)

# --------------------------------------------------------------------------- #
# Status & diagnostics
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class AIStatus:
    """Whether the AI layer can work right now, and with what (status bar)."""

    enabled: bool
    ready: bool
    reason: str = ""
    provider: str | None = None
    provider_title: str = ""
    model: str = ""
    base_url: str = ""
    local: bool = False
    key_present: bool = False
    streaming: bool = True
    redact_paths: bool = False
    local_only: bool = False
    cache_enabled: bool = True
    cache_dir: str = ""
    cache_entries: int = 0
    pricing_known: bool = False
    config_path: str | None = None
    warnings: tuple[str, ...] = ()

    @property
    def label(self) -> str:
        """Short status-bar text (``Ollama (local) / llama3.2, redacted``)."""
        if not self.enabled:
            return "AI off"
        if not self.ready:
            return f"AI not ready: {self.reason}" if self.reason else "AI not ready"
        parts = [f"{self.provider} / {self.model}"]
        if self.local:
            parts.append("local")
        if self.redact_paths:
            parts.append("redacted")
        if self.local_only:
            parts.append("local-only")
        return ", ".join(parts)

    def to_dict(self) -> dict[str, Any]:
        """JSON-ready view."""
        return {
            "enabled": self.enabled,
            "ready": self.ready,
            "reason": self.reason,
            "provider": self.provider,
            "provider_title": self.provider_title,
            "model": self.model,
            "base_url": self.base_url,
            "local": self.local,
            "key_present": self.key_present,
            "streaming": self.streaming,
            "redact_paths": self.redact_paths,
            "local_only": self.local_only,
            "cache_enabled": self.cache_enabled,
            "cache_dir": self.cache_dir,
            "cache_entries": self.cache_entries,
            "pricing_known": self.pricing_known,
            "config_path": self.config_path,
            "warnings": list(self.warnings),
            "label": self.label,
        }


@dataclass(frozen=True)
class CheckResult:
    """The result of ``Test connection`` / ``spacesage ai check``."""

    ok: bool
    provider: str = ""
    model: str = ""
    base_url: str = ""
    latency_s: float | None = None
    models: tuple[ModelInfo, ...] = ()
    model_listed: bool | None = None
    error: AIError | None = None
    warnings: tuple[str, ...] = ()
    hint: str = ""

    def render(self) -> str:
        """A few lines a dialog or the terminal can show as-is."""
        lines = [f"provider: {self.provider} ({self.base_url})"]
        if not self.ok:
            lines.append(f"state: unavailable - {self.error or 'unknown error'}")
            if self.hint:
                lines.append(f"hint: {self.hint}")
            return "\n".join(lines)
        latency = f"{self.latency_s:.2f}s" if self.latency_s is not None else "?"
        lines.append(f"state: ok ({latency})")
        lines.append(
            f"model: {self.model}" + ("" if self.model_listed is not False else " (not in /models)")
        )
        lines.append(f"models offered: {len(self.models)}")
        for warning in self.warnings:
            lines.append(f"warning: {warning}")
        return "\n".join(lines)

    def to_dict(self) -> dict[str, Any]:
        """JSON-ready view."""
        return {
            "ok": self.ok,
            "provider": self.provider,
            "model": self.model,
            "base_url": self.base_url,
            "latency_s": None if self.latency_s is None else round(self.latency_s, 3),
            "models": [info.to_dict() for info in self.models],
            "model_listed": self.model_listed,
            "error": None if self.error is None else self.error.to_dict(),
            "warnings": list(self.warnings),
            "hint": self.hint,
            "rendered": self.render(),
        }


# --------------------------------------------------------------------------- #
# Outcomes
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class Outcome:
    """What every use case returns: the answer plus what it cost to get it."""

    ok: bool = True
    error: AIError | None = None
    provider: str = ""
    model: str = ""
    use_case: str = ""
    usage: Usage = field(default_factory=Usage)
    latency_s: float = 0.0
    cache_hit: bool = False
    cost_usd: float | None = None
    calls: int = 0
    estimated: bool = False

    @property
    def error_code(self) -> str:
        """The failure code (``""`` when the outcome is ok)."""
        return self.error.code if self.error is not None else ""

    @property
    def error_dict(self) -> dict[str, Any] | None:
        """The failure as JSON (``None`` when the outcome is ok)."""
        return None if self.error is None else self.error.to_dict()

    def _common(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "error": self.error_dict,
            "provider": self.provider,
            "model": self.model,
            "use_case": self.use_case,
            "usage": self.usage.to_dict(),
            "latency_s": round(self.latency_s, 3),
            "cache_hit": self.cache_hit,
            "cost_usd": None if self.cost_usd is None else round(self.cost_usd, 6),
            "calls": self.calls,
        }

    def to_dict(self) -> dict[str, Any]:
        """JSON-ready view."""
        return self._common()


@dataclass(frozen=True)
class SuggestOutcome(Outcome):
    """Suggestions for one item or a whole batch."""

    suggestions: tuple[Suggestion, ...] = ()
    by_path: Mapping[str, Suggestion] = field(default_factory=dict)
    rejected: tuple[str, ...] = ()
    """Paths the model invented: reported, never used."""
    missing: tuple[str, ...] = ()
    """Items the model did not answer for (the UI shows them as unfilled)."""
    notes: str | None = None
    batches: int = 0
    cache_hits: int = 0
    failures: int = 0
    cancelled: bool = False
    stopped: str = ""
    """Why the run stopped early (a fatal code), ``""`` when it ran to the end."""
    estimate: Any = None
    """The :class:`~spacesage.ai.guardrails.CostEstimate` this run was planned with."""

    def for_path(self, path: str) -> Suggestion | None:
        """The suggestion for ``path`` (case-folded lookup)."""
        from spacesage import candidates

        return self.by_path.get(candidates.path_key(path))

    def to_dict(self) -> dict[str, Any]:
        """JSON-ready view."""
        data = self._common()
        data.update(
            {
                "suggestions": [item.to_dict() for item in self.suggestions],
                "rejected": list(self.rejected),
                "missing": list(self.missing),
                "notes": self.notes,
                "batches": self.batches,
                "cache_hits": self.cache_hits,
                "failures": self.failures,
                "cancelled": self.cancelled,
                "stopped": self.stopped,
                "estimate": None if self.estimate is None else self.estimate.to_dict(),
            }
        )
        return data


@dataclass(frozen=True)
class ClassifyOutcome(Outcome):
    """Classifications for a batch of ambiguous entries."""

    classifications: tuple[Classification, ...] = ()
    by_path: Mapping[str, Classification] = field(default_factory=dict)
    rejected: tuple[str, ...] = ()
    missing: tuple[str, ...] = ()
    notes: str | None = None
    batches: int = 0
    cache_hits: int = 0
    failures: int = 0
    cancelled: bool = False
    stopped: str = ""
    """Why the run stopped early (a fatal code), ``""`` when it ran to the end."""
    estimate: Any = None

    def for_path(self, path: str) -> Classification | None:
        """The classification for ``path`` (case-folded lookup)."""
        from spacesage import candidates

        return self.by_path.get(candidates.path_key(path))

    def to_dict(self) -> dict[str, Any]:
        """JSON-ready view."""
        data = self._common()
        data.update(
            {
                "classifications": [item.to_dict() for item in self.classifications],
                "rejected": list(self.rejected),
                "missing": list(self.missing),
                "notes": self.notes,
                "batches": self.batches,
                "cache_hits": self.cache_hits,
                "failures": self.failures,
                "cancelled": self.cancelled,
                "stopped": self.stopped,
                "estimate": None if self.estimate is None else self.estimate.to_dict(),
            }
        )
        return data


@dataclass(frozen=True)
class ExplainOutcome(Outcome):
    """A deep explanation of a selection (streamed while it was written)."""

    explanation: Explanation | None = None
    text: str = ""
    """The complete answer as the model wrote it (the streamed prose is a subset)."""

    def to_dict(self) -> dict[str, Any]:
        """JSON-ready view."""
        data = self._common()
        data.update(
            {
                "explanation": None if self.explanation is None else self.explanation.to_dict(),
                "text": self.text,
            }
        )
        return data


@dataclass(frozen=True)
class ReviewOutcome(Outcome):
    """Severity-tagged risk annotations for a plan."""

    annotations: tuple[Annotation, ...] = ()
    summary: str = ""
    rejected: tuple[str, ...] = ()
    """Action ids the model annotated that the plan does not have."""

    def to_dict(self) -> dict[str, Any]:
        """JSON-ready view."""
        data = self._common()
        data.update(
            {
                "annotations": [item.to_dict() for item in self.annotations],
                "summary": self.summary,
                "rejected": list(self.rejected),
            }
        )
        return data


@dataclass(frozen=True)
class SummarizeOutcome(Outcome):
    """A plain-language plan summary."""

    summary: PlanSummary | None = None

    def to_dict(self) -> dict[str, Any]:
        """JSON-ready view."""
        data = self._common()
        data.update({"summary": None if self.summary is None else self.summary.to_dict()})
        return data


# --------------------------------------------------------------------------- #
# Batch bookkeeping
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class BatchProgress:
    """One progress tick of a batch fill (the UI's determinate bar)."""

    use_case: str
    done: int
    total: int
    batches_done: int
    batches_total: int
    calls: int = 0
    cache_hits: int = 0
    failures: int = 0
    rejected: int = 0
    missing: int = 0
    cost_usd: float | None = None
    last_paths: tuple[str, ...] = ()
    message: str = ""

    @property
    def fraction(self) -> float:
        """Completion in ``0..1`` (``1.0`` when there is nothing to do)."""
        return 1.0 if self.total <= 0 else min(1.0, self.done / self.total)

    def render(self) -> str:
        """One line: ``12 / 40 items, batch 2 / 5, 1 cached``."""
        parts = [
            f"{self.done} / {self.total} items",
            f"batch {self.batches_done} / {self.batches_total}",
        ]
        if self.cache_hits:
            parts.append(f"{self.cache_hits} cached")
        if self.failures:
            parts.append(f"{self.failures} failed")
        if self.message:
            parts.append(self.message)
        return ", ".join(parts)

    def to_dict(self) -> dict[str, Any]:
        """JSON-ready view."""
        return {
            "use_case": self.use_case,
            "done": self.done,
            "total": self.total,
            "fraction": round(self.fraction, 4),
            "batches_done": self.batches_done,
            "batches_total": self.batches_total,
            "calls": self.calls,
            "cache_hits": self.cache_hits,
            "failures": self.failures,
            "rejected": self.rejected,
            "missing": self.missing,
            "cost_usd": None if self.cost_usd is None else round(self.cost_usd, 6),
            "last_paths": list(self.last_paths),
            "message": self.message,
            "rendered": self.render(),
        }


@dataclass(frozen=True)
class BatchFailure:
    """One batch that could not be filled, and why."""

    code: str
    message: str
    hint: str = ""
    paths: tuple[str, ...] = ()
    batch: int = 0

    @classmethod
    def of(cls, error: AIError, *, paths: Sequence[str], batch: int) -> BatchFailure:
        """Build a failure record from the engine's error."""
        return cls(
            code=error.code,
            message=error.message,
            hint=error.hint,
            paths=tuple(paths),
            batch=batch,
        )

    def to_dict(self) -> dict[str, Any]:
        """JSON-ready view."""
        return {
            "code": self.code,
            "message": self.message,
            "hint": self.hint,
            "paths": list(self.paths),
            "batch": self.batch,
        }


@dataclass(frozen=True)
class BatchPlan:
    """How a run will be split up, and what it will roughly cost - before running."""

    use_case: str
    batches: tuple[tuple[str, ...], ...] = ()
    """The batches as tuples of (case-folded) path keys."""
    items: int = 0
    truncated: int = 0
    """Items left out by ``max_items`` (0 when everything fits)."""
    cached_batches: int = 0
    estimate: Any = None

    @property
    def calls(self) -> int:
        """Number of requests the run will make."""
        return len(self.batches)

    def to_dict(self) -> dict[str, Any]:
        """JSON-ready view."""
        return {
            "use_case": self.use_case,
            "batches": len(self.batches),
            "items": self.items,
            "truncated": self.truncated,
            "cached_batches": self.cached_batches,
            "estimate": None if self.estimate is None else self.estimate.to_dict(),
        }


@dataclass(frozen=True)
class BatchRun:
    """The collected result of a batched fill (progress ticks + per-batch failures)."""

    use_case: str
    paths: tuple[str, ...] = ()
    """The case-folded keys of the items the run covered, in order."""
    batches: int = 0
    calls: int = 0
    cache_hits: int = 0
    failures: tuple[BatchFailure, ...] = ()
    cancelled: bool = False
    stopped: str = ""
    """Why the run stopped early (``""`` when it ran to the end)."""
    estimate: Any = None
    started_at: float = field(default_factory=time.time)

    @property
    def fully_cached(self) -> bool:
        """True when the whole run came out of the cache (nothing was sent)."""
        return self.batches > 0 and self.calls == 0 and self.cache_hits == self.batches

    def to_dict(self) -> dict[str, Any]:
        """JSON-ready view."""
        return {
            "use_case": self.use_case,
            "items": len(self.paths),
            "batches": self.batches,
            "calls": self.calls,
            "cache_hits": self.cache_hits,
            "fully_cached": self.fully_cached,
            "failures": [failure.to_dict() for failure in self.failures],
            "cancelled": self.cancelled,
            "stopped": self.stopped,
            "estimate": None if self.estimate is None else self.estimate.to_dict(),
        }


__all__ = [
    "AIStatus",
    "BatchFailure",
    "BatchPlan",
    "BatchProgress",
    "BatchRun",
    "CheckResult",
    "ClassifyOutcome",
    "ExplainOutcome",
    "Outcome",
    "ReviewOutcome",
    "SuggestOutcome",
    "SummarizeOutcome",
]
