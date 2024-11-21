"""Skin segmentation tools"""

from OpenLIFULib.lazyimport import openlifu_lz
from slicer import vtkMRMLScalarVolumeNode, vtkMRMLModelNode
from OpenLIFULib.coordinate_system_utils import get_IJK2RAS
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
