from typing import Optional, TYPE_CHECKING, Callable, Any
import numpy as np
import vtk
import slicer
from slicer import (
    vtkMRMLModelNode,
    vtkMRMLTransformNode,
)
from slicer.parameterNodeWrapper import parameterPack
from OpenLIFULib.parameter_node_utils import SlicerOpenLIFUTransducerWrapper
from OpenLIFULib.coordinate_system_utils import numpy_to_vtk_4x4
from OpenLIFULib.transform_conversion import create_openlifu2slicer_matrix

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
            transducer_abspaths_info: dict = {},
            transducer_matrix: Optional[np.ndarray]=None,
            transducer_matrix_units: Optional[str]=None,
            ) -> "SlicerOpenLIFUTransducer":
        """Initialize object with needed scene nodes from just the openlifu object.

        Args:
            transducer: The openlifu Transducer object
            transducer_matrix: The transform matrix of the transducer. Assumed to be the identity if None.
            transducer_abspaths_info: Dictionary containing absolute filepath info to any data affiliated with the transducer object.
                This includes 'transducer_body_abspath' and 'registration_surface_abspath'. The registration surface model is required for
                running the transducer tracking algorithm. If left as empty, the registration surface and transducer body models affiliated 
                with the transducer will not be loaded.
            transducer_matrix_units: The units in which to interpret the transform matrix.
                The transform matrix operates on a version of the coordinate space of the transducer that has been scaled to
                these units. If left as None then the transducer's native units (Transducer.units) will be assumed.
        Returns: the newly constructed SlicerOpenLIFUTransducer object
        """

        shNode = slicer.vtkMRMLSubjectHierarchyNode.GetSubjectHierarchyNode(slicer.mrmlScene)
        slicer_transducer_name = slicer.mrmlScene.GenerateUniqueName(transducer.id)
        parentFolderItem = shNode.CreateFolderItem(shNode.GetSceneItemID(), slicer_transducer_name)
        shNode.SetItemAttribute(parentFolderItem, 'transducer_id', transducer.id)

        transform_node = slicer.mrmlScene.AddNewNodeByClass("vtkMRMLTransformNode")
        transform_node.SetName(f"{slicer_transducer_name}-matrix")

        openlifu2slicer_matrix = create_openlifu2slicer_matrix(transducer.units)
        if transducer_matrix is None:
            transducer_matrix = np.eye(4)
        if transducer_matrix_units is None:
            transducer_matrix_units = transducer.units
        transform_in_native_transducer_coordinates = transducer.convert_transform(transducer_matrix, transducer_matrix_units)
        transform_matrix_numpy = openlifu2slicer_matrix @ transform_in_native_transducer_coordinates

        transform_matrix_vtk = numpy_to_vtk_4x4(transform_matrix_numpy)
        transform_node.SetMatrixTransformToParent(transform_matrix_vtk)
        shNode.SetItemParent(shNode.GetItemByDataNode(transform_node), parentFolderItem)

        #Model nodes
        model_node = slicer.mrmlScene.AddNewNodeByClass("vtkMRMLModelNode")
        model_node.SetName(f"{slicer_transducer_name}-transducer")
        model_node.SetAndObservePolyData(transducer.get_polydata())
        model_node.SetAndObserveTransformNodeID(transform_node.GetID())
        shNode.SetItemParent(shNode.GetItemByDataNode(model_node), parentFolderItem)
        model_node.CreateDefaultDisplayNodes() # toggles the "eyeball" on

        if 'transducer_body_abspath' in transducer_abspaths_info:
            if transducer.transducer_body_filename != transducer_abspaths_info['transducer_body_abspath'].name:
                raise ValueError("The filename provided in 'transducer_body_abspath' does not match the file specified in the Transducer object")
            body_model_node = slicer.util.loadModel(transducer_abspaths_info['transducer_body_abspath'])
            body_model_node.SetName(f"{slicer_transducer_name}-body")
            body_model_node.SetAndObserveTransformNodeID(transform_node.GetID())
            shNode.SetItemParent(shNode.GetItemByDataNode(body_model_node), parentFolderItem)
        else:
            body_model_node = None

        if 'registration_surface_abspath' in transducer_abspaths_info:
            if transducer.registration_surface_filename != transducer_abspaths_info['registration_surface_abspath'].name:
                raise ValueError("The filename provided in 'registration_surface_abspath' does not match the file specified in the Transducer object")
            surface_model_node = slicer.util.loadModel(transducer_abspaths_info['registration_surface_abspath'])
            shNode.SetItemParent(shNode.GetItemByDataNode(surface_model_node), parentFolderItem)
            surface_model_node.SetAndObserveTransformNodeID(transform_node.GetID())
            surface_model_node.SetName(f"{slicer_transducer_name}-surface")
        else:
            surface_model_node = None

        return SlicerOpenLIFUTransducer(slicer_transducer_name,
            SlicerOpenLIFUTransducerWrapper(transducer), model_node, transform_node, body_model_node, surface_model_node
        )

    def update_transform(self, transform_matrix:np.ndarray, transform_matrix_units:Optional[str]=None):
        """ Update the transducer transform by postcomposing an additional matrix on top of the current transform.

        The transform_matrix is assumed to be in "openlifu" style transducer coordinates, which is currently hardcoded to being LPS,
        so this function does the needed conversions.

        This function is useful for applying transform updates that come from algorithms in openlifu-python,
        where the transform would be in openlifu conventions.
        """

        # Convert transform matrix from whaetver units it came with into transducer units
        if transform_matrix_units is None:
            transform_matrix_units = self.transducer.transducer.units
        transform_in_native_transducer_coordinates = self.transducer.transducer.convert_transform(transform_matrix, transform_matrix_units)

        # Get the current transform matrix, as a mapping from transducer-space-and-units to slicer RAS space and mm
        current_transform_vtk = vtk.vtkMatrix4x4()
        self.transform_node.GetMatrixTransformToParent(current_transform_vtk)
        current_transform = slicer.util.arrayFromVTKMatrix(current_transform_vtk)

        # Get the converstions back and forth between LPS-with-transducer-units and RAS-with-mm
        openlifu2slicer_matrix = create_openlifu2slicer_matrix(self.transducer.transducer)
        slicer2openlifu_matrix = np.linalg.inv(openlifu2slicer_matrix)

        # Compute the new transform by postcomposing the new transform with the current transform
        new_transform = openlifu2slicer_matrix @ transform_in_native_transducer_coordinates @ slicer2openlifu_matrix @ current_transform
        new_transform_vtk = numpy_to_vtk_4x4(new_transform)
        self.transform_node.SetMatrixTransformToParent(new_transform_vtk)



    def clear_nodes(self) -> None:
        """Clear associated mrml nodes from the scene. Do this when removing a transducer."""
        
        slicer.mrmlScene.RemoveNode(self.body_model_node)
        slicer.mrmlScene.RemoveNode(self.surface_model_node)
        slicer.mrmlScene.RemoveNode(self.model_node)
        slicer.mrmlScene.RemoveNode(self.transform_node)

        # Get the parent folder and remove the now empty folder if it still exists.
        shNode = slicer.vtkMRMLSubjectHierarchyNode.GetSubjectHierarchyNode(slicer.mrmlScene)
        folderID = shNode.GetItemByName(self.name)
        if folderID:
            shNode.RemoveItem(folderID)
        
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
