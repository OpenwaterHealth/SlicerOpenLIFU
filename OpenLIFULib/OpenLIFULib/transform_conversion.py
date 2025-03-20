"""Utilities for converting transforms between SlicerOpenLIFU and openlifu formats"""

from typing import TYPE_CHECKING
import slicer
from slicer import vtkMRMLTransformNode
import numpy as np
from OpenLIFULib.coordinate_system_utils import (
    linear_to_affine,
    get_xxx2ras_matrix,
    get_xx2mm_scale_factor,
    numpy_to_vtk_4x4,
)
from OpenLIFULib.lazyimport import openlifu_lz

if TYPE_CHECKING:
    from openlifu.geo import ArrayTransform
    from openlifu import Transducer

def create_openlifu2slicer_matrix(units : str) -> np.ndarray:
    """
    Returns a 4x4 affine transform matrix that maps LPS points in transducer units to RAS points in mm
    """
    # TODO: Instead of harcoding 'LPS' here, use something like a "dims" attribute that should be associated with
    # the `transducer` object. There is no such attribute yet but it should exist eventually once this is done:
    # https://github.com/OpenwaterHealth/opw_neuromod_sw/issues/3
    return linear_to_affine(
        get_xxx2ras_matrix('LPS') * get_xx2mm_scale_factor(units)
    )

def transducer_transform_node_to_openlifu(transform_node:vtkMRMLTransformNode, transducer_units:str) -> "ArrayTransform":
    """Convert a transducer transform vtkMRMLTransformNode from Slicer to openlifu format.

    The vtkMRMLTransformNode has a matrix is assumed to convert from the transducer LPS space in the given `transducer_units`
    to the Slicer RAS space in mm.

    The conversion does the following:
    - Extract the matrix from the transform node to get a numpy array
    - Express the transform in LPS coordinates
    - Remove the unit conversion that we build into the Slicer transform nodes, since openlifu stores units separately

    See the reverse function `transform_node_from_openlifu`.
    """
    transform_array = slicer.util.arrayFromTransformMatrix(transform_node, toWorld=True)
    openlifu2slicer_matrix = create_openlifu2slicer_matrix(transducer_units)
    return openlifu_lz().geo.ArrayTransform(
        matrix = np.linalg.inv(openlifu2slicer_matrix) @ transform_array,
        units = transducer_units,
    )

def transducer_transform_node_from_openlifu(
        openlifu_transform_matrix:np.ndarray,
        transducer : "Transducer",
        transform_units:str = None,
    ) -> vtkMRMLTransformNode:
    """Convert a transducer transform matrix to a slicer transform node that has built-in unit conversion.

    The vtkMRMLTransformNode will convert from the transducer LPS space in the given `transducer_units`
    to the Slicer RAS space in mm.

    Args:
        openlifu_transform_matrix: The 4x4 affine transform matrix, which is in `transform_units`
        transducer: The openlifu Transducer that this transform node would eventually be applied to.
            This is needed to get the units right, since conversion to the units of the transducer's native space
            is built into the transform node.
        transform_units: The units of `openlifu_transform_matrix`. If not provided then it is assumed that `openlifu2slicer_matrix`
            is given already in terms of the transducer native units.

    The conversion does the following:
    - Convert the transform matrix from LPS to RAS for slicer.
    - Build the unit conversion into the transform
    - Create a new transform node in the scene.

    See the reverse function `transform_node_to_openlifu`.
    """
    transform_in_native_transducer_coordinates = transducer.convert_transform(openlifu_transform_matrix, transform_units)
    openlifu2slicer_matrix = create_openlifu2slicer_matrix(transducer.units)
    transform_matrix_numpy = openlifu2slicer_matrix @ transform_in_native_transducer_coordinates
    transform_matrix_vtk = numpy_to_vtk_4x4(transform_matrix_numpy)
    transform_node = slicer.mrmlScene.AddNewNodeByClass("vtkMRMLTransformNode")
    transform_node.SetMatrixTransformToParent(transform_matrix_vtk)
    return transform_node
