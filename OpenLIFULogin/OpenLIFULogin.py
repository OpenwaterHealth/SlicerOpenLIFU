from typing import Optional, TYPE_CHECKING
import json

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

#
# OpenLIFULogin
#

all_openlifu_modules = ['OpenLIFUData', 'OpenLIFUHome', 'OpenLIFUPrePlanning', 'OpenLIFUProtocolConfig', 'OpenLIFUSonicationControl', 'OpenLIFUSonicationPlanner', 'OpenLIFUTransducerTracker']

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

#
# OpenLIFULoginParameterNode
#

@parameterNodeWrapper
class OpenLIFULoginParameterNode:
    user_account_mode : bool
    active_user : "Optional[SlicerOpenLIFUUser]"
    
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
            password_text = self.password.text.encode('utf-8')
            salt = bcrypt_lz().gensalt()
            password_hash = bcrypt_lz().hashpw(password_text, salt)
            return (returncode, id, password_hash)
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
        self._parameterNode = None
        self._parameterNodeGuiTag = None

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
        self.ui.loginButton.clicked.connect(self.onLoginClicked)

        # ====================================
        
        # Make sure parameter node is initialized (needed for module reload)
        self.initializeParameterNode()

        self.updateUserAccountModeButton()
        self.updateLoginButton()

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
        self.updateLoginButton()

    def initializeParameterNode(self) -> None:
        """Ensure parameter node exists and observed."""
        # Parameter node stores all user choices in parameter values, node selections, etc.
        # so that when the scene is saved and reloaded, these settings are restored.

        self.setParameterNode(self.logic.getParameterNode())

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
        new_user_account_mode_state = not self._parameterNode.user_account_mode
        if new_user_account_mode_state:
            self.logic.start_user_account_mode()
        else:
            set_user_account_mode_state(new_user_account_mode_state)

        self.updateLoginButton()

    @display_errors
    def onLoginClicked(self, checked:bool):
        loginDlg = UsernamePasswordDialog()
        returncode, user_id, password_hash = loginDlg.customexec_()

        if not returncode:
            return False
        
        print(f'username: {user_id}; password: {password_hash}')

    def updateLoginButton(self):

        # === Multiple things can block the login button ===

        if not self._parameterNode.user_account_mode:
            self.ui.loginButton.setEnabled(False)
            self.ui.loginButton.setToolTip("The login feature is only available with user account mode turned on.")
            return

        if not get_openlifu_data_parameter_node().database_is_loaded:
            self.ui.loginButton.setEnabled(False)
            self.ui.loginButton.setToolTip("The login feature requires a database connection.")
            return

        # Now we see if there is an admin in the database
        users = self.logic.dataLogic.db.load_all_users()
        if not any('admin' in u.roles for u in users):
            self.ui.loginButton.setEnabled(False)
            self.ui.loginButton.setToolTip("The login feature requires at least one administrative user.")
            # set the user to admin, go to home module
            default_admin_user = SlicerOpenLIFUUser(openlifu_lz().db.User(
                    id = "default_admin", 
                    password_hash = "default_admin",
                    roles = ['admin'],
                    name = "default_admin",
                    description = "This is the default admin role automatically assigned if an admin user does not exist in the loaded database."
                    ))
            self._parameterNode.active_user = default_admin_user
            slicer.util.selectModule('OpenLIFUHome')
            return

        # === Otherwise, login works ===

        self.ui.loginButton.setEnabled(True)
        self.ui.loginButton.setToolTip("Login to an account in the database.")

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
        slicer.util.selectModule("OpenLIFULogin")
