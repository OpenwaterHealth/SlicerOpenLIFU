"""Solution source-status resolution.

Each Solution is computed against a specific VF or TT result on the loaded session.
``SolutionInfo.transducer_transform_source`` records the KIND ("virtual_fit" vs.
"localization") and ``SolutionInfo.transducer_transform_source_id`` records the specific
identifier (see openlifu-python#492). This module answers "is the source of this solution
still LIVE?" by cross-checking against the current scene state.

Status values -- returned as strings so they can be used directly in table cells /
tooltips / status labels:

* ``"live"``     -- Source VF / TT node exists in the scene AND is currently approved.
                    Safe to view; safe to send to hardware.
* ``"revoked"``  -- Source exists but is no longer approved. Safe to view (pose is still
                    reproducible via ``array_transform``); user should be warned before
                    sending to hardware.
* ``"missing"``  -- Source has been deleted from the session (e.g. VF result removed,
                    photoscan deleted cascading a TT delete). Safe to view; hard warn on
                    hardware send.
* ``"legacy"``   -- ``SolutionInfo.transducer_transform_source_id`` is None, meaning the
                    solution was computed before this field existed. Can't determine
                    status. Downstream UI should badge as unknown / legacy.
* ``"unknown"``  -- Source kind is not one of the known enum values (defensive).
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Optional

import slicer

if TYPE_CHECKING:
    import openlifu.db.session


# Status constants; keep as strings so callers can use them directly in text UI.
STATUS_LIVE = "live"
STATUS_REVOKED = "revoked"
STATUS_MISSING = "missing"
STATUS_LEGACY = "legacy"
STATUS_UNKNOWN = "unknown"


def get_solution_source_status(
    solution_info: "openlifu.db.session.SolutionInfo",
    session_id: Optional[str],
) -> str:
    """Return one of the ``STATUS_*`` constants describing whether the solution's source
    (VF result or TT result) is still live in the current scene.

    ``session_id`` is the id of the loaded session (used to scope VF / TT scene lookups
    to results belonging to this session); pass ``None`` for the "no session, standalone
    workflow" case, in which case only orphan VF / TT nodes (no ``VF:sessionID`` /
    ``TT:sessionID`` attribute) are consulted.
    """
    source_id = solution_info.transducer_transform_source_id
    if source_id is None:
        # Legacy: solution was computed before ``transducer_transform_source_id`` existed.
        return STATUS_LEGACY

    kind = solution_info.transducer_transform_source
    if kind == "virtual_fit":
        return _vf_source_status(source_id, session_id)
    if kind == "localization":
        return _tt_source_status(source_id, session_id)
    return STATUS_UNKNOWN


def _vf_source_status(source_id: str, session_id: Optional[str]) -> str:
    """Resolve VF composite key ``"<target_id>:<rank>"`` against current scene state."""
    # Parse composite key.
    if ":" not in source_id:
        return STATUS_UNKNOWN
    target_id, rank_str = source_id.rsplit(":", 1)
    if not rank_str.isdigit():
        return STATUS_UNKNOWN

    # Delegate the scene lookup. Local import to avoid an import cycle at module load
    # time (this module lives in OpenLIFULib and virtual_fit_results is in the same
    # package -- direct import at the top would be fine, but keeping it lazy matches
    # how other resolvers here are structured).
    from OpenLIFULib.virtual_fit_results import get_virtual_fit_result_nodes

    # NB: ``get_virtual_fit_result_nodes`` compares ``rank`` to the raw string attribute
    # ``VF:rank`` (no type coercion) but its ``if rank:`` guard treats rank=0 as no-filter.
    # Filter manually to sidestep both quirks.
    all_for_target = list(get_virtual_fit_result_nodes(
        session_id=session_id, target_id=target_id,
    ))
    if session_id is None:
        all_for_target = [n for n in all_for_target if n.GetAttribute("VF:sessionID") is None]
    nodes = [n for n in all_for_target if n.GetAttribute("VF:rank") == rank_str]
    if not nodes:
        return STATUS_MISSING
    # Multiple matches shouldn't happen (session x target x rank should be unique), but
    # if it does, treat "any approved" as approved.
    if any(n.GetAttribute("VF:approvalStatus") == "1" for n in nodes):
        return STATUS_LIVE
    return STATUS_REVOKED


def _tt_source_status(source_id: str, session_id: Optional[str]) -> str:
    """Resolve TT stable result-id against current scene state."""
    from OpenLIFULib.transducer_tracking_results import (
        get_transducer_tracking_result_by_id,
    )

    node = get_transducer_tracking_result_by_id(source_id, session_id)
    if node is None:
        return STATUS_MISSING
    if node.GetAttribute("TT:approvalStatus") == "1":
        return STATUS_LIVE
    return STATUS_REVOKED


# ---------------------------------------------------------------------------
# Presentation helpers -- kept here so status text / tooltip / color are
# consistent across every consumer (Solutions table, Sonication Control
# send-to-hardware guard, planner "Delete solutions with revoked source"
# button label).
# ---------------------------------------------------------------------------

_STATUS_LABEL = {
    STATUS_LIVE:    "Live",
    STATUS_REVOKED: "Revoked",
    STATUS_MISSING: "Missing",
    STATUS_LEGACY:  "Legacy",
    STATUS_UNKNOWN: "Unknown",
}

_STATUS_TOOLTIP = {
    STATUS_LIVE:    "The VF / TT result this solution was computed against is still present and approved.",
    STATUS_REVOKED: "The VF / TT result this solution was computed against is no longer approved. The stored pose is still valid, but you should re-review before sending to hardware.",
    STATUS_MISSING: "The VF / TT result this solution was computed against has been removed from the session. The stored pose is still valid, but there is no live reference to check against.",
    STATUS_LEGACY:  "Solution was computed before source tracking was recorded; source liveness cannot be determined.",
    STATUS_UNKNOWN: "Unrecognized source; treat as needing manual re-review before sending to hardware.",
}


def label_for_status(status: str) -> str:
    return _STATUS_LABEL.get(status, "Unknown")


def tooltip_for_status(status: str) -> str:
    return _STATUS_TOOLTIP.get(status, _STATUS_TOOLTIP[STATUS_UNKNOWN])


def is_safe_to_send_to_hardware(status: str) -> bool:
    """Whether a solution with this source status is OK to send to hardware without a hard warning.

    Currently only ``"live"`` is considered fully safe. ``"legacy"`` is a soft-yes -- the
    solution is old enough that we can't know either way, so the current convention is to
    require the user to explicitly acknowledge (Sonication Control does this via a warn
    dialog). ``"revoked"`` and ``"missing"`` are hard-no.
    """
    return status == STATUS_LIVE


def get_active_solution_source_status() -> str:
    """Convenience: return the source-liveness status of the currently active solution.

    Returns ``"unknown"`` when there is no loaded session, no active solution, or no
    matching :class:`openlifu.db.session.SolutionInfo` entry for the active id (which
    means either a session-solution index mismatch or a solution that was loaded via a
    non-standard path). Downstream callers should treat ``"unknown"`` the same as they
    treat ``"missing"`` -- i.e. require explicit user acknowledgement before sending to
    hardware.
    """
    from OpenLIFULib.util import get_active_solution, get_app_state
    active = get_active_solution()
    if active is None:
        return STATUS_UNKNOWN
    loaded_session = get_app_state().loaded_session
    if loaded_session is None:
        return STATUS_UNKNOWN
    active_id = active.solution.solution.id
    for info in loaded_session.session.session.solutions:
        if info.solution_id == active_id:
            return get_solution_source_status(info, loaded_session.get_session_id())
    return STATUS_UNKNOWN
