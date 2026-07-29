from __future__ import annotations

from typing import Dict, Optional

import qt
import slicer
import vtk
from slicer.parameterNodeWrapper import parameterNodeWrapper

from OpenLIFULib import (
    SlicerOpenLIFUPhotoscan,
    SlicerOpenLIFUPlan,
    SlicerOpenLIFUPlanningSession,
    SlicerOpenLIFUProtocol,
    SlicerOpenLIFURun,
    SlicerOpenLIFUSession,
    SlicerOpenLIFUSolution,
    SlicerOpenLIFUSonicationSession,
    SlicerOpenLIFUTransducer,
)


@parameterNodeWrapper
class OpenLIFUAppState:
    """Consolidated parameter node for the OpenLIFU host module.

    Owns the ``loaded_*`` fields formerly declared on
    ``OpenLIFUDataParameterNode`` (Round 4b of DEMODULING.md). As of
    Round 5b the underlying MRML singleton is the OpenLIFU host module's
    own parameter node; ``get_app_state()`` and
    ``OpenLIFUDataLogic.getParameterNode()`` both return an
    ``OpenLIFUAppState`` wrapper around it.
    """
    loaded_protocols : "Dict[str,SlicerOpenLIFUProtocol]"
    loaded_transducers : "Dict[str,SlicerOpenLIFUTransducer]"
    # Multi-solution support (SlicerOpenLIFU#611): the app can hold any number of loaded solutions,
    # keyed by their openlifu ``Solution.id``. ``active_solution_id`` names the one that is currently
    # ``get_active_solution()``’s target (analysis panel, PNP MIP, transducer-pose selection, etc.).
    # An empty ``active_solution_id`` (the default) means “no active solution” and is treated by
    # ``get_active_solution()`` as ``None``.
    loaded_solutions : "Dict[str,SlicerOpenLIFUSolution]"
    active_solution_id : str
    loaded_session : "Optional[SlicerOpenLIFUSession]"
    """Legacy omnibus session. Retained during the split-session refactor
    (SlicerOpenLIFU#631) so pages that have not yet been rewritten can keep
    functioning; will be removed once every page moves to
    ``loaded_planning_session`` or ``loaded_sonication_session``."""

    loaded_planning_session : "Optional[SlicerOpenLIFUPlanningSession]"
    """The currently-loaded PlanningSession, or ``None``. Mutually exclusive
    with ``loaded_sonication_session`` in the intended workflow (the Data
    Manager loads one at a time), though the app state does not enforce that.
    See ``SESSION_SPLIT_DESIGN.md``."""

    loaded_sonication_session : "Optional[SlicerOpenLIFUSonicationSession]"
    """The currently-loaded SonicationSession, or ``None``. Carries a frozen
    reference to a :class:`SlicerOpenLIFUPlan` for the target / volume /
    protocol / transducer / array_transform context. See
    ``SESSION_SPLIT_DESIGN.md``."""

    loaded_run: "Optional[SlicerOpenLIFURun]"
    loaded_photoscans: "Dict[str,SlicerOpenLIFUPhotoscan]"

    # Set True whenever an in-memory session mutation happens that has NOT been written
    # to ``{session_dir}/{session_id}.json`` on disk. Cleared by :meth:`OpenLIFUDataLogic.save_session`.
    # Consulted by :meth:`clear_session` to prompt the user to save (or discard) unsaved
    # changes before unloading. Solutions and photoscans have their own on-disk artifacts
    # written eagerly by ``set_solution`` / ``write_photoscan`` and are not tracked here;
    # this flag reflects only the state that lives in the session's own JSON.
    session_is_dirty : bool = False


class AppStateSignals(qt.QObject):
    """Qt-signal facade over :class:`OpenLIFUAppState`.

    A single VTK ``ModifiedEvent`` observer sits on the AppState's MRML
    singleton and re-emits every fire as ``dataChanged``. Consumers
    connect Qt slots to ``dataChanged`` instead of adding their own VTK
    observers, so parameter-node writes propagate through the normal
    Qt signal-slot machinery (auto-cleanup on receiver destruction,
    no direct MRML-node coupling).
    """
    dataChanged = qt.Signal()


_app_state_signals: Optional[AppStateSignals] = None
_app_state_vtk_observer_tag: Optional[int] = None
_app_state_observed_node = None  # kept alive for observer removal


def get_app_state_signals() -> AppStateSignals:
    """Return the process-wide :class:`AppStateSignals` singleton.

    On first call, installs a VTK observer on the AppState's underlying
    MRML node that re-emits every ``ModifiedEvent`` as
    ``AppStateSignals.dataChanged``. Safe to call from any module's
    ``setup()``; the host module's parameter node is created lazily by
    Slicer on first ``getParameterNode()`` and lives for the app's
    lifetime.
    """
    global _app_state_signals, _app_state_vtk_observer_tag, _app_state_observed_node
    if _app_state_signals is not None:
        return _app_state_signals
    _app_state_signals = AppStateSignals()
    host_logic = slicer.util.getModuleLogic("OpenLIFU")
    mrml_node = host_logic.getParameterNode().parameterNode
    _app_state_observed_node = mrml_node
    _app_state_vtk_observer_tag = mrml_node.AddObserver(
        vtk.vtkCommand.ModifiedEvent,
        lambda *_: _app_state_signals.dataChanged.emit(),
    )
    return _app_state_signals

