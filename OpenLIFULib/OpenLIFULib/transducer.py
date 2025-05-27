from typing import Optional, TYPE_CHECKING, Callable, Any
import numpy as np
from pathlib import Path
import vtk
import slicer
from slicer import (
    vtkMRMLModelNode,
    vtkMRMLTransformNode,
    vtkMRMLNode,
)
from slicer.parameterNodeWrapper import parameterPack
from OpenLIFULib.parameter_node_utils import SlicerOpenLIFUTransducerWrapper
from OpenLIFULib.coordinate_system_utils import numpy_to_vtk_4x4
from OpenLIFULib.transform_conversion import create_openlifu2slicer_matrix, transducer_transform_node_from_openlifu
from OpenLIFULib.transducer_tracking_results import is_transducer_tracking_result_node
from OpenLIFULib.virtual_fit_results import is_virtual_fit_result_node

if TYPE_CHECKING:
    import openlifu # This import is deferred at runtime, but it is done here for IDE and static analysis purposes


# Define transducer color dictionary
TRANSDUCER_MODEL_COLORS = {
    "default": [230, 230, 77], # yellow
    "virtual_fit_result": [255, 170, 0], # orange
    "transducer_tracking_result": [255, 0, 0], # red
}

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


        if transducer_matrix is None:
            transducer_matrix = np.eye(4)
        if transducer_matrix_units is None:
            transducer_matrix_units = transducer.units

        transform_node = transducer_transform_node_from_openlifu(
            openlifu_transform_matrix = transducer_matrix,
            transform_units = transducer_matrix_units,
            transducer = transducer,
        )

        shNode.SetItemParent(shNode.GetItemByDataNode(transform_node), parentFolderItem)
        transform_node.SetName(f"{slicer_transducer_name}-matrix")

        #Model nodes
        model_node = slicer.mrmlScene.AddNewNodeByClass("vtkMRMLModelNode")
        model_node.SetName(f"{slicer_transducer_name}-transducer")
        model_node.SetAndObservePolyData(transducer.get_polydata())
        model_node.SetAndObserveTransformNodeID(transform_node.GetID())
        shNode.SetItemParent(shNode.GetItemByDataNode(model_node), parentFolderItem)
        model_node.CreateDefaultDisplayNodes() # toggles the "eyeball" on

        if transducer_abspaths_info['transducer_body_abspath'] is not None:
            if transducer.transducer_body_filename != Path(transducer_abspaths_info['transducer_body_abspath']).name:
                raise ValueError("The filename provided in 'transducer_body_abspath' does not match the file specified in the Transducer object")
            body_model_node = slicer.util.loadModel(transducer_abspaths_info['transducer_body_abspath'])
            body_model_node.SetName(f"{slicer_transducer_name}-body")
            body_model_node.SetAndObserveTransformNodeID(transform_node.GetID())
            shNode.SetItemParent(shNode.GetItemByDataNode(body_model_node), parentFolderItem)
        else:
            body_model_node = None

        if transducer_abspaths_info['registration_surface_abspath'] is not None:
            if transducer.registration_surface_filename != Path(transducer_abspaths_info['registration_surface_abspath']).name:
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

        # Convert transform matrix from whatever units it came with into transducer units
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

    def set_current_transform_to_match_transform_node(self, transform_node : vtkMRMLTransformNode) -> None:
        """Set the matrix on the current transform node of this transducer to match the matrix of a given transform node.
        (This is done by a copy not reference, so it's a one-time update -- the tranforms do not become linked in any way.)"""

        transform_matrix = vtk.vtkMatrix4x4()
        transform_node.GetMatrixTransformToParent(transform_matrix)
        self.transform_node.SetMatrixTransformToParent(transform_matrix)

        # Add an attribute which specifies the ID of the transform being matched
        self.set_matching_transform(transform_node)

    def set_matching_transform(self, node: vtkMRMLTransformNode = None) -> None:
        
        model_color = TRANSDUCER_MODEL_COLORS["default"]
        if node:
            self.transform_node.SetAttribute("matching_transform", node.GetID())

            # Set the color of the transdcer model to indicate whether it matches a virtual fit result or tt result
            if is_virtual_fit_result_node(node):
                model_color = TRANSDUCER_MODEL_COLORS["virtual_fit_result"]
            elif is_transducer_tracking_result_node(node):
                model_color = TRANSDUCER_MODEL_COLORS["transducer_tracking_result"]
        else:
            self.transform_node.SetAttribute("matching_transform", None)
        
        # Normalize color to 0-1 range
        normalized_color = [c / 255.0 for c in model_color]
        self.model_node.GetDisplayNode().SetColor(normalized_color)
        if self.body_model_node and self.body_model_node.GetDisplayNode():
            self.body_model_node.GetDisplayNode().SetColor(normalized_color)
        if self.surface_model_node and self.surface_model_node.GetDisplayNode():
            self.surface_model_node.GetDisplayNode().SetColor(normalized_color)

    def move_node_into_transducer_sh_folder(self, node : vtkMRMLNode) -> None:
        """In the subject hiearchy, move the given `node` into this transducer's transform node folder."""
        shNode = slicer.vtkMRMLSubjectHierarchyNode.GetSubjectHierarchyNode(slicer.mrmlScene)
        shNode.SetItemParent(
            shNode.GetItemByDataNode(node),
            shNode.GetItemParent(shNode.GetItemByDataNode(self.transform_node)),
        )

    def is_matching_transform(self, query_transform_node: vtkMRMLTransformNode) -> bool:
        """Returns true if the transform associated with the transducer matches the given transform node"""

        current_transform_matrix = vtk.vtkMatrix4x4()
        self.transform_node.GetMatrixTransformToParent(current_transform_matrix)

        query_transform_matrix = vtk.vtkMatrix4x4()
        query_transform_node.GetMatrixTransformToParent(query_transform_matrix)

        is_matching = (
            np.isclose(slicer.util.arrayFromVTKMatrix(current_transform_matrix), slicer.util.arrayFromVTKMatrix(query_transform_matrix))
        ).all()
        
        return is_matching
