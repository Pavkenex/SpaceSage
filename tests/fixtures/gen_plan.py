"""Regenerate ``tests/fixtures/data/plan.csv`` and ``plan.golden.json``.

The plan stage needs a scenario where every *decision* of ``spacesage.planner``
is visible: which candidates become executable actions, where a move goes, which
link keeps the old path alive, what the target budget does to a move that does
not fit, and which candidates stay review items.  Every entry below exists for a
specific assertion in ``tests/test_planner.py`` -- the docstring of that module
walks through them.

The committed golden is the plan for this export with the source identity pinned
(``SOURCE_CSV``/``MACHINE``/``EXPORTED``) and the volatile fields masked
(``created``, the index path in ``provenance``), so it is stable on every
machine: ``plan_id`` included.  Running this module rewrites both files
deterministically: run it from the repository root with
``uv run python tests/fixtures/gen_plan.py``.
"""

from __future__ import annotations

import json
import sys
import tempfile
from datetime import UTC, datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from fixtures import gen_candidates
from spacesage import db, planner, rules
from spacesage.ingest import ingest_csv

DATA_DIR = Path(__file__).resolve().parent / "data"
CSV_NAME = "plan.csv"
GOLDEN_NAME = "plan.golden.json"

NOW = datetime(2026, 9, 12, 12, 0, 0, tzinfo=UTC)
MIB = 1024**2
GIB = 1024**3

RECENT = gen_candidates.stamp(5)
MID = gen_candidates.stamp(200)
OLD = gen_candidates.stamp(800)
ANCIENT = gen_candidates.stamp(1500)

#: The identity the golden is pinned to (a plan id must not depend on the box).
SOURCE_CSV = "tests/fixtures/data/plan.csv"
MACHINE = "FIXTURE-PC"
EXPORTED = "2026-09-12T12:00:00+00:00"

TARGETS = (planner.PlanTarget(name="D:", free_bytes=200 * GIB, reserve_bytes=20 * GIB),)
"""One target with room for every move: budget problems get their own tests."""

MIN_SIZE = 100 * MIB
"""The planner's default threshold; the entries below sit around it on purpose."""

TOP = 50
"""Per kind, so the golden pins the whole ranked list, not a truncated one."""

#: Folder timestamps that differ from "newest descendant"; the drive roots are
#: ancient on purpose, a root must never become a candidate.
FOLDER_MTIME: dict[str, str] = {"C:\\": ANCIENT, "D:\\": ANCIENT}

File = gen_candidates.File

#: The scenario.  Read the comments as the index of test_planner.py.
FILES: tuple[File, ...] = (
    # --- T1 quarantines ---------------------------------------------------- #
    # The plain cases: a system temp folder, a user temp folder (fresh, so the
    # phase order -- not the recency score -- decides its place).
    File(r"C:\Windows\Temp\wtmp.tmp", 300 * MIB, MID),
    File(r"C:\Users\Alice\AppData\Local\Temp\utmp.tmp", 400 * MIB, RECENT),
    # A browser cache folder: the folder row matches the rule and swallows the
    # file inside it; the sibling History file is unknown data and only shows up
    # in the Google app footprint.
    File(
        r"C:\Users\Alice\AppData\Local\Google\Chrome\User Data\Default\Cache\f_000001",
        450 * MIB,
        MID,
    ),
    File(r"C:\Users\Alice\AppData\Local\Google\Chrome\User Data\Default\History", 150 * MIB, MID),
    # A quarantine *inside* a folder that is being moved: its 200 MiB come out
    # of the move's bytes (the plan must not count them twice).
    File(
        r"C:\Users\Alice\Videos\cache\Google\Chrome\User Data\Default\Cache\f_000002",
        200 * MIB,
        MID,
    ),
    # --- T2 quarantines ---------------------------------------------------- #
    # A download leaving a stale folder behind it: the folder itself is a stale
    # candidate, fully covered by this quarantine, so the plan drops it.
    File(r"C:\Users\Alice\Downloads\legacy.msi", 250 * MIB, OLD),
    # Same shape, one level down: the .bak is quarantined, the folder it sits in
    # is dropped as fully covered.
    File(r"C:\Users\Alice\OldStuff\backup-copy.bak", 300 * MIB, OLD),
    # --- moves: a folder the rules matched --------------------------------- #
    # media-libraries matches the folder and links it back, so this is one MOVE
    # action for the folder, not one per file.
    File(r"C:\Users\Alice\Videos\holiday.mp4", 800 * MIB, MID),
    File(r"C:\Users\Alice\Videos\clips\c1.mp4", 300 * MIB, OLD),
    File(r"C:\Users\Alice\Videos\clips\c2.mp4", 200 * MIB, OLD),
    File(r"C:\Users\Alice\Music\album.flac", 600 * MIB, MID),
    # --- moves: a file group beyond the member cap -------------------------- #
    # No folder rule matches C:\Media\Series: the twelve videos group at folder
    # level, and the ranked list caps ``members`` at ten -- the plan still has to
    # move all twelve.
    *(File(rf"C:\Media\Series\s{index:02d}.mp4", 60 * MIB, OLD) for index in range(1, 13)),
    # --- launcher-managed library (NATIVE, never moved by SpaceSage) -------- #
    File(r"C:\Games\SteamLibrary\steamapps\common\HalfLife\hl2.exe", 700 * MIB, MID),
    # --- stale reviews ----------------------------------------------------- #
    File(r"C:\Users\Alice\Archive\photos.zip", 300 * MIB, ANCIENT),
    File(r"C:\Users\Alice\Documents\resume.part", 200 * MIB, OLD),
    # --- weak duplicates --------------------------------------------------- #
    File(r"C:\Users\Alice\Documents\dataset.bin", 600 * MIB, MID),
    File(r"C:\Users\Alice\Vault\dataset.bin", 600 * MIB, MID),
    # --- applications ------------------------------------------------------ #
    # OneDrive is T3 NATIVE: the app row must stay a review item that carries the
    # vendor command, never a NATIVE action.
    File(r"C:\Users\Alice\AppData\Local\OneDrive\data\one.bin", 500 * MIB, MID),
    # Slack matches no rule at all: an explicit "no advice" review.
    File(r"C:\Users\Alice\AppData\Roaming\Slack\Cache\slack.dat", 200 * MIB, MID),
    # Program Files is the KEEP catch-all: the explicit "No action" review.
    File(r"C:\Program Files\Widget\widget.exe", 1000 * MIB, MID),
    # --- never in a plan --------------------------------------------------- #
    # T3 report-only data (the component store): it must not appear at all.
    File(r"C:\Windows\WinSxS\Manifests\component.bin", 350 * MIB, OLD),
)


def build_rows() -> list[tuple[str, str, str, str, str, str, str]]:
    """The CSV rows of the scenario (folder rows derived, depth-first)."""
    return gen_candidates.build_rows(FILES, FOLDER_MTIME)


def pin_source(db_path: Path, *, csv: str = SOURCE_CSV) -> None:
    """Pin the index identity so the golden (and its plan id) is machine-independent."""
    conn = db.open_db(db_path)
    try:
        db.meta_set_many(
            conn, {"source.csv": csv, "source.machine": MACHINE, "source.exported": EXPORTED}
        )
    finally:
        conn.close()


def build_plan(db_path: Path) -> planner.Plan:
    """Plan the scenario index with the fixture's fixed reference point."""
    conn = db.open_db(db_path)
    try:
        return planner.build_plan(
            conn,
            rules.load_rules(),
            targets=TARGETS,
            min_size=MIN_SIZE,
            top=TOP,
            now=int(NOW.timestamp()),
            db_path=str(db_path),
        )
    finally:
        conn.close()


def normalise(document: dict[str, object]) -> dict[str, object]:
    """Mask the volatile fields so a committed golden stays comparable."""
    out = dict(document)
    out["created"] = "<created>"
    provenance = dict(out["provenance"])  # type: ignore[arg-type]
    provenance["db"] = "<db>"
    out["provenance"] = provenance
    return out


def golden_document(csv_path: Path) -> dict[str, object]:
    """Ingest ``csv_path`` into a throwaway index and return the normalized plan."""
    with tempfile.TemporaryDirectory() as work:
        db_path = Path(work) / "plan.db"
        ingest_csv(csv_path, db_path)
        pin_source(db_path)
        return normalise(build_plan(db_path).to_dict())


def main() -> None:
    """Write the fixture CSV and the golden plan."""
    rows = build_rows()
    csv_path = DATA_DIR / CSV_NAME
    gen_candidates.write_export(csv_path, rows)
    golden = golden_document(csv_path)
    golden_path = DATA_DIR / GOLDEN_NAME
    golden_path.write_text(json.dumps(golden, indent=2) + "\n", encoding="utf-8")
    actions = golden["actions"]
    print(f"wrote {csv_path} ({len(rows)} rows)")
    print(f"wrote {golden_path} ({len(actions)} actions)")  # type: ignore[arg-type]


if __name__ == "__main__":
    main()
