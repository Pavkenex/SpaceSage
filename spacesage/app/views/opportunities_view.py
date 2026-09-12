"""Opportunities -- the core screen (design §9, screen 2).

One table of files and folders, biggest estimated gain first, every row with
the rule engine's suggested solution; a filter bar over size/category/tier/state
plus search and bulk select; a summary strip; and the details pane for the
selected row.  Every number comes from :mod:`spacesage.opportunities` -- the
widgets never touch SQL.
"""

from __future__ import annotations

from PySide6.QtCore import QModelIndex, Qt, Signal
from PySide6.QtWidgets import (
    QAbstractItemView,
    QComboBox,
    QFrame,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QPushButton,
    QSplitter,
    QTableView,
    QVBoxLayout,
    QWidget,
)

from spacesage import opportunities, rules, stats
from spacesage.app import models, theme, widgets
from spacesage.app.views.details_pane import DetailsPane

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

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setObjectName("Page")
        self._target_drive = ""
        self._index: opportunities.OpportunityList | None = None
        self._destination_overrides: dict[str, str] = {}
        self._build()

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

        splitter = QSplitter(Qt.Orientation.Horizontal, self)
        splitter.addWidget(self._build_table())
        self.details = DetailsPane(splitter)
        self.details.destinationEdited.connect(self._on_destination_edited)
        splitter.addWidget(self.details)
        splitter.setStretchFactor(0, 3)
        splitter.setStretchFactor(1, 2)
        # The ranked list is the product: it keeps the larger share of the width,
        # the details pane stays readable at its minimum.
        splitter.setSizes([840, 360])
        layout.addWidget(splitter, 1)

        footer = QHBoxLayout()
        footer.setSpacing(theme.SPACE["sm"])
        self.selection_label = QLabel("No rows checked yet", self)
        self.selection_label.setObjectName("Muted")
        footer.addWidget(self.selection_label)
        footer.addStretch(1)
        self.cascade_hint = QLabel(
            "Selecting a folder covers its contents: every byte is counted once.", self
        )
        self.cascade_hint.setObjectName("Faint")
        footer.addWidget(self.cascade_hint)
        layout.addLayout(footer)

    def _build_summary_strip(self) -> QWidget:
        strip = QWidget(self)
        row = QHBoxLayout(strip)
        row.setContentsMargins(0, 0, 0, 0)
        row.setSpacing(theme.SPACE["sm"])
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
        row = QHBoxLayout(bar)
        row.setContentsMargins(
            theme.SPACE["md"], theme.SPACE["sm"], theme.SPACE["md"], theme.SPACE["sm"]
        )
        row.setSpacing(theme.SPACE["sm"])

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
        self.table.selectionModel().currentRowChanged.connect(self._on_current_row_changed)
        self.table.doubleClicked.connect(self._on_double_clicked)
        return self.table

    # -- data ------------------------------------------------------------- #

    def set_listing(self, listing: opportunities.OpportunityList | None) -> None:
        """Adopt a finished analysis (rows, summary strip, filter options)."""
        self._index = listing
        rows = listing.rows if listing is not None else ()
        self._model.set_rows(tuple(rows))
        self._refresh_options()
        self._refresh_summary()
        self.details.clear()
        self._on_selection_changed()
        if listing is not None:
            self.subtitle.setText(f"{listing.heading()} · target {self._target_drive or 'unset'}")

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

    def _on_selection_changed(self) -> None:
        self.selection_label.setText(self._model.selected_summary())
        self.statusMessage.emit(self._model.selected_summary())

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

    def _on_destination_edited(self, key: str, text: str) -> None:
        if text:
            self._destination_overrides[key] = text
        else:
            self._destination_overrides.pop(key, None)
        self.destinationEdited.emit(key, text)

    # -- theme ------------------------------------------------------------ #

    def apply_theme(self) -> None:
        """Re-render the hand-painted parts after a theme change."""
        self._model.layoutChanged.emit()
        self._refresh_summary()
        self.details.apply_theme()
