# Standard library imports
import json
import logging
import os
import subprocess
import sys
import tempfile
import types
import urllib.request
import zipfile
from collections import defaultdict
from pathlib import Path
from typing import Any, Callable, Optional, List, Tuple, Dict, Sequence, TYPE_CHECKING

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
    SlicerOpenLIFUSolutionAnalysis,
    SlicerOpenLIFUTransducer,
    assign_openlifu_metadata_to_volume_node,
    check_and_install_kwave_binaries,
    check_and_install_python_requirements,
    ensure_python_requirements_for_module_enter,
    get_cur_db,
    get_required_openlifu_version,
    get_target_candidates,
    kwave_binaries_exist,
    openlifu_version_matches,
    python_requirements_exist,
)
from OpenLIFULib.class_definition_widgets import (
    DictTableWidget,
    instantiate_without_post_init,
    ListTableWidget,
    OpenLIFUAbstractDataclassDefinitionFormWidget,
    OpenLIFUAbstractMultipleABCDefinitionFormWidget,
)
from OpenLIFULib.events import SlicerOpenLIFUEvents
from OpenLIFULib.guided_mode_util import GuidedWorkflowMixin, get_guided_mode_state, set_guided_mode_state
from OpenLIFULib.module_layout import apply_module_layout, wire_passive_module_header
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
    cleanup_module_callbacks,
    create_noneditable_QStandardItem,
    display_errors,
    ensure_list,
    get_openlifu_data_parameter_node,
    register_module_callback,
    replace_widget,
)
from OpenLIFULib.volume_thresholding import load_volume_and_threshold_background
from OpenLIFULib.virtual_fit_results import (
    add_virtual_fit_results_from_openlifu_session_format,
    clear_virtual_fit_results,
)

if TYPE_CHECKING:
    import openlifu
    import openlifu.bf
    import openlifu.bf.apod_methods
    import openlifu.bf.delay_methods
    import openlifu.db.database
    import openlifu.nav.photoscan
    import openlifu.plan
    import openlifu.plan.solution_analysis
    import openlifu.seg.seg_methods
    import openlifu.seg.virtual_fit
    import openlifu.sim
    import openlifu.xdc
    import openlifu.xdc.util
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
        # Embedded as a page of the OpenLIFU host module; hide from the modules menu.
        self.parent.hidden = True

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

    def __init__(self, transducer_ids: List[str], protocol_ids: List[str], volume_ids: List[str],
                 subject_id: Optional[str] = None, parent="mainWindow"):
        """ Args:
                transducer_ids: IDs of the transducers available in the loaded database
                protocol_ids: IDs of the protocols available in the loaded database
                volume_ids: IDs of the volumes available for the selected subject in the loaded database
                subject_id: When provided, an "Add Volume" (+) button appears next to the volume
                    combo so the user can import a volume into this subject without leaving the dialog.
        """
        super().__init__(slicer.util.mainWindow() if parent == "mainWindow" else parent)

        self.setWindowTitle("Create New Session")
        self.setWindowModality(qt.Qt.WindowModal)
        self.transducer_ids = transducer_ids
        self.protocol_ids = protocol_ids
        self.volume_ids = volume_ids
        self._subject_id = subject_id
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

        # Volume row: combo + optional "+" import button.
        self.volume = qt.QComboBox()
        self.add_items_to_combobox(self.volume, self.volume_ids, "volume")
        volumeRowLayout = qt.QHBoxLayout()
        volumeRowLayout.addWidget(self.volume)
        if self._subject_id is not None:
            self.addVolumeButton = qt.QPushButton("+")
            self.addVolumeButton.setToolTip("Import a new volume for this subject.")
            self.addVolumeButton.setMaximumWidth(30)
            self.addVolumeButton.clicked.connect(self.on_add_volume_clicked)
            volumeRowLayout.addWidget(self.addVolumeButton)
        formLayout.addRow(_("Volume:"), volumeRowLayout)

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

    def on_add_volume_clicked(self) -> None:
        """Open AddNewVolumeDialog, import the volume into this subject, refresh the combo."""
        if self._subject_id is None:
            return
        volumedlg = AddNewVolumeDialog(parent=self)
        returncode, volume_filepath, volume_name, volume_id = volumedlg.customexec_()
        if not returncode:
            return
        if not (len(volume_filepath) and len(volume_name) and len(volume_id)):
            slicer.util.errorDisplay("Required fields are missing", parent=self)
            return
        slicer.util.getModuleLogic("OpenLIFUData").add_volume_to_database(
            self._subject_id, volume_id, volume_name, volume_filepath
        )
        self._reload_volumes(select_id=volume_id)

    def _reload_volumes(self, select_id: Optional[str] = None) -> None:
        """Repopulate the volume combo from the database and optionally select an entry."""
        db = get_cur_db()
        if db is None or self._subject_id is None:
            return
        self.volume_ids = db.get_volume_ids(self._subject_id)
        was_blocked = self.volume.blockSignals(True)
        try:
            self.volume.clear()
            self.volume.setEnabled(True)
            self.add_items_to_combobox(self.volume, self.volume_ids, "volume")
        finally:
            self.volume.blockSignals(was_blocked)
        if select_id is not None:
            idx = self.volume.findData(select_id)
            if idx >= 0:
                self.volume.setCurrentIndex(idx)

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


def _format_iso_short(iso_str: Optional[str]) -> str:
    """Format an ISO-8601 timestamp as ``YYYY-MM-DD HH:MM`` for table display.

    Falls back to the raw input (or empty string) when the value is missing
    or cannot be parsed; this is the same shape ``Session.from_dict`` would
    have produced via ``datetime.fromisoformat`` followed by ``strftime``.
    """
    if not iso_str:
        return ""
    from datetime import datetime
    try:
        return datetime.fromisoformat(iso_str).strftime('%Y-%m-%d %H:%M')
    except (TypeError, ValueError):
        return str(iso_str)


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
        self.selected_session_id: Optional[str] = None
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
            # Session metadata: load the raw JSON dict (not a fully constructed
            # Session) so populating the table doesn't trigger Database
            # round-trips like load-time tracking-result sanitization.
            session_info = self.db.load_session_info(self.subject_id, session_id)
            self._populate_session_row(row, session_info)

        self.table_widget.resizeRowsToContents()
        self.table_widget.setSortingEnabled(True) # turn on sorting after edit

    def _populate_session_row(self, row: int, session_info: dict) -> None:
        """Fill a single table row from a session info dict (as returned by
        :py:meth:`openlifu.db.Database.load_session_info`)."""
        def safe_call(func, fallback="NA"):
            try:
                return func()
            except Exception:
                return fallback

        protocol_id = session_info.get("protocol_id", "")
        volume_id = session_info.get("volume_id", "")
        transducer_id = session_info.get("transducer_id", "")

        protocol_name = safe_call(lambda: self.db.load_protocol(protocol_id).name)
        volume_name = safe_call(lambda: self.db.get_volume_info(self.subject_id, volume_id)["name"])
        transducer_name = safe_call(lambda: self.db.load_transducer(transducer_id).name)

        protocol_text = f"{protocol_name} ({protocol_id})"
        volume_text = f"{volume_name} ({volume_id})"
        transducer_text = f"{transducer_name} ({transducer_id})"

        self.table_widget.setItem(row, 0, qt.QTableWidgetItem(session_info.get("name", "")))
        self.table_widget.setItem(row, 1, qt.QTableWidgetItem(session_info.get("id", "")))
        self.table_widget.setItem(row, 2, qt.QTableWidgetItem(protocol_text))
        self.table_widget.setItem(row, 3, qt.QTableWidgetItem(volume_text))
        self.table_widget.setItem(row, 4, qt.QTableWidgetItem(transducer_text))
        self.table_widget.setItem(row, 5, qt.QTableWidgetItem(_format_iso_short(session_info.get("date_created"))))
        self.table_widget.setItem(row, 6, qt.QTableWidgetItem(_format_iso_short(session_info.get("date_modified"))))

    @display_errors
    def append_session_to_list(self, session_info: dict) -> None:
        self.table_widget.setSortingEnabled(False) # turn off sorting during edit

        row = self.table_widget.rowCount
        self.table_widget.insertRow(row)
        self._populate_session_row(row, session_info)

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
            volume_ids=db_volume_ids,
            subject_id=self.subject_id,
        )
        returncode, session_parameters, load_checked = sessiondlg.customexec_()

        if not returncode:
            return

        slicer.util.getModuleLogic("OpenLIFUData").add_session_to_database(self.subject_id, session_parameters)
        new_session_info = self.db.load_session_info(self.subject_id, session_parameters["id"])
        self.append_session_to_list(new_session_info)

        if load_checked:
            self.selected_session_id = new_session_info["id"]
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
        session_name_item = self.table_widget.item(row, 0)
        session_name = session_name_item.text() if session_name_item else session_id

        # ---- Prevent loading sessions with unallowed protocols ----
        # Use load_session_info (raw JSON) so the permission check doesn't
        # incur a full Session construction; the actual load happens in the
        # caller via ``logic.load_session(...)``.
        session_info = self.db.load_session_info(self.subject_id, session_id)
        protocol = self.db.load_protocol(session_info["protocol_id"])

        if not get_user_account_mode_state() or 'admin' in get_current_user().roles:
            pass  # No enforcement needed
        else:
            if not any(role in protocol.allowed_roles for role in get_current_user().roles):
                slicer.util.errorDisplay(
                    f"Could not load the session '{session_name}' ({session_id}) because it uses a protocol "
                    f"that does not allow any of the logged-in user's roles."
                )
                return
        # -----------------------------------------------------------

        self.selected_session_id = session_id
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
            session_info = self.db.load_session_info(self.subject_id, session_id)
            protocol = self.db.load_protocol(session_info["protocol_id"])

            if not any(role in protocol.allowed_roles for role in get_current_user().roles):
                slicer.util.errorDisplay(
                    f"Could not delete the session '{session_info.get('name', session_id)}' ({session_id}) because it uses a protocol "
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

    def exec_and_get_session_id(self) -> Optional[str]:
        """
        Execute the dialog and return the selected session ID, or ``None`` if canceled.

        Only the id is returned; the caller is responsible for actually
        loading the session (typically via ``logic.load_session(...)``).
        This avoids redundant ``Database.load_session`` calls during preview
        and permission checks.
        """
        if self.exec() == qt.QDialog.Accepted:
            return self.selected_session_id
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


# ---- Transducer preview helpers ------------------------------------------

def _eval_sensitivity(
    sens: "float | list[tuple[float, float]] | None",
    frequency: float,
) -> float:
    """Evaluate a (possibly frequency-dependent) sensitivity at ``frequency``.

    Mirrors :py:func:`openlifu.xdc.element.sensitivity_at_frequency` so the
    preview dialog can compute effective per-element sensitivity without
    depending on import-time access to that helper.
    """
    if sens is None:
        return 0.0
    if isinstance(sens, list):
        if not sens:
            return 0.0
        freqs = [float(f) for f, _ in sens]
        values = [float(v) for _, v in sens]
        return float(np.interp(float(frequency), freqs, values))
    return float(sens)


def _format_num(value: float, precision: int = 3) -> str:
    try:
        return np.format_float_positional(float(value), precision=precision, trim="-")
    except Exception:
        return str(value)


def _format_sensitivity(sens, frequency: float) -> str:
    """Human-readable summary of a sensitivity, evaluated at ``frequency`` if needed."""
    if isinstance(sens, list):
        if not sens:
            return "[]"
        eff = _eval_sensitivity(sens, frequency)
        return f"{_format_num(eff)} Pa/V (interp @ {_format_num(frequency, 0)} Hz, {len(sens)} pts)"
    return f"{_format_num(sens)} Pa/V"


class TransducerPreviewDialog(qt.QDialog):
    """Standalone two-panel preview of an openlifu Transducer or TransducerArray.

    Left panel: a :class:`QTreeWidget` showing the transducer hierarchy
    (array → modules → elements) with native expand/collapse. Right panel:
    an isolated 3D view showing the transducer geometry built from
    ``transducer.get_polydata()`` plus optional body and registration
    surface meshes. Nothing is added to the data parameter node, and all
    temporary nodes are removed when the dialog closes.
    """

    def __init__(
        self,
        transducer: "openlifu.xdc.Transducer",
        body_abspath: Optional[str] = None,
        registration_surface_abspath: Optional[str] = None,
        parent="mainWindow",
    ):
        super().__init__(slicer.util.mainWindow() if parent == "mainWindow" else parent)
        self.transducer = transducer
        self._body_abspath = body_abspath
        self._registration_surface_abspath = registration_surface_abspath
        self._model_node = None
        self._body_model_node = None
        self._registration_surface_model_node = None
        self._view_node = None
        self._view_owner_node = None
        self.setWindowTitle(f"Transducer Preview - {getattr(transducer, 'name', getattr(transducer, 'id', ''))}")
        self.setWindowModality(qt.Qt.WindowModal)
        self._setup()
        self._setup_view_node()
        self._setup_model_node()
        self._setup_body_model_node()
        self._setup_registration_surface_node()
        # Reset the camera to fit the transducer in the preview view
        try:
            view_widget_3d = self.viewWidget.threeDView() if hasattr(self.viewWidget, "threeDView") else None
            if view_widget_3d is not None:
                view_widget_3d.resetFocalPoint()
                view_widget_3d.resetCamera()
        except Exception as e:
            logging.debug("TransducerPreviewDialog: could not reset camera: %s", e)
        self.finished.connect(self._cleanup)

    def _setup(self) -> None:
        screen = qt.QDesktopWidget().screenGeometry()
        self.resize(int(screen.width() * 0.55), int(screen.height() * 0.55))
        self.setMinimumWidth(700)
        self.setMinimumHeight(450)

        outer = qt.QVBoxLayout()
        self.setLayout(outer)

        splitter = qt.QSplitter(qt.Qt.Horizontal, self)
        outer.addWidget(splitter, 1)

        # Left panel: native Qt tree with collapsible sections. This avoids
        # spinning up a Chromium runtime just to render <details>/<summary>.
        self.infoTree = qt.QTreeWidget(splitter)
        self.infoTree.setColumnCount(2)
        self.infoTree.setHeaderLabels(["Field", "Value"])
        self.infoTree.setMinimumWidth(360)
        self.infoTree.setSizePolicy(qt.QSizePolicy.Preferred, qt.QSizePolicy.Expanding)
        self.infoTree.setAlternatingRowColors(True)
        self.infoTree.setRootIsDecorated(True)
        self.infoTree.setUniformRowHeights(True)
        self.infoTree.setSelectionMode(qt.QAbstractItemView.NoSelection)
        # Wider "Field" column by default; user can still drag to resize.
        header = self.infoTree.header()
        header.setSectionResizeMode(0, qt.QHeaderView.Interactive)
        header.setSectionResizeMode(1, qt.QHeaderView.Stretch)
        header.setStretchLastSection(True)
        self.infoTree.setColumnWidth(0, 200)
        try:
            self._populate_info_tree()
        except Exception as e:
            logging.warning("TransducerPreviewDialog: failed to populate info tree: %s", e)
            self.infoTree.clear()
            err_item = qt.QTreeWidgetItem(["error", str(e)])
            self.infoTree.addTopLevelItem(err_item)
        self.infoTree.expandToDepth(0)

        # Right panel: 3D view widget bound to a private view node
        self.viewWidget = slicer.qMRMLThreeDWidget(splitter)
        self.viewWidget.setMRMLScene(slicer.mrmlScene)
        self.viewWidget.setMinimumHeight(300)
        self.viewWidget.setSizePolicy(qt.QSizePolicy.Expanding, qt.QSizePolicy.Expanding)

        splitter.addWidget(self.infoTree)
        splitter.addWidget(self.viewWidget)
        splitter.setStretchFactor(0, 1)
        splitter.setStretchFactor(1, 2)
        splitter.setSizes([440, 560])

        # Close button
        bb = qt.QDialogButtonBox()
        bb.setStandardButtons(qt.QDialogButtonBox.Close)
        bb.rejected.connect(self.reject)
        bb.accepted.connect(self.accept)
        outer.addWidget(bb)

    # ---- Info tree population --------------------------------------------

    def _populate_info_tree(self) -> None:
        """Build the left-panel tree from the transducer object.

        Handles both ``TransducerArray`` (has ``modules``) and a plain
        ``Transducer`` (has ``elements``). Per-element sensitivity values
        are shown as the *effective* product of the element's stored
        sensitivity and the parent module's sensitivity, evaluated at the
        module's center frequency when frequency-dependent.
        """
        tree = self.infoTree
        tree.clear()

        obj = self.transducer
        is_array = hasattr(obj, "modules")

        def _make_item(key: str, value: str) -> qt.QTreeWidgetItem:
            """Create a tree item with key/value strings and tooltips on each cell."""
            item = qt.QTreeWidgetItem([str(key), str(value)])
            # Tooltips show the full text on hover so users can read content
            # that is clipped by the column width.
            item.setToolTip(0, str(key))
            item.setToolTip(1, str(value))
            return item

        def add_kv(parent, key: str, value: str) -> qt.QTreeWidgetItem:
            item = _make_item(key, value)
            if parent is None:
                tree.addTopLevelItem(item)
            else:
                parent.addChild(item)
            return item

        def add_branch(parent, key: str, value: str) -> qt.QTreeWidgetItem:
            """Create a tree item intended to hold children (gets tooltips too)."""
            item = _make_item(key, value)
            if parent is None:
                tree.addTopLevelItem(item)
            else:
                parent.addChild(item)
            return item

        def add_attrs(parent, attrs: dict) -> None:
            if not attrs:
                return
            attrs_item = add_branch(parent, "Attrs", f"{len(attrs)} key(s)")
            for k in sorted(str(k) for k in attrs):
                v = attrs[k]
                # Compact display for arrays / lists / dicts
                if isinstance(v, np.ndarray):
                    val_str = f"ndarray shape={v.shape} dtype={v.dtype}"
                elif isinstance(v, (list, tuple)):
                    val_str = f"{type(v).__name__}(len={len(v)})"
                elif isinstance(v, dict):
                    val_str = f"dict({len(v)} key(s))"
                else:
                    val_str = str(v)
                add_kv(attrs_item, k, val_str)

        def add_element(parent_item, element, module_freq: float, module_sens) -> None:
            module_sens_at_f = _eval_sensitivity(module_sens, module_freq)
            try:
                eff_sens = (
                    [(f, v * module_sens_at_f) for f, v in element.sensitivity]
                    if isinstance(element.sensitivity, list)
                    else float(element.sensitivity) * module_sens_at_f
                )
            except Exception:
                eff_sens = element.sensitivity
            sens_summary = _format_sensitivity(eff_sens, module_freq)
            elem_label = f"#{element.index} (pin {element.pin})"
            elem_summary = (
                f"pos [{_format_num(element.position[0])}, "
                f"{_format_num(element.position[1])}, "
                f"{_format_num(element.position[2])}]  "
                f"size [{_format_num(element.size[0])}, {_format_num(element.size[1])}]"
            )
            elem_item = add_branch(parent_item, elem_label, elem_summary)
            add_kv(elem_item, "Index", str(element.index))
            add_kv(elem_item, "Pin", str(element.pin))
            add_kv(elem_item, "Units", str(getattr(element, "units", "")))
            add_kv(
                elem_item,
                "Position",
                f"[{_format_num(element.position[0])}, "
                f"{_format_num(element.position[1])}, "
                f"{_format_num(element.position[2])}]",
            )
            try:
                az_deg, el_deg, roll_deg = np.degrees([element.az, element.el, element.roll])
                add_kv(
                    elem_item,
                    "Orientation (deg)",
                    f"az={_format_num(az_deg)}, el={_format_num(el_deg)}, roll={_format_num(roll_deg)}",
                )
            except Exception:
                pass
            add_kv(
                elem_item,
                "Size",
                f"[{_format_num(element.size[0])}, {_format_num(element.size[1])}]",
            )
            add_kv(elem_item, "Sensitivity (effective)", sens_summary)
            if isinstance(element.sensitivity, list):
                stored_item = add_branch(
                    elem_item,
                    "Sensitivity (stored)",
                    f"{len(element.sensitivity)} (freq, value) point(s)",
                )
                for f, v in element.sensitivity:
                    add_kv(stored_item, f"{_format_num(f, 0)} Hz", f"{_format_num(v)}")
            else:
                add_kv(elem_item, "Sensitivity (stored)", _format_num(element.sensitivity))

        def add_transducer(parent_item, t) -> None:
            """Populate fields and elements of a Transducer-like object.

            ``parent_item`` may be ``None`` to add directly at the top level
            of the tree.
            """
            n_elements = t.numelements() if hasattr(t, "numelements") else len(getattr(t, "elements", []))
            add_kv(parent_item, "ID", str(getattr(t, "id", "")))
            add_kv(parent_item, "Name", str(getattr(t, "name", "")))
            add_kv(parent_item, "Frequency", f"{_format_num(getattr(t, 'frequency', 0.0), 0)} Hz")
            add_kv(parent_item, "Units", str(getattr(t, "units", "")))
            add_kv(parent_item, "Sensitivity", _format_sensitivity(getattr(t, "sensitivity", 1.0), getattr(t, "frequency", 0.0)))
            add_kv(
                parent_item,
                "Crosstalk",
                f"frac={_format_num(getattr(t, 'crosstalk_frac', 0.0))}, "
                f"dist={_format_num(getattr(t, 'crosstalk_dist', 0.0))} m",
            )
            add_kv(parent_item, "Registration mesh", str(getattr(t, "registration_surface_filename", None)))
            add_kv(parent_item, "Body mesh", str(getattr(t, "transducer_body_filename", None)))

            elements_item = add_branch(parent_item, "Elements", f"{n_elements} element(s)")
            for el in getattr(t, "elements", []):
                add_element(elements_item, el, getattr(t, "frequency", 0.0), getattr(t, "sensitivity", 1.0))

            add_attrs(parent_item, getattr(t, "attrs", {}) or {})

        if is_array:
            total_elements = sum(m.numelements() for m in obj.modules)
            # Top-level summary items (no redundant root "TransducerArray" node)
            add_kv(None, "ID", str(getattr(obj, "id", "")))
            add_kv(None, "Name", str(getattr(obj, "name", "")))
            add_kv(None, "Total Elements", str(total_elements))

            modules_item = add_branch(None, "Modules", f"{len(obj.modules)} module(s)")
            for i, m in enumerate(obj.modules):
                hwid = (m.attrs or {}).get("hwid") if hasattr(m, "attrs") else None
                tx, ty, tz = (
                    m.transform[0, 3], m.transform[1, 3], m.transform[2, 3]
                ) if hasattr(m, "transform") else (0.0, 0.0, 0.0)
                module_summary = (
                    f"{getattr(m, 'id', '')} | {m.numelements()} els | "
                    f"HWID={hwid} | "
                    f"t=[{_format_num(tx)}, {_format_num(ty)}, {_format_num(tz)}]"
                )
                module_item = add_branch(modules_item, f"Module {i}", module_summary)
                add_transducer(module_item, m)

            add_attrs(None, getattr(obj, "attrs", {}) or {})
            modules_item.setExpanded(True)
        else:
            # Plain Transducer: populate top-level fields directly.
            add_transducer(None, obj)

    def _setup_view_node(self) -> None:
        tid = getattr(self.transducer, "id", "transducer")
        layoutName = f"TransducerPreview-{tid}"
        # Owner node so the layout manager doesn't manage this view
        self._view_owner_node = slicer.mrmlScene.AddNewNodeByClass("vtkMRMLScriptedModuleNode")

        viewLogic = slicer.vtkMRMLViewLogic()
        viewLogic.SetMRMLScene(slicer.mrmlScene)
        viewNode = viewLogic.AddViewNode(layoutName)
        viewNode.SetLayoutLabel("XDC")
        viewNode.SetLayoutColor([0.30, 0.55, 0.85])
        viewNode.SetName(f"view-preview-{tid}")
        viewNode.SetAndObserveParentLayoutNodeID(self._view_owner_node.GetID())
        viewNode.SetAttribute("isWizardViewNode", "true")
        viewNode.SetBackgroundColor(0.20, 0.25, 0.35)
        viewNode.SetBackgroundColor2(0.10, 0.12, 0.18)
        viewNode.SetBoxVisible(False)
        viewNode.SetAxisLabelsVisible(False)
        self._view_node = viewNode
        self.viewWidget.setMRMLViewNode(viewNode)

    def _setup_model_node(self) -> None:
        try:
            # If we were handed a TransducerArray, convert to a flat Transducer
            # just for the purpose of building the preview polydata.
            if hasattr(self.transducer, "to_transducer"):
                mesh_source = self.transducer.to_transducer()
            else:
                mesh_source = self.transducer
            polydata = mesh_source.get_polydata(units="mm", facecolor=[0.0, 0.8, 1.0, 1.0])
        except Exception as e:
            logging.warning("TransducerPreviewDialog: failed to build polydata: %s", e)
            return
        modelNode = slicer.mrmlScene.AddNewNodeByClass(
            "vtkMRMLModelNode", f"TransducerPreview-{getattr(self.transducer, 'id', 'transducer')}"
        )
        modelNode.SetAndObservePolyData(polydata)
        modelNode.CreateDefaultDisplayNodes()
        displayNode = modelNode.GetDisplayNode()
        if displayNode is not None:
            # The polydata carries an unsigned-char color array as point
            # scalars; disable scalar coloring so VTK doesn't try to compute a
            # scalar range from it (which logs "Bad table range: [0, -1]").
            displayNode.SetScalarVisibility(False)
            displayNode.SetColor(0.0, 0.8, 1.0)
            displayNode.SetOpacity(1.0)
            displayNode.SetVisibility(True)
            # Restrict visibility to our private view node only
            displayNode.SetViewNodeIDs([self._view_node.GetID()])
        self._model_node = modelNode

    def _setup_body_model_node(self) -> None:
        """Optionally load the transducer body mesh into the preview view."""
        if not self._body_abspath:
            return
        try:
            expected_name = getattr(self.transducer, "transducer_body_filename", None)
            if expected_name and Path(self._body_abspath).name != expected_name:
                logging.warning(
                    "TransducerPreviewDialog: body file name mismatch (got %s, expected %s); skipping",
                    Path(self._body_abspath).name,
                    expected_name,
                )
                return
            bodyNode = slicer.util.loadModel(self._body_abspath)
        except Exception as e:
            logging.warning("TransducerPreviewDialog: could not load body mesh '%s': %s", self._body_abspath, e)
            return
        if bodyNode is None:
            return
        bodyNode.SetName(f"TransducerPreviewBody-{getattr(self.transducer, 'id', 'transducer')}")
        displayNode = bodyNode.GetDisplayNode()
        if displayNode is None:
            bodyNode.CreateDefaultDisplayNodes()
            displayNode = bodyNode.GetDisplayNode()
        if displayNode is not None:
            displayNode.SetColor(0.85, 0.85, 0.88)
            displayNode.SetOpacity(0.45)
            displayNode.SetVisibility(True)
            if self._view_node is not None:
                displayNode.SetViewNodeIDs([self._view_node.GetID()])
        self._body_model_node = bodyNode

    def _setup_registration_surface_node(self) -> None:
        """Optionally load the transducer registration surface mesh."""
        if not self._registration_surface_abspath:
            return
        try:
            expected_name = getattr(self.transducer, "registration_surface_filename", None)
            if expected_name and Path(self._registration_surface_abspath).name != expected_name:
                logging.warning(
                    "TransducerPreviewDialog: registration surface name mismatch (got %s, expected %s); skipping",
                    Path(self._registration_surface_abspath).name,
                    expected_name,
                )
                return
            surfNode = slicer.util.loadModel(self._registration_surface_abspath)
        except Exception as e:
            logging.warning(
                "TransducerPreviewDialog: could not load registration surface '%s': %s",
                self._registration_surface_abspath, e,
            )
            return
        if surfNode is None:
            return
        surfNode.SetName(
            f"TransducerPreviewSurface-{getattr(self.transducer, 'id', 'transducer')}"
        )
        displayNode = surfNode.GetDisplayNode()
        if displayNode is None:
            surfNode.CreateDefaultDisplayNodes()
            displayNode = surfNode.GetDisplayNode()
        if displayNode is not None:
            displayNode.SetColor(0.45, 0.85, 0.55)
            displayNode.SetOpacity(0.55)
            displayNode.SetVisibility(True)
            if self._view_node is not None:
                displayNode.SetViewNodeIDs([self._view_node.GetID()])
        self._registration_surface_model_node = surfNode

    def _cleanup(self, *args) -> None:
        for node_attr in (
            "_model_node",
            "_body_model_node",
            "_registration_surface_model_node",
            "_view_node",
            "_view_owner_node",
        ):
            node = getattr(self, node_attr, None)
            if node is not None:
                try:
                    slicer.mrmlScene.RemoveNode(node)
                except Exception as e:
                    logging.debug("TransducerPreviewDialog: cleanup failed for %s: %s", node_attr, e)
                setattr(self, node_attr, None)


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
    * **Preview** -- opens a standalone two-panel dialog rendering the
      transducer geometry alongside its ``_repr_html_`` summary, without
      loading it into the main Slicer scene.
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
        self.previewButton.setToolTip("Open a standalone preview of the selected transducer")
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
                import openlifu.xdc.util
                obj = openlifu.xdc.util.load_transducer_from_file(filepath, convert_array=False)
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
                import openlifu.xdc
                arr = openlifu.xdc.TransducerArray.get_connected(
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
            obj = self.db.load_transducer(tid, convert_array=False)
        except Exception as e:
            slicer.util.errorDisplay(f"Could not load transducer '{tid}': {e}", parent=self)
            return
        try:
            abspaths = self.db.get_transducer_absolute_filepaths(tid) or {}
        except Exception:
            abspaths = {}
        body_path = abspaths.get("transducer_body_abspath")
        registration_path = abspaths.get("registration_surface_abspath")
        dialog = TransducerPreviewDialog(
            obj,
            body_abspath=body_path,
            registration_surface_abspath=registration_path,
            parent=self,
        )
        dialog.exec_()

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


def _encode_hwid_b58(raw_hex: str) -> str:
    """Encode a raw-hex HWID into base58.

    Matches the test app's ``lifu_console.py`` / ``lifu_transmitter.py``
    pattern: the raw-hex *string* is sliced to ``HW_ID_DATA_LENGTH``
    (=12) characters before decoding (=> first 6 bytes, ~8 base58
    chars). The raw hex returned by the SDK is 24 chars (12 bytes), but
    only the first half is used for the display ID. base58 ships with
    the openlifu_sdk environment; if for some reason it isn't
    importable we fall back to the raw hex string instead of failing.
    """
    try:
        import base58
        from openlifu_sdk.io.LIFUConfig import HW_ID_DATA_LENGTH
        return base58.b58encode(
            bytes.fromhex(raw_hex[:HW_ID_DATA_LENGTH])
        ).decode("utf-8")
    except Exception as enc_err:  # noqa: BLE001
        logging.warning("Could not base58-encode HWID %r: %s", raw_hex, enc_err)
        return raw_hex


class _SimulatedTransducerPickerDialog(qt.QDialog):
    """Modal dialog for picking which transducer the simulated LIFUInterface
    should mimic.

    Lists every transducer in the currently loaded database and returns the
    chosen :class:`openlifu.xdc.TransducerArray` (or :class:`Transducer`) via
    :meth:`exec_and_get_transducer`. The picked object is later passed to
    :py:meth:`OpenLIFUSonicationControlLogic.connect_simulated_interface` so the
    simulator's module count and per-module user_configs match the selection.
    """

    def __init__(self, db: "openlifu.db.Database", parent: Optional[qt.QWidget] = None):
        super().__init__(slicer.util.mainWindow() if parent is None else parent)
        self.setWindowTitle("Pick Simulated Transducer")
        self.setWindowModality(qt.Qt.WindowModal)
        self.db = db
        self.selected_transducer = None  # openlifu TransducerArray | Transducer | None

        layout = qt.QVBoxLayout(self)

        layout.addWidget(qt.QLabel(
            "Pick a transducer from the database for the simulated device to mimic.",
            self,
        ))

        cols = ["ID", "Name", "Modules"]
        self.table = qt.QTableWidget(self)
        self.table.setColumnCount(len(cols))
        self.table.setHorizontalHeaderLabels(cols)
        self.table.setSelectionBehavior(qt.QAbstractItemView.SelectRows)
        self.table.setSelectionMode(qt.QAbstractItemView.SingleSelection)
        self.table.setEditTriggers(qt.QAbstractItemView.NoEditTriggers)
        self.table.verticalHeader().setVisible(False)
        self.table.setShowGrid(False)
        self.table.horizontalHeader().setHighlightSections(False)
        header = self.table.horizontalHeader()
        header.setSectionResizeMode(0, qt.QHeaderView.ResizeToContents)
        header.setSectionResizeMode(1, qt.QHeaderView.Stretch)
        header.setSectionResizeMode(2, qt.QHeaderView.ResizeToContents)
        self.table.setSortingEnabled(True)
        layout.addWidget(self.table)

        # Buttons
        bb = qt.QDialogButtonBox()
        self.okButton = bb.addButton("Connect", qt.QDialogButtonBox.AcceptRole)
        self.okButton.setEnabled(False)
        self.cancelButton = bb.addButton("Cancel", qt.QDialogButtonBox.RejectRole)
        bb.accepted.connect(self._onAccept)
        bb.rejected.connect(self.reject)
        layout.addWidget(bb)

        self.table.itemSelectionChanged.connect(
            lambda: self.okButton.setEnabled(self._selected_id() is not None)
        )
        self.table.doubleClicked.connect(self._onAccept)

        screen = qt.QDesktopWidget().screenGeometry()
        self.resize(int(screen.width() * 0.30), int(screen.height() * 0.30))

        self._populate()

    def _populate(self) -> None:
        self.table.setSortingEnabled(False)
        self.table.clearContents()
        try:
            ids = list(self.db.get_transducer_ids() or [])
        except Exception as e:  # noqa: BLE001
            logging.warning("Could not list transducer ids: %s", e)
            ids = []
        self.table.setRowCount(len(ids))
        for row, tid in enumerate(ids):
            try:
                obj = self.db.load_transducer(tid, convert_array=False)
                name = getattr(obj, "name", tid) or tid
                n_mods = len(obj.modules) if hasattr(obj, "modules") else 1
            except Exception as e:  # noqa: BLE001
                logging.warning("Could not load transducer %s for picker: %s", tid, e)
                name = tid
                n_mods = 0
            self.table.setItem(row, 0, qt.QTableWidgetItem(str(tid)))
            self.table.setItem(row, 1, qt.QTableWidgetItem(str(name)))
            self.table.setItem(row, 2, qt.QTableWidgetItem(str(n_mods)))
        self.table.resizeRowsToContents()
        self.table.setSortingEnabled(True)

    def _selected_id(self) -> Optional[str]:
        items = self.table.selectedItems()
        if not items:
            return None
        row = items[0].row()
        idItem = self.table.item(row, 0)
        return idItem.text() if idItem else None

    def _onAccept(self) -> None:
        tid = self._selected_id()
        if not tid:
            return
        try:
            self.selected_transducer = self.db.load_transducer(tid, convert_array=False)
        except Exception as e:  # noqa: BLE001
            slicer.util.errorDisplay(
                f"Failed to load transducer '{tid}':\n\n{e}",
                parent=self,
            )
            return
        self.accept()

    def exec_and_get_transducer(self):
        """Show the picker and return the picked transducer object, or ``None``."""
        if self.exec() == qt.QDialog.Accepted:
            return self.selected_transducer
        return None


class _DeviceStatusDialog(qt.QDialog):
    """Modal dialog showing the current TX / HV LIFUInterface state.

    Also exposes a "Connect Simulated Device" button when nothing is
    connected, and a "Disconnect Simulated Device" button when the
    currently active interface is the in-memory
    :class:`~openlifu_sdk.ui.simulated_interface.SimulatedLIFUInterface`.
    """

    def __init__(self, sc_logic, parent=None):
        qt.QDialog.__init__(self, parent or slicer.util.mainWindow())
        self._sc_logic = sc_logic
        self.setWindowTitle("Device Status")
        self.setWindowModality(qt.Qt.ApplicationModal)
        self.setMinimumWidth(360)

        layout = qt.QVBoxLayout(self)

        # Body label: monospace so HWIDs / firmware versions line up.
        self._info_label = qt.QLabel(self)
        self._info_label.setTextInteractionFlags(qt.Qt.TextSelectableByMouse)
        font = qt.QFont("Courier")
        font.setStyleHint(qt.QFont.Monospace)
        self._info_label.setFont(font)
        layout.addWidget(self._info_label)

        # Button row: retry (when locked-out) + simulator action + Close.
        button_row = qt.QHBoxLayout()
        self._retry_button = qt.QPushButton("Retry", self)
        self._retry_button.setToolTip(
            "Retry connecting to the LIFU hardware. Use this after the "
            "other process holding the hardware has been closed."
        )
        self._retry_button.clicked.connect(self._onRetryButtonClicked)
        self._retry_button.setVisible(False)
        button_row.addWidget(self._retry_button)
        self._sim_button = qt.QPushButton(self)
        self._sim_button.clicked.connect(self._onSimButtonClicked)
        button_row.addWidget(self._sim_button)
        button_row.addStretch(1)
        close_button = qt.QPushButton("Close", self)
        close_button.clicked.connect(lambda *args: self.accept())
        button_row.addWidget(close_button)
        layout.addLayout(button_row)

        self._refresh()

    def _refresh(self) -> None:
        """Repopulate the info label and update the simulator button text."""
        iface = getattr(self._sc_logic, "cur_lifu_interface", None)
        is_simulated = bool(getattr(self._sc_logic, "is_simulated", False))
        in_use_pid = getattr(self._sc_logic, "lifu_hw_in_use_pid", None)

        # Special case: no real interface because another process owns it.
        if iface is None and in_use_pid is not None:
            lines = [
                "The LIFU hardware interface is currently in use by another",
                f"process (PID {in_use_pid}).",
                "",
                "Only one application can talk to the LIFU device at a time.",
                "Close the other application (or stop the process) and click",
                "'Retry' to try connecting again.",
                "",
                "You can also click 'Connect Simulated Device' to start an",
                "in-memory simulator (no hardware is touched).",
            ]
            self._info_label.setText("\n".join(lines))
            self._retry_button.setVisible(True)
            self._sim_button.setText("Connect Simulated Device")
            self._sim_button.setToolTip(
                "Replace the (currently unavailable) real LIFUInterface "
                "with an in-memory simulator for development and testing. "
                "No actual hardware is touched."
            )
            self._sim_button.setVisible(True)
            self._sim_button.setEnabled(True)
            return

        self._retry_button.setVisible(False)

        try:
            tx_conn, hv_conn = iface.is_device_connected() if iface else (False, False)
        except Exception as e:  # noqa: BLE001
            self._info_label.setText(f"Could not query device connection status:\n{e}")
            self._sim_button.setVisible(False)
            return

        if is_simulated:
            summary = "Simulated hardware device is connected (no real device)."
        elif tx_conn and hv_conn:
            summary = "Hardware device is fully connected."
        elif tx_conn or hv_conn:
            half = "TX only" if tx_conn else "HV only"
            summary = f"Hardware device is partially connected ({half})."
        else:
            summary = "No hardware device is currently connected."

        lines: List[str] = [summary, ""]

        # ---- Console (HV controller) ----
        lines.append("Console:")
        if hv_conn:
            hv = iface.hvcontroller
            try:
                con_hwid = _encode_hwid_b58(hv.get_hardware_id(raw_hex=True))
            except Exception as e:  # noqa: BLE001
                logging.warning("Could not read console HWID: %s", e)
                con_hwid = "unknown"
            try:
                con_ver = hv.get_version()
            except Exception as e:  # noqa: BLE001
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
            except Exception as e:  # noqa: BLE001
                logging.warning("Could not read TX module count: %s", e)
                module_count = 0
            lines.append(f"  Modules connected: {module_count}")
            for module_idx in range(module_count):
                lines.append(f"  Module {module_idx}:")
                try:
                    mod_hwid = _encode_hwid_b58(
                        tx.get_hardware_id(module=module_idx, raw_hex=True)
                    )
                except Exception as e:  # noqa: BLE001
                    logging.warning("Could not read TX module %d HWID: %s", module_idx, e)
                    mod_hwid = "unknown"
                try:
                    mod_ver = tx.get_version(module=module_idx)
                except Exception as e:  # noqa: BLE001
                    logging.warning("Could not read TX module %d firmware: %s", module_idx, e)
                    mod_ver = "unknown"
                lines.append(f"    HWID: {mod_hwid}")
                lines.append(f"    Firmware: v{mod_ver}")
        else:
            lines.append("  (not connected)")

        self._info_label.setText("\n".join(lines))

        # Show Connect-sim only when no real connection; show
        # Disconnect-sim whenever the simulator is active.
        if is_simulated:
            self._sim_button.setText("Disconnect Simulated Device")
            self._sim_button.setToolTip(
                "Tear down the in-memory simulator and restore the real "
                "LIFUInterface."
            )
            self._sim_button.setVisible(True)
            self._sim_button.setEnabled(not bool(getattr(self._sc_logic, "running", False)))
        elif not tx_conn and not hv_conn:
            self._sim_button.setText("Connect Simulated Device")
            self._sim_button.setToolTip(
                "Replace the real LIFUInterface with an in-memory simulator "
                "for development and testing. No actual hardware is touched."
            )
            self._sim_button.setVisible(True)
            self._sim_button.setEnabled(True)
        else:
            self._sim_button.setVisible(False)

    @display_errors
    def _onSimButtonClicked(self, checked: bool = False) -> None:
        if bool(getattr(self._sc_logic, "is_simulated", False)):
            self._sc_logic.disconnect_simulated_interface()
            self._refresh()
            return

        db = get_cur_db()
        if db is None:
            slicer.util.warningDisplay(
                "A database must be loaded before connecting a simulated device, "
                "so a transducer can be picked for the simulator to mimic.",
                parent=self,
            )
            return
        picker = _SimulatedTransducerPickerDialog(db, parent=self)
        transducer = picker.exec_and_get_transducer()
        if transducer is None:
            return
        self._sc_logic.connect_simulated_interface(transducer=transducer)
        self._refresh()

    @display_errors
    def _onRetryButtonClicked(self, checked: bool = False) -> None:
        """Re-attempt to claim the real LIFU hardware interface.

        Delegates to ``OpenLIFUSonicationControlLogic.retry_create_lifu_interface``
        and refreshes the dialog so the user sees the new state. On success
        we also restart the monitoring timer (idle while locked out) and
        push a state refresh into the SonicationControl widget so its own
        labels and buttons match reality without waiting for the next
        monitor tick.
        """
        new_exc = self._sc_logic.retry_create_lifu_interface()
        if new_exc is None:
            timer = getattr(self._sc_logic, "monitoring_timer", None)
            if timer is not None and not timer.isActive():
                timer.start()
            try:
                sc_widget = slicer.util.getModuleWidget("OpenLIFUSonicationControl")
            except Exception:  # noqa: BLE001
                sc_widget = None
            if sc_widget is not None:
                fn = getattr(sc_widget, "updateAllButtonsEnabled", None)
                if callable(fn):
                    try:
                        fn()
                    except Exception:  # noqa: BLE001
                        logging.warning(
                            "Failed to call updateAllButtonsEnabled on SonicationControl widget after retry",
                            exc_info=True,
                        )
            # Repaint every module header so the device button outline drops
            # its red "in use" state immediately. Without this nudge the
            # outline would only refresh on the next OWSignal-driven event,
            # which never fires when retry succeeds with no hardware actually
            # plugged in (we'd just sit on red until the user navigates).
            for module_name in (
                "OpenLIFUData",
                "OpenLIFUPrePlanning",
                "OpenLIFUTransducerLocalization",
                "OpenLIFUSonicationPlanner",
                "OpenLIFUSonicationControl",
                "OpenLIFUProtocolConfig",
                "OpenLIFUTransducerTracker",
            ):
                try:
                    w = slicer.util.getModuleWidget(module_name)
                except Exception:  # noqa: BLE001
                    continue
                header = getattr(w, "module_header", None)
                if header is not None:
                    try:
                        header.refresh_all()
                    except Exception:  # noqa: BLE001
                        logging.warning(
                            "Failed to refresh module header for %s after retry",
                            module_name,
                            exc_info=True,
                        )
        self._refresh()


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


# ---------------------------------------------------------------------------
# Protocol Manager
# ---------------------------------------------------------------------------
#
# These classes were moved here from the (now-deleted) OpenLIFUProtocolConfig
# module. The standalone Protocol Configuration module was replaced with a
# popup-table workflow modeled after :class:`TransducerManagerDialog`:
#
#   * :class:`ProtocolManagerDialog` -- table + New/Import/Duplicate/Edit/
#     Preview/Export/Delete actions.
#   * :class:`ProtocolEditDialog`    -- modal Save/Cancel editor with
#     name/ID/description above a scrollable area of collapsible parameter
#     sections.
#   * :class:`ProtocolPreviewDialog` -- modal QTreeWidget JSON viewer.
#
# The specialized parameter-section form widgets (Sim Setup, Delay/Apodization/
# Segmentation methods, Parameter Constraints, Solution Analysis Options) are
# preserved verbatim from the old module so the editor UX matches.


class OpenLIFUSimSetupDefinitionFormWidget(OpenLIFUAbstractDataclassDefinitionFormWidget):
    def __init__(self, parent: Optional[qt.QWidget] = None):
        import openlifu.sim
        super().__init__(openlifu.sim.SimSetup, parent, is_collapsible=True, collapsible_title="Simulation Setup")

        x_ext_hbox = self._field_widgets['x_extent'].layout()
        y_ext_hbox = self._field_widgets['y_extent'].layout()
        z_ext_hbox = self._field_widgets['z_extent'].layout()

        self.modify_widget_spinbox(x_ext_hbox.itemAt(0).widget(), default_value=-30, min_value=-200, max_value=-1)
        self.modify_widget_spinbox(x_ext_hbox.itemAt(1).widget(), default_value=30, min_value=1, max_value=200)
        self.modify_widget_spinbox(y_ext_hbox.itemAt(0).widget(), default_value=-30, min_value=-200, max_value=-1)
        self.modify_widget_spinbox(y_ext_hbox.itemAt(1).widget(), default_value=30, min_value=1, max_value=200)
        self.modify_widget_spinbox(z_ext_hbox.itemAt(0).widget(), default_value=-4, min_value=-4, max_value=-4)
        self.modify_widget_spinbox(z_ext_hbox.itemAt(1).widget(), default_value=60, min_value=1, max_value=200)

        spacing_spinbox = self._field_widgets['spacing']
        self.modify_widget_spinbox(spacing_spinbox, default_value=1.0, min_value=0.1, max_value=2.0)

        options_dicttablewidget = self._field_widgets['options']
        options_dicttablewidget.key_name = "Simulation Option"
        options_dicttablewidget.val_name = "Value"
        options_dicttablewidget.add_button.text = "Add Simulation Option"
        options_dicttablewidget.remove_button.text = "Remove Simulation Option"
        options_dicttablewidget.table.setHorizontalHeaderLabels(["Simulation Option", "Value"])
        options_dicttablewidget.table.horizontalHeader().setSectionResizeMode(qt.QHeaderView.ResizeToContents)


class OpenLIFUAbstractDelayMethodDefinitionFormWidget(OpenLIFUAbstractMultipleABCDefinitionFormWidget):
    def __init__(self):
        import openlifu.bf.delay_methods
        super().__init__([openlifu.bf.delay_methods.Direct], is_collapsible=False, collapsible_title="Delay Method", custom_abc_title="Delay Method")
        self.forms.setCurrentIndex(0)
        direct_definition_form_widget = self.forms.widget(0)
        c0_spinbox = direct_definition_form_widget._field_widgets['c0']
        direct_definition_form_widget.modify_widget_spinbox(c0_spinbox, default_value=1480, min_value=1000, max_value=3000)


class OpenLIFUAbstractApodizationMethodDefinitionFormWidget(OpenLIFUAbstractMultipleABCDefinitionFormWidget):
    def __init__(self):
        import openlifu.bf.apod_methods
        super().__init__([openlifu.bf.apod_methods.MaxAngle, openlifu.bf.apod_methods.PiecewiseLinear, openlifu.bf.apod_methods.Uniform], is_collapsible=False, collapsible_title="Apodization Method", custom_abc_title="Apodization Method")
        # Drive the index change via the selector so the combo box and the
        # stacked widget stay in sync (setting forms.setCurrentIndex directly
        # leaves the combo box at index 0, leading to silent mis-saves).
        self.selector.setCurrentIndex(2)
        maxangle_definition_form_widget = self.forms.widget(0)
        max_angle_spinbox = maxangle_definition_form_widget._field_widgets['max_angle']
        maxangle_definition_form_widget.modify_widget_spinbox(max_angle_spinbox, default_value=30, min_value=0, max_value=90)


def _get_form_as_segmentation_method(self, post_init: bool = True):
    """Custom replacement for ``get_form_as_class`` for the segmentation form."""
    d = self.get_form_as_dict()
    if self._cls.__name__ in ["UniformWater", "UniformTissue"]:
        d.pop("ref_material")
    if post_init:
        return self._cls(**d)
    else:
        return instantiate_without_post_init(self._cls, **d)


class OpenLIFUAbstractSegmentationMethodDefinitionFormWidget(OpenLIFUAbstractMultipleABCDefinitionFormWidget):
    """Custom multi-ABC form widget for SegmentationMethod that disables
    ``ref_material`` editing on the UniformWater / UniformTissue forms."""

    def __init__(self):
        import openlifu.seg.seg_methods
        cls_list = [openlifu.seg.seg_methods.UniformSegmentation, openlifu.seg.seg_methods.UniformTissue, openlifu.seg.seg_methods.UniformWater]
        is_collapsible = False
        parent: Optional[qt.QWidget] = None
        custom_abc_title = "Segmentation Method"

        self.cls_list = cls_list
        self.base_class_name = cls_list[0].__bases__[0].__name__
        self.custom_abc_title = self.base_class_name if custom_abc_title is None else custom_abc_title

        qt.QWidget.__init__(self, parent)

        top_level_layout = qt.QFormLayout(self)
        self.selector = qt.QComboBox()
        self.forms = qt.QStackedWidget()

        for cls in cls_list:
            self.selector.addItem(cls.__name__)
            widget = OpenLIFUAbstractDataclassDefinitionFormWidget(cls, parent, is_collapsible, "Segmentation Method")
            widget.get_form_as_class = types.MethodType(_get_form_as_segmentation_method, widget)
            self.forms.addWidget(widget)

        top_level_layout.addRow(qt.QLabel(f"{self.custom_abc_title} type"), self.selector)
        top_level_layout.addRow(qt.QLabel(f"{self.custom_abc_title} options"), self.forms)
        self.selector.currentIndexChanged.connect(self._on_index_changed)

        # Default to UniformWater (drive via selector so the combo box and the
        # stacked widget stay in sync).
        self.selector.setCurrentIndex(2)

        for idx in (1, 2):  # UniformTissue, UniformWater: lock ref_material
            ref_material_line_edit = self.forms.widget(idx)._field_widgets['ref_material']
            ref_material_line_edit.setEnabled(False)

        for abc_form_widget_index in range(self.forms.count):
            materials_dicttablewidget = self.forms.widget(abc_form_widget_index)._field_widgets['materials']
            materials_dicttablewidget.key_name = "Material"
            materials_dicttablewidget.val_name = "Definition"
            materials_dicttablewidget.add_button.text = "Add Material"
            materials_dicttablewidget.remove_button.text = "Remove Material"
            materials_dicttablewidget.table.setHorizontalHeaderLabels(["Material", "Definition"])
            materials_dicttablewidget.table.horizontalHeader().setSectionResizeMode(qt.QHeaderView.ResizeToContents)


class OpenLIFUParameterConstraintsWidget(DictTableWidget):
    """Customized DictTableWidget with a domain-specific Add dialog."""

    class CreateParameterParameterConstraintDialog(qt.QDialog):
        """Dialog for creating a parameter constraint (warning + error thresholds)."""

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
                "outside_inclusive": "is outside inclusive",
            }
            self.inverse_operator_display_map = {v: k for k, v in self.operator_display_map.items()}

            # Pull the comprehensive parameter list from openlifu's
            # solution_analysis.PARAM_FORMATS so this dialog automatically
            # picks up new analysis parameters as they are added upstream.
            # Each PARAM_FORMATS entry is
            # ``[aggregation, format_str, units, display_name]``; we use
            # ``display_name (units)`` as the user-facing label and the dict
            # key as the parameter id stored in the constraint.
            import openlifu.plan.solution_analysis
            param_formats = openlifu.plan.solution_analysis.PARAM_FORMATS
            self.parameter_key_map: Dict[str, str] = {}
            self._parameter_format_map: Dict[str, str] = {}
            for param_id, fmt in param_formats.items():
                display_name, units, fmt_str = fmt[3], fmt[2], fmt[1]
                label = f"{display_name} ({units})" if units else display_name
                # If two upstream entries collide on a label, fall back to
                # appending the raw id to keep the combo box unambiguous.
                if label in self.parameter_key_map:
                    label = f"{label} [{param_id}]"
                self.parameter_key_map[label] = param_id
                self._parameter_format_map[label] = fmt_str
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
            self.parameter_name_input.currentTextChanged.connect(self._update_spinbox_precision)
            formLayout.addRow(_("Parameter Name:"), self.parameter_name_input)

            self.operator_selector = qt.QComboBox()
            self.operator_selector.addItems(list(self.operator_display_map.values()))
            self.operator_selector.currentTextChanged.connect(self._update_visible_spinboxes)
            formLayout.addRow(_("Operator:"), self.operator_selector)

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
            formLayout.addRow(_("Warning Value(s):"), warning_container)

            error_container = qt.QWidget()
            error_container.setLayout(self.error_box_layout)
            formLayout.addRow(_("Error Value(s):"), error_container)

            self._init_spinboxes()

            self.buttonBox = qt.QDialogButtonBox()
            self.buttonBox.setStandardButtons(qt.QDialogButtonBox.Ok | qt.QDialogButtonBox.Cancel)
            formLayout.addWidget(self.buttonBox)

            self.buttonBox.rejected.connect(self.reject)
            self.buttonBox.accepted.connect(self._on_accept)

        def _init_spinboxes(self):
            for _i in range(2):
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
            self._update_spinbox_precision(self.parameter_name_input.currentText)

        def _update_spinbox_precision(self, display_label: str) -> None:
            """Apply the upstream PARAM_FORMATS precision to both spinbox rows.

            ``PARAM_FORMATS`` entries are like ``"0.3f"``; we pull the digit
            count after the dot and apply it to every spinbox. Falls back to
            2 decimals if the format string is unparseable.
            """
            fmt_str = self._parameter_format_map.get(display_label, "")
            decimals = 2
            if "." in fmt_str:
                try:
                    decimals = int(fmt_str.split(".", 1)[1].rstrip("f").rstrip())
                except ValueError:
                    pass
            for sb in (*self.warning_spinboxes, *self.error_spinboxes):
                sb.setDecimals(decimals)
                sb.setSingleStep(10 ** -decimals if decimals > 0 else 1)

        def _update_visible_spinboxes(self, display_operator: str):
            operator = self.inverse_operator_display_map[display_operator]
            use_two_values = operator in ['within', 'inside', 'outside', 'outside_inclusive']
            for i in range(2):
                self.warning_spinboxes[i].setVisible(use_two_values or i == 0)
                self.error_spinboxes[i].setVisible(use_two_values or i == 0)
            self.warning_and_label.setVisible(use_two_values)
            self.error_and_label.setVisible(use_two_values)

        def _get_parameter_constraint_as_class(self) -> "openlifu.plan.ParameterConstraint":
            import openlifu.plan
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

            return openlifu.plan.ParameterConstraint(operator, warning_value, error_value)

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
        self.add_button.text = "Add Parameter Constraint"
        self.remove_button.text = "Remove Parameter Constraint"

    def _open_add_dialog(self):
        existing_keys = list(self.to_dict().keys())
        createDlg = self.CreateParameterParameterConstraintDialog(existing_keys)
        returncode, param, param_constraint = createDlg.customexec_()
        if not returncode:
            return
        self._add_row(param, param_constraint)


class OpenLIFUSolutionAnalysisOptionsDefinitionFormWidget(OpenLIFUAbstractDataclassDefinitionFormWidget):
    def __init__(self, parent: Optional[qt.QWidget] = None):
        import openlifu.plan
        super().__init__(openlifu.plan.SolutionAnalysisOptions, parent, collapsible_title="Solution Analysis Options")

        old_param_constraints_dicttablewidget = self._field_widgets['param_constraints']
        new_param_constraints_widget = OpenLIFUParameterConstraintsWidget()
        replace_widget(old_param_constraints_dicttablewidget, new_param_constraints_widget)
        self._field_widgets['param_constraints'] = new_param_constraints_widget


# ---- Default protocol factories ----

def _default_protocol_blueprint() -> Dict[str, Any]:
    """Common keyword args for a freshly-created Protocol (no id/name/description/roles)."""
    import openlifu.bf
    import openlifu.bf.apod_methods
    import openlifu.bf.delay_methods
    import openlifu.bf.focal_patterns
    import openlifu.plan
    import openlifu.seg.seg_methods
    import openlifu.seg.virtual_fit
    import openlifu.sim
    return dict(
        pulse=openlifu.bf.Pulse(),
        sequence=openlifu.bf.Sequence(),
        focal_pattern=openlifu.bf.focal_patterns.SinglePoint(),
        sim_setup=openlifu.sim.SimSetup(),
        delay_method=openlifu.bf.delay_methods.Direct(),
        apod_method=openlifu.bf.apod_methods.Uniform(),
        seg_method=openlifu.seg.seg_methods.UniformWater(),
        param_constraints={},
        target_constraints=[],
        analysis_options=openlifu.plan.SolutionAnalysisOptions(),
        virtual_fit_options=openlifu.seg.virtual_fit.VirtualFitOptions(),
    )


def _build_default_new_protocol() -> "openlifu.plan.Protocol":
    """Return a freshly-created Protocol pre-populated with the current user's
    non-admin roles, used as the starting point for the *New* action."""
    import openlifu.plan
    user = get_current_user()
    allowed_roles = [r for r in (user.roles if user is not None else []) if r != "admin"]
    return openlifu.plan.Protocol(
        name="New Protocol",
        id="new_protocol",
        description="",
        allowed_roles=allowed_roles,
        **_default_protocol_blueprint(),
    )


def _generate_unique_protocol_id(db: "openlifu.db.Database", base: str = "new_protocol") -> str:
    """Return ``{base}_N`` where N is the smallest positive integer that does
    not collide with any existing protocol in the database or with the
    in-memory loaded set."""
    try:
        existing = set(db.get_protocol_ids() or []) if db is not None else set()
    except Exception:
        existing = set()
    try:
        existing.update(get_openlifu_data_parameter_node().loaded_protocols.keys())
    except Exception:
        pass
    i = 1
    while f"{base}_{i}" in existing:
        i += 1
    return f"{base}_{i}"


# ---- Editor dialog ----

class ProtocolEditDialog(qt.QDialog):
    """Modal Save/Cancel editor for a single :class:`openlifu.plan.Protocol`.

    Layout:
        * Header: Name / ID / Description (always visible).
        * Body: scrollable area of collapsible parameter sections (Allowed
          Roles, Pulse, Sequence, Focal Pattern, Sim Setup, Delay/Apod/Seg
          methods, Parameter Constraints, Target Constraints, Solution
          Analysis Options, Virtual Fit Options).
        * Footer: validity indicator + Save / Cancel buttons.
    """

    def __init__(self, protocol: "openlifu.plan.Protocol", db: "openlifu.db.Database", parent="mainWindow"):
        super().__init__(slicer.util.mainWindow() if parent == "mainWindow" else parent)
        self.setWindowTitle("Edit Protocol")
        self.setWindowModality(qt.Qt.WindowModal)
        self.setWindowFlags(self.windowFlags() & ~qt.Qt.WindowContextHelpButtonHint)
        self._db = db
        self._initial_protocol = protocol
        self._saved_protocol: Optional["openlifu.plan.Protocol"] = None
        self._setup()
        self._populate_ui_from_protocol(protocol)
        # Update validity once initial state is loaded.
        self._update_validity_indicator()
        # Populate initial collapsible-section summaries from the loaded values.
        self._refresh_section_summaries()

    def get_saved_protocol(self) -> Optional["openlifu.plan.Protocol"]:
        """Return the protocol the user committed via Save, or ``None`` if cancelled."""
        return self._saved_protocol

    # ---- UI build ----
    def _setup(self) -> None:
        import openlifu.bf
        import openlifu.plan
        import openlifu.seg.virtual_fit
        outer = qt.QVBoxLayout()
        self.setLayout(outer)

        # --- Header (Name / ID / Description) ---
        headerForm = qt.QFormLayout()
        self.nameLineEdit = qt.QLineEdit()
        self.nameLineEdit.setToolTip("The name of the protocol")
        self.idLineEdit = qt.QLineEdit()
        self.idLineEdit.setToolTip("The unique identifier of the protocol")
        self.descriptionTextEdit = qt.QPlainTextEdit()
        self.descriptionTextEdit.setToolTip("A more detailed description of the protocol")
        self.descriptionTextEdit.setMaximumHeight(80)
        headerForm.addRow(_("Name"), self.nameLineEdit)
        headerForm.addRow(_("Protocol ID"), self.idLineEdit)
        headerForm.addRow(_("Description"), self.descriptionTextEdit)
        outer.addLayout(headerForm)

        # --- Scrollable body ---
        self.scrollArea = qt.QScrollArea()
        self.scrollArea.setWidgetResizable(True)
        self.scrollArea.setHorizontalScrollBarPolicy(qt.Qt.ScrollBarAlwaysOff)
        self.scrollArea.setVerticalScrollBarPolicy(qt.Qt.ScrollBarAsNeeded)
        scrollContents = qt.QWidget()
        bodyLayout = qt.QVBoxLayout(scrollContents)
        # Reserve room on the right edge for the vertical scrollbar so input
        # widgets do not get clipped by it. The exact value is the platform's
        # scrollbar metric; 18px works on every Qt style we ship.
        bodyLayout.setContentsMargins(0, 0, 18, 0)

        # Allowed Roles (collapsible)
        self.allowed_roles_widget = ListTableWidget(object_name="Role", object_type=str)
        self._allowed_roles_collapsible = ctk.ctkCollapsibleButton()
        self._allowed_roles_collapsible.text = "Allowed Roles"
        self._allowed_roles_collapsible.collapsed = True
        _ar_layout = qt.QVBoxLayout(self._allowed_roles_collapsible)
        _ar_layout.addWidget(self.allowed_roles_widget)
        bodyLayout.addWidget(self._allowed_roles_collapsible)

        # Pulse / Sequence / Focal Pattern / Sim Setup / Delay / Apod / Seg
        # All of these provide their own collapsible / labeled headers.
        # ``_summary_sections`` collects (collapsible_button, form_widget,
        # base_title) so we can append a live one-liner summary (from each
        # underlying openlifu class's ``get_summary()``) to the section header
        # whenever the user edits a field.
        self._summary_sections: list[tuple[Any, Any, str]] = []

        self.pulse_definition_widget = OpenLIFUAbstractDataclassDefinitionFormWidget(
            cls=openlifu.bf.Pulse, collapsible_title="Parameters for Pulse")
        self.pulse_definition_widget.layout().setContentsMargins(0, 0, 0, 0)
        bodyLayout.addWidget(self.pulse_definition_widget)
        self.pulse_definition_widget.collapsible.collapsed = True
        self._summary_sections.append((self.pulse_definition_widget.collapsible, self.pulse_definition_widget, "Pulse"))

        self.sequence_definition_widget = OpenLIFUAbstractDataclassDefinitionFormWidget(
            cls=openlifu.bf.Sequence, collapsible_title="Parameters for Sequence")
        self.sequence_definition_widget.layout().setContentsMargins(0, 0, 0, 0)
        bodyLayout.addWidget(self.sequence_definition_widget)
        self.sequence_definition_widget.collapsible.collapsed = True
        self._summary_sections.append((self.sequence_definition_widget.collapsible, self.sequence_definition_widget, "Sequence"))

        self.abstract_focal_pattern_definition_widget = OpenLIFUAbstractMultipleABCDefinitionFormWidget(
            [openlifu.bf.Wheel, openlifu.bf.SinglePoint],
            is_collapsible=False, collapsible_title="Focal Pattern", custom_abc_title="Focal Pattern",
        )
        _fp_collapsible = ctk.ctkCollapsibleButton()
        _fp_collapsible.text = "Focal Pattern"
        _fp_collapsible.collapsed = True
        _fp_layout = qt.QVBoxLayout(_fp_collapsible)
        _fp_layout.addWidget(self.abstract_focal_pattern_definition_widget)
        bodyLayout.addWidget(_fp_collapsible)
        self._summary_sections.append((_fp_collapsible, self.abstract_focal_pattern_definition_widget, "Focal Pattern"))

        self.sim_setup_definition_widget = OpenLIFUSimSetupDefinitionFormWidget()
        self.sim_setup_definition_widget.layout().setContentsMargins(0, 0, 0, 0)
        bodyLayout.addWidget(self.sim_setup_definition_widget)
        self.sim_setup_definition_widget.collapsible.collapsed = True
        self._summary_sections.append((self.sim_setup_definition_widget.collapsible, self.sim_setup_definition_widget, "Sim Setup"))

        self.abstract_delay_method_definition_widget = OpenLIFUAbstractDelayMethodDefinitionFormWidget()
        _delay_collapsible = ctk.ctkCollapsibleButton()
        _delay_collapsible.text = "Delay Method"
        _delay_collapsible.collapsed = True
        _delay_layout = qt.QVBoxLayout(_delay_collapsible)
        _delay_layout.addWidget(self.abstract_delay_method_definition_widget)
        bodyLayout.addWidget(_delay_collapsible)
        self._summary_sections.append((_delay_collapsible, self.abstract_delay_method_definition_widget, "Delay Method"))

        self.abstract_apodization_method_definition_widget = OpenLIFUAbstractApodizationMethodDefinitionFormWidget()
        _apod_collapsible = ctk.ctkCollapsibleButton()
        _apod_collapsible.text = "Apodization Method"
        _apod_collapsible.collapsed = True
        _apod_layout = qt.QVBoxLayout(_apod_collapsible)
        _apod_layout.addWidget(self.abstract_apodization_method_definition_widget)
        bodyLayout.addWidget(_apod_collapsible)
        self._summary_sections.append((_apod_collapsible, self.abstract_apodization_method_definition_widget, "Apodization Method"))

        self.abstract_segmentation_method_definition_widget = OpenLIFUAbstractSegmentationMethodDefinitionFormWidget()
        _seg_collapsible = ctk.ctkCollapsibleButton()
        _seg_collapsible.text = "Segmentation Method"
        _seg_collapsible.collapsed = True
        _seg_layout = qt.QVBoxLayout(_seg_collapsible)
        _seg_layout.addWidget(self.abstract_segmentation_method_definition_widget)
        bodyLayout.addWidget(_seg_collapsible)
        self._summary_sections.append((_seg_collapsible, self.abstract_segmentation_method_definition_widget, "Segmentation Method"))

        # Parameter Constraints (collapsible)
        self.parameter_constraints_widget = OpenLIFUParameterConstraintsWidget()
        self._param_constraints_collapsible = ctk.ctkCollapsibleButton()
        self._param_constraints_collapsible.text = "Parameter Constraints"
        self._param_constraints_collapsible.collapsed = True
        _pc_layout = qt.QVBoxLayout(self._param_constraints_collapsible)
        _pc_layout.addWidget(self.parameter_constraints_widget)
        bodyLayout.addWidget(self._param_constraints_collapsible)

        # Target Constraints (collapsible)
        self.target_constraints_widget = ListTableWidget(
            object_name="Target Constraint", object_type=openlifu.plan.TargetConstraints)
        self._target_constraints_collapsible = ctk.ctkCollapsibleButton()
        self._target_constraints_collapsible.text = "Target Constraints"
        self._target_constraints_collapsible.collapsed = True
        _tc_layout = qt.QVBoxLayout(self._target_constraints_collapsible)
        _tc_layout.addWidget(self.target_constraints_widget)
        bodyLayout.addWidget(self._target_constraints_collapsible)

        # Solution Analysis Options
        self.solution_analysis_options_definition_widget = OpenLIFUSolutionAnalysisOptionsDefinitionFormWidget()
        self.solution_analysis_options_definition_widget.layout().setContentsMargins(0, 0, 0, 0)
        bodyLayout.addWidget(self.solution_analysis_options_definition_widget)
        self.solution_analysis_options_definition_widget.collapsible.collapsed = True
        self._summary_sections.append((
            self.solution_analysis_options_definition_widget.collapsible,
            self.solution_analysis_options_definition_widget,
            "Solution Analysis Options",
        ))

        # Virtual Fit Options
        self.virtual_fit_options_definition_widget = OpenLIFUAbstractDataclassDefinitionFormWidget(
            cls=openlifu.seg.virtual_fit.VirtualFitOptions, collapsible_title="Virtual Fit Options")
        self.virtual_fit_options_definition_widget.layout().setContentsMargins(0, 0, 0, 0)
        bodyLayout.addWidget(self.virtual_fit_options_definition_widget)
        self.virtual_fit_options_definition_widget.collapsible.collapsed = True
        self._summary_sections.append((
            self.virtual_fit_options_definition_widget.collapsible,
            self.virtual_fit_options_definition_widget,
            "Virtual Fit Options",
        ))

        bodyLayout.addStretch(1)
        self.scrollArea.setWidget(scrollContents)
        outer.addWidget(self.scrollArea, 1)

        # --- Footer ---
        self.validityLabel = qt.QLabel("")
        self.validityLabel.setAlignment(qt.Qt.AlignCenter)
        self.validityLabel.setWordWrap(True)
        outer.addWidget(self.validityLabel)

        self.buttonBox = qt.QDialogButtonBox()
        self.saveButton = self.buttonBox.addButton("Save", qt.QDialogButtonBox.AcceptRole)
        self.cancelButton = self.buttonBox.addButton("Cancel", qt.QDialogButtonBox.RejectRole)
        self.buttonBox.accepted.connect(self._on_save_clicked)
        self.buttonBox.rejected.connect(self.reject)
        outer.addWidget(self.buttonBox)

        # Wire validity-indicator updates to all editor signals.
        self.nameLineEdit.textChanged.connect(self._update_validity_indicator)
        self.idLineEdit.textChanged.connect(self._update_validity_indicator)
        self.descriptionTextEdit.textChanged.connect(self._update_validity_indicator)
        self.allowed_roles_widget.table.itemChanged.connect(lambda *_a, **_kw: self._update_validity_indicator())
        for w in (
            self.pulse_definition_widget, self.sequence_definition_widget,
            self.abstract_focal_pattern_definition_widget, self.sim_setup_definition_widget,
            self.abstract_delay_method_definition_widget, self.abstract_apodization_method_definition_widget,
            self.abstract_segmentation_method_definition_widget,
            self.solution_analysis_options_definition_widget, self.virtual_fit_options_definition_widget,
        ):
            w.add_value_changed_signals(lambda *_a, **_kw: self._update_validity_indicator())
            w.add_value_changed_signals(lambda *_a, **_kw: self._refresh_section_summaries())
        self.parameter_constraints_widget.table.itemChanged.connect(lambda *_a, **_kw: self._update_validity_indicator())
        self.target_constraints_widget.table.itemChanged.connect(lambda *_a, **_kw: self._update_validity_indicator())

        screen = qt.QDesktopWidget().screenGeometry()
        # Shrink the dialog width: the form needs roughly half the screen, not
        # the previous 55%. Height stays generous so users can see most of the
        # collapsible sections at once.
        self.resize(int(screen.width() * 0.4), int(screen.height() * 0.75))

    # ---- I/O between Protocol object and form ----
    def _populate_ui_from_protocol(self, protocol: "openlifu.plan.Protocol") -> None:
        self.nameLineEdit.setText(protocol.name)
        self.idLineEdit.setText(protocol.id)
        self.descriptionTextEdit.setPlainText(protocol.description)
        self.allowed_roles_widget.from_list(protocol.allowed_roles)
        self.pulse_definition_widget.update_form_from_class(protocol.pulse)
        self.sequence_definition_widget.update_form_from_class(protocol.sequence)
        self.abstract_focal_pattern_definition_widget.update_form_from_class(protocol.focal_pattern)
        self.sim_setup_definition_widget.update_form_from_class(protocol.sim_setup)
        self.abstract_delay_method_definition_widget.update_form_from_class(protocol.delay_method)
        self.abstract_apodization_method_definition_widget.update_form_from_class(protocol.apod_method)
        self.abstract_segmentation_method_definition_widget.update_form_from_class(protocol.seg_method)
        self.parameter_constraints_widget.from_dict(protocol.param_constraints)
        self.target_constraints_widget.from_list(protocol.target_constraints)
        self.solution_analysis_options_definition_widget.update_form_from_class(protocol.analysis_options)
        self.virtual_fit_options_definition_widget.update_form_from_class(protocol.virtual_fit_options)

    def _build_protocol_from_ui(self, post_init: bool = True) -> "openlifu.plan.Protocol":
        import openlifu.plan
        fields = dict(
            name=self.nameLineEdit.text,
            id=self.idLineEdit.text,
            description=self.descriptionTextEdit.toPlainText(),
            allowed_roles=self.allowed_roles_widget.to_list(),
            pulse=self.pulse_definition_widget.get_form_as_class(post_init=post_init),
            sequence=self.sequence_definition_widget.get_form_as_class(post_init=post_init),
            focal_pattern=self.abstract_focal_pattern_definition_widget.get_form_as_class(post_init=post_init),
            sim_setup=self.sim_setup_definition_widget.get_form_as_class(post_init=post_init),
            delay_method=self.abstract_delay_method_definition_widget.get_form_as_class(post_init=post_init),
            apod_method=self.abstract_apodization_method_definition_widget.get_form_as_class(post_init=post_init),
            seg_method=self.abstract_segmentation_method_definition_widget.get_form_as_class(post_init=post_init),
            param_constraints=self.parameter_constraints_widget.to_dict(),
            target_constraints=self.target_constraints_widget.to_list(),
            analysis_options=self.solution_analysis_options_definition_widget.get_form_as_class(post_init=post_init),
            virtual_fit_options=self.virtual_fit_options_definition_widget.get_form_as_class(post_init=post_init),
        )
        if post_init:
            return openlifu.plan.Protocol(**fields)
        return instantiate_without_post_init(openlifu.plan.Protocol, **fields)

    def _update_validity_indicator(self) -> None:
        try:
            self._build_protocol_from_ui(post_init=True)
        except Exception as e:
            self.validityLabel.setText(f"Protocol is invalid: {e}")
            self.validityLabel.setStyleSheet("color: red; border: 1px solid red; padding: 3px;")
            self.saveButton.setEnabled(False)
        else:
            self.validityLabel.setText("")
            self.validityLabel.setStyleSheet("border: none;")
            self.saveButton.setEnabled(True)

    def _refresh_section_summaries(self) -> None:
        """Update each section's collapsible header with a live one-liner
        derived from the underlying openlifu class's ``get_summary()``.

        Failures (e.g. invalid intermediate state during editing) are silently
        ignored: the section header is reset to its base title, and the
        validity indicator already surfaces the underlying validation error.
        """
        for collapsible, form_widget, base_title in getattr(self, "_summary_sections", []):
            try:
                instance = form_widget.get_form_as_class(post_init=False)
                summary = instance.get_summary() if hasattr(instance, "get_summary") else ""
            except Exception:
                summary = ""
            collapsible.text = f"{base_title} - {summary}" if summary else base_title

    # ---- Save handler ----
    @display_errors
    def _on_save_clicked(self, *_a, **_kw) -> None:
        try:
            entered = self._build_protocol_from_ui(post_init=True)
        except Exception as e:
            slicer.util.errorDisplay(f"Could not save the protocol due to the following reason:\n{e}", parent=self)
            return
        if entered.id == "":
            slicer.util.errorDisplay("You cannot save a protocol without entering in a Protocol ID.", parent=self)
            return
        if self._db is None:
            slicer.util.errorDisplay("Cannot save protocol because there is no database connection.", parent=self)
            return
        # Overwrite check: only prompt when the saved id collides with an
        # *existing* protocol in the database whose id differs from the one we
        # started editing.
        try:
            existing_ids = set(self._db.get_protocol_ids() or [])
        except Exception:
            existing_ids = set()
        if entered.id in existing_ids and entered.id != self._initial_protocol.id:
            if not slicer.util.confirmYesNoDisplay(
                text="A protocol with this ID already exists in the database. Overwrite it?",
                windowTitle="Overwrite Confirmation",
            ):
                return
        try:
            import openlifu.db.database
            self._db.write_protocol(entered, openlifu.db.database.OnConflictOpts.OVERWRITE)
        except Exception as e:
            slicer.util.errorDisplay(f"Failed to write protocol to database: {e}", parent=self)
            return
        self._saved_protocol = entered
        self.accept()


# ---- Preview dialog ----

class ProtocolPreviewDialog(qt.QDialog):
    """Read-only QTreeWidget JSON view of a protocol with action buttons for
    Duplicate / Edit / Export / Close.

    When ``manager`` is provided, the action buttons delegate back to it so
    that any changes made via Edit/Duplicate are reflected in the manager's
    table; the preview dialog closes itself before invoking the action.
    """

    def __init__(
        self,
        protocol: "openlifu.plan.Protocol",
        manager: Optional["ProtocolManagerDialog"] = None,
        parent="mainWindow",
    ):
        super().__init__(slicer.util.mainWindow() if parent == "mainWindow" else parent)
        self.setWindowTitle(f"Preview/Edit: {protocol.name} (ID: {protocol.id})")
        self.setWindowModality(qt.Qt.WindowModal)
        self.setWindowFlags(self.windowFlags() & ~qt.Qt.WindowContextHelpButtonHint)
        self._protocol = protocol
        self._manager = manager
        self._setup()

    def _setup(self) -> None:
        layout = qt.QVBoxLayout()
        self.setLayout(layout)

        self.tree = qt.QTreeWidget()
        self.tree.setColumnCount(2)
        self.tree.setHeaderLabels(["Key", "Value"])
        self.tree.setAlternatingRowColors(True)
        self.tree.header().setSectionResizeMode(0, qt.QHeaderView.ResizeToContents)
        self.tree.header().setStretchLastSection(True)
        layout.addWidget(self.tree, 1)

        try:
            data = self._protocol.to_dict()
        except Exception as e:
            data = {"error": f"Could not serialize protocol: {e}"}

        root = qt.QTreeWidgetItem(self.tree, [f"{self._protocol.id}", ""])
        self._populate_tree_node(root, data)
        root.setExpanded(True)

        bb = qt.QDialogButtonBox()
        self.duplicateButton = bb.addButton("Duplicate", qt.QDialogButtonBox.ActionRole)
        self.duplicateButton.setToolTip(
            "Open the editor on a copy of this protocol (id suffixed with '_copy')"
        )
        self.editButton = bb.addButton("Edit", qt.QDialogButtonBox.ActionRole)
        self.editButton.setToolTip("Open this protocol in the editor")
        self.exportButton = bb.addButton("Export", qt.QDialogButtonBox.ActionRole)
        self.exportButton.setToolTip("Save this protocol to a JSON file on disk")
        self.closeButton = bb.addButton("Close", qt.QDialogButtonBox.RejectRole)
        self.closeButton.setToolTip("Close the preview without making changes")
        bb.rejected.connect(self.reject)
        self.duplicateButton.clicked.connect(self._on_duplicate)
        self.editButton.clicked.connect(self._on_edit)
        self.exportButton.clicked.connect(self._on_export)
        layout.addWidget(bb)

        # If we are not embedded in a manager, hide actions that require one.
        if self._manager is None:
            self.duplicateButton.setVisible(False)
            self.editButton.setVisible(False)

        screen = qt.QDesktopWidget().screenGeometry()
        self.resize(int(screen.width() * 0.4), int(screen.height() * 0.5))

    @display_errors
    def _on_edit(self, *_a, **_kw) -> None:
        if self._manager is None:
            return
        self.accept()
        self._manager._open_editor_and_propagate(self._protocol)

    @display_errors
    def _on_duplicate(self, *_a, **_kw) -> None:
        if self._manager is None:
            return
        self.accept()
        copy = self._protocol
        copy.id = f"{copy.id}_copy"
        copy.name = f"{copy.name} (Copy)"
        self._manager._open_editor_and_propagate(copy)

    @display_errors
    def _on_export(self, *_a, **_kw) -> None:
        ProtocolManagerDialog._export_protocol_to_file(self._protocol, parent=self)

    @classmethod
    def _populate_tree_node(cls, parent_item: qt.QTreeWidgetItem, value: Any) -> None:
        if isinstance(value, dict):
            for k, v in value.items():
                child = qt.QTreeWidgetItem(parent_item, [str(k), "" if isinstance(v, (dict, list)) else cls._format_scalar(v)])
                if isinstance(v, (dict, list)):
                    cls._populate_tree_node(child, v)
        elif isinstance(value, list):
            for i, v in enumerate(value):
                child = qt.QTreeWidgetItem(parent_item, [f"[{i}]", "" if isinstance(v, (dict, list)) else cls._format_scalar(v)])
                if isinstance(v, (dict, list)):
                    cls._populate_tree_node(child, v)

    @staticmethod
    def _format_scalar(v: Any) -> str:
        try:
            return json.dumps(v, default=str)
        except Exception:
            return str(v)


# ---- Manager dialog ----

class ProtocolManagerDialog(qt.QDialog):
    """Tabular manager for protocols stored in the loaded database.

    Columns: ``[ID, Name, Roles, Description]``. Top-level actions:

      * **New** -- create a blank protocol and open the editor.
      * **Import** -- load a protocol JSON file from disk into the database.
      * **Preview/Edit** -- open the preview window for the selected protocol;
        the preview window itself offers Edit / Duplicate / Export actions.
      * **Delete** -- remove the selected protocol from the database.
    """

    def __init__(self, db: "openlifu.db.Database", parent="mainWindow"):
        super().__init__(slicer.util.mainWindow() if parent == "mainWindow" else parent)
        self.setWindowTitle("Manage Protocols")
        self.setWindowModality(qt.Qt.WindowModal)
        self.setWindowFlags(self.windowFlags() & ~qt.Qt.WindowContextHelpButtonHint)
        self.db = db
        self._setup()
        self.refresh()

    # ---- UI ----
    def _setup(self) -> None:
        layout = qt.QVBoxLayout()
        self.setLayout(layout)

        cols = ["ID", "Name", "Roles", "Description"]
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
        header.setSectionResizeMode(0, qt.QHeaderView.ResizeToContents)
        header.setSectionResizeMode(1, qt.QHeaderView.ResizeToContents)
        header.setSectionResizeMode(2, qt.QHeaderView.ResizeToContents)
        header.setSectionResizeMode(3, qt.QHeaderView.Stretch)
        self.table.setSortingEnabled(True)
        layout.addWidget(self.table)

        actionRow = qt.QHBoxLayout()
        self.newButton = qt.QPushButton("New")
        self.newButton.setToolTip("Create a new blank protocol and open the editor")
        self.importButton = qt.QPushButton("Import")
        self.importButton.setToolTip("Import a protocol definition from a JSON file on disk")
        self.previewEditButton = qt.QPushButton("Preview/Edit")
        self._tooltip_preview_enabled = (
            "Open a preview of the selected protocol; from there you can edit, duplicate, or export it"
        )
        self._tooltip_disabled = "No Protocol Selected"
        self.previewEditButton.setToolTip(self._tooltip_preview_enabled)
        self.deleteButton = qt.QPushButton("Delete")
        self._tooltip_delete_enabled = "Remove the selected protocol from the database"
        self.deleteButton.setToolTip(self._tooltip_delete_enabled)
        for b in (self.newButton, self.importButton, self.previewEditButton, self.deleteButton):
            b.setSizePolicy(qt.QSizePolicy.Expanding, qt.QSizePolicy.Preferred)
            actionRow.addWidget(b)
        layout.addLayout(actionRow)

        bb = qt.QDialogButtonBox()
        closeButton = bb.addButton("Close", qt.QDialogButtonBox.RejectRole)
        closeButton.setToolTip("Close the protocol manager")
        bb.rejected.connect(self.reject)
        layout.addWidget(bb)

        self.newButton.clicked.connect(self.onNew)
        self.importButton.clicked.connect(self.onImport)
        self.previewEditButton.clicked.connect(self.onPreviewEdit)
        self.deleteButton.clicked.connect(self.onDelete)
        self.table.doubleClicked.connect(self.onPreviewEdit)
        self.table.itemSelectionChanged.connect(self._update_action_buttons_state)

        self._update_action_buttons_state()

        screen = qt.QDesktopWidget().screenGeometry()
        self.resize(int(screen.width() * 0.5), int(screen.height() * 0.4))

    def _update_action_buttons_state(self) -> None:
        """Enable/disable selection-dependent buttons and update their tooltips."""
        has_selection = self._selected_protocol_id() is not None
        self.previewEditButton.setEnabled(has_selection)
        self.deleteButton.setEnabled(has_selection)
        self.previewEditButton.setToolTip(
            self._tooltip_preview_enabled if has_selection else self._tooltip_disabled
        )
        self.deleteButton.setToolTip(
            self._tooltip_delete_enabled if has_selection else self._tooltip_disabled
        )

    # ---- Population ----
    def refresh(self) -> None:
        self.table.setSortingEnabled(False)
        self.table.clearContents()
        try:
            ids = list(self.db.get_protocol_ids() or [])
        except Exception as e:
            logging.warning("Could not list protocol ids: %s", e)
            ids = []

        self.table.setRowCount(len(ids))
        for row, pid in enumerate(ids):
            try:
                p = self.db.load_protocol(pid)
                name = getattr(p, "name", pid)
                roles = ", ".join(getattr(p, "allowed_roles", []) or [])
                description = (getattr(p, "description", "") or "").replace("\n", " ")
            except Exception as e:
                logging.warning("Could not load protocol %s for listing: %s", pid, e)
                name, roles, description = pid, "", ""

            self.table.setItem(row, 0, qt.QTableWidgetItem(str(pid)))
            self.table.setItem(row, 1, qt.QTableWidgetItem(str(name)))
            self.table.setItem(row, 2, qt.QTableWidgetItem(roles))
            self.table.setItem(row, 3, qt.QTableWidgetItem(description))
        self.table.resizeRowsToContents()
        self.table.setSortingEnabled(True)
        self._update_action_buttons_state()

    def _selected_protocol_id(self) -> Optional[str]:
        items = self.table.selectedItems()
        if not items:
            return None
        idItem = self.table.item(items[0].row(), 0)
        return idItem.text() if idItem else None

    def _load_selected_protocol(self) -> Optional["openlifu.plan.Protocol"]:
        pid = self._selected_protocol_id()
        if pid is None:
            return None
        try:
            return self.db.load_protocol(pid)
        except Exception as e:
            slicer.util.errorDisplay(f"Failed to load protocol {pid}: {e}", parent=self)
            return None

    # ---- Actions ----
    def _open_editor_and_propagate(self, protocol: "openlifu.plan.Protocol") -> None:
        """Open the editor for ``protocol``; on Save, refresh the table and
        propagate the new/updated protocol into the data parameter node so the
        rest of the app sees the change."""
        dlg = ProtocolEditDialog(protocol=protocol, db=self.db, parent=self)
        if dlg.exec_() != qt.QDialog.Accepted:
            return
        saved = dlg.get_saved_protocol()
        if saved is None:
            return
        # Reflect the saved protocol in the in-memory loaded set so other
        # modules (e.g. SonicationPlanner) pick up the new/updated definition.
        try:
            data_logic = slicer.util.getModuleLogic("OpenLIFUData")
            data_logic.load_protocol_from_openlifu(saved, replace_confirmed=True)
        except Exception as e:
            logging.warning("Could not refresh in-memory protocol after save: %s", e)
        self.refresh()

    @display_errors
    def onNew(self, checked: bool = False) -> None:
        protocol = _build_default_new_protocol()
        protocol.id = _generate_unique_protocol_id(self.db)
        self._open_editor_and_propagate(protocol)

    @display_errors
    def onImport(self, checked: bool = False) -> None:
        qsettings = qt.QSettings()
        filepath: str = qt.QFileDialog.getOpenFileName(
            slicer.util.mainWindow(),
            "Import protocol",
            qsettings.value("OpenLIFU/databaseDirectory", "."),
            "Protocols (*.json);;All Files (*)",
        )
        if not filepath:
            return
        try:
            import openlifu.plan
            protocol = openlifu.plan.Protocol.from_file(filepath)
        except Exception as e:
            slicer.util.errorDisplay(f"Failed to read protocol file: {e}", parent=self)
            return
        # Confirm overwrite if collision.
        try:
            existing_ids = set(self.db.get_protocol_ids() or [])
        except Exception:
            existing_ids = set()
        if protocol.id in existing_ids:
            if not slicer.util.confirmYesNoDisplay(
                text=f'A protocol with id "{protocol.id}" already exists in the database. Overwrite it?',
                windowTitle="Overwrite Confirmation",
            ):
                return
        try:
            import openlifu.db.database
            self.db.write_protocol(protocol, openlifu.db.database.OnConflictOpts.OVERWRITE)
        except Exception as e:
            slicer.util.errorDisplay(f"Failed to write protocol to database: {e}", parent=self)
            return
        try:
            data_logic = slicer.util.getModuleLogic("OpenLIFUData")
            data_logic.load_protocol_from_openlifu(protocol, replace_confirmed=True)
        except Exception as e:
            logging.warning("Could not refresh in-memory protocol after import: %s", e)
        self.refresh()

    @display_errors
    def onDuplicate(self, checked: bool = False) -> None:
        protocol = self._load_selected_protocol()
        if protocol is None:
            return
        protocol.id = f"{protocol.id}_copy"
        protocol.name = f"{protocol.name} (Copy)"
        self._open_editor_and_propagate(protocol)

    @display_errors
    def onEdit(self, *_a, **_kw) -> None:
        protocol = self._load_selected_protocol()
        if protocol is None:
            return
        self._open_editor_and_propagate(protocol)

    @display_errors
    def onPreviewEdit(self, *_a, **_kw) -> None:
        protocol = self._load_selected_protocol()
        if protocol is None:
            return
        ProtocolPreviewDialog(protocol, manager=self, parent=self).exec_()

    @display_errors
    def onExport(self, checked: bool = False) -> None:
        protocol = self._load_selected_protocol()
        if protocol is None:
            return
        self._export_protocol_to_file(protocol, parent=self)

    @staticmethod
    def _export_protocol_to_file(protocol: "openlifu.plan.Protocol", parent: qt.QWidget) -> None:
        """Prompt the user for a save path and write ``protocol`` to disk."""
        safe_id = "".join(c if c.isalnum() or c in (' ', '-', '_') else "_" for c in protocol.id)
        initial_file = str(Path(slicer.app.defaultScenePath) / f"{safe_id}.json")
        filepath = qt.QFileDialog.getSaveFileName(
            slicer.util.mainWindow(),
            "Export Protocol",
            initial_file,
            "Protocols (*.json);;All Files (*)",
        )
        if not filepath:
            return
        try:
            protocol.to_file(filepath)
        except Exception as e:
            slicer.util.errorDisplay(f"Failed to write protocol file: {e}", parent=parent)
            return

    @display_errors
    def onDelete(self, checked: bool = False) -> None:
        pid = self._selected_protocol_id()
        if pid is None:
            return
        if not slicer.util.confirmYesNoDisplay(
            text=f'Are you sure you want to delete the protocol "{pid}"?',
            windowTitle="Protocol Delete Confirmation",
        ):
            return
        try:
            import openlifu.db.database
            self.db.delete_protocol(pid, openlifu.db.database.OnConflictOpts.ERROR)
        except Exception as e:
            slicer.util.errorDisplay(f"Failed to delete protocol from database: {e}", parent=self)
            return
        # Also drop from the in-memory loaded set if present.
        try:
            loaded = get_openlifu_data_parameter_node().loaded_protocols
            if pid in loaded:
                loaded.pop(pid)
        except Exception as e:
            logging.warning("Could not unload deleted protocol %s: %s", pid, e)
        self.refresh()


# ---- Per-session: Solution / Photoscan / Run manager dialogs ----


class _JsonTreeDialog(qt.QDialog):
    """Minimal read-only JSON tree viewer used by the per-session manager dialogs."""

    def __init__(self, title: str, data: Dict[str, Any], parent="mainWindow"):
        super().__init__(slicer.util.mainWindow() if parent == "mainWindow" else parent)
        self.setWindowTitle(title)
        self.setWindowModality(qt.Qt.WindowModal)
        self.setWindowFlags(self.windowFlags() & ~qt.Qt.WindowContextHelpButtonHint)

        layout = qt.QVBoxLayout()
        self.setLayout(layout)

        tree = qt.QTreeWidget()
        tree.setColumnCount(2)
        tree.setHeaderLabels(["Key", "Value"])
        tree.setAlternatingRowColors(True)
        tree.header().setSectionResizeMode(0, qt.QHeaderView.ResizeToContents)
        tree.header().setStretchLastSection(True)
        layout.addWidget(tree, 1)

        for top_key, top_val in data.items():
            root = qt.QTreeWidgetItem(tree, [str(top_key), ""])
            ProtocolPreviewDialog._populate_tree_node(root, top_val)
            root.setExpanded(True)

        bb = qt.QDialogButtonBox()
        bb.addButton("Close", qt.QDialogButtonBox.RejectRole)
        bb.rejected.connect(self.reject)
        layout.addWidget(bb)

        screen = qt.QDesktopWidget().screenGeometry()
        self.resize(int(screen.width() * 0.4), int(screen.height() * 0.5))


def _make_led_icon(color: str) -> qt.QIcon:
    """Module-level helper mirroring TransducerManagerDialog._make_led_icon."""
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


class SolutionManagerDialog(qt.QDialog):
    """Tabular manager for solutions stored under the active session.

    Columns: ``[LED, ID, Name, Approved, Foci]``. The LED on a row is lit
    green when the row's ID matches the currently loaded solution. Actions:

    * **Add from File** -- load a ``<id>.json`` solution file (the matching
      ``<id>.nc`` next to it is loaded automatically) and write it into the
      active session's solutions tree.
    * **Preview** -- read-only JSON tree of the solution metadata.
    * **Export** -- copy ``<id>.json`` and ``<id>.nc`` to a folder of the
      user's choosing.
    * **Delete** -- remove the solution directory and drop the id from the
      session's ``solutions.json`` index.
    """

    def __init__(
        self,
        db: "openlifu.db.Database",
        subject_id: str,
        session_id: str,
        parent="mainWindow",
    ):
        super().__init__(slicer.util.mainWindow() if parent == "mainWindow" else parent)
        self.setWindowTitle(f"Manage Solutions - session {session_id}")
        self.setWindowModality(qt.Qt.WindowModal)
        self.db = db
        self.subject_id = subject_id
        self.session_id = session_id
        self._setup()
        self.refresh()

    def _setup(self) -> None:
        layout = qt.QVBoxLayout()
        self.setLayout(layout)

        cols = ["", "ID", "Name", "Approved", "Foci"]
        self.table = qt.QTableWidget(self)
        self.table.setColumnCount(len(cols))
        self.table.setHorizontalHeaderLabels(cols)
        self.table.setSelectionBehavior(qt.QAbstractItemView.SelectRows)
        self.table.setSelectionMode(qt.QAbstractItemView.SingleSelection)
        self.table.setEditTriggers(qt.QAbstractItemView.NoEditTriggers)
        self.table.verticalHeader().setVisible(False)
        self.table.setShowGrid(False)
        self.table.horizontalHeader().setHighlightSections(False)
        header = self.table.horizontalHeader()
        header.setSectionResizeMode(0, qt.QHeaderView.Fixed)
        self.table.setColumnWidth(0, 22)
        header.setSectionResizeMode(1, qt.QHeaderView.ResizeToContents)
        header.setSectionResizeMode(2, qt.QHeaderView.ResizeToContents)
        header.setSectionResizeMode(3, qt.QHeaderView.ResizeToContents)
        header.setSectionResizeMode(4, qt.QHeaderView.Stretch)
        self.table.setSortingEnabled(True)
        layout.addWidget(self.table)

        actionRow = qt.QHBoxLayout()
        self.addFileButton = qt.QPushButton("Add from File")
        self.addFileButton.setToolTip("Import a solution JSON (and its companion .nc) into this session")
        self.previewButton = qt.QPushButton("Preview")
        self.previewButton.setToolTip("Show a read-only JSON view of the selected solution")
        self.exportButton = qt.QPushButton("Export")
        self.exportButton.setToolTip("Copy the selected solution's JSON and .nc files to a folder of your choice")
        self.deleteButton = qt.QPushButton("Delete")
        self.deleteButton.setToolTip("Remove the selected solution from this session")
        for b in (self.addFileButton, self.previewButton, self.exportButton, self.deleteButton):
            b.setSizePolicy(qt.QSizePolicy.Expanding, qt.QSizePolicy.Preferred)
            actionRow.addWidget(b)
        layout.addLayout(actionRow)

        bb = qt.QDialogButtonBox()
        bb.addButton("Close", qt.QDialogButtonBox.RejectRole)
        bb.rejected.connect(self.reject)
        layout.addWidget(bb)

        self.addFileButton.clicked.connect(self.onAddFromFile)
        self.previewButton.clicked.connect(self.onPreview)
        self.exportButton.clicked.connect(self.onExport)
        self.deleteButton.clicked.connect(self.onDelete)
        self.table.doubleClicked.connect(self.onPreview)

        screen = qt.QDesktopWidget().screenGeometry()
        self.resize(int(screen.width() * 0.5), int(screen.height() * 0.4))

    def _loaded_solution_id(self) -> Optional[str]:
        try:
            sol = get_openlifu_data_parameter_node().loaded_solution
            if sol is None:
                return None
            return sol.solution.solution.id
        except Exception:
            return None

    def _load_solution_metadata(self, sid: str) -> Optional["openlifu.plan.Solution"]:
        """Load just the solution metadata (no .nc) for listing/preview."""
        try:
            import openlifu.plan
            json_filepath = self.db.get_solution_filepath(self.subject_id, self.session_id, sid)
            return openlifu.plan.Solution.from_files(json_filepath)
        except Exception as e:
            logging.warning("Could not read solution %s for listing: %s", sid, e)
            return None

    def refresh(self) -> None:
        self.table.setSortingEnabled(False)
        self.table.clearContents()
        try:
            ids = list(self.db.get_solution_ids(self.subject_id, self.session_id) or [])
        except Exception as e:
            logging.warning("Could not list solution ids: %s", e)
            ids = []
        loaded_id = self._loaded_solution_id()
        on_icon = _make_led_icon("#2ecc71")
        off_icon = _make_led_icon("#444")

        self.table.setRowCount(len(ids))
        for row, sid in enumerate(ids):
            obj = self._load_solution_metadata(sid)
            if obj is not None:
                name = getattr(obj, "name", sid)
                approved = "yes" if bool(getattr(obj, "approved", False)) else "no"
                try:
                    foci = len(obj.foci) if getattr(obj, "foci", None) is not None else 0
                except Exception:
                    foci = 0
            else:
                name = sid
                approved = "?"
                foci = 0

            led_item = qt.QTableWidgetItem()
            is_loaded = loaded_id is not None and sid == loaded_id
            led_item.setIcon(on_icon if is_loaded else off_icon)
            led_item.setFlags(qt.Qt.ItemIsEnabled | qt.Qt.ItemIsSelectable)
            led_item.setToolTip("Currently loaded" if is_loaded else "")
            self.table.setItem(row, 0, led_item)
            self.table.setItem(row, 1, qt.QTableWidgetItem(str(sid)))
            self.table.setItem(row, 2, qt.QTableWidgetItem(str(name)))
            self.table.setItem(row, 3, qt.QTableWidgetItem(approved))
            self.table.setItem(row, 4, qt.QTableWidgetItem(str(foci)))

        self.table.resizeRowsToContents()
        self.table.setSortingEnabled(True)

    def _selected_solution_id(self) -> Optional[str]:
        items = self.table.selectedItems()
        if not items:
            return None
        row = items[0].row()
        idItem = self.table.item(row, 1)
        return idItem.text() if idItem else None

    @display_errors
    def onAddFromFile(self, checked: bool = False) -> None:
        qsettings = qt.QSettings()
        filepath: str = qt.QFileDialog.getOpenFileName(
            slicer.util.mainWindow(),
            "Import solution",
            qsettings.value("OpenLIFU/databaseDirectory", "."),
            "Solutions (*.json);;All Files (*)",
        )
        if not filepath:
            return
        json_path = Path(filepath)
        nc_path = json_path.with_suffix(".nc")
        if not nc_path.exists():
            slicer.util.errorDisplay(
                f"Expected a companion netCDF file at:\n\n{nc_path}\n\nbut it does not exist.",
                parent=self,
            )
            return
        try:
            import openlifu.plan
            solution = openlifu.plan.Solution.from_files(str(json_path), str(nc_path))
        except Exception as e:
            slicer.util.errorDisplay(f"Failed to read solution file: {e}", parent=self)
            return
        try:
            session = self.db.load_session(self.db.load_subject(self.subject_id), self.session_id)
        except Exception as e:
            slicer.util.errorDisplay(f"Could not load active session for write: {e}", parent=self)
            return
        try:
            self.db.write_solution(session, solution, on_conflict="overwrite")
        except Exception as e:
            slicer.util.errorDisplay(f"Failed to write solution to database: {e}", parent=self)
            return
        self.refresh()

    @display_errors
    def onPreview(self, *args) -> None:
        sid = self._selected_solution_id()
        if not sid:
            slicer.util.errorDisplay("Select a solution first.", parent=self)
            return
        obj = self._load_solution_metadata(sid)
        if obj is None:
            slicer.util.errorDisplay(f"Could not read solution '{sid}'.", parent=self)
            return
        try:
            data = obj.to_dict()
        except Exception as e:
            data = {"error": f"Could not serialize solution: {e}"}
        _JsonTreeDialog(f"Solution {sid}", {sid: data}, parent=self).exec_()

    @display_errors
    def onExport(self, *args) -> None:
        sid = self._selected_solution_id()
        if not sid:
            slicer.util.errorDisplay("Select a solution first.", parent=self)
            return
        out_dir = qt.QFileDialog.getExistingDirectory(
            self, "Choose folder to export solution into", str(Path.home())
        )
        if not out_dir:
            return
        try:
            json_path = Path(self.db.get_solution_filepath(self.subject_id, self.session_id, sid))
        except Exception as e:
            slicer.util.errorDisplay(f"Could not locate solution '{sid}': {e}", parent=self)
            return
        nc_path = json_path.with_suffix(".nc")
        import shutil
        try:
            shutil.copy(json_path, out_dir)
            if nc_path.exists():
                shutil.copy(nc_path, out_dir)
        except Exception as e:
            slicer.util.errorDisplay(f"Failed to export solution: {e}", parent=self)
            return
        slicer.util.infoDisplay(
            f"Exported solution '{sid}' to:\n\n{out_dir}",
            windowTitle="Export solution",
            parent=self,
        )

    @display_errors
    def onDelete(self, *args) -> None:
        sid = self._selected_solution_id()
        if not sid:
            slicer.util.errorDisplay("Select a solution first.", parent=self)
            return
        if sid == self._loaded_solution_id():
            slicer.util.errorDisplay(
                f"Solution '{sid}' is currently loaded; clear it before deleting.",
                parent=self,
            )
            return
        if not slicer.util.confirmYesNoDisplay(
            f"Delete solution '{sid}' from this session?",
            "Delete solution",
            parent=self,
        ):
            return
        try:
            self._delete_solution_from_db(sid)
        except Exception as e:
            slicer.util.errorDisplay(f"Failed to delete solution '{sid}': {e}", parent=self)
            return
        self.refresh()

    def _delete_solution_from_db(self, sid: str) -> None:
        """Mirror :py:meth:`TransducerManagerDialog._delete_transducer_from_db` for solutions."""
        import shutil
        ids = list(self.db.get_solution_ids(self.subject_id, self.session_id) or [])
        if sid not in ids:
            return
        ids = [i for i in ids if i != sid]
        # write_solution_ids requires a Session; load the active session for that call.
        session = self.db.load_session(self.db.load_subject(self.subject_id), self.session_id)
        self.db.write_solution_ids(session, ids)
        try:
            solution_dir = Path(self.db.get_solution_filepath(self.subject_id, self.session_id, sid)).parent
            if solution_dir.is_dir():
                shutil.rmtree(solution_dir)
        except Exception as e:
            logging.warning(
                "Removed solution '%s' from index but could not remove its directory: %s",
                sid, e,
            )


class PhotoscanManagerDialog(qt.QDialog):
    """Tabular manager for photoscans stored under the active session.

    Columns: ``[Loaded, TT, ID, Name, Approved]``. The first LED is lit when
    the row's photoscan is currently loaded in the Slicer scene. The ``TT``
    column shows whether the photoscan is referenced by an approved
    transducer-tracking result on the active session.

    Actions:

    * **Add from File** -- import a photoscan metadata JSON (its model,
      texture and ``.mtl`` files are auto-discovered relative to the JSON).
    * **Preview** -- read-only JSON tree of the photoscan metadata.
    * **Export** -- copy the entire photoscan directory to a folder of the
      user's choice.
    * **Delete** -- drop from the session's ``photoscans.json`` index and
      remove the photoscan directory; warns when the photoscan is referenced
      by a transducer-tracking result on the active session.
    """

    def __init__(
        self,
        db: "openlifu.db.Database",
        subject_id: str,
        session_id: str,
        loaded_session: "Optional[SlicerOpenLIFUSession]" = None,
        parent="mainWindow",
    ):
        super().__init__(slicer.util.mainWindow() if parent == "mainWindow" else parent)
        self.setWindowTitle(f"Manage Photoscans - session {session_id}")
        self.setWindowModality(qt.Qt.WindowModal)
        self.db = db
        self.subject_id = subject_id
        self.session_id = session_id
        self._loaded_session = loaded_session
        self._setup()
        self.refresh()

    def _setup(self) -> None:
        layout = qt.QVBoxLayout()
        self.setLayout(layout)

        cols = ["", "ID", "Approved", "In Scene", "Name"]
        self.table = qt.QTableWidget(self)
        self.table.setColumnCount(len(cols))
        self.table.setHorizontalHeaderLabels(cols)
        self.table.setSelectionBehavior(qt.QAbstractItemView.SelectRows)
        self.table.setSelectionMode(qt.QAbstractItemView.SingleSelection)
        self.table.setEditTriggers(qt.QAbstractItemView.NoEditTriggers)
        vh = self.table.verticalHeader()
        vh.setVisible(False)
        vh.setSectionResizeMode(qt.QHeaderView.Fixed)
        vh.setDefaultSectionSize(22)
        self.table.setIconSize(qt.QSize(16, 16))
        self.table.setShowGrid(False)
        self.table.horizontalHeader().setHighlightSections(False)
        header = self.table.horizontalHeader()
        header.setSectionResizeMode(0, qt.QHeaderView.Fixed)
        self.table.setColumnWidth(0, 22)
        header.setSectionResizeMode(1, qt.QHeaderView.ResizeToContents)
        header.setSectionResizeMode(2, qt.QHeaderView.ResizeToContents)
        header.setSectionResizeMode(3, qt.QHeaderView.ResizeToContents)
        header.setSectionResizeMode(4, qt.QHeaderView.Stretch)
        self.table.horizontalHeaderItem(0).setToolTip("Transducer-tracking approval on active session")
        self.table.setSortingEnabled(True)
        layout.addWidget(self.table)

        actionRow = qt.QHBoxLayout()
        self.addFileButton = qt.QPushButton("Add from File")
        self.addFileButton.setToolTip("Import a photoscan JSON (and its companion model/texture/.mtl) into this session")
        self.previewButton = qt.QPushButton("Preview")
        self.previewButton.setToolTip("Show a read-only JSON view of the selected photoscan")
        self.exportButton = qt.QPushButton("Export")
        self.exportButton.setToolTip("Copy the selected photoscan's directory to a folder of your choice")
        self.deleteButton = qt.QPushButton("Delete")
        self.deleteButton.setToolTip("Remove the selected photoscan from this session")
        for b in (self.addFileButton, self.previewButton, self.exportButton, self.deleteButton):
            b.setSizePolicy(qt.QSizePolicy.Expanding, qt.QSizePolicy.Preferred)
            actionRow.addWidget(b)
        layout.addLayout(actionRow)

        bb = qt.QDialogButtonBox()
        bb.addButton("Close", qt.QDialogButtonBox.RejectRole)
        bb.rejected.connect(self.reject)
        layout.addWidget(bb)

        self.addFileButton.clicked.connect(self.onAddFromFile)
        self.previewButton.clicked.connect(self.onPreview)
        self.exportButton.clicked.connect(self.onExport)
        self.deleteButton.clicked.connect(self.onDelete)
        self.table.doubleClicked.connect(self.onPreview)

        screen = qt.QDesktopWidget().screenGeometry()
        self.resize(int(screen.width() * 0.5), int(screen.height() * 0.4))

    def _loaded_photoscan_ids(self) -> set:
        try:
            return set(get_openlifu_data_parameter_node().loaded_photoscans.keys())
        except Exception:
            return set()

    def _tt_approved_ids(self) -> set:
        if self._loaded_session is None:
            return set()
        try:
            return set(self._loaded_session.get_transducer_tracking_approvals() or [])
        except Exception:
            return set()

    def _tt_referenced_ids(self) -> set:
        """photoscan_ids that appear in the active session's transducer_tracking_results."""
        if self._loaded_session is None:
            return set()
        try:
            session_openlifu = self._loaded_session.session.session
            return {r.photoscan_id for r in getattr(session_openlifu, "transducer_tracking_results", []) or []}
        except Exception:
            return set()

    def _load_photoscan_metadata(self, pid: str) -> Optional["openlifu.nav.photoscan.Photoscan"]:
        try:
            # With load_data=False the database returns the bare Photoscan (no model/texture tuple).
            return self.db.load_photoscan(self.subject_id, self.session_id, pid, load_data=False)
        except Exception as e:
            logging.warning("Could not read photoscan %s for listing: %s", pid, e)
            return None

    def refresh(self) -> None:
        self.table.setSortingEnabled(False)
        self.table.clearContents()
        try:
            ids = list(self.db.get_photoscan_ids(self.subject_id, self.session_id) or [])
        except Exception as e:
            logging.warning("Could not list photoscan ids: %s", e)
            ids = []
        loaded_ids = self._loaded_photoscan_ids()
        tt_approved = self._tt_approved_ids()
        tt_referenced = self._tt_referenced_ids()
        on_icon = _make_led_icon("#2ecc71")
        off_icon = _make_led_icon("#444")

        self.table.setRowCount(len(ids))
        for row, pid in enumerate(ids):
            obj = self._load_photoscan_metadata(pid)
            if obj is not None:
                name = getattr(obj, "name", pid)
                approved = "yes" if bool(getattr(obj, "photoscan_approved", False)) else "no"
            else:
                name = pid
                approved = "?"

            led_item = qt.QTableWidgetItem()
            if pid in tt_approved:
                led_item.setIcon(on_icon)
                led_item.setToolTip("Transducer-tracking approved on active session")
            else:
                led_item.setIcon(off_icon)
                if pid in tt_referenced:
                    led_item.setToolTip("Referenced by transducer-tracking results but not fully approved")
                else:
                    led_item.setToolTip("")
            led_item.setFlags(qt.Qt.ItemIsEnabled | qt.Qt.ItemIsSelectable)
            self.table.setItem(row, 0, led_item)

            self.table.setItem(row, 1, qt.QTableWidgetItem(str(pid)))
            self.table.setItem(row, 2, qt.QTableWidgetItem(approved))

            in_scene_item = qt.QTableWidgetItem("yes" if pid in loaded_ids else "no")
            in_scene_item.setToolTip(
                "Loaded into the scene (visibility off until previewed or used for tracking)"
                if pid in loaded_ids else ""
            )
            self.table.setItem(row, 3, in_scene_item)

            self.table.setItem(row, 4, qt.QTableWidgetItem(str(name)))

        self.table.setSortingEnabled(True)

    def _selected_photoscan_id(self) -> Optional[str]:
        items = self.table.selectedItems()
        if not items:
            return None
        row = items[0].row()
        idItem = self.table.item(row, 1)
        return idItem.text() if idItem else None

    @display_errors
    def onAddFromFile(self, checked: bool = False) -> None:
        qsettings = qt.QSettings()
        filepath: str = qt.QFileDialog.getOpenFileName(
            slicer.util.mainWindow(),
            "Import photoscan",
            qsettings.value("OpenLIFU/databaseDirectory", "."),
            "Photoscans (*.json);;All Files (*)",
        )
        if not filepath:
            return
        json_path = Path(filepath)
        try:
            import openlifu.nav.photoscan
            photoscan = openlifu.nav.photoscan.Photoscan.from_file(str(json_path))
        except Exception as e:
            slicer.util.errorDisplay(f"Failed to read photoscan file: {e}", parent=self)
            return

        def _maybe_path(relname):
            if not relname:
                return None
            p = json_path.parent / relname
            return str(p) if p.exists() else None

        model_path = _maybe_path(photoscan.model_filename)
        texture_path = _maybe_path(photoscan.texture_filename)
        mtl_path = _maybe_path(photoscan.mtl_filename)

        if model_path is None:
            slicer.util.errorDisplay(
                f"Could not find the model file referenced by the photoscan JSON "
                f"(expected next to it as '{photoscan.model_filename}').",
                parent=self,
            )
            return

        try:
            self.db.write_photoscan(
                self.subject_id,
                self.session_id,
                photoscan,
                model_data_filepath=model_path,
                texture_data_filepath=texture_path,
                mtl_data_filepath=mtl_path,
                on_conflict="overwrite",
            )
        except Exception as e:
            slicer.util.errorDisplay(f"Failed to write photoscan to database: {e}", parent=self)
            return
        self.refresh()

    @display_errors
    def onPreview(self, *args) -> None:
        pid = self._selected_photoscan_id()
        if not pid:
            slicer.util.errorDisplay("Select a photoscan first.", parent=self)
            return

        # The Transducer Localization page already provides a 3D preview dialog with
        # camera reset, visibility toggling and view-node isolation. Use it here so
        # the user gets a consistent preview experience across modules.
        try:
            from OpenLIFUTransducerLocalization import PhotoscanPreviewDialog
        except Exception as e:
            logging.warning("Falling back to JSON preview for photoscan %s: %s", pid, e)
            self._json_preview(pid)
            return

        slicer_photoscan = self._loaded_photoscan_object(pid)
        if slicer_photoscan is None:
            self._json_preview(pid)
            return

        with BusyCursor():
            dialog = PhotoscanPreviewDialog(slicer_photoscan, parent=self)
        dialog.exec_()
        dialog.deleteLater()
        self.refresh()

    def _loaded_photoscan_object(self, pid: str) -> "Optional[SlicerOpenLIFUPhotoscan]":
        """Return the SlicerOpenLIFUPhotoscan for ``pid``, loading it on demand if needed."""
        param = get_openlifu_data_parameter_node()
        if pid in param.loaded_photoscans:
            return param.loaded_photoscans[pid]
        # On-demand load. Requires an active session matching this manager.
        loaded_session = param.loaded_session
        if loaded_session is None or loaded_session.get_session_id() != self.session_id:
            return None
        try:
            data_logic = slicer.util.getModuleLogic("OpenLIFUData")
            openlifu_photoscan = self._load_photoscan_metadata(pid)
            if openlifu_photoscan is None:
                return None
            return data_logic.load_photoscan_from_openlifu(
                openlifu_photoscan,
                load_from_active_session=True,
                replace_confirmed=True,
            )
        except Exception as e:
            logging.warning("Could not load photoscan %s into the scene for preview: %s", pid, e)
            return None

    def _json_preview(self, pid: str) -> None:
        obj = self._load_photoscan_metadata(pid)
        if obj is None:
            slicer.util.errorDisplay(f"Could not read photoscan '{pid}'.", parent=self)
            return
        try:
            data = obj.to_dict()
        except Exception as e:
            data = {"error": f"Could not serialize photoscan: {e}"}
        _JsonTreeDialog(f"Photoscan {pid}", {pid: data}, parent=self).exec_()

    @display_errors
    def onExport(self, *args) -> None:
        pid = self._selected_photoscan_id()
        if not pid:
            slicer.util.errorDisplay("Select a photoscan first.", parent=self)
            return
        try:
            ph_meta_path = Path(self.db.get_photoscan_metadata_filepath(self.subject_id, self.session_id, pid))
        except Exception as e:
            slicer.util.errorDisplay(f"Could not locate photoscan '{pid}': {e}", parent=self)
            return
        src_dir = ph_meta_path.parent
        if not src_dir.is_dir():
            slicer.util.errorDisplay(f"Photoscan directory does not exist:\n\n{src_dir}", parent=self)
            return
        out_parent = qt.QFileDialog.getExistingDirectory(
            self, "Choose folder to export photoscan into", str(Path.home())
        )
        if not out_parent:
            return
        import shutil
        dst_dir = Path(out_parent) / src_dir.name
        try:
            shutil.copytree(src_dir, dst_dir, dirs_exist_ok=True)
        except Exception as e:
            slicer.util.errorDisplay(f"Failed to export photoscan: {e}", parent=self)
            return
        slicer.util.infoDisplay(
            f"Exported photoscan '{pid}' to:\n\n{dst_dir}",
            windowTitle="Export photoscan",
            parent=self,
        )

    @display_errors
    def onDelete(self, *args) -> None:
        pid = self._selected_photoscan_id()
        if not pid:
            slicer.util.errorDisplay("Select a photoscan first.", parent=self)
            return
        if pid in self._loaded_photoscan_ids():
            slicer.util.errorDisplay(
                f"Photoscan '{pid}' is currently loaded in the scene; unload it before deleting.",
                parent=self,
            )
            return
        if pid in self._tt_referenced_ids():
            if not slicer.util.confirmYesNoDisplay(
                f"Photoscan '{pid}' is referenced by a transducer-tracking result on the active "
                "session. Deleting the photoscan will leave a stale reference. Continue?",
                "Delete photoscan",
                parent=self,
            ):
                return
        elif not slicer.util.confirmYesNoDisplay(
            f"Delete photoscan '{pid}' from this session?",
            "Delete photoscan",
            parent=self,
        ):
            return
        try:
            self._delete_photoscan_from_db(pid)
        except Exception as e:
            slicer.util.errorDisplay(f"Failed to delete photoscan '{pid}': {e}", parent=self)
            return
        self.refresh()

    def _delete_photoscan_from_db(self, pid: str) -> None:
        import shutil
        ids = list(self.db.get_photoscan_ids(self.subject_id, self.session_id) or [])
        if pid not in ids:
            return
        ids = [i for i in ids if i != pid]
        self.db.write_photoscan_ids(self.subject_id, self.session_id, ids)
        try:
            ph_dir = Path(self.db.get_photoscan_metadata_filepath(self.subject_id, self.session_id, pid)).parent
            if ph_dir.is_dir():
                shutil.rmtree(ph_dir)
        except Exception as e:
            logging.warning(
                "Removed photoscan '%s' from index but could not remove its directory: %s",
                pid, e,
            )


class RunManagerDialog(qt.QDialog):
    """Tabular manager for runs stored under the active session.

    Columns: ``[LED, ID, Name, Success, Solution ID, Notes]``. The LED is lit
    when the row's run is the currently loaded run. Actions:

    * **Preview** -- read-only JSON tree showing the run JSON together with
      its session and protocol snapshots.
    * **Export** -- copy the run directory (run JSON + session/protocol
      snapshots) to a folder of the user's choice.
    * **Delete** -- drop from the session's ``runs.json`` index and remove
      the run directory.

    When ``view_only=True`` the Add/Delete actions are hidden; this is how
    the Sonication Control page surfaces the dialog as a "run log viewer".
    """

    def __init__(
        self,
        db: "openlifu.db.Database",
        subject_id: str,
        session_id: str,
        parent="mainWindow",
        view_only: bool = False,
    ):
        super().__init__(slicer.util.mainWindow() if parent == "mainWindow" else parent)
        title = "View Run Logs" if view_only else "Manage Runs"
        self.setWindowTitle(f"{title} - session {session_id}")
        self.setWindowModality(qt.Qt.WindowModal)
        self.db = db
        self.subject_id = subject_id
        self.session_id = session_id
        self.view_only = bool(view_only)
        self._setup()
        self.refresh()

    def _setup(self) -> None:
        layout = qt.QVBoxLayout()
        self.setLayout(layout)

        cols = ["", "ID", "Name", "Success", "Solution ID", "Notes"]
        self.table = qt.QTableWidget(self)
        self.table.setColumnCount(len(cols))
        self.table.setHorizontalHeaderLabels(cols)
        self.table.setSelectionBehavior(qt.QAbstractItemView.SelectRows)
        self.table.setSelectionMode(qt.QAbstractItemView.SingleSelection)
        self.table.setEditTriggers(qt.QAbstractItemView.NoEditTriggers)
        self.table.verticalHeader().setVisible(False)
        self.table.setShowGrid(False)
        self.table.horizontalHeader().setHighlightSections(False)
        header = self.table.horizontalHeader()
        header.setSectionResizeMode(0, qt.QHeaderView.Fixed)
        self.table.setColumnWidth(0, 22)
        header.setSectionResizeMode(1, qt.QHeaderView.ResizeToContents)
        header.setSectionResizeMode(2, qt.QHeaderView.ResizeToContents)
        header.setSectionResizeMode(3, qt.QHeaderView.ResizeToContents)
        header.setSectionResizeMode(4, qt.QHeaderView.ResizeToContents)
        header.setSectionResizeMode(5, qt.QHeaderView.Stretch)
        self.table.setSortingEnabled(True)
        layout.addWidget(self.table)

        actionRow = qt.QHBoxLayout()
        self.addFileButton = qt.QPushButton("Add from File")
        self.addFileButton.setToolTip("Import a run JSON into this session")
        self.previewButton = qt.QPushButton("Preview")
        self.previewButton.setToolTip("Show a read-only JSON view of the selected run and its snapshots")
        self.exportButton = qt.QPushButton("Export")
        self.exportButton.setToolTip("Copy the selected run's directory to a folder of your choice")
        self.deleteButton = qt.QPushButton("Delete")
        self.deleteButton.setToolTip("Remove the selected run from this session")
        for b in (self.addFileButton, self.previewButton, self.exportButton, self.deleteButton):
            b.setSizePolicy(qt.QSizePolicy.Expanding, qt.QSizePolicy.Preferred)
            actionRow.addWidget(b)
        if self.view_only:
            self.addFileButton.setVisible(False)
            self.deleteButton.setVisible(False)
        layout.addLayout(actionRow)

        bb = qt.QDialogButtonBox()
        bb.addButton("Close", qt.QDialogButtonBox.RejectRole)
        bb.rejected.connect(self.reject)
        layout.addWidget(bb)

        self.addFileButton.clicked.connect(self.onAddFromFile)
        self.previewButton.clicked.connect(self.onPreview)
        self.exportButton.clicked.connect(self.onExport)
        self.deleteButton.clicked.connect(self.onDelete)
        self.table.doubleClicked.connect(self.onPreview)

        screen = qt.QDesktopWidget().screenGeometry()
        self.resize(int(screen.width() * 0.55), int(screen.height() * 0.4))

    def _loaded_run_id(self) -> Optional[str]:
        try:
            run = get_openlifu_data_parameter_node().loaded_run
            if run is None:
                return None
            return run.run.id
        except Exception:
            return None

    def _load_run_metadata(self, rid: str) -> Optional["openlifu.plan.Run"]:
        try:
            import openlifu.plan
            run_filepath = self.db.get_run_filepath(self.subject_id, self.session_id, rid)
            return openlifu.plan.Run.from_file(run_filepath)
        except Exception as e:
            logging.warning("Could not read run %s for listing: %s", rid, e)
            return None

    def refresh(self) -> None:
        self.table.setSortingEnabled(False)
        self.table.clearContents()
        try:
            ids = list(self.db.get_run_ids(self.subject_id, self.session_id) or [])
        except Exception as e:
            logging.warning("Could not list run ids: %s", e)
            ids = []
        loaded_id = self._loaded_run_id()
        on_icon = _make_led_icon("#2ecc71")
        off_icon = _make_led_icon("#444")

        self.table.setRowCount(len(ids))
        for row, rid in enumerate(ids):
            obj = self._load_run_metadata(rid)
            if obj is not None:
                name = getattr(obj, "name", rid)
                success = "yes" if bool(getattr(obj, "success_flag", False)) else "no"
                sol_id = getattr(obj, "solution_id", "") or ""
                note = (getattr(obj, "note", "") or "").splitlines()[0] if getattr(obj, "note", "") else ""
            else:
                name = rid
                success = "?"
                sol_id = ""
                note = ""

            led_item = qt.QTableWidgetItem()
            is_loaded = loaded_id is not None and rid == loaded_id
            led_item.setIcon(on_icon if is_loaded else off_icon)
            led_item.setFlags(qt.Qt.ItemIsEnabled | qt.Qt.ItemIsSelectable)
            led_item.setToolTip("Currently loaded" if is_loaded else "")
            self.table.setItem(row, 0, led_item)
            self.table.setItem(row, 1, qt.QTableWidgetItem(str(rid)))
            self.table.setItem(row, 2, qt.QTableWidgetItem(str(name)))
            self.table.setItem(row, 3, qt.QTableWidgetItem(success))
            self.table.setItem(row, 4, qt.QTableWidgetItem(str(sol_id)))
            self.table.setItem(row, 5, qt.QTableWidgetItem(str(note)))

        self.table.resizeRowsToContents()
        self.table.setSortingEnabled(True)

    def _selected_run_id(self) -> Optional[str]:
        items = self.table.selectedItems()
        if not items:
            return None
        row = items[0].row()
        idItem = self.table.item(row, 1)
        return idItem.text() if idItem else None

    @display_errors
    def onAddFromFile(self, checked: bool = False) -> None:
        qsettings = qt.QSettings()
        filepath: str = qt.QFileDialog.getOpenFileName(
            slicer.util.mainWindow(),
            "Import run",
            qsettings.value("OpenLIFU/databaseDirectory", "."),
            "Runs (*.json);;All Files (*)",
        )
        if not filepath:
            return
        try:
            import openlifu.plan
            run = openlifu.plan.Run.from_file(filepath)
        except Exception as e:
            slicer.util.errorDisplay(f"Failed to read run file: {e}", parent=self)
            return
        try:
            session = self.db.load_session(self.db.load_subject(self.subject_id), self.session_id)
        except Exception as e:
            slicer.util.errorDisplay(f"Could not load active session for write: {e}", parent=self)
            return
        try:
            self.db.write_run(run, session=session, on_conflict="skip")
        except Exception as e:
            slicer.util.errorDisplay(f"Failed to write run to database: {e}", parent=self)
            return
        self.refresh()

    @display_errors
    def onPreview(self, *args) -> None:
        rid = self._selected_run_id()
        if not rid:
            slicer.util.errorDisplay("Select a run first.", parent=self)
            return
        run = self._load_run_metadata(rid)
        if run is None:
            slicer.util.errorDisplay(f"Could not read run '{rid}'.", parent=self)
            return
        try:
            run_data = run.to_dict()
        except Exception as e:
            run_data = {"error": f"Could not serialize run: {e}"}

        tree_data: Dict[str, Any] = {"run": run_data}
        try:
            session_snap = self.db.load_session_snapshot(self.subject_id, self.session_id, rid)
            tree_data["session_snapshot"] = session_snap.to_dict()
        except Exception as e:
            tree_data["session_snapshot"] = {"error": str(e)}
        try:
            protocol_snap = self.db.load_protocol_snapshot(self.subject_id, self.session_id, rid)
            tree_data["protocol_snapshot"] = protocol_snap.to_dict()
        except Exception as e:
            tree_data["protocol_snapshot"] = {"error": str(e)}

        _JsonTreeDialog(f"Run {rid}", tree_data, parent=self).exec_()

    @display_errors
    def onExport(self, *args) -> None:
        rid = self._selected_run_id()
        if not rid:
            slicer.util.errorDisplay("Select a run first.", parent=self)
            return
        try:
            run_dir = Path(self.db.get_run_dir(self.subject_id, self.session_id, rid))
        except Exception as e:
            slicer.util.errorDisplay(f"Could not locate run '{rid}': {e}", parent=self)
            return
        if not run_dir.is_dir():
            slicer.util.errorDisplay(f"Run directory does not exist:\n\n{run_dir}", parent=self)
            return
        out_parent = qt.QFileDialog.getExistingDirectory(
            self, "Choose folder to export run into", str(Path.home())
        )
        if not out_parent:
            return
        import shutil
        dst_dir = Path(out_parent) / run_dir.name
        try:
            shutil.copytree(run_dir, dst_dir, dirs_exist_ok=True)
        except Exception as e:
            slicer.util.errorDisplay(f"Failed to export run: {e}", parent=self)
            return
        slicer.util.infoDisplay(
            f"Exported run '{rid}' to:\n\n{dst_dir}",
            windowTitle="Export run",
            parent=self,
        )

    @display_errors
    def onDelete(self, *args) -> None:
        rid = self._selected_run_id()
        if not rid:
            slicer.util.errorDisplay("Select a run first.", parent=self)
            return
        if rid == self._loaded_run_id():
            slicer.util.errorDisplay(
                f"Run '{rid}' is currently loaded; clear it before deleting.",
                parent=self,
            )
            return
        if not slicer.util.confirmYesNoDisplay(
            f"Delete run '{rid}' from this session? Runs are write-once and "
            "cannot be recovered after deletion.",
            "Delete run",
            parent=self,
        ):
            return
        try:
            self._delete_run_from_db(rid)
        except Exception as e:
            slicer.util.errorDisplay(f"Failed to delete run '{rid}': {e}", parent=self)
            return
        self.refresh()

    def _delete_run_from_db(self, rid: str) -> None:
        import shutil
        ids = list(self.db.get_run_ids(self.subject_id, self.session_id) or [])
        if rid not in ids:
            return
        ids = [i for i in ids if i != rid]
        self.db.write_run_ids(self.subject_id, self.session_id, ids)
        try:
            run_dir = Path(self.db.get_run_dir(self.subject_id, self.session_id, rid))
            if run_dir.is_dir():
                shutil.rmtree(run_dir)
        except Exception as e:
            logging.warning(
                "Removed run '%s' from index but could not remove its directory: %s",
                rid, e,
            )


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
        self.uiWidget = uiWidget
        self.ui = slicer.util.childWidgetVariables(uiWidget)

        # Restructure the page into header (interactive on Data) + scrollable
        # body + footer. The header's children are aliased onto self.ui so
        # existing self.ui.databasePopupButton / self.ui.loginPopupButton /
        # self.ui.devicePopupButton references continue to work.
        self.module_header = apply_module_layout(
            uiWidget, ui_namespace=self.ui, header_read_only=False
        )

        # Wire all the global-state observers that keep the header's status
        # buttons in sync (DB, login, device, cloud sync, Home
        # navigation). Same plumbing every other module uses for its
        # read-only header. Registration is deferred one event-loop tick
        # internally; see wire_passive_module_header.
        wire_passive_module_header(self, self.module_header)

        # Set scene in MRML widgets. Make sure that in Qt designer the top-level qMRMLWidget's
        # "mrmlSceneChanged(vtkMRMLScene*)" signal in is connected to each MRML widget's.
        # "setMRMLScene(vtkMRMLScene*)" slot.
        uiWidget.setMRMLScene(slicer.mrmlScene)

        # Create logic class. Logic implements all computations that should be possible to run
        # in batch mode, without a graphical user interface.
        self.logic = OpenLIFUDataLogic()

        # The legacy user-account banner has been retired; the shared
        # ``ModuleHeaderWidget`` (inserted by ``apply_module_layout`` above)
        # owns the database / account / device status indicators now.

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
        # OpenLIFUData lives *outside* Workflow.modules (Home jumps straight to
        # OpenLIFUSession). We still want a Back/Next footer so the operator
        # can return to Home or step forward into Session, so we use the
        # off-workflow helper instead of the registry-backed one.
        self.inject_custom_workflow_controls_into_placeholder(
            previous_module_name="OpenLIFUHome",
            next_module_name="OpenLIFUSession",
            include_session_controls=False,
            enforce_can_proceed_always=True,
            on_back_override=self._on_data_manager_back,
        )

        # ---- Internal connections and observers ----
        register_module_callback(
            self,
            self.logic.call_on_subject_changed,
            self.logic.remove_subject_changed_callback,
            self.on_subject_changed,
        )

        # ---- External connections and observers ----

        # These connections ensure that we update parameter node when scene is closed
        self.addObserver(slicer.mrmlScene, slicer.mrmlScene.StartCloseEvent, self.onSceneStartClose)
        self.addObserver(slicer.mrmlScene, slicer.mrmlScene.EndCloseEvent, self.onSceneEndClose)

        # This ensures that we properly handle SlicerOpenLIFU objects that become invalid when their nodes are deleted
        self.setupSHNodeObserver()
        self.addObserver(slicer.mrmlScene, slicer.vtkMRMLScene.NodeAddedEvent, self.onNodeAdded)
        self.addObserver(slicer.mrmlScene, slicer.vtkMRMLScene.NodeRemovedEvent, self.onNodeRemoved)
        
        # Connect to the database logic for updates related to database
        db_logic = slicer.util.getModuleLogic("OpenLIFUDatabase")
        register_module_callback(
            self,
            db_logic.call_on_db_changed,
            db_logic.remove_db_changed_callback,
            self.onDatabaseChanged,
        )

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

        # Dependencies collapsible section: status checks + install buttons.
        # These previously lived on the Login page; they are independent of the
        # login state so they belong on the Data page.
        self.ui.installPythonRequirementsPushButton.clicked.connect(
            self.onUpdateOpenLIFUClicked
        )
        self.ui.installKwaveBinariesPushButton.clicked.connect(
            self.onInstallKwaveBinariesClicked
        )
        self.ui.installADBPushButton.clicked.connect(self.onInstallADBClicked)

        # Protocols collapsible section
        self.ui.manageProtocolsPushButton.clicked.connect(
            self.onManageProtocolsClicked
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
        self.ui.managePhotoscansPushButton.clicked.connect(self.onManagePhotoscansClicked)
        self.ui.manageSolutionsPushButton.clicked.connect(self.onManageSolutionsClicked)
        self.ui.manageRunsPushButton.clicked.connect(self.onManageRunsClicked)

        # Administration collapsible section: admin-only controls. The
        # collapsible itself is hidden for non-admins via the
        # ``slicer.openlifu.allowed-roles`` tag wired by Login. ``Manage
        # Accounts`` is owned by the Login module; we forward the click here
        # so the same dialog is reachable from the Data page.
        manage_accounts_btn = getattr(self.ui, "manageAccountsButton", None)
        if manage_accounts_btn is not None:
            manage_accounts_btn.clicked.connect(self.onManageAccountsClicked)

        # ---- Issue updates that may not have been triggered yet ---
        
        # Make sure parameter node is initialized (needed for module reload)
        self.initializeParameterNode()
        
        self.updateLoadedObjectsView()
        self.updateSessionStatus()
        self.update_loadSubjectButton_enabled()
        self.update_db_dependent_sections_enabled()
        self.update_volumesCollapsibleButton_checked_and_enabled()
        self.update_sessionCollapsibleButton_checked_and_enabled()
        self.updateWorkflowControls()
        self._updateAdministrationStatusLabels()
        self._updateManageAccountsButton()

        self._initDependencyStatus()

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
        """Wire the Data-specific observers that the shared header's passive
        wiring does not cover: per-section permissions gating and the
        device-state transition log line.

        Must run *after* this widget's setup() returns; see the comment in
        setup() for why we defer it via QTimer.singleShot(0).
        """
        # Permissions gating (enable/disable section collapsibles) reacts
        # to the same Login-parameter-node and active-user signals that
        # drive the header's permissions dropdown. The header itself
        # doesn't know about page-body sections, so we observe both
        # signals separately here.
        try:
            login_parameter_node = slicer.util.getModuleLogic("OpenLIFULogin").getParameterNode()
            self.addObserver(
                login_parameter_node,
                vtk.vtkCommand.ModifiedEvent,
                lambda caller, event: self.updatePermissionsGating(),
            )
        except (AttributeError, RuntimeError):
            return
        try:
            login_logic = slicer.util.getModuleLogic("OpenLIFULogin")
            register_module_callback(
                self,
                login_logic.call_on_active_user_changed,
                login_logic.remove_active_user_changed_callback,
                lambda _user: self.updatePermissionsGating(),
            )
        except (AttributeError, RuntimeError):
            pass

        # Device connect / disconnect: the header's passive wiring already
        # repaints the device button outline. We only need the additional
        # ``[LIFUInterface]`` log line on actual transitions (see
        # _logDeviceStateTransition for why this is polled rather than
        # signal-driven).
        try:
            sc_logic = slicer.util.getModuleLogic("OpenLIFUSonicationControl")
            register_module_callback(
                self,
                sc_logic.call_on_lifu_device_connected,
                sc_logic.remove_callback,
                lambda *_args, **_kwargs: self._logDeviceStateTransition(),
            )
            register_module_callback(
                self,
                sc_logic.call_on_lifu_device_disconnected,
                sc_logic.remove_callback,
                lambda *_args, **_kwargs: self._logDeviceStateTransition(),
            )
        except (AttributeError, RuntimeError):
            pass

        # Now that Login is fully alive, run the first sync of per-section
        # gating and emit the initial device-state log line.
        self.updatePermissionsGating()
        self._logDeviceStateTransition()

        # If a database directory was remembered from a previous session
        # (QSettings("OpenLIFU/databaseDirectory")) and it still looks like a
        # valid openlifu database root, auto-connect to it now so the user
        # doesn't have to re-open the Database popup on every Slicer boot.
        # Defer one event-loop tick so the "Loading database..." progress
        # dialog can actually paint before the (potentially slow) openlifu
        # lazy-import + database scan runs. This is especially important in
        # the custom desktop app where setup() runs at startup before the
        # main window has rendered.
        qt.QTimer.singleShot(50, self._tryAutoConnectDatabase)

    def _tryAutoConnectDatabase(self) -> None:
        """Best-effort auto-connect to the last-used openlifu database.

        Reads the persisted ``OpenLIFU/databaseDirectory`` QSetting and, if it
        points at a valid openlifu database root and no database is currently
        loaded, calls the Database logic's ``load_database()``. All errors are
        swallowed (logged) -- a missing or moved database directory must not
        block module setup.

        Shows a modal "Loading database..." popup while the load runs, since
        the very first ``load_database`` call triggers the lazy import of the
        entire ``openlifu`` package and can take a few seconds.
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

            progress = qt.QProgressDialog(
                _("Loading database..."), "", 0, 0, slicer.util.mainWindow()
            )
            progress.setWindowTitle(_("OpenLIFU"))
            progress.setWindowModality(qt.Qt.ApplicationModal)
            progress.setCancelButton(None)
            progress.setMinimumDuration(0)
            progress.setAutoClose(False)
            progress.setAutoReset(False)
            progress.show()
            slicer.app.processEvents()
            try:
                db_logic.load_database(path)
                logging.info("Auto-connected to openlifu database at %s", path)
            finally:
                progress.close()
                progress.deleteLater()
        except Exception as e:
            logging.warning("Database auto-connect failed: %s", e)

    def onDatabaseChanged(self, db: Optional["openlifu.db.Database"] = None):
        self.logic.subject = None
        self.logic.clear_session()
        self.update_loadSubjectButton_enabled()
        self.update_db_dependent_sections_enabled()
        self._updateAdministrationStatusLabels()
        self._updateManageAccountsButton()

    def update_db_dependent_sections_enabled(self) -> None:
        """Disable the Subject / Protocols / Transducers sections when no
        database is connected so the user can't try to expand or interact with
        them. Re-enables them when a database is loaded."""
        db_loaded = get_cur_db() is not None
        for name in ("subjectCollapsibleButton", "protocolsCollapsibleButton", "transducersCollapsibleButton"):
            collapsible = getattr(self.ui, name, None)
            if collapsible is None:
                continue
            collapsible.setEnabled(db_loaded)
            if not db_loaded:
                collapsible.collapsed = True

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

        self._update_session_manage_buttons_enabled()

    def _update_session_manage_buttons_enabled(self) -> None:
        """Enable the per-session Manage buttons only when a session is loaded."""
        loaded = (
            self._parameterNode is not None
            and getattr(self._parameterNode, "loaded_session", None) is not None
        )
        tip_off = "Requires a loaded session"
        for btn, tip_on in (
            (self.ui.managePhotoscansPushButton, "Open the photoscan manager for the active session"),
            (self.ui.manageSolutionsPushButton, "Open the solution manager for the active session"),
            (self.ui.manageRunsPushButton, "Open the run manager for the active session"),
        ):
            btn.setEnabled(loaded)
            btn.setToolTip(tip_on if loaded else tip_off)

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
        new_session_id = load_session_dlg.exec_and_get_session_id()

        if not new_session_id:
            return False

        self.logic.clear_session(clean_up_scene=True)
        self.logic.load_session(self.logic.subject.id, new_session_id)
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
        if self.workflow_controls is None:
            return
        if self.logic.subject is None:
            self.workflow_controls.can_proceed = False
            self.workflow_controls.status_text = "Load a subject and session to proceed."
        elif self._parameterNode is None or self._parameterNode.loaded_session is None:
            self.workflow_controls.can_proceed = False
            self.workflow_controls.status_text = "Load a session to proceed."
        else:
            self.workflow_controls.can_proceed = True
            self.workflow_controls.status_text = "Session loaded, proceed to the next step."

    @display_errors
    def _on_data_manager_back(self) -> None:
        """Custom Back handler for the off-workflow Data Manager footer.

        If a session is loaded, prompt to save/discard it (same flow as the
        in-workflow Exit button) before returning to Home. Without this hook,
        Back would leave the session loaded but jump back to Home, leaving
        the user in an inconsistent state.
        """
        from OpenLIFULib.guided_mode_util import confirm_exit_session_dialog
        if self._parameterNode is not None and self._parameterNode.loaded_session is not None:
            choice = confirm_exit_session_dialog()
            if choice == "cancel":
                return
            if choice == "save":
                self.logic.save_session()
            self.logic.clear_session(clean_up_scene=True)
        slicer.util.selectModule("OpenLIFUHome")

    # ------------------------------------------------------------------
    # Administration section (admin-only)
    # ------------------------------------------------------------------

    def _updateAdministrationStatusLabels(self) -> None:
        """Refresh the display-only User Mode / Guided Mode labels in the
        Administration section. The values are env-driven (single
        ``OPENLIFU_USER_MODE`` env var) and cannot be changed at runtime."""
        from OpenLIFULib.kiosk_util import (
            USER_MODE_ENV_VAR,
            get_user_mode,
        )
        on = bool(get_user_mode())
        on_off = "ON" if on else "OFF"
        for name in ("userModeStatusLabel", "guidedModeStatusLabel"):
            label = getattr(self.ui, name, None)
            if label is None:
                continue
            label.setText(on_off)
        env_help = getattr(self.ui, "userModeEnvHelpLabel", None)
        if env_help is not None:
            env_help.setText(
                f"User Mode is set via the {USER_MODE_ENV_VAR} environment variable "
                f"on launch and cannot be changed at runtime. Currently {on_off}."
            )

    def _updateManageAccountsButton(self) -> None:
        """Enable/disable the Administration > Manage Accounts button based on
        whether a database is connected."""
        btn = getattr(self.ui, "manageAccountsButton", None)
        if btn is None:
            return
        if get_cur_db() is None:
            btn.setEnabled(False)
            btn.setToolTip("Connect a database first.")
        else:
            btn.setEnabled(True)
            btn.setToolTip("Open the Manage Accounts dialog.")

    @display_errors
    def onManageAccountsClicked(self, checked: bool = False) -> None:
        """Forward the click to OpenLIFULogin's Manage Accounts handler."""
        try:
            login_widget = slicer.util.getModuleWidget("OpenLIFULogin")
        except Exception as exc:  # noqa: BLE001
            slicer.util.errorDisplay(
                f"Could not open Manage Accounts: {exc}",
                parent=slicer.util.mainWindow(),
            )
            return
        handler = getattr(login_widget, "onManageAccountsButtonclicked", None)
        if handler is None:
            slicer.util.errorDisplay(
                "Manage Accounts handler is unavailable.",
                parent=slicer.util.mainWindow(),
            )
            return
        handler(False)

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
        self._update_session_manage_buttons_enabled()
        self.updateWorkflowControls()

    def cleanup(self) -> None:
        """Called when the application closes and the module widget is destroyed."""
        cleanup_module_callbacks(self)
        self.removeObservers()

    def _is_blocked_for_non_admin(self) -> bool:
        """Return ``True`` when the Data Manager should refuse to open for the
        current user (user-account-mode is on and the active user is not an
        admin). When this returns ``True``, ``enter`` bounces the user back
        to Home with a warning dialog.
        """
        try:
            uam = bool(get_user_account_mode_state())
        except (AttributeError, RuntimeError):
            uam = False
        if not uam:
            return False
        try:
            cur_user = get_current_user()
        except (AttributeError, RuntimeError):
            cur_user = None
        user_id = getattr(cur_user, "id", None)
        is_real_user = (
            cur_user is not None
            and user_id not in (None, "anonymous", "default_admin")
        )
        user_roles = list(getattr(cur_user, "roles", None) or [])
        return not (is_real_user and "admin" in user_roles)

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

    def _logDeviceStateTransition(self) -> None:
        """Emit a ``[LIFUInterface]`` log line whenever the polled device
        connection state has changed since the last call.

        The shared header's :py:meth:`ModuleHeaderWidget.updateStatusButtons`
        already paints the device button outline; this method exists only to
        emit a human-readable log line on transitions.

        We log here (rather than from the OWSignal ``signal_connected`` /
        ``signal_disconnected`` callbacks alone) because the OWSignal-driven
        logs in OpenLIFUSonicationControl only fire on transitions during a
        running session. The polled state can change without a signal firing
        -- for example on first render, when the device is already in use
        by another Slicer instance.
        """
        try:
            sc_logic = slicer.util.getModuleLogic("OpenLIFUSonicationControl")
            iface = getattr(sc_logic, "cur_lifu_interface", None)
            tx_conn, hv_conn = iface.is_device_connected() if iface is not None else (False, False)
        except (AttributeError, RuntimeError):
            iface = None
            tx_conn, hv_conn = False, False
        is_simulated = bool(getattr(iface, "is_simulated", False))

        prev = getattr(self, "_last_device_conn_state", "unset")
        cur = ("simulated" if is_simulated else (bool(tx_conn), bool(hv_conn)))
        if cur == prev:
            return
        self._last_device_conn_state = cur
        # Route through the same "LIFUInterface" logger that
        # OpenLIFUSonicationControl configures (Slicer status bar +
        # Python Console via stdout StreamHandler). Using the root
        # logger here would only land in the error-log table.
        lifu_logger = logging.getLogger("LIFUInterface")
        if cur == "simulated":
            lifu_logger.info("Simulated hardware device connected (no real device).")
        elif cur == (True, True):
            lifu_logger.info("Hardware device fully connected (TX + HV).")
        elif cur == (True, False):
            lifu_logger.info("Hardware device partially connected (TX only).")
        elif cur == (False, True):
            lifu_logger.info("Hardware device partially connected (HV only).")
        else:
            if iface is None:
                lifu_logger.info(
                    "Hardware device not connected "
                    "(SonicationControl interface not yet initialized)."
                )
            else:
                lifu_logger.info(
                    "Hardware device not connected. The COM ports may be "
                    "in use by another application or no device is plugged in."
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
        self._checkKwaveStatus()
        self._checkADBStatus()

        # Optional dependencies (ADB) -- always start collapsed.
        try:
            self.ui.installDependenciesCollapsibleGroupBox.collapsed = True
        except AttributeError:
            pass

        # Outer Dependencies section -- expanded when openlifu or kwave is not OK.
        openlifu_ok = python_requirements_exist() and openlifu_version_matches()
        kwave_ok = openlifu_ok and kwave_binaries_exist()
        all_ok = openlifu_ok and kwave_ok
        try:
            self.ui.dependenciesCollapsibleButton.collapsed = bool(all_ok)
            # ctkCollapsibleButton's ``checked`` mirrors ``not collapsed``
            # in the .ui file; keep them in sync defensively.
            self.ui.dependenciesCollapsibleButton.setChecked(not all_ok)
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
        self._checkKwaveStatus()

    def _checkKwaveStatus(self) -> None:
        """Update the k-Wave binaries status indicator and install button.

        k-Wave installation requires openlifu to be importable, so the
        install button is disabled until python requirements are present.
        """
        openlifu_ok = python_requirements_exist()
        kwave_ok = kwave_binaries_exist() if openlifu_ok else False

        icon_name = (
            qt.QStyle.SP_DialogApplyButton
            if kwave_ok
            else qt.QStyle.SP_DialogCancelButton
        )
        pixmap = slicer.app.style().standardIcon(icon_name).pixmap(qt.QSize(16, 16))
        self.ui.kwaveStatusIcon.setPixmap(pixmap)
        self.ui.kwaveStatusIcon.setText("")

        if not openlifu_ok:
            self.ui.installKwaveBinariesPushButton.setEnabled(False)
            self.ui.installKwaveBinariesPushButton.setText(
                "Install k-Wave Binaries (install Python requirements first)"
            )
        elif kwave_ok:
            self.ui.installKwaveBinariesPushButton.setEnabled(False)
            self.ui.installKwaveBinariesPushButton.setText("k-Wave Binaries Installed")
        else:
            self.ui.installKwaveBinariesPushButton.setEnabled(True)
            self.ui.installKwaveBinariesPushButton.setText("Install k-Wave Binaries")

    @display_errors
    def onInstallKwaveBinariesClicked(self, checked: bool = False) -> None:
        if not python_requirements_exist():
            slicer.util.errorDisplay(
                text="OpenLIFU python dependencies are not installed. Install Python requirements first.",
                windowTitle="Python requirements missing",
            )
            return
        with BusyCursor():
            check_and_install_kwave_binaries()
        self._checkKwaveStatus()

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
        per-TX-module hardware id and firmware version. The dialog also
        exposes a "Connect Simulated Device" button when no hardware is
        connected, and a "Disconnect Simulated Device" button when the
        currently active interface is the in-memory
        :class:`~openlifu_sdk.ui.simulated_interface.SimulatedLIFUInterface`.
        When the real interface could not be acquired because another
        process owns it, the dialog reports the offending PID and offers a
        Retry button. All I/O calls are defensive: any failure is reported
        inline as "unknown" rather than aborting the dialog (the SDK
        raises ``LIFUError`` on bus errors).
        """
        try:
            sc_logic = slicer.util.getModuleLogic("OpenLIFUSonicationControl")
        except (AttributeError, RuntimeError):
            sc_logic = None
        if sc_logic is None:
            slicer.util.infoDisplay(
                text=(
                    "The OpenLIFU Sonication Control module is not available, "
                    "so the LIFU hardware interface cannot be queried."
                ),
                windowTitle="Device Status",
            )
            return

        dialog = _DeviceStatusDialog(sc_logic, parent=slicer.util.mainWindow())
        dialog.exec_()

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

    @display_errors
    def onManageProtocolsClicked(self, checked: bool = False) -> None:
        """Open the Protocol manager popup.

        Requires a loaded database. The dialog presents the protocols stored
        in that database in a table and offers New / Import / Duplicate /
        Edit / Preview / Export / Delete actions.
        """
        db = get_cur_db()
        if db is None:
            slicer.util.errorDisplay(
                "A database must be loaded before the Protocol manager can be opened.",
                windowTitle="Manage Protocols",
            )
            return
        dlg = ProtocolManagerDialog(db=db, parent=slicer.util.mainWindow())
        dlg.exec_()

    def _get_active_session_ids_or_error(self, dialog_title: str) -> Optional[Tuple[str, str]]:
        """Return (subject_id, session_id) for the active session, or None and show an error."""
        db = get_cur_db()
        if db is None:
            slicer.util.errorDisplay(
                "A database must be loaded before the manager can be opened.",
                windowTitle=dialog_title,
            )
            return None
        loaded_session = self._parameterNode.loaded_session if self._parameterNode is not None else None
        if loaded_session is None:
            slicer.util.errorDisplay(
                "An active session is required before the manager can be opened.",
                windowTitle=dialog_title,
            )
            return None
        return loaded_session.get_subject_id(), loaded_session.get_session_id()

    @display_errors
    def onManagePhotoscansClicked(self, checked: bool = False) -> None:
        """Open the Photoscan manager popup for the active session."""
        ids = self._get_active_session_ids_or_error("Manage Photoscans")
        if ids is None:
            return
        subject_id, session_id = ids
        loaded_session = self._parameterNode.loaded_session
        dlg = PhotoscanManagerDialog(
            db=get_cur_db(),
            subject_id=subject_id,
            session_id=session_id,
            loaded_session=loaded_session,
            parent=slicer.util.mainWindow(),
        )
        dlg.exec_()

    @display_errors
    def onManageSolutionsClicked(self, checked: bool = False) -> None:
        """Open the Solution manager popup for the active session."""
        ids = self._get_active_session_ids_or_error("Manage Solutions")
        if ids is None:
            return
        subject_id, session_id = ids
        dlg = SolutionManagerDialog(
            db=get_cur_db(),
            subject_id=subject_id,
            session_id=session_id,
            parent=slicer.util.mainWindow(),
        )
        dlg.exec_()

    @display_errors
    def onManageRunsClicked(self, checked: bool = False) -> None:
        """Open the Run manager popup for the active session."""
        ids = self._get_active_session_ids_or_error("Manage Runs")
        if ids is None:
            return
        subject_id, session_id = ids
        dlg = RunManagerDialog(
            db=get_cur_db(),
            subject_id=subject_id,
            session_id=session_id,
            parent=slicer.util.mainWindow(),
        )
        dlg.exec_()

    def enter(self) -> None:
        """Called each time the user opens this module."""
        ensure_python_requirements_for_module_enter()
        # Admin-only when user-account-mode is on: bounce non-admin users
        # back to Home. This catches navigations via Slicer's stock module
        # selector toolbar (the Home dashboard button is already disabled
        # for non-admins in _refresh_status_rows).
        if self._is_blocked_for_non_admin():
            slicer.util.warningDisplay(
                "The Data Manager is restricted to admin users. "
                "Sign in as an admin to access it.",
                windowTitle="Admin access required",
            )
            qt.QTimer.singleShot(0, lambda: slicer.util.selectModule("OpenLIFUHome"))
            return
        # Make sure parameter node exists and observed
        self.initializeParameterNode()
        self.updateWorkflowControls()
        # The shared header repaints all three indicators (navigation,
        # permissions, status buttons) in one call.
        self.module_header.refresh_all()
        self.updatePermissionsGating()
        self._logDeviceStateTransition()
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

    def remove_subject_changed_callback(self, f : Callable[[Optional["openlifu.db.Subject"]],None]) -> None:
        """Unregister a callback previously registered via
        :py:meth:`call_on_subject_changed`. Silently no-ops if absent.
        """
        try:
            self._on_subject_changed_callbacks.remove(f)
        except ValueError:
            pass

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
                and loaded_session.session.session.solution_id == self.getParameterNode().loaded_solution.solution.solution.id
            ):
                # Don't clear the persisted session.solution_id link: we want to be able to reload
                # this session later and restore its Solution.
                self.clear_solution(clean_up_scene=True, update_session_link=False)
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

        # Eagerly load each affiliated photoscan into the scene (with visibility off by
        # default; see SlicerOpenLIFUPhotoscan.set_model_display_settings). This means
        # downstream consumers -- e.g. the photoscan manager preview, transducer
        # localization -- can assume an affiliated photoscan is already in
        # ``loaded_photoscans`` and don't need to lazily load on first click.
        already_loaded = self.getParameterNode().loaded_photoscans
        for photoscan_id, photoscan_openlifu in affiliated_photoscans.items():
            if photoscan_id in already_loaded:
                continue
            try:
                self.load_photoscan_from_openlifu(
                    photoscan_openlifu,
                    load_from_active_session=True,
                    replace_confirmed=True,
                )
            except Exception as e:
                logging.warning(
                    "Could not eagerly load affiliated photoscan '%s' into the scene: %s",
                    photoscan_id, e,
                )

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

        # === Restore previously computed Solution + analysis (if any) ===
        # session.solution_id is persisted and cleared whenever the array_transform changes, so if it's
        # set here we know the on-disk Solution is still consistent with the current transducer pose.
        if session_openlifu.solution_id:
            self._restore_solution_for_loaded_session(
                session_openlifu = session_openlifu,
                transducer = newly_loaded_transducer,
            )

        self.session_loading_unloading_in_progress = False  

    def _restore_solution_for_loaded_session(
        self,
        session_openlifu: "openlifu.db.Session",
        transducer: SlicerOpenLIFUTransducer,
    ) -> None:
        """Load the Solution (and analysis) linked to a freshly-loaded session into the scene.

        On any failure (missing files, etc.) we warn but otherwise let session loading proceed; the
        ``session.solution_id`` is left alone so the user can investigate.
        """
        db = get_cur_db()
        if db is None:
            return
        solution_id = session_openlifu.solution_id
        try:
            solution_openlifu = db.load_solution(session_openlifu, solution_id)
        except FileNotFoundError:
            slicer.util.warningDisplay(
                f"Session is linked to solution '{solution_id}' but the solution files were not found"
                " in the database. The session will be loaded without restoring the solution.",
                "Solution not found",
            )
            return

        slicer_solution = SlicerOpenLIFUSolution.initialize_from_loaded_openlifu_solution(
            solution=solution_openlifu,
            transducer=transducer,
        )
        # Make sure the restored pnp/intensity volumes follow the transducer's transform so they render
        # in the correct pose (otherwise they sit at the world origin).
        transducer_transform_id = transducer.transform_node.GetID()
        slicer_solution.pnp.SetAndObserveTransformNodeID(transducer_transform_id)
        slicer_solution.intensity.SetAndObserveTransformNodeID(transducer_transform_id)
        # write_to_db=False: we just loaded the solution from disk, nothing new to write.
        self.set_solution(slicer_solution, write_to_db=False)

        planner_logic = slicer.util.getModuleLogic('OpenLIFUSonicationPlanner')
        analysis_openlifu = None
        try:
            analysis_openlifu = db.load_solution_analysis(session_openlifu, solution_id)
        except FileNotFoundError:
            pass  # Older databases may not have the analysis persisted; recompute below.
        if analysis_openlifu is not None:
            slicer_analysis = SlicerOpenLIFUSolutionAnalysis(analysis_openlifu)
        else:
            slicer_analysis = planner_logic.compute_analysis_from_solution(slicer_solution)
        if slicer_analysis is not None:
            planner_logic.getParameterNode().solution_analysis = slicer_analysis

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

    def set_solution(
        self,
        solution: SlicerOpenLIFUSolution,
        analysis: "Optional[SlicerOpenLIFUSolutionAnalysis]" = None,
        write_to_db: bool = True,
    ):
        """Set a solution to be the currently active solution.

        If there is an active session and ``write_to_db`` is True, persist the solution (and analysis, if
        supplied) to the database and link the session to it by updating ``session.solution_id``. Pass
        ``write_to_db=False`` when the solution was just loaded from disk and there is nothing new to write.

        Args:
            solution: The solution to set as active.
            analysis: Optional analysis to persist alongside the solution. Only written when ``write_to_db``
                is True and there is an active session.
            write_to_db: Whether to write the solution (and analysis) and the updated session to the database.
        """
        self.getParameterNode().loaded_solution = solution
        if not write_to_db:
            return
        if self.validate_session():
            if get_cur_db() is None: # This should not happen -- if there is an active session then there should be a database connection as well.
                raise RuntimeError("Unable to write solution to the session because there is no database connection")
            loaded_session = self.getParameterNode().loaded_session
            session_openlifu = loaded_session.session.session
            solution_openlifu = solution.solution.solution
            import openlifu.db.database
            OnConflictOpts = openlifu.db.database.OnConflictOpts
            get_cur_db().write_solution(session_openlifu, solution_openlifu)
            if analysis is not None:
                get_cur_db().write_solution_analysis(
                    session_openlifu,
                    solution_openlifu.id,
                    analysis.analysis,
                    on_conflict=OnConflictOpts.OVERWRITE,
                )
            # Link the session to this solution and persist that link.
            session_openlifu.solution_id = solution_openlifu.id
            # Write the pack back so the parameterNode's serialized JSON reflects the new solution_id;
            # otherwise subsequent reads (save_session, clear_session) would see the stale "".
            self.getParameterNode().loaded_session = loaded_session
            get_cur_db().write_session(self.subject, session_openlifu, on_conflict=OnConflictOpts.OVERWRITE)


    def clear_solution(self, clean_up_scene: bool = True, update_session_link: bool = True) -> None:
        """Unload the current solution if there is one loaded.

        Args:
            clean_up_scene: Whether to remove the solution's affiliated scene content.
                If False then the scene content is orphaned from its session.
                If True then the scene content is removed.
            update_session_link: When True, and the currently active session is linked to the solution
                being cleared (via ``session.solution_id``), clear that link and persist the session.
                Pass False when the session itself is being unloaded -- we want the persisted
                ``solution_id`` to remain on disk so the Solution can be restored next time the session is
                loaded.
        """
        solution = self.getParameterNode().loaded_solution
        self.getParameterNode().loaded_solution = None
        if solution is None:
            return
        if update_session_link:
            loaded_session = self.getParameterNode().loaded_session
            if (
                loaded_session is not None
                and get_cur_db() is not None
                and loaded_session.session.session.solution_id == solution.solution.solution.id
            ):
                import openlifu.db.database
                OnConflictOpts = openlifu.db.database.OnConflictOpts
                loaded_session.session.session.solution_id = ""
                # Write the pack back so the in-memory parameterNode JSON reflects the cleared link.
                self.getParameterNode().loaded_session = loaded_session
                get_cur_db().write_session(
                    self.subject,
                    loaded_session.session.session,
                    on_conflict=OnConflictOpts.OVERWRITE,
                )
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
            if session.session.session.solution_id == solution.solution.solution.id:
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
