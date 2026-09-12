"""Details pane: everything about the selected row (design §9, screen 2).

Full reasoning (why, rationale, rule and pack), side effects in the plan's own
words, the alternatives with the reason each is or is not on the table, the move
destination editor feeding the planner's arithmetic, and the members a grouped
row covers.  The pane never decides anything itself: wording, gains and link
policy all come from :mod:`spacesage.opportunities` and
:mod:`spacesage.planner`.
"""

from __future__ import annotations

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QFrame,
    QHBoxLayout,
    QLabel,
    QLayout,
    QLineEdit,
    QScrollArea,
    QSizePolicy,
    QVBoxLayout,
    QWidget,
)

from spacesage import candidates, opportunities, stats
from spacesage.app import icons, theme, widgets

_MISSING = "—"


class DetailsPane(QScrollArea):
    """The selected opportunity, in full."""

    destinationEdited = Signal(str, str)
    """``(row key, destination text)`` -- empty text means "no override"."""

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setWidgetResizable(True)
        self.setFrameShape(QFrame.Shape.NoFrame)
        self.setMinimumWidth(300)
        self._target_drive = ""
        self._row: opportunities.Opportunity | None = None
        self._override = ""

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

        path = widgets.mono_label(row.path, box)
        path.setStyleSheet(f"font-weight: 700; color: {theme.tokens().text};")
        layout.addWidget(path)

        chips = QHBoxLayout()
        chips.setSpacing(theme.SPACE["xs"])
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
        chips.addStretch(1)
        layout.addLayout(chips)
        self._layout.addWidget(box)

    def _build_metrics(self, row: opportunities.Opportunity) -> None:
        strip = QWidget(self._body)
        layout = QHBoxLayout(strip)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(theme.SPACE["sm"])
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
        self._section("Suggested solution", row.why, icon_name="list-ordered")
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
    """The icon of one alternative (mirrors the list's suggested-solution icons)."""
    return {
        "DELETE_QUARANTINE": "trash-2",
        "MOVE": "arrow-right-left",
        "COMPRESS_NTFS": "minimize-2",
        "NATIVE": "terminal",
        "REVIEW": "help-circle",
    }.get(action, "info")
