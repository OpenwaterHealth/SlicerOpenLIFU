# Standard library imports
import os
import shutil
import sys
from pathlib import Path
from typing import Optional, List, Sequence, Tuple, Callable, TYPE_CHECKING

# Third-party imports
import qt
import vtk

# Slicer imports
import slicer
from slicer import vtkMRMLScriptedModuleNode
from slicer.ScriptedLoadableModule import *
from slicer.i18n import tr as _
from slicer.i18n import translate
from slicer.parameterNodeWrapper import parameterNodeWrapper
from slicer.util import VTKObservationMixin

# OpenLIFULib imports
from OpenLIFULib import openlifu_lz
from OpenLIFULib.guided_mode_util import GuidedWorkflowMixin
from OpenLIFULib.util import (
    ensure_list,
    display_errors,
    add_slicer_log_handler_for_openlifu_object,
)

# These imports are deferred at runtime using openlifu_lz, 
# but are done here for IDE and static analysis purposes
if TYPE_CHECKING:
    import openlifu
    import openlifu.db

# OpenLIFUDatabase
#

class OpenLIFUDatabase(ScriptedLoadableModule):
    """Uses ScriptedLoadableModule base class, available at:
    https://github.com/Slicer/Slicer/blob/main/Base/Python/slicer/ScriptedLoadableModule.py
    """

    def __init__(self, parent):
        ScriptedLoadableModule.__init__(self, parent)
        self.parent.title = _("OpenLIFU Database")  # TODO: make this more human readable by adding spaces
        self.parent.categories = [translate("qSlicerAbstractCoreModule", "OpenLIFU.OpenLIFU Modules")]
        self.parent.dependencies = ["OpenLIFUHome"]  # add here list of module names that this module requires
        self.parent.contributors = ["Andrew Howe (Kitware), Ebrahim Ebrahim (Kitware), Sadhana Ravikumar (Kitware), Peter Hollender (Openwater), Sam Horvath (Kitware), Brad Moore (Kitware)"]
        # short description of the module and a link to online module documentation
        # _() function marks text as translatable to other languages
        self.parent.helpText = _(
            "This is the database module of the OpenLIFU extension for focused ultrasound. "
            "More information at <a href=\"https://github.com/OpenwaterHealth/SlicerOpenLIFU\">github.com/OpenwaterHealth/SlicerOpenLIFU</a>."
        )
        # organization, grant, and thanks
        self.parent.acknowledgementText = _(
            "This is part of Openwater's OpenLIFU, an open-source "
            "hardware and software platform for Low Intensity Focused Ultrasound (LIFU) research "
            "and development."
        )

#
# OpenLIFUDatabaseParameterNode
#

@parameterNodeWrapper
class OpenLIFUDatabaseParameterNode:
    databaseDirectory : Path
    
class OpenLIFUDatabaseWidget(ScriptedLoadableModuleWidget, VTKObservationMixin, GuidedWorkflowMixin):
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
        uiWidget = slicer.util.loadUI(self.resourcePath("UI/OpenLIFUDatabase.ui"))
        self.layout.addWidget(uiWidget)
        self.ui = slicer.util.childWidgetVariables(uiWidget)

        # Set scene in MRML widgets. Make sure that in Qt designer the top-level qMRMLWidget's
        # "mrmlSceneChanged(vtkMRMLScene*)" signal in is connected to each MRML widget's.
        # "setMRMLScene(vtkMRMLScene*)" slot.
        uiWidget.setMRMLScene(slicer.mrmlScene)

        # Create logic class. Logic implements all computations that should be possible to run
        # in batch mode, without a graphical user interface.
        self.logic = OpenLIFUDatabaseLogic()

        # ---- Inject guided mode workflow controls ----

        self.inject_workflow_controls_into_placeholder()

        # === Connections and UI setup =======

        self.logic.call_on_db_changed(self.onDatabaseChanged)

        self.ui.chooseDatabaseLocationButton.clicked.connect(self.on_choose_database_location_clicked)
        self.ui.databaseDirectoryLineEdit.findChild(qt.QLineEdit).connect(
            "returnPressed()",
            lambda : self.onLoadDatabaseClicked(checked=True)
        )
        self.ui.databaseDirectoryLineEdit.currentPathChanged.connect(self.on_database_directory_path_changed)

        # You do not need to connect databaseDirectoryLineEdit
        # currentPathChanged to something that updates the parameter node
        # because the SlicerParameterName dynamic property was given to the
        # ctkPathLineEdit and given a bidirectional connection in
        # self._parameterNode.connectGui(self.ui) (the line edit inside of the
        # widget always matches the parameter node)

        # ====================================
        
        # Make sure parameter node is initialized (needed for module reload)
        self.initializeParameterNode()

        # Call the routine to update from data parameter node
        self.updateWorkflowControls()

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

    @display_errors
    def on_choose_database_location_clicked(self, checked:bool) -> None:
        user_selected_open: bool = self._custom_browse()
        if not user_selected_open:
            return
        else:
            self.onLoadDatabaseClicked(checked=True)

    @display_errors
    def onLoadDatabaseClicked(self, checked:bool):
        path = Path(self.ui.databaseDirectoryLineEdit.currentPath)

        if not self.logic.path_is_openlifu_database_root(path):
            if not slicer.util.confirmYesNoDisplay(
                f"An openlifu database was not found at the entered path ({str(path)}). Do you want to initialize a default one?",
                "Confirm initialize database"
            ):
                self.logic.db = None
                return
            self.logic.copy_preinitialized_database(path)

        self.logic.load_database(path)

    def initializeParameterNode(self) -> None:
        """Ensure parameter node exists and observed."""
        # Parameter node stores all user choices in parameter values, node selections, etc.
        # so that when the scene is saved and reloaded, these settings are restored.

        self.setParameterNode(self.logic.getParameterNode())
        self.updateParametersFromSettings()

    def updateParametersFromSettings(self):
        parameterNode : vtkMRMLScriptedModuleNode = self._parameterNode.parameterNode
        qsettings = qt.QSettings()
        qsettings.beginGroup("OpenLIFU")
        for parameter_name in [
            # List here the parameters that we want to make persistent in the application settings
            "databaseDirectory",
        ]:
            if qsettings.contains(parameter_name):
                parameterNode.SetParameter(
                    parameter_name,
                    qsettings.value(parameter_name)
                )
        qsettings.endGroup()

        # Reset database color if changed from settings
        self.ui.databaseDirectoryLineEdit.findChild(qt.QLineEdit).setStyleSheet("border: none;")

    def updateSettingFromParameter(self, parameter_name:str) -> None:
        parameterNode : vtkMRMLScriptedModuleNode = self._parameterNode.parameterNode
        qsettings = qt.QSettings()
        qsettings.beginGroup("OpenLIFU")
        qsettings.setValue(parameter_name,parameterNode.GetParameter(parameter_name))
        qsettings.endGroup()


    def setParameterNode(self, inputParameterNode: Optional[OpenLIFUDatabaseParameterNode]) -> None:
        """
        Set and observe parameter node.
        Observation is needed because when the parameter node is changed then the GUI must be updated immediately.
        """

        if self._parameterNode:
            self._parameterNode.disconnectGui(self._parameterNodeGuiTag)

        self._parameterNode = inputParameterNode
        if self._parameterNode:
            # Note: in the .ui file, a Qt dynamic property called "SlicerParameterName" is set on each
            # ui element that needs connection. This creates a bidirectional
            # connection between, e.g. the content within the ctk line edit and
            # the parameter node will always update each other
            self._parameterNodeGuiTag = self._parameterNode.connectGui(self.ui)
            self.addObserver(self._parameterNode, vtk.vtkCommand.ModifiedEvent, self.onParameterNodeModified)

    def onParameterNodeModified(self, caller, event) -> None:
        # Update QSettings from changed parameter node
        for parameter_name in [
            "databaseDirectory",
        ]:
            self.updateSettingFromParameter(parameter_name)

        # Update workflow controls
        self.updateWorkflowControls()

    def onDatabaseChanged(self, db: Optional["openlifu.db.Database"] = None):
        self.updateWorkflowControls()
        if db is not None:
            self.ui.databaseDirectoryLineEdit.findChild(qt.QLineEdit).setStyleSheet("border: 1px solid green;")

    def updateWorkflowControls(self):
        if self.logic.db is None:
            self.workflow_controls.can_proceed = False
            self.workflow_controls.status_text = "Connect a database to proceed."
        else:
            self.workflow_controls.can_proceed = True
            self.workflow_controls.status_text = "Database connected, proceed to the next step."

    def on_database_directory_path_changed(self, new_path: str):
        """Called every time the ctkPathLineEdit is changed, even a single
        character. Note: focus only affects border when the line edit is
        actively selected!"""
        self.ui.databaseDirectoryLineEdit.findChild(qt.QLineEdit).setStyleSheet("border: 1px solid yellow;")

    def _custom_browse(self) -> bool:
        """
        Custom directory selection handler to replace the default browse()
        behavior of ctkPathLineEdit. It opens a directory selection dialog and
        sets the selected path in the ctkPathLineEdit widget.

        This function is needed because the ctkPathLineEdit browse() does not
        return whether the user selected "Open" or "Cancel", but this
        information is required if we want automatic database loading upon
        directory selection.

        Returns:
            True if the user selected a directory (clicked Open),
            False if the user canceled the dialog.
        """
        previous_path: str = self.ui.databaseDirectoryLineEdit.currentPath or "."

        selected_path: str = qt.QFileDialog.getExistingDirectory(
            self.ui.databaseDirectoryLineEdit,
            "Select a directory...",
            previous_path,
            qt.QFileDialog.ShowDirsOnly
        )

        if selected_path:
            self.ui.databaseDirectoryLineEdit.setCurrentPath(selected_path)
            return True  # User clicked Open
        return False  # User clicked Cancel

# OpenLIFUDatabaseLogic
#

class OpenLIFUDatabaseLogic(ScriptedLoadableModuleLogic):
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

        self._db = None
        """The loaded database. Do not set this directly -- use the `db` property."""

        self._on_db_changed_callbacks : List[Callable[[Optional["openlifu.db.Database"]],None]] = []
        """List of functions to call when `database_is_loaded` property is changed."""

    def getParameterNode(self):
        return OpenLIFUDatabaseParameterNode(super().getParameterNode())

    def call_on_db_changed(self, f : Callable[[Optional["openlifu.db.Database"]],None]) -> None:
        """Set a function to be called whenever the `database_is_loaded` property is changed.
        The provided callback should accept a single bool argument which will be the new database_is_loaded state.
        """
        self._on_db_changed_callbacks.append(f)

    @property
    def db(self) -> Optional["openlifu.db.Database"]:
        """The currently loaded db"""
        return self._db

    @db.setter
    def db(self, db_value : Optional["openlifu.db.Database"]):
        self._db = db_value
        for f in self._on_db_changed_callbacks:
            f(self._db)

    def load_database(self, path: Path) -> None:
        """Load an openlifu database from a local folder hierarchy.

        Args:
            path: Path to the openlifu database folder on disk.
        """
        self.db = openlifu_lz().Database(path)
        add_slicer_log_handler_for_openlifu_object(self.db)
    
    @staticmethod
    def get_database_destination():
        if sys.platform.startswith("win"):
            return Path(os.environ["APPDATA"]) / "OpenLIFU-app" / "db"
        elif sys.platform.startswith("darwin"):
            return Path.home() / "Library" / "Application Support" / "OpenLIFU-app" / "db"
        elif sys.platform.startswith("linux"):
            return Path.home() / ".local" / "share" / "OpenLIFU-app" / "db"
        else:
            raise NotImplementedError("Unsupported platform")

    @staticmethod
    def path_is_openlifu_database_root(path: Path) -> bool:
        """
        Check if the given path is the root of a valid OpenLIFU ad-hoc database.

        Returns True if the required directory and file structure exists, otherwise False.
        """
        if not path.is_dir():
            return False

        required_structure = {
            "protocols/protocols.json",
            "subjects/subjects.json",
            "transducers/transducers.json",
            "users/users.json",
            "systems"
        }

        for relative_path in required_structure:
            if not (path / relative_path).exists():
                return False

        return True

    @staticmethod
    def copy_preinitialized_database(destination):
        destination = Path(destination)
        db_source = Path(slicer.util.getModuleWidget('OpenLIFUDatabase').resourcePath(os.path.join("openlifu-database", "empty_db")))

        destination.mkdir(parents=True, exist_ok=True)

        copied_paths = []

        for root, dirs, files in os.walk(db_source):
            
            rel_root = Path(root).relative_to(db_source) # Compute path relative to the source base directory
            dest_root = destination / rel_root # Target directory to copy files into

            dest_root.mkdir(exist_ok=True)

            for file in files:
                src_file = Path(root) / file
                dest_file = dest_root / file

                shutil.copy2(src_file, dest_file) # Copy file with metadata (preserves timestamps and permissions)
                copied_paths.append(dest_file)

        # Set permissions only on **newly copied files** (in case they existed)
        if os.name == "nt":
            for path in copied_paths:
                os.system(f'icacls "{path}" /grant Everyone:F /C')
        else:
            for path in copied_paths:
                os.chmod(path, 0o644 if path.is_file() else 0o755)
