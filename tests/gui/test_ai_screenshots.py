"""Screenshot artifacts for the AI layer in the list (S10b, design §10).

The same rule as :mod:`test_screenshots`: every render is proved to be a real
paint (many colours, a plausible size), and the PNGs under ``artifacts/gui`` are
what the acceptance evidence is taken from.  These four are the screens the
slice has to show a human: the list with AI suggestions filled in, an
explanation streaming into the details pane, a plan review with its
severity-tagged annotations, and the provider settings.

The provider behind all of them is the scripted stub in :mod:`tests.ai_stub` --
a real OpenAI-compatible server on loopback -- so what is rendered is what the
app would render for any provider that answers.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from pathlib import Path
from typing import TYPE_CHECKING, Any

import pytest
from PySide6.QtCore import QPoint
from PySide6.QtWidgets import QDialog

from ai_stub import (
    StubServer,
    annotation_entry,
    explanation_answer,
    item,
    review_answer,
    suggestion_answer,
)
from spacesage.app import ai_models, dialogs, theme, widgets
from spacesage.app.windows import MainWindow

if TYPE_CHECKING:  # the fixtures live in conftest; only the type is needed here
    from conftest import LiveSandbox

#: One scripted answer per row, so the render shows every shape the column has:
#: a destructive AI verdict, a move, a compress, advice, and a first-class
#: "nothing to do".  Cycled across the undecided rows of the fixture listing.
SHAPES: tuple[dict[str, Any], ...] = (
    {
        "action": "DELETE_QUARANTINE",
        "why": "three copies of the same installer; only the newest one is reachable",
        "confidence": 0.9,
        "side_effects": "moves the folder to quarantine, so it can be restored",
        "alternatives": ["archive it on the D: drive instead", "keep only the newest build"],
    },
    {
        "action": "MOVE",
        "why": "a media library that is only ever read, and the system drive is full",
        "confidence": 0.75,
        "side_effects": "writes to the target drive and leaves a link behind",
        "alternatives": ["compress it in place"],
    },
    {
        "action": "COMPRESS",
        "why": "large log files that grow slowly and are read rarely",
        "confidence": 0.6,
        "side_effects": "the folder stays where it is; reads get slower",
        "alternatives": ["delete the rotated files"],
    },
    {
        "action": "REVIEW",
        "why": "a virtual machine image that only its owner can judge",
        "confidence": 0.4,
        "alternatives": ["ask the VM's owner"],
    },
    {
        "action": "NO_ACTION",
        "why": "the game's own asset cache: deleting it costs more time than the space buys",
        "confidence": 0.85,
    },
)


def script_fill(stub: StubServer, view: Any) -> Any:
    """Queue one scripted answer per batch the fill will send, and return the plan."""
    items = ai_models.batch_items(view.undecided_rows())
    plan = view.ai().engine().plan_batches("suggest", items)
    index = 0
    for batch in plan.batches:
        entries: list[Mapping[str, Any]] = []
        for key in batch:
            entries.append(item(key, **SHAPES[index % len(SHAPES)]))
            index += 1
        stub.queue_json(suggestion_answer(*entries))
    return plan


def install_confirm(
    monkeypatch: pytest.MonkeyPatch, *, accepted: bool = True
) -> list[dict[str, Any]]:
    """Accept (or refuse) the confirmation dialog the fill shows."""
    shown: list[dict[str, Any]] = []

    class FakeConfirm:
        def __init__(self, title: str, headline: str, **kwargs: Any) -> None:
            shown.append({"title": title, "headline": headline, **kwargs})

        def exec(self) -> int:
            return int(QDialog.DialogCode.Accepted if accepted else QDialog.DialogCode.Rejected)

    monkeypatch.setattr(dialogs, "ConfirmDialog", FakeConfirm)
    return shown


def gate(view: Any, qtbot: object, *, timeout: int = 60_000) -> None:
    """Wait for the AI task to end (and one paint) without pinning a duration."""
    qtbot.waitUntil(lambda: not view.ai_busy(), timeout=timeout)  # type: ignore[attr-defined]
    qtbot.wait(200)  # type: ignore[attr-defined]


def _visible_toasts(window: Any) -> list[Any]:
    """The toasts currently on screen over ``window`` (a closed one is not one)."""
    return [toast for toast in window.findChildren(widgets.Toast) if toast.isVisible()]


def card_in_view(pane: Any, card: Any) -> bool:
    """Whether the card sits inside the pane's viewport right now."""
    top = card.mapTo(pane.widget(), QPoint(0, 0)).y() - pane.verticalScrollBar().value()
    return -theme.SPACE["md"] <= top < pane.viewport().height()


def assert_in_view(pane: Any, card: Any, qtbot: object) -> None:
    """Prove a card is on screen -- the pane is a scroll area and the AI sits low in it.

    A render that shows the reasoning but not the answer would be evidence of the
    wrong thing, so the screenshots assert the card is actually in view.  The
    pane scrolls to the card when the answer lands, so wait for that scroll to
    land -- a slow runner can still be mid-scroll, and a fixed delay would race
    it.  The wait is bounded: an app that never scrolls still fails the run.
    """
    assert card is not None and card.isVisible(), "the card was not built"
    qtbot.waitUntil(lambda: card_in_view(pane, card), timeout=5_000)  # type: ignore[attr-defined]
    top = card.mapTo(pane.widget(), QPoint(0, 0)).y() - pane.verticalScrollBar().value()
    assert card_in_view(pane, card), (
        f"the card is not in view (top={top}, viewport={pane.viewport().height()})"
    )


def test_suggestions_filled_screenshot(
    ai_window: MainWindow,
    ai_stub: StubServer,
    fixture_listing: Any,
    artifacts: Path,
    grab_png: Callable[..., Path],
    monkeypatch: pytest.MonkeyPatch,
    qtbot: object,
) -> None:
    """suggestions_filled.png: the fill ran -- AI verdicts in the column, provenance, the meter.

    The details pane is pointed at a row the AI answered, so the render carries
    the whole story of one suggestion: what, why, what it costs, where it came
    from, and what a human can do with it.
    """
    ai_window.set_listing(fixture_listing)
    assert ai_window.navigate("opportunities")
    view = ai_window.opportunities_view
    undecided = view.undecided_rows()
    assert undecided, "the fixture listing should have undecided rows"
    # The user is looking at one row and presses the batch action: that row's
    # answer is what the pane shows (and scrolls to) when it lands.
    assert view.select_row(undecided[0].path)
    script_fill(ai_stub, view)
    install_confirm(monkeypatch, accepted=True)

    assert view.request_suggestions() is True
    gate(view, qtbot)

    assert len(view.ai().store) == len(undecided)
    assert ai_window.status_ai.text().startswith("AI: stub / stub-model")
    assert "token" in ai_window.status_ai.text(), ai_window.status_ai.text()
    first = view.ai().store.verdict(undecided[0].key)
    assert first is not None and first.action == "DELETE_QUARANTINE"
    pane = view.details
    assert pane.current_row() is not None and pane.current_row().key == undecided[0].key  # type: ignore[union-attr]
    assert pane.ai_entry() is not None, "the pane shows the answer for the row on screen"
    # The card is the point of the render: prove it is actually in view, not below
    # the fold (the pane scrolls to it when the answer arrives).
    assert_in_view(pane, pane.ai_card(), qtbot)
    qtbot.wait(260)  # type: ignore[attr-defined]
    assert grab_png(ai_window, artifacts / "suggestions_filled.png").is_file()


def test_explain_screenshot(
    ai_window: MainWindow,
    ai_stub: StubServer,
    fixture_listing: Any,
    artifacts: Path,
    grab_png: Callable[..., Path],
    qtbot: object,
) -> None:
    """explain.png: Explain with AI, streamed into the details pane next to the row."""
    ai_window.set_listing(fixture_listing)
    assert ai_window.navigate("opportunities")
    view = ai_window.opportunities_view
    row = view.undecided_rows()[0]
    ai_stub.queue_json(suggestion_answer(item(row.path, **SHAPES[0])))
    assert view.request_row("suggest", row.key) is True
    gate(view, qtbot)
    ai_stub.queue_json(explanation_answer())

    assert view.request_row("explain", row.key) is True
    gate(view, qtbot)

    pane = view.details
    assert "no longer installed" in pane.explanation_text()
    assert "Risks:" in pane.explanation_text() and "Alternatives:" in pane.explanation_text()
    assert not pane.explanation_failed()
    assert "stub / stub-model" in pane.explanation_stage()
    assert_in_view(pane, pane.explanation_card(), qtbot)
    qtbot.wait(260)  # type: ignore[attr-defined]
    assert grab_png(ai_window, artifacts / "explain.png").is_file()


def test_review_screenshot(
    ai_service: ai_models.AIService,
    ai_stub: StubServer,
    live_sandbox: LiveSandbox,
    sandbox_window: Any,
    artifacts: Path,
    grab_png: Callable[..., Path],
    qtbot: object,
) -> None:
    """review.png: Review plan with AI -- the annotations and the summary card."""
    window: MainWindow = sandbox_window(live_sandbox, ai=ai_service)
    window.navigate("opportunities")
    model = window.opportunities_view.table_model()
    for path in live_sandbox.selection():
        model.toggle(path)
    window.opportunities_view.build_plan_button.click()
    plan_view = window.plan_page.plan
    qtbot.waitUntil(lambda: not plan_view.busy(), timeout=60_000)  # type: ignore[attr-defined]
    qtbot.wait(200)  # type: ignore[attr-defined]

    approvable = plan_view.approved_ids()
    assert len(approvable) >= 2, "the sandbox selection should approve several actions"
    ai_stub.queue_json(
        review_answer(
            annotation_entry(
                approvable[0],
                severity="danger",
                title="This delete removes a folder other projects still point at",
                detail="two shortcuts outside the plan resolve into it",
                recommendation="review the references, then run it alone",
            ),
            annotation_entry(
                approvable[1],
                severity="warning",
                title="This move needs the archive drive attached",
                detail="the destination is not on this machine",
                recommendation="attach it before running, or withdraw this action",
            ),
            annotation_entry(
                approvable[-1],
                severity="info",
                title="Quarantine is on the same drive",
                detail="nothing is freed until the quarantine folder is emptied",
            ),
            summary="Three things to look at before you run this plan.",
        )
    )

    assert plan_view.review() is True
    qtbot.waitUntil(lambda: not plan_view.busy(), timeout=60_000)  # type: ignore[attr-defined]
    # The toast that reports the review lives for four seconds and floats over
    # the card: let it go, so the render shows the annotations it would cover.
    qtbot.waitUntil(lambda: not _visible_toasts(window), timeout=10_000)  # type: ignore[attr-defined]
    qtbot.wait(200)  # type: ignore[attr-defined]

    assert plan_view.review_annotations(), "the review has to render its annotations"
    assert window.plan_page.plan.review_card.isVisible()
    assert grab_png(window, artifacts / "review.png").is_file()


def test_providers_screenshot(
    ai_window: MainWindow,
    ai_stub: StubServer,
    artifacts: Path,
    grab_png: Callable[..., Path],
    qtbot: object,
) -> None:
    """providers.png: the provider list, what each one calls, the policies and a test result."""
    assert ai_window.navigate("settings")
    page = ai_window.settings_view

    # A second provider from a preset, so the list shows what "add" produces.
    assert page.add_provider("ollama")
    page.api_key_env.setText("OLLAMA_API_KEY")
    page.save_button.click()
    page.refresh_ai()

    # And back on the provider the app can really talk to (the scripted stub),
    # for the one call this screen makes, rendered underneath the form.
    page.provider_combo.setCurrentIndex(page.provider_combo.findData("stub"))
    assert page.current_provider_name() == "stub"
    assert page.test_connection("stub") is True
    qtbot.waitUntil(lambda: page.test_button.isEnabled(), timeout=30_000)  # type: ignore[attr-defined]
    qtbot.wait(200)  # type: ignore[attr-defined]

    assert "stub" in page.provider_combo.currentText()
    assert "model" in page.ai_result.text().lower()
    assert page.redact_box.isChecked() is False
    assert grab_png(ai_window, artifacts / "providers.png").is_file()
