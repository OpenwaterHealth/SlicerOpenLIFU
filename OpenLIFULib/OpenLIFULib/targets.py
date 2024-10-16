from typing import List, TYPE_CHECKING, Optional
import numpy as np
import slicer
from slicer import vtkMRMLMarkupsFiducialNode
from OpenLIFULib.lazyimport import openlifu_lz
from OpenLIFULib.coordinate_system_utils import get_xx2mm_scale_factor, get_xxx2ras_matrix

if TYPE_CHECKING:
    import openlifu
    from OpenLIFULib.transducer import SlicerOpenLIFUTransducer

def get_target_candidates() -> List[vtkMRMLMarkupsFiducialNode]:
    """Get all fiducial nodes that could be considered openlifu targets, i.e. sonication targets.

    Right now the criterion is just that it be a fiducial markup with a single point in its point list.
    However in the future we will probably also avoid certain attributes to exclude
    for example a registration marker or a sonication focus point.
    (Remember, sonication focus points are part of a focal pattern centered around a sonication target,
    which is a concept we are distinguishing from sonication *target*)
    """
    return [
        fiducial_node
        for fiducial_node in slicer.util.getNodesByClass('vtkMRMLMarkupsFiducialNode')
        if fiducial_node.GetNumberOfControlPoints() == 1
    ]

def openlifu_point_to_fiducial(point : "openlifu.Point") -> vtkMRMLMarkupsFiducialNode:
    """Create a fiducial node out of an openlifu Point, removing any existing nodes that would have the same name.
    The name of the node will be the openlifu point ID, so we do not allow this to be duplicated.
    """

    # Clear out any existing nodes with this name
    node_name  = point.id
    existing_nodes_dict = slicer.util.getNodes(node_name, useLists=True)
    if node_name in existing_nodes_dict:
        for  existing_node in existing_nodes_dict[node_name]:
            slicer.mrmlScene.RemoveNode(existing_node)

    fiducial_node : vtkMRMLMarkupsFiducialNode = slicer.mrmlScene.AddNewNodeByClass("vtkMRMLMarkupsFiducialNode")
    fiducial_node.SetName(node_name)

    # Get point position and convert it to Slicer coordinates
    position = np.array(point.position)
    position = get_xxx2ras_matrix(point.dims) @ position
    position = get_xx2mm_scale_factor(point.units) * position

    target_display_node = fiducial_node.GetDisplayNode()
    target_display_node.SetSelectedColor(point.color)
    fiducial_node.SetLocked(True)
    fiducial_node.SetMaximumNumberOfControlPoints(1)

    fiducial_node.AddControlPoint(
        position
    )
    fiducial_node.SetNthControlPointLabel(0,point.name)

    return fiducial_node

def fiducial_to_openlifu_point_id(fiducial_node:vtkMRMLMarkupsFiducialNode) -> str:
    """Get the openlifu point ID that we would use if we were to convert the given fiducial node to an openlifu Point"""
    return fiducial_node.GetName()

def fiducial_to_openlifu_point_in_transducer_coords(fiducial_node:vtkMRMLMarkupsFiducialNode, transducer:"SlicerOpenLIFUTransducer", name:Optional[str] = None) -> "openlifu.Point":
    """Given a fiducial node with at least one point, return an openlifu Point in the local coordinates of the given transducer.
    If name is provided then it will be used as the name of the openlifu Point. Otherwise we use the label on the control point.
    """
    if fiducial_node.GetNumberOfControlPoints() < 1:
        raise ValueError(f"Fiducial node {fiducial_node.GetID()} does not have any points.")
    position = (np.linalg.inv(slicer.util.arrayFromTransformMatrix(transducer.transform_node)) @ np.array([*fiducial_node.GetNthControlPointPosition(0),1]))[:3] # TODO handle 4th coord here actually, would need to unprojectivize
    return openlifu_lz().Point(
        position=position,
        name = name if name is not None else fiducial_node.GetNthControlPointLabel(0),
        id = f"{fiducial_to_openlifu_point_id(fiducial_node)}-in-transducer-coords",
        dims=('x','y','z'), # Here x,y,z means transducer coordinates.
        units = transducer.transducer.transducer.units,
    )

def fiducial_to_openlifu_point(fiducial_node:vtkMRMLMarkupsFiducialNode) -> "openlifu.Point":
    """Given a fiducial node with at least one point, return an openlifu Point in RAS coordinates.
    This tries to be roughly an inverse operation of `openlifu_point_to_fiducial`, but isn't an inverse when it comes to
    for example the coordinates, and units. The opnenlifu point ID is however preserved between this function and
    `openlifu_point_to_fiducial`, because it is used as the node name."""
    if fiducial_node.GetNumberOfControlPoints() < 1:
        raise ValueError(f"Fiducial node {fiducial_node.GetID()} does not have any points.")
    return openlifu_lz().Point(
        position = np.array(fiducial_node.GetNthControlPointPosition(0)),
        name = fiducial_node.GetNthControlPointLabel(0),
        id = fiducial_to_openlifu_point_id(fiducial_node),
        dims=('R','A','S'),
        units = "mm",
    )