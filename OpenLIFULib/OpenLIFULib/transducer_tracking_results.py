"""Slicer-side helpers for working with the openlifu ``TransducerTrackingResult`` concept.

A *transducer tracking result* (TT result) registers a transducer pose against a volume in the
context of a particular photoscan. Each TT result references the photoscan-to-volume
:class:`PhotoscanRegistration` that it was computed against via the ``TT:registrationID``
attribute on its transform node.

In the Slicer scene, a TT result is a ``vtkMRMLTransformNode`` carrying the attributes
documented below. The mapping to the openlifu ``Session.transducer_tracking_results`` list is
provided by :func:`get_transducer_tracking_results_in_openlifu_session_format` and
:func:`add_transducer_tracking_results_from_openlifu_session_format`.

This module deliberately knows nothing about the photoscan-to-volume transform itself; that
is owned by :mod:`OpenLIFULib.photoscan_registrations`.
"""

from __future__ import annotations

from enum import Enum, auto
from typing import List, Optional, TYPE_CHECKING

import slicer
from slicer import vtkMRMLTransformNode

from OpenLIFULib.transform_conversion import (
    transducer_transform_node_to_openlifu,
    transducer_transform_node_from_openlifu,
)
from OpenLIFULib.util import get_cloned_node

if TYPE_CHECKING:
    from openlifu.db.session import TransducerTrackingResult
    from openlifu import Transducer


class TransducerTrackingTransformType(Enum):
    """Tag identifying a TT transform's role.

    Since the multi-registration split, only ``TRANSDUCER_TO_VOLUME`` is valid for TT
    results. ``PHOTOSCAN_TO_VOLUME`` is preserved here only so that existing call sites that
    import the enum continue to import; passing it to any function in this module raises.
    The photoscan-to-volume transform now lives in :mod:`OpenLIFULib.photoscan_registrations`.
    """

    TRANSDUCER_TO_VOLUME = auto()
    PHOTOSCAN_TO_VOLUME = auto()  # deprecated; will be removed once all callers migrate


# --- MRML attribute conventions ---------------------------------------------------------------

_ATTR_PHOTOSCAN_ID = "TT:photoscanID"
_ATTR_TARGET_ID = "TT:targetID"
_ATTR_SESSION_ID = "TT:sessionID"
_ATTR_APPROVAL = "TT:approvalStatus"
_ATTR_RESULT_ID = "TT:resultID"
_ATTR_RESULT_INDEX = "TT:resultIndex"
_ATTR_REGISTRATION_ID = "TT:registrationID"  # reference to the owning PhotoscanRegistration

_NO_TARGET_TOKEN = "notarget"


def _format_target_token(target_id: Optional[str]) -> str:
    return target_id if target_id else _NO_TARGET_TOKEN


# --- ID generation ----------------------------------------------------------------------------

def generate_next_result_id(photoscan_id: str, target_id: Optional[str], session_id: Optional[str]) -> str:
    """Compute the next stable TT result id for the given (photoscan, target) within the given session.

    The id format is ``f"{photoscan_id}__{target_id or 'notarget'}__{nn:02d}"``. The counter is
    incremented only across results whose photoscan_id+target_id match, so unrelated results do
    not push the counter.
    """
    prefix = f"{photoscan_id}__{_format_target_token(target_id)}__"
    existing_ids = {
        node.GetAttribute(_ATTR_RESULT_ID)
        for node in get_transducer_tracking_result_nodes_in_scene(session_id=session_id)
        if node.GetAttribute(_ATTR_RESULT_ID) and node.GetAttribute(_ATTR_RESULT_ID).startswith(prefix)
    }
    if session_id is None:
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
    """Return the next sequential ``TT:resultIndex`` for a new TT result in the given session."""
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


# --- Scene queries ----------------------------------------------------------------------------

def is_transducer_tracking_result_node(transform_node) -> bool:
    """Return True if ``transform_node`` is a TT (transducer-to-volume) result node."""
    if transform_node is None:
        return False
    return transform_node.GetAttribute(
        f"isTT-{TransducerTrackingTransformType.TRANSDUCER_TO_VOLUME.name}"
    ) == "1"


def get_transducer_tracking_result_nodes_in_scene(
    photoscan_id: Optional[str] = None,
    session_id: Optional[str] = None,
    approved_only: bool = False,
) -> List[vtkMRMLTransformNode]:
    """Return all TT result nodes in the scene, filtered by the given criteria.

    Args:
        photoscan_id: if given, only TT results for this photoscan are returned.
        session_id: if given, only TT results tagged with this session id are returned.
            Pass ``None`` to disable session filtering (does NOT filter for sessionless nodes).
        approved_only: if True, only TT results with ``TT:approvalStatus == "1"`` are returned.
    """
    tt_nodes = [t for t in slicer.util.getNodesByClass('vtkMRMLTransformNode') if is_transducer_tracking_result_node(t)]

    if session_id is not None:
        tt_nodes = [t for t in tt_nodes if t.GetAttribute(_ATTR_SESSION_ID) == session_id]

    if photoscan_id is not None:
        tt_nodes = [t for t in tt_nodes if t.GetAttribute(_ATTR_PHOTOSCAN_ID) == photoscan_id]

    if approved_only:
        tt_nodes = [t for t in tt_nodes if t.GetAttribute(_ATTR_APPROVAL) == "1"]

    return tt_nodes


def get_all_transducer_tracking_results(session_id: Optional[str]) -> List[vtkMRMLTransformNode]:
    """All TT result nodes for the session, ordered by ``TT:resultIndex``.

    Pass ``session_id=None`` to get only TT results that have no affiliated session.
    """
    nodes = list(get_transducer_tracking_result_nodes_in_scene(session_id=session_id))
    if session_id is None:
        nodes = [n for n in nodes if n.GetAttribute(_ATTR_SESSION_ID) is None]

    def _sort_key(node):
        raw = node.GetAttribute(_ATTR_RESULT_INDEX)
        if raw is None:
            return (1, 0)
        try:
            return (0, int(raw))
        except ValueError:
            return (1, 0)

    nodes.sort(key=_sort_key)
    return nodes


def get_transducer_tracking_result_by_id(
    result_id: str,
    session_id: Optional[str],
) -> Optional[vtkMRMLTransformNode]:
    """Look up a TT result node by stable ``TT:resultID`` within a session."""
    candidates = [
        n for n in get_transducer_tracking_result_nodes_in_scene(session_id=session_id)
        if n.GetAttribute(_ATTR_RESULT_ID) == result_id
        and (session_id is not None or n.GetAttribute(_ATTR_SESSION_ID) is None)
    ]
    if not candidates:
        return None
    if len(candidates) > 1:
        raise RuntimeError(f"Multiple TT result nodes match result_id={result_id!r}")
    return candidates[0]


def get_transducer_tracking_result(
    photoscan_id: str,
    session_id: Optional[str],
) -> Optional[vtkMRMLTransformNode]:
    """Retrieve THE TT result for the given photoscan, or None if there isn't one.

    Since the multi-result refactor a photoscan can have many TT results, so this helper now
    raises if more than one matches; callers that need to enumerate should use
    :func:`get_transducer_tracking_result_nodes_in_scene` directly.
    """
    candidates = list(get_transducer_tracking_result_nodes_in_scene(
        photoscan_id=photoscan_id, session_id=session_id))
    if session_id is None:
        candidates = [t for t in candidates if t.GetAttribute(_ATTR_SESSION_ID) is None]
    if not candidates:
        return None
    if len(candidates) > 1:
        raise RuntimeError(
            f"There are {len(candidates)} TT result nodes for photoscan {photoscan_id} "
            + (f"and session {session_id}" if session_id is not None else "with no session.")
            + " Call get_transducer_tracking_result_nodes_in_scene() to enumerate."
        )
    return candidates[0]


def get_photoscan_ids_with_results(session_id: Optional[str], approved_only: bool = False) -> List[str]:
    """Unique photoscan ids that have at least one TT result in the session.

    Args:
        session_id: optional session id. If ``None``, only TT results with no affiliated session are considered.
        approved_only: if True, only photoscans with at least one approved TT result are returned.
    """
    out: List[str] = []
    for node in get_all_transducer_tracking_results(session_id):
        if approved_only and node.GetAttribute(_ATTR_APPROVAL) != "1":
            continue
        pid = node.GetAttribute(_ATTR_PHOTOSCAN_ID)
        if pid and pid not in out:
            out.append(pid)
    return out


# --- Mutation ---------------------------------------------------------------------------------

def add_transducer_tracking_result(
    transform_node: vtkMRMLTransformNode,
    photoscan_id: str,
    session_id: Optional[str] = None,
    target_id: Optional[str] = None,
    registration_id: Optional[str] = None,
    result_id: Optional[str] = None,
    result_index: Optional[int] = None,
    approval_status: bool = False,
    replace: bool = False,
    clone_node: bool = False,
    transform_type: Optional[TransducerTrackingTransformType] = None,
) -> vtkMRMLTransformNode:
    """Add a transducer-to-volume result node by tagging it with the appropriate attributes.

    Args:
        transform_node: source transform node.
        photoscan_id: photoscan this result is computed for.
        session_id: owning session id, or ``None`` for a sessionless result.
        target_id: target point id the result was computed for, or ``None``.
        registration_id: id of the :class:`openlifu.db.session.PhotoscanRegistration` this
            result was computed against. Stored on the node as ``TT:registrationID``.
        result_id: stable result id. If ``None``, a new id is generated.
        result_index: explicit list position. If ``None``, assigned to next available.
        approval_status: initial approval state.
        replace: if True, an existing node with the same ``result_id`` is removed first.
        clone_node: if True, ``transform_node`` is cloned and the clone is tagged.
        transform_type: legacy parameter; only ``TRANSDUCER_TO_VOLUME`` is accepted. Passing
            ``PHOTOSCAN_TO_VOLUME`` raises ``NotImplementedError`` — the PR transform now lives
            in :mod:`OpenLIFULib.photoscan_registrations`.

    Returns: the transform node configured as a TT result.
    """
    if transform_type is not None and transform_type != TransducerTrackingTransformType.TRANSDUCER_TO_VOLUME:
        raise NotImplementedError(
            "add_transducer_tracking_result no longer accepts PHOTOSCAN_TO_VOLUME; use "
            "OpenLIFULib.photoscan_registrations.add_photoscan_registration instead."
        )

    if result_id is None:
        result_id = generate_next_result_id(photoscan_id, target_id, session_id)

    existing_same_id = [
        n for n in get_transducer_tracking_result_nodes_in_scene(session_id=session_id)
        if n.GetAttribute(_ATTR_RESULT_ID) == result_id
        and (session_id is not None or n.GetAttribute(_ATTR_SESSION_ID) is None)
    ]
    if existing_same_id:
        if replace:
            for n in existing_same_id:
                slicer.mrmlScene.RemoveNode(n)
        else:
            raise RuntimeError(
                f"A TT result already exists with id {result_id!r}; pass replace=True to overwrite."
            )

    if result_index is None:
        result_index = _next_result_index(session_id)

    if clone_node:
        tt_node: vtkMRMLTransformNode = get_cloned_node(transform_node)
    else:
        tt_node = transform_node

    tt_node.SetName(f"TT transducer-volume {result_id}")
    tt_node.SetAttribute(f"isTT-{TransducerTrackingTransformType.TRANSDUCER_TO_VOLUME.name}", "1")
    tt_node.SetAttribute(_ATTR_APPROVAL, "1" if approval_status else "0")
    tt_node.SetAttribute(_ATTR_PHOTOSCAN_ID, photoscan_id)
    tt_node.SetAttribute(_ATTR_RESULT_ID, result_id)
    tt_node.SetAttribute(_ATTR_RESULT_INDEX, str(result_index))
    if target_id is not None:
        tt_node.SetAttribute(_ATTR_TARGET_ID, target_id)
    if session_id is not None:
        tt_node.SetAttribute(_ATTR_SESSION_ID, session_id)
    if registration_id is not None:
        tt_node.SetAttribute(_ATTR_REGISTRATION_ID, registration_id)

    tt_node.CreateDefaultDisplayNodes()
    tt_node.GetDisplayNode().SetVisibility(False)

    return tt_node


def set_transducer_tracking_approval_for_node(approval_state: bool, transform_node: vtkMRMLTransformNode) -> None:
    """Set approval state on the given TT result transform node."""
    if not is_transducer_tracking_result_node(transform_node):
        raise ValueError("The specified transform node is not a transducer localization result node")
    transform_node.SetAttribute(_ATTR_APPROVAL, "1" if approval_state else "0")


def set_transducer_tracking_approval_by_id(
    approval_state: bool,
    result_id: str,
    session_id: Optional[str],
) -> None:
    """Set the approval state on the TT result identified by ``result_id``."""
    node = get_transducer_tracking_result_by_id(result_id, session_id)
    if node is None:
        raise RuntimeError(f"No TT result found for result_id={result_id!r}")
    set_transducer_tracking_approval_for_node(approval_state, node)


def remove_transducer_tracking_result_by_id(result_id: str, session_id: Optional[str]) -> None:
    """Remove the TT result identified by ``result_id`` from the scene."""
    node = get_transducer_tracking_result_by_id(result_id, session_id)
    if node is not None:
        slicer.mrmlScene.RemoveNode(node)


def reindex_transducer_tracking_results(session_id: Optional[str]) -> None:
    """Re-pack ``TT:resultIndex`` to 0..N-1 in current order for the session."""
    for new_index, node in enumerate(get_all_transducer_tracking_results(session_id)):
        node.SetAttribute(_ATTR_RESULT_INDEX, str(new_index))


def clear_transducer_tracking_results(session_id: Optional[str]) -> None:
    """Remove all TT result nodes from the scene that match the given session id.

    Pass ``session_id=None`` to remove only TT results that have no affiliated session.
    """
    nodes_to_remove = list(get_transducer_tracking_result_nodes_in_scene(session_id=session_id))
    if session_id is None:
        nodes_to_remove = [t for t in nodes_to_remove if t.GetAttribute(_ATTR_SESSION_ID) is None]
    for node in nodes_to_remove:
        slicer.mrmlScene.RemoveNode(node)


# --- Accessors --------------------------------------------------------------------------------

def get_result_id_from_transducer_tracking_result_node(node: vtkMRMLTransformNode) -> Optional[str]:
    return node.GetAttribute(_ATTR_RESULT_ID)


def get_target_id_from_transducer_tracking_result_node(node: vtkMRMLTransformNode) -> Optional[str]:
    return node.GetAttribute(_ATTR_TARGET_ID)


def get_registration_id_from_transducer_tracking_result_node(node: vtkMRMLTransformNode) -> Optional[str]:
    return node.GetAttribute(_ATTR_REGISTRATION_ID)


def get_result_index_from_transducer_tracking_result_node(node: vtkMRMLTransformNode) -> Optional[int]:
    raw = node.GetAttribute(_ATTR_RESULT_INDEX)
    if raw is None:
        return None
    try:
        return int(raw)
    except ValueError:
        return None


def get_approval_from_transducer_tracking_result_node(node: vtkMRMLTransformNode) -> bool:
    raw = node.GetAttribute(_ATTR_APPROVAL)
    if raw is None:
        raise RuntimeError("Node does not have a transducer localization approval status.")
    return raw == "1"


def get_transform_type_from_transducer_tracking_result_node(node: vtkMRMLTransformNode) -> TransducerTrackingTransformType:
    """Always returns TRANSDUCER_TO_VOLUME under the split; raises if the node isn't a TT node."""
    if node.GetAttribute(f"isTT-{TransducerTrackingTransformType.TRANSDUCER_TO_VOLUME.name}") == "1":
        return TransducerTrackingTransformType.TRANSDUCER_TO_VOLUME
    raise RuntimeError("The given node is not a transducer localization result transform.")


def get_photoscan_id_from_transducer_tracking_result(node: vtkMRMLTransformNode) -> str:
    """Return the photoscan ID associated with a TT result transform node."""
    if not isinstance(node, vtkMRMLTransformNode):
        raise ValueError("Invalid transducer localization result type.")
    pid = node.GetAttribute(_ATTR_PHOTOSCAN_ID)
    if pid is None:
        raise RuntimeError("Transducer localization result does not have a photoscan ID.")
    return pid


# --- Session-format converters ----------------------------------------------------------------

def get_transducer_tracking_results_in_openlifu_session_format(
    session_id: str,
    transducer_units: str,
) -> List["TransducerTrackingResult"]:
    """Walk TT result transform nodes in the scene and return them in openlifu Session
    representation, ordered by ``TT:resultIndex``.

    See also the reverse function :func:`add_transducer_tracking_results_from_openlifu_session_format`.
    """
    import openlifu.db.session

    out: List["TransducerTrackingResult"] = []
    for tt_node in get_all_transducer_tracking_results(session_id=session_id):
        tv_openlifu = transducer_transform_node_to_openlifu(
            transform_node=tt_node,
            transducer_units=transducer_units,
        )
        out.append(openlifu.db.session.TransducerTrackingResult(
            photoscan_id=tt_node.GetAttribute(_ATTR_PHOTOSCAN_ID),
            transducer_to_volume_transform=tv_openlifu,
            photoscan_registration_id=tt_node.GetAttribute(_ATTR_REGISTRATION_ID),
            approval=tt_node.GetAttribute(_ATTR_APPROVAL) == "1",
            id=tt_node.GetAttribute(_ATTR_RESULT_ID),
            target_id=tt_node.GetAttribute(_ATTR_TARGET_ID),
        ))

    return out


def add_transducer_tracking_results_from_openlifu_session_format(
    tt_results_openlifu: List["TransducerTrackingResult"],
    session_id: str,
    transducer: "Transducer",
    replace: bool = False,
) -> List[vtkMRMLTransformNode]:
    """Materialize the openlifu TT list into Slicer scene transform nodes tagged as TT results.

    Each result's ``photoscan_registration_id`` is stored on the node as ``TT:registrationID``;
    callers that need to resolve it to a PR node should use
    :func:`OpenLIFULib.photoscan_registrations.resolve_pr_node_for_tt`.

    Args:
        tt_results_openlifu: TT results in openlifu Session format.
        session_id: session id with which to tag the new transform nodes.
        transducer: the openlifu ``Transducer`` of the session (needed for unit conversion).
        replace: whether to overwrite an existing TT node with the same id.

    Returns: list of newly added TT transform nodes, in input order.
    """
    nodes_added: List[vtkMRMLTransformNode] = []
    for list_index, tt_result in enumerate(tt_results_openlifu):
        tv_node = transducer_transform_node_from_openlifu(
            openlifu_transform_matrix=tt_result.transducer_to_volume_transform.matrix,
            transform_units=tt_result.transducer_to_volume_transform.units,
            transducer=transducer,
        )

        result_id = tt_result.id
        target_id = tt_result.target_id
        if result_id is None:
            result_id = generate_next_result_id(tt_result.photoscan_id, target_id, session_id)

        tv_node = add_transducer_tracking_result(
            transform_node=tv_node,
            photoscan_id=tt_result.photoscan_id,
            session_id=session_id,
            target_id=target_id,
            registration_id=tt_result.photoscan_registration_id,
            result_id=result_id,
            result_index=list_index,
            approval_status=tt_result.approval,
            replace=replace,
        )

        nodes_added.append(tv_node)

    return nodes_added
