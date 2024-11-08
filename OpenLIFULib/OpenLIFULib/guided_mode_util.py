import slicer

def get_guided_mode_status() -> bool:
    """Get guided mode status from the OpenLIFU Home module's parameter node"""
    openlifu_home_parameter_node = slicer.util.getModuleLogic('OpenLIFUHome').getParameterNode()
    return openlifu_home_parameter_node.guided_mode

