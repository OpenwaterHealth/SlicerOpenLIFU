import logging
import sys
from pathlib import Path
from typing import Optional, Any, List, Tuple, Sequence, Dict, TYPE_CHECKING
from numpy.typing import NDArray
import importlib
import logging

import vtk
import qt
import numpy as np

import slicer
from slicer.i18n import tr as _
from slicer.i18n import translate
from slicer.ScriptedLoadableModule import *
from slicer.util import VTKObservationMixin
from slicer.parameterNodeWrapper import parameterNodeWrapper
from slicer import (
    vtkMRMLScriptedModuleNode,
    vtkMRMLScalarVolumeNode,
    vtkMRMLMarkupsFiducialNode,
    vtkMRMLModelNode,
    vtkMRMLTransformNode,
)

if TYPE_CHECKING:
    import openlifu # This import is deferred to later runtime, but it is done here for IDE and static analysis purposes
    import openlifu.db

#
# OpenLIFUHome
#


class OpenLIFUHome(ScriptedLoadableModule):
    """Uses ScriptedLoadableModule base class, available at:
    https://github.com/Slicer/Slicer/blob/main/Base/Python/slicer/ScriptedLoadableModule.py
    """

    def __init__(self, parent):
        ScriptedLoadableModule.__init__(self, parent)
        self.parent.title = _("OpenLIFU Home")
        self.parent.categories = [translate("qSlicerAbstractCoreModule", "OpenLIFU")]
        self.parent.dependencies = []  # add here list of module names that this module requires
        self.parent.contributors = ["Ebrahim Ebrahim (Kitware), Peter Hollender (Openwater), Sam Horvath (Kitware), Brad Moore (Kitware)"]
        # short description of the module and a link to online module documentation
        # _() function marks text as translatable to other languages
        self.parent.helpText = _(
            "This is the home module of the OpenLIFU extension for focused ultrasound. "
            "More information at <a href=\"https://github.com/OpenwaterHealth/SlicerOpenLIFU\">github.com/OpenwaterHealth/SlicerOpenLIFU</a>."
        )
        # organization, grant, and thanks
        self.parent.acknowledgementText = _(
            "This is part of Openwater's OpenLIFU, an open-source "
            "hardware and software platform for Low Intensity Focused Ultrasound (LIFU) research "
            "and development."
        )

#
# General utilities that probably should go elsewhere?
# TODO Move some of these functions to more suitable places, e.g. some need to be where all modules can access them.
#

class BusyCursor:
    """
    Context manager for showing a busy cursor.  Ensures that cursor reverts to normal in
    case of an exception.
    """

    def __enter__(self):
        qt.QApplication.setOverrideCursor(qt.Qt.BusyCursor)

    def __exit__(self, exception_type, exception_value, traceback):
        qt.QApplication.restoreOverrideCursor()
        return False

def install_python_requirements() -> None:
    """Install python requirements"""
    requirements_path = Path(__file__).parent / 'Resources/python-requirements.txt'
    with BusyCursor():
        slicer.util.pip_install(['-r', requirements_path])

def python_requirements_exist() -> bool:
    """Check and return whether python requirements are installed."""
    return importlib.util.find_spec('openlifu') is not None

def check_and_install_python_requirements(prompt_if_found = False) -> None:
    """Check whether python requirements are installed and prompt to install them if not.

    Args:
        prompt_if_found: If this is enabled then in the event that python requirements are found,
            there is a further prompt asking whether to run the install anyway.
    """
    want_install = False
    if not python_requirements_exist():
        want_install = slicer.util.confirmYesNoDisplay(
            text = "Some OpenLIFU python dependencies were not found. Install them now?",
            windowTitle = "Install python dependencies?",
        )
    elif prompt_if_found:
        want_install = slicer.util.confirmYesNoDisplay(
            text = "All OpenLIFU python dependencies were found. Re-run the install command?",
            windowTitle = "Reinstall python dependencies?",
        )
    if want_install:
        install_python_requirements()
        if python_requirements_exist():
            slicer.util.infoDisplay(text="Python requirements installed.", windowTitle="Success")
        else:
            slicer.util.errorDisplay(
                text="OpenLIFU python dependencies are still not found. The install may have failed.",
                windowTitle="Python dependencies still not found"
            )

def import_openlifu_with_check() -> "openlifu":
    """Import openlifu and return the module, checking that it is installed along the way."""
    if "openlifu" not in sys.modules:
        check_and_install_python_requirements(prompt_if_found=False)
        with BusyCursor():
            import openlifu
    return sys.modules["openlifu"]

def display_errors(f):
    """Decorator to make functions forward their python exceptions along as slicer error displays"""
    def f_with_forwarded_errors(*args, **kwargs):
        try:
            return f(*args, **kwargs)
        except Exception as e:
            slicer.util.errorDisplay(f'Exception raised in {f.__name__}: {e}')
            raise e
    return f_with_forwarded_errors

class SlicerLogHandler(logging.Handler):
    def __init__(self, name_to_print, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.name_to_print = name_to_print

    def emit(self, record):
        if record.levelno == logging.ERROR:
            method_to_use = self.handle_error
        elif record.levelno == logging.WARNING:
            method_to_use = self.handle_warning
        else: # info or any other unaccounted for log message level
            method_to_use = self.handle_info
        method_to_use(self.format(record))

    def handle_error(self, msg):
        slicer.util.errorDisplay(f"{self.name_to_print}: {msg}")

    def handle_warning(self, msg):
        slicer.util.warningDisplay(f"{self.name_to_print}: {msg}")

    def handle_info(self, msg):
        slicer.util.showStatusMessage(f"{self.name_to_print}: {msg}")

def add_slicer_log_handler(openlifu_object: Any):
    """Adds an appropriately named SlicerLogHandler to the logger of an openlifu object,
    and only doing so if that logger doesn't already have a SlicerLogHandler"""
    if not hasattr(openlifu_object, "logger"):
        raise ValueError("This object does not have a logger attribute.")
    if not hasattr(openlifu_object, "__class__"):
        raise ValueError("This object is not an instance of an openlifu class.")
    logger : logging.Logger = openlifu_object.logger
    if not any(isinstance(h, SlicerLogHandler) for h in logger.handlers):
        handler = SlicerLogHandler(openlifu_object.__class__.__name__)
        logger.addHandler(handler)

# TODO: Fix the matlab weirdness in openlifu so that we can get rid of ensure_list here.
# The reason for ensure_list is to deal with the fact that matlab fails to distinguish
# between a list with one element and the element itself, and so it doesn't write out
# singleton lists properly
def ensure_list(item: Any) -> List[Any]:
    """ Ensure the input is a list. This is a no-op for lists, and returns a singleton list when given non-list input. """
    if isinstance(item, list):
        return item
    else:
        return [item]

def create_noneditable_QStandardItem(text:str) -> qt.QStandardItem:
            item = qt.QStandardItem(text)
            item.setEditable(False)
            return item

def numpy_to_vtk_4x4(numpy_array_4x4 : NDArray[Any]) -> vtk.vtkMatrix4x4:
            if numpy_array_4x4.shape != (4, 4):
                raise ValueError("The input numpy array must be of shape (4, 4).")
            vtk_matrix = vtk.vtkMatrix4x4()
            for i in range(4):
                for j in range(4):
                    vtk_matrix.SetElement(i, j, numpy_array_4x4[i, j])
            return vtk_matrix

directions_in_RAS_coords_dict = {
    'R' : np.array([1,0,0]),
    'A' : np.array([0,1,0]),
    'S' : np.array([0,0,1]),
    'L' : np.array([-1,0,0]),
    'P' : np.array([0,-1,0]),
    'I' : np.array([0,0,-1]),
}

def get_xxx2ras_matrix(dims:Sequence[str]) -> NDArray[Any]:
    return np.array([
        directions_in_RAS_coords_dict[dim] for dim in dims
    ]).transpose()

def get_xx2mm_scale_factor(length_unit:str) -> float:
    openlifu = import_openlifu_with_check()
    return openlifu.util.units.getsiscale(length_unit, 'distance') / openlifu.util.units.getsiscale('mm', 'distance')

def linear_to_affine(matrix, translation=None):
    """Convert linear 3x3 transform to an affine 4x4 with
    the given translation vector (the default being no translation)"""
    if translation is None:
        translation = np.zeros(3)
    if matrix.shape != (3, 3):
        raise ValueError("The input numpy array must be of shape (3, 3).")
    return np.concatenate(
        [
            np.concatenate([matrix,translation.reshape(-1,1)], axis=1),
            np.array([[0,0,0,1]], dtype=float),
        ],
        axis=0,
    )


#
# OpenLIFUHomeParameterNode
#


@parameterNodeWrapper
class OpenLIFUHomeParameterNode:
    """
    The parameters needed by this module.
    """
    databaseDirectory : Path


#
# OpenLIFUHomeWidget
#


class OpenLIFUHomeWidget(ScriptedLoadableModuleWidget, VTKObservationMixin):
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
        uiWidget = slicer.util.loadUI(self.resourcePath("UI/OpenLIFUHome.ui"))
        self.layout.addWidget(uiWidget)
        self.ui = slicer.util.childWidgetVariables(uiWidget)

        # Set scene in MRML widgets. Make sure that in Qt designer the top-level qMRMLWidget's
        # "mrmlSceneChanged(vtkMRMLScene*)" signal in is connected to each MRML widget's.
        # "setMRMLScene(vtkMRMLScene*)" slot.
        uiWidget.setMRMLScene(slicer.mrmlScene)

        # Create logic class. Logic implements all computations that should be possible to run
        # in batch mode, without a graphical user interface.
        self.logic = OpenLIFUHomeLogic()

        # === Connections and UI setup =======

        # These connections ensure that we update parameter node when scene is closed
        self.addObserver(slicer.mrmlScene, slicer.mrmlScene.StartCloseEvent, self.onSceneStartClose)
        self.addObserver(slicer.mrmlScene, slicer.mrmlScene.EndCloseEvent, self.onSceneEndClose)

        self.ui.installPythonReqsButton.connect("clicked()", self.onInstallPythonRequirements)
        self.updateInstallButtonText()

        self.ui.databaseLoadButton.clicked.connect(self.onLoadDatabaseClicked)
        self.ui.databaseDirectoryLineEdit.findChild(qt.QLineEdit).connect("returnPressed()", self.onLoadDatabaseClicked)

        self.subjectSessionItemModel = qt.QStandardItemModel()
        self.subjectSessionItemModel.setHorizontalHeaderLabels(['Name', 'ID'])
        self.ui.subjectSessionView.setModel(self.subjectSessionItemModel)
        self.ui.subjectSessionView.setColumnWidth(0, 200) # make the Name column wider

        self.ui.subjectSessionView.doubleClicked.connect(self.on_item_double_clicked)

        # Selecting an item and clicking sessionLoadButton is equivalent to doubleclicking the item:
        self.ui.sessionLoadButton.clicked.connect(
            lambda : self.on_item_double_clicked(self.ui.subjectSessionView.currentIndex())
        )

        # ====================================

        # Make sure parameter node is initialized (needed for module reload)
        self.initializeParameterNode()

    def onLoadDatabaseClicked(self):
        # Clear any items that are already there
        self.subjectSessionItemModel.removeRows(0,self.subjectSessionItemModel.rowCount())

        subject_info = self.logic.load_database(self.ui.databaseDirectoryLineEdit.currentPath)

        for subject_id, subject_name in subject_info:
            subject_row = list(map(
                create_noneditable_QStandardItem,
                [subject_name,subject_id]
            ))
            self.subjectSessionItemModel.appendRow(subject_row)

        self.updateSettingFromParameter('databaseDirectory')

    def on_item_double_clicked(self, index : qt.QModelIndex):
        if index.parent().isValid(): # If this has a parent, then it is a session item rather than a subject item
            session_id = self.subjectSessionItemModel.itemFromIndex(index.siblingAtColumn(1)).text()
            subject_id = self.subjectSessionItemModel.itemFromIndex(index.parent().siblingAtColumn(1)).text()
            self.logic.load_session(subject_id, session_id)
        else: # This was a top-level item, so it must be a subject
            subject_id = self.subjectSessionItemModel.itemFromIndex(index.siblingAtColumn(1)).text()
            subject_item : qt.QStandardItem = self.subjectSessionItemModel.itemFromIndex(index.siblingAtColumn(0))
            if subject_item.rowCount() == 0: # If we have not already expanded this subject
                for session_id, session_name in self.logic.get_session_info(subject_id):
                    session_row = list(map(
                        create_noneditable_QStandardItem,
                        [session_name, session_id]
                    ))
                    subject_item.appendRow(session_row)



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

    def setParameterNode(self, inputParameterNode: Optional[OpenLIFUHomeParameterNode]) -> None:
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

    def updateInstallButtonText(self) -> None:
        """Update the text of the install button based on whether it's 'install' or 'reinstall'"""
        if python_requirements_exist():
            self.ui.installPythonReqsButton.text = 'Reinstall Python Requirements'
        else:
            self.ui.installPythonReqsButton.text = 'Install Python Requirements'

    def onInstallPythonRequirements(self) -> None:
        """Install python requirements button action"""
        check_and_install_python_requirements(prompt_if_found=True)
        self.updateInstallButtonText()


#
# OpenLIFUHomeLogic
#


class OpenLIFUHomeLogic(ScriptedLoadableModuleLogic):
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

        self.db : Optional[openlifu.Database] = None

        self.current_session : Optional[openlifu.db.session.Session] = None
        self.volume_node: Optional[vtkMRMLScalarVolumeNode] = None
        self.target_nodes: List[vtkMRMLMarkupsFiducialNode] = []
        self.transducer_node: Optional[vtkMRMLModelNode] = None
        self.transducer_transform_node: Optional[vtkMRMLTransformNode] = None

        self._subjects : Dict[str, openlifu.db.subject.Subject] = {} # Mapping from subject id to Subject

    def getParameterNode(self):
        return OpenLIFUHomeParameterNode(super().getParameterNode())

    def clear_session(self) -> None:
        self.current_session = None

        for node in [self.volume_node, *self.target_nodes, self.transducer_node, self.transducer_transform_node]:
            if node is not None:
                slicer.mrmlScene.RemoveNode(node)
        self.volume_node = None
        self.target_nodes = []
        self.transducer_node = None
        self.transducer_transform_node = None

    @display_errors
    def load_database(self, path: Path) -> Sequence[Tuple[str,str]]:
        """Load an openlifu database from a local folder hierarchy.

        This sets the internal openlifu database object and reads in all the subjects,
        and returns the subject information.

        Args:
            path: Path to the openlifu database folder on disk.

        Returns: A sequence of pairs (subject_id, subject_name) running over all subjects
            in the database.
        """
        openlifu = import_openlifu_with_check()

        self.clear_session()
        self._subjects = {}

        self.db = openlifu.Database(path)
        add_slicer_log_handler(self.db)

        subject_ids : List[str] = ensure_list(self.db.get_subject_ids())
        self._subjects = {
            subject_id : self.db.load_subject(subject_id)
            for subject_id in subject_ids
        }

        subject_names = [subject.name for subject in self._subjects.values()]

        return zip(subject_ids, subject_names)

    def get_subject(self, subject_id:str) -> "openlifu.db.subject.Subject":
        """Get the Subject with a given ID"""
        try:
            return self._subjects[subject_id] # use the in-memory Subject if it is in memory
        except KeyError:
            # otherwise attempt to load it:
            return self.db.load_subject(subject_id)

    def get_sessions(self, subject_id:str) -> "List[openlifu.db.session.Session]":
        """Get the collection of Sessions associated with a given subject ID"""
        return [
            self.db.load_session(
                self.get_subject(subject_id),
                session_id
            )
            for session_id in ensure_list(self.db.get_session_ids(subject_id))
        ]

    @display_errors
    def get_session_info(self, subject_id:str) -> Sequence[Tuple[str,str]]:
        """Fetch the session names and IDs for a particular subject.

        This requires that an openlifu database be loaded.

        Args:
            subject_id: ID of the subject for which to query session info.

        Returns: A sequence of pairs (session_id, session_name) running over all sessions
            for the given subject.
        """
        sessions = self.get_sessions(subject_id)
        return [(session.id, session.name) for session in sessions]

    @display_errors
    def get_session(self, subject_id:str, session_id:str) -> "openlifu.db.session.Session":
        """Fetch the Session with the given ID"""
        return self.db.load_session(self.get_subject(subject_id), session_id)

    @display_errors
    def load_session(self, subject_id, session_id):
        self.clear_session()
        self.current_session = self.get_session(subject_id, session_id)
        volume_id = self.current_session.volume_id
        volume_filename_maybe = Path(self.db.get_volume_filename(subject_id, volume_id))
        volume_file_candidates = volume_filename_maybe.parent.glob(
            volume_filename_maybe.name.split('.')[0] + '.*'
        )

        # === Load volume ===

        volume_files = [
            volume_path
            for volume_path in volume_file_candidates
            if slicer.app.coreIOManager().fileType(volume_path) == 'VolumeFile'
        ]
        if len(volume_files) < 1:
            raise FileNotFoundError(f"Could not find a volume file for subject {subject_id}, session {session_id}.")
        if len(volume_files) > 1:
            raise FileNotFoundError(f"Found multiple candidate volume files for subject {subject_id}, session {session_id}.")

        volume_path = volume_files[0]

        self.volume_node = slicer.util.loadVolume(volume_path)
        self.volume_node.SetName(volume_id)

        # === Load targets ===

        for target in self.current_session.targets:

            target_node : vtkMRMLMarkupsFiducialNode = slicer.mrmlScene.AddNewNodeByClass("vtkMRMLMarkupsFiducialNode")
            self.target_nodes.append(target_node)
            target_node.SetName(target.id)

                # Get target position and convert it to Slicer coordinates
            position = np.array(target.position)
            position = get_xxx2ras_matrix(target.dims) @ position
            position = get_xx2mm_scale_factor(target.units) * position

            target_node.SetControlPointLabelFormat(target.name)
            target_display_node = target_node.GetDisplayNode()
            target_display_node.SetSelectedColor(target.color)
            target_node.SetLocked(True)

            target_node.AddControlPoint(
                position
            )

        # === Load transducer ===

        self.transducer_node = slicer.mrmlScene.AddNewNodeByClass("vtkMRMLModelNode")
        self.transducer_node.SetName(self.current_session.transducer.id)
        self.transducer_node.SetAndObservePolyData(self.current_session.transducer.get_polydata())
        self.transducer_transform_node = slicer.mrmlScene.AddNewNodeByClass("vtkMRMLTransformNode")
        self.transducer_transform_node.SetName(f"{self.current_session.transducer.id}-matrix")
        self.transducer_node.SetAndObserveTransformNodeID(self.transducer_transform_node.GetID())

        # TODO: Instead of harcoding 'LPS' here, use something like a "dims" attribute that should be associated with
        # self.current_session.transducer.matrix. There is no such attribute yet but it should exist eventually once this is done:
        # https://github.com/OpenwaterHealth/opw_neuromod_sw/issues/3
        openlifu2slicer_matrix = linear_to_affine(get_xxx2ras_matrix('LPS') * get_xx2mm_scale_factor(self.current_session.transducer.units))
        slicer2openlifu_matrix = np.linalg.inv(openlifu2slicer_matrix)
        transform_matrix_numpy = openlifu2slicer_matrix @ self.current_session.transducer.matrix @ slicer2openlifu_matrix

        transform_matrix_vtk = numpy_to_vtk_4x4(transform_matrix_numpy)
        self.transducer_transform_node.SetMatrixTransformToParent(transform_matrix_vtk)
        self.transducer_node.CreateDefaultDisplayNodes() # toggles the "eyeball" on

        # === Toggle slice visibility and center slices on first target ===

        slices_center_point = self.target_nodes[0].GetNthControlPointPosition(0)
        for slice_node_name in ["Red", "Green", "Yellow"]:
            sliceNode = slicer.util.getFirstNodeByClassByName("vtkMRMLSliceNode", slice_node_name)
            sliceNode.JumpSliceByCentering(*slices_center_point)
            sliceNode.SetSliceVisible(True)
        sliceNode = slicer.util.getFirstNodeByClassByName("vtkMRMLSliceNode", "Green")
        sliceNode.SetSliceVisible(True)
        sliceNode = slicer.util.getFirstNodeByClassByName("vtkMRMLSliceNode", "Yellow")
        sliceNode.SetSliceVisible(True)

#
# OpenLIFUHomeTest
#


class OpenLIFUHomeTest(ScriptedLoadableModuleTest):
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
