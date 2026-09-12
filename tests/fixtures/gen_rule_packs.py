"""Regenerate ``tests/fixtures/data/rule_packs.csv`` (the classifier fixture).

The fixture is a hand-written file list covering every rule family the built-in
packs are expected to recognise (``tests/test_rules.py::EXPECTED``).  Running
this module rewrites the CSV deterministically: run it from the repository root
with ``uv run python tests/fixtures/gen_rule_packs.py``.

Emission follows WizTree's shape: depth-first (a folder row, then its
subfolders, then its files), folder rows carrying descendant totals, ``Size``
and ``Allocated`` identical, and `Modified` as ``yyyy/MM/dd HH:mm:ss``.
"""

from __future__ import annotations

from pathlib import Path
from typing import NamedTuple

OLD = "2020/01/15 10:00:00"
MID = "2024/03/02 08:30:00"
RECENT = "2026/09/10 09:00:00"

DATA_DIR = Path(__file__).resolve().parent / "data"

#: ``(path, size, modified)`` for every file in the fixture.
FILES: tuple[tuple[str, int, str], ...] = (
    ("C:\\Windows\\Temp\\wtmp.tmp", 1_000_000, RECENT),
    ("C:\\Windows\\SoftwareDistribution\\Download\\update.cab", 10_000_000, RECENT),
    ("C:\\Windows\\WinSxS\\Manifests\\x.manifest", 2_000_000, RECENT),
    ("C:\\Windows\\Minidump\\mini.dmp", 300_000, RECENT),
    ("C:\\Windows\\System32\\kernel.dll", 2_000_000, MID),
    ("C:\\pagefile.sys", 4_000_000, RECENT),
    ("C:\\hiberfil.sys", 5_000_000, RECENT),
    ("C:\\swapfile.sys", 1_000_000, RECENT),
    ("C:\\$Recycle.Bin\\S-1-5-21\\$R123.exe", 800_000, RECENT),
    ("C:\\Program Files\\Widget\\widget.exe", 1_000_000, MID),
    ("C:\\Users\\Alice\\AppData\\Local\\Temp\\utmp.tmp", 2_000_000, RECENT),
    ("C:\\Users\\Alice\\AppData\\Local\\pip\\Cache\\wheels\\x.whl", 3_000_000, RECENT),
    ("C:\\Users\\Alice\\AppData\\Local\\npm-cache\\_cacache\\a.dat", 1_000_000, RECENT),
    ("C:\\Users\\Alice\\AppData\\Local\\uv\\cache\\b.whl", 900_000, RECENT),
    ("C:\\Users\\Alice\\AppData\\Local\\CrashDumps\\app.exe.1234.dmp", 300_000, RECENT),
    ("C:\\Users\\Alice\\AppData\\Local\\Docker\\wsl\\data\\ext4.vhdx", 200_000_000, RECENT),
    (
        "C:\\Users\\Alice\\AppData\\Local\\Google\\Chrome\\User Data\\Default\\Cache\\f_000001",
        5_000_000,
        RECENT,
    ),
    (
        "C:\\Users\\Alice\\AppData\\Local\\Microsoft\\Edge\\User Data\\Default\\Code Cache\\js.bin",
        4_000_000,
        RECENT,
    ),
    (
        "C:\\Users\\Alice\\AppData\\Local\\Mozilla\\Firefox\\Profiles\\abc.default"
        "\\cache2\\entries\\e_000001",
        1_000_000,
        RECENT,
    ),
    ("C:\\Users\\Alice\\AppData\\Local\\D3DSCache\\shader.bin", 2_000_000, RECENT),
    ("C:\\Users\\Alice\\.cache\\huggingface\\models\\blob.bin", 50_000_000, RECENT),
    ("C:\\Users\\Alice\\.cache\\torch\\hub\\checkpoint.pt", 20_000_000, RECENT),
    ("C:\\Users\\Alice\\.nuget\\packages\\newtonsoft\\lib.dll", 1_500_000, RECENT),
    ("C:\\Users\\Alice\\.cargo\\registry\\cache\\serde.crate", 700_000, RECENT),
    ("C:\\Users\\Alice\\.gradle\\caches\\modules\\kotlin.jar", 2_500_000, RECENT),
    ("C:\\Users\\Alice\\.m2\\repository\\junit\\junit.jar", 1_100_000, RECENT),
    ("C:\\Users\\Alice\\Documents\\proj\\node_modules\\react\\index.js", 4_000_000, RECENT),
    ("C:\\Users\\Alice\\Documents\\proj\\.venv\\Lib\\site.py", 500_000, RECENT),
    ("C:\\Users\\Alice\\Documents\\proj\\__pycache__\\mod.pyc", 200_000, RECENT),
    ("C:\\Users\\Alice\\Documents\\proj\\dist\\bundle.js", 6_000_000, RECENT),
    ("C:\\Users\\Alice\\Documents\\logs\\app.log", 5_000, MID),
    ("C:\\Users\\Alice\\Documents\\logs\\app.log.1", 5_000, MID),
    ("C:\\Users\\Alice\\Documents\\thesis.docx", 200_000, MID),
    ("C:\\Users\\Alice\\Documents\\old.docx.bak", 10_000, OLD),
    ("C:\\Users\\Alice\\Documents\\scratch.tmp", 400_000, RECENT),
    ("C:\\Users\\Alice\\Documents\\lookup.xlsx", 30_000, RECENT),
    ("C:\\Users\\Alice\\Downloads\\old_setup.exe", 7_000_000, OLD),
    ("C:\\Users\\Alice\\Downloads\\legacy.msi", 3_000_000, OLD),
    ("C:\\Users\\Alice\\Downloads\\notes.txt", 1_000, RECENT),
    ("C:\\Users\\Alice\\Pictures\\raw\\shot.cr2", 600_000_000, MID),
    ("C:\\Users\\Alice\\Videos\\holiday.mp4", 6_000_000, MID),
    ("C:\\Users\\Alice\\OneDrive\\Documents\\shared.docx", 900_000, MID),
    ("D:\\Games\\SteamLibrary\\steamapps\\common\\HalfLife\\hl2.exe", 7_000_000, MID),
    ("D:\\Games\\SteamLibrary\\steamapps\\shadercache\\half.dxcache", 1_000_000, RECENT),
    ("D:\\Games\\Epic Games\\Fortnite\\fort.exe", 30_000_000, MID),
    ("D:\\Games\\GOG Games\\Witcher\\witcher.exe", 20_000_000, MID),
    ("D:\\Games\\Battle.net\\CallOfDuty\\cod.exe", 40_000_000, MID),
)


class _Node(NamedTuple):
    """One folder: its own timestamp plus its files and subfolders."""

    path: str
    mtime: str
    files: list[tuple[str, int, str]]
    children: dict[str, _Node]


def _blank(path: str, mtime: str = RECENT) -> _Node:
    return _Node(path=path, mtime=mtime, files=[], children={})


def _build() -> dict[str, _Node]:
    """Build the folder tree from :data:`FILES`; every node is a folder."""
    roots: dict[str, _Node] = {}
    for path, size, modified in FILES:
        parts = path.split("\\")
        root = parts[0] + "\\"
        node = roots.setdefault(root, _blank(root))
        for depth in range(1, len(parts) - 1):
            child_path = "\\".join(parts[: depth + 1])
            node = node.children.setdefault(child_path, _blank(child_path))
        node.files.append((parts[-1], size, modified))
    return roots


def _totals(node: _Node) -> tuple[int, int, int]:
    """``(bytes, files, folders)`` for a folder, from its files downwards."""
    size = sum(item[1] for item in node.files)
    count = len(node.files)
    folders = 0
    for child in node.children.values():
        child_size, child_count, child_folders = _totals(child)
        size += child_size
        count += child_count
        folders += child_folders + 1
    return size, count, folders


def _emit(node: _Node, rows: list[tuple[str, str, str, str, str, str, str]]) -> None:
    """Append a folder's row, then its subfolders, then its files."""
    size, count, folders = _totals(node)
    marker = "" if node.path.endswith("\\") else "\\"
    rows.append(
        (node.path + marker, str(size), str(size), node.mtime, "00000010", str(count), str(folders))
    )
    for child in sorted(node.children.values()):
        _emit(child, rows)
    base = node.path.rstrip("\\")
    for name, size, modified in sorted(node.files):
        rows.append((f"{base}\\{name}", str(size), str(size), modified, "00000020", "0", "0"))


def main() -> None:
    """Write the fixture CSV."""
    import csv

    rows: list[tuple[str, str, str, str, str, str, str]] = []
    for root in sorted(_build().values()):
        _emit(root, rows)

    out = DATA_DIR / "rule_packs.csv"
    with out.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.writer(handle, quoting=csv.QUOTE_ALL, lineterminator="\r\n")
        writer.writerow(
            ("File Name", "Size", "Allocated", "Modified", "Attributes", "Files", "Folders")
        )
        writer.writerows(rows)
    print(f"wrote {out} ({len(rows)} rows)")


if __name__ == "__main__":
    main()
