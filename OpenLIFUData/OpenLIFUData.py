from pathlib import Path
from typing import Optional, List,Tuple, Dict, Sequence,TYPE_CHECKING

import qt
import vtk
import numpy as np

import slicer
from slicer.i18n import tr as _
from slicer.i18n import translate
from slicer.ScriptedLoadableModule import *
from slicer.util import VTKObservationMixin

from slicer.parameterNodeWrapper import parameterNodeWrapper, parameterNodeSerializer, Serializer, ValidatedSerializer, validators
from slicer import (
    vtkMRMLScriptedModuleNode,
    vtkMRMLScalarVolumeNode,
    vtkMRMLMarkupsFiducialNode,
    vtkMRMLModelNode,
    vtkMRMLTransformNode,
)

import OpenLIFULib
from OpenLIFULib import import_openlifu_with_check as openlifu_lz # "openlifu_lz" stands for "openlifu lazy import"

if TYPE_CHECKING:
    import openlifu # This import is deferred to later runtime, but it is done here for IDE and static analysis purposes

#
# OpenLIFUData
#

class OpenLIFUData(ScriptedLoadableModule):
    """Uses ScriptedLoadableModule base class, available at:
    https://github.com/Slicer/Slicer/blob/main/Base/Python/slicer/ScriptedLoadableModule.py
    """

    def __init__(self, parent):
        ScriptedLoadableModule.__init__(self, parent)
        self.parent.title = _("OpenLIFU Data")  # TODO: make this more human readable by adding spaces
        self.parent.categories = [translate("qSlicerAbstractCoreModule", "OpenLIFU.OpenLIFU Modules")]
        self.parent.dependencies = []  # add here list of module names that this module requires
        self.parent.contributors = ["Ebrahim Ebrahim (Kitware), Sadhana Ravikumar (Kitware), Peter Hollender (Openwater), Sam Horvath (Kitware), Brad Moore (Kitware)"]
        # short description of the module and a link to online module documentation
        # _() function marks text as translatable to other languages
        self.parent.helpText = _(
            "This is the data module of the OpenLIFU extension for focused ultrasound. "
            "More information at <a href=\"https://github.com/OpenwaterHealth/SlicerOpenLIFU\">github.com/OpenwaterHealth/SlicerOpenLIFU</a>."
        )
        # organization, grant, and thanks
        self.parent.acknowledgementText = _(
            "This is part of Openwater's OpenLIFU, an open-source "
            "hardware and software platform for Low Intensity Focused Ultrasound (LIFU) research "
            "and development."
        )

#
# OpenLIFUDataParameterNode
#

# This very thin wrapper around openlifu.Protocol is needed to do our lazy importing of openlifu
# while still providing type annotations that the parameter node wrapper can use.
# If we tried to make openlifu.Protocol directly supported as a type by parameter nodes, we would
# get errors from parameterNodeWrapper as it tries to use typing.get_type_hints. This fails because
# get_type_hints tries to *evaluate* the type annotations like "openlifu.Protocol" possibly before
# the user has installed openlifu, and possibly before the main window widgets exist that would allow
# an install prompt to even show up.
class SlicerOpenLIFUProtocol:
    """Ultrathin wrapper of openlifu.Protocol. This exists so that protocols can have parameter node
    support while we still do lazy-loading of openlifu."""
    def __init__(self, protocol: "openlifu.Protocol"):
        self.protocol = protocol

@parameterNodeSerializer
class OpenLIFUProtocolSerializer(Serializer):
    @staticmethod
    def canSerialize(type_) -> bool:
        """
        Whether the serializer can serialize the given type if it is properly instantiated.
        """
        return type_ == SlicerOpenLIFUProtocol

    @staticmethod
    def create(type_):
        """
        Creates a new serializer object based on the given type. If this class does not support the given type,
        None is returned.
        """
        if OpenLIFUProtocolSerializer.canSerialize(type_):
            # Add custom validators as we need them to the list here. For now just IsInstance.
            return ValidatedSerializer(OpenLIFUProtocolSerializer(), [validators.IsInstance(SlicerOpenLIFUProtocol)])
        return None

    def default(self):
        """
        The default value to use if another default is not specified.
        """
        return SlicerOpenLIFUProtocol(openlifu_lz().Protocol())

    def isIn(self, parameterNode: slicer.vtkMRMLScriptedModuleNode, name: str) -> bool:
        """
        Whether the parameterNode contains a parameter of the given name.
        Note that most implementations can just use parameterNode.HasParameter(name).
        """
        return parameterNode.HasParameter(name)

    def write(self, parameterNode: slicer.vtkMRMLScriptedModuleNode, name: str, value: SlicerOpenLIFUProtocol) -> None:
        """
        Writes the value to the parameterNode under the given name.
        """
        parameterNode.SetParameter(
            name,
            value.protocol.to_json(compact=True)
        )

    def read(self, parameterNode: slicer.vtkMRMLScriptedModuleNode, name: str) -> SlicerOpenLIFUProtocol:
        """
        Reads and returns the value with the given name from the parameterNode.
        """
        json_string = parameterNode.GetParameter(name)
        return SlicerOpenLIFUProtocol(openlifu_lz().Protocol.from_json(json_string))

    def remove(self, parameterNode: slicer.vtkMRMLScriptedModuleNode, name: str) -> None:
        """
        Removes the value of the given name from the parameterNode.
        """
        parameterNode.UnsetParameter(name)

@parameterNodeWrapper
class OpenLIFUDataParameterNode:
    databaseDirectory : Path
    loaded_protocols : "Dict[str,SlicerOpenLIFUProtocol]"

#
# OpenLIFUDataWidget
#


class OpenLIFUDataWidget(ScriptedLoadableModuleWidget, VTKObservationMixin):
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
        uiWidget = slicer.util.loadUI(self.resourcePath("UI/OpenLIFUData.ui"))
        self.layout.addWidget(uiWidget)
        self.ui = slicer.util.childWidgetVariables(uiWidget)

        # Set scene in MRML widgets. Make sure that in Qt designer the top-level qMRMLWidget's
        # "mrmlSceneChanged(vtkMRMLScene*)" signal in is connected to each MRML widget's.
        # "setMRMLScene(vtkMRMLScene*)" slot.
        uiWidget.setMRMLScene(slicer.mrmlScene)

        # Create logic class. Logic implements all computations that should be possible to run
        # in batch mode, without a graphical user interface.
        self.logic = OpenLIFUDataLogic()

        # Connections

        # These connections ensure that we update parameter node when scene is closed
        self.addObserver(slicer.mrmlScene, slicer.mrmlScene.StartCloseEvent, self.onSceneStartClose)
        self.addObserver(slicer.mrmlScene, slicer.mrmlScene.EndCloseEvent, self.onSceneEndClose)

        # Buttons
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

        self.update_sessionLoadButton_enabled()
        self.ui.subjectSessionView.selectionModel().currentChanged.connect(self.update_sessionLoadButton_enabled)

        # Manual object loading UI and the loaded objects view
        self.loadedObjectsItemModel = qt.QStandardItemModel()
        self.loadedObjectsItemModel.setHorizontalHeaderLabels(['Name', 'ID', 'Type'])
        self.ui.loadedObjectsView.setModel(self.loadedObjectsItemModel)
        self.ui.loadedObjectsView.setColumnWidth(0, 200)
        self.ui.loadedObjectsView.setColumnWidth(1, 200)
        self.ui.loadProtocolButton.clicked.connect(self.onLoadProtocolPressed)
        self.ui.loadVolumeButton.clicked.connect(self.onLoadVolumePressed)
        self.ui.loadFiducialsButton.clicked.connect(self.onLoadFiducialsPressed)
        self.addObserver(self.logic.getParameterNode().parameterNode, vtk.vtkCommand.ModifiedEvent, self.onParameterNodeModified)

        # ====================================

        # Make sure parameter node is initialized (needed for module reload)
        self.initializeParameterNode()

    def onLoadDatabaseClicked(self):
        # Clear any items that are already there
        self.subjectSessionItemModel.removeRows(0,self.subjectSessionItemModel.rowCount())

        subject_info = self.logic.load_database(self.ui.databaseDirectoryLineEdit.currentPath)

        for subject_id, subject_name in subject_info:
            subject_row = list(map(
                OpenLIFULib.create_noneditable_QStandardItem,
                [subject_name,subject_id]
            ))
            self.subjectSessionItemModel.appendRow(subject_row)

        self.updateSettingFromParameter('databaseDirectory')

    def itemIsSession(self, index : qt.QModelIndex) -> bool:
        """Whether an item from the subject/session tree view is a session.
        Returns True if it's a session and False if it's a subject."""
        # If this has a parent, then it is a session item rather than a subject item.
        # Otherwise, it is a top-level item, so it must be a subject.
        return index.parent().isValid()

    def update_sessionLoadButton_enabled(self):
        """Update whether the session loading button is enabled based on whether any subject or session is selected."""
        if self.ui.subjectSessionView.currentIndex().isValid():
            self.ui.sessionLoadButton.setEnabled(True)
            if self.itemIsSession(self.ui.subjectSessionView.currentIndex()):
                self.ui.sessionLoadButton.toolTip = 'Load the currently selected session'
            else:
                self.ui.sessionLoadButton.toolTip = 'Query the list of sessions for the currently selected subject'
        else:
            self.ui.sessionLoadButton.setEnabled(False)
            self.ui.sessionLoadButton.toolTip = 'Select a subject or session to load'

    def on_item_double_clicked(self, index : qt.QModelIndex):
        if self.itemIsSession(index):
            session_id = self.subjectSessionItemModel.itemFromIndex(index.siblingAtColumn(1)).text()
            subject_id = self.subjectSessionItemModel.itemFromIndex(index.parent().siblingAtColumn(1)).text()
            self.logic.load_session(subject_id, session_id)
        else: # If the item was a subject:
            subject_id = self.subjectSessionItemModel.itemFromIndex(index.siblingAtColumn(1)).text()
            subject_item : qt.QStandardItem = self.subjectSessionItemModel.itemFromIndex(index.siblingAtColumn(0))
            if subject_item.rowCount() == 0: # If we have not already expanded this subject
                for session_id, session_name in self.logic.get_session_info(subject_id):
                    session_row = list(map(
                        OpenLIFULib.create_noneditable_QStandardItem,
                        [session_name, session_id]
                    ))
                    subject_item.appendRow(session_row)
                self.ui.subjectSessionView.expand(subject_item.index())

        # Make sure parameter node is initialized (needed for module reload)
        self.initializeParameterNode()

    def onLoadProtocolPressed(self) -> None:
        qsettings = qt.QSettings()

        filepath: str = qt.QFileDialog.getOpenFileName(
            slicer.util.mainWindow(), # parent
            'Load protocol', # title of dialog
            qsettings.value('OpenLIFU/databaseDirectory','.'), # starting dir, with default of '.'
            "Protocols (*.json);;All Files (*)", # file type filter
        )
        if filepath:
            self.logic.load_protocol(filepath)

    def onLoadVolumePressed(self) -> None:
        """ Call slicer dialog to load volumes into the scene"""
        return slicer.util.openAddVolumeDialog()
    
    def onLoadFiducialsPressed(self) -> None:
        """ Call slicer dialog to load fiducials into the scene"""

        # Should use "slicer.util.openAddFiducialsDialog()"" to load the Fiducials dialog. This doesn't work because
        # the ioManager functions are bugged - they have not been updated to use the new file type name for Markups.
        # Instead, using a workaround that directly calls the ioManager with the correct file type name for Markups. 
        ioManager = slicer.app.ioManager()
        return ioManager.openDialog("MarkupsFile", slicer.qSlicerFileDialog.Read)


    def updateLoadedObjectsView(self):
        self.loadedObjectsItemModel.removeRows(0,self.loadedObjectsItemModel.rowCount())
        parameter_node = self.logic.getParameterNode()
        for protocol in parameter_node.loaded_protocols.values():
            row = list(map(
                OpenLIFULib.create_noneditable_QStandardItem,
                [protocol.protocol.name, protocol.protocol.id, "Protocol"]
            ))
            self.loadedObjectsItemModel.appendRow(row)

    def onParameterNodeModified(self, caller, event) -> None:
        self.updateLoadedObjectsView()

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

    def setParameterNode(self, inputParameterNode: Optional[OpenLIFUDataParameterNode]) -> None:
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

# OpenLIFUDataLogic
#

class OpenLIFUDataLogic(ScriptedLoadableModuleLogic):
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
        return OpenLIFUDataParameterNode(super().getParameterNode())

    def clear_session(self) -> None:
        self.current_session = None

        for node in [self.volume_node, *self.target_nodes, self.transducer_node, self.transducer_transform_node]:
            if node is not None:
                slicer.mrmlScene.RemoveNode(node)
        self.volume_node = None
        self.target_nodes = []
        self.transducer_node = None
        self.transducer_transform_node = None

    @OpenLIFULib.display_errors
    def load_database(self, path: Path) -> Sequence[Tuple[str,str]]:
        """Load an openlifu database from a local folder hierarchy.

        This sets the internal openlifu database object and reads in all the subjects,
        and returns the subject information.

        Args:
            path: Path to the openlifu database folder on disk.

        Returns: A sequence of pairs (subject_id, subject_name) running over all subjects
            in the database.
        """
        openlifu = OpenLIFULib.import_openlifu_with_check()

        self.clear_session()
        self._subjects = {}

        self.db = openlifu.Database(path)
        OpenLIFULib.add_slicer_log_handler(self.db)

        subject_ids : List[str] = OpenLIFULib.ensure_list(self.db.get_subject_ids())
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
            for session_id in OpenLIFULib.ensure_list(self.db.get_session_ids(subject_id))
        ]

    @OpenLIFULib.display_errors
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

    @OpenLIFULib.display_errors
    def get_session(self, subject_id:str, session_id:str) -> "openlifu.db.session.Session":
        """Fetch the Session with the given ID"""
        return self.db.load_session(self.get_subject(subject_id), session_id)

    @OpenLIFULib.display_errors
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
            position = OpenLIFULib.get_xxx2ras_matrix(target.dims) @ position
            position = OpenLIFULib.get_xx2mm_scale_factor(target.units) * position

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
        openlifu2slicer_matrix = OpenLIFULib.linear_to_affine(OpenLIFULib.get_xxx2ras_matrix('LPS') * OpenLIFULib.get_xx2mm_scale_factor(self.current_session.transducer.units))
        slicer2openlifu_matrix = np.linalg.inv(openlifu2slicer_matrix)
        transform_matrix_numpy = openlifu2slicer_matrix @ self.current_session.transducer.matrix @ slicer2openlifu_matrix

        transform_matrix_vtk = OpenLIFULib.numpy_to_vtk_4x4(transform_matrix_numpy)
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


    @OpenLIFULib.display_errors
    def load_protocol(self, filepath:str):
        protocol = openlifu_lz().Protocol.from_file(filepath)
        if protocol.id in self.getParameterNode().loaded_protocols:
            if not slicer.util.confirmYesNoDisplay(
                f"A protocol with ID {protocol.id} is already loaded. Reload it?",
                "Protocol already loaded",
            ):
                return
        self.getParameterNode().loaded_protocols[protocol.id] = SlicerOpenLIFUProtocol(protocol)

#
# OpenLIFUDataTest
#


class OpenLIFUDataTest(ScriptedLoadableModuleTest):
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