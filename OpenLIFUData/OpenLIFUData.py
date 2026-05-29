# Standard library imports
import json
import logging
import os
import subprocess
import sys
import tempfile
import urllib.request
import zipfile
from collections import defaultdict
from pathlib import Path
from typing import Callable, Optional, List, Tuple, Dict, Sequence, TYPE_CHECKING

# Third-party imports
import ctk
import qt
import vtk
import numpy as np

# Slicer imports
import slicer
from slicer import (
    vtkMRMLMarkupsFiducialNode,
    vtkMRMLScriptedModuleNode,
)
from slicer.ScriptedLoadableModule import *
from slicer.i18n import tr as _
from slicer.i18n import translate
from slicer.parameterNodeWrapper import parameterNodeWrapper
from slicer.util import VTKObservationMixin

# OpenLIFULib imports
from OpenLIFULib import (
    SlicerOpenLIFUPhotoscan,
    SlicerOpenLIFUProtocol,
    SlicerOpenLIFURun,
    SlicerOpenLIFUSession,
    SlicerOpenLIFUSolution,
    SlicerOpenLIFUTransducer,
    assign_openlifu_metadata_to_volume_node,
    check_and_install_python_requirements,
    ensure_python_requirements_for_module_enter,
    get_cur_db,
    get_required_openlifu_version,
    get_target_candidates,
    openlifu_version_matches,
    python_requirements_exist,
)
from OpenLIFULib.events import SlicerOpenLIFUEvents
from OpenLIFULib.guided_mode_util import GuidedWorkflowMixin, get_guided_mode_state, set_guided_mode_state
from OpenLIFULib.transducer_tracking_results import (
    add_transducer_tracking_results_from_openlifu_session_format,
    clear_transducer_tracking_results,
    get_photoscan_id_from_transducer_tracking_result,
    is_transducer_tracking_result_node,
)
from OpenLIFULib.user_account_mode_util import (
    get_current_user,
    get_user_account_mode_state,
    set_user_account_mode_state,
    UserAccountBanner,
)
from OpenLIFULib.util import (
    BusyCursor,
    create_noneditable_QStandardItem,
    display_errors,
    ensure_list,
    replace_widget,
)
from OpenLIFULib.volume_thresholding import load_volume_and_threshold_background
from OpenLIFULib.virtual_fit_results import (
    add_virtual_fit_results_from_openlifu_session_format,
    clear_virtual_fit_results,
)

if TYPE_CHECKING:
    import openlifu
    import openlifu.nav.photoscan
    import openlifu.plan
    import openlifu.xdc
    from OpenLIFUHome.OpenLIFUHome import OpenLIFUHomeLogic
    from OpenLIFUPrePlanning.OpenLIFUPrePlanning import OpenLIFUPrePlanningWidget

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
        self.parent.dependencies = ["OpenLIFUHome"]  # add here list of module names that this module requires
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
    loaded_protocols : "Dict[str,SlicerOpenLIFUProtocol]"
    loaded_transducers : "Dict[str,SlicerOpenLIFUTransducer]"
    loaded_solution : "Optional[SlicerOpenLIFUSolution]"
    loaded_session : "Optional[SlicerOpenLIFUSession]"
    loaded_run: "Optional[SlicerOpenLIFURun]"
    session_photocollections: List[str]
    loaded_photoscans: "Dict[str,SlicerOpenLIFUPhotoscan]"

#
# OpenLIFUDataDialogs
#

class CreateNewSessionDialog(qt.QDialog):
    """ Create new session dialog """

    def __init__(self, transducer_ids: List[str], protocol_ids: List[str], volume_ids: List[str], parent="mainWindow"):
        """ Args:
                transducer_ids: IDs of the transducers available in the loaded database
                protocol_ids: IDs of the protocols available in the loaded database
                volume_ids: IDs of the volumes available for the selected subject in the loaded database
        """
        super().__init__(slicer.util.mainWindow() if parent == "mainWindow" else parent)

        self.setWindowTitle("Create New Session")
        self.setWindowModality(qt.Qt.WindowModal)
        self.transducer_ids = transducer_ids
        self.protocol_ids = protocol_ids
        self.volume_ids = volume_ids
        self.setup()

    def setup(self) -> None:

        formLayout = qt.QFormLayout()
        self.setLayout(formLayout)

        self.sessionName = qt.QLineEdit()
        formLayout.addRow(_("Session Name:"), self.sessionName)

        self.sessionID = qt.QLineEdit()
        formLayout.addRow(_("Session ID:"), self.sessionID)

        self.transducer = qt.QComboBox()
        self.add_items_to_combobox(self.transducer, self.transducer_ids, "transducer")
        formLayout.addRow(_("Transducer:"), self.transducer)

        self.protocol = qt.QComboBox()
        formLayout.addRow(_("Protocol:"), self.protocol)
        self.add_items_to_combobox(self.protocol, self.protocol_ids, "protocol")

        self.volume = qt.QComboBox()
        formLayout.addRow(_("Volume:"), self.volume)
        self.add_items_to_combobox(self.volume, self.volume_ids, "volume")

        # Add load checkbox into same row as Ok and Cancel buttons
        buttonLayout = qt.QHBoxLayout()

        self.loadCheckBox = qt.QCheckBox(_("Load"))
        self.loadCheckBox.setChecked(True)
        buttonLayout.addWidget(self.loadCheckBox)

        self.buttonBox = qt.QDialogButtonBox()
        self.buttonBox.setStandardButtons(qt.QDialogButtonBox.Ok |
                                          qt.QDialogButtonBox.Cancel)
        buttonLayout.addWidget(self.buttonBox)

        formLayout.addRow(buttonLayout)

        self.buttonBox.rejected.connect(self.reject)
        self.buttonBox.accepted.connect(self.validateInputs)

    def add_items_to_combobox(self, comboBox: qt.QComboBox, itemList: List[str], name: str) -> None:

        if len(itemList) == 0:
            comboBox.addItem(f"No {name} objects found", None)
            comboBox.setDisabled(True)
        else:
            for item in itemList:
                comboBox.addItem(item, item)

    def validateInputs(self) -> None:

        session_name = self.sessionName.text
        session_id = self.sessionID.text
        transducer_id = self.transducer.currentData
        protocol_id = self.protocol.currentData
        volume_id = self.volume.currentData

        if not len(session_name) or not len(session_id) or any(object is None for object in (volume_id, transducer_id, protocol_id)):
            slicer.util.errorDisplay("Required fields are missing", parent=self)
        else:
            self.accept()

    def customexec_(self) -> tuple[int, dict, bool]:

        returncode: int = self.exec_()
        session_parameters: dict = {
            'name': self.sessionName.text,
            'id': self.sessionID.text,
            'transducer_id': self.transducer.currentData,
            'protocol_id': self.protocol.currentData,
            'volume_id': self.volume.currentData,
        }
        load_checked: bool = self.loadCheckBox.isChecked()

        return (returncode, session_parameters, load_checked)

class AddNewVolumeDialog(qt.QDialog):
    """ Add new volume dialog """

    def __init__(self, parent="mainWindow"):
        super().__init__(slicer.util.mainWindow() if parent == "mainWindow" else parent)
        self.setWindowTitle("Add New Volume")
        self.setWindowModality(qt.Qt.WindowModal)
        self.setup()

    def setup(self):

        formLayout = qt.QFormLayout()
        self.setLayout(formLayout)

        self.volumeFilePath = ctk.ctkPathLineEdit()

        # Allowable volume filetypes
        self.volume_extensions = ("Volume" + " (*.hdr *.nhdr *.nrrd *.mhd *.mha *.mnc *.nii *.nii.gz *.mgh *.mgz *.mgh.gz *.img *.img.gz *.pic);;" +
        "Dicom" + " (*.dcm *.ima);;" +
        "All Files" + " (*)")
        self.volumeFilePath.nameFilters = [self.volume_extensions]

        # We hide CTK's browse button because it does not support hybrid
        # file/dirs. We add a similar button below that opens a dialog with
        # special event-handling built in
        self.volumeFilePath.showBrowseButton = False
        self.volumeFilePath.currentPathChanged.connect(self.updateVolumeDetails)
        browseButton = qt.QPushButton("...")
        browseButton.setMaximumWidth(50)
        browseButton.clicked.connect(self.browseForVolume)

        # Now, we add both the CTK file path and the custom browse button
        filePathLayout = qt.QHBoxLayout()
        filePathLayout.addWidget(self.volumeFilePath)
        filePathLayout.addWidget(browseButton)
        formLayout.addRow(_("Filepath:"), filePathLayout)

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

    def browseForVolume(self):
        """Open file dialog with dual file/directory selection support."""
        dlg = qt.QFileDialog(self)
        dlg.setOption(qt.QFileDialog.DontUseNativeDialog, True)
        dlg.setNameFilters(self.volumeFilePath.nameFilters)

        # To be able to select both directories and files, we must reactively
        # change the file mode of the dialog when a file or directory is
        # selected
        def on_item_changed(path):
            if qt.QFileInfo(path).isDir():
                dlg.setFileMode(qt.QFileDialog.Directory)
            else:
                dlg.setFileMode(qt.QFileDialog.ExistingFile)

        dlg.currentChanged.connect(on_item_changed)

        if dlg.exec_():
            self.volumeFilePath.currentPath = dlg.selectedFiles()[0]

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
        elif current_filepath.is_dir():
            # For directories (DICOM folders), use the directory name
            volume_name = current_filepath.name
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
        else:
            # Check if it's a valid volume file or a directory (for DICOM folders)
            filepath_obj = Path(volume_filepath)

            # Accept directories directly (assumed to be DICOM folders)
            if filepath_obj.is_dir():
                self.accept()
            else:
                # For files, check if it's a valid volume file type
                file_type = slicer.app.coreIOManager().fileType(volume_filepath)
                if file_type == 'VolumeFile':
                    self.accept()
                else:
                    slicer.util.errorDisplay("Invalid volume filetype specified", parent = self)


    def customexec_(self):

        returncode = self.exec_()
        volume_name = self.volumeName.text
        volume_id = self.volumeID.text
        volume_filepath = self.volumeFilePath.currentPath

        return (returncode, volume_filepath,volume_name, volume_id)


class LoadSubjectDialog(qt.QDialog):
    """
    Dialog for selecting and loading a subject from the database.

    Presents a table of available subjects with basic information and allows the user
    to select a subject to load. Returns the selected subject ID if one is chosen.
    """

    def __init__(self, db: "openlifu.db.Database", parent: Optional[qt.QWidget] = None):
        super().__init__(slicer.util.mainWindow() if parent is None else parent)

        self.setWindowTitle("Load Subject")
        self.setWindowModality(qt.Qt.WindowModal)

        self.db = db
        self.selected_subject: Optional["openlifu.db.Subject"] = None

        self.setup()

    def setup(self) -> None:
        self.boxLayout = qt.QVBoxLayout()
        self.setLayout(self.boxLayout)
        # ---- Subjects Table ----
        cols = [
            "Subject Name",
            "Subject ID",
            "# Volumes",
            "# Sessions",
        ]
        self.tableWidget = qt.QTableWidget(self)
        self.tableWidget.setColumnCount(len(cols))
        self.tableWidget.setHorizontalHeaderLabels(cols)
        self.tableWidget.horizontalHeader().setDefaultAlignment(qt.Qt.AlignLeft | qt.Qt.AlignVCenter)
        self.tableWidget.setSelectionBehavior(qt.QAbstractItemView.SelectRows)
        self.tableWidget.setEditTriggers(qt.QAbstractItemView.NoEditTriggers)
        self.tableWidget.setSizePolicy(qt.QSizePolicy.Expanding, qt.QSizePolicy.Expanding)
        self.tableWidget.horizontalHeader().setHighlightSections(False)
        self.tableWidget.horizontalHeader().setStretchLastSection(True)
        self.tableWidget.verticalHeader().setVisible(False)
        self.tableWidget.setShowGrid(False)
        self.tableWidget.setFocusPolicy(qt.Qt.NoFocus)
        self.tableWidget.setSortingEnabled(True)

        self.boxLayout.addWidget(self.tableWidget)

        header = self.tableWidget.horizontalHeader()
        header.setSectionResizeMode(0, qt.QHeaderView.Interactive)
        header.setSectionResizeMode(0, qt.QHeaderView.ResizeToContents)
        header.setSectionResizeMode(1, qt.QHeaderView.ResizeToContents)
        header.setSectionResizeMode(2, qt.QHeaderView.ResizeToContents)
        header.setSectionResizeMode(3, qt.QHeaderView.Stretch)

        # ---- Buttons Row ----
        buttonRowLayout = qt.QHBoxLayout()

        self.addButton = qt.QPushButton("Add Subject")
        self.addButton.setToolTip("Add a new subject")
        self.addButton.clicked.connect(self.on_add_subject_clicked)
        buttonRowLayout.addWidget(self.addButton)

        self.loadSubjectButton = qt.QPushButton("Load Subject")
        self.loadSubjectButton.setToolTip("Load the selected subject")
        self.loadSubjectButton.clicked.connect(self.onLoadSubjectClicked)
        self.tableWidget.clicked.connect(lambda: self.loadSubjectButton.setFocus())
        self.tableWidget.doubleClicked.connect(self.onLoadSubjectClicked)
        buttonRowLayout.addWidget(self.loadSubjectButton)

        self.boxLayout.addLayout(buttonRowLayout)

        # ---- Cancel Button ----
        buttonBoxLayout = qt.QHBoxLayout()
        buttonBoxLayout.addStretch()

        self.cancelButton = qt.QPushButton("Cancel")
        self.cancelButton.setToolTip("Close this window without loading any new subject")
        self.cancelButton.clicked.connect(lambda *args: self.reject())
        buttonBoxLayout.addWidget(self.cancelButton)

        self.boxLayout.addLayout(buttonBoxLayout)

        self.updateSubjectsList()
        self._enforceUserPermissions()

        # Resize according to desktop size
        screen = qt.QDesktopWidget().screenGeometry()
        self.resize(int(screen.width() * 0.25), int(screen.height() * 0.25))

    def updateSubjectsList(self) -> None:
        self.tableWidget.setSortingEnabled(False) # turn off sorting during edit

        subject_ids = self.db.get_subject_ids()

        self.tableWidget.clearContents()
        self.tableWidget.setRowCount(len(subject_ids))

        for row, subject_id in enumerate(subject_ids):
            subject = self.db.load_subject(subject_id)
            num_volumes = len(self.db.get_volume_ids(subject_id))
            num_sessions = len(self.db.get_session_ids(subject_id))

            self.tableWidget.setItem(row, 0, qt.QTableWidgetItem(subject.name))
            self.tableWidget.setItem(row, 1, qt.QTableWidgetItem(subject.id))
            self.tableWidget.setItem(row, 2, qt.QTableWidgetItem(str(num_volumes)))
            self.tableWidget.setItem(row, 3, qt.QTableWidgetItem(str(num_sessions)))
        self.tableWidget.resizeRowsToContents()

        self.tableWidget.setSortingEnabled(True) # turn on sorting after edit

    def appendSubjectToList(self, subject: "openlifu.db.subject.Subject") -> None:
        self.tableWidget.setSortingEnabled(False) # turn off sorting during edit

        row = self.tableWidget.rowCount
        self.tableWidget.insertRow(row)

        num_volumes = len(self.db.get_volume_ids(subject.id))
        num_sessions = len(self.db.get_session_ids(subject.id))

        self.tableWidget.setItem(row, 0, qt.QTableWidgetItem(subject.name))
        self.tableWidget.setItem(row, 1, qt.QTableWidgetItem(subject.id))
        self.tableWidget.setItem(row, 2, qt.QTableWidgetItem(str(num_volumes)))
        self.tableWidget.setItem(row, 3, qt.QTableWidgetItem(str(num_sessions)))
        self.tableWidget.resizeRowsToContents()

        self.tableWidget.setSortingEnabled(True) # turn on sorting after edit

    def on_add_subject_clicked(self, checked:bool) -> None:
        subjectdlg = AddNewSubjectDialog()
        returncode, subject_name, subject_id, load_checked = subjectdlg.customexec_()

        if not returncode:
            return

        if not len(subject_name) or not len(subject_id):
            slicer.util.errorDisplay("Required fields are missing")
            return

        # Add subject to database
        slicer.util.getModuleLogic("OpenLIFUData").add_subject_to_database(subject_name,subject_id)
        new_subject = self.db.load_subject(subject_id)
        self.appendSubjectToList(new_subject)

        if load_checked:
            self.selected_subject = new_subject
            self.accept()

    def onLoadSubjectClicked(self) -> None:
        selected_items = self.tableWidget.selectedItems()
        if not selected_items:
            slicer.util.errorDisplay("Please select a subject to load.")
            return

        row = selected_items[0].row()
        subject_id_item = self.tableWidget.item(row, 1)
        self.selected_subject = self.db.load_subject(subject_id_item.text())
        self.accept()

    def exec_and_get_subject(self) -> Optional[str]:
        """
        Execute the dialog and return the selected subject ID, or None if canceled.
        """
        if self.exec() == qt.QDialog.Accepted:
            return self.selected_subject
        return None

    def _enforceUserPermissions(self) -> None:
        """Disable session editing buttons when the user is not an operator or admin. This 
        is implemented in this manner because the existing user role infrastructure operates
        on the GUI of the app. However, this panel is a generated GUI, so we must implement
        enforceUserPermissions uniquely. It follows the same pattern as OpenLIFULogin."""

        _enforcedButtons = [
            self.addButton,
        ]

        _tooltips = [
            "Add a new subject",
        ]

        
        # === Don't enforce if no user account mode ===

        if not get_user_account_mode_state():
            for button, tooltip in zip(_enforcedButtons, _tooltips):
                button.setEnabled(True)
                button.setToolTip(tooltip)
            return

        # === Enforce ===

        if not any(r in ("admin", "operator") for r in get_current_user().roles):
            for button in _enforcedButtons:
                button.setEnabled(False)
                button.setToolTip("You do not have the required role to perform this action.")

class LoadSessionDialog(qt.QDialog):
    """ Interface for managing and selecting a session for a given subject """

    def __init__(self, db: "openlifu.db.Database", subject_id: str, loaded_session_id: Optional[str] = None, parent="mainWindow"):
        super().__init__(slicer.util.mainWindow() if parent == "mainWindow" else parent)
        subject = db.load_subject(subject_id)

        self.setWindowTitle(f"View Sessions for {subject.name} ({subject_id})")
        self.setWindowModality(qt.Qt.WindowModal)

        self.db = db
        self.subject_id = subject_id
        self.subject = subject
        self.selected_session = None
        self.loaded_session_id = loaded_session_id

        self.setup()

        self._enforce_user_permissions()

    def setup(self):
        self.box_layout = qt.QVBoxLayout()
        self.setLayout(self.box_layout)

        # ---- Sessions Table ----
        cols = [
            "Session Name",
            "Session ID",
            "Protocol",
            "Volume",
            "Transducer",
            "Created Date",
            "Modified Date",
        ]
        self.table_widget = qt.QTableWidget(self)
        self.table_widget.setColumnCount(len(cols))
        self.table_widget.setHorizontalHeaderLabels(cols)
        self.table_widget.horizontalHeader().setDefaultAlignment(qt.Qt.AlignLeft | qt.Qt.AlignVCenter)
        self.table_widget.setSelectionBehavior(qt.QAbstractItemView.SelectRows)
        self.table_widget.setEditTriggers(qt.QAbstractItemView.NoEditTriggers)
        self.table_widget.setSizePolicy(qt.QSizePolicy.Expanding, qt.QSizePolicy.Expanding)
        self.table_widget.horizontalHeader().setHighlightSections(False)
        self.table_widget.horizontalHeader().setStretchLastSection(True)
        self.table_widget.verticalHeader().setVisible(False)
        self.table_widget.setShowGrid(False)
        self.table_widget.setFocusPolicy(qt.Qt.NoFocus)
        self.table_widget.setSortingEnabled(True)

        header = self.table_widget.horizontalHeader()
        for i in range(0, 6):
            header.setSectionResizeMode(i, qt.QHeaderView.ResizeToContents)
        header.setSectionResizeMode(6, qt.QHeaderView.Stretch)

        self.box_layout.addWidget(self.table_widget)
        
        # ---- Subject Level Buttons ----
        subject_buttons_layout = qt.QHBoxLayout()

        self.new_session_button = qt.QPushButton("New Session")
        self.new_session_button.setToolTip("Create a new session for this subject")
        self.load_session_button = qt.QPushButton("Load Session")
        self.load_session_button.setToolTip("Load the currently selected session")
        self.delete_session_button = qt.QPushButton("Delete Session")
        self.delete_session_button.setToolTip("Delete the currently selected session")

        for button in [self.new_session_button, self.load_session_button, self.delete_session_button]:
            button.setSizePolicy(qt.QSizePolicy.Expanding, qt.QSizePolicy.Preferred)
            subject_buttons_layout.addWidget(button)

        self.box_layout.addLayout(subject_buttons_layout)

        self.new_session_button.clicked.connect(self.on_new_session_clicked)
        self.load_session_button.clicked.connect(self.on_load_session_clicked)
        self.table_widget.doubleClicked.connect(self.on_load_session_clicked)
        self.table_widget.clicked.connect(lambda: self.load_session_button.setFocus())
        self.delete_session_button.clicked.connect(self.on_delete_session_clicked)

        # ---- Cancel Button ----
        self.button_box = qt.QDialogButtonBox()
        self.cancel_button = self.button_box.addButton("Cancel", qt.QDialogButtonBox.RejectRole)
        self.cancel_button.setToolTip("Close this window without loading any new session")
        self.cancel_button.clicked.connect(lambda *args: self.reject())
        self.box_layout.addWidget(self.button_box)

        # ---- Populate Table ----
        self.update_sessions_list()

        # Resize according to desktop size
        screen = qt.QDesktopWidget().screenGeometry()
        self.resize(int(screen.width() * 0.50), int(screen.height() * 0.25))

    def update_sessions_list(self):
        self.table_widget.setSortingEnabled(False) # turn off sorting during edit

        session_ids = self.db.get_session_ids(self.subject_id)

        self.table_widget.clearContents()
        self.table_widget.setRowCount(0)
        self.table_widget.setRowCount(len(session_ids))

        for row, session_id in enumerate(session_ids):
            # Session info
            session = self.db.load_session(self.subject, session_id)
            self.table_widget.setItem(row, 0, qt.QTableWidgetItem(session.name))
            self.table_widget.setItem(row, 1, qt.QTableWidgetItem(session.id))

            # Protocol, volume, transducer text
            def safe_call(func, fallback="NA"):
                try:
                    return func()
                except Exception:
                    return fallback

            protocol_name = safe_call(lambda: self.db.load_protocol(session.protocol_id).name)
            volume_name = safe_call(lambda: self.db.get_volume_info(self.subject_id, session.volume_id)["name"])
            transducer_name = safe_call(lambda: self.db.load_transducer(session.transducer_id).name)

            protocol_text = f"{protocol_name} ({session.protocol_id})"
            volume_text = f"{volume_name} ({session.volume_id})"
            transducer_text = f"{transducer_name} ({session.transducer_id})"

            self.table_widget.setItem(row, 2, qt.QTableWidgetItem(protocol_text))
            self.table_widget.setItem(row, 3, qt.QTableWidgetItem(volume_text))
            self.table_widget.setItem(row, 4, qt.QTableWidgetItem(transducer_text))

            # Date created, modified

            self.table_widget.setItem(row, 5, qt.QTableWidgetItem(session.date_created.strftime('%Y-%m-%d %H:%M')))
            self.table_widget.setItem(row, 6, qt.QTableWidgetItem(session.date_modified.strftime('%Y-%m-%d %H:%M')))

        self.table_widget.resizeRowsToContents()
        self.table_widget.setSortingEnabled(True) # turn on sorting after edit

    @display_errors
    def append_session_to_list(self, session: "openlifu.db.session.Session") -> None:
        self.table_widget.setSortingEnabled(False) # turn off sorting during edit

        row = self.table_widget.rowCount
        self.table_widget.insertRow(row)

        def safe_call(func, fallback="NA"):
            try:
                return func()
            except Exception:
                return fallback

        protocol_name = safe_call(lambda: self.db.load_protocol(session.protocol_id).name)
        volume_name = safe_call(lambda: self.db.get_volume_info(self.subject_id, session.volume_id)["name"])
        transducer_name = safe_call(lambda: self.db.load_transducer(session.transducer_id).name)

        protocol_text = f"{protocol_name} ({session.protocol_id})"
        volume_text = f"{volume_name} ({session.volume_id})"
        transducer_text = f"{transducer_name} ({session.transducer_id})"

        self.table_widget.setItem(row, 0, qt.QTableWidgetItem(session.name))
        self.table_widget.setItem(row, 1, qt.QTableWidgetItem(session.id))
        self.table_widget.setItem(row, 2, qt.QTableWidgetItem(protocol_text))
        self.table_widget.setItem(row, 3, qt.QTableWidgetItem(volume_text))
        self.table_widget.setItem(row, 4, qt.QTableWidgetItem(transducer_text))
        self.table_widget.setItem(row, 5, qt.QTableWidgetItem(session.date_created.strftime('%Y-%m-%d %H:%M')))
        self.table_widget.setItem(row, 6, qt.QTableWidgetItem(session.date_modified.strftime('%Y-%m-%d %H:%M')))

        self.table_widget.resizeRowsToContents()
        self.table_widget.setSortingEnabled(True) # turn on sorting after edit

    @display_errors
    def on_new_session_clicked(self, checked: bool) -> None:
        """
        Create a new session for the current subject, after applying user permission filtering on protocols.
        """
        db_transducer_ids = self.db.get_transducer_ids()
        db_volume_ids = self.db.get_volume_ids(self.subject_id)

        # ---- Don't show unallowed protocols; requires loading protocols ----
        db_protocol_ids = self.db.get_protocol_ids()
        protocols: List["openlifu.plan.Protocol"] = self.db.load_all_protocols()

        if not get_user_account_mode_state() or 'admin' in get_current_user().roles:
            pass  # No filtering needed
        else:
            # Filter protocol IDs where any user role is in the protocol's allowed roles
            db_protocol_ids = [
                protocol.id for protocol in protocols
                if any(role in protocol.allowed_roles for role in get_current_user().roles)
            ]
        # --------------------------------------------------------------------

        sessiondlg = CreateNewSessionDialog(
            transducer_ids=db_transducer_ids,
            protocol_ids=db_protocol_ids,
            volume_ids=db_volume_ids
        )
        returncode, session_parameters, load_checked = sessiondlg.customexec_()

        if not returncode:
            return

        slicer.util.getModuleLogic("OpenLIFUData").add_session_to_database(self.subject_id, session_parameters)
        new_session = self.db.load_session(self.subject, session_parameters["id"])
        self.append_session_to_list(new_session)

        if load_checked:
            self.selected_session = new_session
            self.accept()

    @display_errors
    def on_load_session_clicked(self, *args) -> None:
        """
        Load the selected session into the OpenLIFUData module if the user has permission.
        """
        selected_items = self.table_widget.selectedItems()
        if not selected_items:
            slicer.util.errorDisplay("Please select a session to load.")
            return

        row = selected_items[0].row()
        session_id_item = self.table_widget.item(row, 1)  # Column 1 is Session ID
        session_id = session_id_item.text()

        # ---- Prevent loading sessions with unallowed protocols ----
        session = self.db.load_session(self.subject, session_id)
        protocol = self.db.load_protocol(session.protocol_id)

        if not get_user_account_mode_state() or 'admin' in get_current_user().roles:
            pass  # No enforcement needed
        else:
            if not any(role in protocol.allowed_roles for role in get_current_user().roles):
                slicer.util.errorDisplay(
                    f"Could not load the session '{session.name}' ({session_id}) because it uses a protocol "
                    f"that does not allow any of the logged-in user's roles."
                )
                return
        # -----------------------------------------------------------

        self.selected_session = session
        self.accept()

    @display_errors
    def on_delete_session_clicked(self, *args) -> None:
        """
        Delete the selected session into the OpenLIFUData module if the user has permission.
        """
        selected_items = self.table_widget.selectedItems()
        if not selected_items:
            slicer.util.errorDisplay("Please select a session to delete.")
            return

        row = selected_items[0].row()
        session_name_item = self.table_widget.item(row, 0)  # Column 0 is Session name
        session_name = session_name_item.text()
        session_id_item = self.table_widget.item(row, 1)  # Column 1 is Session ID
        session_id = session_id_item.text()

        # ---- Prevent deleting sessions if it is the currently loaded session ----
        if session_id == self.loaded_session_id:
            slicer.util.errorDisplay(
                    f"Cannot delete session '{session_name}' ({session_id}) because it is "
                    "active. Exit the currently loaded session before deleting."
                    )
            return

        # ---- Prevent deleting sessions with unallowed protocols ----
        if not get_user_account_mode_state() or 'admin' in get_current_user().roles:
            pass  # No enforcement needed
        else:
            session = self.db.load_session(self.subject, session_id)
            protocol = self.db.load_protocol(session.protocol_id)

            if not any(role in protocol.allowed_roles for role in get_current_user().roles):
                slicer.util.errorDisplay(
                    f"Could not delete the session '{session.name}' ({session_id}) because it uses a protocol "
                    f"that does not allow any of the logged-in user's roles."
                )
                return

        # -----------------------------------------------------------
        # Add an additional layer of confirmation
        if slicer.util.confirmYesNoDisplay(
            text="Are you sure you want to delete this session? This action cannot be undone.",
            windowTitle="Delete session?",
        ):
            self.db.delete_session(self.subject.id, session_id)

            # Update session dialog
            self.table_widget.removeRow(row)
            self.table_widget.resizeRowsToContents()

    def exec_and_get_session(self) -> Optional["openlifu.db.session.Session"]:
        """
        Execute the dialog and return the selected session ID, or None if canceled.
        """
        if self.exec() == qt.QDialog.Accepted:
            return self.selected_session
        return None

    def _enforce_user_permissions(self) -> None:
        """
        Disable session editing buttons when the user is not an operator or admin. This 
        is implemented in this manner because the existing user role infrastructure operates
        on the GUI of the app. However, this panel is a generated GUI, so we must implement
        enforce_user_permissions uniquely. It follows the same pattern as OpenLIFULogin.
        """
        _enforced_buttons = [
            self.new_session_button,
            self.delete_session_button,
        ]
        _tooltips = [
            "Create a new session for this subject",
            "Delete the currently selected session",
        ]

        # Don't enforce if no user account mode
        if not get_user_account_mode_state():
            for button, tooltip in zip(_enforced_buttons, _tooltips):
                button.setEnabled(True)
                button.setToolTip(tooltip)
            return

        # Enforce
        if not any(r in ("admin", "operator") for r in get_current_user().roles):
            for button in _enforced_buttons:
                button.setEnabled(False)
                button.setToolTip("You do not have the required role to perform this action.")

class LoadPhotoscanDialog(qt.QDialog):
    """ Load photoscan dialog """

    def __init__(self, parent="mainWindow"):
        super().__init__(slicer.util.mainWindow() if parent == "mainWindow" else parent)
        self.setWindowTitle("Load photoscan")
        self.setWindowModality(qt.Qt.WindowModal)
        self.setup()

    def setup(self):

        self.formLayout = qt.QFormLayout()
        self.setLayout(self.formLayout)

        # Model filepath
        self.photoscanModelFilePath = ctk.ctkPathLineEdit()
        self.photoscanModelFilePath.filters = ctk.ctkPathLineEdit.Files
        # Allowable photoscan filetypes
        self.photoscan_model_extensions = ("Photoscan Model" + " (*.obj *.vtk *.stl *.ply *.vtp *.g *json);;" +
        "All Files" + " (*)")
        self.photoscanModelFilePath.nameFilters = [self.photoscan_model_extensions]
        self.photoscanModelFilePath.currentPathChanged.connect(self.updateDialog)
        self.formLayout.addRow(_("Photoscan JSON or Model Filepath:"), self.photoscanModelFilePath)

        self.buttonBox = qt.QDialogButtonBox()
        self.buttonBox.setStandardButtons(qt.QDialogButtonBox.Ok |
                                          qt.QDialogButtonBox.Cancel)
        self.formLayout.addWidget(self.buttonBox)

        self.buttonBox.rejected.connect(self.reject)
        self.buttonBox.accepted.connect(self.validateInputs)

    def updateDialog(self):
        """If the selected model file path is an .obj (or related format) model file, then
        the user needs to specify the corresponding texture file. This function updates the 
        dialog to prompt the user to select the texture image. If the user selects a .json file
        as the model file, then the model and texture filepaths are determined from the json file."""

        current_filepath = Path(self.photoscanModelFilePath.currentPath)
        if current_filepath.suffix != '.json' and self.formLayout.rowCount() == 2:
            # Texture filepath
            self.photoscanTextureFilePath = ctk.ctkPathLineEdit()
            self.photoscanTextureFilePath.filters = ctk.ctkPathLineEdit.Files
            # Allowable photoscan filetypes
            self.photoscan_texture_extensions = ("Photoscan Texture" + " (*.jpg *.jpeg *.png *.tiff *.exr);;" +
            "All Files" + " (*)")
            self.photoscanTextureFilePath.nameFilters = [self.photoscan_texture_extensions]
            self.formLayout.insertRow(1,_("Texture Filepath (Optional):"), self.photoscanTextureFilePath)
        elif current_filepath.suffix == '.json' and self.formLayout.rowCount() == 3:
            self.formLayout.removeRow(1) 

    def validateInputs(self):
        photoscan_model_filepath = Path(self.photoscanModelFilePath.currentPath)
        if photoscan_model_filepath.suffix != '.json':
            if not slicer.app.coreIOManager().fileType(photoscan_model_filepath) == 'ModelFile':
                slicer.util.errorDisplay("Invalid photoscan filetype specified", parent = self)
                return
            if len(self.photoscanTextureFilePath.currentPath):
                photoscan_texture_filepath = Path(self.photoscanTextureFilePath.currentPath)
                if photoscan_texture_filepath.suffix not in ['.exr','.jpg','.png','.tiff']:
                    slicer.util.errorDisplay("Invalid photoscan texture filetype specified", parent = self)
                    return
        self.accept()

    def customexec_(self):
        returncode = self.exec_()
        model_or_json_filepath = self.photoscanModelFilePath.currentPath
        if len(model_or_json_filepath) and Path(model_or_json_filepath).suffix != '.json':
            texture_filepath = self.photoscanTextureFilePath.currentPath
            return returncode, model_or_json_filepath, texture_filepath
        else:
            return returncode, model_or_json_filepath, None
    
class AddNewSubjectDialog(qt.QDialog):
    """ Add new subject dialog """

    def __init__(self, parent="mainWindow"):
        super().__init__(slicer.util.mainWindow() if parent == "mainWindow" else parent)
        self.setWindowTitle("Add New Subject")
        self.setWindowModality(qt.Qt.WindowModal)
        self.setup()

    def setup(self) -> None:
        formLayout = qt.QFormLayout()
        self.setLayout(formLayout)

        self.subjectName = qt.QLineEdit()
        formLayout.addRow(_("Subject Name:"), self.subjectName)

        self.subjectID = qt.QLineEdit()
        formLayout.addRow(_("Subject ID:"), self.subjectID)

        # Create a horizontal layout to hold checkbox and button box in same row
        buttonLayout = qt.QHBoxLayout()

        self.loadCheckBox = qt.QCheckBox(_("Load"))
        self.loadCheckBox.setChecked(True)
        buttonLayout.addWidget(self.loadCheckBox)

        self.buttonBox = qt.QDialogButtonBox()
        self.buttonBox.setStandardButtons(qt.QDialogButtonBox.Ok |
                                          qt.QDialogButtonBox.Cancel)
        buttonLayout.addWidget(self.buttonBox)

        formLayout.addRow(buttonLayout)

        self.buttonBox.rejected.connect(self.reject)
        self.buttonBox.accepted.connect(self.accept)

    def customexec_(self) -> tuple[int, str, str, bool]:

        returncode: int = self.exec_()
        subject_name: str = self.subjectName.text
        subject_id: str = self.subjectID.text
        load_checked: bool = self.loadCheckBox.isChecked()

        return (returncode, subject_name, subject_id, load_checked)

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


# ---------------------------------------------------------------------------
# Transducer Manager
# ---------------------------------------------------------------------------

# IDs that ship as part of the openlifu reference database and are required
# as templates when constructing a TransducerArray from a connected device.
# Deleting one of these is gated behind a confirmation dialog.
PROTECTED_TRANSDUCER_IDS = frozenset({
    "openlifu_1x155",
    "openlifu_1x400",
    "openlifu_2x155",
    "openlifu_2x400",
})

# (n_modules, freq_khz) -> template id. Mirrors openlifu.xdc.transducerarray._DEFAULT_TEMPLATE_IDS.
_TEMPLATE_IDS_BY_COUNT_FREQ: Dict[Tuple[int, int], str] = {
    (1, 155): "openlifu_1x155",
    (1, 400): "openlifu_1x400",
    (2, 155): "openlifu_2x155",
    (2, 400): "openlifu_2x400",
}


def _read_connected_user_configs(iface) -> List[dict]:
    """Pull a ``user_config`` dict from every connected TX module.

    Returns the list in module-index order. Raises ``RuntimeError`` if no
    modules are connected or any ``read_config`` call returns ``None``.
    """
    if iface is None:
        raise RuntimeError(
            "The LIFU interface has not been initialized. Open the OpenLIFU "
            "Sonication Control module to initialize the hardware interface."
        )
    txdevice = getattr(iface, "txdevice", None)
    if txdevice is None:
        raise RuntimeError("The LIFU interface has no TX device.")
    count = int(txdevice.get_tx_module_count())
    if count <= 0:
        raise RuntimeError("No TX modules are connected.")
    user_configs: List[dict] = []
    for i in range(count):
        cfg = txdevice.read_config(module=i)
        if cfg is None:
            raise RuntimeError(
                f"Failed to read user_config from TX module {i}. "
                f"The module may not have been provisioned."
            )
        user_configs.append(json.loads(cfg.get_json_str()))
    return user_configs


def _module_count_from_template_id(template_id: str) -> Optional[int]:
    """Parse the leading module count out of a template id like ``openlifu_2x400``.

    Returns ``None`` if the id does not match the expected ``..._<N>x<freq>``
    convention.
    """
    if not isinstance(template_id, str):
        return None
    tail = template_id.rsplit("_", 1)[-1]  # e.g. "2x400"
    if "x" not in tail:
        return None
    head = tail.split("x", 1)[0]
    try:
        return int(head)
    except ValueError:
        return None


def _resolve_template_id_for_user_configs(user_configs: List[dict]) -> Tuple[str, Optional[dict]]:
    """Return ``(template_id, device_block)`` for a list of module user_configs.

    Prefers ``user_configs[0]["device"]["template"]`` if present **and** its
    encoded module count agrees with ``len(user_configs)``. Otherwise falls
    back to ``(n_modules, freq)``. ``device_block`` is the parsed ``device``
    sub-dict of the lead module or ``None`` if no such block exists.

    The consistency check guards against stale device metadata: e.g. a 2x
    transducer whose module 0 was once provisioned with a ``openlifu_1x400``
    template would otherwise be reported as 1x400 even after the second
    module is connected.

    Raises:
        RuntimeError: if neither source can resolve a template id (e.g. all
            modules omit ``freq``).
    """
    device_block = user_configs[0].get("device") or None
    if device_block:
        tid = device_block.get("template")
        if isinstance(tid, str) and tid:
            recorded_count = _module_count_from_template_id(tid)
            if recorded_count is None or recorded_count == len(user_configs):
                return tid, device_block
            # Stale / mismatched template id on module 0 -- ignore it and
            # fall through to the (count, freq) inference below.
            logging.warning(
                "Ignoring stale device.template=%r on module 0: it claims %d "
                "module(s) but %d are currently connected. Falling back to "
                "count+freq inference.",
                tid, recorded_count, len(user_configs),
            )

    freqs = {c.get("freq") for c in user_configs if c.get("freq") is not None}
    if len(freqs) > 1:
        raise RuntimeError(
            f"Connected TX modules report mismatched frequencies: {sorted(freqs)}"
        )
    freq = next(iter(freqs)) if freqs else None
    if freq is None:
        raise RuntimeError(
            "Connected TX modules do not report a frequency; cannot infer "
            "a default template id."
        )
    tid = _TEMPLATE_IDS_BY_COUNT_FREQ.get((len(user_configs), int(freq)))
    if tid is None:
        raise RuntimeError(
            f"No default template is defined for {len(user_configs)} module(s) "
            f"at {int(freq)} kHz."
        )
    return tid, device_block


class DeviceConfigEditDialog(qt.QDialog):
    """Prompt the user for ``id`` + ``name`` of a transducer assembled from a connected device.

    Shown by :class:`TransducerManagerDialog` whenever the user clicks
    *Add from Device*, regardless of whether the lead module already carries
    a ``device`` block (existing values are pre-populated and remain
    editable). The template id and the per-module HWID list are read-only so
    the user can confirm what is being saved.
    """

    def __init__(
        self,
        template_id: str,
        module_hwids: List[str],
        initial_id: str = "",
        initial_name: str = "",
        parent="mainWindow",
    ):
        super().__init__(slicer.util.mainWindow() if parent == "mainWindow" else parent)
        self.setWindowTitle("Add Transducer from Device")
        self.setWindowModality(qt.Qt.WindowModal)
        # Drop the unimplemented ``?`` help button from the title bar.
        self.setWindowFlags(self.windowFlags() & ~qt.Qt.WindowContextHelpButtonHint)
        self.template_id = template_id
        self.module_hwids = list(module_hwids)
        self._initial_id = initial_id
        self._initial_name = initial_name
        self._setup()

    def _setup(self) -> None:
        # Outer VBox so we can place the form on top and the button row at the
        # bottom; QFormLayout.addWidget does not span the form as a row, which
        # is why the original button box wasn't showing up.
        outer = qt.QVBoxLayout()
        self.setLayout(outer)

        form = qt.QFormLayout()
        outer.addLayout(form)

        self.idEdit = qt.QLineEdit()
        self.idEdit.setPlaceholderText("e.g. my_array_001")
        self.idEdit.setText(self._initial_id)
        form.addRow(_("Transducer ID:"), self.idEdit)

        self.nameEdit = qt.QLineEdit()
        self.nameEdit.setPlaceholderText("e.g. My Array #1")
        self.nameEdit.setText(self._initial_name)
        form.addRow(_("Transducer Name:"), self.nameEdit)

        templateLabel = qt.QLabel(self.template_id)
        templateLabel.setStyleSheet("color: #888;")
        templateLabel.setToolTip(
            "Inferred from the number of connected modules and their reported "
            "frequency. The template provides the mesh files."
        )
        form.addRow(_("Template:"), templateLabel)

        hwidList = qt.QListWidget()
        hwidList.setSelectionMode(qt.QAbstractItemView.NoSelection)
        hwidList.setFocusPolicy(qt.Qt.NoFocus)
        for idx, hwid in enumerate(self.module_hwids):
            hwidList.addItem(f"Module {idx}: {hwid}")
        hwidList.setFixedHeight(min(120, 22 * max(1, len(self.module_hwids)) + 10))
        form.addRow(_("Modules:"), hwidList)

        # NOTE: PythonQt's QDialogButtonBox flag-constructor binding is flaky;
        # construct empty and then setStandardButtons, like the other dialogs
        # in this module (AddNewSubjectDialog, CreateNewSessionDialog).
        self.buttonBox = qt.QDialogButtonBox()
        self.buttonBox.setStandardButtons(
            qt.QDialogButtonBox.Ok | qt.QDialogButtonBox.Cancel
        )
        # Per UX request: call the accept button "Finish".
        finishBtn = self.buttonBox.button(qt.QDialogButtonBox.Ok)
        if finishBtn is not None:
            finishBtn.setText("Finish")
        outer.addWidget(self.buttonBox)
        self.buttonBox.accepted.connect(self._validate)
        self.buttonBox.rejected.connect(self.reject)

    def _validate(self) -> None:
        if not self.idEdit.text.strip():
            slicer.util.errorDisplay("Transducer ID is required.", parent=self)
            return
        if not self.nameEdit.text.strip():
            slicer.util.errorDisplay("Transducer name is required.", parent=self)
            return
        self.accept()

    def customexec_(self) -> Tuple[int, str, str]:
        rc = self.exec_()
        return rc, self.idEdit.text.strip(), self.nameEdit.text.strip()


class TransducerManagerDialog(qt.QDialog):
    """Tabular manager for transducer definitions stored in the loaded database.

    Columns: ``[LED, ID, Name, Modules]``. The LED on a row is lit green when
    the row's ID matches the ``device.id`` reported by the currently
    connected hardware's lead module. Actions:

    * **Add from File** -- existing ``load_transducer_from_file`` path.
    * **Add from Device** -- calls
      :py:meth:`openlifu.xdc.TransducerArray.get_connected` against the
      OpenLIFUSonicationControl module's ``LIFUInterface``; surfaces the
      "device config / hardware" validation errors from openlifu-python and
      prompts the user for ``id`` + ``name`` when the lead module carries no
      ``device`` block.
    * **Preview** -- loads the selected transducer into the Slicer scene
      via the existing ``load_transducer_from_openlifu`` path.
    * **Delete** -- removes the selected transducer from the database, with
      a special confirmation when one of the four built-in templates is
      targeted.
    """

    def __init__(self, db: "openlifu.db.Database", parent="mainWindow"):
        super().__init__(slicer.util.mainWindow() if parent == "mainWindow" else parent)
        self.setWindowTitle("Manage Transducers")
        self.setWindowModality(qt.Qt.WindowModal)
        self.db = db
        self._setup()
        self.refresh()

    # ---- UI ----
    def _setup(self) -> None:
        layout = qt.QVBoxLayout()
        self.setLayout(layout)

        cols = ["", "ID", "Name", "Modules"]
        self.table = qt.QTableWidget(self)
        self.table.setColumnCount(len(cols))
        self.table.setHorizontalHeaderLabels(cols)
        self.table.setSelectionBehavior(qt.QAbstractItemView.SelectRows)
        self.table.setSelectionMode(qt.QAbstractItemView.SingleSelection)
        self.table.setEditTriggers(qt.QAbstractItemView.NoEditTriggers)
        self.table.verticalHeader().setVisible(False)
        self.table.setShowGrid(False)
        self.table.horizontalHeader().setHighlightSections(False)
        self.table.horizontalHeader().setStretchLastSection(True)
        header = self.table.horizontalHeader()
        header.setSectionResizeMode(0, qt.QHeaderView.Fixed)
        self.table.setColumnWidth(0, 22)
        header.setSectionResizeMode(1, qt.QHeaderView.ResizeToContents)
        header.setSectionResizeMode(2, qt.QHeaderView.ResizeToContents)
        header.setSectionResizeMode(3, qt.QHeaderView.Stretch)
        self.table.setSortingEnabled(True)
        layout.addWidget(self.table)

        # Action buttons
        actionRow = qt.QHBoxLayout()
        self.addFileButton = qt.QPushButton("Add from File")
        self.addFileButton.setToolTip("Load a transducer definition from a JSON file on disk")
        self.addDeviceButton = qt.QPushButton("Add from Device")
        self.addDeviceButton.setToolTip(
            "Assemble a transducer definition from the connected TX modules' user_configs"
        )
        self.previewButton = qt.QPushButton("Preview")
        self.previewButton.setToolTip("Load the selected transducer into the scene")
        self.deleteButton = qt.QPushButton("Delete")
        self.deleteButton.setToolTip("Remove the selected transducer from the database")
        for b in (self.addFileButton, self.addDeviceButton, self.previewButton, self.deleteButton):
            b.setSizePolicy(qt.QSizePolicy.Expanding, qt.QSizePolicy.Preferred)
            actionRow.addWidget(b)
        layout.addLayout(actionRow)

        # Close
        bb = qt.QDialogButtonBox()
        bb.addButton("Close", qt.QDialogButtonBox.RejectRole)
        bb.rejected.connect(self.reject)
        layout.addWidget(bb)

        self.addFileButton.clicked.connect(self.onAddFromFile)
        self.addDeviceButton.clicked.connect(self.onAddFromDevice)
        self.previewButton.clicked.connect(self.onPreview)
        self.deleteButton.clicked.connect(self.onDelete)
        self.table.doubleClicked.connect(self.onPreview)

        screen = qt.QDesktopWidget().screenGeometry()
        self.resize(int(screen.width() * 0.45), int(screen.height() * 0.35))

    # ---- LED helpers ----
    @staticmethod
    def _make_led_icon(color: str) -> qt.QIcon:
        """Build a small filled-circle icon in the requested CSS color."""
        pix = qt.QPixmap(16, 16)
        pix.fill(qt.Qt.transparent)
        painter = qt.QPainter(pix)
        try:
            painter.setRenderHint(qt.QPainter.Antialiasing, True)
            painter.setPen(qt.QPen(qt.QColor("#444"), 1))
            painter.setBrush(qt.QBrush(qt.QColor(color)))
            painter.drawEllipse(2, 2, 12, 12)
        finally:
            painter.end()
        return qt.QIcon(pix)

    def _connected_device_id(self) -> Optional[str]:
        """Return the ``device.id`` from the lead-module user_config of the connected device.

        Returns ``None`` if no device is connected, or the lead module carries
        no ``device`` block, or any read fails.
        """
        try:
            sc_logic = slicer.util.getModuleLogic("OpenLIFUSonicationControl")
            iface = getattr(sc_logic, "cur_lifu_interface", None)
            if iface is None:
                return None
            tx_conn, _hv = iface.is_device_connected()
            if not tx_conn:
                return None
            cfg = iface.txdevice.read_config(module=0)
            if cfg is None:
                return None
            data = json.loads(cfg.get_json_str())
            dev = data.get("device") or {}
            did = dev.get("id")
            return did if isinstance(did, str) and did else None
        except Exception as e:
            logging.debug("Transducer manager: could not query connected device id: %s", e)
            return None

    # ---- Table population ----
    def refresh(self) -> None:
        self.table.setSortingEnabled(False)
        self.table.clearContents()
        try:
            ids = list(self.db.get_transducer_ids() or [])
        except Exception as e:
            logging.warning("Could not list transducer ids: %s", e)
            ids = []
        connected_id = self._connected_device_id()
        on_icon = self._make_led_icon("#2ecc71")   # green
        off_icon = self._make_led_icon("#444")     # dim gray

        self.table.setRowCount(len(ids))
        for row, tid in enumerate(ids):
            # Resolve metadata via a non-converting load so we get the TransducerArray.
            try:
                obj = self.db.load_transducer(tid, convert_array=False)
                name = getattr(obj, "name", tid)
                if hasattr(obj, "modules"):
                    n_mods = len(obj.modules)
                else:
                    n_mods = 1
            except Exception as e:
                logging.warning("Could not load transducer %s for listing: %s", tid, e)
                name = tid
                n_mods = 0

            led_item = qt.QTableWidgetItem()
            led_item.setIcon(on_icon if (connected_id and tid == connected_id) else off_icon)
            led_item.setFlags(qt.Qt.ItemIsEnabled | qt.Qt.ItemIsSelectable)
            led_item.setToolTip("Connected" if (connected_id and tid == connected_id) else "")
            self.table.setItem(row, 0, led_item)
            self.table.setItem(row, 1, qt.QTableWidgetItem(str(tid)))
            self.table.setItem(row, 2, qt.QTableWidgetItem(str(name)))
            self.table.setItem(row, 3, qt.QTableWidgetItem(str(n_mods)))

        self.table.resizeRowsToContents()
        self.table.setSortingEnabled(True)

    def _selected_transducer_id(self) -> Optional[str]:
        items = self.table.selectedItems()
        if not items:
            return None
        row = items[0].row()
        idItem = self.table.item(row, 1)
        return idItem.text() if idItem else None

    # ---- Actions ----
    @display_errors
    def onAddFromFile(self, checked: bool = False) -> None:
        qsettings = qt.QSettings()
        filepath: str = qt.QFileDialog.getOpenFileName(
            slicer.util.mainWindow(),
            "Load transducer",
            qsettings.value("OpenLIFU/databaseDirectory", "."),
            "Transducers (*.json);;All Files (*)",
        )
        if not filepath:
            return
        # Delegate to the existing module logic so the load is consistent with
        # the legacy "Manual Object Load > Load Transducer" path. After loading
        # into the scene, also persist into the database.
        logic = slicer.util.getModuleLogic("OpenLIFUData")
        loaded = logic.load_transducer_from_file(filepath)
        if loaded is None:
            # load_transducer_from_file currently returns None; fall back to
            # parsing the file again to get something we can write to the db.
            try:
                obj = openlifu_lz().xdc.util.load_transducer_from_file(filepath, convert_array=False)
            except Exception as e:
                slicer.util.errorDisplay(f"Failed to read transducer file: {e}", parent=self)
                return
        else:
            obj = loaded
        try:
            self.db.write_transducer(obj, on_conflict="overwrite")
        except Exception as e:
            slicer.util.errorDisplay(f"Failed to write transducer to database: {e}", parent=self)
            return
        self.refresh()

    @display_errors
    def onAddFromDevice(self, checked: bool = False) -> None:
        try:
            sc_logic = slicer.util.getModuleLogic("OpenLIFUSonicationControl")
            iface = getattr(sc_logic, "cur_lifu_interface", None)
        except (AttributeError, RuntimeError):
            iface = None
        if iface is None:
            slicer.util.errorDisplay(
                "The LIFU interface has not been initialized. Open the OpenLIFU "
                "Sonication Control module to initialize the hardware interface.",
                parent=self,
            )
            return
        try:
            tx_conn, _hv = iface.is_device_connected()
        except Exception as e:
            slicer.util.errorDisplay(f"Could not query device connection: {e}", parent=self)
            return
        if not tx_conn:
            slicer.util.errorDisplay("No TX device is currently connected.", parent=self)
            return

        # 1. Pull user_configs.
        try:
            user_configs = _read_connected_user_configs(iface)
        except Exception as e:
            slicer.util.errorDisplay(
                f"Could not read user_config from the connected device:\n\n{e}",
                parent=self,
            )
            return

        # 2. Resolve the template id (device.template if present, else count+freq).
        try:
            template_id, device_block = _resolve_template_id_for_user_configs(user_configs)
        except Exception as e:
            slicer.util.errorDisplay(str(e), parent=self)
            return

        # 3. Template MUST exist in the database -- the meshless fallback is
        #    not acceptable in the Slicer UI (the meshes are needed for
        #    visualization and registration).
        try:
            db_ids = list(self.db.get_transducer_ids() or [])
        except Exception:
            db_ids = []
        if template_id not in db_ids:
            slicer.util.errorDisplay(
                f"The required template transducer '{template_id}' is not in the loaded "
                f"database. Add it (e.g. from the openlifu sample database) and try again.",
                parent=self,
            )
            return

        # 4. Always show the edit dialog (prepopulated from any existing
        #    device block) so the user can confirm / change the id and name
        #    before we assemble + write.
        hwids = [str(c.get("hwid")) for c in user_configs]
        initial_id = ""
        initial_name = ""
        if device_block is not None:
            initial_id = str(device_block.get("id") or "")
            initial_name = str(device_block.get("name") or "")
        dlg = DeviceConfigEditDialog(
            template_id=template_id,
            module_hwids=hwids,
            initial_id=initial_id,
            initial_name=initial_name,
            parent=self,
        )
        rc, arr_id, arr_name = dlg.customexec_()
        if not rc:
            return
        # Detect changes that warrant pushing the new device block back to the
        # connected hardware: either there was no device block at all, or the
        # user edited the id or name.
        id_changed = device_block is None or arr_id != initial_id
        name_changed = device_block is None or arr_name != initial_name
        should_offer_writeback = device_block is None or id_changed or name_changed

        # 5. Assemble. Use ``use_default_template=False`` so any future db
        #    lookup failure becomes an error rather than a silent meshless
        #    fallback. Capture the db-mismatch UserWarning emitted by
        #    openlifu-python so we can surface it.
        import warnings as _warnings
        with _warnings.catch_warnings(record=True) as caught:
            _warnings.simplefilter("always")
            try:
                arr = openlifu_lz().xdc.TransducerArray.get_connected(
                    interface=iface,
                    db=self.db,
                    arr_id=arr_id,
                    arr_name=arr_name,
                    use_default_template=False,
                )
            except Exception as e:
                slicer.util.errorDisplay(
                    f"Failed to assemble transducer from device:\n\n{e}",
                    parent=self,
                )
                return
        mismatch_warning = next(
            (str(w.message) for w in caught if "differs from the version" in str(w.message)),
            None,
        )
        if mismatch_warning:
            confirmed = slicer.util.confirmYesNoDisplay(
                f"{mismatch_warning}\n\nOverwrite the database copy with the version "
                f"assembled from the connected device?",
                "Database mismatch",
                parent=self,
            )
            if not confirmed:
                return

        # 6. Write to db, copying mesh files in from the template's directory.
        try:
            paths = self.db.get_transducer_absolute_filepaths(template_id) or {}
            reg_path = paths.get("registration_surface_abspath") or None
            body_path = paths.get("transducer_body_abspath") or None
            self.db.write_transducer(
                arr,
                registration_surface_model_filepath=reg_path,
                transducer_body_model_filepath=body_path,
                on_conflict="overwrite",
            )
        except Exception as e:
            slicer.util.errorDisplay(
                f"Failed to write transducer to database:\n\n{e}",
                parent=self,
            )
            return

        slicer.util.infoDisplay(
            f"Saved transducer '{arr.id}' to the database.",
            windowTitle="Add from Device",
            parent=self,
        )

        # 7. If the device block didn't exist (or the user edited id/name),
        #    offer to push the new device block down to module 0 so the
        #    physical device starts reporting it on the next read.
        if should_offer_writeback:
            confirmed = slicer.util.confirmYesNoDisplay(
                "Write this transducer's device definition (id, name, modules) "
                "back to the connected device's module 0?\n\n"
                "This updates the on-device user_config so the device will "
                "report itself as this transducer on subsequent connects.",
                "Write device config",
                parent=self,
            )
            if confirmed:
                try:
                    self._write_device_block_to_module0(iface, arr, user_configs[0])
                except Exception as e:
                    slicer.util.errorDisplay(
                        f"Failed to write device config to module 0:\n\n{e}",
                        parent=self,
                    )

        self.refresh()

    def _write_device_block_to_module0(self, iface, arr, lead_user_config: dict) -> None:
        """Overwrite the ``device`` section of module 0's user_config and write it back.

        Reads module 0's current user_config dict (passed in as
        ``lead_user_config`` to avoid an extra round-trip), splices in the
        new ``device`` block derived from the just-assembled
        :class:`TransducerArray`, and calls
        ``txdevice.write_config_json(json_str, module=0)``.
        """
        new_device = arr.to_device_config()
        # ``to_device_config`` records the assembled template id when present
        # so future ``get_connected`` calls can prefer it over the (count, freq)
        # default mapping.
        try:
            template_id, _ = _resolve_template_id_for_user_configs([lead_user_config])
            new_device.setdefault("template", template_id)
        except Exception:
            pass
        updated = dict(lead_user_config)
        updated["device"] = new_device
        json_str = json.dumps(updated)
        iface.txdevice.write_config_json(json_str, module=0)
        logging.info(
            "Wrote new device block (id=%s) to TX module 0 user_config.",
            new_device.get("id"),
        )

    @display_errors
    def onPreview(self, *args) -> None:
        tid = self._selected_transducer_id()
        if not tid:
            slicer.util.errorDisplay("Select a transducer first.", parent=self)
            return
        try:
            obj = self.db.load_transducer(tid, convert_array=True)
        except Exception as e:
            slicer.util.errorDisplay(f"Could not load transducer '{tid}': {e}", parent=self)
            return
        try:
            abspaths = self.db.get_transducer_absolute_filepaths(tid)
        except Exception:
            abspaths = {}
        logic = slicer.util.getModuleLogic("OpenLIFUData")
        logic.load_transducer_from_openlifu(obj, abspaths)

    @display_errors
    def onDelete(self, *args) -> None:
        tid = self._selected_transducer_id()
        if not tid:
            slicer.util.errorDisplay("Select a transducer first.", parent=self)
            return
        if tid in PROTECTED_TRANSDUCER_IDS:
            confirmed = slicer.util.confirmYesNoDisplay(
                "You are about to delete a built-in transducer definition. "
                "This is used as a template when connecting new transducers of "
                "this type. Are you sure you want to delete it?",
                "Delete built-in transducer",
                parent=self,
            )
        else:
            confirmed = slicer.util.confirmYesNoDisplay(
                f"Delete transducer '{tid}' from the database?",
                "Delete transducer",
                parent=self,
            )
        if not confirmed:
            return
        try:
            self._delete_transducer_from_db(tid)
        except Exception as e:
            slicer.util.errorDisplay(f"Failed to delete transducer '{tid}': {e}", parent=self)
            return
        self.refresh()

    def _delete_transducer_from_db(self, transducer_id: str) -> None:
        """Remove ``transducer_id`` from the database.

        ``openlifu.db.Database`` does not currently expose a ``delete_transducer``
        method, so we mirror the on-disk layout produced by ``write_transducer``:
        drop the id from ``transducers.json`` and remove the per-transducer
        directory.
        """
        import shutil
        ids = list(self.db.get_transducer_ids() or [])
        if transducer_id not in ids:
            return
        ids = [i for i in ids if i != transducer_id]
        self.db.write_transducer_ids(ids)
        try:
            transducer_dir = Path(self.db.get_transducer_filename(transducer_id)).parent
            if transducer_dir.is_dir():
                shutil.rmtree(transducer_dir)
        except Exception as e:
            logging.warning(
                "Removed transducer '%s' from index but could not remove its "
                "directory: %s",
                transducer_id, e,
            )


class _ModuleWidgetPopupDialog(qt.QDialog):
    """A modal dialog that presents another Slicer module's widget as a native popup.

    The given module's ``widgetRepresentation()`` is temporarily reparented into
    this dialog so its full UI and behaviour are preserved without duplicating
    code. When the dialog closes the widget is reparented back. To make the
    popup look like a native dialog (rather than a hosted module panel) we also
    hide the developer-mode "Reload & Test" section and the guided-mode
    workflow-controls widget while the dialog is open, and restore them on
    close.
    """

    def __init__(self, module_name: str, title: str, parent=None):
        qt.QDialog.__init__(self, parent or slicer.util.mainWindow())
        self._module_name = module_name
        self.setWindowTitle(title)
        self.setWindowModality(qt.Qt.ApplicationModal)
        self.setMinimumWidth(450)

        self._hosted_widget = slicer.util.getModule(module_name).widgetRepresentation()
        # Remember where the widget originally lived so we can put it back.
        self._original_parent = self._hosted_widget.parent()
        self._original_visible = self._hosted_widget.visible

        # Collect the embedded-panel-only chrome (dev "Reload & Test" section
        # and guided-mode workflow controls) so we can hide it while the popup
        # is shown and restore its previous visibility on close.
        #
        # The reload section is created by ``ScriptedLoadableModuleWidget.setup``
        # without a Qt object name, but it is exposed as the Python attribute
        # ``reloadCollapsibleButton`` on the Python widget instance (which we
        # reach via ``slicer.util.getModuleWidget``). The workflow controls
        # widget is a ``WorkflowControls``-class child that replaces the
        # ``workflowControlsPlaceholder`` at module setup time, so we find it
        # by class name in the widget tree.
        self._hidden_children: "List[Tuple[qt.QWidget, bool]]" = []

        try:
            python_widget = slicer.util.getModuleWidget(module_name)
        except Exception:  # noqa: BLE001 - widget may not yet be instantiated
            python_widget = None
        reload_section = getattr(python_widget, "reloadCollapsibleButton", None)
        if reload_section is not None:
            self._hidden_children.append((reload_section, reload_section.visible))
            reload_section.setVisible(False)

        for workflow_widget in slicer.util.findChildren(
            self._hosted_widget, className="WorkflowControls"
        ):
            self._hidden_children.append((workflow_widget, workflow_widget.visible))
            workflow_widget.setVisible(False)

        layout = qt.QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.addWidget(self._hosted_widget)
        self._hosted_widget.setVisible(True)

        # Done button row.
        button_row = qt.QHBoxLayout()
        button_row.addStretch(1)
        done_button = qt.QPushButton("Done")
        # Wrap in lambda: QPushButton.clicked emits a `checked` bool which would
        # otherwise be passed as a positional arg to QDialog.accept() and raise
        # "Called accept() -> void with wrong number of arguments: (False,)".
        done_button.clicked.connect(lambda: self.accept())
        button_row.addWidget(done_button)
        layout.addLayout(button_row)

    def _restore_hosted_widget(self) -> None:
        """Reparent the hosted widget back to its original parent."""
        if self._hosted_widget is None:
            return
        # Restore the visibility of children we temporarily hid.
        for child, was_visible in self._hidden_children:
            try:
                child.setVisible(was_visible)
            except Exception:  # noqa: BLE001 - widget may already be gone
                pass
        self._hidden_children = []
        # Reparent back. If the original parent has a layout, re-add to it; if
        # not, just set the parent so Slicer's module panel can rediscover it
        # the next time the module is selected.
        self._hosted_widget.setParent(self._original_parent)
        original_layout = (
            self._original_parent.layout() if self._original_parent is not None else None
        )
        if original_layout is not None:
            original_layout.addWidget(self._hosted_widget)
        self._hosted_widget.setVisible(self._original_visible)
        self._hosted_widget = None

    def closeEvent(self, event):
        # Note: do NOT call ``super().closeEvent(event)``. Slicer's Qt binding
        # does not expose QWidget.closeEvent through ``super()`` and that raises
        # ``AttributeError: 'super' object has no attribute 'closeEvent'``.
        # QDialog's default closeEvent simply calls ``reject()``, which will
        # route through ``done()`` below and trigger our cleanup.
        self._restore_hosted_widget()

    def done(self, result):
        # done() is called for accept()/reject() and is the most reliable entry
        # point that fires for both keyboard-Esc and the Done button.
        self._restore_hosted_widget()
        qt.QDialog.done(self, result)

    def _restore_hosted_widget(self) -> None:
        """Reparent the hosted widget back to its original parent."""
        if self._hosted_widget is None:
            return
        # Restore the visibility of children we temporarily hid.
        for child, was_visible in self._hidden_children:
            try:
                child.setVisible(was_visible)
            except Exception:  # noqa: BLE001 - widget may already be gone
                pass
        self._hidden_children = []
        # Reparent back. If the original parent has a layout, re-add to it; if
        # not, just set the parent so Slicer's module panel can rediscover it
        # the next time the module is selected.
        self._hosted_widget.setParent(self._original_parent)
        original_layout = (
            self._original_parent.layout() if self._original_parent is not None else None
        )
        if original_layout is not None:
            original_layout.addWidget(self._hosted_widget)
        self._hosted_widget.setVisible(self._original_visible)
        self._hosted_widget = None

    def closeEvent(self, event):
        # Note: do NOT call ``super().closeEvent(event)``. Slicer's Qt binding
        # does not expose QWidget.closeEvent through ``super()`` and that raises
        # ``AttributeError: 'super' object has no attribute 'closeEvent'``.
        # QDialog's default closeEvent simply calls ``reject()``, which will
        # route through ``done()`` below and trigger our cleanup.
        self._restore_hosted_widget()

    def done(self, result):
        # done() is called for accept()/reject() and is the most reliable entry
        # point that fires for both keyboard-Esc and the Done button.
        self._restore_hosted_widget()
        qt.QDialog.done(self, result)


#
# OpenLIFUDataWidget
#


class OpenLIFUDataWidget(ScriptedLoadableModuleWidget, VTKObservationMixin, GuidedWorkflowMixin):
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

        # Mapping from mrml node ID to a list of vtkCommand tags that can later be used to remove the observation
        self.node_observations : Dict[str,List[int]] = defaultdict(list)

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

        # User account banner widget replacement. Note: the visibility is
        # initialized to false because this widget will *always* exist before
        # the login module parameter node.
        self.user_account_banner = UserAccountBanner(parent=self.ui.userAccountBannerPlaceholder.parentWidget())
        replace_widget(self.ui.userAccountBannerPlaceholder, self.user_account_banner, self.ui)
        self.user_account_banner.visible = False

        # Manual object loading UI and the loaded objects view
        self.loadedObjectsItemModel = qt.QStandardItemModel()
        self.loadedObjectsItemModel.setHorizontalHeaderLabels(['Name', 'Type', 'ID'])
        self.ui.loadedObjectsView.setModel(self.loadedObjectsItemModel)
        self.ui.loadedObjectsView.setColumnWidth(0, 150)
        self.ui.loadedObjectsView.setColumnWidth(1, 150)
        self.ui.loadProtocolButton.clicked.connect(self.onLoadProtocolPressed)
        self.ui.loadVolumeButton.clicked.connect(self.onLoadVolumePressed)
        self.ui.loadTransducerButton.clicked.connect(self.onLoadTransducerPressed)
        self.ui.loadPhotoscanButton.clicked.connect(self.onLoadPhotoscanPressed)

        # Inject guided mode workflows
        self.inject_workflow_controls_into_placeholder()

        # ---- Internal connections and observers ----
        self.logic.call_on_subject_changed(self.on_subject_changed)

        # ---- External connections and observers ----

        # These connections ensure that we update parameter node when scene is closed
        self.addObserver(slicer.mrmlScene, slicer.mrmlScene.StartCloseEvent, self.onSceneStartClose)
        self.addObserver(slicer.mrmlScene, slicer.mrmlScene.EndCloseEvent, self.onSceneEndClose)

        # This ensures that we properly handle SlicerOpenLIFU objects that become invalid when their nodes are deleted
        self.setupSHNodeObserver()
        self.addObserver(slicer.mrmlScene, slicer.vtkMRMLScene.NodeAddedEvent, self.onNodeAdded)
        self.addObserver(slicer.mrmlScene, slicer.vtkMRMLScene.NodeRemovedEvent, self.onNodeRemoved)
        
        # Connect to the database logic for updates related to database
        slicer.util.getModuleLogic("OpenLIFUDatabase").call_on_db_changed(self.onDatabaseChanged)

        # ---- Internal button setup ----

        # Top toolbar: login and database popup buttons. These open the
        # existing OpenLIFULogin / OpenLIFUDatabase module widgets inside a
        # modal popup so the user does not have to navigate away from the
        # Data page.
        self.ui.loginPopupButton.clicked.connect(self.onOpenLoginPopup)
        self.ui.databasePopupButton.clicked.connect(self.onOpenDatabasePopup)
        self.ui.devicePopupButton.clicked.connect(self.onOpenDevicePopup)

        # The device button uses a custom PNG icon (Resources/Icons/device.png)
        # rather than an emoji glyph so we have a single piece of art that can
        # be swapped in later. Loaded here in Python so we don't need a Qt
        # resource (.qrc) file - matches the pattern used by OpenLIFUHome's
        # toolbar sync action.
        try:
            module_dir = os.path.dirname(__file__)
            icon_path = os.path.join(module_dir, "Resources", "Icons", "device.png")
            if os.path.exists(icon_path):
                self.ui.devicePopupButton.setIcon(qt.QIcon(icon_path))
        except Exception as e:
            logging.warning("Could not load device button icon: %s", e)

        # Top toolbar: Navigation (guided mode) and Permissions (user account
        # mode) dropdowns. Each dropdown drives the corresponding global state
        # via existing helpers; the comboboxes are kept in sync with those
        # states through ``updateNavigationModeComboBox`` /
        # ``updatePermissionsModeComboBox`` (called from observers below and on
        # enter()).
        self.ui.navigationModeComboBox.currentIndexChanged.connect(
            self.onNavigationModeChanged
        )
        self.ui.permissionsModeComboBox.currentIndexChanged.connect(
            self.onPermissionsModeChanged
        )

        # Dependencies collapsible section: status checks + install buttons.
        # These previously lived on the Login page; they are independent of the
        # login state so they belong on the Data page.
        self.ui.installPythonRequirementsPushButton.clicked.connect(
            self.onUpdateOpenLIFUClicked
        )
        self.ui.installADBPushButton.clicked.connect(self.onInstallADBClicked)

        # Protocols collapsible section
        self.ui.manageProtocolsPushButton.clicked.connect(
            lambda: slicer.util.selectModule("OpenLIFUProtocolConfig")
        )

        # Transducers collapsible section (manager popup not yet implemented).
        self.ui.manageTransducersPushButton.clicked.connect(
            self.onManageTransducersClicked
        )

        # Subject collapsible section
        self.ui.chooseSubjectButton.clicked.connect(self.on_load_subject_clicked)

        # Volumes collapsible section
        self.ui.addVolumeButton.clicked.connect(self.on_add_volume_clicked)

        # Session collapsible section
        self.ui.chooseSessionButton.clicked.connect(self.on_load_session_clicked)

        # ---- Issue updates that may not have been triggered yet ---
        
        # Make sure parameter node is initialized (needed for module reload)
        self.initializeParameterNode()
        
        self.updateLoadedObjectsView()
        self.updateSessionStatus()
        self.update_loadSubjectButton_enabled()
        self.update_volumesCollapsibleButton_checked_and_enabled()
        self.update_sessionCollapsibleButton_checked_and_enabled()
        self.updateWorkflowControls()

        # Sync the navigation dropdown with the current guided-mode state
        # (Home is safe to touch during setup), then observe Home so external
        # changes keep the dropdown in sync.
        self._initDependencyStatus()
        self.updateNavigationModeComboBox()
        # Note: do NOT call updateToolbarButtonStates() here. It reads
        # get_current_user() which routes through getModuleLogic("OpenLIFULogin"),
        # forcing the Login widget to be instantiated and re-triggering the
        # cache-walk recursion / ProtocolConfig AttributeError. The toolbar is
        # styled once the deferred wiring runs (one event-loop tick later).
        try:
            home_parameter_node = slicer.util.getModuleLogic("OpenLIFUHome").getParameterNode()
            self.addObserver(
                home_parameter_node,
                vtk.vtkCommand.ModifiedEvent,
                lambda caller, event: self.updateNavigationModeComboBox(),
            )
        except (AttributeError, RuntimeError):
            # Home logic not ready yet; the dropdown will still sync on enter().
            pass

        # IMPORTANT: defer ALL OpenLIFULogin interaction out of setup().
        #
        # Anything that reaches into the Login module - including
        # ``get_user_account_mode_state()``, ``get_current_user()`` and
        # ``slicer.util.getModuleLogic("OpenLIFULogin")`` - forces the Login
        # widget to be instantiated. ``OpenLIFULoginWidget.setup`` then walks
        # every OpenLIFU module's ``widgetRepresentation()`` to cache
        # permission widgets, which re-enters this Data widget and drags
        # OpenLIFUProtocolConfig into a half-built state (triggering
        # ``AttributeError: 'pulse_definition_widget'``). Pushing this wiring
        # onto a 0-ms QTimer runs it past the end of the current setup call so
        # the Login cache-walk happens in isolation.
        #
        # The Permissions dropdown stays at its default "Unrestricted" value
        # until this deferred sync runs (~1 event-loop tick later).
        qt.QTimer.singleShot(0, self._wireDeferredLoginObservers)

    def _wireDeferredLoginObservers(self) -> None:
        """Wire Login-parameter-node observation, active-user callback, and
        initial Permissions-dropdown sync.

        Must run *after* this widget's setup() returns; see the comment in
        setup() for why we defer it via QTimer.singleShot(0).
        """
        try:
            login_parameter_node = slicer.util.getModuleLogic("OpenLIFULogin").getParameterNode()
            self.addObserver(
                login_parameter_node,
                vtk.vtkCommand.ModifiedEvent,
                lambda caller, event: (
                    self.updatePermissionsModeComboBox(),
                    self.updatePermissionsGating(),
                ),
            )
        except (AttributeError, RuntimeError):
            return
        try:
            slicer.util.getModuleLogic("OpenLIFULogin").call_on_active_user_changed(
                lambda _user: (
                    self.updatePermissionsGating(),
                    self.updateToolbarButtonStates(),
                )
            )
        except (AttributeError, RuntimeError):
            pass

        # Subscribe to LIFU device connect / disconnect events so the device
        # button outline reflects the live (TX, HV) connection state. The
        # callbacks fire on every TX or HV connection / disconnection; we
        # just re-paint the toolbar buttons (``updateToolbarButtonStates``
        # reads ``is_device_connected()`` itself).
        try:
            sc_logic = slicer.util.getModuleLogic("OpenLIFUSonicationControl")
            sc_logic.call_on_lifu_device_connected(
                lambda *_args, **_kwargs: self.updateToolbarButtonStates()
            )
            sc_logic.call_on_lifu_device_disconnected(
                lambda *_args, **_kwargs: self.updateToolbarButtonStates()
            )
        except (AttributeError, RuntimeError):
            pass

        # Now that Login is fully alive, run the first sync of the Permissions
        # dropdown, per-section gating, and toolbar-button styling.
        self.updatePermissionsModeComboBox()
        self.updatePermissionsGating()
        self.updateToolbarButtonStates()

        # If a database directory was remembered from a previous session
        # (QSettings("OpenLIFU/databaseDirectory")) and it still looks like a
        # valid openlifu database root, auto-connect to it now so the user
        # doesn't have to re-open the Database popup on every Slicer boot.
        self._tryAutoConnectDatabase()

    def _tryAutoConnectDatabase(self) -> None:
        """Best-effort auto-connect to the last-used openlifu database.

        Reads the persisted ``OpenLIFU/databaseDirectory`` QSetting and, if it
        points at a valid openlifu database root and no database is currently
        loaded, calls the Database logic's ``load_database()``. All errors are
        swallowed (logged) -- a missing or moved database directory must not
        block module setup.
        """
        try:
            if get_cur_db() is not None:
                return  # already connected
            qsettings = qt.QSettings()
            path_str = qsettings.value("OpenLIFU/databaseDirectory", "")
            if not path_str:
                return
            db_logic = slicer.util.getModuleLogic("OpenLIFUDatabase")
            path = Path(str(path_str))
            if not db_logic.path_is_openlifu_database_root(path):
                logging.info(
                    "Skipping database auto-connect: %s is not a valid openlifu "
                    "database root.", path,
                )
                return
            db_logic.load_database(path)
            logging.info("Auto-connected to openlifu database at %s", path)
        except Exception as e:
            logging.warning("Database auto-connect failed: %s", e)

    def onDatabaseChanged(self, db: Optional["openlifu.db.Database"] = None):
        self.logic.subject = None
        self.logic.clear_session()
        self.update_loadSubjectButton_enabled()
        self.updateToolbarButtonStates()

    def on_subject_changed(self, subject: Optional["openlifu.db.Subject"] = None):
        self.logic.clear_session()

        self.update_subject_status()
        self.update_volumes_table(subject, get_cur_db())
        self.update_volumesCollapsibleButton_checked_and_enabled()
        self.update_sessionCollapsibleButton_checked_and_enabled()
        self.updateWorkflowControls()

    def update_loadSubjectButton_enabled(self):
        """ Update whether the load subject button is enabled based on whether a
        database has been loaded"""
        if get_cur_db():
            self.ui.chooseSubjectButton.setEnabled(True)
            self.ui.chooseSubjectButton.toolTip = 'Add new subject to loaded database'
        else:
            self.ui.chooseSubjectButton.setDisabled(True)
            self.ui.chooseSubjectButton.toolTip = 'Requires a loaded database'

    def update_volumesCollapsibleButton_checked_and_enabled(self) -> None:
        if self.logic.subject is None:
            self.ui.volumesCollapsibleButton.setChecked(False)
            self.ui.volumesCollapsibleButton.setEnabled(False)
        else:
            # The volumes section should be automatically opened only if there
            # are no volumes for the subject
            subject_has_no_volumes = len(get_cur_db().get_volume_ids(self.logic.subject.id)) <= 0
            self.ui.volumesCollapsibleButton.setChecked(subject_has_no_volumes)
            self.ui.volumesCollapsibleButton.setEnabled(True)

    def update_sessionCollapsibleButton_checked_and_enabled(self) -> None:
        if self.logic.subject is None:
            self.ui.sessionCollapsibleButton.setChecked(False)
            self.ui.sessionCollapsibleButton.setEnabled(False)
        else:
            # Only enable the sessions collapsible expanded if the subject has volumes
            subject_has_volumes = len(get_cur_db().get_volume_ids(self.logic.subject.id)) > 0
            self.ui.sessionCollapsibleButton.setChecked(subject_has_volumes)
            self.ui.sessionCollapsibleButton.setEnabled(subject_has_volumes)

    @display_errors
    def on_load_subject_clicked(self, checked: bool, load_subject_dlg = None) -> bool:
        if load_subject_dlg is None:
            load_subject_dlg = LoadSubjectDialog(get_cur_db())
        new_subject = load_subject_dlg.exec_and_get_subject()

        if not new_subject:
            return False

        self.logic.subject = new_subject
        return True

    @display_errors
    def on_add_volume_clicked(self, checked: bool) -> bool:
        volumedlg = AddNewVolumeDialog()
        returncode, volume_filepath, volume_name, volume_id = volumedlg.customexec_()
        if not returncode:
            return False

        self.logic.add_volume_to_database(self.logic.subject.id, volume_id, volume_name, volume_filepath)
        self.update_subject_status()
        self.update_volumes_table(self.logic.subject, get_cur_db())

        # Update enabledness of session area in case the first volume was added
        # and we need to enable the section
        self.update_sessionCollapsibleButton_checked_and_enabled()
        return True

    @display_errors
    def on_load_session_clicked(self, checked:bool, load_session_dlg = None) -> bool:
        if load_session_dlg is None:
            load_session_dlg = LoadSessionDialog(get_cur_db(), self.logic.subject.id)
        new_session = load_session_dlg.exec_and_get_session()

        if not new_session:
            return False

        self.logic.clear_session(clean_up_scene=True)
        self.logic.load_session(self.logic.subject.id, new_session.id)
        return True

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
            'Load transducer', # title of dialog
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


    @display_errors
    def onLoadPhotoscanPressed(self, checked:bool) -> None:
        """Opens a dialog for the user to select and load a photoscan into the scene.
        The user can choose either a model file (e.g., `.obj`) along with a corresponding
        texture file (e.g., `.jpg`), or a single JSON file that encapsulates both model
        and texture metadata. 
        """
        load_photoscan_dlg = LoadPhotoscanDialog()
        returncode, model_or_json_filepath, texture_filepath = load_photoscan_dlg.customexec_()
        if not returncode:
            return

        self.logic.load_photoscan_from_file(model_or_json_filepath, texture_filepath)
        self.updateLoadedObjectsView() # Call function here to update view based on node attributes (for texture volume)

    def updateLoadedObjectsView(self):
        self.loadedObjectsItemModel.removeRows(0,self.loadedObjectsItemModel.rowCount())
        parameter_node = self._parameterNode
        if parameter_node is None:
            return
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
            transducer_openlifu : "openlifu.xdc.Transducer" = transducer_slicer.transducer.transducer
            row = list(map(
                create_noneditable_QStandardItem,
                [transducer_openlifu.name, "Transducer", transducer_openlifu.id]
            ))
            self.loadedObjectsItemModel.appendRow(row)
        for volume_node in slicer.util.getNodesByClass('vtkMRMLScalarVolumeNode'):
            if volume_node.GetAttribute('isOpenLIFUSolution') is not None or volume_node.GetAttribute('isOpenLIFUPhotoscan') is not None:
                continue
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
        if parameter_node.loaded_solution is not None:
            solution_openlifu = parameter_node.loaded_solution.solution.solution
            row = list(map(
                create_noneditable_QStandardItem,
                [solution_openlifu.name, "Solution", solution_openlifu.id]
            ))
            self.loadedObjectsItemModel.appendRow(row)
        if parameter_node.loaded_run is not None:
            run_openlifu = parameter_node.loaded_run.run
            row = list(map(
                create_noneditable_QStandardItem,
                [run_openlifu.name, "Run", run_openlifu.id]
            ))
            self.loadedObjectsItemModel.appendRow(row)
        for photoscan_slicer in parameter_node.loaded_photoscans.values():
            photoscan_slicer : SlicerOpenLIFUPhotoscan
            photoscan_openlifu : "openlifu.nav.photoscan.Photoscan" = photoscan_slicer.photoscan.photoscan
            row = list(map(
                create_noneditable_QStandardItem,
                [photoscan_openlifu.name, "Photoscan", photoscan_openlifu.id]
            ))
            self.loadedObjectsItemModel.appendRow(row)

    def update_subject_status(self):
        """Update the active subject status view"""
        subject_status_field_widgets = [
            self.ui.subjectStatusSubjectIdValueLabel,
            self.ui.subjectStatusSubjectNameValueLabel,
            self.ui.subjectStatusSubjectNumberOfSessionsValueLabel,
            self.ui.subjectStatusSubjectNumberOfVolumesValueLabel,
        ]

        if self.logic.subject is None:
            for label in subject_status_field_widgets:
                label.setText("") # Doing this before setCurrentIndex(0) results in the desired scrolling behavior
                # (Doing it after makes Qt maintain the possibly larger size of page 1 of the collectioned widget, providing unnecessary scroll bars)
            self.ui.subjectStatusStackedWidget.setCurrentIndex(0)
        else:
            num_sessions = len(get_cur_db().get_session_ids(self.logic.subject.id))
            num_volumes = len(get_cur_db().get_volume_ids(self.logic.subject.id))

            self.ui.subjectStatusSubjectIdValueLabel.setText(self.logic.subject.id)
            self.ui.subjectStatusSubjectNameValueLabel.setText(self.logic.subject.name)
            self.ui.subjectStatusSubjectNumberOfSessionsValueLabel.setText(num_sessions)
            self.ui.subjectStatusSubjectNumberOfVolumesValueLabel.setText(num_volumes)

            self.ui.subjectStatusStackedWidget.setCurrentIndex(1)

    def update_volumes_table(self, subject: Optional["openlifu.db.subject.Subject"], db: "openlifu.db.Database") -> None:
        """Update the volumes table from a given subject id and database"""
        if subject is None:
            volume_ids = []
        else:
            volume_ids = db.get_volume_ids(subject.id)

        self.ui.volumesTableWidget.clearContents()
        self.ui.volumesTableWidget.setRowCount(0)
        self.ui.volumesTableWidget.setRowCount(len(volume_ids))

        def infer_format(filepath: str) -> str:
            filepath = filepath.lower()
            if filepath.endswith('.nii') or filepath.endswith('.nii.gz'):
                return 'NIFTI'
            elif filepath.endswith('.dcm'):
                return 'DICOM'
            elif filepath.endswith(('.mhd', '.raw', '.mha')):
                return 'MetaImage'
            elif filepath.endswith('.nrrd'):
                return 'NRRD'
            elif filepath.endswith(('.hdr', '.img')):
                return 'Analyze'
            else:
                return ''

        for row, volume_id in enumerate(volume_ids):
            # volume info
            volume_info = db.get_volume_info(subject.id, volume_id)
            self.ui.volumesTableWidget.setItem(row, 0, qt.QTableWidgetItem(volume_info["name"]))
            self.ui.volumesTableWidget.setItem(row, 1, qt.QTableWidgetItem(volume_info["id"]))
            self.ui.volumesTableWidget.setItem(row, 2, qt.QTableWidgetItem(infer_format(str(volume_info["data_abspath"]))))

    def updateSessionStatus(self):
        """Update the active session status view and related buttons"""

        session_status_field_widgets = [
            self.ui.sessionStatusSubjectNameIdValueLabel,
            self.ui.sessionStatusSessionNameIdValueLabel,
            self.ui.sessionStatusProtocolValueLabel,
            self.ui.sessionStatusTransducerValueLabel,
            self.ui.sessionStatusVolumeValueLabel,
        ]

        if self._parameterNode is None or self._parameterNode.loaded_session is None:
            for label in session_status_field_widgets:
                label.setText("") # Doing this before setCurrentIndex(0) results in the desired scrolling behavior
                # (Doing it after makes Qt maintain the possibly larger size of page 1 of the collectioned widget, providing unnecessary scroll bars)
            self.ui.sessionStatusStackedWidget.setCurrentIndex(0)
        else:
            loaded_session = self._parameterNode.loaded_session
            session_openlifu : "openlifu.db.Session" = loaded_session.session.session
            subject_openlifu = self.logic.get_subject(session_openlifu.subject_id)
            protocol_openlifu : "openlifu.plan.Protocol" = loaded_session.get_protocol().protocol
            
            self.ui.sessionStatusSubjectNameIdValueLabel.setText(
                f"{subject_openlifu.name} (ID: {session_openlifu.subject_id})"
            )
            self.ui.sessionStatusSessionNameIdValueLabel.setText(
                f"{session_openlifu.name} (ID: {session_openlifu.id})"
            )
            self.ui.sessionStatusProtocolValueLabel.setText(
                f"{protocol_openlifu.name} (ID: {session_openlifu.protocol_id})"
            )
            self.ui.sessionStatusVolumeValueLabel.setText(session_openlifu.volume_id)
            
            # Add a validity check here since this function call is triggered after a transducer is removed but 
            # before a session is invalidated. 
            if loaded_session.transducer_is_valid():
                transducer_openlifu : "openlifu.xdc.Transducer" = loaded_session.get_transducer().transducer.transducer
                self.ui.sessionStatusTransducerValueLabel.setText(
                    f"{transducer_openlifu.name} (ID: {session_openlifu.transducer_id})"
                )

            # Build the additional info message here; this is status text that conditionally displays.
            additional_info_messages : List[str] = []
            # Add a validity check here since this function call is triggered when a session is invalidated. 
            if self.logic.validate_session():
                approved_vf_targets = self.logic.get_virtual_fit_approvals_in_session()
                num_vf_approved = len(approved_vf_targets)
                if num_vf_approved > 0:
                    additional_info_messages.append(
                        "Virtual fit approved for "
                        + (f"{num_vf_approved} targets" if num_vf_approved > 1 else f"target \"{approved_vf_targets[0]}\"")
                    )

                approved_tt_photoscans = self.logic.get_transducer_tracking_approvals_in_session()
                num_tt_approved = len(approved_tt_photoscans)
                if num_tt_approved > 0:
                    additional_info_messages.append(
                        "Transducer localization approved for "
                        + (f"{num_tt_approved} photoscans" if num_tt_approved > 1 else f"photoscan \"{approved_tt_photoscans[0]}\"")
                    )
            self.ui.sessionStatusAdditionalInfoLabel.setText('\n'.join(additional_info_messages))

            self.ui.sessionStatusStackedWidget.setCurrentIndex(1)

    def updateWorkflowControls(self):
        if self.logic.subject is None:
            self.workflow_controls.can_proceed = False
            self.workflow_controls.status_text = "Load a subject to proceed."
        elif self._parameterNode is None or self._parameterNode.loaded_session is None:
            self.workflow_controls.can_proceed = False
            self.workflow_controls.status_text = "Load a session to proceed."
        else:
            self.workflow_controls.can_proceed = True
            self.workflow_controls.status_text = "Session loaded, proceed to the next step."

    def onParameterNodeModified(self, caller, event) -> None:
        # Pass any new session onto the home module global workflow object
        home_module_logic : OpenLIFUHomeLogic = slicer.util.getModuleLogic('OpenLIFUHome')
        if self._parameterNode is None or self._parameterNode.loaded_session is None:
            home_module_logic.workflow.global_session = None
        else:
            home_module_logic.workflow.global_session = self._parameterNode.loaded_session.session

        # Perform module-level updates
        self.updateLoadedObjectsView()
        self.updateSessionStatus()
        self.updateWorkflowControls()

    def cleanup(self) -> None:
        """Called when the application closes and the module widget is destroyed."""
        self.removeObservers()

    def updateNavigationModeComboBox(self) -> None:
        """Sync the Navigation dropdown with the Home module's guided-mode state."""
        try:
            in_guided_mode = bool(get_guided_mode_state())
        except (AttributeError, RuntimeError):
            # Home parameter node not yet available very early in startup.
            return
        combo = self.ui.navigationModeComboBox
        target_index = 1 if in_guided_mode else 0
        if combo.currentIndex == target_index:
            return
        was_blocked = combo.blockSignals(True)
        try:
            combo.setCurrentIndex(target_index)
        finally:
            combo.blockSignals(was_blocked)

    @display_errors
    def onNavigationModeChanged(self, index: int) -> None:
        """Enter or exit guided mode in response to the Navigation dropdown."""
        if index == 1:
            # Defer to the Home logic so the same start-of-workflow gating
            # (login, database selected, etc.) is enforced.
            slicer.util.getModuleLogic("OpenLIFUHome").start_guided_mode()
        else:
            set_guided_mode_state(False)
        # If start_guided_mode rejected the request, snap the dropdown back.
        self.updateNavigationModeComboBox()

    def updatePermissionsModeComboBox(self) -> None:
        """Sync the Permissions dropdown with the Login module's user_account_mode state.

        Safe to call before the Login module is instantiated: if the underlying
        state isn't readable yet we leave the dropdown at its current value.
        """
        try:
            uam = bool(get_user_account_mode_state())
        except (AttributeError, RuntimeError):
            return
        combo = self.ui.permissionsModeComboBox
        target_index = 1 if uam else 0
        if combo.currentIndex == target_index:
            return
        was_blocked = combo.blockSignals(True)
        try:
            combo.setCurrentIndex(target_index)
        finally:
            combo.blockSignals(was_blocked)

    @display_errors
    def onPermissionsModeChanged(self, index: int) -> None:
        """Toggle user account mode in response to the Permissions dropdown."""
        set_user_account_mode_state(index == 1)
        self.updatePermissionsGating()

    def updatePermissionsGating(self) -> None:
        """Gate the body of the Data page when permissions are restricted.

        When Permissions = "User" and the active user is not authenticated
        (i.e. the default anonymous user with no roles), disable the section
        collapsibles below the top toolbar so the user is nudged to log in
        before interacting with the rest of the page. Once they log in (or
        switch back to Unrestricted), the sections re-enable.

        Safe to call before the Login module is instantiated.
        """
        try:
            uam = bool(get_user_account_mode_state())
        except (AttributeError, RuntimeError):
            uam = False
        try:
            cur_user = get_current_user()
            has_role = bool(getattr(cur_user, "roles", None))
        except (AttributeError, RuntimeError):
            has_role = False
        gate_active = uam and not has_role
        # Section collapsibles to gate. The dependencies section is left
        # enabled even when gated so the user can still install requirements.
        gated_sections = [
            self.ui.subjectCollapsibleButton,
            self.ui.protocolsCollapsibleButton,
            self.ui.transducersCollapsibleButton,
            self.ui.objectsCollapsibleButton,
        ]
        for section in gated_sections:
            section.setEnabled(not gate_active)
            if gate_active:
                section.setToolTip(
                    "Permissions is set to 'User'. Log in to access this section."
                )
            else:
                section.setToolTip("")

    def updateToolbarButtonStates(self) -> None:
        """Reflect database / login status in the top-toolbar icon buttons.

        - The Database button gets a red outline when no database is loaded,
          green when a database is connected.
        - The Login button stays unstyled when no user is signed in, and turns
          green when a real (non-anonymous, non-default-admin) user is
          authenticated. It is disabled whenever no database is loaded, since
          login requires database access to look up users.

        Safe to call before the Login module is instantiated.
        """
        # Stylesheet helper: a 2px coloured border around the emoji button
        # without overriding padding (which would crush the glyph's render
        # box and make the icon disappear). The selector is scoped to the
        # specific objectName so it doesn't leak into nested QPushButtons.
        def _outline_style(object_name: str, hex_color: str) -> str:
            return (
                f"QPushButton#{object_name} {{ "
                f"border: 2px solid {hex_color}; "
                f"border-radius: 4px; "
                f"}}"
            )

        # --- Database ---
        try:
            cur_db = get_cur_db()
        except (AttributeError, RuntimeError):
            cur_db = None
        db_connected = cur_db is not None
        if db_connected:
            self.ui.databasePopupButton.setStyleSheet(
                _outline_style("databasePopupButton", "#2e7d32")
            )
            db_path = getattr(cur_db, "path", None) or "(unknown location)"
            self.ui.databasePopupButton.setToolTip(
                f"Database is connected at:\n{db_path}\n\nClick to view or change."
            )
        else:
            self.ui.databasePopupButton.setStyleSheet(
                _outline_style("databasePopupButton", "#c62828")
            )
            self.ui.databasePopupButton.setToolTip(
                "No database is connected. Click to choose a database directory."
            )

        # --- Login ---
        try:
            cur_user = get_current_user()
        except (AttributeError, RuntimeError):
            cur_user = None
        try:
            uam = bool(get_user_account_mode_state())
        except (AttributeError, RuntimeError):
            uam = False
        # Treat "default_admin" and "anonymous" as not-signed-in for display
        # purposes; both are stand-in users that the Login module assigns
        # automatically depending on database / admin presence.
        user_id = getattr(cur_user, "id", None)
        is_real_user = (
            cur_user is not None
            and user_id not in (None, "anonymous", "default_admin")
        )
        if is_real_user:
            self.ui.loginPopupButton.setStyleSheet(
                _outline_style("loginPopupButton", "#2e7d32")
            )
            self.ui.loginPopupButton.setToolTip(
                f"Signed in as {getattr(cur_user, 'name', '') or user_id}. "
                f"Click to open Account."
            )
        elif uam:
            # Permissions = "User" but no real user is signed in. Highlight the
            # button red to nudge the user to log in.
            self.ui.loginPopupButton.setStyleSheet(
                _outline_style("loginPopupButton", "#c62828")
            )
            self.ui.loginPopupButton.setToolTip(
                "Permissions is set to 'User'. Click to open Account and log in."
            )
        else:
            self.ui.loginPopupButton.setStyleSheet("")
            self.ui.loginPopupButton.setToolTip(
                "Not signed in. Click to open Account."
            )

        # Login requires a database; disable the button when no database is
        # loaded (it can't do anything useful in that state).
        self.ui.loginPopupButton.setEnabled(db_connected)
        if not db_connected:
            self.ui.loginPopupButton.setToolTip(
                "Connect a database first; account features look up users from the database."
            )

        # --- Device ---
        #
        # Reflect TX/HV connectivity in the device button outline:
        #
        #   (False, False)          - nothing connected         -> black outline
        #   (True, False) /         - one half connected (TX    -> yellow outline
        #   (False, True)             only or HV only)
        #   (True, True)            - fully connected           -> green outline
        #
        # Reads ``is_device_connected()`` from OpenLIFUSonicationControl's
        # LIFUInterface. If the SonicationControl logic is not yet
        # instantiated (e.g. before its first navigation), we leave the
        # button at its current style. The on-connect / on-disconnect
        # callbacks installed in ``_wireDeferredLoginObservers`` will repaint
        # whenever the underlying state changes.
        try:
            sc_logic = slicer.util.getModuleLogic("OpenLIFUSonicationControl")
            iface = getattr(sc_logic, "cur_lifu_interface", None)
            tx_conn, hv_conn = iface.is_device_connected() if iface is not None else (False, False)
        except (AttributeError, RuntimeError):
            tx_conn, hv_conn = False, False
        if tx_conn and hv_conn:
            self.ui.devicePopupButton.setStyleSheet(
                _outline_style("devicePopupButton", "#2e7d32")  # green
            )
            self.ui.devicePopupButton.setToolTip(
                "Hardware device fully connected (TX + HV). Click for details."
            )
        elif tx_conn or hv_conn:
            self.ui.devicePopupButton.setStyleSheet(
                _outline_style("devicePopupButton", "#f9a825")  # yellow / amber
            )
            half = "TX only" if tx_conn else "HV only"
            self.ui.devicePopupButton.setToolTip(
                f"Hardware partially connected ({half}). Click for details."
            )
        else:
            self.ui.devicePopupButton.setStyleSheet(
                _outline_style("devicePopupButton", "#000000")  # black
            )
            self.ui.devicePopupButton.setToolTip(
                "No hardware device connected. Click for details."
            )

    # ----- Dependency install (moved from OpenLIFULogin) -----

    def _initDependencyStatus(self) -> None:
        """Initialize the openlifu / ADB dependency status indicators.

        Also enforces the collapse defaults for the Dependencies section:

        * The "Optional" sub-section (ADB) is always collapsed initially --
          users rarely need it.
        * The outer Dependencies section is collapsed when openlifu is
          installed and version-matched (the common case) and expanded when
          openlifu is missing or out of date so the user is immediately
          pointed at the "Install/Update Python Requirements" button.
        """
        self._checkOpenLIFUVersionStatus()
        self._checkADBStatus()

        # Optional dependencies (ADB) -- always start collapsed.
        try:
            self.ui.installDependenciesCollapsibleGroupBox.collapsed = True
        except AttributeError:
            pass

        # Outer Dependencies section -- expanded only when openlifu is not OK.
        openlifu_ok = python_requirements_exist() and openlifu_version_matches()
        try:
            self.ui.dependenciesCollapsibleButton.collapsed = bool(openlifu_ok)
            # ctkCollapsibleButton's ``checked`` mirrors ``not collapsed``
            # in the .ui file; keep them in sync defensively.
            self.ui.dependenciesCollapsibleButton.setChecked(not openlifu_ok)
        except AttributeError:
            pass

    def _checkOpenLIFUVersionStatus(self) -> None:
        import importlib.metadata
        has_openlifu = python_requirements_exist()
        version_ok = openlifu_version_matches() if has_openlifu else False

        icon_name = (
            qt.QStyle.SP_DialogApplyButton
            if (has_openlifu and version_ok)
            else qt.QStyle.SP_DialogCancelButton
        )
        pixmap = slicer.app.style().standardIcon(icon_name).pixmap(qt.QSize(16, 16))
        self.ui.openlifuStatusIcon.setPixmap(pixmap)
        self.ui.openlifuStatusIcon.setText("")

        if not has_openlifu:
            self.ui.installPythonRequirementsPushButton.setText("Install Python Requirements")
        elif not version_ok:
            try:
                installed = importlib.metadata.version("openlifu")
            except importlib.metadata.PackageNotFoundError:
                installed = "unknown"
            required = get_required_openlifu_version() or "unknown"
            self.ui.installPythonRequirementsPushButton.setText(
                f"Update Python Requirements (openlifu: {installed} → {required})"
            )
        else:
            try:
                installed = importlib.metadata.version("openlifu")
            except importlib.metadata.PackageNotFoundError:
                installed = "unknown"
            self.ui.installPythonRequirementsPushButton.setText(
                f"Reinstall Python Requirements (openlifu: {installed})"
            )

    @display_errors
    def onUpdateOpenLIFUClicked(self, checked: bool = False) -> None:
        check_and_install_python_requirements(prompt_if_found=True)
        self._checkOpenLIFUVersionStatus()

    def _checkADBStatus(self) -> None:
        try:
            adb_result = subprocess.run(
                ["adb", "--version"], capture_output=True, check=True, text=True
            )
            adb_version = (
                adb_result.stdout.splitlines()[0]
                if adb_result.stdout
                else "unknown version"
            )
            adb_installed = True
        except (subprocess.CalledProcessError, FileNotFoundError):
            adb_installed = False
            adb_version = None

        adb_icon_name = (
            qt.QStyle.SP_DialogApplyButton
            if adb_installed
            else qt.QStyle.SP_DialogCancelButton
        )
        adb_pixmap = slicer.app.style().standardIcon(adb_icon_name).pixmap(qt.QSize(16, 16))
        self.ui.adbStatusIcon.setPixmap(adb_pixmap)
        self.ui.adbStatusIcon.setText("")

        if adb_installed:
            self.ui.installADBPushButton.setEnabled(False)
            self.ui.installADBPushButton.setText(
                f"Android Platform Tools installed ({adb_version})"
            )
        else:
            self.ui.installADBPushButton.setEnabled(True)
            self.ui.installADBPushButton.setText("Install Android Platform Tools")

    @display_errors
    def onInstallADBClicked(self, checked: bool = False) -> None:
        if sys.platform.startswith("win"):
            self._installADBWindows()
        elif sys.platform == "darwin":
            self._installADBMac()
        elif sys.platform.startswith("linux"):
            self._installADBLinux()
        else:
            slicer.util.infoDisplay("ADB installation is not supported on this platform.")

    def _installADBWindows(self) -> None:
        if not slicer.util.confirmYesNoDisplay(
            "This will download Android Platform Tools (~7 MB) from Google "
            "and add the installation directory to your Windows user PATH. Continue?"
        ):
            return

        selected_dir = qt.QFileDialog.getExistingDirectory(
            slicer.util.mainWindow(),
            "Choose installation directory for Android Platform Tools",
            str(Path.home()),
            qt.QFileDialog.ShowDirsOnly,
        )
        if not selected_dir:
            return

        selected_dir = Path(selected_dir)
        self.ui.installADBPushButton.setEnabled(False)
        tmp_dir = None

        try:
            ADB_URL = "https://dl.google.com/android/repository/platform-tools-latest-windows.zip"

            with BusyCursor():
                tmp_dir = tempfile.mkdtemp(prefix="adb_install_")
                zip_path = Path(tmp_dir) / "platform-tools-latest-windows.zip"

                urllib.request.urlretrieve(ADB_URL, str(zip_path))

                with zipfile.ZipFile(str(zip_path), "r") as zf:
                    zf.extractall(str(selected_dir))

                platform_tools_path = str(selected_dir / "platform-tools")

                # Write to Windows user PATH registry key
                # User PATH does not require admin permissions
                import winreg
                with winreg.OpenKey(
                    winreg.HKEY_CURRENT_USER,
                    "Environment",
                    0,
                    winreg.KEY_READ | winreg.KEY_WRITE,
                ) as reg_key:
                    try:
                        current_path, _ = winreg.QueryValueEx(reg_key, "Path")
                    except FileNotFoundError:
                        current_path = ""
                    entries = [p for p in current_path.split(os.pathsep) if p]
                    if platform_tools_path not in entries:
                        entries.append(platform_tools_path)
                        winreg.SetValueEx(
                            reg_key,
                            "Path",
                            0,
                            winreg.REG_EXPAND_SZ,
                            os.pathsep.join(entries),
                        )

                # Patch the current process's PATH so the re-check below works immediately
                os.environ["PATH"] = (
                    os.environ.get("PATH", "") + os.pathsep + platform_tools_path
                )

        finally:
            if tmp_dir is not None:
                import shutil
                shutil.rmtree(tmp_dir, ignore_errors=True)
            self._checkADBStatus()

    def _installADBMac(self) -> None:
        if not slicer.util.confirmYesNoDisplay(
            "This will run 'brew install android-platform-tools' to install ADB. "
            "Homebrew must already be installed. Continue?"
        ):
            return

        self.ui.installADBPushButton.setEnabled(False)
        self.ui.installADBPushButton.setText("Installing via Homebrew...")
        slicer.app.processEvents()

        try:
            with BusyCursor():
                result = subprocess.run(
                    ["brew", "install", "android-platform-tools"],
                    capture_output=True,
                    text=True,
                )
            if result.returncode != 0:
                slicer.util.errorDisplay(
                    f"Homebrew installation failed:\n{result.stderr or result.stdout}"
                )
        finally:
            self._checkADBStatus()

    def _installADBLinux(self) -> None:
        slicer.util.infoDisplay(
            "To install ADB on Debian-based Linux, run the following commands in a terminal:\n\n"
            "    sudo apt update\n"
            "    sudo apt install android-tools-adb\n\n"
            "After installing, reopen the application to verify."
        )

    @display_errors
    def onOpenLoginPopup(self, checked: bool = False) -> None:
        """Open the Login module's widget in a modal popup dialog."""
        dialog = _ModuleWidgetPopupDialog(
            module_name="OpenLIFULogin",
            title="Account",
            parent=slicer.util.mainWindow(),
        )
        dialog.exec_()

    @display_errors
    def onOpenDatabasePopup(self, checked: bool = False) -> None:
        """Open the Database module's widget in a modal popup dialog."""
        dialog = _ModuleWidgetPopupDialog(
            module_name="OpenLIFUDatabase",
            title="Database",
            parent=slicer.util.mainWindow(),
        )
        dialog.exec_()

    @display_errors
    def onOpenDevicePopup(self, checked: bool = False) -> None:
        """Show a compact info dialog with the current TX / HV device state.

        Queries ``hvcontroller`` and ``txdevice`` on the OpenLIFUSonicationControl
        module's ``LIFUInterface`` for hardware id and firmware version, plus
        per-TX-module hardware id and firmware version. All I/O calls are
        defensive: any failure is reported inline as "unknown" rather than
        aborting the dialog (the SDK raises ``LIFUError`` on bus errors).
        """
        try:
            sc_logic = slicer.util.getModuleLogic("OpenLIFUSonicationControl")
            iface = getattr(sc_logic, "cur_lifu_interface", None)
        except (AttributeError, RuntimeError):
            iface = None
        if iface is None:
            slicer.util.infoDisplay(
                text=(
                    "The LIFU interface has not been initialized yet.\n\n"
                    "Open the OpenLIFU Sonication Control module once to "
                    "initialize the hardware interface."
                ),
                windowTitle="Device Status",
            )
            return
        try:
            tx_conn, hv_conn = iface.is_device_connected()
        except Exception as e:
            slicer.util.errorDisplay(
                text=f"Could not query device connection status: {e}",
                windowTitle="Device Status",
            )
            return

        # Connection summary
        if tx_conn and hv_conn:
            summary = "Hardware device is fully connected."
        elif tx_conn or hv_conn:
            half = "TX only" if tx_conn else "HV only"
            summary = f"Hardware device is partially connected ({half})."
        else:
            summary = "No hardware device is currently connected."

        lines: List[str] = [summary, ""]

        # Helper: encode a raw-hex HWID into base58, matching the test app's
        # lifu_console.py / lifu_transmitter.py pattern. Important detail:
        # the test app slices the raw-hex *string* to HW_ID_DATA_LENGTH (=12)
        # characters before decoding (=> first 6 bytes, ~8 base58 chars). The
        # raw hex returned by the SDK is 24 chars (12 bytes), but only the
        # first half is used for the display ID. base58 ships with the
        # openlifu_sdk environment; if for some reason it isn't importable we
        # fall back to the raw hex string instead of failing.
        def _encode_hwid_b58(raw_hex: str) -> str:
            try:
                import base58
                from openlifu_sdk.io.LIFUConfig import HW_ID_DATA_LENGTH
                return base58.b58encode(
                    bytes.fromhex(raw_hex[:HW_ID_DATA_LENGTH])
                ).decode("utf-8")
            except Exception as enc_err:
                logging.warning("Could not base58-encode HWID %r: %s", raw_hex, enc_err)
                return raw_hex

        # ---- Console (HV controller) ----
        lines.append("Console:")
        if hv_conn:
            hv = iface.hvcontroller
            try:
                con_hwid_hex = hv.get_hardware_id(raw_hex=True)
                con_hwid = _encode_hwid_b58(con_hwid_hex)
            except Exception as e:
                logging.warning("Could not read console HWID: %s", e)
                con_hwid = "unknown"
            try:
                con_ver = hv.get_version()
            except Exception as e:
                logging.warning("Could not read console firmware version: %s", e)
                con_ver = "unknown"
            lines.append(f"  HWID: {con_hwid}")
            lines.append(f"  Firmware: v{con_ver}")
        else:
            lines.append("  (not connected)")

        lines.append("")

        # ---- TX device + per-module info ----
        lines.append("TX device:")
        if tx_conn:
            tx = iface.txdevice
            try:
                module_count = tx.get_module_count()
            except Exception as e:
                logging.warning("Could not read TX module count: %s", e)
                module_count = 0
            lines.append(f"  Modules connected: {module_count}")
            for module_idx in range(module_count):
                lines.append(f"  Module {module_idx}:")
                try:
                    mod_hwid_hex = tx.get_hardware_id(module=module_idx, raw_hex=True)
                    mod_hwid = _encode_hwid_b58(mod_hwid_hex)
                except Exception as e:
                    logging.warning("Could not read TX module %d HWID: %s", module_idx, e)
                    mod_hwid = "unknown"
                try:
                    mod_ver = tx.get_version(module=module_idx)
                except Exception as e:
                    logging.warning("Could not read TX module %d firmware: %s", module_idx, e)
                    mod_ver = "unknown"
                lines.append(f"    HWID: {mod_hwid}")
                lines.append(f"    Firmware: v{mod_ver}")
        else:
            lines.append("  (not connected)")

        slicer.util.infoDisplay(
            text="\n".join(lines),
            windowTitle="Device Status",
        )

    @display_errors
    def onManageTransducersClicked(self, checked: bool = False) -> None:
        """Open the Transducer manager popup.

        Requires a loaded database. The dialog presents the transducers stored
        in that database in a table and offers add-from-file / add-from-device
        / preview / delete actions.
        """
        db = get_cur_db()
        if db is None:
            slicer.util.errorDisplay(
                "A database must be loaded before the Transducer manager can be opened.",
                windowTitle="Manage Transducers",
            )
            return
        dlg = TransducerManagerDialog(db=db, parent=slicer.util.mainWindow())
        dlg.exec_()

    def enter(self) -> None:
        """Called each time the user opens this module."""
        ensure_python_requirements_for_module_enter()
        # Make sure parameter node exists and observed
        self.initializeParameterNode()
        self.updateWorkflowControls()
        self.updateNavigationModeComboBox()
        self.updatePermissionsModeComboBox()
        self.updatePermissionsGating()
        self.updateToolbarButtonStates()
        # If a database directory was remembered but auto-connect did not run
        # at startup (e.g. the Database logic wasn't reachable yet), try again
        # on each entry into the module so the user sees the database loaded.
        self._tryAutoConnectDatabase()

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
        self.setupSHNodeObserver()

    @vtk.calldata_type(vtk.VTK_INT)
    def onSHItemAboutToBeRemoved(self, caller, event, removedItemID):
        #If any SlicerOpenLIFUTransducer or Slicer OpenLIFUPhotoscan objects relied on the removed item, then we need to remove them
        # as they are now invalid.
        shNode = slicer.mrmlScene.GetSubjectHierarchyNode()

        if shNode.GetItemLevel(removedItemID) == 'Folder':
        # If the item being deleted is the parent folder of a SlicerOpenLIFUTransducer, then the affiliated
        # nodes do not need to be manually removed, but the transducer needs to be removed from the
        # loaded_transducers list. 
            folder_transducer_id = shNode.GetItemAttribute(removedItemID,'transducer_id')
            if folder_transducer_id:
                self.logic.on_transducer_affiliated_folder_about_to_be_removed(folder_transducer_id)
        else:
            removed_node = shNode.GetItemDataNode(removedItemID)
            
            if removed_node.IsA('vtkMRMLTransformNode'):
                self.logic.on_transducer_affiliated_node_about_to_be_removed(removed_node.GetID(),['transform_node'])

            if removed_node.IsA('vtkMRMLModelNode'):
                self.logic.on_transducer_affiliated_node_about_to_be_removed(removed_node.GetID(),['model_node', 'body_model_node','surface_model_node'])
                self.logic.on_photoscan_affiliated_node_about_to_be_removed(removed_node.GetID(),['model_node'])
            
            if removed_node.IsA('vtkMRMLVectorVolumeNode'):
                self.logic.on_photoscan_affiliated_node_about_to_be_removed(removed_node.GetID(),['texture_node'])

    @vtk.calldata_type(vtk.VTK_OBJECT)
    def onNodeRemoved(self, caller, event, node : slicer.vtkMRMLNode) -> None:

        # If the volume of the active session was removed, the session becomes invalid.
        if node.IsA('vtkMRMLVolumeNode'):
            self.logic.validate_session()
            self.logic.validate_solution()

        if node.IsA('vtkMRMLMarkupsFiducialNode'):
            self.unwatch_fiducial_node(node)

        self.updateLoadedObjectsView()

    @vtk.calldata_type(vtk.VTK_OBJECT)
    def onNodeAdded(self, caller, event, node : slicer.vtkMRMLNode) -> None:
        if node.IsA('vtkMRMLMarkupsFiducialNode'):
            self.watch_fiducial_node(node)

        self.updateLoadedObjectsView()

    def watch_fiducial_node(self, node:vtkMRMLMarkupsFiducialNode):
        """Add observers so that point-list changes in this fiducial node are tracked by the module."""
        self.node_observations[node.GetID()].append(node.AddObserver(slicer.vtkMRMLMarkupsNode.PointAddedEvent,self.onPointAddRemoveRename))
        self.node_observations[node.GetID()].append(node.AddObserver(slicer.vtkMRMLMarkupsNode.PointRemovedEvent,self.onPointAddRemoveRename))
        self.node_observations[node.GetID()].append(node.AddObserver(SlicerOpenLIFUEvents.TARGET_NAME_MODIFIED_EVENT,self.onPointAddRemoveRename))

    def unwatch_fiducial_node(self, node:vtkMRMLMarkupsFiducialNode):
        """Un-does watch_fiducial_node; see watch_fiducial_node."""
        if node.GetID() not in self.node_observations:
            return
        for tag in self.node_observations.pop(node.GetID()):
            node.RemoveObserver(tag)

    def onPointAddRemoveRename(self, caller, event) -> None:
        self.updateLoadedObjectsView()

    def initializeParameterNode(self) -> None:
        """Ensure parameter node exists and observed."""
        # Parameter node stores all user choices in parameter values, node selections, etc.
        # so that when the scene is saved and reloaded, these settings are restored.

        self.setParameterNode(self.logic.getParameterNode())
        self.updateParametersFromSettings()
    
    def setupSHNodeObserver(self) -> None:
        """Set and observe the subject hierarchy node.
        Observation is needed so that certain actions can take place when an openlifu affiliated node or folder is removed from the scene.
        """
        shNode = slicer.mrmlScene.GetSubjectHierarchyNode()
        shNode.AddObserver(shNode.SubjectHierarchyItemAboutToBeRemovedEvent, self.onSHItemAboutToBeRemoved)

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
            self.addObserver(self._parameterNode, vtk.vtkCommand.ModifiedEvent, self.onParameterNodeModified)


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

        self._folder_deletion_in_progress = False
        self._timer = None 
        
        # To avoid triggering certain events related to nodes being added/removed.
        self.session_loading_unloading_in_progress = False # Used in pre-planning module

        self._subject = None
        """The currently loaded subject. Do not set this directly -- use the `subject` property."""

        self._on_subject_changed_callbacks : List[Callable[[Optional["openlifu.db.Subject"]],None]] = []
        """List of functions to call when `subject` property is changed."""

    def getParameterNode(self):
        return OpenLIFUDataParameterNode(super().getParameterNode())

    def call_on_subject_changed(self, f : Callable[[Optional["openlifu.db.Subject"]],None]) -> None:
        """Set a function to be called whenever the `subject` property is changed.
        The provided callback should accept a single argument which will be the new loaded subject (or None if cleared).
        """
        self._on_subject_changed_callbacks.append(f)

    @property
    def subject(self) -> Optional["openlifu.db.Subject"]:
        """The currently loaded subject.

        Callbacks registered with `call_on_subject_changed` will be invoked when the subject changes.

        This mechanism does not interfere with the parameter-node level session loading in the parameter node
        (see SlicerOpenLIFUSession), which allows saving progress through Slicer scene loading/unloading.
        The loading mechanism for subjects only limits the sessions available to be loaded.
        """
        return self._subject

    @subject.setter
    def subject(self, subject_value : Optional["openlifu.db.Subject"]):
        self._subject = subject_value
        for f in self._on_subject_changed_callbacks:
            f(self._subject)

    def clear_session(self, clean_up_scene:bool = True) -> None:
        """Unload the current session if there is one loaded.

        Args:
            clean_up_scene: Whether to remove the existing session's affiliated scene content.
                If False then the scene content is orphaned from its session, as though
                it was manually loaded without the context of a session. If True then the scene
                content is removed.
        """

        self.session_loading_unloading_in_progress = True

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
            if (
                self.getParameterNode().loaded_solution is not None
                and loaded_session.last_generated_solution_id == self.getParameterNode().loaded_solution.solution.solution.id
            ):
                self.clear_solution(clean_up_scene=True)
            clear_virtual_fit_results(session_id = loaded_session.get_session_id(), target_id=None)
            for photocollection_id in loaded_session.get_affiliated_photocollection_ids():
                if photocollection_id in self.getParameterNode().session_photocollections:
                    self.remove_photocollection(photocollection_id)
            
            for photoscan_id in loaded_session.get_affiliated_photoscan_ids():
                if photoscan_id in self.getParameterNode().loaded_photoscans:
                    self.remove_photoscan(photoscan_id)

            clear_transducer_tracking_results(session_id = loaded_session.get_session_id())

            self.session_loading_unloading_in_progress = False

    def save_session(self) -> None:
        """Save the current session to the openlifu database.
        This first writes the transducer and target information into the in-memory openlifu Session object,
        and then it writes that Session object and any affiliated Photoscan objects to the database.
        """

        if get_cur_db() is None:
            raise RuntimeError("Cannot save session because there is no database connection")

        if self.subject is None:
            raise RuntimeError("Cannot save session because there is no subject loaded")

        if not self.validate_session():
            raise RuntimeError("Cannot save session because there is no active session, or the active session was invalid.")

        session_openlifu = self.update_underlying_openlifu_session()

        import openlifu.db.database

        OnConflictOpts : "openlifu.db.database.OnConflictOpts" = openlifu.db.database.OnConflictOpts
        get_cur_db().write_session(self.subject,session_openlifu,on_conflict=OnConflictOpts.OVERWRITE)

        # Write any affiliated photoscan objects
        for photoscan in self.getParameterNode().loaded_session.get_affiliated_photoscans():
            get_cur_db().write_photoscan(session_openlifu.subject_id, session_openlifu.id, photoscan, on_conflict=OnConflictOpts.OVERWRITE)

    def update_underlying_openlifu_session(self) -> "openlifu.db.Session":
        """Update the underlying openlifu session of the currently loaded session, if there is one.
        Returns the newly updated openlifu Session object."""
        parameter_node = self.getParameterNode()

        if parameter_node.loaded_session is None:
            raise RuntimeError("Cannot save session because OpenLIFUDataParameterNode.loaded_session is None")

        session : SlicerOpenLIFUSession = parameter_node.loaded_session
        if session is not None:
            targets = get_target_candidates()
            # TODO: I think instead of getting all 1-point fiducial nodes as targets, we should attribute-tag targets with
            # the session ID, and have a tool that adds and retrieves targets by session ID similar to what we do for virtual fit results
            session_openlifu = session.update_underlying_openlifu_session(targets)
            parameter_node.loaded_session = session # remember to write the updated session into the parameter node
            return session_openlifu


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

    def validate_solution(self) -> bool:
        """Check to ensure that the currently active solution is in a valid state, clearing out the solution
        if it is not and returning whether there is an active valid solution."""

        solution = self.getParameterNode().loaded_solution

        if solution is None:
            return False # There is no active solution, no problem

        # Check volumes are present
        for volume_node in [solution.intensity, solution.pnp]:
            if volume_node is None or slicer.mrmlScene.GetNodeByID(volume_node.GetID()) is None:
                clean_up_scene = ObjectBeingUnloadedMessageBox(
                    message="A volume that was in use by the active solution is now missing. The solution will be unloaded.",
                    title="Solution invalidated",
                ).customexec_()
                self.clear_solution(clean_up_scene=clean_up_scene)
                return False

        return True

    def get_subject(self, subject_id:str) -> "openlifu.db.subject.Subject":
        """Get the Subject with a given ID"""
        if get_cur_db() is None:
            raise RuntimeError("Unable to fetch subject info because there is no loaded database.")
        return get_cur_db().load_subject(subject_id)

    def get_sessions(self, subject_id:str) -> "List[openlifu.db.session.Session]":
        """Get the collection of Sessions associated with a given subject ID"""
        if get_cur_db() is None:
            raise RuntimeError("Unable to fetch session info because there is no loaded database.")
        return [
            get_cur_db().load_session(
                self.get_subject(subject_id),
                session_id
            )
            for session_id in ensure_list(get_cur_db().get_session_ids(subject_id))
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
        if get_cur_db() is None:
            raise RuntimeError("Unable to fetch session info because there is no loaded database.")
        return get_cur_db().load_session(self.get_subject(subject_id), session_id)

    def get_current_session_transducer_id(self) -> Optional[str]:
        """Get the transducer ID of the current session, if there is a current session. Returns None
        if there isn't a current session."""
        if self.getParameterNode().loaded_session is None:
            return None
        return self.getParameterNode().loaded_session.get_transducer_id()

    def get_current_session_volume_id(self) -> Optional[str]:
        """Get the volume ID of the current session, if there is a current session. Returns None
        if there isn't a current session."""
        if self.getParameterNode().loaded_session is None:
            return None
        return self.getParameterNode().loaded_session.get_volume_id()

    def update_photocollections_affiliated_with_loaded_session(self) -> None:

        loaded_session = self.getParameterNode().loaded_session
        subject_id = loaded_session.get_subject_id()
        session_id = loaded_session.get_session_id()
        
        # Keep track of any photocollections associated with the session
        affiliated_photocollections = get_cur_db().get_photocollection_reference_numbers(subject_id, session_id)
        if affiliated_photocollections:
            loaded_session.set_affiliated_photocollections(affiliated_photocollections)

    def update_photoscans_affiliated_with_loaded_session(self) -> None:

        loaded_session = self.getParameterNode().loaded_session
        subject_id = loaded_session.get_subject_id()
        session_id = loaded_session.get_session_id()
        
        # Keep track of any photoscans associated with the session
        affiliated_photoscans = {id:get_cur_db().load_photoscan(subject_id, session_id, id) for id in get_cur_db().get_photoscan_ids(subject_id, session_id)}
        if affiliated_photoscans:
            loaded_session.set_affiliated_photoscans(affiliated_photoscans)

    def load_session(self, subject_id, session_id) -> None:

        # Certain modules need to have their widgets already set up, if they were not, before loading a session.
        # This is because those module widgets set up observers on certain kinds of nodes as those nodes are added to the scene.
        # If the widgets don't exist when a session is loaded, they will not get a chance to add their observers.
        slicer.util.getModule("OpenLIFUPrePlanning").widgetRepresentation()
        slicer.util.getModule("OpenLIFUTransducerLocalization").widgetRepresentation()
        slicer.util.getModule("OpenLIFUSonicationPlanner").widgetRepresentation()

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

        loaded_volumes = slicer.util.getNodesByClass('vtkMRMLScalarVolumeNode')
        loaded_volume_ids = [volume_node.GetAttribute('OpenLIFUData.volume_id') if volume_node.GetAttribute('OpenLIFUData.volume_id') else volume_node.GetID() for volume_node in loaded_volumes]
        if (
            session_openlifu.volume_id in loaded_volume_ids
            and (
                loaded_session is None
                or session_openlifu.volume_id != loaded_session.get_volume_id()
                # (we are okay reloading the volume if it's just the one affiliated with the session, since user already decided to replace the session)
            )
        ):
            if not slicer.util.confirmYesNoDisplay(
                f"Loading this session will replace the already loaded volume with ID {session_openlifu.volume_id}. Proceed?",
                "Confirm replace volume"
            ):
                return
            else:
                # Remove the volume already in the scene
                idx = loaded_volume_ids.index(session_openlifu.volume_id)
                slicer.mrmlScene.RemoveNode(loaded_volumes[idx])

        session_affiliated_photoscans = get_cur_db().get_photoscan_ids(subject_id, session_id)
        if loaded_session is not None:
            # (we are okay reloading photoscans if they are just the one affiliated with the session, since user already decided to replace the session)
            loaded_session_affiliated_photoscans = get_cur_db().get_photoscan_ids(loaded_session.get_subject_id(), loaded_session.get_session_id())
            conflicting_loaded_photoscans =  [photoscan for photoscan in session_affiliated_photoscans 
                                              if photoscan in self.getParameterNode().loaded_photoscans and 
                                              photoscan not in loaded_session_affiliated_photoscans]
        else:
            conflicting_loaded_photoscans =  [photoscan for photoscan in session_affiliated_photoscans if photoscan in self.getParameterNode().loaded_photoscans]
        if (
            conflicting_loaded_photoscans
        ):
            if not slicer.util.confirmYesNoDisplay(
                f"Loading this session will replace the already loaded photoscan(s) with ID(s) {conflicting_loaded_photoscans}. Proceed?",
                "Confirm replace photoscan"
            ):
                return
        
        # === Proceed with loading session ===

        self.clear_session()

        self.session_loading_unloading_in_progress = True  

        volume_info = get_cur_db().get_volume_info(session_openlifu.subject_id, session_openlifu.volume_id)

       # Create the SlicerOpenLIFU session object; this handles loading volume and targets
        new_session = SlicerOpenLIFUSession.initialize_from_openlifu_session(
            session_openlifu,
            volume_info
        )

        # === Load transducer ===

        transducer_openlifu = get_cur_db().load_transducer(session_openlifu.transducer_id)
        transducer_abspaths_info = get_cur_db().get_transducer_absolute_filepaths(session_openlifu.transducer_id)
        newly_loaded_transducer = self.load_transducer_from_openlifu(
            transducer = transducer_openlifu,
            transducer_abspaths_info = transducer_abspaths_info,
            transducer_matrix = session_openlifu.array_transform.matrix,
            transducer_matrix_units = session_openlifu.array_transform.units,
            replace_confirmed = True,
        )
        newly_loaded_transducer.set_visibility(False)

        # === Load protocol ===

        self.load_protocol_from_openlifu(
            get_cur_db().load_protocol(session_openlifu.protocol_id),
            replace_confirmed = True,
        )

        # === Load virtual fit results ===

        newly_added_vf_result_nodes = add_virtual_fit_results_from_openlifu_session_format(
            vf_results_openlifu = session_openlifu.virtual_fit_results,
            session_id = session_openlifu.id,
            transducer = newly_loaded_transducer.transducer.transducer,
            replace=True, # If there happen to already be some virtual fit result nodes that clash, loading a session will silently overwrite them.
        )

        for vf_node in newly_added_vf_result_nodes:
            preplanning_widget : OpenLIFUPrePlanningWidget = slicer.modules.OpenLIFUPrePlanningWidget
            preplanning_widget.watchVirtualFit(vf_node)

            # Place virtual fit results under the transducer folder
            newly_loaded_transducer.move_node_into_transducer_sh_folder(vf_node)
            
            # Check if the current transducer transform matches the virtual fit result in terms of matrix values.
            if newly_loaded_transducer.is_matching_transform(vf_node):
                newly_loaded_transducer.set_matching_transform(vf_node)
                newly_loaded_transducer.set_visibility(True)
                slicer.util.getModuleLogic("OpenLIFUPrePlanning").chosen_virtual_fit = vf_node

        # === Load transducer localization results ===

        newly_added_tt_result_nodes = add_transducer_tracking_results_from_openlifu_session_format(
            tt_results_openlifu = session_openlifu.transducer_tracking_results,
            session_id = session_openlifu.id,
            transducer = newly_loaded_transducer.transducer.transducer,
            replace=True, # If there happen to already be some transducer localization result nodes that clash, loading a session will silently overwrite them.
        )

        for (transducer_to_volume_node, photoscan_to_volume_node) in newly_added_tt_result_nodes:
            transducer_tracking_widget = slicer.modules.OpenLIFUTransducerLocalizationWidget
            transducer_tracking_widget.watchTransducerTrackingNode(transducer_to_volume_node)
            transducer_tracking_widget.watchTransducerTrackingNode(photoscan_to_volume_node)

            newly_loaded_transducer.move_node_into_transducer_sh_folder(transducer_to_volume_node)
            newly_loaded_transducer.move_node_into_transducer_sh_folder(photoscan_to_volume_node)

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

        # === Set camera ===

        threeDView = slicer.app.layoutManager().threeDWidget(0).threeDView()
        threeDView.resetCamera()
        threeDView.resetFocalPoint()

        # === Set the newly created session as the currently active session ===

        self.getParameterNode().loaded_session = new_session

        # === Keep track of affiliated photoscans and unload any conflicting photoscans that have been previously loaded ===
        self.update_photoscans_affiliated_with_loaded_session()

        # === Load photocollections as all scan_ids ===
        session_affiliated_photocollections = get_cur_db().get_photocollection_reference_numbers(subject_id, session_id)
        self.getParameterNode().session_photocollections = session_affiliated_photocollections

        # If there are any *approved* transducer localization results that we have just loaded in newly_added_tt_result_nodes,
        # then we check to see if any of them match the current transducer transform in terms of matrix values.
        # If there is a match in matrix values, then the first such matching TT result that we encounter in the loop is 
        # "officially" linked to the current transform by setting the "matching_transform" attribute, thereby ensuring that
        # TT approval is revoked if the transducer is moved.
        # Additionally, any other transducer localization results whose matrix does not match current transducer get their approval revoked.
        transducer_tracking_widget = slicer.modules.OpenLIFUTransducerLocalizationWidget
        approved_photoscan_ids = self.getParameterNode().loaded_session.get_transducer_tracking_approvals()
        # approved_photoscan_ids is a list of photoscan IDs for which there is an approved TT result in the openlifu session
        for approved_photoscan_id in approved_photoscan_ids:

            for transducer_to_volume_node, _ in newly_added_tt_result_nodes:
                current_photoscan_id = get_photoscan_id_from_transducer_tracking_result(transducer_to_volume_node)
                if current_photoscan_id == approved_photoscan_id:
                    if newly_loaded_transducer.is_matching_transform(transducer_to_volume_node):
                        newly_loaded_transducer.set_matching_transform(transducer_to_volume_node)
                        newly_loaded_transducer.set_visibility(True)
                    else:
                        transducer_tracking_widget.revokeTransducerTrackingApprovalIfAny(
                            photoscan_id=approved_photoscan_id,
                            reason="The transducer transform does not match the approved localization result."
                        )

        self.session_loading_unloading_in_progress = False  

    # TODO: This should be a widget level function
    def _on_transducer_transform_modified(self, transducer: SlicerOpenLIFUTransducer) -> None:

        slicer.util.getModuleWidget('OpenLIFUSonicationPlanner').deleteSolutionAndSolutionAnalysisIfAny(reason="The transducer was moved.")
        slicer.util.getModuleWidget('OpenLIFUTransducerLocalization').checkCanDisplayVirtualFitResult()

        matching_transform_id = transducer.transform_node.GetAttribute("matching_transform")
        if matching_transform_id:
            # If its a transducer localization node, revoke approval if approved
            transform_node = slicer.mrmlScene.GetNodeByID(matching_transform_id)
            if transform_node and is_transducer_tracking_result_node(transform_node):
                photoscan_id = get_photoscan_id_from_transducer_tracking_result(transform_node)
                transducer_tracking_widget = slicer.modules.OpenLIFUTransducerLocalizationWidget
                transducer_tracking_widget.revokeTransducerTrackingApprovalIfAny(
                    photoscan_id = photoscan_id,
                    reason = "The transducer transform was modified"
                )
            
            transducer.set_matching_transform(None)


    def load_protocol_from_file(self, filepath:str) -> None:
        import openlifu.plan

        protocol = openlifu.plan.Protocol.from_file(filepath)
        self.load_protocol_from_openlifu(protocol)

    def load_protocol_from_openlifu(self, protocol:"openlifu.plan.Protocol", replace_confirmed: bool = False) -> None:
        """Load an openlifu protocol object into the scene as a SlicerOpenLIFUProtocol,
        adding it to the list of loaded openlifu objects. If there are
        changes in the protocol config, also confirms user wants to discard
        changes.

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

        # check if user wants to overwrite WIPs
        protocolConfigLogic = slicer.util.getModuleLogic('OpenLIFUProtocolConfig')
        if not protocolConfigLogic.confirm_and_overwrite_protocol_cache(protocol):
            return

        self.getParameterNode().loaded_protocols[protocol.id] = SlicerOpenLIFUProtocol(protocol)

    def remove_protocol(self, protocol_id:str) -> None:
        """Remove a protocol from the list of loaded protocols. If there are
        changes in the protocol config, also confirms user wants to discard
        changes."""
        loaded_protocols = self.getParameterNode().loaded_protocols
        if not protocol_id in loaded_protocols:
            raise IndexError(f"No protocol with ID {protocol_id} appears to be loaded; cannot remove it.")

        # check if user wants to save changes
        protocolConfigLogic = slicer.util.getModuleLogic('OpenLIFUProtocolConfig')
        if protocolConfigLogic.protocol_id_is_in_cache(protocol_id):
            if slicer.util.confirmYesNoDisplay(
                text=f"You have unsaved changes in the protocol you are about to remove. Do you want to save changes?",
                windowTitle="Save Changes Confirmation",
            ):
                protocolConfigLogic.save_protocol_to_database(protocolConfigLogic.cached_protocols[protocol_id])


        loaded_protocols.pop(protocol_id)
        # We must delete from cache after because parameter node update might add it back to cache
        protocolConfigLogic.delete_protocol_from_cache(protocol_id)

    def load_transducer_from_file(self, filepath:str) -> None:
        import openlifu.xdc.util

        transducer = openlifu.xdc.util.load_transducer_from_file(filepath, convert_array=True)
        transducer_parent_dir = Path(filepath).parent

        transducer_abspaths_info = {
            key: transducer_parent_dir.joinpath(filename) if filename else None
            for key, filename in [
                ('transducer_body_abspath', transducer.transducer_body_filename),
                ('registration_surface_abspath', transducer.registration_surface_filename),
            ]
        }
        
        newly_loaded_transducer = self.load_transducer_from_openlifu(transducer, transducer_abspaths_info)

    def load_transducer_from_openlifu(
            self,
            transducer: "openlifu.xdc.Transducer",
            transducer_abspaths_info: dict = {},
            transducer_matrix: Optional[np.ndarray]=None,
            transducer_matrix_units: Optional[str]=None,
            replace_confirmed: bool = False,
        ) -> SlicerOpenLIFUTransducer:
        """Load an openlifu transducer object into the scene as a SlicerOpenLIFUTransducer,
        adding it to the list of loaded openlifu objects.

        Args:
            transducer: The openlifu Transducer object
            transducer_abspaths_info: Dictionary containing absolute filepath info to any data affiliated with the transducer object.
                This includes 'transducer_body_abspath' and 'registration_surface_abspath'. The registration surface model is required for
                running the transducer localization algorithm. If left as empty, the registration surface and transducer body models affiliated 
                with the transducer will not be loaded.
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
            transducer_abspaths_info,
            transducer_matrix=transducer_matrix,
            transducer_matrix_units=transducer_matrix_units,
        )
        self.getParameterNode().loaded_transducers[transducer.id] = newly_loaded_transducer

        newly_loaded_transducer.observe_transform_modified(self._on_transducer_transform_modified)

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

    def on_transducer_affiliated_folder_about_to_be_removed(self, folder_transducer_id: str) -> None:
        """Handle cleanup on SlicerOpenLIFUTransducer objects when the mrml nodes they depend on get removed from the scene. 
        A folder level deletion by manual mrml scene manipulation automatically clears all of the nodes under the folder.

        Args:
            folder_transducer_id: Transducer affiliated folders have a 'transudcer_id' attribute that matches the 
                ID of the affiliated transducer object.
        """
        
        # Set the folder deletion flag to true
        self._folder_deletion_in_progress = True

        matching_transducer_openlifu_ids = [
            transducer_openlifu_id
            for transducer_openlifu_id, _ in self.getParameterNode().loaded_transducers.items()
            if transducer_openlifu_id == folder_transducer_id 
            ]

        # If this fails, then  this folder was shared across multiple loaded SlicerOpenLIFUTransducers, which
        # should not be possible in the application logic.
        assert(len(matching_transducer_openlifu_ids) <= 1)

        if matching_transducer_openlifu_ids:

            transducer_openlifu_id = matching_transducer_openlifu_ids[0]
            slicer.util.warningDisplay(
                text = f"The transducer with id {transducer_openlifu_id} will be unloaded because affiliated nodes were removed from the scene.",
                windowTitle="Transducer removed", parent = slicer.util.mainWindow()
            )
            self.remove_transducer(transducer_openlifu_id, clean_up_scene=False)

            # If the transducer that was just removed was in use by an active session, invalidate that session
            self.validate_session()
        
        # Reset the flag back to false
        self._folder_deletion_in_progress = False
        self._timer = None

    def on_transducer_affiliated_node_about_to_be_removed(self, node_mrml_id:str, affiliated_node_attribute_names: List[str]) -> None:
        """Handle cleanup on SlicerOpenLIFUTransducer objects when the mrml nodes they depend on get removed from the scene.

        Args:
            node_mrml_id: The mrml scene ID of the node that was (or is about to be) removed
            affiliated_node_attribute_name: List of the possible names of the affected vtkMRMLNode-valued SlicerOpenLIFUTransducerNode attribute
                (so "transform_node" or "model_node" or "body_model_node" or "surface_model_node")
        """
        matching_transducer_openlifu_ids = [
            transducer_openlifu_id
            for transducer_openlifu_id, transducer in self.getParameterNode().loaded_transducers.items()
            for attribute_name in affiliated_node_attribute_names 
            if getattr(transducer,attribute_name) if getattr(transducer,attribute_name).GetID() == node_mrml_id 
        ]

        # If this fails, then a single mrml node was shared across multiple loaded SlicerOpenLIFUTransducers, which
        # should not be possible in the application logic.
        assert(len(matching_transducer_openlifu_ids) <= 1)

        # After a delay, check if the flag indicating folder deletion has been set to True. 
        # If False, then only the node was removed by manual manipulation and manual clean up of affiliated
        # transform nodes can take place if wanted.  
        def delayed_node_deletion():
            if not self._folder_deletion_in_progress:
                #Remove the transducer, but keep any other nodes under it. This transducer was removed
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
                
                # Reset flags
                self._timer = None

        # When a folder is deleted, multiple calls to this function are triggered by each transducer affiliated node. 
        # We include a check for an existing timer to prevent each transducer node from creating its own timer. 
        if matching_transducer_openlifu_ids and self._timer is None:
            # Wait before checking if the _folder_deletion_in_progress flag gets set to True. This indicates that 
            # the node deletion is being triggered by a parent folder-level deletion and that manual node clean does not need
            # to take place.
            self._timer = qt.QTimer()
            self._timer.timeout.connect(delayed_node_deletion)
            self._timer.setSingleShot(True)
            self._timer.start(500) 

    def set_solution(self, solution:SlicerOpenLIFUSolution):
        """Set a solution to be the currently active solution. If there is an active session, write that solution to the database."""
        self.getParameterNode().loaded_solution = solution
        if self.validate_session():
            if get_cur_db() is None: # This should not happen -- if there is an active session then there should be a database connection as well.
                raise RuntimeError("Unable to write solution to the session because there is no database connection")
            session_openlifu = self.getParameterNode().loaded_session.session.session
            solution_openlifu = solution.solution.solution
            self.getParameterNode().loaded_session.last_generated_solution_id = solution_openlifu.id
            get_cur_db().write_solution(session_openlifu, solution_openlifu)


    def clear_solution(self,  clean_up_scene:bool = True) -> None:
        """Unload the current solution if there is one loaded.

        Args:
            clean_up_scene: Whether to remove the solution's affiliated scene content.
                If False then the scene content is orphaned from its session.
                If True then the scene content is removed.
        """
        solution = self.getParameterNode().loaded_solution
        self.getParameterNode().loaded_solution = None
        if solution is None:
            return
        if clean_up_scene:
            solution.clear_nodes()

    def set_run(self, run:SlicerOpenLIFURun):
        """Set a run to be the currently active run. If there is an active session, write that run to the database."""
        self.getParameterNode().loaded_run = run

        # If there is an active session, save run to database
        if self.validate_session():
            if get_cur_db() is None: # This should not happen -- if there is an active session then there should be a database connection as well.
                raise RuntimeError("Unable to write run to the session because there is no database connection")
            loaded_session = self.getParameterNode().loaded_session
            session_openlifu = loaded_session.session.session
            protocol_openlifu = loaded_session.get_protocol().protocol

            run_openlifu = run.run
            
            # Session and protocol snapshots are optional arguments
            get_cur_db().write_run(run_openlifu, session_openlifu, protocol_openlifu)
            
    def add_subject_to_database(self, subject_name, subject_id):
        """ Adds new subject to loaded openlifu database.

        Args:
            subject_name: name of subject to be added (str)
            subject_id: id of subject to be added (str)
        """

        import openlifu.db.database
        import openlifu.db.subject

        newOpenLIFUSubject = openlifu.db.subject.Subject(
            name = subject_name,
            id = subject_id,
        )

        subject_ids = get_cur_db().get_subject_ids()

        if newOpenLIFUSubject.id in subject_ids:
            if not slicer.util.confirmYesNoDisplay(
                f"Subject with ID {newOpenLIFUSubject.id} already exists in the database. Overwrite subject?",
                "Subject already exists"
            ):
                return

        get_cur_db().write_subject(newOpenLIFUSubject, on_conflict = openlifu.db.database.OnConflictOpts.OVERWRITE)

    def get_virtual_fit_approvals_in_session(self) -> List[str]:
        """Get the virtual fit approval state in the current session object, a list of target IDs for which virtual fit
        is approved.
        This does not first check whether there is an active session; make sure that one exists before using this.
        """
        session = self.getParameterNode().loaded_session
        if session is None:
            raise RuntimeError("No active session.")
        approved_vf_targets = session.get_virtual_fit_approvals()
        
        return approved_vf_targets
    
    def get_transducer_tracking_approvals_in_session(self) -> List[str]:
        """Get the transducer localization approval state in the current session object, a list of photoscan IDs for which
        transducer localization is approved.
        """
        session = self.getParameterNode().loaded_session
        if session is None:
            raise RuntimeError("No active session.")
        approved_tt_photoscans = session.get_transducer_tracking_approvals()

        return approved_tt_photoscans

    def load_volume_from_openlifu(self, volume_dir: Path, volume_metadata: Dict):
        """ Load a volume based on openlifu metadata and check for duplicate volumes in the scene.
        Args:
            volume_dir: Full path to the database volume directory
            volume_metadata: openlifu volume metadata including the volume name, id and relative path.
        """
        loaded_volumes = slicer.util.getNodesByClass('vtkMRMLScalarVolumeNode')
        loaded_volume_ids = [volume_node.GetAttribute('OpenLIFUData.volume_id') if volume_node.GetAttribute('OpenLIFUData.volume_id') else volume_node.GetID() for volume_node in loaded_volumes]

        if volume_metadata['id'] == self.get_current_session_volume_id():
            slicer.util.errorDisplay(
                f"A volume with ID {volume_metadata['id']} is in use by the current session. Not loading it.",
                "Volume in use by session",
                )
            return

        # Check whether the same volume_id is already loaded
        if volume_metadata['id'] in loaded_volume_ids:
            if not slicer.util.confirmYesNoDisplay(
                f"A volume with ID {volume_metadata['id']} is already loaded. Reload it?",
                "Volume already loaded",
                ):
                return
            else:
                idx = loaded_volume_ids.index(volume_metadata['id'])
                slicer.mrmlScene.RemoveNode(loaded_volumes[idx])

        volume_filepath = Path(volume_dir,volume_metadata['data_filename'])
        loadedVolumeNode, _ = load_volume_and_threshold_background(volume_filepath)
        # Note: OnNodeAdded/updateLoadedObjectsView is called before openLIFU metadata is assigned to the node so need
        # call updateLoadedObjectsView again to display openlifu name/id.
        assign_openlifu_metadata_to_volume_node(loadedVolumeNode, volume_metadata)

    def load_volume_from_file(self, filepath: str) -> None:
        """ Given either a volume or json filetype, load a volume into the scene and determine whether
        the volume should be loaded based on openlifu metadata or default slicer parameters"""

        parent_dir = Path(filepath).parent
        # Load volume using use slicer default volume name and id based on filepath
        if slicer.app.coreIOManager().fileType(filepath) == 'VolumeFile':
            load_volume_and_threshold_background(filepath)

        # If the user selects a json file, infer volume filepath information based on the volume_metadata.
        elif Path(filepath).suffix == '.json':
            # Check for corresponding volume file
            volume_metadata = json.loads(Path(filepath).read_text())
            if 'data_filename' in volume_metadata:
                volume_filepath = Path(parent_dir,volume_metadata['data_filename'])
                if volume_filepath.exists():
                    self.load_volume_from_openlifu(parent_dir, volume_metadata)
                else:
                    slicer.util.errorDisplay(f"Cannot find associated volume file: {volume_filepath}")
            else:
                slicer.util.errorDisplay("Invalid volume filetype specified")
        else:
            slicer.util.errorDisplay("Invalid volume filetype specified")

    def load_photoscan_from_file(self, model_or_json_filepath: str, texture_filepath:Optional[str] = None) -> SlicerOpenLIFUPhotoscan:
        """ Given either a model or json filetype, load a photoscan model into the scene and determine whether
        the photoscan should be loaded based on openlifu metadata or default slicer parameters. If `model_or_json_filepath` is a
        model filepath, then `texture_filepath` must also be provided.
        """

        # Load from data filepaths
        if slicer.app.coreIOManager().fileType(model_or_json_filepath) == 'ModelFile':
            
            newly_loaded_photoscan = SlicerOpenLIFUPhotoscan.initialize_from_data_filepaths(model_or_json_filepath, texture_filepath)
            self.getParameterNode().loaded_photoscans[newly_loaded_photoscan.photoscan.photoscan.id] = newly_loaded_photoscan
            return newly_loaded_photoscan

        # If the user selects a json file,use the photoscan_metadata included in the json file to load the photoscan. 
        elif Path(model_or_json_filepath).suffix == '.json':
            import openlifu.nav.photoscan

            photoscan_openlifu = openlifu.nav.photoscan.Photoscan.from_file(model_or_json_filepath)
            return self.load_photoscan_from_openlifu(photoscan_openlifu, parent_dir = str(Path(model_or_json_filepath).parent))
        else:
            slicer.util.errorDisplay("Invalid photoscan filetype specified")

    def load_photoscan_from_openlifu(self, photoscan_openlifu, load_from_active_session: bool = False, parent_dir: Optional[str] = None, replace_confirmed : bool = False) -> SlicerOpenLIFUPhotoscan:
        """Load an openlifu photoscan object into the scene as a SlicerOpenLIFUPhotoscan,
        adding it to the list of loaded openlifu objects.

        Args:
            photoscan_openlifu: openlifu photoscan object
            load_from_active_session (bool): If True, the photoscan is loaded based on the active session, from the openlifu database.
            parent_dir (str): If load_from_active_session is False,then the absolute path to the parent directory containing the 
                photoscan object and affiliated model and texture files needs to be specified. 
            replace_confirmed: Whether we can bypass the prompt to re-load an already loaded Photoscan.
                This could be used for example if we already know the user is okay with re-loading the photoscan.

        Returns: The newly loaded SlicerOpenLIFUPhotoscan.
        """

        # Check for valid inputs
        if not load_from_active_session:
            if parent_dir is None:
                raise RuntimeError("Cannot load photoscan because the parent_dir was not specified for the photoscan object.")
        else:
            if get_cur_db() is None: # This shouldn't happen
                raise RuntimeError("Cannot load photoscan because there is a session but no database connection to load the data from.")

        if photoscan_openlifu.id in self.getParameterNode().loaded_photoscans:
            if not replace_confirmed:
                if not slicer.util.confirmYesNoDisplay(
                    f"A photoscan with ID {photoscan_openlifu.id} is already loaded. Reload it?",
                    "Photoscan already loaded",
                ):
                    return
            self.remove_photoscan(photoscan_openlifu.id) 

        with BusyCursor():
            if load_from_active_session:
                loaded_session = self.getParameterNode().loaded_session
                _, (model_data, texture_data) = get_cur_db().load_photoscan(loaded_session.get_subject_id(),loaded_session.get_session_id(),photoscan_openlifu.id, load_data = True)
            else:
                import openlifu.nav.photoscan

                model_data, texture_data = openlifu.nav.photoscan.load_data_from_photoscan(photoscan_openlifu,parent_dir = parent_dir)

        newly_loaded_photoscan = SlicerOpenLIFUPhotoscan.initialize_from_openlifu_photoscan(
            photoscan_openlifu,
            model_data,
            texture_data)
        
        self.getParameterNode().loaded_photoscans[photoscan_openlifu.id] = newly_loaded_photoscan
        return newly_loaded_photoscan
    
    def on_photoscan_affiliated_node_about_to_be_removed(self, node_mrml_id:str, affiliated_node_attribute_names: List[str]) -> None:
        """Handle cleanup on SlicerOpenLIFUPhotoscan objects when the mrml nodes they depend on get removed from the scene.

        Args:
            node_mrml_id: The mrml scene ID of the node that was (or is about to be) removed
            affiliated_node_attribute_name: List of the possible names of the affected vtkMRMLNode-valued SlicerOpenLIFUPhotoscanNode attribute
                (so "texture_node" or "model_node")
        """
        matching_photoscan_openlifu_ids = [
            photoscan_openlifu_id
            for photoscan_openlifu_id, photoscan in self.getParameterNode().loaded_photoscans.items()
            for attribute_name in affiliated_node_attribute_names 
            if getattr(photoscan,attribute_name).GetID() == node_mrml_id
        ]

        # If this fails, then a single mrml node was shared across multiple loaded SlicerOpenLIFUPhotoscans, which
        # should not be possible in the application logic.
        assert(len(matching_photoscan_openlifu_ids) <= 1)

        if matching_photoscan_openlifu_ids:
            # Remove the photoscan, but keep any other nodes under it. This transducer was removed
            # by manual mrml scene manipulation, so we don't want to pull other nodes out from
            # under the user.
            photoscan_openlifu_id = matching_photoscan_openlifu_ids[0]
            clean_up_scene = ObjectBeingUnloadedMessageBox(
                message = f"The photoscan with id {photoscan_openlifu_id} will be unloaded because an affiliated node was removed from the scene.",
                title="Photoscan removed",
                checkbox_tooltip = "Ensures cleanup of the model node and texture volume node affiliated with the photoscan",
            ).customexec_()
            self.remove_photoscan(photoscan_openlifu_id, clean_up_scene=clean_up_scene)

    def remove_photocollection(self, photocollection_id:str) -> None:
        """Remove a photocollection from the list of loaded photocollections.

        Args:
            photocollection_id: The openlifu scan_id of the photocollection to remove
        """
        session_photocollections = self.getParameterNode().session_photocollections
        if not photocollection_id in session_photocollections:
            raise IndexError(f"No photocollection with scan_id {photocollection_id} appears to be loaded; cannot remove it.")

        session_photocollections.remove(photocollection_id)

    def remove_photoscan(self, photoscan_id:str, clean_up_scene:bool = True) -> None:
        """Remove a photoscan from the list of loaded photoscans, clearing away its data from the scene.

        Args:
            photoscan_id: The openlifu ID of the photoscan to remove
            clean_up_scene: Whether to remove the SlicerOpenLIFUPhotoscan's affiliated nodes from the scene.
        """
        loaded_photoscans = self.getParameterNode().loaded_photoscans
        if not photoscan_id in loaded_photoscans:
            raise IndexError(f"No photoscan with ID {photoscan_id} appears to be loaded; cannot remove it.")
        # Clean-up order matters here: we should pop the photoscan out of the loaded objects dict and *then* clear out its
        # affiliated nodes. This is because clearing the nodes triggers the check on_photoscan_affiliated_node_removed.
        photoscan = loaded_photoscans.pop(photoscan_id)
        if clean_up_scene:
            photoscan.clear_nodes()

    def add_volume_to_database(self, subject_id: str, volume_id: str, volume_name: str, volume_filepath: str) -> None:
        """ Adds volume to selected subject in the loaded openlifu database.

        Args:
            subject_id: ID of subject associated with the volume (str)
            volume_id: ID of volume to be added (str)
            volume_name: Name of volume to be added (str)
            volume_filepath: filepath of volume to be added (str)
        """

        volume_ids = get_cur_db().get_volume_ids(subject_id)
        if volume_id in volume_ids:
            if not slicer.util.confirmYesNoDisplay(
                f"Volume ID {volume_id} already exists in the database for subject {subject_id}. Overwrite volume?",
                "Volume already exists"
            ):
                return

        import openlifu.db.database

        get_cur_db().write_volume(subject_id, volume_id, volume_name, volume_filepath, on_conflict = openlifu.db.database.OnConflictOpts.OVERWRITE)

    def add_session_to_database(self, subject_id: str, session_parameters: Dict) -> bool:
        """ Add new session to selected subject in the loaded openlifu database

        Args:
            subject_id: id of the subject to which a session is being added
            session_parameters: Dictionary containing the parameters output from the CreateNewSession Dialog

        Returns True if a session is successfully added to the database
        """

        # Check if session already exists in database
        existing_session_ids = self.get_session_info(subject_id)
        for session in existing_session_ids:
            if session_parameters['id'] == session[0]:
                if not slicer.util.confirmYesNoDisplay(
                f"Session ID {session_parameters['id']} already exists in the database for subject {subject_id}. Overwrite session?",
                "Session already exists"
            ):
                    return False

        import openlifu.db.database
        import openlifu.db.session

        newOpenLIFUSession = openlifu.db.session.Session(
            name = session_parameters['name'],
            id = session_parameters['id'],
            subject_id = subject_id,
            protocol_id = session_parameters['protocol_id'],
            volume_id = session_parameters['volume_id'],
            transducer_id = session_parameters['transducer_id']
        )
        get_cur_db().write_session(self.get_subject(subject_id), newOpenLIFUSession, on_conflict = openlifu.db.database.OnConflictOpts.OVERWRITE)
        return True

    def add_photocollection_to_database(self, subject_id: str, session_id: str, photocollection_parameters: Dict) -> bool:
        """ Add new photocollection to selected subject/session in the loaded openlifu database
        Args:
            subject_id: ID of subject associated with the photocollection (str)
            session_id: ID of session associated with the photocollection (str)
            photocollection_parameters: Dictionary containing the required parameters for adding a photocollection to database

        Returns:
            bool: True if the photocollection was successfully added or overwritten in the database.
                  False if the operation was canceled by the user when prompted to overwrite.
    
        Raises:
            ValueError: If 'scan_id' or 'photo_paths' is missing from photocollection_parameters.
        """
        photocollection_ids = get_cur_db().get_photocollection_reference_numbers(subject_id, session_id)
        if photocollection_parameters['scan_id'] in photocollection_ids:
            if not slicer.util.confirmYesNoDisplay(
                f"Photocollection scan_id {photocollection_parameters['scan_id']} already exists in the database for session {session_id}. Overwrite photocollection?",
                "Photocollection already exists"
            ):
                return False

        scan_id = photocollection_parameters.get("scan_id")
        photo_abspaths = photocollection_parameters.get("photo_paths")

        if scan_id is None:
            raise ValueError("Missing required parameter: 'scan_id'")
        if photo_abspaths is None:
            raise ValueError("Missing required parameter: 'photo_paths'")

        import openlifu.db.database

        get_cur_db().write_photocollection(subject_id, session_id, scan_id,
                                      photo_abspaths, on_conflict =
                                      openlifu.db.database.OnConflictOpts.OVERWRITE)
        return True

    def add_photoscan_to_database(self, subject_id: str, session_id: str, photoscan_parameters: Dict) -> "openlifu.nav.photoscan.Photoscan":
        """ Add new photoscan to selected subject/session in the loaded openlifu database
        Args:
            subject_id: ID of subject associated with the photoscan (str)
            session_id: ID of session associated with the photoscan (str)
            photoscan_parameters: Dictionary containing the required parameters for adding a photoscan to database
        """
        photoscan_ids = get_cur_db().get_photoscan_ids(subject_id, session_id)
        if photoscan_parameters['id'] in photoscan_ids:
            if not slicer.util.confirmYesNoDisplay(
                f"Photoscan ID {photoscan_parameters['id']} already exists in the database for session {session_id}. Overwrite photoscan?",
                "Photoscan already exists"
            ):
                return

        model_abspath = photoscan_parameters.pop("model_abspath")
        texture_abspath = photoscan_parameters.pop("texture_abspath")
        mtl_abspath = photoscan_parameters.pop("mtl_abspath")

        import openlifu.db.database
        import openlifu.nav.photoscan

        newOpenLIFUPhotoscan = openlifu.nav.photoscan.Photoscan().from_dict(photoscan_parameters)
        get_cur_db().write_photoscan(subject_id, session_id, newOpenLIFUPhotoscan,
                                model_abspath,
                                texture_abspath,
                                mtl_abspath,
                                on_conflict = openlifu.db.database.OnConflictOpts.OVERWRITE)
    
        return newOpenLIFUPhotoscan

    def toggle_solution_approval(self):
        """Approve the currently active solution if it was not approved. Revoke approval if it was approved.
        This will write the approval to the solution in memory and, if there is an active session from which this solution was generated,
        we will also write the solution approval to the database.

        Raises runtime error if there is no active solution, or if there appears to be an active session to which the solution is
        affiliated but no connected database to enable writing.
        """
        solution = self.getParameterNode().loaded_solution
        session = self.getParameterNode().loaded_session
        if solution is None: # We should never be calling toggle_solution_approval if there's no active solution
            raise RuntimeError("Cannot toggle solution approval because there is no active solution.")
        solution.toggle_approval() # apply or revoke approval
        if session is not None:
            if session.last_generated_solution_id == solution.solution.solution.id:
                if get_cur_db() is None: # This shouldn't happen
                    raise RuntimeError("Cannot toggle solution approval because there is a session but no database connection to write the approval.")
                import openlifu.db.database

                OnConflictOpts : "openlifu.db.database.OnConflictOpts" = openlifu.db.database.OnConflictOpts
                get_cur_db().write_solution(session.session.session, solution.solution.solution, on_conflict=OnConflictOpts.OVERWRITE)
            else:
                # This can happen if, for example, a solution is generated from a session and then a new session is loaded and the user
                # tries to toggle approval on the old solution. The user would have to have kept the old solution around by
                # invalidating the previous session while keeping its affiliated data around in an orphaned state.
                # Weird case, but it can happen if someone is awkwardly switching between the manual and treatment workflows.
                slicer.util.infoDisplay(
                    text= (
                        "There is an active session but it is not the one that generated this solution."
                        " Since this solution has lost its session link, any approval state change will not be saved into the database."
                    ),
                    windowTitle="Not saving approval state"
                )
        self.getParameterNode().loaded_solution = solution # remember to write the updated solution object into the parameter node


#
# OpenLIFUDataTest
#


class OpenLIFUDataTest(ScriptedLoadableModuleTest):
    """
    This is the test case for your scripted module.
    Uses ScriptedLoadableModuleTest base class, available at:
    https://github.com/Slicer/Slicer/blob/main/Base/Python/slicer/ScriptedLoadableModule.py
    """

    def load_subject_session(self):

        slicer.util.selectModule("OpenLIFUData")
        dw = slicer.modules.OpenLIFUDataWidget

        cur_db = get_cur_db()
        # Load subject
        load_subject_dlg = LoadSubjectDialog(cur_db)

        def simulate_user():
            load_subject_dlg.tableWidget.selectRow(0) # Manually choose first subject
            load_subject_dlg.onLoadSubjectClicked() 

        qt.QTimer.singleShot(0, simulate_user) # Needed since the dialog is modal

        result  = dw.on_load_subject_clicked(True, load_subject_dlg)

        # Verify that the subject was loaded
        assert result is True
        assert dw.logic.subject.id == "example_subject"

        # Simulate session selection
        load_session_dlg = LoadSessionDialog(cur_db, dw.logic.subject.id)
        def simulate_user_session():
            load_session_dlg.table_widget.selectRow(0) # Manually choose first session
            load_session_dlg.on_load_session_clicked()

        qt.QTimer.singleShot(0, simulate_user_session) # Needed since the dialog is modal   
        
        result = dw.on_load_session_clicked(True, load_session_dlg)
        # Verify that the session was loaded

        assert result is True
        assert dw.logic.getParameterNode().loaded_session.get_session_id() == "test_session"
