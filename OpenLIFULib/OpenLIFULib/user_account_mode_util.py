from typing import Optional, TYPE_CHECKING

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
    return get_openlifu_login_logic().active_user.user

def get_user_account_mode_state() -> bool:
    """Get user account mode state from the OpenLIFU Login module's parameter node"""
    return get_openlifu_login_parameter_node().user_account_mode

def set_user_account_mode_state(new_user_account_mode_state: bool):
    """Set user account mode state in OpenLIFU Login module's parameter node"""
    get_openlifu_login_parameter_node().user_account_mode = new_user_account_mode_state
