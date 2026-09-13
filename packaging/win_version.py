"""The Windows version resource of the packaged executable (design §14).

Windows shows an executable's version info in the Explorer properties sheet and
in every security prompt; a PyInstaller build gets it from a ``VSVersionInfo``
file.  Writing that file by hand means the version lives in two places, so this
module renders it from :data:`spacesage.__version__` -- the single source of
truth ``pyproject.toml`` already reads -- and the spec writes it into the build's
work directory.

Only the spec and the tests import this; nothing here is needed at runtime.
"""

from __future__ import annotations

import re

from spacesage import __version__

COMPANY_NAME = "SpaceSage"
PRODUCT_NAME = "SpaceSage"
FILE_DESCRIPTION = "SpaceSage — turn a WizTree export into a safe cleanup plan"
LEGAL_COPYRIGHT = "MIT licensed. Not affiliated with WizTree / Antibody Software."
ORIGINAL_FILENAME = "spacesage.exe"

#: ``1.2.3`` (PEP 440 allows far more than Windows resources do).
_NUMBERS = re.compile(r"(\d+)\.(\d+)\.(\d+)")


def version_tuple(version: str = __version__) -> tuple[int, int, int, int]:
    """A PEP 440 version as the four integers a Windows resource carries.

    ``0.1.0.dev0`` is still *release 0.1.0* to Windows: the fourth field is the
    build number, and a development build is not a build number of its own.
    """
    match = _NUMBERS.match(version)
    if match is None:  # pragma: no cover - a version without numbers is a build error
        raise ValueError(f"cannot express {version!r} as a Windows version tuple")
    major, minor, patch = (int(part) for part in match.groups())
    return major, minor, patch, 0


def render(version: str = __version__) -> str:
    """The ``VSVersionInfo`` text PyInstaller's ``version=`` expects."""
    major, minor, patch, build = version_tuple(version)
    pairs = (major, minor, patch, build)
    language = "040904B0"  # U.S. English, Unicode -- the table Windows reads
    fields = (
        ("CompanyName", COMPANY_NAME),
        ("FileDescription", FILE_DESCRIPTION),
        ("FileVersion", f"{major}.{minor}.{patch}"),
        ("InternalName", PRODUCT_NAME),
        ("LegalCopyright", LEGAL_COPYRIGHT),
        ("OriginalFilename", ORIGINAL_FILENAME),
        ("ProductName", PRODUCT_NAME),
        ("ProductVersion", version),
    )
    strings = ",\n".join(
        f"                StringStruct({key!r}, {value!r})" for key, value in fields
    )
    return f"""VSVersionInfo(
  ffi=FixedFileInfo(
    filevers={pairs},
    prodvers={pairs},
    mask=0x3F,
    flags=0x0,
    OS=0x40004,
    fileType=0x1,
    subtype=0x0,
    date=(0, 0),
  ),
  kids=[
    StringFileInfo(
      [
        StringTable(
          {language!r},
          [
{strings}
          ]
        )
      ]
    ),
    VarFileInfo([VarStruct('Translation', [1033, 1200])]),
  ]
)
"""
