"""Argument-parser vocabulary shared by the engine CLI and the AI CLI.

``--kind`` and ``--min-size`` mean the same thing in both command trees, so the
two parsers share one definition instead of keeping a private copy each.  This
module is the only place a CLI argument type may know a domain module; keep it
free of anything else.
"""

from __future__ import annotations

import argparse

from spacesage import candidates, rules


def kind_list(value: str) -> tuple[str, ...]:
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


def size_arg(value: str) -> int:
    """Parse a ``--min-size`` value with :func:`spacesage.rules.parse_size`."""
    try:
        return rules.parse_size(value)
    except rules.RulesError as exc:
        raise argparse.ArgumentTypeError(str(exc)) from None
