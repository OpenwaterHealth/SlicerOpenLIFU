import slicer
from slicer import vtkMRMLTransformNode
from typing import Iterable, Optional, Tuple, Union, List, TYPE_CHECKING
from OpenLIFULib.transform_conversion import transform_node_to_openlifu, transform_node_from_openlifu
from OpenLIFULib.lazyimport import openlifu_lz
from enum import Enum, auto

if TYPE_CHECKING:
    from openlifu.db.session import TransducerTrackingResult
    from openlifu import Transducer

class TransducerTrackingTransformType(Enum):
    TRANSDUCER_TO_PHOTOCAN = auto()
    PHOTOSCAN_TO_VOLUME = auto()

def add_transducer_tracking_result(
        transform_node: vtkMRMLTransformNode,
        transform_type: TransducerTrackingTransformType,
        photoscan_id: str,
        session_id: Optional[str] = None,
        approval_status: bool = False,
        replace = False,
        ) -> vtkMRMLTransformNode:
    
    # Should only be one per photoscan/per session/per transform_type
    existing_tt_result_nodes = get_transducer_tracking_result_nodes(
        photoscan_id=photoscan_id,
        session_id=session_id,
        transform_type=transform_type) 
    
    if session_id is None:
        existing_tt_result_nodes = filter(
            lambda t : t.GetAttribute("TT:sessionID") is None,
            existing_tt_result_nodes,
        ) # if a sessionless TT result is being added, conflict should only occur among other sessionless results, hence this filtering

    for existing_tt_result_node in existing_tt_result_nodes:
        if replace:
            slicer.mrmlScene.RemoveNode(existing_tt_result_node)  
        else:
            raise RuntimeError("There is already a transducer tracking result node for this transform_type+photoscan+session and replace is False")
    
    if transform_type == TransducerTrackingTransformType.TRANSDUCER_TO_PHOTOCAN:
        transform_node.SetName(f"TT transducer-photoscan {photoscan_id}")
        transform_node.SetAttribute("isTT-TRANSDUCER_TO_PHOTOCAN","1")
    elif transform_type == TransducerTrackingTransformType.PHOTOSCAN_TO_VOLUME:
        transform_node.SetName(f"TT photoscan-volume {photoscan_id}")
        transform_node.SetAttribute("isTT-PHOTOSCAN_TO_VOLUME","1")

    transform_node.SetAttribute("TT:approvalStatus", "1" if approval_status else "0")
    transform_node.SetAttribute("TT:photoscanID", photoscan_id)
    if session_id is not None:
        transform_node.SetAttribute("TT:sessionID", session_id)
    
    return transform_node


def get_transducer_tracking_results_in_openlifu_session_format(session_id:str, units:str) -> List["TransducerTrackingResult"]:
    """Parse through transducer tracking transform nodes in the scene and return the information in Session representation.

    Args:
        session_id: The ID of the session whose transducer tracking result transform nodes we are interested in.
        units: The units of the transducer that the virtual fit transform nodes are meant to apply to.
            (If the transducer model is not in "mm" then there is a built in unit conversion in the transform
            node matrix and this has to be removed to represent the transform in openlifu format.)

    Returns the transducer tracking results in openlifu Session format. To understand this format, see the documentation of
    openlifu.db.Session.transducer_tracking_results.

    See also the reverse function `add_transducer_tracking_results_from_openlifu_session_format`.
    """
    tt_results_for_session = get_complete_transducer_tracking_results(session_id=session_id)
    transducer_tracking_results_openlifu = []
    for tt_result in tt_results_for_session:
        # Confirm that length == 1
        transducer_photoscan_node, photoscan_volume_node = tt_result
        photoscan_id = transducer_photoscan_node.GetAttribute("TT:photoscanID")
        transducer_tracking_results_openlifu.append(
            TransducerTrackingResult(
                    photoscan_id,
                    transform_node_to_openlifu(transform_node=transducer_photoscan_node, transducer_units=units),
                    transform_node_to_openlifu(transform_node=transducer_photoscan_node, transducer_units=units),
                    transducer_to_photoscan_tracking_approved = transducer_photoscan_node.GetAttribute("TT:approvalStatus") == "1",
                    photoscan_to_volume_tracking_approved = photoscan_volume_node.GetAttribute("TT:approvalStatus") == "1",
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
    two transducer tracking result nodes representing the tranducer to photoscan and photoscan to volume
     transforms respectively .

    Args:
        tt_results_openlifu: Transducer tracking results in the openlifu session format. 
        session_id: The ID of the session with which to tag these virtual fit result nodes.
        transducer: The openlifu Transducer of the session. It is needed to configure transforms to be
            in the correct units.
        replace: Whether to replace any existing transducer tracking results that have the
            same session ID and photoscan ID. If this is off, then an error is raised
            in the event that there is already a matching transducer tracking result in the scene.

    Returns a list of tuples, with the pairs of nodes added.

    See also the reverse function `get_transducer_tracking_results_in_openlifu_session_format`
    """
    nodes_that_have_been_added = []
    for tt_result in tt_results_openlifu:

        transducer_to_photoscan_transform_node = transform_node_from_openlifu(
                openlifu_transform_matrix = tt_result.transducer_to_photoscan_transform.matrix,
                transform_units = tt_result.transducer_to_photoscan_transform.units,
                transducer = transducer,
            )
        
        photoscan_to_volume_transform_node = transform_node_from_openlifu(
                openlifu_transform_matrix = tt_result.photoscan_to_volume_transform.matrix,
                transform_units = tt_result.photoscan_to_volume_transform.units,
                transducer = transducer,
            )
        
        transducer_to_photoscan_transform_node = add_transducer_tracking_result(
            transform_node=transducer_to_photoscan_transform_node,
            transform_type=TransducerTrackingTransformType.TRANSDUCER_TO_PHOTOCAN,
            photoscan_id=tt_result.photoscan_id,
            approval_status=tt_result.transducer_to_photoscan_tracking_approved,
            session_id=session_id,
            replace = replace
            )
        
        photoscan_to_volume_transform_node = add_transducer_tracking_result(
            transform_node = photoscan_to_volume_transform_node,
            transform_type =TransducerTrackingTransformType.PHOTOSCAN_TO_VOLUME,
            photoscan_id = tt_result.photoscan_id,
            approval_status = tt_result.photoscan_to_volume_tracking_approved,
            session_id=session_id,
            replace = replace
            )

        nodes_that_have_been_added.append((transducer_to_photoscan_transform_node, photoscan_to_volume_transform_node))

    return nodes_that_have_been_added

def get_transducer_tracking_result_nodes(
        photoscan_id : Optional[str] = None,
        session_id : Optional[str] = None,
        transform_type: Optional[TransducerTrackingTransformType] = None) -> Iterable[vtkMRMLTransformNode]:
    
    """Retrieve a list of all transducer tracking result nodes, filtered as desired.
    Each transducer tracking result is given as a Tuple (Transducer-Photoscan transform node, Photoscan-Volume transform node)

    Args:
        photoscan_id: filter for only this photoscan ID
        session_id: filter for only this session ID

    Returns the list of matching transducer tracking results that are currently in the scene.
    """

    tt_result_nodes = [
        t for t in slicer.util.getNodesByClass('vtkMRMLTransformNode') if t.GetAttribute(f"isTT-{TransducerTrackingTransformType.TRANSDUCER_TO_PHOTOCAN.name}") == "1" or t.GetAttribute(f"isTT-{TransducerTrackingTransformType.PHOTOSCAN_TO_VOLUME.name}") == "1"
        ]

    if session_id is not None:
        tt_result_nodes = filter(lambda t : t.GetAttribute("TT:sessionID") == session_id, tt_result_nodes)

    if photoscan_id is not None:
        tt_result_nodes = filter(lambda t : t.GetAttribute("TT:photoscanID") == photoscan_id, tt_result_nodes)

    if transform_type is not None:
        tt_result_nodes = filter(lambda t : t.GetAttribute(f"isTT-{transform_type.name}") == "1", tt_result_nodes)

    return tt_result_nodes

def get_complete_transducer_tracking_results(session_id:str) -> Iterable[Tuple[vtkMRMLTransformNode, vtkMRMLTransformNode]]:

    tp_nodes = get_transducer_tracking_result_nodes(session_id=session_id, transform_type=TransducerTrackingTransformType.TRANSDUCER_TO_PHOTOCAN)
    pv_nodes = get_transducer_tracking_result_nodes(session_id = session_id, transform_type=TransducerTrackingTransformType.PHOTOSCAN_TO_VOLUME)

    # If session_id None, then at this point `nodes`` is not filtered for session ID
    # So here we specifically filter for nodes that are have *no* session id:
    if session_id is None:
        tp_nodes = filter(lambda t : t.GetAttribute("TT:sessionID") is None, tp_nodes)
        pv_nodes = filter(lambda t : t.GetAttribute("TT:sessionID") is None, pv_nodes)

    # Both transform nodes need to be there for a complete tracking result
    tt_results : Iterable[Tuple[vtkMRMLTransformNode,vtkMRMLTransformNode]] = [
        (t, p) for t in tp_nodes for p in pv_nodes if t.GetAttribute("TT:photoscanID") == p.GetAttribute("TT:photoscanID")]

    return tt_results

def get_approved_photoscan_ids(session_id: str) -> List[str]:
    """List all photoscan IDs for which there is a transducer tracking result in the scene that has an approval on it.

    Args:
        session_id: optional session ID. If None then **only transducer results with no session ID are included**.
    """
    tt_results = get_complete_transducer_tracking_results(session_id = session_id)

    # Both transform nodes need to be approved for the photoscan to be approved
    return [t.GetAttribute("TT:photoscanID") for (t,p) in tt_results if (t.GetAttribute("TT:approvalStatus") == "1") and (p.GetAttribute("TT:approvalStatus") == "1")]

def set_transducer_tracking_approval_for_node(approval_state: bool, transform_node: vtkMRMLTransformNode) -> None:
    """Set approval state on the given transducer tracking transform node.

    Args:
        approval_state: new approval state to apply
        transform_node: vtkMRMLTransformNode
    """
    transform_node.SetAttribute("TT:approvalStatus", "1" if approval_state else "0")

def get_photoscan_id_from_transducer_tracking_result(result: Union[vtkMRMLTransformNode, Tuple[vtkMRMLTransformNode, vtkMRMLTransformNode]]) -> str:
    
    if isinstance(result, vtkMRMLTransformNode):
        transform_node = result
    elif isinstance(result,tuple):
        if result[0].GetAttribute("TT:photoscanID") != result[1].GetAttribute("TT:photoscanID"):
            raise RuntimeError("Transducer tracking transducer-photoscan and photoscan-volume transforms have mismatched photoscan IDs.")
        elif result[0].GetAttribute("TT:photoscanID") is None or result[1].GetAttribute("TT:photoscanID") is None:
            raise RuntimeError("Transducer tracking result does not have a photoscan ID.")
        # Following the above checks, we can return the photoscanID attribute using either transform node
        transform_node = result[0]
    else:
        raise ValueError("Invalid transducer tracking result type.")
    
    return transform_node.GetAttribute("TT:photoscanID")

def clear_transducer_tracking_results(
    session_id: Optional[str],
) -> None:
    """Remove all transducer tracking results nodes from the scene that match the given session id.

    Args:
        session_id: session ID. If None then **only transducer tracking results with no session ID are removed**!
    """

    nodes_to_remove = get_transducer_tracking_result_nodes(session_id=session_id)

    # If session_id None, then at this point nodes_to_remove is not filtered for session ID
    # So here we specifically filter for nodes that are have *no* session id:
    if session_id is None:
        nodes_to_remove = filter(
            lambda t : t.GetAttribute("TT:sessionID") is None,
            nodes_to_remove,
        )

    for node in nodes_to_remove:
        slicer.mrmlScene.RemoveNode(node)
