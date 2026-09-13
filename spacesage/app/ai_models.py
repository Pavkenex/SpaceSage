"""The app's AI seam: one service, one store of suggestions (design §9, §10).

The engine (:mod:`spacesage.ai`) does the work -- payloads, guardrails, cache,
metering.  This module is what the widgets talk to instead of the engine:

:class:`AIService`
    owns the loaded ``ai.toml``, the engine built from it, the status the screens
    render and the one :class:`AIStore` the list reads.  Reloading a changed
    configuration is a method, not a restart.
:class:`AIStore`
    what the AI answered, keyed by path: a suggestion per row (the list's
    suggested-solution column) and, when it was asked, a classification (which
    carries the tier an "apply as rule" needs).  It is *display state*: nothing
    in it is executable, and the plan generator never reads it.
:class:`AISuggestion`
    one answer in display form: the action, the reason, the confidence, the
    alternatives, and where it came from (provider + model, so a row can say
    *rule* or *AI* about its own verdict).

The rendering helpers at the bottom turn engine/outcome objects into the exact
sentences the screens show (estimates, privacy notes, error lines) so the wording
lives in one place, in one voice, and the tests can assert on it.
"""

from __future__ import annotations

import time
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any

from PySide6.QtCore import QObject, Signal

from spacesage.ai import (
    AIConfig,
    AIEngine,
    AIError,
    AIStatus,
    BatchPlan,
    CheckResult,
    Classification,
    ItemFacts,
    ProviderConfig,
    Suggestion,
    prompts,
)
from spacesage.app import icons

ACTIONS_WITHOUT_TIER = frozenset({"NO_ACTION", "KEEP"})
"""AI actions that carry no tier: they are decisions, not work items."""

AI_TONE = "info"
"""Badge tone of anything the AI produced (never a tier colour: it is not a rule)."""

SOURCE_RULE = "rule"
"""Provenance of a verdict the deterministic engine produced."""

SOURCE_AI = "ai"
"""Provenance of a verdict the AI produced -- always a suggestion, never a plan."""


@dataclass(frozen=True)
class AISuggestion:
    """One AI verdict for one item, in the form the screens render."""

    key: str
    """Case-folded comparison key of the path (what the list/model speaks)."""

    path: str
    use_case: str
    """``suggest`` or ``classify`` -- what produced this answer."""

    action: str
    """The engine's action vocabulary (``DELETE_QUARANTINE``, ``KEEP``, ...)."""

    label: str
    why: str
    confidence: float
    provider: str = ""
    model: str = ""
    category: str = ""
    tier: str = ""
    side_effects: str = ""
    alternatives: tuple[str, ...] = ()
    native: str | None = None
    cached: bool = False
    at: float = 0.0
    """When the answer arrived (session-relative display only)."""

    raw: Any = None
    """The engine object behind this view (a ``Suggestion`` or a ``Classification``).

    Kept so *Apply as rule…* can hand the promotion engine exactly what the model
    wrote, rather than a display string round-tripped back into a draft.
    """

    # -- construction ------------------------------------------------------ #

    @classmethod
    def from_suggestion(
        cls,
        suggestion: Suggestion,
        *,
        provider: str = "",
        model: str = "",
        cached: bool = False,
        at: float | None = None,
    ) -> AISuggestion:
        """Display form of an engine :class:`~spacesage.ai.prompts.Suggestion`."""
        return cls(
            key=_key(suggestion.path),
            path=suggestion.path,
            use_case="suggest",
            action=suggestion.engine_action,
            label=suggestion.label,
            why=suggestion.why,
            confidence=suggestion.confidence,
            provider=provider,
            model=model,
            side_effects=suggestion.side_effects,
            alternatives=tuple(suggestion.alternatives),
            native=suggestion.native,
            cached=cached,
            at=time.time() if at is None else at,
            raw=suggestion,
        )

    @classmethod
    def from_classification(
        cls,
        classification: Classification,
        *,
        provider: str = "",
        model: str = "",
        cached: bool = False,
        at: float | None = None,
    ) -> AISuggestion:
        """Display form of an engine :class:`~spacesage.ai.prompts.Classification`."""
        return cls(
            key=_key(classification.path),
            path=classification.path,
            use_case="classify",
            action=classification.action,
            label=prompts.ACTION_LABELS.get(classification.action, classification.action),
            why=classification.rationale,
            confidence=classification.confidence,
            provider=provider,
            model=model,
            category=classification.category,
            tier=classification.tier,
            native=classification.native,
            cached=cached,
            at=time.time() if at is None else at,
            raw=classification,
        )

    # -- display ----------------------------------------------------------- #

    @property
    def provenance(self) -> str:
        """``AI · ollama / llama3.2`` -- who produced this verdict."""
        where = " / ".join(part for part in (self.provider, self.model) if part)
        return f"AI · {where}" if where else "AI"

    @property
    def is_classification(self) -> bool:
        """True when this is a ``classify`` answer (it carries a tier)."""
        return self.use_case == "classify"

    @property
    def no_action(self) -> bool:
        """True for the explicit "nothing should be done" verdict."""
        return self.action in ACTIONS_WITHOUT_TIER

    @property
    def is_destructive(self) -> bool:
        """True for the actions a promotion has to be careful about."""
        return self.action in {"DELETE_QUARANTINE", "COMPRESS_NTFS"}

    def detail_lines(self) -> tuple[str, ...]:
        """The pane's lines about this answer (side effects, alternatives, vendor)."""
        lines: list[str] = []
        if self.side_effects:
            lines.append(f"Side effects: {self.side_effects}")
        if self.alternatives:
            lines.append(f"Alternatives: {', '.join(self.alternatives)}")
        if self.native:
            lines.append(f"Vendor tool: {self.native}")
        return tuple(lines)

    def to_dict(self) -> dict[str, Any]:
        """JSON-ready view (used by the tests and the debug panel)."""
        return {
            "path": self.path,
            "use_case": self.use_case,
            "action": self.action,
            "label": self.label,
            "why": self.why,
            "confidence": self.confidence,
            "provider": self.provider,
            "model": self.model,
            "category": self.category,
            "tier": self.tier,
            "side_effects": self.side_effects,
            "alternatives": list(self.alternatives),
            "native": self.native,
            "cached": self.cached,
        }


def tone(entry: AISuggestion) -> str:
    """Badge tone of an AI verdict: the action's semantics, ``NO_ACTION`` muted.

    The tone describes the *action*, not who proposed it -- the provenance chip
    beside it says that -- so an AI delete reads as dangerous as a rule's delete
    does.  What the AI never gets is a rule's authority: its answer is advice
    until a human promotes it (design §10).
    """
    if entry.no_action:
        return "muted"
    # One table for both vocabularies (``icons.ACTION_TONES``); the AI's only
    # difference is its fallback -- an action it invents is news, not silence.
    return icons.ACTION_TONES.get(entry.action, AI_TONE)


@dataclass(frozen=True)
class Notice:
    """One inline message, in the shape :class:`~spacesage.app.widgets.WarningBanner` renders.

    The AI's failures are states a screen has to *show* -- an unreachable provider
    is not an exception, it is a message under the control that asked -- and the
    app already has one component for "a severity, a sentence, the paths it is
    about".  Reusing it keeps a failed AI call looking like every other warning.
    """

    severity: str
    message: str
    paths: tuple[str, ...] = ()

    @property
    def label(self) -> str:
        """The banner's badge text."""
        return {"danger": "Failed", "warning": "Careful", "info": "Note"}.get(self.severity, "Note")

    @classmethod
    def failure(cls, message: str, *, paths: tuple[str, ...] = ()) -> Notice:
        """A failed AI call, as the screen reports it."""
        return cls(severity="danger", message=message, paths=paths)


class AIStore(QObject):
    """What the AI answered, per row, for the screens to render.

    Two maps on purpose: a ``suggest`` answer is a course of action (what the
    suggested-solution column shows), a ``classify`` answer is a label *and* a
    tier (what "apply as rule" needs).  A row can have either or both; the pane
    shows them side by side and the list prefers the suggestion.
    """

    changed = Signal(object)
    """The keys that changed (an empty tuple means "everything")."""

    def __init__(self, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self._suggestions: dict[str, AISuggestion] = {}
        self._classifications: dict[str, AISuggestion] = {}

    # -- writing ----------------------------------------------------------- #

    def put(self, entry: AISuggestion, *, replace_existing: bool = True) -> bool:
        """Store one answer; returns whether anything changed."""
        target = self._classifications if entry.is_classification else self._suggestions
        if not replace_existing and entry.key in target:
            return False
        target[entry.key] = entry
        self.changed.emit((entry.key,))
        return True

    def extend(self, entries: Iterable[AISuggestion]) -> tuple[str, ...]:
        """Store several answers, emitting once with every key that changed."""
        keys: list[str] = []
        for entry in entries:
            target = self._classifications if entry.is_classification else self._suggestions
            target[entry.key] = entry
            keys.append(entry.key)
        if keys:
            self.changed.emit(tuple(keys))
        return tuple(keys)

    def drop(self, keys: Iterable[str]) -> tuple[str, ...]:
        """Forget the answers for ``keys`` (a promoted row is no longer undecided)."""
        dropped: list[str] = []
        for key in keys:
            removed = self._suggestions.pop(key, None)
            if self._classifications.pop(key, None) is not None or removed is not None:
                dropped.append(key)
        if dropped:
            self.changed.emit(tuple(dropped))
        return tuple(dropped)

    def clear(self) -> None:
        """Forget everything (a fresh analysis replaces the index)."""
        if not self._suggestions and not self._classifications:
            return
        self._suggestions.clear()
        self._classifications.clear()
        self.changed.emit(())

    def mark_cached(self, keys: Iterable[str]) -> tuple[str, ...]:
        """Mark answers as cache hits (the run they came from reported so)."""
        marked: list[str] = []
        for key in keys:
            folded = _key(key)
            for table in (self._suggestions, self._classifications):
                entry = table.get(folded)
                if entry is not None and not entry.cached:
                    table[folded] = replace(entry, cached=True)
                    marked.append(folded)
        if marked:
            self.changed.emit(tuple(marked))
        return tuple(marked)

    # -- reading ----------------------------------------------------------- #

    def suggestion(self, key: str) -> AISuggestion | None:
        """The ``suggest`` answer for a row key or path (case-folded)."""
        return self._suggestions.get(_key(key))

    def classification(self, key: str) -> AISuggestion | None:
        """The ``classify`` answer for a row key or path (case-folded)."""
        return self._classifications.get(_key(key))

    def verdict(self, key: str) -> AISuggestion | None:
        """The best answer for a row: the suggestion, else the classification."""
        folded = _key(key)
        return self._suggestions.get(folded) or self._classifications.get(folded)

    def has(self, key: str) -> bool:
        """True when either kind of answer is stored for the row."""
        folded = _key(key)
        return folded in self._suggestions or folded in self._classifications

    def keys(self) -> tuple[str, ...]:
        """Every key with an answer."""
        return tuple(dict.fromkeys((*self._suggestions, *self._classifications)))

    def entries(self) -> tuple[AISuggestion, ...]:
        """Every stored answer, suggestions first."""
        return (*self._suggestions.values(), *self._classifications.values())

    def __len__(self) -> int:
        return len(self.keys())


class AIService(QObject):
    """The app's AI layer as one object: config, engine, store, meter.

    Rebuilt rather than mutated: editing providers in Settings writes the file and
    calls :meth:`save`, which re-reads it and drops the engine, so nothing the
    widgets hold can go stale.  A broken configuration file is a *state*, not a
    crash: the status then says what is wrong and every action reports it inline.
    """

    statusChanged = Signal(object)
    """The new :class:`~spacesage.ai.AIStatus` (after a reload or a settings write)."""

    configChanged = Signal(object)
    """The new :class:`~spacesage.ai.AIConfig` (after a settings write)."""

    def __init__(
        self,
        config: AIConfig | None = None,
        *,
        env: Mapping[str, str] | None = None,
        config_path: str | Path | None = None,
        store: AIStore | None = None,
        engine_kwargs: Mapping[str, Any] | None = None,
        parent: QObject | None = None,
    ) -> None:
        super().__init__(parent)
        self._env = env
        self._path = Path(config_path) if config_path is not None else None
        self._engine_kwargs: dict[str, Any] = dict(engine_kwargs or {})
        self._error: AIError | None = None
        self._config = config if config is not None else self._load()
        if config is not None and config.source:
            self._path = Path(config.source)
        self._engine: AIEngine | None = None
        self._store = store if store is not None else AIStore(self)

    # -- configuration ----------------------------------------------------- #

    def _load(self) -> AIConfig:
        """Read the configuration, keeping a broken file as a reportable state."""
        try:
            self._error = None
            return AIConfig.load(self._path, env=self._env)
        except AIError as exc:
            self._error = exc
            return AIConfig()

    @property
    def store(self) -> AIStore:
        """The suggestions the screens render."""
        return self._store

    @property
    def error(self) -> AIError | None:
        """The configuration error, when the file could not be read."""
        return self._error

    def config(self) -> AIConfig:
        """The configuration in force."""
        return self._config

    def engine(self) -> AIEngine:
        """The engine built from the configuration (created once per config)."""
        if self._engine is None:
            self._engine = AIEngine(self._config, env=self._env, **self._engine_kwargs)
        return self._engine

    def engine_for(self, provider: str | None = None) -> AIEngine:
        """An engine pinned to one provider (Settings' per-row Test connection).

        Not cached: it is a one-off probe of a provider that is usually not the
        default one, and a fresh engine cannot carry a stale selection anywhere.
        """
        return AIEngine(self._config, provider=provider, env=self._env, **self._engine_kwargs)

    def env(self) -> Mapping[str, str] | None:
        """The environment this service reads keys and paths from (tests inject one)."""
        return self._env

    def reload(self) -> AIStatus:
        """Re-read the configuration file and rebuild the engine."""
        self._config = self._load()
        self._engine = None
        status = self.status()
        self.statusChanged.emit(status)
        return status

    def save(self, config: AIConfig) -> Path:
        """Write ``config`` to the user's file and adopt it (Settings' one write).

        The file is the user's, so it is written where it lives -- the configured
        path, or the platform's default; nothing else is touched.
        """
        target = config.save(self._path, env=self._env)
        self._config = config
        if self._path is None:
            self._path = target
            self._config = AIConfig.load(target, env=self._env)
        self._engine = None
        self._error = None
        self.configChanged.emit(self._config)
        self.statusChanged.emit(self.status())
        return target

    def path(self) -> Path | None:
        """The configuration file this service reads and writes (``None``: default)."""
        return self._path

    # -- status ------------------------------------------------------------ #

    def status(self, *, with_cache: bool = True) -> AIStatus:
        """Whether the layer can work right now, and with what."""
        if self._error is not None:
            return AIStatus(enabled=False, ready=False, reason=str(self._error))
        return self.engine().status(with_cache=with_cache)

    def ready(self) -> bool:
        """True when a configured provider could answer a call."""
        return self.status().ready

    def meter_snapshot(self) -> Any:
        """Tokens, calls and cost so far this session."""
        return self.engine().meter_snapshot()

    def meter_text(self) -> str:
        """The status bar's cost meter (``""`` before anything was asked)."""
        return meter_text(self.meter_snapshot())


# --------------------------------------------------------------------------- #
# Rendering helpers (one voice for every AI sentence the app shows)
# --------------------------------------------------------------------------- #


def _key(value: str) -> str:
    """Fold a path or a row key into the comparison key the list uses."""
    from spacesage import candidates

    return candidates.path_key(value)


def state_line(status: AIStatus) -> str:
    """One line about the AI layer for the status bar and the screens."""
    if not status.enabled:
        return "AI off"
    if not status.ready:
        return f"AI not ready: {status.reason}" if status.reason else "AI not ready"
    return f"AI: {status.label}"


def readiness_hint(status: AIStatus, *, configured: bool) -> str:
    """Why an AI action is unavailable, in the words of a tooltip."""
    if not configured:
        return "AI is off: add a provider in Settings to use suggestions, explanations and review"
    if not status.ready:
        return f"AI is not ready: {status.reason}"
    return ""


def privacy_lines(status: AIStatus) -> tuple[str, ...]:
    """What leaves the machine when a call is made (shown before a batch fill)."""
    lines = [
        "Sends facts about each listed item only: the path, its size and age, and the "
        "verdict the rules already reached. File contents and the index never leave "
        "this machine.",
    ]
    where = " / ".join(part for part in (status.provider, status.model) if part)
    if where:
        lines.append(f"Provider: {where} · {status.base_url}" if status.base_url else where)
    if status.redact_paths:
        lines.append(
            "Paths are replaced by tokens before the request is built (redact_paths); the "
            "mapping stays local. Advice quality drops with the names -- local-only mode "
            "keeps both."
        )
    if status.local_only:
        lines.append(
            "Local-only mode: a non-loopback endpoint is refused before any socket is opened."
        )
    return tuple(lines)


def estimate_lines(plan: BatchPlan, status: AIStatus) -> tuple[str, ...]:
    """The pre-flight estimate of a batch fill, as lines the dialog shows.

    The estimate is the engine's own (:meth:`spacesage.ai.AIEngine.plan_batches`):
    the calls it will make, the cache hits it already knows about, and what the
    rest would cost at the provider's prices.
    """
    lines: list[str] = []
    estimate = plan.estimate
    if estimate is not None:
        rendered = estimate.render()
        if estimate.calls == 0:
            lines.append(rendered.capitalize() + ".")
        else:
            lines.append(f"{plan.items:,} row(s): {rendered}.")
    elif not status.ready:
        lines.append(state_line(status) + ".")
    if plan.truncated:
        lines.append(
            f"{plan.truncated:,} more row(s) are past this run's limit ({plan.items:,} items "
            "per fill); filter the list, or raise max_items in the configuration."
        )
    return tuple(lines)


def meter_text(snapshot: Any) -> str:
    """The cost meter's line (``12.3k tokens, ~$0.0042, 2 cached``)."""
    if snapshot is None:
        return ""
    if not snapshot.calls and not snapshot.cache_hits:
        return ""
    return str(snapshot.render())


def error_text(error: object | None, *, fallback: str = "") -> str:
    """The one-line explanation of a failed call (an :class:`AIError`), hint included."""
    if error is None:
        return fallback
    if not isinstance(error, AIError):
        return str(error) or fallback
    message = f"{error.message} ({error.code})" if error.code else str(error)
    return f"{message} — {error.hint}" if error.hint else message


def outcome_error_text(outcome: Any, *, fallback: str = "") -> str:
    """The failure of an outcome object (:class:`Outcome`), as one line."""
    error = getattr(outcome, "error", None)
    return error_text(error if isinstance(error, AIError) else None, fallback=fallback)


def check_lines(result: CheckResult, *, limit: int = 6) -> tuple[str, ...]:
    """What ``Test connection`` reports: state, latency, models, warnings."""
    lines: list[str] = []
    if not result.ok:
        lines.append(error_text(result.error, fallback="the provider did not answer"))
        return tuple(lines)
    latency = f"{result.latency_s * 1000:.0f} ms" if result.latency_s is not None else "?"
    lines.append(f"Answered in {latency} · {len(result.models)} model(s) offered")
    names = [info.id for info in result.models]
    if result.model:
        listed = "listed" if result.model_listed else "not in /models"
        lines.append(f"Model {result.model} ({listed})")
    if names:
        shown = ", ".join(names[:limit])
        rest = f" (+{len(names) - limit} more)" if len(names) > limit else ""
        lines.append(f"Models: {shown}{rest}")
    lines.extend(f"Warning: {warning}" for warning in result.warnings)
    if result.hint:
        lines.append(result.hint)
    return tuple(lines)


def provider_line(provider: ProviderConfig, *, default: bool = False) -> str:
    """One provider as a list line (``ollama · Local server · http://... · llama3.2``)."""
    parts = [provider.name, provider.title] if provider.title != provider.name else [provider.name]
    if provider.base_url:
        parts.append(provider.base_url)
    if provider.model:
        parts.append(provider.model)
    if default:
        parts.append("default")
    if provider.is_local:
        parts.append("local")
    return " · ".join(parts)


def batch_items(rows: Sequence[Any]) -> tuple[ItemFacts, ...]:
    """The facts a batch fill sends for a list of opportunity rows."""
    return tuple(prompts.facts_from_opportunity(row) for row in rows)


def summarise_run(
    *, filled: int, missing: int, rejected: int, failures: int, cache_hits: int, cancelled: bool
) -> str:
    """The toast after a batch fill: what was answered, what was not, and why."""
    parts = [f"{filled:,} row(s) filled"]
    if cache_hits:
        parts.append(f"{cache_hits:,} from the cache")
    if missing:
        parts.append(f"{missing:,} without an answer")
    if rejected:
        parts.append(f"{rejected:,} answer(s) refused (paths that were not sent)")
    if failures:
        parts.append(f"{failures:,} batch(es) failed")
    if cancelled:
        parts.append("cancelled")
    return ", ".join(parts)


def run_summary(outcome: Any, *, kind: str = "suggest") -> tuple[str, str]:
    """A finished fill as ``(one line, toast tone)``.

    The counts come from the outcome itself: what the engine asked (``missing``
    is what the model did not answer, ``rejected`` what it answered about a path
    that was never sent), so a cancelled or half-failed run reads as what it was
    rather than as a clean success.
    """
    answered = len(getattr(outcome, "by_path", None) or {})
    missing = len(tuple(getattr(outcome, "missing", ()) or ()))
    rejected = len(tuple(getattr(outcome, "rejected", ()) or ()))
    failure_count = int(getattr(outcome, "failures", 0) or 0)
    cancelled = bool(getattr(outcome, "cancelled", False))
    text = summarise_run(
        filled=answered,
        missing=missing,
        rejected=rejected,
        failures=failure_count,
        cache_hits=int(getattr(outcome, "cache_hits", 0) or 0),
        cancelled=cancelled,
    )
    if kind == "classify":
        text = text.replace("row(s) filled", "row(s) classified")
    clean = (
        bool(getattr(outcome, "ok", True)) and not failure_count and not missing and not cancelled
    )
    return text, ("success" if clean else "warning")


def state_tooltip(status: AIStatus) -> str:
    """Everything the AI badge cannot say in three words (hover text)."""
    lines = [status.label]
    if status.provider:
        lines.append(f"{status.provider_title or status.provider} · {status.base_url}")
    if status.model:
        lines.append(f"Model: {status.model}")
    if status.cache_enabled and status.cache_dir:
        lines.append(f"Cache: {status.cache_entries:,} answer(s) in {status.cache_dir}")
    else:
        lines.append("Cache: off (every run would call the provider again)")
    lines.append(f"API key: {'found' if status.key_present else 'not set (or not needed)'}")
    if status.redact_paths:
        lines.append("Paths are replaced by tokens before anything leaves this machine")
    if status.local_only:
        lines.append("Local-only: only a loopback endpoint may be called")
    lines.extend(status.warnings)
    if not status.ready and status.reason:
        lines.append(f"Not ready: {status.reason}")
    if status.config_path:
        lines.append(f"Config: {status.config_path}")
    return "\n".join(lines)


def provider_short(status: AIStatus) -> str:
    """A provider worth naming in a sentence ("stub" / "the provider")."""
    return status.provider or "the provider"


def progress_line(progress: Any) -> str:
    """One batch boundary in one line (the engine's own render)."""
    render = getattr(progress, "render", None)
    return str(render()) if callable(render) else ""


def explanation_render(outcome: Any) -> tuple[str, str]:
    """A finished explanation as ``(text, stage)`` for the pane's card.

    The prose keeps the model's paragraphs; the risks and the alternatives the
    prompt asked for follow as lists; the stage line says who wrote it and what it
    cost (or that it came from the cache).
    """
    explanation: Any = getattr(outcome, "explanation", None)
    text = str(getattr(explanation, "explanation", "") or "")
    raw_risks: Any = getattr(explanation, "risks", None)
    raw_alternatives: Any = getattr(explanation, "alternatives", None)
    risks = tuple(str(item) for item in (raw_risks or ()))
    alternatives = tuple(str(item) for item in (raw_alternatives or ()))
    if risks:
        text = f"{text}\n\nRisks:\n" + "\n".join(f"· {risk}" for risk in risks)
    if alternatives:
        text = f"{text}\n\nAlternatives:\n" + "\n".join(f"· {item}" for item in alternatives)
    provider = str(getattr(outcome, "provider", "") or "")
    model = str(getattr(outcome, "model", "") or "")
    usage = getattr(outcome, "usage", None)
    tokens = int(getattr(usage, "total_tokens", 0) or 0) if usage is not None else 0
    latency = float(getattr(outcome, "latency_s", 0.0) or 0.0)
    parts = [f"{provider} / {model}" if provider else "the provider"]
    if bool(getattr(outcome, "cache_hit", False)):
        parts.append("from the cache")
    else:
        if tokens:
            parts.append(f"{tokens:,} tokens")
        if latency:
            parts.append(f"{latency:,.1f} s")
    return text, " · ".join(parts)


__all__ = [
    "AI_TONE",
    "SOURCE_AI",
    "SOURCE_RULE",
    "AIService",
    "AIStore",
    "AISuggestion",
    "Notice",
    "batch_items",
    "check_lines",
    "error_text",
    "estimate_lines",
    "explanation_render",
    "meter_text",
    "outcome_error_text",
    "privacy_lines",
    "progress_line",
    "provider_line",
    "provider_short",
    "readiness_hint",
    "run_summary",
    "state_line",
    "state_tooltip",
    "summarise_run",
    "tone",
]
