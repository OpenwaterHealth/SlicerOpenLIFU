"""Per-module 3D view-state management.

Each top-level OpenLIFU module calls :func:`apply_module_view_state` from its
``enter()`` so the main 3D view shows the right combination of skin surface,
registered photoscan, and transducer pose for the user's current step. The
matrix of behaviors is:

==================  ====================================  =================  =====================
Module              Transducer pose                       Skin surface       Registered photoscan
==================  ====================================  =================  =====================
Session             farthest completed = approved TT,     visible unless     visible when approved
                    else approved VF, else leave alone    approved TT (then  TT exists, else hidden
                                                          hidden)
PrePlanning         left alone (combobox manages it)      visible            hidden
TransducerLoc.      any TT result, else approved VF,      visible            visible when TT exists,
                    else leave alone                                         else hidden
SonicationPlanner   approved TT, else approved VF,        hidden             visible when approved
                    else leave alone                                         TT exists, else hidden
SonicationControl   approved TT, else approved VF,        hidden             visible when approved
                    else leave alone                                         TT exists, else hidden
==================  ====================================  =================  =====================

PNP / pressure overlay visibility is intentionally NOT touched here -- it stays
under the SonicationPlanner "Render PNP" checkbox.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Optional

from OpenLIFULib.util import get_openlifu_data_parameter_node

if TYPE_CHECKING:
    from OpenLIFULib.transducer import SlicerOpenLIFUTransducer

# Module keys for the dispatch in apply_module_view_state.
SESSION = "session"
PREPLANNING = "preplanning"
LOCALIZATION = "localization"
SONICATION_PLANNER = "sonication_planner"
SONICATION_CONTROL = "sonication_control"


def apply_module_view_state(module_key: str) -> None:
    """Apply the per-module view-state defaults defined in the module-level docstring.

    Silently no-ops when there is no loaded session / transducer / volume; this
    is safe to call unconditionally from every module's ``enter()``.
    """
    transducer = _get_loaded_transducer()
    volume_node = _get_loaded_volume_node()

    approved_tt_node = _find_approved_tt_transducer_node()
    any_tt_node = approved_tt_node or _find_any_tt_transducer_node()
    approved_vf_node = _find_approved_vf_node()
    tt_photoscan_id = _photoscan_id_for_tt_node(approved_tt_node) \
        or _photoscan_id_for_tt_node(any_tt_node)

    if module_key == SESSION:
        if approved_tt_node is not None:
            _apply_transducer_pose(transducer, approved_tt_node)
            _set_skin_visible(volume_node, False)
            _set_photoscan_registered_visible(tt_photoscan_id, True, opacity=0.25)
        elif approved_vf_node is not None:
            _apply_transducer_pose(transducer, approved_vf_node)
            _set_skin_visible(volume_node, True)
            _set_photoscan_registered_visible(tt_photoscan_id, False)
        else:
            _set_skin_visible(volume_node, True)
            _set_photoscan_registered_visible(tt_photoscan_id, False)

    elif module_key == PREPLANNING:
        # Snap the transducer to the approved VF (if any) so going Back from later modules
        # never leaves the transducer sitting at a tracking pose. The VF combobox still owns
        # transitions to other (unapproved) VF candidates from here.
        if approved_vf_node is not None:
            _apply_transducer_pose(transducer, approved_vf_node)
        _set_skin_visible(volume_node, True)
        _set_photoscan_registered_visible(tt_photoscan_id, False)

    elif module_key == LOCALIZATION:
        pose_node = any_tt_node or approved_vf_node
        if pose_node is not None:
            _apply_transducer_pose(transducer, pose_node)
        _set_skin_visible(volume_node, True)
        _set_photoscan_registered_visible(tt_photoscan_id, any_tt_node is not None, opacity=0.5)

    elif module_key in (SONICATION_PLANNER, SONICATION_CONTROL):
        pose_node = approved_tt_node or approved_vf_node
        if pose_node is not None:
            _apply_transducer_pose(transducer, pose_node)
        _set_skin_visible(volume_node, False)
        _set_photoscan_registered_visible(
            tt_photoscan_id,
            approved_tt_node is not None,
            opacity=0.25,
        )


# ---------------------------------------------------------------------------
# Internals
# ---------------------------------------------------------------------------

def _get_loaded_session():
    return get_openlifu_data_parameter_node().loaded_session


def _get_session_id() -> Optional[str]:
    session = _get_loaded_session()
    return None if session is None else session.get_session_id()


def _get_loaded_transducer() -> "Optional[SlicerOpenLIFUTransducer]":
    session = _get_loaded_session()
    param_node = get_openlifu_data_parameter_node()
    if session is None:
        # Manual workflow: if exactly one transducer is loaded, use it; otherwise punt.
        if len(param_node.loaded_transducers) == 1:
            return next(iter(param_node.loaded_transducers.values()))
        return None
    transducer_id = session.get_transducer_id()
    return param_node.loaded_transducers.get(transducer_id)


def _get_loaded_volume_node():
    session = _get_loaded_session()
    if session is None or not session.volume_is_valid():
        return None
    return session.volume_node


def _find_approved_tt_transducer_node():
    """The approved transducer_to_volume TT result node, if any (at most one under the one-approval rule)."""
    from OpenLIFULib.transducer_tracking_results import (
        TransducerTrackingTransformType,
        get_transducer_tracking_result_nodes_in_scene,
    )
    session_id = _get_session_id()
    nodes = list(get_transducer_tracking_result_nodes_in_scene(
        session_id=session_id,
        photoscan_id=None,
        transform_type=TransducerTrackingTransformType.TRANSDUCER_TO_VOLUME,
    ))
    if session_id is None:
        nodes = [n for n in nodes if n.GetAttribute("TT:sessionID") is None]
    for n in nodes:
        if n.GetAttribute("TT:approvalStatus") == "1":
            return n
    return None


def _find_any_tt_transducer_node():
    """The most recently added transducer_to_volume TT result node, regardless of approval."""
    from OpenLIFULib.transducer_tracking_results import (
        TransducerTrackingTransformType,
        get_transducer_tracking_result_nodes_in_scene,
    )
    session_id = _get_session_id()
    nodes = list(get_transducer_tracking_result_nodes_in_scene(
        session_id=session_id,
        photoscan_id=None,
        transform_type=TransducerTrackingTransformType.TRANSDUCER_TO_VOLUME,
    ))
    if session_id is None:
        nodes = [n for n in nodes if n.GetAttribute("TT:sessionID") is None]
    return nodes[-1] if nodes else None


def _find_approved_vf_node():
    """The best approved virtual fit result node across all targets (None if none)."""
    from OpenLIFULib.virtual_fit_results import (
        get_approved_target_ids,
        get_best_virtual_fit_result_node,
    )
    session_id = _get_session_id()
    target_ids = get_approved_target_ids(session_id=session_id)
    for target_id in target_ids:
        node = get_best_virtual_fit_result_node(target_id=target_id, session_id=session_id)
        if node is not None:
            return node
    return None


def _photoscan_id_for_tt_node(tt_node) -> Optional[str]:
    if tt_node is None:
        return None
    return tt_node.GetAttribute("TT:photoscanID")


def _apply_transducer_pose(transducer, source_transform_node) -> None:
    if transducer is None or source_transform_node is None:
        return
    transducer.set_current_transform_to_match_transform_node(source_transform_node)
    transducer.set_visibility(True)


def _set_skin_visible(volume_node, visible: bool) -> None:
    if volume_node is None:
        return
    from OpenLIFULib.skinseg import get_skin_segmentation
    skin_mesh_node = get_skin_segmentation(volume_node)
    if skin_mesh_node is None:
        return  # Don't auto-generate; only toggle what's already there.
    if skin_mesh_node.GetDisplayVisibility() != int(visible):
        skin_mesh_node.SetDisplayVisibility(visible)


def _get_loaded_slicer_photoscan(photoscan_id: Optional[str]):
    if not photoscan_id:
        return None
    return get_openlifu_data_parameter_node().loaded_photoscans.get(photoscan_id)


def _set_photoscan_registered_visible(
    photoscan_id: Optional[str],
    visible: bool,
    opacity: Optional[float] = None,
) -> None:
    """Show or hide the photoscan model in the main view, parenting it to the
    saved photoscan_to_volume transform when becoming visible so it appears
    registered to the volume rather than at identity."""
    slicer_photoscan = _get_loaded_slicer_photoscan(photoscan_id)
    if slicer_photoscan is None or slicer_photoscan.model_node is None:
        return

    if visible:
        from OpenLIFULib.transducer_tracking_results import (
            TransducerTrackingTransformType,
            get_transducer_tracking_result_nodes_in_scene,
        )
        session_id = _get_session_id()
        pv_nodes = list(get_transducer_tracking_result_nodes_in_scene(
            session_id=session_id,
            photoscan_id=photoscan_id,
            transform_type=TransducerTrackingTransformType.PHOTOSCAN_TO_VOLUME,
        ))
        if session_id is None:
            pv_nodes = [n for n in pv_nodes if n.GetAttribute("TT:sessionID") is None]
        pv_node = pv_nodes[0] if pv_nodes else None
        if pv_node is not None:
            slicer_photoscan.model_node.SetAndObserveTransformNodeID(pv_node.GetID())
        # Restrict to default 3D views (clear any wizard-only view-scope).
        display_node = slicer_photoscan.model_node.GetDisplayNode()
        if display_node is not None:
            display_node.SetViewNodeIDs([])
            if opacity is not None:
                display_node.SetOpacity(opacity)
            else:
                display_node.SetOpacity(1.0)
        if slicer_photoscan.model_node.GetDisplayVisibility() != 1:
            slicer_photoscan.model_node.SetDisplayVisibility(True)
    else:
        if slicer_photoscan.model_node.GetDisplayVisibility() != 0:
            slicer_photoscan.model_node.SetDisplayVisibility(False)
        slicer_photoscan.model_node.SetAndObserveTransformNodeID(None)
