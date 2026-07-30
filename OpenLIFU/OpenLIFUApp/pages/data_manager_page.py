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
the sole source of first-render truth (see design doc, section 5.1).
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


# ---------------------------------------------------------------------------
# Widget
# ---------------------------------------------------------------------------

class OpenLIFUDataManagerWidget(ScriptedLoadableModuleWidget):
    """Programmatic Qt UI for the split-session Data Manager."""

    def __init__(self, parent=None):
        ScriptedLoadableModuleWidget.__init__(self, parent)
        self.moduleName = "OpenLIFU"
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

        top_layout.addLayout(self._build_database_row())

        self.tabs = qt.QTabWidget()
        self.tabs.addTab(self._build_subject_tab(), "Subject Data")
        self.tabs.addTab(self._build_protocols_tab(), "Protocols")
        self.tabs.addTab(self._build_transducers_tab(), "Transducers")
        self.tabs.addTab(self._build_users_tab(), "Users")
        top_layout.addWidget(self.tabs, 1)

        self.layout.addWidget(top)
        self.uiWidget = top

    def enter(self) -> None:
        """``enter`` is the SOLE source of first-render truth (design mandate)."""
        self._entered = True
        self._refresh_db_status()
        self._refresh_subjects()
        self._refresh_all_lists()
        self._refresh_loaded_labels()
        self._refresh_db_tables()

    def exit(self) -> None:
        self._entered = False

    def cleanup(self) -> None:
        pass

    # ==================================================================
    # UI builders
    # ==================================================================

    def _build_database_row(self) -> qt.QHBoxLayout:
        row = qt.QHBoxLayout()
        row.addWidget(qt.QLabel("Database:"))
        self.db_path_label = qt.QLabel("(none loaded)")
        self.db_path_label.setStyleSheet("color: #666;")
        row.addWidget(self.db_path_label, 1)
        self.load_db_button = qt.QPushButton("Load…")
        self.load_db_button.setToolTip(
            "Open an openlifu file-based database from a local folder."
        )
        self.load_db_button.clicked.connect(self._on_load_db_clicked)
        row.addWidget(self.load_db_button)
        return row

    # -- Subject Data tab ----------------------------------------------

    def _build_subject_tab(self) -> qt.QWidget:
        tab = qt.QWidget()
        outer = qt.QVBoxLayout(tab)
        outer.setContentsMargins(4, 8, 4, 4)
        outer.setSpacing(6)

        # subject picker row
        row = qt.QHBoxLayout()
        row.addWidget(qt.QLabel("Subject:"))
        self.subject_combo = qt.QComboBox()
        self.subject_combo.setMinimumWidth(240)
        self.subject_combo.currentIndexChanged.connect(self._on_subject_changed)
        row.addWidget(self.subject_combo, 1)
        self.refresh_button = qt.QPushButton("Refresh")
        self.refresh_button.setToolTip("Rescan the database for this subject.")
        self.refresh_button.clicked.connect(self._on_refresh_clicked)
        row.addWidget(self.refresh_button)
        outer.addLayout(row)

        # -- Planning sessions section
        self.planning_table = _make_table([
            ("ID", 200),
            ("Name", 260),
            ("# Targets", 90),
        ])
        outer.addWidget(self._collapsible(
            title="Planning Sessions",
            hint=("Mutable working documents. Owns targets, virtual-fit "
                  "results, and pre-solutions."),
            table=self.planning_table,
            actions=[
                ("New…",   self._on_new_planning_clicked),
                ("Load",   self._on_load_planning_clicked),
                ("Delete", self._on_delete_planning_clicked),
            ],
        ))

        # -- Plans section
        self.plan_table = _make_table([
            ("ID", 200),
            ("Name", 220),
            ("Target", 140),
            ("Parent Planning Session", 200),
        ])
        outer.addWidget(self._collapsible(
            title="Plans",
            hint=("Immutable finalized treatment plans. Produced by "
                  "finalizing a Planning Session."),
            table=self.plan_table,
            actions=[
                ("Delete", self._on_delete_plan_clicked),
            ],
        ))

        # -- Sonication sessions section
        self.sonication_table = _make_table([
            ("ID", 200),
            ("Name", 220),
            ("Plan", 160),
            ("Solution", 160),
        ])
        outer.addWidget(self._collapsible(
            title="Sonication Sessions",
            hint=("At-treatment-time sessions. References a Plan for "
                  "target and pose; owns the photoscan registrations, "
                  "transducer tracking, and the final Solution."),
            table=self.sonication_table,
            actions=[
                ("New…",   self._on_new_sonication_clicked),
                ("Load",   self._on_load_sonication_clicked),
                ("Delete", self._on_delete_sonication_clicked),
            ],
        ))

        # -- Solutions section (subject-scoped)
        self.solution_table = _make_table([
            ("ID", 200),
            ("Target", 140),
            ("Source", 100),
            ("Approved", 90),
            ("Computed at", 180),
        ])
        outer.addWidget(self._collapsible(
            title="Solutions",
            hint=("Computed sonication solutions. Subject-scoped; a single "
                  "Solution can be referenced by a PlanningSession's "
                  "pre-solutions and by a SonicationSession's final "
                  "solution."),
            table=self.solution_table,
            actions=[
                ("Delete", self._on_delete_solution_clicked),
            ],
        ))

        outer.addWidget(self._build_loaded_group())
        outer.addStretch(1)
        return tab

    def _build_loaded_group(self) -> qt.QGroupBox:
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
        self.save_button.clicked.connect(self._on_save_clicked)
        self.close_button = qt.QPushButton("Close")
        self.close_button.setToolTip("Unload the currently-loaded session.")
        self.close_button.clicked.connect(self._on_close_clicked)
        row.addStretch(1)
        row.addWidget(self.save_button)
        row.addWidget(self.close_button)
        layout.addLayout(row)
        return group

    # -- DB-level tabs -------------------------------------------------

    def _build_protocols_tab(self) -> qt.QWidget:
        tab = qt.QWidget()
        layout = qt.QVBoxLayout(tab)
        layout.setContentsMargins(4, 8, 4, 4)
        self.protocol_table = _make_table([
            ("ID", 240),
            ("Name", 320),
        ])
        layout.addWidget(self.protocol_table, 1)
        return tab

    def _build_transducers_tab(self) -> qt.QWidget:
        tab = qt.QWidget()
        layout = qt.QVBoxLayout(tab)
        layout.setContentsMargins(4, 8, 4, 4)
        self.transducer_table = _make_table([
            ("ID", 240),
            ("Name", 320),
            ("# Elements", 100),
        ])
        layout.addWidget(self.transducer_table, 1)
        return tab

    def _build_users_tab(self) -> qt.QWidget:
        tab = qt.QWidget()
        layout = qt.QVBoxLayout(tab)
        layout.setContentsMargins(4, 8, 4, 4)
        self.user_table = _make_table([
            ("ID", 240),
            ("Name", 260),
            ("Roles", 200),
        ])
        layout.addWidget(self.user_table, 1)
        return tab

    def _collapsible(
        self,
        *,
        title: str,
        hint: str,
        table: qt.QTableWidget,
        actions: List[tuple],
    ) -> ctk.ctkCollapsibleButton:
        cb = ctk.ctkCollapsibleButton()
        cb.text = title
        cb.collapsed = False
        inner = qt.QVBoxLayout(cb)

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
        return cb

    # ==================================================================
    # Refresh helpers
    # ==================================================================

    def _refresh_db_status(self) -> None:
        db = get_cur_db()
        if db is None:
            self.db_path_label.text = "(none loaded)"
            self.db_path_label.setStyleSheet("color: #666;")
        else:
            self.db_path_label.text = str(getattr(db, "path", "loaded"))
            self.db_path_label.setStyleSheet("color: #060;")

    def _refresh_subjects(self) -> None:
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
            if prev in subject_ids:
                self.subject_combo.setCurrentText(prev)

    def _refresh_all_lists(self) -> None:
        subject_id = self._current_subject_id()
        db = get_cur_db()
        if db is None or not subject_id:
            _fill_table(self.planning_table, [])
            _fill_table(self.plan_table, [])
            _fill_table(self.sonication_table, [])
            _fill_table(self.solution_table, [])
            return

        planning_rows = []
        for ps_id in db.get_planning_session_ids(subject_id):
            try:
                ps = db.load_planning_session(subject_id, ps_id)
                planning_rows.append((ps.id, ps.name or "", str(len(ps.targets))))
            except Exception as exc:  # noqa: BLE001
                planning_rows.append((ps_id, f"<error: {exc}>", ""))
        _fill_table(self.planning_table, planning_rows)

        plan_rows = []
        for plan_id in db.get_plan_ids(subject_id):
            try:
                plan = db.load_plan(subject_id, plan_id)
                target_id = plan.target.id if plan.target is not None else ""
                plan_rows.append((
                    plan.id,
                    plan.name or "",
                    target_id,
                    plan.parent_planning_session_id or "",
                ))
            except Exception as exc:  # noqa: BLE001
                plan_rows.append((plan_id, f"<error: {exc}>", "", ""))
        _fill_table(self.plan_table, plan_rows)

        sonication_rows = []
        for ss_id in db.get_sonication_session_ids(subject_id):
            try:
                ss = db.load_sonication_session(subject_id, ss_id)
                solution_id = ss.solution.solution_id if ss.solution else ""
                sonication_rows.append((
                    ss.id, ss.name or "", ss.plan_id or "", solution_id,
                ))
            except Exception as exc:  # noqa: BLE001
                sonication_rows.append((ss_id, f"<error: {exc}>", "", ""))
        _fill_table(self.sonication_table, sonication_rows)

        # Solutions: subject-scoped. We don't parse the whole Solution here
        # (too heavy). We list ids and their metadata from anywhere we can:
        # if a SolutionInfo entry references this solution on a session or
        # a Plan, we grab the metadata from there. Otherwise the columns
        # stay blank.
        solution_ids = db.get_subject_solution_ids(subject_id)
        info_by_id = _collect_solution_infos(db, subject_id)
        solution_rows = []
        for sid in solution_ids:
            info = info_by_id.get(sid)
            if info is not None:
                solution_rows.append((
                    sid,
                    info.target_id or "",
                    info.transducer_transform_source or "",
                    "yes" if info.approved else "no",
                    info.computed_at.isoformat() if info.computed_at else "",
                ))
            else:
                solution_rows.append((sid, "", "", "", ""))
        _fill_table(self.solution_table, solution_rows)

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

        self.save_button.enabled = (ps is not None) or (ss is not None)
        self.close_button.enabled = (ps is not None) or (ss is not None)

    def _refresh_db_tables(self) -> None:
        """Refresh the DB-level tabs (protocols / transducers / users)."""
        db = get_cur_db()
        if db is None:
            _fill_table(self.protocol_table, [])
            _fill_table(self.transducer_table, [])
            _fill_table(self.user_table, [])
            return

        # Protocols
        rows = []
        for pid in db.get_protocol_ids():
            try:
                protocol = db.load_protocol(pid)
                rows.append((protocol.id, protocol.name or ""))
            except Exception:  # noqa: BLE001
                rows.append((pid, ""))
        _fill_table(self.protocol_table, rows)

        # Transducers
        rows = []
        for tid in db.get_transducer_ids():
            try:
                xdc = db.load_transducer(tid)
                n_el = len(xdc.elements) if getattr(xdc, "elements", None) else ""
                rows.append((xdc.id, xdc.name or "", str(n_el)))
            except Exception:  # noqa: BLE001
                rows.append((tid, "", ""))
        _fill_table(self.transducer_table, rows)

        # Users
        rows = []
        for uid in db.get_user_ids():
            try:
                user = db.load_user(uid)
                roles = getattr(user, "roles", None) or []
                rows.append((user.id, user.name or "", ", ".join(str(r) for r in roles)))
            except Exception:  # noqa: BLE001
                rows.append((uid, "", ""))
        _fill_table(self.user_table, rows)

    # ==================================================================
    # Selection helpers
    # ==================================================================

    def _current_subject_id(self) -> Optional[str]:
        if not self.subject_combo.isEnabled():
            return None
        text = self.subject_combo.currentText
        return text or None

    def _selected_id_in(self, table: qt.QTableWidget) -> Optional[str]:
        rows = table.selectionModel().selectedRows()
        if not rows:
            return None
        item = table.item(rows[0].row(), 0)
        return item.text() if item is not None else None

    # ==================================================================
    # Signal handlers
    # ==================================================================

    def _on_subject_changed(self, *_args) -> None:
        if not self._entered:
            return
        self._refresh_all_lists()

    def _on_refresh_clicked(self) -> None:
        self._refresh_db_status()
        self._refresh_subjects()
        self._refresh_all_lists()
        self._refresh_loaded_labels()
        self._refresh_db_tables()

    def _on_load_db_clicked(self) -> None:
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
        self._refresh_db_tables()

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

    # ---- solution actions ----

    def _on_delete_solution_clicked(self) -> None:
        subject_id = self._current_subject_id()
        sol_id = self._selected_id_in(self.solution_table)
        if not subject_id or not sol_id:
            _info("Select a solution first.")
            return
        if not _confirm(f"Delete solution {sol_id!r}?\n"
                       "PlanningSessions / Plans / SonicationSessions that "
                       "reference this solution will keep their SolutionInfo "
                       "entries but the on-disk files will be gone."):
            return
        try:
            get_cur_db().delete_solution_at_subject_scope(subject_id, sol_id)
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
    """Business logic for the split-session Data Manager."""

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
        import openlifu.db
        db = _require_db()
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
        db = _require_db()
        ps = db.load_planning_session(subject_id, planning_session_id)
        self.close_loaded_sessions()

        volume_node = _load_volume(db, subject_id, ps.volume_id)
        target_nodes = [_ensure_target_fiducial(t) for t in ps.targets]

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
        state = get_app_state()
        loaded = state.loaded_planning_session
        if loaded is not None and loaded.get_planning_session_id() == planning_session_id:
            self.close_loaded_sessions()
        db.delete_planning_session(subject_id, planning_session_id)

    def delete_plan(self, subject_id: str, plan_id: str) -> None:
        db = _require_db()
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
    """Minimum viable New Planning Session dialog."""

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
        for combo, populate in (
            (self.volume_combo, lambda: db.get_volume_ids(subject_id) if db else []),
            (self.protocol_combo, lambda: db.get_protocol_ids() if db else []),
            (self.transducer_combo, lambda: db.get_transducer_ids() if db else []),
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
    """Minimum viable New Sonication Session dialog."""

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


def _make_table(columns: ColumnSpec) -> qt.QTableWidget:
    """Create a QTableWidget with fixed-by-default resizable column widths.

    Column headers use ``Interactive`` resize mode so the user can drag to
    resize but nothing auto-adjusts to content (avoids the "content
    changed, columns jumped" problem).

    Args:
        columns: list of ``(header, default_width_px)`` tuples, one per column.
    """
    table = qt.QTableWidget()
    table.setColumnCount(len(columns))
    table.setHorizontalHeaderLabels([h for h, _ in columns])
    table.setSelectionBehavior(qt.QAbstractItemView.SelectRows)
    table.setSelectionMode(qt.QAbstractItemView.SingleSelection)
    table.setEditTriggers(qt.QAbstractItemView.NoEditTriggers)
    table.verticalHeader().setVisible(False)
    table.setAlternatingRowColors(True)

    header = table.horizontalHeader()
    header.setSectionResizeMode(qt.QHeaderView.Interactive)
    header.setStretchLastSection(True)
    for idx, (_, width) in enumerate(columns):
        table.setColumnWidth(idx, int(width))

    table.setMinimumHeight(120)
    return table


def _fill_table(table: qt.QTableWidget, rows: Sequence[tuple]) -> None:
    """Populate ``table`` with the given rows and put each cell's full text
    into its tooltip so long strings can be hovered."""
    table.setRowCount(len(rows))
    for row_idx, row in enumerate(rows):
        for col_idx, value in enumerate(row):
            text = "" if value is None else str(value)
            item = qt.QTableWidgetItem(text)
            item.setToolTip(text)
            table.setItem(row_idx, col_idx, item)


def _collect_solution_infos(db, subject_id: str) -> dict:
    """Walk this subject's PlanningSessions / Plans / SonicationSessions and
    collect a ``{solution_id: SolutionInfo}`` map.

    Used to populate the Solutions table's provenance columns (target,
    source, approved, computed_at) without loading the actual Solution
    files.
    """
    out = {}
    try:
        for ps_id in db.get_planning_session_ids(subject_id):
            ps = db.load_planning_session(subject_id, ps_id)
            for info in ps.pre_solutions:
                out.setdefault(info.solution_id, info)
        for plan_id in db.get_plan_ids(subject_id):
            plan = db.load_plan(subject_id, plan_id)
            for info in plan.pre_solutions:
                out.setdefault(info.solution_id, info)
        for ss_id in db.get_sonication_session_ids(subject_id):
            ss = db.load_sonication_session(subject_id, ss_id)
            if ss.solution is not None:
                out.setdefault(ss.solution.solution_id, ss.solution)
    except Exception:  # noqa: BLE001
        pass
    return out


def _require_db() -> "openlifu.db.Database":
    db = get_cur_db()
    if db is None:
        raise RuntimeError("No database is currently loaded.")
    return db


def _load_volume(db, subject_id: str, volume_id: str) -> Optional[slicer.vtkMRMLScalarVolumeNode]:
    info = db.get_volume_info(subject_id, volume_id)
    volume_node = slicer.util.loadVolume(str(info["data_abspath"]))
    assign_openlifu_metadata_to_volume_node(volume_node, info)
    return volume_node


def _ensure_target_fiducial(target: "openlifu.geo.Point") -> slicer.vtkMRMLMarkupsFiducialNode:
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
