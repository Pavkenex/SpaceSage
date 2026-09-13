"""Opportunities -- the core screen (design §9, screen 2).

One table of files and folders, biggest estimated gain first, every row with
the rule engine's suggested solution; a filter bar over size/category/tier/state
plus search and bulk select; a summary strip; and the details pane for the
selected row.  Every number comes from :mod:`spacesage.opportunities` -- the
widgets never touch SQL.

The AI bar under the filter bar is this screen's second source (design §10):
*Generate AI suggestions* fills the suggested-solution column for the rows the
rules left undecided, batch by batch, after an estimate the user approves; the
row menu and the details pane ask about one row; and nothing any of it produces
can reach a plan except through *Apply as rule…*, which writes a rule and re-ranks
the list (never an action).  The calls run in
:mod:`spacesage.app.workers`; this screen only routes their signals.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any

from PySide6.QtCore import QModelIndex, QPoint, Qt, Signal
from PySide6.QtWidgets import (
    QAbstractItemView,
    QComboBox,
    QDialog,
    QFrame,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QMenu,
    QProgressBar,
    QPushButton,
    QSplitter,
    QTableView,
    QVBoxLayout,
    QWidget,
)

from spacesage import opportunities, rules, stats
from spacesage.ai import BatchPlan
from spacesage.app import ai_models, dialogs, icons, models, theme, widgets
from spacesage.app.ai_models import AIService, AISuggestion
from spacesage.app.views.details_pane import AI_CLASSIFY, AI_EXPLAIN, AI_SUGGEST, DetailsPane
from spacesage.app.workers import BackgroundTask, ExplainWorker, SuggestWorker, Worker

MIN_SIZE_CHOICES: tuple[str, ...] = ("Any size", "10 MiB", "50 MiB", "100 MiB", "250 MiB", "1 GiB")

STATE_CHOICES: tuple[tuple[str, str], ...] = (
    ("All states", ""),
    ("Has action", opportunities.STATE_ACTION),
    ("No action", opportunities.STATE_NO_ACTION),
    ("Undecided", opportunities.STATE_UNDECIDED),
)


def drive_label(volume: str) -> str:
    """Display form of a volume key: the engine's ``d:`` reads as ``D:`` in the list."""
    head, separator, tail = volume.partition(":")
    return f"{head.upper()}{separator}{tail}" if separator else volume


class OpportunitiesView(QWidget):
    """Screen 2: the ranked list, its filters, its summary and its details."""

    statusMessage = Signal(str)
    rowSelected = Signal(object)
    destinationEdited = Signal(str, str)
    """``(row key, destination text)`` -- S9 builds plans from these."""

    buildPlanRequested = Signal(object)
    """The paths of the checked rows: *Build plan* was pressed (S9, design §9)."""

    aiChanged = Signal()
    """An AI call finished: the status bar re-reads the meter (design §10)."""

    reanalysisRequested = Signal()
    """A rule was written: rank the index again so it counts from now on."""

    def __init__(self, ai: AIService, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setObjectName("Page")
        self._target_drive = ""
        self._index: opportunities.OpportunityList | None = None
        self._destination_overrides: dict[str, str] = {}
        self._ai = ai
        self._ai_task: BackgroundTask | None = None
        self._ai_kind = ""
        self._build()
        self._ai.store.changed.connect(lambda _keys: self._refresh_ai_bar())
        self.details.set_ai_store(self._ai.store)

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
        self.title = QLabel("Opportunities", header)
        self.title.setObjectName("PageTitle")
        head.addWidget(self.title)
        self.subtitle = QLabel(
            "Everything SpaceSage suggests, ranked by the bytes it frees. "
            '"No action" rows are listed too: they are decisions, not gaps.',
            header,
        )
        self.subtitle.setObjectName("PageSubtitle")
        self.subtitle.setWordWrap(True)
        head.addWidget(self.subtitle)
        layout.addWidget(header)

        layout.addWidget(self._build_summary_strip())
        layout.addWidget(self._build_filter_bar())
        layout.addWidget(self._build_ai_bar())

        splitter = QSplitter(Qt.Orientation.Horizontal, self)
        splitter.addWidget(self._build_table())
        self.details = DetailsPane(splitter)
        self.details.destinationEdited.connect(self._on_destination_edited)
        self.details.aiRequested.connect(self._on_ai_requested)
        self.details.promoteRequested.connect(self.apply_rule)
        splitter.addWidget(self.details)
        splitter.setStretchFactor(0, 3)
        splitter.setStretchFactor(1, 2)
        # The ranked list is the product: it keeps the larger share of the width,
        # the details pane stays readable at its minimum.
        splitter.setSizes([840, 360])
        layout.addWidget(splitter, 1)

        # The figure, the hint and "Build plan" need ~751px at the shell's 980px
        # minimum: the row wraps the button onto a second line rather than eliding
        # the hint (t_af23bb34).  The flow packs from the left.
        footer = widgets.FlowLayout(h_spacing=theme.SPACE["sm"], v_spacing=theme.SPACE["xs"])
        self.selection_label = widgets.ElidedLabel(
            "No rows checked yet", self, mode=Qt.TextElideMode.ElideRight, claim_width=True
        )
        self.selection_label.setObjectName("Muted")
        footer.addWidget(self.selection_label)
        self.cascade_hint = widgets.ElidedLabel(
            "Selecting a folder covers its contents: every byte is counted once.",
            self,
            mode=Qt.TextElideMode.ElideRight,
            claim_width=True,
        )
        self.cascade_hint.setObjectName("Faint")
        footer.addWidget(self.cascade_hint)
        self.build_plan_button = QPushButton("Build plan", self)
        self.build_plan_button.setObjectName("Primary")
        self.build_plan_button.setToolTip(
            "Turn the checked rows into one plan: per-item approval, a dry-run preview, "
            "then a run you can undo"
        )
        self.build_plan_button.setEnabled(False)
        self.build_plan_button.clicked.connect(self.request_plan)
        footer.addWidget(self.build_plan_button)
        layout.addLayout(footer)

    def _build_summary_strip(self) -> QWidget:
        strip = QWidget(self)
        # Six cards need ~837px; the shell allows a 980px window, so they wrap onto
        # a second line instead of squeezing every title into an ellipsis
        # (t_af23bb34).
        row = widgets.FlowLayout(h_spacing=theme.SPACE["sm"], v_spacing=theme.SPACE["sm"])
        strip.setLayout(row)
        self.cards: dict[str, widgets.MetricCard] = {}
        for key, title, icon in (
            ("rows", "Opportunities", "list-ordered"),
            ("gain", "Estimated gain", "hard-drive"),
            ("action", "Has action", "check"),
            ("no_action", "No action", "shield-check"),
            ("undecided", "Undecided", "help-circle"),
            ("volumes", "Drives", "folder"),
        ):
            card = widgets.MetricCard(title, "--", icon_name=icon, parent=strip)
            self.cards[key] = card
            row.addWidget(card)
        return strip

    def _build_filter_bar(self) -> QWidget:
        bar = QFrame(self)
        bar.setObjectName("Card")
        # The bar wraps when narrow (the search keeps its stretch on the line it
        # lands on); at the shell's 980px minimum the two buttons take a second
        # line instead of the search being squeezed to ~46px (t_af23bb34).
        row = widgets.FlowLayout(h_spacing=theme.SPACE["sm"], v_spacing=theme.SPACE["xs"])
        bar.setLayout(row)
        row.setContentsMargins(
            theme.SPACE["md"], theme.SPACE["sm"], theme.SPACE["md"], theme.SPACE["sm"]
        )

        self.search = QLineEdit(bar)
        self.search.setPlaceholderText("Search paths, solutions and reasons")
        self.search.setClearButtonEnabled(True)
        self.search.setToolTip("Search paths, solutions, categories and reasons")
        self.search.textChanged.connect(self._on_filter_changed)
        row.addWidget(self.search, 2)

        self.state_combo = QComboBox(bar)
        for label, value in STATE_CHOICES:
            self.state_combo.addItem(label, value)
        self.state_combo.setToolTip("Has action / No action / Undecided")
        self.state_combo.currentIndexChanged.connect(self._on_filter_changed)
        row.addWidget(self.state_combo)

        self.tier_combo = QComboBox(bar)
        self.tier_combo.setToolTip("Risk tier of the row")
        self.tier_combo.currentIndexChanged.connect(self._on_filter_changed)
        row.addWidget(self.tier_combo)

        self.category_combo = QComboBox(bar)
        self.category_combo.setMinimumWidth(150)
        self.category_combo.setToolTip("Rule category")
        self.category_combo.currentIndexChanged.connect(self._on_filter_changed)
        row.addWidget(self.category_combo)

        self.size_combo = QComboBox(bar)
        self.size_combo.addItems(MIN_SIZE_CHOICES)
        self.size_combo.setToolTip("Smallest entry listed")
        self.size_combo.currentIndexChanged.connect(self._on_filter_changed)
        row.addWidget(self.size_combo)

        self.select_button = QPushButton("Select visible", bar)
        self.select_button.setToolTip("Check every visible row (folders cover their contents)")
        self.select_button.clicked.connect(lambda: self._model.select_all_visible())
        row.addWidget(self.select_button)

        self.clear_button = QPushButton("Clear", bar)
        self.clear_button.setToolTip("Uncheck everything")
        self.clear_button.clicked.connect(lambda: self._model.clear_selection())
        row.addWidget(self.clear_button)
        return bar

    def _build_table(self) -> QWidget:
        self._model = models.OpportunityTableModel(self)
        # The column paints AI answers next to the rules' verdicts (design §10);
        # the store is the model's only source for them, so it is told once here.
        self._model.set_ai_store(self._ai.store)
        self._model.selectionChanged.connect(self._on_selection_changed)

        self.table = QTableView(self)
        self.table.setModel(self._model)
        self.table.setShowGrid(False)
        self.table.setAlternatingRowColors(False)
        self.table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.table.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.table.setTextElideMode(Qt.TextElideMode.ElideMiddle)
        self.table.setWordWrap(False)
        self.table.setMouseTracking(True)
        self.table.setSortingEnabled(True)
        self.table.verticalHeader().setVisible(False)
        self.table.verticalHeader().setDefaultSectionSize(models.ROW_HEIGHT)
        self.table.setItemDelegateForColumn(models.COLUMN_SELECT, models.CheckDelegate(self.table))
        self.table.setItemDelegateForColumn(
            models.COLUMN_SOLUTION, models.SolutionDelegate(self.table)
        )
        self.table.setItemDelegateForColumn(models.COLUMN_TIER, models.TierDelegate(self.table))
        self.table.setItemDelegateForColumn(
            models.COLUMN_CONFIDENCE, models.ConfidenceDelegate(self.table)
        )
        header = self.table.horizontalHeader()
        header.setHighlightSections(False)
        header.setSortIndicator(models.COLUMN_GAIN, Qt.SortOrder.DescendingOrder)
        header.setSortIndicatorShown(True)
        for column, spec in enumerate(models.COLUMNS):
            if spec.key in {"path", "solution"}:
                header.setSectionResizeMode(column, QHeaderView.ResizeMode.Stretch)
            else:
                header.setSectionResizeMode(column, QHeaderView.ResizeMode.Interactive)
                self.table.setColumnWidth(column, spec.width)
        # The numeric cells read as labels ("up to 1023.9 PiB"): size them so a
        # value is never elided into a number nobody can read (design §9.1).
        for column, sample in (
            (models.COLUMN_SIZE, "1023.9 GiB"),
            (models.COLUMN_GAIN, "up to 1023.9 PiB"),
        ):
            self.table.setColumnWidth(
                column, max(self.table.columnWidth(column), models.fitted_width(sample))
            )
        self.table.selectionModel().currentRowChanged.connect(self._on_current_row_changed)
        self.table.doubleClicked.connect(self._on_double_clicked)
        self.table.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.table.customContextMenuRequested.connect(self._on_row_menu)
        widgets.space_toggles(self.table, self._on_space)
        return self.table

    def _build_ai_bar(self) -> QWidget:
        """The AI row: what the layer is, the batch action, its progress.

        It is never hidden -- an off AI has to *look* off, with the reason one
        hover away -- and the progress bar only appears while a fill runs, so the
        row is a single line in the common case (design §9.1).
        """
        bar = QFrame(self)
        bar.setObjectName("Card")
        self.ai_bar = bar
        row = QHBoxLayout(bar)
        row.setContentsMargins(
            theme.SPACE["md"], theme.SPACE["sm"], theme.SPACE["md"], theme.SPACE["sm"]
        )
        row.setSpacing(theme.SPACE["sm"])

        glyph = QLabel(bar)
        glyph.setPixmap(icons.tone_icon("sparkles", "info", 14).pixmap(14, 14))
        row.addWidget(glyph)

        self.ai_status = widgets.Badge("", "muted", bar)
        self.ai_status.setObjectName("AiStatus")
        row.addWidget(self.ai_status)

        self.ai_fill_button = QPushButton("Generate AI suggestions", bar)
        self.ai_fill_button.setObjectName("AiFill")
        self.ai_fill_button.setIcon(icons.icon("sparkles", theme.tokens().accent_text, 14))
        self.ai_fill_button.clicked.connect(self.request_suggestions)
        row.addWidget(self.ai_fill_button)

        self.ai_cancel_button = QPushButton("Cancel", bar)
        self.ai_cancel_button.setObjectName("AiCancel")
        self.ai_cancel_button.setToolTip("Stop after the batch in flight; answers so far are kept")
        self.ai_cancel_button.clicked.connect(self.cancel_ai)
        self.ai_cancel_button.setVisible(False)
        row.addWidget(self.ai_cancel_button)

        self.ai_stage = widgets.ElidedLabel("", bar, mode=Qt.TextElideMode.ElideRight)
        self.ai_stage.setObjectName("Faint")
        row.addWidget(self.ai_stage, 1)

        self.ai_progress = QProgressBar(bar)
        self.ai_progress.setObjectName("AiProgress")
        self.ai_progress.setTextVisible(False)
        self.ai_progress.setFixedWidth(160)
        self.ai_progress.setVisible(False)
        row.addWidget(self.ai_progress)
        self._refresh_ai_bar()
        return bar

    # -- data ------------------------------------------------------------- #

    def set_listing(self, listing: opportunities.OpportunityList | None) -> None:
        """Adopt a finished analysis (rows, summary strip, filter options).

        Answers for rows the rules now decide are dropped: the list shows one
        source per cell, and a rule outranks a suggestion (design §10).
        """
        self._index = listing
        rows = listing.rows if listing is not None else ()
        self._model.set_rows(tuple(rows))
        self._refresh_options()
        self._refresh_summary()
        self.details.clear()
        self._on_selection_changed()
        self._prune_ai()
        self._refresh_ai_bar()
        if listing is not None:
            self.subtitle.setText(f"{listing.heading()} · target {self._target_drive or 'unset'}")

    def _prune_ai(self) -> None:
        """Forget answers whose rows the rule engine has decided in the meantime."""
        store = self._ai.store
        if not len(store):
            return
        if self._index is None:
            store.clear()
            return
        decided = {
            row.key for row in self._index.rows if row.state != opportunities.STATE_UNDECIDED
        }
        # The rules own those rows now: their AI answers leave the store.
        store.drop(tuple(decided & set(store.keys())))

    def shutdown(self) -> None:
        """Stop asking and wait for the worker (window close, tests)."""
        task = self._ai_task
        if task is None:
            return
        worker = task.worker
        if isinstance(worker, SuggestWorker):
            worker.cancel()
        if task.is_running():
            task.wait(30_000)

    def set_target_drive(self, drive: str) -> None:
        """Tell the screen which drive moves would go to (details pane default)."""
        self._target_drive = drive
        self.details.set_target_drive(drive)

    def listing(self) -> opportunities.OpportunityList | None:
        """The analysis currently on screen."""
        return self._index

    def table_model(self) -> models.OpportunityTableModel:
        """The Qt model behind the table (tests drive it directly)."""
        return self._model

    def select_row(self, path: str) -> bool:
        """Move the cursor to a row by path (used by the details/plans flows)."""
        index = self._model.index_of(path)
        if not index.isValid():
            return False
        self.table.setCurrentIndex(index)
        self.table.scrollTo(index, QAbstractItemView.ScrollHint.PositionAtCenter)
        return True

    # -- filters ---------------------------------------------------------- #

    def _refresh_options(self) -> None:
        rows = self._model.rows
        # Rebuilding a combo must not fire the filter once per item.
        for combo, field, placeholder in (
            (self.tier_combo, "tier", "All tiers"),
            (self.category_combo, "category", "All categories"),
        ):
            blocked = combo.blockSignals(True)
            combo.clear()
            combo.addItem(placeholder, "")
            for value in opportunities.option_values(rows, field):
                combo.addItem(value, value)
            combo.setCurrentIndex(0)
            combo.blockSignals(blocked)

    def current_filter(self) -> opportunities.OpportunityFilter:
        """The filter bar's state as the engine model expects it."""
        min_size = 0
        if self.size_combo.currentIndex() > 0:
            min_size = rules.parse_size(MIN_SIZE_CHOICES[self.size_combo.currentIndex()])
        return opportunities.OpportunityFilter(
            min_size=min_size,
            category=self.category_combo.currentData() or None,
            tier=self.tier_combo.currentData() or None,
            state=self.state_combo.currentData() or None,
            text=self.search.text(),
        )

    def _on_filter_changed(self, *_args: object) -> None:
        self._model.set_filter(self.current_filter())
        self._refresh_summary()
        self._on_selection_changed()

    # -- rendering -------------------------------------------------------- #

    def _refresh_summary(self) -> None:
        visible = self._model.visible_rows
        summary = opportunities.summarise(visible)
        selected = summary.state_total(opportunities.STATE_ACTION)
        self.cards["rows"].set_value(
            f"{summary.rows:,}",
            f"{summary.files:,} files · {summary.dirs:,} folders (showing "
            f"{len(visible):,} of {len(self._model.rows):,})",
        )
        self.cards["gain"].set_value(
            stats.format_bytes(summary.gain_bytes),
            f"{summary.effective_rows:,} top-level rows · nested ones counted once",
        )
        self.cards["action"].set_value(
            f"{summary.actionable:,}", f"{stats.format_bytes(selected.gain)} of the gain"
        )
        self.cards["no_action"].set_value(
            f"{summary.no_action:,}", "explicitly left alone, with the reason"
        )
        self.cards["undecided"].set_value(
            f"{summary.undecided:,}",
            f"up to {stats.format_bytes(summary.potential_bytes)} awaiting your call",
        )
        drives = " · ".join(
            f"{drive_label(item.volume)} {stats.format_bytes(item.gain)}"
            for item in summary.volumes[:3]
        )
        self.cards["volumes"].set_value(f"{len(summary.volumes):,}", drives or "nothing listed yet")

    def request_plan(self) -> None:
        """Hand the checked rows to the plan screen (the button's one job)."""
        paths = tuple(row.path for row in self._model.selection.rows)
        if not paths:
            self.statusMessage.emit("Nothing to plan yet: check the rows you want planned")
            return
        self.buildPlanRequested.emit(paths)

    def _on_selection_changed(self) -> None:
        summary = self._model.selected_summary()
        self.selection_label.setText(summary)
        self.selection_label.setToolTip(
            "The checked rows become one plan; a checked folder covers its contents"
        )
        self.build_plan_button.setEnabled(bool(len(self._model.selection)))
        self.statusMessage.emit(summary)

    def _on_current_row_changed(self, current: QModelIndex, _previous: QModelIndex) -> None:
        row = self._model.row_at(current)
        self.details.show_row(
            row,
            target_drive=self._target_drive,
            selection=self._model.selection,
            override=self._destination_overrides.get(row.key, "") if row is not None else "",
        )
        self.rowSelected.emit(row)

    def _on_double_clicked(self, index: QModelIndex) -> None:
        row = self._model.row_at(index)
        if row is None:
            return
        self._model.toggle(row.key)

    def _on_space(self, index: QModelIndex) -> bool:
        """Space checks or unchecks the row under the cursor (keyboard path)."""
        row = self._model.row_at(index) if index.isValid() else None
        if row is None:
            return False
        self._model.toggle(row.key)
        return True

    def _on_destination_edited(self, key: str, text: str) -> None:
        if text:
            self._destination_overrides[key] = text
        else:
            self._destination_overrides.pop(key, None)
        self.destinationEdited.emit(key, text)

    # -- AI (design §10) -------------------------------------------------- #

    def ai(self) -> AIService:
        """The AI layer behind the bar (the window owns it)."""
        return self._ai

    def ai_busy(self) -> bool:
        """Is an AI request in flight (the bar shows it, the tests wait on it)."""
        return self._ai_task is not None

    def undecided_rows(self) -> tuple[opportunities.Opportunity, ...]:
        """The rows a batch fill covers: undecided and not already checked.

        Decided rows are the rule engine's -- the AI has nothing to add -- and a
        row the user has checked (or that a checked folder covers) already holds
        the user's intent, so the AI must not answer for it.
        """
        if self._index is None:
            return ()
        selection = self._model.selection
        return tuple(
            row
            for row in self._index.rows
            if row.state == opportunities.STATE_UNDECIDED
            and not selection.is_selected(row.key)
            and not selection.is_covered(row.key)
        )

    def row_for_key(self, key: str) -> opportunities.Opportunity | None:
        """The row with this key, wherever it sits in the listing."""
        if self._index is None:
            return None
        for row in self._index.rows:
            if row.key == key:
                return row
        return None

    def _refresh_ai_bar(self) -> None:
        """Reflect the provider, the batch action's availability and the progress."""
        status = self._ai.status()
        busy = self._ai_task is not None
        configured = bool(self._ai.config().providers)
        self.ai_status.setText(ai_models.state_line(status))
        self.ai_status.set_tone("muted" if not status.ready else "info")
        self.ai_status.setToolTip(ai_models.state_tooltip(status))

        rows = self.undecided_rows()
        hint = ai_models.readiness_hint(status, configured=configured)
        self.ai_fill_button.setEnabled(status.ready and bool(rows) and not busy)
        self.ai_fill_button.setToolTip(
            hint
            or (
                f"Ask {ai_models.provider_short(status)} to suggest a solution for the "
                f"{len(rows):,} undecided row(s) in view"
            )
        )
        self.ai_cancel_button.setVisible(busy)
        self.ai_cancel_button.setEnabled(busy and self._ai_kind in {"suggest", "classify"})
        if not busy:
            self.ai_progress.setVisible(False)
            self.ai_progress.setRange(0, 1)
            self.ai_progress.setValue(0)
            self.ai_stage.setText(hint or self._ready_hint(len(rows)))

    def _ready_hint(self, rows: int) -> str:
        """The idle line: what a press of the button would do (or why it is off)."""
        if not self._ai.config().providers:
            return "No provider yet — add one in Settings (AI)"
        if rows == 0:
            return "Nothing to ask: every row in view is decided or checked"
        return f"{rows:,} undecided row(s) in view"

    def _start_ai(self, worker: Worker, kind: str) -> BackgroundTask:
        """Run one AI worker, wiring its signals into the store and the pane."""
        task = BackgroundTask(worker, self)
        task.stage.connect(self._on_ai_stage)
        task.progress.connect(self._on_ai_progress)
        task.answers.connect(self._on_ai_answers)
        task.delta.connect(self._on_ai_delta)
        task.finished.connect(lambda outcome: self._on_ai_finished(kind, outcome))
        task.failed.connect(lambda message: self._on_ai_failed(kind, message))
        self._ai_task = task
        self._ai_kind = kind
        self.ai_progress.setVisible(kind in {"suggest", "classify"})
        self._refresh_ai_bar()
        task.start()
        return task

    def _end_ai(self) -> None:
        """One run is over: release the slot before anything else can ask."""
        self._ai_task = None
        self._ai_kind = ""
        self._refresh_ai_bar()

    def request_suggestions(self) -> bool:
        """*Generate AI suggestions*: estimate, confirm, then fill what is undecided.

        The estimate and the privacy note come from the engine and the loaded
        configuration, and a run whose batches are all cached skips the dialog
        entirely: nothing would leave the machine.
        """
        if self._ai_task is not None:
            self.statusMessage.emit("An AI request is already running")
            return False
        rows = self.undecided_rows()
        if not rows:
            self.statusMessage.emit("Nothing to ask: every row in view is decided or checked")
            return False
        try:
            plan = self._ai.engine().plan_batches("suggest", ai_models.batch_items(rows))
        except Exception as exc:  # AIError: no provider, no model, bad config
            self._report_ai_problem(ai_models.error_text(exc))
            return False
        cached_only = plan.estimate is not None and plan.estimate.calls == 0
        if not cached_only and not self._confirm_fill(rows, plan):
            return False
        worker = SuggestWorker(service=self._ai, items=ai_models.batch_items(rows))
        self.details.set_ai_busy(True)
        if cached_only:
            self.statusMessage.emit(
                "Everything in view was answered before: filling from the cache"
            )
        self._start_ai(worker, "suggest")
        return True

    def _confirm_fill(self, rows: Sequence[opportunities.Opportunity], plan: BatchPlan) -> bool:
        """The pre-flight dialog: how many rows, what it costs, what leaves the box."""
        status = self._ai.status()
        estimate = ai_models.estimate_lines(plan, status)
        privacy = ai_models.privacy_lines(status)
        lines = [
            f"{row.path}   ·   {stats.format_bytes(row.size)}   ·   {row.solution}" for row in rows
        ]
        dialog = dialogs.ConfirmDialog(
            "Generate AI suggestions",
            f"Ask {ai_models.provider_short(status)} to suggest a solution for "
            f"{len(rows):,} undecided row(s)?",
            lines=lines,
            detail=" ".join((*estimate, *privacy)),
            accept_label="Generate",
            reject_label="Not now",
            parent=self,
        )
        return dialog.exec() == QDialog.DialogCode.Accepted

    def request_row(self, use_case: str, key: str) -> bool:
        """One row, one question: the pane's *Suggest* / *Classify* / *Explain*."""
        if self._ai_task is not None:
            self.statusMessage.emit("An AI request is already running")
            return False
        row = self.row_for_key(key)
        if row is None:
            return False
        item = ai_models.batch_items([row])
        if not item:
            self.statusMessage.emit("That row carries no facts to send")
            return False
        # The answer belongs to the row on screen: bring it into view first, so an
        # answer never lands in the store with nothing in the pane showing it.
        self.select_row(row.path)
        if use_case == AI_EXPLAIN:
            worker: Worker = ExplainWorker(service=self._ai, items=item)
            self.details.begin_explanation(f"Asking {ai_models.provider_short(self._ai.status())}…")
        else:
            worker = SuggestWorker(service=self._ai, items=item, use_case=use_case)
            self.details.set_ai_busy(True, f"Asking {ai_models.provider_short(self._ai.status())}…")
        self._start_ai(worker, use_case)
        return True

    def cancel_ai(self) -> bool:
        """Stop asking: the batch in flight ends, the answers so far stay."""
        task = self._ai_task
        if task is None or self._ai_kind not in {"suggest", "classify"}:
            return False
        worker = task.worker
        if not isinstance(worker, SuggestWorker):
            return False
        worker.cancel()
        self.ai_cancel_button.setEnabled(False)
        self.ai_stage.setText("Stopping after the batch in flight…")
        return True

    def _on_ai_requested(self, use_case: str, key: str) -> None:
        if use_case in {AI_SUGGEST, AI_CLASSIFY, AI_EXPLAIN}:
            self.request_row(use_case, key)

    def _on_ai_stage(self, stage: str) -> None:
        self.ai_stage.setText(f"{stage}…")

    def _on_ai_progress(self, progress: object) -> None:
        """One batch boundary: keep the run's cost and cache counts visible."""
        total = max(getattr(progress, "total", 0), 1)
        done = getattr(progress, "done", 0)
        self.ai_progress.setRange(0, total)
        self.ai_progress.setValue(min(done, total))
        self.ai_stage.setText(ai_models.progress_line(progress))

    def _on_ai_answers(self, entries: object) -> None:
        """One batch's answers, straight into the store (rows repaint at once)."""
        if isinstance(entries, tuple):
            self._ai.store.extend(entry for entry in entries if isinstance(entry, AISuggestion))

    def _on_ai_delta(self, text: str) -> None:
        self.details.extend_explanation(text)

    def _on_ai_finished(self, kind: str, outcome: object) -> None:
        """A clean end: fold the outcome into the store, then say what happened.

        The outcome is the engine's own summary of a batched run (design §10):
        ``by_path`` is what came back, ``missing`` what the model left out,
        ``cache_hit`` whether the whole run was served locally.
        """
        answered = getattr(outcome, "by_path", None) or {}
        ok = bool(getattr(outcome, "ok", True))
        if kind == AI_EXPLAIN:
            if ok:
                text, stage = ai_models.explanation_render(outcome)
                self.details.finish_explanation(text, stage=stage)
                self.statusMessage.emit("AI explanation ready")
            else:
                failure = ai_models.outcome_error_text(
                    outcome, fallback="The provider did not answer"
                )
                self.details.set_explanation_error(failure)
                self.statusMessage.emit(failure)
            self._end_ai()
            self.aiChanged.emit()
            return
        if bool(getattr(outcome, "cache_hit", False)) and answered:
            self._ai.store.mark_cached(tuple(answered))
        self.details.set_ai_busy(False)
        self._end_ai()
        if ok:
            text, tone = ai_models.run_summary(outcome, kind=kind)
            self._finish_ai_run(text, tone, outcome)
        else:
            self._report_ai_problem(
                ai_models.outcome_error_text(outcome, fallback="The AI call failed")
            )
        self.aiChanged.emit()

    def _on_ai_failed(self, kind: str, message: str) -> None:
        """The call raised: show the reason where the request was made."""
        if kind == AI_EXPLAIN:
            self.details.set_explanation_error(message)
        else:
            self.details.set_ai_busy(False, "")
            self._report_ai_problem(message)
        self._end_ai()
        self.aiChanged.emit()

    def _finish_ai_run(self, text: str, tone: str, outcome: object) -> None:
        """Say what a fill did -- and inline the first failure when there was one."""
        failures = int(getattr(outcome, "failures", 0) or 0)
        cancelled = bool(getattr(outcome, "cancelled", False))
        self.statusMessage.emit(text)
        dialogs.toast(self, text, tone=tone, above=self.ai_bar)
        if failures:
            reason = ai_models.error_text(
                getattr(outcome, "error", None), fallback="the provider refused the call"
            )
            more = f" (+{failures - 1} more)" if failures > 1 else ""
            self.ai_stage.setText(f"Last error: {reason}{more}")
        elif cancelled:
            self.ai_stage.setText("Stopped: the answers already received are kept")

    def _report_ai_problem(self, message: str) -> None:
        """An inline, non-modal error: the user asked for something that cannot run."""
        self.ai_stage.setText(message)
        self.statusMessage.emit(message)
        self.aiChanged.emit()

    # -- apply as rule (design §10) --------------------------------------- #

    def apply_rule(self, key: str) -> bool:
        """*Apply as rule…*: preview the file, write it, then rank the index again.

        A written rule is the only way an answer reaches the engine, so the rule
        is built by :mod:`spacesage.ai.promote` from the engine's own answer, the
        file is parsed back before it replaces the current one, and the listing is
        re-ranked so the same items match instantly from then on.
        """
        row = self.row_for_key(key)
        entry = self._ai.store.verdict(key) if row is not None else None
        if row is None or entry is None:
            self.statusMessage.emit("Nothing to apply: this row has no AI answer")
            return False
        try:
            preview = self._promote(row, entry, dry_run=True)
        except Exception as exc:  # AIError: a refusal, with the reason
            self._report_ai_problem(str(exc))
            return False
        if not preview.ok or not preview.dry_run:
            self._report_ai_problem(str(preview.error or "The rule could not be built"))
            return False
        if not self._confirm_promotion(row, entry, preview):
            return False
        try:
            result = self._promote(row, entry, dry_run=False)
        except (Exception, OSError) as exc:
            self._report_ai_problem(str(exc))
            return False
        if not result.ok:
            self._report_ai_problem(str(result.error or "The rule could not be written"))
            return False
        name = self._data_root_rules_path(result.path)
        text = f"Rule written to {name}: {len(result.written)} rule(s) now cover this path"
        self._ai.store.drop((key,))
        self.statusMessage.emit(text)
        dialogs.toast(self, text, tone="success", above=self.ai_bar)
        self.reanalysisRequested.emit()
        self.aiChanged.emit()
        return True

    def _promote(
        self, row: opportunities.Opportunity, entry: AISuggestion, *, dry_run: bool
    ) -> Any:
        """Build the rule for one answer (the engine owns the wording)."""
        item = ai_models.batch_items([row])[0]
        engine = self._ai.engine()
        if entry.is_classification:
            return engine.promote_classification(item, entry.raw, dry_run=dry_run)
        return engine.promote_suggestion(item, entry.raw, dry_run=dry_run)

    def _confirm_promotion(
        self, row: opportunities.Opportunity, entry: AISuggestion, preview: object
    ) -> bool:
        """Show the exact TOML that would be written, then ask (never a silent write)."""
        lines = str(getattr(preview, "toml", "") or "").splitlines()
        detail = (
            f"{entry.provenance} · {round(entry.confidence * 100)}% confidence. "
            "The file is parsed back by the engine before it replaces the current one: "
            "from then on this path is decided by rules, and the AI's answer is no longer "
            "the only thing that knows about it."
        )
        dialog = dialogs.ConfirmDialog(
            "Apply as rule",
            f"Write this rule to the user pack for {row.path}?",
            lines=lines,
            detail=detail,
            accept_label="Write the rule",
            reject_label="Cancel",
            parent=self,
        )
        return dialog.exec() == QDialog.DialogCode.Accepted

    def _data_root_rules_path(self, path: object) -> str:
        """The rule file as the user knows it (under their data root when it is)."""
        text = str(path)
        root = str(rules.default_rules_dir())
        return text.replace(root, "…") if root and root in text else text

    # -- row menu --------------------------------------------------------- #

    def build_row_menu(self, index: QModelIndex | None = None) -> QMenu:
        """The row menu: ask the AI about one row, or make its answer a rule."""
        row = self._model.row_at(index) if index is not None and index.isValid() else None
        if row is None:
            row = self.details.current_row()
        menu = QMenu(self)
        entry = self._ai.store.verdict(row.key) if row is not None else None
        if row is not None:
            ask = menu.addAction(
                icons.icon("sparkles", theme.tokens().accent_text, 14), "Suggest with AI"
            )
            ask.setObjectName("AiMenuSuggest")
            ask.triggered.connect(lambda: self.request_row(AI_SUGGEST, row.key))
            classify = menu.addAction("Classify with AI")
            classify.setObjectName("AiMenuClassify")
            classify.triggered.connect(lambda: self.request_row("classify", row.key))
            explain = menu.addAction("Explain with AI")
            explain.setObjectName("AiMenuExplain")
            explain.triggered.connect(lambda: self.request_row(AI_EXPLAIN, row.key))
            menu.addSeparator()
            apply_action = menu.addAction("Apply as rule…")
            apply_action.setObjectName("AiMenuApply")
            apply_action.setEnabled(entry is not None)
            apply_action.triggered.connect(lambda: self.apply_rule(row.key))
        return menu

    def _on_row_menu(self, pos: QPoint) -> None:
        menu = self.build_row_menu(self.table.indexAt(pos))
        if not menu.isEmpty():
            menu.exec(self.table.viewport().mapToGlobal(pos))

    # -- theme ------------------------------------------------------------ #

    def apply_theme(self) -> None:
        """Re-render the hand-painted parts after a theme change."""
        self._model.layoutChanged.emit()
        self._refresh_summary()
        self._refresh_ai_bar()
        self.details.apply_theme()
