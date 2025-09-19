import qt
from pathlib import Path
from typing import Optional, Tuple

DOWNLOAD_ACTION = "download"
BROWSE_ACTION = "browse"

class InstallAssetDialog(qt.QDialog):
    """
    A dialog that asks the user to either download a missing file automatically
    or browse for it on their local disk.
    """
    def __init__(self, filename_to_install: str, parent=None):
        """
        Initializes the dialog.

        Args:
            filename_to_install (str): The name of the file that needs to be installed.
            parent: The parent widget, typically the Slicer main window.
        """
        super().__init__(parent)
        self.setWindowTitle("Install missing file")
        self.setModal(True)
        
        self.expected_filename = filename_to_install

        # member variables to store the result
        self._result_action: str = ""
        self._selected_filepath: Optional[str] = None
        
        layout = qt.QVBoxLayout(self)

        message = (
            f"The file '{filename_to_install}' needs to be installed.\n\n"
            "Would you like to automatically download it or browse for a local copy?"
        )
        self.info_label = qt.QLabel(message)
        layout.addWidget(self.info_label)

        # buttons
        self.button_box = qt.QDialogButtonBox(self)
        self.download_button = self.button_box.addButton("Download and Install", qt.QDialogButtonBox.ActionRole)
        self.browse_button = self.button_box.addButton("Browse and Install", qt.QDialogButtonBox.ActionRole)
        self.cancel_button = self.button_box.addButton(qt.QDialogButtonBox.Cancel)
        layout.addWidget(self.button_box)

        # connections
        self.download_button.clicked.connect(self._on_download_clicked)
        self.browse_button.clicked.connect(self._on_browse_clicked)
        self.cancel_button.clicked.connect(lambda : self.reject())

    def _on_download_clicked(self):
        """Sets the result action to 'download' and accepts the dialog."""
        self._result_action = DOWNLOAD_ACTION
        self.accept()

    def _on_browse_clicked(self):
        """
        Opens a file dialog, validates the selected filename, and if it's
        correct, accepts the dialog. Otherwise, it shows an error.
        """
        filepath = qt.QFileDialog.getOpenFileName(self, f"Select '{self.expected_filename}'")

        # Do nothing if the user canceled the file dialog
        if not filepath:
            return

        selected_filename = Path(filepath).name

        if selected_filename == self.expected_filename:
            self._result_action = BROWSE_ACTION
            self._selected_filepath = filepath
            self.accept() # closes the dialog
        else:
            title = "Incorrect File Selected"
            message = (
                f"The selected file has the wrong name.\n\n"
                f"Expected: {self.expected_filename}\n"
                f"Selected: {selected_filename}"
            )
            qt.QMessageBox.warning(self, title, message)

    def get_result(self) -> Tuple[Optional[str], Optional[str]]:
        """
        Returns the user's choice after the dialog has been closed.

        Returns:
            A tuple containing:
            - The action chosen ('download' or 'browse').
            - The selected file path (only if 'browse' was chosen).
        """
        return self._result_action, self._selected_filepath