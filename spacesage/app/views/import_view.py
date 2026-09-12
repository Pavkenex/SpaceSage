"""Import screen: drop a WizTree CSV, choose the run's parameters, analyze.

Design §9, screen 1: file picker or drag & drop, target drive, free-space
reserve and size filter, and an *Analyze* that never blocks the UI -- the
engine runs in a worker thread and reports rows/sec while it reads.
"""

from __future__ import annotations

import os
import sqlite3
from pathlib import Path

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QComboBox,
    QFileDialog,
    QFormLayout,
    QFrame,
    QHBoxLayout,
    QLabel,
    QMessageBox,
    QProgressBar,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from spacesage import planner, rules, stats
from spacesage.app import state, theme, widgets
from spacesage.app.workers import AnalysisWorker, BackgroundTask
from spacesage.ingest import IngestProgress

RESERVE_CHOICES: tuple[int, ...] = (0, 5, 10, 20, 50, 100)
"""Free-space reserve presets in GiB (the planner's ``--reserve``)."""

MIN_SIZE_CHOICES: tuple[str, ...] = ("10 MiB", "50 MiB", "100 MiB", "250 MiB", "500 MiB", "1 GiB")
"""Size-filter presets; parsed by the engine's own ``parse_size``."""


def suggested_drives() -> tuple[str, ...]:
    """Candidate target drives of this machine (the field stays editable)."""
    if os.name == "nt":  # pragma: no cover - Windows only
        found = [f"{letter}:\\" for letter in "DEFGHIJKLMNOPQRSTUVWXYZ"]
        return tuple(drive for drive in found if Path(drive).exists())
    mounts: list[str] = []
    try:
        with open("/proc/mounts", encoding="utf-8") as handle:
            for line in handle:
                parts = line.split()
                if len(parts) >= 2 and parts[1].startswith("/") and parts[1] != "/":
                    mounts.append(parts[1])
    except OSError:
        mounts = []
    return tuple(dict.fromkeys([*mounts[:8], "/"]))


class ImportView(QWidget):
    """Screen 1: the export, the run's parameters and the analysis kickoff."""

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
            "SpaceSage reads the CSV, ranks what is worth doing and shows the estimated "
            "gain of every suggestion. The analysis is read-only.",
            header,
        )
        subtitle.setObjectName("PageSubtitle")
        subtitle.setWordWrap(True)
        head_layout.addWidget(subtitle)
        layout.addWidget(header)

        self.drop_zone = widgets.DropZone("Drop a WizTree CSV export here, or browse for it", self)
        self.drop_zone.fileDropped.connect(self.set_csv)
        self.drop_zone.set_detail("Exports end in .csv and may be UTF-8 or UTF-16.")
        layout.addWidget(self.drop_zone)

        browse_row = QHBoxLayout()
        browse_row.setSpacing(theme.SPACE["sm"])
        self.browse_button = QPushButton("Browse for export", self)
        self.browse_button.setObjectName("Primary")
        self.browse_button.setToolTip("Choose the WizTree CSV export to analyze")
        self.browse_button.clicked.connect(self._browse)
        browse_row.addWidget(self.browse_button)
        self.csv_label = widgets.mono_label("No export selected", self)
        browse_row.addWidget(self.csv_label, 1)
        layout.addLayout(browse_row)

        options = QFrame(self)
        form = QFormLayout(options)
        form.setContentsMargins(0, 0, 0, 0)
        form.setSpacing(theme.SPACE["sm"])
        form.setLabelAlignment(Qt.AlignmentFlag.AlignLeft)

        self.target_combo = QComboBox(options)
        self.target_combo.setEditable(True)
        self.target_combo.setToolTip("Drive the plan will move data to (S9 builds the plan)")
        self.target_combo.addItems(suggested_drives())
        self.target_combo.setCurrentText(self._settings.target_drive("D:"))
        self.target_combo.currentTextChanged.connect(self._update_target_hint)
        form.addRow("Target drive", self.target_combo)

        self.target_hint = QLabel("", options)
        self.target_hint.setObjectName("Faint")
        self.target_hint.setWordWrap(True)
        form.addRow("", self.target_hint)

        self.reserve_combo = QComboBox(options)
        self.reserve_combo.setToolTip("Free space the plan leaves untouched on the target")
        for gib in RESERVE_CHOICES:
            self.reserve_combo.addItem(f"{gib} GiB", gib * 1024**3)
        reserve = self._settings.reserve_bytes()
        index = self.reserve_combo.findData(reserve)
        self.reserve_combo.setCurrentIndex(index if index >= 0 else 2)
        form.addRow("Reserve on target", self.reserve_combo)

        self.min_size_combo = QComboBox(options)
        self.min_size_combo.setToolTip("Entries smaller than this are left off the list")
        self.min_size_combo.addItems(MIN_SIZE_CHOICES)
        saved = self._settings.min_size()
        for position, label in enumerate(MIN_SIZE_CHOICES):
            if rules.parse_size(label) == saved:
                self.min_size_combo.setCurrentIndex(position)
        form.addRow("Smallest entry", self.min_size_combo)
        layout.addWidget(options)

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

        self.reuse_button = QPushButton("Use the existing index", self)
        self.reuse_button.setObjectName("Quiet")
        self.reuse_button.setToolTip(f"Rank the index already at {self._db_path}")
        self.reuse_button.clicked.connect(lambda: self.analyze(reuse_index=True))
        actions.addWidget(self.reuse_button)
        actions.addStretch(1)
        layout.addLayout(actions)
        layout.addStretch(1)

        self._update_target_hint(self.target_combo.currentText())
        self.refresh_existing()

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

    def target_drive(self) -> str:
        """The selected target drive."""
        return self.target_combo.currentText().strip()

    def reserve_bytes(self) -> int:
        """The selected free-space reserve in bytes."""
        data = self.reserve_combo.currentData()
        return int(data) if data is not None else state.DEFAULT_RESERVE_BYTES

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
        """Enable "use the existing index" when there is one to use."""
        has_index = self._db_path.is_file()
        self.reuse_button.setEnabled(has_index and not self._busy)
        self.reuse_button.setToolTip(
            f"Rank the index already at {self._db_path}"
            if has_index
            else "No index yet: analyze an export first"
        )

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

        self._settings.set_target_drive(self.target_drive())
        self._settings.set_reserve_bytes(self.reserve_bytes())
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
            self.target_combo,
            self.reserve_combo,
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

    def _update_target_hint(self, drive: str) -> None:
        value = drive.strip()
        if not value:
            self.target_hint.setText("No target drive: moves stay out of the plan.")
            return
        try:
            free = planner.measure_free_space(value)
        except planner.PlannerError:
            self.target_hint.setText(
                f"{value} cannot be measured on this machine — state its free space when the "
                "plan is built."
            )
            return
        reserve = self.reserve_bytes()
        usable = max(0, free - reserve)
        self.target_hint.setText(
            f"{stats.format_bytes(free)} free, {stats.format_bytes(reserve)} reserved → "
            f"{stats.format_bytes(usable)} usable for moves."
        )


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
