import slicer
from slicer import vtkMRMLTransformNode
from typing import Iterable, Optional, Tuple, Union, List, TYPE_CHECKING
from enum import Enum, auto

from OpenLIFULib.transform_conversion import (
    transducer_transform_node_to_openlifu,
    transducer_transform_node_from_openlifu,
    create_openlifu2slicer_matrix
    )
from OpenLIFULib.lazyimport import openlifu_lz
import numpy as np
from OpenLIFULib.coordinate_system_utils import numpy_to_vtk_4x4
from OpenLIFULib.util import get_cloned_node

if TYPE_CHECKING:
    from openlifu.db.session import TransducerTrackingResult
    from openlifu import Transducer

class TransducerTrackingTransformType(Enum):
    TRANSDUCER_TO_VOLUME = auto()
    PHOTOSCAN_TO_VOLUME = auto()

def add_transducer_tracking_result(
        transform_node: vtkMRMLTransformNode,
        transform_type: TransducerTrackingTransformType,
        photoscan_id: str,
        session_id: Optional[str] = None,
        approval_status: bool = False,
        replace = False,
        clone_node = False,
        ) -> vtkMRMLTransformNode:
    """Add a  transducer tracking result node by giving it the appropriate attributes.
    This means the transform node will be named appropriately
    and will have a bunch of attributes set on it so that we can identify it
    later as a transducer tracking result node.

    Args:
        transform_node: The transform node associated with the transducer tracking result. This node is cloned to
        create the transducer tracking result node. 
        transform_type: The direction of the transform - TRANSDUCER_TO_VOLUME or PHOTOSCAN_TO_VOLUME
        photoscan_id: The ID of the photoscan for which the transducer tracking transform was computed.
        session_id: The ID of the openlifu.Session during which transducer tracking took place.
            If not provided then it is assumed the transducer tracking took place without
            a session -- in such a workflow it is probably up to the user what they
            want to do with the resulting transform node since the transducer tracking
            result has no openlifu session to be saved into.
        approval_status: The approval status of the transducer tracking transform node.
        replace: Whether to replace any existing transducer tracking results that have the
            same session ID, photoscan ID, and transform type. If this is off, then an error is raised
            in the event that there is already a matching transducer tracking result in the scene.
        clone_node: Whether to clone or to take the `transform_node`. If True, then the node is cloned
            to create the transducer tracking result node, and the passed in `transform_node` is left unharmed.
            If False then the node is taken and turned into a transducer tracking result node (renamed, given attributes, etc.).
            Set clone_node to False if you no longer need the original `transform_node`; set it to True if you want to
            preserve the integrity of the original `transform_node`

    Returns: The the transducer tracking result transform node with the required attributes
    """
    
    # Should only be one per photoscan/per session/per transform_type
    existing_tt_result_nodes = get_transducer_tracking_result_nodes_in_scene(
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
    
    if clone_node:
        transducer_tracking_result_node : vtkMRMLTransformNode = get_cloned_node(transform_node)
    else:
        transducer_tracking_result_node = transform_node


    if transform_type == TransducerTrackingTransformType.TRANSDUCER_TO_VOLUME:
        transducer_tracking_result_node.SetName(f"TT transducer-volume {photoscan_id}")
        transducer_tracking_result_node.SetAttribute(f"isTT-{TransducerTrackingTransformType.TRANSDUCER_TO_VOLUME.name}","1")
    elif transform_type == TransducerTrackingTransformType.PHOTOSCAN_TO_VOLUME:
        transducer_tracking_result_node.SetName(f"TT photoscan-volume {photoscan_id}")
        transducer_tracking_result_node.SetAttribute(f"isTT-{TransducerTrackingTransformType.PHOTOSCAN_TO_VOLUME.name}","1")
    else:
        raise RuntimeError("Invalid transducer tracking transform type specified")

    transducer_tracking_result_node.SetAttribute("TT:approvalStatus", "1" if approval_status else "0")
    transducer_tracking_result_node.SetAttribute("TT:photoscanID", photoscan_id)
    if session_id is not None:
        transducer_tracking_result_node.SetAttribute("TT:sessionID", session_id)

    transducer_tracking_result_node.CreateDefaultDisplayNodes()
    transducer_tracking_result_node.GetDisplayNode().SetVisibility(False)
    
    return transducer_tracking_result_node

def get_transducer_tracking_results_in_openlifu_session_format(session_id:str, transducer_units:str) -> List["TransducerTrackingResult"]:
    """Parse through transducer tracking transform nodes in the scene and return the information in Session representation.

    Args:
        session_id: The ID of the session whose transducer tracking result transform nodes we are interested in.
        transducer_units: The units of the transducer that the transducer tracking transform nodes are meant to apply to.
            (If the transducer model is not in "mm" then there is a built in unit conversion in the transform
            node matrix and this has to be removed to represent the transform in openlifu format.)

    Returns the transducer tracking results in openlifu Session format. To understand this format, see the documentation of
    openlifu.db.Session.transducer_tracking_results.

    See also the reverse function `add_transducer_tracking_results_from_openlifu_session_format`.
    """
    
    photoscan_ids_for_session = get_photoscan_ids_with_results(session_id)
    transducer_tracking_results_openlifu = []
    for photoscan_id in photoscan_ids_for_session:
        tt_result_for_session_photoscan = get_complete_transducer_tracking_results(session_id=session_id, photoscan_id=photoscan_id)
        if len(tt_result_for_session_photoscan) > 1:
            raise RuntimeError(f"There are {len(tt_result_for_session_photoscan)} transducer tracking results for photoscan {photoscan_id}" 
                               + (f"and session {session_id}" if session_id is not None else "with no session.")
            )

        transducer_volume_node, photoscan_volume_node = tt_result_for_session_photoscan[0]
        
        # Convert photoscan to volume transform to LPS
        transform_array = slicer.util.arrayFromTransformMatrix(photoscan_volume_node, toWorld=True)
        openlifu2slicer_matrix = create_openlifu2slicer_matrix('mm')
        photoscan_to_volume_transform_openlifu = openlifu_lz().db.session.ArrayTransform(
            matrix = np.linalg.inv(openlifu2slicer_matrix) @ transform_array,
            units = 'mm',
        )

        transducer_to_volume_transform_openlifu = transducer_transform_node_to_openlifu(
            transform_node=transducer_volume_node,
            transducer_units=transducer_units)

        photoscan_id = transducer_volume_node.GetAttribute("TT:photoscanID")
        transducer_tracking_results_openlifu.append(
            openlifu_lz().db.session.TransducerTrackingResult(
                    photoscan_id = photoscan_id,
                    transducer_to_volume_transform = transducer_to_volume_transform_openlifu,
                    photoscan_to_volume_transform = photoscan_to_volume_transform_openlifu,
                    transducer_to_volume_tracking_approved = transducer_volume_node.GetAttribute("TT:approvalStatus") == "1",
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
        
        transducer_to_volume_transform_node = add_transducer_tracking_result(
            transform_node=transducer_to_volume_transform_node,
            transform_type=TransducerTrackingTransformType.TRANSDUCER_TO_VOLUME,
            photoscan_id=tt_result.photoscan_id,
            approval_status=tt_result.transducer_to_volume_tracking_approved,
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

        nodes_that_have_been_added.append((transducer_to_volume_transform_node, photoscan_to_volume_transform_node))

    return nodes_that_have_been_added

def get_transducer_tracking_result_nodes_in_scene(
        photoscan_id : Optional[str] = None,
        session_id : Optional[str] = None,
        transform_type: Optional[TransducerTrackingTransformType] = None) -> vtkMRMLTransformNode:
    
    """Retrieve a list of all transducer tracking result nodes, filtered as desired.

    Args:
        photoscan_id: filter for only this photoscan ID
        session_id: filter for only this session ID
        transform_type: filter for only this TransducerTrackingTransformType

    Returns the list of matching transducer tracking transform nodes that are currently in the scene.
    """

    tt_result_nodes = [
        t for t in slicer.util.getNodesByClass('vtkMRMLTransformNode') if t.GetAttribute(f"isTT-{TransducerTrackingTransformType.TRANSDUCER_TO_VOLUME.name}") == "1" or t.GetAttribute(f"isTT-{TransducerTrackingTransformType.PHOTOSCAN_TO_VOLUME.name}") == "1"
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
    """Retrieve the transducer tracking result for the given photoscan and transform type/direction, returning None if there isn't one,
    and raising an exception if there appears to be a non-unique one.

    Args:
        photoscan_id: photoscan ID for which to retrieve the transducer tracking result
        transform_type: transform type for which to retrieve the the transducer tracking result
        session_id: session ID to help identify the correct transducer tracking result node, or None to work with
            only transducer tracking result nodes that do not have an affiliated session

    Returns: The retrieved transducer tracking result vtkMRMLTransformNode.
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
            f"There are {len(tt_result_nodes)} transducer tracking result nodes of type {transform_type.name} for photoscan {photoscan_id} "
            + (f"and session {session_id}" if session_id is not None else "with no session.")
        )

    tt_result_node = tt_result_nodes[0]

    return tt_result_node

def get_complete_transducer_tracking_results(session_id: Optional[str], photoscan_id: Optional[str]) -> Iterable[Tuple[vtkMRMLTransformNode, vtkMRMLTransformNode]]:
    """A transducer tracking result is considered 'complete' when both the transducer_to_volume 
    and photoscan_to_volume transforms nodes have been computed and added to the scene. Only complete
    transducer tracking results can be added to a session. Therefore, this function identifies
    paired transducer_to_volume and photoscan_to_volume transform nodes and returns each result pair as a
    Tuple. Paired transformed nodes are identified as having the same session ID (unless session-less) and photoscan ID.

    Args:
        session_id: optional session ID. If None then **only transducer results with no session ID are included**.
        photoscan_id: optional photoscan ID. If None then transducer tracking results for any affiliated photoscans are included.

    Returns a list of associated transducer tracking results in the scene. Each result is a
    tuple of transducer tracking nodes: (transducer_to_volume_transform, photoscan_to_volume_transform) 
    """

    tp_nodes = get_transducer_tracking_result_nodes_in_scene(session_id=session_id, photoscan_id=photoscan_id, transform_type=TransducerTrackingTransformType.TRANSDUCER_TO_VOLUME)
    pv_nodes = get_transducer_tracking_result_nodes_in_scene(session_id = session_id, photoscan_id= photoscan_id, transform_type=TransducerTrackingTransformType.PHOTOSCAN_TO_VOLUME)

    # If session_id None, then at this point `nodes`` is not filtered for session ID
    # So here we specifically filter for nodes that are have *no* session id:
    if session_id is None:
        tp_nodes = filter(lambda t : t.GetAttribute("TT:sessionID") is None, tp_nodes)
        pv_nodes = filter(lambda t : t.GetAttribute("TT:sessionID") is None, pv_nodes)

    # Both transform nodes need to be there for a complete tracking result
    tt_results : Iterable[Tuple[vtkMRMLTransformNode,vtkMRMLTransformNode]] = [
        (t, p) for t in tp_nodes for p in pv_nodes if t.GetAttribute("TT:photoscanID") == p.GetAttribute("TT:photoscanID")]

    return tt_results

def get_photoscan_ids_with_results(session_id: str, approved_only = False) -> List[str]:
    """Returns a list of all photoscan IDs for which there is a transducer tracking result in the scene.

    Args:
        session_id: optional session ID. If None then **only transducer results with no session ID are included**.
        approved_only: optional flag. If True, then only approved results are returned.
    """
    tt_results = get_complete_transducer_tracking_results(session_id = session_id, photoscan_id=None)

    if approved_only:
        # Both transform nodes need to be approved for the photoscan to be approved
        return [t.GetAttribute("TT:photoscanID") for (t,p) in tt_results if (t.GetAttribute("TT:approvalStatus") == "1") and (p.GetAttribute("TT:approvalStatus") == "1")]
    else:
        return [t.GetAttribute("TT:photoscanID") for (t,_) in tt_results]

def set_transducer_tracking_approval_for_node(approval_state: bool, transform_node: vtkMRMLTransformNode) -> None:
    """Set approval state on the given transducer tracking transform node.

    Args:
        approval_state: new approval state to apply
        transform_node: vtkMRMLTransformNode
    """
    if (transform_node.GetAttribute(f"isTT-{TransducerTrackingTransformType.TRANSDUCER_TO_VOLUME.name}") != "1" 
        and transform_node.GetAttribute(f"isTT-{TransducerTrackingTransformType.PHOTOSCAN_TO_VOLUME.name}") != "1"
    ):
        raise ValueError("The specified transform node is a not a transducer tracking result node")
    transform_node.SetAttribute("TT:approvalStatus", "1" if approval_state else "0")

def get_approval_from_transducer_tracking_result_node(node : vtkMRMLTransformNode) -> bool:
    if node.GetAttribute("TT:approvalStatus") is None:
        raise RuntimeError("Node does not have a transducer tracking approval status.")
    return node.GetAttribute("TT:approvalStatus") == "1"

def get_photoscan_id_from_transducer_tracking_result(result: Union[vtkMRMLTransformNode, Tuple[vtkMRMLTransformNode, vtkMRMLTransformNode]]) -> str:
    """Returns the photoscan ID associated with a transducer tracking transform node. 
    If a transducer tracking result i.e. tuple of transform nodes is provided, this function
    includes a check to ensure that the paired transform nodes are associated with the same photoscan ID."""
    
    if isinstance(result, vtkMRMLTransformNode):
        transform_node = result
    elif isinstance(result,tuple):
        if result[0].GetAttribute("TT:photoscanID") != result[1].GetAttribute("TT:photoscanID"):
            raise RuntimeError("Transducer tracking transducer-volume and photoscan-volume transforms have mismatched photoscan IDs.")
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
