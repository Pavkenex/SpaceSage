"""Executor contract tests: guards, digests, manifests, resolution, journal, CLI.

The contract this module pins is the one ``docs/design.md`` section 8 describes:
which paths are refused *before* anything runs, what the resolved per-op plan
looks like, what the journal records (and how an interrupted run reads back),
and what the internal CLI does.  The live filesystem operations themselves are
covered by ``tests/test_executor_posix.py`` (everywhere) and
``tests/test_executor_win.py`` (the Windows job).
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from fixtures import gen_executor
from spacesage import executor, planner
from spacesage.executor import backend

POSIX_ONLY = pytest.mark.skipif(os.name == "nt", reason="POSIX-only path semantics")


def run_cli(*args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, "-m", "spacesage", *args],
        capture_output=True,
        text=True,
        check=False,
    )


# --------------------------------------------------------------------------- #
# Path arithmetic and guards
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    ("path", "expected"),
    [
        ("C:\\Users\\Alice\\Temp", "C:\\"),
        ("d:/media/videos", "d:\\"),
        ("\\\\server\\share\\folder", "\\\\server\\share\\"),
        ("/mnt/data/tree", "/"),
        ("/", "/"),
    ],
)
def test_volume_root_keeps_the_style_of_the_path(path: str, expected: str) -> None:
    assert backend.volume_root(path) == expected


@pytest.mark.parametrize(
    ("path", "expected"),
    [
        ("C:\\Users\\Alice\\Temp", ("Users", "Alice", "Temp")),
        ("C:/Users/Alice", ("Users", "Alice")),
        ("C:\\", ()),
        ("/mnt/data", ("mnt", "data")),
        ("/", ()),
    ],
)
def test_path_components_below_the_volume_root(path: str, expected: tuple[str, ...]) -> None:
    assert backend.path_components(path) == expected


@pytest.mark.parametrize(
    ("path", "expected"),
    [
        ("C:\\Users\\Alice\\Temp", ("C", "Users", "Alice", "Temp")),
        ("D:\\media", ("D", "media")),
        ("\\\\server\\share\\x", ("server_share", "x")),
        ("/mnt/data/tree", ("mnt", "data", "tree")),
    ],
)
def test_quarantine_layout_is_volume_then_path(path: str, expected: tuple[str, ...]) -> None:
    assert backend.quarantine_relative(path) == expected


def test_join_path_follows_the_style_of_its_base() -> None:
    assert backend.join_path("D:\\_q", "abc", "Users", "Alice") == "D:\\_q\\abc\\Users\\Alice"
    assert backend.join_path("/mnt/q", "abc", "Users") == "/mnt/q/abc/Users"
    assert backend.join_path("/mnt/q/", "abc") == "/mnt/q/abc"


@pytest.mark.parametrize(
    ("path", "root", "expected"),
    [
        ("C:\\Users\\Alice\\Temp", "C:\\Users", True),
        ("c:/users/alice/temp", "C:\\Users", True),
        ("C:\\Users\\Alice", "C:\\Users\\Alice", True),
        ("C:\\UsersBob", "C:\\Users", False),
        ("/mnt/data/tree", "/mnt/data", True),
        ("/mnt/datax", "/mnt/data", False),
    ],
)
def test_is_under_handles_both_path_styles(path: str, root: str, expected: bool) -> None:
    assert backend.is_under(path, root) is expected


def test_plan_token_is_the_short_hex_prefix() -> None:
    plan_id = "sha256:" + "ab" * 32
    assert backend.plan_token(plan_id) == "ab" * 8


@pytest.mark.parametrize("plan_id", ["", "sha256:short", "sha1:" + "a" * 64, "nonsense"])
def test_plan_token_refuses_anything_but_a_sha256_id(plan_id: str) -> None:
    with pytest.raises(executor.ExecutorError):
        backend.plan_token(plan_id)


@pytest.mark.parametrize(
    ("path", "blocked"),
    [
        ("/", True),
        ("/etc", True),
        ("/home", True),
        ("/home/alice", True),
        ("/home/alice/.cache", False),
        ("/var", True),
        ("/var/cache/pip", False),
        ("/usr", True),
        ("/usr/share/docs", False),
        ("/tmp/tree/cache", False),
        ("C:\\", True),
        ("C:\\Windows", True),
        ("C:\\Windows\\Temp", False),
        ("C:\\Program Files", True),
        ("C:\\Program Files\\X\\cache", False),
        ("C:\\Users", True),
        ("C:\\Users\\Alice", True),
        ("C:\\Users\\Alice\\AppData\\Local\\Temp", False),
        ("_spacesage_quarantine", True),
        ("C:\\_spacesage_quarantine", True),
    ],
)
def test_protected_reason_refuses_roots_but_not_their_contents(path: str, blocked: bool) -> None:
    reason = backend.protected_reason(path)
    if blocked:
        assert reason, f"{path} should be refused"
    else:
        assert reason is None, f"{path} should be allowed (got {reason!r})"


@pytest.mark.parametrize("path", ["relative/path", "C:relative", "*.tmp", "C:\\Users\\*"])
def test_protected_reason_refuses_non_absolute_and_wildcard_paths(path: str) -> None:
    assert backend.protected_reason(path) is not None


def test_home_directory_is_protected() -> None:
    assert backend.protected_reason(os.path.expanduser("~")) == "the home directory"


# --------------------------------------------------------------------------- #
# Digests
# --------------------------------------------------------------------------- #


def test_digest_of_a_missing_path_is_empty_not_an_error(tmp_path: Path) -> None:
    result = backend.digest(str(tmp_path / "nope"))
    assert result.exists is False
    assert (result.files, result.dirs, result.bytes) == (0, 0, 0)
    assert result.tree_sha256 is None


def test_digest_of_a_file_records_size_and_content(tmp_path: Path) -> None:
    path = gen_executor.write_file(tmp_path / "blob.bin", 4096)
    result = backend.digest(str(path))
    assert (result.files, result.bytes, result.is_dir) == (1, 4096, False)
    assert result.content_complete
    assert result.content_sha256 is not None
    assert result.tree_sha256 is not None


def test_digest_is_location_independent(tmp_path: Path) -> None:
    first = gen_executor.plant_tree(tmp_path / "one")
    second = gen_executor.plant_tree(tmp_path / "two")
    assert backend.digest(str(first)).content_sha256 == backend.digest(str(second)).content_sha256


def test_digest_changes_with_content(tmp_path: Path) -> None:
    path = gen_executor.write_file(tmp_path / "blob.bin", 1024)
    before = backend.digest(str(path))
    path.write_bytes(b"other")
    after = backend.digest(str(path))
    assert backend.verify(before, after) == "mismatch"


def test_digest_skips_content_above_the_limit(tmp_path: Path) -> None:
    path = gen_executor.write_file(tmp_path / "blob.bin", 4096)
    limited = backend.digest(str(path), content_limit=1024)
    assert limited.content_sha256 is None
    assert limited.content_complete is False
    assert limited.tree_sha256 is not None
    assert backend.verify(limited, backend.digest(str(path))) == "verified"


def test_digest_of_a_directory_counts_files_and_dirs(tmp_path: Path) -> None:
    root = gen_executor.plant_tree(tmp_path / "tree")
    result = backend.digest(str(root))
    assert result.is_dir
    assert result.dirs == 5  # media, media/clips, cache, cache/deep, keep
    assert result.files == len(gen_executor.TREE)


def test_digest_of_a_symlink_is_its_target_not_the_payload(tmp_path: Path) -> None:
    target = gen_executor.write_file(tmp_path / "target.bin", 512)
    link = tmp_path / "link.bin"
    link.symlink_to(target)
    result = backend.digest(str(link))
    assert result.is_link and result.bytes == 0
    assert result.tree_sha256 is not None
    link.unlink()
    link.symlink_to(tmp_path / "elsewhere")
    assert backend.digest(str(link)).tree_sha256 != result.tree_sha256


def test_digest_of_a_directory_ignores_where_links_point_but_records_them(
    tmp_path: Path,
) -> None:
    root = tmp_path / "tree"
    gen_executor.write_file(root / "file.bin", 128)
    (root / "link").symlink_to(tmp_path / "outside")
    result = backend.digest(str(root))
    assert result.files == 1
    assert result.is_dir


def test_verify_reports_unavailable_when_neither_side_has_a_digest(tmp_path: Path) -> None:
    empty = backend.digest(str(tmp_path / "gone"))
    assert backend.verify(empty, empty) == "mismatch"


def test_reparse_kind_distinguishes_links_from_files(tmp_path: Path) -> None:
    plain = gen_executor.write_file(tmp_path / "plain.bin", 16)
    link = tmp_path / "link.bin"
    link.symlink_to(plain)
    assert backend.reparse_kind(str(plain)) is None
    assert backend.reparse_kind(str(link)) == "symlink"
    assert backend.is_reparse(str(link)) is True


# --------------------------------------------------------------------------- #
# The approval manifest
# --------------------------------------------------------------------------- #


def test_manifest_round_trip(tmp_path: Path) -> None:
    manifest = executor.make_manifest(
        "sha256:" + "1" * 64, ["a2", "a1"], rejected=["a3"], note="looks fine"
    )
    path = executor.write_manifest(tmp_path / "approved.json", manifest)
    loaded = executor.load_manifest(path)
    assert loaded.plan_id == manifest.plan_id
    assert loaded.approved == ("a2", "a1")
    assert loaded.rejected == ("a3",)
    assert loaded.note == "looks fine"
    assert loaded.schema == executor.MANIFEST_SCHEMA


def test_manifest_deduplicates_ids_but_keeps_the_order() -> None:
    manifest = executor.make_manifest("sha256:" + "1" * 64, ["a3", "a1", "a3"])
    assert manifest.approved == ("a3", "a1")


@pytest.mark.parametrize(
    "document",
    [
        {"plan_id": "sha256:" + "1" * 64, "approved": ["a1"]},
        {"schema": executor.MANIFEST_SCHEMA, "approved": ["a1"]},
        {"schema": executor.MANIFEST_SCHEMA, "plan_id": "", "approved": ["a1"]},
        {"schema": executor.MANIFEST_SCHEMA, "plan_id": "x", "approved": "a1"},
        {"schema": executor.MANIFEST_SCHEMA, "plan_id": "x", "approved": ["b1"]},
        {"schema": executor.MANIFEST_SCHEMA, "plan_id": "x", "approved": ["a1"], "created": 3},
        {"schema": executor.MANIFEST_SCHEMA, "plan_id": "x", "approved": ["a1"], "note": 3},
        {"schema": executor.MANIFEST_SCHEMA, "plan_id": "x", "approved": ["a1"], "rejected": "a2"},
        "not an object",
    ],
)
def test_parse_manifest_refuses_malformed_documents(document: object) -> None:
    with pytest.raises(executor.ExecutorError):
        executor.parse_manifest(document)


def test_load_manifest_refuses_a_missing_file(tmp_path: Path) -> None:
    with pytest.raises(executor.ExecutorError, match="no approval manifest"):
        executor.load_manifest(tmp_path / "nope.json")


def test_load_manifest_refuses_broken_json(tmp_path: Path) -> None:
    path = tmp_path / "approved.json"
    path.write_text("{not json", encoding="utf-8")
    with pytest.raises(executor.ExecutorError, match="not JSON"):
        executor.load_manifest(path)


def test_load_plan_refuses_a_missing_file_and_broken_json(tmp_path: Path) -> None:
    with pytest.raises(executor.ExecutorError, match="no plan at"):
        executor.load_plan(tmp_path / "plan.json")
    path = tmp_path / "plan.json"
    path.write_text("[]", encoding="utf-8")
    with pytest.raises(executor.ExecutorError, match="must be a JSON object"):
        executor.load_plan(path)


# --------------------------------------------------------------------------- #
# Plan validation and refusals
# --------------------------------------------------------------------------- #


def test_plan_ops_accepts_a_valid_plan(tmp_path: Path) -> None:
    result = gen_executor.scenario(tmp_path)
    plan_id, actions = executor.plan_ops(result.plan)
    assert plan_id == result.plan_id
    assert [action.id for action in actions] == ["a1", "a2", "a3"]
    assert [action.advisory for action in actions] == [False, False, True]


def test_plan_ops_refuses_a_tampered_action_list(tmp_path: Path) -> None:
    result = gen_executor.scenario(tmp_path)
    tampered = json.loads(json.dumps(result.plan))
    tampered["actions"][0]["bytes"] = 1
    with pytest.raises(executor.ExecutorError, match=r"not a valid spacesage\.plan/v1"):
        executor.plan_ops(tampered)


def test_plan_ops_refuses_a_t3_action_in_an_executable_slot(tmp_path: Path) -> None:
    result = gen_executor.scenario(tmp_path)
    bad = gen_executor.plan_dict(
        [
            gen_executor.action(
                "a1",
                "DELETE_QUARANTINE",
                result.root / "cache",
                1024,
                tier="T3",
            )
        ],
        targets={str(result.target): 10**9},
    )
    with pytest.raises(executor.ExecutorError, match=r"T3 is report-only|not a valid"):
        executor.plan_ops(bad)


def test_plan_ops_refuses_a_non_mapping_plan() -> None:
    with pytest.raises(executor.ExecutorError, match=r"spacesage\.plan/v1 mapping"):
        executor.plan_ops("not a plan")  # type: ignore[arg-type]


def test_apply_refuses_a_manifest_for_another_plan(tmp_path: Path) -> None:
    result = gen_executor.scenario(tmp_path)
    other = executor.make_manifest("sha256:" + "9" * 64, ["a1"])
    with pytest.raises(executor.ExecutorError, match="re-approve the current plan"):
        executor.apply_plan(result.plan, other, quarantine_root=result.quarantine)


def test_apply_refuses_unknown_action_ids(tmp_path: Path) -> None:
    result = gen_executor.scenario(tmp_path)
    manifest = executor.make_manifest(result.plan_id, ["a1", "a7"])
    with pytest.raises(executor.ExecutorError, match="does not have: a7"):
        executor.apply_plan(result.plan, manifest, quarantine_root=result.quarantine)


def test_apply_with_an_empty_manifest_is_a_no_op(tmp_path: Path) -> None:
    result = gen_executor.scenario(tmp_path)
    manifest = executor.make_manifest(result.plan_id, [])
    report = executor.apply_plan(result.plan, manifest, quarantine_root=result.quarantine)
    assert report.ops == ()
    assert report.ok()
    assert "nothing to do" in report.render_text()


def test_apply_refuses_to_execute_without_a_journal(tmp_path: Path) -> None:
    result = gen_executor.scenario(tmp_path)
    with pytest.raises(executor.ExecutorError, match="refusing to execute without a journal"):
        executor.apply_plan(result.plan, result.manifest(), execute=True)


def test_apply_refuses_a_non_manifest_object(tmp_path: Path) -> None:
    result = gen_executor.scenario(tmp_path)
    with pytest.raises(executor.ExecutorError, match=r"must be an executor\.Manifest"):
        executor.apply_plan(result.plan, "not a manifest", quarantine_root=result.quarantine)  # type: ignore[arg-type]


# --------------------------------------------------------------------------- #
# Resolution and dry runs
# --------------------------------------------------------------------------- #


def test_dry_run_resolves_everything_and_touches_nothing(tmp_path: Path) -> None:
    result = gen_executor.scenario(tmp_path)
    before = gen_executor.snapshot(tmp_path)
    report = executor.apply_plan(
        result.plan,
        result.manifest(),
        quarantine_root=result.quarantine,
        plan_path=result.plan_path,
        manifest_path=result.manifest_path,
    )
    assert report.dry_run and report.ok()
    assert [op.outcome for op in report.ops] == ["planned", "planned", "skipped"]
    assert gen_executor.snapshot(tmp_path) == before, "the dry run touched the tree"
    assert not result.quarantine.exists(), "the dry run created the quarantine store"
    assert not result.journal_path.exists(), "the dry run wrote a journal"
    assert report.total_actions == 3
    assert report.reclaimed_bytes() == sum(op.bytes for op in report.ops if op.outcome == "planned")


def test_dry_run_resolves_quarantine_into_the_plan_token(tmp_path: Path) -> None:
    result = gen_executor.scenario(tmp_path)
    report = executor.apply_plan(
        result.plan, result.manifest("a1"), quarantine_root=result.quarantine
    )
    step = report.ops[0].steps[0]
    token = backend.plan_token(result.plan_id)
    assert step.op == "quarantine" and step.outcome == "planned"
    assert step.dest == str(
        result.quarantine / token / result.root.as_posix().lstrip("/") / "cache"
    )


def test_dry_run_resolves_a_move_and_its_link(tmp_path: Path) -> None:
    result = gen_executor.scenario(tmp_path)
    report = executor.apply_plan(result.plan, result.manifest("a2"))
    steps = report.ops[0].steps
    assert [step.op for step in steps] == ["move", "link"]
    assert steps[0].dest == str(result.target / "Moved" / "media")
    assert steps[1].link == "JUNCTION"
    assert "junction" in steps[1].reason
    assert report.ops[0].outcome == "planned"


def test_dry_run_reports_advisory_actions_as_skipped_with_a_reason(tmp_path: Path) -> None:
    result = gen_executor.scenario(tmp_path)
    report = executor.apply_plan(result.plan, result.manifest("a3"))
    assert report.ops[0].advisory is True
    assert report.ops[0].outcome == "skipped"
    assert "nothing to execute" in report.ops[0].reason


def test_dry_run_keeps_plan_order_not_manifest_order(tmp_path: Path) -> None:
    result = gen_executor.scenario(tmp_path)
    report = executor.apply_plan(
        result.plan, result.manifest("a3", "a2", "a1"), quarantine_root=result.quarantine
    )
    assert [op.action_id for op in report.ops] == ["a1", "a2", "a3"]


def test_t3_paths_are_refused_per_op(tmp_path: Path) -> None:
    result = gen_executor.scenario(tmp_path)
    plan = gen_executor.plan_dict(
        [
            gen_executor.action("a1", "DELETE_QUARANTINE", "/etc", 1024, tier="T1"),
            gen_executor.action("a2", "DELETE_QUARANTINE", result.root / "cache", 512, tier="T1"),
        ]
    )
    report = executor.apply_plan(
        plan,
        executor.make_manifest(str(plan["plan_id"]), ["a1"]),
        quarantine_root=result.quarantine,
    )
    assert report.ops[0].outcome == "refused"
    assert "system directory" in report.ops[0].reason
    assert report.ok() is False


def test_the_volume_root_is_refused_per_op(tmp_path: Path) -> None:
    plan = gen_executor.plan_dict(
        [gen_executor.action("a1", "DELETE_QUARANTINE", "/", 1024, tier="T1")]
    )
    report = executor.apply_plan(
        plan, executor.make_manifest(str(plan["plan_id"]), ["a1"]), quarantine_root=tmp_path / "q"
    )
    assert report.ops[0].outcome == "refused"
    assert "volume root" in report.ops[0].reason


def test_wildcards_are_refused_per_op(tmp_path: Path) -> None:
    plan = gen_executor.plan_dict(
        [gen_executor.action("a1", "DELETE_QUARANTINE", "/tmp/*.tmp", 1024, tier="T1")]
    )
    report = executor.apply_plan(
        plan, executor.make_manifest(str(plan["plan_id"]), ["a1"]), quarantine_root=tmp_path / "q"
    )
    assert report.ops[0].outcome == "refused"
    assert "wildcards" in report.ops[0].reason


def test_the_within_guard_refuses_paths_outside_its_roots(tmp_path: Path) -> None:
    result = gen_executor.scenario(tmp_path)
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    report = executor.apply_plan(
        result.plan,
        result.manifest("a1", "a2"),
        quarantine_root=result.quarantine,
        within=[str(elsewhere)],
    )
    assert [op.outcome for op in report.ops] == ["refused", "refused"]
    assert "confined to" in report.ops[0].reason


def test_a_destination_outside_the_declared_targets_is_refused(tmp_path: Path) -> None:
    result = gen_executor.scenario(tmp_path)
    plan = gen_executor.plan_dict(
        [
            gen_executor.action(
                "a1",
                "MOVE",
                result.root / "media",
                1024,
                kind="move",
                dest=result.root / "elsewhere" / "media",
                link="JUNCTION",
            )
        ],
        targets={str(result.target): 10**9},
    )
    # The plan contract already refuses this, and the executor re-checks it at
    # resolution time, so a hand-made document cannot slip past either.
    with pytest.raises(executor.ExecutorError, match="not under any declared target"):
        executor.apply_plan(
            plan,
            executor.make_manifest(str(plan["plan_id"]), ["a1"]),
            quarantine_root=tmp_path / "q",
        )


def test_the_executor_rechecks_a_move_destination_against_the_targets() -> None:
    action = executor._Action(
        id="a1",
        type="MOVE",
        path="/tmp/tree/media",
        bytes=1,
        tier="T1",
        dest="/tmp/elsewhere/media",
        link="JUNCTION",
        why="w",
        advisory=False,
    )
    problems = executor._revalidate(
        action,
        op="move",
        dest=action.dest,
        link="JUNCTION",
        plan_data={"targets": {"D:": {"free_bytes": 1, "reserve_bytes": 0}}},
        within=(),
    )
    assert any("not under any declared target" in problem for problem in problems)


def test_a_hardlink_move_across_volumes_is_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    result = gen_executor.scenario(tmp_path)
    plan = gen_executor.plan_dict(
        [
            gen_executor.action(
                "a1",
                "MOVE",
                result.root / "media",
                1024,
                kind="move",
                dest=result.target / "Moved" / "media",
                link="HARDLINK",
            )
        ],
        targets={str(result.target): 10**9},
    )
    monkeypatch.setattr(executor, "same_volume", lambda first, second: False)
    report = executor.apply_plan(
        plan, executor.make_manifest(str(plan["plan_id"]), ["a1"]), quarantine_root=tmp_path / "q"
    )
    assert report.ops[0].outcome == "refused"
    assert "hard link cannot cross volumes" in report.ops[0].reason


def test_a_folder_move_within_declared_targets_resolves(tmp_path: Path) -> None:
    result = gen_executor.scenario(tmp_path)
    report = executor.apply_plan(result.plan, result.manifest("a2"))
    assert report.ops[0].outcome == "planned"
    assert report.ops[0].steps[0].dest == str(result.target / "Moved" / "media")


def test_dry_run_with_an_explicit_journal_records_a_run(tmp_path: Path) -> None:
    result = gen_executor.scenario(tmp_path)
    journal = tmp_path / "dry.jsonl"
    executor.apply_plan(
        result.plan, result.manifest("a1"), journal=journal, quarantine_root=result.quarantine
    )
    parsed = executor.read_journal(journal)
    assert [run.mode for run in parsed.runs] == ["dry-run"]
    assert parsed.ops == ()
    assert not result.quarantine.exists()


def test_apply_accepts_a_planner_plan_object(tmp_path: Path) -> None:
    result = gen_executor.scenario(tmp_path)
    actions = gen_executor.standard_actions(result.root, result.target)
    plan = planner.Plan(
        plan_id=planner.compute_plan_id(gen_executor.DEFAULT_SOURCE, actions),
        created=planner.datetime(2026, 9, 12, 12, 0, 0, tzinfo=planner.UTC),
        source=dict(gen_executor.DEFAULT_SOURCE),
        targets=(planner.PlanTarget(name=str(result.target), free_bytes=10**9, reserve_bytes=0),),
        actions=actions,
        summary=planner.PlanSummary(
            **planner._recompute_summary([item.to_dict() for item in actions]),
            dropped=0,
        ),
    )
    report = executor.apply_plan(plan, result.manifest("a1"), quarantine_root=result.quarantine)
    assert report.plan_id == result.plan_id
    assert report.ops[0].outcome == "planned"


# --------------------------------------------------------------------------- #
# The journal
# --------------------------------------------------------------------------- #


def test_journal_round_trip_records_every_field(tmp_path: Path) -> None:
    journal = tmp_path / "j.jsonl"
    writer = executor.JournalWriter(journal)
    run = writer.start_run(
        mode="execute",
        backend="posix",
        plan_id="sha256:" + "a" * 64,
        plan="/tmp/plan.json",
        manifest="/tmp/approved.json",
        quarantine_root="/tmp/q",
    )
    seq = writer.start_op(
        run=run,
        action_id="a1",
        action_type="DELETE_QUARANTINE",
        op="quarantine",
        reason="quarantine it",
        bytes_=2048,
        src="/tmp/tree/cache",
        dest="/tmp/q/token/tmp/tree/cache",
        link=None,
        before=backend.digest(str(tmp_path / "missing")),
    )
    writer.finish_op(
        run=run,
        seq=seq,
        action_id="a1",
        action_type="DELETE_QUARANTINE",
        op="quarantine",
        outcome="done",
        reason="renamed (same volume)",
        bytes_=2048,
        src="/tmp/tree/cache",
        dest="/tmp/q/token/tmp/tree/cache",
        link=None,
        verify="verified",
        notes=("all good",),
        command=("robocopy", "/MOVE"),
    )
    writer.end_run(run=run, mode="execute", counts={"done": 1}, bytes_=2048, ok=True)

    parsed = executor.read_journal(journal)
    assert [run.mode for run in parsed.runs] == ["execute"]
    assert parsed.next_run() == 2
    assert parsed.next_seq() == 2
    op = parsed.by_seq(1)
    assert op is not None
    assert op.op == "quarantine" and op.outcome == "done"
    assert op.verify == "verified" and op.notes == ("all good",)
    assert op.finished is True
    assert op.before is not None and op.before.exists is False
    assert op.inverse == "move_back"
    assert parsed.pending() == (op,)
    assert parsed.to_dict()["pending"] == [1]


def test_journal_treats_a_start_without_an_end_as_unfinished(tmp_path: Path) -> None:
    journal = tmp_path / "j.jsonl"
    writer = executor.JournalWriter(journal)
    run = writer.start_run(mode="execute", backend="posix")
    writer.start_op(
        run=run,
        action_id="a1",
        action_type="MOVE",
        op="move",
        reason="move it",
        bytes_=10,
        src="/tmp/a",
        dest="/tmp/b",
        link="JUNCTION",
    )
    parsed = executor.read_journal(journal)
    op = parsed.ops[0]
    assert op.finished is False and op.outcome == "started"
    assert op.touched_filesystem is True
    assert parsed.pending() == (op,)


def test_journal_pending_skips_ops_that_never_touched_anything(tmp_path: Path) -> None:
    journal = tmp_path / "j.jsonl"
    writer = executor.JournalWriter(journal)
    run = writer.start_run(mode="execute", backend="posix")
    seq = writer.start_op(
        run=run,
        action_id="a1",
        action_type="MOVE",
        op="move",
        reason="move it",
        bytes_=10,
        src="/tmp/a",
        dest="/tmp/b",
        link="JUNCTION",
    )
    writer.finish_op(
        run=run,
        seq=seq,
        action_id="a1",
        action_type="MOVE",
        op="move",
        outcome="skipped",
        reason="the path is gone",
        bytes_=10,
        src="/tmp/a",
        dest="/tmp/b",
        link="JUNCTION",
        verify="skipped",
    )
    assert executor.read_journal(journal).pending() == ()


def test_journal_pending_skips_ops_whose_undo_succeeded(tmp_path: Path) -> None:
    journal = tmp_path / "j.jsonl"
    writer = executor.JournalWriter(journal)
    run = writer.start_run(mode="execute", backend="posix")
    seq = writer.start_op(
        run=run,
        action_id="a1",
        action_type="MOVE",
        op="move",
        reason="move it",
        bytes_=10,
        src="/tmp/a",
        dest="/tmp/b",
        link=None,
    )
    writer.finish_op(
        run=run,
        seq=seq,
        action_id="a1",
        action_type="MOVE",
        op="move",
        outcome="done",
        reason="renamed",
        bytes_=10,
        src="/tmp/a",
        dest="/tmp/b",
        link=None,
        verify="verified",
    )
    undo_run = writer.start_run(mode="undo", backend="posix")
    undo_seq = writer.start_undo(
        run=undo_run,
        op_ref=seq,
        action_id="a1",
        op="move_back",
        reason="put it back",
        src="/tmp/b",
        dest="/tmp/a",
    )
    writer.finish_undo(
        run=undo_run,
        seq=undo_seq,
        op_ref=seq,
        action_id="a1",
        op="move_back",
        outcome="done",
        reason="renamed",
        src="/tmp/b",
        dest="/tmp/a",
        verify="verified",
    )
    parsed = executor.read_journal(journal)
    assert parsed.pending() == ()
    assert parsed.resolved(parsed.ops[0]) is not None
    assert [undo.outcome for undo in parsed.undos] == ["done"]


@pytest.mark.parametrize(
    "line",
    [
        "{not json",
        "[]",
        '{"kind": "nonsense"}',
        '{"schema": "spacesage.journal/v1", "kind": "op", "seq": "one"}',
        '{"schema": "spacesage.journal/v2", "kind": "run", "run": 1}',
    ],
)
def test_read_journal_refuses_malformed_records(tmp_path: Path, line: str) -> None:
    journal = tmp_path / "j.jsonl"
    journal.write_text(line + "\n", encoding="utf-8")
    with pytest.raises(executor.ExecutorError):
        executor.read_journal(journal)


def test_read_journal_names_the_line_it_cannot_parse(tmp_path: Path) -> None:
    journal = tmp_path / "j.jsonl"
    header = json.dumps(
        {
            "schema": "spacesage.journal/v1",
            "kind": "run",
            "run": 1,
            "at": "x",
            "mode": "execute",
            "backend": "posix",
        }
    )
    journal.write_text(f"{header}\n\n{{oops\n", encoding="utf-8")
    with pytest.raises(executor.ExecutorError, match=r":3: not JSON"):
        executor.read_journal(journal)


def test_read_journal_refuses_a_missing_file(tmp_path: Path) -> None:
    with pytest.raises(executor.ExecutorError, match="no journal at"):
        executor.read_journal(tmp_path / "nope.jsonl")


def test_undo_of_a_journal_with_nothing_pending_is_a_no_op(tmp_path: Path) -> None:
    journal = tmp_path / "j.jsonl"
    executor.JournalWriter(journal).start_run(mode="execute", backend="posix")
    report = executor.undo_journal(journal)
    assert report.ops == ()
    assert report.ok()
    assert "nothing to undo" in report.render_text()


def test_inverse_ops_cover_every_journaled_operation() -> None:
    assert executor.INVERSE_OPS["quarantine"] == "move_back"
    assert executor.INVERSE_OPS["move"] == "move_back"
    assert executor.INVERSE_OPS["link"] == "remove_link"
    assert executor.INVERSE_OPS["compress"] == "uncompress"


def test_default_journal_path_sits_next_to_the_plan(tmp_path: Path) -> None:
    plan = tmp_path / "sub" / "plan.json"
    assert executor.default_journal_path(plan) == tmp_path / "sub" / executor.DEFAULT_JOURNAL_NAME


# --------------------------------------------------------------------------- #
# The internal CLI
# --------------------------------------------------------------------------- #


def test_cli_apply_requires_an_approval_file(tmp_path: Path) -> None:
    result = gen_executor.scenario(tmp_path)
    completed = run_cli("apply", str(result.plan_path))
    assert completed.returncode == 2
    assert "--approve" in completed.stderr


def test_cli_apply_dry_run_prints_the_resolved_plan(tmp_path: Path) -> None:
    result = gen_executor.scenario(tmp_path)
    completed = run_cli(
        "apply",
        str(result.plan_path),
        "--approve",
        str(result.manifest_path),
        "--quarantine",
        str(result.quarantine),
    )
    assert completed.returncode == 0, completed.stderr
    assert "dry run" in completed.stdout
    assert result.plan_id in completed.stdout
    assert "quarantine" in completed.stdout
    assert not result.quarantine.exists()


def test_cli_apply_refuses_a_manifest_for_another_plan(tmp_path: Path) -> None:
    result = gen_executor.scenario(tmp_path)
    other = tmp_path / "other.json"
    executor.write_manifest(
        other, executor.make_manifest("sha256:" + "9" * 64, ["a1"], created=gen_executor.CREATED)
    )
    completed = run_cli("apply", str(result.plan_path), "--approve", str(other))
    assert completed.returncode == 1
    assert "re-approve the current plan" in completed.stderr


def test_cli_apply_refuses_unknown_ids(tmp_path: Path) -> None:
    result = gen_executor.scenario(tmp_path)
    executor.write_manifest(
        result.manifest_path,
        executor.make_manifest(result.plan_id, ["a1", "a9"], created=gen_executor.CREATED),
    )
    completed = run_cli("apply", str(result.plan_path), "--approve", str(result.manifest_path))
    assert completed.returncode == 1
    assert "a9" in completed.stderr


def test_cli_apply_execute_then_undo_round_trip(tmp_path: Path) -> None:
    result = gen_executor.scenario(tmp_path)
    before = gen_executor.snapshot(result.root)
    executed = run_cli(
        "apply",
        str(result.plan_path),
        "--approve",
        str(result.manifest_path),
        "--execute",
        "--quarantine",
        str(result.quarantine),
        "--within",
        str(result.root),
        "--within",
        str(result.target),
    )
    assert executed.returncode == 0, executed.stderr
    assert "done" in executed.stdout
    assert not (result.root / "cache").exists()
    assert result.journal_path.is_file()

    undone = run_cli("undo", str(result.journal_path))
    assert undone.returncode == 0, undone.stderr
    assert "reversed" in undone.stdout
    assert gen_executor.snapshot(result.root) == before

    again = run_cli("undo", str(result.journal_path))
    assert again.returncode == 0
    assert "nothing to undo" in again.stdout


def test_cli_apply_json_report_schema(tmp_path: Path) -> None:
    result = gen_executor.scenario(tmp_path)
    completed = run_cli(
        "apply",
        str(result.plan_path),
        "--approve",
        str(result.manifest_path),
        "--json",
        "--quarantine",
        str(result.quarantine),
    )
    assert completed.returncode == 0, completed.stderr
    document = json.loads(completed.stdout)
    assert document["schema"] == "spacesage.executor/v1"
    assert document["mode"] == "dry-run"
    assert document["plan_id"] == result.plan_id
    assert document["counts"]["planned"] == 2
    assert document["total_actions"] == 3
    assert [op["id"] for op in document["ops"]] == ["a1", "a2", "a3"]


def test_cli_undo_json_report_schema(tmp_path: Path) -> None:
    result = gen_executor.scenario(tmp_path)
    run_cli(
        "apply",
        str(result.plan_path),
        "--approve",
        str(result.manifest_path),
        "--execute",
        "--quarantine",
        str(result.quarantine),
    )
    completed = run_cli("undo", str(result.journal_path), "--json")
    assert completed.returncode == 0, completed.stderr
    document = json.loads(completed.stdout)
    assert document["schema"] == "spacesage.undo/v1"
    assert document["counts"]["done"] == len(document["ops"]) == 3
    assert document["restored_bytes"] > 0


def test_cli_undo_reports_a_missing_journal(tmp_path: Path) -> None:
    completed = run_cli("undo", str(tmp_path / "nope.jsonl"))
    assert completed.returncode == 1
    assert "no journal at" in completed.stderr


def test_cli_apply_fails_loudly_when_an_op_is_refused(tmp_path: Path) -> None:
    result = gen_executor.scenario(tmp_path)
    completed = run_cli(
        "apply",
        str(result.plan_path),
        "--approve",
        str(result.manifest_path),
        "--quarantine",
        str(result.quarantine),
        "--within",
        str(tmp_path / "somewhere-else"),
    )
    assert completed.returncode == 1
    assert "refused" in completed.stdout


# --------------------------------------------------------------------------- #
# Rendering
# --------------------------------------------------------------------------- #


def test_report_renders_a_summary_line_per_outcome(tmp_path: Path) -> None:
    result = gen_executor.scenario(tmp_path)
    report = executor.apply_plan(result.plan, result.manifest(), quarantine_root=result.quarantine)
    text = report.render_text()
    assert "approved:  3 of 3 actions" in text
    assert "2 planned, 0 done, 1 skipped, 0 refused, 0 failed" in text
    assert "dry run: nothing was changed" in text
    assert str(result.quarantine) in text


def test_worst_outcome_ranks_failures_first() -> None:
    from spacesage.executor.report import worst_outcome

    assert worst_outcome(["planned", "done"]) == "done"
    assert worst_outcome(["done", "skipped"]) == "skipped"
    assert worst_outcome(["refused", "failed"]) == "failed"
    assert worst_outcome(["done", "refused"]) == "refused"
    assert worst_outcome([]) == "skipped"


def test_link_and_verify_labels_are_human_readable() -> None:
    from spacesage.executor.report import link_label, verify_label

    assert link_label("JUNCTION") == "junction"
    assert link_label("HARDLINK") == "hard link"
    assert link_label(None) == ""
    assert verify_label("mismatch") == "MISMATCH"
    assert verify_label("not-run") == "not run"


@POSIX_ONLY
def test_digest_never_reads_through_a_symlink(tmp_path: Path) -> None:
    outside = gen_executor.write_file(tmp_path / "outside.bin", 32)
    root = tmp_path / "tree"
    root.mkdir()
    (root / "link.bin").symlink_to(outside)
    result = backend.digest(str(root))
    assert result.files == 0  # the link is an entry, not the file it points at
    assert result.bytes == 0
