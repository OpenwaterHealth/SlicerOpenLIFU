import slicer
from slicer import vtkMRMLTransformNode
from typing import Optional, Iterable, List, Tuple, Dict, TYPE_CHECKING, Union
from OpenLIFULib.transform_conversion import transform_node_to_openlifu, transform_node_from_openlifu
from OpenLIFULib.lazyimport import openlifu_lz

if TYPE_CHECKING:
    from openlifu.db.session import ArrayTransform
    from openlifu import Transducer

def add_virtual_fit_result(
    transform_node: vtkMRMLTransformNode,
    target_id: str,
    session_id: Optional[str] = None,
    approval_status: bool = False,
    rank: int = 1,
    replace = False,
    clone_node = False,
) -> vtkMRMLTransformNode:
    """Add a "virtual fit result" by cloning or creating a transform node and giving it appropriate attributes.

    This means the transform node will be named appropriately
    and will have a bunch of attributes set on it so that we can identify it
    later as a virtual fit result.

    Args:
        transform_node: The transform node to create this virtual fit result. Will be
            either cloned or stolen depending on the choice of `clone_node`.
        target_id: The ID of the target for which the virtual fit was computed.
            For example in a session-based workflow this should be the id of the
            target openlifu.Point.
        session_id: The ID of the openlifu.Session during which the virtual fit took place.
            If not provided then it is assumed the virtual fit took place without
            a session -- in such a workflow it is probably up to the user what they
            want to do with the virtual fit result transform node since the virtual fit
            result has no openlifu session to be saved into.
        approval_status: The approval status of the virtual fit result.
            Only the rank 1 virtual fit should take an approval status.
        rank: The rank of the virtual fit result in the ordering, from best to worst,
            of virtual fit results for this target. This can be needed because multiple candidate
            transforms can get generated by a virtual fitting algorithm.
        replace: Whether to replace any existing virtual fit results that have the
            same session ID, target ID, and rank. If this is off, then an error is raised
            in the event that there is already a matching virtual fit result in the scene.
        clone_node: Whether to clone or to take the `transform_node`. If True, then the node is cloned
            to create the virtual fit result node, and the passed in `transform_node` is left unharmed.
            If False then the node is taken and turned into a virtual fit result node (renamed, given attributes, etc.).

    Returns: The newly created virtual fit result transform node
    """

    existing_vf_result_nodes = get_virtual_fit_result_nodes(target_id=target_id, session_id=session_id)
    if session_id is None:
        existing_vf_result_nodes = filter(
            lambda t : t.GetAttribute("VF:sessionID") is None,
            existing_vf_result_nodes,
        ) # if a sessionless VF result is being added, conflict should only occur among other sessionless results, hence this filtering
    for existing_vf_result_node in existing_vf_result_nodes:
        existing_vf_result_node_rank = int(existing_vf_result_node.GetAttribute("VF:rank"))
        if existing_vf_result_node_rank == rank:
            if replace:
                slicer.mrmlScene.RemoveNode(existing_vf_result_node)
            else:
                raise RuntimeError("There is already a virtual fit result node for this target+session+rank and replace is False")

    if approval_status and rank != 1:
        raise ValueError("Only the rank 1 (best) virtual fit result can be approved")

    if clone_node:
        shNode = slicer.vtkMRMLSubjectHierarchyNode.GetSubjectHierarchyNode(slicer.mrmlScene)
        itemIDToClone = shNode.GetItemByDataNode(transform_node)
        clonedItemID = slicer.modules.subjecthierarchy.logic().CloneSubjectHierarchyItem(shNode, itemIDToClone)
        virtual_fit_result : vtkMRMLTransformNode = shNode.GetItemDataNode(clonedItemID)
    else:
        virtual_fit_result = transform_node

    virtual_fit_result.SetName(f"VF {target_id} {rank}")
    virtual_fit_result.SetAttribute("isVirtualFitResult", "1")
    virtual_fit_result.SetAttribute("VF:targetID", target_id)
    virtual_fit_result.SetAttribute("VF:rank", str(rank))
    if rank == 1:
        virtual_fit_result.SetAttribute("VF:approvalStatus", "1" if approval_status else "0")
    if session_id is not None:
        virtual_fit_result.SetAttribute("VF:sessionID", session_id)

    return virtual_fit_result

def get_virtual_fit_result_nodes(
    target_id : Optional[str] = None,
    session_id : Optional[str] = None,
    sort : bool = False,
    best_only : bool = False,
) -> Iterable[vtkMRMLTransformNode]:
    """Retrieve a list of all virtual fit result nodes, filtered and sorted as desired.

    Args:
        target_id: filter for only this target ID
        session_id: filter for only this session ID
        sort: sort by rank, from best rank to worst  (ascending rank value)
        best_only: filter for only the rank 1 virtual fit results

    Returns the list of matching virtual fit result transform nodes that are currently in the scene.
    """
    if sort and best_only:
        raise ValueError("It does not make sense to both sort by rank and retrieve only rank 1 results.")

    vf_result_nodes : Iterable[vtkMRMLTransformNode] = [
        t for t in slicer.util.getNodesByClass('vtkMRMLTransformNode') if t.GetAttribute("isVirtualFitResult") == "1"
    ]

    if session_id is not None:
        vf_result_nodes = filter(lambda t : t.GetAttribute("VF:sessionID") == session_id, vf_result_nodes)

    if target_id is not None:
        vf_result_nodes = filter(lambda t : t.GetAttribute("VF:targetID") == target_id, vf_result_nodes)

    if best_only:
        vf_result_nodes = filter(lambda t : int(t.GetAttribute("VF:rank")) == 1, vf_result_nodes)

    if sort:
        vf_result_nodes = sorted(vf_result_nodes, key = lambda t : int(t.GetAttribute("VF:rank")))

    return vf_result_nodes

def get_virtual_fit_results_in_openlifu_session_format(session_id:str, units:str) -> "Dict[str,Tuple[bool,List[ArrayTransform]]]":
    """Parse through virtual fit transform nodes in the scene and return the information in Session representation.

    Args:
        session_id: The ID of the session whose virtual fit result transform nodes we are interested in.
        units: The units of the transducer that the virtual fit transform nodes are meant to apply to.
            (If the transducer model is not in "mm" then there is a built in unit conversion in the transform
            node matrix and this has to be removed to represent the transform in openlifu format.)

    Returns the virtual fit results in openlifu Session format. To understand this format, see the documentation of
    openlifu.db.Session.virtual_fit_results.

    See also the reverse function `add_virtual_fit_results_from_openlifu_session_format`.
    """
    vf_nodes_for_session = get_virtual_fit_result_nodes(session_id=session_id)
    target_ids = [t.GetAttribute("VF:targetID") for t in vf_nodes_for_session]
    virtual_fit_results_openlifu = {}
    for target_id in target_ids:
        vf_nodes_for_target = get_virtual_fit_result_nodes(
            session_id=session_id,
            target_id=target_id,
            sort=True, # Sorted!
        )
        if int(vf_nodes_for_target[0].GetAttribute("VF:rank")) != 1:
            raise RuntimeError("The first virtual fit result node in the sorted list should have rank 1. Something went wrong.")
        approved : bool = vf_nodes_for_target[0].GetAttribute("VF:approvalStatus") == "1"
        virtual_fit_results_openlifu[target_id] = (
            approved,
            [
                transform_node_to_openlifu(transform_node=t, transducer_units=units)
                for t in vf_nodes_for_target
            ],
        )
    return virtual_fit_results_openlifu

def add_virtual_fit_results_from_openlifu_session_format(
    vf_results_openlifu : "Dict[str,Tuple[bool,List[ArrayTransform]]]",
    session_id:str,
    transducer:"Transducer",
    replace = False,
) -> List[vtkMRMLTransformNode]:
    """Read the openlifu session format and load the data into the slicer scene as virtual fit result nodes.

    Args:
        vf_results_openlifu: Virtual fit results in the openlifu session format. To understand this format,
            see the documentation of openlifu.db.Session.virtual_fit_results.
        session_id: The ID of the session with which to tag these virtual fit result nodes.
        transducer_units: The units of the transducer used in this session. It needs to be known so that we can build
            the conversion into Slicer's units (mm) directly into the transforms.
        replace: Whether to replace any existing virtual fit results that have the
            same session ID, target ID, and rank. If this is off, then an error is raised
            in the event that there is already a matching virtual fit result in the scene.

    Returns a list of the nodes that were added.

    See also the reverse function `get_virtual_fit_results_in_openlifu_session_format`
    """
    nodes_that_have_been_added = []
    for target_id, (is_approved, array_transforms) in vf_results_openlifu.items():
        for i, array_transform in enumerate(array_transforms):
            virtual_fit_result_transform = transform_node_from_openlifu(
                openlifu_transform_matrix = array_transform.matrix,
                transform_units = array_transform.units,
                transducer = transducer,
            )
            node = add_virtual_fit_result(
                transform_node = virtual_fit_result_transform,
                target_id = target_id,
                session_id = session_id,
                approval_status = is_approved if i==0 else False, # Only label approval on the top transform
                rank = i+1,
                clone_node=False,
                replace=replace,
            )
            nodes_that_have_been_added.append(node)
    return nodes_that_have_been_added

def get_best_virtual_fit_result_node(
    target_id : str,
    session_id : Optional[str],
) -> Optional[vtkMRMLTransformNode]:
    """Retrieve the best virtual fit result node for the given target, returning None if there isn't one,
    and raising an exception if there appears to be a non-unique one.

    Args:
        target_id: target ID for which to retrieve the unique best virtual fit result
        session_id: session ID to help identify the correct virtual fit result node, or None to work with
            only virtual fit result nodes that do not have an affiliated session

    Returns: The retrieved virtual fit result vtkMRMLTransformNode.
    """
    vf_result_nodes = list(get_virtual_fit_result_nodes(target_id=target_id, session_id=session_id, best_only=True))

    # If session_id None, then at this point vf_result_nodes is not filtered for session ID
    # So here we specifically filter for nodes that are have *no* session id:
    if session_id is None:
        vf_result_nodes = list(filter(
            lambda t : t.GetAttribute("VF:sessionID") is None,
            vf_result_nodes,
        ))

    if len(vf_result_nodes) < 1:
        return None

    if len(vf_result_nodes) > 1:
        raise RuntimeError(
            f"There are {len(vf_result_nodes)} rank 1 virtual fit result nodes for target {target_id} "
            + (f"and session {session_id}" if session_id is not None else "with no session.")
        )

    vf_result_node = vf_result_nodes[0]

    if int(vf_result_node.GetAttribute("VF:rank")) != 1:
        raise RuntimeError("The best virtual fit result node appears not have rank 1... this should not happen.")

    return vf_result_node

def clear_virtual_fit_results(
    target_id: Optional[str],
    session_id: Optional[str],
) -> None:
    """Remove all virtual fit results nodes from the scene that match the given target and session id.

    Args:
        target_id: target ID that needs to match for a virtual fit result to be removed. If None
            then *all* targets for the given session ID are cleared out.
        session_id: session ID. If None then **only virtual fit results with no session ID are removed**!
    """

    nodes_to_remove = get_virtual_fit_result_nodes(target_id=target_id, session_id=session_id)

    # If session_id None, then at this point nodes_to_remove is not filtered for session ID
    # So here we specifically filter for nodes that are have *no* session id:
    if session_id is None:
        nodes_to_remove = filter(
            lambda t : t.GetAttribute("VF:sessionID") is None,
            nodes_to_remove,
        )

    for node in nodes_to_remove:
        slicer.mrmlScene.RemoveNode(node)

def get_approved_target_ids(session_id: str) -> List[str]:
    """List all target IDs for which there is a virtual fit result node in the scene that has an approval on it.

    Args:
        session_id: optional session ID. If None then **only virtual fit results with no session ID are included**.
    """

    nodes = get_virtual_fit_result_nodes(session_id=session_id, best_only=True)

    # If session_id None, then at this point `nodes`` is not filtered for session ID
    # So here we specifically filter for nodes that are have *no* session id:
    if session_id is None:
        nodes = filter(lambda t : t.GetAttribute("VF:sessionID") is None, nodes)

    return [t.GetAttribute("VF:targetID") for t in nodes if int(t.GetAttribute("VF:approvalStatus")) == 1]

def set_virtual_fit_approval_for_target(
    approval_state : bool,
    target_id : str,
    session_id : Optional[str],
) -> None:
    """Set approval state on the best virtual fit result node for the given target.

    Args:
        approval_state: new approval state to apply
        target_id: target ID for which to apply new approval state to the best virtual fit result
        session_id: session ID to help identify the correct virtual fit result node, or None to work with
            only virtual fit result nodes that do not have an affiliated session
    """
    node = get_best_virtual_fit_result_node(target_id=target_id, session_id=session_id)
    if node is None:
        raise RuntimeError("There is no virtual fit node for the given target id and session id.")
    node.SetAttribute("VF:approvalStatus", "1" if approval_state else "0")

def get_virtual_fit_approval_for_target(
    target_id : str,
    session_id : Optional[str],
) -> bool:
    """Get approval state on the best virtual fit result node for the given target.

    Args:
        target_id: target ID for which to retrieve the virtual fit approval state
        session_id: session ID to help identify the correct virtual fit result node, or None to work with
            only virtual fit result nodes that do not have an affiliated session
    """
    node = get_best_virtual_fit_result_node(target_id=target_id, session_id=session_id)
    if node is None:
        raise RuntimeError("There is no virtual fit node for the given target id and session id.")
    if node.GetAttribute("VF:approvalStatus") is None:
        raise RuntimeError("A rank 1 virtual fit node appears to not have an approval state. This should not happen.")
    return node.GetAttribute("VF:approvalStatus") == "1"

def get_approval_from_virtual_fit_result_node(node : vtkMRMLTransformNode) -> bool:
    if node.GetAttribute("VF:approvalStatus") is None:
        raise RuntimeError("Node does not have a virtual fit approval status.")
    return node.GetAttribute("VF:approvalStatus") == "1"

def get_target_id_from_virtual_fit_result_node(node : vtkMRMLTransformNode) -> str:
    if node.GetAttribute("VF:targetID") is None:
        raise RuntimeError("Node does not have a target ID.")
    return node.GetAttribute("VF:targetID")
