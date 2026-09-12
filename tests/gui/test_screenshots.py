"""Screenshot artifacts: the screens the acceptance evidence is taken from.

Every render is proved to be non-blank (a real paint with many colours, not an
empty frame), so a broken layout cannot pass as a screenshot.
"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

from PySide6.QtWidgets import QApplication

from spacesage import opportunities
from spacesage.app.windows import MainWindow

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
