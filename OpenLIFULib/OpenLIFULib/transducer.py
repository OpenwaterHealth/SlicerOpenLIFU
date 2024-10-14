from typing import Optional, TYPE_CHECKING
import numpy as np
import slicer
from slicer import (
    vtkMRMLModelNode,
    vtkMRMLTransformNode,
)
from slicer.parameterNodeWrapper import parameterPack
from OpenLIFULib.parameter_node_utils import SlicerOpenLIFUTransducerWrapper
from OpenLIFULib.coordinate_system_utils import (
    linear_to_affine,
    get_xxx2ras_matrix,
    get_xx2mm_scale_factor,
    numpy_to_vtk_4x4
)

if TYPE_CHECKING:
    import openlifu # This import is deferred at runtime, but it is done here for IDE and static analysis purposes

@parameterPack
class SlicerOpenLIFUTransducer:
    """An openlifu Trasducer that has been loaded into Slicer (has a model node and transform node)"""
    transducer : SlicerOpenLIFUTransducerWrapper
    model_node : vtkMRMLModelNode
    transform_node : vtkMRMLTransformNode

    @staticmethod
    def initialize_from_openlifu_transducer(
            transducer : "openlifu.Transducer",
            transducer_matrix: Optional[np.ndarray]=None,
            transducer_matrix_units: Optional[str]=None,
        ) -> "SlicerOpenLIFUTransducer":
        """Initialize object with needed scene nodes from just the openlifu object.

        Args:
            transducer: The openlifu Transducer object
            transducer_matrix: The transform matrix of the transducer. Assumed to be the identity if None.
            transducer_matrix_units: The units in which to interpret the transform matrix.
                The transform matrix operates on a version of the coordinate space of the transducer that has been scaled to
                these units. If left as None then the transducer's native units (Transducer.units) will be assumed.

        Returns: the newly constructed SlicerOpenLIFUTransducer object
        """

        model_node = slicer.mrmlScene.AddNewNodeByClass("vtkMRMLModelNode")
        model_node.SetName(slicer.mrmlScene.GenerateUniqueName(transducer.id))
        model_node.SetAndObservePolyData(transducer.get_polydata())
        transform_node = slicer.mrmlScene.AddNewNodeByClass("vtkMRMLTransformNode")
        transform_node.SetName(slicer.mrmlScene.GenerateUniqueName(f"{transducer.id}-matrix"))
        model_node.SetAndObserveTransformNodeID(transform_node.GetID())

        # TODO: Instead of harcoding 'LPS' here, use something like a "dims" attribute that should be associated with
        # the `transducer` object. There is no such attribute yet but it should exist eventually once this is done:
        # https://github.com/OpenwaterHealth/opw_neuromod_sw/issues/3
        openlifu2slicer_matrix = linear_to_affine(
            get_xxx2ras_matrix('LPS') * get_xx2mm_scale_factor(transducer.units)
        )
        if transducer_matrix is None:
            transducer_matrix = np.eye(4)
        if transducer_matrix_units is None:
            transducer_matrix_units = transducer.units
        transform_in_native_transducer_coordinates = transducer.convert_transform(transducer_matrix, transducer_matrix_units)
        transform_matrix_numpy = openlifu2slicer_matrix @ transform_in_native_transducer_coordinates

        transform_matrix_vtk = numpy_to_vtk_4x4(transform_matrix_numpy)
        transform_node.SetMatrixTransformToParent(transform_matrix_vtk)
        model_node.CreateDefaultDisplayNodes() # toggles the "eyeball" on

        return SlicerOpenLIFUTransducer(
            SlicerOpenLIFUTransducerWrapper(transducer), model_node, transform_node
        )

    def clear_nodes(self) -> None:
        """Clear associated mrml nodes from the scene. Do this when removing a transducer."""
        slicer.mrmlScene.RemoveNode(self.model_node)
        slicer.mrmlScene.RemoveNode(self.transform_node)