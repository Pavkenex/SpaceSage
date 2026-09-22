"""The app's last line of defense: a crash is a log file and a real message.

A ``--windowed`` build has no console, so an unhandled exception used to
reach the user as the bootloader's one-line "Failed to execute script" box --
no traceback, nothing to forward.  These tests pin the replacement: the
traceback lands in ``<data dir>/logs/crash-*.log`` and the user gets a
message that says where it is.
"""

from __future__ import annotations

import tempfile
from pathlib import Path

import pytest

from spacesage.app import crash
from spacesage.app.main import run as app_run
from spacesage.app.main import state as app_state

# --------------------------------------------------------------------------- #
# The crash log
# --------------------------------------------------------------------------- #


def test_write_crash_log_records_the_traceback(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The log carries the traceback plus the context a maintainer needs."""
    monkeypatch.setenv("SPACESAGE_DATA_DIR", str(tmp_path))
    try:
        raise RuntimeError("the disk ate the index")
    except RuntimeError as exc:
        path = crash.write_crash_log(exc)
    assert path is not None and path.is_file()
    assert path.parent == tmp_path / "logs"
    text = path.read_text(encoding="utf-8")
    assert "RuntimeError: the disk ate the index" in text
    assert "Traceback (most recent call last):" in text
    assert "version:" in text and "argv:" in text


def test_write_crash_log_falls_back_to_temp_when_the_data_dir_is_not_a_directory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A machine whose data directory is unusable still gets the report."""
    blocker = tmp_path / "not-a-dir"
    blocker.write_text("a file, not a directory", encoding="utf-8")
    monkeypatch.setattr(crash, "crash_log_dir", lambda env=None: blocker / "logs")
    monkeypatch.setattr(tempfile, "gettempdir", lambda: str(tmp_path / "temp"))
    try:
        raise OSError("the data directory is gone")
    except OSError as exc:
        path = crash.write_crash_log(exc)
    assert path is not None and path.is_file()
    assert path.parent == tmp_path / "temp" / "spacesage-logs"
    assert "the data directory is gone" in path.read_text(encoding="utf-8")


# --------------------------------------------------------------------------- #
# The message the user sees
# --------------------------------------------------------------------------- #


def test_report_crash_points_at_the_log(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """The message names the exception and the file to forward."""
    monkeypatch.setenv("SPACESAGE_DATA_DIR", str(tmp_path))
    shown: list[str] = []
    code = crash.report_crash(
        RuntimeError("boom"), dialog=lambda message: shown.append(message) or True
    )
    assert code == crash.CRASH_EXIT_CODE == 3
    assert shown and "RuntimeError: boom" in shown[0]
    assert "crash-" in shown[0] and str(tmp_path) in shown[0]
    assert "boom" in capsys.readouterr().err


def test_fallback_dialog_prefers_a_native_message_box() -> None:
    """On Windows the message box is the first reporter, not the last."""
    shown: list[str] = []
    shown_ok = crash.fallback_dialog("hello", message_box=lambda m: shown.append(m) or True)
    assert shown_ok is True
    assert shown == ["hello"]


def test_fallback_dialog_reports_false_when_nothing_can_show_it(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """No box, no dialog tools: the message on stderr is the report."""
    monkeypatch.setattr(crash.shutil, "which", lambda name: None)
    assert crash.fallback_dialog("hello", message_box=lambda _m: False) is False


def test_windows_message_box_is_a_no_op_off_windows() -> None:
    """The POSIX hosts never grow a MessageBox; the call declines cleanly."""
    if crash.os.name == "nt":  # pragma: no cover - the windows job runs the real one
        pytest.skip("this assertion is for the POSIX hosts")
    assert crash.windows_message_box("hi") is False


# --------------------------------------------------------------------------- #
# run() itself
# --------------------------------------------------------------------------- #


def test_run_reports_an_unhandled_startup_crash(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, qtbot: object
) -> None:
    """An exception on the way to the window never reaches the bootloader."""
    monkeypatch.setenv("SPACESAGE_DATA_DIR", str(tmp_path))
    monkeypatch.setattr("spacesage.app.main.probe_qt", lambda: None)
    shown: list[str] = []
    monkeypatch.setattr(crash, "fallback_dialog", lambda message: shown.append(message) or True)

    def boom(cls: type) -> None:
        raise RuntimeError("settings are gone")

    monkeypatch.setattr(app_state.Settings, "persisted", classmethod(boom))
    code = app_run([])
    assert code == crash.CRASH_EXIT_CODE
    assert shown and "settings are gone" in shown[0]
    logs = list((tmp_path / "logs").glob("crash-*.log"))
    assert logs and "settings are gone" in logs[0].read_text(encoding="utf-8")
