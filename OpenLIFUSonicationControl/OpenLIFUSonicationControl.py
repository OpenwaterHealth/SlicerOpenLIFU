# Standard library imports
import asyncio
import logging
import re
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
    openlifu_sdk_lz,
)
from OpenLIFULib.guided_mode_util import GuidedWorkflowMixin
from OpenLIFULib.module_layout import apply_module_layout, wire_passive_module_header
from OpenLIFULib.user_account_mode_util import UserAccountBanner
from OpenLIFULib.util import SlicerLogHandler, add_slicer_log_handler, display_errors, replace_widget


# This import is deferred at runtime using openlifu_lz, 
# but is done here for IDE and static analysis purposes
if TYPE_CHECKING:
    import openlifu
    import openlifu_sdk


def _lifu_exceptions():
    """Lazy accessor for the ``openlifu_sdk.io.exceptions`` module.

    The exceptions module defines :class:`LIFUError` and its specialized
    subclasses (e.g. :class:`LIFUHVSettleError`, :class:`LIFUSolutionError`,
    :class:`LIFUNoTriggerStatusError`, ...). Each carries a stable numeric
    ``code`` attribute and a human-readable message.
    """
    # openlifu_sdk_lz() guarantees the package is imported.
    openlifu_sdk_lz()
    import importlib
    return importlib.import_module("openlifu_sdk.io.exceptions")


def _format_lifu_error(exc: Exception) -> str:
    """Format a :class:`LIFUError` (or any exception) into a user-friendly string.

    The resulting string includes the exception class name, the numeric LIFU
    error code (when available), and the underlying message.
    """
    code = getattr(exc, "code", None)
    message = str(exc)
    # LIFUError prepends a ``[LIFU-<code>] `` tag to the message; strip it so
    # we can present the code in a more explicit way in the dialog.
    if code is not None:
        prefix = f"[LIFU-{code}] "
        if message.startswith(prefix):
            message = message[len(prefix):]
        return f"{type(exc).__name__} (LIFU error code {code}):\n{message}"
    return f"{type(exc).__name__}:\n{message}"


def _display_lifu_error(exc: Exception, action_description: str) -> None:
    """Show a Slicer error dialog describing a LIFU device exception.

    Args:
        exc: The exception that was raised by the openlifu-sdk.
        action_description: Short human-readable description of what was being
            attempted when the error occurred (e.g. "starting sonication").
    """
    logging.error("LIFU error while %s: %s", action_description, exc, exc_info=True)
    slicer.util.errorDisplay(
        f"Error while {action_description}.\n\n{_format_lifu_error(exc)}",
        windowTitle="LIFU Device Error",
    )

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
    SENDING=3
    RUN_FAILED=4

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
        logging.debug("OpenLIFUSonicationControlWidget.setup() called")
        ScriptedLoadableModuleWidget.setup(self)

        # Load widget from .ui file (created by Qt Designer).
        # Additional widgets can be instantiated manually and added to self.layout.
        uiWidget = slicer.util.loadUI(self.resourcePath("UI/OpenLIFUSonicationControl.ui"))
        self.layout.addWidget(uiWidget)
        self.ui = slicer.util.childWidgetVariables(uiWidget)

        # Restructure into shared header (read-only) + scrollable body + footer.
        self.module_header = apply_module_layout(
            uiWidget, ui_namespace=self.ui, header_read_only=True
        )

        # Set scene in MRML widgets. Make sure that in Qt designer the top-level qMRMLWidget's
        # "mrmlSceneChanged(vtkMRMLScene*)" signal in is connected to each MRML widget's.
        # "setMRMLScene(vtkMRMLScene*)" slot.
        uiWidget.setMRMLScene(slicer.mrmlScene)

        # Create logic class. Logic implements all computations that should be possible to run
        # in batch mode, without a graphical user interface.
        self.logic = OpenLIFUSonicationControlLogic()

        # User-account status is now shown by the shared header inserted
        # by ``apply_module_layout`` above; no per-module banner needed.

        # ---- Connect loggers into Slicer ----
        #
        # The openlifu_sdk and LIFUInterface package handlers are
        # registered in ``OpenLIFUSonicationControlLogic.__init__`` so
        # they are in place before LIFUInterface spawns its UART monitor
        # threads (and so they work even when other modules access this
        # Logic before our Widget.setup() runs).
        #
        # Other legacy short-name loggers, kept for any callers that still
        # emit under these names directly.
        add_slicer_log_handler("UART", "UART", use_dialogs=False)
        add_slicer_log_handler("LIFUHVController", "LIFUHVController", use_dialogs=False)
        add_slicer_log_handler("LIFUTXDevice", "LIFUTXDevice", use_dialogs=False)

        # ---- Inject guided mode workflow controls ----

        self.inject_workflow_controls_into_placeholder()

        # ---- Passive header observers ----
        wire_passive_module_header(self, self.module_header)

        # ---- Connections ----

        # These connections ensure that we update parameter node when scene is closed
        self.addObserver(slicer.mrmlScene, slicer.mrmlScene.StartCloseEvent, self.onSceneStartClose)
        self.addObserver(slicer.mrmlScene, slicer.mrmlScene.EndCloseEvent, self.onSceneEndClose)

        # Buttons
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
        self.updateVersionLabels()
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

    def cleanup(self) -> None:
        """Called when the application closes and the module widget is destroyed."""
        logging.debug("OpenLIFUSonicationControlWidget.cleanup() called")
        self.removeObservers()

    def enter(self) -> None:
        """Called each time the user opens this module."""
        logging.debug("OpenLIFUSonicationControlWidget.enter() called")
        # Make sure parameter node exists and observed
        self.initializeParameterNode()
        self.updateVersionLabels()
        self.updateWorkflowControls()

    def exit(self) -> None:
        """Called each time the user opens a different module."""
        logging.debug("OpenLIFUSonicationControlWidget.exit() called")
        # Do not react to parameter node changes (GUI will be updated when the user enters into the module)
        if self._parameterNode:
            self._parameterNode.disconnectGui(self._parameterNodeGuiTag)
            self._parameterNodeGuiTag = None

    def onSceneStartClose(self, caller, event) -> None:
        """Called just before the scene is closed."""
        logging.debug("onSceneStartClose() called")
        # Parameter node will be reset, do not use it anymore
        self.setParameterNode(None)

    def onSceneEndClose(self, caller, event) -> None:
        """Called just after the scene is closed."""
        logging.debug("onSceneEndClose() called")
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
        logging.debug("onDataParameterNodeModified() called")
        self.updateAllButtonsEnabled()
        if (solution_parameter_pack := get_openlifu_data_parameter_node().loaded_solution) is None:
            self._cur_solution_id = None
            self.updateWidgetSolutionOnHardwareState(SolutionOnHardwareState.NOT_SENT)
        elif solution_parameter_pack.solution.solution.id != self._cur_solution_id:
            self._cur_solution_id = solution_parameter_pack.solution.solution.id
            self.updateWidgetSolutionOnHardwareState(SolutionOnHardwareState.NOT_SENT)

        self.updateWorkflowControls()

    @display_errors
    def updateManuallyGetDeviceStatusPushButtonEnabled(self, checked=False):
        if self._cur_device_connected_state != DeviceConnectedState.CONNECTED:
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
        elif self._cur_device_connected_state != DeviceConnectedState.CONNECTED:
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
        self.updateManuallyGetDeviceStatusPushButtonEnabled()
        self.updateSendSonicationSolutionToDevicePushButtonEnabled()
        self.updateRunEnabled()
        self.updateAbortEnabled()

    @display_errors
    def onRunCompleted(self, new_sonication_run_complete_state: bool):
        """If the soniction_run_complete variable changes from False to True, then open the RunComplete 
        dialog to determine whether the run should be saved. Saving the run creates a SlicerOpenLIFURun object and 
        writes the run to the database (only if there is an active session)."""

        logging.debug(f" onRunCompleted() called with run_complete={new_sonication_run_complete_state}")
        self.ui.runHardwareStatusLabel.setProperty("text", "Run Completed.")
        
        if new_sonication_run_complete_state:
            runCompleteDialog = OnRunCompletedDialog(True)
            returncode, run_parameters = runCompleteDialog.customexec_()
            if returncode:
                self.logic.create_openlifu_run(run_parameters)
        LIFUError = _lifu_exceptions().LIFUError
        try:
            self.logic.stop()
        except LIFUError as e:
            _display_lifu_error(e, "stopping sonication")
        self.updateAllButtonsEnabled()

    @display_errors
    def onDeviceConnected(self):
        logging.debug("onDeviceConnected() called")
        # Even though this call explicitly tells us whether "Connected" or
        # "Disconnected", we still update from the actual hardware for the best
        # possible synchronization
        self.updateDeviceConnectedStateFromDevice()
        self.updateWidgetSolutionOnHardwareState(SolutionOnHardwareState.NOT_SENT)
        self.updateAllButtonsEnabled()
        self.updateVersionLabels()

    @display_errors
    def onDeviceDisconnected(self):
        logging.debug("onDeviceDisconnected() called")
        # Even though this call explicitly tells us whether "Connected" or
        # "Disconnected", we still update from the actual hardware for the best
        # possible synchronization
        self.updateDeviceConnectedStateFromDevice()
        self.updateWidgetSolutionOnHardwareState(SolutionOnHardwareState.NOT_SENT)
        self.updateAllButtonsEnabled()
        self.updateVersionLabels()

    @display_errors
    def onSendSonicationSolutionToDevicePushButtonClicked(self, checked=False):
        logging.debug("onSendSonicationSolutionToDevicePushButtonClicked() called")

        LIFUError = _lifu_exceptions().LIFUError
        # Reflect the in-progress state immediately and flush the event loop so
        # the user gets visual feedback while the (synchronous) device call runs.
        self.updateWidgetSolutionOnHardwareState(SolutionOnHardwareState.SENDING)
        slicer.app.processEvents()

        success = False
        lifu_error_detail: str | None = None
        try:
            self.logic.cur_lifu_interface.set_solution(get_openlifu_data_parameter_node().loaded_solution.solution.solution.to_dict())
            if self.logic.cur_lifu_interface.get_status() != openlifu_sdk_lz().LIFUInterfaceStatus.STATUS_READY:
                raise RuntimeError("Interface not ready")
            self.logic.cur_solution_on_hardware = get_openlifu_data_parameter_node().loaded_solution.solution.solution
            logging.debug("Solution successfully sent to device")
            success = True
        except LIFUError as e:
            # The openlifu-sdk raised a typed device-communication error.
            # Show a descriptive popup including the exception type and LIFU error code.
            lifu_error_detail = _format_lifu_error(e)
            _display_lifu_error(e, "sending the sonication solution to the device")
        finally:
            # Any other (non-LIFUError) exception will propagate out via @display_errors,
            # but we still want the UI state to reflect the failure on its way out.
            if success:
                self.updateWidgetSolutionOnHardwareState(SolutionOnHardwareState.SUCCESSFUL_SEND)
            else:
                self.updateWidgetSolutionOnHardwareState(
                    SolutionOnHardwareState.FAILED_SEND,
                    self.logic.cur_lifu_interface.get_status(),
                    detail=lifu_error_detail,
                )
            self.updateWorkflowControls()

    def onManuallyGetDeviceStatusPushButtonClicked(self, checked=False):
        slicer.util.infoDisplay(text=f"{self.logic.cur_lifu_interface.get_status().name}", windowTitle="Device Status")

    def onRunningChanged(self, new_running_state:bool):
        logging.debug(f" onRunningChanged() called with running={new_running_state}")
        self.updateSendSonicationSolutionToDevicePushButtonEnabled()
        self.updateRunEnabled()
        self.updateAbortEnabled()
        self.updateRunHardwareStatusLabel()

    def onRunClicked(self):
        logging.debug("onRunClicked() called")
        if not slicer.util.getModuleLogic('OpenLIFUData').validate_solution():
            raise RuntimeError("Invalid solution; not running sonication.")
        self.ui.runProgressBar.value = 0

        # Give the user immediate visual feedback while the (potentially long)
        # synchronous start_sonication() call runs. This is especially important
        # because the HV settle wait can take a few seconds, and a faulty
        # console may take even longer before raising LIFUHVSettleError.
        self.ui.runHardwareStatusLabel.setProperty("text", "⏳ Starting sonication...")
        slicer.app.processEvents()

        LIFUError = _lifu_exceptions().LIFUError
        try:
            self.logic.run()
        except LIFUError as e:
            # The openlifu-sdk raised a typed device error (e.g. LIFUHVSettleError
            # when the HV rail fails to settle on a faulty console). Surface it
            # with the exception type and LIFU error code instead of a generic
            # uncaught error popup, and reflect the failure in the solution
            # state label so the green "Solution sent" message is replaced.
            _display_lifu_error(e, "starting sonication")
            self.ui.runHardwareStatusLabel.setProperty("text", "Run not in progress.")
            self.updateWidgetSolutionOnHardwareState(
                SolutionOnHardwareState.RUN_FAILED,
                detail=_format_lifu_error(e),
            )
        else:
            # The hardware acknowledged starting; reflect that immediately so
            # the user isn't left looking at a stale "Run not in progress." label
            # (an artifact of the in-run callback ordering) until the first
            # status update arrives from the device, which can take a while.
            self.ui.runHardwareStatusLabel.setProperty("text", "Run in progress.")
            slicer.app.processEvents()
        self.updateWorkflowControls()
        
    def onAbortClicked(self):
        logging.debug("onAbortClicked() called")
        LIFUError = _lifu_exceptions().LIFUError
        try:
            self.logic.abort()
        except LIFUError as e:
            _display_lifu_error(e, "aborting sonication")
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

    def updateVersionLabels(self):
        """Populate SDK / console / TX firmware version labels when both devices are connected."""
        if self._cur_device_connected_state == DeviceConnectedState.CONNECTED:
            import importlib.metadata
            LIFUError = _lifu_exceptions().LIFUError
            try:
                sdk_ver = openlifu_sdk_lz().LIFUInterface.get_sdk_version()
            except importlib.metadata.PackageNotFoundError as e:
                logging.warning("Could not read SDK version: %s", e)
                sdk_ver = "unknown"
            self.ui.sdkVersionLabel.setText(f"SDK: {sdk_ver or 'unknown'}")

            try:
                con_ver = self.logic.cur_lifu_interface.hvcontroller.get_version()
            except LIFUError as e:
                logging.warning("Could not read console firmware version: %s", e)
                con_ver = "unknown"
            self.ui.consoleVersionLabel.setText(f"Console FW: {con_ver}")

            try:
                module_count = self.logic.cur_lifu_interface.txdevice.get_module_count()
            except LIFUError as e:
                module_count = 0
                logging.warning("Could not read TX module count: %s", e)

            modules_info = []
            display_text = ""

            try:
                for module_idx in range(module_count):
                    tx_ver = self.logic.cur_lifu_interface.txdevice.get_version(module=module_idx)
                    modules_info.append({
                        "Module": module_idx,
                        "FW": tx_ver
                    })

                display_text = "\n".join(
                    f"TX {m['Module']} FW: v{m['FW']}"
                    for m in modules_info
                ) if modules_info else "TX FW: unknown"
            except LIFUError as e:
                logging.warning("Could not read TX firmware version: %s", e)
                display_text = "TX FW: unknown"
            self.ui.txVersionLabel.setText(display_text)
        else:
            self.ui.sdkVersionLabel.setText("")
            self.ui.consoleVersionLabel.setText("")
            self.ui.txVersionLabel.setText("")

    def updateDeviceConnectedStateFromDevice(self):
        if self.logic.get_lifu_device_connected():
            self.updateDeviceConnectedState(DeviceConnectedState.CONNECTED)
        else:
            self.updateDeviceConnectedState(DeviceConnectedState.NOT_CONNECTED)

    def updateDeviceConnectedState(self, connected_state: DeviceConnectedState):
        self._cur_device_connected_state = connected_state
        if connected_state == DeviceConnectedState.CONNECTED:
            self.ui.connectedStateLabel.setProperty("text", "🟢 LIFU Device (connected)")
        elif connected_state == DeviceConnectedState.NOT_CONNECTED:
            self.ui.connectedStateLabel.setProperty("text", "🔴 LIFU Device (not connected)")
        self.updateAllButtonsEnabled()

    def updateWidgetSolutionOnHardwareState(
        self,
        solution_state: SolutionOnHardwareState,
        hardware_state: "openlifu_sdk.LIFUInterfaceStatus | None" = None,
        detail: str | None = None,
    ):
        """Update the solution-on-hardware status label.

        Args:
            solution_state: One of the :class:`SolutionOnHardwareState` values.
            hardware_state: Optional LIFUInterfaceStatus to include on FAILED_SEND.
            detail: Optional extra text to append (e.g. the formatted message of
                a :class:`LIFUError` for ``FAILED_SEND``/``RUN_FAILED`` states).
        """
        self._cur_solution_on_hardware_state = solution_state
        if solution_state == SolutionOnHardwareState.SUCCESSFUL_SEND:
            self.ui.solutionStateLabel.setProperty("text", "Solution sent to device.")
            self.ui.solutionStateLabel.setProperty("styleSheet", "color: green; border: 1px solid green; padding: 5px;")
        elif solution_state == SolutionOnHardwareState.SENDING:
            text = "⏳ Sending solution to device..."
            self.ui.solutionStateLabel.setProperty("text", text)
            # Amber/orange indicates an in-progress action.
            self.ui.solutionStateLabel.setProperty(
                "styleSheet", "color: #b36b00; border: 1px solid #b36b00; padding: 5px;"
            )
        elif solution_state == SolutionOnHardwareState.FAILED_SEND:
            # If we have information from the hardware, display that too.
            if hardware_state is not None:
                text = f"Send to device failed! (Hardware status: {hardware_state.name})"
            else:
                text = "Send to device failed!"
            if detail:
                text = f"{text}\n{detail}"

            self.ui.solutionStateLabel.setProperty("text", text)
            self.ui.solutionStateLabel.setProperty("styleSheet", "color: red; border: 1px solid red; padding: 5px;")
        elif solution_state == SolutionOnHardwareState.RUN_FAILED:
            text = "Run failed; re-send the solution to retry."
            if detail:
                text = f"{text}\n{detail}"
            self.ui.solutionStateLabel.setProperty("text", text)
            self.ui.solutionStateLabel.setProperty("styleSheet", "color: red; border: 1px solid red; padding: 5px;")
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
class _LIFUBridge(qt.QObject):
    """Thread-safe bridge from OWSignal to Qt, plus UI notification signals."""
    # Input bridge signals (OWSignal from hvcontroller/txdevice connects to these)
    signal_connected = qt.Signal(str, str)       # (descriptor, port)
    signal_disconnected = qt.Signal(str, str)    # (descriptor, port)
    signal_data_received = qt.Signal(str, str)   # (descriptor, data)
    signal_error = qt.Signal(str, int, str)      # (descriptor, code, message)

    # Output UI signals (Widget connects to these)
    runProgressUpdated = qt.Signal(float) # Expecting pulse_train_percent as float
    finishScanning = qt.Signal(bool)  # Signal to indicate that scanning is finished

class OpenLIFUSonicationControlLogic(ScriptedLoadableModuleLogic):


    def _pumpMonitoringLoop(self):
        if self._monitor_loop.is_running():
            # Harmless tickle: sends a no-op callback into the loop to keep it alive
            self._monitor_loop.call_soon_threadsafe(lambda: None)

    def _run_monitor_loop(self):
        """Runs the asyncio event loop to monitor USB device status."""
        asyncio.set_event_loop(self._monitor_loop)
        # This runs on a background daemon thread, so a broad except is used here
        # deliberately: an unhandled exception here would otherwise silently kill
        # the monitor thread. LIFU-specific errors and asyncio/OS errors are the
        # expected failure modes; anything else also gets logged.
        LIFUError = _lifu_exceptions().LIFUError
        try:
            self._monitor_loop.run_until_complete(
                self.cur_lifu_interface.start_monitoring(interval=1)
            )
            self._monitor_loop.run_forever()
        except (LIFUError, OSError, RuntimeError) as e:
            # Background asyncio thread -- route through the dedicated
            # LIFUInterface logger (propagate=False) instead of the root
            # logger to avoid cross-thread Qt parenting issues.
            self._lifu_logger.error(f"[LIFU] Monitor loop error: {e}")

    def __init__(self) -> None:
        """Called when the logic class is instantiated. Can be used for initializing member variables."""
        logging.debug("OpenLIFUSonicationControlLogic.__init__() called")
        ScriptedLoadableModuleLogic.__init__(self)

        # ---- Connect SDK loggers into Slicer ----
        #
        # This MUST happen before ``LIFUInterface(...)`` is constructed
        # below, because that spawns UART monitor threads which start
        # logging immediately. We also have to do it here in Logic.__init__
        # rather than in Widget.setup(): the Data module calls
        # ``getModuleLogic("OpenLIFUSonicationControl")`` to look up the
        # interface, which constructs Logic without ever running
        # Widget.setup().
        #
        # The openlifu_sdk submodules use ``logging.getLogger(__name__)``
        # (e.g. ``openlifu_sdk.io.uart``). We attach a single Slicer
        # handler to the package-level logger and set
        # ``propagate = False`` -- that is critical because SDK warnings
        # are emitted from background threads (UART monitor / reader),
        # and if they propagate up to the root logger, whichever handler
        # is attached there (Slicer's default console handler) will run
        # on the background thread and may touch Qt -- producing
        # "QObject::setParent: Cannot set parent, new parent is in a
        # different thread" warnings. Our SlicerLogHandler short-circuits
        # cleanly on non-main-thread records.
        #
        # We deliberately do NOT call ``setLevel`` on the SDK logger;
        # the SDK is responsible for its own verbosity (currently it
        # defaults to WARNING, which is what we want). Connect /
        # disconnect / error events are surfaced as INFO via the
        # dedicated "LIFUInterface" logger below, driven from the SDK's
        # OWSignal callbacks.
        add_slicer_log_handler("openlifu_sdk", "openlifu_sdk", use_dialogs=False)
        logging.getLogger("openlifu_sdk").propagate = False

        # Our own "LIFUInterface" logger: routed through Slicer (no
        # dialogs) and pinned at INFO so connect / disconnect events
        # emitted from on_lifu_device_connected / on_lifu_device_disconnected
        # below show up in the terminal regardless of the root level.
        # SlicerLogHandler routes INFO records to the status bar / error
        # log model only -- it does NOT write to stdout. Add a plain
        # StreamHandler so these messages also show up in Slicer's
        # Python Console (which captures sys.stdout).
        add_slicer_log_handler("LIFUInterface", "LIFUInterface", use_dialogs=False)
        lifu_logger = logging.getLogger("LIFUInterface")
        lifu_logger.setLevel(logging.INFO)
        lifu_logger.propagate = False
        if not any(isinstance(h, logging.StreamHandler) and not isinstance(h, SlicerLogHandler)
                   for h in lifu_logger.handlers):
            stream_handler = logging.StreamHandler(sys.stdout)
            stream_handler.setLevel(logging.INFO)
            stream_handler.setFormatter(logging.Formatter("[LIFUInterface] %(message)s"))
            lifu_logger.addHandler(stream_handler)
        self._lifu_logger = lifu_logger

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

        self._create_lifu_interface_bridge()
        self.cur_lifu_interface = openlifu_sdk_lz().LIFUInterface(run_async=True)
        # Connect signals before starting the monitor thread to avoid missing early events
        self._connect_owsignals()

        # Set up asyncio event loop and monitoring thread
        self._monitor_loop = asyncio.new_event_loop()
        self._monitor_thread = threading.Thread(
            target=self._run_monitor_loop,
            daemon=True
        )
        self._monitor_thread.start()

        self.monitoring_timer = qt.QTimer()
        self.monitoring_timer.setInterval(100)
        self.monitoring_timer.timeout.connect(self._pumpMonitoringLoop)
        self.monitoring_timer.start()

        self.cur_solution_on_hardware: Optional[openlifu.plan.Solution] = None
        """The active Solution object last sent to the ultrasound hardware."""

        # Note: we deliberately do NOT clobber the "LIFUInterface" /
        # "UART" / "LIFUHVController" / "LIFUTXDevice" logger levels here
        # any more. The previous code pinned them all to ERROR, which
        # silently swallowed the connect / disconnect INFO messages we
        # set up at the top of this method. SDK verbosity is now managed
        # by the SDK itself (defaults to WARNING for the openlifu_sdk
        # package logger we route through ``add_slicer_log_handler``);
        # the legacy short-name loggers above stay at their inherited
        # level so anything emitted under those names still surfaces.

    def _create_lifu_interface_bridge(self):
        """Create the bridge QObject and wire its output signals to handlers. Call once from __init__."""
        self.qt_signals = _LIFUBridge()
        self.qt_signals.signal_connected.connect(self.on_lifu_device_connected)
        self.qt_signals.signal_disconnected.connect(self.on_lifu_device_disconnected)
        self.qt_signals.signal_data_received.connect(self.on_lifu_data_received)

    def _connect_owsignals(self):
        """Wire the current interface's OWSignals into the bridge. Call from __init__ and after swapping interfaces."""
        for device in (self.cur_lifu_interface.hvcontroller, self.cur_lifu_interface.txdevice):
            device.signal_connected.connect(self.qt_signals.signal_connected.emit)
            device.signal_disconnected.connect(self.qt_signals.signal_disconnected.emit)
            device.signal_data_received.connect(self.qt_signals.signal_data_received.emit)
            device.signal_error.connect(self.qt_signals.signal_error.emit)

    def stop_monitoring(self):
        if self.cur_lifu_interface:
            self.cur_lifu_interface.stop_monitoring()

        if hasattr(self, "_monitor_loop") and self._monitor_loop:
            if self._monitor_loop.is_running():
                self._monitor_loop.call_soon_threadsafe(self._monitor_loop.stop)

        if hasattr(self, "_monitor_thread") and self._monitor_thread:
            if self._monitor_thread.is_alive():
                self._monitor_thread.join(timeout=2)

        if hasattr(self, "_monitor_loop") and self._monitor_loop:
            try:
                self._monitor_loop.close()
            except RuntimeError as e:
                # asyncio raises RuntimeError if the loop is still running when
                # close() is called; the call_soon_threadsafe(stop) above is
                # best-effort and may race, so this is the realistic failure.
                logging.warning("Error closing monitor loop: %s", e)

    @property
    def is_simulated(self) -> bool:
        """True when ``cur_lifu_interface`` is a SimulatedLIFUInterface."""
        return bool(getattr(self.cur_lifu_interface, "is_simulated", False))

    def connect_simulated_interface(self) -> None:
        """Swap in a :class:`SimulatedLIFUInterface` and report it as connected.

        Tears down the real :class:`LIFUInterface`, replaces
        ``cur_lifu_interface`` with an in-memory simulator from
        ``openlifu_sdk.ui.simulated_interface``, rewires the bridge
        signals, and synchronously emits ``signal_connected`` for both
        the HV controller and the TX device so every observer flips to
        the connected state. The simulated interface has no USB to
        poll, so we deliberately do NOT restart the asyncio monitor
        thread.
        """
        if self.is_simulated:
            logging.debug("connect_simulated_interface(): already simulated; ignoring")
            return

        LIFUError = _lifu_exceptions().LIFUError
        try:
            self.monitoring_timer.stop()
            self.stop_monitoring()
            if self.cur_lifu_interface is not None:
                self.cur_lifu_interface.close()
        except (LIFUError, RuntimeError, OSError) as e:
            logging.warning("[LIFU] Error tearing down real interface before sim swap: %s", e)

        # Lazy import: keeps PyQt6 (which the SDK's ui submodule prefers)
        # out of Slicer's import path until the user actually opts in to
        # the simulator. The simulated_interface module also accepts
        # Slicer's PythonQt-based ``qt`` module as a fallback Qt backend.
        import importlib
        sim_mod = importlib.import_module("openlifu_sdk.ui.simulated_interface")
        self.cur_lifu_interface = sim_mod.SimulatedLIFUInterface(num_modules=1)
        self._connect_owsignals()

        # Emit the connected signals synchronously (we are on the GUI
        # thread). The simulated interface's ``start_monitoring`` would
        # do this on a QTimer.singleShot tick, but we don't run it
        # because we have no USB hot-plug to monitor.
        self.cur_lifu_interface.hvcontroller.emit_connected()
        self.cur_lifu_interface.txdevice.emit_connected()
        logging.info("[LIFU] Connected simulated LIFUInterface")

    def disconnect_simulated_interface(self) -> None:
        """Tear down the simulator and restore a real (probably unconnected)
        :class:`LIFUInterface` plus its asyncio monitor loop.
        """
        if not self.is_simulated:
            logging.debug("disconnect_simulated_interface(): not simulated; ignoring")
            return

        # Emit disconnect signals first so listeners flip state before
        # the simulator is torn down.
        try:
            self.cur_lifu_interface.txdevice.emit_disconnected()
        except Exception as e:  # noqa: BLE001
            logging.debug("Error emitting sim TX disconnect: %s", e)
        try:
            self.cur_lifu_interface.hvcontroller.emit_disconnected()
        except Exception as e:  # noqa: BLE001
            logging.debug("Error emitting sim HV disconnect: %s", e)
        try:
            self.cur_lifu_interface.close()
        except Exception as e:  # noqa: BLE001
            logging.debug("Error closing simulated interface: %s", e)

        # Recreate a real LIFUInterface and resume USB monitoring.
        self.cur_lifu_interface = openlifu_sdk_lz().LIFUInterface(
            run_async=True,
        )
        self._connect_owsignals()

        self._monitor_loop = asyncio.new_event_loop()
        self._monitor_thread = threading.Thread(
            target=self._run_monitor_loop,
            daemon=True,
        )
        self._monitor_thread.start()
        self.monitoring_timer.start()
        logging.info("[LIFU] Restored real LIFUInterface after simulator")

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
            # Try pattern WITH PULSE field
            pattern_with_pulse = re.compile(
                r"STATUS:(\w+),"
                r"MODE:(\w+),"
                r"PULSE_TRAIN:\[(\d+)/(\d+)\],"
                r"PULSE:\[(\d+)/(\d+)\],"
                r"TEMP_TX:([0-9.]+),"
                r"TEMP_AMBIENT:([0-9.]+)"
            )
            match = pattern_with_pulse.match(status_str.strip())

            if match:
                (
                    status,
                    mode,
                    pt_current, pt_total,
                    p_current, p_total,
                    temp_tx,
                    temp_ambient
                ) = match.groups()

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

            else:
                # Try pattern WITHOUT PULSE field
                pattern_without_pulse = re.compile(
                    r"STATUS:(\w+),"
                    r"MODE:(\w+),"
                    r"PULSE_TRAIN:\[(\d+)/(\d+)\],"
                    r"TEMP_TX:([0-9.]+),"
                    r"TEMP_AMBIENT:([0-9.]+)"
                )
                match = pattern_without_pulse.match(status_str.strip())

                if not match:
                    raise ValueError("Input string format is invalid.")

                (
                    status,
                    mode,
                    pt_current, pt_total,
                    temp_tx,
                    temp_ambient
                ) = match.groups()

                pt_current = int(pt_current)
                pt_total = int(pt_total)

                result["status"] = status
                result["mode"] = mode
                result["pulse_train_percent"] = (pt_current / pt_total * 100) if pt_total > 0 else 0
                result["pulse_percent"] = None
                result["temp_tx"] = float(temp_tx)
                result["temp_ambient"] = float(temp_ambient)

            return result

        except (ValueError, AttributeError, TypeError, ZeroDivisionError) as e:
            self._lifu_logger.error(f"Failed to parse status string: {e}")
            return result
        
    def _dispatch_device_connected(self):
        for f in self._on_lifu_device_connected_callbacks:
            f()

    def _dispatch_device_disconnected(self):
        for f in self._on_lifu_device_disconnected_callbacks:
            f()

    def _dispatch_data_received(self, descriptor, message):
        for f in self._on_lifu_device_data_received_callbacks:
            f(descriptor, message)

    def on_lifu_device_connected(self, descriptor, port):
        # Use the dedicated "LIFUInterface" logger (set up in __init__
        # at INFO with a Slicer handler and propagate=False) so this
        # message reaches the terminal regardless of the root level,
        # without crossing thread boundaries to root handlers.
        self._lifu_logger.info(f"CONNECTED: {descriptor} on port {port}")
        self._dispatch_device_connected()

    def on_lifu_device_disconnected(self, descriptor, port):
        self._lifu_logger.info(f"DISCONNECTED: {descriptor} from port {port}")
        self._dispatch_device_disconnected()
    
    def on_lifu_data_received(self, descriptor, message):
        """Called when the LIFUInterface receives data from the hardware.
        This is used to update the run progress and hardware status.
        """
        # OWSignal callback: runs on a background UART thread. Use the
        # dedicated LIFUInterface logger (propagate=False) instead of the
        # root logger to keep these high-frequency records off Slicer's
        # root handler, which feeds Qt-backed sinks and risks cross-
        # thread parenting warnings.
        self._lifu_logger.info(f"DATA [{descriptor}]: {message}")

        if descriptor == "TX":
            LIFUError = _lifu_exceptions().LIFUError
            try:
                parsed = self.parse_status_string(message)
                progress = parsed["pulse_train_percent"]
                self.qt_signals.runProgressUpdated.emit(progress)
                if parsed["status"] in {"RUNNING", "STOPPED"}:
                    # Update internal trigger state and notify QML
                    if parsed["status"] == "STOPPED":
                        self._lifu_logger.info("Trigger is stopped.")
                        self.cur_lifu_interface.set_status(openlifu_sdk_lz().LIFUInterfaceStatus.STATUS_FINISHED)
                        self.qt_signals.finishScanning.emit(True)  # Signal that scanning is finished
                    else:
                        #update status
                        self.cur_lifu_interface.set_status(openlifu_sdk_lz().LIFUInterfaceStatus.STATUS_RUNNING)

            except (LIFUError, KeyError, TypeError) as e:
                self._lifu_logger.error(f"Failed to parse and update trigger state: {e}")


        self._dispatch_data_received(descriptor, message)
    
    def run(self):
        " Returns True when the sonication control algorithm is done"
        logging.debug("Logic.run() called")

        if get_openlifu_data_parameter_node().loaded_solution is None:
            raise RuntimeError("No solution loaded; cannot run sonication.")

        self.run_progress = 0
        self.sonication_run_complete = False

        # ---- Start the run ----
        self.running = True

        started = False
        try:
            self.cur_lifu_interface.start_sonication()
            started = True
        finally:
            # If the hardware refused to start (e.g. LIFUHVSettleError when the
            # HV rail does not settle in time), roll the running state back so
            # that the UI returns to a consistent "not running" state. The
            # exception itself is allowed to propagate to the caller, which is
            # responsible for surfacing it (typically via _display_lifu_error).
            if not started:
                self.running = False

    def stop(self):
        logging.debug("Logic.stop() called")
        # ---- Start the run ----
        self.running = False
        
        # TODO START SONICATION on HARDWARE
        self.cur_lifu_interface.stop_sonication()    

    def abort(self) -> None:
        logging.debug("Logic.abort() called")
        # Assumes that the sonication control algorithm will have a callback function to abort run, 
        # that callback can be called here. 
        
        # STOP SONICATION on HARDWARE
        self.cur_lifu_interface.stop_sonication()
        
        self.sonication_run_complete = False

        # ---- Stop the run ----
        self.running = False

    def create_openlifu_run(self, run_parameters: Dict) -> SlicerOpenLIFURun:
        logging.debug(f" create_openlifu_run() called with success_flag={run_parameters.get('success_flag')}")

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
        logging.debug(f" create_openlifu_run() created run with id={run_id}")
        slicer.util.getModuleLogic('OpenLIFUData').set_run(run)
        
        return run

    def get_lifu_device_connected(self) -> bool:
        tx_connected = self.cur_lifu_interface.txdevice.is_connected()
        hv_connected = self.cur_lifu_interface.hvcontroller.is_connected()
        logging.debug(f" get_lifu_device_connected(): tx={tx_connected}, hv={hv_connected}")
        return tx_connected and hv_connected
    

#
# OpenLIFUSonicationControlTest
#

class OpenLIFUSonicationControlTest(ScriptedLoadableModuleTest):
    """
    This is the test case for your scripted module.
    Uses ScriptedLoadableModuleTest base class, available at:
    https://github.com/Slicer/Slicer/blob/main/Base/Python/slicer/ScriptedLoadableModule.py
    """

    def _workflow_sonication_control(self):
        
        slicer.util.selectModule("OpenLIFUSonicationControl")
        sc_widget = slicer.modules.OpenLIFUSonicationControlWidget
        sc_logic = sc_widget.logic 

        test_run_parameters = {
            'success_flag': False,
            'note': 'example notes for testing',
        }

        # Create a run
        sc_logic.create_openlifu_run(test_run_parameters)
        assert get_openlifu_data_parameter_node().loaded_run is not None
