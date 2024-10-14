from typing import List, NamedTuple
import numpy as np
import slicer
from slicer import vtkMRMLScalarVolumeNode
from slicer.parameterNodeWrapper import parameterPack
from OpenLIFULib.lazyimport import openlifu_lz, xarray_lz
from OpenLIFULib.parameter_node_utils import (
    SlicerOpenLIFUPoint,
    SlicerOpenLIFUXADataset,
    SlicerOpenLIFUProtocol,
)
from OpenLIFULib.transducer import SlicerOpenLIFUTransducer
from OpenLIFULib.util import get_openlifu_data_parameter_node, BusyCursor
from OpenLIFULib.targets import (
    get_target_candidates,
    fiducial_to_openlifu_point,
    fiducial_to_openlifu_point_in_transducer_coords,
    openlifu_point_to_fiducial,
)
from OpenLIFULib.algorithm_input_widget import OpenLIFUAlgorithmInputWidget
from OpenLIFULib.session import SlicerOpenLIFUSession, assign_openlifu_metadata_to_volume_node
from OpenLIFULib.simulation import (
    make_volume_from_xarray_in_transducer_coords,
    make_xarray_in_transducer_coords_from_volume,
)

__all__ = [
    "openlifu_lz",
    "xarray_lz",
    "SlicerOpenLIFUPlan",
    "SlicerOpenLIFUProtocol",
    "SlicerOpenLIFUTransducer",
    "SlicerOpenLIFUPoint",
    "SlicerOpenLIFUXADataset",
    "PlanFocus",
    "get_openlifu_data_parameter_node",
    "BusyCursor",
    "get_target_candidates",
    "OpenLIFUAlgorithmInputWidget",
    "SlicerOpenLIFUSession",
    "make_volume_from_xarray_in_transducer_coords",
    "make_xarray_in_transducer_coords_from_volume",
    "fiducial_to_openlifu_point",
    "fiducial_to_openlifu_point_in_transducer_coords",
    "openlifu_point_to_fiducial",
    "assign_openlifu_metadata_to_volume_node",
]

class PlanFocus(NamedTuple):
    """Information that is generated by the SlicerOpenLIFU planning module for a particular focus point"""

    point : SlicerOpenLIFUPoint
    """Focus location"""

    delays : np.ndarray
    """Delays to steer the beam"""

    apodization : np.ndarray
    """Apodization to steer the beam"""

    simulation_output : SlicerOpenLIFUXADataset
    """Output of the k-wave simulation for this configuration"""

@parameterPack
class SlicerOpenLIFUPlan:
    """Information that is generated by running the SlicerOpenLIFU planning module"""

    # We list the type here as "List[Tuple[...]]" to help the parameter node wrapper do the right thing,
    # but really the type is "List[PlanFocus]"
    # The clean solution would have been to make PlanFocus a parameterPack, but it seems
    # that a List of parameterPack is not supported by slicer right now.
    plan_info : List[PlanFocus]
    """List of points for the beam to focus on, each with inforation that was generated to steer the beam"""

    pnp : vtkMRMLScalarVolumeNode
    """Peak negative pressure volume, aggregated over the results from each focus point"""

    intensity : vtkMRMLScalarVolumeNode
    """Average intensity volume, aggregated over the results from each focus point"""

    def clear_nodes(self) -> None:
        """Clear associated mrml nodes from the scene. Do this when removing a transducer."""
        slicer.mrmlScene.RemoveNode(self.pnp)
        slicer.mrmlScene.RemoveNode(self.intensity)
