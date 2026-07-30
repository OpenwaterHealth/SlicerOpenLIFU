"""Planning Session Overview page (split-session v2).

Read-only status card for the currently-loaded ``PlanningSession``.
Shows the session's key fields, sub-tables of targets / virtual-fit
results / pre-solutions / finalized plans, and the actions the user
takes at the session level (Pre-Planning navigation, Solution
Generator navigation, Finalize Plan).

Design mandate: reads state on ``enter()``, no cross-page observers.
Signal handlers mutate local state then call refresh directly.

See ``SESSION_SPLIT_DESIGN.md`` section 5.3 and section 8 for the
Plan-finalization flow, and ``docs/pages/planning-session-overview.md``
for the page-level documentation.

Relates to SlicerOpenLIFU#633.
"""

from __future__ import annotations

import logging
from datetime import datetime
from typing import TYPE_CHECKING, List, Optional

import ctk
import qt
import slicer
from slicer.ScriptedLoadableModule import (
    ScriptedLoadableModuleLogic,
    ScriptedLoadableModuleWidget,
)

from OpenLIFULib import (
    SlicerOpenLIFUPlanningSession,
    get_app_state,
    get_cur_db,
)

from OpenLIFUApp.table_widgets import (
    fill_table_with_tooltips,
    make_fixed_width_table,
)

if TYPE_CHECKING:
    import openlifu.db


class OpenLIFUPlanningSessionOverviewWidget(ScriptedLoadableModuleWidget):
    """Status card for a loaded PlanningSession.

    Reads its entire state from ``get_app_state().loaded_planning_session``
    on ``enter()``. When nothing is loaded, shows a placeholder screen
    inviting the user to load a session from the Data Manager.
    """

    def __init__(self, parent=None):
        ScriptedLoadableModuleWidget.__init__(self, parent)
        self.moduleName = "OpenLIFU"
        self.logic: Optional[OpenLIFUPlanningSessionOverviewLogic] = None
        self.is_entered = False

    # ------------------------------------------------------------------
    # Slicer widget lifecycle
    # ------------------------------------------------------------------

    def setup(self) -> None:
        """Build the programmatic Qt UI once."""
        ScriptedLoadableModuleWidget.setup(self)
        self.logic = OpenLIFUPlanningSessionOverviewLogic()

        top = qt.QWidget()
        outer = qt.QVBoxLayout(top)
        outer.setContentsMargins(8, 8, 8, 8)
        outer.setSpacing(8)

        outer.addWidget(self.build_header_group())
        outer.addWidget(self.build_metadata_group())
        outer.addWidget(self.build_targets_section())
        outer.addWidget(self.build_virtual_fit_section())
        outer.addWidget(self.build_pre_solutions_section())
        outer.addWidget(self.build_finalized_plans_section())
        outer.addLayout(self.build_action_row())
        outer.addStretch(1)

        self.layout.addWidget(top)
        self.uiWidget = top

    def enter(self) -> None:
        """Repopulate every visible piece of state from
        ``get_app_state().loaded_planning_session``."""
        self.is_entered = True
        self.refresh_all()

    def exit(self) -> None:
        self.is_entered = False

    def cleanup(self) -> None:
        pass

    # ------------------------------------------------------------------
    # UI builders
    # ------------------------------------------------------------------

    def build_header_group(self) -> qt.QGroupBox:
        """Build the top header showing the session name / id / subject."""
        group = qt.QGroupBox("Planning Session")
        layout = qt.QFormLayout(group)
        self.name_label = qt.QLabel("—")
        self.id_label = qt.QLabel("—")
        self.subject_label = qt.QLabel("—")
        layout.addRow("Name:", self.name_label)
        layout.addRow("ID:", self.id_label)
        layout.addRow("Subject:", self.subject_label)
        return group

    def build_metadata_group(self) -> qt.QGroupBox:
        """Build the session's context: volume, protocol, transducer."""
        group = qt.QGroupBox("Context")
        layout = qt.QFormLayout(group)
        self.volume_label = qt.QLabel("—")
        self.protocol_label = qt.QLabel("—")
        self.transducer_label = qt.QLabel("—")
        layout.addRow("Volume:", self.volume_label)
        layout.addRow("Protocol:", self.protocol_label)
        layout.addRow("Transducer:", self.transducer_label)
        return group

    def build_targets_section(self) -> ctk.ctkCollapsibleButton:
        """Build the collapsible 'Targets' section."""
        self.targets_table = make_fixed_width_table([
            ("ID", 200),
            ("Name", 260),
            ("Position (R, A, S) mm", 260),
        ])
        return self.make_collapsible_section("Targets", self.targets_table)

    def build_virtual_fit_section(self) -> ctk.ctkCollapsibleButton:
        """Build the collapsible 'Virtual Fit Results' section."""
        self.vf_table = make_fixed_width_table([
            ("Target", 200),
            ("# Results", 100),
            ("# Approved", 100),
        ])
        return self.make_collapsible_section("Virtual Fit Results", self.vf_table)

    def build_pre_solutions_section(self) -> ctk.ctkCollapsibleButton:
        """Build the collapsible 'Pre-Solutions' section."""
        self.pre_solutions_table = make_fixed_width_table([
            ("Solution ID", 200),
            ("Target", 140),
            ("Source", 120),
            ("Approved", 90),
            ("Computed at", 180),
        ])
        return self.make_collapsible_section(
            "Pre-Solutions", self.pre_solutions_table,
        )

    def build_finalized_plans_section(self) -> ctk.ctkCollapsibleButton:
        """Build the collapsible 'Finalized Plans' section."""
        self.plans_table = make_fixed_width_table([
            ("Plan ID", 240),
            ("Date created", 180),
            ("# Pre-solutions", 120),
            ("Notes", 260),
        ])
        return self.make_collapsible_section(
            "Finalized Plans", self.plans_table,
        )

    def build_action_row(self) -> qt.QHBoxLayout:
        """Build the action buttons at the bottom of the page.

        Pre-Planning and Solution Generator navigation buttons are
        disabled until those pages land (SlicerOpenLIFU#TBD). Finalize
        Plan is enabled when the loaded session has an approved virtual
        fit on its first target.
        """
        row = qt.QHBoxLayout()
        row.setSpacing(8)

        self.preplanning_button = qt.QPushButton("Go to Pre-Planning")
        self.preplanning_button.setToolTip("Coming soon: Pre-Planning page.")
        self.preplanning_button.enabled = False
        row.addWidget(self.preplanning_button)

        self.solution_generator_button = qt.QPushButton("Compute pre-solutions")
        self.solution_generator_button.setToolTip(
            "Coming soon: Solution Generator page."
        )
        self.solution_generator_button.enabled = False
        row.addWidget(self.solution_generator_button)

        row.addStretch(1)

        self.finalize_button = qt.QPushButton("Finalize Plan…")
        self.finalize_button.setToolTip(
            "Freeze the currently-approved virtual fit + target into a new "
            "immutable Plan."
        )
        self.finalize_button.clicked.connect(self.on_finalize_button_clicked)
        row.addWidget(self.finalize_button)

        return row

    def make_collapsible_section(
        self,
        title: str,
        table: qt.QTableWidget,
    ) -> ctk.ctkCollapsibleButton:
        """Wrap a table in a ``ctkCollapsibleButton``."""
        section = ctk.ctkCollapsibleButton()
        section.text = title
        section.collapsed = False
        inner = qt.QVBoxLayout(section)
        inner.addWidget(table)
        return section

    # ------------------------------------------------------------------
    # Refresh
    # ------------------------------------------------------------------

    def refresh_all(self) -> None:
        """Rebuild every widget from the currently-loaded session.

        When no session is loaded, empties every table and shows placeholder
        text in the header labels.
        """
        planning_session = get_app_state().loaded_planning_session
        if planning_session is None:
            self.render_no_session_loaded()
            return

        session = planning_session.session.planning_session
        self.name_label.text = session.name or "—"
        self.id_label.text = session.id
        self.subject_label.text = session.subject_id or "—"
        self.volume_label.text = session.volume_id or "—"
        self.protocol_label.text = session.protocol_id or "—"
        self.transducer_label.text = session.transducer_id or "—"

        # Targets
        target_rows: List[tuple] = []
        for target in session.targets:
            position = target.position
            position_text = ", ".join(f"{float(v):.2f}" for v in position)
            target_rows.append((target.id, target.name or "", position_text))
        fill_table_with_tooltips(self.targets_table, target_rows)

        # Virtual fit results
        vf_rows: List[tuple] = []
        for target_id, transforms in session.virtual_fit_results.items():
            approved_count = sum(1 for approval, _ in transforms if approval)
            vf_rows.append((target_id, str(len(transforms)), str(approved_count)))
        fill_table_with_tooltips(self.vf_table, vf_rows)

        # Pre-solutions
        pre_solution_rows: List[tuple] = []
        for info in session.pre_solutions:
            pre_solution_rows.append((
                info.solution_id,
                info.target_id or "",
                info.transducer_transform_source or "",
                "yes" if info.approved else "no",
                info.computed_at.isoformat() if info.computed_at else "",
            ))
        fill_table_with_tooltips(self.pre_solutions_table, pre_solution_rows)

        # Finalized plans -- load each from disk to show its metadata.
        plan_rows: List[tuple] = []
        database = get_cur_db()
        for plan_id in session.finalized_plan_ids:
            plan = None
            if database is not None:
                try:
                    plan = database.load_plan(session.subject_id, plan_id)
                except Exception:  # noqa: BLE001
                    plan = None
            if plan is not None:
                plan_rows.append((
                    plan.id,
                    plan.date_created.isoformat() if plan.date_created else "",
                    str(len(plan.pre_solutions)),
                    plan.notes or "",
                ))
            else:
                plan_rows.append((plan_id, "", "", "<not loadable>"))
        fill_table_with_tooltips(self.plans_table, plan_rows)

        # Finalize button enablement: session must have at least one target
        # AND an approved virtual fit for that target.
        can_finalize = self.session_can_finalize(session)
        self.finalize_button.enabled = can_finalize
        if not can_finalize:
            self.finalize_button.setToolTip(
                "Finalize requires at least one target with an approved "
                "virtual fit."
            )
        else:
            self.finalize_button.setToolTip(
                "Freeze the currently-approved virtual fit + target into a "
                "new immutable Plan."
            )

    def render_no_session_loaded(self) -> None:
        """Show placeholder text everywhere and disable the finalize button."""
        for label in (
            self.name_label, self.id_label, self.subject_label,
            self.volume_label, self.protocol_label, self.transducer_label,
        ):
            label.text = "—"
        for table in (
            self.targets_table, self.vf_table,
            self.pre_solutions_table, self.plans_table,
        ):
            fill_table_with_tooltips(table, [])
        self.finalize_button.enabled = False
        self.finalize_button.setToolTip(
            "Load a planning session from the Data Manager to enable this."
        )

    def session_can_finalize(self, session) -> bool:
        """Return True iff the session has at least one target with an
        approved virtual fit."""
        if not session.targets:
            return False
        primary_target = session.targets[0]
        entries = session.virtual_fit_results.get(primary_target.id, [])
        return any(approval for approval, _ in entries)

    # ------------------------------------------------------------------
    # Signal handlers
    # ------------------------------------------------------------------

    def on_finalize_button_clicked(self) -> None:
        """Prompt for plan id / name / notes and finalize."""
        planning_session = get_app_state().loaded_planning_session
        if planning_session is None:
            return
        session = planning_session.session.planning_session
        if not self.session_can_finalize(session):
            slicer.util.errorDisplay(
                "Cannot finalize: no approved virtual fit for the first target.",
                windowTitle="Finalize Plan",
            )
            return

        default_id = f"{session.id}_{datetime.now().strftime('%Y%m%d_%H%M%S')}"
        default_name = session.name or session.id
        dialog = FinalizePlanDialog(default_id, default_name, self.parent)
        if dialog.exec_() != qt.QDialog.Accepted:
            return

        try:
            plan_id = self.logic.finalize_plan(
                plan_id=dialog.plan_id,
                plan_name=dialog.plan_name,
                notes=dialog.notes,
            )
        except Exception as exc:  # noqa: BLE001
            slicer.util.errorDisplay(
                f"Finalize failed: {exc}", windowTitle="Finalize Plan",
            )
            return
        slicer.util.infoDisplay(
            f"Finalized Plan {plan_id!r}.", windowTitle="Finalize Plan",
        )
        self.refresh_all()


# ---------------------------------------------------------------------------
# Logic
# ---------------------------------------------------------------------------

class OpenLIFUPlanningSessionOverviewLogic(ScriptedLoadableModuleLogic):
    """Business logic for the Planning Session Overview page.

    Currently exposes just the ``finalize_plan()`` method. Driveable
    without the widget for future scripted tests.
    """

    def __init__(self):
        ScriptedLoadableModuleLogic.__init__(self)

    def finalize_plan(
        self,
        *,
        plan_id: str,
        plan_name: str,
        notes: str,
    ) -> str:
        """Freeze the currently-loaded PlanningSession into a new Plan.

        Semantics from SESSION_SPLIT_DESIGN.md section 8:

        1. Validate: the loaded session has an approved virtual fit on
           its first target.
        2. Construct a Plan with subject / volume / protocol / transducer
           from the session, target = the session's first target,
           array_transform = the first approved VF for that target,
           pre_solutions = the session's pre_solutions filtered to those
           computed against a virtual-fit source for the target.
        3. Write the Plan to disk.
        4. Append the new Plan's id to
           ``session.finalized_plan_ids`` and persist the session.

        Returns the Plan id.

        Raises:
            RuntimeError: if no PlanningSession is loaded, or if the
                session has no approved VF, or if no database is
                currently loaded.
            ValueError: if a Plan with ``plan_id`` already exists.
        """
        state = get_app_state()
        loaded = state.loaded_planning_session
        if loaded is None:
            raise RuntimeError("No PlanningSession is loaded.")
        session = loaded.session.planning_session

        if not session.targets:
            raise RuntimeError("PlanningSession has no targets to finalize.")
        primary_target = session.targets[0]
        approved_transform = None
        for approval, transform in session.virtual_fit_results.get(primary_target.id, []):
            if approval:
                approved_transform = transform
                break
        if approved_transform is None:
            raise RuntimeError(
                f"No approved virtual fit for target {primary_target.id!r}."
            )

        # Filter pre-solutions to those computed against this target's VF.
        matching_pre_solutions = [
            info for info in session.pre_solutions
            if info.target_id == primary_target.id
            and info.transducer_transform_source == "virtual_fit"
        ]

        database = get_cur_db()
        if database is None:
            raise RuntimeError("No database is currently loaded.")

        if plan_id in database.get_plan_ids(session.subject_id):
            raise ValueError(
                f"A Plan with id {plan_id!r} already exists for subject "
                f"{session.subject_id!r}."
            )

        import openlifu.db
        plan = openlifu.db.Plan(
            id=plan_id,
            name=plan_name or plan_id,
            subject_id=session.subject_id,
            volume_id=session.volume_id,
            protocol_id=session.protocol_id,
            transducer_id=session.transducer_id,
            target=primary_target,
            array_transform=approved_transform,
            pre_solutions=list(matching_pre_solutions),
            parent_planning_session_id=session.id,
            notes=notes,
        )
        database.write_plan(session.subject_id, plan)

        session.finalized_plan_ids.append(plan_id)
        database.write_planning_session(
            session.subject_id, session, on_conflict="overwrite",
        )

        logging.info(
            "Finalized Plan %s from PlanningSession %s (subject=%s)",
            plan_id, session.id, session.subject_id,
        )
        return plan_id


# ---------------------------------------------------------------------------
# Dialog
# ---------------------------------------------------------------------------

class FinalizePlanDialog(qt.QDialog):
    """Prompts the user for a Plan id, name, and free-form notes when
    finalizing the currently-loaded PlanningSession."""

    def __init__(self, default_id: str, default_name: str, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Finalize Plan")
        self.plan_id = ""
        self.plan_name = ""
        self.notes = ""

        layout = qt.QFormLayout(self)
        self.id_edit = qt.QLineEdit(default_id)
        self.name_edit = qt.QLineEdit(default_name)
        self.notes_edit = qt.QPlainTextEdit()
        self.notes_edit.setPlaceholderText("Optional free-form notes.")
        self.notes_edit.setMinimumHeight(80)

        layout.addRow("Plan ID:", self.id_edit)
        layout.addRow("Plan name:", self.name_edit)
        layout.addRow("Notes:", self.notes_edit)

        buttons = qt.QDialogButtonBox(
            qt.QDialogButtonBox.Ok | qt.QDialogButtonBox.Cancel
        )
        buttons.accepted.connect(self.on_ok_clicked)
        buttons.rejected.connect(self.reject)
        layout.addRow(buttons)

    def on_ok_clicked(self) -> None:
        plan_id = self.id_edit.text.strip()
        if not plan_id:
            slicer.util.infoDisplay(
                "A Plan ID is required.", windowTitle="Finalize Plan",
            )
            return
        self.plan_id = plan_id
        self.plan_name = self.name_edit.text.strip() or plan_id
        self.notes = self.notes_edit.toPlainText().strip()
        self.accept()
