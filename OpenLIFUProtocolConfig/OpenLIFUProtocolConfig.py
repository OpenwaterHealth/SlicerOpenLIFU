import logging
import os
from pathlib import Path
from typing import Annotated, Optional, Dict, List, Tuple, TYPE_CHECKING
from enum import Enum

import vtk
import qt
import ctk

import slicer
from slicer.i18n import tr as _
from slicer.i18n import translate
from slicer.ScriptedLoadableModule import *
from slicer.util import VTKObservationMixin
from slicer.parameterNodeWrapper import parameterNodeWrapper

from OpenLIFULib import (
    openlifu_lz,
    get_openlifu_data_parameter_node,
    SlicerOpenLIFUProtocol,
)

from OpenLIFULib.util import (
    display_errors,
)

if TYPE_CHECKING:
    import openlifu # This import is deferred at runtime using openlifu_lz, but it is done here for IDE and static analysis purposes
    import openlifu.db

#
# OpenLIFUProtocolConfig
#


class OpenLIFUProtocolConfig(ScriptedLoadableModule):
    """Uses ScriptedLoadableModule base class, available at:
    https://github.com/Slicer/Slicer/blob/main/Base/Python/slicer/ScriptedLoadableModule.py
    """

    def __init__(self, parent):
        ScriptedLoadableModule.__init__(self, parent)
        self.parent.title = _("OpenLIFU Protocol Configuration")  # TODO: make this more human readable by adding spaces
        self.parent.categories = [translate("qSlicerAbstractCoreModule", "OpenLIFU.OpenLIFU Modules")]
        self.parent.dependencies = []  # add here list of module names that this module requires
        self.parent.contributors = [
            "Ebrahim Ebrahim (Kitware), Andrew Howe (Kitware), Sadhana Ravikumar (Kitware), Peter Hollender (Openwater), Sam Horvath (Kitware), Brad Moore (Kitware)"
        ]
        # short description of the module and a link to online module documentation
        # _() function marks text as translatable to other languages
        self.parent.helpText = _(
            "This is the protocol configuration module of the OpenLIFU extension for focused ultrasound. "
            "More information at <a href=\"https://github.com/OpenwaterHealth/SlicerOpenLIFU\">github.com/OpenwaterHealth/SlicerOpenLIFU</a>."
        )
        # organization, grant, and thanks
        self.parent.acknowledgementText = _(
            "This is part of Openwater's OpenLIFU, an open-source "
            "hardware and software platform for Low Intensity Focused Ultrasound (LIFU) research "
            "and development."
        )

class SaveState(Enum):
    NO_CHANGES=0
    UNSAVED_CHANGES=1
    SAVED_CHANGES=2

class FocalPatternType(Enum):
    SINGLE_POINT=0
    WHEEL=1

    def to_string(self) -> str:
        if self == FocalPatternType.SINGLE_POINT:
            return "single point"
        elif self == FocalPatternType.WHEEL:
            return "wheel"
        else:
            raise ValueError(f"Unhandled enum value: {self}")

    @staticmethod
    def get_pattern_names() -> List[str]:
        return ['single point', 'wheel']

    @classmethod
    def from_string_to_enum(cls, focal_pattern: str) -> "FocalPatternType":
            if focal_pattern == "single point":
                return cls.SINGLE_POINT
            elif focal_pattern == "wheel":
                return cls.WHEEL
            else:
                raise ValueError(f"Unknown focal pattern: {focal_pattern}")
#
    @classmethod
    def from_classtype_to_enum(cls, focal_pattern_classname: str) -> "FocalPatternType":
            if focal_pattern_classname == "SinglePoint":
                return cls.SINGLE_POINT
            elif focal_pattern_classname == "Wheel":
                return cls.WHEEL
            else:
                raise ValueError(f"Unknown focal pattern class: {focal_pattern_classname}")

# OpenLIFUProtocolConfigParameterNode
#

@parameterNodeWrapper
class OpenLIFUProtocolConfigParameterNode:
    """
    The parameters needed by module.

    """

#
# OpenLIFUProtocolConfigDialogs
#

class ProtocolSelectionFromDatabaseDialog(qt.QDialog):
    """ Create new protocol selection from database dialog """

    def __init__(self, protocol_names_and_IDs : List[Tuple[str,str]], parent="mainWindow"):
        super().__init__(slicer.util.mainWindow() if parent == "mainWindow" else parent)
        """ Args:
                protocol_names_and_IDs: list of pairs containing the protocol
                names and ids (name,id) that will populate the dialog
        """

        self.setWindowTitle("Select a Protocol")
        self.setWindowModality(qt.Qt.WindowModal)
        self.resize(600, 400)

        self.protocol_names_and_IDs : List[Tuple[str,str]] = protocol_names_and_IDs
        self.selected_protocol_id : str = None

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

        # display protocols and protocol ids

        for name, id in self.protocol_names_and_IDs:
            display_text = f"{name} (ID: {id})"
            self.listWidget.addItem(display_text)


    def onItemDoubleClicked(self, item):
        self.validateInputs()

    def validateInputs(self):

        selected_idx = self.listWidget.currentRow
        if selected_idx >= 0:
            _, self.selected_protocol_id = self.protocol_names_and_IDs[selected_idx]
        self.accept()

    def get_selected_protocol_id(self) -> str:

        return self.selected_protocol_id

#
# OpenLIFUProtocolConfigWidget
#


class OpenLIFUProtocolConfigWidget(ScriptedLoadableModuleWidget, VTKObservationMixin):
    """Uses ScriptedLoadableModuleWidget base class, available at:
    https://github.com/Slicer/Slicer/blob/main/Base/Python/slicer/ScriptedLoadableModule.py
    """

    def __init__(self, parent=None) -> None:
        """Called when the user opens the module the first time and the widget is initialized."""
        ScriptedLoadableModuleWidget.__init__(self, parent)
        VTKObservationMixin.__init__(self)  # needed for parameter node observation
        self.logic: Optional[OpenLIFUProtocolConfigLogic] = None

        # Flag for keeping "saved changes" box intuitive. When saving changes,
        # the protocol is loaded/reloaded, which triggers an update to the
        # combo box. However, during this update, we should not treat the
        # re-selected protocol as no changes, because we want a special display
        # of saved changes.
        self._is_saving_changes: bool = False

        # Flag for preventing update of widget save state when programmatically
        # changing fields. When a user edits a protocol, a callback is triggered
        # to update the save state to UNSAVED_CHANGES. However, sometimes, we
        # programmatically changing those fields, and we don't want to
        # trigger a display update.
        self._is_updating_display: bool = False

        self._cur_protocol_id: str = ""  # important if WIPs change the ID
        self._cur_save_state = SaveState.NO_CHANGES
        self._parameterNode: Optional[OpenLIFUProtocolConfigParameterNode] = None
        self._parameterNodeGuiTag = None
        self.focalPattern_type_to_pageName : Dict[FocalPatternType,str] = {
            FocalPatternType.SINGLE_POINT : "singlePointPage",
            FocalPatternType.WHEEL : "wheelPage",
        }

    @property
    def cur_protocol_id(self) -> str:
        return self._cur_protocol_id

    @property
    def cur_save_state(self) -> SaveState:
        return self._cur_save_state

    def setup(self) -> None:
        """Called when the user opens the module the first time and the widget is initialized."""
        ScriptedLoadableModuleWidget.setup(self)

        # Load widget from .ui file (created by Qt Designer).
        # Additional widgets can be instantiated manually and added to self.layout.
        uiWidget = slicer.util.loadUI(self.resourcePath("UI/OpenLIFUProtocolConfig.ui"))
        self.layout.addWidget(uiWidget)
        self.ui = slicer.util.childWidgetVariables(uiWidget)

        # Set scene in MRML widgets. Make sure that in Qt designer the top-level qMRMLWidget's
        # "mrmlSceneChanged(vtkMRMLScene*)" signal in is connected to each MRML widget's.
        # "setMRMLScene(vtkMRMLScene*)" slot.
        uiWidget.setMRMLScene(slicer.mrmlScene)

        # Create logic class. Logic implements all computations that should be possible to run
        # in batch mode, without a graphical user interface.
        self.logic = OpenLIFUProtocolConfigLogic()

        # === Connections and UI setup =======

        # These connections ensure that we update parameter node when scene is closed
        self.addObserver(slicer.mrmlScene, slicer.mrmlScene.StartCloseEvent, self.onSceneStartClose)
        self.addObserver(slicer.mrmlScene, slicer.mrmlScene.EndCloseEvent, self.onSceneEndClose)
        self.addObserver(get_openlifu_data_parameter_node().parameterNode, vtk.vtkCommand.ModifiedEvent, self.onDataParameterNodeModified)

        # Connect signals to trigger save state update
        trigger_unsaved_changes = lambda: self.updateWidgetSaveState(SaveState.UNSAVED_CHANGES) if not self._is_saving_changes and not self._is_updating_display else None

        self.ui.protocolNameLineEdit.textChanged.connect(trigger_unsaved_changes)
        self.ui.protocolIdLineEdit.textChanged.connect(trigger_unsaved_changes)
        self.ui.protocolDescriptionTextEdit.textChanged.connect(trigger_unsaved_changes)
        self.ui.pulseFrequencySpinBox.valueChanged.connect(trigger_unsaved_changes)
        self.ui.pulseDurationSpinBox.valueChanged.connect(trigger_unsaved_changes)

        self.ui.wheelCenterCheckBox.stateChanged.connect(trigger_unsaved_changes)  # wheel
        self.ui.numSpokesSpinBox.valueChanged.connect(trigger_unsaved_changes)  # wheel
        self.ui.spokeRadiusSpinBox.valueChanged.connect(trigger_unsaved_changes)  # wheel

        # Connect main widget functions

        self.ui.protocolSelector.currentIndexChanged.connect(self.onProtocolSelectorIndexChanged)
        self.ui.loadProtocolFromFileButton.clicked.connect(self.onLoadProtocolFromFileClicked)
        self.ui.loadProtocolFromDatabaseButton.clicked.connect(self.onLoadProtocolFromDatabaseClicked)
        self.ui.createNewProtocolButton.clicked.connect(self.onNewProtocolClicked)

        self.ui.protocolEditRevertDiscardButton.clicked.connect(self.onEditRevertDiscardProtocolClicked)
        self.ui.protocolFileSaveButton.clicked.connect(self.onSaveProtocolToFileClicked)
        self.ui.protocolDatabaseSaveButton.clicked.connect(self.onSaveProtocolToDatabaseClicked)
        self.ui.protocolDatabaseDeleteButton.clicked.connect(self.onDeleteProtocolFromDatabaseClicked)

        # === Connections and UI setup for Focal Pattern specifically =======

        self.ui.focalPatternComboBox.currentIndexChanged.connect(
            lambda : self.ui.focalPatternOptionsStackedWidget.setCurrentWidget(
                self.ui.focalPatternOptionsStackedWidget.findChild(
                    qt.QWidget,
                    self.focalPattern_type_to_pageName[self.getCurrentlySelectedFocalPatternType()]
                )
            )
        )
        self.ui.focalPatternComboBox.addItems(FocalPatternType.get_pattern_names())

        # === Disable some of the widgets ===

        self.setProtocolEditButtonEnabled(False)
        self.setProtocolEditorEnabled(False)

        self.onDataParameterNodeModified()  # might not have queued

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

    def onDataParameterNodeModified(self, caller = None, event = None):

        if not get_openlifu_data_parameter_node().database_is_loaded:
            self.setDatabaseButtonsEnabled(False)
        else:
            self.setDatabaseButtonsEnabled(True)


        # Edits to data parameter node should not change selected protocol

        prev_protocol = self.ui.protocolSelector.currentText

        self.reloadProtocols()

        if (len(get_openlifu_data_parameter_node().loaded_protocols) + len(self.logic.new_protocol_ids)) > 1:
            self.ui.protocolSelector.setCurrentText(prev_protocol)

    def reloadProtocols(self):
        self.ui.protocolSelector.clear()
        if (len(get_openlifu_data_parameter_node().loaded_protocols) + len(self.logic.new_protocol_ids)) == 0:
            tooltip = "Load a protocol first in order to select it for editing"
            self.ui.protocolSelector.setProperty("defaultText", "No protocols to select.")  
            self.setProtocolEditButtonEnabled(False)
        else:
            tooltip = "Select among the currently loaded protocols"
            for protocol_id, protocol_w in get_openlifu_data_parameter_node().loaded_protocols.items():
                protocol_text = f"{protocol_w.protocol.name} (ID: {protocol_id})"
                if protocol_id in self.logic.cached_protocols:
                    protocol_text = "[  +  ]  " + protocol_text
                self.ui.protocolSelector.addItem(protocol_text, protocol_w.protocol)
            self.setProtocolEditButtonEnabled(True)

        for protocol_id in self.logic.new_protocol_ids:
            protocol = self.logic.cached_protocols[protocol_id]
            self.ui.protocolSelector.addItem(f"[  +  ]  {protocol.name} (ID: {protocol.id})", protocol)

        self.ui.protocolSelector.setToolTip(tooltip)

        if self._cur_protocol_id in self.logic.new_protocol_ids:
            self.setNewProtocolWidgets()

    def onProtocolSelectorIndexChanged(self):
        if self._cur_save_state == SaveState.UNSAVED_CHANGES:
            protocol_changed = self.getProtocolFromGUI()
            self.logic.cache_protocol(self._cur_protocol_id, protocol_changed)

        protocol = self.ui.protocolSelector.currentData
        if protocol is None:
            protocol = self.logic.EMPTY_PROTOCOL
            self.setProtocolEditButtonEnabled(False)

        self._cur_protocol_id = protocol.id

        if protocol.id in self.logic.cached_protocols:
            cached_protocol = self.logic.cached_protocols[protocol.id]
            self.updateProtocolDisplayFromProtocol(cached_protocol)
            self.setProtocolEditorEnabled(True)
            self.updateWidgetSaveState(SaveState.UNSAVED_CHANGES)
        else:
            self.updateProtocolDisplayFromProtocol(protocol)
            self.setProtocolEditorEnabled(False)
            if self._is_saving_changes:
                self.updateWidgetSaveState(SaveState.SAVED_CHANGES)
            else:
                self.updateWidgetSaveState(SaveState.NO_CHANGES)

        # You can't delete new protocols from db, so make sure the widgets reflect that
        if self._cur_protocol_id in self.logic.new_protocol_ids:
            self.setNewProtocolWidgets()

    @display_errors
    def onNewProtocolClicked(self, checked: bool) -> None:
        """Set the widget fields with default protocol values."""
        defaults = self.logic.NEW_DEFAULTS
        unique_default_id = self.logic.generate_unique_default_id()

        protocol = openlifu_lz().plan.Protocol(
            name = defaults["Name"],
            id = unique_default_id,
            description = defaults["Description"],
            pulse = openlifu_lz().bf.Pulse(frequency=defaults["Pulse frequency"], duration=defaults["Pulse duration"]),
            focal_pattern = openlifu_lz().bf.focal_patterns.SinglePoint()
        )

        self.updateProtocolDisplayFromProtocol(protocol)

        self._cur_protocol_id = protocol.id
        self.logic.cache_protocol(self._cur_protocol_id, protocol)
        self.logic.new_protocol_ids.add(protocol.id)

        # Set the text of the protocolSelector
        self.ui.protocolSelector.addItem(text := f'[  +  ]  {protocol.name} (ID: {protocol.id})', protocol)
        self.ui.protocolSelector.setCurrentText(text)

        self.setNewProtocolWidgets()

        self.updateWidgetSaveState(SaveState.UNSAVED_CHANGES)

    @display_errors
    def onEditRevertDiscardProtocolClicked(self, checked: bool) -> None:
        if self.ui.protocolEditRevertDiscardButton.text == "Edit Protocol":
            self.setProtocolEditorEnabled(True)
        elif self.ui.protocolEditRevertDiscardButton.text == "Discard New Protocol":
            self.logic.delete_protocol_from_cache(self._cur_protocol_id)
            self.updateWidgetSaveState(SaveState.NO_CHANGES)
            self.reloadProtocols()
        elif self.ui.protocolEditRevertDiscardButton.text == "Revert Changes":
            self.logic.delete_protocol_from_cache(self._cur_protocol_id)
            self.updateWidgetSaveState(SaveState.NO_CHANGES)
            prev_protocol = self.ui.protocolSelector.currentText
            self.reloadProtocols()
            self.ui.protocolSelector.setCurrentText(prev_protocol.lstrip("[  +  ] "))

    @display_errors
    def onSaveProtocolToFileClicked(self, checked:bool) -> None:
        initial_dir = slicer.app.defaultScenePath
        protocol: "openlifu.plan.Protocol" = self.getProtocolFromGUI()

        safe_protocol_id = "".join(c if c.isalnum() or c in (' ', '-', '_') else "_" for c in protocol.id)

        initial_file = Path(initial_dir) / f'{safe_protocol_id}.json'
        
        # Open a QFileDialog for saving a file
        filepath = qt.QFileDialog.getSaveFileName(
            slicer.util.mainWindow(),  # parent
            'Save Protocol',  # dialog title
            initial_file,  # starting file
            "Protocols (*.json);;All Files (*)"  # file type filter
        )

        if filepath:
            protocol.to_file(filepath)  # save to file
            self.updateWidgetSaveState(SaveState.SAVED_CHANGES)

            self.logic.delete_protocol_from_cache(self._cur_protocol_id)

            self._is_saving_changes = True
            self.logic.dataLogic.load_protocol_from_openlifu(protocol, replace_confirmed=True)  # load (if new) or reload (if changes) to memory
            self.reloadProtocols()
            self.ui.protocolSelector.setCurrentText(f"{protocol.name} (ID: {protocol.id})")  # details might have changed
            self._is_saving_changes = False
            self._cur_protocol_id = protocol.id  # id might have changed

            self.setProtocolEditorEnabled(False)

    @display_errors
    def onSaveProtocolToDatabaseClicked(self, checked: bool) -> None:
        protocol: "openlifu.plan.Protocol" = self.getProtocolFromGUI()

        if protocol.id == "":
            slicer.util.errorDisplay("You cannot save a protocol without entering in a Protocol ID.")
            return

        if self.logic.protocol_id_is_in_database(protocol.id):
            if not slicer.util.confirmYesNoDisplay(
                text = "This protocol ID already exists in the loaded database. Do you want to overwrite it?",
                windowTitle = "Overwrite Confirmation",
            ):
                return

        self.logic.save_protocol_to_database(protocol)  # save to database
        self.ui.protocolDatabaseDeleteButton.setEnabled(True)  # can delete now
        self.updateWidgetSaveState(SaveState.SAVED_CHANGES)

        self.logic.delete_protocol_from_cache(self._cur_protocol_id)

        self._is_saving_changes = True
        self.logic.dataLogic.load_protocol_from_openlifu(protocol, replace_confirmed=True)  # load (if new) or reload (if changes) to memory
        self.reloadProtocols()
        self.ui.protocolSelector.setCurrentText(f"{protocol.name} (ID: {protocol.id})")  # details might have changed
        self._is_saving_changes = False
        self._cur_protocol_id = protocol.id  # id might have changed

        self.setProtocolEditorEnabled(False)

    @display_errors
    def onDeleteProtocolFromDatabaseClicked(self, checked: bool) -> None:
        protocol = self.ui.protocolSelector.currentData
        # Check if the user really wants to delete
        if not slicer.util.confirmYesNoDisplay(
            text = f'Are you sure you want to delete the protocol "{self.ui.protocolSelector.currentText}"?',
            windowTitle = "Protocol Delete Confirmation",
        ):
            return

        # Delete the protocol

        self.logic.cached_protocols.pop(protocol.id, None)  # delete from cache
        self.logic.delete_protocol_from_database(protocol.id)  # delete in db
        get_openlifu_data_parameter_node().loaded_protocols.pop(protocol.id)  # unload (calls onDataParameterNodeModified)

        # Notify user
        slicer.util.infoDisplay("Protocol deleted from database.")

    @display_errors
    def onLoadProtocolFromFileClicked(self, checked:bool) -> None:
        # You could load a non-cached protocol if you edit one, don't change
        # protocols, then load the same protocol
        if self._cur_save_state == SaveState.UNSAVED_CHANGES:
            protocol_changed = self.getProtocolFromGUI()
            self.logic.cache_protocol(self._cur_protocol_id, protocol_changed)

        qsettings = qt.QSettings()

        filepath: str = qt.QFileDialog.getOpenFileName(
            slicer.util.mainWindow(), # parent
            'Load protocol', # title of dialog
            qsettings.value('OpenLIFU/databaseDirectory','.'), # starting dir, with default of '.'
            "Protocols (*.json);;All Files (*)", # file type filter
        )
        if not filepath:
            return

        protocol = openlifu_lz().Protocol.from_file(filepath)

        if not self.load_protocol_from_openlifu(protocol):
            return

        self.ui.protocolSelector.setCurrentText(f"{protocol.name} (ID: {protocol.id})")  # Update UI
        self.setProtocolEditorEnabled(False)

    @display_errors
    def onLoadProtocolFromDatabaseClicked(self, checked:bool) -> None:
        # You could load a non-cached protocol if you edit one, don't change
        # protocols, then load the same protocol
        if self._cur_save_state == SaveState.UNSAVED_CHANGES:
            protocol_changed = self.getProtocolFromGUI()
            self.logic.cache_protocol(self._cur_protocol_id, protocol_changed)

        if not get_openlifu_data_parameter_node().database_is_loaded:
            raise RuntimeError("Cannot load protocol from database because there is no database connection")

        # Open the protocol selection dialog
        protocols : "List[openlifu.plan.Protocol]" = self.logic.dataLogic.db.load_all_protocols()
        protocol_names_and_IDs = [(p.name, p.id) for p in protocols]

        dialog = ProtocolSelectionFromDatabaseDialog(protocol_names_and_IDs)
        if dialog.exec_() == qt.QDialog.Accepted:
            selected_protocol_id = dialog.get_selected_protocol_id()
            if not selected_protocol_id:
                return

            protocol = self.logic.dataLogic.db.load_protocol(selected_protocol_id)

            if not self.load_protocol_from_openlifu(protocol):
                return

            self.ui.protocolSelector.setCurrentText(f"{protocol.name} (ID: {protocol.id})")  # Update UI
            self.setProtocolEditorEnabled(False)

    def updateWidgetSaveState(self, state: SaveState):
        self._cur_save_state = state
        if state == SaveState.NO_CHANGES:
            self.ui.saveStateLabel.setProperty("text", "")  
            self.ui.saveStateLabel.setProperty("styleSheet", "border: none;")
            self.ui.protocolEditRevertDiscardButton.setText("Edit Protocol")
            self.ui.protocolEditRevertDiscardButton.setToolTip("Edit the currently selected protocol.")
        elif state == SaveState.UNSAVED_CHANGES:
            self.ui.saveStateLabel.setProperty("text", "You have unsaved changes!")
            self.ui.saveStateLabel.setProperty("styleSheet", "color: red; font-weight: bold; font-size: 16px; border: 3px solid red; padding: 30px;")
            if not self.ui.protocolSelector.currentText.startswith("[  +  ]  "):
                new_text = "[  +  ]  " + self.ui.protocolSelector.currentText
                self.ui.protocolSelector.setItemText(self.ui.protocolSelector.currentIndex, new_text)
            if self._cur_protocol_id in self.logic.new_protocol_ids:
                self.ui.protocolEditRevertDiscardButton.setText("Discard New Protocol")
                self.ui.protocolEditRevertDiscardButton.setToolTip("Revert changes in currently selected protocol.")
            else:
                self.ui.protocolEditRevertDiscardButton.setText("Revert Changes")
                self.ui.protocolEditRevertDiscardButton.setToolTip("Revert changes in currently selected protocol.")
        elif state == SaveState.SAVED_CHANGES:
            self.ui.saveStateLabel.setProperty("text", "Changes saved.")
            self.ui.saveStateLabel.setProperty("styleSheet", "color: green; font-size: 16px; border: 2px solid green; padding: 30px;")
            self.ui.protocolEditRevertDiscardButton.setText("Edit Protocol")
            self.ui.protocolEditRevertDiscardButton.setToolTip("Edit the currently selected protocol.")

    def updateProtocolDisplayFromProtocol(self, protocol: "openlifu.plan.Protocol"):
        self._is_updating_display = True

        # Set the main fields
        self.ui.protocolNameLineEdit.setText(protocol.name)
        self.ui.protocolIdLineEdit.setText(protocol.id)
        self.ui.protocolDescriptionTextEdit.setPlainText(protocol.description)
        self.ui.pulseFrequencySpinBox.setValue(protocol.pulse.frequency)
        self.ui.pulseDurationSpinBox.setValue(protocol.pulse.duration)
        
        # Deal with getting the focal pattern
        focal_pattern_classname: str = type(protocol.focal_pattern).__name__
        focal_pattern: FocalPatternType = FocalPatternType.from_classtype_to_enum(focal_pattern_classname)
        self.ui.focalPatternComboBox.setCurrentText(focal_pattern.to_string())

        if focal_pattern == FocalPatternType.WHEEL:
            self.ui.wheelCenterCheckBox.setCheckState(2 if protocol.focal_pattern.center else 0)  # wheel
            self.ui.numSpokesSpinBox.setValue(protocol.focal_pattern.num_spokes)  # wheel
            self.ui.spokeRadiusSpinBox.setValue(protocol.focal_pattern.spoke_radius)  # wheel

        self._is_updating_display = False

    def initializeParameterNode(self) -> None:
        """Ensure parameter node exists and observed."""
        # Parameter node stores all user choices in parameter values, node selections, etc.
        # so that when the scene is saved and reloaded, these settings are restored.

        self.setParameterNode(self.logic.getParameterNode())

    def setParameterNode(self, inputParameterNode: Optional[OpenLIFUProtocolConfigParameterNode]) -> None:
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

    def getCurrentlySelectedFocalPatternType(self) -> FocalPatternType:
        """Return the type of focal pattern that is currently selected in the protocol configuration."""
        return FocalPatternType.from_string_to_enum(self.ui.focalPatternComboBox.currentText)
    
    def getProtocolFromGUI(self) -> "openlifu.plan.Protocol":
        # First get the focal pattern class
        focal_pattern_type = FocalPatternType.from_string_to_enum(self.ui.focalPatternComboBox.currentText)
        focal_pattern = None
        if focal_pattern_type == FocalPatternType.SINGLE_POINT:
            focal_pattern = openlifu_lz().bf.focal_patterns.SinglePoint()
        elif focal_pattern_type == FocalPatternType.WHEEL:
            focal_pattern = openlifu_lz().bf.focal_patterns.Wheel(center=self.ui.wheelCenterCheckBox.isChecked(),
                                                                  num_spokes=self.ui.numSpokesSpinBox.value,
                                                                  spoke_radius=self.ui.spokeRadiusSpinBox.value)

        # Get the pulse class
        pulse = openlifu_lz().bf.Pulse(frequency=self.ui.pulseFrequencySpinBox.value, duration=self.ui.pulseDurationSpinBox.value)

        # Then get the protocol class and return it
        protocol = openlifu_lz().plan.Protocol(
            name = self.ui.protocolNameLineEdit.text,
            id = self.ui.protocolIdLineEdit.text,
            description = self.ui.protocolDescriptionTextEdit.toPlainText(),
            pulse = pulse,
            focal_pattern = focal_pattern
        )

        return protocol

    def setNewProtocolWidgets(self) -> None:
        self.setProtocolEditButtonEnabled(True)  # enable edit button (consistency)
        self.setProtocolEditorEnabled(True)  # enable editor
        self.ui.protocolDatabaseDeleteButton.setEnabled(False)

    def setProtocolEditorEnabled(self, enabled: bool) -> None:
        self.ui.protocolEditorSectionGroupBox.setEnabled(enabled)
        self.setAllSaveAndDeleteButtonsEnabled(enabled)
        if not get_openlifu_data_parameter_node().database_is_loaded:
            self.setDatabaseSaveAndDeleteButtonsEnabled(False)

    def setProtocolEditButtonEnabled(self, enabled: bool) -> None:
        self.ui.protocolEditRevertDiscardButton.setEnabled(enabled)
        if not enabled:
            self.setProtocolEditorEnabled(False)  # depends

    def setDatabaseSaveAndDeleteButtonsEnabled(self, enabled: bool) -> None:
        self.ui.protocolDatabaseSaveButton.setEnabled(enabled)
        self.ui.protocolDatabaseDeleteButton.setEnabled(enabled)

    def setAllSaveAndDeleteButtonsEnabled(self, enabled: bool) -> None:
        self.setDatabaseSaveAndDeleteButtonsEnabled(enabled)  # also updates tooltips

        self.ui.protocolFileSaveButton.setEnabled(enabled)
        if enabled:
            self.ui.protocolFileSaveButton.setToolTip("Save the current openlifu protocol to a file")
        else:
            self.ui.protocolFileSaveButton.setToolTip("You must be editing a protocol to perform this action")

    def setDatabaseButtonsEnabled(self, enabled: bool) -> None:
        self.ui.loadProtocolFromDatabaseButton.setEnabled(enabled)
        self.ui.protocolDatabaseSaveButton.setEnabled(enabled)
        self.ui.protocolDatabaseDeleteButton.setEnabled(enabled)
        if enabled:
            self.ui.loadProtocolFromDatabaseButton.setToolTip("Load an openlifu protocol from database")
            self.ui.protocolDatabaseSaveButton.setToolTip("Save the current openlifu protocol to the database")
            self.ui.protocolDatabaseDeleteButton.setToolTip("Delete the current openlifu protocol from database")
        else:
            tooltip = "A database must be loaded to perform this action"
            self.ui.loadProtocolFromDatabaseButton.setToolTip(tooltip)
            self.ui.protocolDatabaseSaveButton.setToolTip(tooltip)
            self.ui.protocolDatabaseDeleteButton.setToolTip(tooltip)

    def setCreateNewProtocolButtonEnabled(self, enabled: bool) -> None:
        self.ui.createNewProtocolButton.setEnabled(enabled)

    def setAllWidgetsEnabled(self, enabled: bool) -> None:
        self.ui.protocolSelector.setEnabled(enabled)
        self.ui.loadProtocolFromFileButton.setEnabled(enabled)
        self.ui.createNewProtocolButton.setEnabled(enabled)
        
        self.ui.protocolEditorSectionGroupBox.setEnabled(enabled)

        self.ui.protocolDatabaseSaveButton.setEnabled(enabled)
        self.ui.protocolDatabaseDeleteButton.setEnabled(enabled)
        self.ui.protocolEditRevertDiscardButton.setEnabled(enabled)

    def load_protocol_from_openlifu(self, protocol: "openlifu.plan.Protocol", check_cache: bool = True) -> bool:

        """
        Handles loading a protocol, checking the cache for conflicts, and updating UI state.
        """
        if check_cache:
            if not self.confirm_and_overwrite_protocol_cache(protocol):
                return False

            self.updateWidgetSaveState(SaveState.NO_CHANGES)
            self.reloadProtocols()

            replace_confirmed = True
        else:
            replace_confirmed = False

        # Load the protocol
        self.logic.dataLogic.load_protocol_from_openlifu(protocol, replace_confirmed=replace_confirmed)
        return True

    def confirm_and_overwrite_protocol_cache(self, protocol: "openlifu.plan.Protocol") -> bool:
        """
        Checks if the protocol ID exists in the cache. If so, prompts the user to confirm overwriting it.
        Returns False if the user cancels, otherwise updates the cache and returns True.
        """
        if self.logic.protocol_id_is_in_cache(protocol.id):
            if not slicer.util.confirmYesNoDisplay(
                text=f"You have unsaved changes in a protocol with the same ID. Discard and load the new one?",
                windowTitle="Discard Changes Confirmation",
            ):
                return False  # User canceled the load process

            self.logic.delete_protocol_from_cache(protocol.id)
            return True
        else:
            return True

#
# OpenLIFUProtocolConfigLogic
#


class OpenLIFUProtocolConfigLogic(ScriptedLoadableModuleLogic):
    """This class should implement all the actual
    computation done by your module.  The interface
    should be such that other python code can import
    this class and make use of the functionality without
    requiring an instance of the Widget.
    Uses ScriptedLoadableModuleLogic base class, available at:
    https://github.com/Slicer/Slicer/blob/main/Base/Python/slicer/ScriptedLoadableModule.py
    """
    
    NEW_DEFAULTS = {
        "Name": "New Protocol",
        "ID": "new_protocol",
        "Description": "",
        "Pulse frequency": 0.00,
        "Pulse duration": 0.00,
        "Focal patten type": "single point",
        "Focal patten options": None,
    }

    EMPTY_PROTOCOL = openlifu_lz().plan.Protocol(
        name = "",
        id = "",
        description = "",
        pulse = openlifu_lz().bf.Pulse(frequency=0.00, duration=0.00),
        focal_pattern = openlifu_lz().bf.focal_patterns.SinglePoint()
    )

    def __init__(self) -> None:
        """Called when the logic class is instantiated. Can be used for initializing member variables."""
        self.dataLogic = slicer.util.getModuleLogic('OpenLIFUData')

        self.cached_protocols = {}
        self.new_protocol_ids = set()

        ScriptedLoadableModuleLogic.__init__(self)

    def getParameterNode(self):
        return OpenLIFUProtocolConfigParameterNode(super().getParameterNode())  # pyright: ignore[reportCallIssue]

    def protocol_id_is_in_cache(self, protocol_id: str) -> bool:
        return protocol_id in self.cached_protocols

    def protocol_id_is_new(self, protocol_id: str) -> bool:
        return protocol_id in self.new_protocol_ids

    def protocol_id_is_loaded(self, protocol_id: str) -> bool:
        return protocol_id in get_openlifu_data_parameter_node().loaded_protocols

    def protocol_id_is_in_database(self, protocol_id: str) -> bool:
        if not get_openlifu_data_parameter_node().database_is_loaded:
            return False
        return protocol_id in self.dataLogic.db.get_protocol_ids()

    def protocol_id_exists(self, protocol_id: str) -> bool:
        return self.protocol_id_is_loaded(protocol_id) or self.protocol_id_is_in_database(protocol_id) or self.protocol_id_is_new(protocol_id) or self.protocol_id_is_in_cache(protocol_id)

    def generate_unique_default_id(self) -> str:
        i = 1
        base_name = self.NEW_DEFAULTS['ID']
        while self.protocol_id_exists(name := f"{base_name}_{i}"):
            i += 1
        return name

    def save_protocol_to_database(self, protocol: "openlifu.plan.Protocol") -> None:
        if self.dataLogic.db is None:
            raise RuntimeError("Cannot save protocol because there is no database connection")
        self.dataLogic.db.write_protocol(protocol, openlifu_lz().db.database.OnConflictOpts.OVERWRITE)

    def delete_protocol_from_database(self, protocol_id: str) -> None:
        if self.dataLogic.db is None:
            raise RuntimeError("Cannot delete protocol because there is no database connection")
        self.dataLogic.db.delete_protocol(protocol_id, openlifu_lz().db.database.OnConflictOpts.ERROR)

    def cache_protocol(self, protocol_id: str, protocol: "openlifu.plan.Protocol") -> None:
        self.cached_protocols[protocol_id] = protocol

    def delete_protocol_from_cache(self, protocol_id: str) -> None:
        self.cached_protocols.pop(protocol_id, None)  # remove from cache
        if protocol_id in self.new_protocol_ids:
            self.new_protocol_ids.discard(protocol_id)

#
# OpenLIFUProtocolConfigTest
#


class OpenLIFUProtocolConfigTest(ScriptedLoadableModuleTest):
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
