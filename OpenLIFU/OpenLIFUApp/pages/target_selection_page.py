"""Target Selection page (split-session v2).

Fresh rewrite for the split-session refactor (SlicerOpenLIFU#631,
SlicerOpenLIFU#640). The legacy Pre-Planning page (2100 lines)
combined two workflows -- targets management and virtual-fit
management -- and the two sections' "current target" concepts could
disagree, producing ambiguous 3D scenes and pervasive re-entrancy
guards.

This page is the target-management half only. Virtual Fit lives on
the next page. Volume Segmentation will slot in ahead of us later.

Responsibilities:

* Show the loaded PlanningSession's targets in a single table.
* Add a target via click-to-place in a slice view.
* Import an existing scene fiducial or a fiducial file on disk.
* Edit target name / RAS coordinates via the table (Edit mode toggle).
* Toggle per-target visibility and jump-to-target in slice views.
* Remove a target -- and clear any dangling ``virtual_fit_results``
  entry that referenced it in the in-memory PlanningSession dict.

Deliberately NOT responsible for:

* Virtual fit computation, approval, or transducer pose. Those are
  the next page's job. This page therefore does not touch the
  transducer scene state or the skin surface.
* Timeline navigation (deferred until Virtual Fit lands).

Uses a programmatic Qt UI (no ``.ui`` file), following the convention
used by the other split-session pages.

See ``docs/pages/target-selection.md`` for the page-level docs and
``docs/coding-standards.md`` for the naming / enter-refresh / logic
separation rules this file follows.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import TYPE_CHECKING, List, Optional

import ctk
import qt
import slicer
import vtk
from slicer.ScriptedLoadableModule import (
    ScriptedLoadableModuleLogic,
    ScriptedLoadableModuleTest,
    ScriptedLoadableModuleWidget,
)

from OpenLIFULib import get_app_state
from OpenLIFULib.module_layout import wrap_page_in_scroll_area
from OpenLIFULib.targets import (
    assign_unique_color_to_fiducial,
    fiducial_to_openlifu_point,
    fiducial_to_openlifu_point_id,
    get_target_candidates,
)
from OpenLIFULib.util import mark_session_dirty

from OpenLIFUApp.dialogs import make_ok_cancel_button_box
from OpenLIFUApp.logic.session_actions import navigate_to_host_page
from OpenLIFUApp.table_widgets import (
    SignalBlocker,
    fill_table_with_tooltips,
    make_fixed_width_table,
)

if TYPE_CHECKING:
    from slicer import vtkMRMLMarkupsFiducialNode


# Slicer interaction-mode enum value for point placement, exposed for the
# EndPlacementEvent observer. Cheap to look up but reads better as a
# module-level constant.
PLACE_INTERACTION_MODE = slicer.vtkMRMLInteractionNode().Place

# Column indices for the targets table. Kept as named constants so
# ``on_targets_table_item_changed`` doesn't rely on magic numbers.
COLUMN_COLOR = 0
COLUMN_NAME = 1
COLUMN_ID = 2
COLUMN_R = 3
COLUMN_A = 4
COLUMN_S = 5
COLUMN_SHOW = 6
COLUMN_JUMP = 7

# Edit-mode highlight color for the actively-focused target's fiducial.
# Restored to its palette color when the row loses focus or edit mode
# toggles off. Chosen for high contrast against the tab10 palette.
EDIT_FOCUS_COLOR = (1.0, 1.0, 0.0)


class OpenLIFUTargetSelectionWidget(ScriptedLoadableModuleWidget):
    """Target Selection page widget.

    Owns the targets table + action row and drives all its state from
    ``enter()``. No cross-page observers -- ``mark_session_dirty()``
    (fired by the Logic class after every mutation) flips the toolbar
    Save button through the host's app-state signal.
    """

    def __init__(self, parent=None):
        ScriptedLoadableModuleWidget.__init__(self, parent)
        # Every embedded page borrows the host module's resource path so
        # ``self.resourcePath("Icons/foo.png")`` resolves under
        # ``OpenLIFU/Resources/`` rather than a per-page module dir.
        self.moduleName = "OpenLIFU"
        self.logic: Optional[OpenLIFUTargetSelectionLogic] = None
        # Set True in ``enter()`` and False in ``exit()``. Guards signal
        # handlers so they can bail during table rebuilds when the page
        # is not the visible page (Qt fires cell-change signals during
        # UI construction).
        self.is_entered = False
        # Set True while a ``refresh_targets_table()`` is repopulating
        # the QTableWidget. Signal handlers on cell changes / selection
        # bail during this window because the mutations they'd try to
        # apply reflect programmatic table state, not user intent.
        self.is_refreshing_table = False
        # Edit mode: cells are user-editable, the focused row's fiducial
        # is highlighted + unlocked so it can be dragged in slice views,
        # every other fiducial stays locked.
        self.is_in_edit_mode = False
        # Row that Edit mode focused on. Selection changes while
        # ``is_in_edit_mode`` is True are silently reverted to this row
        # -- the user is committed to editing one target at a time and
        # switching focus mid-edit is not supported
        # (SlicerOpenLIFU#644). ``None`` outside edit mode.
        self.edit_focus_row: Optional[int] = None
        # Placeholder for the fiducial the user is actively placing via
        # Add Target's PLACE-mode flow. ``None`` when no placement is in
        # progress. Only one at a time -- the Add button is disabled
        # while a placement is running.
        self.placement_node: Optional["vtkMRMLMarkupsFiducialNode"] = None
        # VTK observer tag on the interaction singleton, populated during
        # a placement and cleared in :meth:`finalize_placement`.
        self.placement_observer_tag: Optional[int] = None

    # ------------------------------------------------------------------
    # Slicer widget lifecycle
    # ------------------------------------------------------------------

    def setup(self) -> None:
        """Build the programmatic Qt UI once."""
        ScriptedLoadableModuleWidget.setup(self)
        self.logic = OpenLIFUTargetSelectionLogic()

        top = qt.QWidget()
        outer = qt.QVBoxLayout(top)
        outer.setContentsMargins(8, 8, 8, 8)
        outer.setSpacing(8)

        outer.addWidget(self.build_header())
        outer.addWidget(self.build_context_group())
        outer.addWidget(self.build_targets_group())
        outer.addStretch(1)

        # Wrap in a QScrollArea so the page scrolls INSIDE the host's
        # pageStack when its content exceeds the viewport, instead of
        # pushing the host's fixed header / footer off-screen
        # (SlicerOpenLIFU#643). ``uiWidget`` MUST point at the scroll
        # area itself, because the host's embed helper adds
        # ``widget.uiWidget`` directly into the pageStack.
        scroll = wrap_page_in_scroll_area(top)
        self.layout.addWidget(scroll)
        self.uiWidget = scroll

    def enter(self) -> None:
        """Sole first-render truth. Refresh every widget from the
        loaded PlanningSession + scene state."""
        self.is_entered = True
        self.refresh_all()

    def exit(self) -> None:
        """Mark the page as not entered. Cancel any in-flight placement
        so the user doesn't return to Home with an empty placeholder
        fiducial hanging around. Lock every target fiducial so navigating
        to another page doesn't leave a draggable target -- edit affordances
        are strictly this page's concern (SlicerOpenLIFU#641).

        Also captures any 3D-drag changes into the openlifu Session on
        the way out, so navigating away with Back-to-Home doesn't
        silently lose an unsaved drag.
        """
        self.is_entered = False
        if self.placement_node is not None:
            self.cancel_placement()
        if self.is_in_edit_mode:
            try:
                self.logic.sync_targets_from_scene()
            except Exception:  # noqa: BLE001
                logging.exception("TargetSelection: sync_targets_from_scene failed")
            with SignalBlocker(self.edit_button):
                self.edit_button.setChecked(False)
            self.is_in_edit_mode = False
            self.edit_focus_row = None
            self.edit_button.setText("Edit")
        self.apply_edit_focus_to_selection()

    def cleanup(self) -> None:
        """Widget teardown. Cancels any pending placement observer."""
        if self.placement_node is not None:
            self.cancel_placement()

    # ------------------------------------------------------------------
    # UI builders
    # ------------------------------------------------------------------

    def build_header(self) -> qt.QWidget:
        """Title + one-line hint describing the page's scope."""
        widget = qt.QWidget()
        layout = qt.QVBoxLayout(widget)
        layout.setContentsMargins(0, 0, 0, 4)
        title = qt.QLabel("Target Selection")
        title_font = title.font
        title_font.setPointSize(title_font.pointSize() + 3)
        title_font.setBold(True)
        title.font = title_font
        hint = qt.QLabel(
            "Add, import, edit, or remove sonication targets for the "
            "loaded planning session. Virtual fit is handled on the next page."
        )
        hint.setWordWrap(True)
        hint.setStyleSheet("color: #666;")
        layout.addWidget(title)
        layout.addWidget(hint)
        return widget

    def build_context_group(self) -> qt.QGroupBox:
        """Small read-only card showing which session's targets are being edited."""
        group = qt.QGroupBox("Planning Session")
        layout = qt.QFormLayout(group)
        self.session_label = qt.QLabel("—")
        self.subject_label = qt.QLabel("—")
        self.volume_label = qt.QLabel("—")
        layout.addRow("Session:", self.session_label)
        layout.addRow("Subject:", self.subject_label)
        layout.addRow("Volume:", self.volume_label)
        return group

    def build_targets_group(self) -> qt.QGroupBox:
        """Targets table + action row (Add / Import / Edit / Remove)."""
        group = qt.QGroupBox("Targets")
        layout = qt.QVBoxLayout(group)

        self.targets_table = make_fixed_width_table([
            ("",     28),
            ("Name", 180),
            ("ID",   140),
            ("R",     80),
            ("A",     80),
            ("S",     80),
            ("Show",  50),
            ("Jump",  56),
        ])
        # Cap the table's height at ~4 rows plus the header. Beyond
        # that the table scrolls internally rather than pushing the
        # action buttons off the bottom of the viewport
        # (SlicerOpenLIFU#642). Reserve a bit of extra room for the
        # optional horizontal scrollbar the ID column can trigger.
        self.targets_table.setMinimumHeight(160)
        self.targets_table.setMaximumHeight(200)
        # The ID column is user-visible but off by default -- the
        # display label is what the user cares about; the ID is only
        # relevant for debugging or for cross-referencing with an
        # openlifu Session JSON on disk.
        self.targets_table.setColumnHidden(COLUMN_ID, True)
        self.targets_table.itemChanged.connect(self.on_targets_table_item_changed)
        self.targets_table.itemSelectionChanged.connect(
            self.on_targets_table_selection_changed,
        )
        # Right-click on the header lets the user un-hide the ID column.
        self.targets_table.horizontalHeader().setContextMenuPolicy(qt.Qt.CustomContextMenu)
        self.targets_table.horizontalHeader().customContextMenuRequested.connect(
            self.on_targets_header_context_menu_requested,
        )
        layout.addWidget(self.targets_table)

        row = qt.QHBoxLayout()
        self.add_button = qt.QPushButton("Add Target")
        self.add_button.setToolTip(
            "Place a new target: click here, then click in a slice view."
        )
        self.add_button.clicked.connect(self.on_add_button_clicked)

        self.import_button = qt.QPushButton("Import…")
        self.import_button.setToolTip(
            "Register an existing scene fiducial as a target, or load "
            "one from a .mrk.json / .fcsv file."
        )
        self.import_button.clicked.connect(self.on_import_button_clicked)

        self.edit_button = qt.QPushButton("Edit")
        self.edit_button.setCheckable(True)
        self.edit_button.setToolTip(
            "Toggle target edit mode: unlock the selected target and "
            "allow inline editing of its name and R/A/S coordinates."
        )
        self.edit_button.toggled.connect(self.on_edit_toggle)

        self.remove_button = qt.QPushButton("Remove")
        self.remove_button.setToolTip("Remove the selected target.")
        self.remove_button.clicked.connect(self.on_remove_button_clicked)

        row.addWidget(self.add_button)
        row.addWidget(self.import_button)
        row.addStretch(1)
        row.addWidget(self.edit_button)
        row.addWidget(self.remove_button)
        layout.addLayout(row)

        return group

    # ------------------------------------------------------------------
    # Refresh
    # ------------------------------------------------------------------

    def refresh_all(self) -> None:
        """Refresh every visible piece of state from live sources.

        Idempotent: every call reads the current PlanningSession + scene
        and rebuilds each widget. Safe to invoke from ``enter()`` and
        from any signal handler that changed state.
        """
        self.refresh_context()
        self.refresh_targets_table()
        self.refresh_action_buttons()
        # Reapply the "edit-mode lock focus" contract after every
        # refresh. Enter-time this ensures every target starts locked
        # (edit mode always starts off on page entry, since ``exit()``
        # reset it). Mid-flow it re-locks anything that a scene
        # observer or external code may have unlocked out of band
        # (SlicerOpenLIFU#641).
        self.apply_edit_focus_to_selection()

    def refresh_context(self) -> None:
        """Repopulate the Planning Session context labels."""
        planning_session = get_app_state().loaded_planning_session
        if planning_session is None:
            self.session_label.text = "(none loaded)"
            self.subject_label.text = "—"
            self.volume_label.text = "—"
            return
        session = planning_session.session.planning_session
        self.session_label.text = f"{session.name or session.id} ({session.id})"
        self.subject_label.text = session.subject_id or "—"
        self.volume_label.text = session.volume_id or "—"

    def refresh_targets_table(self) -> None:
        """Rebuild the targets table from the loaded PlanningSession's
        ``target_nodes`` field.

        Blocks table signals with :attr:`is_refreshing_table` so cell
        assignments in this method don't fire user-intent handlers.
        """
        self.is_refreshing_table = True
        try:
            planning_session = get_app_state().loaded_planning_session
            target_nodes: List["vtkMRMLMarkupsFiducialNode"] = (
                planning_session.get_target_nodes()
                if planning_session is not None else []
            )

            self.targets_table.setRowCount(len(target_nodes))
            for row, node in enumerate(target_nodes):
                self.populate_target_row(row, node)
        finally:
            self.is_refreshing_table = False

    def populate_target_row(
        self, row: int, node: "vtkMRMLMarkupsFiducialNode",
    ) -> None:
        """Populate one row of the targets table from a fiducial node."""
        target_id = fiducial_to_openlifu_point_id(node)
        display_label = node.GetNthControlPointLabel(0) if node.GetNumberOfControlPoints() >= 1 else ""
        display_node = node.GetDisplayNode()
        color = display_node.GetSelectedColor() if display_node is not None else (1.0, 0.0, 0.0)
        position = (
            node.GetNthControlPointPosition(0)
            if node.GetNumberOfControlPoints() >= 1
            else (0.0, 0.0, 0.0)
        )
        visibility = bool(display_node.GetVisibility()) if display_node is not None else True

        # Column 0: color swatch (not editable). Uses a plain QWidget with
        # a background stylesheet -- more portable than QColorDialog fluff.
        swatch = qt.QWidget()
        css_color = (
            f"rgb({int(color[0] * 255)}, {int(color[1] * 255)}, {int(color[2] * 255)})"
        )
        swatch.setStyleSheet(f"background-color: {css_color}; border-radius: 6px;")
        swatch.setToolTip("Target color; assigned automatically from the palette.")
        self.targets_table.setCellWidget(row, COLUMN_COLOR, swatch)

        # Column 1: display Name (editable in edit mode). We stash the
        # fiducial node handle on the item so selection handlers can
        # recover it without walking scene ids.
        name_item = qt.QTableWidgetItem(display_label or target_id)
        name_item.setData(qt.Qt.UserRole, node)
        self.apply_edit_flag(name_item)
        self.targets_table.setItem(row, COLUMN_NAME, name_item)

        # Column 2: internal ID (openlifu Point id / fiducial node name).
        # Read-only regardless of edit mode -- changing it would break
        # ``virtual_fit_results`` / ``pre_solutions`` refs.
        id_item = qt.QTableWidgetItem(target_id)
        id_item.setFlags(id_item.flags() & ~qt.Qt.ItemIsEditable)
        id_item.setToolTip(
            "Internal openlifu Point id. Read-only; used as the key by "
            "virtual fit results, solutions, and the session JSON."
        )
        self.targets_table.setItem(row, COLUMN_ID, id_item)

        # Columns 3-5: R / A / S coordinates.
        for column, value in (
            (COLUMN_R, position[0]),
            (COLUMN_A, position[1]),
            (COLUMN_S, position[2]),
        ):
            item = qt.QTableWidgetItem(f"{value:.2f}")
            self.apply_edit_flag(item)
            self.targets_table.setItem(row, column, item)

        # Column 6: visibility checkbox.
        show_widget = qt.QCheckBox()
        show_widget.setChecked(visibility)
        show_widget.toggled.connect(
            lambda checked, n=node: self.on_show_checkbox_toggled(n, checked),
        )
        self.targets_table.setCellWidget(row, COLUMN_SHOW, show_widget)

        # Column 7: jump-to-target button.
        jump_button = qt.QPushButton("→")
        jump_button.setToolTip("Snap slice views to this target.")
        jump_button.clicked.connect(
            lambda _checked=False, n=node: self.on_jump_button_clicked(n),
        )
        self.targets_table.setCellWidget(row, COLUMN_JUMP, jump_button)

    def apply_edit_flag(self, item: qt.QTableWidgetItem) -> None:
        """Set the item's editable flag based on the current edit-mode toggle.

        Called during row population so newly-built rows respect the
        current mode. Toggling ``self.is_in_edit_mode`` at runtime
        triggers a full ``refresh_targets_table`` so this is always up
        to date.
        """
        flags = item.flags()
        if self.is_in_edit_mode:
            item.setFlags(flags | qt.Qt.ItemIsEditable)
        else:
            item.setFlags(flags & ~qt.Qt.ItemIsEditable)

    def refresh_action_buttons(self) -> None:
        """Enable / disable the action row based on state.

        Three modes (SlicerOpenLIFU#644):

        * **Placing**: user clicked Add Target and we're waiting for a
          slice-view click. Every button disabled. Escape is the sole
          cancel path -- it fires ``EndPlacementEvent`` with 0 control
          points and cleans up in :meth:`on_placement_ended`.
        * **Editing**: user is fine-tuning one focused target. Add /
          Import disabled so we can't start another placement while a
          target is open. Edit button stays enabled (reads "Done"). Remove
          stays enabled (still confirms).
        * **Normal**: nothing special. Add / Import / Edit enabled if a
          session is loaded; Remove enabled if a session is loaded AND a
          row is selected.
        """
        loaded = get_app_state().loaded_planning_session is not None
        placing = self.placement_node is not None
        editing = self.is_in_edit_mode
        selection_row = self.targets_table.currentRow()
        has_selection = selection_row is not None and selection_row >= 0

        if placing:
            self.add_button.enabled = False
            self.import_button.enabled = False
            self.edit_button.enabled = False
            self.remove_button.enabled = False
            return

        self.add_button.enabled = loaded and not editing
        self.import_button.enabled = loaded and not editing
        self.edit_button.enabled = loaded
        self.remove_button.enabled = loaded and has_selection

    # ------------------------------------------------------------------
    # Signal handlers
    # ------------------------------------------------------------------

    def on_add_button_clicked(self, _checked: bool = False) -> None:
        """Start point-placement: create a placeholder fiducial, enter
        PLACE mode, and observe the interaction node for placement-end.

        Guarded against re-entry while a placement is already in flight.
        On placement completion, :meth:`on_placement_ended` finalizes.
        """
        planning_session = get_app_state().loaded_planning_session
        if planning_session is None:
            self.show_info("Load a planning session first.")
            return
        if self.placement_node is not None:
            return  # already placing
        node = slicer.mrmlScene.AddNewNodeByClass("vtkMRMLMarkupsFiducialNode")
        node.SetMaximumNumberOfControlPoints(1)
        node.SetName(slicer.mrmlScene.GenerateUniqueName("Target"))
        node.SetMarkupLabelFormat("%N")
        self.placement_node = node

        interaction_node = slicer.mrmlScene.GetNodeByID("vtkMRMLInteractionNodeSingleton")
        self.placement_observer_tag = interaction_node.AddObserver(
            interaction_node.EndPlacementEvent, self.on_placement_ended,
        )
        # ``place mode persistence = False`` means we place one point
        # and immediately drop out of PLACE mode.
        slicer.modules.markups.logic().StartPlaceMode(False)
        self.refresh_action_buttons()

    def on_placement_ended(self, caller, _event) -> None:
        """Called by Slicer when the user places a point or cancels
        (Escape). Registers a completed target or removes an aborted
        placeholder, then enters Edit mode focused on the new row so
        the user can immediately drag the fiducial or type coordinates
        (SlicerOpenLIFU#641)."""
        interaction_node = caller
        if self.placement_observer_tag is not None:
            interaction_node.RemoveObserver(self.placement_observer_tag)
            self.placement_observer_tag = None
        node = self.placement_node
        self.placement_node = None
        if node is None or not slicer.mrmlScene.IsNodePresent(node):
            self.refresh_action_buttons()
            return
        if node.GetNumberOfControlPoints() < 1:
            # User pressed Escape or otherwise ended placement without
            # actually placing a point. Clean up the placeholder.
            slicer.mrmlScene.RemoveNode(node)
            self.refresh_action_buttons()
            return
        try:
            self.logic.add_target_from_scene(node)
        except Exception as exc:  # noqa: BLE001
            self.show_error(f"Failed to register placed target: {exc}")
            slicer.mrmlScene.RemoveNode(node)
            self.refresh_all()
            return

        # Refresh the table first so the new row exists, then select
        # that row BEFORE toggling into edit mode. Order matters:
        # entering edit mode uses the current selection to decide which
        # fiducial to unlock.
        self.refresh_context()
        self.refresh_targets_table()
        self.select_row_for_node(node)
        # Toggle Edit ON (or refresh focus if it was already on) so the
        # newly-placed fiducial is unlocked + draggable and the user can
        # fine-tune it right away.
        if self.is_in_edit_mode:
            self.apply_edit_focus_to_selection()
        else:
            self.edit_button.setChecked(True)  # fires on_edit_toggle
        self.refresh_action_buttons()

    def cancel_placement(self) -> None:
        """Cancel an in-flight placement (e.g. on ``exit()``).

        Removes the interaction-node observer, drops PLACE mode, and
        deletes the placeholder fiducial so the user doesn't return to
        find a stray empty target in the scene.
        """
        if self.placement_observer_tag is not None:
            interaction_node = slicer.mrmlScene.GetNodeByID(
                "vtkMRMLInteractionNodeSingleton",
            )
            if interaction_node is not None:
                try:
                    interaction_node.RemoveObserver(self.placement_observer_tag)
                except Exception:  # noqa: BLE001
                    pass
            self.placement_observer_tag = None
        if self.placement_node is not None and slicer.mrmlScene.IsNodePresent(self.placement_node):
            slicer.mrmlScene.RemoveNode(self.placement_node)
        self.placement_node = None
        # Best-effort: leave PLACE mode if we started it.
        try:
            slicer.modules.markups.logic().SetActiveList(None)
            interaction_node = slicer.mrmlScene.GetNodeByID(
                "vtkMRMLInteractionNodeSingleton",
            )
            if interaction_node is not None:
                interaction_node.SetCurrentInteractionMode(
                    slicer.vtkMRMLInteractionNode.ViewTransform,
                )
        except Exception:  # noqa: BLE001
            pass

    def on_import_button_clicked(self, _checked: bool = False) -> None:
        """Open the Import Target dialog and register whatever the user chose."""
        planning_session = get_app_state().loaded_planning_session
        if planning_session is None:
            self.show_info("Load a planning session first.")
            return
        # Filter out fiducials already registered as targets on this
        # session -- the dialog should only offer "importable" ones.
        current_ids = {
            n.GetID() for n in planning_session.get_target_nodes() if n is not None
        }
        candidates = [
            n for n in get_target_candidates() if n.GetID() not in current_ids
        ]
        dialog = ImportTargetDialog(candidates, self.uiWidget)
        if dialog.exec_() != qt.QDialog.Accepted:
            return
        try:
            if dialog.imported_node is not None:
                self.logic.add_target_from_scene(dialog.imported_node)
            elif dialog.imported_path:
                self.logic.import_target_from_disk(dialog.imported_path)
        except Exception as exc:  # noqa: BLE001
            self.show_error(f"Import failed: {exc}")
            return
        self.refresh_all()

    def on_edit_toggle(self, checked: bool) -> None:
        """Enter or exit Edit mode.

        Edit mode (SlicerOpenLIFU#641):

        * Unlocks the currently-focused (selected) fiducial so it can
          be dragged in slice / 3D views. Locks every other target
          fiducial to avoid accidental drags.
        * Sets table edit triggers so cells become editable.
        * Renames the Edit button to "Done".
        * Records the focused row in ``edit_focus_row`` so
          :meth:`on_targets_table_selection_changed` can revert user
          attempts to switch to another row (SlicerOpenLIFU#644).

        Leaving edit mode locks all fiducials, resets the table
        triggers so cells are read-only, and force-syncs any drag-only
        changes into the openlifu Session (the R / A / S cell path is
        already synced through :meth:`on_targets_table_item_changed`,
        but a 3D drag has no such sink until we push it here).
        """
        self.is_in_edit_mode = checked
        if checked:
            row = self.targets_table.currentRow()
            self.edit_focus_row = row if row is not None and row >= 0 else None
        else:
            self.edit_focus_row = None
        self.edit_button.setText("Done" if checked else "Edit")
        self.targets_table.setEditTriggers(
            qt.QAbstractItemView.DoubleClicked
            | qt.QAbstractItemView.EditKeyPressed
            | qt.QAbstractItemView.AnyKeyPressed
            if checked else qt.QAbstractItemView.NoEditTriggers
        )
        if not checked:
            # Exiting edit mode: capture any 3D-drag changes the user
            # made to the focused fiducial (or to any other target if
            # they touched multiple during the session). Also refresh
            # the R / A / S cells so the table shows the committed
            # values.
            try:
                self.logic.sync_targets_from_scene()
            except Exception:  # noqa: BLE001
                logging.exception("TargetSelection: sync_targets_from_scene failed")
        # Rebuild the table so Qt.ItemIsEditable flag toggles pick up
        # and R / A / S cells match the freshly-synced fiducial state.
        self.refresh_targets_table()
        # Belt-and-suspenders: the table rebuild may clobber selection
        # depending on Qt version. Re-select the edit-focus row so the
        # user's committed target stays the one that's highlighted +
        # unlocked when :meth:`apply_edit_focus_to_selection` runs
        # below (SlicerOpenLIFU#644).
        if checked and self.edit_focus_row is not None:
            row_count = self.targets_table.rowCount
            if 0 <= self.edit_focus_row < row_count:
                with SignalBlocker(self.targets_table):
                    self.targets_table.selectRow(self.edit_focus_row)
        # Apply the lock/unlock focus AFTER the table rebuild so the
        # selection restoration in the rebuild doesn't move focus
        # around unexpectedly.
        self.apply_edit_focus_to_selection()
        self.refresh_action_buttons()

    def apply_edit_focus_to_selection(self) -> None:
        """Scope the "unlocked in 3D" affordance to the selected row.

        Called on edit-toggle, on selection change while in edit mode,
        and after placement. Idempotent.

        Contract (SlicerOpenLIFU#641):

        * ``is_in_edit_mode == False``: lock every target fiducial.
        * ``is_in_edit_mode == True`` + no row selected: lock every
          target (nothing to edit yet).
        * ``is_in_edit_mode == True`` + row selected: unlock the
          selected fiducial, lock every other target.

        Focus is one-target-at-a-time by design -- the legacy code
        allowed multi-select unlocking and it produced ambiguous 3D
        interactions.
        """
        planning_session = get_app_state().loaded_planning_session
        if planning_session is None:
            return
        target_nodes = planning_session.get_target_nodes()
        if not self.is_in_edit_mode:
            for node in target_nodes:
                try:
                    node.SetLocked(True)
                except Exception:  # noqa: BLE001
                    pass
            return
        selected = self.selected_target_node()
        for node in target_nodes:
            try:
                node.SetLocked(node is not selected)
            except Exception:  # noqa: BLE001
                pass

    def select_row_for_node(
        self, node: "vtkMRMLMarkupsFiducialNode",
    ) -> None:
        """Move table selection to the row whose Name cell carries
        ``node`` in its UserRole data.

        Used after placement so the new target is the row the user
        sees selected (and therefore the fiducial that unlocks when
        edit mode enters). Silently no-ops if the row doesn't exist
        (e.g. the refresh raced ahead of the caller).
        """
        for row in range(self.targets_table.rowCount):
            name_item = self.targets_table.item(row, COLUMN_NAME)
            if name_item is not None and name_item.data(qt.Qt.UserRole) is node:
                self.targets_table.selectRow(row)
                return

    def on_remove_button_clicked(self, _checked: bool = False) -> None:
        """Remove the currently-selected target from the session and scene.

        If the removed target was the one under Edit-mode focus,
        exit Edit mode -- the ``edit_focus_row`` index would otherwise
        dangle at a stale row (SlicerOpenLIFU#644).
        """
        node = self.selected_target_node()
        if node is None:
            return
        target_id = fiducial_to_openlifu_point_id(node)
        display_label = node.GetNthControlPointLabel(0) or target_id
        if not self.confirm(
            f"Remove target {display_label!r}?\n"
            "Any virtual-fit results for this target will also be cleared."
        ):
            return
        try:
            self.logic.remove_target(node)
        except Exception as exc:  # noqa: BLE001
            self.show_error(f"Remove failed: {exc}")
            return
        if self.is_in_edit_mode:
            # Toggling the button fires on_edit_toggle(False), which
            # clears edit_focus_row, syncs (no-op after remove), and
            # resets edit triggers + button label.
            self.edit_button.setChecked(False)
        self.refresh_all()

    def on_targets_table_item_changed(self, item: qt.QTableWidgetItem) -> None:
        """Handle a table cell edit (name or R/A/S).

        Bails during refresh + when the page is not entered (Qt fires
        ``itemChanged`` during row construction too).
        """
        if not self.is_entered or self.is_refreshing_table:
            return
        row = item.row()
        column = item.column()
        name_item = self.targets_table.item(row, COLUMN_NAME)
        node: Optional["vtkMRMLMarkupsFiducialNode"] = (
            name_item.data(qt.Qt.UserRole) if name_item is not None else None
        )
        if node is None:
            return
        target_id = fiducial_to_openlifu_point_id(node)
        try:
            if column == COLUMN_NAME:
                self.logic.rename_target(target_id, item.text().strip())
            elif column in (COLUMN_R, COLUMN_A, COLUMN_S):
                position = list(node.GetNthControlPointPosition(0))
                axis_index = column - COLUMN_R
                position[axis_index] = float(item.text())
                self.logic.move_target(target_id, tuple(position))
        except ValueError:
            # User typed non-numeric coords etc.; roll back by refreshing.
            self.refresh_targets_table()
        except Exception as exc:  # noqa: BLE001
            self.show_error(f"Edit failed: {exc}")
            self.refresh_targets_table()

    def on_targets_table_selection_changed(self) -> None:
        """Handle a table selection change.

        Contract (SlicerOpenLIFU#644):

        * If Edit mode is on and the user tries to select a row that
          isn't the ``edit_focus_row``, revert the selection with a
          signal-blocked ``selectRow`` -- the user is committed to
          editing one target at a time.
        * If Edit mode is on and the selection lands back on
          ``edit_focus_row`` (e.g. because we just reverted), no-op.
        * Otherwise: rebuild Remove-button enablement.
        """
        if self.is_refreshing_table:
            return
        if self.is_in_edit_mode and self.edit_focus_row is not None:
            current = self.targets_table.currentRow()
            if current != self.edit_focus_row:
                with SignalBlocker(self.targets_table):
                    self.targets_table.selectRow(self.edit_focus_row)
                # After revert, the selection is back on the focused
                # row and edit focus is already applied to it. No need
                # to re-apply.
                return
            self.apply_edit_focus_to_selection()
        self.refresh_action_buttons()

    def on_show_checkbox_toggled(
        self, node: "vtkMRMLMarkupsFiducialNode", checked: bool,
    ) -> None:
        """Toggle a target fiducial's display-node visibility.

        Called by the per-row QCheckBox in the Show column.
        Visibility is a scene-node property and not persisted in the
        openlifu Point -- so this does NOT mark the session dirty.
        """
        try:
            self.logic.toggle_visibility(node, checked)
        except Exception as exc:  # noqa: BLE001
            self.show_error(f"Failed to toggle visibility: {exc}")

    def on_jump_button_clicked(
        self, node: "vtkMRMLMarkupsFiducialNode",
    ) -> None:
        """Snap slice views to the given target's position."""
        try:
            self.logic.jump_to_target(node)
        except Exception as exc:  # noqa: BLE001
            self.show_error(f"Failed to jump to target: {exc}")

    def on_targets_header_context_menu_requested(self, pos: qt.QPoint) -> None:
        """Right-click on the header shows a menu with hide/show toggles
        for the hideable columns (currently just the ID column)."""
        header = self.targets_table.horizontalHeader()
        menu = qt.QMenu(header)
        hideable = [COLUMN_ID]
        for column in hideable:
            header_item = self.targets_table.horizontalHeaderItem(column)
            label = header_item.text() if header_item is not None else f"Column {column}"
            if not label:
                label = f"Column {column}"
            action = menu.addAction(label)
            action.setCheckable(True)
            action.setChecked(not self.targets_table.isColumnHidden(column))
            action.toggled.connect(
                lambda checked, c=column: self.targets_table.setColumnHidden(c, not checked),
            )
        menu.exec_(header.mapToGlobal(pos))

    # ------------------------------------------------------------------
    # Small helpers
    # ------------------------------------------------------------------

    def selected_target_node(self) -> Optional["vtkMRMLMarkupsFiducialNode"]:
        """Return the fiducial node stashed on the currently-selected row."""
        row = self.targets_table.currentRow()
        if row is None or row < 0:
            return None
        name_item = self.targets_table.item(row, COLUMN_NAME)
        if name_item is None:
            return None
        return name_item.data(qt.Qt.UserRole)

    def show_info(self, text: str) -> None:
        slicer.util.infoDisplay(text, windowTitle="Target Selection")

    def show_error(self, text: str) -> None:
        slicer.util.errorDisplay(text, windowTitle="Target Selection")

    def confirm(self, text: str) -> bool:
        message_box = qt.QMessageBox()
        message_box.setIcon(qt.QMessageBox.Question)
        message_box.setWindowTitle("Target Selection")
        message_box.setText(text)
        message_box.setStandardButtons(qt.QMessageBox.Ok | qt.QMessageBox.Cancel)
        message_box.setDefaultButton(qt.QMessageBox.Cancel)
        return message_box.exec_() == qt.QMessageBox.Ok


# ---------------------------------------------------------------------------
# Logic
# ---------------------------------------------------------------------------

class OpenLIFUTargetSelectionLogic(ScriptedLoadableModuleLogic):
    """Business logic for the Target Selection page.

    Every mutation on this class operates on the currently-loaded
    :class:`SlicerOpenLIFUPlanningSession` and its scene state, then
    calls :func:`OpenLIFULib.util.mark_session_dirty` so a subsequent
    Save persists to disk. Nothing here writes to disk itself --
    memory is the source of truth (SlicerOpenLIFU#636).

    Consumable from a scripted test without the widget existing.
    """

    def __init__(self):
        ScriptedLoadableModuleLogic.__init__(self)

    # ------------------------------------------------------------------
    # Session-scoped mutations
    # ------------------------------------------------------------------

    def add_target_from_scene(
        self, fiducial_node: "vtkMRMLMarkupsFiducialNode",
    ) -> None:
        """Register an existing scene fiducial as a session target.

        Idempotent: silently no-ops if the node is already tracked.
        Assigns a distinct palette color, appends to
        ``planning_session.target_nodes``, and rebuilds
        ``planning_session.session.planning_session.targets`` from the
        new set. Marks session dirty.

        Raises ``RuntimeError`` if no PlanningSession is loaded.
        """
        planning_session = self._require_planning_session()
        current = planning_session.get_target_nodes()
        if fiducial_node in current:
            return
        assign_unique_color_to_fiducial(fiducial_node, current)
        planning_session.target_nodes = [*current, fiducial_node]
        self._rebuild_openlifu_targets(planning_session)
        self._persist_planning_session_changes(planning_session)
        mark_session_dirty()
        logging.info(
            "TargetSelection: added target %s",
            fiducial_to_openlifu_point_id(fiducial_node),
        )

    def import_target_from_disk(self, path: Path) -> None:
        """Load a fiducial file (.mrk.json / .fcsv) and register the
        first fiducial in it as a target.

        Raises ``RuntimeError`` if the load produces no fiducial node.
        """
        loaded = slicer.util.loadMarkups(str(path), returnNode=True)
        # ``loadMarkups(returnNode=True)`` historically returns
        # (success, node) but the exact tuple shape has varied.
        if isinstance(loaded, tuple):
            success, node = (loaded + (None, None))[:2]
        else:
            node, success = loaded, loaded is not None
        if not success or node is None:
            raise RuntimeError(f"Failed to load markups from {path}")
        # Trim to a single control point -- extra control points on an
        # imported markup are ignored for target purposes.
        if node.GetNumberOfControlPoints() > 1:
            for _ in range(node.GetNumberOfControlPoints() - 1):
                node.RemoveNthControlPoint(node.GetNumberOfControlPoints() - 1)
        node.SetMaximumNumberOfControlPoints(1)
        self.add_target_from_scene(node)

    def rename_target(self, target_id: str, new_label: str) -> None:
        """Update a target's user-visible display label.

        Writes the control point label on the fiducial node; leaves the
        node name (== openlifu Point id) alone so
        ``virtual_fit_results`` / ``pre_solutions`` refs stay valid.
        Empty labels are ignored to avoid unlabelled cells sneaking into
        the table.
        """
        if not new_label:
            return
        node = self._find_target_node(target_id)
        if node is None or node.GetNumberOfControlPoints() < 1:
            return
        if node.GetNthControlPointLabel(0) == new_label:
            return
        node.SetNthControlPointLabel(0, new_label)
        planning_session = self._require_planning_session()
        self._rebuild_openlifu_targets(planning_session)
        self._persist_planning_session_changes(planning_session)
        mark_session_dirty()

    def move_target(
        self, target_id: str, ras_position: "tuple[float, float, float]",
    ) -> None:
        """Update a target's RAS coordinates.

        Also clears any pre-existing VF-approval flags for this target
        in the in-memory ``virtual_fit_results`` dict -- a moved target
        can no longer count as "approved-at-old-position". Marks dirty.
        """
        node = self._find_target_node(target_id)
        if node is None or node.GetNumberOfControlPoints() < 1:
            return
        current = tuple(node.GetNthControlPointPosition(0))
        if all(abs(a - b) < 1e-6 for a, b in zip(current, ras_position)):
            return
        node.SetNthControlPointPosition(0, *ras_position)
        planning_session = self._require_planning_session()
        self._revoke_vf_approvals_for_target(planning_session, target_id)
        self._rebuild_openlifu_targets(planning_session)
        self._persist_planning_session_changes(planning_session)
        mark_session_dirty()

    def remove_target(
        self, fiducial_node: "vtkMRMLMarkupsFiducialNode",
    ) -> None:
        """Remove a target from the session and the scene.

        Also drops any ``virtual_fit_results`` entry keyed by this
        target from the in-memory PlanningSession dict -- a target
        that no longer exists cannot host a VF result. Marks dirty.
        """
        planning_session = self._require_planning_session()
        target_id = fiducial_to_openlifu_point_id(fiducial_node)
        current = planning_session.get_target_nodes()
        if fiducial_node not in current:
            return
        planning_session.target_nodes = [
            n for n in current if n is not fiducial_node
        ]
        try:
            slicer.mrmlScene.RemoveNode(fiducial_node)
        except Exception:  # noqa: BLE001
            pass
        # Prune the openlifu-side virtual_fit_results dict too. Solutions
        # and TT / photoscan cascades come with those pages -- for now
        # this page's cascade footprint is limited to VF entries the
        # PlanningSession JSON itself carries.
        wrapper = planning_session.session
        if target_id in wrapper.planning_session.virtual_fit_results:
            del wrapper.planning_session.virtual_fit_results[target_id]
        planning_session.session = wrapper
        self._rebuild_openlifu_targets(planning_session)
        self._persist_planning_session_changes(planning_session)
        mark_session_dirty()
        logging.info("TargetSelection: removed target %s", target_id)

    # ------------------------------------------------------------------
    # Scene-only helpers (do not touch the session or dirty flag)
    # ------------------------------------------------------------------

    def sync_targets_from_scene(self) -> bool:
        """Rebuild the openlifu Session's ``targets`` from the scene
        fiducials and persist the change.

        Called on Done (exit edit mode): if the user dragged a fiducial
        in 3D but didn't type into the R / A / S cells, only the
        fiducial has the new position -- the openlifu Session's
        ``targets`` list is still stale. This method pushes the current
        fiducial state into the openlifu Session.

        Returns True iff anything actually changed. Marks dirty in that
        case. Idempotent when nothing has moved.
        """
        planning_session = self._require_planning_session()
        # Snapshot the current openlifu targets to compare against.
        wrapper_before = planning_session.session
        before = [
            (p.id, tuple(p.position), p.name)
            for p in wrapper_before.planning_session.targets
        ]
        self._rebuild_openlifu_targets(planning_session)
        wrapper_after = planning_session.session
        after = [
            (p.id, tuple(p.position), p.name)
            for p in wrapper_after.planning_session.targets
        ]
        if before == after:
            return False
        self._persist_planning_session_changes(planning_session)
        mark_session_dirty()
        return True

    def toggle_visibility(
        self, fiducial_node: "vtkMRMLMarkupsFiducialNode", visible: bool,
    ) -> None:
        """Set the target fiducial's display-node visibility.

        A pure scene mutation -- visibility is not persisted to the
        openlifu Point, so this does not mark the session dirty.
        """
        display_node = fiducial_node.GetDisplayNode()
        if display_node is None:
            return
        display_node.SetVisibility(visible)

    def jump_to_target(
        self, fiducial_node: "vtkMRMLMarkupsFiducialNode",
    ) -> None:
        """Snap all three slice views to the target's RAS position."""
        if fiducial_node.GetNumberOfControlPoints() < 1:
            return
        position = fiducial_node.GetNthControlPointPosition(0)
        slicer.modules.markups.logic().JumpSlicesToLocation(
            position[0], position[1], position[2], True,
        )

    # ------------------------------------------------------------------
    # Private
    # ------------------------------------------------------------------

    def _require_planning_session(self):
        """Return the loaded PlanningSession or raise."""
        planning_session = get_app_state().loaded_planning_session
        if planning_session is None:
            raise RuntimeError("No planning session is loaded.")
        return planning_session

    def _find_target_node(
        self, target_id: str,
    ) -> Optional["vtkMRMLMarkupsFiducialNode"]:
        """Find a session target by its openlifu Point id.

        Uses the pack's ``target_nodes`` list rather than scene lookup so
        loose scene fiducials with the same name don't get matched.
        """
        planning_session = self._require_planning_session()
        for node in planning_session.get_target_nodes():
            if fiducial_to_openlifu_point_id(node) == target_id:
                return node
        return None

    def _rebuild_openlifu_targets(self, planning_session) -> None:
        """Rewrite ``planning_session.session.planning_session.targets``
        from the current fiducial nodes.

        Every mutation calls this so the openlifu Point list stays in
        sync with the scene throughout the edit session; :func:`save_loaded_session`
        can then serialise the up-to-date list without any pre-save
        sync step.

        Uses the "capture wrapper, mutate, write wrapper back" pattern
        because ``@parameterPack`` fields with custom serializers
        deserialise-on-read but do not re-serialise on nested-attribute
        mutation. Without the wrapper reassignment the mutation lives
        on an ephemeral wrapper and evaporates by the time
        :func:`save_loaded_session` re-reads the pack --
        SlicerOpenLIFU#641. The caller must additionally reassign the
        pack to the app state (see :meth:`_persist_planning_session_changes`).
        """
        target_points = []
        for node in planning_session.get_target_nodes():
            try:
                target_points.append(fiducial_to_openlifu_point(node))
            except Exception:  # noqa: BLE001
                logging.exception(
                    "TargetSelection: could not convert fiducial to openlifu Point",
                )
        wrapper = planning_session.session
        wrapper.planning_session.targets = target_points
        planning_session.session = wrapper

    def _persist_planning_session_changes(self, planning_session) -> None:
        """Force a full pack re-serialisation into the app state.

        Every mutation on this Logic class ends with a call here so the
        parameter pack's on-disk representation stays consistent with
        the in-memory mutations (SlicerOpenLIFU#641).

        Mirror of the legacy
        ``parameter_node.loaded_session = session`` pattern
        (``OpenLIFUDataLogic.update_underlying_openlifu_session``).
        Without this, mutations to ``target_nodes`` DO persist (list
        field with a built-in serializer) but mutations to
        ``session.planning_session.targets`` (nested attribute on a
        custom-serialised wrapper) get lost.
        """
        state = get_app_state()
        state.loaded_planning_session = planning_session

    def _revoke_vf_approvals_for_target(
        self, planning_session, target_id: str,
    ) -> None:
        """Flip every VF-approval flag for ``target_id`` to False in the
        in-memory ``virtual_fit_results`` dict.

        Called when a target moves so an "approved at old position" VF
        result doesn't silently become the "approved at new position"
        one. Full VF cascade (delete VF nodes, revoke TT approvals, drop
        solutions) will land with the Virtual Fit page.

        Uses the wrapper reassignment pattern for the same reason as
        :meth:`_rebuild_openlifu_targets` (SlicerOpenLIFU#641).
        """
        wrapper = planning_session.session
        entries = wrapper.planning_session.virtual_fit_results.get(target_id)
        if not entries:
            return
        wrapper.planning_session.virtual_fit_results[target_id] = [
            (False, transform) for _approved, transform in entries
        ]
        planning_session.session = wrapper


# ---------------------------------------------------------------------------
# Dialogs
# ---------------------------------------------------------------------------

class ImportTargetDialog(qt.QDialog):
    """Import-target dialog.

    Two tabs, matching the legacy Pre-Planning import flow:

    * **From scene**: a list of fiducial nodes currently in the scene
      that aren't already this session's targets. Pick one, OK.
    * **From file**: a file picker for ``.mrk.json`` / ``.fcsv``.

    On accept, exposes ``imported_node`` (the scene fiducial) or
    ``imported_path`` (the file path). Exactly one of these is set;
    the caller inspects both and calls the appropriate Logic method.

    Single call site (this page's ``on_import_button_clicked``), so
    the dialog lives in-file per ``docs/coding-standards.md`` rule 7.
    """

    def __init__(
        self,
        scene_candidates: List["vtkMRMLMarkupsFiducialNode"],
        parent=None,
    ):
        super().__init__(parent)
        self.setWindowTitle("Import Target")
        self.imported_node: Optional["vtkMRMLMarkupsFiducialNode"] = None
        self.imported_path: Optional[Path] = None
        self._scene_candidates = list(scene_candidates)

        outer = qt.QVBoxLayout(self)
        self.tabs = qt.QTabWidget()
        self.tabs.addTab(self._build_scene_tab(), "From scene")
        self.tabs.addTab(self._build_file_tab(), "From file…")
        outer.addWidget(self.tabs, 1)

        buttons = make_ok_cancel_button_box(ok_label="Import")
        buttons.accepted.connect(self.on_ok_clicked)
        buttons.rejected.connect(self.reject)
        outer.addWidget(buttons)

    def _build_scene_tab(self) -> qt.QWidget:
        tab = qt.QWidget()
        layout = qt.QVBoxLayout(tab)
        hint = qt.QLabel(
            "Existing scene fiducials that are not already registered as "
            "targets on this session:"
        )
        hint.setWordWrap(True)
        hint.setStyleSheet("color: #666;")
        layout.addWidget(hint)
        self.scene_list = qt.QListWidget()
        self.scene_list.setMinimumHeight(160)
        for candidate in self._scene_candidates:
            label = (
                candidate.GetNthControlPointLabel(0)
                if candidate.GetNumberOfControlPoints() >= 1 else ""
            )
            display = label or candidate.GetName()
            item = qt.QListWidgetItem(display)
            item.setData(qt.Qt.UserRole, candidate)
            self.scene_list.addItem(item)
        # Double-click a row is a natural "Import" shortcut.
        self.scene_list.itemDoubleClicked.connect(lambda *_: self.on_ok_clicked())
        layout.addWidget(self.scene_list, 1)
        return tab

    def _build_file_tab(self) -> qt.QWidget:
        tab = qt.QWidget()
        layout = qt.QVBoxLayout(tab)
        hint = qt.QLabel(
            "Load a fiducial from a Slicer .mrk.json or .fcsv file."
        )
        hint.setWordWrap(True)
        hint.setStyleSheet("color: #666;")
        layout.addWidget(hint)
        row = qt.QHBoxLayout()
        self.path_edit = qt.QLineEdit()
        self.path_edit.setPlaceholderText("Choose a file…")
        row.addWidget(self.path_edit, 1)
        browse_button = qt.QPushButton("Browse…")
        browse_button.clicked.connect(self.on_browse_clicked)
        row.addWidget(browse_button)
        layout.addLayout(row)
        layout.addStretch(1)
        return tab

    def on_browse_clicked(self, _checked: bool = False) -> None:
        path = qt.QFileDialog.getOpenFileName(
            self, "Choose a fiducial file", "",
            "Fiducials (*.mrk.json *.fcsv);;All files (*.*)",
        )
        if path:
            self.path_edit.text = path

    def on_ok_clicked(self) -> None:
        if self.tabs.currentIndex() == 0:
            item = self.scene_list.currentItem()
            if item is None:
                slicer.util.infoDisplay(
                    "Select a fiducial to import.", windowTitle="Import Target",
                )
                return
            self.imported_node = item.data(qt.Qt.UserRole)
            self.accept()
        else:
            path_text = self.path_edit.text.strip()
            if not path_text:
                slicer.util.infoDisplay(
                    "Choose a fiducial file to import.", windowTitle="Import Target",
                )
                return
            path = Path(path_text)
            if not path.exists():
                slicer.util.errorDisplay(
                    f"File does not exist:\n{path}", windowTitle="Import Target",
                )
                return
            self.imported_path = path
            self.accept()


# ---------------------------------------------------------------------------
# Test stub
# ---------------------------------------------------------------------------

class OpenLIFUTargetSelectionTest(ScriptedLoadableModuleTest):
    """Placeholder. Automated tests land with the Virtual Fit page."""

    def runTest(self) -> None:
        self.delayDisplay("OpenLIFUTargetSelection: no automated tests yet.")
