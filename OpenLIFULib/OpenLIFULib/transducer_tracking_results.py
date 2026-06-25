import slicer
from slicer import vtkMRMLTransformNode
from typing import Optional, Tuple, Union, List, TYPE_CHECKING
from enum import Enum, auto

from OpenLIFULib.transform_conversion import (
    transducer_transform_node_to_openlifu,
    transducer_transform_node_from_openlifu,
    create_openlifu2slicer_matrix
    )
import numpy as np
from OpenLIFULib.coordinate_system_utils import numpy_to_vtk_4x4
from OpenLIFULib.util import get_cloned_node

if TYPE_CHECKING:
    from openlifu.db.session import TransducerTrackingResult
    from openlifu import Transducer

class TransducerTrackingTransformType(Enum):
    TRANSDUCER_TO_VOLUME = auto()
    PHOTOSCAN_TO_VOLUME = auto()

# Attribute names stored on TT result transform nodes.
_ATTR_PHOTOSCAN_ID = "TT:photoscanID"
_ATTR_TARGET_ID = "TT:targetID"
_ATTR_SESSION_ID = "TT:sessionID"
_ATTR_APPROVAL = "TT:approvalStatus"
_ATTR_RESULT_ID = "TT:resultID"
_ATTR_RESULT_INDEX = "TT:resultIndex"

_NO_TARGET_TOKEN = "notarget"


def _format_target_token(target_id: Optional[str]) -> str:
    return target_id if target_id else _NO_TARGET_TOKEN


def generate_next_result_id(photoscan_id: str, target_id: Optional[str], session_id: Optional[str]) -> str:
    """Compute the next stable TT result id for the given (photoscan, target) within the given session.

    The id format is ``f"{photoscan_id}__{target_id or 'notarget'}__{nn:02d}"``. The counter is incremented
    only across results whose photoscan_id+target_id match, so unrelated results do not push the counter.
    """
    prefix = f"{photoscan_id}__{_format_target_token(target_id)}__"
    existing_ids = {
        node.GetAttribute(_ATTR_RESULT_ID)
        for node in get_transducer_tracking_result_nodes_in_scene(session_id=session_id)
        if node.GetAttribute(_ATTR_RESULT_ID) and node.GetAttribute(_ATTR_RESULT_ID).startswith(prefix)
    }
    if session_id is None:
        # get_transducer_tracking_result_nodes_in_scene with session_id=None does not actually filter for
        # nodes that have no session id (it accepts everything), so restrict to sessionless here:
        existing_ids = {
            i for i in existing_ids
            if any(
                n.GetAttribute(_ATTR_SESSION_ID) is None
                for n in get_transducer_tracking_result_nodes_in_scene()
                if n.GetAttribute(_ATTR_RESULT_ID) == i
            )
        }
    n = 0
    while f"{prefix}{n:02d}" in existing_ids:
        n += 1
    return f"{prefix}{n:02d}"


def _next_result_index(session_id: Optional[str]) -> int:
    """Return the next sequential resultIndex for a new TT result added to the given session."""
    nodes = list(get_transducer_tracking_result_nodes_in_scene(session_id=session_id))
    if session_id is None:
        nodes = [n for n in nodes if n.GetAttribute(_ATTR_SESSION_ID) is None]
    used = set()
    for n in nodes:
        raw = n.GetAttribute(_ATTR_RESULT_INDEX)
        if raw is not None:
            try:
                used.add(int(raw))
            except ValueError:
                pass
    return (max(used) + 1) if used else 0


def add_transducer_tracking_result(
        transform_node: vtkMRMLTransformNode,
        transform_type: TransducerTrackingTransformType,
        photoscan_id: str,
        session_id: Optional[str] = None,
        target_id: Optional[str] = None,
        result_id: Optional[str] = None,
        result_index: Optional[int] = None,
        approval_status: bool = False,
        replace: bool = False,
        clone_node: bool = False,
        ) -> vtkMRMLTransformNode:
    """Add a transducer localization result node by giving it the appropriate attributes.

    Args:
        transform_node: source transform node.
        transform_type: TRANSDUCER_TO_VOLUME or PHOTOSCAN_TO_VOLUME.
        photoscan_id: photoscan this result is computed for.
        session_id: owning session id, or None for a sessionless result.
        target_id: target point id the result was computed for, or None.
        result_id: stable result id. If None, a new id is generated. If given and a node with
            that id + transform_type already exists, `replace` controls whether it is overwritten.
        result_index: explicit list position. If None, assigned to next available (max+1).
        approval_status: initial approval state.
        replace: if True, an existing node with the same result_id + transform_type is removed first.
        clone_node: if True, `transform_node` is cloned (left untouched) and the clone is used.

    Returns: the transform node configured as a TT result.
    """

    if result_id is None:
        result_id = generate_next_result_id(photoscan_id, target_id, session_id)

    # Look for an existing node with the same result_id + transform_type to either replace or refuse.
    existing_same_id = [
        n for n in get_transducer_tracking_result_nodes_in_scene(
            session_id=session_id, transform_type=transform_type)
        if n.GetAttribute(_ATTR_RESULT_ID) == result_id
        and (session_id is not None or n.GetAttribute(_ATTR_SESSION_ID) is None)
    ]
    if existing_same_id:
        if replace:
            for n in existing_same_id:
                slicer.mrmlScene.RemoveNode(n)
        else:
            raise RuntimeError(
                f"A transducer localization result already exists with id {result_id!r} "
                f"and transform_type {transform_type.name}; pass replace=True to overwrite.")

    if result_index is None:
        # Try to inherit the existing pair's index (so PV and TV stay together).
        siblings = [
            n for n in get_transducer_tracking_result_nodes_in_scene(session_id=session_id)
            if n.GetAttribute(_ATTR_RESULT_ID) == result_id
            and (session_id is not None or n.GetAttribute(_ATTR_SESSION_ID) is None)
        ]
        if siblings:
            raw = siblings[0].GetAttribute(_ATTR_RESULT_INDEX)
            if raw is not None:
                try:
                    result_index = int(raw)
                except ValueError:
                    result_index = None
        if result_index is None:
            result_index = _next_result_index(session_id)

    if clone_node:
        transducer_tracking_result_node: vtkMRMLTransformNode = get_cloned_node(transform_node)
    else:
        transducer_tracking_result_node = transform_node

    if transform_type == TransducerTrackingTransformType.TRANSDUCER_TO_VOLUME:
        transducer_tracking_result_node.SetName(f"TT transducer-volume {result_id}")
        transducer_tracking_result_node.SetAttribute(f"isTT-{TransducerTrackingTransformType.TRANSDUCER_TO_VOLUME.name}", "1")
    elif transform_type == TransducerTrackingTransformType.PHOTOSCAN_TO_VOLUME:
        transducer_tracking_result_node.SetName(f"TT photoscan-volume {result_id}")
        transducer_tracking_result_node.SetAttribute(f"isTT-{TransducerTrackingTransformType.PHOTOSCAN_TO_VOLUME.name}", "1")
    else:
        raise RuntimeError("Invalid transducer localization transform type specified")

    transducer_tracking_result_node.SetAttribute(_ATTR_APPROVAL, "1" if approval_status else "0")
    transducer_tracking_result_node.SetAttribute(_ATTR_PHOTOSCAN_ID, photoscan_id)
    transducer_tracking_result_node.SetAttribute(_ATTR_RESULT_ID, result_id)
    transducer_tracking_result_node.SetAttribute(_ATTR_RESULT_INDEX, str(result_index))
    if target_id is not None:
        transducer_tracking_result_node.SetAttribute(_ATTR_TARGET_ID, target_id)
    if session_id is not None:
        transducer_tracking_result_node.SetAttribute(_ATTR_SESSION_ID, session_id)

    transducer_tracking_result_node.CreateDefaultDisplayNodes()
    transducer_tracking_result_node.GetDisplayNode().SetVisibility(False)

    return transducer_tracking_result_node

def get_transducer_tracking_results_in_openlifu_session_format(session_id:str, transducer_units:str) -> List["TransducerTrackingResult"]:
    """Parse through transducer localization transform nodes in the scene and return the information in Session representation.

    Args:
        session_id: The ID of the session whose transducer localization result transform nodes we are interested in.
        transducer_units: The units of the transducer that the transducer localization transform nodes are meant to apply to.
            (If the transducer model is not in "mm" then there is a built in unit conversion in the transform
            node matrix and this has to be removed to represent the transform in openlifu format.)

    Returns the transducer localization results in openlifu Session format, ordered by TT:resultIndex.
    See also the reverse function `add_transducer_tracking_results_from_openlifu_session_format`.
    """
    import openlifu.db.session

    tt_results = get_complete_transducer_tracking_results(session_id=session_id, photoscan_id=None)

    transducer_tracking_results_openlifu = []
    for transducer_volume_node, photoscan_volume_node in tt_results:
        # Convert photoscan to volume transform to LPS
        transform_array = slicer.util.arrayFromTransformMatrix(photoscan_volume_node, toWorld=True)
        openlifu2slicer_matrix = create_openlifu2slicer_matrix('mm')
        photoscan_to_volume_transform_openlifu = openlifu.db.session.ArrayTransform(
            matrix=np.linalg.inv(openlifu2slicer_matrix) @ transform_array,
            units='mm',
        )

        transducer_to_volume_transform_openlifu = transducer_transform_node_to_openlifu(
            transform_node=transducer_volume_node,
            transducer_units=transducer_units)

        photoscan_id = transducer_volume_node.GetAttribute(_ATTR_PHOTOSCAN_ID)
        target_id = transducer_volume_node.GetAttribute(_ATTR_TARGET_ID)
        result_id = transducer_volume_node.GetAttribute(_ATTR_RESULT_ID)
        transducer_tracking_results_openlifu.append(
            openlifu.db.session.TransducerTrackingResult(
                photoscan_id=photoscan_id,
                transducer_to_volume_transform=transducer_to_volume_transform_openlifu,
                photoscan_to_volume_transform=photoscan_to_volume_transform_openlifu,
                transducer_to_volume_tracking_approved=transducer_volume_node.GetAttribute(_ATTR_APPROVAL) == "1",
                photoscan_to_volume_tracking_approved=photoscan_volume_node.GetAttribute(_ATTR_APPROVAL) == "1",
                id=result_id,
                target_id=target_id,
            )
        )

    return transducer_tracking_results_openlifu

def add_transducer_tracking_results_from_openlifu_session_format(
        tt_results_openlifu : List["TransducerTrackingResult"],
        session_id:str,
        transducer:"Transducer",
        replace = False,
        ) -> List[Tuple[vtkMRMLTransformNode, vtkMRMLTransformNode]]:
    """Read the openlifu session format and load the data into the slicer scene as
    two transducer localization result nodes representing the tranducer to photoscan and photoscan to volume
    transforms respectively.

    Args:
        tt_results_openlifu: Transducer localization results in the openlifu session format.
        session_id: The ID of the session with which to tag these virtual fit result nodes.
        transducer: The openlifu Transducer of the session. It is needed to configure transforms to be
            in the correct units.
        replace: Whether to replace any existing transducer localization results with the same result id
            (or any sessionless legacy results sharing the same photoscan id during the migration
            window). If False and a conflict is detected an error is raised.

    Returns a list of tuples, with the pairs of nodes added.

    See also the reverse function `get_transducer_tracking_results_in_openlifu_session_format`
    """
    nodes_that_have_been_added = []
    for list_index, tt_result in enumerate(tt_results_openlifu):

        transducer_to_volume_transform_node = transducer_transform_node_from_openlifu(
                openlifu_transform_matrix = tt_result.transducer_to_volume_transform.matrix,
                transform_units = tt_result.transducer_to_volume_transform.units,
                transducer = transducer,
            )

        # Convert photoscan_to_volume transform from LPS space to RAS space, both in mm.
        openlifu2slicer_matrix = create_openlifu2slicer_matrix('mm')
        transform_matrix_numpy = openlifu2slicer_matrix @  tt_result.photoscan_to_volume_transform.matrix
        transform_matrix_vtk = numpy_to_vtk_4x4(transform_matrix_numpy)
        photoscan_to_volume_transform_node = slicer.mrmlScene.AddNewNodeByClass("vtkMRMLTransformNode")
        photoscan_to_volume_transform_node.SetMatrixTransformToParent(transform_matrix_vtk)

        # Determine stable id: prefer the persisted id; otherwise generate one. Legacy openlifu
        # sessions saved before id+target_id were added will have id=None and target_id=None on
        # every result.
        result_id = tt_result.id
        target_id = tt_result.target_id
        if result_id is None:
            result_id = generate_next_result_id(tt_result.photoscan_id, target_id, session_id)

        transducer_to_volume_transform_node = add_transducer_tracking_result(
            transform_node=transducer_to_volume_transform_node,
            transform_type=TransducerTrackingTransformType.TRANSDUCER_TO_VOLUME,
            photoscan_id=tt_result.photoscan_id,
            approval_status=tt_result.transducer_to_volume_tracking_approved,
            session_id=session_id,
            target_id=target_id,
            result_id=result_id,
            result_index=list_index,
            replace=replace,
        )

        photoscan_to_volume_transform_node = add_transducer_tracking_result(
            transform_node=photoscan_to_volume_transform_node,
            transform_type=TransducerTrackingTransformType.PHOTOSCAN_TO_VOLUME,
            photoscan_id=tt_result.photoscan_id,
            approval_status=tt_result.photoscan_to_volume_tracking_approved,
            session_id=session_id,
            target_id=target_id,
            result_id=result_id,
            result_index=list_index,
            replace=replace,
        )

        nodes_that_have_been_added.append((transducer_to_volume_transform_node, photoscan_to_volume_transform_node))

    return nodes_that_have_been_added

def get_transducer_tracking_result_nodes_in_scene(
        photoscan_id : Optional[str] = None,
        session_id : Optional[str] = None,
        transform_type: Optional[TransducerTrackingTransformType] = None) -> vtkMRMLTransformNode:
    
    """Retrieve a list of all transducer localization result nodes, filtered as desired.

    Args:
        photoscan_id: filter for only this photoscan ID
        session_id: filter for only this session ID
        transform_type: filter for only this TransducerTrackingTransformType

    Returns the list of matching transducer localization transform nodes that are currently in the scene.
    """

    tt_result_nodes = [
        t for t in slicer.util.getNodesByClass('vtkMRMLTransformNode') if is_transducer_tracking_result_node(t)
        ]

    if session_id is not None:
        tt_result_nodes = filter(lambda t : t.GetAttribute("TT:sessionID") == session_id, tt_result_nodes)

    if photoscan_id is not None:
        tt_result_nodes = filter(lambda t : t.GetAttribute("TT:photoscanID") == photoscan_id, tt_result_nodes)

    if transform_type is not None:
        tt_result_nodes = filter(lambda t : t.GetAttribute(f"isTT-{transform_type.name}") == "1", tt_result_nodes)

    return tt_result_nodes

def get_transducer_tracking_result(
    photoscan_id : str,
    transform_type: TransducerTrackingTransformType,
    session_id : Optional[str]
    ) -> Optional[vtkMRMLTransformNode]:
    """Retrieve the transducer localization result for the given photoscan and transform type/direction, returning None if there isn't one,
    and raising an exception if there appears to be a non-unique one.

    Args:
        photoscan_id: photoscan ID for which to retrieve the transducer localization result
        transform_type: transform type for which to retrieve the the transducer localization result
        session_id: session ID to help identify the correct transducer localization result node, or None to work with
            only transducer localization result nodes that do not have an affiliated session

    Returns: The retrieved transducer localization result vtkMRMLTransformNode.
    """
    tt_result_nodes = list(get_transducer_tracking_result_nodes_in_scene(
        photoscan_id= photoscan_id,
        transform_type= transform_type,
        session_id=session_id
    ))

    # If session_id None, then at this point tt_result_nodes is not filtered for session ID
    # So here we specifically filter for nodes that are have *no* session id:
    if session_id is None:
        tt_result_nodes = list(filter(
            lambda t : t.GetAttribute("TT:sessionID") is None,
            tt_result_nodes,
        ))

    if len(tt_result_nodes) < 1:
        return None

    if len(tt_result_nodes) > 1:
        raise RuntimeError(
            f"There are {len(tt_result_nodes)} transducer localization result nodes of type {transform_type.name} for photoscan {photoscan_id} "
            + (f"and session {session_id}" if session_id is not None else "with no session.")
        )

    tt_result_node = tt_result_nodes[0]

    return tt_result_node

def get_complete_transducer_tracking_results(session_id: Optional[str], photoscan_id: Optional[str]) -> List[Tuple[vtkMRMLTransformNode, vtkMRMLTransformNode]]:
    """A transducer localization result is considered 'complete' when both the transducer_to_volume
    and photoscan_to_volume transforms nodes have been computed and added to the scene. This function
    pairs up complete results and returns them ordered by ``TT:resultIndex``.

    Pairing is by ``TT:resultID`` (preferred). Legacy nodes that pre-date resultID fall back to
    photoscan_id based pairing.

    Args:
        session_id: optional session ID. If None then **only transducer results with no session ID are included**.
        photoscan_id: optional photoscan ID. If None then transducer localization results for any affiliated photoscans are included.

    Returns the list of complete transducer localization results in the scene, ordered by resultIndex (
    legacy results without resultIndex are appended at the end in scene order). Each result is a
    tuple ``(transducer_to_volume_transform, photoscan_to_volume_transform)``.
    """

    tp_nodes = get_transducer_tracking_result_nodes_in_scene(session_id=session_id, photoscan_id=photoscan_id, transform_type=TransducerTrackingTransformType.TRANSDUCER_TO_VOLUME)
    pv_nodes = get_transducer_tracking_result_nodes_in_scene(session_id=session_id, photoscan_id=photoscan_id, transform_type=TransducerTrackingTransformType.PHOTOSCAN_TO_VOLUME)

    if session_id is None:
        tp_nodes = filter(lambda t: t.GetAttribute(_ATTR_SESSION_ID) is None, tp_nodes)
        pv_nodes = filter(lambda t: t.GetAttribute(_ATTR_SESSION_ID) is None, pv_nodes)

    tp_nodes = list(tp_nodes)
    pv_nodes = list(pv_nodes)

    # Pair by result_id first.
    pv_by_result_id = {}
    pv_legacy_by_photoscan = {}
    for pv in pv_nodes:
        rid = pv.GetAttribute(_ATTR_RESULT_ID)
        if rid:
            pv_by_result_id[rid] = pv
        else:
            pv_legacy_by_photoscan[pv.GetAttribute(_ATTR_PHOTOSCAN_ID)] = pv

    paired = []
    for tp in tp_nodes:
        rid = tp.GetAttribute(_ATTR_RESULT_ID)
        pv = None
        if rid and rid in pv_by_result_id:
            pv = pv_by_result_id[rid]
        else:
            # Legacy fallback: pair by photoscan_id when ids missing on either side.
            pv = pv_legacy_by_photoscan.get(tp.GetAttribute(_ATTR_PHOTOSCAN_ID))
        if pv is not None:
            paired.append((tp, pv))

    def _sort_key(pair):
        raw = pair[0].GetAttribute(_ATTR_RESULT_INDEX)
        if raw is None:
            return (1, 0)
        try:
            return (0, int(raw))
        except ValueError:
            return (1, 0)

    paired.sort(key=_sort_key)
    return paired


def get_all_transducer_tracking_results(session_id: Optional[str]) -> List[Tuple[vtkMRMLTransformNode, vtkMRMLTransformNode]]:
    """Convenience: every complete TT result for the session, ordered by resultIndex."""
    return get_complete_transducer_tracking_results(session_id=session_id, photoscan_id=None)


def get_transducer_tracking_result_by_id(
    result_id: str,
    transform_type: TransducerTrackingTransformType,
    session_id: Optional[str],
) -> Optional[vtkMRMLTransformNode]:
    """Look up a TT result node by stable result id + transform type within a session."""
    candidates = [
        n for n in get_transducer_tracking_result_nodes_in_scene(
            session_id=session_id, transform_type=transform_type)
        if n.GetAttribute(_ATTR_RESULT_ID) == result_id
        and (session_id is not None or n.GetAttribute(_ATTR_SESSION_ID) is None)
    ]
    if not candidates:
        return None
    if len(candidates) > 1:
        raise RuntimeError(
            f"Multiple TT result nodes match result_id={result_id!r} and transform_type={transform_type.name}")
    return candidates[0]


def set_transducer_tracking_approval_by_id(
    approval_state: bool,
    result_id: str,
    session_id: Optional[str],
) -> None:
    """Set the approval state on both transform nodes of the TT result identified by result_id."""
    pv = get_transducer_tracking_result_by_id(result_id, TransducerTrackingTransformType.PHOTOSCAN_TO_VOLUME, session_id)
    tv = get_transducer_tracking_result_by_id(result_id, TransducerTrackingTransformType.TRANSDUCER_TO_VOLUME, session_id)
    if pv is None or tv is None:
        raise RuntimeError(f"No complete TT result found for result_id={result_id!r}")
    set_transducer_tracking_approval_for_node(approval_state, pv)
    set_transducer_tracking_approval_for_node(approval_state, tv)


def remove_transducer_tracking_result_by_id(result_id: str, session_id: Optional[str]) -> None:
    """Remove both transform nodes for the TT result identified by result_id."""
    for transform_type in (TransducerTrackingTransformType.TRANSDUCER_TO_VOLUME, TransducerTrackingTransformType.PHOTOSCAN_TO_VOLUME):
        node = get_transducer_tracking_result_by_id(result_id, transform_type, session_id)
        if node is not None:
            slicer.mrmlScene.RemoveNode(node)


def reindex_transducer_tracking_results(session_id: Optional[str]) -> None:
    """Re-pack ``TT:resultIndex`` to 0..N-1 in current order, preserving each pair's joint position."""
    pairs = get_all_transducer_tracking_results(session_id)
    for new_index, (tv, pv) in enumerate(pairs):
        tv.SetAttribute(_ATTR_RESULT_INDEX, str(new_index))
        pv.SetAttribute(_ATTR_RESULT_INDEX, str(new_index))


def get_result_id_from_transducer_tracking_result_node(node: vtkMRMLTransformNode) -> Optional[str]:
    return node.GetAttribute(_ATTR_RESULT_ID)


def get_target_id_from_transducer_tracking_result_node(node: vtkMRMLTransformNode) -> Optional[str]:
    return node.GetAttribute(_ATTR_TARGET_ID)


def get_result_index_from_transducer_tracking_result_node(node: vtkMRMLTransformNode) -> Optional[int]:
    raw = node.GetAttribute(_ATTR_RESULT_INDEX)
    if raw is None:
        return None
    try:
        return int(raw)
    except ValueError:
        return None

def get_photoscan_ids_with_results(session_id: str, approved_only = False) -> List[str]:
    """Returns a list of all photoscan IDs for which there is a transducer localization result in the scene.

    Under multi-result, a photoscan may correspond to multiple TT results. The returned list contains
    each photoscan id at most once (any-approved semantics when ``approved_only=True``).

    Args:
        session_id: optional session ID. If None then **only transducer results with no session ID are included**.
        approved_only: optional flag. If True, then only photoscans with at least one approved (both PV and TV) result are returned.
    """
    tt_results = get_complete_transducer_tracking_results(session_id=session_id, photoscan_id=None)

    if approved_only:
        out = []
        for (t, p) in tt_results:
            if t.GetAttribute(_ATTR_APPROVAL) == "1" and p.GetAttribute(_ATTR_APPROVAL) == "1":
                pid = t.GetAttribute(_ATTR_PHOTOSCAN_ID)
                if pid not in out:
                    out.append(pid)
        return out
    else:
        out = []
        for (t, _) in tt_results:
            pid = t.GetAttribute(_ATTR_PHOTOSCAN_ID)
            if pid not in out:
                out.append(pid)
        return out

def set_transducer_tracking_approval_for_node(approval_state: bool, transform_node: vtkMRMLTransformNode) -> None:
    """Set approval state on the given transducer localization transform node.

    Args:
        approval_state: new approval state to apply
        transform_node: vtkMRMLTransformNode
    """
    if not is_transducer_tracking_result_node(transform_node):
        raise ValueError("The specified transform node is a not a transducer localization result node")
    transform_node.SetAttribute("TT:approvalStatus", "1" if approval_state else "0")

def set_transducer_tracking_approval_for_photoscan(approval_state: bool, photoscan_id: str, session_id: str):
    """Set approval state on both the transform nodes affiliated with the given photoscan.
    
    Args:
    approval_state: new approval state to apply
    photoscan_id: photoscan ID for which to apply new approval state to the transducer localization result nodes
    session_id: session ID to help identify the correct transducer localization result ndoes, or None to work with
    only transducer localization result nodes that do not have an affiliated session"""

    pv_node = get_transducer_tracking_result(photoscan_id, TransducerTrackingTransformType.PHOTOSCAN_TO_VOLUME, session_id) 
    set_transducer_tracking_approval_for_node(approval_state, pv_node)
   
    tv_node = get_transducer_tracking_result(photoscan_id, TransducerTrackingTransformType.TRANSDUCER_TO_VOLUME, session_id) 
    set_transducer_tracking_approval_for_node(approval_state, tv_node)

def get_approval_from_transducer_tracking_result_node(node : vtkMRMLTransformNode) -> bool:
    if node.GetAttribute("TT:approvalStatus") is None:
        raise RuntimeError("Node does not have a transducer localization approval status.")
    return node.GetAttribute("TT:approvalStatus") == "1"

def get_transform_type_from_transducer_tracking_result_node(node : vtkMRMLTransformNode) -> TransducerTrackingTransformType:
    if node.GetAttribute(f"isTT-{TransducerTrackingTransformType.TRANSDUCER_TO_VOLUME.name}") == "1":
        return TransducerTrackingTransformType.TRANSDUCER_TO_VOLUME
    elif node.GetAttribute(f"isTT-{TransducerTrackingTransformType.PHOTOSCAN_TO_VOLUME.name}") == "1":
        return TransducerTrackingTransformType.PHOTOSCAN_TO_VOLUME
    else:
        raise RuntimeError("The given node is not a transducer localization result transform.")

def get_photoscan_id_from_transducer_tracking_result(result: Union[vtkMRMLTransformNode, Tuple[vtkMRMLTransformNode, vtkMRMLTransformNode]]) -> str:
    """Returns the photoscan ID associated with a transducer localization transform node. 
    If a transducer localization result i.e. tuple of transform nodes is provided, this function
    includes a check to ensure that the paired transform nodes are associated with the same photoscan ID."""
    
    if isinstance(result, vtkMRMLTransformNode) and result.GetAttribute("TT:photoscanID") is not None:
        transform_node = result
    elif isinstance(result,tuple):
        if result[0].GetAttribute("TT:photoscanID") != result[1].GetAttribute("TT:photoscanID"):
            raise RuntimeError("Transducer localization transducer-volume and photoscan-volume transforms have mismatched photoscan IDs.")
        elif result[0].GetAttribute("TT:photoscanID") is None or result[1].GetAttribute("TT:photoscanID") is None:
            raise RuntimeError("Transducer localization result does not have a photoscan ID.")
        # Following the above checks, we can return the photoscanID attribute using either transform node
        transform_node = result[0]
    else:
        raise ValueError("Invalid transducer localization result type.")
    
    return transform_node.GetAttribute("TT:photoscanID")

def clear_transducer_tracking_results(
    session_id: Optional[str],
) -> None:
    """Remove all transducer localization results nodes from the scene that match the given session id.

    Args:
        session_id: session ID. If None then **only transducer localization results with no session ID are removed**!
    """

    nodes_to_remove = get_transducer_tracking_result_nodes_in_scene(session_id=session_id)

    # If session_id None, then at this point nodes_to_remove is not filtered for session ID
    # So here we specifically filter for nodes that are have *no* session id:
    if session_id is None:
        nodes_to_remove = filter(
            lambda t : t.GetAttribute("TT:sessionID") is None,
            nodes_to_remove,
        )

    for node in nodes_to_remove:
        slicer.mrmlScene.RemoveNode(node)
    
def is_transducer_tracking_result_node(transform_node) -> bool:
    """Returns True if the given node is a transducer localization result node"""
    if transform_node is None:
        return False
    return (
        transform_node.GetAttribute(f"isTT-{TransducerTrackingTransformType.TRANSDUCER_TO_VOLUME.name}") == "1"
        or transform_node.GetAttribute(f"isTT-{TransducerTrackingTransformType.PHOTOSCAN_TO_VOLUME.name}") == "1"
    )
