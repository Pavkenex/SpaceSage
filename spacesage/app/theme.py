"""The one token set every screen draws from (design §9.1).

Light and dark are the *same* token names with different values, components
never hardcode a colour, and the app follows the OS by default with an explicit
toggle in Settings.  The stylesheet is generated from the tokens with
:class:`string.Template`, so a token change reaches every widget that Qt styles
itself; the hand-painted pieces (badges, checkboxes, the solution column) read
:class:`Tokens` directly through :func:`tokens`.

Scale (design §9.1): a 4px spacing grid, 6-8px radii, a 11/12/13/15/18/24
typography scale and 150-200ms motion.
"""

from __future__ import annotations

from dataclasses import dataclass
from string import Template

from PySide6.QtCore import (
    QEasingCurve,
    QObject,
    QPropertyAnimation,
    Qt,
    QTimer,
    Signal,
)
from PySide6.QtGui import QFont, QFontDatabase, QGuiApplication
from PySide6.QtWidgets import (
    QApplication,
    QGraphicsOpacityEffect,
    QWidget,
)

# --------------------------------------------------------------------------- #
# Scales
# --------------------------------------------------------------------------- #

SPACE: dict[str, int] = {"xs": 4, "sm": 8, "md": 12, "lg": 16, "xl": 24, "xxl": 32}
"""The 4px spacing grid (design §9.1)."""

RADIUS: dict[str, int] = {"sm": 6, "md": 8}
"""Corner radii: controls get 6px, cards and panels 8px."""

TYPE_SCALE: dict[str, int] = {"xs": 11, "sm": 12, "base": 13, "lg": 15, "xl": 18, "xxl": 24}
"""Typography scale in pixels -- the unit the stylesheet and the fonts share."""

MOTION_FAST = 150
"""Milliseconds for a hover/state fade."""

MOTION_NORMAL = 200
"""Milliseconds for a panel or page fade (the upper end of the design's range)."""

UI_FAMILIES: tuple[str, ...] = (
    "Segoe UI",
    "Inter",
    "SF Pro Text",
    "Ubuntu",
    "Cantarell",
    "DejaVu Sans",
    "Liberation Sans",
    "Noto Sans",
    "sans-serif",
)
"""System font stack; the first family Qt actually has is used."""

MONO_FAMILIES: tuple[str, ...] = (
    "Cascadia Mono",
    "Consolas",
    "SFMono-Regular",
    "Menlo",
    "DejaVu Sans Mono",
    "Liberation Mono",
    "Noto Sans Mono",
    "Courier New",
    "monospace",
)
"""Mono stack for paths, sizes and commands (design §9.1)."""

MODE_SYSTEM = "system"
MODE_LIGHT = "light"
MODE_DARK = "dark"
MODES: tuple[str, ...] = (MODE_SYSTEM, MODE_LIGHT, MODE_DARK)
MODE_LABELS: dict[str, str] = {
    MODE_SYSTEM: "Follow the system",
    MODE_LIGHT: "Light",
    MODE_DARK: "Dark",
}


@dataclass(frozen=True)
class Tokens:
    """Every colour the app is allowed to use, for one theme."""

    name: str
    base: str
    """The window background (surface level 0)."""

    raised: str
    """Cards, tables, inputs (surface level 1)."""

    overlay: str
    """Menus, dialogs, tooltips (surface level 2)."""

    sunken: str
    """Recessed areas: drop zones, table headers, scrollbar tracks."""

    border: str
    border_strong: str
    text: str
    muted: str
    faint: str
    inverse: str
    accent: str
    accent_hover: str
    accent_soft: str
    accent_text: str
    hover: str
    selection: str
    focus: str
    success: str
    success_soft: str
    warning: str
    warning_soft: str
    danger: str
    danger_soft: str
    info: str
    info_soft: str
    shadow: str

    def tone(self, name: str) -> tuple[str, str]:
        """``(foreground, background)`` of a badge tone.

        Tones are the design's semantic set plus the tier colours; an unknown
        tone falls back to the muted pair rather than inventing a colour.
        """
        pairs = {
            "accent": (self.accent, self.accent_soft),
            "success": (self.success, self.success_soft),
            "warning": (self.warning, self.warning_soft),
            "danger": (self.danger, self.danger_soft),
            "info": (self.info, self.info_soft),
            "muted": (self.muted, self.sunken),
            "T1": (self.success, self.success_soft),
            "T2": (self.warning, self.warning_soft),
            "T3": (self.muted, self.sunken),
        }
        return pairs.get(name, pairs["muted"])


LIGHT = Tokens(
    name=MODE_LIGHT,
    base="#F6F7F9",
    raised="#FFFFFF",
    overlay="#FFFFFF",
    sunken="#EEF0F3",
    border="#E2E5E9",
    border_strong="#C9CFD6",
    text="#1B1F24",
    muted="#5A6472",
    faint="#8A94A2",
    inverse="#FFFFFF",
    accent="#2F6FEB",
    accent_hover="#2A62D1",
    accent_soft="#E7EEFD",
    accent_text="#FFFFFF",
    hover="#F0F2F5",
    selection="#E4ECFD",
    focus="#2F6FEB",
    success="#197A3D",
    success_soft="#E4F4E9",
    warning="#8A5A00",
    warning_soft="#FBF0DC",
    danger="#B42318",
    danger_soft="#FBE7E4",
    info="#0B5CAD",
    info_soft="#E4EFFB",
    shadow="rgba(15, 23, 42, 0.12)",
)

DARK = Tokens(
    name=MODE_DARK,
    base="#15171B",
    raised="#1C1F24",
    overlay="#23272E",
    sunken="#101215",
    border="#2B3038",
    border_strong="#3A414B",
    text="#E8EAEE",
    muted="#A2AAB6",
    faint="#79818E",
    inverse="#0F1115",
    accent="#5B8DEF",
    accent_hover="#6F9CF3",
    accent_soft="#1E2A44",
    accent_text="#0F1115",
    hover="#23272E",
    selection="#22304C",
    focus="#5B8DEF",
    success="#4CC38A",
    success_soft="#16281F",
    warning="#E0B341",
    warning_soft="#2A2317",
    danger="#F07167",
    danger_soft="#331C1B",
    info="#6EA8FE",
    info_soft="#17233A",
    shadow="rgba(0, 0, 0, 0.45)",
)

THEMES: dict[str, Tokens] = {MODE_LIGHT: LIGHT, MODE_DARK: DARK}

#: The theme every component starts from; :class:`ThemeManager` swaps the module
#: state when the user (or the OS) changes it.  Hand-painted widgets read this
#: through :func:`tokens`, so nothing ever holds a stale palette.
_active: Tokens = LIGHT


def tokens() -> Tokens:
    """The active token set (hand-painted widgets call this while rendering)."""
    return _active


def scheme_name() -> str:
    """``"light"`` or ``"dark"`` -- the active theme's name."""
    return _active.name


def _set_active(theme: Tokens) -> None:
    global _active
    _active = theme


# --------------------------------------------------------------------------- #
# Fonts
# --------------------------------------------------------------------------- #

_family_cache: dict[str, str] = {}


def resolve_family(stack: tuple[str, ...]) -> str:
    """The first family in ``stack`` this system actually has."""
    key = stack[0]
    cached = _family_cache.get(key)
    if cached is not None:
        return cached
    available = set(QFontDatabase.families())
    chosen = next((family for family in stack if family in available), stack[-1])
    _family_cache[key] = chosen
    return chosen


def ui_family() -> str:
    """The resolved UI font family."""
    return resolve_family(UI_FAMILIES)


def mono_family() -> str:
    """The resolved mono font family (paths, sizes, commands)."""
    return resolve_family(MONO_FAMILIES)


def ui_font(size: str = "base", *, weight: QFont.Weight = QFont.Weight.Normal) -> QFont:
    """A UI font at one step of the typography scale.

    The scale is in pixels, exactly like the stylesheet's ``font-size``; a point
    size would render ~33% larger than the token on a 96 dpi display and make
    dense screens unreadable.
    """
    font = QFont(ui_family())
    font.setPixelSize(TYPE_SCALE[size])
    font.setWeight(weight)
    return font


def mono_font(size: str = "sm") -> QFont:
    """A mono font at one step of the typography scale (paths, sizes, commands)."""
    font = QFont(mono_family())
    font.setPixelSize(TYPE_SCALE[size])
    return font


# --------------------------------------------------------------------------- #
# The stylesheet
# --------------------------------------------------------------------------- #

_STYLESHEET = Template(
    """
QWidget {
    background: $base;
    color: $text;
    font-family: "$ui_family";
    font-size: ${type_base}px;
}
QMainWindow, QDialog { background: $base; }
QToolTip {
    background: $overlay;
    color: $text;
    border: 1px solid $border_strong;
    border-radius: ${radius_sm}px;
    padding: 6px 8px;
}

/* ---- navigation rail ------------------------------------------------ */
#NavRail {
    background: $raised;
    border-right: 1px solid $border;
}
#NavRail QLabel#RailTitle {
    color: $muted;
    font-size: ${type_xs}px;
    font-weight: 700;
    padding: 0 ${space_sm}px;
}
#NavButton {
    background: transparent;
    border: none;
    border-radius: ${radius_sm}px;
    color: $muted;
    font-size: ${type_base}px;
    font-weight: 600;
    padding: ${space_sm}px ${space_md}px;
    text-align: left;
}
#NavButton:hover { background: $hover; color: $text; }
#NavButton:checked { background: $accent_soft; color: $accent; }
#NavButton:focus { border: 1px solid $focus; }

/* ---- surfaces -------------------------------------------------------- */
#Card {
    background: $raised;
    border: 1px solid $border;
    border-radius: ${radius_md}px;
}
#PageHeader { background: $base; }
QLabel#PageTitle {
    font-size: ${type_xl}px;
    font-weight: 700;
    color: $text;
}
QLabel#PageSubtitle { color: $muted; font-size: ${type_sm}px; }
QLabel#SectionTitle {
    font-size: ${type_sm}px;
    font-weight: 700;
    color: $muted;
}
QLabel#Muted { color: $muted; }
QLabel#Faint { color: $faint; font-size: ${type_sm}px; }
QLabel#Mono { font-family: "$mono_family"; font-size: ${type_sm}px; }

/* ---- buttons --------------------------------------------------------- */
QPushButton {
    background: $raised;
    border: 1px solid $border_strong;
    border-radius: ${radius_sm}px;
    padding: 6px ${space_md}px;
    color: $text;
    font-weight: 600;
}
QPushButton:hover { background: $hover; }
QPushButton:disabled { color: $faint; border-color: $border; }
QPushButton#Primary {
    background: $accent;
    border: 1px solid $accent;
    color: $accent_text;
}
QPushButton#Primary:hover { background: $accent_hover; border-color: $accent_hover; }
QPushButton#Primary:disabled { background: $sunken; border-color: $border; color: $faint; }
QPushButton#Quiet {
    background: transparent;
    border: none;
    color: $accent;
    padding: 4px ${space_sm}px;
}
QPushButton#Quiet:hover { background: $hover; }
QPushButton#Danger {
    background: $danger;
    border: 1px solid $danger;
    color: $overlay;
}
QPushButton#Danger:hover { background: $danger; border-color: $text; }
QPushButton#Danger:disabled { background: $sunken; border-color: $border; color: $faint; }

/* ---- segmented switch (one page, several views) ----------------------- */
#Segmented {
    background: $sunken;
    border: 1px solid $border;
    border-radius: ${radius_sm}px;
}
QPushButton#Segment {
    background: transparent;
    border: 1px solid transparent;
    border-radius: ${radius_sm}px;
    color: $muted;
    padding: 4px ${space_md}px;
}
QPushButton#Segment:hover { color: $text; background: $hover; }
QPushButton#Segment:checked { background: $raised; border-color: $border_strong; color: $text; }
QPushButton#Segment:focus { border-color: $focus; }

/* ---- inputs ---------------------------------------------------------- */
QLineEdit, QComboBox, QSpinBox, QDoubleSpinBox {
    background: $raised;
    border: 1px solid $border_strong;
    border-radius: ${radius_sm}px;
    padding: 5px ${space_sm}px;
    color: $text;
    selection-background-color: $selection;
    selection-color: $text;
}
QLineEdit:focus, QComboBox:focus, QSpinBox:focus, QDoubleSpinBox:focus {
    border: 1px solid $focus;
}
QLineEdit:disabled, QComboBox:disabled { color: $faint; background: $sunken; }
QComboBox::drop-down { border: none; width: 18px; }
QComboBox QAbstractItemView {
    background: $overlay;
    border: 1px solid $border_strong;
    selection-background-color: $selection;
    selection-color: $text;
    outline: none;
}

/* ---- tables ---------------------------------------------------------- */
QTableView {
    background: $raised;
    alternate-background-color: $raised;
    border: 1px solid $border;
    border-radius: ${radius_md}px;
    gridline-color: transparent;
    selection-background-color: $selection;
    selection-color: $text;
    outline: none;
}
QTableView::item { padding: 0 ${space_sm}px; border: none; }
QTableView::item:hover { background: $hover; }
QHeaderView { background: $raised; }
QHeaderView::section {
    background: $raised;
    color: $muted;
    border: none;
    border-bottom: 1px solid $border;
    font-size: ${type_sm}px;
    font-weight: 700;
    padding: 6px ${space_sm}px;
}
QHeaderView::section:hover { color: $text; }
QTableCornerButton::section { background: $raised; border: none; }

/* ---- misc ------------------------------------------------------------ */
QScrollBar:vertical { background: transparent; width: 10px; margin: 2px; }
QScrollBar:horizontal { background: transparent; height: 10px; margin: 2px; }
QScrollBar::handle:vertical, QScrollBar::handle:horizontal {
    background: $border_strong;
    border-radius: 5px;
    min-height: 24px;
    min-width: 24px;
}
QScrollBar::handle:hover { background: $muted; }
QScrollBar::add-line, QScrollBar::sub-line { height: 0; width: 0; }
QScrollBar::add-page, QScrollBar::sub-page { background: transparent; }
QSplitter::handle { background: $border; }
QSplitter::handle:horizontal { width: 1px; }
QSplitter::handle:vertical { height: 1px; }
QStatusBar {
    background: $raised;
    border-top: 1px solid $border;
    color: $muted;
}
QStatusBar::item { border: none; }
QProgressBar {
    background: $sunken;
    border: none;
    border-radius: 3px;
    height: 6px;
    text-align: center;
}
QProgressBar::chunk { background: $accent; border-radius: 3px; }
QMenu {
    background: $overlay;
    border: 1px solid $border_strong;
    border-radius: ${radius_sm}px;
    padding: ${space_xs}px;
}
QMenu::item { padding: 6px ${space_md}px; border-radius: ${radius_sm}px; }
QMenu::item:selected { background: $selection; }
QCheckBox, QRadioButton { spacing: ${space_sm}px; }
QGroupBox {
    border: 1px solid $border;
    border-radius: ${radius_md}px;
    margin-top: ${space_md}px;
    padding: ${space_md}px;
    font-weight: 600;
}
QGroupBox::title { subcontrol-origin: margin; left: ${space_md}px; color: $muted; }
QFrame#DropZone {
    background: $sunken;
    border: 1px dashed $border_strong;
    border-radius: ${radius_md}px;
}
QFrame#DropZone[hot="true"] { border: 1px dashed $accent; background: $accent_soft; }
QFrame#DropZone[filled="true"] { border: 1px solid $border_strong; background: $raised; }
"""
)


def stylesheet(theme: Tokens | None = None) -> str:
    """The application stylesheet for one theme."""
    active = theme if theme is not None else tokens()
    values: dict[str, object] = {
        "ui_family": ui_family(),
        "mono_family": mono_family(),
        **{f"space_{name}": value for name, value in SPACE.items()},
        **{f"radius_{name}": value for name, value in RADIUS.items()},
        **{f"type_{name}": value for name, value in TYPE_SCALE.items()},
        **{
            field: getattr(active, field)
            for field in (
                "base",
                "raised",
                "overlay",
                "sunken",
                "border",
                "border_strong",
                "text",
                "muted",
                "faint",
                "inverse",
                "accent",
                "accent_hover",
                "accent_soft",
                "accent_text",
                "hover",
                "selection",
                "focus",
                "success",
                "success_soft",
                "warning",
                "warning_soft",
                "danger",
                "danger_soft",
                "info",
                "info_soft",
            )
        },
    }
    return _STYLESHEET.substitute(values)


# --------------------------------------------------------------------------- #
# Theme manager
# --------------------------------------------------------------------------- #


def system_scheme() -> str:
    """The OS's colour scheme (``"light"`` when the platform does not say)."""
    hints = QGuiApplication.styleHints()
    scheme = hints.colorScheme()
    return MODE_DARK if scheme == Qt.ColorScheme.Dark else MODE_LIGHT


class ThemeManager(QObject):
    """Applies the active theme to a QApplication and follows the OS by default."""

    changed = Signal(str)

    def __init__(self, app: QApplication, mode: str = MODE_SYSTEM) -> None:
        super().__init__()
        self._app = app
        self._mode = mode if mode in MODES else MODE_SYSTEM
        self._app.setStyleSheet(stylesheet(self.active_tokens()))
        _set_active(self.active_tokens())
        hints = QGuiApplication.styleHints()
        hints.colorSchemeChanged.connect(self._on_system_change)

    # -- state ------------------------------------------------------------ #

    @property
    def mode(self) -> str:
        """``system``, ``light`` or ``dark`` -- what the toggle says."""
        return self._mode

    @property
    def scheme(self) -> str:
        """The theme actually in force (resolves ``system``)."""
        return system_scheme() if self._mode == MODE_SYSTEM else self._mode

    def active_tokens(self) -> Tokens:
        """The token set of the theme actually in force."""
        return THEMES[self.scheme]

    def set_mode(self, mode: str) -> None:
        """Switch theme mode and restyle everything."""
        if mode not in MODES or mode == self._mode:
            return
        self._mode = mode
        self.refresh()

    def refresh(self) -> None:
        """Re-apply the stylesheet (mode change, OS change, DPI change)."""
        active = self.active_tokens()
        _set_active(active)
        self._app.setStyleSheet(stylesheet(active))
        self.changed.emit(active.name)

    def _on_system_change(self, _scheme: Qt.ColorScheme) -> None:
        if self._mode == MODE_SYSTEM:
            self.refresh()


# --------------------------------------------------------------------------- #
# Motion
# --------------------------------------------------------------------------- #


def fade_in(widget: QWidget, *, duration: int = MOTION_NORMAL) -> QPropertyAnimation | None:
    """Fade a widget in over ``duration`` ms (subtle motion only, design §9.1).

    The opacity effect is removed once the animation ends, so it never affects
    the widget again; the animation is parented to the widget to keep it alive.
    A safety timer removes the effect even if the event loop never advances the
    animation (a frozen UI, a headless screenshot run), so a widget can never be
    left invisible by its own entrance animation.
    """
    if duration <= 0 or not widget.isVisible():
        return None
    effect = QGraphicsOpacityEffect(widget)
    widget.setGraphicsEffect(effect)
    animation = QPropertyAnimation(effect, b"opacity", widget)
    animation.setDuration(duration)
    animation.setStartValue(0.0)
    animation.setEndValue(1.0)
    animation.setEasingCurve(QEasingCurve.Type.InOutQuad)

    def finish() -> None:
        # PySide allows None here (that is how an effect is removed) while the
        # stubs insist on a QGraphicsEffect, hence the explicit ignore.
        widget.setGraphicsEffect(None)  # type: ignore[arg-type]

    animation.finished.connect(finish)
    # The widget is the timer's context object, so Qt drops the safety call when
    # the window goes away: a closed widget is never repainted by a stale timer.
    QTimer.singleShot(duration + 80, widget, finish)
    animation.start(QPropertyAnimation.DeletionPolicy.KeepWhenStopped)
    return animation
