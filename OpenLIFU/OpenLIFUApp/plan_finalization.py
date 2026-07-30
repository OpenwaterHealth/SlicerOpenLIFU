"""Plan finalization: freeze a loaded PlanningSession into an immutable Plan.

Standalone module so any page that hosts a "Finalize Plan" action
(Pre-Planning, Solution Generator, workflow toolbar) can invoke it
without importing from another page.

Design decision background:

* SESSION_SPLIT_DESIGN.md section 8 originally placed the Finalize
  Plan button on the Planning Session Overview page.
* SlicerOpenLIFU#634 revised that: Session Overview is information-
  only, Finalize Plan moves to a page that owns workflow-mutating
  actions.
* The concrete new home is still TBD; when it lands, it will import
  :func:`finalize_plan` and :class:`FinalizePlanDialog` from here.

Nothing in this file uses cross-page state directly; everything is
passed in explicitly so :func:`finalize_plan` is driveable from a
scripted test without a widget.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

import qt
import slicer

from OpenLIFULib import get_app_state, get_cur_db

if TYPE_CHECKING:
    import openlifu.db


def finalize_plan(
    *,
    plan_id: str,
    plan_name: str,
    notes: str,
) -> str:
    """Freeze the currently-loaded PlanningSession into a new Plan.

    Semantics from SESSION_SPLIT_DESIGN.md section 8:

    1. Validate: the loaded PlanningSession has an approved virtual
       fit on its first target.
    2. Construct a Plan with subject / volume / protocol / transducer
       from the session, target = the session's first target,
       array_transform = the first approved VF for that target,
       pre_solutions = the session's pre_solutions filtered to those
       computed against a virtual-fit source for the target.
    3. Write the Plan to disk via ``db.write_plan``.
    4. Append the new Plan's id to
       ``session.finalized_plan_ids`` and persist the session via
       ``db.write_planning_session(..., on_conflict="overwrite")``.

    Args:
        plan_id: Id for the new plan. Must not already exist for the
            subject.
        plan_name: Human-readable name.
        notes: Free-form notes recorded at finalization time.

    Returns:
        The new plan id.

    Raises:
        RuntimeError: no PlanningSession loaded / no targets / no
            approved VF / no database loaded.
        ValueError: a Plan with ``plan_id`` already exists.
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


class FinalizePlanDialog(qt.QDialog):
    """Prompts the user for a Plan id, name, and free-form notes.

    Used by whichever page hosts the Finalize Plan action (see
    SlicerOpenLIFU#634 for the decision-in-progress). Displayed via
    ``dialog.exec_()``; on accept the ``plan_id`` / ``plan_name`` /
    ``notes`` attributes are populated.
    """

    def __init__(self, default_id: str, default_name: str, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Finalize Plan")
        # Result fields populated on accept.
        self.plan_id = ""
        self.plan_name = ""
        self.notes = ""

        # Body: Plan ID / Plan name / Notes.
        outer = qt.QVBoxLayout(self)
        form_widget = qt.QWidget()
        form = qt.QFormLayout(form_widget)
        self.id_edit = qt.QLineEdit(default_id)
        self.name_edit = qt.QLineEdit(default_name)
        self.notes_edit = qt.QPlainTextEdit()
        self.notes_edit.setPlaceholderText("Optional free-form notes.")
        self.notes_edit.setMinimumHeight(80)

        form.addRow("Plan ID:", self.id_edit)
        form.addRow("Plan name:", self.name_edit)
        form.addRow("Notes:", self.notes_edit)
        outer.addWidget(form_widget)

        # Footer: OK / Cancel button box. Kept OUTSIDE the QFormLayout
        # so the OK button always renders (a QFormLayout row wrapping a
        # QDialogButtonBox has intermittently swallowed the OK button
        # under PythonQt -- see SlicerOpenLIFU#634).
        button_box = qt.QDialogButtonBox(
            qt.QDialogButtonBox.Ok | qt.QDialogButtonBox.Cancel
        )
        button_box.accepted.connect(self.on_ok_clicked)
        button_box.rejected.connect(self.reject)
        outer.addWidget(button_box)

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
