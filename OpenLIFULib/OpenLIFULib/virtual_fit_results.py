import slicer
from slicer import vtkMRMLTransformNode
from typing import Optional

def add_virtual_fit_result(
    transform_node: vtkMRMLTransformNode,
    target_id: str,
    session_id: Optional[str] = None,
    approval_status: bool = False,
    rank: int = 1,
) -> vtkMRMLTransformNode:
    """Clone a transform node and set it to be a "virtual fit result".
    This means the transform node clone will be named appropriately
    and will have a bunch of attributes set on it so that we can identify it
    later as a virtual fit result.

    Args:
        transform_node: The transform node to clone in order to create this virtual
            fit result. Probably this is the transducer transform node after a transducer
            got sent through the virtual fit process.
        target_id: The ID of the target for which the virtual fit was computed.
            For example in a session-based workflow this should be the id of the
            target openlifu.Point.
        session_id: The ID of the openlifu.Session during which the virtual fit took place.
            If not provided then it is assumed the virtual fit took place without
            a session -- in such a workflow it is probably up to the user what they
            want to do with the virtual fit result transform node since the virtual fit
            result has no openlifu session to be saved into.
        approval_status: The approval status of the virtual fit result
        rank: The rank of the virtual fit result in the ordering, from best to worst,
            of virtual fit results for this target. This can be needed because multiple candidate
            transforms can get generated by a virtual fitting algorithm.

    Returns: The newly created virtual fit result transform node
    """

    shNode = slicer.vtkMRMLSubjectHierarchyNode.GetSubjectHierarchyNode(slicer.mrmlScene)
    itemIDToClone = shNode.GetItemByDataNode(transform_node)
    clonedItemID = slicer.modules.subjecthierarchy.logic().CloneSubjectHierarchyItem(shNode, itemIDToClone)
    virtual_fit_result : vtkMRMLTransformNode = shNode.GetItemDataNode(clonedItemID)

    virtual_fit_result.SetName(f"VF {target_id} {rank}")
    virtual_fit_result.SetAttribute("isVirtualFitResult", "1")
    virtual_fit_result.SetAttribute("VF:targetID", target_id)
    virtual_fit_result.SetAttribute("VF:approvalStatus", "1" if approval_status else "0")
    virtual_fit_result.SetAttribute("VF:rank", str(rank))
    if session_id is not None:
        virtual_fit_result.SetAttribute("VF:sessionID", session_id)

    return virtual_fit_result