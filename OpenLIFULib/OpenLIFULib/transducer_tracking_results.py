import slicer
from slicer import vtkMRMLTransformNode
from typing import Iterable, Optional, Tuple, Union, List, TYPE_CHECKING
from OpenLIFULib.transform_conversion import transform_node_to_openlifu, transform_node_from_openlifu
from OpenLIFULib.lazyimport import openlifu_lz

if TYPE_CHECKING:
    from openlifu.db.session import TransducerTrackingResult
    from openlifu import Transducer

def add_transducer_tracking_result(
        transducer_to_photoscan_transform_node: vtkMRMLTransformNode,
        photoscan_to_volume_transform_node: vtkMRMLTransformNode,
        photoscan_id: str,
        session_id: Optional[str] = None,
        transducer_to_photoscan_approval_status: bool = False,
        photoscan_to_volume_approval_status: bool = False,
        replace = False,
        ) -> Tuple[vtkMRMLTransformNode, vtkMRMLTransformNode]:
    
    # Should only be one per photoscan/per session
    existing_tt_result_nodes = get_transducer_tracking_results(photoscan_id=photoscan_id, session_id=session_id) # Returns a list of tuples
    if session_id is None:
        existing_tt_result_nodes = filter(
            lambda t : t.GetAttribute("TT:sessionID") is None,
            existing_tt_result_nodes,
        ) # if a sessionless TT result is being added, conflict should only occur among other sessionless results, hence this filtering

    for existing_tt_result_node in existing_tt_result_nodes:
        if replace:
            slicer.mrmlScene.RemoveNode(existing_tt_result_node[0])  
            slicer.mrmlScene.RemoveNode(existing_tt_result_node[1]) 
        else:
            raise RuntimeError("There is already a transducer tracking result node for this photoscan+session and replace is False")
    
    transducer_to_photoscan_transform_node.SetName(f"TT transducer-photoscan {photoscan_id}")
    transducer_to_photoscan_transform_node.SetAttribute("isTT-TransducerPhotoscanResult","1")
    transducer_to_photoscan_transform_node.SetAttribute("TT:approvalStatus", "1" if transducer_to_photoscan_approval_status else "0")
    transducer_to_photoscan_transform_node.SetAttribute("TT:photoscanID", photoscan_id)
    if session_id is not None:
        transducer_to_photoscan_transform_node.SetAttribute("TT:sessionID", session_id)
    
    photoscan_to_volume_transform_node.SetName(f"TT photoscan-volume {photoscan_id}")
    photoscan_to_volume_transform_node.SetAttribute("isTT-PhotoscanVolumeResult","1")
    photoscan_to_volume_transform_node.SetAttribute("TT:approvalStatus", "1" if photoscan_to_volume_approval_status else "0")
    photoscan_to_volume_transform_node.SetAttribute("TT:photoscanID", photoscan_id)
    if session_id is not None:
        photoscan_to_volume_transform_node.SetAttribute("TT:sessionID", session_id)
    
    return [transducer_to_photoscan_transform_node, photoscan_to_volume_transform_node]

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
    
    tt_nodes_for_session = get_transducer_tracking_results(session_id=session_id)
    photoscan_ids = [transducer_photoscan.GetAttribute("TT:photoscanID") for (transducer_photoscan, photoscan_volume) in tt_nodes_for_session]
    transducer_tracking_results_openlifu = []
    for photoscan_id in photoscan_ids:
        tt_nodes_for_photoscan = get_transducer_tracking_results(
            session_id=session_id,
            photoscan_id=photoscan_id,
        )
        # Confirm that length == 1
        transducer_photoscan_node, photoscan_volume_node = tt_nodes_for_photoscan[0]
        approved : bool = transducer_photoscan_node.GetAttribute("TT:approvalStatus") == "1" and photoscan_volume_node.GetAttribute("TT:approvalStatus") == "1" 
        transducer_tracking_results_openlifu.append(
            TransducerTrackingResult(
                    photoscan_id,
                    transform_node_to_openlifu(transform_node=transducer_photoscan_node, transducer_units=units),
                    transform_node_to_openlifu(transform_node=transducer_photoscan_node, transducer_units=units),
                    approved
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
        
        nodes_added = add_transducer_tracking_result(
            transducer_to_photoscan_transform_node = transducer_to_photoscan_transform_node,
            photoscan_to_volume_transform_node = photoscan_to_volume_transform_node,
            photoscan_id = tt_result.photoscan_id,
            transducer_to_photoscan_approval_status = tt_result.transducer_tracking_approved,
            photoscan_to_volume_approval_status = tt_result.transducer_tracking_approved,
            session_id = session_id,
            replace=replace
        )
        nodes_that_have_been_added.append(nodes_added)

    return nodes_that_have_been_added

def get_transducer_tracking_results(
        photoscan_id : Optional[str] = None,
        session_id : Optional[str] = None) -> Iterable[Tuple[vtkMRMLTransformNode,vtkMRMLTransformNode]]:
    
    """Retrieve a list of all transducer tracking result nodes, filtered as desired.
    Each transducer tracking result is given as a Tuple (Transducer-Photoscan transform node, Photoscan-Volume transform node)

    Args:
        photoscan_id: filter for only this photoscan ID
        session_id: filter for only this session ID

    Returns the list of matching transducer tracking results that are currently in the scene.
    """

    tp_nodes = [t for t in slicer.util.getNodesByClass('vtkMRMLTransformNode') if t.GetAttribute("isTT-TransducerPhotoscanResult") == "1"]
    pv_nodes = [t for t in slicer.util.getNodesByClass('vtkMRMLTransformNode') if t.GetAttribute("isTT-PhotoscanVolumeResult") == "1"]
    tt_result_nodes : Iterable[Tuple[vtkMRMLTransformNode,vtkMRMLTransformNode]] = [
        (t, p) for t in tp_nodes for p in pv_nodes if t.GetAttribute("TT:photoscanID") == p.GetAttribute("TT:photoscanID")]

    if session_id is not None:
        tt_result_nodes = filter(lambda t : t[0].GetAttribute("TT:sessionID") == session_id, tt_result_nodes)

    if photoscan_id is not None:
        tt_result_nodes = filter(lambda t : t[0].GetAttribute("TT:photoscanID") == photoscan_id, tt_result_nodes)

    return tt_result_nodes

def get_approved_photoscan_ids(session_id: str) -> List[str]:
    """List all photoscan IDs for which there is a transducer tracking result node in the scene that has an approval on it.

    Args:
        session_id: optional session ID. If None then **only transducer results with no session ID are included**.
    """

    nodes = get_transducer_tracking_results(session_id=session_id)

    # If session_id None, then at this point `nodes`` is not filtered for session ID
    # So here we specifically filter for nodes that are have *no* session id:
    if session_id is None:
        nodes = filter(lambda t : t[0].GetAttribute("TT:sessionID") is None, nodes)

    # Both transform nodes need to be approved for the photoscan to be approved
    return [t.GetAttribute("TT:photoscanID") for (t,p) in nodes if (t.GetAttribute("TT:approvalStatus") == "1") and (p.GetAttribute("TT:approvalStatus") == "1")]

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
