from typing import TYPE_CHECKING, Optional
import vtk
from pathlib import Path
import slicer
from slicer import vtkMRMLVectorVolumeNode, vtkMRMLModelNode, vtkMRMLViewNode, vtkMRMLMarkupsFiducialNode
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

    tracking_fiducial_node : vtkMRMLMarkupsFiducialNode = None
    """Fiducial node containing the control points required for photoscan-volume registration when
     running transducer tracking. The control points mark the left ear, right ear and nasion."""

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
    def initialize_from_openlifu_photoscan(photoscan_openlifu : "openlifu.Photoscan",
                                           model_data: vtk.vtkPolyData,
                                           texture_data: vtk.vtkImageData
                                           ) -> "SlicerOpenLIFUPhotoscan":
        """Create a SlicerOpenLIFUPhotoscan from an openlifu Photoscan.
        Args:
            photoscan: OpenLIFU Photoscan object
            model_data: vtkPolyData
            texture_data: vtkImageData
        Returns: the newly constructed SlicerOpenLIFUPhotoscan object
        """
        
        model_node, texture_node = SlicerOpenLIFUPhotoscan._create_nodes(model_data, texture_data, photoscan_openlifu.id)
        photoscan = SlicerOpenLIFUPhotoscan(SlicerOpenLIFUPhotoscanWrapper(photoscan_openlifu),model_node,texture_node)
        photoscan.apply_texture_to_model()
        
        return photoscan

    @staticmethod
    def initialize_from_data_filepaths(model_abspath, texture_abspath) -> "SlicerOpenLIFUPhotoscan":
        """Create a SlicerOpenLIFUPhotoscan based on absolute paths to the data filenames.
        Args:
            model_abspath: Absolute path to the model data file
            texture_abspath: Absolute path to the texture data file
        Returns: the newly constructed SlicerOpenLIFUPhotoscan object
        """

        with BusyCursor():
            model_data, texture_data = openlifu_lz().photoscan.load_data_from_filepaths(model_abspath, texture_abspath)

        node_name_prefix = Path(model_abspath).stem
        model_node, texture_node = SlicerOpenLIFUPhotoscan._create_nodes(model_data, texture_data, node_name_prefix)

        # Create a dummy photoscan to keep track of metadata to apply to the openlifu object. This photoscan is not associated with the database
        photoscan_openlifu = openlifu_lz().photoscan.Photoscan(id = model_node.GetID(), 
                                                                  name = node_name_prefix,
                                                                  )
        photoscan = SlicerOpenLIFUPhotoscan(SlicerOpenLIFUPhotoscanWrapper(photoscan_openlifu), model_node,texture_node)
        photoscan.apply_texture_to_model()
        return photoscan
    
    def clear_nodes(self) -> None:
        """Clear associated mrml nodes from the scene."""
        slicer.mrmlScene.RemoveNode(self.model_node)
        slicer.mrmlScene.RemoveNode(self.texture_node)

    def apply_texture_to_model(self):
        """Apply the texture image to the model node"""
        
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

        self.model_node.CreateDefaultDisplayNodes() # By default, this turns model visibility on
        modelDisplayNode = self.model_node.GetDisplayNode()
        modelDisplayNode.SetBackfaceCulling(0)
        textureImageFlipVert = vtk.vtkImageFlip()
        textureImageFlipVert.SetFilteredAxis(1)
        textureImageFlipVert.SetInputConnection(filter.GetOutputPort())
        modelDisplayNode.SetTextureImageDataConnection(textureImageFlipVert.GetOutputPort())

        # Turn model visibility off
        modelDisplayNode.SetVisibility(False)

    def is_approved(self) -> bool:
        return self.photoscan.photoscan.photoscan_approved
                       
    def toggle_approval(self) -> None:
        self.photoscan.photoscan.photoscan_approved = not self.photoscan.photoscan.photoscan_approved 
    
    def toggle_model_display(self, visibility_on: bool = False, viewNode: vtkMRMLViewNode = None):
        """ If a viewNode is not specified, the model is displayed in all views by default"""
        self.model_node.GetDisplayNode().SetVisibility(visibility_on)
        self.model_node.GetDisplayNode().SetViewNodeIDs([viewNode.GetID()] if viewNode else [])
        
        if self.tracking_fiducial_node:
            self.tracking_fiducial_node.GetDisplayNode().SetVisibility(visibility_on)
            self.tracking_fiducial_node.GetDisplayNode().SetViewNodeIDs([viewNode.GetID()] if viewNode else [])
                        
    def create_tracking_fiducial_node(self, right_ear_coordinates = [0,0,0], left_ear_coordinates = [0,0,0], nasion_coordinates = [0,0,0]):
        """Nodes are created by default at the origin"""

        photoscan_id = self.photoscan.photoscan.id
        self.tracking_fiducial_node = slicer.mrmlScene.AddNewNodeByClass("vtkMRMLMarkupsFiducialNode",f"Photoscan-{photoscan_id}-TrackingFiducials" )
        self.tracking_fiducial_node.SetMaximumNumberOfControlPoints(3)
        self.tracking_fiducial_node.SetMarkupLabelFormat("%N")
        self.tracking_fiducial_node.AddControlPoint(right_ear_coordinates[0],right_ear_coordinates[0],right_ear_coordinates[0],"Right Ear")
        self.tracking_fiducial_node.AddControlPoint(left_ear_coordinates[0],left_ear_coordinates[0],left_ear_coordinates[0],"Left Ear")
        self.tracking_fiducial_node.AddControlPoint(nasion_coordinates[0],nasion_coordinates[0],nasion_coordinates[0],"Nasion")
        
        return self.tracking_fiducial_node

        
