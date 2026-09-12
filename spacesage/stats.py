"""Aggregates over the index: directory, extension, age and per-app views.

The ``stats`` stage of the pipeline (``docs/design.md`` section 3) answers
*where do the bytes live?* -- directory subtrees, biggest files, per-extension
totals, age buckets and per-application footprints.

Everything here is computed from **file rows** of the index:

* Folder rows carry the export's verbatim descendant total in ``size``.  Those
  values are **never summed** (that would double-count); they are only *read*
  for the cross-check, which reports folders whose exported total disagrees
  with the file-row sum beyond a tolerance (:class:`CrossCheckWarning`).
* ``hardlink_flag = 1`` marks a copy whose payload another entry already
  accounts for.  ``unique_*`` values count such rows once (they are credited
  to the unflagged source), mirroring :func:`spacesage.db.index_summary`; the
  raw values count every copy.
* ``mtime`` is epoch seconds (see :mod:`spacesage.ingest`); files without one
  fall into the :data:`UNKNOWN_AGE_BUCKET` bucket.

:func:`stats_report` builds every view in a single streaming pass over the
index -- memory is one small accumulator per folder, not one object per file
-- and :func:`iter_dir_sizes` is the children-before-parents iterator behind
it.  :func:`build_derived` materialises those same numbers into the
``dir_sizes`` / ``app_footprints`` tables (schema v2) so later stages can
query them with plain SQL.

The **per-app heuristic** (``docs/design.md`` section 5) groups the direct
children of the well-known environment folders :data:`APP_ROOT_COMPONENTS`
(``%LOCALAPPDATA%``, ``%APPDATA%``, ``%PROGRAMFILES%``,
``%PROGRAMFILES(X86)%``) into applications: ``C:\\Users\\a\\AppData\\Local\\
Chrome`` and ``D:\\Program Files\\Chrome`` are one ``Chrome`` footprint.
Matching is component-wise and case-insensitive, and spellings are merged
across roots and users, so a capitalisation variant never splits a footprint.
"""

from __future__ import annotations

import heapq
import json
import re
import sqlite3
from collections.abc import Iterable, Iterator, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime

from spacesage import db

DEFAULT_TOP = 20
"""Rows per ranked list in :func:`stats_report`."""

DEFAULT_TOLERANCE_BYTES = 4096
"""Absolute slack allowed between a folder row and its file-row sum."""

DEFAULT_TOLERANCE_RATIO = 0.001
"""Relative slack (fraction of the exported total) for the cross-check."""

DEFAULT_AGE_BUCKETS: tuple[tuple[str, int | None], ...] = (
    ("<7d", 7),
    ("7-30d", 30),
    ("30-90d", 90),
    ("90-365d", 365),
    (">1y", None),
)
"""Age buckets as ``(label, max_age_days)``; the last entry is open-ended."""

UNKNOWN_AGE_BUCKET = "unknown"
"""Bucket label for files without a usable ``Modified`` timestamp."""

APP_ROOT_COMPONENTS: tuple[tuple[str, ...], ...] = (
    ("appdata", "local"),
    ("appdata", "roaming"),
    ("program files (x86)",),
    ("program files",),
)
"""Path tails whose direct children are treated as applications.

Longest spec first, compared component-wise and case-insensitively, so
``%LOCALAPPDATA%`` / ``%APPDATA%`` / ``%PROGRAMFILES%`` /
``%PROGRAMFILES(X86)%`` match whatever their capitalisation.
"""

_INSERT_BATCH = 10_000
_DAY_SECONDS = 86_400
_SEPARATOR_RE = re.compile(r"[\\/]+")


class StatsError(RuntimeError):
    """Raised when a stats query cannot be answered for the given index."""


# --------------------------------------------------------------------------- #
# Report data
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class DirSize:
    """One folder with its subtree aggregates, computed from file rows only."""

    entry_id: int
    parent_id: int | None
    path: str
    depth: int
    bytes: int
    unique_bytes: int
    allocated_bytes: int
    unique_allocated_bytes: int
    own_bytes: int
    file_count: int
    unique_file_count: int
    dir_count: int
    child_dir_count: int
    export_bytes: int
    export_allocated: int | None

    @property
    def delta_bytes(self) -> int:
        """Exported folder total minus the file-row sum (the cross-check delta)."""
        return self.export_bytes - self.bytes


@dataclass(frozen=True)
class FileRow:
    """One file row, as reported by :func:`top_files`."""

    entry_id: int
    path: str
    size: int
    allocated: int | None
    mtime: int | None
    ext: str | None
    hardlink: bool
    depth: int


@dataclass(frozen=True)
class ExtensionTotal:
    """Aggregates for one file extension (``''`` when there is none)."""

    ext: str
    files: int
    unique_files: int
    bytes: int
    unique_bytes: int
    allocated_bytes: int


@dataclass(frozen=True)
class AgeBucket:
    """One age bucket (by ``mtime``) with its file/size totals."""

    label: str
    files: int
    unique_files: int
    bytes: int
    unique_bytes: int
    allocated_bytes: int
    oldest_mtime: int | None
    newest_mtime: int | None


@dataclass(frozen=True)
class AppFootprint:
    """One application: everything under every ``<app root>\\<name>`` folder."""

    app: str
    bytes: int
    unique_bytes: int
    allocated_bytes: int
    unique_allocated_bytes: int
    file_count: int
    unique_file_count: int
    dir_count: int
    roots: tuple[str, ...]


@dataclass(frozen=True)
class CrossCheckWarning:
    """A folder whose exported total disagrees with its file-row sum."""

    path: str
    entry_id: int
    depth: int
    export_bytes: int
    computed_bytes: int
    delta_bytes: int
    tolerance_bytes: int


@dataclass(frozen=True)
class CrossCheck:
    """Outcome of comparing folder rows against the file-row sums."""

    checked_dirs: int
    tolerance_bytes: int
    tolerance_ratio: float
    warnings: tuple[CrossCheckWarning, ...]

    @property
    def ok(self) -> bool:
        """True when every folder row agreed within the tolerance."""
        return not self.warnings


@dataclass(frozen=True)
class DerivedStats:
    """Result of :func:`build_derived` (materialised table row counts)."""

    dir_sizes: int
    app_footprints: int
    built_at: datetime


@dataclass(frozen=True)
class StatsReport:
    """Every view the ``stats`` CLI prints, in one object."""

    generated: datetime
    db_path: str | None
    schema_version: int
    source_csv: str | None
    source_machine: str | None
    source_exported: str | None
    totals: db.IndexSummary
    allocation_available: bool
    unattributed_files: int
    unattributed_bytes: int
    top: int
    dirs: tuple[DirSize, ...]
    files: tuple[FileRow, ...]
    extensions: tuple[ExtensionTotal, ...]
    distinct_extensions: int
    age_as_of: int
    age: tuple[AgeBucket, ...]
    apps: tuple[AppFootprint, ...]
    total_apps: int
    app_roots: tuple[str, ...]
    quality: CrossCheck

    def to_dict(self) -> dict[str, object]:
        """JSON-ready mapping (``spacesage.stats/v1``)."""
        return {
            "schema": "spacesage.stats/v1",
            "generated": _iso(self.generated),
            "index": {
                "db": self.db_path,
                "schema_version": self.schema_version,
                "source_csv": self.source_csv,
                "source_machine": self.source_machine,
                "source_exported": self.source_exported,
            },
            "totals": {
                "entries": self.totals.entries,
                "files": self.totals.files,
                "dirs": self.totals.dirs,
                "file_bytes": self.totals.file_bytes,
                "unique_file_bytes": self.totals.unique_file_bytes,
                "allocated_bytes": self.totals.allocated_bytes,
                "unique_allocated_bytes": self.totals.unique_allocated_bytes,
                "hardlink_files": self.totals.hardlink_files,
                "allocation_available": self.allocation_available,
                "unattributed_files": self.unattributed_files,
                "unattributed_bytes": self.unattributed_bytes,
            },
            "data_quality": {
                "checked_dirs": self.quality.checked_dirs,
                "tolerance_bytes": self.quality.tolerance_bytes,
                "tolerance_ratio": self.quality.tolerance_ratio,
                "warnings": [
                    {
                        "path": warning.path,
                        "depth": warning.depth,
                        "export_bytes": warning.export_bytes,
                        "computed_bytes": warning.computed_bytes,
                        "delta_bytes": warning.delta_bytes,
                        "tolerance_bytes": warning.tolerance_bytes,
                    }
                    for warning in self.quality.warnings
                ],
            },
            "top": self.top,
            "as_of": _iso(datetime.fromtimestamp(self.age_as_of, tz=UTC)),
            "dirs": {
                "listed": len(self.dirs),
                "total_dirs": self.totals.dirs,
                "items": [_dir_dict(size) for size in self.dirs],
            },
            "files": {
                "listed": len(self.files),
                "items": [_file_dict(row) for row in self.files],
            },
            "extensions": {
                "listed": len(self.extensions),
                "distinct": self.distinct_extensions,
                "items": [
                    {
                        "ext": item.ext,
                        "files": item.files,
                        "unique_files": item.unique_files,
                        "bytes": item.bytes,
                        "unique_bytes": item.unique_bytes,
                        "allocated_bytes": item.allocated_bytes,
                    }
                    for item in self.extensions
                ],
            },
            "age_buckets": {
                "as_of": _iso(datetime.fromtimestamp(self.age_as_of, tz=UTC)),
                "items": [
                    {
                        "bucket": item.label,
                        "files": item.files,
                        "unique_files": item.unique_files,
                        "bytes": item.bytes,
                        "unique_bytes": item.unique_bytes,
                        "allocated_bytes": item.allocated_bytes,
                        "oldest_mtime": item.oldest_mtime,
                        "newest_mtime": item.newest_mtime,
                    }
                    for item in self.age
                ],
            },
            "apps": {
                "listed": len(self.apps),
                "total_apps": self.total_apps,
                "roots": list(self.app_roots),
                "items": [
                    {
                        "app": item.app,
                        "bytes": item.bytes,
                        "unique_bytes": item.unique_bytes,
                        "allocated_bytes": item.allocated_bytes,
                        "unique_allocated_bytes": item.unique_allocated_bytes,
                        "file_count": item.file_count,
                        "unique_file_count": item.unique_file_count,
                        "dir_count": item.dir_count,
                        "roots": list(item.roots),
                    }
                    for item in self.apps
                ],
            },
        }


def _iso(value: datetime) -> str:
    return value.astimezone(UTC).isoformat(timespec="seconds")


def _dir_dict(size: DirSize) -> dict[str, object]:
    return {
        "path": size.path,
        "depth": size.depth,
        "bytes": size.bytes,
        "unique_bytes": size.unique_bytes,
        "allocated_bytes": size.allocated_bytes,
        "unique_allocated_bytes": size.unique_allocated_bytes,
        "own_bytes": size.own_bytes,
        "file_count": size.file_count,
        "unique_file_count": size.unique_file_count,
        "dir_count": size.dir_count,
        "child_dir_count": size.child_dir_count,
        "export_bytes": size.export_bytes,
        "export_allocated": size.export_allocated,
        "delta_bytes": size.delta_bytes,
    }


def _file_dict(row: FileRow) -> dict[str, object]:
    return {
        "path": row.path,
        "size": row.size,
        "allocated": row.allocated,
        "mtime": row.mtime,
        "ext": row.ext,
        "hardlink": row.hardlink,
        "depth": row.depth,
    }


# --------------------------------------------------------------------------- #
# Totals and directory sizes
# --------------------------------------------------------------------------- #


def totals(conn: sqlite3.Connection) -> db.IndexSummary:
    """Index-wide totals, including the hardlink-aware ``unique_file_bytes``."""
    return db.index_summary(conn)


def allocation_available(conn: sqlite3.Connection) -> bool:
    """Whether the export carried any ``Allocated`` data."""
    row = conn.execute(
        "SELECT 1 FROM entries WHERE is_dir = 0 AND allocated IS NOT NULL LIMIT 1"
    ).fetchone()
    return row is not None


def unattributed_usage(conn: sqlite3.Connection) -> tuple[int, int]:
    """Files/size with no parent in the index (orphan rows), as ``(files, bytes)``."""
    row = conn.execute(
        "SELECT COUNT(*), COALESCE(SUM(size), 0) FROM entries "
        "WHERE is_dir = 0 AND parent_id IS NULL"
    ).fetchone()
    if row is None:  # pragma: no cover - a bare COUNT always returns one row
        return 0, 0
    return int(row[0]), int(row[1])


@dataclass(slots=True)
class _FolderAccumulator:
    """Running subtree totals for one folder (files plus finished children)."""

    bytes: int = 0
    unique_bytes: int = 0
    allocated_bytes: int = 0
    unique_allocated_bytes: int = 0
    own_bytes: int = 0
    file_count: int = 0
    unique_file_count: int = 0
    dir_count: int = 0
    child_dir_count: int = 0

    def add_file(self, size: int, allocated: int | None, *, hardlink: bool) -> None:
        payload = allocated or 0
        self.bytes += size
        self.allocated_bytes += payload
        self.own_bytes += size
        self.file_count += 1
        if hardlink:
            # The payload is attributed to the unflagged source entry.
            return
        self.unique_bytes += size
        self.unique_allocated_bytes += payload
        self.unique_file_count += 1

    def add_child(self, child: _FolderAccumulator) -> None:
        self.bytes += child.bytes
        self.unique_bytes += child.unique_bytes
        self.allocated_bytes += child.allocated_bytes
        self.unique_allocated_bytes += child.unique_allocated_bytes
        self.file_count += child.file_count
        self.unique_file_count += child.unique_file_count
        self.dir_count += child.dir_count + 1
        self.child_dir_count += 1


_FILES_SQL = "SELECT parent_id, size, allocated, hardlink_flag FROM entries WHERE is_dir = 0"

_FOLDERS_SQL = """
SELECT id, parent_id, path, depth, size, allocated
FROM entries
WHERE is_dir = 1
ORDER BY depth DESC, id DESC
"""


def iter_dir_sizes(conn: sqlite3.Connection) -> Iterator[DirSize]:
    """Yield every folder with its subtree aggregates, children before parents.

    Byte values are accumulated from **file rows only**; the folder row's own
    (verbatim, descendant) totals are carried along untouched in
    ``export_bytes`` / ``export_allocated`` for the cross-check.  The pass is
    streaming: memory is one accumulator per folder, never one per file.
    """
    accumulators: dict[int, _FolderAccumulator] = {}
    for parent_id, size, allocated, hardlink in conn.execute(_FILES_SQL):
        if parent_id is None:
            continue  # orphan rows have no folder to count towards
        accumulator = accumulators.get(parent_id)
        if accumulator is None:
            accumulator = _FolderAccumulator()
            accumulators[parent_id] = accumulator
        accumulator.add_file(int(size), allocated, hardlink=bool(hardlink))

    for entry_id, parent_id, path, depth, export_bytes, export_allocated in conn.execute(
        _FOLDERS_SQL
    ):
        accumulator = accumulators.pop(int(entry_id), None)
        if accumulator is None:
            accumulator = _FolderAccumulator()
        yield DirSize(
            entry_id=int(entry_id),
            parent_id=int(parent_id) if parent_id is not None else None,
            path=str(path),
            depth=int(depth),
            bytes=accumulator.bytes,
            unique_bytes=accumulator.unique_bytes,
            allocated_bytes=accumulator.allocated_bytes,
            unique_allocated_bytes=accumulator.unique_allocated_bytes,
            own_bytes=accumulator.own_bytes,
            file_count=accumulator.file_count,
            unique_file_count=accumulator.unique_file_count,
            dir_count=accumulator.dir_count,
            child_dir_count=accumulator.child_dir_count,
            export_bytes=int(export_bytes),
            export_allocated=int(export_allocated) if export_allocated is not None else None,
        )
        if parent_id is not None:
            parent = accumulators.get(int(parent_id))
            if parent is None:
                parent = _FolderAccumulator()
                accumulators[int(parent_id)] = parent
            parent.add_child(accumulator)


class _TopDirs:
    """Bounded heap: the ``limit`` largest folders seen so far (streaming)."""

    def __init__(self, limit: int) -> None:
        self._limit = limit
        self._heap: list[tuple[int, int, DirSize]] = []
        self._sequence = 0

    def offer(self, size: DirSize) -> None:
        """Consider one folder; keeps memory at ``limit`` items."""
        if self._limit <= 0:
            return
        item = (size.bytes, self._sequence, size)
        self._sequence += 1
        if len(self._heap) < self._limit:
            heapq.heappush(self._heap, item)
        elif size.bytes > self._heap[0][0]:
            heapq.heapreplace(self._heap, item)

    def result(self) -> tuple[DirSize, ...]:
        """The selected folders, largest first (ties by path)."""
        return tuple(sorted((item[2] for item in self._heap), key=_dir_rank_key))


def _dir_rank_key(size: DirSize) -> tuple[int, str]:
    return (-size.bytes, size.path)


def rank_dirs(sizes: Iterable[DirSize], limit: int) -> tuple[DirSize, ...]:
    """Return the ``limit`` largest folders by subtree bytes (desc, path asc)."""
    top = _TopDirs(limit)
    for size in sizes:
        top.offer(size)
    return top.result()


def top_dirs(conn: sqlite3.Connection, limit: int = DEFAULT_TOP) -> tuple[DirSize, ...]:
    """The ``limit`` largest folders by subtree bytes (file rows only)."""
    return rank_dirs(iter_dir_sizes(conn), limit)


def top_files(
    conn: sqlite3.Connection, limit: int = DEFAULT_TOP, *, include_hardlinks: bool = False
) -> tuple[FileRow, ...]:
    """The ``limit`` largest files.

    Hard-linked copies are skipped by default: their payload is already
    reported on the source entry (`include_hardlinks=True` lists them too).
    """
    limit = max(limit, 0)
    sql = (
        "SELECT id, path, size, allocated, mtime, ext, hardlink_flag, depth "
        "FROM entries WHERE is_dir = 0"
    )
    if not include_hardlinks:
        sql += " AND hardlink_flag = 0"
    sql += " ORDER BY size DESC, path ASC LIMIT ?"
    rows = conn.execute(sql, (limit,)).fetchall()
    return tuple(
        FileRow(
            entry_id=int(row[0]),
            path=str(row[1]),
            size=int(row[2]),
            allocated=int(row[3]) if row[3] is not None else None,
            mtime=int(row[4]) if row[4] is not None else None,
            ext=row[5],
            hardlink=bool(row[6]),
            depth=int(row[7]),
        )
        for row in rows
    )


# --------------------------------------------------------------------------- #
# Cross-check (folder rows vs file-row sums)
# --------------------------------------------------------------------------- #


def _cross_check_warning(
    size: DirSize, tolerance_bytes: int, tolerance_ratio: float
) -> CrossCheckWarning | None:
    """A warning for one folder, or ``None`` when it is within tolerance."""
    tolerance = max(tolerance_bytes, round(tolerance_ratio * size.export_bytes))
    if abs(size.delta_bytes) <= tolerance:
        return None
    return CrossCheckWarning(
        path=size.path,
        entry_id=size.entry_id,
        depth=size.depth,
        export_bytes=size.export_bytes,
        computed_bytes=size.bytes,
        delta_bytes=size.delta_bytes,
        tolerance_bytes=tolerance,
    )


def check_folder_sizes(
    sizes: Iterable[DirSize],
    *,
    tolerance_bytes: int = DEFAULT_TOLERANCE_BYTES,
    tolerance_ratio: float = DEFAULT_TOLERANCE_RATIO,
) -> CrossCheck:
    """Compare each folder row against its file-row sum.

    A folder is reported when the difference is larger than both the absolute
    and the ratio tolerance (the larger of the two wins, so a huge folder gets
    proportionally more slack than a small one).
    """
    checked = 0
    warnings: list[CrossCheckWarning] = []
    for size in sizes:
        checked += 1
        warning = _cross_check_warning(size, tolerance_bytes, tolerance_ratio)
        if warning is not None:
            warnings.append(warning)
    return CrossCheck(
        checked_dirs=checked,
        tolerance_bytes=tolerance_bytes,
        tolerance_ratio=tolerance_ratio,
        warnings=tuple(warnings),
    )


def folder_cross_check(
    conn: sqlite3.Connection,
    *,
    tolerance_bytes: int = DEFAULT_TOLERANCE_BYTES,
    tolerance_ratio: float = DEFAULT_TOLERANCE_RATIO,
) -> CrossCheck:
    """Cross-check every folder row against the sum of its descendant file rows."""
    return check_folder_sizes(
        iter_dir_sizes(conn), tolerance_bytes=tolerance_bytes, tolerance_ratio=tolerance_ratio
    )


# --------------------------------------------------------------------------- #
# Extensions and age
# --------------------------------------------------------------------------- #


_EXTENSIONS_SQL = """
SELECT
    ext,
    COUNT(*),
    SUM(CASE WHEN hardlink_flag = 0 THEN 1 ELSE 0 END),
    SUM(size),
    SUM(CASE WHEN hardlink_flag = 0 THEN size ELSE 0 END),
    SUM(COALESCE(allocated, 0))
FROM entries
WHERE is_dir = 0
GROUP BY ext
ORDER BY SUM(size) DESC, ext ASC
"""


def extension_totals(conn: sqlite3.Connection) -> tuple[ExtensionTotal, ...]:
    """Per-extension file counts and byte totals, largest first."""
    return tuple(
        ExtensionTotal(
            ext=str(row[0] or ""),
            files=int(row[1]),
            unique_files=int(row[2]),
            bytes=int(row[3]),
            unique_bytes=int(row[4]),
            allocated_bytes=int(row[5]),
        )
        for row in conn.execute(_EXTENSIONS_SQL)
    )


def _validate_age_buckets(buckets: Sequence[tuple[str, int | None]]) -> None:
    if not buckets:
        raise ValueError("at least one age bucket is required")
    seen: set[str] = set()
    previous = 0
    for position, (label, limit) in enumerate(buckets):
        if not label or label == UNKNOWN_AGE_BUCKET:
            raise ValueError(f"age bucket label {label!r} is empty or reserved")
        if label in seen:
            raise ValueError(f"duplicate age bucket label {label!r}")
        seen.add(label)
        if limit is None:
            if position != len(buckets) - 1:
                raise ValueError(f"only the last age bucket may be open-ended: {label!r}")
            continue
        if limit <= previous:
            raise ValueError(f"age bucket limits must increase: {label!r} follows {previous} days")
        previous = limit


def age_buckets(
    conn: sqlite3.Connection,
    *,
    now: int | None = None,
    buckets: Sequence[tuple[str, int | None]] = DEFAULT_AGE_BUCKETS,
) -> tuple[AgeBucket, ...]:
    """File counts and byte totals per age bucket (by ``mtime``).

    ``now`` is epoch seconds (default: the clock).  Files without a timestamp
    land in the :data:`UNKNOWN_AGE_BUCKET` bucket; timestamps in the future
    fall into the youngest bucket.
    """
    _validate_age_buckets(buckets)
    moment = int(now if now is not None else datetime.now(tz=UTC).timestamp())
    conditions = ["WHEN mtime IS NULL THEN :unknown"]
    parameters: dict[str, object] = {"unknown": UNKNOWN_AGE_BUCKET, "now": moment}
    for position, (_label, limit) in enumerate(buckets):
        if limit is None:
            continue
        conditions.append(f"WHEN :now - mtime <= :limit{position} THEN :label{position}")
        parameters[f"limit{position}"] = limit * _DAY_SECONDS
        parameters[f"label{position}"] = buckets[position][0]
    sql = (
        f"SELECT CASE {' '.join(conditions)} ELSE :last END AS bucket, "
        "COUNT(*), "
        "SUM(CASE WHEN hardlink_flag = 0 THEN 1 ELSE 0 END), "
        "SUM(size), "
        "SUM(CASE WHEN hardlink_flag = 0 THEN size ELSE 0 END), "
        "SUM(COALESCE(allocated, 0)), "
        "MIN(mtime), MAX(mtime) "
        "FROM entries WHERE is_dir = 0 GROUP BY bucket"
    )
    parameters["last"] = buckets[-1][0]
    found = {str(row[0]): row for row in conn.execute(sql, parameters)}
    ordered = [label for label, _limit in buckets] + [UNKNOWN_AGE_BUCKET]
    results: list[AgeBucket] = []
    for label in ordered:
        row = found.get(label)
        if row is None:
            results.append(AgeBucket(label, 0, 0, 0, 0, 0, None, None))
            continue
        results.append(
            AgeBucket(
                label=label,
                files=int(row[1]),
                unique_files=int(row[2]),
                bytes=int(row[3]),
                unique_bytes=int(row[4]),
                allocated_bytes=int(row[5]),
                oldest_mtime=int(row[6]) if row[6] is not None else None,
                newest_mtime=int(row[7]) if row[7] is not None else None,
            )
        )
    return tuple(results)


# --------------------------------------------------------------------------- #
# Per-app footprints
# --------------------------------------------------------------------------- #


def _path_components(path: str) -> tuple[str, ...]:
    """Split a stored path into components (``C:\\A\\B`` -> ``("C:", "A", "B")``)."""
    return tuple(part for part in _SEPARATOR_RE.split(path) if part)


def _is_app_root(path: str) -> bool:
    """True when ``path`` ends with one of :data:`APP_ROOT_COMPONENTS`."""
    components = _path_components(path)
    for spec in APP_ROOT_COMPONENTS:
        if (
            len(components) >= len(spec)
            and tuple(part.lower() for part in components[-len(spec) :]) == spec
        ):
            return True
    return False


def app_roots(conn: sqlite3.Connection) -> dict[int, str]:
    """Ids and paths of the folders that anchor the per-app heuristic."""
    patterns: list[str] = []
    for spec in APP_ROOT_COMPONENTS:
        windows_tail = "\\".join(spec)
        posix_tail = "/".join(spec)
        patterns.append(f"%{windows_tail}")
        patterns.append(f"%{posix_tail}")
    where = " OR ".join("path LIKE ?" for _ in patterns)
    sql = f"SELECT id, path FROM entries WHERE is_dir = 1 AND ({where})"
    roots: dict[int, str] = {}
    for entry_id, path in conn.execute(sql, patterns):
        if _is_app_root(str(path)):
            roots[int(entry_id)] = str(path)
    return roots


@dataclass(slots=True)
class _AppAccumulator:
    """Running totals for one application name (merged across roots)."""

    bytes: int = 0
    unique_bytes: int = 0
    allocated_bytes: int = 0
    unique_allocated_bytes: int = 0
    file_count: int = 0
    unique_file_count: int = 0
    dir_count: int = 0
    roots: list[tuple[int, str]] = field(default_factory=list)
    best_name: str = ""
    best_bytes: int = -1

    def add_root(self, name: str, size: DirSize) -> None:
        self.roots.append((size.bytes, size.path))
        self.bytes += size.bytes
        self.unique_bytes += size.unique_bytes
        self.allocated_bytes += size.allocated_bytes
        self.unique_allocated_bytes += size.unique_allocated_bytes
        self.file_count += size.file_count
        self.unique_file_count += size.unique_file_count
        self.dir_count += size.dir_count
        if size.bytes > self.best_bytes or (
            size.bytes == self.best_bytes and name < self.best_name
        ):
            # The spelling carrying the most bytes names the footprint.
            self.best_name, self.best_bytes = name, size.bytes


class AppScan:
    """Feeds :func:`iter_dir_sizes` results into per-app accumulators."""

    def __init__(self, roots: Mapping[int, str]) -> None:
        self._roots = roots
        self._apps: dict[str, _AppAccumulator] = {}

    def feed(self, size: DirSize) -> None:
        """Attribute one folder to an application when its parent is a root."""
        if size.parent_id is None or size.parent_id not in self._roots:
            return
        components = _path_components(size.path)
        name = components[-1] if components else size.path
        accumulator = self._apps.get(name.lower())
        if accumulator is None:
            accumulator = _AppAccumulator()
            self._apps[name.lower()] = accumulator
        accumulator.add_root(name, size)

    @property
    def footprints(self) -> tuple[AppFootprint, ...]:
        """Finished footprints, largest first (path-stable tie-break)."""
        built = [
            AppFootprint(
                app=accumulator.best_name,
                bytes=accumulator.bytes,
                unique_bytes=accumulator.unique_bytes,
                allocated_bytes=accumulator.allocated_bytes,
                unique_allocated_bytes=accumulator.unique_allocated_bytes,
                file_count=accumulator.file_count,
                unique_file_count=accumulator.unique_file_count,
                dir_count=accumulator.dir_count,
                roots=tuple(
                    path for _bytes, path in sorted(accumulator.roots, key=lambda r: (-r[0], r[1]))
                ),
            )
            for accumulator in self._apps.values()
        ]
        built.sort(key=lambda item: (-item.bytes, item.app.lower(), item.app))
        return tuple(built)


def app_footprints(conn: sqlite3.Connection) -> tuple[AppFootprint, ...]:
    """Per-app footprints derived from the direct children of app roots."""
    scan = AppScan(app_roots(conn))
    for size in iter_dir_sizes(conn):
        scan.feed(size)
    return scan.footprints


# --------------------------------------------------------------------------- #
# The full report
# --------------------------------------------------------------------------- #


def stats_report(
    conn: sqlite3.Connection,
    *,
    top: int = DEFAULT_TOP,
    now: int | None = None,
    db_path: str | None = None,
    tolerance_bytes: int = DEFAULT_TOLERANCE_BYTES,
    tolerance_ratio: float = DEFAULT_TOLERANCE_RATIO,
) -> StatsReport:
    """Build every stats view in one streaming pass over the index.

    ``top`` bounds each ranked list, ``now`` (epoch seconds) is the reference
    point for the age buckets, and ``db_path`` is echoed into the report for
    provenance only.
    """
    if top < 1:
        raise StatsError(f"--top must be >= 1, got {top}")
    index_totals = db.index_summary(conn)
    if index_totals.entries == 0:
        raise StatsError("the index is empty; ingest a WizTree export first")

    moment = int(now if now is not None else datetime.now(tz=UTC).timestamp())
    roots = app_roots(conn)
    app_scan = AppScan(roots)
    ranked = _TopDirs(top)
    warnings: list[CrossCheckWarning] = []
    checked = 0
    for size in iter_dir_sizes(conn):
        checked += 1
        warning = _cross_check_warning(size, tolerance_bytes, tolerance_ratio)
        if warning is not None:
            warnings.append(warning)
        app_scan.feed(size)
        ranked.offer(size)

    quality = CrossCheck(
        checked_dirs=checked,
        tolerance_bytes=tolerance_bytes,
        tolerance_ratio=tolerance_ratio,
        warnings=tuple(warnings),
    )
    unattributed_files, unattributed_bytes = unattributed_usage(conn)
    extensions = extension_totals(conn)
    apps = app_scan.footprints
    return StatsReport(
        generated=datetime.now(tz=UTC),
        db_path=db_path,
        schema_version=db.schema_version(conn),
        source_csv=db.meta_get(conn, "source.csv"),
        source_machine=db.meta_get(conn, "source.machine"),
        source_exported=db.meta_get(conn, "source.exported"),
        totals=index_totals,
        allocation_available=allocation_available(conn),
        unattributed_files=unattributed_files,
        unattributed_bytes=unattributed_bytes,
        top=top,
        dirs=ranked.result(),
        files=top_files(conn, top),
        extensions=extensions[:top],
        distinct_extensions=len(extensions),
        age_as_of=moment,
        age=age_buckets(conn, now=moment),
        apps=apps[:top],
        total_apps=len(apps),
        app_roots=tuple(roots.values()),
        quality=quality,
    )


# --------------------------------------------------------------------------- #
# Materialisation (schema v2 tables)
# --------------------------------------------------------------------------- #


def build_derived(conn: sqlite3.Connection) -> DerivedStats:
    """Rebuild the materialised ``dir_sizes`` / ``app_footprints`` tables.

    One streaming pass over the index; the tables are derived data, so they
    are cleared and rewritten (and removed with their entries by the foreign
    key cascade).  Later stages can then answer "how big is this folder?"
    with a plain ``SELECT`` instead of re-deriving it.
    """
    roots = app_roots(conn)
    app_scan = AppScan(roots)
    built_at = datetime.now(tz=UTC)
    dir_rows = 0
    batch: list[tuple[object, ...]] = []
    conn.execute("BEGIN IMMEDIATE")
    try:
        conn.execute("DELETE FROM dir_sizes")
        conn.execute("DELETE FROM app_footprints")
        for size in iter_dir_sizes(conn):
            app_scan.feed(size)
            batch.append(
                (
                    size.entry_id,
                    size.path,
                    size.depth,
                    size.bytes,
                    size.unique_bytes,
                    size.allocated_bytes,
                    size.unique_allocated_bytes,
                    size.own_bytes,
                    size.file_count,
                    size.unique_file_count,
                    size.dir_count,
                    size.child_dir_count,
                    size.export_bytes,
                    size.export_allocated,
                )
            )
            if len(batch) >= _INSERT_BATCH:
                _insert_dir_sizes(conn, batch)
                dir_rows += len(batch)
                batch.clear()
        if batch:
            _insert_dir_sizes(conn, batch)
            dir_rows += len(batch)
            batch.clear()
        apps = app_scan.footprints
        conn.executemany(
            "INSERT INTO app_footprints(app, bytes, unique_bytes, allocated_bytes, "
            "unique_allocated_bytes, file_count, unique_file_count, dir_count, roots) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            [
                (
                    item.app,
                    item.bytes,
                    item.unique_bytes,
                    item.allocated_bytes,
                    item.unique_allocated_bytes,
                    item.file_count,
                    item.unique_file_count,
                    item.dir_count,
                    json.dumps(list(item.roots)),
                )
                for item in apps
            ],
        )
        db.meta_set(conn, "stats.built_entries", db.entry_count(conn))
        db.meta_set(conn, "stats.built_at", _iso(built_at))
    except BaseException:
        conn.rollback()
        raise
    conn.commit()
    return DerivedStats(dir_sizes=dir_rows, app_footprints=len(apps), built_at=built_at)


def _insert_dir_sizes(conn: sqlite3.Connection, rows: list[tuple[object, ...]]) -> None:
    conn.executemany(
        "INSERT INTO dir_sizes(entry_id, path, depth, bytes, unique_bytes, allocated_bytes, "
        "unique_allocated_bytes, own_bytes, file_count, unique_file_count, dir_count, "
        "child_dir_count, export_bytes, export_allocated) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        rows,
    )


# --------------------------------------------------------------------------- #
# Rendering
# --------------------------------------------------------------------------- #


def format_bytes(value: int) -> str:
    """Human-readable binary size (``2_150_400 -> '2.1 MiB'``)."""
    sign = "-" if value < 0 else ""
    size = float(abs(value))
    unit = "B"
    for candidate in ("KiB", "MiB", "GiB", "TiB", "PiB"):
        if size < 1024:
            break
        size /= 1024
        unit = candidate
    if unit == "B":
        return f"{sign}{int(size)} B"
    return f"{sign}{size:.1f} {unit}"


def _plural(count: int, singular: str, plural: str | None = None) -> str:
    return f"{count} {singular if count == 1 else (plural or singular + 's')}"


def _section(title: str) -> str:
    return f"\n{title}:"


def render_text(report: StatsReport, *, by: str | None = None) -> str:
    """Render the report as compact plain text (``by`` selects one section)."""
    if by is not None and by not in ("dir", "ext", "age", "app"):
        raise StatsError(f"unknown breakdown {by!r}; pick dir, ext, age or app")

    totals_row = report.totals
    lines = [
        f"index: {report.db_path or '(index)'} (schema v{report.schema_version})",
    ]
    if report.source_csv:
        machine = f", machine {report.source_machine}" if report.source_machine else ""
        lines.append(f"source: {report.source_csv}{machine}")
    allocated = (
        f", {format_bytes(totals_row.allocated_bytes)} allocated "
        f"({format_bytes(totals_row.unique_allocated_bytes)} unique)"
        if report.allocation_available
        else ", allocated data not in export"
    )
    lines.append(
        f"totals: {_plural(totals_row.dirs, 'dir')}, {_plural(totals_row.files, 'file')}, "
        f"{format_bytes(totals_row.file_bytes)} logical "
        f"({format_bytes(totals_row.unique_file_bytes)} unique){allocated}"
    )
    if totals_row.hardlink_files:
        unique_files = totals_row.files - totals_row.hardlink_files
        lines.append(
            f"hardlinks: {_plural(totals_row.hardlink_files, 'file')} "
            f"({_plural(unique_files, 'file')} count their bytes)"
        )
    if report.unattributed_files:
        lines.append(
            f"unattributed: {_plural(report.unattributed_files, 'file')}, "
            f"{format_bytes(report.unattributed_bytes)} without a folder row"
        )

    quality = report.quality
    if quality.warnings:
        verb = "disagrees" if len(quality.warnings) == 1 else "disagree"
        lines.append(
            f"data quality: {_plural(len(quality.warnings), 'folder')} {verb} with file-row "
            f"sums (checked {_plural(quality.checked_dirs, 'folder')}, tolerance "
            f"{format_bytes(quality.tolerance_bytes)} / {quality.tolerance_ratio:.1%})"
        )
        for warning in quality.warnings[:5]:
            sign = "+" if warning.delta_bytes >= 0 else ""
            lines.append(
                f"  warning: {warning.path}  export {format_bytes(warning.export_bytes)} vs "
                f"computed {format_bytes(warning.computed_bytes)} "
                f"(delta {sign}{format_bytes(warning.delta_bytes)})"
            )
        if len(quality.warnings) > 5:
            lines.append(f"  ... {len(quality.warnings) - 5} more")
    else:
        lines.append(
            f"data quality: ok ({_plural(quality.checked_dirs, 'folder')} checked, tolerance "
            f"{format_bytes(quality.tolerance_bytes)} / {quality.tolerance_ratio:.1%})"
        )

    if by in (None, "dir"):
        title = f"top directories by subtree ({len(report.dirs)} of {totals_row.dirs})"
        lines.append(_section(title))
        for dir_size in report.dirs:
            lines.append(
                f"  {format_bytes(dir_size.bytes):>10}  {dir_size.path}  "
                f"[{_plural(dir_size.file_count, 'file')}, "
                f"{_plural(dir_size.dir_count, 'dir')}]"
            )
        lines.append(_section(f"top files ({len(report.files)})"))
        for file_row in report.files:
            marker = " (hardlink copy)" if file_row.hardlink else ""
            lines.append(f"  {format_bytes(file_row.size):>10}  {file_row.path}{marker}")

    if by in (None, "ext"):
        title = f"extensions ({len(report.extensions)} of {report.distinct_extensions})"
        lines.append(_section(title))
        for ext_total in report.extensions:
            label = ext_total.ext or "(none)"
            lines.append(
                f"  {format_bytes(ext_total.bytes):>10}  {label:<10} "
                f"{_plural(ext_total.files, 'file')}"
            )

    if by in (None, "age"):
        stamp = _iso(datetime.fromtimestamp(report.age_as_of, tz=UTC))
        lines.append(_section(f"age buckets (as of {stamp})"))
        for bucket in report.age:
            counts = _plural(bucket.files, "file")
            lines.append(f"  {bucket.label:<10} {counts:>12}  {format_bytes(bucket.bytes):>10}")

    if by in (None, "app"):
        lines.append(_section(f"apps ({len(report.apps)} of {report.total_apps})"))
        for app in report.apps:
            extra = f" +{len(app.roots) - 1}" if len(app.roots) > 1 else ""
            root = app.roots[0] if app.roots else "(unknown)"
            lines.append(
                f"  {format_bytes(app.bytes):>10}  {app.app:<20} "
                f"{_plural(app.file_count, 'file')}  [{root}{extra}]"
            )
    return "\n".join(lines) + "\n"


def render_json(report: StatsReport) -> str:
    """Render the complete report (every view) as pretty-printed JSON."""
    return json.dumps(report.to_dict(), indent=2) + "\n"


__all__ = [
    "APP_ROOT_COMPONENTS",
    "DEFAULT_AGE_BUCKETS",
    "DEFAULT_TOLERANCE_BYTES",
    "DEFAULT_TOLERANCE_RATIO",
    "DEFAULT_TOP",
    "UNKNOWN_AGE_BUCKET",
    "AgeBucket",
    "AppFootprint",
    "AppScan",
    "CrossCheck",
    "CrossCheckWarning",
    "DerivedStats",
    "DirSize",
    "ExtensionTotal",
    "FileRow",
    "StatsError",
    "StatsReport",
    "age_buckets",
    "allocation_available",
    "app_footprints",
    "app_roots",
    "build_derived",
    "check_folder_sizes",
    "extension_totals",
    "folder_cross_check",
    "format_bytes",
    "iter_dir_sizes",
    "rank_dirs",
    "render_json",
    "render_text",
    "stats_report",
    "top_dirs",
    "top_files",
    "totals",
    "unattributed_usage",
]
