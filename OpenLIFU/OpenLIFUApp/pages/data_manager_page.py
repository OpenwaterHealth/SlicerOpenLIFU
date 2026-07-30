"""Data Manager page (split-session v2).

Fresh rewrite for the session-split refactor (SlicerOpenLIFU#631,
SESSION_SPLIT_DESIGN.md).

Layout:

* Top row: database path + Load button.
* Tabs:

  * **Subject data**: subject picker + four collapsible sections
    (Planning Sessions / Plans / Sonication Sessions / Solutions),
    plus a Loaded status card with Save / Close.
  * **Protocols**: DB-level protocols table.
  * **Transducers**: DB-level transducers table.
  * **Users**: DB-level users table.

Every table uses fixed-by-default resizable column widths and puts the
full cell text into each item's tooltip so long strings can be
inspected without dragging the column.

Responsibilities: CRUD surface for the split-session model against the
subject-scoped Database API. No cross-page observers; ``enter()`` is
the sole source of first-render truth (see
``docs/coding-standards.md`` rule 4 and ``docs/architecture.md``
section 5).

See ``docs/coding-standards.md`` for the naming convention: no
leading-underscore prefixes on public-behaviour methods, signal
handlers named ``on_<widget>_<event>``, docstrings that explain WHY.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, List, Optional, Sequence

import ctk
import qt
import slicer
from slicer.ScriptedLoadableModule import (
    ScriptedLoadableModuleLogic,
    ScriptedLoadableModuleTest,
    ScriptedLoadableModuleWidget,
)

from OpenLIFULib import (
    SlicerOpenLIFUPlan,
    SlicerOpenLIFUPlanningSession,
    SlicerOpenLIFUSonicationSession,
    assign_openlifu_metadata_to_volume_node,
    get_app_state,
    get_cur_db,
)
from OpenLIFULib.parameter_node_utils import (
    SlicerOpenLIFUPlanningSessionWrapper,
    SlicerOpenLIFUPlanWrapper,
    SlicerOpenLIFUSonicationSessionWrapper,
)

if TYPE_CHECKING:
    import openlifu.db


# Column-config type. ``(header, default_width_px)`` per column.
ColumnSpec = List[tuple]


class OpenLIFUDataManagerWidget(ScriptedLoadableModuleWidget):
    """Programmatic Qt UI for the split-session Data Manager.

    Owns four tab pages (Subject data / Protocols / Transducers / Users)
    and drives all its state from ``enter()``. No cross-page observers.
    Every action on the page is a button handler that calls a
    corresponding ``OpenLIFUDataManagerLogic`` method and then a local
    refresh.
    """

    def __init__(self, parent=None):
        ScriptedLoadableModuleWidget.__init__(self, parent)
        # Every embedded page borrows the host module's resource path.
        self.moduleName = "OpenLIFU"
        self.logic: Optional[OpenLIFUDataManagerLogic] = None
        # Set True in ``enter()`` and False in ``exit()``. Consulted by
        # signal handlers (e.g. subject-combo change) so they can skip
        # work when the page is not visible (Qt fires signal handlers
        # during UI construction too).
        self.is_entered = False

    # ------------------------------------------------------------------
    # Slicer widget lifecycle
    # ------------------------------------------------------------------

    def setup(self) -> None:
        """Build the programmatic Qt UI once, when the widget is created."""
        ScriptedLoadableModuleWidget.setup(self)
        self.logic = OpenLIFUDataManagerLogic()

        top = qt.QWidget()
        top_layout = qt.QVBoxLayout(top)
        top_layout.setContentsMargins(8, 8, 8, 8)
        top_layout.setSpacing(8)

        top_layout.addLayout(self.build_database_row())

        self.tabs = qt.QTabWidget()
        self.tabs.addTab(self.build_subject_data_tab(), "Subject Data")
        self.tabs.addTab(self.build_protocols_tab(), "Protocols")
        self.tabs.addTab(self.build_transducers_tab(), "Transducers")
        self.tabs.addTab(self.build_users_tab(), "Users")
        top_layout.addWidget(self.tabs, 1)

        self.layout.addWidget(top)
        self.uiWidget = top

    def enter(self) -> None:
        """Rebuild every visible piece of state from live sources.

        Sole source of first-render truth. Called every time the user
        navigates to this page. Signal handlers only mutate local state
        + refresh; nothing else drives this page's UI.
        """
        self.is_entered = True
        self.refresh_database_status()
        self.refresh_subject_combo()
        self.refresh_subject_scoped_lists()
        self.refresh_loaded_labels()
        self.refresh_database_scoped_tables()

    def exit(self) -> None:
        """Mark the page as no longer visible without tearing down state."""
        self.is_entered = False

    def cleanup(self) -> None:
        """Slicer widget teardown hook; nothing to clean up here."""

    # ==================================================================
    # UI builders
    # ==================================================================

    def build_database_row(self) -> qt.QHBoxLayout:
        """Build the row at the top of the page showing the current
        database path plus a Load button."""
        row = qt.QHBoxLayout()
        row.addWidget(qt.QLabel("Database:"))
        self.db_path_label = qt.QLabel("(none loaded)")
        self.db_path_label.setStyleSheet("color: #666;")
        row.addWidget(self.db_path_label, 1)
        self.load_db_button = qt.QPushButton("Load…")
        self.load_db_button.setToolTip(
            "Open an openlifu file-based database from a local folder."
        )
        self.load_db_button.clicked.connect(self.on_load_db_button_clicked)
        row.addWidget(self.load_db_button)
        return row

    # -- Subject Data tab ----------------------------------------------

    def build_subject_data_tab(self) -> qt.QWidget:
        """Build the Subject Data tab, which contains the subject picker
        and four collapsible sections + the Loaded status card."""
        tab = qt.QWidget()
        outer = qt.QVBoxLayout(tab)
        outer.setContentsMargins(4, 8, 4, 4)
        outer.setSpacing(6)

        # subject picker row
        row = qt.QHBoxLayout()
        row.addWidget(qt.QLabel("Subject:"))
        self.subject_combo = qt.QComboBox()
        self.subject_combo.setMinimumWidth(240)
        self.subject_combo.currentIndexChanged.connect(self.on_subject_combo_changed)
        row.addWidget(self.subject_combo, 1)
        self.refresh_button = qt.QPushButton("Refresh")
        self.refresh_button.setToolTip("Rescan the database for this subject.")
        self.refresh_button.clicked.connect(self.on_refresh_button_clicked)
        row.addWidget(self.refresh_button)
        outer.addLayout(row)

        # -- Planning sessions section
        self.planning_table = make_fixed_width_table([
            ("ID", 200),
            ("Name", 260),
            ("# Targets", 90),
        ])
        outer.addWidget(self.make_collapsible_section(
            title="Planning Sessions",
            hint=("Mutable working documents. Owns targets, virtual-fit "
                  "results, and pre-solutions."),
            table=self.planning_table,
            actions=[
                ("New…",   self.on_new_planning_button_clicked),
                ("Load",   self.on_load_planning_button_clicked),
                ("Delete", self.on_delete_planning_button_clicked),
            ],
        ))

        # -- Plans section
        self.plan_table = make_fixed_width_table([
            ("ID", 200),
            ("Name", 220),
            ("Target", 140),
            ("Parent Planning Session", 200),
        ])
        outer.addWidget(self.make_collapsible_section(
            title="Plans",
            hint=("Immutable finalized treatment plans. Produced by "
                  "finalizing a Planning Session."),
            table=self.plan_table,
            actions=[
                ("Delete", self.on_delete_plan_button_clicked),
            ],
        ))

        # -- Sonication sessions section
        self.sonication_table = make_fixed_width_table([
            ("ID", 200),
            ("Name", 220),
            ("Plan", 160),
            ("Solution", 160),
        ])
        outer.addWidget(self.make_collapsible_section(
            title="Sonication Sessions",
            hint=("At-treatment-time sessions. References a Plan for "
                  "target and pose; owns the photoscan registrations, "
                  "transducer tracking, and the final Solution."),
            table=self.sonication_table,
            actions=[
                ("New…",   self.on_new_sonication_button_clicked),
                ("Load",   self.on_load_sonication_button_clicked),
                ("Delete", self.on_delete_sonication_button_clicked),
            ],
        ))

        # -- Solutions section (subject-scoped)
        self.solution_table = make_fixed_width_table([
            ("ID", 200),
            ("Target", 140),
            ("Source", 100),
            ("Approved", 90),
            ("Computed at", 180),
        ])
        outer.addWidget(self.make_collapsible_section(
            title="Solutions",
            hint=("Computed sonication solutions. Subject-scoped; a single "
                  "Solution can be referenced by a PlanningSession's "
                  "pre-solutions and by a SonicationSession's final "
                  "solution."),
            table=self.solution_table,
            actions=[
                ("Delete", self.on_delete_solution_button_clicked),
            ],
        ))

        outer.addWidget(self.build_loaded_status_group())
        outer.addStretch(1)
        return tab

    def build_loaded_status_group(self) -> qt.QGroupBox:
        """Build the "Loaded" card at the bottom of the Subject Data tab
        showing which planning / sonication session is currently loaded
        into the app state, with Save and Close actions."""
        group = qt.QGroupBox("Loaded")
        layout = qt.QVBoxLayout(group)
        self.loaded_planning_label = qt.QLabel("Planning session: —")
        self.loaded_sonication_label = qt.QLabel("Sonication session: —")
        layout.addWidget(self.loaded_planning_label)
        layout.addWidget(self.loaded_sonication_label)

        row = qt.QHBoxLayout()
        self.save_button = qt.QPushButton("Save")
        self.save_button.setToolTip(
            "Write the loaded PlanningSession or SonicationSession JSON to disk."
        )
        self.save_button.clicked.connect(self.on_save_button_clicked)
        self.close_button = qt.QPushButton("Close")
        self.close_button.setToolTip("Unload the currently-loaded session.")
        self.close_button.clicked.connect(self.on_close_button_clicked)
        row.addStretch(1)
        row.addWidget(self.save_button)
        row.addWidget(self.close_button)
        layout.addLayout(row)
        return group

    # -- Database-level tabs -------------------------------------------

    def build_protocols_tab(self) -> qt.QWidget:
        """Build the Protocols tab. Read-only listing for now."""
        tab = qt.QWidget()
        layout = qt.QVBoxLayout(tab)
        layout.setContentsMargins(4, 8, 4, 4)
        self.protocol_table = make_fixed_width_table([
            ("ID", 240),
            ("Name", 320),
        ])
        layout.addWidget(self.protocol_table, 1)
        return tab

    def build_transducers_tab(self) -> qt.QWidget:
        """Build the Transducers tab. Read-only listing for now."""
        tab = qt.QWidget()
        layout = qt.QVBoxLayout(tab)
        layout.setContentsMargins(4, 8, 4, 4)
        self.transducer_table = make_fixed_width_table([
            ("ID", 240),
            ("Name", 320),
            ("# Elements", 100),
        ])
        layout.addWidget(self.transducer_table, 1)
        return tab

    def build_users_tab(self) -> qt.QWidget:
        """Build the Users tab. Read-only listing for now."""
        tab = qt.QWidget()
        layout = qt.QVBoxLayout(tab)
        layout.setContentsMargins(4, 8, 4, 4)
        self.user_table = make_fixed_width_table([
            ("ID", 240),
            ("Name", 260),
            ("Roles", 200),
        ])
        layout.addWidget(self.user_table, 1)
        return tab

    def make_collapsible_section(
        self,
        *,
        title: str,
        hint: str,
        table: qt.QTableWidget,
        actions: List[tuple],
    ) -> ctk.ctkCollapsibleButton:
        """Build a ``ctkCollapsibleButton`` around a table + action row.

        Args:
            title: Displayed as the section heading.
            hint: Multi-line hint shown at the top of the section body.
            table: The already-built table widget to embed.
            actions: List of ``(button_label, handler_callable)`` tuples.
        """
        section = ctk.ctkCollapsibleButton()
        section.text = title
        section.collapsed = False
        inner = qt.QVBoxLayout(section)

        hint_label = qt.QLabel(hint)
        hint_label.setWordWrap(True)
        hint_label.setStyleSheet("color: #666;")
        inner.addWidget(hint_label)

        inner.addWidget(table)

        row = qt.QHBoxLayout()
        row.addStretch(1)
        for label, slot in actions:
            btn = qt.QPushButton(label)
            btn.clicked.connect(slot)
            row.addWidget(btn)
        inner.addLayout(row)
        return section

    # ==================================================================
    # Refresh
    # ==================================================================

    def refresh_database_status(self) -> None:
        """Update the database status label at the top of the page."""
        database = get_cur_db()
        if database is None:
            self.db_path_label.text = "(none loaded)"
            self.db_path_label.setStyleSheet("color: #666;")
        else:
            self.db_path_label.text = str(getattr(database, "path", "loaded"))
            self.db_path_label.setStyleSheet("color: #060;")

    def refresh_subject_combo(self) -> None:
        """Repopulate the subject combo box from the loaded database.

        Preserves the current selection when possible so navigating away
        and back does not reset the user's context.
        """
        database = get_cur_db()
        previous_selection = self.subject_combo.currentText
        with SignalBlocker(self.subject_combo):
            self.subject_combo.clear()
            if database is None:
                self.subject_combo.addItem("(no database)")
                self.subject_combo.setEnabled(False)
                return
            self.subject_combo.setEnabled(True)
            subject_ids = database.get_subject_ids()
            for subject_id in subject_ids:
                self.subject_combo.addItem(subject_id)
            if previous_selection in subject_ids:
                self.subject_combo.setCurrentText(previous_selection)

    def refresh_subject_scoped_lists(self) -> None:
        """Repopulate the four subject-scoped tables (planning sessions,
        plans, sonication sessions, solutions) from the current subject.
        """
        subject_id = self.current_subject_id()
        database = get_cur_db()
        if database is None or not subject_id:
            fill_table_with_tooltips(self.planning_table, [])
            fill_table_with_tooltips(self.plan_table, [])
            fill_table_with_tooltips(self.sonication_table, [])
            fill_table_with_tooltips(self.solution_table, [])
            return

        # -- Planning sessions
        planning_rows: List[tuple] = []
        for ps_id in database.get_planning_session_ids(subject_id):
            try:
                planning_session = database.load_planning_session(subject_id, ps_id)
                planning_rows.append((
                    planning_session.id,
                    planning_session.name or "",
                    str(len(planning_session.targets)),
                ))
            except Exception as exc:  # noqa: BLE001
                planning_rows.append((ps_id, f"<error: {exc}>", ""))
        fill_table_with_tooltips(self.planning_table, planning_rows)

        # -- Plans
        plan_rows: List[tuple] = []
        for plan_id in database.get_plan_ids(subject_id):
            try:
                plan = database.load_plan(subject_id, plan_id)
                target_id = plan.target.id if plan.target is not None else ""
                plan_rows.append((
                    plan.id,
                    plan.name or "",
                    target_id,
                    plan.parent_planning_session_id or "",
                ))
            except Exception as exc:  # noqa: BLE001
                plan_rows.append((plan_id, f"<error: {exc}>", "", ""))
        fill_table_with_tooltips(self.plan_table, plan_rows)

        # -- Sonication sessions
        sonication_rows: List[tuple] = []
        for ss_id in database.get_sonication_session_ids(subject_id):
            try:
                sonication_session = database.load_sonication_session(subject_id, ss_id)
                solution_id = (
                    sonication_session.solution.solution_id
                    if sonication_session.solution else ""
                )
                sonication_rows.append((
                    sonication_session.id,
                    sonication_session.name or "",
                    sonication_session.plan_id or "",
                    solution_id,
                ))
            except Exception as exc:  # noqa: BLE001
                sonication_rows.append((ss_id, f"<error: {exc}>", "", ""))
        fill_table_with_tooltips(self.sonication_table, sonication_rows)

        # -- Solutions: subject-scoped. We fill in the provenance columns
        # from SolutionInfo refs found on any session/plan that references
        # each solution -- no need to load the heavy Solution binary files.
        solution_ids = database.get_subject_solution_ids(subject_id)
        info_by_id = collect_solution_infos_from_sessions(database, subject_id)
        solution_rows: List[tuple] = []
        for solution_id in solution_ids:
            info = info_by_id.get(solution_id)
            if info is not None:
                solution_rows.append((
                    solution_id,
                    info.target_id or "",
                    info.transducer_transform_source or "",
                    "yes" if info.approved else "no",
                    info.computed_at.isoformat() if info.computed_at else "",
                ))
            else:
                solution_rows.append((solution_id, "", "", "", ""))
        fill_table_with_tooltips(self.solution_table, solution_rows)

    def refresh_loaded_labels(self) -> None:
        """Rebuild the "Loaded" card + toggle Save / Close enablement."""
        state = get_app_state()
        planning_session = state.loaded_planning_session
        sonication_session = state.loaded_sonication_session
        if planning_session is not None:
            self.loaded_planning_label.text = (
                f"Planning session: {planning_session.get_planning_session_id()} "
                f"(subject={planning_session.get_subject_id()})"
            )
        else:
            self.loaded_planning_label.text = "Planning session: —"
        if sonication_session is not None:
            self.loaded_sonication_label.text = (
                f"Sonication session: {sonication_session.get_sonication_session_id()} "
                f"(subject={sonication_session.get_subject_id()}, "
                f"plan={sonication_session.get_plan_id()})"
            )
        else:
            self.loaded_sonication_label.text = "Sonication session: —"

        has_session_loaded = (planning_session is not None) or (sonication_session is not None)
        self.save_button.enabled = has_session_loaded
        self.close_button.enabled = has_session_loaded

    def refresh_database_scoped_tables(self) -> None:
        """Repopulate the Protocols / Transducers / Users tabs."""
        database = get_cur_db()
        if database is None:
            fill_table_with_tooltips(self.protocol_table, [])
            fill_table_with_tooltips(self.transducer_table, [])
            fill_table_with_tooltips(self.user_table, [])
            return

        protocol_rows: List[tuple] = []
        for protocol_id in database.get_protocol_ids():
            try:
                protocol = database.load_protocol(protocol_id)
                protocol_rows.append((protocol.id, protocol.name or ""))
            except Exception:  # noqa: BLE001
                protocol_rows.append((protocol_id, ""))
        fill_table_with_tooltips(self.protocol_table, protocol_rows)

        transducer_rows: List[tuple] = []
        for transducer_id in database.get_transducer_ids():
            try:
                transducer = database.load_transducer(transducer_id)
                element_count = (
                    len(transducer.elements)
                    if getattr(transducer, "elements", None) else ""
                )
                transducer_rows.append((
                    transducer.id, transducer.name or "", str(element_count),
                ))
            except Exception:  # noqa: BLE001
                transducer_rows.append((transducer_id, "", ""))
        fill_table_with_tooltips(self.transducer_table, transducer_rows)

        user_rows: List[tuple] = []
        for user_id in database.get_user_ids():
            try:
                user = database.load_user(user_id)
                roles = getattr(user, "roles", None) or []
                user_rows.append((
                    user.id, user.name or "",
                    ", ".join(str(r) for r in roles),
                ))
            except Exception:  # noqa: BLE001
                user_rows.append((user_id, "", ""))
        fill_table_with_tooltips(self.user_table, user_rows)

    # ==================================================================
    # Selection helpers
    # ==================================================================

    def current_subject_id(self) -> Optional[str]:
        """Return the currently-selected subject id, or ``None`` if no
        database is loaded or no subject is selected."""
        if not self.subject_combo.isEnabled():
            return None
        text = self.subject_combo.currentText
        return text or None

    def selected_id_in_table(self, table: qt.QTableWidget) -> Optional[str]:
        """Return the id (column 0) of the currently-selected row in
        ``table``, or ``None`` if nothing is selected."""
        selected_rows = table.selectionModel().selectedRows()
        if not selected_rows:
            return None
        item = table.item(selected_rows[0].row(), 0)
        return item.text() if item is not None else None

    # ==================================================================
    # Signal handlers
    # ==================================================================

    def on_subject_combo_changed(self, *_args) -> None:
        """Refresh the subject-scoped tables when the user picks a different subject.

        Guarded by ``is_entered`` because Qt fires ``currentIndexChanged``
        during initial construction of the combo box too.
        """
        if not self.is_entered:
            return
        self.refresh_subject_scoped_lists()

    def on_refresh_button_clicked(self) -> None:
        """Force-refresh every visible piece of state.

        Same as ``enter()`` -- kept as an explicit button so the user can
        rescan the database after making changes on disk from outside
        this Slicer session.
        """
        self.refresh_database_status()
        self.refresh_subject_combo()
        self.refresh_subject_scoped_lists()
        self.refresh_loaded_labels()
        self.refresh_database_scoped_tables()

    def on_load_db_button_clicked(self) -> None:
        """Prompt for a database directory and load it into the host DatabaseLogic."""
        path = qt.QFileDialog.getExistingDirectory(
            self.parent, "Select openlifu database root", "",
        )
        if not path:
            return
        try:
            slicer.util.getModuleLogic("OpenLIFU").database_logic.load_database(path)
        except Exception as exc:  # noqa: BLE001
            show_error_dialog(f"Failed to load database:\n{exc}")
            return
        self.refresh_database_status()
        self.refresh_subject_combo()
        self.refresh_subject_scoped_lists()
        self.refresh_loaded_labels()
        self.refresh_database_scoped_tables()

    # -- planning session actions --------------------------------------

    def on_new_planning_button_clicked(self) -> None:
        """Prompt for a new PlanningSession's parameters and create it."""
        subject_id = self.current_subject_id()
        if not subject_id:
            show_info_dialog("Select a subject first.")
            return
        database = get_cur_db()
        dialog = NewPlanningSessionDialog(database, subject_id, self.parent)
        if dialog.exec_() != qt.QDialog.Accepted:
            return
        try:
            self.logic.create_planning_session(
                subject_id=subject_id,
                planning_session_id=dialog.session_id,
                name=dialog.session_name,
                volume_id=dialog.volume_id,
                protocol_id=dialog.protocol_id,
                transducer_id=dialog.transducer_id,
            )
        except Exception as exc:  # noqa: BLE001
            show_error_dialog(f"Create failed: {exc}")
            return
        self.refresh_subject_scoped_lists()

    def on_load_planning_button_clicked(self) -> None:
        """Load the currently-selected PlanningSession into the app state."""
        subject_id = self.current_subject_id()
        planning_session_id = self.selected_id_in_table(self.planning_table)
        if not subject_id or not planning_session_id:
            show_info_dialog("Select a planning session first.")
            return
        try:
            self.logic.load_planning_session(subject_id, planning_session_id)
        except Exception as exc:  # noqa: BLE001
            show_error_dialog(f"Load failed: {exc}")
            return
        self.refresh_loaded_labels()

    def on_delete_planning_button_clicked(self) -> None:
        """Delete the currently-selected PlanningSession from the database.

        Prompts for confirmation. Plans finalized from the deleted
        session are NOT touched.
        """
        subject_id = self.current_subject_id()
        planning_session_id = self.selected_id_in_table(self.planning_table)
        if not subject_id or not planning_session_id:
            show_info_dialog("Select a planning session first.")
            return
        if not confirm_action(
            f"Delete planning session {planning_session_id!r}?\n"
            "Plans this session finalized will remain."
        ):
            return
        try:
            self.logic.delete_planning_session(subject_id, planning_session_id)
        except Exception as exc:  # noqa: BLE001
            show_error_dialog(f"Delete failed: {exc}")
            return
        self.refresh_subject_scoped_lists()

    # -- plan actions --------------------------------------------------

    def on_delete_plan_button_clicked(self) -> None:
        """Delete the currently-selected Plan from the database.

        Prompts for confirmation. SonicationSessions that reference the
        deleted Plan will become dangling.
        """
        subject_id = self.current_subject_id()
        plan_id = self.selected_id_in_table(self.plan_table)
        if not subject_id or not plan_id:
            show_info_dialog("Select a plan first.")
            return
        if not confirm_action(
            f"Delete plan {plan_id!r}?\n"
            "Sonication sessions that reference this plan will "
            "become dangling."
        ):
            return
        try:
            self.logic.delete_plan(subject_id, plan_id)
        except Exception as exc:  # noqa: BLE001
            show_error_dialog(f"Delete failed: {exc}")
            return
        self.refresh_subject_scoped_lists()

    # -- sonication session actions ------------------------------------

    def on_new_sonication_button_clicked(self) -> None:
        """Prompt for a new SonicationSession + its referenced Plan and create it."""
        subject_id = self.current_subject_id()
        if not subject_id:
            show_info_dialog("Select a subject first.")
            return
        database = get_cur_db()
        plan_ids = database.get_plan_ids(subject_id)
        if not plan_ids:
            show_info_dialog("This subject has no plans. Finalize a plan first.")
            return
        dialog = NewSonicationSessionDialog(subject_id, plan_ids, self.parent)
        if dialog.exec_() != qt.QDialog.Accepted:
            return
        try:
            self.logic.create_sonication_session(
                subject_id=subject_id,
                sonication_session_id=dialog.session_id,
                name=dialog.session_name,
                plan_id=dialog.plan_id,
            )
        except Exception as exc:  # noqa: BLE001
            show_error_dialog(f"Create failed: {exc}")
            return
        self.refresh_subject_scoped_lists()

    def on_load_sonication_button_clicked(self) -> None:
        """Load the currently-selected SonicationSession + its Plan into the app state."""
        subject_id = self.current_subject_id()
        sonication_session_id = self.selected_id_in_table(self.sonication_table)
        if not subject_id or not sonication_session_id:
            show_info_dialog("Select a sonication session first.")
            return
        try:
            self.logic.load_sonication_session(subject_id, sonication_session_id)
        except Exception as exc:  # noqa: BLE001
            show_error_dialog(f"Load failed: {exc}")
            return
        self.refresh_loaded_labels()

    def on_delete_sonication_button_clicked(self) -> None:
        """Delete the currently-selected SonicationSession from the database.

        Prompts for confirmation. The referenced Plan and any subject-
        scoped photoscans / solutions are NOT touched.
        """
        subject_id = self.current_subject_id()
        sonication_session_id = self.selected_id_in_table(self.sonication_table)
        if not subject_id or not sonication_session_id:
            show_info_dialog("Select a sonication session first.")
            return
        if not confirm_action(
            f"Delete sonication session {sonication_session_id!r}?\n"
            "The referenced Plan and any subject-scoped photoscans "
            "or solutions will remain."
        ):
            return
        try:
            self.logic.delete_sonication_session(subject_id, sonication_session_id)
        except Exception as exc:  # noqa: BLE001
            show_error_dialog(f"Delete failed: {exc}")
            return
        self.refresh_subject_scoped_lists()

    # -- solution actions ----------------------------------------------

    def on_delete_solution_button_clicked(self) -> None:
        """Delete the currently-selected subject-scoped Solution from the database.

        Sessions and plans that reference the deleted solution keep
        their ``SolutionInfo`` entries but the on-disk files are gone.
        """
        subject_id = self.current_subject_id()
        solution_id = self.selected_id_in_table(self.solution_table)
        if not subject_id or not solution_id:
            show_info_dialog("Select a solution first.")
            return
        if not confirm_action(
            f"Delete solution {solution_id!r}?\n"
            "PlanningSessions / Plans / SonicationSessions that "
            "reference this solution will keep their SolutionInfo "
            "entries but the on-disk files will be gone."
        ):
            return
        try:
            require_current_database().delete_solution_at_subject_scope(
                subject_id, solution_id,
            )
        except Exception as exc:  # noqa: BLE001
            show_error_dialog(f"Delete failed: {exc}")
            return
        self.refresh_subject_scoped_lists()

    # -- save / close --------------------------------------------------

    def on_save_button_clicked(self) -> None:
        """Persist the currently-loaded PlanningSession / SonicationSession to disk."""
        try:
            self.logic.save_loaded_session()
        except Exception as exc:  # noqa: BLE001
            show_error_dialog(f"Save failed: {exc}")
            return
        self.refresh_subject_scoped_lists()

    def on_close_button_clicked(self) -> None:
        """Unload the currently-loaded session, prompting to confirm."""
        state = get_app_state()
        if state.loaded_planning_session is None and state.loaded_sonication_session is None:
            return
        if not confirm_action("Close the loaded session?"):
            return
        self.logic.close_loaded_sessions()
        self.refresh_loaded_labels()


# ---------------------------------------------------------------------------
# Logic
# ---------------------------------------------------------------------------

class OpenLIFUDataManagerLogic(ScriptedLoadableModuleLogic):
    """Business logic for the split-session Data Manager.

    Every method takes explicit inputs (no reaching into signals or GUIs)
    and touches disk / app state directly. Verification tests import
    this class and drive it without the widget.
    """

    def __init__(self):
        ScriptedLoadableModuleLogic.__init__(self)

    # -- creation ------------------------------------------------------

    def create_planning_session(
        self,
        *,
        subject_id: str,
        planning_session_id: str,
        name: Optional[str],
        volume_id: str,
        protocol_id: str,
        transducer_id: str,
    ) -> None:
        """Create and write a new (empty) PlanningSession."""
        import openlifu.db
        database = require_current_database()
        session = openlifu.db.PlanningSession(
            id=planning_session_id,
            name=name or planning_session_id,
            subject_id=subject_id,
            volume_id=volume_id,
            protocol_id=protocol_id,
            transducer_id=transducer_id,
        )
        database.write_planning_session(subject_id, session)
        logging.info(
            "Created PlanningSession %s for subject %s",
            planning_session_id, subject_id,
        )

    def create_sonication_session(
        self,
        *,
        subject_id: str,
        sonication_session_id: str,
        name: Optional[str],
        plan_id: str,
    ) -> None:
        """Create and write a new (empty) SonicationSession against a Plan."""
        import openlifu.db
        database = require_current_database()
        if plan_id not in database.get_plan_ids(subject_id):
            raise ValueError(
                f"Plan {plan_id!r} does not exist for subject {subject_id!r}."
            )
        session = openlifu.db.SonicationSession(
            id=sonication_session_id,
            name=name or sonication_session_id,
            subject_id=subject_id,
            plan_id=plan_id,
        )
        database.write_sonication_session(subject_id, session)
        logging.info(
            "Created SonicationSession %s for subject %s (plan=%s)",
            sonication_session_id, subject_id, plan_id,
        )

    # -- loading -------------------------------------------------------

    def load_planning_session(self, subject_id: str, planning_session_id: str) -> None:
        """Load a PlanningSession into the app state.

        Unloads any currently-loaded planning or sonication session first
        so the app is in a single-loaded-session state.
        """
        database = require_current_database()
        planning_session = database.load_planning_session(subject_id, planning_session_id)
        self.close_loaded_sessions()

        volume_node = load_volume_node_from_database(
            database, subject_id, planning_session.volume_id,
        )
        target_nodes = [
            create_target_fiducial_node(target) for target in planning_session.targets
        ]

        state = get_app_state()
        state.loaded_planning_session = SlicerOpenLIFUPlanningSession(
            session=SlicerOpenLIFUPlanningSessionWrapper(planning_session=planning_session),
            volume_node=volume_node,
            target_nodes=target_nodes,
        )
        logging.info(
            "Loaded PlanningSession %s for subject %s",
            planning_session_id, subject_id,
        )

    def load_sonication_session(self, subject_id: str, sonication_session_id: str) -> None:
        """Load a SonicationSession + its referenced Plan into the app state."""
        database = require_current_database()
        sonication_session = database.load_sonication_session(subject_id, sonication_session_id)
        if sonication_session.plan_id is None:
            raise ValueError(
                f"SonicationSession {sonication_session_id!r} has no plan_id."
            )
        plan = database.load_plan(subject_id, sonication_session.plan_id)
        self.close_loaded_sessions()

        volume_node = load_volume_node_from_database(
            database, subject_id, plan.volume_id,
        )

        state = get_app_state()
        state.loaded_sonication_session = SlicerOpenLIFUSonicationSession(
            session=SlicerOpenLIFUSonicationSessionWrapper(sonication_session=sonication_session),
            plan=SlicerOpenLIFUPlanWrapper(plan=plan),
            volume_node=volume_node,
        )
        logging.info(
            "Loaded SonicationSession %s for subject %s (plan=%s)",
            sonication_session_id, subject_id, plan.id,
        )

    # -- deletion ------------------------------------------------------

    def delete_planning_session(self, subject_id: str, planning_session_id: str) -> None:
        """Delete a PlanningSession, first closing it if currently loaded."""
        database = require_current_database()
        state = get_app_state()
        loaded_session = state.loaded_planning_session
        if loaded_session is not None and loaded_session.get_planning_session_id() == planning_session_id:
            self.close_loaded_sessions()
        database.delete_planning_session(subject_id, planning_session_id)

    def delete_plan(self, subject_id: str, plan_id: str) -> None:
        """Delete a Plan, first closing any SonicationSession that references it."""
        database = require_current_database()
        state = get_app_state()
        loaded_session = state.loaded_sonication_session
        if loaded_session is not None and loaded_session.get_plan_id() == plan_id:
            self.close_loaded_sessions()
        database.delete_plan(subject_id, plan_id)

    def delete_sonication_session(self, subject_id: str, sonication_session_id: str) -> None:
        """Delete a SonicationSession, first closing it if currently loaded."""
        database = require_current_database()
        state = get_app_state()
        loaded_session = state.loaded_sonication_session
        if loaded_session is not None and loaded_session.get_sonication_session_id() == sonication_session_id:
            self.close_loaded_sessions()
        database.delete_sonication_session(subject_id, sonication_session_id)

    # -- save / close --------------------------------------------------

    def save_loaded_session(self) -> None:
        """Persist the currently-loaded PlanningSession / SonicationSession
        JSONs to disk.

        Raises ``RuntimeError`` if nothing is loaded.
        """
        database = require_current_database()
        state = get_app_state()
        planning_session = state.loaded_planning_session
        sonication_session = state.loaded_sonication_session
        wrote_something = False
        if planning_session is not None:
            database.write_planning_session(
                planning_session.get_subject_id(),
                planning_session.session.planning_session,
                on_conflict="overwrite",
            )
            wrote_something = True
        if sonication_session is not None:
            database.write_sonication_session(
                sonication_session.get_subject_id(),
                sonication_session.session.sonication_session,
                on_conflict="overwrite",
            )
            wrote_something = True
        if not wrote_something:
            raise RuntimeError("Nothing loaded to save.")

    def close_loaded_sessions(self) -> None:
        """Unload the loaded PlanningSession / SonicationSession from the app state.

        Tears down scene nodes each session owned (volume, targets). The
        app state never nulls a session on its own; this method is the
        single point at which unload happens.
        """
        state = get_app_state()
        planning_session = state.loaded_planning_session
        if planning_session is not None:
            for node in planning_session.get_target_nodes():
                try:
                    slicer.mrmlScene.RemoveNode(node)
                except Exception:  # noqa: BLE001
                    pass
            if planning_session.volume_node is not None:
                try:
                    slicer.mrmlScene.RemoveNode(planning_session.volume_node)
                except Exception:  # noqa: BLE001
                    pass
            state.loaded_planning_session = None
        sonication_session = state.loaded_sonication_session
        if sonication_session is not None:
            if sonication_session.volume_node is not None:
                try:
                    slicer.mrmlScene.RemoveNode(sonication_session.volume_node)
                except Exception:  # noqa: BLE001
                    pass
            state.loaded_sonication_session = None


# ---------------------------------------------------------------------------
# Dialogs
# ---------------------------------------------------------------------------

class NewPlanningSessionDialog(qt.QDialog):
    """Minimal New Planning Session dialog.

    Collects id, name, and combo-box selections for volume, protocol,
    and transducer. Used only from
    :meth:`OpenLIFUDataManagerWidget.on_new_planning_button_clicked`;
    if it grows a second call site it should move to a
    ``dialogs/`` package.
    """

    def __init__(self, database, subject_id: str, parent=None):
        super().__init__(parent)
        self.setWindowTitle("New Planning Session")
        self.session_id = ""
        self.session_name = ""
        self.volume_id = ""
        self.protocol_id = ""
        self.transducer_id = ""

        layout = qt.QFormLayout(self)
        self.id_edit = qt.QLineEdit()
        self.name_edit = qt.QLineEdit()
        self.volume_combo = qt.QComboBox()
        self.protocol_combo = qt.QComboBox()
        self.transducer_combo = qt.QComboBox()
        for combo, populate in (
            (self.volume_combo,     lambda: database.get_volume_ids(subject_id) if database else []),
            (self.protocol_combo,   lambda: database.get_protocol_ids() if database else []),
            (self.transducer_combo, lambda: database.get_transducer_ids() if database else []),
        ):
            try:
                combo.addItems(populate())
            except Exception:  # noqa: BLE001
                pass

        layout.addRow("ID:", self.id_edit)
        layout.addRow("Name:", self.name_edit)
        layout.addRow("Volume:", self.volume_combo)
        layout.addRow("Protocol:", self.protocol_combo)
        layout.addRow("Transducer:", self.transducer_combo)

        buttons = qt.QDialogButtonBox(
            qt.QDialogButtonBox.Ok | qt.QDialogButtonBox.Cancel
        )
        buttons.accepted.connect(self.on_ok_clicked)
        buttons.rejected.connect(self.reject)
        layout.addRow(buttons)

    def on_ok_clicked(self) -> None:
        session_id = self.id_edit.text.strip()
        if not session_id:
            show_info_dialog("An ID is required.")
            return
        self.session_id = session_id
        self.session_name = self.name_edit.text.strip() or session_id
        self.volume_id = self.volume_combo.currentText
        self.protocol_id = self.protocol_combo.currentText
        self.transducer_id = self.transducer_combo.currentText
        if not (self.volume_id and self.protocol_id and self.transducer_id):
            show_info_dialog("Volume, protocol, and transducer are required.")
            return
        self.accept()


class NewSonicationSessionDialog(qt.QDialog):
    """Minimal New Sonication Session dialog.

    Collects id, name, and a Plan selection. Used only from
    :meth:`OpenLIFUDataManagerWidget.on_new_sonication_button_clicked`.
    """

    def __init__(self, subject_id: str, plan_ids: List[str], parent=None):
        super().__init__(parent)
        self.setWindowTitle("New Sonication Session")
        self.session_id = ""
        self.session_name = ""
        self.plan_id = ""

        layout = qt.QFormLayout(self)
        self.id_edit = qt.QLineEdit()
        self.name_edit = qt.QLineEdit()
        self.plan_combo = qt.QComboBox()
        self.plan_combo.addItems(plan_ids)

        layout.addRow("ID:", self.id_edit)
        layout.addRow("Name:", self.name_edit)
        layout.addRow("Plan:", self.plan_combo)

        buttons = qt.QDialogButtonBox(
            qt.QDialogButtonBox.Ok | qt.QDialogButtonBox.Cancel
        )
        buttons.accepted.connect(self.on_ok_clicked)
        buttons.rejected.connect(self.reject)
        layout.addRow(buttons)

    def on_ok_clicked(self) -> None:
        session_id = self.id_edit.text.strip()
        if not session_id:
            show_info_dialog("An ID is required.")
            return
        self.session_id = session_id
        self.session_name = self.name_edit.text.strip() or session_id
        self.plan_id = self.plan_combo.currentText
        if not self.plan_id:
            show_info_dialog("A plan is required.")
            return
        self.accept()


# ---------------------------------------------------------------------------
# Module-level helpers (public per docs/coding-standards.md)
# ---------------------------------------------------------------------------

class SignalBlocker:
    """Context manager that blocks Qt signals on a ``QObject``.

    Use around programmatic mutation of a combo box or other widget
    when you do NOT want your own signal handlers to fire.
    """

    def __init__(self, obj):
        self.obj = obj
        self.previous = None

    def __enter__(self):
        self.previous = self.obj.blockSignals(True)
        return self.obj

    def __exit__(self, *exc_info):
        if self.previous is not None:
            self.obj.blockSignals(self.previous)


def make_fixed_width_table(columns: ColumnSpec) -> qt.QTableWidget:
    """Create a ``QTableWidget`` with fixed-by-default resizable column widths.

    Column headers use ``QHeaderView.Interactive`` resize mode so the
    user can drag to resize but nothing auto-adjusts to content -- this
    avoids the "content changed, columns jumped" problem you get with
    the default ``ResizeToContents`` mode.

    Args:
        columns: list of ``(header, default_width_px)`` tuples, one per column.
    """
    table = qt.QTableWidget()
    table.setColumnCount(len(columns))
    table.setHorizontalHeaderLabels([header for header, _ in columns])
    table.setSelectionBehavior(qt.QAbstractItemView.SelectRows)
    table.setSelectionMode(qt.QAbstractItemView.SingleSelection)
    table.setEditTriggers(qt.QAbstractItemView.NoEditTriggers)
    table.verticalHeader().setVisible(False)
    table.setAlternatingRowColors(True)

    header = table.horizontalHeader()
    header.setSectionResizeMode(qt.QHeaderView.Interactive)
    header.setStretchLastSection(True)
    for index, (_, width) in enumerate(columns):
        table.setColumnWidth(index, int(width))

    table.setMinimumHeight(120)
    return table


def fill_table_with_tooltips(table: qt.QTableWidget, rows: Sequence[tuple]) -> None:
    """Populate ``table`` with ``rows`` and put each cell's full text
    into its tooltip so long strings can be hovered."""
    table.setRowCount(len(rows))
    for row_index, row in enumerate(rows):
        for col_index, value in enumerate(row):
            text = "" if value is None else str(value)
            item = qt.QTableWidgetItem(text)
            item.setToolTip(text)
            table.setItem(row_index, col_index, item)


def collect_solution_infos_from_sessions(database, subject_id: str) -> dict:
    """Walk this subject's PlanningSessions / Plans / SonicationSessions
    and collect a ``{solution_id: SolutionInfo}`` map.

    Used to populate the Solutions table's provenance columns (target,
    source, approved, computed_at) without loading the actual Solution
    binary files.
    """
    result: dict = {}
    try:
        for ps_id in database.get_planning_session_ids(subject_id):
            planning_session = database.load_planning_session(subject_id, ps_id)
            for info in planning_session.pre_solutions:
                result.setdefault(info.solution_id, info)
        for plan_id in database.get_plan_ids(subject_id):
            plan = database.load_plan(subject_id, plan_id)
            for info in plan.pre_solutions:
                result.setdefault(info.solution_id, info)
        for ss_id in database.get_sonication_session_ids(subject_id):
            sonication_session = database.load_sonication_session(subject_id, ss_id)
            if sonication_session.solution is not None:
                result.setdefault(sonication_session.solution.solution_id, sonication_session.solution)
    except Exception:  # noqa: BLE001
        pass
    return result


def require_current_database() -> "openlifu.db.Database":
    """Return the currently-loaded database or raise ``RuntimeError``.

    Prefer this over ``get_cur_db()`` when the caller cannot proceed
    without a database -- the raise gives every button handler a clean
    error path.
    """
    database = get_cur_db()
    if database is None:
        raise RuntimeError("No database is currently loaded.")
    return database


def load_volume_node_from_database(
    database, subject_id: str, volume_id: str,
) -> Optional[slicer.vtkMRMLScalarVolumeNode]:
    """Load a subject volume into the Slicer scene as a scalar volume node.

    Reuses the DB metadata + NIfTI loader that legacy pages relied on;
    scoped to the minimum viable behaviour for split-session pages.
    """
    info = database.get_volume_info(subject_id, volume_id)
    volume_node = slicer.util.loadVolume(str(info["data_abspath"]))
    assign_openlifu_metadata_to_volume_node(volume_node, info)
    return volume_node


def create_target_fiducial_node(target: "openlifu.geo.Point") -> slicer.vtkMRMLMarkupsFiducialNode:
    """Create a fiducial node for the given openlifu Point target.

    Uses the shared conversion helper so downstream pages that read
    fiducials back as Points get consistent metadata (id, dims, units).
    """
    from OpenLIFULib.targets import openlifu_point_to_fiducial
    return openlifu_point_to_fiducial(target)


def confirm_action(text: str) -> bool:
    """Show a Cancel/OK confirmation dialog. Returns True on OK."""
    message_box = qt.QMessageBox()
    message_box.setIcon(qt.QMessageBox.Question)
    message_box.setWindowTitle("OpenLIFU Data Manager")
    message_box.setText(text)
    message_box.setStandardButtons(qt.QMessageBox.Ok | qt.QMessageBox.Cancel)
    message_box.setDefaultButton(qt.QMessageBox.Cancel)
    return message_box.exec_() == qt.QMessageBox.Ok


def show_info_dialog(text: str) -> None:
    """Show a modal info dialog with the Data Manager as its window title."""
    slicer.util.infoDisplay(text, windowTitle="OpenLIFU Data Manager")


def show_error_dialog(text: str) -> None:
    """Show a modal error dialog with the Data Manager as its window title."""
    slicer.util.errorDisplay(text, windowTitle="OpenLIFU Data Manager")


# ---------------------------------------------------------------------------
# Test stub
# ---------------------------------------------------------------------------

class OpenLIFUDataManagerTest(ScriptedLoadableModuleTest):
    """Placeholder test class. Automated tests will land alongside the
    Planning Session Overview / Sonication Session Overview commits."""

    def runTest(self) -> None:
        self.delayDisplay("OpenLIFUDataManager: no automated tests yet.")
