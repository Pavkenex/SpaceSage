"""Shared pytest configuration and fixtures."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

TESTS_DIR = Path(__file__).resolve().parent

# ``tests/fixtures`` is a namespace package holding the export generator; make
# it importable as ``from fixtures import gen`` regardless of pytest's import
# mode or the current working directory.
if str(TESTS_DIR) not in sys.path:
    sys.path.insert(0, str(TESTS_DIR))


@pytest.fixture
def data_dir() -> Path:
    """Directory holding the committed WizTree export fixtures."""
    return TESTS_DIR / "fixtures" / "data"
