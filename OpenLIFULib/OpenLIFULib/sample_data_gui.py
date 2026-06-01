# Standard library imports
import json
import logging
import shutil
import tempfile
from enum import Enum
from pathlib import Path
from typing import Callable, List, Optional, Sequence, Tuple

# Third-party imports
import qt

# Slicer imports
import slicer

# OpenLIFULib imports
from OpenLIFULib import sample_data
from OpenLIFULib.guided_mode_util import get_guided_mode_state


logger = logging.getLogger(__name__)


class InitializationResult(Enum):
    READY_TO_LOAD = "ready_to_load"
    ASYNC_STARTED = "async_started"
    CANCELED = "canceled"


def initialize_missing_database(
    parent,
    path: Path,
    create_empty_database: Callable[[Path], None],
    setup_controller: "SampleDatabaseSetupController",
) -> InitializationResult:
    initialization_choice = select_database_initialization_option(parent, path)
    if initialization_choice is None:
        return InitializationResult.CANCELED

    if initialization_choice in ("sample", "starter"):
        archive_url, display_name, readme_url = sample_database_setup_info_for_initialization_choice(
            initialization_choice
        )
        if setup_controller.start_sample_database_setup(
            path,
            archive_url=archive_url,
            display_name=display_name,
            readme_url=readme_url,
        ):
            return InitializationResult.ASYNC_STARTED
        return InitializationResult.CANCELED

    create_empty_database(path)
    return InitializationResult.READY_TO_LOAD


def select_database_initialization_option(parent, path: Path) -> Optional[str]:
    message_box = qt.QMessageBox(parent)
    message_box.setIcon(qt.QMessageBox.Question)
    message_box.setWindowTitle("Initialize OpenLIFU database")
    message_box.setText(
        f"An OpenLIFU database was not found at the entered path ({str(path)})."
    )
    message_box.setInformativeText("Choose how to initialize the selected folder.")

    sample_button = message_box.addButton("Use Sample Data", qt.QMessageBox.ActionRole)
    sample_button.setToolTip(sample_data.SAMPLE_DATABASE_BUTTON_HELP_TEXT)
    starter_button = message_box.addButton("Create Starter Database", qt.QMessageBox.ActionRole)
    starter_button.setToolTip(sample_data.STARTER_DATABASE_BUTTON_HELP_TEXT)
    empty_button = message_box.addButton("Create Empty Database", qt.QMessageBox.ActionRole)
    empty_button.setToolTip("Create a completely empty database in the selected folder.")
    cancel_button = message_box.addButton(qt.QMessageBox.Cancel)
    message_box.setDefaultButton(sample_button)
    message_box.exec_()

    clicked_button = message_box.clickedButton()
    if clicked_button == sample_button:
        return "sample"
    if clicked_button == starter_button:
        return "starter"
    if clicked_button == empty_button:
        return "empty"
    if clicked_button == cancel_button:
        return None
    return None


def sample_database_setup_info_for_initialization_choice(initialization_choice: str) -> Tuple[str, str, str]:
    if initialization_choice == "starter":
        return (
            sample_data.STARTER_DATABASE_ARCHIVE_URL,
            "starter database",
            sample_data.STARTER_DATABASE_README_URL,
        )
    return (
        sample_data.SAMPLE_DATABASE_ARCHIVE_URL,
        "sample database",
        sample_data.SAMPLE_DATABASE_README_URL,
    )


def sample_database_cli_path() -> Path:
    return Path(sample_data.__file__).resolve().with_name("sample_data_cli.py")


class SampleDatabaseSetupController:
    def __init__(
        self,
        parent,
        path_line_edit,
        controls: Sequence[object],
        clear_database: Callable[[], None],
        load_database: Callable[[Path], None],
        suppress_dialogs_for_testing: bool = False,
    ) -> None:
        self.parent = parent
        self.path_line_edit = path_line_edit
        self.controls = list(controls)
        self.clear_database = clear_database
        self.load_database = load_database
        self.suppress_dialogs_for_testing = suppress_dialogs_for_testing

        self._process = None
        self._dialog = None
        self._destination = None
        self._work_dir = None
        self._cancel_file = None
        self._background_canceled_process = None
        self._stdout_buffer = ""
        self._stderr_buffer = ""
        self._diagnostics: List[str] = []
        self._child_succeeded = False
        self._child_error = ""
        self._process_error = ""
        self._was_canceled = False
        self._display_name = "sample database"
        self._readme_url = sample_data.SAMPLE_DATABASE_README_URL

    def is_active(self) -> bool:
        return self._process is not None

    def cleanup(self) -> None:
        if self._process is None:
            return

        self.cancel_sample_database_setup()

    def start_sample_database_setup(
        self,
        path: Path,
        archive_url: Optional[str] = None,
        display_name: str = "sample database",
        readme_url: str = sample_data.SAMPLE_DATABASE_README_URL,
        cli_path: Optional[Path] = None,
    ) -> bool:
        if self.is_active():
            slicer.util.warningDisplay("Sample database setup is already in progress.")
            return True
        if self._background_canceled_process is not None:
            self._display_sample_database_setup_warning(
                "The previous sample database setup is still canceling. Please wait a moment and try again."
            )
            return False

        destination = Path(path)
        try:
            sample_data.validate_sample_database_destination_can_install(destination)
        except Exception as exc:
            self.clear_database()
            self._set_database_path_border("red")
            self._display_sample_database_setup_error(
                f"Failed to create {display_name}:\n{exc}"
            )
            return False

        python_slicer = shutil.which("PythonSlicer")
        if python_slicer is None:
            self.clear_database()
            self._set_database_path_border("red")
            self._display_sample_database_setup_error(
                f"Failed to create {display_name}:\nPythonSlicer was not found on PATH."
            )
            return False

        cli_path = Path(cli_path) if cli_path is not None else sample_database_cli_path()
        if not cli_path.is_file():
            self.clear_database()
            self._set_database_path_border("red")
            self._display_sample_database_setup_error(
                f"Failed to create {display_name}:\nSample database helper was not found: {cli_path}"
            )
            return False

        self.clear_database()
        self._destination = destination
        self._display_name = display_name
        self._readme_url = readme_url
        self._destination.parent.mkdir(parents=True, exist_ok=True)
        self._work_dir = Path(
            tempfile.mkdtemp(
                prefix=f".{self._destination.name}-sample-download-",
                dir=self._destination.parent,
            )
        )
        self._cancel_file = self._work_dir / "cancel-requested"
        self._reset_output_state()
        self._set_database_path_border("yellow")
        self._set_controls_enabled(False)

        progress_dialog = qt.QProgressDialog(
            f"Downloading OpenLIFU {display_name}. This is a large download and may take several minutes.",
            "Cancel",
            0,
            0,
            self.parent,
        )
        progress_dialog.setWindowTitle(f"Creating OpenLIFU {display_name.title()}")
        progress_dialog.setAutoClose(False)
        progress_dialog.setAutoReset(False)
        progress_dialog.setMinimumDuration(0)
        progress_dialog.setWindowModality(qt.Qt.ApplicationModal)
        progress_dialog.setRange(0, 0)
        progress_dialog.canceled.connect(self.cancel_sample_database_setup)
        if not self.suppress_dialogs_for_testing:
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
            "--destination",
            str(self._destination),
            "--work-dir",
            str(self._work_dir),
            "--cancel-file",
            str(self._cancel_file),
        ]
        if archive_url is not None:
            args.extend(["--archive-url", archive_url])

        process.start(python_slicer, args)
        if not process.waitForStarted(3000):
            error_message = process.errorString() or "The sample database helper process failed to start."
            self._complete_sample_database_setup(False, error_message)
            return False
        return True

    def cancel_sample_database_setup(self) -> None:
        process = self._process
        if process is None:
            return

        self._was_canceled = True
        self.clear_database()
        self._request_child_cancel()
        if self._dialog is not None:
            self._dialog.setLabelText("Canceling sample database setup...")

        if process.state() == qt.QProcess.NotRunning:
            self._complete_sample_database_setup(False, canceled=True)
            return

        self._detach_canceled_process(process, self._work_dir)
        self._process = None
        self._work_dir = None
        self._cancel_file = None
        self._complete_sample_database_setup(False, canceled=True)

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

        logger.info("OpenLIFU sample database setup: %s", line)
        self._diagnostics.append(line)
        self._diagnostics = self._diagnostics[-40:]

    def _handle_stdout_line(self, line: str) -> None:
        line = line.strip()
        if not line:
            return

        if not line.startswith(sample_data.PROGRESS_LINE_PREFIX):
            self._append_diagnostic(line)
            return

        try:
            progress_event = json.loads(line[len(sample_data.PROGRESS_LINE_PREFIX):])
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
                    progress_event.get("error") or progress_event.get("message") or "Sample database setup failed."
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
            self._complete_sample_database_setup(False, canceled=True)
            return

        exit_code = args[0] if args else process.exitCode()
        if self._child_succeeded and not self._child_error and exit_code == 0:
            self._complete_sample_database_setup(True)
            return

        self._complete_sample_database_setup(False, self._failure_message(exit_code))

    def _failure_message(self, exit_code: int) -> str:
        if self._child_error:
            message = self._child_error
        elif self._process_error:
            message = self._process_error
        elif exit_code != 0:
            message = f"Sample database setup subprocess failed with exit code {exit_code}."
        else:
            message = "Sample database setup subprocess finished without reporting success."

        if self._diagnostics:
            message += "\n\nChild process output:\n" + "\n".join(self._diagnostics[-10:])
        return message

    def _complete_sample_database_setup(
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

        self._set_controls_enabled(True)

        path = self._destination
        self._destination = None

        work_dir = self._work_dir
        self._work_dir = None
        self._cancel_file = None
        if work_dir is not None:
            shutil.rmtree(work_dir, ignore_errors=True)

        if canceled:
            self.clear_database()
            self._set_database_path_border("red")
            self._reset_output_state()
            return

        if not succeeded:
            self.clear_database()
            self._set_database_path_border("red")
            self._display_sample_database_setup_error(
                f"Failed to create {self._display_name}:\n{error_message}"
            )
            self._reset_output_state()
            return

        try:
            self.load_database(path)
        except Exception as exc:
            self.clear_database()
            self._set_database_path_border("red")
            self._display_sample_database_setup_error(
                f"Failed to load {self._display_name}:\n{exc}"
            )
            self._reset_output_state()
            return

        self._reset_output_state()
        if not self.suppress_dialogs_for_testing:
            self.show_sample_database_setup_info()

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

    def _set_controls_enabled(self, enabled: bool) -> None:
        self.path_line_edit.enabled = enabled
        for control in self.controls:
            control.enabled = enabled

    def _set_database_path_border(self, color: str) -> None:
        line_edit = self.path_line_edit.findChild(qt.QLineEdit)
        if line_edit is not None:
            line_edit.setStyleSheet(f"border: 1px solid {color};")

    def _display_sample_database_setup_error(self, message: str) -> None:
        if not self.suppress_dialogs_for_testing:
            slicer.util.errorDisplay(message)

    def _display_sample_database_setup_warning(self, message: str) -> None:
        if not self.suppress_dialogs_for_testing:
            slicer.util.warningDisplay(message)

    def _reset_output_state(self) -> None:
        self._stdout_buffer = ""
        self._stderr_buffer = ""
        self._diagnostics = []
        self._child_succeeded = False
        self._child_error = ""
        self._process_error = ""
        self._was_canceled = False

    @staticmethod
    def open_external_url(url: str) -> bool:
        return qt.QDesktopServices.openUrl(qt.QUrl(url))

    def show_sample_database_setup_info(self) -> None:
        title = "Starter Database Ready" if self._display_name == "starter database" else "Sample Database Ready"

        message_box = qt.QMessageBox(self.parent)
        message_box.setIcon(qt.QMessageBox.Information)
        message_box.setWindowTitle(title)
        message_box.setText(f"The OpenLIFU {self._display_name} has been created and connected.")
        if get_guided_mode_state():
            message_box.setInformativeText(
                "To get started, sign in with username <b>example_admin</b> and password <b>example</b>."
                "<br><br>"
                "More details are available in the database README."
            )
            message_box.setTextFormat(qt.Qt.RichText)
            message_box.setTextInteractionFlags(qt.Qt.TextSelectableByMouse)
        readme_button = message_box.addButton("Open README", qt.QMessageBox.ActionRole)
        message_box.addButton(qt.QMessageBox.Ok)
        message_box.exec_()
        if message_box.clickedButton() == readme_button:
            if not self.open_external_url(self._readme_url):
                slicer.util.errorDisplay(f"Failed to open README URL:\n{self._readme_url}")
