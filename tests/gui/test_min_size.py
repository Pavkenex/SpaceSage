"""The shell's smallest window: every action bar fits, and nothing is cut.

The shell allows a 980x620 window (``MainWindow.setMinimumSize``) and several
rows need more width than that; at the reference size (1440x900, where the
acceptance renders are taken) everything fits, so the defects only show at the
small size.

Two rules are pinned here, and the first one no longer stands alone:

* **A visible single-line label either fits its text or elides it visibly.**  A
  plain ``QLabel``/``QPushButton`` paints past its own edge -- the user reads a
  truncated figure with no sign that anything is missing (design §9.1, "Every
  long line is elided *or* wrapped on purpose -- never clipped"); the app's own
  ``ElidedLabel``/``ElidedButton`` cut the text with an ellipsis and keep the
  whole of it one hover away.
* **The six action bars reflow instead of eliding at all.**  They need 750-1190px
  and the shell gives them 736px at its minimum, so each of them is a
  ``widgets.FlowLayout`` (t_af23bb34): a row that runs out of width puts its last
  items on a second line, and the figures and captions stay whole.  Elision is
  what is left for text no row can hold -- a path, a hint -- not for the bars.

So at 980x620 the six rows are whole *and* taller, and at 1440x900 they are whole
on one line; this file walks both sizes and fails if a row elides anything it
could have wrapped.
"""

from __future__ import annotations

from collections.abc import Iterator, Sequence
from typing import TYPE_CHECKING, Any

from PySide6.QtCore import QSize, Qt
from PySide6.QtWidgets import QLabel, QPushButton, QWidget

from spacesage.app import widgets
from spacesage.app.windows import MainWindow

if TYPE_CHECKING:  # the fixtures live in conftest; only the type is needed here
    from conftest import LiveSandbox

#: The shell's own minimum (``spacesage/app/windows.py``), and the reference size.
MIN_WINDOW = (980, 620)
REFERENCE_WINDOW = (1440, 900)


def settle(qtbot: object, ms: int = 260) -> None:
    """Let layout, paint and the entrance fade happen."""
    qtbot.wait(ms)  # type: ignore[attr-defined]


def app_with_a_plan(live_sandbox: LiveSandbox, sandbox_window: Any, qtbot: object) -> MainWindow:
    """The whole app over the live sandbox, holding a plan and an undo history."""
    window: MainWindow = sandbox_window(live_sandbox)
    assert window.navigate("opportunities")
    model = window.opportunities_view.table_model()
    for path in live_sandbox.selection():
        model.toggle(path)
    settle(qtbot)
    window.opportunities_view.build_plan_button.click()
    plan_view = window.plan_page.plan
    qtbot.waitUntil(lambda: not plan_view.busy(), timeout=30_000)  # type: ignore[attr-defined]
    settle(qtbot)
    return window


def readable_labels(root: QWidget) -> Iterator[QLabel]:
    """Every label a user can read right now: visible, single line, plain text.

    The root itself counts when it is a label: a row is often *made of* its
    figures, and the pin has to measure those too.
    """
    candidates = (
        [root, *root.findChildren(QLabel)]
        if isinstance(root, QLabel)
        else list(root.findChildren(QLabel))
    )
    for label in candidates:
        if not label.isVisible() or not label.text():
            continue
        if label.wordWrap() or label.textFormat() == Qt.TextFormat.RichText:
            continue  # wrapped text cannot run past the edge; markup is not its own width
        yield label


def assert_label_is_readable(label: QLabel, where: str) -> None:
    """A label fits its text, or elides it visibly and hands the full text over."""
    metrics = label.fontMetrics()
    painted = metrics.horizontalAdvance(label.text())
    room = label.contentsRect().width()
    if isinstance(label, widgets.ElidedLabel):
        assert painted <= room, (
            f"{where}: the eliding label {label.objectName() or label.text()!r} paints past "
            f"its own edge: {room}px to paint in, {painted}px needed"
        )
        if label.text() != label.full_text():
            assert label.full_text() in label.toolTip(), (
                f"{where}: {label.full_text()!r} is elided to {label.text()!r} with no tooltip "
                "carrying the full text"
            )
        return
    assert painted <= room, (
        f"{where}: {label.text()!r} is clipped: {label.width()}px painted, {painted}px needed "
        "(a plain QLabel paints past its edge; design §9.1)"
    )


def readable_buttons(root: QWidget) -> Iterator[QPushButton]:
    """Every button caption a user can read right now: visible, with text."""
    candidates = (
        [root, *root.findChildren(QPushButton)]
        if isinstance(root, QPushButton)
        else list(root.findChildren(QPushButton))
    )
    for button in candidates:
        if button.isVisible() and button.text():
            yield button


def assert_caption_is_readable(button: QPushButton, where: str) -> None:
    """A caption fits the room its style leaves, or elides visibly onto a tooltip."""
    metrics = button.fontMetrics()
    painted = metrics.horizontalAdvance(button.text())
    room = widgets.caption_room(button)
    if isinstance(button, widgets.ElidedButton):
        assert painted <= room, (
            f"{where}: the eliding button {button.full_text()!r} paints past the room its "
            f"style leaves: {room}px to paint in, {painted}px needed"
        )
        if button.text() != button.full_text():
            assert button.full_text() in button.toolTip(), (
                f"{where}: the caption {button.full_text()!r} is elided to {button.text()!r} "
                "with no tooltip carrying the whole caption"
            )
        return
    assert painted <= room, (
        f"{where}: the caption {button.text()!r} is cut: {room}px to paint in, "
        f"{painted}px needed (a plain QPushButton clips at both ends; design §9.1)"
    )


def screens(window: MainWindow, qtbot: object) -> Iterator[tuple[str, QWidget]]:
    """Every screen of the shell, as a user walks them, each one laid out."""
    for name in ("import", "opportunities", "plan", "settings"):
        assert window.navigate(name)
        if name == "plan":
            # The Plan page remembers which half was on screen; a plain navigate
            # would leave the undo half in front and the plan half unlaid out.
            window.plan_page.show_plan()
        settle(qtbot, 200)
        yield name, window
    window.navigate("plan")
    window.plan_page.show_undo()
    settle(qtbot, 200)
    yield "undo", window


# --------------------------------------------------------------------------- #
# The reflow: the six bars, their parts, and the rule they hold
# --------------------------------------------------------------------------- #


def action_rows(window: MainWindow) -> dict[str, list[QWidget]]:
    """The six rows t_af23bb34 reflows, each as the widgets that share its lines.

    Named as the user meets them, because a failure message has to say *which*
    bar gave something up.  The summary strips carry their metric cards: a card
    holds its own figure and title, and those count as part of the row.
    """
    view = window.opportunities_view
    plan = window.plan_page.plan
    undo = window.plan_page.undo
    return {
        "Opportunities filter bar": [
            view.search,
            view.state_combo,
            view.tier_combo,
            view.category_combo,
            view.size_combo,
            view.select_button,
            view.clear_button,
        ],
        "Opportunities summary strip": list(view.cards.values()),
        "Opportunities list footer": [
            view.selection_label,
            view.cascade_hint,
            view.build_plan_button,
        ],
        "Plan toolbar": [
            plan.approval_label,
            plan.approve_all_button,
            plan.reject_all_button,
            plan.preview_button,
            plan.undo_button,
            plan.execute_button,
        ],
        "Plan summary strip": list(plan.cards.values()),
        "Undo footer": [
            undo.selection_label,
            undo.hint_label,
            undo.open_button,
            undo.refresh_button,
            undo.revert_selected_button,
            undo.revert_all_button,
        ],
    }


def row_lines(parts: Sequence[QWidget]) -> int:
    """How many lines the row is using (the y offsets its widgets landed on)."""
    return len({part.geometry().y() for part in parts})


def assert_row_is_whole(where: str, parts: Sequence[QWidget]) -> None:
    """Nothing in the row is elided: the row wrapped instead of giving text up.

    Every widget in the row, and every figure or caption inside one of its cards,
    must paint all of the text it was given -- an ellipsis here means the row had
    room to wrap and did not take it.
    """
    for part in parts:
        for widget in [part, *part.findChildren(QWidget)]:
            if not isinstance(widget, (widgets.ElidedLabel, widgets.ElidedButton)):
                continue
            if not widget.isVisible() or not widget.full_text():
                continue
            assert not widget.is_elided(), (
                f"{where}: {widget.full_text()!r} is elided to {widget.text()!r} -- at the "
                "shell's minimum the row wraps onto another line instead (t_af23bb34)"
            )
        for label in readable_labels(part):
            assert_label_is_readable(label, where)
        for button in readable_buttons(part):
            assert_caption_is_readable(button, where)


def walk_to_the_rows(window: MainWindow, qtbot: object) -> None:
    """Lay out both halves of the plan page, so the undo footer is really laid out."""
    assert window.navigate("opportunities")
    settle(qtbot, 200)
    assert window.navigate("plan")
    window.plan_page.show_plan()
    settle(qtbot, 200)
    window.plan_page.show_undo()
    settle(qtbot, 260)


# --------------------------------------------------------------------------- #
# The pins
# --------------------------------------------------------------------------- #


def test_no_label_is_clipped_at_the_shell_minimum(
    live_sandbox: LiveSandbox, sandbox_window: Any, qtbot: object
) -> None:
    """The whole app, at its smallest window and at the reference size."""
    window = app_with_a_plan(live_sandbox, sandbox_window, qtbot)
    for width, height in (MIN_WINDOW, REFERENCE_WINDOW):
        window.resize(width, height)
        settle(qtbot, 400)
        for name, root in screens(window, qtbot):
            for label in readable_labels(root):
                assert_label_is_readable(label, f"{name} at {width}x{height}")
            for button in readable_buttons(root):
                assert_caption_is_readable(button, f"{name} at {width}x{height}")


def test_the_action_bars_wrap_at_the_shell_minimum_and_pack_at_the_reference(
    live_sandbox: LiveSandbox, sandbox_window: Any, qtbot: object
) -> None:
    """The pin under the whole change: six bars, whole at the minimum, one line at 1440.

    At 980x620 every row needs 750-1190px and has 736px, so every one of them
    takes a second line; at 1440x900 they all fit on one.  Before t_af23bb34 the
    same rows elided their figures, their captions and their card titles instead.
    """
    window = app_with_a_plan(live_sandbox, sandbox_window, qtbot)
    window.resize(*MIN_WINDOW)
    walk_to_the_rows(window, qtbot)
    for name, parts in action_rows(window).items():
        assert_row_is_whole(f"{name} at 980x620", parts)
        assert row_lines(parts) == 2, (
            f"{name} at 980x620: the row is using {row_lines(parts)} line(s) -- this pin "
            "describes rows that have to reflow to stay whole"
        )

    window.resize(*REFERENCE_WINDOW)
    walk_to_the_rows(window, qtbot)
    for name, parts in action_rows(window).items():
        assert_row_is_whole(f"{name} at 1440x900", parts)
        assert row_lines(parts) == 1, (
            f"{name} at 1440x900: the row wrapped onto {row_lines(parts)} lines although "
            "the reference size has the width for it"
        )


def test_the_squeezed_bars_wrap_their_buttons_at_the_shell_minimum(
    live_sandbox: LiveSandbox, sandbox_window: Any, qtbot: object
) -> None:
    """The two bars that ran out of room: the whole captions move down, not under an ellipsis."""
    window = app_with_a_plan(live_sandbox, sandbox_window, qtbot)
    plan = window.plan_page.plan
    undo = window.plan_page.undo
    squeezed = (  # the captions that used to be elided at the shell's minimum
        (plan.preview_button, "Dry-run preview"),
        (undo.open_button, "Open journal file…"),
        (undo.revert_selected_button, "Revert selected"),
        (undo.revert_all_button, "Revert all pending"),
    )

    for size, wrapped in ((MIN_WINDOW, True), (REFERENCE_WINDOW, False)):
        window.resize(*size)
        walk_to_the_rows(window, qtbot)
        for button, caption in squeezed:
            where = f"{caption!r} at {size[0]}x{size[1]}"
            assert button.full_text() == caption, f"{where}: the button was rebuilt"
            assert button.text() == caption, (
                f"{where}: the caption is elided to {button.text()!r}; the row has room to "
                "wrap it onto another line instead"
            )
            assert not button.toolTip().startswith(caption), (
                f"{where}: whole, but its tooltip still leads with the caption"
            )
        if wrapped:
            assert plan.preview_button.geometry().y() > plan.approval_label.geometry().y(), (
                "the toolbar kept every button on the figure's line although the shell's "
                "minimum is too narrow for it"
            )
            assert undo.revert_all_button.geometry().y() > undo.selection_label.geometry().y(), (
                "the undo footer kept every button on the figure's line although the shell's "
                "minimum is too narrow for it"
            )
        else:
            assert plan.preview_button.geometry().y() == plan.approval_label.geometry().y()
            assert undo.revert_all_button.geometry().y() == undo.selection_label.geometry().y()


def test_the_plan_figure_is_whole_at_the_shell_minimum(
    live_sandbox: LiveSandbox, sandbox_window: Any, qtbot: object
) -> None:
    """The Plan toolbar's figure: the row pays for it at both sizes, so it never elides."""
    window = app_with_a_plan(live_sandbox, sandbox_window, qtbot)
    assert window.navigate("plan")
    figure = window.plan_page.plan.approval_label
    full = figure.full_text()
    assert full, "the toolbar has no figure to measure"

    for size, lines in ((MIN_WINDOW, 2), (REFERENCE_WINDOW, 1)):
        window.resize(*size)
        settle(qtbot, 400)
        where = f"the plan figure at {size[0]}x{size[1]}"
        assert figure.text() == full, (
            f"{where}: elided to {figure.text()!r} -- the toolbar has to wrap its buttons "
            "instead of giving the figure up"
        )
        assert not figure.is_elided()
        assert figure.width() >= figure.fontMetrics().horizontalAdvance(full), (
            f"{where}: the label is narrower than its text although the row wrapped"
        )
        assert row_lines(action_rows(window)["Plan toolbar"]) == lines, (
            f"{where}: the toolbar is using {row_lines(action_rows(window)['Plan toolbar'])} "
            f"line(s); the figure is whole only because the row wraps onto {lines}"
        )


def test_the_list_footer_wraps_instead_of_eliding(
    live_sandbox: LiveSandbox, sandbox_window: Any, qtbot: object
) -> None:
    """The Opportunities footer: the figure and the hint stay whole; the button moves down."""
    window = app_with_a_plan(live_sandbox, sandbox_window, qtbot)
    assert window.navigate("opportunities")
    view = window.opportunities_view

    window.resize(*MIN_WINDOW)
    settle(qtbot, 400)
    for name, widget in (
        ("the checked-rows figure", view.selection_label),
        ("the cascade hint", view.cascade_hint),
    ):
        assert widget.text() == widget.full_text(), (
            f"{name} at 980x620: elided to {widget.text()!r}; the row wraps 'Build plan' "
            "onto a second line instead"
        )
    assert view.build_plan_button.geometry().y() > view.selection_label.geometry().y(), (
        "the footer kept 'Build plan' on the figure's line although the shell's minimum "
        "is too narrow for it"
    )

    window.resize(*REFERENCE_WINDOW)
    settle(qtbot, 400)
    assert view.selection_label.text() == view.selection_label.full_text()
    assert view.cascade_hint.text() == view.cascade_hint.full_text()
    assert view.build_plan_button.geometry().y() == view.selection_label.geometry().y(), (
        "the footer wrapped at the reference size although it has the width for one line"
    )


def test_the_filter_bar_wraps_and_keeps_its_search_usable(
    live_sandbox: LiveSandbox, sandbox_window: Any, qtbot: object
) -> None:
    """The filter bar: the search keeps its room; the two buttons take the second line."""
    window = app_with_a_plan(live_sandbox, sandbox_window, qtbot)
    assert window.navigate("opportunities")
    search = window.opportunities_view.search
    select = window.opportunities_view.select_button

    window.resize(*MIN_WINDOW)
    settle(qtbot, 400)
    assert search.width() >= 200, (
        f"the search field is {search.width()}px wide at the shell's minimum -- a field that "
        "narrow cannot search (it was 46px before the bar reflowed, 158px with a plain wrap)"
    )
    assert select.geometry().y() > search.geometry().y(), (
        "the filter bar kept its buttons on the search's line although the shell's minimum "
        "is too narrow for it"
    )

    window.resize(*REFERENCE_WINDOW)
    settle(qtbot, 400)
    assert search.width() >= 400, (
        f"the search field is {search.width()}px wide at the reference size: the row's "
        "leftover width has to go to the field, not to a gap on the right"
    )
    assert select.geometry().y() == search.geometry().y()


def test_the_shell_minimum_did_not_move(
    live_sandbox: LiveSandbox, sandbox_window: Any, qtbot: object
) -> None:
    """The decision itself: the bars reflow, the window still goes down to 980x620."""
    window = app_with_a_plan(live_sandbox, sandbox_window, qtbot)
    assert window.minimumSize() == QSize(*MIN_WINDOW), (
        "the shell's minimum changed; t_af23bb34 decided the rows reflow instead"
    )
    window.resize(*MIN_WINDOW)
    settle(qtbot, 300)
    assert window.size() == QSize(*MIN_WINDOW)
