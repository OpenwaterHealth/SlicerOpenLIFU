import slicer
import qt
from typing import Tuple, List
from slicer import vtkMRMLViewNode, vtkMRMLModelNode
from OpenLIFULib.util import replace_widget
from OpenLIFULib import SlicerOpenLIFUPhotoscan

def initialize_wizard_ui(wizard: qt.QWizard):

    vBoxLayout = qt.QVBoxLayout()
    wizard.setLayout(vBoxLayout)
    ui_path = slicer.modules.OpenLIFUTransducerTrackerWidget.resourcePath("UI/TransducerTrackingWizard.ui")
    uiWidget = slicer.util.loadUI(ui_path)
    vBoxLayout.addWidget(uiWidget)
    
    return slicer.util.childWidgetVariables(uiWidget)

def set_threeD_view_widget(ui):
     
    viewWidget = slicer.qMRMLThreeDWidget()
    viewWidget.setMRMLScene(slicer.mrmlScene)
    viewWidget.setMinimumHeight(200)

    # Add the threeD view widget to specified ui
    # In the layout, the UI should have the same name
    replace_widget(ui.viewWidgetPlaceholder, viewWidget, ui)

    return viewWidget

def set_threeD_view_node(view_widget, threeD_view_node: vtkMRMLViewNode):

    view_widget.setMRMLViewNode(threeD_view_node)

def create_threeD_photoscan_view_node(photoscan_id: str):
    """Creates view node for displaying the photoscan model. Before transducer tracking registration,
     a subject's photoscan lives in a different coordinate space than their volume. Therefore we need to create
    a separate view node for visualizing the photoscan before registration
    
    Args: photoscan_id This is used to set the name of the view node"""
    
    # Layout name is used to create and identify the underlying view node 
    layoutName = f"PhotoscanCoordinates-{photoscan_id}"
    layoutLabel = "Photoscan Co-ordinate Space"
    layoutColor = [0.97, 0.54, 0.12] # Orange background
    # ownerNode manages this view instead of the layout manager (it can be any node in the scene)
    viewOwnerNode = slicer.mrmlScene.AddNewNodeByClass("vtkMRMLScriptedModuleNode")

    viewNode = slicer.util.getFirstNodeByClassByName('vtkMRMLViewNode',f'view-{photoscan_id}')
    if not viewNode:
        viewLogic = slicer.vtkMRMLViewLogic()
        viewLogic.SetMRMLScene(slicer.mrmlScene)
        viewNode = viewLogic.AddViewNode(layoutName)
        viewNode.SetLayoutLabel(layoutLabel)
        viewNode.SetLayoutColor(layoutColor)
        viewNode.SetName(f'view-{photoscan_id}')
        viewNode.SetAndObserveParentLayoutNodeID(viewOwnerNode.GetID())

    # Customize view node. 
    viewNode.SetBackgroundColor(0.98, 0.9,0.77) # shades of orange
    viewNode.SetBackgroundColor2(0.98,0.58,0.4)
    viewNode.SetBoxVisible(False) # Turn off bounding box visibility
    viewNode.SetAxisLabelsVisible(False) # Turn off axis labels visibility

    return viewNode

def get_threeD_transducer_tracking_view_node():
    """Creates view node for performing transducer tracking
    """

    # Layout name is used to create and identify the underlying view node 
    layoutName = "TransducerTracking"
    layoutLabel = "Volume Co-ordinate Space"
    layoutColor = [0.97, 0.54, 0.12] # Orange background
    # ownerNode manages this view instead of the layout manager (it can be any node in the scene)
    viewOwnerNode = slicer.mrmlScene.AddNewNodeByClass("vtkMRMLScriptedModuleNode")

    viewNode = slicer.util.getFirstNodeByClassByName('vtkMRMLViewNode','view-transducertracking')
    if not viewNode:
        viewLogic = slicer.vtkMRMLViewLogic()
        viewLogic.SetMRMLScene(slicer.mrmlScene)
        viewNode = viewLogic.AddViewNode(layoutName)
        viewNode.SetLayoutLabel(layoutLabel)
        viewNode.SetLayoutColor(layoutColor)
        viewNode.SetName(f'view-transducertracking')
        viewNode.SetAndObserveParentLayoutNodeID(viewOwnerNode.GetID())

    # Customize view node. 
    viewNode.SetBackgroundColor(0.98, 0.9,0.77) # shades of orange
    viewNode.SetBackgroundColor2(0.98,0.58,0.4)
    viewNode.SetBoxVisible(False) # Turn off bounding box visibility
    viewNode.SetAxisLabelsVisible(False) # Turn off axis labels visibility

    return viewNode

def hide_displayable_nodes_from_view(wizard_view_nodes: List[vtkMRMLViewNode]):

    # IDs of all the view nodes in the main Window. This excludes the photoscan's view node
    views_mainwindow = [
        node.GetID() for node in slicer.util.getNodesByClass('vtkMRMLViewNode') 
        for wizard_view_node in wizard_view_nodes
        if node.GetID() != wizard_view_node.GetID()]
    
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

def reset_view_node_camera(view_node: vtkMRMLViewNode):

    layoutManager = slicer.app.layoutManager()
    for threeDViewIndex in range(layoutManager.threeDViewCount):
        view = layoutManager.threeDWidget(threeDViewIndex).threeDView()
        if view.mrmlViewNode().GetID() == view_node.GetID():
            specifiedViewIndex = threeDViewIndex
    
    threeDWidget = layoutManager.threeDWidget(specifiedViewIndex)
    threeDView = threeDWidget.threeDView() 
    threeDView.rotateToViewAxis(3)  # look from anterior direction
    threeDView.resetFocalPoint()  # reset the 3D view cube size and center it
    threeDView.resetCamera()  # reset camera zoom
