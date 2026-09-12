"""The Opportunities table: view-model and cell delegates.

``spacesage.opportunities`` owns every number and every rule (no SQL, no Qt in
the widgets); this module is the thin Qt layer that renders it -- one row per
:class:`~spacesage.opportunities.Opportunity`, the checkbox cascade as the
check-state of the first column, and hand-painted badges and chips so the list
looks the same in both themes on every platform.

Sorting and filtering live in the engine module; the model only maps them onto
Qt's ``sort()``/``QModelIndex`` vocabulary.
"""

from __future__ import annotations

from dataclasses import dataclass

from PySide6.QtCore import (
    QAbstractItemModel,
    QAbstractTableModel,
    QEvent,
    QModelIndex,
    QObject,
    QPersistentModelIndex,
    QPoint,
    QRect,
    QRectF,
    QSize,
    Qt,
    Signal,
)
from PySide6.QtGui import QColor, QFont, QFontMetrics, QPainter, QPainterPath
from PySide6.QtWidgets import QStyledItemDelegate, QStyleOptionViewItem

from spacesage import opportunities, stats
from spacesage.app import icons, theme

COLUMN_SELECT = 0
COLUMN_PATH = 1
COLUMN_SIZE = 2
COLUMN_GAIN = 3
COLUMN_SOLUTION = 4
COLUMN_TIER = 5
COLUMN_CONFIDENCE = 6

ROW_HEIGHT = 34
"""One comfortable line per opportunity (the solution cell keeps its why)."""

BADGE_PADDING = 12
"""Horizontal padding inside a badge pill (6px each side, on the 4px grid)."""


@dataclass(frozen=True)
class Column:
    """One table column: header, width, alignment and sort key."""

    key: str
    title: str
    width: int
    sort_key: str | None = None
    tooltip: str = ""


COLUMNS: tuple[Column, ...] = (
    Column("select", "", 32, None, "Select this opportunity (Ctrl+Space)"),
    Column("path", "Path", 420, "path", "The file or folder, biggest gain first"),
    Column("size", "Size", 84, "size", "Logical size; a folder is its whole subtree"),
    Column("gain", "Est. gain", 118, "gain", "Bytes the suggested solution frees"),
    Column("solution", "Suggested solution", 460, "solution", "The rule engine's verdict"),
    Column("tier", "Tier", 52, "", "T1 disposable, T2 app-owned, T3 report-only"),
    Column("confidence", "Confidence", 84, "confidence", "How sure the rules are"),
)


def solution_tone(row: opportunities.Opportunity) -> str:
    """Badge tone of a row's suggested solution."""
    if row.action == "KEEP":
        return "muted"
    return {
        "DELETE_QUARANTINE": "danger",
        "MOVE": "accent",
        "COMPRESS_NTFS": "info",
        "NATIVE": "info",
        "REVIEW": "warning",
    }.get(row.action, "muted")


class OpportunityTableModel(QAbstractTableModel):
    """Qt view-model over a ranked list (design §9, screen 2)."""

    selectionChanged = Signal()

    def __init__(self, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self._all: tuple[opportunities.Opportunity, ...] = ()
        self._filtered: tuple[opportunities.Opportunity, ...] = ()
        self._rows: tuple[opportunities.Opportunity, ...] = ()
        self._filter = opportunities.OpportunityFilter()
        self._sort_key = "gain"
        self._descending = True
        self._selection = opportunities.Selection(())
        self._index_of_key: dict[str, int] = {}

    # -- data ------------------------------------------------------------- #

    def set_rows(self, rows: tuple[opportunities.Opportunity, ...]) -> None:
        """Replace the dataset (fresh analysis or a re-sort of the engine list)."""
        self.beginResetModel()
        self._all = rows
        self._selection = opportunities.Selection(rows)
        self._rebuild()
        self.endResetModel()
        self.selectionChanged.emit()

    def set_filter(self, filter_: opportunities.OpportunityFilter) -> None:
        """Apply the filter bar's state."""
        self.beginResetModel()
        self._filter = filter_
        self._rebuild()
        self.endResetModel()
        self.selectionChanged.emit()

    def set_sort(self, column_key: str, descending: bool) -> None:
        """Order the visible rows by one column (header click or keyboard)."""
        if column_key not in opportunities.SORT_COLUMNS:
            return
        self.beginResetModel()
        self._sort_key = column_key
        self._descending = descending
        self._rebuild()
        self.endResetModel()

    def _rebuild(self) -> None:
        """Recompute the visible rows from the filter and the sort order."""
        self._filtered = opportunities.apply_filter(self._all, self._filter)
        self._rows = opportunities.sort_rows(
            self._filtered, self._sort_key, descending=self._descending
        )
        self._index_of_key = {row.key: index for index, row in enumerate(self._rows)}

    # -- queries ---------------------------------------------------------- #

    @property
    def rows(self) -> tuple[opportunities.Opportunity, ...]:
        """Every row the model holds (filtered or not)."""
        return self._all

    @property
    def visible_rows(self) -> tuple[opportunities.Opportunity, ...]:
        """The rows currently on screen, in their display order."""
        return self._rows

    @property
    def filter(self) -> opportunities.OpportunityFilter:
        """The filter bar's state as the model sees it."""
        return self._filter

    @property
    def selection(self) -> opportunities.Selection:
        """The checkbox selection (one row per branch, never two)."""
        return self._selection

    @property
    def sort_key(self) -> str:
        """The column the table is ordered by."""
        return self._sort_key

    @property
    def descending(self) -> bool:
        """Whether the sort runs biggest/latest first."""
        return self._descending

    def row_at(
        self, index: QModelIndex | QPersistentModelIndex | None
    ) -> opportunities.Opportunity | None:
        """The opportunity behind a model index."""
        if index is None or not index.isValid():
            return None
        row = index.row()
        if 0 <= row < len(self._rows):
            return self._rows[row]
        return None

    def index_of(self, path: str) -> QModelIndex:
        """The index of a row by path (or its normalised key); invalid when filtered out.

        Callers hold the path the export carries (``C:\\Users\\...``); the model
        keys rows by :func:`spacesage.opportunities.path_key`, so the lookup
        normalises -- the same fold the ranking uses.  A key works too: the
        normalisation is idempotent.
        """
        row = self._index_of_key.get(opportunities.path_key(path))
        if row is None:
            return QModelIndex()
        return self.index(row, 0)

    def selected_summary(self) -> str:
        """``"3 checked · 1.2 GiB estimated gain"`` -- the footer's figure.

        Wording is about the *checkboxes*: a highlighted row is not a selection,
        and saying "Nothing selected" next to one reads as a bug.
        """
        selection = self._selection
        if not len(selection):
            return "No rows checked yet"
        nouns = f"{len(selection)} checked"
        if selection.gain > 0:
            return f"{nouns} · {stats.format_bytes(selection.gain)} estimated gain"
        return nouns

    # -- Qt interface ----------------------------------------------------- #

    def rowCount(self, parent: QModelIndex | QPersistentModelIndex | None = None) -> int:
        return 0 if parent is not None and parent.isValid() else len(self._rows)

    def columnCount(self, parent: QModelIndex | QPersistentModelIndex | None = None) -> int:
        return 0 if parent is not None and parent.isValid() else len(COLUMNS)

    def headerData(
        self,
        section: int,
        orientation: Qt.Orientation,
        role: int = Qt.ItemDataRole.DisplayRole,
    ) -> object:
        if orientation != Qt.Orientation.Horizontal or not 0 <= section < len(COLUMNS):
            return None
        column = COLUMNS[section]
        if role == Qt.ItemDataRole.ToolTipRole and column.tooltip:
            return column.tooltip
        if role == Qt.ItemDataRole.DisplayRole:
            return column.title
        return None

    def flags(self, index: QModelIndex | QPersistentModelIndex) -> Qt.ItemFlag:
        base = super().flags(index)
        if index.column() == COLUMN_SELECT:
            return Qt.ItemFlag(base | Qt.ItemFlag.ItemIsUserCheckable | Qt.ItemFlag.ItemIsEnabled)
        return Qt.ItemFlag(base | Qt.ItemFlag.ItemIsEnabled | Qt.ItemFlag.ItemIsSelectable)

    def data(
        self,
        index: QModelIndex | QPersistentModelIndex,
        role: int = Qt.ItemDataRole.DisplayRole,
    ) -> object:
        row = self.row_at(index)
        if row is None:
            return None
        column = COLUMNS[index.column()]
        selection = self._selection
        if role == Qt.ItemDataRole.CheckStateRole and column.key == "select":
            if selection.is_selected(row.key):
                return Qt.CheckState.Checked
            if selection.has_selected_descendant(row.key):
                return Qt.CheckState.PartiallyChecked
            return Qt.CheckState.Unchecked
        if role == Qt.ItemDataRole.UserRole:
            return row
        if role == Qt.ItemDataRole.UserRole + 1:
            return selection.state(row.key)
        if role == Qt.ItemDataRole.DisplayRole:
            return self._display(row, column.key)
        if role == Qt.ItemDataRole.ToolTipRole:
            return self._tooltip(row)
        if role == Qt.ItemDataRole.TextAlignmentRole:
            if column.key in {"size", "gain"}:
                return int(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
            if column.key in {"tier", "confidence", "select"}:
                return int(Qt.AlignmentFlag.AlignCenter)
            return int(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter)
        if role == Qt.ItemDataRole.FontRole and column.key in {"size", "gain", "path"}:
            return theme.mono_font("sm")
        if role == Qt.ItemDataRole.ForegroundRole and column.key not in {"select", "solution"}:
            if selection.is_covered(row.key) or row.state == opportunities.STATE_NO_ACTION:
                return QColor(theme.tokens().faint)
            return QColor(theme.tokens().text)
        if role == Qt.ItemDataRole.DecorationRole and column.key == "path":
            return icons.icon("folder" if row.is_dir else "file", theme.tokens().muted, 14)
        return None

    def _display(self, row: opportunities.Opportunity, key: str) -> str:
        if key == "path":
            return row.path
        if key == "size":
            return stats.format_bytes(row.size)
        if key == "gain":
            return row.gain_label
        if key == "solution":
            return row.solution
        if key == "tier":
            return row.tier
        if key == "confidence":
            return f"{round(row.confidence * 100)}%"
        return ""

    def _tooltip(self, row: opportunities.Opportunity) -> str:
        lines = [row.path, "", row.why]
        if row.kind_label:
            lines.append(f"Kind: {row.kind_label}")
        lines.append(f"State: {row.state_label} · Tier {row.tier} · Gain: {row.gain_basis}")
        if row.members:
            shown = ", ".join(row.members[:3])
            extra = (
                f" (+{row.member_count - len(row.members)} more)" if row.member_count > 3 else ""
            )
            lines.append(f"Covers: {shown}{extra}")
        return "\n".join(lines)

    def sort(self, column: int, order: Qt.SortOrder = Qt.SortOrder.AscendingOrder) -> None:
        """Qt's sort hook (fed by header clicks)."""
        if not 0 <= column < len(COLUMNS):
            return
        key = COLUMNS[column].sort_key
        if not key:
            return
        self.set_sort(key, order == Qt.SortOrder.DescendingOrder)

    def setData(
        self,
        index: QModelIndex | QPersistentModelIndex,
        value: object,
        role: int = Qt.ItemDataRole.EditRole,
    ) -> bool:
        row = self.row_at(index)
        if row is None or role != Qt.ItemDataRole.CheckStateRole:
            return False
        if index.column() != COLUMN_SELECT:
            return False
        wanted = value == Qt.CheckState.Checked or value is True
        self.set_checked(row.key, wanted)
        return True

    # -- selection -------------------------------------------------------- #

    def set_checked(self, path: str, checked: bool) -> None:
        """Check or uncheck one row (by path) and repaint exactly what changed."""
        touched = self._selection.set_selected(opportunities.path_key(path), checked)
        self._emit_touched(touched)

    def toggle(self, path: str) -> None:
        """Flip one row's checkbox by path (the cascade rules live in the engine model)."""
        touched = self._selection.toggle(opportunities.path_key(path))
        self._emit_touched(touched)

    def select_all_visible(self) -> None:
        """Check every visible row, folders first (children stay covered)."""
        touched = self._selection.select_all(row.key for row in self._rows)
        self._emit_touched(touched)

    def clear_selection(self) -> None:
        """Uncheck everything."""
        touched = self._selection.clear()
        self._emit_touched(touched)

    def _emit_touched(self, touched: tuple[str, ...]) -> None:
        for key in touched:
            index = self._index_of_key.get(key)
            if index is None:
                continue
            self.dataChanged.emit(
                self.index(index, 0),
                self.index(index, len(COLUMNS) - 1),
                [Qt.ItemDataRole.CheckStateRole, Qt.ItemDataRole.ForegroundRole],
            )
        self.selectionChanged.emit()


# --------------------------------------------------------------------------- #
# Delegates
# --------------------------------------------------------------------------- #


def _paint_badge(
    painter: QPainter,
    rect: QRect,
    text: str,
    tone: str,
    *,
    font: QFont,
) -> None:
    """Paint one rounded pill with its text.

    ``BADGE_PADDING`` is the pill's whole horizontal padding: a badge is sized
    ``text width + BADGE_PADDING`` and its text is centred in that, so the first
    and last glyphs are never clipped by the pill's own edge.
    """
    foreground, background = theme.tokens().tone(tone)
    painter.save()
    painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
    path = QPainterPath()
    path.addRoundedRect(
        float(rect.x()), float(rect.y()), float(rect.width()), float(rect.height()), 6.0, 6.0
    )
    painter.fillPath(path, QColor(background))
    painter.setFont(font)
    painter.setPen(QColor(foreground))
    painter.drawText(
        rect.adjusted(BADGE_PADDING // 2, 0, -(BADGE_PADDING // 2), 0),
        int(Qt.AlignmentFlag.AlignCenter | Qt.TextFlag.TextSingleLine),
        text,
    )
    painter.restore()


class CheckDelegate(QStyledItemDelegate):
    """The select column: a 16px box that shows checked / partial / covered."""

    BOX = 16

    def paint(
        self,
        painter: QPainter,
        option: QStyleOptionViewItem,
        index: QModelIndex | QPersistentModelIndex,
    ) -> None:
        state = index.data(Qt.ItemDataRole.UserRole + 1) or "unchecked"
        tokens = theme.tokens()
        painter.save()
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        box = QRect(0, 0, self.BOX, self.BOX)
        box.moveCenter(QPoint(option.rect.center().x(), option.rect.center().y()))
        border = QColor(tokens.border_strong if state != "covered" else tokens.border)
        fill = QColor("transparent")
        if state == "checked":
            border = QColor(tokens.accent)
            fill = QColor(tokens.accent)
        elif state == "partial":
            border = QColor(tokens.accent)
            fill = QColor(tokens.accent_soft)
        elif state == "covered":
            fill = QColor(tokens.sunken)
        path = QPainterPath()
        path.addRoundedRect(
            float(box.x()), float(box.y()), float(box.width()), float(box.height()), 4.0, 4.0
        )
        painter.fillPath(path, fill)
        painter.setPen(border)
        painter.drawPath(path)
        if state == "checked":
            glyph = icons.pixmap("check", tokens.accent_text, 12)
            painter.drawPixmap(box.x() + 2, box.y() + 2, glyph)
        elif state == "partial":
            painter.setPen(QColor(tokens.accent))
            painter.drawLine(box.x() + 4, box.center().y(), box.right() - 4, box.center().y())
        elif state == "covered":
            painter.setPen(QColor(tokens.faint))
            painter.drawLine(box.x() + 5, box.center().y(), box.right() - 5, box.center().y())
        painter.restore()

    def sizeHint(
        self, option: QStyleOptionViewItem, index: QModelIndex | QPersistentModelIndex
    ) -> QSize:
        return QSize(COLUMNS[COLUMN_SELECT].width, ROW_HEIGHT)

    def editorEvent(
        self,
        event: object,
        model: QAbstractItemModel,
        option: QStyleOptionViewItem,
        index: QModelIndex | QPersistentModelIndex,
    ) -> bool:
        """Toggle on a click anywhere in the cell (the whole cell is the target)."""
        if not isinstance(model, OpportunityTableModel):
            return False
        row = index.data(Qt.ItemDataRole.UserRole)
        if not isinstance(row, opportunities.Opportunity):
            return False
        position = getattr(event, "position", None)
        if position is None:
            return False
        point = position().toPoint()
        if not QRectF(option.rect).contains(point.x(), point.y()):
            return False
        event_type = getattr(event, "type", lambda: None)()
        if event_type != QEvent.Type.MouseButtonRelease:
            return False
        checked = index.data(Qt.ItemDataRole.CheckStateRole) == Qt.CheckState.Checked
        model.set_checked(row.key, not checked)
        return True


class SolutionDelegate(QStyledItemDelegate):
    """The solution column: action icon, badge and the one-line why."""

    MIN_WHY = 80
    """Narrowest stretch of the why line worth painting next to the badge."""

    def paint(
        self,
        painter: QPainter,
        option: QStyleOptionViewItem,
        index: QModelIndex | QPersistentModelIndex,
    ) -> None:
        row = index.data(Qt.ItemDataRole.UserRole)
        if not isinstance(row, opportunities.Opportunity):
            super().paint(painter, option, index)
            return
        tokens = theme.tokens()
        selection = index.data(Qt.ItemDataRole.UserRole + 1)
        painter.save()
        painter.setClipRect(option.rect)
        rect = option.rect.adjusted(theme.SPACE["sm"], 0, -theme.SPACE["sm"], 0)

        icon_size = 14
        glyph = icons.action_icon(row.action, icon_size).pixmap(icon_size, icon_size)
        painter.drawPixmap(rect.x(), rect.center().y() - icon_size // 2 - 1, glyph)
        x = rect.x() + icon_size + theme.SPACE["sm"]

        label = row.solution
        font = theme.ui_font("xs", weight=QFont.Weight.Bold)
        metrics = QFontMetrics(font)
        # The badge gets the room it needs first; it is elided only when the
        # column is narrower than the words themselves.
        space = max(rect.right() - x, 0)
        full_width = metrics.horizontalAdvance(label) + BADGE_PADDING
        if full_width <= space:
            badge_width, badge_label = full_width, label
        else:
            badge_width = space
            badge_label = metrics.elidedText(
                label, Qt.TextElideMode.ElideRight, max(space - BADGE_PADDING, 0)
            )
        tone = solution_tone(row)
        badge = QRect(x, rect.center().y() - 9, badge_width, 18)
        _paint_badge(painter, badge, badge_label, tone, font=font)
        x = badge.right() + theme.SPACE["sm"]

        # The badge already names the action, so the line beside it carries only
        # the reason -- and is dropped, never squeezed, when the room is gone.
        remaining = QRect(x, rect.y(), max(rect.right() - x, 0), rect.height())
        if remaining.width() < self.MIN_WHY:
            painter.restore()
            return
        why_font = theme.ui_font("sm")
        painter.setFont(why_font)
        painter.setPen(QColor(tokens.muted if selection != "covered" else tokens.faint))
        painter.drawText(
            remaining,
            int(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter),
            painter.fontMetrics().elidedText(
                why_without_label(row), Qt.TextElideMode.ElideRight, remaining.width()
            ),
        )
        painter.restore()


def why_without_label(row: opportunities.Opportunity) -> str:
    """The reason alone: the row's why line minus the label its badge already shows."""
    label = f"{row.solution}: "
    return row.why[len(label) :] if row.why.startswith(label) else row.why


class TierDelegate(QStyledItemDelegate):
    """The tier column: a T1/T2/T3 badge with consistent semantics."""

    def paint(
        self,
        painter: QPainter,
        option: QStyleOptionViewItem,
        index: QModelIndex | QPersistentModelIndex,
    ) -> None:
        from spacesage.app.widgets import tier_tone

        text = str(index.data(Qt.ItemDataRole.DisplayRole) or "")
        if not text:
            return
        font = theme.mono_font("xs")
        font.setWeight(QFont.Weight.Bold)
        painter.save()
        painter.setFont(font)
        width = painter.fontMetrics().horizontalAdvance(text) + BADGE_PADDING
        badge = QRect(0, 0, width, 18)
        badge.moveCenter(option.rect.center())
        _paint_badge(painter, badge, text, tier_tone(text), font=font)
        painter.restore()


class ConfidenceDelegate(QStyledItemDelegate):
    """The confidence column: a percentage chip toned by how sure the rules are."""

    def paint(
        self,
        painter: QPainter,
        option: QStyleOptionViewItem,
        index: QModelIndex | QPersistentModelIndex,
    ) -> None:
        from spacesage.app.widgets import confidence_tone

        row = index.data(Qt.ItemDataRole.UserRole)
        if not isinstance(row, opportunities.Opportunity):
            super().paint(painter, option, index)
            return
        font = theme.mono_font("xs")
        font.setWeight(QFont.Weight.Bold)
        text = f"{round(row.confidence * 100)}%"
        painter.save()
        painter.setFont(font)
        width = painter.fontMetrics().horizontalAdvance(text) + BADGE_PADDING
        badge = QRect(0, 0, width, 18)
        badge.moveCenter(option.rect.center())
        _paint_badge(painter, badge, text, confidence_tone(row.confidence), font=font)
        painter.restore()
