"""Synthetic on-disk tree for the deep-scan tests and the demo run.

Unlike the export fixtures (``gen.py``, ``gen_candidates.py``, ``gen_plan.py``)
this one plants **real files**: the deep scan runs where the files are, so its
tests need a live tree with exact sizes and timestamps -- every count in
``tests/test_deepscan.py`` is arithmetic, not an estimate.

One of each interesting case:

- ``data/keep.bin`` + ``backup/copy.bin`` + ``archive/another.bin`` -- the same
  300,000 bytes under three names: three physical copies, two reclaimable.
- ``links/keep-link.bin`` -- a hard link to ``data/keep.bin``: a fourth path
  that frees nothing and must never be counted as another copy.
- ``data/trap.bin`` -- *same size*, different bytes (the same-size trap).
- ``data/prefix-1.bin`` / ``data/prefix-2.bin`` -- identical first 64 KiB,
  different tails (the partial-hash trap: only the full read settles it).
- ``big/big-a.bin`` / ``big/big-b.bin`` -- 1.1 MB duplicates above the default
  1 MiB floor; ``big/solo.bin`` -- big but unique, so never hashed.
- ``data/daily.bin`` (1 day old) vs ``data/weekly.bin`` (40 days old) -- the
  keep policy must pick the newest.
- ``data/tie.bin`` vs ``data/tie/deep/tie.bin`` -- equal timestamps, so the
  shortest path wins.
- ``small/small-a.bin`` / ``small-b.bin`` -- 4,000-byte duplicates below the
  default floor.
- ``data/real/`` with ``data/link-dir`` and ``data/link-file.bin`` pointing at
  it -- links the scan must count and never follow (``Tree.symlinks`` says
  whether the platform let us create them).

Run it directly to plant the tree for a manual demo::

    python tests/fixtures/gen_deepscan.py --root /tmp/deepscan-demo
"""

from __future__ import annotations

import argparse
import hashlib
import os
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

MIB = 1024**2
"""One mebibyte, in bytes."""

DAY = 86_400
"""One day, in seconds."""

NOW = int(datetime(2026, 9, 12, 12, 0, tzinfo=UTC).timestamp())
"""Reference point every planted timestamp is derived from."""

BLOCK = 300_000
"""Size of the three-copy duplicate (300,000 bytes, comfortably over 64 KiB)."""

BIG = 1_100_000
"""Size of the pair above the default 1 MiB floor."""

PREFIX = 100_000
"""Size of the two files that share their first 64 KiB."""

PARTIAL = 64 * 1024
"""The scan's partial-hash window (kept in sync with ``deepscan``)."""


def blob(tag: str, size: int) -> bytes:
    """Deterministic bytes for ``tag``: a repeated sha256 of the tag."""
    seed = hashlib.sha256(tag.encode("utf-8")).digest()
    return (seed * (size // len(seed) + 1))[:size]


@dataclass(frozen=True)
class Tree:
    """Every path the generator planted, so tests never guess a name."""

    root: Path
    keep: Path
    copy: Path
    another: Path
    keep_link: Path
    trap: Path
    prefix_one: Path
    prefix_two: Path
    daily: Path
    weekly: Path
    tie: Path
    tie_deep: Path
    small_one: Path
    small_two: Path
    big_one: Path
    big_two: Path
    solo: Path
    real: Path
    link_dir: Path | None
    link_file: Path | None
    symlinks: bool

    @property
    def files(self) -> tuple[Path, ...]:
        """Every regular file the tree holds (includes the hard link)."""
        return (
            self.keep,
            self.copy,
            self.another,
            self.keep_link,
            self.trap,
            self.prefix_one,
            self.prefix_two,
            self.daily,
            self.weekly,
            self.tie,
            self.tie_deep,
            self.small_one,
            self.small_two,
            self.big_one,
            self.big_two,
            self.solo,
        )


def _write(path: Path, data: bytes, *, mtime: int) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    os.utime(path, (mtime, mtime))
    return path


def _hardlink(source: Path, target: Path) -> Path:
    """Create ``target`` as a hard link to ``source`` (re-planting is fine)."""
    target.parent.mkdir(parents=True, exist_ok=True)
    if target.exists() or target.is_symlink():
        target.unlink()
    os.link(source, target)
    return target


def _symlink(source: Path, target: Path, *, directory: bool) -> Path:
    """Create ``target`` pointing at ``source`` (re-planting is fine)."""
    if target.exists() or target.is_symlink():
        target.unlink()
    target.symlink_to(source, target_is_directory=directory)
    return target


def plant(root: Path, *, now: int = NOW) -> Tree:
    """Plant the tree under ``root`` (created if needed) and return its paths."""
    root = Path(root)
    root.mkdir(parents=True, exist_ok=True)
    data = root / "data"
    keep = _write(data / "keep.bin", blob("keep", BLOCK), mtime=now - 10 * DAY)
    copy = _write(root / "backup" / "copy.bin", blob("keep", BLOCK), mtime=now - 20 * DAY)
    another = _write(root / "archive" / "another.bin", blob("keep", BLOCK), mtime=now - 30 * DAY)
    keep_link = _hardlink(keep, root / "links" / "keep-link.bin")
    trap = _write(data / "trap.bin", blob("trap", BLOCK), mtime=now - 10 * DAY)
    prefix_one = _write(
        data / "prefix-1.bin",
        blob("prefix", PARTIAL) + b"1" * (PREFIX - PARTIAL),
        mtime=now - 10 * DAY,
    )
    prefix_two = _write(
        data / "prefix-2.bin",
        blob("prefix", PARTIAL) + b"2" * (PREFIX - PARTIAL),
        mtime=now - 10 * DAY,
    )
    daily = _write(data / "daily.bin", blob("daily", 40_000), mtime=now - DAY)
    weekly = _write(data / "weekly.bin", blob("daily", 40_000), mtime=now - 40 * DAY)
    tie = _write(data / "tie.bin", blob("tie", 20_000), mtime=now - 5 * DAY)
    tie_deep = _write(data / "tie" / "deep" / "tie.bin", blob("tie", 20_000), mtime=now - 5 * DAY)
    small_one = _write(root / "small" / "small-a.bin", blob("small", 4_000), mtime=now)
    small_two = _write(root / "small" / "small-b.bin", blob("small", 4_000), mtime=now)
    big_one = _write(root / "big" / "big-a.bin", blob("big", BIG), mtime=now - 10 * DAY)
    big_two = _write(root / "big" / "big-b.bin", blob("big", BIG), mtime=now - 10 * DAY)
    solo = _write(root / "big" / "solo.bin", blob("solo", 1_050_000), mtime=now - 10 * DAY)
    real = root / "data" / "real"
    real.mkdir(parents=True, exist_ok=True)
    link_dir: Path | None = None
    link_file: Path | None = None
    symlinks = True
    try:
        link_dir = _symlink(real, data / "link-dir", directory=True)
        link_file = _symlink(keep, data / "link-file.bin", directory=False)
        if not link_dir.is_symlink() or not link_file.is_symlink():  # pragma: no cover
            symlinks = False
    except (OSError, NotImplementedError):  # pragma: no cover - platform limit
        symlinks = False
        link_dir = None
        link_file = None
    return Tree(
        root=root,
        keep=keep,
        copy=copy,
        another=another,
        keep_link=keep_link,
        trap=trap,
        prefix_one=prefix_one,
        prefix_two=prefix_two,
        daily=daily,
        weekly=weekly,
        tie=tie,
        tie_deep=tie_deep,
        small_one=small_one,
        small_two=small_two,
        big_one=big_one,
        big_two=big_two,
        solo=solo,
        real=real,
        link_dir=link_dir,
        link_file=link_file,
        symlinks=symlinks,
    )


def main(argv: list[str] | None = None) -> int:
    """Plant the tree (``--root``) so the CLI can be run over it by hand."""
    parser = argparse.ArgumentParser(
        description="Plant the deterministic deep-scan demo tree under --root."
    )
    parser.add_argument(
        "--root",
        type=Path,
        required=True,
        help="directory to plant the tree in (created if missing)",
    )
    args = parser.parse_args(argv)
    tree = plant(args.root)
    print(f"planted {len(tree.files)} files under {tree.root}")
    for path in tree.files:
        print(f"  {path.stat().st_size:>9}  {path}")
    return 0


if __name__ == "__main__":  # pragma: no cover - manual demo helper
    raise SystemExit(main())
