from typing import List, TYPE_CHECKING, Optional, Tuple, Dict
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
from OpenLIFULib.parameter_node_utils import SlicerOpenLIFUSessionWrapper, SlicerOpenLIFUPhotoscanWrapper
from OpenLIFULib.targets import (
    openlifu_point_to_fiducial,
    fiducial_to_openlifu_point,
)
from OpenLIFULib.transform_conversion import transform_node_to_openlifu
from OpenLIFULib.virtual_fit_results import get_virtual_fit_results_in_openlifu_session_format

if TYPE_CHECKING:
    import openlifu
    import openlifu.db
    from OpenLIFULib import SlicerOpenLIFUTransducer, SlicerOpenLIFUProtocol

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

    affiliated_photocollections : List[str] = []
    """List containing photocollection_reference_numbers for any photocollections associated with the session. We keep track of any
    photocollections associated with the session here so that they can be loaded into slicer during transducer tracking as required."""

    affiliated_photoscans : Dict[str,SlicerOpenLIFUPhotoscanWrapper] = {}
    """Dictionary containing photoscan_id: SlicerOpenLIFUPhotoscanWrapper for any photoscans associated with the session. We keep track of 
    any photoscans associated with the session here so that they can be loaded into slicer as a SlicerOpenLIFUPhotoscan during
    transducer tracking as required. SlicerOpenLIFUPhotoscanWrapper is a wrapper around an openlifu photoscan."""

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

    def get_affiliated_photocollection_reference_numbers(self):
        return self.affiliated_photocollections

    def get_affiliated_photoscan_ids(self):
        return list(self.affiliated_photoscans.keys())
    
    def get_affiliated_photoscans(self):
        """Returns a list of openlifu photoscans associated with this session"""
        return [photoscan.photoscan for photoscan in self.affiliated_photoscans.values()]

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
        volume_info : dict,
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

    def set_affiliated_photocollections(self, affiliated_photocollections : List[str]):
        
        self.affiliated_photocollections = affiliated_photocollections


    def set_affiliated_photoscans(self, affiliated_photoscans : Dict[str, "openlifu.Photoscan"]):
        
        # Wrap the list of affiliated openlifu photoscans using the SlicerOpenLIFUPhotoscanWrapper for 
        # compatability with the SlicerOpenLIFUSession parameter pack. 
        wrapped_openlifu_photoscans = {photoscan.id:SlicerOpenLIFUPhotoscanWrapper(photoscan) for photoscan in affiliated_photoscans.values()}
        self.affiliated_photoscans = wrapped_openlifu_photoscans

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
        self.session.session.array_transform = transform_node_to_openlifu(transducer_transform_node, transducer_openlifu.units)

        # Update virtual fit results
        self.session.session.virtual_fit_results = get_virtual_fit_results_in_openlifu_session_format(
            session_id=self.get_session_id(),
            units = transducer_openlifu.units,
        )

        return self.session.session

    def get_transducer_tracking_approvals(self):
        """Get the transducer tracking approval state in the current session object, a list of photoscan IDs for which
        transducer tracking is approved.
        """
        session_openlifu = self.session.session
        approved_tt_photoscans = [
            photoscan_id for photoscan_id in self.get_affiliated_photoscan_ids()
            for tt_result in session_openlifu.transducer_tracking_results
            if photoscan_id == tt_result.photoscan_id 
            and tt_result.transducer_to_photoscan_tracking_approved 
            and tt_result.photoscan_to_volume_tracking_approved 
        ]

        return approved_tt_photoscans
    
    def get_virtual_fit_approvals(self):

        session_openlifu = self.session.session
        approved_vf_targets = []
        for target in session_openlifu.targets:
            if target.id not in session_openlifu.virtual_fit_results:
                continue
            if session_openlifu.virtual_fit_results[target.id][0]:
                approved_vf_targets.append(target.id)
        
        return approved_vf_targets
