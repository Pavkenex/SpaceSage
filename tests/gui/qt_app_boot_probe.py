"""Child process of ``test_app_shutdown_guard.py``: boot the app, report counts.

Runs the *product* bootstrap -- ``spacesage.app:main`` with ``--self-check``, the
same entry ``spacesage-app`` and the frozen build execute -- in its own
interpreter, because the abort this guards against happens while the process
exits.  With ``--pumps`` it then turns the event loop that many times, the
shape that drains the singletons on 3.11, and prints the reference counts
around both halves.

Prints one line per step, so a failing run says which half died and what the
counts were; ``BOOT OK`` on the last line means the process survived to the end.
"""

from __future__ import annotations

import argparse
import os
import sys

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")


def counts() -> tuple[int, int, int]:
    return (sys.getrefcount(None), sys.getrefcount(True), sys.getrefcount(False))


def main(args: list[str]) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--pumps", type=int, default=0)
    opts = parser.parse_args(args)

    from PySide6.QtWidgets import QApplication

    from spacesage.app import main as app_entry

    code = app_entry([sys.argv[0], "--self-check"])
    print(f"BOOT exit={code} counts None/True/False {counts()}", flush=True)
    if code != 0:
        return code

    application = QApplication.instance()
    if application is None:  # pragma: no cover - --self-check always builds one
        print("no QApplication after the boot", file=sys.stderr, flush=True)
        return 2
    if opts.pumps:
        for _ in range(opts.pumps):
            application.processEvents()
        print(f"PUMPED {opts.pumps} turns; counts {counts()}", flush=True)
    print("BOOT OK", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
