"""GUI tests: pytest-qt on Qt's offscreen platform (design §13).

The offscreen platform renders for real (``widget.grab()``), so the suite can
assert what a user would see -- and the screenshots in ``artifacts/gui`` are the
same renders the acceptance evidence is taken from.  Nothing here needs a
display; ``QT_QPA_PLATFORM`` is set before Qt picks a platform.
"""

from __future__ import annotations

import os
from collections.abc import Callable, Iterator
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest
from PySide6.QtGui import QPixmap

from spacesage import db, ingest, opportunities, rules
from spacesage.app import state, theme
from spacesage.app.main import create_window
from spacesage.app.windows import MainWindow

TESTS_DIR = Path(__file__).resolve().parent
DATA_DIR = TESTS_DIR.parent / "fixtures" / "data"
REPO_ROOT = TESTS_DIR.parents[1]

#: Reference point of the fixture: 2026-09-12 12:00:00 UTC (ages stay stable).
NOW = 1789213200
MIN_SIZE = 10 * 1024**2

#: Where the screenshot tests write; the acceptance evidence is copied from here.
ARTIFACT_DIR = Path(os.environ.get("SPACESAGE_ARTIFACTS", REPO_ROOT / "artifacts" / "gui"))


@pytest.fixture(scope="session")
def fixture_csv() -> Path:
    """The committed candidate fixture (see tests/fixtures/gen_candidates.py)."""
    return DATA_DIR / "candidates.csv"


@pytest.fixture(scope="session")
def fixture_index(tmp_path_factory: pytest.TempPathFactory, fixture_csv: Path) -> Path:
    """An index built from the fixture export (once per session)."""
    target = tmp_path_factory.mktemp("gui-index") / "spacesage.db"
    ingest.ingest_csv(fixture_csv, target, replace=True)
    return target


@pytest.fixture(scope="session")
def fixture_listing(fixture_index: Path) -> opportunities.OpportunityList:
    """The ranked list of the fixture export (once per session)."""
    conn = db.open_db(fixture_index)
    try:
        return opportunities.build_opportunities(
            conn,
            rules.load_rules(include_user=False),
            min_size=MIN_SIZE,
            list_top=500,
            explicit=250,
            now=NOW,
            db_path=str(fixture_index),
        )
    finally:
        conn.close()


@pytest.fixture
def settings() -> state.Settings:
    """Ephemeral settings: tests never touch the user's config file."""
    return state.Settings.ephemeral()


@pytest.fixture
def window(
    qtbot: object, qapp: object, settings: state.Settings, fixture_index: Path, tmp_path: Path
) -> Iterator[MainWindow]:
    """A shown main window over the fixture index."""
    manager = theme.ThemeManager(qapp, mode=theme.MODE_LIGHT)
    built = create_window(qapp, settings, db_path=fixture_index, theme_manager=manager)
    built.resize(1440, 900)
    built.show()
    _settle(qtbot)
    try:
        yield built
    finally:
        built.close()
        built.deleteLater()


def _settle(qtbot: object, ms: int = 260) -> None:
    """Let the interface settle: layout, one paint and the entrance fade."""
    qtbot.wait(ms)  # type: ignore[attr-defined]


@pytest.fixture
def settle() -> Callable[[object, int], None]:
    """Wait until layout, paint and the entrance fade have all happened."""
    return _settle


@pytest.fixture
def artifacts() -> Path:
    """Where screenshot tests write their PNGs."""
    return ARTIFACT_DIR


@pytest.fixture
def grab_png() -> Callable[..., Path]:
    """Render a widget to a PNG, proving the render is not blank."""
    return grab


def grab(widget: object, path: Path, *, minimum_bytes: int = 12_000) -> Path:
    """Render a widget to a PNG and prove the render is not an empty frame."""
    pixmap = widget.grab()  # type: ignore[attr-defined]
    assert isinstance(pixmap, QPixmap) and not pixmap.isNull(), "widget did not render"
    assert _distinct_colours(pixmap) > 25, "the render looks blank (single-colour frame)"
    path.parent.mkdir(parents=True, exist_ok=True)
    assert pixmap.save(str(path)), f"could not write {path}"
    assert path.stat().st_size >= minimum_bytes, f"{path} is suspiciously small"
    return path


def _distinct_colours(pixmap: QPixmap, *, sample: int = 5) -> int:
    """How many distinct colours a render has (a blank frame has very few)."""
    image = pixmap.toImage()
    seen: set[int] = set()
    for y in range(0, image.height(), sample):
        for x in range(0, image.width(), sample):
            seen.add(image.pixel(x, y))
    return len(seen)
