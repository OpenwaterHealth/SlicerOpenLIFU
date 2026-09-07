import qt
import slicer
import time
import os
import signal
import logging
from pathlib import Path
from OpenLIFULib import ensure_python_requirements_for_module_enter
from OpenLIFULib.util import disconnect_signal_connections, display_errors
from slicer.ScriptedLoadableModule import *
from slicer.i18n import tr as _
from slicer.i18n import translate
logger = logging.getLogger('OpenLIFU.CloudSync')

# Global logic singleton
_sharedLogicInstance = None


def getCloudSyncLogic():
    global _sharedLogicInstance
    if _sharedLogicInstance is None or _sharedLogicInstance._closed:
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
        self.parent.categories = [
            translate("qSlicerAbstractCoreModule", "OpenLIFU")]
        self.parent.dependencies = ["OpenLIFUHome"]


class OpenLIFUCloudSyncWidget(ScriptedLoadableModuleWidget):
    def setup(self):
        ScriptedLoadableModuleWidget.setup(self)
        self._cleaned_up = False
        self._status_connections = []
        self.logic = getCloudSyncLogic()

        uiPath = os.path.join(os.path.dirname(__file__),
                              'Resources', 'UI', 'OpenLIFUCloudSync.ui')
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

        self._status_connections.append((self.logic.statusHelper.statusChanged, self.onCloudStatusChanged))
        self.logic.statusHelper.statusChanged.connect(self.onCloudStatusChanged)

        self.updateGUI()

    def cleanup(self):
        if self._cleaned_up:
            return
        self._cleaned_up = True
        disconnect_signal_connections(self._status_connections)
        self.logic.cleanup()
        self.logic = None

    def enter(self):
        ensure_python_requirements_for_module_enter()
        self.updateGUI()

    def onCloudStatusChanged(self, message, timestamp):
        """Thread-safe update of UI labels from background cloud events."""
        if self._cleaned_up:
            return
        slicer.util.showStatusMessage(f"Cloud: {message}", 3000)
        if hasattr(self.ui, 'lastSyncLabel'):
            self.ui.lastSyncLabel.text = timestamp

    def updateGUI(self):
        token = self.logic.getValidToken()
        isLoggedIn = token is not None
        self.ui.statusLabel.text = _(
            "Logged In") if isLoggedIn else _("Not Logged In")
        self.ui.loginButton.text = _(
            "Logout") if isLoggedIn else _("Login to Cloud")

    def onBack(self, checked=False):
        prev_module = slicer.util.mainWindow().property("OpenLIFU_PreviousModule")
        slicer.util.selectModule(
            prev_module if prev_module else "OpenLIFUHome")

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
        self.syncProcess = None
        self.apiKey = "AIzaSyBzPH2T6Cf17_KGeOSnncauJY2t1Lz4ndY"
        self._cloudTokens = None
        self._isServiceRunning = False
        self._active_runner = None
        self._closed = False
        self._process_connections = []
        # Instantiate signal bridge
        self.statusHelper = CloudStatusHelper()

        self.monitorTimer = qt.QTimer()
        self.monitorTimer.setInterval(50000)
        self.startupTimer = qt.QTimer()
        self.startupTimer.setSingleShot(True)
        self.startupTimer.setInterval(1000)
        self.heartbeatRequestTimer = qt.QTimer()
        self.heartbeatRequestTimer.setSingleShot(True)
        self.heartbeatRequestTimer.setInterval(100)
        self._timer_connections = [
            (self.monitorTimer.timeout, self.heartbeat),
            (self.startupTimer.timeout, self.startHeartbeat),
            (self.heartbeatRequestTimer.timeout, self.heartbeat),
        ]
        for timer_signal, callback in self._timer_connections:
            timer_signal.connect(callback)

        self._shutdown_connections = [(slicer.app.aboutToQuit, self.cleanup)]
        slicer.app.aboutToQuit.connect(self.cleanup)
        self._previous_sigint_handler = signal.signal(signal.SIGINT, self._handleTerminalInterrupt)
        self.startupTimer.start()

    def _handleTerminalInterrupt(self, signum, frame):
        """Ensures cleanup runs even if Ctrl+C is pressed in terminal."""
        logger.info("Terminal interrupt detected (Ctrl+C). Cleaning up...")
        self.cleanup()
        slicer.app.quit()

    def startHeartbeat(self):
        if self._closed:
            return
        self.monitorTimer.start()
        self.heartbeat()

    def heartbeat(self):
        if self._closed:
            return
        logger.info("Heartbeat: Checking Cloud Sync status...")
        token = self.getValidToken()
        if not self.syncProcess and token:
            self.attemptAutoStartSync()

    def _safeStatusUpdate(self, status):
        """Thread-safe bridge to emit UI updates from background threads."""
        if self._closed:
            return
        timestamp = time.strftime("%H:%M:%S")
        self.statusHelper.statusChanged.emit(status, timestamp)

    def attemptAutoStartSync(self):
        """Launches the background sync engine via QProcess."""
        import sys
        if self._closed:
            return
        if self.syncProcess and self.syncProcess.state() != qt.QProcess.NotRunning:
            return

        db_dir = qt.QSettings().value("OpenLIFU/databaseDirectory")
        refresh_token = qt.QSettings().value("OpenLIFU/CloudRefreshToken")
        token = self.getValidToken()

        if not token or not refresh_token or not db_dir:
            logger.warning("Sync failed: Missing token or database directory.")
            return

        moduleDir = os.path.dirname(__file__)
        scriptPath = os.path.abspath(os.path.join(
            moduleDir, "..", "bin", "OpenLIFUCloudSyncCLI.py"))

        if not os.path.exists(scriptPath):
            logging.error(f"Sync Engine not found at: {scriptPath}")
            return

        env = qt.QProcessEnvironment.systemEnvironment()
        env.insert("PYTHONPATH", os.pathsep.join(sys.path))

        self._stopSyncProcess()
        self.syncProcess = qt.QProcess()
        self.syncProcess.setProcessEnvironment(env)
        # Combine stdout and stderr for easier logging
        self.syncProcess.setProcessChannelMode(qt.QProcess.MergedChannels)

        self._process_connections = [
            (self.syncProcess.readyReadStandardOutput, self.onProcessOutput),
            (self.syncProcess.finished, self.onProcessFinished),
        ]
        for process_signal, callback in self._process_connections:
            process_signal.connect(callback)

        args = [scriptPath, "--db_path", db_dir, "--api_key",
                self.apiKey, "--refresh_token", refresh_token]
        self.syncProcess.start(sys.executable, args)
        logger.info("Cloud Sync Engine started via QProcess.")

    def onProcessOutput(self):
        """Captures real-time prints and logs from the child process."""
        if not self._closed and self.syncProcess:
            raw_data = self.syncProcess.readAllStandardOutput().data().decode()
            for line in raw_data.splitlines():
                line = line.strip()

                print(f"[CloudSync Engine]: {line}")

                if line.startswith("NEW_ID_TOKEN:"):
                    token = line.split(":", 1)[1]
                    if not self._cloudTokens:
                        self._cloudTokens = {}
                    if self._cloudTokens:
                        self._cloudTokens["idToken"] = token
                    logger.info("Logic memory updated with fresh ID token.")

                elif line.startswith("NEW_EXPIRY:"):
                    expiry = line.split(":", 1)[1]
                    if not self._cloudTokens:
                        self._cloudTokens = {}
                    self._cloudTokens["expiresAt"] = float(expiry)
                    logger.info(
                        f"Logic memory updated with new expiry: {expiry}")

                elif line.startswith("SYNC_COMPLETED_AT:"):
                    timestamp = line.split(":", 1)[1]
                    self.statusHelper.statusChanged.emit("Idle", timestamp)

    def onProcessFinished(self, exitStatus):
        if self._closed:
            return
        logger.info(f"Sync Engine stopped. Exit code: {exitStatus}")
        self._safeStatusUpdate("Sync Stopped")

    def _stopSyncProcess(self):
        """Stop syncing without disabling future login or heartbeat activity."""
        disconnect_signal_connections(self._process_connections)
        process = self.syncProcess
        self.syncProcess = None
        if process is None:
            return
        try:
            if process.state() != qt.QProcess.NotRunning:
                logger.info("Stopping background sync engine...")
                process.terminate()
                if not process.waitForFinished(2000):
                    process.kill()
                    if not process.waitForFinished(2000):
                        logger.warning("Cloud sync process did not stop after being killed.")
        finally:
            process.deleteLater()

    def cleanup(self):
        """Release the shared service before Python finalization or module reload."""
        global _sharedLogicInstance
        if self._closed:
            return
        self._closed = True
        for timer in (self.startupTimer, self.monitorTimer, self.heartbeatRequestTimer):
            timer.stop()
        disconnect_signal_connections(self._timer_connections)
        self.startupTimer = self.monitorTimer = self.heartbeatRequestTimer = None
        try:
            self._stopSyncProcess()
        except Exception:
            logger.warning("Could not stop cloud sync during cleanup", exc_info=True)
        finally:
            disconnect_signal_connections(self._shutdown_connections)
            if signal.getsignal(signal.SIGINT) == self._handleTerminalInterrupt:
                signal.signal(signal.SIGINT, self._previous_sigint_handler)
            self._previous_sigint_handler = None
            self.statusHelper = None
            if _sharedLogicInstance is self:
                _sharedLogicInstance = None

    def getValidToken(self):
        if not self._cloudTokens:
            savedRef = qt.QSettings().value("OpenLIFU/CloudRefreshToken")
            if not savedRef:
                return None
            self._cloudTokens = {"refreshToken": savedRef, "expiresAt": 0}

        if time.time() > (self._cloudTokens.get("expiresAt", 0) - 300):
            self.refreshCloudToken()

        return self._cloudTokens.get("idToken") if self._cloudTokens else None

    def refreshCloudToken(self):
        import requests

        url = f"https://securetoken.googleapis.com/v1/token?key={self.apiKey}"
        try:
            r = requests.post(url, data={
                "grant_type": "refresh_token",
                "refresh_token": self._cloudTokens['refreshToken']
            }, timeout=5)
            r.raise_for_status()
            data = r.json()

            self._cloudTokens["idToken"] = data['id_token']
            self._cloudTokens["expiresAt"] = time.time() + \
                int(data['expires_in'])
        except Exception as e:
            logger.error(f"Token refresh failed: {e}")
            self._cloudTokens = None

    def login(self, email, password):
        """Authenticates user and saves refresh token."""
        if self._closed:
            raise RuntimeError("Cloud Sync logic has been cleaned up.")
        import requests

        url = f"https://identitytoolkit.googleapis.com/v1/accounts:signInWithPassword?key={self.apiKey}"
        try:
            r = requests.post(url, json={
                              "email": email, "password": password, "returnSecureToken": True}, timeout=5)
            r.raise_for_status()
            data = r.json()
            self._cloudTokens = {
                "idToken": data['idToken'],
                "refreshToken": data['refreshToken'],
                "expiresAt": time.time() + int(data['expiresIn'])
            }
            qt.QSettings().setValue(
                "OpenLIFU/CloudRefreshToken", data['refreshToken'])
            self.heartbeatRequestTimer.start()
            return True, "Success"
        except Exception as e:
            logger.error(f"Login error: {e}")
            return False, str(e)

    def logout(self):
        self._stopSyncProcess()
        self._cloudTokens = None
        qt.QSettings().remove("OpenLIFU/CloudRefreshToken")
