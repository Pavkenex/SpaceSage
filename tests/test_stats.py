"""Stats tests: dir/ext/age/app aggregates, cross-check, derived tables, CLI."""

from __future__ import annotations

import json
import sqlite3
import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path

import pytest

from fixtures import gen
from spacesage import db, stats
from spacesage.ingest import ingest_csv
from spacesage.stats import (
    DEFAULT_AGE_BUCKETS,
    UNKNOWN_AGE_BUCKET,
    AppFootprint,
    DirSize,
    StatsError,
)

DATA_DIR = Path(__file__).resolve().parent / "fixtures" / "data"

#: Fixed reference point for the age buckets (2026-09-12 12:00:00 UTC).
NOW = int(datetime(2026, 9, 12, 12, 0, 0, tzinfo=UTC).timestamp())

DAY = 86_400


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #


def ingest_fixture(tmp_path: Path, name: str, *, db_name: str = "index.db") -> Path:
    """Ingest a committed fixture and return the index path."""
    db_path = tmp_path / db_name
    ingest_csv(DATA_DIR / name, db_path)
    return db_path


def open_index(db_path: Path) -> sqlite3.Connection:
    return db.open_db(db_path)


def run_cli(*args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, "-m", "spacesage", *args],
        capture_output=True,
        text=True,
        check=False,
    )


def dir_size_map(conn: sqlite3.Connection) -> dict[str, DirSize]:
    return {size.path: size for size in stats.iter_dir_sizes(conn)}


def folder_sums_from_rows(db_path: Path) -> dict[str, int]:
    """Recompute each folder's descendant file sum straight from the rows."""
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    try:
        rows = conn.execute("SELECT id, parent_id, path, is_dir, size FROM entries").fetchall()
    finally:
        conn.close()
    children: dict[int | None, list[sqlite3.Row]] = {}
    for row in rows:
        children.setdefault(row["parent_id"], []).append(row)
    memo: dict[int, int] = {}

    def subtree(row: sqlite3.Row) -> int:
        if not row["is_dir"]:
            return int(row["size"])
        if row["id"] not in memo:
            memo[row["id"]] = sum(subtree(child) for child in children.get(row["id"], []))
        return memo[row["id"]]

    return {str(row["path"]): subtree(row) for row in rows if row["is_dir"]}


def truth_dir_totals(truth: gen.GroundTruth) -> dict[str, tuple[int, int, int]]:
    """``path -> (subtree bytes, unique bytes, file count)`` from generator truth."""
    dirs = {entry.path: entry for entry in truth.entries if entry.is_dir}
    totals: dict[str, list[int]] = {path: [0, 0, 0] for path in dirs}
    for entry in truth.entries:
        if entry.is_dir:
            continue
        assert entry.parent_path is not None
        bucket = totals[entry.parent_path]
        bucket[0] += entry.size
        bucket[1] += 0 if entry.hardlink else entry.size
        bucket[2] += 1
    for entry in sorted(dirs.values(), key=lambda item: -item.depth):
        if entry.parent_path is None:
            continue
        parent = totals[entry.parent_path]
        here = totals[entry.path]
        parent[0] += here[0]
        parent[1] += here[1]
        parent[2] += here[2]
    return {path: (values[0], values[1], values[2]) for path, values in totals.items()}


def truth_extension_totals(truth: gen.GroundTruth) -> dict[str, tuple[int, int]]:
    """``ext -> (bytes, unique bytes)`` from generator truth."""
    totals: dict[str, tuple[int, int]] = {}
    for entry in truth.entries:
        if entry.is_dir:
            continue
        key = entry.ext or ""
        size, unique = totals.get(key, (0, 0))
        totals[key] = (size + entry.size, unique + (0 if entry.hardlink else entry.size))
    return totals


def expected_bucket(mtime: int | None, now: int) -> str:
    """Independent label lookup for one timestamp."""
    if mtime is None:
        return UNKNOWN_AGE_BUCKET
    age = now - mtime
    for label, limit in DEFAULT_AGE_BUCKETS:
        if limit is None or age <= limit * DAY:
            return label
    raise AssertionError(f"no bucket for {mtime}")  # pragma: no cover - open-ended by construction


def bucket_map(conn: sqlite3.Connection, now: int) -> dict[str, stats.AgeBucket]:
    return {bucket.label: bucket for bucket in stats.age_buckets(conn, now=now)}


def write_csv(tmp_path: Path, text: str, *, name: str = "export.csv") -> Path:
    path = tmp_path / name
    path.write_text(text, encoding="utf-8", newline="")
    return path


# --------------------------------------------------------------------------- #
# Committed fixture: exact, hand-checked expectations
# --------------------------------------------------------------------------- #


def test_fixture_totals_come_from_file_rows_only(tmp_path: Path) -> None:
    db_path = ingest_fixture(tmp_path, "app_roots.csv")
    report = stats.stats_report(open_index(db_path), now=NOW, db_path=str(db_path))

    assert report.totals.entries == 28
    assert report.totals.dirs == 19
    assert report.totals.files == 9
    assert report.totals.file_bytes == 3_972_000
    assert report.totals.unique_file_bytes == 3_972_000  # no hardlinks in this fixture
    assert report.totals.allocated_bytes == 4_075_520
    assert report.totals.unique_allocated_bytes == 4_075_520
    assert report.totals.hardlink_files == 0
    assert report.allocation_available is True
    assert (report.unattributed_files, report.unattributed_bytes) == (0, 0)


def test_fixture_top_dirs_ranked_and_consistent_with_rows(tmp_path: Path) -> None:
    db_path = ingest_fixture(tmp_path, "app_roots.csv")
    conn = open_index(db_path)
    ranked = stats.top_dirs(conn, 6)

    assert [(size.path, size.bytes) for size in ranked] == [
        ("C:\\", 3_972_000),
        ("C:\\Program Files", 2_105_000),
        ("C:\\Program Files\\Widget", 2_100_000),
        ("C:\\Windows", 1_000_000),  # 1 MiB tie: shorter path sorts first
        ("C:\\Windows\\System32", 1_000_000),
        ("C:\\Program Files (x86)", 500_000),
    ]
    # own_bytes counts only the files directly inside the folder
    program_files = ranked[1]
    assert (program_files.own_bytes, program_files.file_count, program_files.dir_count) == (
        5_000,
        3,
        1,
    )
    assert program_files.child_dir_count == 1
    assert program_files.export_bytes == 2_105_000  # folder row carried verbatim

    # every folder's subtree sum equals a direct recomputation from the rows
    computed = folder_sums_from_rows(db_path)
    sizes = dir_size_map(conn)
    assert set(sizes) == set(computed)
    for path, expected in computed.items():
        assert sizes[path].bytes == expected, path

    root = sizes["C:\\"]
    assert root.parent_id is None
    assert root.depth == 0
    assert (root.bytes, root.unique_bytes) == (3_972_000, 3_972_000)
    assert root.dir_count == 18


def test_fixture_top_files_ranked(tmp_path: Path) -> None:
    db_path = ingest_fixture(tmp_path, "app_roots.csv")
    files = stats.top_files(open_index(db_path), 5)
    assert [(row.path, row.size) for row in files] == [
        ("C:\\Program Files\\Widget\\widget.exe", 2_000_000),
        ("C:\\Windows\\System32\\kernel.dll", 1_000_000),
        ("C:\\Program Files (x86)\\Legacy\\old.dll", 500_000),
        ("C:\\Users\\Alice\\AppData\\Local\\Widget\\cache.bin", 300_000),
        ("C:\\Program Files\\Widget\\readme.txt", 100_000),
    ]
    assert files[0].ext == "exe"
    assert files[0].hardlink is False
    assert files[0].allocated == 2_048_000
    assert files[0].mtime == int(datetime(2026, 9, 1, 10, 0, 0, tzinfo=UTC).timestamp())


def test_fixture_extension_totals(tmp_path: Path) -> None:
    db_path = ingest_fixture(tmp_path, "app_roots.csv")
    totals = stats.extension_totals(open_index(db_path))
    assert [(item.ext, item.files, item.bytes) for item in totals] == [
        ("exe", 1, 2_000_000),
        ("dll", 2, 1_500_000),
        ("bin", 2, 350_000),  # cache.bin + tmp.bin
        ("txt", 2, 105_000),
        ("json", 1, 10_000),
        ("tmp", 1, 7_000),
    ]
    assert all(item.unique_bytes == item.bytes for item in totals)


def test_fixture_age_buckets(tmp_path: Path) -> None:
    db_path = ingest_fixture(tmp_path, "app_roots.csv")
    buckets = bucket_map(open_index(db_path), NOW)
    assert [
        (item.label, item.files, item.bytes)
        for item in stats.age_buckets(open_index(db_path), now=NOW)
    ] == [
        ("<7d", 2, 15_000),  # settings.json + loose.txt
        ("7-30d", 2, 2_100_000),  # widget.exe + readme.txt
        ("30-90d", 0, 0),
        ("90-365d", 2, 1_300_000),  # cache.bin + kernel.dll
        (">1y", 3, 557_000),  # old.dll + tmp.bin + scratch.tmp
        (UNKNOWN_AGE_BUCKET, 0, 0),
    ]
    assert buckets["7-30d"].newest_mtime == int(
        datetime(2026, 9, 1, 10, 0, 0, tzinfo=UTC).timestamp()
    )
    assert buckets["7-30d"].oldest_mtime == int(
        datetime(2026, 8, 20, 10, 0, 0, tzinfo=UTC).timestamp()
    )
    # the buckets partition the file set exactly
    assert sum(bucket.files for bucket in buckets.values()) == 9
    assert sum(bucket.bytes for bucket in buckets.values()) == 3_972_000


def test_fixture_app_footprints_merge_spellings_and_users(tmp_path: Path) -> None:
    db_path = ingest_fixture(tmp_path, "app_roots.csv")
    conn = open_index(db_path)
    apps = stats.app_footprints(conn)
    by_name = {item.app: item for item in apps}
    assert set(by_name) == {"Widget", "Legacy"}

    widget = by_name["Widget"]
    assert widget.bytes == 2_460_000  # 2.0 M + 100 k (Program Files) + 300 k + 10 k + 50 k
    assert widget.unique_bytes == widget.bytes
    assert widget.file_count == 5
    assert widget.dir_count == 0
    assert widget.roots == (
        "C:\\Program Files\\Widget",
        "C:\\Users\\Alice\\AppData\\Local\\Widget",
        "C:\\Users\\Bob\\appdata\\local\\widget",  # lowercase spellings merge in
        "C:\\Users\\Alice\\AppData\\Roaming\\Widget",
    )

    legacy = by_name["Legacy"]
    assert (legacy.bytes, legacy.file_count, legacy.roots) == (
        500_000,
        1,
        ("C:\\Program Files (x86)\\Legacy",),
    )

    # direct files under an app root are not attributed to an app
    assert all("loose.txt" not in root for item in apps for root in item.roots)
    assert stats.app_roots(conn) and len(stats.app_roots(conn)) == 5
    assert list(stats.top_dirs(conn, 19))  # sanity: helper works on the same index


def test_fixture_cross_check_is_clean(tmp_path: Path) -> None:
    db_path = ingest_fixture(tmp_path, "app_roots.csv")
    conn = open_index(db_path)
    outcome = stats.folder_cross_check(conn)
    assert outcome.checked_dirs == 19
    assert outcome.ok
    assert outcome.warnings == ()
    report = stats.stats_report(conn, now=NOW)
    assert report.quality == outcome
    assert "data quality: ok" in stats.render_text(report)


# --------------------------------------------------------------------------- #
# Cross-check: injected disagreements
# --------------------------------------------------------------------------- #

SYSTEM32_ROW = '"C:\\Windows\\System32\\","1000000"'


def broken_fixture(tmp_path: Path, size: str, *, name: str = "broken.csv") -> Path:
    text = (DATA_DIR / "app_roots.csv").read_text(encoding="utf-8")
    assert SYSTEM32_ROW in text
    replacement = f'"C:\\Windows\\System32\\","{size}"'
    return write_csv(tmp_path, text.replace(SYSTEM32_ROW, replacement), name=name)


def test_injected_disagreement_raises_a_warning(tmp_path: Path) -> None:
    db_path = tmp_path / "index.db"
    ingest_csv(broken_fixture(tmp_path, "2048576"), db_path)
    conn = open_index(db_path)

    outcome = stats.folder_cross_check(conn)
    assert outcome.checked_dirs == 19
    assert len(outcome.warnings) == 1
    warning = outcome.warnings[0]
    assert warning.path == "C:\\Windows\\System32"
    assert warning.export_bytes == 2_048_576
    assert warning.computed_bytes == 1_000_000
    assert warning.delta_bytes == 1_048_576
    assert warning.tolerance_bytes == stats.DEFAULT_TOLERANCE_BYTES

    # the reported sizes still come from file rows, never from the lying row
    sizes = dir_size_map(conn)
    assert sizes["C:\\Windows\\System32"].bytes == 1_000_000
    assert sizes["C:\\Windows\\System32"].export_bytes == 2_048_576
    assert sizes["C:\\Windows"].bytes == 1_000_000  # the parent row is still consistent
    assert stats.top_files(conn, 1)[0].size == 2_000_000

    report = stats.stats_report(conn, now=NOW)
    text = stats.render_text(report)
    assert "1 folder disagree" in text
    assert "warning: C:\\Windows\\System32" in text
    payload = report.to_dict()
    quality = payload["data_quality"]
    assert isinstance(quality, dict)
    assert quality["warnings"] == [
        {
            "path": "C:\\Windows\\System32",
            "depth": 2,
            "export_bytes": 2_048_576,
            "computed_bytes": 1_000_000,
            "delta_bytes": 1_048_576,
            "tolerance_bytes": stats.DEFAULT_TOLERANCE_BYTES,
        }
    ]


def test_small_disagreements_stay_inside_the_tolerance(tmp_path: Path) -> None:
    db_path = tmp_path / "index.db"
    ingest_csv(broken_fixture(tmp_path, "1000100"), db_path)  # +100 bytes
    conn = open_index(db_path)

    assert stats.folder_cross_check(conn).ok  # 4 KiB floor absorbs it

    strict = stats.folder_cross_check(conn, tolerance_bytes=0, tolerance_ratio=0.0)
    assert [warning.path for warning in strict.warnings] == ["C:\\Windows\\System32"]
    assert strict.warnings[0].delta_bytes == 100

    # the relative tolerance absorbs small deltas on big folders ...
    assert stats.folder_cross_check(conn, tolerance_bytes=0, tolerance_ratio=0.001).ok
    # ... but an explicit ratio cannot rescue a delta above it
    assert stats.folder_cross_check(conn, tolerance_bytes=0, tolerance_ratio=0.00001)


def test_orphan_files_are_reported_as_unattributed(tmp_path: Path) -> None:
    csv = write_csv(
        tmp_path,
        "File Name,Size,Allocated,Modified,Attributes,Files,Folders\n"
        "C:\\lost\\a.bin,100000,100000,2026/09/01 00:00:00,00000020,0,0\n"
        "C:\\,400000,400000,2026/09/01 00:00:00,00000010,2,0\n"
        "C:\\b.bin,300000,300000,2026/09/01 00:00:00,00000020,0,0\n",
    )
    db_path = tmp_path / "index.db"
    stats_ingest = ingest_csv(csv, db_path)
    assert stats_ingest.orphan_rows == 1
    conn = open_index(db_path)

    report = stats.stats_report(conn, now=NOW)
    assert (report.unattributed_files, report.unattributed_bytes) == (1, 100_000)
    assert report.totals.file_bytes == 400_000  # still counted in the totals
    assert [size.bytes for size in report.dirs] == [300_000]  # but not in any folder
    # the folder row claims 400 k while its subtree holds 300 k: one warning
    assert [warning.path for warning in report.quality.warnings] == ["C:\\"]
    assert report.quality.warnings[0].delta_bytes == 100_000
    assert "unattributed: 1 file" in stats.render_text(report)


# --------------------------------------------------------------------------- #
# Hardlinks
# --------------------------------------------------------------------------- #


def test_hardlink_copies_count_once(tmp_path: Path) -> None:
    db_path = ingest_fixture(tmp_path, "hardlinks.csv")
    conn = open_index(db_path)
    report = stats.stats_report(conn, now=NOW)

    assert report.totals.files == 4
    assert report.totals.file_bytes == 3_000_000
    assert report.totals.unique_file_bytes == 2_000_000  # the flagged copy once
    assert report.totals.allocated_bytes == 2_097_152
    assert report.totals.unique_allocated_bytes == 1_048_576
    assert report.totals.hardlink_files == 1

    sizes = dir_size_map(conn)
    store = sizes["G:\\Store"]
    assert (store.bytes, store.unique_bytes) == (3_000_000, 2_000_000)
    assert (store.allocated_bytes, store.unique_allocated_bytes) == (2_097_152, 1_048_576)
    assert (store.file_count, store.unique_file_count) == (4, 3)
    assert sizes["G:\\"].bytes == store.bytes

    # top files: the copy is hidden by default (its bytes belong to the source)
    default_list = stats.top_files(conn, 10)
    assert [row.path for row in default_list] == [
        "G:\\Store\\blob.dat",
        "G:\\Store\\sparse.img",
        "G:\\Store\\empty.bin",
    ]
    listed = stats.top_files(conn, 10, include_hardlinks=True)
    assert "G:\\Store\\blob link.dat" in [row.path for row in listed]
    assert any(row.hardlink for row in listed)

    # extension totals: `dat` has two rows but one payload
    dat = next(item for item in stats.extension_totals(conn) if item.ext == "dat")
    assert (dat.files, dat.unique_files, dat.bytes, dat.unique_bytes) == (
        2,
        1,
        2_000_000,
        1_000_000,
    )


# --------------------------------------------------------------------------- #
# Age buckets: boundaries
# --------------------------------------------------------------------------- #


def test_age_bucket_boundaries(tmp_path: Path) -> None:
    csv = write_csv(
        tmp_path,
        "File Name,Size,Allocated,Modified,Attributes,Files,Folders\n"
        "C:\\,600,600,2026/09/01 00:00:00,00000010,6,0\n"
        "C:\\a_exact7d.bin,100,100,2026/09/05 12:00:00,00000020,0,0\n"
        "C:\\b_over7d.bin,100,100,2026/09/05 11:59:59,00000020,0,0\n"
        "C:\\c_exact365d.bin,100,100,2025/09/12 12:00:00,00000020,0,0\n"
        "C:\\d_over365d.bin,100,100,2025/09/12 11:59:59,00000020,0,0\n"
        "C:\\e_future.bin,100,100,2026/09/13 12:00:00,00000020,0,0\n"
        "C:\\f_none.bin,100,100,,00000020,0,0\n",
    )
    db_path = tmp_path / "index.db"
    ingest_csv(csv, db_path)
    buckets = bucket_map(open_index(db_path), NOW)

    def files(label: str) -> list[str]:
        conn = open_index(db_path)
        rows = conn.execute(
            "SELECT path, mtime FROM entries WHERE is_dir = 0 ORDER BY path"
        ).fetchall()
        return [str(path) for path, mtime in rows if expected_bucket(mtime, NOW) == label]

    assert files("<7d") == ["C:\\a_exact7d.bin", "C:\\e_future.bin"]  # boundary + future
    assert files("7-30d") == ["C:\\b_over7d.bin"]
    assert files("30-90d") == []
    assert files("90-365d") == ["C:\\c_exact365d.bin"]
    assert files(">1y") == ["C:\\d_over365d.bin"]
    assert files(UNKNOWN_AGE_BUCKET) == ["C:\\f_none.bin"]
    assert buckets["<7d"].files == 2
    assert buckets[UNKNOWN_AGE_BUCKET].files == 1
    assert buckets[UNKNOWN_AGE_BUCKET].oldest_mtime is None


def test_age_bucket_definition_is_validated() -> None:
    with pytest.raises(ValueError, match="at least one"):
        stats.age_buckets(sqlite3.connect(":memory:"), buckets=())
    with pytest.raises(ValueError, match="reserved"):
        stats.age_buckets(
            sqlite3.connect(":memory:"), buckets=((UNKNOWN_AGE_BUCKET, 1), ("open", None))
        )
    with pytest.raises(ValueError, match="open-ended"):
        stats.age_buckets(sqlite3.connect(":memory:"), buckets=(("a", None), ("b", 7)))
    with pytest.raises(ValueError, match="must increase"):
        stats.age_buckets(sqlite3.connect(":memory:"), buckets=(("a", 7), ("b", 7), ("c", None)))


# --------------------------------------------------------------------------- #
# Generated trees: ground-truth round trip
# --------------------------------------------------------------------------- #

GENERATED_OPTIONS = gen.GenOptions(
    seed=101,
    min_files=400,
    hardlink_ratio=0.12,
    unicode_ratio=0.2,
    comma_ratio=0.15,
    quote_ratio=0.05,
)


def test_generated_tree_matches_ground_truth(tmp_path: Path) -> None:
    generated = gen.generate(tmp_path / "export.csv", GENERATED_OPTIONS)
    truth = generated.truth
    db_path = tmp_path / "index.db"
    ingest_csv(generated.path, db_path)
    conn = open_index(db_path)
    report = stats.stats_report(conn, now=NOW, db_path=str(db_path))

    assert report.totals.file_bytes == truth.total_file_bytes
    assert report.totals.unique_file_bytes == sum(
        entry.size for entry in truth.entries if not entry.is_dir and not entry.hardlink
    )
    assert report.totals.allocated_bytes == truth.allocated_bytes
    assert report.totals.unique_allocated_bytes == truth.unique_allocated_bytes
    assert report.totals.hardlink_files == truth.hardlink_files > 0
    assert report.unattributed_files == 0

    expected_dirs = truth_dir_totals(truth)
    sizes = dir_size_map(conn)
    assert set(sizes) == set(expected_dirs)
    for path, (bytes_, unique, files) in expected_dirs.items():
        assert sizes[path].bytes == bytes_, path
        assert sizes[path].unique_bytes == unique, path
        assert sizes[path].file_count == files, path

    # ranking agrees with the derived oracle
    expected_top = sorted(expected_dirs.items(), key=lambda item: (-item[1][0], item[0]))[:10]
    assert [size.path for size in stats.top_dirs(conn, 10)] == [path for path, _ in expected_top]
    assert [size.bytes for size in stats.top_dirs(conn, 10)] == [
        values[0] for _, values in expected_top
    ]

    # the biggest files are the biggest file rows
    expected_files = sorted(
        (entry for entry in truth.entries if not entry.is_dir and not entry.hardlink),
        key=lambda entry: (-entry.size, entry.path),
    )[:10]
    assert [row.path for row in stats.top_files(conn, 10)] == [
        entry.path for entry in expected_files
    ]

    # extensions and age buckets agree with the oracle too
    assert {
        item.ext: (item.bytes, item.unique_bytes) for item in stats.extension_totals(conn)
    } == truth_extension_totals(truth)
    expected_buckets: dict[str, int] = {}
    for entry in truth.entries:
        if entry.is_dir:
            continue
        label = expected_bucket(entry.mtime, NOW)
        expected_buckets[label] = expected_buckets.get(label, 0) + 1
    assert {
        bucket.label: bucket.files for bucket in stats.age_buckets(conn, now=NOW) if bucket.files
    } == expected_buckets

    assert stats.folder_cross_check(conn).ok  # generated folder rows are consistent


def test_report_top_limit_bounds_the_ranked_lists(tmp_path: Path) -> None:
    generated = gen.generate(tmp_path / "export.csv", GENERATED_OPTIONS)
    db_path = tmp_path / "index.db"
    ingest_csv(generated.path, db_path)
    conn = open_index(db_path)

    small = stats.stats_report(conn, top=3, now=NOW)
    large = stats.stats_report(conn, top=50, now=NOW)
    assert len(small.dirs) == 3 == len(small.files) == len(small.extensions)
    assert small.apps == ()  # the generated tree has no app roots
    assert small.total_apps == 0
    assert [size.path for size in small.dirs] == [size.path for size in large.dirs[:3]]
    assert small.totals == large.totals
    assert large.dirs[:3] == small.dirs
    assert small.quality == large.quality
    assert small.distinct_extensions == large.distinct_extensions > 3


# --------------------------------------------------------------------------- #
# Derived tables (schema v2)
# --------------------------------------------------------------------------- #


def test_build_derived_matches_the_streaming_pass(tmp_path: Path) -> None:
    db_path = ingest_fixture(tmp_path, "app_roots.csv")
    conn = open_index(db_path)
    streamed = {size.path: size for size in stats.iter_dir_sizes(conn)}

    derived = stats.build_derived(conn)
    assert (derived.dir_sizes, derived.app_footprints) == (19, 2)
    assert db.meta_get(conn, "stats.built_entries") == "28"
    assert str(db.meta_get(conn, "stats.built_at", "")).endswith("+00:00")

    rows = conn.execute(
        "SELECT entry_id, path, depth, bytes, unique_bytes, allocated_bytes, own_bytes, "
        "file_count, dir_count, child_dir_count, export_bytes FROM dir_sizes"
    ).fetchall()
    assert len(rows) == len(streamed) == 19
    for (
        entry_id,
        path,
        depth,
        bytes_,
        unique,
        allocated,
        own,
        files,
        dirs,
        children,
        export,
    ) in rows:
        expected = streamed[str(path)]
        assert (int(entry_id), int(depth), int(bytes_), int(unique)) == (
            expected.entry_id,
            expected.depth,
            expected.bytes,
            expected.unique_bytes,
        )
        assert (int(allocated), int(own), int(files), int(dirs), int(children)) == (
            expected.allocated_bytes,
            expected.own_bytes,
            expected.file_count,
            expected.dir_count,
            expected.child_dir_count,
        )
        assert int(export) == expected.export_bytes

    table_apps = {
        str(row[0]): (json.loads(str(row[1])), int(row[2]))
        for row in conn.execute("SELECT app, roots, bytes FROM app_footprints")
    }
    live_apps: dict[str, AppFootprint] = {item.app: item for item in stats.app_footprints(conn)}
    assert set(table_apps) == set(live_apps)
    for name, (roots, bytes_) in table_apps.items():
        assert bytes_ == live_apps[name].bytes
        assert tuple(roots) == live_apps[name].roots

    again = stats.build_derived(conn)  # idempotent rebuild
    assert (again.dir_sizes, again.app_footprints) == (19, 2)
    assert conn.execute("SELECT COUNT(*) FROM dir_sizes").fetchone()[0] == 19


def test_derived_tables_follow_the_entries(tmp_path: Path) -> None:
    db_path = ingest_fixture(tmp_path, "app_roots.csv")
    conn = open_index(db_path)
    stats.build_derived(conn)
    assert conn.execute("SELECT COUNT(*) FROM dir_sizes").fetchone()[0] == 19

    # reloading the index (ingest --replace) must not leave stale derivations
    ingest_csv(DATA_DIR / "basic.csv", db_path, replace=True)
    assert conn.execute("SELECT COUNT(*) FROM dir_sizes").fetchone()[0] == 0
    assert conn.execute("SELECT COUNT(*) FROM app_footprints").fetchone()[0] == 0
    reloaded = stats.build_derived(conn)
    assert (reloaded.dir_sizes, reloaded.app_footprints) == (4, 1)


def test_schema_v1_indexes_migrate_to_v2(tmp_path: Path) -> None:
    db_path = tmp_path / "legacy.db"
    raw = sqlite3.connect(db_path)
    raw.executescript(db.MIGRATIONS[1])
    raw.execute("PRAGMA user_version=1")
    raw.commit()
    raw.close()

    conn = db.open_db(db_path)
    assert db.schema_version(conn) == db.SCHEMA_VERSION == 2
    tables = {
        str(row[0]) for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
    }
    assert {"dir_sizes", "app_footprints"} <= tables
    conn.close()

    ingest_csv(DATA_DIR / "app_roots.csv", db_path)
    conn = db.open_db(db_path)
    assert stats.build_derived(conn).dir_sizes == 19


# --------------------------------------------------------------------------- #
# Errors
# --------------------------------------------------------------------------- #


def test_report_rejects_an_empty_index(tmp_path: Path) -> None:
    conn = db.open_db(tmp_path / "empty.db")
    with pytest.raises(StatsError, match="empty"):
        stats.stats_report(conn)
    assert stats.totals(conn) == db.IndexSummary(0, 0, 0, 0, 0, 0, 0, 0)
    assert stats.top_dirs(conn) == ()
    assert stats.top_files(conn) == ()
    assert stats.extension_totals(conn) == ()
    assert stats.app_footprints(conn) == ()


def test_report_rejects_a_non_positive_top(tmp_path: Path) -> None:
    db_path = ingest_fixture(tmp_path, "basic.csv")
    with pytest.raises(StatsError, match="--top"):
        stats.stats_report(open_index(db_path), top=0)


def test_render_text_rejects_an_unknown_breakdown(tmp_path: Path) -> None:
    db_path = ingest_fixture(tmp_path, "basic.csv")
    report = stats.stats_report(open_index(db_path))
    with pytest.raises(StatsError, match="unknown breakdown"):
        stats.render_text(report, by="nope")


def test_format_bytes() -> None:
    assert stats.format_bytes(0) == "0 B"
    assert stats.format_bytes(999) == "999 B"
    assert stats.format_bytes(1024) == "1.0 KiB"
    assert stats.format_bytes(2_105_000) == "2.0 MiB"
    assert stats.format_bytes(-1_048_576) == "-1.0 MiB"
    assert stats.format_bytes(5 * 1024**4) == "5.0 TiB"


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #


def test_cli_stats_prints_every_section(tmp_path: Path) -> None:
    db_path = ingest_fixture(tmp_path, "app_roots.csv")
    result = run_cli("stats", "--db", str(db_path), "--top", "5")
    assert result.returncode == 0, result.stderr
    out = result.stdout
    assert f"index: {db_path} (schema v2)" in out
    assert "totals: 19 dirs, 9 files" in out
    assert "data quality: ok" in out
    assert "top directories by subtree (5 of 19)" in out
    assert "top files (5)" in out
    assert "extensions (5 of 6)" in out  # --top caps the extension list, count stays honest
    assert "age buckets (as of" in out
    assert "apps (2 of 2)" in out
    assert "Widget" in out and "C:\\Program Files\\Widget" in out


def test_cli_stats_by_selects_one_breakdown(tmp_path: Path) -> None:
    db_path = ingest_fixture(tmp_path, "app_roots.csv")

    extensions = run_cli("stats", "--db", str(db_path), "--by", "ext")
    assert extensions.returncode == 0, extensions.stderr
    assert "extensions (" in extensions.stdout
    assert "top directories" not in extensions.stdout
    assert "age buckets" not in extensions.stdout
    assert "apps (" not in extensions.stdout

    ages = run_cli("stats", "--db", str(db_path), "--by", "age")
    assert "age buckets (" in ages.stdout
    assert "extensions (" not in ages.stdout

    apps = run_cli("stats", "--db", str(db_path), "--by", "app")
    assert "apps (" in apps.stdout
    assert "top files" not in apps.stdout

    dirs = run_cli("stats", "--db", str(db_path), "--by", "dir")
    assert "top directories" in dirs.stdout and "top files" in dirs.stdout


def test_cli_stats_json_report(tmp_path: Path) -> None:
    db_path = ingest_fixture(tmp_path, "app_roots.csv")
    result = run_cli("stats", "--db", str(db_path), "--json", "--top", "3")
    assert result.returncode == 0, result.stderr
    payload = json.loads(result.stdout)

    assert payload["schema"] == "spacesage.stats/v1"
    assert payload["index"]["db"] == str(db_path)
    assert payload["index"]["schema_version"] == 2
    assert payload["totals"]["file_bytes"] == 3_972_000
    assert payload["totals"]["unique_file_bytes"] == 3_972_000

    dirs = payload["dirs"]
    assert (dirs["listed"], dirs["total_dirs"]) == (3, 19)
    assert dirs["items"][0]["path"] == "C:\\"
    assert dirs["items"][0]["bytes"] == 3_972_000
    assert dirs["items"][0]["export_bytes"] == 3_972_000
    assert dirs["items"][0]["delta_bytes"] == 0

    assert payload["files"]["listed"] == 3
    assert payload["files"]["items"][0]["path"].endswith("widget.exe")
    assert payload["extensions"]["listed"] == 3
    assert payload["extensions"]["distinct"] == 6
    assert [item["bucket"] for item in payload["age_buckets"]["items"]] == [
        "<7d",
        "7-30d",
        "30-90d",
        "90-365d",
        ">1y",
        "unknown",
    ]
    apps = payload["apps"]
    assert (apps["listed"], apps["total_apps"]) == (2, 2)
    widget = next(item for item in apps["items"] if item["app"] == "Widget")
    assert widget["bytes"] == 2_460_000
    assert widget["roots"] == [
        "C:\\Program Files\\Widget",
        "C:\\Users\\Alice\\AppData\\Local\\Widget",
        "C:\\Users\\Bob\\appdata\\local\\widget",
        "C:\\Users\\Alice\\AppData\\Roaming\\Widget",
    ]
    assert payload["data_quality"]["warnings"] == []


def test_cli_stats_warns_about_injected_disagreement(tmp_path: Path) -> None:
    db_path = tmp_path / "index.db"
    ingest_csv(broken_fixture(tmp_path, "2048576"), db_path)
    result = run_cli("stats", "--db", str(db_path), "--by", "dir")
    assert result.returncode == 0, result.stderr
    assert "1 folder disagree" in result.stdout
    assert "warning: C:\\Windows\\System32" in result.stdout

    as_json = run_cli("stats", "--db", str(db_path), "--json")
    warnings = json.loads(as_json.stdout)["data_quality"]["warnings"]
    assert len(warnings) == 1 and warnings[0]["delta_bytes"] == 1_048_576


def test_cli_stats_materialize_writes_derived_tables(tmp_path: Path) -> None:
    db_path = ingest_fixture(tmp_path, "app_roots.csv")
    result = run_cli("stats", "--db", str(db_path), "--materialize", "--by", "ext")
    assert result.returncode == 0, result.stderr
    assert "derived: 19 dir_sizes rows, 2 app_footprints rows" in result.stderr

    conn = open_index(db_path)
    assert conn.execute("SELECT COUNT(*) FROM dir_sizes").fetchone()[0] == 19
    assert conn.execute("SELECT COUNT(*) FROM app_footprints").fetchone()[0] == 2


def test_cli_stats_error_paths(tmp_path: Path) -> None:
    missing = run_cli("stats", "--db", str(tmp_path / "missing.db"))
    assert missing.returncode == 1
    assert "no index at" in missing.stderr

    empty = db.open_db(tmp_path / "empty.db")
    empty.close()
    result = run_cli("stats", "--db", str(tmp_path / "empty.db"))
    assert result.returncode == 1
    assert "index is empty" in result.stderr

    db_path = ingest_fixture(tmp_path, "basic.csv")
    bad_top = run_cli("stats", "--db", str(db_path), "--top", "0")
    assert bad_top.returncode == 1
    assert "--top must be >= 1" in bad_top.stderr

    bad_by = run_cli("stats", "--db", str(db_path), "--by", "size")
    assert bad_by.returncode == 2  # argparse rejects it
    assert "invalid choice" in bad_by.stderr
