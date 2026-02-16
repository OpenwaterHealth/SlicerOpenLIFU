import qt
import slicer
import time
import os
import requests
import signal
import logging
from pathlib import Path
from OpenLIFULib.util import display_errors
from slicer.ScriptedLoadableModule import *
from slicer.i18n import tr as _
from slicer.i18n import translate

logger = logging.getLogger('OpenLIFU.CloudSync')

# Global logic singleton
_sharedLogicInstance = None

def getCloudSyncLogic():
    global _sharedLogicInstance
    if _sharedLogicInstance is None:
        _sharedLogicInstance = OpenLIFUCloudSyncLogic()
    return _sharedLogicInstance

# --- Signal Bridge for Thread-Safe UI Updates ---
class CloudStatusHelper(qt.QObject):
    # Signal carries: (statusMessage, timestamp)
    statusChanged = qt.Signal(str, str)

    def __init__(self):
        super().__init__()

class OpenLIFUCloudSync(ScriptedLoadableModule):
    def __init__(self, parent):
        ScriptedLoadableModule.__init__(self, parent)
        self.parent.title = _("OpenLIFU Cloud Sync")
        self.parent.categories = [translate("qSlicerAbstractCoreModule", "OpenLIFU")]
        self.parent.dependencies = ["OpenLIFUHome"]

class OpenLIFUCloudSyncWidget(ScriptedLoadableModuleWidget):
    def setup(self):
        ScriptedLoadableModuleWidget.setup(self)
        self.logic = getCloudSyncLogic()

        uiPath = os.path.join(os.path.dirname(__file__), 'Resources', 'UI', 'OpenLIFUCloudSync.ui')
        if not os.path.exists(uiPath):
            uiPath = self.resourcePath('UI/OpenLIFUCloudSync.ui')

        self.uiWidget = slicer.util.loadUI(uiPath)
        self.layout.addWidget(self.uiWidget)
        self.ui = slicer.util.childWidgetVariables(self.uiWidget)

        # Connect UI signals
        self.ui.backButton.clicked.connect(self.onBack)
        self.ui.loginButton.clicked.connect(self.onLoginToggle)

        if hasattr(self.ui, 'syncButton'):
            self.ui.syncButton.hide()

        self.logic.statusHelper.statusChanged.connect(self.onCloudStatusChanged)

        self.updateGUI()

    def onCloudStatusChanged(self, message, timestamp):
        """Thread-safe update of UI labels from background cloud events."""
        slicer.util.showStatusMessage(f"Cloud: {message}", 3000)
        if hasattr(self.ui, 'lastSyncLabel'):
            self.ui.lastSyncLabel.text = timestamp

    def updateGUI(self):
        token = self.logic.getValidToken()
        isLoggedIn = token is not None
        self.ui.statusLabel.text = _("Logged In") if isLoggedIn else _("Not Logged In")
        self.ui.loginButton.text = _("Logout") if isLoggedIn else _("Login to Cloud")

    def onBack(self, checked=False):
        prev_module = slicer.util.mainWindow().property("OpenLIFU_PreviousModule")
        slicer.util.selectModule(prev_module if prev_module else "OpenLIFUHome")

    def onLoginToggle(self, checked=False):
        if self.logic.getValidToken():
            self.logic.logout()
        else:
            from OpenLIFULogin import UsernamePasswordDialog
            dlg = UsernamePasswordDialog()
            res, user, pw = dlg.customexec_()
            if res == qt.QDialog.Accepted:
                success, msg = self.logic.login(user, pw)
                if not success:
                    slicer.util.errorDisplay(f"Login failed: {msg}")
        self.updateGUI()       

class OpenLIFUCloudSyncLogic(ScriptedLoadableModuleLogic):
    def __init__(self):
        ScriptedLoadableModuleLogic.__init__(self)
        self.apiKey = "AIzaSyBzPH2T6Cf17_KGeOSnncauJY2t1Lz4ndY"
        self._cloudTokens = None
        self._isServiceRunning = False
        self._active_runner = None
        # Instantiate signal bridge
        self.statusHelper = CloudStatusHelper()

        # Cleanup connections for both graceful and terminal exits
        slicer.app.connect("aboutToQuit()", self.cleanup)
        signal.signal(signal.SIGINT, self._handleTerminalInterrupt)

        # Defer heartbeat startup
        qt.QTimer.singleShot(1000, self.startHeartbeat)

        self.dummyTimer = qt.QTimer()
        self.dummyTimer.timeout.connect(lambda: None) # Do nothing
        self.dummyTimer.start(100) # Fire every 100ms to "nudge" the GIL

    def _handleTerminalInterrupt(self, signum, frame):
        """Ensures cleanup runs even if Ctrl+C is pressed in terminal."""
        logger.info("Terminal interrupt detected (Ctrl+C). Cleaning up...")
        self.cleanup()
        slicer.app.quit()

    def startHeartbeat(self):
        self.monitorTimer = qt.QTimer()
        self.monitorTimer.timeout.connect(self.heartbeat)
        self.monitorTimer.start(10000)
        self.heartbeat()

    def heartbeat(self):
        logger.info("Heartbeat: Checking Cloud Sync status...")
        token = self.getValidToken()
        if not self._isServiceRunning and token:
            self.attemptAutoStartSync()

    def _safeStatusUpdate(self, status):
        """Thread-safe bridge to emit UI updates from background threads."""
        timestamp = time.strftime("%H:%M:%S")
        self.statusHelper.statusChanged.emit(status, timestamp)

    def attemptAutoStartSync(self):
        db_dir = qt.QSettings().value("OpenLIFU/databaseDirectory")
        token = self.getValidToken()

        if token and db_dir and os.path.exists(db_dir):
            try:
                from openlifu.cloud.cloud import Cloud
                from OpenLIFULib.slicer_sync_runner import SlicerSyncRunner
                
                db_path = Path(db_dir).resolve()
                if not self._active_runner:
                    # Pass self as parent to anchor it to Slicer's memory
                    self._active_runner = SlicerSyncRunner(db_path, token)
                
                if self._active_runner:
                    self._active_runner.start()
                self._isServiceRunning = True
                logger.info(f"Connecting to Cloud service on {db_path}")                

            except Exception as e:
                logger.error(f"Cloud Initialization Error: {e}")

    def getValidToken(self):
        if not self._cloudTokens:
            savedRef = qt.QSettings().value("OpenLIFU/CloudRefreshToken")
            if not savedRef: return None
            self._cloudTokens = {"refreshToken": savedRef, "expiresAt": 0}
        
        if time.time() > (self._cloudTokens.get("expiresAt", 0) - 300):
            self.refreshCloudToken()
            
        return self._cloudTokens.get("idToken") if self._cloudTokens else None

    def refreshCloudToken(self):
        url = f"https://securetoken.googleapis.com/v1/token?key={self.apiKey}"
        try:
            r = requests.post(url, data={
                "grant_type": "refresh_token", 
                "refresh_token": self._cloudTokens['refreshToken']
            }, timeout=5)
            r.raise_for_status()
            data = r.json()
            
            self._cloudTokens["idToken"] = data['id_token']
            self._cloudTokens["expiresAt"] = time.time() + int(data['expires_in'])
        except Exception as e:
            logger.error(f"Token refresh failed: {e}")
            self._cloudTokens = None

    def login(self, email, password):
        """Authenticates user and saves refresh token."""
        url = f"https://identitytoolkit.googleapis.com/v1/accounts:signInWithPassword?key={self.apiKey}"
        try:
            r = requests.post(url, json={"email": email, "password": password, "returnSecureToken": True}, timeout=5)
            r.raise_for_status()
            data = r.json()
            self._cloudTokens = {
                "idToken": data['idToken'], 
                "refreshToken": data['refreshToken'],
                "expiresAt": time.time() + int(data['expiresIn'])
            }
            qt.QSettings().setValue("OpenLIFU/CloudRefreshToken", data['refreshToken'])
            qt.QTimer.singleShot(100, self.heartbeat)
            return True, "Success"
        except Exception as e:
            logger.error(f"Login error: {e}")
            return False, str(e)

    def cleanup(self):
        """Orderly shutdown of background cloud threads."""
        if self._active_runner:
            logger.info("Cleaning up cloud threads...")
            try:
                self._active_runner.stop()
                self._active_runner = None
            except Exception as e:
                logger.error(f"Error stopping Cloud Manager: {e}")
        self._isServiceRunning = False
        self._cloudManager = None

    def logout(self):
        self.cleanup()
        self._cloudTokens = None
        qt.QSettings().remove("OpenLIFU/CloudRefreshToken")