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
)
from OpenLIFULib.coordinate_system_utils import get_IJK2RAS
from OpenLIFULib.events import SlicerOpenLIFUEvents
from OpenLIFULib.guided_mode_util import GuidedWorkflowMixin
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
    set_virtual_fit_approval_for_target,
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

        self.algorithm_input_widget.inputs_dict["Target"].combo_box.currentIndexChanged.connect(self.updateApproveButton)

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
        self.updateApproveButton()
        self.updateInputOptions()
        self.updateApprovalStatusLabel()
        self.updateEditTargetEnabled()
        self.updateTargetPositionInputs()
        self.updateLockButtonIcon()

        self.ui.newTargetButton.clicked.connect(self.onNewTargetClicked)
        self.ui.removeTargetButton.clicked.connect(self.onremoveTargetClicked)
        self.ui.lockButton.clicked.connect(self.onLockClicked)
        self.ui.approveButton.clicked.connect(self.onApproveClicked)
        self.ui.virtualfitButton.clicked.connect(self.onVirtualfitClicked)

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
        if node.IsA('vtkMRMLMarkupsFiducialNode'):
            self.watch_fiducial_node(node)

        self.updateTargetsListView()
        self.updateInputOptions()

    @vtk.calldata_type(vtk.VTK_OBJECT)
    def onNodeRemoved(self, caller, event, node : slicer.vtkMRMLNode) -> None:
        if node.IsA('vtkMRMLMarkupsFiducialNode'):
            self.unwatch_fiducial_node(node)

            data_logic : "OpenLIFUDataLogic" = slicer.util.getModuleLogic('OpenLIFUData')
            if not data_logic.session_loading_unloading_in_progress:
                self.revokeApprovalIfAny(node, reason="The target was removed.\n" +
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
        for tag in self.node_observations.pop(node.GetID()):
            node.RemoveObserver(tag)

    def onPointAddedOrRemoved(self, node:vtkMRMLMarkupsFiducialNode, caller, event):
        self.updateTargetsListView()
        self.updateInputOptions()
        self.updateWorkflowControls()
        data_logic : "OpenLIFUDataLogic" = slicer.util.getModuleLogic('OpenLIFUData')
        if not data_logic.session_loading_unloading_in_progress:
            reason = "The target was modified."
            self.revokeApprovalIfAny(node, reason=reason)
            self.clearVirtualFitResultsIfAny(node, reason = reason)
            slicer.util.getModuleWidget('OpenLIFUSonicationPlanner').deleteSolutionAndSolutionAnalysisIfAny(reason=reason)
                        
    def onPointModified(self, node:vtkMRMLMarkupsFiducialNode, caller, event):
        self.updateTargetPositionInputs()

        data_logic : "OpenLIFUDataLogic" = slicer.util.getModuleLogic('OpenLIFUData')
        if not data_logic.session_loading_unloading_in_progress:
            reason = "The target was modified."
            self.revokeApprovalIfAny(node, reason=reason)
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

    def revokeApprovalIfAny(self, target : Union[str,vtkMRMLMarkupsFiducialNode], reason:str):
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
            self.updateApproveButton()
            self.updateApprovalStatusLabel()

    def updateTargetsListView(self):
        """Update the list of targets in the target management UI"""
        self.ui.targetListWidget.clear()
        for target_node in get_target_candidates():
            item = qt.QListWidgetItem(target_node.GetName())
            item.setFlags(item.flags() | qt.Qt.ItemIsEditable) # Make it possible to click and rename items
            item.setData(qt.Qt.UserRole, target_node)
            self.ui.targetListWidget.addItem(item)

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
        self.updateApproveButton()
        self.updateInputOptions()
        self.updateApprovalStatusLabel()
        self.updateWorkflowControls()

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

    def updateApproveButton(self):
        selected_target : Optional[vtkMRMLMarkupsFiducialNode] = self.algorithm_input_widget.get_current_data()['Target']
        if selected_target is None:
            self.ui.approveButton.setEnabled(False)
            self.ui.approveButton.setText("Approve virtual fit")
            self.ui.approveButton.setToolTip("Please select a target and run virtual fit first")
            return
        target_id = fiducial_to_openlifu_point_id(selected_target)
        virtual_fit_result_node : Optional[vtkMRMLTransformNode] = self.logic.find_best_virtual_fit_result_for_target(target_id=target_id)
        if virtual_fit_result_node is None:
            self.ui.approveButton.setEnabled(False)
            self.ui.approveButton.setText("Approve virtual fit")
            self.ui.approveButton.setToolTip("Please run virtual fit first")
            return

        approved : bool = get_approval_from_virtual_fit_result_node(virtual_fit_result_node)

        self.ui.approveButton.setEnabled(True)
        if not approved:
            self.ui.approveButton.setText("Approve virtual fit")
            self.ui.approveButton.setToolTip("Approve the virtual fit result for the selected target")
        else:
            self.ui.approveButton.setText("Revoke virtual fit approval")
            self.ui.approveButton.setToolTip("Revoke virtual fit approval for the selected target")

    def updateInputOptions(self):
        """Update the algorithm input options"""
        self.algorithm_input_widget.update()
        self.updateVirtualfitButtonEnabled()

    def updateVirtualfitButtonEnabled(self):
        """Update the enabled status of the virtual fit button based on whether all inputs have valid selections"""
        if self.algorithm_input_widget.has_valid_selections():
            self.ui.virtualfitButton.enabled = True
            self.ui.virtualfitButton.setToolTip("Run virtual fit algorithm to automatically suggest a transducer positioning")
        else:
            self.ui.virtualfitButton.enabled = False
            self.ui.virtualfitButton.setToolTip("Specify all required inputs to enable virtual fitting")

    def onApproveClicked(self):
        session = get_openlifu_data_parameter_node().loaded_session
        session_id = None if session is None else session.get_session_id()
        selected_target : Optional[vtkMRMLMarkupsFiducialNode] = self.algorithm_input_widget.get_current_data()['Target']
        if selected_target is None:
            raise RuntimeError("The approve button should not have been enabled with no selected target.")
        target_id = fiducial_to_openlifu_point_id(selected_target)
        self.logic.toggle_virtual_fit_approval(target_id=target_id, session_id=session_id)
        self.updateApproveButton()
        self.updateApprovalStatusLabel()
        self.updateWorkflowControls()  

    def updateApprovalStatusLabel(self):
        approved_target_ids = self.logic.get_approved_target_ids()
        if len(approved_target_ids) == 0:
            self.ui.approvalStatusLabel.text = "There are currently no virtual fit approvals."
        else:
            self.ui.approvalStatusLabel.text = (
                "Virtual fit is approved for the following targets:\n- "
                + "\n- ".join(approved_target_ids)
            )

    def updateWorkflowControls(self):
        session = get_openlifu_data_parameter_node().loaded_session
        session_id = None if session is None else session.get_session_id()

        if session is None:
            self.workflow_controls.can_proceed = False
            self.workflow_controls.status_text = "If you are seeing this, guided mode is being run out of order! Load a session to proceed."
        if not get_target_candidates():
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



    def onVirtualfitClicked(self):
        activeData = self.algorithm_input_widget.get_current_data()

        def progress_callback(progress_percent:int, step_description:str) -> None:
            self.setVirtualFitProgressDisplay(value = progress_percent, status_text = step_description)
            slicer.app.processEvents()

        with BusyCursor():
            try:
                virtual_fit_result : Optional[vtkMRMLTransformNode] = self.logic.virtual_fit(
                    protocol = activeData["Protocol"],
                    transducer = activeData["Transducer"],
                    volume = activeData["Volume"],
                    target = activeData["Target"],
                    progress_callback = progress_callback,
                    include_debug_info = self.ui.virtualfitDebugCheckbox.checked,
                )
            finally:
                self.resetVirtualFitProgressDisplay()

        if virtual_fit_result is None:
            slicer.util.errorDisplay("Virtual fit failed. No viable transducer positions found.")
            return
        else:
            # TODO: Make the virtual fit button both update the transducer transform and populate in the virtual fit results
            activeData["Transducer"].set_current_transform_to_match_transform_node(virtual_fit_result)
            self.watchVirtualFit(virtual_fit_result)
            activeData["Transducer"].set_visibility(True)

        self.updateApproveButton()
        self.updateApprovalStatusLabel()
        self.updateWorkflowControls()

    def watchVirtualFit(self, virtual_fit_transform_node : vtkMRMLTransformNode):
        """Watch the virtual fit transform node to revoke approval in case the transform node is approved and then modified."""
        target_id = get_target_id_from_virtual_fit_result_node(virtual_fit_transform_node)
        self.addObserver(
            virtual_fit_transform_node,
            slicer.vtkMRMLTransformNode.TransformModifiedEvent,
            lambda caller, event: self.revokeApprovalIfAny(target_id, reason="The virtual fit transform was modified."),
        )

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

    def getParameterNode(self):
        return OpenLIFUPrePlanningParameterNode(super().getParameterNode())

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

    def toggle_virtual_fit_approval(self, target_id: str, session_id: Optional[str]):
        """Toggle approval for the virtual fit of the given target and session. If the session_id is provided
        as None, then the action will apply to a virtual fit result that has no affiliated session."""
        is_approved = get_virtual_fit_approval_for_target(target_id=target_id, session_id=session_id)
        set_virtual_fit_approval_for_target(
            approval_state=not is_approved,
            target_id=target_id,
            session_id=session_id
        )
        data_logic : "OpenLIFUDataLogic" = slicer.util.getModuleLogic('OpenLIFUData')
        data_logic.update_underlying_openlifu_session()

    def find_best_virtual_fit_result_for_target(self, target_id: str) -> vtkMRMLTransformNode:
        session = get_openlifu_data_parameter_node().loaded_session
        session_id = None if session is None else session.get_session_id()
        virtual_fit_result = get_best_virtual_fit_result_node(target_id=target_id, session_id=session_id)
        return virtual_fit_result

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
        set_virtual_fit_approval_for_target(False, target_id=target_id, session_id=session_id)
        data_logic : "OpenLIFUDataLogic" = slicer.util.getModuleLogic('OpenLIFUData')
        data_logic.update_underlying_openlifu_session()

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

        vf_transforms = openlifu_lz().virtual_fit(
            volume_array = slicer.util.arrayFromVolume(volume).transpose((2,1,0)),
            volume_affine_RAS = get_IJK2RAS(volume),
            units = units,
            target_RAS = target.GetNthControlPointPosition(0),
            standoff_transform = transducer_openlifu.get_standoff_transform_in_units(units),
            options = protocol_openlifu.virtual_fit_options,
            progress_callback = progress_callback,
            include_debug_info = include_debug_info,
        )
        if include_debug_info:
            vf_transforms, debug_info = vf_transforms # In this case two things were actually returned, the first of which is the list of transforms
            self.load_vf_debugging_info(debug_info)


        session = get_openlifu_data_parameter_node().loaded_session
        session_id : Optional[str] = session.get_session_id() if session is not None else None

        target_id = fiducial_to_openlifu_point_id(target)
        self.clear_virtual_fit_results(target = target)

        vf_result_nodes = []

        for i,vf_transform in zip(range(10), vf_transforms): # We only add the top 10 virtual fit nodes, to not put so many transforms into the scene.
            node = add_virtual_fit_result(
                transform_node = transducer_transform_node_from_openlifu(vf_transform, transducer.transducer.transducer, "mm"),
                target_id = target_id,
                session_id = session_id,
                approval_status = False,
                clone_node=False,
                rank = i+1,
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
