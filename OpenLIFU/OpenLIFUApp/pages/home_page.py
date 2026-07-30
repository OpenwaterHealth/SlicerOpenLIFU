"""Home page (split-session v2).

Landing page for the OpenLIFU host module. Owns:

* Auto-connect to the last-used database (SlicerOpenLIFU#635). On
  every ``enter()``, if no database is currently loaded, ask
  ``DatabaseLogic.try_auto_connect()`` to re-open the path persisted
  in ``QSettings("OpenLIFU/databaseDirectory")``.
* Read-only status labels for the current database and any loaded
  planning / sonication session.
* Four prominent launch buttons for the clinical workflow:

  * New Planning Session
  * Continue Planning Session
  * New Sonication Session
  * Continue Sonication Session

* A smaller "Open Data Manager…" link for admin actions.

Non-goals:

* No sign-in, hardware-connect, or cloud-sync gating.
* No guided-workflow timeline participation.
* No cross-page observers -- ``enter()`` is the sole source of
  first-render truth (``docs/coding-standards.md`` rule 4).

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
from OpenLIFULib.util import mark_session_dirty

from OpenLIFUApp.dialogs.session_dialogs import (
    ContinuePlanningSessionDialog,
    ContinueSonicationSessionDialog,
    NewPlanningSessionDialog,
    NewSonicationSessionDialog,
    SubjectPickerDialog,
)
from OpenLIFUApp.logic.session_actions import (
    build_planning_session,
    build_sonication_session,
    navigate_to_host_page,
    open_planning_session_from_disk,
    open_planning_session_into_app,
    open_sonication_session_from_disk,
    open_sonication_session_into_app,
    prompt_save_before_replacing_loaded_session,
)


class OpenLIFUHomeWidget(ScriptedLoadableModuleWidget):
    """Landing page for the OpenLIFU host module.

    Owns three status labels (database, loaded planning session, loaded
    sonication session), four launch buttons, and a Data Manager link.
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
        # Set True once ``enter()`` has tried the QSettings-backed
        # auto-connect at least once in this Slicer session. Re-entering
        # Home should not repeatedly re-attempt an auto-connect if the
        # user explicitly disconnected the database from the Data
        # Manager. See :meth:`try_auto_connect_if_needed` for details.
        self.has_attempted_auto_connect = False

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
        outer.addWidget(self.build_launch_group())
        outer.addLayout(self.build_admin_row())
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

        The first time this page becomes visible in a Slicer session,
        it also tries an auto-connect to the last-used database (see
        SlicerOpenLIFU#635).
        """
        self.is_entered = True
        self.try_auto_connect_if_needed()
        self.refresh_status()
        self.refresh_launch_buttons()

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

    def build_launch_group(self) -> qt.QGroupBox:
        """Build the group of four large launch buttons.

        Grid layout: two columns (New / Continue) x two rows
        (Planning / Sonication). Each button carries a tooltip
        describing the action; the buttons themselves are the primary
        entry points into the clinical workflow.
        """
        group = qt.QGroupBox("Launch a session")
        grid = qt.QGridLayout(group)
        grid.setSpacing(10)

        self.new_planning_button = self.make_big_button(
            "New Planning Session",
            tooltip="Create a new PlanningSession on the current database.",
            handler=self.on_new_planning_button_clicked,
        )
        self.continue_planning_button = self.make_big_button(
            "Continue Planning Session",
            tooltip="Open an existing PlanningSession from the current database.",
            handler=self.on_continue_planning_button_clicked,
        )
        self.new_sonication_button = self.make_big_button(
            "New Sonication Session",
            tooltip=(
                "Create a new SonicationSession against a finalized Plan."
            ),
            handler=self.on_new_sonication_button_clicked,
        )
        self.continue_sonication_button = self.make_big_button(
            "Continue Sonication Session",
            tooltip="Open an existing SonicationSession from the current database.",
            handler=self.on_continue_sonication_button_clicked,
        )

        # Row 0: Planning; Row 1: Sonication. Column 0: New; Column 1: Continue.
        grid.addWidget(self.new_planning_button,        0, 0)
        grid.addWidget(self.continue_planning_button,   0, 1)
        grid.addWidget(self.new_sonication_button,      1, 0)
        grid.addWidget(self.continue_sonication_button, 1, 1)

        # Hint under the grid explains disabled state when there's no
        # database. Updated by :meth:`refresh_launch_buttons`.
        self.launch_hint_label = qt.QLabel("")
        self.launch_hint_label.setWordWrap(True)
        self.launch_hint_label.setStyleSheet("color: #888;")
        grid.addWidget(self.launch_hint_label, 2, 0, 1, 2)

        return group

    def make_big_button(self, label: str, *, tooltip: str, handler) -> qt.QPushButton:
        """Build one of the four large launch buttons.

        Factored out so all four share the same minimum height and
        emphasis; changing the launch-button styling should require
        editing exactly one place.
        """
        button = qt.QPushButton(label)
        button.setMinimumHeight(56)
        font = button.font
        font.setPointSize(font.pointSize() + 2)
        button.font = font
        button.setToolTip(tooltip)
        button.clicked.connect(handler)
        return button

    def build_admin_row(self) -> qt.QHBoxLayout:
        """Build the admin-actions row (currently: Data Manager only).

        Deliberately smaller and less emphasised than the launch
        buttons; the Data Manager is the admin panel, not the clinical
        entry point.
        """
        row = qt.QHBoxLayout()
        row.addStretch(1)
        self.data_manager_button = qt.QPushButton("Open Data Manager…")
        self.data_manager_button.setToolTip(
            "Browse and manage subjects, planning sessions, plans, "
            "sonication sessions, solutions, protocols, transducers, "
            "and users."
        )
        self.data_manager_button.clicked.connect(self.on_data_manager_button_clicked)
        row.addWidget(self.data_manager_button)
        return row

    # ------------------------------------------------------------------
    # Auto-connect
    # ------------------------------------------------------------------

    def try_auto_connect_if_needed(self) -> None:
        """Ask the host's ``DatabaseLogic`` to try a QSettings-backed
        auto-connect, if we haven't already tried in this session.

        Runs at most once per Slicer session -- if the user
        subsequently disconnects the database from the Data Manager
        and returns to Home, we do NOT try to re-open it. That
        respects an explicit user disconnect while still giving them
        a re-open of the last-used database on the next Slicer launch.

        Guarded so that repeated ``enter()`` calls during the same
        session (e.g. navigating away and back) don't repeatedly try
        the same failed path -- either the initial attempt succeeds
        and there's now a loaded database, or it failed and we log +
        stop trying.
        """
        if self.has_attempted_auto_connect:
            return
        self.has_attempted_auto_connect = True
        if get_cur_db() is not None:
            return  # already connected -- nothing to auto-connect
        try:
            database_logic = slicer.util.getModuleLogic(
                "OpenLIFU",
            ).database_logic
            database_logic.try_auto_connect()
        except Exception:  # noqa: BLE001
            logging.exception("Home: auto-connect attempt failed")

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

    def refresh_launch_buttons(self) -> None:
        """Toggle enablement + hint text on the four launch buttons.

        Disabled when no database is loaded (nothing to create /
        continue against). Enabled otherwise. Individual availability
        for "New Sonication Session" (which needs at least one Plan)
        is checked at click time rather than continuously here --
        walking the DB on every ``enter()`` to count plans would add
        latency for a rare disabled state.
        """
        database_loaded = get_cur_db() is not None
        for button in (
            self.new_planning_button,
            self.continue_planning_button,
            self.new_sonication_button,
            self.continue_sonication_button,
        ):
            button.enabled = database_loaded
        if database_loaded:
            self.launch_hint_label.text = ""
        else:
            self.launch_hint_label.text = (
                "Load a database from the Data Manager to enable "
                "session actions."
            )

    # ------------------------------------------------------------------
    # Signal handlers
    # ------------------------------------------------------------------

    def on_new_planning_button_clicked(self) -> None:
        """Build a fresh PlanningSession in memory, open it, navigate to overview.

        Memory-first (SlicerOpenLIFU#636): the new session is NOT
        written to disk here. It lives in the app state as a dirty
        loaded session; a later Save (from the Overview page's Save
        button, the host's Save toolbar, or Save-and-Exit) is what
        writes it. Discard at exit-time leaves disk untouched.

        Guard order:

        1. Prompt save / discard / cancel for any unsaved loaded
           session so we don't silently clobber work.
        2. Prompt for a subject.
        3. Open the New dialog for the session's fields.
        4. Refuse to accept a duplicate ID on disk before we build
           anything, so we never have to reason about "what if the
           save overwrites someone else's session with the same id".
        5. Build in memory, open into app, mark dirty, navigate.
        """
        database = get_cur_db()
        if database is None:
            self.show_info("Load a database first.")
            return
        if not prompt_save_before_replacing_loaded_session():
            return
        subject_id = self.prompt_for_subject(database, "New Planning Session")
        if not subject_id:
            return
        dialog = NewPlanningSessionDialog(database, subject_id, self.uiWidget)
        if dialog.exec_() != qt.QDialog.Accepted:
            return
        try:
            if dialog.session_id in database.get_planning_session_ids(subject_id):
                self.show_info(
                    f"A planning session with ID {dialog.session_id!r} "
                    f"already exists for subject {subject_id!r}."
                )
                return
            planning_session = build_planning_session(
                subject_id=subject_id,
                planning_session_id=dialog.session_id,
                name=dialog.session_name,
                volume_id=dialog.volume_id,
                protocol_id=dialog.protocol_id,
                transducer_id=dialog.transducer_id,
            )
            open_planning_session_into_app(planning_session)
            mark_session_dirty()
        except Exception as exc:  # noqa: BLE001
            self.show_error(f"Create failed: {exc}")
            return
        navigate_to_host_page("OpenLIFUPlanningSessionOverview")

    def on_continue_planning_button_clicked(self) -> None:
        """Open a picker for an existing PlanningSession, load, navigate to overview.

        Loaded copy is not marked dirty -- disk is authoritative for
        this path (SlicerOpenLIFU#636).
        """
        database = get_cur_db()
        if database is None:
            self.show_info("Load a database first.")
            return
        if not prompt_save_before_replacing_loaded_session():
            return
        dialog = ContinuePlanningSessionDialog(database, self.uiWidget)
        if dialog.exec_() != qt.QDialog.Accepted:
            return
        try:
            open_planning_session_from_disk(dialog.subject_id, dialog.session_id)
        except Exception as exc:  # noqa: BLE001
            self.show_error(f"Load failed: {exc}")
            return
        navigate_to_host_page("OpenLIFUPlanningSessionOverview")

    def on_new_sonication_button_clicked(self) -> None:
        """Build a fresh SonicationSession in memory, open it, navigate to overview.

        Memory-first (SlicerOpenLIFU#636), same shape as
        :meth:`on_new_planning_button_clicked`. The referenced Plan
        must already exist on disk -- Plans are always disk-first --
        so we surface a "finalize a plan first" info dialog when the
        subject has none.
        """
        database = get_cur_db()
        if database is None:
            self.show_info("Load a database first.")
            return
        if not prompt_save_before_replacing_loaded_session():
            return
        subject_id = self.prompt_for_subject(database, "New Sonication Session")
        if not subject_id:
            return
        try:
            plan_ids = list(database.get_plan_ids(subject_id))
        except Exception as exc:  # noqa: BLE001
            self.show_error(f"Could not list plans: {exc}")
            return
        if not plan_ids:
            self.show_info(
                "This subject has no plans. Finalize a plan first."
            )
            return
        dialog = NewSonicationSessionDialog(subject_id, plan_ids, self.uiWidget)
        if dialog.exec_() != qt.QDialog.Accepted:
            return
        try:
            if dialog.session_id in database.get_sonication_session_ids(subject_id):
                self.show_info(
                    f"A sonication session with ID {dialog.session_id!r} "
                    f"already exists for subject {subject_id!r}."
                )
                return
            sonication_session = build_sonication_session(
                subject_id=subject_id,
                sonication_session_id=dialog.session_id,
                name=dialog.session_name,
                plan_id=dialog.plan_id,
            )
            open_sonication_session_into_app(sonication_session)
            mark_session_dirty()
        except Exception as exc:  # noqa: BLE001
            self.show_error(f"Create failed: {exc}")
            return
        navigate_to_host_page("OpenLIFUSonicationSessionOverview")

    def on_continue_sonication_button_clicked(self) -> None:
        """Open a picker for an existing SonicationSession, load, navigate to overview."""
        database = get_cur_db()
        if database is None:
            self.show_info("Load a database first.")
            return
        if not prompt_save_before_replacing_loaded_session():
            return
        dialog = ContinueSonicationSessionDialog(database, self.uiWidget)
        if dialog.exec_() != qt.QDialog.Accepted:
            return
        try:
            open_sonication_session_from_disk(dialog.subject_id, dialog.session_id)
        except Exception as exc:  # noqa: BLE001
            self.show_error(f"Load failed: {exc}")
            return
        navigate_to_host_page("OpenLIFUSonicationSessionOverview")

    def on_data_manager_button_clicked(self) -> None:
        """Ask the host module to swap the visible page to the Data Manager."""
        navigate_to_host_page("OpenLIFUDataManager")

    # ------------------------------------------------------------------
    # Small helpers
    # ------------------------------------------------------------------

    def prompt_for_subject(
        self, database, title: str,
    ) -> Optional[str]:
        """Modal picker: choose a subject from the current database.

        Returns the chosen subject id, or ``None`` if the user
        cancels / there are no subjects.

        Uses a bespoke :class:`SubjectPickerDialog` rather than
        ``qt.QInputDialog.getItem`` because PythonQt (Slicer's Qt
        binding) returns a bare ``str`` from ``getItem`` on some
        builds -- unlike PyQt5's documented ``(text, ok)`` tuple --
        which caused a ``ValueError: too many values to unpack``
        regression on the first click of a launch button.
        """
        dialog = SubjectPickerDialog(database, title, self.uiWidget)
        if dialog.exec_() != qt.QDialog.Accepted:
            return None
        return dialog.subject_id or None

    def show_info(self, text: str) -> None:
        """Show a modal info dialog scoped to OpenLIFU Home."""
        slicer.util.infoDisplay(text, windowTitle="OpenLIFU")

    def show_error(self, text: str) -> None:
        """Show a modal error dialog scoped to OpenLIFU Home."""
        slicer.util.errorDisplay(text, windowTitle="OpenLIFU")


class OpenLIFUHomeLogic(ScriptedLoadableModuleLogic):
    """Landing-page business logic.

    Currently empty. Kept in place so the host module's
    ``home_logic`` attribute always references a construct-able object,
    and so future landing-page behaviour (e.g. env-var-driven mode
    locks, sign-in-required gating) has a home.
    """
