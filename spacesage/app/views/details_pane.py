"""Details pane: everything about the selected row (design §9, screen 2).

Full reasoning (why, rationale, rule and pack), side effects in the plan's own
words, the alternatives with the reason each is or is not on the table, the move
destination editor feeding the planner's arithmetic, and the members a grouped
row covers.  The pane never decides anything itself: wording, gains and link
policy all come from :mod:`spacesage.opportunities` and
:mod:`spacesage.planner`.

The AI card (design §10) is the pane's one live surface: what the AI answered
for this row, who answered it, and what a user can do with that advice -- ask
for a suggestion or a classification, ask for an explanation (which streams in
here while the provider writes it), or promote the answer to a rule.  The pane
renders; the screen beside it owns the calls.
"""

from __future__ import annotations

from collections.abc import Callable

from PySide6.QtCore import QPoint, Qt, Signal
from PySide6.QtWidgets import (
    QFrame,
    QHBoxLayout,
    QLabel,
    QLayout,
    QLineEdit,
    QPushButton,
    QScrollArea,
    QSizePolicy,
    QVBoxLayout,
    QWidget,
)

from spacesage import candidates, opportunities, stats
from spacesage.app import ai_models as ai_models_module
from spacesage.app import icons, models, theme, widgets
from spacesage.app.ai_models import AIStore, AISuggestion, Notice

_MISSING = "—"

AI_SUGGEST = "suggest"
AI_CLASSIFY = "classify"
AI_EXPLAIN = "explain"
"""The three things the pane can ask the AI for (the worker's use cases)."""


class DetailsPane(QScrollArea):
    """The selected opportunity, in full."""

    destinationEdited = Signal(str, str)
    """``(row key, destination text)`` -- empty text means "no override"."""

    aiRequested = Signal(str, str)
    """``(use case, row key)`` -- *Suggest* / *Classify* / *Explain with AI* was pressed."""

    promoteRequested = Signal(str)
    """The key of the row whose AI answer should become a rule (*Apply as rule…*)."""

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setWidgetResizable(True)
        self.setFrameShape(QFrame.Shape.NoFrame)
        self.setMinimumWidth(300)
        self._target_drive = ""
        self._row: opportunities.Opportunity | None = None
        self._override = ""
        self._ai_store: AIStore | None = None
        self._ai_busy = False
        self._ai_stage = ""
        self._ai_error = ""
        self._explanation = ""
        self._explanation_stage = ""
        self._explanation_error = ""
        self._ai_card: QWidget | None = None
        self._explanation_card: QWidget | None = None
        self._explanation_label: QLabel | None = None

        self._body = QWidget(self)
        self._layout = QVBoxLayout(self._body)
        self._layout.setContentsMargins(
            theme.SPACE["lg"], theme.SPACE["md"], theme.SPACE["lg"], theme.SPACE["lg"]
        )
        self._layout.setSpacing(theme.SPACE["md"])
        self.setWidget(self._body)
        self.clear()

    # -- public API ------------------------------------------------------- #

    def set_target_drive(self, drive: str) -> None:
        """Destinations are planned against this drive."""
        self._target_drive = drive

    def current_row(self) -> opportunities.Opportunity | None:
        """The row currently explained (``None`` in the empty state)."""
        return self._row

    def set_ai_store(self, store: AIStore | None) -> None:
        """Read this row's AI answers from ``store`` (repainting as they arrive)."""
        if self._ai_store is not None:
            self._ai_store.changed.disconnect(self._on_ai_changed)
        self._ai_store = store
        if store is not None:
            store.changed.connect(self._on_ai_changed)

    def ai_entry(self) -> AISuggestion | None:
        """The AI answer for the row on screen, if any (the newest kind wins).

        A row can carry both a suggestion and a classification; the card shows one
        at a time -- the column's verdict or the label a rule could be written
        from -- and names which one it is showing.
        """
        if self._row is None or self._ai_store is None:
            return None
        return self._ai_store.suggestion(self._row.key) or self._ai_store.classification(
            self._row.key
        )

    def ai_card(self) -> QWidget | None:
        """The AI card currently on screen (``None`` in the empty state)."""
        return self._ai_card

    def explanation_card(self) -> QWidget | None:
        """The explanation card currently on screen (``None`` in the empty state)."""
        return self._explanation_card

    def set_ai_busy(self, busy: bool, stage: str = "") -> None:
        """Show (or hide) the "asking the provider" state of the AI card."""
        self._ai_busy = busy
        self._ai_stage = stage if busy else ""
        if busy:
            # The user just asked about this row: reveal before the card is
            # rebuilt, because the card in the slot is the one with a geometry.
            self._reveal(self._ai_card)
        self._refresh_ai_card()

    def set_ai_error(self, message: str) -> None:
        """Show an inline error in the AI card (unreachable provider, missing key)."""
        self._ai_error = message
        self._ai_busy = False
        self._reveal(self._ai_card)
        self._refresh_ai_card()

    def clear_ai_error(self) -> None:
        """Drop the inline error (a new request is on its way)."""
        self._ai_error = ""
        self._refresh_ai_card()

    def begin_explanation(self, stage: str = "") -> None:
        """Open the explanation card for a run that is about to stream."""
        self._explanation = ""
        self._explanation_error = ""
        self._explanation_stage = stage or "Explaining…"
        self._reveal(self._explanation_card or self._ai_card)
        self._rebuild_explanation_card()

    def extend_explanation(self, delta: str) -> None:
        """Append streamed prose (the provider writes it a chunk at a time)."""
        self._explanation += delta
        if self._explanation_label is None:
            self._rebuild_explanation_card()
            return
        if self._explanation_label.objectName() != "Muted":
            # The label was carrying the empty state's faint hint; streamed prose
            # is the answer itself, so it takes the body tone from the first chunk.
            self._explanation_label.setObjectName("Muted")
            self._explanation_label.style().unpolish(self._explanation_label)
            self._explanation_label.style().polish(self._explanation_label)
        self._explanation_label.setText(self._explanation)

    def finish_explanation(self, text: str, *, stage: str = "") -> None:
        """Close the stream with the parsed explanation."""
        self._explanation = text
        self._explanation_stage = stage
        self._rebuild_explanation_card()

    def set_explanation_error(self, message: str) -> None:
        """Report a failed explanation inline, in the card that asked for it."""
        self._explanation_error = message
        self._explanation_stage = ""
        self._rebuild_explanation_card()

    def explanation_text(self) -> str:
        """The explanation currently on screen (streamed or finished)."""
        return self._explanation

    def explanation_stage(self) -> str:
        """The provenance line of the explanation card (``""`` before one arrives)."""
        return self._explanation_stage

    def explanation_failed(self) -> bool:
        """Did the last explanation attempt fail (the card shows the reason inline)?"""
        return bool(self._explanation_error)

    def destination_text(self) -> str:
        """The destination editor's current text (``""`` when there is none)."""
        return self._destination.text() if hasattr(self, "_destination") else ""

    def destination_hint(self) -> str:
        """The line under the destination editor (link policy, validation)."""
        return self._destination_hint.text() if hasattr(self, "_destination_hint") else ""

    def set_destination_text(self, text: str) -> None:
        """Set the destination editor's text (tests and presets)."""
        if hasattr(self, "_destination"):
            self._destination.setText(text)

    def clear(self) -> None:
        """Show the empty state ("select an item")."""
        self._row = None
        self._override = ""
        self._reset()
        empty = widgets.EmptyState(
            "No row picked",
            "Pick a row in the list to see why SpaceSage suggests what it does, what it would "
            "change and what the alternatives are.",
            icon_name="eye",
            parent=self._body,
        )
        self._layout.addWidget(empty)
        self._layout.addStretch(1)

    def show_row(
        self,
        row: opportunities.Opportunity | None,
        *,
        target_drive: str = "",
        selection: opportunities.Selection | None = None,
        override: str = "",
    ) -> None:
        """Explain ``row`` (``None`` clears the pane)."""
        if row is None:
            self.clear()
            return
        self._target_drive = target_drive or self._target_drive
        self._row = row
        self._override = override
        self._reset()
        checked = selection.is_selected(row.key) if selection is not None else False
        self._build_header(row, checked=checked)
        self._build_metrics(row)
        self._build_solution(row)
        self._build_ai(row)
        self._build_explanation(row)
        self._build_side_effects(row)
        self._build_destination(row)
        self._build_alternatives(row)
        self._build_members(row)
        self._build_rank(row)
        self._layout.addStretch(1)

    def apply_theme(self) -> None:
        """Re-render after a theme change."""
        if self._row is None:
            self.clear()
        else:
            self.show_row(self._row, target_drive=self._target_drive, override=self._override)

    # -- building blocks -------------------------------------------------- #

    def _reset(self) -> None:
        self._ai_card = None
        self._explanation_card = None
        self._explanation_label = None
        while self._layout.count():
            item = self._layout.takeAt(0)
            if item is None:
                continue
            widget = item.widget()
            nested = item.layout()
            if widget is not None:
                widget.setParent(None)
                widget.deleteLater()
            elif nested is not None:
                self._clear_layout(nested)

    def _clear_layout(self, layout: QLayout) -> None:
        while layout.count():
            item = layout.takeAt(0)
            if item is None:
                continue
            widget = item.widget()
            if widget is not None:
                widget.setParent(None)
                widget.deleteLater()

    def _section(self, title: str, body: str, *, icon_name: str | None = None) -> QWidget:
        frame = QFrame(self._body)
        frame.setObjectName("Card")
        layout = QVBoxLayout(frame)
        layout.setContentsMargins(
            theme.SPACE["md"], theme.SPACE["sm"], theme.SPACE["md"], theme.SPACE["md"]
        )
        layout.setSpacing(theme.SPACE["xs"])
        head = QHBoxLayout()
        head.setSpacing(theme.SPACE["sm"])
        if icon_name is not None:
            glyph = QLabel(frame)
            glyph.setPixmap(icons.icon(icon_name, theme.tokens().muted, 14).pixmap(14, 14))
            head.addWidget(glyph)
        head.addWidget(widgets.section_label(title, frame))
        head.addStretch(1)
        layout.addLayout(head)
        text = QLabel(body, frame)
        text.setWordWrap(True)
        text.setObjectName("Muted")
        text.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        layout.addWidget(text)
        self._layout.addWidget(frame)
        return frame

    def _build_header(self, row: opportunities.Opportunity, *, checked: bool) -> None:
        box = QFrame(self._body)
        box.setObjectName("Card")
        layout = QVBoxLayout(box)
        layout.setContentsMargins(
            theme.SPACE["md"], theme.SPACE["sm"], theme.SPACE["md"], theme.SPACE["sm"]
        )
        layout.setSpacing(theme.SPACE["sm"])

        path = widgets.ElidedLabel(row.path, box, mode=Qt.TextElideMode.ElideMiddle)
        path.setObjectName("Mono")
        path.setStyleSheet(f"font-weight: 700; color: {theme.tokens().text};")
        path.setToolTip(row.path)
        layout.addWidget(path)

        chips = widgets.FlowLayout(h_spacing=theme.SPACE["xs"])
        state = widgets.Badge(row.state_label, widgets.state_tone(row.state), box)
        chips.addWidget(state)
        tier = widgets.Badge(row.tier, widgets.tier_tone(row.tier), box)
        chips.addWidget(tier)
        confidence = widgets.Chip(row.confidence, box)
        chips.addWidget(confidence)
        if row.kind_label:
            chips.addWidget(widgets.Badge(row.kind_label, "info", box))
        if checked:
            chips.addWidget(widgets.Badge("Selected", "success", box))
        layout.addLayout(chips)
        self._layout.addWidget(box)

    def _build_metrics(self, row: opportunities.Opportunity) -> None:
        strip = QWidget(self._body)
        layout = widgets.FlowLayout(h_spacing=theme.SPACE["sm"], v_spacing=theme.SPACE["sm"])
        strip.setLayout(layout)
        age = f"{row.age_days} days" if row.age_days is not None else "unknown"
        cards = (
            ("Size", stats.format_bytes(row.size), "folder" if row.is_dir else "file"),
            ("Estimated gain", row.gain_label, "hard-drive"),
            ("Age", age, "refresh-cw"),
        )
        for title, value, icon_name in cards:
            layout.addWidget(widgets.MetricCard(title, value, icon_name=icon_name, parent=strip))
        self._layout.addWidget(strip)

    def _build_solution(self, row: opportunities.Opportunity) -> None:
        display = models.solution_display(row, self.ai_entry())
        source = (
            f"Source: {display.provenance} — advice, not executable"
            if display.from_ai
            else "Source: rule engine" + (f" · pack {row.pack}" if row.pack else "")
        )
        self._section("Suggested solution", f"{source}\n{display.why}", icon_name="list-ordered")
        detail = row.rationale
        if row.rule_id:
            detail += f"\nRule: {row.rule_id}"
            if row.pack:
                detail += f" (pack {row.pack})"
        detail += f"\nCategory: {row.category} · gain basis: {row.gain_basis}"
        if row.native:
            detail += f"\nVendor alternative: {row.native}"
        note = opportunities.advisory_note(row)
        if note:
            detail += f"\n{note}"
        self._section("Reasoning", detail, icon_name="info")

    # -- the AI card ------------------------------------------------------ #

    def _build_ai(self, row: opportunities.Opportunity) -> QWidget:
        """The AI's answer for this row, and the three things to do with it.

        It is a card, not a control panel: the verdict and its provenance read
        first, the buttons last, and every failure the call can hit is a line in
        here rather than a modal box (design §10).
        """
        entry = self.ai_entry()
        frame = QFrame(self._body)
        frame.setObjectName("Card")
        layout = QVBoxLayout(frame)
        layout.setContentsMargins(
            theme.SPACE["md"], theme.SPACE["sm"], theme.SPACE["md"], theme.SPACE["md"]
        )
        layout.setSpacing(theme.SPACE["xs"])

        head = QHBoxLayout()
        head.setSpacing(theme.SPACE["sm"])
        glyph = QLabel(frame)
        glyph.setPixmap(icons.tone_icon("sparkles", "info", 14).pixmap(14, 14))
        head.addWidget(glyph)
        head.addWidget(
            widgets.section_label(
                "AI classification"
                if entry is not None and entry.is_classification
                else "AI suggestion",
                frame,
            )
        )
        head.addStretch(1)
        layout.addLayout(head)

        if self._ai_error:
            layout.addWidget(
                widgets.WarningBanner(Notice.failure(self._ai_error, paths=(row.path,)), frame)
            )
        elif self._ai_busy:
            stage = QLabel(self._ai_stage or "Asking the provider…", frame)
            stage.setObjectName("Faint")
            stage.setWordWrap(True)
            layout.addWidget(stage)

        if entry is None:
            empty = QLabel(
                "No AI answer for this row yet. Ask for one below, or fill the whole list "
                "from the opportunities toolbar.",
                frame,
            )
            empty.setObjectName("Muted")
            empty.setWordWrap(True)
            layout.addWidget(empty)
        else:
            layout.addLayout(self._ai_verdict_row(entry, frame))
            why = QLabel(entry.why, frame)
            why.setWordWrap(True)
            why.setObjectName("Muted")
            why.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
            layout.addWidget(why)
            for line in entry.detail_lines():
                detail = QLabel(line, frame)
                detail.setObjectName("Muted")
                detail.setWordWrap(True)
                layout.addWidget(detail)
            note = QLabel(
                "Advice only: it becomes a decision when you apply it as a rule. Nothing the "
                "AI writes can execute on its own.",
                frame,
            )
            note.setObjectName("Faint")
            note.setWordWrap(True)
            layout.addWidget(note)

        layout.addLayout(self._ai_buttons(entry, frame))
        self._ai_card = frame
        self._layout.addWidget(frame)
        return frame

    def _ai_verdict_row(self, entry: AISuggestion, parent: QWidget) -> QLayout:
        """The badges of one AI answer: what it says, who said it, how sure."""
        row = widgets.FlowLayout(h_spacing=theme.SPACE["xs"])
        row.addWidget(widgets.Badge(entry.label, ai_models_module.tone(entry), parent))
        row.addWidget(widgets.Badge(entry.provenance, "muted", parent))
        row.addWidget(widgets.Chip(entry.confidence, parent))
        if entry.cached:
            row.addWidget(widgets.Badge("cached", "muted", parent))
        if entry.is_classification and entry.category:
            label = f"{entry.category} · {entry.tier}" if entry.tier else entry.category
            row.addWidget(widgets.Badge(label, "info", parent))
        return row

    def _ai_buttons(self, entry: AISuggestion | None, parent: QWidget) -> QLayout:
        """*Suggest* / *Classify* / *Explain* / *Apply as rule…*, in that order."""
        row = widgets.FlowLayout(h_spacing=theme.SPACE["xs"])
        for key, label, use_case, tip in (
            (
                "AiSuggest",
                "Suggest with AI",
                AI_SUGGEST,
                "Ask the provider for a suggested solution for this row",
            ),
            (
                "AiClassify",
                "Classify with AI",
                AI_CLASSIFY,
                "Ask the provider for a category, a tier and an action for this row",
            ),
            (
                "AiExplain",
                "Explain with AI",
                AI_EXPLAIN,
                "Ask for a deep explanation of this selection, streamed here as it is written",
            ),
        ):
            button = QPushButton(label, parent)
            button.setObjectName(key)
            button.setToolTip(tip)
            button.setEnabled(not self._ai_busy)
            button.clicked.connect(lambda _checked=False, use=use_case: self._ask(use))
            row.addWidget(button)
        apply_button = QPushButton("Apply as rule…", parent)
        apply_button.setObjectName("AiPromote")
        apply_button.setToolTip(
            "Write this answer into your user rule pack, after showing you the exact rule"
        )
        apply_button.setEnabled(entry is not None)
        apply_button.clicked.connect(
            lambda _checked=False: self.promoteRequested.emit(self._row.key if self._row else "")
        )
        row.addWidget(apply_button)
        return row

    def _ask(self, use_case: str) -> None:
        """Ask the screen for one AI call about the row on screen."""
        if self._row is None:
            return
        self.clear_ai_error()
        self.aiRequested.emit(use_case, self._row.key)

    # -- the explanation card --------------------------------------------- #

    def _build_explanation(self, row: opportunities.Opportunity) -> QWidget:
        """The streamed explanation: an honest empty state until one is asked for."""
        frame = QFrame(self._body)
        frame.setObjectName("Card")
        layout = QVBoxLayout(frame)
        layout.setContentsMargins(
            theme.SPACE["md"], theme.SPACE["sm"], theme.SPACE["md"], theme.SPACE["md"]
        )
        layout.setSpacing(theme.SPACE["xs"])

        head = QHBoxLayout()
        head.setSpacing(theme.SPACE["sm"])
        glyph = QLabel(frame)
        glyph.setPixmap(icons.tone_icon("sparkles", "info", 14).pixmap(14, 14))
        head.addWidget(glyph)
        head.addWidget(widgets.section_label("Explanation", frame))
        head.addStretch(1)
        layout.addLayout(head)

        if self._explanation_stage:
            stage = QLabel(self._explanation_stage, frame)
            stage.setObjectName("Faint")
            stage.setWordWrap(True)
            layout.addWidget(stage)
        if self._explanation_error:
            layout.addWidget(
                widgets.WarningBanner(
                    Notice.failure(self._explanation_error, paths=(row.path,)), frame
                )
            )
        empty = not self._explanation and not self._explanation_stage
        text = QLabel(
            "Nothing yet: Explain with AI asks the provider what this row is, what it "
            "risks and what the alternatives are, and writes the answer here."
            if empty
            else self._explanation,
            frame,
        )
        text.setObjectName("Muted" if not empty else "Faint")
        text.setWordWrap(True)
        text.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        layout.addWidget(text)
        self._explanation_label = text
        self._explanation_card = frame
        self._layout.addWidget(frame)
        return frame

    # -- live updates ----------------------------------------------------- #

    def _on_ai_changed(self, keys: object) -> None:
        """Repaint the AI card when the row on screen got (or lost) an answer."""
        if self._row is None or self._ai_card is None:
            return
        touched = tuple(keys) if isinstance(keys, (tuple, list, set)) else ()
        if touched and self._row.key not in touched:
            return
        # Reveal before the swap: the card in the slot right now is the one whose
        # geometry is valid (see _reveal), and the slot is where the new one lands.
        if self.ai_entry() is not None:
            self._reveal(self._ai_card)
        self._refresh_ai_card()

    def _reveal(self, widget: QWidget | None) -> None:
        """Scroll the pane so this card's slot is on screen.

        The pane is a scroll area and the AI cards sit below the reasoning, so an
        answer is usually below the fold when it arrives.  The offset comes from
        the widget currently in that slot: a freshly rebuilt card has no geometry
        of its own until the next layout pass, but the slot does not move when it
        is swapped, so what is measured here is still where it will be.
        """
        if widget is None:
            return
        top = widget.mapTo(self._body, QPoint(0, 0)).y()
        self.verticalScrollBar().setValue(max(0, top - theme.SPACE["md"]))

    def _refresh_ai_card(self) -> None:
        """Swap the AI card for a freshly built one (in place: nothing else moves)."""
        if self._row is None or self._ai_card is None:
            return
        self._ai_card = self._swap(self._ai_card, lambda: self._build_ai(self._row))  # type: ignore[arg-type]

    def _rebuild_explanation_card(self) -> None:
        """Show, update or extend the explanation card (streaming writes here)."""
        if self._row is None:
            return
        if self._explanation_card is None:
            self._insert_after(self._ai_card, self._build_explanation(self._row))
            return
        self._explanation_card = self._swap(
            self._explanation_card,
            lambda: self._build_explanation(self._row),  # type: ignore[arg-type]
        )

    def _swap(self, old: QWidget, build: Callable[[], QWidget]) -> QWidget:
        """Replace ``old`` with a freshly built card, keeping its position."""
        index = self._layout.indexOf(old)
        fresh = build()
        old.setParent(None)
        old.deleteLater()
        if index < 0:
            self._layout.addWidget(fresh)
            return fresh
        self._layout.insertWidget(index, fresh)
        return fresh

    def _insert_after(self, anchor: QWidget | None, card: QWidget) -> None:
        """Put ``card`` right after ``anchor`` (the AI card), or at the very end."""
        index = self._layout.indexOf(anchor) if anchor is not None else -1
        if index < 0:
            self._layout.addWidget(card)
        else:
            self._layout.insertWidget(index + 1, card)

    def _build_side_effects(self, row: opportunities.Opportunity) -> None:
        text = opportunities.side_effects(row, target=self._target_drive or None)
        self._section("Side effects", text, icon_name="alert-triangle")

    def _build_destination(self, row: opportunities.Opportunity) -> None:
        frame = QFrame(self._body)
        frame.setObjectName("Card")
        layout = QVBoxLayout(frame)
        layout.setContentsMargins(
            theme.SPACE["md"], theme.SPACE["sm"], theme.SPACE["md"], theme.SPACE["md"]
        )
        layout.setSpacing(theme.SPACE["xs"])
        layout.addWidget(widgets.section_label("Move destination", frame))

        movable = row.action in {"MOVE", "NATIVE"}
        self._destination = QLineEdit(frame)
        self._destination.setPlaceholderText("D:\\Moved\\… (mirrors the source layout)")
        self._destination.setClearButtonEnabled(True)
        self._destination.setEnabled(movable)
        default = ""
        if self._target_drive:
            default = opportunities.destination_for(row, self._target_drive)
        self._destination.setText(self._override or default)
        self._destination.textEdited.connect(self._on_destination_edited)
        layout.addWidget(self._destination)

        self._destination_hint = QLabel("", frame)
        self._destination_hint.setObjectName("Faint")
        self._destination_hint.setWordWrap(True)
        layout.addWidget(self._destination_hint)
        self._update_destination_hint(row)
        self._destination.textChanged.connect(lambda _text: self._update_destination_hint(row))
        if not movable:
            self._destination.setToolTip(
                f"The rules suggest {row.solution.lower()} for this entry, so the plan will not "
                "move it; the destination is only used for relocations."
            )
        self._layout.addWidget(frame)

    def _update_destination_hint(self, row: opportunities.Opportunity) -> None:
        text = self._destination.text().strip()
        if not text:
            self._destination_hint.setText(
                "No destination yet — pick a target drive on Import to get the default."
            )
            return
        notes: list[str] = []
        if not _looks_absolute(text):
            notes.append("A destination must be an absolute path.")
        target = self._target_drive or text
        if self._target_drive:
            link, elevated = opportunities.link_for(row, self._target_drive)
            notes.append(f"The original path becomes a {link.lower()}.")
            if elevated:
                notes.append("Creating it needs elevation (or Developer Mode) on Windows.")
        planned = opportunities.destination_for(row, target)
        if planned and planned != text:
            notes.append(f"The planner mirrors the source below the target: {planned}.")
        self._destination_hint.setText(" ".join(notes))

    def _on_destination_edited(self, text: str) -> None:
        if self._row is not None:
            self.destinationEdited.emit(self._row.key, text.strip())

    def _build_alternatives(self, row: opportunities.Opportunity) -> None:
        frame = QFrame(self._body)
        frame.setObjectName("Card")
        layout = QVBoxLayout(frame)
        layout.setContentsMargins(
            theme.SPACE["md"], theme.SPACE["sm"], theme.SPACE["md"], theme.SPACE["md"]
        )
        layout.setSpacing(theme.SPACE["sm"])
        layout.addWidget(widgets.section_label("Alternatives", frame))
        for item in opportunities.alternatives(row, target=self._target_drive or None):
            line = QWidget(frame)
            row_layout = QHBoxLayout(line)
            row_layout.setContentsMargins(0, 0, 0, 0)
            row_layout.setSpacing(theme.SPACE["sm"])
            glyph = QLabel(line)
            tone = "muted" if not item.available else "accent"
            glyph.setPixmap(
                icons.tone_icon(_alternative_icon(item.action), tone, 14).pixmap(14, 14)
            )
            row_layout.addWidget(glyph)
            name = QLabel(item.label, line)
            name.setStyleSheet(
                f"font-weight: 600; color: "
                f"{theme.tokens().text if item.available else theme.tokens().faint};"
            )
            row_layout.addWidget(name)
            gain = QLabel(item.gain_label, line)
            gain.setFont(theme.mono_font("xs"))
            gain.setObjectName("Faint")
            row_layout.addWidget(gain)
            row_layout.addStretch(1)
            layout.addWidget(line)
            note = item.note if item.available else item.reason
            if note:
                caption = QLabel(note, frame)
                caption.setObjectName("Faint")
                caption.setWordWrap(True)
                caption.setSizePolicy(QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Minimum)
                layout.addWidget(caption)
        self._layout.addWidget(frame)

    def _build_members(self, row: opportunities.Opportunity) -> None:
        if not row.members:
            return
        listing = "\n".join(
            [f"· {member}" for member in row.members]
            + (
                [f"· … and {row.member_count - len(row.members)} more"]
                if row.member_count > len(row.members)
                else []
            )
        )
        self._section(
            f"Covers {row.member_count:,} entries · {stats.format_bytes(row.member_bytes)}",
            listing,
            icon_name="list-ordered",
        )

    def _build_rank(self, row: opportunities.Opportunity) -> None:
        age = f"age {row.age_days}d" if row.age_days is not None else "age unknown"
        text = (
            f"{stats.format_bytes(row.size)} x "
            f"{candidates.TIER_WEIGHTS.get(row.tier, 0.2):.2f} tier x "
            f"{row.confidence:.2f} confidence x recency ({age}) = rank {row.score:,.0f}"
        )
        self._section("Rank", text, icon_name="hard-drive")


def _looks_absolute(path: str) -> bool:
    """True for a Windows drive/UNC path or a POSIX absolute path."""
    if path.startswith(("\\\\", "//")):
        return True
    if len(path) >= 2 and path[1] == ":" and path[0].isalpha():
        return True
    return path.startswith("/")


def _alternative_icon(action: str) -> str:
    """The icon of one alternative (the one map that knows both vocabularies)."""
    return icons.action_icon_name(action)
