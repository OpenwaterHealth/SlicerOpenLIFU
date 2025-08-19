from typing import TYPE_CHECKING, Any, List, Optional, get_type_hints, Annotated
from typing_extensions import get_type_hints as get_type_hints_ext # for <3.10 compatibility
import logging
from pathlib import Path
import qt
import slicer
from slicer import vtkMRMLNode
if TYPE_CHECKING:
    from openlifu.db import Database
    from OpenLIFUDatabase.OpenLIFUDatabase import OpenLIFUDatabaseParameterNode
    from OpenLIFUData.OpenLIFUData import OpenLIFUDataParameterNode
    from OpenLIFULogin.OpenLIFULogin import OpenLIFULoginParameterNode
    from OpenLIFULogin.OpenLIFULogin import OpenLIFULoginLogic
    from OpenLIFUDatabase.OpenLIFUDatabase import OpenLIFUDatabaseLogic

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
    db_logic : OpenLIFUDatabaseLogic = slicer.util.getModuleLogic('OpenLIFUDatabase')
    if db_logic.db is None:
        db_dir : Path = db_logic.getParameterNode().databaseDirectory
        if db_dir.exists():
            db_logic.load_database(db_dir)
    return db_logic.db

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
    def __init__(self, name_to_print, use_dialogs=True, *args, **kwargs):
        """A python logging handler that sends logs to various Slicer places.

        Args:
            name_to_print: The display name by which to prepend log messages
            use_dialogs: Whether to involve slicer dialogs for warnings and errors.
                This is something I needed to turn off in the case where a logger was emitting messages
                from a different thread, which made Qt very angry when trying to parent the error or warning
                dialog to the slicer main window on a different thread.
            args, kwargs: These get forwarded onto the parent class logging.Handler
        """
        super().__init__(*args, **kwargs)
        self.name_to_print = name_to_print
        self.use_dialogs = use_dialogs

    def emit(self, record):
        if record.levelno == logging.ERROR:
            method_to_use = self.handle_error_with_dialog if self.use_dialogs else self.handle_error_without_dialog
        elif record.levelno == logging.WARNING:
            method_to_use = self.handle_warning_with_dialog if self.use_dialogs else self.handle_warning_without_dialog
        else: # info or any other unaccounted for log message level
            method_to_use = self.handle_info
        
        slicer.app.processEvents()
        method_to_use(self.format(record))
        slicer.app.processEvents()

    def handle_error_with_dialog(self, msg):
        slicer.util.errorDisplay(f"{self.name_to_print}: {msg}")

    def handle_warning_with_dialog(self, msg):
        slicer.util.warningDisplay(f"{self.name_to_print}: {msg}")

    def handle_error_without_dialog(self, msg):
        slicer.util.showStatusMessage(f"{self.name_to_print} ERROR: {msg}")

    def handle_warning_without_dialog(self, msg):
        slicer.util.showStatusMessage(f"{self.name_to_print} WARNING: {msg}")


    def handle_info(self, msg):
        slicer.util.showStatusMessage(f"{self.name_to_print}: {msg}")

def add_slicer_log_handler(logger_name : str, name_to_print : str, use_dialogs=True):
    """Adds a SlicerLogHandler to the logger of a given name,
    and only doing so if that logger doesn't already have a SlicerLogHandler.

    Args:
        logger_name: The name of the logger that should receive Slicer log handling
        name_to_print: The display name of the logger to put on Slicer messages and 
            dialogs to indicate which logger the messages are coming from.
        use_dialogs: Whether to involve slicer dialogs for warnings and errors.
                This is something I needed to turn off in the case where a logger was emitting messages
                from a different thread, which made Qt very angry when trying to parent the error or warning
                dialog to the slicer main window on a different thread.
    """
    logger : logging.Logger = logging.getLogger(logger_name)
    if not any(isinstance(h, SlicerLogHandler) for h in logger.handlers):
        handler = SlicerLogHandler(name_to_print=name_to_print, use_dialogs=use_dialogs)
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
    """Replace a widget by another, supporting both standard layouts and QFormLayout.

    Args:
        old_widget: The widget to replace. It is assumed to be inside a layout.
        new_widget: The new widget that should replace old_widget.
        ui_object: The ui object from which to erase the replaced widget, if there is one.
    """
    parent = old_widget.parentWidget()
    layout = parent.layout()
    ui_attrs_to_delete = []

    if ui_object is not None:
        ui_attrs_to_delete = [
            child.name
            for child in slicer.util.findChildren(old_widget)
            if hasattr(child,"name")
        ]

    if isinstance(layout, qt.QFormLayout):
        # Find the correct row and replace the field widget, preserving the label
        for row in range(layout.rowCount()):
            field_item = layout.itemAt(row, qt.QFormLayout.FieldRole)
            if field_item and field_item.widget() == old_widget:
                label_item = layout.itemAt(row, qt.QFormLayout.LabelRole)
                label_widget = label_item.widget() if label_item else None

                layout.removeWidget(old_widget)
                old_widget.deleteLater()
                old_widget.hide()

                new_widget.setParent(parent)
                new_widget.show()

                layout.setWidget(row, qt.QFormLayout.LabelRole, label_widget)  # re-set label if needed (safety)
                layout.setWidget(row, qt.QFormLayout.FieldRole, new_widget)
                break
    else:
        index = layout.indexOf(old_widget)
        layout.removeWidget(old_widget)

        old_widget.deleteLater()
        old_widget.hide()

        new_widget.setParent(parent)
        new_widget.show()
        layout.insertWidget(index, new_widget)

    if ui_object is not None:
        for attr_name in ui_attrs_to_delete:
            delattr(ui_object, attr_name)

def get_cloned_node(node_to_clone: vtkMRMLNode) -> vtkMRMLNode:

    shNode = slicer.vtkMRMLSubjectHierarchyNode.GetSubjectHierarchyNode(slicer.mrmlScene)
    itemIDToClone = shNode.GetItemByDataNode(node_to_clone)
    clonedItemID = slicer.modules.subjecthierarchy.logic().CloneSubjectHierarchyItem(shNode, itemIDToClone)
    cloned_node = shNode.GetItemDataNode(clonedItemID)
    cloned_node.SetAttribute("cloned", "True")

    return cloned_node
