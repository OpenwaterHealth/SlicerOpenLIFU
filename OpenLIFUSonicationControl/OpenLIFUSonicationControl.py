# Standard library imports
import asyncio
import logging
import re
from datetime import datetime
from enum import Enum
from typing import Optional, Callable, Dict, List, TYPE_CHECKING

# Third-party imports
import qt
import vtk

# Slicer imports
import slicer
from slicer.ScriptedLoadableModule import *
from slicer.i18n import tr as _
from slicer.i18n import translate
from slicer.parameterNodeWrapper import parameterNodeWrapper
from slicer.util import VTKObservationMixin

# OpenLIFULib imports
from OpenLIFULib import (
    SlicerOpenLIFURun,
    get_openlifu_data_parameter_node,
    openlifu_lz,
)
from OpenLIFULib.guided_mode_util import GuidedWorkflowMixin
from OpenLIFULib.user_account_mode_util import UserAccountBanner
from OpenLIFULib.util import add_slicer_log_handler, display_errors, replace_widget

# This import is deferred at runtime using openlifu_lz, 
# but is done here for IDE and static analysis purposes
if TYPE_CHECKING:
    import openlifu

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
        self.parent.dependencies = ["OpenLIFUHome"]  # add here list of module names that this module requires
        self.parent.contributors = ["Ebrahim Ebrahim (Kitware), Sadhana Ravikumar (Kitware), Andrew Howe (Kitware) Peter Hollender (Openwater), Sam Horvath (Kitware), Brad Moore (Kitware)"]
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

class DeviceConnectedState(Enum):
    NOT_CONNECTED=0
    CONNECTED=1

class SolutionOnHardwareState(Enum):
    SUCCESSFUL_SEND=0
    FAILED_SEND=1
    NOT_SENT=2

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

class OnRunCompletedDialog(qt.QDialog):
    """ Dialog to save run """

    def __init__(self, run_complete : bool, parent="mainWindow"):
        super().__init__(slicer.util.mainWindow() if parent == "mainWindow" else parent)
        """
        Args:
            run_complete (bool): Flag indicating whether the sonication ran till completion (True) or was aborted (False) 
        """
        self.setWindowTitle("Run completed")
        self.setWindowModality(1)
        self.run_complete = run_complete
        if self.run_complete:
            self.status = "completed"
        else:
            self.status = "aborted"
        self.setup()

    def setup(self):

        self.setMinimumWidth(200)

        vBoxLayout = qt.QVBoxLayout()
        self.setLayout(vBoxLayout)

        self.label = qt.QLabel()
        self.label.setText(f"Sonication control {self.status}. Do you want to save this run? ")
        vBoxLayout.addWidget(self.label)

        self.successfulCheckBox = qt.QCheckBox('Check this box if the run was successful.')
        self.successfulCheckBox.setStyleSheet("font-weight: bold")
        vBoxLayout.addWidget(self.successfulCheckBox)

        # If the run was aborted, the success_flag is set to False
        if not self.run_complete:
            self.successfulCheckBox.setChecked(False)
            self.successfulCheckBox.setVisible(False)
            self.run_unsuccesful_label = qt.QLabel()
            self.run_unsuccesful_label.setText("Run flagged as unsuccessful")
            self.run_unsuccesful_label.setStyleSheet("font-weight: bold")
            vBoxLayout.addWidget(self.run_unsuccesful_label)

        self.label_notes = qt.QLabel()
        self.label_notes.setText("Enter additional notes to include:")
        vBoxLayout.addWidget(self.label_notes)
        self.textBox = qt.QTextEdit()
        vBoxLayout.addWidget(self.textBox)

        self.buttonBox = qt.QDialogButtonBox()
        self.buttonBox.setStandardButtons(qt.QDialogButtonBox.Save)
        vBoxLayout.addWidget(self.buttonBox)

        self.buttonBox.accepted.connect(self.validateInputs)
    
    def validateInputs(self):

        success_flag =  self.successfulCheckBox.isChecked()
        note = self.textBox.toPlainText()

        if not success_flag and not note:
            slicer.util.errorDisplay("Additional notes are required for unsuccessful or aborted runs", parent = self)
        else:
            self.accept()

    def closeEvent(self,event):

        reply = qt.QMessageBox.question(self, "Confirmation", "Closing this window will not save the sonication run. \nAre you sure you want to discard this run?", qt.QMessageBox.Yes | qt.QMessageBox.No)
        if reply == qt.QMessageBox.Yes:
            event.accept()
        else:
            event.ignore()

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


class OpenLIFUSonicationControlWidget(ScriptedLoadableModuleWidget, VTKObservationMixin, GuidedWorkflowMixin):
    """Uses ScriptedLoadableModuleWidget base class, available at:
    https://github.com/Slicer/Slicer/blob/main/Base/Python/slicer/ScriptedLoadableModule.py
    """

    def __init__(self, parent=None) -> None:
        """Called when the user opens the module the first time and the widget is initialized."""
        ScriptedLoadableModuleWidget.__init__(self, parent)
        VTKObservationMixin.__init__(self)  # needed for parameter node observation
        self.logic = None
        self._cur_device_connected_state : DeviceConnectedState = DeviceConnectedState.NOT_CONNECTED
        self._cur_solution_on_hardware_state : SolutionOnHardwareState = SolutionOnHardwareState.NOT_SENT
        self._cur_solution_id: str | None = None
        self._parameterNode = None
        self._parameterNodeGuiTag = None

    @property
    def cur_solution_on_hardware_state(self) -> SolutionOnHardwareState:
        return self._cur_solution_on_hardware_state

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

        # User account banner widget replacement. Note: the visibility is
        # initialized to false because this widget will *always* exist before
        # the login module parameter node.
        self.user_account_banner = UserAccountBanner(parent=self.ui.userAccountBannerPlaceholder.parentWidget())
        replace_widget(self.ui.userAccountBannerPlaceholder, self.user_account_banner, self.ui)
        self.user_account_banner.visible = False

        # ---- Connect loggers into Slicer ----

        add_slicer_log_handler("LIFUInterface", "LIFUInterface", use_dialogs=False)
        add_slicer_log_handler("UART", "UART", use_dialogs=False)
        add_slicer_log_handler("LIFUHVController", "LIFUHVController", use_dialogs=False)
        add_slicer_log_handler("LIFUTXDevice", "LIFUTXDevice", use_dialogs=False)

        # ---- Inject guided mode workflow controls ----

        self.inject_workflow_controls_into_placeholder()

        # ---- Connections ----

        # These connections ensure that we update parameter node when scene is closed
        self.addObserver(slicer.mrmlScene, slicer.mrmlScene.StartCloseEvent, self.onSceneStartClose)
        self.addObserver(slicer.mrmlScene, slicer.mrmlScene.EndCloseEvent, self.onSceneEndClose)

        # Buttons
        self.ui.reinitializeLIFUInterfacePushButton.clicked.connect(self.onReinitializeLIFUInterfacePushButtonClicked)
        self.ui.sendSonicationSolutionToDevicePushButton.clicked.connect(self.onSendSonicationSolutionToDevicePushButtonClicked)
        self.ui.runPushButton.clicked.connect(self.onRunClicked)
        self.ui.abortPushButton.clicked.connect(self.onAbortClicked)
        self.ui.manuallyGetDeviceStatusPushButton.clicked.connect(self.onManuallyGetDeviceStatusPushButtonClicked)
        self.logic.call_on_running_changed(self.onRunningChanged)
        self.logic.call_on_sonication_complete(self.onRunCompleted)
        self.logic.call_on_run_progress_updated(self.updateRunProgressBar)
        self.logic.call_on_run_hardware_status_updated(self.updateRunHardwareStatusLabel)
        self.logic.call_on_lifu_device_connected(self.onDeviceConnected)
        self.logic.call_on_lifu_device_disconnected(self.onDeviceDisconnected)

        # Initialize UI
        self.updateRunProgressBar()
        self.updateDeviceConnectedStateFromDevice()
        self.updateWidgetSolutionOnHardwareState(SolutionOnHardwareState.NOT_SENT)

        # Add an observer on the Data module's parameter node
        self.addObserver(
            get_openlifu_data_parameter_node().parameterNode,
            vtk.vtkCommand.ModifiedEvent,
            self.onDataParameterNodeModified
        )

        # Make sure parameter node is initialized (needed for module reload)
        self.initializeParameterNode()

        # After setup, update the module state from the data parameter node
        self.onDataParameterNodeModified()
        self.updateWorkflowControls()

        # Update the state of any buttons that may not yet have been updated
        self.updateAllButtonsEnabled()
        self.updateAllButtons()

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

    def onDataParameterNodeModified(self, caller=None, event=None) -> None:
        self.updateAllButtonsEnabled()
        if (solution_parameter_pack := get_openlifu_data_parameter_node().loaded_solution) is None:
            self._cur_solution_id = None
            self.updateWidgetSolutionOnHardwareState(SolutionOnHardwareState.NOT_SENT)
        elif solution_parameter_pack.solution.solution.id != self._cur_solution_id:
            self._cur_solution_id = solution_parameter_pack.solution.solution.id
            self.updateWidgetSolutionOnHardwareState(SolutionOnHardwareState.NOT_SENT)

        self.updateWorkflowControls()

    def updateReinitializeLIFUInterfacePushButtonEnabled(self):

        if self.logic.running:
            enabled = False
            tooltip = "Cannot reinitialize LIFUInterface while a sonication is running."
        else:
            enabled = True
            tooltip = "Reinitialize LIFUInterface, an interface to the connected hardware."

        self.ui.reinitializeLIFUInterfacePushButton.setEnabled(enabled)
        self.ui.reinitializeLIFUInterfacePushButton.setToolTip(tooltip)

    @display_errors
    def updateManuallyGetDeviceStatusPushButtonEnabled(self, checked=False):
        if not self.logic.get_lifu_device_connected():
            enabled = False
            tooltip = "The LIFU device must be connected to get its status."
        else:
            enabled = True
            tooltip = "Get the current state of the LIFU device."

        self.ui.manuallyGetDeviceStatusPushButton.setEnabled(enabled)
        self.ui.manuallyGetDeviceStatusPushButton.setToolTip(tooltip)

    def updateSendSonicationSolutionToDevicePushButtonEnabled(self):
        solution = get_openlifu_data_parameter_node().loaded_solution

        if solution is None:
            enabled = False
            tooltip = "To run a sonication, first generate and approve a solution in the sonication planning module."
        elif not self.logic.get_lifu_device_connected():
            enabled = False
            tooltip = "To send a sonication solution to the device, the LIFU device must be connected."
        elif not solution.is_approved():
            enabled = False
            tooltip = "Cannot send to device because the currently active solution is not approved. Approve it in the sonication planning module."
        elif self.logic.running:
            enabled = False
            tooltip = "Cannot send solution while a sonication is running."
        else:
            enabled = True
            tooltip = "Send the sonication solution to the connected hardware."

        self.ui.sendSonicationSolutionToDevicePushButton.setEnabled(enabled)
        self.ui.sendSonicationSolutionToDevicePushButton.setToolTip(tooltip)

    def updateRunEnabled(self):
        solution = get_openlifu_data_parameter_node().loaded_solution
        if solution is None:
            self.ui.runPushButton.enabled = False
            self.ui.runPushButton.setToolTip("To run a sonication, first generate and approve a solution in the sonication planning module.")
        elif not solution.is_approved():
            self.ui.runPushButton.enabled = False
            self.ui.runPushButton.setToolTip("Cannot run because the currently active solution is not approved. It can be approved in the sonication planning module.")
        elif not self._cur_solution_on_hardware_state == SolutionOnHardwareState.SUCCESSFUL_SEND:
            self.ui.runPushButton.enabled = False
            self.ui.runPushButton.setToolTip("To run a sonication, you must send an approved solution to the hardware device.")
        elif self.logic.running:
            self.ui.runPushButton.enabled = False
            self.ui.runPushButton.setToolTip("Currently running...")
        else:
            self.ui.runPushButton.enabled = True
            self.ui.runPushButton.setToolTip("Run sonication")

    def updateAbortEnabled(self):
        self.ui.abortPushButton.setEnabled(self.logic.running)

    def updateAllButtonsEnabled(self):
        self.updateReinitializeLIFUInterfacePushButtonEnabled()
        self.updateManuallyGetDeviceStatusPushButtonEnabled()
        self.updateSendSonicationSolutionToDevicePushButtonEnabled()
        self.updateRunEnabled()
        self.updateAbortEnabled()

    def updateReinitializeLIFUInterfacePushButton(self):
        if self.logic.cur_lifu_interface._test_mode:
            self.ui.reinitializeLIFUInterfacePushButton.setText("Reinitialize LIFUInterface not in test_mode")
        else:
            self.ui.reinitializeLIFUInterfacePushButton.setText("Reinitialize LIFUInterface in test_mode")

    def updateAllButtons(self):
        self.updateReinitializeLIFUInterfacePushButton()

    @display_errors
    def onRunCompleted(self, new_sonication_run_complete_state: bool):
        """If the soniction_run_complete variable changes from False to True, then open the RunComplete 
        dialog to determine whether the run should be saved. Saving the run creates a SlicerOpenLIFURun object and 
        writes the run to the database (only if there is an active session)."""
        if new_sonication_run_complete_state:
            runCompleteDialog = OnRunCompletedDialog(True)
            returncode, run_parameters = runCompleteDialog.customexec_()
            if returncode:
                self.logic.create_openlifu_run(run_parameters)

    @display_errors
    def onDeviceConnected(self):
        slicer.util.infoDisplay(text="Ultrasound device connected")

        # Even though this call explicitly tells us whether "Connected" or
        # "Disconnected", we still update from the actual hardware for the best
        # possible synchronization
        self.updateDeviceConnectedStateFromDevice()
        self.updateWidgetSolutionOnHardwareState(SolutionOnHardwareState.NOT_SENT)
        self.updateAllButtonsEnabled()

    @display_errors
    def onDeviceDisconnected(self):
        slicer.util.infoDisplay(text="Ultrasound device disconnected")

        # Even though this call explicitly tells us whether "Connected" or
        # "Disconnected", we still update from the actual hardware for the best
        # possible synchronization
        self.updateDeviceConnectedStateFromDevice()
        self.updateWidgetSolutionOnHardwareState(SolutionOnHardwareState.NOT_SENT)
        self.updateAllButtonsEnabled()

    @display_errors
    def onReinitializeLIFUInterfacePushButtonClicked(self, checked=False):
        new_test_mode_state = not self.logic.cur_lifu_interface._test_mode
        if new_test_mode_state:
            self.logic.reinitialize_lifu_interface(test_mode=True)
            slicer.util.infoDisplay(text="LIFUInterface reinitialized in test_mode")
        else:
            self.logic.reinitialize_lifu_interface(test_mode=False)
            slicer.util.infoDisplay(text="LIFUInterface reinitialized not in test_mode")

        self.updateDeviceConnectedStateFromDevice()
        self.updateWidgetSolutionOnHardwareState(SolutionOnHardwareState.NOT_SENT)
        self.updateAllButtons()
        self.updateAllButtonsEnabled()

    @display_errors
    def onSendSonicationSolutionToDevicePushButtonClicked(self, checked=False):

        try:
            self.logic.cur_lifu_interface.set_solution(get_openlifu_data_parameter_node().loaded_solution.solution.solution)
            if self.logic.cur_lifu_interface.get_status() != openlifu_lz().io.LIFUInterfaceStatus.STATUS_READY:
                raise RuntimeError("Interface not ready")
            self.logic.cur_solution_on_hardware = get_openlifu_data_parameter_node().loaded_solution.solution.solution
            self.updateWidgetSolutionOnHardwareState(SolutionOnHardwareState.SUCCESSFUL_SEND)
                
        except Exception as e:
            print("Exception thrown:", e)
            import traceback
            traceback.print_exc()
            self.updateWidgetSolutionOnHardwareState(SolutionOnHardwareState.FAILED_SEND, self.logic.cur_lifu_interface.get_status())

        self.updateWorkflowControls()

    def onManuallyGetDeviceStatusPushButtonClicked(self, checked=False):
        slicer.util.infoDisplay(text=f"{self.logic.cur_lifu_interface.get_status().name}", windowTitle="Device Status")

    def onRunningChanged(self, new_running_state:bool):
        self.updateReinitializeLIFUInterfacePushButtonEnabled()
        self.updateSendSonicationSolutionToDevicePushButtonEnabled()
        self.updateRunEnabled()
        self.updateAbortEnabled()
        self.updateRunHardwareStatusLabel()

    def onRunClicked(self):
        if not slicer.util.getModuleLogic('OpenLIFUData').validate_solution():
            raise RuntimeError("Invalid solution; not running sonication.")
        self.ui.runProgressBar.value = 0

        self.logic.run() 
        self.updateWorkflowControls()
        
    def onAbortClicked(self):
        self.logic.abort()
        runCompleteDialog = OnRunCompletedDialog(False)
        returncode, run_parameters = runCompleteDialog.customexec_()
        if returncode:
            run_parameters['note'] = "Run aborted." + run_parameters['note'] # Append a note that the run was aborted.
            self.logic.create_openlifu_run(run_parameters)

        self.updateWorkflowControls()

    def updateRunProgressBar(self, new_run_progress_value = None):
        """Update the run progress bar. 0% if there is no existing  run, 100% if there is an existing run."""
        self.ui.runProgressBar.maximum = 100 
        if new_run_progress_value is not None:
            self.ui.runProgressBar.value = new_run_progress_value
        else:
            if get_openlifu_data_parameter_node().loaded_run is None:
                self.ui.runProgressBar.value = 0
            else:
                self.ui.runProgressBar.value = 100

    def updateRunHardwareStatusLabel(self, new_run_hardware_status_value=None):
        """Update the label indicating the hardware status of the running hardware."""
        if self.logic.running:
            if new_run_hardware_status_value is not None:
                self.ui.runHardwareStatusLabel.setProperty("text", f"Hardware status: {new_run_hardware_status_value.name}")
        else: # not running
            self.ui.runHardwareStatusLabel.setProperty("text", "Run not in progress.")

    def updateDeviceConnectedStateFromDevice(self):
        if self.logic.get_lifu_device_connected():
            self.updateDeviceConnectedState(DeviceConnectedState.CONNECTED)
            self.status = self.logic.cur_lifu_interface.set_status(openlifu_lz().io.LIFUInterfaceStatus.STATUS_SYS_ON)
        else:
            self.updateDeviceConnectedState(DeviceConnectedState.NOT_CONNECTED)

    def updateDeviceConnectedState(self, connected_state: DeviceConnectedState):
        self._cur_solution_on_hardware_state = connected_state
        if connected_state == DeviceConnectedState.CONNECTED:
            self.ui.connectedStateLabel.setProperty("text", "🟢 LIFU Device (connected)")
        elif connected_state == DeviceConnectedState.NOT_CONNECTED:
            self.ui.connectedStateLabel.setProperty("text", "🔴 LIFU Device (not connected)")
        self.updateAllButtonsEnabled()

    def updateWidgetSolutionOnHardwareState(self, solution_state: SolutionOnHardwareState, hardware_state: "openlifu.io.LIFUInterfaceStatus | None" = None):
        self._cur_solution_on_hardware_state = solution_state
        if solution_state == SolutionOnHardwareState.SUCCESSFUL_SEND:
            self.ui.solutionStateLabel.setProperty("text", "Solution sent to device.")
            self.ui.solutionStateLabel.setProperty("styleSheet", "color: green; border: 1px solid green; padding: 5px;")
            self.updateRunEnabled()
        elif solution_state == SolutionOnHardwareState.FAILED_SEND:
            # If we have information from the hardware, display that too.
            if hardware_state is not None:
                text = f"Send to device failed! (Hardware status: {hardware_state.name})"
            else:
                text = "Send to device failed!"

            self.ui.solutionStateLabel.setProperty("text", text)
            self.ui.solutionStateLabel.setProperty("styleSheet", "color: red; border: 1px solid red; padding: 5px;")
            self.updateRunEnabled()
        elif solution_state == SolutionOnHardwareState.NOT_SENT:
            self.ui.solutionStateLabel.setProperty("text", "")  
            self.ui.solutionStateLabel.setProperty("styleSheet", "border: none;")
            self.updateRunEnabled()

    def updateWorkflowControls(self):
        session = get_openlifu_data_parameter_node().loaded_session

        if session is None:
            self.workflow_controls.can_proceed = False
            self.workflow_controls.status_text = "If you are seeing this, guided mode is being run out of order! Load a session to proceed."
        else:
            self.workflow_controls.can_proceed = True
            self.workflow_controls.status_text = "Run the sonication solution on the hardware device."

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

        self._run_hardware_status = -1
        """ The live status of the hardware device as returned during the sonication run."""

        self._on_run_hardware_status_updated_callbacks = []
        """List of functions to call when `run_hardware_status` property is changed."""

        self._on_lifu_device_connected_callbacks = []
        """List of functions to call when the LIFU interface is connected."""

        self._on_lifu_device_disconnected_callbacks = []
        """List of functions to call when the LIFU interface is disconnected."""

        # ---- LIFU Interface Connection ----

        self.cur_lifu_interface: Optional[openlifu.io.LIFUInterface] = openlifu_lz().io.LIFUInterface(run_async=False, TX_test_mode=False, HV_test_mode=False)
        """The active LIFUInterface object to the ultrasound hardware."""

        self.cur_solution_on_hardware: Optional[openlifu.plan.Solution] = None
        """The active Solution object last sent to the ultrasound hardware."""

        self.cur_lifu_interface.signal_connect.connect(self.on_lifu_device_connected)
        self.cur_lifu_interface.signal_disconnect.connect(self.on_lifu_device_disconnected)
        # TODO: self.cur_lifu_interface.signal_data_received.connect(self.update_run_progress_from_lifuinterface)

        # self.cur_lifu_interface.txdevice.enum_tx7332_devices(2) TODO: @georgevigelette : Is this necessary?

        # Set up an event loop just for start_monitoring

        self.monitoring_loop = asyncio.new_event_loop()
        asyncio.set_event_loop(self.monitoring_loop)
        def pumpMonitoringLoop():
            self.monitoring_loop.stop()
            self.monitoring_loop.run_forever()
        self.monitoring_timer = qt.QTimer()
        self.monitoring_timer.setInterval(1)
        self.monitoring_timer.timeout.connect(pumpMonitoringLoop)
        self.monitoring_timer.start()

        # Allow us to change the task between self.cur_lifu_interface.start_monitoring() and self.test_mode_start_monitoring()
        self.current_monitoring_task = self.monitoring_loop.create_task(self.cur_lifu_interface.start_monitoring())

        # Set logging
        logging.getLogger("LIFUInterface").setLevel(logging.WARNING)
        logging.getLogger("UART").setLevel(logging.WARNING)
        logging.getLogger("LIFUHVController").setLevel(logging.WARNING)
        logging.getLogger("LIFUTXDevice").setLevel(logging.WARNING)


    def __del__(self):
        self.cur_lifu_interface.stop_monitoring()

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

    def call_on_run_hardware_status_updated(self, f) -> None:
        """Set a function to be called whenever the `run_hardware_status` property is changed.
        The provided callback should accept a single int value (from a status enum) which will indicate status
        of the running openlifu harware device.
        """
        self._on_run_hardware_status_updated_callbacks.append(f)

    def call_on_lifu_device_connected(self, f) -> None:
        """Set a function to be called whenever the LIFU device is connected. """
        self._on_lifu_device_connected_callbacks.append(f)

    def call_on_lifu_device_disconnected(self, f) -> None:
        """Set a function to be called whenever the LIFU device is disconnected. """
        self._on_lifu_device_disconnected_callbacks.append(f)

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

    @property
    def run_hardware_status(self):
        """The amount of progress made by the sonication algorithm on a scale of 0-100"""
        return self._run_hardware_status
    
    @run_hardware_status.setter
    def run_hardware_status(self, run_hardware_status_value):
        self._run_hardware_status = run_hardware_status_value
        for f in self._on_run_hardware_status_updated_callbacks:
            f(self._run_hardware_status)

    def on_lifu_device_connected(self, *args, **kwargs):
        # This would be useful to uncomment if debugging hardware/software integration
        #print("on_lifu_device_connected called in sonication control module. The arguments given in were:")
        #print("Positional arguments:", args)
        #print("Keyword arguments:", kwargs)
        for f in self._on_lifu_device_connected_callbacks:
            f()

    def on_lifu_device_disconnected(self, *args, **kwargs):
        # This would be useful to uncomment if debugging hardware/software integration
        #print("on_lifu_device_disconnected called in sonication control module. The arguments given in were:")
        #print("Positional arguments:", args)
        #print("Keyword arguments:", kwargs)
        for f in self._on_lifu_device_disconnected_callbacks:
            f()
            
    def update_run_progress_from_lifuinterface(self, descriptor, message):
        """ Parses the status message from LIFUInterface. """

        if descriptor != "TX":  
            return  # Ignore non-transmitter messages

        # Parse progress
        status = re.search(r'STATUS:(\w+)', message)
        if status.group(1) == "RUNNING":
            if self.cur_lifu_interface.get_status() != openlifu_lz().io.LIFUInterfaceStatus.STATUS_RUNNING:
                self.cur_lifu_interface.set_status(openlifu_lz().io.LIFUInterfaceStatus.STATUS_RUNNING)
                logging.debug("Hardware status set to RUNNING")
        
        match = re.search(r'PULSE_TRAIN:\[(\d+)/(\d+)\]', message)
        if match:
            current = int(match.group(1))
            total = int(match.group(2))
            logging.debug(f"Current: {current}, Total: {total}")
        else:
            logging.debug("No match for progress in message:", message)
            return
        
        if current <= 0 or total <= 0:
            progress = 0
        else:
            progress = int((current/total) * 100)  # Convert to percentage
        logging.debug("Parsed progress:", progress)

        # if progress >= 100 or status == "STOPPED":
        if progress >= 100:
            self.cur_lifu_interface.set_status(openlifu_lz().io.LIFUInterfaceStatus.STATUS_FINISHED)
        self.run_progress = progress

    def get_total_runtime(self) -> float:
        """Returns the total runtime of the sonication run in seconds."""
        if get_openlifu_data_parameter_node().loaded_solution is None:
            raise RuntimeError("No solution loaded; cannot get total runtime.")

        sequence = get_openlifu_data_parameter_node().loaded_solution.solution.solution.sequence
        pulse_interval = sequence.pulse_interval
        pulse_count = sequence.pulse_count
        pulse_train_interval = sequence.pulse_train_interval
        pulse_train_count = sequence.pulse_train_count

        return ((pulse_interval * pulse_count) + (pulse_train_count * pulse_train_interval))
        
    def run(self):
        " Returns True when the sonication control algorithm is done"

        if get_openlifu_data_parameter_node().loaded_solution is None:
            raise RuntimeError("No solution loaded; cannot run sonication.")

        timer_value_ms = 500 # milliseconds
        self.run_progress = 0
        self.sonication_run_complete = False

        total_sonication_time = self.get_total_runtime()
        progress = 0
        
        # ---- Start the run ----
        self.running = True
        self.cur_lifu_interface.start_sonication()
        # TODO: self.cur_lifu_interface.txdevice.async_mode(True)  # Set the TX device to async mode
        # -----------------------

        # TODO: self.cur_lifu_interface.signal_data_received.connect(self.update_run_progress_from_lifuinterface)

        def poll():
            self.run_hardware_status = self.cur_lifu_interface.get_status()
            nonlocal total_sonication_time
            nonlocal progress 
            # In non-test mode we simulate the run bars
            if self.cur_lifu_interface._test_mode:
                self.run_progress = 0.9*self.run_progress+11 # 11 because deq converges to 99 because of integer division if adding 10
                if self.run_progress >= 100:
                    self.run_progress = 100
                self.sonication_run_complete = self.run_progress >= 100
            else:
                self.run_progress = int((progress/total_sonication_time) * 100)
                if self.run_progress >= 100:
                    self.run_progress = 100
                    self.sonication_run_complete = True
                progress += timer_value_ms / 1000  # Increment progress by the timer interval in seconds
                # TODO: self.sonication_run_complete = self.cur_lifu_interface.get_status() == openlifu_lz().io.LIFUInterfaceStatus.STATUS_FINISHED

            if self.sonication_run_complete:
                self.timer.stop()
                self.running = False
                self.cur_lifu_interface.stop_sonication()
                # TODO: self.cur_lifu_interface.txdevice.async_mode(False)

                # disconnect signals
                self.cur_lifu_interface.signal_data_received.disconnect(self.update_run_progress_from_lifuinterface)

        self.timer = qt.QTimer()
        self.timer.timeout.connect(poll)
        self.timer.start(timer_value_ms)

    def abort(self) -> None:
        # Assumes that the sonication control algorithm will have a callback function to abort run, 
        # that callback can be called here. 
        self.timer.stop()
        self.sonication_run_complete = False

        # ---- Stop the run ----
        self.running = False
        self.cur_lifu_interface.stop_sonication()
        # -----------------------

        # disconnect signals
        self.cur_lifu_interface.signal_data_received.disconnect(self.update_run_progress_from_lifuinterface)

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
        else:
            raise RuntimeError("No loaded solution -- this run should not have been possible!")
             
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

    def reinitialize_lifu_interface(self, test_mode=False):
        """ Reinitializes the LIFUInterface and switches out the monitoring task
        with the test mode version """

        # Note: in test_mode, the true start_monitoring() was never called

        self.cur_lifu_interface.stop_monitoring()

        # Cancel current task if it's running

        if self.current_monitoring_task and not self.current_monitoring_task.done():
            self.current_monitoring_task.cancel()
            try:
                self.monitoring_loop.run_until_complete(self.current_monitoring_task)  # Ensure it's fully canceled
            except asyncio.CancelledError:
                pass

        # If in test mode, we mock start_monitoring for better feedback in app

        if test_mode:
            self.cur_lifu_interface = openlifu_lz().io.LIFUInterface(run_async=False, TX_test_mode=True, HV_test_mode=True)
            self.current_monitoring_task = self.monitoring_loop.create_task(self.test_mode_start_monitoring())
        else:
            self.cur_lifu_interface = openlifu_lz().io.LIFUInterface(run_async=False, TX_test_mode=False, HV_test_mode=False)
            self.current_monitoring_task = self.monitoring_loop.create_task(self.cur_lifu_interface.start_monitoring())
            # self.updateDeviceConnectedStateFromDevice()
        # self.cur_lifu_interface.txdevice.enum_tx7332_devices(2) TODO: @georgevigelette : Is this necessary?

        # Reconnect the signals. The old cur_lifu_interface was destroyed

        self.cur_lifu_interface.signal_connect.connect(self.on_lifu_device_connected)
        self.cur_lifu_interface.signal_disconnect.connect(self.on_lifu_device_disconnected)

    def get_lifu_device_connected(self) -> bool:
        tx_connected = self.cur_lifu_interface.txdevice.is_connected()
        hv_connected = self.cur_lifu_interface.hvcontroller.is_connected()
        return tx_connected and hv_connected

    async def test_mode_start_monitoring(self, interval=1):
        """Simulate an asynchronous, repeating call (meant for USB device
        monitoring)"""
        while True:
            # This would be useful to uncomment if debugging hardware/software integration
            #print(f"{__file__}:{inspect.currentframe().f_lineno} - test_mode_start_monitoring() loop iteration to simulate LIFUInterface USB device monitoring")
            await asyncio.sleep(interval)
