"""The "full disk" scenario: a planted drive, its export, and a sandbox around it.

This is the largest of the three test trees (see ``tests/fixtures/gen_live.py``
for the GUI sandbox and ``tests/fixtures/data`` for the committed exports): a
temp directory that looks like a real, full system drive -- OS scratch space,
browser profiles, developer trees, personal media, a game library, downloaded
installers and a pair of byte-identical duplicates -- plus everything the
end-to-end pipeline needs to run against it:

``disk/``
    the planted tree, the machine's "C:" drive,
``full-disk.csv``
    the WizTree export **written from that tree** (sizes, mtimes and the folder
    totals are read back off the disk, so the export can never describe
    something the disk does not have),
``index/``
    the SQLite index the engine pipeline builds from the export,
``gui-index/``
    a second, empty index the GUI smoke pass builds through the app itself,
``data/``
    the app's data root (plan workspaces, settings),
``quarantine/``
    the quarantine store the executor moves payloads into,
``target/``
    the destination "drive" for moves -- on a *different* volume, because a
    move onto the source's own volume frees nothing (design section 7).

The layout is one of every shape the rules and the planner have to handle: T1
scratch, T2 caches and build artifacts, moves onto a data drive, native-tool
advice (T3), an installer that is old enough to go, a disk image and a partial
download that must stay advice, a file no rule matches, a file below every size
floor, and two identical ``blob.bin`` copies for the duplicate groups.

Nothing here is committed to git: :func:`build` plants the tree, and every test
in ``tests/e2e`` starts from it.
"""

from __future__ import annotations

import os
import shutil
import sys
import tempfile
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from fixtures import gen_live
from spacesage import db, planner

MIB = 1024**2

#: Ages of the planted files, in days before the moment they are planted.
OLD = 400
"""Older than a year: stale candidates, stale installers, media."""
AGED = 45
"""Older than the 30-day Downloads cutoff, younger than a year."""
FRESH = 2
"""Touched this week: nothing age-based may act on these on age alone."""

#: ``relative path -> (bytes, age in days)`` of the planted "C:" drive.
DISK: Mapping[str, tuple[int, int]] = {
    # -- Windows itself: OS scratch (T1) and OS-managed stores (T3) --------- #
    "Windows/Temp/wu-setup.tmp": (9 * MIB, FRESH),
    "Windows/Prefetch/CHROME.EXE-1A2B.pf": (3 * MIB, AGED),
    "Windows/Logs/CBS/CBS.log": (5 * MIB, AGED),
    "Windows/WinSxS/Backup/amd64_package.manifest": (14 * MIB, OLD),
    "Windows/Installer/1a2b3c4d.msi": (7 * MIB, OLD),
    # -- the user's temp, caches and browser profiles ---------------------- #
    "Users/Alice/AppData/Local/Temp/setup-helper.tmp": (6 * MIB, FRESH),
    "Users/Alice/AppData/Local/CrashDumps/app-20260901.dmp": (7 * MIB, AGED),
    ("Users/Alice/AppData/Local/Google/Chrome/User Data/Default/Cache/Cache_Data/data_0"): (
        9 * MIB,
        FRESH,
    ),
    ("Users/Alice/AppData/Local/Google/Chrome/User Data/Default/Code Cache/js/index"): (
        6 * MIB,
        FRESH,
    ),
    ("Users/Alice/AppData/Local/Microsoft/Edge/User Data/Default/Cache/Cache_Data/data_1"): (
        7 * MIB,
        FRESH,
    ),
    (
        "Users/Alice/AppData/Local/Mozilla/Firefox/Profiles/k9s2x.default-release/cache2/entries/9A3F"
    ): (8 * MIB, FRESH),
    "Users/Alice/AppData/Local/pip/Cache/http/ab/body": (4 * MIB, FRESH),
    "Users/Alice/AppData/Local/npm-cache/_cacache/content-v2/large.tgz": (5 * MIB, FRESH),
    "Users/Alice/.cache/uv/archive-v0/wheelhouse.whl": (6 * MIB, FRESH),
    # -- downloads: installers, a disk image, a stuck download -------------- #
    "Users/Alice/Downloads/SetupApp-v3.2.1.msi": (11 * MIB, AGED),
    "Users/Alice/Downloads/game-patch-setup.exe": (13 * MIB, AGED),
    "Users/Alice/Downloads/Rescue-Image.iso": (16 * MIB, AGED),
    "Users/Alice/Downloads/holiday.zip.crdownload": (3 * MIB, FRESH),
    # -- personal media: the move candidates -------------------------------- #
    "Users/Alice/Videos/holiday-2024.mp4": (24 * MIB, AGED),
    "Users/Alice/Videos/concert.mkv": (12 * MIB, AGED),
    "Users/Alice/Pictures/photo_0001.jpg": (7 * MIB, AGED),
    "Users/Alice/Pictures/photo_0002.jpg": (6 * MIB, AGED),
    "Users/Alice/Music/live-set.flac": (9 * MIB, AGED),
    # -- documents: stale data, duplicates, files that stay ----------------- #
    "Users/Alice/Documents/dataset_v1.bin": (20 * MIB, OLD),
    "Users/Alice/Documents/backup/blob.bin": (8 * MIB, OLD),
    "Users/Alice/Documents/notes.txt": (16 * 1024, FRESH),
    "Users/Alice/OneDrive/Archive/big-backup.zip": (15 * MIB, OLD),
    # -- developer trees ---------------------------------------------------- #
    "Dev/projects/shop/node_modules/left-pad/lib/big.js": (13 * MIB, OLD),
    "Dev/projects/shop/__pycache__/app.cpython-313.pyc": (2 * MIB, FRESH),
    "Dev/projects/shop/.venv/lib/python3.13/site-packages/libtorch.so": (16 * MIB, OLD),
    "Dev/projects/shop/dist/bundle.js": (7 * MIB, OLD),
    "Dev/projects/shop/data/blob.bin": (8 * MIB, OLD),
    # -- game library ------------------------------------------------------- #
    "Games/SteamLibrary/steamapps/common/BigGame/data.pak": (30 * MIB, OLD),
    "Games/SteamLibrary/steamapps/shadercache/12345/state.bin": (14 * MIB, FRESH),
    "Games/SteamLibrary/steamapps/downloading/patch.tmp": (9 * MIB, FRESH),
    # -- installed programs: the rules say keep ----------------------------- #
    "Program Files/BigApp/app.dll": (17 * MIB, OLD),
    "Program Files/BigApp/readme.txt": (8 * 1024, OLD),
}

#: The two byte-identical copies (same name, same size, same content).
DUPLICATES: tuple[str, str] = (
    "Users/Alice/Documents/backup/blob.bin",
    "Dev/projects/shop/data/blob.bin",
)

#: Files below every size floor: no stage may ever list them.
TOO_SMALL: tuple[str, ...] = (
    "Users/Alice/Documents/notes.txt",
    "Program Files/BigApp/readme.txt",
)

#: The media files a plan moves onto the target drive.
MOVED: tuple[str, ...] = (
    "Users/Alice/Videos/holiday-2024.mp4",
    "Users/Alice/Videos/concert.mkv",
    "Users/Alice/Pictures/photo_0001.jpg",
    "Users/Alice/Pictures/photo_0002.jpg",
    "Users/Alice/Music/live-set.flac",
)

#: Folders the rules quarantine as a whole (their children are swallowed).
QUARANTINED_FOLDERS: tuple[str, ...] = (
    "Windows/Temp",
    "Users/Alice/AppData/Local/Temp",
    "Users/Alice/AppData/Local/CrashDumps",
    "Dev/projects/shop/node_modules",
    "Games/SteamLibrary/steamapps/shadercache",
    "Games/SteamLibrary/steamapps/downloading",
)

#: Default scenario root: fixed, so every render and log shows a stable path.
DEFAULT_ROOT = Path(tempfile.gettempdir()) / "spacesage-e2e"

#: Candidate roots for the target drive; the first one on another volume wins.
TARGET_CANDIDATES: tuple[Path, ...] = (Path("/var/tmp"), Path("/dev/shm"))

#: The export's volume row (the "drive letter" of the planted tree).
VOLUME = "/"


@dataclass(frozen=True)
class FullDisk:
    """Everything one scenario run plants and needs: the disk and its sandbox."""

    root: Path
    disk: Path
    csv_path: Path
    index_dir: Path
    gui_index_dir: Path
    data_root: Path
    quarantine: Path
    rules_dir: Path
    target: Path
    planted_at: float
    """Unix time the files were planted at (their ages are relative to it)."""
    files: int
    dirs: int
    bytes: int

    @property
    def index_db(self) -> Path:
        """The index the engine pipeline builds from the export."""
        return db.resolve_db_path(self.index_dir)

    @property
    def gui_index_db(self) -> Path:
        """The (initially empty) index the GUI smoke pass builds itself."""
        return db.resolve_db_path(self.gui_index_dir)

    def plans_dir(self) -> Path:
        """Where the app keeps one workspace per plan."""
        return self.data_root / "plans"

    def export_of(self, tree: Path) -> Path:
        """Write a WizTree export of ``tree`` next to the scenario's export."""
        return gen_live.write_export(tree, self.csv_path, volume=VOLUME)

    def describe(self) -> str:
        """One line about what was planted (for logs and evidence)."""
        return (
            f"{self.files} files in {self.dirs} folders, {self.bytes / MIB:.1f} MiB at {self.disk}"
        )


class ScenarioUnavailable(RuntimeError):
    """The host cannot host the scenario (no second volume to plant a target on).

    ``tests/e2e`` turns this into a skip: the suite is POSIX-shaped on purpose
    and an environment that cannot serve it is not a failing build.
    """


def unavailable_reason() -> str | None:
    """Why the full-disk scenario cannot be planted here (``None`` when it can)."""
    if os.name != "posix":
        return "the full-disk scenario needs POSIX symlinks and a second temporary volume"
    return None


def target_root(root: Path) -> Path:
    """A fixed directory for the target drive that is *not* on ``root``'s volume.

    The planner refuses a move target on the source's own volume, and a fixed,
    scenario-named directory keeps the rendered paths stable run to run.  Falls
    back to the shared helper's random directory (a second temporary volume)
    when no fixed candidate is usable.
    """
    base = Path(root).expanduser().resolve()
    if planner.volume_of(str(base)) == "/":
        raise ValueError(f"refusing to plant the scenario at {base}: that is a volume root")
    name = f"{base.name}-target"
    for candidate in TARGET_CANDIDATES:
        if not candidate.is_dir():
            continue
        if planner.volume_of(str(candidate)) == planner.volume_of(str(base)):
            continue
        target = candidate / name
        try:
            shutil.rmtree(target, ignore_errors=True)
            target.mkdir(parents=True)
        except OSError:  # pragma: no cover - a read-only candidate
            continue
        return target
    foreign = gen_live.foreign_root()  # pragma: no cover - every CI runner has one
    if foreign is None:  # pragma: no cover - a host without a second volume
        raise ScenarioUnavailable("no writable temporary area on another volume")
    return foreign / "target"


def plant(disk: Path, *, extra: Mapping[str, int] | None = None) -> float:
    """Write the planted drive below ``disk``; returns the reference moment used."""
    import os
    from datetime import UTC, datetime, timedelta

    now = datetime.now(tz=UTC)
    for relative, (size, age) in DISK.items():
        path = disk / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(gen_live.content(path.name, size))
        stamp = (now - timedelta(days=age)).timestamp()
        os.utime(path, (stamp, stamp))
    for relative, size in (extra or {}).items():
        path = disk / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(gen_live.content(path.name, size))
    return now.timestamp()


def tree_facts(root: Path) -> tuple[int, int, int]:
    """``(files, folders, bytes)`` of a planted tree (links counted, not followed)."""
    files = 0
    folders = 0
    total = 0
    for path in sorted(root.rglob("*")):
        if path.is_symlink():
            continue
        if path.is_dir():
            folders += 1
        elif path.is_file():
            files += 1
            total += path.stat().st_size
    return files, folders, total


def snapshot(root: Path) -> dict[str, str]:
    """Exact identity of a tree: ``{relative path: kind:content:size:mtime}``.

    Directories carry no mtime on purpose: creating, moving and removing entries
    inside one rewrites it, and a restored tree is only ever expected to match
    *file* mtimes (which the executor preserves through ``rename``/``copy2``).
    Files carry content, size and nanosecond mtime, links their target -- so an
    equality of two snapshots is an equality of the trees themselves.
    """
    import hashlib

    result: dict[str, str] = {}
    for path in sorted(root.rglob("*")):
        relative = path.relative_to(root).as_posix()
        if path.is_symlink():
            result[relative] = f"LINK:{path.readlink()}"
        elif path.is_dir():
            result[relative] = "DIR"
        else:
            stat = path.stat()
            digest = hashlib.sha256(path.read_bytes()).hexdigest()
            result[relative] = f"FILE:{digest}:{stat.st_size}:{stat.st_mtime_ns}"
    return result


def build(root: Path | str = DEFAULT_ROOT, *, clean: bool = True) -> FullDisk:
    """Plant the full disk at ``root`` and lay out the sandbox around it.

    Idempotent: with ``clean`` (the default) a leftover scenario from an earlier
    or crashed run is removed first, so the paths a caller sees are always the
    paths of a freshly planted disk.
    """
    base = Path(root).expanduser().resolve()
    if clean:
        shutil.rmtree(base, ignore_errors=True)
    base.mkdir(parents=True, exist_ok=True)

    disk = base / "disk"
    disk.mkdir(parents=True, exist_ok=True)
    planted_at = plant(disk)

    files, dirs, total = tree_facts(disk)
    scenario = FullDisk(
        root=base,
        disk=disk,
        csv_path=base / "full-disk.csv",
        index_dir=base / "index",
        gui_index_dir=base / "gui-index",
        data_root=base / "data",
        quarantine=base / "quarantine",
        rules_dir=base / "rules",
        target=target_root(base),
        planted_at=planted_at,
        files=files,
        dirs=dirs,
        bytes=total,
    )
    scenario.export_of(disk)
    scenario.rules_dir.mkdir(parents=True, exist_ok=True)
    scenario.quarantine.mkdir(parents=True, exist_ok=True)
    return scenario


def cleanup(root: Path | str = DEFAULT_ROOT) -> None:
    """Remove a scenario root and its target drive (best effort)."""
    base = Path(root).expanduser()
    shutil.rmtree(base, ignore_errors=True)
    for candidate in TARGET_CANDIDATES:
        shutil.rmtree(candidate / f"{base.name}-target", ignore_errors=True)


def main(argv: Sequence[str] | None = None) -> int:
    """Plant the scenario in ``argv[0]`` (default: the fixed scenario root)."""
    args = list(argv if argv is not None else sys.argv[1:])
    scenario = build(args[0] if args else DEFAULT_ROOT)
    print(f"root       {scenario.root}")
    print(f"disk       {scenario.disk}")
    print(f"csv        {scenario.csv_path}")
    print(f"index      {scenario.index_db}")
    print(f"data       {scenario.data_root}")
    print(f"quarantine {scenario.quarantine}")
    print(f"target     {scenario.target}")
    print(f"planted    {scenario.describe()}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
