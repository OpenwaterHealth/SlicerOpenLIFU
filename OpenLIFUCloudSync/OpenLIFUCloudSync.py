import qt
import slicer
import time
import os
import requests
import signal
import logging
from typing import Callable, List
from slicer.ScriptedLoadableModule import *
from slicer.i18n import tr as _
from slicer.i18n import translate
logger = logging.getLogger('OpenLIFU.CloudSync')


_sharedLogicInstance = None
def getCloudSyncLogic():
    global _sharedLogicInstance
    if _sharedLogicInstance is None:
        _sharedLogicInstance = OpenLIFUCloudSyncLogic()
    return _sharedLogicInstance


class CloudStatusHelper(qt.QObject):
    # Signal carries: (statusMessage, timestamp)
    statusChanged = qt.Signal(str, str)
    # Generic state-changed signal (login / service running / enabled flags)
    stateChanged = qt.Signal()
    environmentChanged = qt.Signal(str)

    def __init__(self):
        super().__init__()


class OpenLIFUCloudSync(ScriptedLoadableModule):
    def __init__(self, parent):
        ScriptedLoadableModule.__init__(self, parent)
        self.parent.title = _("OpenLIFU Cloud Sync")
        self.parent.categories = [
            translate("qSlicerAbstractCoreModule", "OpenLIFU")]
        self.parent.dependencies = ["OpenLIFUHome"]
        # Deprecated: cloud-sync controls now live inside the Database popup
        # on the Data page. The logic class below is still the source of
        # truth for the background sync engine, but this standalone module
        # is hidden from the module selector.
        self.parent.hidden = True
        self.parent.contributors = ["Andrew Howe (Kitware), Erik (NVP Software"]
        # short description of the module and a link to online module documentation
        # _() function marks text as translatable to other languages
        self.parent.helpText = _(
            "This is the database module of the OpenLIFU extension for focused ultrasound. "
            "More information at <a href=\"https://github.com/OpenwaterHealth/SlicerOpenLIFU\">github.com/OpenwaterHealth/SlicerOpenLIFU</a>."
        )
        # organization, grant, and thanks
        self.parent.acknowledgementText = _(
            "This is part of Openwater's OpenLIFU, an open-source "
            "hardware and software platform for Low Intensity Focused Ultrasound (LIFU) research "
            "and development."
        )


class OpenLIFUCloudSyncWidget(ScriptedLoadableModuleWidget):
    """Minimal placeholder widget. The cloud-sync UI now lives in the
    Database popup; this widget exists only so the module can still be
    instantiated by Slicer without errors.
    """

    def setup(self):
        ScriptedLoadableModuleWidget.setup(self)
        # Make sure the singleton logic is alive so QSettings-based
        # autostart on boot still works.
        getCloudSyncLogic()

        label = qt.QLabel(
            _("Cloud Sync controls have moved into the Database popup on the Data page.")
        )
        label.setWordWrap(True)
        self.layout.addWidget(label)
        self.layout.addStretch(1)


class OpenLIFUCloudSyncLogic(ScriptedLoadableModuleLogic):

    # QSettings keys
    _SETTING_REFRESH_TOKEN = "OpenLIFU/CloudRefreshToken"
    _SETTING_SERVICE_ENABLED = "OpenLIFU/CloudSyncEnabled"
    _SETTING_LAST_SYNC = "OpenLIFU/CloudLastSync"
    _SETTING_CLOUD_EMAIL = "OpenLIFU/CloudAccountEmail"

    def __init__(self):
        ScriptedLoadableModuleLogic.__init__(self)
        self.syncProcess = None
        self._cloudTokens = None
        self._last_sync_timestamp = qt.QSettings().value(self._SETTING_LAST_SYNC, "") or ""
        self._last_status_message = ""
        self._service_failed = False
        self.previousEnvironment = qt.QSettings().value("OpenLIFU/CloudEnvironment", "prod")
        self.environment = self.getEnvironment()
        self.apiKey = "AIzaSyA45zDuDfjpkgmnszo5SRsoLdL4mJqgA8E" if self.environment == 'dev' else "AIzaSyBzPH2T6Cf17_KGeOSnncauJY2t1Lz4ndY"

        if self.environment != self.previousEnvironment:
            qt.QSettings().setValue("OpenLIFU/CloudEnvironment", self.environment)
            logger.info(f"Cloud environment set to '{self.environment}' and saved to settings.")
            self.logout()

        # Instantiate signal bridge
        self.statusHelper = CloudStatusHelper()

        self._state_callbacks: List[Callable[[], None]] = []

        # Cleanup connections for both graceful and terminal exits
        slicer.app.connect("aboutToQuit()", self.cleanup)
        try:
            signal.signal(signal.SIGINT, self._handleTerminalInterrupt)
        except (ValueError, OSError):
            # signal.signal can only be called from the main thread; ignore
            # if the logic is instantiated outside of it.
            pass

        # Defer heartbeat startup
        qt.QTimer.singleShot(1000, self.startHeartbeat)
        self.statusHelper.environmentChanged.emit(self.environment)

    # ---------------- Service-enabled flag ----------------

    def is_service_enabled(self) -> bool:
        """True if the user has opted in to running the background sync
        service. Persisted across sessions in QSettings."""
        v = qt.QSettings().value(self._SETTING_SERVICE_ENABLED, False)
        # QSettings on some platforms stores bools as strings.
        if isinstance(v, str):
            return v.lower() in ("1", "true", "yes")
        return bool(v)

    def set_service_enabled(self, enabled: bool) -> None:
        """Toggle the cloud-sync service. When disabled, any running
        background process is stopped immediately. Credentials are NOT
        cleared by this call."""
        qt.QSettings().setValue(self._SETTING_SERVICE_ENABLED, bool(enabled))
        if enabled:
            self._service_failed = False
            qt.QTimer.singleShot(100, self.heartbeat)
        else:
            self._stopSyncProcess()
        self._notifyStateChanged()

    # ---------------- State observers ----------------

    def call_on_state_changed(self, f: Callable[[], None]) -> None:
        self._state_callbacks.append(f)

    def remove_state_changed_callback(self, f: Callable[[], None]) -> None:
        """Unregister a callback previously registered via
        :py:meth:`call_on_state_changed`. Silently no-ops if absent.
        """
        try:
            self._state_callbacks.remove(f)
        except ValueError:
            pass

    def _notifyStateChanged(self) -> None:
        for f in list(self._state_callbacks):
            try:
                f()
            except Exception as e:  # noqa: BLE001
                logger.warning(f"Cloud-sync state callback failed: {e}")
        try:
            self.statusHelper.stateChanged.emit()
        except Exception:  # noqa: BLE001
            pass

    # ---------------- Status accessors ----------------

    def is_logged_in(self) -> bool:
        """True iff a refresh token is stored. The token may still need
        refreshing before any API call."""
        return bool(qt.QSettings().value(self._SETTING_REFRESH_TOKEN))

    def is_service_running(self) -> bool:
        return (
            self.syncProcess is not None
            and self.syncProcess.state() != qt.QProcess.NotRunning
        )

    def did_service_fail(self) -> bool:
        """True if the service was enabled and a launch attempt failed (or the
        process exited unexpectedly) without a subsequent successful start."""
        return self._service_failed

    def get_last_sync_timestamp(self) -> str:
        return self._last_sync_timestamp or ""

    def get_status_message(self) -> str:
        return self._last_status_message or ""

    # ---------------- Heartbeat / process supervision ----------------

    def _handleTerminalInterrupt(self, signum, frame):
        """Ensures cleanup runs even if Ctrl+C is pressed in terminal."""
        logger.info("Terminal interrupt detected (Ctrl+C). Cleaning up...")
        self.cleanup()
        slicer.app.quit()

    def getEnvironment(self):
        self.environment = os.getenv("OPENLIFU_CLOUD_ENV", "prod").lower()
        if self.environment not in ["prod", "dev"]:
            self.environment = "prod"
        
        return self.environment

    def startHeartbeat(self):
        self.monitorTimer = qt.QTimer()
        self.monitorTimer.timeout.connect(self.heartbeat)
        self.monitorTimer.start(50000)
        self.heartbeat()

    def heartbeat(self):
        logger.info("Heartbeat: Checking Cloud Sync status...")
        if not self.is_service_enabled():
            return
        token = self.getValidToken()
        if not self.syncProcess and token:
            self.attemptAutoStartSync()
    
    def _safeStatusUpdate(self, status):
        """Thread-safe bridge to emit UI updates from background threads."""
        timestamp = time.strftime("%H:%M:%S")
        self._last_status_message = status
        self.statusHelper.statusChanged.emit(status, timestamp)
        self._notifyStateChanged()

    def attemptAutoStartSync(self):
        import sys
        """Launches the background sync engine via QProcess."""
        if self.syncProcess and self.syncProcess.state() != qt.QProcess.NotRunning:
            return

        if not self.is_service_enabled():
            return

        db_dir = qt.QSettings().value("OpenLIFU/databaseDirectory")
        refresh_token = qt.QSettings().value(self._SETTING_REFRESH_TOKEN)
        token = self.getValidToken()

        if not token or not refresh_token or not db_dir:
            logger.warning("Sync failed: Missing token or database directory.")
            self._service_failed = True
            self._notifyStateChanged()
            return

        moduleDir = os.path.dirname(__file__)
        # In a packaged / CMake-built layout the CLI is installed to
        # ``<lib>/bin/OpenLIFUCloudSyncCLI.py`` (a sibling of the
        # ``qt-scripted-modules`` directory that holds this file). When
        # running from the source tree it lives in the
        # ``OpenLIFUCloudSyncEngine`` subfolder. Try both.
        candidatePaths = [
            os.path.abspath(os.path.join(moduleDir, "..", "bin", "OpenLIFUCloudSyncCLI.py")),
            os.path.abspath(os.path.join(moduleDir, "OpenLIFUCloudSyncEngine", "OpenLIFUCloudSyncCLI.py")),
        ]
        scriptPath = next((p for p in candidatePaths if os.path.exists(p)), None)

        if scriptPath is None:
            logging.error(
                "Sync Engine not found. Looked in: " + ", ".join(candidatePaths)
            )
            self._service_failed = True
            self._notifyStateChanged()
            return

        env = qt.QProcessEnvironment.systemEnvironment()
        env.insert("PYTHONPATH", os.pathsep.join(sys.path))

        self.syncProcess = qt.QProcess()
        self.syncProcess.setProcessEnvironment(env)
        # Combine stdout and stderr for easier logging
        self.syncProcess.setProcessChannelMode(qt.QProcess.MergedChannels)

        self.syncProcess.readyReadStandardOutput.connect(self.onProcessOutput)
        self.syncProcess.finished.connect(self.onProcessFinished)

        args = [scriptPath, "--db_path", db_dir, "--api_key",
                self.apiKey, "--refresh_token", refresh_token, "--env", self.environment]
        self.syncProcess.start(sys.executable, args)
        logger.info("Cloud Sync Engine started via QProcess.")
        self._service_failed = False
        self._last_status_message = "Starting"
        self._notifyStateChanged()

    def onProcessOutput(self):
        """Captures real-time prints and logs from the child process."""
        if self.syncProcess:
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
                    self._last_sync_timestamp = timestamp
                    qt.QSettings().setValue(self._SETTING_LAST_SYNC, timestamp)
                    self._last_status_message = "Idle"
                    self.statusHelper.statusChanged.emit("Idle", timestamp)
                    self._notifyStateChanged()

    def onProcessFinished(self, exitStatus):
        logger.info(f"Sync Engine stopped. Exit code: {exitStatus}")
        # If the user still wants the service enabled, the unexpected exit
        # is a failure; otherwise it was an explicit stop.
        if self.is_service_enabled():
            self._service_failed = True
        self._safeStatusUpdate("Sync Stopped")

    def _stopSyncProcess(self) -> None:
        """Terminate the background sync process if it is running.

        Uses a graceful three-stage shutdown so we don't end up with the
        Qt warning "QProcess: Destroyed while process is still running":

        1. Ask the engine to shut down by writing ``STOP\\n`` and closing
           its stdin. The engine watches stdin and runs ``cloud.stop()``
           cleanly when it sees EOF or the STOP sentinel.
        2. If it doesn't exit within 5 s, fall back to ``terminate()``.
        3. If it still hasn't exited after another 2 s, ``kill()`` it and
           wait for the OS to reap the process before dropping the
           QProcess reference.
        """
        proc = self.syncProcess
        if proc is None or proc.state() == qt.QProcess.NotRunning:
            self.syncProcess = None
            return

        logger.info("Stopping background sync engine...")
        self._safeStatusUpdate("Stopping...")
        try:
            # Stage 1: graceful stdin handshake.
            try:
                proc.write(b"STOP\n")
                proc.closeWriteChannel()
            except Exception as e:  # noqa: BLE001
                logger.debug(f"Could not send STOP to sync engine: {e}")

            if proc.waitForFinished(5000):
                return

            # Stage 2: terminate.
            logger.warning(
                "Sync engine did not stop after STOP request; terminating.")
            proc.terminate()
            if proc.waitForFinished(2000):
                return

            # Stage 3: hard kill, and wait so the QProcess wrapper isn't
            # destroyed while the OS process is still running.
            logger.warning(
                "Sync engine did not respond to terminate; killing.")
            proc.kill()
            proc.waitForFinished(2000)
        finally:
            self.syncProcess = None
            self._safeStatusUpdate("Sync stopped.")

    def cleanup(self):
        """Gracefully kill the background process."""
        self._stopSyncProcess()

    def getValidToken(self):
        if not self._cloudTokens:
            savedRef = qt.QSettings().value(self._SETTING_REFRESH_TOKEN)
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

    # Map Firebase Identity Toolkit error codes to user-friendly text.
    # See https://firebase.google.com/docs/reference/rest/auth#section-sign-in-email-password
    _LOGIN_ERROR_MESSAGES = {
        "EMAIL_NOT_FOUND": "No account with that email was found.",
        "INVALID_PASSWORD": "Incorrect password.",
        "INVALID_LOGIN_CREDENTIALS": "Incorrect email or password.",
        "INVALID_EMAIL": "The email address is not valid.",
        "MISSING_PASSWORD": "Please enter a password.",
        "MISSING_EMAIL": "Please enter an email.",
        "USER_DISABLED": "This account has been disabled by an administrator.",
        "TOO_MANY_ATTEMPTS_TRY_LATER":
            "Too many failed attempts. Please wait and try again.",
        "OPERATION_NOT_ALLOWED": "Password sign-in is disabled for this project.",
    }

    def _friendly_login_error(self, response, exc) -> str:
        """Translate a Firebase auth response (or transport exception) into
        a one-line user-readable message."""
        # Network / no-response failure.
        if response is None:
            if isinstance(exc, requests.exceptions.Timeout):
                return "Login timed out. Check your network connection."
            if isinstance(exc, requests.exceptions.ConnectionError):
                return "Could not reach the cloud login server. Check your network connection."
            return f"Login failed: {exc}"
        # Try to parse the Firebase error envelope.
        code = None
        try:
            payload = response.json()
            err = payload.get("error", {}) if isinstance(payload, dict) else {}
            raw = err.get("message", "") if isinstance(err, dict) else ""
            # Firebase sometimes appends details after a colon, e.g.
            # "TOO_MANY_ATTEMPTS_TRY_LATER : ...". Strip to the first token.
            code = raw.split(":", 1)[0].strip() if raw else None
        except ValueError:
            code = None
        mapped = self._LOGIN_ERROR_MESSAGES.get(code) if code else None
        if mapped:
            return mapped
        if response.status_code == 400:
            return "Login failed: invalid credentials or request was rejected by the server."
        if 500 <= response.status_code < 600:
            return f"Cloud login server error ({response.status_code}). Please try again later."
        return f"Login failed ({response.status_code})."

    def login(self, email, password):
        """Authenticates user and saves refresh token.

        Returns ``(success, message)``. On failure, ``message`` is a short
        user-readable string suitable for display in a popup or status bar.
        """
        url = f"https://identitytoolkit.googleapis.com/v1/accounts:signInWithPassword?key={self.apiKey}"
        response = None
        try:
            response = requests.post(url, json={
                              "email": email, "password": password, "returnSecureToken": True}, timeout=5)
            response.raise_for_status()
            data = response.json()
            self._cloudTokens = {
                "idToken": data['idToken'],
                "refreshToken": data['refreshToken'],
                "expiresAt": time.time() + int(data['expiresIn'])
            }
            qt.QSettings().setValue(
                self._SETTING_REFRESH_TOKEN, data['refreshToken'])
            qt.QSettings().setValue(
                self._SETTING_CLOUD_EMAIL, email)
            self._service_failed = False
            qt.QTimer.singleShot(100, self.heartbeat)
            self._notifyStateChanged()
            return True, "Success"
        except Exception as e:
            friendly = self._friendly_login_error(response, e)
            logger.error(f"Login error: {friendly} (raw: {e})")
            return False, friendly

    def forget_credentials(self):
        """Clear stored credentials. Stops the service (since it can't run
        without a refresh token) but does NOT toggle the service-enabled
        flag, so the next successful login will resume autostart."""
        self._stopSyncProcess()
        self._cloudTokens = None
        qt.QSettings().remove(self._SETTING_REFRESH_TOKEN)
        qt.QSettings().remove(self._SETTING_CLOUD_EMAIL)
        self._notifyStateChanged()

    def logout(self):
        """Backwards-compatible alias for forget_credentials."""
        self.forget_credentials()
