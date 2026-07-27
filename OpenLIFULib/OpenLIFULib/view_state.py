"""Per-module 3D view-state management.

Each top-level OpenLIFU module calls :func:`apply_module_view_state` from its
``enter()`` so the main 3D view shows the right combination of skin surface,
registered photoscan, and transducer pose for the user's current step. The
matrix of behaviors is:

==================  ====================================  =================  =====================
Module              Transducer pose                       Skin surface       Registered photoscan
==================  ====================================  =================  =====================
Home                hidden                                 hidden             hidden
Session             farthest completed = approved TT,     visible unless     visible when approved
                    else approved VF, else leave alone    approved TT (then  TT exists, else hidden
                                                          hidden)
PrePlanning         left alone (combobox manages it)      visible            hidden
TransducerLoc.      any TT result, else hide                              visible            visible when TT exists,
                    (empty table -> hidden, no                                                 else hidden
                    approved-VF fallback)
SonicationPlanner   active Solution.array_transform       hidden             visible when approved
                    (see #622), else legacy fallback:                        TT exists, else hidden
                    approved TT for a normal solution,
                    approved VF for a pre-solution,
                    else leave alone
SonicationControl   same as SonicationPlanner             hidden             visible when approved
                                                                             TT exists, else hidden
==================  ====================================  =================  =====================

PNP / pressure overlay visibility is intentionally NOT touched here -- it stays
under the SonicationPlanner "Render PNP" checkbox.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Optional

from OpenLIFULib.util import active_solution_is_pre_solution, get_app_state

if TYPE_CHECKING:
    from OpenLIFULib.transducer import SlicerOpenLIFUTransducer

# Module keys for the dispatch in apply_module_view_state.
HOME = "home"
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

    if module_key == HOME:
        # Home is a status / landing page and does not visualize any scene content.
        # A loaded session's segmentation, photoscan, and transducer must not leak
        # into Home's 3D view; hide everything the view-state layer knows about.
        # The nodes stay in the scene (Home does not unload the session), only their
        # visibility is toggled off (#618).
        if transducer is not None:
            transducer.set_visibility(False)
        _set_skin_visible(volume_node, False)
        _set_photoscan_registered_visible_for_tt(None, False)

    elif module_key == SESSION:
        if approved_tt_node is not None:
            _apply_transducer_pose(transducer, approved_tt_node)
            _set_skin_visible(volume_node, False)
            _set_photoscan_registered_visible_for_tt(approved_tt_node, True, opacity=0.25)
        elif approved_vf_node is not None:
            _apply_transducer_pose(transducer, approved_vf_node)
            _set_skin_visible(volume_node, True)
            _set_photoscan_registered_visible_for_tt(None, False)
        else:
            _set_skin_visible(volume_node, True)
            _set_photoscan_registered_visible_for_tt(None, False)

    elif module_key == PREPLANNING:
        # Snap the transducer to the approved VF (if any) so going Back from later modules
        # never leaves the transducer sitting at a tracking pose. The VF combobox still owns
        # transitions to other (unapproved) VF candidates from here.
        if approved_vf_node is not None:
            _apply_transducer_pose(transducer, approved_vf_node)
        _set_skin_visible(volume_node, True)
        _set_photoscan_registered_visible_for_tt(None, False)

    elif module_key == LOCALIZATION:
        # Prefer the approved TT (matches what Solution/Control display) so going Back
        # from later steps doesn't leave the view stuck on a stale or different pose.
        # No approved-VF fallback here: TL is the page where the user *creates* TT
        # results, so with an empty TT table there is nothing to represent. Falling back
        # to the approved VF pose would leak Pre-Planning's virtual-fit visualization
        # onto the TL page when returning to it from Pre-Planning with an empty table
        # (issue #602). Explicitly hide the transducer instead, so the empty-table view
        # matches the empty-table intent.
        pose_node = approved_tt_node or any_tt_node
        if pose_node is not None:
            _apply_transducer_pose(transducer, pose_node)
        elif transducer is not None:
            transducer.set_visibility(False)
        _set_skin_visible(volume_node, True)
        # Show the registered photoscan whenever any TT or an approved PR exists. When
        # only a PR exists (e.g. the user just approved a registration but has not yet
        # run tracking), still show its photoscan; revoking approval hides it again.
        photoscan_id_for_view: Optional[str] = _photoscan_id_for_tt_node(any_tt_node)
        if photoscan_id_for_view is None:
            latest_pr = _find_latest_pr_node(approved_only=True)
            if latest_pr is not None:
                photoscan_id_for_view = latest_pr.GetAttribute("PR:photoscanID")
        if photoscan_id_for_view is not None:
            _set_photoscan_registered_visible_for_tt(
                approved_tt_node or any_tt_node, True, opacity=0.5,
                photoscan_id_override=photoscan_id_for_view)
        else:
            _set_photoscan_registered_visible_for_tt(None, False)

    elif module_key in (SONICATION_PLANNER, SONICATION_CONTROL):
        # SlicerOpenLIFU#622: prefer the transducer pose that was actually used when the
        # active Solution was computed (persisted on ``SolutionInfo.array_transform``). This
        # pins the PNP / intensity volume rendering to the compute-time pose so it stays
        # invariant under later approval churn (a VF whose approval was revoked, or a fresh
        # TT that displaced the earlier approved one). Legacy solutions saved before this
        # field existed have ``array_transform is None``; fall back to the previous
        # "best-current-approved VF/TT" behavior in that case.
        is_pre_solution = active_solution_is_pre_solution()
        active_array_transform = _get_active_solution_array_transform()
        if active_array_transform is not None:
            _apply_transducer_pose_from_openlifu_array_transform(transducer, active_array_transform)
        else:
            # Legacy fallback path (pre-#622): pick approved VF for pre-solutions, else TT.
            if is_pre_solution:
                pose_node = approved_vf_node or approved_tt_node
            else:
                pose_node = approved_tt_node or approved_vf_node
            if pose_node is not None:
                _apply_transducer_pose(transducer, pose_node)
        _set_skin_visible(volume_node, False)
        _set_photoscan_registered_visible_for_tt(
            approved_tt_node, approved_tt_node is not None and not is_pre_solution, opacity=0.25,
        )


# ---------------------------------------------------------------------------
# Internals
# ---------------------------------------------------------------------------

def _get_loaded_session():
    return get_app_state().loaded_session


def _get_session_id() -> Optional[str]:
    session = _get_loaded_session()
    return None if session is None else session.get_session_id()


def _get_loaded_transducer() -> "Optional[SlicerOpenLIFUTransducer]":
    session = _get_loaded_session()
    param_node = get_app_state()
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
        get_transducer_tracking_result_nodes_in_scene,
    )
    session_id = _get_session_id()
    nodes = list(get_transducer_tracking_result_nodes_in_scene(
        session_id=session_id,
        photoscan_id=None,
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
        get_transducer_tracking_result_nodes_in_scene,
    )
    session_id = _get_session_id()
    nodes = list(get_transducer_tracking_result_nodes_in_scene(
        session_id=session_id,
        photoscan_id=None,
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


def _find_latest_pr_node(approved_only: bool = False):
    """The most recently added PR transform node in the current session, or None."""
    from OpenLIFULib.photoscan_registrations import (
        get_photoscan_registration_nodes_in_scene,
    )
    session_id = _get_session_id()
    nodes = list(get_photoscan_registration_nodes_in_scene(
        session_id=session_id, approved_only=approved_only))
    if session_id is None:
        nodes = [n for n in nodes if n.GetAttribute("PR:sessionID") is None]
    if not nodes:
        return None

    def _idx(n):
        raw = n.GetAttribute("PR:registrationIndex")
        try:
            return int(raw) if raw is not None else -1
        except ValueError:
            return -1

    nodes.sort(key=_idx)
    return nodes[-1]


def _photoscan_id_for_tt_node(tt_node) -> Optional[str]:
    if tt_node is None:
        return None
    return tt_node.GetAttribute("TT:photoscanID")


def _apply_transducer_pose(transducer, source_transform_node) -> None:
    if transducer is None or source_transform_node is None:
        return
    transducer.set_current_transform_to_match_transform_node(source_transform_node)
    transducer.set_visibility(True)


def _apply_transducer_pose_from_openlifu_array_transform(transducer, array_transform) -> None:
    """Snap ``transducer.transform_node`` to the given openlifu ``ArrayTransform``.

    ``array_transform`` is stored in openlifu conventions (LPS coords, transducer native units);
    a Slicer transform node stores in RAS + mm, so we convert with the same matrix used in
    :func:`OpenLIFULib.transform_conversion.transducer_transform_node_to_openlifu`. Also clears
    the "matching_transform" attribute so the transducer's color reverts to the neutral
    solution-mode color (rather than showing VF-blue or TT-green as if it were parented under
    that specific result node) -- the pose came from a persisted matrix on the SolutionInfo,
    not from a currently-live VF / TT transform node.
    """
    if transducer is None or array_transform is None:
        return
    import vtk
    from OpenLIFULib.coordinate_system_utils import numpy_to_vtk_4x4
    from OpenLIFULib.transform_conversion import create_openlifu2slicer_matrix

    openlifu2slicer = create_openlifu2slicer_matrix(array_transform.units)
    slicer_matrix = openlifu2slicer @ array_transform.matrix
    transducer.transform_node.SetMatrixTransformToParent(numpy_to_vtk_4x4(slicer_matrix))
    transducer.set_matching_transform(None)
    transducer.set_visibility(True)


def _get_active_solution_array_transform():
    """Return the ``array_transform`` on the active Solution's :class:`SolutionInfo`, or None.

    Reads ``session.solutions`` (list of ``openlifu.db.session.SolutionInfo``) for the entry
    whose ``solution_id`` matches the active solution id and returns its ``array_transform``.
    Returns ``None`` when there is no active solution, no matching SolutionInfo entry (e.g.
    legacy sessions saved before SlicerOpenLIFU#611 provenance), or the field is ``None``
    (legacy solutions saved before openlifu-python#491 introduced this field).
    """
    from OpenLIFULib.util import get_active_solution
    active = get_active_solution()
    if active is None:
        return None
    active_id = active.solution.solution.id
    loaded_session = get_app_state().loaded_session
    if loaded_session is None:
        return None
    for entry in loaded_session.session.session.solutions:
        if entry.solution_id == active_id:
            return getattr(entry, "array_transform", None)
    return None


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
    return get_app_state().loaded_photoscans.get(photoscan_id)


def _set_photoscan_registered_visible_for_tt(
    tt_node,
    visible: bool,
    opacity: Optional[float] = None,
    photoscan_id_override: Optional[str] = None,
) -> None:
    """Show or hide the photoscan model in the main view, parenting it to the
    photoscan-to-volume registration that is paired with the given TT result so
    pose and registration come from the same record (otherwise the model can drift
    relative to the transducer when multiple registrations exist for the same photoscan).

    Falls back to the latest approved (then any) PR for the photoscan when ``tt_node``
    is None. ``photoscan_id_override`` lets callers (e.g. LOCALIZATION mode after a fresh
    PR but before any TT) pick a photoscan directly when no TT result is available yet.
    """
    photoscan_id = photoscan_id_override or _photoscan_id_for_tt_node(tt_node)

    if not visible:
        # Hide every loaded photoscan when no specific one was identified, so callers
        # that just want "no photoscan on screen" (e.g. PrePlanning) work regardless
        # of which photoscan was last shown.
        photoscans_to_hide = (
            [_get_loaded_slicer_photoscan(photoscan_id)] if photoscan_id is not None
            else list(get_app_state().loaded_photoscans.values())
        )
        for slicer_photoscan in photoscans_to_hide:
            if slicer_photoscan is None or slicer_photoscan.model_node is None:
                continue
            if slicer_photoscan.model_node.GetDisplayVisibility() != 0:
                slicer_photoscan.model_node.SetDisplayVisibility(False)
            slicer_photoscan.model_node.SetAndObserveTransformNodeID(None)
        return

    slicer_photoscan = _get_loaded_slicer_photoscan(photoscan_id)
    if slicer_photoscan is None or slicer_photoscan.model_node is None:
        return

    from OpenLIFULib.photoscan_registrations import (
        get_photoscan_registration_by_id,
        get_photoscan_registration_nodes_in_scene,
    )
    session_id = _get_session_id()
    pr_node = None
    # Prefer the PR that is explicitly paired with this TT (via TT:registrationID).
    if tt_node is not None:
        registration_id = tt_node.GetAttribute("TT:registrationID")
        if registration_id:
            pr_node = get_photoscan_registration_by_id(registration_id, session_id)
    # Fall back: prefer an approved PR for the photoscan, then any PR.
    if pr_node is None:
        pr_nodes = get_photoscan_registration_nodes_in_scene(
            session_id=session_id,
            photoscan_id=photoscan_id,
            approved_only=True,
        )
        if not pr_nodes:
            pr_nodes = get_photoscan_registration_nodes_in_scene(
                session_id=session_id,
                photoscan_id=photoscan_id,
            )
        if session_id is None:
            pr_nodes = [n for n in pr_nodes if n.GetAttribute("PR:sessionID") is None]
        pr_node = pr_nodes[0] if pr_nodes else None
    if pr_node is not None:
        slicer_photoscan.model_node.SetAndObserveTransformNodeID(pr_node.GetID())
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
