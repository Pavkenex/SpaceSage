"""Import screen: drag & drop, parameters, and the worker-thread analysis."""

from __future__ import annotations

from pathlib import Path

import pytest
from PySide6.QtWidgets import QApplication, QMessageBox

from spacesage import opportunities
from spacesage.app import state
from spacesage.app.main import create_window
from spacesage.app.views.import_view import ImportView


def test_analysis_runs_off_the_ui_thread(
    qtbot: object, settings: state.Settings, fixture_csv: Path, tmp_path: Path
) -> None:
    """Analyze ingests and ranks on a worker; the screen stays responsive."""
    view = ImportView(settings, db_path=tmp_path / "index.db")
    qtbot.addWidget(view)  # type: ignore[attr-defined]
    view.resize(1200, 800)
    view.show()
    view.min_size_combo.setCurrentIndex(0)  # the fixture's entries are small
    view.set_csv(fixture_csv)

    assert view.csv_path() == str(fixture_csv)
    assert view.analyze_button.isEnabled()

    with qtbot.waitSignal(view.analysisReady, timeout=120_000) as blocker:  # type: ignore[attr-defined]
        view.analyze()
    assert view.busy is False

    listing = blocker.args[0]
    assert isinstance(listing, opportunities.OpportunityList)
    assert len(listing) > 0
    assert view.progress.value() == 100
    assert view.db_path.is_file()


def test_analysis_without_an_export_is_refused(
    qtbot: object, settings: state.Settings, tmp_path: Path
) -> None:
    """Analyze without a CSV: a toast, no worker, no index written."""
    view = ImportView(settings, db_path=tmp_path / "index.db")
    qtbot.addWidget(view)  # type: ignore[attr-defined]
    assert view.analyze_button.isEnabled() is False
    view.analyze()
    assert view.busy is False
    assert not view.db_path.is_file()


def test_a_bad_export_fails_loudly_and_resets_the_controls(
    qtbot: object, settings: state.Settings, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An unreadable export surfaces a dialog (never a crash) and frees the UI."""
    shown: list[str] = []

    def fake_exec(box: QMessageBox) -> int:
        shown.append(box.informativeText())
        return int(QMessageBox.StandardButton.Ok)

    monkeypatch.setattr(QMessageBox, "exec", fake_exec)
    broken = tmp_path / "not-a-wiztree.csv"
    broken.write_text("this is not an export\n", encoding="utf-8")

    view = ImportView(settings, db_path=tmp_path / "index.db")
    qtbot.addWidget(view)  # type: ignore[attr-defined]
    view.min_size_combo.setCurrentIndex(0)
    view.set_csv(broken)
    edges: list[bool] = []
    ready: list[object] = []
    view.busyChanged.connect(edges.append)
    view.analysisReady.connect(ready.append)
    view.analyze()
    qtbot.waitUntil(lambda: not view.busy, timeout=120_000)  # type: ignore[attr-defined]
    # Both edges happened: the worker started (True) and the failure freed the UI
    # (False).  A failed run reports and stops -- it hands no listing to the UI.
    assert edges == [True, False]
    assert not ready
    assert shown, "the failure was not reported to the user"
    assert shown[0].strip(), "the dialog carried no reason"
    assert view.analyze_button.isEnabled()


def test_the_existing_index_can_be_reused(
    qtbot: object, settings: state.Settings, fixture_csv: Path, tmp_path: Path
) -> None:
    """Second launch: rank the index that is already there, no re-import."""
    db_path = tmp_path / "index.db"
    view = ImportView(settings, db_path=db_path)
    qtbot.addWidget(view)  # type: ignore[attr-defined]
    view.min_size_combo.setCurrentIndex(0)
    view.set_csv(fixture_csv)
    with qtbot.waitSignal(view.analysisReady, timeout=120_000):  # type: ignore[attr-defined]
        view.analyze()
    assert view.reuse_button.isEnabled()

    with qtbot.waitSignal(view.analysisReady, timeout=120_000) as second:  # type: ignore[attr-defined]
        view.analyze(reuse_index=True)
    listing = second.args[0]
    assert isinstance(listing, opportunities.OpportunityList)
    assert len(listing) > 0


def test_the_window_reports_the_analysis_in_the_status_bar(
    qtbot: object,
    qapp: QApplication,
    settings: state.Settings,
    fixture_index: Path,
    fixture_listing: opportunities.OpportunityList,
) -> None:
    """A finished analysis lands on the Opportunities screen, not in a dialog."""
    window = create_window(qapp, settings, db_path=fixture_index)
    qtbot.addWidget(window)  # type: ignore[attr-defined]
    window.show()
    window.set_listing(fixture_listing)
    assert window.current_page() == "import"  # adopting a listing does not navigate
    window.navigate("opportunities")
    assert "opportunities" in window.status_dataset.text()
    assert window.listing() is fixture_listing
    window.close()


def test_min_size_and_target_are_persisted(
    qtbot: object, settings: state.Settings, tmp_path: Path
) -> None:
    """The parameters the user picks survive the screen (they feed the plan)."""
    view = ImportView(settings, db_path=tmp_path / "index.db")
    qtbot.addWidget(view)  # type: ignore[attr-defined]
    view.min_size_combo.setCurrentIndex(2)
    view.target_combo.setCurrentText("D:")
    view.reserve_combo.setCurrentIndex(1)
    assert view.min_size() >= 10 * 1024**2
    assert view.target_drive() == "D:"
    assert view.reserve_bytes() > 0
