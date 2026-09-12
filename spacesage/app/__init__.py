"""The SpaceSage desktop application (PySide6).

``python -m spacesage.app`` for a source checkout, ``spacesage-app`` when
installed.  The engine does the work; this package is the product surface: the
shell, the theme token set, the ranked Opportunities screen and the details
pane (design §9).
"""

from __future__ import annotations

from collections.abc import Sequence

__all__ = ["main"]


def main(argv: Sequence[str] | None = None) -> int:
    """Entry point of the desktop app (``spacesage.app:main``)."""
    from spacesage.app.main import run

    return run(argv)
