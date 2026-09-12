"""The Plan table: the draft's items, their approval, and their outcome.

One row per plan action, and nothing else -- the plan screen's whole job is to
show what would happen, let the user take items out of it, and then report what
did happen, per item, in the same rows.

The model owns exactly three pieces of state, in the order they arrive:

1. the **draft** -- actions, their origin, their resolved detail, and the status
   the composer already knows (``ready`` / ``advice`` / ``refused``);
2. the **approval** -- which action ids may run, which are taken out, and the
   advice items that can never run (``REVIEW``/``NATIVE``);
3. the **run** -- the outcome of the dry run and, later, of the execution, both
   reported per action id, so a row's status badge always says what happened to
   *that* item.

All engine facts come from :mod:`spacesage.planning` and
:mod:`spacesage.executor`; this module renders them and collects the user's
decisions (design §9, screen 3).
"""

from __future__ import annotations

from PySide6.QtCore import (
    QAbstractItemModel,
    QAbstractTableModel,
    QEvent,
    QModelIndex,
    QObject,
    QPersistentModelIndex,
    QPoint,
    QRect,
    QSize,
    Qt,
    Signal,
)
from PySide6.QtGui import QColor, QFont, QFontMetrics, QPainter
from PySide6.QtWidgets import QStyledItemDelegate, QStyleOptionViewItem

from spacesage import candidates, executor, planning, stats
from spacesage.app import icons, models, theme, widgets

COLUMN_APPROVE = 0
COLUMN_PATH = 1
COLUMN_ACTION = 2
COLUMN_TIER = 3
COLUMN_SIZE = 4
COLUMN_DETAIL = 5
COLUMN_STATUS = 6

ROW_HEIGHT = 34
"""One comfortable line per action: the detail cell keeps its destination."""

ACTION_TONES = {
    "DELETE_QUARANTINE": "danger",
    "MOVE": "accent",
    "COMPRESS_NTFS": "info",
    "NATIVE": "info",
    "REVIEW": "warning",
}
"""Badge tone of a plan action type (the same semantics the list uses)."""

COLUMNS: tuple[models.Column, ...] = (
    models.Column("approve", "", 32, None, "Approve this action for execution (Ctrl+Space)"),
    models.Column("path", "Path", 340, "path", "The entry this action belongs to"),
    models.Column("action", "Action", 190, "action", "What SpaceSage would do"),
    models.Column("tier", "Tier", 52, "", "Risk tier: executable actions are T1/T2 only"),
    models.Column("size", "Size", 96, "size", "Bytes this action reclaims"),
    models.Column("detail", "What happens", 380, "detail", "The resolved operation"),
    models.Column("status", "Status", 120, "status", "Approval, preview and run outcome"),
)


def action_label(item: planning.PlanItem) -> str:
    """The badge text of one item (advice items say so)."""
    label = candidates.action_label(item.action.type)
    return label


def action_tone(item: planning.PlanItem) -> str:
    """The badge tone of one item."""
    return ACTION_TONES.get(item.action.type, "muted")


def status_text(item: planning.PlanItem, outcome: str) -> str:
    """The status cell's text for an item (its run outcome wins over the draft)."""
    if outcome:
        return widgets.status_label(outcome)
    if item.status == "advice":
        return "Advice only"
    if item.status == "refused":
        return "Refused"
    return "Ready"


class PlanTableModel(QAbstractTableModel):
    """Qt view-model over one plan draft and the run it goes through."""

    approvalChanged = Signal()

    def __init__(self, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self._draft: planning.PlanDraft | None = None
        self._items: tuple[planning.PlanItem, ...] = ()
        self._approved: set[str] = set()
        self._outcomes: dict[str, str] = {}
        """``{action id: outcome}`` from the dry run or the execution."""

        self._reasons: dict[str, str] = {}
        self._index_of: dict[str, int] = {}

    # -- data ------------------------------------------------------------- #

    def set_draft(self, draft: planning.PlanDraft | None) -> None:
        """Replace the plan (a fresh draft, or ``None`` to clear the screen)."""
        self.beginResetModel()
        self._draft = draft
        self._items = draft.items if draft is not None else ()
        self._approved = set(draft.executable_ids()) if draft is not None else set()
        self._outcomes = {}
        self._reasons = {}
        self._index_of = {item.action.id: index for index, item in enumerate(self._items)}
        if draft is not None and draft.preview is not None:
            self._absorb_preview(draft.preview)
        self.endResetModel()
        self.approvalChanged.emit()

    @property
    def draft(self) -> planning.PlanDraft | None:
        """The plan the table shows."""
        return self._draft

    @property
    def items(self) -> tuple[planning.PlanItem, ...]:
        """Every row, in plan order."""
        return self._items

    def row_at(self, index: QModelIndex | QPersistentModelIndex | None) -> planning.PlanItem | None:
        """The item behind a model index."""
        if index is None or not index.isValid():
            return None
        row = index.row()
        return self._items[row] if 0 <= row < len(self._items) else None

    def index_of(self, action_id: str) -> QModelIndex:
        """The index of one action id (invalid when the plan does not hold it)."""
        row = self._index_of.get(action_id)
        return QModelIndex() if row is None else self.index(row, 0)

    def item(self, action_id: str) -> planning.PlanItem | None:
        """The item of one action id."""
        row = self._index_of.get(action_id)
        return self._items[row] if row is not None else None

    # -- approval --------------------------------------------------------- #

    def approved_ids(self) -> tuple[str, ...]:
        """The ids that may run, in plan order."""
        return tuple(item.action.id for item in self._items if item.action.id in self._approved)

    def approved_items(self) -> tuple[planning.PlanItem, ...]:
        """The items that may run, in plan order."""
        return tuple(item for item in self._items if item.action.id in self._approved)

    def rejected_ids(self) -> tuple[str, ...]:
        """The executable items the user took out of the plan."""
        return tuple(
            item.action.id
            for item in self._items
            if item.executable and item.action.id not in self._approved
        )

    def advice_ids(self) -> tuple[str, ...]:
        """The ids that can never run (``REVIEW``/``NATIVE`` advice)."""
        return tuple(item.action.id for item in self._items if not item.executable)

    def is_approved(self, action_id: str) -> bool:
        """Is this action id approved?"""
        return action_id in self._approved

    def set_approved(self, action_id: str, approved: bool) -> bool:
        """Approve or take out one action (advice items cannot be approved)."""
        item = self.item(action_id)
        if item is None or not item.executable:
            return False
        if approved == (action_id in self._approved):
            return True
        if approved:
            self._approved.add(action_id)
        else:
            self._approved.discard(action_id)
        self._touch(action_id)
        self.approvalChanged.emit()
        return True

    def approve_all(self) -> None:
        """Approve every executable action (advice items stay out)."""
        self._approved = {item.action.id for item in self._items if item.executable}
        self._touch_all()
        self.approvalChanged.emit()

    def reject_all(self) -> None:
        """Take every action out of the plan (nothing will run)."""
        self._approved = set()
        self._touch_all()
        self.approvalChanged.emit()

    def approved_count(self) -> int:
        """How many actions are approved."""
        return len(self._approved)

    def approved_bytes(self) -> int:
        """Bytes the approved actions claim (the plan's own numbers)."""
        return sum(item.action.bytes for item in self._items if item.action.id in self._approved)

    def selected_summary(self) -> str:
        """One line about the approval (the screen's footer)."""
        approved = self.approved_count()
        executable = sum(1 for item in self._items if item.executable)
        actions = "" if executable == 1 else "s"
        parts = [
            f"{approved} of {executable} executable action{actions} approved",
            f"{stats.format_bytes(self.approved_bytes())} to reclaim",
        ]
        advice = len(self.advice_ids())
        if advice:
            parts.append(f"{advice} advice item{'' if advice == 1 else 's'} (never executed)")
        return " · ".join(parts)

    # -- results ---------------------------------------------------------- #

    def set_preview(self, report: executor.ApplyReport) -> None:
        """Adopt a dry-run report: every row shows what the run would do."""
        self._outcomes = {}
        self._reasons = {}
        self._absorb_preview(report)
        self._touch_all()

    def set_result(self, report: executor.ApplyReport | None) -> None:
        """Adopt an execution report (or clear the run state with ``None``)."""
        self._outcomes = {}
        self._reasons = {}
        if report is not None:
            for op in report.ops:
                self._outcomes[op.action_id] = op.outcome
                self._reasons[op.action_id] = op.reason
        self._touch_all()

    def set_op_result(self, op: executor.OpResult) -> None:
        """Update one row when an execution reports it (live progress)."""
        self._outcomes[op.action_id] = op.outcome
        self._reasons[op.action_id] = op.reason
        self._touch(op.action_id)

    def _absorb_preview(self, report: executor.ApplyReport) -> None:
        for op in report.ops:
            self._outcomes[op.action_id] = op.outcome
            self._reasons[op.action_id] = op.reason

    def outcome_of(self, action_id: str) -> str:
        """The outcome reported for one action (``""`` before any run)."""
        return self._outcomes.get(action_id, "")

    def failures(self) -> tuple[tuple[str, str], ...]:
        """``(action id, reason)`` for everything that failed or was refused."""
        return tuple(
            (action_id, self._reasons.get(action_id, ""))
            for action_id, outcome in self._outcomes.items()
            if outcome in ("failed", "refused")
        )

    def has_failures(self) -> bool:
        """True when something failed or was refused (never hidden)."""
        return bool(self.failures())

    def counts(self) -> dict[str, int]:
        """``{outcome: count}`` over the rows that reported an outcome."""
        counts: dict[str, int] = {}
        for outcome in self._outcomes.values():
            counts[outcome] = counts.get(outcome, 0) + 1
        return counts

    def ready_to_execute(self) -> bool:
        """True when at least one approved action is neither refused nor advice."""
        return any(
            item.executable and item.status != "refused" and item.action.id in self._approved
            for item in self._items
        )

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
        if index.column() == COLUMN_APPROVE:
            flags = base | Qt.ItemFlag.ItemIsEnabled
            if item is not None and item.executable:
                flags |= Qt.ItemFlag.ItemIsUserCheckable
            return flags
        return base | Qt.ItemFlag.ItemIsEnabled | Qt.ItemFlag.ItemIsSelectable

    def data(
        self,
        index: QModelIndex | QPersistentModelIndex,
        role: int = Qt.ItemDataRole.DisplayRole,
    ) -> object:
        item = self.row_at(index)
        if item is None:
            return None
        column = COLUMNS[index.column()]
        if role == Qt.ItemDataRole.CheckStateRole and column.key == "approve":
            if not item.executable:
                return Qt.CheckState.Unchecked
            return (
                Qt.CheckState.Checked
                if item.action.id in self._approved
                else Qt.CheckState.Unchecked
            )
        if role == Qt.ItemDataRole.UserRole:
            return item
        if role == Qt.ItemDataRole.UserRole + 1:
            if not item.executable:
                return "advice"
            return "checked" if item.action.id in self._approved else "unchecked"
        if role == Qt.ItemDataRole.UserRole + 2:
            return self._outcomes.get(item.action.id, "")
        if role == Qt.ItemDataRole.DisplayRole:
            return self._display(item, column.key)
        if role == Qt.ItemDataRole.ToolTipRole:
            return self._tooltip(item)
        if role == Qt.ItemDataRole.TextAlignmentRole:
            if column.key in {"size", "tier", "approve"}:
                return int(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
            return int(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter)
        if role == Qt.ItemDataRole.FontRole and column.key in {"path", "size"}:
            return theme.mono_font("sm")
        if role == Qt.ItemDataRole.ForegroundRole and column.key in {"path", "size"}:
            if not item.executable or item.status == "refused":
                return QColor(theme.tokens().faint)
            if item.action.id in self._approved:
                return QColor(theme.tokens().text)
            return QColor(theme.tokens().muted)
        if role == Qt.ItemDataRole.DecorationRole and column.key == "path":
            return icons.icon("file", theme.tokens().muted, 14)
        return None

    def _display(self, item: planning.PlanItem, key: str) -> str:
        if key == "path":
            return item.action.path
        if key == "action":
            return action_label(item)
        if key == "tier":
            return item.action.tier
        if key == "size":
            return stats.format_bytes(item.action.bytes) if item.action.bytes else "--"
        if key == "detail":
            return item.detail
        if key == "status":
            return status_text(item, self._outcomes.get(item.action.id, ""))
        return ""

    def _tooltip(self, item: planning.PlanItem) -> str:
        action = item.action
        lines = [f"{action.path}", "", action.why or action.rationale]
        if item.detail:
            lines.append(item.detail)
        lines.append(
            f"Tier {action.tier} · confidence {round(action.confidence * 100)}% · "
            f"{stats.format_bytes(action.bytes)} · {candidates.action_label(action.type)}"
        )
        if action.side_effects:
            lines.append(f"Side effects: {action.side_effects}")
        if item.origin is not None and item.origin != action.path:
            lines.append(f"From the selected row: {item.origin}")
        outcome = self._outcomes.get(action.id, "")
        if outcome:
            reason = self._reasons.get(action.id, "")
            lines.append(f"Run: {widgets.status_label(outcome)} — {reason}")
        if not item.executable:
            lines.append("This is advice: SpaceSage never executes a review or native item.")
        return "\n".join(lines)

    def sort(self, column: int, order: Qt.SortOrder = Qt.SortOrder.AscendingOrder) -> None:
        """The plan's order is the execution order: sorting would misrepresent it."""
        return None

    def setData(
        self,
        index: QModelIndex | QPersistentModelIndex,
        value: object,
        role: int = Qt.ItemDataRole.EditRole,
    ) -> bool:
        item = self.row_at(index)
        if item is None or role != Qt.ItemDataRole.CheckStateRole:
            return False
        if index.column() != COLUMN_APPROVE:
            return False
        wanted = value == Qt.CheckState.Checked or value is True
        return self.set_approved(item.action.id, wanted)

    def _touch(self, action_id: str) -> None:
        row = self._index_of.get(action_id)
        if row is None:
            return
        self.dataChanged.emit(
            self.index(row, 0),
            self.index(row, len(COLUMNS) - 1),
            [
                Qt.ItemDataRole.CheckStateRole,
                Qt.ItemDataRole.DisplayRole,
                Qt.ItemDataRole.ForegroundRole,
                Qt.ItemDataRole.ToolTipRole,
            ],
        )

    def _touch_all(self) -> None:
        if self._items:
            self.dataChanged.emit(
                self.index(0, 0),
                self.index(len(self._items) - 1, len(COLUMNS) - 1),
                [Qt.ItemDataRole.DisplayRole, Qt.ItemDataRole.ToolTipRole],
            )


# --------------------------------------------------------------------------- #
# Delegates
# --------------------------------------------------------------------------- #


class PlanCheckDelegate(models.CheckDelegate):
    """The approve column: the list's checkbox, wired to the approval."""

    def editorEvent(
        self,
        event: object,
        model: QAbstractItemModel,
        option: QStyleOptionViewItem,
        index: QModelIndex | QPersistentModelIndex,
    ) -> bool:
        """Toggle on a click anywhere in the cell (advice rows do not react)."""
        if not isinstance(model, PlanTableModel):
            return False
        item = index.data(Qt.ItemDataRole.UserRole)
        if not isinstance(item, planning.PlanItem) or not item.executable:
            return False
        position = getattr(event, "position", None)
        if position is None:
            return False
        point = position().toPoint()
        if not option.rect.contains(QPoint(point.x(), point.y())):
            return False
        event_type = getattr(event, "type", lambda: None)()
        if event_type != QEvent.Type.MouseButtonRelease:
            return False
        approved = index.data(Qt.ItemDataRole.CheckStateRole) == Qt.CheckState.Checked
        return model.set_approved(item.action.id, not approved)

    def sizeHint(
        self, option: QStyleOptionViewItem, index: QModelIndex | QPersistentModelIndex
    ) -> QSize:
        return QSize(COLUMNS[COLUMN_APPROVE].width, ROW_HEIGHT)


class PlanActionDelegate(QStyledItemDelegate):
    """The action column: the icon and badge of what would happen, plus the why."""

    MIN_WHY = 60

    def paint(
        self,
        painter: QPainter,
        option: QStyleOptionViewItem,
        index: QModelIndex | QPersistentModelIndex,
    ) -> None:
        item = index.data(Qt.ItemDataRole.UserRole)
        if not isinstance(item, planning.PlanItem):
            super().paint(painter, option, index)
            return
        tokens = theme.tokens()
        painter.save()
        painter.setClipRect(option.rect)
        rect = option.rect.adjusted(theme.SPACE["sm"], 0, -theme.SPACE["sm"], 0)

        icon_size = 14
        glyph = icons.action_icon(item.action.type, icon_size).pixmap(icon_size, icon_size)
        painter.drawPixmap(rect.x(), rect.center().y() - icon_size // 2 - 1, glyph)
        x = rect.x() + icon_size + theme.SPACE["sm"]

        label = action_label(item)
        font = theme.ui_font("xs", weight=QFont.Weight.Bold)
        metrics = QFontMetrics(font)
        space = max(rect.right() - x, 0)
        full = metrics.horizontalAdvance(label) + models.BADGE_PADDING
        if full <= space:
            width, text = full, label
        else:
            width = space
            text = metrics.elidedText(
                label, Qt.TextElideMode.ElideRight, max(space - models.BADGE_PADDING, 0)
            )
        models._paint_badge(
            painter,
            QRect(x, rect.center().y() - 9, width, 18),
            text,
            action_tone(item),
            font=font,
        )
        x += width + theme.SPACE["sm"]
        remaining = QRect(x, rect.y(), max(rect.right() - x, 0), rect.height())
        if remaining.width() < self.MIN_WHY:
            painter.restore()
            return
        painter.setFont(theme.ui_font("sm"))
        painter.setPen(QColor(tokens.faint if not item.executable else tokens.muted))
        painter.drawText(
            remaining,
            int(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter),
            models.elide_words(
                item.reason if item.reason else _short_reason(item),
                remaining.width(),
                painter.fontMetrics(),
            ),
        )
        painter.restore()


def _short_reason(item: planning.PlanItem) -> str:
    """The one line beside an action's badge."""
    why = item.action.why or item.action.rationale
    label = f"{candidates.action_label(item.action.type)}: "
    return why[len(label) :] if why.startswith(label) else why


def detail_line(item: planning.PlanItem, width: int, metrics: QFontMetrics) -> str:
    """The detail column's line for one item, elided at a word boundary.

    ``item.detail`` is a sentence ("moves to D:\\… (Developer Mode) · needs
    elevation"), not a path: eliding it in the middle leaves the destination
    unreadable, so it is cut at whole words and the tooltip keeps the rest.
    """
    return models.elide_words(item.detail, width, metrics)


class PlanDetailDelegate(QStyledItemDelegate):
    """The *What happens* column: the resolved operation, elided at whole words."""

    def paint(
        self,
        painter: QPainter,
        option: QStyleOptionViewItem,
        index: QModelIndex | QPersistentModelIndex,
    ) -> None:
        item = index.data(Qt.ItemDataRole.UserRole)
        if not isinstance(item, planning.PlanItem):
            super().paint(painter, option, index)
            return
        tokens = theme.tokens()
        painter.save()
        painter.setClipRect(option.rect)
        rect = option.rect.adjusted(theme.SPACE["sm"], 0, -theme.SPACE["sm"], 0)
        painter.setFont(theme.ui_font("sm"))
        painter.setPen(QColor(tokens.faint if not item.executable else tokens.muted))
        painter.drawText(
            rect,
            int(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter),
            detail_line(item, rect.width(), painter.fontMetrics()),
        )
        painter.restore()

    def sizeHint(
        self, option: QStyleOptionViewItem, index: QModelIndex | QPersistentModelIndex
    ) -> QSize:
        return QSize(COLUMNS[COLUMN_DETAIL].width, ROW_HEIGHT)


class PlanStatusDelegate(QStyledItemDelegate):
    """The status column: a badge whose tone follows the outcome."""

    def paint(
        self,
        painter: QPainter,
        option: QStyleOptionViewItem,
        index: QModelIndex | QPersistentModelIndex,
    ) -> None:
        item = index.data(Qt.ItemDataRole.UserRole)
        if not isinstance(item, planning.PlanItem):
            super().paint(painter, option, index)
            return
        outcome = str(index.data(Qt.ItemDataRole.UserRole + 2) or "")
        tone = (
            widgets.plan_status_tone(outcome)
            if outcome
            else ("muted" if not item.executable else widgets.plan_status_tone(item.status))
        )
        text = status_text(item, outcome)
        font = theme.ui_font("xs", weight=QFont.Weight.Bold)
        painter.save()
        painter.setFont(font)
        width = painter.fontMetrics().horizontalAdvance(text) + models.BADGE_PADDING
        badge = QRect(0, 0, width, 18)
        badge.moveCenter(option.rect.center())
        models._paint_badge(painter, badge, text, tone, font=font)
        painter.restore()
