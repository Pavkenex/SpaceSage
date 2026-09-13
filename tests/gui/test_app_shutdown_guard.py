"""The app parks the singletons too: the product bootstrap holds the guard.

``tests/qt_shutdown_guard`` explains the binding bug and what parking does; the
suites apply it from ``tests/conftest.py``, and the app applies it from
``spacesage/app/main.py`` before any QApplication is built.  Both need it for
the same reason: the binding loses references to the singletons on Python->C++
calls, and a session that drains them aborts the interpreter.  The app's
sessions make the same calls the suites' do -- status updates, list adoptions,
theme switches, window drags -- and their length is unbounded, so the *product*
path parks them rather than the harness alone (`spacesage/app/qt_shutdown_guard`
carries the per-call measurements).

These tests are the out-of-process proof of the app half: a child that starts
through the real product entry ends with the singletons out of reach and exits
0, and a Qt-heavy session on top of that same bootstrap exits 0 as well.  The
unguarded half -- the interpreter dying at exit -- is the harness's own test
(``tests/gui/test_shutdown_guard.py``); it is the same mechanism, so it is not
duplicated here.  Both halves are 3.11-specific: on 3.12+ the singletons are
immortal and the app's guard is a documented no-op.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
REPO_ROOT = HERE.parents[1]
PROBE = HERE / "qt_app_boot_probe.py"

#: Enough turns that a drainable count would be gone several times over (the
#: binding loses one per turn and the live counts are in the thousands, while
#: the parked count is 2**62).  The suites' unguarded child dies of this size.
PUMPS = 20_000


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


def boot_counts(output: str) -> tuple[int, ...]:
    """The counts the child printed right after the product bootstrap."""
    line = next(line for line in output.splitlines() if line.startswith("BOOT exit="))
    numbers = line.split("counts None/True/False", 1)[1].strip().strip("()").split(",")
    return tuple(int(part) for part in numbers)


def test_the_app_bootstrap_parks_the_singletons() -> None:
    """After ``spacesage.app:main`` has booted, no session can drain them.

    On 3.11 that is the app's own guard (parked at ``2**62``); on 3.12+ the
    singletons already are immortal and report ``2**32 - 1``.  Either way they
    are out of reach, which is what the session test below exercises.
    """
    completed = run_probe()
    assert completed.returncode == 0, completed.stderr[-2000:]
    parked = boot_counts(completed.stdout)
    assert min(parked) > 2**31, f"the app bootstrap left the singletons drainable: {parked}"


def test_a_qt_heavy_session_on_the_app_bootstrap_exits_cleanly() -> None:
    """Twenty thousand event-loop turns after the product boot, then exit 0."""
    completed = run_probe("--pumps", str(PUMPS))
    assert completed.returncode == 0, completed.stderr[-2000:]
    assert f"PUMPED {PUMPS} turns" in completed.stdout
    assert "BOOT OK" in completed.stdout
