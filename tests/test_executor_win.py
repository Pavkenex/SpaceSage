"""The Windows backend (robocopy / mklink / compact) -- and its pure helpers.

The pure helpers run everywhere (``longpath``, the quarantine root, the link
policy): they are what a POSIX dev box can still pin.  Everything that actually
calls ``robocopy``, ``mklink`` or ``compact`` is gated to the Windows CI job, and
so is the full apply/undo round trip through the ``win`` backend -- that job is
the only place the product's real target platform is exercised.
"""

from __future__ import annotations

import os
import shutil
from pathlib import Path

import pytest

from fixtures import gen_executor
from spacesage import executor
from spacesage.executor import backend, win

windows_only = pytest.mark.skipif(os.name != "nt", reason="Windows-only system tools")
posix_only = pytest.mark.skipif(os.name == "nt", reason="POSIX-only semantics")


# --------------------------------------------------------------------------- #
# Pure helpers (every platform)
# --------------------------------------------------------------------------- #


def test_longpath_only_prefixes_paths_that_need_it() -> None:
    short = r"C:\Users\Alice\Temp"
    assert win.longpath(short) == short
    long = "C:\\Users\\" + "a" * 300
    assert win.longpath(long) == "\\\\?\\" + long
    assert win.longpath("C:/Users/" + "a" * 300).startswith("\\\\?\\C:\\Users\\")


def test_longpath_prefixes_unc_shares_as_unc() -> None:
    long = "\\\\server\\share\\" + "a" * 300
    assert win.longpath(long) == "\\\\?\\UNC\\server\\share\\" + "a" * 300


def test_longpath_leaves_an_already_prefixed_path_alone() -> None:
    prefixed = "\\\\?\\C:\\" + "a" * 300
    assert win.longpath(prefixed) == prefixed


def test_win_path_for_os_prefixes_only_what_the_os_needs() -> None:
    instance = win.WinBackend()
    assert instance.path_for_os(r"C:\short") == r"C:\short"
    assert instance.path_for_os("C:\\" + "b" * 275).startswith("\\\\?\\")


def test_the_default_quarantine_root_sits_on_the_source_volume() -> None:
    instance = win.WinBackend()
    assert (
        instance.default_quarantine_root("D:\\Games\\SteamLibrary") == "D:\\_spacesage_quarantine"
    )
    assert (
        instance.default_quarantine_root(r"C:\Users\Alice\AppData\Local\Temp")
        == "C:\\_spacesage_quarantine"
    )


def test_the_junction_policy_matches_the_design() -> None:
    instance = win.WinBackend()
    allowed, reason = instance.can_link("JUNCTION", is_dir=True)
    assert allowed and "no elevation" in reason
    refused, why = instance.can_link("JUNCTION", is_dir=False)
    assert not refused and "directory link" in why
    refused, why = instance.can_link("HARDLINK", is_dir=True)
    assert not refused and "does not hard-link directories" in why
    assert instance.can_link("HARDLINK", is_dir=False)[0] is True
    assert instance.can_link("NONE", is_dir=False) == (True, "no link requested")
    assert instance.can_link("TELEPORT", is_dir=False)[0] is False


def test_compression_is_claimed_available_on_windows_only() -> None:
    allowed, reason = win.WinBackend().can_compress()
    assert allowed is (os.name == "nt")
    assert reason


def test_the_windows_name_is_recorded_in_reports() -> None:
    assert win.WinBackend().name == "win"
    assert win.BACKEND.name == "win"


@posix_only
def test_symlink_privilege_probes_are_honest_off_windows() -> None:
    allowed, reason = win.can_create_symlinks()
    assert allowed is False and "not running on Windows" in reason
    assert win.is_elevated() is False
    assert win.developer_mode_enabled() is False
    assert win.WinBackend().is_locked("/tmp") == (False, "")


# --------------------------------------------------------------------------- #
# The real thing (Windows job)
# --------------------------------------------------------------------------- #


@windows_only
def test_windows_quarantine_undo_restores_the_tree_byte_identically(tmp_path: Path) -> None:
    result = gen_executor.scenario(tmp_path)
    before = gen_executor.snapshot(result.root)
    report = gen_executor.apply(result, "a1", backend=win.BACKEND)
    assert report.ok(), report.to_dict()
    assert not (result.root / "cache").exists()
    assert report.ops[0].steps[0].verify == "verified"

    undone = executor.undo_journal(result.journal_path, backend=win.BACKEND)
    assert undone.ok(), undone.to_dict()
    assert gen_executor.snapshot(result.root) == before


@windows_only
def test_windows_a_move_uses_robocopy_and_a_junction(tmp_path: Path) -> None:
    result = gen_executor.scenario(tmp_path)
    report = gen_executor.apply(result, "a2", backend=win.BACKEND)
    assert report.ok(), report.to_dict()
    move_step, link_step = report.ops[0].steps
    if shutil.which("robocopy"):
        assert move_step.command and move_step.command[0] == "robocopy"
        assert "/MOVE" in move_step.command or "/MOV" in move_step.command
    assert link_step.command and link_step.command[0] == "cmd"
    assert "/J" in link_step.command
    source = result.root / "media"
    moved = result.target / "Moved" / "media"
    assert moved.is_dir()
    assert backend.reparse_kind(str(source)) == "junction"
    assert (source / "movie.mp4").read_bytes() == gen_executor.content(
        "movie.mp4", 2 * gen_executor.MIB
    )

    undone = executor.undo_journal(result.journal_path, backend=win.BACKEND)
    assert undone.ok(), undone.to_dict()
    assert not source.exists()
    assert not moved.exists()
    assert (result.root / "media").is_dir()  # restored as a real directory


@windows_only
def test_windows_removing_a_junction_never_touches_its_target(tmp_path: Path) -> None:
    result = gen_executor.scenario(tmp_path)
    report = gen_executor.apply(result, "a2", backend=win.BACKEND)
    assert report.ok(), report.to_dict()
    source = result.root / "media"
    moved = result.target / "Moved" / "media"
    removed = win.BACKEND.remove_link(str(source))
    assert removed.ok, removed.detail
    assert not source.exists()
    assert (moved / "movie.mp4").is_file()


@windows_only
def test_windows_a_symlink_move_is_refused_without_the_privilege(tmp_path: Path) -> None:
    allowed, _reason = win.can_create_symlinks()
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
        backend=win.BACKEND,
    )
    if allowed:
        assert report.ok(), report.to_dict()
        assert (result.root / "media").is_symlink()
    else:
        # No elevation and no Developer Mode: the whole move is skipped, so the
        # path can never end up dangling.
        assert report.ops[0].outcome == "skipped"
        assert "Developer Mode" in report.ops[0].reason
        assert (result.root / "media").is_dir()
        assert not (result.target / "Moved" / "media").exists()


@windows_only
def test_windows_a_hardlink_move_round_trips(tmp_path: Path) -> None:
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
        backend=win.BACKEND,
    )
    assert report.ok(), report.to_dict()
    source = result.root / "media" / "movie.mp4"
    moved = result.target / "Moved" / "movie.mp4"
    assert os.path.samefile(source, moved)
    undone = executor.undo_journal(result.journal_path, backend=win.BACKEND)
    assert undone.ok(), undone.to_dict()
    assert source.is_file() and not source.is_symlink()
    assert not moved.exists()


@windows_only
def test_windows_compression_round_trips_through_compact(tmp_path: Path) -> None:
    result = gen_executor.scenario(tmp_path)
    target = result.root / "keep" / "notes.txt"
    plan = gen_executor.plan_dict(
        [gen_executor.action("a1", "COMPRESS_NTFS", target, 1024, tier="T1", kind="delete")]
    )
    report = executor.apply_plan(
        plan,
        executor.make_manifest(str(plan["plan_id"]), ["a1"]),
        execute=True,
        journal=result.journal_path,
        quarantine_root=result.quarantine,
        backend=win.BACKEND,
    )
    assert report.ok(), report.to_dict()
    assert backend.is_compressed(str(target))
    assert report.ops[0].steps[0].command[0] == "compact"

    undone = executor.undo_journal(result.journal_path, backend=win.BACKEND)
    assert undone.ok(), undone.to_dict()
    assert not backend.is_compressed(str(target))
    assert target.read_bytes() == gen_executor.content("notes.txt", 1024)


@windows_only
def test_windows_a_file_held_open_is_skipped(tmp_path: Path) -> None:
    """A directory with an open file inside it cannot be renamed: skipped, not forced."""
    result = gen_executor.scenario(tmp_path)
    payload = result.root / "cache"
    handle = (payload / "blob.bin").open("rb")
    try:
        locked, _reason = win.BACKEND.is_locked(str(payload))
        if not locked:
            pytest.skip("this filesystem does not report the open file as a lock")
        report = gen_executor.apply(result, "a1", backend=win.BACKEND)
        assert report.ops[0].outcome == "skipped"
        assert "in use" in report.ops[0].reason
        assert payload.is_dir()
        assert report.ok()
    finally:
        handle.close()


@windows_only
def test_windows_long_paths_survive_a_quarantine_round_trip(tmp_path: Path) -> None:
    result = gen_executor.scenario(tmp_path)
    deep = tmp_path / "deep"
    current = deep
    for index in range(12):
        current = current / f"level-{index}-{'x' * 20}"
    current.mkdir(parents=True, exist_ok=True)
    leaf = current / "payload.bin"
    leaf.write_bytes(gen_executor.content("deep", 4096))
    assert len(str(leaf)) > 260

    long_root = str(deep)
    plan = gen_executor.plan_dict(
        [gen_executor.action("a1", "DELETE_QUARANTINE", current, 4096, tier="T1", kind="delete")]
    )
    report = executor.apply_plan(
        plan,
        executor.make_manifest(str(plan["plan_id"]), ["a1"]),
        execute=True,
        journal=result.journal_path,
        quarantine_root=result.quarantine,
        backend=win.BACKEND,
    )
    assert report.ok(), report.to_dict()
    assert not os.path.lexists(win.longpath(str(current)))
    undone = executor.undo_journal(result.journal_path, backend=win.BACKEND)
    assert undone.ok(), undone.to_dict()
    assert leaf.read_bytes() == gen_executor.content("deep", 4096)
    shutil.rmtree(win.longpath(long_root), ignore_errors=True)


@windows_only
def test_windows_robocopy_renaming_falls_back_to_shutil(tmp_path: Path) -> None:
    result = gen_executor.scenario(tmp_path)
    source = result.root / "keep" / "notes.txt"
    destination = result.target / "renamed.txt"
    primitive = win.BACKEND.move(str(source), str(destination))
    assert primitive.ok, primitive.detail
    assert destination.is_file()
    assert primitive.command == ("robocopy",) or primitive.command == ()
    assert primitive.note and "shutil.move" in primitive.note
