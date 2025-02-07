import logging
import os
from typing import Annotated, Optional, Dict, List, TYPE_CHECKING
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

# TODO: somehow you need to observe the data logic so you can connect the button here to it.
from OpenLIFULib import (
    openlifu_lz,
    get_openlifu_data_parameter_node
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
        self._parameterNode: Optional[OpenLIFUProtocolConfigParameterNode] = None
        self._parameterNodeGuiTag = None
        self.focalPattern_type_to_pageName : Dict[FocalPatternType,str] = {
            FocalPatternType.SINGLE_POINT : "singlePointPage",
            FocalPatternType.WHEEL : "wheelPage",
        }


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

        # Gets the logic _instance_ of the Data module
        self.logic.dataLogic = slicer.util.getModuleLogic('OpenLIFUData')
        self.onDataParameterNodeModified()

        # === Connections and UI setup =======

        # These connections ensure that we update parameter node when scene is closed
        self.addObserver(slicer.mrmlScene, slicer.mrmlScene.StartCloseEvent, self.onSceneStartClose)
        self.addObserver(slicer.mrmlScene, slicer.mrmlScene.EndCloseEvent, self.onSceneEndClose)
        self.addObserver(get_openlifu_data_parameter_node().parameterNode, vtk.vtkCommand.ModifiedEvent, self.onDataParameterNodeModified)

        # Connect signals to trigger save state update
        trigger_unsaved_changes = lambda: self.updateWidgetSaveState(SaveState.UNSAVED_CHANGES)

        self.ui.protocolNameLineEdit.textChanged.connect(trigger_unsaved_changes)
        self.ui.protocolIdLineEdit.textChanged.connect(trigger_unsaved_changes)
        self.ui.protocolDescriptionTextEdit.textChanged.connect(trigger_unsaved_changes)
        self.ui.pulseFrequencySpinBox.valueChanged.connect(trigger_unsaved_changes)
        self.ui.pulseDurationSpinBox.valueChanged.connect(trigger_unsaved_changes)

        self.ui.wheelCenterCheckBox.stateChanged.connect(trigger_unsaved_changes)  # wheel
        self.ui.numSpokesSpinBox.valueChanged.connect(trigger_unsaved_changes)  # wheel
        self.ui.spokeRadiusSpinBox.valueChanged.connect(trigger_unsaved_changes)  # wheel

        # Connect main widget functions

        self.ui.protocolSelector.currentIndexChanged.connect(self.onProtocolSelected)
        self.ui.loadProtocolFromFileButton.clicked.connect(self.onLoadProtocolFromFileClicked)
        self.ui.createNewProtocolButton.clicked.connect(self.onNewProtocolClicked)

        self.ui.protocolEditButton.clicked.connect(self.onEditProtocolClicked)
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

        # If there is no database loaded, widget should be grayed out
        if not get_openlifu_data_parameter_node().database_is_loaded:
            self.setAllWidgetsEnabled(False)
            self.ui.saveStateLabel.setProperty("text", "You must load a database to configure protocols.")
            self.ui.saveStateLabel.setProperty("styleSheet", "color: blue; font-weight: bold; font-size: 16px; border: 3px solid blue; padding: 30px;")
            return

        # If there is a database loaded, then set up the available protocols and editing widgets

        self.setAllWidgetsEnabled(True)

        self.ui.protocolSelector.clear()
        if len(get_openlifu_data_parameter_node().loaded_protocols) == 0:
            tooltip = "Load a protocol first in order to select it for editing"
            self.ui.protocolSelector.setProperty("defaultText", "No protocols to select.")  
            self.setProtocolEditButtonEnabled(False)
        else:
            tooltip = "Select a protocol..."
            for protocol_id, protocol in get_openlifu_data_parameter_node().loaded_protocols.items():
                text = f"{protocol.protocol.name} (ID: {protocol_id})"
                self.ui.protocolSelector.addItem(text, protocol.protocol)
            self.setProtocolEditButtonEnabled(True)

        self.ui.protocolSelector.setToolTip(tooltip)

        # We update the save state because we assume reloading all the protocols will reset all changes
        self.updateWidgetSaveState(SaveState.NO_CHANGES)

    def onProtocolSelected(self):
        protocol = self.ui.protocolSelector.currentData
        if protocol is not None:
            self.updateProtocolDisplayFromProtocol(protocol)
            self.setProtocolEditorEnabled(False)
        self.updateWidgetSaveState(SaveState.NO_CHANGES)

    @display_errors
    def onNewProtocolClicked(self, checked: bool) -> None:
        """Set the widget fields with default protocol values."""
        defaults = self.logic.DEFAULTS
        unique_default_id = self.logic.generate_unique_default_id()

        self.ui.protocolNameLineEdit.setText(defaults["Name"])
        self.ui.protocolIdLineEdit.setText(unique_default_id)
        self.ui.protocolDescriptionTextEdit.setPlainText(defaults["Description"])
        self.ui.pulseFrequencySpinBox.setValue(defaults["Pulse frequency"])
        self.ui.pulseDurationSpinBox.setValue(defaults["Pulse duration"])
        self.ui.focalPatternComboBox.setCurrentText(defaults["Focal patten type"])

        protocol = self.getProtocolFromGUI()

        # Set the text of the protocolSelector to a nonexistent protocol
        text = f'{protocol.name} (ID: {protocol.id})'
        self.ui.protocolSelector.addItem(text, protocol)
        self.ui.protocolSelector.setCurrentText(text)

        self.setProtocolEditButtonEnabled(True)  # enable edit button (consistency)
        self.setProtocolEditorEnabled(True)  # enable editor

        self.updateWidgetSaveState(SaveState.UNSAVED_CHANGES)

    @display_errors
    def onEditProtocolClicked(self, checked: bool) -> None:
        self.setProtocolEditorEnabled(True)

    @display_errors
    def onSaveProtocolToDatabaseClicked(self, checked: bool) -> None:
        protocol: "openlifu.plan.Protocol" = self.getProtocolFromGUI()

        if protocol.id == "":
            slicer.util.errorDisplay("You cannot save a protocol without entering in a Protocol ID.")
            return

        if self.logic.protocol_id_exists(protocol.id):
            want_overwrite = False
            want_overwrite = slicer.util.confirmYesNoDisplay(
                text = "This protocol ID already exists in the loaded database. Do you want to overwrite it?",
                windowTitle = "Overwrite Confirmation",
            )
            if not want_overwrite:
                return

        self.logic.save_protocol_to_database(protocol)  # save to database
        self.logic.dataLogic.load_protocol_from_openlifu(protocol, replace_confirmed=True)  # load to memory
        self.updateProtocolDisplayFromProtocol(protocol)  # update widget
        self.ui.protocolSelector.setCurrentText(f"{protocol.name} (ID: {protocol.id})")

        self.updateWidgetSaveState(SaveState.SAVED_CHANGES)

    @display_errors
    def onDeleteProtocolFromDatabaseClicked(self, checked: bool) -> None:
        protocol = self.ui.protocolSelector.currentData
        # Check if the user really wants to delete
        want_delete = False
        want_delete = slicer.util.confirmYesNoDisplay(
            text = f'Are you sure you want to delete the protocol "{self.ui.protocolSelector.currentText}"?',
            windowTitle = "Protocol Delete Confirmation",
        )
        if not want_delete:
            return

        # Delete the protocol

        self.logic.delete_protocol_from_database(protocol.id)  # delete in db
        get_openlifu_data_parameter_node().loaded_protocols.pop(protocol.id)  # unload (calls onDataParameterNodeModified)

        # Notify user
        slicer.util.infoDisplay("Protocol deleted from database.")

    @display_errors
    def onLoadProtocolFromFileClicked(self, checked:bool) -> None:
        qsettings = qt.QSettings()

        filepath: str = qt.QFileDialog.getOpenFileName(
            slicer.util.mainWindow(), # parent
            'Load protocol', # title of dialog
            qsettings.value('OpenLIFU/databaseDirectory','.'), # starting dir, with default of '.'
            "Protocols (*.json);;All Files (*)", # file type filter
        )
        if filepath:
            self.logic.dataLogic.load_protocol_from_file(filepath)

    def updateWidgetSaveState(self, state: SaveState):
        if state == SaveState.NO_CHANGES:
            self.ui.saveStateLabel.setProperty("text", "")  
            self.ui.saveStateLabel.setProperty("styleSheet", "border: none;")
        elif state == SaveState.UNSAVED_CHANGES:
            self.ui.saveStateLabel.setProperty("text", "You have unsaved changes!")
            self.ui.saveStateLabel.setProperty("styleSheet", "color: red; font-weight: bold; font-size: 16px; border: 3px solid red; padding: 30px;")
        elif state == SaveState.SAVED_CHANGES:
            self.ui.saveStateLabel.setProperty("text", "Changes saved.")
            self.ui.saveStateLabel.setProperty("styleSheet", "color: green; font-size: 16px; border: 2px solid green; padding: 30px;")

    def updateProtocolDisplayFromProtocol(self, protocol: "openlifu.plan.Protocol"):
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

    def setProtocolEditorEnabled(self, enabled: bool) -> None:
        self.ui.protocolEditorSectionGroupBox.setEnabled(enabled)
        self.ui.protocolDatabaseSaveButton.setEnabled(enabled)
        self.ui.protocolDatabaseDeleteButton.setEnabled(enabled)

    def setProtocolEditButtonEnabled(self, enabled: bool) -> None:
        self.ui.protocolEditButton.setEnabled(enabled)
        if enabled == False:
            self.setProtocolEditorEnabled(False)  # depends

    def setAllWidgetsEnabled(self, enabled: bool) -> None:
        self.ui.protocolSelector.setEnabled(enabled)
        self.ui.loadProtocolFromFileButton.setEnabled(enabled)
        self.ui.createNewProtocolButton.setEnabled(enabled)
        
        self.ui.protocolEditorSectionGroupBox.setEnabled(enabled)

        self.ui.protocolDatabaseSaveButton.setEnabled(enabled)
        self.ui.protocolDatabaseDeleteButton.setEnabled(enabled)
        self.ui.protocolEditButton.setEnabled(enabled)

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
    
    DEFAULTS = {
        "Name": "New Protocol",
        "ID": "new_protocol",
        "Description": "",
        "Pulse frequency": 0.00,
        "Pulse duration": 0.00,
        "Focal patten type": "single point",
        "Focal patten options": None,
    }

    def __init__(self) -> None:
        """Called when the logic class is instantiated. Can be used for initializing member variables."""
        self.dataLogic = None
        ScriptedLoadableModuleLogic.__init__(self)

    def getParameterNode(self):
        return OpenLIFUProtocolConfigParameterNode(super().getParameterNode())  # pyright: ignore[reportCallIssue]

    def protocol_id_is_loaded(self, protocol_id: str) -> bool:
        return protocol_id in get_openlifu_data_parameter_node().loaded_protocols

    def protocol_id_exists(self, protocol_id: str) -> bool:
        return self.protocol_id_is_loaded(protocol_id) or protocol_id in self.dataLogic.db.get_protocol_ids()

    def generate_unique_default_id(self) -> str:
        i = 1
        base_name = self.DEFAULTS['ID']
        while self.protocol_id_is_loaded(name := f"{base_name}_{i}"):
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
