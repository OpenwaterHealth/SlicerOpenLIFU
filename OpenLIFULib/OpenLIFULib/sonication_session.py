"""SlicerOpenLIFUSonicationSession parameter pack.

A loaded :class:`openlifu.db.SonicationSession` with its associated Slicer
scene data. References a
:class:`~OpenLIFULib.plan.SlicerOpenLIFUPlan` by id (frozen input); the plan
provides target + volume + protocol + transducer + array_transform.

Does NOT carry a ``target_nodes`` field (target comes from the Plan) and does
NOT carry photoscans on the pack itself (photoscans live in the app state's
``loaded_photoscans`` map, filtered by the SonicationSession's
``photoscan_ids`` list). The final Solution is a subject-scoped artifact
loaded through the app state's ``loaded_solutions`` map, referenced by the
SonicationSession's ``solution: Optional[SolutionInfo]`` field.

See ``SESSION_SPLIT_DESIGN.md`` for the full design.
"""

from typing import TYPE_CHECKING, Optional

import slicer
from slicer import vtkMRMLScalarVolumeNode
from slicer.parameterNodeWrapper import parameterPack

from OpenLIFULib.parameter_node_utils import (
    SlicerOpenLIFUPlanWrapper,
    SlicerOpenLIFUSonicationSessionWrapper,
)

if TYPE_CHECKING:
    import openlifu.db


@parameterPack
class SlicerOpenLIFUSonicationSession:
    """A loaded, mutable :class:`openlifu.db.SonicationSession` bound to its
    Slicer scene representation.

    The referenced :class:`openlifu.db.Plan` is embedded as a frozen wrapper
    so consumers can read plan-derived context (target, array_transform,
    volume_id, protocol_id, transducer_id) without an extra database
    round-trip. The plan is treated as read-only.
    """

    session: SlicerOpenLIFUSonicationSessionWrapper
    """The wrapped openlifu SonicationSession object."""

    plan: SlicerOpenLIFUPlanWrapper
    """Frozen reference to the Plan that this SonicationSession is executing.
    Immutable; treat as read-only. The plan's target, volume_id, protocol_id,
    transducer_id, and array_transform are the "what we're trying to hit"
    reference for this SonicationSession."""

    volume_node: vtkMRMLScalarVolumeNode
    """The scene volume (loaded from the Plan's ``volume_id``). The
    SonicationSession itself does not store a volume_id; volume identity comes
    from the plan."""

    def get_sonication_session_id(self) -> str:
        """Return the underlying openlifu SonicationSession id."""
        return self.session.sonication_session.id

    def get_subject_id(self) -> Optional[str]:
        """Return the subject id."""
        return self.session.sonication_session.subject_id

    def get_plan_id(self) -> Optional[str]:
        """Return the id of the Plan this SonicationSession is executing."""
        return self.session.sonication_session.plan_id

    def get_volume_id(self) -> Optional[str]:
        """Return the volume id.

        Comes from the referenced Plan; the SonicationSession itself does not
        carry a volume_id field.
        """
        return self.plan.plan.volume_id if self.plan.plan is not None else None

    def get_protocol_id(self) -> Optional[str]:
        """Return the protocol id (from the Plan)."""
        return self.plan.plan.protocol_id if self.plan.plan is not None else None

    def get_transducer_id(self) -> Optional[str]:
        """Return the transducer id (from the Plan)."""
        return self.plan.plan.transducer_id if self.plan.plan is not None else None

    def get_solution_info(self) -> "Optional[openlifu.db.session.SolutionInfo]":
        """Return the :class:`SolutionInfo` reference for the final Solution
        computed on this SonicationSession, or ``None`` if none has been
        computed yet.

        The actual Solution binary artifacts (``.nc`` + analysis) live at
        subject scope and are loaded separately via the app state's
        ``loaded_solutions`` map.
        """
        return self.session.sonication_session.solution

    def volume_is_valid(self) -> bool:
        """Return whether this session's volume is present in the scene."""
        return (
            self.volume_node is not None
            and slicer.mrmlScene.GetNodeByID(self.volume_node.GetID()) is not None
        )
