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

        layout = qt.QHBoxLayout(self)

        self.active_user_label = qt.QLabel()
        self.active_user_label.setAlignment(qt.Qt.AlignCenter)
        font = self.active_user_label.font()
        font.setPointSize(16)
        self.active_user_label.setFont(font)
        layout.addWidget(self.active_user_label)

        self.go_to_login_button = qt.QPushButton("👤")
        self.go_to_login_button.setFixedSize(30, 30)
        self.go_to_login_button.clicked.connect(lambda : slicer.util.selectModule("OpenLIFULogin"))
        layout.addWidget(self.go_to_login_button)

        # Add connection to change the label when a new account is logged in

        get_openlifu_login_logic().call_on_active_user_changed(self.on_active_user_changed)


    
    def on_active_user_changed(self, new_active_user: Optional["openlifu.db.User"]):
        if new_active_user is None:
            self.active_user_label.text = ""
        else:
            self.active_user_label.text = f"{new_active_user.name} ({new_active_user.id})"
