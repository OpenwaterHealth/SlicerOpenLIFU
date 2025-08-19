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
    get_openlifu_data_parameter_node,
    get_target_candidates,
    openlifu_lz,
    threadpoolctl_lz,
)
from OpenLIFULib.coordinate_system_utils import get_IJK2RAS
from OpenLIFULib.events import SlicerOpenLIFUEvents
from OpenLIFULib.guided_mode_util import GuidedWorkflowMixin
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
    import openlifu.virtual_fit
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

    def setup(self) -> None:
        """Called when the user opens the module the first time and the widget is initialized."""
        ScriptedLoadableModuleWidget.setup(self)

        # Mapping from mrml node ID to a list of vtkCommand tags that can later be used to remove the observation
        self.node_observations : Dict[str,List[int]] = defaultdict(list)

        # Load widget from .ui file (created by Qt Designer).
        # Additional widgets can be instantiated manually and added to self.layout.
        uiWidget = slicer.util.loadUI(self.resourcePath("UI/OpenLIFUPrePlanning.ui"))
        self.layout.addWidget(uiWidget)
        self.ui = slicer.util.childWidgetVariables(uiWidget)


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

        # User account banner widget replacement. Note: the visibility is
        # initialized to false because this widget will *always* exist before
        # the login module parameter node.
        self.user_account_banner = UserAccountBanner(parent=self.ui.userAccountBannerPlaceholder.parentWidget())
        replace_widget(self.ui.userAccountBannerPlaceholder, self.user_account_banner, self.ui)
        self.user_account_banner.visible = False

        # ---- Inject guided mode workflow controls ----

        self.inject_workflow_controls_into_placeholder()

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

        self.ui.targetListWidget.currentItemChanged.connect(self.onTargetListWidgetCurrentItemChanged)
        self.ui.targetListWidget.itemChanged.connect(self.onTargetListWidgetItemDataChanged)

        position_coordinate_validator = qt.QDoubleValidator(slicer.util.mainWindow())
        position_coordinate_validator.setNotation(qt.QDoubleValidator.StandardNotation)
        self.targetPositionInputs = [
            self.ui.positionRLineEdit,
            self.ui.positionALineEdit,
            self.ui.positionSLineEdit,
        ]
        for positionLineEdit in self.targetPositionInputs:
            positionLineEdit.setValidator(position_coordinate_validator)
            positionLineEdit.editingFinished.connect(self.onTargetPositionEditingFinished)

        # Watch any fiducial nodes that already existed before this module was set up
        for fiducial_node in slicer.util.getNodesByClass("vtkMRMLMarkupsFiducialNode"):
            self.watch_fiducial_node(fiducial_node)

        self.resetVirtualFitProgressDisplay()
        self.updateTargetsListView()
        self.updateInputOptions()
        self.updateApprovalStatusLabel()
        self.updateEditTargetEnabled()
        self.updateTargetPositionInputs()
        self.updateLockButtonIcon()

        self.ui.newTargetButton.clicked.connect(self.onNewTargetClicked)
        self.ui.removeTargetButton.clicked.connect(self.onremoveTargetClicked)
        self.ui.lockButton.clicked.connect(self.onLockClicked)
        self.ui.approveButton.clicked.connect(self.onApproveClicked)
        self.ui.virtualfitButton.clicked.connect(self.onRunAutoFitClicked)

        # ---- Virtual fit result options ----
        # self.ui.virtualFitResultTable.itemClicked.connect(self.onVirtualFitResultSelected)
        self.ui.virtualFitResultTable.itemSelectionChanged.connect(self.onVirtualFitResultSelected)
        self.ui.modifyTransformPushButton.clicked.connect(self.onModifyTransformClicked)
        self.ui.modifyTransformPushButton.setStyleSheet("""
        QPushButton:checked {
        border: 2px solid green; 
        background-color: lightgray; 
        padding: 4px;
        }
        """)
        self.ui.modifyTransformPushButton.setToolTip("Modify virtual fit transform")
        self.ui.addTransformPushButton.clicked.connect(self.onAddVirtualFitResultClicked)
        self.ui.addTransformPushButton.setToolTip("Create new virtual fit result")
        self.updateVirtualFitResultsTable()
        slicer.util.getModule("OpenLIFUTransducerTracker").widgetRepresentation() 
        self.logic.call_on_chosen_virtual_fit_changed(slicer.modules.OpenLIFUTransducerTrackerWidget.setVirtualFitResultForTracking)
        # ------------------------------------

        self.updateWorkflowControls()

    def cleanup(self) -> None:
        """Called when the application closes and the module widget is destroyed."""
        self.removeObservers()

    def enter(self) -> None:
        """Called each time the user opens this module."""
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

        self.updateTargetsListView()
        self.updateInputOptions()

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

        self.updateTargetsListView()
        self.updateInputOptions()

    def watch_fiducial_node(self, node:vtkMRMLMarkupsFiducialNode):
        """Add observers so that point-list changes in this fiducial node are tracked by the module."""
        self.node_observations[node.GetID()].append(node.AddObserver(slicer.vtkMRMLMarkupsNode.PointAddedEvent,partial(self.onPointAddedOrRemoved, node)))
        self.node_observations[node.GetID()].append(node.AddObserver(slicer.vtkMRMLMarkupsNode.PointRemovedEvent,partial(self.onPointAddedOrRemoved, node)))
        self.node_observations[node.GetID()].append(node.AddObserver(slicer.vtkMRMLMarkupsNode.PointModifiedEvent,partial(self.onPointModified, node)))
        self.node_observations[node.GetID()].append(node.AddObserver(slicer.vtkMRMLMarkupsNode.LockModifiedEvent,self.onLockModified))
        self.node_observations[node.GetID()].append(node.AddObserver(SlicerOpenLIFUEvents.TARGET_NAME_MODIFIED_EVENT,self.onTargetNameModified))

    def unwatch_fiducial_node(self, node:vtkMRMLMarkupsFiducialNode):
        """Un-does watch_fiducial_node; see watch_fiducial_node."""
        if node.GetID() not in self.node_observations:
            return
        for tag in self.node_observations.pop(node.GetID()):
            node.RemoveObserver(tag)

    def onPointAddedOrRemoved(self, node:vtkMRMLMarkupsFiducialNode, caller, event):
        self.updateTargetsListView()
        self.updateInputOptions()
        self.updateWorkflowControls()
        data_logic : "OpenLIFUDataLogic" = slicer.util.getModuleLogic('OpenLIFUData')
        if not data_logic.session_loading_unloading_in_progress and not slicer.util.getModuleWidget("OpenLIFUTransducerTracker")._running_wizard:
            reason = "The target was modified."
            self.revokeTargetApprovalIfAny(node, reason=reason)
            self.clearVirtualFitResultsIfAny(node, reason = reason)
            slicer.util.getModuleWidget('OpenLIFUSonicationPlanner').deleteSolutionAndSolutionAnalysisIfAny(reason=reason)
                        
    def onPointModified(self, node:vtkMRMLMarkupsFiducialNode, caller, event):
        self.updateTargetPositionInputs()

        data_logic : "OpenLIFUDataLogic" = slicer.util.getModuleLogic('OpenLIFUData')
        if not data_logic.session_loading_unloading_in_progress and not slicer.util.getModuleWidget("OpenLIFUTransducerTracker")._running_wizard:
            reason = "The target was modified."
            self.revokeTargetApprovalIfAny(node, reason=reason)
            self.clearVirtualFitResultsIfAny(node, reason = reason)
            slicer.util.getModuleWidget('OpenLIFUSonicationPlanner').deleteSolutionAndSolutionAnalysisIfAny(reason=reason)

    def onLockModified(self, caller, event):
        self.updateLockButtonIcon()
        self.updateEditTargetEnabled()

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
            self.updateApprovalStatusLabel()

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
            self.updateApprovalStatusLabel()

            # Need this because updates to the data parameter node resets the combo box 
            self.setCurrentVirtualFitSelection(node)
            self.updateVirtualfitButtons()

    def updateTargetsListView(self):
        """Update the list of targets in the target management UI"""
        currently_selected_row = self.ui.targetListWidget.currentRow
        
        self.ui.targetListWidget.clear()
        for target_node in get_target_candidates():
            item = qt.QListWidgetItem(target_node.GetName())
            item.setFlags(item.flags() | qt.Qt.ItemIsEditable) # Make it possible to click and rename items
            item.setData(qt.Qt.UserRole, target_node)
            self.ui.targetListWidget.addItem(item)
        
        if currently_selected_row == -1:
            self.ui.targetListWidget.setCurrentRow(0)
        else:
            self.ui.targetListWidget.setCurrentRow(currently_selected_row)

    def getTargetsListViewCurrentSelection(self) -> Optional[vtkMRMLMarkupsFiducialNode]:
        """Get the fiducial node associated to the currently selected target in the list view;
        returns None if nothing is selected."""
        item = self.ui.targetListWidget.currentItem()
        if item is None:
            return None
        return item.data(qt.Qt.UserRole)

    def selectTargetByID(self, fiducial_node_mrml_id:str):
        """Set the currently selected target in the targets list widget to the one with the given ID, if it is there.
        If it is not there then then the selection is unaffected."""
        for i in range(self.ui.targetListWidget.count):
            item = self.ui.targetListWidget.item(i)
            if item.data(qt.Qt.UserRole).GetID() == fiducial_node_mrml_id:
                self.ui.targetListWidget.setCurrentItem(item)
                break

    def onTargetListWidgetCurrentItemChanged(self, current:qt.QListWidgetItem, previous:qt.QListWidgetItem):
        self.updateEditTargetEnabled()
        self.updateTargetPositionInputs()
        self.updateLockButtonIcon()

    def onTargetListWidgetItemDataChanged(self, item:qt.QListWidgetItem):
        node : vtkMRMLMarkupsFiducialNode = item.data(qt.Qt.UserRole)
        node.SetName(item.text().replace(" ", "-")) # This becomes openlifu Point ID
        node.SetNthControlPointLabel(0, item.text()) # This becomes openlifu Point name
        node.InvokeEvent(SlicerOpenLIFUEvents.TARGET_NAME_MODIFIED_EVENT)

    def onTargetNameModified(self, caller, event):
        self.updateInputOptions()

    def onDataParameterNodeModified(self,caller, event) -> None:
        self.updateInputOptions() 
        self.updateWorkflowControls()
        self.updateVirtualFitRelatedLabels()

    def updateVirtualFitRelatedLabels(self):
        """ When virtual fit approval is revoked or toggled, the messages displayed 
        in the data module, transducer tracking module and pre-planning module need to be updated."""
        self.updateApprovalStatusLabel()
        slicer.modules.OpenLIFUDataWidget.updateSessionStatus()  
        slicer.modules.OpenLIFUTransducerTrackerWidget.updateVirtualFitStatus() 

    def updateEditTargetEnabled(self):
        """Update whether the controls that edit targets are enabled"""
        current_selection = self.getTargetsListViewCurrentSelection()
        target_position_inputs_enabled = (current_selection is not None) and (not current_selection.GetLocked())
        target_deletion_and_locking_enabled = current_selection is not None
        for widget in self.targetPositionInputs:
            widget.setEnabled(target_position_inputs_enabled)
        for widget in [self.ui.removeTargetButton, self.ui.lockButton]:
            widget.setEnabled(target_deletion_and_locking_enabled)

    def onNewTargetClicked(self):
        # If we are already in point placement mode then do nothing
        if slicer.mrmlScene.GetNodeByID("vtkMRMLInteractionNodeSingleton").GetCurrentInteractionMode() == PLACE_INTERACTION_MODE_ENUM_VALUE:
            return

        node = slicer.mrmlScene.AddNewNodeByClass("vtkMRMLMarkupsFiducialNode")
        node.SetMaximumNumberOfControlPoints(1)
        node.SetName(slicer.mrmlScene.GenerateUniqueName("Target"))
        node.SetMarkupLabelFormat("%N")

        slicer.modules.markups.logic().StartPlaceMode(
            False # "place mode persistence" set to False means we want to place one target and then stop
        )

        self.updateWorkflowControls()

    def onremoveTargetClicked(self):
        node = self.getTargetsListViewCurrentSelection()
        if node is None:
            raise RuntimeError("It should not be possible to click Remove target while there is not a valid target selected.")
        slicer.mrmlScene.RemoveNode(node)

        self.updateWorkflowControls()

    def updateTargetPositionInputs(self):
        node = self.getTargetsListViewCurrentSelection()

        if node is None:
            for positionLineEdit in self.targetPositionInputs:
                positionLineEdit.text = ""
            return

        position_ras = node.GetNthControlPointPosition(0)
        for coord_value, positionLineEdit in zip(position_ras,self.targetPositionInputs):
            if not positionLineEdit.hasFocus():
                # If the RAS coordinates are not being input by the user, round what is displayed for easier reading.
                # Note that this only affects what is displayed and isn't actually rounding the position of the point.
                coord_value = f"{coord_value:0.2f}"

            positionLineEdit.text = coord_value

    def onTargetPositionEditingFinished(self):
        try:
            new_ras_position = [float(positionLineEdit.text) for positionLineEdit in self.targetPositionInputs]
        except ValueError: # The text was not convertible float (e.g blank input)
            return
        node = self.getTargetsListViewCurrentSelection()
        node.SetNthControlPointPosition(0,*new_ras_position)

    def updateLockButtonIcon(self):
        node = self.getTargetsListViewCurrentSelection()
        if node is None:
            self.ui.lockButton.setIcon(qt.QIcon())
            self.ui.lockButton.setToolTip("")
            return
        if node.GetLocked():
            self.ui.lockButton.setIcon(qt.QIcon(":Icons/Medium/SlicerLock.png"))
            self.ui.lockButton.setToolTip("Target locked. Click to unlock moving the target.")
        else:
            self.ui.lockButton.setIcon(qt.QIcon(":Icons/Medium/SlicerUnlock.png"))
            self.ui.lockButton.setToolTip("Target unlocked. Click to lock target from being moved.")

    def onLockClicked(self):
        node = self.getTargetsListViewCurrentSelection()
        if node is None:
            raise RuntimeError("It should not be possible to click the lock button with no target selected.")
        node.SetLocked(not node.GetLocked())

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
                self.ui.approveButton,
                self.ui.modifyTransformPushButton,
                self.ui.addTransformPushButton,
            ]:
                button.enabled = False
                button.setToolTip("Specify all required inputs to enable virtual fitting")
            self.ui.modifyTransformPushButton.checked = False
        else:
            selected_vf_result = self.getCurrentVirtualFitSelection()
            currently_interacting = len(self.get_currently_active_interaction_node()) > 0

            if currently_interacting:
                for button in [
                    self.ui.virtualfitButton,
                    self.ui.approveButton,
                    self.ui.modifyTransformPushButton,
                    self.ui.addTransformPushButton,
                ]:
                    button.enabled = False
                    button.setToolTip("Finish modifying the transform first")
                self.ui.modifyTransformPushButton.enabled =True # Enabled because it is a "Finish" button
            
            else:
                self.ui.virtualfitButton.enabled = True
                self.ui.virtualfitButton.setToolTip("Run virtual fit algorithm to automatically suggest a transducer positioning." \
                    "Any existing virtual fit results for the selected target will be removed.")
                self.ui.addTransformPushButton.enabled=True
                self.ui.addTransformPushButton.setToolTip("Add a new transducer transform to the table, to be manually positioned.")
                
                self.ui.modifyTransformPushButton.checked = False

                if selected_vf_result is None:
                    for button in [
                        self.ui.modifyTransformPushButton,
                        self.ui.approveButton,
                    ]:
                        button.enabled = False
                        button.setToolTip("Select a virtual fit result on which to do this")
                else:
                    self.ui.modifyTransformPushButton.enabled = True
                    self.ui.modifyTransformPushButton.setToolTip("Modify the selected transform")
                    
                    self.ui.approveButton.enabled = True
                    approved : bool = get_approval_from_virtual_fit_result_node(selected_vf_result)                
                    if not approved:
                        self.ui.approveButton.setText("Approve virtual fit")
                        self.ui.approveButton.setToolTip("Approve the virtual fit result for the selected target")
                    else:
                        self.ui.approveButton.setText("Revoke virtual fit approval")
                        self.ui.approveButton.setToolTip("Revoke virtual fit approval for the selected target")

    def onApproveClicked(self):
        
        selected_vf_result = self.getCurrentVirtualFitSelection()
        if selected_vf_result is None:
            raise RuntimeError("No virtual fit result selected")
        approval_status = self.logic.toggle_virtual_fit_approval(selected_vf_result) # Triggers data parameter node modified
        if approval_status:
            self.watchVirtualFit(selected_vf_result)
        else:
            self.unwatchVirtualFit(selected_vf_result)

        # Restore the most recent selection if it's still valid
        self.setCurrentVirtualFitSelection(selected_vf_result)
        self.updateVirtualfitButtons()
        self.updateWorkflowControls()

    def updateApprovalStatusLabel(self):
        approved_target_ids = self.logic.get_approved_target_ids()
        if len(approved_target_ids) == 0:
            self.ui.approvalStatusLabel.text = "There are currently no virtual fit approvals."
        else:
            # Display the names of the approved VF results alongside each target
            formatted_targets = []
            for target_id in approved_target_ids:
                # Get the virtual fit results
                approved_results = self.logic.find_approved_virtual_fit_results_for_target(target_id)
                approved_results_names = [node.GetName() for node in approved_results]

                if not approved_results:
                    raise RuntimeError("Target cannot be approved without any approved virtual fit result nodes")
                # Join the results with commas and enclose in parentheses
                approved_result_name_strings = ", ".join(approved_results_names)
                formatted_targets.append(f"{target_id} ({approved_result_name_strings})")

            self.ui.approvalStatusLabel.text = (
                "Virtual fit is approved for the following targets:\n- "
                + "\n- ".join(formatted_targets)
            )

    def updateWorkflowControls(self):
        session = get_openlifu_data_parameter_node().loaded_session
        session_id = None if session is None else session.get_session_id()

        if session is None:
            self.workflow_controls.can_proceed = False
            self.workflow_controls.status_text = "If you are seeing this, guided mode is being run out of order! Load a session to proceed."
        if self._vf_interaction_in_progress:
            self.workflow_controls.can_proceed = False
            self.workflow_controls.status_text = "Finish modifying the virtual fit transform before proceeding."
        elif not get_target_candidates():
            self.workflow_controls.can_proceed = False
            self.workflow_controls.status_text = "Create a target to proceed."
        elif not list(get_virtual_fit_result_nodes(session_id=session_id)):
            self.workflow_controls.can_proceed = False
            self.workflow_controls.status_text = "Run a virtual fit result for a target to proceed."
        elif not self.logic.get_approved_target_ids():
            self.workflow_controls.can_proceed = False
            self.workflow_controls.status_text = "A virtual fit result needs to be approved for a target to proceed."
        else:
            self.workflow_controls.can_proceed = True
            self.workflow_controls.status_text = "Approved virtual fit result detected, proceed to the next step."

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

        most_recent_selection = self.ui.virtualFitResultTable.currentRow
        self.ui.virtualFitResultTable.clearContents()
        self.ui.virtualFitResultTable.setRowCount(0) # Remove all rows

        activeData = self.algorithm_input_widget.get_current_data()
        target = activeData["Target"]
        session = get_openlifu_data_parameter_node().loaded_session
        session_id : Optional[str] = session.get_session_id() if session is not None else None

        if not target:
            self.updateVirtualfitButtons()
            return

        target_id = fiducial_to_openlifu_point_id(target)
        vf_results = list(get_virtual_fit_result_nodes(target_id=target_id, session_id=session_id, sort = True))

        # Populate the table
        if vf_results:
            self.ui.virtualFitResultTable.setRowCount(len(vf_results)) 
            
            for row_idx, result in enumerate(vf_results):

                result_item = qt.QTableWidgetItem(result.GetName())
                self.ui.virtualFitResultTable.setItem(row_idx, 0, result_item)
                result_item.setData(qt.Qt.UserRole, result)

                approval_status = "True" if get_approval_from_virtual_fit_result_node(result) else "False"
                self.ui.virtualFitResultTable.setItem(row_idx, 1, qt.QTableWidgetItem(approval_status))

            # Restore the most recent selection if it's still valid
            if 0 <= most_recent_selection < self.ui.virtualFitResultTable.rowCount:
                self.ui.virtualFitResultTable.selectRow(most_recent_selection)
            
            self.ui.virtualFitResultTable.resizeRowsToContents()

        # If nothing is selected or no results are available
        if not vf_results:
            self.logic.chosen_virtual_fit = None
        
        self.updateVirtualfitButtons()

        slicer.modules.OpenLIFUTransducerTrackerWidget.setVirtualFitResultForTracking(self.logic.chosen_virtual_fit)

    def getCurrentVirtualFitSelection(self):
        """ Returns the virtual fit transform node associated with the current selection."""

        selected_items = self.ui.virtualFitResultTable.selectedItems()
        if not selected_items:
            return None
        selected_item = selected_items[0] # Get the first selected item (usually from col 0 if SelectRows)
        selected_vf_result = selected_item.data(qt.Qt.UserRole)
        if selected_vf_result is None:
            raise RuntimeError("No transform node found in association with the selected virtual fit result")
        return selected_vf_result

    def setCurrentVirtualFitSelection(self, node: vtkMRMLTransformNode):
        """ Selects the row associated with the given transform node in the results table.
        If different to the current selection, this updates the transducer position."""

        selected_item = self.ui.virtualFitResultTable.findItems(node.GetName(), qt.Qt.MatchExactly)
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

        self.updateApprovalStatusLabel()
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
        slicer.modules.OpenLIFUTransducerTrackerWidget.updateModelRenderingSettings()

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

    def onModifyTransformClicked(self):

        selected_vf_result = self.getCurrentVirtualFitSelection()
        if selected_vf_result is None:
            self.ui.modifyTransformPushButton.checked = False
            return
        selected_transducer = self.algorithm_input_widget.get_current_data()["Transducer"]

        if not selected_vf_result.GetDisplayNode().GetEditorVisibility():
            self.enable_manual_interaction(selected_transducer, selected_vf_result)
        else:
            self.disable_manual_interaction(selected_transducer, selected_vf_result)
    
    def enable_manual_interaction(self, transducer: SlicerOpenLIFUTransducer, vf_result_node: vtkMRMLTransformNode):
        self.ui.modifyTransformPushButton.text = "Finish"
        self.ui.modifyTransformPushButton.setToolTip("")
        
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
        self.ui.modifyTransformPushButton.text = "Modify"
        self.ui.modifyTransformPushButton.setToolTip("Modify virtual fit transform")
        
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

        transducer_openlifu : "openlifu.Transducer" = transducer.transducer.transducer
        protocol_openlifu : "openlifu.Protocol" = protocol.protocol

        units = "mm" # These are the units of the output space of the transform returned by get_IJK2RAS

        # Get the skin mesh associated with the volume
        skin_mesh_node = get_skin_segmentation(volume)
        if skin_mesh_node is None:
            skin_mesh_node = generate_skin_segmentation(volume)

        with threadpoolctl_lz().threadpool_limits(limits=1): # caps BLAS and OpenMP threads
            # Capping BLAS threads appears to have a performance improvement when running this algorithm in Slicer.
            # This may be because Slicer already occupies BLAS threads with its VTK/ITK stuff and so the virtual fit's many
            # tiny svd calls end up having more overhead than is worth it.
            # For some unknown reason, the improvement is only noticable when we do not use the embree
            # option in virtual fitting, which makes things very fast.
            vf_transforms = openlifu_lz().run_virtual_fit(
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

        for i,vf_transform in zip(range(10), vf_transforms): # We only add the top 10 virtual fit nodes, to not put so many transforms into the scene.
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

    def load_vf_debugging_info(self, debug_info : "openlifu.virtual_fit.VirtualFitDebugInfo") -> None:
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

    def setUp(self):
        """Do whatever is needed to reset the state - typically a scene clear will be enough."""
        slicer.mrmlScene.Clear()

    def runTest(self):
        """Run as few or as many tests as needed here."""
        self.setUp()
