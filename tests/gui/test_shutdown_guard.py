"""The Qt suites exit 0 on Python 3.11: the shutdown guard holds.

``tests/qt_shutdown_guard`` explains the upstream bug and the guard; these tests
are the out-of-process proof of both halves of it -- a child that pumps the
event loop twenty thousand times exits 0 with the guard, and on 3.11 the same
child dies at exit without it.

The unguarded child runs with fewer pumps on purpose: with more it drains the
singletons mid-run (``none_dealloc`` while "initialized"), with these it stays
alive until the interpreter finalizes and dies of the reported abort.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

HERE = Path(__file__).resolve().parent
REPO_ROOT = HERE.parents[1]
PROBE = HERE / "qt_shutdown_probe.py"

#: Fewer than the live references to ``None``, so the count only crosses zero
#: while the interpreter finalizes -- the signature docs/verification.md reports.
UNGUARDED_PUMPS = 4_000


def run_probe(*args: str) -> subprocess.CompletedProcess[str]:
    """Run the probe in its own interpreter, offscreen, from the repository root."""
    env = dict(os.environ, QT_QPA_PLATFORM="offscreen")
    return subprocess.run(
        [sys.executable, str(PROBE), *args],
        cwd=REPO_ROOT,
        env=env,
        capture_output=True,
        text=True,
        timeout=300,
    )


def test_a_qt_heavy_child_process_exits_cleanly() -> None:
    """Twenty thousand event loop turns, then an honest exit code (never 134)."""
    completed = run_probe()
    assert completed.returncode == 0, completed.stderr[-2000:]
    assert "PROBE OK" in completed.stdout


def test_the_singletons_would_drain_without_the_guard() -> None:
    """The guard is load-bearing: without it the same child dies at exit on 3.11."""
    if sys.version_info >= (3, 12):
        pytest.skip("the singletons already are immortal on this interpreter")
    completed = run_probe("--no-guard", "--pumps", str(UNGUARDED_PUMPS))
    assert completed.returncode != 0, (
        "the unguarded child was expected to abort; its output was:\n"
        f"{completed.stdout}{completed.stderr[-2000:]}"
    )
    assert "deallocating" in completed.stderr, "expected CPython's refcount fatal error"


@pytest.mark.parametrize("name", ["None", "True", "False"])
def test_the_singletons_cannot_be_drained(name: str) -> None:
    """The guard is in force for this session: the counts are out of reach.

    3.12+ reports the immortal marker (``2**32 - 1``) for these; on 3.11 the
    guard parks them at ``2**62``.  Either way, far above a normal count.
    """
    singletons: dict[str, object] = {"None": None, "True": True, "False": False}
    assert sys.getrefcount(singletons[name]) > 2**31
