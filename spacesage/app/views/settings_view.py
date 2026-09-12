"""Settings (design §9, screen 4): appearance now, the rest with their slices.

The theme toggle lives here because §9.1 requires one token set with a light/dark
switch that follows the OS by default; thresholds, quarantine location and AI
providers get their own screens when the slices that own them land.
"""

from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import Signal
from PySide6.QtWidgets import (
    QButtonGroup,
    QFormLayout,
    QFrame,
    QHBoxLayout,
    QLabel,
    QRadioButton,
    QVBoxLayout,
    QWidget,
)

from spacesage import stats
from spacesage.app import state, theme, widgets


class SettingsView(QWidget):
    """Screen 4: appearance and the app's data locations."""

    themeModeChanged = Signal(str)

    def __init__(
        self,
        settings: state.Settings,
        *,
        db_path: Path | None = None,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self.setObjectName("Page")
        self._settings = settings
        self._db_path = db_path if db_path is not None else state.index_path()
        self._buttons: dict[str, QRadioButton] = {}
        self._build()

    # -- construction ----------------------------------------------------- #

    def _build(self) -> None:
        layout = QVBoxLayout(self)
        layout.setContentsMargins(
            theme.SPACE["xl"], theme.SPACE["xl"], theme.SPACE["xl"], theme.SPACE["xl"]
        )
        layout.setSpacing(theme.SPACE["lg"])

        header = QWidget(self)
        head = QVBoxLayout(header)
        head.setContentsMargins(0, 0, 0, 0)
        head.setSpacing(theme.SPACE["xs"])
        title = QLabel("Settings", header)
        title.setObjectName("PageTitle")
        head.addWidget(title)
        subtitle = QLabel(
            "Appearance follows the system until you choose otherwise. Everything else lands "
            "with the slice that needs it.",
            header,
        )
        subtitle.setObjectName("PageSubtitle")
        subtitle.setWordWrap(True)
        head.addWidget(subtitle)
        layout.addWidget(header)

        layout.addWidget(self._build_appearance())
        layout.addWidget(self._build_data())
        layout.addWidget(self._build_coming())
        layout.addStretch(1)

    def _build_appearance(self) -> QWidget:
        frame = QFrame(self)
        frame.setObjectName("Card")
        layout = QVBoxLayout(frame)
        layout.setContentsMargins(
            theme.SPACE["lg"], theme.SPACE["md"], theme.SPACE["lg"], theme.SPACE["lg"]
        )
        layout.setSpacing(theme.SPACE["sm"])
        layout.addWidget(widgets.section_label("Appearance", frame))

        row = QHBoxLayout()
        row.setSpacing(theme.SPACE["lg"])
        group = QButtonGroup(frame)
        for mode in theme.MODES:
            button = QRadioButton(theme.MODE_LABELS[mode], frame)
            button.setToolTip(f"Use the {theme.MODE_LABELS[mode].lower()} theme")
            button.toggled.connect(
                lambda checked, chosen=mode: self._on_mode_toggled(chosen, checked)
            )
            group.addButton(button)
            self._buttons[mode] = button
            row.addWidget(button)
        row.addStretch(1)
        layout.addLayout(row)

        hint = QLabel(
            "One token set drives both themes: spacing grid, typography scale, semantic colours, "
            "tier badges and the mono stack for paths and sizes.",
            frame,
        )
        hint.setObjectName("Faint")
        hint.setWordWrap(True)
        layout.addWidget(hint)
        return frame

    def _build_data(self) -> QWidget:
        frame = QFrame(self)
        frame.setObjectName("Card")
        layout = QVBoxLayout(frame)
        layout.setContentsMargins(
            theme.SPACE["lg"], theme.SPACE["md"], theme.SPACE["lg"], theme.SPACE["lg"]
        )
        layout.setSpacing(theme.SPACE["xs"])
        layout.addWidget(widgets.section_label("Index", frame))
        form = QFormLayout()
        form.setSpacing(theme.SPACE["sm"])
        form.addRow("Location", widgets.mono_label(str(self._db_path), frame))
        form.addRow("Contents", QLabel(self._index_description(), frame))
        form.addRow("Data folder", widgets.mono_label(str(state.data_dir()), frame))
        layout.addLayout(form)
        note = QLabel(
            "The index is the imported WizTree export. Re-importing replaces it; nothing on disk "
            "is touched by an analysis.",
            frame,
        )
        note.setObjectName("Faint")
        note.setWordWrap(True)
        layout.addWidget(note)
        return frame

    def _build_coming(self) -> QWidget:
        frame = QFrame(self)
        frame.setObjectName("Card")
        layout = QVBoxLayout(frame)
        layout.setContentsMargins(
            theme.SPACE["lg"], theme.SPACE["md"], theme.SPACE["lg"], theme.SPACE["lg"]
        )
        layout.setSpacing(theme.SPACE["xs"])
        layout.addWidget(widgets.section_label("Coming with their slices", frame))
        for line in (
            "Thresholds — minimum gain, stale age and duplicate floors (planning slice).",
            "Quarantine location and target drives (execution slice).",
            "Rule-pack overrides — user packs shadow built-ins by rule id.",
            "AI providers — off until configured, keys never stored in the index.",
        ):
            item = QLabel(f"· {line}", frame)
            item.setObjectName("Muted")
            item.setWordWrap(True)
            layout.addWidget(item)
        return frame

    # -- behaviour -------------------------------------------------------- #

    def set_mode(self, mode: str) -> None:
        """Reflect the active theme mode in the radio buttons."""
        button = self._buttons.get(mode)
        if button is not None and not button.isChecked():
            blocked = button.blockSignals(True)
            button.setChecked(True)
            button.blockSignals(blocked)

    def current_mode(self) -> str:
        """The mode the radio buttons show."""
        for mode, button in self._buttons.items():
            if button.isChecked():
                return mode
        return theme.MODE_SYSTEM

    def _on_mode_toggled(self, mode: str, checked: bool) -> None:
        if not checked:
            return
        self._settings.set_theme_mode(mode)
        self.themeModeChanged.emit(mode)

    def _index_description(self) -> str:
        if not self._db_path.is_file():
            return "no index yet — import a WizTree export"
        size = stats.format_bytes(self._db_path.stat().st_size)
        return f"{size} on disk"
