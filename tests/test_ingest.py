"""Ingest tests: parsing edge cases, generator ground truth, CLI, perf smoke."""

from __future__ import annotations

import os
import re
import sqlite3
import subprocess
import sys
import time
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

import pytest

from fixtures import gen
from spacesage import db
from spacesage.ingest import (
    IngestError,
    detect_encoding,
    extract_ext,
    extract_name,
    ingest_csv,
    normalise_path,
    parent_path,
    path_depth,
)

DATA_DIR = Path(__file__).resolve().parent / "fixtures" / "data"

#: Perf regression floor. The design target is >= 1M rows/min (~16.7k rows/s);
#: the dev container measures ~24k rows/s, so the floor is set far below that
#: to survive slow CI runners while still catching order-of-magnitude
#: regressions (a per-row SQL lookup drops throughput below 1k rows/s).
PERF_FLOOR_ROWS_PER_SEC = 5_000


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #


def fetch_rows(db_path: Path) -> list[sqlite3.Row]:
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    try:
        return conn.execute(
            "SELECT id, path, name, parent_id, is_dir, size, allocated, mtime, attrs, "
            "hardlink_flag, depth, ext FROM entries ORDER BY id"
        ).fetchall()
    finally:
        conn.close()


def write_csv(
    tmp_path: Path, text: str, *, name: str = "export.csv", encoding: str = "utf-8"
) -> Path:
    path = tmp_path / name
    path.write_text(text, encoding=encoding, newline="")
    return path


def epoch(text: str) -> int:
    return int(datetime.strptime(text, "%Y/%m/%d %H:%M:%S").replace(tzinfo=UTC).timestamp())


def run_cli(*args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, "-m", "spacesage", *args],
        capture_output=True,
        text=True,
        check=False,
    )


def assert_folder_totals_consistent(db_path: Path) -> None:
    """Folder rows equal the sum of their descendant *file* rows (never summed with them)."""
    rows = fetch_rows(db_path)
    children: dict[int | None, list[sqlite3.Row]] = {}
    for row in rows:
        children.setdefault(row["parent_id"], []).append(row)
    memo: dict[int, int] = {}

    def subtree_file_bytes(row: sqlite3.Row) -> int:
        if not row["is_dir"]:
            return int(row["size"])
        if row["id"] in memo:
            return memo[row["id"]]
        total = sum(subtree_file_bytes(child) for child in children.get(row["id"], []))
        memo[row["id"]] = total
        return total

    for row in rows:
        if row["is_dir"]:
            assert row["size"] == subtree_file_bytes(row), f"folder row mismatch: {row['path']}"


def assert_matches_truth(db_path: Path, truth: gen.GroundTruth) -> None:
    rows = fetch_rows(db_path)
    assert len(rows) == len(truth.entries)
    id_to_path = {row["id"]: row["path"] for row in rows}
    for row, expected in zip(rows, truth.entries, strict=True):
        assert row["path"] == expected.path
        assert row["name"] == expected.name
        assert bool(row["is_dir"]) is expected.is_dir
        assert row["size"] == expected.size
        assert row["allocated"] == expected.allocated
        assert row["mtime"] == expected.mtime, (
            f"{row['path']}: mtime {row['mtime']} != {expected.mtime}"
        )
        assert row["attrs"] == expected.attrs
        assert bool(row["hardlink_flag"]) is expected.hardlink
        assert row["depth"] == expected.depth, (
            f"{row['path']}: depth {row['depth']} != {expected.depth}"
        )
        assert row["ext"] == expected.ext, f"{row['path']}: ext {row['ext']!r} != {expected.ext!r}"
        parent = id_to_path[row["parent_id"]] if row["parent_id"] is not None else None
        assert parent == expected.parent_path, (
            f"{row['path']}: parent {parent!r} != {expected.parent_path!r}"
        )


# --------------------------------------------------------------------------- #
# Parsing helpers
# --------------------------------------------------------------------------- #


def test_detect_encoding_variants() -> None:
    assert detect_encoding(b"") == "utf-8-sig"
    assert detect_encoding(b'"File Name","Size"\n') == "utf-8-sig"
    assert detect_encoding("\ufeffFile Name,Size\n".encode("utf-8")) == "utf-8-sig"
    assert detect_encoding('"File Name"\n'.encode("utf-16")) == "utf-16"
    assert detect_encoding('"File Name"\n'.encode("utf-16-le")) == "utf-16-le"
    assert detect_encoding('"File Name"\n'.encode("utf-16-be")) == "utf-16-be"


def test_normalise_path_rules() -> None:
    assert normalise_path("C:\\", is_dir=True) == "C:\\"  # drive root keeps its separator
    assert normalise_path("C:\\Windows\\", is_dir=True) == "C:\\Windows"
    assert normalise_path("C:\\Windows", is_dir=True) == "C:\\Windows"
    assert normalise_path("C:\\pagefile.sys", is_dir=False) == "C:\\pagefile.sys"


def test_parent_path_rules() -> None:
    assert parent_path("C:\\", is_dir=True) is None
    assert parent_path("C:\\Windows\\", is_dir=True) == "C:\\"
    assert parent_path("C:\\Windows\\System32\\", is_dir=True) == "C:\\Windows"
    assert parent_path("C:\\Windows\\notepad.exe", is_dir=False) == "C:\\Windows"
    assert parent_path("C:\\pagefile.sys", is_dir=False) == "C:\\"


def test_extract_name_and_ext() -> None:
    assert extract_name("C:\\", is_dir=True) == "C:"
    assert extract_name("C:\\Windows\\", is_dir=True) == "Windows"
    assert extract_name("C:\\Windows\\notepad.exe", is_dir=False) == "notepad.exe"
    assert extract_ext("C:\\Windows\\notepad.exe") == "exe"
    assert extract_ext("C:\\a\\ARCHIVE.TAR.GZ") == "gz"
    assert extract_ext("C:\\a\\no_extension") == ""
    assert extract_ext("C:\\a\\.gitignore") == ""
    assert extract_ext("C:\\a\\trailing.") == ""


def test_path_depth_counts_components_below_root() -> None:
    assert path_depth("C:\\", is_dir=True) == 0
    assert path_depth("C:\\Windows\\", is_dir=True) == 1
    assert path_depth("C:\\Windows\\System32", is_dir=True) == 2
    assert path_depth("C:\\pagefile.sys", is_dir=False) == 1
    assert path_depth("C:\\Windows\\notepad.exe", is_dir=False) == 2


# --------------------------------------------------------------------------- #
# Committed fixtures
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class FixtureExpectation:
    rows: int
    files: int
    dirs: int
    total_file_bytes: int
    allocated_bytes: int
    unique_allocated_bytes: int
    hardlink_files: int = 0


FIXTURES: dict[str, FixtureExpectation] = {
    # quoted fields, CRLF, folder rows, root row
    "basic.csv": FixtureExpectation(7, 3, 4, 3_001_000, 3_100_672, 3_100_672),
    # names with commas, escaped quotes, unicode; unquoted header
    "quoted_commas.csv": FixtureExpectation(5, 2, 3, 1_000_000, 1_024_000, 1_024_000),
    # reordered header, extra columns, no Folders column
    "extra_columns.csv": FixtureExpectation(4, 2, 2, 5_000, 12_288, 12_288),
    # UTF-16 LE with BOM
    "utf16.csv": FixtureExpectation(4, 2, 2, 120_000, 126_976, 126_976),
    # hardlink marker (leading zero), zero allocated, zero-byte file
    "hardlinks.csv": FixtureExpectation(6, 4, 2, 3_000_000, 2_097_152, 1_048_576, 1),
    # plain 7 columns plus a drive summary row ("H:")
    "capacity_variant.csv": FixtureExpectation(3, 2, 1, 1_276_590, 1_313_792, 1_313_792),
    # blank lines, short rows, long rows, empty name cell
    "ragged.csv": FixtureExpectation(4, 3, 1, 9_000, 6_000, 6_000),
}


@pytest.mark.parametrize("name", sorted(FIXTURES))
def test_fixture_ingest_matches_expected_counts(name: str, tmp_path: Path) -> None:
    expected = FIXTURES[name]
    db_path = tmp_path / "index.db"
    stats = ingest_csv(DATA_DIR / name, db_path)

    assert (stats.rows, stats.files, stats.dirs) == (expected.rows, expected.files, expected.dirs)
    assert stats.total_file_bytes == expected.total_file_bytes
    assert stats.allocated_bytes == expected.allocated_bytes
    assert stats.unique_allocated_bytes == expected.unique_allocated_bytes
    assert stats.hardlink_files == expected.hardlink_files

    # bytes straight out of the index (not just the counters)
    summary = db.index_summary(db.open_db(db_path))
    assert summary.entries == expected.rows
    assert summary.files == expected.files
    assert summary.dirs == expected.dirs
    assert summary.file_bytes == expected.total_file_bytes
    assert summary.hardlink_files == expected.hardlink_files

    assert_folder_totals_consistent(db_path)


def test_basic_fixture_structures(tmp_path: Path) -> None:
    db_path = tmp_path / "index.db"
    ingest_csv(DATA_DIR / "basic.csv", db_path)
    rows = {row["path"]: row for row in fetch_rows(db_path)}

    assert set(rows) == {
        "C:\\",
        "C:\\Program Files",
        "C:\\Program Files\\App",
        "C:\\Program Files\\App\\app.exe",
        "C:\\Program Files\\App\\readme.txt",
        "C:\\Temp",
        "C:\\Temp\\scratch.tmp",
    }
    root = rows["C:\\"]
    assert root["parent_id"] is None
    assert root["is_dir"] == 1
    assert root["name"] == "C:"
    assert root["depth"] == 0
    assert root["ext"] is None
    assert root["mtime"] == epoch("2020/01/01 10:00:00")

    app = rows["C:\\Program Files\\App"]
    assert app["parent_id"] == rows["C:\\Program Files"]["id"]
    assert app["depth"] == 2

    exe = rows["C:\\Program Files\\App\\app.exe"]
    assert exe["parent_id"] == app["id"]
    assert exe["is_dir"] == 0
    assert exe["size"] == 2_000_000
    assert exe["allocated"] == 2_048_000
    assert exe["ext"] == "exe"
    assert exe["hardlink_flag"] == 0
    assert exe["attrs"] == "00000020"


def test_quoted_names_survive_round_trip(tmp_path: Path) -> None:
    db_path = tmp_path / "index.db"
    ingest_csv(DATA_DIR / "quoted_commas.csv", db_path)
    paths = {row["path"] for row in fetch_rows(db_path)}
    assert 'E:\\Music, Videos\\AC, DC\\Back "Live", 1992.mp3' in paths
    assert "E:\\Music, Videos\\L\u00e9on - d\u00e9j\u00e0 vu.flac" in paths
    assert "E:\\Music, Videos\\AC, DC" in paths


def test_hardlink_marker_flagged_and_counted_once(tmp_path: Path) -> None:
    db_path = tmp_path / "index.db"
    stats = ingest_csv(DATA_DIR / "hardlinks.csv", db_path)
    rows = {row["path"]: row for row in fetch_rows(db_path)}

    marked = rows["G:\\Store\\blob link.dat"]
    assert marked["hardlink_flag"] == 1
    assert marked["allocated"] == 1_048_576  # leading zero stripped, value kept

    source = rows["G:\\Store\\blob.dat"]
    assert source["hardlink_flag"] == 0
    assert source["allocated"] == 1_048_576 == marked["allocated"]

    # zero is not a marker ("0" has no leading zero to strip), even for 0-byte rows
    assert rows["G:\\Store\\empty.bin"]["hardlink_flag"] == 0
    assert rows["G:\\Store\\empty.bin"]["allocated"] == 0
    assert rows["G:\\Store\\sparse.img"]["hardlink_flag"] == 0

    assert stats.hardlink_files == 1
    assert stats.allocated_bytes == 2_097_152  # both copies counted raw
    assert stats.unique_allocated_bytes == 1_048_576  # payload counted once


def test_capacity_variant_row_is_skipped(tmp_path: Path) -> None:
    db_path = tmp_path / "index.db"
    stats = ingest_csv(DATA_DIR / "capacity_variant.csv", db_path)
    assert stats.capacity_rows == 1
    assert stats.drives_recorded == 0  # no free-space data in a plain summary row
    assert db.entry_count(db.open_db(db_path)) == 3
    assert db.meta_get(db.open_db(db_path), "ingest.capacity_rows") == "1"
    # the summary row must not appear as an entry (it is named "H:", not "H:\")
    assert all(row["path"] != "H:" for row in fetch_rows(db_path))


def test_ragged_fixture_reports_skips(tmp_path: Path) -> None:
    db_path = tmp_path / "index.db"
    stats = ingest_csv(DATA_DIR / "ragged.csv", db_path)
    assert (stats.blank_rows, stats.short_rows, stats.long_rows) == (2, 2, 1)
    assert stats.empty_name_rows == 1
    assert stats.rows == 4
    rows = {row["path"]: row for row in fetch_rows(db_path)}
    # missing cells become NULL/empty, never a crash
    assert rows["I:\\short.txt"]["allocated"] is None
    assert rows["I:\\short.txt"]["mtime"] is None
    assert rows["I:\\nodate.bin"]["mtime"] is None
    assert rows["I:\\long.bin"]["allocated"] == 6_000


def test_utf16_fixture_reports_encoding(tmp_path: Path) -> None:
    db_path = tmp_path / "index.db"
    stats = ingest_csv(DATA_DIR / "utf16.csv", db_path)
    assert stats.encoding == "utf-16"
    rows = {row["path"]: row for row in fetch_rows(db_path)}
    assert rows["F:\\Videos\\clip 01.mp4"]["size"] == 100_000
    assert rows["F:\\Videos\\clip 02.mp4"]["size"] == 20_000


def test_extra_columns_and_reordered_header(tmp_path: Path) -> None:
    db_path = tmp_path / "index.db"
    stats = ingest_csv(DATA_DIR / "extra_columns.csv", db_path)
    assert stats.header[:5] == ("Modified", "File Name", "Size", "Allocated", "Attributes")
    rows = {row["path"]: row for row in fetch_rows(db_path)}
    assert rows["F:\\Games\\save.dat"]["size"] == 4_000
    assert rows["F:\\Games\\save.dat"]["mtime"] == epoch("2023/07/04 12:34:56")
    assert rows["F:\\"]["parent_id"] is None


# --------------------------------------------------------------------------- #
# Generator ground truth (full round trip)
# --------------------------------------------------------------------------- #


ROUND_TRIP_VARIANTS: dict[str, gen.GenOptions] = {
    "default": gen.GenOptions(seed=1, min_files=200),
    "nasty-names": gen.GenOptions(
        seed=2,
        min_files=150,
        unicode_ratio=1.0,
        comma_ratio=1.0,
        quote_ratio=1.0,
        long_name_ratio=0.2,
    ),
    "hardlink-heavy": gen.GenOptions(
        seed=3, min_files=120, hardlink_ratio=0.5, zero_byte_ratio=0.3
    ),
    "extra-columns": gen.GenOptions(seed=4, min_files=150, header_style="extra"),
    "utf16-bom": gen.GenOptions(seed=5, min_files=120, encoding="utf-16", capacity_row=True),
    "utf8-bom-empty-times": gen.GenOptions(
        seed=6, min_files=120, encoding="utf-8-sig", empty_dir_mtime_ratio=1.0
    ),
}


@pytest.mark.parametrize("variant", sorted(ROUND_TRIP_VARIANTS))
def test_generated_export_round_trips_exactly(variant: str, tmp_path: Path) -> None:
    options = ROUND_TRIP_VARIANTS[variant]
    generated = gen.generate(tmp_path / "export.csv", options)
    truth = generated.truth
    db_path = tmp_path / "index.db"
    stats = ingest_csv(generated.path, db_path)

    assert stats.rows == generated.rows == truth.files + truth.dirs
    assert stats.files == truth.files
    assert stats.dirs == truth.dirs
    assert stats.total_file_bytes == truth.total_file_bytes
    assert stats.allocated_bytes == truth.allocated_bytes
    assert stats.unique_allocated_bytes == truth.unique_allocated_bytes
    assert stats.hardlink_files == truth.hardlink_files
    assert stats.capacity_rows == truth.capacity_rows

    assert_matches_truth(db_path, truth)
    assert_folder_totals_consistent(db_path)


def test_generation_is_deterministic(tmp_path: Path) -> None:
    options = gen.GenOptions(seed=77, min_files=60)
    first = gen.generate(tmp_path / "a.csv", options)
    second = gen.generate(tmp_path / "b.csv", options)
    assert first.path.read_bytes() == second.path.read_bytes()
    assert first.truth == second.truth


def test_nasty_names_in_generated_export(tmp_path: Path) -> None:
    generated = gen.generate(
        tmp_path / "export.csv",
        gen.GenOptions(seed=9, min_files=100, unicode_ratio=1.0, comma_ratio=1.0, quote_ratio=1.0),
    )
    raw = generated.path.read_text(encoding="utf-8")
    assert "日本語" in raw
    assert '"' in raw.replace('""', "")  # escaped quotes present in the file itself
    ingest_csv(generated.path, tmp_path / "index.db")
    names = [row["name"] for row in fetch_rows(tmp_path / "index.db")]
    assert any("," in name for name in names)
    assert any('"' in name for name in names)
    assert any("日本語" in name for name in names)


# --------------------------------------------------------------------------- #
# Robustness
# --------------------------------------------------------------------------- #


def test_duplicate_paths_are_counted_and_ignored(tmp_path: Path) -> None:
    csv = write_csv(
        tmp_path,
        "File Name,Size,Allocated,Modified,Attributes,Files,Folders\n"
        "C:\\,300,300,2020/01/01 00:00:00,00000010,2,0\n"
        "C:\\a.bin,100,100,2020/01/01 00:00:00,00000020,0,0\n"
        "C:\\a.bin,100,100,2020/01/01 00:00:00,00000020,0,0\n"
        "C:\\b.bin,200,200,2020/01/01 00:00:00,00000020,0,0\n",
    )
    stats = ingest_csv(csv, tmp_path / "index.db")
    assert stats.duplicate_rows == 1
    assert stats.rows == 3
    assert db.entry_count(db.open_db(tmp_path / "index.db")) == 3


@pytest.mark.parametrize("batch_size", [2, 50_000])
def test_duplicate_folder_rows_keep_children_on_the_first_occurrence(
    tmp_path: Path, batch_size: int
) -> None:
    csv = write_csv(
        tmp_path,
        "File Name,Size,Allocated,Modified,Attributes,Files,Folders\n"
        "C:\\,300,300,2020/01/01 00:00:00,00000010,2,1\n"
        "C:\\Dir\\,100,100,2020/01/01 00:00:00,00000010,1,0\n"
        "C:\\Dir\\a.bin,100,100,2020/01/01 00:00:00,00000020,0,0\n"
        "C:\\Dir\\,150,150,2020/01/01 00:00:00,00000010,1,0\n"
        "C:\\Dir\\b.bin,200,200,2020/01/01 00:00:00,00000020,0,0\n",
    )
    db_path = tmp_path / "index.db"
    stats = ingest_csv(csv, db_path, batch_size=batch_size)
    assert stats.duplicate_rows == 1
    assert stats.rows == 4
    assert db.entry_count(db.open_db(db_path)) == 4
    rows = {row["path"]: row for row in fetch_rows(db_path)}
    # both children hang off the first (kept) folder row, never a skipped one
    assert rows["C:\\Dir\\a.bin"]["parent_id"] == rows["C:\\Dir"]["id"]
    assert rows["C:\\Dir\\b.bin"]["parent_id"] == rows["C:\\Dir"]["id"]


def test_orphan_rows_and_out_of_order_parent_lookup(tmp_path: Path) -> None:
    csv = write_csv(
        tmp_path,
        "File Name,Size,Allocated,Modified,Attributes,Files,Folders\n"
        "C:\\lost\\first.bin,100,4096,2020/01/01 00:00:00,00000020,0,0\n"
        "C:\\,400,8192,2020/01/01 00:00:00,00000010,2,1\n"
        "C:\\lost\\,200,4096,2020/01/01 00:00:00,00000010,2,0\n"
        "C:\\lost\\second.bin,100,4096,2020/01/01 00:00:00,00000020,0,0\n",
    )
    db_path = tmp_path / "index.db"
    stats = ingest_csv(csv, db_path)
    assert stats.orphan_rows == 1  # the first row's parent did not exist yet
    rows = {row["path"]: row for row in fetch_rows(db_path)}
    assert rows["C:\\lost\\first.bin"]["parent_id"] is None
    # the late parent is found through the index for subsequent rows
    assert rows["C:\\lost\\second.bin"]["parent_id"] == rows["C:\\lost"]["id"]


def test_missing_optional_columns(tmp_path: Path) -> None:
    csv = write_csv(
        tmp_path,
        "File Name,Size\nC:\\,1500\nC:\\a.bin,1000\nC:\\b.bin,500\n",
    )
    db_path = tmp_path / "index.db"
    stats = ingest_csv(csv, db_path)
    assert stats.has_allocated is False
    assert stats.allocated_bytes is None
    assert stats.unique_allocated_bytes is None
    rows = fetch_rows(db_path)
    assert all(row["allocated"] is None for row in rows)
    assert all(row["mtime"] is None for row in rows)
    assert all(row["attrs"] is None for row in rows)
    assert rows[1]["ext"] == "bin"


def test_capacity_row_with_free_column_records_drive(tmp_path: Path) -> None:
    csv = write_csv(
        tmp_path,
        "File Name,Size,Allocated,Modified,Attributes,Files,Folders,Free\n"
        "C:\\,3000,3000,2020/01/01 00:00:00,00000010,2,0,\n"
        "C:\\a.bin,1000,1000,2020/01/01 00:00:00,00000020,0,0,\n"
        "C:\\b.bin,2000,2000,2020/01/01 00:00:00,00000020,0,0,\n"
        "C:,500107862016,123456789,,,,,123456789\n",
    )
    db_path = tmp_path / "index.db"
    stats = ingest_csv(csv, db_path)
    assert (stats.capacity_rows, stats.drives_recorded) == (1, 1)
    conn = db.open_db(db_path)
    drive = conn.execute("SELECT name, capacity_bytes, free_bytes FROM drives").fetchall()
    assert drive == [("C:", 500_107_862_016, 123_456_789)]
    assert db.entry_count(conn) == 3


def test_root_row_with_empty_metadata_is_treated_as_capacity(tmp_path: Path) -> None:
    csv = write_csv(
        tmp_path,
        "File Name,Size,Allocated,Modified,Attributes,Files,Folders\n"
        "C:\\,3000,3000,2020/01/01 00:00:00,00000010,2,0\n"
        "C:\\a.bin,1000,1000,2020/01/01 00:00:00,00000020,0,0\n"
        "C:\\b.bin,2000,2000,2020/01/01 00:00:00,00000020,0,0\n"
        "C:\\,999,,,, ,\n",
    )
    stats = ingest_csv(csv, tmp_path / "index.db")
    assert stats.capacity_rows == 1
    assert stats.rows == 3


def test_batching_and_progress_callback(tmp_path: Path) -> None:
    events: list[tuple[int, int]] = []
    db_path = tmp_path / "index.db"
    stats = ingest_csv(
        DATA_DIR / "basic.csv",
        db_path,
        batch_size=2,
        progress=lambda update: events.append((update.rows_read, update.bytes_read)),
    )
    # 7 rows with batch_size=2 -> flushes after rows 2, 4, 6 and on finish
    assert [rows_read for rows_read, _ in events] == [2, 4, 6, 7]
    assert events[-1][1] > 0
    assert stats.rows == 7


def test_loader_flushes_incrementally_for_larger_trees(tmp_path: Path) -> None:
    generated = gen.generate(
        tmp_path / "export.csv",
        gen.GenOptions(seed=21, min_files=3_000, collect_entries=False),
    )
    flushes = 0

    def count(_update: object) -> None:
        nonlocal flushes
        flushes += 1

    stats = ingest_csv(generated.path, tmp_path / "index.db", batch_size=1_000, progress=count)
    assert stats.rows >= 3_000
    assert flushes >= stats.rows // 1_000


def test_empty_file_and_error_conditions(tmp_path: Path) -> None:
    empty = write_csv(tmp_path, "", name="empty.csv")
    with pytest.raises(IngestError, match="empty"):
        ingest_csv(empty, tmp_path / "a.db")

    with pytest.raises(IngestError, match="not found"):
        ingest_csv(tmp_path / "missing.csv", tmp_path / "b.db")

    no_path = write_csv(tmp_path, "Foo,Bar\n1,2\n", name="nopath.csv")
    with pytest.raises(IngestError, match="file-name column"):
        ingest_csv(no_path, tmp_path / "c.db")

    no_size = write_csv(tmp_path, "File Name,Allocated\nC:\\,1\n", name="nosize.csv")
    with pytest.raises(IngestError, match="Size"):
        ingest_csv(no_size, tmp_path / "d.db")


def test_reingest_requires_replace_or_empty_index(tmp_path: Path) -> None:
    db_path = tmp_path / "index.db"
    first = ingest_csv(DATA_DIR / "basic.csv", db_path)
    with pytest.raises(IngestError, match="already contains rows"):
        ingest_csv(DATA_DIR / "quoted_commas.csv", db_path)

    second = ingest_csv(DATA_DIR / "quoted_commas.csv", db_path, replace=True)
    rows = fetch_rows(db_path)
    assert first.rows == 7
    assert second.rows == 5
    assert len(rows) == 5
    # the reload reset the index: ids are contiguous again
    assert [row["id"] for row in rows] == list(range(1, 6))
    meta = db.meta_get(db.open_db(db_path), "source.csv")
    assert meta is not None and meta.endswith("quoted_commas.csv")


# --------------------------------------------------------------------------- #
# Schema / meta
# --------------------------------------------------------------------------- #


def test_schema_is_versioned_with_wal_and_indexes(tmp_path: Path) -> None:
    db_path = tmp_path / "index.db"
    ingest_csv(DATA_DIR / "basic.csv", db_path)
    conn = db.open_db(db_path)
    assert db.schema_version(conn) == db.SCHEMA_VERSION == 3
    assert conn.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
    tables = {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}
    assert {"meta", "drives", "entries"} <= tables
    indexes = {
        row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type = 'index'")
    }
    expected_indexes = {
        "idx_entries_size",
        "idx_entries_parent",
        "idx_entries_mtime",
        "idx_entries_ext",
    }
    assert expected_indexes <= indexes


def test_migrations_are_idempotent_and_guard_against_newer_schemas(tmp_path: Path) -> None:
    db_path = tmp_path / "index.db"
    ingest_csv(DATA_DIR / "basic.csv", db_path)

    conn = db.open_db(db_path)  # re-open: no migration churn, data intact
    assert db.entry_count(conn) == 7

    conn.execute(f"PRAGMA user_version={db.SCHEMA_VERSION + 1}")
    with pytest.raises(db.SchemaError, match="newer"):
        db.apply_migrations(conn)


def test_meta_records_provenance_and_run_stats(tmp_path: Path) -> None:
    db_path = tmp_path / "index.db"
    stats = ingest_csv(DATA_DIR / "basic.csv", db_path)
    conn = db.open_db(db_path)
    meta = dict(conn.execute("SELECT key, value FROM meta").fetchall())

    assert meta["schema.version"] == str(db.SCHEMA_VERSION)
    assert meta["source.csv"] == str((DATA_DIR / "basic.csv").resolve())
    assert meta["ingest.rows"] == str(stats.rows)
    assert meta["ingest.files"] == "3"
    assert meta["ingest.dirs"] == "4"
    assert meta["ingest.total_file_bytes"] == "3001000"
    assert meta["ingest.encoding"] == "utf-8-sig"
    assert meta["ingest.finished_at"].endswith("+00:00")
    assert int(meta["source.csv_bytes"]) > 0


def test_resolve_db_path_accepts_directory(tmp_path: Path) -> None:
    file_target = tmp_path / "custom.db"
    assert db.resolve_db_path(file_target) == file_target
    directory = tmp_path / "indexdir"
    directory.mkdir()
    assert db.resolve_db_path(directory) == directory / db.DEFAULT_DB_NAME


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #

ROWS_RE = re.compile(r"^rows: (\d+) \(files: (\d+), dirs: (\d+)\)$", re.MULTILINE)
BYTES_RE = re.compile(r"^total file bytes: (\d+)$", re.MULTILINE)
DURATION_RE = re.compile(r"^duration: (\d+\.\d+) s$", re.MULTILINE)
RATE_RE = re.compile(r"^rows/sec: (\d+)$", re.MULTILINE)


def test_cli_ingest_prints_required_stats(tmp_path: Path) -> None:
    db_path = tmp_path / "index.db"
    result = run_cli("ingest", str(DATA_DIR / "basic.csv"), "--db", str(db_path))
    assert result.returncode == 0, result.stderr
    out = result.stdout

    rows_match = ROWS_RE.search(out)
    assert rows_match is not None
    assert [int(value) for value in rows_match.groups()] == [7, 3, 4]
    assert BYTES_RE.search(out).group(1) == "3001000"  # type: ignore[union-attr]
    assert float(DURATION_RE.search(out).group(1)) >= 0  # type: ignore[union-attr]
    assert int(RATE_RE.search(out).group(1)) > 0  # type: ignore[union-attr]
    assert f"db: {db_path}" in out
    assert db_path.is_file()
    assert db.entry_count(db.open_db(db_path)) == 7


def test_cli_ingest_into_directory_creates_default_db(tmp_path: Path) -> None:
    index_dir = tmp_path / "index"
    index_dir.mkdir()
    result = run_cli("ingest", str(DATA_DIR / "basic.csv"), "--db", str(index_dir))
    assert result.returncode == 0, result.stderr
    assert (index_dir / db.DEFAULT_DB_NAME).is_file()


def test_cli_refuses_existing_index_without_replace(tmp_path: Path) -> None:
    db_path = tmp_path / "index.db"
    assert run_cli("ingest", str(DATA_DIR / "basic.csv"), "--db", str(db_path)).returncode == 0
    second = run_cli("ingest", str(DATA_DIR / "basic.csv"), "--db", str(db_path))
    assert second.returncode == 1
    assert "error:" in second.stderr and "already contains rows" in second.stderr

    replaced = run_cli(
        "ingest", str(DATA_DIR / "quoted_commas.csv"), "--db", str(db_path), "--replace"
    )
    assert replaced.returncode == 0, replaced.stderr
    assert db.entry_count(db.open_db(db_path)) == 5


def test_cli_reports_missing_file_and_bad_header(tmp_path: Path) -> None:
    missing = run_cli("ingest", str(tmp_path / "nope.csv"), "--db", str(tmp_path / "a.db"))
    assert missing.returncode == 1
    assert "not found" in missing.stderr

    bad = write_csv(tmp_path, "Colour,Weight\nred,1\n", name="bad.csv")
    result = run_cli("ingest", str(bad), "--db", str(tmp_path / "b.db"))
    assert result.returncode == 1
    assert "file-name column" in result.stderr


def test_cli_progress_flag_writes_to_stderr(tmp_path: Path) -> None:
    result = run_cli(
        "ingest",
        str(DATA_DIR / "basic.csv"),
        "--db",
        str(tmp_path / "index.db"),
        "--progress",
    )
    assert result.returncode == 0, result.stderr
    assert "progress:" in result.stderr
    assert "rows/s" in result.stderr


# --------------------------------------------------------------------------- #
# Performance smoke (slow)
# --------------------------------------------------------------------------- #


@pytest.mark.slow
def test_perf_smoke_250k_rows(tmp_path: Path) -> None:
    """250k+ rows stream through the loader; prints rows/sec; floor guards regressions."""
    generated = gen.generate(
        tmp_path / "perf.csv",
        gen.GenOptions(
            seed=4242,
            min_files=250_000,
            files_per_dir=12,
            dirs_per_dir=4,
            min_depth=6,
            collect_entries=False,
        ),
    )
    db_path = tmp_path / "perf.db"
    started = time.perf_counter()
    stats = ingest_csv(generated.path, db_path)
    elapsed = time.perf_counter() - started
    csv_mb = os.path.getsize(generated.path) / 1e6
    print(
        f"\nperf smoke: {stats.rows} rows ({stats.files} files / {stats.dirs} dirs) "
        f"from {csv_mb:.1f} MB in {elapsed:.2f}s -> {stats.rows_per_sec:,.0f} rows/s "
        f"(design target >= 1M rows/min)"
    )

    assert stats.rows >= 250_000
    assert stats.files == generated.truth.files
    assert stats.dirs == generated.truth.dirs
    assert stats.total_file_bytes == generated.truth.total_file_bytes
    assert stats.hardlink_files == generated.truth.hardlink_files
    assert stats.rows_per_sec >= PERF_FLOOR_ROWS_PER_SEC
    assert elapsed < 240, f"250k rows took {elapsed:.1f}s"
