# Standard library imports
import asyncio
import logging
import re
import asyncio
import sys
import threading
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
    CONFIGURED = 2
    READY = 3
    RUNNING = 4

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

        self.logic.qt_signals.runProgressUpdated.connect(self.updateRunProgressBar)
        self.logic.qt_signals.finishScanning.connect(self.onRunCompleted)

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
        self.logic.cur_lifu_interface.stop_sonication()
        
        self.ui.runHardwareStatusLabel.setProperty("text", "Run Completed.")
        
        if new_sonication_run_complete_state:
            runCompleteDialog = OnRunCompletedDialog(True)
            returncode, run_parameters = runCompleteDialog.customexec_()
            if returncode:
                self.logic.create_openlifu_run(run_parameters)
        self.logic.stop()
        self.updateAllButtonsEnabled()

    @display_errors
    def onDeviceConnected(self):        
        # Even though this call explicitly tells us whether "Connected" or
        # "Disconnected", we still update from the actual hardware for the best
        # possible synchronization
        self.updateDeviceConnectedStateFromDevice()
        self.updateWidgetSolutionOnHardwareState(SolutionOnHardwareState.NOT_SENT)
        self.updateAllButtonsEnabled()

    @display_errors
    def onDeviceDisconnected(self):
        # Even though this call explicitly tells us whether "Connected" or
        # "Disconnected", we still update from the actual hardware for the best
        # possible synchronization
        self.updateDeviceConnectedStateFromDevice()
        self.updateWidgetSolutionOnHardwareState(SolutionOnHardwareState.NOT_SENT)
        self.updateAllButtonsEnabled()

    @display_errors
    def onReinitializeLIFUInterfacePushButtonClicked(self, checked=False):
        new_test_mode_state = not self.logic.cur_lifu_interface._test_mode
        logging.info("Reinitializing LIFUInterface with test_mode =", new_test_mode_state)
        
        self.logic.reinitialize_lifu_interface(test_mode=new_test_mode_state)
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
            logging.error("Exception thrown:", e)
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
            self.ui.runHardwareStatusLabel.setProperty("text", "Run in progress.")
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
class LIFUQtSignals(qt.QObject):
    runProgressUpdated = qt.Signal(float)  # Expecting pulse_train_percent as float
    finishScanning = qt.Signal(bool)  # Signal to indicate that scanning is finished

    def __init__(self, parent=None):
        super().__init__(parent)

class OpenLIFUSonicationControlLogic(ScriptedLoadableModuleLogic):


    def _pumpMonitoringLoop(self):
        self._monitor_loop.stop()
        self._monitor_loop.run_forever()

    def _run_monitor_loop(self):
        """Runs the asyncio event loop to monitor USB device status."""
        asyncio.set_event_loop(self._monitor_loop)
        try:
            self._monitor_loop.run_until_complete(
                self.cur_lifu_interface.start_monitoring(interval=1)
            )
            self._monitor_loop.run_forever()
        except Exception as e:
            logging.error(f"[LIFU] Monitor loop error: {e}")

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

        self._on_lifu_device_data_received_callbacks = []
        """List of functions to call when the LIFU interface receives data."""

        # ---- LIFU Interface Connection ----
        
        self.qt_signals = LIFUQtSignals()

        self.cur_lifu_interface = openlifu_lz().io.LIFUInterface(run_async=True, TX_test_mode=False, HV_test_mode=False)

        # Set up asyncio event loop and monitoring thread
        self._monitor_loop = asyncio.new_event_loop()
        self._monitor_thread = threading.Thread(
            target=self._run_monitor_loop,
            daemon=True
        )
        self._monitor_thread.start()

        self.monitoring_timer = qt.QTimer()
        self.monitoring_timer.setInterval(1)
        self.monitoring_timer.timeout.connect(self._pumpMonitoringLoop)
        self.monitoring_timer.start()

        # Connect signals
        self.cur_lifu_interface.signal_connect.connect(self.on_lifu_device_connected)
        self.cur_lifu_interface.signal_disconnect.connect(self.on_lifu_device_disconnected)
        self.cur_lifu_interface.signal_data_received.connect(self.on_lifu_data_received)

        self.cur_solution_on_hardware: Optional[openlifu.plan.Solution] = None
        """The active Solution object last sent to the ultrasound hardware."""

        # Set logging
        logging.getLogger("LIFUInterface").setLevel(logging.WARNING)
        logging.getLogger("UART").setLevel(logging.WARNING)
        logging.getLogger("LIFUHVController").setLevel(logging.WARNING)
        logging.getLogger("LIFUTXDevice").setLevel(logging.WARNING)

    def stop_monitoring(self):
        if self.cur_lifu_interface:
            self.cur_lifu_interface.stop_monitoring()
        if self._monitor_loop.is_running():
            self._monitor_loop.call_soon_threadsafe(self._monitor_loop.stop)

    def reinitialize_lifu_interface(self, test_mode: bool = False):
        """Cleanly shut down and reinitialize the LIFUInterface."""
        pass

    def __del__(self):
        print("OpenLIFUSonicationControlLogic.__del__ called")

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

    def call_on_lifu_device_data_received(self, f) -> None:
        """Set a function to be called whenever the LIFU device is disconnected. """
        self._on_lifu_device_data_received_callbacks.append(f)

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

    def parse_status_string(self, status_str):
        result = {
            "status": None,
            "mode": None,
            "pulse_train_percent": None,
            "pulse_percent": None,
            "temp_tx": None,
            "temp_ambient": None
        }

        try:
            # Match top-level fields
            pattern = re.compile(
                r"STATUS:(\w+),"
                r"MODE:(\w+),"
                r"PULSE_TRAIN:\[(\d+)/(\d+)\],"
                r"PULSE:\[(\d+)/(\d+)\],"
                r"TEMP_TX:([0-9.]+),"
                r"TEMP_AMBIENT:([0-9.]+)"
            )
            match = pattern.match(status_str.strip())

            if not match:
                raise ValueError("Input string format is invalid.")

            (
                status,
                mode,
                pt_current, pt_total,
                p_current, p_total,
                temp_tx,
                temp_ambient
            ) = match.groups()

            # Convert and compute percentages
            pt_current = int(pt_current)
            pt_total = int(pt_total)
            p_current = int(p_current)
            p_total = int(p_total)

            result["status"] = status
            result["mode"] = mode
            result["pulse_train_percent"] = (pt_current / pt_total * 100) if pt_total > 0 else 0
            result["pulse_percent"] = (p_current / p_total * 100) if p_total > 0 else 0
            result["temp_tx"] = float(temp_tx)
            result["temp_ambient"] = float(temp_ambient)

            return result

        except Exception as e:
            logging.error(f"Failed to parse status string: {e}")
            return result
        
    def on_lifu_device_connected(self, descriptor, port):
        # This would be useful to uncomment if debugging hardware/software integration
        logging.info(f"🔌 CONNECTED: {descriptor} on port {port}")
        
        for f in self._on_lifu_device_connected_callbacks:
            f()

        slicer.app.processEvents()

    def on_lifu_device_disconnected(self, descriptor, port):
        # This would be useful to uncomment if debugging hardware/software integration
        logging.info(f"❌ DISCONNECTED: {descriptor} from port {port}")
        
        for f in self._on_lifu_device_disconnected_callbacks:
            f()

        slicer.app.processEvents()
    
    def on_lifu_data_received(self, descriptor, message):
        """Called when the LIFUInterface receives data from the hardware.
        This is used to update the run progress and hardware status.
        """
        logging.info(f"📦 DATA [{descriptor}]: {message}")

        if descriptor == "TX":
            try:
                parsed = self.parse_status_string(message)
                progress = parsed["pulse_train_percent"]
                self.qt_signals.runProgressUpdated.emit(progress) 
                if parsed["status"] in {"RUNNING", "STOPPED"}:
                    # Update internal trigger state and notify QML
                    if parsed["status"] == "STOPPED":
                        logging.info("Trigger is stopped.")
                        self.cur_lifu_interface.set_status(openlifu_lz().io.LIFUInterfaceStatus.STATUS_FINISHED)
                        self.qt_signals.finishScanning.emit(True)  # Signal that scanning is finished 
                    else:
                        #update status
                        self.cur_lifu_interface.set_status(openlifu_lz().io.LIFUInterfaceStatus.STATUS_RUNNING)
        
            except Exception as e:
                logging.error(f"Failed to parse and update trigger state: {e}")
        

        for f in self._on_lifu_device_data_received_callbacks:
            f(descriptor, message)

        slicer.app.processEvents()
    
    def run(self):
        " Returns True when the sonication control algorithm is done"

        if get_openlifu_data_parameter_node().loaded_solution is None:
            raise RuntimeError("No solution loaded; cannot run sonication.")

        self.run_progress = 0
        self.sonication_run_complete = False

        # ---- Start the run ----
        self.running = True

        # TODO START SONICATION on HARDWARE
        self.cur_lifu_interface.start_sonication()        

    def stop(self):
        # ---- Start the run ----
        self.running = False
        
        # TODO START SONICATION on HARDWARE
        self.cur_lifu_interface.stop_sonication()    

    def abort(self) -> None:
        # Assumes that the sonication control algorithm will have a callback function to abort run, 
        # that callback can be called here. 
        
        # STOP SONICATION on HARDWARE
        self.cur_lifu_interface.stop_sonication()
        
        self.sonication_run_complete = False

        # ---- Stop the run ----
        self.running = False

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

    def get_lifu_device_connected(self) -> bool:
        tx_connected = self.cur_lifu_interface.txdevice.is_connected()
        hv_connected = self.cur_lifu_interface.hvcontroller.is_connected()
        return tx_connected and hv_connected
    