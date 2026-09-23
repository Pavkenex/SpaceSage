"""Bootstrap: pick a theme, build the window, or fail with a real message.

Three things this module takes seriously:

* **The UI must be able to say why it cannot start.**  A missing Qt runtime
  (``libEGL.so.1`` and friends on a bare Linux box) makes Qt abort the process
  before a single widget exists, so the app probes Qt **in a subprocess** first
  and, when that probe fails, prints the reason and shows it through whatever
  graphical tool the machine has (``zenity`` / ``kdialog`` / ``xmessage``).
  A frozen (PyInstaller) build re-runs *itself* with ``--self-check`` instead of
  ``python -m spacesage.app``, which is the same probe through the same door the
  user came in.
* **Nothing else in the app may block on the engine.**  The window is built
  empty; analysis runs in a worker thread (see ``spacesage.app.workers``).
* **The packaged binary has to be provable without a display.**  ``--capture
  PATH`` shows the real window offscreen, renders it to a PNG and exits -- that
  is what the packaging smoke job in CI (and a human checking a release) runs
  against the built executable.
"""

from __future__ import annotations

import os
import subprocess
import sys
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import TextIO

from PySide6.QtCore import QTimer
from PySide6.QtWidgets import QApplication

from spacesage import __version__
from spacesage.app import ai_models, icons, qt_shutdown_guard, state, theme
from spacesage.app.crash import fatal, report_crash
from spacesage.app.windows import MainWindow

SELF_CHECK_FLAG = "--self-check"
"""Internal flag: create a QApplication, print the platform, exit."""

VERSION_FLAG = "--version"

CAPTURE_FLAG = "--capture"
"""Internal flag: ``--capture PATH`` renders the window to a PNG and exits."""

CAPTURE_DELAY_FLAG = "--capture-delay"
"""Internal flag: milliseconds to let the window settle before the capture."""

DEFAULT_CAPTURE_DELAY_MS = 800
"""How long the capture flag waits for layout, the entrance fade and a paint."""

CAPTURE_USAGE = f"usage: spacesage {CAPTURE_FLAG} PATH [{CAPTURE_DELAY_FLAG} MS]"

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


def ensure_streams() -> None:
    """Give a windowed build somewhere to print.

    A ``--windowed`` executable on Windows can start with no console at all, in
    which case ``sys.stdout``/``sys.stderr`` are ``None`` and every ``print``
    raises instead of reporting.  The internal flags (``--self-check``,
    ``--capture``) are exactly what CI runs on that build, so they have to be
    able to speak: the fallback is ``os.devnull``, and a real inherited handle
    (a pipe from the parent) is left alone.
    """
    for name in ("stdout", "stderr"):
        if getattr(sys, name, None) is None:
            # Deliberately kept open for the life of the process: this *is* the
            # stream the app will print to, not a leaked handle.
            setattr(sys, name, open(os.devnull, "w", encoding="utf-8"))  # noqa: SIM115


def self_check() -> int:
    """``--self-check``: create a QApplication, report the platform, exit.

    The font line is the frozen build's honesty check: the release smoke
    prints it, so a bundle that cannot resolve a text font is visible in the
    run's annotations instead of only in a render nobody reads.  The rules
    line does the same for the built-in rule packs -- data files the bundle
    can silently omit, leaving a window that starts and an analysis that
    always fails.  Neither report changes the exit code: this probe answers
    "can this build run", and the smoke steps assert what it says.
    """
    try:
        from PySide6.QtCore import qVersion
        from PySide6.QtGui import QFontDatabase
        from PySide6.QtWidgets import QApplication
    except ImportError as exc:  # pragma: no cover - the probe reports this first
        print(f"PySide6 is not importable: {exc}", file=sys.stderr)
        return 2
    existing = QApplication.instance()
    application = existing if isinstance(existing, QApplication) else QApplication([])
    families = QFontDatabase.families()
    print(
        f"qt {qVersion()} platform {application.platformName()} "
        f"fonts {len(families)} ui {theme.resolve_family(theme.UI_FAMILIES)!r}"
    )
    try:
        from spacesage import rules

        ruleset = rules.load_rules()
    except Exception as exc:  # a broken pack is reported, never a crash on a probe
        print(f"rules missing: {exc}")
    else:
        print(f"rules {len(ruleset.builtin_packs)} packs {len(ruleset.rules)} rules")
    return 0


def self_check_command() -> list[str]:
    """The child-process command that probes Qt.

    From a source checkout that is ``python -m spacesage.app
    --self-check``; in a frozen build there is no ``-m`` to run, so the
    executable re-runs itself with the flag.  PyInstaller hands a re-executed
    one-file binary its own extraction directory, so the probe is as cheap as
    any other start.
    """
    if getattr(sys, "frozen", False):  # PyInstaller sets this on the bundled app
        return [sys.executable, SELF_CHECK_FLAG]
    return [sys.executable, "-m", "spacesage.app", SELF_CHECK_FLAG]


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
    command = self_check_command()
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


class UsageError(ValueError):
    """A command line the app cannot act on."""


def capture_request(args: Sequence[str]) -> tuple[Path, int] | None:
    """``(path, delay_ms)`` for a ``--capture`` run, ``None`` when not capturing.

    Raises :class:`UsageError` for a flag without its value -- the app is a
    windowed program, so a malformed command line has to say what it wanted
    instead of starting a window nobody asked for.
    """
    if CAPTURE_FLAG not in args:
        return None
    index = list(args).index(CAPTURE_FLAG)
    if index + 1 >= len(args):
        raise UsageError(f"{CAPTURE_FLAG} needs the PNG path to write")
    delay = DEFAULT_CAPTURE_DELAY_MS
    listed = list(args)
    if CAPTURE_DELAY_FLAG in listed:
        at = listed.index(CAPTURE_DELAY_FLAG)
        if at + 1 >= len(listed):
            raise UsageError(f"{CAPTURE_DELAY_FLAG} needs milliseconds")
        try:
            delay = int(listed[at + 1])
        except ValueError as exc:
            problem = f"{CAPTURE_DELAY_FLAG} needs milliseconds, not {listed[at + 1]!r}"
            raise UsageError(problem) from exc
        if delay < 0:
            raise UsageError(f"{CAPTURE_DELAY_FLAG} cannot be negative")
    return Path(args[index + 1]).expanduser(), delay


def schedule_capture(
    application: QApplication,
    window: MainWindow,
    path: Path,
    *,
    delay_ms: int = DEFAULT_CAPTURE_DELAY_MS,
    stream: TextIO | None = None,
) -> None:
    """Render ``window`` to ``path`` once it has settled, then quit.

    The exit code is the verdict: 0 only when a PNG was written, so a packaging
    smoke run cannot pass on a blank file.  The delay is a real wait for layout,
    the entrance fade and the first paint -- what is grabbed is the window a user
    would see.
    """
    out = sys.stdout if stream is None else stream
    timer = QTimer(window)
    timer.setSingleShot(True)

    def shoot() -> None:
        pixmap = window.grab()
        saved = not pixmap.isNull() and pixmap.save(str(path))
        if saved:
            print(f"captured {path} ({pixmap.width()}x{pixmap.height()})", file=out)
        else:
            print(f"spacesage: could not render {path}", file=sys.stderr)
        application.exit(0 if saved else 3)

    timer.timeout.connect(shoot)
    timer.start(delay_ms)


def run(argv: Sequence[str] | None = None) -> int:
    """Run the desktop app; returns the process exit code.

    Any unhandled exception between here and the event loop is reported by
    ``crash.report_crash``: a full traceback in the crash log and a message
    the user can forward -- never the bootloader's one-line dialog.
    """
    ensure_streams()
    try:
        return _run(argv)
    except Exception as exc:
        return report_crash(exc)


def _run(argv: Sequence[str] | None = None) -> int:
    """The app proper (``run`` only adds the crash net and real streams)."""
    # The first thing the product does: park the singleton reference counts the
    # Qt binding corrupts on Python->C++ calls.  A session that drains them
    # aborts the interpreter -- mid-run or while it finalizes, after the user's
    # work -- and the app's session length is unbounded.  See
    # ``spacesage.app.qt_shutdown_guard`` for the measurements.
    qt_shutdown_guard.keep_singletons_alive()
    args = list(sys.argv if argv is None else argv)
    if VERSION_FLAG in args:
        print(f"spacesage {__version__}")
        return 0
    if SELF_CHECK_FLAG in args:
        return self_check()

    try:
        capture = capture_request(args)
    except UsageError as exc:
        print(f"spacesage: {exc}\n{CAPTURE_USAGE}", file=sys.stderr)
        return 2

    failure = probe_qt()
    if failure is not None:
        return fatal(MISSING_QT_MESSAGE.format(detail=failure))

    application = QApplication.instance()
    if not isinstance(application, QApplication):
        QApplication.setApplicationName("SpaceSage")
        QApplication.setOrganizationName("SpaceSage")
        QApplication.setApplicationVersion(__version__)
        application = QApplication([args[0] if args else "spacesage"])
    application.setWindowIcon(icons.app_icon())

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
    if capture is not None:
        path, delay = capture
        path.parent.mkdir(parents=True, exist_ok=True)
        schedule_capture(application, window, path, delay_ms=delay)
    return application.exec()
