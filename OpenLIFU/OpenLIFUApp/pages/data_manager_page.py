"""Data Manager page (split-session v2).

Fresh rewrite for the session-split refactor (SlicerOpenLIFU#631,
SESSION_SPLIT_DESIGN.md). Not derived from the legacy ``data_page.py``.

Responsibilities:

* Show the CRUD surface for the split-session model on one subject at a
  time: PlanningSessions, Plans, SonicationSessions.
* Show the reference-data lists (Protocols, Transducers, Volumes) needed
  when creating a new PlanningSession or SonicationSession.
* Route every read/write through the new subject-scoped ``Database``
  API (``load_planning_session`` / ``write_plan`` /
  ``load_sonication_session`` etc.).

Non-goals (deliberately):

* Any legacy ``openlifu.db.Session`` handling.
* Photoscan management, photocollection generation, protocol/transducer
  editing, run history, cloud sync, guided-workflow gating -- those get
  their own pages / dialogs in later commits or stay in the legacy Data
  page until retired.
* Cross-page observers wired to ``dataChanged``. The page reads state on
  ``enter()`` and refreshes itself in response to its own button
  handlers -- nothing else drives its UI.

Uses a programmatic Qt UI (no ``.ui`` file). This keeps the whole page
in one reviewable Python file and avoids another XML dependency during
the transition.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, List, Optional

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


# ---------------------------------------------------------------------------
# Widget
# ---------------------------------------------------------------------------

_COL_ID = 0
_COL_NAME = 1
_COL_EXTRA = 2  # per-list purpose: target-id / plan-id / plan-id


class OpenLIFUDataManagerWidget(ScriptedLoadableModuleWidget):
    """Programmatic Qt UI for the split-session Data Manager.

    Layout:

    * top row: subject picker + Refresh
    * three grouped sections (Planning / Plans / Sonication)
    * bottom row: current-load status + Save / Close for the loaded
      Planning or Sonication session
    """

    def __init__(self, parent=None):
        ScriptedLoadableModuleWidget.__init__(self, parent)
        self.moduleName = "OpenLIFU"  # embed under the host module's resources
        self.logic: Optional[OpenLIFUDataManagerLogic] = None
        self._entered = False

    # ------------------------------------------------------------------
    # Slicer lifecycle
    # ------------------------------------------------------------------

    def setup(self) -> None:
        ScriptedLoadableModuleWidget.setup(self)
        self.logic = OpenLIFUDataManagerLogic()

        top = qt.QWidget()
        top_layout = qt.QVBoxLayout(top)
        top_layout.setContentsMargins(8, 8, 8, 8)
        top_layout.setSpacing(8)

        # -- Database row -----------------------------------------------
        db_row = qt.QHBoxLayout()
        db_row.addWidget(qt.QLabel("Database:"))
        self.db_path_label = qt.QLabel("(none loaded)")
        self.db_path_label.setStyleSheet("color: #666;")
        db_row.addWidget(self.db_path_label, 1)
        self.load_db_button = qt.QPushButton("Load…")
        self.load_db_button.setToolTip(
            "Open an openlifu file-based database from a local folder."
        )
        self.load_db_button.clicked.connect(self._on_load_db_clicked)
        db_row.addWidget(self.load_db_button)
        top_layout.addLayout(db_row)

        # -- Subject row -------------------------------------------------
        subject_row = qt.QHBoxLayout()
        subject_row.addWidget(qt.QLabel("Subject:"))
        self.subject_combo = qt.QComboBox()
        self.subject_combo.setMinimumWidth(240)
        self.subject_combo.currentIndexChanged.connect(self._on_subject_changed)
        subject_row.addWidget(self.subject_combo, 1)

        self.refresh_button = qt.QPushButton("Refresh")
        self.refresh_button.setToolTip("Rescan the database for this subject.")
        self.refresh_button.clicked.connect(self._on_refresh_clicked)
        subject_row.addWidget(self.refresh_button)
        top_layout.addLayout(subject_row)

        # -- Planning sessions ------------------------------------------
        top_layout.addWidget(self._build_group(
            title="Planning Sessions",
            help_text=(
                "Mutable working documents. Owns targets, virtual-fit results, "
                "and pre-solutions. Finalize a plan from the Planning Session "
                "Overview page."
            ),
            table_headers=("ID", "Name", "# Targets"),
            attr_prefix="planning",
            actions=[
                ("New...", self._on_new_planning_clicked),
                ("Load",   self._on_load_planning_clicked),
                ("Delete", self._on_delete_planning_clicked),
            ],
        ))

        # -- Plans ------------------------------------------------------
        top_layout.addWidget(self._build_group(
            title="Plans",
            help_text=(
                "Immutable finalized treatment plans. Produced by finalizing a "
                "Planning Session. A Sonication Session starts from a Plan."
            ),
            table_headers=("ID", "Name", "Target"),
            attr_prefix="plan",
            actions=[
                ("Delete", self._on_delete_plan_clicked),
            ],
        ))

        # -- Sonication sessions ----------------------------------------
        top_layout.addWidget(self._build_group(
            title="Sonication Sessions",
            help_text=(
                "At-treatment-time sessions. References a Plan for target and "
                "pose; owns photoscan registrations, transducer tracking, and "
                "the final Solution."
            ),
            table_headers=("ID", "Name", "Plan"),
            attr_prefix="sonication",
            actions=[
                ("New...", self._on_new_sonication_clicked),
                ("Load",   self._on_load_sonication_clicked),
                ("Delete", self._on_delete_sonication_clicked),
            ],
        ))

        # -- Loaded status + Save / Close -------------------------------
        loaded_group = qt.QGroupBox("Loaded")
        loaded_layout = qt.QVBoxLayout(loaded_group)
        self.loaded_planning_label = qt.QLabel("Planning session: —")
        self.loaded_sonication_label = qt.QLabel("Sonication session: —")
        loaded_layout.addWidget(self.loaded_planning_label)
        loaded_layout.addWidget(self.loaded_sonication_label)

        buttons_row = qt.QHBoxLayout()
        self.save_button = qt.QPushButton("Save")
        self.save_button.setToolTip(
            "Write the loaded PlanningSession or SonicationSession JSON to disk."
        )
        self.save_button.clicked.connect(self._on_save_clicked)
        self.close_button = qt.QPushButton("Close")
        self.close_button.setToolTip("Unload the currently-loaded session.")
        self.close_button.clicked.connect(self._on_close_clicked)
        buttons_row.addStretch(1)
        buttons_row.addWidget(self.save_button)
        buttons_row.addWidget(self.close_button)
        loaded_layout.addLayout(buttons_row)
        top_layout.addWidget(loaded_group)

        top_layout.addStretch(1)

        self.layout.addWidget(top)
        # Required for _embed_page_widget_into_stack.
        self.uiWidget = top

    def enter(self) -> None:
        """Enter is the SOLE source of first-render truth (design mandate)."""
        self._entered = True
        self._refresh_db_status()
        self._refresh_subjects()
        self._refresh_all_lists()
        self._refresh_loaded_labels()

    def exit(self) -> None:
        self._entered = False

    def cleanup(self) -> None:
        pass

    # ------------------------------------------------------------------
    # UI builders
    # ------------------------------------------------------------------

    def _build_group(
        self,
        *,
        title: str,
        help_text: str,
        table_headers: tuple,
        attr_prefix: str,
        actions: List[tuple],
    ) -> qt.QGroupBox:
        """Build a labelled group with a compact 3-column table and action buttons.

        Stashes the table on ``self`` as ``<attr_prefix>_table`` for later
        access by the refresh methods.
        """
        group = qt.QGroupBox(title)
        layout = qt.QVBoxLayout(group)

        help_label = qt.QLabel(help_text)
        help_label.setWordWrap(True)
        help_label.setStyleSheet("color: #666;")
        layout.addWidget(help_label)

        table = qt.QTableWidget()
        table.setColumnCount(len(table_headers))
        table.setHorizontalHeaderLabels(list(table_headers))
        table.setSelectionBehavior(qt.QAbstractItemView.SelectRows)
        table.setSelectionMode(qt.QAbstractItemView.SingleSelection)
        table.setEditTriggers(qt.QAbstractItemView.NoEditTriggers)
        table.verticalHeader().setVisible(False)
        table.horizontalHeader().setStretchLastSection(True)
        table.setMinimumHeight(120)
        setattr(self, f"{attr_prefix}_table", table)
        layout.addWidget(table)

        actions_row = qt.QHBoxLayout()
        actions_row.addStretch(1)
        for label, slot in actions:
            btn = qt.QPushButton(label)
            btn.clicked.connect(slot)
            actions_row.addWidget(btn)
        layout.addLayout(actions_row)

        return group

    # ------------------------------------------------------------------
    # Refresh helpers
    # ------------------------------------------------------------------

    def _refresh_db_status(self) -> None:
        """Update the database status label and enable/disable child controls."""
        db = get_cur_db()
        if db is None:
            self.db_path_label.text = "(none loaded)"
            self.db_path_label.setStyleSheet("color: #666;")
        else:
            self.db_path_label.text = str(getattr(db, "path", "loaded"))
            self.db_path_label.setStyleSheet("color: #060;")

    def _refresh_subjects(self) -> None:
        """Repopulate the subject dropdown from the database."""
        db = get_cur_db()
        prev = self.subject_combo.currentText
        with _SignalBlocker(self.subject_combo):
            self.subject_combo.clear()
            if db is None:
                self.subject_combo.addItem("(no database)")
                self.subject_combo.setEnabled(False)
                return
            self.subject_combo.setEnabled(True)
            subject_ids = db.get_subject_ids()
            for sid in subject_ids:
                self.subject_combo.addItem(sid)
            # Restore prior selection if still available.
            if prev in subject_ids:
                self.subject_combo.setCurrentText(prev)

    def _refresh_all_lists(self) -> None:
        subject_id = self._current_subject_id()
        db = get_cur_db()
        if db is None or not subject_id:
            self._fill_table(self.planning_table, [])
            self._fill_table(self.plan_table, [])
            self._fill_table(self.sonication_table, [])
            return

        # -- Planning sessions
        planning_rows = []
        for ps_id in db.get_planning_session_ids(subject_id):
            try:
                ps = db.load_planning_session(subject_id, ps_id)
                planning_rows.append((ps.id, ps.name or "", str(len(ps.targets))))
            except Exception as exc:  # noqa: BLE001
                planning_rows.append((ps_id, f"<error: {exc}>", ""))
        self._fill_table(self.planning_table, planning_rows)

        # -- Plans
        plan_rows = []
        for plan_id in db.get_plan_ids(subject_id):
            try:
                plan = db.load_plan(subject_id, plan_id)
                target_id = plan.target.id if plan.target is not None else ""
                plan_rows.append((plan.id, plan.name or "", target_id))
            except Exception as exc:  # noqa: BLE001
                plan_rows.append((plan_id, f"<error: {exc}>", ""))
        self._fill_table(self.plan_table, plan_rows)

        # -- Sonication sessions
        sonication_rows = []
        for ss_id in db.get_sonication_session_ids(subject_id):
            try:
                ss = db.load_sonication_session(subject_id, ss_id)
                sonication_rows.append((ss.id, ss.name or "", ss.plan_id or ""))
            except Exception as exc:  # noqa: BLE001
                sonication_rows.append((ss_id, f"<error: {exc}>", ""))
        self._fill_table(self.sonication_table, sonication_rows)

    def _refresh_loaded_labels(self) -> None:
        state = get_app_state()
        ps = state.loaded_planning_session
        ss = state.loaded_sonication_session
        if ps is not None:
            self.loaded_planning_label.text = (
                f"Planning session: {ps.get_planning_session_id()} "
                f"(subject={ps.get_subject_id()})"
            )
        else:
            self.loaded_planning_label.text = "Planning session: —"
        if ss is not None:
            self.loaded_sonication_label.text = (
                f"Sonication session: {ss.get_sonication_session_id()} "
                f"(subject={ss.get_subject_id()}, plan={ss.get_plan_id()})"
            )
        else:
            self.loaded_sonication_label.text = "Sonication session: —"

        # Save button is only meaningful when something is loaded.
        self.save_button.enabled = (ps is not None) or (ss is not None)
        self.close_button.enabled = (ps is not None) or (ss is not None)

    def _fill_table(self, table: qt.QTableWidget, rows: List[tuple]) -> None:
        table.clearContents()
        table.setRowCount(len(rows))
        for row_idx, row in enumerate(rows):
            for col_idx, value in enumerate(row):
                item = qt.QTableWidgetItem(str(value))
                table.setItem(row_idx, col_idx, item)
        table.resizeColumnsToContents()

    # ------------------------------------------------------------------
    # Selection helpers
    # ------------------------------------------------------------------

    def _current_subject_id(self) -> Optional[str]:
        if not self.subject_combo.isEnabled():
            return None
        text = self.subject_combo.currentText
        return text or None

    def _selected_id_in(self, table: qt.QTableWidget) -> Optional[str]:
        rows = table.selectionModel().selectedRows()
        if not rows:
            return None
        row_idx = rows[0].row()
        item = table.item(row_idx, _COL_ID)
        return item.text() if item is not None else None

    # ------------------------------------------------------------------
    # Signal handlers
    # ------------------------------------------------------------------

    def _on_subject_changed(self, *_args) -> None:
        if not self._entered:
            return
        self._refresh_all_lists()

    def _on_refresh_clicked(self) -> None:
        self._refresh_db_status()
        self._refresh_subjects()
        self._refresh_all_lists()
        self._refresh_loaded_labels()

    def _on_load_db_clicked(self) -> None:
        """Prompt for a database directory and load it via the host DatabaseLogic."""
        path = qt.QFileDialog.getExistingDirectory(
            self.parent, "Select openlifu database root", "",
        )
        if not path:
            return
        try:
            slicer.util.getModuleLogic("OpenLIFU").database_logic.load_database(path)
        except Exception as exc:  # noqa: BLE001
            _error(f"Failed to load database:\n{exc}")
            return
        self._refresh_db_status()
        self._refresh_subjects()
        self._refresh_all_lists()
        self._refresh_loaded_labels()

    # ---- planning session actions ----

    def _on_new_planning_clicked(self) -> None:
        subject_id = self._current_subject_id()
        if not subject_id:
            _info("Select a subject first.")
            return
        db = get_cur_db()
        dialog = NewPlanningSessionDialog(db, subject_id, self.parent)
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
            _error(f"Create failed: {exc}")
            return
        self._refresh_all_lists()

    def _on_load_planning_clicked(self) -> None:
        subject_id = self._current_subject_id()
        ps_id = self._selected_id_in(self.planning_table)
        if not subject_id or not ps_id:
            _info("Select a planning session first.")
            return
        try:
            self.logic.load_planning_session(subject_id, ps_id)
        except Exception as exc:  # noqa: BLE001
            _error(f"Load failed: {exc}")
            return
        self._refresh_loaded_labels()

    def _on_delete_planning_clicked(self) -> None:
        subject_id = self._current_subject_id()
        ps_id = self._selected_id_in(self.planning_table)
        if not subject_id or not ps_id:
            _info("Select a planning session first.")
            return
        if not _confirm(f"Delete planning session {ps_id!r}?\n"
                       "Plans this session finalized will remain."):
            return
        try:
            self.logic.delete_planning_session(subject_id, ps_id)
        except Exception as exc:  # noqa: BLE001
            _error(f"Delete failed: {exc}")
            return
        self._refresh_all_lists()

    # ---- plan actions ----

    def _on_delete_plan_clicked(self) -> None:
        subject_id = self._current_subject_id()
        plan_id = self._selected_id_in(self.plan_table)
        if not subject_id or not plan_id:
            _info("Select a plan first.")
            return
        if not _confirm(f"Delete plan {plan_id!r}?\n"
                       "Sonication sessions that reference this plan will "
                       "become dangling."):
            return
        try:
            self.logic.delete_plan(subject_id, plan_id)
        except Exception as exc:  # noqa: BLE001
            _error(f"Delete failed: {exc}")
            return
        self._refresh_all_lists()

    # ---- sonication session actions ----

    def _on_new_sonication_clicked(self) -> None:
        subject_id = self._current_subject_id()
        if not subject_id:
            _info("Select a subject first.")
            return
        db = get_cur_db()
        plan_ids = db.get_plan_ids(subject_id)
        if not plan_ids:
            _info("This subject has no plans. Finalize a plan first.")
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
            _error(f"Create failed: {exc}")
            return
        self._refresh_all_lists()

    def _on_load_sonication_clicked(self) -> None:
        subject_id = self._current_subject_id()
        ss_id = self._selected_id_in(self.sonication_table)
        if not subject_id or not ss_id:
            _info("Select a sonication session first.")
            return
        try:
            self.logic.load_sonication_session(subject_id, ss_id)
        except Exception as exc:  # noqa: BLE001
            _error(f"Load failed: {exc}")
            return
        self._refresh_loaded_labels()

    def _on_delete_sonication_clicked(self) -> None:
        subject_id = self._current_subject_id()
        ss_id = self._selected_id_in(self.sonication_table)
        if not subject_id or not ss_id:
            _info("Select a sonication session first.")
            return
        if not _confirm(f"Delete sonication session {ss_id!r}?\n"
                       "The referenced Plan and any subject-scoped photoscans "
                       "or solutions will remain."):
            return
        try:
            self.logic.delete_sonication_session(subject_id, ss_id)
        except Exception as exc:  # noqa: BLE001
            _error(f"Delete failed: {exc}")
            return
        self._refresh_all_lists()

    # ---- save/close ----

    def _on_save_clicked(self) -> None:
        try:
            self.logic.save_loaded_session()
        except Exception as exc:  # noqa: BLE001
            _error(f"Save failed: {exc}")
            return
        self._refresh_all_lists()

    def _on_close_clicked(self) -> None:
        state = get_app_state()
        if state.loaded_planning_session is None and state.loaded_sonication_session is None:
            return
        if not _confirm("Close the loaded session?"):
            return
        self.logic.close_loaded_sessions()
        self._refresh_loaded_labels()


# ---------------------------------------------------------------------------
# Logic
# ---------------------------------------------------------------------------

class OpenLIFUDataManagerLogic(ScriptedLoadableModuleLogic):
    """Business logic for the split-session Data Manager.

    Every method takes explicit inputs (no reaching into signals or GUIs)
    and touches disk / app state directly. Designed to be usable from
    scripted tests without the widget.
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
        db = _require_db()
        session = openlifu.db.PlanningSession(
            id=planning_session_id,
            name=name or planning_session_id,
            subject_id=subject_id,
            volume_id=volume_id,
            protocol_id=protocol_id,
            transducer_id=transducer_id,
        )
        db.write_planning_session(subject_id, session)
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
        db = _require_db()
        # Validate: Plan must exist on disk.
        if plan_id not in db.get_plan_ids(subject_id):
            raise ValueError(
                f"Plan {plan_id!r} does not exist for subject {subject_id!r}."
            )
        session = openlifu.db.SonicationSession(
            id=sonication_session_id,
            name=name or sonication_session_id,
            subject_id=subject_id,
            plan_id=plan_id,
        )
        db.write_sonication_session(subject_id, session)
        logging.info(
            "Created SonicationSession %s for subject %s (plan=%s)",
            sonication_session_id, subject_id, plan_id,
        )

    # -- loading -------------------------------------------------------

    def load_planning_session(self, subject_id: str, planning_session_id: str) -> None:
        """Load a PlanningSession into the app state.

        Unloads any currently-loaded planning or sonication session first so
        the app is in a single-loaded-session state.
        """
        db = _require_db()
        ps = db.load_planning_session(subject_id, planning_session_id)
        self.close_loaded_sessions()

        # Volume node: load / import from the DB volume.
        volume_node = _load_volume(db, subject_id, ps.volume_id)

        # Target fiducial nodes: minimum viable rendering -- one node per
        # target Point. Populating VF-derived transforms and re-solving
        # existing pre-solutions is out of scope for this commit.
        target_nodes = [_ensure_target_fiducial(target) for target in ps.targets]

        state = get_app_state()
        state.loaded_planning_session = SlicerOpenLIFUPlanningSession(
            session=SlicerOpenLIFUPlanningSessionWrapper(planning_session=ps),
            volume_node=volume_node,
            target_nodes=target_nodes,
        )
        logging.info(
            "Loaded PlanningSession %s for subject %s",
            planning_session_id, subject_id,
        )

    def load_sonication_session(self, subject_id: str, sonication_session_id: str) -> None:
        """Load a SonicationSession + its referenced Plan into the app state."""
        db = _require_db()
        ss = db.load_sonication_session(subject_id, sonication_session_id)
        if ss.plan_id is None:
            raise ValueError(
                f"SonicationSession {sonication_session_id!r} has no plan_id."
            )
        plan = db.load_plan(subject_id, ss.plan_id)
        self.close_loaded_sessions()

        volume_node = _load_volume(db, subject_id, plan.volume_id)

        state = get_app_state()
        state.loaded_sonication_session = SlicerOpenLIFUSonicationSession(
            session=SlicerOpenLIFUSonicationSessionWrapper(sonication_session=ss),
            plan=SlicerOpenLIFUPlanWrapper(plan=plan),
            volume_node=volume_node,
        )
        logging.info(
            "Loaded SonicationSession %s for subject %s (plan=%s)",
            sonication_session_id, subject_id, plan.id,
        )

    # -- deletion ------------------------------------------------------

    def delete_planning_session(self, subject_id: str, planning_session_id: str) -> None:
        db = _require_db()
        # If currently loaded, close first.
        state = get_app_state()
        loaded = state.loaded_planning_session
        if loaded is not None and loaded.get_planning_session_id() == planning_session_id:
            self.close_loaded_sessions()
        db.delete_planning_session(subject_id, planning_session_id)

    def delete_plan(self, subject_id: str, plan_id: str) -> None:
        db = _require_db()
        # If a currently-loaded SonicationSession references this plan, close it.
        state = get_app_state()
        loaded = state.loaded_sonication_session
        if loaded is not None and loaded.get_plan_id() == plan_id:
            self.close_loaded_sessions()
        db.delete_plan(subject_id, plan_id)

    def delete_sonication_session(self, subject_id: str, sonication_session_id: str) -> None:
        db = _require_db()
        state = get_app_state()
        loaded = state.loaded_sonication_session
        if loaded is not None and loaded.get_sonication_session_id() == sonication_session_id:
            self.close_loaded_sessions()
        db.delete_sonication_session(subject_id, sonication_session_id)

    # -- save / close --------------------------------------------------

    def save_loaded_session(self) -> None:
        """Write the currently-loaded PlanningSession or SonicationSession to disk."""
        db = _require_db()
        state = get_app_state()
        ps = state.loaded_planning_session
        ss = state.loaded_sonication_session
        wrote = False
        if ps is not None:
            db.write_planning_session(
                ps.get_subject_id(),
                ps.session.planning_session,
                on_conflict="overwrite",
            )
            wrote = True
        if ss is not None:
            db.write_sonication_session(
                ss.get_subject_id(),
                ss.session.sonication_session,
                on_conflict="overwrite",
            )
            wrote = True
        if not wrote:
            raise RuntimeError("Nothing loaded to save.")

    def close_loaded_sessions(self) -> None:
        """Unload any currently-loaded planning or sonication session.

        Tears down the scene nodes each session owned. Explicit; the app
        state never nulls a session out on its own.
        """
        state = get_app_state()

        ps = state.loaded_planning_session
        if ps is not None:
            for node in ps.get_target_nodes():
                try:
                    slicer.mrmlScene.RemoveNode(node)
                except Exception:  # noqa: BLE001
                    pass
            if ps.volume_node is not None:
                try:
                    slicer.mrmlScene.RemoveNode(ps.volume_node)
                except Exception:  # noqa: BLE001
                    pass
            state.loaded_planning_session = None

        ss = state.loaded_sonication_session
        if ss is not None:
            if ss.volume_node is not None:
                try:
                    slicer.mrmlScene.RemoveNode(ss.volume_node)
                except Exception:  # noqa: BLE001
                    pass
            state.loaded_sonication_session = None


# ---------------------------------------------------------------------------
# Dialog helpers
# ---------------------------------------------------------------------------

class NewPlanningSessionDialog(qt.QDialog):
    """Minimum viable New Planning Session dialog: pick id, name, volume,
    protocol, transducer."""

    def __init__(self, db, subject_id: str, parent=None):
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

        try:
            self.volume_combo.addItems(db.get_volume_ids(subject_id) if db else [])
        except Exception:  # noqa: BLE001
            pass
        try:
            self.protocol_combo.addItems(db.get_protocol_ids() if db else [])
        except Exception:  # noqa: BLE001
            pass
        try:
            self.transducer_combo.addItems(db.get_transducer_ids() if db else [])
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
        buttons.accepted.connect(self._on_accept)
        buttons.rejected.connect(self.reject)
        layout.addRow(buttons)

    def _on_accept(self) -> None:
        sid = self.id_edit.text.strip()
        if not sid:
            _info("An ID is required.")
            return
        self.session_id = sid
        self.session_name = self.name_edit.text.strip() or sid
        self.volume_id = self.volume_combo.currentText
        self.protocol_id = self.protocol_combo.currentText
        self.transducer_id = self.transducer_combo.currentText
        if not (self.volume_id and self.protocol_id and self.transducer_id):
            _info("Volume, protocol, and transducer are required.")
            return
        self.accept()


class NewSonicationSessionDialog(qt.QDialog):
    """Minimum viable New Sonication Session dialog: pick id, name, plan."""

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
        buttons.accepted.connect(self._on_accept)
        buttons.rejected.connect(self.reject)
        layout.addRow(buttons)

    def _on_accept(self) -> None:
        sid = self.id_edit.text.strip()
        if not sid:
            _info("An ID is required.")
            return
        self.session_id = sid
        self.session_name = self.name_edit.text.strip() or sid
        self.plan_id = self.plan_combo.currentText
        if not self.plan_id:
            _info("A plan is required.")
            return
        self.accept()


# ---------------------------------------------------------------------------
# Module-level helpers
# ---------------------------------------------------------------------------

class _SignalBlocker:
    """Context manager that blocks Qt signals on a QObject."""

    def __init__(self, obj: qt.QObject):
        self._obj = obj
        self._prev: Optional[bool] = None

    def __enter__(self):
        self._prev = self._obj.blockSignals(True)
        return self._obj

    def __exit__(self, *exc_info):
        if self._prev is not None:
            self._obj.blockSignals(self._prev)


def _require_db() -> "openlifu.db.Database":
    db = get_cur_db()
    if db is None:
        raise RuntimeError("No database is currently loaded.")
    return db


def _load_volume(db, subject_id: str, volume_id: str) -> Optional[slicer.vtkMRMLScalarVolumeNode]:
    """Load a subject volume into the scene as a scalar volume node.

    Reuses the DB metadata + NIfTI loader that legacy pages have relied on;
    scoped to the minimum viable behavior for split-session pages.
    """
    info = db.get_volume_info(subject_id, volume_id)
    volume_node = slicer.util.loadVolume(str(info["data_abspath"]))
    assign_openlifu_metadata_to_volume_node(volume_node, info)
    return volume_node


def _ensure_target_fiducial(target: "openlifu.geo.Point") -> slicer.vtkMRMLMarkupsFiducialNode:
    """Create a fiducial node for the given openlifu Point target.

    Uses the shared conversion helper so downstream pages that read
    fiducials back as Points get consistent metadata (id, dims, units).
    """
    from OpenLIFULib.targets import openlifu_point_to_fiducial
    return openlifu_point_to_fiducial(target)


def _confirm(text: str) -> bool:
    mb = qt.QMessageBox()
    mb.setIcon(qt.QMessageBox.Question)
    mb.setWindowTitle("OpenLIFU Data Manager")
    mb.setText(text)
    mb.setStandardButtons(qt.QMessageBox.Ok | qt.QMessageBox.Cancel)
    mb.setDefaultButton(qt.QMessageBox.Cancel)
    return mb.exec_() == qt.QMessageBox.Ok


def _info(text: str) -> None:
    slicer.util.infoDisplay(text, windowTitle="OpenLIFU Data Manager")


def _error(text: str) -> None:
    slicer.util.errorDisplay(text, windowTitle="OpenLIFU Data Manager")


# ---------------------------------------------------------------------------
# Test stub
# ---------------------------------------------------------------------------

class OpenLIFUDataManagerTest(ScriptedLoadableModuleTest):
    def runTest(self) -> None:
        self.delayDisplay("OpenLIFUDataManager: no automated tests yet.")
