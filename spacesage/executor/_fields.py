"""Typed accessors for the JSON records the executor reads and writes.

The plan, the manifest and the journal are plain mappings, and each field is
read the same way: a required non-empty string, or an optional one that may be
absent but must be a string when present.  A bad record has to name the field
and where it came from, so the two readers (``executor`` and
``executor.journal``) share the message here instead of keeping a copy each.
"""

from __future__ import annotations

from collections.abc import Mapping

from .backend import ExecutorError


def required_str(record: Mapping[str, object], key: str, *, where: str) -> str:
    """A required, non-empty string field."""
    value = record.get(key)
    if not isinstance(value, str) or not value:
        raise ExecutorError(f"{where}: {key!r} must be a non-empty string (got {value!r})")
    return value


def optional_str(record: Mapping[str, object], key: str, *, where: str) -> str | None:
    """An optional field: absent or null is ``None``; anything else must be a string."""
    value = record.get(key)
    if value is None:
        return None
    if not isinstance(value, str) or not value:
        raise ExecutorError(f"{where}: {key!r} must be a non-empty string or null (got {value!r})")
    return value
