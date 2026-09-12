"""The dialogs the plan and execute flow goes through (design §9, §9.1).

Three of them, each with one job:

:class:`ConfirmDialog`
    The one confirmation dialog the app uses, so "are you sure" always looks
    and behaves the same.  It lists exactly what will happen (the itemized
    actions), states what cannot be taken back, and carries the danger styling
    *only* when the action is genuinely destructive -- executing a plan and
    reverting one are; approving a plan is not.
:class:`PreviewDialog`
    The dry-run preview: every resolved operation, in order, with the
    destination it would use and the reason a refusal stops it.  Nothing here
    runs anything -- it renders an :class:`~spacesage.executor.ApplyReport` the
    caller already produced with ``execute=False``.
:func:`report_error`
    One error surface for the whole app (a real dialog with the engine's own
    message, never a traceback in a label).
"""

from __future__ import annotations

from collections.abc import Sequence

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QCheckBox,
    QDialog,
    QDialogButtonBox,
    QHBoxLayout,
    QLabel,
    QListWidget,
    QListWidgetItem,
    QMessageBox,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from spacesage import executor, stats
from spacesage.app import theme, widgets


class ConfirmDialog(QDialog):
    """Ask before something irreversible, listing exactly what will happen.

    ``lines`` is the itemized list (one line per action, already worded by the
    caller); ``danger`` paints the confirm button in the danger tone and is only
    set for the genuinely destructive choices.  ``acknowledge`` adds a checkbox
    the user has to tick, for the runs where a single click is too cheap.
    """

    def __init__(
        self,
        title: str,
        headline: str,
        *,
        lines: Sequence[str] = (),
        detail: str = "",
        accept_label: str = "Confirm",
        reject_label: str = "Cancel",
        danger: bool = False,
        acknowledge: str | None = None,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self.setObjectName("ConfirmDialog")
        self.setWindowTitle(title)
        self.setModal(True)
        self.setMinimumWidth(560)
        self._acknowledge_box: QCheckBox | None = None

        layout = QVBoxLayout(self)
        layout.setContentsMargins(
            theme.SPACE["xl"], theme.SPACE["lg"], theme.SPACE["xl"], theme.SPACE["lg"]
        )
        layout.setSpacing(theme.SPACE["md"])

        head = QLabel(headline, self)
        head.setObjectName("PageTitle")
        head.setWordWrap(True)
        layout.addWidget(head)

        if detail:
            explanation = QLabel(detail, self)
            explanation.setObjectName("Muted")
            explanation.setWordWrap(True)
            layout.addWidget(explanation)

        if lines:
            listing = QListWidget(self)
            listing.setObjectName("ConfirmList")
            listing.setSelectionMode(QListWidget.SelectionMode.NoSelection)
            listing.setFocusPolicy(Qt.FocusPolicy.NoFocus)
            listing.setAlternatingRowColors(False)
            for line in lines:
                item = QListWidgetItem(line)
                item.setToolTip(line)
                listing.addItem(item)
            listing.setMinimumHeight(min(280, 28 + 24 * len(lines)))
            layout.addWidget(listing)

        if acknowledge is not None:
            box = QCheckBox(acknowledge, self)
            box.setToolTip("Required before the confirm button turns on")
            box.toggled.connect(self._on_acknowledge)
            self._acknowledge_box = box
            layout.addWidget(box)

        buttons = QDialogButtonBox(self)
        self._accept = QPushButton(accept_label, self)
        self._accept.setObjectName("Danger" if danger else "Primary")
        self._accept.setDefault(True)
        self._accept.setToolTip(accept_label)
        cancel = QPushButton(reject_label, self)
        cancel.setToolTip("Leave everything as it is")
        buttons.addButton(self._accept, QDialogButtonBox.ButtonRole.AcceptRole)
        buttons.addButton(cancel, QDialogButtonBox.ButtonRole.RejectRole)
        buttons.rejected.connect(self.reject)
        buttons.accepted.connect(self.accept)
        layout.addWidget(buttons)
        if self._acknowledge_box is not None:
            self._on_acknowledge(False)

    def _on_acknowledge(self, checked: bool) -> None:
        self._accept.setEnabled(checked)

    def confirm_button(self) -> QPushButton:
        """The button that accepts (tests drive it)."""
        return self._accept

    def acknowledge_box(self) -> QCheckBox | None:
        """The required acknowledgement, when the dialog has one."""
        return self._acknowledge_box


class PreviewDialog(QDialog):
    """The dry-run preview: exactly what the approved actions would do."""

    def __init__(
        self,
        report: executor.ApplyReport,
        *,
        title: str = "Dry-run preview",
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self.setObjectName("PreviewDialog")
        self.setWindowTitle(title)
        self.setModal(True)
        self.setMinimumSize(760, 520)
        self._report = report

        layout = QVBoxLayout(self)
        layout.setContentsMargins(
            theme.SPACE["xl"], theme.SPACE["lg"], theme.SPACE["xl"], theme.SPACE["lg"]
        )
        layout.setSpacing(theme.SPACE["md"])

        head = QLabel("Dry run: this is exactly what would happen", self)
        head.setObjectName("PageTitle")
        head.setWordWrap(True)
        layout.addWidget(head)

        counts = report.counts()
        summary = QLabel(
            f"{len(report.ops)} approved of {report.total_actions} actions · "
            f"{stats.format_bytes(report.reclaimed_bytes())} to reclaim · "
            f"{counts['refused']} refused · nothing on disk is touched by a dry run.",
            self,
        )
        summary.setObjectName("Muted")
        summary.setWordWrap(True)
        layout.addWidget(summary)

        self.list = QListWidget(self)
        self.list.setObjectName("PreviewList")
        self.list.setSelectionMode(QListWidget.SelectionMode.NoSelection)
        self.list.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        for op in report.ops:
            for line in _op_lines(op):
                item = QListWidgetItem(line)
                item.setToolTip(line)
                if op.outcome == "refused":
                    item.setForeground(Qt.GlobalColor.red)
                self.list.addItem(item)
        layout.addWidget(self.list, 1)

        footer = QHBoxLayout()
        footer.setSpacing(theme.SPACE["sm"])
        note = QLabel(
            "Refusals are decisions the engine makes: report-only tiers, protected paths, "
            "destinations outside the plan's target drives.",
            self,
        )
        note.setObjectName("Faint")
        note.setWordWrap(True)
        footer.addWidget(note, 1)
        close = QPushButton("Close", self)
        close.setObjectName("Primary")
        close.setToolTip("Close the preview (nothing has happened)")
        close.clicked.connect(self.accept)
        footer.addWidget(close)
        layout.addLayout(footer)

    def report(self) -> executor.ApplyReport:
        """The preview this dialog renders."""
        return self._report

    def lines(self) -> list[str]:
        """Every rendered line (tests read them)."""
        return [self.list.item(index).text() for index in range(self.list.count())]


def _op_lines(op: executor.OpResult) -> list[str]:
    """The two or three lines one resolved action takes in the preview."""
    size = f"{stats.format_bytes(op.bytes)} · " if op.bytes else ""
    advice = "  (advice -- nothing runs)" if op.advisory else ""
    lines = [f"{op.action_id}   {op.type}   {size}{op.path}{advice}"]
    for step in op.steps:
        where = step.src if step.dest is None else f"{step.src}  ->  {step.dest}"
        link = (
            f", then a {executor.link_label(step.link)} at the original path" if step.link else ""
        )
        lines.append(f"      {step.op} · {step.outcome} · {where}{link}")
        if step.reason:
            lines.append(f"        {step.reason}")
    if not op.steps:
        lines.append(f"      {op.outcome}: {op.reason}")
    return lines


def report_error(parent: QWidget | None, title: str, message: str) -> None:
    """Show the engine's message in the app's error dialog (never a traceback)."""
    box = QMessageBox(parent)
    box.setIcon(QMessageBox.Icon.Critical)
    box.setWindowTitle(title)
    box.setText(title)
    box.setInformativeText(message)
    box.setStandardButtons(QMessageBox.StandardButton.Ok)
    box.exec()


def report_note(parent: QWidget | None, title: str, message: str) -> None:
    """An informational dialog for a result the user asked to see."""
    box = QMessageBox(parent)
    box.setIcon(QMessageBox.Icon.Information)
    box.setWindowTitle(title)
    box.setText(title)
    box.setInformativeText(message)
    box.setStandardButtons(QMessageBox.StandardButton.Ok)
    box.exec()


def toast(parent: QWidget | None, message: str, *, tone: str = "info") -> widgets.Toast | None:
    """Pop a toast over the window that owns ``parent``."""
    if parent is None:
        return None
    window = parent.window()
    target = window if isinstance(window, QWidget) else parent
    return widgets.Toast.pop_up(target, message, tone=tone)
