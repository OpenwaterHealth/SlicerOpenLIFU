import slicer
import qt
from typing import Tuple, List
from slicer import vtkMRMLViewNode, vtkMRMLModelNode
from OpenLIFULib.util import replace_widget
from OpenLIFULib import SlicerOpenLIFUPhotoscan

# Fixed pixel width for the wizard's left controls column. A fixed value keeps
# long photoscan/volume names in the pickers from stretching the dialog while
# leaving room for the fiducial table and the multi-control PV panel.
_WIZARD_LEFT_COLUMN_WIDTH = 520


def initialize_wizard_ui(wizard: qt.QWizard):

    root = qt.QHBoxLayout()
    root.setContentsMargins(0, 0, 0, 0)
    wizard.setLayout(root)
    ui_path = slicer.modules.OpenLIFUTransducerLocalizationWidget.resourcePath("UI/TransducerLocalizationWizard.ui")
    uiWidget = slicer.util.loadUI(ui_path)
    ui = slicer.util.childWidgetVariables(uiWidget)

    # Left column: page-specific controls (expand to fill), then the
    # Edit/Done button (lockPanel), then a stable slot for the proxy nav
    # buttons (see hook_wizard_nav_buttons). Making dialogControls vertically
    # Expanding pushes lockPanel + nav buttons to the bottom of the column and
    # gives the fiducial table the rest of the vertical space.
    ui.dialogControls.setSizePolicy(qt.QSizePolicy.Preferred, qt.QSizePolicy.Expanding)

    controlsColumn = qt.QWidget()
    controlsLayout = qt.QVBoxLayout(controlsColumn)
    controlsLayout.setContentsMargins(0, 0, 0, 0)
    controlsLayout.addWidget(ui.dialogControls, 1)
    controlsLayout.addWidget(ui.lockPanel)

    navLayout = qt.QHBoxLayout()
    navLayout.setContentsMargins(0, 0, 0, 0)
    controlsLayout.addLayout(navLayout)
    ui._wizardNavButtonLayout = navLayout

    controlsColumn.setFixedWidth(_WIZARD_LEFT_COLUMN_WIDTH)

    root.addWidget(controlsColumn)
    root.addWidget(ui.viewWidgetPlaceholder, 1)  # stretch=1 so the 3D view fills remaining space

    return ui


def hook_wizard_nav_buttons(wizard: qt.QWizard) -> None:
    """Put per-page Back/Next/Cancel/Approve buttons in the page's left-column
    nav slot. Call once after all pages have been added.

    Implementation notes: QWizard's internal Back/Next/Cancel/Finish buttons
    are left in place but hidden (zero-sized). Reparenting them into a page
    layout, or replacing the wizard's button layout via setButtonLayout, has
    crashed Slicer hard (no Python traceback) in PythonQt -- QWizard keeps
    internal pointers to those widgets and gets confused when their parent/
    layout changes. Instead, we add lightweight proxy QPushButtons that
    forward clicks to the originals, so all of QWizard's state machinery
    (enabled/disabled, finish-on-last-page, field validation, etc.) keeps
    working unmodified.
    """

    # Hide QWizard's default bottom buttons without touching its button layout
    # or reparenting them.
    _internal_button_ids = (
        qt.QWizard.BackButton,
        qt.QWizard.NextButton,
        qt.QWizard.FinishButton,
        qt.QWizard.CancelButton,
        qt.QWizard.CommitButton,
        qt.QWizard.HelpButton,
    )
    for bid in _internal_button_ids:
        btn = wizard.button(bid)
        if btn is None:
            continue
        btn.setVisible(False)
        btn.setMaximumHeight(0)
        btn.setMaximumWidth(0)

    # Build proxy buttons for each page once. Pages have already been added by
    # the caller, so wizard.pageIds() returns all of them.
    finish_text = wizard.buttonText(qt.QWizard.FinishButton) or "Finish"
    for page_id in wizard.pageIds():
        page = wizard.page(page_id)
        if page is None:
            continue
        ui = getattr(page, "ui", None)
        nav_layout = getattr(ui, "_wizardNavButtonLayout", None) if ui is not None else None
        if nav_layout is None:
            continue

        back_btn = qt.QPushButton("< Back")
        next_btn = qt.QPushButton("Next >")
        finish_btn = qt.QPushButton(finish_text)
        cancel_btn = qt.QPushButton("Cancel")

        back_btn.clicked.connect(lambda _checked=False, w=wizard: w.button(qt.QWizard.BackButton).click())
        next_btn.clicked.connect(lambda _checked=False, w=wizard: w.button(qt.QWizard.NextButton).click())
        finish_btn.clicked.connect(lambda _checked=False, w=wizard: w.button(qt.QWizard.FinishButton).click())
        cancel_btn.clicked.connect(lambda _checked=False, w=wizard: w.button(qt.QWizard.CancelButton).click())

        nav_layout.addWidget(back_btn)
        nav_layout.addStretch(1)
        nav_layout.addWidget(next_btn)
        nav_layout.addWidget(finish_btn)
        nav_layout.addWidget(cancel_btn)

        page._proxyBackButton = back_btn
        page._proxyNextButton = next_btn
        page._proxyFinishButton = finish_btn
        page._proxyCancelButton = cancel_btn

    start_id = wizard.startId

    def _sync_proxy_enabled():
        # Mirror each internal QWizard button's `enabled` state onto its proxy
        # on the current page. QWizard already toggles Next/Finish via the
        # page's isComplete(); we just reflect that into the visible proxy so
        # 'Approve' greys out while a page is being edited (unlocked).
        page = wizard.page(wizard.currentId)
        if page is None:
            return
        for bid, attr in (
            (qt.QWizard.BackButton,   "_proxyBackButton"),
            (qt.QWizard.NextButton,   "_proxyNextButton"),
            (qt.QWizard.FinishButton, "_proxyFinishButton"),
            (qt.QWizard.CancelButton, "_proxyCancelButton"),
        ):
            internal = wizard.button(bid)
            proxy = getattr(page, attr, None)
            if internal is None or proxy is None:
                continue
            proxy.setEnabled(internal.enabled)

    def _refresh(_id):
        page = wizard.page(_id)
        if page is None:
            return
        back_btn = getattr(page, "_proxyBackButton", None)
        next_btn = getattr(page, "_proxyNextButton", None)
        finish_btn = getattr(page, "_proxyFinishButton", None)
        is_final = page.nextId() == -1
        if back_btn is not None:
            back_btn.setVisible(_id != start_id)
        if next_btn is not None:
            next_btn.setVisible(not is_final)
        if finish_btn is not None:
            finish_btn.setVisible(is_final)
        _sync_proxy_enabled()

    wizard.currentIdChanged.connect(_refresh)
    # completeChanged is the signal each page emits when its isComplete()
    # answer flips; mirror to the proxy enabled states.
    for page_id in wizard.pageIds():
        p = wizard.page(page_id)
        if p is not None:
            p.completeChanged.connect(_sync_proxy_enabled)
    if start_id != -1:
        _refresh(start_id)


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

def reset_view_node_camera(view_node: vtkMRMLViewNode, axis_index: int = 3):
    """Reset the camera for the 3D view whose MRML view node matches ``view_node``.

    ``axis_index`` is forwarded to ``qMRMLThreeDView.rotateToViewAxis``: 0:-X,
    1:+X, 2:-Y, 3:+Y (RAS anterior, default), 4:-Z, 5:+Z. Callers showing a raw
    photoscan typically pass ``5`` so the head's face (which is captured in the
    +Z direction) is visible from the front by default.
    """

    layoutManager = slicer.app.layoutManager()
    for threeDViewIndex in range(layoutManager.threeDViewCount):
        view = layoutManager.threeDWidget(threeDViewIndex).threeDView()
        if view.mrmlViewNode().GetID() == view_node.GetID():
            specifiedViewIndex = threeDViewIndex
    
    threeDWidget = layoutManager.threeDWidget(specifiedViewIndex)
    threeDView = threeDWidget.threeDView() 
    threeDView.rotateToViewAxis(axis_index)
    threeDView.resetFocalPoint()  # reset the 3D view cube size and center it
    threeDView.resetCamera()  # reset camera zoom
