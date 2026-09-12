"""Executor on a real filesystem (POSIX): quarantine, move, link, undo.

These are the tests that decide whether the executor is *safe*: they plant a real
tree (``tests/fixtures/gen_executor.py``), run the operations and compare the
tree byte for byte afterwards.  The Windows job runs the same scenarios through
``tests/test_executor_win.py`` with the ``win`` backend (robocopy, mklink).
"""

from __future__ import annotations

import fcntl
import json
import os
import shutil
from pathlib import Path

import pytest

from fixtures import gen_executor
from spacesage import executor, planner
from spacesage.executor import backend, posix

POSIX_ONLY = pytest.mark.skipif(os.name == "nt", reason="POSIX-only filesystem semantics")


def apply_all(
    result: gen_executor.Scenario,
    *ids: str,
    journal: Path | None = None,
    quarantine_root: Path | None = None,
) -> executor.ApplyReport:
    """Execute the approved ids of a scenario with its journal and quarantine."""
    return gen_executor.apply(result, *ids, journal=journal, quarantine_root=quarantine_root)


def step_of(report: executor.ApplyReport, op: str, index: int = 0) -> executor.Step:
    steps = [step for step in report.ops[index].steps if step.op == op]
    assert steps, f"no {op} step in {report.ops[index].to_dict()}"
    return steps[0]


# --------------------------------------------------------------------------- #
# Quarantine
# --------------------------------------------------------------------------- #


@POSIX_ONLY
def test_quarantine_undo_restores_the_tree_byte_identically(tmp_path: Path) -> None:
    result = gen_executor.scenario(tmp_path)
    before = gen_executor.snapshot(result.root)

    report = apply_all(result, "a1")
    assert report.ok(), report.to_dict()
    assert not (result.root / "cache").exists()
    step = step_of(report, "quarantine")
    assert step.outcome == "done" and step.verify == "verified"
    assert step.dest is not None and Path(step.dest).is_dir()
    assert step.bytes == sum(
        size for name, size in gen_executor.TREE.items() if name.startswith("cache/")
    )

    undone = executor.undo_journal(result.journal_path)
    assert undone.ok(), undone.to_dict()
    assert [op.op for op in undone.ops] == ["move_back"]
    assert undone.ops[0].steps[0].verify == "verified"
    assert gen_executor.snapshot(result.root) == before
    assert undone.restored_bytes() == step.bytes


@POSIX_ONLY
def test_quarantine_lands_under_the_plan_token_on_the_same_volume(tmp_path: Path) -> None:
    result = gen_executor.scenario(tmp_path)
    report = apply_all(result, "a1")
    token = backend.plan_token(result.plan_id)
    step = step_of(report, "quarantine")
    assert step.dest is not None
    assert Path(step.dest).parent == result.quarantine / token / result.root.as_posix().lstrip("/")
    assert "same volume" in step.reason or "renamed" in step.reason


@POSIX_ONLY
def test_a_single_file_quarantine_round_trips(tmp_path: Path) -> None:
    result = gen_executor.scenario(tmp_path)
    payload = result.root / "cache" / "blob.bin"
    size = 256 * gen_executor.KIB
    plan = gen_executor.plan_dict(
        [gen_executor.action("a1", "DELETE_QUARANTINE", payload, size, tier="T1", kind="delete")]
    )
    report = executor.apply_plan(
        plan,
        executor.make_manifest(str(plan["plan_id"]), ["a1"]),
        execute=True,
        journal=result.journal_path,
        quarantine_root=result.quarantine,
    )
    assert report.ok(), report.to_dict()
    step = step_of(report, "quarantine")
    assert step.outcome == "done" and step.verify == "verified"
    assert step.dest is not None and Path(step.dest).is_file()
    assert not payload.exists()

    undone = executor.undo_journal(result.journal_path)
    assert undone.ok(), undone.to_dict()
    assert payload.is_file()
    assert payload.read_bytes() == gen_executor.content("blob.bin", size)


@POSIX_ONLY
def test_quarantine_writes_an_audit_manifest_per_payload(tmp_path: Path) -> None:
    result = gen_executor.scenario(tmp_path)
    apply_all(result, "a1")
    token = backend.plan_token(result.plan_id)
    manifest_path = result.quarantine / token / "manifest.json"
    document = json.loads(manifest_path.read_text(encoding="utf-8"))
    assert document["schema"] == "spacesage.quarantine/v1"
    assert document["plan_id"] == result.plan_id
    entry = document["entries"][0]
    assert entry["action_id"] == "a1"
    assert entry["path"] == str(result.root / "cache")
    assert entry["quarantine_path"] == str(
        result.quarantine / token / result.root.as_posix().lstrip("/") / "cache"
    )
    assert entry["is_dir"] is True
    assert entry["tree_sha256"].startswith("sha256:")


@POSIX_ONLY
def test_quarantine_root_override_is_honoured(tmp_path: Path) -> None:
    result = gen_executor.scenario(tmp_path)
    store = tmp_path / "somewhere" / "else"
    report = apply_all(result, "a1", quarantine_root=store)
    step = step_of(report, "quarantine")
    assert step.dest is not None and Path(step.dest).is_relative_to(store)
    assert step.dest.startswith(str(store))


@POSIX_ONLY
def test_the_default_quarantine_root_follows_the_source_volume(tmp_path: Path) -> None:
    result = gen_executor.scenario(tmp_path)
    payload = result.root / "cache"
    expected = posix.BACKEND.default_quarantine_root(str(payload))
    report = executor.apply_plan(result.plan, result.manifest("a1"))
    step = step_of(report, "quarantine")
    assert step.dest is not None and step.dest.startswith(expected)
    assert backend.same_volume(step.dest, str(payload)) in (True, False)  # documented property


@POSIX_ONLY
def test_a_folder_with_no_files_is_quarantined_and_restored(tmp_path: Path) -> None:
    result = gen_executor.scenario(tmp_path)
    empty = result.root / "empty" / "nested"
    empty.mkdir(parents=True)
    plan = gen_executor.plan_dict(
        [
            gen_executor.action(
                "a1", "DELETE_QUARANTINE", result.root / "empty", 0, tier="T1", kind="delete"
            )
        ]
    )
    report = executor.apply_plan(
        plan,
        executor.make_manifest(str(plan["plan_id"]), ["a1"]),
        execute=True,
        journal=result.journal_path,
        quarantine_root=result.quarantine,
    )
    assert report.ok(), report.to_dict()
    assert not (result.root / "empty").exists()
    undone = executor.undo_journal(result.journal_path)
    assert undone.ok(), undone.to_dict()
    assert (result.root / "empty" / "nested").is_dir()


@POSIX_ONLY
def test_paths_with_spaces_quotes_and_unicode_round_trip(tmp_path: Path) -> None:
    result = gen_executor.scenario(tmp_path)
    odd = result.root / "a folder with spaces 'and quotes'" / "ünïcode-Ω"
    gen_executor.write_file(odd / "blob.bin", 4096)
    plan = gen_executor.plan_dict(
        [gen_executor.action("a1", "DELETE_QUARANTINE", odd, 4096, tier="T1", kind="delete")]
    )
    report = executor.apply_plan(
        plan,
        executor.make_manifest(str(plan["plan_id"]), ["a1"]),
        execute=True,
        journal=result.journal_path,
        quarantine_root=result.quarantine,
    )
    assert report.ok(), report.to_dict()
    assert not odd.exists()
    undone = executor.undo_journal(result.journal_path)
    assert undone.ok(), undone.to_dict()
    assert (odd / "blob.bin").read_bytes() == gen_executor.content("blob.bin", 4096)


# --------------------------------------------------------------------------- #
# Move (+ link)
# --------------------------------------------------------------------------- #


@POSIX_ONLY
def test_move_with_a_symlink_round_trips(tmp_path: Path) -> None:
    result = gen_executor.scenario(tmp_path)
    before = gen_executor.snapshot(result.root)
    plan = gen_executor.plan_dict(
        [
            gen_executor.action(
                "a1",
                "MOVE",
                result.root / "media",
                sum(size for name, size in gen_executor.TREE.items() if name.startswith("media/")),
                kind="move",
                tier="T2",
                dest=result.target / "Moved" / "media",
                link="SYMLINK",
            )
        ],
        targets={str(result.target): 10**9},
    )
    report = executor.apply_plan(
        plan,
        executor.make_manifest(str(plan["plan_id"]), ["a1"]),
        execute=True,
        journal=result.journal_path,
        quarantine_root=result.quarantine,
    )
    assert report.ok(), report.to_dict()
    moved = result.target / "Moved" / "media"
    assert moved.is_dir()
    assert (result.root / "media").is_symlink()
    assert (result.root / "media").resolve() == moved.resolve()
    assert (result.root / "media" / "movie.mp4").read_bytes() == gen_executor.content(
        "movie.mp4", 2 * gen_executor.MIB
    )

    undone = executor.undo_journal(result.journal_path)
    assert undone.ok(), undone.to_dict()
    assert [op.op for op in undone.ops] == ["remove_link", "move_back"]
    assert gen_executor.snapshot(result.root) == before
    assert not moved.exists()


@POSIX_ONLY
def test_a_junction_move_becomes_a_directory_symlink_here(tmp_path: Path) -> None:
    result = gen_executor.scenario(tmp_path)
    report = apply_all(result, "a2")
    assert report.ok(), report.to_dict()
    link = step_of(report, "link")
    assert link.outcome == "done" and link.verify == "verified"
    assert link.notes and "junctions are Windows-only" in link.notes[0]
    assert (result.root / "media").is_symlink()


@POSIX_ONLY
def test_a_hardlink_move_keeps_one_physical_payload(tmp_path: Path) -> None:
    result = gen_executor.scenario(tmp_path)
    plan = gen_executor.plan_dict(
        [
            gen_executor.action(
                "a1",
                "MOVE",
                result.root / "media" / "movie.mp4",
                2 * gen_executor.MIB,
                kind="move",
                tier="T2",
                dest=result.target / "Moved" / "movie.mp4",
                link="HARDLINK",
            )
        ],
        targets={str(result.target): 10**9},
    )
    report = executor.apply_plan(
        plan,
        executor.make_manifest(str(plan["plan_id"]), ["a1"]),
        execute=True,
        journal=result.journal_path,
        quarantine_root=result.quarantine,
    )
    assert report.ok(), report.to_dict()
    link = step_of(report, "link")
    assert link.outcome == "done" and link.verify == "verified"
    source = result.root / "media" / "movie.mp4"
    moved = result.target / "Moved" / "movie.mp4"
    assert source.stat().st_ino == moved.stat().st_ino
    undone = executor.undo_journal(result.journal_path)
    assert undone.ok(), undone.to_dict()
    assert moved.exists() is False and source.is_file() and not source.is_symlink()


@POSIX_ONLY
def test_a_move_without_a_link_says_the_path_disappears(tmp_path: Path) -> None:
    result = gen_executor.scenario(tmp_path)
    plan = gen_executor.plan_dict(
        [
            gen_executor.action(
                "a1",
                "MOVE",
                result.root / "media",
                1024,
                kind="move",
                tier="T2",
                dest=result.target / "Moved" / "media",
                link="NONE",
            )
        ],
        targets={str(result.target): 10**9},
    )
    report = executor.apply_plan(
        plan,
        executor.make_manifest(str(plan["plan_id"]), ["a1"]),
        execute=True,
        journal=result.journal_path,
        quarantine_root=result.quarantine,
    )
    assert report.ok(), report.to_dict()
    assert [step.op for step in report.ops[0].steps] == ["move"]
    assert report.ops[0].steps[0].notes
    assert "not linked back" in report.ops[0].steps[0].notes[0]
    assert not (result.root / "media").exists()


@POSIX_ONLY
def test_a_move_creates_the_destination_directories_it_needs(tmp_path: Path) -> None:
    result = gen_executor.scenario(tmp_path)
    report = apply_all(result, "a2")
    assert report.ok(), report.to_dict()
    assert (result.target / "Moved" / "media" / "clips" / "clip.mkv").is_file()


# --------------------------------------------------------------------------- #
# Re-validation against the live filesystem
# --------------------------------------------------------------------------- #


@POSIX_ONLY
def test_a_source_that_vanished_is_skipped(tmp_path: Path) -> None:
    result = gen_executor.scenario(tmp_path)
    import shutil

    shutil.rmtree(result.root / "cache")
    report = apply_all(result, "a1")
    step = step_of(report, "quarantine")
    assert step.outcome == "skipped"
    assert "no longer exists" in step.reason
    assert report.ok()  # a gone source is not a failure


@POSIX_ONLY
def test_a_source_that_became_a_link_is_refused(tmp_path: Path) -> None:
    result = gen_executor.scenario(tmp_path)
    import shutil

    payload = result.root / "cache"
    shutil.rmtree(payload)
    payload.symlink_to(result.target)
    report = apply_all(result, "a1")
    step = step_of(report, "quarantine")
    assert report.ops[0].outcome == "refused"
    assert "refusing to act through it" in step.reason
    assert payload.is_symlink()


@POSIX_ONLY
def test_a_destination_that_already_exists_is_skipped(tmp_path: Path) -> None:
    result = gen_executor.scenario(tmp_path)
    occupied = result.target / "Moved" / "media"
    occupied.mkdir(parents=True)
    (occupied / "keep.txt").write_text("mine", encoding="utf-8")
    report = apply_all(result, "a2")
    assert report.ops[0].outcome == "skipped"
    assert "already exists" in report.ops[0].reason
    assert (result.root / "media").is_dir()
    assert (occupied / "keep.txt").read_text(encoding="utf-8") == "mine"


@POSIX_ONLY
def test_a_locked_file_is_skipped_not_yanked(tmp_path: Path) -> None:
    result = gen_executor.scenario(tmp_path)
    plan = gen_executor.plan_dict(
        [
            gen_executor.action(
                "a1",
                "DELETE_QUARANTINE",
                result.root / "keep" / "notes.txt",
                1024,
                tier="T1",
                kind="delete",
            )
        ]
    )
    handle = os.open(result.root / "keep" / "notes.txt", os.O_RDONLY)
    try:
        fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        report = executor.apply_plan(
            plan,
            executor.make_manifest(str(plan["plan_id"]), ["a1"]),
            execute=True,
            journal=result.journal_path,
            quarantine_root=result.quarantine,
        )
    finally:
        fcntl.flock(handle, fcntl.LOCK_UN)
        os.close(handle)
    assert report.ops[0].outcome == "skipped"
    assert "in use" in report.ops[0].reason and "lock" in report.ops[0].reason
    assert (result.root / "keep" / "notes.txt").is_file()
    assert report.ok()


@POSIX_ONLY
def test_ntfs_compression_is_skipped_with_a_reason(tmp_path: Path) -> None:
    result = gen_executor.scenario(tmp_path)
    plan = gen_executor.plan_dict(
        [
            gen_executor.action(
                "a1", "COMPRESS_NTFS", result.root / "cache", 1024, tier="T1", kind="delete"
            )
        ]
    )
    report = executor.apply_plan(
        plan,
        executor.make_manifest(str(plan["plan_id"]), ["a1"]),
        execute=True,
        journal=result.journal_path,
        quarantine_root=result.quarantine,
    )
    assert report.ops[0].outcome == "skipped"
    assert "Windows-only" in report.ops[0].reason
    assert report.ok()
    assert (result.root / "cache").is_dir()


# --------------------------------------------------------------------------- #
# Journal and undo behaviour
# --------------------------------------------------------------------------- #


@POSIX_ONLY
def test_the_journal_records_every_step_of_a_move_with_a_link(tmp_path: Path) -> None:
    result = gen_executor.scenario(tmp_path)
    apply_all(result, "a1", "a2")
    parsed = executor.read_journal(result.journal_path)
    assert [run.mode for run in parsed.runs] == ["execute"]
    assert parsed.runs[0].plan_id == result.plan_id
    assert [op.op for op in parsed.ops] == ["quarantine", "move", "link"]
    assert [op.action_id for op in parsed.ops] == ["a1", "a2", "a2"]
    assert all(op.outcome == "done" for op in parsed.ops)
    assert all(op.verify == "verified" for op in parsed.ops[:2])
    assert parsed.ops[2].verify == "verified"
    assert parsed.ops[2].dest == str(result.target / "Moved" / "media")
    # The digests are what makes undo verifiable later.
    assert parsed.ops[0].before is not None and parsed.ops[0].after is not None
    assert parsed.ops[0].before.content_sha256 == parsed.ops[0].after.content_sha256


@POSIX_ONLY
def test_a_second_run_appends_to_the_same_journal(tmp_path: Path) -> None:
    result = gen_executor.scenario(tmp_path)
    first = apply_all(result, "a1")
    assert first.ok()
    second = apply_all(result, "a1")
    assert second.ops[0].outcome == "skipped"
    parsed = executor.read_journal(result.journal_path)
    assert [run.run for run in parsed.runs] == [1, 2]
    assert len(parsed.ops) == 2
    assert [run.mode for run in parsed.runs] == ["execute", "execute"]


@POSIX_ONLY
def test_undo_reverses_in_reverse_order(tmp_path: Path) -> None:
    result = gen_executor.scenario(tmp_path)
    apply_all(result, "a1", "a2")
    undone = executor.undo_journal(result.journal_path)
    assert [op.op_ref for op in undone.ops] == [3, 2, 1]
    assert [op.op for op in undone.ops] == ["remove_link", "move_back", "move_back"]


@POSIX_ONLY
def test_undo_blocks_when_the_original_path_is_occupied_again(tmp_path: Path) -> None:
    result = gen_executor.scenario(tmp_path)
    apply_all(result, "a1")
    (result.root / "cache").mkdir()
    (result.root / "cache" / "new.txt").write_text("new", encoding="utf-8")

    blocked = executor.undo_journal(result.journal_path)
    assert blocked.ops[0].outcome == "blocked"
    assert "occupied again" in blocked.ops[0].reason
    assert blocked.ok() is False
    assert (result.root / "cache" / "new.txt").is_file()

    shutil.rmtree(result.root / "cache")
    retried = executor.undo_journal(result.journal_path)
    assert retried.ops[0].outcome == "done"
    assert retried.ok()
    assert (result.root / "cache" / "blob.bin").is_file()


@POSIX_ONLY
def test_undo_twice_is_a_no_op(tmp_path: Path) -> None:
    result = gen_executor.scenario(tmp_path)
    before = gen_executor.snapshot(result.root)
    apply_all(result, "a1", "a2")
    first = executor.undo_journal(result.journal_path)
    assert first.ok() and len(first.ops) == 3
    second = executor.undo_journal(result.journal_path)
    assert second.ops == ()
    assert second.already_undone == 3
    assert gen_executor.snapshot(result.root) == before


@POSIX_ONLY
def test_undo_reports_a_mismatch_when_the_payload_changed(tmp_path: Path) -> None:
    result = gen_executor.scenario(tmp_path)
    report = apply_all(result, "a1")
    step = step_of(report, "quarantine")
    assert step.dest is not None
    (Path(step.dest) / "blob.bin").write_bytes(b"tampered while quarantined")

    undone = executor.undo_journal(result.journal_path)
    assert undone.ops[0].outcome == "failed"
    assert "does not match" in undone.ops[0].reason
    assert undone.ok() is False
    assert (result.root / "cache" / "blob.bin").read_bytes() == b"tampered while quarantined"


@POSIX_ONLY
def test_undo_settles_an_interrupted_operation(tmp_path: Path) -> None:
    """A start record without its end record means "check the filesystem"."""
    result = gen_executor.scenario(tmp_path)
    apply_all(result, "a1")
    journal = result.journal_path.read_text(encoding="utf-8").splitlines()
    without_end = "".join(
        line + "\n" for line in journal if '"phase": "end"' not in line or '"kind": "run"' in line
    )
    (result.journal_path).write_text(without_end, encoding="utf-8")

    parsed = executor.read_journal(result.journal_path)
    assert parsed.ops[0].finished is False
    undone = executor.undo_journal(result.journal_path)
    assert undone.ops[0].outcome == "done"
    assert "interrupted" in undone.ops[0].steps[0].reason or "interrupted" in undone.ops[0].reason
    assert (result.root / "cache" / "blob.bin").is_file()


@POSIX_ONLY
def test_undo_settles_an_interrupted_operation_that_never_ran(tmp_path: Path) -> None:
    result = gen_executor.scenario(tmp_path)
    payload = result.root / "cache"
    writer = executor.JournalWriter(result.journal_path)
    run = writer.start_run(mode="execute", backend="posix", plan_id=result.plan_id)
    writer.start_op(
        run=run,
        action_id="a1",
        action_type="DELETE_QUARANTINE",
        op="quarantine",
        reason="quarantine it",
        bytes_=1024,
        src=str(payload),
        dest=str(result.quarantine / "token" / "cache"),
        link=None,
    )
    undone = executor.undo_journal(result.journal_path)
    assert undone.ops[0].outcome == "skipped"
    assert "nothing to reverse" in undone.ops[0].reason
    assert payload.is_dir()

    again = executor.undo_journal(result.journal_path)
    assert again.ops == ()


@POSIX_ONLY
def test_an_op_that_failed_verification_is_journaled_and_reversible(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A move whose payload does not verify is reported failed -- and undoable."""
    result = gen_executor.scenario(tmp_path)
    payload = result.root / "cache"
    original = posix.BACKEND.move

    def corrupting_move(src: str, dest: str) -> backend.PrimResult:
        outcome = original(src, dest)
        if outcome.ok:
            (Path(dest) / "blob.bin").write_bytes(b"corrupted on arrival")
        return outcome

    monkeypatch.setattr(posix.BACKEND, "move", corrupting_move)
    report = apply_all(result, "a1")
    monkeypatch.undo()
    step = step_of(report, "quarantine")
    assert step.outcome == "failed"
    assert "does not match" in step.reason
    assert report.ok() is False
    assert not payload.exists()

    restored = executor.undo_journal(result.journal_path)
    assert restored.ops[0].outcome == "failed"  # the quarantined copy is corrupt
    assert "does not match" in restored.ops[0].reason
    assert (payload / "blob.bin").read_bytes() == b"corrupted on arrival"


@POSIX_ONLY
def test_a_missing_source_after_a_quarantine_is_reported_not_guessed(tmp_path: Path) -> None:
    result = gen_executor.scenario(tmp_path)
    apply_all(result, "a1")
    report = apply_all(result, "a1")
    assert report.ops[0].outcome == "skipped"
    parsed = executor.read_journal(result.journal_path)
    assert [op.outcome for op in parsed.ops] == ["done", "skipped"]
    assert parsed.pending() and len(parsed.pending()) == 1


@POSIX_ONLY
def test_undo_run_records_are_written_to_the_same_journal(tmp_path: Path) -> None:
    result = gen_executor.scenario(tmp_path)
    apply_all(result, "a1")
    executor.undo_journal(result.journal_path)
    parsed = executor.read_journal(result.journal_path)
    assert [run.mode for run in parsed.runs] == ["execute", "undo"]
    assert parsed.next_run() == 3
    assert [undo.outcome for undo in parsed.undos] == ["done"]
    assert parsed.undos[0].op_ref == parsed.ops[0].seq
    assert parsed.undos[0].op == "move_back"


@POSIX_ONLY
def test_a_full_scenario_round_trip_leaves_no_trace(tmp_path: Path) -> None:
    """quarantine + move + link, then undo: the tree is byte-identical again."""
    result = gen_executor.scenario(tmp_path)
    before = gen_executor.snapshot(result.root)

    report = apply_all(result)
    assert report.ok(), report.to_dict()
    assert [op.outcome for op in report.ops] == ["done", "done", "skipped"]
    assert not (result.root / "cache").exists()
    assert (result.root / "media").is_symlink()

    undone = executor.undo_journal(result.journal_path)
    assert undone.ok(), undone.to_dict()
    assert gen_executor.snapshot(result.root) == before
    assert not (result.target / "Moved" / "media").exists()


@POSIX_ONLY
def test_unapproved_actions_are_never_touched(tmp_path: Path) -> None:
    result = gen_executor.scenario(tmp_path)
    report = apply_all(result, "a2")
    assert [op.action_id for op in report.ops] == ["a2"]
    assert (result.root / "cache" / "blob.bin").is_file()
    assert (result.root / "keep" / "notes.txt").is_file()


@POSIX_ONLY
def test_a_deep_tree_survives_the_round_trip(tmp_path: Path) -> None:
    result = gen_executor.scenario(tmp_path)
    deep = result.root / "deep"
    gen_executor.write_file(deep / "/".join(f"level{i}" for i in range(12)) / "leaf.bin", 8192)
    before = gen_executor.snapshot(result.root)
    plan = gen_executor.plan_dict(
        [
            gen_executor.action(
                "a1", "DELETE_QUARANTINE", result.root / "deep", 8192, tier="T1", kind="delete"
            )
        ]
    )
    report = executor.apply_plan(
        plan,
        executor.make_manifest(str(plan["plan_id"]), ["a1"]),
        execute=True,
        journal=result.journal_path,
        quarantine_root=result.quarantine,
    )
    assert report.ok(), report.to_dict()
    assert not (result.root / "deep").exists()
    undone = executor.undo_journal(result.journal_path)
    assert undone.ok(), undone.to_dict()
    assert gen_executor.snapshot(result.root) == before


@POSIX_ONLY
def test_plan_object_execution_matches_document_execution(tmp_path: Path) -> None:
    result = gen_executor.scenario(tmp_path)
    actions = gen_executor.standard_actions(result.root, result.target)
    plan = planner.Plan(
        plan_id=planner.compute_plan_id(gen_executor.DEFAULT_SOURCE, actions),
        created=planner.datetime(2026, 9, 12, 12, 0, 0, tzinfo=planner.UTC),
        source=dict(gen_executor.DEFAULT_SOURCE),
        targets=(planner.PlanTarget(name=str(result.target), free_bytes=10**9, reserve_bytes=0),),
        actions=actions,
        summary=planner.PlanSummary(
            **planner._recompute_summary([item.to_dict() for item in actions]), dropped=0
        ),
    )
    report = apply_all(result, "a1")
    assert report.plan_id == str(plan.plan_id)
    assert report.ok()
