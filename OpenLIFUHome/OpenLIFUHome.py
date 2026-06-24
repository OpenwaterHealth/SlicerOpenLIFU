# Standard library imports
from typing import Optional

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
    check_and_install_kwave_binaries,
    ensure_python_requirements_for_module_enter,
    get_cur_db,
)
from OpenLIFULib.guided_mode_util import set_guided_mode_state, Workflow
from OpenLIFULib.kiosk_util import (
    get_require_login_on_home,
)
from OpenLIFULib.module_layout import apply_module_layout, wire_passive_module_header
from OpenLIFULib.user_account_mode_util import get_current_user, get_user_account_mode_state, set_user_account_mode_state
from OpenLIFULib.util import (
    cleanup_module_callbacks,
    display_errors,
    register_module_callback,
)
from OpenLIFUCloudSync import getCloudSyncLogic

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
        self.parent.contributors = ["Ebrahim Ebrahim (Kitware), Sadhana Ravikumar (Kitware), Peter Hollender (Openwater), Sam Horvath (Kitware), Brad Moore (Kitware)"]
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
# OpenLIFUHomeParameterNode
#


@parameterNodeWrapper
class OpenLIFUHomeParameterNode:
    guided_mode : bool = False

#
# OpenLIFUHomeWidget
#


class _StatusLabelClickFilter(qt.QObject):
    """Event filter that turns a QLabel into a clickable hyperlink-style trigger.

    Used by the Home status rows so the Database / User / Transducer value
    labels open their respective popup dialogs (same as the header icons).
    """

    def __init__(self, parent: qt.QObject, callback) -> None:
        super().__init__(parent)
        self._callback = callback

    def eventFilter(self, watched, event):  # noqa: N802 (Qt naming)
        if event.type() == qt.QEvent.MouseButtonRelease:
            try:
                self._callback()
            except Exception:  # noqa: BLE001
                pass
            return True
        return False


class OpenLIFUHomeWidget(ScriptedLoadableModuleWidget, VTKObservationMixin):
    """Operator-focused landing page.

    Shows live status for the database, signed-in user, and connected
    hardware, plus three primary actions (New Session, Load Session, Sign In)
    and a Data Manager entry point for power users.
    """

    def __init__(self, parent=None) -> None:
        """Called when the user opens the module the first time and the widget is initialized."""
        ScriptedLoadableModuleWidget.__init__(self, parent)
        VTKObservationMixin.__init__(self)  # needed for parameter node observation
        self.logic: Optional["OpenLIFUHomeLogic"] = None
        self._parameterNode = None
        self._parameterNodeGuiTag = None

    def setup(self) -> None:
        """Called when the user opens the module the first time and the widget is initialized."""
        ScriptedLoadableModuleWidget.setup(self)

        # Load widget from .ui file (created by Qt Designer).
        uiWidget = slicer.util.loadUI(self.resourcePath("UI/OpenLIFUHome.ui"))
        self.layout.addWidget(uiWidget)
        self.uiWidget = uiWidget
        self.ui = slicer.util.childWidgetVariables(uiWidget)

        # Shared status header (read-only on Home; the Data Manager owns the
        # interactive header). Home does not use a workflow-controls footer,
        # so apply_module_layout simply inserts the header above the body.
        # Home opts in to keep the sign-in icon live in the header so the
        # operator can sign in / out without leaving the landing page.
        self.module_header = apply_module_layout(
            uiWidget,
            ui_namespace=self.ui,
            header_read_only=True,
            keep_login_button_active=True,
        )

        uiWidget.setMRMLScene(slicer.mrmlScene)

        self.logic = OpenLIFUHomeLogic()

        # The standalone OpenLIFUCloudSync module has been deprecated -- its
        # controls live in the Database popup on the Data page. We still
        # bootstrap the singleton logic here so the background sync engine
        # can autostart on boot when the user has it enabled.
        getCloudSyncLogic()

        # Note: we deliberately do NOT force-instantiate the OpenLIFUData
        # widget here. Doing so during setup() triggers Data's setup,
        # whose deferred Login wiring eventually calls
        # ``OpenLIFULoginWidget.cacheAllLoginRelatedWidgets`` which walks
        # ``getModule("OpenLIFUHome").widgetRepresentation()`` -- if Home's
        # setup() has not yet returned, that re-enters Home and recurses
        # without bound. Instead, ``_ensure_data_widget()`` lazily builds
        # the Data widget the first time a button handler needs it.

        # These connections ensure that we update parameter node when scene is closed
        self.addObserver(slicer.mrmlScene, slicer.mrmlScene.StartCloseEvent, self.onSceneStartClose)
        self.addObserver(slicer.mrmlScene, slicer.mrmlScene.EndCloseEvent, self.onSceneEndClose)
        # Make sure parameter node is initialized (needed for module reload)
        self.initializeParameterNode()

        # Header status-icon observers (DB / login / device / cloud / nav / perms).
        wire_passive_module_header(self, self.module_header)

        # Buttons.
        self.ui.newSessionPushButton.clicked.connect(self.on_new_session_clicked)
        self.ui.loadSessionPushButton.clicked.connect(self.on_load_session_clicked)
        self.ui.dataManagerPushButton.clicked.connect(self.on_data_manager_clicked)

        # Status rows: clicking on a value label opens the matching popup
        # (database / sign-in / device) -- same dialogs as the header icons.
        self._install_status_label_click_handlers()

        # Status-row observers and initial paint are deferred one tick so the
        # OpenLIFUDatabase / OpenLIFULogin / OpenLIFUSonicationControl logics
        # are constructed before we touch them.
        qt.QTimer.singleShot(0, self._wire_status_row_observers)
        qt.QTimer.singleShot(0, self._refresh_status_rows)

        # Apply env-var-driven mode locks (OPENLIFU_GUIDED_MODE +
        # OPENLIFU_REQUIRE_LOGIN implies user-account mode). Deferred so the
        # Login parameter node exists before we touch it.
        qt.QTimer.singleShot(0, self._apply_env_locked_modes)

    def cleanup(self) -> None:
        """Called when the application closes and the module widget is destroyed."""
        cleanup_module_callbacks(self)
        self.removeObservers()

        # Legacy: an older build of this module installed a "CloudSyncToolBar"
        # that navigated to the now-deprecated OpenLIFUCloudSync module.
        # Remove it on cleanup if it is still present from a previous launch.
        # ``findChild`` raises when the widget is absent, so use the list
        # variant instead.
        mw = slicer.util.mainWindow()
        for toolBar in slicer.util.findChildren(mw, "CloudSyncToolBar"):
            mw.removeToolBar(toolBar)
            toolBar.deleteLater()

    def enter(self) -> None:
        """Called each time the user opens this module."""
        ensure_python_requirements_for_module_enter()
        self.initializeParameterNode()
        self._apply_env_locked_modes()
        self._refresh_status_rows()

    def exit(self) -> None:
        """Called each time the user opens a different module."""
        if self._parameterNode:
            self._parameterNode.disconnectGui(self._parameterNodeGuiTag)
            self._parameterNodeGuiTag = None

    def onSceneStartClose(self, caller, event) -> None:
        """Called just before the scene is closed."""
        self.setParameterNode(None)

    def onSceneEndClose(self, caller, event) -> None:
        """Called just after the scene is closed."""
        if self.parent.isEntered:
            self.initializeParameterNode()
            self._refresh_status_rows()

    def initializeParameterNode(self) -> None:
        """Ensure parameter node exists and observed."""
        self.setParameterNode(self.logic.getParameterNode())

    def setParameterNode(self, inputParameterNode: Optional[OpenLIFUHomeParameterNode]) -> None:
        """Set and observe parameter node."""
        if self._parameterNode:
            self._parameterNode.disconnectGui(self._parameterNodeGuiTag)
        self._parameterNode = inputParameterNode
        if self._parameterNode:
            self._parameterNodeGuiTag = self._parameterNode.connectGui(self.ui)
            self.addObserver(self._parameterNode, vtk.vtkCommand.ModifiedEvent, self.onParameterNodeModified)

    def onParameterNodeModified(self, caller, event) -> None:
        if self._parameterNode is not None:
            self.logic.workflow.enforceGuidedModeVisibility(self._parameterNode.guided_mode)

        # Refresh transducer-localization enable/disable when guided_mode
        # changes. Only touch the TL widget if it already exists -- forcing
        # widgetRepresentation() to instantiate it from inside
        # onParameterNodeModified (which may fire during Home's own setup
        # via initializeParameterNode/connectGui) can recurse via the
        # Login cacheAllLoginRelatedWidgets walk.
        tl_widget = getattr(slicer.modules, "OpenLIFUTransducerLocalizationWidget", None)
        if tl_widget is not None:
            try:
                tl_widget.checkCanRunTracking()
            except Exception:  # noqa: BLE001
                pass

    # ------------------------------------------------------------------
    # Status row wiring + refresh
    # ------------------------------------------------------------------

    def _wire_status_row_observers(self) -> None:
        """Hook events that should trigger a status-row refresh."""
        try:
            db_logic = slicer.util.getModuleLogic("OpenLIFUDatabase")
            register_module_callback(
                self,
                db_logic.call_on_db_changed,
                db_logic.remove_db_changed_callback,
                lambda *_a, **_kw: self._refresh_status_rows(),
            )
        except Exception:  # noqa: BLE001
            pass
        try:
            login_logic = slicer.util.getModuleLogic("OpenLIFULogin")
            register_module_callback(
                self,
                login_logic.call_on_active_user_changed,
                login_logic.remove_active_user_changed_callback,
                lambda *_a, **_kw: self._refresh_status_rows(),
            )
        except Exception:  # noqa: BLE001
            pass
        try:
            login_pn = slicer.util.getModuleLogic("OpenLIFULogin").getParameterNode()
            self.addObserver(
                login_pn,
                vtk.vtkCommand.ModifiedEvent,
                lambda caller, event: self._refresh_status_rows(),
            )
        except Exception:  # noqa: BLE001
            pass
        try:
            sc_logic = slicer.util.getModuleLogic("OpenLIFUSonicationControl")
            register_module_callback(
                self,
                sc_logic.call_on_lifu_device_connected,
                sc_logic.remove_callback,
                lambda *_a, **_kw: self._refresh_status_rows(),
            )
            register_module_callback(
                self,
                sc_logic.call_on_lifu_device_disconnected,
                sc_logic.remove_callback,
                lambda *_a, **_kw: self._refresh_status_rows(),
            )
        except Exception:  # noqa: BLE001
            pass

    def _apply_env_locked_modes(self) -> None:
        """Push the env-driven user-mode value into its live parameter nodes.

        ``OPENLIFU_USER_MODE`` (and its legacy aliases) controls all three of
        require-login / user-account-mode / guided-mode together. Re-runs each
        time Home is shown (cheap; idempotent) so a transient scene clear
        cannot leave the modes in an inconsistent state.
        """
        from OpenLIFULib.kiosk_util import get_user_mode
        target = bool(get_user_mode())
        try:
            set_guided_mode_state(target)
        except Exception:  # noqa: BLE001
            pass
        try:
            set_user_account_mode_state(target)
        except Exception:  # noqa: BLE001
            pass

    def _refresh_status_rows(self) -> None:
        """Repaint the three status rows and gate the primary buttons."""
        # ---- Database row ----
        try:
            db = get_cur_db()
        except Exception:  # noqa: BLE001
            db = None
        if db is not None:
            db_path = getattr(db, "path", None) or "(unknown location)"
            self.ui.databaseStatusValueLabel.setText(f"Connected: {db_path}")
        else:
            self.ui.databaseStatusValueLabel.setText("Not connected")

        # ---- User row ----
        try:
            cur_user = get_current_user()
        except Exception:  # noqa: BLE001
            cur_user = None
        try:
            uam = bool(get_user_account_mode_state())
        except Exception:  # noqa: BLE001
            uam = False
        user_id = getattr(cur_user, "id", None)
        is_real_user = (
            cur_user is not None
            and user_id not in (None, "anonymous", "default_admin")
        )
        if is_real_user:
            who = getattr(cur_user, "name", "") or user_id
            roles = list(getattr(cur_user, "roles", None) or [])
            role_str = f" ({', '.join(roles)})" if roles else ""
            self.ui.userStatusValueLabel.setText(f"Signed in as {who}{role_str}")
        else:
            self.ui.userStatusValueLabel.setText("Not signed in")

        # ---- Transducer row ----
        try:
            sc_logic = slicer.util.getModuleLogic("OpenLIFUSonicationControl")
            iface = getattr(sc_logic, "cur_lifu_interface", None)
            tx_conn, hv_conn = (
                iface.is_device_connected() if iface is not None else (False, False)
            )
            is_simulated = bool(getattr(iface, "is_simulated", False))
        except Exception:  # noqa: BLE001
            iface = None
            tx_conn = hv_conn = False
            is_simulated = False
        if iface is None:
            self.ui.transducerStatusValueLabel.setText("Not connected")
        elif is_simulated:
            self.ui.transducerStatusValueLabel.setText("Simulated (no real device)")
        elif tx_conn and hv_conn:
            self.ui.transducerStatusValueLabel.setText("Connected (TX + HV)")
        elif tx_conn:
            self.ui.transducerStatusValueLabel.setText("Partially connected (TX only)")
        elif hv_conn:
            self.ui.transducerStatusValueLabel.setText("Partially connected (HV only)")
        else:
            self.ui.transducerStatusValueLabel.setText("Not connected")

        # ---- Primary buttons gating ----
        # Soft-kiosk: when "require sign-in on Home" is effective and no real
        # user is signed in, lock down everything except the Sign In button.
        require_login = get_require_login_on_home()
        kiosk_locked = require_login and not is_real_user

        # New / Load Session require a connected database AND (a signed-in
        # real user when user-account-mode or kiosk lock is in effect).
        sessions_enabled = (
            db is not None
            and (not uam or is_real_user)
            and not kiosk_locked
        )
        self.ui.newSessionPushButton.setEnabled(sessions_enabled)
        self.ui.loadSessionPushButton.setEnabled(sessions_enabled)
        if db is None:
            gate_tip = "Connect a database in the Data Manager first."
        elif kiosk_locked:
            gate_tip = "Sign in to start or load a session (sign-in is required on this system)."
        elif uam and not is_real_user:
            gate_tip = "Sign in to start or load a session."
        else:
            gate_tip = None
        if gate_tip:
            self.ui.newSessionPushButton.setToolTip(gate_tip)
            self.ui.loadSessionPushButton.setToolTip(gate_tip)
        else:
            self.ui.newSessionPushButton.setToolTip(
                "Pick or add a subject, then create a new treatment session."
            )
            self.ui.loadSessionPushButton.setToolTip(
                "Pick a subject and load an existing session."
            )

        # Data Manager is admin-only when user-account-mode is on:
        # * kiosk-locked (sign-in required but no real user) -> disabled
        # * UAM on and signed-in user is not an admin -> disabled
        # * Otherwise -> enabled
        user_roles = list(getattr(cur_user, "roles", None) or [])
        is_admin = is_real_user and "admin" in user_roles
        if kiosk_locked:
            data_mgr_enabled = False
            data_mgr_tip = (
                "Sign in to access the Data Manager (sign-in is required on this system)."
            )
        elif uam and not is_admin:
            data_mgr_enabled = False
            data_mgr_tip = (
                "The Data Manager is restricted to admin users. Sign in as an admin to access it."
            )
        else:
            data_mgr_enabled = True
            data_mgr_tip = (
                "Open the Data Manager (power-user view of subjects, sessions, "
                "protocols, transducers, and the database)."
            )
        self.ui.dataManagerPushButton.setEnabled(data_mgr_enabled)
        self.ui.dataManagerPushButton.setToolTip(data_mgr_tip)

    # ------------------------------------------------------------------
    # Button handlers
    # ------------------------------------------------------------------

    def _ensure_data_widget(self):
        """Instantiate the OpenLIFUData widget on first use and return it.

        Called from button handlers (never from setup()) so the cascading
        Data->Login->cacheAllLoginRelatedWidgets walk never re-enters
        Home's own setup().
        """
        slicer.util.getModule("OpenLIFUData").widgetRepresentation()
        return slicer.modules.OpenLIFUDataWidget

    @display_errors
    def on_sign_in_clicked(self, checked: bool = False) -> None:
        from OpenLIFUData import _ModuleWidgetPopupDialog
        dlg = _ModuleWidgetPopupDialog(
            "OpenLIFULogin", "Account", parent=slicer.util.mainWindow()
        )
        dlg.exec_()
        self._refresh_status_rows()

    @display_errors
    def on_database_label_clicked(self) -> None:
        from OpenLIFUData import _ModuleWidgetPopupDialog
        dlg = _ModuleWidgetPopupDialog(
            "OpenLIFUDatabase", "Database", parent=slicer.util.mainWindow()
        )
        dlg.exec_()
        self._refresh_status_rows()

    @display_errors
    def on_transducer_label_clicked(self) -> None:
        data_widget = self._ensure_data_widget()
        handler = getattr(data_widget, "onOpenDevicePopup", None)
        if handler is not None:
            handler()
        self._refresh_status_rows()

    def _install_status_label_click_handlers(self) -> None:
        """Make the three Home status value labels clickable.

        Each label opens the same popup dialog that the corresponding header
        icon would: Database / Sign-in / Device. Tooltips (set in the .ui)
        are the only affordance hint; the labels render as plain text.
        """
        cursor = qt.QCursor(qt.Qt.PointingHandCursor)
        self._status_label_click_filters = []
        targets = [
            (self.ui.databaseStatusValueLabel, self.on_database_label_clicked),
            (self.ui.userStatusValueLabel, self.on_sign_in_clicked),
            (self.ui.transducerStatusValueLabel, self.on_transducer_label_clicked),
        ]
        for label, handler in targets:
            label.setCursor(cursor)
            f = _StatusLabelClickFilter(label, handler)
            label.installEventFilter(f)
            self._status_label_click_filters.append(f)

        grid = getattr(self.ui, "statusGridLayout", None)
        if grid is not None:
            grid.setColumnStretch(0, 1)
            grid.setColumnStretch(1, 4)

    @display_errors
    def on_new_session_clicked(self, checked: bool) -> None:
        db = get_cur_db()
        if db is None:
            slicer.util.errorDisplay("Connect a database in the Data Manager first.")
            return

        # Make sure Data's widget (and therefore its dialog classes) is
        # available before we pop the subject picker.
        self._ensure_data_widget()

        # Step 1: subject pick (reuses Data's LoadSubjectDialog, which also
        # exposes "Add Subject" so the user can create a subject on the fly).
        from OpenLIFUData import CreateNewSessionDialog, LoadSubjectDialog
        subject_dlg = LoadSubjectDialog(db)
        subject = subject_dlg.exec_and_get_subject()
        if subject is None:
            return
        subject_id = subject.id

        # Step 2: protocol-filtered create-session dialog.
        protocol_ids = db.get_protocol_ids()
        if get_user_account_mode_state():
            cur_user = get_current_user()
            user_roles = list(getattr(cur_user, "roles", None) or [])
            if "admin" not in user_roles:
                protocols = db.load_all_protocols()
                protocol_ids = [
                    p.id for p in protocols
                    if any(r in p.allowed_roles for r in user_roles)
                ]

        sessiondlg = CreateNewSessionDialog(
            transducer_ids=db.get_transducer_ids(),
            protocol_ids=protocol_ids,
            volume_ids=db.get_volume_ids(subject_id),
            subject_id=subject_id,
        )
        returncode, session_parameters, load_checked = sessiondlg.customexec_()
        if not returncode:
            return

        data_logic = slicer.util.getModuleLogic("OpenLIFUData")
        data_logic.add_session_to_database(subject_id, session_parameters)

        if load_checked:
            data_logic.clear_session(clean_up_scene=True)
            data_logic.load_session(subject_id, session_parameters["id"])
            set_guided_mode_state(True)
            slicer.util.selectModule("OpenLIFUSession")

    @display_errors
    def on_load_session_clicked(self, checked: bool) -> None:
        db = get_cur_db()
        if db is None:
            slicer.util.errorDisplay("Connect a database in the Data Manager first.")
            return

        # Reuse the Data widget's existing load dialogs so behaviour stays in
        # lockstep with the Data Manager's own Load buttons.
        data_widget = self._ensure_data_widget()
        if not data_widget.on_load_subject_clicked(True):
            return
        if not data_widget.on_load_session_clicked(True):
            return
        set_guided_mode_state(True)
        slicer.util.selectModule("OpenLIFUSession")

    @display_errors
    def on_data_manager_clicked(self, checked: bool) -> None:
        slicer.util.selectModule("OpenLIFUData")


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

        self.workflow = Workflow()

    def getParameterNode(self):
        return OpenLIFUHomeParameterNode(super().getParameterNode())

    def clear_session(self) -> None:
        self.current_session = None

    def start_guided_mode(self):
        set_guided_mode_state(True)
        self.workflow_go_to_start()

    def workflow_jump_ahead(self):
        """Jump ahead in the guided workflow to the furthest step for which `can_proceed` is True."""
        slicer.util.selectModule(self.workflow.furthest_module_to_which_can_proceed())

    def workflow_go_to_start(self):
        """Go to the starting module of the workflow"""
        slicer.util.selectModule(self.workflow.starting_module())

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

    def _ensure_dvc_gdrive_support(self):
        
        import importlib.util

        # Check if dvc is installed with gdrive support
        dvc_installed = importlib.util.find_spec("dvc") is not None
        gdrive_installed = importlib.util.find_spec("pydrive2") is not None

        if not dvc_installed or not gdrive_installed:
            slicer.util.pip_install("dvc[gdrive]")

    def get_test_database(self):
        """
        Downloads the test database from Google Drive via DVC.
    
        Setup Requirements:
            - DVC with Google Drive support must be installed in the Slicer environment.
            - The path to a Service Account JSON key must be provided via the 
            DVC_GDRIVE_KEY_PATH CMake variable during configuration.
        
        Authentication Flow:
            At configuration time, CMake reads the key file's content into the 
            GDRIVE_CREDENTIALS_DATA environment variable. This allows DVC to 
            authenticate headlessly during runtime without requiring a manual browser login.
        """

        self._ensure_dvc_gdrive_support()
        import os
        from pathlib import Path
        from dvc.repo import Repo

        dvc_repo_path = os.environ.get('DVC_REPO_DIR')
        if not dvc_repo_path:
            raise EnvironmentError("DVC_REPO_DIR environment variable is not set." )
    
        dvc_repo_path = Path(dvc_repo_path)
        dvc_file = dvc_repo_path / 'db_dvc_slicertesting.dvc'
        dvc_config_file = dvc_repo_path / '.dvc' / 'config'
        
        assert dvc_config_file.exists() and dvc_file.exists(), f"DVC file not found at expected location: {dvc_file}"

        try: 
            creds = os.environ.get('GDRIVE_CREDENTIALS_DATA')
            if not creds:
                raise EnvironmentError("GDRIVE_CREDENTIALS_DATA environment variable is not set." \
                " DVC cannot authenticate with Google Drive.")
            # Point to directory containing .dvc files
            # unitialized=True allows working in a directory that is not a git repo
            repo = Repo(str(dvc_repo_path), uninitialized=True)
            repo.pull(targets=[str(dvc_file)], force=True)
        except Exception as e:
            raise RuntimeError(f"An error occurred during dvc pull: {e}") from e
        
        return str(dvc_repo_path / 'db_dvc_slicertesting')
    
    def runTest(self):
        """Run as few or as many tests as needed here."""
        
        ensure_python_requirements_for_module_enter()
        check_and_install_kwave_binaries()

        self.setUp()
        
        # Download test database using dvc
        db_path = self.get_test_database()

        self._OpenLIFU_FullTest1(db_path = db_path)
            
    def _OpenLIFU_FullTest1(self, db_path:str) -> None:

        from OpenLIFUDatabase import OpenLIFUDatabaseTest
        dbt = OpenLIFUDatabaseTest()
        dbt.connect_database(database_dir = db_path)

        from OpenLIFUData import OpenLIFUDataTest
        dt = OpenLIFUDataTest()
        dt.load_subject_session()

        from OpenLIFUSession import OpenLIFUSessionTest
        st = OpenLIFUSessionTest()
        st.workflow_session_dashboard()

        from OpenLIFUPrePlanning import OpenLIFUPrePlanningTest
        pt = OpenLIFUPrePlanningTest()
        pt._workflow_virtual_fit()

        from OpenLIFUTransducerLocalization import OpenLIFUTransducerLocalizationTest
        tlt = OpenLIFUTransducerLocalizationTest()
        tlt._workflow_localization()

        from OpenLIFUSonicationPlanner import OpenLIFUSonicationPlannerTest
        spt = OpenLIFUSonicationPlannerTest()
        spt._workflow_planning()

        from OpenLIFUSonicationControl import OpenLIFUSonicationControlTest
        sct = OpenLIFUSonicationControlTest()
        sct._workflow_sonication_control()
