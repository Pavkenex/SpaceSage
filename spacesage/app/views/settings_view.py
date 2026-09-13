"""Settings (design §9, screen 4): appearance, the AI layer, the app's data.

The theme toggle lives here because §9.1 requires one token set with a light/dark
switch that follows the OS by default.  The AI card is the one place the layer is
configured (design §10): which providers exist, what each one calls, whether API
keys can be found, and the two policy switches that decide what may leave the
machine.  A key itself is never stored or shown -- the field names the environment
variable and says whether it is set.

Everything on this screen writes the user's own ``ai.toml`` through
:class:`~spacesage.app.ai_models.AIService`; nothing here calls a provider on its
own except *Test connection* and *Refresh models*, and both say so.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QButtonGroup,
    QCheckBox,
    QComboBox,
    QFormLayout,
    QFrame,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMenu,
    QPushButton,
    QRadioButton,
    QVBoxLayout,
    QWidget,
)

from spacesage import stats
from spacesage.ai import CheckResult, ProviderConfig
from spacesage.ai import config as ai_config
from spacesage.app import ai_models, state, theme, widgets
from spacesage.app.ai_models import AIService
from spacesage.app.workers import BackgroundTask, ConnectionWorker


class SettingsView(QWidget):
    """Screen 4: appearance, AI providers and the app's data locations."""

    themeModeChanged = Signal(str)

    aiChanged = Signal()
    """A provider was added, edited or tested: the shell re-reads the AI status."""

    def __init__(
        self,
        settings: state.Settings,
        *,
        db_path: Path | None = None,
        ai: AIService | None = None,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self.setObjectName("Page")
        self._settings = settings
        self._db_path = db_path if db_path is not None else state.index_path()
        self._ai = ai if ai is not None else AIService(parent=self)
        self._buttons: dict[str, QRadioButton] = {}
        self._task: BackgroundTask | None = None
        self._test_lines: tuple[str, ...] = ()
        self._test_tone = "muted"
        self._build()
        self._ai.configChanged.connect(lambda _config: self.refresh_ai())
        self._ai.statusChanged.connect(lambda _status: self.refresh_ai())

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
            "Appearance follows the system until you choose otherwise. The AI layer is off until "
            "you add a provider, and only ever writes a rule pack from an answer it gave.",
            header,
        )
        subtitle.setObjectName("PageSubtitle")
        subtitle.setWordWrap(True)
        head.addWidget(subtitle)
        layout.addWidget(header)

        layout.addWidget(self._build_appearance())
        layout.addWidget(self._build_ai())
        layout.addWidget(self._build_data())
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

    def _build_ai(self) -> QWidget:
        """The AI card: providers, what each calls, keys and the two policies."""
        frame = QFrame(self)
        frame.setObjectName("Card")
        self.ai_card = frame
        layout = QVBoxLayout(frame)
        layout.setContentsMargins(
            theme.SPACE["lg"], theme.SPACE["md"], theme.SPACE["lg"], theme.SPACE["lg"]
        )
        layout.setSpacing(theme.SPACE["sm"])
        layout.addWidget(widgets.section_label("AI", frame))

        status_row = QHBoxLayout()
        status_row.setSpacing(theme.SPACE["sm"])
        self.ai_status = widgets.Badge("", "muted", frame)
        self.ai_status.setObjectName("AiSettingsStatus")
        status_row.addWidget(self.ai_status)
        self.ai_status_line = widgets.ElidedLabel("", frame, mode=Qt.TextElideMode.ElideRight)
        self.ai_status_line.setObjectName("Faint")
        status_row.addWidget(self.ai_status_line, 1)
        layout.addLayout(status_row)

        pick_row = QHBoxLayout()
        pick_row.setSpacing(theme.SPACE["sm"])
        self.provider_combo = QComboBox(frame)
        self.provider_combo.setObjectName("AiProvider")
        self.provider_combo.currentIndexChanged.connect(lambda _index: self._load_provider())
        pick_row.addWidget(self.provider_combo, 1)

        self.add_button = QPushButton("Add provider", frame)
        self.add_button.setObjectName("AiAddProvider")
        self.add_button.setMenu(self._preset_menu())
        pick_row.addWidget(self.add_button)

        self.default_button = QPushButton("Make default", frame)
        self.default_button.setObjectName("AiMakeDefault")
        self.default_button.clicked.connect(self._on_make_default)
        pick_row.addWidget(self.default_button)

        self.remove_button = QPushButton("Remove", frame)
        self.remove_button.setObjectName("AiRemoveProvider")
        self.remove_button.clicked.connect(self._on_remove_provider)
        pick_row.addWidget(self.remove_button)
        layout.addLayout(pick_row)

        form = QFormLayout()
        form.setSpacing(theme.SPACE["sm"])
        self.base_url = QLineEdit(frame)
        self.base_url.setObjectName("AiBaseUrl")
        form.addRow("Base URL", self.base_url)

        model_row = QHBoxLayout()
        model_row.setSpacing(theme.SPACE["sm"])
        self.model = QComboBox(frame)
        self.model.setObjectName("AiModel")
        self.model.setEditable(True)
        model_row.addWidget(self.model, 1)
        self.models_button = QPushButton("Refresh models", frame)
        self.models_button.setObjectName("AiRefreshModels")
        self.models_button.setToolTip("Ask the provider's /models endpoint what it serves")
        self.models_button.clicked.connect(self._on_refresh_models)
        model_row.addWidget(self.models_button)
        form.addRow("Model", model_row)

        key_row = QHBoxLayout()
        key_row.setSpacing(theme.SPACE["sm"])
        self.api_key_env = QLineEdit(frame)
        self.api_key_env.setObjectName("AiKeyEnv")
        self.api_key_env.setPlaceholderText("OPENAI_API_KEY")
        key_row.addWidget(self.api_key_env, 1)
        self.key_badge = widgets.Badge("", "muted", frame)
        self.key_badge.setObjectName("AiKeyState")
        key_row.addWidget(self.key_badge)
        form.addRow("Key env var", key_row)
        layout.addLayout(form)

        save_row = QHBoxLayout()
        save_row.setSpacing(theme.SPACE["sm"])
        self.save_button = QPushButton("Save provider", frame)
        self.save_button.setObjectName("AiSaveProvider")
        self.save_button.clicked.connect(self._on_save_provider)
        save_row.addWidget(self.save_button)
        self.test_button = QPushButton("Test connection", frame)
        self.test_button.setObjectName("AiTest")
        self.test_button.setToolTip("One request to /models; nothing else is sent")
        self.test_button.clicked.connect(self._on_test_connection)
        save_row.addWidget(self.test_button)
        save_row.addStretch(1)
        layout.addLayout(save_row)

        policy_row = QHBoxLayout()
        policy_row.setSpacing(theme.SPACE["lg"])
        self.redact_box = QCheckBox("Replace paths with tokens before sending", frame)
        self.redact_box.setObjectName("AiRedact")
        self.redact_box.toggled.connect(lambda checked: self._on_policy("redact_paths", checked))
        policy_row.addWidget(self.redact_box)
        self.local_box = QCheckBox("Local endpoints only (nothing may leave this machine)", frame)
        self.local_box.setObjectName("AiLocalOnly")
        self.local_box.toggled.connect(lambda checked: self._on_policy("local_only", checked))
        policy_row.addWidget(self.local_box)
        policy_row.addStretch(1)
        layout.addLayout(policy_row)

        stream_row = QHBoxLayout()
        stream_row.setSpacing(theme.SPACE["lg"])
        self.stream_box = QCheckBox("Stream answers as they arrive", frame)
        self.stream_box.setObjectName("AiStreaming")
        self.stream_box.setToolTip(
            "Answers stream token by token while they are written. Off: one request, "
            "one complete answer (some gateways prefer it)."
        )
        self.stream_box.toggled.connect(lambda checked: self._on_policy("streaming", checked))
        stream_row.addWidget(self.stream_box)
        stream_row.addStretch(1)
        layout.addLayout(stream_row)

        cache_row = QHBoxLayout()
        cache_row.setSpacing(theme.SPACE["sm"])
        self.cache_line = widgets.ElidedLabel("", frame, mode=Qt.TextElideMode.ElideMiddle)
        self.cache_line.setObjectName("Faint")
        cache_row.addWidget(self.cache_line, 1)
        self.clear_cache_button = QPushButton("Clear cache", frame)
        self.clear_cache_button.setObjectName("AiClearCache")
        self.clear_cache_button.setToolTip("Answers already validated are re-asked after this")
        self.clear_cache_button.clicked.connect(self._on_clear_cache)
        cache_row.addWidget(self.clear_cache_button)
        layout.addLayout(cache_row)

        self.ai_result = QLabel("", frame)
        self.ai_result.setObjectName("Faint")
        self.ai_result.setWordWrap(True)
        self.ai_result.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        layout.addWidget(self.ai_result)

        self.refresh_ai()
        return frame

    def _preset_menu(self) -> QMenu:
        """The quick-add menu: every preset, with what it is in the tooltip."""
        menu = QMenu(self)
        for kind in ai_config.preset_choices():
            preset = ai_config.PRESETS.get(kind)
            label = preset.title if preset is not None else kind.capitalize()
            action = menu.addAction(label)
            action.setObjectName(f"AiPreset{kind.capitalize()}")
            if preset is not None and preset.note:
                action.setToolTip(preset.note)
            action.triggered.connect(lambda _checked=False, chosen=kind: self.add_provider(chosen))
        return menu

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

    # -- AI card behaviour ------------------------------------------------ #

    def refresh_ai(self) -> None:
        """Re-read the layer: which provider is selected, keys, cache, meter."""
        config = self._ai.config()
        status = self._ai.status()
        self.ai_status.setText(ai_models.state_line(status))
        self.ai_status.set_tone("muted" if not status.ready else "info")
        self.ai_status.setToolTip(ai_models.state_tooltip(status))
        self.ai_status_line.setText(
            ai_models.readiness_hint(status, configured=bool(config.providers))
            or ai_models.meter_text(self._ai.meter_snapshot())
            or "Ready"
        )

        names = config.provider_names()
        current = self.provider_combo.currentData()  # the name, not the "name · title" label
        blocked = self.provider_combo.blockSignals(True)
        self.provider_combo.clear()
        for name in names:
            self.provider_combo.addItem(f"{name} · {config.provider(name).title}", name)
        if current in names:
            self.provider_combo.setCurrentIndex(names.index(str(current)))
        self.provider_combo.blockSignals(blocked)
        has_provider = bool(names)
        self.provider_combo.setEnabled(has_provider)
        self.default_button.setEnabled(has_provider)
        self.remove_button.setEnabled(has_provider)
        self.save_button.setEnabled(has_provider)
        self.test_button.setEnabled(has_provider)
        self.models_button.setEnabled(has_provider)
        self.clear_cache_button.setEnabled(status.cache_enabled and status.cache_entries > 0)

        self.redact_box.setChecked(config.redact_paths)
        self.local_box.setChecked(config.local_only)
        self.stream_box.setChecked(config.streaming)
        cache_size = ""
        if status.cache_dir:
            cache_size = (
                f"{status.cache_entries:,} answer(s) in {status.cache_dir}"
                if status.cache_enabled
                else "cache off"
            )
        meter = ai_models.meter_text(self._ai.meter_snapshot())
        self.cache_line.setText(cache_size + (f" · this session: {meter}" if meter else ""))
        self._load_provider()
        self._render_result()

    def current_provider_name(self) -> str:
        """The provider the form is editing (``""`` when none is configured)."""
        return str(self.provider_combo.currentData() or "")

    def _load_provider(self) -> None:
        """Put the selected provider's settings in the form."""
        name = self.current_provider_name()
        if not name:
            self.base_url.clear()
            self.model.setCurrentText("")
            self.api_key_env.clear()
            self.key_badge.setText("no provider")
            self.key_badge.set_tone("muted")
            return
        provider = self._ai.config().provider(name)
        self.base_url.setText(provider.base_url)
        self.model.setCurrentText(provider.model)
        self.api_key_env.setText(provider.api_key_env or "")
        set_key = provider.api_key(env=self._ai.env()) is not None
        self.key_badge.setText("key found" if set_key else "not set")
        self.key_badge.set_tone("success" if set_key else "muted")
        self.key_badge.setToolTip(
            f"{provider.api_key_env} is set in this environment"
            if set_key
            else (
                f"{provider.api_key_env} is not set — export it and restart the app"
                if provider.api_key_env
                else "this provider needs no key"
            )
        )

    def add_provider(self, kind: str) -> bool:
        """Add a provider from a preset, with a name that does not collide."""
        config = self._ai.config()
        name = self._unique_name(kind, config.provider_names())
        try:
            provider = ProviderConfig.from_preset(name, kind)
        except Exception as exc:  # AIError: an unknown preset
            self._report(str(exc), tone="danger")
            return False
        self._write(config.with_provider(provider))
        index = self.provider_combo.findData(name)
        if index >= 0:
            self.provider_combo.setCurrentIndex(index)  # the form now edits the new one
        self._load_provider()
        self._report(f"Added {name} ({provider.title}) — set the model, then test it")
        return True

    def _unique_name(self, kind: str, taken: tuple[str, ...]) -> str:
        """``ollama``, then ``ollama-2``, ``ollama-3`` ..."""
        if kind not in taken:
            return kind
        index = 2
        while f"{kind}-{index}" in taken:
            index += 1
        return f"{kind}-{index}"

    def _on_make_default(self) -> bool:
        name = self.current_provider_name()
        if not name:
            return False
        self._write(self._ai.config().with_settings(default_provider=name))
        self._report(f"{name} is now the provider every call uses")
        return True

    def _on_remove_provider(self) -> bool:
        name = self.current_provider_name()
        if not name:
            return False
        self._write(self._ai.config().without_provider(name))
        self._report(f"Removed {name}")
        return True

    def _on_save_provider(self) -> bool:
        """Write the form back into the selected provider (the file is the state)."""
        name = self.current_provider_name()
        if not name:
            return False
        provider = self._ai.config().provider(name)
        model = self.model.currentText().strip()
        key_env = self.api_key_env.text().strip()
        updated = ProviderConfig(
            name=provider.name,
            kind=provider.kind,
            base_url=self.base_url.text().strip() or provider.base_url,
            model=model or provider.model,
            api_key_env=key_env or None,
            api_key_file=provider.api_key_file,
            pricing_in=provider.pricing_in,
            pricing_out=provider.pricing_out,
            timeout_s=provider.timeout_s,
            stream=provider.stream,
            json_mode=provider.json_mode,
            extra_headers=provider.extra_headers,
        )
        self._write(self._ai.config().with_provider(updated))
        self._report(f"Saved {name} — {updated.base_url} · {updated.model}")
        return True

    def _on_policy(self, key: str, checked: bool) -> None:
        """The switches that decide what leaves the machine, and how it comes back."""
        config = self._ai.config()
        if bool(getattr(config, key, False)) == checked:
            return
        self._write(config.with_settings(**{key: checked}))
        if key == "redact_paths":
            self._report(
                "Paths are replaced by tokens before anything is sent"
                if checked
                else "Real paths are sent to the provider"
            )
        elif key == "streaming":
            self._report(
                "Answers stream in as they are written"
                if checked
                else "Answers arrive in one piece"
            )
        else:
            self._report(
                "Local-only: a call to anything but a loopback endpoint is refused"
                if checked
                else "Remote endpoints are allowed again"
            )

    def _on_clear_cache(self) -> int:
        """Forget every validated answer (the next run asks again)."""
        try:
            removed = self._ai.engine().clear_cache()
        except Exception as exc:  # AIError: nothing configured yet
            self._report(str(exc), tone="danger")
            return 0
        self.refresh_ai()
        self._report(f"Cleared {removed:,} cached answer(s)")
        self.aiChanged.emit()
        return removed

    def _on_test_connection(self) -> bool:
        """One request to /models, on a worker, in the selected provider's name."""
        name = self.current_provider_name()
        if name:
            return self.test_connection(name)
        return False

    def test_connection(self, provider: str | None = None) -> bool:
        """Ask the provider what it serves (the only call this screen makes)."""
        if self._task is not None and self._task.is_running():
            self._report("A test is already running")
            return False
        worker = ConnectionWorker(service=self._ai, provider=provider)
        task = BackgroundTask(worker, self)
        task.stage.connect(self._on_ai_stage)
        task.finished.connect(self._on_check_finished)
        task.failed.connect(self._on_check_failed)
        self._task = task
        self.test_button.setEnabled(False)
        task.start()
        return True

    def _on_ai_stage(self, stage: str) -> None:
        self._render_result((f"{stage}…",), tone="muted")

    def _on_check_finished(self, result: object) -> None:
        self._task = None
        self.test_button.setEnabled(bool(self._ai.config().providers))
        if not isinstance(result, CheckResult):
            self._report("The provider sent something that is not a check result", tone="danger")
            return
        self._render_result(
            ai_models.check_lines(result), tone="success" if result.ok else "danger"
        )
        self.aiChanged.emit()

    def _on_check_failed(self, message: str) -> None:
        self._task = None
        self.test_button.setEnabled(bool(self._ai.config().providers))
        self._render_result((message,), tone="danger")
        self.aiChanged.emit()

    def _on_refresh_models(self) -> bool:
        """List the models the provider offers into the editable model combo."""
        name = self.current_provider_name()
        if not name:
            return False
        worker = ConnectionWorker(service=self._ai, provider=name)
        task = BackgroundTask(worker, self)

        def done(result: object) -> None:
            self._task = None
            self._on_check_finished(result)
            raw_models: Any = getattr(result, "models", None)
            models = tuple(raw_models or ())
            if not models:
                return
            current = self.model.currentText()
            blocked = self.model.blockSignals(True)
            self.model.clear()
            for info in models:
                self.model.addItem(str(getattr(info, "id", info)))
            self.model.setCurrentText(current or str(getattr(models[0], "id", "")))
            self.model.blockSignals(blocked)

        task.finished.connect(done)
        task.failed.connect(self._on_check_failed)
        self._task = task
        task.start()
        return True

    def _render_result(self, lines: tuple[str, ...] | None = None, *, tone: str = "muted") -> None:
        """Show the last test result (or the last local report) under the form."""
        if lines is not None:
            self._test_lines = tuple(lines)
            self._test_tone = tone
        text = "\n".join(self._test_lines)
        self.ai_result.setText(text)
        colours = theme.tokens()
        colour = {
            "danger": colours.danger,
            "success": colours.success,
            "warning": colours.warning,
        }.get(self._test_tone, colours.faint)
        self.ai_result.setStyleSheet(f"color: {colour};")

    def _report(self, message: str, *, tone: str = "info") -> None:
        """A local note (nothing was called): shown where a test result would be."""
        self._render_result((message,), tone=tone)
        self.aiChanged.emit()

    def _write(self, config: object) -> None:
        """Write the configuration file and adopt it (one path for every edit)."""
        try:
            self._ai.save(config)  # type: ignore[arg-type]
        except Exception as exc:  # OSError: the file could not be written
            self._report(f"The configuration could not be written: {exc}", tone="danger")

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

    def shutdown(self) -> None:
        """Wait for a running test (window close, tests)."""
        if self._task is not None and self._task.is_running():
            self._task.wait(30_000)
