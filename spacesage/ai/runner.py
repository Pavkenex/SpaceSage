"""Bounded batch fills: how a 500-row list gets suggestions without 500 calls.

A run is planned before it starts - the batches are grouped by ``batch_size`` and
by the prompt budget, the ones already in the cache are counted, and the whole
thing is estimated (tokens and money) - and then executed batch by batch, once,
with a progress tick after each one and that batch's own answers handed to
``on_results``, so a list fills in row by row while the run is still going.

Failure policy: a batch that fails is recorded and the run keeps going, because
one rate-limited request should not cost the user the other 40 items.  A run
stops early only when continuing cannot work at all (no provider, missing key,
local-only refusal, the model does not exist, or the user cancelled); the reason
lands in :attr:`BatchRun.stopped` and on the last progress tick.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field, replace
from typing import Any, Protocol, cast

from spacesage.ai.client import Usage
from spacesage.ai.errors import AIError
from spacesage.ai.prompts import Classification, ItemFacts, Suggestion
from spacesage.ai.results import (
    BatchFailure,
    BatchPlan,
    BatchProgress,
    BatchRun,
    ClassifyOutcome,
    SuggestOutcome,
)


class VerdictOutcome(Protocol):
    """What one batch call must answer with, whichever use case produced it.

    Both :class:`~spacesage.ai.results.SuggestOutcome` and
    :class:`~spacesage.ai.results.ClassifyOutcome` satisfy this structurally; the
    runner only needs the answers, the accounting and the rejection lists.
    """

    ok: bool
    error: AIError | None
    usage: Usage
    latency_s: float
    cost_usd: float | None
    calls: int
    cache_hits: int
    by_path: Mapping[str, Any]
    rejected: tuple[str, ...]


STOP_CODES: frozenset[str] = frozenset(
    {"disabled", "invalid_config", "local_only", "auth", "model_missing"}
)
"""Codes after which retrying the next batch cannot possibly help."""

STOP_LABELS: Mapping[str, str] = {
    "disabled": "AI is off",
    "invalid_config": "the provider is misconfigured",
    "local_only": "local-only mode blocked the provider",
    "auth": "the provider rejected the key",
    "model_missing": "the provider does not serve the model",
    "cancelled": "cancelled",
    "unreachable": "the provider is unreachable",
    "rate_limited": "the provider is rate-limiting",
}


@dataclass(frozen=True)
class BatchExecution:
    """Everything one run produced: the run record plus the merged answers."""

    run: BatchRun
    results: Mapping[str, Any] = field(default_factory=dict)
    """Path key -> :class:`~spacesage.ai.prompts.Suggestion` (or Classification)."""
    rejected: tuple[str, ...] = ()
    usage: Usage = field(default_factory=Usage)
    latency_s: float = 0.0
    cost_usd: float | None = 0.0

    @property
    def ok(self) -> bool:
        """True when every planned batch was filled - a partial run is not ok.

        The answers are still there when it is ``False`` (``results`` holds what
        came back, ``run.failures`` says what did not), so a caller can show the
        partial result *and* tell the user it is partial.  A cancelled run is
        judged by what it produced: the user stopped it on purpose.
        """
        if self.run.batches == 0:
            return True
        if self.run.failures:
            return False
        if self.run.cancelled:
            return bool(self.results)
        return True

    def missing(self, paths: Sequence[str]) -> tuple[str, ...]:
        """Keys of the run's items that received no answer."""
        return tuple(path for path in paths if path not in self.results)


@dataclass(frozen=True)
class BatchRunner:
    """Plans and executes bounded batches through an :class:`~spacesage.ai.engine.AIEngine`."""

    engine: Any
    """The engine (typed loosely to keep this module import-order free)."""

    case_id: str = "suggest"
    batch_size: int | None = None
    max_items: int | None = None
    max_prompt_chars: int | None = None
    on_progress: Callable[[BatchProgress], None] | None = None
    cancel: Any = None
    use_cache: bool = True
    context: Mapping[str, Any] | None = None
    on_results: Callable[[Mapping[str, Any]], None] | None = None
    """Called with *one batch's* answers as it lands, so a list can fill in.

    The GUI's batch action needs this: rows update batch by batch instead of all
    at once when the run ends.  ``on_progress`` reports the counters, this
    reports the answers themselves (path key -> suggestion/classification).
    """

    # -- bounds ------------------------------------------------------------- #

    def bounds(self) -> tuple[int, int, int]:
        """``(batch_size, max_items, max_prompt_chars)`` after config defaults."""
        config = self.engine.config
        batch_size = self.batch_size if self.batch_size is not None else config.batch_size
        max_items = self.max_items if self.max_items is not None else config.max_items
        max_chars = (
            self.max_prompt_chars if self.max_prompt_chars is not None else config.max_prompt_chars
        )
        return max(1, batch_size), max(1, max_items), max(1_000, max_chars)

    def group(self, items: Sequence[ItemFacts]) -> tuple[tuple[ItemFacts, ...], ...]:
        """Split items into batches that honour both bounds."""
        batch_size, _, max_chars = self.bounds()
        groups: list[list[ItemFacts]] = []
        current: list[ItemFacts] = []
        for item in items:
            candidate = [*current, item]
            too_many = len(candidate) > batch_size
            too_big = bool(current) and self._payload_chars(candidate) > max_chars
            if too_many or too_big:
                groups.append(current)
                current = [item]
            else:
                current = candidate
        if current:
            groups.append(current)
        return tuple(tuple(group) for group in groups)

    def _payload_chars(self, items: Sequence[ItemFacts]) -> int:
        try:
            payload = self.engine.payload_for(self.case_id, items, context=self.context)
        except AIError:  # pragma: no cover - rendering never needs the network
            return 0
        return len(payload)

    def plan(self, items: Sequence[ItemFacts]) -> BatchPlan:
        """What the run will look like: batches, truncation, cache hits, estimate."""
        _, max_items, _ = self.bounds()
        selected = tuple(items[:max_items])
        groups = self.group(selected)
        cached = sum(1 for group in groups if self._is_cached(group))
        estimate = None
        if groups and self.engine.config.providers:
            try:
                estimate = self.engine.estimate(
                    self.case_id, groups, context=self.context, cached=cached
                )
            except AIError:  # pragma: no cover - the estimate only needs provider config
                estimate = None
        return BatchPlan(
            use_case=self.case_id,
            batches=tuple(tuple(item.key() for item in group) for group in groups),
            items=len(selected),
            truncated=len(items) - len(selected),
            cached_batches=cached,
            estimate=estimate,
        )

    def _is_cached(self, group: Sequence[ItemFacts]) -> bool:
        if not self.use_cache or not self.engine.cache.enabled:
            return False
        key = self.engine.cached_key_for(self.case_id, group, context=self.context)
        return bool(self.engine.cache.peek(key))

    def _cancelled(self) -> bool:
        cancel = self.cancel
        if cancel is None:
            return False
        flag = getattr(cancel, "is_set", None)
        return bool(flag()) if callable(flag) else False

    # -- execution ---------------------------------------------------------- #

    def execute(self, items: Sequence[ItemFacts]) -> BatchExecution:
        """Run the plan once: call each batch, merge answers, tick progress."""
        plan = self.plan(items)
        if not plan.batches:
            return BatchExecution(run=BatchRun(use_case=self.case_id, estimate=plan.estimate))
        by_key = {item.key(): item for item in items}
        batches = tuple(
            tuple(by_key[key] for key in group if key in by_key) for group in plan.batches
        )
        results: dict[str, Any] = {}
        rejected: list[str] = []
        failures: list[BatchFailure] = []
        usage = Usage()
        latency = 0.0
        cost: float | None = 0.0
        calls = 0
        cache_hits = 0
        done = 0
        batches_done = 0
        cancelled = False
        stopped = ""
        self._tick(
            plan, done=0, batches_done=0, calls=0, cache_hits=0, failures=0, results={}, cost=cost
        )
        for index, batch in enumerate(batches, start=1):
            if self._cancelled():
                cancelled = True
                stopped = "cancelled"
                break
            outcome = self._call(batch)
            calls += outcome.calls
            cache_hits += outcome.cache_hits
            usage = _add_usage(usage, outcome.usage)
            latency += outcome.latency_s
            cost = _add_cost(cost, outcome.cost_usd)
            if outcome.ok:
                answers = _by_path(outcome)
                results.update(answers)
                rejected.extend(_rejected(outcome))
                if answers:
                    self._results(answers)
                if not answers and batch:
                    # A valid-looking answer that names only paths outside the
                    # batch (or none at all): the rows stay missing, and that is
                    # a failure the caller must see rather than an empty success.
                    error = AIError(
                        "blocked_path",
                        "the answer named none of the items in this batch",
                        hint="the model hallucinated paths; re-run or lower the temperature",
                    )
                    failures.append(
                        BatchFailure.of(error, paths=[item.path for item in batch], batch=index)
                    )
            else:
                error = outcome.error or AIError("bad_response", "the batch failed")
                failures.append(
                    BatchFailure.of(error, paths=[item.path for item in batch], batch=index)
                )
                batches_done += 1
                done += len(batch)
                if error.code in STOP_CODES:
                    stopped = error.code
                    break
                self._tick(
                    plan,
                    done=done,
                    batches_done=batches_done,
                    calls=calls,
                    cache_hits=cache_hits,
                    failures=len(failures),
                    results=results,
                    rejected=len(rejected),
                    cost=cost,
                    last=tuple(item.path for item in batch),
                    message=STOP_LABELS.get(error.code, error.code),
                )
                continue
            batches_done += 1
            done += len(batch)
            self._tick(
                plan,
                done=done,
                batches_done=batches_done,
                calls=calls,
                cache_hits=cache_hits,
                failures=len(failures),
                results=results,
                rejected=len(rejected),
                cost=cost,
                last=tuple(item.path for item in batch),
                message=STOP_LABELS.get(stopped, "") if stopped else "",
            )
        run = BatchRun(
            use_case=self.case_id,
            paths=tuple(item.key() for batch in batches for item in batch),
            batches=batches_done,
            calls=calls,
            cache_hits=cache_hits,
            failures=tuple(failures),
            cancelled=cancelled,
            stopped=stopped,
            estimate=plan.estimate,
        )
        return BatchExecution(
            run=run,
            results=results,
            rejected=tuple(rejected),
            usage=usage,
            latency_s=latency,
            cost_usd=cost,
        )

    def run(self, items: Sequence[ItemFacts]) -> BatchRun:
        """The run record alone (the caller does not need the merged answers)."""
        return self.execute(items).run

    def _call(self, batch: Sequence[ItemFacts]) -> VerdictOutcome:
        if self.case_id == "classify":
            return cast(
                VerdictOutcome,
                self.engine.classify_batch(
                    list(batch), context=self.context, use_cache=self.use_cache
                ),
            )
        return cast(
            VerdictOutcome,
            self.engine.suggest_batch(list(batch), context=self.context, use_cache=self.use_cache),
        )

    def _results(self, answers: Mapping[str, Any]) -> None:
        """Hand one batch's freshly validated answers to the caller (if it wants them)."""
        if self.on_results is None:
            return
        self.on_results(dict(answers))

    def _tick(
        self,
        plan: BatchPlan,
        *,
        done: int,
        batches_done: int,
        calls: int,
        cache_hits: int,
        failures: int,
        results: Mapping[str, Any],
        cost: float | None,
        rejected: int = 0,
        last: tuple[str, ...] = (),
        message: str = "",
    ) -> None:
        if self.on_progress is None:
            return
        self.on_progress(
            BatchProgress(
                use_case=self.case_id,
                done=done,
                total=plan.items,
                batches_done=batches_done,
                batches_total=len(plan.batches),
                calls=calls,
                cache_hits=cache_hits,
                failures=failures,
                rejected=rejected,
                missing=max(0, done - len(results) - rejected),
                cost_usd=cost,
                last_paths=last,
                message=message,
            )
        )

    # -- typed entry points ------------------------------------------------- #

    def run_suggest(
        self, items: Sequence[ItemFacts], *, context: Mapping[str, Any] | None = None
    ) -> SuggestOutcome:
        """Fill suggestions for a list, in batches (the UI's batch action)."""
        runner = self if context is None else replace(self, context=context)
        execution = runner.execute(items)
        suggestions = tuple(
            item for item in execution.results.values() if isinstance(item, Suggestion)
        )
        return SuggestOutcome(
            ok=execution.ok,
            error=_first_error(execution.run.failures),
            provider=runner.engine._safe_provider_name(),
            model=runner.engine.model,
            use_case="suggest",
            usage=execution.usage,
            latency_s=execution.latency_s,
            cache_hit=execution.run.fully_cached,
            cost_usd=execution.cost_usd,
            calls=execution.run.calls,
            estimated=execution.usage.estimated,
            suggestions=suggestions,
            by_path=dict(execution.results),
            rejected=execution.rejected,
            missing=execution.missing(execution.run.paths),
            batches=execution.run.batches,
            cache_hits=execution.run.cache_hits,
            failures=len(execution.run.failures),
            cancelled=execution.run.cancelled,
            stopped=execution.run.stopped,
            estimate=execution.run.estimate,
        )

    def run_classify(
        self, items: Sequence[ItemFacts], *, context: Mapping[str, Any] | None = None
    ) -> ClassifyOutcome:
        """Fill classifications for a list, in batches."""
        runner = self if context is None else replace(self, context=context)
        execution = runner.execute(items)
        classifications = tuple(
            item for item in execution.results.values() if isinstance(item, Classification)
        )
        return ClassifyOutcome(
            ok=execution.ok,
            error=_first_error(execution.run.failures),
            provider=runner.engine._safe_provider_name(),
            model=runner.engine.model,
            use_case="classify",
            usage=execution.usage,
            latency_s=execution.latency_s,
            cache_hit=execution.run.fully_cached,
            cost_usd=execution.cost_usd,
            calls=execution.run.calls,
            estimated=execution.usage.estimated,
            classifications=classifications,
            by_path=dict(execution.results),
            rejected=execution.rejected,
            missing=execution.missing(execution.run.paths),
            batches=execution.run.batches,
            cache_hits=execution.run.cache_hits,
            failures=len(execution.run.failures),
            cancelled=execution.run.cancelled,
            stopped=execution.run.stopped,
            estimate=execution.run.estimate,
        )


def _first_error(failures: Sequence[BatchFailure]) -> AIError | None:
    if not failures:
        return None
    first = failures[0]
    return AIError(first.code, first.message, hint=first.hint)


def _by_path(outcome: VerdictOutcome) -> dict[str, Any]:
    return dict(outcome.by_path)


def _rejected(outcome: VerdictOutcome) -> tuple[str, ...]:
    return tuple(str(item) for item in outcome.rejected)


def _add_usage(left: Usage, right: Usage) -> Usage:
    if left.prompt_tokens == 0 and left.completion_tokens == 0 and left.total_tokens == 0:
        return right
    return Usage(
        prompt_tokens=left.prompt_tokens + right.prompt_tokens,
        completion_tokens=left.completion_tokens + right.completion_tokens,
        total_tokens=left.total_tokens + right.total_tokens,
        estimated=left.estimated or right.estimated,
    )


def _add_cost(left: float | None, right: float | None) -> float | None:
    """Sum costs; unknown pricing (``None``) makes the total unknown."""
    if left is None or right is None:
        return None
    return left + right


__all__ = ["STOP_CODES", "STOP_LABELS", "BatchExecution", "BatchRunner"]
