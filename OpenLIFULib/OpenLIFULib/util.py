from typing import TYPE_CHECKING, Any, List, Optional, get_type_hints, Annotated
from typing_extensions import get_type_hints as get_type_hints_ext # for <3.10 compatibility
import logging
import qt
import slicer
from slicer import vtkMRMLNode
if TYPE_CHECKING:
    from openlifu.db import Database
    from OpenLIFUApp.pages.database_page import OpenLIFUDatabaseParameterNode
    from OpenLIFUApp.logic.app_state import OpenLIFUAppState
    from OpenLIFUApp.pages.login_page import OpenLIFULoginParameterNode
    from OpenLIFUApp.pages.login_page import OpenLIFULoginLogic
    from OpenLIFULib.solution import SlicerOpenLIFUSolution

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
    return slicer.util.getModuleLogic("OpenLIFU").database_logic.getParameterNode()

def get_cur_db() -> "Optional[Database]":
    """Get the current openlifu.db.Database loaded in the OpenLIFU Database module"""
    return slicer.util.getModuleLogic("OpenLIFU").database_logic.db

def get_app_state() -> "OpenLIFUAppState":
    """Get the OpenLIFU app-state parameter node (owned by the host module)."""
    return slicer.util.getModuleLogic('OpenLIFU').getParameterNode()

def get_active_solution() -> "Optional[SlicerOpenLIFUSolution]":
    """Return the currently active :class:`SlicerOpenLIFUSolution`, or ``None``.

    The app can hold any number of solutions in :attr:`OpenLIFUAppState.loaded_solutions`
    at once (SlicerOpenLIFU#611). :attr:`OpenLIFUAppState.active_solution_id` names the
    one that page-level UI (analysis panel, PNP MIP, transducer-pose selection, sonication
    control) should treat as *the* current solution. An empty ``active_solution_id`` --
    the default -- means there is no active solution and this returns ``None``. It is
    also safe (and returns ``None``) if the id points at something that is not in the
    dict, which can happen briefly during load/unload transitions.
    """
    state = get_app_state()
    sid = state.active_solution_id
    if not sid:
        return None
    return state.loaded_solutions.get(sid)

def set_active_solution(solution_id: str) -> None:
    """Set the active solution by id.

    Pass an empty string to indicate \u201cno active solution\u201d. Callers are responsible for
    ensuring the id already exists in :attr:`OpenLIFUAppState.loaded_solutions` (or is
    ``""``); this helper does not validate, so that transient states during a coordinated
    dict + id update do not need to be linearized.
    """
    get_app_state().active_solution_id = solution_id


def mark_session_dirty() -> None:
    """Flag the loaded session as having unsaved in-memory changes.

    Called by any in-memory mutation that will only be persisted to
    ``{session_dir}/{session_id}.json`` when :meth:`OpenLIFUDataLogic.save_session`
    runs (target edits, VF / TT / PR approvals, photoscan approvals, etc.). Solutions
    and photoscan objects have their own on-disk artifacts written eagerly by
    ``set_solution`` / ``write_photoscan``, so mutations to *those* don't need to be
    flagged here -- only mutations to state that lives in the session JSON itself.
    Silently no-ops if no session is loaded (there's nothing to be dirty).

    Cleared by :meth:`OpenLIFUDataLogic.save_session`; consulted by
    :meth:`clear_session` to prompt the user before discarding unsaved changes.
    """
    state = get_app_state()
    if state.loaded_session is None:
        return
    if not state.session_is_dirty:
        state.session_is_dirty = True


def session_is_dirty() -> bool:
    """Return whether the loaded session has unsaved in-memory changes.

    See :func:`mark_session_dirty` for what counts as dirty. Returns ``False`` when
    no session is loaded.
    """
    state = get_app_state()
    if state.loaded_session is None:
        return False
    return bool(state.session_is_dirty)


def active_solution_is_pre_solution() -> bool:
    """Return ``True`` iff the active solution was computed against a virtual-fit-derived transducer pose.

    Consults ``session.solutions`` (see SlicerOpenLIFU#611): the :class:`openlifu.db.session.SolutionInfo`
    entry whose ``solution_id`` matches the active solution is authoritative. A source of
    ``"virtual_fit"`` means pre-solution; ``"localization"`` means not. Solutions saved before
    per-solution provenance existed have no matching entry, in which case this falls back to the
    legacy ``"presolution_"`` id-prefix check that Phase 4a of #611 introduced provenance for.

    Returns ``False`` when there is no active solution.
    """
    active = get_active_solution()
    if active is None:
        return False
    active_id = active.solution.solution.id
    loaded_session = get_app_state().loaded_session
    if loaded_session is not None:
        for entry in loaded_session.session.session.solutions:
            if entry.solution_id == active_id:
                return entry.transducer_transform_source == "virtual_fit"
    # Legacy: session predates SlicerOpenLIFU#611 and has no matching SolutionInfo entry.
    # Fall back to the historical id-prefix convention (see #609) so approvals from
    # older databases still gate the send-to-device flow / drive pose-visualization
    # correctly. Phase 4c's cascade-delete purges these orphans on save, at which
    # point this fallback becomes dead code and can be removed.
    return active_id.startswith("presolution_")

def get_openlifu_login_parameter_node() -> "OpenLIFULoginParameterNode":
    """Get the parameter node of the OpenLIFU Login module"""
    return slicer.util.getModuleLogic("OpenLIFU").login_logic.getParameterNode()

def get_openlifu_login_logic() -> "OpenLIFULoginLogic":
    """Get the logic of the OpenLIFU Login module"""
    return slicer.util.getModuleLogic("OpenLIFU").login_logic


def register_module_callback(widget, register_func, remove_func, callback) -> None:
    """Register ``callback`` via ``register_func`` and remember how to unregister it.

    ``register_func(callback)`` is called immediately. The corresponding
    ``remove_func`` is recorded on ``widget._registered_module_callbacks`` so
    :func:`cleanup_module_callbacks` can detach everything when the widget
    is destroyed (e.g. on Reload Module). Without this, bound-method
    callbacks on destroyed widgets accumulate inside long-lived module
    logic singletons and fire against stale Qt references.
    """
    register_func(callback)
    if not hasattr(widget, "_registered_module_callbacks"):
        widget._registered_module_callbacks = []
    widget._registered_module_callbacks.append((remove_func, callback))


def cleanup_module_callbacks(widget) -> None:
    """Detach every callback registered via :func:`register_module_callback`.

    Safe to call repeatedly and on widgets that never registered anything.
    """
    registered = getattr(widget, "_registered_module_callbacks", None)
    if not registered:
        return
    for remove_func, callback in registered:
        try:
            remove_func(callback)
        except Exception:  # noqa: BLE001
            # A logic class may have been collected before the widget; the
            # callbacks are already gone in that case.
            pass
    widget._registered_module_callbacks = []


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
        # Qt UI calls are only safe on the main thread. If this log record was emitted
        # from a background thread (e.g. UART monitor/read threads), skip Qt interactions
        # entirely to avoid QObject::setParent and processEvents cross-thread crashes.
        if qt.QThread.currentThread() is not slicer.app.thread():
            return

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
