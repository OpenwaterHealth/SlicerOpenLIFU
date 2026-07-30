"""Sonication Session Overview page (split-session v2).

Information-only status card for the currently-loaded
SonicationSession. Shows the session's key fields plus counted
summaries -- no tables, no navigation buttons. Photoscans /
registrations / TT results / runs are all edited on their owning
pages (Localization / Sonication Control), not here.

Design revised per SlicerOpenLIFU#634 -- see that issue for
background. See ``docs/pages/sonication-session-overview.md`` for
the page-level docs.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Optional

import qt
from slicer.ScriptedLoadableModule import (
    ScriptedLoadableModuleLogic,
    ScriptedLoadableModuleWidget,
)

from OpenLIFULib import get_app_state

if TYPE_CHECKING:
    import openlifu.db


class OpenLIFUSonicationSessionOverviewWidget(ScriptedLoadableModuleWidget):
    """Read-only status card for a loaded SonicationSession.

    Reads its entire state from
    ``get_app_state().loaded_sonication_session`` on ``enter()``.
    """

    def __init__(self, parent=None):
        ScriptedLoadableModuleWidget.__init__(self, parent)
        self.moduleName = "OpenLIFU"
        self.logic: Optional[OpenLIFUSonicationSessionOverviewLogic] = None
        self.is_entered = False

    # ------------------------------------------------------------------
    # Slicer widget lifecycle
    # ------------------------------------------------------------------

    def setup(self) -> None:
        """Build the programmatic Qt UI once."""
        ScriptedLoadableModuleWidget.setup(self)
        self.logic = OpenLIFUSonicationSessionOverviewLogic()

        top = qt.QWidget()
        outer = qt.QVBoxLayout(top)
        outer.setContentsMargins(8, 8, 8, 8)
        outer.setSpacing(8)

        outer.addWidget(self.build_header_group())
        outer.addWidget(self.build_plan_group())
        outer.addWidget(self.build_summary_group())
        outer.addStretch(1)

        self.layout.addWidget(top)
        self.uiWidget = top

    def enter(self) -> None:
        """Repopulate every visible piece of state from
        ``get_app_state().loaded_sonication_session``."""
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
        """Session name / id / subject / plan_id / date."""
        group = qt.QGroupBox("Sonication Session")
        layout = qt.QFormLayout(group)
        self.name_label = qt.QLabel("—")
        self.id_label = qt.QLabel("—")
        self.subject_label = qt.QLabel("—")
        self.plan_id_label = qt.QLabel("—")
        self.date_created_label = qt.QLabel("—")
        self.date_modified_label = qt.QLabel("—")
        layout.addRow("Name:", self.name_label)
        layout.addRow("ID:", self.id_label)
        layout.addRow("Subject:", self.subject_label)
        layout.addRow("Plan:", self.plan_id_label)
        layout.addRow("Created:", self.date_created_label)
        layout.addRow("Modified:", self.date_modified_label)
        return group

    def build_plan_group(self) -> qt.QGroupBox:
        """Read-only view of the Plan's frozen fields.

        These come from the ``Plan`` referenced by the loaded
        ``SonicationSession`` and cannot be edited from this page.
        """
        group = qt.QGroupBox("Plan (frozen)")
        layout = qt.QFormLayout(group)
        self.plan_target_label = qt.QLabel("—")
        self.plan_volume_label = qt.QLabel("—")
        self.plan_protocol_label = qt.QLabel("—")
        self.plan_transducer_label = qt.QLabel("—")
        layout.addRow("Target:", self.plan_target_label)
        layout.addRow("Volume:", self.plan_volume_label)
        layout.addRow("Protocol:", self.plan_protocol_label)
        layout.addRow("Transducer:", self.plan_transducer_label)
        return group

    def build_summary_group(self) -> qt.QGroupBox:
        """Count-based summary of the session's editable content.

        Each row's rich editing surface lives on a subsequent workflow
        page (Localization / Solution Generator / Sonication Control).
        """
        group = qt.QGroupBox("Summary")
        layout = qt.QFormLayout(group)
        self.photoscans_summary_label = qt.QLabel("—")
        self.registrations_summary_label = qt.QLabel("—")
        self.tt_summary_label = qt.QLabel("—")
        self.solution_summary_label = qt.QLabel("—")
        self.runs_summary_label = qt.QLabel("—")
        layout.addRow("Photoscans:", self.photoscans_summary_label)
        layout.addRow("Photoscan registrations:", self.registrations_summary_label)
        layout.addRow("Transducer tracking:", self.tt_summary_label)
        layout.addRow("Final solution:", self.solution_summary_label)
        layout.addRow("Runs:", self.runs_summary_label)
        return group

    # ------------------------------------------------------------------
    # Refresh
    # ------------------------------------------------------------------

    def refresh_all(self) -> None:
        """Rebuild every label from the currently-loaded session."""
        sonication_session = get_app_state().loaded_sonication_session
        if sonication_session is None:
            self.render_no_session_loaded()
            return

        session = sonication_session.session.sonication_session
        plan = sonication_session.plan.plan

        self.name_label.text = session.name or "—"
        self.id_label.text = session.id
        self.subject_label.text = session.subject_id or "—"
        self.plan_id_label.text = session.plan_id or "—"
        self.date_created_label.text = (
            session.date_created.isoformat() if session.date_created else "—"
        )
        self.date_modified_label.text = (
            session.date_modified.isoformat() if session.date_modified else "—"
        )

        if plan is not None:
            target_id = plan.target.id if plan.target is not None else ""
            target_position = ""
            if plan.target is not None:
                target_position = ", ".join(f"{float(v):.2f}" for v in plan.target.position)
            self.plan_target_label.text = (
                f"{target_id} ({target_position} mm)" if target_id
                else "—"
            )
            self.plan_volume_label.text = plan.volume_id or "—"
            self.plan_protocol_label.text = plan.protocol_id or "—"
            self.plan_transducer_label.text = plan.transducer_id or "—"
        else:
            self.plan_target_label.text = "—"
            self.plan_volume_label.text = "—"
            self.plan_protocol_label.text = "—"
            self.plan_transducer_label.text = "—"

        self.photoscans_summary_label.text = self.build_photoscans_summary(session)
        self.registrations_summary_label.text = self.build_registrations_summary(session)
        self.tt_summary_label.text = self.build_tt_summary(session)
        self.solution_summary_label.text = self.build_solution_summary(session)
        self.runs_summary_label.text = self.build_runs_summary(session)

    def render_no_session_loaded(self) -> None:
        """Reset every label to placeholder text."""
        for label in (
            self.name_label, self.id_label, self.subject_label, self.plan_id_label,
            self.date_created_label, self.date_modified_label,
            self.plan_target_label, self.plan_volume_label,
            self.plan_protocol_label, self.plan_transducer_label,
            self.photoscans_summary_label, self.registrations_summary_label,
            self.tt_summary_label, self.solution_summary_label,
            self.runs_summary_label,
        ):
            label.text = "—"

    # ------------------------------------------------------------------
    # Summary formatters
    # ------------------------------------------------------------------

    def build_photoscans_summary(self, session) -> str:
        count = len(session.photoscan_ids)
        if count == 0:
            return "none captured"
        return f"{count}"

    def build_registrations_summary(self, session) -> str:
        registrations = session.photoscan_registrations
        count = len(registrations)
        if count == 0:
            return "none"
        approved = sum(1 for r in registrations if r.approval)
        return f"{count} ({approved} approved)"

    def build_tt_summary(self, session) -> str:
        results = session.transducer_tracking_results
        count = len(results)
        if count == 0:
            return "none"
        approved = sum(1 for r in results if r.approval)
        return f"{count} ({approved} approved)"

    def build_solution_summary(self, session) -> str:
        info = session.solution
        if info is None:
            return "not computed"
        parts = [info.solution_id]
        if info.transducer_transform_source:
            parts.append(f"from {info.transducer_transform_source}")
        parts.append("approved" if info.approved else "unapproved")
        return " · ".join(parts)

    def build_runs_summary(self, session) -> str:
        count = len(session.run_ids)
        if count == 0:
            return "none performed"
        return f"{count}"


class OpenLIFUSonicationSessionOverviewLogic(ScriptedLoadableModuleLogic):
    """Overview page's Logic class.

    Empty. Retained so the host's page-logic construction contract is
    consistent (see ``OpenLIFUApp.host.host_logic``); no business
    logic lives here because this page is information-only.
    """

    def __init__(self):
        ScriptedLoadableModuleLogic.__init__(self)
