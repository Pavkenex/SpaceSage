"""Guardrails: validation, dataset locking, caching, metering, redaction.

These are the pieces that make an untrusted text generator safe to put in front of
a disk: what it says is checked, attributed, priced and - when the user asks for
it - written into the prompt without any real path.
"""

from __future__ import annotations

import json
import sqlite3
import time
from pathlib import Path

import pytest

from spacesage.ai import AIError, guardrails, prompts
from spacesage.ai.client import Usage
from spacesage.ai.config import ProviderConfig

SUGGEST = "suggest"


def item(path: str = "C:\\Temp\\a.msi", **overrides: object) -> prompts.ItemFacts:
    base: dict[str, object] = {"path": path, "is_dir": False, "size": 1024, "ext": "msi"}
    base.update(overrides)
    return prompts.ItemFacts(**base)  # type: ignore[arg-type]


def provider(**overrides: object) -> ProviderConfig:
    base: dict[str, object] = {
        "name": "stub",
        "kind": "custom",
        "base_url": "http://127.0.0.1:9/v1",
        "model": "stub-model",
    }
    base.update(overrides)
    return ProviderConfig(**base)  # type: ignore[arg-type]


# --------------------------------------------------------------------------- #
# validation
# --------------------------------------------------------------------------- #


def test_a_valid_answer_passes() -> None:
    answer = {
        "suggestions": [
            {
                "path": "C:\\Temp\\a.msi",
                "action": "DELETE_QUARANTINE",
                "why": "an old installer",
                "confidence": 0.9,
            }
        ]
    }

    parsed, errors = guardrails.read_answer(SUGGEST, f"Sure: {json.dumps(answer)}")

    assert errors == ()
    assert parsed == answer


def test_validation_reports_every_problem() -> None:
    errors = guardrails.validate(
        prompts.use_case(SUGGEST).schema,
        {"suggestions": [{"path": "", "action": "NOPE", "confidence": 3}]},
    )

    assert any("why" in error for error in errors)  # required but missing
    assert any("action" in error for error in errors)  # not in the vocabulary
    assert any("confidence" in error for error in errors)  # above the maximum
    assert any("path" in error for error in errors)  # too short


def test_an_unparseable_answer_becomes_an_error_not_an_exception() -> None:
    parsed, errors = guardrails.read_answer(SUGGEST, "I cannot do that.")

    assert parsed == {}
    assert errors and "no JSON object" in errors[0]


def test_a_json_answer_that_is_not_an_object_is_refused() -> None:
    parsed, errors = guardrails.read_answer(SUGGEST, "[1, 2, 3]")

    assert parsed == {}
    assert errors and "expected an object" in errors[0]


def test_repair_failure_carries_both_answers() -> None:
    error = guardrails.repair_failure(
        SUGGEST, "not json", ["$.suggestions: required property missing"]
    )

    assert error.code == "repair_failed"
    assert "suggest" in error.message
    assert error.detail is not None
    assert error.detail["body"] == "not json"
    assert error.detail["errors"] == ["$.suggestions: required property missing"]
    assert "spacesage ai models" in error.hint


# --------------------------------------------------------------------------- #
# dataset locking
# --------------------------------------------------------------------------- #


def test_the_scope_allows_its_items_and_case_folds_paths() -> None:
    scope = guardrails.ScopedDataset.of([item("C:\\Temp\\A.MSI"), item("C:\\Temp\\B.MSI")])

    allowed, blocked = scope.allow(["c:\\temp\\a.msi", "C:/Temp/b.msi", "C:\\Temp\\C.MSI"])

    assert len(allowed) == 2
    assert blocked == ("C:\\Temp\\C.MSI",)
    assert scope.has("c:\\temp\\a.msi") is True
    assert scope.fingerprint()


def test_an_out_of_scope_path_is_a_coded_error() -> None:
    scope = guardrails.ScopedDataset.of_paths(["C:\\Temp\\a.msi"], label="selection")

    allowed, blocked = scope.allow(["C:\\Temp\\a.msi", "C:\\Windows\\system32\\hack.dll"])
    assert allowed == ("C:\\Temp\\a.msi",)
    assert blocked == ("C:\\Windows\\system32\\hack.dll",)

    error = guardrails.DatasetError(blocked, scope="selection")
    assert error.code == "blocked_path"
    assert "C:\\Windows\\system32\\hack.dll" in error.message


def test_the_index_lock_answers_from_a_database(tmp_path: Path) -> None:
    conn = sqlite3.connect(tmp_path / "index.db")
    conn.execute("CREATE TABLE entries (path TEXT)")
    conn.executemany("INSERT INTO entries VALUES (?)", [("C:\\Temp\\a.msi",), ("C:\\Temp\\b.msi",)])
    conn.commit()

    index = guardrails.IndexDataset.from_db(conn, cap=1)  # force query mode
    assert index.mode == "query"
    assert index.has("c:/temp/a.msi") is True
    assert index.has("C:\\Temp\\zzz.msi") is False

    allowed, blocked = index.allow(["C:\\Temp\\a.msi", "C:\\Temp\\zzz.msi"])
    assert allowed == ("C:\\Temp\\a.msi",)
    assert blocked == ("C:\\Temp\\zzz.msi",)
    conn.close()


def test_the_lock_helper_splits_a_mixed_selection() -> None:
    scope = guardrails.ScopedDataset.of([item()])
    allowed, blocked = scope.allow(["C:\\Temp\\a.msi", "C:\\Users\\other\\b.txt"])
    assert allowed == ("C:\\Temp\\a.msi",)
    assert blocked == ("C:\\Users\\other\\b.txt",)

    # a caller that mixes sources (index rows plus a hand-typed path) sees what
    # the lock kept and what it dropped
    scope, kept, dropped = guardrails.locked([item(), item("D:\\Other\\b.txt")], label="batch")
    assert scope.has("C:\\Temp\\a.msi") is True
    assert len(kept) == 2
    assert dropped == ()


# --------------------------------------------------------------------------- #
# the cache
# --------------------------------------------------------------------------- #


def entry(
    cache: guardrails.ResponseCache, key: str, result: dict[str, object]
) -> guardrails.CacheEntry:
    return guardrails.CacheEntry(
        key=key,
        created=time.time(),
        provider="stub",
        model="stub-model",
        use_case=SUGGEST,
        dataset="",
        redacted=False,
        result=result,
        usage={"prompt_tokens": 10, "completion_tokens": 5},
    )


def test_a_cache_round_trip(tmp_path: Path) -> None:
    cache = guardrails.ResponseCache(tmp_path / "cache")
    key = guardrails.ResponseCache.key(
        use_case=SUGGEST, version="v1", provider="stub", model="m", payload="{}", dataset=""
    )

    assert cache.peek(key) is False
    assert cache.get(key) is None

    path = cache.put(entry(cache, key, {"suggestions": []}))
    assert path is not None and path.exists()

    assert cache.peek(key) is True
    loaded = cache.get(key)
    assert loaded is not None
    assert loaded.result == {"suggestions": []}
    assert loaded.usage["prompt_tokens"] == 10
    stats = cache.stats()
    assert stats.entries == 1
    assert stats.hits == 1
    assert 0.0 < stats.hit_rate <= 1.0

    assert cache.clear() == 1
    assert cache.stats().entries == 0


def test_the_key_changes_with_every_part_of_the_question() -> None:
    base = {
        "use_case": SUGGEST,
        "version": "v1",
        "provider": "stub",
        "model": "m",
        "payload": "[1]",
        "dataset": "",
    }

    keys = {
        guardrails.ResponseCache.key(**base),
        guardrails.ResponseCache.key(**(base | {"model": "other"})),
        guardrails.ResponseCache.key(**(base | {"payload": "[2]"})),
        guardrails.ResponseCache.key(**(base | {"dataset": "C:\\"})),
        guardrails.ResponseCache.key(**(base | {"version": "v2"})),
    }

    assert len(keys) == 5


def test_a_corrupt_entry_is_a_miss_not_a_crash(tmp_path: Path) -> None:
    cache = guardrails.ResponseCache(tmp_path / "cache")
    key = guardrails.ResponseCache.key(
        use_case=SUGGEST, version="v1", provider="stub", model="m", payload="{}", dataset=""
    )
    path = cache.path_for(key)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("{not json", encoding="utf-8")

    assert cache.get(key) is None
    assert cache.stats().corrupt == 1
    assert not path.exists()  # the bad entry is dropped


def test_a_disabled_cache_stores_nothing(tmp_path: Path) -> None:
    cache = guardrails.ResponseCache(tmp_path / "cache", enabled=False)
    key = "deadbeef"

    assert cache.put(entry(cache, key, {})) is None
    assert cache.get(key) is None
    assert cache.peek(key) is False


# --------------------------------------------------------------------------- #
# metering and estimates
# --------------------------------------------------------------------------- #


def test_the_meter_accumulates_tokens_and_cost() -> None:
    meter = guardrails.Meter()

    meter.record(Usage(prompt_tokens=1000, completion_tokens=500), pricing_in=1.0, pricing_out=2.0)
    meter.record(Usage(prompt_tokens=1000, completion_tokens=500), pricing_in=1.0, pricing_out=2.0)
    meter.record_cache_hit()
    meter.record_failure()

    snapshot = meter.snapshot()
    assert snapshot.calls == 2
    assert snapshot.failures == 1
    assert snapshot.cache_hits == 1
    assert snapshot.prompt_tokens == 2000
    assert snapshot.completion_tokens == 1000
    assert snapshot.total_tokens == 3000
    assert snapshot.cost_usd == pytest.approx(0.004)
    assert snapshot.pricing_known is True
    assert "tokens" in snapshot.render()


def test_the_meter_says_unknown_until_a_price_is_known() -> None:
    meter = guardrails.Meter()

    meter.record(Usage(prompt_tokens=10, completion_tokens=5))

    snapshot = meter.snapshot()
    assert snapshot.cost_usd is None
    assert snapshot.pricing_known is False
    assert "cost unknown" in snapshot.render()


def test_an_estimate_marks_unpriced_runs_as_unknown() -> None:
    priced = guardrails.estimate_batch(
        case_id=SUGGEST,
        payloads=["x" * 4000],
        provider=provider(pricing_in=1.0, pricing_out=1.0),
        max_tokens=1200,
        items=4,
    )
    unpriced = guardrails.estimate_batch(
        case_id=SUGGEST, payloads=["x" * 4000], provider=provider(), max_tokens=1200, items=4
    )

    assert priced.calls == 1
    assert priced.prompt_tokens > 0
    assert priced.total_tokens == priced.prompt_tokens + priced.completion_tokens
    assert priced.cost_usd is not None and priced.cost_usd > 0
    assert priced.pricing_known is True
    assert "$" in priced.render()

    assert unpriced.cost_usd is None
    assert unpriced.pricing_known is False


def test_cached_calls_are_discountable_in_an_estimate() -> None:
    estimate = guardrails.estimate_batch(
        case_id=SUGGEST,
        payloads=["a" * 100, "b" * 100],
        provider=provider(),
        max_tokens=1200,
        items=8,
        cached_calls=1,
    )

    assert estimate.calls == 2
    assert estimate.cached_calls == 1
    assert "1 call(s) will actually be sent" in estimate.note


# --------------------------------------------------------------------------- #
# redaction
# --------------------------------------------------------------------------- #


def test_redaction_finds_and_hides_real_paths() -> None:
    paths = ["C:\\Users\\matija\\Downloads", "D:\\Photos\\2019"]

    payload = prompts.build_payload(
        SUGGEST,
        [
            item(paths[0], is_dir=True),
            item(paths[1], is_dir=True, ext=None),
        ],
        redactor=guardrails.redactor(True),
    )

    assert guardrails.leaks(payload, paths) == ()
    guardrails.assert_redacted(payload, paths)
    assert "path-1" in payload and "path-2" in payload


def test_a_leak_is_detected() -> None:
    payload = '{"ref": "C:\\\\Users\\\\matija\\\\Downloads"}'
    paths = ["C:\\Users\\matija\\Downloads"]

    assert guardrails.leaks(payload, paths)
    with pytest.raises(AIError) as caught:
        guardrails.assert_redacted(payload, paths)
    assert caught.value.code == "blocked_path"


def test_the_redactor_helper_can_be_off() -> None:
    assert guardrails.redactor(False) is None
    assert guardrails.redactor(True) is not None
