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
    import openlifu.xdc


# Define transducer color dictionary
TRANSDUCER_MODEL_COLORS = {
    "default": [230, 230, 77], # YELLOW
    "virtual_fit_result": [0, 85, 255], # BLUE
    "transducer_tracking_result": [0, 170, 0], # GREEN
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
    # Translucent "where would the transducer land" preview clone positioned at
    # a virtual-fit transform. Its polydata is the union (via ``vtkAppendPolyData``)
    # of ``body_model_node`` + ``surface_model_node`` (falling back to whichever
    # is present, and finally to ``model_node``) so a single MRML node carries
    # the whole visual signature -- no split fields for the parameterPack to
    # drop-then-restore behind our back on scene events. Manage via
    # :meth:`set_cloned_virtual_fit_model`, :meth:`set_cloned_virtual_fit_visibility`,
    # :meth:`set_cloned_virtual_fit_view_node_ids`, :meth:`remove_cloned_virtual_fit`.
    cloned_virtual_fit_model: Optional[vtkMRMLModelNode] = None

    @staticmethod
    def initialize_from_openlifu_transducer(
            transducer : "openlifu.xdc.Transducer",
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
                running the transducer localization algorithm. If left as empty, the registration surface and transducer body models affiliated 
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

        # Pause rendering while we load model files and wire them up to the transducer transform.
        # slicer.util.loadModel auto-creates display nodes that render immediately at the world
        # origin; without this, the body/surface models flash at the origin before the
        # SetAndObserveTransformNodeID call snaps them into place.
        slicer.app.pauseRender()
        try:
            #Model nodes
            model_node = slicer.mrmlScene.AddNewNodeByClass("vtkMRMLModelNode")
            model_node.SetName(f"{slicer_transducer_name}-transducer")
            model_node.SetAndObservePolyData(transducer.get_polydata())
            model_node.SetAndObserveTransformNodeID(transform_node.GetID())
            shNode.SetItemParent(shNode.GetItemByDataNode(model_node), parentFolderItem)
            model_node.CreateDefaultDisplayNodes() # toggles the "eyeball" on
            model_node.GetDisplayNode().SetVisibility2D(True)

            if transducer_abspaths_info['transducer_body_abspath'] is not None:
                if transducer.transducer_body_filename != Path(transducer_abspaths_info['transducer_body_abspath']).name:
                    raise ValueError("The filename provided in 'transducer_body_abspath' does not match the file specified in the Transducer object")
                body_model_node = slicer.util.loadModel(transducer_abspaths_info['transducer_body_abspath'])
                body_model_node.SetName(f"{slicer_transducer_name}-body")
                body_model_node.SetAndObserveTransformNodeID(transform_node.GetID())
                body_model_node.GetDisplayNode().SetVisibility2D(True)
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
                surface_model_node.GetDisplayNode().SetVisibility2D(True)
            else:
                surface_model_node = None
        finally:
            slicer.app.resumeRender()

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
        openlifu2slicer_matrix = create_openlifu2slicer_matrix(transform_matrix_units)
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

        if node:
            self.transform_node.SetAttribute("matching_transform", node.GetID())
        else:
            self.transform_node.RemoveAttribute("matching_transform")

        self.update_color()

    def update_color(self) -> None:
        """ Updates the color of the transducer model nodes based on the transform node
         specified using the "matching_transform" attribute."""

        matching_node_id = self.transform_node.GetAttribute("matching_transform")
        # Set the color of the transdcer model to indicate whether it matches a virtual fit result or tt result
        model_color = TRANSDUCER_MODEL_COLORS["default"]
        if matching_node_id:
            node = slicer.mrmlScene.GetNodeByID(matching_node_id)
            if node is None:
                # Stale reference -- the matched node was removed from the scene. Clear the attribute.
                self.transform_node.RemoveAttribute("matching_transform")
            elif is_virtual_fit_result_node(node):
                model_color = TRANSDUCER_MODEL_COLORS["virtual_fit_result"]
            elif is_transducer_tracking_result_node(node):
                model_color = TRANSDUCER_MODEL_COLORS["transducer_tracking_result"]

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
    
    def set_visibility(self, visibility: bool):
        """Sets the visibility of any model nodes associated with the transducer"""

        self.model_node.GetDisplayNode().SetVisibility(visibility)
        if self.body_model_node:
            self.body_model_node.GetDisplayNode().SetVisibility(visibility)
        if self.surface_model_node:
            self.surface_model_node.GetDisplayNode().SetVisibility(visibility)    
    
    def set_cloned_virtual_fit_model(self, virtual_fit_transform: vtkMRMLTransformNode):
        """Create (or reuse) a translucent VF-preview clone at ``virtual_fit_transform``.

        The clone is a single model node whose polydata unions body + surface via
        ``vtkAppendPolyData`` (falling back to whichever mesh exists, and finally
        to :attr:`model_node`). Storing one node keeps parameterPack persistence
        trivially correct across scene / data events.

        Idempotent: if the existing clone already observes ``virtual_fit_transform``
        it is returned as-is; otherwise the old clone is removed and a fresh one
        is built.
        """
        if (
            self.cloned_virtual_fit_model is not None
            and self.cloned_virtual_fit_model.GetTransformNodeID() == virtual_fit_transform.GetID()
        ):
            return self.cloned_virtual_fit_model
        self.remove_cloned_virtual_fit()

        # Build the union polydata. Prefer body+surface; fall back to whichever
        # is present; last resort is ``model_node``. We DEEP COPY the source
        # polydata so the clone's polydata is independent of any later edits
        # to the live meshes.
        append = vtk.vtkAppendPolyData()
        for source in (self.body_model_node, self.surface_model_node):
            if source is not None and source.GetPolyData() is not None:
                copy = vtk.vtkPolyData()
                copy.DeepCopy(source.GetPolyData())
                append.AddInputData(copy)
        if append.GetNumberOfInputConnections(0) == 0:
            # Neither body nor surface -- use ``model_node`` as a last resort.
            src_polydata = self.model_node.GetPolyData()
            if src_polydata is None:
                return None
            merged_polydata = vtk.vtkPolyData()
            merged_polydata.DeepCopy(src_polydata)
        else:
            append.Update()
            merged_polydata = append.GetOutput()

        clone = slicer.mrmlScene.AddNewNodeByClass("vtkMRMLModelNode")
        clone.SetName(f"{self.name}-VF-{virtual_fit_transform.GetName()}")
        clone.SetAndObservePolyData(merged_polydata)
        clone.CreateDefaultDisplayNodes()
        clone.SetAndObserveTransformNodeID(virtual_fit_transform.GetID())
        # Mark as a clone so ``onNodeAdded`` observers in pages skip the full
        # combo-refresh cascade (see ``util.get_cloned_node`` for the shared
        # convention).
        clone.SetAttribute("cloned", "True")
        normalized_color = [c / 255.0 for c in TRANSDUCER_MODEL_COLORS["virtual_fit_result"]]
        display_node = clone.GetDisplayNode()
        display_node.SetColor(*normalized_color)
        display_node.SetOpacity(0.5)
        display_node.SetVisibility(False)
        display_node.SetVisibility2D(True)

        self.cloned_virtual_fit_model = clone
        return clone

    def has_cloned_virtual_fit(self) -> bool:
        """True when the virtual-fit preview clone exists."""
        return self.cloned_virtual_fit_model is not None

    def set_cloned_virtual_fit_visibility(self, visibility: bool) -> None:
        """Toggle the VF-preview clone's display visibility (no-op when absent)."""
        if self.cloned_virtual_fit_model is None:
            return
        self.cloned_virtual_fit_model.SetDisplayVisibility(visibility)

    def set_cloned_virtual_fit_view_node_ids(self, view_node_ids) -> None:
        """Restrict the VF-preview clone to the given views (empty = all views)."""
        if self.cloned_virtual_fit_model is None:
            return
        self.cloned_virtual_fit_model.GetDisplayNode().SetViewNodeIDs(view_node_ids)

    def remove_cloned_virtual_fit(self) -> None:
        """Remove the VF-preview clone from the scene (no-op when absent)."""
        if self.cloned_virtual_fit_model is None:
            return
        slicer.mrmlScene.RemoveNode(self.cloned_virtual_fit_model)
        self.cloned_virtual_fit_model = None
