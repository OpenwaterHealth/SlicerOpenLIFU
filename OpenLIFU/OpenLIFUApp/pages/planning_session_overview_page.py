"""Planning Session Overview page (split-session v2).

Information-only status card for the currently-loaded PlanningSession.
Shows the session's key fields plus counted summaries -- no tables,
no action buttons that permute session state. Actions that mutate
targets / virtual fits / pre-solutions / finalized plans belong to
Pre-Planning, Solution Generator, and the workflow toolbar
respectively.

Design revised per SlicerOpenLIFU#634 -- see that issue for
background on why the earlier "rich tables + Finalize Plan button"
design was pulled back to summary-only. See
``docs/pages/planning-session-overview.md`` for the page-level
docs.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Optional

import qt
from slicer.ScriptedLoadableModule import (
    ScriptedLoadableModuleLogic,
    ScriptedLoadableModuleWidget,
)

from OpenLIFULib import get_app_state, get_cur_db

if TYPE_CHECKING:
    import openlifu.db


class OpenLIFUPlanningSessionOverviewWidget(ScriptedLoadableModuleWidget):
    """Read-only status card for a loaded PlanningSession.

    Reads its entire state from ``get_app_state().loaded_planning_session``
    on ``enter()``. When nothing is loaded, shows placeholder text in
    every label.
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
        outer.addWidget(self.build_context_group())
        outer.addWidget(self.build_summary_group())
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
        """Session name / id / subject / date."""
        group = qt.QGroupBox("Planning Session")
        layout = qt.QFormLayout(group)
        self.name_label = qt.QLabel("—")
        self.id_label = qt.QLabel("—")
        self.subject_label = qt.QLabel("—")
        self.date_created_label = qt.QLabel("—")
        self.date_modified_label = qt.QLabel("—")
        layout.addRow("Name:", self.name_label)
        layout.addRow("ID:", self.id_label)
        layout.addRow("Subject:", self.subject_label)
        layout.addRow("Created:", self.date_created_label)
        layout.addRow("Modified:", self.date_modified_label)
        return group

    def build_context_group(self) -> qt.QGroupBox:
        """Volume / protocol / transducer."""
        group = qt.QGroupBox("Context")
        layout = qt.QFormLayout(group)
        self.volume_label = qt.QLabel("—")
        self.protocol_label = qt.QLabel("—")
        self.transducer_label = qt.QLabel("—")
        layout.addRow("Volume:", self.volume_label)
        layout.addRow("Protocol:", self.protocol_label)
        layout.addRow("Transducer:", self.transducer_label)
        return group

    def build_summary_group(self) -> qt.QGroupBox:
        """Count-based summary of the session's editable content.

        Each row's rich editing surface lives on a subsequent workflow
        page (Pre-Planning / Solution Generator / ...).
        """
        group = qt.QGroupBox("Summary")
        layout = qt.QFormLayout(group)
        self.targets_summary_label = qt.QLabel("—")
        self.vf_summary_label = qt.QLabel("—")
        self.pre_solutions_summary_label = qt.QLabel("—")
        self.finalized_plans_summary_label = qt.QLabel("—")
        layout.addRow("Targets:", self.targets_summary_label)
        layout.addRow("Virtual fit results:", self.vf_summary_label)
        layout.addRow("Pre-solutions:", self.pre_solutions_summary_label)
        layout.addRow("Finalized plans:", self.finalized_plans_summary_label)
        return group

    # ------------------------------------------------------------------
    # Refresh
    # ------------------------------------------------------------------

    def refresh_all(self) -> None:
        """Rebuild every label from the currently-loaded session.

        When nothing is loaded, shows placeholder text.
        """
        planning_session = get_app_state().loaded_planning_session
        if planning_session is None:
            self.render_no_session_loaded()
            return

        session = planning_session.session.planning_session
        self.name_label.text = session.name or "—"
        self.id_label.text = session.id
        self.subject_label.text = session.subject_id or "—"
        self.date_created_label.text = (
            session.date_created.isoformat() if session.date_created else "—"
        )
        self.date_modified_label.text = (
            session.date_modified.isoformat() if session.date_modified else "—"
        )
        self.volume_label.text = session.volume_id or "—"
        self.protocol_label.text = session.protocol_id or "—"
        self.transducer_label.text = session.transducer_id or "—"

        self.targets_summary_label.text = self.build_targets_summary(session)
        self.vf_summary_label.text = self.build_vf_summary(session)
        self.pre_solutions_summary_label.text = self.build_pre_solutions_summary(session)
        self.finalized_plans_summary_label.text = self.build_finalized_plans_summary(session)

    def render_no_session_loaded(self) -> None:
        """Reset every label to placeholder text."""
        for label in (
            self.name_label, self.id_label, self.subject_label,
            self.date_created_label, self.date_modified_label,
            self.volume_label, self.protocol_label, self.transducer_label,
            self.targets_summary_label, self.vf_summary_label,
            self.pre_solutions_summary_label, self.finalized_plans_summary_label,
        ):
            label.text = "—"

    # ------------------------------------------------------------------
    # Summary formatters
    # ------------------------------------------------------------------

    def build_targets_summary(self, session) -> str:
        """Return the "N targets" summary line."""
        count = len(session.targets)
        if count == 0:
            return "no targets"
        target_names = ", ".join(t.id for t in session.targets[:3])
        if count > 3:
            target_names += ", ..."
        return f"{count} ({target_names})"

    def build_vf_summary(self, session) -> str:
        """Return the "N results across M targets, K approved" summary line."""
        vf = session.virtual_fit_results
        total = sum(len(v) for v in vf.values())
        target_count = len(vf)
        approved = sum(
            1
            for pairs in vf.values()
            for approval, _ in pairs
            if approval
        )
        if total == 0:
            return "none computed"
        return (
            f"{total} across {target_count} target{'s' if target_count != 1 else ''}, "
            f"{approved} approved"
        )

    def build_pre_solutions_summary(self, session) -> str:
        """Return the pre-solution count + approval summary."""
        count = len(session.pre_solutions)
        if count == 0:
            return "none computed"
        approved = sum(1 for info in session.pre_solutions if info.approved)
        return f"{count} ({approved} approved)"

    def build_finalized_plans_summary(self, session) -> str:
        """Return the finalized-plans count summary."""
        count = len(session.finalized_plan_ids)
        if count == 0:
            return "none"
        # Also count how many of those still exist on disk.
        database = get_cur_db()
        if database is None:
            return f"{count}"
        try:
            on_disk_ids = set(database.get_plan_ids(session.subject_id))
        except Exception:  # noqa: BLE001
            return f"{count}"
        missing = [pid for pid in session.finalized_plan_ids if pid not in on_disk_ids]
        if missing:
            return f"{count} ({len(missing)} missing on disk)"
        return f"{count}"


class OpenLIFUPlanningSessionOverviewLogic(ScriptedLoadableModuleLogic):
    """Overview page's Logic class.

    Empty. Retained so the host's page-logic construction contract is
    consistent (see ``OpenLIFUApp.host.host_logic``); no business
    logic lives here because this page is information-only. The
    Finalize Plan action lives in
    :mod:`OpenLIFUApp.plan_finalization` and will be invoked from
    whichever page hosts the finalize button (see
    SlicerOpenLIFU#634).
    """

    def __init__(self):
        ScriptedLoadableModuleLogic.__init__(self)
