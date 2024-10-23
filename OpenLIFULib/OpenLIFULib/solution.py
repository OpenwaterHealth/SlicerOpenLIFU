from typing import List, NamedTuple
import numpy as np
import slicer
from slicer import vtkMRMLScalarVolumeNode
from slicer.parameterNodeWrapper import parameterPack
from OpenLIFULib.parameter_node_utils import (
    SlicerOpenLIFUPoint,
    SlicerOpenLIFUXADataset,
)

class SolutionFocus(NamedTuple):
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
class SlicerOpenLIFUSolution:
    """Information that is generated by running the SlicerOpenLIFU planning module"""

    # We list the type here as "List[Tuple[...]]" to help the parameter node wrapper do the right thing,
    # but really the type is "List[SolutionFocus]"
    # The clean solution would have been to make SolutionFocus a parameterPack, but it seems
    # that a List of parameterPack is not supported by slicer right now.
    solution_info : List[SolutionFocus]
    """List of points for the beam to focus on, each with inforation that was generated to steer the beam"""

    pnp : vtkMRMLScalarVolumeNode
    """Peak negative pressure volume, aggregated over the results from each focus point"""

    intensity : vtkMRMLScalarVolumeNode
    """Average intensity volume, aggregated over the results from each focus point"""

    def clear_nodes(self) -> None:
        """Clear associated mrml nodes from the scene. Do this when removing a transducer."""
        slicer.mrmlScene.RemoveNode(self.pnp)
        slicer.mrmlScene.RemoveNode(self.intensity)