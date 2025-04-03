"""Custom vtk events for use throughout the application."""
from enum import IntEnum, unique
import vtk

OPENLIFU_STARTING_VTK_EVENT_ID = vtk.vtkCommand.UserEvent + 5000

@unique
class SlicerOpenLIFUEvents(IntEnum):
    """Custom vtk events for SlicerOpenLIFU."""

    TARGET_NAME_MODIFIED_EVENT = OPENLIFU_STARTING_VTK_EVENT_ID + 1
    """Invoked by an openlifu target fiducial node when its name is modified via SlicerOpenLIFU."""
