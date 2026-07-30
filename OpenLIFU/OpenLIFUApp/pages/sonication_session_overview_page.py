"""Sonication Session Overview page (split-session v2).

Read-only status card for the currently-loaded ``SonicationSession``.
Shows the session's key fields and the frozen fields from its
referenced ``Plan`` (target, volume, protocol, transducer,
array_transform). Also shows the photoscans / photoscan
registrations / transducer-tracking results / final solution / runs
attached to this session.

Design mandate: reads state on ``enter()``, no cross-page observers.

See ``SESSION_SPLIT_DESIGN.md`` section 5.3 and
``docs/pages/sonication-session-overview.md`` for details.

Relates to SlicerOpenLIFU#633.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, List, Optional

import ctk
import qt
from slicer.ScriptedLoadableModule import (
    ScriptedLoadableModuleLogic,
    ScriptedLoadableModuleWidget,
)

from OpenLIFULib import (
    SlicerOpenLIFUSonicationSession,
    get_app_state,
)

from OpenLIFUApp.table_widgets import (
    fill_table_with_tooltips,
    make_fixed_width_table,
)

if TYPE_CHECKING:
    import openlifu.db


class OpenLIFUSonicationSessionOverviewWidget(ScriptedLoadableModuleWidget):
    """Status card for a loaded SonicationSession.

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
        outer.addWidget(self.build_photoscans_section())
        outer.addWidget(self.build_registrations_section())
        outer.addWidget(self.build_tt_section())
        outer.addWidget(self.build_solution_group())
        outer.addWidget(self.build_runs_section())
        outer.addLayout(self.build_action_row())
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
        """Build the top header showing the session name / id / subject / plan."""
        group = qt.QGroupBox("Sonication Session")
        layout = qt.QFormLayout(group)
        self.name_label = qt.QLabel("—")
        self.id_label = qt.QLabel("—")
        self.subject_label = qt.QLabel("—")
        self.plan_id_label = qt.QLabel("—")
        layout.addRow("Name:", self.name_label)
        layout.addRow("ID:", self.id_label)
        layout.addRow("Subject:", self.subject_label)
        layout.addRow("Plan:", self.plan_id_label)
        return group

    def build_plan_group(self) -> qt.QGroupBox:
        """Build the Plan-derived context section (read-only).

        The plan is frozen input to the sonication session -- these
        fields cannot be changed here.
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

    def build_photoscans_section(self) -> ctk.ctkCollapsibleButton:
        """Build the collapsible 'Photoscans' section."""
        self.photoscans_table = make_fixed_width_table([
            ("Photoscan ID", 260),
        ])
        return self.make_collapsible_section("Photoscans", self.photoscans_table)

    def build_registrations_section(self) -> ctk.ctkCollapsibleButton:
        """Build the collapsible 'Photoscan Registrations' section."""
        self.registrations_table = make_fixed_width_table([
            ("Registration ID", 240),
            ("Photoscan", 200),
            ("Approved", 100),
        ])
        return self.make_collapsible_section(
            "Photoscan Registrations", self.registrations_table,
        )

    def build_tt_section(self) -> ctk.ctkCollapsibleButton:
        """Build the collapsible 'Transducer Tracking Results' section."""
        self.tt_table = make_fixed_width_table([
            ("TT Result ID", 220),
            ("Target", 140),
            ("Photoscan", 200),
            ("Registration", 200),
            ("Approved", 100),
        ])
        return self.make_collapsible_section(
            "Transducer Tracking Results", self.tt_table,
        )

    def build_solution_group(self) -> qt.QGroupBox:
        """Build the 'Final Solution' section (one solution per session)."""
        group = qt.QGroupBox("Final Solution")
        layout = qt.QFormLayout(group)
        self.solution_id_label = qt.QLabel("—")
        self.solution_source_label = qt.QLabel("—")
        self.solution_approved_label = qt.QLabel("—")
        self.solution_computed_at_label = qt.QLabel("—")
        layout.addRow("Solution ID:", self.solution_id_label)
        layout.addRow("Source:", self.solution_source_label)
        layout.addRow("Approved:", self.solution_approved_label)
        layout.addRow("Computed at:", self.solution_computed_at_label)
        return group

    def build_runs_section(self) -> ctk.ctkCollapsibleButton:
        """Build the collapsible 'Runs' section."""
        self.runs_table = make_fixed_width_table([
            ("Run ID", 260),
        ])
        return self.make_collapsible_section("Runs", self.runs_table)

    def build_action_row(self) -> qt.QHBoxLayout:
        """Build the action buttons at the bottom of the page.

        All three navigation buttons are disabled until the corresponding
        pages land (SlicerOpenLIFU#TBD).
        """
        row = qt.QHBoxLayout()
        row.setSpacing(8)

        self.localization_button = qt.QPushButton("Go to Localization")
        self.localization_button.setToolTip("Coming soon: Localization page.")
        self.localization_button.enabled = False
        row.addWidget(self.localization_button)

        self.solution_generator_button = qt.QPushButton("Compute solution")
        self.solution_generator_button.setToolTip(
            "Coming soon: Solution Generator page."
        )
        self.solution_generator_button.enabled = False
        row.addWidget(self.solution_generator_button)

        self.sonication_control_button = qt.QPushButton("Go to Sonication Control")
        self.sonication_control_button.setToolTip(
            "Coming soon: Sonication Control page."
        )
        self.sonication_control_button.enabled = False
        row.addWidget(self.sonication_control_button)

        row.addStretch(1)
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
        """Rebuild every widget from the currently-loaded session."""
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

        if plan is not None:
            target_id = plan.target.id if plan.target is not None else ""
            target_position = ""
            if plan.target is not None:
                position_values = plan.target.position
                target_position = ", ".join(f"{float(v):.2f}" for v in position_values)
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

        # Photoscans (owned by this session).
        fill_table_with_tooltips(
            self.photoscans_table,
            [(pid,) for pid in session.photoscan_ids],
        )

        # Photoscan registrations.
        registration_rows: List[tuple] = []
        for registration in session.photoscan_registrations:
            registration_rows.append((
                registration.id or "",
                registration.photoscan_id or "",
                "yes" if registration.approval else "no",
            ))
        fill_table_with_tooltips(self.registrations_table, registration_rows)

        # Transducer-tracking results.
        tt_rows: List[tuple] = []
        for tt in session.transducer_tracking_results:
            tt_rows.append((
                tt.id or "",
                tt.target_id or "",
                tt.photoscan_id or "",
                tt.photoscan_registration_id or "",
                "yes" if tt.approval else "no",
            ))
        fill_table_with_tooltips(self.tt_table, tt_rows)

        # Final solution.
        if session.solution is not None:
            self.solution_id_label.text = session.solution.solution_id
            self.solution_source_label.text = (
                f"{session.solution.transducer_transform_source} "
                f"({session.solution.transducer_transform_source_id or 'unknown'})"
            )
            self.solution_approved_label.text = "yes" if session.solution.approved else "no"
            self.solution_computed_at_label.text = (
                session.solution.computed_at.isoformat()
                if session.solution.computed_at else "—"
            )
        else:
            self.solution_id_label.text = "(not computed)"
            self.solution_source_label.text = "—"
            self.solution_approved_label.text = "—"
            self.solution_computed_at_label.text = "—"

        # Runs -- id list only, no per-run detail load yet.
        fill_table_with_tooltips(
            self.runs_table,
            [(rid,) for rid in session.run_ids],
        )

    def render_no_session_loaded(self) -> None:
        """Show placeholder text everywhere."""
        for label in (
            self.name_label, self.id_label, self.subject_label, self.plan_id_label,
            self.plan_target_label, self.plan_volume_label,
            self.plan_protocol_label, self.plan_transducer_label,
            self.solution_id_label, self.solution_source_label,
            self.solution_approved_label, self.solution_computed_at_label,
        ):
            label.text = "—"
        for table in (
            self.photoscans_table, self.registrations_table,
            self.tt_table, self.runs_table,
        ):
            fill_table_with_tooltips(table, [])


# ---------------------------------------------------------------------------
# Logic
# ---------------------------------------------------------------------------

class OpenLIFUSonicationSessionOverviewLogic(ScriptedLoadableModuleLogic):
    """Business logic for the Sonication Session Overview page.

    Empty for now; retained as a hook for the actions those disabled
    navigation buttons will grow when the downstream pages
    (Localization, Solution Generator, Sonication Control) land.
    """

    def __init__(self):
        ScriptedLoadableModuleLogic.__init__(self)
