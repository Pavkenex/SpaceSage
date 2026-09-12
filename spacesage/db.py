"""SQLite index: schema, versioned migrations and shared helpers.

The index is a single SQLite file (WAL journal mode) built from a WizTree CSV
export by :mod:`spacesage.ingest`; the data model is ``docs/design.md``
section 5.

Schema revisions are keyed by ``PRAGMA user_version``: a migration with key
*n* raises a database from schema version ``n - 1`` to ``n``.  A database at a
higher version than this build understands is refused rather than corrupted.

Conventions:

* ``entries`` holds **both** file and folder rows exactly as exported.  Folder
  rows carry descendant totals and must never be summed together with their
  children -- roll-ups use file rows only (``is_dir = 0``).
* ``hardlink_flag = 1`` marks a hard-linked file whose bytes are already
  accounted for by another entry; roll-ups count those rows once.
* ``mtime`` is an epoch (seconds).  WizTree timestamps are zone-less local
  time, so they are interpreted as UTC -- deterministic and documented; see
  :mod:`spacesage.ingest`.
* ``ext`` is the lower-cased extension without the dot for files (``''`` when
  absent) and ``NULL`` for folders.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import NamedTuple

SCHEMA_VERSION = 1
"""Schema revision this build reads and writes."""

DEFAULT_DB_NAME = "spacesage.db"
"""File name used when ``--db`` points at a directory."""

_SCHEMA_V1 = """
CREATE TABLE meta (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE drives (
    name           TEXT PRIMARY KEY,          -- "C:"
    fs_type        TEXT,
    capacity_bytes INTEGER,
    free_bytes     INTEGER
);

CREATE TABLE entries (
    id            INTEGER PRIMARY KEY,
    path          TEXT    NOT NULL UNIQUE,    -- full path, folders keep no trailing slash
    name          TEXT    NOT NULL,           -- last path component
    parent_id     INTEGER REFERENCES entries(id),
    is_dir        INTEGER NOT NULL CHECK (is_dir IN (0, 1)),
    size          INTEGER NOT NULL,           -- logical bytes (folders: descendant total)
    allocated     INTEGER,                    -- on-disk bytes, NULL when not exported
    mtime         INTEGER,                    -- epoch seconds (see module docstring)
    attrs         TEXT,                       -- raw attribute cell
    hardlink_flag INTEGER NOT NULL DEFAULT 0 CHECK (hardlink_flag IN (0, 1)),
    depth         INTEGER NOT NULL,           -- components below the exported root
    ext           TEXT                        -- file extension, lowercase, no dot
);

CREATE INDEX idx_entries_size   ON entries(size DESC);
CREATE INDEX idx_entries_parent ON entries(parent_id);
CREATE INDEX idx_entries_mtime  ON entries(mtime);
CREATE INDEX idx_entries_ext    ON entries(ext);
"""

MIGRATIONS: Mapping[int, str] = {1: _SCHEMA_V1}
"""Migration scripts keyed by the schema version they produce."""

INSERT_ENTRY_SQL = """
INSERT OR IGNORE INTO entries
    (id, path, name, parent_id, is_dir, size, allocated, mtime, attrs, hardlink_flag, depth, ext)
VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
"""


class SchemaError(RuntimeError):
    """Raised when the on-disk schema cannot be migrated by this build."""


class EntryRow(NamedTuple):
    """One row of ``entries``, in insert order (see :data:`INSERT_ENTRY_SQL`)."""

    id: int
    path: str
    name: str
    parent_id: int | None
    is_dir: int
    size: int
    allocated: int | None
    mtime: int | None
    attrs: str | None
    hardlink_flag: int
    depth: int
    ext: str | None


@dataclass(frozen=True)
class IndexSummary:
    """Aggregates recomputed from the index itself (never from ingest counters)."""

    entries: int
    dirs: int
    files: int
    file_bytes: int
    allocated_bytes: int
    unique_allocated_bytes: int
    hardlink_files: int


_SUMMARY_SQL = """
SELECT
    COUNT(*),
    COALESCE(SUM(is_dir), 0),
    COALESCE(SUM(CASE WHEN is_dir = 0 THEN 1 ELSE 0 END), 0),
    COALESCE(SUM(CASE WHEN is_dir = 0 THEN size ELSE 0 END), 0),
    COALESCE(SUM(CASE WHEN is_dir = 0 THEN allocated ELSE 0 END), 0),
    COALESCE(
        SUM(CASE WHEN is_dir = 0 AND hardlink_flag = 0 THEN allocated ELSE 0 END), 0
    ),
    COALESCE(SUM(CASE WHEN is_dir = 0 AND hardlink_flag = 1 THEN 1 ELSE 0 END), 0)
FROM entries
"""


def resolve_db_path(value: str | Path) -> Path:
    """Return the index file for ``value``.

    ``value`` may be a file path or an existing directory (the index is then
    ``<directory>/spacesage.db``, the ``--db DIR`` form in docs/design.md).
    """
    path = Path(value).expanduser()
    if path.is_dir():
        return path / DEFAULT_DB_NAME
    return path


def open_db(path: str | Path, *, migrate: bool = True) -> sqlite3.Connection:
    """Open (creating if needed) the index at ``path`` and apply migrations.

    The connection is in autocommit mode (``isolation_level = None``) so
    callers control transactions explicitly -- the ingest loader wraps the
    whole load in one transaction.
    """
    target = Path(path).expanduser()
    if str(target.parent):
        target.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(target, isolation_level=None)
    _configure(conn)
    if migrate:
        apply_migrations(conn)
    return conn


def _configure(conn: sqlite3.Connection) -> None:
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("PRAGMA temp_store=MEMORY")
    conn.execute("PRAGMA cache_size=-65536")  # 64 MiB page cache


def schema_version(conn: sqlite3.Connection) -> int:
    """Return the schema version stored in ``PRAGMA user_version`` (0 if unset)."""
    row = conn.execute("PRAGMA user_version").fetchone()
    return int(row[0]) if row else 0


def apply_migrations(conn: sqlite3.Connection) -> int:
    """Bring ``conn`` up to :data:`SCHEMA_VERSION`; return the resulting version."""
    current = schema_version(conn)
    if current > SCHEMA_VERSION:
        raise SchemaError(
            f"index schema v{current} is newer than this build supports (v{SCHEMA_VERSION})"
        )
    for target in range(current + 1, SCHEMA_VERSION + 1):
        script = MIGRATIONS.get(target)
        if script is None:  # pragma: no cover - MIGRATIONS is exhaustive by construction
            raise SchemaError(f"missing migration script for schema v{target}")
        conn.executescript(script)
        conn.execute(f"PRAGMA user_version={target}")
    return schema_version(conn)


def meta_get(conn: sqlite3.Connection, key: str, default: str | None = None) -> str | None:
    """Read one ``meta`` value."""
    row = conn.execute("SELECT value FROM meta WHERE key = ?", (key,)).fetchone()
    return str(row[0]) if row else default


def meta_set(conn: sqlite3.Connection, key: str, value: object) -> None:
    """Upsert one ``meta`` value (stored as text)."""
    conn.execute(
        "INSERT INTO meta(key, value) VALUES (?, ?) "
        "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
        (key, str(value)),
    )


def meta_set_many(conn: sqlite3.Connection, items: Mapping[str, object]) -> None:
    """Upsert several ``meta`` values."""
    for key, value in items.items():
        meta_set(conn, key, value)


def entry_count(conn: sqlite3.Connection) -> int:
    """Number of rows currently in ``entries``."""
    row = conn.execute("SELECT COUNT(*) FROM entries").fetchone()
    return int(row[0]) if row else 0


def index_summary(conn: sqlite3.Connection) -> IndexSummary:
    """Aggregates recomputed from the index (file rows only for byte totals)."""
    row = conn.execute(_SUMMARY_SQL).fetchone()
    if row is None:  # pragma: no cover - COUNT(*) always returns a row
        return IndexSummary(0, 0, 0, 0, 0, 0, 0)
    return IndexSummary(*(int(value) for value in row))


def insert_entries(conn: sqlite3.Connection, rows: Iterable[EntryRow]) -> int:
    """Insert a batch of entries; return how many rows were actually inserted.

    ``INSERT OR IGNORE`` makes duplicate paths a counted, non-fatal condition
    (the caller reports them); the returned count is the difference.
    """
    cursor = conn.executemany(INSERT_ENTRY_SQL, rows)
    return cursor.rowcount if cursor.rowcount is not None else 0


def clear_index(conn: sqlite3.Connection) -> None:
    """Delete every indexed row (schema untouched); used by ``ingest --replace``."""
    conn.execute("BEGIN IMMEDIATE")
    try:
        conn.execute("DELETE FROM entries")
        conn.execute("DELETE FROM drives")
        conn.execute("DELETE FROM meta")
    except BaseException:
        conn.rollback()
        raise
    conn.commit()
