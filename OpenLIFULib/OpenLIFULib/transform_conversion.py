"""Utilities for converting transforms between SlicerOpenLIFU and openlifu formats"""

from typing import TYPE_CHECKING
import slicer
from slicer import vtkMRMLTransformNode
import numpy as np
from OpenLIFULib.coordinate_system_utils import (
    linear_to_affine,
    get_xxx2ras_matrix,
    get_xx2mm_scale_factor,
)
from OpenLIFULib.lazyimport import openlifu_lz

if TYPE_CHECKING:
    from openlifu.db.session import ArrayTransform

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

def transform_node_to_openlifu(transform_node:vtkMRMLTransformNode, units:str) -> "ArrayTransform":
    """Convert a transducer transform vtkMRMLTransformNode from Slicer to openlifu format.
    The conversion does the following:
    - Extract the matrix from the transform node to get a numpy array
    - Express the transform in LPS coordinates
    - Express the transform in the requested units
    """
    transform_array = slicer.util.arrayFromTransformMatrix(transform_node, toWorld=True)
    openlifu2slicer_matrix = create_openlifu2slicer_matrix(units)
    return openlifu_lz().db.session.ArrayTransform(
        matrix = np.linalg.inv(openlifu2slicer_matrix) @ transform_array,
        units = units,
    )