# Standard library imports
import inspect
import logging
import os
from dataclasses import fields, is_dataclass
from enum import Enum
from pathlib import Path
from typing import (
    Annotated,
    Any,
    List,
    Optional,
    Tuple,
    Type,
    TYPE_CHECKING,
    get_args,
    get_origin,
)

# Third-party imports
import ctk
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
    SlicerOpenLIFUProtocol,
    get_cur_db,
    get_openlifu_data_parameter_node,
    openlifu_lz,
)
from OpenLIFULib.user_account_mode_util import UserAccountBanner
from OpenLIFULib.util import (
    display_errors,
    get_hints,
    replace_widget,
)

# These imports are deferred at runtime using openlifu_lz, 
# but are done here for IDE and static analysis purposes
if TYPE_CHECKING:
    import openlifu
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

class DefaultProtocolValues(Enum):
    NAME = ""
    ID = ""
    DESCRIPTION = ""

class DefaultNewProtocolValues(Enum):
    NAME = "New Protocol"
    ID = "new_protocol"
    DESCRIPTION = ""

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

        # === Instantiation of Placeholder Widgets ====

        # User account banner widget replacement. Note: the visibility is
        # initialized to false because this widget will *always* exist before
        # the login module parameter node.
        self.user_account_banner = UserAccountBanner(parent=self.ui.userAccountBannerPlaceholder.parentWidget())
        replace_widget(self.ui.userAccountBannerPlaceholder, self.user_account_banner, self.ui)
        self.user_account_banner.visible = False

        self.pulse_definition_widget = OpenLIFUAbstractDataclassDefinitionFormWidget(cls=openlifu_lz().bf.Pulse, parent=self.ui.pulseDefinitionWidgetPlaceholder.parentWidget(), collapsible_title="Parameters for Pulse")
        replace_widget(self.ui.pulseDefinitionWidgetPlaceholder, self.pulse_definition_widget, self.ui)

        self.sequence_definition_widget = OpenLIFUAbstractDataclassDefinitionFormWidget(cls=openlifu_lz().bf.Sequence, parent=self.ui.sequenceDefinitionWidgetPlaceholder.parentWidget(), collapsible_title="Parameters for Sequence")
        replace_widget(self.ui.sequenceDefinitionWidgetPlaceholder, self.sequence_definition_widget, self.ui)

        self.abstract_focal_pattern_definition_widget = OpenLIFUAbstractMultipleABCDefinitionFormWidget([openlifu_lz().bf.Wheel, openlifu_lz().bf.SinglePoint], is_collapsible=False, collapsible_title="Focal Pattern", custom_abc_title="Focal Pattern")
        replace_widget(self.ui.abstractFocalPatternDefinitionWidgetPlaceholder, self.abstract_focal_pattern_definition_widget, self.ui)

        self.sim_setup_definition_widget = OpenLIFUAbstractDataclassDefinitionFormWidget(cls=openlifu_lz().sim.SimSetup, parent=self.ui.simSetupDefinitionWidgetPlaceholder.parentWidget(), collapsible_title="Sim Setup")
        replace_widget(self.ui.simSetupDefinitionWidgetPlaceholder, self.sim_setup_definition_widget, self.ui)

        self.abstract_delay_method_definition_widget = OpenLIFUAbstractMultipleABCDefinitionFormWidget([openlifu_lz().bf.delay_methods.Direct], is_collapsible=False, collapsible_title="Delay Method", custom_abc_title="Delay Method")
        replace_widget(self.ui.abstractDelayMethodDefinitionWidgetPlaceholder, self.abstract_delay_method_definition_widget, self.ui)

        self.abstract_apodization_method_definition_widget = OpenLIFUAbstractMultipleABCDefinitionFormWidget([openlifu_lz().bf.apod_methods.MaxAngle, openlifu_lz().bf.apod_methods.PiecewiseLinear, openlifu_lz().bf.apod_methods.Uniform], is_collapsible=False, collapsible_title="Apodization Method", custom_abc_title="Apodization Method")
        replace_widget(self.ui.abstractApodizationMethodDefinitionWidgetPlaceholder, self.abstract_apodization_method_definition_widget, self.ui)

        self.segmentation_method_definition_widget = OpenLIFUAbstractDataclassDefinitionFormWidget(cls=openlifu_lz().seg.SegmentationMethod, parent=self.ui.segmentationMethodDefinitionWidgetPlaceholder.parentWidget(), collapsible_title="Segmentation Method")
        replace_widget(self.ui.segmentationMethodDefinitionWidgetPlaceholder, self.segmentation_method_definition_widget, self.ui)

        self.parameter_constraints_widget = DictTableWidget(key_name="Parameter", val_name="Constraint")
        replace_widget(self.ui.parameterConstraintsWidgetPlaceholder, self.parameter_constraints_widget, self.ui)

        self.target_constraints_widget = ListTableWidget(object_name="Target Constraint", object_type=openlifu_lz().plan.TargetConstraints)
        replace_widget(self.ui.targetConstraintsWidgetPlaceholder, self.target_constraints_widget, self.ui)

        self.solution_analysis_options_definition_widget = OpenLIFUAbstractDataclassDefinitionFormWidget(cls=openlifu_lz().plan.SolutionAnalysisOptions, parent=self.ui.solutionAnalysisOptionsDefinitionWidgetPlaceholder.parentWidget(), collapsible_title="Solution Analysis Options")
        replace_widget(self.ui.solutionAnalysisOptionsDefinitionWidgetPlaceholder, self.solution_analysis_options_definition_widget, self.ui)

        self.virtual_fit_options_definition_widget = OpenLIFUAbstractDataclassDefinitionFormWidget(cls=openlifu_lz().VirtualFitOptions, parent=self.ui.virtualFitOptionsDefinitionWidgetPlaceholder.parentWidget(), collapsible_title="Virtual Fit Options")
        replace_widget(self.ui.virtualFitOptionsDefinitionWidgetPlaceholder, self.virtual_fit_options_definition_widget, self.ui)

        # === Connections and UI setup =======

        # These connections ensure that we update parameter node when scene is closed
        self.addObserver(slicer.mrmlScene, slicer.mrmlScene.StartCloseEvent, self.onSceneStartClose)
        self.addObserver(slicer.mrmlScene, slicer.mrmlScene.EndCloseEvent, self.onSceneEndClose)

        # Connect to the database logic for updates related to database
        slicer.util.getModuleLogic("OpenLIFUDatabase").call_on_db_changed(self.onDatabaseChanged)

        # Watch the data parameter node for loaded_protocol-related changes
        self.addObserver(get_openlifu_data_parameter_node().parameterNode, vtk.vtkCommand.ModifiedEvent, self.onDataParameterNodeModified)

        # Go to data module
        self.ui.returnToSubjectSessionManagementPushButton.clicked.connect(lambda : slicer.util.selectModule("OpenLIFUData"))

        # Connect signals to trigger save state update
        trigger_unsaved_changes = lambda: self.updateWidgetSaveState(SaveState.UNSAVED_CHANGES) if not self._is_saving_changes and not self._is_updating_display else None

        self.ui.protocolNameLineEdit.textChanged.connect(trigger_unsaved_changes)
        self.ui.protocolIdLineEdit.textChanged.connect(trigger_unsaved_changes)
        self.ui.protocolDescriptionTextEdit.textChanged.connect(trigger_unsaved_changes)

        self.pulse_definition_widget.add_value_changed_signals(trigger_unsaved_changes)
        self.sequence_definition_widget.add_value_changed_signals(trigger_unsaved_changes)
        self.abstract_focal_pattern_definition_widget.add_value_changed_signals(trigger_unsaved_changes)
        self.sim_setup_definition_widget.add_value_changed_signals(trigger_unsaved_changes)
        self.abstract_delay_method_definition_widget.add_value_changed_signals(trigger_unsaved_changes)
        self.abstract_apodization_method_definition_widget.add_value_changed_signals(trigger_unsaved_changes)
        self.segmentation_method_definition_widget.add_value_changed_signals(trigger_unsaved_changes)
        self.parameter_constraints_widget.table.itemChanged.connect(lambda *_: trigger_unsaved_changes())
        self.target_constraints_widget.table.itemChanged.connect(lambda *_: trigger_unsaved_changes())
        self.solution_analysis_options_definition_widget.add_value_changed_signals(trigger_unsaved_changes)
        self.virtual_fit_options_definition_widget.add_value_changed_signals(trigger_unsaved_changes)

        # Connect main widget functions

        self.ui.protocolSelector.currentIndexChanged.connect(self.onProtocolSelectorIndexChanged)
        self.ui.loadProtocolFromFileButton.clicked.connect(self.onLoadProtocolFromFileClicked)
        self.ui.loadProtocolFromDatabaseButton.clicked.connect(self.onLoadProtocolFromDatabaseClicked)
        self.ui.createNewProtocolButton.clicked.connect(self.onNewProtocolClicked)

        self.ui.protocolEditRevertDiscardButton.clicked.connect(self.onEditRevertDiscardProtocolClicked)
        self.ui.protocolFileSaveButton.clicked.connect(self.onSaveProtocolToFileClicked)
        self.ui.protocolDatabaseSaveButton.clicked.connect(self.onSaveProtocolToDatabaseClicked)
        self.ui.protocolDatabaseDeleteButton.clicked.connect(self.onDeleteProtocolFromDatabaseClicked)

        # === Disable some of the widgets ===

        self.setProtocolEditButtonEnabled(False)
        self.setProtocolEditorEnabled(False)

        self.onDatabaseChanged()  # might not have queued
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

        # Cache a WIP (other modules might load one)
        if self._cur_save_state == SaveState.UNSAVED_CHANGES:
            protocol_changed = self.getProtocolFromGUI()
            self.logic.cache_protocol(self._cur_protocol_id, protocol_changed)

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

    def onDatabaseChanged(self, db: Optional["openlifu.db.Database"] = None):
        if not get_cur_db():
            self.setDatabaseButtonsEnabled(False)
        else:
            self.setDatabaseButtonsEnabled(True)

        # Edits to database parameter node should not change selected protocol
        prev_protocol = self.ui.protocolSelector.currentText

        self.reloadProtocols()

        if (len(get_openlifu_data_parameter_node().loaded_protocols) + len(self.logic.new_protocol_ids)) > 1:
            self.ui.protocolSelector.setCurrentText(prev_protocol)

    def onDataParameterNodeModified(self, caller = None, event = None):
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
            protocol = self.logic.get_default_protocol()
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
        protocol = self.logic.get_default_new_protocol()
        
        # Make sure default new protocol initialization has a unique id
        unique_default_id = self.logic.generate_unique_default_id()
        protocol.id = unique_default_id

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

        if not get_cur_db():
            raise RuntimeError("Cannot load protocol from database because there is no database connection")

        # Open the protocol selection dialog
        protocols : "List[openlifu.plan.Protocol]" = get_cur_db().load_all_protocols()
        protocol_names_and_IDs = [(p.name, p.id) for p in protocols]

        dialog = ProtocolSelectionFromDatabaseDialog(protocol_names_and_IDs)
        if dialog.exec_() == qt.QDialog.Accepted:
            selected_protocol_id = dialog.get_selected_protocol_id()
            if not selected_protocol_id:
                return

            protocol = get_cur_db().load_protocol(selected_protocol_id)

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

        self.pulse_definition_widget.update_form_from_class(protocol.pulse)
        self.sequence_definition_widget.update_form_from_class(protocol.sequence)
        self.abstract_focal_pattern_definition_widget.update_form_from_class(protocol.focal_pattern)
        self.sim_setup_definition_widget.update_form_from_class(protocol.sim_setup)
        self.abstract_delay_method_definition_widget.update_form_from_class(protocol.delay_method)
        self.abstract_apodization_method_definition_widget.update_form_from_class(protocol.apod_method)
        self.segmentation_method_definition_widget.update_form_from_class(protocol.seg_method)
        self.parameter_constraints_widget.from_dict(protocol.param_constraints)
        self.target_constraints_widget.from_list(protocol.target_constraints)
        self.solution_analysis_options_definition_widget.update_form_from_class(protocol.analysis_options)
        self.virtual_fit_options_definition_widget.update_form_from_class(protocol.virtual_fit_options)

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

    def getProtocolFromGUI(self) -> "openlifu.plan.Protocol":
        # Get the classes from dynamic widgets
        pulse = self.pulse_definition_widget.get_form_as_class()
        sequence = self.sequence_definition_widget.get_form_as_class()
        focal_pattern = self.abstract_focal_pattern_definition_widget.get_form_as_class()
        sim_setup = self.sim_setup_definition_widget.get_form_as_class()
        delay_method = self.abstract_delay_method_definition_widget.get_form_as_class()
        apodization_method = self.abstract_apodization_method_definition_widget.get_form_as_class()
        segmentation_method = self.segmentation_method_definition_widget.get_form_as_class()
        parameter_constraints = self.parameter_constraints_widget.to_dict()
        target_constraints = self.target_constraints_widget.to_list()
        solution_analysis_options = self.solution_analysis_options_definition_widget.get_form_as_class()
        virtual_fit_options = self.virtual_fit_options_definition_widget.get_form_as_class()

        # Then get the protocol class and return it
        protocol = openlifu_lz().plan.Protocol(
            name = self.ui.protocolNameLineEdit.text,
            id = self.ui.protocolIdLineEdit.text,
            description = self.ui.protocolDescriptionTextEdit.toPlainText(),
            pulse = pulse,
            sequence = sequence,
            focal_pattern = focal_pattern,
            sim_setup = sim_setup,
            delay_method = delay_method,
            apod_method = apodization_method,
            seg_method = segmentation_method,
            param_constraints = parameter_constraints,
            target_constraints = target_constraints,
            analysis_options = solution_analysis_options,
            virtual_fit_options = virtual_fit_options,
        )

        return protocol

    def setNewProtocolWidgets(self) -> None:
        self.setProtocolEditButtonEnabled(True)  # enable edit button (consistency)
        self.setProtocolEditorEnabled(True)  # enable editor
        self.ui.protocolDatabaseDeleteButton.setEnabled(False)

    def setProtocolEditorEnabled(self, enabled: bool) -> None:
        self.ui.protocolEditorSectionGroupBox.setEnabled(enabled)

        # Dynamic widgets
        self.pulse_definition_widget.setEnabled(enabled)
        self.sequence_definition_widget.setEnabled(enabled)
        self.abstract_focal_pattern_definition_widget.setEnabled(enabled)
        self.sim_setup_definition_widget.setEnabled(enabled)
        self.abstract_delay_method_definition_widget.setEnabled(enabled)
        self.abstract_apodization_method_definition_widget.setEnabled(enabled)
        self.segmentation_method_definition_widget.setEnabled(enabled)
        self.parameter_constraints_widget.setEnabled(enabled)
        self.target_constraints_widget.setEnabled(enabled)
        self.solution_analysis_options_definition_widget.setEnabled(enabled)
        self.virtual_fit_options_definition_widget.setEnabled(enabled)

        self.setAllSaveAndDeleteButtonsEnabled(enabled)
        if not get_cur_db():
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

        self.setCreateNewProtocolButtonEnabled(enabled)
        self.setProtocolEditorEnabled(enabled)
        self.setProtocolEditButtonEnabled(enabled)

    def load_protocol_from_openlifu(self, protocol: "openlifu.plan.Protocol", check_cache: bool = True) -> bool:

        """
        Handles loading a protocol, checking the cache for conflicts, and updating UI state.
        """
        if check_cache:
            if not self.logic.confirm_and_overwrite_protocol_cache(protocol):
                return False

            self.updateWidgetSaveState(SaveState.NO_CHANGES)
            self.reloadProtocols()

            replace_confirmed = True
        else:
            replace_confirmed = False

        # Load the protocol
        self.logic.dataLogic.load_protocol_from_openlifu(protocol, replace_confirmed=replace_confirmed)
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
        if not get_cur_db():
            return False
        return protocol_id in get_cur_db().get_protocol_ids()

    def protocol_id_exists(self, protocol_id: str) -> bool:
        return self.protocol_id_is_loaded(protocol_id) or self.protocol_id_is_in_database(protocol_id) or self.protocol_id_is_new(protocol_id) or self.protocol_id_is_in_cache(protocol_id)

    def generate_unique_default_id(self) -> str:
        i = 1
        base_id = DefaultNewProtocolValues.ID.value
        while self.protocol_id_exists(name := f"{base_id}_{i}"):
            i += 1
        return name

    def save_protocol_to_database(self, protocol: "openlifu.plan.Protocol") -> None:
        if get_cur_db() is None:
            raise RuntimeError("Cannot save protocol because there is no database connection")
        get_cur_db().write_protocol(protocol, openlifu_lz().db.database.OnConflictOpts.OVERWRITE)

    def delete_protocol_from_database(self, protocol_id: str) -> None:
        if get_cur_db() is None:
            raise RuntimeError("Cannot delete protocol because there is no database connection")
        get_cur_db().delete_protocol(protocol_id, openlifu_lz().db.database.OnConflictOpts.ERROR)

    def cache_protocol(self, protocol_id: str, protocol: "openlifu.plan.Protocol") -> None:
        self.cached_protocols[protocol_id] = protocol

    def delete_protocol_from_cache(self, protocol_id: str) -> None:
        self.cached_protocols.pop(protocol_id, None)  # remove from cache
        if protocol_id in self.new_protocol_ids:
            self.new_protocol_ids.discard(protocol_id)

    def confirm_and_overwrite_protocol_cache(self, protocol: "openlifu.plan.Protocol") -> bool:
        """
        Checks if the protocol ID exists in the cache. If so, prompts the user to confirm overwriting it.
        Returns False if the user cancels, otherwise updates the cache and returns True.
        """
        if self.protocol_id_is_in_cache(protocol.id):
            if not slicer.util.confirmYesNoDisplay(
                text=f"You have unsaved changes in a protocol with the same ID as the protocol you are trying to load. Discard and load the new one?",
                windowTitle="Discard Changes Confirmation",
            ):
                return False  # User canceled the load process

            self.delete_protocol_from_cache(protocol.id)
            return True
        else:
            return True
    
    @classmethod
    def get_default_pulse(cls):
        return openlifu_lz().bf.Pulse()

    @classmethod
    def get_default_sequence(cls):
        return openlifu_lz().bf.Sequence()

    @classmethod
    def get_default_focal_pattern(cls):
        return openlifu_lz().bf.focal_patterns.SinglePoint()

    @classmethod
    def get_default_sim_setup(cls):
        return openlifu_lz().sim.SimSetup()

    @classmethod
    def get_default_delay_method(cls):
        return openlifu_lz().bf.delay_methods.Direct()

    @classmethod
    def get_default_apodization_method(cls):
        return openlifu_lz().bf.apod_methods.Uniform()

    @classmethod
    def get_default_segmentation_method(cls):
        return openlifu_lz().seg.seg_methods.Water()

    @classmethod
    def get_default_parameter_constraints(cls):
        return {}

    @classmethod
    def get_default_target_constraints(cls):
        return []

    @classmethod
    def get_default_solution_analysis_options(cls):
        return openlifu_lz().plan.SolutionAnalysisOptions()

    @classmethod
    def get_default_virtual_fit_options(cls):
        return openlifu_lz().VirtualFitOptions()

    @classmethod
    def get_default_protocol(cls):
        return openlifu_lz().plan.Protocol(
            name=DefaultProtocolValues.NAME.value,
            id=DefaultProtocolValues.ID.value,
            description=DefaultProtocolValues.DESCRIPTION.value,

            pulse=cls.get_default_pulse(),
            sequence=cls.get_default_sequence(),
            focal_pattern=cls.get_default_focal_pattern(),
            sim_setup=cls.get_default_sim_setup(),
            delay_method=cls.get_default_delay_method(),
            apod_method=cls.get_default_apodization_method(),
            seg_method=cls.get_default_segmentation_method(),
            param_constraints=cls.get_default_parameter_constraints(),
            target_constraints=cls.get_default_target_constraints(),
            analysis_options=cls.get_default_solution_analysis_options(),
            virtual_fit_options=cls.get_default_virtual_fit_options(),
        )

    @classmethod
    def get_default_new_protocol(cls):
        return openlifu_lz().plan.Protocol(
            name=DefaultNewProtocolValues.NAME.value,
            id=DefaultNewProtocolValues.ID.value,
            description=DefaultNewProtocolValues.DESCRIPTION.value,

            pulse=cls.get_default_pulse(),
            sequence=cls.get_default_sequence(),
            focal_pattern=cls.get_default_focal_pattern(),
            sim_setup=cls.get_default_sim_setup(),
            delay_method=cls.get_default_delay_method(),
            apod_method=cls.get_default_apodization_method(),
            seg_method=cls.get_default_segmentation_method(),
            param_constraints=cls.get_default_parameter_constraints(),
            target_constraints=cls.get_default_target_constraints(),
            analysis_options=cls.get_default_solution_analysis_options(),
            virtual_fit_options=cls.get_default_virtual_fit_options(),
        )

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

#
# OpenLIFU Definition Widgets. All of these widgets rely on strongly typed
# classes.
#

class CreateKeyValueDialog(qt.QDialog):
    """
    Dialog for entering a key-value pair as strings, typically used for
    dictionary inputs.
    """

    def __init__(self, key_name: str, val_name: str, existing_keys: List[str], parent="mainWindow"):
        """
        Args:
            key_name (str): Label for the key input field.
            val_name (str): Label for the value input field.
            existing_keys (List[str]): List of keys to prevent duplicates.
            parent (QWidget or str): Parent widget or "mainWindow". Defaults to "mainWindow".
        """
        super().__init__(slicer.util.mainWindow() if parent == "mainWindow" else parent)
        self.setWindowTitle("Add Entry")
        self.setWindowModality(qt.Qt.ApplicationModal)
        self.key_name = key_name
        self.val_name = val_name
        self.existing_keys = existing_keys
        self.setup()

    def setup(self):
        self.setMinimumWidth(300)
        self.setContentsMargins(15, 15, 15, 15)

        formLayout = qt.QFormLayout()
        formLayout.setSpacing(10)
        self.setLayout(formLayout)

        self.key_input = qt.QLineEdit()
        formLayout.addRow(_(f"{self.key_name}:"), self.key_input)

        self.val_input = qt.QLineEdit()
        formLayout.addRow(_(f"{self.val_name}:"), self.val_input)

        self.buttonBox = qt.QDialogButtonBox()
        self.buttonBox.setStandardButtons(qt.QDialogButtonBox.Ok |
                                          qt.QDialogButtonBox.Cancel)
        formLayout.addWidget(self.buttonBox)

        self.buttonBox.rejected.connect(self.reject)
        self.buttonBox.accepted.connect(self.validateInputs)

    def validateInputs(self):
        """
        Ensure a key does not exist and that inputs are valid
        """
        typed_key = self.key_input.text
        typed_val = self.val_input.text

        if not typed_key:
            slicer.util.errorDisplay(f"{self.key_name} field cannot be empty.", parent=self)
            return
        if not typed_val:
            slicer.util.errorDisplay(f"{self.val_name} field cannot be empty.", parent=self)
            return
        if any(k == typed_key for k in self.existing_keys):
            slicer.util.errorDisplay(f"You cannot add duplicate {self.key_name} entries.", parent=self)
            return

        self.accept()

    def customexec_(self):
        returncode = self.exec_()
        if returncode == qt.QDialog.Accepted:
            return (returncode, self.key_input.text, self.val_input.text)
        return (returncode, None, None)

class CreateKeyAbstractClassValueDialog(CreateKeyValueDialog):
    """
    Dialog for entering a key-class pair, where class can be entered
    through the form generated by OpenLIFUAbstractDataclassDefinitionFormWidget;
    ideal for adding entries with arbitrary value types (e.g. custom
    classes) into dictionaries
    """

    def __init__(self, key_name: str, val_name: str, val_type: Type, existing_keys: List[str], parent="mainWindow"):
        """
        Args:
            key_name (str): Label for the key input field.
            val_name (str): Label for the value input section.
            val_type (Type): Class type used to generate the form for value input.
            existing_keys (List[str]): List of keys to prevent duplicates.
            parent (QWidget or str): Parent widget or "mainWindow". Defaults to "mainWindow".
        """
        self.val_type = val_type
        super().__init__(key_name, val_name, existing_keys, slicer.util.mainWindow() if parent == "mainWindow" else parent)

    def setup(self):
        self.setMinimumWidth(300)
        self.setContentsMargins(15, 15, 15, 15)

        formLayout = qt.QFormLayout()
        formLayout.setSpacing(10)
        self.setLayout(formLayout)

        self.key_input = qt.QLineEdit()
        formLayout.addRow(_(f"{self.key_name}:"), self.key_input)

        self.val_input = OpenLIFUAbstractDataclassDefinitionFormWidget(self.val_type, parent=self, is_collapsible=False)
        formLayout.addRow(_(f"{self.val_name}:"), self.val_input)

        self.buttonBox = qt.QDialogButtonBox()
        self.buttonBox.setStandardButtons(qt.QDialogButtonBox.Ok |
                                          qt.QDialogButtonBox.Cancel)
        formLayout.addWidget(self.buttonBox)

        self.buttonBox.rejected.connect(self.reject)
        self.buttonBox.accepted.connect(self.validateInputs)

    def validateInputs(self):
        """
        Ensure a key does not exist and that inputs are valid
        """
        typed_key = self.key_input.text
        typed_val = self.val_input.get_form_as_class()

        if not typed_key:
            slicer.util.errorDisplay(f"{self.key_name} field cannot be empty.", parent=self)
            return
        if typed_val is None:
            raise ValueError(f"{self.val_name} field cannot be None.")
        if any(k == typed_key for k in self.existing_keys):
            slicer.util.errorDisplay(f"You cannot add duplicate {self.key_name} entries.", parent=self)
            return

        self.accept()

    def customexec_(self):
        returncode = self.exec_()
        if returncode == qt.QDialog.Accepted:
            return (returncode, self.key_input.text, self.val_input.get_form_as_class())
        return (returncode, None, None)

class DictTableWidget(qt.QWidget):
    """
    A widget for displaying and editing dictionary entries in a two-column
    table.

    Each row represents a key-value pair. Values can be of any specified type
    and are stored using Qt's UserRole for internal retrieval. Includes a
    button to add new entries via a dialog.
    """

    def __init__(self, parent=None, key_name: str = "Key", val_name: str = "Value", val_type: Type = str):
        """
        Args:
            parent (QWidget, optional): Parent widget.
            key_name (str): Label for the key column. Defaults to "Key".
            val_name (str): Label for the value column. Defaults to "Value".
            val_type (Type): Type of the values stored in the dictionary. Defaults to str.
        """
        super().__init__(parent)
        self.key_name = key_name
        self.val_name = val_name
        self.val_type = val_type

        top_level_layout = qt.QVBoxLayout(self)

        # Add the table representing the dictionary

        self.table = qt.QTableWidget(self)
        self.table.setColumnCount(2)
        self.table.setHorizontalHeaderLabels([key_name, val_name])
        self.table.horizontalHeader().setStretchLastSection(True)
        self.table.verticalHeader().setVisible(False)
        self.table.setMinimumHeight(150)

        top_level_layout.addWidget(self.table)

        # Add and Remove buttons
        buttons_layout = qt.QHBoxLayout()

        self.add_button = qt.QPushButton(f"Add entry", self)
        self.remove_button = qt.QPushButton(f"Remove entry", self)

        for button in [self.add_button, self.remove_button]:
            button.setSizePolicy(qt.QSizePolicy.Expanding, qt.QSizePolicy.Preferred)
            buttons_layout.addWidget(button)

        self.add_button.clicked.connect(self._open_add_dialog)
        self.remove_button.clicked.connect(self._remove_selected_item)

        top_level_layout.addLayout(buttons_layout)

    def _open_add_dialog(self):
        existing_keys = list(self.to_dict().keys())
        if self.val_type is str:
            createDlg = CreateKeyValueDialog(self.key_name, self.val_name, existing_keys)
        else:
            createDlg = CreateKeyAbstractClassValueDialog(self.key_name, self.val_name, self.val_type, existing_keys)
        returncode, key, val = createDlg.customexec_()
        if not returncode:
            return

        self._add_row(key, val)

    def _remove_selected_item(self):
        selected_items = self.table.selectedItems()
        if not selected_items:
            slicer.util.errorDisplay(f"Please select an entry to delete.")
            return

        selected_row = selected_items[0].row()
        self.table.removeRow(selected_row)

    def _add_row(self, key, val):
        row_position = self.table.rowCount
        self.table.insertRow(row_position)

        key_item = qt.QTableWidgetItem(key)
        key_item.setFlags(key_item.flags() & ~qt.Qt.ItemIsEditable)
        self.table.setItem(row_position, 0, key_item)

        # Within the table itself, a string representation is required.
        # However, to associate custom user data, Qt.UserRole is used, which is
        # a predefined constant in Qt used to store custom,
        # application-specific data in the table, set with setData and
        # retrieved with .data(Qt.UserRole).
        val_item = qt.QTableWidgetItem(str(val))
        val_item.setData(qt.Qt.UserRole, val)
        val_item.setFlags(val_item.flags() & ~qt.Qt.ItemIsEditable)
        self.table.setItem(row_position, 1, val_item)

    def to_dict(self):
        result = {}
        for row in range(self.table.rowCount):
            key_item = self.table.item(row, 0)
            val_item = self.table.item(row, 1)
            if key_item and val_item:
                result[key_item.text()] = val_item.data(qt.Qt.UserRole)
        return result

    def from_dict(self, data: dict):
        self.table.setRowCount(0)
        for key, val in data.items():
            self._add_row(str(key), val)

class CreateAbstractClassDialog(qt.QDialog):
    """
    Dialog for creating a custom object, where the class is entered
    through the form generated by OpenLIFUAbstractDataclassDefinitionFormWidget;
    ideal for adding custom objects into lists
    """

    def __init__(self, object_name: str, object_type: Type, parent="mainWindow"):
        """
        Args:
            object_name (str): Label for the object input field.
            object_type (Type): Class type used to generate the form for object input.
            parent (QWidget or str): Parent widget or "mainWindow". Defaults to "mainWindow".
        """
        super().__init__(slicer.util.mainWindow() if parent == "mainWindow" else parent)
        self.setWindowTitle(f"Add {object_name}")
        self.setWindowModality(qt.Qt.ApplicationModal)
        self.object_name = object_name
        self.object_type = object_type
        self.setup()

    def setup(self):
        self.setMinimumWidth(300)
        self.setContentsMargins(15, 15, 15, 15)

        top_level_layout = qt.QFormLayout(self)
        top_level_layout.setSpacing(10)

        self.object_input = OpenLIFUAbstractDataclassDefinitionFormWidget(self.object_type, parent=self, is_collapsible=False)
        top_level_layout.addRow(_(f"{self.object_name}:"), self.object_input)

        self.buttonBox = qt.QDialogButtonBox()
        self.buttonBox.setStandardButtons(qt.QDialogButtonBox.Ok |
                                          qt.QDialogButtonBox.Cancel)
        top_level_layout.addWidget(self.buttonBox)

        self.buttonBox.rejected.connect(self.reject)
        self.buttonBox.accepted.connect(self.objectidateInputs)

    def objectidateInputs(self):
        """
        Ensure object is valid
        """
        typed_object = self.object_input.get_form_as_class()

        if typed_object is None:
            raise ValueError(f"{self.object_name} field cannot be None.")

        self.accept()

    def customexec_(self):
        returncode = self.exec_()
        if returncode == qt.QDialog.Accepted:
            return (returncode, self.object_input.get_form_as_class())
        return (returncode, None)

class ListTableWidget(qt.QWidget):
    """
    A widget for displaying and editing a list of items in a single-column
    table.

    Each row represents an item. Items are stored using Qt's UserRole for
    internal retrieval. Includes a button to add new entries via a dialog.
    """
    
    def __init__(self, parent=None, object_name: str = "Item", object_type: Type = str):
        """
        Args:
            parent (QWidget, optional): Parent widget.
            object_name (str): Label for the table column and button. Defaults to "Item".
            object_type (Type): Type of the items stored in the list. Defaults to str.
        """
        super().__init__(parent)
        self.object_name = object_name
        self.object_type = object_type

        top_level_layout = qt.QVBoxLayout(self)

        # Add the table representing the list

        self.table = qt.QTableWidget(self)
        self.table.setColumnCount(1)
        self.table.setHorizontalHeaderLabels([object_name])
        self.table.horizontalHeader().setStretchLastSection(True)
        self.table.verticalHeader().setVisible(False)
        self.table.setMinimumHeight(150)

        top_level_layout.addWidget(self.table)

        # Add and Remove buttons
        buttons_layout = qt.QHBoxLayout()

        self.add_button = qt.QPushButton(f"Add {object_name}", self)
        self.remove_button = qt.QPushButton(f"Remove {object_name}", self)

        for button in [self.add_button, self.remove_button]:
            button.setSizePolicy(qt.QSizePolicy.Expanding, qt.QSizePolicy.Preferred)
            buttons_layout.addWidget(button)

        self.add_button.clicked.connect(self._open_add_dialog)
        self.remove_button.clicked.connect(self._remove_selected_item)

        top_level_layout.addLayout(buttons_layout)

    def _open_add_dialog(self):
        createDlg = CreateAbstractClassDialog(self.object_name, self.object_type)
        returncode, new_object = createDlg.customexec_()

        if not returncode:
            return

        self._add_row(new_object)

    def _remove_selected_item(self):
        selected_items = self.table.selectedItems()
        if not selected_items:
            slicer.util.errorDisplay(f"Please select a {self.object_name} to delete.")
            return

        selected_row = selected_items[0].row()
        self.table.removeRow(selected_row)

    def _add_row(self, new_object):
        row_position = self.table.rowCount
        self.table.insertRow(row_position)

        # Within the table itself, a string representation is required.
        # However, to associate custom user data, Qt.UserRole is used, which is
        # a predefined constant in Qt used to store custom,
        # application-specific data in the table, set with setData and
        # retrieved with .data(Qt.UserRole).
        new_object_item = qt.QTableWidgetItem(str(new_object))
        new_object_item.setData(qt.Qt.UserRole, new_object)
        new_object_item.setFlags(new_object_item.flags() & ~qt.Qt.ItemIsEditable)
        self.table.setItem(row_position, 0, new_object_item)

    def to_list(self):
        """
        Returns:
            list: A list of items currently stored in the table.
        """
        result = []
        for row in range(self.table.rowCount):
            object_item = self.table.item(row, 0)
            if object_item:
                result.append(object_item.data(qt.Qt.UserRole))
        return result

    def from_list(self, data: list):
        """
        Populates the table from a given list of items.

        Args:
            data (list): List of items to display in the table.
        """
        self.table.setRowCount(0)
        for obj in data:
            self._add_row(obj)

class OpenLIFUAbstractDataclassDefinitionFormWidget(qt.QWidget):
    DEFAULT_INT_VALUE = 0
    DEFAULT_INT_RANGE = (-1_000_000, 1_000_000)
    DEFAULT_FLOAT_VALUE = 0.
    DEFAULT_FLOAT_RANGE = (-1e6, 1e6)
    DEFAULT_FLOAT_NUM_DECIMALS = 4

    def __init__(self, cls: Type[Any], parent: Optional[qt.QWidget] = None, is_collapsible: bool = True, collapsible_title: Optional[str] = None):
        """
        Initializes a QWidget containing a form layout with labeled inputs for
        each attribute of an instance created from the specified dataclass. Input
        widgets are generated based on attribute types:

        - int: QSpinBox
        - float: QDoubleSpinBox
        - str: QLineEdit
        - bool: QCheckBox
        - dict: DictTableWidget (2 columns for key-value pairs)
        - Tuple[primitive_type, ...]: Horizontal of widgets for filling out all the values in the tuple
        - Tuple[Tuple[primitive_type] | primitive_type ...]: Vertical container of horizontal containers of widgets for filling out all the values in the nested tuple

        If is_collapsible is True, the form is enclosed in a collapsible container
        with an optional title.

        Args:
            cls: A dataclass (not an instance) whose attributes will populate the form.
            parent: Optional parent widget.
            is_collapsible: Whether to enclose the form in a collapsible container.
            collapsible_title: Optional title for the collapsible section.
        """

        if not inspect.isclass(cls) or cls in (int, float, str, bool, dict, list, tuple, set):
            raise TypeError(f"'cls' must be a user-defined class with type annotations, not a built-in type like {cls.__name__}")

        if not is_dataclass(cls):
            raise TypeError(f"{cls.__name__} is not a dataclass. This class form widget only works for dataclasses.")

        super().__init__(parent)
        self._field_widgets: dict[str, qt.QWidget] = {}
        self._cls = cls

        if is_collapsible:
            # self (QWidget) has a QVBoxLayout layout
            top_level_layout = qt.QVBoxLayout(self)

            # Create collapsible button and add it to the top level layout
            collapsible = ctk.ctkCollapsibleButton()
            collapsible.text = f"Parameters for {cls.__name__}" if collapsible_title is None else collapsible_title
            top_level_layout.addWidget(collapsible)

            # collapsible (ctkCollapsibleButton) has a QVBoxLayout layout
            collapsible_layout = qt.QVBoxLayout(collapsible)

            # Create the inner form widget and add it to the collapsible layout
            form_widget = qt.QWidget()
            form_layout = qt.QFormLayout(form_widget)
            collapsible_layout.addWidget(form_widget)
        else:
            form_layout = qt.QFormLayout(self)

        type_hints = get_hints(cls, include_extras=True)
        dataclass_fields = {f.name: f for f in fields(cls)}

        for name, annotated_type in type_hints.items():

            # Some dataclass fields cannot be initialized
            field_info = dataclass_fields.get(name)
            if field_info is None or not field_info.init:
                continue # Skip fields not meant to be initialized. e.g., openlifu.seg.material.Material.param_ids

            # Now, for each member of cls, create widgets and tooltips
            origin = get_origin(annotated_type)
            args = get_args(annotated_type)

            if origin is Annotated and len(args) > 1:  # If field has Annotated[]
                base_type = args[0]
                metadata = args[1]
                label_text = metadata.name if metadata.name is not None else name
                tooltip_text = metadata.description if metadata.description is not None else f"Write a description for {name}"
            else:  # Field was not Annotated[]
                base_type = annotated_type
                label_text = name
                tooltip_text = f"Write a description for {name}"

            widget = self._create_widget_for_type(base_type)
            if widget:
                label = qt.QLabel(label_text)
                label.setToolTip(tooltip_text)
                widget.setToolTip(tooltip_text)

                form_layout.addRow(label, widget)
                self._field_widgets[name] = widget

    def _create_widget_for_type(self, annotated_type: Any) -> Optional[qt.QWidget]:
        origin = get_origin(annotated_type)
        args = get_args(annotated_type)

        def create_basic_widget(typ: Any) -> Optional[qt.QWidget]:
            if typ is int:
                w = qt.QSpinBox()
                w.setRange(*self.DEFAULT_INT_RANGE)
                w.setValue(self.DEFAULT_INT_VALUE)
                return w
            elif typ is float:
                w = qt.QDoubleSpinBox()
                w.setDecimals(self.DEFAULT_FLOAT_NUM_DECIMALS)
                w.setRange(*self.DEFAULT_FLOAT_RANGE)
                w.setValue(self.DEFAULT_FLOAT_VALUE)
                return w
            elif typ is str:
                return qt.QLineEdit()
            elif typ is bool:
                return qt.QCheckBox()
            elif typ is dict:
                # raw dict does not have origin or args. We assume dict[str,str]
                return DictTableWidget()
            return None

        if origin is None:
            return create_basic_widget(annotated_type)

        if origin is dict:
            if len(args) == 2:
                key_type, val_type = args
                if key_type is str and val_type is str:
                    return DictTableWidget()
                elif key_type is str and hasattr(val_type, "__annotations__"):
                    return DictTableWidget(val_type=val_type)
            return DictTableWidget()

        # non-nested tuple
        if origin is tuple and all(get_origin(t) is None for t in args):
            # if making a form entry for a tuple, it's a container of widgets
            container = qt.QWidget()
            layout = qt.QHBoxLayout(container)
            layout.setContentsMargins(0, 0, 0, 0)

            for typ in args:
                if typ is dict:
                    raise TypeError(f"Invalid tuple field: dict inside a tuple structure not yet supported.")

                widget = create_basic_widget(typ)
                if widget is None:
                    return None  # unsupported tuple element type
                layout.addWidget(widget)

            container.setLayout(layout)
            return container

        # Tuple[Tuple[primitive_type], ...] or Tuple[primitive_type, Tuple[...], ...]
        if origin is tuple:
            container = qt.QWidget()
            layout = qt.QVBoxLayout(container)
            layout.setContentsMargins(0, 0, 0, 0)

            for outer_type in args:
                outer_origin = get_origin(outer_type)
                outer_args = get_args(outer_type)

                # Case: Nested tuple -> horizontal row of widgets
                if outer_origin is tuple:
                    row_widget = qt.QWidget()
                    row_layout = qt.QHBoxLayout(row_widget)
                    row_layout.setContentsMargins(0, 0, 0, 0)

                    for inner_type in outer_args:
                        if inner_type is dict:
                            raise TypeError("Invalid tuple field: dict inside a tuple structure not yet supported.")

                        inner_widget = create_basic_widget(inner_type)
                        if inner_widget is None:
                            return None
                        row_layout.addWidget(inner_widget)

                    layout.addWidget(row_widget)

                # Case: Single type -> single widget in its own row
                else:
                    single_widget = create_basic_widget(outer_type)
                    if single_widget is None:
                        return None
                    layout.addWidget(single_widget)

            container.setLayout(layout)
            return container

        return None # unsupported type

    def update_form_from_values(self, values: dict[str, Any]) -> None:
        """
        Updates form inputs from a dictionary of values.

        Args:
            values: Dictionary mapping attribute names to new values.
        """

        def _set_widget_value(widget: qt.QWidget, value: Any) -> None:
            """
            Helper method to set a value on a widget based on its type, with relaxed type checks.
            """
            if isinstance(widget, qt.QSpinBox) and isinstance(value, int):
                widget.setValue(value)
            elif isinstance(widget, qt.QDoubleSpinBox) and isinstance(value, (int, float)):
                widget.setValue(float(value))
            elif isinstance(widget, qt.QLineEdit):
                widget.setText(str(value))
            elif isinstance(widget, qt.QCheckBox) and isinstance(value, bool):
                widget.setChecked(value)
            elif isinstance(widget, DictTableWidget) and isinstance(value, dict):
                raise TypeError("Invalid tuple field: dict inside a tuple structure not yet supported.")
            else:
                raise TypeError(f"Unsupported widget-value combination: {type(widget)} and {type(value)}")
    
        for name, val in values.items():
            if name not in self._field_widgets:
                continue
            w = self._field_widgets[name]
            if isinstance(w, qt.QSpinBox):
                w.setValue(int(val))
            elif isinstance(w, qt.QDoubleSpinBox):
                w.setValue(float(val))
            elif isinstance(w, qt.QLineEdit):
                w.setText(str(val))
            elif isinstance(w, qt.QCheckBox):
                w.setChecked(bool(val))
            elif isinstance(w, DictTableWidget) and isinstance(val, dict):
                w.from_dict(val)
            elif isinstance(w, qt.QWidget) and isinstance(val, tuple):
                # determine if it's a flat tuple (horizontal row) or a nested one (vertical stack)
                layout = w.layout()
                if isinstance(layout, qt.QHBoxLayout) and layout.count() == len(val):
                    # flat tuple (horizontal row of widgets)
                    for i, item in enumerate(val):
                        child = layout.itemAt(i).widget()
                        _set_widget_value(child, item)

                elif isinstance(layout, qt.QVBoxLayout) and layout.count() == len(val):
                    # nested tuple (vertical stack of rows)
                    for row_idx, row_val in enumerate(val):
                        row_item = layout.itemAt(row_idx)
                        row_widget = row_item.widget()
                        if isinstance(row_val, tuple):
                            # row is a tuple 
                            row_layout = row_widget.layout()
                            if row_layout.count() != len(row_val):
                                continue
                            for col_idx, col_val in enumerate(row_val):
                                child = row_layout.itemAt(col_idx).widget()
                                _set_widget_value(child, col_val)
                                
                        else:
                            # row is a single widget
                            _set_widget_value(row_widget, row_val)

    def get_form_as_dict(self) -> dict[str, Any]:
        """
        Returns the current form values as a dictionary.
        """
        
        def _extract_widget_value(widget: qt.QWidget) -> Any:
            """
            Helper method to get a value from a widget based on its type.
            """
            if isinstance(widget, qt.QSpinBox):
                return widget.value
            elif isinstance(widget, qt.QDoubleSpinBox):
                return widget.value
            elif isinstance(widget, qt.QLineEdit):
                return widget.text
            elif isinstance(widget, qt.QCheckBox):
                return widget.isChecked()
            return None

        values: dict[str, Any] = {}
        for name, w in self._field_widgets.items():
            if isinstance(w, qt.QSpinBox):
                values[name] = w.value
            elif isinstance(w, qt.QDoubleSpinBox):
                values[name] = w.value
            elif isinstance(w, qt.QLineEdit):
                values[name] = w.text
            elif isinstance(w, qt.QCheckBox):
                values[name] = w.isChecked()
            elif isinstance(w, DictTableWidget):
                values[name] = w.to_dict()
            elif isinstance(w, qt.QWidget):  # assumed to be flat or nested tuple
                layout = w.layout()
                if isinstance(layout, qt.QHBoxLayout): # flat tuple (hbox)
                    tuple_values = []
                    for i in range(layout.count()):
                        child = layout.itemAt(i).widget()
                        value = _extract_widget_value(child)
                        if value is None:
                            break
                        tuple_values.append(value)
                    else:
                        values[name] = tuple(tuple_values)
                elif isinstance(layout, qt.QVBoxLayout): # nested tuple (vbox)
                    nested_values = []
                    for i in range(layout.count()):
                        row_widget = layout.itemAt(i).widget()
                        if row_widget is None:
                            continue
                        row_layout = row_widget.layout()
                        if isinstance(row_layout, qt.QHBoxLayout):
                            # row is a tuple (hbox)
                            row_values = []
                            for j in range(row_layout.count()):
                                child = row_layout.itemAt(j).widget()
                                value = _extract_widget_value(child)
                                if value is None:
                                    break
                                row_values.append(value)
                            else:
                                nested_values.append(tuple(row_values))
                        else:
                            # row is a singular widget
                            value = _extract_widget_value(row_widget)
                            if value is not None:
                                nested_values.append(value)
                    values[name] = tuple(nested_values)
        return values

    def update_form_from_class(self, instance: Any) -> None:
        """
        Updates the form fields using the attribute values from the provided instance.

        Args:
            instance: An instance of the same class used to create the form.
        """
        values = vars(instance)
        self.update_form_from_values(values)

    def get_form_as_class(self) -> Any:
        """
        Constructs and returns a new instance of the class using the current form values.

        Returns:
            A new instance of the class populated with the form's current values.
        """
        return self._cls(**self.get_form_as_dict())

    def add_value_changed_signals(self, callback) -> None:
        """
        Connects value change signals of all widgets to a given callback.

        Args:
            callback: Function to call on value change.
        """
        for w in self._field_widgets.values():
            if isinstance(w, qt.QSpinBox):
                w.valueChanged.connect(callback)
            elif isinstance(w, qt.QDoubleSpinBox):
                w.valueChanged.connect(callback)
            elif isinstance(w, qt.QLineEdit):
                w.textChanged.connect(callback)
            elif isinstance(w, qt.QCheckBox):
                w.stateChanged.connect(callback)
            elif isinstance(w, DictTableWidget):
                w.table.itemChanged.connect(lambda *_: callback())
            elif isinstance(w, qt.QWidget): # assumed to be container for tuple
                for child in slicer.util.findChildren(w):
                    if isinstance(child, qt.QSpinBox):
                        child.valueChanged.connect(callback)
                    elif isinstance(child, qt.QDoubleSpinBox):
                        child.valueChanged.connect(callback)
                    elif isinstance(child, qt.QLineEdit):
                        child.textChanged.connect(callback)
                    elif isinstance(child, qt.QCheckBox):
                        child.stateChanged.connect(callback)

    def modify_field_spinbox(self, field: str, default_value = None, min_value = None, max_value = None, num_decimals = None) -> None:
        """
        Sets the default, minimum & maximum values, and number of decimals (if
        applicable) for a spin box widget associated with a specific field.

        Args:
            field (str): The name of the field associated with the widget.

        Raises:
            TypeError: If the widget is not a QSpinBox or QDoubleSpinBox, as
            only these types support numeric range control.
        """
        w: qt.QWidget = self._field_widgets[field]

        if isinstance(w, qt.QSpinBox):
            if default_value is None:
                default_value = self.DEFAULT_INT_VALUE
            if min_value is None:
                min_value = self.DEFAULT_INT_RANGE[0]
            if max_value is None:
                min_value = self.DEFAULT_INT_RANGE[1]
        elif isinstance(w, qt.QDoubleSpinBox):
            if default_value is None:
                default_value = self.DEFAULT_FLOAT_VALUE
            if min_value is None:
                min_value = self.DEFAULT_FLOAT_RANGE[0]
            if max_value is None:
                min_value = self.DEFAULT_FLOAT_RANGE[1]
            if num_decimals is None:
                num_decimals = self.DEFAULT_FLOAT_NUM_DECIMALS
            w.setDecimals(num_decimals)
        else:
            raise TypeError(
                f"Widget for field '{field}' must be a QSpinBox or QDoubleSpinBox to set range and default."
                f"Got {type(w).__name__} instead."
            )
        w.setRange(min_value, max_value)
        w.setValue(default_value)

class OpenLIFUAbstractMultipleABCDefinitionFormWidget(qt.QWidget):
    def __init__(self, cls_list: List[Type[Any]], parent: Optional[qt.QWidget] = None, is_collapsible: bool = True, collapsible_title: Optional[str] = None, custom_abc_title: Optional[str] = None):
        """
        Creates a QWidget that allows multiple implementations of an Abstract
        Base Class to be selected, which after selection will display the
        corresponding form widget (through
        OpenLIFUAbstractDataclassDefinitionFormWidget) allowing the specific ABC to
        be configured

        Args:
            cls_list: A list of classes belonging to the same ABC whose attributes will populate the form.
            parent: Optional parent widget.
        """
        if not cls_list:
            raise ValueError("cls_list cannot be empty.")

        self.base_class_name = cls_list[0].__bases__[0].__name__
        self.custom_abc_title = self.base_class_name if custom_abc_title is None else custom_abc_title
        
        if not all(cls.__bases__[0].__name__ == self.base_class_name for cls in cls_list):
            raise TypeError("All classes in cls_list must share the same base class name.")

        super().__init__(parent)

        top_level_layout = qt.QFormLayout(self)

        self.selector = qt.QComboBox()
        self.forms = qt.QStackedWidget()

        for cls in cls_list:
            self.selector.addItem(cls.__name__)
            self.forms.addWidget(OpenLIFUAbstractDataclassDefinitionFormWidget(cls, parent, is_collapsible, collapsible_title))

        top_level_layout.addRow(qt.QLabel(f"{self.custom_abc_title} type"), self.selector) 
        top_level_layout.addRow(qt.QLabel(f"{self.custom_abc_title} options"), self.forms) 

        # Connect combo box to setting the widget. Assumes indices match
        self.selector.currentIndexChanged.connect(self.forms.setCurrentIndex)

    def update_form_from_class(self, instance_of_derived: Any) -> None:
        """
        Updates the selected form and form fields using the class name of the instance_of_derived as well as the attribute values in the instance.

        Args:
            instance_of_derived: An instance_of_derived of the same class used to create the form.
        """

        # __name__ is an attribute of a class, not an instance.
        index = self.selector.findText(instance_of_derived.__class__.__name__)
        self.selector.setCurrentIndex(index) # also changes the stacked widget through the signal

        self.forms.currentWidget().update_form_from_class(instance_of_derived)

    def get_form_as_class(self) -> Any:
        """
        Constructs and returns a new instance of the derived class using the current form values.

        Returns:
            A new instance of the derived class populated with the form's current values.
        """
        return self.forms.currentWidget().get_form_as_class()


    def add_value_changed_signals(self, callback) -> None:
        """
        Connects value change signals of all widgets in all forms to a given
        callback.

        Args:
            callback: Function to call on value change.
        """
        self.selector.currentIndexChanged.connect(callback)
        for w_idx in range(self.forms.count):
            form = self.forms.widget(w_idx)
            form.add_value_changed_signals(callback)
