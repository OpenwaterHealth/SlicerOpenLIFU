# Standard library imports
import json
import os
import subprocess
import sys
import tempfile
import urllib.request
import zipfile
from enum import Enum
from pathlib import Path
from typing import Optional, List, Dict, Callable, TYPE_CHECKING
import time

# Third-party imports
import qt
import vtk
import numpy as np

# Slicer imports
import slicer
from slicer import vtkMRMLScriptedModuleNode
from slicer.ScriptedLoadableModule import *
from slicer.i18n import tr as _
from slicer.i18n import translate
from slicer.parameterNodeWrapper import parameterNodeWrapper
from slicer.util import VTKObservationMixin

# OpenLIFULib imports
from OpenLIFULib import (
    ensure_python_requirements_for_module_enter,
    get_cur_db,
    get_current_user,
)
from OpenLIFULib.class_definition_widgets import ListTableWidget
from OpenLIFULib.user_account_mode_util import UserAccountBanner, set_user_account_mode_state
from OpenLIFULib.util import (
    display_errors,
    get_openlifu_data_parameter_node,
    cleanup_module_callbacks,
    register_module_callback,
)

if TYPE_CHECKING:
    import openlifu
    import openlifu.db

#
# OpenLIFULogin
#

class OpenLIFULogin(ScriptedLoadableModule):
    """Uses ScriptedLoadableModule base class, available at:
    https://github.com/Slicer/Slicer/blob/main/Base/Python/slicer/ScriptedLoadableModule.py
    """

    def __init__(self, parent):
        ScriptedLoadableModule.__init__(self, parent)
        self.parent.title = _("OpenLIFU Login")  # TODO: make this more human readable by adding spaces
        self.parent.categories = [translate("qSlicerAbstractCoreModule", "OpenLIFU.OpenLIFU Modules")]
        self.parent.dependencies = [
            "OpenLIFUDatabase",
            "OpenLIFUData",
            "OpenLIFUHome",
            "OpenLIFUPrePlanning",
            "OpenLIFUSonicationControl",
            "OpenLIFUSonicationPlanner",
            "OpenLIFUTransducerLocalization",
        ]  # add here list of module names that this module requires
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
        # Hide this module from the module selector panel. Its functionality is
        # surfaced via the Login popup on the Data page; the module's logic and
        # widget representation remain accessible to other modules through
        # ``slicer.util.getModuleLogic`` / ``slicer.util.getModuleWidget``.
        self.parent.hidden = True

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
    user_account_mode : bool = False
    
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

        self.setMinimumWidth(300)
        self.setContentsMargins(15, 15, 15, 15)

        formLayout = qt.QFormLayout()
        formLayout.setSpacing(10)
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
            return (returncode, id, password_text)
        return (returncode, None, None)

class CreateNewAccountDialog(qt.QDialog):
    """ Create a new account dialog """

    def __init__(self, existing_users: List["openlifu.db.User"], parent="mainWindow"):
        super().__init__(slicer.util.mainWindow() if parent == "mainWindow" else parent)
        self.setWindowTitle("Create an account")
        self.setWindowModality(qt.Qt.ApplicationModal)
        self.existing_users = existing_users
        self.setup()

    def setup(self):

        self.setMinimumWidth(400)
        self.setContentsMargins(15, 15, 15, 15)

        formLayout = qt.QFormLayout()
        formLayout.setSpacing(10)
        formLayout.setFormAlignment(qt.Qt.AlignTop)
        self.setLayout(formLayout)

        # ---- User account fields ----

        self.idField = qt.QLineEdit()
        usernameLabel = qt.QLabel(_('Username:') + ' <span style="color: red;">*</span>')
        formLayout.addRow(usernameLabel, self.idField)
        self.idHintLabel = qt.QLabel(_("(use letters, #s, and _)"))
        self.idHintLabel.setStyleSheet("color: gray; font-size: small;")
        formLayout.addRow("", self.idHintLabel)

        self.passwordField = qt.QLineEdit()
        self.passwordField.setEchoMode(qt.QLineEdit.Password)
        passwordLabel = qt.QLabel(_('Password:') + ' <span style="color: red;">*</span>')
        formLayout.addRow(passwordLabel, self.passwordField)

        self.nameField = qt.QLineEdit()
        formLayout.addRow(_("Name:"), self.nameField)

        self.descriptionField = qt.QLineEdit()
        formLayout.addRow(_("Description:"), self.descriptionField)

        self.roleField = qt.QComboBox()
        self.roleField.addItems(["operator", "admin"])
        formLayout.addRow(_("Role:"), self.roleField)

        # ---- Field restrictions ----

        self.idField.setMaxLength(20)
        self.passwordField.setMaxLength(50)
        self.nameField.setMaxLength(50)
        self.descriptionField.setMaxLength(100)

        # ---- Closing buttons ----

        self.buttonBox = qt.QDialogButtonBox()
        self.buttonBox.setStandardButtons(
            qt.QDialogButtonBox.Ok | qt.QDialogButtonBox.Cancel
        )
        formLayout.addWidget(self.buttonBox)

        self.buttonBox.rejected.connect(self.reject)
        self.buttonBox.accepted.connect(self.validateInputs)

    def validateInputs(self):
        """
        Ensure a user account does not exist with that ID and inputs are valid
        """
        user_id = self.idField.text
        password_text = self.passwordField.text

        if not user_id:
            slicer.util.errorDisplay("Username cannot be empty.", parent=self)
            return
        if user_id in ["anonymous", "default_admin"]:
            slicer.util.errorDisplay("You cannot create an account with this username.", parent=self)
            return
        if len(user_id) < 3:
            slicer.util.errorDisplay("Username must be at least 3 characters.", parent=self)
            return
        if not all(c.isalnum() or c == '_' for c in user_id):
            slicer.util.errorDisplay("Username can only contain letters, numbers, and underscores.", parent=self)
            return
        if any(u.id == user_id for u in self.existing_users):
            slicer.util.errorDisplay("An account with that name already exists.", parent=self)
            return
        if not password_text or len(password_text) < 6:
            slicer.util.errorDisplay("Password must be at least 6 characters.", parent=self)
            return

        self.accept()

    def customexec_(self):
        returncode = self.exec_()
        if returncode == qt.QDialog.Accepted:
            import bcrypt

            user_id = self.idField.text
            password_text = self.passwordField.text
            name = self.nameField.text
            description = self.descriptionField.text
            role = self.roleField.currentText

            salt = bcrypt.gensalt()
            password_hash = bcrypt.hashpw(password_text.encode('utf-8'), salt).decode('utf-8')

            user_dict = {
                "id": user_id,
                "password_hash": password_hash,
                "roles": [role],
                "name": name,
                "description": description
            }
            return (returncode, user_dict)
        return (returncode, None)

class ChangePasswordDialog(qt.QDialog):
    """ Change password dialog """

    def __init__(self, user: "openlifu.db.User", parent="mainWindow"):
        super().__init__(slicer.util.mainWindow() if parent == "mainWindow" else parent)
        self.setWindowTitle("Change password")
        self.setWindowModality(qt.Qt.ApplicationModal)
        self.user = user
        self.setup()

    def setup(self):

        self.setMinimumWidth(400)
        self.setContentsMargins(20, 20, 20, 20)

        mainLayout = qt.QVBoxLayout()
        mainLayout.setSpacing(15)
        self.setLayout(mainLayout)

        formLayout = qt.QFormLayout()
        formLayout.setSpacing(12)
        formLayout.setFormAlignment(qt.Qt.AlignTop)
        mainLayout.addLayout(formLayout)

        self.infoLabel = qt.QLabel(f"Change the password for {self.user.id}:")
        self.infoLabel.setAlignment(qt.Qt.AlignCenter)
        self.infoLabel.setWordWrap(True)
        self.infoLabel.setStyleSheet("""
            font-size: 150%;
            font-weight: bold;
            padding-bottom: 5px;
        """)
        formLayout.addRow(self.infoLabel)

        # ---- Password fields ----

        self.createPasswordField = qt.QLineEdit()
        self.createPasswordField.setEchoMode(qt.QLineEdit.Password)
        createPasswordLabel = qt.QLabel(_('Create Password:') + ' <span style="color: red;">*</span>')
        formLayout.addRow(createPasswordLabel, self.createPasswordField)

        self.confirmPasswordField = qt.QLineEdit()
        self.confirmPasswordField.setEchoMode(qt.QLineEdit.Password)
        confirmPasswordLabel = qt.QLabel(_('Confirm Password:') + ' <span style="color: red;">*</span>')
        formLayout.addRow(confirmPasswordLabel, self.confirmPasswordField)

        # ---- Field restrictions ----

        self.createPasswordField.setMaxLength(50)
        self.confirmPasswordField.setMaxLength(50)

        # ---- Closing buttons ----

        self.buttonBox = qt.QDialogButtonBox()
        self.buttonBox.setStandardButtons(
            qt.QDialogButtonBox.Ok | qt.QDialogButtonBox.Cancel
        )
        mainLayout.addWidget(self.buttonBox)

        self.buttonBox.rejected.connect(self.reject)
        self.buttonBox.accepted.connect(self.validateInputs)

    def validateInputs(self):
        """
        Ensure the password is valid and the passwords match
        """
        create_password_text = self.createPasswordField.text
        confirm_password_text = self.confirmPasswordField.text

        if not create_password_text or len(create_password_text) < 6:
            slicer.util.errorDisplay("Password must be at least 6 characters.", parent=self)
            return
        if create_password_text != confirm_password_text:
            slicer.util.errorDisplay("Passwords do not match.", parent=self)
            return

        self.accept()

    def customexec_(self):
        returncode = self.exec_()
        if returncode == qt.QDialog.Accepted:
            import bcrypt

            password_text = self.createPasswordField.text
            salt = bcrypt.gensalt()
            password_hash = bcrypt.hashpw(password_text.encode('utf-8'), salt).decode('utf-8')

            user_dict = {
                "id": self.user.id,
                "password_hash": password_hash,
                "roles": self.user.roles,
                "name": self.user.name,
                "description": self.user.description
            }
            return (returncode, user_dict)
        return (returncode, None)

class ManageAccountsDialog(qt.QDialog):
    """ Interface for managing user accounts """

    def __init__(self, db : "openlifu.db.Database", parent="mainWindow"):
        super().__init__(slicer.util.mainWindow() if parent == "mainWindow" else parent)
        """ Args:
                existing_users: openlifu.db.User objects
        """

        self.setWindowTitle("Select a user account to manage")
        self.setWindowModality(qt.Qt.WindowModal)
        self.resize(600, 400)

        self.db = db # Needed for all database interaction

        self.selected_user_id : str = None
        self.setup()

    def setup(self):

        self.boxLayout = qt.QVBoxLayout()
        self.setLayout(self.boxLayout)
        self.setMinimumSize(1000, 500)
        self.setMaximumSize(1200, 800)

        # ---- Users table ----

        cols = ["ID", "Name", "Roles", "Description"]
        self.tableWidget = qt.QTableWidget(self)
        self.tableWidget.setColumnCount(len(cols))
        self.tableWidget.setHorizontalHeaderLabels(cols)
        self.tableWidget.setSelectionBehavior(qt.QAbstractItemView.SelectRows)
        self.tableWidget.setEditTriggers(qt.QAbstractItemView.NoEditTriggers)
        self.tableWidget.setWordWrap(True) # style
        self.tableWidget.setShowGrid(True)  # style
        self.tableWidget.setSizePolicy(qt.QSizePolicy.Expanding, qt.QSizePolicy.Expanding)  # style
        self.tableWidget.verticalHeader().setDefaultSectionSize(24)  # style
        self.tableWidget.verticalHeader().setVisible(False)
        self.tableWidget.setFocusPolicy(qt.Qt.NoFocus)
        self.tableWidget.horizontalHeader().setHighlightSections(False)

        header = self.tableWidget.horizontalHeader()
        header.setSectionResizeMode(0, qt.QHeaderView.ResizeToContents)  # ID
        header.setSectionResizeMode(1, qt.QHeaderView.ResizeToContents)  # Name
        header.setSectionResizeMode(2, qt.QHeaderView.ResizeToContents)  # Roles
        header.setSectionResizeMode(3, qt.QHeaderView.Stretch)           # Description

        self.boxLayout.addWidget(self.tableWidget)

        # ---- User management buttons ----

        buttonsLayout = qt.QHBoxLayout()

        self.createUserButton = qt.QPushButton("Create New User")
        self.changePasswordButton = qt.QPushButton("Change User Password")
        self.editRolesButton = qt.QPushButton("Edit User Roles")
        self.deleteUserButton = qt.QPushButton("Delete User")

        for button in [self.createUserButton, self.changePasswordButton, self.editRolesButton, self.deleteUserButton]:
            button.setSizePolicy(qt.QSizePolicy.Expanding, qt.QSizePolicy.Preferred)
            buttonsLayout.addWidget(button)

        self.boxLayout.addLayout(buttonsLayout)

        self.createUserButton.clicked.connect(self.onCreateNewUserClicked)
        self.changePasswordButton.clicked.connect(self.onChangePasswordClicked)
        self.editRolesButton.clicked.connect(self.onEditUserRolesClicked)
        self.deleteUserButton.clicked.connect(self.onDeleteUserClicked)

        # ---- Ok button ----

        self.buttonBox = qt.QDialogButtonBox()
        self.buttonBox.setStandardButtons(
            qt.QDialogButtonBox.Ok,
        )
        self.boxLayout.addWidget(self.buttonBox)

        self.buttonBox.accepted.connect(self.accept)

        # ----

        self.updateUsersList()

    def updateUsersList(self):
        users = self.db.load_all_users()

        # Reset the table
        self.tableWidget.clearContents()
        self.tableWidget.setRowCount(0)

        # Reload the table
        self.tableWidget.setRowCount(len(users))
        for row, user in enumerate(users):
            self.tableWidget.setItem(row, 0, qt.QTableWidgetItem(user.id))
            self.tableWidget.setItem(row, 1, qt.QTableWidgetItem(user.name))
            self.tableWidget.setItem(row, 2, qt.QTableWidgetItem(", ".join(user.roles)))
            self.tableWidget.setItem(row, 3, qt.QTableWidgetItem(user.description))

        for row in range(self.tableWidget.rowCount):
            self.tableWidget.setRowHeight(row, 48) # help wrap

    def onCreateNewUserClicked(self):
        slicer.util.getModuleWidget("OpenLIFULogin").onCreateNewAccountClicked()
        self.updateUsersList()

    def onEditUserRolesClicked(self):

        # --- Spawn a dialog for editing user roles as list ---

        selected_items = self.tableWidget.selectedItems()
        if not selected_items:
            slicer.util.errorDisplay("Please select a user to edit roles.")
            return

        selected_row = selected_items[0].row()
        user_id = self.tableWidget.item(selected_row, 0).text()
        user = self.db.load_user(user_id)

        dialog = qt.QDialog(self)
        dialog.setWindowTitle(f"Edit Roles for {user.id}")
        dialog.setModal(True)
        dialog.resize(400, 300)

        layout = qt.QVBoxLayout(dialog)
        roles_widget = ListTableWidget(dialog, object_name="Role", object_type=str)
        roles_widget.from_list(user.roles)
        layout.addWidget(roles_widget)

        self.buttonBox = qt.QDialogButtonBox()
        self.buttonBox.setStandardButtons(qt.QDialogButtonBox.Ok |
                                          qt.QDialogButtonBox.Cancel)
        def on_accept():
            import openlifu.db.database

            user.roles = roles_widget.to_list()
            self.db.write_user(user, on_conflict=openlifu.db.database.OnConflictOpts.OVERWRITE)
            self.updateUsersList()
            dialog.accept()

        self.buttonBox.accepted.connect(on_accept)
        self.buttonBox.rejected.connect(dialog.reject)

        layout.addWidget(self.buttonBox)

        dialog.exec_()

    def onDeleteUserClicked(self):

        # Get item and delete user

        selected_items = self.tableWidget.selectedItems()
        if not selected_items:
            slicer.util.errorDisplay("Please select a user to delete.")
            return

        selected_row = selected_items[0].row()
        user_id = self.tableWidget.item(selected_row, 0).text()

        if get_current_user().id == user_id:
            if not slicer.util.confirmYesNoDisplay(
                text=f"You are currently logged into the user {user_id}. Deleting this user will log you out. Are you sure you want to delete?",
                windowTitle="User Delete Confirmation",
            ):
                return
            self.db.delete_user(user_id)
            slicer.util.getModuleWidget("OpenLIFULogin").logout()
            self.accept()
        else:
            if not slicer.util.confirmYesNoDisplay(
                text=f"Are you sure you want to delete the user with id '{user_id}'?",
                windowTitle="User Delete Confirmation",
            ):
                return

            self.db.delete_user(user_id)

            # Update GUI

            self.updateUsersList()

        slicer.util.infoDisplay(f"User deleted: \'{user_id}\'")

    def onChangePasswordClicked(self):

        # Get item and change user password

        selected_items = self.tableWidget.selectedItems()
        if not selected_items:
            slicer.util.errorDisplay("Please select a user to change password.")
            return

        selected_row = selected_items[0].row()
        user_id = self.tableWidget.item(selected_row, 0).text()
        user = self.db.load_user(user_id)

        change_password_dlg = ChangePasswordDialog(user)
        returncode, user_dict = change_password_dlg.customexec_()
        if not returncode or user_dict is None:
            return

        import openlifu.db
        import openlifu.db.database

        modifiedUser = openlifu.db.User.from_dict(user_dict)
        self.db.write_user(modifiedUser, on_conflict = openlifu.db.database.OnConflictOpts.OVERWRITE)

        # Update GUI

        self.updateUsersList()
        slicer.util.infoDisplay(f"Password changed for: \'{user_id}\'")

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
        self._user_account_banners : List[UserAccountBanner] = []
        self._parameterNode = None
        self._parameterNodeGuiTag = None
        self._default_anonymous_user = None  # initialized after dependency check
        self._default_admin_user = None  # initialized after dependency check
        self._last_active_user = self._default_anonymous_user

    def setup(self) -> None:
        """Called when the user opens the module the first time and the widget is initialized."""
        ScriptedLoadableModuleWidget.setup(self)

        # Load widget from .ui file (created by Qt Designer).
        # Additional widgets can be instantiated manually and added to self.layout.
        uiWidget = slicer.util.loadUI(self.resourcePath("UI/OpenLIFULogin.ui"))
        self.layout.addWidget(uiWidget)
        self.uiWidget = uiWidget
        self.ui = slicer.util.childWidgetVariables(uiWidget)

        # Set scene in MRML widgets. Make sure that in Qt designer the top-level qMRMLWidget's
        # "mrmlSceneChanged(vtkMRMLScene*)" signal in is connected to each MRML widget's.
        # "setMRMLScene(vtkMRMLScene*)" slot.
        uiWidget.setMRMLScene(slicer.mrmlScene)

        # Create logic class. Logic implements all computations that should be possible to run
        # in batch mode, without a graphical user interface.
        self.logic = OpenLIFULoginLogic()

        # === Connections and UI setup =======

        # Connect to the database logic for updates related to database
        db_logic = slicer.util.getModuleLogic("OpenLIFUDatabase")
        register_module_callback(
            self,
            db_logic.call_on_db_changed,
            db_logic.remove_db_changed_callback,
            self.onDatabaseChanged,
        )

        # Login

        self.ui.loginLogoutButton.clicked.connect(self.onLoginLogoutClicked)
        register_module_callback(
            self,
            self.logic.call_on_active_user_changed,
            self.logic.remove_active_user_changed_callback,
            self.onActiveUserChanged,
        )

        # Account management

        self.ui.manageAccountsButton.clicked.connect(self.onManageAccountsButtonclicked)

        # Note: User Account Mode toggling and the "Install dependencies"
        # section both used to live here but have been moved to the Data page
        # ("Permissions" dropdown in the top toolbar and the "Dependencies"
        # collapsible section, respectively).

        # Make sure parameter node is initialized (needed for module reload)
        self.initializeParameterNode()
        self.cacheAllLoginRelatedWidgets()

        self.updateWidgetLoginState(LoginState.NOT_LOGGED_IN)
        self.onDatabaseChanged() # Call the routine to update from data parameter node

    def _initDefaultUsers(self) -> None:
        """Create default User objects after dependencies are available."""
        if self._default_anonymous_user is not None and self._default_admin_user is not None:
            return

        import openlifu.db

        self._default_anonymous_user = openlifu.db.User(
            id = "anonymous",
            password_hash = "",
            roles = [],
            name = "Anonymous",
            description = "This is the default role set when the app opens, without anyone logged in, with user account mode activated. It has no roles and is therefore the most restricted."
        )
        self._default_admin_user = openlifu.db.User(
            id = "default_admin",
            password_hash = "default_admin",
            roles = ['admin'],
            name = "default_admin",
            description = "This is the default admin role automatically assigned if an admin user does not exist in the loaded database or if there is no database loaded at all."
        )
        self._last_active_user = self._default_anonymous_user

    def cleanup(self) -> None:
        """Called when the application closes and the module widget is destroyed."""
        cleanup_module_callbacks(self)
        self.removeObservers()

    def enter(self) -> None:
        """Called each time the user opens this module."""
        dependencies_available = ensure_python_requirements_for_module_enter()
        # Make sure parameter node exists and observed
        self.initializeParameterNode()
        if dependencies_available:
            self._initDefaultUsers()
            if self.logic.active_user is None:
                self.logic.active_user = self._default_anonymous_user
            self.updateWidgetLoginState(self._cur_login_state)
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

    def onDatabaseChanged(self, db: Optional["openlifu.db.Database"] = None):
        if self._cur_login_state == LoginState.LOGGED_IN:
            slicer.util.infoDisplay(f"You have been logged out because the database location was changed.")
            self.logout()
        self.updateWidgetLoginState(LoginState.NOT_LOGGED_IN)

    def initializeParameterNode(self) -> None:
        """Ensure parameter node exists and observed."""
        # Parameter node stores all user choices in parameter values, node selections, etc.
        # so that when the scene is saved and reloaded, these settings are restored.

        self.setParameterNode(self.logic.getParameterNode())

    def cacheAllLoginRelatedWidgets(self) -> None:
        all_openlifu_modules = [
            "OpenLIFUDatabase",
            "OpenLIFUData",
            "OpenLIFUHome",
            "OpenLIFUPrePlanning",
            "OpenLIFUSonicationControl",
            "OpenLIFUSonicationPlanner",
            "OpenLIFUTransducerLocalization",
            ]
        for moduleName in all_openlifu_modules:
            module = slicer.util.getModule(moduleName)
            widgetRepresentation = module.widgetRepresentation()
            self._permissions_widgets.extend(slicer.util.findChildren(widgetRepresentation, name="permissionsWidget*"))
            self._user_account_banners.extend(slicer.util.findChildren(widgetRepresentation, className="UserAccountBanner"))

        self._permissions_widgets.extend([self.ui.permissionsWidget1])

        # Subscribe banners to database state changes and seed initial state.
        try:
            db_logic = slicer.util.getModuleLogic("OpenLIFUDatabase")
        except Exception:
            db_logic = None
        if db_logic is not None:
            try:
                register_module_callback(
                    self,
                    db_logic.call_on_db_changed,
                    db_logic.remove_db_changed_callback,
                    self._onDatabaseChangedForBanners,
                )
                current_db = getattr(db_logic, "db", None)
                for widget in self._user_account_banners:
                    widget.change_database_status(current_db)
            except Exception:
                pass
        # Seed active user state too.
        current_user = getattr(self.logic, "active_user", None)
        try:
            current_uam = bool(self._parameterNode.user_account_mode) if self._parameterNode else False
        except (AttributeError, RuntimeError):
            current_uam = False
        for widget in self._user_account_banners:
            try:
                widget.change_user_account_mode(current_uam)
                widget.change_active_user(current_user)
            except Exception:
                pass

    def _onDatabaseChangedForBanners(self, db) -> None:
        """Dispatch DB-changed events to all cached UserAccountBanner widgets."""
        for widget in self._user_account_banners:
            try:
                widget.change_database_status(db)
            except Exception:
                pass

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

    @display_errors
    def onLoginLogoutClicked(self, checked: bool = False) -> None:
        if self.ui.loginLogoutButton.text == "Logout":
            self.logout()
        elif self.ui.loginLogoutButton.text == "Login":
            self.login()

    def logout(self) -> None:
        """Log the user out, setting the current user to be the default anonymous user."""
        self._initDefaultUsers()
        self.logic.active_user = self._default_anonymous_user
        self.updateWidgetLoginState(LoginState.NOT_LOGGED_IN)
    
    def login(self) -> None:
        """Execute the interactive login process, which could succeed, fail, or be canceled."""
        loginDlg = UsernamePasswordDialog()
        returncode, user_id, password_text = loginDlg.customexec_()

        if not returncode:
            return

        users = get_cur_db().load_all_users()
        import bcrypt

        verify_password = lambda text, _hash: bcrypt.checkpw(text.encode('utf-8'), _hash.encode('utf-8'))

        matched_user = next((u for u in users if u.id == user_id and verify_password(password_text, u.password_hash)), None)

        if not matched_user:
            self.updateWidgetLoginState(LoginState.UNSUCCESSFUL_LOGIN)
            return

        self.logic.active_user = matched_user
        self.updateWidgetLoginState(LoginState.LOGGED_IN)

    @display_errors
    def onCreateNewAccountClicked(self, checked:bool = False) -> None:
        new_account_dlg = CreateNewAccountDialog(get_cur_db().load_all_users())
        returncode, user_dict = new_account_dlg.customexec_()
        if not returncode or user_dict is None:
            return

        self.logic.add_user_to_database(user_dict)
        self.updateWidgetLoginState() # reload in case an admin user was added when there previously wasn't one

    @display_errors
    def onManageAccountsButtonclicked(self, checked:bool) -> None:
        new_account_dlg = ManageAccountsDialog(get_cur_db())
        new_account_dlg.exec_()
        self.updateWidgetLoginState() # reload in case an admin user was added when there previously wasn't one

        # In case the currently logged in user was edited in the management
        # dialog, we reload it. Skip the reload if no user is currently signed in
        # (anonymous session) — there is nothing to refresh and ``active_user``
        # would be ``None``.
        if self.logic.active_user is None:
            return
        cur_user_id = self.logic.active_user.id
        users = get_cur_db().load_all_users()
        potentially_updated_user = next((u for u in users if u.id == cur_user_id), self._default_anonymous_user)
        self.logic.active_user = potentially_updated_user

    def updateLoginLogoutButtonAsLoginButton(self):

        # === Multiple things can block the login button ===

        # Note: in the new design (popup-based Account UI driven from the
        # OpenLIFU Data toolbar), Permissions mode (formerly "user account
        # mode") is selected via a dropdown that lives behind this modal
        # popup. Gating login on UAM would leave the user unable to log in
        # without first dismissing this popup, switching the dropdown, and
        # reopening - which is confusing. Permissions mode now only governs
        # role enforcement, not whether sign-in is available. Logging in is
        # allowed in either mode as long as a database with an admin exists.

        if not get_cur_db():
            self.ui.loginLogoutButton.setEnabled(False)
            self.ui.loginLogoutButton.setToolTip("The login feature requires a database connection.")
            return

        if self._cur_login_state == LoginState.DEFAULT_ADMIN:
            self.ui.loginLogoutButton.setEnabled(False)
            self.ui.loginLogoutButton.setToolTip("The login feature requires at least one admin user.")
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

    def updateAccountManagementButtons(self):
        # You only need a database loaded to be able to do this. User account
        # mode can be off. If user account mode is on, only admins can interact
        # with the button.
        if not get_cur_db():
            self.ui.manageAccountsButton.setEnabled(False)
            self.ui.manageAccountsButton.setToolTip("The login feature requires a database connection.")
            return
        self.ui.manageAccountsButton.setEnabled(True)
        self.ui.manageAccountsButton.setToolTip("Manage accounts")


    def updateWidgetLoginState(self, state: Optional[LoginState] = None):
        if state is None:
            state = self._cur_login_state # if called with None, we reload with prev state

        if self._default_anonymous_user is None or self._default_admin_user is None:
            self.logic.active_user = None
            self._cur_login_state = LoginState.NOT_LOGGED_IN
            self.updateLoginStateNotificationLabel()
            self.updateLoginLogoutButton()
            self.updateAccountManagementButtons()
            self.enforceUserPermissions()
            return

        if get_cur_db() and not any('admin' in u.roles for u in get_cur_db().load_all_users()):
            # if there is a connected db with no admin users in it, set the user to admin
            self.logic.active_user = self._default_admin_user
            self._cur_login_state = LoginState.DEFAULT_ADMIN
        elif get_cur_db() and self._cur_login_state == LoginState.DEFAULT_ADMIN:
            # If there *IS* an admin in the db, but the state is DEFAULT_ADMIN,
            # we have to exit the DEFAULT_ADMIN state.
            self.logic.active_user = self._default_anonymous_user
            self._cur_login_state = LoginState.NOT_LOGGED_IN
        elif get_cur_db() is None:
            # if there is no connected db, set the user to admin
            self.logic.active_user = self._default_admin_user
            self._cur_login_state = LoginState.DEFAULT_ADMIN
        else:
            self._cur_login_state = state

        self.updateLoginStateNotificationLabel()
        self.updateLoginLogoutButton()
        self.updateAccountManagementButtons()
        self.enforceUserPermissions()
        self.updateUserInfoLabels()

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
            self.ui.loginStateNotificationLabel.setProperty("text", f"Welcome, {self.logic.active_user.name}!")
            self.ui.loginStateNotificationLabel.setProperty("styleSheet", f"color: {text_color}; font-weight: bold; font-size: 16px; border: none;")
        elif self._cur_login_state == LoginState.DEFAULT_ADMIN:
            self.ui.loginStateNotificationLabel.setProperty("text",
                "Welcome! You currently have root permissions. "
                "Connect to a database and add an admin account for user account permissions to engage across the application."
            )

    def updateUserAccountModeButton(self):
        # The User Account Mode toggle has been relocated to the Data page
        # ("Permissions" dropdown). This method is preserved as a no-op so
        # legacy call sites (e.g. ``onParameterNodeModified``) keep working.
        return

    def updateUserInfoLabels(self):
        """Refresh the active-user info block (name / id / roles).

        When user account mode is off or no real user is logged in (i.e. the
        active user is the default anonymous user), each label reads
        "Not Logged In". Otherwise the labels show the active user's name,
        id, and comma-separated role list.
        """
        active = self.logic.active_user
        is_anonymous = (
            active is None
            or getattr(active, "id", None) is None
            or (self._default_anonymous_user is not None
                and active is self._default_anonymous_user)
        )
        if is_anonymous or self._cur_login_state in (
            LoginState.NOT_LOGGED_IN,
            LoginState.UNSUCCESSFUL_LOGIN,
        ):
            placeholder = "Not Logged In"
            self.ui.activeUserNameLabel.setText(placeholder)
            self.ui.activeUserIdLabel.setText(placeholder)
            self.ui.activeUserRolesLabel.setText(placeholder)
            return
        self.ui.activeUserNameLabel.setText(getattr(active, "name", "") or "")
        self.ui.activeUserIdLabel.setText(getattr(active, "id", "") or "")
        roles = getattr(active, "roles", None) or []
        self.ui.activeUserRolesLabel.setText(", ".join(roles) if roles else "(none)")

    def onParameterNodeModified(self, caller, event) -> None:
        self.updateUserAccountModeButton()
        self.updateAccountManagementButtons()
        self.enforceUserPermissions()
        self.updateUserInfoLabels()
        # Refresh the login/logout button so the User-mode dropdown in the
        # Data toolbar enables/disables this button immediately (it gates on
        # ``self._parameterNode.user_account_mode``).
        self.updateLoginLogoutButton()
        # Banners stay visible regardless of UAM state; their internal styling
        # already reflects whether a real user is signed in. Refresh them so
        # the User-mode red outline appears/disappears immediately.
        try:
            uam = bool(self._parameterNode.user_account_mode) if self._parameterNode else False
        except (AttributeError, RuntimeError):
            uam = False
        for widget in self._user_account_banners:
            try:
                widget.change_user_account_mode(uam)
                widget.change_active_user(self.logic.active_user)
            except Exception:
                pass

    def enforceUserPermissions(self) -> None:
        
        # === Don't enforce if no user account mode ===

        if not self._parameterNode.user_account_mode:
            for widget in self._permissions_widgets:
                widget.setEnabled(True)
            return

        # === Enforce ===

        for widget in self._permissions_widgets:
            allowed_roles = widget.property("slicer.openlifu.allowed-roles")
            user_roles = self.logic.active_user.roles if self.logic.active_user is not None else []
            widget.setEnabled(any(role in allowed_roles for role in user_roles))

    def onActiveUserChanged(self, new_active_user: Optional["openlifu.db.User"]) -> None:
        for widget in self._user_account_banners:
            widget.change_active_user(new_active_user)

        if self._last_active_user == new_active_user:
            return  # If it's the same user, we don't need to delete data

        # Clear Data module items
        slicer.util.getModuleLogic('OpenLIFUData').clear_session()
        for protocol_id in get_openlifu_data_parameter_node().loaded_protocols:
            slicer.util.getModuleLogic('OpenLIFUData').remove_protocol(protocol_id)

        self._last_active_user = new_active_user

# OpenLIFULoginLogic
#

class OpenLIFULoginLogic(ScriptedLoadableModuleLogic):
    """This class should implement all the actual
    computation done by your module. The interface
    should be such that other Python code can import
    this class and make use of the functionality without
    requiring an instance of the Widget.
    Uses ScriptedLoadableModuleLogic base class, available at:
    https://github.com/Slicer/Slicer/blob/main/Base/Python/slicer/ScriptedLoadableModule.py
    """

    def __init__(self) -> None:
        """Called when the logic class is instantiated. Can be used for initializing member variables."""
        ScriptedLoadableModuleLogic.__init__(self)

        self._active_user: "Optional[openlifu.db.User]" = None
        """The currently active user. Do not set this directly -- use the `active_user` property.
        Initialized to None and set from the widget after openlifu is confirmed available."""

        self._on_active_user_changed_callbacks: List[Callable[[Optional["openlifu.db.User"]], None]] = []
        """List of functions to call when the `active_user` property is changed."""

    def getParameterNode(self):
        return OpenLIFULoginParameterNode(super().getParameterNode())

    def call_on_active_user_changed(self, f: Callable[[Optional["openlifu.db.User"]], None]) -> None:
        """Register a function to be called whenever the `active_user` property is updated.

        Args:
            f: Callback accepting a single argument with the new `active_user` value.
        """
        self._on_active_user_changed_callbacks.append(f)

    def remove_active_user_changed_callback(self, f: Callable[[Optional["openlifu.db.User"]], None]) -> None:
        """Unregister a callback previously registered via
        :py:meth:`call_on_active_user_changed`. Silently no-ops if absent.
        """
        try:
            self._on_active_user_changed_callbacks.remove(f)
        except ValueError:
            pass

    @property
    def active_user(self) -> "openlifu.db.User":
        """The currently active user."""
        return self._active_user

    @active_user.setter
    def active_user(self, user: "openlifu.db.User") -> None:
        self._active_user = user
        for callback in self._on_active_user_changed_callbacks:
            callback(self._active_user)

    def start_user_account_mode(self):
        set_user_account_mode_state(True)

    def add_user_to_database(self, user_parameters: Dict[str, str]) -> None:
        """ Add user to selected subject/session in the loaded openlifu database
        Args:
            user_parameters: Dictionary containing the required parameters for adding a user to database
        """
        user_ids = get_cur_db().get_user_ids()
        if user_parameters['id'] in user_ids:
            if not slicer.util.confirmYesNoDisplay(
                f"user ID {user_parameters['id']} already exists in the database. Overwrite user?",
                "user already exists"
            ):
                return

        import openlifu.db
        import openlifu.db.database

        newOpenLIFUuser = openlifu.db.User.from_dict(user_parameters)
        get_cur_db().write_user(newOpenLIFUuser, on_conflict = openlifu.db.database.OnConflictOpts.OVERWRITE)
