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

See ``docs/coding-standards.md`` for the naming convention adopted
here: no leading-underscore prefixes on public-behaviour methods,
signal handlers named ``on_<widget>_<event>``, ``is_``/``has_``
booleans, and docstrings that explain WHY.
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


class OpenLIFUHomeWidget(ScriptedLoadableModuleWidget):
    """Landing page for the OpenLIFU host module.

    Owns three status labels (database, loaded planning session, loaded
    sonication session) and a single "Open Data Manager" button.
    Deliberately holds no domain state -- everything it renders is
    pulled from ``get_app_state()`` and ``get_cur_db()`` at
    ``enter()`` time.
    """

    def __init__(self, parent=None):
        ScriptedLoadableModuleWidget.__init__(self, parent)
        # Every embedded page borrows the host module's resource path so
        # ``self.resourcePath("Icons/foo.png")`` resolves under
        # ``OpenLIFU/Resources/`` rather than a per-page module dir.
        self.moduleName = "OpenLIFU"
        self.logic: Optional[OpenLIFUHomeLogic] = None
        # Tracked so cross-page events (e.g. a scene reset) can distinguish
        # "this page is currently visible" from "this page hasn't been
        # entered yet" without walking Qt visibility state.
        self.is_entered = False

    # ------------------------------------------------------------------
    # Slicer widget lifecycle
    # ------------------------------------------------------------------

    def setup(self) -> None:
        """Build the programmatic Qt UI once, when the widget is created.

        Called by the host module's ``instantiate_page_widget`` helper.
        We do not touch the parameter node or database here; those are
        read on ``enter()``.
        """
        ScriptedLoadableModuleWidget.setup(self)
        self.logic = OpenLIFUHomeLogic()

        top = qt.QWidget()
        outer = qt.QVBoxLayout(top)
        outer.setContentsMargins(16, 16, 16, 16)
        outer.setSpacing(16)

        title = qt.QLabel("OpenLIFU")
        title_font = title.font
        title_font.setPointSize(title_font.pointSize() + 6)
        title_font.setBold(True)
        title.font = title_font
        outer.addWidget(title)

        subtitle = qt.QLabel(
            "Low-intensity focused-ultrasound treatment planning."
        )
        subtitle.setStyleSheet("color: #666;")
        outer.addWidget(subtitle)

        outer.addWidget(self.build_status_group())
        outer.addLayout(self.build_action_row())
        outer.addStretch(1)

        self.layout.addWidget(top)
        # The host looks up ``uiWidget`` when embedding the page into its
        # QStackedWidget. Naming enforced by the host's embed helper.
        self.uiWidget = top

    def enter(self) -> None:
        """Reset first-render state and populate every widget from live sources.

        ``enter`` is the SOLE source of first-render truth (see
        ``docs/coding-standards.md`` rule 4). Nothing else drives this
        page's UI -- there are no cross-page observers.
        """
        self.is_entered = True
        self.refresh_status()

    def exit(self) -> None:
        """Mark the page as no longer visible without tearing down state."""
        self.is_entered = False

    def cleanup(self) -> None:
        """Slicer widget teardown hook; nothing to clean up here."""

    # ------------------------------------------------------------------
    # UI builders
    # ------------------------------------------------------------------

    def build_status_group(self) -> qt.QGroupBox:
        """Build the read-only status group (database + loaded sessions)."""
        group = qt.QGroupBox("Status")
        layout = qt.QFormLayout(group)
        self.db_status_label = qt.QLabel("—")
        self.planning_status_label = qt.QLabel("—")
        self.sonication_status_label = qt.QLabel("—")
        layout.addRow("Database:", self.db_status_label)
        layout.addRow("Planning session:", self.planning_status_label)
        layout.addRow("Sonication session:", self.sonication_status_label)
        return group

    def build_action_row(self) -> qt.QVBoxLayout:
        """Build the action buttons block (currently: Data Manager only)."""
        row = qt.QVBoxLayout()
        row.setSpacing(8)

        self.data_manager_button = qt.QPushButton("Open Data Manager")
        self.data_manager_button.setMinimumHeight(40)
        self.data_manager_button.setToolTip(
            "Browse and manage Planning Sessions, Plans, and Sonication Sessions."
        )
        self.data_manager_button.clicked.connect(self.on_data_manager_button_clicked)
        row.addWidget(self.data_manager_button)
        return row

    # ------------------------------------------------------------------
    # Refresh
    # ------------------------------------------------------------------

    def refresh_status(self) -> None:
        """Repopulate the status labels from the currently-loaded database
        and application state.

        Idempotent: every call regenerates every label from live sources.
        Safe to invoke from ``enter()`` and from any future signal handler
        that needs to force a repaint.
        """
        database = get_cur_db()
        if database is not None:
            self.db_status_label.text = str(getattr(database, "path", "loaded"))
        else:
            self.db_status_label.text = "(none loaded)"

        state = get_app_state()
        planning_session = state.loaded_planning_session
        if planning_session is not None:
            self.planning_status_label.text = (
                f"{planning_session.get_planning_session_id()} "
                f"(subject={planning_session.get_subject_id()})"
            )
        else:
            self.planning_status_label.text = "—"

        sonication_session = state.loaded_sonication_session
        if sonication_session is not None:
            self.sonication_status_label.text = (
                f"{sonication_session.get_sonication_session_id()} "
                f"(plan={sonication_session.get_plan_id()})"
            )
        else:
            self.sonication_status_label.text = "—"

    # ------------------------------------------------------------------
    # Signal handlers
    # ------------------------------------------------------------------

    def on_data_manager_button_clicked(self) -> None:
        """Ask the host module to swap the visible page to the Data Manager.

        We navigate through the host rather than through
        ``slicer.util.selectModule`` because the pages are embedded in
        the host's QStackedWidget; the host owns the swap machinery.
        """
        try:
            host_widget = slicer.util.getModule("OpenLIFU").widgetRepresentation().self()
            host_widget.show_page("OpenLIFUDataManager")
        except Exception:  # noqa: BLE001
            logging.exception("Home: unable to navigate to Data Manager.")


class OpenLIFUHomeLogic(ScriptedLoadableModuleLogic):
    """Landing-page business logic.

    Currently empty. Kept in place so the host module's
    ``home_logic`` attribute always references a construct-able object,
    and so future landing-page behaviour (e.g. env-var-driven mode
    locks, sign-in-required gating) has a home.
    """
