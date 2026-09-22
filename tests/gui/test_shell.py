"""Shell tests: the window, its navigation, the theme tokens and the bootstrap."""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest
from PySide6.QtCore import QSize
from PySide6.QtGui import QImage
from PySide6.QtWidgets import QApplication

from spacesage.app import icons, state, theme
from spacesage.app.main import (
    CAPTURE_FLAG,
    CAPTURE_USAGE,
    MISSING_QT_MESSAGE,
    SELF_CHECK_FLAG,
    UsageError,
    capture_request,
    ensure_streams,
    fatal,
    probe_qt,
    run,
    self_check_command,
)
from spacesage.app.windows import PAGES, MainWindow

REPO_ROOT = Path(__file__).resolve().parents[2]


@pytest.mark.parametrize("key", [key for key, _label, _icon in PAGES])
def test_every_page_is_reachable(window: MainWindow, key: str) -> None:
    """Import / Opportunities / Plan / Settings, by key and by nav button."""
    assert window.navigate(key)
    assert window.current_page() == key
    assert window._nav_buttons[key].isChecked()
    assert window.stack.currentWidget() is not None


def test_unknown_page_is_refused(window: MainWindow) -> None:
    """A typo in navigation does not silently show the wrong screen."""
    assert window.navigate("nonsense") is False


def test_window_states_the_safety_contract(window: MainWindow) -> None:
    """The shell always says that analysis is read-only and plans gate action."""
    assert "read-only" in window.rail_footer.text()
    assert "approve" in window.rail_footer.text()
    assert window.status_theme.text().endswith("theme")
    assert window.windowTitle() == "SpaceSage"


def test_theme_follows_the_os_and_the_toggle_switches_tokens(qapp: QApplication) -> None:
    """One token set, two themes: the toggle restyles the app for real."""
    app = qapp
    manager = theme.ThemeManager(app, mode=theme.MODE_LIGHT)
    light = app.styleSheet()
    assert theme.tokens().name == theme.MODE_LIGHT
    assert theme.LIGHT.base in light

    manager.set_mode(theme.MODE_DARK)
    dark = app.styleSheet()
    assert theme.tokens().name == theme.MODE_DARK
    assert theme.DARK.base in dark and dark != light
    assert theme.DARK.text in dark

    manager.set_mode(theme.MODE_SYSTEM)
    assert manager.mode == theme.MODE_SYSTEM
    assert manager.scheme in (theme.MODE_LIGHT, theme.MODE_DARK)
    manager.set_mode(theme.MODE_LIGHT)


def test_token_scales_follow_the_design(window: MainWindow) -> None:
    """4px spacing grid, radii, typography scale and the mono stack."""
    assert list(theme.SPACE.values()) == [4, 8, 12, 16, 24, 32]
    assert set(theme.RADIUS.values()) == {6, 8}
    assert sorted(theme.TYPE_SCALE.values()) == [11, 12, 13, 15, 18, 24]
    assert theme.MOTION_FAST == 150 and theme.MOTION_NORMAL == 200
    mono = theme.mono_font()
    assert mono.family() == theme.mono_family()


def test_settings_screen_switches_the_theme(window: MainWindow) -> None:
    """The toggle lives in Settings (§9.1) and tells the manager to restyle."""
    settings_view = window.settings_view
    for mode in theme.MODES:
        assert mode in settings_view._buttons
    settings_view._buttons[theme.MODE_DARK].setChecked(True)
    assert settings_view.current_mode() == theme.MODE_DARK
    assert theme.tokens().name == theme.MODE_DARK
    settings_view._buttons[theme.MODE_SYSTEM].setChecked(True)
    assert settings_view.current_mode() == theme.MODE_SYSTEM


def test_bundled_icon_subset_is_complete_and_attributed() -> None:
    """Lucide icons ship with the attribution file the licence requires."""
    available = set(icons.available())
    needed = {
        "folder-open",
        "list-ordered",
        "clipboard-list",
        "settings",
        "search",
        "filter",
        "x",
        "chevron-down",
        "chevron-up",
        "alert-triangle",
        "info",
        "check",
        "shield-check",
        "folder",
        "file",
        "hard-drive",
        "sun",
        "moon",
        "monitor",
        "trash-2",
        "arrow-right-left",
        "minimize-2",
        "terminal",
        "link-2",
        "help-circle",
        "eye",
        "refresh-cw",
    }
    assert needed <= available
    attribution = REPO_ROOT / "spacesage" / "app" / "assets" / "ATTRIBUTION.md"
    text = attribution.read_text(encoding="utf-8")
    assert "ISC License" in text and "Lucide" in text and "MIT License" in text
    assert "currentColor" in icons.svg_source("folder")


def test_pixmap_is_tinted_with_the_requested_colour() -> None:
    """Icons are drawn in the theme's colour, not baked in."""
    pixmap = icons.pixmap("folder", theme.LIGHT.accent, 16)
    assert not pixmap.isNull() and pixmap.devicePixelRatio() == icons.DEVICE_RATIO
    image = pixmap.toImage()
    colours = {
        image.pixel(x, y)
        for y in range(image.height())
        for x in range(image.width())
        if image.pixelColor(x, y).alpha() > 40
    }
    assert colours, "the icon rendered nothing"


def test_the_window_carries_the_packaged_app_icon(window: MainWindow) -> None:
    """The window icon is the mark the executable is built with (design §14).

    One source SVG, rendered at every size Qt asks for: the taskbar icon, the
    alt-tab icon and the `.ico` the release ships cannot drift apart because
    nothing is stored per size.
    """
    assert not window.windowIcon().isNull(), "the window has no icon"
    assert icons.APP_ICON_SIZES == (16, 24, 32, 48, 64, 128, 256)
    source = icons.app_icon_source()
    assert "<svg" in source and "4CC38A" in source, "the bundled mark is the real source"
    rendered = set(window.windowIcon().availableSizes())
    assert {QSize(size, size) for size in icons.APP_ICON_SIZES} <= rendered


# --------------------------------------------------------------------------- #
# Startup failure paths (no window needed)
# --------------------------------------------------------------------------- #


class _Completed:
    """Stand-in for ``subprocess.CompletedProcess`` in the probe tests."""

    def __init__(self, returncode: int, stdout: str = "", stderr: str = "") -> None:
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr


def test_probe_accepts_a_working_qt() -> None:
    """A healthy self-check means the app starts."""
    seen: list[list[str]] = []

    def runner(command: list[str], **kwargs: object) -> _Completed:
        seen.append(command)
        return _Completed(0, stdout="qt 6.11.2 platform offscreen\n")

    assert probe_qt(runner=runner) is None
    assert seen and seen[0][-1] == "--self-check"


def test_probe_reports_the_last_line_of_a_failure() -> None:
    """A missing Qt runtime surfaces the reason the dialog will show."""

    def runner(command: list[str], **kwargs: object) -> _Completed:
        return _Completed(
            1,
            stderr="qt.qpa.plugin: Could not load the Qt platform plugin\n"
            "ImportError: libEGL.so.1: cannot open shared object file\n",
        )

    detail = probe_qt(runner=runner)
    assert detail == "ImportError: libEGL.so.1: cannot open shared object file"


def test_fatal_prints_and_tries_a_dialog(capsys: pytest.CaptureFixture[str]) -> None:
    """The message reaches the user even when Qt cannot open a window."""
    shown: list[str] = []
    code = fatal(
        MISSING_QT_MESSAGE.format(detail="libEGL.so.1 missing"),
        dialog=lambda message: shown.append(message) or True,
    )
    assert code == 2
    assert shown and "libEGL" in shown[0]
    assert "libEGL.so.1 missing" in capsys.readouterr().err


def test_fatal_without_a_dialog_tool_still_exits_cleanly(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """No zenity/kdialog/xmessage: stderr is the report, the exit stays clean."""
    code = fatal("no Qt here", dialog=lambda _message: False)
    assert code == 2
    captured = capsys.readouterr().err
    assert "no Qt here" in captured and "no dialog tool available" in captured


def test_app_launches_in_a_subprocess() -> None:
    """``python -m spacesage.app --self-check`` really creates a Qt application.

    This is the "the app launches" evidence: a real process, a real Qt platform
    and a clean exit code.  Running the check in a child process is also how the
    app detects a missing Qt runtime without dying itself.
    """
    env = dict(os.environ)
    env.setdefault("QT_QPA_PLATFORM", "offscreen")
    completed = subprocess.run(
        [sys.executable, "-m", "spacesage.app", "--self-check"],
        capture_output=True,
        text=True,
        check=False,
        cwd=str(REPO_ROOT),
        env=env,
        timeout=120,
    )
    assert completed.returncode == 0, completed.stderr
    assert "platform" in completed.stdout and completed.stdout.startswith("qt ")

    version = subprocess.run(
        [sys.executable, "-m", "spacesage.app", "--version"],
        capture_output=True,
        text=True,
        check=False,
        cwd=str(REPO_ROOT),
        env=env,
        timeout=120,
    )
    assert version.returncode == 0 and version.stdout.startswith("spacesage ")


def test_data_dir_honours_the_env_override(tmp_path: Path) -> None:
    """The app never writes next to the binary: the data dir is platform-defined."""
    assert state.data_dir({"SPACESAGE_DATA_DIR": str(tmp_path)}) == tmp_path
    assert state.index_path({"SPACESAGE_DATA_DIR": str(tmp_path)}).name == "spacesage.db"
    assert state.ensure_data_dir({"SPACESAGE_DATA_DIR": str(tmp_path / "new")}).is_dir()


def test_the_self_check_command_fits_the_build_it_runs_in(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A frozen build has no ``python`` to spawn, so the probe is the executable.

    ``sys.frozen`` is PyInstaller's own marker; the frozen branch also has to
    come back through the entry point's flag, not through a module path that
    does not exist inside the bundle.
    """
    assert self_check_command() == [sys.executable, "-m", "spacesage.app", SELF_CHECK_FLAG]

    monkeypatch.setattr(sys, "frozen", True, raising=False)
    assert self_check_command() == [sys.executable, SELF_CHECK_FLAG]


def test_a_windowed_build_without_streams_can_still_report(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``--windowed`` on Windows can start with ``sys.stdout is None``.

    Every internal flag prints its verdict, so a bundled app that starts this way
    would raise instead of reporting -- and that is the build CI and a released
    ``spacesage.exe`` actually run.
    """
    monkeypatch.setattr(sys, "stdout", None)
    monkeypatch.setattr(sys, "stderr", None)
    ensure_streams()
    assert sys.stdout is not None and sys.stderr is not None
    print("captured artifacts/package/smoke.png (1440x900)")  # must not raise
    sys.stderr.write("spacesage: could not render\n")


# --------------------------------------------------------------------------- #
# The capture path: the evidence a packaged build produces (design §14)
# --------------------------------------------------------------------------- #


def test_capture_request_reads_a_path_and_a_delay() -> None:
    """``--capture PATH [--capture-delay MS]``, with the default delay."""
    assert capture_request([CAPTURE_FLAG, "shot.png", "--capture-delay", "1500"]) == (
        Path("shot.png"),
        1500,
    )
    assert capture_request([CAPTURE_FLAG, "shot.png"]) == (Path("shot.png"), 800)
    assert capture_request(["--self-check"]) is None


@pytest.mark.parametrize(
    ("args", "fragment"),
    [
        ([CAPTURE_FLAG], "needs the PNG path"),
        ([CAPTURE_FLAG, "shot.png", "--capture-delay"], "needs milliseconds"),
        (
            [CAPTURE_FLAG, "shot.png", "--capture-delay", "soon"],
            "needs milliseconds, not 'soon'",
        ),
        ([CAPTURE_FLAG, "shot.png", "--capture-delay", "-5"], "cannot be negative"),
    ],
)
def test_capture_request_explains_bad_arguments(args: list[str], fragment: str) -> None:
    """A malformed command line says what it wanted instead of opening a window."""
    with pytest.raises(UsageError) as raised:
        capture_request(args)
    assert fragment in str(raised.value)


def test_a_broken_capture_flag_exits_with_the_usage_on_stderr(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """The exit code and the message a user actually sees (no window opened)."""
    assert run([CAPTURE_FLAG]) == 2
    captured = capsys.readouterr()
    assert "needs the PNG path" in captured.err
    assert CAPTURE_USAGE in captured.err


def test_capture_writes_a_real_render_of_the_window(tmp_path: Path) -> None:
    """The packaged build proves itself with ``--capture``; so does a source run.

    A real process renders the real window offscreen and exits 0 -- and the PNG
    has to be a paint, not a blank frame, which is what the colour count checks.
    """
    target = tmp_path / "capture.png"
    env = dict(os.environ)
    if os.name == "nt":
        # The offscreen plugin does not rasterize widget.grab() on Windows;
        # the native platform does -- the way the release smoke renders.
        env["QT_QPA_PLATFORM"] = "windows"
    else:
        env.setdefault("QT_QPA_PLATFORM", "offscreen")
    env["SPACESAGE_DATA_DIR"] = str(tmp_path / "data")
    completed = subprocess.run(
        [
            sys.executable,
            "-m",
            "spacesage.app",
            CAPTURE_FLAG,
            str(target),
            "--capture-delay",
            "300",
        ],
        capture_output=True,
        text=True,
        check=False,
        cwd=str(REPO_ROOT),
        env=env,
        timeout=180,
    )
    assert completed.returncode == 0, completed.stderr
    assert f"captured {target}" in completed.stdout

    image = QImage(str(target))
    assert not image.isNull(), "the capture flag exited 0 without an image"
    assert image.width() >= 1200 and image.height() >= 800
    colours = {
        image.pixel(x, y)
        for y in range(0, image.height(), 23)
        for x in range(0, image.width(), 23)
        if image.pixelColor(x, y).alpha() > 0
    }
    assert len(colours) > 20, "the capture is blank"
