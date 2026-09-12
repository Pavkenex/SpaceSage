"""Streaming WizTree CSV export -> SQLite index.

Format contract (``docs/design.md`` section 5):

* Columns ``File Name, Size, Allocated, Modified, Attributes, Files, Folders``.
  The header is parsed **by column name**, case-insensitively, in any order;
  extra, renamed or missing optional columns are tolerated.
* ``File Name`` holds the full path.  Directory rows end with a trailing
  separator; their ``Size``/``Allocated`` values are **descendant totals** and
  are indexed verbatim -- they are never summed together with file rows.
* ``Modified`` is ``yyyy/MM/dd HH:mm:ss`` (zone-less local time), stored as an
  epoch with the documented assumption that the value is UTC so ordering and
  age arithmetic are deterministic.
* A leading zero on a non-zero ``Allocated`` text value marks a hard-linked
  file: those bytes are already accounted for by another entry
  (``hardlink_flag = 1``); the value itself is kept and roll-ups count those
  rows once.
* Capacity/summary rows are optional (see :func:`_looks_like_capacity_row`).
  When present they are recorded in ``drives`` if they carry free-space data,
  otherwise counted and skipped.

The reader streams: the file is never loaded into memory, rows are inserted in
batches of :data:`BATCH_SIZE` inside a **single** transaction, and only the
open ancestor chain (for parent reconstruction) plus the current batch live in
memory.  Ingest is deliberately fail-loud: anything that cannot be reconciled
between the parsed counters and the index raises :class:`IngestError`.
"""

from __future__ import annotations

import codecs
import csv
import io
import os
import platform
import re
import sqlite3
import time
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from spacesage import db

BATCH_SIZE = 50_000
"""Rows per ``executemany`` batch (the load itself is one transaction)."""

SAMPLE_BYTES = 4096
"""Bytes read for encoding sniffing before the streaming pass."""

_MTIME_FORMATS = (
    "%Y/%m/%d %H:%M:%S",
    "%Y/%m/%d %H:%M",
    "%Y/%m/%d",
    "%Y-%m-%d %H:%M:%S",
    "%Y-%m-%d %H:%M",
    "%Y-%m-%d",
)

_DRIVE_SPEC_RE = re.compile(r"^[A-Za-z]:$")

#: Header aliases, keyed by normalised header cell -> canonical field.
_COLUMN_ALIASES: dict[str, str] = {
    "file name": "path",
    "filename": "path",
    "file": "path",
    "name": "path",
    "path": "path",
    "full path": "path",
    "size": "size",
    "size bytes": "size",
    "size (bytes)": "size",
    "logical size": "size",
    "allocated": "allocated",
    "allocated bytes": "allocated",
    "allocated size": "allocated",
    "allocated (bytes)": "allocated",
    "physical size": "allocated",
    "modified": "modified",
    "modified date": "modified",
    "modified time": "modified",
    "date modified": "modified",
    "last modified": "modified",
    "attributes": "attributes",
    "attribute": "attributes",
    "attr": "attributes",
    "attrs": "attributes",
    "files": "files",
    "file count": "files",
    "folders": "folders",
    "folder count": "folders",
    "free": "free",
    "free bytes": "free",
    "free space": "free",
    "free space (bytes)": "free",
    "capacity": "capacity",
    "capacity bytes": "capacity",
    "total": "capacity",
    "total size": "capacity",
}


class IngestError(RuntimeError):
    """Raised when an export cannot be ingested as specified."""


@dataclass(frozen=True)
class IngestProgress:
    """Progress snapshot handed to the optional callback (once per batch)."""

    rows_read: int
    bytes_read: int
    file_size: int
    elapsed_s: float

    @property
    def rows_per_sec(self) -> float:
        """Rows parsed per second so far."""
        return self.rows_read / self.elapsed_s if self.elapsed_s > 0 else 0.0


@dataclass(frozen=True)
class RunStats:
    """Outcome of one ingest run.

    Row counts and byte totals are read back from the index itself (see
    :func:`spacesage.db.index_summary`), never from the parse counters --
    the counters only feed the ``*_rows`` skip/diagnostic fields.
    """

    source: str
    db_path: str
    encoding: str
    header: tuple[str, ...]
    rows: int
    rows_read: int
    files: int
    dirs: int
    total_file_bytes: int
    allocated_bytes: int | None
    unique_allocated_bytes: int | None
    hardlink_files: int
    duplicate_rows: int
    blank_rows: int
    short_rows: int
    long_rows: int
    empty_name_rows: int
    capacity_rows: int
    drives_recorded: int
    orphan_rows: int
    bad_number_rows: int
    bad_mtime_rows: int
    duration_s: float

    @property
    def rows_per_sec(self) -> float:
        """Index rows per second for this run."""
        return self.rows / self.duration_s if self.duration_s > 0 else 0.0

    @property
    def has_allocated(self) -> bool:
        """Whether the export carried an ``Allocated`` column."""
        return self.allocated_bytes is not None


@dataclass
class _Counters:
    """Mutable parse counters (diagnostics; never the source of row totals)."""

    rows: int = 0
    files: int = 0
    dirs: int = 0
    duplicate_rows: int = 0
    blank_rows: int = 0
    short_rows: int = 0
    long_rows: int = 0
    empty_name_rows: int = 0
    capacity_rows: int = 0
    drives_recorded: int = 0
    orphan_rows: int = 0
    bad_number_rows: int = 0
    bad_mtime_rows: int = 0
    hardlink_files: int = 0
    total_file_bytes: int = 0
    allocated_bytes: int = 0
    unique_allocated_bytes: int = 0


@dataclass(frozen=True)
class _Columns:
    """Resolved header positions (``None`` when the column is absent)."""

    width: int
    path: int
    size: int
    allocated: int | None
    modified: int | None
    attributes: int | None
    files: int | None
    folders: int | None
    free: int | None
    capacity: int | None


@dataclass(frozen=True)
class _Header:
    columns: _Columns
    raw: tuple[str, ...]
    meta_columns_present: bool


# --------------------------------------------------------------------------- #
# Parsing helpers
# --------------------------------------------------------------------------- #


def detect_encoding(sample: bytes) -> str:
    """Return the text encoding to open an export with.

    Handles ``UTF-8`` (with or without BOM) and ``UTF-16`` (BOM'd, plus a
    BOM-less heuristic: a high proportion of NUL bytes selects the endianness
    by which byte positions hold them).
    """
    if sample.startswith(codecs.BOM_UTF8):
        return "utf-8-sig"
    if sample.startswith((codecs.BOM_UTF16_LE, codecs.BOM_UTF16_BE)):
        return "utf-16"
    if sample:
        nuls = sample.count(0)
        if nuls * 5 >= len(sample):  # >= 20% NUL bytes: UTF-16 without BOM
            odd = sum(1 for index in range(1, len(sample), 2) if sample[index] == 0)
            even = nuls - odd
            return "utf-16-le" if odd >= even else "utf-16-be"
    return "utf-8-sig"


def _normalise_header_cell(raw: str) -> str:
    text = raw.replace("\ufeff", "").strip().strip('"').lower()
    return " ".join(text.split()).rstrip(":")


def _parse_header(header: list[str]) -> _Header:
    """Map header cells to canonical fields (first occurrence wins)."""
    seen: dict[str, int] = {}
    for index, raw in enumerate(header):
        field_name = _COLUMN_ALIASES.get(_normalise_header_cell(raw))
        if field_name is not None:
            seen.setdefault(field_name, index)
    if "path" not in seen:
        raise IngestError(
            "cannot find the file-name column: expected a header cell such as "
            f"'File Name' or 'Path', got {header!r}"
        )
    if "size" not in seen:
        raise IngestError(f"cannot find the 'Size' column in header {header!r}")

    columns = _Columns(
        width=len(header),
        path=seen["path"],
        size=seen["size"],
        allocated=seen.get("allocated"),
        modified=seen.get("modified"),
        attributes=seen.get("attributes"),
        files=seen.get("files"),
        folders=seen.get("folders"),
        free=seen.get("free"),
        capacity=seen.get("capacity"),
    )
    meta_names = ("modified", "attributes", "files", "folders")
    meta_columns_present = all(getattr(columns, name) is not None for name in meta_names)
    return _Header(columns=columns, raw=tuple(header), meta_columns_present=meta_columns_present)


def _parse_int(raw: str | None) -> int | None:
    """Parse a byte-count cell; tolerates spaces, thousands separators, floats."""
    if raw is None:
        return None
    text = raw.strip().replace(",", "").replace(" ", "").replace("_", "")
    if not text:
        return None
    try:
        return int(text, 10)
    except ValueError:
        try:
            return int(float(text))
        except ValueError:
            return None


def _parse_mtime(raw: str | None) -> int | None:
    """Parse ``yyyy/MM/dd HH:mm:ss`` into epoch seconds (documented as UTC)."""
    if raw is None:
        return None
    text = raw.strip()
    if not text:
        return None
    for fmt in _MTIME_FORMATS:
        try:
            stamp = datetime.strptime(text, fmt)
        except ValueError:
            continue
        return int(stamp.replace(tzinfo=UTC).timestamp())
    return None


def _parse_allocated(raw: str | None, *, is_dir: bool) -> tuple[int | None, bool]:
    """Return ``(allocated_bytes, hardlink_flag)`` for an ``Allocated`` cell."""
    if raw is None:
        return None, False
    text = raw.strip()
    if not text:
        return None, False
    value = _parse_int(text)
    if value is None:
        return None, False
    digits = text.replace(",", "").replace(" ", "")
    marked = (
        not is_dir and len(digits) > 1 and digits.startswith("0") and value > 0 and digits.isdigit()
    )
    return value, marked


def _is_root_path(path: str) -> bool:
    """True for drive roots (``C:\\``, ``C:``) and POSIX roots."""
    if path in ("", "/", "\\"):
        return True
    if len(path) == 3 and path[1] == ":" and path[2] in "\\/":
        return True
    return len(path) == 2 and path[1] == ":"


def normalise_path(path: str, *, is_dir: bool) -> str:
    """Normalise a full path for storage.

    Folder paths lose their trailing separator (the export's folder marker is
    preserved by ``is_dir``); drive roots keep theirs (``C:\\``), because a
    bare ``C:`` means "current directory on C:" on Windows.
    """
    text = path.strip()
    if not is_dir:
        return text
    stripped = text.rstrip("\\/")
    if _is_root_path(stripped) or not stripped:
        return text if text else "/"
    return stripped


def parent_path(path: str, *, is_dir: bool) -> str | None:
    """Return the stored path of the parent folder (``None`` for a root row)."""
    text = normalise_path(path, is_dir=is_dir)
    if _is_root_path(text):
        return None
    index = max(text.rfind("\\"), text.rfind("/"))
    if index < 0:
        return None
    return normalise_path(text[: index + 1], is_dir=True)


def extract_name(path: str, *, is_dir: bool) -> str:
    """Return the last path component (drive roots keep their ``C:`` form)."""
    text = normalise_path(path, is_dir=is_dir)
    if not text:
        return "/"
    if _is_root_path(text):
        return text.rstrip("\\/")
    index = max(text.rfind("\\"), text.rfind("/"))
    return text[index + 1 :]


def extract_ext(path: str) -> str:
    """Return the lower-cased file extension without the dot (``''`` if none)."""
    name = extract_name(path, is_dir=False)
    suffix = os.path.splitext(name)[1]
    return suffix[1:].lower() if suffix not in ("", ".") else ""


def path_depth(path: str, *, is_dir: bool) -> int:
    """Number of components below the exported root (a root row is 0)."""
    text = normalise_path(path, is_dir=is_dir)
    if _is_root_path(text):
        return 0
    return max(text.count("\\") + text.count("/"), 0)


def _looks_like_capacity_row(name: str, *, is_dir: bool, meta_cells: tuple[str, ...]) -> bool:
    """True for drive summary rows that are not tree entries.

    Exports name drives ``C:\\``; a row named exactly ``C:`` is never a tree
    entry and is treated as a capacity/summary row.  A root row (``C:\\``)
    whose Modified/Attributes/Files/Folders cells are *all* empty -- only
    checkable when the export provides those columns -- is treated the same.
    """
    if not is_dir and _DRIVE_SPEC_RE.match(name):
        return True
    return bool(is_dir and meta_cells and not any(cell.strip() for cell in meta_cells))


def _cell(row: list[str], index: int | None) -> str:
    if index is None or index >= len(row):
        return ""
    return row[index]


# --------------------------------------------------------------------------- #
# Loader
# --------------------------------------------------------------------------- #


class _Loader:
    """Single-pass loader: parent reconstruction, batching, counters."""

    def __init__(
        self,
        conn: sqlite3.Connection,
        header: _Header,
        counters: _Counters,
        *,
        batch_size: int,
        progress: Callable[[IngestProgress], None] | None,
        position: Callable[[], int],
        file_size: int,
    ) -> None:
        self._conn: sqlite3.Connection = conn
        self._columns: _Columns = header.columns
        self._meta_columns_present = header.meta_columns_present
        self._counters = counters
        self._batch_size = batch_size
        self._progress = progress
        self._position = position
        self._file_size = file_size
        self._started = time.perf_counter()
        self._stack: list[tuple[str, int]] = []
        self._batch: list[db.EntryRow] = []
        self._pending_dirs: dict[str, int] = {}
        self._next_id = 1
        self._inserted = 0

    # -- row handling ------------------------------------------------------ #

    def handle(self, row: list[str]) -> None:
        """Consume one CSV record."""
        counters = self._counters
        if not row or not any(cell.strip() for cell in row):
            counters.blank_rows += 1
            return
        if len(row) < self._columns.width:
            counters.short_rows += 1
        elif len(row) > self._columns.width:
            counters.long_rows += 1

        raw_name = _cell(row, self._columns.path).strip()
        if not raw_name:
            counters.empty_name_rows += 1
            return

        is_dir = raw_name.endswith(("\\", "/"))
        meta_cells = (
            (
                _cell(row, self._columns.modified),
                _cell(row, self._columns.attributes),
                _cell(row, self._columns.files),
                _cell(row, self._columns.folders),
            )
            if self._meta_columns_present
            else ()
        )
        if _looks_like_capacity_row(raw_name, is_dir=is_dir, meta_cells=meta_cells):
            counters.capacity_rows += 1
            if self._record_drive(raw_name, row):
                counters.drives_recorded += 1
            return

        parent_id = self._resolve_parent(parent_path(raw_name, is_dir=is_dir))
        path = normalise_path(raw_name, is_dir=is_dir)

        if is_dir:
            existing_id = self._existing_dir_id(path)
            if existing_id is not None:
                # A folder row repeated later in the export: keep children
                # pointing at the first occurrence instead of a skipped row.
                counters.rows += 1
                counters.duplicate_rows += 1
                self._stack.append((path, existing_id))
                return

        size = _parse_int(_cell(row, self._columns.size))
        if size is None:
            size = 0
            counters.bad_number_rows += 1

        allocated: int | None = None
        hardlink = False
        if self._columns.allocated is not None:
            allocated_cell = _cell(row, self._columns.allocated)
            allocated, hardlink = _parse_allocated(allocated_cell, is_dir=is_dir)
            if allocated is None and allocated_cell.strip():
                counters.bad_number_rows += 1

        mtime: int | None = None
        if self._columns.modified is not None:
            raw_mtime = _cell(row, self._columns.modified)
            mtime = _parse_mtime(raw_mtime)
            if mtime is None and raw_mtime.strip():
                counters.bad_mtime_rows += 1

        attrs = _cell(row, self._columns.attributes).strip() or None

        entry = db.EntryRow(
            id=self._next_id,
            path=path,
            name=extract_name(raw_name, is_dir=is_dir),
            parent_id=parent_id,
            is_dir=1 if is_dir else 0,
            size=size,
            allocated=allocated,
            mtime=mtime,
            attrs=attrs,
            hardlink_flag=1 if hardlink else 0,
            depth=path_depth(raw_name, is_dir=is_dir),
            ext=None if is_dir else extract_ext(raw_name),
        )
        self._next_id += 1
        self._batch.append(entry)

        counters.rows += 1
        if is_dir:
            counters.dirs += 1
            self._stack.append((path, entry.id))
            self._pending_dirs[path] = entry.id
        else:
            counters.files += 1
            counters.total_file_bytes += size
            if allocated is not None:
                counters.allocated_bytes += allocated
                if hardlink:
                    counters.hardlink_files += 1
                else:
                    counters.unique_allocated_bytes += allocated

        if len(self._batch) >= self._batch_size:
            self.flush()

    def flush(self) -> None:
        """Insert the pending batch and emit a progress snapshot."""
        if not self._batch:
            return
        inserted = db.insert_entries(self._conn, self._batch)
        self._counters.duplicate_rows += len(self._batch) - inserted
        self._inserted += inserted
        self._batch.clear()
        self._pending_dirs.clear()
        self._emit_progress()

    @property
    def inserted(self) -> int:
        """Rows inserted so far."""
        return self._inserted

    # -- internals --------------------------------------------------------- #

    def _resolve_parent(self, parent: str | None) -> int | None:
        counters = self._counters
        if parent is None:
            return None
        # Rows are exported depth-first: every still-open ancestor is on the
        # stack, and anything deeper than the new parent is closed for good.
        while self._stack and self._stack[-1][0] != parent:
            self._stack.pop()
        if self._stack:
            return self._stack[-1][1]
        # Out-of-order export: the parent may already be in the index.
        row = self._conn.execute("SELECT id FROM entries WHERE path = ?", (parent,)).fetchone()
        if row is None:
            counters.orphan_rows += 1
            return None
        return int(row[0])

    def _existing_dir_id(self, path: str) -> int | None:
        """Id of an already-known folder row (pending batch or committed index)."""
        pending = self._pending_dirs.get(path)
        if pending is not None:
            return pending
        row = self._conn.execute("SELECT id FROM entries WHERE path = ?", (path,)).fetchone()
        return int(row[0]) if row is not None else None

    def _record_drive(self, name: str, row: list[str]) -> bool:
        columns = self._columns
        free = _parse_int(_cell(row, columns.free)) if columns.free is not None else None
        capacity = (
            _parse_int(_cell(row, columns.capacity)) if columns.capacity is not None else None
        )
        if capacity is None and free is not None:
            # A free-space column plus the row's Size: the pair is capacity/free.
            capacity = _parse_int(_cell(row, columns.size))
        if capacity is None and free is None:
            return False
        drive = name.rstrip("\\/")
        self._conn.execute(
            "INSERT INTO drives(name, fs_type, capacity_bytes, free_bytes) VALUES (?, NULL, ?, ?) "
            "ON CONFLICT(name) DO UPDATE SET "
            "capacity_bytes = excluded.capacity_bytes, free_bytes = excluded.free_bytes",
            (drive, capacity, free),
        )
        return True

    def _emit_progress(self) -> None:
        if self._progress is None:
            return
        elapsed = time.perf_counter() - self._started
        self._progress(
            IngestProgress(
                rows_read=self._counters.rows,
                bytes_read=self._position(),
                file_size=self._file_size,
                elapsed_s=elapsed,
            )
        )


# --------------------------------------------------------------------------- #
# Entry point
# --------------------------------------------------------------------------- #


def _read_header(reader: Iterator[list[str]]) -> list[str]:
    for row in reader:
        if any(cell.strip() for cell in row):
            return row
    raise IngestError("the export is empty: no header row found")


def _verify_consistency(
    counters: _Counters, summary: db.IndexSummary, *, has_allocated: bool
) -> None:
    """Fail loud if parsed counters and the index disagree."""
    expected_rows = counters.rows - counters.duplicate_rows
    if summary.entries != expected_rows:
        raise IngestError(
            f"index/parse mismatch: {summary.entries} rows in the index, "
            f"{expected_rows} expected ({counters.rows} parsed, "
            f"{counters.duplicate_rows} duplicates)"
        )
    if counters.duplicate_rows:
        return  # duplicate rows also shift the per-type counters; counts suffice
    mismatches: list[str] = []
    if summary.files != counters.files:
        mismatches.append(f"files {summary.files} != {counters.files}")
    if summary.dirs != counters.dirs:
        mismatches.append(f"dirs {summary.dirs} != {counters.dirs}")
    if summary.file_bytes != counters.total_file_bytes:
        mismatches.append(f"file bytes {summary.file_bytes} != {counters.total_file_bytes}")
    if summary.hardlink_files != counters.hardlink_files:
        mismatches.append(f"hardlinks {summary.hardlink_files} != {counters.hardlink_files}")
    if has_allocated and summary.allocated_bytes != counters.allocated_bytes:
        mismatches.append(f"allocated {summary.allocated_bytes} != {counters.allocated_bytes}")
    if has_allocated and summary.unique_allocated_bytes != counters.unique_allocated_bytes:
        mismatches.append(
            f"unique allocated {summary.unique_allocated_bytes} != "
            f"{counters.unique_allocated_bytes}"
        )
    if mismatches:
        raise IngestError("index/parse mismatch: " + "; ".join(mismatches))


def ingest_csv(
    source: str | Path,
    db_path: str | Path,
    *,
    batch_size: int = BATCH_SIZE,
    replace: bool = False,
    progress: Callable[[IngestProgress], None] | None = None,
) -> RunStats:
    """Stream ``source`` into the index at ``db_path`` and return run stats.

    ``db_path`` may be a database file or an existing directory.  A non-empty
    index is refused unless ``replace=True``, in which case it is cleared
    first (the load itself is still atomic: it commits once, at the end).
    """
    if batch_size < 1:
        raise IngestError(f"batch_size must be >= 1, got {batch_size}")
    src = Path(source).expanduser()
    if not src.is_file():
        raise IngestError(f"CSV export not found: {src}")
    target = db.resolve_db_path(db_path)
    file_size = src.stat().st_size
    started = time.perf_counter()

    conn = db.open_db(target)
    counters = _Counters()
    try:
        if db.entry_count(conn) and not replace:
            raise IngestError(
                f"the index at {target} already contains rows; pass --replace to reload it"
            )
        if replace:
            db.clear_index(conn)

        with src.open("rb") as raw:
            encoding = detect_encoding(raw.read(SAMPLE_BYTES))
            raw.seek(0)
            with io.TextIOWrapper(raw, encoding=encoding, newline="") as text:
                reader = csv.reader(text)
                header = _parse_header(_read_header(reader))
                loader = _Loader(
                    conn,
                    header,
                    counters,
                    batch_size=batch_size,
                    progress=progress,
                    position=raw.tell,
                    file_size=file_size,
                )
                conn.execute("BEGIN IMMEDIATE")
                try:
                    conn.execute("PRAGMA defer_foreign_keys=ON")
                    for row in reader:
                        loader.handle(row)
                    loader.flush()
                except BaseException:
                    conn.rollback()
                    raise
                try:
                    conn.commit()
                except sqlite3.IntegrityError as exc:  # pragma: no cover - defensive
                    conn.rollback()
                    raise IngestError(f"index integrity error while committing: {exc}") from exc

        summary = db.index_summary(conn)
        _verify_consistency(counters, summary, has_allocated=header.columns.allocated is not None)

        duration = time.perf_counter() - started
        stats = RunStats(
            source=str(src.resolve()),
            db_path=str(target),
            encoding=encoding,
            header=header.raw,
            rows=summary.entries,
            rows_read=counters.rows,
            files=summary.files,
            dirs=summary.dirs,
            total_file_bytes=summary.file_bytes,
            allocated_bytes=(
                summary.allocated_bytes if header.columns.allocated is not None else None
            ),
            unique_allocated_bytes=(
                summary.unique_allocated_bytes if header.columns.allocated is not None else None
            ),
            hardlink_files=summary.hardlink_files,
            duplicate_rows=counters.duplicate_rows,
            blank_rows=counters.blank_rows,
            short_rows=counters.short_rows,
            long_rows=counters.long_rows,
            empty_name_rows=counters.empty_name_rows,
            capacity_rows=counters.capacity_rows,
            drives_recorded=counters.drives_recorded,
            orphan_rows=counters.orphan_rows,
            bad_number_rows=counters.bad_number_rows,
            bad_mtime_rows=counters.bad_mtime_rows,
            duration_s=duration,
        )
        db.meta_set_many(conn, _meta_for(stats))
        conn.execute("PRAGMA optimize")
        return stats
    finally:
        conn.close()


def _meta_for(stats: RunStats) -> dict[str, object]:
    """Provenance + run statistics recorded in ``meta`` for later slices."""
    source = Path(stats.source)
    exported = datetime.fromtimestamp(source.stat().st_mtime, tz=UTC)
    return {
        "schema.version": db.SCHEMA_VERSION,
        "source.csv": stats.source,
        "source.csv_bytes": source.stat().st_size,
        "source.exported": exported.isoformat(timespec="seconds"),
        "source.machine": platform.node(),
        "ingest.encoding": stats.encoding,
        "ingest.finished_at": datetime.now(tz=UTC).isoformat(timespec="seconds"),
        "ingest.rows": stats.rows,
        "ingest.files": stats.files,
        "ingest.dirs": stats.dirs,
        "ingest.total_file_bytes": stats.total_file_bytes,
        "ingest.allocated_bytes": stats.allocated_bytes if stats.has_allocated else "",
        "ingest.unique_allocated_bytes": (
            stats.unique_allocated_bytes if stats.has_allocated else ""
        ),
        "ingest.hardlink_files": stats.hardlink_files,
        "ingest.capacity_rows": stats.capacity_rows,
        "ingest.orphan_rows": stats.orphan_rows,
        "ingest.duplicate_rows": stats.duplicate_rows,
        "ingest.duration_s": f"{stats.duration_s:.3f}",
        "ingest.rows_per_sec": f"{stats.rows_per_sec:.0f}",
    }


__all__ = [
    "BATCH_SIZE",
    "IngestError",
    "IngestProgress",
    "RunStats",
    "detect_encoding",
    "extract_ext",
    "extract_name",
    "ingest_csv",
    "normalise_path",
    "parent_path",
    "path_depth",
]
