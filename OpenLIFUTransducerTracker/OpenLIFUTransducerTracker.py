from typing import Optional, Tuple, TYPE_CHECKING

import vtk
import qt
import slicer
from slicer.i18n import tr as _
from slicer.i18n import translate
from slicer.ScriptedLoadableModule import *
from slicer.util import VTKObservationMixin
from slicer.parameterNodeWrapper import parameterNodeWrapper
from slicer import vtkMRMLScalarVolumeNode,vtkMRMLModelNode, vtkMRMLTransformNode

from OpenLIFULib import (
    get_openlifu_data_parameter_node,
    OpenLIFUAlgorithmInputWidget,
    SlicerOpenLIFUProtocol,
    SlicerOpenLIFUTransducer,
    SlicerOpenLIFUSession
)
from OpenLIFULib.util import replace_widget

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
        self.ui.skinSegmentationModelqMRMLNodeComboBox.currentNodeChanged.connect(self.checkCanRunTracking)
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
            
    def onPreviewPhotoscanClicked(self):
        session = get_openlifu_data_parameter_node().loaded_session
        current_data = self.algorithm_input_widget.get_current_data()
        selected_photoscan = current_data['Photoscan']
        if session:
            # add busy cursor
            (model_data, texture_data) = self.logic.load_photoscan_data_from_database(session, selected_photoscan.id)
            self.CreateVTKRenderWindow()

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

    def CreateVTKRenderWindow(self):

        # Create a sphere source
        sphere_source = vtk.vtkSphereSource()
        sphere_source.SetRadius(1.0)
        sphere_source.SetThetaResolution(20)
        sphere_source.SetPhiResolution(20)
        sphere_source.Update()

        # Create a mapper
        mapper = vtk.vtkPolyDataMapper()
        mapper.SetInputData(sphere_source.GetOutput())

        # Create an actor
        actor = vtk.vtkActor()
        actor.SetMapper(mapper)

        # Create renderer and add actor
        renderer = vtk.vtkRenderer()
        renderer.AddActor(actor)
        renderer.SetBackground(0.1, 0.2, 0.4)

        # Create render window and interactor
        render_window = vtk.vtkRenderWindow()
        render_window.SetSize(600, 400)
        render_window.AddRenderer(renderer)

        interactor = vtk.vtkRenderWindowInteractor()
        interactor.SetRenderWindow(render_window)

        # Initialize and start interaction
        render_window.Render()
        interactor.Initialize()
        interactor.Start()


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

        self._loaded_database = slicer.util.getModuleLogic('OpenLIFUData').db

    def getParameterNode(self):
        return OpenLIFUTransducerTrackerParameterNode(super().getParameterNode())

    def load_photoscan_data_from_database(self, current_session: SlicerOpenLIFUSession, photoscan_id: str) -> Tuple[vtk.vtkPolyData, vtk.vtkImageData]:
        """TODO: Loads openlifu data and returns vtk data"""

        _,(model_data, texture_data) = self._loaded_database.load_photoscan(current_session.get_subject_id(),current_session.get_session_id(), photoscan_id, load_data = True)
        return (model_data, texture_data)

    def toggleTransducerTrackingApproval(self) -> None:
        """Approve transducer tracking for the currently active session if it was not approved. Revoke approval if it was approved."""
        data_parameter_node = get_openlifu_data_parameter_node()
        session = data_parameter_node.loaded_session
        if session is None: # We should never be calling toggleTransducerTrackingApproval if there's no active session.
            raise RuntimeError("Cannot toggle tracking approval because there is no active session.")
        session.toggle_transducer_tracking_approval() # apply the approval or lack thereof
        data_parameter_node.loaded_session = session # remember to write the updated session object into the parameter node

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