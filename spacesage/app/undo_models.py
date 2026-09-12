"""The Undo table: view-model and cell delegates (design §9, screen 3).

``spacesage.planning`` owns every fact this screen shows -- one
:class:`~spacesage.planning.JournalItem` per journaled operation, with the status
the journal implies and the verification the executor recorded -- so this module
is only the thin Qt layer that renders it.  The first column is the check state
of the row (checked = "this one will be reverted", and only operations still
awaiting a reversal are checkable at all) and the pills are hand-painted, so the
table looks the same in both themes on every platform.

Nothing a row cannot show disappears: the reason, the journal reference and the
verification ride on the row's tooltip, and a blocked or failed operation keeps
its danger tone in the status column.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from datetime import datetime

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

from spacesage import executor, planning, stats
from spacesage.app import icons, theme, widgets

COLUMN_SELECT = 0
COLUMN_WHEN = 1
COLUMN_PATH = 2
COLUMN_OPERATION = 3
COLUMN_SIZE = 4
COLUMN_STATUS = 5
COLUMN_VERIFICATION = 6

ROW_HEIGHT = 34
"""One comfortable line per operation (the tooltip keeps the whole story)."""

BADGE_PADDING = 12
"""Horizontal padding inside a badge pill (6px each side, on the 4px grid)."""

ROLE_ITEM = Qt.ItemDataRole.UserRole
"""``JournalItem`` behind a model index."""

ROLE_CHECK = Qt.ItemDataRole.UserRole + 1
"""``"checked"`` / ``"unchecked"`` / ``"unavailable"`` -- the select cell's state."""

ROLE_TONE = Qt.ItemDataRole.UserRole + 2
"""The status column's badge tone, resolved from the live status."""

AWAITING_STATUSES: tuple[str, ...] = ("pending", "interrupted")
"""The statuses a reversal still applies to (the executor's own ``pending()``)."""

OP_WORDS: dict[str, str] = {
    "quarantine": "Quarantine",
    "move": "Move",
    "link": "Link",
    "compress": "Compress",
}

INVERSE_WORDS: dict[str, str] = {
    "move_back": "move back",
    "remove_link": "remove link",
    "uncompress": "uncompress",
}


def operation_label(item: planning.JournalItem) -> str:
    """The journaled operation and the inverse it would run ("Quarantine -> move back")."""
    word = OP_WORDS.get(item.op, item.op.capitalize())
    inverse = INVERSE_WORDS.get(item.inverse)
    return f"{word} -> {inverse}" if inverse else word


def short_when(at: str) -> str:
    """``2026-09-12T12:00:00+00:00`` -> ``2026-09-12 12:00`` (the column's format)."""
    try:
        return datetime.fromisoformat(at).strftime("%Y-%m-%d %H:%M")
    except ValueError:
        return at[:16].replace("T", " ")


def verification_word(verify: str) -> str:
    """The one word (or acronym) the verification column shows."""
    if verify == "verified":
        return "verified"
    if verify == "mismatch":
        return "MISMATCH"
    return "unverified"


def verification_tone(verify: str) -> str:
    """Badge tone of a verification state (danger only for a real mismatch)."""
    return {"verified": "success", "mismatch": "danger"}.get(verify, "muted")


def awaiting_items(history: planning.JournalHistory) -> tuple[planning.JournalItem, ...]:
    """The operations still awaiting a reversal -- what "revert all pending" reverts.

    The executor's own ``pending()`` folds an interrupted operation (a start
    record without its end record) into the same set and settles it against the
    filesystem, so the screen counts it as awaiting too.
    """
    return tuple(
        item for item in history.items if item.status in AWAITING_STATUSES and item.reversible
    )


@dataclass(frozen=True)
class Column:
    """One table column: header, width, alignment and tooltip."""

    key: str
    title: str
    width: int
    tooltip: str = ""


COLUMNS: tuple[Column, ...] = (
    Column("select", "", 32, "Check the operations that should be reverted (Ctrl+Space)"),
    Column("when", "When", 148, "When the operation ran"),
    Column("path", "Path", 340, "The entry the operation acted on"),
    Column("operation", "Operation", 230, "The journaled operation and the inverse that will run"),
    Column("size", "Size", 90, "Bytes this operation moved"),
    Column("status", "Status", 104, "Pending / Reversed / Blocked / Failed / Interrupted"),
    Column("verification", "Verification", 116, "Verified against the digest taken when it ran"),
)


class UndoTableModel(QAbstractTableModel):
    """Qt view-model over one journal's operations (newest first)."""

    selection_changed = Signal()

    def __init__(self, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self._items: tuple[planning.JournalItem, ...] = ()
        self._index_of_seq: dict[int, int] = {}
        self._checked: set[int] = set()
        self._results: dict[int, executor.UndoResult] = {}

    # -- data ------------------------------------------------------------- #

    def set_history(self, history: planning.JournalHistory | None) -> None:
        """Adopt one journal: newest operation first, every awaiting one checked."""
        self.beginResetModel()
        self._items = () if history is None else tuple(reversed(history.items))
        self._index_of_seq = {item.seq: index for index, item in enumerate(self._items)}
        self._results = {}
        self.endResetModel()
        self.check_all_pending()

    def set_results(self, report: executor.UndoReport) -> None:
        """Fold a finished undo run into the rows (per-item outcome + verification)."""
        self._results = {result.op_ref: result for result in report.ops}
        self._emit_changed(range(len(self._items)))

    def apply_result(self, result: executor.UndoResult) -> None:
        """Show one live :class:`~spacesage.executor.UndoResult` as it lands."""
        self._results[result.op_ref] = result
        row = self._index_of_seq.get(result.op_ref)
        if row is not None:
            self._emit_changed([row])

    def _emit_changed(self, rows: Iterable[int]) -> None:
        roles = [
            Qt.ItemDataRole.DisplayRole,
            Qt.ItemDataRole.ToolTipRole,
            Qt.ItemDataRole.ForegroundRole,
        ]
        for row in rows:
            self.dataChanged.emit(self.index(row, 0), self.index(row, len(COLUMNS) - 1), roles)

    # -- queries ---------------------------------------------------------- #

    @property
    def items(self) -> tuple[planning.JournalItem, ...]:
        """Every row the model holds, in display order (newest first)."""
        return self._items

    def pending_seqs(self) -> tuple[int, ...]:
        """The seqs still awaiting a reversal (what "revert all pending" reverts)."""
        return tuple(item.seq for item in self._items if self._checkable(item))

    def checked_seqs(self) -> tuple[int, ...]:
        """The seqs the user checked (sorted; an empty tuple when nothing is checked)."""
        return tuple(sorted(self._checked))

    def checked_summary(self) -> str:
        """``"2 checked · 3.1 MiB to restore"`` -- the footer's figure."""
        if not self._checked:
            return "Nothing checked: nothing will be reverted"
        size = sum(item.bytes for item in self._items if item.seq in self._checked)
        return f"{len(self._checked)} checked · {stats.format_bytes(size)} to restore"

    def row_at(
        self, index: QModelIndex | QPersistentModelIndex | None
    ) -> planning.JournalItem | None:
        """The operation behind a model index."""
        if index is None or not index.isValid():
            return None
        row = index.row()
        if 0 <= row < len(self._items):
            return self._items[row]
        return None

    def index_of(self, seq: int) -> QModelIndex:
        """The index of the row with this journal ``seq`` (invalid when it is absent)."""
        row = self._index_of_seq.get(seq)
        if row is None:
            return QModelIndex()
        return self.index(row, 0)

    def status_of(self, item: planning.JournalItem) -> str:
        """The row's status, with a live undo result folded in."""
        result = self._results.get(item.seq)
        if result is None:
            return item.status
        return "reversed" if result.outcome == "done" else result.outcome

    def _verify_of(self, item: planning.JournalItem) -> str:
        """The verification state the row shows, live results first."""
        result = self._results.get(item.seq)
        if result is not None and result.steps:
            return result.steps[0].verify
        return item.verify

    def _checkable(self, item: planning.JournalItem) -> bool:
        """True when checking the row means something (it is awaiting, and reversible)."""
        return item.status in AWAITING_STATUSES and item.reversible

    # -- checks ----------------------------------------------------------- #

    def set_checked(self, seq: int, checked: bool) -> bool:
        """Check or uncheck one row by journal seq (only awaiting rows can be checked)."""
        row = self._index_of_seq.get(seq)
        if row is None:
            return False
        item = self._items[row]
        if checked and not self._checkable(item):
            return False
        was = seq in self._checked
        if checked:
            self._checked.add(seq)
        else:
            self._checked.discard(seq)
        if was != checked:
            self.dataChanged.emit(
                self.index(row, 0),
                self.index(row, len(COLUMNS) - 1),
                [Qt.ItemDataRole.CheckStateRole, Qt.ItemDataRole.ForegroundRole],
            )
            self.selection_changed.emit()
        return True

    def check_all_pending(self) -> None:
        """Check every operation that is still awaiting a reversal."""
        self._checked = {item.seq for item in self._items if self._checkable(item)}
        self._emit_changed(range(len(self._items)))
        self.selection_changed.emit()

    def clear_checks(self) -> None:
        """Uncheck everything."""
        self._checked.clear()
        self._emit_changed(range(len(self._items)))
        self.selection_changed.emit()

    # -- Qt interface ----------------------------------------------------- #

    def rowCount(self, parent: QModelIndex | QPersistentModelIndex | None = None) -> int:
        return 0 if parent is not None and parent.isValid() else len(self._items)

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
        item = self.row_at(index)
        if index.column() == COLUMN_SELECT and item is not None and self._checkable(item):
            return Qt.ItemFlag(base | Qt.ItemFlag.ItemIsUserCheckable | Qt.ItemFlag.ItemIsEnabled)
        return Qt.ItemFlag(base | Qt.ItemFlag.ItemIsEnabled | Qt.ItemFlag.ItemIsSelectable)

    def data(
        self,
        index: QModelIndex | QPersistentModelIndex,
        role: int = Qt.ItemDataRole.DisplayRole,
    ) -> object:
        item = self.row_at(index)
        if item is None:
            return None
        column = COLUMNS[index.column()]
        if role == Qt.ItemDataRole.CheckStateRole and column.key == "select":
            if not self._checkable(item):
                return None
            if item.seq in self._checked:
                return Qt.CheckState.Checked
            return Qt.CheckState.Unchecked
        if role == ROLE_ITEM:
            return item
        if role == ROLE_CHECK:
            return self._check_state(item)
        if role == ROLE_TONE:
            return widgets.undo_status_tone(self.status_of(item))
        if role == Qt.ItemDataRole.DisplayRole:
            return self._display(item, column.key)
        if role == Qt.ItemDataRole.ToolTipRole:
            return self._tooltip(item)
        if role == Qt.ItemDataRole.TextAlignmentRole:
            if column.key == "size":
                return int(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
            if column.key in {"select", "status", "verification"}:
                return int(Qt.AlignmentFlag.AlignCenter)
            return int(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter)
        if role == Qt.ItemDataRole.FontRole and column.key in {"when", "path", "size"}:
            return theme.mono_font("sm")
        if role == Qt.ItemDataRole.ForegroundRole:
            return self._foreground(item, column.key)
        return None

    def _check_state(self, item: planning.JournalItem) -> str:
        if not self._checkable(item):
            return "unavailable"
        return "checked" if item.seq in self._checked else "unchecked"

    def _display(self, item: planning.JournalItem, key: str) -> str:
        if key == "when":
            return short_when(item.at)
        if key == "path":
            return item.path
        if key == "operation":
            return operation_label(item)
        if key == "size":
            return stats.format_bytes(item.bytes)
        if key == "status":
            return widgets.status_label(self.status_of(item))
        if key == "verification":
            return verification_word(self._verify_of(item))
        return ""

    def _foreground(self, item: planning.JournalItem, key: str) -> QColor | None:
        tokens = theme.tokens()
        if key == "verification":
            return QColor(tokens.tone(verification_tone(self._verify_of(item)))[0])
        if key in {"select", "status"}:
            return None  # the delegates paint these cells themselves
        if self.status_of(item) in {"reversed", "skipped"}:
            return QColor(tokens.faint)
        return QColor(tokens.text)

    def _tooltip(self, item: planning.JournalItem) -> str:
        """The whole story of a row: nothing the table cannot show is hidden here."""
        status = self.status_of(item)
        lines = [item.path]
        if item.dest:
            lines.append(f"-> {item.dest}")
        lines.append("")
        lines.append(f"Operation: {operation_label(item)}")
        lines.append(f"Action {item.action_id} · run {item.run} · journal #{item.seq}")
        lines.append(f"Status: {widgets.status_label(status)}")
        lines.append(f"Note: {item.reason or 'no note was recorded'}")
        lines.append(f"Verification: {verification_word(self._verify_of(item))}")
        if not item.reversible:
            lines.append("This operation cannot be reversed; it is listed for the record.")
        return "\n".join(lines)

    def setData(
        self,
        index: QModelIndex | QPersistentModelIndex,
        value: object,
        role: int = Qt.ItemDataRole.EditRole,
    ) -> bool:
        item = self.row_at(index)
        if item is None or role != Qt.ItemDataRole.CheckStateRole:
            return False
        if index.column() != COLUMN_SELECT:
            return False
        self.set_checked(item.seq, value == Qt.CheckState.Checked or value is True)
        return True


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
    """The select column: a 16px box; an awaiting operation gets one to tick."""

    BOX = 16

    def paint(
        self,
        painter: QPainter,
        option: QStyleOptionViewItem,
        index: QModelIndex | QPersistentModelIndex,
    ) -> None:
        state = index.data(ROLE_CHECK) or "unavailable"
        tokens = theme.tokens()
        painter.save()
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        box = QRect(0, 0, self.BOX, self.BOX)
        box.moveCenter(QPoint(option.rect.center().x(), option.rect.center().y()))
        if state == "checked":
            border = QColor(tokens.accent)
            fill = QColor(tokens.accent)
        elif state == "unchecked":
            border = QColor(tokens.border_strong)
            fill = QColor("transparent")
        else:
            border = QColor(tokens.border)
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
        elif state == "unavailable":
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
        if not isinstance(model, UndoTableModel):
            return False
        item = index.data(ROLE_ITEM)
        if not isinstance(item, planning.JournalItem):
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
        model.set_checked(item.seq, not checked)
        return True


class StatusDelegate(QStyledItemDelegate):
    """The status column: one badge, toned by what the status means."""

    def paint(
        self,
        painter: QPainter,
        option: QStyleOptionViewItem,
        index: QModelIndex | QPersistentModelIndex,
    ) -> None:
        text = str(index.data(Qt.ItemDataRole.DisplayRole) or "")
        if not text:
            return
        tone = str(index.data(ROLE_TONE) or "muted")
        font = theme.ui_font("xs", weight=QFont.Weight.Bold)
        metrics = QFontMetrics(font)
        space = max(option.rect.right() - option.rect.x() - theme.SPACE["sm"], 0)
        width = min(metrics.horizontalAdvance(text) + BADGE_PADDING, space)
        painter.save()
        painter.setFont(font)
        badge = QRect(0, 0, width, 18)
        badge.moveCenter(option.rect.center())
        _paint_badge(painter, badge, text, tone, font=font)
        painter.restore()
