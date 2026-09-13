# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller spec: the windowed, single-file SpaceSage (design §14).

Build it with::

    uv run --extra build pyinstaller packaging/spacesage.spec --noconfirm \\
        --distpath dist --workpath build/pyinstaller

What this file is responsible for:

* **One file, no console.**  A one-file build (``EXE`` gets the binaries and the
  data, no ``COLLECT``) with ``console=False``: on Windows the user never sees a
  black window next to the app, which is the whole point of a GUI program.
  Errors still reach a human -- the app reports a startup failure through a
  dialog (see ``spacesage.app.main.fatal``).
* **The app's own data.**  The Lucide icon subset and the app icon are read at
  runtime through ``importlib.resources``, so they have to be in the bundle:
  a build that forgot them would raise ``IconError`` on the first paint.
* **Icon and version resource.**  ``packaging/spacesage.ico`` (rendered from
  ``spacesage/app/assets/app-icon.svg`` by ``scripts/make_app_icons.py``) plus a
  version resource generated from ``spacesage.__version__`` -- both Windows
  concerns, and both no-ops elsewhere.
* **Entry point.**  ``spacesage/app/__main__.py``, the same module a source run
  executes.  Nothing is duplicated for the frozen build except the Qt probe,
  which re-runs the executable instead of ``python -m`` (``main.self_check_command``).
"""

import importlib.util
import sys
from pathlib import Path

SPEC_DIR = Path(SPECPATH).resolve()  # noqa: F821 - PyInstaller injects SPECPATH
ROOT = SPEC_DIR.parent
# PyInstaller injects its configuration as ``workpath`` (lower case); the
# fallback keeps the file importable by the test suite, which execs the spec.
WORK_DIR = Path(globals().get("workpath") or ROOT / "build" / "pyinstaller").resolve()


def _load_win_version():
    """Import ``packaging/win_version.py`` without shadowing PyPI's ``packaging``."""
    module_path = SPEC_DIR / "win_version.py"
    spec = importlib.util.spec_from_file_location("spacesage_win_version", module_path)
    if spec is None or spec.loader is None:  # pragma: no cover - a missing file is a build error
        raise SystemExit(f"cannot load {module_path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _version_resource() -> str | None:
    """Write the Windows version resource and return its path (Windows only)."""
    if sys.platform != "win32":
        return None
    module = _load_win_version()
    target = WORK_DIR / "win-version-info.txt"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(module.render(), encoding="utf-8")
    return str(target)


ASSETS = ROOT / "spacesage" / "app" / "assets"
ICON = SPEC_DIR / "spacesage.ico"
ENTRY = ROOT / "spacesage" / "app" / "__main__.py"

if not ICON.is_file():  # pragma: no cover - a missing icon is a build error
    raise SystemExit(
        f"{ICON} is missing; regenerate it with: uv run python scripts/make_app_icons.py"
    )

datas = [
    (str(ASSETS / "icons"), "spacesage/app/assets/icons"),
    (str(ASSETS / "app-icon.svg"), "spacesage/app/assets"),
    (str(ASSETS / "ATTRIBUTION.md"), "spacesage/app/assets"),
]

# The Qt modules the app imports directly; PyInstaller's PySide6 hook resolves
# their plugins and libraries on top of this.
hiddenimports = [
    "PySide6.QtCore",
    "PySide6.QtGui",
    "PySide6.QtSvg",
    "PySide6.QtWidgets",
]

a = Analysis(  # noqa: F821 - PyInstaller injects Analysis
    [str(ENTRY)],
    pathex=[str(ROOT)],
    binaries=[],
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    # The AI layer talks to providers over urllib, and the engine is stdlib-only:
    # only the GUI extra (PySide6) is a runtime dependency, so nothing here needs
    # a module exclusion list.
    excludes=[],
    noarchive=False,
    optimize=0,
)

pyz = PYZ(a.pure)  # noqa: F821 - injected

exe = EXE(  # noqa: F821 - injected
    pyz,
    a.scripts,
    a.binaries,
    a.datas,
    [],
    name="spacesage",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    upx_exclude=[],
    runtime_tmpdir=None,
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon=str(ICON),
    version=_version_resource(),
)
