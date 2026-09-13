"""Bounded batch fills: the difference between 4 calls and 500.

A 500-row list must not become 500 requests, must not send more items than the
budget allows, must report progress while it runs, and must keep going when one
batch fails - a single unhelpful answer cannot lose the other 480 rows.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from ai_stub import StubServer, item, suggestion_answer
from ai_support import fact
from spacesage.ai import AIConfig, AIEngine, ProviderConfig
from spacesage.ai.runner import STOP_CODES, BatchRunner


def make_engine(stub: StubServer, tmp_path: Path, **settings: object) -> AIEngine:
    provider = ProviderConfig(
        name="stub", kind="custom", base_url=stub.url, model="stub-model", timeout_s=5.0
    )
    base: dict[str, object] = {
        "enabled": True,
        "default_provider": "stub",
        "providers": (provider,),
        "cache_dir": str(tmp_path / "cache"),
        "retries": 0,
    }
    base.update(settings)
    return AIEngine(AIConfig(**base), sleep=lambda _seconds: None)  # type: ignore[arg-type]


def paths(count: int) -> list[str]:
    return [f"C:\\Temp\\folder-{index:03d}\\setup-{index:03d}.msi" for index in range(count)]


def script(engine: AIEngine, items: list[Any], **plan_kwargs: Any) -> list[dict[str, Any]]:
    """One model answer per planned batch (the plan decides what to script)."""
    plan = engine.plan_batches("suggest", items, **plan_kwargs)
    by_key = {entry.key(): entry for entry in items}
    return [suggestion_answer(*[item(by_key[key].path) for key in batch]) for batch in plan.batches]


def test_a_long_list_is_split_into_bounded_batches(ai_stub: StubServer, tmp_path: Path) -> None:
    engine = make_engine(ai_stub, tmp_path)
    items = [fact(path) for path in paths(9)]

    plan = engine.plan_batches("suggest", items, batch_size=4)

    assert plan.calls == 3
    assert plan.items == 9
    assert [len(batch) for batch in plan.batches] == [4, 4, 1]
    assert plan.truncated == 0
    assert plan.estimate is not None and plan.estimate.calls == 3
    assert plan.to_dict()["batches"] == 3


def test_the_prompt_budget_also_splits_batches(ai_stub: StubServer, tmp_path: Path) -> None:
    budget = 2_000  # the runner ignores budgets below 1_000 (one call per item is worse)
    engine = make_engine(ai_stub, tmp_path, max_prompt_chars=budget)
    items = [fact(path, age_days=400, tier="T1") for path in paths(25)]
    by_key = {entry.key(): entry for entry in items}

    plan = engine.plan_batches("suggest", items, batch_size=len(items))

    assert plan.calls > 1  # 25 items cannot be one batch within the budget
    assert sum(len(batch) for batch in plan.batches) == 25
    for batch in plan.batches:
        rendered = engine.payload_for("suggest", [by_key[key] for key in batch])
        assert len(rendered) <= budget or len(batch) == 1


def test_max_items_caps_the_run_and_says_so(ai_stub: StubServer, tmp_path: Path) -> None:
    engine = make_engine(ai_stub, tmp_path, max_items=5)
    items = [fact(path) for path in paths(9)]

    plan = engine.plan_batches("suggest", items, batch_size=3)

    assert plan.items == 5
    assert plan.truncated == 4
    assert sum(len(batch) for batch in plan.batches) == 5


def test_every_batch_is_asked_once_and_the_answers_are_merged(
    ai_stub: StubServer, tmp_path: Path
) -> None:
    engine = make_engine(ai_stub, tmp_path)
    items = [fact(path) for path in paths(5)]
    for answer in script(engine, items, batch_size=2):
        ai_stub.queue_json(answer)

    outcome = engine.suggest(items, batch_size=2)

    assert outcome.ok is True
    assert outcome.batches == 3
    assert ai_stub.calls == 3
    assert len(outcome.suggestions) == 5
    assert outcome.missing == ()
    assert outcome.calls == 3
    assert outcome.estimate is not None and outcome.estimate.calls == 3


def test_progress_ticks_report_a_determinate_bar(ai_stub: StubServer, tmp_path: Path) -> None:
    engine = make_engine(ai_stub, tmp_path)
    items = [fact(path) for path in paths(4)]
    for answer in script(engine, items, batch_size=2):
        ai_stub.queue_json(answer)
    ticks: list[Any] = []

    engine.suggest(items, batch_size=2, on_progress=ticks.append)

    assert ticks
    first, last = ticks[0], ticks[-1]
    assert first.done == 0
    assert last.done == 4
    assert last.total == 4
    assert last.batches_done == 2
    assert last.batches_total == 2
    assert last.fraction == 1.0
    assert "4 / 4 items" in last.render()
    assert last.use_case == "suggest"


def test_one_failed_batch_does_not_lose_the_others(ai_stub: StubServer, tmp_path: Path) -> None:
    engine = make_engine(ai_stub, tmp_path)
    items = [fact(path) for path in paths(4)]
    answers = script(engine, items, batch_size=2)
    ai_stub.queue_error(500, "the first batch exploded")
    ai_stub.queue_json(answers[1])

    outcome = engine.suggest(items, batch_size=2)

    assert outcome.ok is False
    assert outcome.failures == 1
    assert len(outcome.suggestions) == 2  # the second batch still landed
    assert outcome.missing == (items[0].key(), items[1].key())
    assert outcome.error is not None and outcome.error.code == "server_error"


def test_a_fatal_error_stops_the_run(ai_stub: StubServer, tmp_path: Path) -> None:
    engine = make_engine(ai_stub, tmp_path)
    items = [fact(path) for path in paths(6)]
    ai_stub.queue_error(401, "bad key", code="invalid_api_key")

    outcome = engine.suggest(items, batch_size=2)

    assert outcome.ok is False
    assert outcome.cancelled is False
    assert ai_stub.calls == 1  # the other batches were not attempted
    assert outcome.failures == 1
    assert outcome.error is not None and outcome.error.code == "auth"
    assert "auth" in STOP_CODES
    assert outcome.to_dict()["stopped"] == "auth"


def test_a_cancelled_run_reports_what_it_finished(ai_stub: StubServer, tmp_path: Path) -> None:
    engine = make_engine(ai_stub, tmp_path)
    items = [fact(path) for path in paths(4)]
    answers = script(engine, items, batch_size=2)
    ai_stub.queue_json(answers[0])
    ai_stub.queue_json(answers[1])
    flag = {"cleared": False}

    class Cancel:
        def is_set(self) -> bool:
            return flag["cleared"]

    def on_progress(tick: Any) -> None:
        if tick.batches_done >= 1:  # cancel after the first batch
            flag["cleared"] = True

    outcome = engine.suggest(items, batch_size=2, cancel=Cancel(), on_progress=on_progress)

    assert outcome.cancelled is True
    assert ai_stub.calls == 1
    assert len(outcome.suggestions) == 2
    assert outcome.calls == 1


def test_a_second_run_over_the_same_list_sends_nothing(ai_stub: StubServer, tmp_path: Path) -> None:
    engine = make_engine(ai_stub, tmp_path)
    items = [fact(path) for path in paths(2)]
    for answer in script(engine, items, batch_size=1):
        ai_stub.queue_json(answer)

    first = engine.suggest(items, batch_size=1)
    second = engine.suggest(items, batch_size=1)

    assert ai_stub.calls == 2  # the first run only
    assert first.cache_hits == 0
    assert second.cache_hits == 2
    assert second.calls == 0
    assert second.cache_hit is True  # the whole run came out of the cache
    assert len(second.suggestions) == 2


def test_a_mixed_run_only_sends_the_missing_batches(ai_stub: StubServer, tmp_path: Path) -> None:
    engine = make_engine(ai_stub, tmp_path)
    all_paths = paths(4)
    known = [fact(path) for path in all_paths[:2]]
    for answer in script(engine, known, batch_size=2):
        ai_stub.queue_json(answer)
    engine.suggest(known, batch_size=2)

    extra = [fact(path) for path in all_paths[2:]]
    for answer in script(engine, extra, batch_size=2):
        ai_stub.queue_json(answer)
    outcome = engine.suggest([*known, *extra], batch_size=2)

    assert outcome.cache_hits == 1
    assert outcome.calls == 1
    assert ai_stub.calls == 2  # one batch from the first run, one from this one
    assert len(outcome.suggestions) == 4


def test_the_runner_can_be_driven_directly(ai_stub: StubServer, tmp_path: Path) -> None:
    engine = make_engine(ai_stub, tmp_path)
    items = [fact(path) for path in paths(2)]
    for answer in script(engine, items, batch_size=2):
        ai_stub.queue_json(answer)

    execution = BatchRunner(engine, case_id="suggest", batch_size=2).execute(items)

    assert execution.ok is True
    assert execution.run.batches == 1
    assert execution.run.calls == 1
    assert len(execution.results) == 2
    assert execution.run.fully_cached is False
    assert execution.missing(execution.run.paths) == ()
    assert execution.run.to_dict()["calls"] == 1


def test_an_empty_list_is_a_no_op(ai_stub: StubServer, tmp_path: Path) -> None:
    engine = make_engine(ai_stub, tmp_path)

    plan = engine.plan_batches("suggest", [])
    outcome = engine.suggest([])

    assert plan.calls == 0
    assert outcome.ok is True
    assert outcome.batches == 0
    assert ai_stub.calls == 0


def test_the_estimate_grows_with_the_list(ai_stub: StubServer, tmp_path: Path) -> None:
    engine = make_engine(ai_stub, tmp_path)

    small = engine.estimate("suggest", [[fact(paths(1)[0])]])
    large = engine.estimate("suggest", [[fact(path) for path in paths(10)]])

    assert small.calls == 1
    assert small.prompt_tokens < large.prompt_tokens
    assert large.items == 10


def test_classify_uses_the_same_machinery(ai_stub: StubServer, tmp_path: Path) -> None:
    from ai_stub import classification_answer, classification_entry

    engine = make_engine(ai_stub, tmp_path)
    items = [fact(path) for path in paths(3)]
    ai_stub.queue_json(
        classification_answer(
            classification_entry(paths(3)[0]),
            classification_entry(paths(3)[1]),
            classification_entry(paths(3)[2]),
        )
    )

    outcome = engine.classify(items, batch_size=3)

    assert outcome.ok is True
    assert len(outcome.classifications) == 3
    assert outcome.to_dict()["batches"] == 1
    assert outcome.usage.total_tokens > 0
