"""Slicer-side helpers for working with the openlifu ``PhotoscanRegistration`` concept.

A *photoscan registration* is the photoscan-to-volume rigid alignment that pairs a photoscan
model with its skin-segmented volume. Each photoscan may have multiple stored registration
attempts; at most one is expected to be approved at a time. Downstream transducer-tracking
results refer back to a specific registration by id.

In the Slicer scene a registration is represented by a ``vtkMRMLTransformNode`` carrying the
attributes documented below. The mapping to the openlifu ``Session.photoscan_registrations``
list is provided by :func:`get_photoscan_registrations_in_openlifu_session_format` and
:func:`add_photoscan_registrations_from_openlifu_session_format`.
"""

from __future__ import annotations

from typing import List, Optional, TYPE_CHECKING

import logging

import numpy as np
import slicer
from slicer import vtkMRMLTransformNode

from OpenLIFULib.coordinate_system_utils import numpy_to_vtk_4x4
from OpenLIFULib.transform_conversion import create_openlifu2slicer_matrix
from OpenLIFULib.util import get_cloned_node

if TYPE_CHECKING:
    from openlifu.db.session import PhotoscanRegistration


# --- MRML attribute conventions ---------------------------------------------------------------

_ATTR_IS_PR = "isPR-PHOTOSCAN_TO_VOLUME"
_ATTR_REGISTRATION_ID = "PR:registrationID"
_ATTR_PHOTOSCAN_ID = "PR:photoscanID"
_ATTR_SESSION_ID = "PR:sessionID"
_ATTR_APPROVAL = "PR:approvalStatus"
_ATTR_REGISTRATION_INDEX = "PR:registrationIndex"


# --- ID generation ----------------------------------------------------------------------------

def generate_next_registration_id(photoscan_id: str, session_id: Optional[str]) -> str:
    """Return the next stable PR id for the given photoscan within the given session.

    Format: ``f"{photoscan_id}__pr__{nn:02d}"``. Counter increments across only those
    registrations that share the same ``photoscan_id``.
    """
    prefix = f"{photoscan_id}__pr__"
    existing_ids = {
        node.GetAttribute(_ATTR_REGISTRATION_ID)
        for node in get_photoscan_registration_nodes_in_scene(session_id=session_id)
        if node.GetAttribute(_ATTR_REGISTRATION_ID)
        and node.GetAttribute(_ATTR_REGISTRATION_ID).startswith(prefix)
    }
    if session_id is None:
        # get_photoscan_registration_nodes_in_scene(session_id=None) does not filter for the
        # *absence* of a session id; restrict here to sessionless nodes only.
        existing_ids = {
            i for i in existing_ids
            if any(
                n.GetAttribute(_ATTR_SESSION_ID) is None
                for n in get_photoscan_registration_nodes_in_scene()
                if n.GetAttribute(_ATTR_REGISTRATION_ID) == i
            )
        }
    n = 0
    while f"{prefix}{n:02d}" in existing_ids:
        n += 1
    return f"{prefix}{n:02d}"


def _next_registration_index(session_id: Optional[str]) -> int:
    """Return the next sequential ``PR:registrationIndex`` for a new PR in the given session."""
    nodes = list(get_photoscan_registration_nodes_in_scene(session_id=session_id))
    if session_id is None:
        nodes = [n for n in nodes if n.GetAttribute(_ATTR_SESSION_ID) is None]
    used = set()
    for n in nodes:
        raw = n.GetAttribute(_ATTR_REGISTRATION_INDEX)
        if raw is not None:
            try:
                used.add(int(raw))
            except ValueError:
                pass
    return (max(used) + 1) if used else 0


# --- Scene queries ----------------------------------------------------------------------------

def is_photoscan_registration_node(transform_node) -> bool:
    """Return True if ``transform_node`` is tagged as a photoscan registration node."""
    if transform_node is None:
        return False
    return transform_node.GetAttribute(_ATTR_IS_PR) == "1"


def get_photoscan_registration_nodes_in_scene(
    photoscan_id: Optional[str] = None,
    session_id: Optional[str] = None,
    approved_only: bool = False,
) -> List[vtkMRMLTransformNode]:
    """Return all PR transform nodes in the scene, filtered by the given criteria.

    Args:
        photoscan_id: if given, only registrations for this photoscan are returned.
        session_id: if given, only registrations tagged with this session id are returned.
            Pass ``None`` to disable session filtering (does NOT filter for sessionless nodes).
        approved_only: if True, only registrations with ``PR:approvalStatus == "1"`` are returned.
    """
    pr_nodes = [t for t in slicer.util.getNodesByClass('vtkMRMLTransformNode') if is_photoscan_registration_node(t)]

    if session_id is not None:
        pr_nodes = [t for t in pr_nodes if t.GetAttribute(_ATTR_SESSION_ID) == session_id]

    if photoscan_id is not None:
        pr_nodes = [t for t in pr_nodes if t.GetAttribute(_ATTR_PHOTOSCAN_ID) == photoscan_id]

    if approved_only:
        pr_nodes = [t for t in pr_nodes if t.GetAttribute(_ATTR_APPROVAL) == "1"]

    return pr_nodes


def get_photoscan_registration_by_id(
    registration_id: str,
    session_id: Optional[str],
) -> Optional[vtkMRMLTransformNode]:
    """Look up a PR node by stable id within a session (or sessionless if ``session_id`` is None)."""
    candidates = [
        n for n in get_photoscan_registration_nodes_in_scene(session_id=session_id)
        if n.GetAttribute(_ATTR_REGISTRATION_ID) == registration_id
        and (session_id is not None or n.GetAttribute(_ATTR_SESSION_ID) is None)
    ]
    if not candidates:
        return None
    if len(candidates) > 1:
        raise RuntimeError(
            f"Multiple PR nodes match registration_id={registration_id!r} in session {session_id!r}"
        )
    return candidates[0]


def get_all_photoscan_registrations(session_id: Optional[str]) -> List[vtkMRMLTransformNode]:
    """All PR nodes for the session, ordered by ``PR:registrationIndex``."""
    nodes = list(get_photoscan_registration_nodes_in_scene(session_id=session_id))
    if session_id is None:
        nodes = [n for n in nodes if n.GetAttribute(_ATTR_SESSION_ID) is None]

    def _sort_key(node):
        raw = node.GetAttribute(_ATTR_REGISTRATION_INDEX)
        if raw is None:
            return (1, 0)
        try:
            return (0, int(raw))
        except ValueError:
            return (1, 0)

    nodes.sort(key=_sort_key)
    return nodes


def get_photoscan_ids_with_approved_registrations(session_id: Optional[str]) -> List[str]:
    """Return the unique photoscan ids that have at least one approved PR in the session."""
    out: List[str] = []
    for node in get_all_photoscan_registrations(session_id):
        if node.GetAttribute(_ATTR_APPROVAL) != "1":
            continue
        pid = node.GetAttribute(_ATTR_PHOTOSCAN_ID)
        if pid and pid not in out:
            out.append(pid)
    return out


# --- Mutation ---------------------------------------------------------------------------------

def add_photoscan_registration(
    transform_node: vtkMRMLTransformNode,
    photoscan_id: str,
    session_id: Optional[str] = None,
    registration_id: Optional[str] = None,
    registration_index: Optional[int] = None,
    approval_status: bool = False,
    replace: bool = False,
    clone_node: bool = False,
) -> vtkMRMLTransformNode:
    """Tag ``transform_node`` as a PR for the given photoscan and add it to the scene attributes.

    Args:
        transform_node: source transform node.
        photoscan_id: photoscan this registration applies to.
        session_id: owning session id, or ``None`` for a sessionless registration.
        registration_id: stable id; if ``None``, a new id is generated.
        registration_index: explicit list position. If ``None``, assigned to the next available.
        approval_status: initial approval state.
        replace: if True, an existing node with the same ``registration_id`` is removed first.
        clone_node: if True, ``transform_node`` is cloned and the clone is tagged (leaving the
            original untouched).

    Returns: the transform node configured as a PR.
    """
    if registration_id is None:
        registration_id = generate_next_registration_id(photoscan_id, session_id)

    existing_same_id = [
        n for n in get_photoscan_registration_nodes_in_scene(session_id=session_id)
        if n.GetAttribute(_ATTR_REGISTRATION_ID) == registration_id
        and (session_id is not None or n.GetAttribute(_ATTR_SESSION_ID) is None)
    ]
    if existing_same_id:
        if replace:
            for n in existing_same_id:
                slicer.mrmlScene.RemoveNode(n)
        else:
            raise RuntimeError(
                f"A photoscan registration already exists with id {registration_id!r}; "
                f"pass replace=True to overwrite."
            )

    if registration_index is None:
        registration_index = _next_registration_index(session_id)

    if clone_node:
        pr_node: vtkMRMLTransformNode = get_cloned_node(transform_node)
    else:
        pr_node = transform_node

    pr_node.SetName(f"PR photoscan-volume {registration_id}")
    pr_node.SetAttribute(_ATTR_IS_PR, "1")
    pr_node.SetAttribute(_ATTR_APPROVAL, "1" if approval_status else "0")
    pr_node.SetAttribute(_ATTR_PHOTOSCAN_ID, photoscan_id)
    pr_node.SetAttribute(_ATTR_REGISTRATION_ID, registration_id)
    pr_node.SetAttribute(_ATTR_REGISTRATION_INDEX, str(registration_index))
    if session_id is not None:
        pr_node.SetAttribute(_ATTR_SESSION_ID, session_id)

    pr_node.CreateDefaultDisplayNodes()
    pr_node.GetDisplayNode().SetVisibility(False)

    return pr_node


def set_photoscan_registration_approval_for_node(approval_state: bool, transform_node: vtkMRMLTransformNode) -> None:
    """Set approval state on the given PR transform node."""
    if not is_photoscan_registration_node(transform_node):
        raise ValueError("The specified transform node is not a photoscan registration node")
    transform_node.SetAttribute(_ATTR_APPROVAL, "1" if approval_state else "0")


def set_photoscan_registration_approval_by_id(
    approval_state: bool,
    registration_id: str,
    session_id: Optional[str],
) -> None:
    """Set approval state on the PR identified by ``registration_id``."""
    node = get_photoscan_registration_by_id(registration_id, session_id)
    if node is None:
        raise RuntimeError(f"No photoscan registration found for registration_id={registration_id!r}")
    set_photoscan_registration_approval_for_node(approval_state, node)


def remove_photoscan_registration_by_id(registration_id: str, session_id: Optional[str]) -> None:
    """Remove the PR identified by ``registration_id`` from the scene."""
    node = get_photoscan_registration_by_id(registration_id, session_id)
    if node is not None:
        slicer.mrmlScene.RemoveNode(node)


def reindex_photoscan_registrations(session_id: Optional[str]) -> None:
    """Re-pack ``PR:registrationIndex`` to 0..N-1 in current order for the session."""
    for new_index, node in enumerate(get_all_photoscan_registrations(session_id)):
        node.SetAttribute(_ATTR_REGISTRATION_INDEX, str(new_index))


def clear_photoscan_registrations(session_id: Optional[str]) -> None:
    """Remove all PR nodes from the scene that match the given session id.

    Pass ``session_id=None`` to remove only PRs that have no affiliated session.
    """
    nodes_to_remove = list(get_photoscan_registration_nodes_in_scene(session_id=session_id))
    if session_id is None:
        nodes_to_remove = [n for n in nodes_to_remove if n.GetAttribute(_ATTR_SESSION_ID) is None]
    for node in nodes_to_remove:
        slicer.mrmlScene.RemoveNode(node)


# --- Accessors --------------------------------------------------------------------------------

def get_registration_id_from_photoscan_registration_node(node: vtkMRMLTransformNode) -> Optional[str]:
    return node.GetAttribute(_ATTR_REGISTRATION_ID)


def get_photoscan_id_from_photoscan_registration_node(node: vtkMRMLTransformNode) -> Optional[str]:
    return node.GetAttribute(_ATTR_PHOTOSCAN_ID)


def get_registration_index_from_photoscan_registration_node(node: vtkMRMLTransformNode) -> Optional[int]:
    raw = node.GetAttribute(_ATTR_REGISTRATION_INDEX)
    if raw is None:
        return None
    try:
        return int(raw)
    except ValueError:
        return None


def get_approval_from_photoscan_registration_node(node: vtkMRMLTransformNode) -> bool:
    raw = node.GetAttribute(_ATTR_APPROVAL)
    if raw is None:
        raise RuntimeError("Node does not have a photoscan registration approval status.")
    return raw == "1"


# --- Session-format converters ----------------------------------------------------------------

def get_photoscan_registrations_in_openlifu_session_format(session_id: str) -> List["PhotoscanRegistration"]:
    """Walk PR transform nodes in the scene and return them in openlifu Session representation,
    ordered by ``PR:registrationIndex``.
    """
    import openlifu.db.session

    out: List["PhotoscanRegistration"] = []
    for pr_node in get_all_photoscan_registrations(session_id=session_id):
        # Convert the PV transform back to LPS (the openlifu storage convention).
        transform_array = slicer.util.arrayFromTransformMatrix(pr_node, toWorld=True)
        openlifu2slicer_matrix = create_openlifu2slicer_matrix('mm')
        transform_openlifu = openlifu.db.session.ArrayTransform(
            matrix=np.linalg.inv(openlifu2slicer_matrix) @ transform_array,
            units='mm',
        )

        out.append(openlifu.db.session.PhotoscanRegistration(
            photoscan_id=pr_node.GetAttribute(_ATTR_PHOTOSCAN_ID),
            transform=transform_openlifu,
            approval=pr_node.GetAttribute(_ATTR_APPROVAL) == "1",
            id=pr_node.GetAttribute(_ATTR_REGISTRATION_ID),
        ))

    return out


def add_photoscan_registrations_from_openlifu_session_format(
    pr_list_openlifu: List["PhotoscanRegistration"],
    session_id: str,
    replace: bool = False,
) -> List[vtkMRMLTransformNode]:
    """Materialize the openlifu PR list into Slicer scene transform nodes tagged as PRs.

    Args:
        pr_list_openlifu: registrations in openlifu Session format.
        session_id: session id with which to tag the new transform nodes.
        replace: whether to overwrite an existing PR node with the same id.

    Returns: list of newly added PR transform nodes, in input order.
    """
    nodes_added: List[vtkMRMLTransformNode] = []
    openlifu2slicer_matrix = create_openlifu2slicer_matrix('mm')
    for list_index, pr in enumerate(pr_list_openlifu):
        transform_matrix_numpy = openlifu2slicer_matrix @ pr.transform.matrix
        transform_matrix_vtk = numpy_to_vtk_4x4(transform_matrix_numpy)
        new_node = slicer.mrmlScene.AddNewNodeByClass("vtkMRMLTransformNode")
        new_node.SetMatrixTransformToParent(transform_matrix_vtk)

        registration_id = pr.id
        if registration_id is None:
            registration_id = generate_next_registration_id(pr.photoscan_id, session_id)

        pr_node = add_photoscan_registration(
            transform_node=new_node,
            photoscan_id=pr.photoscan_id,
            session_id=session_id,
            registration_id=registration_id,
            registration_index=list_index,
            approval_status=pr.approval,
            replace=replace,
        )
        nodes_added.append(pr_node)

    return nodes_added


# --- TT->PR resolution helpers (used by callers that have a TT node and want its PR) ----------

def resolve_pr_node_for_tt(
    tt_node: vtkMRMLTransformNode,
    session_id: Optional[str],
    photoscan_id_fallback: Optional[str] = None,
    prefer_approved: bool = True,
) -> Optional[vtkMRMLTransformNode]:
    """Resolve the PR transform node that a given TT node was computed against.

    The TT node's ``TT:registrationID`` attribute is the primary reference. If absent (e.g. an
    older TT that pre-dates the split) we fall back to any PR for ``photoscan_id_fallback``,
    preferring an approved one when ``prefer_approved`` is True.

    Returns None if no candidate is found; the caller decides whether that is fatal.
    """
    registration_id = tt_node.GetAttribute("TT:registrationID")
    if registration_id:
        return get_photoscan_registration_by_id(registration_id, session_id)

    if photoscan_id_fallback is None:
        return None

    candidates = get_photoscan_registration_nodes_in_scene(
        photoscan_id=photoscan_id_fallback,
        session_id=session_id,
    )
    if not candidates:
        return None
    if prefer_approved:
        approved = [n for n in candidates if n.GetAttribute(_ATTR_APPROVAL) == "1"]
        if approved:
            return approved[0]
    return candidates[0]


# --- Logging shim -----------------------------------------------------------------------------

_logger = logging.getLogger(__name__)


def log_orphaned_tt(tt_result_id: Optional[str], registration_id: Optional[str], session_id: Optional[str]) -> None:
    """Used by session-load code to warn about TT entries whose PR reference cannot be resolved."""
    _logger.warning(
        "Skipping orphaned TT result %r in session %r: photoscan_registration_id=%r not found in session.",
        tt_result_id, session_id, registration_id,
    )
