from typing import Optional, List, Sequence, Tuple, Callable, TYPE_CHECKING
from pathlib import Path
import sys
import os
import shutil

import qt

import vtk

import slicer
from slicer.i18n import tr as _
from slicer.i18n import translate
from slicer.ScriptedLoadableModule import *
from slicer.util import VTKObservationMixin

from slicer.parameterNodeWrapper import parameterNodeWrapper
from slicer import (
    vtkMRMLScriptedModuleNode,
)

from OpenLIFULib import (
    openlifu_lz,
)

from OpenLIFULib.util import (
    ensure_list,
    display_errors,
    add_slicer_log_handler_for_openlifu_object,
)

from OpenLIFULib.guided_mode_util import GuidedWorkflowMixin

if TYPE_CHECKING:
    import openlifu # This import is deferred at runtime using openlifu_lz, but it is done here for IDE and static analysis purposes
    import openlifu.db

#
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

        # === Connections and UI setup =======

        self.ui.databaseLoadButton.clicked.connect(self.onLoadDatabaseClicked)
        self.ui.databaseDirectoryLineEdit.findChild(qt.QLineEdit).connect(
            "returnPressed()",
            lambda : self.onLoadDatabaseClicked(checked=True)
        )

        # ====================================
        
        # Make sure parameter node is initialized (needed for module reload)
        self.initializeParameterNode()

        # Call the routine to update from data parameter node
        self.onDataParameterNodeModified()

        # ---- Inject guided mode workflow controls ----

        self.inject_workflow_controls_into_placeholder()

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
        pass

    @display_errors
    def onLoadDatabaseClicked(self, checked:bool):

        subject_info = self.logic.load_database(Path(self.ui.databaseDirectoryLineEdit.currentPath))

        slicer.util.getModuleWidget('OpenLIFUData').updateSubjectSessionSelector(subject_info) # vestigial from previous Datamodule behavior

        self.updateSettingFromParameter('databaseDirectory')

        slicer.util.getModuleWidget('OpenLIFUData').update_newSubjectButton_enabled() # vestigial from previous Datamodule behavior

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
            # ui element that needs connection.
            self._parameterNodeGuiTag = self._parameterNode.connectGui(self.ui)
            self.addObserver(self._parameterNode, vtk.vtkCommand.ModifiedEvent, self.onParameterNodeModified)

    def onParameterNodeModified(self, caller, event) -> None:
        pass

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

    db = None

    def __init__(self) -> None:
        """Called when the logic class is instantiated. Can be used for initializing member variables."""
        ScriptedLoadableModuleLogic.__init__(self)

        self._database_is_loaded : bool = False
        """Whether a database is loaded. Do not set this directly -- use the `database_is_loaded` property."""

        self._on_database_is_loaded_changed_callbacks : List[Callable[[bool],None]] = []
        """List of functions to call when `database_is_loaded` property is changed."""

    def getParameterNode(self):
        return OpenLIFUDatabaseParameterNode(super().getParameterNode())

    def call_on_database_is_loaded_changed(self, f : Callable[[bool],None]) -> None:
        """Set a function to be called whenever the `database_is_loaded` property is changed.
        The provided callback should accept a single bool argument which will be the new database_is_loaded state.
        """
        self._on_database_is_loaded_changed_callbacks.append(f)

    @property
    def database_is_loaded(self) -> bool:
        """Whether database_is_loaded"""
        return self._database_is_loaded

    @database_is_loaded.setter
    def database_is_loaded(self, database_is_loaded_value : bool):
        self._database_is_loaded = database_is_loaded_value
        for f in self._on_database_is_loaded_changed_callbacks:
            f(self._database_is_loaded)

    def load_database(self, path: Path) -> Sequence[Tuple[str,str]]:
        """Load an openlifu database from a local folder hierarchy.

        This sets the internal openlifu database object and reads in all the subjects,
        and returns the subject information.

        Args:
            path: Path to the openlifu database folder on disk.

        Returns: A sequence of pairs (subject_id, subject_name) running over all subjects
            in the database.
        """
        slicer.util.getModuleLogic('OpenLIFUData').clear_session() # from previous implementation
        subjects = {}

        if not self.path_is_openlifu_database_root(path):
            if not slicer.util.confirmYesNoDisplay(
                f"An openlifu database was not found at the entered path ({str(path)}). Do you want to initialize a default one?",
                "Confirm initialize database"
            ):
                self.db = None
                self.database_is_loaded = False
                return list()

            self.copy_preinitialized_database(path)

        self.db = openlifu_lz().Database(path)
        add_slicer_log_handler_for_openlifu_object(self.db)

        subject_ids : List[str] = ensure_list(self.db.get_subject_ids())
        subjects = {
            subject_id : self.db.load_subject(subject_id)
            for subject_id in subject_ids
        }

        slicer.util.getModuleLogic('OpenLIFUData')._subjects = subjects # from previous implementation

        subject_names : List[str] = [subject.name for subject in subjects.values()]

        self.database_is_loaded = True

        return list(zip(subject_ids, subject_names))
    
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
