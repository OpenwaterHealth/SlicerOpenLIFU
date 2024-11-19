from typing import Optional, TYPE_CHECKING

import vtk
import qt
import slicer
from slicer.i18n import tr as _
from slicer.i18n import translate
from slicer.ScriptedLoadableModule import *
from slicer.util import VTKObservationMixin
from slicer.parameterNodeWrapper import parameterNodeWrapper
from OpenLIFULib import (
    get_openlifu_data_parameter_node,
    OpenLIFUAlgorithmInputWidget,
)
from OpenLIFULib.util import replace_widget

if TYPE_CHECKING:
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
    """
    The parameters needed by the module.

    """


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

        # Replace the placeholder algorithm input widget by the actual one
        self.algorithm_input_names = ["Protocol", "Transducer", "Volume"]
        self.algorithm_input_widget = OpenLIFUAlgorithmInputWidget(self.algorithm_input_names, parent = self.ui.algorithmInputWidgetPlaceholder.parentWidget())
        replace_widget(self.ui.algorithmInputWidgetPlaceholder, self.algorithm_input_widget, self.ui)
        self.updateInputOptions()

        self.ui.approveButton.clicked.connect(self.onApproveClicked)

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

    def updateInputOptions(self):
        """Update the algorithm input options"""
        self.algorithm_input_widget.update()

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
