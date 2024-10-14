import qt
import vtk
from typing import Any, List, Sequence, TYPE_CHECKING, NamedTuple, Optional, Tuple, Type, TypeVar
from scipy.ndimage import affine_transform
from vtk.util import numpy_support
import numpy as np
from numpy.typing import NDArray
from pathlib import Path
import slicer
from slicer import (
    vtkMRMLModelNode,
    vtkMRMLTransformNode,
    vtkMRMLScalarVolumeNode,
    vtkMRMLMarkupsFiducialNode,
)
from slicer.parameterNodeWrapper import parameterPack
from slicer.parameterNodeWrapper.serializers import createSerializerFromAnnotatedType
import logging
from OpenLIFULib.lazyimport import openlifu_lz, xarray_lz
from OpenLIFULib.parameter_node_utils import (
    SlicerOpenLIFUTransducerWrapper,
    SlicerOpenLIFUSessionWrapper,
    SlicerOpenLIFUPoint,
    SlicerOpenLIFUXADataset,
    SlicerOpenLIFUProtocol,
)
from OpenLIFULib.busycursor import BusyCursor
if TYPE_CHECKING:
    import openlifu # This import is deferred at runtime, but it is done here for IDE and static analysis purposes
    import openlifu.db
    import xarray
    from OpenLIFUData.OpenLIFUData import OpenLIFUDataParameterNode

__all__ = [
    "openlifu_lz",
    "xarray_lz",
    "SlicerOpenLIFUPlan",
    "SlicerOpenLIFUProtocol",
    "SlicerOpenLIFUTransducer",
    "SlicerOpenLIFUPoint",
    "SlicerOpenLIFUXADataset",
    "PlanFocus",
    "display_errors",
    "create_noneditable_QStandardItem",
    "ensure_list",
    "add_slicer_log_handler",
    "get_xxx2ras_matrix",
    "get_xx2mm_scale_factor",
    "fiducial_to_openlifu_point_in_transducer_coords",
    "make_volume_from_xarray_in_transducer_coords",
    "make_xarray_in_transducer_coords_from_volume",
    "BusyCursor",
]

def get_openlifu_data_parameter_node() -> "OpenLIFUDataParameterNode":
    """Get the parameter node of the OpenLIFU Data module"""
    return slicer.util.getModuleLogic('OpenLIFUData').getParameterNode()

def display_errors(f):
    """Decorator to make functions forward their python exceptions along as slicer error displays"""
    def f_with_forwarded_errors(*args, **kwargs):
        try:
            return f(*args, **kwargs)
        except Exception as e:
            slicer.util.errorDisplay(f'Exception raised in {f.__name__}: {e}')
            raise e
    return f_with_forwarded_errors

class SlicerLogHandler(logging.Handler):
    def __init__(self, name_to_print, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.name_to_print = name_to_print

    def emit(self, record):
        if record.levelno == logging.ERROR:
            method_to_use = self.handle_error
        elif record.levelno == logging.WARNING:
            method_to_use = self.handle_warning
        else: # info or any other unaccounted for log message level
            method_to_use = self.handle_info
        method_to_use(self.format(record))

    def handle_error(self, msg):
        slicer.util.errorDisplay(f"{self.name_to_print}: {msg}")

    def handle_warning(self, msg):
        slicer.util.warningDisplay(f"{self.name_to_print}: {msg}")

    def handle_info(self, msg):
        slicer.util.showStatusMessage(f"{self.name_to_print}: {msg}")

def add_slicer_log_handler(openlifu_object: Any):
    """Adds an appropriately named SlicerLogHandler to the logger of an openlifu object,
    and only doing so if that logger doesn't already have a SlicerLogHandler"""
    if not hasattr(openlifu_object, "logger"):
        raise ValueError("This object does not have a logger attribute.")
    if not hasattr(openlifu_object, "__class__"):
        raise ValueError("This object is not an instance of an openlifu class.")
    logger : logging.Logger = openlifu_object.logger
    if not any(isinstance(h, SlicerLogHandler) for h in logger.handlers):
        handler = SlicerLogHandler(openlifu_object.__class__.__name__)
        logger.addHandler(handler)

# TODO: Fix the matlab weirdness in openlifu so that we can get rid of ensure_list here.
# The reason for ensure_list is to deal with the fact that matlab fails to distinguish
# between a list with one element and the element itself, and so it doesn't write out
# singleton lists properly
def ensure_list(item: Any) -> List[Any]:
    """ Ensure the input is a list. This is a no-op for lists, and returns a singleton list when given non-list input. """
    if isinstance(item, list):
        return item
    else:
        return [item]

def create_noneditable_QStandardItem(text:str) -> qt.QStandardItem:
            item = qt.QStandardItem(text)
            item.setEditable(False)
            return item

def numpy_to_vtk_4x4(numpy_array_4x4 : NDArray[Any]) -> vtk.vtkMatrix4x4:
            if numpy_array_4x4.shape != (4, 4):
                raise ValueError("The input numpy array must be of shape (4, 4).")
            vtk_matrix = vtk.vtkMatrix4x4()
            for i in range(4):
                for j in range(4):
                    vtk_matrix.SetElement(i, j, numpy_array_4x4[i, j])
            return vtk_matrix

directions_in_RAS_coords_dict = {
    'R' : np.array([1,0,0]),
    'A' : np.array([0,1,0]),
    'S' : np.array([0,0,1]),
    'L' : np.array([-1,0,0]),
    'P' : np.array([0,-1,0]),
    'I' : np.array([0,0,-1]),
}

def get_xxx2ras_matrix(dims:Sequence[str]) -> NDArray[Any]:
    return np.array([
        directions_in_RAS_coords_dict[dim] for dim in dims
    ]).transpose()

def get_xx2mm_scale_factor(length_unit:str) -> float:
    openlifu = openlifu_lz()
    return openlifu.util.units.getsiscale(length_unit, 'distance') / openlifu.util.units.getsiscale('mm', 'distance')

def linear_to_affine(matrix, translation=None):
    """Convert linear 3x3 transform to an affine 4x4 with
    the given translation vector (the default being no translation)"""
    if translation is None:
        translation = np.zeros(3)
    if matrix.shape != (3, 3):
        raise ValueError("The input numpy array must be of shape (3, 3).")
    return np.concatenate(
        [
            np.concatenate([matrix,translation.reshape(-1,1)], axis=1),
            np.array([[0,0,0,1]], dtype=float),
        ],
        axis=0,
    )

def get_RAS2IJK(volume_node: vtkMRMLScalarVolumeNode):
    """Get the _world_ RAS to volume IJK affine matrix for a given volume node.

    This takes into account any transforms that the volume node may be subject to.

    Returns a numpy array of shape (4,4).
    """
    IJK_to_volumeRAS_vtk = vtk.vtkMatrix4x4()
    volume_node.GetRASToIJKMatrix(IJK_to_volumeRAS_vtk)
    IJK_to_volumeRAS = slicer.util.arrayFromVTKMatrix(IJK_to_volumeRAS_vtk)
    if volume_node.GetParentTransformNode():
        volumeRAS_to_worldRAS_vtk = vtk.vtkMatrix4x4()
        volume_node.GetParentTransformNode().GetMatrixTransformToWorld(volumeRAS_to_worldRAS_vtk)
        volumeRAS_to_worldRAS = slicer.util.arrayFromVTKMatrix(volumeRAS_to_worldRAS_vtk)
        IJK_to_worldRAS = volumeRAS_to_worldRAS @ IJK_to_volumeRAS
    else:
        IJK_to_worldRAS = IJK_to_volumeRAS
    return IJK_to_worldRAS

def openlifu_point_to_fiducial(point : "openlifu.Point") -> vtkMRMLMarkupsFiducialNode:
    """Create a fiducial node out of an openlifu Point."""
    fiducial_node : vtkMRMLMarkupsFiducialNode = slicer.mrmlScene.AddNewNodeByClass("vtkMRMLMarkupsFiducialNode")
    fiducial_node.SetName(slicer.mrmlScene.GenerateUniqueName(point.id))

    # Get point position and convert it to Slicer coordinates
    position = np.array(point.position)
    position = get_xxx2ras_matrix(point.dims) @ position
    position = get_xx2mm_scale_factor(point.units) * position

    target_display_node = fiducial_node.GetDisplayNode()
    target_display_node.SetSelectedColor(point.color)
    fiducial_node.SetLocked(True)
    fiducial_node.SetMaximumNumberOfControlPoints(1)

    fiducial_node.AddControlPoint(
        position
    )
    fiducial_node.SetNthControlPointLabel(0,point.name)

    return fiducial_node

def fiducial_to_openlifu_point_in_transducer_coords(fiducial_node:vtkMRMLMarkupsFiducialNode, transducer:"SlicerOpenLIFUTransducer", name:str = '') -> "openlifu.Point":
    """Given a fiducial node with at least one point, return an openlifu Point in the local coordinates of the given transducer."""
    if fiducial_node.GetNumberOfControlPoints() < 1:
        raise ValueError(f"Fiducial node {fiducial_node.GetID()} does not have any points.")
    position = (np.linalg.inv(slicer.util.arrayFromTransformMatrix(transducer.transform_node)) @ np.array([*fiducial_node.GetNthControlPointPosition(0),1]))[:3] # TODO handle 4th coord here actually, would need to unprojectivize
    return openlifu_lz().Point(position=position, name = name, dims=('x','y','z'), units = transducer.transducer.transducer.units) # Here x,y,z means transducer coordinates.

def fiducial_to_openlifu_point(fiducial_node:vtkMRMLMarkupsFiducialNode) -> "openlifu.Point":
    """Given a fiducial node with at least one point, return an openlifu Point in RAS coordinates.
    This tries to be roughly an inverse operation of `openlifu_point_to_fiducial`, but isn't an inverse when it comes to
    for example the name, id, coordinates, and units."""
    if fiducial_node.GetNumberOfControlPoints() < 1:
        raise ValueError(f"Fiducial node {fiducial_node.GetID()} does not have any points.")
    return openlifu_lz().Point(
        position = np.array(fiducial_node.GetNthControlPointPosition(0)),
        name = fiducial_node.GetNthControlPointLabel(0),
        id = fiducial_node.GetName(),
        dims=('R','A','S'),
        units = "mm",
    )

def make_volume_from_xarray_in_transducer_coords(data_array: "xarray.DataArray", transducer: "SlicerOpenLIFUTransducer") -> vtkMRMLScalarVolumeNode:
    """Convert a DataArray in the coordinates of a given transducer into a volume node. It is assumed that the DataArray coords form a regular grid.
    See also `make_xarray_in_transducer_coords_from_volume`.
    """
    array = data_array.data
    coords = data_array.coords

    nodeName = data_array.name
    imageSize = list(array.shape)
    voxelType=vtk.VTK_DOUBLE

    imageData = vtk.vtkImageData()
    imageData.SetDimensions(imageSize)
    imageData.AllocateScalars(voxelType, 1)

    vtk_array = numpy_support.numpy_to_vtk(array.transpose((2,1,0)).ravel(), deep=True, array_type=voxelType)
    imageData.GetPointData().SetScalars(vtk_array)

    # Create volume node
    volumeNode = slicer.mrmlScene.AddNewNodeByClass("vtkMRMLScalarVolumeNode", slicer.mrmlScene.GenerateUniqueName(nodeName))
    volumeNode.SetOrigin([float(coords[x][0]) for x in coords])
    volumeNode.SetSpacing([np.diff(coords[x][:2]).item() for x in coords])
    volumeNode.SetAndObserveImageData(imageData)
    volumeNode.CreateDefaultDisplayNodes()

    volumeNode.SetAndObserveTransformNodeID(transducer.transform_node.GetID())

    return volumeNode

def make_xarray_in_transducer_coords_from_volume(volume_node:vtkMRMLScalarVolumeNode, transducer:"SlicerOpenLIFUTransducer", protocol:"openlifu.Protocol") -> "xarray.DataArray":
    """Convert a volume node into a DataArray in the coordinates of a given transducer.
    See also `make_volume_from_xarray_in_transducer_coords`.
    """
    coords = protocol.sim_setup.get_coords()
    origin = np.array([coord_array[0].item() for coord_array in coords.values()])
    spacing = np.array([np.diff(coord_array)[0].item() for coord_array in coords.values()])
    coords_shape = tuple(coords.sizes.values())

    # Here are the coordinate systems involved:
    # ijk : DataArray indices. When running openlifu simulations, this would typically be the "simulation grid"
    # xyz : Transducer coordinates. x=lateral, y=elevation, z=axial. When the transducer is on the patient forehead, this roughly relates
    # to patient coordinates as follows: x=right, y=superior, z=posterior. (When I say x=right I mean x increases as you go right)
    # ras : The slicer world RAS coordinate system
    # IJK : the volume node's underlying data array indices
    ijk2xyz = np.concatenate([np.concatenate([np.diag(spacing),origin.reshape(3,1)], axis=1), np.array([0,0,0,1],dtype=origin.dtype).reshape(1,4)])
    xyz2ras = slicer.util.arrayFromTransformMatrix(transducer.transform_node)
    ras2IJK = get_RAS2IJK(volume_node)
    ijk2IJK = ras2IJK @ xyz2ras @ ijk2xyz
    volume_resampled_array = affine_transform(
        slicer.util.arrayFromVolume(volume_node).transpose((2,1,0)), # the array indices come in KJI rather than IJK so we permute them
        ijk2IJK,
        order = 1, # equivalent to trilinear interpolation, I think
        mode = 'nearest', # method of sampling beyond input array boundary
        output_shape = coords_shape,
    )
    volume_resampled_dataarray = xarray_lz().DataArray(
        volume_resampled_array,
        coords=coords,
        name=volume_node.GetName(),
        attrs={'vtkMRMLNodeID':volume_node.GetID(),}
    )
    return volume_resampled_dataarray

def get_target_candidates() -> List[vtkMRMLMarkupsFiducialNode]:
    """Get all fiducial nodes that could be considered openlifu targets, i.e. sonication targets.

    Right now the criterion is just that it be a fiducial markup with a single point in its point list.
    However in the future we will probably also avoid certain attributes to exclude
    for example a registration marker or a sonication focus point.
    (Remember, sonication focus points are part of a focal pattern centered around a sonication target,
    which is a concept we are distinguishing from sonication *target*)
    """
    return [
        fiducial_node
        for fiducial_node in slicer.util.getNodesByClass('vtkMRMLMarkupsFiducialNode')
        if fiducial_node.GetNumberOfControlPoints() == 1
    ]

def assign_openlifu_metadata_to_volume_node(volume_node: vtkMRMLScalarVolumeNode, metadata: dict):
    """ Assign the volume name and ID used by OpenLIFU to a volume node"""

    volume_node.SetName(metadata['name'])
    volume_node.SetAttribute('OpenLIFUData.volume_id', metadata['id'])
    
@parameterPack
class SlicerOpenLIFUTransducer:
    """An openlifu Trasducer that has been loaded into Slicer (has a model node and transform node)"""
    transducer : SlicerOpenLIFUTransducerWrapper
    model_node : vtkMRMLModelNode
    transform_node : vtkMRMLTransformNode

    @staticmethod
    def initialize_from_openlifu_transducer(
            transducer : "openlifu.Transducer",
            transducer_matrix: Optional[np.ndarray]=None,
            transducer_matrix_units: Optional[str]=None,
        ) -> "SlicerOpenLIFUTransducer":
        """Initialize object with needed scene nodes from just the openlifu object.

        Args:
            transducer: The openlifu Transducer object
            transducer_matrix: The transform matrix of the transducer. Assumed to be the identity if None.
            transducer_matrix_units: The units in which to interpret the transform matrix.
                The transform matrix operates on a version of the coordinate space of the transducer that has been scaled to
                these units. If left as None then the transducer's native units (Transducer.units) will be assumed.

        Returns: the newly constructed SlicerOpenLIFUTransducer object
        """

        model_node = slicer.mrmlScene.AddNewNodeByClass("vtkMRMLModelNode")
        model_node.SetName(slicer.mrmlScene.GenerateUniqueName(transducer.id))
        model_node.SetAndObservePolyData(transducer.get_polydata())
        transform_node = slicer.mrmlScene.AddNewNodeByClass("vtkMRMLTransformNode")
        transform_node.SetName(slicer.mrmlScene.GenerateUniqueName(f"{transducer.id}-matrix"))
        model_node.SetAndObserveTransformNodeID(transform_node.GetID())

        # TODO: Instead of harcoding 'LPS' here, use something like a "dims" attribute that should be associated with
        # the `transducer` object. There is no such attribute yet but it should exist eventually once this is done:
        # https://github.com/OpenwaterHealth/opw_neuromod_sw/issues/3
        openlifu2slicer_matrix = linear_to_affine(
            get_xxx2ras_matrix('LPS') * get_xx2mm_scale_factor(transducer.units)
        )
        if transducer_matrix is None:
            transducer_matrix = np.eye(4)
        if transducer_matrix_units is None:
            transducer_matrix_units = transducer.units
        transform_in_native_transducer_coordinates = transducer.convert_transform(transducer_matrix, transducer_matrix_units)
        transform_matrix_numpy = openlifu2slicer_matrix @ transform_in_native_transducer_coordinates

        transform_matrix_vtk = numpy_to_vtk_4x4(transform_matrix_numpy)
        transform_node.SetMatrixTransformToParent(transform_matrix_vtk)
        model_node.CreateDefaultDisplayNodes() # toggles the "eyeball" on

        return SlicerOpenLIFUTransducer(
            SlicerOpenLIFUTransducerWrapper(transducer), model_node, transform_node
        )

    def clear_nodes(self) -> None:
        """Clear associated mrml nodes from the scene. Do this when removing a transducer."""
        slicer.mrmlScene.RemoveNode(self.model_node)
        slicer.mrmlScene.RemoveNode(self.transform_node)

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

    def get_transducer_id(self) -> Optional[str]:
        """Get the ID of the openlifu transducer associated with this session"""
        return self.session.session.transducer_id

    def get_protocol_id(self) -> Optional[str]:
        """Get the ID of the openlifu protocol associated with this session"""
        return self.session.session.protocol_id

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

    def get_transducer(self) -> SlicerOpenLIFUTransducer:
        """Return the transducer associated with this session, from the  list of loaded transducers in the scene.

        Does not check that the session is still valid and everything it needs is there in the scene; make sure to
        check before using this.
        """
        return get_openlifu_data_parameter_node().loaded_transducers[self.get_transducer_id()]

    def get_protocol(self) -> SlicerOpenLIFUProtocol:
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
            session: OpenLIFUSession
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

class PlanFocus(NamedTuple):
    """Information that is generated by the SlicerOpenLIFU planning module for a particular focus point"""

    point : SlicerOpenLIFUPoint
    """Focus location"""

    delays : np.ndarray
    """Delays to steer the beam"""

    apodization : np.ndarray
    """Apodization to steer the beam"""

    simulation_output : SlicerOpenLIFUXADataset
    """Output of the k-wave simulation for this configuration"""

@parameterPack
class SlicerOpenLIFUPlan:
    """Information that is generated by running the SlicerOpenLIFU planning module"""

    # We list the type here as "List[Tuple[...]]" to help the parameter node wrapper do the right thing,
    # but really the type is "List[PlanFocus]"
    # The clean solution would have been to make PlanFocus a parameterPack, but it seems
    # that a List of parameterPack is not supported by slicer right now.
    plan_info : List[PlanFocus]
    """List of points for the beam to focus on, each with inforation that was generated to steer the beam"""

    pnp : vtkMRMLScalarVolumeNode
    """Peak negative pressure volume, aggregated over the results from each focus point"""

    intensity : vtkMRMLScalarVolumeNode
    """Average intensity volume, aggregated over the results from each focus point"""

    def clear_nodes(self) -> None:
        """Clear associated mrml nodes from the scene. Do this when removing a transducer."""
        slicer.mrmlScene.RemoveNode(self.pnp)
        slicer.mrmlScene.RemoveNode(self.intensity)

T = TypeVar('T', bound=qt.QWidget)
def replace_widget(old_widget: qt.QWidget, new_widget_class: Type[T], ui_object=None) -> T:
    """Replace a widget by another. Meant for use in a scripted module, to replace widgets inside a layout.

    Args:
        old_widget: The widget to replace. It is assumed to be inside a layout.
        new_widget_class: The class that should be used to construct the new widget.
        ui_object: The ui object from which to erase the replaced widget, if there is one.
            This is referring to the `ui` attribute that is often defined in the setup of scripted
            modules and constructed via `slicer.util.childWidgetVariables`.

    """
    parent = old_widget.parentWidget()
    layout = parent.layout()
    index = layout.indexOf(old_widget)
    layout.removeWidget(old_widget)

    if ui_object is not None:
        ui_attrs_to_delete = [
            child.name
            for child in slicer.util.findChildren(old_widget)
            if hasattr(child,"name")
        ]

    # The order of deleteLater and delattr matters here. The attribute names to remove from the ui_object must be collected before the
    # deletion is requested, and the deletion must be requested before the attributes are dropped -- once the attributes are dropped
    # there is a possibility of the widgets getting auto-deleted just because there is no remaining reference to them.
    old_widget.deleteLater()

    if ui_object is not None:
        for attr_name in ui_attrs_to_delete:
            delattr(ui_object, attr_name)

    new_widget = new_widget_class(parent=parent)
    layout.insertWidget(index, new_widget)
    return new_widget

class OpenLIFUAlgorithmInputWidget(qt.QWidget):
    def __init__(self, parent=None):
        super().__init__(parent)

        layout = qt.QVBoxLayout(self)
        self.setLayout(layout)

        self.protocolComboBox = qt.QComboBox(self)
        self.transducerComboBox = qt.QComboBox(self)
        self.volumeComboBox = qt.QComboBox(self)
        self.targetComboBox = qt.QComboBox(self)

        self.combo_boxes = [
            self.protocolComboBox,
            self.transducerComboBox,
            self.volumeComboBox,
            self.targetComboBox,
        ]

        for combo_box in self.combo_boxes:
            layout.addWidget(combo_box)

    def add_protocol_to_combobox(self, protocol : SlicerOpenLIFUProtocol) -> None:
        self.protocolComboBox.addItem("{} (ID: {})".format(protocol.protocol.name,protocol.protocol.id), protocol)

    def add_transducer_to_combobox(self, transducer : SlicerOpenLIFUTransducer) -> None:
        transducer_openlifu = transducer.transducer.transducer
        self.transducerComboBox.addItem("{} (ID: {})".format(transducer_openlifu.name,transducer_openlifu.id), transducer)

    def add_volume_to_combobox(self, volume_node : vtkMRMLScalarVolumeNode) -> None:
        self.volumeComboBox.addItem("{} (ID: {})".format(volume_node.GetName(),volume_node.GetID()), volume_node)

    def set_session_related_combobox_tooltip(self, text:str):
        """Set tooltip on the transducer and volume comboboxes."""
        self.protocolComboBox.setToolTip(text)
        self.transducerComboBox.setToolTip(text)
        self.volumeComboBox.setToolTip(text)

    def _populate_from_loaded_objects(self) -> None:
        """" Update protocol, transducer, and volume comboboxes based on the OpenLIFU objects loaded into the scene."""

        dataParameterNode = get_openlifu_data_parameter_node()

        # Update protocol combo box
        self.protocolComboBox.clear()
        if len(dataParameterNode.loaded_protocols) == 0:
            self.protocolComboBox.addItem("Select a Protocol")
            self.protocolComboBox.setDisabled(True)
        else:
            self.protocolComboBox.setEnabled(True)
            for protocol in dataParameterNode.loaded_protocols.values():
                self.add_protocol_to_combobox(protocol)

        # Update transducer combo box
        self.transducerComboBox.clear()
        if len(dataParameterNode.loaded_transducers) == 0:
            self.transducerComboBox.addItem("Select a Transducer")
            self.transducerComboBox.setDisabled(True)
        else:
            self.transducerComboBox.setEnabled(True)
            for transducer in dataParameterNode.loaded_transducers.values():
                self.add_transducer_to_combobox(transducer)

        # Update volume combo box
        self.volumeComboBox.clear()
        if len(slicer.util.getNodesByClass('vtkMRMLScalarVolumeNode')) == 0:
            self.volumeComboBox.addItem("Select a Volume")
            self.volumeComboBox.setDisabled(True)
        else:
            self.volumeComboBox.setEnabled(True)
            for volume_node in slicer.util.getNodesByClass('vtkMRMLScalarVolumeNode'):
                self.add_volume_to_combobox(volume_node)

        self.set_session_related_combobox_tooltip("")

    def _populate_from_session(self) -> None:
        """Update protocol, transducer, and volume comboboxes based on the active session, and lock them.

        Does not check that the session is still valid and everything it needs is there in the scene; make sure to
        check before using this.
        """
        session = get_openlifu_data_parameter_node().loaded_session

        # These are the protocol, transducer, and volume that will be used
        protocol : SlicerOpenLIFUProtocol = session.get_protocol()
        transducer : SlicerOpenLIFUTransducer = session.get_transducer()
        volume_node : vtkMRMLScalarVolumeNode = session.volume_node

        # Update protocol combo box
        self.protocolComboBox.clear()
        self.protocolComboBox.setDisabled(True)
        self.add_protocol_to_combobox(protocol)

        # Update transducer combo box
        self.transducerComboBox.clear()
        self.transducerComboBox.setDisabled(True)
        self.add_transducer_to_combobox(transducer)

        # Update volume combo box
        self.volumeComboBox.clear()
        self.volumeComboBox.setDisabled(True)
        self.add_volume_to_combobox(volume_node)

        self.set_session_related_combobox_tooltip("This choice is fixed by the active session")

    def update(self):
        """Update the comboboxes, forcing some of them to take values derived from the active session if there is one"""

        # Update protocol, transducer, and volume comboboxes
        if slicer.util.getModuleLogic('OpenLIFUData').validate_session():
            self._populate_from_session()
        else:
            self._populate_from_loaded_objects()

        # Update target combo box
        self.targetComboBox.clear()
        target_nodes = get_target_candidates()
        if len(target_nodes) == 0:
            self.targetComboBox.addItem("Select a Target")
            self.targetComboBox.setDisabled(True)
        else:
            self.targetComboBox.setEnabled(True)
            for target_node in target_nodes:
                self.targetComboBox.addItem("{} (ID: {})".format(target_node.GetName(),target_node.GetID()), target_node)

    def has_valid_selections(self) -> bool:
        """Whether all options have been selected, so that get_current_data would return
        a complete set of data with no `None`s."""
        return all(combo_box.currentData is not None for combo_box in self.combo_boxes)

    def get_current_data(self) -> Tuple[SlicerOpenLIFUProtocol,SlicerOpenLIFUTransducer,vtkMRMLScalarVolumeNode,vtkMRMLMarkupsFiducialNode]:
        """Get the current selections as a tuple"""
        return tuple(combo_box.currentData() for combo_box in self.combo_boxes)
