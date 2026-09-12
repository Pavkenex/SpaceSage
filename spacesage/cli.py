"""Internal command-line interface (development, CI and automation only).

The product is the PySide6 desktop application; this CLI exists so the engine
can be driven headlessly by tests, scripts and CI. Engine subcommands
(``ingest``, ``stats``, ``plan``, ``apply``, ``undo``, ``ai``) are registered by
the slices that implement them -- see ``docs/slices.md``.
"""

from __future__ import annotations

import argparse
import sys
from collections.abc import Callable, Sequence
from pathlib import Path

from spacesage import __version__, db
from spacesage.ingest import IngestError, IngestProgress, RunStats, ingest_csv

PROG = "spacesage"
DESCRIPTION = "Turn a WizTree export into a safety-gated course of action for a full disk."
EPILOG = "The product is the desktop app; this CLI is a development and automation surface."


class _ProgressPrinter:
    """Progress callback used by ``ingest --progress`` (one line per batch)."""

    def __call__(self, update: IngestProgress) -> None:
        percent = (update.bytes_read / update.file_size * 100) if update.file_size else 0.0
        print(
            f"progress: {update.rows_read} rows, {percent:.1f}%, {update.rows_per_sec:.0f} rows/s",
            file=sys.stderr,
            flush=True,
        )


def build_parser() -> argparse.ArgumentParser:
    """Build the top-level argument parser."""
    parser = argparse.ArgumentParser(
        prog=PROG,
        description=DESCRIPTION,
        epilog=EPILOG,
    )
    parser.add_argument("--version", action="version", version=f"{PROG} {__version__}")
    subparsers = parser.add_subparsers(dest="command", metavar="<command>")

    ingest = subparsers.add_parser(
        "ingest",
        help="stream a WizTree CSV export into the SQLite index",
        description=(
            "Stream a WizTree CSV export into the SQLite index. The export is "
            "parsed by column name, folders keep no trailing slash, and hard "
            "links are flagged (leading zero in the Allocated cell)."
        ),
    )
    ingest.add_argument("csv", metavar="CSV", type=Path, help="path to the WizTree CSV export")
    ingest.add_argument(
        "--db",
        metavar="PATH",
        default=db.DEFAULT_DB_NAME,
        help=("index database file, or a directory (then PATH/spacesage.db); default: %(default)s"),
    )
    ingest.add_argument(
        "--replace",
        action="store_true",
        help="clear an existing index at PATH before loading (refused otherwise)",
    )
    ingest.add_argument(
        "--progress",
        action="store_true",
        help="print progress lines to stderr while loading",
    )
    ingest.set_defaults(handler=_run_ingest)
    return parser


def _run_ingest(args: argparse.Namespace) -> int:
    progress = _ProgressPrinter() if args.progress else None
    try:
        stats = ingest_csv(args.csv, args.db, replace=bool(args.replace), progress=progress)
    except IngestError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    _print_stats(stats)
    return 0


def _skip_summary(stats: RunStats) -> str:
    parts = (
        (stats.blank_rows, "blank"),
        (stats.short_rows, "short"),
        (stats.long_rows, "long"),
        (stats.empty_name_rows, "nameless"),
        (stats.duplicate_rows, "duplicate"),
        (stats.orphan_rows, "orphan-parent"),
        (stats.capacity_rows, "capacity"),
        (stats.bad_number_rows, "non-numeric"),
        (stats.bad_mtime_rows, "bad-timestamp"),
    )
    return ", ".join(f"{count} {label}" for count, label in parts if count)


def _print_stats(stats: RunStats) -> None:
    print(f"source: {stats.source}")
    print(f"encoding: {stats.encoding}")
    print(f"columns: {', '.join(stats.header)}")
    print(f"rows: {stats.rows} (files: {stats.files}, dirs: {stats.dirs})")
    print(f"total file bytes: {stats.total_file_bytes}")
    if stats.has_allocated:
        print(f"allocated bytes: {stats.allocated_bytes}")
        print(f"unique allocated bytes: {stats.unique_allocated_bytes}")
    print(f"hardlink file rows: {stats.hardlink_files}")
    skipped = _skip_summary(stats)
    if skipped:
        print(f"skipped: {skipped}")
    print(f"duration: {stats.duration_s:.2f} s")
    print(f"rows/sec: {stats.rows_per_sec:.0f}")
    print(f"db: {stats.db_path} (schema v{db.SCHEMA_VERSION}, {stats.rows} entries)")


def main(argv: Sequence[str] | None = None) -> int:
    """Console-script entry point; returns the process exit code."""
    parser = build_parser()
    args = parser.parse_args(argv)
    handler: Callable[[argparse.Namespace], int] | None = getattr(args, "handler", None)
    if handler is None:
        # No subcommand: print help (scripting-friendly, exit 0).
        parser.print_help()
        return 0
    return handler(args)


if __name__ == "__main__":
    raise SystemExit(main())
