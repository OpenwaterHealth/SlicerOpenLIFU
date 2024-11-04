from typing import Optional, Callable, Dict, List

import qt
import vtk
from datetime import datetime

import slicer
from slicer.i18n import tr as _
from slicer.i18n import translate
from slicer.ScriptedLoadableModule import *
from slicer.util import VTKObservationMixin
from slicer.parameterNodeWrapper import parameterNodeWrapper

from OpenLIFULib import (get_openlifu_data_parameter_node, 
                         SlicerOpenLIFUSolution,
                         openlifu_lz,
                         SlicerOpenLIFURun
)

from OpenLIFULib.util import display_errors

#
# OpenLIFUSonicationControl
#


class OpenLIFUSonicationControl(ScriptedLoadableModule):
    """Uses ScriptedLoadableModule base class, available at:
    https://github.com/Slicer/Slicer/blob/main/Base/Python/slicer/ScriptedLoadableModule.py
    """

    def __init__(self, parent):
        ScriptedLoadableModule.__init__(self, parent)
        self.parent.title = _("OpenLIFU Sonication Control")  # TODO: make this more human readable by adding spaces
        self.parent.categories = [translate("qSlicerAbstractCoreModule", "OpenLIFU.OpenLIFU Modules")]
        self.parent.dependencies = []  # add here list of module names that this module requires
        self.parent.contributors = ["Ebrahim Ebrahim (Kitware), Sadhana Ravikumar (Kitware), Peter Hollender (Openwater), Sam Horvath (Kitware), Brad Moore (Kitware)"]
        # short description of the module and a link to online module documentation
        # _() function marks text as translatable to other languages
        self.parent.helpText = _(
            "This is the sonication control module of the OpenLIFU extension for focused ultrasound. "
            "More information at <a href=\"https://github.com/OpenwaterHealth/SlicerOpenLIFU\">github.com/OpenwaterHealth/SlicerOpenLIFU</a>."
        )
        # organization, grant, and thanks
        self.parent.acknowledgementText = _(
            "This is part of Openwater's OpenLIFU, an open-source "
            "hardware and software platform for Low Intensity Focused Ultrasound (LIFU) research "
            "and development."
        )

#
# OpenLIFUSonicationControlParameterNode
#


@parameterNodeWrapper
class OpenLIFUSonicationControlParameterNode:
    """
    The parameters needed by module.

    """

#
# OpenLIFUSonicationControlDialogs
#

class onRunCompletedDialog(qt.QDialog):
    """ Dialog to save run """

    def __init__(self, parent="mainWindow"):
        super().__init__(slicer.util.mainWindow() if parent == "mainWindow" else parent)
        self.setWindowTitle("Run completed")
        self.setWindowModality(1)
        self.setup()

    def setup(self):

        self.setMinimumWidth(200)

        vBoxLayout = qt.QVBoxLayout()
        self.setLayout(vBoxLayout)

        self.label = qt.QLabel()
        self.label.setText("Sonication control completed. Do you want to save this run? ")
        vBoxLayout.addWidget(self.label)

        self.successfulCheckBox = qt.QCheckBox('Check this box if the run was successful.')
        self.successfulCheckBox.setStyleSheet("font-weight: bold")
        vBoxLayout.addWidget(self.successfulCheckBox)

        self.label_notes = qt.QLabel()
        self.label_notes.setText("Enter additional notes to include (optional):")
        vBoxLayout.addWidget(self.label_notes)
        self.textBox = qt.QTextEdit()
        vBoxLayout.addWidget(self.textBox)

        self.buttonBox = qt.QDialogButtonBox()
        self.buttonBox.setStandardButtons(qt.QDialogButtonBox.Save |
                                          qt.QDialogButtonBox.Cancel)
        vBoxLayout.addWidget(self.buttonBox)

        self.buttonBox.rejected.connect(self.reject)
        self.buttonBox.accepted.connect(self.accept)
    
    def customexec_(self):

        returncode = self.exec_()
        run_parameters = {
            'success_flag': self.successfulCheckBox.isChecked(),
            'note': self.textBox.toPlainText(),
        }

        return (returncode, run_parameters)

#
# OpenLIFUSonicationControlWidget
#


class OpenLIFUSonicationControlWidget(ScriptedLoadableModuleWidget, VTKObservationMixin):
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
        uiWidget = slicer.util.loadUI(self.resourcePath("UI/OpenLIFUSonicationControl.ui"))
        self.layout.addWidget(uiWidget)
        self.ui = slicer.util.childWidgetVariables(uiWidget)

        # Set scene in MRML widgets. Make sure that in Qt designer the top-level qMRMLWidget's
        # "mrmlSceneChanged(vtkMRMLScene*)" signal in is connected to each MRML widget's.
        # "setMRMLScene(vtkMRMLScene*)" slot.
        uiWidget.setMRMLScene(slicer.mrmlScene)

        # Create logic class. Logic implements all computations that should be possible to run
        # in batch mode, without a graphical user interface.
        self.logic = OpenLIFUSonicationControlLogic()

        # These connections ensure that we update parameter node when scene is closed
        self.addObserver(slicer.mrmlScene, slicer.mrmlScene.StartCloseEvent, self.onSceneStartClose)
        self.addObserver(slicer.mrmlScene, slicer.mrmlScene.EndCloseEvent, self.onSceneEndClose)

        # Buttons
        self.ui.runPushButton.clicked.connect(self.onRunClicked)
        self.ui.abortPushButton.clicked.connect(self.onAbortClicked)
        self.updateRunEnabled()
        self.updateAbortEnabled()
        self.logic.call_on_running_changed(self.onRunningChanged)
        self.logic.call_on_sonication_complete(self.onRunCompleted)
        self.logic.call_on_run_progress_updated(self.updateRunProgressBar)

        # Initialize UI
        self.updateRunProgressBar()

        # Add an observer on the Data module's parameter node
        self.addObserver(
            get_openlifu_data_parameter_node().parameterNode,
            vtk.vtkCommand.ModifiedEvent,
            self.onDataParameterNodeModified
        )

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


    def setParameterNode(self, inputParameterNode: Optional[OpenLIFUSonicationControlParameterNode]) -> None:
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

    def onDataParameterNodeModified(self,caller, event) -> None:
        self.updateRunEnabled()
        self.updateRunProgressBar()

    def updateRunEnabled(self):
        solution = get_openlifu_data_parameter_node().loaded_solution
        if solution is None:
            self.ui.runPushButton.enabled = False
            self.ui.runPushButton.setToolTip("To run a sonication, first generate and approve a solution in the sonication planning module.")
        elif self.logic.running:
            self.ui.runPushButton.enabled = False
            self.ui.runPushButton.setToolTip("Currently running...")
        elif not solution.is_approved():
            self.ui.runPushButton.enabled = False
            self.ui.runPushButton.setToolTip("Cannot run because the currently active solution is not approved. It can be approved in the sonication planning module.")
        else:
            self.ui.runPushButton.enabled = True
            self.ui.runPushButton.setToolTip("Run sonication")

    def updateAbortEnabled(self):
        self.ui.abortPushButton.setEnabled(self.logic.running)

    @display_errors
    def onRunCompleted(self, new_sonication_run_complete_state: bool):
        """If the soniction_run_complete variable changes from False to True, then open the RunComplete 
        dialog to determine whether the run should be saved. Saving the run creates a SlicerOpenLIFURun object and 
        writes the run to the database (only if there is an active session)."""
        if new_sonication_run_complete_state:
            runCompleteDialog = onRunCompletedDialog()
            returncode, run_parameters = runCompleteDialog.customexec_()
            if returncode:
                self.logic.create_openlifu_run(run_parameters)

    def onRunningChanged(self, new_running_state:bool):
        self.updateRunEnabled()
        self.updateAbortEnabled()

    def onRunClicked(self):
        if not slicer.util.getModuleLogic('OpenLIFUData').validate_solution():
            raise RuntimeError("Invalid solution; not running sonication.")
        solution = get_openlifu_data_parameter_node().loaded_solution

        self.ui.runProgressBar.value = 0
        self.logic.run(solution) 
        
    def onAbortClicked(self):
        self.logic.abort()

    def updateRunProgressBar(self, new_run_progress_value = None):
        """Update the run progress bar. 0% if there is no existing  run, 100% if there is an existing run."""
        self.ui.runProgressBar.maximum = 100 # (during computation we set maxmimum=0 to put it into an infinite loading animation)
        if new_run_progress_value is not None:
            self.ui.runProgressBar.value = new_run_progress_value
        else:
            if get_openlifu_data_parameter_node().loaded_run is None:
                self.ui.runProgressBar.value = 0
            else:
                self.ui.runProgressBar.value = 100
# OpenLIFUSonicationControlLogic
#


class OpenLIFUSonicationControlLogic(ScriptedLoadableModuleLogic):

    def __init__(self) -> None:
        """Called when the logic class is instantiated. Can be used for initializing member variables."""
        ScriptedLoadableModuleLogic.__init__(self)

        self._running : bool = False
        """Whether sonication is currently running. Do not set this directly -- use the `running` property."""

        self._sonication_run_complete : bool = False
        """Whether sonication finished running till completion. Do not set this directly -- use the `sonication_run_complete` property.
        This variable is needed to distinguish when a run has ended due to sonication completion as opposed to the user aborting the process"""

        self._on_running_changed_callbacks : List[Callable[[bool],None]] = []
        """List of functions to call when `running` property is changed."""

        self._on_sonication_run_complete_changed_callbacks : List[Callable[[bool],None]] = []
        """List of functions to call when `sonication_run_complete` property is changed."""

        self._run_progress : int = 0
        """ The amount of progress made by the sonication algorithm. Do not set this directly -- use the `run_progress` property."""

        self._on_run_progress_updated_callbacks: List[Callable[[int],None]] = []
        """List of functions to call when `run_progress` property is changed."""

    def getParameterNode(self):
        return OpenLIFUSonicationControlParameterNode(super().getParameterNode())

    def call_on_running_changed(self, f : Callable[[bool],None]) -> None:
        """Set a function to be called whenever the `running` property is changed.
        The provided callback should accept a single bool argument which will be the new running state.
        """
        self._on_running_changed_callbacks.append(f)

    def call_on_sonication_complete(self, f: Callable[[bool], None]) -> None:
        """Set a function to be called whenever the `sonication_run_complete` property is changed.
        The provided callback should accept a single bool argument which will indicate whether the sonication run is complete.
        """
        self._on_sonication_run_complete_changed_callbacks.append(f)

    def call_on_run_progress_updated(self, f : Callable[[int],None]) -> None:
        """Set a function to be called whenever the `run_progress` property is changed.
        The provided callback should accept a single int value which will indicate the percentage (i.e. scale 0-100)
        of progress made by the sonication control algorithm.
        """
        self._on_run_progress_updated_callbacks.append(f)

    @property
    def running(self) -> bool:
        """Whether sonication is currently running"""
        return self._running

    @running.setter
    def running(self, running_value : bool):
        self._running = running_value
        for f in self._on_running_changed_callbacks:
            f(self._running)

    @property
    def sonication_run_complete(self) -> bool:
        """Whether sonication ran till completion"""
        return self._sonication_run_complete
    
    @sonication_run_complete.setter
    def sonication_run_complete(self, sonication_run_complete_value : bool):
        self._sonication_run_complete = sonication_run_complete_value
        for f in self._on_sonication_run_complete_changed_callbacks:
            f(self._sonication_run_complete)

    @property
    def run_progress(self) -> int:
        """The amount of progress made by the sonication algorithm on a scale of 0-100"""
        return self._run_progress
    
    @run_progress.setter
    def run_progress(self, run_progress_value : int):
        self._run_progress = run_progress_value
        for f in self._on_run_progress_updated_callbacks:
            f(self._run_progress)

    def run(self, solution:SlicerOpenLIFUSolution):
        " Returns True when the sonication control algorithm is done"
        self.running = True
        slicer.util.infoDisplay(
            text=(
                "The run sonication button is a placeholder. Sonication control is not yet implemented."
                f" Here the solution that would have been run is {solution.solution.solution.id}."
                " The fake \"run\" will start after you close this dialog and end after three seconds."
            ),
            windowTitle="Not implemented"
        )

        def end_run():
            """Placeholder function that represents a sonication ending"""
            self.running = False
            self.sonication_run_complete = True

        self.timer = qt.QTimer()
        self.timer.timeout.connect(end_run) # Assumes that the sonication algorithm can be connected to a function
        self.timer.setSingleShot(True)
        self.timer.start(3000)

        # Dummy code to test updating run progress.
        # TODO: This value should be set based on progress updates provided by the sonication algorithm
        self.run_progress = 50

    def abort(self) -> None:
        # Assumes that the sonication control algorithm will have a callback function to abort run, 
        # that callback can be called here. 
        self.timer.stop()
        self.running = False
        self.sonication_run_complete = False

    def create_openlifu_run(self, run_parameters: Dict) -> SlicerOpenLIFURun:

        loaded_session = get_openlifu_data_parameter_node().loaded_session
        loaded_solution = get_openlifu_data_parameter_node().loaded_solution

        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
        run_id = timestamp
        if loaded_session is not None:
            session_id = loaded_session.session.session.id
            run_id = f"{session_id}_{run_id}"
        else:
            session_id = None
        
        if loaded_solution is not None: # This should never be the case. Cannot initiate a run without an approved solution
            solution_id = loaded_solution.solution.solution.id
            
        run_openlifu = openlifu_lz().plan.run.Run(
            id = run_id,
            name = f"Run_{timestamp}",
            success_flag = run_parameters["success_flag"],
            note = run_parameters["note"],
            session_id = session_id,
            solution_id = solution_id
        )

        # Add SlicerOpenLIFURun to data parameter node
        run = SlicerOpenLIFURun(run_openlifu)
        slicer.util.getModuleLogic('OpenLIFUData').set_run(run)
        
        return run


