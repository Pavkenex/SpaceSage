"""``spacesage ai`` - the AI layer's internal command surface.

The product is the desktop app; this exists so the layer can be driven by tests,
scripts and CI without Qt (design.md §10, docs/ai.md).  Every subcommand is
read-only and prints what the engine returns; nothing here executes a file
operation, and the AI never decides anything on its own:

``spacesage ai status``   configuration, cache and mode flags (no network).
``spacesage ai check``    one round trip to the provider: models, latency, errors.
``spacesage ai models``   the provider's model list (the settings picker's data).
``spacesage ai suggest``  fill suggestions for the undecided rows of an index.
``spacesage ai explain``  stream a deep explanation for a selection of paths.
``spacesage ai review``   severity-tagged risk annotations for a plan.json.
``spacesage ai cache``    what the answer cache holds, or ``--clear`` it.
"""

from __future__ import annotations

import argparse
import functools
import json
import os
import sqlite3
import sys
import time
from collections.abc import Callable, Mapping, Sequence
from typing import Any

from spacesage import candidates, db, executor, opportunities, rules, stats
from spacesage.ai import (
    AIConfig,
    AIEngine,
    AIError,
    BatchProgress,
    ItemFacts,
    facts_from_opportunity,
)

DEFAULT_SUGGEST_TOP = 40
"""Rows ``ai suggest`` fills when the caller does not say (one fill per row max)."""

SEVERITY_ORDER: Mapping[str, int] = {"danger": 0, "warning": 1, "info": 2}
"""Review annotations are printed most severe first."""


# --------------------------------------------------------------------------- #
# Registration
# --------------------------------------------------------------------------- #


def add_ai_commands(subparsers: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:
    """Register the ``ai`` command and its subcommands on the top-level parser."""
    parser = subparsers.add_parser(
        "ai",
        help="optional AI assist: suggestions, explanations, plan review",
        description=(
            "The AI layer is off until a provider is configured (docs/ai.md). "
            "It only ever suggests: nothing here executes, approves or plans "
            "anything, and every answer is schema-validated and locked to the "
            "data that was sent before it is shown."
        ),
    )
    subparsers_ai = parser.add_subparsers(dest="ai_command", metavar="<command>")

    check = subparsers_ai.add_parser(
        "check",
        help="one round trip to the provider: models, latency, actionable errors",
        description=(
            "Read GET {base_url}/models and report what a real call would hit: "
            "the provider, the model, the latency, and - on failure - the coded "
            "reason and its fix (auth, model_missing, unreachable, ...)."
        ),
    )
    _add_engine_options(check)
    check.add_argument("--json", action="store_true", help="emit the check result as JSON")
    check.set_defaults(handler=_run_check)

    models = subparsers_ai.add_parser(
        "models",
        help="list the provider's models (the settings picker's data)",
        description="GET {base_url}/models for the selected provider.",
    )
    _add_engine_options(models)
    models.add_argument("--json", action="store_true", help="emit the model list as JSON")
    models.set_defaults(handler=_run_models)

    status = subparsers_ai.add_parser(
        "status",
        help="configuration, cache and mode flags (no network)",
        description=(
            "What the AI layer is set to right now: provider, model, streaming, "
            "redact_paths, local-only, cache location and warnings. Never calls "
            "the network."
        ),
    )
    _add_engine_options(status)
    status.add_argument("--json", action="store_true", help="emit the status as JSON")
    status.set_defaults(handler=_run_status)

    suggest = subparsers_ai.add_parser(
        "suggest",
        help="fill solution suggestions for the undecided rows of an index",
        description=(
            "Read the ranked Opportunities list from the index and ask the model "
            "for a course of action on the rows the rules could not decide "
            "(stale entries, unverified duplicates, unmatched paths). Answers are "
            "schema-validated, locked to the rows that were sent, cached and "
            "metered. Read-only: no plan is written and nothing is executed."
        ),
    )
    suggest.add_argument(
        "--db",
        metavar="PATH",
        default=db.DEFAULT_DB_NAME,
        help="index database file, or a directory (then PATH/spacesage.db); default: %(default)s",
    )
    suggest.add_argument(
        "--rules",
        metavar="DIR",
        default=None,
        help="user rule-pack directory to load instead of ~/.config/spacesage/rules",
    )
    suggest.add_argument(
        "--kind",
        action="append",
        type=_kind_list,
        metavar="KIND",
        help=(
            "only rows of this candidate kind; repeat or comma-separate "
            f"({', '.join(candidates.KINDS)}); default: all"
        ),
    )
    suggest.add_argument(
        "--min-size",
        type=_size_arg,
        default=candidates.DEFAULT_MIN_SIZE,
        metavar="SIZE",
        help="ignore entries smaller than this (bytes or '100 MiB'); default: %(default)s",
    )
    suggest.add_argument(
        "--top",
        type=int,
        default=DEFAULT_SUGGEST_TOP,
        metavar="N",
        help="rows to fill at most (0 = every undecided row); default: %(default)s",
    )
    suggest.add_argument(
        "--all",
        action="store_true",
        help="also fill rows the rules already decided (default: undecided rows only)",
    )
    suggest.add_argument(
        "--batch-size",
        type=int,
        default=None,
        metavar="N",
        help="items per request; default: the configured batch_size",
    )
    suggest.add_argument(
        "--max-items",
        type=int,
        default=None,
        metavar="N",
        help="hard cap on the run; default: the configured max_items",
    )
    suggest.add_argument(
        "--no-cache",
        action="store_true",
        help="ignore and do not write cached answers (costs a call every time)",
    )
    suggest.add_argument(
        "--no-progress",
        action="store_true",
        help="do not print per-batch progress to stderr",
    )
    suggest.add_argument("--json", action="store_true", help="emit the run and its answers as JSON")
    suggest.set_defaults(handler=_run_suggest)

    explain = subparsers_ai.add_parser(
        "explain",
        help="stream a deep explanation for a selection of paths",
        description=(
            "One bounded call for a selection: why these entries are what they "
            "are, what the rules saw, what the risks are and what to do next. "
            "Streamed to stdout as it is written."
        ),
    )
    explain.add_argument(
        "--path",
        action="append",
        required=True,
        metavar="PATH",
        dest="paths",
        help="a selected path (repeat for a selection)",
    )
    explain.add_argument(
        "--db",
        metavar="PATH",
        default=None,
        help="index to take the facts from (optional; a directory means PATH/spacesage.db)",
    )
    explain.add_argument(
        "--rules",
        metavar="DIR",
        default=None,
        help="user rule-pack directory to load instead of ~/.config/spacesage/rules",
    )
    explain.add_argument(
        "--no-cache",
        action="store_true",
        help="ignore and do not write cached answers",
    )
    explain.add_argument("--json", action="store_true", help="emit the finished answer as JSON")
    _add_engine_options(explain)
    explain.set_defaults(handler=_run_explain)

    review = subparsers_ai.add_parser(
        "review",
        help="severity-tagged risk annotations for a plan.json",
        description=(
            "Read a plan.json produced by `spacesage plan` and return "
            "info/warning/danger annotations keyed by action id: what could go "
            "wrong, what is irreversible, what needs a closer look first. The AI "
            "annotates the plan; it never changes it."
        ),
    )
    review.add_argument(
        "--plan",
        required=True,
        metavar="PATH",
        help="plan.json to review",
    )
    review.add_argument("--json", action="store_true", help="emit the annotations as JSON")
    _add_engine_options(review)
    review.set_defaults(handler=_run_review)

    cache = subparsers_ai.add_parser(
        "cache",
        help="what the answer cache holds, or --clear it",
        description=(
            "The cache is keyed by sha256(use case + version + provider + model + "
            "payload + dataset), so a repeated batch fill costs nothing. Entries "
            "hold answers and, in redact_paths mode, the local token map."
        ),
    )
    _add_engine_options(cache)
    cache.add_argument("--clear", action="store_true", help="delete every cached answer")
    cache.add_argument("--json", action="store_true", help="emit the cache stats as JSON")
    cache.set_defaults(handler=_run_cache)

    parser.set_defaults(handler=_run_help, ai_parser=parser)


def _add_engine_options(parser: argparse.ArgumentParser) -> None:
    """The provider overrides every ``ai`` subcommand accepts."""
    parser.add_argument(
        "--provider",
        metavar="NAME",
        default=None,
        help="provider to use instead of the configured default",
    )
    parser.add_argument(
        "--model",
        metavar="MODEL",
        default=None,
        help="model to call instead of the configured one",
    )
    parser.add_argument(
        "--base-url",
        metavar="URL",
        default=None,
        help="endpoint to call instead of the configured one",
    )
    parser.add_argument(
        "--local-only",
        action="store_true",
        help="refuse any non-loopback endpoint for this invocation",
    )
    parser.add_argument(
        "--redact-paths",
        action="store_true",
        help="send path tokens instead of real paths (answers are mapped back locally)",
    )


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


# --------------------------------------------------------------------------- #
# Dispatch
# --------------------------------------------------------------------------- #


def _run_help(args: argparse.Namespace) -> int:
    """``spacesage ai`` with no subcommand: print its help."""
    parser = getattr(args, "ai_parser", None)
    if parser is not None:
        parser.print_help()
    return 0


def _engine(args: argparse.Namespace, *, cache: bool = True) -> AIEngine:
    """Build the engine for one invocation from the config plus the flags."""
    config = AIConfig.load().with_overrides(
        provider=getattr(args, "provider", None),
        model=getattr(args, "model", None),
        base_url=getattr(args, "base_url", None),
        local_only=True if getattr(args, "local_only", False) else None,
        redact_paths=True if getattr(args, "redact_paths", False) else None,
    )
    if not cache:
        config = config.with_settings(cache=False)
    return AIEngine(config)


def _fail(exc: AIError) -> int:
    """Print a coded failure the way the other commands do, and exit 1."""
    print(f"error: {exc}", file=sys.stderr)
    return 1


def _guarded(
    handler: Callable[[argparse.Namespace], int],
) -> Callable[[argparse.Namespace], int]:
    """A command that turns a coded :class:`AIError` into a plain CLI error.

    A broken config file, an unreachable provider or a refused answer are
    expected failures: they print ``error: <code>: ...`` (with the fix in the
    hint) and exit 1, never a traceback.
    """

    @functools.wraps(handler)
    def run(args: argparse.Namespace) -> int:
        try:
            return handler(args)
        except AIError as exc:
            return _fail(exc)

    return run


class _BatchProgressPrinter:
    """Progress callback used by ``ai suggest`` (one line per batch)."""

    def __call__(self, update: BatchProgress) -> None:
        parts = [
            f"{update.batches_done}/{update.batches_total} batches",
            f"{update.done}/{update.total} items",
            f"{update.calls} calls",
        ]
        if update.cache_hits:
            parts.append(f"{update.cache_hits} cached")
        if update.failures:
            parts.append(f"{update.failures} failed")
        if update.rejected:
            parts.append(f"{update.rejected} rejected")
        if update.cost_usd is not None:
            parts.append(f"${update.cost_usd:.4f}")
        if update.message:
            parts.append(update.message)
        print("progress: " + ", ".join(parts), file=sys.stderr, flush=True)


# --------------------------------------------------------------------------- #
# check / models / status / cache
# --------------------------------------------------------------------------- #


@_guarded
def _run_check(args: argparse.Namespace) -> int:
    engine = _engine(args)
    result = engine.check(model=args.model)
    if args.json:
        print(json.dumps(result.to_dict(), indent=2, sort_keys=True))
        return 0 if result.ok else 1
    if not result.ok:
        return _fail(result.error) if result.error is not None else 1
    latency = f"{result.latency_s * 1000:.0f} ms" if result.latency_s is not None else "n/a"
    print(f"provider  {result.provider}")
    print(f"base url  {result.base_url}")
    print(f"model     {result.model}")
    print(f"latency   {latency}")
    print(f"models    {len(result.models)} listed")
    for warning in result.warnings:
        print(f"warning: {warning}", file=sys.stderr)
    if result.hint:
        print(f"note: {result.hint}", file=sys.stderr)
    return 0


@_guarded
def _run_models(args: argparse.Namespace) -> int:
    engine = _engine(args)
    try:
        models = engine.models()
    except AIError as exc:
        return _fail(exc)
    if args.json:
        print(json.dumps([info.to_dict() for info in models], indent=2, sort_keys=True))
        return 0
    if not models:
        print("no models listed by the provider", file=sys.stderr)
        return 0
    width = max(len(info.id) for info in models)
    for info in models:
        owner = info.owned_by or "-"
        print(f"{info.id:<{width}}  {owner}")
    return 0


@_guarded
def _run_status(args: argparse.Namespace) -> int:
    engine = _engine(args)
    status = engine.status()
    if args.json:
        print(json.dumps(status.to_dict(), indent=2, sort_keys=True))
        return 0
    state = "ready" if status.ready else ("off" if not status.enabled else "not ready")
    print(f"state     {state}")
    if status.reason:
        print(f"reason    {status.reason}")
    print(f"provider  {status.provider or '-'} ({status.provider_title or '-'})")
    print(f"model     {status.model or '-'}")
    print(f"base url  {status.base_url or '-'}")
    print(f"key       {'present' if status.key_present else 'absent'}")
    print(f"local     {'yes' if status.local else 'no'}")
    print(
        f"modes     streaming={'on' if status.streaming else 'off'} "
        f"redact_paths={'on' if status.redact_paths else 'off'} "
        f"local_only={'on' if status.local_only else 'off'}"
    )
    print(
        f"cache     {'on' if status.cache_enabled else 'off'} "
        f"({status.cache_entries} entries, {status.cache_dir})"
    )
    print(f"pricing   {'known' if status.pricing_known else 'unknown'}")
    if status.config_path:
        print(f"config    {status.config_path}")
    for warning in status.warnings:
        print(f"warning: {warning}", file=sys.stderr)
    return 0


@_guarded
def _run_cache(args: argparse.Namespace) -> int:
    engine = _engine(args)
    if args.clear:
        removed = engine.clear_cache()
        print(f"cleared {removed} cached answer(s)")
        return 0
    cache = engine.cache_stats()
    meter = engine.meter_snapshot()
    if args.json:
        print(
            json.dumps(
                {"cache": cache.to_dict(), "meter": meter.to_dict()}, indent=2, sort_keys=True
            )
        )
        return 0
    print(f"entries   {cache.entries}")
    print(f"hits      {cache.hits}")
    print(f"misses    {cache.misses}")
    print(f"size      {stats.format_bytes(cache.bytes)}")
    print(f"directory {engine.cache.directory}")
    print(
        f"tokens    {meter.prompt_tokens} in / {meter.completion_tokens} out "
        f"({meter.calls} calls, {meter.cache_hits} cached)"
    )
    if meter.cost_usd is not None:
        print(f"cost      ${meter.cost_usd:.4f}")
    return 0


# --------------------------------------------------------------------------- #
# suggest / explain / review
# --------------------------------------------------------------------------- #


def _load_ruleset(args: argparse.Namespace) -> rules.RuleSet:
    return rules.load_rules(user_dir=getattr(args, "rules", None))


def _open_index(args: argparse.Namespace) -> tuple[sqlite3.Connection | None, int]:
    """Open ``--db`` when it exists; ``(None, 1)`` with a printed error otherwise."""
    raw = getattr(args, "db", None)
    if raw is None:
        return None, 0
    target = db.resolve_db_path(raw)
    if not target.is_file():
        print(
            f"error: no index at {target}; ingest a WizTree export first "
            f"(spacesage ingest <csv> --db {raw})",
            file=sys.stderr,
        )
        return None, 1
    try:
        return db.open_db(target), 0
    except (db.SchemaError, sqlite3.Error) as exc:
        print(f"error: cannot open the index at {target}: {exc}", file=sys.stderr)
        return None, 1


@_guarded
def _run_suggest(args: argparse.Namespace) -> int:
    conn, code = _open_index(args)
    if code:
        return code
    assert conn is not None
    try:
        try:
            ruleset = _load_ruleset(args)
        except rules.RulesError as exc:
            print(f"error: {exc}", file=sys.stderr)
            return 1
        moment = time.time()
        try:
            listing = opportunities.build_opportunities(
                conn,
                ruleset,
                kinds=tuple(kind for group in (args.kind or ()) for kind in group)
                or candidates.KINDS,
                min_size=args.min_size,
                now=moment,
                db_path=str(db.resolve_db_path(args.db)),
            )
        except (opportunities.OpportunitiesError, candidates.CandidatesError, sqlite3.Error) as exc:
            print(f"error: {exc}", file=sys.stderr)
            return 1
        wanted = tuple(
            row for row in listing.rows if args.all or row.state == opportunities.STATE_UNDECIDED
        )
        if args.top:
            wanted = wanted[: args.top]
        if not wanted:
            print("nothing to fill: every row the rules could reach is already decided")
            return 0
        facts = tuple(facts_from_opportunity(row) for row in wanted)
        engine = _engine(args, cache=not args.no_cache)
        progress = None if args.no_progress else _BatchProgressPrinter()
        outcome = engine.suggest(
            facts,
            batch_size=args.batch_size,
            max_items=args.max_items,
            on_progress=progress,
            use_cache=not args.no_cache,
        )
        if args.json:
            print(json.dumps(_suggest_json(outcome, facts), indent=2, sort_keys=True))
        else:
            _print_suggestions(outcome, facts)
        if outcome.error is not None and not outcome.ok:
            print(f"error: {outcome.error}", file=sys.stderr)
            return 1
        return 0
    finally:
        conn.close()


def _suggest_json(outcome: Any, facts: Sequence[ItemFacts]) -> dict[str, Any]:
    """The JSON body of ``ai suggest`` (answers plus the run's accounting)."""
    solved = {item.key(): item for item in facts}
    answers = []
    for key, suggestion in outcome.by_path.items():
        row = solved.get(key)
        answers.append(
            {
                **suggestion.to_dict(),
                "path": row.path if row is not None else suggestion.path,
                "size": row.size if row is not None else None,
            }
        )
    return {
        "schema": "spacesage.ai.suggest/v1",
        "use_case": outcome.use_case,
        "ok": outcome.ok,
        "provider": outcome.provider,
        "model": outcome.model,
        "answers": answers,
        "rejected": list(outcome.rejected),
        "missing": list(outcome.missing),
        "batches": outcome.batches,
        "calls": outcome.calls,
        "cache_hits": outcome.cache_hits,
        "failures": outcome.failures,
        "cancelled": outcome.cancelled,
        "usage": outcome.usage.to_dict(),
        "cost_usd": outcome.cost_usd,
        "latency_s": outcome.latency_s,
        "estimate": None if outcome.estimate is None else outcome.estimate.to_dict(),
        "error": None if outcome.error is None else outcome.error.to_dict(),
    }


def _print_suggestions(outcome: Any, facts: Sequence[ItemFacts]) -> None:
    """The human view: one line per answer, then the run's accounting."""
    solved = {item.key(): item for item in facts}
    if not outcome.by_path:
        print("the model returned no usable suggestion", file=sys.stderr)
    for key, suggestion in outcome.by_path.items():
        row = solved.get(key)
        size = stats.format_bytes(row.size) if row is not None else "-"
        path = row.path if row is not None else suggestion.path
        print(f"{suggestion.action:<18} {suggestion.confidence:>5.2f}  {size:>10}  {path}")
        print(f"    {suggestion.why}")
        if suggestion.side_effects:
            print(f"    side effects: {suggestion.side_effects}")
        if suggestion.alternatives:
            print(f"    alternatives: {', '.join(suggestion.alternatives)}")
        if suggestion.native:
            print(f"    vendor tool: {suggestion.native}")
    if outcome.rejected:
        print(f"rejected (not sent): {len(outcome.rejected)}", file=sys.stderr)
    if outcome.missing:
        print(f"no answer for: {len(outcome.missing)} of {len(facts)}", file=sys.stderr)
    if outcome.failures:
        print(f"failed batches: {outcome.failures}", file=sys.stderr)
    estimate = "" if outcome.estimate is None else f", estimate {outcome.estimate.render()}"
    print(
        f"\n{len(outcome.by_path)} answer(s) from {outcome.calls} call(s) "
        f"({outcome.cache_hits} cached), {outcome.usage.total_tokens} tokens, "
        f"{outcome.latency_s:.1f}s{estimate}",
        file=sys.stderr,
    )


@_guarded
def _run_explain(args: argparse.Namespace) -> int:
    facts, code = _facts_for_paths(args)
    if code:
        return code
    engine = _engine(args, cache=not args.no_cache)
    emitted = 0

    def emit(text: str) -> None:
        nonlocal emitted
        emitted += len(text)
        print(text, end="", flush=True)

    outcome = engine.explain(facts, on_delta=emit, use_cache=not args.no_cache)
    if emitted:
        print()
    if args.json:
        print(json.dumps(outcome.to_dict(), indent=2, sort_keys=True))
        return 0 if outcome.ok else 1
    if not outcome.ok:
        return _fail(outcome.error) if outcome.error is not None else 1
    if outcome.explanation is not None:
        if not emitted:
            print(outcome.explanation.explanation)
        if outcome.explanation.title and not emitted:
            print(f"\n{outcome.explanation.title}", file=sys.stderr)
        risks = outcome.explanation.risks
        if risks:
            print("\nrisks:")
            for risk in risks:
                print(f"  - {risk}")
        if outcome.explanation.alternatives:
            print("\nalternatives:")
            for alternative in outcome.explanation.alternatives:
                print(f"  - {alternative}")
    print(
        f"\n{outcome.usage.total_tokens} tokens, {outcome.latency_s:.1f}s"
        + (f", ${outcome.cost_usd:.4f}" if outcome.cost_usd is not None else ""),
        file=sys.stderr,
    )
    return 0


def _facts_for_paths(args: argparse.Namespace) -> tuple[tuple[ItemFacts, ...], int]:
    """Item facts for ``--path`` values: from the index when it holds them."""
    paths = tuple(args.paths)  # argparse guarantees at least one
    rows: dict[str, opportunities.Opportunity] = {}
    conn, code = _open_index(args)
    if code:
        return (), code
    if conn is not None:
        try:
            ruleset = _load_ruleset(args)
            listing = opportunities.build_opportunities(
                conn, ruleset, db_path=str(db.resolve_db_path(args.db))
            )
            rows = {row.key: row for row in listing.rows}
        except (rules.RulesError, opportunities.OpportunitiesError, sqlite3.Error) as exc:
            print(
                f"note: reading the index did not work out ({exc}); using bare facts",
                file=sys.stderr,
            )
        finally:
            conn.close()
    facts: list[ItemFacts] = []
    for path in paths:
        row = rows.get(opportunities.path_key(path))
        if row is not None:
            facts.append(facts_from_opportunity(row))
            continue
        facts.append(_bare_facts(path))
    return tuple(facts), 0


def _bare_facts(path: str) -> ItemFacts:
    """Facts for a path the index does not hold: whatever the filesystem says."""
    is_dir = path.endswith(("/", "\\"))
    size = 0
    age_days = None
    try:
        info = os.stat(path)
        is_dir = os.path.isdir(path)
        size = info.st_size
        age_days = max(0, int((time.time() - info.st_mtime) // 86_400))
    except OSError:
        pass
    return ItemFacts(path=path, is_dir=is_dir, size=size, ext=_ext_of(path), age_days=age_days)


def _ext_of(path: str) -> str | None:
    """Lower-case extension without the dot (``None`` when there is none)."""
    name = path.replace("\\", "/").rsplit("/", 1)[-1]
    if "." not in name[1:]:
        return None
    ext = name.rsplit(".", 1)[-1].lower()
    return ext or None


def _plan_actions(plan: Mapping[str, Any]) -> dict[str, Mapping[str, Any]]:
    """The plan's actions by id, for looking up what an annotation refers to."""
    raw = plan.get("actions")
    if not isinstance(raw, Sequence) or isinstance(raw, (str, bytes)):
        return {}
    actions: dict[str, Mapping[str, Any]] = {}
    for action in raw:
        if isinstance(action, Mapping) and action.get("id") is not None:
            actions[str(action["id"])] = action
    return actions


@_guarded
def _run_review(args: argparse.Namespace) -> int:
    try:
        plan = executor.load_plan(args.plan)
    except executor.ExecutorError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    engine = _engine(args)
    outcome = engine.review(plan, plan_path=args.plan)
    if args.json:
        print(json.dumps(outcome.to_dict(), indent=2, sort_keys=True))
        return 0 if outcome.ok else 1
    if not outcome.ok:
        return _fail(outcome.error) if outcome.error is not None else 1
    actions = _plan_actions(plan)
    if outcome.summary:
        print(outcome.summary)
        print()
    ordered = sorted(
        outcome.annotations,
        key=lambda item: (SEVERITY_ORDER.get(item.severity, 9), item.action_id),
    )
    if not ordered:
        print("no annotations: the model found nothing to flag", file=sys.stderr)
    for annotation in ordered:
        action = actions.get(annotation.action_id, {})
        action_type = str(action.get("type", "?"))
        path = str(action.get("path", "?"))
        print(f"[{annotation.severity}] {annotation.action_id} ({action_type}) {path}")
        print(f"    {annotation.title}: {annotation.detail}")
        if annotation.recommendation:
            print(f"    recommendation: {annotation.recommendation}")
    if outcome.rejected:
        print(f"rejected annotations (not in the plan): {outcome.rejected}", file=sys.stderr)
    print(
        f"\n{len(ordered)} annotation(s), {outcome.usage.total_tokens} tokens, "
        f"{outcome.latency_s:.1f}s"
        + (f", ${outcome.cost_usd:.4f}" if outcome.cost_usd is not None else ""),
        file=sys.stderr,
    )
    return 0


def run_ai(args: argparse.Namespace) -> int:
    """Entry point registered on the ``ai`` parser (kept for direct dispatch)."""
    handler = getattr(args, "handler", None)
    if handler is None or handler is run_ai:
        return _run_help(args)
    return int(handler(args))


__all__ = ["add_ai_commands", "run_ai"]
