from typing import Optional, TYPE_CHECKING

import qt
import slicer

from OpenLIFULib.util import (
        get_openlifu_login_parameter_node,
        get_openlifu_login_logic,
        )

if TYPE_CHECKING:
    import openlifu.db

def get_current_user() -> "Optional[openlifu.db.User]":
    """Get the active openlifu user. If no user is logged in or user account
    mode is off, a default user is returned, with the intention of being the most
    restricted"""
    return get_openlifu_login_logic().active_user

def get_user_account_mode_state() -> bool:
    """Get user account mode state from the OpenLIFU Login module's parameter node"""
    return get_openlifu_login_parameter_node().user_account_mode

def set_user_account_mode_state(new_user_account_mode_state: bool):
    """Set user account mode state in OpenLIFU Login module's parameter node"""
    get_openlifu_login_parameter_node().user_account_mode = new_user_account_mode_state

class UserAccountBanner(qt.QWidget):
    """ This is a lightweight widget that shows the current user account and
    allows jumping to the login module widget. """

    def __init__(
            self,
            parent:qt.QWidget,
        ):
        """User account shortcut QWidget

        Args:
            parent: Parent QWidget
        """
        super().__init__(parent)

        top_level_layout = qt.QVBoxLayout(self)

        # ---- top_level_layout contains a group box with the label and icon ----

        group_box = qt.QGroupBox()
        group_layout = qt.QHBoxLayout(group_box)

        self.active_user_label = qt.QLabel("Not signed in")
        self.active_user_label.setAlignment(qt.Qt.AlignLeft | qt.Qt.AlignVCenter)
        self.active_user_label.setFont(qt.QFont("", 14))
        group_layout.addWidget(self.active_user_label, 1)  # stretch=1 as positional argument

        self.go_to_login_button = qt.QPushButton("👤")
        self.go_to_login_button.setToolTip("Switch user")
        self.go_to_login_button.setFixedSize(28, 28)
        self.go_to_login_button.clicked.connect(lambda: slicer.util.selectModule("OpenLIFULogin"))
        group_layout.addWidget(self.go_to_login_button)

        top_level_layout.addWidget(group_box)  # Add the group box to the top_level_layout
    
    def change_active_user(self, new_active_user: Optional["openlifu.db.User"]):
        if new_active_user is None or new_active_user.id == "anonymous":
            self.active_user_label.setText("Not signed in")
        else:
            self.active_user_label.setText(f"Signed in as {new_active_user.id}")
