"""Crash reporting: the app must never die with an unreadable box.

A ``--windowed`` PyInstaller build has no console, so an unhandled exception
during startup is reported by the bootloader's dialog -- one truncated line
("Failed to execute script ... due to unhandled exception: ...") with no
traceback and nowhere to look.  That turns any machine-specific startup
failure into an unanswerable bug report.

This module is the last line of defense, and it is deliberately importable
when little else is: stdlib only, no Qt, no engine.  ``__main__`` imports it
before the application, and ``run()`` wraps startup with it, so every path
out of the app either works or leaves

* a full traceback in ``<data dir>/logs/crash-<stamp>.log`` -- with the
  version, platform and argv that produced it -- and
* a message the user can read and forward: a native message box on Windows,
  zenity/kdialog/xmessage on Linux, stderr as the floor.

The log directory mirrors ``spacesage.app.state.data_dir`` on purpose: this
module must not import the Qt-dependent package to find it.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import tempfile
import traceback
from collections.abc import Callable, Mapping
from datetime import UTC, datetime
from pathlib import Path

APP_NAME = "SpaceSage"

CRASH_EXIT_CODE = 3
"""Exit code for an unhandled exception (2 is a known startup failure)."""

_LOG_DIR_NAME = "logs"


def crash_log_dir(env: Mapping[str, str] | None = None) -> Path:
    """``<data dir>/logs`` -- computed without importing the Qt-dependent app."""
    values = os.environ if env is None else env
    override = values.get("SPACESAGE_DATA_DIR")
    if override:
        return Path(override).expanduser() / _LOG_DIR_NAME
    if sys.platform.startswith("win"):
        base = values.get("LOCALAPPDATA") or str(Path.home() / "AppData" / "Local")
        return Path(base) / "spacesage" / _LOG_DIR_NAME
    if sys.platform == "darwin":
        base = Path.home() / "Library" / "Application Support" / "spacesage"
        return base / _LOG_DIR_NAME
    xdg = values.get("XDG_DATA_HOME")
    root = Path(xdg) if xdg else Path.home() / ".local" / "share"
    return root / "spacesage" / _LOG_DIR_NAME


def _report_text(exc: BaseException) -> str:
    """The full crash report: what ran, where, and the traceback."""
    try:
        from spacesage import __version__ as version
    except Exception:  # pragma: no cover - the report must survive anything
        version = "unknown"
    return "\n".join(
        [
            f"{APP_NAME} crash report",
            f"when: {datetime.now(UTC).isoformat(timespec='seconds')}",
            f"version: {version}",
            f"platform: {sys.platform}",
            f"frozen: {bool(getattr(sys, 'frozen', False))}",
            f"executable: {sys.executable}",
            f"argv: {sys.argv!r}",
            "",
            "".join(traceback.format_exception(type(exc), exc, exc.__traceback__)),
        ]
    )


def write_crash_log(exc: BaseException, *, env: Mapping[str, str] | None = None) -> Path | None:
    """Write the traceback to a crash log; ``None`` when nowhere is writable.

    The data directory is tried first; a machine whose data directory cannot
    be created (permissions, a read-only volume) still gets the report from
    the system temporary directory.
    """
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    text = _report_text(exc)
    fallback = Path(tempfile.gettempdir()) / "spacesage-logs"
    for directory in (crash_log_dir(env), fallback):
        try:
            directory.mkdir(parents=True, exist_ok=True)
            path = directory / f"crash-{stamp}.log"
            path.write_text(text, encoding="utf-8")
            return path
        except OSError:
            continue
    return None


def windows_message_box(message: str) -> bool:
    """Show a native message box; ``False`` off Windows or when it cannot.

    ``MessageBoxW`` needs no console, no Qt and no dialog tool -- it is the
    one reporter guaranteed to exist on a Windows desktop.
    """
    if os.name != "nt":
        return False
    import ctypes

    windll = getattr(ctypes, "windll", None)
    if windll is None:  # pragma: no cover - defensive
        return False
    try:
        windll.user32.MessageBoxW(None, message, APP_NAME, 0x10)  # MB_ICONERROR
    except Exception:  # pragma: no cover - defensive: a reporter must not raise
        return False
    return True


def fallback_dialog(
    message: str, *, message_box: Callable[[str], bool] = windows_message_box
) -> bool:
    """Show ``message`` without Qt: a native box on Windows, else dialog tools.

    Returns ``True`` when something displayed it; ``False`` means the message
    on stderr is all the user gets (still a clean exit, never a traceback).
    """
    try:
        if message_box(message):
            return True
    except Exception:  # an injected box must never take the report down
        pass
    commands: tuple[tuple[str, list[str]], ...] = (
        ("zenity", ["--error", "--title=SpaceSage", f"--text={message}"]),
        ("kdialog", ["--error", message, "--title", "SpaceSage"]),
        ("xmessage", ["-center", message]),
    )
    for name, args in commands:
        if shutil.which(name) is None:
            continue
        try:
            subprocess.run([name, *args], check=False, timeout=120)
        except (OSError, subprocess.SubprocessError):
            continue
        return True
    return False


def fatal(
    message: str,
    *,
    dialog: Callable[[str], bool] | None = None,
    stream: object | None = None,
    code: int = 2,
) -> int:
    """Report a startup failure and return the process exit code."""
    out = sys.stderr if stream is None else stream
    print(f"spacesage: {message}", file=out)  # type: ignore[arg-type]
    shown = (dialog if dialog is not None else fallback_dialog)(message)
    if not shown:
        print("(no dialog tool available: the message above is the report)", file=out)  # type: ignore[arg-type]
    return code


def report_crash(
    exc: BaseException,
    *,
    dialog: Callable[[str], bool] | None = None,
    stream: object | None = None,
) -> int:
    """Log an unhandled exception, tell the user where the log is, exit 3.

    This is what turns "Failed to execute script ... due to unhandled
    exception: ..." into something a user can forward and a maintainer can
    act on.
    """
    log_path = write_crash_log(exc)
    if log_path is not None:
        tail = f"The full report was written to:\n{log_path}\n\nPlease send that file."
    else:
        tail = "No crash log could be written (nowhere writable was found)."
    message = (
        f"{APP_NAME} hit an unexpected problem and had to stop.\n\n"
        f"{type(exc).__name__}: {exc}\n\n{tail}"
    )
    return fatal(message, dialog=dialog, stream=stream, code=CRASH_EXIT_CODE)
