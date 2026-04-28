# Standard library imports
import json
import os
import shutil
import sys
import tempfile
import time
import zipfile
import requests
from pathlib import Path
from typing import Optional, List, Callable, TYPE_CHECKING

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
from OpenLIFULib import sample_data
from OpenLIFULib import sample_data_gui
from OpenLIFULib import openlifu_lz
from OpenLIFULib.guided_mode_util import GuidedWorkflowMixin
from OpenLIFULib.sample_data_gui import (
    InitializationResult,
    SampleDatabaseSetupController,
    initialize_missing_database,
)
from OpenLIFULib.util import (
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
        self.sampleDatabaseSetupController = None

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
        self.sampleDatabaseSetupController = SampleDatabaseSetupController(
            parent=self.parent,
            path_line_edit=self.ui.databaseDirectoryLineEdit,
            controls=[
                self.ui.connectDatabaseButton,
                self.ui.chooseDatabaseLocationButton,
            ],
            clear_database=lambda: setattr(self.logic, "db", None),
            load_database=self.logic.load_database,
        )

        self.ui.chooseDatabaseLocationButton.clicked.connect(self.on_choose_database_location_clicked)
        self.ui.databaseDirectoryLineEdit.findChild(qt.QLineEdit).connect(
            "returnPressed()",
            lambda : self.onLoadDatabaseClicked(checked=True)
        )
        self.ui.connectDatabaseButton.clicked.connect(self.onLoadDatabaseClicked)
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
        self.updateAll()

    def cleanup(self) -> None:
        """Called when the application closes and the module widget is destroyed."""
        if self.sampleDatabaseSetupController is not None:
            self.sampleDatabaseSetupController.cleanup()
        self.removeObservers()

    def enter(self) -> None:
        """Called each time the user opens this module."""
        # Make sure parameter node exists and observed
        self.initializeParameterNode()
        self.updateAll()

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
            initialization_result = initialize_missing_database(
                parent=self.parent,
                path=path,
                create_empty_database=self.logic.copy_preinitialized_database,
                setup_controller=self.sampleDatabaseSetupController,
            )
            if initialization_result == InitializationResult.CANCELED:
                self.logic.db = None
                self.ui.databaseDirectoryLineEdit.findChild(qt.QLineEdit).setStyleSheet("border: 1px solid red;")
                return
            if initialization_result == InitializationResult.ASYNC_STARTED:
                return

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

        # Update UI state
        self.updateAll()

    def onDatabaseChanged(self, db: Optional["openlifu.db.Database"] = None):
        self.updateAll()
        if db is not None:
            self.ui.databaseDirectoryLineEdit.findChild(qt.QLineEdit).setStyleSheet("border: 1px solid green;")

    def updateDatabaseConnectedStateLabel(self):
        if self.logic.db is None:
            self.ui.databaseConnectedStateLabel.text = "🔴 Database (not connected)"
        else:
            self.ui.databaseConnectedStateLabel.text = "🟢 Database (connected)"

    def updateWorkflowControls(self):
        if self.logic.db is None:
            self.workflow_controls.can_proceed = False
            self.workflow_controls.status_text = "Connect a database to proceed."
        else:
            self.workflow_controls.can_proceed = True
            self.workflow_controls.status_text = "Database connected, proceed to the next step."

    def updateAll(self):
        self.updateDatabaseConnectedStateLabel()
        self.updateWorkflowControls()

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
        self.db = openlifu_lz().db.Database(path)
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
        return sample_data.path_is_openlifu_database_root(path)

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

    def performSync(self):
        x= requests.get(f'https://api.nvpsoftware.com/users/{slicer.util.getModuleLogic("OpenLIFULogin").getUserId()}', headers={'Authorization': f'Bearer {slicer.util.getModuleLogic("OpenLIFULogin").getValidToken()}'})
        print(x.json())

#
# OpenLIFUDatabaseTest
#


class OpenLIFUDatabaseTest(ScriptedLoadableModuleTest):
    """
    This is the test case for your scripted module.
    Uses ScriptedLoadableModuleTest base class, available at:
    https://github.com/Slicer/Slicer/blob/main/Base/Python/slicer/ScriptedLoadableModule.py
    """

    def connect_database(self, database_dir: str):

        from OpenLIFULib import get_cur_db

        slicer.util.selectModule("OpenLIFUDatabase")
        dbw = slicer.modules.OpenLIFUDatabaseWidget
        dbw.ui.databaseDirectoryLineEdit.currentPath = database_dir
        dbw.onLoadDatabaseClicked(True) 
        
        slicer.app.processEvents()

        curr_db = get_cur_db()
        assert curr_db is not None, "Database failed to load"

    def _write_minimal_database_fixture(
        self,
        database_root: Path,
        transducer_ids: Optional[List[str]] = None,
    ) -> None:
        transducer_ids = transducer_ids if transducer_ids is not None else []
        index_files = {
            "protocols/protocols.json": {"protocol_ids": []},
            "subjects/subjects.json": {"subject_ids": []},
            "transducers/transducers.json": {"transducer_ids": transducer_ids},
            "users/users.json": {"user_ids": []},
        }
        for relative_path, contents in index_files.items():
            index_file = database_root / relative_path
            index_file.parent.mkdir(parents=True, exist_ok=True)
            index_file.write_text(json.dumps(contents), encoding="utf-8")

    def _write_database_archive(self, database_root: Path, archive_path: Path) -> None:
        with zipfile.ZipFile(archive_path, "w") as archive:
            for source_file in database_root.rglob("*"):
                if source_file.is_file():
                    archive.write(source_file, source_file.relative_to(database_root.parent))

    def _process_events_until(self, condition: Callable[[], bool], timeout_seconds: float = 10.0) -> bool:
        deadline = time.monotonic() + timeout_seconds
        while time.monotonic() < deadline:
            slicer.app.processEvents()
            if condition():
                return True
            qt.QThread.msleep(10)
        slicer.app.processEvents()
        return condition()

    def _python_slicer_or_skip(self) -> str:
        python_slicer = shutil.which("PythonSlicer")
        if python_slicer is None:
            self.skipTest("PythonSlicer was not found.")
        return python_slicer

    def _run_python_slicer_script(self, script_path: Path, args: List[str], timeout_seconds: float = 120.0):
        import subprocess

        proc = slicer.util.launchConsoleProcess(
            [self._python_slicer_or_skip(), str(script_path), *args],
            useStartupEnvironment=False,
        )
        try:
            stdout, stderr = proc.communicate(timeout=timeout_seconds)
        except subprocess.TimeoutExpired:
            proc.kill()
            stdout, stderr = proc.communicate()
            self.fail(
                f"PythonSlicer process timed out after {timeout_seconds} seconds.\n"
                f"stdout:\n{stdout or ''}\n"
                f"stderr:\n{stderr or ''}"
            )
        return proc.returncode, stdout or "", stderr or ""

    def _make_sample_database_setup_controller_for_test(self):
        path_widget = qt.QWidget()
        qt.QLineEdit(path_widget)
        controls = [qt.QPushButton(), qt.QPushButton()]
        state = {"db": object(), "loaded_paths": []}

        controller = sample_data_gui.SampleDatabaseSetupController(
            parent=None,
            path_line_edit=path_widget,
            controls=controls,
            clear_database=lambda: state.__setitem__("db", None),
            load_database=lambda path: state["loaded_paths"].append(Path(path)),
            suppress_dialogs_for_testing=True,
        )
        return controller, state

    def test_copy_sample_database_from_archive_with_local_archive_and_work_dir(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_dir_path = Path(temp_dir)
            source_database = temp_dir_path / "source-database"
            archive_path = temp_dir_path / "source-database.zip"
            destination = temp_dir_path / "destination"
            destination.mkdir()
            work_dir = temp_dir_path / "work"
            progress_updates = []

            self._write_minimal_database_fixture(
                source_database,
                transducer_ids=["openlifu_2x400_evt1"],
            )
            self._write_database_archive(source_database, archive_path)

            sample_data.copy_sample_database_from_archive(
                destination,
                progress_callback=lambda message, value, maximum: progress_updates.append((message, value, maximum)),
                archive_url=archive_path.as_uri(),
                work_dir=work_dir,
            )

            self.assertTrue(OpenLIFUDatabaseLogic.path_is_openlifu_database_root(destination))
            self.assertTrue(any("Downloading" in update[0] for update in progress_updates))
            self.assertTrue(any("Installing" in update[0] for update in progress_updates))
            logic = OpenLIFUDatabaseLogic()
            logic.load_database(destination)
            self.assertIn("openlifu_2x400_evt1", logic.db.get_transducer_ids())

    def test_copy_sample_database_rejects_non_empty_destination_before_download(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_dir_path = Path(temp_dir)
            destination = temp_dir_path / "destination"
            destination.mkdir()
            (destination / "notes.txt").write_text("not an OpenLIFU database\n", encoding="utf-8")
            work_dir = temp_dir_path / "work"
            download_calls = []

            original_download = sample_data.download_and_extract_archive_with_progress

            def fail_if_download_starts(*args, **kwargs):
                download_calls.append((args, kwargs))
                raise AssertionError("Download should not start for a non-empty destination.")

            sample_data.download_and_extract_archive_with_progress = fail_if_download_starts
            try:
                with self.assertRaisesRegex(RuntimeError, "selected folder is not empty"):
                    sample_data.copy_sample_database_from_archive(
                        destination,
                        archive_url="file:///unused-sample-database.zip",
                        work_dir=work_dir,
                    )
            finally:
                sample_data.download_and_extract_archive_with_progress = original_download

            self.assertEqual([], download_calls)
            self.assertFalse(work_dir.exists())

    def test_sample_data_cli_installs_local_archive_with_python_slicer(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_dir_path = Path(temp_dir)
            source_database = temp_dir_path / "source-database"
            archive_path = temp_dir_path / "source-database.zip"
            destination = temp_dir_path / "destination"
            destination.mkdir()
            work_dir = temp_dir_path / "work"

            self._write_minimal_database_fixture(
                source_database,
                transducer_ids=["openlifu_2x400_evt1"],
            )
            self._write_database_archive(source_database, archive_path)

            returncode, stdout, stderr = self._run_python_slicer_script(
                sample_data_gui.sample_database_cli_path(),
                [
                    "--destination",
                    str(destination),
                    "--work-dir",
                    str(work_dir),
                    "--archive-url",
                    archive_path.as_uri(),
                ],
            )

            self.assertEqual(
                0,
                returncode,
                msg=f"stdout:\n{stdout}\nstderr:\n{stderr}",
            )
            progress_events = [
                json.loads(line[len(sample_data.PROGRESS_LINE_PREFIX):])
                for line in stdout.splitlines()
                if line.startswith(sample_data.PROGRESS_LINE_PREFIX)
            ]
            self.assertTrue(progress_events)
            self.assertTrue(progress_events[-1].get("success"))
            self.assertTrue(OpenLIFUDatabaseLogic.path_is_openlifu_database_root(destination))

    def test_failed_sample_database_subprocess_leaves_destination_empty_and_clears_db(self):
        self._python_slicer_or_skip()
        controller, state = self._make_sample_database_setup_controller_for_test()

        try:
            with tempfile.TemporaryDirectory() as temp_dir:
                temp_dir_path = Path(temp_dir)
                source_database = temp_dir_path / "source-database"
                archive_path = temp_dir_path / "source-database.zip"
                destination = temp_dir_path / "destination"
                destination.mkdir()

                self._write_minimal_database_fixture(source_database)
                (source_database / "users" / "users.json").unlink()
                self._write_database_archive(source_database, archive_path)

                self.assertTrue(
                    controller.start_sample_database_setup(destination, archive_url=archive_path.as_uri())
                )
                self.assertTrue(
                    self._process_events_until(lambda: not controller.is_active(), timeout_seconds=30),
                    "Sample database setup subprocess did not finish.",
                )
                self.assertIsNone(state["db"])
                self.assertEqual([], state["loaded_paths"])
                self.assertEqual([], list(destination.iterdir()))
                self.assertTrue(controller.path_line_edit.enabled)
                self.assertTrue(all(control.enabled for control in controller.controls))
        finally:
            controller.cleanup()

    def test_sample_database_setup_rejects_non_empty_destination_before_subprocess(self):
        controller, state = self._make_sample_database_setup_controller_for_test()

        try:
            with tempfile.TemporaryDirectory() as temp_dir:
                temp_dir_path = Path(temp_dir)
                destination = temp_dir_path / "destination"
                destination.mkdir()
                (destination / "notes.txt").write_text("not an OpenLIFU database\n", encoding="utf-8")

                self.assertFalse(controller.start_sample_database_setup(destination))
                self.assertFalse(controller.is_active())
                self.assertIsNone(controller._destination)
                self.assertIsNone(controller._work_dir)
                self.assertIsNone(state["db"])
                self.assertEqual([], state["loaded_paths"])
                self.assertTrue(controller.path_line_edit.enabled)
                self.assertTrue(all(control.enabled for control in controller.controls))
        finally:
            controller.cleanup()

    def test_cancel_sample_database_subprocess_leaves_destination_empty_and_clears_db(self):
        self._python_slicer_or_skip()
        controller, state = self._make_sample_database_setup_controller_for_test()

        try:
            with tempfile.TemporaryDirectory() as temp_dir:
                temp_dir_path = Path(temp_dir)
                destination = temp_dir_path / "destination"
                destination.mkdir()
                progress_payload = json.dumps(
                    {
                        "message": "Waiting for cancellation...",
                        "value": 0,
                        "maximum": 0,
                    }
                )
                slow_cli = temp_dir_path / "slow_sample_data_cli.py"
                slow_cli.write_text(
                    f"import argparse\n"
                    f"import time\n"
                    f"from pathlib import Path\n"
                    f"parser = argparse.ArgumentParser()\n"
                    f"parser.add_argument('--destination')\n"
                    f"parser.add_argument('--work-dir')\n"
                    f"parser.add_argument('--cancel-file', default='')\n"
                    f"args = parser.parse_args()\n"
                    f"print({(sample_data.PROGRESS_LINE_PREFIX + progress_payload)!r}, flush=True)\n"
                    f"deadline = time.monotonic() + 30\n"
                    f"while time.monotonic() < deadline:\n"
                    f"    if args.cancel_file and Path(args.cancel_file).exists():\n"
                    f"        break\n"
                    f"    time.sleep(0.05)\n",
                    encoding="utf-8",
                )

                self.assertTrue(controller.start_sample_database_setup(destination, cli_path=slow_cli))
                self.assertTrue(
                    self._process_events_until(lambda: controller.is_active(), timeout_seconds=10),
                    "Sample database setup subprocess did not start.",
                )
                self.assertFalse(controller.path_line_edit.enabled)
                self.assertTrue(all(not control.enabled for control in controller.controls))

                controller.cancel_sample_database_setup()
                self.assertFalse(controller.is_active())
                self.assertTrue(controller.path_line_edit.enabled)
                self.assertTrue(all(control.enabled for control in controller.controls))
                self.assertTrue(
                    self._process_events_until(
                        lambda: controller._background_canceled_process is None,
                        timeout_seconds=10,
                    ),
                    "Canceled sample database setup subprocess did not exit after cancellation.",
                )
                self.assertIsNone(state["db"])
                self.assertEqual([], state["loaded_paths"])
                self.assertEqual([], list(destination.iterdir()))
        finally:
            controller.cleanup()

    def test_move_sample_database_rejects_git_lfs_pointer_file(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_dir_path = Path(temp_dir)
            sample_database = temp_dir_path / "sample-database"
            destination = temp_dir_path / "destination"
            destination.mkdir()
            self._write_minimal_database_fixture(sample_database)
            pointer_file = sample_database / "subjects" / "unresolved-volume.nii.gz"
            pointer_file.write_text(
                "version https://git-lfs.github.com/spec/v1\n"
                "oid sha256:0000000000000000000000000000000000000000000000000000000000000000\n"
                "size 123\n",
                encoding="utf-8",
            )

            with self.assertRaisesRegex(RuntimeError, "Git LFS pointer"):
                sample_data.move_sample_database_into_place(sample_database, destination)

            self.assertEqual([], list(destination.iterdir()))

    def test_move_sample_database_rejects_missing_required_index_file(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_dir_path = Path(temp_dir)
            sample_database = temp_dir_path / "sample-database"
            destination = temp_dir_path / "destination"
            destination.mkdir()
            self._write_minimal_database_fixture(sample_database)
            (sample_database / "users" / "users.json").unlink()

            with self.assertRaisesRegex(RuntimeError, "missing required database index files"):
                sample_data.move_sample_database_into_place(sample_database, destination)

            self.assertEqual([], list(destination.iterdir()))

    def test_copy_preinitialized_database_still_creates_empty_database(self):
        slicer.util.selectModule("OpenLIFUDatabase")
        with tempfile.TemporaryDirectory() as temp_dir:
            destination = Path(temp_dir) / "empty-database"

            OpenLIFUDatabaseLogic.copy_preinitialized_database(destination)

            self.assertTrue(OpenLIFUDatabaseLogic.path_is_openlifu_database_root(destination))
            logic = OpenLIFUDatabaseLogic()
            logic.load_database(destination)
            self.assertIsNotNone(logic.db)
            self.assertEqual([], logic.db.get_transducer_ids())
