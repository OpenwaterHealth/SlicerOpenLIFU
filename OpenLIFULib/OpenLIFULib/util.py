from typing import TYPE_CHECKING, Any, List, Optional, get_type_hints, Annotated
from typing_extensions import get_type_hints as get_type_hints_ext # for <3.10 compatibility
import logging
import qt
import slicer
if TYPE_CHECKING:
    from openlifu.db import Database
    from OpenLIFUDatabase.OpenLIFUDatabase import OpenLIFUDatabaseParameterNode
    from OpenLIFUData.OpenLIFUData import OpenLIFUDataParameterNode
    from OpenLIFULogin.OpenLIFULogin import OpenLIFULoginParameterNode
    from OpenLIFULogin.OpenLIFULogin import OpenLIFULoginLogic

# Use this to ensure compatibility in Python 3.9
get_hints = get_type_hints if hasattr(Annotated, '__metadata__') else get_type_hints_ext
class BusyCursor:
    """
    Context manager for showing a busy cursor.  Ensures that cursor reverts to normal in
    case of an exception.
    """

    def __enter__(self):
        qt.QApplication.setOverrideCursor(qt.Qt.BusyCursor)

    def __exit__(self, exception_type, exception_value, traceback):
        qt.QApplication.restoreOverrideCursor()
        return False

def get_openlifu_database_parameter_node() -> "OpenLIFUDatabaseParameterNode":
    """Get the parameter node of the OpenLIFU Database module"""
    return slicer.util.getModuleLogic('OpenLIFUDatabase').getParameterNode()

def get_cur_db() -> "Optional[Database]":
    """Get the current openlifu.db.Database loaded in the OpenLIFU Database module"""
    return slicer.util.getModuleLogic('OpenLIFUDatabase').db

def get_openlifu_data_parameter_node() -> "OpenLIFUDataParameterNode":
    """Get the parameter node of the OpenLIFU Data module"""
    return slicer.util.getModuleLogic('OpenLIFUData').getParameterNode()

def get_openlifu_login_parameter_node() -> "OpenLIFULoginParameterNode":
    """Get the parameter node of the OpenLIFU Login module"""
    return slicer.util.getModuleLogic('OpenLIFULogin').getParameterNode()

def get_openlifu_login_logic() -> "OpenLIFULoginLogic":
    """Get the logic of the OpenLIFU Login module"""
    return slicer.util.getModuleLogic('OpenLIFULogin')

def display_errors(f):
    """Decorator to make functions forward their python exceptions along as slicer error displays"""
    def f_with_forwarded_errors(*args, **kwargs):
        try:
            return f(*args, **kwargs)
        except Exception as e:
            slicer.util.errorDisplay(f'Exception raised in {f.__name__}: {e}')
            raise e
    return f_with_forwarded_errors

class SlicerLogHandler(logging.Handler):
    def __init__(self, name_to_print, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.name_to_print = name_to_print

    def emit(self, record):
        if record.levelno == logging.ERROR:
            method_to_use = self.handle_error
        elif record.levelno == logging.WARNING:
            method_to_use = self.handle_warning
        else: # info or any other unaccounted for log message level
            method_to_use = self.handle_info
        
        slicer.app.processEvents()
        method_to_use(self.format(record))
        slicer.app.processEvents()

    def handle_error(self, msg):
        slicer.util.errorDisplay(f"{self.name_to_print}: {msg}")

    def handle_warning(self, msg):
        slicer.util.warningDisplay(f"{self.name_to_print}: {msg}")

    def handle_info(self, msg):
        slicer.util.showStatusMessage(f"{self.name_to_print}: {msg}")

def add_slicer_log_handler(logger_name : str, name_to_print : str):
    """Adds a SlicerLogHandler to the logger of a given name,
    and only doing so if that logger doesn't already have a SlicerLogHandler.

    Args:
        logger_name: The name of the logger that should receive Slicer log handling
        name_to_print: The display name of the logger to put on Slicer messages and 
            dialogs to indicate which logger the messages are coming from.
    """
    logger : logging.Logger = logging.getLogger(logger_name)
    if not any(isinstance(h, SlicerLogHandler) for h in logger.handlers):
        handler = SlicerLogHandler(name_to_print)
        logger.addHandler(handler)

def add_slicer_log_handler_for_openlifu_object(openlifu_object: Any):
    """Adds an appropriately named SlicerLogHandler to the logger of an openlifu object,
    and only doing so if that logger doesn't already have a SlicerLogHandler.
    This is designed to work with those openlifu classes that have a `logger` attribute,
    a common pattern in the openlifu python codebase.
    """
    if not hasattr(openlifu_object, "logger"):
        raise ValueError("This object does not have a logger attribute.")
    if not hasattr(openlifu_object, "__class__"):
        raise ValueError("This object is not an instance of an openlifu class.")
    logger : logging.Logger = openlifu_object.logger
    if not any(isinstance(h, SlicerLogHandler) for h in logger.handlers):
        handler = SlicerLogHandler(openlifu_object.__class__.__name__)
        logger.addHandler(handler)

# TODO: Fix the matlab weirdness in openlifu so that we can get rid of ensure_list here.
# The reason for ensure_list is to deal with the fact that matlab fails to distinguish
# between a list with one element and the element itself, and so it doesn't write out
# singleton lists properly
def ensure_list(item: Any) -> List[Any]:
    """ Ensure the input is a list. This is a no-op for lists, and returns a singleton list when given non-list input. """
    if isinstance(item, list):
        return item
    else:
        return [item]

def create_noneditable_QStandardItem(text:str) -> qt.QStandardItem:
            item = qt.QStandardItem(text)
            item.setEditable(False)
            return item

def replace_widget(old_widget: qt.QWidget, new_widget: qt.QWidget, ui_object=None):
    """Replace a widget by another. Meant for use in a scripted module, to replace widgets inside a layout.

    Args:
        old_widget: The widget to replace. It is assumed to be inside a layout.
        new_widget: The new widget that should replace old_widget.
        ui_object: The ui object from which to erase the replaced widget, if there is one.
            This is referring to the `ui` attribute that is often defined in the setup of scripted
            modules and constructed via `slicer.util.childWidgetVariables`.

    """
    parent = old_widget.parentWidget()
    layout = parent.layout()
    index = layout.indexOf(old_widget)
    layout.removeWidget(old_widget)

    if ui_object is not None:
        ui_attrs_to_delete = [
            child.name
            for child in slicer.util.findChildren(old_widget)
            if hasattr(child,"name")
        ]

    # The order of deleteLater and delattr matters here. The attribute names to remove from the ui_object must be collected before the
    # deletion is requested, and the deletion must be requested before the attributes are dropped -- once the attributes are dropped
    # there is a possibility of the widgets getting auto-deleted just because there is no remaining reference to them.
    old_widget.deleteLater()
    old_widget.hide()  # TODO: Find reason for replaced widgets having dangling reference. See https://github.com/OpenwaterHealth/OpenLIFU-app/pull/18

    if ui_object is not None:
        for attr_name in ui_attrs_to_delete:
            delattr(ui_object, attr_name)
            
    new_widget.setParent(parent)
    new_widget.show()
    layout.insertWidget(index, new_widget)
