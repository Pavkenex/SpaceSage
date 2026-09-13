"""GUI tests: pytest-qt on Qt's offscreen platform (design §13).

The offscreen platform renders for real (``widget.grab()``), so the suite can
assert what a user would see -- and the screenshots in ``artifacts/gui`` are the
same renders the acceptance evidence is taken from.  Nothing here needs a
display; ``QT_QPA_PLATFORM`` is set before Qt picks a platform.
"""

from __future__ import annotations

import os
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest
from PySide6.QtGui import QPixmap

from fixtures import gen_live
from spacesage import ai as ai_layer
from spacesage import db, ingest, opportunities, planner, rules
from spacesage.ai import config as ai_config
from spacesage.app import ai_models, state, theme
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
    """A shown main window over the fixture index, with the AI layer off.

    The window owns the AI layer, so every test that builds one gets an offline
    one: no test can reach a provider (or another test's cache) by accident.
    """
    manager = theme.ThemeManager(qapp, mode=theme.MODE_LIGHT)
    built = create_window(
        qapp,
        settings,
        db_path=fixture_index,
        theme_manager=manager,
        ai_service=offline_ai(tmp_path),
    )
    built.resize(1440, 900)
    built.show()
    _settle(qtbot)
    try:
        yield built
    finally:
        built.close()
        built.deleteLater()


def offline_ai(tmp_path: Path) -> ai_models.AIService:
    """An AI layer with no provider: "AI off", an empty cache, nothing on disk."""
    return ai_models.AIService(config=ai_layer.AIConfig(cache_dir=str(tmp_path / "ai-cache")))


@pytest.fixture
def ai_service(ai_config: ai_layer.AIConfig) -> ai_models.AIService:
    """The app's AI seam over the scripted stub provider (design §10)."""
    return ai_models.AIService(config=ai_config)


@pytest.fixture
def ai_window(
    qtbot: object,
    qapp: object,
    settings: state.Settings,
    fixture_index: Path,
    ai_service: ai_models.AIService,
) -> Iterator[MainWindow]:
    """The main window with a working (scripted) provider behind the AI layer."""
    manager = theme.ThemeManager(qapp, mode=theme.MODE_LIGHT)
    built = create_window(
        qapp, settings, db_path=fixture_index, theme_manager=manager, ai_service=ai_service
    )
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


@pytest.fixture(autouse=True)
def ai_off_by_default(
    monkeypatch: pytest.MonkeyPatch, tmp_path_factory: pytest.TempPathFactory
) -> None:
    """No GUI test may read the developer's AI config, cache or rule packs.

    Views constructed directly (``PlanView`` and friends) build their own
    :class:`spacesage.app.ai_models.AIService` when a test does not hand one in;
    pointing the environment at an empty config keeps every one of them "AI off"
    instead of picking up whatever is on the machine running the suite.
    """
    home = tmp_path_factory.mktemp("ai-off")
    monkeypatch.setenv(ai_config.CONFIG_ENV_VAR, str(home / "ai.toml"))
    monkeypatch.setenv(ai_config.CACHE_DIR_ENV_VAR, str(home / "ai-cache"))
    monkeypatch.setenv(rules.RULES_ENV_VAR, str(home / "rules"))


@pytest.fixture(autouse=True)
def no_blocking_dialogs(
    monkeypatch: pytest.MonkeyPatch,
) -> list[tuple[str, str]]:
    """Capture the app's two message dialogs so no test can block on a modal box.

    ``report_error``/``report_note`` wrap ``QMessageBox.exec()``, which waits
    forever in a headless run: a worker that failed unexpectedly would hang the
    suite instead of failing a test.  Tests that expect one of them assert on the
    ``(title, message)`` pairs collected here (or patch the function themselves,
    which wins for that test).
    """
    from spacesage.app import dialogs

    seen: list[tuple[str, str]] = []

    def capture(_parent: object, title: str, message: str) -> None:
        seen.append((title, message))

    monkeypatch.setattr(dialogs, "report_error", capture)
    monkeypatch.setattr(dialogs, "report_note", capture)
    return seen


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


# --------------------------------------------------------------------------- #
# The live sandbox: the plan/undo loop on files that really exist
# --------------------------------------------------------------------------- #

GIB = 1024**3
MIB = 1024**2

#: Reference point of the live export, as the engine's own float.
SANDBOX_NOW = int(gen_live.NOW.timestamp())


@dataclass
class LiveSandbox:
    """A planted tree, an index of it, a plan data root and a target on another volume."""

    live: gen_live.Live
    listing: opportunities.OpportunityList
    db_path: Path
    data_root: Path
    target: Path
    quarantine: Path

    def target_spec(self) -> planner.PlanTarget:
        """The target drive moves are planned onto (roomy, no reserve)."""
        return planner.PlanTarget(name=str(self.target), free_bytes=10 * GIB, reserve_bytes=0)

    def selection(self) -> list[str]:
        """The rows a user would check for a complete run: two folders and three files.

        ``app/node_modules`` is a folder rule whose actions swallow its children,
        ``scratch/old.dmp`` a T1 file quarantine, ``logs/session.log`` a T1 file
        inside a review folder, ``media`` the move candidate and
        ``archive/setup.msi`` the advice row -- the five shapes the plan screen
        has to render.
        """
        tree = self.live.tree
        return [
            str(tree / "app" / "node_modules"),
            str(tree / "scratch" / "old.dmp"),
            str(tree / "logs" / "session.log"),
            str(tree / "media"),
            str(tree / "archive" / "setup.msi"),
        ]


@pytest.fixture
def live_sandbox(tmp_path: Path) -> Iterator[LiveSandbox]:
    """A live sandbox tree, indexed, with a target drive on another volume."""
    foreign = gen_live.foreign_root()
    if foreign is None:  # pragma: no cover - every CI runner has one
        pytest.skip("no writable temporary area on another volume")
        foreign = tmp_path / "foreign"
    live = gen_live.scenario(tmp_path / "sandbox")
    ingest.ingest_csv(live.csv_path, live.db)
    target = foreign / "target"
    target.mkdir(parents=True, exist_ok=True)  # the move destination must exist to be measured
    conn = db.open_db(live.db)
    try:
        listing = opportunities.build_opportunities(
            conn,
            rules.load_rules(include_user=False),
            min_size=1 * MIB,
            now=SANDBOX_NOW,
            db_path=str(live.db),
        )
    finally:
        conn.close()
    try:
        yield LiveSandbox(
            live=live,
            listing=listing,
            db_path=live.db,
            data_root=tmp_path / "data",
            target=target,
            quarantine=tmp_path / "sandbox" / "quarantine",
        )
    finally:
        gen_live.cleanup(foreign)


@pytest.fixture
def sandbox_window(qtbot: object, qapp: object, tmp_path: Path) -> Callable[..., MainWindow]:
    """A shown main window over a live sandbox (target drive configured).

    Pass ``ai=`` to put a working (scripted) provider behind the window's AI
    layer; without it the window gets the offline service the fixture builds.
    """

    def build(sandbox: LiveSandbox, ai: ai_models.AIService | None = None) -> MainWindow:
        settings = state.Settings.persisted(tmp_path / "settings.ini")
        settings.set_target_drive(str(sandbox.target))
        settings.set_reserve_bytes(0)
        settings.set_quarantine_dir(str(sandbox.quarantine))
        manager = theme.ThemeManager(qapp, mode=theme.MODE_LIGHT)
        window = create_window(
            qapp,
            settings,
            db_path=sandbox.db_path,
            data_root=sandbox.data_root,
            theme_manager=manager,
            ai_service=ai,
        )
        qtbot.addWidget(window)  # type: ignore[attr-defined]
        window.resize(1440, 900)
        window.show()
        window.set_listing(sandbox.listing)
        _settle(qtbot)
        return window

    return build
