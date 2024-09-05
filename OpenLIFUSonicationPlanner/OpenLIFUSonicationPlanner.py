import logging
import os
from typing import Annotated, Optional

import vtk

import slicer
from slicer.i18n import tr as _
from slicer.i18n import translate
from slicer.ScriptedLoadableModule import *
from slicer.util import VTKObservationMixin
from slicer.parameterNodeWrapper import parameterNodeWrapper
from slicer import vtkMRMLScalarVolumeNode,vtkMRMLMarkupsFiducialNode

from OpenLIFULib import (SlicerOpenLIFUProtocol,
                         SlicerOpenLIFUTransducer)


#
# OpenLIFUSonicationPlanner
#


class OpenLIFUSonicationPlanner(ScriptedLoadableModule):
    """Uses ScriptedLoadableModule base class, available at:
    https://github.com/Slicer/Slicer/blob/main/Base/Python/slicer/ScriptedLoadableModule.py
    """

    def __init__(self, parent):
        ScriptedLoadableModule.__init__(self, parent)
        self.parent.title = _("OpenLIFU Sonication Planning")  # TODO: make this more human readable by adding spaces
        self.parent.categories = [translate("qSlicerAbstractCoreModule", "OpenLIFU.OpenLIFU Modules")]
        self.parent.dependencies = []  # add here list of module names that this module requires
        self.parent.contributors = ["Ebrahim Ebrahim (Kitware), Sadhana Ravikumar (Kitware), Peter Hollender (Openwater), Sam Horvath (Kitware), Brad Moore (Kitware)"]
        # short description of the module and a link to online module documentation
        # _() function marks text as translatable to other languages
        self.parent.helpText = _(
            "This is the sonication module of the OpenLIFU extension for focused ultrasound. "
            "More information at <a href=\"https://github.com/OpenwaterHealth/SlicerOpenLIFU\">github.com/OpenwaterHealth/SlicerOpenLIFU</a>."
        )
        # organization, grant, and thanks
        self.parent.acknowledgementText = _(
            "This is part of Openwater's OpenLIFU, an open-source "
            "hardware and software platform for Low Intensity Focused Ultrasound (LIFU) research "
            "and development."
        )



#
# OpenLIFUSonicationPlannerParameterNode
#


@parameterNodeWrapper
class OpenLIFUSonicationPlannerParameterNode:
    """
    The parameters needed by module.

    """

    activeVolume: vtkMRMLScalarVolumeNode
    activeTarget: vtkMRMLMarkupsFiducialNode


#
# OpenLIFUSonicationPlannerWidget
#


class OpenLIFUSonicationPlannerWidget(ScriptedLoadableModuleWidget, VTKObservationMixin):
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
        uiWidget = slicer.util.loadUI(self.resourcePath("UI/OpenLIFUSonicationPlanner.ui"))
        self.layout.addWidget(uiWidget)
        self.ui = slicer.util.childWidgetVariables(uiWidget)

        # Set scene in MRML widgets. Make sure that in Qt designer the top-level qMRMLWidget's
        # "mrmlSceneChanged(vtkMRMLScene*)" signal in is connected to each MRML widget's.
        # "setMRMLScene(vtkMRMLScene*)" slot.
        uiWidget.setMRMLScene(slicer.mrmlScene)

        # Create logic class. Logic implements all computations that should be possible to run
        # in batch mode, without a graphical user interface.
        self.logic = OpenLIFUSonicationPlannerLogic()

        # Connections

        # These connections ensure that we update parameter node when scene is closed
        self.addObserver(slicer.mrmlScene, slicer.mrmlScene.StartCloseEvent, self.onSceneStartClose)
        self.addObserver(slicer.mrmlScene, slicer.mrmlScene.EndCloseEvent, self.onSceneEndClose)

        # Initialize combo boxes
        self.OpenLIFUDataLogic = slicer.util.getModuleLogic('OpenLIFUData')
        self.updateComboBoxOptions()

        # Add an observer on the Data module's parameter node
        self.addObserver(self.OpenLIFUDataLogic.getParameterNode().parameterNode, vtk.vtkCommand.ModifiedEvent, self.onDataParameterNodeModified)

        # This ensures we update the drop down options in the volume and fiducial combo boxes when nodes are added/removed
        self.addObserver(slicer.mrmlScene, slicer.vtkMRMLScene.NodeAddedEvent, self.onNodeAdded)
        self.addObserver(slicer.mrmlScene, slicer.vtkMRMLScene.NodeRemovedEvent, self.onNodeRemoved)

        # Buttons
        self.ui.PlanPushButton.clicked.connect(self.onPlanClicked)

        # Make sure parameter node is initialized (needed for module reload)
        self.initializeParameterNode()

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
            self.addObserver(self._parameterNode, vtk.vtkCommand.ModifiedEvent, self.checkCanPlan)
        self.checkCanPlan()
    

    def checkCanPlan(self, caller = None, event = None) -> None:

        # If all the needed objects/nodes are loaded within the Slicer scene, all of the combo boxes will be enabled
        # This means that the plan button can be enabled
        if self.ui.TargetComboBox.enabled and self.ui.ProtocolComboBox.enabled and self.ui.VolumeComboBox.enabled and self.ui.TargetComboBox.enabled:
            self.ui.PlanPushButton.enabled = True
            self.ui.PlanPushButton.setToolTip("Execute planning")
        else:
            self.ui.PlanPushButton.enabled = False
            self.ui.PlanPushButton.setToolTip("Please specify the required inputs")

    @vtk.calldata_type(vtk.VTK_OBJECT)
    def onNodeRemoved(self, caller, event, node : slicer.vtkMRMLNode) -> None:
        """ Update volume and target combo boxes when nodes are added to the scene"""
        self.updateComboBoxOptions()

    @vtk.calldata_type(vtk.VTK_OBJECT)
    def onNodeAdded(self, caller, event, node : slicer.vtkMRMLNode) -> None:
        """ Update volume and target combo boxes when nodes are removed from the scene"""
        self.updateComboBoxOptions()

    def updateComboBoxOptions(self):
        """" Update all the combo boxes based on the openLIFU objects
        loaded into the scene. The protocol and transducer information is stored in the openLIFUDataModule's parameter node.
        The volumes and fiducials are tracked as vtkMRML nodes. 
        The dropdowns are disabled if the relevant OpenLIFU objects/nodes aren't found"""

        dataLogicParameterNode = self.OpenLIFUDataLogic.getParameterNode()

        # Update parameter combo box
        self.ui.ProtocolComboBox.clear() 
        if len(dataLogicParameterNode.loaded_protocols) == 0:
            self.ui.ProtocolComboBox.addItems(["Select a Protocol"])
            self.ui.ProtocolComboBox.setDisabled(True)
        else:
            self.ui.ProtocolComboBox.setEnabled(True)
            for protocol in dataLogicParameterNode.loaded_protocols.values():
                # Better to use ID or name here? Showing both for now
                self.ui.ProtocolComboBox.addItems(["{} (ID: {})".format(protocol.protocol.name,protocol.protocol.id)]) 
    
        # Update transducer combo box
        self.ui.TransducerComboBox.clear()
        if len(dataLogicParameterNode.loaded_transducers) == 0:
            self.ui.TransducerComboBox.addItems(["Select a Transducer"]) 
            self.ui.TransducerComboBox.setDisabled(True)
        else:
            self.ui.TransducerComboBox.setEnabled(True)
            for transducer in dataLogicParameterNode.loaded_transducers.values():
                transducer_openlifu = transducer.transducer.transducer
                # Better to use ID or name here? Showing both for now
                self.ui.TransducerComboBox.addItems(["{} (ID: {})".format(transducer_openlifu.name,transducer_openlifu.id)]) 

        # Update volume combo box 
        self.ui.VolumeComboBox.clear()  
        if len(slicer.util.getNodesByClass('vtkMRMLScalarVolumeNode')) == 0:
            self.ui.VolumeComboBox.addItems(["Select a Volume"])
            self.ui.VolumeComboBox.setDisabled(True)
        else:
            self.ui.VolumeComboBox.setEnabled(True)
            for volume_node in slicer.util.getNodesByClass('vtkMRMLScalarVolumeNode'):
                self.ui.VolumeComboBox.addItems(["{} (ID: {})".format(volume_node.GetName(),volume_node.GetID())])
          
        # Update target combo box 
        self.ui.TargetComboBox.clear()  
        if len(slicer.util.getNodesByClass('vtkMRMLMarkupsFiducialNode')) == 0:
            self.ui.TargetComboBox.addItems(["Select a Target"])
            self.ui.TargetComboBox.setDisabled(True)
        else:
            self.ui.TargetComboBox.setEnabled(True)
            for target_node in slicer.util.getNodesByClass('vtkMRMLMarkupsFiducialNode'):
                self.ui.TargetComboBox.addItems(["{} (ID: {})".format(target_node.GetName(),target_node.GetID())])

    def onDataParameterNodeModified(self,caller, event) -> None:
        self.updateComboBoxOptions()

    def onPlanClicked(self):
        print("Run sonication planning")

        dataLogicParameterNode = self.OpenLIFUDataLogic.getParameterNode()
        activeTransducer = list(dataLogicParameterNode.loaded_transducers.values())[self.ui.TransducerComboBox.currentIndex]
        activeProtocol = list(dataLogicParameterNode.loaded_protocols.values())[self.ui.ProtocolComboBox.currentIndex]
        activeVolume = list(slicer.util.getNodesByClass('vtkMRMLScalarVolumeNode'))[self.ui.VolumeComboBox.currentIndex]
        activeTarget = list(slicer.util.getNodesByClass('vtkMRMLMarkupsFiducialNode'))[self.ui.TargetComboBox.currentIndex]

        # Call runPlanning
        self.logic.runPlanning(activeVolume, activeTarget, activeTransducer, activeProtocol)
      
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

    def runPlanning(self, inputVolume: vtkMRMLScalarVolumeNode, inputTarget: vtkMRMLMarkupsFiducialNode, inputTransducer : SlicerOpenLIFUTransducer , inputProtocol: SlicerOpenLIFUProtocol ):
        print("Volume:", inputVolume)
        print("Target:", inputTarget)
        print("Protocol:", inputProtocol)
        print("Transducer:", inputTransducer)


#
# OpenLIFUSonicationPlannerTest
#

class OpenLIFUSonicationPlannerTest(ScriptedLoadableModuleTest):
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
      