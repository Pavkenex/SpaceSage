"""``spacesage ai`` - the layer driven without Qt.

The commands exist so the AI layer can be exercised, scripted and smoke-tested
without the desktop app (``docs/ai.md``).  These tests run the real CLI: in
process for the fast paths, and as a real subprocess where the entry point
itself is what is being checked.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from ai_stub import (
    StubServer,
    annotation_entry,
    explanation_answer,
    item,
    review_answer,
    suggestion_answer,
)
from spacesage import cli
from spacesage.ai import AIConfig, ProviderConfig

MIB = 1024 * 1024
GIB = 1024 * MIB
STUB_MODEL = "stub-model"
OLD = "2023/01/15 09:00:00"
"""A modification date three years in the past (the fixtures' format)."""
"""Model the stub answers with (the same one ``conftest.ai_config`` uses)."""

AI_ENV = (
    "SPACESAGE_AI_OFF",
    "SPACESAGE_AI_PROVIDER",
    "SPACESAGE_AI_MODEL",
    "SPACESAGE_AI_BASE_URL",
    "SPACESAGE_AI_KEY_ENV",
    "SPACESAGE_AI_LOCAL_ONLY",
    "SPACESAGE_AI_REDACT_PATHS",
    "SPACESAGE_AI_CACHE_DIR",
)


def point_at(
    monkeypatch: pytest.MonkeyPatch,
    config: Path,
    cache_dir: Path,
    *,
    off: bool = False,
) -> None:
    """Make ``AIConfig.load`` read ``config`` in this process (and children)."""
    for name in AI_ENV:
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("SPACESAGE_AI_CONFIG", str(config))
    monkeypatch.setenv("SPACESAGE_AI_CACHE_DIR", str(cache_dir))
    if off:
        monkeypatch.setenv("SPACESAGE_AI_OFF", "1")


def write_config(tmp_path: Path, stub: StubServer) -> Path:
    """A config file for the stub provider, as the settings screen would save it."""
    provider = ProviderConfig(
        name="stub",
        kind="custom",
        base_url=stub.url,
        model=STUB_MODEL,
        timeout_s=5.0,
    )
    config = AIConfig(enabled=True, default_provider="stub", providers=(provider,), retries=0)
    return config.save(tmp_path / "ai.toml")


def run_cli(*args: str, env: dict[str, str]) -> subprocess.CompletedProcess[str]:
    """The real console entry point, in a child process with a private env."""
    return subprocess.run(
        [sys.executable, "-m", "spacesage", *args],
        capture_output=True,
        text=True,
        check=False,
        env={**os.environ, **env},
        cwd=Path(__file__).resolve().parents[1],
        timeout=60,
    )


# --------------------------------------------------------------------------- #
# status / check / models: no index, no Qt
# --------------------------------------------------------------------------- #


def test_ai_without_a_subcommand_prints_help(capsys: pytest.CaptureFixture[str]) -> None:
    assert cli.main(["ai"]) == 0
    out = capsys.readouterr().out
    assert "usage: spacesage ai" in out
    assert "suggest" in out and "explain" in out and "review" in out


def test_status_says_off_without_a_config(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    point_at(monkeypatch, tmp_path / "absent.toml", tmp_path / "cache")

    assert cli.main(["ai", "status"]) == 0
    out = capsys.readouterr().out.lower()
    assert "off" in out

    assert cli.main(["ai", "status", "--json"]) == 0
    body = json.loads(capsys.readouterr().out)
    assert body["enabled"] is False
    assert body["reason"]


def test_status_reports_the_configured_provider(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    ai_stub: StubServer,
) -> None:
    point_at(monkeypatch, write_config(tmp_path, ai_stub), tmp_path / "cache")

    assert cli.main(["ai", "status", "--json"]) == 0
    body = json.loads(capsys.readouterr().out)

    assert body["enabled"] is True
    assert body["provider"] == "stub"
    assert body["model"] == STUB_MODEL
    assert body["base_url"] == ai_stub.url
    assert body["ready"] is True


def test_check_reports_the_models_the_server_has(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    ai_stub: StubServer,
) -> None:
    point_at(monkeypatch, write_config(tmp_path, ai_stub), tmp_path / "cache")

    assert cli.main(["ai", "check"]) == 0
    captured = capsys.readouterr()
    assert "stub-model" in captured.out
    assert "stub-model" in (captured.out + captured.err)
    assert len(ai_stub.model_requests) == 1
    assert ai_stub.chat_requests == ()  # check never sends a chat call


def test_check_returns_one_when_the_server_refuses(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    ai_stub: StubServer,
) -> None:
    ai_stub.models_status = 503
    ai_stub.models_message = "the provider is down for maintenance"
    point_at(monkeypatch, write_config(tmp_path, ai_stub), tmp_path / "cache")

    assert cli.main(["ai", "check"]) == 1
    captured = capsys.readouterr()
    assert "error" in captured.err.lower()
    assert "down for maintenance" in captured.err or "HTTP" in captured.err


def test_models_prints_the_catalog(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    ai_stub: StubServer,
) -> None:
    point_at(monkeypatch, write_config(tmp_path, ai_stub), tmp_path / "cache")

    assert cli.main(["ai", "models", "--json"]) == 0
    body = json.loads(capsys.readouterr().out)

    assert [entry["id"] for entry in body] == [
        "stub-embed",
        "stub-model",
        "stub-model-mini",
    ]


# --------------------------------------------------------------------------- #
# explain: streaming, caching, redaction
# --------------------------------------------------------------------------- #


def test_explain_prints_the_prose_and_meters_the_call(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    ai_stub: StubServer,
) -> None:
    ai_stub.queue_json(explanation_answer(text="A big old installer; nothing needs it anymore."))
    point_at(monkeypatch, write_config(tmp_path, ai_stub), tmp_path / "cache")

    code = cli.main(["ai", "explain", "--path", "C:\\Temp\\setup-old.msi"])

    captured = capsys.readouterr()
    assert code == 0
    assert "nothing needs it anymore" in captured.out + captured.err
    assert "tokens" in captured.err
    # the payload carried the fact, not just the path
    assert "setup-old.msi" in ai_stub.last_payload()


def test_explain_needs_at_least_one_path(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    ai_stub: StubServer,
) -> None:
    point_at(monkeypatch, write_config(tmp_path, ai_stub), tmp_path / "cache")

    with pytest.raises(SystemExit) as caught:
        cli.main(["ai", "explain"])

    assert caught.value.code == 2  # argparse: --path is required
    assert "--path" in capsys.readouterr().err
    assert ai_stub.calls == 0


def test_explain_is_answered_from_the_cache_the_second_time(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    ai_stub: StubServer,
) -> None:
    ai_stub.queue_json(explanation_answer(text="First and only answer, long enough to parse."))
    point_at(monkeypatch, write_config(tmp_path, ai_stub), tmp_path / "cache")
    command = ["ai", "explain", "--path", "C:\\Temp\\setup-old.msi"]

    assert cli.main(command) == 0
    first = capsys.readouterr()
    calls_after_first = len(ai_stub.chat_requests)

    assert cli.main(command) == 0
    second = capsys.readouterr()

    assert "First and only answer" in first.out + first.err
    assert "First and only answer" in second.out + second.err
    assert len(ai_stub.chat_requests) == calls_after_first  # nothing new was asked


def test_no_cache_forces_a_fresh_call(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    ai_stub: StubServer,
) -> None:
    ai_stub.queue_json(explanation_answer(text="Answer number one, long enough to be parsed."))
    ai_stub.queue_json(explanation_answer(text="Answer number two, deliberately different."))
    point_at(monkeypatch, write_config(tmp_path, ai_stub), tmp_path / "cache")
    command = ["ai", "explain", "--path", "C:\\Temp\\setup-old.msi", "--no-cache"]

    assert cli.main(command) == 0
    assert cli.main(command) == 0

    assert len(ai_stub.chat_requests) == 2
    assert "number two" in capsys.readouterr().out


def test_explain_survives_a_dead_provider(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    ai_stub: StubServer,
) -> None:
    ai_stub.queue_error(500, "the model server is not running", code="server_error")
    point_at(monkeypatch, write_config(tmp_path, ai_stub), tmp_path / "cache")

    code = cli.main(["ai", "explain", "--path", "C:\\Temp\\setup-old.msi", "--json"])

    captured = capsys.readouterr()
    assert code == 1
    body = json.loads(captured.out)
    assert body["ok"] is False
    assert body["error"]["code"]
    assert body["error"]["hint"]  # every coded failure carries its fix
    assert "Traceback" not in captured.err


# --------------------------------------------------------------------------- #
# cache: listing and clearing
# --------------------------------------------------------------------------- #


def test_cache_lists_and_clears_answers(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    ai_stub: StubServer,
) -> None:
    ai_stub.queue_json(explanation_answer(text="A cached answer, long enough to be parsed."))
    point_at(monkeypatch, write_config(tmp_path, ai_stub), tmp_path / "cache")
    assert cli.main(["ai", "explain", "--path", "C:\\Temp\\setup-old.msi"]) == 0
    capsys.readouterr()

    assert cli.main(["ai", "cache", "--json"]) == 0
    body = json.loads(capsys.readouterr().out)
    assert body["cache"]["entries"] >= 1

    assert cli.main(["ai", "cache", "--clear"]) == 0
    assert "cleared" in capsys.readouterr().out.lower()

    assert cli.main(["ai", "cache", "--json"]) == 0
    assert json.loads(capsys.readouterr().out)["cache"]["entries"] == 0


# --------------------------------------------------------------------------- #
# suggest: a real index, a real subprocess
# --------------------------------------------------------------------------- #


def build_index(tmp_path: Path) -> Path:
    """An index holding two huge, old archives no rule pack matches."""
    from fixtures import gen_candidates
    from spacesage.ingest import ingest_csv

    rows = gen_candidates.build_rows(
        (
            gen_candidates.File("C:\\Users\\Alice\\Backups\\old-project.dump", 2 * GIB, OLD),
            gen_candidates.File("C:\\Users\\Alice\\Backups\\old-photos.dump", 1 * GIB, OLD),
        ),
        {},
    )
    export = tmp_path / "export.csv"
    gen_candidates.write_export(export, rows)
    db_path = tmp_path / "index.db"
    ingest_csv(export, db_path)
    return db_path


def top_row(db_path: Path) -> str:
    """The first row of the ranked Opportunities list (what suggest fills first)."""
    from spacesage import db, opportunities, rules

    conn = db.open_db(db_path)
    try:
        ruleset = rules.load_rules(user_dir=None)
        listing = opportunities.build_opportunities(conn, ruleset, db_path=str(db_path))
    finally:
        conn.close()
    return listing.rows[0].path


def test_suggest_fills_rows_of_a_real_index(tmp_path: Path, ai_stub: StubServer) -> None:
    db_path = build_index(tmp_path)
    top = top_row(db_path)
    # note: no `tier` - the suggest schema refuses keys it does not define, and
    # the repair round is the engine's job to run, not this test's to trigger
    ai_stub.queue_json(suggestion_answer(item(top, action="DELETE_QUARANTINE", confidence=0.8)))
    config = write_config(tmp_path, ai_stub)

    result = run_cli(
        "ai",
        "suggest",
        "--db",
        str(db_path),
        "--top",
        "1",
        "--min-size",
        "1 MiB",
        "--json",
        env={
            "SPACESAGE_AI_CONFIG": str(config),
            "SPACESAGE_AI_CACHE_DIR": str(tmp_path / "cache"),
        },
    )

    assert result.returncode == 0, result.stderr
    body = json.loads(result.stdout)
    assert [answer["path"] for answer in body["answers"]] == [top]
    assert body["answers"][0]["action"] == "DELETE_QUARANTINE"
    assert len(ai_stub.chat_requests) == 1
    assert "batches" in body  # the run's accounting travels with the answers


def test_suggest_reports_a_missing_index(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    ai_stub: StubServer,
) -> None:
    point_at(monkeypatch, write_config(tmp_path, ai_stub), tmp_path / "cache")

    assert cli.main(["ai", "suggest", "--db", str(tmp_path / "nope.db")]) == 1
    assert "index" in capsys.readouterr().err.lower()


def test_suggest_is_off_when_the_layer_is_off(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    ai_stub: StubServer,
) -> None:
    config = write_config(tmp_path, ai_stub)
    point_at(monkeypatch, config, tmp_path / "cache", off=True)

    assert cli.main(["ai", "suggest", "--db", str(tmp_path / "index.db")]) == 1
    assert ai_stub.calls == 0
    assert "off" in capsys.readouterr().err.lower()


# --------------------------------------------------------------------------- #
# review: a plan's actions, annotated
# --------------------------------------------------------------------------- #


def test_review_annotates_the_plan(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    ai_stub: StubServer,
) -> None:
    plan = tmp_path / "plan.json"
    plan.write_text(
        json.dumps(
            {
                "schema": "spacesage.plan/v1",
                "actions": [
                    {
                        "id": "a3",
                        "type": "DELETE_QUARANTINE",
                        "path": "C:\\Windows\\Temp",
                        "bytes": 300 * MIB,
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    ai_stub.queue_json(
        review_answer(
            annotation_entry(
                "a3",
                severity="warning",
                title="Windows is still using this folder",
                detail="Some files under C:\\Windows\\Temp are locked by a running update.",
                recommendation="Run it after the next reboot instead.",
            ),
            summary="One warning: a pending update owns part of the folder.",
        )
    )
    point_at(monkeypatch, write_config(tmp_path, ai_stub), tmp_path / "cache")

    assert cli.main(["ai", "review", "--plan", str(plan)]) == 0
    captured = capsys.readouterr().out
    assert "a3" in captured
    assert "pending update owns part of the folder" in captured
    assert "Run it after the next reboot" in captured


def test_review_rejects_annotations_the_plan_does_not_have(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    ai_stub: StubServer,
) -> None:
    plan = tmp_path / "plan.json"
    plan.write_text(
        json.dumps({"schema": "spacesage.plan/v1", "actions": [{"id": "a3", "type": "DELETE"}]}),
        encoding="utf-8",
    )
    ai_stub.queue_json(review_answer(annotation_entry("z9", severity="info")))
    point_at(monkeypatch, write_config(tmp_path, ai_stub), tmp_path / "cache")

    assert cli.main(["ai", "review", "--plan", str(plan)]) == 0
    captured = capsys.readouterr()
    assert "z9" in captured.err  # reported as rejected
    assert "z9" not in captured.out


def test_review_needs_a_plan_file(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    ai_stub: StubServer,
) -> None:
    point_at(monkeypatch, write_config(tmp_path, ai_stub), tmp_path / "cache")

    assert cli.main(["ai", "review", "--plan", str(tmp_path / "absent.json")]) == 1
    assert "plan" in capsys.readouterr().err.lower()


# --------------------------------------------------------------------------- #
# the module entry point itself
# --------------------------------------------------------------------------- #


def test_python_m_spacesage_runs_the_ai_commands(tmp_path: Path, ai_stub: StubServer) -> None:
    config = write_config(tmp_path, ai_stub)

    result = run_cli(
        "ai",
        "status",
        "--json",
        env={
            "SPACESAGE_AI_CONFIG": str(config),
            "SPACESAGE_AI_CACHE_DIR": str(tmp_path / "cache"),
        },
    )

    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout)["provider"] == "stub"


def test_a_broken_config_file_is_a_coded_failure(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    broken = tmp_path / "ai.toml"
    broken.write_text("default_provider = \n", encoding="utf-8")
    point_at(monkeypatch, broken, tmp_path / "cache")

    assert cli.main(["ai", "status"]) == 1  # a coded failure, never a traceback
    captured = capsys.readouterr()
    assert "error" in captured.err.lower()
    assert "ai.toml" in captured.err
