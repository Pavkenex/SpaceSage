"""The Undo screen: journal history, verified revert, and its calm edges.

The fixtures here are real: :mod:`fixtures.gen_executor` plants a tree, runs a
quarantine and a move against it and journals both, so every test drives the
screen over the executor's own journal file -- the same shape the app writes
after an execute run.
"""

from __future__ import annotations

import os
import re
import shutil
from pathlib import Path
from typing import Any

import pytest
from PySide6.QtCore import Qt
from PySide6.QtWidgets import QAbstractItemView, QDialog, QPushButton

from fixtures import gen_executor
from spacesage import planning
from spacesage.app import dialogs, undo_models, widgets
from spacesage.app.views.undo_view import UndoView

TOKEN = "9de083b00dd0ca11"
"""Workspace token the journal is filed under (what the engine's plan token looks like)."""


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #


def prepared(tmp_path: Path) -> tuple[gen_executor.Scenario, Path, Path, dict[str, str]]:
    """A scenario whose quarantine and move already ran, plus a data root holding it.

    Returns ``(scenario, data root, journal filed under the root, the tree's
    snapshot before the run)``.
    """
    result = gen_executor.scenario(tmp_path / "scenario")
    pristine = gen_executor.snapshot(result.root)
    gen_executor.apply(result, "a1", "a2")
    root = tmp_path / "data"
    journal = root / planning.PLANS_DIRNAME / TOKEN / planning.JOURNAL_FILE
    journal.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(result.journal_path, journal)
    return result, root, journal, pristine


def gate(view: UndoView, qtbot: object) -> None:
    """Let every worker thread finish and the painted widgets settle."""
    qtbot.waitUntil(lambda: not view.busy(), timeout=20_000)  # type: ignore[attr-defined]
    qtbot.wait(60)  # type: ignore[attr-defined]


def open_view(root: Path, qtbot: object) -> UndoView:
    """A shown UndoView over ``root``, with its first journal load finished."""
    view = UndoView(data_root=root)
    qtbot.addWidget(view)  # type: ignore[attr-defined]
    view.resize(1280, 780)
    view.show()
    gate(view, qtbot)
    return view


def install_confirm(monkeypatch: pytest.MonkeyPatch, *, accepted: bool) -> list[dict[str, Any]]:
    """Replace the confirmation dialog; returns the arguments of every dialog shown."""
    shown: list[dict[str, Any]] = []

    class FakeConfirm:
        def __init__(self, title: str, headline: str, **kwargs: Any) -> None:
            shown.append({"title": title, "headline": headline, **kwargs})

        def exec(self) -> int:
            return int(QDialog.DialogCode.Accepted if accepted else QDialog.DialogCode.Rejected)

    monkeypatch.setattr(dialogs, "ConfirmDialog", FakeConfirm)
    return shown


def status_of(model: undo_models.UndoTableModel, seq: int) -> str:
    """The status column's text for one journal operation."""
    index = model.index_of(seq)
    assert index.isValid()
    return str(
        model.index(index.row(), undo_models.COLUMN_STATUS).data(Qt.ItemDataRole.DisplayRole)
    )


# --------------------------------------------------------------------------- #
# Loading
# --------------------------------------------------------------------------- #


def test_the_empty_state_points_at_the_plan_screen(tmp_path: Path, qtbot: object) -> None:
    """Before anything is executed: no rows, and one primary action that leaves for Plan."""
    root = tmp_path / "data"
    root.mkdir()
    view = open_view(root, qtbot)

    assert view.history_list() == ()
    assert view.current_history() is None
    assert view.table_model().rowCount() == 0
    assert view.stack.currentWidget() is view.empty_state
    assert not view.revert_selected_button.isEnabled()
    assert not view.revert_all_button.isEnabled()

    button = view.empty_state.findChild(QPushButton)
    assert button is not None
    assert button.objectName() == "Primary"
    seen: list[bool] = []
    view.goToPlan.connect(lambda: seen.append(True))
    button.click()
    assert seen == [True]


def test_a_journal_loads_one_row_per_operation(tmp_path: Path, qtbot: object) -> None:
    """Every journaled operation becomes a row: when, where, what, size, status, verify."""
    result, root, journal, _pristine = prepared(tmp_path)
    assert planning.journal_history(journal).items  # the fixture journal parses as the app reads it
    view = UndoView(data_root=tmp_path / "empty-root")
    qtbot.addWidget(view)  # type: ignore[attr-defined]
    view.resize(1280, 780)
    view.show()
    view.set_data_root(root)
    gate(view, qtbot)
    view.refresh()
    gate(view, qtbot)

    assert len(view.history_list()) == 1
    history = view.current_history()
    assert history is not None and history.path == journal
    model = view.table_model()
    assert model.rowCount() == len(history.items) == 3
    assert view.table().model() is model
    assert view.table().selectionBehavior() == QAbstractItemView.SelectionBehavior.SelectRows

    operations = {
        str(model.index(row, undo_models.COLUMN_OPERATION).data(Qt.ItemDataRole.DisplayRole))
        for row in range(model.rowCount())
    }
    assert operations == {
        "Quarantine -> move back",
        "Move -> move back",
        "Link -> remove link",
    }
    paths = {
        str(model.index(row, undo_models.COLUMN_PATH).data(Qt.ItemDataRole.DisplayRole))
        for row in range(model.rowCount())
    }
    assert paths == {str(result.root / "cache"), str(result.root / "media")}

    when = str(model.index(0, undo_models.COLUMN_WHEN).data(Qt.ItemDataRole.DisplayRole))
    assert re.fullmatch(r"\d{4}-\d{2}-\d{2} \d{2}:\d{2}", when)
    size = str(model.index(0, undo_models.COLUMN_SIZE).data(Qt.ItemDataRole.DisplayRole))
    assert size.endswith(("B", "iB"))
    assert model.index(0, undo_models.COLUMN_STATUS).data(Qt.ItemDataRole.DisplayRole) == "Pending"
    assert (
        model.index(0, undo_models.COLUMN_VERIFICATION).data(Qt.ItemDataRole.DisplayRole)
        == "verified"
    )
    item = model.row_at(model.index(0, undo_models.COLUMN_PATH))
    tooltip = str(model.index(0, undo_models.COLUMN_PATH).data(Qt.ItemDataRole.ToolTipRole))
    assert item is not None and item.path in tooltip
    assert "Note:" in tooltip and "Verification:" in tooltip


def test_open_journal_adds_a_file_that_lives_elsewhere(tmp_path: Path, qtbot: object) -> None:
    """A journal outside the app's workspaces is listed and selected once it is read."""
    result = gen_executor.scenario(tmp_path / "scenario")
    gen_executor.apply(result, "a1", "a2")
    root = tmp_path / "data"
    root.mkdir()
    view = open_view(root, qtbot)
    assert view.history_list() == ()

    view.open_journal(result.journal_path)
    gate(view, qtbot)

    assert len(view.history_list()) == 1
    history = view.current_history()
    assert history is not None and history.path == result.journal_path
    assert view.table_model().rowCount() == 3
    assert view.table_model().pending_seqs()


def test_the_default_check_state_is_every_pending_operation(tmp_path: Path, qtbot: object) -> None:
    """Loading a journal checks every operation that still awaits a reversal."""
    _result, root, _journal, _pristine = prepared(tmp_path)
    view = open_view(root, qtbot)
    model = view.table_model()

    assert model.pending_seqs()
    assert set(model.checked_seqs()) == set(model.pending_seqs())
    assert view.revert_selected_button.isEnabled()
    assert view.revert_all_button.isEnabled()
    assert "checked" in view.selection_label.text()
    for row in range(model.rowCount()):
        index = model.index(row, undo_models.COLUMN_SELECT)
        assert index.data(Qt.ItemDataRole.CheckStateRole) == Qt.CheckState.Checked
        assert index.data(undo_models.ROLE_CHECK) == "checked"

    model.clear_checks()
    assert model.checked_seqs() == ()
    assert not view.revert_selected_button.isEnabled()
    assert "Nothing checked" in view.selection_label.text()


def test_space_checks_the_operation_under_the_cursor(tmp_path: Path, qtbot: object) -> None:
    """Keyboard: Space checks or unchecks the operation the cursor is on (§9.1)."""
    _result, root, _journal, _pristine = prepared(tmp_path)
    view = open_view(root, qtbot)
    model = view.table_model()
    table = view.table()
    model.clear_checks()
    assert model.checked_seqs() == ()

    first = model.pending_seqs()[0]
    table.setCurrentIndex(model.index_of(first))
    qtbot.keyClick(table, Qt.Key.Key_Space)  # type: ignore[attr-defined]
    assert model.checked_seqs() == (first,)
    assert view.revert_selected_button.isEnabled()

    qtbot.keyClick(table, Qt.Key.Key_Space)  # type: ignore[attr-defined]
    assert model.checked_seqs() == ()
    assert not view.revert_selected_button.isEnabled()


# --------------------------------------------------------------------------- #
# Reverting
# --------------------------------------------------------------------------- #


def test_revert_selected_leaves_the_other_operations_pending(
    tmp_path: Path, qtbot: object, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Revert selected touches exactly the checked operations; the rest stay pending."""
    result, root, _journal, _pristine = prepared(tmp_path)
    view = open_view(root, qtbot)
    model = view.table_model()
    history = view.current_history()
    assert history is not None
    link = history.items[-1]
    assert link.op == "link"

    model.clear_checks()
    assert model.set_checked(link.seq, True)
    assert view.revert_selected_button.isEnabled()

    shown = install_confirm(monkeypatch, accepted=True)
    qtbot.mouseClick(view.revert_selected_button, Qt.MouseButton.LeftButton)  # type: ignore[attr-defined]
    gate(view, qtbot)

    assert len(shown) == 1
    assert shown[0]["danger"] is True
    assert shown[0]["accept_label"]
    assert len(shown[0]["lines"]) == 1
    assert "Link -> remove link" in shown[0]["lines"][0]

    after = view.current_history()
    assert after is not None
    statuses = {item.seq: item.status for item in after.items}
    assert sorted(statuses.values()) == ["pending", "pending", "reversed"]
    assert statuses[link.seq] == "reversed"
    assert not os.path.lexists(result.root / "media")  # the link is gone
    assert (result.target / "Moved" / "media" / "movie.mp4").is_file()  # still at the target
    assert not (result.root / "cache").exists()  # the quarantine was left alone

    assert status_of(model, link.seq) == "Reversed"
    settled = model.index_of(link.seq)
    assert (
        model.index(settled.row(), undo_models.COLUMN_SELECT).data(Qt.ItemDataRole.CheckStateRole)
        is None
    )
    assert model.set_checked(link.seq, True) is False  # a settled operation is not checkable


def test_revert_all_restores_the_tree(tmp_path: Path, qtbot: object) -> None:
    """Revert all reverses every operation and puts the tree back byte for byte."""
    result, root, _journal, before = prepared(tmp_path)
    view = open_view(root, qtbot)
    assert gen_executor.snapshot(result.root) != before  # the run did change the tree
    messages: list[str] = []
    view.statusMessage.connect(messages.append)

    assert view.revert() is True
    gate(view, qtbot)

    history = view.current_history()
    assert history is not None
    assert {item.status for item in history.items} == {"reversed"}
    assert history.pending() == ()
    assert gen_executor.snapshot(result.root) == before
    assert any("3 operations reverted" in message for message in messages)
    assert any("restored" in message for message in messages)

    model = view.table_model()
    for row in range(model.rowCount()):
        assert model.index(row, undo_models.COLUMN_STATUS).data(Qt.ItemDataRole.DisplayRole) == (
            "Reversed"
        )
        assert (
            model.index(row, undo_models.COLUMN_VERIFICATION).data(Qt.ItemDataRole.DisplayRole)
            == "verified"
        )
    assert model.pending_seqs() == ()
    assert not view.revert_all_button.isEnabled()
    assert not view.revert_selected_button.isEnabled()


def test_a_second_revert_reports_nothing_to_do(tmp_path: Path, qtbot: object) -> None:
    """A journal with nothing left to reverse says so, calmly and without a worker."""
    _result, root, _journal, _pristine = prepared(tmp_path)
    view = open_view(root, qtbot)
    messages: list[str] = []
    view.statusMessage.connect(messages.append)

    assert view.revert() is True
    gate(view, qtbot)

    messages.clear()
    assert view.revert() is False
    assert view.revert(seqs=[1, 2, 3]) is False
    gate(view, qtbot)

    assert any("nothing to revert" in message.lower() for message in messages)
    assert any("already reversed" in message.lower() for message in messages)
    assert not view.busy()
    history = view.current_history()
    assert history is not None and history.pending() == ()
    assert view.table_model().pending_seqs() == ()


def test_confirming_can_be_cancelled_without_any_change(
    tmp_path: Path, qtbot: object, monkeypatch: pytest.MonkeyPatch
) -> None:
    """confirm=True asks first; dismissing the dialog changes nothing at all."""
    result, root, _journal, _pristine = prepared(tmp_path)
    view = open_view(root, qtbot)
    after_apply = gen_executor.snapshot(result.root)
    shown = install_confirm(monkeypatch, accepted=False)
    messages: list[str] = []
    view.statusMessage.connect(messages.append)

    assert view.revert(confirm=True) is False
    gate(view, qtbot)

    assert len(shown) == 1
    assert shown[0]["danger"] is True
    assert shown[0]["accept_label"]
    assert len(shown[0]["lines"]) == 3
    assert all(line for line in shown[0]["lines"])
    assert gen_executor.snapshot(result.root) == after_apply
    history = view.current_history()
    assert history is not None
    assert {item.status for item in history.items} == {"pending"}
    assert any("cancelled" in message for message in messages)
    assert not view.busy()


def test_a_broken_journal_is_shown_not_crashed(tmp_path: Path, qtbot: object) -> None:
    """A journal that cannot be parsed is listed with its error; revert refuses calmly."""
    root = tmp_path / "data"
    journal = root / planning.PLANS_DIRNAME / "broken" / planning.JOURNAL_FILE
    journal.parent.mkdir(parents=True, exist_ok=True)
    journal.write_text("not json\n", encoding="utf-8")

    view = open_view(root, qtbot)

    assert len(view.history_list()) == 1
    history = view.current_history()
    assert history is not None and history.path == journal
    assert "not JSON" in history.error
    assert view.picker.count() == 1
    assert view.table_model().rowCount() == 0
    assert view.warning_list.count() == 1
    banners = view.warning_list.findChildren(widgets.WarningBanner)
    assert banners and "not JSON" in banners[0].text()

    messages: list[str] = []
    view.statusMessage.connect(messages.append)
    assert view.revert() is False
    assert any("could not be read" in message for message in messages)
    assert not view.busy()


# --------------------------------------------------------------------------- #
# Chrome
# --------------------------------------------------------------------------- #


def test_every_button_carries_a_tooltip(tmp_path: Path, qtbot: object) -> None:
    """Keyboard and mouse users both get told what each control does."""
    _result, root, _journal, _pristine = prepared(tmp_path)
    view = open_view(root, qtbot)

    assert view.revert_selected_button.toolTip()
    assert view.revert_all_button.toolTip()
    assert view.open_button.objectName() == "Quiet"
    assert view.open_button.toolTip()
    assert view.refresh_button.toolTip()
    buttons = view.findChildren(QPushButton)
    assert len(buttons) >= 5
    for button in buttons:
        assert button.toolTip(), f"{button.text()} has no tooltip"


def test_a_theme_change_keeps_the_table_intact(tmp_path: Path, qtbot: object) -> None:
    """Re-rendering the hand-painted cells loses no rows and no checks."""
    _result, root, _journal, _pristine = prepared(tmp_path)
    view = open_view(root, qtbot)
    model = view.table_model()
    rows = model.rowCount()

    view.apply_theme()
    assert model.rowCount() == rows
    assert model.pending_seqs()
    assert model.checked_seqs()
    assert status_of(model, model.pending_seqs()[0]) == "Pending"
