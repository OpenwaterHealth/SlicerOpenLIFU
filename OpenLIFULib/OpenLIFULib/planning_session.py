"""SlicerOpenLIFUPlanningSession parameter pack.

A loaded :class:`openlifu.db.PlanningSession` with its associated Slicer scene
data: the volume being planned against, and the fiducial markup nodes
representing the working target(s).

Does NOT carry photoscans (those belong to a
:class:`~OpenLIFULib.sonication_session.SlicerOpenLIFUSonicationSession`) and
does NOT carry solutions on the pack itself (subject-scoped solutions are
loaded via the app state's ``loaded_solutions`` map, filtered by the
PlanningSession's ``pre_solutions`` list of
:class:`~openlifu.db.session.SolutionInfo` refs).

See ``SESSION_SPLIT_DESIGN.md`` for the full design.
"""

from typing import TYPE_CHECKING, List, Optional

import slicer
from slicer import vtkMRMLMarkupsFiducialNode, vtkMRMLScalarVolumeNode
from slicer.parameterNodeWrapper import parameterPack

from OpenLIFULib.parameter_node_utils import SlicerOpenLIFUPlanningSessionWrapper

if TYPE_CHECKING:
    import openlifu.db


@parameterPack
class SlicerOpenLIFUPlanningSession:
    """A loaded, mutable :class:`openlifu.db.PlanningSession` bound to its
    Slicer scene representation."""

    session: SlicerOpenLIFUPlanningSessionWrapper
    """The wrapped openlifu PlanningSession object."""

    volume_node: vtkMRMLScalarVolumeNode
    """The scene volume being planned against. Owned by this PlanningSession
    for lifecycle purposes (unloaded when the session is unloaded)."""

    target_nodes: List[vtkMRMLMarkupsFiducialNode]
    """The list of target fiducial nodes loaded from this PlanningSession's
    ``targets``. Kept here so they can be unloaded when the session is
    unloaded; SlicerOpenLIFU treats every fiducial in the scene as a candidate
    target, not just the ones listed here."""

    def get_planning_session_id(self) -> str:
        """Return the underlying openlifu PlanningSession id."""
        return self.session.planning_session.id

    def get_subject_id(self) -> Optional[str]:
        """Return the subject id."""
        return self.session.planning_session.subject_id

    def get_volume_id(self) -> Optional[str]:
        """Return the volume id.

        Read from the openlifu PlanningSession field rather than the pack's
        ``volume_node.GetAttribute(...)``: the parameterPack's MRML-node
        reference can go stale during teardown and would otherwise crash
        observers.
        """
        return self.session.planning_session.volume_id

    def get_protocol_id(self) -> Optional[str]:
        """Return the protocol id."""
        return self.session.planning_session.protocol_id

    def get_transducer_id(self) -> Optional[str]:
        """Return the transducer id."""
        return self.session.planning_session.transducer_id

    def get_finalized_plan_ids(self) -> List[str]:
        """Return the list of Plan ids this PlanningSession has finalized.

        The primary flow is one PlanningSession -> one Plan, but multiple
        Plans per PlanningSession are supported (each Finalize action
        produces a new immutable Plan).
        """
        return list(self.session.planning_session.finalized_plan_ids)

    def get_target_nodes(self) -> List[vtkMRMLMarkupsFiducialNode]:
        """Return the session's target fiducial nodes, filtering out stale entries.

        The ``target_nodes`` field stores each fiducial by MRML ID; when a node
        is removed from the scene, the stored ID goes stale and the entry
        dereferences to ``None``. Every caller that iterates targets should go
        through this accessor so observer cascades that fire during scene
        teardown do not see the ``None`` placeholders.
        """
        return [n for n in self.target_nodes if n is not None]

    def volume_is_valid(self) -> bool:
        """Return whether this session's volume is present in the scene."""
        return (
            self.volume_node is not None
            and slicer.mrmlScene.GetNodeByID(self.volume_node.GetID()) is not None
        )
