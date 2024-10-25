from typing import List, TYPE_CHECKING, Optional, Tuple
from pathlib import Path
import numpy as np
import slicer
from slicer import (
    vtkMRMLTransformNode,
    vtkMRMLScalarVolumeNode,
    vtkMRMLMarkupsFiducialNode,
)
from slicer.parameterNodeWrapper import parameterPack
from OpenLIFULib.util import get_openlifu_data_parameter_node
from OpenLIFULib.lazyimport import openlifu_lz
from OpenLIFULib.parameter_node_utils import SlicerOpenLIFUSessionWrapper
from OpenLIFULib.targets import (
    openlifu_point_to_fiducial,
    fiducial_to_openlifu_point,
    fiducial_to_openlifu_point_id,
)
from OpenLIFULib.coordinate_system_utils import (
    get_xx2mm_scale_factor,
    get_xxx2ras_matrix,
    linear_to_affine,
)

if TYPE_CHECKING:
    import openlifu
    import openlifu.db
    from OpenLIFULib import SlicerOpenLIFUTransducer
    from OpenLIFULib import SlicerOpenLIFUProtocol

def assign_openlifu_metadata_to_volume_node(volume_node: vtkMRMLScalarVolumeNode, metadata: dict):
    """ Assign the volume name and ID used by OpenLIFU to a volume node"""

    volume_node.SetName(metadata['name'])
    volume_node.SetAttribute('OpenLIFUData.volume_id', metadata['id'])

@parameterPack
class SlicerOpenLIFUSession:
    """An openlifu Session that has been loaded into Slicer (i.e. has associated scene data)"""
    session : SlicerOpenLIFUSessionWrapper

    volume_node : vtkMRMLScalarVolumeNode
    """The volume of the session. This is meant to be owned by the session."""

    target_nodes : List[vtkMRMLMarkupsFiducialNode]
    """The list of targets that were loaded by loading the session. We remember these here just
    in order to have the option of unloading them when unloading the session. In SlicerOpenLIFU, all
    fiducial markups in the scene are potential targets, not necessarily just the ones listed here."""

    last_generated_solution_id : Optional[str] = None
    """The solution ID of the last solution that was generated for this session, or None if there isn't one.
    We remember this so that if the currently active solution (there can only be one loaded at a time) is
    the one that matches this ID then we can clean it up when unloading this session."""

    def get_session_id(self) -> str:
        """Get the ID of the underlying openlifu session"""
        return self.session.session.id

    def get_subject_id(self) -> str:
        """Get the ID of the underlying openlifu subject"""
        return self.session.session.subject_id

    def get_transducer_id(self) -> Optional[str]:
        """Get the ID of the openlifu transducer associated with this session"""
        return self.session.session.transducer_id

    def get_protocol_id(self) -> Optional[str]:
        """Get the ID of the openlifu protocol associated with this session"""
        return self.session.session.protocol_id

    def get_volume_id(self) -> Optional[str]:
        """Get the ID of the volume_node associated with this session"""
        return self.volume_node.GetAttribute('OpenLIFUData.volume_id')

    def transducer_is_valid(self) -> bool:
        """Return whether this session's transducer is present in the list of loaded objects."""
        return self.get_transducer_id() in get_openlifu_data_parameter_node().loaded_transducers

    def protocol_is_valid(self) -> bool:
        """Return whether this session's protocol is present in the list of loaded objects."""
        return self.get_protocol_id() in get_openlifu_data_parameter_node().loaded_protocols

    def volume_is_valid(self) -> bool:
        """Return whether this session's volume is present in the scene."""
        return (
            self.volume_node is not None
            and slicer.mrmlScene.GetNodeByID(self.volume_node.GetID()) is not None
        )

    def get_transducer(self) -> "SlicerOpenLIFUTransducer":
        """Return the transducer associated with this session, from the  list of loaded transducers in the scene.

        Does not check that the session is still valid and everything it needs is there in the scene; make sure to
        check before using this.
        """
        return get_openlifu_data_parameter_node().loaded_transducers[self.get_transducer_id()]

    def get_protocol(self) -> "SlicerOpenLIFUProtocol":
        """Return the protocol associated with this session, from the  list of loaded protocols in the scene.

        Does not check that the session is still valid and everything it needs is there in the scene; make sure to
        check before using this.
        """
        return get_openlifu_data_parameter_node().loaded_protocols[self.get_protocol_id()]

    def clear_volume_and_target_nodes(self) -> None:
        """Clear the session's affiliated volume and target nodes from the scene."""
        for node in [self.volume_node, *self.target_nodes]:
            if node is not None:
                slicer.mrmlScene.RemoveNode(node)

    def get_initial_center_point(self) -> Tuple[float]:
        """Get a point in slicer RAS space that would be reasonable to start slices centered on when first loading this session.
        Returns the coordintes of the first target if there is one, or the middle of the volume otherwise."""
        if self.target_nodes:
            return self.target_nodes[0].GetNthControlPointPosition(0)
        bounds = [0]*6
        self.volume_node.GetRASBounds(bounds)
        return tuple(np.array(bounds).reshape((3,2)).sum(axis=1) / 2) # midpoints derived from bounds

    @staticmethod
    def initialize_from_openlifu_session(
        session : "openlifu.db.Session",
        volume_info : dict
    ) -> "SlicerOpenLIFUSession":
        """Create a SlicerOpenLIFUSession from an openlifu Session, loading affiliated data into the scene.

        Args:
            session: OpenLIFU Session
            volume_info: Dictionary containing the metadata (name, id and filepath) of the volume
            being loaded as part of the session
        """

        # Load volume
        volume_node = slicer.util.loadVolume(volume_info['data_abspath'])
        assign_openlifu_metadata_to_volume_node(volume_node, volume_info)

        # Load targets
        target_nodes = [openlifu_point_to_fiducial(target) for target in session.targets]

        return SlicerOpenLIFUSession(SlicerOpenLIFUSessionWrapper(session), volume_node, target_nodes)

    def update_underlying_openlifu_session(self, targets : List[vtkMRMLMarkupsFiducialNode]) -> "openlifu.db.Session":
        """Update the underlying openlifu session and the list of target nodes that are considered to be affiliated with this session.

        Args:
            targets: new list of targets

        Returns: the now updated underlying openlifu Session
        """

        # Update target fiducial nodes in this object
        self.target_nodes = targets

        if self.session.session is None:
            raise RuntimeError("No underlying openlifu session")

        # Update target Points in the underlying Session
        self.session.session.targets = list(map(fiducial_to_openlifu_point,targets))

        # Update transducer transform in the underlying Session
        transducer = get_openlifu_data_parameter_node().loaded_transducers[self.get_transducer_id()]
        transducer_openlifu = transducer.transducer.transducer
        transducer_transform_node : vtkMRMLTransformNode = transducer.transform_node
        transducer_transform_array = slicer.util.arrayFromTransformMatrix(transducer_transform_node, toWorld=True)
        openlifu2slicer_matrix = linear_to_affine(
            get_xxx2ras_matrix('LPS') * get_xx2mm_scale_factor(transducer_openlifu.units)
        )
        self.session.session.array_transform = openlifu_lz().db.session.ArrayTransform(
            matrix = np.linalg.inv(openlifu2slicer_matrix) @ transducer_transform_array,
            units = transducer_openlifu.units,
        )

        return self.session.session

    def approve_virtual_fit_for_target(self, target : Optional[vtkMRMLMarkupsFiducialNode] = None):
        """Apply approval for the virtual fit of the given target. If no target is provided, then
        any existing approval is revoked."""
        target_id = None
        if target is not None:
            target_id = fiducial_to_openlifu_point_id(target)
        self.session.session.virtual_fit_approval_for_target_id = target_id # apply the approval or lack thereof

    def virtual_fit_is_approved_for_target(self, target : vtkMRMLMarkupsFiducialNode) -> bool:
        """Return whether there is a virtual fit approval for the given target"""
        return self.session.session.virtual_fit_approval_for_target_id == fiducial_to_openlifu_point_id(target)

    def toggle_transducer_tracking_approval(self) -> None:
        """Approve transducer tracking if it was not approved. Revoke approval if it was approved."""
        self.session.session.transducer_tracking_approved = not self.session.session.transducer_tracking_approved

    def transducer_tracking_is_approved(self) -> bool:
        """Return whether transducer tracking has been approved"""
        return self.session.session.transducer_tracking_approved
