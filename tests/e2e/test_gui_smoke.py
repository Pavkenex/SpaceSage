"""[S12] The desktop app, end to end, on the planted full disk.

The GUI suite (``tests/gui``) proves each screen on a small crafted sandbox and
its own fixture export.  This pass proves something else: the *whole app* -- the
import screen's own ingest, the ranked list, the plan, the apply, the undo --
on the same "full disk" scenario the engine pass runs on, with the five screens
rendered to ``artifacts/e2e`` as the release evidence.

Nothing is staged for it: the window is handed the sandbox (CSV, index path,
data root, target drive, quarantine) and a user's route -- *Analyze*, *Select
visible*, *Build plan*, *Dry-run preview*, *Execute*, *Undo*, *Settings* --
does the rest.  The pass fails if any screen reports a problem (unhandled
exceptions, Qt warnings beyond the offscreen platform's known noise, or any
dialog the app had to raise), and it ends by comparing the tree byte for byte
with what was planted.
"""

from __future__ import annotations

import os
import sys
import threading
import traceback
from collections.abc import Callable
from pathlib import Path
from typing import Any

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest
import scenario
from PySide6.QtCore import QtMsgType, qInstallMessageHandler
from PySide6.QtWidgets import QDialog

from conftest import REPO_ROOT
from spacesage import ai as ai_layer
from spacesage import planning
from spacesage.app import ai_models, dialogs, state, theme, widgets
from spacesage.app.main import create_window
from spacesage.app.windows import MainWindow

#: The offscreen platform and this container are noisy; these are the messages
#: that are not the app's doing (anything else in the run log fails the pass).
BENIGN_QT_MESSAGES: tuple[str, ...] = (
    "propagateSizeHints",  # QPA: the offscreen plugin cannot resize windows
    "XDG_RUNTIME_DIR",  # QStandardPaths: no session runtime dir in CI
    "Populating font family aliases",  # fontconfig warm-up
    "QStandardPaths",
    "QLayout: Attempting to add QLayout",  # known shell noise (see tests/gui)
)

MIN_SIZE_TO_ANALYSE = "10 MiB"
"""The smallest floor the import screen offers; the scenario clears it everywhere."""


# --------------------------------------------------------------------------- #
# The run log: everything the app would have said
# --------------------------------------------------------------------------- #


class RunLog:
    """Qt messages and unhandled exceptions, collected while the pass runs."""

    def __init__(self) -> None:
        self.messages: list[str] = []
        self._handler: Any = None
        self._hooks: tuple[Any, Any] = (sys.excepthook, threading.excepthook)

    def install(self) -> RunLog:
        self._handler = qInstallMessageHandler(self._on_message)
        sys.excepthook = self._on_exception
        threading.excepthook = self._on_thread_exception
        return self

    def close(self) -> None:
        qInstallMessageHandler(self._handler)
        sys.excepthook, threading.excepthook = self._hooks

    def _on_message(self, mode: QtMsgType, context: object, message: str) -> None:
        del context
        self.messages.append(f"qt {int(mode)}: {message}")

    def _on_exception(self, exc_type: Any, exc: Any, tb: Any) -> None:
        self.messages.append("".join(traceback.format_exception(exc_type, exc, tb)))

    def _on_thread_exception(self, args: Any) -> None:
        self._on_exception(args.exc_type, args.exc_value, args.exc_traceback)

    def problems(self) -> list[str]:
        """Everything that is not known offscreen noise (a traceback always is one)."""
        return [
            message
            for message in self.messages
            if "Traceback" in message or not any(known in message for known in BENIGN_QT_MESSAGES)
        ]


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #


def wait(
    qtbot: object,
    predicate: Callable[[], bool],
    *,
    what: str = "the worker",
    timeout: int = 180_000,
) -> None:
    """Wait for a background worker to finish, then let the paint land."""
    try:
        qtbot.waitUntil(predicate, timeout=timeout)  # type: ignore[attr-defined]
    except Exception as exc:  # pragma: no cover - a hung worker is the failure
        raise AssertionError(f"timed out waiting for {what}: {exc}") from None
    qtbot.wait(80)  # type: ignore[attr-defined]


def settle(qtbot: object, ms: int = 260) -> None:
    """Let layout, paint and the entrance fade happen."""
    qtbot.wait(ms)  # type: ignore[attr-defined]


def open_window(
    smoke: scenario.FullDisk, tmp_path: Path, qapp: object, qtbot: object
) -> MainWindow:
    """A shown main window over the smoke sandbox: its CSV, index and sandbox dirs."""
    settings = state.Settings.persisted(tmp_path / "settings.ini")
    settings.set_target_drive(str(smoke.target))
    settings.set_reserve_bytes(0)
    settings.set_quarantine_dir(str(smoke.quarantine))
    manager = theme.ThemeManager(qapp, mode=theme.MODE_LIGHT)
    ai_service = ai_models.AIService(config=ai_layer.AIConfig(cache_dir=str(tmp_path / "ai-cache")))
    window = create_window(
        qapp,
        settings,
        db_path=smoke.gui_index_db,
        data_root=smoke.data_root,
        theme_manager=manager,
        ai_service=ai_service,
    )
    qtbot.addWidget(window)  # type: ignore[attr-defined]
    window.resize(1440, 900)
    window.show()
    settle(qtbot)
    return window


def accept_confirms(monkeypatch: pytest.MonkeyPatch) -> list[dict[str, Any]]:
    """Replace the confirmation dialog with one that always accepts."""
    shown: list[dict[str, Any]] = []

    class FakeConfirm:
        def __init__(self, title: str, headline: str, **kwargs: Any) -> None:
            shown.append({"title": title, "headline": headline, **kwargs})

        def exec(self) -> int:
            return int(QDialog.DialogCode.Accepted)

    monkeypatch.setattr(dialogs, "ConfirmDialog", FakeConfirm)
    return shown


def visible_toasts(window: MainWindow) -> list[widgets.Toast]:
    """The toasts the app is showing right now (a closed one is hidden, not gone)."""
    return [toast for toast in window.findChildren(widgets.Toast) if toast.isVisible()]


# --------------------------------------------------------------------------- #
# The pass
# --------------------------------------------------------------------------- #


def test_the_app_runs_the_whole_loop_on_the_full_disk(
    smoke_disk: scenario.FullDisk,
    qtbot: object,
    qapp: object,
    tmp_path: Path,
    artifacts: Path,
    monkeypatch: pytest.MonkeyPatch,
    no_blocking_dialogs: list[tuple[str, str]],
    evidence: Callable[[str, str], Path],
    grab_png: Callable[..., Path],
) -> None:
    """Every screen, one real run, and a tree that ends up exactly as planted."""
    before = scenario.snapshot(smoke_disk.disk)
    shots: list[Path] = []
    log = RunLog().install()
    window: MainWindow | None = None
    try:
        window = open_window(smoke_disk, tmp_path, qapp, qtbot)

        # -- screen 1: import.  The app ingests and ranks this disk itself. ---- #
        assert window.navigate("import"), "the shell has no import screen"
        assert window._nav_buttons["import"].isChecked(), "the rail does not mark the screen"
        import_view = window.import_view
        assert Path(import_view.db_path) == smoke_disk.gui_index_db
        assert not smoke_disk.gui_index_db.exists(), "the smoke run must build its own index"
        import_view.set_csv(smoke_disk.csv_path)
        index = import_view.min_size_combo.findText(MIN_SIZE_TO_ANALYSE)
        assert index >= 0, f"the screen does not offer {MIN_SIZE_TO_ANALYSE}"
        import_view.min_size_combo.setCurrentIndex(index)
        assert import_view.min_size() == 10 * 1024**2
        settle(qtbot)
        shots.append(grab_png(window, artifacts / "smoke-1-import.png"))

        import_view.analyze()
        wait(qtbot, lambda: not import_view.busy, what="the analysis")
        assert smoke_disk.gui_index_db.is_file(), "the app did not build an index"

        listing = window.listing()
        assert listing is not None and len(listing) > 0, "the analysis listed nothing"
        assert all(str(row.path).startswith(str(smoke_disk.disk)) for row in listing.rows)
        assert any(row.action == "MOVE" for row in listing.rows), "no move candidate listed"
        assert window.current_page() == "opportunities", "the app stays on the analysis"
        assert window._nav_buttons["opportunities"].isChecked(), "the rail lags behind"
        assert not window._nav_buttons["import"].isChecked()
        said = [toast.text() for toast in visible_toasts(window)]
        assert said and said[0].startswith("Analysis done:"), "the app did not report the analysis"
        evidence(
            "smoke-analysis.txt",
            f"{listing.heading()}\n"
            + "\n".join(
                f"{row.size:>12}  {row.tier}  {row.state:<12} {row.action:<18} {row.path}"
                for row in listing.rows
            ),
        )

        # -- screen 2: the ranked list, checked the way a user checks it. ----- #
        wait(qtbot, lambda: not visible_toasts(window), what="the toast")
        opportunities_view = window.opportunities_view
        assert opportunities_view.select_button.isEnabled()
        opportunities_view.select_button.click()
        model = opportunities_view.table_model()
        checked = len(model.selection)
        assert checked > 0, "Select visible checked nothing"
        assert checked <= len(listing)
        selected_bytes = model.selection.gain
        assert selected_bytes > 0
        shots.append(grab_png(window, artifacts / "smoke-2-opportunities.png"))
        footer = opportunities_view.selection_label
        settle(qtbot)
        # A bar figure that is longer than the space it got would be clipped with
        # no sign of it (a QLabel paints past its edge); the screen may only be
        # called good when the figure is either painted in full or elided by one
        # of the app's own eliding labels.
        assert footer.width() >= footer.fontMetrics().horizontalAdvance(footer.text()), (
            "the checked-rows figure is clipped: "
            f"{footer.width()}px painted, "
            f"{footer.fontMetrics().horizontalAdvance(footer.text())}px needed"
        )
        evidence(
            "smoke-footer.txt",
            "\n".join(
                [
                    f"selection text : {footer.text()!r}",
                    f"selection width: {footer.width()}px painted, "
                    f"{footer.fontMetrics().horizontalAdvance(footer.text())}px needed, "
                    f"font {footer.font().family()} {footer.font().pixelSize()}px",
                    f"hint text      : {opportunities_view.cascade_hint.text()!r}",
                    f"hint width     : {opportunities_view.cascade_hint.width()}px painted, "
                    f"{opportunities_view.cascade_hint.fontMetrics().horizontalAdvance(opportunities_view.cascade_hint.text())}"
                    "px needed",
                    f"window         : {window.width()}x{window.height()}",
                    f"toasts visible : {len(visible_toasts(window))}",
                ]
            ),
        )

        # -- screen 3: the plan, then the dry run and the run itself. ---------- #
        opportunities_view.build_plan_button.click()
        plan_view = window.plan_page.plan
        wait(qtbot, lambda: not plan_view.busy(), what="the plan")
        assert window.current_page() == "plan"
        session = plan_view.session()
        assert session is not None and plan_view.draft() is not None
        draft = session.draft
        assert len(draft.items) > 0
        assert plan_view.table_model().rowCount() == len(draft.items)
        assert plan_view.plan_id_label.text() == f"plan {session.plan_id}"
        executable = draft.executable_ids()
        assert executable, "the checked rows produced nothing executable"
        assert plan_view.approved_ids() == executable
        # One action per thing: an executable action is never nested inside
        # another one.  An *advisory* parent may overlap (the app suggests the
        # native tool for the game library and still quarantines its cache --
        # advice does nothing, so nothing acts twice).
        executable_paths = [str(item.action.path) for item in draft.items if item.executable]
        for path in executable_paths:
            assert not any(
                other != path and path.startswith(other.rstrip("/") + "/")
                for other in executable_paths
            ), f"{path} is nested inside another executable action"
        shots.append(grab_png(window, artifacts / "smoke-3-plan.png"))

        assert plan_view.dry_run(show=False)
        wait(qtbot, lambda: not plan_view.busy(), what="the dry run")
        preview = plan_view.preview_report()
        assert preview is not None, "the dry run adopted no report"
        assert preview.ok(), preview.render_text()
        assert len(preview.ops) == len(executable)
        assert all(op.outcome == "planned" for op in preview.ops)
        assert scenario.snapshot(smoke_disk.disk) == before, "the dry run touched the disk"

        accepted = accept_confirms(monkeypatch)
        assert plan_view.execute()
        wait(qtbot, lambda: not plan_view.busy(), what="the execution")
        assert len(accepted) == 1 and accepted[0]["title"] == "Execute the plan"
        run = plan_view.apply_report()
        assert run is not None, "the execution adopted no report"
        assert run.ok(), run.render_text()
        assert {op.action_id for op in run.ops} <= set(executable), "advice never runs"
        assert run.reclaimed_bytes() > 0
        executed = scenario.snapshot(smoke_disk.disk)
        assert executed != before, "the run changed nothing"
        journal = session.workspace.journal_path
        assert journal.is_file(), "the run wrote no journal"
        history = planning.journal_history(journal)
        assert history.pending() and history.reclaimed_bytes() == run.reclaimed_bytes()

        # -- screen 4: undo, one click for the whole run. ---------------------- #
        page = window.plan_page
        page.show_undo()
        wait(qtbot, lambda: not page.undo.busy(), what="the undo screen")
        assert page.current() == "undo"
        shown_history = page.undo.current_history()
        assert shown_history is not None and shown_history.path == journal
        assert page.undo.revert_all_button.isEnabled()
        shots.append(grab_png(window, artifacts / "smoke-4-undo.png"))

        accepted = accept_confirms(monkeypatch)
        page.undo.revert_all_button.click()
        wait(qtbot, lambda: not page.undo.busy(), what="the undo run")
        assert len(accepted) == 1, "undo asked for no confirmation"
        assert scenario.snapshot(smoke_disk.disk) == before, "undo did not restore the tree"
        after = planning.journal_history(journal)
        assert not after.pending()
        assert after.counts()["reversed"] == len(after.items)
        assert after.restored_bytes() == run.reclaimed_bytes()

        # -- screen 5: settings, the last one a release pass has to look at. --- #
        assert window.navigate("settings")
        settle(qtbot)
        shots.append(grab_png(window, artifacts / "smoke-5-settings.png"))

        # -- what the run said, and what it left behind ----------------------- #
        assert no_blocking_dialogs == [], "the app had to tell the user something failed"
        assert log.problems() == [], "\n".join(log.problems())
        assert len(shots) == 5, "every screen has to leave a render behind"
        for shot in shots:
            assert shot.is_file() and shot.stat().st_size > 12_000, f"{shot.name} is not a render"
        evidence(
            "smoke-run.txt",
            "\n".join(
                [
                    f"scenario         {smoke_disk.describe()}",
                    f"listing          {listing.heading()}",
                    f"checked rows     {checked}",
                    f"plan             {session.plan_id}",
                    f"actions          {len(draft.items)} ({len(executable)} executable)",
                    f"planned bytes    {preview.reclaimed_bytes()}",
                    f"reclaimed bytes  {run.reclaimed_bytes()}",
                    f"restored bytes   {after.restored_bytes()}",
                    f"journal          {journal}",
                    f"tree before      {len(before)} entries",
                    f"tree after undo  {len(scenario.snapshot(smoke_disk.disk))} entries"
                    " (identical)",
                    f"screenshots      {artifacts}",
                ]
            ),
        )
    finally:
        log.close()
        if window is not None:
            window.close()
            window.deleteLater()


def test_the_app_boots_and_renders_its_first_screen(
    smoke_disk: scenario.FullDisk,
    artifacts: Path,
    evidence: Callable[[str, str], Path],
) -> None:
    """The real entry point, launched the way a user launches it, in a subprocess.

    The in-process pass drives the window's widgets; this one proves the app the
    project actually ships (``python -m spacesage.app``) starts, paints and exits
    on its own -- the check that catches an import that only fails on boot.  It
    runs in its own data/config/cache directories and with the AI layer off.
    """
    import subprocess

    render = artifacts / "smoke-boot.png"
    env = {
        **os.environ,
        "QT_QPA_PLATFORM": "offscreen",
        "SPACESAGE_DATA_DIR": str(smoke_disk.root / "boot-data"),
        "SPACESAGE_AI_OFF": "1",
        "XDG_CONFIG_HOME": str(smoke_disk.root / "boot-config"),
        "XDG_CACHE_HOME": str(smoke_disk.root / "boot-cache"),
    }
    result = subprocess.run(
        [sys.executable, "-m", "spacesage.app", "--capture", str(render)],
        capture_output=True,
        text=True,
        cwd=str(REPO_ROOT),
        env=env,
        timeout=300,
    )
    assert result.returncode == 0, f"boot failed: {result.stderr[-4000:]}"
    assert "Traceback" not in result.stderr, result.stderr[-4000:]
    assert render.is_file() and render.stat().st_size > 12_000, "the app rendered nothing"
    evidence("smoke-boot-stderr.txt", result.stderr or "(silent)")
