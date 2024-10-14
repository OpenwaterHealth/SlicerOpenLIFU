from typing import TYPE_CHECKING
from scipy.ndimage import affine_transform
import numpy as np
import vtk
from vtk.util import numpy_support
import slicer
from slicer import vtkMRMLScalarVolumeNode
from OpenLIFULib.coordinate_system_utils import get_RAS2IJK
from OpenLIFULib.lazyimport import xarray_lz

if TYPE_CHECKING:
    import openlifu
    import openlifu.db
    import xarray
    from OpenLIFULib import SlicerOpenLIFUTransducer

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
