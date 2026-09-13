"""The shell's smallest window: no bar clips the figure it is showing.

The shell allows a 980x620 window (``MainWindow.setMinimumSize``) and several
rows need more than that; at the reference size (1440x900, where the acceptance
renders are taken) everything fits, so the defect only shows at the small size:
a row that cannot pay shrinks its label, and a plain ``QLabel`` then paints past
its own edge -- the user reads a truncated figure with no sign that anything is
missing (design §9.1, "Every long line is elided *or* wrapped on purpose --
never clipped").  A ``QPushButton`` behaves the same way, only worse: it clips
its caption at *both* ends, so the destructive ``Revert all pending`` reads
``vert all pendin``.

The rule this pins, at both sizes: a visible single-line label either fits its
text, or it is one of the app's own eliding labels, which cuts the text with a
sign (the ellipsis) and keeps the full text one hover away.  A visible button
caption follows the same rule -- whole, or elided with the full caption in its
tooltip.
"""

from __future__ import annotations

from collections.abc import Iterator
from typing import TYPE_CHECKING, Any

from PySide6.QtCore import Qt
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
    """Every label a user can read right now: visible, single line, plain text."""
    for label in root.findChildren(QLabel):
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
    for button in root.findChildren(QPushButton):
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


def test_the_squeezed_bars_elide_their_buttons_at_the_shell_minimum(
    live_sandbox: LiveSandbox, sandbox_window: Any, qtbot: object
) -> None:
    """The two bars that run out of room: the captions give way with a sign, not cut."""
    window = app_with_a_plan(live_sandbox, sandbox_window, qtbot)
    plan = window.plan_page.plan
    undo = window.plan_page.undo
    squeezed = (  # the captions the four squeezed buttons must carry
        (plan.preview_button, "Dry-run preview"),
        (undo.open_button, "Open journal file…"),
        (undo.revert_selected_button, "Revert selected"),
        (undo.revert_all_button, "Revert all pending"),
    )

    for size, give_way in ((MIN_WINDOW, True), (REFERENCE_WINDOW, False)):
        window.resize(*size)
        assert window.navigate("plan")
        window.plan_page.show_plan()  # the undo half was left in front by the last pass
        settle(qtbot, 400)
        window.plan_page.show_undo()
        settle(qtbot, 400)
        for button, caption in squeezed:
            where = f"{caption!r} at {size[0]}x{size[1]}"
            if give_way:
                assert button.text().endswith("…"), (
                    f"{where}: the caption is cut with no ellipsis, painted as {button.text()!r}"
                )
                assert caption in button.toolTip(), (
                    f"{where}: elided and not reachable in a tooltip"
                )
                assert button.text() != caption, (
                    f"{where} fits the shell's smallest window: this pin no longer describes "
                    "the row"
                )
            else:
                assert button.text() == caption, f"{where}: still elided"
                assert not button.toolTip().startswith(caption), (
                    f"{where}: whole, but its tooltip still leads with the caption"
                )


def test_the_plan_figure_elides_visibly_at_the_shell_minimum(
    live_sandbox: LiveSandbox, sandbox_window: Any, qtbot: object
) -> None:
    """The Plan toolbar's figure: painted in full when the row can pay, elided when it cannot."""
    window = app_with_a_plan(live_sandbox, sandbox_window, qtbot)
    assert window.navigate("plan")
    figure = window.plan_page.plan.approval_label
    full = figure.full_text()

    # The toolbar asks for more than the smallest window has (label + five buttons),
    # so the figure is the first thing to give: it may only do that with a sign.
    window.resize(*MIN_WINDOW)
    settle(qtbot, 400)
    assert figure.width() < figure.fontMetrics().horizontalAdvance(full), (
        "the toolbar fits at the shell minimum: this pin no longer describes the row"
    )
    assert figure.text() != full, "the figure is cut with no ellipsis"
    assert figure.text().endswith("…"), f"the painted text is not visibly elided: {figure.text()!r}"
    assert full in figure.toolTip(), "the full figure is not reachable"

    # At the reference size the same figure is complete.
    window.resize(*REFERENCE_WINDOW)
    settle(qtbot, 400)
    assert figure.text() == full


def test_the_list_footer_keeps_its_figure_and_elides_its_hint(
    live_sandbox: LiveSandbox, sandbox_window: Any, qtbot: object
) -> None:
    """The Opportunities footer: the figure the user confirms stays whole, the hint yields."""
    window = app_with_a_plan(live_sandbox, sandbox_window, qtbot)
    assert window.navigate("opportunities")
    window.resize(*MIN_WINDOW)
    settle(qtbot, 400)
    view = window.opportunities_view
    figure = view.selection_label
    assert figure.text() == figure.full_text(), (
        f"the checked-rows figure gave up its width instead of the hint: {figure.text()!r}"
    )
    hint = view.cascade_hint
    assert hint.text() != hint.full_text(), "the hint kept a width the row does not have"
    assert hint.full_text() in hint.toolTip(), "the hint elides with no way to read it in full"
