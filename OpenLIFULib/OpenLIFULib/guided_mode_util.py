import slicer

def get_guided_mode_state() -> bool:
    """Get guided mode state from the OpenLIFU Home module's parameter node"""
    openlifu_home_parameter_node = slicer.util.getModuleLogic('OpenLIFUHome').getParameterNode()
    return openlifu_home_parameter_node.guided_mode

def set_guided_mode_state(new_guided_mode_state: bool):
    """Set guided mode state in OpenLIFU Home module's parameter node"""
    openlifu_home_parameter_node = slicer.util.getModuleLogic('OpenLIFUHome').getParameterNode()
    openlifu_home_parameter_node.guided_mode = new_guided_mode_state
