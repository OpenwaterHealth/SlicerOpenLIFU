"""Skin segmentation tools"""

from OpenLIFULib.lazyimport import openlifu_lz
from slicer import vtkMRMLScalarVolumeNode, vtkMRMLModelNode
from OpenLIFULib.coordinate_system_utils import get_IJK2RAS
from OpenLIFULib.transducer import TRANSDUCER_MODEL_COLORS
import slicer
from typing import Union

def generate_skin_segmentation(volume_node:vtkMRMLScalarVolumeNode) -> vtkMRMLModelNode:
    """Computes the skin segmentation for the given volume. The ID of the volume node used to create the 
    skin segmentation is added as a model node attribute. Note, this is different from the openlifu volume id.
    """
    volume_array = slicer.util.arrayFromVolume(volume_node).transpose((2,1,0)) # the array indices come in KJI rather than IJK so we permute them
    volume_affine_RAS = get_IJK2RAS(volume_node)
    skin_mesh = openlifu_lz().virtual_fit.compute_skin_mesh_from_volume(
        volume_array = volume_array,
        volume_affine_RAS = volume_affine_RAS)
    skin_mesh_node = slicer.mrmlScene.AddNewNodeByClass("vtkMRMLModelNode")
    skin_mesh_node.SetAndObservePolyData(skin_mesh)
    
    skin_mesh_node.SetName(f'{volume_node.GetName()}-skinsegmentation')
    # Set the ID of corresponding volume as a node attribute 
    skin_mesh_node.SetAttribute('OpenLIFUData.volume_id', volume_node.GetID())
    skin_mesh_node.CreateDefaultDisplayNodes()
    skin_mesh_node.GetDisplayNode().SetVisibility(False) # visibility is turned on by default

    # Default display settings
    model_color = TRANSDUCER_MODEL_COLORS["virtual_fit_result"]
    normalized_color = [c / 255.0 for c in model_color]
    skin_mesh_node.GetDisplayNode().SetColor(normalized_color)
    skin_mesh_node.GetDisplayNode().SetOpacity(0.5)
    skin_mesh_node.SetSelectable(False)

    return skin_mesh_node

def get_skin_segmentation(volume : Union[vtkMRMLScalarVolumeNode, str]) -> vtkMRMLModelNode:
    """Returns the model node containing the skin segmentation associated with the specified volume 
    node or ID. Returns None if no skin segmentation is found.
    """

    if isinstance(volume,vtkMRMLScalarVolumeNode):
        volume_id = volume.GetID()
    elif isinstance(volume, str):
        volume_id = volume
    else:
        raise ValueError("Invalid input type.")

    skin_mesh_node = [
        node for node in slicer.util.getNodesByClass('vtkMRMLModelNode') 
        if node.GetAttribute('OpenLIFUData.volume_id') == volume_id
        ]
    if not skin_mesh_node:
        return None
    elif len(skin_mesh_node) > 1:
        raise RuntimeError(f"Found multiple skin segmentation models affiliated with volume {volume_id}")
    else:
        return skin_mesh_node[0]
