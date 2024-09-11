import qt
import vtk
from typing import Any, List, Sequence, TYPE_CHECKING, NamedTuple
from scipy.ndimage import affine_transform
from vtk.util import numpy_support
import numpy as np
from numpy.typing import NDArray
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
    SlicerOpenLIFUPoint,
    SlicerOpenLIFUXADataset,
    SlicerOpenLIFUProtocol,
)
from OpenLIFULib.busycursor import BusyCursor
if TYPE_CHECKING:
    import openlifu # This import is deferred at runtime, but it is done here for IDE and static analysis purposes
    import xarray

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

def fiducial_to_openlifu_point_in_transducer_coords(fiducial_node:vtkMRMLMarkupsFiducialNode, transducer:"SlicerOpenLIFUTransducer", name:str = '') -> "openlifu.Point":
    """Given a fiducial node with at least one point, return an openlifu Point in the local coordinates of the given transducer."""
    if fiducial_node.GetNumberOfControlPoints() < 1:
        raise ValueError(f"Fiducial node {fiducial_node.GetID()} does not have any points.")
    position = (np.linalg.inv(slicer.util.arrayFromTransformMatrix(transducer.transform_node)) @ np.array([*fiducial_node.GetNthControlPointPosition(0),1]))[:3] # TODO handle 4th coord here actually, would need to unprojectivize
    return openlifu_lz().Point(position=position, name = name, dims=('x','y','z'), units = transducer.transducer.transducer.units) # Here x,y,z means transducer coordinates.

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
    volumeNode = slicer.mrmlScene.AddNewNodeByClass("vtkMRMLScalarVolumeNode", nodeName)
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

@parameterPack
class SlicerOpenLIFUTransducer:
    """An openlifu Trasducer that has been loaded into Slicer (has a model node and transform node)"""
    transducer : SlicerOpenLIFUTransducerWrapper
    model_node : vtkMRMLModelNode
    transform_node : vtkMRMLTransformNode

    @staticmethod
    def initialize_from_openlifu_transducer(transducer : "openlifu.Transducer") -> "SlicerOpenLIFUTransducer":
        """Initialize object with needed scene nodes from just the openlifu object."""

        model_node = slicer.mrmlScene.AddNewNodeByClass("vtkMRMLModelNode")
        model_node.SetName(transducer.id)
        model_node.SetAndObservePolyData(transducer.get_polydata())
        transform_node = slicer.mrmlScene.AddNewNodeByClass("vtkMRMLTransformNode")
        transform_node.SetName(f"{transducer.id}-matrix")
        model_node.SetAndObserveTransformNodeID(transform_node.GetID())

        # TODO: Instead of harcoding 'LPS' here, use something like a "dims" attribute that should be associated with
        # self.current_session.transducer.matrix. There is no such attribute yet but it should exist eventually once this is done:
        # https://github.com/OpenwaterHealth/opw_neuromod_sw/issues/3
        openlifu2slicer_matrix = linear_to_affine(
            get_xxx2ras_matrix('LPS') * get_xx2mm_scale_factor(transducer.units)
        )
        transform_matrix_numpy = openlifu2slicer_matrix @ transducer.matrix

        transform_matrix_vtk = numpy_to_vtk_4x4(transform_matrix_numpy)
        transform_node.SetMatrixTransformToParent(transform_matrix_vtk)
        model_node.CreateDefaultDisplayNodes() # toggles the "eyeball" on

        # Transducers should not have a transform matrix; it can mess up simulation down the line;
        # openlifu functions can try to be clever and use the transform matrix inside the transducer,
        # while here we put the transform into a slicer transform node and use that.
        # See https://github.com/OpenwaterHealth/OpenLIFU-python/issues/97
        # Once that issue is resolved we should no longer need this line.
        # And we will have to pass the transform into this initialization function as a separate parameter.
        transducer.matrix=np.eye(4)

        return SlicerOpenLIFUTransducer(
            SlicerOpenLIFUTransducerWrapper(transducer), model_node, transform_node
        )

    def clear_nodes(self) -> None:
        """Clear associated mrml nodes from the scene. Do this when removing a transducer."""
        slicer.mrmlScene.RemoveNode(self.model_node)
        slicer.mrmlScene.RemoveNode(self.transform_node)

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
