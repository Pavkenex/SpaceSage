"""Internal command-line interface (development, CI and automation only).

The product is the PySide6 desktop application; this CLI exists so the engine
can be driven headlessly by tests, scripts and CI. Engine subcommands
(``ingest``, ``stats``, ``plan``, ``apply``, ``undo``, ``ai``) are registered by
the slices that implement them -- see ``docs/slices.md``.
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
import time
from collections.abc import Callable, Sequence
from pathlib import Path

from spacesage import __version__, candidates, db, deepscan, planner, rules, stats
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

    deepscan_parser = subparsers.add_parser(
        "deepscan",
        help="hash-verify exact duplicate files on the live filesystem (read-only)",
        description=(
            "Walk one or more roots and prove which files are byte-identical: "
            "group by size, hash the first 64 KiB, then read the full content of "
            "whatever is still ambiguous. Symlinks, junctions and every other "
            "reparse point are counted but never followed, and hard-linked paths "
            "are reported as one physical copy instead of free space. Prints the "
            "groups (members, reclaimable bytes, suggested keep policy and a "
            "same-volume hardlink-dedupe suggestion) biggest win first. Strictly "
            "read-only."
        ),
    )
    deepscan_parser.add_argument(
        "roots",
        metavar="ROOT",
        nargs="+",
        help="directory (or drive) to scan; links are never followed",
    )
    deepscan_parser.add_argument(
        "--min-size",
        type=_size_arg,
        default=deepscan.DEFAULT_MIN_SIZE,
        metavar="SIZE",
        help="ignore files smaller than this (bytes or '1 MiB'); default: %(default)s",
    )
    deepscan_parser.add_argument(
        "--top",
        type=int,
        default=deepscan.DEFAULT_TOP,
        metavar="N",
        help="groups listed (0 = every group; --json is always complete); default: %(default)s",
    )
    deepscan_parser.add_argument(
        "--json",
        action="store_true",
        help="emit the complete report as JSON (spacesage.deepscan/v1)",
    )
    deepscan_parser.add_argument(
        "--progress",
        action="store_true",
        help="print progress lines to stderr while walking and hashing",
    )
    deepscan_parser.set_defaults(handler=_run_deepscan)

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

    stats_parser = subparsers.add_parser(
        "stats",
        help="aggregate an index: top dirs/files, extensions, age, apps",
        description=(
            "Aggregate an ingested index (read-only): the largest directories and "
            "files, per-extension totals, age buckets and per-app footprints. Byte "
            "totals come from file rows only; folder rows are cross-checked and "
            "disagreements are reported as data-quality warnings."
        ),
    )
    stats_parser.add_argument(
        "--db",
        metavar="PATH",
        default=db.DEFAULT_DB_NAME,
        help=("index database file, or a directory (then PATH/spacesage.db); default: %(default)s"),
    )
    stats_parser.add_argument(
        "--by",
        choices=("dir", "ext", "age", "app"),
        default=None,
        help="print only this breakdown (default: all of them)",
    )
    stats_parser.add_argument(
        "--top",
        type=int,
        default=stats.DEFAULT_TOP,
        metavar="N",
        help="rows per ranked list (default: %(default)s)",
    )
    stats_parser.add_argument(
        "--json",
        action="store_true",
        help="emit the complete report as JSON (every view)",
    )
    stats_parser.add_argument(
        "--materialize",
        action="store_true",
        help="also rebuild the derived dir_sizes/app_footprints tables (writes to the index)",
    )
    stats_parser.set_defaults(handler=_run_stats)

    classify_parser = subparsers.add_parser(
        "classify",
        help="classify entries with the TOML rule packs (category, tier, action)",
        description=(
            "Classify every indexed entry with the rule packs (built-in packs "
            "plus user packs shadowing them by rule id). Prints per-category, "
            "per-tier and unknown counts and sizes; read-only unless "
            "--materialize is passed, which also writes the categories table."
        ),
    )
    classify_parser.add_argument(
        "--db",
        metavar="PATH",
        default=db.DEFAULT_DB_NAME,
        help=("index database file, or a directory (then PATH/spacesage.db); default: %(default)s"),
    )
    classify_parser.add_argument(
        "--rules",
        metavar="DIR",
        default=None,
        help="user rule-pack directory to load instead of ~/.config/spacesage/rules",
    )
    classify_parser.add_argument(
        "--list-rules",
        action="store_true",
        help="print the effective rule order (with --json: as JSON) and exit",
    )
    classify_parser.add_argument(
        "--top",
        type=int,
        default=rules.DEFAULT_TOP,
        metavar="N",
        help="rows per ranked list (default: %(default)s)",
    )
    classify_parser.add_argument(
        "--json",
        action="store_true",
        help="emit the complete report as JSON",
    )
    classify_parser.add_argument(
        "--materialize",
        action="store_true",
        help="also rebuild the derived categories table (writes to the index)",
    )
    classify_parser.set_defaults(handler=_run_classify)

    candidates_parser = subparsers.add_parser(
        "candidates",
        help="rank the opportunities per action kind (delete/move/stale/dupes/app)",
        description=(
            "Turn the classifier's verdicts into ranked candidate lists, biggest "
            "estimated win first: entries to quarantine, data to relocate, big "
            "cold entries to review, weak duplicate clusters and the largest app "
            "footprints. Every candidate carries its tier, confidence, the "
            "reason, and the score factors behind its rank. Read-only."
        ),
    )
    candidates_parser.add_argument(
        "--db",
        metavar="PATH",
        default=db.DEFAULT_DB_NAME,
        help=("index database file, or a directory (then PATH/spacesage.db); default: %(default)s"),
    )
    candidates_parser.add_argument(
        "--rules",
        metavar="DIR",
        default=None,
        help="user rule-pack directory to load instead of ~/.config/spacesage/rules",
    )
    candidates_parser.add_argument(
        "--kind",
        action="append",
        type=_kind_list,
        metavar="KIND",
        help=(
            f"only this candidate kind; repeat or comma-separate "
            f"({', '.join(candidates.KINDS)}); default: all. Kinds left out are "
            "not generated, so they cannot claim paths from the ones listed"
        ),
    )
    candidates_parser.add_argument(
        "--min-size",
        type=_size_arg,
        default=candidates.DEFAULT_MIN_SIZE,
        metavar="SIZE",
        help="ignore entries smaller than this (bytes or '100 MiB'); default: %(default)s",
    )
    candidates_parser.add_argument(
        "--top",
        type=int,
        default=candidates.DEFAULT_TOP,
        metavar="N",
        help="candidates listed per kind (0 = no limit); default: %(default)s",
    )
    candidates_parser.add_argument(
        "--stale-after-days",
        type=float,
        default=candidates.DEFAULT_STALE_DAYS,
        metavar="DAYS",
        help="age at which a big entry counts as stale; default: %(default)s",
    )
    candidates_parser.add_argument(
        "--dupes-min-copies",
        type=int,
        default=candidates.DEFAULT_DUPES_MIN_COPIES,
        metavar="N",
        help="same-name/same-size group size that counts as duplicates; default: %(default)s",
    )
    candidates_parser.add_argument(
        "--json",
        action="store_true",
        help="emit the complete report as JSON",
    )
    candidates_parser.set_defaults(handler=_run_candidates)

    plan_parser = subparsers.add_parser(
        "plan",
        help="compose the ranked candidates into plan.json v1 (the course of action)",
        description=(
            "Compose the ranked candidates into one ordered, itemized plan: T1/T2 "
            "quarantines first, then moves onto the target drives you pick (respecting "
            "each drive's free space minus reserve), then compressions, then review and "
            "native-tool items. Executable actions are only ever T1/T2 -- T3 is "
            "report-only -- and every move names its destination and the link that keeps "
            "the old path working. Prints the Markdown summary; --json prints plan.json, "
            "-o writes it to a file. Read-only."
        ),
    )
    plan_parser.add_argument(
        "--db",
        metavar="PATH",
        default=db.DEFAULT_DB_NAME,
        help=("index database file, or a directory (then PATH/spacesage.db); default: %(default)s"),
    )
    plan_parser.add_argument(
        "--rules",
        metavar="DIR",
        default=None,
        help="user rule-pack directory to load instead of ~/.config/spacesage/rules",
    )
    plan_parser.add_argument(
        "--to",
        action="append",
        type=str,
        default=[],
        metavar="DRIVE",
        help=(
            "target drive or path for moves, repeatable ('D:'; '/mnt/data'); without it "
            "every move candidate becomes a review item"
        ),
    )
    plan_parser.add_argument(
        "--reserve",
        type=_size_arg,
        default=planner.DEFAULT_RESERVE,
        metavar="SIZE",
        help="free space to keep untouched on every target; default: %(default)s",
    )
    plan_parser.add_argument(
        "--free",
        type=_size_arg,
        default=None,
        metavar="SIZE",
        help=(
            "assume this much free space on every target instead of measuring it "
            "(drives this machine cannot see, scripting and tests)"
        ),
    )
    plan_parser.add_argument(
        "--min-size",
        type=_size_arg,
        default=candidates.DEFAULT_MIN_SIZE,
        metavar="SIZE",
        help="ignore entries smaller than this (bytes or '100 MiB'); default: %(default)s",
    )
    plan_parser.add_argument(
        "--top",
        type=int,
        default=candidates.DEFAULT_TOP,
        metavar="N",
        help="candidates listed per kind before planning (0 = no limit); default: %(default)s",
    )
    plan_parser.add_argument(
        "--stale-after-days",
        type=float,
        default=candidates.DEFAULT_STALE_DAYS,
        metavar="DAYS",
        help="age at which a big entry counts as stale; default: %(default)s",
    )
    plan_parser.add_argument(
        "--dupes-min-copies",
        type=int,
        default=candidates.DEFAULT_DUPES_MIN_COPIES,
        metavar="N",
        help="same-name/same-size group size that counts as duplicates; default: %(default)s",
    )
    plan_parser.add_argument(
        "--no-links",
        action="store_true",
        help="plan moves without linking the original path back (link_after NONE)",
    )
    plan_parser.add_argument(
        "--json",
        action="store_true",
        help="print plan.json (spacesage.plan/v1) instead of the Markdown summary",
    )
    plan_parser.add_argument(
        "-o",
        "--output",
        metavar="FILE",
        type=Path,
        default=None,
        help="write plan.json to FILE ('-' writes it next to stdout as well)",
    )
    plan_parser.set_defaults(handler=_run_plan)
    return parser


def _kind_list(value: str) -> tuple[str, ...]:
    """Parse a ``--kind`` value: one kind, or a comma-separated list of them."""
    parts = tuple(part.strip() for part in value.split(",") if part.strip())
    if not parts:
        raise argparse.ArgumentTypeError("expected at least one kind")
    unknown = [part for part in parts if part not in candidates.KINDS]
    if unknown:
        raise argparse.ArgumentTypeError(
            f"unknown kind(s) {', '.join(unknown)}; pick from {', '.join(candidates.KINDS)}"
        )
    return parts


def _size_arg(value: str) -> int:
    """Parse a ``--min-size`` value with :func:`spacesage.rules.parse_size`."""
    try:
        return rules.parse_size(value)
    except rules.RulesError as exc:
        raise argparse.ArgumentTypeError(str(exc)) from None


class _DeepScanProgressPrinter:
    """Progress callback used by ``deepscan --progress`` (one line per event)."""

    def __call__(self, update: deepscan.ScanProgress) -> None:
        print(
            f"progress: {update.stage}: {update.files} files, "
            f"{stats.format_bytes(update.bytes)} scanned, {update.candidates} candidates, "
            f"{update.hashed} hashed ({stats.format_bytes(update.bytes_read)} read), "
            f"{update.elapsed_s:.1f}s",
            file=sys.stderr,
            flush=True,
        )


def _run_deepscan(args: argparse.Namespace) -> int:
    progress = _DeepScanProgressPrinter() if args.progress else None
    try:
        report = deepscan.scan(args.roots, min_size=args.min_size, progress=progress)
    except deepscan.DeepScanError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    try:
        rendered = (
            deepscan.render_json(report)
            if args.json
            else deepscan.render_text(report, top=args.top)
        )
    except deepscan.DeepScanError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    print(rendered, end="")
    return 0


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


def _run_stats(args: argparse.Namespace) -> int:
    target = db.resolve_db_path(args.db)
    if not target.is_file():
        print(
            f"error: no index at {target}; ingest a WizTree export first "
            f"(spacesage ingest <csv> --db {args.db})",
            file=sys.stderr,
        )
        return 1
    try:
        conn = db.open_db(target)
    except (db.SchemaError, sqlite3.Error) as exc:
        print(f"error: cannot open the index at {target}: {exc}", file=sys.stderr)
        return 1
    try:
        try:
            if args.materialize:
                derived = stats.build_derived(conn)
                print(
                    f"derived: {derived.dir_sizes} dir_sizes rows, "
                    f"{derived.app_footprints} app_footprints rows",
                    file=sys.stderr,
                )
            report = stats.stats_report(conn, top=args.top, db_path=str(target))
        except (stats.StatsError, db.SchemaError, sqlite3.Error) as exc:
            print(f"error: {exc}", file=sys.stderr)
            return 1
        print(
            stats.render_json(report) if args.json else stats.render_text(report, by=args.by),
            end="",
        )
        return 0
    finally:
        conn.close()


def _load_ruleset(args: argparse.Namespace) -> rules.RuleSet:
    """Load the effective rule set; a bad --rules path is a hard error."""
    return rules.load_rules(user_dir=Path(args.rules) if args.rules else None)


def _run_classify(args: argparse.Namespace) -> int:
    try:
        ruleset = _load_ruleset(args)
    except rules.RulesError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1

    if args.list_rules:
        print(
            json.dumps(ruleset.to_dict(), indent=2) + "\n"
            if args.json
            else rules.render_rules(ruleset),
            end="",
        )
        return 0

    target = db.resolve_db_path(args.db)
    if not target.is_file():
        print(
            f"error: no index at {target}; ingest a WizTree export first "
            f"(spacesage ingest <csv> --db {args.db})",
            file=sys.stderr,
        )
        return 1
    try:
        conn = db.open_db(target)
    except (db.SchemaError, sqlite3.Error) as exc:
        print(f"error: cannot open the index at {target}: {exc}", file=sys.stderr)
        return 1
    try:
        try:
            # One reference point for the whole invocation: the materialised
            # rows and the printed report must agree on age-based rules.
            moment = time.time()
            if args.materialize:
                built = rules.build_categories(conn, ruleset, now=moment)
                print(
                    f"derived: {built.entries} categories rows "
                    f"({built.matched} matched, {built.unknown} unknown), "
                    f"rules {built.rules_sha256[:12]}",
                    file=sys.stderr,
                )
            report = rules.classify_report(
                conn, ruleset, top=args.top, now=moment, db_path=str(target)
            )
        except (rules.RulesError, db.SchemaError, sqlite3.Error) as exc:
            print(f"error: {exc}", file=sys.stderr)
            return 1
        print(
            rules.render_json(report) if args.json else rules.render_text(report),
            end="",
        )
        return 0
    finally:
        conn.close()


def _run_candidates(args: argparse.Namespace) -> int:
    try:
        ruleset = _load_ruleset(args)
    except rules.RulesError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1

    target = db.resolve_db_path(args.db)
    if not target.is_file():
        print(
            f"error: no index at {target}; ingest a WizTree export first "
            f"(spacesage ingest <csv> --db {args.db})",
            file=sys.stderr,
        )
        return 1
    try:
        conn = db.open_db(target)
    except (db.SchemaError, sqlite3.Error) as exc:
        print(f"error: cannot open the index at {target}: {exc}", file=sys.stderr)
        return 1
    try:
        selected = tuple(kind for group in (args.kind or ()) for kind in group) or candidates.KINDS
        try:
            # One reference point for the whole invocation: age filters and the
            # printed recency factors must agree.
            moment = time.time()
            report = candidates.candidate_report(
                conn,
                ruleset,
                kinds=selected,
                min_size=args.min_size,
                top=args.top,
                now=moment,
                stale_after_days=args.stale_after_days,
                dupes_min_copies=args.dupes_min_copies,
                db_path=str(target),
            )
        except (candidates.CandidatesError, rules.RulesError, db.SchemaError, sqlite3.Error) as exc:
            print(f"error: {exc}", file=sys.stderr)
            return 1
        print(
            candidates.render_json(report) if args.json else candidates.render_text(report),
            end="",
        )
        return 0
    finally:
        conn.close()


def _run_plan(args: argparse.Namespace) -> int:
    try:
        ruleset = _load_ruleset(args)
    except rules.RulesError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1

    try:
        targets = [
            planner.target_from_spec(spec, reserve=args.reserve, free=args.free) for spec in args.to
        ]
    except planner.PlannerError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1

    target = db.resolve_db_path(args.db)
    if not target.is_file():
        print(
            f"error: no index at {target}; ingest a WizTree export first "
            f"(spacesage ingest <csv> --db {args.db})",
            file=sys.stderr,
        )
        return 1
    try:
        conn = db.open_db(target)
    except (db.SchemaError, sqlite3.Error) as exc:
        print(f"error: cannot open the index at {target}: {exc}", file=sys.stderr)
        return 1
    try:
        if not targets:
            print(
                "note: no target drive selected (--to); move candidates are planned as "
                "review items",
                file=sys.stderr,
            )
        try:
            # One reference point for the whole invocation: the ranked candidates
            # and the plan's ages must agree.
            moment = time.time()
            plan = planner.build_plan(
                conn,
                ruleset,
                targets=targets,
                min_size=args.min_size,
                top=args.top,
                now=moment,
                stale_after_days=args.stale_after_days,
                dupes_min_copies=args.dupes_min_copies,
                links=not args.no_links,
                db_path=str(target),
            )
        except (
            planner.PlannerError,
            candidates.CandidatesError,
            rules.RulesError,
            db.SchemaError,
            sqlite3.Error,
        ) as exc:
            print(f"error: {exc}", file=sys.stderr)
            return 1
        if args.output is not None:
            try:
                args.output.parent.mkdir(parents=True, exist_ok=True)
                args.output.write_text(planner.render_json(plan), encoding="utf-8")
            except OSError as exc:
                print(f"error: cannot write {args.output}: {exc}", file=sys.stderr)
                return 1
            print(f"wrote {args.output}", file=sys.stderr)
        print(
            planner.render_json(plan) if args.json else planner.render_markdown(plan),
            end="",
        )
        return 0
    finally:
        conn.close()


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
