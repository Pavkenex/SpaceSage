"""Internal command-line interface (development, CI and automation only).

The product is the PySide6 desktop application; this CLI exists so the engine
can be driven headlessly by tests, scripts and CI. Engine subcommands
(``ingest``, ``stats``, ``plan``, ``apply``, ``undo``, ``ai``) are registered by
the slices that implement them -- see ``docs/slices.md``.
"""

from __future__ import annotations

import argparse
from collections.abc import Sequence

from spacesage import __version__

PROG = "spacesage"
DESCRIPTION = "Turn a WizTree export into a safety-gated course of action for a full disk."
EPILOG = "The product is the desktop app; this CLI is a development and automation surface."


def build_parser() -> argparse.ArgumentParser:
    """Build the top-level argument parser."""
    parser = argparse.ArgumentParser(
        prog=PROG,
        description=DESCRIPTION,
        epilog=EPILOG,
    )
    parser.add_argument("--version", action="version", version=f"{PROG} {__version__}")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    """Console-script entry point; returns the process exit code."""
    parser = build_parser()
    parser.parse_args(argv)
    # No subcommands exist yet: every invocation without --version prints help.
    parser.print_help()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
