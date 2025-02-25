from typing import Optional, Tuple, TYPE_CHECKING, List

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
    qMRMLThreeDWidget
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

if TYPE_CHECKING:
    import openlifu
    from OpenLIFUData.OpenLIFUData import OpenLIFUDataLogic

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
        self.parent.dependencies = []  # add here list of module names that this module requires
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
        self.photoscanViewWidget = None

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

        # Replace the placeholder algorithm input widget by the actual one
        algorithm_input_names = ["Protocol","Volume","Transducer", "Photoscan"]
        self.algorithm_input_widget = OpenLIFUAlgorithmInputWidget(algorithm_input_names)
        replace_widget(self.ui.algorithmInputWidgetPlaceholder, self.algorithm_input_widget, self.ui)
        self.updateInputOptions()
        self.algorithm_input_widget.connect_combobox_indexchanged_signal(self.checkCanRunTracking)

        self.ui.runTrackingButton.clicked.connect(self.onRunTrackingClicked)
        self.ui.approveButton.clicked.connect(self.onApproveClicked)
        self.ui.skinSegmentationModelqMRMLNodeComboBox.currentNodeChanged.connect(self.checkCanRunTracking) # Temporary functionality
        self.ui.previewPhotoscanButton.clicked.connect(self.onPreviewPhotoscanClicked)

        self.updateApproveButton()
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
        self.updateApproveButton()
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

        # Temporary code to include skin segmentation model as input
        loaded_models = slicer.util.getNodesByClass('vtkMRMLModelNode')
        if len(loaded_models) == 3:
            self.ui.skinSegmentationModelqMRMLNodeComboBox.enabled = False
        else:
            self.ui.skinSegmentationModelqMRMLNodeComboBox.enabled = True

        # Determine whether transducer tracking can be run based on the status of combo boxes
        self.checkCanRunTracking()

        # Determine whether a photoscan can be previewed based on the status of the photoscan combo box
        self.checkCanPreviewPhotoscan()
            
    def onPreviewPhotoscanClicked(self):

        current_data = self.algorithm_input_widget.get_current_data()
        selected_photoscan_openlifu = current_data['Photoscan']

        # In the manual workflow or if the photoscan has been previously loaded as part of a session
        if selected_photoscan_openlifu.id in get_openlifu_data_parameter_node().loaded_photoscans:
            loaded_slicer_photoscan = get_openlifu_data_parameter_node().loaded_photoscans[selected_photoscan_openlifu.id]
        elif get_openlifu_data_parameter_node().loaded_session:
            loaded_slicer_photoscan = slicer.util.getModuleLogic('OpenLIFUData').load_photoscan_from_openlifu(
                    selected_photoscan_openlifu,
                    load_from_active_session = True)
        # This shouldn't happen - can't click the Preview button without a loaded photoscan or session
        else:
            raise RuntimeError("No photoscans found to preview.") 
        
        self.DisplayPhotoscanPreviewDialog(loaded_slicer_photoscan)

    def DisplayPhotoscanPreviewDialog(self,photoscan: SlicerOpenLIFUPhotoscan):
        """ Creates and displays a pop-up, blocking dialog for previewing a SlicerOpenLIFUPhotoscan object"""

        # Create a threeD widget with its own viewNode for displaying the photoscan
        if not self.photoscanViewWidget:
            self.photoscanViewWidget = self._create_threeDview_widget()
        
        # Display the photoscan and hide all displayable nodes from this view node except for the photoscan models
        self._display_photoscan_in_widget(photoscan, self.photoscanViewWidget)

        # Create dialog for photoscan preview and add threeD view widget to dialog
        self.photoscanPreviewDialog = slicer.util.loadUI(self.resourcePath("UI/PhotoscanPreview.ui"))
        self.photoscanPreviewDialogUI = slicer.util.childWidgetVariables(self.photoscanPreviewDialog)
        self.photoscanPreviewDialog.setWindowTitle("Photoscan Preview")
        replace_widget(self.photoscanPreviewDialogUI.photoscanPlaceholderWidget, self.photoscanViewWidget, self.photoscanPreviewDialogUI)
        
        self.updatePhotoscanApproveButton(photoscan.is_approved())
        self.updatePhotoscanApprovalStatusLabel(photoscan.is_approved())

        def onPhotoscanApproveClicked():
            # Update the approval status in the underlying openlifu object
            self.logic.togglePhotoscanApproval(photoscan)
            
            # Update the dialog
            self.updatePhotoscanApproveButton(photoscan.is_approved())
            self.updatePhotoscanApprovalStatusLabel(photoscan.is_approved())

        def onPlaceLandmarksClicked():
            markupsWidget = self.photoscanPreviewDialogUI.photoscanMarkupsPlaceWidget
            markupsWidget.enabled = True
            markupsWidget.setMRMLScene(slicer.mrmlScene)
            markupsNode = slicer.mrmlScene.AddNewNodeByClass("vtkMRMLMarkupsFiducialNode")
            markupsNode.GetDisplayNode().SetViewNodeIDs([self.photoscanViewWidget.mrmlViewNode().GetID()])
            markupsWidget.setCurrentNode(slicer.mrmlScene.GetNodeByID(markupsNode.GetID()))
    
        # Connect buttons
        self.photoscanPreviewDialogUI.photoscanApprovalButton.clicked.connect(onPhotoscanApproveClicked)
        self.photoscanPreviewDialogUI.placeLandmarksButton.clicked.connect(onPlaceLandmarksClicked)

        # Display dialog
        self.photoscanPreviewDialog.exec_()
        self.photoscanPreviewDialog.deleteLater() # Needed to avoid memory leaks when slicer is exited. 

    def _create_threeDview_widget(self):
        
        # Layout name is used to create and identify the underlying view node 
        layoutName = "PhotoscanPreview"
        layoutLabel = "Photoscan Preview"
        layoutColor = [0.97, 0.54, 0.12] # Orange
        # ownerNode manages this view instead of the layout manager (it can be any node in the scene)
        viewOwnerNode = slicer.mrmlScene.AddNewNodeByClass("vtkMRMLScriptedModuleNode")

        # Create a view node if it hasn't been previously created
        viewNode = slicer.util.getFirstNodeByClassByName('vtkMRMLViewNode','ViewPhotoscan')
        if not viewNode:
            viewLogic = slicer.vtkMRMLViewLogic()
            viewLogic.SetMRMLScene(slicer.mrmlScene)
            viewNode = viewLogic.AddViewNode(layoutName)
            viewNode.SetName('ViewPhotoscan')
            viewNode.SetLayoutLabel(layoutLabel)
            viewNode.SetLayoutColor(layoutColor)
            viewNode.SetAndObserveParentLayoutNodeID(viewOwnerNode.GetID())

            # Customize view node. 
            viewNode.SetBackgroundColor(0.98, 0.9,0.77) # shades of orange
            viewNode.SetBackgroundColor2(0.98,0.58,0.4)
            viewNode.SetBoxVisible(False) # Turn off bounding box visibility
            viewNode.SetAxisLabelsVisible(False) # Turn off axis labels visibility

        # Create widget
        viewWidget = slicer.qMRMLThreeDWidget()
        viewWidget.setMRMLScene(slicer.mrmlScene)
        viewWidget.setMRMLViewNode(viewNode)
    
        return viewWidget

    def _display_photoscan_in_widget(self, photoscan: SlicerOpenLIFUPhotoscan, viewWidget: qMRMLThreeDWidget) -> None:

        photoscan_view_node = viewWidget.mrmlViewNode()

        # IDs of all the view nodes in the main Window. This excludes the photoscan widget's view node
        views_mainwindow = [node.GetID() for node in slicer.util.getNodesByClass('vtkMRMLViewNode') if node.GetName() != photoscan_view_node.GetName()]
        
        # Set the view nodes for all displayable nodes
        for displayable_node in list(slicer.util.getNodesByClass('vtkMRMLDisplayableNode')):
            if displayable_node.IsA('vtkMRMLScalarVolumeNode'):
                # Check for any volume renderings
                vrDisplayNode = slicer.modules.volumerendering.logic().GetFirstVolumeRenderingDisplayNode(displayable_node)
                if vrDisplayNode and vrDisplayNode.GetVisibility():
                        vrDisplayNode.SetViewNodeIDs(views_mainwindow)
            elif displayable_node.GetDisplayVisibility():
                displayable_node.GetDisplayNode().SetViewNodeIDs(views_mainwindow)
        
        # Set the view nodes for the Red, Green and Yellow slice nodes
        for slice_node in list(slicer.util.getNodesByClass('vtkMRMLSliceNode')):
            for view_nodeID in views_mainwindow:
                slice_node.AddThreeDViewID(view_nodeID)

        # Display the photoscan (TODO: and fiducials if previously placed)
        photoscan.toggle_model_display(True, photoscan_view_node) # Specify a view node for display

        # Center and fit displayed photoscan in 3D view
        layoutManager = slicer.app.layoutManager()
        for threeDViewIndex in range(layoutManager.threeDViewCount):
            view = layoutManager.threeDWidget(threeDViewIndex).threeDView()
            if view.mrmlViewNode().GetName() == photoscan_view_node.GetName():
                photoscanViewIndex = threeDViewIndex
        
        threeDWidget = layoutManager.threeDWidget(photoscanViewIndex)
        threeDView = threeDWidget.threeDView() 
        threeDView.rotateToViewAxis(3)  # look from anterior direction
        threeDView.resetFocalPoint()  # reset the 3D view cube size and center it
        threeDView.resetCamera()  # reset camera zoom

    def updatePhotoscanApproveButton(self, photoscan_is_approved: bool):

        loaded_session = get_openlifu_data_parameter_node().loaded_session
        if photoscan_is_approved:
            self.photoscanPreviewDialogUI.photoscanApprovalButton.setText("Unapprove photoscan")
            self.photoscanPreviewDialogUI.photoscanApprovalButton.setToolTip(
                    "Revoke approval that the current photoscan is of sufficient quality to be used for transducer tracking")
        else:
            self.photoscanPreviewDialogUI.photoscanApprovalButton.setText("Approve photoscan")
            self.photoscanPreviewDialogUI.photoscanApprovalButton.setToolTip("Approve that the current photoscan can be used for transducer tracking")

        if loaded_session is None:
            self.photoscanPreviewDialogUI.photoscanApprovalButton.setEnabled(False)
            self.photoscanPreviewDialogUI.photoscanApprovalButton.setToolTip("Cannot toggle photoscan approval because there is no active session to write the approval")

    def updatePhotoscanApprovalStatusLabel(self, photoscan_is_approved: bool):
        
        loaded_session = get_openlifu_data_parameter_node().loaded_session
        if loaded_session is not None:
            if photoscan_is_approved:
                self.photoscanPreviewDialogUI.photoscanApprovalStatusLabel.text = "Photoscan is approved for transducer tracking"
            else:
                self.photoscanPreviewDialogUI.photoscanApprovalStatusLabel.text = "Photoscan is not approved for transducer tracking"
        else:
            self.photoscanPreviewDialogUI.photoscanApprovalStatusLabel.text = f"Photoscan approval status: {photoscan_is_approved}."

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
            if transducer.surface_model_node and self.ui.skinSegmentationModelqMRMLNodeComboBox.currentNode() is not None:
                self.ui.runTrackingButton.enabled = True
                self.ui.runTrackingButton.setToolTip("Run transducer tracking to align the selected photoscan and transducer registration surface to the MRI volume")
            elif transducer.surface_model_node and self.ui.skinSegmentationModelqMRMLNodeComboBox.currentNode() is None:
                # This is temporary behavior until skin segmentation is offloaded to an openlifu algorithm
                self.ui.runTrackingButton.enabled = False
                self.ui.runTrackingButton.setToolTip("For now, please also specify a skin segmentation model.")
            elif transducer.surface_model_node is None and self.ui.skinSegmentationModelqMRMLNodeComboBox.currentNode():
                self.ui.runTrackingButton.enabled = False
                self.ui.runTrackingButton.setToolTip("The selected transducer does not have an affiliated registration surface model, which is needed to run tracking.")
        else:
            self.ui.runTrackingButton.enabled = False
            self.ui.runTrackingButton.setToolTip("Please specify the required inputs")

    def onRunTrackingClicked(self):
        activeData = self.algorithm_input_widget.get_current_data()
        self.skinSurfaceModel = self.ui.skinSegmentationModelqMRMLNodeComboBox.currentNode()
        self.logic.runTransducerTracking(activeData["Protocol"], activeData["Transducer"], self.skinSurfaceModel, activeData["Photoscan"])

    def updateApproveButton(self):
        if get_openlifu_data_parameter_node().loaded_session is None:
            self.ui.approveButton.setEnabled(False)
            self.ui.approveButton.setToolTip("There is no active session to write the approval")
            self.ui.approveButton.setText("Approve transducer tracking")
        else:
            self.ui.approveButton.setEnabled(True)
            session_openlifu = get_openlifu_data_parameter_node().loaded_session.session.session
            if session_openlifu.transducer_tracking_approved:
                self.ui.approveButton.setText("Unapprove transducer tracking")
                self.ui.approveButton.setToolTip(
                    "Revoke approval that the current transducer positioning is accurately tracking the real transducer configuration relative to the subject"
                )
            else:
                self.ui.approveButton.setText("Approve transducer tracking")
                self.ui.approveButton.setToolTip(
                    "Approve the current transducer positioning as accurately tracking the real transducer configuration relative to the subject"
                )

    def updateApprovalStatusLabel(self):
        loaded_session = get_openlifu_data_parameter_node().loaded_session
        if loaded_session is not None:
            if loaded_session.transducer_tracking_is_approved():
                self.ui.approvalStatusLabel.text = "Transducer tracking is approved."
            else:
                self.ui.approvalStatusLabel.text = "Transducer tracking is currently unapproved."
        else:
            self.ui.approvalStatusLabel.text = ""

    def onApproveClicked(self):
        self.logic.toggleTransducerTrackingApproval()

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

    def toggleTransducerTrackingApproval(self) -> None:
        """Approve transducer tracking for the currently active session if it was not approved. Revoke approval if it was approved."""
        data_parameter_node = get_openlifu_data_parameter_node()
        session = data_parameter_node.loaded_session
        if session is None: # We should never be calling toggleTransducerTrackingApproval if there's no active session.
            raise RuntimeError("Cannot toggle tracking approval because there is no active session.")
        session.toggle_transducer_tracking_approval() # apply the approval or lack thereof
        data_parameter_node.loaded_session = session # remember to write the updated session object into the parameter node
    
    def togglePhotoscanApproval(self, photoscan: SlicerOpenLIFUPhotoscan) -> None:
        """Approve the specified photoscan if it was not approved. Revoke approval if it was approved. Write changes
        to the underlying openlifu photoscan object to the database. """
        data_parameter_node = get_openlifu_data_parameter_node()
        session = data_parameter_node.loaded_session
        if session is None: # We should never be calling togglePhotoscanApproval if there's no active session.
            raise RuntimeError("Cannot toggle photoscan approval because there is no active session.")
        photoscan.toggle_approval()
        # Write changes to the database
        self._loaded_db = slicer.util.getModuleLogic('OpenLIFUData').db
        if self._loaded_db is None: # This shouldn't happen
            raise RuntimeError("Cannot toggle photoscan approval because there is a session but no database connection to write the approval.")
        OnConflictOpts : "openlifu.db.database.OnConflictOpts" = openlifu_lz().db.database.OnConflictOpts
        self._loaded_db.write_photoscan(session.get_subject_id(), session.get_session_id(), photoscan.photoscan.photoscan, on_conflict=OnConflictOpts.OVERWRITE)

    def runTransducerTracking(self,
                              inputProtocol: SlicerOpenLIFUProtocol,
                              inputTransducer : SlicerOpenLIFUTransducer,
                              inputSkinSegmentation: vtkMRMLModelNode,
                              inputPhotoscan: vtkMRMLModelNode,
                              inputTRS: vtkMRMLModelNode
                              ) -> Tuple[vtkMRMLTransformNode, vtkMRMLTransformNode]:
        ## Need to integrate with transducer tracking library here
        slicer.util.infoDisplay(
            text="This run button is a placeholder. The transducer tracking algorithm is under development.",
            windowTitle="Not implemented"
        )
        return None, None