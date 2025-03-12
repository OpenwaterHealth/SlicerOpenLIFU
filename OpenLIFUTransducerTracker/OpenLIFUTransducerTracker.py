from typing import Optional, Tuple, TYPE_CHECKING, List, Dict
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
    )

from OpenLIFULib.util import replace_widget
from OpenLIFULib import (
    openlifu_lz,
    get_openlifu_data_parameter_node,
    OpenLIFUAlgorithmInputWidget,
    SlicerOpenLIFUProtocol,
    SlicerOpenLIFUTransducer,
    SlicerOpenLIFUPhotoscan
)

from OpenLIFULib.transducer_tracking_results import (
    add_transducer_tracking_result,
    get_photoscan_id_from_transducer_tracking_result,
    set_transducer_tracking_approval_for_node,
    get_photoscan_ids_with_results,
)

from OpenLIFULib.transducer_tracking_wizard_utils import (
    initialize_wizard_ui,
    set_threeD_view_node,
    display_photoscan_in_viewnode,
    create_threeD_photoscan_view_node,
)

from OpenLIFULib.skinseg import generate_skin_mesh

if TYPE_CHECKING:
    import openlifu
    from OpenLIFUData.OpenLIFUData import OpenLIFUDataLogic

class PhotoscanPreviewPage(qt.QWizardPage):
    def __init__(self):
        super().__init__()
        self.setTitle("Photoscan preview")
        self.ui = initialize_wizard_ui(self)
        self.ui.dialogControls.setCurrentIndex(0)

class PhotoscanMarkupPage(qt.QWizardPage):
    def __init__(self):
        super().__init__()
        self.setTitle("Photoscan fiducials markup")
        self.ui = initialize_wizard_ui(self)
        self.ui.dialogControls.setCurrentIndex(1)

class SkinSegmentationMarkupPage(qt.QWizardPage):
    def __init__(self):
        super().__init__()
        self.setTitle("Skin segmentation markup")
        self.ui = initialize_wizard_ui(self)
        self.ui.dialogControls.setCurrentIndex(2)

class PhotoscanVolumeTrackingPage(qt.QWizardPage):
    def __init__(self):
        super().__init__()
        self.setTitle("Register photoscan to volume")
        self.ui = initialize_wizard_ui(self)
        self.ui.dialogControls.setCurrentIndex(3)

class TransducerTrackingWizard(qt.QWizard):
    def __init__(self, photoscan_preview_mode = False):
        super().__init__()

        if photoscan_preview_mode:
            self.setWindowTitle("Photoscan Preview")
            self.photoscanPreviewPage = PhotoscanPreviewPage()
            self.addPage(self.photoscanPreviewPage)

        else:
            self.setWindowTitle("Transducer Tracking Wizard")
            self.photoscanMarkupPage = PhotoscanMarkupPage()
            self.addPage(self.photoscanMarkupPage)
            self.skinSegmentationMarkupPage = SkinSegmentationMarkupPage()
            self.addPage(self.skinSegmentationMarkupPage)
            self.photoscanVolumeTrackingPage = PhotoscanVolumeTrackingPage()
            self.addPage(self.photoscanVolumeTrackingPage)

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
        self.photoscanViewNode : Dict[str, vtkMRMLViewNode] = {}
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
        algorithm_input_names = ["Protocol","Volume","Transducer", "Photoscan"]
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
        self.setupPhotoscanPreviewDialog(loaded_slicer_photoscan)

    def setupPhotoscanPreviewDialog(self,photoscan: SlicerOpenLIFUPhotoscan):
        """ Creates and displays a pop-up, blocking dialog for previewing a SlicerOpenLIFUPhotoscan object.
        This dialog is used for approving/revoking approval of the photoscan"""

        photoscan_id = photoscan.photoscan.photoscan.id
        
        # Create a viewNode for displaying the photoscan if it hasn't been created
        if photoscan_id not in self.photoscanViewNode:
            view_node = create_threeD_photoscan_view_node(photoscan_id = photoscan_id)
            self.photoscanViewNode[photoscan_id] = view_node
            reset_camera_view = True
        else:
            reset_camera_view = False

        wizard = TransducerTrackingWizard(photoscan_preview_mode = True)
        wizard_page = wizard.photoscanPreviewPage
        ui = wizard_page.ui
        set_threeD_view_node(wizard_page= wizard_page,
                             threeD_view_node = self.photoscanViewNode[photoscan_id])
        slicer.util.childWidgetVariables(ui)
        
        # Display the photoscan and hide all displayable nodes from this view node except for the photoscan models
        display_photoscan_in_viewnode(photoscan,
                                      view_node = self.photoscanViewNode[photoscan_id],
                                      reset_camera_view = reset_camera_view)
        
        self.updatePhotoscanApprovalStatusLabel(wizard_page, photoscan.is_approved())

        def onPhotoscanApproveClicked():

            # Update the approval status in the underlying openlifu object
            self.logic.togglePhotoscanApproval(photoscan)
            
            # Update the wizard page
            updatePhotoscanApproveButton(photoscan.is_approved())
            self.updatePhotoscanApprovalStatusLabel(wizard_page, photoscan.is_approved())

        def updatePhotoscanApproveButton(photoscan_is_approved: bool):

            loaded_session = get_openlifu_data_parameter_node().loaded_session
            if photoscan_is_approved:
                ui.photoscanApprovalButton.setText("Revoke photoscan approval")
                ui.photoscanApprovalButton.setToolTip(
                        "Revoke approval that the current photoscan is of sufficient quality to be used for transducer tracking")
            else:
                ui.photoscanApprovalButton.setText("Approve photoscan")
                ui.photoscanApprovalButton.setToolTip("Approve that the current photoscan can be used for transducer tracking")

            if loaded_session is None:
                ui.photoscanApprovalButton.setEnabled(False)
                ui.photoscanApprovalButton.setToolTip("Cannot toggle photoscan approval because there is no active session to write the approval")

        # Connect buttons and signals
        updatePhotoscanApproveButton(photoscan.is_approved())
        ui.photoscanApprovalButton.clicked.connect(onPhotoscanApproveClicked)

        # Display dialog
        wizard.exec_()
        wizard.deleteLater() # Needed to avoid memory leaks when slicer is exited. 

    def updatePhotoscanApprovalStatusLabel(self, wizard_page: qt.QWizardPage, photoscan_is_approved: bool):
        
        loaded_session = get_openlifu_data_parameter_node().loaded_session
        if loaded_session is not None:
            if photoscan_is_approved:
                wizard_page.ui.photoscanApprovalStatusLabel.text = "Photoscan is approved for transducer tracking"
            else:
                wizard_page.ui.photoscanApprovalStatusLabel.text = "Photoscan is not approved for transducer tracking"
        else:
            wizard_page.ui.photoscanApprovalStatusLabel.text = f"Photoscan approval status: {photoscan_is_approved}."

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
        activeData = self.algorithm_input_widget.get_current_data()
        selected_photoscan_openlifu = activeData["Photoscan"]
        
        loaded_slicer_photoscan = self.logic.load_openlifu_photoscan(selected_photoscan_openlifu)
        self.setupTransducerTrackingWizard(photoscan=loaded_slicer_photoscan,
                                           volume = activeData["Volume"])
        
        self.wizard.exec_()
        self.wizard.deleteLater() # Needed to avoid memory leaks when slicer is exited. 

    def setupTransducerTrackingWizard(self, photoscan: SlicerOpenLIFUPhotoscan, volume: vtkMRMLScalarVolumeNode):

        self.wizard = TransducerTrackingWizard()

        self.setupPhotoscanMarkupsPage(photoscan)
        self.setupVolumeMarkupsPage(volume)

    def setupPhotoscanMarkupsPage(self, photoscan: SlicerOpenLIFUPhotoscan):

        if self.wizard is None:
            return
        else:
            current_page = self.wizard.photoscanMarkupPage
        
        photoscan_id = photoscan.photoscan.photoscan.id
        
        # Create a viewNode for displaying the photoscan if it hasn't been created
        if photoscan_id not in self.photoscanViewNode:
            view_node = create_threeD_photoscan_view_node(photoscan_id)
            self.photoscanViewNode[photoscan_id] = view_node
            reset_camera_view = True
        else:
            reset_camera_view = False
    
        set_threeD_view_node(wizard_page=current_page, threeD_view_node = self.photoscanViewNode[photoscan_id])
 
        # Display the photoscan and hide all displayable nodes from this view node except for the photoscan models
        display_photoscan_in_viewnode(photoscan, view_node = self.photoscanViewNode[photoscan_id], reset_camera_view = reset_camera_view)

        # Specify controls for adding markups to the dialog
        if photoscan.tracking_fiducial_node:
            current_page.ui.photoscanMarkupsWidget.setMRMLScene(slicer.mrmlScene)
            current_page.photoscanMarkupPage.ui.photoscanMarkupsWidget.setCurrentNode(photoscan.tracking_fiducial_node)
            photoscan.tracking_fiducial_node.SetLocked(True)
            current_page.ui.photoscanMarkupsWidget.enabled = False
        else:
            current_page.ui.photoscanMarkupsWidget.show()

        self.updatePhotoscanApprovalStatusLabel(current_page, photoscan.is_approved())

        def onPlaceLandmarksClicked():
            markupsWidget = current_page.ui.photoscanMarkupsWidget
            if photoscan.tracking_fiducial_node is None:
                tracking_fiducial_node = self.logic.initialize_photoscan_tracking_fiducials(photoscan)
            else:
                tracking_fiducial_node = photoscan.tracking_fiducial_node
            
            markupsWidget.setMRMLScene(slicer.mrmlScene)
            markupsWidget.setCurrentNode(tracking_fiducial_node)
            markupsWidget.show()
            markupsWidget.enabled = False

            if current_page.ui.placeLandmarksButton.text == "Place/Edit Registration Landmarks":
                tracking_fiducial_node.SetLocked(False)
                current_page.ui.placeLandmarksButton.setText("Done Placing Landmarks")
            
            elif current_page.ui.placeLandmarksButton.text == "Done Placing Landmarks":
                tracking_fiducial_node.SetLocked(True)
                current_page.ui.placeLandmarksButton.setText("Place/Edit Registration Landmarks")

        # Connect buttons
        current_page.ui.placeLandmarksButton.clicked.connect(onPlaceLandmarksClicked)

    def setupVolumeMarkupsPage(self, volume: vtkMRMLScalarVolumeNode):

        if self.wizard is None:
            return
        else:
            current_page = self.wizard.skinSegmentationMarkupPage
        
        # compute skin segmentation
        skin_mesh_node = self.logic.compute_skin_segmentation(volume)
        # skin_mesh_node.CreateDefaultDisplayNodes()
        # skin_mesh_node.GetDisplayNode().SetVisibility(True)

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

    def run_transducer_tracking(self,
                              inputProtocol: SlicerOpenLIFUProtocol,
                              inputTransducer : SlicerOpenLIFUTransducer,
                              inputSkinSegmentation: vtkMRMLModelNode,
                              inputPhotoscan: "openlifu.Photoscan"
                              ) -> Tuple[vtkMRMLTransformNode, vtkMRMLTransformNode]:
        ## Need to integrate with transducer tracking library here
        slicer.util.infoDisplay(
            text="This run button is a placeholder. The transducer tracking algorithm is under development.",
            windowTitle="Not implemented"
        )

        transducer_to_photoscan_transform_node = slicer.mrmlScene.AddNewNodeByClass("vtkMRMLTransformNode")
        photoscan_to_volume_transform_node = slicer.mrmlScene.AddNewNodeByClass("vtkMRMLTransformNode")
        
        session = get_openlifu_data_parameter_node().loaded_session
        session_id : Optional[str] = session.get_session_id() if session is not None else None

        transducer_to_photoscan_result, photoscan_to_volume_result = add_transducer_tracking_result(
            transducer_to_photoscan_transform_node,
            photoscan_to_volume_transform_node,
            photoscan_id = inputPhotoscan.id,
            session_id = session_id, 
            transducer_to_photoscan_approval_status = True,
            photoscan_to_volume_approval_status = False,
            replace = True, # make this True
            )
        
        return (transducer_to_photoscan_result, photoscan_to_volume_result)
    
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
        fiducial_node = photoscan.create_tracking_fiducial_node()
        # remember to write the updated photoscan into the parameter node
        get_openlifu_data_parameter_node().loaded_photoscans[photoscan.photoscan.photoscan.id] = photoscan 

        return fiducial_node
        
    def compute_skin_segmentation(self, volume : vtkMRMLScalarVolumeNode) -> vtkMRMLModelNode:
        skin_mesh = generate_skin_mesh(volume)
        return skin_mesh