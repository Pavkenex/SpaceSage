"""Small helpers shared across the engine, the AI layer and the executor.

Each of these used to be copied into every module that needed one (four
``_iso``s, five ``_plural``s, two TOML escapers), so a fix had to be made in
five places or it silently drifted.  One home keeps them together.

The module is stdlib-only, like everything below ``spacesage.app``: it must not
import a domain module, or it stops being importable from all of them.
"""

from __future__ import annotations

import json
import sys
from datetime import UTC, datetime

PLATFORM: str = sys.platform
"""``sys.platform``, widened to ``str`` so mypy cannot fold the per-OS branches.

``mypy --warn-unreachable`` knows the literal value of ``sys.platform``: on
Windows it sees the ``win32`` branch return first and reports every later
``darwin`` check as dead code.  A plain ``str`` keeps every branch reachable on
every platform.
"""


def iso_utc(value: datetime | int | float) -> str:
    """A UTC timestamp at seconds precision.

    Datetimes are normalised to UTC; an epoch number is converted first, so the
    same call serves a model's ``datetime`` and a stored integer column.
    """
    if isinstance(value, (int, float)):
        value = datetime.fromtimestamp(value, tz=UTC)
    return value.astimezone(UTC).isoformat(timespec="seconds")


def plural(count: int, singular: str, plural: str | None = None) -> str:
    """``1 file`` / ``3 files``; pass ``plural`` for irregulars (``copy``/``copies``)."""
    return f"{count} {singular if count == 1 else (plural or singular + 's')}"


def toml_string(value: str) -> str:
    """A TOML basic string (JSON escaping is a valid subset for our values)."""
    return json.dumps(value, ensure_ascii=False)


def toml_number(value: float) -> str:
    """A TOML number: integers stay integers, floats keep one decimal."""
    if float(value).is_integer():
        return str(int(value))
    return repr(float(value))


def ext_of(path: str) -> str | None:
    """Lower-case extension without the dot (``None`` when there is none).

    Either separator style is accepted; a leading dot is not an extension
    (``.gitignore`` has none).
    """
    name = path.replace("\\", "/").rsplit("/", 1)[-1]
    if "." not in name[1:]:
        return None
    return name.rsplit(".", 1)[-1].lower() or None
