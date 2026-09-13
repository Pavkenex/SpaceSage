"""Plan & Execute -- one plan, approved, previewed, run (design §9, screen 3).

The screen's whole job, in the order the product does it:

1. **Build** -- the checked rows of the Opportunities list arrive as paths, the
   :class:`~spacesage.app.workers.PlanWorker` composes one plan out of them and
   persists its workspace (``plan.json`` + a plan_id-bound ``approved.json``)
   behind the scenes;
2. **Approve** -- every executable action is approved by default and the user
   takes items *out*; a folder row's actions cascade into the plan, the advice
   items (``REVIEW``/``NATIVE``) can never be approved, and each click rewrites
   the approval on disk;
3. **Preview** -- the dry run resolves every approved action into exactly what
   would happen (destination, link, refusal) and the
   :class:`~spacesage.app.dialogs.PreviewDialog` shows it; nothing is touched;
4. **Execute** -- behind one itemized confirmation dialog, with live per-item
   progress, and a result per row plus a failure list that is never hidden;
5. **Undo** -- the run is journaled in this plan's workspace, which is what the
   ``Undo`` half of :class:`PlanPage` reads.

The AI review (design §10) sits between *approve* and *preview*: the same plan
document the executor reads goes to the provider, which comes back with
severity-tagged annotations keyed by action id -- "this delete takes a folder
that something else still uses" -- and each one can take its action out of the
plan in place.  An annotation can never approve anything, and nothing it says
reaches the executor: the plan on disk only changes through the same approval
path a click uses.

All engine work happens in worker threads (design §9.1); this module renders
what the engine returned and collects the user's decisions.
"""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

from PySide6.QtCore import QModelIndex, Qt, Signal
from PySide6.QtGui import QKeySequence, QShortcut
from PySide6.QtWidgets import (
    QAbstractItemView,
    QDialog,
    QFrame,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QProgressBar,
    QPushButton,
    QStackedWidget,
    QTableView,
    QVBoxLayout,
    QWidget,
)

from spacesage import executor, opportunities, planner, planning, stats
from spacesage.ai import Annotation, ReviewOutcome
from spacesage.app import (
    ai_models,
    dialogs,
    icons,
    models,
    plan_models,
    state,
    theme,
    widgets,
    workers,
)
from spacesage.app.ai_models import AIService
from spacesage.app.views.undo_view import UndoView
from spacesage.app.workers import (
    ApplyWorker,
    BackgroundTask,
    PlanWorker,
    PreviewWorker,
    ReviewWorker,
)

EXECUTE_SHORTCUT = "Ctrl+Return"
DRY_RUN_SHORTCUT = "Ctrl+D"

REVIEW_SEVERITY_TONES: dict[str, str] = {
    "danger": "danger",
    "warning": "warning",
    "info": "info",
}
"""Tone of an annotation's badge (the engine's three severities, painted)."""

ATTENTION_SEVERITY: dict[str, str] = {
    "failed": "blocker",
    "refused": "blocker",
    "interrupted": "blocker",
    "skipped": "conflict",
}
"""Outcomes that are not a clean success, with the banner severity each one gets.

A *skipped* operation is not a failure and not a success either (the path was
gone when the run reached it): it is surfaced as a warning, because a plan that
quietly did less than it promised is exactly what a user must not have to
discover later.  A refused action is the engine saying no, so it is a blocker.
"""

REVIEW_ORDER: tuple[str, ...] = ("danger", "warning", "info")
"""Annotations are shown most severe first (the engine's ordering, kept)."""


def _ordered_annotations(outcome: ReviewOutcome | None) -> tuple[Annotation, ...]:
    """The annotations of a review, most severe first, ties in the model's order."""
    if outcome is None or not outcome.ok:
        return ()
    ranked = sorted(
        outcome.annotations,
        key=lambda annotation: (
            REVIEW_ORDER.index(annotation.severity)
            if annotation.severity in REVIEW_ORDER
            else len(REVIEW_ORDER)
        ),
    )
    return tuple(ranked)


def _severity_counts(outcome: ReviewOutcome | None) -> dict[str, int]:
    """``{severity: count}``, most severe first, empty when nothing was flagged."""
    counts: dict[str, int] = {}
    for annotation in _ordered_annotations(outcome):
        severity = annotation.severity if annotation.severity in REVIEW_ORDER else "info"
        counts[severity] = counts.get(severity, 0) + 1
    return counts


class PlanView(QWidget):
    """Screen 3's plan half: the draft, its approval, the run and its results."""

    statusMessage = Signal(str)
    """One-line message for the app's status bar."""

    goToOpportunities = Signal()
    """The empty state's primary action: the user asked for the ranked list."""

    goToUndo = Signal()
    """The user asked for the journal history (the other half of the page)."""

    built = Signal(object)
    """A fresh plan was adopted (the page moves the switch to the plan half)."""

    aiChanged = Signal()
    """The plan review ran (the shell re-reads the AI status and the meter)."""

    def __init__(
        self,
        data_root: Path | None = None,
        *,
        db_path: Path | None = None,
        settings: state.Settings | None = None,
        ai: AIService | None = None,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self.setObjectName("Page")
        self._data_root = Path(data_root) if data_root is not None else state.data_dir()
        self._db_path = Path(db_path) if db_path is not None else state.index_path()
        self._settings = settings if settings is not None else state.Settings.ephemeral()
        self._ai = ai if ai is not None else AIService(parent=self)
        self._session: planning.PlanSession | None = None
        self._task: BackgroundTask | None = None
        self._busy = False
        self._running = ""
        """What the running task is doing: ``plan`` / ``preview`` / ``execute`` / ``review``."""
        self._preview_show = True
        self._preview_report: executor.ApplyReport | None = None
        self._apply_report: executor.ApplyReport | None = None
        self._review: ReviewOutcome | None = None
        self._review_error = ""
        """Why the last review call failed (a message, not an outcome)."""
        self._review_buttons: dict[str, QPushButton] = {}
        self._completed = 0
        self.last_error = ""
        """The engine's own message when the last task failed (tests read it)."""
        self._build()
        self._refresh()

    def ai(self) -> AIService:
        """The AI layer this screen reviews plans with (the window owns it)."""
        return self._ai

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
        self.title = QLabel("Plan & Execute", header)
        self.title.setObjectName("PageTitle")
        head.addWidget(self.title)
        self.subtitle = QLabel(
            "The checked opportunities as one plan: approve or take out each action, "
            "see exactly what a dry run would do, then run it -- every operation is "
            "journaled and can be reverted from Undo.",
            header,
        )
        self.subtitle.setObjectName("PageSubtitle")
        self.subtitle.setWordWrap(True)
        head.addWidget(self.subtitle)
        layout.addWidget(header)

        self.stack = QStackedWidget(self)
        self.empty_state = widgets.EmptyState(
            "No plan yet",
            "Select opportunities in the ranked list and press Build plan: they become "
            "one plan here, with per-item approval, a dry-run preview of every resolved "
            "operation and a run you can undo.",
            icon_name="clipboard-list",
            action="Go to Opportunities",
            on_action=self.goToOpportunities.emit,
            parent=self.stack,
        )
        self.body = self._build_body(self.stack)
        self.stack.addWidget(self.empty_state)
        self.stack.addWidget(self.body)
        layout.addWidget(self.stack, 1)

        layout.addWidget(self._build_progress())
        self._shortcuts()

    def _build_body(self, parent: QWidget) -> QWidget:
        body = QWidget(parent)
        layout = QVBoxLayout(body)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(theme.SPACE["sm"])

        meta = QWidget(body)
        row = QHBoxLayout(meta)
        row.setContentsMargins(0, 0, 0, 0)
        row.setSpacing(theme.SPACE["sm"])
        self.plan_id_label = widgets.mono_label("", meta)
        self.plan_id_label.setToolTip("The plan id every approval binds to")
        row.addWidget(self.plan_id_label)
        self.workspace_label = widgets.ElidedLabel("", meta)
        self.workspace_label.setObjectName("Faint")
        self.workspace_label.setToolTip("Where plan.json, approved.json and the journal live")
        row.addWidget(self.workspace_label, 1)
        layout.addWidget(meta)

        self.warnings = widgets.WarningList((), body)
        layout.addWidget(self.warnings)

        layout.addWidget(self._build_summary_strip(body))
        layout.addWidget(self._build_table(body), 1)

        self.review_card = self._build_review(body)
        layout.addWidget(self.review_card)

        self.attention_list = widgets.WarningList((), body)
        self.attention_list.setVisible(False)
        layout.addWidget(self.attention_list)

        self.result_label = QLabel("", body)
        self.result_label.setObjectName("Muted")
        self.result_label.setWordWrap(True)
        self.result_label.setVisible(False)
        layout.addWidget(self.result_label)

        layout.addWidget(self._build_toolbar(body))
        return body

    def _build_summary_strip(self, parent: QWidget) -> QWidget:
        strip = QWidget(parent)
        row = QHBoxLayout(strip)
        row.setContentsMargins(0, 0, 0, 0)
        row.setSpacing(theme.SPACE["sm"])
        self.cards: dict[str, widgets.MetricCard] = {}
        for key, title, icon in (
            ("actions", "Plan actions", "clipboard-list"),
            ("approved", "Approved", "check"),
            ("reclaim", "To reclaim", "hard-drive"),
            ("advice", "Advice", "shield-check"),
            ("warnings", "Warnings", "alert-triangle"),
        ):
            card = widgets.MetricCard(title, "--", icon_name=icon, parent=strip)
            self.cards[key] = card
            row.addWidget(card)
        return strip

    def _build_table(self, parent: QWidget) -> QWidget:
        self._model = plan_models.PlanTableModel(self)
        self._model.approvalChanged.connect(self._on_approval_changed)

        self._table = QTableView(parent)
        self._table.setModel(self._model)
        self._table.setShowGrid(False)
        self._table.setAlternatingRowColors(False)
        self._table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self._table.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self._table.setTextElideMode(Qt.TextElideMode.ElideMiddle)
        self._table.setWordWrap(False)
        self._table.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self._table.setToolTip("One row per action, in the order the run performs them")
        self._table.verticalHeader().setVisible(False)
        self._table.verticalHeader().setDefaultSectionSize(plan_models.ROW_HEIGHT)
        self._table.setItemDelegateForColumn(
            plan_models.COLUMN_APPROVE, plan_models.PlanCheckDelegate(self._table)
        )
        self._table.setItemDelegateForColumn(
            plan_models.COLUMN_ACTION, plan_models.PlanActionDelegate(self._table)
        )
        self._table.setItemDelegateForColumn(
            plan_models.COLUMN_DETAIL, plan_models.PlanDetailDelegate(self._table)
        )
        self._table.setItemDelegateForColumn(
            plan_models.COLUMN_STATUS, plan_models.PlanStatusDelegate(self._table)
        )
        header = self._table.horizontalHeader()
        header.setHighlightSections(False)
        header.setSortIndicatorShown(False)
        for column, spec in enumerate(plan_models.COLUMNS):
            if spec.key == "detail":
                header.setSectionResizeMode(column, QHeaderView.ResizeMode.Stretch)
            else:
                header.setSectionResizeMode(column, QHeaderView.ResizeMode.Interactive)
                self._table.setColumnWidth(column, spec.width)
        # A size or a status badge is a value, not prose: fit it rather than elide it
        # into something unreadable (the operator QA note on the S8 renders).
        self._table.setColumnWidth(
            plan_models.COLUMN_SIZE,
            max(
                self._table.columnWidth(plan_models.COLUMN_SIZE), models.fitted_width("1023.9 GiB")
            ),
        )
        badge = models.BADGE_PADDING + 2 * theme.SPACE["sm"]
        self._table.setColumnWidth(
            plan_models.COLUMN_STATUS,
            max(
                self._table.columnWidth(plan_models.COLUMN_STATUS),
                models.fitted_width("Advice only", mono=False, padding=badge),
            ),
        )
        self._table.doubleClicked.connect(self._on_double_clicked)
        widgets.space_toggles(self._table, self._on_space)
        return self._table

    def _build_toolbar(self, parent: QWidget) -> QWidget:
        bar = QFrame(parent)
        self.toolbar = bar
        bar.setObjectName("Card")
        row = QHBoxLayout(bar)
        row.setContentsMargins(
            theme.SPACE["md"], theme.SPACE["sm"], theme.SPACE["md"], theme.SPACE["sm"]
        )
        row.setSpacing(theme.SPACE["sm"])

        self.approval_label = QLabel("", bar)
        self.approval_label.setObjectName("Muted")
        row.addWidget(self.approval_label)
        row.addStretch(1)

        self.approve_all_button = QPushButton("Approve all", bar)
        self.approve_all_button.setToolTip("Approve every executable action (advice stays advice)")
        self.approve_all_button.clicked.connect(self.approve_all)
        row.addWidget(self.approve_all_button)

        self.reject_all_button = QPushButton("Take all out", bar)
        self.reject_all_button.setToolTip("Approve nothing: the plan stays a record")
        self.reject_all_button.clicked.connect(self.reject_all)
        row.addWidget(self.reject_all_button)

        self.preview_button = QPushButton("Dry-run preview", bar)
        self.preview_button.setToolTip(
            f"Resolve every approved action without touching anything ({DRY_RUN_SHORTCUT})"
        )
        self.preview_button.clicked.connect(lambda: self.dry_run(show=True))
        row.addWidget(self.preview_button)

        self.undo_button = QPushButton("Undo…", bar)
        self.undo_button.setToolTip("The journal history of everything SpaceSage executed")
        self.undo_button.clicked.connect(self.goToUndo.emit)
        row.addWidget(self.undo_button)

        self.execute_button = QPushButton("Execute…", bar)
        self.execute_button.setObjectName("Primary")
        self.execute_button.setToolTip(
            f"Run the approved actions, journaled and undoable ({EXECUTE_SHORTCUT})"
        )
        self.execute_button.clicked.connect(lambda: self.execute())
        row.addWidget(self.execute_button)
        return bar

    def _build_progress(self) -> QWidget:
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
        return self.progress_row

    def _shortcuts(self) -> None:
        run = QShortcut(QKeySequence(EXECUTE_SHORTCUT), self)
        run.activated.connect(lambda: self.execute())
        preview = QShortcut(QKeySequence(DRY_RUN_SHORTCUT), self)
        preview.activated.connect(lambda: self.dry_run(show=True))

    # -- public surface (the window and the tests drive these) ------------ #

    def session(self) -> planning.PlanSession | None:
        """The plan currently on screen (``None`` before one is built)."""
        return self._session

    def draft(self) -> planning.PlanDraft | None:
        """The draft behind the table."""
        return None if self._session is None else self._session.draft

    def table_model(self) -> plan_models.PlanTableModel:
        """The Qt model behind the table."""
        return self._model

    def table(self) -> QTableView:
        """The plan table itself."""
        return self._table

    def busy(self) -> bool:
        """True while a worker is composing, previewing or running the plan."""
        return self._busy

    def preview_report(self) -> executor.ApplyReport | None:
        """The last dry run (what the preview dialog rendered)."""
        return self._preview_report

    def apply_report(self) -> executor.ApplyReport | None:
        """The last execution report."""
        return self._apply_report

    def failures(self) -> tuple[tuple[str, str], ...]:
        """``(action id, reason)`` for everything that failed or was refused."""
        return self._model.failures()

    def attention(self) -> tuple[tuple[str, str, str], ...]:
        """``(action id, outcome, reason)`` for everything that did not simply run.

        Failures, refusals and skipped operations all land here: the screen never
        reports a run as cleaner than it was.
        """
        report = self._apply_report if self._apply_report is not None else self._preview_report
        if report is None:
            return ()
        return tuple(
            (op.action_id, op.outcome, op.reason)
            for op in report.ops
            if op.outcome in ATTENTION_SEVERITY
        )

    def approved_ids(self) -> tuple[str, ...]:
        """The ids that may run, in plan order."""
        return self._model.approved_ids()

    def set_approved(self, action_id: str, approved: bool) -> bool:
        """Approve (or take out) one action."""
        return self._model.set_approved(action_id, approved)

    def approve_all(self) -> None:
        """Approve every executable action."""
        self._model.approve_all()

    def reject_all(self) -> None:
        """Take every action out of the plan."""
        self._model.reject_all()

    # -- building --------------------------------------------------------- #

    def build(
        self,
        listing: opportunities.OpportunityList | None,
        paths: Sequence[str],
        *,
        targets: Sequence[planner.PlanTarget] | None = None,
        links: bool | None = None,
    ) -> bool:
        """Compose one plan out of the checked rows (off the UI thread).

        ``targets`` defaults to the configured target drive, measured on this
        machine; a drive that cannot be measured is dropped here and the draft
        says so -- a move with nowhere to go becomes advice, never a guess.
        """
        if self._busy:
            return False
        if listing is None:
            dialogs.report_note(
                self,
                "Nothing to plan yet",
                "Import and analyze an export first: a plan is built from the ranked list.",
            )
            return False
        if not paths:
            dialogs.report_note(
                self,
                "No rows checked",
                "Check the opportunities you want planned (the checkbox column), then "
                "press Build plan again.",
            )
            return False
        request = planning.PlanRequest.from_listing(
            listing,
            list(paths),
            targets=tuple(targets) if targets is not None else self._targets(),
            links=self._settings.plan_links() if links is None else links,
        )
        self._start(
            PlanWorker(
                db_path=self._db_path,
                request=request,
                root=self._data_root,
                quarantine_root=self._quarantine_root(),
            ),
            "plan",
        )
        return True

    def _targets(self) -> tuple[planner.PlanTarget, ...]:
        """The configured target drive, with its free space measured here.

        An unmeasurable drive (an export from another machine) is not invented:
        the plan is drafted without it, and the draft's own warning says that
        moves have nowhere to go.
        """
        spec = self._settings.target_drive().strip()
        if not spec:
            return ()
        try:
            return (planner.target_from_spec(spec, reserve=self._settings.reserve_bytes()),)
        except planner.PlannerError as exc:
            self.statusMessage.emit(f"Target {spec} cannot be used: {exc}")
            return ()

    def _quarantine_root(self) -> Path | None:
        """The configured quarantine folder (``None``: the engine picks per volume)."""
        configured = self._settings.quarantine_dir().strip()
        return Path(configured) if configured else None

    def set_session(self, session: planning.PlanSession | None) -> None:
        """Adopt a plan (a fresh draft, or ``None`` to empty the screen)."""
        self._session = session
        self._preview_report = None
        self._apply_report = None
        self._review = None
        self._review_error = ""
        self.last_error = ""
        self._completed = 0
        self.attention_list.set_warnings(())
        self.result_label.setVisible(False)
        self.warnings.set_warnings(() if session is None else session.draft.warnings)
        self._model.set_draft(None if session is None else session.draft)
        self._render_review()
        if session is not None:
            self.plan_id_label.setText(f"plan {session.plan_id}")
            self.plan_id_label.setToolTip(f"The plan id every approval binds to: {session.plan_id}")
            self.workspace_label.setText(str(session.workspace.directory))
            self.workspace_label.setToolTip(
                f"plan.json and approved.json live here:\n{session.workspace.directory}"
            )
            theme.fade_in(self.body, duration=theme.MOTION_FAST)
            self.built.emit(session)
        self._refresh()

    # -- dry run ---------------------------------------------------------- #

    def dry_run(self, *, show: bool = True) -> bool:
        """Resolve the approved subset and (optionally) show the preview dialog."""
        session = self._session
        approved = self._model.approved_ids()
        if session is None or not approved or self._busy:
            return False
        self._preview_show = show
        self._start(
            PreviewWorker(
                session=session,
                approved=approved,
                quarantine_root=self._quarantine_root(),
            ),
            "preview",
        )
        return True

    # -- execution -------------------------------------------------------- #

    def execute(self, *, confirm: bool = True) -> bool:
        """Run the approved subset behind one confirmation dialog."""
        session = self._session
        approved = self._model.approved_ids()
        if session is None or not approved or self._busy:
            return False
        if not self._model.ready_to_execute():
            dialogs.report_note(
                self,
                "Nothing to execute",
                "Every approved action is advice or refused by the engine; the plan is "
                "still worth keeping as a record.",
            )
            return False
        if confirm and not self._confirm_run():
            return False
        self.attention_list.set_warnings(())
        self.result_label.setVisible(False)
        self._start(
            ApplyWorker(
                session=session,
                approved=approved,
                quarantine_root=self._quarantine_root(),
            ),
            "execute",
        )
        return True

    def _confirm_run(self) -> bool:
        """The explicit confirmation: the itemized actions, danger styling, no surprises."""
        items = self._model.approved_items()
        lines = [self._action_line(item) for item in items]
        reclaimable = sum(item.action.bytes for item in items)
        dialog = dialogs.ConfirmDialog(
            "Execute the plan",
            f"Run {len(items)} approved action{'' if len(items) == 1 else 's'} "
            f"(up to {stats.format_bytes(reclaimable)})?",
            lines=lines,
            detail=(
                "Every operation is written to this plan's journal first, so the Undo "
                "screen can reverse it with verification. Deletions are quarantined, "
                "never purged."
            ),
            accept_label="Execute",
            reject_label="Not now",
            danger=True,
            parent=self,
        )
        return dialog.exec() == QDialog.DialogCode.Accepted

    @staticmethod
    def _action_line(item: planning.PlanItem) -> str:
        """One itemized line of the confirmation dialog (what, where, how much)."""
        size = f"{stats.format_bytes(item.action.bytes)} · " if item.action.bytes else ""
        return f"{item.action.path}   {plan_models.action_label(item)}   {size}{item.detail}"

    # -- task plumbing ---------------------------------------------------- #

    def _start(self, worker: workers.Worker, kind: str) -> BackgroundTask:
        """Run one worker and route its signals into the screen."""
        task = BackgroundTask(worker, self)
        task.stage.connect(self._on_stage)
        task.opDone.connect(self._on_op_done)
        task.finished.connect(lambda result: self._on_finished(kind, result))
        task.failed.connect(lambda message: self._on_failed(kind, message))
        self._task = task
        self._busy = True
        self._running = kind
        self._completed = 0
        total = int(getattr(worker, "total", 0))
        self.progress.setRange(0, max(total, 1))
        self.progress.setValue(0)
        self._show_progress(True)
        self._refresh()
        task.start()
        return task

    def _finish_task(self) -> None:
        self._busy = False
        self._running = ""
        self._show_progress(False)
        self._refresh()

    def _show_progress(self, active: bool) -> None:
        self.progress_row.setVisible(active)
        if not active:
            self.stage_label.setText("")

    def _on_stage(self, stage: str) -> None:
        self.stage_label.setText(stage)

    def _on_op_done(self, result: object) -> None:
        """One executed operation landed: the row says so, immediately."""
        if not isinstance(result, executor.OpResult):
            return
        self._model.set_op_result(result)
        self._completed += 1
        self.progress.setValue(self._completed)
        self.stage_label.setText(
            f"Ran {self._completed} of {self.progress.maximum()} · {result.outcome}"
        )

    def _build_review(self, parent: QWidget) -> QWidget:
        """The AI review card: one button, one verdict line, one row per annotation.

        The card is always there (a plan without a second opinion should look like
        one); the rows are rebuilt from the last outcome, so a re-review replaces
        what is on screen rather than piling up.
        """
        card = QFrame(parent)
        card.setObjectName("Card")
        layout = QVBoxLayout(card)
        layout.setContentsMargins(
            theme.SPACE["md"], theme.SPACE["sm"], theme.SPACE["md"], theme.SPACE["md"]
        )
        layout.setSpacing(theme.SPACE["xs"])

        head = QHBoxLayout()
        head.setSpacing(theme.SPACE["sm"])
        glyph = QLabel(card)
        glyph.setPixmap(icons.tone_icon("sparkles", "info", 14).pixmap(14, 14))
        head.addWidget(glyph)
        head.addWidget(widgets.section_label("AI review", card))
        self.review_status = widgets.Badge("", "muted", card)
        self.review_status.setObjectName("ReviewStatus")
        head.addWidget(self.review_status)
        self.review_stage = widgets.ElidedLabel("", card, mode=Qt.TextElideMode.ElideRight)
        self.review_stage.setObjectName("Faint")
        head.addWidget(self.review_stage, 1)
        self.review_button = QPushButton("Review plan with AI", card)
        self.review_button.setObjectName("AiReview")
        self.review_button.setIcon(icons.icon("sparkles", theme.tokens().accent_text, 14))
        self.review_button.setToolTip(
            "Send the plan document to the provider and come back with risks, keyed by action id"
        )
        self.review_button.clicked.connect(lambda: self.review(show=True))
        head.addWidget(self.review_button)
        layout.addLayout(head)

        self.review_summary = QLabel("", card)
        self.review_summary.setObjectName("Muted")
        self.review_summary.setWordWrap(True)
        self.review_summary.setVisible(False)
        layout.addWidget(self.review_summary)

        self.review_rows = QWidget(card)
        self.review_rows_layout = QVBoxLayout(self.review_rows)
        self.review_rows_layout.setContentsMargins(0, 0, 0, 0)
        self.review_rows_layout.setSpacing(theme.SPACE["xs"])
        layout.addWidget(self.review_rows)
        self._render_review()
        return card

    def _clear_review_rows(self) -> None:
        """Drop the annotation rows (they are rebuilt from the outcome)."""
        self._review_buttons.clear()
        while self.review_rows_layout.count():
            item = self.review_rows_layout.takeAt(0)
            widget = item.widget() if item is not None else None
            if widget is not None:
                widget.setParent(None)
                widget.deleteLater()

    def _sync_review_rows(self) -> None:
        """Re-read the approval each annotation row offers (a click just moved one)."""
        approved = set(self._model.approved_ids())
        for action_id, button in self._review_buttons.items():
            item = self._item_for_action(action_id)
            withdraw = action_id in approved
            button.setText("Take out of the plan" if withdraw else "Approve it")
            button.setEnabled(item is not None and item.executable and withdraw)

    def _render_review(self) -> None:
        """Paint the last outcome: its summary, then one row per annotation."""
        self._clear_review_rows()
        outcome = self._review
        status = self._ai.status()
        status_text = ai_models.state_line(status)
        self.review_status.setText(
            f"{outcome.provider} / {outcome.model}" if outcome is not None else status_text
        )
        self.review_status.set_tone("info" if outcome is not None else "muted")
        self.review_rows.setVisible(outcome is not None)
        if outcome is None:
            self.review_summary.setVisible(True)
            self.review_summary.setText(
                self._review_error
                or ai_models.readiness_hint(status, configured=bool(self._ai.config().providers))
                or "Ask for a second opinion on this plan before you run it."
            )
            return
        if not outcome.ok:
            self.review_summary.setVisible(True)
            self.review_summary.setText(
                ai_models.outcome_error_text(outcome, fallback="The review could not be completed")
            )
            return
        counts = _severity_counts(outcome)
        summary = outcome.summary or "The model flagged nothing about this plan."
        parts = [summary]
        if counts:
            parts.append(" · ".join(f"{count} {name}" for name, count in counts.items()))
        if outcome.rejected:
            parts.append(
                f"{len(outcome.rejected)} annotation(s) named actions this plan does not have"
            )
        self.review_summary.setVisible(True)
        self.review_summary.setText(" · ".join(parts))
        for annotation in _ordered_annotations(outcome):
            self.review_rows_layout.addWidget(self._annotation_row(annotation))
        self.review_rows_layout.addStretch(1)

    def _annotation_row(self, annotation: Annotation) -> QWidget:
        """One annotation: severity, the action it names, and what to do about it."""
        action_id = str(getattr(annotation, "action_id", ""))
        severity = str(getattr(annotation, "severity", "info"))
        row = QFrame(self.review_rows)
        row.setObjectName("Banner")
        row.setProperty("severity", severity)
        layout = QHBoxLayout(row)
        layout.setContentsMargins(
            theme.SPACE["md"], theme.SPACE["xs"], theme.SPACE["md"], theme.SPACE["xs"]
        )
        layout.setSpacing(theme.SPACE["sm"])
        badge = widgets.Badge(
            str(getattr(annotation, "label", severity.capitalize())),
            REVIEW_SEVERITY_TONES.get(severity, "info"),
            row,
        )
        layout.addWidget(badge, 0, Qt.AlignmentFlag.AlignTop)

        body = QVBoxLayout()
        body.setSpacing(0)
        title = QLabel(str(getattr(annotation, "title", "")), row)
        title.setWordWrap(True)
        body.addWidget(title)
        for line in (getattr(annotation, "detail", ""), getattr(annotation, "recommendation", "")):
            text = str(line or "")
            if not text:
                continue
            label = QLabel(text, row)
            label.setObjectName("Muted")
            label.setWordWrap(True)
            body.addWidget(label)
        item = self._item_for_action(action_id)
        path_label = QLabel(
            f"{action_id} · {item.action.type} · {item.action.path}"
            if item is not None
            else f"{action_id} · not in this plan",
            row,
        )
        path_label.setObjectName("Mono")
        path_label.setWordWrap(True)
        body.addWidget(path_label)
        layout.addLayout(body, 1)

        approved = action_id in self._model.approved_ids()
        button = QPushButton("Take out of the plan" if approved else "Approve it", row)
        button.setObjectName(f"ReviewAct_{action_id}")
        button.setEnabled(item is not None and item.executable and approved)
        button.setToolTip(
            "Withdraw the approval: it stays in the plan and never runs"
            if approved
            else "Already out of the plan"
        )
        button.clicked.connect(lambda: self.take_out(action_id))
        self._review_buttons[action_id] = button
        layout.addWidget(button, 0, Qt.AlignmentFlag.AlignTop)
        return row

    def _item_for_action(self, action_id: str) -> planning.PlanItem | None:
        """The plan item an annotation names (``None`` when the plan has no such action)."""
        for item in self._model.items:
            if item.action.id == action_id:
                return item
        return None

    # -- AI review -------------------------------------------------------- #

    def review(self, *, show: bool = True) -> bool:
        """Ask the provider what is risky about this plan (off the UI thread)."""
        session = self._session
        if session is None or self._busy:
            return False
        plan = self._plan_document(session)
        if plan is None:
            return False
        self._review_error = ""
        self._start(
            ReviewWorker(service=self._ai, plan=plan, plan_path=str(session.workspace.plan_path)),
            "review",
        )
        return True

    def _plan_document(self, session: planning.PlanSession) -> Mapping[str, Any] | None:
        """The plan as the executor would read it (the review's only input)."""
        path = session.workspace.plan_path
        try:
            document = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            self.last_error = f"the plan document at {path} could not be read: {exc}"
            self.statusMessage.emit(self.last_error)
            return None
        return document if isinstance(document, dict) else None

    def review_outcome(self) -> ReviewOutcome | None:
        """The last review (tests and the shell read this)."""
        return self._review

    def review_annotations(self) -> tuple[tuple[str, str, str], ...]:
        """``(severity, action id, title)`` of every annotation, most severe first."""
        return tuple(
            (annotation.severity, annotation.action_id, annotation.title)
            for annotation in _ordered_annotations(self._review)
        )

    def take_out(self, action_id: str) -> bool:
        """Withdraw one action's approval (what an annotation usually asks for)."""
        return self._model.set_approved(action_id, False)

    def _adopt_review(self, outcome: ReviewOutcome) -> None:
        """Show the annotations and say what came back."""
        self._review = outcome
        self._render_review()
        if not outcome.ok:
            message = ai_models.outcome_error_text(
                outcome, fallback="The plan review could not be completed"
            )
            self.last_error = message
            self.statusMessage.emit(message)
            self.aiChanged.emit()
            return
        counts = _severity_counts(outcome)
        worst = (
            "nothing flagged"
            if not counts
            else ", ".join(f"{count} {name}" for name, count in counts.items())
        )
        summary = (
            f"AI review: {worst} across {len(self._model.items):,} action(s) · "
            f"{outcome.usage.total_tokens:,} tokens"
            + (" · from the cache" if outcome.cache_hit else "")
            + "."
        )
        self.statusMessage.emit(summary)
        self._toast(summary, tone="warning" if counts.get("danger") else "info")
        self.aiChanged.emit()

    def _on_finished(self, kind: str, result: object) -> None:
        """One worker finished: turn its report into what the screen shows."""
        if kind == "plan":
            self._finish_task()
            if isinstance(result, planning.PlanSession):
                self.set_session(result)
        elif kind == "preview":
            self._finish_task()
            if isinstance(result, executor.ApplyReport):
                self._adopt_preview(result)
        elif kind == "execute":
            self._finish_task()
            if isinstance(result, executor.ApplyReport):
                self._adopt_result(result)
        elif kind == "review":
            self._finish_task()
            if isinstance(result, ReviewOutcome):
                self._adopt_review(result)

    def _on_failed(self, kind: str, message: object) -> None:
        """One worker failed: the engine's own sentence goes to a dialog, never a traceback."""
        self._finish_task()
        self.last_error = str(message)
        if kind == "review":
            self._review = None
            self._review_error = self.last_error
            self._render_review()
            self.statusMessage.emit(f"The plan review could not be completed: {self.last_error}")
            self.aiChanged.emit()
            return
        titles = {
            "plan": "Could not build the plan",
            "preview": "The dry run could not be completed",
            "execute": "The execution failed",
        }
        title = titles.get(kind, "Something went wrong")
        self.statusMessage.emit(f"{title}: {self.last_error}")
        dialogs.report_error(self, title, self.last_error)

    # -- results ---------------------------------------------------------- #

    def _adopt_preview(self, report: executor.ApplyReport) -> None:
        """Show the dry run on every row, then the dialog that itemizes it."""
        self._preview_report = report
        self._model.set_preview(report)
        counts = report.counts()
        refused = counts.get("refused", 0)
        summary = (
            f"Dry run: {len(report.ops)} action{'' if len(report.ops) == 1 else 's'} resolved · "
            f"{stats.format_bytes(report.reclaimed_bytes())} would be reclaimed · "
            f"{refused} refused · nothing on disk was touched."
        )
        self.statusMessage.emit(summary)
        self._toast(summary, tone="warning" if refused else "info")
        self._show_attention(report)
        self._refresh()
        if self._preview_show:
            dialog = dialogs.PreviewDialog(report, parent=self)
            dialog.exec()

    def _adopt_result(self, report: executor.ApplyReport) -> None:
        """Show the execution per row, and never hide a failure or a refusal."""
        self._apply_report = report
        self._model.set_result(report)
        counts = report.counts()
        done = counts.get("done", 0)
        failed = counts.get("failed", 0)
        refused = counts.get("refused", 0)
        skipped = counts.get("skipped", 0)
        parts = [
            f"Executed {done} of {len(self._model.approved_items())} approved "
            f"actions · {stats.format_bytes(report.reclaimed_bytes())} reclaimed",
        ]
        if refused:
            parts.append(f"{refused} refused by the engine")
        if failed:
            parts.append(f"{failed} failed")
        if skipped:
            parts.append(f"{skipped} skipped (the path was gone)")
        summary = " · ".join(parts) + "."
        if not (refused or failed or skipped):
            summary += " The Undo screen can reverse this run."
        self.result_label.setText(summary)
        self.result_label.setVisible(True)
        self.statusMessage.emit(summary)
        self._show_attention(report)
        self._toast(summary, tone="warning" if (refused or failed or skipped) else "success")
        self._refresh()

    def _show_attention(self, report: executor.ApplyReport) -> None:
        """Turn everything that did not simply run into a banner naming it."""
        warnings: list[planning.PlanWarning] = []
        for op in report.ops:
            severity = ATTENTION_SEVERITY.get(op.outcome)
            if severity is None:
                continue
            label = widgets.status_label(op.outcome)
            reason = op.reason or "no reason reported"
            warnings.append(
                planning.PlanWarning(
                    severity=severity,
                    message=f"{label}: {op.path} -- {reason}",
                    paths=(op.path,),
                )
            )
        self.attention_list.set_warnings(warnings)

    def _toast(self, message: str, *, tone: str = "info") -> None:
        dialogs.toast(self, message, tone=tone, above=self.toolbar)

    # -- approval --------------------------------------------------------- #

    def _on_approval_changed(self) -> None:
        """A checkbox moved: persist the approval, then re-render the chrome."""
        self._persist_approval()
        self._refresh()

    def _persist_approval(self) -> None:
        """Rewrite the plan_id-bound ``approved.json`` behind the scenes.

        The approval is a property of the plan (design §7), so it is written as
        it changes rather than at the end: whatever is on disk is what the user
        had decided when they last touched the plan.
        """
        session = self._session
        if session is None or self._busy:
            return
        rejected = tuple(self._model.rejected_ids()) + session.draft.advisory_ids()
        try:
            session.approve(self._model.approved_ids(), rejected=rejected)
        except planning.PlanningError as exc:  # pragma: no cover - a hand-edited workspace
            self.statusMessage.emit(f"Could not save the approval: {exc}")

    def _on_double_clicked(self, index: QModelIndex) -> None:
        """Double-clicking a row toggles its approval (the list's gesture, kept)."""
        item = self._model.row_at(index)
        if item is not None:
            wanted = item.action.id not in self._model.approved_ids()
            self._model.set_approved(item.action.id, wanted)

    def _on_space(self, index: QModelIndex) -> bool:
        """Space approves or takes out the row under the cursor (keyboard path)."""
        item = self._model.row_at(index) if index.isValid() else None
        if item is None:
            return False
        if not item.executable:
            self.statusMessage.emit(
                f"{item.action.id} is advice only: SpaceSage never executes a review or native item"
            )
            return False
        return self._model.set_approved(
            item.action.id, item.action.id not in self._model.approved_ids()
        )

    # -- rendering -------------------------------------------------------- #

    def _refresh(self) -> None:
        """Re-render everything that depends on the draft, the approval or the run."""
        session = self._session
        self.stack.setCurrentWidget(self.empty_state if session is None else self.body)
        model = self._model
        has_draft = session is not None
        executable = sum(1 for item in model.items if item.executable)
        approved = model.approved_count()
        advice = len(model.advice_ids())
        self.cards["actions"].set_value(
            f"{len(model.items):,}", f"{executable:,} executable · {advice:,} advice"
        )
        self.cards["approved"].set_value(
            f"{approved:,}", f"of {executable:,} executable action{'' if executable == 1 else 's'}"
        )
        self.cards["reclaim"].set_value(
            stats.format_bytes(model.approved_bytes()), "bytes the approved set claims"
        )
        self.cards["advice"].set_value(f"{advice:,}", "review and native items: never executed")
        blockers = len(session.draft.warnings_of("blocker")) if session is not None else 0
        self.cards["warnings"].set_value(
            f"{self.warnings.count():,}",
            f"{blockers:,} blocker{'' if blockers == 1 else 's'}",
        )
        self.approval_label.setText(model.selected_summary())
        self.approve_all_button.setEnabled(has_draft and not self._busy and approved < executable)
        self.reject_all_button.setEnabled(has_draft and not self._busy and approved > 0)
        self.preview_button.setEnabled(has_draft and not self._busy and approved > 0)
        self.execute_button.setEnabled(has_draft and not self._busy and model.ready_to_execute())
        self.review_button.setEnabled(has_draft and not self._busy)
        self.review_stage.setText("Reviewing…" if self._running == "review" else "")
        self._sync_review_rows()
        self.approval_label.setToolTip(
            "Approve or take out actions: what is approved here is what a run executes"
        )

    def apply_theme(self) -> None:
        """Re-render the hand-painted parts after a theme change."""
        self._model.layoutChanged.emit()
        self._refresh()

    def shutdown(self) -> None:
        """Wait for a running worker so nothing is killed mid-write."""
        task = self._task
        if task is not None and task.is_running():
            task.wait(60_000)


class PlanPage(QWidget):
    """The Plan page: the plan half and the undo half behind one switch (design §9)."""

    statusMessage = Signal(str)
    """One-line message for the app's status bar."""

    goToOpportunities = Signal()

    aiChanged = Signal()
    """A review ran: the shell re-reads the AI status and the meter (design §10)."""

    def __init__(
        self,
        data_root: Path | None = None,
        *,
        db_path: Path | None = None,
        settings: state.Settings | None = None,
        ai: AIService | None = None,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self.setObjectName("Page")
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, theme.SPACE["md"], 0, 0)
        layout.setSpacing(0)

        switcher = QWidget(self)
        switch_row = QHBoxLayout(switcher)
        switch_row.setContentsMargins(theme.SPACE["xl"], 0, theme.SPACE["xl"], 0)
        switch_row.addStretch(1)
        self.switch = widgets.Segmented(
            ("Plan", "Undo"),
            icons_by_label={"Plan": "clipboard-list", "Undo": "refresh-cw"},
            parent=switcher,
        )
        self.switch.setToolTip("Plan and its journal history share this page")
        switch_row.addWidget(self.switch)
        layout.addWidget(switcher)

        self.stack = QStackedWidget(self)
        self.plan = PlanView(
            data_root, db_path=db_path, settings=settings, ai=ai, parent=self.stack
        )
        self.undo = UndoView(data_root, parent=self.stack)
        self.stack.addWidget(self.plan)
        self.stack.addWidget(self.undo)
        layout.addWidget(self.stack, 1)

        self.switch.changed.connect(self._on_switch)
        self.plan.statusMessage.connect(self.statusMessage.emit)
        self.undo.statusMessage.connect(self.statusMessage.emit)
        self.plan.goToUndo.connect(lambda: self.show_undo())
        self.undo.goToPlan.connect(lambda: self.show_plan())
        self.plan.goToOpportunities.connect(self.goToOpportunities.emit)
        self.plan.built.connect(self._on_built)
        self.plan.aiChanged.connect(self.aiChanged.emit)

    # -- navigation ------------------------------------------------------- #

    def show_plan(self) -> None:
        """Show the plan half."""
        self.switch.select(0, emit=False)
        self.stack.setCurrentWidget(self.plan)

    def show_undo(self) -> None:
        """Show the undo half (and re-read the journals: a run may just have happened)."""
        self.switch.select(1, emit=False)
        self.stack.setCurrentWidget(self.undo)
        self.undo.refresh()

    def current(self) -> str:
        """``plan`` or ``undo`` -- the half on screen."""
        return "undo" if self.stack.currentWidget() is self.undo else "plan"

    def _on_switch(self, index: int) -> None:
        if index == 1:
            self.show_undo()
        else:
            self.show_plan()

    def _on_built(self, session: object) -> None:
        """A fresh plan arrived: the switch belongs on the plan half."""
        if isinstance(session, planning.PlanSession):
            self.show_plan()
            self.statusMessage.emit(
                f"Plan {session.plan_id} drafted: {len(session.draft.items)} action"
                f"{'' if len(session.draft.items) == 1 else 's'} · "
                f"{stats.format_bytes(sum(item.action.bytes for item in session.draft.items))} "
                "in play."
            )

    # -- lifecycle -------------------------------------------------------- #

    def apply_theme(self) -> None:
        """Re-tint the switch and both halves after a theme change."""
        self.switch.apply_theme()
        self.plan.apply_theme()
        self.undo.apply_theme()

    def shutdown(self) -> None:
        """Wait for anything still running on either half."""
        self.plan.shutdown()
        self.undo.shutdown()
