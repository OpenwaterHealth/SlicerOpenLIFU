from typing import List, TYPE_CHECKING, Optional, Tuple
import numpy as np
import slicer
from slicer import vtkMRMLMarkupsFiducialNode
from OpenLIFULib.coordinate_system_utils import get_xx2mm_scale_factor, get_xxx2ras_matrix

if TYPE_CHECKING:
    import openlifu
    import openlifu.geo
    from OpenLIFULib.transducer import SlicerOpenLIFUTransducer

# Qualitative color palette for automatic per-target color assignment. Colors are 3-tuples of
# floats in [0.0, 1.0] applied to the fiducial display node's SelectedColor (which is what the
# targets table and 3D-view glyph read). Values are matplotlib "tab10" hues, chosen for
# categorical distinguishability; the exact rounding is stable so equality comparisons in
# `assign_unique_color_to_fiducial` work reliably across round-trips through openlifu Points.
TARGET_COLOR_PALETTE: List[Tuple[float, float, float]] = [
    (0.121, 0.466, 0.705),  # blue
    (1.000, 0.498, 0.054),  # orange
    (0.172, 0.627, 0.172),  # green
    (0.839, 0.152, 0.156),  # red
    (0.580, 0.403, 0.741),  # purple
    (0.549, 0.337, 0.294),  # brown
    (0.890, 0.466, 0.760),  # pink
    (0.498, 0.498, 0.498),  # gray
    (0.737, 0.741, 0.133),  # olive
    (0.090, 0.745, 0.811),  # cyan
]

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

def openlifu_point_to_fiducial(point : "openlifu.geo.Point") -> vtkMRMLMarkupsFiducialNode:
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

def label_for_target_id(target_id: str) -> str:
    """Return the user-facing display label associated to a target's openlifu point ID.

    Target identity is split across two axes: an internal, immutable ``target_id`` (the fiducial
    node's name — used as the record key for virtual fit results, solutions, session persistence,
    etc.) and a user-facing display label (the control-point label, editable in the PrePlanning
    targets table). Once the user renames a target, the internal id is no longer visible anywhere in
    the UI; user-facing messages that still interpolate the raw id become unhelpful (see #594).

    Prefer this helper anywhere a target is mentioned in a message, notification, or table cell:
    it resolves ``target_id`` to the current display label. Falls back to ``target_id`` itself when
    the mapping cannot be resolved (target no longer in scene, or the display label is empty).
    """
    if not target_id:
        return target_id
    nodes = slicer.util.getNodes(target_id, useLists=True).get(target_id, [])
    for node in nodes:
        if node.IsA("vtkMRMLMarkupsFiducialNode") and node.GetNumberOfControlPoints() >= 1:
            label = node.GetNthControlPointLabel(0)
            if label:
                return label
            break
    return target_id

def fiducial_to_openlifu_point_in_transducer_coords(fiducial_node:vtkMRMLMarkupsFiducialNode, transducer:"SlicerOpenLIFUTransducer", name:Optional[str] = None) -> "openlifu.geo.Point":
    """Given a fiducial node with at least one point, return an openlifu Point in the local coordinates of the given transducer.
    If name is provided then it will be used as the name of the openlifu Point. Otherwise we use the label on the control point.
    """
    import openlifu.geo

    if fiducial_node.GetNumberOfControlPoints() < 1:
        raise ValueError(f"Fiducial node {fiducial_node.GetID()} does not have any points.")
    position = (np.linalg.inv(slicer.util.arrayFromTransformMatrix(transducer.transform_node)) @ np.array([*fiducial_node.GetNthControlPointPosition(0),1]))[:3] # TODO handle 4th coord here actually, would need to unprojectivize
    return openlifu.geo.Point(
        position=position,
        name = name if name is not None else fiducial_node.GetNthControlPointLabel(0),
        id = f"{fiducial_to_openlifu_point_id(fiducial_node)}-in-transducer-coords",
        dims=('x','y','z'), # Here x,y,z means transducer coordinates.
        units = transducer.transducer.transducer.units,
    )

def fiducial_to_openlifu_point(fiducial_node:vtkMRMLMarkupsFiducialNode) -> "openlifu.geo.Point":
    """Given a fiducial node with at least one point, return an openlifu Point in RAS coordinates.
    This tries to be roughly an inverse operation of `openlifu_point_to_fiducial`, but isn't an inverse when it comes to
    for example the coordinates, and units. The opnenlifu point ID is however preserved between this function and
    `openlifu_point_to_fiducial`, because it is used as the node name."""
    import openlifu.geo

    if fiducial_node.GetNumberOfControlPoints() < 1:
        raise ValueError(f"Fiducial node {fiducial_node.GetID()} does not have any points.")

    # Round-trip the display color into the openlifu Point so per-target colors persist to the
    # underlying openlifu.Session (and therefore to disk on save). SelectedColor is what
    # `openlifu_point_to_fiducial` writes in the other direction.
    display_node = fiducial_node.GetDisplayNode()
    color = tuple(display_node.GetSelectedColor()) if display_node is not None else (1.0, 0.0, 0.0)

    return openlifu.geo.Point(
        position = np.array(fiducial_node.GetNthControlPointPosition(0)),
        name = fiducial_node.GetNthControlPointLabel(0),
        id = fiducial_to_openlifu_point_id(fiducial_node),
        color = color,
        dims=('R','A','S'),
        units = "mm",
    )

def assign_unique_color_to_fiducial(
    node: vtkMRMLMarkupsFiducialNode,
    other_target_nodes: List[vtkMRMLMarkupsFiducialNode],
) -> None:
    """Set ``node``'s display SelectedColor to the first TARGET_COLOR_PALETTE entry that is not
    already in use by any target in ``other_target_nodes``.

    If every palette entry is already in use (more targets than palette length), falls back to
    cycling by index (``len(other_target_nodes) % len(TARGET_COLOR_PALETTE)``); collisions past that
    point are acceptable since the visual distinguishability guarantee only holds up to the palette
    size.

    ``other_target_nodes`` should be the set of already-registered targets, excluding ``node``.
    No-op if ``node`` has no display node yet (rare -- can happen during construction).
    """
    display_node = node.GetDisplayNode()
    if display_node is None:
        return

    def _round(color):
        return tuple(round(c, 3) for c in color)

    used = set()
    for other in other_target_nodes:
        if other is None or other is node:
            continue
        other_display = other.GetDisplayNode()
        if other_display is None:
            continue
        used.add(_round(other_display.GetSelectedColor()))

    chosen: Optional[Tuple[float, float, float]] = None
    for candidate in TARGET_COLOR_PALETTE:
        if _round(candidate) not in used:
            chosen = candidate
            break
    if chosen is None:
        chosen = TARGET_COLOR_PALETTE[len(other_target_nodes) % len(TARGET_COLOR_PALETTE)]

    display_node.SetSelectedColor(*chosen)
