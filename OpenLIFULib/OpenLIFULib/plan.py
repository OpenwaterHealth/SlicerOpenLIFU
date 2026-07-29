"""SlicerOpenLIFUPlan parameter pack.

Immutable finalized treatment plan produced by a
:class:`~OpenLIFULib.planning_session.SlicerOpenLIFUPlanningSession`. Wraps an
:class:`openlifu.db.Plan` and can be persisted in a Slicer parameter node via
:class:`~OpenLIFULib.parameter_node_utils.SlicerOpenLIFUPlanWrapper`.

Scene fields are deliberately minimal: a Plan is a snapshot artifact used
either as a "read-only" preview on the Planning Session Overview page or as
the frozen input to a :class:`~OpenLIFULib.sonication_session.SlicerOpenLIFUSonicationSession`.
Larger scene state (volume, transducer) is loaded separately as needed.

See ``SESSION_SPLIT_DESIGN.md`` for the full design.
"""

from typing import TYPE_CHECKING, Optional

from slicer.parameterNodeWrapper import parameterPack

from OpenLIFULib.parameter_node_utils import SlicerOpenLIFUPlanWrapper

if TYPE_CHECKING:
    import openlifu.db


@parameterPack
class SlicerOpenLIFUPlan:
    """A loaded, immutable :class:`openlifu.db.Plan`.

    The Plan itself is frozen once written; this pack exists so a loaded Plan
    can round-trip through a Slicer parameter node.
    """

    plan: SlicerOpenLIFUPlanWrapper

    def get_plan_id(self) -> str:
        """Return the underlying openlifu Plan id."""
        return self.plan.plan.id

    def get_subject_id(self) -> Optional[str]:
        """Return the subject id the plan was finalized against."""
        return self.plan.plan.subject_id

    def get_volume_id(self) -> Optional[str]:
        """Return the volume id the plan was finalized against."""
        return self.plan.plan.volume_id

    def get_protocol_id(self) -> Optional[str]:
        """Return the protocol id the plan was finalized against."""
        return self.plan.plan.protocol_id

    def get_transducer_id(self) -> Optional[str]:
        """Return the transducer id the plan was finalized against."""
        return self.plan.plan.transducer_id

    def get_parent_planning_session_id(self) -> Optional[str]:
        """Return the id of the PlanningSession that finalized this Plan.

        This is provenance-only; a Plan is a standalone artifact and does not
        depend on its parent PlanningSession still existing.
        """
        return self.plan.plan.parent_planning_session_id

    def get_target(self) -> "Optional[openlifu.db.Plan.target]":
        """Return the frozen :class:`openlifu.geo.Point` target for this Plan."""
        return self.plan.plan.target
