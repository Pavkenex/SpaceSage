"""Engine work off the UI thread (design §9.1: the UI never blocks).

One worker per analysis: ingest the export (with rows/sec progress), then build
the ranked opportunity list, then hand the finished
:class:`~spacesage.opportunities.OpportunityList` back on the GUI thread.  The
worker owns the SQLite connection it opens and closes it before returning, so
nothing that belongs to a thread leaks out of it.

The plan and execute flow adds four more, all following the same shape --
``stage`` / ``progress`` / ``finished`` / ``failed``, plus ``opDone`` where a run
reports per-item results:

``PlanWorker``
    compose the plan of a selection and persist its workspace;
``ApplyWorker``
    execute the approved subset, reporting every operation as it lands;
``UndoWorker``
    reverse a journal (all of it, or the selection) with verification;
``HistoryWorker``
    read the app's journals back into per-item undo status.
"""

from __future__ import annotations

from collections.abc import Sequence
from pathlib import Path

from PySide6.QtCore import QObject, QThread, Signal, Slot

from spacesage import db, ingest, opportunities, planning, rules


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


class PlanWorker(QObject):
    """Compose the plan of a selection and persist its workspace.

    The request is built on the GUI thread (it is only arithmetic over the
    listing's own thresholds); everything that opens a file -- the index, the
    plan document, the approval -- happens here.
    """

    stage = Signal(str)
    progress = Signal(object)
    finished = Signal(object)
    """The finished :class:`spacesage.planning.PlanSession`."""

    failed = Signal(str)

    def __init__(
        self,
        *,
        db_path: Path,
        request: planning.PlanRequest,
        root: Path,
        quarantine_root: Path | None = None,
        user_rules_dir: Path | None = None,
    ) -> None:
        super().__init__()
        self._db_path = db_path
        self._request = request
        self._root = root
        self._quarantine_root = quarantine_root
        self._user_rules_dir = user_rules_dir

    @Slot()
    def run(self) -> None:
        """Draft the plan; every failure is reported through ``failed``."""
        try:
            self.stage.emit("Composing the plan")
            ruleset = rules.load_rules(user_dir=self._user_rules_dir)
            session = planning.open_session(
                self._db_path,
                ruleset,
                self._request,
                root=self._root,
                quarantine_root=self._quarantine_root,
            )
        except Exception as exc:  # any engine refusal is a dialog, never a crash
            self.failed.emit(str(exc))
            return
        self.finished.emit(session)


class ApplyWorker(QObject):
    """Execute the approved subset of a plan session, reporting every operation."""

    stage = Signal(str)
    progress = Signal(object)
    opDone = Signal(object)
    """One :class:`spacesage.executor.OpResult`, as it lands."""

    finished = Signal(object)
    """The finished :class:`spacesage.executor.ApplyReport`."""

    failed = Signal(str)

    def __init__(
        self,
        *,
        session: planning.PlanSession,
        approved: Sequence[str],
        quarantine_root: Path | None = None,
        within: Sequence[str] = (),
    ) -> None:
        super().__init__()
        self._session = session
        self._approved = tuple(approved)
        self._quarantine_root = quarantine_root
        self._within = tuple(within)

    @property
    def total(self) -> int:
        """How many operations this run will perform."""
        return len(self._approved)

    @Slot()
    def run(self) -> None:
        """Run the approved operations; the manifest gates exactly those."""
        try:
            self.stage.emit("Executing the approved actions")
            report = self._session.execute(
                self._approved,
                quarantine_root=self._quarantine_root,
                within=self._within,
                on_op=lambda result, index, total: self.opDone.emit(result),
            )
        except Exception as exc:
            self.failed.emit(str(exc))
            return
        self.finished.emit(report)


class UndoWorker(QObject):
    """Reverse a journal -- all of it, or the operations the user picked."""

    stage = Signal(str)
    progress = Signal(object)
    opDone = Signal(object)
    """One :class:`spacesage.executor.UndoResult`, as it lands."""

    finished = Signal(object)
    """The finished :class:`spacesage.executor.UndoReport`."""

    failed = Signal(str)

    def __init__(
        self,
        *,
        journal_path: Path,
        only: Sequence[int] | None = None,
    ) -> None:
        super().__init__()
        self._journal_path = journal_path
        self._only = None if only is None else tuple(only)

    @property
    def total(self) -> int:
        """How many operations were asked for (unknown before the journal is read)."""
        return len(self._only) if self._only is not None else 0

    @Slot()
    def run(self) -> None:
        """Reverse the picked operations, verifying every payload."""
        try:
            self.stage.emit("Reverting")
            report = planning.revert(
                self._journal_path,
                only=self._only,
                on_op=lambda result, index, total: self.opDone.emit(result),
            )
        except Exception as exc:
            self.failed.emit(str(exc))
            return
        self.finished.emit(report)


class HistoryWorker(QObject):
    """Read the app's journals back into per-item undo status."""

    stage = Signal(str)
    progress = Signal(object)
    finished = Signal(object)
    """``tuple[spacesage.planning.JournalHistory, ...]``."""

    failed = Signal(str)

    def __init__(self, *, root: Path, extra: Sequence[Path] = ()) -> None:
        super().__init__()
        self._root = root
        self._extra = tuple(extra)

    @Slot()
    def run(self) -> None:
        """Load every journal under the app's plan workspaces (plus any extras)."""
        try:
            self.stage.emit("Reading the journal")
            found = list(planning.histories(self._root))
            known = {history.path for history in found}
            for path in self._extra:
                if path not in known and path.is_file():
                    found.append(planning.journal_history(path))
        except Exception as exc:
            self.failed.emit(str(exc))
            return
        self.finished.emit(tuple(found))


Worker = AnalysisWorker | PlanWorker | ApplyWorker | UndoWorker | HistoryWorker
"""Every worker :class:`BackgroundTask` can run, in one union.

Typing the task against the concrete workers (rather than ``QObject``) keeps the
signal wiring checked: a worker missing ``stage``/``finished``/``failed`` is a
compile error, not a silent no-op at runtime.
"""


class BackgroundTask(QObject):
    """Runs one worker in its own thread and relays its signals."""

    stage = Signal(str)
    progress = Signal(object)
    opDone = Signal(object)
    finished = Signal(object)
    failed = Signal(str)

    def __init__(self, worker: Worker, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self._worker = worker
        self._thread = QThread(self)
        worker.moveToThread(self._thread)
        self._thread.started.connect(worker.run)
        worker.stage.connect(self.stage)
        worker.progress.connect(self.progress)
        # Not every worker has per-item results (the analysis worker does not):
        # relay it when it is there instead of forcing an unused signal on all.
        op_done = getattr(worker, "opDone", None)
        if op_done is not None:
            op_done.connect(self.opDone)
        worker.finished.connect(self._on_finished)
        worker.failed.connect(self._on_failed)

    @property
    def worker(self) -> Worker:
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
