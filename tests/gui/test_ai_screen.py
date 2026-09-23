"""(S10) The AI layer in the app: the fill, the column, promotion and review (design §10).

The only AI in this module is the scripted provider in :mod:`tests.ai_stub`: it
speaks the OpenAI protocol over loopback and answers from a queue, so "the app
asked, then showed what came back" is an assertion about real HTTP traffic.

What is being pinned down here is the *seam*: an AI answer fills a column the
rules left undecided, it is never executable on its own, the only door between
it and the executor is *Apply as rule…*, and everything the screens say about an
answer names who produced it.
"""

from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path
from typing import TYPE_CHECKING, Any

import pytest
from PySide6.QtCore import Qt
from PySide6.QtWidgets import QDialog

from ai_stub import (
    StubServer,
    annotation_entry,
    classification_answer,
    classification_entry,
    explanation_answer,
    item,
    review_answer,
    suggestion_answer,
)
from spacesage import opportunities, rules
from spacesage.ai import promote
from spacesage.ai.config import AIConfig
from spacesage.app import ai_models, dialogs, models
from spacesage.app.views.plan_view import PlanView

if TYPE_CHECKING:  # the fixtures live in conftest; only the type is needed here
    from conftest import LiveSandbox
    from spacesage.app.windows import MainWindow

AI_ACTION = "DELETE_QUARANTINE"
AI_WHY = "a stale installer for a program that is no longer installed"


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #


def install_confirm(
    monkeypatch: pytest.MonkeyPatch, *, accepted: bool = True
) -> list[dict[str, Any]]:
    """Replace the confirmation dialog; returns the arguments of every dialog shown."""
    shown: list[dict[str, Any]] = []

    class FakeConfirm:
        def __init__(self, title: str, headline: str, **kwargs: Any) -> None:
            shown.append({"title": title, "headline": headline, **kwargs})

        def exec(self) -> int:
            return int(QDialog.DialogCode.Accepted if accepted else QDialog.DialogCode.Rejected)

    monkeypatch.setattr(dialogs, "ConfirmDialog", FakeConfirm)
    return shown


def fill_plan(view: Any, use_case: str = "suggest") -> Any:
    """The batch plan the fill button would run (the engine's own estimate)."""
    items = ai_models.batch_items(view.undecided_rows())
    return view.ai().engine().plan_batches(use_case, items)


def script_fill(
    stub: StubServer,
    view: Any,
    *,
    action: str = AI_ACTION,
    why: str = AI_WHY,
    use_case: str = "suggest",
    overrides: Mapping[str, Any] | None = None,
) -> Any:
    """Queue one scripted answer per batch the fill will send, then return the plan."""
    plan = fill_plan(view, use_case)
    for batch in plan.batches:
        entries = [
            dict(
                classification_entry(key, action=action, rationale=why)
                if use_case == "classify"
                else item(key, action=action, why=why)
            )
            for key in batch
        ]
        if overrides:
            for entry in entries:
                entry.update(overrides)
        payload = (
            classification_answer(*entries)
            if use_case == "classify"
            else suggestion_answer(*entries)
        )
        stub.queue_json(payload)
    return plan


def gate(view: Any, qtbot: object, *, timeout: int = 30_000) -> None:
    """Wait for the AI task to end (and one paint) without pinning the test to a duration."""
    qtbot.waitUntil(lambda: not view.ai_busy(), timeout=timeout)  # type: ignore[attr-defined]
    qtbot.wait(60)  # type: ignore[attr-defined]


def solution_text(view: Any, row: opportunities.Opportunity) -> str:
    """What the suggested-solution cell shows for one row."""
    model = view.table_model()
    index = model.index_of(row.path)
    assert index.isValid(), f"{row.path} is not in the table"
    return str(model.index(index.row(), models.COLUMN_SOLUTION).data(Qt.ItemDataRole.DisplayRole))


def open_plan_view(
    sandbox: LiveSandbox, tmp_path: Path, qtbot: object, ai: Any, session: Any
) -> PlanView:
    """A shown PlanView over the live sandbox with a working provider behind it."""
    from spacesage.app import state

    settings = state.Settings.persisted(tmp_path / "settings.ini")
    settings.set_target_drive(str(sandbox.target))
    settings.set_reserve_bytes(0)
    settings.set_quarantine_dir(str(sandbox.quarantine))
    view = PlanView(sandbox.data_root, db_path=sandbox.db_path, settings=settings, ai=ai)
    qtbot.addWidget(view)  # type: ignore[attr-defined]
    view.resize(1280, 820)
    view.show()
    view.set_session(session)
    qtbot.wait(120)  # type: ignore[attr-defined]
    return view


# --------------------------------------------------------------------------- #
# The bar: off, or ready and honest about what it will send
# --------------------------------------------------------------------------- #


def test_the_bar_says_off_until_a_provider_is_configured(window: Any, fixture_listing: Any) -> None:
    """With no provider the feature is visible, explained, and inert."""
    window.set_listing(fixture_listing)
    window.navigate("opportunities")
    view = window.opportunities_view

    assert view.ai_status.text() == "AI off"
    assert view.undecided_rows(), "the fixture listing should have undecided rows"
    assert not view.ai_fill_button.isEnabled()
    assert "provider" in view.ai_stage.text().lower()
    assert "Settings" in view.ai_stage.text()
    assert window.status_ai.text() == "AI off"


def test_a_ready_bar_names_the_provider_and_counts_the_rows(
    ai_window: Any, fixture_listing: Any
) -> None:
    """Ready means the badge names provider and model, and the button counts work."""
    ai_window.set_listing(fixture_listing)
    ai_window.navigate("opportunities")
    view = ai_window.opportunities_view

    assert view.ai_status.text().startswith("AI: stub / stub-model")
    assert view.ai_fill_button.isEnabled()
    assert "row" in view.ai_stage.text()
    assert str(len(view.undecided_rows())) in view.ai_stage.text()
    assert ai_window.status_ai.text().startswith("AI: stub / stub-model")


# --------------------------------------------------------------------------- #
# The fill
# --------------------------------------------------------------------------- #


def test_a_fill_estimates_first_then_fills_the_undecided_rows(
    ai_window: Any,
    ai_stub: StubServer,
    fixture_listing: Any,
    monkeypatch: pytest.MonkeyPatch,
    qtbot: object,
) -> None:
    """One press: estimate -> consent -> answers for every undecided row, in batches."""
    ai_window.set_listing(fixture_listing)
    ai_window.navigate("opportunities")
    view = ai_window.opportunities_view
    undecided = view.undecided_rows()
    plan = script_fill(ai_stub, view)
    shown = install_confirm(monkeypatch, accepted=True)

    assert view.request_suggestions() is True
    gate(view, qtbot)

    assert len(shown) == 1
    dialog = shown[0]
    assert dialog["title"] == "Generate AI suggestions"
    assert str(len(undecided)) in dialog["headline"]
    assert "call(s)" in dialog["detail"]
    assert "tokens" in dialog["detail"]
    assert "stub" in dialog["detail"] and "stub-model" in dialog["detail"]
    assert ai_stub.calls == plan.calls

    store = view.ai().store
    assert len(store) == len(undecided)
    for row in undecided:
        entry = store.verdict(row.key)
        assert entry is not None, row.path
        assert entry.provider == "stub" and entry.model == "stub-model"
        assert entry.action == AI_ACTION
        assert entry.confidence == pytest.approx(0.9)
        assert entry.at > 0
    assert "row(s) filled" in ai_window.status_message.full_text()
    assert str(len(undecided)) in ai_window.status_message.full_text()


def test_the_ai_fills_the_column_the_rules_left_undecided(
    ai_window: Any,
    ai_stub: StubServer,
    fixture_listing: Any,
    monkeypatch: pytest.MonkeyPatch,
    qtbot: object,
) -> None:
    """The suggestion column paints the AI's verdict, with AI provenance, for undecided rows."""
    ai_window.set_listing(fixture_listing)
    ai_window.navigate("opportunities")
    view = ai_window.opportunities_view
    undecided = view.undecided_rows()
    script_fill(ai_stub, view)
    install_confirm(monkeypatch, accepted=True)

    assert view.request_suggestions() is True
    gate(view, qtbot)

    store = view.ai().store
    for row in undecided[:4]:
        entry = store.verdict(row.key)
        assert entry is not None
        display = models.solution_display(row, entry)
        assert display.from_ai is True
        assert display.label == "Delete (quarantine)"
        assert display.why == AI_WHY
        assert display.provenance == "AI · stub / stub-model"
        assert solution_text(view, row) == display.label

    decided = [row for row in fixture_listing.rows if row not in undecided]
    assert decided, "the fixture should also have rows the rules decide"
    for row in decided[:3]:
        display = models.solution_display(row, None)
        assert display.from_ai is False
        assert display.label == row.solution
        assert solution_text(view, row) == row.solution


def test_a_no_action_answer_renders_as_advice_not_as_work(
    ai_window: Any, ai_stub: StubServer, fixture_listing: Any, qtbot: object
) -> None:
    """NO_ACTION is a first-class answer: painted muted, named, and never work.

    The AI's "leave it alone" is the answer a user most needs to trust: it has to
    look like advice (muted, no tier colour) next to the destructive verdicts, and
    the row has to keep saying it is not executable.
    """
    ai_window.set_listing(fixture_listing)
    ai_window.navigate("opportunities")
    view = ai_window.opportunities_view
    rows = view.undecided_rows()
    assert len(rows) >= 2, "the fixture listing should have several undecided rows"
    keep, drop = rows[0], rows[1]
    ai_stub.queue_json(
        suggestion_answer(
            item(
                keep.path,
                action="NO_ACTION",
                why="the game's own asset cache: deleting it costs more time than the space buys",
                confidence=0.85,
            )
        )
    )
    ai_stub.queue_json(suggestion_answer(item(drop.path, action=AI_ACTION, why=AI_WHY)))

    assert view.request_row("suggest", keep.key) is True
    gate(view, qtbot)
    assert view.request_row("suggest", drop.key) is True
    gate(view, qtbot)

    quiet = view.ai().store.verdict(keep.key)
    busy = view.ai().store.verdict(drop.key)
    assert quiet is not None and busy is not None
    assert quiet.no_action is True and busy.no_action is False

    quiet_display = models.solution_display(keep, quiet)
    busy_display = models.solution_display(drop, busy)
    assert quiet_display.label == "No action"
    assert quiet_display.tone == "muted"
    assert quiet_display.from_ai is True and busy_display.from_ai is True
    assert busy_display.tone != "muted", "a work suggestion is not painted like 'no action'"
    # The AI's own action vocabulary paints too: an AI "COMPRESS" or "NO_ACTION"
    # must not fall back to a generic glyph next to the engine's names.
    assert quiet_display.icon == "shield-check"
    assert models.solution_display(drop, busy).icon == "trash-2"
    assert solution_text(view, keep) == "No action"
    assert solution_text(view, drop) == busy_display.label

    # The cell says where it came from, and that nothing about it can run.
    model = view.table_model()
    tooltip = str(model.index_of(keep.path).data(Qt.ItemDataRole.ToolTipRole))
    assert "AI suggestion" in tooltip and "not executable" in tooltip
    assert quiet_display.provenance == "AI · stub / stub-model"
    assert keep.state == opportunities.STATE_UNDECIDED, "the AI decided nothing"


def test_the_rules_verdict_wins_over_an_ai_answer(
    fixture_listing: Any,
) -> None:
    """A row the rules decided keeps its rule verdict even when an AI answer exists.

    The AI is a suggestion for the rows nobody could decide; it never overrides a
    rule, and the plan's wording always stays the rules'.
    """
    decided = None
    for row in fixture_listing.rows:
        if row.state != opportunities.STATE_UNDECIDED:
            decided = row
            break
    assert decided is not None, "the fixture needs a decided row"
    entry = ai_models.AISuggestion(
        key=decided.key,
        path=decided.path,
        use_case="suggest",
        action="MOVE",
        label="Move",
        why="the AI thinks this belongs on the archive drive",
        confidence=0.6,
        provider="stub",
        model="stub-model",
    )

    display = models.solution_display(decided, entry)

    assert display.from_ai is False
    assert display.label == decided.solution
    assert display.why and display.why in decided.why


def test_a_second_fill_is_free_and_served_from_the_cache(
    ai_window: Any,
    ai_stub: StubServer,
    fixture_listing: Any,
    monkeypatch: pytest.MonkeyPatch,
    qtbot: object,
) -> None:
    """Nothing is sent twice: the second pass re-uses the cache and says so."""
    ai_window.set_listing(fixture_listing)
    ai_window.navigate("opportunities")
    view = ai_window.opportunities_view
    script_fill(ai_stub, view)
    install_confirm(monkeypatch, accepted=True)

    assert view.request_suggestions() is True
    gate(view, qtbot)
    calls_after_first = ai_stub.calls
    assert calls_after_first > 0

    # Nothing is queued for the second pass, and a call would take it: the only
    # way this can pass is if every batch was answered from the cache on disk.
    ai_stub.reset()
    assert view.request_suggestions() is True
    gate(view, qtbot)

    assert ai_stub.calls == 0, "the second fill left the machine"
    store = view.ai().store
    assert len(store) == len(view.undecided_rows())
    for row in view.undecided_rows():
        entry = store.verdict(row.key)
        assert entry is not None and entry.cached is True
    assert "cache" in ai_window.status_message.full_text().lower()
    assert ai_window.ai().meter_text() != ""


def test_a_fill_reports_a_provider_that_is_not_there(
    ai_window: Any,
    ai_stub: StubServer,
    fixture_listing: Any,
    monkeypatch: pytest.MonkeyPatch,
    qtbot: object,
) -> None:
    """A provider that answers with errors is an inline failure, never a traceback."""
    ai_window.set_listing(fixture_listing)
    ai_window.navigate("opportunities")
    view = ai_window.opportunities_view
    plan = fill_plan(view)
    for _ in range(plan.calls):
        ai_stub.queue_error(500, message="model is on fire")
    install_confirm(monkeypatch, accepted=True)

    assert view.request_suggestions() is True
    gate(view, qtbot)

    assert len(view.ai().store) == 0
    stage = view.ai_stage.text()
    assert stage, "a failed fill has to say why inline"
    assert "500" in stage or "failed" in stage.lower() or "error" in stage.lower()


def test_a_single_row_can_be_asked_for_an_answer(
    ai_window: Any, ai_stub: StubServer, fixture_listing: Any, qtbot: object
) -> None:
    """The details pane's own question: one row, one answer, no batch machinery."""
    ai_window.set_listing(fixture_listing)
    ai_window.navigate("opportunities")
    view = ai_window.opportunities_view
    row = view.undecided_rows()[0]
    ai_stub.queue_json(suggestion_answer(item(row.path, action=AI_ACTION, why=AI_WHY)))

    assert view.request_row("suggest", row.key) is True
    gate(view, qtbot)

    entry = view.ai().store.verdict(row.key)
    assert entry is not None and entry.action == AI_ACTION
    pane = view.details
    assert pane.current_row() is not None and pane.current_row().key == row.key
    assert pane.ai_entry() is not None and pane.ai_entry().why == AI_WHY
    assert ai_stub.calls == 1


def test_explain_streams_into_the_details_pane(
    ai_window: Any, ai_stub: StubServer, fixture_listing: Any, qtbot: object
) -> None:
    """Explain with AI: the answer arrives in pieces and stays on the row."""
    ai_window.set_listing(fixture_listing)
    ai_window.navigate("opportunities")
    view = ai_window.opportunities_view
    row = view.undecided_rows()[0]
    ai_stub.queue_json(explanation_answer())

    assert view.request_row("explain", row.key) is True
    gate(view, qtbot)

    pane = view.details
    text = pane.explanation_text()
    assert "installers for programs that are no longer installed" in text
    assert "Risks:" in text and "Alternatives:" in text
    assert "stub / stub-model" in pane.explanation_stage()
    assert not pane.explanation_failed()
    assert ai_stub.requests[-1].stream is True


def test_the_streaming_text_is_assembled_in_order() -> None:
    """The pane's streaming sink appends deltas instead of replacing the answer."""
    from spacesage.app.views.details_pane import DetailsPane

    pane = DetailsPane()
    try:
        pane.begin_explanation("Asking stub / stub-model…")
        pane.extend_explanation("This folder holds ")
        pane.extend_explanation("installers nobody needs.")
        assert pane.explanation_text() == "This folder holds installers nobody needs."
    finally:
        pane.deleteLater()


# --------------------------------------------------------------------------- #
# Apply as rule: the only door into the engine
# --------------------------------------------------------------------------- #


def _decided(listing: Any, key: str) -> bool:
    """Has the row stopped being undecided in this listing?"""
    row = None if listing is None else listing.row(key)
    return row is not None and row.state != opportunities.STATE_UNDECIDED


def _rule_of(listing: Any, key: str) -> str:
    """The id of the rule that decides this row (``""`` when none matched)."""
    row = None if listing is None else listing.row(key)
    return "" if row is None else str(row.rule_id or "")


def test_apply_as_rule_writes_a_pack_and_the_row_is_decided_on_the_next_pass(
    ai_window: Any,
    ai_stub: StubServer,
    fixture_listing: Any,
    monkeypatch: pytest.MonkeyPatch,
    qtbot: object,
) -> None:
    """Promotion: preview the TOML, write it, re-rank the index, see the rule win."""
    ai_window.set_listing(fixture_listing)
    ai_window.navigate("opportunities")
    view = ai_window.opportunities_view
    row = view.undecided_rows()[0]
    # A REVIEW answer is the one an undecided entry can always turn into a rule:
    # nothing destructive may be written for an entry with no tier yet.
    ai_stub.queue_json(suggestion_answer(item(row.path, action="REVIEW", why=AI_WHY)))
    assert view.request_row("suggest", row.key) is True
    gate(view, qtbot)
    entry = view.ai().store.verdict(row.key)
    assert entry is not None and entry.action == "REVIEW"

    shown = install_confirm(monkeypatch, accepted=True)
    assert view.apply_rule(row.key) is True

    assert len(shown) == 1
    dialog = shown[0]
    assert dialog["title"] == "Apply as rule"
    text = "\n".join(dialog["lines"])
    assert "[[rule]]" in text and 'action = "REVIEW"' in text
    assert entry.provenance in dialog["detail"]

    written = sorted(rules.default_rules_dir().glob("*.toml"))
    assert written, f"no rule pack was written under {rules.default_rules_dir()}"
    pack = written[0].read_text(encoding="utf-8")
    assert "[[rule]]" in pack and row.path.replace("\\", "\\\\") in pack
    assert view.ai().store.verdict(row.key) is None, "an applied answer leaves the store"

    # The listing is re-ranked in the background: the promoted rule decides the row.
    wanted = promote.rule_id_for(row.path)
    qtbot.waitUntil(lambda: _rule_of(ai_window.listing(), row.key) == wanted, timeout=60_000)  # type: ignore[attr-defined]
    decided = ai_window.listing().row(row.key)
    assert decided is not None
    # A REVIEW rule is advice: the row stays a row a human decides, never an action.
    assert decided.state == opportunities.STATE_UNDECIDED
    assert decided.pack == promote.PACK_ID
    assert AI_WHY in decided.why or AI_WHY in decided.rationale


def test_a_destructive_answer_is_refused_a_rule_until_a_tier_exists(
    ai_window: Any,
    ai_stub: StubServer,
    fixture_listing: Any,
    monkeypatch: pytest.MonkeyPatch,
    qtbot: object,
) -> None:
    """The engine's guardrail reaches the button: no tier, no destructive rule."""
    ai_window.set_listing(fixture_listing)
    ai_window.navigate("opportunities")
    view = ai_window.opportunities_view
    row = view.undecided_rows()[0]
    ai_stub.queue_json(suggestion_answer(item(row.path, action="DELETE_QUARANTINE", why=AI_WHY)))
    assert view.request_row("suggest", row.key) is True
    gate(view, qtbot)
    assert view.ai().store.verdict(row.key) is not None

    shown = install_confirm(monkeypatch, accepted=True)

    assert view.apply_rule(row.key) is False
    assert shown == [], "the confirmation must not be shown for a refused rule"
    assert not list(rules.default_rules_dir().glob("*.toml")), "a refused rule wrote a file"
    stage = view.ai_stage.text()
    assert "T1/T2" in stage or "refus" in stage.lower(), stage
    assert view.ai().store.verdict(row.key) is not None, "the answer stays on screen"


def test_classify_answers_the_tier_that_lets_a_rule_be_written(
    ai_window: Any,
    ai_stub: StubServer,
    fixture_listing: Any,
    monkeypatch: pytest.MonkeyPatch,
    qtbot: object,
) -> None:
    """Classify (which assigns a tier), then apply: the destructive rule is written."""
    ai_window.set_listing(fixture_listing)
    ai_window.navigate("opportunities")
    view = ai_window.opportunities_view
    row = view.undecided_rows()[0]
    ai_stub.queue_json(
        classification_answer(classification_entry(row.path, tier="T2", category="installers"))
    )
    assert view.request_row("classify", row.key) is True
    gate(view, qtbot)
    entry = view.ai().store.verdict(row.key)
    assert entry is not None and entry.is_classification is True
    assert entry.tier == "T2"

    install_confirm(monkeypatch, accepted=True)
    assert view.apply_rule(row.key) is True

    written = sorted(rules.default_rules_dir().glob("*.toml"))
    assert written
    pack = "\n".join(path.read_text(encoding="utf-8") for path in written)
    assert 'action = "DELETE_QUARANTINE"' in pack
    assert 'tier = "T2"' in pack
    assert "ai" in pack.lower()  # the pack says where the rule came from

    # The classification carried the tier, so a destructive rule could be written:
    # the advice row is now an action the plan will run.
    wanted = promote.rule_id_for(row.path)
    qtbot.waitUntil(lambda: _rule_of(ai_window.listing(), row.key) == wanted, timeout=60_000)  # type: ignore[attr-defined]
    decided = ai_window.listing().row(row.key)
    assert decided is not None
    assert decided.state == opportunities.STATE_ACTION
    assert "quarantine" in decided.solution.lower(), decided.solution
    assert decided.tier == "T2"


def test_a_suggestion_is_never_executable_without_a_rule(
    ai_window: Any, ai_stub: StubServer, fixture_listing: Any, qtbot: object
) -> None:
    """Nothing the AI says reaches a plan: the plan only knows the rules' verdicts."""
    ai_window.set_listing(fixture_listing)
    ai_window.navigate("opportunities")
    view = ai_window.opportunities_view
    row = view.undecided_rows()[0]
    ai_stub.queue_json(suggestion_answer(item(row.path, action="MOVE", why=AI_WHY)))
    assert view.request_row("suggest", row.key) is True
    gate(view, qtbot)
    assert view.ai().store.verdict(row.key) is not None

    view.table_model().toggle(row.path)
    view.request_plan()
    plan_view = ai_window.plan_page.plan
    qtbot.waitUntil(lambda: not plan_view.busy(), timeout=60_000)  # type: ignore[attr-defined]
    qtbot.wait(120)  # type: ignore[attr-defined]

    session = plan_view.session()
    assert session is not None
    # The AI asked for a MOVE; the plan carries the rules' verdict for that row, not the AI's.
    actions = list(session.plan.actions)
    same_path = [
        action
        for action in actions
        if action.path.casefold().replace("/", "\\") == row.path.casefold().replace("/", "\\")
    ]
    assert all(action.type != "MOVE" for action in same_path), (
        "the AI's suggestion reached the plan: " + repr([a.type for a in same_path])
    )


# --------------------------------------------------------------------------- #
# The plan review (design §10 on the plan screen)
# --------------------------------------------------------------------------- #


def test_the_review_lists_the_risks_and_can_take_an_action_out(
    ai_service: ai_models.AIService,
    ai_stub: StubServer,
    live_sandbox: LiveSandbox,
    sandbox_window: Any,
    qtbot: object,
) -> None:
    """Review this plan: each annotation names an action, and *Take out* respects it."""
    window: MainWindow = sandbox_window(live_sandbox, ai=ai_service)
    window.navigate("opportunities")
    model = window.opportunities_view.table_model()
    for path in live_sandbox.selection():
        model.toggle(path)
    window.opportunities_view.build_plan_button.click()
    plan_view = window.plan_page.plan
    qtbot.waitUntil(lambda: not plan_view.busy(), timeout=60_000)  # type: ignore[attr-defined]
    qtbot.wait(120)  # type: ignore[attr-defined]

    approvable = plan_view.approved_ids()
    assert approvable, "the sandbox selection has executable actions"
    target = approvable[0]
    ai_stub.queue_json(
        review_answer(
            annotation_entry(target, severity="warning", title="Moves data off this drive"),
            annotation_entry(target, severity="danger", title="Needs the archive drive"),
            annotation_entry("op-that-is-not-in-this-plan", severity="info"),
            summary="Two actions, one caveat.",
        )
    )

    assert plan_view.review() is True
    qtbot.waitUntil(lambda: not plan_view.busy(), timeout=60_000)  # type: ignore[attr-defined]
    qtbot.wait(120)  # type: ignore[attr-defined]

    outcome = plan_view.review_outcome()
    assert outcome is not None and outcome.ok is True
    assert outcome.summary == "Two actions, one caveat."
    annotations = plan_view.review_annotations()
    assert annotations, "the annotations reached the screen"
    assert annotations[0][0] == "danger", "the worst annotation comes first"
    assert {severity for severity, _id, _title in annotations} == {"danger", "warning"}
    assert "op-that-is-not-in-this-plan" not in {action_id for _s, action_id, _t in annotations}

    # The remedy the card offers: withdraw that action's approval.
    assert plan_view.take_out(target) is True
    assert target not in plan_view.approved_ids()
    assert window.plan_page.plan.approved_ids() == plan_view.approved_ids()


def test_the_review_failure_is_said_inline_not_raised(
    ai_service: ai_models.AIService,
    ai_stub: StubServer,
    live_sandbox: LiveSandbox,
    sandbox_window: Any,
    qtbot: object,
) -> None:
    """A provider that errors leaves the plan on screen and one sentence in the card."""
    window: MainWindow = sandbox_window(live_sandbox, ai=ai_service)
    window.navigate("opportunities")
    model = window.opportunities_view.table_model()
    seed = live_sandbox.selection()[0]
    model.toggle(seed)
    window.opportunities_view.build_plan_button.click()
    plan_view = window.plan_page.plan
    qtbot.waitUntil(lambda: not plan_view.busy(), timeout=60_000)  # type: ignore[attr-defined]
    ai_stub.queue_error(500, message="model is on fire")

    assert plan_view.review() is True
    qtbot.waitUntil(lambda: not plan_view.busy(), timeout=60_000)  # type: ignore[attr-defined]
    qtbot.wait(120)  # type: ignore[attr-defined]

    outcome = plan_view.review_outcome()
    assert outcome is not None and outcome.ok is False
    assert plan_view.session() is not None, "a failed review leaves the plan alone"
    assert plan_view.review_annotations() == ()


# --------------------------------------------------------------------------- #
# Settings: the file is the state
# --------------------------------------------------------------------------- #


def test_settings_shows_the_layer_and_can_add_and_test_a_provider(
    ai_window: Any, ai_stub: StubServer, qtbot: object
) -> None:
    """The AI card: what is configured, where the key comes from, and one call."""
    assert ai_window.navigate("settings")
    page = ai_window.settings_view

    assert page.current_provider_name() == "stub"
    assert "stub" in page.ai_status.text() and "stub-model" in page.ai_status.text()
    assert page.base_url.text() == ai_stub.url
    assert page.api_key_env.text() == ""
    assert page.key_badge.text() == "not set"
    assert "needs no key" in page.key_badge.toolTip()

    # A preset adds a provider to the file, and the form starts editing it.
    assert page.add_provider("ollama") is True
    assert page.current_provider_name() == "ollama"
    assert "ollama" in ai_window.ai().config().provider_names()
    assert "ollama" in page.provider_combo.currentText()

    # A key is named, never typed: the badge only says whether the variable is set.
    page.api_key_env.setText("SPACESAGE_TEST_KEY")
    page.save_button.click()
    page.refresh_ai()
    assert page.current_provider_name() == "ollama"
    assert page.key_badge.text() == "not set"
    assert page.api_key_env.text() == "SPACESAGE_TEST_KEY"
    assert "SPACESAGE_TEST_KEY" in page.key_badge.toolTip()

    # The one call this screen makes: a test connection, on a worker.
    assert page.test_connection("stub") is True
    qtbot.waitUntil(lambda: page.test_button.isEnabled(), timeout=30_000)  # type: ignore[attr-defined]
    assert "model" in page.ai_result.text().lower()

    page.remove_button.click()
    assert page.current_provider_name() == "stub"
    assert ai_window.ai().config().provider_names() == ("stub",)

    # OpenCode Zen is offered like every other preset, and adds the gateway's URL.
    labels = [action.text() for action in page.add_button.menu().actions()]
    assert "OpenCode Zen" in labels
    assert page.add_provider("opencode") is True
    assert page.base_url.text() == "https://opencode.ai/zen/v1"
    page.remove_button.click()
    assert ai_window.ai().config().provider_names() == ("stub",)


def test_a_provider_that_is_not_filled_in_does_not_break_the_settings_card(
    ai_window: Any, ai_stub: StubServer
) -> None:
    """The blank custom preset is editable, not fatal: reading it back must not validate.

    A half-filled provider (no base_url yet) used to crash the whole window when
    the card listed or reloaded it, because the editor asked for the *validated*
    provider.  The card now reads the raw one and lets the user finish the form.
    """
    assert ai_window.navigate("settings")
    page = ai_window.settings_view

    assert page.add_provider("custom") is True
    assert page.current_provider_name() == "custom"
    assert page.base_url.text() == ""
    assert page.model.currentText() == ""

    page.refresh_ai()  # the crash point: must survive a provider with no base_url
    assert "custom" in page.provider_combo.currentText()
    assert ai_window.ai().config().provider_names() == ("stub", "custom")

    # Editing and saving the half-filled provider is the same raw read, no validation.
    page.base_url.setText("http://127.0.0.1:8080/v1")
    page.save_button.click()
    assert page.base_url.text() == "http://127.0.0.1:8080/v1"
    assert ai_window.ai().config().configured("custom").base_url == "http://127.0.0.1:8080/v1"


def test_the_policy_switches_write_the_file_the_engine_reads(
    ai_window: Any, ai_stub: StubServer
) -> None:
    """Redact, local-only and streaming: one handler, one file, one stance.

    The three switches decide what leaves the machine, so they are saved the way a
    provider is -- the file the engine is rebuilt from carries the new value and
    the card reads it back. A toggle that only flipped a checkbox would leave the
    running engine on the old stance (design §9.4).
    """
    assert ai_window.navigate("settings")
    page = ai_window.settings_view
    service = ai_window.ai()
    before = service.config()

    assert page.redact_box.isChecked() is before.redact_paths
    assert page.local_box.isChecked() is before.local_only
    assert page.stream_box.isChecked() is before.streaming

    page.redact_box.setChecked(not before.redact_paths)
    page.local_box.setChecked(not before.local_only)
    page.stream_box.setChecked(not before.streaming)

    after = service.config()
    assert after.redact_paths is (not before.redact_paths)
    assert after.local_only is (not before.local_only)
    assert after.streaming is (not before.streaming)

    # The file, not just the in-memory object: this is what a restart reads.
    path = service.path()
    assert path is not None, "the toggle has to have written the configuration"
    on_disk = AIConfig.load(path)
    assert on_disk.streaming is after.streaming
    assert on_disk.redact_paths is after.redact_paths
    assert on_disk.local_only is after.local_only

    # Each switch says what it just did -- and says it without calling anyone: a
    # toggle is a note in the card, the only call this screen makes is "Test".
    assert not ai_stub.requests, "a settings toggle must not touch the network"
    page.stream_box.setChecked(before.streaming)
    assert "stream" in page.ai_result.text().lower(), "streaming on says so"
    page.stream_box.setChecked(not before.streaming)
    assert "one piece" in page.ai_result.text().lower(), "streaming off says so"
