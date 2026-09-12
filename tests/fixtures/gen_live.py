"""A live tree plus the WizTree export that describes it (GUI end-to-end).

The GUI's full loop -- select in the ranked list, plan, dry-run, execute, undo --
has to act on files that really exist, so this fixture plants a small tree with
the extensions and sizes the rule packs react to, and writes the WizTree-shaped
CSV *of that very tree*: absolute paths, the volume row first, every ancestor
folder before its children (no orphan rows), folder rows carrying their
descendant totals, ``Modified`` read back from the planted mtimes.

The tree is deliberately one of each kind the plan screen has to show:

``app/node_modules/``
    a folder rule (T2 ``DELETE_QUARANTINE``) that swallows its children,
``scratch/old.dmp``
    a T1 file quarantine,
``logs/session.log``
    a T1 file quarantine inside a folder that is itself a T2 review item,
``media/holiday.mp4``
    a T2 move candidate (needs a target on another volume),
``archive/setup.msi``
    an installer older than a year outside Downloads: the classifier's advice
    (a T2 ``REVIEW``), so the plan screen has an advice row that never executes,
``keep/notes.txt``
    an entry no rule matches -- the explicit *No action* row.
"""

from __future__ import annotations

import csv
import os
import shutil
import sys
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

MIB = 1024**2

#: Reference moment of the export: 2026-09-12 12:00:00 UTC (ages stay stable).
NOW = datetime(2026, 9, 12, 12, 0, 0, tzinfo=UTC)

#: Age of every planted file, in days before :data:`NOW`.
AGE_DAYS = 400

#: ``relative path -> bytes`` of the standard sandbox tree.
TREE: Mapping[str, int] = {
    "app/node_modules/pkg/index.js": 3 * MIB,
    "app/node_modules/pkg/lib/util.js": 2 * MIB,
    "scratch/old.dmp": 6 * MIB,
    "logs/session.log": 3 * MIB,
    "media/holiday.mp4": 5 * MIB,
    "archive/setup.msi": 2 * MIB,
    "keep/notes.txt": 2 * MIB,
}

COLUMNS = ("File Name", "Size", "Allocated", "Modified", "Attributes", "Files", "Folders")

STAMP = "%Y/%m/%d %H:%M:%S"


@dataclass(frozen=True)
class Live:
    """A planted tree, the CSV that describes it and the target drive's root."""

    tree: Path
    """The folder being analysed."""

    csv_path: Path
    target: Path
    """A folder on another volume that moves are planned onto."""

    @property
    def db(self) -> Path:
        """Where an index of this export is built."""
        return self.csv_path.parent / "spacesage.db"


def content(seed: str, size: int) -> bytes:
    """Deterministic bytes: ``seed`` repeated up to ``size``."""
    pattern = f"--{seed}--".encode()
    return (pattern * (size // len(pattern) + 1))[:size]


def plant(root: Path, *, extra: Mapping[str, int] | None = None) -> Path:
    """Create the standard tree below ``root`` with fixed sizes and ages."""
    stamp = NOW - timedelta(days=AGE_DAYS)
    for relative, size in {**TREE, **(extra or {})}.items():
        path = root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(content(path.name, size))
        os.utime(path, (stamp.timestamp(), stamp.timestamp()))
    return root


def snapshot(root: Path) -> dict[str, str]:
    """``{relative path: sha256 | "DIR" | "LINK:<target>"}`` of a whole tree."""
    import hashlib

    result: dict[str, str] = {}
    for path in sorted(root.rglob("*")):
        relative = path.relative_to(root).as_posix()
        if path.is_symlink():
            result[relative] = f"LINK:{path.readlink()}"
        elif path.is_dir():
            result[relative] = "DIR"
        else:
            result[relative] = hashlib.sha256(path.read_bytes()).hexdigest()
    return result


def _mtime_text(path: Path) -> str:
    return datetime.fromtimestamp(path.stat().st_mtime, tz=UTC).strftime(STAMP)


def _totals(path: Path) -> tuple[int, int, int]:
    """``(bytes, files, folders)`` below ``path`` (links counted as entries)."""
    total = 0
    files = 0
    folders = 0
    for entry in sorted(path.iterdir()):
        if entry.is_dir() and not entry.is_symlink():
            folders += 1
            sub_bytes, sub_files, sub_folders = _totals(entry)
            total += sub_bytes
            files += sub_files
            folders += sub_folders
        elif entry.is_file():
            files += 1
            total += entry.stat().st_size
    return total, files, folders


def _row(path: Path, *, is_dir: bool) -> list[str]:
    """One CSV row for a folder (descendant totals) or a file."""
    text = path.as_posix() + ("/" if is_dir else "")
    modified = _mtime_text(path)
    if is_dir:
        total, files, folders = _totals(path)
        return [text, str(total), str(total), modified, "00000010", str(files), str(folders)]
    size = path.stat().st_size
    return [text, str(size), str(size), modified, "00000020", "0", "0"]


def rows_for(root: Path, *, volume: str = "/") -> list[list[str]]:
    """The WizTree rows describing ``root``, ancestors included (no orphans)."""
    if not root.is_dir():
        raise FileNotFoundError(f"no tree at {root}")
    rows: list[list[str]] = [[volume, "0", "0", _mtime_text(root), "00000010", "0", "0"]]
    # Every folder between the volume root and the tree, so no row is an orphan.
    parts = [part for part in root.resolve().as_posix().split("/") if part]
    for index in range(1, len(parts) + 1):
        ancestor = Path("/").joinpath(*parts[:index])
        if root.resolve() == ancestor and index == len(parts):
            break
        rows.append(_row(ancestor, is_dir=True))

    def walk(directory: Path) -> None:
        children = sorted(directory.iterdir())
        for child in children:
            if child.is_dir() and not child.is_symlink():
                rows.append(_row(child, is_dir=True))
                walk(child)
        for child in children:
            if child.is_file() and not child.is_symlink():
                rows.append(_row(child, is_dir=False))

    rows.append(_row(root, is_dir=True))
    walk(root)
    return rows


def write_export(root: Path, csv_path: Path, *, volume: str = "/") -> Path:
    """Write the WizTree-shaped CSV of ``root`` (UTF-8 with BOM, CRLF, quoted)."""
    csv_path.parent.mkdir(parents=True, exist_ok=True)
    with csv_path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.writer(handle, quoting=csv.QUOTE_ALL, lineterminator="\r\n")
        writer.writerow(COLUMNS)
        writer.writerows(rows_for(root, volume=volume))
    return csv_path


def scenario(base: Path, *, tree_name: str = "tree") -> Live:
    """Plant the tree under ``base`` and export it next to the CSV.

    ``base/target`` is the *target drive* if the caller points the plan at it;
    it is created here so a move has somewhere to land.
    """
    tree = plant(base / tree_name)
    csv_path = write_export(tree, base / "sandbox.csv")
    target = base / "target"
    target.mkdir(parents=True, exist_ok=True)
    return Live(tree=tree, csv_path=csv_path, target=target)


def cleanup(*paths: Path) -> None:
    """Remove directories a test had to create outside its ``tmp_path``."""
    for path in paths:
        shutil.rmtree(path, ignore_errors=True)


def foreign_root(prefix: str = "spacesage-live-") -> Path | None:
    """A writable temporary folder that is not on ``/tmp``'s volume, or ``None``.

    The planner refuses a move target on the source's own volume (a move there
    frees nothing -- :func:`spacesage.planner.same_volume`), so a test that plans
    and executes a move needs a second one.  ``/var/tmp`` sits on a different
    first component than ``/tmp`` and exists on every Linux runner; ``/dev/shm``
    is the fallback.
    """
    import tempfile

    for candidate in ("/var/tmp", "/dev/shm"):
        root = Path(candidate)
        if not root.is_dir():
            continue
        try:
            return Path(tempfile.mkdtemp(prefix=prefix, dir=str(root)))
        except OSError:  # pragma: no cover - a read-only tmp area
            continue
    return None


def main(argv: Sequence[str] | None = None) -> int:
    """Plant the demo sandbox in ``argv[0]`` (default: ./sandbox)."""
    args = list(argv if argv is not None else sys.argv[1:])
    base = Path(args[0] if args else "sandbox").resolve()
    live = scenario(base)
    print(f"tree   {live.tree}")
    print(f"csv    {live.csv_path}")
    print(f"target {live.target}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
