from typing import Optional
import warnings

import qt
import vtk

import slicer
from slicer.i18n import tr as _
from slicer.i18n import translate
from slicer.ScriptedLoadableModule import *
from slicer.util import VTKObservationMixin
from slicer.parameterNodeWrapper import parameterNodeWrapper
from slicer import vtkMRMLMarkupsFiducialNode, vtkMRMLScalarVolumeNode

from OpenLIFULib import (
    get_target_candidates,
    get_openlifu_data_parameter_node,
    SlicerOpenLIFUTransducer,
    SlicerOpenLIFUProtocol,
)


class OpenLIFUPrePlanning(ScriptedLoadableModule):
    """Uses ScriptedLoadableModule base class, available at:
    https://github.com/Slicer/Slicer/blob/main/Base/Python/slicer/ScriptedLoadableModule.py
    """

    def __init__(self, parent):
        ScriptedLoadableModule.__init__(self, parent)
        self.parent.title = _("OpenLIFU Pre-Planning")
        self.parent.categories = [translate("qSlicerAbstractCoreModule", "OpenLIFU.OpenLIFU Modules")]
        self.parent.dependencies = []  # add here list of module names that this module requires
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


class OpenLIFUPrePlanningWidget(ScriptedLoadableModuleWidget, VTKObservationMixin):
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

        # These connections ensure that we update parameter node when scene is closed
        self.addObserver(slicer.mrmlScene, slicer.mrmlScene.StartCloseEvent, self.onSceneStartClose)
        self.addObserver(slicer.mrmlScene, slicer.mrmlScene.EndCloseEvent, self.onSceneEndClose)

        # Make sure parameter node is initialized (needed for module reload)
        self.initializeParameterNode()

        self.addObserver(slicer.mrmlScene, slicer.vtkMRMLScene.NodeAddedEvent, self.onNodeAdded)
        self.addObserver(slicer.mrmlScene, slicer.vtkMRMLScene.NodeRemovedEvent, self.onNodeRemoved)
        self.addObserver(get_openlifu_data_parameter_node().parameterNode, vtk.vtkCommand.ModifiedEvent, self.onDataParameterNodeModified)

        self.ui.targetListWidget.currentItemChanged.connect(self.onTargetListWidgetCurrentItemChanged)

        position_coordinate_validator = qt.QDoubleValidator(slicer.util.mainWindow())
        position_coordinate_validator.setNotation(qt.QDoubleValidator.StandardNotation)
        self.ui.positionRLineEdit.setValidator(position_coordinate_validator)
        self.ui.positionALineEdit.setValidator(position_coordinate_validator)
        self.ui.positionSLineEdit.setValidator(position_coordinate_validator)

        self.updateTargetsListView()
        self.updateApproveButtonEnabled()
        self.updateComboBoxOptions()

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
        self.updateComboBoxOptions()

    @vtk.calldata_type(vtk.VTK_OBJECT)
    def onNodeRemoved(self, caller, event, node : slicer.vtkMRMLNode) -> None:
        if node.IsA('vtkMRMLMarkupsFiducialNode'):
            with warnings.catch_warnings():
                warnings.simplefilter("ignore") # if the observer doesn't exist, then no problem we don't need to see the warning.
                self.unwatch_fiducial_node(node)
        self.updateTargetsListView()
        self.updateComboBoxOptions()

    def watch_fiducial_node(self, node:vtkMRMLMarkupsFiducialNode):
        """Add observers so that point-list changes in this fiducial node are tracked by the module."""
        self.addObserver(node,slicer.vtkMRMLMarkupsNode.PointAddedEvent,self.onPointAddedOrRemoved)
        self.addObserver(node,slicer.vtkMRMLMarkupsNode.PointRemovedEvent,self.onPointAddedOrRemoved)

    def unwatch_fiducial_node(self, node:vtkMRMLMarkupsFiducialNode):
        """Un-does watch_fiducial_node; see watch_fiducial_node."""
        self.removeObserver(node,slicer.vtkMRMLMarkupsNode.PointAddedEvent,self.onPointAddedOrRemoved)
        self.removeObserver(node,slicer.vtkMRMLMarkupsNode.PointRemovedEvent,self.onPointAddedOrRemoved)

    def onPointAddedOrRemoved(self, caller, event):
        self.updateTargetsListView()
        self.updateComboBoxOptions()

    def updateTargetsListView(self):
        """Update the list of targets in the target management UI"""
        self.ui.targetListWidget.clear()
        for target_node in get_target_candidates():
            item = qt.QListWidgetItem("{} (ID: {})".format(target_node.GetName(),target_node.GetID()))
            item.setData(qt.Qt.UserRole, target_node)
            self.ui.targetListWidget.addItem(item)

    def getTargetsListViewCurrentSelection(self) -> Optional[vtkMRMLMarkupsFiducialNode]:
        """Get the fiducial node associated to the currently selected target in the list view;
        returns None if nothing is selected."""
        item = self.ui.targetListWidget.currentItem()
        if item is None:
            return None
        return item.data(qt.Qt.UserRole)

    def onTargetListWidgetCurrentItemChanged(self, current:qt.QListWidgetItem, previous:qt.QListWidgetItem):
        pass # This is a stub that will be filled in soon

    def onDataParameterNodeModified(self,caller, event) -> None:
        self.updateApproveButtonEnabled()
        self.updateComboBoxOptions()

    def updateApproveButtonEnabled(self):
        if get_openlifu_data_parameter_node().loaded_session is None:
            self.ui.approveButton.setEnabled(False)
            self.ui.approveButton.setToolTip("There is no active session to write the approval")
        else:
            self.ui.approveButton.setEnabled(True)
            self.ui.approveButton.setToolTip("Approve the current transducer position as a virtual fit for the selected target")

    def add_protocol_to_combobox(self, protocol : SlicerOpenLIFUProtocol) -> None:
        self.ui.protocolComboBox.addItem("{} (ID: {})".format(protocol.protocol.name,protocol.protocol.id), protocol)

    def add_transducer_to_combobox(self, transducer : SlicerOpenLIFUTransducer) -> None:
        transducer_openlifu = transducer.transducer.transducer
        self.ui.transducerComboBox.addItem("{} (ID: {})".format(transducer_openlifu.name,transducer_openlifu.id), transducer)

    def add_volume_to_combobox(self, volume_node : vtkMRMLScalarVolumeNode) -> None:
        self.ui.volumeComboBox.addItem("{} (ID: {})".format(volume_node.GetName(),volume_node.GetID()), volume_node)

    def set_session_related_combobox_tooltip(self, text:str):
        """Set tooltip on the transducer and volume comboboxes."""
        self.ui.protocolComboBox.setToolTip(text)
        self.ui.transducerComboBox.setToolTip(text)
        self.ui.volumeComboBox.setToolTip(text)

    def populateComboBoxOptionsFromLoadedObjects(self):
        """" Update protocol, transducer, and volume comboboxes based on the OpenLIFU objects loaded into the scene."""

        dataParameterNode = get_openlifu_data_parameter_node()

        # Update protocol combo box
        self.ui.protocolComboBox.clear()
        if len(dataParameterNode.loaded_protocols) == 0:
            self.ui.protocolComboBox.addItem("Select a Protocol")
            self.ui.protocolComboBox.setDisabled(True)
        else:
            self.ui.protocolComboBox.setEnabled(True)
            for protocol in dataParameterNode.loaded_protocols.values():
                self.add_protocol_to_combobox(protocol)

        # Update transducer combo box
        self.ui.transducerComboBox.clear()
        if len(dataParameterNode.loaded_transducers) == 0:
            self.ui.transducerComboBox.addItem("Select a Transducer")
            self.ui.transducerComboBox.setDisabled(True)
        else:
            self.ui.transducerComboBox.setEnabled(True)
            for transducer in dataParameterNode.loaded_transducers.values():
                self.add_transducer_to_combobox(transducer)

        # Update volume combo box
        self.ui.volumeComboBox.clear()
        if len(slicer.util.getNodesByClass('vtkMRMLScalarVolumeNode')) == 0:
            self.ui.volumeComboBox.addItem("Select a Volume")
            self.ui.volumeComboBox.setDisabled(True)
        else:
            self.ui.volumeComboBox.setEnabled(True)
            for volume_node in slicer.util.getNodesByClass('vtkMRMLScalarVolumeNode'):
                self.add_volume_to_combobox(volume_node)

        self.set_session_related_combobox_tooltip("")

    def populateComboBoxOptionsFromSession(self):
        """Update protocol, transducer, and volume comboboxes based on the active session, and lock them.

        Does not check that the session is still valid and everything it needs is there in the scene; make sure to
        check before using this.
        """
        dataLogicParameterNode = get_openlifu_data_parameter_node()
        session = dataLogicParameterNode.loaded_session

        # These are the protocol, transducer, and volume that will be used
        protocol : SlicerOpenLIFUProtocol = session.get_protocol()
        transducer : SlicerOpenLIFUTransducer = session.get_transducer()
        volume_node : vtkMRMLScalarVolumeNode = session.volume_node

        # Update protocol combo box
        self.ui.protocolComboBox.clear()
        self.ui.protocolComboBox.setDisabled(True)
        self.add_protocol_to_combobox(protocol)

        # Update transducer combo box
        self.ui.transducerComboBox.clear()
        self.ui.transducerComboBox.setDisabled(True)
        self.add_transducer_to_combobox(transducer)

        # Update volume combo box
        self.ui.volumeComboBox.clear()
        self.ui.volumeComboBox.setDisabled(True)
        self.add_volume_to_combobox(volume_node)

        self.set_session_related_combobox_tooltip("This choice is fixed by the active session")

    def updateComboBoxOptions(self):
        """Update the comboboxes, forcing some of them to take values derived from the active session if there is one"""

        # Update protocol, transducer, and volume comboboxes
        if slicer.util.getModuleLogic('OpenLIFUData').validate_session():
            self.populateComboBoxOptionsFromSession()
        else:
            self.populateComboBoxOptionsFromLoadedObjects()

        # Update target combo box
        self.ui.targetComboBox.clear()
        target_nodes = get_target_candidates()
        if len(target_nodes) == 0:
            self.ui.targetComboBox.addItem("Select a Target")
            self.ui.targetComboBox.setDisabled(True)
        else:
            self.ui.targetComboBox.setEnabled(True)
            for target_node in target_nodes:
                self.ui.targetComboBox.addItem("{} (ID: {})".format(target_node.GetName(),target_node.GetID()), target_node)

        self.updateVirtualfitButtonEnabled()

    def updateVirtualfitButtonEnabled(self):
        """Update the enabled status of the virtual fit button based on whether all comboboxes have valid selections"""
        if all(
            comboBox.currentData is not None
            for comboBox in [
                self.ui.protocolComboBox,
                self.ui.transducerComboBox,
                self.ui.volumeComboBox,
                self.ui.targetComboBox,
            ]
        ):
            self.ui.virtualfitButton.enabled = True
            self.ui.virtualfitButton.setToolTip("Run virtual fit algorithm to automatically suggest a transducer positioning")
        else:
            self.ui.virtualfitButton.enabled = False
            self.ui.virtualfitButton.setToolTip("Specify all required inputs to enable virtual fitting")

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
