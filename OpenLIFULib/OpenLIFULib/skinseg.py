"""Skin segmentation tools"""

from OpenLIFULib.lazyimport import openlifu_lz
from slicer import vtkMRMLScalarVolumeNode, vtkMRMLModelNode
from OpenLIFULib.coordinate_system_utils import get_IJK2RAS
from OpenLIFULib.util import BusyCursor
import slicer

def generate_skin_mesh(volume_node:vtkMRMLScalarVolumeNode) -> vtkMRMLModelNode:
    volume_array = slicer.util.arrayFromVolume(volume_node).transpose((2,1,0)) # the array indices come in KJI rather than IJK so we permute them
    volume_affine_RAS = get_IJK2RAS(volume_node)
    foreground_mask_array = openlifu_lz().seg.skinseg.compute_foreground_mask(volume_array)
    foreground_mask_vtk_image = openlifu_lz().seg.skinseg.vtk_img_from_array_and_affine(foreground_mask_array, volume_affine_RAS)
    skin_mesh = openlifu_lz().seg.skinseg.create_closed_surface_from_labelmap(foreground_mask_vtk_image)
    skin_node = slicer.mrmlScene.AddNewNodeByClass("vtkMRMLModelNode")
    skin_node.SetAndObservePolyData(skin_mesh)
    return skin_node

def threshold_volume_by_foreground_mask(volume_node:vtkMRMLScalarVolumeNode) -> None:
    """Compute the foreground mask for a loaded volume and threshold the volume to strip out the background.
    This modifies the values of the background region in the volume and sets them to 1 less than the minimum value in the volume.
    This way we can simply enable volume thresholding to remove
    It can take a moment to actually compute the foreground mask.
    """
    volume_array = slicer.util.arrayFromVolume(volume_node)
    volume_array_min = volume_array.min()
    foreground_mask = openlifu_lz().seg.skinseg.compute_foreground_mask(volume_array)
    slicer.util.arrayFromVolume(volume_node)[~foreground_mask] = volume_array_min - 1
    volume_node.GetDisplayNode().SetThreshold(volume_array_min,volume_array.max())
    volume_node.GetDisplayNode().SetApplyThreshold(1)
    volume_node.GetDisplayNode().SetAutoThreshold(0)
    volume_node.Modified()

def load_volume_and_threshold_background(volume_filepath) -> vtkMRMLScalarVolumeNode:
    """Load a volume node from file, and also set the background values to a certain value that can be threshoded out, and threshold it out."""
    volume_node = slicer.util.loadVolume(volume_filepath)
    with BusyCursor():
        threshold_volume_by_foreground_mask(volume_node)
    return volume_node
