import slicer
import qt
from typing import Tuple, List
from slicer import vtkMRMLViewNode, vtkMRMLModelNode
from OpenLIFULib.util import replace_widget
from OpenLIFULib import SlicerOpenLIFUPhotoscan

def initialize_wizard_ui(wizard: qt.QWizard):

    layout = qt.QVBoxLayout()
    layout.setContentsMargins(0, 0, 0, 0)
    wizard.setLayout(layout)
    ui_path = slicer.modules.OpenLIFUTransducerLocalizationWidget.resourcePath("UI/TransducerLocalizationWizard.ui")
    uiWidget = slicer.util.loadUI(ui_path)
    ui = slicer.util.childWidgetVariables(uiWidget)

    # Reparent the key widgets directly into the page layout, bypassing the
    # intermediate uiWidget container whose designer geometry (553x793) constrains sizing.
    layout.addWidget(ui.viewWidgetPlaceholder, 1)  # stretch=1 so it fills remaining space
    layout.addWidget(ui.dialogControls)
    layout.addWidget(ui.lockPanel)

    return ui

def set_threeD_view_widget(ui):

    viewWidget = slicer.qMRMLThreeDWidget()
    viewWidget.setMRMLScene(slicer.mrmlScene)
    viewWidget.setMinimumHeight(200)
    viewWidget.setSizePolicy(qt.QSizePolicy.Expanding, qt.QSizePolicy.Expanding)

    # Replace the placeholder with the 3D view widget in the page layout.
    # The stretch factor is already set in initialize_wizard_ui.
    replace_widget(ui.viewWidgetPlaceholder, viewWidget, ui)

    return viewWidget

def set_threeD_view_node(view_widget, threeD_view_node: vtkMRMLViewNode):

    view_widget.setMRMLViewNode(threeD_view_node)

def create_threeD_photoscan_view_node(photoscan_id: str):
    """Creates view node for displaying the photoscan model. Before transducer localization registration,
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
        viewNode.SetAttribute("isWizardViewNode", "true") 

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
        viewNode.SetAttribute("isWizardViewNode", "true")  # Set an attribute to identify this as a wizard view nodee

    # Customize view node. 
    viewNode.SetBackgroundColor(0.98, 0.9,0.77) # shades of orange
    viewNode.SetBackgroundColor2(0.98,0.58,0.4)
    viewNode.SetBoxVisible(False) # Turn off bounding box visibility
    viewNode.SetAxisLabelsVisible(False) # Turn off axis labels visibility

    return viewNode

def hide_displayable_nodes_from_view(wizard_view_nodes: List[vtkMRMLViewNode]):

    # IDs of all the view nodes in the main Window. This excludes the photoscan's view node
    all_view_nodes = slicer.util.getNodesByClass('vtkMRMLViewNode')
    wizard_node_ids = [node.GetID() for node in wizard_view_nodes]

    # Exclude any wizard related nodes. Even view nodes associated with other photoscans.
    views_mainwindow = [node.GetID() for node in all_view_nodes if node.GetAttribute("isWizardViewNode") != "true"]
    
    # Set the view nodes for all displayable nodes.
    # If GetViewNodeIDs() is (), the node is displayed in all views so we need to exclude the photoscan view
    for displayable_node in list(slicer.util.getNodesByClass('vtkMRMLDisplayableNode')):
        
        # If the node has a custom set of view nodes, we need to preserve them
        if displayable_node.GetDisplayNode() and displayable_node.GetDisplayNode().GetViewNodeIDs():
            view_nodes = [node_id for node_id in displayable_node.GetDisplayNode().GetViewNodeIDs() if node_id not in wizard_node_ids]
        else:
            view_nodes = views_mainwindow

        if displayable_node.IsA('vtkMRMLScalarVolumeNode'):
            # Check for any volume renderings
            vrDisplayNode = slicer.modules.volumerendering.logic().GetFirstVolumeRenderingDisplayNode(displayable_node)
            if vrDisplayNode and vrDisplayNode.GetVisibility():
                # If the node has a custom set of view nodes, we need to preserve them
                if vrDisplayNode.GetViewNodeIDs():
                    view_nodes = [node_id for node_id  in vrDisplayNode.GetViewNodeIDs() if node_id not in wizard_node_ids]
                else:
                    view_nodes = views_mainwindow
                vrDisplayNode.SetViewNodeIDs(view_nodes)
        elif displayable_node.IsA('vtkMRMLTransformNode') and displayable_node.GetDisplayNode() is not None:
            displayable_node.GetDisplayNode().SetEditorVisibility(False)
        elif displayable_node.IsA('vtkMRMLMarkupsNode') and displayable_node.GetDisplayVisibility():
            fiducial_views = view_nodes + ['vtkMRMLSliceNodeRed','vtkMRMLSliceNodeYellow','vtkMRMLSliceNodeGreen']
            displayable_node.GetDisplayNode().SetViewNodeIDs(fiducial_views)
        elif displayable_node.GetDisplayVisibility():
            displayable_node.GetDisplayNode().SetViewNodeIDs(view_nodes)
    
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
