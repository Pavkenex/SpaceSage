"""Import screen: pick what to read, then analyze (design §9, screen 1).

The screen has one job: choose the data.  Two ways in, shown side by side --
drop a fresh WizTree CSV, or continue from the index the last analysis left --
plus the single analysis option (the size floor), and an *Analyze* that never
blocks the UI: the engine runs in a worker thread and reports rows/sec while it
reads.

Where moves go and how much free space to keep are decisions about the *plan*,
not about reading the export, so they live on the Plan screen
(``plan_view.PlanView``), next to the budget they affect.
"""

from __future__ import annotations

import sqlite3
import time
from pathlib import Path

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QComboBox,
    QFileDialog,
    QFrame,
    QHBoxLayout,
    QLabel,
    QMessageBox,
    QProgressBar,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from spacesage import rules, stats
from spacesage._util import plural
from spacesage.app import state, theme, widgets
from spacesage.app.workers import AnalysisWorker, BackgroundTask
from spacesage.ingest import IngestProgress

MIN_SIZE_CHOICES: tuple[str, ...] = ("10 MiB", "50 MiB", "100 MiB", "250 MiB", "500 MiB", "1 GiB")
"""Size-filter presets; parsed by the engine's own ``parse_size``."""


def human_age(seconds: float) -> str:
    """A short "x ago" phrase for a file's age (seconds since it was written)."""
    minutes = int(max(0.0, seconds) // 60)
    if minutes < 1:
        return "just now"
    if minutes < 60:
        return f"{plural(minutes, 'minute')} ago"
    hours = minutes // 60
    if hours < 24:
        return f"{plural(hours, 'hour')} ago"
    days = hours // 24
    if days < 30:
        return f"{plural(days, 'day')} ago"
    return f"{plural(days // 30, 'month')} ago"


class ImportView(QWidget):
    """Screen 1: the source, the analysis option and the kickoff."""

    analysisReady = Signal(object)
    """Emitted with the finished :class:`~spacesage.opportunities.OpportunityList`."""

    busyChanged = Signal(bool)

    def __init__(
        self,
        settings: state.Settings,
        *,
        db_path: Path | None = None,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self._settings = settings
        self._db_path = db_path if db_path is not None else state.index_path()
        self._task: BackgroundTask | None = None
        self._busy = False
        self._csv = ""
        self.setObjectName("Page")
        self._build()

    # -- construction ----------------------------------------------------- #

    def _build(self) -> None:
        layout = QVBoxLayout(self)
        layout.setContentsMargins(
            theme.SPACE["xl"], theme.SPACE["xl"], theme.SPACE["xl"], theme.SPACE["xl"]
        )
        layout.setSpacing(theme.SPACE["lg"])

        header = QWidget(self)
        header.setObjectName("PageHeader")
        head_layout = QVBoxLayout(header)
        head_layout.setContentsMargins(0, 0, 0, 0)
        head_layout.setSpacing(theme.SPACE["xs"])
        title = QLabel("Import a WizTree export", header)
        title.setObjectName("PageTitle")
        head_layout.addWidget(title)
        subtitle = QLabel(
            "Pick a WizTree CSV, or continue from your last analysis. SpaceSage ranks "
            "what is worth doing and shows the estimated gain of every suggestion. "
            "Nothing on disk is touched.",
            header,
        )
        subtitle.setObjectName("PageSubtitle")
        subtitle.setWordWrap(True)
        head_layout.addWidget(subtitle)
        layout.addWidget(header)

        sources = QHBoxLayout()
        sources.setSpacing(theme.SPACE["md"])
        sources.addWidget(self._build_new_export_card(), 3)
        sources.addWidget(self._build_last_analysis_card(), 2)
        layout.addLayout(sources)

        layout.addWidget(self._build_analysis_options())

        self.progress = QProgressBar(self)
        self.progress.setRange(0, 100)
        self.progress.setValue(0)
        self.progress.setTextVisible(False)
        self.progress.setFixedHeight(6)
        layout.addWidget(self.progress)

        self.progress_label = QLabel("", self)
        self.progress_label.setObjectName("Muted")
        layout.addWidget(self.progress_label)

        actions = QHBoxLayout()
        actions.setSpacing(theme.SPACE["sm"])
        self.analyze_button = QPushButton("Analyze", self)
        self.analyze_button.setObjectName("Primary")
        self.analyze_button.setToolTip("Read the export and rank the opportunities")
        self.analyze_button.setEnabled(False)
        self.analyze_button.clicked.connect(lambda: self.analyze())
        actions.addWidget(self.analyze_button)
        actions.addStretch(1)
        layout.addLayout(actions)
        layout.addStretch(1)

        self.refresh_existing()

    def _build_new_export_card(self) -> QFrame:
        card = QFrame(self)
        card.setObjectName("Card")
        box = QVBoxLayout(card)
        box.setContentsMargins(
            theme.SPACE["md"], theme.SPACE["md"], theme.SPACE["md"], theme.SPACE["md"]
        )
        box.setSpacing(theme.SPACE["sm"])
        box.addWidget(widgets.section_label("New export", card))

        self.drop_zone = widgets.DropZone("Drop a WizTree CSV here, or browse for it", card)
        self.drop_zone.fileDropped.connect(self.set_csv)
        self.drop_zone.set_detail("Exports end in .csv and may be UTF-8 or UTF-16.")
        box.addWidget(self.drop_zone, 1)

        browse_row = QHBoxLayout()
        browse_row.setSpacing(theme.SPACE["sm"])
        self.browse_button = QPushButton("Browse for export", card)
        self.browse_button.setObjectName("Primary")
        self.browse_button.setToolTip("Choose the WizTree CSV export to analyze")
        self.browse_button.clicked.connect(self._browse)
        browse_row.addWidget(self.browse_button)
        self.csv_label = widgets.mono_label("No export selected", card)
        browse_row.addWidget(self.csv_label, 1)
        box.addLayout(browse_row)
        return card

    def _build_last_analysis_card(self) -> QFrame:
        card = QFrame(self)
        card.setObjectName("Card")
        box = QVBoxLayout(card)
        box.setContentsMargins(
            theme.SPACE["md"], theme.SPACE["md"], theme.SPACE["md"], theme.SPACE["md"]
        )
        box.setSpacing(theme.SPACE["sm"])
        box.addWidget(widgets.section_label("Last analysis", card))

        self.index_label = widgets.muted_label("", card)
        self.index_label.setWordWrap(True)
        box.addWidget(self.index_label, 1)

        self.reuse_button = QPushButton("Re-analyze", card)
        self.reuse_button.clicked.connect(lambda: self.analyze(reuse_index=True))
        box.addWidget(self.reuse_button, 0, Qt.AlignmentFlag.AlignLeft)
        return card

    def _build_analysis_options(self) -> QFrame:
        card = QFrame(self)
        card.setObjectName("Card")
        box = QHBoxLayout(card)
        box.setContentsMargins(
            theme.SPACE["md"], theme.SPACE["sm"], theme.SPACE["md"], theme.SPACE["sm"]
        )
        box.setSpacing(theme.SPACE["sm"])
        box.addWidget(widgets.section_label("Analysis options", card))
        box.addStretch(1)

        caption = QLabel("Ignore files smaller than", card)
        caption.setObjectName("Muted")
        box.addWidget(caption)

        self.min_size_combo = QComboBox(card)
        self.min_size_combo.setToolTip("Entries smaller than this are left off the list")
        self.min_size_combo.addItems(MIN_SIZE_CHOICES)
        saved = self._settings.min_size()
        for position, label in enumerate(MIN_SIZE_CHOICES):
            if rules.parse_size(label) == saved:
                self.min_size_combo.setCurrentIndex(position)
        box.addWidget(self.min_size_combo)
        return card

    # -- queries ---------------------------------------------------------- #

    @property
    def db_path(self) -> Path:
        """The index this screen writes to and reads from."""
        return self._db_path

    @property
    def busy(self) -> bool:
        """True while an analysis is running."""
        return self._busy

    def csv_path(self) -> str:
        """The selected export (``""`` when none is chosen)."""
        return self._csv

    def min_size(self) -> int:
        """The selected size filter in bytes."""
        return int(rules.parse_size(MIN_SIZE_CHOICES[self.min_size_combo.currentIndex()]))

    # -- actions ---------------------------------------------------------- #

    def set_csv(self, path: str | Path) -> None:
        """Choose the export to analyze (from the picker or a drop)."""
        target = Path(path).expanduser()
        self._csv = str(target)
        exists = target.is_file()
        size = stats.format_bytes(target.stat().st_size) if exists else "missing"
        self.csv_label.setText(f"{target}  ·  {size}")
        if exists:
            self.drop_zone.set_detail(f"{target.name} ({size}) is ready to analyze.", filled=True)
            self._settings.set_last_csv(str(target))
        else:
            self.drop_zone.set_detail("That file is not there any more.", filled=False)
        self.analyze_button.setEnabled(exists and not self._busy)

    def refresh_existing(self) -> None:
        """Fill the last-analysis card; enable *Re-analyze* when there is an index."""
        has_index = self._db_path.is_file()
        self.reuse_button.setEnabled(has_index and not self._busy)
        if has_index:
            try:
                age = human_age(time.time() - self._db_path.stat().st_mtime)
            except OSError:  # pragma: no cover - the file vanished under us
                age = "unknown age"
            self.index_label.setText(f"{index_entries(self._db_path):,} rows · {age}")
            self.reuse_button.setToolTip(f"Rank the index already at {self._db_path}")
        else:
            self.index_label.setText("Nothing analyzed yet. Analyze an export to create the index.")
            self.reuse_button.setToolTip("No index yet: analyze an export first")

    def analyze(self, *, reuse_index: bool = False) -> None:
        """Start the analysis worker (never blocks the UI thread)."""
        if self._busy:
            return
        csv = None if reuse_index else self._csv
        if csv == "" and not reuse_index:
            self._report("Choose a WizTree CSV export first.", "warning")
            return
        if csv is not None and not Path(csv).is_file():
            self._report(f"The export {csv} is not there any more.", "warning")
            return
        if reuse_index and not self._db_path.is_file():
            self._report("There is no index to reuse yet.", "warning")
            return

        self._settings.set_min_size(self.min_size())
        self._settings.set_last_db(str(self._db_path))

        worker = AnalysisWorker(
            db_path=self._db_path,
            csv_path=Path(csv) if csv is not None else None,
            min_size=self.min_size(),
            list_top=state.DEFAULT_LIST_TOP,
            replace=True,
        )
        task = BackgroundTask(worker, self)
        task.stage.connect(self._on_stage)
        task.progress.connect(self._on_progress)
        task.finished.connect(self._on_finished)
        task.failed.connect(self._on_failed)
        self._task = task
        self._set_busy(True)
        task.start()

    def shutdown(self) -> None:
        """Wait for a running analysis (window close, tests)."""
        if self._task is not None and self._task.is_running():
            self._task.wait(30_000)

    # -- worker callbacks ------------------------------------------------- #

    def _set_busy(self, busy: bool) -> None:
        self._busy = busy
        for control in (
            self.analyze_button,
            self.browse_button,
            self.reuse_button,
            self.min_size_combo,
        ):
            control.setEnabled(not busy)
        if not busy:
            self.analyze_button.setEnabled(bool(self._csv))
            self.refresh_existing()
        self.busyChanged.emit(busy)

    def _on_stage(self, stage: str) -> None:
        self.progress_label.setText(f"{stage}…")

    def _on_progress(self, snapshot: object) -> None:
        if not isinstance(snapshot, IngestProgress):
            return
        total = max(snapshot.file_size, 1)
        self.progress.setValue(min(100, int(snapshot.bytes_read * 100 / total)))
        self.progress_label.setText(
            f"{snapshot.rows_read:,} rows · {snapshot.rows_per_sec:,.0f} rows/s · "
            f"{snapshot.elapsed_s:,.1f} s"
        )

    def _on_finished(self, listing: object) -> None:
        self._set_busy(False)
        self.progress.setValue(100)
        self.progress_label.setText("")
        self.refresh_existing()
        self.analysisReady.emit(listing)

    def _on_failed(self, message: str) -> None:
        self._set_busy(False)
        self.progress.setValue(0)
        self.progress_label.setText("")
        self.refresh_existing()
        box = QMessageBox(self)
        box.setIcon(QMessageBox.Icon.Critical)
        box.setWindowTitle("Analysis failed")
        box.setText("SpaceSage could not analyze that export.")
        box.setInformativeText(message)
        box.setStandardButtons(QMessageBox.StandardButton.Ok)
        box.exec()

    # -- helpers ---------------------------------------------------------- #

    def _report(self, message: str, tone: str = "info") -> None:
        window = self.window()
        widgets.Toast.pop_up(window if isinstance(window, QWidget) else self, message, tone=tone)

    def _browse(self) -> None:
        start = self._csv or self._settings.last_csv() or str(Path.home())
        chosen, _ = QFileDialog.getOpenFileName(
            self, "Choose a WizTree CSV export", start, "WizTree export (*.csv);;All files (*)"
        )
        if chosen:
            self.set_csv(chosen)


def index_entries(path: Path) -> int:
    """Rows in an index file (0 when it cannot be read) -- status hint."""
    try:
        conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    except sqlite3.Error:
        return 0
    try:
        return int(conn.execute("SELECT COUNT(*) FROM entries").fetchone()[0])
    except sqlite3.Error:
        return 0
    finally:
        conn.close()
