from pathlib import Path
from typing import Optional, List,Tuple, Dict, Sequence,TYPE_CHECKING
import json

import qt
import ctk

import vtk
import numpy as np

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
    SlicerOpenLIFUProtocol,
    SlicerOpenLIFUTransducer,
    SlicerOpenLIFUPlan,
    SlicerOpenLIFUSession,
    get_target_candidates,
    assign_openlifu_metadata_to_volume_node,
)
from OpenLIFULib.util import (
    display_errors,
    create_noneditable_QStandardItem,
    ensure_list,
    add_slicer_log_handler,
)

if TYPE_CHECKING:
    import openlifu # This import is deferred at runtime using openlifu_lz, but it is done here for IDE and static analysis purposes
    import openlifu.db

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

@parameterNodeWrapper
class OpenLIFUDataParameterNode:
    databaseDirectory : Path
    loaded_protocols : "Dict[str,SlicerOpenLIFUProtocol]"
    loaded_transducers : "Dict[str,SlicerOpenLIFUTransducer]"
    loaded_plan : "Optional[SlicerOpenLIFUPlan]"
    loaded_session : "Optional[SlicerOpenLIFUSession]"


class AddNewVolumeDialog(qt.QDialog):
    """ Add new volume dialog """

    def __init__(self, parent="mainWindow"):
        super().__init__(slicer.util.mainWindow() if parent == "mainWindow" else parent)
        self.setWindowTitle("Add New Volume")
        self.setWindowModality(1)
        self.setup()

    def setup(self):

        self.setMinimumWidth(400)

        formLayout = qt.QFormLayout()
        self.setLayout(formLayout)

        self.volumeFilePath = ctk.ctkPathLineEdit()
        self.volumeFilePath.filters = ctk.ctkPathLineEdit.Files
        
        # Allowable volume filetypes
        self.volume_extensions = ("Volume" + " (*.hdr *.nhdr *.nrrd *.mhd *.mha *.mnc *.nii *.nii.gz *.mgh *.mgz *.mgh.gz *.img *.img.gz *.pic);;" +
        "Dicom" + " (*.dcm *.ima);;" + 
        "All Files" + " (*)")
        self.volumeFilePath.nameFilters = [self.volume_extensions]

        self.volumeFilePath.currentPathChanged.connect(self.updateVolumeDetails)

        formLayout.addRow(_("Filepath:"), self.volumeFilePath)

        self.volumeName = qt.QLineEdit()
        formLayout.addRow(_("Volume Name:"), self.volumeName)

        self.volumeID = qt.QLineEdit()
        formLayout.addRow(_("Volume ID:"), self.volumeID)

        self.buttonBox = qt.QDialogButtonBox()
        self.buttonBox.setStandardButtons(qt.QDialogButtonBox.Ok |
                                          qt.QDialogButtonBox.Cancel)
        formLayout.addWidget(self.buttonBox)

        self.buttonBox.rejected.connect(self.reject)
        self.buttonBox.accepted.connect(self.validateInputs)

    def updateVolumeDetails(self):
        current_filepath = Path(self.volumeFilePath.currentPath)
        if current_filepath.is_file():
            while current_filepath.suffix:
                current_filepath = current_filepath.with_suffix('')
            volume_name = current_filepath.stem
            if not len(self.volumeName.text):
                self.volumeName.setText(volume_name)
            if not len(self.volumeID.text): 
                self.volumeID.setText(volume_name)

    def validateInputs(self):

        volume_name = self.volumeName.text
        volume_id = self.volumeID.text
        volume_filepath = self.volumeFilePath.currentPath

        if not len(volume_name) or not len(volume_id) or not len(volume_filepath):
            slicer.util.errorDisplay("Required fields are missing", parent = self)
        elif not slicer.app.coreIOManager().fileType(volume_filepath) == 'VolumeFile':
            slicer.util.errorDisplay("Invalid volume filetype specified", parent = self)
        else:
            self.accept()
                               
    def customexec_(self):

        returncode = self.exec_()
        volume_name = self.volumeName.text
        volume_id = self.volumeID.text
        volume_filepath = self.volumeFilePath.currentPath

        return (returncode, volume_filepath,volume_name, volume_id)
    
class AddNewSubjectDialog(qt.QDialog):
    """ Add new subject dialog """

    def __init__(self, parent="mainWindow"):
        super().__init__(slicer.util.mainWindow() if parent == "mainWindow" else parent)
        self.setWindowTitle("Add New Subject")
        self.setWindowModality(1)
        self.setup()

    def setup(self):

        self.setMinimumWidth(200)

        formLayout = qt.QFormLayout()
        self.setLayout(formLayout)

        self.subjectName = qt.QLineEdit()
        formLayout.addRow(_("Subject Name:"), self.subjectName)

        self.subjectID = qt.QLineEdit()
        formLayout.addRow(_("Subject ID:"), self.subjectID)

        self.buttonBox = qt.QDialogButtonBox()
        self.buttonBox.setStandardButtons(qt.QDialogButtonBox.Ok |
                                          qt.QDialogButtonBox.Cancel)
        formLayout.addWidget(self.buttonBox)

        self.buttonBox.rejected.connect(self.reject)
        self.buttonBox.accepted.connect(self.accept)


    def customexec_(self):

        returncode = self.exec_()
        subject_name = self.subjectName.text
        subject_id = self.subjectID.text

        return (returncode, subject_name, subject_id)

class ObjectBeingUnloadedMessageBox(qt.QMessageBox):
    """Warning box for when an object is about to be or has been unloaded"""

    def __init__(self, message:str, title:Optional[str] = None, parent="mainWindow", checkbox_tooltip:Optional[str] = None):
        """Args:
            message: The message to display
            title: Dialog window title
            parent: Parent QWidget, or just mainWindow to just use the Slicer main window as parent.
            checkbox_tooltip: Optional tooltip to elaborate on what "clear affiliated data" would do
        """
        super().__init__(slicer.util.mainWindow() if parent == "mainWindow" else parent)
        self.setWindowTitle(title if title is not None else "Object removed")
        self.setIcon(qt.QMessageBox.Warning)
        self.setText(message)
        self.checkbox = qt.QCheckBox("Clear affiliated data from the scene")
        self.checkbox.setChecked(False) # By default we leave session-affiliated data in the scene
        if checkbox_tooltip is not None:
            self.checkbox.setToolTip(checkbox_tooltip)
        self.setCheckBox(self.checkbox)
        self.addButton(qt.QMessageBox.Ok)

    def customexec_(self) -> bool:
        """Show the dialog (blocking) and once it's closed return whether the checkbox was checked
        (i.e. whether the user has opted to clear session-affiliated data from the scene)"""
        self.exec_()
        checkbox_checked = self.checkbox.isChecked()
        return checkbox_checked

def sessionInvalidatedDialogDisplay(message:str) -> bool:
    """Display a warning dialog for when the active session has been invalidate, showing the specified message"""
    return ObjectBeingUnloadedMessageBox(
        message = message,
        title = "Session invalidated",
        checkbox_tooltip = "Unloads the volume and transducer affiliated with this session."
    ).customexec_()


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

        # This ensures that we properly handle SlicerOpenLIFU objects that become invalid when their nodes are deleted
        self.addObserver(slicer.mrmlScene, slicer.vtkMRMLScene.NodeAboutToBeRemovedEvent, self.onNodeAboutToBeRemoved)
        self.addObserver(slicer.mrmlScene, slicer.vtkMRMLScene.NodeAddedEvent, self.onNodeAdded)
        self.addObserver(slicer.mrmlScene, slicer.vtkMRMLScene.NodeRemovedEvent, self.onNodeRemoved)

        # Buttons
        self.ui.databaseLoadButton.clicked.connect(self.onLoadDatabaseClicked)
        self.ui.databaseDirectoryLineEdit.findChild(qt.QLineEdit).connect(
            "returnPressed()",
            lambda : self.onLoadDatabaseClicked(checked=True)
        )

        # Add new subject
        self.ui.newSubjectButton.clicked.connect(self.onAddNewSubjectClicked)
        self.update_newSubjectButton_enabled()

        # Add new volume to subject
        self.ui.addVolumeToSubjectButton.clicked.connect(self.onAddVolumeToSubjectClicked)
        self.update_addVolumeToSubjectButton_enabled()

        self.subjectSessionItemModel = qt.QStandardItemModel()
        self.subjectSessionItemModel.setHorizontalHeaderLabels(['Name', 'ID'])
        self.ui.subjectSessionView.setModel(self.subjectSessionItemModel)
        self.ui.subjectSessionView.setColumnWidth(0, 200) # make the Name column wider

        self.ui.subjectSessionView.doubleClicked.connect(self.on_item_double_clicked)

        # If a subject is clicked or double clicked, the add volume to subject button should be enabled
        self.ui.subjectSessionView.selectionModel().selectionChanged.connect(self.onSubjectSessionSelected)

        # Create custom context menu on right-click
        self.ui.subjectSessionView.setContextMenuPolicy(qt.Qt.CustomContextMenu)
        self.ui.subjectSessionView.customContextMenuRequested.connect(self.openSubjectSessionContextMenu)

        # Selecting an item and clicking sessionLoadButton is equivalent to doubleclicking the item:
        self.ui.sessionLoadButton.clicked.connect(
            lambda : self.on_item_double_clicked(self.ui.subjectSessionView.currentIndex())
        )

        self.update_sessionLoadButton_enabled()
        self.ui.subjectSessionView.selectionModel().currentChanged.connect(self.update_sessionLoadButton_enabled)

        # Session management buttons
        self.ui.unloadSessionButton.clicked.connect(self.onUnloadSessionClicked)
        self.ui.saveSessionButton.clicked.connect(self.onSaveSessionClicked)

        # Manual object loading UI and the loaded objects view
        self.loadedObjectsItemModel = qt.QStandardItemModel()
        self.loadedObjectsItemModel.setHorizontalHeaderLabels(['Name', 'Type', 'ID'])
        self.ui.loadedObjectsView.setModel(self.loadedObjectsItemModel)
        self.ui.loadedObjectsView.setColumnWidth(0, 150)
        self.ui.loadedObjectsView.setColumnWidth(1, 150)
        self.ui.loadProtocolButton.clicked.connect(self.onLoadProtocolPressed)
        self.ui.loadVolumeButton.clicked.connect(self.onLoadVolumePressed)
        self.ui.loadFiducialsButton.clicked.connect(self.onLoadFiducialsPressed)
        self.ui.loadTransducerButton.clicked.connect(self.onLoadTransducerPressed)
        self.addObserver(self.logic.getParameterNode().parameterNode, vtk.vtkCommand.ModifiedEvent, self.onParameterNodeModified)

        self.session_status_field_widgets = [
            self.ui.sessionStatusSubjectNameIdValueLabel,
            self.ui.sessionStatusSessionNameIdValueLabel,
            self.ui.sessionStatusProtocolValueLabel,
            self.ui.sessionStatusTransducerValueLabel,
            self.ui.sessionStatusVolumeValueLabel,
        ]

        # ====================================

        # Make sure parameter node is initialized (needed for module reload)
        self.initializeParameterNode()

        self.updateLoadedObjectsView()
        self.updateSessionStatus()

    def onSubjectSessionSelected(self):
        self.update_addVolumeToSubjectButton_enabled()
        # Move elsewhere, only if subject
        currentIndex = self.ui.subjectSessionView.currentIndex()
        if not self.itemIsSession(currentIndex):
            self.currentSubjectID = self.subjectSessionItemModel.itemFromIndex(currentIndex.siblingAtColumn(1)).text()
 
    def openSubjectSessionContextMenu(self, point):
        index = self.ui.subjectSessionView.indexAt(point)
        if not self.itemIsSession(index):
            menu = qt.QMenu()
            addNewSubjectAction = menu.addAction("Add volume to subject")
            action = menu.exec_(self.ui.subjectSessionView.mapToGlobal(point))

            if action == addNewSubjectAction:
                self.currentSubjectID = self.subjectSessionItemModel.itemFromIndex(index.siblingAtColumn(1)).text()
                self.onAddVolumeToSubjectClicked(checked=True)
            
    @display_errors
    def onLoadDatabaseClicked(self, checked:bool):

        self.updateSubjectSessionSelector()

        self.updateSettingFromParameter('databaseDirectory')
        self.update_newSubjectButton_enabled()

    def updateSubjectSessionSelector(self):
        # Clear any items that are already there
        self.subjectSessionItemModel.removeRows(0,self.subjectSessionItemModel.rowCount())

        subject_info = self.logic.load_database(self.ui.databaseDirectoryLineEdit.currentPath)

        for subject_id, subject_name in subject_info:
            subject_row = list(map(
                create_noneditable_QStandardItem,
                [subject_name,subject_id]
            ))
            self.subjectSessionItemModel.appendRow(subject_row)

    def itemIsSession(self, index : qt.QModelIndex) -> bool:
        """Whether an item from the subject/session tree view is a session.
        Returns True if it's a session and False if it's a subject."""
        # If this has a parent, then it is a session item rather than a subject item.
        # Otherwise, it is a top-level item, so it must be a subject.
        return index.parent().isValid()

    def update_newSubjectButton_enabled(self):
        """ Update whether the add new subject button is enabled based on whether a database has been loaded"""
        if self.logic.db:
            self.ui.newSubjectButton.setEnabled(True)
            self.ui.newSubjectButton.toolTip = 'Add new subject to loaded database'
        else:
            self.ui.newSubjectButton.setDisabled(True)
            self.ui.newSubjectButton.toolTip = 'Requires a loaded database'

    def update_addVolumeToSubjectButton_enabled(self):
        """ Update whether the add volume to subject button is enabled based on whether a database has been loaded
        and a subject has been selected in the tree view"""

        if self.logic.db and not self.itemIsSession(self.ui.subjectSessionView.currentIndex()):
            self.ui.addVolumeToSubjectButton.setEnabled(True)
            self.ui.addVolumeToSubjectButton.toolTip = 'Add new volume to selected subject'
        else:
            self.ui.addVolumeToSubjectButton.setDisabled(True)
            self.ui.addVolumeToSubjectButton.toolTip = 'Requires a loaded database and subject to be selected'

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

    @display_errors
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
                        create_noneditable_QStandardItem,
                        [session_name, session_id]
                    ))
                    subject_item.appendRow(session_row)
                self.ui.subjectSessionView.expand(subject_item.index())
            
        # Make sure parameter node is initialized (needed for module reload)
        self.initializeParameterNode()

    @display_errors
    def onAddNewSubjectClicked(self, checked:bool) -> None:

        subjectdlg = AddNewSubjectDialog()
        returncode, subject_name, subject_id = subjectdlg.customexec_()

        if returncode:
            if not len(subject_name) or not len(subject_id):
                slicer.util.errorDisplay("Required fields are missing")
                return
            else:
                # Add subject to database
                self.logic.add_subject_to_database(subject_name,subject_id)
                #Update loaded subjects view
                self.updateSubjectSessionSelector()
    
    @display_errors
    def onAddVolumeToSubjectClicked(self, checked:bool) -> None:
        volumedlg = AddNewVolumeDialog()
        returncode, volume_filepath, volume_name, volume_id = volumedlg.customexec_()
        if not returncode:
            return False
        self.logic.add_volume_to_database(self.currentSubjectID, volume_id, volume_name, volume_filepath)

    @display_errors
    def onUnloadSessionClicked(self, checked:bool) -> None:
        self.logic.clear_session(clean_up_scene=True)

    @display_errors
    def onSaveSessionClicked(self, checked:bool) -> None:
        self.logic.save_session()

    @display_errors
    def onLoadProtocolPressed(self, checked:bool) -> None:
        qsettings = qt.QSettings()

        filepath: str = qt.QFileDialog.getOpenFileName(
            slicer.util.mainWindow(), # parent
            'Load protocol', # title of dialog
            qsettings.value('OpenLIFU/databaseDirectory','.'), # starting dir, with default of '.'
            "Protocols (*.json);;All Files (*)", # file type filter
        )
        if filepath:
            self.logic.load_protocol_from_file(filepath)

    @display_errors
    def onLoadTransducerPressed(self, checked:bool) -> None:
        qsettings = qt.QSettings()

        filepath: str = qt.QFileDialog.getOpenFileName(
            slicer.util.mainWindow(), # parent
            'Load protocol', # title of dialog
            qsettings.value('OpenLIFU/databaseDirectory','.'), # starting dir, with default of '.'
            "Transducers (*.json);;All Files (*)", # file type filter
        )
        if filepath:
            self.logic.load_transducer_from_file(filepath)

    def onLoadVolumePressed(self) -> None:
        """ Call slicer dialog to load volumes into the scene"""
        qsettings = qt.QSettings()

        # Allowable volume filetypes includes *.json
        volume_extensions = ("Volume" + " (*.json *.hdr *.nhdr *.nrrd *.mhd *.mha *.mnc *.nii *.nii.gz *.mgh *.mgz *.mgh.gz *.img *.img.gz *.pic);;" +
        "Dicom" + " (*.dcm *.ima);;" + 
        "All Files" + " (*)")

        filepath: str = qt.QFileDialog.getOpenFileName(
            slicer.util.mainWindow(), # parent
            'Load volume', # title of dialog
            qsettings.value('OpenLIFU/databaseDirectory','.'), # starting dir, with default of '.'
            volume_extensions, # file type filter
        )

        if filepath:
            self.logic.load_volume_from_file(filepath)
            self.updateLoadedObjectsView() # Call function here to update view based on node attributes
            

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
        if parameter_node.loaded_session is not None:
            session : SlicerOpenLIFUSession = parameter_node.loaded_session
            session_openlifu : "openlifu.db.Session" = session.session.session
            row = list(map(
                create_noneditable_QStandardItem,
                [session_openlifu.name, "Session", session_openlifu.id]
            ))
            self.loadedObjectsItemModel.appendRow(row)
        for protocol in parameter_node.loaded_protocols.values():
            row = list(map(
                create_noneditable_QStandardItem,
                [protocol.protocol.name,  "Protocol", protocol.protocol.id]
            ))
            self.loadedObjectsItemModel.appendRow(row)
        for transducer_slicer in parameter_node.loaded_transducers.values():
            transducer_slicer : SlicerOpenLIFUTransducer
            transducer_openlifu : "openlifu.Transducer" = transducer_slicer.transducer.transducer
            row = list(map(
                create_noneditable_QStandardItem,
                [transducer_openlifu.name, "Transducer", transducer_openlifu.id]
            ))
            self.loadedObjectsItemModel.appendRow(row)
        for volume_node in slicer.util.getNodesByClass('vtkMRMLScalarVolumeNode'):
            if volume_node.GetAttribute('OpenLIFUData.volume_id'):
                row = list(map(
                    create_noneditable_QStandardItem,
                    [volume_node.GetName(), "Volume", volume_node.GetAttribute('OpenLIFUData.volume_id')]
                ))
            else:
                row = list(map(
                    create_noneditable_QStandardItem,
                    [volume_node.GetName(), "Volume", volume_node.GetID()]
                ))

            self.loadedObjectsItemModel.appendRow(row)
        for fiducial_node in slicer.util.getNodesByClass('vtkMRMLMarkupsFiducialNode'):
            points_type = "Point" if fiducial_node.GetMaximumNumberOfControlPoints() == 1 else "Points"
            row = list(map(
                create_noneditable_QStandardItem,
                [fiducial_node.GetName(), points_type, fiducial_node.GetID()]
            ))
            self.loadedObjectsItemModel.appendRow(row)
        if parameter_node.loaded_plan is not None:
            row = list(map(
                create_noneditable_QStandardItem,
                ["Treatment Plan", "Plan", '']
            ))
            self.loadedObjectsItemModel.appendRow(row)

    def updateSessionStatus(self):
        """Update the active session status view and related buttons"""
        loaded_session = self.logic.getParameterNode().loaded_session
        if loaded_session is None:
            for label in self.session_status_field_widgets:
                label.setText("") # Doing this before setCurrentIndex(0) results in the desired scrolling behavior
                # (Doing it after makes Qt maintain the possibly larger size of page 1 of the stacked widget, providing unnecessary scroll bars)
            self.ui.sessionStatusStackedWidget.setCurrentIndex(0)
            for button in [self.ui.unloadSessionButton, self.ui.saveSessionButton]:
                button.setEnabled(False)
                button.setToolTip("There is no active session")
        else:
            session_openlifu : "openlifu.db.Session" = loaded_session.session.session
            subject_openlifu = self.logic.get_subject(session_openlifu.subject_id)
            protocol_openlifu : "openlifu.Protocol" = loaded_session.get_protocol().protocol
            transducer_openlifu : "openlifu.Transducer" = loaded_session.get_transducer().transducer.transducer
            self.ui.sessionStatusSubjectNameIdValueLabel.setText(
                f"{subject_openlifu.name} (ID: {session_openlifu.subject_id})"
            )
            self.ui.sessionStatusSessionNameIdValueLabel.setText(
                f"{session_openlifu.name} (ID: {session_openlifu.id})"
            )
            self.ui.sessionStatusProtocolValueLabel.setText(
                f"{protocol_openlifu.name} (ID: {session_openlifu.protocol_id})"
            )
            self.ui.sessionStatusTransducerValueLabel.setText(
                f"{transducer_openlifu.name} (ID: {session_openlifu.transducer_id})"
            )
            self.ui.sessionStatusVolumeValueLabel.setText(session_openlifu.volume_id)

            # Build the additional info message here; this is status text that conditionally displays.
            additional_info = ""
            if session_openlifu.virtual_fit_approval_for_target_id is not None:
                additional_info += f"Virtual fit approved for \"{session_openlifu.virtual_fit_approval_for_target_id}\""
            self.ui.sessionStatusAdditionalInfoLabel.setText(additional_info)

            self.ui.sessionStatusStackedWidget.setCurrentIndex(1)
            for button in [self.ui.unloadSessionButton, self.ui.saveSessionButton]:
                button.setEnabled(True)
            self.ui.unloadSessionButton.setToolTip("Unload the active session, cleaning up session-affiliated nodes in the scene")
            self.ui.saveSessionButton.setToolTip("Save the current session to the database, including session-specific transducer and target configurations")

    def onParameterNodeModified(self, caller, event) -> None:
        self.updateLoadedObjectsView()
        self.updateSessionStatus()

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

    @vtk.calldata_type(vtk.VTK_OBJECT)
    def onNodeAboutToBeRemoved(self, caller, event, node : slicer.vtkMRMLNode) -> None:

        # If any SlicerOpenLIFUTransducer objects relied on this transform node, then we need to remove them
        # as they are now invalid.
        if node.IsA('vtkMRMLTransformNode'):
            self.logic.on_transducer_affiliated_node_about_to_be_removed(node.GetID(),'transform_node')
        if node.IsA('vtkMRMLModelNode'):
            self.logic.on_transducer_affiliated_node_about_to_be_removed(node.GetID(),'model_node')

    @vtk.calldata_type(vtk.VTK_OBJECT)
    def onNodeRemoved(self, caller, event, node : slicer.vtkMRMLNode) -> None:

        # If the volume of the active session was removed, the session becomes invalid.
        if node.IsA('vtkMRMLVolumeNode'):
            self.logic.validate_session()
            self.logic.validate_plan()

        self.updateLoadedObjectsView()

    @vtk.calldata_type(vtk.VTK_OBJECT)
    def onNodeAdded(self, caller, event, node : slicer.vtkMRMLNode) -> None:
        self.updateLoadedObjectsView()

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

        self._subjects : Dict[str, openlifu.db.subject.Subject] = {} # Mapping from subject id to Subject


    def getParameterNode(self):
        return OpenLIFUDataParameterNode(super().getParameterNode())

    def clear_session(self, clean_up_scene:bool = True) -> None:
        """Unload the current session if there is one loaded.

        Args:
            clean_up_scene: Whether to remove the existing session's affiliated scene content.
                If False then the scene content is orphaned from its session, as though
                it was manually loaded without the context of a session. If True then the scene
                content is removed.
        """
        loaded_session = self.getParameterNode().loaded_session
        if loaded_session is None:
            return # There is no active session to clear
        self.getParameterNode().loaded_session = None
        if clean_up_scene:
            loaded_session.clear_volume_and_target_nodes()
            if loaded_session.get_transducer_id() in self.getParameterNode().loaded_transducers:
                self.remove_transducer(loaded_session.get_transducer_id())
            if loaded_session.get_protocol_id() in self.getParameterNode().loaded_protocols:
                self.remove_protocol(loaded_session.get_protocol_id())

    def save_session(self) -> None:
        """Save the current session to the openlifu database.
        This first writes the transducer and target information into the in-memory openlifu Session object,
        and then it writes that Session object to the database.
        """

        if self.db is None:
            raise RuntimeError("Cannot save session because there is no database connection")

        if not self.validate_session():
            raise RuntimeError("Cannot save session because there is no active session, or the active session was invalid.")

        parameter_node = self.getParameterNode()
        session : SlicerOpenLIFUSession = parameter_node.loaded_session
        targets = get_target_candidates() # future TODO: ask the user which targets they want to include in the session
        session_openlifu = session.update_underlying_openlifu_session(targets)
        parameter_node.loaded_session = session # remember to write the updated session to the parameter node

        OnConflictOpts : "openlifu.db.database.OnConflictOpts" = openlifu_lz().db.database.OnConflictOpts
        self.db.write_session(self._subjects[session_openlifu.subject_id],session_openlifu,on_conflict=OnConflictOpts.OVERWRITE)



    def validate_session(self) -> bool:
        """Check to ensure that the currently active session is in a valid state, clearing out the session
        if it is not and returning whether there is an active valid session.

        In guided mode we want this function to never ever return False -- it should not be
        possible to invalidate a session. Outside of guided mode, users can do all kinds of things like deleting
        data nodes that are in use by a session."""

        loaded_session = self.getParameterNode().loaded_session

        if loaded_session is None:
            return False # There is no active session

        # Check transducer is present
        if not loaded_session.transducer_is_valid():
            clean_up_scene = sessionInvalidatedDialogDisplay(
                "The transducer that was in use by the active session is now missing. The session will be unloaded.",
            )
            self.clear_session(clean_up_scene=clean_up_scene)
            return False

        # Check volume is present
        if not loaded_session.volume_is_valid():
            clean_up_scene = sessionInvalidatedDialogDisplay(
                "The volume that was in use by the active session is now missing. The session will be unloaded.",
            )
            self.clear_session(clean_up_scene=clean_up_scene)
            return False

        # Check protocol is present
        if not loaded_session.protocol_is_valid():
            clean_up_scene = sessionInvalidatedDialogDisplay(
                "The protocol that was in use by the active session is now missing. The session will be unloaded.",
            )
            self.clear_session(clean_up_scene=clean_up_scene)
            return False

        return True

    def validate_plan(self) -> bool:
        """Check to ensure that the currently active plan is in a valid state, clearing out the plan
        if it is not and returning whether there is an active valid plan."""

        plan = self.getParameterNode().loaded_plan

        if plan is None:
            return False # There is no active plan, no problem

        # Check volumes are present
        for volume_node in [plan.intensity, plan.pnp]:
            if volume_node is None or slicer.mrmlScene.GetNodeByID(volume_node.GetID()) is None:
                clean_up_scene = ObjectBeingUnloadedMessageBox(
                    message="A volume that was in use by the active plan is now missing. The plan will be unloaded.",
                    title="Plan invalidated",
                ).customexec_()
                self.clear_plan(clean_up_scene=clean_up_scene)
                return False

        return True

    def load_database(self, path: Path) -> Sequence[Tuple[str,str]]:
        """Load an openlifu database from a local folder hierarchy.

        This sets the internal openlifu database object and reads in all the subjects,
        and returns the subject information.

        Args:
            path: Path to the openlifu database folder on disk.

        Returns: A sequence of pairs (subject_id, subject_name) running over all subjects
            in the database.
        """
        self.clear_session()
        self._subjects = {}

        self.db = openlifu_lz().Database(path)
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
        if self.db is None:
            raise RuntimeError("Unable to fetch subject info because there is no loaded database.")
        try:
            return self._subjects[subject_id] # use the in-memory Subject if it is in memory
        except KeyError:
            # otherwise attempt to load it:
            return self.db.load_subject(subject_id)

    def get_sessions(self, subject_id:str) -> "List[openlifu.db.session.Session]":
        """Get the collection of Sessions associated with a given subject ID"""
        if self.db is None:
            raise RuntimeError("Unable to fetch session info because there is no loaded database.")
        return [
            self.db.load_session(
                self.get_subject(subject_id),
                session_id
            )
            for session_id in ensure_list(self.db.get_session_ids(subject_id))
        ]

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

    def get_session(self, subject_id:str, session_id:str) -> "openlifu.db.session.Session":
        """Fetch the Session with the given ID"""
        if self.db is None:
            raise RuntimeError("Unable to fetch session info because there is no loaded database.")
        return self.db.load_session(self.get_subject(subject_id), session_id)

    def get_current_session_transducer_id(self) -> Optional[str]:
        """Get the transducer ID of the current session, if there is a current session. Returns None
        if there isn't a current session."""
        if self.getParameterNode().loaded_session is None:
            return None
        return self.getParameterNode().loaded_session.get_transducer_id()

    def load_session(self, subject_id, session_id) -> None:

        # Make sure to the preplanning module is loaded in -- it watches for some events
        # that would cause virtual fit approval to be revoked. We could be about to load a
        # session with virtual fit approval already applied so this is important.
        slicer.util.getModule("OpenLIFUPrePlanning").widgetRepresentation()

        # === Ensure it's okay to load a session ===

        session_openlifu = self.get_session(subject_id, session_id)
        loaded_session = self.getParameterNode().loaded_session
        if (
            session_openlifu.transducer_id in self.getParameterNode().loaded_transducers
            and (
                loaded_session is None
                or session_openlifu.transducer_id != loaded_session.get_transducer_id()
                # (we are okay reloading the transducer if it's just the one affiliated with the session, since user already decided to replace the session)
            )
        ):
            if not slicer.util.confirmYesNoDisplay(
                f"Loading this session will replace the already loaded transducer with ID {session_openlifu.transducer_id}. Proceed?",
                "Confirm replace transducer"
            ):
                return

        if (
            session_openlifu.protocol_id in self.getParameterNode().loaded_protocols
            and (
                loaded_session is None
                or session_openlifu.protocol_id != loaded_session.get_protocol_id()
                # (we are okay reloading the protocol if it's just the one affiliated with the session, since user already decided to replace the session)
            )
        ):
            if not slicer.util.confirmYesNoDisplay(
                f"Loading this session will replace the already loaded protocol with ID {session_openlifu.protocol_id}. Proceed?",
                "Confirm replace protocol"
            ):
                return

        # === Proceed with loading session ===

        self.clear_session()

        volume_info = self.db.get_volume_info(session_openlifu.subject_id, session_openlifu.volume_id)

        # Create the SlicerOpenLIFU session object; this handles loading volume and targets
        new_session = SlicerOpenLIFUSession.initialize_from_openlifu_session(
            session_openlifu,
            volume_info
        )

        # === Load transducer ===

        newly_loaded_transducer = self.load_transducer_from_openlifu(
            transducer = self.db.load_transducer(session_openlifu.transducer_id),
            transducer_matrix = session_openlifu.array_transform.matrix,
            transducer_matrix_units = session_openlifu.array_transform.units,
            replace_confirmed = True,
        )
        newly_loaded_transducer.observe_transform_modified(self._on_transducer_transform_modified)

        # === Load protocol ===

        self.load_protocol_from_openlifu(
            self.db.load_protocol(session_openlifu.protocol_id),
            replace_confirmed = True,
        )

        # === Toggle slice visibility and center slices on first target ===

        slices_center_point = new_session.get_initial_center_point()
        for slice_node_name in ["Red", "Green", "Yellow"]:
            sliceNode = slicer.util.getFirstNodeByClassByName("vtkMRMLSliceNode", slice_node_name)
            sliceNode.JumpSliceByCentering(*slices_center_point)
            sliceNode.SetSliceVisible(True)
        sliceNode = slicer.util.getFirstNodeByClassByName("vtkMRMLSliceNode", "Green")
        sliceNode.SetSliceVisible(True)
        sliceNode = slicer.util.getFirstNodeByClassByName("vtkMRMLSliceNode", "Yellow")
        sliceNode.SetSliceVisible(True)

        # === Set the newly created session as the currently active session ===

        self.getParameterNode().loaded_session = new_session

    def _on_transducer_transform_modified(self, transducer: SlicerOpenLIFUTransducer) -> None:
        # Revoke any possible virtual fit approval if the transducer whose transform was just modified
        # belongs to an active session
        session = self.getParameterNode().loaded_session
        if (
            session is None
            or session.session.session.virtual_fit_approval_for_target_id is None
        ):
            return
        if session.get_transducer_id() == transducer.transducer.transducer.id:
            session.approve_virtual_fit_for_target(None) # revoke approval
            self.getParameterNode().loaded_session = session # remember to write the updated session object into the parameter node

    def load_protocol_from_file(self, filepath:str) -> None:
        protocol = openlifu_lz().Protocol.from_file(filepath)
        self.load_protocol_from_openlifu(protocol)

    def load_protocol_from_openlifu(self, protocol:"openlifu.Protocol", replace_confirmed: bool = False) -> None:
        """Load an openlifu protocol object into the scene as a SlicerOpenLIFUProtocol,
        adding it to the list of loaded openlifu objects.

        Args:
            protocol: The openlifu Protocol object
            replace_confirmed: Whether we can bypass the prompt to re-load an already loaded Protocol.
                This could be used for example if we already know the user is okay with re-loading the protocol.
        """
        loaded_protocols = self.getParameterNode().loaded_protocols
        if protocol.id in loaded_protocols and not replace_confirmed:
            if not slicer.util.confirmYesNoDisplay(
                f"A protocol with ID {protocol.id} is already loaded. Reload it?",
                "Protocol already loaded",
            ):
                return
        self.getParameterNode().loaded_protocols[protocol.id] = SlicerOpenLIFUProtocol(protocol)

    def remove_protocol(self, protocol_id:str) -> None:
        """Remove a protocol from the list of loaded protocols."""
        loaded_protocols = self.getParameterNode().loaded_protocols
        if not protocol_id in loaded_protocols:
            raise IndexError(f"No protocol with ID {protocol_id} appears to be loaded; cannot remove it.")
        loaded_protocols.pop(protocol_id)

    def load_transducer_from_file(self, filepath:str) -> None:
        transducer = openlifu_lz().Transducer.from_file(filepath)
        self.load_transducer_from_openlifu(transducer)

    def load_transducer_from_openlifu(
            self,
            transducer: "openlifu.Transducer",
            transducer_matrix: Optional[np.ndarray]=None,
            transducer_matrix_units: Optional[str]=None,
            replace_confirmed: bool = False,
        ) -> SlicerOpenLIFUTransducer:
        """Load an openlifu transducer object into the scene as a SlicerOpenLIFUTransducer,
        adding it to the list of loaded openlifu objects.

        Args:
            transducer: The openlifu Transducer object
            transducer_matrix: The transform matrix of the transducer. Assumed to be the identity if None.
            transducer_matrix_units: The units in which to interpret the transform matrix.
                The transform matrix operates on a version of the coordinate space of the transducer that has been scaled to
                these units. If left as None then the transducer's native units (Transducer.units) will be assumed.
            replace_confirmed: Whether we can bypass the prompt to re-load an already loaded Transducer.
                This could be used for example if we already know the user is okay with re-loading the transducer.

        Returns: The newly loaded SlicerOpenLIFUTransducer.
        """
        if transducer.id == self.get_current_session_transducer_id():
            slicer.util.errorDisplay(
                f"A transducer with ID {transducer.id} is in use by the current session. Not loading it.",
                "Transducer in use by session",
            )
            return
        if transducer.id in self.getParameterNode().loaded_transducers:
            if not replace_confirmed:
                if not slicer.util.confirmYesNoDisplay(
                    f"A transducer with ID {transducer.id} is already loaded. Reload it?",
                    "Transducer already loaded",
                ):
                    return
            self.remove_transducer(transducer.id)

        newly_loaded_transducer = SlicerOpenLIFUTransducer.initialize_from_openlifu_transducer(
            transducer,
            transducer_matrix=transducer_matrix,
            transducer_matrix_units=transducer_matrix_units,
        )
        self.getParameterNode().loaded_transducers[transducer.id] = newly_loaded_transducer
        return newly_loaded_transducer

    def remove_transducer(self, transducer_id:str, clean_up_scene:bool = True) -> None:
        """Remove a transducer from the list of loaded transducer, clearing away its data from the scene.

        Args:
            transducer_id: The openlifu ID of the transducer to remove
            clean_up_scene: Whether to remove the SlicerOpenLIFUTransducer's affiliated nodes from the scene.
        """
        loaded_transducers = self.getParameterNode().loaded_transducers
        if not transducer_id in loaded_transducers:
            raise IndexError(f"No transducer with ID {transducer_id} appears to be loaded; cannot remove it.")
        # Clean-up order matters here: we should pop the transducer out of the loaded objects dict and *then* clear out its
        # affiliated nodes. This is because clearing the nodes triggers the check on_transducer_affiliated_node_removed.
        transducer = loaded_transducers.pop(transducer_id)
        if clean_up_scene:
            transducer.clear_nodes()

    def on_transducer_affiliated_node_about_to_be_removed(self, node_mrml_id:str, affiliated_node_attribute_name:str) -> None:
        """Handle cleanup on SlicerOpenLIFUTransducer objects when the mrml nodes they depend on get removed from the scene.

        Args:
            node_mrml_id: The mrml scene ID of the node that was (or is about to be) removed
            affiliated_node_attribute_name: The name of the affected vtkMRMLNode-valued SlicerOpenLIFUTransducerNode attribute
                (so "transform_node" or "model_node")
        """
        matching_transducer_openlifu_ids = [
            transducer_openlifu_id
            for transducer_openlifu_id, transducer in self.getParameterNode().loaded_transducers.items()
            if getattr(transducer,affiliated_node_attribute_name).GetID() == node_mrml_id
        ]

        # If this fails, then a single mrml node was shared across multiple loaded SlicerOpenLIFUTransducers, which
        # should not be possible in the application logic.
        assert(len(matching_transducer_openlifu_ids) <= 1)

        if matching_transducer_openlifu_ids:
            # Remove the transducer, but keep any other nodes under it. This transducer was removed
            # by manual mrml scene manipulation, so we don't want to pull other nodes out from
            # under the user.
            transducer_openlifu_id = matching_transducer_openlifu_ids[0]
            clean_up_scene = ObjectBeingUnloadedMessageBox(
                message = f"The transducer with id {transducer_openlifu_id} will be unloaded because an affiliated node was removed from the scene.",
                title="Transducer removed",
                checkbox_tooltip = "Ensures cleanup of the model node and transform node affiliated with the transducer",
            ).customexec_()
            self.remove_transducer(transducer_openlifu_id, clean_up_scene=clean_up_scene)

            # If the transducer that was just removed was in use by an active session, invalidate that session
            self.validate_session()

    def set_plan(self, plan:SlicerOpenLIFUPlan):
        self.getParameterNode().loaded_plan = plan

    def clear_plan(self,  clean_up_scene:bool = True) -> None:
        """Unload the current plan if there is one loaded.

        Args:
            clean_up_scene: Whether to remove the plan's affiliated scene content.
                If False then the scene content is orphaned from its session.
                If True then the scene content is removed.
        """
        plan = self.getParameterNode().loaded_plan
        self.getParameterNode().loaded_plan = None
        if plan is None:
            return
        if clean_up_scene:
            plan.clear_nodes()

    @display_errors
    def add_subject_to_database(self, subject_name, subject_id):
        """ Adds new subject to loaded openlifu database.

        Args:
            subject_name: name of subject to be added (str)
            subject_id: id of subject to be added (str)
        """

        newOpenLIFUSubject = openlifu_lz().db.subject.Subject(
            name = subject_name,
            id = subject_id,
        )

        subject_ids = self.db.get_subject_ids()

        if newOpenLIFUSubject.id in subject_ids:
            if not slicer.util.confirmYesNoDisplay(
                f"Subject with ID {newOpenLIFUSubject.id} already exists in the database. Overwrite subject?",
                "Subject already exists"
            ):
                return

        self.db.write_subject(newOpenLIFUSubject, on_conflict = openlifu_lz().db.database.OnConflictOpts.OVERWRITE)

    def get_virtual_fit_approval_state(self) -> Optional[str]:
        """Get the virtual fit approval state in the current session, i.e. the value of virtual_fit_approval_for_target_id.
        This does not first check whether there is an active session; make sure that one exists before using this.
        """
        session = self.getParameterNode().loaded_session
        if session is None:
            raise RuntimeError("No active session.")
        return session.session.session.virtual_fit_approval_for_target_id

    def load_volume_from_file(self, filepath: str) -> None:

        parent_dir = Path(filepath).parent
        volume_id = parent_dir.name # assuming the user selected a volume within the database
  
        if slicer.app.coreIOManager().fileType(filepath) == 'VolumeFile':

            # If a corresponding json file exists in the volume's parent directory,
            # then use volume_metadata included in the json file
            volume_json_filepath = Path(parent_dir, volume_id + '.json')
            if volume_json_filepath.exists():

                volume_metadata = json.loads(volume_json_filepath.read_text())
                if volume_metadata['data_filename'] == Path(filepath).name:
                        loadedVolumeNode = slicer.util.loadVolume(filepath)
                        assign_openlifu_metadata_to_volume_node(loadedVolumeNode, volume_metadata)
                        # Note: OnNodeAdded/updateLoadedObjectsView is called before openLIFU metadata is added to the node.

                else:
                    slicer.util.loadVolume(filepath)

            # Otherwise, use default volume name and id based on filepath
            else:
                slicer.util.loadVolume(filepath)

        # If the user selects a json file, infer volume filepath information based on the volume_metadata. 
        elif Path(filepath).suffix == '.json':
            
            # Check for corresponding volume file
            volume_metadata = json.loads(Path(filepath).read_text())
            if 'data_filename' in volume_metadata: 
                volume_filepath = Path(parent_dir,volume_metadata['data_filename'])
                if volume_filepath.exists():
                    loadedVolumeNode = slicer.util.loadVolume(volume_filepath)
                    assign_openlifu_metadata_to_volume_node(loadedVolumeNode,volume_metadata)
                else:
                    slicer.util.errorDisplay(f"Cannot find associated volume file: {volume_filepath}")
            else:
                slicer.util.errorDisplay("Invalid volume filetype specified")
        else:
            slicer.util.errorDisplay("Invalid volume filetype specified")

    def add_volume_to_database(self, subject_id: str, volume_id: str, volume_name: str, volume_filepath: str) -> None:
        """ Adds volume to selected subject in loaded openlifu database.

        Args:
            subject_id: ID of subject associated with the volume (str)
            volume_id: ID of volume to be added (str)
            volume_name: Name of volume to be added (str)
            volume_filepath: filepath of volume to be added (str)
        """

        volume_ids = self.db.get_volume_ids(subject_id)
        if volume_id in volume_ids:
            if not slicer.util.confirmYesNoDisplay(
                f"Volume ID {volume_id} already exists in the database for subject {subject_id}. Overwrite volume?",
                "Volume already exists"
            ):
                return

        self.db.write_volume(subject_id, volume_id, volume_name, volume_filepath, on_conflict = openlifu_lz().db.database.OnConflictOpts.OVERWRITE)
  
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