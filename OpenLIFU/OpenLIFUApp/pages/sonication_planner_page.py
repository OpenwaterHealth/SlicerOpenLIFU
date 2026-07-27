"""Sonication planner page for the OpenLIFU host module.

Extracted from the former standalone ``OpenLIFUSonicationPlanner`` scripted
module during the de-moduling migration (Round 2 of DEMODULING.md). The
Slicer module shell ``OpenLIFUSonicationPlanner/OpenLIFUSonicationPlanner.py``
still exists as a thin adapter so navigation and
``slicer.util.getModuleLogic(...)`` calls keep working until Round 5 folds
page navigation into the host.
"""

from __future__ import annotations

# Standard library imports
import logging
import warnings
from dataclasses import dataclass, fields
from datetime import datetime
import math
from pathlib import Path
from typing import Dict, List, Literal, Optional, Union, Tuple, TYPE_CHECKING, get_origin, get_args

# Third-party imports
import qt
import vtk

# Slicer imports
import slicer
from slicer import vtkMRMLMarkupsFiducialNode, vtkMRMLScalarVolumeNode, vtkMRMLTransformNode
from slicer.ScriptedLoadableModule import (
    ScriptedLoadableModuleWidget,
    ScriptedLoadableModuleLogic,
    ScriptedLoadableModuleTest,
)
from slicer.parameterNodeWrapper import parameterNodeWrapper
from slicer.util import VTKObservationMixin

# OpenLIFULib imports
from OpenLIFULib import (
    BusyCursor,
    check_and_install_kwave_binaries,
    ensure_python_requirements_for_module_enter,
    SlicerOpenLIFUProtocol,
    SlicerOpenLIFUSolution,
    SlicerOpenLIFUSolutionAnalysis,
    SlicerOpenLIFUTransducer,
    fiducial_to_openlifu_point_in_transducer_coords,
    get_active_solution,
    get_app_state,
    label_for_target_id,
    make_xarray_in_transducer_coords_from_volume,
    set_active_solution,
)
from OpenLIFUApp.logic.app_state import get_app_state_signals
from OpenLIFULib.events import SlicerOpenLIFUEvents
from OpenLIFULib.guided_mode_util import GuidedWorkflowMixin
from OpenLIFULib.module_layout import apply_module_layout, navigate_to_page, wire_passive_module_header
from OpenLIFULib.targets import fiducial_to_openlifu_point_id
from OpenLIFULib.user_account_mode_util import UserAccountBanner
from OpenLIFULib.util import (
    active_solution_is_pre_solution,
    create_noneditable_QStandardItem,
    display_errors,
)
from OpenLIFULib.notifications import notify

if TYPE_CHECKING:
    import openlifu
    import openlifu.plan
    import openlifu.xdc
    import xarray
    from OpenLIFUApp.pages.data_page import OpenLIFUDataLogic

#
# OpenLIFUSonicationPlannerParameterNode
#


@parameterNodeWrapper
class OpenLIFUSonicationPlannerParameterNode:
    solution_analysis : Optional[SlicerOpenLIFUSolutionAnalysis] = None


# ---------------------------------------------------------------------------
# New-Solution picker
# ---------------------------------------------------------------------------
#
# The Sonication Planner used to expose a Protocol/Transducer/Volume/Target
# inputs section whose Target combobox doubled as the picker for which
# transducer-transform source (transducer tracking vs. virtual fit) drove the
# compute. That silently competed with the Solutions table for control of the
# live transducer pose, so we dropped the inputs section entirely (SlicerOpenLIFU
# #611). The session pins Protocol/Transducer/Volume unambiguously; Target and
# transform source are now selected via an explicit modal picker enumerating
# each eligible (target x transducer-transform source) combo. See #609 for the
# VF -> pre-solution semantic.


@dataclass(frozen=True)
class _NewSolutionOption:
    """One eligible (target, transducer-transform source) row in the New Solution picker."""

    target_node: vtkMRMLMarkupsFiducialNode
    target_label: str
    target_id: str
    kind: Literal["TT", "VF"]
    transform_node: vtkMRMLTransformNode

    @property
    def is_pre_solution(self) -> bool:
        return self.kind == "VF"

    @property
    def display_text(self) -> str:
        source_label = "Tracked (localization)" if self.kind == "TT" else "Virtual Fit (pre-solution)"
        return f"{self.target_label} \u00b7 {source_label}"


class _NewSolutionPickerDialog(qt.QDialog):
    """Modal picker shown when the New Solution toolbar button is clicked and more
    than one eligible (target x transducer-transform source) option is available."""

    def __init__(self, options: List[_NewSolutionOption], parent: Optional[qt.QWidget] = None):
        super().__init__(parent if parent is not None else slicer.util.mainWindow())
        self.setWindowTitle("New Solution")
        self._options = options
        layout = qt.QVBoxLayout(self)
        layout.addWidget(qt.QLabel(
            "Choose the target and transducer-transform source for the new solution:"
        ))
        self._list = qt.QListWidget()
        for opt in options:
            self._list.addItem(qt.QListWidgetItem(opt.display_text))
        self._list.setCurrentRow(0)
        self._list.itemDoubleClicked.connect(lambda *_: self.accept())
        layout.addWidget(self._list)
        # PythonQt's ``QDialogButtonBox.button(StandardButton)`` returns None, so we can't
        # construct with the standard-buttons mask and then rename the OK button. Instead
        # create the two buttons explicitly and add them with the appropriate roles.
        btns = qt.QDialogButtonBox()
        compute_button = qt.QPushButton("Compute")
        compute_button.setDefault(True)
        btns.addButton(compute_button, qt.QDialogButtonBox.AcceptRole)
        btns.addButton(qt.QPushButton("Cancel"), qt.QDialogButtonBox.RejectRole)
        btns.accepted.connect(self.accept)
        btns.rejected.connect(self.reject)
        layout.addWidget(btns)

    def selected(self) -> Optional[_NewSolutionOption]:
        row = self._list.currentRow
        if row < 0 or row >= len(self._options):
            return None
        return self._options[row]


#
# OpenLIFUSonicationPlannerWidget
#


class OpenLIFUSonicationPlannerWidget(ScriptedLoadableModuleWidget, VTKObservationMixin, GuidedWorkflowMixin):
    """Uses ScriptedLoadableModuleWidget base class, available at:
    https://github.com/Slicer/Slicer/blob/main/Base/Python/slicer/ScriptedLoadableModule.py
    """

    def __init__(self, parent=None) -> None:
        """Called when the user opens the module the first time and the widget is initialized."""
        ScriptedLoadableModuleWidget.__init__(self, parent)
        # Resolve resourcePath() via the OpenLIFU host module (post-5c-2 layout).
        self.moduleName = "OpenLIFU"
        VTKObservationMixin.__init__(self)  # needed for parameter node observation
        self.logic = None
        self._parameterNode = None
        self._parameterNodeGuiTag = None

        self._updating_solution_analysis = False
        """Flag to help prevent recursive event when onParameterNodeModified causes the parameter node to be modified"""
        self._updating_gui_from_sliders = False
        """Flag to prevent recursive updates when setting slider values programmatically."""
        self._refreshing_solutions_table = False
        """Guard flag: True while we are programmatically rebuilding the solutions table so
        that our own cell writes do not re-enter ``_on_solutions_table_item_changed``."""

        # See :func:`OpenLIFULib.util.page_is_entered`: the host's ``_delegate_enter`` /
        # ``_delegate_exit`` toggles this in ``enter()`` / ``exit()`` so cross-page
        # ``dataChanged`` observers can short-circuit when this page is off-screen. Needed
        # because in the custom app ``self.parent`` is a plain QWidget with no ``isEntered``.
        self._entered = False

    def setup(self) -> None:
        """Called when the user opens the module the first time and the widget is initialized."""
        ScriptedLoadableModuleWidget.setup(self)

        # Load widget from .ui file (created by Qt Designer).
        # Additional widgets can be instantiated manually and added to self.layout.
        uiWidget = slicer.util.loadUI(self.resourcePath("UI/OpenLIFUSonicationPlanner.ui"))
        self.layout.addWidget(uiWidget)
        self.uiWidget = uiWidget
        self.ui = slicer.util.childWidgetVariables(uiWidget)

        # Restructure into shared header (read-only) + scrollable body + footer.
        self.module_header = apply_module_layout(
            uiWidget, ui_namespace=self.ui, header_read_only=True
        )

        # Set scene in MRML widgets. Make sure that in Qt designer the top-level qMRMLWidget's
        # "mrmlSceneChanged(vtkMRMLScene*)" signal in is connected to each MRML widget's.
        # "setMRMLScene(vtkMRMLScene*)" slot.
        uiWidget.setMRMLScene(slicer.mrmlScene)

        # Use the single shared logic instance created by the host module
        # (``OpenLIFULogic.__init__``) so callback subscribers registered
        # elsewhere fire when this widget mutates logic state.
        self.logic = slicer.util.getModuleLogic("OpenLIFU").sonication_planner_logic

        # Create and set solution analysis table models
        self.globalAnalysisTableModel = qt.QStandardItemModel() # analysis metrics that are for the whole solution, i.e. over all focus points
        self.ui.globalAnalysisTableView.setModel(self.globalAnalysisTableModel)

        # User-account status is now shown by the shared header inserted
        # by ``apply_module_layout`` above; no per-module banner needed.

        # ---- Inject guided mode workflow controls ----

        self.inject_workflow_controls_into_placeholder()

        # ---- Passive header observers ----
        wire_passive_module_header(self, self.module_header)

        # ---- Connections ----

        # These connections ensure that we update parameter node when scene is closed
        self.addObserver(slicer.mrmlScene, slicer.mrmlScene.StartCloseEvent, self.onSceneStartClose)
        self.addObserver(slicer.mrmlScene, slicer.mrmlScene.EndCloseEvent, self.onSceneEndClose)

        # Initialize UI
        self.updateSolutionProgressBar()
        self.updateRenderPNPCheckBox()
        self.updatePNPSliders()
        self.updateSolutionAnalysis()

        # Refresh when AppState changes. Round 5b: Qt signal.
        get_app_state_signals().dataChanged.connect(self.onDataParameterNodeModified)
        
        # This ensures we update the drop down options in the volume and fiducial combo boxes when nodes are added/removed
        self.addObserver(slicer.mrmlScene, slicer.vtkMRMLScene.NodeAddedEvent, self.onNodeAdded)
        self.addObserver(slicer.mrmlScene, slicer.vtkMRMLScene.NodeRemovedEvent, self.onNodeRemoved)


        self.ui.newSolutionButton.clicked.connect(self.onComputeSolutionClicked)
        self.ui.renderPNPCheckBox.toggled.connect(self.onrenderPNPCheckBoxToggled)
        self.ui.showSolutionButton.clicked.connect(self.onShowSelectedClicked)
        self.ui.exportSolutionButton.clicked.connect(self.onExportClicked)
        self.ui.deleteSolutionButton.clicked.connect(self.onDeleteSelectedClicked)

        # Solutions table (SlicerOpenLIFU#611): shows every SolutionInfo record on the loaded
        # Session. Interactive columns are Show (single-row checkbox that drives the active
        # solution), Name (editable in place; mutates the in-memory Solution.name and is
        # persisted on the next session save), and Approved (per-row session-side approval on
        # SolutionInfo.approved -- independent of the safety-gated Solution.approved axis).
        # Double-clicking any row also promotes it to the shown solution.
        self._configure_solutions_table_columns()
        self.ui.solutionsTableWidget.itemChanged.connect(self._on_solutions_table_item_changed)
        self.ui.solutionsTableWidget.itemDoubleClicked.connect(self._on_solutions_table_item_double_clicked)
        # The toolbar buttons (Show / Export / Delete) act on the currently selected row, so
        # their enabled state must track selection changes.
        self.ui.solutionsTableWidget.itemSelectionChanged.connect(self._update_solutions_toolbar)

        # Connect PNP sliders
        self.ui.pnpColorSlider.valuesChanged.connect(self.onPnpColorSliderChanged)
        self.ui.pnpOpacitySlider.valueChanged.connect(self.onPnpOpacitySliderChanged)

        self.checkCanComputeSolution()
        self._update_solutions_toolbar()

        # Make sure parameter node is initialized (needed for module reload)
        self.initializeParameterNode()

        # Re-run the analysis refresh now that ``_parameterNode`` is wired.
        # The earlier call at the top of setup no-ops when the parameter
        # node isn't ready yet (see the guard in ``updateSolutionAnalysis``);
        # this second call catches the case where a solution was already
        # loaded (e.g. Data page auto-connected the database) before this
        # page's setup ran (#586).
        self.updateSolutionAnalysis()

        self.updateWorkflowControls()

        # Populate the Solutions table + "Now showing" label from initial app state.
        # ``onDataParameterNodeModified`` will handle subsequent refreshes.
        self._refresh_solutions_table()
        self._update_now_showing_label()

    def cleanup(self) -> None:
        """Called when the application closes and the module widget is destroyed."""
        try:
            get_app_state_signals().dataChanged.disconnect(self.onDataParameterNodeModified)
        except Exception:  # noqa: BLE001
            pass
        self.removeObservers()

    def enter(self) -> None:
        """Called each time the user opens this module."""
        ensure_python_requirements_for_module_enter()
        self._entered = True
        # Make sure parameter node exists and observed
        self.initializeParameterNode()
        # Full reconstruction from current app state. This subsumes the previous piecemeal
        # calls to updateWorkflowControls / apply_module_view_state and adds the
        # Solutions-table / analysis-header / render-PNP-checkbox refreshes that were
        # previously only fired through the ``dataChanged`` observer -- now that that
        # observer is guarded on ``page_is_entered``, ``enter()`` is the source of truth
        # for first-render state on this page.
        self._refresh_from_app_state()

        # Default-on Render PNP whenever a solution is already loaded on entry.
        if (
            get_active_solution() is not None
            and not self.ui.renderPNPCheckBox.checked
        ):
            self.ui.renderPNPCheckBox.checked = True  # triggers onrenderPNPCheckBoxToggled -> render_pnp

    def exit(self) -> None:
        """Called each time the user opens a different module."""
        self._entered = False
        # Do not react to parameter node changes (GUI will be updated when the user enters into the module)
        if self._parameterNode:
            self._parameterNode.disconnectGui(self._parameterNodeGuiTag)
            self._parameterNodeGuiTag = None

    def onSceneStartClose(self, caller, event) -> None:
        """Called just before the scene is closed."""
        # Parameter node will be reset, do not use it anymore
        self.setParameterNode(None)

    def onSceneEndClose(self, caller, event) -> None:
        """Called just after the scene is closed."""
        # If this module is shown while the scene is closed then recreate a new parameter node immediately
        from OpenLIFULib.util import page_is_entered
        if page_is_entered(self):
            self.initializeParameterNode()

    def initializeParameterNode(self) -> None:
        """Ensure parameter node exists and observed."""
        # Parameter node stores all user choices in parameter values, node selections, etc.
        # so that when the scene is saved and reloaded, these settings are restored.

        self.setParameterNode(self.logic.getParameterNode())

    def setParameterNode(self, inputParameterNode: Optional[OpenLIFUSonicationPlannerParameterNode]) -> None:
        """
        Set and observe parameter node.
        Observation is needed because when the parameter node is changed then the GUI must be updated immediately.
        """

        if self._parameterNode:
            self._parameterNode.disconnectGui(self._parameterNodeGuiTag)

        self._parameterNode = inputParameterNode
        if self._parameterNode:
            # Note: in the .ui file, a Qt dynamic property called "SlicerParameterName" is set on each
            # ui element that needs connection.
            self._parameterNodeGuiTag = self._parameterNode.connectGui(self.ui)
            self.addObserver(self._parameterNode, vtk.vtkCommand.ModifiedEvent, self.onParameterNodeModified)

    def checkCanComputeSolution(self, caller=None, event=None) -> None:
        """Enable/disable the New Solution toolbar button.

        Enabled iff there is at least one eligible ``(target, transducer-transform source)``
        option on the loaded session. The picker enumerates the eligible options when the
        button is clicked; when only one is eligible the picker is auto-skipped (see
        ``onComputeSolutionClicked``).
        """
        state = get_app_state()
        session = state.loaded_session if state is not None else None
        if session is None:
            self.ui.newSolutionButton.enabled = False
            self.ui.newSolutionButton.setToolTip("Load a session before computing a solution.")
            return
        if not self._build_new_solution_options():
            self.ui.newSolutionButton.enabled = False
            self.ui.newSolutionButton.setToolTip(
                "No approved transducer-transform results (transducer tracking or virtual fit) "
                "are available on this session."
            )
            return
        self.ui.newSolutionButton.enabled = True
        self.ui.newSolutionButton.setToolTip(
            "Compute a new sonication solution against a target and an approved transducer pose."
        )

    @vtk.calldata_type(vtk.VTK_OBJECT)
    def onNodeRemoved(self, caller, event, node : slicer.vtkMRMLNode) -> None:
        """ Update volume and target combo boxes when nodes are added to the scene"""
        if node.IsA('vtkMRMLMarkupsFiducialNode'):
            with warnings.catch_warnings():
                warnings.simplefilter("ignore") # if the observer doesn't exist, then no problem we don't need to see the warning.
                self.unwatch_fiducial_node(node)
        self.updateInputOptions()

    @vtk.calldata_type(vtk.VTK_OBJECT)
    def onNodeAdded(self, caller, event, node : slicer.vtkMRMLNode) -> None:
        """ Update volume and target combo boxes when nodes are removed from the scene"""
        if node.IsA('vtkMRMLMarkupsFiducialNode'):
            self.watch_fiducial_node(node)
        self.updateInputOptions()

    def updateInputOptions(self):
        """Refresh input-dependent UI state after data-source changes.

        Post-#611 there is no algorithm-input widget on this page; the eligible
        ``(target, transducer-transform source)`` set is queried on demand by
        ``checkCanComputeSolution`` and by the New-Solution picker itself.
        """
        self.checkCanComputeSolution()

    def updateSolutionProgressBar(self):
        """Update the solution progress bar. 0% if there is no existing solution, 100% if there is an existing solution."""
        self.ui.solutionProgressBar.maximum = 1 # (during computation we set maxmimum=0 to put it into an infinite loading animation)

        if get_active_solution() is None:
            self.ui.solutionProgressBar.value = 0
        else:
            self.ui.solutionProgressBar.value = 1

    def updateRenderPNPCheckBox(self):
        if get_active_solution() is None:
            self.ui.renderPNPCheckBox.enabled = False
            self.ui.renderPNPCheckBox.checked = False
            self.ui.renderPNPCheckBox.setToolTip("Compute a solution first to generate a PNP volume that can be visualized")
        else:
            self.ui.renderPNPCheckBox.enabled = True
            self.ui.renderPNPCheckBox.setToolTip("Show the PNP volume in the 3D view with maximum intensity projection")

    def updatePNPSliders(self, caller=None, event=None) -> None:
        """
        Updates the ranges and default values of the PNP color and opacity sliders
        based on the target pressure from the loaded protocol.
        """
        # Disable sliders and labels by default. They will be enabled if all data is valid.
        self.ui.pnpColorSlider.enabled = False
        self.ui.pnpOpacitySlider.enabled = False
        self.ui.pnpColorLabel.enabled = False
        self.ui.pnpOpacityLabel.enabled = False
    
        # Get target pressure from the current protocol with validation checks.
        data_parameter_node = get_app_state()
        if not data_parameter_node:
            return
    
        solution_wrapper = get_active_solution()
        if not solution_wrapper or not solution_wrapper.solution or not solution_wrapper.solution.solution:
            return
    
        solution_openlifu = solution_wrapper.solution.solution
        protocols = data_parameter_node.loaded_protocols
        if not protocols or solution_openlifu.protocol_id not in protocols:
            return
    
        protocol_openlifu= protocols[solution_openlifu.protocol_id].protocol
        if not protocol_openlifu or not protocol_openlifu.focal_pattern or not hasattr(protocol_openlifu.focal_pattern, 'target_pressure'):
            return
    
        target_pressure = protocol_openlifu.focal_pattern.target_pressure
        if not isinstance(target_pressure, (int, float)) or target_pressure <= 0:
            return

        pnp_volume_node: "vtkMRMLScalarVolumeNode" = self.logic.get_pnp()
        if not pnp_volume_node:
            return
        
        max_pnp_in_array = pnp_volume_node.GetImageData().GetPointData().GetScalars().GetRange()[1]
    
        # If all checks passed, enable the UI elements.
        self.ui.pnpColorSlider.enabled = True
        self.ui.pnpOpacitySlider.enabled = True
        self.ui.pnpColorLabel.enabled = True
        self.ui.pnpOpacityLabel.enabled = True
    
        # Block slider signals to prevent premature updates.
        self._updating_gui_from_sliders = True
        self.ui.pnpColorSlider.blockSignals(True)
        self.ui.pnpOpacitySlider.blockSignals(True)
    
        # Configure the color slider (double-handled).
        N_STEPS = 200.
        self.ui.pnpColorSlider.minimum = 0
        self.ui.pnpColorSlider.maximum = target_pressure * 1.5
        self.ui.pnpColorSlider.minimumValue = 0
        self.ui.pnpColorSlider.maximumValue = target_pressure
        self.ui.pnpColorSlider.singleStep = (target_pressure - 0) / N_STEPS
    
        # Configure the opacity slider (single-handled).
        self.ui.pnpOpacitySlider.minimum = 0
        self.ui.pnpOpacitySlider.maximum = max_pnp_in_array
        self.ui.pnpOpacitySlider.value = 0.1 * target_pressure
        self.ui.pnpOpacitySlider.singleStep = (max_pnp_in_array - 0) / N_STEPS
    
        # Unblock signals.
        self.ui.pnpColorSlider.blockSignals(False)
        self.ui.pnpOpacitySlider.blockSignals(False)
        self._updating_gui_from_sliders = False

        # Set up thresholding and following volume display node
        pnp_volume_node.GetDisplayNode().SetAutoWindowLevel(0)
        pnp_volume_node.GetDisplayNode().SetApplyThreshold(1)

        # Manually trigger updates to apply the new default values.
        self.onPnpColorSliderChanged(self.ui.pnpColorSlider.minimumValue, self.ui.pnpColorSlider.maximumValue)
        self.onPnpOpacitySliderChanged(self.ui.pnpOpacitySlider.value)

    def onDataParameterNodeModified(self, caller=None, event=None) -> None:
        # Cross-page fanout guard: only refresh when the Sonication Planner is the active
        # page. Session load in Data, TT approvals in TL, target edits in PrePlanning etc.
        # all fire this observer; we pick them up next time the user re-enters this page.
        from OpenLIFULib.util import page_is_entered
        if not page_is_entered(self):
            return
        self._refresh_from_app_state()

    def _refresh_from_app_state(self) -> None:
        """Rebuild all app-state-derived UI on this page.

        Single entry point used by ``enter()`` and by ``onDataParameterNodeModified``
        (the latter is guarded on ``isEntered`` so it only fires when this page is
        actually on screen). Consolidates the previous per-observer fanout so both
        entry paths update the same slice of state.
        """
        self.updateInputOptions()
        self.updateSolutionProgressBar()
        self.updateRenderPNPCheckBox()
        self.updatePNPSliders()

        if get_active_solution() is None:
            self.logic.getParameterNode().solution_analysis = None

        self.updateWorkflowControls()
        # The Solutions table and its accompanying "Now showing" label mirror app state
        # (Session.solutions + loaded_solutions + active_solution_id) (#611).
        self._refresh_solutions_table()
        self._update_now_showing_label()
        self._update_solutions_toolbar()
        self._update_analysis_collapsible_label()
        # Snap the transducer pose to the active Solution's array_transform (#622) and set
        # the derived photoscan visibility. Safe to call unconditionally here because this
        # method only runs from ``enter()`` (page is on screen) or from the guarded observer.
        from OpenLIFULib.view_state import apply_module_view_state, SONICATION_PLANNER
        apply_module_view_state(SONICATION_PLANNER)

    def watch_fiducial_node(self, node:vtkMRMLMarkupsFiducialNode):
        """Add observers so that point-list changes in this fiducial node are tracked by the module."""
        self.addObserver(node,slicer.vtkMRMLMarkupsNode.PointAddedEvent,self.onPointAddedOrRemoved)
        self.addObserver(node,slicer.vtkMRMLMarkupsNode.PointRemovedEvent,self.onPointAddedOrRemoved)
        self.addObserver(node,SlicerOpenLIFUEvents.TARGET_NAME_MODIFIED_EVENT,self.onTargetNameModified)

    def unwatch_fiducial_node(self, node:vtkMRMLMarkupsFiducialNode):
        """Un-does watch_fiducial_node; see watch_fiducial_node."""
        self.removeObserver(node,slicer.vtkMRMLMarkupsNode.PointAddedEvent,self.onPointAddedOrRemoved)
        self.removeObserver(node,slicer.vtkMRMLMarkupsNode.PointRemovedEvent,self.onPointAddedOrRemoved)

    def onPointAddedOrRemoved(self, caller, event):
        self.updateInputOptions()

    def onTargetNameModified(self, caller, event):
        self.updateInputOptions()

    @display_errors
    def onComputeSolutionClicked(self, checked: bool):
        """Show the New Solution picker (auto-skipped when only one option is on offer),
        then compute the solution against the chosen (target, transducer-transform source).

        Options are enumerated from the loaded session's approved transducer transforms:
        one row per session target for TT (when the session has any approved TT result), plus
        one row per session target that has an approved VF result (VF rows are suppressed in
        kiosk / user mode; see #609). See ``_build_new_solution_options``.
        """
        if not check_and_install_kwave_binaries():
            raise RuntimeError("Cannot find kwave binaries required to compute sonication solutions.")
        state = get_app_state()
        session = state.loaded_session if state is not None else None
        if session is None:
            raise RuntimeError("Cannot compute a new solution: no session is loaded.")

        options = self._build_new_solution_options()
        if not options:
            slicer.util.errorDisplay(
                "No approved transducer transforms are available on this session. Please "
                "approve at least one transducer-tracking result (or, outside kiosk mode, "
                "one virtual-fit result) before computing a solution.",
                "No approved transforms",
            )
            return

        # Kiosk auto-skip: when only one option is on offer -- typically because VF rows have
        # been filtered out for user mode and only a single target has an approved TT -- take
        # it directly without prompting (per the #611 clarification).
        if len(options) == 1:
            chosen = options[0]
        else:
            dialog = _NewSolutionPickerDialog(options, parent=slicer.util.mainWindow())
            if not dialog.exec_():
                return
            chosen = dialog.selected()
            if chosen is None:
                return

        self._compute_solution_for_option(chosen)

    def _build_new_solution_options(self) -> List[_NewSolutionOption]:
        """Enumerate eligible ``(target, transducer-transform source)`` rows.

        Rules:
          * TT rows: one per session target when the session has any approved
            transducer-tracking result. The pose node is the approved
            transducer_to_volume TT result node (there is at most one under the
            one-approval rule).
          * VF rows: one per session target that has an approved virtual-fit
            result (highest-ranked approved VF is used as the pose node).
            Suppressed in kiosk / user mode, since VF-derived solutions are the
            deviation-from-standard flow (#609).
        """
        from OpenLIFULib.kiosk_util import get_user_mode
        from OpenLIFULib.transducer_tracking_results import (
            get_transducer_tracking_result_nodes_in_scene,
        )
        from OpenLIFULib.virtual_fit_results import get_virtual_fit_result_nodes

        state = get_app_state()
        session = state.loaded_session if state is not None else None
        if session is None:
            return []
        target_nodes = session.get_target_nodes()
        if not target_nodes:
            return []
        session_id = session.get_session_id()

        # Approved TT result node for this session's transducer_to_volume (at most one under
        # the one-approval rule). We match the view_state pattern (see
        # OpenLIFULib.view_state._find_approved_tt_transducer_node) rather than call it
        # directly since it is underscore-prefixed.
        tt_nodes = list(get_transducer_tracking_result_nodes_in_scene(
            session_id=session_id,
            photoscan_id=None,
        ))
        if session_id is None:
            tt_nodes = [n for n in tt_nodes if n.GetAttribute("TT:sessionID") is None]
        approved_tt_node = next(
            (n for n in tt_nodes if n.GetAttribute("TT:approvalStatus") == "1"),
            None,
        )

        # Approved VF result node per target (best-ranked first).
        approved_vf_by_target: Dict[str, vtkMRMLTransformNode] = {}
        if not get_user_mode():
            for vf_node in get_virtual_fit_result_nodes(
                session_id=session_id,
                approved_only=True,
                sort=True,  # ascending by rank; best first
            ):
                approved_vf_by_target.setdefault(vf_node.GetAttribute("VF:targetID"), vf_node)

        options: List[_NewSolutionOption] = []
        for target_node in target_nodes:
            target_id = fiducial_to_openlifu_point_id(target_node)
            target_label = label_for_target_id(target_id) or target_id
            if approved_tt_node is not None:
                options.append(_NewSolutionOption(
                    target_node=target_node,
                    target_label=target_label,
                    target_id=target_id,
                    kind="TT",
                    transform_node=approved_tt_node,
                ))
            if target_id in approved_vf_by_target:
                options.append(_NewSolutionOption(
                    target_node=target_node,
                    target_label=target_label,
                    target_id=target_id,
                    kind="VF",
                    transform_node=approved_vf_by_target[target_id],
                ))
        return options

    def _compute_solution_for_option(self, option: _NewSolutionOption) -> None:
        """Snap the live transducer to the option's pose, then run the compute.

        Shared entry point for the picker path and the module Test class so both go through
        exactly the same pose-snap + compute sequence.
        """
        state = get_app_state()
        session = state.loaded_session if state is not None else None
        if session is None:
            raise RuntimeError("Cannot compute a new solution: no session is loaded.")
        # Snap the live transducer to the chosen pose so the compute (and the PNP
        # visualization that follows) matches what the user picked.
        live_tx = session.get_transducer()
        live_tx.set_current_transform_to_match_transform_node(option.transform_node)

        # Hide any PNP that was previously being displayed for the outgoing active solution
        # before compute runs; the outgoing solution's volume is about to stop being active.
        self.logic.hide_pnp()

        with BusyCursor():
            try:
                self.ui.solutionProgressBar.maximum = 0
                slicer.app.processEvents()
                new_solution, _analysis = self.logic.computeSolution(
                    session.volume_node,
                    option.target_node,
                    live_tx,
                    session.get_protocol(),
                    pre_solution=option.is_pre_solution,
                )
            finally:
                self.updateSolutionProgressBar()

        # Route through the single atomic activation entry point so the analysis table,
        # the transducer pose, and the PNP render all switch together. ``computeSolution``
        # already flipped ``active_solution_id`` (via ``set_solution``) and installed the
        # planner-parameter-node analysis; ``_activate_solution`` here is idempotent w.r.t.
        # those and additionally renders the PNP (force True on compute so the user sees
        # the just-computed volume immediately).
        self._activate_solution(new_solution.solution.solution.id, render_pnp_override=True)
        self.updateWorkflowControls()

    def onrenderPNPCheckBoxToggled(self, checked:bool):
        if checked:
            self.logic.render_pnp()
            # render_pnp() / volume-rendering node creation can reset the registered
            # photoscan model's opacity back to 1.0; re-apply the module view state
            # so it stays at the 25% used in Sonication Planner / Control.
            from OpenLIFULib.view_state import apply_module_view_state, SONICATION_PLANNER
            apply_module_view_state(SONICATION_PLANNER)
        else:
            self.logic.hide_pnp()

    def onPnpColorSliderChanged(self, new_min_val: float, new_max_val: float) -> None:
        """Called when the PNP color slider values are changed."""
        pnp_volume_node: "vtkMRMLScalarVolumeNode" = self.logic.get_pnp()
        pnp_volume_node.GetDisplayNode().SetWindowLevelMinMax(new_min_val, new_max_val)

        vrDisplayNode = slicer.modules.volumerendering.logic().GetFirstVolumeRenderingDisplayNode(pnp_volume_node)
        if vrDisplayNode is not None and not vrDisplayNode.GetFollowVolumeDisplayNode():
          vrDisplayNode.SetFollowVolumeDisplayNode(1)
        if vrDisplayNode is not None and vrDisplayNode.GetIgnoreVolumeDisplayNodeThreshold():
          vrDisplayNode.SetIgnoreVolumeDisplayNodeThreshold(0)


    def onPnpOpacitySliderChanged(self, new_min_val: float) -> None:
        """Called when the PNP opacity slider value is changed."""
        pnp_volume_node: "vtkMRMLScalarVolumeNode" = self.logic.get_pnp()
        pnp_volume_node.GetDisplayNode().SetThreshold(new_min_val, self.ui.pnpOpacitySlider.maximum)

        vrDisplayNode = slicer.modules.volumerendering.logic().GetFirstVolumeRenderingDisplayNode(pnp_volume_node)
        if vrDisplayNode is not None and not vrDisplayNode.GetFollowVolumeDisplayNode():
          vrDisplayNode.SetFollowVolumeDisplayNode(1)
        if vrDisplayNode is not None and vrDisplayNode.GetIgnoreVolumeDisplayNodeThreshold():
          vrDisplayNode.SetIgnoreVolumeDisplayNodeThreshold(0)


    def deleteSolutionAndSolutionAnalysisIfAny(self, reason:str):
        """Delete the solution in the data module and the solution analysis in
        the sonication planner module, and show a message dialog to that effect.
        """
        data_logic : "OpenLIFUDataLogic" = slicer.util.getModuleLogic("OpenLIFU").data_logic
        if self.logic.solution_analysis_exists():
            data_logic.clear_solution(clean_up_scene=True)
            self._parameterNode.solution_analysis = None
            notify(f"Solution deleted:\n{reason}")

    def _update_analysis_collapsible_label(self) -> None:
        """Reflect whether the currently shown solution is a pre-solution in the analysis
        section header. VF-derived (pre-)solutions carry
        ``transducer_transform_source == "virtual_fit"`` on their SolutionInfo record; see
        ``active_solution_is_pre_solution`` and #609."""
        if active_solution_is_pre_solution():
            self.ui.analysisCollapsible.setText("Pre-solution analysis")
        else:
            self.ui.analysisCollapsible.setText("Solution analysis")

    @display_errors
    def onShowSelectedClicked(self, checked: bool) -> None:
        """Promote the selected Solutions-table row to the shown / active solution."""
        sid = self._selected_solution_id()
        if sid is None:
            raise RuntimeError("Cannot show solution: no row is selected in the Solutions table.")
        self._activate_solution(sid)

    def _activate_solution(
        self,
        sid: str,
        render_pnp_override: Optional[bool] = None,
    ) -> None:
        """Atomically switch which Solution is the shown / active one.

        Handles, in order:

        1. Hide the outgoing solution's PNP (resolved via ``get_active_solution().pnp`` BEFORE
           we flip active, so the correct volume is hidden).
        2. Flip ``active_solution_id`` on the app state. The parameter-node ``ModifiedEvent``
           fanout (``onDataParameterNodeModified``) then refreshes the Solutions table, the
           workflow controls, and the transducer pose (via ``apply_module_view_state``).
        3. Install the incoming solution's ``analysis`` on the planner's own parameter node.
           This triggers ``updateSolutionAnalysis`` which populates the analysis table with
           the new content -- previously only the collapsible header updated on switch and
           the table stayed stale (#622-adjacent Show bug).
        4. Render the incoming PNP if desired. The default is "preserve the previous checkbox
           state" (if the user was viewing PNP, keep viewing it for the new solution);
           ``render_pnp_override`` lets callers force a specific state (compute-time forces
           ``True`` so the user immediately sees the just-computed volume).

        Single entry point for both the "New" (compute) path and the "Show" table action, so
        the same state transitions happen the same way regardless of how the user got here.
        """
        state = get_app_state()
        if sid not in state.loaded_solutions:
            raise RuntimeError(f"Cannot activate solution {sid!r}: not in loaded_solutions.")

        # Capture desired PNP visibility before we change any state.
        should_render_pnp = (
            render_pnp_override
            if render_pnp_override is not None
            else self.ui.renderPNPCheckBox.checked
        )

        # (1) Hide the outgoing PNP. ``hide_pnp`` short-circuits if there is no PNP or the
        # active solution has none. It resolves through ``get_active_solution()`` which is
        # still pointing at the OUTGOING solution at this point.
        self.logic.hide_pnp()

        # (2) Flip active. Fires the standard fanout (see onDataParameterNodeModified).
        set_active_solution(sid)

        # (3) Install the incoming solution's analysis on the planner parameter node so the
        # analysis table refreshes. ``updateSolutionAnalysis`` will recompute-and-cache if
        # the incoming's ``analysis`` is None (e.g. legacy solution without a persisted
        # analysis on disk).
        incoming = state.loaded_solutions[sid]
        self.logic.getParameterNode().solution_analysis = incoming.analysis

        # (4) Render the incoming PNP (or explicitly hide, if the caller forced False). Keep
        # the checkbox state consistent with what is actually shown.
        can_render = incoming.pnp is not None
        will_render = should_render_pnp and can_render
        if will_render:
            self.logic.render_pnp()
        else:
            # The outgoing PNP was already hidden in (1); nothing more to hide.
            pass
        if self.ui.renderPNPCheckBox.checked != will_render:
            self.ui.renderPNPCheckBox.blockSignals(True)
            try:
                self.ui.renderPNPCheckBox.checked = will_render
            finally:
                self.ui.renderPNPCheckBox.blockSignals(False)

    @display_errors
    def onDeleteSelectedClicked(self, checked: bool) -> None:
        """Remove the selected row's SolutionInfo from the loaded Session.

        The on-disk solution directory is left in place until the next
        ``save_session``, at which point ``purge_orphaned_solutions`` reconciles the on-disk
        state against ``Session.solutions``. The in-memory ``loaded_solutions`` entry is
        dropped immediately; if the deleted solution was the active one, ``active_solution_id``
        is cleared.
        """
        sid = self._selected_solution_id()
        if sid is None:
            raise RuntimeError("Cannot delete solution: no row is selected in the Solutions table.")
        state = get_app_state()
        loaded_session = state.loaded_session
        if loaded_session is None:
            raise RuntimeError("Cannot delete solution: no session is loaded.")
        session_openlifu = loaded_session.session.session
        # Find the SolutionInfo (needed for the confirm-dialog label; also for a clean
        # error message if the row is somehow stale).
        info = next((si for si in session_openlifu.solutions if si.solution_id == sid), None)
        if info is None:
            raise RuntimeError(f"Cannot delete solution: {sid!r} is not on the loaded session.")

        # Human-friendly label: prefer the loaded openlifu Solution.name over the raw id.
        loaded_solutions = state.loaded_solutions
        label = (
            loaded_solutions[sid].solution.solution.name
            if sid in loaded_solutions
            else sid
        )
        if not slicer.util.confirmYesNoDisplay(
            text=(
                f"Delete solution '{label}' (ID: {sid}) from this session?\n\n"
                "The associated files will be removed from disk the next time the session is saved."
            ),
            windowTitle="Delete solution?",
        ):
            return

        # Drop the SolutionInfo entry. Reassigning the pack fires the write hook so the
        # cross-module observers (and our own ``_refresh_solutions_table``) run.
        session_openlifu.solutions = [
            si for si in session_openlifu.solutions if si.solution_id != sid
        ]
        state.loaded_session = loaded_session

        # Drop the in-memory Solution wrapper and its scene nodes (PNP / intensity volumes).
        # Previously we popped from ``loaded_solutions`` without calling ``clear_nodes()``, so
        # the volumes were orphaned in the scene -- they survived even a full session unload
        # because ``OpenLIFUDataLogic.clear_solutions`` iterates the (now empty)
        # ``loaded_solutions`` dict to find nodes to remove.
        was_active = state.active_solution_id == sid
        popped_solution = None
        if sid in loaded_solutions:
            popped_solution = loaded_solutions.pop(sid)
            state.loaded_solutions = loaded_solutions
        if popped_solution is not None:
            try:
                popped_solution.clear_nodes()
            except Exception as e:  # noqa: BLE001
                logging.warning("Could not remove scene nodes for deleted solution %s: %s", sid, e)

        # If the deleted solution was NOT the active one, we're done -- the Solutions table
        # will refresh via the ``state.loaded_session`` write above.
        if not was_active:
            return

        # Deleted the shown solution. Prefer promoting another loaded solution (via the atomic
        # activation entry point) rather than dropping to "no active solution": that keeps the
        # Sonication Planner page in a consistent shown-solution state for the user, and moves
        # the row-checkmark to the newly-active row via ``_refresh_solutions_table``.
        remaining_ids_in_session_order = [
            si.solution_id for si in session_openlifu.solutions
            if si.solution_id in loaded_solutions
        ]
        if remaining_ids_in_session_order:
            # Activate the first remaining. Force PNP render True so the user immediately sees
            # the promoted solution's volume (same UX as compute-time).
            self._activate_solution(remaining_ids_in_session_order[0], render_pnp_override=True)
        else:
            # No remaining solutions. Hide the (already-cleared) checkbox and drop active.
            # ``hide_pnp`` is a no-op here because the popped solution's PNP volume was already
            # removed above, but we still toggle the checkbox to keep the widget state coherent.
            self.ui.renderPNPCheckBox.blockSignals(True)
            try:
                self.ui.renderPNPCheckBox.checked = False
            finally:
                self.ui.renderPNPCheckBox.blockSignals(False)
            # ``active_solution_id`` is a non-None string field on the parameter node; use the
            # empty-string sentinel documented on ``set_active_solution``.
            set_active_solution("")

    def _selected_solution_id(self) -> Optional[str]:
        """Return the solution id of the currently selected Solutions-table row, or None."""
        tbl = self.ui.solutionsTableWidget
        rows = tbl.selectionModel().selectedRows() if tbl.selectionModel() is not None else []
        if not rows:
            return None
        return self._row_solution_id(rows[0].row())

    def _update_solutions_toolbar(self) -> None:
        """Update enabled state + tooltips of the Solutions toolbar buttons.

        * New   -- driven by ``checkCanComputeSolution`` (input completeness).
        * Show  -- enabled when a non-active row is selected.
        * Export -- enabled when the selected row's solution is loaded.
        * Delete -- enabled when a row is selected.
        """
        sid = self._selected_solution_id()
        state = get_app_state()
        loaded_solutions = state.loaded_solutions if state is not None else {}
        active_id = state.active_solution_id if state is not None else None

        if sid is None:
            self.ui.showSolutionButton.setEnabled(False)
            self.ui.showSolutionButton.setToolTip("Select a solution in the table first")
            self.ui.exportSolutionButton.setEnabled(False)
            self.ui.exportSolutionButton.setToolTip("Select a solution in the table first")
            self.ui.deleteSolutionButton.setEnabled(False)
            self.ui.deleteSolutionButton.setToolTip("Select a solution in the table first")
            return

        # Show
        if sid == active_id:
            self.ui.showSolutionButton.setEnabled(False)
            self.ui.showSolutionButton.setToolTip("The selected solution is already the shown solution")
        else:
            self.ui.showSolutionButton.setEnabled(True)
            self.ui.showSolutionButton.setToolTip(
                "Show the selected solution (make it the active solution driving the analysis, PNP rendering, and hardware send)"
            )

        # Export
        if sid in loaded_solutions:
            self.ui.exportSolutionButton.setEnabled(True)
            self.ui.exportSolutionButton.setToolTip(
                "Export the selected solution to a JSON file (with optional simulation data .nc file)"
            )
        else:
            self.ui.exportSolutionButton.setEnabled(False)
            self.ui.exportSolutionButton.setToolTip(
                "The selected solution is not loaded into memory; cannot export"
            )

        # Delete
        self.ui.deleteSolutionButton.setEnabled(True)
        self.ui.deleteSolutionButton.setToolTip(
            "Delete the selected solution from the session (removed from disk on next session save)"
        )

    @display_errors
    def onExportClicked(self, checked: bool):
        """Export the selected Solutions-table row to a JSON file (and optionally a .nc
        file with the simulation data) chosen by the user."""
        sid = self._selected_solution_id()
        if sid is None:
            raise RuntimeError("Cannot export solution: no row is selected in the Solutions table.")
        state = get_app_state()
        loaded_solutions = state.loaded_solutions
        if sid not in loaded_solutions:
            raise RuntimeError(
                f"Cannot export solution {sid!r}: the underlying Solution object is not loaded."
            )
        solution_openlifu: "openlifu.plan.Solution" = loaded_solutions[sid].solution.solution

        # Build a save dialog with an extra "Also export simulation data"
        # checkbox embedded directly in the file picker.
        initial_dir = slicer.app.defaultScenePath
        safe_id = "".join(
            c if c.isalnum() or c in (" ", "-", "_") else "_"
            for c in solution_openlifu.id
        )
        initial_file = str(Path(initial_dir) / f"{safe_id}.json")

        dialog = qt.QFileDialog(
            slicer.util.mainWindow(),
            "Export Solution",
            initial_file,
            "Solution JSON (*.json);;All Files (*)",
        )
        dialog.setAcceptMode(qt.QFileDialog.AcceptSave)
        dialog.setOption(qt.QFileDialog.DontUseNativeDialog, True)
        dialog.setDefaultSuffix("json")

        export_nc_checkbox = qt.QCheckBox(
            "Also export simulation data (.nc file alongside the .json)"
        )
        export_nc_checkbox.setChecked(True)
        dialog_layout = dialog.layout()
        if isinstance(dialog_layout, qt.QGridLayout):
            dialog_layout.addWidget(
                export_nc_checkbox,
                dialog_layout.rowCount(),
                0,
                1,
                dialog_layout.columnCount(),
            )
        else:
            dialog_layout.addWidget(export_nc_checkbox)

        if not dialog.exec_():
            return
        selected_files = dialog.selectedFiles()
        if not selected_files:
            return

        json_path = Path(selected_files[0])
        if json_path.suffix.lower() != ".json":
            json_path = json_path.with_suffix(".json")

        include_nc = export_nc_checkbox.isChecked()
        # Match openlifu's own naming convention for the companion .nc file
        # (strip everything from the first dot in the json filename and append .nc).
        nc_path: Optional[Path] = None
        if include_nc:
            nc_path = json_path.with_name(json_path.name.split(".")[0] + ".nc")

        # The QFileDialog (non-native) already prompts about overwriting the
        # JSON. We additionally need to check the companion .nc file.
        if nc_path is not None and nc_path.exists():
            if not slicer.util.confirmYesNoDisplay(
                text=f"The file already exists:\n{nc_path}\n\nOverwrite?",
                windowTitle="Overwrite existing file?",
            ):
                return

        with BusyCursor():
            if include_nc:
                solution_openlifu.to_files(json_path, nc_path)
            else:
                json_path.parent.mkdir(parents=True, exist_ok=True)
                json_path.write_text(
                    solution_openlifu.to_json(include_simulation_data=False, compact=False)
                )

        notify(f"Solution exported to:\n{json_path}")

    def onParameterNodeModified(self, caller, event) -> None:
        # ---- Update the solution analysis ----
        if not self._updating_solution_analysis: # prevent recursive observer event
            self._updating_solution_analysis = True
            self.updateSolutionAnalysis()
            self._updating_solution_analysis = False

        # ---- Revoke the solution approval in certain cases ----
        active_solution = get_active_solution()
        if active_solution is not None:
            solution_is_approved = active_solution.is_approved()
            if solution_is_approved and not self.logic.solution_analysis_exists():
                self.logic.toggle_solution_approval()
                notify(f"Solution approval revoked: missing solution analysis!")
            elif solution_is_approved and self.logic.solution_analysis_has_errors():
                self.logic.toggle_solution_approval()
                notify(f"Solution approval revoked: errors in solution analysis!")

    def updateSolutionAnalysis(self) -> None:
        """Update the solution analysis widgets"""

        solution = get_active_solution()

        if solution is None:
            self.clear_solution_analysis_tables() # clear out the table
            self.ui.analysisStackedWidget.setCurrentIndex(0) # set the page to "no solution"
            return

        # ``setup`` calls this method before ``initializeParameterNode`` runs
        # (parameter node is set up last so it can connectGui to fully-swapped
        # widgets). Skip the analysis refresh in that transient window; setup
        # will call updateSolutionAnalysis again through the parameter-node
        # observer once the parameter node is wired (#586).
        if self._parameterNode is None:
            return

        analysis = self._parameterNode.solution_analysis

        if analysis is None: # There exists a solution but no solution analysis (we don't want this to be possible but with manual workflow it might be)
            slicer.util.warningDisplay(
                "There is a solution, but no associated solution analysis. The analysis will be computed now.",
                "Missing analysis",
            )
            analysis = self.logic.compute_analysis_from_solution(solution)
            if analysis is None: # This could happen for example if the user deletes the transducer from the scene after computing the solution
                slicer.util.errorDisplay(
                    "Could not compute analysis because OpenLIFU objects that were used to generate the solution are missing.",
                    "Cannot compute analysis",
                )
                self.clear_solution_analysis_tables()
                self.ui.analysisStackedWidget.setCurrentIndex(2) # set the page to show that this is an error state
                return
            self._parameterNode.solution_analysis = analysis

        self.populate_solution_analysis_table()
        self.ui.analysisStackedWidget.setCurrentIndex(1) # set the page to analysis

    def updateWorkflowControls(self):
        active_solution = get_active_solution()
        if get_app_state().loaded_session is None:
            self.workflow_controls.can_proceed = False
            self.workflow_controls.status_text = "If you are seeing this, guided mode is being run out of order! Load a session to proceed."
        elif active_solution is None:
            self.workflow_controls.can_proceed = False
            self.workflow_controls.status_text = "Compute a sonication solution to proceed."
        elif not active_solution.is_approved():
            self.workflow_controls.can_proceed = False
            self.workflow_controls.status_text = "Approve a sonication solution to proceed."
        else:
            self.workflow_controls.can_proceed = True
            self.workflow_controls.status_text = "Approved sonication solution detected, proceed to the next step."

    # ------------------------------------------------------------------
    # Solutions table (SlicerOpenLIFU#611)
    # ------------------------------------------------------------------
    # The Solutions table is the primary surface for the multi-solution session model. It
    # renders every ``SolutionInfo`` on ``Session.solutions`` as a row and lets the user pick
    # which one is currently shown (drives the analysis section, PNP rendering, and hardware
    # send in the Sonication Control page), edit its user-facing Name, and toggle its
    # session-side Approved flag. Legacy solutions on disk that lack a ``SolutionInfo`` entry
    # on the loaded session are intentionally not shown (see #613).

    #: 0-based column indices, kept in one place so ``_refresh_solutions_table`` and
    #: ``_on_solutions_table_item_changed`` agree.
    _COL_SHOW = 0
    _COL_NAME = 1
    _COL_ID = 2
    _COL_TARGET = 3
    _COL_TYPE = 4
    _COL_PROTOCOL = 5
    _COL_COMPUTED = 6
    _COL_APPROVED = 7

    def _configure_solutions_table_columns(self) -> None:
        """One-time setup of the Solutions table columns, headers, and resize policy."""
        tbl = self.ui.solutionsTableWidget
        tbl.setColumnCount(8)
        tbl.setHorizontalHeaderLabels(
            ["Show", "Name", "ID", "Target", "Type", "Protocol", "Compute time", "Approved"]
        )
        header = tbl.horizontalHeader()
        header.setSectionResizeMode(qt.QHeaderView.ResizeToContents)
        header.setStretchLastSection(False)
        # Let Name stretch so long solution names do not force the panel wider.
        header.setSectionResizeMode(self._COL_NAME, qt.QHeaderView.Stretch)

    def _refresh_solutions_table(self) -> None:
        """Rebuild the Solutions table from app state.

        Data flow:
          * ``Session.solutions`` is the authoritative list of provenance records
            (:class:`openlifu.db.session.SolutionInfo`).
          * ``loaded_solutions`` supplies each row's editable ``Name`` (mirroring the
            openlifu ``Solution.name`` for the same id).
          * ``active_solution_id`` drives the single "Show" checkbox.

        Guarded by ``self._refreshing_solutions_table`` so our own ``setItem`` calls do not
        re-enter :meth:`_on_solutions_table_item_changed`.
        """
        tbl = self.ui.solutionsTableWidget
        self._refreshing_solutions_table = True
        try:
            tbl.clearContents()
            state = get_app_state()
            loaded_session = state.loaded_session
            if loaded_session is None:
                tbl.setRowCount(0)
                return
            session_openlifu = loaded_session.session.session
            loaded_solutions = state.loaded_solutions
            active_id = state.active_solution_id
            rows = list(session_openlifu.solutions)
            tbl.setRowCount(len(rows))
            for row_idx, info in enumerate(rows):
                sid = info.solution_id
                # ``loaded_solutions`` is an ObservedDict wrapping a dict[str, SlicerOpenLIFUSolution].
                slicer_solution = loaded_solutions[sid] if sid in loaded_solutions else None
                name = (
                    slicer_solution.solution.solution.name
                    if slicer_solution is not None
                    else "(unloaded)"
                )
                target_label = label_for_target_id(info.target_id) or info.target_id
                type_label = (
                    "Virtual fit" if info.transducer_transform_source == "virtual_fit"
                    else "Localization"
                )
                computed_label = (
                    info.computed_at.strftime("%Y-%m-%d %H:%M:%S")
                    if info.computed_at is not None
                    else "\u2014"  # em-dash for legacy entries that predate ``computed_at``
                )

                # Show
                show_item = qt.QTableWidgetItem()
                show_item.setFlags(
                    qt.Qt.ItemIsUserCheckable | qt.Qt.ItemIsEnabled | qt.Qt.ItemIsSelectable
                )
                show_item.setCheckState(qt.Qt.Checked if sid == active_id else qt.Qt.Unchecked)
                # Stash the solution id so handlers can recover it from any cell in the row.
                show_item.setData(qt.Qt.UserRole, sid)
                tbl.setItem(row_idx, self._COL_SHOW, show_item)

                # Name (editable when the underlying Solution object is loaded)
                name_item = qt.QTableWidgetItem(name)
                if slicer_solution is None:
                    name_item.setFlags(qt.Qt.ItemIsSelectable | qt.Qt.ItemIsEnabled)
                else:
                    name_item.setFlags(
                        qt.Qt.ItemIsSelectable | qt.Qt.ItemIsEnabled | qt.Qt.ItemIsEditable
                    )
                name_item.setData(qt.Qt.UserRole, sid)
                tbl.setItem(row_idx, self._COL_NAME, name_item)

                # ID
                id_item = qt.QTableWidgetItem(sid)
                id_item.setFlags(qt.Qt.ItemIsSelectable | qt.Qt.ItemIsEnabled)
                tbl.setItem(row_idx, self._COL_ID, id_item)

                # Target
                target_item = qt.QTableWidgetItem(target_label)
                target_item.setFlags(qt.Qt.ItemIsSelectable | qt.Qt.ItemIsEnabled)
                tbl.setItem(row_idx, self._COL_TARGET, target_item)

                # Type
                type_item = qt.QTableWidgetItem(type_label)
                type_item.setFlags(qt.Qt.ItemIsSelectable | qt.Qt.ItemIsEnabled)
                tbl.setItem(row_idx, self._COL_TYPE, type_item)

                # Protocol
                protocol_item = qt.QTableWidgetItem(info.protocol_id)
                protocol_item.setFlags(qt.Qt.ItemIsSelectable | qt.Qt.ItemIsEnabled)
                tbl.setItem(row_idx, self._COL_PROTOCOL, protocol_item)

                # Compute time
                computed_item = qt.QTableWidgetItem(computed_label)
                computed_item.setFlags(qt.Qt.ItemIsSelectable | qt.Qt.ItemIsEnabled)
                tbl.setItem(row_idx, self._COL_COMPUTED, computed_item)

                # Approved (checkbox drives SolutionInfo.approved)
                approved_item = qt.QTableWidgetItem()
                approved_item.setFlags(
                    qt.Qt.ItemIsUserCheckable | qt.Qt.ItemIsEnabled | qt.Qt.ItemIsSelectable
                )
                approved_item.setCheckState(qt.Qt.Checked if info.approved else qt.Qt.Unchecked)
                approved_item.setData(qt.Qt.UserRole, sid)
                tbl.setItem(row_idx, self._COL_APPROVED, approved_item)
        finally:
            self._refreshing_solutions_table = False

    def _row_solution_id(self, row: int) -> Optional[str]:
        """Recover the solution id stashed on the Show cell of the given row, or ``None``."""
        tbl = self.ui.solutionsTableWidget
        sid_item = tbl.item(row, self._COL_SHOW)
        if sid_item is None:
            return None
        sid = sid_item.data(qt.Qt.UserRole)
        return sid or None

    def _on_solutions_table_item_changed(self, item) -> None:
        """Route user edits on interactive columns.

        * Show   -> sets ``active_solution_id`` (single-selection is enforced by the
          resulting table refresh, which rebuilds every row's checkbox from the new id).
          Un-checking the shown row directly is disallowed and reverted.
        * Name   -> mutates the in-memory ``Solution.name`` for the row's solution; the change
          is persisted to disk the next time the user saves the session.
        * Approved -> flips ``SolutionInfo.approved`` for the row's solution on the loaded
          session; also persisted on save.

        Guarded by ``self._refreshing_solutions_table`` to ignore our own ``setItem`` calls.
        """
        if self._refreshing_solutions_table:
            return
        col = item.column()
        row = item.row()
        sid = self._row_solution_id(row)
        if sid is None:
            return
        state = get_app_state()

        if col == self._COL_SHOW:
            if item.checkState() == qt.Qt.Checked:
                # Reassigning ``active_solution_id`` fires the data-parameter-node
                # ModifiedEvent, which triggers ``_refresh_solutions_table`` and clears the
                # checkboxes on all other rows.
                set_active_solution(sid)
            else:
                # There is no "no solution shown" state -- one row must always be checked.
                # Rebuild to revert the visual.
                self._refresh_solutions_table()
            return

        if col == self._COL_NAME:
            loaded_solutions = state.loaded_solutions
            if sid not in loaded_solutions:
                # Shouldn't happen (Name is only editable for loaded rows), but be defensive.
                self._refresh_solutions_table()
                return
            slicer_solution = loaded_solutions[sid]
            new_name = item.text()
            if slicer_solution.solution.solution.name == new_name:
                return
            slicer_solution.solution.solution.name = new_name
            # Re-insert to trigger the parameterNodeWrapper's write hook (in-place mutation
            # of the pack does not fire it; reassigning the whole dict does).
            loaded_solutions[sid] = slicer_solution
            state.loaded_solutions = loaded_solutions
            self._update_now_showing_label()
            return

        if col == self._COL_APPROVED:
            loaded_session = state.loaded_session
            if loaded_session is None:
                self._refresh_solutions_table()
                return
            session_openlifu = loaded_session.session.session
            checked = item.checkState() == qt.Qt.Checked
            for info in session_openlifu.solutions:
                if info.solution_id == sid:
                    if info.approved == checked:
                        return
                    info.approved = checked
                    break
            else:
                self._refresh_solutions_table()
                return
            # Reassign the pack so ``@parameterNodeWrapper`` serializes the mutation.
            state.loaded_session = loaded_session
            return

    def _on_solutions_table_item_double_clicked(self, item) -> None:
        """Double-clicking any cell in a row promotes that row to the shown solution."""
        sid = self._row_solution_id(item.row())
        if sid:
            set_active_solution(sid)

    def _update_now_showing_label(self) -> None:
        """Refresh the "Now showing" info label above the Solution analysis section.

        The Solutions table decouples "which solution is on disk" from "which solution the
        analysis / PNP rendering / hardware-send flow is looking at". This label removes any
        ambiguity by naming the currently shown solution.
        """
        active_solution = get_active_solution()
        if active_solution is None:
            self.ui.nowShowingLabel.setText("No solution shown.")
            return
        sol = active_solution.solution.solution
        self.ui.nowShowingLabel.setText(f"Now showing: {sol.name}   (ID: {sol.id})")

    def clear_solution_analysis_tables(self) -> None:
        """Clear out the solution analysis tables, removing all rows and column headers"""
        self.globalAnalysisTableModel.removeRows(0,self.globalAnalysisTableModel.rowCount())
        self.globalAnalysisTableModel.setColumnCount(0)

    def populate_solution_analysis_table(self) -> None:
        """Fill the solution analysis table models with the information from the current solution analysis.
        Assumes that there is a valid solution analysis, raises error if not.
        """

        analysis = self._parameterNode.solution_analysis
        if analysis is None:
            raise RuntimeError("Cannot populate solution analysis tables because there is no solution analysis.")

        def format_value(val):
            """
            Format numeric values:
            - Floats >= 0.01 → rounded to 2 decimal places
            - Floats < 0.01  → 3 significant digits
            - Non-floats     → converted to string as-is
            """
            if isinstance(val, float):
                return f"{val:.2f}" if abs(val) >= 0.01 else f"{val:.3g}"
            return str(val)

        analysis_openlifu = analysis.analysis
        self.clear_solution_analysis_tables()

        # Extract the DataFrame with the desired columns
        df = analysis_openlifu.to_table()[["Param", "Value", "Units", "Status"]]

        # Set headers
        self.globalAnalysisTableModel.setHorizontalHeaderLabels(df.columns.tolist())

        # Adjust column widths to be more compact
        self.ui.globalAnalysisTableView.setColumnWidth(0, 180)  # Param
        self.ui.globalAnalysisTableView.setColumnWidth(1, 80)  # Value
        self.ui.globalAnalysisTableView.setColumnWidth(2, 80)   # Units
        self.ui.globalAnalysisTableView.setColumnWidth(3, 40)  # Status

        # Increase table view height
        self.ui.globalAnalysisTableView.setMinimumHeight(400)  # adjust as needed

        # Populate the model
        import openlifu.plan.param_constraint

        for _, row in df.iterrows():
            row["Status"] = row["Status"] if row["Status"] else "-"
            items = [create_noneditable_QStandardItem(format_value(cell)) for cell in row]
            self.globalAnalysisTableModel.appendRow(items)#

# Solution computation function using openlifu
#

def compute_solution_openlifu(
        protocol: "openlifu.plan.Protocol",
        transducer:SlicerOpenLIFUTransducer,
        target_node:vtkMRMLMarkupsFiducialNode,
        volume_node:vtkMRMLScalarVolumeNode
    ) -> "Tuple[openlifu.plan.Solution, xarray.DataArray, xarray.DataArray, openlifu.plan.SolutionAnalysis]":
    """Run openlifu beamforming and k-wave simulation

    Returns:
        solution: the generated openlifu Solution
        pnp_aggregated: Peak negative pressure volume, a simulation output. This is max-aggregated over all focus points.
        intensity_aggregated: Time-averaged intensity, a simulation output. This is mean-aggregated over all focus points.
            Note: It should be weighted by the number of times each focus point is focused on, but this functionality is not yet represented by openlifu.
    """
    session = get_app_state().loaded_session
    solution, simulation_result_aggregated, scaled_solution_analysis = protocol.calc_solution(
        transducer=transducer.transducer.transducer,
        volume=make_xarray_in_transducer_coords_from_volume(volume_node, transducer, protocol),
        target=fiducial_to_openlifu_point_in_transducer_coords(target_node, transducer, name = 'sonication target')
    )
    return solution, simulation_result_aggregated["p_min"], simulation_result_aggregated["intensity"], scaled_solution_analysis


#
# OpenLIFUSonicationPlannerLogic
#


class OpenLIFUSonicationPlannerLogic(ScriptedLoadableModuleLogic):
    """This class should implement all the actual
    computation done by your module.  The interface
    should be such that other python code can import
    this class and make use of the functionality without
    requiring an instance of the Widget.
    Uses ScriptedLoadableModuleLogic base class, available at:
    https://github.com/Slicer/Slicer/blob/main/Base/Python/slicer/ScriptedLoadableModule.py
    """

    def __init__(self) -> None:
        """Called when the logic class is instantiated. Can be used for initializing member variables."""
        ScriptedLoadableModuleLogic.__init__(self)

    def getParameterNode(self):
        return OpenLIFUSonicationPlannerParameterNode(super().getParameterNode())

    def computeSolution(
            self,
            inputVolume: vtkMRMLScalarVolumeNode,
            inputTarget: vtkMRMLMarkupsFiducialNode,
            inputTransducer : SlicerOpenLIFUTransducer,
            inputProtocol: SlicerOpenLIFUProtocol,
            pre_solution: bool = False) -> Tuple[SlicerOpenLIFUSolution, SlicerOpenLIFUSolutionAnalysis]:
        """Compute solution for the given volume, target, transducer, and protocol, setting the solution as the active solution.
        Note that setting the solution will trigger a write of the solution to the databse if there is an active session.

        Args:
            pre_solution: if True, this solution was computed against a virtual-fit-derived
                transducer transform rather than an approved tracking result. The solution's
                ``id`` and ``name`` are prefixed accordingly so downstream consumers (e.g.
                sonication control's load-to-device flow) can distinguish and warn. See #609.
        """
        solution_openlifu, pnp_aggregated, intensity_aggregated, analysis_openlifu = compute_solution_openlifu(
            inputProtocol.protocol,
            inputTransducer,
            inputTarget,
            inputVolume,
        )
        if pre_solution:
            # Provenance is carried on the Session's SolutionInfo entry (transducer_transform_source =
            # "virtual_fit"), so the id itself no longer needs a "presolution_" prefix
            # (SlicerOpenLIFU#611). The display name is still decorated so pre-solutions are visually
            # distinct in the UI.
            solution_openlifu.name = f"Pre-Solution for {solution_openlifu.name}"
        analysis = SlicerOpenLIFUSolutionAnalysis(analysis_openlifu)
        solution = SlicerOpenLIFUSolution.initialize_from_openlifu_data(
            solution = solution_openlifu,
            pnp_datarray=pnp_aggregated,
            intensity_dataarray=intensity_aggregated,
            transducer=inputTransducer,
            analysis=analysis,
        )

        # Build the provenance record that will be attached to the Session so consumers can
        # later trace this Solution back to its target / transducer / protocol / pose source
        # (SlicerOpenLIFU#611). ``transducer_transform_source`` follows the "VF" / "TT" split of
        # the algorithm-input widget's Target row (#609): VF rows produce pre-solutions computed
        # against a virtual-fit transform; the standard flow uses the live transducer transform,
        # which is driven by a transducer-tracking result and is therefore recorded as
        # ``"localization"``.
        #
        # ``array_transform`` (SlicerOpenLIFU#622) captures the exact transducer array-to-volume
        # transform matrix used at this compute time. The PNP / intensity volumes are stored in
        # transducer-local coordinates and parented under ``transducer.transform_node``; showing
        # this solution later must reproduce this same pose, otherwise the PNP moves with
        # whichever VF / TT is currently approved -- which is especially catastrophic for a
        # pre-solution whose backing VF approval is later revoked.
        import openlifu.db.session
        from OpenLIFULib.transform_conversion import transducer_transform_node_to_openlifu
        array_transform_openlifu = transducer_transform_node_to_openlifu(
            transform_node=inputTransducer.transform_node,
            transducer_units=inputTransducer.transducer.transducer.units,
        )
        solution_info = openlifu.db.session.SolutionInfo(
            solution_id=solution_openlifu.id,
            protocol_id=inputProtocol.protocol.id,
            target_id=fiducial_to_openlifu_point_id(inputTarget),
            transducer_id=inputTransducer.transducer.transducer.id,
            transducer_transform_source="virtual_fit" if pre_solution else "localization",
            approved=False,
            computed_at=datetime.now(),
            array_transform=array_transform_openlifu,
        )
        slicer.util.getModuleLogic("OpenLIFU").data_logic.set_solution(
            solution,
            analysis=analysis,
            solution_info=solution_info,
        )
        self.getParameterNode().solution_analysis = analysis
        return solution, analysis

    def get_pnp(self) -> Optional[vtkMRMLScalarVolumeNode]:
        """Get the PNP volume of the active solution, if there is an active solution. Return None if there isn't."""
        solution : "Optional[SlicerOpenLIFUSolution]" = get_active_solution()
        if solution is None:
            return None
        return solution.pnp

    def render_pnp(self) -> None:
        """
        Renders the PNP solution in both the 3D view (as a volume rendering)
        and all 2D slice views (as a fully opaque foreground overlay).
        """
        pnp = self.get_pnp()
        if pnp is None:
            raise RuntimeError("Cannot render PNP as there is no active solution.")

        # --- 3D View Logic (Volume Rendering) ---
        pnp.GetDisplayNode().SetAndObserveColorNodeID("vtkMRMLColorTableNodeFilePlasma.txt")
        volRenLogic = slicer.modules.volumerendering.logic()
        displayNode = volRenLogic.GetFirstVolumeRenderingDisplayNode(pnp)
        if not displayNode:
            displayNode = volRenLogic.CreateDefaultVolumeRenderingNodes(pnp)
            volRenLogic.CopyDisplayToVolumeRenderingDisplayNode(displayNode)

        for view_node in slicer.util.getNodesByClass("vtkMRMLViewNode"):
            if view_node.GetAttribute("isWizardViewNode") == "true": # Just incase, skip the wizard view nodes
                continue
            view_node.SetRaycastTechnique(slicer.vtkMRMLViewNode.MaximumIntensityProjection)
        
        displayNode.SetVisibility(True)
        scalar_opacity_mapping = displayNode.GetVolumePropertyNode().GetVolumeProperty().GetScalarOpacity()
        scalar_opacity_mapping.RemoveAllPoints()
        vmin, vmax = pnp.GetImageData().GetScalarRange()
        scalar_opacity_mapping.AddPoint(vmin,0.0)
        scalar_opacity_mapping.AddPoint(vmax,1.0)
        
        # --- 2D Slice View Logic (Foreground Layer) ---
        # Set the foreground layer with 100% opacity.
        slicer.util.setSliceViewerLayers(foreground=pnp, foregroundOpacity=1.0)

    def hide_pnp(self) -> None:
        """
        Hide the PNP volume from the 3D view and remove it from the
        foreground of slice views ONLY IF it is the active foreground volume.
        This prevents accidentally clearing other user-set foregrounds.
        """
        pnp = self.get_pnp()
        if pnp is None:
            return

        # --- 3D View Logic (Volume Rendering) ---
        volRenLogic = slicer.modules.volumerendering.logic()
        displayNode = volRenLogic.GetFirstVolumeRenderingDisplayNode(pnp)
        if displayNode:
            displayNode.SetVisibility(False)
            
        # --- 2D Slice View Logic (Surgical Foreground Clearing) ---
        # Iterate through each slice view to check its state before modifying it.
        pnp_id = pnp.GetID()
        layoutManager = slicer.app.layoutManager()
        for sliceViewName in layoutManager.sliceViewNames():
            sliceWidget = layoutManager.sliceWidget(sliceViewName)
            compositeNode = sliceWidget.mrmlSliceCompositeNode()
            
            # Check if the PNP volume is the one in the foreground
            if compositeNode.GetForegroundVolumeID() == pnp_id:
                # If it is, clear the foreground for this view only
                compositeNode.SetForegroundVolumeID("") # Set to empty string to clear

    def solution_analysis_exists(self) -> bool:
        """
        Check if a valid solution analysis exists.

        Returns:
            bool: True if both the solution_analysis and its internal analysis are present, False otherwise.
        """
        analysis = self.getParameterNode().solution_analysis
        if analysis is None or analysis.analysis is None:
            return False
        else:
            return True

    def solution_analysis_has_warnings(self) -> bool:
        """
        Check whether the solution_analysis of the OpenLIFUSonicationPlanner parameter node 
        has a warning status for any of the parameters.

        Returns:
            bool: True if any parameter has a warning flag, False otherwise.

        Raises:
            RuntimeError: If there is no solution analysis or analysis data available.
        """
        analysis = self.getParameterNode().solution_analysis
        if analysis is None:
            raise RuntimeError("Cannot check warnings because there is no solution analysis wrapper.")
        
        analysis_openlifu = analysis.analysis
        if analysis_openlifu is None:
            raise RuntimeError("Cannot check warnings because there is no solution analysis.")

        table = analysis_openlifu.to_table()
        return table['_warning'].any()


    def solution_analysis_has_errors(self) -> bool:
        """
        Check whether the solution_analysis of the OpenLIFUSonicationPlanner parameter node 
        has an error status for any of the parameters.

        Returns:
            bool: True if any parameter has an error flag, False otherwise.

        Raises:
            RuntimeError: If there is no solution analysis or analysis data available.
        """
        analysis = self.getParameterNode().solution_analysis
        if analysis is None:
            raise RuntimeError("Cannot check warnings because there is no solution analysis wrapper.")
        
        analysis_openlifu = analysis.analysis
        if analysis_openlifu is None:
            raise RuntimeError("Cannot check warnings because there is no solution analysis.")

        table = analysis_openlifu.to_table()
        return table['_error'].any()

    def toggle_solution_approval(self):
        """Approve the currently active solution if it was not approved. Revoke approval if it was approved.
        This will write the approval to the solution in memory and, if there is an active session from which
        the active solution was generated, then it will also write the solution approval to the database.
        """
        slicer.util.getModuleLogic("OpenLIFU").data_logic.toggle_solution_approval()

    def compute_analysis_from_solution(self, solution:SlicerOpenLIFUSolution) -> Optional[SlicerOpenLIFUSolutionAnalysis]:
        """Compute solution analysis from a given solution.
        Returns the SlicerOpenLIFUSolutionAnalysis on success.
        If the protocol used to compute the solution is not present, then this returns None.
        """
        solution_openlifu : "openlifu.plan.Solution" = solution.solution.solution
        data_parameter_node = get_app_state()
        if solution_openlifu.protocol_id not in data_parameter_node.loaded_protocols:
            return None
        protocol = data_parameter_node.loaded_protocols[solution_openlifu.protocol_id]
        analysis_openlifu = solution_openlifu.analyze(
            options=protocol.protocol.analysis_options
        )
        return SlicerOpenLIFUSolutionAnalysis(analysis_openlifu)


#
# OpenLIFUSonicationPlannerTest
#

class OpenLIFUSonicationPlannerTest(ScriptedLoadableModuleTest):
    """
    This is the test case for your scripted module.
    Uses ScriptedLoadableModuleTest base class, available at:
    https://github.com/Slicer/Slicer/blob/main/Base/Python/slicer/ScriptedLoadableModule.py
    """

    def _workflow_planning(self):

        import numpy as np
        from scipy.linalg import expm

        navigate_to_page("OpenLIFUSonicationPlanner")
        sp_widget = slicer.util.getModuleWidget("OpenLIFU").get_page_widget("OpenLIFUSonicationPlanner")
        sp_logic = sp_widget.logic

        # Post-#611: the page no longer has an algorithm-input widget. Session provides
        # Protocol/Transducer/Volume unambiguously; target + transducer-transform source come
        # from the New-Solution picker. We drive the same code path here by picking the first
        # eligible option and invoking the picker's shared compute helper.
        session = get_app_state().loaded_session
        assert session is not None, "Test setup: expected a loaded session on the app state."
        options = sp_widget._build_new_solution_options()
        assert options, "Test setup: expected at least one eligible (target, transform) option."
        chosen = options[0]
        selected_target = chosen.target_node
        selected_transducer = session.get_transducer()
        selected_volume = session.volume_node
        selected_protocol = session.get_protocol()

        sp_widget._compute_solution_for_option(chosen)
        assert get_active_solution() is not None

        # Test that moving the target clears the solution
        curr_pos = selected_target.GetNthControlPointPositionWorld(0)

        selected_target.SetNthControlPointPositionWorld(0, (curr_pos[0], curr_pos[1], curr_pos[2]+0.1)) # this should clear the results
        slicer.app.processEvents()
        assert get_active_solution() is None

        # Test that moving the transducer clears the solution
        solution, analysis = sp_logic.computeSolution(
            selected_volume, selected_target,
            selected_transducer, selected_protocol,
            )
        assert get_active_solution() is not None

        def make_random_matrix() -> np.ndarray:
            rng = np.random.default_rng()
            affine = np.eye(4)
            affine[:3,:3] = expm((lambda A: (A - A.T)/2)(rng.normal(size=(3,3)))) # generate a random orthogonal matrix
            affine[:3,3] = rng.random(3) # generate a random origin
            return affine

        original_transducer_transform = vtk.vtkMatrix4x4()
        selected_transducer.transform_node.GetMatrixTransformToParent(original_transducer_transform)
        selected_transducer.update_transform(make_random_matrix())
        slicer.app.processEvents()
        assert get_active_solution() is None

        # Sonication control requires a loaded solution. Compute a fresh solution
        # with live volume nodes for the sonication control workflow.
        # Create new solution ID to avoid database conflict
        selected_transducer.transform_node.SetMatrixTransformToParent(original_transducer_transform)
        slicer.app.processEvents()
        solution, analysis = sp_logic.computeSolution(
            selected_volume, selected_target,
            selected_transducer, selected_protocol,
        )
        solution.solution.solution.id = "TestSolutionID"
        slicer.util.getModuleLogic("OpenLIFU").data_logic.set_solution(solution)
        sp_logic.getParameterNode().solution_analysis = analysis
