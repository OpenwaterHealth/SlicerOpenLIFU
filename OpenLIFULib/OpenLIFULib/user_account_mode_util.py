import slicer

def get_user_account_mode_state() -> bool:
    """Get user account mode state from the OpenLIFU Login module's parameter node"""
    openlifu_login_parameter_node = slicer.util.getModuleLogic('OpenLIFULogin').getParameterNode()
    return openlifu_login_parameter_node.user_account_mode

def set_user_account_mode_state(new_user_account_mode_state: bool):
    """Set user account mode state in OpenLIFU Login module's parameter node"""
    openlifu_login_parameter_node = slicer.util.getModuleLogic('OpenLIFULogin').getParameterNode()
    openlifu_login_parameter_node.user_account_mode = new_user_account_mode_state
