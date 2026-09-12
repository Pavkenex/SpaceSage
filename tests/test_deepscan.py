"""Deep-scan tests: the walk, the two hash passes, groups, hardlinks, the CLI.

The scenario is the live tree planted by ``tests/fixtures/gen_deepscan.py`` --
real files with exact sizes and timestamps, so every count here is arithmetic.
Reference point: :data:`gen_deepscan.NOW`.
"""

from __future__ import annotations

import hashlib
import json
import os
import stat
import subprocess
import sys
from collections.abc import Callable
from itertools import pairwise
from pathlib import Path

import pytest

from fixtures import gen_deepscan
from spacesage import deepscan
from spacesage.deepscan import (
    KEEP_POLICY,
    ROLE_DUPLICATE,
    ROLE_HARDLINK,
    ROLE_KEEP,
    DeepScanError,
    DuplicateGroup,
    GroupMember,
    HardlinkSet,
)

NOW = gen_deepscan.NOW
DAY = gen_deepscan.DAY
BLOCK = gen_deepscan.BLOCK
BIG = gen_deepscan.BIG
PREFIX = gen_deepscan.PREFIX
PARTIAL = gen_deepscan.PARTIAL

POSIX_ONLY = pytest.mark.skipif(os.name == "nt", reason="POSIX-only filesystem semantics")
windows_only = pytest.mark.skipif(os.name != "nt", reason="Windows-only path")


def _probe_file_identities() -> bool:
    """Does this machine's filesystem report ``st_dev``/``st_ino`` at all?"""
    import tempfile

    with tempfile.TemporaryDirectory() as tmp:
        probe = Path(tmp) / "probe.bin"
        probe.write_bytes(b"x")
        info = probe.stat()
    return bool(getattr(info, "st_ino", 0)) and bool(getattr(info, "st_dev", 0))


IDENTITY_FS = _probe_file_identities()
requires_identity = pytest.mark.skipif(
    not IDENTITY_FS, reason="the filesystem does not report file identities (no hardlinks)"
)


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #


def plant(tmp_path: Path) -> gen_deepscan.Tree:
    """Plant the standard tree under ``tmp_path``."""
    return gen_deepscan.plant(tmp_path / "tree")


def scan_tree(
    tree: gen_deepscan.Tree,
    *,
    min_size: int | None = None,
    progress: Callable[[deepscan.ScanProgress], None] | None = None,
) -> deepscan.ScanReport:
    """Scan the tree with the fixed reference point (``min_size``: the default)."""
    kwargs = {} if min_size is None else {"min_size": min_size}
    return deepscan.scan([str(tree.root)], now=NOW, progress=progress, **kwargs)


def group_of(report: deepscan.ScanReport, path: Path | str) -> DuplicateGroup | None:
    """The duplicate group holding ``path``, if any."""
    wanted = str(path)
    for group in report.groups:
        if any(member.path == wanted for member in group.members):
            return group
    return None


def set_of(report: deepscan.ScanReport, path: Path | str) -> HardlinkSet | None:
    """The hardlink set holding ``path``, if any."""
    wanted = str(path)
    for item in report.hardlink_sets:
        if any(member.path == wanted for member in item.members):
            return item
    return None


def member_of(group: DuplicateGroup | HardlinkSet, path: Path | str) -> GroupMember:
    """The member entry for ``path``."""
    wanted = str(path)
    for member in group.members:
        if member.path == wanted:
            return member
    raise AssertionError(f"{wanted} is not a member of the group")


def run_cli(*args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, "-m", "spacesage", *args],
        capture_output=True,
        text=True,
        check=False,
    )


def digest_of(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


# --------------------------------------------------------------------------- #
# Walk: counting, filtering, refusing bad roots
# --------------------------------------------------------------------------- #


def test_walk_counts_every_regular_file(tmp_path: Path) -> None:
    tree = plant(tmp_path)
    report = scan_tree(tree, min_size=0)
    assert report.stats.files == len(tree.files)
    assert report.stats.bytes == sum(path.stat().st_size for path in tree.files)
    assert report.stats.candidates == len(tree.files)
    assert report.stats.skipped_small == 0


def test_default_min_size_skips_small_files(tmp_path: Path) -> None:
    """Only the 1.1 MB pair clears the default 1 MiB floor."""
    tree = plant(tmp_path)
    report = scan_tree(tree)
    assert report.min_size == deepscan.DEFAULT_MIN_SIZE
    assert report.stats.candidates == 3  # big-a, big-b and the unique solo.bin
    assert report.stats.skipped_small == len(tree.files) - 3
    assert group_of(report, tree.big_one) is not None
    assert group_of(report, tree.small_one) is None
    assert group_of(report, tree.keep) is None


def test_min_size_zero_finds_the_small_duplicates(tmp_path: Path) -> None:
    tree = plant(tmp_path)
    report = scan_tree(tree, min_size=0)
    group = group_of(report, tree.small_one)
    assert group is not None
    assert group.size == 4_000
    assert group.copies == 2


def test_min_size_boundary_is_inclusive(tmp_path: Path) -> None:
    tree = plant(tmp_path)
    report = scan_tree(tree, min_size=BIG)
    assert report.stats.candidates == 2
    assert group_of(report, tree.big_one) is not None


def test_a_unique_size_is_never_hashed(tmp_path: Path) -> None:
    """``solo.bin`` is a candidate but its size has no rival: no read."""
    tree = plant(tmp_path)
    report = scan_tree(tree)
    assert report.stats.candidates == 3
    assert report.stats.partial_reads == 2
    assert report.stats.full_reads == 2


def test_empty_directory_is_a_clean_scan(tmp_path: Path) -> None:
    root = tmp_path / "empty"
    root.mkdir()
    report = deepscan.scan([str(root)], now=NOW)
    assert report.groups == ()
    assert report.hardlink_sets == ()
    assert report.stats.files == 0
    assert report.stats.errors == 0


def test_missing_root_is_a_hard_error(tmp_path: Path) -> None:
    with pytest.raises(DeepScanError, match="does not exist"):
        deepscan.scan([str(tmp_path / "nope")])


def test_file_as_root_is_a_hard_error(tmp_path: Path) -> None:
    target = tmp_path / "file.bin"
    target.write_bytes(b"x")
    with pytest.raises(DeepScanError, match="not a directory"):
        deepscan.scan([str(target)])


def test_no_root_is_a_hard_error() -> None:
    with pytest.raises(DeepScanError, match="at least one root"):
        deepscan.scan([])


def test_bad_thresholds_are_hard_errors(tmp_path: Path) -> None:
    root = str(tmp_path)
    with pytest.raises(DeepScanError, match="min_size"):
        deepscan.scan([root], min_size=-1)
    with pytest.raises(DeepScanError, match="partial_bytes"):
        deepscan.scan([root], partial_bytes=0)


def test_roots_are_normalized_to_absolute_paths(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    tree = plant(tmp_path)
    monkeypatch.chdir(tree.root)
    report = deepscan.scan(["data"], now=NOW, min_size=0)
    assert report.roots == (str(tree.root / "data"),)
    # Only what lives inside data/ is in the report: the trio needs its copies.
    assert group_of(report, tree.keep) is None
    assert group_of(report, tree.daily) is not None


def test_nested_roots_are_scanned_once(tmp_path: Path) -> None:
    tree = plant(tmp_path)
    report = deepscan.scan([str(tree.root), str(tree.keep.parent)], now=NOW, min_size=0)
    assert report.roots == (str(tree.root),)
    assert report.notes and "inside" in report.notes[0]
    paths = [member.path for group in report.groups for member in group.members]
    assert len(paths) == len(set(paths))


def test_a_root_given_twice_is_scanned_once(tmp_path: Path) -> None:
    tree = plant(tmp_path)
    report = deepscan.scan([str(tree.root), str(tree.root)], now=NOW, min_size=0)
    assert report.roots == (str(tree.root),)
    paths = [member.path for group in report.groups for member in group.members]
    assert len(paths) == len(set(paths))


def test_multiple_roots_are_scanned(tmp_path: Path) -> None:
    first = gen_deepscan.plant(tmp_path / "one")
    second = gen_deepscan.plant(tmp_path / "two")
    report = deepscan.scan([str(first.root), str(second.root)], now=NOW, min_size=0)
    assert len(report.roots) == 2
    assert group_of(report, first.keep) is not None
    assert group_of(report, second.keep) is not None


@POSIX_ONLY
def test_unreadable_directory_is_reported_and_the_scan_continues(tmp_path: Path) -> None:
    tree = plant(tmp_path)
    locked = tree.root / "locked"
    locked.mkdir()
    (locked / "hidden.bin").write_bytes(gen_deepscan.blob("hidden", 5_000))
    locked.chmod(0)
    try:
        report = scan_tree(tree, min_size=0)
    finally:
        locked.chmod(0o700)
    assert report.stats.errors == 1
    assert report.stats.error_samples[0].path == str(locked)
    assert "cannot list" in report.stats.error_samples[0].reason
    assert group_of(report, tree.keep) is not None  # the rest was still scanned


@POSIX_ONLY
def test_unreadable_file_is_dropped(tmp_path: Path) -> None:
    tree = plant(tmp_path)
    victim = tree.trap
    victim.chmod(0)
    try:
        report = scan_tree(tree, min_size=0)
    finally:
        victim.chmod(0o600)
    assert report.stats.unreadable == 1
    assert any("cannot read" in issue.reason for issue in report.stats.error_samples)
    assert group_of(report, tree.keep) is not None


@POSIX_ONLY
def test_special_files_are_counted_but_not_hashed(tmp_path: Path) -> None:
    tree = plant(tmp_path)
    fifo = tree.root / "data" / "pipe"
    os.mkfifo(fifo)
    report = scan_tree(tree, min_size=0)
    assert report.stats.skipped_special == 1
    assert report.stats.files == len(tree.files)
    assert group_of(report, tree.keep) is not None


def test_symlinked_file_is_counted_and_skipped(tmp_path: Path) -> None:
    tree = plant(tmp_path)
    if not tree.symlinks:
        pytest.skip("symlinks are not available on this platform")
    assert tree.link_file is not None
    report = scan_tree(tree, min_size=0)
    assert report.stats.skipped_links >= 2  # the file link and the directory link
    paths = [member.path for group in report.groups for member in group.members]
    assert str(tree.link_file) not in paths
    assert report.stats.candidates == len(tree.files)  # links are not candidates


def test_symlinked_directory_is_never_followed(tmp_path: Path) -> None:
    tree = plant(tmp_path)
    if not tree.symlinks:
        pytest.skip("symlinks are not available on this platform")
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "foreign.bin").write_bytes(gen_deepscan.blob("foreign", 5_000))
    (tree.root / "data" / "real").rmdir()
    (tree.root / "data" / "real").symlink_to(outside, target_is_directory=True)
    report = scan_tree(tree, min_size=0)
    # The 5,000-byte blob lives only outside the tree and is reachable only
    # through the link: following it would make it a candidate.
    assert report.stats.candidates == len(tree.files)
    assert report.stats.skipped_links >= 3  # file link, dir link, replaced dir link
    paths = [member.path for group in report.groups for member in group.members]
    assert not any("foreign" in path for path in paths)


def test_root_may_not_be_a_symlink(tmp_path: Path) -> None:
    target = tmp_path / "target"
    target.mkdir()
    link = tmp_path / "link"
    try:
        link.symlink_to(target, target_is_directory=True)
    except OSError:
        pytest.skip("symlinks are not available on this platform")
    with pytest.raises(DeepScanError, match="never follows links"):
        deepscan.scan([str(link)])


@windows_only
def test_junction_inside_the_tree_is_not_followed(tmp_path: Path) -> None:
    tree = plant(tmp_path)
    target = tmp_path / "junction-target"
    target.mkdir()
    (target / "foreign.bin").write_bytes(gen_deepscan.blob("foreign", 5_000))
    junction = tree.root / "data" / "junction"
    result = subprocess.run(
        ["cmd", "/c", "mklink", "/J", str(junction), str(target)],
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:  # pragma: no cover - runner without junction support
        pytest.skip(f"mklink /J failed: {result.stderr.strip() or result.stdout.strip()}")
    report = scan_tree(tree, min_size=0)
    assert report.stats.skipped_links >= 1
    paths = [member.path for group in report.groups for member in group.members]
    assert not any("foreign" in path for path in paths)
    assert report.stats.candidates == len(tree.files)


def test_a_file_changed_mid_scan_is_dropped(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    tree = plant(tmp_path)
    real_hash = deepscan._hash_file
    touched: list[str] = []

    def flaky(path: str, limit: int | None) -> tuple[str, int]:
        digest, read = real_hash(path, limit)
        if path == str(tree.prefix_one) and not touched:
            touched.append(path)
            tree.prefix_one.write_bytes(b"changed after the read")
        return digest, read

    monkeypatch.setattr(deepscan, "_hash_file", flaky)
    report = scan_tree(tree, min_size=0)
    assert report.stats.changed == 1
    assert any("changed" in issue.reason for issue in report.stats.error_samples)
    assert group_of(report, tree.keep) is not None


# --------------------------------------------------------------------------- #
# Duplicate detection
# --------------------------------------------------------------------------- #


def test_exact_duplicates_are_grouped(tmp_path: Path) -> None:
    tree = plant(tmp_path)
    report = scan_tree(tree, min_size=0)
    group = group_of(report, tree.keep)
    assert group is not None
    assert group.size == BLOCK
    assert group.sha256 == digest_of(tree.keep)
    assert group.partial_sha256 == hashlib.sha256(tree.keep.read_bytes()[:PARTIAL]).hexdigest()


def test_same_size_different_content_is_not_a_group(tmp_path: Path) -> None:
    """The trap file shares its size with the trio but not one byte of it."""
    tree = plant(tmp_path)
    report = scan_tree(tree, min_size=0)
    group = group_of(report, tree.keep)
    assert group is not None
    assert tree.trap.stat().st_size == BLOCK
    assert group_of(report, tree.trap) is None
    assert group.paths == 4  # the trio plus its hard link, the trap is not one


def test_a_shared_prefix_is_not_enough(tmp_path: Path) -> None:
    tree = plant(tmp_path)
    report = scan_tree(tree, min_size=0)
    assert group_of(report, tree.prefix_one) is None
    assert group_of(report, tree.prefix_two) is None
    # Both were read in full before the verdict: the partial pass matched.
    assert report.stats.full_reads >= 2


def test_files_up_to_the_partial_window_are_read_once(tmp_path: Path) -> None:
    """4,000-byte duplicates: the partial read *is* the full content."""
    tree = plant(tmp_path)
    report = scan_tree(tree, min_size=0)
    group = group_of(report, tree.small_one)
    assert group is not None
    assert group.sha256 == digest_of(tree.small_one)
    assert group.sha256 == group.partial_sha256


def test_only_files_over_the_partial_window_get_a_second_read(tmp_path: Path) -> None:
    """A tree with nothing but small duplicates: no full read at all."""
    root = tmp_path / "smalltree"
    root.mkdir()
    for name in ("a.bin", "b.bin"):
        (root / name).write_bytes(gen_deepscan.blob("tiny", 1_000))
    report = deepscan.scan([str(root)], now=NOW, min_size=0)
    assert report.stats.partial_reads == 2
    assert report.stats.full_reads == 0
    assert report.groups[0].sha256 == digest_of(root / "a.bin")


@requires_identity
def test_reclaimable_bytes_count_copies_not_paths(tmp_path: Path) -> None:
    """The trio plus its hard link: 4 paths, 3 copies, 2 reclaimable."""
    tree = plant(tmp_path)
    report = scan_tree(tree, min_size=0)
    group = group_of(report, tree.keep)
    assert group is not None
    assert group.copies == 3
    assert group.paths == 4
    assert group.reclaimable_bytes == 2 * BLOCK
    assert group.linked_bytes == BLOCK


def test_groups_are_sorted_by_reclaimable_bytes(tmp_path: Path) -> None:
    tree = plant(tmp_path)
    report = scan_tree(tree, min_size=0)
    reclaimable = [group.reclaimable_bytes for group in report.groups]
    assert reclaimable == sorted(reclaimable, reverse=True)
    assert report.groups[0].size == BIG
    assert [group.group_id for group in report.groups] == [
        f"g{index}" for index in range(1, len(report.groups) + 1)
    ]


def test_summary_totals_match_the_groups(tmp_path: Path) -> None:
    tree = plant(tmp_path)
    report = scan_tree(tree, min_size=0)
    summary = report.summary
    assert summary.groups == len(report.groups)
    assert summary.reclaimable_bytes == sum(group.reclaimable_bytes for group in report.groups)
    assert summary.paths == sum(group.paths for group in report.groups)
    assert summary.copies == sum(group.copies for group in report.groups)
    assert summary.mixed_volume_groups == 0  # one temporary filesystem
    assert summary.same_volume_groups == len(report.groups)


def test_progress_reports_the_walk_and_the_end(tmp_path: Path) -> None:
    tree = plant(tmp_path)
    seen: list[deepscan.ScanProgress] = []
    report = deepscan.scan([str(tree.root)], now=NOW, min_size=0, progress=seen.append)
    assert seen[-1].stage == "done"
    assert seen[-1].files == report.stats.files
    assert seen[-1].candidates == report.stats.candidates
    assert seen[-1].hashed == report.stats.partial_reads + report.stats.full_reads
    assert all(snapshot.elapsed_s >= 0.0 for snapshot in seen)
    assert all(earlier.files <= later.files for earlier, later in pairwise(seen))


def test_scan_is_deterministic_for_a_static_tree(tmp_path: Path) -> None:
    tree = plant(tmp_path)
    first = scan_tree(tree, min_size=0).to_dict()
    second = scan_tree(tree, min_size=0).to_dict()
    assert first["groups"] == second["groups"]
    assert first["hardlink_sets"] == second["hardlink_sets"]
    assert first["summary"] == second["summary"]
    assert first["roots"] == second["roots"]


# --------------------------------------------------------------------------- #
# Hard links
# --------------------------------------------------------------------------- #


@requires_identity
def test_hardlinked_paths_alone_are_a_hardlink_set(tmp_path: Path) -> None:
    root = tmp_path / "links"
    root.mkdir()
    origin = root / "origin.bin"
    origin.write_bytes(gen_deepscan.blob("linked", 200_000))
    twin = root / "twin.bin"
    os.link(origin, twin)
    report = deepscan.scan([str(root)], now=NOW, min_size=0)
    assert report.groups == ()
    assert len(report.hardlink_sets) == 1
    item = report.hardlink_sets[0]
    assert item.paths == 2
    assert item.size == 200_000
    assert item.linked_bytes == 200_000
    assert item.sha256 == digest_of(origin)
    assert item.keep == str(twin)  # one file, one timestamp: the shorter path wins
    assert item.set_id == "h1"


@requires_identity
def test_hardlink_set_members_carry_their_roles(tmp_path: Path) -> None:
    root = tmp_path / "links"
    root.mkdir()
    origin = root / "origin.bin"
    origin.write_bytes(gen_deepscan.blob("linked", 200_000))
    twin = root / "twin.bin"
    os.link(origin, twin)
    report = deepscan.scan([str(root)], now=NOW, min_size=0)
    item = report.hardlink_sets[0]
    assert member_of(item, twin).role == ROLE_KEEP
    linked = member_of(item, origin)
    assert linked.role == ROLE_HARDLINK
    assert linked.same_as == str(twin)
    assert linked.links == 2


@requires_identity
def test_the_trio_group_marks_its_hard_link_as_a_link(tmp_path: Path) -> None:
    tree = plant(tmp_path)
    report = scan_tree(tree, min_size=0)
    group = group_of(report, tree.keep)
    assert group is not None
    assert member_of(group, tree.keep).role == ROLE_KEEP
    link = member_of(group, tree.keep_link)
    assert link.role == ROLE_HARDLINK
    assert link.same_as == str(tree.keep)
    assert member_of(group, tree.copy).role == ROLE_DUPLICATE
    assert member_of(group, tree.another).role == ROLE_DUPLICATE


@requires_identity
def test_hardlinked_paths_are_not_read_twice(tmp_path: Path) -> None:
    tree = plant(tmp_path)
    report = scan_tree(tree, min_size=0)
    assert report.stats.reused_reads == 2  # partial + full digest reused
    naive = report.stats.candidates  # one read per path would be more
    assert report.stats.partial_reads + report.stats.full_reads < naive * 2


@requires_identity
def test_hardlinked_bytes_are_not_reclaimable(tmp_path: Path) -> None:
    """Deleting a hard link frees nothing, so it must not inflate the gain."""
    root = tmp_path / "links"
    root.mkdir()
    origin = root / "origin.bin"
    origin.write_bytes(gen_deepscan.blob("linked", 200_000))
    os.link(origin, root / "twin.bin")
    report = deepscan.scan([str(root)], now=NOW, min_size=0)
    assert report.summary.reclaimable_bytes == 0
    assert report.summary.linked_bytes == 200_000


# --------------------------------------------------------------------------- #
# Keep policy
# --------------------------------------------------------------------------- #


def test_keep_policy_prefers_the_newest_copy(tmp_path: Path) -> None:
    tree = plant(tmp_path)
    report = scan_tree(tree, min_size=0)
    group = group_of(report, tree.daily)
    assert group is not None
    assert group.keep == str(tree.daily)  # 1 day old beats 40 days
    assert "newest" in group.keep_reason
    assert "39 d newer" in group.keep_reason


def test_keep_policy_breaks_ties_with_the_shortest_path(tmp_path: Path) -> None:
    tree = plant(tmp_path)
    report = scan_tree(tree, min_size=0)
    group = group_of(report, tree.tie)
    assert group is not None
    assert group.keep == str(tree.tie)
    assert member_of(group, tree.tie).mtime == member_of(group, tree.tie_deep).mtime
    assert "shortest" in group.keep_reason


def test_keep_policy_is_recorded_on_every_group(tmp_path: Path) -> None:
    tree = plant(tmp_path)
    report = scan_tree(tree, min_size=0)
    assert report.groups
    assert all(group.keep_policy == KEEP_POLICY for group in report.groups)
    assert KEEP_POLICY == "newest-then-shortest-path"


@requires_identity
def test_identical_timestamps_are_reported_as_a_tie(tmp_path: Path) -> None:
    """keep.bin and its hard link share one timestamp: the shorter path wins."""
    tree = plant(tmp_path)
    report = scan_tree(tree, min_size=0)
    group = group_of(report, tree.keep)
    assert group is not None
    assert group.keep == str(tree.keep)
    assert group.keep_reason.startswith("shortest of the 2 paths modified")
    assert "newest" not in group.keep_reason  # two paths tie for newest


def test_keep_reason_without_timestamps() -> None:
    """Exotic filesystems may report no mtime; the policy says so."""
    plain = SimpleFile("/a/b.bin", mtime=None)
    other = SimpleFile("/a/b.bin", mtime=None)
    reason = deepscan._keep_reason([plain, other], plain)  # type: ignore[arg-type]
    assert reason == "shortest of 2 paths (none carries a usable timestamp)"


def test_keep_reason_when_only_some_paths_are_stamped() -> None:
    stamped = SimpleFile("/a/b.bin", mtime=NOW)
    undated = SimpleFile("/c/d.bin", mtime=None)
    reason = deepscan._keep_reason([stamped, undated], stamped)  # type: ignore[arg-type]
    assert "newest of 2 paths" in reason
    assert "1 of them have no timestamp" in reason


class SimpleFile:
    """Minimal duck type for the keep-reason branches a filesystem cannot produce."""

    def __init__(self, path: str, *, mtime: int | None) -> None:
        self.path = path
        self.mtime = mtime
        self.size = 1
        self.mtime_ns = 0
        self.dev = None
        self.ino = None
        self.links = 1


# --------------------------------------------------------------------------- #
# Same-volume hardlink dedupe suggestions
# --------------------------------------------------------------------------- #


def member(path: str, *, dev: int | None = 1, ino: int | None = 10) -> GroupMember:
    return GroupMember(
        path=path, size=100, mtime=NOW, dev=dev, ino=ino, links=1, role=ROLE_DUPLICATE
    )


@requires_identity
def test_hardlink_suggestion_is_feasible_on_one_volume(tmp_path: Path) -> None:
    tree = plant(tmp_path)
    report = scan_tree(tree, min_size=0)
    group = group_of(report, tree.keep)
    assert group is not None
    plan = group.hardlink
    assert plan.feasible
    assert plan.link_to == str(tree.keep)
    assert sorted(plan.relink) == sorted([str(tree.copy), str(tree.another)])
    assert plan.reclaimable_bytes == group.reclaimable_bytes
    assert "one volume" in plan.reason


@requires_identity
def test_hardlink_suggestion_leaves_existing_links_alone(tmp_path: Path) -> None:
    """The link to the kept path already shares its payload: nothing to do."""
    tree = plant(tmp_path)
    report = scan_tree(tree, min_size=0)
    group = group_of(report, tree.keep)
    assert group is not None
    assert str(tree.keep_link) not in group.hardlink.relink


def test_hardlink_suggestion_rejects_multiple_volumes() -> None:
    members = [
        member("/mnt/one/a.bin", dev=1, ino=10),
        member("/mnt/two/b.bin", dev=2, ino=11),
    ]
    plan = deepscan.hardlink_dedupe(100, members, "/mnt/one/a.bin")
    assert not plan.feasible
    assert "cannot cross volumes" in plan.reason
    assert plan.relink == ("/mnt/two/b.bin",)
    assert plan.reclaimable_bytes == 100


def test_hardlink_suggestion_rejects_an_unknown_volume() -> None:
    members = [member("/a/a.bin", dev=None, ino=10), member("/b/b.bin", dev=None, ino=11)]
    plan = deepscan.hardlink_dedupe(100, members, "/a/a.bin")
    assert not plan.feasible
    assert "volume" in plan.reason


def test_hardlink_suggestion_rejects_unknown_file_identities() -> None:
    members = [member("/a/a.bin", dev=1, ino=0), member("/b/b.bin", dev=1, ino=0)]
    plan = deepscan.hardlink_dedupe(100, members, "/a/a.bin")
    assert not plan.feasible
    assert "file identities" in plan.reason


def test_hardlink_suggestion_needs_a_second_copy() -> None:
    members = [member("/a/a.bin", dev=1, ino=10), member("/a/a-link.bin", dev=1, ino=10)]
    plan = deepscan.hardlink_dedupe(100, members, "/a/a.bin")
    assert not plan.feasible
    assert plan.relink == ()
    assert "same physical file" in plan.reason


def test_hardlink_suggestion_without_members() -> None:
    plan = deepscan.hardlink_dedupe(100, [], "/a/a.bin")
    assert not plan.feasible
    assert plan.link_to is None


# --------------------------------------------------------------------------- #
# Read-only guarantee
# --------------------------------------------------------------------------- #


def test_scan_leaves_the_tree_untouched(tmp_path: Path) -> None:
    tree = plant(tmp_path)
    before = {
        path: (path.stat().st_size, path.stat().st_mtime_ns, digest_of(path)) for path in tree.files
    }
    listing_before = sorted(str(item) for item in tree.root.rglob("*"))
    scan_tree(tree, min_size=0)
    after = {
        path: (path.stat().st_size, path.stat().st_mtime_ns, digest_of(path)) for path in tree.files
    }
    assert before == after
    assert sorted(str(item) for item in tree.root.rglob("*")) == listing_before


def test_scan_of_a_lone_file_tree_creates_nothing(tmp_path: Path) -> None:
    root = tmp_path / "solo"
    root.mkdir()
    (root / "only.bin").write_bytes(b"data")
    report = deepscan.scan([str(root)], now=NOW)
    assert report.stats.files == 1
    assert sorted(item.name for item in root.iterdir()) == ["only.bin"]


# --------------------------------------------------------------------------- #
# Rendering
# --------------------------------------------------------------------------- #


@requires_identity
def test_text_report_mentions_the_key_facts(tmp_path: Path) -> None:
    tree = plant(tmp_path)
    text = deepscan.render_text(scan_tree(tree, min_size=0))
    group = group_of(scan_tree(tree, min_size=0), tree.keep)
    assert group is not None
    assert f"roots: {tree.root}" in text
    assert "g1" in text
    assert f"keep: {group.keep}" in text
    assert "dedupe: re-create" in text
    assert "same file as" in text  # the hard link of the kept path
    assert "already saved" in text
    assert "reclaimable" in text


@requires_identity
def test_text_report_lists_hardlink_sets(tmp_path: Path) -> None:
    root = tmp_path / "links"
    root.mkdir()
    origin = root / "origin.bin"
    origin.write_bytes(gen_deepscan.blob("linked", 200_000))
    os.link(origin, root / "twin.bin")
    text = deepscan.render_text(deepscan.scan([str(root)], now=NOW, min_size=0))
    assert "h1" in text
    assert "nothing to reclaim" in text


def test_text_top_limits_the_listing(tmp_path: Path) -> None:
    tree = plant(tmp_path)
    report = scan_tree(tree, min_size=0)
    assert len(report.groups) >= 3
    text = deepscan.render_text(report, top=1)
    assert "g1" in text
    assert "g2" not in text
    assert f"({len(report.groups) - 1} more groups; --top 0 lists every one)" in text
    full = deepscan.render_text(report, top=0)
    assert "g2" in full
    assert "more groups" not in full


def test_text_rejects_a_negative_top(tmp_path: Path) -> None:
    tree = plant(tmp_path)
    with pytest.raises(DeepScanError, match="top"):
        deepscan.render_text(scan_tree(tree, min_size=0), top=-1)


def test_text_lists_the_errors(tmp_path: Path) -> None:
    tree = plant(tmp_path)
    report = scan_tree(tree, min_size=0)
    errors = deepscan.ScanIssue(path="/some/path", reason="cannot read: denied")
    patched = deepscan.ScanReport(
        roots=report.roots,
        notes=report.notes,
        min_size=report.min_size,
        partial_bytes=report.partial_bytes,
        as_of=report.as_of,
        elapsed_s=report.elapsed_s,
        stats=deepscan.ScanStats(
            files=report.stats.files,
            bytes=report.stats.bytes,
            candidates=report.stats.candidates,
            candidate_bytes=report.stats.candidate_bytes,
            skipped_small=report.stats.skipped_small,
            skipped_links=report.stats.skipped_links,
            skipped_special=report.stats.skipped_special,
            unreadable=report.stats.unreadable,
            changed=report.stats.changed,
            reused_reads=report.stats.reused_reads,
            partial_reads=report.stats.partial_reads,
            full_reads=report.stats.full_reads,
            bytes_read=report.stats.bytes_read,
            errors=1,
            error_samples=(errors,),
        ),
        groups=report.groups,
        hardlink_sets=report.hardlink_sets,
    )
    text = deepscan.render_text(patched)
    assert "errors (1):" in text
    assert "/some/path: cannot read: denied" in text


def test_json_report_matches_the_schema(tmp_path: Path) -> None:
    tree = plant(tmp_path)
    document = json.loads(deepscan.render_json(scan_tree(tree, min_size=0)))
    assert document["schema"] == "spacesage.deepscan/v1"
    assert document["roots"] == [str(tree.root)]
    assert document["as_of"].endswith("+00:00")
    assert document["thresholds"] == {
        "min_size": 0,
        "partial_bytes": PARTIAL,
        "keep_policy": KEEP_POLICY,
    }
    stats = document["stats"]
    for key in (
        "files",
        "bytes",
        "candidates",
        "candidate_bytes",
        "skipped_small",
        "skipped_links",
        "skipped_special",
        "unreadable",
        "changed",
        "reused_reads",
        "partial_reads",
        "full_reads",
        "bytes_read",
        "errors",
        "error_samples",
    ):
        assert key in stats
    summary = document["summary"]
    for key in (
        "groups",
        "paths",
        "copies",
        "reclaimable_bytes",
        "hardlink_sets",
        "hardlink_paths",
        "linked_bytes",
        "same_volume_groups",
        "mixed_volume_groups",
    ):
        assert key in summary
    group = document["groups"][0]
    for key in (
        "id",
        "size",
        "copies",
        "paths",
        "reclaimable_bytes",
        "linked_bytes",
        "sha256",
        "partial_sha256",
        "keep",
        "keep_policy",
        "keep_reason",
        "members",
        "hardlink",
    ):
        assert key in group
    member_entry = group["members"][0]
    for key in ("path", "size", "mtime", "dev", "ino", "links", "role", "same_as"):
        assert key in member_entry
    assert group["hardlink"]["feasible"] is True
    assert set(group["hardlink"]) == {
        "feasible",
        "link_to",
        "relink",
        "reclaimable_bytes",
        "reason",
    }


def test_json_member_mtimes_are_iso_utc(tmp_path: Path) -> None:
    tree = plant(tmp_path)
    document = json.loads(deepscan.render_json(scan_tree(tree, min_size=0)))
    stamps = [member["mtime"] for group in document["groups"] for member in group["members"]]
    assert stamps and all(stamp.endswith("+00:00") for stamp in stamps)


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #


def test_cli_prints_the_groups(tmp_path: Path) -> None:
    tree = plant(tmp_path)
    result = run_cli("deepscan", str(tree.root), "--min-size", "0")
    assert result.returncode == 0
    assert "groups:" in result.stdout
    assert f"keep: {tree.keep}" in result.stdout
    assert "dedupe:" in result.stdout


def test_cli_json_matches_the_engine(tmp_path: Path) -> None:
    tree = plant(tmp_path)
    result = run_cli("deepscan", str(tree.root), "--min-size", "0", "--json")
    assert result.returncode == 0
    document = json.loads(result.stdout)
    engine = scan_tree(tree, min_size=0).to_dict()
    assert document["schema"] == "spacesage.deepscan/v1"
    assert document["groups"] == engine["groups"]
    assert document["summary"] == engine["summary"]


def test_cli_min_size_filters(tmp_path: Path) -> None:
    tree = plant(tmp_path)
    result = run_cli("deepscan", str(tree.root))
    assert result.returncode == 0
    assert f"keep: {tree.big_one}" in result.stdout  # the 1.1 MB pair survives
    assert str(tree.small_one) not in result.stdout
    assert "skipped" in result.stdout


def test_cli_progress_goes_to_stderr(tmp_path: Path) -> None:
    tree = plant(tmp_path)
    result = run_cli("deepscan", str(tree.root), "--min-size", "0", "--progress")
    assert result.returncode == 0
    assert "progress: done:" in result.stderr
    assert "progress:" not in result.stdout


def test_cli_top_zero_lists_every_group(tmp_path: Path) -> None:
    tree = plant(tmp_path)
    result = run_cli("deepscan", str(tree.root), "--min-size", "0", "--top", "1")
    assert result.returncode == 0
    assert "more groups; --top 0 lists every one" in result.stdout
    every = run_cli("deepscan", str(tree.root), "--min-size", "0", "--top", "0")
    assert "more groups" not in every.stdout


def test_cli_reports_a_missing_root(tmp_path: Path) -> None:
    result = run_cli("deepscan", str(tmp_path / "nope"))
    assert result.returncode == 1
    assert "does not exist" in result.stderr


def test_cli_rejects_a_bad_size(tmp_path: Path) -> None:
    result = run_cli("deepscan", str(tmp_path), "--min-size", "nope")
    assert result.returncode == 2
    assert "not a size" in result.stderr


def test_cli_rejects_a_negative_top(tmp_path: Path) -> None:
    tree = plant(tmp_path)
    result = run_cli("deepscan", str(tree.root), "--top", "-1")
    assert result.returncode == 1
    assert "top must not be negative" in result.stderr


# --------------------------------------------------------------------------- #
# Demo (the acceptance run)
# --------------------------------------------------------------------------- #


def test_demo_run_on_a_generated_tree(tmp_path: Path) -> None:
    """The slice's acceptance check, automated: plant, scan, read the report."""
    tree = gen_deepscan.plant(tmp_path / "demo")
    report = deepscan.scan([str(tree.root)], now=NOW, min_size=0)
    summary = report.summary
    assert summary.groups >= 5
    assert summary.reclaimable_bytes == 2 * BLOCK + BIG + 40_000 + 20_000 + 4_000
    assert summary.linked_bytes >= BLOCK
    text = deepscan.render_text(report)
    assert text.startswith("roots: ")


def test_stats_and_fixture_sizes_stay_in_sync(tmp_path: Path) -> None:
    """Guard rail: the planted sizes are what the tests above assume."""
    tree = plant(tmp_path)
    assert tree.keep.stat().st_size == BLOCK
    assert tree.big_one.stat().st_size == BIG
    assert tree.prefix_one.stat().st_size == PREFIX > PARTIAL
    assert tree.small_one.stat().st_size == 4_000 < deepscan.DEFAULT_MIN_SIZE
    assert stat.S_ISREG(tree.keep_link.stat().st_mode)
