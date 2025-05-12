# Standard library imports
import itertools
import warnings
from subprocess import CalledProcessError
from typing import Optional, Tuple, TYPE_CHECKING, List, Dict, Union

# Third-party imports
import numpy as np
import qt
import vtk

# Slicer imports
import slicer
from slicer import (
    vtkMRMLMarkupsFiducialNode,
    vtkMRMLModelNode,
    vtkMRMLScalarVolumeNode,
    vtkMRMLTransformNode,
    vtkMRMLViewNode,
)
from slicer.ScriptedLoadableModule import *
from slicer.i18n import tr as _
from slicer.i18n import translate
from slicer.parameterNodeWrapper import parameterNodeWrapper
from slicer.util import VTKObservationMixin

# OpenLIFULib imports
from OpenLIFULib import (
    OpenLIFUAlgorithmInputWidget,
    SlicerOpenLIFUPhotoscan,
    SlicerOpenLIFUTransducer,
    get_cur_db,
    get_openlifu_data_parameter_node,
    openlifu_lz,
)
from OpenLIFULib.coordinate_system_utils import numpy_to_vtk_4x4
from OpenLIFULib.events import SlicerOpenLIFUEvents
from OpenLIFULib.guided_mode_util import get_guided_mode_state, GuidedWorkflowMixin
from OpenLIFULib.skinseg import generate_skin_mesh
from OpenLIFULib.targets import fiducial_to_openlifu_point_id
from OpenLIFULib.transform_conversion import transducer_transform_node_from_openlifu
from OpenLIFULib.transducer_tracking_results import (
    TransducerTrackingTransformType,
    add_transducer_tracking_result,
    get_approval_from_transducer_tracking_result_node,
    get_transform_type_from_transducer_tracking_result_node,
    get_photoscan_id_from_transducer_tracking_result,
    get_photoscan_ids_with_results,
    get_transducer_tracking_result,
    set_transducer_tracking_approval_for_photoscan,
)
from OpenLIFULib.transducer_tracking_wizard_utils import (
    create_threeD_photoscan_view_node,
    hide_displayable_nodes_from_view,
    initialize_wizard_ui,
    reset_view_node_camera,
    set_threeD_view_node,
    set_threeD_view_widget,
    get_threeD_transducer_tracking_view_node,
)
from OpenLIFULib.user_account_mode_util import UserAccountBanner
from OpenLIFULib.util import add_slicer_log_handler, BusyCursor, get_cloned_node, replace_widget 
from OpenLIFULib.virtual_fit_results import get_best_virtual_fit_result_node,  get_virtual_fit_approval_for_target

# These imports are for IDE and static analysis purposes only
if TYPE_CHECKING:
    import openlifu
    from openlifu.db import Database
    import openlifu.nav.photoscan
    from OpenLIFUData.OpenLIFUData import OpenLIFUDataLogic

class FacialLandmarksMarkupPageBase(qt.QWizardPage):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.placingLandmarks = False
        self._pointModifiedObserverTag = None
        self._currentlyPlacingIndex = -1
        self._currentlyUnsettingIndex = -1
        # We need to create this dictionary of temporary fiducial nodes because when
        # entering place mode, to use `SetActiveListID` i.e. set the node associated with control point placement,
        # The input must be a fiducial node. However, to be able to list the required facial landmarks
        # within the qMRMLSimpleMarkupsWidget, the landmarks need to be represented as control points within a fiducial node.
        # Therefore, we introduce a temporary markup fiducial during point placement, that gets copied to the landmark control point 
        # once a position is defined.
        self.temp_markup_fiducials = {
            'Right Ear': None,
            'Left Ear': None,
            'Nasion': None}
        self.facial_landmarks_fiducial_node: vtkMRMLMarkupsFiducialNode = None

    def setupMarkupsWidget(self):
        self.markupsWidget.setMRMLScene(slicer.mrmlScene)
        self.markupsWidget.enabled = False

        tableWidget = self.markupsWidget.tableWidget()
        tableWidget.setSelectionMode(tableWidget.SingleSelection)
        tableWidget.setSelectionBehavior(tableWidget.SelectRows)
        tableWidget.setContextMenuPolicy(qt.Qt.NoContextMenu)
        tableWidget.itemClicked.connect(self.markupTableWidgetSelected)
        tableWidget.itemDoubleClicked.connect(self.unsetControlPoint)

        if self.facial_landmarks_fiducial_node:
            self.markupsWidget.setCurrentNode(self.facial_landmarks_fiducial_node)
            for row in range(tableWidget.rowCount):
                item = tableWidget.item(row, 0)
                item.setFlags(~qt.Qt.ItemIsEditable | qt.Qt.ItemIsSelectable | qt.Qt.ItemIsEnabled)
        
        #  Adjust table height to fit the contents
        tableWidget.resizeRowsToContents()
        total_height = tableWidget.horizontalHeader().height  # Account for header
        for row in range(tableWidget.rowCount):
            total_height += tableWidget.rowHeight(row)
        tableWidget.setFixedHeight(total_height)
        tableWidget.setSizePolicy(tableWidget.sizePolicy.horizontalPolicy(), qt.QSizePolicy.Fixed)

    def markupTableWidgetSelected(self, item):
        if not self.placingLandmarks:
            return
        currentRow = item.row()
        if currentRow == -1 or self.facial_landmarks_fiducial_node.GetNthControlPointPositionStatus(currentRow) != 0:
            self._currentlyPlacingIndex = -1
            self.exitPlaceFiducialMode()
            return

        selected_text = self.markupsWidget.tableWidget().item(currentRow, 0).text()
        self.currently_placing_node = self._getSelectedNode(selected_text=selected_text)
        if self.currently_placing_node.GetNumberOfControlPoints() == 0:
            self.enterPlaceFiducialMode()
            self._currentlyPlacingIndex = currentRow

    def _getSelectedNode(self, selected_text: str):
        selected_landmark_name = None
        for landmark_name in self.temp_markup_fiducials:
            if landmark_name in selected_text:
                selected_landmark_name = landmark_name
                break
        if not selected_landmark_name:
            slicer.util.infoDisplay(
                text="Could not find a fiducial node matching the selected control point. Control points labels should include 'Right Ear', 'Left Ear' or 'Nasion'.",
                windowTitle="Matching fiducial node not found", parent=self.wizard()
            )
        if self.temp_markup_fiducials[selected_landmark_name] is None:
            self.temp_markup_fiducials[selected_landmark_name] = self._initialize_temporary_tracking_fiducial(node_name=selected_landmark_name)

        return self.temp_markup_fiducials[selected_landmark_name]

    def _initialize_temporary_tracking_fiducial(self, node_name: str):
        initialized_node = slicer.mrmlScene.AddNewNodeByClass("vtkMRMLMarkupsFiducialNode", node_name)
        initialized_node.GetDisplayNode().SetVisibility(False)
        initialized_node.SetMaximumNumberOfControlPoints(1)
        initialized_node.SetMarkupLabelFormat("%N")
        initialized_node.GetDisplayNode().SetViewNodeIDs([self.wizard().photoscan.view_node.GetID(), self.wizard().volume_view_node.GetID()])
        initialized_node.GetDisplayNode().SetVisibility(True)
        return initialized_node

    def _initialize_facial_landmarks_fiducial_node(self, node_name: str, existing_landmarks_node=None) -> vtkMRMLMarkupsFiducialNode:
        if existing_landmarks_node:  # Clone the existing node if valid
            node = get_cloned_node(existing_landmarks_node)
            node.SetName(node_name)  # Use the provided node name
            node.GetDisplayNode().SetVisibility(False)

        else:  # Initialize a new node
            node = slicer.mrmlScene.AddNewNodeByClass("vtkMRMLMarkupsFiducialNode", node_name)  # Use the provided node name
            node.GetDisplayNode().SetVisibility(False)  # Ensure that visibility is turned off
            node.SetMarkupLabelFormat("%N")
            for landmark_name in self.temp_markup_fiducials:
                node.AddControlPoint(0, 0, 0, f"Click to Place {landmark_name}")
                index = list(self.temp_markup_fiducials.keys()).index(landmark_name)
                node.UnsetNthControlPointPosition(index)  # Unset all the points initially

        node.GetDisplayNode().SetViewNodeIDs([self.wizard().photoscan.view_node.GetID(), self.wizard().volume_view_node.GetID()])
        node.GetDisplayNode().SetVisibility(True)  # Ensure that visibility is turned on after setting biew nodes

        # Add an observer if any of the points are undefined
        node.AddObserver(slicer.vtkMRMLMarkupsNode.PointAboutToBeRemovedEvent, self.onPointRemoved)
        node.AddObserver(slicer.vtkMRMLMarkupsNode.PointAddedEvent, self.onPointAdded)
        node.AddObserver(slicer.vtkMRMLMarkupsNode.PointModifiedEvent, self.onPointModified)
        self.facial_landmarks_fiducial_node = node
        return node

    def unsetControlPoint(self, item):
        currentRow = item.row()
        self._currentlyUnsettingIndex = currentRow
        if not self.placingLandmarks or currentRow == -1:
            return

        selected_text = self.markupsWidget.tableWidget().item(self._currentlyUnsettingIndex, 0).text()
        self.currently_placing_node = self._getSelectedNode(selected_text=selected_text)
        self.facial_landmarks_fiducial_node.SetNthControlPointPosition(self._currentlyUnsettingIndex, 0, 0, 0)
        self.facial_landmarks_fiducial_node.UnsetNthControlPointPosition(self._currentlyUnsettingIndex)
        self.enterPlaceFiducialMode()
        self._currentlyPlacingIndex = currentRow

    def enterPlaceFiducialMode(self):
        markupLogic = slicer.modules.markups.logic()
        markupLogic.SetActiveListID(self.currently_placing_node)
        markupLogic.StartPlaceMode(0)

        self._pointModifiedObserverTag = self.currently_placing_node.AddObserver(
            slicer.vtkMRMLMarkupsNode.PointPositionDefinedEvent, self.onPointPlaced)

    def onPointPlaced(self, caller, event):
        if caller.GetNumberOfControlPoints() < 1:
            return

        position = [0.0, 0.0, 0.0]
        self.currently_placing_node.GetNthControlPointPosition(0, position)
        self.facial_landmarks_fiducial_node.SetNthControlPointPosition(self._currentlyPlacingIndex, position)
        self.facial_landmarks_fiducial_node.SetNthControlPointLabel(self._currentlyPlacingIndex, caller.GetName())

        self.exitPlaceFiducialMode()
        self.currently_placing_node.RemoveAllObservers()
        slicer.mrmlScene.RemoveNode(self.currently_placing_node)
        self.temp_markup_fiducials[self.currently_placing_node.GetName()] = None
        if self._checkAllLandmarksDefined():
            self.updateLandmarkPlacementStatus()

    @vtk.calldata_type(vtk.VTK_INT)
    def onPointRemoved(self, node, eventID, callData):
        slicer.util.infoDisplay(
            text=f"{node.GetNthControlPointLabel(callData)} is essential for tracking. Deletion blocked.",
            windowTitle="Control point cannot be deleted", parent=self.wizard()
        )
        position = [0.0, 0.0, 0.0]
        node.GetNthControlPointPosition(callData, position)
        node.AddControlPoint(position, node.GetNthControlPointLabel(callData))
        
    @vtk.calldata_type(vtk.VTK_INT)
    def onPointAdded(self, node, eventID, callData):

        # Ensures that the original order of control points is maintained i.e. Right Ear - Left Ear - Nasion
        # This is important for fiducial registration
        point_label = node.GetNthControlPointLabel(callData)
        if point_label not in self.temp_markup_fiducials:
            # This should not happen
            raise ValueError("Invalid control point added to facial landmarks node.")
        landmark_labels_list = list(self.temp_markup_fiducials.keys())
        original_index = landmark_labels_list.index(point_label)
        current_index = callData
        while current_index > original_index:
            node.SwapControlPoints(current_index -1, current_index)
            current_index -= 1

    @vtk.calldata_type(vtk.VTK_INT)
    def onPointModified(self, node, eventID, callData):
        # If the fiducial node was initiaized based on a previously computed tt result, modifying the fiducial 
        # invalidates the result and resets previously initialized transform nodes
        self._clear_downstream_results_if_any()
            
    def _clear_downstream_results_if_any(self):
        if self.wizard()._valid_tt_result_exists:
            self.wizard()._valid_tt_result_exists = False
            self.wizard().photoscanVolumeTrackingPage.resetScalingTransform()
            # Clear downstream nodes
            slicer.mrmlScene.RemoveNode(self.wizard().photoscanVolumeTrackingPage.photoscan_to_volume_transform_node)
            slicer.mrmlScene.RemoveNode(self.wizard().transducerPhotoscanTrackingPage.transducer_to_volume_transform_node)
            self.wizard().photoscanVolumeTrackingPage.photoscan_to_volume_transform_node = None
            # self.wizard().transducerPhotoscanTrackingPage.transducer_to_volume_transform_node = None

    def exitPlaceFiducialMode(self):
        if self._pointModifiedObserverTag:
            self.currently_placing_node.RemoveObserver(self._pointModifiedObserverTag)
            self._pointModifiedObserverTag = None

        interactionNode = slicer.app.applicationLogic().GetInteractionNode()
        interactionNode.SetCurrentInteractionMode(interactionNode.ViewTransform)

    def _checkAllLandmarksDefined(self):
        if self.facial_landmarks_fiducial_node is None:
            return False

        all_points_defined = True
        for i in range(self.facial_landmarks_fiducial_node.GetNumberOfControlPoints()):
            if self.facial_landmarks_fiducial_node.GetNthControlPointPositionStatus(i) == 0:
                all_points_defined = False
        return all_points_defined

    def isComplete(self):
        if self.placingLandmarks:
            return False
        elif self.wizard()._valid_tt_result_exists:
            return True
        elif self.facial_landmarks_fiducial_node is not None:
            return self._checkAllLandmarksDefined()
        else:
            True

    # Abstract methods to be implemented by subclasses
    def initializePage(self):
        raise NotImplementedError

    def onPlaceLandmarksClicked(self):
        raise NotImplementedError

    def updateLandmarkPlacementStatus(self):
        raise NotImplementedError

class PhotoscanMarkupPage(FacialLandmarksMarkupPageBase):  # Inherit from the base class
    def __init__(self, parent=None):
        super().__init__(parent)  
        self.setTitle("Place facial landmarks on photoscan")
        self.ui = initialize_wizard_ui(self)  
        self.viewWidget = set_threeD_view_widget(self.ui)  
        self.ui.dialogControls.setCurrentIndex(1)
        self.markupsWidget = self.ui.photoscanMarkupsWidget  # Assign the correct markups widget
        self.ui.placeLandmarksButton.clicked.connect(self.onPlaceLandmarksClicked)
        self.page_locked = False

    def initializePage(self):
        set_threeD_view_node(self.viewWidget, threeD_view_node=self.wizard().photoscan.view_node)

        existing_fiducial_node = self.wizard().photoscan.facial_landmarks_fiducial_node
        if existing_fiducial_node and self.facial_landmarks_fiducial_node is None:
            if existing_fiducial_node .GetNumberOfControlPoints() != 3:
                slicer.util.infoDisplay(
                    text="Incorrect number of control points detected in the photoscan facial landmarks fiducial node. "
                    "Transudcer Tracking Wizard will replace the existing node.",
                    windowTitle="Invalid fiducial node detected", parent=self.wizard()
                )
                slicer.mrmlScene.RemoveNode(self.wizard().photoscan.facial_landmarks_fiducial_node)
                self.wizard().photoscan.facial_landmarks_fiducial_node = None
            else:
                existing_fiducial_node.GetDisplayNode().SetVisibility(False)
                self._initialize_facial_landmarks_fiducial_node(
                    node_name = "photoscan-wizard-faciallandmarks",
                    existing_landmarks_node=existing_fiducial_node)

        self.setupMarkupsWidget()
        self.updatePhotoscanApprovalStatusLabel(self.wizard().photoscan.is_approved())

        if self.wizard()._valid_tt_result_exists and not self.wizard()._existing_approval_revoked:
            self.page_locked = True
        else:
            self.page_locked = False
        self.updatePageLock()
    
    def updatePageLock(self):

        self.wizard().updateCurrentPageLockButton(locked = self.page_locked)
        self.ui.controlsWidget.enabled = not self.page_locked
        if not self.page_locked:
            self.ui.trackingApprovalWidget.hide()
        else:
            self.ui.trackingApprovalWidget.show()
            self.ui.approveTransformButton.hide()
            self.ui.approvalStatusLabel.text = ("A transducer tracking is currently approved for this photoscan. "
            "To re-do or edit fiducial landmarks, navigate to the "
            "'Register photoscan to skin surface' page and revoke the existing approval.")

    def onPlaceLandmarksClicked(self):
        if self.facial_landmarks_fiducial_node is None:
            self._initialize_facial_landmarks_fiducial_node(node_name = "photoscan-wizard-faciallandmarks")
            self.setupMarkupsWidget()
            self._clear_downstream_results_if_any()

        if self.ui.placeLandmarksButton.text == "Place/Edit Registration Landmarks":
            self.facial_landmarks_fiducial_node.SetLocked(False)
            self.ui.placeLandmarksButton.setText("Done Placing Landmarks")
            self.placingLandmarks = True
            self.ui.photoscanMarkupsWidget.enabled = True
            if self._checkAllLandmarksDefined():
                self.updateLandmarkPlacementStatus()
            else:
                self.ui.landmarkPlacementStatus.text = "- Select the desired landmark (Right Ear, Left Ear, or Nasion) from the list.\n" \
                                                     "- Click on the corresponding location on the photoscan mesh to place the landmark.\n" \
                                                     "- To unset a landmark's position, double-click it in the list."

        elif self.ui.placeLandmarksButton.text == "Done Placing Landmarks":
            self.facial_landmarks_fiducial_node.SetLocked(True)
            self.ui.placeLandmarksButton.setText("Place/Edit Registration Landmarks")
            self.placingLandmarks = False
            self.ui.photoscanMarkupsWidget.tableWidget().clearSelection()
            self.ui.photoscanMarkupsWidget.enabled = False
            self.exitPlaceFiducialMode()
            self.ui.landmarkPlacementStatus.text = ""

        self.completeChanged()

    def updatePhotoscanApprovalStatusLabel(self, photoscan_is_approved: bool):
        loaded_session = get_openlifu_data_parameter_node().loaded_session
        status = "approved" if photoscan_is_approved else "not approved"
        self.ui.photoscanApprovalStatusLabel_Markup.text = (
            f"Photoscan is {status} for transducer tracking" if loaded_session else f"Photoscan approval status: {photoscan_is_approved}."
        )

    def updateLandmarkPlacementStatus(self):
        self.ui.landmarkPlacementStatus.text = "Landmark positions unlocked. Click on the mesh to adjust."

class SkinSegmentationMarkupPage(FacialLandmarksMarkupPageBase):  # Inherit from the base
    def __init__(self, parent=None):
        super().__init__(parent)  # Call the base class constructor
        self.setTitle("Place facial landmarks on skin surface")
        self.ui = initialize_wizard_ui(self)  # Initialize your specific UI
        self.viewWidget = set_threeD_view_widget(self.ui)  # Initialize your specific view widget
        self.ui.dialogControls.setCurrentIndex(2)
        self.markupsWidget = self.ui.skinSegMarkupsWidget  # Assign the correct markups widget
        self.ui.placeLandmarksButtonSkinSeg.clicked.connect(self.onPlaceLandmarksClicked)
        self.page_locked: bool = False

    def initializePage(self):
        view_node = self.wizard().volume_view_node
        set_threeD_view_node(self.viewWidget, view_node)

        existing_skin_seg_fiducials = self.wizard()._logic.get_volume_facial_landmarks(self.wizard().skin_mesh_node)
        if existing_skin_seg_fiducials and self.facial_landmarks_fiducial_node is None:
            if existing_skin_seg_fiducials.GetNumberOfControlPoints() != 3:
                slicer.util.infoDisplay(
                    text="Incorrect number of control points detected in the volume facial landmarks fiducial node. "
                    "Transudcer Tracking Wizard will replace the existing node.",
                    windowTitle="Invalid fiducial node detected", parent=self.wizard()
                )
                slicer.mrmlScene.RemoveNode(existing_skin_seg_fiducials)
            else:  
                existing_skin_seg_fiducials.GetDisplayNode().SetVisibility(False)
                self._initialize_facial_landmarks_fiducial_node(
                    node_name = "skinseg-wizard-faciallandmarks",
                    existing_landmarks_node=existing_skin_seg_fiducials)

        self.setupMarkupsWidget()
        if self.wizard()._valid_tt_result_exists and not self.wizard()._existing_approval_revoked:
            self.page_locked = True
        else:
            self.page_locked = False
        self.updatePageLock()
    
    def updatePageLock(self):

        self.wizard().updateCurrentPageLockButton(locked = self.page_locked)
        self.ui.controlsWidget.enabled = not self.page_locked
        if not self.page_locked:
            self.ui.trackingApprovalWidget.hide()
        else:
            self.ui.trackingApprovalWidget.show()
            self.ui.approveTransformButton.hide()
            self.ui.approvalStatusLabel.text = ("Transducer tracking is currently approved for this photoscan. "
            "To re-do or edit fiducial landmarks, navigate to the "
            "'Register photoscan to skin surface' page and revoke the existing approval.")

    def onPlaceLandmarksClicked(self):
        if self.facial_landmarks_fiducial_node is None:
            self._initialize_facial_landmarks_fiducial_node(node_name = "skinseg-wizard-faciallandmarks")
            self.setupMarkupsWidget()
            # self.wizard()._valid_tt_result_exists = False

        if self.ui.placeLandmarksButtonSkinSeg.text == "Place/Edit Registration Landmarks":
            self.facial_landmarks_fiducial_node.SetLocked(False)
            self.ui.placeLandmarksButtonSkinSeg.setText("Done Placing Landmarks")
            self.placingLandmarks = True
            self.ui.skinSegMarkupsWidget.enabled = True
            if self._checkAllLandmarksDefined():
                self.updateLandmarkPlacementStatus()
            else:
                self.ui.landmarkPlacementStatus_2.text = "- Select the desired landmark (Right Ear, Left Ear, or Nasion) from the list.\n" \
                                                     "- Click on the corresponding location on the skin surface mesh to place the landmark.\n" \
                                                     "- To unset a landmark's position, double-click it in the list."

        elif self.ui.placeLandmarksButtonSkinSeg.text == "Done Placing Landmarks":
            self.facial_landmarks_fiducial_node.SetLocked(True)
            self.ui.placeLandmarksButtonSkinSeg.setText("Place/Edit Registration Landmarks")
            self.placingLandmarks = False
            self.ui.skinSegMarkupsWidget.tableWidget().clearSelection()
            self.ui.skinSegMarkupsWidget.enabled = False
            self.exitPlaceFiducialMode()
            self.ui.landmarkPlacementStatus_2.text = ""

        self.completeChanged()
    
    def _initialize_facial_landmarks_fiducial_node(self, node_name: str, existing_landmarks_node=None) -> vtkMRMLMarkupsFiducialNode:
        
        super()._initialize_facial_landmarks_fiducial_node(
            node_name="skinseg-wizard-faciallandmarks",
            existing_landmarks_node=existing_landmarks_node
        )
        self.facial_landmarks_fiducial_node.GetDisplayNode().SetColor(0, 0, 1)
        self.facial_landmarks_fiducial_node.GetDisplayNode().SetSelectedColor(0, 0, 1)
        if existing_landmarks_node:
            # Clear the volume meta data attribute 
            self.facial_landmarks_fiducial_node.SetAttribute('OpenLIFUData.volume_id', None)
                
    def updateLandmarkPlacementStatus(self):
        self.ui.landmarkPlacementStatus_2.text = "Landmark positions unlocked. Click on the mesh to adjust.\n" \
                                             "- To unset a landmark's position, double-click it in the list."
        
class PhotoscanVolumeTrackingPage(qt.QWizardPage):
    def __init__(self, parent = None):
        super().__init__()
        self.setTitle("Register photoscan to skin surface")
        self.ui = initialize_wizard_ui(self)
        self.viewWidget = set_threeD_view_widget(self.ui)
        self.ui.dialogControls.setCurrentIndex(3)

        self.ui.approveTransformButton.clicked.connect(self.onTransformApproveClicked)
        self.ui.enableManualPVRegistration.clicked.connect(self.onManualRegistrationClicked)
        self.ui.runICPRegistrationPV.clicked.connect(self.onRunICPRegistrationClicked)
        self.ui.initializePVRegistration.clicked.connect(self.onInitializeRegistrationClicked)
        self.runningRegistration = False
        self.page_locked: bool = False

        # Transform scale slider
        self.ui.scalingTransformMRMLSliderWidget.setMRMLScene(slicer.mrmlScene)
        self.ui.scalingTransformMRMLSliderWidget.minimum = 0.8
        self.ui.scalingTransformMRMLSliderWidget.maximum = 1.2
        self.ui.scalingTransformMRMLSliderWidget.value = 1
        self.ui.scalingTransformMRMLSliderWidget.decimals = 3
        self.ui.scalingTransformMRMLSliderWidget.singleStep = 0.002
        self.ui.scalingTransformMRMLSliderWidget.pageStep = 1.0
        self.ui.scalingTransformMRMLSliderWidget.setToolTip(_('Adjust the scale of the photosan mesh."'))
        self.ui.scalingTransformMRMLSliderWidget.connect("valueChanged(double)", self.updateScaledTransformNode)

        self.photoscan_to_volume_transform_node: vtkMRMLTransformNode = None
        self.scaling_transform_node = slicer.mrmlScene.AddNewNodeByClass("vtkMRMLTransformNode")
        self.scaling_transform_node.SetName("wizard_photoscan_volume-scaling_factor")
    
    def initializePage(self):
        """ This function is called when the user clicks 'Next'."""
        
        # We don't need to reset the view node here since the skin 
        # surface markup from the previous page happens in the same space. 
        view_node = self.wizard().volume_view_node
        set_threeD_view_node(self.viewWidget, view_node)

        # Show the existing transform node if the tt_result has not yet been modified by the wizard
        if not self.photoscan_to_volume_transform_node and self.wizard()._valid_tt_result_exists:
            # Clone the existing node
            existing_transform_node = self.wizard().photoscan_to_volume_transform_node
            self.photoscan_to_volume_transform_node = get_cloned_node(existing_transform_node)
            self.photoscan_to_volume_transform_node.CreateDefaultDisplayNodes()
            self.photoscan_to_volume_transform_node.GetDisplayNode().SetVisibility(False)
            self.photoscan_to_volume_transform_node.RemoveAttribute('isTT-PHOTOSCAN_TO_VOLUME')
            self.transform_approved = get_approval_from_transducer_tracking_result_node(existing_transform_node)

        # Check for facial landmarks
        self.has_facial_landmarks = (
            self.wizard().photoscanMarkupPage.facial_landmarks_fiducial_node
            and self.wizard().skinSegmentationMarkupPage.facial_landmarks_fiducial_node)
        if self.has_facial_landmarks:
            self.ui.initializePVRegistration.enabled = True
            self.ui.initializePVRegistration.setToolTip("Run fiducial-based registration between the photoscan mesh and skin surface.")
            self.ui.runICPRegistrationPV.enabled = True
            self.ui.runICPRegistrationPV.setToolTip("Run Iterative Closest Point (ICP) registration of the face.")
            if self.photoscan_to_volume_transform_node:
                self.ui.initializePVRegistration.setText("Re-initialize photoscan-volume transform")
        else:
            self.ui.initializePVRegistration.setText("Initialize photoscan-volume transform")
            self.ui.initializePVRegistration.enabled = False
            self.ui.initializePVRegistration.setToolTip("Please place fiducial landmarks on both the photoscan"
            " and skin surface mesh on the preceding pages to enable fiducial-based registration.")
            self.ui.runICPRegistrationPV.enabled = False
            self.ui.runICPRegistrationPV.setToolTip("Iterative Closest Point (ICP) registration of the face requires the user to"
            " first define fiducial landmarks on the facial surface on the preceding pages to delineate the region of interest.")

        if self.photoscan_to_volume_transform_node:
            self.setupTransformNode()
        else:
            self.ui.runICPRegistrationPV.enabled = False
            self.ui.ManualRegistrationGroupBox.enabled = False
            self.ui.approveTransformButton.enabled = False
            self.transform_approved = False
        
        self.updateTransformApprovalStatusLabel()
        self.updateTransformApproveButton()

        if self.wizard()._valid_tt_result_exists and not self.wizard()._existing_approval_revoked:
            self.page_locked = True
        else:
            self.page_locked = False
        self.updatePageLock()
    
    def updatePageLock(self):

        self.wizard().updateCurrentPageLockButton(locked = self.page_locked)
        self.ui.controlsWidget.enabled = not self.page_locked
        
    def onTransformModified(self, node, eventID,):
        
        # Check that this function was triggered by actions on this page
        current_page = self.wizard().page(self.wizard().currentId)
        if not isinstance(current_page, PhotoscanVolumeTrackingPage):
            return

        # If the transform node was initiaized based on a previously computed tt result, modifying the transform
        # invalidates the result 
        if self.wizard()._valid_tt_result_exists:
            self.wizard()._valid_tt_result_exists = False
            # self.wizard().transducerPhotoscanTrackingPage.transducer_to_volume_transform_node = None

    def updateTransformApprovalStatusLabel(self):

        self.ui.approvalStatusLabel.show()

        if self.transform_approved is None:
            self.ui.approvalStatusLabel.text = ""
            return
        
        status = "approved" if self.transform_approved else "not approved"
        self.ui.approvalStatusLabel.text = (
            f"The photoscan-volume transform is {status} for transducer tracking."
        )

    def updateTransformApproveButton(self):
        
        self.ui.approveTransformButton.show()
        if self.transform_approved:
            self.ui.approveTransformButton.setText("Revoke approval")
            self.ui.approveTransformButton.setToolTip(
                    "Revoke approval that the current transducer tracking result is correct")
        else:
            self.ui.approveTransformButton.setText("Approve photoscan-volume transform")
            self.ui.approveTransformButton.setToolTip("Approve the current transducer tracking result")

    def onTransformApproveClicked(self):

        self.transform_approved = not self.transform_approved

        if self.transform_approved:
            self.ui.controlsWidget.enabled = False
            if self.wizard()._valid_tt_result_exists:
                self.wizard()._existing_approval_revoked = False
                self.wizard().transducerPhotoscanTrackingPage.transform_approved = True
                # self.wizard().photoscanMarkupPage.enablePageControls(enabled = False)
                # self.wizard().skinSegmentationMarkupPage.enablePageControls(enabled = False)
        else:
            self.ui.controlsWidget.enabled = True
            if self.wizard()._valid_tt_result_exists:
                self.wizard()._existing_approval_revoked = True
                self.wizard().transducerPhotoscanTrackingPage.transform_approved = False
                # self.wizard().photoscanMarkupPage.enablePageControls(enabled = True)
                # self.wizard().skinSegmentationMarkupPage.enablePageControls(enabled = True)

        # Update the wizard page
        self.updateTransformApprovalStatusLabel()
        self.updateTransformApproveButton()
        self.wizard().updateWarningLabel()

        # Emit signal to update the enable/disable state of 'Next' button. 
        self.completeChanged()

    def onInitializeRegistrationClicked(self):
        """ This function is called when the user clicks 'Next'."""

        # Clear previous result if it exists
        slicer.mrmlScene.RemoveNode(self.photoscan_to_volume_transform_node) # Clear current result node if it exists (restarting process)
        if self.wizard()._valid_tt_result_exists:
            self.wizard()._valid_tt_result_exists = False

        self.photoscan_to_volume_transform_node = self.wizard()._logic.run_fiducial_registration(
            moving_landmarks = self.wizard().photoscanMarkupPage.facial_landmarks_fiducial_node,
            fixed_landmarks = self.wizard().skinSegmentationMarkupPage.facial_landmarks_fiducial_node)
        self.setupTransformNode()
        self.resetScalingTransform()

        self.updateTransformApprovalStatusLabel()
        self.ui.initializePVRegistration.setText("Re-initialize photoscan-volume transform")

        # Enable approval and registration fine-tuning buttons
        self.ui.runICPRegistrationPV.enabled = True
        self.ui.ManualRegistrationGroupBox.enabled = True
        self.ui.approveTransformButton.enabled = True

    def setupTransformNode(self):

        self.photoscan_to_volume_transform_node.GetDisplayNode().SetViewNodeIDs(
            [self.wizard().volume_view_node.GetID()]
            ) # Specify a view node for display
        self.photoscan_to_volume_transform_node.GetDisplayNode().SetEditorVisibility(False)
        
        # Update photoscan model and fiducial to observe transform
        self.wizard().photoscan.model_node.SetAndObserveTransformNodeID(self.photoscan_to_volume_transform_node.GetID())
        if self.wizard().photoscanMarkupPage.facial_landmarks_fiducial_node:
            self.wizard().photoscanMarkupPage.facial_landmarks_fiducial_node.SetAndObserveTransformNodeID(self.photoscan_to_volume_transform_node.GetID())
        
        # Set the center of the transformation to the center of the photocan model node
        bounds = [0.0, 0.0, 0.0, 0.0, 0.0, 0.0]
        self.wizard().photoscan.model_node.GetRASBounds(bounds)
        center_world = [
            (bounds[0] + bounds[1]) / 2,
            (bounds[2] + bounds[3]) / 2,
            (bounds[4] + bounds[5]) / 2
        ]
        
        center_local = [0.0,0.0,0.0]
        transform_from_world = vtk.vtkGeneralTransform()
        self.photoscan_to_volume_transform_node.GetTransformFromWorld(transform_from_world)
        transform_from_world.TransformPoint(center_world,center_local )
        self.photoscan_to_volume_transform_node.SetCenterOfTransformation(center_local)

        self.photoscan_to_volume_transform_node.SetAndObserveTransformNodeID(self.scaling_transform_node.GetID())
        # Add observer after setup
        self.photoscan_to_volume_transform_node.AddObserver(slicer.vtkMRMLTransformNode.TransformModifiedEvent, self.onTransformModified)

    def resetScalingTransform(self):
        self.ui.scalingTransformMRMLSliderWidget.value = 1
        self.updateScaledTransformNode()

    def onRunICPRegistrationClicked(self):

        self.photoscan_to_volume_transform_node.HardenTransform()
        
        # Clone photoscan model node to harden transform since ICP uses the coordinate space of the model
        photoscan_hardened = get_cloned_node(self.wizard().photoscan.model_node)
        photoscan_hardened.SetAndObserveTransformNodeID(self.photoscan_to_volume_transform_node.GetID())
        photoscan_hardened.HardenTransform()

        # Clone photoscan fiducial node to harden transform for select by points
        photoscan_landmarks_hardened = get_cloned_node(self.wizard().photoscanMarkupPage.facial_landmarks_fiducial_node)
        photoscan_landmarks_hardened.SetAndObserveTransformNodeID(self.photoscan_to_volume_transform_node.GetID())
        photoscan_landmarks_hardened.HardenTransform()

        with BusyCursor():
            photoscan_roi_submesh = self.wizard()._logic.extract_facial_roi_submesh(
                fiducial_node = photoscan_landmarks_hardened,
                surface_model_node = photoscan_hardened
            )

            try:
                self.photoscan_to_volume_icp_transform_node = self.wizard()._logic.run_icp_model_registration(
                    input_fixed_model = self.wizard().skin_mesh_node,
                    input_moving_model = photoscan_roi_submesh)

                self.photoscan_to_volume_transform_node.SetAndObserveTransformNodeID(self.photoscan_to_volume_icp_transform_node.GetID())
                self.photoscan_to_volume_transform_node.HardenTransform() # Combine ICP and initialization transform
            
                # Reset the photoscan to volume transform and now observe the ICP result
                self.resetScalingTransform()
                self.photoscan_to_volume_transform_node.SetAndObserveTransformNodeID(self.scaling_transform_node.GetID())
                slicer.mrmlScene.RemoveNode(self.photoscan_to_volume_icp_transform_node)

            except Exception as e:
                slicer.util.errorDisplay('ICP failed. Check logs for details.')
                raise e
            
            finally:
            
                # Remove temporary hardened nodes
                slicer.mrmlScene.RemoveNode(photoscan_hardened)
                slicer.mrmlScene.RemoveNode(photoscan_landmarks_hardened)
                slicer.mrmlScene.RemoveNode(photoscan_roi_submesh)

    def onManualRegistrationClicked(self):
        """ Enables the interaction handles on the transform, allowing the user to manually edit the photoscan-volume transform. """

        if not self.photoscan_to_volume_transform_node.GetDisplayNode().GetEditorVisibility():

            self.ui.enableManualPVRegistration.text = "Disable manual transform interaction"
            self.photoscan_to_volume_transform_node.GetDisplayNode().SetEditorVisibility(True)
            self.runningRegistration = True
            
            # For now, disable the approval and initialization button while in manual editing mode
            self.ui.initializePVRegistration.enabled = False
            self.ui.runICPRegistrationPV.enabled = False
            self.ui.approveTransformButton.enabled = False

        else:
            self.ui.enableManualPVRegistration.text = "Enable manual transform interaction"
            self.photoscan_to_volume_transform_node.GetDisplayNode().SetEditorVisibility(False)
            self.runningRegistration = False
            self.ui.initializePVRegistration.enabled = True if self.has_facial_landmarks else False
            self.ui.runICPRegistrationPV.enabled = True
            self.ui.approveTransformButton.enabled = True

        # Emit signal to update the enable/disable state of 'Next button'. 
        self.completeChanged()
    
    def updateScaledTransformNode(self):

        scaling_value = self.ui.scalingTransformMRMLSliderWidget.value
        scaling_matrix = np.diag([scaling_value, scaling_value, scaling_value, 1])
        # Need to also update the origin of the scaling transform
        self.scaling_transform_node.SetMatrixTransformToParent(numpy_to_vtk_4x4(scaling_matrix))

    def isComplete(self):
        """" Determines if the 'Next' button should be enabled"""
        return not self.runningRegistration and self.transform_approved

class TransducerPhotoscanTrackingPage(qt.QWizardPage):
    def __init__(self, parent = None):
        super().__init__()
        self.setTitle("Register transducer to photoscan")
        self.ui = initialize_wizard_ui(self)
        self.viewWidget = set_threeD_view_widget(self.ui)
        self.ui.dialogControls.setCurrentIndex(4)

        self.ui.approveTransformButton.clicked.connect(self.onTransformApproveClicked)
        self.ui.enableManualTPRegistration.clicked.connect(self.onManualRegistrationClicked)
        self.ui.initializeTPRegistration.clicked.connect(self.onInitializeRegistrationClicked)
        self.ui.runICPRegistrationTP.clicked.connect(self.onRunICPRegistrationClicked)
        self.runningRegistration = False 
        self.transducer_to_volume_transform_node: vtkMRMLTransformNode = None
        self.page_locked: bool = False

        self.ui.initializeTPRegistration.setToolTip("Use the best virtual fit result to initialize the transducer position. If virtual fit has not been run,"
        "the transform is initialized to idenity.")
    
    def initializePage(self):
        """ This function is called when the user clicks 'Next'."""

        view_node = self.wizard().volume_view_node
        set_threeD_view_node(self.viewWidget, view_node)

        if not self.transducer_to_volume_transform_node and self.wizard().transducer_to_volume_transform_node:
            # Clone the existing node
            existing_transform_node = self.wizard().transducer_to_volume_transform_node
            self.transducer_to_volume_transform_node = get_cloned_node(existing_transform_node)
            self.transducer_to_volume_transform_node.GetDisplayNode().SetVisibility(False)
            self.transducer_to_volume_transform_node.RemoveAttribute('isTT-TRANSDUCER_TO_VOLUME')
            self.transform_approved = get_approval_from_transducer_tracking_result_node(existing_transform_node)

        if self.transducer_to_volume_transform_node:
            self.ui.initializeTPRegistration.setText("Re-initialize transducer-photoscan transform")
            self.setupTransformNode()
        else:
            self.ui.initializeTPRegistration.setText("Initialize transducer-photoscan transform")
            self.ui.runICPRegistrationTP.enabled = False
            self.ui.approveTransformButton.enabled = False
            self.ui.enableManualTPRegistration.enabled = False
            self.transform_approved = False
        
        self.updateTransformApprovalStatusLabel()
        self.updateTransformApproveButton()

        if self.wizard()._valid_tt_result_exists and not self.wizard()._existing_approval_revoked and self.transform_approved:
            self.page_locked = True
        else:
            self.page_locked = False
        self.updatePageLock()
    
    def updatePageLock(self):

        self.wizard().updateCurrentPageLockButton(locked = self.page_locked)
        self.ui.controlsWidget.enabled = not self.page_locked

    def onTransformModified(self, node, eventID,):
        
        # If the transform node was initialized based on a previously computed tt result, modifying the transform
        # invalidates the result 
        if self.wizard()._valid_tt_result_exists:
            self.wizard()._valid_tt_result_exists = False

    def updateTransformApprovalStatusLabel(self):

        if self.transform_approved is None:
            self.ui.approvalStatusLabel.text = ""
            return
        
        status = "approved" if self.transform_approved else "not approved"
        self.ui.approvalStatusLabel.text = (
            f"The transducer-volume transform is {status} for transducer tracking."
        )

    def updateTransformApproveButton(self):

        if self.transform_approved:
            self.ui.approveTransformButton.setText("Revoke approval")
            self.ui.approveTransformButton.setToolTip(
                    "Revoke approval that the current transducer tracking result is correct")
        else:
            self.ui.approveTransformButton.setText("Approve transducer-volume transform")
            self.ui.approveTransformButton.setToolTip("Approve the current transducer tracking result")

    def onTransformApproveClicked(self):

        if self.transducer_to_volume_transform_node is None:
            raise RuntimeError("Transducer-photoscan transform not found.")
        
        self.transform_approved = not self.transform_approved
             
        if self.transform_approved:
            self.ui.controlsWidget.enabled = False
            if self.wizard()._valid_tt_result_exists:
                self.wizard()._existing_approval_revoked = False
        else:
            self.ui.controlsWidget.enabled = True
            if self.wizard()._valid_tt_result_exists:
                self.wizard()._existing_approval_revoked = True

        # Update the wizard page
        self.updateTransformApprovalStatusLabel()
        self.updateTransformApproveButton()
        self.wizard().updateWarningLabel()

        # Emit signal to update the enable/disable state of 'Finish' button. 
        self.completeChanged()
    
    def onInitializeRegistrationClicked(self):

        slicer.mrmlScene.RemoveNode(self.transducer_to_volume_transform_node)
        self.transducer_to_volume_transform_node = self.wizard()._logic.initialize_node_from_virtual_fit_result(
            transducer = self.wizard().transducer,
            target = self.wizard().target)
        self.updateTransformApprovalStatusLabel()
        self.setupTransformNode()
        self.ui.initializeTPRegistration.setText("Re-initialize transducer-photoscan transform")

        # Enable approval and registration fine-tuning buttons
        self.ui.runICPRegistrationTP.enabled = True
        self.ui.enableManualTPRegistration.enabled = True
        self.ui.approveTransformButton.enabled = True
            
    def setupTransformNode(self):

        self.wizard().transducer_surface.SetAndObserveTransformNodeID(self.transducer_to_volume_transform_node.GetID())
        self.transducer_to_volume_transform_node.GetDisplayNode().SetViewNodeIDs(
            [self.wizard().volume_view_node.GetID()]) # Specify a view node for display
        self.transducer_to_volume_transform_node.GetDisplayNode().SetEditorVisibility(False)
        self.transducer_to_volume_transform_node.AddObserver(slicer.vtkMRMLTransformNode.TransformModifiedEvent, self.onTransformModified)

    def onManualRegistrationClicked(self):
        """ This allows the user to manually edit the transducer-volume transform. """
        
        if not self.transducer_to_volume_transform_node.GetDisplayNode().GetEditorVisibility():
            self.ui.enableManualTPRegistration.text = "Disable manual transform interaction"
            self.transducer_to_volume_transform_node.GetDisplayNode().SetEditorVisibility(True)
            self.runningRegistration = True
            # For now, disable the approval and initialization button while in manual editing mode
            self.ui.initializeTPRegistration.enabled = False
            self.ui.runICPRegistrationTP.enabled = False
            self.ui.approveTransformButton.enabled = False
        else:
            self.ui.enableManualTPRegistration.text = "Enable manual transform interaction"
            self.transducer_to_volume_transform_node.GetDisplayNode().SetEditorVisibility(False)
            self.runningRegistration = False
            self.ui.initializeTPRegistration.enabled = True
            self.ui.approveTransformButton.enabled = True
            self.ui.runICPRegistrationTP.enabled = True
    
        # Emit signal to update the enable/disable state of 'Finish' button. 
        self.completeChanged()
    
    def onRunICPRegistrationClicked(self):

        # Harden the photoscan to volume registration result
        self.wizard().photoscanVolumeTrackingPage.photoscan_to_volume_transform_node.HardenTransform()

        # Clone photoscan model node and harden transform since ICP uses the coordinate space of the model
        photoscan_hardened = get_cloned_node(self.wizard().photoscan.model_node)
        photoscan_hardened.SetAndObserveTransformNodeID(self.wizard().photoscanVolumeTrackingPage.photoscan_to_volume_transform_node.GetID())
        photoscan_hardened.HardenTransform()

        # Clone transducer surface model node and harden tansform after virtual fit initialization
        transducer_hardened = get_cloned_node(self.wizard().transducer_surface)
        transducer_hardened.SetAndObserveTransformNodeID(self.transducer_to_volume_transform_node.GetID())
        transducer_hardened.HardenTransform()
 
        try:
            self.transducer_to_photoscan_icp_transform_node = self.wizard()._logic.run_icp_model_registration(
                input_fixed_model = photoscan_hardened,
                input_moving_model = transducer_hardened,
                transformType = 0,
            )

            self.transducer_to_volume_transform_node.SetAndObserveTransformNodeID(self.transducer_to_photoscan_icp_transform_node.GetID())
            self.transducer_to_volume_transform_node.HardenTransform() # Combine ICP and initialization transform
            slicer.mrmlScene.RemoveNode(self.transducer_to_photoscan_icp_transform_node)

        except Exception as e:
            slicer.util.errorDisplay('ICP failed. Check logs for details.')
            raise e
            
        finally:
            # Remove hardened photoscan and transducer node 
            slicer.mrmlScene.RemoveNode(photoscan_hardened)
            slicer.mrmlScene.RemoveNode(transducer_hardened)

    def isComplete(self):
        """" Determines if the 'Next' button should be enabled"""
        return not self.runningRegistration and self.transform_approved

class TransducerTrackingWizard(qt.QWizard):
    def __init__(self, photoscan: SlicerOpenLIFUPhotoscan, 
                 volume: vtkMRMLScalarVolumeNode, 
                 transducer: SlicerOpenLIFUTransducer,
                 target: vtkMRMLMarkupsFiducialNode):
        super().__init__()

        self._logic = slicer.util.getModuleLogic('OpenLIFUTransducerTracker')
        
        with BusyCursor():

            self.transducer = transducer
            self.target = target
            self.transducer_surface = transducer.surface_model_node
            
            # These steps take some time
            self.skin_mesh_node = self._logic.compute_skin_segmentation(volume)
            self.photoscan = self._logic.load_openlifu_photoscan(photoscan)

            self.setupViewNodes()

        self.setWindowTitle("Transducer Tracking Wizard")
        self.photoscanMarkupPage = PhotoscanMarkupPage(self)
        self.skinSegmentationMarkupPage = SkinSegmentationMarkupPage(self)
        self.photoscanVolumeTrackingPage = PhotoscanVolumeTrackingPage(self)
        self.transducerPhotoscanTrackingPage = TransducerPhotoscanTrackingPage(self)

        self.addPage(self.photoscanMarkupPage)
        self.addPage(self.skinSegmentationMarkupPage)
        self.addPage(self.photoscanVolumeTrackingPage)
        self.addPage(self.transducerPhotoscanTrackingPage)

        self.setOption(qt.QWizard.NoBackButtonOnStartPage)
        self.setWizardStyle(qt.QWizard.ClassicStyle)
        self.setButtonText(qt.QWizard.FinishButton,"Approve")
        # Connect the currentIdChanged signal
        self.currentIdChanged.connect(self.setPageSpecificNodeDisplaySettings)
        # Connect signals for finish and cancel
        self.button(qt.QWizard.FinishButton).clicked.connect(self.onFinish)
        self.button(qt.QWizard.CancelButton).clicked.connect(self.onCancel)

        # Check the scene for previously computed tt results for the specified photoscan
        self._valid_tt_result_exists = False
        self.photoscan_to_volume_transform_node = self._logic.get_transducer_tracking_result_node(
            photoscan_id = self.photoscan.get_id(),
            transform_type = TransducerTrackingTransformType.PHOTOSCAN_TO_VOLUME)

        self.transducer_to_volume_transform_node = self._logic.get_transducer_tracking_result_node(
            photoscan_id = self.photoscan.get_id(),
            transform_type = TransducerTrackingTransformType.TRANSDUCER_TO_VOLUME)
        
        self._existing_approval_revoked = False
        if self.photoscan_to_volume_transform_node and self.transducer_to_volume_transform_node:
            self._valid_tt_result_exists = True
            if not (
                get_approval_from_transducer_tracking_result_node(self.photoscan_to_volume_transform_node) 
                and get_approval_from_transducer_tracking_result_node(self.transducer_to_volume_transform_node)
            ): #Flag to keep track of when approval is revoked and an existing result can be modified
                self._existing_approval_revoked = True    
        
    def customexec_(self):
        returncode = self.exec_()
        return (returncode, self.photoscan_to_volume_transform_node, self.transducer_to_volume_transform_node)
    
    def setPageSpecificNodeDisplaySettings(self, page_id: int):
        current_page = self.page(page_id)

        if isinstance(current_page, PhotoscanMarkupPage):

            # Display the photoscan. This sets the visibility on the model and fiducial node
            # Reset the view node everytime the photoscan is displayed
            self.photoscan.model_node.GetDisplayNode().SetVisibility(True)
            self.photoscan.model_node.SetAndObserveTransformNodeID(None) # Should be viewed in native space

            # Disable editing of the fiducial node position
            if self.photoscanMarkupPage.facial_landmarks_fiducial_node:
                self.photoscanMarkupPage.facial_landmarks_fiducial_node.SetLocked(True)
                self.photoscanMarkupPage.facial_landmarks_fiducial_node.GetDisplayNode().SetVisibility(True)
                self.photoscanMarkupPage.facial_landmarks_fiducial_node.SetAndObserveTransformNodeID(None) # Should be viewed in native space

            # If the user clicks 'Back' from the skin segmentation markup page
            self.skin_mesh_node.GetDisplayNode().SetVisibility(False)
            if self.skinSegmentationMarkupPage.facial_landmarks_fiducial_node:
                self.skinSegmentationMarkupPage.facial_landmarks_fiducial_node.GetDisplayNode().SetVisibility(False)
            
            reset_view_node_camera(self.photoscan.view_node)

        elif isinstance(current_page, SkinSegmentationMarkupPage):

            # Display skin segmentation and hide the photoscan and transducer surface
            self.skin_mesh_node.GetDisplayNode().SetVisibility(True)
            self.photoscan.model_node.GetDisplayNode().SetVisibility(False)
            self.transducer_surface.GetDisplayNode().SetVisibility(False)

            if self.photoscanMarkupPage.facial_landmarks_fiducial_node:
                self.photoscanMarkupPage.facial_landmarks_fiducial_node.GetDisplayNode().SetVisibility(False)
            
            # If the facial landmarks have been created, set their display settings
            if self.skinSegmentationMarkupPage.facial_landmarks_fiducial_node:
                self.skinSegmentationMarkupPage.facial_landmarks_fiducial_node.SetLocked(True)
                self.skinSegmentationMarkupPage.facial_landmarks_fiducial_node.GetDisplayNode().SetVisibility(True)

        elif isinstance(current_page, PhotoscanVolumeTrackingPage):

            # Display the photoscan and volume and hide the transducer
            self.skin_mesh_node.GetDisplayNode().SetVisibility(True)
            self.photoscan.model_node.GetDisplayNode().SetVisibility(True)
            self.transducer_surface.GetDisplayNode().SetVisibility(False)
            
            if self.photoscanMarkupPage.facial_landmarks_fiducial_node:
                self.photoscanMarkupPage.facial_landmarks_fiducial_node.GetDisplayNode().SetVisibility(True)
            
            if self.skinSegmentationMarkupPage.facial_landmarks_fiducial_node:
                self.skinSegmentationMarkupPage.facial_landmarks_fiducial_node.GetDisplayNode().SetVisibility(True)

        elif isinstance(current_page, TransducerPhotoscanTrackingPage):

            # Display the photoscan and transducer and hide the skin mesh
            self.skin_mesh_node.GetDisplayNode().SetVisibility(False)
            self.photoscan.model_node.GetDisplayNode().SetVisibility(True)
            self.transducer_surface.GetDisplayNode().SetVisibility(True)

            if self.photoscanMarkupPage.facial_landmarks_fiducial_node:
                self.photoscanMarkupPage.facial_landmarks_fiducial_node.GetDisplayNode().SetVisibility(False)
            
            if self.skinSegmentationMarkupPage.facial_landmarks_fiducial_node:
                self.skinSegmentationMarkupPage.facial_landmarks_fiducial_node.GetDisplayNode().SetVisibility(False)
        
        # Reset the wizard volume view node based on the display settings
        reset_view_node_camera(self.volume_view_node)
        self.updateWarningLabel()
        # self.updateCurrentPageLockButton()

    def updateWarningLabel(self):

        current_page = self.page(self.currentId)
        # Trigger an update of the warning label on the relevant wizard page
        if self._existing_approval_revoked:
            text = "WARNING: Modifying a previously approved tracking result!"
        else:
            text = ""
        
        if isinstance(current_page, PhotoscanMarkupPage):
                self.photoscanMarkupPage.ui.warningTrackingResultLabel.text = text
                self.photoscanMarkupPage.ui.warningTrackingResultLabel.styleSheet = "color:red;"
        elif isinstance(current_page, SkinSegmentationMarkupPage):
            self.skinSegmentationMarkupPage.ui.warningTrackingResultLabel.text = text
            self.skinSegmentationMarkupPage.ui.warningTrackingResultLabel.styleSheet = "color:red;"
        elif isinstance(current_page, PhotoscanVolumeTrackingPage):
            self.photoscanVolumeTrackingPage.ui.warningTrackingResultLabel.text = text
            self.photoscanVolumeTrackingPage.ui.warningTrackingResultLabel.styleSheet = "color:red;"
        elif isinstance(current_page, TransducerPhotoscanTrackingPage):
            self.transducerPhotoscanTrackingPage.ui.warningTrackingResultLabel.text = text
            self.transducerPhotoscanTrackingPage.ui.warningTrackingResultLabel.styleSheet = "color:red;"

    def updateCurrentPageLockButton(self, locked = False):

        current_page = self.page(self.currentId)
        
        lockButton = None
        if isinstance(current_page, PhotoscanMarkupPage):
            lockButton = self.photoscanMarkupPage.ui.pageLockButton
        elif isinstance(current_page, SkinSegmentationMarkupPage):
            lockButton = self.skinSegmentationMarkupPage.ui.pageLockButton
        elif isinstance(current_page, PhotoscanVolumeTrackingPage):
            lockButton = self.photoscanVolumeTrackingPage.ui.pageLockButton
        elif isinstance(current_page, TransducerPhotoscanTrackingPage):
            lockButton = self.transducerPhotoscanTrackingPage.ui.pageLockButton

        if not lockButton:
            return

        lockButton.setIcon(qt.QIcon())
        lockButton.setToolTip("")

        if locked:
            lockButton.setIcon(qt.QIcon(":Icons/Medium/SlicerLock.png"))
            lockButton.setToolTip("Page locked. Click to unlock and modify the transducer tracking result.")
        else:
            lockButton.setIcon(qt.QIcon(":Icons/Medium/SlicerUnlock.png"))
            lockButton.setToolTip("Page unlocked. Click to approve tracking result.")

    def onFinish(self):
        """Handle Finish button click."""
        self.resetViewNodes()

        # Reset the transducer surface to observe the transducer transform
        self.transducer_surface.SetAndObserveTransformNodeID(self.transducer.transform_node.GetID())

        # Copy photoscan and skin segmentation landmarks to slicer scene
        # There may not be fiducials created of the user is viewing previous tracking results
        if self.photoscanMarkupPage.facial_landmarks_fiducial_node:
            self._logic.update_photoscan_tracking_fiducials_from_node(
                photoscan = self.photoscan,
                fiducial_node =  self.photoscanMarkupPage.facial_landmarks_fiducial_node)
        if self.skinSegmentationMarkupPage.facial_landmarks_fiducial_node:
            self._logic.update_volume_facial_landmarks_from_node(volume_or_skin_mesh = self.skin_mesh_node,
                fiducial_node =  self.skinSegmentationMarkupPage.facial_landmarks_fiducial_node)
        
        # Add the transducer tracking result nodes to the slicer scene
        # Shouldn't be able to get to this final stage without both transform nodes
        if self.photoscanVolumeTrackingPage.photoscan_to_volume_transform_node and self.transducerPhotoscanTrackingPage.transducer_to_volume_transform_node:
            
            # Remove observer
            self.photoscanVolumeTrackingPage.photoscan_to_volume_transform_node.RemoveAllObservers()
            self.photoscanVolumeTrackingPage.photoscan_to_volume_transform_node.HardenTransform() 
            
            self.photoscan_to_volume_transform_node, self.transducer_to_volume_transform_node = self._logic.add_transducer_tracking_result(
                photoscan_to_volume_transform = self.photoscanVolumeTrackingPage.photoscan_to_volume_transform_node,
                photoscan_to_volume_approval_state = self.photoscanVolumeTrackingPage.transform_approved,
                transducer_to_volume_transform = self.transducerPhotoscanTrackingPage.transducer_to_volume_transform_node,
                transducer_to_volume_approval_state = self.transducerPhotoscanTrackingPage.transform_approved,
                photoscan_id = self.photoscan.get_id(),
                transducer = self.transducer)
        else:
            raise RuntimeError("Something went wrong. You should not be able to complete the wizard without creating transducer tracking transforms.")

        self.clearWizardNodes() #remove the wizard-level node
            
        # When clearing the nodes associated with the markups widgets, the interaction node gets set to Place mode.
        # This forced set of the interaction node is needed to solve that. 
        interactionNode = slicer.app.applicationLogic().GetInteractionNode()
        interactionNode.SwitchToViewTransformMode()

        self.accept()  # Closes the wizard

    def onCancel(self):
        """Handle Cancel button click."""
        self.resetViewNodes()
        # Reset the transducer surface to observe the transducer transform
        self.transducer_surface.SetAndObserveTransformNodeID(self.transducer.transform_node.GetID())
        self.clearWizardNodes()

        # Exit place mode
        interactionNode = slicer.app.applicationLogic().GetInteractionNode()
        interactionNode.SwitchToViewTransformMode()

        self.reject()  # Closes the wizard
    
    def clearWizardNodes(self):
        # Ensure any temporary variables are cleared. Nodes in the scene are not updated
        for node in self.photoscanMarkupPage.temp_markup_fiducials.values():
            if node:
                node.RemoveAllObservers()
            slicer.mrmlScene.RemoveNode(node)
        slicer.mrmlScene.RemoveNode(self.photoscanMarkupPage.facial_landmarks_fiducial_node)

        for node in self.skinSegmentationMarkupPage.temp_markup_fiducials.values():
            if node:
                node.RemoveAllObservers()
            slicer.mrmlScene.RemoveNode(node)
        slicer.mrmlScene.RemoveNode(self.skinSegmentationMarkupPage.facial_landmarks_fiducial_node)

        slicer.mrmlScene.RemoveNode(self.photoscanVolumeTrackingPage.photoscan_to_volume_transform_node)
        slicer.mrmlScene.RemoveNode(self.transducerPhotoscanTrackingPage.transducer_to_volume_transform_node)
        slicer.mrmlScene.RemoveNode(self.photoscanVolumeTrackingPage.scaling_transform_node)

    def setupViewNodes(self):
                
        # Create a viewNode for displaying the photoscan if it hasn't been created
        photoscan_id = self.photoscan.get_id()
        if self.photoscan.view_node is None:
            self.photoscan.view_node = create_threeD_photoscan_view_node(photoscan_id = photoscan_id)
        self.volume_view_node = get_threeD_transducer_tracking_view_node()
        wizard_view_nodes = [self.photoscan.view_node, self.volume_view_node]

        # Set view nodes for the skin mesh, transducer and photoscan
        self.skin_mesh_node.GetDisplayNode().SetViewNodeIDs([self.volume_view_node.GetID()])

        # For transducers, ensure that the parent folder visibility is turned on
        # and save the current view settings on the transducer surface
        shNode = slicer.vtkMRMLSubjectHierarchyNode.GetSubjectHierarchyNode(slicer.mrmlScene)
        parentFolderID = shNode.GetItemParent(shNode.GetItemByDataNode(self.transducer_surface))
        shNode.SetItemDisplayVisibility(parentFolderID, True)

        # If the transducer surface has specific view nodes associated with it, maintain those view nodes
        # We need to check for current view settings since the transducer exists in the scene
        # before the wizard.
        self.current_transducer_surface_visibility = self.transducer_surface.GetDisplayNode().GetVisibility()
        self.current_transducer_surface_viewnodes = self.transducer_surface.GetDisplayNode().GetViewNodeIDs()
        self.transducer_surface.GetDisplayNode().SetViewNodeIDs([self.volume_view_node.GetID()])

        self.photoscan.set_view_nodes(wizard_view_nodes)

        # Hide all displayable nodes in the scene from the wizard view nodes
        hide_displayable_nodes_from_view(wizard_view_nodes = wizard_view_nodes)
        
    def resetViewNodes(self):
        """Resets the view nodes of all models created by the wizard to null '()'. This allows the
        user to toggle and view the models in the main window through scene manipulation if they 
        choose it. """
        
        self.photoscan.model_node.GetDisplayNode().SetVisibility(False)
        self.photoscan.set_view_nodes([])

        # Restore previous view settings
        self.transducer_surface.GetDisplayNode().SetViewNodeIDs(self.current_transducer_surface_viewnodes)
        self.transducer_surface.GetDisplayNode().SetVisibility(self.current_transducer_surface_visibility) 
        
        self.skin_mesh_node.GetDisplayNode().SetViewNodeIDs(())
        self.skin_mesh_node.GetDisplayNode().SetVisibility(False)
        skin_facial_landmarks_node = self._logic.get_volume_facial_landmarks(self.skin_mesh_node)
        if skin_facial_landmarks_node:
            skin_facial_landmarks_node.GetDisplayNode().SetVisibility(False)
            skin_facial_landmarks_node.GetDisplayNode().SetViewNodeIDs(())
    
class PhotoscanPreviewPage(qt.QWizardPage):
    def __init__(self, parent = None):
        super().__init__()
        self.setTitle("Photoscan preview")

        self.ui = initialize_wizard_ui(self)
        self.viewWidget = set_threeD_view_widget(self.ui)
        self.ui.dialogControls.setCurrentIndex(0)
        self.ui.trackingApprovalWidget.hide()
        self.ui.warningTrackingResultLabel.hide()
        self.ui.lockPanel.hide()

    def initializePage(self):
        """ This function is called when the user clicks 'Next'."""

        self._photoscan_approved = self.wizard().photoscan_approved
        # Connect buttons and signals
        self.updatePhotoscanApproveButton()
        self.updatePhotoscanApprovalStatusLabel()
        self.ui.photoscanApprovalButton.clicked.connect(self.onPhotoscanApproveClicked)

        set_threeD_view_node(self.viewWidget, threeD_view_node = self.wizard().photoscan.view_node)
        
        # Display the photoscan 
        self.wizard().photoscan.model_node.GetDisplayNode().SetVisibility(True) # Specify a view node for display
        # Reset the camera associated with the view node based on the photoscan model
        reset_view_node_camera(self.wizard().photoscan.view_node)

    def updatePhotoscanApprovalStatusLabel(self):
        
        status = "approved" if self._photoscan_approved else "not approved"
        self.ui.photoscanApprovalStatusLabel.text = (
            f"Photoscan is {status} for transducer tracking."
        )

    def updatePhotoscanApproveButton(self):
        if self._photoscan_approved:
            self.ui.photoscanApprovalButton.setText("Revoke photoscan approval")
            self.ui.photoscanApprovalButton.setToolTip(
                    "Revoke approval that the current photoscan is of sufficient quality to be used for transducer tracking")
        else:
            self.ui.photoscanApprovalButton.setText("Approve photoscan")
            self.ui.photoscanApprovalButton.setToolTip("Approve that the current photoscan can be used for transducer tracking")

    def onPhotoscanApproveClicked(self):

        # Update the approval status at a wizard-level
        self._photoscan_approved = not self._photoscan_approved 
        
        # Update the wizard page
        self.updatePhotoscanApprovalStatusLabel()
        self.updatePhotoscanApproveButton()

class PhotoscanPreviewWizard(qt.QWizard):
    def __init__(self, photoscan : "openlifu.nav.photoscan.Photoscan"):
        super().__init__()

        self._logic = self._logic = slicer.util.getModuleLogic('OpenLIFUTransducerTracker')
        self.photoscan = self._logic.load_openlifu_photoscan(photoscan)
        self.photoscan_approved: bool = self.photoscan.is_approved()

        self.setupViewNode()

        self.setWindowTitle("Photoscan Preview")
        self.photoscanPreviewPage = PhotoscanPreviewPage(self)
        self.addPage(self.photoscanPreviewPage)

        # Customize view
        self.setOption(qt.QWizard.NoBackButtonOnStartPage)
        self.setOption(qt.QWizard.NoCancelButton)

        # Connect signals for finish and cancel
        self.button(qt.QWizard.FinishButton).clicked.connect(self.onFinish)

    def onFinish(self):
        self.resetViewNodes()

        # Update final photoscan approval state
        self.photoscan_approved = self.photoscanPreviewPage._photoscan_approved

        # Update the photoscan approval status in the underlying openlifu photoscan object
        self._logic.update_photoscan_approval(
            photoscan = self.photoscan,
            approval_state = self.photoscan_approved)

        self.accept()  # Closes the wizard
    
    def setupViewNode(self):
        """ Returns the view node associated with the photoscan.
        When a new view node is created, the view node centers and fits the displayed photoscan in 3D view.
        This should only happen when the user is viewing the photoscan for the first time. 
        If the user has previously interacted with the 3D view widget, then
        maintain the previous camera/focal point."""
                
        # Create a viewNode for displaying the photoscan if it hasn't been created
        photoscan_id = self.photoscan.get_id()
        if self.photoscan.view_node is None:
            self.photoscan.view_node = create_threeD_photoscan_view_node(photoscan_id = photoscan_id)

        # Set view nodes on the photoscan
        self.photoscan.set_view_nodes([self.photoscan.view_node])
        
        # Hide all displayable nodes in the scene from the wizard view ndoes
        hide_displayable_nodes_from_view(wizard_view_nodes = [self.photoscan.view_node])
        
    def resetViewNodes(self):
        
        self.photoscan.model_node.GetDisplayNode().SetVisibility(False)
        self.photoscan.set_view_nodes([])

class PhotoscanGenerationOptionsDialog(qt.QDialog):
    def __init__(self, meshroom_pipeline_names: list[str], parent=None):
        super().__init__(parent)
        self.setWindowTitle("Configure photoscan generation")
        self.setModal(True)

        form = qt.QFormLayout(self)

        self.meshroom_pipeline_combobox = qt.QComboBox(self)
        self.meshroom_pipeline_combobox.addItems(meshroom_pipeline_names)
        form.addRow("Meshroom pipeline:", self.meshroom_pipeline_combobox)
        self.meshroom_pipeline_combobox.setToolTip(
            "Meshroom pipelines are defined in the openlifu python library."
        )

        self.image_width_line_edit = qt.QLineEdit(self)
        image_width_validator = qt.QIntValidator(256, 16384, self)
        self.image_width_line_edit.setValidator(image_width_validator)
        self.image_width_line_edit.text = "2048" # default value
        form.addRow("Input image width:", self.image_width_line_edit)
        self.image_width_line_edit.setToolTip(
            "The width in pixels to which input photos should be resized before going through mesh reconstruction."
        )

        buttons = qt.QDialogButtonBox(
            qt.QDialogButtonBox.Ok | qt.QDialogButtonBox.Cancel,
            self
        )
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        form.addRow(buttons)

        self.ok_button = buttons.button(qt.QDialogButtonBox.Ok)
        self.image_width_line_edit.textChanged.connect(self._on_image_width_changed)

    def _on_image_width_changed(self, text: str):
        self.ok_button.setEnabled(self.image_width_line_edit.hasAcceptableInput())

    def get_selected_meshroom_pipeline(self) -> str:
        return self.meshroom_pipeline_combobox.currentText

    def get_entered_image_width(self) -> int:
        return int(self.image_width_line_edit.text)

#
# OpenLIFUTransducerTracker
#

class OpenLIFUTransducerTracker(ScriptedLoadableModule):
    """Uses ScriptedLoadableModule base class, available at:
    https://github.com/Slicer/Slicer/blob/main/Base/Python/slicer/ScriptedLoadableModule.py
    """

    def __init__(self, parent):
        ScriptedLoadableModule.__init__(self, parent)
        self.parent.title = _("OpenLIFU Transducer Tracking")
        self.parent.categories = [translate("qSlicerAbstractCoreModule", "OpenLIFU.OpenLIFU Modules")]
        self.parent.dependencies = ['OpenLIFUData',"OpenLIFUHome"]  # add here list of module names that this module requires
        self.parent.contributors = ["Ebrahim Ebrahim (Kitware), Sadhana Ravikumar (Kitware), Peter Hollender (Openwater), Sam Horvath (Kitware)"]
        # short description of the module and a link to online module documentation
        # _() function marks text as translatable to other languages
        self.parent.helpText = _(
            "This is the transducer tracking module of the OpenLIFU extension for focused ultrasound. "
            "More information at <a href=\"https://github.com/OpenwaterHealth/SlicerOpenLIFU\">github.com/OpenwaterHealth/SlicerOpenLIFU</a>."
        )
        # organization, grant, and thanks
        self.parent.acknowledgementText = _(
            "This is part of Openwater's OpenLIFU, an open-source "
            "hardware and software platform for Low Intensity Focused Ultrasound (LIFU) research "
            "and development."
        )


#
# OpenLIFUTransducerTrackerParameterNode
#


@parameterNodeWrapper
class OpenLIFUTransducerTrackerParameterNode:
    pass

#
# OpenLIFUTransducerTrackerDialogs
#

class PhotoscanFromPhotocollectionDialog(qt.QDialog):
    """ Create new photoscan from photocollection dialog. Only displayed if
    there are multiple photocollections. """

    def __init__(self, reference_numbers : List[str], parent="mainWindow"):
        super().__init__(slicer.util.mainWindow() if parent == "mainWindow" else parent)
        """ Args:
                reference_numbers: list of reference numbers for
                photocollections from which to choose to generate a photoscan
        """

        self.setWindowTitle("Select a Photocollection")
        self.setWindowModality(qt.Qt.WindowModal)
        self.resize(600, 400)

        self.reference_numbers : List[str] = reference_numbers
        self.selected_reference_number : str = None

        self.setup()

    def setup(self):

        self.boxLayout = qt.QVBoxLayout()
        self.setLayout(self.boxLayout)

        self.listWidget = qt.QListWidget(self)
        self.listWidget.itemDoubleClicked.connect(self.onItemDoubleClicked)
        self.boxLayout.addWidget(self.listWidget)

        self.buttonBox = qt.QDialogButtonBox(
            qt.QDialogButtonBox.Ok | qt.QDialogButtonBox.Cancel,
            self
        )
        self.boxLayout.addWidget(self.buttonBox)

        self.buttonBox.accepted.connect(self.validateInputs)
        self.buttonBox.rejected.connect(self.reject)

        # display the reference_numbers

        for num in self.reference_numbers:
            display_text = f"Photocollection (Reference Number: {num})"
            self.listWidget.addItem(display_text)


    def onItemDoubleClicked(self, item):
        self.validateInputs()

    def validateInputs(self):

        selected_idx = self.listWidget.currentRow
        if selected_idx >= 0:
            self.selected_reference_number = self.reference_numbers[selected_idx]
        self.accept()

    def get_selected_reference_number(self) -> str:

        return self.selected_reference_number

#
# OpenLIFUTransducerTrackerWidget
#
    
class OpenLIFUTransducerTrackerWidget(ScriptedLoadableModuleWidget, VTKObservationMixin, GuidedWorkflowMixin):
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

        # Keep a reference to the wizard.
        # This is needed to prevent slicer from
        # crashing after the wizard is closed. 
        self.wizard = None

    def setup(self) -> None:
        """Called when the user opens the module the first time and the widget is initialized."""
        ScriptedLoadableModuleWidget.setup(self)

        # Load widget from .ui file (created by Qt Designer).
        # Additional widgets can be instantiated manually and added to self.layout.
        uiWidget = slicer.util.loadUI(self.resourcePath("UI/OpenLIFUTransducerTracker.ui"))
        self.layout.addWidget(uiWidget)
        self.ui = slicer.util.childWidgetVariables(uiWidget)

        # Set scene in MRML widgets. Make sure that in Qt designer the top-level qMRMLWidget's
        # "mrmlSceneChanged(vtkMRMLScene*)" signal in is connected to each MRML widget's.
        # "setMRMLScene(vtkMRMLScene*)" slot.
        uiWidget.setMRMLScene(slicer.mrmlScene)

        # Create logic class. Logic implements all computations that should be possible to run
        # in batch mode, without a graphical user interface.
        self.logic = OpenLIFUTransducerTrackerLogic()

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

        self.addObserver(get_openlifu_data_parameter_node().parameterNode, vtk.vtkCommand.ModifiedEvent, self.onDataParameterNodeModified)

        # This ensures we update the drop down options in the volume and photoscan comboBox when nodes are added/removed
        self.addObserver(slicer.mrmlScene, slicer.vtkMRMLScene.NodeAddedEvent, self.onNodeAdded)
        self.addObserver(slicer.mrmlScene, slicer.vtkMRMLScene.NodeRemovedEvent, self.onNodeRemoved)

        # ---- Photoscan generation connections ----
        data_module = slicer.util.getModuleWidget('OpenLIFUData')
        self.ui.startPhotocollectionCaptureButton.clicked.connect(data_module.onStartPhotocollectionCaptureClicked)
        self.ui.startPhotoscanGenerationButton.clicked.connect(self.onStartPhotoscanGenerationButtonClicked)
        self.ui.importPhotocollectionFromDiskButton.clicked.connect(data_module.onImportPhotocollectionFromDiskClicked)
        self.ui.photoscanGenerationStatusMessage.hide() # Only displayed when running photoscan generation
        # ------------------------------------------

        # Replace the placeholder algorithm input widget by the actual one
        algorithm_input_names = ["Protocol","Volume","Transducer", "Target", "Photoscan"]
        self.algorithm_input_widget = OpenLIFUAlgorithmInputWidget(algorithm_input_names)
        replace_widget(self.ui.algorithmInputWidgetPlaceholder, self.algorithm_input_widget, self.ui)
        self.updateInputOptions()
        self.algorithm_input_widget.connect_combobox_indexchanged_signal(self.checkCanRunTracking)
        self.algorithm_input_widget.connect_combobox_indexchanged_signal(self.updateApprovalWarningsIfAny)

        self.ui.runTrackingButton.clicked.connect(self.onRunTrackingClicked)
        self.ui.previewPhotoscanButton.clicked.connect(self.onPreviewPhotoscanClicked)

        self.updatePhotoscanGenerationButtons()
        self.updateApprovalStatusLabel()
        self.updateApprovalWarningsIfAny()
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

    def setParameterNode(self, inputParameterNode: Optional[OpenLIFUTransducerTrackerParameterNode]) -> None:
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

    def onDataParameterNodeModified(self, caller, event) -> None:
        self.updatePhotoscanGenerationButtons()
        self.updateApprovalStatusLabel()
        self.updateApprovalWarningsIfAny()
        self.updateInputOptions()
        
    @vtk.calldata_type(vtk.VTK_OBJECT)
    def onNodeRemoved(self, caller, event, node : slicer.vtkMRMLNode) -> None:
        """ Update volume and photoscan combo boxes when nodes are removed from the scene"""
        if node.IsA('vtkMRMLMarkupsFiducialNode'):
            with warnings.catch_warnings():
                warnings.simplefilter("ignore") # if the observer doesn't exist, then no problem we don't need to see the warning.
                self.unwatch_fiducial_node(node)
        self.updateInputOptions()

        # If a volume node is removed, clear the associated skin surface and facial landmarks fiducial nodes
        if node.IsA('vtkMRMLScalarVolumeNode'):
            self.logic.clear_any_openlifu_volume_affiliated_nodes(node)

    @vtk.calldata_type(vtk.VTK_OBJECT)
    def onNodeAdded(self, caller, event, node : slicer.vtkMRMLNode) -> None:
        """ Update volume and photoscan combo boxes when nodes are added to the scene"""
        if node.IsA('vtkMRMLMarkupsFiducialNode'):
            self.watch_fiducial_node(node)
        self.updateInputOptions()
    
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

    def updateInputOptions(self):
        """Update the algorithm input options"""
        self.algorithm_input_widget.update()

        # Determine whether transducer tracking can be run based on the status of combo boxes
        self.checkCanRunTracking()

        # Determine whether a photoscan can be previewed based on the status of the photoscan combo box
        self.checkCanPreviewPhotoscan()

    def onStartPhotoscanGenerationButtonClicked(self):
        add_slicer_log_handler("MeshRecon", "Mesh reconstruction")
        add_slicer_log_handler("Meshroom", "Meshroom process", use_dialogs=False)
        reference_numbers = get_openlifu_data_parameter_node().session_photocollections
        if len(reference_numbers) > 1:
            dialog = PhotoscanFromPhotocollectionDialog(reference_numbers)
            if dialog.exec_() == qt.QDialog.Accepted:
                selected_reference_number = dialog.get_selected_reference_number()
                if not selected_reference_number:
                    return
            else:
                return
        else:
            selected_reference_number = reference_numbers[0]

        data_parameter_node = get_openlifu_data_parameter_node()
        if data_parameter_node.loaded_session is None:
            raise RuntimeError("The photoscan generation button should not be clickable without an active session.")
        session_id = data_parameter_node.loaded_session.get_session_id()
        subject_id = data_parameter_node.loaded_session.get_subject_id()
        photoscan_generation_options_dialog = PhotoscanGenerationOptionsDialog(
            openlifu_lz().nav.photoscan.get_meshroom_pipeline_names()
        )
        if photoscan_generation_options_dialog.exec_() == qt.QDialog.Accepted:
            self.updatePhotoscanGeneratorProgressBar(new_photoscan_generator_progress_value = 0)
            self.ui.photoscanGenerationStatusMessage.show()
            self.ui.photoscanGenerationStatusMessage.text = ("Generating mesh... this process can take up to 20 minutes.")
            self.ui.photoscanGenerationStatusMessage.styleSheet = "color:red;"
            try:
                self.logic.generate_photoscan(
                    subject_id = subject_id,
                    session_id = session_id,
                    photocollection_reference_number = selected_reference_number,
                    meshroom_pipeline = photoscan_generation_options_dialog.get_selected_meshroom_pipeline(),
                    image_width = photoscan_generation_options_dialog.get_entered_image_width(),
                )
                self.updatePhotoscanGeneratorProgressBar(new_photoscan_generator_progress_value = 100)
            except CalledProcessError as e:
                slicer.util.errorDisplay("The underlying Meshroom process encountered an error.", "Meshroom error")
                raise e
            finally:
                self.ui.photoscanGenerationStatusMessage.hide()
        data_logic : OpenLIFUDataLogic = slicer.util.getModuleLogic("OpenLIFUData")
        data_logic.update_photoscans_affiliated_with_loaded_session()
        self.updateInputOptions()
        self.updateWorkflowControls()
    
    def updatePhotoscanGeneratorProgressBar(self,  new_photoscan_generator_progress_value = None):
        """Update the photoscan generation progress bar. 
        Currently this function is called before and after photoscan generation is run.
        The progress bar needs to be connected to updates from the meshroom reconstruction pipeline. """
        self.ui.photoscanGeneratorProgressBar.maximum = 100 
        if new_photoscan_generator_progress_value is not None:
            self.ui.photoscanGeneratorProgressBar.value = new_photoscan_generator_progress_value
        else:
            self.ui.photoscanGeneratorProgressBar.value = 0


    def onPreviewPhotoscanClicked(self):

        current_data = self.algorithm_input_widget.get_current_data()
        selected_photoscan_openlifu = current_data['Photoscan']
        
        self.wizard = PhotoscanPreviewWizard(photoscan = selected_photoscan_openlifu)
        # Display dialog
        self.wizard.exec_() 
        self.wizard.deleteLater() # Needed to avoid memory leaks when slicer is exited. 

        # If the photoscan is no longer approved, revoke tt approval if approved
        selected_photoscan = get_openlifu_data_parameter_node().loaded_photoscans[selected_photoscan_openlifu.id]
        if not selected_photoscan.is_approved():
            self.revokeTransducerTrackingApprovalIfAny(photoscan_id = selected_photoscan_openlifu.id,
            reason = "Approval was revoked for the affiliated photoscan.")

        # Update 
        self.updateInputOptions() # To update the photoscan associated with the input options
        self.updateWorkflowControls()

    def checkCanPreviewPhotoscan(self,caller = None, event = None) -> None:
        # If the photoscan combo box has valid data selected then enable the preview photoscan button
        current_data = self.algorithm_input_widget.get_current_data()
        if current_data['Photoscan'] is None:
            self.ui.previewPhotoscanButton.enabled = False
            self.ui.previewPhotoscanButton.setToolTip("Please specify a photoscan to preview")
        else:
            self.ui.previewPhotoscanButton.enabled = True
            self.ui.previewPhotoscanButton.setToolTip("Preview and toggle approval of the selected photoscan before registration")

    def checkCanRunTracking(self,caller = None, event = None) -> None:
        # If all the needed objects/nodes are loaded within the Slicer scene, all of the combo boxes will have valid data selected
        if self.algorithm_input_widget.has_valid_selections():
            current_data = self.algorithm_input_widget.get_current_data()
            transducer = current_data['Transducer']
            photoscan = current_data['Photoscan']
            target = current_data["Target"]
            
            target_is_approved = False
            if target:
                target_id = fiducial_to_openlifu_point_id(target)
                target_is_approved = slicer.util.getModuleLogic('OpenLIFUPrePlanning').get_virtual_fit_approval(target_id)

            if transducer.surface_model_node is None: # Check that the selected transducer has an affiliated registration surface model
                self.ui.runTrackingButton.enabled = False
                self.ui.runTrackingButton.setToolTip("The selected transducer does not have an affiliated registration surface model, which is needed to run tracking.")
            elif get_guided_mode_state() and (photoscan and not photoscan.photoscan_approved): # GM: Check that the selected photoscan is approved
                self.ui.runTrackingButton.enabled = False 
                self.ui.runTrackingButton.setToolTip("The selected photoscan has not been approved for transducer tracking.")
            elif get_guided_mode_state() and (target and not target_is_approved): # GM: Check that virtual fit is approved for the selected target
                self.ui.runTrackingButton.enabled = False
                self.ui.runTrackingButton.setToolTip("Virtual fit has not been approved for the selected target.")
            else:
                self.ui.runTrackingButton.enabled = True
                self.ui.runTrackingButton.setToolTip("Run transducer tracking to align the selected photoscan and transducer registration surface to the MRI volume")
        else:
            self.ui.runTrackingButton.enabled = False
            self.ui.runTrackingButton.setToolTip("Please specify the required inputs")
        
    def onRunTrackingClicked(self):

        activeData = self.algorithm_input_widget.get_current_data()
        selected_photoscan_openlifu = activeData["Photoscan"]
        selected_transducer = activeData["Transducer"]
        
        self.wizard = TransducerTrackingWizard(
            photoscan = selected_photoscan_openlifu,
            volume = activeData["Volume"],
            transducer = activeData["Transducer"],
            target = activeData["Target"])
        
        returncode, photoscan_to_volume_transform_node, transducer_to_volume_transform_node = self.wizard.customexec_()
        self.wizard.deleteLater() # Needed to avoid memory leaks when slicer is exited. 

        if returncode:
            # This shouldn't be possible
            if photoscan_to_volume_transform_node is None or transducer_to_volume_transform_node is None:
                raise RuntimeError("Transducer tracking wizard was completed without generating valid transducer tracking transforms")
            # Watch the transducer tracking results for any deletions/modifications
            self.watchTransducerTrackingNode(photoscan_to_volume_transform_node)
            self.watchTransducerTrackingNode(transducer_to_volume_transform_node)
            # Set the current transducer transform node to the transducer tracking result.
            selected_transducer.set_current_transform_to_match_transform_node(transducer_to_volume_transform_node)
            self.updateWorkflowControls()

    def watchTransducerTrackingNode(self, transducer_tracking_transform_node: vtkMRMLTransformNode):
        """Watch the transducer tracking transform node to revoke approval in case the transform node is approved and then modified."""

        photoscan_id = get_photoscan_id_from_transducer_tracking_result(transducer_tracking_transform_node)
        transform_type = get_transform_type_from_transducer_tracking_result_node(transducer_tracking_transform_node)
        self.addObserver(
            transducer_tracking_transform_node,
            slicer.vtkMRMLTransformNode.TransformModifiedEvent,
            lambda caller, event: self.revokeTransducerTrackingApprovalIfAny(
                photoscan_id = photoscan_id,
                reason=f"The {transform_type.name} transducer tracking transform was modified."),
        )

    def revokeTransducerTrackingApprovalIfAny(self, photoscan_id: str, reason:str):
        """Revoke transducer tracking approval for the transform node if there was an approval,
        and show a message dialog to that effect.
        """
        if self.logic.get_transducer_tracking_approval(photoscan_id):
            slicer.util.infoDisplay(
                text= "Transducer tracking approval has been revoked for the following reason:\n"+reason,
                windowTitle="Approval revoked"
            )
            self.logic.revoke_transducer_tracking_approval(photoscan_id = photoscan_id)
            self.updateApprovalStatusLabel()


    def updateStartPhotocollectionCaptureButton(self):
        if get_openlifu_data_parameter_node().loaded_session is None:
            self.ui.startPhotocollectionCaptureButton.setEnabled(False)
            self.ui.startPhotocollectionCaptureButton.setToolTip("Adding a photocollection requires an active session.")
        else:
            self.ui.startPhotocollectionCaptureButton.setEnabled(True)
            self.ui.startPhotocollectionCaptureButton.setToolTip("Add a photocollection to the active session.")

    def updateStartPhotoscanGenerationButton(self):
        if get_openlifu_data_parameter_node().loaded_session is None:
            self.ui.startPhotoscanGenerationButton.setEnabled(False)
            self.ui.startPhotoscanGenerationButton.setToolTip("Generating a photoscan requires an active session.")
        elif len(get_openlifu_data_parameter_node().session_photocollections) == 0:
            self.ui.startPhotoscanGenerationButton.setEnabled(False)
            self.ui.startPhotoscanGenerationButton.setToolTip("Generating a photoscan requires at least one photocollection.")
        else:
            self.ui.startPhotoscanGenerationButton.setEnabled(True)
            self.ui.startPhotoscanGenerationButton.setToolTip("Click to begin photoscan generation from a photocollection of the subject. This process can take up to 20 minutes.")

    def updateImportPhotocollectionFromDiskButton(self):
        if get_openlifu_data_parameter_node().loaded_session is None:
            self.ui.importPhotocollectionFromDiskButton.setEnabled(False)
            self.ui.importPhotocollectionFromDiskButton.setToolTip("Adding a photocollection requires an active session.")
        else:
            self.ui.importPhotocollectionFromDiskButton.setEnabled(True)
            self.ui.importPhotocollectionFromDiskButton.setToolTip("Add a photocollection to the active session.")

    def updatePhotoscanGenerationButtons(self):
        self.updateStartPhotocollectionCaptureButton()
        self.updateStartPhotoscanGenerationButton()
        self.updateImportPhotocollectionFromDiskButton()

    def updateApprovalStatusLabel(self):
        """ Updates the status message that displays which photoscans have been approved or have
        transducer tracking results that have been approved."""
        approved_photoscan_ids = self.logic.get_photoscan_ids_with_approval()
        if len(approved_photoscan_ids) == 0:
            photoscan_approval_status = "There are currently no photoscan approvals."
        else:
            photoscan_approval_status = (
                "The following photoscans are approved for transducer tracking:\n- "
                + "\n- ".join(approved_photoscan_ids)
            )
        photoscan_ids_with_approved_tt_results = self.logic.get_photoscan_ids_with_approved_tt_results()
        if len(photoscan_ids_with_approved_tt_results ) == 0:
            tt_approval_status = "There are currently no transducer tracking approvals."
        else:
            tt_approval_status = (
                "Transducer tracking is approved for the following photoscans:\n- "
                + "\n- ".join(photoscan_ids_with_approved_tt_results)
            )
        self.ui.approvalStatusLabel.text = photoscan_approval_status + "\n" + tt_approval_status
    
    def updateApprovalWarningsIfAny(self):
        """ Updates the status message that warns the user if virtual fit is not 
        approved for the selected target or if the selected photoscan is not
        approved for transducer tracking"""
        
        current_data = self.algorithm_input_widget.get_current_data()
        selected_photoscan = current_data['Photoscan']
        selected_target = current_data["Target"]
        warnings = ''
        if selected_target:
            target_id = fiducial_to_openlifu_point_id(selected_target)
            target_is_approved = slicer.util.getModuleLogic('OpenLIFUPrePlanning').get_virtual_fit_approval(target_id)
            if not target_is_approved:
                warnings += '\n-Virtual fit is not approved for the selected target.'
        if selected_photoscan and not selected_photoscan.photoscan_approved:
            warnings += '\n-The selected photoscan is not approved for transducer tracking.'
        
        if warnings:
            self.ui.approvalWarningLabel.text = "WARNING(S):" + warnings
            self.ui.approvalWarningLabel.styleSheet = "color:red;"
        else:
            self.ui.approvalWarningLabel.text = ""

    def updateWorkflowControls(self):
        session = get_openlifu_data_parameter_node().loaded_session
        session_id = None if session is None else session.get_session_id()

        current_data = self.algorithm_input_widget.get_current_data()
        selected_photoscan_openlifu = current_data['Photoscan']
        approved_photoscans = self.logic.get_photoscan_ids_with_approval()
        photoscans_with_approved_tt = self.logic.get_photoscan_ids_with_approved_tt_results(approved_photoscans_only = True)
        
        if session is None:
            self.workflow_controls.can_proceed = False
            self.workflow_controls.status_text = "If you are seeing this, guided mode is being run out of order! Load a session to proceed."
        elif not selected_photoscan_openlifu:
            self.workflow_controls.can_proceed = False
            self.workflow_controls.status_text = "Select a photoscan to proceed."
        elif not approved_photoscans:
            self.workflow_controls.can_proceed = False
            self.workflow_controls.status_text = "Approve a photoscan to proceed."
        elif not photoscans_with_approved_tt:
            self.workflow_controls.can_proceed = False
            self.workflow_controls.status_text = "Run transducer tracking for an approved photoscan and approve the result to proceed."
        else:
            self.workflow_controls.can_proceed = True
            self.workflow_controls.status_text = "Approved transducer tracking result detected, proceed to the next step."

#
# OpenLIFUTransducerTrackerLogic
#


class OpenLIFUTransducerTrackerLogic(ScriptedLoadableModuleLogic):
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
        return OpenLIFUTransducerTrackerParameterNode(super().getParameterNode())

    def generate_photoscan(self,
        subject_id:str,
        session_id:str,
        photocollection_reference_number:str,
        meshroom_pipeline:str,
        image_width:int,
    ) -> None:
        """Call mesh reconstruction using openlifu, which should call Meshroom.

        Args:
            subject_id: The subject ID
            session_id: The session ID
            photocollection_reference_number: The photocollection reference number
            meshroom_pipeline: The name of the meshroom pipeline to use. See openlifu.nav.photoscan.get_meshroom_pipeline_names.
            image_width: The image width to which to resize input images before sending them into meshroom
        """
        if get_cur_db() is None:
            raise RuntimeError("Cannot generate photoscan without a database connected to write it into.")
        photocollection_filepaths = get_cur_db().get_photocollection_absolute_filepaths(
            subject_id=subject_id,
            session_id=session_id,
            reference_number=photocollection_reference_number,
        )
        with BusyCursor():
            photoscan, data_dir = openlifu_lz().nav.photoscan.run_reconstruction(
                images = photocollection_filepaths,
                pipeline_name = meshroom_pipeline,
                input_resize_width = image_width,
                use_masks = True,
            )
        photoscan.name = f"{subject_id}'s photoscan during session {session_id} for photocollection {photocollection_reference_number}"
        photoscan_ids = get_cur_db().get_photoscan_ids(subject_id=subject_id, session_id=session_id)
        for i in itertools.count(): # Assumes a finite number of photoscans :)
            photoscan_id = f"{photocollection_reference_number}_{i}"
            if photoscan_id not in photoscan_ids:
                break
        photoscan.id = photoscan_id
        get_cur_db().write_photoscan(
            subject_id = subject_id,
            session_id = session_id,
            photoscan = photoscan,
            model_data_filepath = data_dir/photoscan.model_filename,
            texture_data_filepath = data_dir/photoscan.texture_filename,
            mtl_data_filepath = data_dir/photoscan.mtl_filename,
        )

    def update_photoscan_approval(self, photoscan: SlicerOpenLIFUPhotoscan, approval_state: bool) -> None:
        """Updates the approval status of the given photoscan. """
        
        data_parameter_node = get_openlifu_data_parameter_node()
        session = data_parameter_node.loaded_session
        current_state = photoscan.is_approved()

        if current_state != approval_state: # If the approval state has changed

            # Check that the session doesn't have another approved photoscan
            if approval_state and session: # Only a single photoscan in a session can be approved
                approved_photoscans = self.get_photoscan_ids_with_approval()
                if approved_photoscans:
                    if len(approved_photoscans) > 1:
                        raise RuntimeError("Multiple approved photoscans detected (IDs: {approved_photoscans}). Only one should be approved per session.")
                    slicer.util.infoDisplay(
                    text= f"Only one photoscan can be approved per session. Photoscan ID {approved_photoscans[0]} is already approved. Revoke it to approve another.",
                    windowTitle="Approved photoscan exists"
                    )
                    return

            photoscan.set_approval(approval_state = approval_state)
            # Update the loaded SlicerOpenLIFUPhotoscan.
            data_parameter_node.loaded_photoscans[photoscan.get_id()] = photoscan 
            #  If this is a session-based workflow, update the list of photoscans affiliated with the session
            if session:
                session.update_affiliated_photoscan(photoscan.photoscan.photoscan)
            
    def revoke_transducer_tracking_approval(self, photoscan_id: str) -> bool:
        """Revoke transducer tracking approval for the given  photoscan if there was an approval"""
        session = get_openlifu_data_parameter_node().loaded_session
        session_id = None if session is None else session.get_session_id()
        set_transducer_tracking_approval_for_photoscan(approval_state = False, photoscan_id = photoscan_id, session_id = session_id)
        data_logic : "OpenLIFUDataLogic" = slicer.util.getModuleLogic('OpenLIFUData')
        data_logic.update_underlying_openlifu_session()
            
    def get_transducer_tracking_approval(self, photoscan_id : str) -> bool:
        """Return whether there is a transducer tracking approval for the photoscan. In case there is not even a transducer
        tracking result for the photoscan, this returns False."""
        
        approved_photoscan_ids = self.get_photoscan_ids_with_approved_tt_results()
        return photoscan_id in approved_photoscan_ids
    
    def get_photoscan_ids_with_approved_tt_results(self, approved_photoscans_only = False) -> List[str]:
        """Return a list of photoscan IDs that have approved transducer_tracking, for the currently active session.
        Or if there is no session, then sessionless approved photoscan IDs are returned."""
        
        session = get_openlifu_data_parameter_node().loaded_session
        session_id = None if session is None else session.get_session_id()
        photoscans_with_approved_tt = get_photoscan_ids_with_results(session_id=session_id, approved_only = True)

        if approved_photoscans_only:
            approved_photoscans = set(self.get_photoscan_ids_with_approval())
            photoscans_with_approved_tt = approved_photoscans.intersection(set(photoscans_with_approved_tt))

        return list(photoscans_with_approved_tt)
    
    def get_photoscan_ids_with_approval(self) -> List[str]:
        """Return a list of photoscan IDs that are approved for transducer tracking"""
        session = get_openlifu_data_parameter_node().loaded_session
        approved_photoscans = []
        if not session and not get_openlifu_data_parameter_node().loaded_photoscans:
            return approved_photoscans
        if session:
            approved_photoscans = [id for id, wrapped_photoscan in session.affiliated_photoscans.items() if wrapped_photoscan.photoscan.photoscan_approved]
        elif get_openlifu_data_parameter_node().loaded_photoscans:
            approved_photoscans = [id for id, slicer_photoscan in get_openlifu_data_parameter_node().loaded_photoscans.items() if slicer_photoscan.is_approved()]
        return approved_photoscans
    
    def load_openlifu_photoscan(self, photoscan: "openlifu.nav.photoscan.Photoscan") -> SlicerOpenLIFUPhotoscan:

        # In the manual workflow or if the photoscan has been previously loaded as part of a session
        if photoscan.id in get_openlifu_data_parameter_node().loaded_photoscans:
            loaded_slicer_photoscan = get_openlifu_data_parameter_node().loaded_photoscans[photoscan.id]
        elif get_openlifu_data_parameter_node().loaded_session:
            loaded_slicer_photoscan = slicer.util.getModuleLogic('OpenLIFUData').load_photoscan_from_openlifu(
                    photoscan,
                    load_from_active_session = True)
        # This shouldn't happen - can't click the Preview button without a loaded photoscan or session
        else:
            raise RuntimeError("No photoscans found to preview.") 
        
        return loaded_slicer_photoscan
    
    def update_photoscan_tracking_fiducials_from_node(self, photoscan: SlicerOpenLIFUPhotoscan, fiducial_node: vtkMRMLMarkupsFiducialNode) -> vtkMRMLMarkupsFiducialNode:
        """This is a placeholder function for calling the algorithm for detecting
        initial registration landmarks positions on the photoscan surface. For now, 
        the landmarks are initialized at the origin by default.
        """

        if photoscan.facial_landmarks_fiducial_node is None:
            fiducial_node = photoscan.initialize_facial_landmarks_from_node(fiducial_node)
            # remember to write the updated photoscan into the parameter node
            get_openlifu_data_parameter_node().loaded_photoscans[photoscan.get_id()] = photoscan 
        else:
            # Just update the coorindates in the existing node
            if fiducial_node.GetNumberOfControlPoints() != photoscan.facial_landmarks_fiducial_node.GetNumberOfControlPoints():
                raise RuntimeError("There is an existing fiducial node associated with the photoscan with a different number of control points")
            else:
                for i in range(fiducial_node.GetNumberOfControlPoints()):
                    position = [0.0, 0.0, 0.0]
                    fiducial_node.GetNthControlPointPosition(i, position)
                    photoscan.facial_landmarks_fiducial_node.SetNthControlPointPosition(i, position)
            
        return photoscan.facial_landmarks_fiducial_node
        
    def compute_skin_segmentation(self, volume : vtkMRMLScalarVolumeNode) -> vtkMRMLModelNode:
        """Computes skin segmentation if it has not been created. The ID of the volume node used to create the 
        skin segmentation is added as a model node attribute. Note, this is different from the openlifu volume id.
        """
        skin_mesh_node = [
            node for node in slicer.util.getNodesByClass('vtkMRMLModelNode') 
            if node.GetAttribute('OpenLIFUData.volume_id') == volume.GetID()
            ]
        if len(skin_mesh_node) > 1:
            raise RuntimeError(f"Found multiple skin segmentation models affiliated with volume {volume.GetID()}")
    
        if not skin_mesh_node:
            skin_mesh_node = generate_skin_mesh(volume)
            skin_mesh_node.SetName(f'{volume.GetName()}-skinsegmentation')
            # Set the ID of corresponding volume as a node attribute 
            skin_mesh_node.SetAttribute('OpenLIFUData.volume_id', volume.GetID())
            skin_mesh_node.CreateDefaultDisplayNodes()
            skin_mesh_node.GetDisplayNode().SetVisibility(False) # visibility is turned on by default
        else:
            skin_mesh_node = skin_mesh_node[0]

        return skin_mesh_node

    def get_volume_facial_landmarks(self, volume_or_skin_mesh : Union[vtkMRMLScalarVolumeNode, vtkMRMLModelNode]) -> vtkMRMLMarkupsFiducialNode:
        """Returns the facial landmarks fiducial node affiliated with the specified volume or skin_mesh node. Returns None is
        no affiliated landmarks are found."""

        if isinstance(volume_or_skin_mesh,vtkMRMLScalarVolumeNode):
            volume_tracking_fiducial_id = volume_or_skin_mesh.GetID()
        elif isinstance(volume_or_skin_mesh, vtkMRMLModelNode):
            volume_tracking_fiducial_id = volume_or_skin_mesh.GetAttribute('OpenLIFUData.volume_id')
        else:
            raise ValueError("Invalid input type.")
        
        volume_facial_landmarks_node = [
            node for node in slicer.util.getNodesByClass('vtkMRMLMarkupsFiducialNode') 
            if node.GetAttribute('OpenLIFUData.volume_id') == volume_tracking_fiducial_id
            ]
        if len(volume_facial_landmarks_node) > 1:
            raise RuntimeError(f"Found multiple transducer tracking fiducial nodes affiliated with volume {volume_tracking_fiducial_id}")
        
        if not volume_facial_landmarks_node:
            return None

        return volume_facial_landmarks_node[0]
    
    def update_volume_facial_landmarks_from_node(self, volume_or_skin_mesh : Union[vtkMRMLScalarVolumeNode, vtkMRMLModelNode], fiducial_node: vtkMRMLMarkupsFiducialNode) -> vtkMRMLMarkupsFiducialNode:
        """Clones the provided vtkMRMLMarkupsFiducialNode and returns a new markup node with the required volume metadata as attributes.
        The input fiducial node is expected to contain 3 control points, marking the Right Ear, Left Ear and Nasion on the skin surface mesh. This node
        can be created using the Transducer Tracking Wizard.
        Args:
            volume_or_skin_mesh: The volume or skin mesh node to associate with the landmarks.
            fiducial_node: Fiducial node to clone, containing right ear, nasion and left ear control points.
        """
       
        if isinstance(volume_or_skin_mesh,vtkMRMLScalarVolumeNode):
            volume_name = volume_or_skin_mesh.GetName()
            volume_tracking_fiducial_id = volume_or_skin_mesh.GetID()
        elif isinstance(volume_or_skin_mesh, vtkMRMLModelNode):
            volume_name = volume_or_skin_mesh.GetName().split('-')[0]
            volume_tracking_fiducial_id = volume_or_skin_mesh.GetAttribute('OpenLIFUData.volume_id')
        else:
            raise ValueError("Invalid input type.")
        
        volume_facial_landmarks_node = self.get_volume_facial_landmarks(volume_or_skin_mesh = volume_or_skin_mesh)
        
        if volume_facial_landmarks_node is None:
            # By default, turn visibility off so that the node is visible before the 
            # appropriate view node IDs are set. 
            volume_facial_landmarks_node : vtkMRMLMarkupsFiducialNode = get_cloned_node(fiducial_node)
            volume_facial_landmarks_node.SetName(f"{volume_name}-faciallandmarks")

            # Ensure that visibility is turned off
            volume_facial_landmarks_node.GetDisplayNode().SetVisibility(False)
            volume_facial_landmarks_node.SetMarkupLabelFormat("%N")
            volume_facial_landmarks_node.GetDisplayNode().SetSelectedColor(0,0,1)
            volume_facial_landmarks_node.GetDisplayNode().SetColor(0,0,1)
            # Set the ID of corresponding volume as a node attribute 
            volume_facial_landmarks_node.SetAttribute('OpenLIFUData.volume_id', volume_tracking_fiducial_id)
                
        else:
            # Just update the coorindates in the existing node
            if fiducial_node.GetNumberOfControlPoints() != volume_facial_landmarks_node.GetNumberOfControlPoints():
                raise RuntimeError("There is an existing fiducial markup node associated with the volume with a different number of control points")
            else:
                for i in range(fiducial_node.GetNumberOfControlPoints()):
                    position = [0.0, 0.0, 0.0]
                    fiducial_node.GetNthControlPointPosition(i, position)
                    volume_facial_landmarks_node.SetNthControlPointPosition(i, position)

        return volume_facial_landmarks_node
    
    def run_fiducial_registration(self,
            moving_landmarks: vtkMRMLMarkupsFiducialNode,
            fixed_landmarks: vtkMRMLMarkupsFiducialNode) -> vtkMRMLTransformNode:
        """Runs fiducial registration between the provided fixed and moving fiducial node landmarks and returns the result as a `vtkMRMLTransformNode`."""
        
        fiducial_result_node = slicer.mrmlScene.AddNewNodeByClass("vtkMRMLTransformNode","fiducial_transform_result")
        fiducial_registration_cli = slicer.modules.fiducialregistration
        parameters = {}
        parameters["fixedLandmarks"] = fixed_landmarks
        parameters["movingLandmarks"] = moving_landmarks
        parameters["saveTransform"] = fiducial_result_node
        parameters["transformType"] = "Similarity"
        slicer.cli.run(fiducial_registration_cli, node = None, parameters = parameters, wait_for_completion = True, update_display = False)
        return fiducial_result_node

    def extract_facial_roi_submesh(
        self,
        surface_model_node: vtkMRMLModelNode,
        fiducial_node: vtkMRMLMarkupsFiducialNode, 
        num_points: int = 8,
        surface_selection_distance: int = 40):

        """
        Extracts a facial region of interest (ROI) submesh from a surface model based on fiducial points.

        This function takes a fiducial node containing facial landmarks (specifically right ear,
        nasion, and left ear) and a surface model node of the face. It interpolates a specified
        number of points along the lines defined by these landmarks to create a denser set of
        control points around the eyes and nose. These interpolated points are then used to
        select and extract a submesh from the input surface model using the dynamic modeler's
        "Select by points" tool.

        Args:
            fiducial_node: The input fiducial node containing the original facial landmarks.
                        It is expected to have the 'RightEar', 'Nasion', and
                        'LeftEar' landmarks defined.
            surface_model_node: The surface model node of the face from which to extract the submesh.
            num_points: The number of points to interpolate *between* each pair of original
                        fiducial points (e.g., between RightEar and Nasion). This determines
                        the density of the interpolated control points. Defaults to 11.
            surface_selection_distance: The distance (in millimeters) from the interpolated fiducial
             points within which model points will be selected as part of the submesh. 

        Returns:
            A new vtkMRMLModelNode containing the extracted facial submesh. Returns None if the required 
            landmarks are not found in the fiducial node.
        """

        try:
            # Check for required landmarks
            required_landmarks = ['Right Ear', 'Nasion', 'Left Ear']
            for landmark_label in required_landmarks:
                if fiducial_node.GetControlPointIndexByLabel(landmark_label) == -1:
                    raise ValueError(f"Landmark '{landmark_label}' not found in fiducial node.")
                    return None

            # Interpolate between the  right ear/nasion and nasion/left ear fiducial pairs to generate a dense sampling of
            # control points across the face
            interpolated_facial_landmarks = slicer.mrmlScene.AddNewNodeByClass('vtkMRMLMarkupsFiducialNode', fiducial_node.GetName() + '_interp')
            
            # Create a cell locator for the mesh
            pointsLocator = vtk.vtkPointLocator() # could try using vtk.vtkStaticPointLocator() if need to optimize
            pointsLocator.SetDataSet(surface_model_node.GetPolyData())
            pointsLocator.BuildLocator()

            def linear_interpolate_3d(p1, p2, num_points):
                x = (1 - t) * p1[0] + t * p2[0]
                y = (1 - t) * p1[1] + t * p2[1]
                z = (1 - t) * p1[2] + t * p2[2]
                return [x, y, z]

            for landmark_1, landmark_2 in [['Right Ear','Nasion'],['Nasion','Left Ear']]:

                p1 = [0.0, 0.0, 0.0]
                fiducial_node.GetNthControlPointPositionWorld(fiducial_node.GetControlPointIndexByLabel(landmark_1),p1)
                p2 = [0.0,0.0,0.0]
                fiducial_node.GetNthControlPointPositionWorld(fiducial_node.GetControlPointIndexByLabel(landmark_2),p2)

                for t in np.linspace(0,1,num_points):

                    interpolated_position = linear_interpolate_3d(p1, p2,t)
                    # Find the closest point on the surface model
                    closestPointId = pointsLocator.FindClosestPoint(interpolated_position)
                    closest_point = surface_model_node.GetPolyData().GetPoint(closestPointId)
                    interpolated_facial_landmarks.AddControlPoint(closest_point)

            # Extract submesh using the dynamic modeler, select by points tool
            selectByPointsModeler = slicer.mrmlScene.AddNewNodeByClass("vtkMRMLDynamicModelerNode")
            selectByPointsModeler.SetToolName("Select by points")
            selectByPointsModeler.SetNodeReferenceID("SelectByPoints.InputModel", surface_model_node.GetID())
            selectByPointsModeler.SetNodeReferenceID("SelectByPoints.InputFiducial", interpolated_facial_landmarks.GetID())
            submesh_model_node = slicer.mrmlScene.AddNewNodeByClass("vtkMRMLModelNode", 'Result_SP')  # this node will store the submesh
            selectByPointsModeler.SetNodeReferenceID("SelectByPoints.SelectedFacesModel", submesh_model_node.GetID())
            selectByPointsModeler.SetAttribute("SelectionDistance", "40")  
            selectByPointsModeler.SetAttribute("SelectionAlgorithm", "SphereRadius")  
            slicer.modules.dynamicmodeler.logic().RunDynamicModelerTool(selectByPointsModeler)

            slicer.mrmlScene.RemoveNode(interpolated_facial_landmarks)
            slicer.mrmlScene.RemoveNode(selectByPointsModeler)

            return submesh_model_node
        
        except Exception as e:
            raise RuntimeError(f"Error extracting facial ROI submesh: {e}")
            return None

    def run_icp_model_registration(
        self,
        input_fixed_model: vtkMRMLModelNode,
        input_moving_model: vtkMRMLModelNode,
        transformType: int = 1,
        numLandmarks: int = 200,
        numIterations: int = 100
    ):
        """Registers a moving model to a fixed model using the Iterative Closest Point (ICP) algorithm.
        Note: This function operates directly on the point sets of the
        input models and does not consider any parent transforms. Therefore,
        both input models should be defined within the coordinate system intended for registration.

        Args:
            input_fixed_model (vtkMRMLModelNode): The fixed model (target) to which the moving model will be registered.
            input_moving_model (vtkMRMLModelNode): The moving model (source) that will be transformed to align with the fixed model.
            transformType (int, optional): The type of transformation to be estimated.
                - 0: Rigid body transformation
                - 1: Similarity transformation
                - 2: Affine transformation 
                Defaults to 1 (Similarity).
            numLandmarks: Maximum number of landmarks sampled from the moving model. The default is 200.
            numIterations: Maximum number of iterations. The default is 100.

        Returns:
            vtkMRMLTransformNode or None: A new vtkMRMLTransformNode containing the computed
            transformation that aligns the moving model with the fixed model. Returns None
            if the registration fails. The transform node will be automatically added to the
            scene.
        """

        icp_result_node =  slicer.mrmlScene.AddNewNodeByClass("vtkMRMLLinearTransformNode", "icp_transform_result")

        icpTransform = vtk.vtkIterativeClosestPointTransform()
        icpTransform.SetSource( input_moving_model.GetPolyData() )
        icpTransform.SetTarget( input_fixed_model.GetPolyData() )
        icpTransform.GetLandmarkTransform().SetModeToRigidBody()
        if transformType == 1:
            icpTransform.GetLandmarkTransform().SetModeToSimilarity()
        if transformType == 2:
            icpTransform.GetLandmarkTransform().SetModeToAffine()
        icpTransform.SetMaximumNumberOfIterations( numIterations )
        icpTransform.SetMaximumNumberOfLandmarks( numLandmarks )
        icpTransform.Modified()
        icpTransform.Update()

        icp_result_node.SetMatrixTransformToParent( icpTransform.GetMatrix() )
        icp_result_node.SetNodeReferenceID(slicer.vtkMRMLTransformNode.GetMovingNodeReferenceRole(), input_moving_model.GetID())
        icp_result_node.SetNodeReferenceID(slicer.vtkMRMLTransformNode.GetFixedNodeReferenceRole(), input_fixed_model.GetID())
        
        return icp_result_node

    def add_transducer_tracking_result(
        self,
        photoscan_to_volume_transform: vtkMRMLTransformNode,
        photoscan_to_volume_approval_state: bool,
        transducer_to_volume_transform: vtkMRMLTransformNode,
        transducer_to_volume_approval_state: bool,
        photoscan_id: str,
        transducer: SlicerOpenLIFUTransducer) -> Tuple[vtkMRMLTransformNode, vtkMRMLTransformNode]:
        """Adds transducer tracking result transform nodes to the scene.
        Creates and configures 'PHOTOSCAN_TO_VOLUME' and 'TRANSDUCER_TO_VOLUME'
        transform nodes by cloning the given transform nodes, and associates them
        with the given photoscan and transducer.
        The underlying OpenLIFU session, if any, is updated to include the new result.
        Returns:
            A tuple containing the created photoscan-to-volume and
            transducer-to-volume transform nodes.
        """
        
        session = get_openlifu_data_parameter_node().loaded_session
        session_id : Optional[str] = session.get_session_id() if session is not None else None

        # Check if there is already a transducer tracking result associated with the session. If there is, revoke approval first
        approved_photoscan_id = session.get_transducer_tracking_approvals()
        if approved_photoscan_id and (photoscan_id != approved_photoscan_id[0]):
            self.revoke_transducer_tracking_approval(photoscan_id = approved_photoscan_id[0]) # This does not trigger an info box
            transducer.set_matching_transform(None)

        pv_transform_node = add_transducer_tracking_result(
            transform_node = photoscan_to_volume_transform,
            transform_type = TransducerTrackingTransformType.PHOTOSCAN_TO_VOLUME,
            photoscan_id = photoscan_id,
            session_id = session_id, 
            approval_status = photoscan_to_volume_approval_state,
            replace = True, 
            clone_node = True)
        transducer.move_node_into_transducer_sh_folder(pv_transform_node)

        tv_transform_node = add_transducer_tracking_result(
            transform_node = transducer_to_volume_transform,
            transform_type = TransducerTrackingTransformType.TRANSDUCER_TO_VOLUME,
            photoscan_id = photoscan_id,
            session_id = session_id, 
            approval_status = transducer_to_volume_approval_state,
            replace = True, 
            clone_node = True)
        transducer.move_node_into_transducer_sh_folder(tv_transform_node)
    
        # This should trigger `onDataParameterNodeModified` which will trigger the approval status update
        data_logic : OpenLIFUDataLogic = slicer.util.getModuleLogic('OpenLIFUData')
        data_logic.update_underlying_openlifu_session()

        return (pv_transform_node, tv_transform_node)
    
    def get_transducer_tracking_result_node(self, photoscan_id: str, transform_type: TransducerTrackingTransformType) -> vtkMRMLTransformNode:
        """ Returns 'None' if no result is found """
        session = get_openlifu_data_parameter_node().loaded_session
        session_id : Optional[str] = session.get_session_id() if session is not None else None
    
        transform_node = get_transducer_tracking_result(
                photoscan_id= photoscan_id,
                session_id=session_id,
                transform_type= transform_type) 
        
        return transform_node

    def initialize_node_from_virtual_fit_result(self,
            transducer: SlicerOpenLIFUTransducer,
            target: vtkMRMLMarkupsFiducialNode) -> vtkMRMLTransformNode:
        """Initializes a transform node using the best available virtual fit result for a 
        target fiducial. If no virtual fit result is found, the transform is initialized to the identity matrix. 
        Args:
            transducer: The `SlicerOpenLIFUTransducer` object associated with this transform.
            target: The target for which the virtual fit result should be retrieved.
        """
      
        session = get_openlifu_data_parameter_node().loaded_session
        session_id : Optional[str] = session.get_session_id() if session is not None else None

        # Initialize with virtual fit result if available
        best_virtual_fit_result_node = get_best_virtual_fit_result_node(
            target_id = fiducial_to_openlifu_point_id(target),
            session_id = session_id)
        
        if best_virtual_fit_result_node is None:
            # Initialize transform with identity matrix
            transform_node = transducer_transform_node_from_openlifu(
                openlifu_transform_matrix = np.eye(4) ,
                transducer = transducer.transducer.transducer,
                transform_units = transducer.transducer.transducer.units)
        else:
            virtual_fit_transform = vtk.vtkMatrix4x4()
            best_virtual_fit_result_node.GetMatrixTransformToParent(virtual_fit_transform)
            transform_node = slicer.mrmlScene.AddNewNodeByClass("vtkMRMLTransformNode")
            transform_node.SetMatrixTransformToParent(virtual_fit_transform)
        transform_node.CreateDefaultDisplayNodes()

        return transform_node

    def clear_any_openlifu_volume_affiliated_nodes(self, volume_node: vtkMRMLScalarVolumeNode) -> None:

        # Check for and remove any affiliated skin segmentation models
        skin_mesh_node = [
            node for node in slicer.util.getNodesByClass('vtkMRMLModelNode') 
            if node.GetAttribute('OpenLIFUData.volume_id') == volume_node.GetID()
            ]
        for node in skin_mesh_node:
            slicer.mrmlScene.RemoveNode(node)
        
        # Check for and remove any affiliated facial landmark fiducial nodes
        facial_landmark_node = [
            node for node in slicer.util.getNodesByClass('vtkMRMLMarkupsFiducialNode') 
            if node.GetAttribute('OpenLIFUData.volume_id') == volume_node.GetID()
            ]
        for node in facial_landmark_node:
            slicer.mrmlScene.RemoveNode(node)
