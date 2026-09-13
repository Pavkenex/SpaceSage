"""Shared pytest configuration and fixtures."""

from __future__ import annotations

import sys
from collections.abc import Iterator
from pathlib import Path

import pytest

from spacesage import ai

TESTS_DIR = Path(__file__).resolve().parent

# ``tests/fixtures`` is a namespace package holding the export generator, and
# ``tests/ai_stub.py`` the scripted OpenAI-compatible server the AI tests talk
# to; both are importable by name regardless of pytest's import mode or the
# current working directory.  (``tests/gui/conftest.py`` claims the module name
# ``conftest`` in a full run, so shared *helpers* live in ``ai_support.py``.)
if str(TESTS_DIR) not in sys.path:
    sys.path.insert(0, str(TESTS_DIR))

from ai_stub import StubServer  # noqa: E402 - needs the path set up above
from qt_shutdown_guard import keep_singletons_alive  # noqa: E402 - likewise

# The Qt binding loses references to the CPython singletons on every Python->C++
# call; on Python 3.11 that drains them and the interpreter aborts while
# finalizing, *after* an all-green summary (exit 134).  ``tests/qt_shutdown_guard``
# carries the measurements and what the guard does; it is a no-op without Qt and
# on 3.12+, where the singletons already are immortal.
keep_singletons_alive()


@pytest.fixture
def data_dir() -> Path:
    """Directory holding the committed WizTree export fixtures."""
    return TESTS_DIR / "fixtures" / "data"


# --------------------------------------------------------------------------- #
# The AI layer (S10): a scripted OpenAI-compatible server and an engine
# --------------------------------------------------------------------------- #

STUB_MODEL = "stub-model"
"""Model the stub server answers with; the fixtures' providers call it."""


@pytest.fixture
def ai_stub() -> Iterator[StubServer]:
    """A running stub provider on a loopback port (script replies with ``push``)."""
    server = StubServer().start()
    try:
        yield server
    finally:
        server.stop()


@pytest.fixture
def ai_config(tmp_path: Path, ai_stub: StubServer) -> ai.AIConfig:
    """A ready config pointing at the stub, with an isolated cache directory.

    Deliberately offline-safe: loopback endpoint, retries off, a five-second
    timeout, and a cache under the test's own ``tmp_path`` so no test can read
    (or leave) another one's answers.
    """
    provider = ai.ProviderConfig(
        name="stub",
        kind="custom",
        base_url=ai_stub.url,
        model=STUB_MODEL,
        timeout_s=5.0,
        stream=True,
    )
    return ai.AIConfig(
        enabled=True,
        default_provider="stub",
        providers=(provider,),
        cache_dir=str(tmp_path / "ai-cache"),
        retries=0,
    )


@pytest.fixture
def ai_engine(ai_config: ai.AIConfig) -> ai.AIEngine:
    """An engine on the stub provider; retry sleeps are patched out (fast tests)."""
    return ai.AIEngine(ai_config, sleep=lambda _seconds: None)
