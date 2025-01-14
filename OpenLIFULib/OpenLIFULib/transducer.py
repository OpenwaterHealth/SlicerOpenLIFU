from typing import Optional, TYPE_CHECKING, Callable, Any
import numpy as np
from pathlib import Path
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
from vtk import vtkCollection 

if TYPE_CHECKING:
    import openlifu # This import is deferred at runtime, but it is done here for IDE and static analysis purposes

@parameterPack
class SlicerOpenLIFUTransducer:
    """An openlifu Trasducer that has been loaded into Slicer (has a model node and transform node)"""
    name : str
    transducer : SlicerOpenLIFUTransducerWrapper
    model_node : vtkMRMLModelNode
    transform_node : vtkMRMLTransformNode
    body_model_node : Optional[vtkMRMLModelNode] = None
    surface_model_node : Optional[vtkMRMLModelNode] = None

    @staticmethod
    def initialize_from_openlifu_transducer(
            transducer : "openlifu.Transducer",
            parent_dir,
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
            parent_dir: Path to the parent directory containing the transducer object and associated files. 

        Returns: the newly constructed SlicerOpenLIFUTransducer object
        """

        shNode = slicer.vtkMRMLSubjectHierarchyNode.GetSubjectHierarchyNode(slicer.mrmlScene)
        slicer_transducer_name = slicer.mrmlScene.GenerateUniqueName(transducer.id)
        parentFolderItem = shNode.CreateFolderItem(shNode.GetSceneItemID(), slicer_transducer_name)

        model_node = slicer.mrmlScene.AddNewNodeByClass("vtkMRMLModelNode")
        model_node.SetName(slicer_transducer_name)
        model_node.SetAndObservePolyData(transducer.get_polydata())
        transform_node = slicer.mrmlScene.AddNewNodeByClass("vtkMRMLTransformNode")
        transform_node.SetName(f"{slicer_transducer_name}-matrix")
        model_node.SetAndObserveTransformNodeID(transform_node.GetID())
        shNode.SetItemParent(shNode.GetItemByDataNode(model_node), parentFolderItem)

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

        if transducer.transducer_body_filename:
            body_model_node = slicer.util.loadModel(Path(parent_dir)/transducer.transducer_body_filename)
            body_model_node.SetName(f"{slicer_transducer_name}-body")
            body_model_node.SetAndObserveTransformNodeID(transform_node.GetID())
            shNode.SetItemParent(shNode.GetItemByDataNode(body_model_node), parentFolderItem)
        else:
            body_model_node = None

        if transducer.registration_surface_filename:
            surface_model_node = slicer.util.loadModel(Path(parent_dir)/transducer.registration_surface_filename)
            shNode.SetItemParent(shNode.GetItemByDataNode(surface_model_node), parentFolderItem)
            surface_model_node.SetAndObserveTransformNodeID(transform_node.GetID())
            surface_model_node.SetName(f"{slicer_transducer_name}-surface")
        else:
            surface_model_node = None

        return SlicerOpenLIFUTransducer(slicer_transducer_name,
            SlicerOpenLIFUTransducerWrapper(transducer), model_node, transform_node, body_model_node, surface_model_node
        )

    def clear_nodes(self) -> None:
        """Clear associated mrml nodes from the scene. Do this when removing a transducer."""
        
        shNode = slicer.vtkMRMLSubjectHierarchyNode.GetSubjectHierarchyNode(slicer.mrmlScene)
        sceneItemID = shNode.GetSceneItemID()
        folderID = shNode.GetItemChildWithName(sceneItemID, self.name) # Find the ID of the parent folder
        shNode.RemoveItem(folderID, True, True) # TODO: Fix error when folder is deleted by user
        slicer.mrmlScene.RemoveNode(self.transform_node)

    def observe_transform_modified(self, callback : "Callable[[SlicerOpenLIFUTransducer],Any]") -> int:
        """Add an observer to the TransformModifiedEvent of the transducer's transform node, providing this object to the callback.

        The provided callback function should accept a single argument of type SlicerOpenLIFUTransducer.
        When the transducer transform is modified, the callback will be called with this SlicerOpenLIFUTransducer as input.

        Returns the observer tag, so that the observer could be removed using `stop_observing_transform_modified`.
        """
        return self.transform_node.AddObserver(
            slicer.vtkMRMLTransformNode.TransformModifiedEvent,
            lambda caller,event : callback(self)
        )

    def stop_observing_transform_modified(self, tag:int) -> None:
        self.transform_node.RemoveObserver(tag)
