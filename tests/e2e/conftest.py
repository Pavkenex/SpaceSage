"""Fixtures of the end-to-end suite: the planted disk, the CLI driver, artifacts.

The suite is the S12 acceptance pass (``docs/slices.md``): one "full disk"
scenario (``scenario.py``) is driven through every stage the project has -- the
engine pipeline through the real CLI and the desktop app through its own widget
tree -- and the runs are compared against each other, stage by stage.

Three scenarios are planted, all under one root:

``full_disk``
    session-wide, read-only: ingest, classify, candidates, plan, report,
``mutation_disk``
    one per test that *changes* the disk: dry run, execute, undo,
``smoke_disk``
    one for the GUI smoke pass, which drives the app end to end (and changes
    its own copy, never the shared one).

The CLI is always run the way a user or CI would run it -- a subprocess of
``python -m spacesage`` in the repository root -- so the driver covers argument
parsing, exit codes and stdout, not just the library calls underneath.
"""

from __future__ import annotations

import os
import subprocess
import sys
from collections.abc import Callable, Iterator
from pathlib import Path

import pytest

# Offscreen before Qt picks a platform: the app renders for real but needs no display.
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

E2E_DIR = Path(__file__).resolve().parent
TESTS_DIR = E2E_DIR.parent
REPO_ROOT = TESTS_DIR.parent

# ``scenario.py`` lives in this directory; ``tests/`` holds the shared fixture
# generators (``fixtures.gen_live``) it builds on -- both importable by name.
for entry in (str(E2E_DIR), str(TESTS_DIR)):
    if entry not in sys.path:
        sys.path.insert(0, entry)

import scenario  # noqa: E402

ARTIFACT_DIR = Path(os.environ.get("SPACESAGE_E2E_ARTIFACTS", REPO_ROOT / "artifacts" / "e2e"))
"""Where the smoke pass and the pipeline write their renders and demo files."""


def scenario_root() -> Path:
    """Root the suite plants its scenarios in (``SPACESAGE_E2E_ROOT`` overrides)."""
    override = os.environ.get("SPACESAGE_E2E_ROOT")
    return Path(override).expanduser() if override else scenario.DEFAULT_ROOT


@pytest.fixture(scope="session")
def full_disk() -> Iterator[scenario.FullDisk]:
    """The planted full disk every read-only stage of the pipeline runs on.

    The scenario is POSIX-shaped on purpose (symlinks, a second temporary
    volume): where the host cannot host it, the suite skips with the reason
    instead of failing on an environment it was never meant to run in.
    """
    reason = scenario.unavailable_reason()
    if reason is not None:
        pytest.skip(reason)
    try:
        planted = scenario.build(scenario_root())
    except scenario.ScenarioUnavailable as unavailable:
        pytest.skip(str(unavailable))
    try:
        yield planted
    finally:
        scenario.cleanup(planted.root)


@pytest.fixture
def mutation_disk(full_disk: scenario.FullDisk) -> Iterator[scenario.FullDisk]:
    """A private copy of the scenario for the run that executes and undoes."""
    planted = scenario.build(full_disk.root / "mutation")
    try:
        yield planted
    finally:
        scenario.cleanup(planted.root)


@pytest.fixture
def smoke_disk(full_disk: scenario.FullDisk) -> Iterator[scenario.FullDisk]:
    """A private copy of the scenario for the GUI smoke pass."""
    planted = scenario.build(full_disk.root / "smoke")
    try:
        yield planted
    finally:
        scenario.cleanup(planted.root)


class Cli:
    """``python -m spacesage`` in a subprocess; fails the test on a bad exit code."""

    def __init__(self) -> None:
        self.calls: list[list[str]] = []

    def __call__(self, *args: str, check: bool = True) -> subprocess.CompletedProcess[str]:
        """Run one CLI invocation and (unless ``check=False``) insist it worked."""
        self.calls.append(list(args))
        result = subprocess.run(
            [sys.executable, "-m", "spacesage", *args],
            capture_output=True,
            text=True,
            cwd=str(REPO_ROOT),
        )
        if check and result.returncode != 0:
            pytest.fail(
                f"spacesage {' '.join(args)} exited {result.returncode}\n"
                f"--- stdout\n{result.stdout[-4000:]}\n--- stderr\n{result.stderr[-4000:]}"
            )
        return result


@pytest.fixture
def cli() -> Cli:
    """The CLI driver (one per test; ``.calls`` records what was run)."""
    return Cli()


@pytest.fixture
def artifacts() -> Path:
    """Where the suite writes its evidence (renders, demo plan, run logs)."""
    return ARTIFACT_DIR


@pytest.fixture
def evidence(artifacts: Path) -> Callable[[str, str], Path]:
    """Write one piece of text evidence into the artifact directory."""

    def write(name: str, text: str) -> Path:
        artifacts.mkdir(parents=True, exist_ok=True)
        path = artifacts / name
        path.write_text(text, encoding="utf-8")
        return path

    return write


@pytest.fixture(autouse=True)
def no_blocking_dialogs(monkeypatch: pytest.MonkeyPatch) -> list[tuple[str, str]]:
    """Capture the app's two message boxes so a modal can never hang the suite.

    Same seam the GUI suite uses: ``report_error``/``report_note`` wrap
    ``QMessageBox.exec()``, which waits forever without a user.  The smoke pass
    asserts the list is empty afterwards -- a run that had to tell the user
    something went wrong is a failed pass, not a diagnosis to scroll past.
    """
    from spacesage.app import dialogs

    seen: list[tuple[str, str]] = []

    def capture(_parent: object, title: str, message: str) -> None:
        seen.append((title, message))

    monkeypatch.setattr(dialogs, "report_error", capture)
    monkeypatch.setattr(dialogs, "report_note", capture)
    return seen


@pytest.fixture(autouse=True)
def isolation(monkeypatch: pytest.MonkeyPatch, tmp_path_factory: pytest.TempPathFactory) -> None:
    """No stage may read the developer's rule packs, AI config or AI cache."""
    from spacesage import rules
    from spacesage.ai import config as ai_config

    home = tmp_path_factory.mktemp("isolation")
    monkeypatch.setenv(rules.RULES_ENV_VAR, str(home / "rules"))
    monkeypatch.setenv(ai_config.CONFIG_ENV_VAR, str(home / "ai.toml"))
    monkeypatch.setenv(ai_config.CACHE_DIR_ENV_VAR, str(home / "ai-cache"))


@pytest.fixture
def grab_png() -> Callable[..., Path]:
    """Render a widget to a PNG, proving the render is not a blank frame."""
    return grab


def grab(widget: object, path: Path, *, minimum_bytes: int = 12_000) -> Path:
    """Render a widget to a PNG and prove the render is not an empty frame.

    Everything queued (a layout request from a label whose text just changed, a
    pending paint) is flushed first: a frame taken mid-relayout is a frame of a
    geometry the user never sees, and it is easy to mistake for a clipped screen.

    The same proof the GUI suite's screenshots carry (``tests/gui/conftest.py``):
    a frame with three colours is not a screen, so it may not become evidence.
    """
    from PySide6.QtGui import QPixmap
    from PySide6.QtWidgets import QApplication

    application = QApplication.instance()
    if application is not None:
        application.processEvents()

    pixmap = widget.grab()  # type: ignore[attr-defined]
    assert isinstance(pixmap, QPixmap) and not pixmap.isNull(), "widget did not render"
    assert _distinct_colours(pixmap) > 25, "the render looks blank (single-colour frame)"
    path.parent.mkdir(parents=True, exist_ok=True)
    assert pixmap.save(str(path)), f"could not write {path}"
    assert path.stat().st_size >= minimum_bytes, f"{path} is suspiciously small"
    return path


def _distinct_colours(pixmap: object, *, sample: int = 5) -> int:
    """How many distinct colours a render has (a blank frame has very few)."""
    image = pixmap.toImage()  # type: ignore[attr-defined]
    seen: set[int] = set()
    for y in range(0, image.height(), sample):
        for x in range(0, image.width(), sample):
            seen.add(image.pixel(x, y))
    return len(seen)
