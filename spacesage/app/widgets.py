"""Small shared widgets: metric cards, badges, chips, drop zone, toasts.

Everything here draws from ``spacesage.app.theme`` tokens -- no component picks
its own colour -- and every icon-only control carries a tooltip, so the design's
accessibility rules hold without each screen re-implementing them.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable, Sequence

from PySide6.QtCore import (
    QEvent,
    QModelIndex,
    QObject,
    QPoint,
    QRect,
    QSize,
    Qt,
    QTimer,
    Signal,
)
from PySide6.QtGui import (
    QDragEnterEvent,
    QDragLeaveEvent,
    QDropEvent,
    QFontMetrics,
    QKeyEvent,
    QResizeEvent,
    QTextCursor,
    QTextOption,
)
from PySide6.QtWidgets import (
    QButtonGroup,
    QFrame,
    QHBoxLayout,
    QLabel,
    QLayout,
    QLayoutItem,
    QPlainTextEdit,
    QPushButton,
    QSizePolicy,
    QStyle,
    QStyleOptionButton,
    QTableView,
    QVBoxLayout,
    QWidget,
)

from spacesage.app import icons, theme

# --------------------------------------------------------------------------- #
# A layout that wraps
# --------------------------------------------------------------------------- #


class FlowLayout(QLayout):
    """A layout that moves what does not fit onto the next line.

    Qt has no wrapping layout, and a row of badges, chips or buttons is exactly
    the case that needs one: a ``QHBoxLayout`` silently clips whatever does not
    fit -- buttons included -- when its parent shrinks.  Rows built from
    :class:`FlowLayout` instead reflow, so a resizable pane never hides a control.

    The standard Qt idiom: the layout keeps its own items, reports a minimum
    width of its widest item (never the sum) and implements ``heightForWidth`` so
    the row's height follows the wrapping.

    ``addWidget(widget, stretch)`` keeps the one thing a plain wrap would lose:
    an item added with a stretch absorbs the width its line has left over, so a
    row that wraps can still hold a field that grows -- the Opportunities filter
    bar's search stays as wide as the bar allows instead of packing left at its
    hint.
    """

    def __init__(
        self,
        *,
        h_spacing: int = theme.SPACE["xs"],
        v_spacing: int = theme.SPACE["xs"],
    ) -> None:
        super().__init__()
        self._items: list[QLayoutItem] = []
        self._stretch: list[int] = []
        self._h_spacing = h_spacing
        self._v_spacing = v_spacing
        self.setContentsMargins(0, 0, 0, 0)

    # -- QLayout plumbing -------------------------------------------------- #

    def addItem(self, item: QLayoutItem) -> None:
        self._items.append(item)
        self._stretch.append(0)

    def addWidget(self, widget: QWidget, stretch: int = 0) -> None:
        """Add a widget; a ``stretch`` above zero fills its line's leftover width."""
        super().addWidget(widget)
        self._stretch[-1] = stretch

    def count(self) -> int:
        return len(self._items)

    def itemAt(self, index: int) -> QLayoutItem | None:
        return self._items[index] if 0 <= index < len(self._items) else None

    def takeAt(self, index: int) -> QLayoutItem | None:
        if not 0 <= index < len(self._items):
            return None
        self._stretch.pop(index)
        return self._items.pop(index)

    def expandingDirections(self) -> Qt.Orientation:
        return Qt.Orientation(0)

    def hasHeightForWidth(self) -> bool:
        return True

    def heightForWidth(self, width: int) -> int:
        return self._arrange(QRect(0, 0, width, 0), apply=False)

    def setGeometry(self, rect: QRect) -> None:
        super().setGeometry(rect)
        self._arrange(rect, apply=True)

    def sizeHint(self) -> QSize:
        return self.minimumSize()

    def minimumSize(self) -> QSize:
        size = QSize()
        for item in self._items:
            size = size.expandedTo(item.minimumSize())
        margins = self.contentsMargins()
        return size + QSize(margins.left() + margins.right(), margins.top() + margins.bottom())

    # -- the wrap ---------------------------------------------------------- #

    def _arrange(self, rect: QRect, *, apply: bool) -> int:
        """Place every item left to right, dropping to a new line when it does not fit.

        A line gives the width it has left over to the items added with a
        stretch, so a row that wraps can still hold a field that grows.
        """
        margins = self.contentsMargins()
        area = rect.adjusted(margins.left(), margins.top(), -margins.right(), -margins.bottom())
        lines = self._lines(area)
        if not lines:
            return margins.top() + margins.bottom()
        y = area.y()
        for line in lines:
            widths = self._widths(line, area.width())
            height = 0
            x = area.x()
            for index, width in zip(line, widths, strict=True):
                item = self._items[index]
                hint = item.sizeHint()
                if apply:
                    item.setGeometry(QRect(QPoint(x, y), QSize(width, hint.height())))
                x += width + self._h_spacing
                height = max(height, hint.height())
            y += height + self._v_spacing
        return y - self._v_spacing - rect.y() + margins.bottom()

    def _lines(self, area: QRect) -> list[list[int]]:
        """The items of each line a greedy left-to-right wrap produces."""
        lines: list[list[int]] = []
        line: list[int] = []
        x = area.x()
        for index, item in enumerate(self._items):
            hint = item.sizeHint()
            next_x = x + hint.width() + self._h_spacing
            if line and next_x - self._h_spacing > area.right():
                lines.append(line)
                line = []
                next_x = area.x() + hint.width() + self._h_spacing
            line.append(index)
            x = next_x
        if line:
            lines.append(line)
        return lines

    def _widths(self, line: list[int], width: int) -> list[int]:
        """Each item's width: what it asks for, plus the line's leftover, shared."""
        hints = [self._items[index].sizeHint().width() for index in line]
        leftover = width - (sum(hints) + self._h_spacing * (len(line) - 1))
        stretches = [self._stretch[index] for index in line]
        shares = sum(stretches)
        if leftover <= 0 or shares <= 0:
            return hints
        widths = list(hints)
        growing = [index for index, stretch in enumerate(stretches) if stretch]
        remaining = leftover
        for index in growing[:-1]:
            share = leftover * stretches[index] // shares
            widths[index] += share
            remaining -= share
        widths[growing[-1]] += remaining
        return widths


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


def severity_tone(severity: str) -> str:
    """Banner tone of a plan warning (design §9.1: danger, warning, info)."""
    return {"blocker": "danger", "budget": "warning", "conflict": "warning"}.get(severity, "info")


def plan_status_tone(status: str) -> str:
    """Badge tone of a plan item's status (draft, preview, execution)."""
    return {
        "ready": "accent",
        "advice": "muted",
        "refused": "danger",
        "planned": "info",
        "done": "success",
        "skipped": "warning",
        "failed": "danger",
        "pending": "muted",
    }.get(status, "muted")


def undo_status_tone(status: str) -> str:
    """Badge tone of an undo item's status."""
    return {
        "pending": "warning",
        "reversed": "success",
        "skipped": "muted",
        "blocked": "danger",
        "failed": "danger",
        "interrupted": "danger",
    }.get(status, "muted")


def status_label(status: str) -> str:
    """Plain-language name of a status value (``done`` -> "Done")."""
    return status.replace("_", " ").capitalize()


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
# Text that is never cut without a sign
# --------------------------------------------------------------------------- #


def _fitted_text(metrics: QFontMetrics, text: str, mode: Qt.TextElideMode, width: int) -> str:
    """``text`` elided to ``width`` -- or whole, when it fits exactly.

    Qt's ``elidedText`` reserves room for the ellipsis it may append, so a line
    that fits its width exactly still comes back cut: paint it whole when it does
    fit, and let a row that leaves less than it needs show the ellipsis.
    """
    if width <= 0 or metrics.horizontalAdvance(text) <= width:
        return text
    return metrics.elidedText(text, mode, width)


def _help_tooltip(full: str, painted: str, tip: str) -> str:
    """The tooltip that keeps ``full`` reachable while ``painted`` is less than it."""
    if painted != full and full and full not in tip:
        return f"{full}\n\n{tip}" if tip else full
    return tip


class ElidedLabel(QLabel):
    """A single-line label that elides instead of clipping.

    A workspace path or a plan id is longer than any row it sits in, and a plain
    ``QLabel`` simply paints past its edge: the user reads half a path with no
    sign that anything is missing (design §9.1).  This one keeps the full text
    available (``full_text()``), elides to the width it was given -- middle for a
    path, where the tail is the part that identifies it, right for a sentence --
    and hands the full text over in its tooltip whenever it had to elide.

    ``claim_width`` says how the label asks for room, because a row has to decide
    who claims width and who yields:

    * ``False`` (the default) -- the label asks for nothing (``Ignored``) and
      takes what the row leaves it: for a path, which is long by nature and whose
      tail is what identifies it.
    * ``True`` -- the label asks for the width its full text needs, exactly as a
      plain label would, so a row short of room shrinks it *last*; and when the
      row cannot pay after all (Qt goes below a minimum when nothing else is
      left, measured at 980x620 on the Plan toolbar), the figure elides visibly
      instead of painting past its edge.
    """

    def __init__(
        self,
        text: str = "",
        parent: QWidget | None = None,
        *,
        mode: Qt.TextElideMode = Qt.TextElideMode.ElideMiddle,
        claim_width: bool = False,
    ) -> None:
        super().__init__("", parent)
        self._full = text
        self._mode = mode
        self._claim = claim_width
        self._tip = ""
        if claim_width:
            self.setSizePolicy(QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Preferred)
        else:
            self.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
            self.setMinimumWidth(80)
        self.setText(text)

    # -- what the label was given, and what it is showing ------------------ #

    def setText(self, text: str) -> None:
        """Set the full text; what is painted is elided to the current width."""
        self._full = text
        super().setText(self._fitted())
        self._sync_tooltip()

    def full_text(self) -> str:
        """The text this label was given, elided or not."""
        return self._full

    def is_elided(self) -> bool:
        """Whether the label is painting less than the text it was given."""
        return self.text() != self._full

    def setToolTip(self, tip: str) -> None:
        """Set the label's own tooltip; the full text leads it while elided."""
        self._tip = tip
        self._sync_tooltip()

    # -- QLabel plumbing --------------------------------------------------- #

    def sizeHint(self) -> QSize:
        """The width the full text needs, for a label that claims width."""
        hint = super().sizeHint()
        if self._claim:
            hint.setWidth(self._needed())
        return hint

    def minimumSizeHint(self) -> QSize:
        """How narrow a claiming label may get: the width of its full text."""
        return self.sizeHint() if self._claim else super().minimumSizeHint()

    def resizeEvent(self, event: QResizeEvent) -> None:
        super().resizeEvent(event)
        super().setText(self._fitted())
        self._sync_tooltip()

    # -- internals --------------------------------------------------------- #

    def _fitted(self) -> str:
        """The full text, elided to the painted width (never a cut-off glyph)."""
        return _fitted_text(self.fontMetrics(), self._full, self._mode, self.contentsRect().width())

    def _needed(self) -> int:
        """How much width the full text and this label's margins need."""
        margins = self.contentsMargins()
        return self.fontMetrics().horizontalAdvance(self._full) + margins.left() + margins.right()

    def _sync_tooltip(self) -> None:
        """Keep the full text one hover away whenever it is not all painted."""
        super().setToolTip(_help_tooltip(self._full, self.text(), self._tip))
        # A screen reader is told the text the label was given, not the cut one.
        super().setAccessibleName(self._full if self.is_elided() else "")


def _button_option(button: QPushButton) -> QStyleOptionButton:
    """A push button as the style sees it, for its own metrics."""
    option = QStyleOptionButton()
    option.initFrom(button)
    option.rect = button.rect()
    option.text = button.text()
    option.icon = button.icon()
    option.iconSize = button.iconSize()
    return option


def caption_room(button: QPushButton) -> int:
    """How much width a push button's style leaves for its caption.

    Public because the pinning test measures plain buttons with it as well: a
    caption that needs more than this room is painted with its ends missing.
    """
    option = _button_option(button)
    room = (
        button.style()
        .subElementRect(QStyle.SubElement.SE_PushButtonContents, option, button)
        .width()
    )
    if not option.icon.isNull():
        # The contents rect covers icon and text; the icon takes its size and the
        # style's fixed 4px gap on the left (QCommonStyle::CE_PushButtonLabel).
        room -= option.iconSize.width() + 4
    return room


class ElidedButton(QPushButton):
    """A push button whose caption elides instead of being cut (design §9.1).

    A plain ``QPushButton`` clips its label where the button ends: on the Undo
    bar measured at the shell's minimum size (980x620), ``Revert all pending``
    reaches the screen as ``vert all pendin`` -- the middle of a caption on the
    destructive action of the screen, with no ellipsis and no sign that a word is
    missing.  This one keeps the caption (``full_text()``), asks for the width it
    needs, and when the row cannot pay it paints an ellipsis and hands the whole
    caption over in its tooltip, above the tooltip the button already had.
    """

    def __init__(
        self,
        text: str = "",
        parent: QWidget | None = None,
        *,
        mode: Qt.TextElideMode = Qt.TextElideMode.ElideRight,
    ) -> None:
        super().__init__("", parent)
        self._full = text
        self._mode = mode
        self._tip = ""
        self.setText(text)

    # -- what the button was given, and what it is showing ------------------ #

    def setText(self, text: str) -> None:
        """Set the full caption; what is painted is elided to the room it has."""
        self._full = text
        super().setText(self._fitted())
        self._sync_tooltip()

    def full_text(self) -> str:
        """The caption this button was given, elided or not."""
        return self._full

    def is_elided(self) -> bool:
        """Whether the button is painting less than the caption it was given."""
        return self.text() != self._full

    def setToolTip(self, tip: str) -> None:
        """Set the button's own tooltip; the full caption leads it while elided."""
        self._tip = tip
        self._sync_tooltip()

    # -- QPushButton plumbing ---------------------------------------------- #

    def sizeHint(self) -> QSize:
        """The size the whole caption needs, so the row shrinks others first."""
        hint = super().sizeHint()
        hint.setWidth(hint.width() + self._caption_slack())
        return hint

    def minimumSizeHint(self) -> QSize:
        """What the button may be asked for: the same, and never less."""
        return self.sizeHint()

    def resizeEvent(self, event: QResizeEvent) -> None:
        super().resizeEvent(event)
        super().setText(self._fitted())
        self._sync_tooltip()

    # -- internals --------------------------------------------------------- #

    def _caption_slack(self) -> int:
        """How much wider the whole caption is than the part now painted."""
        metrics = self.fontMetrics()
        return metrics.horizontalAdvance(self._full) - metrics.horizontalAdvance(self.text())

    def _fitted(self) -> str:
        """The full caption, elided to the room the style leaves for the text."""
        return _fitted_text(self.fontMetrics(), self._full, self._mode, caption_room(self))

    def _sync_tooltip(self) -> None:
        """Keep the full caption one hover away whenever it is not all painted."""
        super().setToolTip(_help_tooltip(self._full, self.text(), self._tip))
        # A screen reader is told the caption the button was given, not the cut one.
        super().setAccessibleName(self._full if self.is_elided() else "")


class LogPanel(QPlainTextEdit):
    """A read-only monospace log: what is listed is *exactly* what will happen.

    The itemized actions of a plan are paths, and a path is wider than any
    dialog -- an item view elides it (or hides it behind a scrollbar) and the
    user never sees the destination they are approving.  This panel wraps
    instead of clipping, so a line is always complete on screen, and the text
    stays selectable so a path can be copied out (design §9.1).
    """

    def __init__(self, lines: Sequence[str] = (), parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setObjectName("LogPanel")
        self.setReadOnly(True)
        self.setFont(theme.mono_font("sm"))
        self.setLineWrapMode(QPlainTextEdit.LineWrapMode.WidgetWidth)
        self.setWordWrapMode(QTextOption.WrapMode.WrapAtWordBoundaryOrAnywhere)
        self.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.setFrameShape(QFrame.Shape.NoFrame)
        self.set_lines(lines)

    def set_lines(self, lines: Sequence[str]) -> None:
        """Replace the whole log (one entry per line)."""
        self.setPlainText("\n".join(lines))
        self.moveCursor(QTextCursor.MoveOperation.Start)

    def lines(self) -> list[str]:
        """Every line as it renders (the reverse of :meth:`set_lines`)."""
        return self.toPlainText().splitlines()


class _SpaceToggle(QObject):
    """Space over a table toggles the current row (design §9.1: keyboard)."""

    def __init__(self, table: QTableView, toggle: Callable[[QModelIndex], bool]) -> None:
        super().__init__(table)
        self._table = table
        self._toggle = toggle

    def eventFilter(self, watched: QObject, event: QEvent) -> bool:
        if (
            watched is self._table
            and event.type() == QEvent.Type.KeyPress
            and isinstance(event, QKeyEvent)
            and event.key() == Qt.Key.Key_Space
        ):
            return self._toggle(self._table.currentIndex())
        return False


def space_toggles(table: QTableView, toggle: Callable[[QModelIndex], bool]) -> None:
    """Give ``table`` a keyboard path to its checkboxes: Space toggles the row.

    A checkable cell that only answers the mouse leaves the whole flow
    mouse-only, and "full keyboard navigation" is part of the design bar.
    ``toggle`` returns ``True`` when it changed something (the key is then
    consumed instead of scrolling the view).
    """
    table.installEventFilter(_SpaceToggle(table, toggle))


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
        self._title = ElidedLabel(
            title.upper(), self, mode=Qt.TextElideMode.ElideRight, claim_width=True
        )
        self._title.setObjectName("SectionTitle")
        head.addWidget(self._title)
        head.addStretch(1)
        layout.addLayout(head)

        self._value = ElidedLabel(value, self, mode=Qt.TextElideMode.ElideRight, claim_width=True)
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
        """The value this card carries (what it paints, elided or not)."""
        return self._value.full_text()


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
        above: QWidget | None = None,
    ) -> Toast:
        """Show ``text`` over ``parent`` and take it away again.

        Centred horizontally and floats just above ``above`` when the caller
        names the row of controls the message belongs to: every screen that
        reports a background result ends in an action bar (Build plan, Execute,
        Revert), and a toast covering the button the user is about to press is
        worse than no toast at all.

        One toast at a time: a new result replaces the one on screen instead of
        stacking on top of it (two boxes in the same place hide each other's
        text).
        """
        for previous in parent.findChildren(cls):
            previous.close()
        toast = cls(text, tone, parent)
        toast.adjustSize()
        if above is not None:
            bottom = above.mapTo(parent, QPoint(0, 0)).y() - theme.SPACE["sm"]
        else:
            bottom = parent.height() - theme.SPACE["xxl"]
        toast.move(
            max(theme.SPACE["lg"], (parent.width() - toast.width()) // 2),
            max(theme.SPACE["lg"], bottom - toast.height()),
        )
        toast.show()
        toast.raise_()
        theme.fade_in(toast, duration=theme.MOTION_FAST)
        QTimer.singleShot(timeout_ms, toast.close)
        return toast


# --------------------------------------------------------------------------- #
# Plan & undo chrome (design §9, screen 3)
# --------------------------------------------------------------------------- #


class WarningBanner(QFrame):
    """One plan warning, styled by severity -- blockers never look like notes.

    The severity is spelled out in a badge ("Cannot run", "No room", "Conflict",
    "Note"), the message is the engine's own sentence, and the paths the warning
    names are listed in the mono stack, because a plan warning is only useful
    when it says *which* entry it is about.
    """

    MAX_PATHS = 4
    """Paths shown before the rest are summarised as "(+N more)"."""

    def __init__(self, warning: object, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setObjectName("Banner")
        severity = getattr(warning, "severity", "info")
        message = str(getattr(warning, "message", warning))
        paths = tuple(getattr(warning, "paths", ()) or ())
        self._severity = severity
        self._message = message

        layout = QHBoxLayout(self)
        layout.setContentsMargins(
            theme.SPACE["md"], theme.SPACE["sm"], theme.SPACE["md"], theme.SPACE["sm"]
        )
        layout.setSpacing(theme.SPACE["sm"])

        badge = Badge(
            str(getattr(warning, "label", severity_tone(severity))), severity_tone(severity)
        )
        badge.setToolTip(f"{severity.capitalize()} warning")
        layout.addWidget(badge, 0, Qt.AlignmentFlag.AlignTop)

        body = QVBoxLayout()
        body.setSpacing(theme.SPACE["xs"])
        text = QLabel(message, self)
        text.setWordWrap(True)
        body.addWidget(text)
        if paths:
            shown = ", ".join(paths[: self.MAX_PATHS])
            if len(paths) > self.MAX_PATHS:
                shown += f" (+{len(paths) - self.MAX_PATHS} more)"
            detail = QLabel(shown, self)
            detail.setObjectName("Mono")
            detail.setWordWrap(True)
            detail.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
            body.addWidget(detail)
        layout.addLayout(body, 1)
        self._restyle()

    def severity(self) -> str:
        """The warning's severity key."""
        return self._severity

    def text(self) -> str:
        """The message shown."""
        return self._message

    def set_warning(self, warning: object) -> None:
        """Re-tone the banner after a theme change (the message stays)."""
        self._restyle()

    def _restyle(self) -> None:
        foreground, background = theme.tokens().tone(severity_tone(self._severity))
        self.setStyleSheet(
            "QFrame#Banner {"
            f" background: {background}; border: 1px solid {foreground};"
            f" border-radius: {theme.RADIUS['md']}px; }}"
        )


class WarningList(QWidget):
    """A vertical stack of :class:`WarningBanner` (empty when there is nothing to say)."""

    def __init__(self, warnings: Iterable[object] = (), parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._layout = QVBoxLayout(self)
        self._layout.setContentsMargins(0, 0, 0, 0)
        self._layout.setSpacing(theme.SPACE["sm"])
        self.set_warnings(warnings)

    def set_warnings(self, warnings: Iterable[object] = ()) -> None:
        """Replace every banner (the plan changed or the theme did)."""
        while self._layout.count():
            item = self._layout.takeAt(0)
            widget = item.widget() if item is not None else None
            if widget is not None:
                widget.deleteLater()
        count = 0
        for warning in warnings:
            self._layout.addWidget(WarningBanner(warning, self))
            count += 1
        self.setVisible(count > 0)

    def count(self) -> int:
        """How many banners are shown."""
        return self._layout.count()


class Segmented(QWidget):
    """A segmented switch for views that share a page (``Plan | Undo``).

    Every segment is a checkable button in an exclusive group, so the switch is
    keyboard reachable and arrow-navigable like any other button row; the
    selected index is emitted on change.
    """

    changed = Signal(int)

    def __init__(
        self,
        labels: tuple[str, ...],
        *,
        icons_by_label: dict[str, str] | None = None,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self.setObjectName("Segmented")
        row = QHBoxLayout(self)
        row.setContentsMargins(0, 0, 0, 0)
        row.setSpacing(theme.SPACE["xs"])
        self._group = QButtonGroup(self)
        self._group.setExclusive(True)
        self._buttons: list[QPushButton] = []
        self._icons = dict(icons_by_label or {})
        for index, label in enumerate(labels):
            button = QPushButton(label, self)
            button.setObjectName("Segment")
            button.setCheckable(True)
            icon_name = self._icons.get(label)
            if icon_name:
                button.setIcon(icons.icon(icon_name, theme.tokens().muted, 14))
            button.setToolTip(f"Show {label.lower()}")
            button.clicked.connect(lambda _checked=False, value=index: self.select(value))
            self._group.addButton(button)
            self._buttons.append(button)
            row.addWidget(button)
        if self._buttons:
            self._buttons[0].setChecked(True)

    def select(self, index: int, *, emit: bool = True) -> bool:
        """Select one segment (``emit=False`` for programmatic switches)."""
        if not 0 <= index < len(self._buttons):
            return False
        button = self._buttons[index]
        was_checked = button.isChecked()
        blocked = button.blockSignals(True)
        button.setChecked(True)
        button.blockSignals(blocked)
        if emit and not was_checked:
            self.changed.emit(index)
        return True

    def current(self) -> int:
        """The selected segment's index."""
        for index, button in enumerate(self._buttons):
            if button.isChecked():
                return index
        return 0

    def apply_theme(self) -> None:
        """Re-tint the icons after a theme change."""
        for button in self._buttons:
            icon = button.icon()
            if icon.isNull():
                continue
            name = self._icons.get(button.text()) or button.text().lower()
            tone = "accent" if button.isChecked() else "muted"
            button.setIcon(icons.tone_icon(name, tone, 14))


class StatusBadge(QLabel):
    """A badge whose text and tone follow one status value (plan/undo rows)."""

    def __init__(self, status: str = "pending", parent: QWidget | None = None) -> None:
        super().__init__("", parent)
        self.setObjectName("Badge")
        self.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.setSizePolicy(QSizePolicy.Policy.Maximum, QSizePolicy.Policy.Fixed)
        self._tone = "muted"
        self.set_status(status, undo=False)

    def set_status(self, status: str, *, undo: bool = False) -> None:
        """Show ``status`` with the tone its meaning carries."""
        self.setText(status_label(status))
        self._tone = undo_status_tone(status) if undo else plan_status_tone(status)
        foreground, background = theme.tokens().tone(self._tone)
        self.setStyleSheet(
            "QLabel#Badge {"
            f" background: {background}; color: {foreground};"
            f" border-radius: {theme.RADIUS['sm']}px;"
            f" padding: 1px {theme.SPACE['xs'] + 2}px;"
            f" font-size: {theme.TYPE_SCALE['xs']}px; font-weight: 700; }}"
        )
