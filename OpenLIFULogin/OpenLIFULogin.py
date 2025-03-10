from typing import Optional, List, TYPE_CHECKING
import json
from enum import Enum

from OpenLIFULib.user_account_mode_util import set_user_account_mode_state
import qt

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
    bcrypt_lz,
    SlicerOpenLIFUUser,
    get_openlifu_data_parameter_node,
)

from OpenLIFULib.util import (
    display_errors,
)

if TYPE_CHECKING:
    import openlifu # This import is deferred at runtime using openlifu_lz, but it is done here for IDE and static analysis purposes
    import openlifu.db

all_modules = [
            "OpenLIFUData",
            "OpenLIFUHome",
            "OpenLIFUPrePlanning",
            "OpenLIFUProtocolConfig",
            "OpenLIFUSonicationControl",
            "OpenLIFUSonicationPlanner",
            "OpenLIFUTransducerTracker",
        ]

#
# OpenLIFULogin
#

all_openlifu_modules = [
            "OpenLIFUData",
            "OpenLIFUHome",
            "OpenLIFUPrePlanning",
            "OpenLIFUProtocolConfig",
            "OpenLIFUSonicationControl",
            "OpenLIFUSonicationPlanner",
            "OpenLIFUTransducerTracker",
        ]

class OpenLIFULogin(ScriptedLoadableModule):
    """Uses ScriptedLoadableModule base class, available at:
    https://github.com/Slicer/Slicer/blob/main/Base/Python/slicer/ScriptedLoadableModule.py
    """

    def __init__(self, parent):
        ScriptedLoadableModule.__init__(self, parent)
        self.parent.title = _("OpenLIFU Login")  # TODO: make this more human readable by adding spaces
        self.parent.categories = [translate("qSlicerAbstractCoreModule", "OpenLIFU.OpenLIFU Modules")]
        self.parent.dependencies = all_openlifu_modules  # add here list of module names that this module requires
        self.parent.contributors = ["Andrew Howe (Kitware), Ebrahim Ebrahim (Kitware), Sadhana Ravikumar (Kitware), Peter Hollender (Openwater), Sam Horvath (Kitware), Brad Moore (Kitware)"]
        # short description of the module and a link to online module documentation
        # _() function marks text as translatable to other languages
        self.parent.helpText = _(
            "This is the login module of the OpenLIFU extension for focused ultrasound. "
            "More information at <a href=\"https://github.com/OpenwaterHealth/SlicerOpenLIFU\">github.com/OpenwaterHealth/SlicerOpenLIFU</a>."
        )
        # organization, grant, and thanks
        self.parent.acknowledgementText = _(
            "This is part of Openwater's OpenLIFU, an open-source "
            "hardware and software platform for Low Intensity Focused Ultrasound (LIFU) research "
            "and development."
        )

class LoginState(Enum):
    NOT_LOGGED_IN=0
    UNSUCCESSFUL_LOGIN=1
    LOGGED_IN=2
    DEFAULT_ADMIN=3

#
# OpenLIFULoginParameterNode
#

@parameterNodeWrapper
class OpenLIFULoginParameterNode:
    user_account_mode : bool
    
#
# OpenLIFULoginDialogs
#

class UsernamePasswordDialog(qt.QDialog):
    """ Login with Username and Password dialog """

    def __init__(self, parent="mainWindow"):
        super().__init__(slicer.util.mainWindow() if parent == "mainWindow" else parent)
        self.setWindowTitle("Login credentials")
        self.setWindowModality(qt.Qt.ApplicationModal)
        self.setup()

        self.password_hash = None

    def setup(self):

        self.setMinimumWidth(200)

        formLayout = qt.QFormLayout()
        self.setLayout(formLayout)

        self.username = qt.QLineEdit()
        formLayout.addRow(_("Username:"), self.username)

        self.password = qt.QLineEdit()
        self.password.setEchoMode(qt.QLineEdit.Password)
        formLayout.addRow(_("Password:"), self.password)

        self.buttonBox = qt.QDialogButtonBox()
        self.buttonBox.setStandardButtons(qt.QDialogButtonBox.Ok |
                                          qt.QDialogButtonBox.Cancel)
        formLayout.addWidget(self.buttonBox)

        self.buttonBox.rejected.connect(self.reject)
        self.buttonBox.accepted.connect(self.accept)

    def customexec_(self):

        returncode = self.exec_()
        if returncode == qt.QDialog.Accepted:
            id = self.username.text
            password_text = self.password.text
            # TODO: We should hash passwords with bcrypt.hashpw() and the gensalt, but then use checkpw against the stored hash
            # TODO: This will be implemented with the ability to create new passwords, issue #173
            # salt = bcrypt_lz().gensalt()
            # password_hash = bcrypt_lz().hashpw(password_text, salt).decode('utf-8')  # convert bytestring back to string
            return (returncode, id, password_text)
        return (returncode, None, None)

#
# OpenLIFULoginWidget
#

class OpenLIFULoginWidget(ScriptedLoadableModuleWidget, VTKObservationMixin):
    """Uses ScriptedLoadableModuleWidget base class, available at:
    https://github.com/Slicer/Slicer/blob/main/Base/Python/slicer/ScriptedLoadableModule.py
    """

    def __init__(self, parent=None) -> None:
        """Called when the user opens the module the first time and the widget is initialized."""
        ScriptedLoadableModuleWidget.__init__(self, parent)
        VTKObservationMixin.__init__(self)  # needed for parameter node observation
        self.logic = None
        self._cur_login_state = LoginState.NOT_LOGGED_IN
        self._cur_user_id_enforced : str = ""  # for caching enforced permissions
        self._permissions_widgets : List[qt.QWidget] = []
        self._parameterNode = None
        self._parameterNodeGuiTag = None
        self._default_anonymous_user = SlicerOpenLIFUUser(openlifu_lz().db.User(
                id = "anonymous", 
                password_hash = "",
                roles = [],
                name = "Anonymous",
                description = "This is the default role set when the app opens, without anyone logged in, and when user account mode is deactivated. It has no roles, and therefore is the most restricted."
        ))

    def setup(self) -> None:
        """Called when the user opens the module the first time and the widget is initialized."""
        ScriptedLoadableModuleWidget.setup(self)

        # Load widget from .ui file (created by Qt Designer).
        # Additional widgets can be instantiated manually and added to self.layout.
        uiWidget = slicer.util.loadUI(self.resourcePath("UI/OpenLIFULogin.ui"))
        self.layout.addWidget(uiWidget)
        self.ui = slicer.util.childWidgetVariables(uiWidget)

        # Set scene in MRML widgets. Make sure that in Qt designer the top-level qMRMLWidget's
        # "mrmlSceneChanged(vtkMRMLScene*)" signal in is connected to each MRML widget's.
        # "setMRMLScene(vtkMRMLScene*)" slot.
        uiWidget.setMRMLScene(slicer.mrmlScene)

        # Create logic class. Logic implements all computations that should be possible to run
        # in batch mode, without a graphical user interface.
        self.logic = OpenLIFULoginLogic()

        # === Connections and UI setup =======

        # Connect to the data parameter node for updates related to database

        self.addObserver(get_openlifu_data_parameter_node().parameterNode, vtk.vtkCommand.ModifiedEvent, self.onDataParameterNodeModified)

        # Connect the buttons

        self.ui.userAccountModePushButton.clicked.connect(self.onUserAccountModeClicked)
        self.ui.loginLogoutButton.clicked.connect(self.onLoginLogoutClicked)

        # ====================================
        
        # Make sure parameter node is initialized (needed for module reload)
        self.initializeParameterNode()

        self.updateUserAccountModeButton()
        self.updateWidgetLoginState(LoginState.NOT_LOGGED_IN)
        self.logic.active_user = self._default_anonymous_user

        self.cacheAllPermissionswidgets()

    def cleanup(self) -> None:
        """Called when the application closes and the module widget is destroyed."""
        self.removeObservers()

    def enter(self) -> None:
        """Called each time the user opens this module."""
        # Make sure parameter node exists and observed
        self.initializeParameterNode()
        self.updateLoginStateNotificationLabel()

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
        self.updateLoginLogoutButton()

    def initializeParameterNode(self) -> None:
        """Ensure parameter node exists and observed."""
        # Parameter node stores all user choices in parameter values, node selections, etc.
        # so that when the scene is saved and reloaded, these settings are restored.

        self.setParameterNode(self.logic.getParameterNode())

    def cacheAllPermissionswidgets(self) -> None:
        for moduleName in all_openlifu_modules:
            module = slicer.util.getModule(moduleName)
            widgetRepresentation = module.widgetRepresentation()
            self._permissions_widgets.extend(slicer.util.findChildren(widgetRepresentation, name="permissionsWidget*"))

    def setParameterNode(self, inputParameterNode: Optional[OpenLIFULoginParameterNode]) -> None:
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

    def onUserAccountModeClicked(self):
        # toggle and propagate to parameter node
        new_user_account_mode_state = not self._parameterNode.user_account_mode
        set_user_account_mode_state(new_user_account_mode_state)

        # reset user state
        self.logic.active_user = self._default_anonymous_user
        self.updateWidgetLoginState(LoginState.NOT_LOGGED_IN)

    @display_errors
    def onLoginLogoutClicked(self, checked: bool) -> None:
        if self.ui.loginLogoutButton.text == "Logout":
            self.logic.active_user = self._default_anonymous_user
            self.updateWidgetLoginState(LoginState.NOT_LOGGED_IN)
        elif self.ui.loginLogoutButton.text == "Login":
            loginDlg = UsernamePasswordDialog()
            returncode, user_id, password_text = loginDlg.customexec_()

            if not returncode:
                return

            users = self.logic.dataLogic.db.load_all_users()
            verify_password = lambda text, _hash: bcrypt_lz().checkpw(text.encode('utf-8'), _hash.encode('utf-8'))

            matched_user = next((u for u in users if u.id == user_id and verify_password(password_text, u.password_hash)), None)

            if not matched_user:
                self.updateWidgetLoginState(LoginState.UNSUCCESSFUL_LOGIN)
                return

            self.logic.active_user = SlicerOpenLIFUUser(matched_user)
            self.updateWidgetLoginState(LoginState.LOGGED_IN)
            slicer.util.selectModule('OpenLIFUHome')

    def updateLoginLogoutButtonAsLoginButton(self):

        # === Multiple things can block the login button ===

        if not self._parameterNode.user_account_mode:
            self.ui.loginLogoutButton.setEnabled(False)
            self.ui.loginLogoutButton.setToolTip("The login feature is only available with user account mode turned on.")
            return

        if not get_openlifu_data_parameter_node().database_is_loaded:
            self.ui.loginLogoutButton.setEnabled(False)
            self.ui.loginLogoutButton.setToolTip("The login feature requires a database connection.")
            return

        # Now we see if there is an admin in the database
        users = self.logic.dataLogic.db.load_all_users()
        if not any('admin' in u.roles for u in users):
            self.ui.loginLogoutButton.setEnabled(False)
            self.ui.loginLogoutButton.setToolTip("The login feature requires at least one administrative user in the database.")
            # set the user to admin, go to home module
            default_admin_user = SlicerOpenLIFUUser(openlifu_lz().db.User(
                    id = "default_admin", 
                    password_hash = "default_admin",
                    roles = ['admin'],
                    name = "default_admin",
                    description = "This is the default admin role automatically assigned if an admin user does not exist in the loaded database."
                    ))
            self.logic.active_user = default_admin_user
            self.updateWidgetLoginState(LoginState.DEFAULT_ADMIN)
            slicer.util.selectModule('OpenLIFUHome')
            return

        # === Otherwise, login works ===

        self.ui.loginLogoutButton.setEnabled(True)
        self.ui.loginLogoutButton.setToolTip("Login to an account in the database.")


    def updateLoginLogoutButton(self):
        if self._cur_login_state == LoginState.NOT_LOGGED_IN:
            self.ui.loginLogoutButton.setText("Login")
        elif self._cur_login_state == LoginState.UNSUCCESSFUL_LOGIN:
            self.ui.loginLogoutButton.setText("Login")
        elif self._cur_login_state == LoginState.LOGGED_IN:
            self.ui.loginLogoutButton.setText("Logout")
        elif self._cur_login_state == LoginState.DEFAULT_ADMIN:
            self.ui.loginLogoutButton.setText("Login")

        if self.ui.loginLogoutButton.text == "Logout":
            self.ui.loginLogoutButton.setEnabled(True)
            self.ui.loginLogoutButton.setToolTip("Logout to an account in the database.")
        elif self.ui.loginLogoutButton.text == "Login":
            self.updateLoginLogoutButtonAsLoginButton()

    def updateWidgetLoginState(self, state: LoginState):
        self._cur_login_state = state
        self.updateLoginStateNotificationLabel()
        self.updateLoginLogoutButton()
        self.enforceUserPermissions()

    def updateLoginStateNotificationLabel(self):
        if self._cur_login_state == LoginState.NOT_LOGGED_IN:
            self.ui.loginStateNotificationLabel.setProperty("text", "")  
            self.ui.loginStateNotificationLabel.setProperty("styleSheet", "border: none;")
        elif self._cur_login_state == LoginState.UNSUCCESSFUL_LOGIN:
            self.ui.loginStateNotificationLabel.setProperty("text", "Unsuccessful login. Please try again.")
            self.ui.loginStateNotificationLabel.setProperty("styleSheet", "color: red; font-size: 16px; border: 1px solid red;")
        elif self._cur_login_state == LoginState.LOGGED_IN:
            # We want the standard text color to make sense if in night-mode
            palette = qt.QApplication.instance().palette()
            text_color = palette.color(qt.QPalette.WindowText).name()
            self.ui.loginStateNotificationLabel.setProperty("text", f"Welcome, {self.logic.active_user.user.name}!")
            self.ui.loginStateNotificationLabel.setProperty("styleSheet", f"color: {text_color}; font-weight: bold; font-size: 16px; border: none;")
        elif self._cur_login_state == LoginState.DEFAULT_ADMIN:
            self.ui.loginStateNotificationLabel.setProperty("text", f"Welcome! Please create an admin account for user accounts to work.")

    def updateUserAccountModeButton(self):
        if self._parameterNode.user_account_mode:
            self.ui.userAccountModePushButton.setText("Exit User Account Mode")
        else:
            self.ui.userAccountModePushButton.setText("Start User Account Mode")
            self.ui.userAccountModePushButton.setToolTip(
                    "User Account mode will enforce restrictions over available widgets based on user credentials."
                )
    
    def onParameterNodeModified(self, caller, event) -> None:
        self.updateUserAccountModeButton()
        self.enforceUserPermissions()

    def enforceUserPermissions(self) -> None:
        
        # === Don't enforce if no user account mode ===

        if not self._parameterNode.user_account_mode:
            for widget in self._permissions_widgets:
                widget.setEnabled(True)
            return

        # === Check cache if there is an active user ===

        if self.logic.active_user is not None:
            if self._cur_user_id_enforced == self.logic.active_user.user.id:
                return
            else:
                self._cur_user_id_enforced = self.logic.active_user.user.id

        # === Enforce ===

        for widget in self._permissions_widgets:
            allowed_roles = widget.property("slicer.openlifu.allowed-roles")
            user_roles = self.logic.active_user.user.roles
            widget.setEnabled(any(role in allowed_roles for role in user_roles))

# OpenLIFULoginLogic
#

class OpenLIFULoginLogic(ScriptedLoadableModuleLogic):
    """This class should implement all the actual
    computation done by your module.  The interface
    should be such that other python code can import
    this class and make use of the functionality without
    requiring an instance of the Widget.
    Uses ScriptedLoadableModuleLogic base class, available at:
    https://github.com/Slicer/Slicer/blob/main/Base/Python/slicer/ScriptedLoadableModule.py
    """

    active_user : "Optional[SlicerOpenLIFUUser]"

    def __init__(self) -> None:
        """Called when the logic class is instantiated. Can be used for initializing member variables."""
        self.dataLogic = slicer.util.getModuleLogic('OpenLIFUData')

        ScriptedLoadableModuleLogic.__init__(self)

    def getParameterNode(self):
        return OpenLIFULoginParameterNode(super().getParameterNode())

    def clear_session(self) -> None:
        self.current_session = None

    def start_user_account_mode(self):
        set_user_account_mode_state(True)
