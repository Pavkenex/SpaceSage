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

The AI layer adds four more, and their signals say what makes them different --
network calls that answer slowly and can be cancelled mid-run:

``SuggestWorker``
    fill suggestions (or classifications) for a list, batch by batch, reporting
    every batch's answers as they land so the list fills in row by row;
``ExplainWorker``
    write one selection's deep explanation, streaming the prose as it arrives;
``ReviewWorker``
    annotate a drafted plan with severity-tagged risks;
``ConnectionWorker``
    the Settings screen's "Test connection": ``/models`` plus a latency probe.

None of them can produce an executable action: they fill display state, and the
only way any of it reaches a plan is a rule promotion (design §10).
"""

from __future__ import annotations

import threading
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

from PySide6.QtCore import QObject, QThread, Signal, Slot

from spacesage import db, ingest, opportunities, planning, rules
from spacesage.ai import Classification, ItemFacts, Suggestion
from spacesage.app.ai_models import AIService, AISuggestion


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


class PreviewWorker(QObject):
    """Resolve the approved subset without touching anything (the dry run).

    The same resolution the execution then performs, minus the run: every
    destination, link and refusal comes back as an
    :class:`~spacesage.executor.ApplyReport`, and no quarantine directory,
    journal or filesystem change happens (design §9, screen 3).
    """

    stage = Signal(str)
    progress = Signal(object)
    opDone = Signal(object)
    """Never emitted: a dry run resolves, it does not step through ops."""

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
        """How many operations the preview resolves."""
        return len(self._approved)

    @Slot()
    def run(self) -> None:
        """Resolve every approved action; nothing on disk is touched."""
        try:
            self.stage.emit("Resolving the approved actions")
            report = self._session.preview(
                self._approved,
                quarantine_root=self._quarantine_root,
                within=self._within,
            )
        except Exception as exc:  # an engine refusal is a dialog, never a crash
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


class SuggestWorker(QObject):
    """Fill suggestions (or classifications) for a list, off the UI thread.

    One run is several requests; the answers of every batch are emitted as they
    land (``answers``), so a 200-row list fills in as the provider answers rather
    than freezing until the last one.  ``cancel`` flips a ``threading.Event`` the
    runner checks between batches: nothing is sent after that, and the outcome
    that comes back says it was cancelled.
    """

    stage = Signal(str)
    """What the run is doing ('Asking stub for 3 suggestions')."""

    progress = Signal(object)
    """One :class:`spacesage.ai.BatchProgress` tick per finished batch."""

    answers = Signal(object)
    """One batch's answers, already in display form: ``tuple[AISuggestion, ...]``."""

    finished = Signal(object)
    """The finished ``SuggestOutcome`` / ``ClassifyOutcome`` (``ok`` says how it went)."""

    failed = Signal(str)
    """The run could not even start (no provider, invalid config)."""

    def __init__(
        self,
        *,
        service: AIService,
        items: Sequence[ItemFacts],
        use_case: str = "suggest",
        context: Mapping[str, Any] | None = None,
        batch_size: int | None = None,
        max_items: int | None = None,
        use_cache: bool = True,
    ) -> None:
        super().__init__()
        self._service = service
        self._items = tuple(items)
        self._use_case = use_case
        self._context = context
        self._batch_size = batch_size
        self._max_items = max_items
        self._use_cache = use_cache
        self._cancel = threading.Event()
        self._provider = ""
        self._model = ""

    @property
    def use_case(self) -> str:
        """``suggest`` or ``classify`` -- also the verb in the stage lines."""
        return self._use_case

    @property
    def total(self) -> int:
        """How many items the run was asked to cover."""
        return len(self._items)

    def cancel(self) -> None:
        """Ask the run to stop after the batch in flight (thread-safe)."""
        self._cancel.set()

    @property
    def cancelled(self) -> bool:
        """True once :meth:`cancel` was called."""
        return self._cancel.is_set()

    @Slot()
    def run(self) -> None:
        """Plan and execute the run; every failure is an outcome or a ``failed``."""
        try:
            engine = self._service.engine()
            verb = "classifications" if self._use_case == "classify" else "suggestions"
            provider = engine.provider()
            self._provider = provider.name
            self._model = provider.model
            self.stage.emit(f"Asking {provider.name} for {len(self._items)} {verb}")
            call = engine.classify if self._use_case == "classify" else engine.suggest
            outcome = call(
                self._items,
                batch_size=self._batch_size,
                max_items=self._max_items,
                context=self._context,
                on_progress=self.progress.emit,
                on_results=self._on_answers,
                cancel=self._cancel,
                use_cache=self._use_cache,
            )
        except Exception as exc:  # a refusal is a dialog, never a crash
            self.failed.emit(str(exc))
            return
        self.finished.emit(outcome)

    def _on_answers(self, answers: Mapping[str, Any]) -> None:
        """Re-shape one batch's raw answers into display form and emit them."""
        wrapped: list[AISuggestion] = []
        for answer in answers.values():
            if isinstance(answer, Classification):
                wrapped.append(
                    AISuggestion.from_classification(
                        answer, provider=self._provider, model=self._model
                    )
                )
            elif isinstance(answer, Suggestion):
                wrapped.append(
                    AISuggestion.from_suggestion(answer, provider=self._provider, model=self._model)
                )
        if wrapped:
            self.answers.emit(tuple(wrapped))


class ExplainWorker(QObject):
    """Write one selection's explanation, streaming the prose as it arrives."""

    stage = Signal(str)
    progress = Signal(object)
    """Never emitted: an explanation is one request."""

    delta = Signal(str)
    """A chunk of the explanation, as the provider writes it."""

    finished = Signal(object)
    """The finished :class:`spacesage.ai.ExplainOutcome`."""

    failed = Signal(str)

    def __init__(
        self,
        *,
        service: AIService,
        items: Sequence[ItemFacts],
        context: Mapping[str, Any] | None = None,
        use_cache: bool = True,
    ) -> None:
        super().__init__()
        self._service = service
        self._items = tuple(items)
        self._context = context
        self._use_cache = use_cache

    @Slot()
    def run(self) -> None:
        """Ask for the explanation; deltas are relayed as they arrive."""
        try:
            engine = self._service.engine()
            self.stage.emit(f"Asking {engine.provider().name} to explain the selection")
            outcome = engine.explain(
                self._items,
                context=self._context,
                on_delta=self.delta.emit,
                use_cache=self._use_cache,
            )
        except Exception as exc:
            self.failed.emit(str(exc))
            return
        self.finished.emit(outcome)


class ReviewWorker(QObject):
    """Annotate a drafted plan with severity-tagged risks, off the UI thread."""

    stage = Signal(str)
    progress = Signal(object)
    """Never emitted: a review is one request over the plan document."""

    finished = Signal(object)
    """The finished :class:`spacesage.ai.ReviewOutcome`."""

    failed = Signal(str)

    def __init__(
        self,
        *,
        service: AIService,
        plan: Mapping[str, Any],
        plan_path: str | None = None,
        use_cache: bool = True,
    ) -> None:
        super().__init__()
        self._service = service
        self._plan = plan
        self._plan_path = plan_path
        self._use_cache = use_cache

    @Slot()
    def run(self) -> None:
        """Ask for the annotations; a refusal comes back as a failed outcome."""
        try:
            engine = self._service.engine()
            self.stage.emit(f"Asking {engine.provider().name} to review the plan")
            outcome = engine.review(
                self._plan, plan_path=self._plan_path, use_cache=self._use_cache
            )
        except Exception as exc:
            self.failed.emit(str(exc))
            return
        self.finished.emit(outcome)


class ConnectionWorker(QObject):
    """Test one provider: does it answer, how fast, and which models does it offer."""

    stage = Signal(str)
    progress = Signal(object)
    """Never emitted: the check is one or two short requests."""

    finished = Signal(object)
    """The finished :class:`spacesage.ai.CheckResult` (``ok`` says how it went)."""

    failed = Signal(str)

    def __init__(
        self,
        *,
        service: AIService,
        provider: str | None = None,
        model: str | None = None,
    ) -> None:
        super().__init__()
        self._service = service
        self._provider = provider
        self._model = model

    @Slot()
    def run(self) -> None:
        """Run the check; an unreachable provider is a result, not an exception."""
        try:
            engine = self._service.engine_for(self._provider)
            name = self._provider or engine.provider().name
            self.stage.emit(f"Asking {name} which models it offers")
            result = engine.check(model=self._model)
        except Exception as exc:
            self.failed.emit(str(exc))
            return
        self.finished.emit(result)


Worker = (
    AnalysisWorker
    | PlanWorker
    | PreviewWorker
    | ApplyWorker
    | UndoWorker
    | HistoryWorker
    | SuggestWorker
    | ExplainWorker
    | ReviewWorker
    | ConnectionWorker
)
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
    answers = Signal(object)
    delta = Signal(str)
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
        # relay each optional signal when it is there instead of forcing an
        # unused one onto every worker.
        for name, relay in (
            ("opDone", self.opDone),
            ("answers", self.answers),
            ("delta", self.delta),
        ):
            source = getattr(worker, name, None)
            if source is not None:
                source.connect(relay)
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
