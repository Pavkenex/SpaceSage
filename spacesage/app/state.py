"""Where the desktop app keeps its own files (index, settings).

The *engine* is deliberately cwd-relative (``spacesage ingest x.csv --db DIR``)
because it is a scripting surface.  The app is not: a packaged ``spacesage.exe``
must not care where it was started from, so its index and preferences live in
the platform's per-user location -- ``SPACESAGE_DATA_DIR`` overrides it for
tests and portable installs.
"""

from __future__ import annotations

import os
import sys
from collections.abc import Mapping
from pathlib import Path

from PySide6.QtCore import QSettings

from spacesage import db

APP_DIR_NAME = "spacesage"
"""Folder name under the platform's data directory."""

SETTINGS_SECTION = "app"

DEFAULT_RESERVE_BYTES = 20 * 1024**3
"""Free space the planner keeps untouched by default (design §7: 20 GB)."""

DEFAULT_MIN_SIZE = 100 * 1024**2
"""Smallest entry the list considers by default (100 MiB)."""

DEFAULT_LIST_TOP = 500
"""Candidates listed per kind before the list is built."""


def data_dir(env: Mapping[str, str] | None = None) -> Path:
    """Per-user data directory of the app (created on demand)."""
    values = os.environ if env is None else env
    override = values.get("SPACESAGE_DATA_DIR")
    if override:
        return Path(override).expanduser()
    if sys.platform.startswith("win"):
        base = values.get("LOCALAPPDATA") or str(Path.home() / "AppData" / "Local")
        return Path(base) / APP_DIR_NAME
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Application Support" / APP_DIR_NAME
    xdg = values.get("XDG_DATA_HOME")
    root = Path(xdg) if xdg else Path.home() / ".local" / "share"
    return root / APP_DIR_NAME


def index_path(env: Mapping[str, str] | None = None) -> Path:
    """The app's index file (``<data dir>/spacesage.db``)."""
    return db.resolve_db_path(data_dir(env))


def ensure_data_dir(env: Mapping[str, str] | None = None) -> Path:
    """Create the data directory if needed and return it."""
    target = data_dir(env)
    target.mkdir(parents=True, exist_ok=True)
    return target


class Settings:
    """The app's persisted preferences (theme, target drive, thresholds)."""

    def __init__(self, storage: QSettings | None = None) -> None:
        self._storage = storage

    # -- storage ---------------------------------------------------------- #

    @classmethod
    def ephemeral(cls) -> Settings:
        """A settings object that persists nothing (tests, throwaway runs)."""
        return cls(None)

    @classmethod
    def persisted(cls, path: str | Path | None = None) -> Settings:
        """Settings backed by a file (default: the platform's config location)."""
        if path is None:
            return cls(QSettings("SpaceSage", "SpaceSage"))
        return cls(QSettings(str(path), QSettings.Format.IniFormat))

    @property
    def storage(self) -> QSettings | None:
        """The underlying QSettings, when the app persists anything."""
        return self._storage

    # -- values ----------------------------------------------------------- #

    def _get(self, key: str, default: object) -> object:
        if self._storage is None:
            return default
        return self._storage.value(f"{SETTINGS_SECTION}/{key}", default)

    def _set(self, key: str, value: object) -> None:
        if self._storage is None:
            return
        self._storage.setValue(f"{SETTINGS_SECTION}/{key}", value)
        self._storage.sync()

    def theme_mode(self, default: str = "system") -> str:
        """Saved theme mode (``system``/``light``/``dark``)."""
        return str(self._get("theme_mode", default))

    def set_theme_mode(self, mode: str) -> None:
        """Persist the theme mode."""
        self._set("theme_mode", mode)

    def target_drive(self, default: str = "D:") -> str:
        """Saved target drive for moves."""
        return str(self._get("target_drive", default))

    def set_target_drive(self, drive: str) -> None:
        """Persist the target drive."""
        self._set("target_drive", drive)

    def reserve_bytes(self, default: int = DEFAULT_RESERVE_BYTES) -> int:
        """Saved free-space reserve in bytes."""
        return int(str(self._get("reserve_bytes", default)))

    def set_reserve_bytes(self, value: int) -> None:
        """Persist the free-space reserve."""
        self._set("reserve_bytes", int(value))

    def min_size(self, default: int = DEFAULT_MIN_SIZE) -> int:
        """Saved smallest listed entry in bytes."""
        return int(str(self._get("min_size", default)))

    def set_min_size(self, value: int) -> None:
        """Persist the smallest listed entry."""
        self._set("min_size", int(value))

    def last_csv(self, default: str = "") -> str:
        """Path of the export used last."""
        return str(self._get("last_csv", default))

    def set_last_csv(self, path: str) -> None:
        """Persist the last export path."""
        self._set("last_csv", path)

    def last_db(self, default: str = "") -> str:
        """Path of the index used last."""
        return str(self._get("last_db", default))

    def set_last_db(self, path: str) -> None:
        """Persist the last index path."""
        self._set("last_db", path)
