"""The application shell: navigation rail, pages, status bar (design §9).

One window, left navigation with Import / Opportunities / Plan / Settings, a
status bar that always says where the data came from and that analysis is
read-only, and keyboard access to every page (Ctrl+1..4) with visible focus.
"""

from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import QSize, Qt, Signal
from PySide6.QtGui import QCloseEvent, QKeySequence, QShortcut
from PySide6.QtWidgets import (
    QButtonGroup,
    QHBoxLayout,
    QLabel,
    QMainWindow,
    QPushButton,
    QStackedWidget,
    QStatusBar,
    QVBoxLayout,
    QWidget,
)

from spacesage import opportunities
from spacesage.app import icons, state, theme, widgets
from spacesage.app.views.import_view import ImportView
from spacesage.app.views.opportunities_view import OpportunitiesView
from spacesage.app.views.plan_view import PlanPage
from spacesage.app.views.settings_view import SettingsView

PAGES: tuple[tuple[str, str, str], ...] = (
    ("import", "Import", "folder-open"),
    ("opportunities", "Opportunities", "list-ordered"),
    ("plan", "Plan", "clipboard-list"),
    ("settings", "Settings", "settings"),
)
"""``(key, label, icon)`` of every page, in navigation order."""


class MainWindow(QMainWindow):
    """The product window."""

    themeModeChanged = Signal(str)

    def __init__(
        self,
        settings: state.Settings,
        *,
        db_path: Path | None = None,
        data_root: Path | None = None,
        theme_manager: theme.ThemeManager | None = None,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self._settings = settings
        self._db_path = db_path if db_path is not None else state.index_path()
        self._data_root = data_root if data_root is not None else state.data_dir()
        self._theme_manager = theme_manager
        self.setWindowTitle("SpaceSage")
        self.resize(1440, 900)
        self.setMinimumSize(980, 620)
        self._build()
        self.apply_theme()

    # -- construction ----------------------------------------------------- #

    def _build(self) -> None:
        central = QWidget(self)
        layout = QHBoxLayout(central)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)
        layout.addWidget(self._build_rail())

        self.stack = QStackedWidget(central)
        self.import_view = ImportView(self._settings, db_path=self._db_path, parent=self.stack)
        self.opportunities_view = OpportunitiesView(self.stack)
        self.plan_page = PlanPage(
            self._data_root,
            db_path=self._db_path,
            settings=self._settings,
            parent=self.stack,
        )
        self.settings_view = SettingsView(self._settings, db_path=self._db_path, parent=self.stack)
        for widget in (
            self.import_view,
            self.opportunities_view,
            self.plan_page,
            self.settings_view,
        ):
            self.stack.addWidget(widget)
        layout.addWidget(self.stack, 1)
        self.setCentralWidget(central)

        self.import_view.analysisReady.connect(self._on_analysis_ready)
        self.import_view.busyChanged.connect(self._on_busy_changed)
        self.opportunities_view.statusMessage.connect(self.set_status)
        self.opportunities_view.buildPlanRequested.connect(self.build_plan)
        self.plan_page.goToOpportunities.connect(lambda: self.navigate("opportunities"))
        self.plan_page.statusMessage.connect(self.set_status)
        self.settings_view.themeModeChanged.connect(self.themeModeChanged.emit)

        self._build_status_bar()
        self._shortcuts()
        self.navigate("import")

    def _build_rail(self) -> QWidget:
        rail = QWidget(self)
        rail.setObjectName("NavRail")
        rail.setFixedWidth(196)
        layout = QVBoxLayout(rail)
        layout.setContentsMargins(
            theme.SPACE["md"], theme.SPACE["md"], theme.SPACE["md"], theme.SPACE["md"]
        )
        layout.setSpacing(theme.SPACE["xs"])

        title = QLabel("SPACESAGE", rail)
        title.setObjectName("RailTitle")
        layout.addWidget(title)
        layout.addSpacing(theme.SPACE["sm"])

        self._group = QButtonGroup(rail)
        self._group.setExclusive(True)
        self._nav_buttons: dict[str, QPushButton] = {}
        for index, (key, label, icon_name) in enumerate(PAGES):
            button = QPushButton(label, rail)
            button.setObjectName("NavButton")
            button.setCheckable(True)
            button.setIcon(icons.icon(icon_name, theme.tokens().muted, 16))
            button.setIconSize(QSize(16, 16))
            button.setToolTip(f"{label} (Ctrl+{index + 1})")
            button.clicked.connect(lambda _checked=False, name=key: self.navigate(name))
            self._group.addButton(button)
            self._nav_buttons[key] = button
            layout.addWidget(button)

        layout.addStretch(1)
        self.rail_footer = QLabel(
            "Analysis is read-only.\nSpaceSage only acts on a plan you approve.", rail
        )
        self.rail_footer.setObjectName("Faint")
        self.rail_footer.setWordWrap(True)
        layout.addWidget(self.rail_footer)
        return rail

    def _build_status_bar(self) -> None:
        bar = QStatusBar(self)
        bar.setSizeGripEnabled(False)
        self.status_dataset = QLabel("No analysis yet", bar)
        bar.addWidget(self.status_dataset, 1)
        self.status_message = widgets.ElidedLabel("", bar, mode=Qt.TextElideMode.ElideRight)
        self.status_message.setObjectName("Faint")
        bar.addPermanentWidget(self.status_message)
        self.status_theme = QLabel("", bar)
        self.status_theme.setObjectName("Faint")
        bar.addPermanentWidget(self.status_theme)
        self.setStatusBar(bar)

    def _shortcuts(self) -> None:
        for index, (key, _label, _icon) in enumerate(PAGES):
            shortcut = QShortcut(QKeySequence(f"Ctrl+{index + 1}"), self)
            shortcut.activated.connect(lambda name=key: self.navigate(name))
        focus = QShortcut(QKeySequence("Ctrl+F"), self)
        focus.activated.connect(self._focus_search)

    # -- navigation ------------------------------------------------------- #

    def _index_of(self, name: str) -> int:
        for index, (key, _label, _icon) in enumerate(PAGES):
            if key == name:
                return index
        return -1

    def navigate(self, name: str) -> bool:
        """Show a page by key (``import``/``opportunities``/``plan``/``settings``)."""
        index = self._index_of(name)
        if index < 0:
            return False
        self.stack.setCurrentIndex(index)
        button = self._nav_buttons.get(name)
        if button is not None:
            blocked = button.blockSignals(True)
            button.setChecked(True)
            button.blockSignals(blocked)
        current = self.stack.currentWidget()
        if current is not None:
            theme.fade_in(current, duration=theme.MOTION_FAST)
        return True

    def current_page(self) -> str:
        """The key of the page on screen."""
        index = self.stack.currentIndex()
        return PAGES[index][0] if 0 <= index < len(PAGES) else ""

    def _focus_search(self) -> None:
        self.navigate("opportunities")
        self.opportunities_view.search.setFocus()

    # -- data ------------------------------------------------------------- #

    def _on_analysis_ready(self, listing: object) -> None:
        if not isinstance(listing, opportunities.OpportunityList):
            return
        self.set_listing(listing)
        self.navigate("opportunities")
        widgets.Toast.pop_up(
            self,
            f"Analysis done: {listing.heading()}",
            tone="success",
        )

    def set_listing(self, listing: opportunities.OpportunityList | None) -> None:
        """Adopt an analysis and show it."""
        self.opportunities_view.set_target_drive(self.import_view.target_drive())
        self.opportunities_view.set_listing(listing)
        if listing is None:
            self.status_dataset.setText("No analysis yet")
        else:
            self.status_dataset.setText(listing.heading())

    def listing(self) -> opportunities.OpportunityList | None:
        """The analysis currently shown."""
        return self.opportunities_view.listing()

    def build_plan(self, paths: object) -> bool:
        """Compose a plan out of the checked rows and put the user on it (design §9, screen 3)."""
        listing = self.listing()
        if listing is None:
            return False
        wanted = [str(path) for path in paths] if isinstance(paths, (list, tuple)) else []
        if not wanted:
            return False
        self.navigate("plan")
        return self.plan_page.plan.build(listing, wanted)

    def _on_busy_changed(self, busy: bool) -> None:
        self.status_message.setText("Analyzing…" if busy else "")

    def set_status(self, message: str) -> None:
        """Put a one-line message in the status bar."""
        self.status_message.setText(message)

    # -- theme ------------------------------------------------------------ #

    def apply_theme(self) -> None:
        """Re-tint every hand-painted part after a theme change."""
        active = theme.tokens()
        for key, _label, icon_name in PAGES:
            tone = "accent" if self.current_page() == key else "muted"
            self._nav_buttons[key].setIcon(icons.tone_icon(icon_name, tone, 16))
        self.rail_footer.setStyleSheet(f"color: {active.faint};")
        self.status_theme.setText(f"{active.name.capitalize()} theme")
        self.opportunities_view.apply_theme()
        self.plan_page.apply_theme()
        self.settings_view.set_mode(self._theme_manager.mode if self._theme_manager else "system")

    # -- lifecycle -------------------------------------------------------- #

    def closeEvent(self, event: QCloseEvent) -> None:
        """Wait for a running analysis or run so nothing is killed mid-write."""
        self.import_view.shutdown()
        self.plan_page.shutdown()
        super().closeEvent(event)
