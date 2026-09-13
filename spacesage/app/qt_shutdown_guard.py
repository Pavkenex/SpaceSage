"""Keep CPython's singletons alive so a long app session can exit cleanly.

The Qt binding loses references to ``None``/``True``/``False`` on Python->C++
calls, in both directions.  Measured on 3.11.15 (aarch64) with the locked
PySide6, on the app's own window and listing (``probe_app_mix.py``, logs in
``/opt/data/scratch/t_1b70d03f/logs``): one reference lost per
``QApplication.processEvents()``, ``QWidget.resize()`` or
``setWindowTitle()`` call; three per ``MainWindow.set_status()`` (the status bar
which says something after every action); about twelve per theme toggle; about
ninety per re-adoption of a listing (``set_listing``).  Only *building* a
window adds -- about 1500 references to ``None`` per fresh window, and the app
builds exactly one per run, while every interaction after it nets a drain.  A
simulated session of those same calls walks the count from ~15000 down past
zero and the interpreter aborts.

On CPython 3.11 the three singletons are ordinary refcounted objects, so a long
enough session drains them and the process dies -- mid-run with
``none_dealloc`` while initialized, or during finalization with ``bool_dealloc``
*after* the user's work, both with the same "bug likely caused by a refcount
error in a C extension" and exit 134.  The test suites hit exactly this
(``tests/qt_shutdown_guard.py`` holds their half of the story and the probe
that says when the binding stops losing references); the app is the longer-lived
process, so it parks them too, from :func:`spacesage.app.main.run` before any
QApplication is built.

CPython 3.12 made the singletons immortal (PEP 683), which is why the same runs
exit 0 there.  :func:`keep_singletons_alive` does explicitly what 3.12 does
implicitly: it parks the three singletons far from zero so no session can drain
them, and the exit code stops lying about the run.

Remove it when the binding stops losing references -- the probe in
``tests/gui/test_shutdown_guard.py`` is what says so -- and the guard from
``tests/qt_shutdown_guard.py`` goes with it.
"""

from __future__ import annotations

import ctypes
import sys

#: A refcount no session of this app can drain, well inside ``Py_ssize_t``.
_OUT_OF_REACH = 2**62


def keep_singletons_alive() -> bool:
    """Park ``None``/``True``/``False`` out of reach; ``False`` when not needed.

    Returns ``True`` when the guard was applied -- CPython < 3.12 on a release
    build -- and ``False`` on 3.12+ (the singletons already are immortal) or on
    a ``Py_TRACE_REFS`` build, whose object layout puts other fields first.
    """
    if sys.version_info >= (3, 12) or hasattr(sys, "gettotalrefcount"):
        return False
    for singleton in (None, True, False):
        # id() is the object header, whose first field is the refcount.
        ctypes.c_ssize_t.from_address(id(singleton)).value = _OUT_OF_REACH
    return True
