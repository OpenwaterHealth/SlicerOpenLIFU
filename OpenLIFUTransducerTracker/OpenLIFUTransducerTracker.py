from typing import Optional, Tuple, TYPE_CHECKING, List, Dict, Union
import numpy as np
import vtk
import qt
import slicer
from slicer.i18n import tr as _
from slicer.i18n import translate
from slicer.ScriptedLoadableModule import *
from slicer.util import VTKObservationMixin
from slicer.parameterNodeWrapper import parameterNodeWrapper
from slicer import (
    vtkMRMLModelNode,
    vtkMRMLTransformNode,
    vtkMRMLScalarVolumeNode,
    vtkMRMLViewNode,
    vtkMRMLMarkupsFiducialNode
    )

from OpenLIFULib.util import replace_widget, BusyCursor
from OpenLIFULib import (
    openlifu_lz,
    get_openlifu_data_parameter_node,
    OpenLIFUAlgorithmInputWidget,
    SlicerOpenLIFUProtocol,
    SlicerOpenLIFUTransducer,
    SlicerOpenLIFUPhotoscan
)

from OpenLIFULib.transducer_tracking_results import (
    TransducerTrackingTransformType,
    add_transducer_tracking_result,
    get_photoscan_id_from_transducer_tracking_result,
    set_transducer_tracking_approval_for_node,
    get_photoscan_ids_with_results,
    get_transducer_tracking_result
)

from OpenLIFULib.transducer_tracking_wizard_utils import (
    initialize_wizard_ui,
    set_threeD_view_node,
    set_threeD_view_widget,
    hide_displayable_nodes_from_view,
    reset_view_node_camera,
    create_threeD_photoscan_view_node,
    get_threeD_transducer_tracking_view_node
)

from OpenLIFULib.virtual_fit_results import get_best_virtual_fit_result_node
from OpenLIFULib.targets import fiducial_to_openlifu_point_id
from OpenLIFULib.coordinate_system_utils import numpy_to_vtk_4x4

from OpenLIFULib.skinseg import generate_skin_mesh

if TYPE_CHECKING:
    import openlifu
    from OpenLIFUData.OpenLIFUData import OpenLIFUDataLogic

class PhotoscanMarkupPage(qt.QWizardPage):
    def __init__(self, parent = None):
        super().__init__()
        self.setTitle("Place facial landmarks on photoscan")
        self.ui = initialize_wizard_ui(self)
        self.viewWidget = set_threeD_view_widget(self.ui)
        self.placingLandmarks = False

        # Connect buttons
        self.ui.dialogControls.setCurrentIndex(1)
        self.ui.placeLandmarksButton.clicked.connect(self.onPlaceLandmarksClicked)

    def initializePage(self):

        set_threeD_view_node(self.viewWidget, threeD_view_node = self.wizard().photoscan_view_node)
 
        # Display the photoscan 
        self.wizard().photoscan.toggle_model_display(visibility_on = True) # Specify a view node for display

        # Specify controls for adding markups to the dialog
        if self.wizard().photoscan.facial_landmarks_fiducial_node:
            self.setupMarkupsWidget()

        self.updatePhotoscanApprovalStatusLabel(self.wizard().photoscan.is_approved())

    def updatePhotoscanApprovalStatusLabel(self, photoscan_is_approved: bool):
        
        loaded_session = get_openlifu_data_parameter_node().loaded_session
        status = "approved" if photoscan_is_approved else "not approved"
        self.ui.photoscanApprovalStatusLabel_Markup.text = (
            f"Photoscan is {status} for transducer tracking" if loaded_session else f"Photoscan approval status: {photoscan_is_approved}."
        )
    
    def onPlaceLandmarksClicked(self):

        photoscan_facial_landmarks_node = self.wizard().photoscan.facial_landmarks_fiducial_node

        if photoscan_facial_landmarks_node is None:
            photoscan_facial_landmarks_node = self.wizard()._logic.initialize_photoscan_tracking_fiducials(self.wizard().photoscan)
            # Set view nodes on fiducials
            self.wizard().photoscan.set_view_nodes(viewNodes = [self.wizard().photoscan_view_node, self.wizard().volume_view_node]) # Specify a view node for display
            self.setupMarkupsWidget()

        if self.ui.placeLandmarksButton.text == "Place/Edit Registration Landmarks":
            photoscan_facial_landmarks_node.SetLocked(False)
            self.ui.placeLandmarksButton.setText("Done Placing Landmarks")
            self.placingLandmarks = True
            # Emit signal to update the enable/disable state of 'Next button'. 
            self.completeChanged()
        elif self.ui.placeLandmarksButton.text == "Done Placing Landmarks":
            photoscan_facial_landmarks_node.SetLocked(True)
            self.ui.placeLandmarksButton.setText("Place/Edit Registration Landmarks")
            self.placingLandmarks = False
            # Emit signal to update the enable/disable state of 'Next button'. 
            self.completeChanged()
    
    def setupMarkupsWidget(self):

        self.ui.photoscanMarkupsWidget.setMRMLScene(slicer.mrmlScene)
        self.ui.photoscanMarkupsWidget.setCurrentNode(self.wizard().photoscan.facial_landmarks_fiducial_node)
        self.wizard().photoscan.facial_landmarks_fiducial_node.SetLocked(True)
        self.ui.photoscanMarkupsWidget.enabled = False

    def isComplete(self):
        """" Determines if the 'Next' button should be enabled"""
        landmarks_exist = self.wizard().photoscan.facial_landmarks_fiducial_node is not None
        return landmarks_exist and not self.placingLandmarks

class SkinSegmentationMarkupPage(qt.QWizardPage):
    def __init__(self, parent = None):
        super().__init__()
        self.setTitle("Place facial landmarks on skin surface")
        self.ui = initialize_wizard_ui(self)
        self.viewWidget = set_threeD_view_widget(self.ui)
        self.ui.dialogControls.setCurrentIndex(2)
    
        # self.skinseg_facial_landmarks = None
        self.placingLandmarks = False

        self.ui.placeLandmarksButtonSkinSeg.clicked.connect(self.onPlaceLandmarksClicked)

    def initializePage(self):
        
        view_node = self.wizard().volume_view_node
        set_threeD_view_node(self.viewWidget, view_node)

        # Display skin segmentation and hide the photoscan and transducer surface
        self.wizard().skin_mesh_node.GetDisplayNode().SetVisibility(True)
        self.wizard().photoscan.toggle_model_display(visibility_on = False) # Specify a view node for display
        self.wizard().transducer_surface.GetDisplayNode().SetVisibility(False)

        reset_view_node_camera(view_node)

        self.skinseg_facial_landmarks = self.wizard()._logic.get_volume_facial_landmarks(self.wizard().skin_mesh_node)
        if self.skinseg_facial_landmarks:
            self.setupMarkupsWidget()
            self.skinseg_facial_landmarks.GetDisplayNode().SetViewNodeIDs([self.wizard().volume_view_node.GetID()]) # Specify a view node for display
            self.skinseg_facial_landmarks.GetDisplayNode().SetVisibility(True)

    def onPlaceLandmarksClicked(self):

        if self.skinseg_facial_landmarks is None:
            self.skinseg_facial_landmarks = self.wizard()._logic.initialize_volume_facial_landmarks(self.wizard().skin_mesh_node)
            # Set view nodes on fiducials
            self.skinseg_facial_landmarks.GetDisplayNode().SetViewNodeIDs([self.wizard().volume_view_node.GetID()]) # Specify a view node for display
            self.skinseg_facial_landmarks.GetDisplayNode().SetVisibility(True)
            self.setupMarkupsWidget()

        if self.ui.placeLandmarksButtonSkinSeg.text == "Place/Edit Registration Landmarks":
            self.skinseg_facial_landmarks.SetLocked(False)
            self.ui.placeLandmarksButtonSkinSeg.setText("Done Placing Landmarks")
            self.placingLandmarks = True
            # Emit signal to update the enable/disable state of 'Next button'. 
            self.completeChanged()
        elif self.ui.placeLandmarksButtonSkinSeg.text == "Done Placing Landmarks":
            self.skinseg_facial_landmarks.SetLocked(True)
            self.ui.placeLandmarksButtonSkinSeg.setText("Place/Edit Registration Landmarks")
            self.placingLandmarks = False
            # Emit signal to update the enable/disable state of 'Next button'. 
            self.completeChanged()
    
    def setupMarkupsWidget(self):

        self.ui.skinSegMarkupsWidget.setMRMLScene(slicer.mrmlScene)
        self.ui.skinSegMarkupsWidget.setCurrentNode(self.skinseg_facial_landmarks)
        self.skinseg_facial_landmarks.SetLocked(True)
        self.ui.skinSegMarkupsWidget.enabled = False

    def isComplete(self):
        """" Determines if the 'Next' button should be enabled"""
        landmarks_exist = self.skinseg_facial_landmarks is not None
        return landmarks_exist and not self.placingLandmarks

class PhotoscanVolumeTrackingPage(qt.QWizardPage):
    def __init__(self, parent = None):
        super().__init__()
        self.setTitle("Register photoscan to skin surface")
        self.ui = initialize_wizard_ui(self)
        self.viewWidget = set_threeD_view_widget(self.ui)
        self.ui.dialogControls.setCurrentIndex(3)
        
        # Temp functionality. This will be determined based on the transform node
        # if it already exists in the scene. 
        self.transform_approved = False

        self.ui.approvePhotoscanVolumeTransform.clicked.connect(self.onTransformApproveClicked)
        self.ui.runPhotoscanVolumeRegistration.clicked.connect(self.onRunRegistrationClicked)
    
    def initializePage(self):
        
        view_node = self.wizard().volume_view_node
        set_threeD_view_node(self.viewWidget, view_node)

        # Display the photoscan and volume and hide the transducer
        self.wizard().skin_mesh_node.GetDisplayNode().SetVisibility(True)
        self.wizard().photoscan.toggle_model_display(visibility_on = True) 
        self.wizard().transducer_surface.GetDisplayNode().SetVisibility(False)
        skinseg_facial_landmarks = self.wizard()._logic.get_volume_facial_landmarks(self.wizard().skin_mesh_node)
        skinseg_facial_landmarks.GetDisplayNode().SetVisibility(True)
    
        reset_view_node_camera(view_node)

        self.updateTransformApprovalStatusLabel()
        self.updateTransformApproveButton()

        self.runningRegistration = False

        self.photoscan_to_volume_transform_node = self.wizard()._logic.get_transducer_tracking_result_node(
            photoscan_id = self.wizard().photoscan.photoscan.photoscan.id,
            transform_type = TransducerTrackingTransformType.PHOTOSCAN_TO_VOLUME)

        if self.photoscan_to_volume_transform_node is None:
            self.photoscan_to_volume_transform_node = self.wizard()._logic.run_photoscan_volume_fiducial_registration(
                photoscan =  self.wizard().photoscan,
                skin_mesh_node = self.wizard().skin_mesh_node,
                transducer = self.wizard().transducer)

        self.photoscan_to_volume_transform_node.GetDisplayNode().SetViewNodeIDs(
            [self.wizard().volume_view_node.GetID()]
            ) # Specify a view node for display
        self.photoscan_to_volume_transform_node.GetDisplayNode().SetEditorVisibility(False)
        
        self.wizard().photoscan.set_transform_node(self.photoscan_to_volume_transform_node)
    
    def updateTransformApprovalStatusLabel(self):
        
        status = "approved" if self.transform_approved else "not approved"
        self.ui.photoscanVolumeTransformApprovalStatusLabel.text = (
            f"The photoscan-volume transform is {status} for transducer tracking"
        )

    def updateTransformApproveButton(self):

        if self.transform_approved:
            self.ui.approvePhotoscanVolumeTransform.setText("Revoke approval")
            self.ui.approvePhotoscanVolumeTransform.setToolTip(
                    "Revoke approval that the current transducer tracking result is correct")
        else:
            self.ui.approvePhotoscanVolumeTransform.setText("Approve photoscan-volume transform")
            self.ui.approvePhotoscanVolumeTransform.setToolTip("Approve the current transducer tracking result")

    def onTransformApproveClicked(self):

        self.transform_approved = not self.transform_approved
        
        # Update the wizard page
        self.updateTransformApprovalStatusLabel()
        self.updateTransformApproveButton()
    
    def onRunRegistrationClicked(self):

        # Need to integrate ICP registration here
        if not self.photoscan_to_volume_transform_node.GetDisplayNode().GetEditorVisibility():
    
            self.ui.ICPPlaceholderLabel.text = "This run button is a placeholder. The transducer tracking algorithm is under development. " \
            "Use the interaction handles to manually align the photoscan and volume mesh." \
            "You can click the run button again to remove the interaction handles."
            self.ui.ICPPlaceholderLabel.setProperty("styleSheet", "color: red;")

            self.photoscan_to_volume_transform_node.GetDisplayNode().SetEditorVisibility(True)

            self.runningRegistration = True

        else:
            self.ui.ICPPlaceholderLabel.text = ""
            self.photoscan_to_volume_transform_node.GetDisplayNode().SetEditorVisibility(False)
            self.runningRegistration = False
        
        # Emit signal to update the enable/disable state of 'Next button'. 
        self.completeChanged()
        
    def isComplete(self):
        """" Determines if the 'Next' button should be enabled"""
        return not self.runningRegistration

class TransducerPhotoscanTrackingPage(qt.QWizardPage):
    def __init__(self, parent = None):
        super().__init__()
        self.setTitle("Register transducer to photoscan")
        self.ui = initialize_wizard_ui(self)
        self.viewWidget = set_threeD_view_widget(self.ui)
        self.ui.dialogControls.setCurrentIndex(4)

        # Temp functionality. This will be determined based on the transform node
        # if it already exists in the scene. 
        self.transform_approved = False
        self.ui.approveTransducerPhotoscanTransform.clicked.connect(self.onTransformApproveClicked)
        self.ui.runTransducerPhotoscanRegistration.clicked.connect(self.onRunRegistrationClicked)
    
    def initializePage(self):

        view_node = self.wizard().volume_view_node
        set_threeD_view_node(self.viewWidget, view_node)

        reset_view_node_camera(view_node)
    
        self.updateTransformApprovalStatusLabel()
        self.updateTransformApproveButton()

        self.transducer_to_volume_transform_node = self.wizard()._logic.get_transducer_tracking_result_node(
            photoscan_id = self.wizard().photoscan.photoscan.photoscan.id,
            transform_type = TransducerTrackingTransformType.TRANSDUCER_TO_VOLUME
        )
        if self.transducer_to_volume_transform_node is None:
            self.transducer_to_volume_transform_node = self.wizard()._logic.initialize_transducer_to_volume_node_from_virtual_fit_result(
                transducer = self.wizard().transducer,
                target = self.wizard().target,
                photoscan_id = self.wizard().photoscan.photoscan.photoscan.id
            )
            
        # This can probably be outside the wizard. And after exiting wizard, reset all view nodes.
        self.runningRegistration = False 
        self.wizard().transducer_surface.SetAndObserveTransformNodeID(self.transducer_to_volume_transform_node.GetID())
        self.transducer_to_volume_transform_node.GetDisplayNode().SetViewNodeIDs(
            [self.wizard().volume_view_node.GetID()]
            ) # Specify a view node for display
        self.transducer_to_volume_transform_node.GetDisplayNode().SetEditorVisibility(False)

        # Display the photoscan and transducer and hide the skin mesh
        self.wizard().skin_mesh_node.GetDisplayNode().SetVisibility(False)
        skinseg_facial_landmarks = self.wizard()._logic.get_volume_facial_landmarks(self.wizard().skin_mesh_node)
        skinseg_facial_landmarks.GetDisplayNode().SetVisibility(False)
        self.wizard().photoscan.toggle_model_display(visibility_on = True) 
        self.wizard().transducer_surface.GetDisplayNode().SetVisibility(True)
        
    def updateTransformApprovalStatusLabel(self):
        
        status = "approved" if self.transform_approved else "not approved"
        self.ui.transducerPhotoscanTransformApprovalStatusLabel.text = (
            f"The transducer-volume transform is {status} for transducer tracking"
        )

    def updateTransformApproveButton(self):

        if self.transform_approved:
            self.ui.approveTransducerPhotoscanTransform.setText("Revoke approval")
            self.ui.approveTransducerPhotoscanTransform.setToolTip(
                    "Revoke approval that the current transducer tracking result is correct")
        else:
            self.ui.approveTransducerPhotoscanTransform.setText("Approve transducer-volume transform")
            self.ui.approveTransducerPhotoscanTransform.setToolTip("Approve the current transducer tracking result")

    def onTransformApproveClicked(self):

        self.transform_approved = not self.transform_approved
        
        # Update the wizard page
        self.updateTransformApprovalStatusLabel()
        self.updateTransformApproveButton()
    
    def onRunRegistrationClicked(self):
        # Need to integrate ICP registration here
        if not self.transducer_to_volume_transform_node.GetDisplayNode().GetEditorVisibility():
            
            self.ui.ICPPlaceholderLabel_2.text = "This run button is a placeholder. The transducer tracking algorithm is under development. " \
            "Use the interaction handles to manually align the transducer and photoscan." \
            "You can click the run button again to remove the interaction handles."
            self.ui.ICPPlaceholderLabel_2.setProperty("styleSheet", "color: red;")

            self.transducer_to_volume_transform_node.GetDisplayNode().SetEditorVisibility(True)
            self.runningRegistration = True

        else:
            self.ui.ICPPlaceholderLabel_2.text = ""
            self.transducer_to_volume_transform_node.GetDisplayNode().SetEditorVisibility(False)
            self.runningRegistration = False
    
        # Emit signal to update the enable/disable state of 'Finish' button. 
        self.completeChanged()

    def isComplete(self):
        """" Determines if the 'Next' button should be enabled"""
        return not self.runningRegistration


class TransducerTrackingWizard(qt.QWizard):
    def __init__(self, photoscan: SlicerOpenLIFUPhotoscan, 
                 skin_mesh_node: vtkMRMLModelNode, 
                 transducer: SlicerOpenLIFUTransducer,
                 target: vtkMRMLMarkupsFiducialNode,
                 photoscan_view_node: vtkMRMLViewNode, 
                 volume_view_node: vtkMRMLViewNode):
        super().__init__()

        self.photoscan = photoscan
        self.skin_mesh_node = skin_mesh_node
        self.transducer = transducer
        self.target = target

        #TODO: Call all setup functions in here

        self.transducer_surface = transducer.surface_model_node
        self.photoscan_view_node = photoscan_view_node
        self.volume_view_node = volume_view_node

        self._logic = OpenLIFUTransducerTrackerLogic()

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
    
class PhotoscanPreviewPage(qt.QWizardPage):
    def __init__(self, parent = None):
        super().__init__()
        self.setTitle("Photoscan preview")

        self.ui = initialize_wizard_ui(self)
        self.viewWidget = set_threeD_view_widget(self.ui)
        self.ui.dialogControls.setCurrentIndex(0)

    def initializePage(self):

        # Connect buttons and signals
        self.updatePhotoscanApproveButton(self.wizard().photoscan.is_approved())
        self.ui.photoscanApprovalButton.clicked.connect(self.onPhotoscanApproveClicked)

        set_threeD_view_node(self.viewWidget, threeD_view_node = self.wizard().photoscan_view_node)
        
        # Display the photoscan 
        self.wizard().photoscan.toggle_model_display(visibility_on=True) # Specify a view node for display

        self.updatePhotoscanApprovalStatusLabel(self.wizard().photoscan.is_approved())

    def updatePhotoscanApprovalStatusLabel(self, photoscan_is_approved: bool):
        
        loaded_session = get_openlifu_data_parameter_node().loaded_session
        status = "approved" if photoscan_is_approved else "not approved"
        self.ui.photoscanApprovalStatusLabel.text = (
            f"Photoscan is {status} for transducer tracking" if loaded_session else f"Photoscan approval status: {photoscan_is_approved}."
        )

    def updatePhotoscanApproveButton(self, photoscan_is_approved: bool):
        loaded_session = get_openlifu_data_parameter_node().loaded_session
        if photoscan_is_approved:
            self.ui.photoscanApprovalButton.setText("Revoke photoscan approval")
            self.ui.photoscanApprovalButton.setToolTip(
                    "Revoke approval that the current photoscan is of sufficient quality to be used for transducer tracking")
        else:
            self.ui.photoscanApprovalButton.setText("Approve photoscan")
            self.ui.photoscanApprovalButton.setToolTip("Approve that the current photoscan can be used for transducer tracking")

        if loaded_session is None:
            self.ui.photoscanApprovalButton.setEnabled(False)
            self.ui.photoscanApprovalButton.setToolTip("Cannot toggle photoscan approval because there is no active session to write the approval")

    def onPhotoscanApproveClicked(self):

        # Update the approval status in the underlying openlifu object
        self.wizard().logic.togglePhotoscanApproval(self.wizard().photoscan)
        
        # Update the wizard page
        self.updatePhotoscanApprovalStatusLabel(self.wizard().photoscan.is_approved())
        self.updatePhotoscanApproveButton(self.wizard().photoscan.is_approved())

class PhotoscanPreviewWizard(qt.QWizard):
    def __init__(self, photoscan : SlicerOpenLIFUPhotoscan, photoscan_view_node: vtkMRMLViewNode):
        super().__init__()

        self.logic = OpenLIFUTransducerTrackerLogic()
        self.photoscan = photoscan
        self.photoscan_view_node = photoscan_view_node

        self.setWindowTitle("Photoscan Preview")
        self.photoscanPreviewPage = PhotoscanPreviewPage(self)
        self.addPage(self.photoscanPreviewPage)

        # Customize view
        self.setOption(qt.QWizard.NoBackButtonOnStartPage)
        self.setOption(qt.QWizard.NoCancelButton)

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
        self.parent.dependencies = ['OpenLIFUData']  # add here list of module names that this module requires
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
    
class OpenLIFUTransducerTrackerWidget(ScriptedLoadableModuleWidget, VTKObservationMixin):
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
        self.ui.addPhotocollectionToSessionButton.clicked.connect(data_module.onAddPhotocollectionToSessionClicked)
        self.ui.startPhotoscanGenerationButton.clicked.connect(self.onStartPhotoscanGenerationButtonClicked)
        # ------------------------------------------

        # Replace the placeholder algorithm input widget by the actual one
        algorithm_input_names = ["Protocol","Volume","Transducer", "Target", "Photoscan"]
        self.algorithm_input_widget = OpenLIFUAlgorithmInputWidget(algorithm_input_names)
        replace_widget(self.ui.algorithmInputWidgetPlaceholder, self.algorithm_input_widget, self.ui)
        self.updateInputOptions()
        self.algorithm_input_widget.connect_combobox_indexchanged_signal(self.checkCanRunTracking)

        self.ui.runTrackingButton.clicked.connect(self.onRunTrackingClicked)
        self.ui.previewPhotoscanButton.clicked.connect(self.onPreviewPhotoscanClicked)

        self.updatePhotoscanGenerationButtons()
        self.updateApprovalStatusLabel()

    def cleanup(self) -> None:
        """Called when the application closes and the module widget is destroyed."""
        self.removeObservers()

    def enter(self) -> None:
        """Called each time the user opens this module."""
        # Make sure parameter node exists and observed
        self.initializeParameterNode()

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
        self.updateInputOptions()
        
    @vtk.calldata_type(vtk.VTK_OBJECT)
    def onNodeRemoved(self, caller, event, node : slicer.vtkMRMLNode) -> None:
        """ Update volume and photoscan combo boxes when nodes are removed from the scene"""
        self.updateInputOptions()

        # If a volume node is removed, clear the associated skin surface and facial landmarks fiducial nodes
        if node.IsA('vtkMRMLScalarVolumeNode'):
            self.logic.clear_any_openlifu_volume_affiliated_nodes(node)

    @vtk.calldata_type(vtk.VTK_OBJECT)
    def onNodeAdded(self, caller, event, node : slicer.vtkMRMLNode) -> None:
        """ Update volume and photoscan combo boxes when nodes are added to the scene"""
        self.updateInputOptions()

    def updateInputOptions(self):
        """Update the algorithm input options"""
        self.algorithm_input_widget.update()

        # Determine whether transducer tracking can be run based on the status of combo boxes
        self.checkCanRunTracking()

        # Determine whether a photoscan can be previewed based on the status of the photoscan combo box
        self.checkCanPreviewPhotoscan()

    def onStartPhotoscanGenerationButtonClicked(self):
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

        print(selected_reference_number)

        # TODO: Pass the reference number to the photocollection to a function
        # that generates the photoscan from the photocollection. I guess this
        # would then add the photoscan to the database here
            
    def onPreviewPhotoscanClicked(self):

        current_data = self.algorithm_input_widget.get_current_data()
        selected_photoscan_openlifu = current_data['Photoscan']
        loaded_slicer_photoscan = self.logic.load_openlifu_photoscan(selected_photoscan_openlifu)
        
        photoscan_view_node = self.setupWizardViewNodes(
            loaded_slicer_photoscan,
            photoscan_preview_only= True)

        wizard = PhotoscanPreviewWizard(loaded_slicer_photoscan, photoscan_view_node)

        # Display dialog
        wizard.exec_()

        # TODO: This should be associated with the finish/cancel signal
        self.resetViewNodes(
            loaded_slicer_photoscan,
            photoscan_preview_only = True)
          
        wizard.deleteLater() # Needed to avoid memory leaks when slicer is exited. 

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
            # Check that the selected transducer has an affiliated registration surface model
            if transducer.surface_model_node:
                self.ui.runTrackingButton.enabled = True
                self.ui.runTrackingButton.setToolTip("Run transducer tracking to align the selected photoscan and transducer registration surface to the MRI volume")
            else:
                self.ui.runTrackingButton.enabled = False
                self.ui.runTrackingButton.setToolTip("The selected transducer does not have an affiliated registration surface model, which is needed to run tracking.")
        else:
            self.ui.runTrackingButton.enabled = False
            self.ui.runTrackingButton.setToolTip("Please specify the required inputs")

    def onRunTrackingClicked(self):

        with BusyCursor():
            
            # Loading the photoscan the first time may take some time depending on the model size
            activeData = self.algorithm_input_widget.get_current_data()
            selected_photoscan_openlifu = activeData["Photoscan"]
            loaded_slicer_photoscan = self.logic.load_openlifu_photoscan(selected_photoscan_openlifu)

            selected_transducer = activeData["Transducer"]
            transducer_registration_surface = selected_transducer.surface_model_node

            # Computing the skin segmentation takes some time the first time
            volume = activeData["Volume"]
            skin_mesh_node = self.logic.compute_skin_segmentation(volume)

            photoscan_view_node, volume_view_node = self.setupWizardViewNodes(
                loaded_slicer_photoscan,
                photoscan_preview_only = False,
                skin_mesh_node =skin_mesh_node,
                transducer_surface = transducer_registration_surface,
                )

            wizard = TransducerTrackingWizard(
                photoscan = loaded_slicer_photoscan,
                skin_mesh_node = skin_mesh_node,
                transducer = activeData["Transducer"],
                target = activeData["Target"],
                photoscan_view_node= photoscan_view_node,
                volume_view_node= volume_view_node)
        
        wizard.exec_()

        # TODO: This should be associated with the finish/cancel signal
        self.resetViewNodes(
            loaded_slicer_photoscan,
            photoscan_preview_only = False,
            skin_mesh_node =skin_mesh_node,
            transducer_surface = transducer_registration_surface,
            )    
        wizard.deleteLater() # Needed to avoid memory leaks when slicer is exited. 

    def setupWizardViewNodes(self,
                             photoscan: SlicerOpenLIFUPhotoscan,
                             photoscan_preview_only = False,
                             skin_mesh_node: Optional[vtkMRMLModelNode] = None,
                             transducer_surface: Optional[vtkMRMLModelNode] = None):
        """ Returns the view node associated with the photoscan.
        If reset_camera_view is True, the view node centers and fits the displayed photoscan in 3D view.
        This should only happen when the user is viewing the photoscan for the first time. 
        If the user has previously interacted with the 3Dview widget, then
        maintain the previous camera/focal point. """
                
        # Create a viewNode for displaying the photoscan if it hasn't been created
        photoscan_id = photoscan.photoscan.photoscan.id
        if photoscan.view_node is None:
            photoscan_view_node = create_threeD_photoscan_view_node(photoscan_id = photoscan_id)
            photoscan.view_node = photoscan_view_node
            reset_camera_view = True
        else:
            photoscan_view_node = photoscan.view_node
            reset_camera_view = False

        wizard_view_nodes = [photoscan_view_node]

        if photoscan_preview_only:
            # Set view nodes on the photoscan
            photoscan.set_view_nodes(wizard_view_nodes)
            
            # Hide all displayable nodes in the scene from the wizard view ndoes
            hide_displayable_nodes_from_view(wizard_view_nodes = wizard_view_nodes)
            
            if reset_camera_view:
                photoscan.toggle_model_display(visibility_on = True)
                reset_view_node_camera(photoscan_view_node)
            
            return photoscan_view_node

        volume_view_node = get_threeD_transducer_tracking_view_node()
        wizard_view_nodes.append(volume_view_node)

        # Set view nodes for the skin mesh, transducer and photoscan
        skin_mesh_node.GetDisplayNode().SetViewNodeIDs([volume_view_node.GetID()])

        # For transducers, ensure that the parent folder visibility is turned on
        transducer_surface.GetDisplayNode().SetViewNodeIDs([volume_view_node.GetID()])
        shNode = slicer.vtkMRMLSubjectHierarchyNode.GetSubjectHierarchyNode(slicer.mrmlScene)
        parentFolderID = shNode.GetItemParent(shNode.GetItemByDataNode(transducer_surface))
        shNode.SetItemDisplayVisibility(parentFolderID, True)

        photoscan.set_view_nodes(wizard_view_nodes)

        # Hide all displayable nodes in the scene from the wizard view nodes
        hide_displayable_nodes_from_view(wizard_view_nodes = wizard_view_nodes)
        
        if reset_camera_view:
            photoscan.toggle_model_display(visibility_on = True)
            reset_view_node_camera(photoscan_view_node)
        
        return wizard_view_nodes
    
    def resetViewNodes(self,
                       photoscan: SlicerOpenLIFUPhotoscan,
                       photoscan_preview_only = False,
                       skin_mesh_node: Optional[vtkMRMLModelNode] = None,
                       transducer_surface: Optional[vtkMRMLModelNode] = None):
        
        photoscan.toggle_model_display(visibility_on = False)
        photoscan.set_view_nodes([])

        if not photoscan_preview_only:
            transducer_surface.GetDisplayNode().SetViewNodeIDs(())
            transducer_surface.GetDisplayNode().SetVisibility(False) # This could be left on? Incase it was on before
            
            skin_mesh_node.GetDisplayNode().SetViewNodeIDs(())
            skin_mesh_node.GetDisplayNode().SetVisibility(False)
            skin_facial_landmarks_node = self.logic.get_volume_facial_landmarks(skin_mesh_node)
            if skin_facial_landmarks_node:
                skin_facial_landmarks_node.GetDisplayNode().SetVisibility(False)
                skin_facial_landmarks_node.GetDisplayNode().SetViewNodeIDs(())

    def watchTransducerTrackingNode(self, transducer_tracking_transform_node: vtkMRMLTransformNode):
        """Watch the transducer tracking transform node to revoke approval in case the transform node is approved and then modified."""

        self.addObserver(
            transducer_tracking_transform_node,
            slicer.vtkMRMLTransformNode.TransformModifiedEvent,
            lambda caller, event: self.revokeApprovalIfAny(
                transducer_tracking_transform_node,
                reason="The transducer tracking transform was modified."),
        )

    def revokeApprovalIfAny(self, transform_node: vtkMRMLTransformNode, reason:str):
        """Revoke transducer tracking approval for the transform node if there was an approval, and show a message dialog to that effect.
        """
        photoscan_id = get_photoscan_id_from_transducer_tracking_result(transform_node)
        if self.logic.get_transducer_tracking_approval(photoscan_id):
            slicer.util.infoDisplay(
                text= "Transducer tracking approval has been revoked for the following reason:\n"+reason,
                windowTitle="Approval revoked"
            )
            self.updateApprovalStatusLabel()
        set_transducer_tracking_approval_for_node(False, transform_node)

    def updateAddPhotocollectionToSessionButton(self):
        if get_openlifu_data_parameter_node().loaded_session is None:
            self.ui.addPhotocollectionToSessionButton.setEnabled(False)
            self.ui.addPhotocollectionToSessionButton.setToolTip("Adding a photocollection requires an active session.")
        else:
            self.ui.addPhotocollectionToSessionButton.setEnabled(True)
            self.ui.addPhotocollectionToSessionButton.setToolTip("Add a photocollection to the active session.")

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

    def updatePhotoscanGenerationButtons(self):
        self.updateAddPhotocollectionToSessionButton()
        self.updateStartPhotoscanGenerationButton()

    def updateApprovalStatusLabel(self):
        
        approved_photoscan_ids = self.logic.get_approved_photoscan_ids()
        if len(approved_photoscan_ids) == 0:
            self.ui.approvalStatusLabel.text = "There are currently no transducer tracking approvals."
        else:
            self.ui.approvalStatusLabel.text = (
                "Transducer tracking is approved for the following photoscans:\n- "
                + "\n- ".join(approved_photoscan_ids)
            )

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

    def togglePhotoscanApproval(self, photoscan: SlicerOpenLIFUPhotoscan) -> None:
        """Approve the specified photoscan if it was not approved. Revoke approval if it was approved. Write changes
        to the underlying openlifu photoscan object to the database. """
        data_parameter_node = get_openlifu_data_parameter_node()
        session = data_parameter_node.loaded_session
        if session is None: # We should never be calling togglePhotoscanApproval if there's no active session.
            raise RuntimeError("Cannot toggle photoscan approval because there is no active session.")
        photoscan.toggle_approval()
        data_parameter_node.loaded_photoscans[photoscan.photoscan.photoscan.id] = photoscan # remember to write the updated photoscan into the parameter node
        
        # Write changes to the database
        loaded_db = slicer.util.getModuleLogic('OpenLIFUData').db
        if loaded_db is None: # This shouldn't happen
            raise RuntimeError("Cannot toggle photoscan approval because there is a session but no database connection to write the approval.")
        OnConflictOpts : "openlifu.db.database.OnConflictOpts" = openlifu_lz().db.database.OnConflictOpts
        loaded_db.write_photoscan(session.get_subject_id(), session.get_session_id(), photoscan.photoscan.photoscan, on_conflict=OnConflictOpts.OVERWRITE)

    def get_transducer_tracking_approval(self, photoscan_id : str) -> bool:
        """Return whether there is a transducer tracking approval for the photoscan. In case there is not even a transducer
        tracking result for the photoscan, this returns False."""
        
        approved_photoscan_ids = self.get_approved_photoscan_ids()
        return photoscan_id in approved_photoscan_ids
    
    def get_approved_photoscan_ids(self) -> List[str]:
        """Return a list of photoscan IDs that have approved transducer_tracking, for the currently active session.
        Or if there is no session, then sessionless approved photoscan IDs are returned."""
        
        session = get_openlifu_data_parameter_node().loaded_session
        session_id = None if session is None else session.get_session_id()
        approved_photoscan_ids = get_photoscan_ids_with_results(session_id=session_id, approved_only = True)
        return approved_photoscan_ids
    
    def load_openlifu_photoscan(self, photoscan: "openlifu.Photoscan") -> SlicerOpenLIFUPhotoscan:

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
    
    def initialize_photoscan_tracking_fiducials(self, photoscan: SlicerOpenLIFUPhotoscan):
        """This is a placeholder function for calling the algorithm for detecting
        initial registration landmarks positions on the photoscan surface. For now, 
        the landmarks are initialized at the origin by default.
        """
        fiducial_node = photoscan.create_facial_landmarks_fiducial_node()
        # remember to write the updated photoscan into the parameter node
        get_openlifu_data_parameter_node().loaded_photoscans[photoscan.photoscan.photoscan.id] = photoscan 

        return fiducial_node
        
    def compute_skin_segmentation(self, volume : vtkMRMLScalarVolumeNode) -> vtkMRMLModelNode:
        """Computes skin segmentation if it has not been created. The ID of the volume node used to create the 
        skin segmentation is added as a model node attribute.Note, this is different from the openlifu volume id.
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

    def get_volume_facial_landmarks(self, volume_or_skin_mesh : Union[vtkMRMLScalarVolumeNode, vtkMRMLModelNode]):

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
    
    def initialize_volume_facial_landmarks(self, volume_or_skin_mesh : Union[vtkMRMLScalarVolumeNode, vtkMRMLModelNode]):
        """"Place holder function until the algorithm for detecting facial landmarks
        using the skin segmentation and mri. """

        if isinstance(volume_or_skin_mesh,vtkMRMLScalarVolumeNode):
            volume_name = volume_or_skin_mesh.GetName()
            volume_tracking_fiducial_id = volume_or_skin_mesh.GetID()
        elif isinstance(volume_or_skin_mesh, vtkMRMLModelNode):
            volume_name = volume_or_skin_mesh.GetName().split('-')[0]
            volume_tracking_fiducial_id = volume_or_skin_mesh.GetAttribute('OpenLIFUData.volume_id')
        else:
            raise ValueError("Invalid input type.")
        
        # For now, initialize them at the origin
        right_ear_coordinates = [0,0,0]
        left_ear_coordinates = [0,0,0]
        nasion_coordinates = [0,0,0]

        volume_facial_landmarks_node = slicer.mrmlScene.AddNewNodeByClass("vtkMRMLMarkupsFiducialNode",f'{volume_name}-faciallandmarks')
        volume_facial_landmarks_node.SetMaximumNumberOfControlPoints(3)
        volume_facial_landmarks_node.SetMarkupLabelFormat("%N")
        volume_facial_landmarks_node.AddControlPoint(right_ear_coordinates[0],right_ear_coordinates[0],right_ear_coordinates[0],"Right Ear")
        volume_facial_landmarks_node.AddControlPoint(left_ear_coordinates[0],left_ear_coordinates[0],left_ear_coordinates[0],"Left Ear")
        volume_facial_landmarks_node.AddControlPoint(nasion_coordinates[0],nasion_coordinates[0],nasion_coordinates[0],"Nasion")

        # Set the ID of corresponding volume as a node attribute 
        volume_facial_landmarks_node.SetAttribute('OpenLIFUData.volume_id', volume_tracking_fiducial_id)
        volume_facial_landmarks_node.CreateDefaultDisplayNodes()
        volume_facial_landmarks_node.GetDisplayNode().SetVisibility(False) # visibility is turned on by default
        volume_facial_landmarks_node.GetDisplayNode().SetSelectedColor(0,1,1)
        volume_facial_landmarks_node.GetDisplayNode().SetColor(0,1,1)
        return volume_facial_landmarks_node
    
    def run_photoscan_volume_fiducial_registration(self,
            photoscan: SlicerOpenLIFUPhotoscan,
            skin_mesh_node: vtkMRMLModelNode,
            transducer : SlicerOpenLIFUTransducer):
        """Initializes the photoscan to volume transform node with the result of
        fiducial registration between the photoscan and skin segmentation. The resulting transform node
        gets added to the scene with the required transducer tracking result attributes.
        Args:
            photoscan: Should contain a valid facial_landmarks_fiducial_node attribute, which are the moving landmarks for registration.
            skin_mesh_node: Should be associated with TODO: complete. 
        """

        photoscan_to_volume_transform_node = slicer.mrmlScene.AddNewNodeByClass("vtkMRMLTransformNode")

        volume_facial_landmarks = self.get_volume_facial_landmarks(skin_mesh_node)
        if volume_facial_landmarks is None:
            raise RuntimeError("Can't perform fiducial registration. The provided skin mesh does not have the necessary landmarks for registration.")
        
        if photoscan.facial_landmarks_fiducial_node is None:
            raise RuntimeError("Can't perform fiducial registration. The provided photoscan does not have the necessary landmarks for registration.")

        fiducial_registration_cli = slicer.modules.fiducialregistration
        parameters = {}
        parameters["fixedLandmarks"] = volume_facial_landmarks
        parameters["movingLandmarks"] = photoscan.facial_landmarks_fiducial_node
        parameters["saveTransform"] = photoscan_to_volume_transform_node
        parameters["transformType"] = "Similarity"
        slicer.cli.run(fiducial_registration_cli, node = None, parameters = parameters, wait_for_completion = True, update_display = False)

        session = get_openlifu_data_parameter_node().loaded_session
        session_id : Optional[str] = session.get_session_id() if session is not None else None

        photoscan_to_volume_result = add_transducer_tracking_result(
            photoscan_to_volume_transform_node,
            TransducerTrackingTransformType.PHOTOSCAN_TO_VOLUME,
            photoscan_id = photoscan.photoscan.photoscan.id,
            session_id = session_id, 
            approval_status = False,
            replace = True, 
            )
        transducer.move_node_into_transducer_sh_folder(photoscan_to_volume_result)

        return photoscan_to_volume_result
    
    def get_transducer_tracking_result_node(self, photoscan_id: str, transform_type: TransducerTrackingTransformType):

        session = get_openlifu_data_parameter_node().loaded_session
        session_id : Optional[str] = session.get_session_id() if session is not None else None
    
        transform_node = get_transducer_tracking_result(
                photoscan_id= photoscan_id,
                session_id=session_id,
                transform_type= transform_type) 
        
        return transform_node

    def initialize_transducer_to_volume_node_from_virtual_fit_result(self,
            transducer: SlicerOpenLIFUTransducer,
            target: vtkMRMLMarkupsFiducialNode,
            photoscan_id: str):
        """Placeholder function for initializing function using
        virtual fit result. For now, a transform node with
        the required transducer tracking result attributes gets added to the scene."""
      
        session = get_openlifu_data_parameter_node().loaded_session
        session_id : Optional[str] = session.get_session_id() if session is not None else None

        # Initialize with virtual fit result
        # TODO: Add check that virtual fit result exists. 
        best_virtual_fit_result_node = get_best_virtual_fit_result_node(
            target_id = fiducial_to_openlifu_point_id(target),
            session_id = session_id)
        virutal_fit_transform = vtk.vtkMatrix4x4()
        best_virtual_fit_result_node.GetMatrixTransformFromParent(virutal_fit_transform)
        
        transducer_to_volume_result = slicer.mrmlScene.AddNewNodeByClass("vtkMRMLTransformNode")
        transducer_to_volume_result.SetMatrixTransformToParent(virutal_fit_transform)

        transducer_to_volume_result = add_transducer_tracking_result(
            transducer_to_volume_result,
            TransducerTrackingTransformType.TRANSDUCER_TO_VOLUME,
            photoscan_id = photoscan_id,
            session_id = session_id, 
            approval_status = False,
            replace = True, 
            )
        
        transducer.move_node_into_transducer_sh_folder(transducer_to_volume_result)
    
        return transducer_to_volume_result

    def clear_any_openlifu_volume_affiliated_nodes(self, volume_node: vtkMRMLScalarVolumeNode):

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