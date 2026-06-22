import json
import logging
import shutil
import tempfile
from pathlib import Path
from typing import Callable, List, Optional

import qt
import slicer

from OpenLIFULib.meshroom_install_cli import PROGRESS_LINE_PREFIX

logger = logging.getLogger(__name__)

MESHROOM_VERSION = "2025.1.0"
MESHROOM_WINDOWS_URL = "https://zenodo.org/records/16887472/files/Meshroom-2025.1.0-Windows.zip"
MESHROOM_LINUX_URL = "https://zenodo.org/records/16887472/files/Meshroom-2025.1.0-Linux.tar.gz"
MESHROOM_EXTRACTED_DIR_NAME = "Meshroom-2025.1.0"


def meshroom_install_cli_path() -> Path:
    return Path(__file__).resolve().with_name("meshroom_install_cli.py")


class MeshroomInstallController:
    def __init__(
        self,
        parent,
        on_finished: Callable[[bool], None],
    ) -> None:
        self.parent = parent
        self._on_finished_callback = on_finished

        self._process: Optional[qt.QProcess] = None
        self._dialog: Optional[qt.QProgressDialog] = None
        self._destination: Optional[Path] = None
        self._work_dir: Optional[Path] = None
        self._cancel_file: Optional[Path] = None
        self._background_canceled_process: Optional[qt.QProcess] = None
        self._reset_output_state()

    def is_active(self) -> bool:
        return self._process is not None

    def cleanup(self) -> None:
        if self._process is not None:
            self.cancel_install()

    def start_install(self, destination: Path, archive_url: str) -> bool:
        if self.is_active():
            slicer.util.warningDisplay("Meshroom installation is already in progress.")
            return False
        if self._background_canceled_process is not None:
            slicer.util.warningDisplay(
                "The previous Meshroom installation is still canceling. Please wait a moment and try again."
            )
            return False

        python_slicer = shutil.which("PythonSlicer")
        if python_slicer is None:
            slicer.util.errorDisplay("PythonSlicer was not found on PATH.")
            return False

        cli_path = meshroom_install_cli_path()
        if not cli_path.is_file():
            slicer.util.errorDisplay(f"Meshroom install helper was not found: {cli_path}")
            return False

        self._destination = destination
        self._destination.mkdir(parents=True, exist_ok=True)
        self._work_dir = Path(
            tempfile.mkdtemp(
                prefix=".meshroom-install-",
                dir=self._destination,
            )
        )
        self._cancel_file = self._work_dir / "cancel-requested"
        self._reset_output_state()

        progress_dialog = qt.QProgressDialog(
            "Downloading Meshroom. This is a large download and may take several minutes.",
            "Cancel",
            0,
            0,
            self.parent,
        )
        progress_dialog.setWindowTitle("Installing Meshroom")
        progress_dialog.setAutoClose(False)
        progress_dialog.setAutoReset(False)
        progress_dialog.setMinimumDuration(0)
        progress_dialog.setWindowModality(qt.Qt.ApplicationModal)
        progress_dialog.setRange(0, 0)
        progress_dialog.canceled.connect(self.cancel_install)
        progress_dialog.show()
        slicer.app.processEvents()
        self._dialog = progress_dialog

        process = qt.QProcess()
        process.readyReadStandardOutput.connect(self._on_stdout_ready)
        process.readyReadStandardError.connect(self._on_stderr_ready)
        process.finished.connect(self._on_finished)
        process.errorOccurred.connect(self._on_process_error)
        self._process = process

        args = [
            str(cli_path),
            "--destination", str(self._destination),
            "--work-dir", str(self._work_dir),
            "--archive-url", archive_url,
            "--cancel-file", str(self._cancel_file),
        ]

        process.start(python_slicer, args)
        if not process.waitForStarted(3000):
            error_message = process.errorString() or "The Meshroom install helper process failed to start."
            self._complete_install(False, error_message)
            return False
        return True

    def cancel_install(self) -> None:
        process = self._process
        if process is None:
            return

        self._was_canceled = True
        self._request_child_cancel()
        if self._dialog is not None:
            self._dialog.setLabelText("Canceling Meshroom installation...")

        if process.state() == qt.QProcess.NotRunning:
            self._complete_install(False, canceled=True)
            return

        self._detach_canceled_process(process, self._work_dir)
        self._process = None
        self._work_dir = None
        self._cancel_file = None
        self._complete_install(False, canceled=True)

    def _request_child_cancel(self) -> None:
        cancel_file = self._cancel_file
        if cancel_file is None:
            return
        try:
            cancel_file.parent.mkdir(parents=True, exist_ok=True)
            cancel_file.write_text("cancel\n", encoding="utf-8")
        except Exception as exc:
            self._append_diagnostic(f"Could not write cancel file: {exc}")

    def _detach_canceled_process(self, process, work_dir: Optional[Path]) -> None:
        self._disconnect_process_signals(process)
        self._background_canceled_process = process

        def cleanup_detached_process(*args, detached_process=process, detached_work_dir=work_dir):
            self._cleanup_detached_process(detached_process, detached_work_dir)

        process.finished.connect(cleanup_detached_process)
        if process.state() == qt.QProcess.NotRunning:
            cleanup_detached_process()

    def _cleanup_detached_process(self, process, work_dir: Optional[Path]) -> None:
        try:
            process.finished.disconnect()
        except Exception:
            pass
        if self._background_canceled_process is process:
            self._background_canceled_process = None
        process.deleteLater()
        if work_dir is not None:
            shutil.rmtree(work_dir, ignore_errors=True)

    def _on_stdout_ready(self) -> None:
        process = self._process
        if process is None:
            return
        self._stdout_buffer += process.readAllStandardOutput().data().decode("utf-8", errors="replace")
        self._consume_stdout_lines()

    def _on_stderr_ready(self) -> None:
        process = self._process
        if process is None:
            return
        self._stderr_buffer += process.readAllStandardError().data().decode("utf-8", errors="replace")
        self._consume_stderr_lines()

    def _consume_stdout_lines(self, flush: bool = False) -> None:
        while "\n" in self._stdout_buffer:
            line, self._stdout_buffer = self._stdout_buffer.split("\n", 1)
            self._handle_stdout_line(line.rstrip("\r"))
        if flush and self._stdout_buffer:
            line = self._stdout_buffer
            self._stdout_buffer = ""
            self._handle_stdout_line(line.rstrip("\r"))

    def _consume_stderr_lines(self, flush: bool = False) -> None:
        while "\n" in self._stderr_buffer:
            line, self._stderr_buffer = self._stderr_buffer.split("\n", 1)
            self._append_diagnostic(line.rstrip("\r"))
        if flush and self._stderr_buffer:
            line = self._stderr_buffer
            self._stderr_buffer = ""
            self._append_diagnostic(line.rstrip("\r"))

    def _append_diagnostic(self, line: str) -> None:
        line = line.strip()
        if not line:
            return
        logger.info("Meshroom install: %s", line)
        self._diagnostics.append(line)
        self._diagnostics = self._diagnostics[-40:]

    def _handle_stdout_line(self, line: str) -> None:
        line = line.strip()
        if not line:
            return
        if not line.startswith(PROGRESS_LINE_PREFIX):
            self._append_diagnostic(line)
            return
        try:
            progress_event = json.loads(line[len(PROGRESS_LINE_PREFIX):])
        except json.JSONDecodeError as exc:
            self._append_diagnostic(f"Could not parse progress line: {exc}: {line}")
            return

        message = str(progress_event.get("message") or "")
        value = int(progress_event.get("value") or 0)
        maximum = int(progress_event.get("maximum") or 0)
        if message:
            self._update_progress(message, value, maximum)

        if "success" in progress_event:
            if progress_event["success"]:
                self._child_succeeded = True
            else:
                self._child_error = str(
                    progress_event.get("error") or progress_event.get("message") or "Meshroom installation failed."
                )

    def _on_process_error(self, process_error) -> None:
        process = self._process
        if process is not None:
            self._process_error = process.errorString()

    def _update_progress(self, message: str, value: int, maximum: int) -> None:
        progress_dialog = self._dialog
        if progress_dialog is None:
            return
        if maximum <= 0:
            progress_dialog.setRange(0, 0)
        else:
            progress_dialog.setRange(0, maximum)
            progress_dialog.setValue(value)
        progress_dialog.setLabelText(message)

    def _on_finished(self, *args) -> None:
        process = self._process
        if process is None:
            return

        self._on_stdout_ready()
        self._on_stderr_ready()
        self._consume_stdout_lines(flush=True)
        self._consume_stderr_lines(flush=True)

        if self._was_canceled:
            self._complete_install(False, canceled=True)
            return

        exit_code = args[0] if args else process.exitCode()
        if self._child_succeeded and not self._child_error and exit_code == 0:
            self._complete_install(True)
            return

        self._complete_install(False, self._failure_message(exit_code))

    def _failure_message(self, exit_code: int) -> str:
        if self._child_error:
            message = self._child_error
        elif self._process_error:
            message = self._process_error
        elif exit_code != 0:
            message = f"Meshroom install subprocess failed with exit code {exit_code}."
        else:
            message = "Meshroom install subprocess finished without reporting success."
        if self._diagnostics:
            message += "\n\nChild process output:\n" + "\n".join(self._diagnostics[-10:])
        return message

    def _complete_install(
        self,
        succeeded: bool,
        error_message: str = "",
        canceled: bool = False,
    ) -> None:
        process = self._process
        self._process = None
        if process is not None:
            process.deleteLater()

        if self._dialog is not None:
            self._dialog.close()
            self._dialog = None

        self._destination = None

        work_dir = self._work_dir
        self._work_dir = None
        self._cancel_file = None
        if work_dir is not None:
            shutil.rmtree(work_dir, ignore_errors=True)

        if canceled:
            self._reset_output_state()
            self._on_finished_callback(False)
            return

        if not succeeded:
            slicer.util.errorDisplay(f"Failed to install Meshroom:\n{error_message}")
            self._reset_output_state()
            self._on_finished_callback(False)
            return

        self._reset_output_state()
        self._on_finished_callback(True)

    def _disconnect_process_signals(self, process) -> None:
        for signal, slot in (
            (process.readyReadStandardOutput, self._on_stdout_ready),
            (process.readyReadStandardError, self._on_stderr_ready),
            (process.finished, self._on_finished),
            (process.errorOccurred, self._on_process_error),
        ):
            try:
                signal.disconnect(slot)
            except Exception:
                pass

    def _reset_output_state(self) -> None:
        self._stdout_buffer = ""
        self._stderr_buffer = ""
        self._diagnostics: List[str] = []
        self._child_succeeded = False
        self._child_error = ""
        self._process_error = ""
        self._was_canceled = False
