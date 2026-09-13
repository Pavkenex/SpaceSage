"""Undo -- journal history, one-click revert with verification (design §9, screen 3).

Every operation a plan executes is journaled, and this screen is where those
journals come back to life: the app's plan workspaces on the left (plus any
journal file opened from elsewhere), one row per operation on the right, and a
revert of the pending ones -- all of it off the UI thread, through the
:class:`~spacesage.app.workers.HistoryWorker` and
:class:`~spacesage.app.workers.UndoWorker` workers, with the executor verifying
every payload against the digest taken on the way in.

Nothing here reads a journal or touches the filesystem itself: the workers do
the engine's work and the widgets render what the engine returned.
"""

from __future__ import annotations

from collections.abc import Sequence
from pathlib import Path

from PySide6.QtCore import QModelIndex, Qt, Signal
from PySide6.QtWidgets import (
    QAbstractItemView,
    QDialog,
    QFileDialog,
    QFrame,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QListWidget,
    QListWidgetItem,
    QProgressBar,
    QPushButton,
    QSplitter,
    QStackedWidget,
    QTableView,
    QVBoxLayout,
    QWidget,
)

from spacesage import executor, planning, stats
from spacesage.app import dialogs, icons, state, theme, undo_models, widgets, workers
from spacesage.app.workers import BackgroundTask, HistoryWorker, UndoWorker


class UndoView(QWidget):
    """Screen 3's undo half: the journals, their operations, and verified revert."""

    statusMessage = Signal(str)
    """One-line message for the app's status bar."""

    goToPlan = Signal()
    """The empty state's primary action: the user asked for the Plan screen."""

    def __init__(self, data_root: Path | None = None, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setObjectName("Page")
        self._data_root = Path(data_root) if data_root is not None else state.data_dir()
        self._extra: list[Path] = []
        """Journals opened from elsewhere (``open_journal``); they ride along."""
        self._histories: tuple[planning.JournalHistory, ...] = ()
        self._current: planning.JournalHistory | None = None
        self._task: BackgroundTask | None = None
        self._running = False
        self._refresh_pending = False
        self._select_path: Path | None = None
        self._report: executor.UndoReport | None = None
        self._wanted: tuple[int, ...] = ()
        self._progress_done = 0
        self._build()
        self.refresh()

    # -- construction ----------------------------------------------------- #

    def _build(self) -> None:
        layout = QVBoxLayout(self)
        layout.setContentsMargins(
            theme.SPACE["xl"], theme.SPACE["xl"], theme.SPACE["xl"], theme.SPACE["lg"]
        )
        layout.setSpacing(theme.SPACE["md"])

        header = QWidget(self)
        head = QVBoxLayout(header)
        head.setContentsMargins(0, 0, 0, 0)
        head.setSpacing(theme.SPACE["xs"])
        self.title = QLabel("Undo", header)
        self.title.setObjectName("PageTitle")
        head.addWidget(self.title)
        self.subtitle = QLabel(
            "The journal history of everything SpaceSage executed: one row per operation, "
            "each one ready to be reverted with its bytes verified.",
            header,
        )
        self.subtitle.setObjectName("PageSubtitle")
        self.subtitle.setWordWrap(True)
        head.addWidget(self.subtitle)
        layout.addWidget(header)

        layout.addWidget(self._build_summary_strip())

        self.attention = QWidget(self)
        self._attention_row = QHBoxLayout(self.attention)
        self._attention_row.setContentsMargins(0, 0, 0, 0)
        self._attention_row.setSpacing(theme.SPACE["sm"])
        self.attention.hide()
        layout.addWidget(self.attention)

        self.stack = QStackedWidget(self)
        self.empty_state = widgets.EmptyState(
            "Nothing to undo yet",
            "This is where the operations a plan executed wait to be reverted, each one "
            "verified against the digest it recorded. Nothing has been executed yet: build a "
            "plan and run it, and its journal will appear here.",
            icon_name="clipboard-list",
            action="Go to Plan",
            on_action=self.goToPlan.emit,
            parent=self.stack,
        )
        empty_button = self.empty_state.findChild(QPushButton)
        if empty_button is not None:
            empty_button.setToolTip("Nothing has been executed yet: build a plan first")
        self.body = self._build_body(self.stack)
        self.stack.addWidget(self.empty_state)
        self.stack.addWidget(self.body)
        layout.addWidget(self.stack, 1)

        self.progress_row = QWidget(self)
        progress = QVBoxLayout(self.progress_row)
        progress.setContentsMargins(0, 0, 0, 0)
        progress.setSpacing(theme.SPACE["xs"])
        self.progress = QProgressBar(self.progress_row)
        self.progress.setRange(0, 1)
        self.progress.setValue(0)
        self.progress.setTextVisible(False)
        self.progress.setFixedHeight(6)
        progress.addWidget(self.progress)
        self.stage_label = QLabel("", self.progress_row)
        self.stage_label.setObjectName("Muted")
        progress.addWidget(self.stage_label)
        self.progress_row.hide()
        layout.addWidget(self.progress_row)

        layout.addLayout(self._build_footer())
        self._refresh_actions()

    def _build_summary_strip(self) -> QWidget:
        strip = QWidget(self)
        row = QHBoxLayout(strip)
        row.setContentsMargins(0, 0, 0, 0)
        row.setSpacing(theme.SPACE["sm"])
        self.cards: dict[str, widgets.MetricCard] = {}
        for key, title, icon in (
            ("operations", "Operations", "clipboard-list"),
            ("reclaimed", "Reclaimed", "hard-drive"),
            ("restored", "Restored", "arrow-right-left"),
            ("verification", "Verified", "shield-check"),
        ):
            card = widgets.MetricCard(title, "--", icon_name=icon, parent=strip)
            self.cards[key] = card
            row.addWidget(card)
        return strip

    def _build_body(self, parent: QWidget) -> QWidget:
        splitter = QSplitter(Qt.Orientation.Horizontal, parent)
        splitter.addWidget(self._build_picker(splitter))
        splitter.addWidget(self._build_table(splitter))
        splitter.setStretchFactor(0, 0)
        splitter.setStretchFactor(1, 1)
        splitter.setSizes([280, 860])
        return splitter

    def _build_picker(self, parent: QWidget) -> QWidget:
        card = QFrame(parent)
        card.setObjectName("Card")
        card.setMinimumWidth(240)
        card.setMaximumWidth(360)
        column = QVBoxLayout(card)
        column.setContentsMargins(
            theme.SPACE["md"], theme.SPACE["md"], theme.SPACE["md"], theme.SPACE["md"]
        )
        column.setSpacing(theme.SPACE["sm"])
        column.addWidget(widgets.section_label("Journals", card))
        self.picker = QListWidget(card)
        self.picker.setObjectName("UndoPicker")
        self.picker.setSelectionMode(QListWidget.SelectionMode.SingleSelection)
        self.picker.setAlternatingRowColors(False)
        self.picker.setToolTip("The app's plan workspaces, newest first")
        self.picker.currentRowChanged.connect(self._on_picker_row)
        column.addWidget(self.picker, 1)
        self.root_label = widgets.mono_label(str(self._data_root), card)
        self.root_label.setToolTip("The folder that holds the app's plan workspaces")
        column.addWidget(self.root_label)
        return card

    def _build_table(self, parent: QWidget) -> QWidget:
        page = QWidget(parent)
        column = QVBoxLayout(page)
        column.setContentsMargins(0, 0, 0, 0)
        column.setSpacing(theme.SPACE["sm"])

        self.warning_list = widgets.WarningList((), page)
        column.addWidget(self.warning_list)

        self._model = undo_models.UndoTableModel(self)
        self._model.selection_changed.connect(self._on_selection_changed)

        self._table = QTableView(page)
        self._table.setModel(self._model)
        self._table.setShowGrid(False)
        self._table.setAlternatingRowColors(False)
        self._table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self._table.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self._table.setTextElideMode(Qt.TextElideMode.ElideMiddle)
        self._table.setWordWrap(False)
        self._table.setMouseTracking(True)
        self._table.setSortingEnabled(False)
        self._table.verticalHeader().setVisible(False)
        self._table.verticalHeader().setDefaultSectionSize(undo_models.ROW_HEIGHT)
        self._table.setItemDelegateForColumn(
            undo_models.COLUMN_SELECT, undo_models.CheckDelegate(self._table)
        )
        self._table.setItemDelegateForColumn(
            undo_models.COLUMN_STATUS, undo_models.StatusDelegate(self._table)
        )
        widgets.space_toggles(self._table, self._on_space)
        header = self._table.horizontalHeader()
        header.setHighlightSections(False)
        for column_index, spec in enumerate(undo_models.COLUMNS):
            if spec.key in {"path", "operation"}:
                header.setSectionResizeMode(column_index, QHeaderView.ResizeMode.Stretch)
            else:
                header.setSectionResizeMode(column_index, QHeaderView.ResizeMode.Interactive)
                self._table.setColumnWidth(column_index, spec.width)
        column.addWidget(self._table, 1)
        return page

    def _build_footer(self) -> QHBoxLayout:
        footer = QHBoxLayout()
        footer.setSpacing(theme.SPACE["sm"])

        self.selection_label = widgets.ElidedLabel(
            "Nothing checked: nothing will be reverted",
            self,
            mode=Qt.TextElideMode.ElideRight,
            claim_width=True,
        )
        self.selection_label.setObjectName("Muted")
        footer.addWidget(self.selection_label)
        footer.addStretch(1)

        self.hint_label = QLabel(
            "A revert verifies every payload on the way back; a blocked operation stays pending.",
            self,
        )
        self.hint_label.setObjectName("Faint")
        # The footer may be squeezed: the hint wraps instead of forcing a wide page.
        self.hint_label.setWordWrap(True)
        footer.addWidget(self.hint_label)

        self.open_button = widgets.ElidedButton("Open journal file…", self)
        self.open_button.setObjectName("Quiet")
        self.open_button.setIcon(icons.tone_icon("folder-open", "accent", 14))
        self.open_button.setToolTip(
            "Open a journal file that lives outside the app's plan workspaces"
        )
        self.open_button.clicked.connect(self._browse)
        footer.addWidget(self.open_button)

        self.refresh_button = widgets.ElidedButton("Refresh", self)
        self.refresh_button.setIcon(icons.tone_icon("refresh-cw", "muted", 14))
        self.refresh_button.setToolTip("Reload the journal history")
        self.refresh_button.clicked.connect(self.refresh)
        footer.addWidget(self.refresh_button)

        self.revert_selected_button = widgets.ElidedButton("Revert selected", self)
        self.revert_selected_button.setObjectName("Danger")
        self.revert_selected_button.setToolTip(
            "Revert the checked operations and verify every payload on the way back"
        )
        self.revert_selected_button.clicked.connect(self._revert_selected)
        footer.addWidget(self.revert_selected_button)

        self.revert_all_button = widgets.ElidedButton("Revert all pending", self)
        self.revert_all_button.setObjectName("Danger")
        self.revert_all_button.setToolTip(
            "Revert every pending operation in this journal, newest first"
        )
        self.revert_all_button.clicked.connect(self._revert_all)
        footer.addWidget(self.revert_all_button)
        return footer

    # -- public API ------------------------------------------------------- #

    def set_data_root(self, root: Path) -> None:
        """Point the screen at the folder that holds the app's plan workspaces."""
        self._data_root = Path(root)
        self.root_label.setText(str(self._data_root))
        self._histories = ()
        self._current = None
        self._model.set_history(None)
        self._rebuild_picker(None)
        self.refresh()

    def refresh(self) -> None:
        """(Re)load the history in a worker thread (queued when one is running)."""
        if self._running:
            self._refresh_pending = True
            return
        self._refresh_pending = False
        worker = HistoryWorker(root=self._data_root, extra=tuple(self._extra))
        task = self._start_task(worker)
        task.finished.connect(self._on_histories)
        task.failed.connect(self._on_history_failed)

    def history_list(self) -> tuple[planning.JournalHistory, ...]:
        """Every journal currently loaded (newest workspace first)."""
        return self._histories

    def current_history(self) -> planning.JournalHistory | None:
        """The journal on screen (``None`` before the first one is loaded)."""
        return self._current

    def select_history(self, index: int) -> None:
        """Show the journal at ``index`` of :meth:`history_list` (0-based)."""
        if not 0 <= index < len(self._histories):
            return
        blocked = self.picker.blockSignals(True)
        self.picker.setCurrentRow(index)
        self.picker.blockSignals(blocked)
        self._adopt(index)

    def table_model(self) -> undo_models.UndoTableModel:
        """The Qt model behind the operations table (tests drive it directly)."""
        return self._model

    def table(self) -> QTableView:
        """The items table."""
        return self._table

    def busy(self) -> bool:
        """True while a worker runs (a journal read or a revert)."""
        return self._running

    def revert(self, seqs: Sequence[int] | None = None, *, confirm: bool = False) -> bool:
        """Revert all pending operations (``seqs=None``) or exactly those ``seqs``.

        ``confirm=True`` asks first with the itemized confirmation dialog;
        ``confirm=False`` runs directly (tests).  Returns ``True`` when the run
        was started, ``False`` when there was nothing to do, the journal could
        not be read, another run is in progress, or the confirmation was
        dismissed.
        """
        history = self._current
        if history is None:
            self.statusMessage.emit("No journal selected: open one first.")
            return False
        if history.error:
            self.statusMessage.emit(
                "This journal could not be read, so nothing was reverted. "
                "The file is still listed where it is."
            )
            return False
        if self._running:
            self.statusMessage.emit("A run is already in progress.")
            return False

        awaiting = undo_models.awaiting_items(history)
        allowed = {item.seq for item in awaiting}
        if seqs is None:
            wanted = tuple(item.seq for item in awaiting)
        else:
            wanted = tuple(dict.fromkeys(seq for seq in seqs if seq in allowed))
        if not wanted:
            self.statusMessage.emit(
                "Nothing to revert: every operation in this journal is already reversed."
            )
            return False

        by_seq = {item.seq: item for item in history.items}
        chosen = tuple(by_seq[seq] for seq in wanted)
        if confirm and not self._confirm_revert(history, chosen):
            self.statusMessage.emit("Revert cancelled: nothing was changed.")
            return False

        worker = UndoWorker(journal_path=history.path, only=list(wanted))
        task = self._start_task(worker)
        task.opDone.connect(self._on_op_done)
        task.finished.connect(self._on_undo_finished)
        task.failed.connect(self._on_undo_failed)
        self._report = None
        self._wanted = wanted
        self._progress_done = 0
        self.progress.setRange(0, len(wanted))
        self.progress.setValue(0)
        self.stage_label.setText(f"Reverting {len(wanted)} operations…")
        self.statusMessage.emit(f"Reverting {len(wanted)} operation(s)…")
        return True

    def open_journal(self, path: Path) -> None:
        """Add a journal file that lives elsewhere and show it once it is read."""
        target = Path(path)
        if target not in self._extra:
            self._extra.append(target)
        self._select_path = target
        self.statusMessage.emit(f"Opening {target.name}…")
        self.refresh()

    def apply_theme(self) -> None:
        """Re-render the hand-painted parts after a theme change."""
        self._model.layoutChanged.emit()
        self.open_button.setIcon(icons.tone_icon("folder-open", "accent", 14))
        self.refresh_button.setIcon(icons.tone_icon("refresh-cw", "muted", 14))
        self._refresh_summary()
        self._refresh_attention()
        self._refresh_actions()

    def shutdown(self) -> None:
        """Wait for a running worker, so nothing is killed mid-write."""
        task = self._task
        if task is not None and task.is_running():
            task.wait(30_000)

    # -- worker plumbing -------------------------------------------------- #

    def _start_task(self, worker: workers.Worker) -> BackgroundTask:
        """Run one worker on its own thread, with the shared busy chrome."""
        if self._task is not None and not self._task.is_running():
            previous, self._task = self._task, None
            previous.deleteLater()
        task = BackgroundTask(worker, self)
        task.stage.connect(self._on_stage)
        self._task = task
        self._running = True
        self._show_progress(True)
        self._refresh_actions()
        task.start()
        return task

    def _finish_task(self) -> None:
        """The GUI side of a finished worker: no run is in flight any more."""
        self._running = False
        self._show_progress(False)
        self._refresh_actions()

    def _maybe_reload(self) -> None:
        """A refresh requested while a worker ran starts now that it has finished."""
        if self._refresh_pending:
            self._refresh_pending = False
            self.refresh()

    def _show_progress(self, active: bool) -> None:
        self.progress_row.setVisible(active)
        if active:
            self.progress.setRange(0, 0)
            self.progress.setValue(0)
        else:
            self.progress.setRange(0, 1)
            self.progress.setValue(0)
            self.stage_label.setText("")

    def _on_stage(self, stage: str) -> None:
        self.stage_label.setText(f"{stage}…")

    def _on_histories(self, histories: object) -> None:
        """Adopt the journals a finished :class:`HistoryWorker` returned."""
        self._finish_task()
        found: tuple[planning.JournalHistory, ...] = ()
        if isinstance(histories, tuple):
            found = tuple(
                entry for entry in histories if isinstance(entry, planning.JournalHistory)
            )
        self._histories = found
        preferred = self._select_path
        if preferred is None and self._current is not None:
            preferred = self._current.path
        self._select_path = None
        self._rebuild_picker(preferred)
        if not found:
            self.statusMessage.emit("No journals yet: nothing has been executed.")
        self._maybe_reload()

    def _on_history_failed(self, message: object) -> None:
        self._finish_task()
        text = message if isinstance(message, str) else str(message)
        self.statusMessage.emit(f"Could not read the journal history: {text}")
        dialogs.report_error(self, "Could not read the journal history", text)
        self._maybe_reload()

    def _on_op_done(self, result: object) -> None:
        """Show one reversal the moment it lands (live per-item progress)."""
        if not isinstance(result, executor.UndoResult):
            return
        self._model.apply_result(result)
        self._progress_done += 1
        if self._wanted:
            self.progress.setValue(min(self._progress_done, len(self._wanted)))
        self.stage_label.setText(f"Reverting {self._progress_done} of {len(self._wanted)}…")
        detail = result.op.replace("_", " ")
        if result.outcome == "done":
            self.statusMessage.emit(f"Reverted #{result.op_ref} ({detail})")
        else:
            self.statusMessage.emit(f"#{result.op_ref} {result.outcome}: {result.reason or detail}")

    def _on_undo_finished(self, report: object) -> None:
        """Report a finished run, then reload so the table shows the journal's truth."""
        self._finish_task()
        summary: str
        tone = "info"
        if isinstance(report, executor.UndoReport):
            self._report = report
            self._model.set_results(report)
            if report.ops:
                summary = self._summarize(report)
                tone = "success" if report.ok() else "warning"
            else:
                summary = "Nothing to revert: every operation in this journal is already reversed."
        else:
            summary = "The revert finished, but it returned no report."
            tone = "warning"
        self.statusMessage.emit(summary)
        dialogs.toast(self, summary, tone=tone, above=self.revert_all_button)
        self.refresh()

    def _summarize(self, report: executor.UndoReport) -> str:
        """One line about a finished run: what happened, in the screen's words."""
        counts = report.counts()
        done = counts["done"]
        parts = [f"{done} operation{'' if done == 1 else 's'} reverted"]
        for name in ("skipped", "blocked", "failed"):
            if counts[name]:
                parts.append(f"{counts[name]} {name}")
        return " · ".join(parts) + f" · {stats.format_bytes(report.restored_bytes())} restored"

    def _on_undo_failed(self, message: object) -> None:
        self._finish_task()
        text = message if isinstance(message, str) else str(message)
        self.statusMessage.emit(f"Revert failed: {text}")
        dialogs.report_error(self, "Revert failed", text)
        self.refresh()

    # -- picker ----------------------------------------------------------- #

    def _rebuild_picker(self, preferred: Path | None) -> None:
        """Rebuild the journal list, keeping (or restoring) the shown journal."""
        blocked = self.picker.blockSignals(True)
        self.picker.clear()
        target = -1
        for index, history in enumerate(self._histories):
            item = QListWidgetItem()
            item.setData(Qt.ItemDataRole.UserRole, index)
            item.setToolTip(str(history.path))
            row = self._picker_row(history)
            item.setSizeHint(row.sizeHint())
            self.picker.addItem(item)
            self.picker.setItemWidget(item, row)
            if preferred is not None and history.path == preferred:
                target = index
        if not self._histories:
            self.picker.setCurrentRow(-1)
            self.picker.blockSignals(blocked)
            self._current = None
            self._model.set_history(None)
            self._refresh_ui()
            self._show_state()
            return
        row_index = target if target >= 0 else 0
        self.picker.setCurrentRow(row_index)
        self.picker.blockSignals(blocked)
        self._adopt(row_index)

    def _picker_row(self, history: planning.JournalHistory) -> QWidget:
        """One picker entry: the workspace token and its pending/restored summary."""
        row = QWidget(self.picker)
        column = QVBoxLayout(row)
        column.setContentsMargins(
            theme.SPACE["sm"], theme.SPACE["xs"], theme.SPACE["sm"], theme.SPACE["xs"]
        )
        column.setSpacing(0)
        name = QLabel(history.name, row)
        name.setStyleSheet("font-weight: 600;")
        column.addWidget(name)
        if history.error:
            summary = "this journal could not be read"
        else:
            awaiting = len(undo_models.awaiting_items(history))
            summary = (
                f"{awaiting} pending · {stats.format_bytes(history.restored_bytes())} restored"
            )
        column.addWidget(widgets.muted_label(summary, row))
        return row

    def _on_picker_row(self, row: int) -> None:
        if 0 <= row < len(self._histories):
            self._adopt(row)

    def _adopt(self, index: int) -> None:
        """Show one journal of the list: its rows, its warnings and its numbers."""
        self._current = self._histories[index]
        history = self._current
        self._model.set_history(history)
        self._refresh_ui()
        self._show_state()
        if history.error:
            self.statusMessage.emit(f"{history.name}: this journal could not be read")
            return
        pending = len(undo_models.awaiting_items(history))
        self.statusMessage.emit(
            f"{history.name}: {len(history.items)} operations · {pending} pending"
        )

    # -- rendering -------------------------------------------------------- #

    def _refresh_ui(self) -> None:
        self._refresh_warnings()
        self._refresh_summary()
        self._refresh_attention()
        self._refresh_actions()
        self._refresh_selection_label()

    def _refresh_warnings(self) -> None:
        """A journal that failed to parse is shown, never hidden or crashed on."""
        history = self._current
        if history is None or not history.error:
            self.warning_list.set_warnings(())
            return
        self.warning_list.set_warnings(
            (
                planning.PlanWarning(
                    severity="blocker",
                    message=f"This journal could not be read: {history.error}",
                    paths=(str(history.path),),
                ),
            )
        )

    def _refresh_summary(self) -> None:
        history = self._current
        if history is None:
            for card in self.cards.values():
                card.set_value("--", "no journal selected")
            return
        counts = history.counts()
        caption = f"{counts['pending']} pending · {counts['reversed']} reversed"
        for name in ("blocked", "failed", "interrupted"):
            if counts[name]:
                caption += f" · {counts[name]} {name}"
        self.cards["operations"].set_value(f"{len(history.items):,}", caption)
        self.cards["reclaimed"].set_value(
            stats.format_bytes(history.reclaimed_bytes()),
            "moved by this journal's operations",
        )
        self.cards["restored"].set_value(
            stats.format_bytes(history.restored_bytes()),
            "already back in place",
        )
        verified = sum(1 for item in history.items if item.verify == "verified")
        mismatched = sum(1 for item in history.items if item.verify == "mismatch")
        if mismatched == 1:
            verify_caption = "1 MISMATCH — a payload came back different"
        elif mismatched:
            verify_caption = f"{mismatched} MISMATCH — payloads came back different"
        else:
            verify_caption = "every payload compared with its digest"
        self.cards["verification"].set_value(f"{verified}", verify_caption)

    def _refresh_attention(self) -> None:
        """Blocked, failed and interrupted operations get their own stated row."""
        while self._attention_row.count():
            entry = self._attention_row.takeAt(0)
            widget = entry.widget() if entry is not None else None
            if widget is not None:
                widget.deleteLater()
        history = self._current
        if history is None:
            self.attention.hide()
            return
        counts = history.counts()
        notes = (
            ("failed", "failed; the payload stays where it is"),
            ("blocked", "blocked: the original path is occupied again, so they stay pending"),
            ("interrupted", "interrupted: the next revert settles them against the filesystem"),
        )
        added = False
        for status, tail in notes:
            count = counts.get(status, 0)
            if not count:
                continue
            noun = "operation" if count == 1 else "operations"
            badge = widgets.StatusBadge(status, self.attention)
            badge.set_status(status, undo=True)
            badge.setToolTip(f"{count} {noun} {status}")
            self._attention_row.addWidget(badge)
            self._attention_row.addWidget(
                widgets.muted_label(f"{count} {noun} {tail}", self.attention)
            )
            added = True
        if added:
            self._attention_row.addStretch(1)
        self.attention.setVisible(added)

    def _refresh_actions(self) -> None:
        history = self._current
        busy = self._running
        readable = history is not None and not history.error
        checked = bool(self._model.checked_seqs())
        pending = self._model.pending_seqs()

        self.refresh_button.setEnabled(not busy)
        self.refresh_button.setToolTip(
            "A run is in progress…" if busy else "Reload the journal history"
        )
        self.open_button.setEnabled(not busy)

        self.revert_selected_button.setEnabled(checked and readable and not busy)
        if checked and readable and not busy:
            selected_tip = "Revert the checked operations and verify every payload on the way back"
        elif not readable:
            selected_tip = "No journal is selected"
        else:
            selected_tip = "Check at least one pending operation first"
        self.revert_selected_button.setToolTip(selected_tip)

        self.revert_all_button.setEnabled(bool(pending) and readable and not busy)
        if pending and readable and not busy:
            all_tip = f"Revert all {len(pending)} pending operations, newest first, verifying each"
        elif busy:
            all_tip = "A run is in progress…"
        elif not readable:
            all_tip = "No journal is selected"
        else:
            all_tip = "Nothing is pending: every operation is already reversed"
        self.revert_all_button.setToolTip(all_tip)

    def _refresh_selection_label(self) -> None:
        self.selection_label.setText(self._model.checked_summary())

    def _show_state(self) -> None:
        self.stack.setCurrentWidget(self.empty_state if not self._histories else self.body)

    # -- actions ---------------------------------------------------------- #

    def _on_selection_changed(self) -> None:
        self._refresh_actions()
        self._refresh_selection_label()
        self.statusMessage.emit(self._model.checked_summary())

    def _on_space(self, index: QModelIndex) -> bool:
        """Space checks or unchecks the operation under the cursor (keyboard path)."""
        item = self._model.row_at(index) if index.isValid() else None
        if item is None:
            return False
        checked = item.seq in self._model.checked_seqs()
        if not self._model.set_checked(item.seq, not checked):
            self.statusMessage.emit(
                f"Operation {item.seq} cannot be reverted: {self._model.status_of(item)}"
            )
            return False
        self._on_selection_changed()
        return True

    def _revert_selected(self) -> None:
        self.revert(self._model.checked_seqs(), confirm=True)

    def _revert_all(self) -> None:
        self.revert(None, confirm=True)

    def _browse(self) -> None:
        chosen, _ = QFileDialog.getOpenFileName(
            self,
            "Open a journal file",
            str(self._data_root),
            "SpaceSage journal (*.jsonl);;All files (*)",
        )
        if chosen:
            self.open_journal(Path(chosen))

    def _confirm_revert(
        self, history: planning.JournalHistory, chosen: Sequence[planning.JournalItem]
    ) -> bool:
        """Ask before a revert, listing exactly which operations it will reverse."""
        count = len(chosen)
        noun = "operation" if count == 1 else "operations"
        lines = [
            f"{undo_models.operation_label(item)} · {stats.format_bytes(item.bytes)} · {item.path}"
            for item in chosen
        ]
        dialog = dialogs.ConfirmDialog(
            "Revert operations",
            f"Revert {count} {noun} from {history.name}?",
            lines=lines,
            detail=(
                "Every payload is verified against the digest taken when the operation ran; an "
                "operation whose original path is occupied again is blocked, never overwritten."
            ),
            accept_label="Revert",
            danger=True,
            parent=self,
        )
        return dialog.exec() == QDialog.DialogCode.Accepted
