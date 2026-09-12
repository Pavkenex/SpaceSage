"""Small shared widgets: metric cards, badges, chips, drop zone, toasts.

Everything here draws from ``spacesage.app.theme`` tokens -- no component picks
its own colour -- and every icon-only control carries a tooltip, so the design's
accessibility rules hold without each screen re-implementing them.
"""

from __future__ import annotations

from collections.abc import Callable

from PySide6.QtCore import Qt, QTimer, Signal
from PySide6.QtGui import QDragEnterEvent, QDragLeaveEvent, QDropEvent
from PySide6.QtWidgets import (
    QFrame,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QSizePolicy,
    QVBoxLayout,
    QWidget,
)

from spacesage.app import icons, theme

# --------------------------------------------------------------------------- #
# Tones
# --------------------------------------------------------------------------- #


def state_tone(state: str) -> str:
    """Badge tone of a row state (has action / no action / undecided)."""
    from spacesage import opportunities

    return {
        opportunities.STATE_ACTION: "accent",
        opportunities.STATE_NO_ACTION: "muted",
        opportunities.STATE_UNDECIDED: "warning",
    }.get(state, "muted")


def tier_tone(tier: str) -> str:
    """Badge tone of a risk tier (T1 safe, T2 investigate, T3 report-only)."""
    return tier if tier in {"T1", "T2", "T3"} else "muted"


def confidence_tone(value: float) -> str:
    """Chip tone of a confidence value: high, middling, low."""
    if value >= 0.8:
        return "success"
    if value >= 0.5:
        return "warning"
    return "muted"


# --------------------------------------------------------------------------- #
# Labels and badges
# --------------------------------------------------------------------------- #


class Badge(QLabel):
    """A small pill of text in one semantic tone (design §9.1)."""

    def __init__(self, text: str = "", tone: str = "muted", parent: QWidget | None = None) -> None:
        super().__init__(text, parent)
        self.setObjectName("Badge")
        self.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.setSizePolicy(QSizePolicy.Policy.Maximum, QSizePolicy.Policy.Fixed)
        self.set_tone(tone)

    def set_tone(self, tone: str) -> None:
        """Recolour the badge (safe to call on every theme change)."""
        foreground, background = theme.tokens().tone(tone)
        self.setStyleSheet(
            "QLabel#Badge {"
            f" background: {background}; color: {foreground};"
            f" border-radius: {theme.RADIUS['sm']}px;"
            f" padding: 1px {theme.SPACE['xs'] + 2}px;"
            f" font-size: {theme.TYPE_SCALE['xs']}px; font-weight: 700; }}"
        )


class Chip(QLabel):
    """A confidence chip: the value in a mono face, toned by how sure it is."""

    def __init__(self, value: float = 0.0, parent: QWidget | None = None) -> None:
        super().__init__("", parent)
        self.setObjectName("Chip")
        self.set_value(value)

    def set_value(self, value: float) -> None:
        """Show ``value`` (0-1) as a percentage chip."""
        self.setText(f"{round(value * 100)}%")
        foreground, background = theme.tokens().tone(confidence_tone(value))
        self.setStyleSheet(
            "QLabel#Chip {"
            f" background: {background}; color: {foreground};"
            f" border-radius: {theme.RADIUS['sm']}px;"
            f" padding: 1px {theme.SPACE['xs'] + 2}px;"
            f' font-family: "{theme.mono_family()}";'
            f" font-size: {theme.TYPE_SCALE['xs']}px; font-weight: 700; }}"
        )


def section_label(text: str, parent: QWidget | None = None) -> QLabel:
    """A muted, upper-case section heading."""
    label = QLabel(text.upper(), parent)
    label.setObjectName("SectionTitle")
    return label


def muted_label(text: str, parent: QWidget | None = None) -> QLabel:
    """Secondary text (muted, never a chrome colour of its own)."""
    label = QLabel(text, parent)
    label.setObjectName("Muted")
    return label


def mono_label(text: str, parent: QWidget | None = None) -> QLabel:
    """A path/size/command label in the mono stack."""
    label = QLabel(text, parent)
    label.setObjectName("Mono")
    label.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
    label.setWordWrap(True)
    return label


# --------------------------------------------------------------------------- #
# Cards
# --------------------------------------------------------------------------- #


class MetricCard(QFrame):
    """One number with a caption (the summary strip is a row of these)."""

    def __init__(
        self,
        title: str,
        value: str = "--",
        caption: str = "",
        *,
        icon_name: str | None = None,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self.setObjectName("Card")
        layout = QVBoxLayout(self)
        layout.setContentsMargins(
            theme.SPACE["md"], theme.SPACE["sm"], theme.SPACE["md"], theme.SPACE["sm"]
        )
        layout.setSpacing(theme.SPACE["xs"])

        head = QHBoxLayout()
        head.setSpacing(theme.SPACE["sm"])
        self._icon = QLabel(self)
        if icon_name is None:
            self._icon.hide()
        else:
            self._icon.setPixmap(icons.icon(icon_name, theme.tokens().muted, 14).pixmap(14, 14))
        head.addWidget(self._icon)
        self._title = section_label(title, self)
        head.addWidget(self._title)
        head.addStretch(1)
        layout.addLayout(head)

        self._value = QLabel(value, self)
        self._value.setStyleSheet(
            f"font-size: {theme.TYPE_SCALE['lg']}px; font-weight: 700;"
            f" color: {theme.tokens().text};"
        )
        layout.addWidget(self._value)

        self._caption = QLabel(caption, self)
        self._caption.setObjectName("Faint")
        self._caption.setWordWrap(True)
        layout.addWidget(self._caption)
        layout.addStretch(1)

    def set_value(self, value: str, caption: str | None = None) -> None:
        """Update the number (and optionally the caption under it)."""
        self._value.setText(value)
        if caption is not None:
            self._caption.setText(caption)

    def caption(self) -> str:
        """The caption currently shown."""
        return self._caption.text()

    def value(self) -> str:
        """The value currently shown."""
        return self._value.text()


class EmptyState(QWidget):
    """One-line explanation plus the primary action (design §9.1)."""

    def __init__(
        self,
        title: str,
        explanation: str,
        *,
        icon_name: str = "info",
        action: str | None = None,
        on_action: Callable[[], None] | None = None,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(
            theme.SPACE["xxl"], theme.SPACE["xxl"], theme.SPACE["xxl"], theme.SPACE["xxl"]
        )
        layout.setSpacing(theme.SPACE["sm"])
        layout.setAlignment(Qt.AlignmentFlag.AlignCenter)

        picture = QLabel(self)
        picture.setPixmap(icons.icon(icon_name, theme.tokens().faint, 32).pixmap(32, 32))
        picture.setAlignment(Qt.AlignmentFlag.AlignCenter)
        layout.addWidget(picture)

        heading = QLabel(title, self)
        heading.setAlignment(Qt.AlignmentFlag.AlignCenter)
        heading.setStyleSheet(
            f"font-size: {theme.TYPE_SCALE['lg']}px; font-weight: 700;"
            f" color: {theme.tokens().text};"
        )
        layout.addWidget(heading)

        body = QLabel(explanation, self)
        body.setObjectName("Muted")
        body.setAlignment(Qt.AlignmentFlag.AlignCenter)
        body.setWordWrap(True)
        layout.addWidget(body)

        if action is not None and on_action is not None:
            button = QPushButton(action, self)
            button.setObjectName("Primary")
            button.clicked.connect(lambda: on_action())
            row = QHBoxLayout()
            row.addStretch(1)
            row.addWidget(button)
            row.addStretch(1)
            layout.addLayout(row)


class DropZone(QFrame):
    """A dashed area that accepts a file drop (the WizTree export)."""

    fileDropped = Signal(str)

    def __init__(self, hint: str, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setObjectName("DropZone")
        self.setAcceptDrops(True)
        self.setMinimumHeight(120)
        self.setProperty("hot", False)
        self.setProperty("filled", False)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(
            theme.SPACE["lg"], theme.SPACE["lg"], theme.SPACE["lg"], theme.SPACE["lg"]
        )
        layout.setSpacing(theme.SPACE["sm"])
        layout.setAlignment(Qt.AlignmentFlag.AlignCenter)

        self._icon = QLabel(self)
        self._icon.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._icon.setPixmap(icons.icon("folder-open", theme.tokens().muted, 28).pixmap(28, 28))
        layout.addWidget(self._icon)

        self._title = QLabel(hint, self)
        self._title.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._title.setWordWrap(True)
        self._title.setStyleSheet("font-weight: 600;")
        layout.addWidget(self._title)

        self._detail = QLabel("", self)
        self._detail.setObjectName("Faint")
        self._detail.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._detail.setWordWrap(True)
        layout.addWidget(self._detail)

    # -- state ------------------------------------------------------------ #

    def set_detail(self, text: str, *, filled: bool = False) -> None:
        """Show the chosen export (or the empty hint) under the title."""
        self._detail.setText(text)
        self.setProperty("filled", filled)
        self._restyle()

    def set_hot(self, hot: bool) -> None:
        """Highlight while a drag hovers the zone."""
        self.setProperty("hot", hot)
        self._restyle()

    def _restyle(self) -> None:
        self.style().unpolish(self)
        self.style().polish(self)

    @staticmethod
    def csv_path(event: QDropEvent | QDragEnterEvent) -> str | None:
        """The first local ``.csv`` path a drag event carries."""
        data = event.mimeData()
        if not data.hasUrls():
            return None
        for url in data.urls():
            if url.isLocalFile():
                path = url.toLocalFile()
                if path.lower().endswith(".csv"):
                    return path
        return None

    # -- Qt events -------------------------------------------------------- #

    def dragEnterEvent(self, event: QDragEnterEvent) -> None:
        if self.csv_path(event) is not None:
            self.set_hot(True)
            event.acceptProposedAction()

    def dragLeaveEvent(self, event: QDragLeaveEvent) -> None:
        self.set_hot(False)
        super().dragLeaveEvent(event)

    def dropEvent(self, event: QDropEvent) -> None:
        path = self.csv_path(event)
        self.set_hot(False)
        if path is not None:
            event.acceptProposedAction()
            self.fileDropped.emit(path)


class Toast(QFrame):
    """A transient message for a background result (design §9.1)."""

    def __init__(self, text: str, tone: str = "info", parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setObjectName("Card")
        foreground, background = theme.tokens().tone(tone)
        self.setStyleSheet(
            "QFrame#Card {"
            f" background: {background}; border: 1px solid {foreground};"
            f" border-radius: {theme.RADIUS['md']}px; }}"
        )
        layout = QHBoxLayout(self)
        layout.setContentsMargins(
            theme.SPACE["md"], theme.SPACE["sm"], theme.SPACE["md"], theme.SPACE["sm"]
        )
        layout.setSpacing(theme.SPACE["sm"])
        symbol = QLabel(self)
        symbol.setPixmap(icons.tone_icon("info", tone, 14).pixmap(14, 14))
        layout.addWidget(symbol)
        self._label = QLabel(text, self)
        self._label.setWordWrap(True)
        layout.addWidget(self._label)

    def text(self) -> str:
        """The message shown."""
        return self._label.text()

    @classmethod
    def pop_up(
        cls,
        parent: QWidget,
        text: str,
        *,
        tone: str = "info",
        timeout_ms: int = 4000,
    ) -> Toast:
        """Show ``text`` over ``parent`` and take it away again."""
        toast = cls(text, tone, parent)
        toast.adjustSize()
        toast.move(
            max(theme.SPACE["lg"], (parent.width() - toast.width()) // 2),
            max(theme.SPACE["lg"], parent.height() - toast.height() - theme.SPACE["xxl"]),
        )
        toast.show()
        toast.raise_()
        theme.fade_in(toast, duration=theme.MOTION_FAST)
        QTimer.singleShot(timeout_ms, toast.close)
        return toast
