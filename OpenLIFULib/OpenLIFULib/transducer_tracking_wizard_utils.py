import slicer
import qt
from typing import Tuple, Optional
from slicer import qMRMLThreeDWidget, vtkMRMLViewNode
from OpenLIFULib.util import replace_widget
from OpenLIFULib import SlicerOpenLIFUPhotoscan


def create_dialog_with_viewnode(dialog_title : str, view_node: vtkMRMLViewNode, ui_path: str) -> Tuple[slicer.qMRMLThreeDWidget, qt.QDialog]:
        
    # Create a threeD widget with the viewNode for displaying the photoscan
    # This widget gets destroyed with the dialog so needs to be created each time
    photoscanViewWidget = slicer.qMRMLThreeDWidget()
    photoscanViewWidget.setMRMLScene(slicer.mrmlScene)
    photoscanViewWidget.setMRMLViewNode(view_node)
    
    # Create dialog for photoscan preview and add threeD view widget to dialog
    dialog = slicer.util.loadUI(ui_path)
    ui = slicer.util.childWidgetVariables(dialog)
    dialog.setWindowTitle(dialog_title)
    replace_widget(ui.photoscanPlaceholderWidget, photoscanViewWidget, ui)

    return dialog 

def create_threeD_photoscan_view_node():
    
    # Layout name is used to create and identify the underlying view node 
    layoutName = "PhotoscanCoordinates"
    layoutLabel = "Photoscan Co-ordinate Space"
    layoutColor = [0.97, 0.54, 0.12] # Orange
    # ownerNode manages this view instead of the layout manager (it can be any node in the scene)
    viewOwnerNode = slicer.mrmlScene.AddNewNodeByClass("vtkMRMLScriptedModuleNode")

    # Create a view node if it hasn't been previously created
    viewNode = slicer.util.getFirstNodeByClassByName('vtkMRMLViewNode','ViewPhotoscan')
    if not viewNode:
        viewLogic = slicer.vtkMRMLViewLogic()
        viewLogic.SetMRMLScene(slicer.mrmlScene)
        viewNode = viewLogic.AddViewNode(layoutName)
        viewNode.SetName('ViewPhotoscan')
        viewNode.SetLayoutLabel(layoutLabel)
        viewNode.SetLayoutColor(layoutColor)
        viewNode.SetAndObserveParentLayoutNodeID(viewOwnerNode.GetID())

        # Customize view node. 
        viewNode.SetBackgroundColor(0.98, 0.9,0.77) # shades of orange
        viewNode.SetBackgroundColor2(0.98,0.58,0.4)
        viewNode.SetBoxVisible(False) # Turn off bounding box visibility
        viewNode.SetAxisLabelsVisible(False) # Turn off axis labels visibility

    return viewNode

def display_photoscan_in_viewnode(photoscan: SlicerOpenLIFUPhotoscan, view_node: vtkMRMLViewNode, reset_camera_view: bool = False) -> None:
    """ When a display node is created, by default, no viewIDs are set. When GetViewNodeIDs is null, the node is displayed
    in all views. Therefore, to restrict nodes from being displayed in the photoscan preview widget, we need to set the 
    viewNodeIDs of any displayed nodes to IDs of all viewNodes in the scene, excluding the photoscan widget."""

    # IDs of all the view nodes in the main Window. This excludes the photoscan widget's view node
    views_mainwindow = [node.GetID() for node in slicer.util.getNodesByClass('vtkMRMLViewNode') if node.GetID() != view_node.GetID()]
    
    # Set the view nodes for all displayable nodes.
    # If GetViewNodeIDs() is (), the node is displayed in all views so we need to exclude the photoscan view
    for displayable_node in list(slicer.util.getNodesByClass('vtkMRMLDisplayableNode')):
        if displayable_node.IsA('vtkMRMLScalarVolumeNode'):
            # Check for any volume renderings
            vrDisplayNode = slicer.modules.volumerendering.logic().GetFirstVolumeRenderingDisplayNode(displayable_node)
            if vrDisplayNode and vrDisplayNode.GetVisibility() and not vrDisplayNode.GetViewNodeIDs():
                    vrDisplayNode.SetViewNodeIDs(views_mainwindow)
        elif displayable_node.GetDisplayVisibility() and not displayable_node.GetDisplayNode().GetViewNodeIDs():
            displayable_node.GetDisplayNode().SetViewNodeIDs(views_mainwindow)
    
    # Set the view nodes for the Red, Green and Yellow slice nodes if empty
    for slice_node in list(slicer.util.getNodesByClass('vtkMRMLSliceNode')):
        if slice_node.GetNumberOfThreeDViewIDs() == 0:
            for view_nodeID in views_mainwindow:
                slice_node.AddThreeDViewID(view_nodeID)

    # Display the photoscan 
    photoscan.toggle_model_display(visibility_on = True, viewNode = view_node) # Specify a view node for display

    # Center and fit displayed photoscan in 3D view.
    # This should only happen when the user is viewing the photoscan for the first time. 
    # If the user has previously interacted with the 3Dview widget, then
    # maintain the previous camera/focal point. 
    if reset_camera_view:
        layoutManager = slicer.app.layoutManager()
        for threeDViewIndex in range(layoutManager.threeDViewCount):
            view = layoutManager.threeDWidget(threeDViewIndex).threeDView()
            if view.mrmlViewNode().GetID() == view_node.GetID():
                photoscanViewIndex = threeDViewIndex
        
        threeDWidget = layoutManager.threeDWidget(photoscanViewIndex)
        threeDView = threeDWidget.threeDView() 
        threeDView.rotateToViewAxis(3)  # look from anterior direction
        threeDView.resetFocalPoint()  # reset the 3D view cube size and center it
        threeDView.resetCamera()  # reset camera zoom