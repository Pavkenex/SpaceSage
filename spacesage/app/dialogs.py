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

from PySide6.QtCore import QTimer
from PySide6.QtGui import QShowEvent
from PySide6.QtWidgets import (
    QCheckBox,
    QDialog,
    QDialogButtonBox,
    QHBoxLayout,
    QLabel,
    QMessageBox,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from spacesage import executor, stats
from spacesage.app import theme, widgets


class LogDialog(QDialog):
    """A dialog whose itemized listing is fitted to what it has to show.

    Both the confirmation and the dry-run preview list paths, and a path is
    wider than any dialog that still looks like a dialog.  The shared behaviour
    is here: the panel wraps instead of clipping, the dialog opens wide enough
    that one operation reads as one line, and it grows until the listing fits --
    capped by the window it belongs to, so it never becomes a window of its own.
    """

    #: A sanity ceiling for the listing (the parent window's own height rules).
    MAX_LOG_HEIGHT = 900
    #: The widest the dialog will ask for, however long a path is.
    MAX_WIDTH = 1180
    #: The narrowest a listing dialog may become.
    MIN_WIDTH = 560

    def _bind_log(self, panel: widgets.LogPanel) -> None:
        """Register the listing panel: it is what the dialog sizes itself around."""
        self._log = panel
        self._log.setMinimumHeight(self._log_height())
        self.setMinimumWidth(max(self.minimumWidth(), self._fit_width()))

    # -- sizing ---------------------------------------------------------- #

    def _height_cap(self) -> int:
        """As tall as the window it opens in allows, never more than ``MAX_LOG_HEIGHT``."""
        limit = self.MAX_LOG_HEIGHT
        owner = self.parentWidget()
        if owner is not None:
            limit = min(limit, max(240, owner.height() - self._chrome() - theme.SPACE["md"]))
        else:
            screen = self.screen()
            if screen is not None:
                limit = min(limit, max(240, screen.availableGeometry().height() - 220))
        return limit

    def _chrome(self) -> int:
        """Everything in the dialog that is not the listing (measured, not guessed)."""
        return max(self.height() - self._log.height(), 0)

    def _fit_width(self) -> int:
        """Widen the dialog so a resolved path does not have to wrap."""
        metrics = self._log.fontMetrics()
        longest = max((metrics.horizontalAdvance(line) for line in self._log.lines()), default=0)
        chrome = 2 * theme.SPACE["xl"] + 2 * theme.SPACE["sm"] + theme.SPACE["md"]
        limit = self.MAX_WIDTH
        owner = self.parentWidget()
        if owner is not None:
            limit = min(limit, max(self.MIN_WIDTH, owner.width() - 2 * theme.SPACE["md"]))
        return min(limit, max(self.MIN_WIDTH, longest + chrome))

    def _log_height(self) -> int:
        """The panel height that shows every line without scrolling (capped)."""
        metrics = self._log.fontMetrics()
        wanted = metrics.lineSpacing() * (len(self._log.lines()) + 1) + 2 * theme.SPACE["sm"] + 4
        return min(self._height_cap(), wanted)

    def showEvent(self, event: QShowEvent) -> None:
        """Open at the size that shows what the dialog is about to do."""
        super().showEvent(event)
        self._fit_to_content()
        # The wrap points of long paths are only known once the panel has its
        # real width, so the fit is corrected after the first layout pass.
        QTimer.singleShot(0, self, self._fit_to_content)

    def _fit_to_content(self) -> None:
        """Grow until the listing fits -- or until the window runs out of height."""
        layout = self.layout()
        if layout is None:  # pragma: no cover - both dialogs build their layout
            return
        for _ in range(4):
            layout.activate()
            # The panel's scrollbar counts *lines*, not pixels: how many rows it
            # still has to hide times the row height is what the dialog lacks.
            hidden = self._log.verticalScrollBar().maximum()
            if hidden <= 0:
                return
            room = self._height_cap() - self._log.minimumHeight()
            if room <= 0:
                return
            step = min(hidden * self._log.fontMetrics().lineSpacing() + 1, room)
            self._log.setMinimumHeight(self._log.minimumHeight() + step)
            self.resize(self.width(), self.height() + step)


class ConfirmDialog(LogDialog):
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
        self.setMinimumWidth(self.MIN_WIDTH)
        self._acknowledge_box: QCheckBox | None = None
        self._listing: widgets.LogPanel | None = None

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
            self._listing = widgets.LogPanel(lines, self)
            layout.addWidget(self._listing)
            self._bind_log(self._listing)

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

    def lines(self) -> list[str]:
        """The itemized lines as they render (empty when the dialog lists nothing)."""
        return self._listing.lines() if self._listing is not None else []

    def acknowledge_box(self) -> QCheckBox | None:
        """The required acknowledgement, when the dialog has one."""
        return self._acknowledge_box


class PreviewDialog(LogDialog):
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

        self.list = widgets.LogPanel(_op_lines(report.ops), self)
        layout.addWidget(self.list, 1)
        self._bind_log(self.list)

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
        return self.list.lines()


def _op_lines(ops: Sequence[executor.OpResult]) -> list[str]:
    """The itemized preview: one block per action, a blank line between them.

    Nothing is elided and nothing is on a second axis: each path gets a line of
    its own, because a preview that hides a destination is not "exactly what
    would happen" (design §9, screen 3).
    """
    lines: list[str] = []
    for index, op in enumerate(ops):
        if index:
            lines.append("")
        lines.extend(_one_op(op))
    return lines


def _one_op(op: executor.OpResult) -> list[str]:
    """The block one resolved action takes in the preview."""
    size = f" · {stats.format_bytes(op.bytes)}" if op.bytes else ""
    advice = "  (advice -- nothing runs)" if op.advisory else ""
    refused = f"  (refused: {op.reason})" if op.outcome == "refused" and op.reason else ""
    lines = [f"{op.action_id}   {op.type}{size}{advice}{refused}", f"    {op.path}"]
    for step in op.steps:
        lines.append(f"    {step.op} · {step.outcome}")
        lines.append(f"        {step.src}")
        if step.dest is not None:
            lines.append(f"        ->  {step.dest}")
        if step.link:
            lines.append(f"        then a {executor.link_label(step.link)} at the original path")
        if step.reason:
            lines.append(f"        {step.reason}")
    if not op.steps:
        lines.append(f"    {op.outcome}: {op.reason}")
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


def toast(
    parent: QWidget | None,
    message: str,
    *,
    tone: str = "info",
    above: QWidget | None = None,
) -> widgets.Toast | None:
    """Pop a toast over the window that owns ``parent`` (above ``above``, if given)."""
    if parent is None:
        return None
    window = parent.window()
    target = window if isinstance(window, QWidget) else parent
    return widgets.Toast.pop_up(target, message, tone=tone, above=above)
