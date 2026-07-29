"""Home page (split-session v2).

Fresh rewrite for the session-split refactor. Not derived from the legacy
``pages_legacy/home_page.py``.

Responsibilities:

* Minimal landing page.
* Show current database + loaded-session status.
* Provide a button to enter the Data Manager (subject / session CRUD).

Non-goals:

* No sign-in, hardware connect, cloud sync, or guided-workflow gating on
  the Home page itself. Those are separate concerns that will get their
  own dialogs / pages as needed. Home stays as small as possible so we
  can verify the plumbing works end-to-end from the very start.

Uses a programmatic Qt UI (no ``.ui`` file).
"""

from __future__ import annotations

import logging
from typing import Optional

import qt
import slicer
from slicer.ScriptedLoadableModule import (
    ScriptedLoadableModuleLogic,
    ScriptedLoadableModuleWidget,
)

from OpenLIFULib import get_app_state, get_cur_db


# ---------------------------------------------------------------------------
# Widget
# ---------------------------------------------------------------------------

class OpenLIFUHomeWidget(ScriptedLoadableModuleWidget):
    """Programmatic Qt landing page."""

    def __init__(self, parent=None):
        ScriptedLoadableModuleWidget.__init__(self, parent)
        self.moduleName = "OpenLIFU"
        self.logic: Optional[OpenLIFUHomeLogic] = None
        self._entered = False

    # ------------------------------------------------------------------

    def setup(self) -> None:
        ScriptedLoadableModuleWidget.setup(self)
        self.logic = OpenLIFUHomeLogic()

        top = qt.QWidget()
        outer = qt.QVBoxLayout(top)
        outer.setContentsMargins(16, 16, 16, 16)
        outer.setSpacing(16)

        title = qt.QLabel("OpenLIFU")
        f = title.font
        f.setPointSize(f.pointSize() + 6)
        f.setBold(True)
        title.font = f
        outer.addWidget(title)

        subtitle = qt.QLabel(
            "Low-intensity focused-ultrasound treatment planning."
        )
        subtitle.setStyleSheet("color: #666;")
        outer.addWidget(subtitle)

        # -- Status group -----------------------------------------------
        status_group = qt.QGroupBox("Status")
        status_layout = qt.QFormLayout(status_group)
        self.db_status_label = qt.QLabel("—")
        self.planning_status_label = qt.QLabel("—")
        self.sonication_status_label = qt.QLabel("—")
        status_layout.addRow("Database:", self.db_status_label)
        status_layout.addRow("Planning session:", self.planning_status_label)
        status_layout.addRow("Sonication session:", self.sonication_status_label)
        outer.addWidget(status_group)

        # -- Action buttons ---------------------------------------------
        actions = qt.QVBoxLayout()
        actions.setSpacing(8)

        self.data_manager_button = qt.QPushButton("Open Data Manager")
        self.data_manager_button.setMinimumHeight(40)
        self.data_manager_button.setToolTip(
            "Browse and manage Planning Sessions, Plans, and Sonication Sessions."
        )
        self.data_manager_button.clicked.connect(self._on_data_manager_clicked)
        actions.addWidget(self.data_manager_button)

        outer.addLayout(actions)
        outer.addStretch(1)

        self.layout.addWidget(top)
        self.uiWidget = top

    def enter(self) -> None:
        self._entered = True
        self._refresh_status()

    def exit(self) -> None:
        self._entered = False

    def cleanup(self) -> None:
        pass

    # ------------------------------------------------------------------
    # Refresh helpers
    # ------------------------------------------------------------------

    def _refresh_status(self) -> None:
        db = get_cur_db()
        if db is not None:
            self.db_status_label.text = str(getattr(db, "path", "loaded"))
        else:
            self.db_status_label.text = "(none loaded)"

        state = get_app_state()
        ps = state.loaded_planning_session
        if ps is not None:
            self.planning_status_label.text = (
                f"{ps.get_planning_session_id()} (subject={ps.get_subject_id()})"
            )
        else:
            self.planning_status_label.text = "—"

        ss = state.loaded_sonication_session
        if ss is not None:
            self.sonication_status_label.text = (
                f"{ss.get_sonication_session_id()} (plan={ss.get_plan_id()})"
            )
        else:
            self.sonication_status_label.text = "—"

    # ------------------------------------------------------------------
    # Signal handlers
    # ------------------------------------------------------------------

    def _on_data_manager_clicked(self) -> None:
        """Ask the host to swap the visible page to the Data Manager."""
        try:
            host_widget = slicer.util.getModule("OpenLIFU").widgetRepresentation().self()
            host_widget.show_page("OpenLIFUDataManager")
        except Exception:  # noqa: BLE001
            logging.exception("Home: unable to navigate to Data Manager.")


# ---------------------------------------------------------------------------
# Logic
# ---------------------------------------------------------------------------

class OpenLIFUHomeLogic(ScriptedLoadableModuleLogic):
    """Landing-page logic. Deliberately empty for now."""

    def __init__(self):
        ScriptedLoadableModuleLogic.__init__(self)
