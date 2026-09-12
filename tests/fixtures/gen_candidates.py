"""Regenerate ``tests/fixtures/data/candidates.csv`` (the candidate fixture).

A hand-written scenario that exercises every candidate kind of
:mod:`spacesage.candidates` with expectations small enough to reason about by
hand (``tests/test_candidates.py``).  Every entry is chosen for a *specific*
assertion: a folder that must swallow its nested files, a T2 delete next to a
T1 delete, a media folder that groups, a media folder that does not, a weak
duplicate cluster with all its controls, apps whose advice comes from their
biggest matched entry, big cold entries at each tier, plus the entries that must
never show up at all (too small, too fresh, unknown age, T3, hard-linked copies,
the drive roots).

Timestamps are absolute and the tests pin ``NOW = 2026-09-12 12:00:00 UTC``, so
the fixture is stable forever.  Running this module rewrites the CSV
deterministically: run it from the repository root with
``uv run python tests/fixtures/gen_candidates.py``.

Emission follows WizTree's shape: depth-first (a folder row, then its
subfolders, then its files), folder rows carrying descendant totals and a
``Modified`` stamp (the newest descendant's unless pinned), ``Size`` and
``Allocated`` identical, and a leading zero on ``Allocated`` marking a
hard-linked copy.
"""

from __future__ import annotations

import csv
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import NamedTuple

DATA_DIR = Path(__file__).resolve().parent / "data"
NOW = datetime(2026, 9, 12, 12, 0, 0, tzinfo=UTC)
MIB = 1024**2


def stamp(days: float) -> str:
    """WizTree ``Modified`` text for ``days`` before :data:`NOW`."""
    return (NOW - timedelta(days=days)).strftime("%Y/%m/%d %H:%M:%S")


#: The five ages the scenario uses, in days before NOW.
RECENT = stamp(5)
MID = stamp(200)
OLD = stamp(800)
ANCIENT = stamp(1500)
UNKNOWN = ""


class File(NamedTuple):
    """One file row: path, logical bytes, ``Modified`` text and hardlink marker."""

    path: str
    size: int
    modified: str = MID
    hardlink: bool = False


#: Folder timestamps that differ from "the newest descendant"; the drive roots
#: are ancient on purpose -- a root must never become a candidate.
FOLDER_MTIME: dict[str, str] = {
    "C:\\": ANCIENT,
    "D:\\": ANCIENT,
    "C:\\Users\\Alice\\Archive": ANCIENT,
}

#: ``(path, size, modified)`` for every file; see the module docstring.
FILES: tuple[File, ...] = (
    # --- T1 deletes: a folder that must swallow its nested files ----------- #
    File("C:\\Windows\\Temp\\wtmp.tmp", 300 * MIB, MID),
    # --- T1 deletes: fresh and bigger, yet ranked below the cold folder ---- #
    File("C:\\Users\\Alice\\AppData\\Local\\Temp\\utmp.tmp", 400 * MIB, RECENT),
    File("C:\\Users\\Alice\\Documents\\big.dmp", 400 * MIB, RECENT),
    # --- T2 deletes, at and just below the 100 MiB threshold --------------- #
    File("C:\\Users\\Alice\\Downloads\\legacy.msi", 250 * MIB, OLD),
    File("C:\\Users\\Alice\\Downloads\\at-threshold.exe", 100 * MIB, OLD),
    File("C:\\Users\\Alice\\Downloads\\below-threshold.exe", 100 * MIB - 1, OLD),
    File("C:\\Users\\Alice\\Documents\\old.docx.bak", 150 * MIB, MID),
    # --- media: a folder rule covers the folder and its nested files ------- #
    File("C:\\Users\\Alice\\Videos\\holiday.mp4", 800 * MIB, MID),
    File("C:\\Users\\Alice\\Videos\\clips\\c1.mp4", 300 * MIB, OLD),
    File("C:\\Users\\Alice\\Videos\\clips\\c2.mp4", 200 * MIB, OLD),
    # --- media: no folder rule, so the files group at directory level ------ #
    File("C:\\Media\\Movies\\m1.mp4", 250 * MIB, OLD),
    File("C:\\Media\\Movies\\m2.mp4", 150 * MIB, OLD),
    File("C:\\Media\\Movies\\m4.mp4", 120 * MIB, RECENT),
    # --- media: a group below the threshold must not be listed ------------- #
    File("C:\\Media\\Clips\\c1.mp4", 40 * MIB, OLD),
    File("C:\\Media\\Clips\\c2.mp4", 30 * MIB, OLD),
    # --- launcher-managed games: NATIVE advice, folder level --------------- #
    File("D:\\Games\\SteamLibrary\\steamapps\\common\\HalfLife\\hl2.exe", 700 * MIB, MID),
    File("D:\\Games\\Epic Games\\Fortnite\\fort.exe", 600 * MIB, MID),
    # --- stale: big, cold, and nothing else claims them -------------------- #
    # A leftover partial download (T2 REVIEW) whose folder is far too busy to
    # be stale itself: the file is a stale candidate with the rule's verdict.
    File("C:\\Users\\Alice\\Documents\\resume.part", 200 * MIB, OLD),
    # An unknown folder older than everything else in the export.
    File("C:\\Users\\Alice\\Archive\\photos.zip", 300 * MIB, ANCIENT),
    # --- never candidates: too fresh / unknown age / T3 ------------------- #
    File("C:\\Users\\Alice\\Documents\\fresh.blob", 700 * MIB, RECENT),
    File("C:\\Users\\Alice\\Documents\\unknown-age.dat", 500 * MIB, UNKNOWN),
    File("C:\\Users\\Alice\\Documents\\a.dat", 600 * MIB, MID),
    File("C:\\Windows\\WinSxS\\Manifests\\component.bin", 350 * MIB, OLD),
    # --- weak duplicates --------------------------------------------------- #
    File("C:\\Users\\Alice\\Documents\\dataset.bin", 600 * MIB, MID),
    File("C:\\Users\\Alice\\Vault\\dataset.bin", 600 * MIB, MID),
    File("C:\\Users\\Alice\\Documents\\other\\dataset.bin", 300 * MIB, MID),
    File("C:\\Users\\Alice\\Vault\\small-pair.bin", 10 * MIB, MID),
    File("C:\\Users\\Alice\\Documents\\small-pair.bin", 10 * MIB, MID),
    File("C:\\Users\\Alice\\Vault\\tiny.bin", 700_000, MID),
    File("C:\\Users\\Alice\\Documents\\tiny.bin", 700_000, MID),
    File("C:\\Users\\Alice\\Documents\\big2.bin", 700 * MIB, MID),
    File("C:\\Users\\Alice\\Vault\\big2.bin", 700 * MIB, MID, hardlink=True),
    # --- applications ------------------------------------------------------ #
    File(
        "C:\\Users\\Alice\\AppData\\Local\\Google\\Chrome\\User Data\\Default\\Cache\\f_000001",
        450 * MIB,
        MID,
    ),
    File("C:\\Users\\Alice\\AppData\\Roaming\\Slack\\Cache\\slack.dat", 200 * MIB, MID),
    File("C:\\Program Files\\Widget\\widget.exe", 1000 * MIB, MID),
)


class _Node(NamedTuple):
    """One folder: its pinned timestamp plus its files and subfolders."""

    path: str
    mtime: str | None
    files: list[File]
    children: dict[str, _Node]


def _blank(path: str, mtime: str | None = None) -> _Node:
    return _Node(path=path, mtime=mtime, files=[], children={})


def build_rows(
    files: tuple[File, ...] = FILES,
    folder_mtime: dict[str, str] | None = None,
) -> list[tuple[str, str, str, str, str, str, str]]:
    """Build the CSV rows for ``files`` (folder rows derived, depth-first)."""
    pinned = FOLDER_MTIME if folder_mtime is None else folder_mtime
    roots: dict[str, _Node] = {}
    for entry in files:
        parts = entry.path.split("\\")
        root_path = parts[0] + "\\"
        node = roots.setdefault(root_path, _blank(root_path, pinned.get(root_path)))
        for depth in range(1, len(parts) - 1):
            child_path = "\\".join(parts[: depth + 1])
            node = node.children.setdefault(child_path, _blank(child_path, pinned.get(child_path)))
        node.files.append(entry)
    rows: list[tuple[str, str, str, str, str, str, str]] = []
    for root in sorted(roots.values()):
        _emit(root, rows)
    return rows


def write_export(path: Path, rows: list[tuple[str, str, str, str, str, str, str]]) -> None:
    """Write ``rows`` as a WizTree-style CSV at ``path``."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.writer(handle, quoting=csv.QUOTE_ALL, lineterminator="\r\n")
        writer.writerow(
            ("File Name", "Size", "Allocated", "Modified", "Attributes", "Files", "Folders")
        )
        writer.writerows(rows)


def _totals(node: _Node) -> tuple[int, int, int, str | None]:
    """``(bytes, files, folders, newest file timestamp)`` for a folder."""
    size = sum(entry.size for entry in node.files)
    count = len(node.files)
    folders = 0
    newest: str | None = None
    for entry in node.files:
        if entry.modified and (newest is None or entry.modified > newest):
            newest = entry.modified
    for child in node.children.values():
        child_size, child_count, child_folders, child_newest = _totals(child)
        size += child_size
        count += child_count
        folders += child_folders + 1
        if child_newest and (newest is None or child_newest > newest):
            newest = child_newest
    return size, count, folders, newest


def _emit(node: _Node, rows: list[tuple[str, str, str, str, str, str, str]]) -> None:
    """Append a folder's row, then its subfolders, then its files."""
    size, count, folders, newest = _totals(node)
    modified = node.mtime if node.mtime is not None else (newest or "")
    marker = "" if node.path.endswith("\\") else "\\"
    rows.append(
        (node.path + marker, str(size), str(size), modified, "00000010", str(count), str(folders))
    )
    for child in sorted(node.children.values()):
        _emit(child, rows)
    base = node.path.rstrip("\\")
    for entry in sorted(node.files):
        allocated = str(entry.size)
        if entry.hardlink and entry.size:
            allocated = "0" + allocated
        rows.append(
            (
                f"{base}\\{entry.path.split(chr(92))[-1]}",
                str(entry.size),
                allocated,
                entry.modified,
                "00000020",
                "0",
                "0",
            )
        )


def main() -> None:
    """Write the fixture CSV."""
    rows = build_rows()
    out = DATA_DIR / "candidates.csv"
    write_export(out, rows)
    print(f"wrote {out} ({len(rows)} rows)")


if __name__ == "__main__":
    main()
