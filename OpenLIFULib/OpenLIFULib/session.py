from typing import List, TYPE_CHECKING, Optional, Tuple, Dict
import numpy as np
import slicer
from slicer import (
    vtkMRMLTransformNode,
    vtkMRMLScalarVolumeNode,
    vtkMRMLMarkupsFiducialNode,
)
from slicer.parameterNodeWrapper import parameterPack
from OpenLIFULib.util import get_app_state, BusyCursor, mark_session_dirty
from OpenLIFULib.volume_thresholding import load_volume_and_threshold_background
from OpenLIFULib.parameter_node_utils import SlicerOpenLIFUSessionWrapper, SlicerOpenLIFUPhotoscanWrapper
from OpenLIFULib.targets import (
    openlifu_point_to_fiducial,
    fiducial_to_openlifu_point,
    assign_unique_color_to_fiducial,
)
from OpenLIFULib.virtual_fit_results import get_virtual_fit_results_in_openlifu_session_format
from OpenLIFULib.skinseg import get_skin_segmentation, generate_skin_segmentation
from OpenLIFULib.transducer_tracking_results import get_transducer_tracking_results_in_openlifu_session_format
from OpenLIFULib.photoscan_registrations import get_photoscan_registrations_in_openlifu_session_format

if TYPE_CHECKING:
    import openlifu
    import openlifu.db
    from OpenLIFULib import SlicerOpenLIFUTransducer, SlicerOpenLIFUProtocol
    from OpenLIFULib.photoscan import SlicerOpenLIFUPhotoscan
    import openlifu.nav.photoscan

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
    """List containing photocollection_ids for any photocollections associated with the session. We keep track of any
    photocollections associated with the session here so that they can be loaded into slicer during transducer localization as required."""

    # NOTE: photoscans are NOT stored as a pack field. The list of photoscan IDs affiliated
    # with this session lives on the underlying ``openlifu.db.Session.photoscans`` (accessed
    # via ``self.session.session.photoscans``), and the loaded ``SlicerOpenLIFUPhotoscan``
    # objects live in ``OpenLIFUAppState.loaded_photoscans``. See #619: keeping a duplicate
    # ``Dict[str, SlicerOpenLIFUPhotoscanWrapper]`` on the pack led to stale entries after
    # session switches because ``@parameterPack``'s Dict-field lifecycle across
    # ``state.loaded_session = None`` / ``= new_session`` transitions was fragile. All
    # photoscan queries now go through :meth:`get_affiliated_photoscan_ids` /
    # :meth:`get_affiliated_photoscans`.

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
        """Get the ID of the volume associated with this session.

        Read from the openlifu Session field rather than the pack's
        ``volume_node.GetAttribute(...)``: the parameterPack's MRML-node
        reference can go stale (return None) when the volume node is removed
        from the scene, which happens during ``clear_session`` and would
        otherwise crash observers that call this before ``loaded_session``
        itself is nulled.
        """
        return self.session.session.volume_id

    def transducer_is_valid(self) -> bool:
        """Return whether this session's transducer is present in the list of loaded objects."""
        return self.get_transducer_id() in get_app_state().loaded_transducers

    def protocol_is_valid(self) -> bool:
        """Return whether this session's protocol is present in the list of loaded objects."""
        return self.get_protocol_id() in get_app_state().loaded_protocols

    def volume_is_valid(self) -> bool:
        """Return whether this session's volume is present in the scene."""
        return (
            self.volume_node is not None
            and slicer.mrmlScene.GetNodeByID(self.volume_node.GetID()) is not None
        )

    def get_target_nodes(self) -> List[vtkMRMLMarkupsFiducialNode]:
        """Return the session's target fiducial nodes, filtering out stale entries.

        The ``target_nodes`` parameterPack field stores each fiducial by MRML ID;
        when a node is removed from the scene, the stored ID goes stale and the
        entry dereferences to ``None``. Every caller that iterates targets should
        go through this accessor so observer cascades that fire during scene
        teardown do not see the ``None`` placeholders.
        """
        return [n for n in self.target_nodes if n is not None]

    def get_transducer(self) -> "SlicerOpenLIFUTransducer":
        """Return the transducer associated with this session, from the  list of loaded transducers in the scene.

        Does not check that the session is still valid and everything it needs is there in the scene; make sure to
        check before using this.
        """
        return get_app_state().loaded_transducers[self.get_transducer_id()]

    def get_protocol(self) -> "SlicerOpenLIFUProtocol":
        """Return the protocol associated with this session, from the  list of loaded protocols in the scene.

        Does not check that the session is still valid and everything it needs is there in the scene; make sure to
        check before using this.
        """
        return get_app_state().loaded_protocols[self.get_protocol_id()]

    def get_affiliated_photocollection_ids(self):
        return self.affiliated_photocollections

    def get_affiliated_photoscan_ids(self) -> List[str]:
        """Return the list of photoscan IDs affiliated with this session.

        Source of truth is the underlying openlifu ``Session.photoscans`` list, which is
        populated from the on-disk ``photoscans.json`` catalog when the session is loaded.
        See #619: previously this returned ``self.affiliated_photoscans.keys()``, a pack-owned
        Dict field that was prone to stale entries across session switches.
        """
        return list(self.session.session.photoscans or [])

    def get_affiliated_photoscans(self) -> "List[openlifu.nav.photoscan.Photoscan]":
        """Return a list of the openlifu Photoscans affiliated with this session.

        Photoscan objects are resolved through ``OpenLIFUAppState.loaded_photoscans`` (the
        single canonical dict of "photoscans currently in the scene"); if the session
        references an ID that is not currently loaded (e.g. the eager-load step during
        session load has not yet run for it), that ID is silently skipped in the returned
        list. Callers that need to know about a missing entry should query
        :meth:`get_affiliated_photoscan_ids` and cross-check ``loaded_photoscans`` themselves.
        """
        loaded_photoscans = get_app_state().loaded_photoscans
        return [
            loaded_photoscans[pid].photoscan.photoscan
            for pid in self.get_affiliated_photoscan_ids()
            if pid in loaded_photoscans
        ]

    def get_affiliated_slicer_photoscans(self) -> "List[SlicerOpenLIFUPhotoscan]":
        """Return the loaded ``SlicerOpenLIFUPhotoscan`` objects affiliated with this session.

        Same lookup as :meth:`get_affiliated_photoscans` but returns the Slicer-side
        parameterPack wrappers (which carry the model / texture / view scene nodes) rather
        than the underlying openlifu Photoscan dataclasses. Silently drops IDs that are not
        yet in ``loaded_photoscans``.
        """
        loaded_photoscans = get_app_state().loaded_photoscans
        return [
            loaded_photoscans[pid]
            for pid in self.get_affiliated_photoscan_ids()
            if pid in loaded_photoscans
        ]

    def get_affiliated_openlifu_photoscan(self, photoscan_id: str) -> "Optional[openlifu.nav.photoscan.Photoscan]":
        """Return the openlifu Photoscan for ``photoscan_id`` if it is affiliated with this session AND currently loaded, else ``None``."""
        if photoscan_id not in self.get_affiliated_photoscan_ids():
            return None
        loaded_photoscans = get_app_state().loaded_photoscans
        slicer_photoscan = loaded_photoscans.get(photoscan_id)
        if slicer_photoscan is None:
            return None
        return slicer_photoscan.photoscan.photoscan

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
        affiliated_photocollections : Optional[List[str]] = None,
        affiliated_photoscans : Optional[Dict[str, "openlifu.nav.photoscan.Photoscan"]] = None,
    ) -> "SlicerOpenLIFUSession":
        """Create a SlicerOpenLIFUSession from an openlifu Session, loading affiliated data into the scene.

        Args:
            session: OpenLIFU Session
            volume_info: Dictionary containing the metadata (name, id and filepath) of the volume
                being loaded as part of the session
            affiliated_photocollections: Optional list of photocollection scan_ids to seed the
                session's affiliated_photocollections field. Callers should read this from the
                database up-front so the returned session is fully populated in one shot.
            affiliated_photoscans: Optional dict of ``{photoscan_id: openlifu Photoscan}`` used
                to sync ``session.photoscans`` with the on-disk catalog. The dict values (the
                Photoscan objects themselves) are the caller's responsibility to eagerly load
                into ``OpenLIFUAppState.loaded_photoscans`` after this factory returns; this
                method no longer stores photoscan wrappers on the pack (see #619).
        """

        # Load volume
        volume_node, foreground_mask = load_volume_and_threshold_background(volume_info['data_abspath'])
        assign_openlifu_metadata_to_volume_node(volume_node, volume_info)

        if (
            (
                any(len(transform_list)>0 for transform_list in session.virtual_fit_results.values()) # if there is a virtual fit result in the session
                or len(session.transducer_tracking_results)>0 # or if there is a transducer localization result
            )
            and get_skin_segmentation(volume_node) is None
        ):
            with BusyCursor():
                generate_skin_segmentation(volume_node, foreground_mask) # provide foreground mask so that we don't waste time recomputing it
            slicer.util.getModuleWidget("OpenLIFU").get_page_widget("OpenLIFUPrePlanning").showSkin(volume_node)

        # Load targets
        target_nodes = [openlifu_point_to_fiducial(target) for target in session.targets]

        # Reconcile ``openlifu.Session.photoscans`` with the caller-supplied on-disk catalog.
        # ``db.get_photoscan_ids`` (a separate catalog file) is the authoritative source, and
        # legacy session JSONs may have a stale or missing ``photoscans`` field; overwrite it
        # here so subsequent ``get_affiliated_photoscan_ids`` reads see the correct list.
        if affiliated_photoscans is not None:
            session.photoscans = list(affiliated_photoscans.keys())

        return SlicerOpenLIFUSession(
            SlicerOpenLIFUSessionWrapper(session),
            volume_node,
            target_nodes,
            list(affiliated_photocollections or []),
        )

    def set_affiliated_photocollections(self, affiliated_photocollections : List[str]):
        
        self.affiliated_photocollections = affiliated_photocollections

    def set_affiliated_photoscans(self, affiliated_photoscans : Dict[str, "openlifu.nav.photoscan.Photoscan"]) -> None:
        """Set the list of photoscan IDs affiliated with this session.

        Overwrites ``self.session.session.photoscans`` (the openlifu-side authoritative
        list) and re-persists the SessionWrapper so the change survives across parameter-node
        reads. This does NOT touch ``OpenLIFUAppState.loaded_photoscans``; callers that want
        the scene state to match should eagerly load / unload photoscans separately.
        """
        new_ids = list(affiliated_photoscans.keys())
        self.session.session.photoscans = new_ids
        # Re-assign the wrapper to fire the parameterPack write hook. Mutating
        # ``self.session.session.photoscans`` in-place would leave the MRML-slot JSON stale.
        self.session = SlicerOpenLIFUSessionWrapper(self.session.session)

    def update_affiliated_photoscan(self, photoscan: "openlifu.nav.photoscan.Photoscan") -> None:
        """Refresh the loaded openlifu Photoscan for ``photoscan.id``.

        The photoscan ID must already be affiliated with this session and loaded in
        ``OpenLIFUAppState.loaded_photoscans``; this method replaces the loaded photoscan's
        wrapper so callers that read ``get_affiliated_photoscans()`` see the updated fields
        (name, approvals, etc.). See #619 -- previously this mutated a Dict on the pack;
        the pack no longer holds photoscan state.
        """
        if photoscan.id not in self.get_affiliated_photoscan_ids():
            raise RuntimeError("The specified photoscan is not affiliated with this session")
        loaded_photoscans = get_app_state().loaded_photoscans
        if photoscan.id not in loaded_photoscans:
            raise RuntimeError(
                f"Photoscan {photoscan.id!r} is affiliated with this session but is not currently loaded;"
                " nothing to update."
            )
        slicer_photoscan = loaded_photoscans[photoscan.id]
        slicer_photoscan.photoscan = SlicerOpenLIFUPhotoscanWrapper(photoscan)
        # Reassign the dict entry to fire the parameterNodeWrapper write hook.
        loaded_photoscans[photoscan.id] = slicer_photoscan
        get_app_state().loaded_photoscans = loaded_photoscans
        # Photoscan objects are written to disk by ``save_session`` (which iterates the
        # session's affiliated photoscans and calls ``write_photoscan`` for each). So an
        # in-memory field edit here is an unsaved change until the user Saves.
        mark_session_dirty()

    def add_target(self, node: vtkMRMLMarkupsFiducialNode) -> None:
        """Register ``node`` as a session-owned target.

        Appends the fiducial to ``target_nodes`` (skipping duplicates and any
        stale/``None`` entries the pack may currently hold) and rewrites the
        underlying openlifu ``Session.targets`` in one shot. This is the only
        supported way to make a scene fiducial count as a session target under the
        session-owns-all model; loose scene fiducials are no longer picked up.
        """
        if self.session.session is None:
            raise RuntimeError("No underlying openlifu session")
        if node is None:
            return
        current = [n for n in self.target_nodes if n is not None]
        if node in current:
            return
        # Assign a distinct display color from the palette before snapshotting the fiducial
        # into the underlying openlifu.Session. This is the only path through which a fiducial
        # becomes a session target for the first time (user placement, import-from-scene, and
        # import-from-file all call this method); session-load creates fiducials directly via
        # openlifu_point_to_fiducial with the previously-stored color, bypassing add_target,
        # so we don't clobber persisted colors here.
        assign_unique_color_to_fiducial(node, current)
        self.target_nodes = [*current, node]
        self.session.session.targets = list(map(fiducial_to_openlifu_point, self.target_nodes))
        mark_session_dirty()

    def remove_target(self, node: vtkMRMLMarkupsFiducialNode) -> bool:
        """Deregister ``node`` from this session's targets.

        Removes the fiducial from ``target_nodes`` (also dropping any stale/``None``
        entries the pack may hold) and rewrites the underlying openlifu
        ``Session.targets``. Returns True if the node was tracked (and therefore
        removed), False otherwise. Does not touch the scene; the caller is
        responsible for ``slicer.mrmlScene.RemoveNode`` when appropriate.
        """
        if self.session.session is None:
            raise RuntimeError("No underlying openlifu session")
        current = [n for n in self.target_nodes if n is not None]
        if node not in current:
            # Still rewrite the pack if we filtered out any None entries.
            if len(current) != len(self.target_nodes):
                self.target_nodes = current
                self.session.session.targets = list(map(fiducial_to_openlifu_point, self.target_nodes))
                mark_session_dirty()
            return False
        self.target_nodes = [n for n in current if n is not node]
        self.session.session.targets = list(map(fiducial_to_openlifu_point, self.target_nodes))
        mark_session_dirty()
        return True

    def update_underlying_openlifu_session(self) -> "openlifu.db.Session":
        """Sync derived session state (targets, transducer transform, VF / PR / TT results)
        from the current scene into the underlying openlifu Session.

        Targets are read from ``self.target_nodes`` (the session-owned list); scene
        fiducials that were never added via :meth:`add_target` are ignored.

        Returns: the now updated underlying openlifu Session
        """

        if self.session.session is None:
            raise RuntimeError("No underlying openlifu session")

        # Update target Points in the underlying Session (filter stale/None entries;
        # see SlicerOpenLIFUSession.get_target_nodes for why the pack may hold Nones).
        valid_target_nodes = self.get_target_nodes()
        self.session.session.targets = list(map(fiducial_to_openlifu_point, valid_target_nodes))

        # We no longer sync the transducer's current scene pose back onto
        # ``session.array_transform``. There is no single "the transducer position" anymore --
        # each page (pre-planning, localization, solution) renders the transducer at whichever
        # of the persisted VF / TT transforms the user has selected. The units of the loaded
        # transducer are still needed below to serialize VF / TT results.
        transducer = get_app_state().loaded_transducers[self.get_transducer_id()]
        transducer_openlifu = transducer.transducer.transducer

        # Update virtual fit results
        self.session.session.virtual_fit_results = get_virtual_fit_results_in_openlifu_session_format(
            session_id=self.get_session_id(),
            units = transducer_openlifu.units,
        )

        # Update photoscan registrations (must happen before TT serialization so that any TT
        # node whose registration was just (re)approved/edited rides on the fresh PR list).
        self.session.session.photoscan_registrations = get_photoscan_registrations_in_openlifu_session_format(
            session_id=self.get_session_id(),
        )

        #Update transducer localization results
        self.session.session.transducer_tracking_results = get_transducer_tracking_results_in_openlifu_session_format(
            session_id=self.get_session_id(),
            transducer_units = transducer_openlifu.units,
        )

        # Cascade-prune SolutionInfo entries whose referenced target no longer exists
        # (SlicerOpenLIFU#611). Target deletion is the primary cascade trigger; a solution
        # that targets a point that is no longer in the session has no meaning. The on-disk
        # purge of the corresponding solution directory happens in
        # ``OpenLIFUDataLogic.save_session`` via ``db.purge_orphaned_solutions``.
        current_target_ids = {p.id for p in self.session.session.targets}
        self.session.session.solutions = [
            si for si in self.session.session.solutions
            if si.target_id in current_target_ids
        ]

        return self.session.session

    def get_transducer_tracking_approvals(self) -> List[str]:
        """Get the transducer localization approval state in the current session object, a list of photoscan IDs for which
        transducer localization is approved.
        """
        # Post-split: a TT result no longer carries the PV approval flag; PV approval lives on
        # the PhotoscanRegistration. We treat a photoscan as "approved for TT" when it has at
        # least one approved TT result. (PR approval is queried separately.)
        session_openlifu = self.session.session
        approved_tt_results = [
            tt_result
            for tt_result in session_openlifu.transducer_tracking_results
            if tt_result.approval
            ]

        approved_tt_photoscans = [
            photoscan.id
            for photoscan in self.get_affiliated_photoscans()
            if any(photoscan.id == tt_result.photoscan_id for tt_result in approved_tt_results)
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
