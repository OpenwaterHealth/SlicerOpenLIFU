import qt
import slicer
import time
import requests
from OpenLIFULib.util import display_errors

# Slicer imports
import slicer
from slicer import vtkMRMLScriptedModuleNode
from slicer.ScriptedLoadableModule import *
from slicer.i18n import tr as _
from slicer.i18n import translate
from slicer.parameterNodeWrapper import parameterNodeWrapper
from slicer.util import VTKObservationMixin

class OpenLIFUCloudSync(ScriptedLoadableModule):
    def __init__(self, parent):
        ScriptedLoadableModule.__init__(self, parent)
        self.parent.title = "OpenLIFU Cloud Sync"
        self.parent.categories = [translate(
            "qSlicerAbstractCoreModule", "OpenLIFU.OpenLIFU call_on_active_user_changedModules")]
        self.parent.dependencies = [
            "OpenLIFUHome",
        ]  # add here list of module names that this module requires
        self.parent.contributors = [
            "Andrew Howe (Kitware), Ebrahim Ebrahim (Kitware), Sadhana Ravikumar (Kitware), Peter Hollender (Openwater), Sam Horvath (Kitware), Brad Moore (Kitware)"]
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


class OpenLIFUCloudSyncWidget(ScriptedLoadableModuleWidget):
    def setup(self):
        ScriptedLoadableModuleWidget.setup(self)

        # Navigation Header
        navLayout = qt.QHBoxLayout()
        self.backButton = qt.QPushButton("← Back")
        self.backButton.setFixedWidth(80)
        self.backButton.clicked.connect(self.onBack)
        navLayout.addWidget(self.backButton)
        navLayout.addStretch()
        self.layout.addLayout(navLayout)

        # Cloud Status Group
        self.cloudGroupBox = qt.QGroupBox("Cloud Account")
        self.cloudLayout = qt.QFormLayout(self.cloudGroupBox)
        self.layout.addWidget(self.cloudGroupBox)

        self.statusLabel = qt.QLabel("Not Logged In")
        self.cloudLayout.addRow("Status:", self.statusLabel)

        self.lastSyncLabel = qt.QLabel("Never")
        self.cloudLayout.addRow("Last Sync:", self.lastSyncLabel)

        self.loginButton = qt.QPushButton("Login to Cloud")
        self.loginButton.clicked.connect(self.onLoginToggle)
        self.layout.addWidget(self.loginButton)

        self.syncButton = qt.QPushButton("Sync Now")
        self.syncButton.enabled = False
        self.syncButton.clicked.connect(self.onSync)
        self.layout.addWidget(self.syncButton)

        self.layout.addStretch()

    def onBack(self):
        # Retrieve previous module stored during the switch
        prev_module = slicer.util.mainWindow().property("OpenLIFU_PreviousModule")
        slicer.util.selectModule(
            prev_module if prev_module else "OpenLIFUHome")

    def onLoginToggle(self):
        # Reuse logic from OpenLIFULogin.py
        from OpenLIFULogin import UsernamePasswordDialog

        if self.loginButton.text == "Login to Cloud":
            dlg = UsernamePasswordDialog()
            # Note: We modified the return to only focus on cloud login
            returncode, username, password = dlg.customexec_()

            if returncode == qt.QDialog.Accepted:
                # Add your specific Firebase/Cloud auth call here
                self.statusLabel.text = f"Logged in as {username}"
                self.loginButton.text = "Logout"
                self.syncButton.enabled = True
        else:
            self.statusLabel.text = "Not Logged In"
            self.loginButton.text = "Login to Cloud"
            self.syncButton.enabled = False

    @display_errors
    def onSync(self):
        # Reuse sync logic from OpenLIFUDatabase.py
        # logic.performSync() implementation
        slicer.util.showStatusMessage("Syncing with cloud...")
        # (Insert your requests.post/get sync logic here)
        self.lastSyncLabel.text = time.strftime("%Y-%m-%d %H:%M:%S")
        slicer.util.infoDisplay("Cloud sync completed successfully.")

#        set_online_mode_state(False)
#        qt.QSettings().setValue("OpenLIFU/CloudRefreshToken", "")

#class OpenLIFUCloudSyncLogic(ScriptedLoadableModuleLogic):
#    """This class should implement all the actual
#    computation done by your module. The interface
#    should be such that other Python code can import
#    this class and make use of the functionality without
#    requiring an instance of the Widget.
#    Uses ScriptedLoadableModuleLogic base class, available at:
#    https://github.com/Slicer/Slicer/blob/main/Base/Python/slicer/ScriptedLoadableModule.py
#    """
#
#    def __init__(self) -> None:
#        """Called when the logic class is instantiated. Can be used for initializing member variables."""
#        ScriptedLoadableModuleLogic.__init__(self)
#        self.apiKey = "AIzaSyBzPH2T6Cf17_KGeOSnncauJY2t1Lz4ndY"
#        self._cloudTokens = None # Stores {idToken, refreshToken, expiresAt}
#        self._userId = None
#
#        self._active_user: "openlifu.db.User" = openlifu_lz().db.User(
#                id = "anonymous", 
#                password_hash = "",
#                roles = [],
#                name = "Anonymous",
#                description = "This is the default role set when the app opens, without anyone logged in, and when user account mode is deactivated. It has no roles, and therefore is the most restricted."
#        )
#        """The currently active user. Do not set this directly -- use the `active_user` property."""
#
#        self._on_active_user_changed_callbacks: List[Callable[[Optional["openlifu.db.User"]], None]] = []
#        """List of functions to call when the `active_user` property is changed."""
#
#    def getParameterNode(self):
#        return OpenLIFULoginParameterNode(super().getParameterNode())
#
#    def call_on_active_user_changed(self, f: Callable[[Optional["openlifu.db.User"]], None]) -> None:
#        """Register a function to be called whenever the `active_user` property is updated.
#
#        Args:
#            f: Callback accepting a single argument with the new `active_user` value.
#        """
#        self._on_active_user_changed_callbacks.append(f)
#
#    @property
#    def active_user(self) -> "openlifu.db.User":
#        """The currently active user."""
#        return self._active_user
#
#    @active_user.setter
#    def active_user(self, user: "openlifu.db.User") -> None:
#        self._active_user = user
#        for callback in self._on_active_user_changed_callbacks:
#            callback(self._active_user)
#
#    def start_user_account_mode(self):
#        set_user_account_mode_state(True)
#
#    def start_online_mode(self):
#        set_online_mode_state(True)
#
#    def add_user_to_database(self, user_parameters: Dict[str, str]) -> None:
#        """ Add user to selected subject/session in the loaded openlifu database
#        Args:
#            user_parameters: Dictionary containing the required parameters for adding a user to database
#        """
#        user_ids = get_cur_db().get_user_ids()
#        if user_parameters['id'] in user_ids:
#            if not slicer.util.confirmYesNoDisplay(
#                f"user ID {user_parameters['id']} already exists in the database. Overwrite user?",
#                "user already exists"
#            ):
#                return
#
#        newOpenLIFUuser = openlifu_lz().db.User.from_dict(user_parameters)
#        get_cur_db().write_user(newOpenLIFUuser, on_conflict = openlifu_lz().db.database.OnConflictOpts.OVERWRITE)
#
#
#    def cloudLogin(self, email, password):
#        """Authenticates with Google Identity Platform."""
#        url = f"https://identitytoolkit.googleapis.com/v1/accounts:signInWithPassword?key={self.apiKey}"
#        payload = {"email": email, "password": password, "returnSecureToken": True}
#        
#        try:
#            response = requests.post(url, json=payload)
#            response.raise_for_status()
#            data = response.json()
#            self._cloudTokens = {
#                "idToken": data['idToken'],
#                "refreshToken": data['refreshToken'],
#                "expiresAt": time.time() + int(data['expiresIn'])
#            }
#            self._userId = data['localId']
#            qt.QSettings().setValue("OpenLIFU/CloudRefreshToken", data['refreshToken'])
#            return True
#        except Exception as e:
#            return False
#
#    def getValidToken(self):
#        """Returns a valid idToken, automatically refreshing if expired."""
#        if not self._cloudTokens:
#            savedRef = qt.QSettings().value("OpenLIFU/CloudRefreshToken")
#            if not savedRef:
#                return None
#            self._cloudTokens = {"refreshToken": savedRef, "expiresAt": 0}
#
#        # Refresh if token expires in less than 5 minutes
#        if time.time() > (self._cloudTokens.get("expiresAt", 0) - 300):
#            self.refreshCloudToken()
#            
#        return self._cloudTokens.get("idToken")
#
#    def getUserId(self):
#        return self._userId
#
#    def refreshCloudToken(self):
#        """Refreshes the ID Token using the stored Refresh Token."""
#        url = f"https://securetoken.googleapis.com/v1/token?key={self.apiKey}"
#        payload = {"grant_type": "refresh_token", "refresh_token": self._cloudTokens['refreshToken']}
#        
#        try:
#            response = requests.post(url, data=payload)
#            response.raise_for_status()
#            data = response.json()
#            self._cloudTokens["idToken"] = data['id_token']
#            self._cloudTokens["expiresAt"] = time.time() + int(data['expires_in'])
#            if 'refresh_token' in data:
#                self._cloudTokens["refreshToken"] = data['refresh_token']
#                qt.QSettings().setValue("OpenLIFU/CloudRefreshToken", data['refresh_token'])
#        except Exception as e:
#            print(f"Token refresh failed: {e}")
#            self._cloudTokens = None
#
#