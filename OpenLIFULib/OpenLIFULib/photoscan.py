from typing import TYPE_CHECKING
import vtk
from pathlib import Path
import slicer
from slicer import vtkMRMLVectorVolumeNode, vtkMRMLModelNode
from slicer.parameterNodeWrapper import parameterPack
from OpenLIFULib.parameter_node_utils import (
    SlicerOpenLIFUPhotoscanWrapper,
)
from OpenLIFULib.util import BusyCursor
from OpenLIFULib import (
    openlifu_lz,
)

if TYPE_CHECKING:
    import openlifu # This import is deferred at runtime using openlifu_lz, but it is done here for IDE and static analysis purposes

@parameterPack
class SlicerOpenLIFUPhotoscan:
    """"""
    photoscan : SlicerOpenLIFUPhotoscanWrapper 
    """Underlying openlifu Photoscan in a thin wrapper"""

    model_node : vtkMRMLModelNode
    """Photoscan model node"""

    texture_node : vtkMRMLVectorVolumeNode
    """Texture volume node"""

    @staticmethod
    def _create_nodes(model_data, texture_data, node_name_prefix: str):
        """Helper method to create model and texture nodes."""
        model_node = slicer.mrmlScene.AddNewNodeByClass("vtkMRMLModelNode")
        model_node.SetAndObservePolyData(model_data)
        model_node.SetAttribute('isOpenLIFUPhotoscan', 'True')
        model_node.SetName(slicer.mrmlScene.GenerateUniqueName(f"{node_name_prefix}-model"))

        texture_node = slicer.mrmlScene.AddNewNodeByClass("vtkMRMLVectorVolumeNode")
        texture_node.SetAndObserveImageData(texture_data)
        texture_node.SetAttribute('isOpenLIFUPhotoscan', 'True')
        texture_node.SetName(slicer.mrmlScene.GenerateUniqueName(f"{node_name_prefix}-texture"))

        return model_node, texture_node
    
    @staticmethod
    def initialize_from_openlifu_photoscan(photoscan : "openlifu.Photoscan", parent_dir) -> "SlicerOpenLIFUPhotoscan":
        """Create a SlicerOpenLIFUPhotoscan from an openlifu Photoscan.
        Args:
            photoscan: OpenLIFU Photoscan object
            parent_dir: Absolute path to folder containing photoscan data
        Returns: the newly constructed SlicerOpenLIFUPhotoscan object
        """
        with BusyCursor():
            model_data, texture_data = openlifu_lz().db.photoscan.load_data_from_photoscan(photoscan, parent_dir = parent_dir)

        model_node, texture_node = SlicerOpenLIFUPhotoscan._create_nodes(model_data, texture_data, photoscan.id)

        return SlicerOpenLIFUPhotoscan(SlicerOpenLIFUPhotoscanWrapper(photoscan),model_node,texture_node)

    @staticmethod
    def initialize_from_data_filepaths(model_abspath, texture_abspath) -> "SlicerOpenLIFUPhotoscan":
        """Create a SlicerOpenLIFUPhotoscan based on absolute paths to the data filenames.
        Args:
            model_abspath: Absolute path to the model data file
            texture_abspath: Absolute path to the texture data file
        Returns: the newly constructed SlicerOpenLIFUPhotoscan object
        """

        with BusyCursor():
            model_data, texture_data = openlifu_lz().db.photoscan.load_data_from_filepaths(model_abspath, texture_abspath)

        node_name_prefix = Path(model_abspath).stem
        model_node, texture_node = SlicerOpenLIFUPhotoscan._create_nodes(model_data, texture_data, node_name_prefix)

        # Create a dummy photoscan to keep track of metadata to apply to the openlifu object. This photoscan is not associated with the database
        photoscan_openlifu = openlifu_lz().db.photoscan.Photoscan(id = model_node.GetID(), 
                                                                  name = node_name_prefix,
                                                                  )

        return SlicerOpenLIFUPhotoscan(SlicerOpenLIFUPhotoscanWrapper(photoscan_openlifu), model_node,texture_node)

    def clear_nodes(self) -> None:
        """Clear associated mrml nodes from the scene."""
        slicer.mrmlScene.RemoveNode(self.model_node)
        slicer.mrmlScene.RemoveNode(self.texture_node)

    def show_model_with_texture(self):
        """Displays the photoscan model node with the texture image applied"""
        
        # Shift/Scale texture map to uchar
        filter = vtk.vtkImageShiftScale()
        typeString = self.texture_node.GetImageData().GetScalarTypeAsString()
        # default
        scale = 1
        if typeString =='unsigned short':
            scale = 1 / 255.0
        filter.SetScale(scale)
        filter.SetOutputScalarTypeToUnsignedChar()
        filter.SetInputData(self.texture_node.GetImageData())
        filter.SetClampOverflow(True)
        filter.Update()

        self.model_node.CreateDefaultDisplayNodes()
        modelDisplayNode = self.model_node.GetDisplayNode()
        modelDisplayNode.SetBackfaceCulling(0)
        textureImageFlipVert = vtk.vtkImageFlip()
        textureImageFlipVert.SetFilteredAxis(1)
        textureImageFlipVert.SetInputConnection(filter.GetOutputPort())
        modelDisplayNode.SetTextureImageDataConnection(textureImageFlipVert.GetOutputPort())

    def is_approved(self) -> bool:
        return self.photoscan.photoscan.photoscan_approved
                       
    def toggle_approval(self) -> None:
        self.photoscan.photoscan.approved = not self.photoscan.photoscan.photoscan_approved 

        
