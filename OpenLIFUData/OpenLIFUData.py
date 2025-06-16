# Standard library imports
import json
import os
import random
import shutil
import string
import subprocess
import tempfile
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
    get_cur_db,
    get_target_candidates,
    openlifu_lz,
)
from OpenLIFULib.events import SlicerOpenLIFUEvents
from OpenLIFULib.guided_mode_util import GuidedWorkflowMixin
from OpenLIFULib.transducer_tracking_results import (
    add_transducer_tracking_results_from_openlifu_session_format,
    clear_transducer_tracking_results,
    get_photoscan_id_from_transducer_tracking_result,
    is_transducer_tracking_result_node,
)
from OpenLIFULib.user_account_mode_util import get_current_user, get_user_account_mode_state, UserAccountBanner
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

# These imports are deferred at runtime using openlifu_lz, 
# but are done here for IDE and static analysis purposes
if TYPE_CHECKING:
    import openlifu
    import openlifu.nav.photoscan
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

    def __init__(self, db: "openlifu.db.Database", subject_id: str, parent="mainWindow"):
        super().__init__(slicer.util.mainWindow() if parent == "mainWindow" else parent)
        subject = db.load_subject(subject_id)

        self.setWindowTitle(f"View Sessions for {subject.name} ({subject_id})")
        self.setWindowModality(qt.Qt.WindowModal)

        self.db = db
        self.subject_id = subject_id
        self.subject = subject
        self.selected_session = None

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

        for button in [self.new_session_button, self.load_session_button]:
            button.setSizePolicy(qt.QSizePolicy.Expanding, qt.QSizePolicy.Preferred)
            subject_buttons_layout.addWidget(button)

        self.box_layout.addLayout(subject_buttons_layout)

        self.new_session_button.clicked.connect(self.on_new_session_clicked)
        self.load_session_button.clicked.connect(self.on_load_session_clicked)
        self.table_widget.doubleClicked.connect(self.on_load_session_clicked)
        self.table_widget.clicked.connect(lambda: self.load_session_button.setFocus())

        # ---- Cancel Button ----
        self.button_box = qt.QDialogButtonBox()
        self.cancel_button = self.button_box.addButton("Cancel", qt.QDialogButtonBox.RejectRole)
        self.cancel_button.setToolTip("Close this window without loading any new session")
        self.cancel_button.clicked.connect(lambda *args: self.reject())
        self.box_layout.addWidget(self.button_box)

        # ---- Populate Table ----
        self.update_sessions_list()

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
            self.new_session_button
        ]
        _tooltips = [
            "Create a new session for this subject"
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

class StartPhotocollectionCaptureDialog(qt.QDialog):
    """ Add new photocollection dialog """

    MINIMUM_NUMBER_OF_PHOTOS_FOR_PHOTOSCAN=1

    def __init__(self, parent="mainWindow"):
        super().__init__(slicer.util.mainWindow() if parent == "mainWindow" else parent)
        self.setWindowTitle("Add New Photocollection")
        self.setWindowModality(qt.Qt.WindowModal)

        self.reference_number = "".join(random.choices(string.digits, k=8))
        self.pulled_files = []

        temp_dir = tempfile.gettempdir()
        self.temp_photocollection_path = os.path.join(temp_dir, self.reference_number)
        os.makedirs(self.temp_photocollection_path, exist_ok=True)

        self.setup()

    def setup(self):

        self.setContentsMargins(15, 15, 15, 15)

        vBoxLayout = qt.QVBoxLayout()
        vBoxLayout.setSpacing(10)
        self.setLayout(vBoxLayout)

        self.directionLabel1 = qt.QLabel(
            f"Please create a 3D Open Water photocollection with the following reference number: {self.reference_number}. "
            "Click \"OK\" when the Android device has finished and is plugged into the computer."
        )
        self.directionLabel1.setWordWrap(True)
        vBoxLayout.addWidget(self.directionLabel1)

        vBoxLayout.addStretch(1)

        self.buttonBox = qt.QDialogButtonBox()
        self.buttonBox.setStandardButtons(
            qt.QDialogButtonBox.Ok | qt.QDialogButtonBox.Cancel
        )
        vBoxLayout.addWidget(self.buttonBox)

        self.buttonBox.rejected.connect(self.reject)
        self.buttonBox.accepted.connect(self.validateInputs)

    def validateInputs(self):
        """
        We need to make sure that the android file system has the files
        associated with the reference id in the right location.
        """
        # The path /sdcard/DCIM/Camera/ is the standard internal storage path
        # from the Android device’s perspective when accessed via adb, not the
        # computer’s mounted file system like with Android File Transfer.
        android_dir = Path("/sdcard/DCIM/Camera")
        result = subprocess.run(
            ["adb", "shell", "ls", android_dir/f"{self.reference_number}_*.jpeg"],
            capture_output=True, text=True
        ) 

        if result.returncode != 0:
            slicer.util.errorDisplay(
              "Error finding files on Android device."
              " Please make sure the device is connected,"
              " you have installed android platform tools on this machine,"
              " you have enabled developer mode on the device,"
              " and you have enabled USB debugging on the device.",
              parent = self
            )
            return
        
        files = [f for f in result.stdout.strip().split('\n') if f]

        if not files or len(files) < self.MINIMUM_NUMBER_OF_PHOTOS_FOR_PHOTOSCAN:
            slicer.util.errorDisplay(f"Not enough photos were found in the photocollection.", parent = self)
            return

        for file in files:
            filename = os.path.basename(file)
            dest_path = os.path.join(self.temp_photocollection_path, filename)
            subprocess.run(
                ["adb", "pull", f"{android_dir}/{filename}", dest_path]
            )
            self.pulled_files.append(dest_path)

        self.accept()

    def customexec_(self):

        returncode = self.exec_()
        photocollection_dict = {
            "reference_number" : self.reference_number,
            "photo_paths" : self.pulled_files,
        }
        return (returncode, photocollection_dict)

class AddNewPhotoscanDialog(qt.QDialog):
    """ Add new photoscan dialog """

    def __init__(self, parent="mainWindow"):
        super().__init__(slicer.util.mainWindow() if parent == "mainWindow" else parent)
        self.setWindowTitle("Add New Photoscan")
        self.setWindowModality(qt.Qt.WindowModal)
        self.setup()

    def setup(self):

        formLayout = qt.QFormLayout()
        self.setLayout(formLayout)

        # Model filepath
        self.photoscanModelFilePath = ctk.ctkPathLineEdit()
        self.photoscanModelFilePath.filters = ctk.ctkPathLineEdit.Files
        # Allowable photoscan filetypes
        self.photoscan_model_extensions = ("Photoscan Model" + " (*.obj *.vtk *.stl *.ply *.vtp *.g);;" +
        "All Files" + " (*)")
        self.photoscanModelFilePath.nameFilters = [self.photoscan_model_extensions]
        self.photoscanModelFilePath.currentPathChanged.connect(self.updatePhotoscanDetails)
        formLayout.addRow(_("Model Filepath:"), self.photoscanModelFilePath)

        # Texture filepath
        self.photoscanTextureFilePath = ctk.ctkPathLineEdit()
        self.photoscanTextureFilePath.filters = ctk.ctkPathLineEdit.Files
        # Allowable photoscan filetypes
        self.photoscan_texture_extensions = ("Photoscan Texture" + " (*.jpg *. *.png *.tiff *.exr);;" +
        "All Files" + " (*)")
        self.photoscanTextureFilePath.nameFilters = [self.photoscan_texture_extensions]
        formLayout.addRow(_("Texture Filepath:"), self.photoscanTextureFilePath)

        # MTL filepath
        self.photoscanMTLFilePath = ctk.ctkPathLineEdit()
        self.photoscanMTLFilePath.filters = ctk.ctkPathLineEdit.Files
        # Allowable photoscan filetypes
        self.photoscan_mtl_extensions = ("Photoscan Material" + " (*.mtl);;" +
        "All Files" + " (*)")
        self.photoscanMTLFilePath.nameFilters = [self.photoscan_mtl_extensions]
        formLayout.addRow(_("Materials Filepath (Optional):"), self.photoscanMTLFilePath)

        self.photoscanName = qt.QLineEdit()
        formLayout.addRow(_("Photoscan Name:"), self.photoscanName)

        self.photoscanID = qt.QLineEdit()
        formLayout.addRow(_("Photoscan ID:"), self.photoscanID)

        self.buttonBox = qt.QDialogButtonBox()
        self.buttonBox.setStandardButtons(qt.QDialogButtonBox.Ok |
                                          qt.QDialogButtonBox.Cancel)
        formLayout.addWidget(self.buttonBox)

        self.buttonBox.rejected.connect(self.reject)
        self.buttonBox.accepted.connect(self.validateInputs)

    def updatePhotoscanDetails(self):
        current_filepath = Path(self.photoscanModelFilePath.currentPath)
        if current_filepath.is_file():
            while current_filepath.suffix:
                current_filepath = current_filepath.with_suffix('')
            photoscan_name = current_filepath.stem
            if not len(self.photoscanName.text):
                self.photoscanName.setText(photoscan_name)
            if not len(self.photoscanID.text):
                self.photoscanID.setText(photoscan_name)

    def validateInputs(self):
        """
        The MTL filepath is an optional input for writing a photoscan to the database.
        """
        photoscan_name = self.photoscanName.text
        photoscan_id = self.photoscanID.text
        photoscan_model_filepath = self.photoscanModelFilePath.currentPath
        photoscan_texture_filepath = self.photoscanTextureFilePath.currentPath  

        if not len(photoscan_name) or not len(photoscan_id) or not len(photoscan_model_filepath) or not len(photoscan_texture_filepath):
            slicer.util.errorDisplay("Required fields are missing", parent = self)
        elif not slicer.app.coreIOManager().fileType(photoscan_model_filepath) == 'ModelFile':
            slicer.util.errorDisplay("Invalid photoscan filetype specified", parent = self)
        else:
            self.accept()

    def customexec_(self):

        returncode = self.exec_()
        if not len(self.photoscanMTLFilePath.currentPath):
            mtl_filepath = None
        else:
            mtl_filepath = self.photoscanMTLFilePath.currentPath
        photoscan_dict = {
            "model_abspath" : self.photoscanModelFilePath.currentPath,
            "texture_abspath" : self.photoscanTextureFilePath.currentPath,
            "mtl_abspath" : mtl_filepath,
            "name": self.photoscanName.text,
            "id": self.photoscanID.text
        }
        return (returncode, photoscan_dict)

class ImportPhotocollectionFromDiskDialog(qt.QDialog):
    """Import photocollection from disk dialog."""

    MINIMUM_NUMBER_OF_PHOTOS_FOR_PHOTOSCAN = 1

    def __init__(self, parent="mainWindow"):
        super().__init__(slicer.util.mainWindow() if parent == "mainWindow" else parent)
        self.reference_number = ""
        self.setWindowTitle("Import Photocollection")
        self.setWindowModality(qt.Qt.WindowModal)
        self.photo_files = []
        self.setup()

    def setup(self):
        self.setContentsMargins(15, 15, 15, 15)

        self.formLayout = qt.QFormLayout()
        self.setLayout(self.formLayout)

        # Reference number input
        self.referenceNumberLineEdit = qt.QLineEdit()
        self.referenceNumberLineEdit.setPlaceholderText("Enter reference number (alphanumeric)")
        self.formLayout.addRow(_("Reference Number:"), self.referenceNumberLineEdit)

        # Directory path selector
        self.photocollectionDirectoryPath = ctk.ctkPathLineEdit()
        self.photocollectionDirectoryPath.filters = ctk.ctkPathLineEdit.Dirs
        self.formLayout.addRow(_("Photocollection Directory:"), self.photocollectionDirectoryPath)

        self.buttonBox = qt.QDialogButtonBox()
        self.buttonBox.setStandardButtons(qt.QDialogButtonBox.Ok | qt.QDialogButtonBox.Cancel)
        self.formLayout.addWidget(self.buttonBox)

        self.buttonBox.rejected.connect(self.reject)
        self.buttonBox.accepted.connect(self.validateInputs)

    def validateInputs(self):
        """Validate the reference number and selected directory."""
        ref_number = self.referenceNumberLineEdit.text.strip()
        if not ref_number.isalnum():
            slicer.util.errorDisplay("Reference number must be alphanumeric.", parent=self)
            return

        directory = self.photocollectionDirectoryPath.currentPath
        if not os.path.isdir(directory):
            slicer.util.errorDisplay("Selected path is not a valid directory.", parent=self)
            return

        photo_files = [
            os.path.join(directory, f)
            for f in os.listdir(directory)
            if f.lower().endswith((".jpg", ".jpeg", ".png"))
        ]

        if len(photo_files) < self.MINIMUM_NUMBER_OF_PHOTOS_FOR_PHOTOSCAN:
            slicer.util.errorDisplay(f"Not enough photos were found in the directory (found {len(photo_files)}).", parent=self)
            return

        self.reference_number = ref_number
        self.photo_files = photo_files
        self.accept()

    def customexec_(self):
        returncode = self.exec_()
        photocollection_dict = {
            "reference_number": self.reference_number,
            "photo_paths": self.photo_files,
        }
        return returncode, photocollection_dict

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
            self.photoscan_texture_extensions = ("Photoscan Texture" + " (*.jpg *. *.png *.tiff *.exr);;" +
            "All Files" + " (*)")
            self.photoscanTextureFilePath.nameFilters = [self.photoscan_texture_extensions]
            self.formLayout.insertRow(1,_("Texture Filepath:"), self.photoscanTextureFilePath)
        elif current_filepath.suffix == '.json' and self.formLayout.rowCount() == 3:
            self.formLayout.removeRow(1) 

    def validateInputs(self):
        photoscan_model_filepath = Path(self.photoscanModelFilePath.currentPath)
        if photoscan_model_filepath.suffix != '.json':
            photoscan_texture_filepath = self.photoscanTextureFilePath.currentPath  
            if not len(photoscan_texture_filepath):
                slicer.util.errorDisplay("Model and texture files both need to be specified", parent = self)
                return
            elif not slicer.app.coreIOManager().fileType(photoscan_model_filepath) == 'ModelFile':
                slicer.util.errorDisplay("Invalid photoscan filetype specified", parent = self)
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
        self.ui.loadFiducialsButton.clicked.connect(self.onLoadFiducialsPressed)
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

        # Go to protocol config
        self.ui.configureProtocolsPushButton.clicked.connect(lambda : slicer.util.selectModule("OpenLIFUProtocolConfig"))

        # Subject collapsible section
        self.ui.chooseSubjectButton.clicked.connect(self.on_load_subject_clicked)

        # Volumes collapsible section
        self.ui.addVolumeButton.clicked.connect(self.on_add_volume_clicked)

        # Session collapsible section
        self.ui.chooseSessionButton.clicked.connect(self.on_load_session_clicked)
        self.ui.saveSessionButton.clicked.connect(self.on_save_session_clicked)
        self.ui.exitSessionButton.clicked.connect(self.on_exit_session_clicked)

        # ---- Issue updates that may not have been triggered yet ---
        
        # Make sure parameter node is initialized (needed for module reload)
        self.initializeParameterNode()
        
        self.updateLoadedObjectsView()
        self.updateSessionStatus()
        self.update_loadSubjectButton_enabled()
        self.update_volumesCollapsibleButton_checked_and_enabled()
        self.update_sessionCollapsibleButton_checked_and_enabled()
        self.update_session_level_buttons_enabled()
        self.updateWorkflowControls()

    def onDatabaseChanged(self, db: Optional["openlifu.db.Database"] = None):
        self.logic.subject = None
        self.logic.clear_session()
        self.update_loadSubjectButton_enabled()

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

    def update_session_level_buttons_enabled(self) -> None:
        if self._parameterNode is None or self._parameterNode.loaded_session is None:
            self.ui.saveSessionButton.setEnabled(False)
            self.ui.saveSessionButton.setToolTip("There is no active session")
            self.ui.exitSessionButton.setEnabled(False)
            self.ui.exitSessionButton.setToolTip("There is no active session")
        else:
            self.ui.saveSessionButton.setEnabled(True)
            self.ui.saveSessionButton.setToolTip("Save the current session to the database, including session-specific transducer and target configurations")
            self.ui.exitSessionButton.setEnabled(True)
            self.ui.exitSessionButton.setToolTip("Exit this session and clear all session data.")

    @display_errors
    def on_load_subject_clicked(self, checked: bool) -> bool:
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
    def on_load_session_clicked(self, checked:bool) -> bool:
        load_session_dlg = LoadSessionDialog(get_cur_db(), self.logic.subject.id)
        new_session = load_session_dlg.exec_and_get_session()

        if not new_session:
            return False

        self.logic.clear_session(clean_up_scene=True)
        self.logic.load_session(self.logic.subject.id, new_session.id)
        return True

    @display_errors
    def on_import_photocollection_clicked(self, checked:bool):
        loaded_session = self._parameterNode.loaded_session
        if loaded_session is None:
            raise RuntimeError("Cannot import photocollection because a session is not loaded.")

        importDlg = ImportPhotocollectionFromDiskDialog()
        returncode, photocollection_dict = importDlg.customexec_()
        if not returncode:
            return False

        self.logic.add_photocollection_to_database(loaded_session.get_subject_id(), loaded_session.get_session_id(), photocollection_dict.copy())  # logic mutates the dict
        self._parameterNode.session_photocollections.append(photocollection_dict["reference_number"]) # automatically load as well
        self.logic.update_photocollections_affiliated_with_loaded_session()

    @display_errors
    def on_capture_photocollection_clicked(self, checked:bool) -> bool:
        loaded_session = self._parameterNode.loaded_session
        if loaded_session is None:
            raise RuntimeError("Cannot start photocollection capture because a session is not loaded.")

        photocollectiondlg = StartPhotocollectionCaptureDialog()
        returncode, photocollection_dict = photocollectiondlg.customexec_()
        if not returncode:
            return False

        self.logic.add_photocollection_to_database(loaded_session.get_subject_id(), loaded_session.get_session_id(), photocollection_dict.copy())  # logic mutates the dict
        self._parameterNode.session_photocollections.append(photocollection_dict["reference_number"]) # automatically load as well
        self.logic.update_photocollections_affiliated_with_loaded_session()
        return True

    @display_errors
    def on_add_photoscan_clicked(self, checked:bool) -> bool:
        loaded_session = self._parameterNode.loaded_session
        if loaded_session is None:
            raise RuntimeError("Cannot add photoscan because a session is not loaded.")

        photoscandlg = AddNewPhotoscanDialog()
        returncode, photoscan_dict = photoscandlg.customexec_()
        if not returncode:
            return False

        self.logic.add_photoscan_to_database(loaded_session.get_subject_id(), loaded_session.get_session_id(), photoscan_dict.copy())  # logic mutates the dict
        self.logic.update_photoscans_affiliated_with_loaded_session()

        # Update the transducer tracking drop down to reflect new photoscans 
        transducer_tracking_widget = slicer.modules.OpenLIFUTransducerTrackerWidget
        transducer_tracking_widget.algorithm_input_widget.update()
        
        return True

    @display_errors
    def on_save_session_clicked(self, checked:bool) -> None:
        self.logic.save_session()

    @display_errors
    def on_exit_session_clicked(self, checked:bool) -> None:
        if slicer.util.confirmYesNoDisplay("Do you want to save your progress before exiting?", windowTitle="Save Confirmation"):
            self.logic.save_session()
        self.logic.clear_session(clean_up_scene=True)

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


    def onLoadFiducialsPressed(self) -> None:
        """ Call slicer dialog to load fiducials into the scene"""

        # Should use "slicer.util.openAddFiducialsDialog()"" to load the Fiducials dialog. This doesn't work because
        # the ioManager functions are bugged - they have not been updated to use the new file type name for Markups.
        # Instead, using a workaround that directly calls the ioManager with the correct file type name for Markups.
        ioManager = slicer.app.ioManager()
        return ioManager.openDialog("MarkupsFile", slicer.qSlicerFileDialog.Read)

    @display_errors
    def onLoadPhotoscanPressed(self, checked:bool) -> None:
        load_photoscan_dlg = LoadPhotoscanDialog()
        returncode, model_or_json_filepath, texture_filepath = load_photoscan_dlg.customexec_()
        if not returncode:
            return False

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
            transducer_openlifu : "openlifu.Transducer" = transducer_slicer.transducer.transducer
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
            protocol_openlifu : "openlifu.Protocol" = loaded_session.get_protocol().protocol
            
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
                transducer_openlifu : "openlifu.Transducer" = loaded_session.get_transducer().transducer.transducer
                self.ui.sessionStatusTransducerValueLabel.setText(
                    f"{transducer_openlifu.name} (ID: {session_openlifu.transducer_id})"
                )

            # Build the additional info message here; this is status text that conditionally displays.
            additional_info_messages : List[str] = []
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
                    "Transducer tracking approved for "
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
        self.update_session_level_buttons_enabled()
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
            for photocollection_reference_number in loaded_session.get_affiliated_photocollection_reference_numbers():
                if photocollection_reference_number in self.getParameterNode().session_photocollections:
                    self.remove_photocollection(photocollection_reference_number)
            
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

        OnConflictOpts : "openlifu.db.database.OnConflictOpts" = openlifu_lz().db.database.OnConflictOpts
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
        affiliated_photocollections = [num for num in get_cur_db().get_photocollection_reference_numbers(subject_id, session_id)]
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
        slicer.util.getModule("OpenLIFUTransducerTracker").widgetRepresentation()
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

        session_affiliated_photocollections = get_cur_db().get_photocollection_reference_numbers(subject_id, session_id)
        conflicting_session_photocollections =  [num for num in session_affiliated_photocollections if num in self.getParameterNode().session_photocollections]
        if (
            conflicting_session_photocollections
            # TODO: We should add a way for sessions to associate with
            # photocollections. See @ebrahimebrahim's comment on
            # https://github.com/OpenwaterHealth/OpenLIFU-python/pull/235#pullrequestreview-2662421403
            # and (
            #     loaded_session is None
            #       or not any(session_openlifu.photocollection_id != loaded_session.get_photocollection_id())
            #     # (we are okay reloading the photocollection if it's just the one affiliated with the session, since user already decided to replace the session)
            #     )
        ):
            if not slicer.util.confirmYesNoDisplay(
                f"Loading this session will replace the already loaded photocollection(s) with reference_number(s) {conflicting_session_photocollections}. Proceed?",
                "Confirm replace photocollection"
            ):
                return
            
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
            
            # For the best virtual fit result nodes, check if the current transducer
            # transform matches the virtual fit result in terms of matrix values.
            if int(vf_node.GetAttribute("VF:rank")) == 1 and newly_loaded_transducer.is_matching_transform(vf_node):
                newly_loaded_transducer.set_matching_transform(vf_node)
                newly_loaded_transducer.set_visibility(True)

        # === Load transducer tracking results ===

        newly_added_tt_result_nodes = add_transducer_tracking_results_from_openlifu_session_format(
            tt_results_openlifu = session_openlifu.transducer_tracking_results,
            session_id = session_openlifu.id,
            transducer = newly_loaded_transducer.transducer.transducer,
            replace=True, # If there happen to already be some transducer tracking result nodes that clash, loading a session will silently overwrite them.
        )

        for (transducer_to_volume_node, photoscan_to_volume_node) in newly_added_tt_result_nodes:
            transducer_tracking_widget = slicer.modules.OpenLIFUTransducerTrackerWidget
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

        # === Load photocollections as all reference_numbers ===
        # There is no openlifu object for photocollections, so we just add them to the list!

        session_affiliated_photocollections = get_cur_db().get_photocollection_reference_numbers(subject_id, session_id)
        self.getParameterNode().session_photocollections.extend(session_affiliated_photocollections)

        # === Also keep track of affiliated photocollections and unload any conflicting photocollections that have been previously loaded ===
        for photocollection_reference_number in conflicting_session_photocollections:
            self.remove_photocollection(photocollection_reference_number) 
        self.update_photocollections_affiliated_with_loaded_session()

        # If there are any *approved* transducer tracking results that we have just loaded in newly_added_tt_result_nodes,
        # then we check to see if any of them match the current transducer transform in terms of matrix values.
        # If there is a match in matrix values, then the first such matching TT result that we encounter in the loop is 
        # "officially" linked to the current transform by setting the "matching_transform" attribute, thereby ensuring that
        # TT approval is revoked if the transducer is moved.
        # Additionally, any other transducer tracking results whose matrix does not match current transducer get their approval revoked.
        transducer_tracking_widget = slicer.modules.OpenLIFUTransducerTrackerWidget
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
                            reason="The transducer transform does not match the approved tracking result."
                        )

        self.session_loading_unloading_in_progress = False  

    # TODO: This should be a widget level function
    def _on_transducer_transform_modified(self, transducer: SlicerOpenLIFUTransducer) -> None:

        slicer.util.getModuleWidget('OpenLIFUSonicationPlanner').deleteSolutionAndSolutionAnalysisIfAny(reason="The transducer was moved.")
        slicer.util.getModuleWidget('OpenLIFUTransducerTracker').updateVirtualFitResultForDisplay()

        matching_transform_id = transducer.transform_node.GetAttribute("matching_transform")
        if matching_transform_id:
            # If its a transducer tracking node, revoke approval if approved
            transform_node = slicer.mrmlScene.GetNodeByID(matching_transform_id)
            if transform_node and is_transducer_tracking_result_node(transform_node):
                photoscan_id = get_photoscan_id_from_transducer_tracking_result(transform_node)
                transducer_tracking_widget = slicer.modules.OpenLIFUTransducerTrackerWidget
                transducer_tracking_widget.revokeTransducerTrackingApprovalIfAny(
                    photoscan_id = photoscan_id,
                    reason = "The transducer transform was modified"
                )
            
            transducer.set_matching_transform(None)


    def load_protocol_from_file(self, filepath:str) -> None:
        protocol = openlifu_lz().Protocol.from_file(filepath)
        self.load_protocol_from_openlifu(protocol)

    def load_protocol_from_openlifu(self, protocol:"openlifu.Protocol", replace_confirmed: bool = False) -> None:
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
        transducer = openlifu_lz().Transducer.from_file(filepath)
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
            transducer: "openlifu.Transducer",
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
                running the transducer tracking algorithm. If left as empty, the registration surface and transducer body models affiliated 
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

        newOpenLIFUSubject = openlifu_lz().db.subject.Subject(
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

        get_cur_db().write_subject(newOpenLIFUSubject, on_conflict = openlifu_lz().db.database.OnConflictOpts.OVERWRITE)

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
        """Get the transducer tracking approval state in the current session object, a list of photoscan IDs for which
        transducer tracking is approved.
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
            photoscan_openlifu = openlifu_lz().nav.photoscan.Photoscan.from_file(model_or_json_filepath)
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
                model_data, texture_data = openlifu_lz().nav.photoscan.load_data_from_photoscan(photoscan_openlifu,parent_dir = parent_dir)

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

    def remove_photocollection(self, photocollection_reference_number:str) -> None:
        """Remove a photocollection from the list of loaded photocollections.

        Args:
            photocollection_reference_number: The openlifu reference_number of the photocollection to remove
        """
        session_photocollections = self.getParameterNode().session_photocollections
        if not photocollection_reference_number in session_photocollections:
            raise IndexError(f"No photocollection with reference_number {photocollection_reference_number} appears to be loaded; cannot remove it.")

        session_photocollections.remove(photocollection_reference_number)

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

        get_cur_db().write_volume(subject_id, volume_id, volume_name, volume_filepath, on_conflict = openlifu_lz().db.database.OnConflictOpts.OVERWRITE)

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

        newOpenLIFUSession = openlifu_lz().db.session.Session(
            name = session_parameters['name'],
            id = session_parameters['id'],
            subject_id = subject_id,
            protocol_id = session_parameters['protocol_id'],
            volume_id = session_parameters['volume_id'],
            transducer_id = session_parameters['transducer_id']
        )
        get_cur_db().write_session(self.get_subject(subject_id), newOpenLIFUSession, on_conflict = openlifu_lz().db.database.OnConflictOpts.OVERWRITE)
        return True

    def add_photocollection_to_database(self, subject_id: str, session_id: str, photocollection_parameters: Dict) -> None:
        """ Add new photocollection to selected subject/session in the loaded openlifu database
        Args:
            subject_id: ID of subject associated with the photocollection (str)
            session_id: ID of session associated with the photocollection (str)
            photocollection_parameters: Dictionary containing the required parameters for adding a photocollection to database
        """
        photocollection_reference_numbers = get_cur_db().get_photocollection_reference_numbers(subject_id, session_id)
        if photocollection_parameters['reference_number'] in photocollection_reference_numbers:
            if not slicer.util.confirmYesNoDisplay(
                f"Photocollection reference_number {photocollection_parameters['reference_number']} already exists in the database for session {session_id}. Overwrite photocollection?",
                "Photocollection already exists"
            ):
                return

        reference_number = photocollection_parameters.pop("reference_number")
        photo_abspaths = photocollection_parameters.pop("photo_paths")

        get_cur_db().write_photocollection(subject_id, session_id, reference_number,
                                      photo_abspaths, on_conflict =
                                      openlifu_lz().db.database.OnConflictOpts.OVERWRITE)

    def add_photoscan_to_database(self, subject_id: str, session_id: str, photoscan_parameters: Dict) -> None:
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

        newOpenLIFUPhotoscan = openlifu_lz().nav.photoscan.Photoscan().from_dict(photoscan_parameters)
        get_cur_db().write_photoscan(subject_id, session_id, newOpenLIFUPhotoscan,
                                model_abspath,
                                texture_abspath,
                                mtl_abspath,
                                on_conflict = openlifu_lz().db.database.OnConflictOpts.OVERWRITE)

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
                OnConflictOpts : "openlifu.db.database.OnConflictOpts" = openlifu_lz().db.database.OnConflictOpts
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
