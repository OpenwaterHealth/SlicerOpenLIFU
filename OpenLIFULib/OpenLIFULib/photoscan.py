from typing import TYPE_CHECKING
import vtk
import slicer
from slicer import vtkMRMLVectorVolumeNode, vtkMRMLModelNode
from slicer.parameterNodeWrapper import parameterPack
from OpenLIFULib.parameter_node_utils import (
    SlicerOpenLIFUPhotoscanWrapper,
)

from OpenLIFULib.util import BusyCursor

if TYPE_CHECKING:
    import openlifu

@parameterPack
class SlicerOpenLIFUPhotoscan:
    """"""
    photoscan : SlicerOpenLIFUPhotoscanWrapper
    """Underlying openlifu Photoscan in a thin wrapper"""

    model : vtkMRMLModelNode
    """Photoscan model node"""

    texture : vtkMRMLVectorVolumeNode
    """Texture volume node"""

    @staticmethod
    def initialize_from_openlifu_photoscan(photoscan : "openlifu.Photoscan",) -> "SlicerOpenLIFUPhotoscan":
        """Create a SlicerOpenLIFUPhotoscan from an openlifu Photoscan.

        Args:
            photoscan: OpenLIFU Photoscan object

        Returns: the newly constructed SlicerOpenLIFUPhotoscan object
        """
        with BusyCursor():
            model_node = slicer.mrmlScene.AddNewNodeByClass("vtkMRMLModelNode")
            model_node.SetAndObservePolyData(photoscan.model)
            model_node.SetAttribute('isOpenLIFUPhotoscan', 'True')
            model_node.SetName(slicer.mrmlScene.GenerateUniqueName(f"{photoscan.id}-model"))

            texture_node = slicer.mrmlScene.AddNewNodeByClass("vtkMRMLVectorVolumeNode")
            texture_node.SetAndObserveImageData(photoscan.texture)
            texture_node.SetAttribute('isOpenLIFUPhotoscan', 'True') 
            texture_node.SetName(slicer.mrmlScene.GenerateUniqueName(f"{photoscan.id}-texture"))

        return SlicerOpenLIFUPhotoscan(SlicerOpenLIFUPhotoscanWrapper(photoscan),model_node,texture_node)

    def clear_nodes(self) -> None:
        """Clear associated mrml nodes from the scene. Do this when removing a transducer."""
        slicer.mrmlScene.RemoveNode(self.model_node)
        slicer.mrmlScene.RemoveNode(self.texture_node)

    def show_texture_on_model(self):
        # Shift/Scale texture map to uchar
        filter = vtk.vtkImageShiftScale()
        typeString = self.texture.GetImageData().GetScalarTypeAsString()
        # default
        scale = 1
        if typeString =='unsigned short':
            scale = 1 / 255.0
        filter.SetScale(scale)
        filter.SetOutputScalarTypeToUnsignedChar()
        filter.SetInputData(self.texture.GetImageData())
        filter.SetClampOverflow(True)
        filter.Update()

        modelDisplayNode = self.model.GetDisplayNode()
        modelDisplayNode.SetBackfaceCulling(0)
        textureImageFlipVert = vtk.vtkImageFlip()
        textureImageFlipVert.SetFilteredAxis(1)
        textureImageFlipVert.SetInputConnection(filter.GetOutputPort())
        modelDisplayNode.SetTextureImageDataConnection(textureImageFlipVert.GetOutputPort())

    def is_approved(self) -> bool:
        return self.photoscan.photoscan.photoscan_approved
                       
    def toggle_approval(self) -> None:
        self.photoscan.photoscan.approved = not self.photoscan.photoscan.photoscan_approved 

        
