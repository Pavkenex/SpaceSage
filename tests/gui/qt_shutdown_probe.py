"""Child process of ``test_shutdown_guard.py``: pump Qt, then exit.

Runs in its own interpreter on purpose -- the abort this guards against happens
while the process exits, so it can only be observed (and only be fixed) out of
process.  With ``--no-guard`` the child skips the guard, drains the CPython
singletons and dies at exit on Python 3.11; that is what the test asserts.

Prints the reference counts it started and ended with, so a failing run says
whether the binding moved them -- and up or down.
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

TESTS_DIR = Path(__file__).resolve().parents[1]
if str(TESTS_DIR) not in sys.path:
    sys.path.insert(0, str(TESTS_DIR))

from PySide6.QtWidgets import QApplication  # noqa: E402 - platform set above

from qt_shutdown_guard import keep_singletons_alive  # noqa: E402 - tests/ on the path

#: Enough turns to prove the guard survives a heavy run: the counts of live
#: references to the singletons are in the low thousands, and the binding moves
#: them by one per call (see ``tests/qt_shutdown_guard.py``).
DEFAULT_PUMPS = 20_000


def counts() -> tuple[int, int, int]:
    return (sys.getrefcount(None), sys.getrefcount(True), sys.getrefcount(False))


def main(args: list[str]) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--pumps", type=int, default=DEFAULT_PUMPS)
    parser.add_argument("--no-guard", action="store_true")
    opts = parser.parse_args(args)

    guarded = keep_singletons_alive() if not opts.no_guard else False
    app = QApplication([sys.argv[0]])
    before = counts()
    for _ in range(opts.pumps):
        app.processEvents()
    print(f"REFCOUNTS None/True/False {before} -> {counts()} (guard={guarded})", flush=True)
    print(f"PROBE OK {opts.pumps} pumps", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
