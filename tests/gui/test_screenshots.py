"""Screenshot artifacts: the screens the acceptance evidence is taken from.

Every render is proved to be non-blank (a real paint with many colours, not an
empty frame), so a broken layout cannot pass as a screenshot.
"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from typing import TYPE_CHECKING, Any

import pytest
from PySide6.QtWidgets import QApplication, QDialog

from spacesage import opportunities
from spacesage.app import dialogs
from spacesage.app.windows import MainWindow

if TYPE_CHECKING:  # the fixtures live in conftest; only the type is needed here
    from conftest import LiveSandbox

VIDEOS = "C:\\Users\\Alice\\Videos"
CHROME_CACHE = "C:\\Users\\Alice\\AppData\\Local\\Google\\Chrome\\User Data\\Default\\Cache"


def test_import_screenshot(
    window: MainWindow,
    fixture_csv: Path,
    artifacts: Path,
    grab_png: Callable[..., Path],
    qtbot: object,
) -> None:
    """import.png: the export chosen, the parameters and the analyze action."""
    assert window.navigate("import")
    window.import_view.set_csv(fixture_csv)
    qtbot.wait(260)  # type: ignore[attr-defined]
    assert grab_png(window, artifacts / "import.png").is_file()


def test_opportunities_screenshot(
    window: MainWindow,
    fixture_listing: opportunities.OpportunityList,
    artifacts: Path,
    grab_png: Callable[..., Path],
    qtbot: object,
) -> None:
    """opportunities.png: the ranked list with the summary strip and filters."""
    window.set_listing(fixture_listing)
    assert window.navigate("opportunities")
    window.opportunities_view.set_target_drive("D:")
    qtbot.wait(260)  # type: ignore[attr-defined]
    assert grab_png(window, artifacts / "opportunities.png").is_file()


def test_details_screenshot(
    window: MainWindow,
    fixture_listing: opportunities.OpportunityList,
    artifacts: Path,
    grab_png: Callable[..., Path],
    qtbot: object,
    qapp: QApplication,
) -> None:
    """details.png: the details pane explaining the selected opportunity."""
    window.set_listing(fixture_listing)
    assert window.navigate("opportunities")
    window.opportunities_view.set_target_drive("D:")
    assert window.opportunities_view.select_row(VIDEOS)
    qtbot.wait(260)  # type: ignore[attr-defined]
    assert grab_png(window, artifacts / "details.png").is_file()

    # A second render for the docs: the same screen in the dark theme.
    from spacesage.app import theme

    theme.ThemeManager(qapp, mode="dark").set_mode("dark")
    window.apply_theme()
    assert window.opportunities_view.select_row(CHROME_CACHE)
    qtbot.wait(260)  # type: ignore[attr-defined]
    assert grab_png(window, artifacts / "opportunities-dark.png").is_file()
    theme.ThemeManager(qapp, mode="light").set_mode("light")


def test_plan_flow_screenshots(
    live_sandbox: LiveSandbox,
    sandbox_window: Callable[[LiveSandbox], MainWindow],
    artifacts: Path,
    grab_png: Callable[..., Path],
    qtbot: object,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """plan.png, dryrun.png, execute.png, undo.png: the whole loop as it renders.

    Driven over the live sandbox, so the renders show a plan of files that exist,
    a dry run of real destinations, a run that really ran, and the journal it
    left behind.
    """
    window = sandbox_window(live_sandbox)
    window.navigate("opportunities")
    model = window.opportunities_view.table_model()
    for path in live_sandbox.selection():
        model.toggle(path)
    qtbot.wait(200)  # type: ignore[attr-defined]
    window.opportunities_view.build_plan_button.click()

    plan_view = window.plan_page.plan
    qtbot.waitUntil(lambda: not plan_view.busy(), timeout=30_000)  # type: ignore[attr-defined]
    window.navigate("plan")
    qtbot.wait(260)  # type: ignore[attr-defined]
    assert grab_png(window, artifacts / "plan.png").is_file()

    # The dry run: the resolved operations, rendered by the dialog that shows them.
    assert plan_view.dry_run(show=False)
    qtbot.waitUntil(lambda: not plan_view.busy(), timeout=30_000)  # type: ignore[attr-defined]
    report = plan_view.preview_report()
    assert report is not None
    dialog = dialogs.PreviewDialog(report, parent=window)
    qtbot.addWidget(dialog)  # type: ignore[attr-defined]
    dialog.show()  # opens at the size that shows everything it resolved
    qtbot.wait(300)  # type: ignore[attr-defined]
    # "Exactly what would happen" is no good if the last operation is below the fold.
    assert dialog.list.verticalScrollBar().maximum() == 0
    assert grab_png(dialog, artifacts / "dryrun.png").is_file()
    dialog.close()

    # The confirmation dialog the run goes through, rendered for real: the
    # itemized actions and the danger-styled button are the last thing between
    # the plan and the disk, so it gets the same evidence as the screens.
    dialog = open_confirmation_dialog(plan_view, qtbot)
    qtbot.wait(260)  # type: ignore[attr-defined]
    assert grab_png(dialog, artifacts / "confirm.png").is_file()
    dialog.close()

    # The run: per-item results on the screen that started it.
    install_accepted_confirm(monkeypatch)
    assert plan_view.execute()
    qtbot.waitUntil(lambda: not plan_view.busy(), timeout=30_000)  # type: ignore[attr-defined]
    assert plan_view.apply_report() is not None
    window.navigate("plan")
    qtbot.wait(260)  # type: ignore[attr-defined]
    assert grab_png(window, artifacts / "execute.png").is_file()

    # And the journal it left, ready to be reverted.
    page = window.plan_page
    page.show_undo()
    qtbot.waitUntil(lambda: not page.undo.busy(), timeout=30_000)  # type: ignore[attr-defined]
    qtbot.wait(260)  # type: ignore[attr-defined]
    assert page.undo.current_history() is not None
    assert page.undo.current_history().pending(), "the run must be revertible"
    assert grab_png(window, artifacts / "undo.png").is_file()


def install_accepted_confirm(monkeypatch: pytest.MonkeyPatch) -> None:
    """Accept the next confirmation dialog (a real one would block offscreen)."""

    class Accepted:
        def __init__(self, *_args: Any, **_kwargs: Any) -> None:
            pass

        def exec(self) -> int:
            return int(QDialog.DialogCode.Accepted)

    monkeypatch.setattr(dialogs, "ConfirmDialog", Accepted)


def open_confirmation_dialog(view: Any, qtbot: object) -> dialogs.ConfirmDialog:
    """Open the *real* confirmation dialog the screen builds, without executing.

    A modal ``exec()`` never returns offscreen, so the dialog is constructed
    exactly as the screen builds it in a ``_confirm_run`` that is never
    accepted -- the render is the real one, the run is not.
    """
    seen: list[dialogs.ConfirmDialog] = []

    class Capturing(dialogs.ConfirmDialog):
        def __init__(self, *args: Any, **kwargs: Any) -> None:
            super().__init__(*args, **kwargs)
            seen.append(self)

        def exec(self) -> int:
            return int(QDialog.DialogCode.Rejected)

    original = dialogs.ConfirmDialog
    dialogs.ConfirmDialog = Capturing  # type: ignore[misc]
    try:
        assert view.execute() is False
    finally:
        dialogs.ConfirmDialog = original  # type: ignore[misc]
    assert len(seen) == 1
    dialog = seen[0]
    qtbot.addWidget(dialog)  # type: ignore[attr-defined]
    dialog.show()  # the size the screen itself gives it: no artificial resize
    return dialog
