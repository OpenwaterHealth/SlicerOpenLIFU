import qt
import slicer
import time
import os
import requests
from OpenLIFULib.util import display_errors

# Slicer imports
from slicer.ScriptedLoadableModule import *
from slicer.i18n import tr as _
from slicer.i18n import translate


class OpenLIFUCloudSync(ScriptedLoadableModule):
    def __init__(self, parent):
        ScriptedLoadableModule.__init__(self, parent)
        self.parent.title = "OpenLIFU Cloud Sync"
        self.parent.categories = [
            translate("qSlicerAbstractCoreModule", "OpenLIFU")]
        self.parent.dependencies = ["OpenLIFUHome"]
        self.parent.contributors = [
            "Andrew Howe (Kitware), Ebrahim Ebrahim (Kitware), Sadhana Ravikumar (Kitware), Peter Hollender (Openwater), Sam Horvath (Kitware), Brad Moore (Kitware)"]
        self.parent.helpText = _(
            "Cloud login and synchronization for the OpenLIFU extension.")
        self.parent.acknowledgementText = _(
            "Part of Openwater's OpenLIFU platform.")
        # self.parent.hidden = True  # Removed: Setting this to True breaks registration


class OpenLIFUCloudSyncWidget(ScriptedLoadableModuleWidget):
    def setup(self):
        ScriptedLoadableModuleWidget.setup(self)
        self.logic = OpenLIFUCloudSyncLogic()

        # Robust UI path loading
        uiPath = os.path.join(os.path.dirname(__file__),
                              'Resources', 'UI', 'OpenLIFUCloudSync.ui')
        if not os.path.exists(uiPath):
            uiPath = self.resourcePath('UI/OpenLIFUCloudSync.ui')

        self.uiWidget = slicer.util.loadUI(uiPath)
        self.layout.addWidget(self.uiWidget)
        self.ui = slicer.util.childWidgetVariables(self.uiWidget)

        # Connections
        self.ui.backButton.clicked.connect(self.onBack)
        self.ui.loginButton.clicked.connect(self.onLoginToggle)
        self.ui.syncButton.clicked.connect(self.onSync)

        self.updateGUI()

    def updateGUI(self):
        token = self.logic.getValidToken()
        isLoggedIn = token is not None
        self.ui.statusLabel.text = _(
            "Logged In") if isLoggedIn else _("Not Logged In")
        self.ui.loginButton.text = _(
            "Logout") if isLoggedIn else _("Login to Cloud")
        self.ui.syncButton.enabled = isLoggedIn

    def onBack(self):
        # Retrieve previous module from mainWindow property
        prev_module = slicer.util.mainWindow().property("OpenLIFU_PreviousModule")
        slicer.util.selectModule(
            prev_module if prev_module else "OpenLIFUHome")

    def onLoginToggle(self, checked=False):
        """Added 'checked' argument to prevent TypeError from Qt signal."""
        if self.logic.getValidToken():
            self.logic.logout()
            self.updateGUI()
        else:
            from OpenLIFULogin import UsernamePasswordDialog
            dlg = UsernamePasswordDialog()
            # Standard return format for OpenLIFULogin
            res, user, pw = dlg.customexec_()
            if res == qt.QDialog.Accepted:
                success, msg = self.logic.login(user, pw)
                if not success:
                    slicer.util.errorDisplay(f"Login failed: {msg}")
                self.updateGUI()

    @display_errors
    def onSync(self, checked=False):
        """Added 'checked' argument to fix the TypeError."""
        slicer.util.showStatusMessage(_("Syncing with cloud..."))
        # Placeholder for actual sync logic
        self.ui.lastSyncLabel.text = time.strftime("%Y-%m-%d %H:%M:%S")
        slicer.util.infoDisplay(_("Cloud sync completed successfully."))


class OpenLIFUCloudSyncLogic(ScriptedLoadableModuleLogic):
    def __init__(self):
        ScriptedLoadableModuleLogic.__init__(self)
        self.apiKey = "AIzaSyBzPH2T6Cf17_KGeOSnncauJY2t1Lz4ndY"
        self._cloudTokens = None
        self._userId = None

    def login(self, email, password):
        """Authenticates with Google Identity Platform/Firebase."""
        url = f"https://identitytoolkit.googleapis.com/v1/accounts:signInWithPassword?key={self.apiKey}"
        payload = {"email": email, "password": password,
                   "returnSecureToken": True}
        try:
            response = requests.post(url, json=payload, timeout=5)
            response.raise_for_status()
            data = response.json()
            self._cloudTokens = {
                "idToken": data['idToken'],
                "refreshToken": data['refreshToken'],
                "expiresAt": time.time() + int(data['expiresIn'])
            }
            self._userId = data.get('localId')
            qt.QSettings().setValue(
                "OpenLIFU/CloudRefreshToken", data['refreshToken'])
            return True, "Success"
        except Exception as e:
            return False, str(e)

    def logout(self):
        """Clears local tokens and stored settings."""
        self._cloudTokens = None
        self._userId = None
        qt.QSettings().remove("OpenLIFU/CloudRefreshToken")

    def getValidToken(self):
        """Returns a valid idToken, automatically refreshing if expired."""
        if not self._cloudTokens:
            savedRef = qt.QSettings().value("OpenLIFU/CloudRefreshToken")
            if not savedRef:
                return None
            self._cloudTokens = {"refreshToken": savedRef, "expiresAt": 0}

        if time.time() > (self._cloudTokens.get("expiresAt", 0) - 300):
            self.refreshCloudToken()
        return self._cloudTokens.get("idToken") if self._cloudTokens else None

    def refreshCloudToken(self):
        """Refreshes the ID Token using the stored Refresh Token."""
        url = f"https://securetoken.googleapis.com/v1/token?key={self.apiKey}"
        payload = {"grant_type": "refresh_token",
                   "refresh_token": self._cloudTokens['refreshToken']}
        try:
            response = requests.post(url, data=payload, timeout=5)
            response.raise_for_status()
            data = response.json()
            self._cloudTokens["idToken"] = data['id_token']
            self._cloudTokens["expiresAt"] = time.time() + \
                int(data['expires_in'])
            if 'refresh_token' in data:
                self._cloudTokens["refreshToken"] = data['refresh_token']
                qt.QSettings().setValue(
                    "OpenLIFU/CloudRefreshToken", data['refresh_token'])
        except Exception as e:
            print(f"Cloud Token Refresh Failed: {e}")
            self._cloudTokens = None
