"""Bootstrap: pick a theme, build the window, or fail with a real message.

Two things this module takes seriously:

* **The UI must be able to say why it cannot start.**  A missing Qt runtime
  (``libEGL.so.1`` and friends on a bare Linux box) makes Qt abort the process
  before a single widget exists, so the app probes Qt **in a subprocess** first
  and, when that probe fails, prints the reason and shows it through whatever
  graphical tool the machine has (``zenity`` / ``kdialog`` / ``xmessage``).
* **Nothing else in the app may block on the engine.**  The window is built
  empty; analysis runs in a worker thread (see ``spacesage.app.workers``).
"""

from __future__ import annotations

import shutil
import subprocess
import sys
from collections.abc import Callable, Sequence
from pathlib import Path

from spacesage import __version__
from spacesage.app import ai_models, state, theme
from spacesage.app.windows import MainWindow

SELF_CHECK_FLAG = "--self-check"
"""Internal flag: create a QApplication, print the platform, exit."""

VERSION_FLAG = "--version"

PROBE_TIMEOUT_S = 60.0

MISSING_QT_MESSAGE = (
    "SpaceSage could not open a window: Qt could not load a windowing platform.\n\n"
    "On a bare Linux machine this usually means the Qt runtime libraries are missing:\n"
    "    sudo apt-get install -y libegl1 libgl1 libglvnd0 libxkbcommon0\n"
    "In the project's dev container, source the helper first:\n"
    "    source scripts/gui-env.sh && uv run python -m spacesage.app\n"
    "On a headless machine, set QT_QPA_PLATFORM=offscreen to run without a display.\n\n"
    "Qt said: {detail}"
)


def self_check() -> int:
    """``--self-check``: create a QApplication, report the platform, exit."""
    try:
        from PySide6.QtCore import qVersion
        from PySide6.QtWidgets import QApplication
    except ImportError as exc:  # pragma: no cover - the probe reports this first
        print(f"PySide6 is not importable: {exc}", file=sys.stderr)
        return 2
    existing = QApplication.instance()
    application = existing if isinstance(existing, QApplication) else QApplication([])
    print(f"qt {qVersion()} platform {application.platformName()}")
    return 0


def probe_qt(
    *,
    runner: Callable[..., subprocess.CompletedProcess[str]] | None = None,
    timeout_s: float = PROBE_TIMEOUT_S,
) -> str | None:
    """Run the Qt self-check in a subprocess; return ``None`` when it works.

    The parent process survives a Qt abort, which is the whole point: only then
    can the app explain what is missing instead of dying with a stack trace.
    """
    run = runner if runner is not None else subprocess.run
    command = [sys.executable, "-m", "spacesage.app", SELF_CHECK_FLAG]
    try:
        completed = run(command, capture_output=True, text=True, timeout=timeout_s)
    except (OSError, subprocess.SubprocessError) as exc:
        return f"the Qt self check could not run: {exc}"
    if completed.returncode == 0:
        return None
    detail = [
        line.strip()
        for line in (completed.stderr or completed.stdout or "").splitlines()
        if line.strip()
    ]
    if detail:
        return detail[-1]
    return f"the Qt self check exited with code {completed.returncode}"


def fallback_dialog(message: str) -> bool:
    """Show ``message`` without Qt, through zenity/kdialog/xmessage.

    Returns ``True`` when something displayed it; ``False`` means the message on
    stderr is all the user gets (still a clean exit, never a traceback).
    """
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
) -> int:
    """Report a startup failure and return the process exit code."""
    out = sys.stderr if stream is None else stream
    print(f"spacesage: {message}", file=out)  # type: ignore[arg-type]
    shown = (dialog if dialog is not None else fallback_dialog)(message)
    if not shown:
        print("(no dialog tool available: the message above is the report)", file=out)  # type: ignore[arg-type]
    return 2


def create_window(
    application: object,
    settings: state.Settings,
    *,
    db_path: Path | None = None,
    data_root: Path | None = None,
    theme_manager: theme.ThemeManager | None = None,
    ai_service: ai_models.AIService | None = None,
) -> MainWindow:
    """Build the main window for an existing QApplication (tests use this)."""
    window = MainWindow(
        settings,
        db_path=db_path if db_path is not None else state.index_path(),
        data_root=data_root,
        theme_manager=theme_manager,
        ai_service=ai_service,
    )
    if theme_manager is not None:
        theme_manager.changed.connect(lambda _scheme: window.apply_theme())
        window.themeModeChanged.connect(theme_manager.set_mode)
    return window


def run(argv: Sequence[str] | None = None) -> int:
    """Run the desktop app; returns the process exit code."""
    args = list(sys.argv if argv is None else argv)
    if VERSION_FLAG in args:
        print(f"spacesage {__version__}")
        return 0
    if SELF_CHECK_FLAG in args:
        return self_check()

    failure = probe_qt()
    if failure is not None:
        return fatal(MISSING_QT_MESSAGE.format(detail=failure))

    from PySide6.QtWidgets import QApplication

    application = QApplication.instance()
    if not isinstance(application, QApplication):
        QApplication.setApplicationName("SpaceSage")
        QApplication.setOrganizationName("SpaceSage")
        QApplication.setApplicationVersion(__version__)
        application = QApplication([args[0] if args else "spacesage"])

    settings = state.Settings.persisted()
    manager = theme.ThemeManager(application, mode=settings.theme_mode())
    window = create_window(application, settings, theme_manager=manager)
    index_file = state.index_path()
    window.set_status(
        f"Index ready at {index_file} — use it from Import."
        if index_file.is_file()
        else "Import a WizTree CSV export to start."
    )
    window.show()
    return application.exec()
