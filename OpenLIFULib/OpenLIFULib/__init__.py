import qt
import vtk
from typing import Any, List, Sequence, TYPE_CHECKING
import numpy as np
from numpy.typing import NDArray
from pathlib import Path
import slicer
import importlib
import sys
import logging


if TYPE_CHECKING:
    import openlifu # This import is deferred to later runtime, but it is done here for IDE and static analysis purposes

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

def install_python_requirements() -> None:
    """Install python requirements"""
    requirements_path = Path(__file__).parent / 'Resources/python-requirements.txt'
    with BusyCursor():
        slicer.util.pip_install(['-r', requirements_path])

def python_requirements_exist() -> bool:
    """Check and return whether python requirements are installed."""
    return importlib.util.find_spec('openlifu') is not None

def check_and_install_python_requirements(prompt_if_found = False) -> None:
    """Check whether python requirements are installed and prompt to install them if not.

    Args:
        prompt_if_found: If this is enabled then in the event that python requirements are found,
            there is a further prompt asking whether to run the install anyway.
    """
    want_install = False
    if not python_requirements_exist():
        want_install = slicer.util.confirmYesNoDisplay(
            text = "Some OpenLIFU python dependencies were not found. Install them now?",
            windowTitle = "Install python dependencies?",
        )
    elif prompt_if_found:
        want_install = slicer.util.confirmYesNoDisplay(
            text = "All OpenLIFU python dependencies were found. Re-run the install command?",
            windowTitle = "Reinstall python dependencies?",
        )
    if want_install:
        install_python_requirements()
        if python_requirements_exist():
            slicer.util.infoDisplay(text="Python requirements installed.", windowTitle="Success")
        else:
            slicer.util.errorDisplay(
                text="OpenLIFU python dependencies are still not found. The install may have failed.",
                windowTitle="Python dependencies still not found"
            )

def import_openlifu_with_check() -> "openlifu":
    """Import openlifu and return the module, checking that it is installed along the way."""
    if "openlifu" not in sys.modules:
        check_and_install_python_requirements(prompt_if_found=False)
        with BusyCursor():
            import openlifu
    return sys.modules["openlifu"]

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
        method_to_use(self.format(record))

    def handle_error(self, msg):
        slicer.util.errorDisplay(f"{self.name_to_print}: {msg}")

    def handle_warning(self, msg):
        slicer.util.warningDisplay(f"{self.name_to_print}: {msg}")

    def handle_info(self, msg):
        slicer.util.showStatusMessage(f"{self.name_to_print}: {msg}")

def add_slicer_log_handler(openlifu_object: Any):
    """Adds an appropriately named SlicerLogHandler to the logger of an openlifu object,
    and only doing so if that logger doesn't already have a SlicerLogHandler"""
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

def numpy_to_vtk_4x4(numpy_array_4x4 : NDArray[Any]) -> vtk.vtkMatrix4x4:
            if numpy_array_4x4.shape != (4, 4):
                raise ValueError("The input numpy array must be of shape (4, 4).")
            vtk_matrix = vtk.vtkMatrix4x4()
            for i in range(4):
                for j in range(4):
                    vtk_matrix.SetElement(i, j, numpy_array_4x4[i, j])
            return vtk_matrix

directions_in_RAS_coords_dict = {
    'R' : np.array([1,0,0]),
    'A' : np.array([0,1,0]),
    'S' : np.array([0,0,1]),
    'L' : np.array([-1,0,0]),
    'P' : np.array([0,-1,0]),
    'I' : np.array([0,0,-1]),
}

def get_xxx2ras_matrix(dims:Sequence[str]) -> NDArray[Any]:
    return np.array([
        directions_in_RAS_coords_dict[dim] for dim in dims
    ]).transpose()

def get_xx2mm_scale_factor(length_unit:str) -> float:
    openlifu = import_openlifu_with_check()
    return openlifu.util.units.getsiscale(length_unit, 'distance') / openlifu.util.units.getsiscale('mm', 'distance')

def linear_to_affine(matrix, translation=None):
    """Convert linear 3x3 transform to an affine 4x4 with
    the given translation vector (the default being no translation)"""
    if translation is None:
        translation = np.zeros(3)
    if matrix.shape != (3, 3):
        raise ValueError("The input numpy array must be of shape (3, 3).")
    return np.concatenate(
        [
            np.concatenate([matrix,translation.reshape(-1,1)], axis=1),
            np.array([[0,0,0,1]], dtype=float),
        ],
        axis=0,
    )

