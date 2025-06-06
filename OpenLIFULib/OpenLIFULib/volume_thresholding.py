"""Volume thresholding tools"""

import logging
import vtk
import slicer
from slicer import vtkMRMLScalarVolumeNode
from OpenLIFULib.util import BusyCursor
from OpenLIFULib.lazyimport import openlifu_lz

def cast_volume_to_float(volume_node:vtkMRMLScalarVolumeNode) -> None:
    """Converts a volume node to float, replacing the underlying vtkImageData."""
    image_cast = vtk.vtkImageCast()
    image_cast.SetInputData(volume_node.GetImageData())
    image_cast.SetOutputScalarTypeToDouble()
    image_cast.Update()
    volume_node.SetAndObserveImageData(image_cast.GetOutput())

    # I am not certain that the display node will know to update itself to handle the new image data type,
    # so I hope poking `CreateDefaultDisplayNodes` here makes it do the right thing. If it's not needed then it's harmless anyway:
    volume_node.CreateDefaultDisplayNodes()

def threshold_volume_by_foreground_mask(volume_node:vtkMRMLScalarVolumeNode) -> None:
    """Compute the foreground mask for a loaded volume and threshold the volume to strip out the background.
    This modifies the values of the background region in the volume and sets them to 1 less than the minimum value in the volume.
    This way we can simply enable volume thresholding to remove
    It can take a moment to actually compute the foreground mask.
    """
    volume_array = slicer.util.arrayFromVolume(volume_node)
    volume_array_min = volume_array.min()

    background_value = volume_array_min - 1
    if background_value < volume_node.GetImageData().GetScalarTypeMin(): # e.g. if volume_array_min is 0 and it's an unsigned int type
        logging.info("Casting volume to float for the sake of `threshold_volume_by_foreground_mask`.")
        cast_volume_to_float(volume_node)

    foreground_mask = openlifu_lz().seg.skinseg.compute_foreground_mask(volume_array)
    slicer.util.arrayFromVolume(volume_node)[~foreground_mask] = background_value
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