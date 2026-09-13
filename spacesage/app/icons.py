"""Bundled Lucide icons, tinted from the theme tokens (design §9.1).

The SVG subset lives in ``spacesage/app/assets/icons/`` (ISC/MIT, see the
attribution file next to it).  Lucide icons are stroked with ``currentColor``,
so tinting is a text substitution followed by an :class:`QSvgRenderer` pass --
no image assets, no colour drift between themes, and every icon stays crisp at
any device pixel ratio.
"""

from __future__ import annotations

from functools import lru_cache
from importlib import resources

from PySide6.QtCore import QByteArray, Qt
from PySide6.QtGui import QIcon, QPainter, QPixmap
from PySide6.QtSvg import QSvgRenderer

from spacesage.app import theme

_PACKAGE = "spacesage.app.assets.icons"

#: Rendered at 2x and drawn at the logical size, so icons stay sharp on HiDPI.
DEVICE_RATIO = 2


class IconError(RuntimeError):
    """Raised when an icon is not part of the bundled subset."""


@lru_cache(maxsize=256)
def svg_source(name: str) -> str:
    """The bundled SVG text of one Lucide icon."""
    path = resources.files(_PACKAGE).joinpath(f"{name}.svg")
    if not path.is_file():
        raise IconError(f"icon {name!r} is not part of the bundled subset")
    return path.read_text(encoding="utf-8")


def available() -> tuple[str, ...]:
    """Names of every bundled icon."""
    return tuple(
        sorted(entry.name[: -len(".svg")] for entry in resources.files(_PACKAGE).iterdir())
    )


@lru_cache(maxsize=512)
def pixmap(name: str, color: str, size: int = 16) -> QPixmap:
    """One icon rendered in ``color`` at ``size`` logical pixels."""
    source = svg_source(name).replace("currentColor", color)
    renderer = QSvgRenderer(QByteArray(source.encode("utf-8")))
    target = QPixmap(size * DEVICE_RATIO, size * DEVICE_RATIO)
    target.fill(Qt.GlobalColor.transparent)
    painter = QPainter(target)
    try:
        renderer.render(painter)
    finally:
        painter.end()
    target.setDevicePixelRatio(DEVICE_RATIO)
    return target


def icon(name: str, color: str | None = None, size: int = 16) -> QIcon:
    """A QIcon of one bundled icon, tinted with ``color`` (default: body text)."""
    return QIcon(pixmap(name, color or theme.tokens().text, size))


def tone_icon(name: str, tone: str, size: int = 16) -> QIcon:
    """A QIcon tinted with a semantic tone (``success``, ``danger``, ``T1``, ...)."""
    foreground, _ = theme.tokens().tone(tone)
    return icon(name, foreground, size)


ACTION_ICONS: dict[str, str] = {
    "DELETE_QUARANTINE": "trash-2",
    "MOVE": "arrow-right-left",
    "COMPRESS_NTFS": "minimize-2",
    "COMPRESS": "minimize-2",
    "NATIVE": "terminal",
    "LINK": "link-2",
    "REVIEW": "help-circle",
    "KEEP": "shield-check",
    "NO_ACTION": "shield-check",
}
"""The icon of each action, in both vocabularies the UI carries.

The engine's names (``COMPRESS_NTFS``, ``KEEP``) arrive on rule verdicts, the
AI-side ones (``COMPRESS``, ``LINK``, ``NO_ACTION`` -- see
:data:`spacesage.ai.prompts.AI_ACTIONS`) on AI advice, and both are painted by
the same list: an AI answer must not fall back to a generic glyph beside a rule
verdict for the same kind of work.
"""

ACTION_TONES: dict[str, str] = {
    "DELETE_QUARANTINE": "danger",
    "MOVE": "accent",
    "COMPRESS_NTFS": "info",
    "COMPRESS": "info",
    "NATIVE": "info",
    "LINK": "info",
    "REVIEW": "warning",
    "KEEP": "success",
    "NO_ACTION": "muted",
}
"""The semantic tone of each action, in both vocabularies."""


def action_icon_name(action: str) -> str:
    """The icon name for one action, in either vocabulary (``info`` when unknown)."""
    return ACTION_ICONS.get(action, "info")


def action_tone(action: str) -> str:
    """The tone for one action, in either vocabulary (``muted`` when unknown)."""
    return ACTION_TONES.get(action, "muted")


def action_icon(action: str, size: int = 16) -> QIcon:
    """The icon that goes with a solution (the design's suggested-solution set)."""
    return tone_icon(action_icon_name(action), action_tone(action), size)
