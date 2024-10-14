from typing import Any, Sequence
import numpy as np
from numpy.typing import NDArray
import vtk
import slicer
from slicer import vtkMRMLScalarVolumeNode
from OpenLIFULib.lazyimport import openlifu_lz

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