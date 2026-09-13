"""The Plan screen: select -> plan -> dry-run -> execute -> undo (design §9, screen 3).

The fixtures here are live (:mod:`fixtures.gen_live` plants a tree and exports
the WizTree CSV describing it), so "execute ran the plan" and "undo restored the
tree" are assertions about the filesystem, not about the widgets' opinion of it.

Both routes into the screen are covered: a focused :class:`PlanView` driven with
an engine-built session, and the whole app -- check rows in the ranked list,
press *Build plan*, run, then revert from the Undo half of the page.
"""

from __future__ import annotations

import dataclasses
import json
from collections.abc import Sequence
from pathlib import Path
from typing import TYPE_CHECKING, Any

import pytest
from PySide6.QtCore import QPoint, Qt
from PySide6.QtGui import QFontMetrics
from PySide6.QtWidgets import QDialog, QPushButton

from fixtures import gen_live
from spacesage import executor, planner, planning, rules, stats
from spacesage.app import dialogs, plan_models, state, theme, widgets
from spacesage.app.views.plan_view import PlanPage, PlanView
from spacesage.app.windows import MainWindow

if TYPE_CHECKING:  # the fixtures live in conftest; only the type is needed here
    from conftest import LiveSandbox


def settle(qtbot: object, ms: int = 260) -> None:
    """Let layout, paint and the entrance fade happen."""
    qtbot.wait(ms)  # type: ignore[attr-defined]


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #


def draft_session(sandbox: LiveSandbox, paths: Sequence[str] | None = None) -> planning.PlanSession:
    """Draft and persist a session for the sandbox (what *Build plan* runs)."""
    request = planning.PlanRequest.from_listing(
        sandbox.listing,
        list(sandbox.selection() if paths is None else paths),
        targets=(sandbox.target_spec(),),
    )
    return planning.open_session(
        sandbox.db_path,
        rules.load_rules(include_user=False),
        request,
        root=sandbox.data_root,
        quarantine_root=sandbox.quarantine,
    )


def settings_for(sandbox: LiveSandbox, tmp_path: Path) -> state.Settings:
    """Persisted settings pointing at the sandbox's target drive and quarantine."""
    settings = state.Settings.persisted(tmp_path / "settings.ini")
    settings.set_target_drive(str(sandbox.target))
    settings.set_reserve_bytes(0)
    settings.set_quarantine_dir(str(sandbox.quarantine))
    return settings


def open_view(
    sandbox: LiveSandbox, tmp_path: Path, qtbot: object, *, session: planning.PlanSession | None
) -> PlanView:
    """A shown PlanView over the sandbox, already holding a session when given one."""
    view = PlanView(
        sandbox.data_root,
        db_path=sandbox.db_path,
        settings=settings_for(sandbox, tmp_path),
    )
    qtbot.addWidget(view)  # type: ignore[attr-defined]
    view.resize(1280, 820)
    view.show()
    if session is not None:
        view.set_session(session)
    settle(qtbot)
    return view


def gate(view: PlanView, qtbot: object) -> None:
    """Let every worker thread finish and the painted widgets settle."""
    qtbot.waitUntil(lambda: not view.busy(), timeout=30_000)  # type: ignore[attr-defined]
    qtbot.wait(60)  # type: ignore[attr-defined]


def install_confirm(monkeypatch: pytest.MonkeyPatch, *, accepted: bool) -> list[dict[str, Any]]:
    """Replace the confirmation dialog; returns the arguments of every dialog shown."""
    shown: list[dict[str, Any]] = []

    class FakeConfirm:
        def __init__(self, title: str, headline: str, **kwargs: Any) -> None:
            shown.append({"title": title, "headline": headline, **kwargs})

        def exec(self) -> int:
            return int(QDialog.DialogCode.Accepted if accepted else QDialog.DialogCode.Rejected)

    monkeypatch.setattr(dialogs, "ConfirmDialog", FakeConfirm)
    return shown


def install_preview(monkeypatch: pytest.MonkeyPatch) -> list[Any]:
    """Replace the preview dialog; returns the reports it was asked to render."""
    shown: list[Any] = []

    class FakePreview:
        def __init__(self, report: executor.ApplyReport, **kwargs: Any) -> None:
            shown.append(report)

        def exec(self) -> int:
            return int(QDialog.DialogCode.Accepted)

    monkeypatch.setattr(dialogs, "PreviewDialog", FakePreview)
    return shown


def status_of(view: PlanView, action_id: str) -> str:
    """The status column's text for one action."""
    model = view.table_model()
    index = model.index_of(action_id)
    assert index.isValid()
    return str(
        model.index(index.row(), plan_models.COLUMN_STATUS).data(Qt.ItemDataRole.DisplayRole)
    )


def outcomes(view: PlanView) -> dict[str, str]:
    """``{action id: outcome}`` of the last run the view adopted."""
    report = view.apply_report()
    assert report is not None
    return {op.action_id: op.outcome for op in report.ops}


# --------------------------------------------------------------------------- #
# The empty screen
# --------------------------------------------------------------------------- #


def test_the_empty_state_points_at_the_ranked_list(qtbot: object, tmp_path: Path) -> None:
    """Before a plan exists: no rows, no actions, and one way forward."""
    view = PlanView(tmp_path / "data", db_path=tmp_path / "index.db")
    qtbot.addWidget(view)  # type: ignore[attr-defined]
    view.show()
    settle(qtbot)

    assert view.session() is None
    assert view.draft() is None
    assert view.table_model().rowCount() == 0
    assert view.stack.currentWidget() is view.empty_state
    assert not view.execute_button.isEnabled()
    assert not view.preview_button.isEnabled()
    assert not view.approve_all_button.isEnabled()

    button = view.empty_state.findChild(QPushButton)
    assert button is not None and button.text() == "Go to Opportunities"
    seen: list[bool] = []
    view.goToOpportunities.connect(lambda: seen.append(True))
    button.click()
    assert seen == [True]


# --------------------------------------------------------------------------- #
# Building from the checked rows
# --------------------------------------------------------------------------- #


def test_building_from_the_list_lands_on_the_plan_page(
    live_sandbox: LiveSandbox,
    sandbox_window: Any,
    qtbot: object,
) -> None:
    """The loop's first leg: check rows, press *Build plan*, get one plan."""
    window: MainWindow = sandbox_window(live_sandbox)
    assert window.navigate("opportunities")
    opportunities_view = window.opportunities_view
    model = opportunities_view.table_model()

    # Nothing is checked yet: the action bar says so and offers nothing.
    assert not opportunities_view.build_plan_button.isEnabled()
    assert opportunities_view.selection_label.text() == "No rows checked yet"

    for path in live_sandbox.selection():
        model.toggle(path)
    assert len(model.selection) == len(live_sandbox.selection())
    assert opportunities_view.build_plan_button.isEnabled()
    checked = opportunities_view.selection_label.full_text()
    assert checked.startswith(f"{len(live_sandbox.selection())} checked")
    assert "estimated gain" in checked

    opportunities_view.build_plan_button.click()
    plan_view = window.plan_page.plan
    gate(plan_view, qtbot)

    assert window.current_page() == "plan"
    session = plan_view.session()
    assert session is not None
    draft = session.draft
    assert plan_view.table_model().rowCount() == len(draft.items) > 0
    assert plan_view.plan_id_label.text() == f"plan {session.plan_id}"
    assert str(session.workspace.directory) in plan_view.workspace_label.text()

    # The workspace is the plan's own: the document and a bound approval.
    assert session.workspace.plan_path.is_file()
    approval = json.loads(session.workspace.manifest_path.read_text(encoding="utf-8"))
    assert approval["plan_id"] == session.plan_id
    assert tuple(approval["approved"]) == draft.executable_ids()
    assert approval["rejected"] == list(draft.advisory_ids())
    assert not session.workspace.has_journal()  # nothing was executed

    # Every item the draft refuses is refused for a reason the screen shows.
    for item in draft.items:
        if item.status == "refused":
            assert item.reason
    assert plan_view.warnings.count() == len(draft.warnings)


def test_the_folder_row_covers_its_children_in_the_plan(
    live_sandbox: LiveSandbox, sandbox_window: Any, qtbot: object
) -> None:
    """Checking a folder plans the folder's actions once -- its children never double up."""
    window: MainWindow = sandbox_window(live_sandbox)
    window.navigate("opportunities")
    model = window.opportunities_view.table_model()
    folder = str(live_sandbox.live.tree / "app" / "node_modules")
    model.toggle(folder)
    assert len(model.selection) == 1

    window.opportunities_view.build_plan_button.click()
    plan_view = window.plan_page.plan
    gate(plan_view, qtbot)
    draft = plan_view.draft()
    assert draft is not None
    planned = [item.action.path for item in draft.items]
    assert folder in planned
    # The folder's own action is the plan's entry: nothing below it is planned again.
    assert not any(path.startswith(folder + "/") for path in planned)
    folder_item = next(item for item in draft.items if item.action.path == folder)
    assert plan_view.table_model().index_of(folder_item.action.id).isValid()


# --------------------------------------------------------------------------- #
# Approval
# --------------------------------------------------------------------------- #


def test_taking_an_item_out_rewrites_the_bound_approval(
    live_sandbox: LiveSandbox, tmp_path: Path, qtbot: object
) -> None:
    """Approval is per item, and the file on disk always says what the user decided."""
    session = draft_session(live_sandbox)
    view = open_view(live_sandbox, tmp_path, qtbot, session=session)
    model = view.table_model()
    victim = session.draft.executable_ids()[0]

    assert view.set_approved(victim, False)
    approval = json.loads(session.workspace.manifest_path.read_text(encoding="utf-8"))
    assert victim not in approval["approved"]
    assert victim in approval["rejected"]
    assert view.approved_ids() == tuple(
        action_id for action_id in session.draft.executable_ids() if action_id != victim
    )
    assert f"{len(session.draft.executable_ids()) - 1} of" in view.approval_label.text()

    # Approving it again puts it back (the manifest follows every click).
    assert view.set_approved(victim, True)
    approval = json.loads(session.workspace.manifest_path.read_text(encoding="utf-8"))
    assert victim in approval["approved"]
    assert victim not in approval["rejected"]

    # Take everything out: the plan stays a record, and nothing can run.
    view.reject_all()
    assert view.approved_ids() == ()
    assert not view.execute_button.isEnabled()
    assert not view.preview_button.isEnabled()
    assert "0 of" in view.approval_label.text()

    view.approve_all()
    assert view.approved_ids() == session.draft.executable_ids()
    assert view.execute_button.isEnabled()
    assert model.approved_count() == len(session.draft.executable_ids())


def test_advice_items_can_never_be_approved(
    live_sandbox: LiveSandbox, tmp_path: Path, qtbot: object
) -> None:
    """``REVIEW``/``NATIVE`` items are advice: the screen refuses to approve them."""
    session = draft_session(live_sandbox)
    advice = session.draft.advisory_ids()
    assert advice, "the sandbox selection carries an advice row (archive/setup.msi)"
    view = open_view(live_sandbox, tmp_path, qtbot, session=session)

    for action_id in advice:
        assert not view.set_approved(action_id, True)
        assert action_id not in view.approved_ids()
    model = view.table_model()
    index = model.index_of(advice[0])
    state_value = index.sibling(index.row(), plan_models.COLUMN_APPROVE).data(
        Qt.ItemDataRole.UserRole + 1
    )
    assert state_value == "advice"
    assert status_of(view, advice[0]) == "Advice only"


# --------------------------------------------------------------------------- #
# Dry run
# --------------------------------------------------------------------------- #


def test_the_dry_run_resolves_everything_and_touches_nothing(
    live_sandbox: LiveSandbox, tmp_path: Path, qtbot: object, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The preview shows the resolved operations; the tree is byte-identical after it."""
    session = draft_session(live_sandbox)
    view = open_view(live_sandbox, tmp_path, qtbot, session=session)
    before = gen_live.snapshot(live_sandbox.live.tree)
    dialogs_shown = install_preview(monkeypatch)

    assert view.dry_run(show=True)
    gate(view, qtbot)

    report = view.preview_report()
    assert report is not None and report.ok(), report.render_text()
    assert len(report.ops) == len(session.draft.executable_ids())
    assert all(op.outcome == "planned" for op in report.ops), report.render_text()
    assert all(op.steps for op in report.ops)  # each one resolved into real steps
    assert view.attention() == ()
    assert gen_live.snapshot(live_sandbox.live.tree) == before  # nothing was touched
    assert not session.workspace.has_journal()  # a dry run journals nothing

    # The dialog was handed the very report the screen adopted.
    assert len(dialogs_shown) == 1 and dialogs_shown[0] is report
    # And every row now says what would happen to it.
    for op in report.ops:
        assert status_of(view, op.action_id) == "Planned"
    assert view.execute_button.isEnabled()  # executing stays available
    assert view.preview_report() is report


def test_the_preview_dialog_itemizes_every_operation(
    live_sandbox: LiveSandbox, tmp_path: Path, qtbot: object
) -> None:
    """The dialog lists one line per resolved operation, with its destination."""
    session = draft_session(live_sandbox)
    view = open_view(live_sandbox, tmp_path, qtbot, session=session)
    assert view.dry_run(show=False)
    gate(view, qtbot)
    report = view.preview_report()
    assert report is not None

    dialog = dialogs.PreviewDialog(report)
    qtbot.addWidget(dialog)  # type: ignore[attr-defined]
    lines = dialog.lines()
    assert lines
    text = "\n".join(lines)
    for op in report.ops:
        assert op.action_id in text
        assert op.path in text
    moved = [op for op in report.ops if op.type == "MOVE"]
    if moved:  # pragma: no cover - the sandbox has a move candidate
        assert str(live_sandbox.target) in text
    assert dialog.report() is report


# --------------------------------------------------------------------------- #
# Execution
# --------------------------------------------------------------------------- #


def test_execute_runs_behind_an_itemized_confirmation(
    live_sandbox: LiveSandbox, tmp_path: Path, qtbot: object, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The confirmation names every action; the run reports each of them back."""
    session = draft_session(live_sandbox)
    view = open_view(live_sandbox, tmp_path, qtbot, session=session)
    before = gen_live.snapshot(live_sandbox.live.tree)
    shown = install_confirm(monkeypatch, accepted=True)

    assert view.execute()
    gate(view, qtbot)

    # The dialog: itemized, danger-styled, and it says what cannot be undone.
    assert len(shown) == 1
    dialog = shown[0]
    assert dialog["title"] == "Execute the plan"
    assert dialog["danger"] is True
    assert len(dialog["lines"]) == len(session.draft.executable_ids())
    itemized = "\n".join(dialog["lines"])
    for item in view.table_model().approved_items():
        assert item.action.path in itemized
        assert plan_models.action_label(item) in itemized
    assert "journal" in dialog["detail"]

    report = view.apply_report()
    assert report is not None and report.ok(), report.render_text()
    assert outcomes(view) == dict.fromkeys(session.draft.executable_ids(), "done")
    for action_id in session.draft.executable_ids():
        assert status_of(view, action_id) == "Done"
    assert gen_live.snapshot(live_sandbox.live.tree) != before  # the tree really changed

    # The run is journaled in this plan's workspace, which is what Undo reads.
    # A journal holds one entry per primitive operation, so a move that also plants
    # a link records more than the plan has actions.
    assert session.workspace.has_journal()
    history = planning.journal_history(session.workspace.journal_path)
    assert history.plan_id == session.plan_id
    assert len(history.pending()) >= len(session.draft.executable_ids())
    assert {item.action_id for item in history.pending()} == set(session.draft.executable_ids())

    assert view.result_label.isVisible()
    assert "reclaimed" in view.result_label.text()
    assert view.attention() == ()
    assert view.attention_list.count() == 0


def test_the_real_confirmation_dialog_itemizes_the_run(
    live_sandbox: LiveSandbox, tmp_path: Path, qtbot: object, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The dialog itself (not a stand-in): what it renders, and how it is styled.

    Every other execution test replaces the dialog, which is how a confirmation
    that lists nothing (or is styled like a friendly question) could ship green.
    """
    session = draft_session(live_sandbox)
    view = open_view(live_sandbox, tmp_path, qtbot, session=session)
    seen: list[dialogs.ConfirmDialog] = []

    class Capturing(dialogs.ConfirmDialog):
        """The real dialog, captured and dismissed (a modal box would hang)."""

        def __init__(self, *args: Any, **kwargs: Any) -> None:
            super().__init__(*args, **kwargs)
            seen.append(self)

        def exec(self) -> int:
            return int(QDialog.DialogCode.Rejected)

    monkeypatch.setattr(dialogs, "ConfirmDialog", Capturing)
    assert view.execute() is False  # dismissed: nothing may have run
    assert len(seen) == 1
    dialog = seen[0]
    qtbot.addWidget(dialog)  # type: ignore[attr-defined]
    dialog.show()
    settle(qtbot)

    lines = dialog.lines()
    assert len(lines) == len(session.draft.executable_ids())
    itemized = "\n".join(lines)
    for item in view.table_model().approved_items():
        assert item.action.path in itemized
        assert plan_models.action_label(item) in itemized
    # A run is destructive: danger styling, and the button says what it does.
    assert dialog.windowTitle() == "Execute the plan"
    assert dialog.confirm_button().text() == "Execute"
    assert dialog.confirm_button().objectName() == "Danger"
    assert view.apply_report() is None  # nothing ran: no report was adopted


def test_space_toggles_the_row_under_the_cursor(
    live_sandbox: LiveSandbox, tmp_path: Path, qtbot: object
) -> None:
    """Keyboard: Space approves or takes out the current row (design §9.1)."""
    session = draft_session(live_sandbox)
    view = open_view(live_sandbox, tmp_path, qtbot, session=session)
    model = view.table_model()
    table = view.table()
    victim = session.draft.executable_ids()[0]

    table.setCurrentIndex(model.index_of(victim))
    qtbot.keyClick(table, Qt.Key.Key_Space)  # type: ignore[attr-defined]
    assert victim not in view.approved_ids()
    qtbot.keyClick(table, Qt.Key.Key_Space)  # type: ignore[attr-defined]
    assert victim in view.approved_ids()

    # An advice row cannot be approved by keyboard either, and it says so.
    advice = session.draft.advisory_ids()[0]
    messages: list[str] = []
    view.statusMessage.connect(messages.append)
    table.setCurrentIndex(model.index_of(advice))
    qtbot.keyClick(table, Qt.Key.Key_Space)  # type: ignore[attr-defined]
    assert messages and "advice only" in messages[-1]
    assert advice not in view.approved_ids()


def test_the_detail_column_elides_at_whole_words(
    live_sandbox: LiveSandbox, tmp_path: Path, qtbot: object
) -> None:
    """*What happens* is a sentence, never a value cut through the middle.

    The default cell rendering elides in the middle, which turned the move's
    "moves to D:\\… (Developer Mode) · needs elevation" into a line whose
    destination nobody can read (the operator's QA note on the S8 renders).
    """
    session = draft_session(live_sandbox)
    view = open_view(live_sandbox, tmp_path, qtbot, session=session)
    table = view.table()
    model = view.table_model()
    assert isinstance(
        table.itemDelegateForColumn(plan_models.COLUMN_DETAIL), plan_models.PlanDetailDelegate
    )
    metrics = QFontMetrics(theme.ui_font("sm"))
    for row in range(model.rowCount()):
        index = model.index(row, plan_models.COLUMN_DETAIL)
        item = session.draft.items[row]
        assert index.data(Qt.ItemDataRole.DisplayRole) == item.detail
        assert item.detail in str(index.data(Qt.ItemDataRole.ToolTipRole))  # never lost
        for width in (120, 240, 380, 900):
            line = plan_models.detail_line(item, width, metrics)
            if line == item.detail:
                continue
            assert line.endswith("…")
            cut = line[:-1]
            assert item.detail.startswith(cut)
            assert item.detail[len(cut)] == " "  # the cut lands between two words


def test_execute_is_not_run_when_the_confirmation_is_dismissed(
    live_sandbox: LiveSandbox, tmp_path: Path, qtbot: object, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Dismissing the dialog really cancels: nothing runs, nothing is journaled."""
    session = draft_session(live_sandbox)
    view = open_view(live_sandbox, tmp_path, qtbot, session=session)
    before = gen_live.snapshot(live_sandbox.live.tree)
    install_confirm(monkeypatch, accepted=False)

    assert not view.execute()
    gate(view, qtbot)
    assert view.apply_report() is None
    assert gen_live.snapshot(live_sandbox.live.tree) == before
    assert not session.workspace.has_journal()


def test_a_toast_never_covers_the_action_bar(
    live_sandbox: LiveSandbox, tmp_path: Path, qtbot: object
) -> None:
    """The background result floats above the bar whose buttons it reports on (§9.1)."""
    session = draft_session(live_sandbox)
    view = open_view(live_sandbox, tmp_path, qtbot, session=session)
    settle(qtbot)

    toast = widgets.Toast.pop_up(
        view, "Executed 4 of 4 approved actions · 19.0 MiB reclaimed.", above=view.toolbar
    )
    try:
        assert toast.isVisible()
        # One coordinate space: where the bar starts, seen from the page.
        toolbar_top = view.toolbar.mapTo(view, QPoint(0, 0)).y()
        assert toast.geometry().bottom() < toolbar_top, (
            "the toast must not sit on Execute / Dry-run preview / Undo"
        )
    finally:
        toast.close()


def test_a_second_result_replaces_the_toast_on_screen(
    live_sandbox: LiveSandbox, tmp_path: Path, qtbot: object
) -> None:
    """Two toasts land in the same place: the newest one wins, nothing stacks."""
    session = draft_session(live_sandbox)
    view = open_view(live_sandbox, tmp_path, qtbot, session=session)
    settle(qtbot)

    first = widgets.Toast.pop_up(view, "Dry run: 4 actions resolved.")
    second = widgets.Toast.pop_up(view, "Executed 4 of 4 approved actions.", above=view.toolbar)
    try:
        assert not first.isVisible()
        assert second.isVisible()
    finally:
        second.close()


def test_background_results_arrive_as_toasts(
    live_sandbox: LiveSandbox, tmp_path: Path, qtbot: object, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Every background result lands on one channel, toned by what it says (§9.1)."""
    session = draft_session(live_sandbox)
    view = open_view(live_sandbox, tmp_path, qtbot, session=session)
    seen: list[tuple[str, str]] = []

    def capture(_parent: object, message: str, *, tone: str = "info", above: object = None) -> None:
        seen.append((message, tone))

    monkeypatch.setattr(dialogs, "toast", capture)

    assert view.dry_run(show=False)
    gate(view, qtbot)
    assert seen and seen[-1][1] == "info"
    assert "Dry run" in seen[-1][0] and "nothing on disk was touched" in seen[-1][0]

    # A clean run is a success; a run that had to skip is a warning, not a success.
    seen.clear()
    install_confirm(monkeypatch, accepted=True)
    assert view.execute()
    gate(view, qtbot)
    assert seen and seen[-1][1] == "success"
    assert "reclaimed" in seen[-1][0]

    seen.clear()
    install_confirm(monkeypatch, accepted=True)
    assert view.execute()
    gate(view, qtbot)
    assert seen and seen[-1][1] == "warning"  # the paths are gone: skipped, not "done"
    assert "skipped" in seen[-1][0]


def test_the_workspace_path_is_elided_never_clipped(
    live_sandbox: LiveSandbox, tmp_path: Path, qtbot: object
) -> None:
    """A long workspace path is elided (with its tail kept), never painted past the edge."""
    session = draft_session(live_sandbox)
    view = open_view(live_sandbox, tmp_path, qtbot, session=session)
    label = view.workspace_label
    full = str(session.workspace.directory)
    assert label.full_text() == full
    fitted = label.fontMetrics().elidedText(
        full, Qt.TextElideMode.ElideMiddle, label.contentsRect().width()
    )
    assert label.text() in {full, fitted}

    # Squeezed to a sliver it still paints something that fits, and keeps the tail
    # (this plan's own id) so the path stays identifiable.
    label.resize(120, label.height())
    settle(qtbot)
    narrow = label.text()
    assert label.fontMetrics().horizontalAdvance(narrow) <= label.contentsRect().width()
    assert full.endswith(narrow[-8:])


def test_a_vanished_file_is_surfaced_as_skipped(
    live_sandbox: LiveSandbox, tmp_path: Path, qtbot: object, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An operation the engine had to skip is reported, never quietly dropped."""
    session = draft_session(live_sandbox)
    view = open_view(live_sandbox, tmp_path, qtbot, session=session)
    target = next(
        item
        for item in session.draft.items
        if item.executable and item.action.path.endswith("old.dmp")
    )
    Path(target.action.path).unlink()  # the world changed under the plan
    install_confirm(monkeypatch, accepted=True)

    assert view.execute()
    gate(view, qtbot)

    skipped = [entry for entry in view.attention() if entry[1] == "skipped"]
    assert skipped and skipped[0][0] == target.action.id
    assert "no longer exists" in skipped[0][2]
    assert view.attention_list.count() >= 1
    assert status_of(view, target.action.id) == "Skipped"
    assert "skipped" in view.result_label.text()
    assert outcomes(view)[target.action.id] == "skipped"


def tampered_tier(session: planning.PlanSession) -> tuple[planning.PlanSession, str]:
    """The same plan, with one executable action's tier turned report-only."""
    victim = session.draft.items[0]
    replaced = dataclasses.replace(victim.action, tier=planner.REPORT_TIER)
    return _with_actions(session, {replaced.id: replaced}), "report-only"


def tampered_destination(session: planning.PlanSession) -> tuple[planning.PlanSession, str]:
    """The same plan, with a move pointed outside the drives the plan declares."""
    move = next(item for item in session.draft.items if item.action.type == "MOVE")
    replaced = dataclasses.replace(move.action, dest=str(move.action.path) + ".elsewhere")
    return _with_actions(session, {replaced.id: replaced}), "not under any declared target"


def _with_actions(
    session: planning.PlanSession, changes: dict[str, planner.PlanAction]
) -> planning.PlanSession:
    """A session whose plan holds ``changes`` instead of the actions they replace."""
    plan = dataclasses.replace(
        session.draft.plan,
        actions=tuple(changes.get(action.id, action) for action in session.draft.plan.actions),
    )
    return planning.PlanSession(
        draft=dataclasses.replace(session.draft, plan=plan), workspace=session.workspace
    )


@pytest.mark.parametrize(
    "tamper", [tampered_tier, tampered_destination], ids=["tier-mismatch", "tampered-plan"]
)
def test_a_refused_plan_is_reported_and_nothing_runs(
    live_sandbox: LiveSandbox,
    tmp_path: Path,
    qtbot: object,
    monkeypatch: pytest.MonkeyPatch,
    no_blocking_dialogs: list[tuple[str, str]],
    tamper: Any,
) -> None:
    """The executor validates the plan document before anything runs.

    Both refusals the acceptance names -- a tier the plan may not act on, and a
    plan document that was tampered with -- stop the run, and the screen shows the
    engine's own sentence instead of quietly doing nothing.
    """
    session, needle = tamper(draft_session(live_sandbox))
    view = open_view(live_sandbox, tmp_path, qtbot, session=session)
    before = gen_live.snapshot(live_sandbox.live.tree)
    install_confirm(monkeypatch, accepted=True)

    assert view.execute()  # the run was asked for ...
    gate(view, qtbot)

    assert view.apply_report() is None  # ... and refused
    assert needle in view.last_error
    assert "The execution failed" in [title for title, _message in no_blocking_dialogs]
    assert any(needle in message for _title, message in no_blocking_dialogs)
    assert gen_live.snapshot(live_sandbox.live.tree) == before  # not one byte moved
    assert not session.workspace.has_journal()


def test_a_tampered_workspace_is_reported_not_crashed(
    live_sandbox: LiveSandbox,
    tmp_path: Path,
    qtbot: object,
    monkeypatch: pytest.MonkeyPatch,
    no_blocking_dialogs: list[tuple[str, str]],
) -> None:
    """A workspace that belongs to another plan stops the run with the engine's sentence."""
    session = draft_session(live_sandbox)
    foreign = planning.PlanWorkspace(
        plan_id="sha256:" + "0" * 64, directory=session.workspace.directory
    )
    session = planning.PlanSession(draft=session.draft, workspace=foreign)
    view = open_view(live_sandbox, tmp_path, qtbot, session=session)
    before = gen_live.snapshot(live_sandbox.live.tree)
    install_confirm(monkeypatch, accepted=True)

    assert view.execute()
    gate(view, qtbot)

    assert "The execution failed" in [title for title, _message in no_blocking_dialogs]
    assert "workspace" in view.last_error
    assert view.apply_report() is None
    assert gen_live.snapshot(live_sandbox.live.tree) == before  # nothing ran
    assert not session.workspace.has_journal()


# --------------------------------------------------------------------------- #
# The page: plan and its journals
# --------------------------------------------------------------------------- #


def test_the_page_switches_between_the_plan_and_its_journals(
    live_sandbox: LiveSandbox, tmp_path: Path, qtbot: object
) -> None:
    """Plan and Undo share the page behind one switch, and each half links to the other."""
    page = PlanPage(
        live_sandbox.data_root,
        db_path=live_sandbox.db_path,
        settings=settings_for(live_sandbox, tmp_path),
    )
    qtbot.addWidget(page)  # type: ignore[attr-defined]
    page.resize(1280, 820)
    page.show()
    settle(qtbot)

    assert page.current() == "plan"
    assert page.switch.current() == 0
    page.plan.goToUndo.emit()
    settle(qtbot)
    assert page.current() == "undo"
    assert page.switch.current() == 1
    page.undo.goToPlan.emit()
    settle(qtbot)
    assert page.current() == "plan"
    assert page.switch.current() == 0

    # The switch itself is keyboard-reachable and drives the same navigation.
    page.switch.select(1)
    assert page.current() == "undo"
    page.switch.select(0)
    assert page.current() == "plan"

    seen: list[bool] = []
    page.goToOpportunities.connect(lambda: seen.append(True))
    page.plan.goToOpportunities.emit()
    assert seen == [True]


def test_a_theme_change_re_renders_the_plan(
    live_sandbox: LiveSandbox, tmp_path: Path, qtbot: object, qapp: object
) -> None:
    """Both themes re-tint the badges and the table without losing the plan."""
    session = draft_session(live_sandbox)
    view = open_view(live_sandbox, tmp_path, qtbot, session=session)
    manager = theme.ThemeManager(qapp, mode=theme.MODE_DARK)
    manager.set_mode("dark")
    view.apply_theme()
    settle(qtbot)
    assert view.table_model().rowCount() == len(session.draft.items)
    manager.set_mode("light")
    view.apply_theme()
    settle(qtbot)
    assert view.table_model().rowCount() == len(session.draft.items)


# --------------------------------------------------------------------------- #
# The whole loop
# --------------------------------------------------------------------------- #


def test_the_full_loop_executes_and_undo_restores_the_tree(
    live_sandbox: LiveSandbox,
    sandbox_window: Any,
    qtbot: object,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Select -> plan -> dry-run -> execute -> undo, and the tree comes back byte for byte."""
    window: MainWindow = sandbox_window(live_sandbox)
    before = gen_live.snapshot(live_sandbox.live.tree)

    # 1. Select the opportunities in the ranked list.
    window.navigate("opportunities")
    model = window.opportunities_view.table_model()
    for path in live_sandbox.selection():
        model.toggle(path)
    assert len(model.selection) == len(live_sandbox.selection())
    selected_bytes = model.selection.gain
    assert selected_bytes > 0
    assert stats.format_bytes(selected_bytes) in (
        window.opportunities_view.selection_label.full_text()
    )

    # 2. Build one plan from them.
    window.opportunities_view.build_plan_button.click()
    plan_view = window.plan_page.plan
    gate(plan_view, qtbot)
    assert window.current_page() == "plan"
    session = plan_view.session()
    assert session is not None
    executable = session.draft.executable_ids()
    assert executable
    assert plan_view.approved_ids() == executable

    # The advice row rides along in the plan, marked as advice, never approvable.
    advice = session.draft.advisory_ids()
    assert advice, "the sandbox selection carries an advice row"
    assert not set(advice) & set(executable)
    for action_id in advice:
        assert status_of(plan_view, action_id) == "Advice only"

    # 3. The dry run resolves everything and touches nothing.
    assert plan_view.dry_run(show=False)
    gate(plan_view, qtbot)
    report = plan_view.preview_report()
    assert report is not None and report.ok()
    assert gen_live.snapshot(live_sandbox.live.tree) == before

    # 4. Execute, behind the confirmation.
    shown = install_confirm(monkeypatch, accepted=True)
    assert plan_view.execute()
    gate(plan_view, qtbot)
    assert len(shown) == 1
    applied = plan_view.apply_report()
    assert applied is not None and applied.ok(), applied.render_text()
    assert {op.action_id for op in applied.ops} <= set(executable)  # advice never ran
    assert gen_live.snapshot(live_sandbox.live.tree) != before
    journal = session.workspace.journal_path
    assert journal.is_file()
    assert planning.journal_history(journal).pending()

    # 5. Undo, from the other half of the page: one click reverts the whole run.
    page = window.plan_page
    page.show_undo()
    gate_undo(page.undo, qtbot)
    assert page.current() == "undo"
    history = page.undo.current_history()
    assert history is not None and history.path == journal
    assert len(history.pending()) >= len(applied.ops)  # one entry per primitive operation
    assert page.undo.revert_all_button.isEnabled()

    install_confirm(monkeypatch, accepted=True)
    page.undo.revert_all_button.click()
    gate_undo(page.undo, qtbot)

    assert gen_live.snapshot(live_sandbox.live.tree) == before  # byte for byte
    after = planning.journal_history(journal)
    assert not after.pending()
    assert after.counts()["reversed"] == len(after.items)
    assert after.restored_bytes() == history.reclaimed_bytes()


def gate_undo(view: object, qtbot: object) -> None:
    """Let the Undo screen's workers finish."""
    qtbot.waitUntil(lambda: not view.busy(), timeout=30_000)  # type: ignore[attr-defined]
    qtbot.wait(80)  # type: ignore[attr-defined]


def test_the_footer_reports_the_approval_in_bytes(
    live_sandbox: LiveSandbox, tmp_path: Path, qtbot: object
) -> None:
    """The toolbar's figure is the approved set's own sum, in the app's byte format."""
    session = draft_session(live_sandbox)
    view = open_view(live_sandbox, tmp_path, qtbot, session=session)
    approved = sum(
        item.action.bytes for item in session.draft.items if item.action.id in view.approved_ids()
    )
    assert stats.format_bytes(approved) in view.cards["reclaim"].value()
    summary = view.table_model().selected_summary()
    assert summary.startswith(f"{len(view.approved_ids())} of")
    assert "to reclaim" in summary
    assert isinstance(view.warnings, widgets.WarningList)
    assert view.cards["approved"].value() == str(len(view.approved_ids()))
