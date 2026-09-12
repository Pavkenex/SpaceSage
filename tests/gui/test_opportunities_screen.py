"""The core screen: the ranked list, its cascade, the filters and the details pane."""

from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QApplication

from spacesage import opportunities
from spacesage.app import models
from spacesage.app.windows import MainWindow

VIDEOS = "C:\\Users\\Alice\\Videos"
VIDEOS_CLIP = "C:\\Users\\Alice\\Videos\\holiday.mp4"
CHROME_CACHE = "C:\\Users\\Alice\\AppData\\Local\\Google\\Chrome\\User Data\\Default\\Cache"
WINDOWS = "C:\\Windows"


def show_listing(window: MainWindow, listing: opportunities.OpportunityList, qtbot: object) -> None:
    """Put an analysis on screen (as the import flow does)."""
    window.set_listing(listing)
    assert window.navigate("opportunities")
    qtbot.wait(260)  # type: ignore[attr-defined]


def test_the_list_is_ranked_by_gain_and_carries_rule_solutions(
    window: MainWindow, fixture_listing: opportunities.OpportunityList, qtbot: object
) -> None:
    """The screen's promise: biggest estimated gain first, with the rules' advice."""
    show_listing(window, fixture_listing, qtbot)
    view = window.opportunities_view
    model = view.table_model()

    assert model.rowCount() == len(fixture_listing)
    visible = model.visible_rows
    gains = [row.gain for row in visible]
    assert gains == sorted(gains, reverse=True)

    # The solution column shows the classifier's verdict, not a paraphrase.
    index = model.index_of(CHROME_CACHE)
    assert index.isValid()
    solution = index.sibling(index.row(), models.COLUMN_SOLUTION)
    assert solution.data(Qt.ItemDataRole.DisplayRole) == "Delete (quarantine)"
    assert str(solution.data(Qt.ItemDataRole.DisplayRole)) in str(
        solution.data(Qt.ItemDataRole.ToolTipRole)
    )
    assert index.sibling(index.row(), models.COLUMN_PATH).data(Qt.ItemDataRole.DisplayRole) == (
        CHROME_CACHE
    )
    assert index.sibling(index.row(), models.COLUMN_TIER).data(Qt.ItemDataRole.DisplayRole) in {
        "T1",
        "T2",
        "T3",
    }
    assert (
        index.sibling(index.row(), models.COLUMN_GAIN)
        .data(Qt.ItemDataRole.DisplayRole)
        .endswith(("B", "iB", "B)"))
    )


def test_no_action_rows_are_listed_with_their_reason(
    window: MainWindow, fixture_listing: opportunities.OpportunityList, qtbot: object
) -> None:
    """An explicit "No action" row is a decision: it is never hidden by default."""
    show_listing(window, fixture_listing, qtbot)
    model = window.opportunities_view.table_model()

    no_action = [row for row in model.visible_rows if row.state == opportunities.STATE_NO_ACTION]
    assert no_action, "the fixture lists entries the rules deliberately leave alone"
    index = model.index_of(WINDOWS) if model.index_of(WINDOWS).isValid() else None
    listed_no_action = index is not None
    assert listed_no_action
    solution = index.sibling(index.row(), models.COLUMN_SOLUTION)
    tooltip = str(solution.data(Qt.ItemDataRole.ToolTipRole))
    assert "No action" in tooltip and "Windows" in tooltip
    row = model.row_at(index)
    assert row is not None and row.state == opportunities.STATE_NO_ACTION
    assert row.gain == 0 and row.gain_label.strip()  # nothing is freed: the row says so


def test_checking_a_folder_covers_its_contents(
    window: MainWindow, fixture_listing: opportunities.OpportunityList, qtbot: object
) -> None:
    """The cascade: a folder counts once, its children render as covered."""
    show_listing(window, fixture_listing, qtbot)
    model = window.opportunities_view.table_model()
    folder = model.index_of(VIDEOS)
    child = model.index_of(VIDEOS_CLIP)
    assert folder.isValid() and child.isValid()

    model.toggle(VIDEOS)
    assert folder.data(Qt.ItemDataRole.CheckStateRole) == Qt.CheckState.Checked
    assert child.data(Qt.ItemDataRole.CheckStateRole) == Qt.CheckState.Unchecked
    assert child.data(Qt.ItemDataRole.UserRole + 1) == "covered"

    selection = model.selection
    child_row = model.row_at(child)
    folder_row = model.row_at(folder)
    assert child_row is not None and folder_row is not None
    assert selection.is_covered(child_row.key)
    assert selection.gain == folder_row.gain  # not folder + child
    assert "estimated gain" in window.opportunities_view.selection_label.text()

    # Clicking the covered child moves the selection down to it.
    model.toggle(VIDEOS_CLIP)
    assert not selection.is_selected(folder_row.key)
    assert selection.is_selected(child_row.key)
    assert selection.gain == child_row.gain
    assert folder.data(Qt.ItemDataRole.CheckStateRole) == Qt.CheckState.PartiallyChecked

    model.clear_selection()
    assert len(selection) == 0
    assert folder.data(Qt.ItemDataRole.CheckStateRole) == Qt.CheckState.Unchecked


def test_select_visible_never_double_counts(
    window: MainWindow, fixture_listing: opportunities.OpportunityList, qtbot: object
) -> None:
    """Bulk select walks the tree top-down and keeps one row per branch."""
    show_listing(window, fixture_listing, qtbot)
    model = window.opportunities_view.table_model()
    model.select_all_visible()
    selection = model.selection
    assert len(selection) > 0
    keys = set(selection.keys)
    for row in selection.rows:
        assert not any(ancestor in keys for ancestor in opportunities.ancestor_keys(row.key))
    assert selection.gain == sum(row.gain for row in selection.rows)
    model.clear_selection()


def test_filters_narrow_the_list_and_keep_the_summary_honest(
    window: MainWindow, fixture_listing: opportunities.OpportunityList, qtbot: object
) -> None:
    """Search, state, tier and size filters, and the strip they drive."""
    show_listing(window, fixture_listing, qtbot)
    view = window.opportunities_view
    model = view.table_model()
    total = model.rowCount()

    view.search.setText("holiday.mp4")
    qtbot.wait(60)  # type: ignore[attr-defined]
    assert model.rowCount() == 1
    assert model.visible_rows[0].path == VIDEOS_CLIP

    view.search.clear()
    qtbot.wait(60)  # type: ignore[attr-defined]
    assert model.rowCount() == total

    state_index = view.state_combo.findData(opportunities.STATE_NO_ACTION)
    view.state_combo.setCurrentIndex(state_index)
    qtbot.wait(60)  # type: ignore[attr-defined]
    assert model.rowCount() > 0
    assert all(row.state == opportunities.STATE_NO_ACTION for row in model.visible_rows)
    assert view.cards["rows"].value().startswith(f"{model.rowCount():,}")

    view.state_combo.setCurrentIndex(0)
    tier_index = view.tier_combo.findData("T1")
    view.tier_combo.setCurrentIndex(tier_index)
    qtbot.wait(60)  # type: ignore[attr-defined]
    assert model.rowCount() > 0
    assert all(row.tier == "T1" for row in model.visible_rows)

    view.tier_combo.setCurrentIndex(0)
    view.size_combo.setCurrentIndex(2)  # 50 MiB
    qtbot.wait(60)  # type: ignore[attr-defined]
    assert model.rowCount() > 0
    assert all(row.size >= 50 * 1024**2 for row in model.visible_rows)


def test_summary_strip_counters_match_the_rows(
    window: MainWindow, fixture_listing: opportunities.OpportunityList, qtbot: object
) -> None:
    """The strip's numbers are the engine's arithmetic, deduplicated."""
    show_listing(window, fixture_listing, qtbot)
    view = window.opportunities_view
    summary = opportunities.summarise(view.table_model().visible_rows)
    assert view.cards["rows"].value().startswith(f"{summary.rows:,}")
    assert view.cards["gain"].value() == summary.gain_label
    assert view.cards["action"].value() == f"{summary.actionable:,}"
    assert view.cards["undecided"].value() == f"{summary.undecided:,}"
    # Volume keys stay normalised for the arithmetic; the strip shows ``D:``, not ``d:``.
    detail = view.cards["volumes"].caption()
    assert "d:" not in detail and "c:" not in detail
    assert "D:" in detail


def test_drive_label_keeps_non_drive_volumes_as_they_are() -> None:
    """A POSIX mount or a UNC share is shown exactly as the engine keyed it."""
    from spacesage.app.views.opportunities_view import drive_label

    assert drive_label("d:") == "D:"
    assert drive_label("/home") == "/home"
    assert drive_label("\\\\server\\share") == "\\\\server\\share"


def test_details_pane_explains_the_selected_row(
    window: MainWindow, fixture_listing: opportunities.OpportunityList, qtbot: object
) -> None:
    """Reasoning, side effects, alternatives and the destination editor."""
    show_listing(window, fixture_listing, qtbot)
    view = window.opportunities_view
    view.set_target_drive("D:")

    assert view.select_row(VIDEOS)
    qtbot.wait(60)  # type: ignore[attr-defined]
    details = view.details
    row = details.current_row()
    assert row is not None and row.path == VIDEOS
    assert details.destination_text() == "D:\\Moved\\Users\\Alice\\Videos"
    assert "junction" in details.destination_hint()

    assert view.select_row(CHROME_CACHE)
    qtbot.wait(60)  # type: ignore[attr-defined]
    changed = details.current_row()
    assert changed is not None and changed.path == CHROME_CACHE
    # The destination editor is for moves: a quarantine row says so instead.
    assert details._destination.isEnabled() is False


def test_editing_a_destination_reaches_the_controller(
    window: MainWindow, fixture_listing: opportunities.OpportunityList, qtbot: object
) -> None:
    """The destination editor feeds the plan (S9) through one signal."""
    show_listing(window, fixture_listing, qtbot)
    view = window.opportunities_view
    view.set_target_drive("D:")
    assert view.select_row(VIDEOS)
    qtbot.wait(60)  # type: ignore[attr-defined]

    seen: list[tuple[str, str]] = []
    view.destinationEdited.connect(lambda key, text: seen.append((key, text)))
    view.details.set_destination_text("E:\\Moved\\Videos")
    view.details._on_destination_edited("E:\\Moved\\Videos")
    assert seen and seen[-1][1] == "E:\\Moved\\Videos"


def test_double_click_toggles_a_row(
    window: MainWindow, fixture_listing: opportunities.OpportunityList, qtbot: object
) -> None:
    """A checkbox is not the only way in: the whole row can be clicked."""
    show_listing(window, fixture_listing, qtbot)
    model = window.opportunities_view.table_model()
    index = model.index_of(CHROME_CACHE)
    row = model.row_at(index)
    assert row is not None
    window.opportunities_view._on_double_clicked(index)
    assert model.selection.is_selected(row.key)
    window.opportunities_view._on_double_clicked(index)
    assert not model.selection.is_selected(row.key)


def test_sorting_by_another_column(
    window: MainWindow, fixture_listing: opportunities.OpportunityList, qtbot: object
) -> None:
    """Header clicks order by size or path, deterministically."""
    show_listing(window, fixture_listing, qtbot)
    model = window.opportunities_view.table_model()
    model.set_sort("size", True)
    sizes = [row.size for row in model.visible_rows]
    assert sizes == sorted(sizes, reverse=True)
    model.set_sort("path", False)
    keys = [row.key for row in model.visible_rows]
    assert keys == sorted(keys)
    model.set_sort("gain", True)
    assert model.sort_key == "gain"


def test_space_checks_the_row_under_the_cursor(
    window: MainWindow, fixture_listing: opportunities.OpportunityList, qtbot: object
) -> None:
    """Keyboard: Space checks or unchecks the row the cursor is on (design §9.1)."""
    show_listing(window, fixture_listing, qtbot)
    view = window.opportunities_view
    model = view.table_model()
    table = view.table
    index = model.index_of(VIDEOS)
    assert index.isValid()

    table.setCurrentIndex(index)
    qtbot.keyClick(table, Qt.Key.Key_Space)  # type: ignore[attr-defined]
    assert model.selection.is_selected(opportunities.path_key(VIDEOS))
    qtbot.keyClick(table, Qt.Key.Key_Space)  # type: ignore[attr-defined]
    assert len(model.selection) == 0


def test_theme_switch_keeps_the_screen_intact(
    window: MainWindow,
    fixture_listing: opportunities.OpportunityList,
    qtbot: object,
    qapp: QApplication,
) -> None:
    """A theme change re-renders the hand-painted cells without losing state."""
    show_listing(window, fixture_listing, qtbot)
    view = window.opportunities_view
    model = view.table_model()
    model.toggle(VIDEOS)
    before = model.rowCount()
    view.apply_theme()
    from spacesage.app import theme

    theme.ThemeManager(qapp, mode="dark").set_mode("dark")
    window.apply_theme()
    qtbot.wait(60)  # type: ignore[attr-defined]
    assert model.rowCount() == before
    assert model.selection.is_selected(opportunities.path_key(VIDEOS))
    theme.ThemeManager(qapp, mode="light").set_mode("light")
