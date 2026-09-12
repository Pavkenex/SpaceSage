"""Plan & Execute placeholder (design §9, screen 3 -- built by the S9 slice).

The screen exists, states what will land there and points at where the work
starts (the Opportunities list); nothing here pretends to plan yet.
"""

from __future__ import annotations

from PySide6.QtCore import Signal
from PySide6.QtWidgets import QVBoxLayout, QWidget

from spacesage.app import theme, widgets


class PlanView(QWidget):
    """Screen 3, as an honest placeholder."""

    goToOpportunities = Signal()

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setObjectName("Page")
        layout = QVBoxLayout(self)
        layout.setContentsMargins(
            theme.SPACE["xl"], theme.SPACE["xl"], theme.SPACE["xl"], theme.SPACE["xl"]
        )
        layout.addWidget(
            widgets.EmptyState(
                "No plan yet",
                "This is where selected opportunities become one plan: per-item approval, a "
                "dry-run preview of every resolved operation, execution with live per-item "
                "results and one-click undo. It arrives with the planning slice -- start by "
                "selecting rows in Opportunities.",
                icon_name="clipboard-list",
                action="Go to Opportunities",
                on_action=self.goToOpportunities.emit,
                parent=self,
            )
        )
        layout.addStretch(1)
