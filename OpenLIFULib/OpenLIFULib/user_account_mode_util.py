from OpenLIFULib.util import (
        get_openlifu_login_parameter_node,
        )

def get_user_account_mode_state() -> bool:
    """Get user account mode state from the OpenLIFU Login module's parameter node"""
    return get_openlifu_login_parameter_node().user_account_mode

def set_user_account_mode_state(new_user_account_mode_state: bool):
    """Set user account mode state in OpenLIFU Login module's parameter node"""
    get_openlifu_login_parameter_node().user_account_mode = new_user_account_mode_state
