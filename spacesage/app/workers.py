"""Engine work off the UI thread (design §9.1: the UI never blocks).

One worker per analysis: ingest the export (with rows/sec progress), then build
the ranked opportunity list, then hand the finished
:class:`~spacesage.opportunities.OpportunityList` back on the GUI thread.  The
worker owns the SQLite connection it opens and closes it before returning, so
nothing that belongs to a thread leaks out of it.
"""

from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import QObject, QThread, Signal, Slot

from spacesage import db, ingest, opportunities, rules


class AnalysisWorker(QObject):
    """Ingest an export and rank it -- the whole analysis, off the UI thread."""

    stage = Signal(str)
    """Human-readable phase ('Reading the export', 'Ranking opportunities')."""

    progress = Signal(object)
    """An :class:`spacesage.ingest.IngestProgress` snapshot (rows/sec)."""

    finished = Signal(object)
    """The finished :class:`spacesage.opportunities.OpportunityList`."""

    failed = Signal(str)
    """The engine's error message (ingest refused the file, index unreadable...)."""

    def __init__(
        self,
        *,
        db_path: Path,
        csv_path: Path | None = None,
        min_size: int = opportunities.DEFAULT_MIN_SIZE,
        list_top: int = opportunities.DEFAULT_LIST_TOP,
        explicit: int = opportunities.DEFAULT_EXPLICIT,
        user_rules_dir: Path | None = None,
        now: float | None = None,
        replace: bool = True,
    ) -> None:
        super().__init__()
        self._csv_path = csv_path
        self._db_path = db_path
        self._min_size = min_size
        self._list_top = list_top
        self._explicit = explicit
        self._user_rules_dir = user_rules_dir
        self._now = now
        self._replace = replace

    @property
    def db_path(self) -> Path:
        """The index this worker reads (or writes first)."""
        return self._db_path

    @Slot()
    def run(self) -> None:
        """Do the work; every failure is reported through ``failed``."""
        try:
            if self._csv_path is not None:
                self.stage.emit(f"Reading {self._csv_path.name}")
                ingest.ingest_csv(
                    self._csv_path,
                    self._db_path,
                    replace=self._replace,
                    progress=self.progress.emit,
                )
            self.stage.emit("Ranking opportunities")
            ruleset = rules.load_rules(user_dir=self._user_rules_dir)
            conn = db.open_db(self._db_path)
            try:
                listing = opportunities.build_opportunities(
                    conn,
                    ruleset,
                    min_size=self._min_size,
                    list_top=self._list_top,
                    explicit=self._explicit,
                    now=self._now,
                    db_path=str(self._db_path),
                )
            finally:
                conn.close()
        except Exception as exc:  # any engine refusal is a dialog, never a crash
            self.failed.emit(str(exc))
            return
        self.finished.emit(listing)


class BackgroundTask(QObject):
    """Runs one :class:`AnalysisWorker` in its own thread and relays its signals."""

    stage = Signal(str)
    progress = Signal(object)
    finished = Signal(object)
    failed = Signal(str)

    def __init__(self, worker: AnalysisWorker, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self._worker = worker
        self._thread = QThread(self)
        worker.moveToThread(self._thread)
        self._thread.started.connect(worker.run)
        worker.stage.connect(self.stage)
        worker.progress.connect(self.progress)
        worker.finished.connect(self._on_finished)
        worker.failed.connect(self._on_failed)

    @property
    def worker(self) -> AnalysisWorker:
        """The worker this task runs."""
        return self._worker

    def is_running(self) -> bool:
        """True while the thread is alive."""
        return self._thread.isRunning()

    def start(self) -> None:
        """Start the analysis."""
        self._thread.start()

    def wait(self, timeout_ms: int = 60_000) -> bool:
        """Block until the task finishes (used by shutdown and tests)."""
        return self._thread.wait(timeout_ms)

    def _on_finished(self, result: object) -> None:
        self._thread.quit()
        self.finished.emit(result)

    def _on_failed(self, message: str) -> None:
        self._thread.quit()
        self.failed.emit(message)
