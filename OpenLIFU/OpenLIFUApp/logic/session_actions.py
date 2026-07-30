"""Session lifecycle actions shared across Home and Data Manager.

Owns the create / load / save / close operations for
:class:`openlifu.db.PlanningSession` and
:class:`openlifu.db.SonicationSession`. Home's launch buttons
(SlicerOpenLIFU#635) and Data Manager's per-row controls both call
these module-level functions; neither page reaches into the other
page's ``Logic`` class (see ``docs/coding-standards.md`` rule 6).

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
# Creation
# ---------------------------------------------------------------------------

def create_planning_session(
    *,
    subject_id: str,
    planning_session_id: str,
    name: Optional[str],
    volume_id: str,
    protocol_id: str,
    transducer_id: str,
) -> None:
    """Create and write a new (empty) PlanningSession to the database.

    Does NOT load the new session into app state; the caller is
    expected to follow up with :func:`load_planning_session_into_app`
    if they want to open it in the UI.
    """
    import openlifu.db
    database = require_current_database()
    session = openlifu.db.PlanningSession(
        id=planning_session_id,
        name=name or planning_session_id,
        subject_id=subject_id,
        volume_id=volume_id,
        protocol_id=protocol_id,
        transducer_id=transducer_id,
    )
    database.write_planning_session(subject_id, session)
    logging.info(
        "Created PlanningSession %s for subject %s",
        planning_session_id, subject_id,
    )


def create_sonication_session(
    *,
    subject_id: str,
    sonication_session_id: str,
    name: Optional[str],
    plan_id: str,
) -> None:
    """Create and write a new (empty) SonicationSession against a Plan.

    Raises ``ValueError`` if ``plan_id`` does not exist for the given
    subject.
    """
    import openlifu.db
    database = require_current_database()
    if plan_id not in database.get_plan_ids(subject_id):
        raise ValueError(
            f"Plan {plan_id!r} does not exist for subject {subject_id!r}."
        )
    session = openlifu.db.SonicationSession(
        id=sonication_session_id,
        name=name or sonication_session_id,
        subject_id=subject_id,
        plan_id=plan_id,
    )
    database.write_sonication_session(subject_id, session)
    logging.info(
        "Created SonicationSession %s for subject %s (plan=%s)",
        sonication_session_id, subject_id, plan_id,
    )


# ---------------------------------------------------------------------------
# Loading
# ---------------------------------------------------------------------------

def load_planning_session_into_app(
    subject_id: str, planning_session_id: str,
) -> None:
    """Load a PlanningSession into the app state.

    Unloads any currently-loaded planning or sonication session first
    so the app is in a single-loaded-session state.
    """
    database = require_current_database()
    planning_session = database.load_planning_session(
        subject_id, planning_session_id,
    )
    close_loaded_sessions()

    volume_node = load_volume_node_from_database(
        database, subject_id, planning_session.volume_id,
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
    logging.info(
        "Loaded PlanningSession %s for subject %s",
        planning_session_id, subject_id,
    )


def load_sonication_session_into_app(
    subject_id: str, sonication_session_id: str,
) -> None:
    """Load a SonicationSession + its referenced Plan into the app state.

    Raises ``ValueError`` if the SonicationSession has no ``plan_id``.
    """
    database = require_current_database()
    sonication_session = database.load_sonication_session(
        subject_id, sonication_session_id,
    )
    if sonication_session.plan_id is None:
        raise ValueError(
            f"SonicationSession {sonication_session_id!r} has no plan_id."
        )
    plan = database.load_plan(subject_id, sonication_session.plan_id)
    close_loaded_sessions()

    volume_node = load_volume_node_from_database(
        database, subject_id, plan.volume_id,
    )

    state = get_app_state()
    state.loaded_sonication_session = SlicerOpenLIFUSonicationSession(
        session=SlicerOpenLIFUSonicationSessionWrapper(sonication_session=sonication_session),
        plan=SlicerOpenLIFUPlanWrapper(plan=plan),
        volume_node=volume_node,
    )
    logging.info(
        "Loaded SonicationSession %s for subject %s (plan=%s)",
        sonication_session_id, subject_id, plan.id,
    )


# ---------------------------------------------------------------------------
# Save / close
# ---------------------------------------------------------------------------

def save_loaded_session() -> None:
    """Persist the currently-loaded PlanningSession / SonicationSession
    JSON(s) to disk.

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


def close_loaded_sessions() -> None:
    """Unload the loaded PlanningSession / SonicationSession from the app state.

    Tears down scene nodes each session owned (volume, targets). The
    app state never nulls a session on its own; this function is the
    single point at which unload happens.
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
