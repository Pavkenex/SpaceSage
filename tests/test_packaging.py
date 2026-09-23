"""Packaging gates: the spec, the icon, the workflows and the docs cannot rot.

The build itself takes minutes and needs PyInstaller, so CI runs it (the
``package`` job) and a human can run it locally; what these tests cover is
everything *around* that: the spec wires the right files and flags, the icon the
executable embeds is the one the source SVG draws, the workflows reference real
paths and pinned actions, and every link and screenshot the docs point at exists.
"""

from __future__ import annotations

import importlib.util
import os
import re
import struct
import sys
from pathlib import Path
from typing import Any

import pytest
import yaml

REPO_ROOT = Path(__file__).resolve().parents[1]
PACKAGING_DIR = REPO_ROOT / "packaging"
SPEC_PATH = PACKAGING_DIR / "spacesage.spec"
ICON_ICO = PACKAGING_DIR / "spacesage.ico"
ICON_PNG = REPO_ROOT / "docs" / "assets" / "app-icon.png"
ICON_SVG = REPO_ROOT / "spacesage" / "app" / "assets" / "app-icon.svg"
WORKFLOWS = REPO_ROOT / ".github" / "workflows"
RENDERS = REPO_ROOT / "artifacts" / "gui"

#: The sizes the icon container has to carry (Explorer's list, desk and tile).
EXPECTED_ICO_SIZES = (16, 24, 32, 48, 64, 128, 256)


# --------------------------------------------------------------------------- #
# The PyInstaller spec, executed the way PyInstaller executes it
# --------------------------------------------------------------------------- #


def run_spec(workpath: Path) -> dict[str, Any]:
    """Execute the spec with recording stand-ins for PyInstaller's classes.

    Returns what the build *would* be told to do.  Running the real thing is
    CI's job; this is the check that catches a spec whose data files, entry
    script, icon or windowed mode drifted -- before a three-minute build does.
    """
    calls: dict[str, Any] = {}

    class Analysis:
        def __init__(self, scripts: list[str], **kwargs: Any) -> None:
            calls["Analysis"] = {"scripts": scripts, **kwargs}
            self.pure = ["pure-modules"]
            self.scripts = ["scripts"]
            self.binaries = ["binaries"]
            self.datas = ["datas"]

    class PYZ:
        def __init__(self, *args: Any, **kwargs: Any) -> None:
            calls["PYZ"] = {"args": args, "kwargs": kwargs}

    class EXE:
        def __init__(self, *args: Any, **kwargs: Any) -> None:
            calls["EXE"] = {"args": args, **kwargs}

    namespace: dict[str, Any] = {
        "SPECPATH": str(SPEC_PATH.parent),
        "SPEC": str(SPEC_PATH),
        "workpath": str(workpath),
        "Analysis": Analysis,
        "PYZ": PYZ,
        "EXE": EXE,
        "os": os,
    }
    exec(compile(SPEC_PATH.read_bytes(), str(SPEC_PATH), "exec"), namespace)
    return calls


def test_spec_builds_one_windowed_file(tmp_path: Path) -> None:
    """One file, no console, the app's own entry point, the committed icon."""
    calls = run_spec(tmp_path)

    analysis = calls["Analysis"]
    assert analysis["scripts"] == [str(REPO_ROOT / "spacesage" / "app" / "__main__.py")]
    assert Path(analysis["scripts"][0]).is_file(), "the packaged entry point must exist"
    assert analysis["pathex"] == [str(REPO_ROOT)], "the package is imported from the checkout"

    exe = calls["EXE"]
    assert exe["name"] == "spacesage"
    assert exe["console"] is False, "a GUI program must not open a console window"
    assert exe["icon"] == str(ICON_ICO) and ICON_ICO.is_file()
    # One-file: the EXE itself takes the binaries and the data, and no COLLECT
    # collects them into a folder (the spec would have raised NameError here if
    # it called one -- the namespace above deliberately has no COLLECT).
    assert list(exe["args"][2:4]) == [["binaries"], ["datas"]]
    assert "COLLECT" not in calls


def test_spec_ships_what_the_app_reads_at_runtime(tmp_path: Path) -> None:
    """The Lucide subset, the app icon, the attribution and the rule packs."""
    from spacesage import rules

    datas = run_spec(tmp_path)["Analysis"]["datas"]
    bundled = {Path(source).resolve(): dest for source, dest in datas}

    icons = (REPO_ROOT / "spacesage" / "app" / "assets" / "icons").resolve()
    assert bundled.get(icons) == "spacesage/app/assets/icons"
    assert bundled.get(ICON_SVG.resolve()) == "spacesage/app/assets"
    assert (REPO_ROOT / "spacesage" / "app" / "assets" / "ATTRIBUTION.md").resolve() in bundled
    # rules.BUILTIN_RULES_DIR is package-relative; a frozen build resolves it
    # under the extraction dir, so the packs must land at that exact path.
    builtin_rules = rules.BUILTIN_RULES_DIR.resolve()
    assert builtin_rules.is_dir() and list(builtin_rules.glob("*.toml"))
    assert bundled.get(builtin_rules) == "spacesage/rules"

    for source in bundled:
        assert Path(source).exists(), f"{source} is referenced by the spec but missing"

    # The icon subset the shell actually paints is part of that directory.
    from spacesage.app.windows import PAGES

    names = {name for _key, _label, name in PAGES}
    assert names <= {entry.stem for entry in icons.iterdir()}

    hidden = run_spec(tmp_path)["Analysis"]["hiddenimports"]
    assert {"PySide6.QtCore", "PySide6.QtGui", "PySide6.QtSvg", "PySide6.QtWidgets"} <= set(hidden)


def test_spec_embeds_the_windows_version_resource(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """On Windows the build gets a version resource generated from ``__version__``.

    The resource is what the Explorer properties sheet and Windows' security
    prompts show, so it must not be a second place the version is written down.
    """
    from spacesage import __version__

    monkeypatch.setattr(sys, "platform", "win32")
    exe = run_spec(tmp_path)["EXE"]
    written = Path(exe["version"])
    assert written.is_file(), "the spec has to write the version resource it passes to EXE"
    assert written.read_text(encoding="utf-8").startswith("VSVersionInfo(")
    assert __version__ in written.read_text(encoding="utf-8")

    monkeypatch.setattr(sys, "platform", "linux")
    assert run_spec(tmp_path)["EXE"]["version"] is None, "no version resource off Windows"


# --------------------------------------------------------------------------- #
# The version resource and the icon the executable carries
# --------------------------------------------------------------------------- #


def load_win_version() -> Any:
    """Import ``packaging/win_version.py`` by path (the name would shadow PyPI's)."""
    spec = importlib.util.spec_from_file_location("win_version", PACKAGING_DIR / "win_version.py")
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_version_resource_carries_the_package_version() -> None:
    """``0.1.0.dev0`` is release 0.1.0 to Windows, with the PEP 440 string kept."""
    from spacesage import __version__

    win_version = load_win_version()
    assert win_version.version_tuple("1.2.3") == (1, 2, 3, 0)
    assert win_version.version_tuple("0.1.0.dev0") == (0, 1, 0, 0)
    assert win_version.version_tuple("2.10.4rc1") == (2, 10, 4, 0)
    with pytest.raises(ValueError):
        win_version.version_tuple("no-numbers-here")

    rendered = win_version.render(__version__)
    assert f"filevers={win_version.version_tuple(__version__)}" in rendered
    assert f"'ProductVersion', {__version__!r}" in rendered
    assert "'OriginalFilename', 'spacesage.exe'" in rendered


def versioninfo_stub() -> dict[str, Any]:
    """Local stand-ins for the classes a PyInstaller version file calls.

    PyInstaller's own loader pulls in ``pefile``, which is a Windows-only
    dependency, so the rendered text is checked against this stand-in on every
    platform -- and again with PyInstaller's real classes where they import.
    """

    class VSVersionInfo:
        def __init__(self, ffi: Any = None, kids: Any = None) -> None:
            self.ffi, self.kids = ffi, list(kids or [])

    class FixedFileInfo:
        def __init__(self, **kwargs: Any) -> None:
            self.kwargs = kwargs
            self.filevers, self.prodvers = kwargs["filevers"], kwargs["prodvers"]

    class StringFileInfo:
        def __init__(self, kids: Any = None) -> None:
            self.kids = list(kids or [])

    class StringTable:
        def __init__(self, name: Any = None, kids: Any = None) -> None:
            self.name, self.kids = name, list(kids or [])

    class StringStruct:
        def __init__(self, name: Any = None, val: Any = None) -> None:
            self.name, self.val = name, val

    class VarFileInfo:
        def __init__(self, kids: Any = None) -> None:
            self.kids = list(kids or [])

    class VarStruct:
        def __init__(self, name: Any = None, kids: Any = None) -> None:
            self.name, self.kids = name, list(kids or [])

    return {
        "VSVersionInfo": VSVersionInfo,
        "FixedFileInfo": FixedFileInfo,
        "StringFileInfo": StringFileInfo,
        "StringTable": StringTable,
        "StringStruct": StringStruct,
        "VarFileInfo": VarFileInfo,
        "VarStruct": VarStruct,
    }


def test_version_resource_is_valid_vsversioninfo_text() -> None:
    """The rendered resource parses, and carries the version the app reports."""
    from spacesage import __version__

    win_version = load_win_version()
    info = eval(win_version.render(), {}, versioninfo_stub())

    major, minor, patch, build = win_version.version_tuple()
    assert info.ffi.filevers == (major, minor, patch, build)
    assert info.ffi.prodvers == (major, minor, patch, build)

    table = info.kids[0].kids[0]
    assert table.name == "040904B0", "Windows reads the U.S. English/Unicode table"
    entries = {entry.name: entry.val for entry in table.kids}
    assert entries["FileVersion"] == f"{major}.{minor}.{patch}"
    assert entries["ProductVersion"] == __version__
    assert entries["ProductName"] == "SpaceSage"
    assert entries["OriginalFilename"] == "spacesage.exe"
    assert info.kids[1].kids[0].name == "Translation"


def test_version_resource_parses_with_pyinstaller() -> None:
    """The same text through PyInstaller's real classes (where they import)."""
    versioninfo = pytest.importorskip(
        "PyInstaller.utils.win32.versioninfo",
        reason="PyInstaller (+ pefile) is the optional [build] extra",
    )
    win_version = load_win_version()
    info = eval(
        win_version.render(),
        {},
        {
            "VSVersionInfo": versioninfo.VSVersionInfo,
            "FixedFileInfo": versioninfo.FixedFileInfo,
            "StringFileInfo": versioninfo.StringFileInfo,
            "StringTable": versioninfo.StringTable,
            "StringStruct": versioninfo.StringStruct,
            "VarFileInfo": versioninfo.VarFileInfo,
            "VarStruct": versioninfo.VarStruct,
        },
    )
    assert isinstance(info, versioninfo.VSVersionInfo)
    fixed = info.ffi
    # PyInstaller 6.22 stopped keeping the ``filevers`` tuple; the same numbers
    # are the MS/LS halves of the fixed file info.
    version = getattr(fixed, "filevers", None)
    if version is None:
        version = (
            fixed.fileVersionMS >> 16,
            fixed.fileVersionMS & 0xFFFF,
            fixed.fileVersionLS >> 16,
            fixed.fileVersionLS & 0xFFFF,
        )
    assert version == win_version.version_tuple()
    assert info.kids[0].kids[0].kids, "the string table has to carry the fields"


def test_ico_container_carries_every_size_windows_asks_for() -> None:
    """The committed .ico is a real icon container: PNG entries, one per size."""
    data = ICON_ICO.read_bytes()
    reserved, kind, count = struct.unpack("<HHH", data[:6])
    assert (reserved, kind) == (0, 1), "not an icon directory"
    assert count == len(EXPECTED_ICO_SIZES)

    seen: list[int] = []
    for index in range(count):
        entry = data[6 + 16 * index : 6 + 16 * (index + 1)]
        width, height, _colors, _reserved, planes, depth, length, offset = struct.unpack(
            "<BBBBHHII", entry
        )
        assert (planes, depth) == (1, 32)
        declared = 256 if width == 0 else width
        assert height == width
        payload = data[offset : offset + length]
        assert payload.startswith(b"\x89PNG\r\n\x1a\n"), "entries are PNG-compressed"
        # The PNG's own header must agree with the directory entry.
        png_width, png_height = struct.unpack(">II", payload[16:24])
        assert (png_width, png_height) == (declared, declared)
        seen.append(declared)
    assert tuple(seen) == EXPECTED_ICO_SIZES


def test_committed_icon_derivatives_are_the_source_svg(tmp_path: Path) -> None:
    """The .ico and the PNG are what the source SVG renders to -- not a stale copy.

    Sampled at flat, unambiguous points (background, drive body, slot, light,
    outside the rounded corner) with a tolerance, so a different Qt build's
    antialiasing cannot fail the suite while a redrawn or forgotten icon does.
    """
    pytest.importorskip("PySide6.QtGui")
    from PySide6.QtCore import QByteArray
    from PySide6.QtGui import QImage, QPainter
    from PySide6.QtSvg import QSvgRenderer

    renderer = QSvgRenderer(QByteArray(ICON_SVG.read_bytes()))
    assert renderer.isValid(), "the app icon source is not valid SVG"
    rendered = QImage(256, 256, QImage.Format.Format_ARGB32_Premultiplied)
    rendered.fill(0)
    painter = QPainter(rendered)
    try:
        renderer.render(painter)
    finally:
        painter.end()

    committed = QImage(str(ICON_PNG))
    assert not committed.isNull() and (committed.width(), committed.height()) == (256, 256)

    points = [(20, 20), (40, 40), (128, 200), (128, 161), (170, 201), (128, 60), (246, 246)]
    for x, y in points:
        produced = rendered.pixelColor(x, y)
        stored = committed.pixelColor(x, y)
        for channel in ("red", "green", "blue", "alpha"):
            delta = abs(getattr(produced, channel)() - getattr(stored, channel)())
            assert delta <= 32, (
                f"{ICON_PNG.name} differs from the source SVG at ({x}, {y}).{channel} "
                f"by {delta}; regenerate with: uv run python scripts/make_app_icons.py"
            )


# --------------------------------------------------------------------------- #
# The workflows
# --------------------------------------------------------------------------- #


def load_workflow(name: str) -> dict[str, Any]:
    """Parse one workflow file."""
    document = yaml.safe_load((WORKFLOWS / name).read_text(encoding="utf-8"))
    assert isinstance(document, dict)
    return document


def test_ci_builds_runs_and_uploads_the_bundle() -> None:
    """The spec cannot rot: every push builds it and *runs* what it produced."""
    jobs = load_workflow("ci.yml")["jobs"]
    assert {"ruff", "mypy", "pytest", "package"} <= set(jobs)

    steps = jobs["package"]["steps"]
    runs = [step.get("run", "") for step in steps]
    installs = " ".join(runs)
    assert "uv sync --frozen --extra build" in installs, "PyInstaller comes from the build extra"

    build = next(run for run in runs if "pyinstaller" in run)
    assert "packaging/spacesage.spec" in build
    assert SPEC_PATH.is_file()

    assert any("--self-check" in run for run in runs), "the bundle has to start on CI"
    assert any("--capture" in run for run in runs), "the bundle has to render its window"
    assert any("--capture-delay" in run for run in runs), "and wait for it to settle"

    uploads = [
        step for step in steps if str(step.get("uses", "")).startswith("actions/upload-artifact")
    ]
    assert uploads, "a smoke build nobody can download is not evidence"
    uploaded = uploads[0]["with"]["path"]
    assert "dist/spacesage" in uploaded and "smoke.png" in uploaded
    assert uploads[0]["with"]["if-no-files-found"] == "error"


def test_release_workflow_tags_into_an_exe_and_a_bundle() -> None:
    """A tag builds both platforms, smoke-tests them and publishes them."""
    workflow = load_workflow("release.yml")
    # YAML 1.1 parses a bare ``on:`` key as the boolean True, hence the fallback.
    triggers = workflow.get("on") or workflow[True]
    assert "v*" in triggers["push"]["tags"]
    assert workflow["permissions"]["contents"] == "write", "publishing needs a writable token"

    jobs = workflow["jobs"]
    assert {"verify-version", "windows", "linux", "release"} <= set(jobs)

    windows = [step.get("run", "") for step in jobs["windows"]["steps"]]
    assert any("pyinstaller" in run and "packaging/spacesage.spec" in run for run in windows)
    assert any("--self-check" in run for run in windows)
    assert any("--capture" in run for run in windows)

    linux = [step.get("run", "") for step in jobs["linux"]["steps"]]
    assert any("spacesage-linux-x86_64.tar.gz" in run for run in linux)
    assert any("--capture" in run for run in linux)

    publish = jobs["release"]["steps"][-1]
    assert publish["uses"].startswith("softprops/action-gh-release")
    published = publish["with"]["files"]
    assert "spacesage.exe" in published and "spacesage-linux-x86_64.tar.gz" in published
    assert published.count("smoke-") == 2, "each artifact ships with the render that proves it"


def test_every_workflow_parses_and_pins_its_actions() -> None:
    """No floating action refs: an action is used at a version, not at ``main``."""
    files = sorted(WORKFLOWS.glob("*.yml"))
    assert files, "the repository has to keep its workflows"
    for path in files:
        document = yaml.safe_load(path.read_text(encoding="utf-8"))
        assert isinstance(document, dict), f"{path.name} is not a mapping"
        assert "jobs" in document, f"{path.name} has no jobs"

        def walk(node: Any, where: Path = path) -> None:
            if isinstance(node, dict):
                for key, value in node.items():
                    if key == "uses":
                        assert re.fullmatch(r"[^@\s]+@[vV]?\d[\w.\-]*", str(value)), (
                            f"{where.name}: {value} is not pinned to a major version"
                        )
                    walk(value)
            elif isinstance(node, list):
                for item in node:
                    walk(item)

        walk(document)


# --------------------------------------------------------------------------- #
# The docs
# --------------------------------------------------------------------------- #

MARKDOWN = [
    REPO_ROOT / "README.md",
    REPO_ROOT / "CHANGELOG.md",
    *sorted((REPO_ROOT / "docs").rglob("*.md")),
]
LINK = re.compile(r"!?\[[^\]]*\]\(([^)\s]+)(?:\s+\"[^\"]*\")?\)")


def test_docs_links_and_screenshots_resolve() -> None:
    """Every relative link and every screenshot a doc shows exists on disk."""
    missing: list[str] = []
    for document in MARKDOWN:
        text = document.read_text(encoding="utf-8")
        for target in LINK.findall(text):
            if target.startswith(("http://", "https://", "mailto:", "#")):
                continue
            relative = target.split("#", 1)[0]
            if not relative:
                continue
            if not (document.parent / relative).exists():
                missing.append(f"{document.relative_to(REPO_ROOT)} -> {target}")
    assert not missing, "docs point at files that do not exist:\n" + "\n".join(missing)


def test_readme_leads_with_the_app() -> None:
    """The product surface comes first; the engine library is the secondary note."""
    text = (REPO_ROOT / "README.md").read_text(encoding="utf-8")
    links = LINK.findall(text)
    assert links, "the README shows something"
    first = links[0]
    assert first.startswith(("artifacts/gui/", "docs/assets/")), (
        f"the README opens with a real screenshot of the app, not {first!r}"
    )

    # The app's own headings (quick start, the screens, the safety contract) all
    # come before the engine section; the engine is the second half of the page.
    app_headings = ["## Quick start", "## The app in pictures", "## Desktop app"]
    engine_at = text.find("## Engine")
    assert engine_at != -1, "the README documents the engine library too"
    app_at = min(
        (text.find(heading) for heading in app_headings if text.find(heading) != -1), default=-1
    )
    assert app_at != -1 and app_at < engine_at, (
        "the app has to be introduced before the engine-library section"
    )


def test_changelog_follows_keep_a_changelog() -> None:
    """Design §14: semantic versioning with a changelog, newest release first."""
    text = (REPO_ROOT / "CHANGELOG.md").read_text(encoding="utf-8")
    assert text.startswith("# Changelog")
    assert "## [Unreleased]" in text
    headings = re.findall(r"^## \[([^\]]+)\]", text, flags=re.MULTILINE)
    assert headings and headings[0] == "Unreleased", "the changelog leads with unreleased work"
    for name in headings[1:]:
        assert re.fullmatch(r"\d+\.\d+\.\d+", name), f"{name!r} is not a semantic version"

    readme = (REPO_ROOT / "README.md").read_text(encoding="utf-8")
    assert "CHANGELOG.md" in readme, "the changelog is reachable from the README"


def test_app_guide_shows_every_screen() -> None:
    """The walkthrough is screen by screen, with the renders it talks about."""
    guide = (REPO_ROOT / "docs" / "app-guide.md").read_text(encoding="utf-8")
    for render in (
        "import.png",
        "opportunities.png",
        "details.png",
        "plan.png",
        "dryrun.png",
        "confirm.png",
        "execute.png",
        "undo.png",
        "providers.png",
        "suggestions_filled.png",
        "explain.png",
        "review.png",
        "opportunities-dark.png",
    ):
        assert render in guide, f"{render} is not shown in the guide"
        assert (RENDERS / render).is_file(), f"{render} has not been rendered"
