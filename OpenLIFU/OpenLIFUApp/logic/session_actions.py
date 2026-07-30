"""Session lifecycle actions shared across Home and Data Manager.

**Memory is the source of truth for the loaded session** (see
SlicerOpenLIFU#636). Every action here reflects that:

* :func:`build_planning_session` / :func:`build_sonication_session`
  are pure factories -- they return in-memory openlifu objects and
  never touch disk.
* :func:`open_planning_session_into_app` /
  :func:`open_sonication_session_into_app` install an already-in-memory
  openlifu object as the loaded session and materialise its scene
  nodes (volume, target fiducials).
* :func:`open_planning_session_from_disk` /
  :func:`open_sonication_session_from_disk` load an existing session
  off disk and delegate to the ``_into_app`` functions.
* :func:`save_loaded_session` is the SOLE code path that writes the
  loaded session's JSON to disk. It also clears the app state's
  ``session_is_dirty`` flag.
* :func:`close_loaded_sessions` tears down scene nodes and nulls the
  loaded-session fields. It NEVER writes -- discarding is the whole
  point.
* :func:`prompt_save_before_replacing_loaded_session` is the guard
  every "open a new session" action should call first, so unsaved
  work isn't silently clobbered.

Home's launch buttons (SlicerOpenLIFU#635) and Data Manager's
per-row controls both call these module-level functions; neither
page reaches into the other page's ``Logic`` class (see
``docs/coding-standards.md`` rule 6).

Everything here operates directly on the currently-loaded database
(via :func:`get_cur_db`) and on the app state (via
:func:`get_app_state`). Callers do not have to thread references
through each call.

Not a home for the admin-only actions (delete PlanningSession /
Plan / SonicationSession / Solution). Those live on
``OpenLIFUDataManagerLogic`` because deletion is a Data-Manager-only
capability and there's no reuse to justify moving them here.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Optional

import slicer

from OpenLIFULib import (
    SlicerOpenLIFUPlanningSession,
    SlicerOpenLIFUSonicationSession,
    assign_openlifu_metadata_to_volume_node,
    get_app_state,
    get_cur_db,
)
from OpenLIFULib.guided_mode_util import confirm_exit_session_dialog
from OpenLIFULib.parameter_node_utils import (
    SlicerOpenLIFUPlanningSessionWrapper,
    SlicerOpenLIFUPlanWrapper,
    SlicerOpenLIFUSonicationSessionWrapper,
)

if TYPE_CHECKING:
    import openlifu.db
    import openlifu.geo


# ---------------------------------------------------------------------------
# Database helpers
# ---------------------------------------------------------------------------

def require_current_database() -> "openlifu.db.Database":
    """Return the currently-loaded database or raise ``RuntimeError``.

    Prefer this over :func:`get_cur_db` in call sites that cannot
    proceed without a database -- the raise gives every button handler
    a clean error path without an ``if database is None`` prelude.
    """
    database = get_cur_db()
    if database is None:
        raise RuntimeError("No database is currently loaded.")
    return database


def load_volume_node_from_database(
    database, subject_id: str, volume_id: str,
):
    """Load a subject volume into the Slicer scene as a scalar volume node.

    Reuses the DB metadata + NIfTI loader that legacy pages relied on;
    scoped to the minimum viable behaviour for split-session pages.
    """
    info = database.get_volume_info(subject_id, volume_id)
    volume_node = slicer.util.loadVolume(str(info["data_abspath"]))
    assign_openlifu_metadata_to_volume_node(volume_node, info)
    return volume_node


def create_target_fiducial_node(target: "openlifu.geo.Point"):
    """Create a fiducial node for the given openlifu Point target.

    Uses the shared conversion helper so downstream pages that read
    fiducials back as Points get consistent metadata (id, dims, units).
    """
    from OpenLIFULib.targets import openlifu_point_to_fiducial
    return openlifu_point_to_fiducial(target)


# ---------------------------------------------------------------------------
# In-memory factories (no disk write)
# ---------------------------------------------------------------------------

def build_planning_session(
    *,
    subject_id: str,
    planning_session_id: str,
    name: Optional[str],
    volume_id: str,
    protocol_id: str,
    transducer_id: str,
) -> "openlifu.db.PlanningSession":
    """Build a fresh :class:`openlifu.db.PlanningSession` in memory.

    **Does not touch the database.** The returned object is a plain
    in-memory openlifu dataclass; the caller is expected to hand it
    to :func:`open_planning_session_into_app` and call
    :func:`OpenLIFULib.util.mark_session_dirty` so that a subsequent
    :func:`save_loaded_session` writes it to disk. If the user exits
    without saving, the object is dropped -- disk stays untouched.

    Split off from the legacy ``create_planning_session`` per
    SlicerOpenLIFU#636 ("memory is the source of truth").
    """
    import openlifu.db
    return openlifu.db.PlanningSession(
        id=planning_session_id,
        name=name or planning_session_id,
        subject_id=subject_id,
        volume_id=volume_id,
        protocol_id=protocol_id,
        transducer_id=transducer_id,
    )


def build_sonication_session(
    *,
    subject_id: str,
    sonication_session_id: str,
    name: Optional[str],
    plan_id: str,
) -> "openlifu.db.SonicationSession":
    """Build a fresh :class:`openlifu.db.SonicationSession` in memory.

    Validates that ``plan_id`` already exists on disk for this subject
    -- Plans are always disk-first (they're produced by
    ``plan_finalization.finalize_plan``), so a SonicationSession that
    referenced an in-memory-only plan would be uncomfortably dangling.

    Raises ``ValueError`` if the plan does not exist for the subject.
    Does NOT write the new SonicationSession itself to disk -- see
    module docstring.
    """
    import openlifu.db
    database = require_current_database()
    if plan_id not in database.get_plan_ids(subject_id):
        raise ValueError(
            f"Plan {plan_id!r} does not exist for subject {subject_id!r}."
        )
    return openlifu.db.SonicationSession(
        id=sonication_session_id,
        name=name or sonication_session_id,
        subject_id=subject_id,
        plan_id=plan_id,
    )


# ---------------------------------------------------------------------------
# Open into app state (in-memory object → loaded session)
# ---------------------------------------------------------------------------

def open_planning_session_into_app(
    planning_session: "openlifu.db.PlanningSession",
) -> None:
    """Install an in-memory :class:`openlifu.db.PlanningSession` as the
    loaded session.

    Closes any currently-loaded planning or sonication session first
    so the app remains in a single-loaded-session state. Loads the
    session's volume + target fiducials into the Slicer scene. Does
    NOT touch disk. Does NOT mark the app state dirty -- the caller
    decides:

    * Freshly-built via :func:`build_planning_session`: mark dirty
      so Save actually persists it.
    * Just-loaded from disk via :func:`open_planning_session_from_disk`:
      do NOT mark dirty; disk already has this exact copy.
    """
    database = require_current_database()
    close_loaded_sessions()

    volume_node = load_volume_node_from_database(
        database, planning_session.subject_id, planning_session.volume_id,
    )
    target_nodes = [
        create_target_fiducial_node(target)
        for target in planning_session.targets
    ]

    state = get_app_state()
    state.loaded_planning_session = SlicerOpenLIFUPlanningSession(
        session=SlicerOpenLIFUPlanningSessionWrapper(planning_session=planning_session),
        volume_node=volume_node,
        target_nodes=target_nodes,
    )


def open_sonication_session_into_app(
    sonication_session: "openlifu.db.SonicationSession",
) -> None:
    """Install an in-memory :class:`openlifu.db.SonicationSession` as the
    loaded session.

    Loads the referenced Plan (Plans are always disk-first) plus the
    Plan's volume into the Slicer scene. Closes any currently-loaded
    session first. Does NOT touch disk for the SonicationSession
    itself. Does NOT mark the app state dirty -- caller's choice.

    Raises ``ValueError`` if the SonicationSession has no ``plan_id``.
    """
    database = require_current_database()
    if sonication_session.plan_id is None:
        raise ValueError(
            f"SonicationSession {sonication_session.id!r} has no plan_id."
        )
    plan = database.load_plan(
        sonication_session.subject_id, sonication_session.plan_id,
    )
    close_loaded_sessions()

    volume_node = load_volume_node_from_database(
        database, sonication_session.subject_id, plan.volume_id,
    )

    state = get_app_state()
    state.loaded_sonication_session = SlicerOpenLIFUSonicationSession(
        session=SlicerOpenLIFUSonicationSessionWrapper(sonication_session=sonication_session),
        plan=SlicerOpenLIFUPlanWrapper(plan=plan),
        volume_node=volume_node,
    )


# ---------------------------------------------------------------------------
# Load from disk
# ---------------------------------------------------------------------------

def open_planning_session_from_disk(
    subject_id: str, planning_session_id: str,
) -> None:
    """Read a PlanningSession off disk and install it as the loaded session.

    Thin wrapper: reads the openlifu object, then hands off to
    :func:`open_planning_session_into_app`. Does NOT mark the state
    dirty -- disk is the source of truth for this path.
    """
    database = require_current_database()
    planning_session = database.load_planning_session(
        subject_id, planning_session_id,
    )
    open_planning_session_into_app(planning_session)
    logging.info(
        "Loaded PlanningSession %s for subject %s from disk",
        planning_session_id, subject_id,
    )


def open_sonication_session_from_disk(
    subject_id: str, sonication_session_id: str,
) -> None:
    """Read a SonicationSession off disk and install it as the loaded session.

    Thin wrapper: reads the openlifu object, then hands off to
    :func:`open_sonication_session_into_app`. Does NOT mark dirty.
    """
    database = require_current_database()
    sonication_session = database.load_sonication_session(
        subject_id, sonication_session_id,
    )
    open_sonication_session_into_app(sonication_session)
    logging.info(
        "Loaded SonicationSession %s for subject %s from disk",
        sonication_session_id, subject_id,
    )


# ---------------------------------------------------------------------------
# Save / close
# ---------------------------------------------------------------------------

def save_loaded_session() -> None:
    """Persist the currently-loaded PlanningSession / SonicationSession
    JSON(s) to disk and clear the ``session_is_dirty`` flag.

    **Sole code path that writes the loaded session's JSON to disk.**
    This is what makes memory-first sessions materialise on disk.

    Raises ``RuntimeError`` if nothing is loaded.
    """
    database = require_current_database()
    state = get_app_state()
    planning_session = state.loaded_planning_session
    sonication_session = state.loaded_sonication_session
    wrote_something = False
    if planning_session is not None:
        database.write_planning_session(
            planning_session.get_subject_id(),
            planning_session.session.planning_session,
            on_conflict="overwrite",
        )
        wrote_something = True
    if sonication_session is not None:
        database.write_sonication_session(
            sonication_session.get_subject_id(),
            sonication_session.session.sonication_session,
            on_conflict="overwrite",
        )
        wrote_something = True
    if not wrote_something:
        raise RuntimeError("Nothing loaded to save.")
    # Successful save leaves memory and disk in sync -- clear the
    # dirty flag so the exit-guard doesn't re-prompt to save.
    state.session_is_dirty = False


def close_loaded_sessions() -> None:
    """Unload the loaded PlanningSession / SonicationSession from the app state.

    Tears down scene nodes each session owned (volume, targets).
    **Never writes to disk.** This is the "Discard" primitive: an
    in-memory-only session that never got saved simply ceases to
    exist when this runs.

    The app state never nulls a session on its own; this function is
    the single point at which unload happens.

    Also clears ``session_is_dirty`` because there is no longer any
    dirty session to track.
    """
    state = get_app_state()
    planning_session = state.loaded_planning_session
    if planning_session is not None:
        for node in planning_session.get_target_nodes():
            try:
                slicer.mrmlScene.RemoveNode(node)
            except Exception:  # noqa: BLE001
                pass
        if planning_session.volume_node is not None:
            try:
                slicer.mrmlScene.RemoveNode(planning_session.volume_node)
            except Exception:  # noqa: BLE001
                pass
        state.loaded_planning_session = None
    sonication_session = state.loaded_sonication_session
    if sonication_session is not None:
        if sonication_session.volume_node is not None:
            try:
                slicer.mrmlScene.RemoveNode(sonication_session.volume_node)
            except Exception:  # noqa: BLE001
                pass
        state.loaded_sonication_session = None
    state.session_is_dirty = False


# ---------------------------------------------------------------------------
# Guard against silently clobbering unsaved work
# ---------------------------------------------------------------------------

def prompt_save_before_replacing_loaded_session() -> bool:
    """Prompt save / discard / cancel before an action that would
    clobber an unsaved loaded session.

    Returns ``True`` if the caller should proceed:

    * Nothing is loaded, OR
    * The loaded session has no unsaved changes, OR
    * The user chose Save (and it succeeded) or Discard.

    Returns ``False`` if the user cancelled -- caller should abort
    without touching state.

    Called by every "open a new session" action: Home's
    ``on_new_*_button_clicked`` and ``on_continue_*_button_clicked``
    handlers, Data Manager's equivalents, and any other flow that
    would otherwise silently drop an unsaved dirty session via
    :func:`close_loaded_sessions` inside
    :func:`open_planning_session_into_app` /
    :func:`open_sonication_session_into_app`.
    """
    state = get_app_state()
    if (
        state.loaded_planning_session is None
        and state.loaded_sonication_session is None
    ):
        return True
    if not state.session_is_dirty:
        return True
    choice = confirm_exit_session_dialog()
    if choice == "cancel":
        return False
    if choice == "save":
        try:
            save_loaded_session()
        except RuntimeError:
            # Nothing to save -- shouldn't happen given the checks
            # above, but treat as a discard rather than raising.
            pass
    return True


# ---------------------------------------------------------------------------
# Navigation
# ---------------------------------------------------------------------------

def navigate_to_host_page(module_name: str) -> None:
    """Ask the OpenLIFU host module to swap the visible page.

    Thin wrapper so page-level code does not have to know how to fish
    the host widget out of Slicer's module registry. Silent no-op if
    the host is not yet available (extremely early in Slicer startup).

    Kept in this module because both Home and Data Manager -- the two
    pages that host session-lifecycle actions -- need to navigate to
    an overview page after a successful load.
    """
    try:
        host_widget = slicer.util.getModule("OpenLIFU").widgetRepresentation().self()
        host_widget.show_page(module_name)
    except Exception:  # noqa: BLE001
        logging.exception("session_actions: unable to navigate to %s.", module_name)
