"""The AI engine: the one object the app and the CLI talk to.

It owns the pipeline every use case runs through, in this order:

1. **scope** - which items this call may talk about (the dataset lock),
2. **payload** - the records, tokenised first when ``redact_paths`` is on,
3. **cache** - ``sha256(model + prompt + payload + dataset)``; a hit costs nothing,
4. **call** - the OpenAI-compatible client, streaming when the caller wants deltas,
5. **guards** - JSON extraction, schema validation, exactly one repair retry,
6. **locking** - the answer may only reference paths/ids that were sent,
7. **accounting** - tokens, cost and latency on the meter, the answer in the cache.

Whatever happens, the caller gets an *outcome* object: ``ok=False`` with a coded
error for an unreachable provider or a missing key, partial results for a batch
where some calls failed, and never an exception for the failures a user can fix.
The AI is suggestions only: nothing here can execute, approve or plan anything -
the engine produces text that has passed the same validation pipeline as
everything else the product shows.
"""

from __future__ import annotations

import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any, TypeVar

from spacesage import candidates
from spacesage.ai import guardrails, prompts
from spacesage.ai import promote as promote_engine
from spacesage.ai.client import (
    AIClient,
    ChatResult,
    Message,
    ModelInfo,
    Opener,
    Usage,
    http_opener,
)
from spacesage.ai.config import AIConfig, ProviderConfig
from spacesage.ai.errors import DISABLED, AIError
from spacesage.ai.prompts import Classification, ItemFacts, Suggestion
from spacesage.ai.results import (
    AIStatus,
    BatchPlan,
    BatchProgress,
    CheckResult,
    ClassifyOutcome,
    ExplainOutcome,
    Outcome,
    ReviewOutcome,
    SuggestOutcome,
    SummarizeOutcome,
)

T = TypeVar("T", bound=Outcome)

DEFAULT_BATCH_SIZE = 8
"""Items per request when neither the config nor the caller says otherwise."""

DEFAULT_MAX_ITEMS = 200
"""Items a single batch run covers (the rest are reported as not filled)."""


@dataclass(frozen=True)
class _Answer:
    """One valid answer plus what it took to get it."""

    answer: Mapping[str, Any]
    cache_hit: bool
    usage: Usage
    latency_s: float
    calls: int
    cost_usd: float | None
    redactor: prompts.PathRedactor | None = None


@dataclass(frozen=True)
class _Prepared:
    """A rendered question: the payload and everything keyed off it."""

    case_id: str
    payload: str
    dataset: str
    key: str
    redactor: prompts.PathRedactor | None = None


class AIEngine:
    """Configured, cache-aware, guardrail-enforcing access to the AI layer."""

    def __init__(
        self,
        config: AIConfig | None = None,
        *,
        provider: str | None = None,
        model: str | None = None,
        base_url: str | None = None,
        api_key_env: str | None = None,
        env: Mapping[str, str] | None = None,
        sleep: Callable[[float], None] = time.sleep,
        opener: Opener = http_opener,
        retries: int | None = None,
        cache: guardrails.ResponseCache | None = None,
        meter: guardrails.Meter | None = None,
        batch_size: int | None = None,
        max_items: int | None = None,
    ) -> None:
        base = AIConfig.load(env=env) if config is None else config
        if provider or model or base_url or api_key_env:
            base = base.with_overrides(
                provider=provider, model=model, base_url=base_url, api_key_env=api_key_env
            )
        settings: dict[str, object] = {}
        if batch_size is not None:
            settings["batch_size"] = max(1, batch_size)
        if max_items is not None:
            settings["max_items"] = max(1, max_items)
        if settings:
            base = base.with_settings(**settings)
        self.config = base
        self._provider_name = provider
        self._env = env
        self._sleep = sleep
        self._opener = opener
        # ``retries`` wins when given; otherwise the config file decides (the
        # client's own default is the last resort).
        self._retries = base.retries if retries is None else max(0, retries)
        self._client: AIClient | None = None
        self.cache = (
            cache
            if cache is not None
            else guardrails.ResponseCache(base.cache_path(env), enabled=base.cache)
        )
        self.meter = meter if meter is not None else guardrails.Meter()

    # ------------------------------------------------------------------ #
    # Wiring
    # ------------------------------------------------------------------ #

    def provider(self) -> ProviderConfig:
        """The selected provider, validated (raises a coded :class:`AIError`)."""
        if not self.config.providers:
            raise AIError(
                DISABLED,
                "no AI provider is configured",
                hint=(
                    "add one in ai.toml (see docs/ai.md), or set SPACESAGE_AI_BASE_URL "
                    "and SPACESAGE_AI_MODEL for a local server"
                ),
            )
        return self.config.provider(self._provider_name)

    def client(self) -> AIClient:
        """The HTTP client for the selected provider (created once)."""
        if self._client is None:
            provider = self.provider()
            self._client = AIClient(
                provider,
                local_only=self.config.local_only,
                env=self._env,
                sleep=self._sleep,
                opener=self._opener,
                retries=self._retries,
            )
        return self._client

    @property
    def model(self) -> str:
        """The model that will be called (``""`` when nothing is configured)."""
        try:
            return self.provider().model
        except AIError:
            return ""

    def status(self, *, with_cache: bool = True) -> AIStatus:
        """Everything the status bar and the settings screen need."""
        stats = self.cache.stats() if with_cache else guardrails.CacheStats()
        warnings: list[str] = []
        provider: ProviderConfig | None = None
        reason = self.config.reason() if not self.config.providers else ""
        try:
            provider = self.provider()
        except AIError as exc:
            reason = reason or str(exc)
        if provider is not None:
            warning = provider.key_warning(self._env)
            if warning:
                warnings.append(warning)
            if not provider.key_present(self._env) and provider.has_key_source():
                warnings.append(
                    f"no API key found for {provider.name} "
                    f"(looked at {provider.api_key_env or provider.api_key_file})"
                )
        return AIStatus(
            enabled=self.config.enabled and bool(self.config.providers),
            ready=self.config.is_ready() and (provider is not None),
            reason=reason,
            provider=None if provider is None else provider.name,
            provider_title="" if provider is None else provider.title,
            model="" if provider is None else provider.model,
            base_url="" if provider is None else provider.base_url,
            local=False if provider is None else provider.is_local,
            key_present=False if provider is None else provider.key_present(self._env),
            streaming=self.config.streaming,
            redact_paths=self.config.redact_paths,
            local_only=self.config.local_only,
            cache_enabled=self.cache.enabled,
            cache_dir=str(self.cache.directory),
            cache_entries=stats.entries,
            pricing_known=bool(
                provider is not None
                and provider.pricing_in is not None
                and provider.pricing_out is not None
            ),
            config_path=self.config.source,
            warnings=tuple(warnings),
        )

    def check(self, *, model: str | None = None) -> CheckResult:
        """``Test connection``: list models, measure latency, explain what is wrong."""
        try:
            provider = self.provider()
        except AIError as exc:
            return CheckResult(ok=False, error=exc, hint=exc.hint)
        chosen = model or provider.model
        warnings: list[str] = []
        warning = provider.key_warning(self._env)
        if warning:
            warnings.append(warning)
        started = time.monotonic()
        try:
            models = self.models()
        except AIError as exc:
            return CheckResult(
                ok=False,
                provider=provider.name,
                model=chosen,
                base_url=provider.base_url,
                latency_s=time.monotonic() - started,
                error=exc,
                warnings=tuple(warnings),
                hint=exc.hint,
            )
        latency = time.monotonic() - started
        listed = any(info.id == chosen for info in models)
        return CheckResult(
            ok=True,
            provider=provider.name,
            model=chosen,
            base_url=provider.base_url,
            latency_s=latency,
            models=models,
            model_listed=listed,
            warnings=tuple(warnings),
            hint=""
            if listed
            else f"{chosen!r} is not in the /models list; the call may still work",
        )

    def models(self, *, model: str | None = None) -> tuple[ModelInfo, ...]:
        """The provider's model list (raises a coded error when unreachable)."""
        client = self.client()
        if model is not None:
            client = AIClient(
                replace(self.provider(), model=model),
                local_only=self.config.local_only,
                env=self._env,
                sleep=self._sleep,
                opener=self._opener,
                retries=self._retries,
            )
        return client.models()

    def cache_stats(self) -> guardrails.CacheStats:
        """What the answer cache holds (with this session's hit counters)."""
        return self.cache.stats()

    def clear_cache(self) -> int:
        """Delete every cached answer; returns how many entries went away."""
        return self.cache.clear()

    def meter_snapshot(self) -> guardrails.MeterSnapshot:
        """Tokens and cost so far this session."""
        return self.meter.snapshot()

    # ------------------------------------------------------------------ #
    # Preparing a question
    # ------------------------------------------------------------------ #

    def payload_for(
        self,
        case_id: str,
        items: Sequence[ItemFacts],
        *,
        context: Mapping[str, Any] | None = None,
        redactor: prompts.PathRedactor | None = None,
    ) -> str:
        """The rendered data block for one call (also what the estimate measures)."""
        return prompts.build_payload(case_id, items, context=context, redactor=redactor)

    def cache_key_for(self, case_id: str, payload: str, *, dataset: str = "") -> str:
        """The cache key of a question (model + prompt + payload + dataset).

        Never raises: an unconfigured engine has nothing to hit, and the batch
        planner still wants to say "0 of 5 calls are cached".
        """
        case = prompts.use_case(case_id)
        try:
            provider_name = self.provider().name
        except AIError:
            provider_name = self.config.default_provider or ""
        return guardrails.ResponseCache.key(
            use_case=case.id,
            version=case.version,
            provider=provider_name,
            model=self.model,
            payload=payload,
            dataset=dataset,
        )

    def estimate(
        self,
        case_id: str,
        batches: Sequence[Sequence[ItemFacts]],
        *,
        context: Mapping[str, Any] | None = None,
        cached: int = 0,
    ) -> guardrails.CostEstimate:
        """What a list of batches will roughly cost (tokens + money)."""
        case = prompts.use_case(case_id)
        provider = self.provider()
        payloads = [self.payload_for(case_id, batch, context=context) for batch in batches]
        items = sum(len(batch) for batch in batches)
        return guardrails.estimate_batch(
            case_id=case.id,
            payloads=payloads,
            provider=provider,
            max_tokens=case.max_tokens,
            items=items,
            cached_calls=cached,
        )

    def _prepare(
        self,
        case_id: str,
        items: Sequence[ItemFacts],
        *,
        context: Mapping[str, Any] | None = None,
        dataset: str = "",
    ) -> _Prepared:
        redactor = guardrails.redactor(self.config.redact_paths)
        payload = self.payload_for(case_id, items, context=context, redactor=redactor)
        fingerprint = dataset or guardrails.ScopedDataset.of(items).fingerprint()
        key = self.cache_key_for(case_id, payload, dataset=fingerprint)
        return _Prepared(
            case_id=case_id, payload=payload, dataset=fingerprint, key=key, redactor=redactor
        )

    def cached_key_for(
        self,
        case_id: str,
        items: Sequence[ItemFacts],
        *,
        context: Mapping[str, Any] | None = None,
        dataset: str = "",
    ) -> str:
        """The cache key a call for ``items`` would use (redaction included)."""
        redactor = guardrails.redactor(self.config.redact_paths)
        payload = self.payload_for(case_id, items, context=context, redactor=redactor)
        fingerprint = dataset or guardrails.ScopedDataset.of(items).fingerprint()
        return self.cache_key_for(case_id, payload, dataset=fingerprint)

    # ------------------------------------------------------------------ #
    # The call pipeline
    # ------------------------------------------------------------------ #

    def _answer(
        self,
        prepared: _Prepared,
        *,
        stream: bool = False,
        on_delta: Callable[[str], None] | None = None,
        use_cache: bool = True,
    ) -> _Answer:
        """One guardrail-enforced call: cache, stream, validate, repair, meter, cache."""
        provider = self.provider()
        case = prompts.use_case(prepared.case_id)
        if use_cache:
            entry = self.cache.get(prepared.key)
            if entry is not None:
                self.meter.record_cache_hit()
                return _Answer(
                    answer=dict(entry.result),
                    cache_hit=True,
                    usage=_usage_from_mapping(entry.usage),
                    latency_s=0.0,
                    calls=0,
                    cost_usd=0.0,
                    redactor=_redactor_from_entry(entry),
                )
        client = self.client()
        messages = prompts.build_messages(prepared.case_id, prepared.payload)
        started = time.monotonic()
        if stream and on_delta is not None:
            result = client.chat_stream(
                messages,
                on_delta=on_delta,
                temperature=case.temperature,
                max_tokens=case.max_tokens,
            )
        else:
            result = client.chat(messages, temperature=case.temperature, max_tokens=case.max_tokens)
        latency = time.monotonic() - started
        self.meter.record(
            result.usage, pricing_in=provider.pricing_in, pricing_out=provider.pricing_out
        )
        calls = 1
        answer, errors = guardrails.read_answer(prepared.case_id, result.text)
        if errors:
            repaired = self._repair(
                client=client, case_id=prepared.case_id, result=result, errors=errors
            )
            calls += 1
            self.meter.record(
                repaired.usage, pricing_in=provider.pricing_in, pricing_out=provider.pricing_out
            )
            answer, errors = guardrails.read_answer(prepared.case_id, repaired.text)
            if errors:
                raise guardrails.repair_failure(prepared.case_id, repaired.text, errors)
            result = repaired
        if use_cache:
            self.cache.put(
                guardrails.CacheEntry(
                    key=prepared.key,
                    created=time.time(),
                    provider=provider.name,
                    model=result.model,
                    use_case=case.id,
                    dataset=prepared.dataset,
                    redacted=prepared.redactor is not None,
                    result=answer,
                    usage=result.usage.to_dict(),
                    tokens=prepared.redactor.mapping() if prepared.redactor is not None else {},
                )
            )
        cost = _cost_of(result.usage, provider)
        return _Answer(
            answer=answer,
            cache_hit=False,
            usage=result.usage,
            latency_s=latency,
            calls=calls,
            cost_usd=cost,
            redactor=prepared.redactor,
        )

    def _repair(
        self,
        *,
        client: AIClient,
        case_id: str,
        result: ChatResult,
        errors: Sequence[str],
    ) -> ChatResult:
        """Exactly one repair round-trip, carrying the validator's complaints."""
        case = prompts.use_case(case_id)
        repair = prompts.build_repair_message(previous=result.text, errors=errors, case_id=case_id)
        return client.chat(
            (Message(role="system", content=case.system), repair),
            temperature=0.0,
            max_tokens=case.max_tokens,
        )

    # ------------------------------------------------------------------ #
    # Use cases
    # ------------------------------------------------------------------ #

    def suggest_batch(
        self,
        items: Sequence[ItemFacts],
        *,
        context: Mapping[str, Any] | None = None,
        use_cache: bool = True,
    ) -> SuggestOutcome:
        """One batch: suggestions for every item sent."""
        if not items:
            return SuggestOutcome(
                ok=True, use_case="suggest", provider=self._safe_provider_name(), model=self.model
            )
        scope = guardrails.ScopedDataset.of(items)
        try:
            prepared = self._prepare("suggest", items, context=context)
            call = self._answer(prepared, use_cache=use_cache)
            suggestions = prompts.parse_suggestions(call.answer)
        except AIError as exc:
            return self._failed(exc, SuggestOutcome, use_case="suggest")
        kept, rejected, by_path = _lock_suggestions(suggestions, scope, call.redactor)
        missing = tuple(item.path for item in items if item.key() not in by_path)
        return SuggestOutcome(
            ok=True,
            provider=self._safe_provider_name(),
            model=self.model,
            use_case="suggest",
            usage=call.usage,
            latency_s=call.latency_s,
            cache_hit=call.cache_hit,
            cost_usd=call.cost_usd,
            calls=call.calls,
            estimated=call.usage.estimated,
            suggestions=kept,
            by_path=by_path,
            rejected=rejected,
            missing=missing,
            notes=prompts.notes_of(call.answer),
            batches=1,
            cache_hits=1 if call.cache_hit else 0,
        )

    def classify_batch(
        self,
        items: Sequence[ItemFacts],
        *,
        context: Mapping[str, Any] | None = None,
        use_cache: bool = True,
    ) -> ClassifyOutcome:
        """One batch: classifications for every item sent."""
        if not items:
            return ClassifyOutcome(
                ok=True, use_case="classify", provider=self._safe_provider_name(), model=self.model
            )
        scope = guardrails.ScopedDataset.of(items)
        try:
            prepared = self._prepare("classify", items, context=context)
            call = self._answer(prepared, use_cache=use_cache)
            classifications = prompts.parse_classifications(call.answer)
        except AIError as exc:
            return self._failed(exc, ClassifyOutcome, use_case="classify")
        kept, rejected, by_path = _lock_classifications(classifications, scope, call.redactor)
        missing = tuple(item.path for item in items if item.key() not in by_path)
        return ClassifyOutcome(
            ok=True,
            provider=self._safe_provider_name(),
            model=self.model,
            use_case="classify",
            usage=call.usage,
            latency_s=call.latency_s,
            cache_hit=call.cache_hit,
            cost_usd=call.cost_usd,
            calls=call.calls,
            estimated=call.usage.estimated,
            classifications=kept,
            by_path=by_path,
            rejected=rejected,
            missing=missing,
            notes=prompts.notes_of(call.answer),
            batches=1,
            cache_hits=1 if call.cache_hit else 0,
        )

    def explain(
        self,
        items: Sequence[ItemFacts],
        *,
        context: Mapping[str, Any] | None = None,
        on_delta: Callable[[str], None] | None = None,
        use_cache: bool = True,
    ) -> ExplainOutcome:
        """A deep explanation, streamed to ``on_delta`` while it is written."""
        if not items:
            return ExplainOutcome(
                ok=True, use_case="explain", provider=self._safe_provider_name(), model=self.model
            )
        sink = _DeltaSink(on_delta)
        try:
            prepared = self._prepare("explain", items, context=context)
            call = self._answer(
                prepared, stream=sink.active, on_delta=sink.emit, use_cache=use_cache
            )
            explanation = prompts.parse_explanation(call.answer)
        except AIError as exc:
            return self._failed(exc, ExplainOutcome, use_case="explain")
        sink.flush(explanation.explanation)
        return ExplainOutcome(
            ok=True,
            provider=self._safe_provider_name(),
            model=self.model,
            use_case="explain",
            usage=call.usage,
            latency_s=call.latency_s,
            cache_hit=call.cache_hit,
            cost_usd=call.cost_usd,
            calls=call.calls,
            estimated=call.usage.estimated,
            explanation=explanation,
            text=explanation.explanation,
        )

    def review(
        self,
        plan: Mapping[str, Any],
        *,
        plan_path: str | None = None,
        use_cache: bool = True,
    ) -> ReviewOutcome:
        """Severity-tagged annotations for a plan, keyed by its action ids."""
        action_ids = _plan_action_ids(plan)
        if not action_ids:
            return ReviewOutcome(
                ok=True,
                use_case="review",
                provider=self._safe_provider_name(),
                model=self.model,
            )
        context: dict[str, Any] = {"plan": plan}
        if plan_path:
            context["plan_path"] = plan_path
        dataset = guardrails.ScopedDataset.of_paths(action_ids, label="plan").fingerprint()
        try:
            prepared = self._prepare("review", (), context=context, dataset=dataset)
            call = self._answer(prepared, use_cache=use_cache)
            annotations = prompts.parse_annotations(call.answer)
        except AIError as exc:
            return self._failed(exc, ReviewOutcome, use_case="review")
        known = set(action_ids)
        kept = [annotation for annotation in annotations if annotation.action_id in known]
        rejected = tuple(
            annotation.action_id for annotation in annotations if annotation.action_id not in known
        )
        summary = call.answer.get("summary")
        return ReviewOutcome(
            ok=True,
            provider=self._safe_provider_name(),
            model=self.model,
            use_case="review",
            usage=call.usage,
            latency_s=call.latency_s,
            cache_hit=call.cache_hit,
            cost_usd=call.cost_usd,
            calls=call.calls,
            estimated=call.usage.estimated,
            annotations=tuple(kept),
            summary=summary if isinstance(summary, str) else "",
            rejected=rejected,
        )

    def summarize(
        self,
        plan: Mapping[str, Any],
        *,
        plan_path: str | None = None,
        use_cache: bool = True,
    ) -> SummarizeOutcome:
        """A plain-language summary of a plan."""
        context: dict[str, Any] = {"plan": plan}
        if plan_path:
            context["plan_path"] = plan_path
        dataset = guardrails.ScopedDataset.of_paths(
            _plan_action_ids(plan), label="plan"
        ).fingerprint()
        try:
            prepared = self._prepare("summarize", (), context=context, dataset=dataset)
            call = self._answer(prepared, use_cache=use_cache)
            summary = prompts.parse_plan_summary(call.answer)
        except AIError as exc:
            return self._failed(exc, SummarizeOutcome, use_case="summarize")
        return SummarizeOutcome(
            ok=True,
            provider=self._safe_provider_name(),
            model=self.model,
            use_case="summarize",
            usage=call.usage,
            latency_s=call.latency_s,
            cache_hit=call.cache_hit,
            cost_usd=call.cost_usd,
            calls=call.calls,
            estimated=call.usage.estimated,
            summary=summary,
        )

    # ------------------------------------------------------------------ #
    # Batched fills (the runner)
    # ------------------------------------------------------------------ #

    def plan_batches(
        self,
        case_id: str,
        items: Sequence[ItemFacts],
        *,
        batch_size: int | None = None,
        max_items: int | None = None,
    ) -> BatchPlan:
        """How the items split into requests, and what that will cost."""
        from spacesage.ai import runner as runner_module

        runner = runner_module.BatchRunner(
            self, case_id=case_id, batch_size=batch_size, max_items=max_items
        )
        return runner.plan(items)

    def suggest(
        self,
        items: Sequence[ItemFacts],
        *,
        batch_size: int | None = None,
        max_items: int | None = None,
        context: Mapping[str, Any] | None = None,
        on_progress: Callable[[BatchProgress], None] | None = None,
        cancel: Any = None,
        use_cache: bool = True,
    ) -> SuggestOutcome:
        """Fill suggestions for a list of items in bounded batches."""
        from spacesage.ai import runner as runner_module

        runner = runner_module.BatchRunner(
            self,
            case_id="suggest",
            batch_size=batch_size,
            max_items=max_items,
            on_progress=on_progress,
            cancel=cancel,
            use_cache=use_cache,
        )
        return runner.run_suggest(items, context=context)

    def classify(
        self,
        items: Sequence[ItemFacts],
        *,
        batch_size: int | None = None,
        max_items: int | None = None,
        context: Mapping[str, Any] | None = None,
        on_progress: Callable[[BatchProgress], None] | None = None,
        cancel: Any = None,
        use_cache: bool = True,
    ) -> ClassifyOutcome:
        """Fill classifications for a list of items in bounded batches."""
        from spacesage.ai import runner as runner_module

        runner = runner_module.BatchRunner(
            self,
            case_id="classify",
            batch_size=batch_size,
            max_items=max_items,
            on_progress=on_progress,
            cancel=cancel,
            use_cache=use_cache,
        )
        return runner.run_classify(items, context=context)

    # ------------------------------------------------------------------ #
    # Rule promotion
    # ------------------------------------------------------------------ #

    def promote_suggestion(
        self,
        item: ItemFacts,
        suggestion: Suggestion,
        *,
        tier: str | None = None,
        rule_id: str | None = None,
        dest_dir: str | Path | None = None,
        dry_run: bool = False,
    ) -> promote_engine.PromotionResult:
        """Preview (or write) a rule built from an accepted suggestion."""
        try:
            draft = promote_engine.draft_from_suggestion(
                item, suggestion, tier=tier, rule_id=rule_id
            )
        except AIError as exc:  # PromotionError is an AIError
            return promote_engine.PromotionResult(ok=False, error=exc, dry_run=dry_run)
        return promote_engine.write_rules([draft], dest_dir=dest_dir, dry_run=dry_run)

    def promote_classification(
        self,
        item: ItemFacts,
        classification: Classification,
        *,
        rule_id: str | None = None,
        dest_dir: str | Path | None = None,
        dry_run: bool = False,
    ) -> promote_engine.PromotionResult:
        """Preview (or write) a rule built from an accepted classification."""
        try:
            draft = promote_engine.draft_from_classification(item, classification, rule_id=rule_id)
        except AIError as exc:
            return promote_engine.PromotionResult(ok=False, error=exc, dry_run=dry_run)
        return promote_engine.write_rules([draft], dest_dir=dest_dir, dry_run=dry_run)

    # ------------------------------------------------------------------ #
    # Helpers
    # ------------------------------------------------------------------ #

    def _safe_provider_name(self) -> str:
        try:
            return self.provider().name
        except AIError:
            return self._provider_name or ""

    def _failed(self, exc: AIError, cls: type[T], *, use_case: str) -> T:
        """An outcome carrying the failure (never an exception for fixable errors)."""
        self.meter.record_failure()
        return cls(
            ok=False,
            error=exc,
            provider=self._safe_provider_name(),
            model=self.model,
            use_case=use_case,
        )


# --------------------------------------------------------------------------- #
# Module helpers
# --------------------------------------------------------------------------- #


class _DeltaSink:
    """Routes stream deltas through the prose streamer into the caller's callback.

    A model writes JSON (``{"explanation": "...", ...}``) whose ``str`` for the
    pane is just the prose: :class:`~spacesage.ai.prompts.ProseStreamer` peels the
    field out of the token stream, so the details pane fills with readable
    sentences rather than with braces.
    """

    def __init__(self, callback: Callable[[str], None] | None) -> None:
        self._callback = callback
        self._streamer = prompts.ProseStreamer()

    @property
    def active(self) -> bool:
        """True when the caller wants deltas."""
        return self._callback is not None

    def emit(self, delta: str) -> None:
        """Consume one streamed chunk."""
        if self._callback is None:
            return
        text = self._streamer.feed(delta)
        if text:
            self._callback(text)

    def flush(self, text: str) -> None:
        """Hand the finished prose over when the stream never produced any."""
        if self._callback is not None and not self._streamer.visible:
            self._callback(text)


def _cost_of(usage: Usage, provider: ProviderConfig) -> float | None:
    if provider.pricing_in is None or provider.pricing_out is None:
        return None
    return (
        usage.prompt_tokens * provider.pricing_in + usage.completion_tokens * provider.pricing_out
    ) / guardrails.TOKENS_PER_MILLION


def _usage_from_mapping(data: Mapping[str, Any]) -> Usage:
    def number(key: str) -> int:
        value = data.get(key, 0)
        return int(value) if isinstance(value, (int, float)) else 0

    return Usage(
        prompt_tokens=number("prompt_tokens"),
        completion_tokens=number("completion_tokens"),
        total_tokens=number("total_tokens"),
        estimated=bool(data.get("estimated", False)),
    )


def _redactor_from_entry(entry: guardrails.CacheEntry) -> prompts.PathRedactor | None:
    if not entry.redacted or not entry.tokens:
        return None
    return prompts.PathRedactor.from_mapping(entry.tokens)


def _lock_suggestions(
    suggestions: Sequence[Suggestion],
    scope: guardrails.ScopedDataset,
    redactor: prompts.PathRedactor | None,
) -> tuple[tuple[Suggestion, ...], tuple[str, ...], dict[str, Suggestion]]:
    """Keep the suggestions about items that were actually sent."""
    kept: list[Suggestion] = []
    rejected: list[str] = []
    by_path: dict[str, Suggestion] = {}
    for suggestion in suggestions:
        real = _restore(suggestion.path, redactor, rejected)
        if real is None:
            continue
        if not scope.has(real):
            rejected.append(real)
            continue
        key = candidates.path_key(real)
        if key in by_path:
            continue
        resolved = replace(suggestion, path=real)
        by_path[key] = resolved
        kept.append(resolved)
    return tuple(kept), tuple(rejected), by_path


def _lock_classifications(
    classifications: Sequence[Classification],
    scope: guardrails.ScopedDataset,
    redactor: prompts.PathRedactor | None,
) -> tuple[tuple[Classification, ...], tuple[str, ...], dict[str, Classification]]:
    """Keep the classifications about items that were actually sent."""
    kept: list[Classification] = []
    rejected: list[str] = []
    by_path: dict[str, Classification] = {}
    for classification in classifications:
        real = _restore(classification.path, redactor, rejected)
        if real is None:
            continue
        if not scope.has(real):
            rejected.append(real)
            continue
        key = candidates.path_key(real)
        if key in by_path:
            continue
        resolved = replace(classification, path=real)
        by_path[key] = resolved
        kept.append(resolved)
    return tuple(kept), tuple(rejected), by_path


def _restore(path: str, redactor: prompts.PathRedactor | None, rejected: list[str]) -> str | None:
    """Map a token back to its path (``None`` when it is not one we sent)."""
    if redactor is None:
        return path
    real = redactor.restore(path)
    if real is None:
        rejected.append(path)
        return None
    return real


def _plan_action_ids(plan: Mapping[str, Any]) -> tuple[str, ...]:
    actions = plan.get("actions")
    if not isinstance(actions, Sequence) or isinstance(actions, (str, bytes)):
        return ()
    out: list[str] = []
    for action in actions:
        if isinstance(action, Mapping):
            identifier = action.get("id")
            if isinstance(identifier, str) and identifier:
                out.append(identifier)
    return tuple(out)


def default_engine(**kwargs: Any) -> AIEngine:
    """An engine from the user's configuration (``None`` of it = a disabled one)."""
    return AIEngine(**kwargs)


__all__ = ["DEFAULT_BATCH_SIZE", "DEFAULT_MAX_ITEMS", "AIEngine", "default_engine"]
