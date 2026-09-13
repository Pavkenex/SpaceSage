"""Helpers shared by the AI test modules.

Deliberately *not* in ``tests/conftest.py``: that module is imported by pytest
under the bare name ``conftest``, and ``tests/gui/conftest.py`` claims that name
first when the whole suite runs, so ``from conftest import ...`` is a race.
"""

from __future__ import annotations

from spacesage.ai.prompts import ItemFacts


def fact(path: str, **overrides: object) -> ItemFacts:
    """An :class:`ItemFacts` for a path (test helper, not a fixture)."""
    values: dict[str, object] = {
        "is_dir": False,
        "size": 3 * 1024 * 1024,
        "ext": "msi",
        "age_days": 400,
    }
    values.update(overrides)
    return ItemFacts(path=path, **values)  # type: ignore[arg-type]
