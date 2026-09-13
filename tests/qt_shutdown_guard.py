"""Keep CPython's singletons alive so a long Qt run can exit cleanly.

The Qt binding loses references to ``None``/``True``/``False`` on Python->C++
calls.  Measured on 3.11.15 (aarch64) with the locked PySide6, and with 6.8.3,
6.9.3, 6.10.3 and 6.11.0 for comparison: one lost reference per
``QApplication.processEvents()`` call, and the same for ``QWidget.resize()``,
``setParent()``, ``setProperty()``, ``QObject.property()``, ``quit()``,
``closeAllWindows()`` and ``sendPostedEvents()`` -- while the number of live
objects and of ``gc.get_referrers(None)`` does not move.  Nothing is freed: the
reference count itself is wrong.

On CPython 3.11 the three singletons are ordinary refcounted objects, so a long
enough session drains them and the interpreter aborts during finalization --
*after* an all-green summary::

    Fatal Python error: bool_dealloc: deallocating True or False:
    bug likely caused by a refcount error in a C extension
    Python runtime state: finalizing (tstate=0x...)
    Current thread ... (most recent call first):
      Garbage-collecting
    EXIT=134

CPython 3.12 made the singletons immortal (PEP 683), which is why the same
suites exit 0 there.  :func:`keep_singletons_alive` does explicitly what 3.12
does implicitly: it parks the three singletons far from zero so no run can
drain them, and the exit code stops lying about the tests.

Remove it when the binding stops losing references -- the probe in
``tests/gui/test_shutdown_guard.py`` is what says so.
"""

from __future__ import annotations

import ctypes
import sys

#: A refcount no run of this project can drain, well inside ``Py_ssize_t``.
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
