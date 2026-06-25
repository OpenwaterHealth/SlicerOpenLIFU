# Standard library imports
from collections import defaultdict
from functools import partial
from typing import Callable, Optional, TYPE_CHECKING, Dict, List, Union

# Third-party imports
import qt
import vtk
import numpy as np

# Slicer imports
import slicer
from slicer import (
    vtkMRMLMarkupsFiducialNode,
    vtkMRMLScalarVolumeNode,
    vtkMRMLTransformNode,
)
from slicer.ScriptedLoadableModule import *
from slicer.i18n import tr as _
from slicer.i18n import translate
from slicer.parameterNodeWrapper import parameterNodeWrapper
from slicer.util import VTKObservationMixin

# OpenLIFULib imports
from OpenLIFULib import (
    OpenLIFUAlgorithmInputWidget,
    SlicerOpenLIFUProtocol,
    SlicerOpenLIFUTransducer,
    ensure_python_requirements_for_module_enter,
    get_openlifu_data_parameter_node,
    get_target_candidates,
)
from OpenLIFULib.coordinate_system_utils import get_IJK2RAS
from OpenLIFULib.events import SlicerOpenLIFUEvents
from OpenLIFULib.guided_mode_util import GuidedWorkflowMixin
from OpenLIFULib.module_layout import apply_module_layout, wire_passive_module_header
from OpenLIFULib.skinseg import get_skin_segmentation, generate_skin_segmentation
from OpenLIFULib.targets import fiducial_to_openlifu_point_id
from OpenLIFULib.transform_conversion import transducer_transform_node_from_openlifu
from OpenLIFULib.user_account_mode_util import UserAccountBanner
from OpenLIFULib.util import (
    BusyCursor,
    add_slicer_log_handler,
    replace_widget,
)
from OpenLIFULib.notifications import notify
from OpenLIFULib.virtual_fit_results import (
    add_virtual_fit_result,
    clear_virtual_fit_results,
    get_approved_target_ids,
    get_approval_from_virtual_fit_result_node,
    get_best_virtual_fit_result_node,
    get_target_id_from_virtual_fit_result_node,
    get_virtual_fit_approval_for_target,
    get_virtual_fit_result_nodes,
    revoke_any_virtual_fit_approvals_for_target,
    set_approval_for_virtual_fit_result_node
)

# These imports are done only for IDE and static analysis purposes
if TYPE_CHECKING:
    import openlifu
    import openlifu.geo
    import openlifu.plan
    import openlifu.seg.virtual_fit
    import openlifu.xdc
    from OpenLIFUData.OpenLIFUData import OpenLIFUDataLogic

PLACE_INTERACTION_MODE_ENUM_VALUE = slicer.vtkMRMLInteractionNode().Place

class OpenLIFUPrePlanning(ScriptedLoadableModule):
    """Uses ScriptedLoadableModule base class, available at:
    https://github.com/Slicer/Slicer/blob/main/Base/Python/slicer/ScriptedLoadableModule.py
    """

    def __init__(self, parent):
        ScriptedLoadableModule.__init__(self, parent)
        self.parent.title = _("OpenLIFU Pre-Planning")
        self.parent.categories = [translate("qSlicerAbstractCoreModule", "OpenLIFU.OpenLIFU Modules")]
        self.parent.dependencies = ["OpenLIFUHome"]  # add here list of module names that this module requires
        self.parent.contributors = ["Ebrahim Ebrahim (Kitware), Sadhana Ravikumar (Kitware), Peter Hollender (Openwater), Sam Horvath (Kitware), Brad Moore (Kitware)"]
        # short description of the module and a link to online module documentation
        # _() function marks text as translatable to other languages
        self.parent.helpText = _(
            "This is the pre-planning module of the OpenLIFU extension for focused ultrasound. "
            "More information at <a href=\"https://github.com/OpenwaterHealth/SlicerOpenLIFU\">github.com/OpenwaterHealth/SlicerOpenLIFU</a>."
        )
        # organization, grant, and thanks
        self.parent.acknowledgementText = _(
            "This is part of Openwater's OpenLIFU, an open-source "
            "hardware and software platform for Low Intensity Focused Ultrasound (LIFU) research "
            "and development."
        )
        # Embedded as a page of the OpenLIFU host module; hide from the modules menu.
        self.parent.hidden = True



#
# OpenLIFUPrePlanningParameterNode
#


@parameterNodeWrapper
class OpenLIFUPrePlanningParameterNode:
    """
    The parameters needed by module.

    """


#
# OpenLIFUPrePlanningWidget
#


class OpenLIFUPrePlanningWidget(ScriptedLoadableModuleWidget, VTKObservationMixin, GuidedWorkflowMixin):
    """Uses ScriptedLoadableModuleWidget base class, available at:
    https://github.com/Slicer/Slicer/blob/main/Base/Python/slicer/ScriptedLoadableModule.py
    """

    def __init__(self, parent=None) -> None:
        """Called when the user opens the module the first time and the widget is initialized."""
        ScriptedLoadableModuleWidget.__init__(self, parent)
        VTKObservationMixin.__init__(self)  # needed for parameter node observation
        self.logic = None
        self._parameterNode = None
        self._parameterNodeGuiTag = None

        self._vf_interaction_in_progress = False
        # True while we are programmatically rebuilding the targets table, to suppress itemChanged handlers
        self._populating_targets_table = False
        # True while we are programmatically rebuilding the virtual-fit results table
        self._populating_vf_results_table = False
        # True while a user edit on the targets table is propagating to the fiducial node, to skip the
        # echoed PointModifiedEvent / cell repopulation that would otherwise interrupt typing
        self._target_table_edit_in_progress = False
        # True while the user is in "edit targets" mode (cells editable, fiducials unlocked)
        self._targets_in_edit_mode = False
        # State tracking for the "Add Target" → click-to-place workflow. While placement is in progress,
        # other controls are disabled and the placement-end observer fires once.
        self._placement_in_progress = False
        self._placement_node : Optional[vtkMRMLMarkupsFiducialNode] = None
        self._placement_observer_tag : Optional[int] = None
        # Bright yellow glyph color used to indicate a target is currently editable / draggable.
        self._edit_mode_color = (1.0, 1.0, 0.0)
        # Attribute name used to stash the original SelectedColor when entering edit mode so it can be restored.
        self._original_color_attr = "SlicerOpenLIFU.OriginalSelectedColor"

    def setup(self) -> None:
        """Called when the user opens the module the first time and the widget is initialized."""
        ScriptedLoadableModuleWidget.setup(self)

        # Mapping from mrml node ID to a list of vtkCommand tags that can later be used to remove the observation
        self.node_observations : Dict[str,List[int]] = defaultdict(list)

        # Load widget from .ui file (created by Qt Designer).
        # Additional widgets can be instantiated manually and added to self.layout.
        uiWidget = slicer.util.loadUI(self.resourcePath("UI/OpenLIFUPrePlanning.ui"))
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

        # Create logic class. Logic implements all computations that should be possible to run
        # in batch mode, without a graphical user interface.
        self.logic = OpenLIFUPrePlanningLogic()

        # Prevents possible creation of two OpenLIFUData widgets
        # see https://github.com/OpenwaterHealth/SlicerOpenLIFU/issues/120
        slicer.util.getModule("OpenLIFUData").widgetRepresentation()

        # User-account status is now shown by the shared header inserted
        # by ``apply_module_layout`` above; no per-module banner needed.

        # ---- Inject guided mode workflow controls ----

        self.inject_workflow_controls_into_placeholder()

        # ---- Passive header observers ----
        # Keep the read-only header status indicators in sync with global
        # state (database / login / device / cloud sync / mode dropdowns).
        wire_passive_module_header(self, self.module_header)

        # ---- Connections ----

        # These connections ensure that we update parameter node when scene is closed
        self.addObserver(slicer.mrmlScene, slicer.mrmlScene.StartCloseEvent, self.onSceneStartClose)
        self.addObserver(slicer.mrmlScene, slicer.mrmlScene.EndCloseEvent, self.onSceneEndClose)

        # Make sure parameter node is initialized (needed for module reload)
        self.initializeParameterNode()

        self.addObserver(slicer.mrmlScene, slicer.vtkMRMLScene.NodeAddedEvent, self.onNodeAdded)
        self.addObserver(slicer.mrmlScene, slicer.vtkMRMLScene.NodeRemovedEvent, self.onNodeRemoved)
        self.addObserver(get_openlifu_data_parameter_node().parameterNode, vtk.vtkCommand.ModifiedEvent, self.onDataParameterNodeModified)

        # Replace the placeholder algorithm input widget by the actual one
        algorithm_input_names = ["Protocol", "Transducer", "Volume", "Target"]
        self.algorithm_input_widget = OpenLIFUAlgorithmInputWidget(algorithm_input_names, parent = self.ui.algorithmInputWidgetPlaceholder.parentWidget())
        replace_widget(self.ui.algorithmInputWidgetPlaceholder, self.algorithm_input_widget, self.ui)

        self.algorithm_input_widget.inputs_dict["Target"].combo_box.currentIndexChanged.connect(self.updateVirtualFitResultsTable)

        # ---- Targets table ----
        targets_table = self.ui.targetsTableWidget
        targets_header = targets_table.horizontalHeader()
        targets_header.setSectionResizeMode(0, qt.QHeaderView.Stretch)
        for col in (1, 2, 3):
            targets_header.setSectionResizeMode(col, qt.QHeaderView.ResizeToContents)
        targets_header.setSectionResizeMode(4, qt.QHeaderView.ResizeToContents)
        targets_header.setSectionResizeMode(5, qt.QHeaderView.ResizeToContents)
        targets_table.itemSelectionChanged.connect(self.onTargetsTableSelectionChanged)
        targets_table.itemChanged.connect(self.onTargetsTableItemChanged)

        # Watch any fiducial nodes that already existed before this module was set up
        for fiducial_node in slicer.util.getNodesByClass("vtkMRMLMarkupsFiducialNode"):
            self.watch_fiducial_node(fiducial_node)

        self.resetVirtualFitProgressDisplay()
        self.updateTargetsTable()
        self.updateInputOptions()
        self.updateTargetsActionButtonsEnabled()
        self.updateVirtualFitSectionState()

        self.ui.addTargetButton.clicked.connect(self.onAddTargetClicked)
        self.ui.removeTargetButton.clicked.connect(self.onRemoveTargetClicked)
        self.ui.editTargetsButton.toggled.connect(self.onEditTargetsToggled)
        self.ui.virtualfitButton.clicked.connect(self.onRunAutoFitClicked)

        # ---- Virtual fit result options ----
        self.ui.virtualFitResultTable.itemSelectionChanged.connect(self.onVirtualFitResultSelected)
        self.ui.virtualFitResultTable.itemChanged.connect(self.onVirtualFitResultItemChanged)
        vf_header = self.ui.virtualFitResultTable.horizontalHeader()
        vf_header.setSectionResizeMode(0, qt.QHeaderView.Stretch)
        vf_header.setSectionResizeMode(1, qt.QHeaderView.ResizeToContents)
        self.ui.editTransformPushButton.clicked.connect(self.onEditTransformClicked)
        self.ui.editTransformPushButton.setStyleSheet("""
        QPushButton:checked {
        border: 2px solid green; 
        background-color: lightgray; 
        padding: 4px;
        }
        """)
        self.ui.editTransformPushButton.setToolTip("Toggle whether the selected virtual fit transform can be interactively edited via the 3D view handles")
        self.ui.addTransformPushButton.clicked.connect(self.onAddVirtualFitResultClicked)
        self.ui.addTransformPushButton.setToolTip("Create new virtual fit result")
        self.ui.removeTransformPushButton.clicked.connect(self.onRemoveVirtualFitClicked)
        self.ui.removeTransformPushButton.setToolTip("Remove the selected virtual fit result from the scene")
        self.updateVirtualFitResultsTable()
        slicer.util.getModule("OpenLIFUTransducerLocalization").widgetRepresentation()
        self.logic.call_on_chosen_virtual_fit_changed(slicer.modules.OpenLIFUTransducerLocalizationWidget.setVirtualFitResultForTracking)
        # ------------------------------------

        self.updateWorkflowControls()

    def cleanup(self) -> None:
        """Called when the application closes and the module widget is destroyed."""
        self.removeObservers()

    def enter(self) -> None:
        """Called each time the user opens this module."""
        ensure_python_requirements_for_module_enter()
        # Make sure parameter node exists and observed
        self.initializeParameterNode()
        self.updateWorkflowControls()

    def exit(self) -> None:
        """Called each time the user opens a different module."""
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
        if self.parent.isEntered:
            self.initializeParameterNode()

    def initializeParameterNode(self) -> None:
        """Ensure parameter node exists and observed."""
        # Parameter node stores all user choices in parameter values, node selections, etc.
        # so that when the scene is saved and reloaded, these settings are restored.

        self.setParameterNode(self.logic.getParameterNode())

    def setParameterNode(self, inputParameterNode: Optional[OpenLIFUPrePlanningParameterNode]) -> None:
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

    @vtk.calldata_type(vtk.VTK_OBJECT)
    def onNodeAdded(self, caller, event, node : slicer.vtkMRMLNode) -> None:
        
        if node.GetAttribute("cloned"):
            return

        if node.IsA('vtkMRMLMarkupsFiducialNode'):
            self.watch_fiducial_node(node)

        self.updateTargetsTable()
        self.updateInputOptions()
        self.updateVirtualFitSectionState()

    @vtk.calldata_type(vtk.VTK_OBJECT)
    def onNodeRemoved(self, caller, event, node : slicer.vtkMRMLNode) -> None:

        if node.GetAttribute("cloned"):
            return
        if node.IsA('vtkMRMLMarkupsFiducialNode'):
            self.unwatch_fiducial_node(node)

            data_logic : "OpenLIFUDataLogic" = slicer.util.getModuleLogic('OpenLIFUData')
            if not data_logic.session_loading_unloading_in_progress:
                self.revokeTargetApprovalIfAny(node, reason="The target was removed.\n" +
                "Any virtual fit transforms associated with this target will also be removed.")

                # Clear affiliated virtual fit results if present
                self.logic.clear_virtual_fit_results(target = node)
                self.updateWorkflowControls()

        self.updateTargetsTable()
        self.updateInputOptions()
        self.updateVirtualFitSectionState()

    def watch_fiducial_node(self, node:vtkMRMLMarkupsFiducialNode):
        """Add observers so that point-list changes in this fiducial node are tracked by the module."""
        self.node_observations[node.GetID()].append(node.AddObserver(slicer.vtkMRMLMarkupsNode.PointAddedEvent,partial(self.onPointAddedOrRemoved, node)))
        self.node_observations[node.GetID()].append(node.AddObserver(slicer.vtkMRMLMarkupsNode.PointRemovedEvent,partial(self.onPointAddedOrRemoved, node)))
        self.node_observations[node.GetID()].append(node.AddObserver(slicer.vtkMRMLMarkupsNode.PointModifiedEvent,partial(self.onPointModified, node)))
        self.node_observations[node.GetID()].append(node.AddObserver(SlicerOpenLIFUEvents.TARGET_NAME_MODIFIED_EVENT,self.onTargetNameModified))

    def unwatch_fiducial_node(self, node:vtkMRMLMarkupsFiducialNode):
        """Un-does watch_fiducial_node; see watch_fiducial_node."""
        if node.GetID() not in self.node_observations:
            return
        for tag in self.node_observations.pop(node.GetID()):
            node.RemoveObserver(tag)

    def onPointAddedOrRemoved(self, node:vtkMRMLMarkupsFiducialNode, caller, event):
        self.updateTargetsTable()
        self.updateInputOptions()
        self.updateWorkflowControls()
        self.updateVirtualFitSectionState()
        data_logic : "OpenLIFUDataLogic" = slicer.util.getModuleLogic('OpenLIFUData')
        if not data_logic.session_loading_unloading_in_progress and not slicer.util.getModuleWidget("OpenLIFUTransducerLocalization")._running_wizard:
            reason = "The target was modified."
            self.revokeTargetApprovalIfAny(node, reason=reason)
            self.clearVirtualFitResultsIfAny(node, reason = reason)
            slicer.util.getModuleWidget('OpenLIFUSonicationPlanner').deleteSolutionAndSolutionAnalysisIfAny(reason=reason)

    def onPointModified(self, node:vtkMRMLMarkupsFiducialNode, caller, event):
        # Refresh the corresponding row's R/A/S cells to reflect the new fiducial position, unless we
        # ourselves just wrote that position from a cell edit (in which case the cells are already correct
        # and re-populating would steal focus / cancel an in-progress edit on the next cell).
        if not self._target_table_edit_in_progress:
            self._refresh_target_row_for_node(node)

        data_logic : "OpenLIFUDataLogic" = slicer.util.getModuleLogic('OpenLIFUData')
        if not data_logic.session_loading_unloading_in_progress and not slicer.util.getModuleWidget("OpenLIFUTransducerLocalization")._running_wizard:
            reason = "The target was modified."
            self.revokeTargetApprovalIfAny(node, reason=reason)
            self.clearVirtualFitResultsIfAny(node, reason = reason)
            slicer.util.getModuleWidget('OpenLIFUSonicationPlanner').deleteSolutionAndSolutionAnalysisIfAny(reason=reason)

    def clearVirtualFitResultsIfAny(self,target: vtkMRMLMarkupsFiducialNode, reason:str):
        """Clear virtual fit results for the target from the scene if any.
        """
        target_id = fiducial_to_openlifu_point_id(target)
        session = get_openlifu_data_parameter_node().loaded_session
        session_id = None if session is None else session.get_session_id()
        
        if list(get_virtual_fit_result_nodes(target_id, session_id)):
            self.logic.clear_virtual_fit_results(target = target)
            self.updateWorkflowControls()
            notify(f"Virtual fit results for {target_id} removed:\n{reason}")

    def revokeTargetApprovalIfAny(self, target : Union[str,vtkMRMLMarkupsFiducialNode], reason:str):
        """Revoke virtual fit approval for the target if there was an approval, and show a message dialog to that effect.
        The target can be provided as either a mrml node or an openlifu target ID.
        """

        if isinstance(target,str):
            target_id = target
        elif isinstance(target,vtkMRMLMarkupsFiducialNode):
            target_id = fiducial_to_openlifu_point_id(target)
        else:
            raise ValueError("Invalid target type.")

        if self.logic.get_virtual_fit_approval(target_id):
            self.logic.revoke_virtual_fit_approval(target_id)
            notify(f"Virtual fit approval revoked:\n{reason}")

    def revokeVirtualFitApprovalIfAny(self, node: vtkMRMLTransformNode, reason:str):
        """Revoke virtual fit approval for the virtual fit result node if there was an approval, and show a message dialog to that effect.
        """

        is_approved = get_approval_from_virtual_fit_result_node(node)
        if is_approved:
            set_approval_for_virtual_fit_result_node(
                approval_state= False,
                vf_result_node = node)
            data_logic : "OpenLIFUDataLogic" = slicer.util.getModuleLogic('OpenLIFUData')
            data_logic.update_underlying_openlifu_session()
            notify(f"Virtual fit approval revoked:\n{reason}")

            # Need this because updates to the data parameter node resets the combo box 
            self.setCurrentVirtualFitSelection(node)
            self.updateVirtualfitButtons()

    def updateTargetsTable(self):
        """Rebuild the targets table from the current set of target candidate fiducials."""
        table = self.ui.targetsTableWidget
        currently_selected_row = table.currentRow()

        self._populating_targets_table = True
        try:
            table.clearContents()
            table.setRowCount(0)  # ensures any previous cell widgets (jump buttons) are released
            target_nodes = get_target_candidates()
            table.setRowCount(len(target_nodes))

            editable_flags = (
                qt.Qt.ItemIsSelectable | qt.Qt.ItemIsEnabled | qt.Qt.ItemIsEditable
            ) if self._targets_in_edit_mode else (
                qt.Qt.ItemIsSelectable | qt.Qt.ItemIsEnabled
            )

            for row, node in enumerate(target_nodes):
                # Column 0: display name (control-point label). Editing here only renames; it does not
                # affect the underlying openlifu Point ID (the node name), so virtual-fit results keyed
                # off the target ID are preserved.
                name_item = qt.QTableWidgetItem(node.GetNthControlPointLabel(0))
                name_item.setData(qt.Qt.UserRole, node)
                name_item.setFlags(editable_flags)
                table.setItem(row, 0, name_item)

                # Columns 1-3: R, A, S coordinates.
                position = node.GetNthControlPointPosition(0)
                for col, coord_value in enumerate(position, start=1):
                    coord_item = qt.QTableWidgetItem(f"{coord_value:0.2f}")
                    coord_item.setFlags(editable_flags)
                    coord_item.setTextAlignment(qt.Qt.AlignRight | qt.Qt.AlignVCenter)
                    table.setItem(row, col, coord_item)

                # Column 4: Show checkbox.
                show_item = qt.QTableWidgetItem()
                show_item.setFlags(
                    qt.Qt.ItemIsSelectable | qt.Qt.ItemIsEnabled | qt.Qt.ItemIsUserCheckable
                )
                display_node = node.GetDisplayNode()
                visible = bool(display_node and display_node.GetVisibility())
                show_item.setCheckState(qt.Qt.Checked if visible else qt.Qt.Unchecked)
                show_item.setTextAlignment(qt.Qt.AlignCenter)
                table.setItem(row, 4, show_item)

                # Column 5: jump-to-target button. Snaps all slice views to the target's position.
                jump_button = qt.QPushButton("\u2316")  # position indicator (crosshair) glyph
                jump_button.setToolTip("Jump slice views to this target's position")
                jump_button.setFlat(True)
                jump_button.setFixedSize(qt.QSize(22, 18))
                jump_button.clicked.connect(partial(self._jump_slices_to_node, node))
                table.setCellWidget(row, 5, jump_button)
        finally:
            self._populating_targets_table = False

        # Restore selection: keep previous row if still valid, otherwise select first row.
        if table.rowCount > 0:
            if 0 <= currently_selected_row < table.rowCount:
                table.selectRow(currently_selected_row)
            else:
                table.selectRow(0)

        self.updateTargetsActionButtonsEnabled()

    def _refresh_target_row_for_node(self, node: vtkMRMLMarkupsFiducialNode) -> None:
        """Refresh the R/A/S cells of the row associated to the given fiducial node, if it is in the table."""
        table = self.ui.targetsTableWidget
        self._populating_targets_table = True
        try:
            for row in range(table.rowCount):
                item = table.item(row, 0)
                if item is not None and item.data(qt.Qt.UserRole) is node:
                    position = node.GetNthControlPointPosition(0)
                    for col, coord_value in enumerate(position, start=1):
                        coord_item = table.item(row, col)
                        if coord_item is not None:
                            coord_item.setText(f"{coord_value:0.2f}")
                    break
        finally:
            self._populating_targets_table = False

    def getCurrentSelectedTarget(self) -> Optional[vtkMRMLMarkupsFiducialNode]:
        """Return the fiducial node associated to the currently selected row, or None."""
        table = self.ui.targetsTableWidget
        row = table.currentRow()
        if row < 0:
            return None
        item = table.item(row, 0)
        if item is None:
            return None
        return item.data(qt.Qt.UserRole)

    def onTargetsTableSelectionChanged(self):
        self.updateTargetsActionButtonsEnabled()

    def onTargetsTableItemChanged(self, item: qt.QTableWidgetItem):
        """Apply user edits in the targets table to the underlying fiducial node."""
        if self._populating_targets_table:
            return
        table = self.ui.targetsTableWidget
        row = item.row()
        name_item = table.item(row, 0)
        if name_item is None:
            return
        node : vtkMRMLMarkupsFiducialNode = name_item.data(qt.Qt.UserRole)
        if node is None:
            return

        col = item.column()
        if col == 0:
            # Display-name edit: only update the control-point label and emit the name-modified event so
            # other modules can refresh their displays. We deliberately do NOT call node.SetName(), since
            # the node name is the openlifu Point ID that virtual-fit results are keyed on.
            new_label = item.text()
            node.SetNthControlPointLabel(0, new_label)
            node.InvokeEvent(SlicerOpenLIFUEvents.TARGET_NAME_MODIFIED_EVENT)
        elif col in (1, 2, 3):
            try:
                new_value = float(item.text())
            except ValueError:
                # Restore the previous text for that cell from the fiducial node.
                self._refresh_target_row_for_node(node)
                return
            position = list(node.GetNthControlPointPosition(0))
            position[col - 1] = new_value
            self._target_table_edit_in_progress = True
            try:
                node.SetNthControlPointPosition(0, *position)
            finally:
                self._target_table_edit_in_progress = False
        elif col == 4:
            display_node = node.GetDisplayNode()
            if display_node is not None:
                display_node.SetVisibility(item.checkState() == qt.Qt.Checked)

    def onTargetNameModified(self, caller, event):
        self.updateInputOptions()

    def onDataParameterNodeModified(self,caller, event) -> None:
        self.updateInputOptions() 
        self.updateWorkflowControls()
        self.updateVirtualFitRelatedLabels()

    def updateVirtualFitRelatedLabels(self):
        """When virtual fit approval is revoked or toggled, the messages displayed in the data module
        and transducer tracking module need to be updated."""
        slicer.modules.OpenLIFUDataWidget.updateSessionStatus()
        slicer.modules.OpenLIFUTransducerLocalizationWidget.updateVirtualFitStatus()

    def updateTargetsActionButtonsEnabled(self):
        """Update enabled state and label of the targets action buttons (Add / Edit / Remove)."""
        current_selection = self.getCurrentSelectedTarget()
        self.ui.removeTargetButton.setEnabled(current_selection is not None and not self._targets_in_edit_mode)
        self.ui.addTargetButton.setEnabled(not self._targets_in_edit_mode)
        # The Edit/Done toggle is always enabled (you can leave edit mode even if no target is selected).
        self.ui.editTargetsButton.setEnabled(self.ui.targetsTableWidget.rowCount > 0 or self._targets_in_edit_mode)
        self.ui.editTargetsButton.setText("Done Editing" if self._targets_in_edit_mode else "Edit")

    def updateVirtualFitSectionState(self):
        """Collapse the virtual-fit section if there are no targets; disable it while editing targets."""
        has_targets = self.ui.targetsTableWidget.rowCount > 0
        if not has_targets:
            self.ui.virtualfitCollapsible.collapsed = True
        self.ui.virtualfitCollapsible.setEnabled(not self._targets_in_edit_mode)

    def onEditTargetsToggled(self, checked: bool):
        self._set_targets_edit_mode(checked)

    def _set_targets_edit_mode(self, enabled: bool) -> None:
        """Toggle edit mode for the targets table: unlock all fiducials and allow cell editing when on."""
        self._targets_in_edit_mode = enabled
        # Lock state on the fiducials mirrors edit mode -- when not editing, all targets are locked so
        # they cannot be dragged in the 3D view either. We also tint the glyph color to indicate state.
        for node in get_target_candidates():
            node.SetLocked(not enabled)
            self._apply_edit_mode_color(node, editing=enabled)
        # Toggle table edit triggers so the cells become editable / read-only in sync with the mode.
        self.ui.targetsTableWidget.setEditTriggers(
            qt.QAbstractItemView.DoubleClicked | qt.QAbstractItemView.EditKeyPressed | qt.QAbstractItemView.AnyKeyPressed
            if enabled else qt.QAbstractItemView.NoEditTriggers
        )
        # Reapply per-cell editable flags via a rebuild so the change takes effect immediately.
        self.updateTargetsTable()
        self.updateVirtualFitSectionState()
        self.updateWorkflowControls()

    def _apply_edit_mode_color(self, node: vtkMRMLMarkupsFiducialNode, editing: bool) -> None:
        """Tint the fiducial's selected glyph color to a fixed edit-mode color while editing; restore
        the originally-assigned color otherwise. The original color is stashed in a node attribute so it
        survives across edit-mode toggles."""
        display_node = node.GetDisplayNode()
        if display_node is None:
            return
        if editing:
            if not node.GetAttribute(self._original_color_attr):
                r, g, b = display_node.GetSelectedColor()
                node.SetAttribute(self._original_color_attr, f"{r},{g},{b}")
            display_node.SetSelectedColor(*self._edit_mode_color)
        else:
            saved = node.GetAttribute(self._original_color_attr)
            if saved:
                try:
                    r, g, b = (float(x) for x in saved.split(","))
                    display_node.SetSelectedColor(r, g, b)
                except ValueError:
                    pass
                node.RemoveAttribute(self._original_color_attr)

    def _jump_slices_to_node(self, node: vtkMRMLMarkupsFiducialNode, *args) -> None:
        """Snap all slice views to the position of the given target's first control point."""
        if node is None or node.GetNumberOfControlPoints() < 1:
            return
        position = node.GetNthControlPointPosition(0)
        markups_logic = slicer.modules.markups.logic()
        # JumpSlicesToLocation(r, a, s, centered) is supported across recent Slicer versions.
        markups_logic.JumpSlicesToLocation(position[0], position[1], position[2], True)

    def _set_non_placement_controls_enabled(self, enabled: bool) -> None:
        """Enable/disable the rest of the targets+virtual-fit UI while a target is being placed."""
        for widget in (
            self.ui.targetsTableWidget,
            self.ui.addTargetButton,
            self.ui.editTargetsButton,
            self.ui.removeTargetButton,
            self.ui.virtualfitCollapsible,
        ):
            widget.setEnabled(enabled)

    def onAddTargetClicked(self, checked: bool = False):
        # If we are already in point placement mode then do nothing
        interaction_node = slicer.mrmlScene.GetNodeByID("vtkMRMLInteractionNodeSingleton")
        if (
            self._placement_in_progress
            or interaction_node.GetCurrentInteractionMode() == PLACE_INTERACTION_MODE_ENUM_VALUE
        ):
            return

        node = slicer.mrmlScene.AddNewNodeByClass("vtkMRMLMarkupsFiducialNode")
        node.SetMaximumNumberOfControlPoints(1)
        node.SetName(slicer.mrmlScene.GenerateUniqueName("Target"))
        node.SetMarkupLabelFormat("%N")

        self._placement_in_progress = True
        self._placement_node = node
        self._set_non_placement_controls_enabled(False)

        # Observe the interaction node so we can detect placement completion or cancellation (escape).
        self._placement_observer_tag = interaction_node.AddObserver(
            interaction_node.EndPlacementEvent, self._onPlacementEnded
        )

        slicer.modules.markups.logic().StartPlaceMode(
            False  # "place mode persistence" set to False means we want to place one target and then stop
        )

        self.updateWorkflowControls()

    def _onPlacementEnded(self, caller, event) -> None:
        """Called once when the user either places the target or cancels with Escape."""
        interaction_node = caller
        if self._placement_observer_tag is not None:
            interaction_node.RemoveObserver(self._placement_observer_tag)
            self._placement_observer_tag = None

        node = self._placement_node
        self._placement_node = None
        self._placement_in_progress = False
        self._set_non_placement_controls_enabled(True)

        if node is None or not slicer.mrmlScene.IsNodePresent(node):
            self.updateWorkflowControls()
            return

        if node.GetNumberOfControlPoints() < 1:
            # User cancelled placement (e.g. pressed Escape) — clean up the empty placeholder node.
            slicer.mrmlScene.RemoveNode(node)
            self.updateWorkflowControls()
            return

        # A point was placed: enter edit mode (so the user can fine-tune by dragging) and select the
        # new target in the table.
        if not self._targets_in_edit_mode:
            self.ui.editTargetsButton.setChecked(True)  # triggers onEditTargetsToggled → _set_targets_edit_mode
        else:
            # Already in edit mode: just make sure the new node has the edit-mode color applied.
            self._apply_edit_mode_color(node, editing=True)
            self.updateTargetsTable()

        # Select the row of the newly-placed target.
        table = self.ui.targetsTableWidget
        for row in range(table.rowCount):
            name_item = table.item(row, 0)
            if name_item is not None and name_item.data(qt.Qt.UserRole) is node:
                table.selectRow(row)
                break

        self.updateWorkflowControls()

    def onRemoveTargetClicked(self, checked: bool = False):
        node = self.getCurrentSelectedTarget()
        if node is None:
            raise RuntimeError("It should not be possible to click Remove target while there is not a valid target selected.")
        slicer.mrmlScene.RemoveNode(node)

        self.updateWorkflowControls()

    def updateInputOptions(self):
        """Update the algorithm input options"""

        self._input_update_in_progress = True
        self.algorithm_input_widget.update()
        self._input_update_in_progress = False  # Prevents repeated function calls due to combo box index changed signals

        self.updateVirtualFitResultsTable()

    def updateVirtualfitButtons(self):
        """Update the enabled status of all the virtual fit related buttons"""

        if not self.algorithm_input_widget.has_valid_selections():
            for button in [
                self.ui.virtualfitButton,
                self.ui.editTransformPushButton,
                self.ui.addTransformPushButton,
                self.ui.removeTransformPushButton,
            ]:
                button.enabled = False
                button.setToolTip("Specify all required inputs to enable virtual fitting")
            self.ui.editTransformPushButton.checked = False
        else:
            selected_vf_result = self.getCurrentVirtualFitSelection()
            currently_interacting = len(self.get_currently_active_interaction_node()) > 0

            if currently_interacting:
                for button in [
                    self.ui.virtualfitButton,
                    self.ui.editTransformPushButton,
                    self.ui.addTransformPushButton,
                    self.ui.removeTransformPushButton,
                ]:
                    button.enabled = False
                    button.setToolTip("Finish editing the transform first")
                self.ui.editTransformPushButton.enabled = True  # Enabled because it acts as the "Done Editing" button

            else:
                self.ui.virtualfitButton.enabled = True
                self.ui.virtualfitButton.setToolTip("Run virtual fit algorithm to automatically suggest a transducer positioning." \
                    "Any existing virtual fit results for the selected target will be removed.")
                self.ui.addTransformPushButton.enabled=True
                self.ui.addTransformPushButton.setToolTip("Add a new transducer transform to the table, to be manually positioned.")

                self.ui.editTransformPushButton.checked = False

                if selected_vf_result is None:
                    self.ui.editTransformPushButton.enabled = False
                    self.ui.editTransformPushButton.setToolTip("Select a virtual fit result to edit")
                    self.ui.removeTransformPushButton.enabled = False
                    self.ui.removeTransformPushButton.setToolTip("Select a virtual fit result to remove")
                else:
                    self.ui.editTransformPushButton.enabled = True
                    self.ui.editTransformPushButton.setToolTip("Edit the selected transform via interaction handles in the 3D view")
                    self.ui.removeTransformPushButton.enabled = True
                    self.ui.removeTransformPushButton.setToolTip("Remove the selected virtual fit result from the scene")

    def updateWorkflowControls(self):
        session = get_openlifu_data_parameter_node().loaded_session
        session_id = None if session is None else session.get_session_id()

        if session is None:
            self.workflow_controls.can_proceed = False
            self.workflow_controls.status_text = "If you are seeing this, guided mode is being run out of order! Load a session to proceed."
            return
        if self._vf_interaction_in_progress:
            self.workflow_controls.can_proceed = False
            self.workflow_controls.status_text = "Finish modifying the virtual fit transform before proceeding."
            return

        target_nodes = get_target_candidates()
        if not target_nodes:
            self.workflow_controls.can_proceed = False
            self.workflow_controls.status_text = "Create a target to proceed."
            return

        # Every current target must have an approved virtual fit before proceeding. Targets that have
        # been deleted from the table no longer count.
        target_ids = {fiducial_to_openlifu_point_id(n) for n in target_nodes}
        vf_nodes = list(get_virtual_fit_result_nodes(session_id=session_id))
        targets_with_any_vf = {n.GetAttribute("VF:targetID") for n in vf_nodes}
        approved_ids = set(self.logic.get_approved_target_ids())

        missing_vf = target_ids - targets_with_any_vf
        missing_approval = target_ids - approved_ids

        if missing_vf:
            self.workflow_controls.can_proceed = False
            self.workflow_controls.status_text = "Run a virtual fit for every target to proceed."
        elif missing_approval:
            self.workflow_controls.can_proceed = False
            self.workflow_controls.status_text = "Approve a virtual fit for every target to proceed."
        else:
            self.workflow_controls.can_proceed = True
            self.workflow_controls.status_text = "All targets have an approved virtual fit, proceed to the next step."

    def resetVirtualFitProgressDisplay(self):
        self.ui.virtualFitProgressBar.hide()
        self.ui.virtualFitProgressStatusLabel.hide()
    
    def setVirtualFitProgressDisplay(self, value: int, status_text: str):
        self.ui.virtualFitProgressBar.value = value
        self.ui.virtualFitProgressStatusLabel.text = status_text
        self.ui.virtualFitProgressBar.show()
        self.ui.virtualFitProgressStatusLabel.show()

    def updateVirtualFitResultsTable(self):
        """ Updates the list of virtual list results shown. This is dependent on the 
        currently selected target in the algorithm inputs."""
        
        # Ignore function calls while the algorithm inputs are updated.
        if self._input_update_in_progress:
            return

        activeData = self.algorithm_input_widget.get_current_data()
        target = activeData["Target"]
        session = get_openlifu_data_parameter_node().loaded_session
        session_id : Optional[str] = session.get_session_id() if session is not None else None
        vf_results = []
        if target is not None:
            target_id = fiducial_to_openlifu_point_id(target)
            vf_results = list(get_virtual_fit_result_nodes(target_id=target_id, session_id=session_id, sort=True))

        # Only one VF per target may be approved: while any row is approved, the unchecked rows have
        # the checkbox disabled. Users must un-approve the current one before approving another.
        any_approved = any(get_approval_from_virtual_fit_result_node(r) for r in vf_results)

        most_recent_selection = self.ui.virtualFitResultTable.currentRow()
        self._populating_vf_results_table = True
        try:
            self.ui.virtualFitResultTable.clearContents()
            self.ui.virtualFitResultTable.setRowCount(len(vf_results))

            for row_idx, result in enumerate(vf_results):
                result_item = qt.QTableWidgetItem(result.GetAttribute("DisplayName"))
                result_item.setFlags(qt.Qt.ItemIsSelectable | qt.Qt.ItemIsEnabled)
                result_item.setData(qt.Qt.UserRole, result)
                self.ui.virtualFitResultTable.setItem(row_idx, 0, result_item)

                is_approved = get_approval_from_virtual_fit_result_node(result)
                approved_item = qt.QTableWidgetItem()
                # Greyed-out but visible checkbox: keep ItemIsUserCheckable, drop ItemIsEnabled.
                if any_approved and not is_approved:
                    approved_item.setFlags(qt.Qt.ItemIsSelectable | qt.Qt.ItemIsUserCheckable)
                    approved_item.setToolTip(
                        "Un-approve the currently approved virtual fit for this target before approving another."
                    )
                else:
                    approved_item.setFlags(
                        qt.Qt.ItemIsSelectable | qt.Qt.ItemIsEnabled | qt.Qt.ItemIsUserCheckable
                    )
                approved_item.setCheckState(qt.Qt.Checked if is_approved else qt.Qt.Unchecked)
                approved_item.setTextAlignment(qt.Qt.AlignCenter)
                self.ui.virtualFitResultTable.setItem(row_idx, 1, approved_item)

            if vf_results and 0 <= most_recent_selection < self.ui.virtualFitResultTable.rowCount:
                self.ui.virtualFitResultTable.selectRow(most_recent_selection)
            if vf_results:
                self.ui.virtualFitResultTable.resizeRowsToContents()
        finally:
            self._populating_vf_results_table = False

        if not vf_results:
            self.logic.chosen_virtual_fit = None

        self.updateVirtualfitButtons()

        # The TransducerLocalization widget may not yet be constructed/setup when this fires during PrePlanning setup
        # (Slicer creates module widgets lazily; cross-module attribute access is otherwise unguarded).
        tl_widget = getattr(slicer.modules, "OpenLIFUTransducerLocalizationWidget", None)
        if tl_widget is not None and hasattr(tl_widget, "_input_update_in_progress"):
            tl_widget.setVirtualFitResultForTracking(self.logic.chosen_virtual_fit)

    def onVirtualFitResultItemChanged(self, item: qt.QTableWidgetItem):
        """Handle in-table edits to a virtual fit result row. Currently only the Approved checkbox is editable."""
        if self._populating_vf_results_table:
            return
        if item.column() != 1:
            return
        row = item.row()
        name_item = self.ui.virtualFitResultTable.item(row, 0)
        if name_item is None:
            return
        vf_node : vtkMRMLTransformNode = name_item.data(qt.Qt.UserRole)
        if vf_node is None:
            return
        desired = (item.checkState() == qt.Qt.Checked)
        current = get_approval_from_virtual_fit_result_node(vf_node)
        if desired == current:
            return
        new_state = self.logic.toggle_virtual_fit_approval(vf_node)  # triggers data parameter node modified
        if new_state:
            self.watchVirtualFit(vf_node)
        else:
            self.unwatchVirtualFit(vf_node)
        self.setCurrentVirtualFitSelection(vf_node)
        self.updateVirtualfitButtons()
        self.updateWorkflowControls()

    def getCurrentVirtualFitSelection(self):
        """ Returns the virtual fit transform node associated with the current selection."""

        row = self.ui.virtualFitResultTable.currentRow()
        if row < 0:
            return None
        name_item = self.ui.virtualFitResultTable.item(row, 0)
        if name_item is None:
            return None
        selected_vf_result = name_item.data(qt.Qt.UserRole)
        if selected_vf_result is None:
            raise RuntimeError("No transform node found in association with the selected virtual fit result")
        return selected_vf_result

    def setCurrentVirtualFitSelection(self, node: vtkMRMLTransformNode):
        """ Selects the row associated with the given transform node in the results table.
        If different to the current selection, this updates the transducer position."""

        selected_item = self.ui.virtualFitResultTable.findItems(node.GetAttribute("DisplayName"), qt.Qt.MatchExactly)
        if not selected_item:
            raise RuntimeError("Cannot find the given node in the virtual fit results table")
        self.ui.virtualFitResultTable.selectRow(selected_item[0].row())
        
    def onRunAutoFitClicked(self):  
        self.create_virtual_fit_result(auto_fit = True)
    
    def onAddVirtualFitResultClicked(self):
        self.create_virtual_fit_result(auto_fit = False)

    def create_virtual_fit_result(self, auto_fit: bool):

        activeData = self.algorithm_input_widget.get_current_data()
        protocol = activeData["Protocol"]
        transducer = activeData["Transducer"]
        volume = activeData["Volume"]
        target = activeData["Target"]

        if auto_fit:
            virtual_fit_result = self.run_virtual_fit_algorithm(
                protocol = protocol,
                transducer = transducer,
                volume = volume,
                target = target
            )
            if virtual_fit_result is None:
                slicer.util.errorDisplay("Fitting algorithm failed. No viable transducer positions found.")
                return
        else:
            virtual_fit_result = self.logic.create_manual_virtual_fit_result(
                transducer = transducer,
                volume = volume,
                target = target)
        
        # If running the fitting algorithm, defaults to the rank 1 virtual fit result
        transducer.set_current_transform_to_match_transform_node(virtual_fit_result)
        self.watchVirtualFit(virtual_fit_result)
        self.updateVirtualFitResultsTable()
        self.setCurrentVirtualFitSelection(virtual_fit_result)

        # Display the skin segmentation and transducer
        self.showSkin(activeData["Volume"])
        activeData["Transducer"].set_visibility(True)

        # A manually-added virtual fit starts in editing mode so the user can position it via the
        # 3D-view interaction handles until they explicitly click "Done Editing".
        if not auto_fit:
            self.enable_manual_interaction(transducer, virtual_fit_result)

        self.updateWorkflowControls()

    def showSkin(self, volume_node : vtkMRMLScalarVolumeNode) -> None:
        """Enable visibility on the skin mesh node associted to a particular volume,
        and update the associated visibility controls across SlicerOpenLIFU.

        Raises an error if there is no skin mesh node.
        """
        skin_mesh_node = get_skin_segmentation(volume_node)
        if skin_mesh_node is None:
            raise RuntimeError(f"There is no skin mesh node associated to the volume {volume_node.GetID()}")
        skin_mesh_node.SetDisplayVisibility(True)
        slicer.modules.OpenLIFUTransducerLocalizationWidget.updateModelRenderingSettings()

    def run_virtual_fit_algorithm(
        self,
        protocol: SlicerOpenLIFUProtocol,
        transducer: SlicerOpenLIFUTransducer,
        volume: vtkMRMLScalarVolumeNode,
        target: vtkMRMLMarkupsFiducialNode
        ):

        def progress_callback(progress_percent:int, step_description:str) -> None:
            self.setVirtualFitProgressDisplay(value = progress_percent, status_text = step_description)
            slicer.app.processEvents()

        target_id = fiducial_to_openlifu_point_id(target)
        notify(f"Removing any existing virtual fit results for {target_id}.")

        with BusyCursor():
            try:
                virtual_fit_result : Optional[vtkMRMLTransformNode] = self.logic.virtual_fit(
                    protocol = protocol,
                    transducer = transducer,
                    volume = volume,
                    target = target,
                    progress_callback = progress_callback,
                    include_debug_info = self.ui.virtualfitDebugCheckbox.checked,
                )
            finally:
                self.resetVirtualFitProgressDisplay()
        
        return virtual_fit_result

    def onVirtualFitResultSelected(self):
        """Updates the transducer transform to match the currently selected virtual fit result"""

        # selectRow() during a table repopulate would otherwise move the transducer back to the
        # VF pose -- masquerading as a user click when no click occurred.
        if self._populating_vf_results_table:
            return

        selected_vf_result = self.getCurrentVirtualFitSelection()

        if selected_vf_result is None:
            # TODO: There should be a separate radio button for indicating the 'chosen' result for tracking
            return

        activeData = self.algorithm_input_widget.get_current_data()
        activeData["Transducer"].set_current_transform_to_match_transform_node(selected_vf_result)
        # Incase they were not previously shown
        activeData["Transducer"].set_visibility(True) 
        self.showSkin(activeData["Volume"])
        
        self.setCurrentVirtualFitSelection(selected_vf_result)

        # TODO: There should be a separate radio button for indicating the 'chosen' result for tracking
        self.logic.chosen_virtual_fit = selected_vf_result #Temporary functionality till radio buttons are added

        self.updateVirtualfitButtons()

    def onEditTransformClicked(self):

        selected_vf_result = self.getCurrentVirtualFitSelection()
        if selected_vf_result is None:
            self.ui.editTransformPushButton.checked = False
            return
        selected_transducer = self.algorithm_input_widget.get_current_data()["Transducer"]

        if not selected_vf_result.GetDisplayNode().GetEditorVisibility():
            self.enable_manual_interaction(selected_transducer, selected_vf_result)
        else:
            self.disable_manual_interaction(selected_transducer, selected_vf_result)

    def onRemoveVirtualFitClicked(self, checked: bool = False):
        selected_vf_result = self.getCurrentVirtualFitSelection()
        if selected_vf_result is None:
            raise RuntimeError("It should not be possible to click Remove virtual fit while there is not a valid virtual fit result selected.")
        self.unwatchVirtualFit(selected_vf_result)
        slicer.mrmlScene.RemoveNode(selected_vf_result)

        # Update the underlying session so persisted virtual fit results stay in sync with the scene.
        if get_openlifu_data_parameter_node().loaded_session is not None:
            data_logic: "OpenLIFUDataLogic" = slicer.util.getModuleLogic('OpenLIFUData')
            data_logic.update_underlying_openlifu_session()

        self.updateVirtualFitResultsTable()
        self.updateWorkflowControls()

    def enable_manual_interaction(self, transducer: SlicerOpenLIFUTransducer, vf_result_node: vtkMRMLTransformNode):
        self.ui.editTransformPushButton.text = "Done Editing"
        self.ui.editTransformPushButton.checked = True
        self.ui.editTransformPushButton.setToolTip("")
        
        self._vf_interaction_in_progress = True # Needed to prevent unwanted update routines

        # Temporarily observe selected result
        transducer.model_node.SetAndObserveTransformNodeID(vf_result_node.GetID())
        if transducer.body_model_node:
            transducer.body_model_node.SetAndObserveTransformNodeID(vf_result_node.GetID())
        if transducer.surface_model_node:
            transducer.surface_model_node.SetAndObserveTransformNodeID(vf_result_node.GetID())
        vf_result_node.GetDisplayNode().SetEditorVisibility(True)

        # Disable other VF functionality
        self.ui.virtualFitResultTable.enabled = False 
        self.updateVirtualfitButtons()
        self.updateWorkflowControls()
    
    def disable_manual_interaction(self, transducer: SlicerOpenLIFUTransducer, vf_result_node: vtkMRMLTransformNode):
        self.ui.editTransformPushButton.text = "Edit"
        self.ui.editTransformPushButton.checked = False
        self.ui.editTransformPushButton.setToolTip("Edit the selected transform via interaction handles in the 3D view")
        
        self._vf_interaction_in_progress = False # Needed to prevent unwanted update routines

        # Update current transform
        transducer.set_current_transform_to_match_transform_node(vf_result_node)
        transducer.model_node.SetAndObserveTransformNodeID(transducer.transform_node.GetID())
        if transducer.body_model_node:
            transducer.body_model_node.SetAndObserveTransformNodeID(transducer.transform_node.GetID())
        if transducer.surface_model_node:
            transducer.surface_model_node.SetAndObserveTransformNodeID(transducer.transform_node.GetID())

        vf_result_node.GetDisplayNode().SetEditorVisibility(False)

        # Enable other VF functionality
        self.ui.virtualFitResultTable.enabled = True
        self.updateVirtualfitButtons()
        self.updateWorkflowControls()
    
    def get_currently_active_interaction_node(self) -> List[vtkMRMLTransformNode]:
        """Returns a list of virtual fit result nodes with interaction handles enabled."""

        activeData = self.algorithm_input_widget.get_current_data() 
        target = activeData["Target"]
        session = get_openlifu_data_parameter_node().loaded_session
        session_id : Optional[str] = session.get_session_id() if session is not None else None

        if not target:
            return []

        target_id = fiducial_to_openlifu_point_id(target)
        vf_results = list(get_virtual_fit_result_nodes(target_id=target_id, session_id=session_id, sort=True))

        active_nodes = [
            node for node in vf_results
            if node.GetDisplayNode() is not None and node.GetDisplayNode().GetEditorVisibility()
        ]

        return active_nodes

    def watchVirtualFit(self, virtual_fit_transform_node : vtkMRMLTransformNode):
        """Watch the virtual fit transform node to revoke approval in case the transform node is approved and then modified."""
        self.node_observations[virtual_fit_transform_node.GetID()].append(virtual_fit_transform_node.AddObserver(
            slicer.vtkMRMLTransformNode.TransformModifiedEvent,
            lambda node, event: self.revokeVirtualFitApprovalIfAny(node, reason="The virtual fit transform was modified.")))
        
    def unwatchVirtualFit(self, virtual_fit_transform_node : vtkMRMLTransformNode):
        """Un-does watchVirtualFit; see watchVirtualFit."""
        if virtual_fit_transform_node.GetID() not in self.node_observations:
            return
        for tag in self.node_observations.pop(virtual_fit_transform_node.GetID()):
            virtual_fit_transform_node.RemoveObserver(tag)

#
# OpenLIFUPrePlanningLogic
#


class OpenLIFUPrePlanningLogic(ScriptedLoadableModuleLogic):
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

        self._chosen_virtual_fit = None
        """The currently chosen virtual fit result to be used for tracking. Do not set this directly -- use the `chosen_virtual_fit` property."""

        self._on_chosen_virtual_fit_changed_callbacks : List[Callable[[Optional[vtkMRMLTransformNode]],None]] = []
        """List of functions to call when `chosen_virtual_fit` property is changed."""

    def getParameterNode(self):
        return OpenLIFUPrePlanningParameterNode(super().getParameterNode())

    def call_on_chosen_virtual_fit_changed(self, f : Callable[[Optional[vtkMRMLTransformNode]],None]) -> None:
        """Set a function to be called whenever the `chosen_virtual_fit` property is changed.
        The provided callback should accept a single argument which will be the new chosen virtual fit result (or None).
        """
        self._on_chosen_virtual_fit_changed_callbacks.append(f)

    @property
    def chosen_virtual_fit(self) -> Optional[vtkMRMLTransformNode]:
        """The currently chosen virtual fit result that will be used for transducer tracking.

        Callbacks registered with `call_on_chosen_virtual_fit_changed` will be invoked when the virtual fit changes.

        """
        return self._chosen_virtual_fit

    @chosen_virtual_fit.setter
    def chosen_virtual_fit(self, transform_node : Optional[vtkMRMLTransformNode]):
        self._chosen_virtual_fit = transform_node
        for f in self._on_chosen_virtual_fit_changed_callbacks:
            f(self._chosen_virtual_fit)

    def get_approved_target_ids(self) -> List[str]:
        """Return a list of target IDs that have approved virtual fit, for the currently active session.
        Or if there is no session, then sessionless approved target IDs are returned."""
        data_logic : "OpenLIFUDataLogic" = slicer.util.getModuleLogic('OpenLIFUData')
        session_id = None if not data_logic.validate_session() else data_logic.getParameterNode().loaded_session.get_session_id()
        approved_target_ids = get_approved_target_ids(session_id=session_id)
        return approved_target_ids

    def clear_virtual_fit_results(self, target: vtkMRMLMarkupsFiducialNode):
        """Remove all virtual fit results nodes from the scene that match the given target for the currently active session.
        Or if there is no session, then sessionless results are cleared."""
        session = get_openlifu_data_parameter_node().loaded_session
        session_id : Optional[str] = session.get_session_id() if session is not None else None
        target_id = fiducial_to_openlifu_point_id(target)
        clear_virtual_fit_results(target_id=target_id,session_id=session_id)

    def toggle_virtual_fit_approval(self, node: vtkMRMLTransformNode) -> bool:
        """Toggle approval for the given virtual fit result node and return
        the updated approval status."""

        is_approved = get_approval_from_virtual_fit_result_node(node)
        set_approval_for_virtual_fit_result_node(
            approval_state=not is_approved,
            vf_result_node = node)
        data_logic : "OpenLIFUDataLogic" = slicer.util.getModuleLogic('OpenLIFUData')
        data_logic.update_underlying_openlifu_session()

        return not is_approved

    def find_best_virtual_fit_result_for_target(self, target_id: str) -> vtkMRMLTransformNode:
        session = get_openlifu_data_parameter_node().loaded_session
        session_id = None if session is None else session.get_session_id()
        virtual_fit_result = get_best_virtual_fit_result_node(target_id=target_id, session_id=session_id)
        return virtual_fit_result
    
    def find_approved_virtual_fit_results_for_target(self, target_id: str) -> vtkMRMLTransformNode:
        session = get_openlifu_data_parameter_node().loaded_session
        session_id = None if session is None else session.get_session_id()
        virtual_fit_results = list(get_virtual_fit_result_nodes(target_id=target_id, session_id=session_id, approved_only = True))
        return virtual_fit_results

    def get_virtual_fit_approval(self, target_id : str) -> bool:
        """Return whether there is a virtual fit approval for the target. In case there is not even a virtual
        fit result for the target, this returns False."""
        virtual_fit_result = self.find_best_virtual_fit_result_for_target(target_id=target_id)
        if virtual_fit_result is None:
            return False
        return get_approval_from_virtual_fit_result_node(virtual_fit_result)

    def revoke_virtual_fit_approval(self, target_id : str):
        session = get_openlifu_data_parameter_node().loaded_session
        session_id = None if session is None else session.get_session_id()
        revoke_any_virtual_fit_approvals_for_target(target_id=target_id, session_id=session_id)
        data_logic : "OpenLIFUDataLogic" = slicer.util.getModuleLogic('OpenLIFUData')
        data_logic.update_underlying_openlifu_session()

    def create_manual_virtual_fit_result(
        self,
        transducer : SlicerOpenLIFUTransducer,
        volume: vtkMRMLScalarVolumeNode,
        target: vtkMRMLMarkupsFiducialNode,
        ) -> vtkMRMLTransformNode:

        with BusyCursor():
            # Get the skin mesh associated with the volume
            skin_mesh_node = get_skin_segmentation(volume)
            if skin_mesh_node is None:
                skin_mesh_node = generate_skin_segmentation(volume)

        session = get_openlifu_data_parameter_node().loaded_session
        session_id : Optional[str] = session.get_session_id() if session is not None else None

        target_id = fiducial_to_openlifu_point_id(target)

        existing_vf_results = list(get_virtual_fit_result_nodes(target_id=target_id, session_id=session_id, sort = True))
        
        if not existing_vf_results:
            rank = 1
        else:
            current_lowest_rank = int(existing_vf_results[-1].GetAttribute("VF:rank"))
            rank = current_lowest_rank + 1

        vf_result_node = add_virtual_fit_result(
                transform_node = transducer.transform_node,
                target_id = target_id,
                session_id = session_id,
                approval_status = False,
                clone_node=True, # Important. Initialize based on the current transducer position
                rank = rank, 
        )

        return vf_result_node

    def virtual_fit(
        self,
        protocol: SlicerOpenLIFUProtocol,
        transducer : SlicerOpenLIFUTransducer,
        volume: vtkMRMLScalarVolumeNode,
        target: vtkMRMLMarkupsFiducialNode,
        progress_callback : Callable[[int,str],None],
        include_debug_info : bool,
    ) -> Optional[vtkMRMLTransformNode]:

        add_slicer_log_handler("VirtualFit", "Virtual fitting")

        transducer_openlifu : "openlifu.xdc.Transducer" = transducer.transducer.transducer
        protocol_openlifu : "openlifu.plan.Protocol" = protocol.protocol

        units = "mm" # These are the units of the output space of the transform returned by get_IJK2RAS

        # Get the skin mesh associated with the volume
        skin_mesh_node = get_skin_segmentation(volume)
        if skin_mesh_node is None:
            skin_mesh_node = generate_skin_segmentation(volume)

        import openlifu.seg
        import threadpoolctl

        with threadpoolctl.threadpool_limits(limits=1): # caps BLAS and OpenMP threads
            # Capping BLAS threads appears to have a performance improvement when running this algorithm in Slicer.
            # This may be because Slicer already occupies BLAS threads with its VTK/ITK stuff and so the virtual fit's many
            # tiny svd calls end up having more overhead than is worth it.
            # For some unknown reason, the improvement is only noticable when we do not use the embree
            # option in virtual fitting, which makes things very fast.
            vf_transforms = openlifu.seg.run_virtual_fit(
                units = units,
                target_RAS = target.GetNthControlPointPosition(0),
                standoff_transform = transducer_openlifu.get_standoff_transform_in_units(units),
                options = protocol_openlifu.virtual_fit_options,
                skin_mesh = skin_mesh_node.GetPolyData(),
                progress_callback = progress_callback,
                include_debug_info = include_debug_info,
            )
        if include_debug_info:
            vf_transforms, debug_info = vf_transforms # In this case two things were actually returned, the first of which is the list of transforms
            self.load_vf_debugging_info(debug_info)

        session = get_openlifu_data_parameter_node().loaded_session
        session_id : Optional[str] = session.get_session_id() if session is not None else None

        target_id = fiducial_to_openlifu_point_id(target)
        self.clear_virtual_fit_results(target = target) # TODO: This should only clear the previously computed automatic ones

        existing_vf_results = list(get_virtual_fit_result_nodes(target_id=target_id, session_id=session_id, sort = True))
        
        if not existing_vf_results:
            current_lowest_rank = 0
        else:
            current_lowest_rank = int(existing_vf_results[-1].GetAttribute("VF:rank"))

        vf_result_nodes = []

        for i,vf_transform in enumerate(vf_transforms): 
            node = add_virtual_fit_result(
                transform_node = transducer_transform_node_from_openlifu(vf_transform, transducer.transducer.transducer, "mm"),
                target_id = target_id,
                session_id = session_id,
                approval_status = False,
                clone_node=False,
                rank = current_lowest_rank+i+1,
            )
            vf_result_nodes.append(node)
            transducer.move_node_into_transducer_sh_folder(node)
        if len(vf_result_nodes)==0:
            return None
        return vf_result_nodes[0]

    def load_vf_debugging_info(self, debug_info : "openlifu.seg.virtual_fit.VirtualFitDebugInfo") -> None:
        """Load virtual fit debugging info into the Slicer scene."""
        skin_node = slicer.mrmlScene.AddNewNodeByClass("vtkMRMLModelNode")
        skin_node.SetAndObservePolyData(debug_info.skin_mesh)
        skin_node.SetName("VF-debug-skin")

        interpolated_skin_node = slicer.mrmlScene.AddNewNodeByClass("vtkMRMLModelNode")
        interpolated_skin_node.SetAndObservePolyData(debug_info.spherically_interpolated_mesh)
        interpolated_skin_node.SetName("VF-debug-spherically-interpolated-skin")

        for points, scalars, vectors, node_name, visible in [
            (debug_info.search_points, debug_info.steering_dists, None, 'VF-debug-search-points-all', True),
            (debug_info.search_points[debug_info.in_bounds], debug_info.steering_dists[debug_info.in_bounds], None, 'VF-debug-search-points-in-bounds', False),
            (debug_info.search_points, debug_info.steering_dists, -debug_info.plane_normals, 'VF-debug-fitted_plane-normals', False),
        ]:
            points_vtk = vtk.vtkPoints()
            for pt in points:
                points_vtk.InsertNextPoint(pt)

            points_polydata = vtk.vtkPolyData()
            points_polydata.SetPoints(points_vtk)

            scalar_array = vtk.vtkDoubleArray()
            scalar_array.SetName('steeringDist')
            scalar_array.SetNumberOfTuples(points.shape[0])
            for i, v in enumerate(scalars):
                scalar_array.SetValue(i, float(v))
            points_polydata.GetPointData().AddArray(scalar_array)
            points_polydata.GetPointData().SetActiveScalars('steeringDist')

            if vectors is not None:
                vector_array = vtk.vtkDoubleArray()
                vector_array.SetName('planeNormal')
                vector_array.SetNumberOfComponents(3)
                vector_array.SetNumberOfTuples(vectors.shape[0])
                for i, vec in enumerate(vectors):
                    vector_array.SetTuple(i, vec)
                points_polydata.GetPointData().AddArray(vector_array)
                points_polydata.GetPointData().SetActiveVectors('planeNormal')

                arrow = vtk.vtkArrowSource()
                glyph = vtk.vtkGlyph3D()
                glyph.SetSourceConnection(arrow.GetOutputPort())
                glyph.SetInputData(points_polydata)
                glyph.SetVectorModeToUseVector()
                glyph.SetScaleModeToScaleByVector()
                glyph.SetScaleFactor(5.0)
                glyph.OrientOn()
                glyph.Update()

            else:
                sphere = vtk.vtkSphereSource()
                sphere.SetRadius(1.0)  # marker size
                glyph = vtk.vtkGlyph3D()
                glyph.SetSourceConnection(sphere.GetOutputPort())
                glyph.SetInputData(points_polydata)
                glyph.SetColorModeToColorByScalar()
                glyph.SetScaleModeToDataScalingOff()
                glyph.Update()

            points_model_node = slicer.mrmlScene.AddNewNodeByClass('vtkMRMLModelNode', node_name)
            points_model_node.SetAndObservePolyData(glyph.GetOutput())

            points_model_node.CreateDefaultDisplayNodes()
            disp = points_model_node.GetDisplayNode()
            disp.SetActiveScalarName('steeringDist')
            disp.SetAndObserveColorNodeID("vtkMRMLColorTableNodeRainbow")
            disp.SetScalarVisibility(True)
            disp.SetScalarRange(float(scalars.min()), float(scalars.max()))

            disp.SetVisibility(visible)

        self.debug_info=debug_info # TODO REMOVE. FOr now I use it like this: debug_info = slicer.modules.OpenLIFUPrePlanningWidget.logic.debug_info

#
# OpenLIFUPrePlanningTest
#

class OpenLIFUPrePlanningTest(ScriptedLoadableModuleTest):
    """
    This is the test case for your scripted module.
    Uses ScriptedLoadableModuleTest base class, available at:
    https://github.com/Slicer/Slicer/blob/main/Base/Python/slicer/ScriptedLoadableModule.py
    """

    def _workflow_virtual_fit(self):
        """Test running virtual fit and approving results."""

        slicer.util.selectModule("OpenLIFUPrePlanning")
        preplanning_widget = slicer.modules.OpenLIFUPrePlanningWidget
        preplanning_logic = preplanning_widget.logic

        # Get the example target loaded in the scene
        example_target = get_target_candidates()[0]
        target_id = fiducial_to_openlifu_point_id(example_target)
        curr_pos = example_target.GetNthControlPointPositionWorld(0)

        # Validate session and run virtual fit
        session = get_openlifu_data_parameter_node().loaded_session
        session_id = None if session is None else session.get_session_id()
        assert session_id is not None
        preplanning_widget.create_virtual_fit_result(auto_fit = True)

        # Confirm that virtual fit result exists
        vf_nodes = list(get_virtual_fit_result_nodes(target_id, session_id))
        num_vf_results = session.get_protocol().protocol.virtual_fit_options.top_n_candidates
        assert len(vf_nodes) == num_vf_results

        assert get_approval_from_virtual_fit_result_node(vf_nodes[0]) is False
        preplanning_logic.toggle_virtual_fit_approval(vf_nodes[0])
        assert get_approval_from_virtual_fit_result_node(vf_nodes[0]) is True

        approved_targets = preplanning_logic.get_approved_target_ids() 
        assert len(approved_targets) == 1
        assert approved_targets[0] == target_id

        # Change target position
        example_target.SetNthControlPointPositionWorld(0, (curr_pos[0], curr_pos[1], curr_pos[2]+0.1)) # this should clear the results
        slicer.app.processEvents()
        assert list(get_virtual_fit_result_nodes(target_id, session_id)) == []

        preplanning_widget.create_virtual_fit_result(auto_fit = False)
        vf_nodes = list(get_virtual_fit_result_nodes(target_id, session_id))
        assert len(vf_nodes) == 1

        # --- One-approved-per-target UI gating ---------------------------------------------------
        # Run virtual fit again so we have multiple candidates to exercise the disable behavior.
        preplanning_widget.create_virtual_fit_result(auto_fit = True)
        vf_nodes = list(get_virtual_fit_result_nodes(target_id, session_id, sort=True))
        assert len(vf_nodes) >= 2, "Expected multiple VF candidates from auto-fit."

        # No approvals yet -> every Approved cell must be user-checkable.
        preplanning_widget.updateVirtualFitResultsTable()
        table = preplanning_widget.ui.virtualFitResultTable
        for row in range(table.rowCount):
            flags = int(table.item(row, 1).flags())
            assert flags & int(qt.Qt.ItemIsEnabled), f"Row {row} approve cell should be enabled when nothing is approved."

        # Approve the first row -> all other rows' Approved cells must lose ItemIsEnabled.
        preplanning_logic.toggle_virtual_fit_approval(vf_nodes[0])
        slicer.app.processEvents()
        approved_row = None
        for row in range(table.rowCount):
            item = table.item(row, 1)
            if item.checkState() == qt.Qt.Checked:
                approved_row = row
                break
        assert approved_row is not None, "Expected one approved row after toggling approval."
        for row in range(table.rowCount):
            flags = int(table.item(row, 1).flags())
            is_enabled = bool(flags & int(qt.Qt.ItemIsEnabled))
            if row == approved_row:
                assert is_enabled, "Approved row should remain user-checkable so it can be un-approved."
            else:
                assert not is_enabled, f"Row {row} approve cell should be disabled while another row is approved."

        # Un-approving releases the lock -> every cell becomes user-checkable again.
        preplanning_logic.toggle_virtual_fit_approval(vf_nodes[0])
        slicer.app.processEvents()
        for row in range(table.rowCount):
            flags = int(table.item(row, 1).flags())
            assert flags & int(qt.Qt.ItemIsEnabled), f"Row {row} approve cell should be enabled after revoking all approvals."

        # --- All-targets-approved proceed gating -------------------------------------------------
        # Re-approve so the single existing target has an approved VF.
        preplanning_logic.toggle_virtual_fit_approval(vf_nodes[0])
        slicer.app.processEvents()
        preplanning_widget.updateWorkflowControls()
        assert preplanning_widget.workflow_controls.can_proceed is True, \
            "Should be proceedable once every (single) target has an approved VF."

        # Add a second target with no VF -> proceed must be blocked with the per-target message.
        extra_target = slicer.mrmlScene.AddNewNodeByClass("vtkMRMLMarkupsFiducialNode")
        extra_target.SetMaximumNumberOfControlPoints(1)
        extra_target.SetName(slicer.mrmlScene.GenerateUniqueName("Target"))
        extra_target.SetMarkupLabelFormat("%N")
        extra_target.AddControlPoint(curr_pos[0] + 5.0, curr_pos[1], curr_pos[2])
        slicer.app.processEvents()
        try:
            preplanning_widget.updateWorkflowControls()
            assert preplanning_widget.workflow_controls.can_proceed is False, \
                "Adding an unfit target should block Proceed."
            assert "every target" in preplanning_widget.workflow_controls.status_text.lower(), \
                f"Unexpected status text: {preplanning_widget.workflow_controls.status_text!r}"
        finally:
            slicer.mrmlScene.RemoveNode(extra_target)
            preplanning_widget.updateWorkflowControls()
