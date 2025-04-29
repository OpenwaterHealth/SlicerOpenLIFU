# Standard library imports
from enum import Enum
from pathlib import Path
from typing import (
    List,
    Optional,
    Tuple,
    TYPE_CHECKING,
)

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
    get_cur_db,
    get_openlifu_data_parameter_node,
    openlifu_lz,
)
from OpenLIFULib.class_definition_widgets import (
    DictTableWidget,
    ListTableWidget,
    OpenLIFUAbstractDataclassDefinitionFormWidget,
    OpenLIFUAbstractMultipleABCDefinitionFormWidget,
)
from OpenLIFULib.user_account_mode_util import UserAccountBanner
from OpenLIFULib.util import (
    display_errors,
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

        self.allowed_roles_widget = ListTableWidget(parent=self.ui.pulseDefinitionWidgetPlaceholder.parentWidget(), object_name="Role", object_type=str)
        replace_widget(self.ui.allowedRolesWidgetPlaceholder, self.allowed_roles_widget, self.ui)

        self.pulse_definition_widget = OpenLIFUAbstractDataclassDefinitionFormWidget(cls=openlifu_lz().bf.Pulse, parent=self.ui.pulseDefinitionWidgetPlaceholder.parentWidget(), collapsible_title="Parameters for Pulse")
        replace_widget(self.ui.pulseDefinitionWidgetPlaceholder, self.pulse_definition_widget, self.ui)

        self.sequence_definition_widget = OpenLIFUAbstractDataclassDefinitionFormWidget(cls=openlifu_lz().bf.Sequence, parent=self.ui.sequenceDefinitionWidgetPlaceholder.parentWidget(), collapsible_title="Parameters for Sequence")
        replace_widget(self.ui.sequenceDefinitionWidgetPlaceholder, self.sequence_definition_widget, self.ui)

        self.abstract_focal_pattern_definition_widget = OpenLIFUAbstractMultipleABCDefinitionFormWidget([openlifu_lz().bf.Wheel, openlifu_lz().bf.SinglePoint], is_collapsible=False, collapsible_title="Focal Pattern", custom_abc_title="Focal Pattern")
        replace_widget(self.ui.abstractFocalPatternDefinitionWidgetPlaceholder, self.abstract_focal_pattern_definition_widget, self.ui)

        self.sim_setup_definition_widget = OpenLIFUSimSetupDefinitionFormWidget(parent=self.ui.simSetupDefinitionWidgetPlaceholder.parentWidget())
        replace_widget(self.ui.simSetupDefinitionWidgetPlaceholder, self.sim_setup_definition_widget, self.ui)

        self.abstract_delay_method_definition_widget = OpenLIFUAbstractDelayMethodDefinitionFormWidget()
        replace_widget(self.ui.abstractDelayMethodDefinitionWidgetPlaceholder, self.abstract_delay_method_definition_widget, self.ui)

        self.abstract_apodization_method_definition_widget = OpenLIFUAbstractApodizationMethodDefinitionFormWidget()
        replace_widget(self.ui.abstractApodizationMethodDefinitionWidgetPlaceholder, self.abstract_apodization_method_definition_widget, self.ui)

        self.segmentation_method_definition_widget = OpenLIFUAbstractDataclassDefinitionFormWidget(cls=openlifu_lz().seg.SegmentationMethod, parent=self.ui.segmentationMethodDefinitionWidgetPlaceholder.parentWidget(), collapsible_title="Segmentation Method")
        replace_widget(self.ui.segmentationMethodDefinitionWidgetPlaceholder, self.segmentation_method_definition_widget, self.ui)

        self.parameter_constraints_widget = OpenLIFUParameterConstraintsWidget()
        replace_widget(self.ui.parameterConstraintsWidgetPlaceholder, self.parameter_constraints_widget, self.ui)

        self.target_constraints_widget = ListTableWidget(object_name="Target Constraint", object_type=openlifu_lz().plan.TargetConstraints)
        replace_widget(self.ui.targetConstraintsWidgetPlaceholder, self.target_constraints_widget, self.ui)

        self.solution_analysis_options_definition_widget = OpenLIFUSolutionAnalysisOptionsDefinitionFormWidget()
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

        self.allowed_roles_widget.table.itemChanged.connect(lambda *_: trigger_unsaved_changes())
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

        self.allowed_roles_widget.from_list(protocol.allowed_roles)
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
        allowed_roles = self.allowed_roles_widget.to_list()
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
            allowed_roles = allowed_roles,
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
        self.allowed_roles_widget.setEnabled(enabled)
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
    def get_default_allowed_roles(cls):
        return []

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

            allowed_roles=cls.get_default_allowed_roles(),
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

            allowed_roles=cls.get_default_allowed_roles(),
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
# Subclasses of OpenLIFU Definition Widgets.
#

class OpenLIFUSimSetupDefinitionFormWidget(OpenLIFUAbstractDataclassDefinitionFormWidget):
    def __init__(self, parent: Optional[qt.QWidget] = None):
        super().__init__(openlifu_lz().sim.SimSetup, parent, is_collapsible=True, collapsible_title="Sim Setup")

        # Modify the defaults and ranges for x_extent, y_extent, and z_extent
        x_ext_hbox = self._field_widgets['x_extent'].layout()
        y_ext_hbox = self._field_widgets['y_extent'].layout()
        z_ext_hbox = self._field_widgets['z_extent'].layout()
        
        self.modify_widget_spinbox(x_ext_hbox.itemAt(0).widget(), default_value=-30, min_value=-200, max_value=-1)
        self.modify_widget_spinbox(x_ext_hbox.itemAt(1).widget(), default_value=30, min_value=1, max_value=200)
        self.modify_widget_spinbox(y_ext_hbox.itemAt(0).widget(), default_value=-30, min_value=-200, max_value=-1)
        self.modify_widget_spinbox(y_ext_hbox.itemAt(1).widget(), default_value=30, min_value=1, max_value=200)
        self.modify_widget_spinbox(z_ext_hbox.itemAt(0).widget(), default_value=-4, min_value=-4, max_value=-4)
        self.modify_widget_spinbox(z_ext_hbox.itemAt(1).widget(), default_value=60, min_value=1, max_value=200)

        # Modify the default and range for spacing
        spacing_spinbox = self._field_widgets['spacing']
        self.modify_widget_spinbox(spacing_spinbox, default_value=1.0, min_value=0.1, max_value=2.0)

class OpenLIFUAbstractDelayMethodDefinitionFormWidget(OpenLIFUAbstractMultipleABCDefinitionFormWidget):
    def __init__(self):
        super().__init__([openlifu_lz().bf.delay_methods.Direct], is_collapsible=False, collapsible_title="Delay Method", custom_abc_title="Delay Method")

        # Select Direct as the default. Note: `get_default_delay_method` should
        # make sure Direct is also set.
        self.forms.setCurrentIndex(0)

        # ---- Configure Direct class ----

        direct_definition_form_widget = self.forms.widget(0)

        # Modify the default and range for c0
        c0_spinbox = direct_definition_form_widget._field_widgets['c0']
        direct_definition_form_widget.modify_widget_spinbox(c0_spinbox, default_value=1480, min_value=1000, max_value=3000)

class OpenLIFUAbstractApodizationMethodDefinitionFormWidget(OpenLIFUAbstractMultipleABCDefinitionFormWidget):
    def __init__(self):
        super().__init__([openlifu_lz().bf.apod_methods.MaxAngle, openlifu_lz().bf.apod_methods.PiecewiseLinear, openlifu_lz().bf.apod_methods.Uniform], is_collapsible=False, collapsible_title="Apodization Method", custom_abc_title="Apodization Method")

        # Select Uniform as the default. Note: `get_default_apodization_method`
        # should make sure Uniform also set.
        self.forms.setCurrentIndex(2)

        # ---- Configure MaxAngle class ----

        maxangle_definition_form_widget = self.forms.widget(0)

        # Modify the default and range for max_angle
        max_angle_spinbox = maxangle_definition_form_widget._field_widgets['max_angle']
        maxangle_definition_form_widget.modify_widget_spinbox(max_angle_spinbox, default_value=30, min_value=0, max_value=90)

class OpenLIFUParameterConstraintsWidget(DictTableWidget):

    class CreateParameterParameterConstraintDialog(qt.QDialog):
        """
        Dialog for creating a parameter constraint, allowing users to define warning and error thresholds
        for a specific parameter using different operators (e.g., <, <=, within, outside).

        The operator dropdown determines whether the constraint uses a single value (e.g., < 5)
        or a range of values (e.g., within [4, 6]). When a range is needed, two spin boxes are shown
        for both warning and error values. Otherwise, only one is visible for each. Therefore, there 
        is some logic in here for updating between showing 2 spinboxes and 1. Furthermore, there is a
        mapping to the text that is displayed vs the operator as defined in "openlifu.plan.ParameterConstraint"
        """

        def __init__(self, existing_keys: List[str], parent="mainWindow"):
            super().__init__(slicer.util.mainWindow() if parent == "mainWindow" else parent)
            self.existing_keys = existing_keys
            self.setWindowTitle("Create Parameter Constraint")
            self.setMinimumWidth(350)

            self.operator_display_map = {
                "<": "is less than (<)",
                "<=": "is less than or equal to (<=)",
                ">": "is greater than (>)",
                ">=": "is greater than or equal to (>=)",
                "within": "is within",
                "inside": "is inside",
                "outside": "is outside",
                "outside_inclusive": "is outside inclusive"
            }
            self.inverse_operator_display_map = {v: k for k, v in self.operator_display_map.items()}

            self.parameter_key_map = {
                "Thermal Index (TIC)": "TIC",
                "Mechanical Index (MI)": "MI",
                "Mainlobe PNP (MPa)": "mainlobe_pnp_MPa",
                "Mainlobe I_SPPA (W/cm^2)": "mainlobe_isppa_Wcm2",
                "Mainlobe I_SPTA (W/cm^2)": "mainloba_ispta_Wcm2",
                "3 dB Lateral Beamwidth (mm)": "beamwidth_lat_3dB_mm",
                "3 dB Elevational Beamwidth (mm)": "beamwidth_ele_3dB_mm",
                "3 dB Axial Beamwidth (mm)": "beamwidth_ax_3dB_mm",
                "6 dB Lateral Beamwidth (mm)": "beamwidth_lat_6dB_mm",
                "6 dB Elevational Beamwidth (mm)": "beamwidth_ele_6dB_mm",
                "6 dB Axial Beamwidth (mm)": "beamwidth_ax_6dB_mm",
                "Sidelobe PNP (MPa)": "sidelobe_pnp_MPa",
                "Sidelobe I_SPPA (W/cm2)": "sidelobe_isppa_Wcm2",
                "Global PNP (MPa)": "global_pnp_MPa",
                "Global I_SPPA (W/cm^2)": "global_isppa_Wcm2",
                "Global I_SPTA (W/cm^2)": "global_ispta_Wcm2",
                "Emitted Pressure (MPa)": "p0_MPa",
                "Emitted Power (W)": "power_W"
            }
            self.inverse_parameter_key_map = {v: k for k, v in self.parameter_key_map.items()}

            self.setup()

        def setup(self):
            self.setMinimumWidth(400)
            self.setContentsMargins(15, 15, 15, 15)

            formLayout = qt.QFormLayout()
            formLayout.setSpacing(5)
            self.setLayout(formLayout)

            self.parameter_name_input = qt.QComboBox()
            self.parameter_name_input.addItems(list(self.parameter_key_map.keys()))
            formLayout.addRow(_(f"Parameter Name:"), self.parameter_name_input)

            self.operator_selector = qt.QComboBox()
            self.operator_selector.addItems(list(self.operator_display_map.values()))
            self.operator_selector.currentTextChanged.connect(self._update_visible_spinboxes)
            formLayout.addRow(_(f"Operator:"), self.operator_selector)

            self.warning_spinboxes = []
            self.error_spinboxes = []
            self.warning_and_label = qt.QLabel("and")
            self.warning_and_label.setAlignment(qt.Qt.AlignCenter)
            self.error_and_label = qt.QLabel("and")
            self.error_and_label.setAlignment(qt.Qt.AlignCenter)

            self.warning_box_layout = qt.QHBoxLayout()
            self.error_box_layout = qt.QHBoxLayout()

            warning_container = qt.QWidget()
            warning_container.setLayout(self.warning_box_layout)
            formLayout.addRow(_(f"Warning Value(s):"), warning_container)

            error_container = qt.QWidget()
            error_container.setLayout(self.error_box_layout)
            formLayout.addRow(_(f"Error Value(s):"), error_container)

            self._init_spinboxes()

            self.buttonBox = qt.QDialogButtonBox()
            self.buttonBox.setStandardButtons(qt.QDialogButtonBox.Ok | qt.QDialogButtonBox.Cancel)
            formLayout.addWidget(self.buttonBox)

            self.buttonBox.rejected.connect(self.reject)
            self.buttonBox.accepted.connect(self._on_accept)

        def _init_spinboxes(self):
            for _ in range(2):
                warning_spinbox = qt.QDoubleSpinBox()
                error_spinbox = qt.QDoubleSpinBox()
                warning_spinbox.setRange(-1e6, 1e6)
                error_spinbox.setRange(-1e6, 1e6)
                self.warning_spinboxes.append(warning_spinbox)
                self.error_spinboxes.append(error_spinbox)
                self.warning_box_layout.addWidget(warning_spinbox)
                self.error_box_layout.addWidget(error_spinbox)

            self.warning_box_layout.insertWidget(1, self.warning_and_label)
            self.error_box_layout.insertWidget(1, self.error_and_label)

            self._update_visible_spinboxes(self.operator_selector.currentText)

        def _update_visible_spinboxes(self, display_operator: str):
            operator = self.inverse_operator_display_map[display_operator]
            use_two_values = operator in ['within', 'inside', 'outside', 'outside_inclusive']
            for i in range(2):
                self.warning_spinboxes[i].setVisible(use_two_values or i == 0)
                self.error_spinboxes[i].setVisible(use_two_values or i == 0)
            self.warning_and_label.setVisible(use_two_values)
            self.error_and_label.setVisible(use_two_values)

        def _get_parameter_constraint_as_class(self) -> "openlifu.plan.ParameterConstraint":
            display_operator = self.operator_selector.currentText
            operator = self.inverse_operator_display_map[display_operator]
            is_range_operator = operator in ['within', 'inside', 'outside', 'outside_inclusive']

            warning_value = (
                (self.warning_spinboxes[0].value, self.warning_spinboxes[1].value)
                if is_range_operator else self.warning_spinboxes[0].value
            )
            error_value = (
                (self.error_spinboxes[0].value, self.error_spinboxes[1].value)
                if is_range_operator else self.error_spinboxes[0].value
            )

            return openlifu_lz().plan.ParameterConstraint(operator, warning_value, error_value)

        def _on_accept(self):
            display_name = self.parameter_name_input.currentText
            parameter_name = self.parameter_key_map[display_name]

            if not parameter_name:
                slicer.util.errorDisplay("Parameter name cannot be empty.", parent=self)
                return
            if parameter_name in self.existing_keys:
                slicer.util.errorDisplay("You cannot define multiple constraints for the same parameter.", parent=self)
                return

            self.accept()

        def customexec_(self):
            returncode = self.exec_()
            if returncode == qt.QDialog.Accepted:
                display_name = self.parameter_name_input.currentText
                parameter_name = self.parameter_key_map[display_name]
                return returncode, parameter_name, self._get_parameter_constraint_as_class()
            return returncode, None, None

    def __init__(self):
        super().__init__(key_name="Parameter", val_name="Parameter Constraint")

    # Override the add dialog to use a special dialog just for the parameter
    # constraint
    def _open_add_dialog(self):
        existing_keys = list(self.to_dict().keys())
        createDlg = self.CreateParameterParameterConstraintDialog(existing_keys)
        returncode, param, param_constraint = createDlg.customexec_()
        if not returncode:
            return

        self._add_row(param, param_constraint)

class OpenLIFUSolutionAnalysisOptionsDefinitionFormWidget(OpenLIFUAbstractDataclassDefinitionFormWidget):
    def __init__(self, parent: Optional[qt.QWidget] = None):
        super().__init__(openlifu_lz().plan.SolutionAnalysisOptions, parent, collapsible_title="Solution Analysis Options")

        # Modify the widget for configuring SolutionAnalysis.param_constraints
        # so that it uses the OpenLIFUParameterConstraintsWidget
        old_param_constraints_dicttablewidget = self._field_widgets['param_constraints']
        new_param_constraints_widget = OpenLIFUParameterConstraintsWidget()

        replace_widget(old_param_constraints_dicttablewidget, new_param_constraints_widget)

        # Update internal mapping
        self._field_widgets['param_constraints'] = new_param_constraints_widget
