from typing import Optional, Union, TYPE_CHECKING, Tuple, get_origin, get_args
import warnings
from dataclasses import fields

import qt
import vtk

import slicer
from slicer.i18n import tr as _
from slicer.i18n import translate
from slicer.ScriptedLoadableModule import *
from slicer.util import VTKObservationMixin
from slicer.parameterNodeWrapper import parameterNodeWrapper
from slicer import vtkMRMLScalarVolumeNode,vtkMRMLMarkupsFiducialNode

from OpenLIFULib import (
    SlicerOpenLIFUProtocol,
    SlicerOpenLIFUTransducer,
    SlicerOpenLIFUSolution,
    fiducial_to_openlifu_point_in_transducer_coords,
    make_xarray_in_transducer_coords_from_volume,
    get_openlifu_data_parameter_node,
    BusyCursor,
    OpenLIFUAlgorithmInputWidget,
    SlicerOpenLIFUSolutionAnalysis,
)
from OpenLIFULib.util import replace_widget, create_noneditable_QStandardItem

if TYPE_CHECKING:
    import openlifu # This import is deferred at runtime using openlifu_lz, but it is done here for IDE and static analysis purposes
    import openlifu.plan
    import xarray
    from OpenLIFUData.OpenLIFUData import OpenLIFUDataLogic

#
# OpenLIFUSonicationPlanner
#


class OpenLIFUSonicationPlanner(ScriptedLoadableModule):
    """Uses ScriptedLoadableModule base class, available at:
    https://github.com/Slicer/Slicer/blob/main/Base/Python/slicer/ScriptedLoadableModule.py
    """

    def __init__(self, parent):
        ScriptedLoadableModule.__init__(self, parent)
        self.parent.title = _("OpenLIFU Sonication Planning")
        self.parent.categories = [translate("qSlicerAbstractCoreModule", "OpenLIFU.OpenLIFU Modules")]
        self.parent.dependencies = []  # add here list of module names that this module requires
        self.parent.contributors = ["Ebrahim Ebrahim (Kitware), Sadhana Ravikumar (Kitware), Peter Hollender (Openwater), Sam Horvath (Kitware), Brad Moore (Kitware)"]
        # short description of the module and a link to online module documentation
        # _() function marks text as translatable to other languages
        self.parent.helpText = _(
            "This is the sonication module of the OpenLIFU extension for focused ultrasound. "
            "More information at <a href=\"https://github.com/OpenwaterHealth/SlicerOpenLIFU\">github.com/OpenwaterHealth/SlicerOpenLIFU</a>."
        )
        # organization, grant, and thanks
        self.parent.acknowledgementText = _(
            "This is part of Openwater's OpenLIFU, an open-source "
            "hardware and software platform for Low Intensity Focused Ultrasound (LIFU) research "
            "and development."
        )



#
# OpenLIFUSonicationPlannerParameterNode
#


@parameterNodeWrapper
class OpenLIFUSonicationPlannerParameterNode:
    solution_analysis : Optional[SlicerOpenLIFUSolutionAnalysis] = None

#
# OpenLIFUSonicationPlannerWidget
#


class OpenLIFUSonicationPlannerWidget(ScriptedLoadableModuleWidget, VTKObservationMixin):
    """Uses ScriptedLoadableModuleWidget base class, available at:
    https://github.com/Slicer/Slicer/blob/main/Base/Python/slicer/ScriptedLoadableModule.py
    """

    def __init__(self, parent=None) -> None:
        """Called when the user opens the module the first time and the widget is initialized."""
        ScriptedLoadableModuleWidget.__init__(self, parent)
        VTKObservationMixin.__init__(self)  # needed for parameter node observation
        self.logic = None
        self._parameterNode = None
        self._parameterNodeGuiTag = None

        self._updating_solution_analysis = False
        """Flag to help prevent recursive event when onParameterNodeModified causes the parameter node to be modified"""

    def setup(self) -> None:
        """Called when the user opens the module the first time and the widget is initialized."""
        ScriptedLoadableModuleWidget.setup(self)

        # Load widget from .ui file (created by Qt Designer).
        # Additional widgets can be instantiated manually and added to self.layout.
        uiWidget = slicer.util.loadUI(self.resourcePath("UI/OpenLIFUSonicationPlanner.ui"))
        self.layout.addWidget(uiWidget)
        self.ui = slicer.util.childWidgetVariables(uiWidget)

        # Set scene in MRML widgets. Make sure that in Qt designer the top-level qMRMLWidget's
        # "mrmlSceneChanged(vtkMRMLScene*)" signal in is connected to each MRML widget's.
        # "setMRMLScene(vtkMRMLScene*)" slot.
        uiWidget.setMRMLScene(slicer.mrmlScene)

        # Create logic class. Logic implements all computations that should be possible to run
        # in batch mode, without a graphical user interface.
        self.logic = OpenLIFUSonicationPlannerLogic()

        # Create and set solution analysis table models
        self.focusAnalysisTableModel = qt.QStandardItemModel() # analysis metrics that are per focus point
        self.globalAnalysisTableModel = qt.QStandardItemModel() # analysis metrics that are for the whole solution, i.e. over all focus points
        self.ui.focusAnalysisTableView.setModel(self.focusAnalysisTableModel)
        self.ui.globalAnalysisTableView.setModel(self.globalAnalysisTableModel)

        # Connections

        # These connections ensure that we update parameter node when scene is closed
        self.addObserver(slicer.mrmlScene, slicer.mrmlScene.StartCloseEvent, self.onSceneStartClose)
        self.addObserver(slicer.mrmlScene, slicer.mrmlScene.EndCloseEvent, self.onSceneEndClose)

        # Replace the placeholder algorithm input widget by the actual one
        algorithm_input_names = ["Protocol", "Transducer", "Volume", "Target"]
        self.algorithm_input_widget = OpenLIFUAlgorithmInputWidget(algorithm_input_names, parent = self.ui.algorithmInputWidgetPlaceholder.parentWidget())
        replace_widget(self.ui.algorithmInputWidgetPlaceholder, self.algorithm_input_widget, self.ui)

        # Initialize UI
        self.updateInputOptions()
        self.updateSolutionProgressBar()
        self.updateRenderPNPCheckBox()
        self.updateVirtualFitApprovalStatus()
        self.updateTrackingApprovalStatus()
        self.updateSolutionAnalysis()

        # Add observers on the Data module's parameter node and this module's own parameter node
        self.addObserver(get_openlifu_data_parameter_node().parameterNode, vtk.vtkCommand.ModifiedEvent, self.onDataParameterNodeModified)
        
        # This ensures we update the drop down options in the volume and fiducial combo boxes when nodes are added/removed
        self.addObserver(slicer.mrmlScene, slicer.vtkMRMLScene.NodeAddedEvent, self.onNodeAdded)
        self.addObserver(slicer.mrmlScene, slicer.vtkMRMLScene.NodeRemovedEvent, self.onNodeRemoved)


        self.ui.solutionPushButton.clicked.connect(self.onComputeSolutionClicked)
        self.ui.renderPNPCheckBox.clicked.connect(self.onrenderPNPCheckBoxClicked)
        self.ui.approveButton.clicked.connect(self.onApproveClicked)

        self.checkCanComputeSolution()
        self.updateApproveButton()

        # Make sure parameter node is initialized (needed for module reload)
        self.initializeParameterNode()


    def cleanup(self) -> None:
        """Called when the application closes and the module widget is destroyed."""
        self.removeObservers()

    def enter(self) -> None:
        """Called each time the user opens this module."""
        # Make sure parameter node exists and observed
        self.initializeParameterNode()

    def exit(self) -> None:
        """Called each time the user opens a different module."""
        # Do not react to parameter node changes (GUI will be updated when the user enters into the module)
        if self._parameterNode:
            self._parameterNode.disconnectGui(self._parameterNodeGuiTag)
            self._parameterNodeGuiTag = None

    def onSceneStartClose(self, caller, event) -> None:
        """Called just before the scene is closed."""
        # Parameter node will be reset, do not use it anymore
        self.setParameterNode(None)

    def onSceneEndClose(self, caller, event) -> None:
        """Called just after the scene is closed."""
        # If this module is shown while the scene is closed then recreate a new parameter node immediately
        if self.parent.isEntered:
            self.initializeParameterNode()

    def initializeParameterNode(self) -> None:
        """Ensure parameter node exists and observed."""
        # Parameter node stores all user choices in parameter values, node selections, etc.
        # so that when the scene is saved and reloaded, these settings are restored.

        self.setParameterNode(self.logic.getParameterNode())

    def setParameterNode(self, inputParameterNode: Optional[OpenLIFUSonicationPlannerParameterNode]) -> None:
        """
        Set and observe parameter node.
        Observation is needed because when the parameter node is changed then the GUI must be updated immediately.
        """

        if self._parameterNode:
            self._parameterNode.disconnectGui(self._parameterNodeGuiTag)

        self._parameterNode = inputParameterNode
        if self._parameterNode:
            # Note: in the .ui file, a Qt dynamic property called "SlicerParameterName" is set on each
            # ui element that needs connection.
            self._parameterNodeGuiTag = self._parameterNode.connectGui(self.ui)
            self.addObserver(self._parameterNode, vtk.vtkCommand.ModifiedEvent, self.onParameterNodeModified)

    def checkCanComputeSolution(self, caller = None, event = None) -> None:

        # If all the needed objects/nodes are loaded within the Slicer scene, all of the combo boxes will have valid data selected
        # This means that the compute solution button can be enabled
        if self.algorithm_input_widget.has_valid_selections():
            self.ui.solutionPushButton.enabled = True
            self.ui.solutionPushButton.setToolTip("Compute a sonication solution for the target under this protocol and subject-transducer scene")
        else:
            self.ui.solutionPushButton.enabled = False
            self.ui.solutionPushButton.setToolTip("Please specify the required inputs")

    @vtk.calldata_type(vtk.VTK_OBJECT)
    def onNodeRemoved(self, caller, event, node : slicer.vtkMRMLNode) -> None:
        """ Update volume and target combo boxes when nodes are added to the scene"""
        if node.IsA('vtkMRMLMarkupsFiducialNode'):
            with warnings.catch_warnings():
                warnings.simplefilter("ignore") # if the observer doesn't exist, then no problem we don't need to see the warning.
                self.unwatch_fiducial_node(node)
        self.updateInputOptions()

    @vtk.calldata_type(vtk.VTK_OBJECT)
    def onNodeAdded(self, caller, event, node : slicer.vtkMRMLNode) -> None:
        """ Update volume and target combo boxes when nodes are removed from the scene"""
        if node.IsA('vtkMRMLMarkupsFiducialNode'):
            self.watch_fiducial_node(node)
        self.updateInputOptions()

    def updateInputOptions(self):
        """Update the comboboxes, forcing some of them to take values derived from the active session if there is one"""
        self.algorithm_input_widget.update()

        # Determine whether solution can be computed based on the status of combo boxes
        self.checkCanComputeSolution()

    def updateSolutionProgressBar(self):
        """Update the solution progress bar. 0% if there is no existing solution, 100% if there is an existing solution."""
        self.ui.solutionProgressBar.maximum = 1 # (during computation we set maxmimum=0 to put it into an infinite loading animation)

        if get_openlifu_data_parameter_node().loaded_solution is None:
            self.ui.solutionProgressBar.value = 0
        else:
            self.ui.solutionProgressBar.value = 1

    def updateRenderPNPCheckBox(self):
        if get_openlifu_data_parameter_node().loaded_solution is None:
            self.ui.renderPNPCheckBox.enabled = False
            self.ui.renderPNPCheckBox.checked = False
            self.ui.renderPNPCheckBox.setToolTip("Compute a solution first to generate a PNP volume that can be visualized")
        else:
            self.ui.renderPNPCheckBox.enabled = True
            self.ui.renderPNPCheckBox.setToolTip("Show the PNP volume in the 3D view with maximum intensity projection")


    def onDataParameterNodeModified(self,caller, event) -> None:
        self.updateInputOptions()
        self.updateSolutionProgressBar()
        self.updateRenderPNPCheckBox()
        self.updateVirtualFitApprovalStatus()
        self.updateTrackingApprovalStatus()
        self.updateApproveButton()

        if get_openlifu_data_parameter_node().loaded_solution is None:
            self.logic.getParameterNode().solution_analysis = None

    def watch_fiducial_node(self, node:vtkMRMLMarkupsFiducialNode):
        """Add observers so that point-list changes in this fiducial node are tracked by the module."""
        self.addObserver(node,slicer.vtkMRMLMarkupsNode.PointAddedEvent,self.onPointAddedOrRemoved)
        self.addObserver(node,slicer.vtkMRMLMarkupsNode.PointRemovedEvent,self.onPointAddedOrRemoved)

    def unwatch_fiducial_node(self, node:vtkMRMLMarkupsFiducialNode):
        """Un-does watch_fiducial_node; see watch_fiducial_node."""
        self.removeObserver(node,slicer.vtkMRMLMarkupsNode.PointAddedEvent,self.onPointAddedOrRemoved)
        self.removeObserver(node,slicer.vtkMRMLMarkupsNode.PointRemovedEvent,self.onPointAddedOrRemoved)

    def onPointAddedOrRemoved(self, caller, event):
        self.updateInputOptions()

    def onComputeSolutionClicked(self):
        activeData = self.algorithm_input_widget.get_current_data()

        # In case a PNP was previously being displayed, hide it since it is about to no longer belong to the active solution.
        self.ui.renderPNPCheckBox.checked = False
        self.logic.hide_pnp()

        with BusyCursor():
            try:
                self.ui.solutionProgressBar.maximum = 0
                slicer.app.processEvents()
                self.logic.computeSolution(activeData["Volume"], activeData["Target"],
                                           activeData["Transducer"], activeData["Protocol"])
            finally:
                self.updateSolutionProgressBar()

    def onrenderPNPCheckBoxClicked(self, checked:bool):
        if checked:
            self.logic.render_pnp()
        else:
            self.logic.hide_pnp()

    def updateVirtualFitApprovalStatus(self) -> None:
        data_logic : "OpenLIFUDataLogic" = slicer.util.getModuleLogic('OpenLIFUData')
        if data_logic.validate_session():
            target_id = data_logic.get_virtual_fit_approval_state()
            if target_id is None:
                self.ui.virtualFitApprovalStatusLabel.text = ""
            else:
                self.ui.virtualFitApprovalStatusLabel.text = f"(Virtual fit was approved for target \"{target_id}\")"
        else:
            self.ui.virtualFitApprovalStatusLabel.text = ""

    def updateTrackingApprovalStatus(self) -> None:
        loaded_session = get_openlifu_data_parameter_node().loaded_session
        if loaded_session is not None:
            if loaded_session.transducer_tracking_is_approved():
                self.ui.trackingApprovalStatusLabel.text = f"(Transducer tracking is approved)"
                self.ui.trackingApprovalStatusLabel.styleSheet = ""
            else:
                self.ui.trackingApprovalStatusLabel.text = f"WARNING: Transducer tracking is currently unapproved!"
                self.ui.trackingApprovalStatusLabel.styleSheet = "color:red;"
        else:
            self.ui.virtualFitApprovalStatusLabel.text = ""

    def updateApproveButton(self):
        data_parameter_node = get_openlifu_data_parameter_node()
        if data_parameter_node.loaded_solution is None:
            self.ui.approveButton.setEnabled(False)
            self.ui.approveButton.setToolTip("There is no active solution to write the approval")
            self.ui.approveButton.setText("Approve solution")
        else:
            self.ui.approveButton.setEnabled(True)
            if data_parameter_node.loaded_solution.is_approved():
                self.ui.approveButton.setText("Unapprove solution")
                self.ui.approveButton.setToolTip(
                    "Revoke approval for the sonication solution"
                )
            else:
                self.ui.approveButton.setText("Approve solution")
                self.ui.approveButton.setToolTip(
                    "Approve the sonicaiton solution"
                )

    def onApproveClicked(self):
        with BusyCursor():
            self.logic.toggle_solution_approval()

    def onParameterNodeModified(self, caller, event) -> None:
        if not self._updating_solution_analysis: # prevent recursive observer event
            self._updating_solution_analysis = True
            self.updateSolutionAnalysis()
            self._updating_solution_analysis = False

    def updateSolutionAnalysis(self) -> None:
        """Update the solution analysis widgets"""

        data_parameter_node = get_openlifu_data_parameter_node()
        solution = data_parameter_node.loaded_solution

        if solution is None:
            self.clear_solution_analysis_tables() # clear out the table
            self.ui.analysisStackedWidget.setCurrentIndex(0) # set the page to "no solution"
            return

        analysis = self._parameterNode.solution_analysis

        if analysis is None: # There exists a solution but no solution analysis (we don't want this to be possible but with manual workflow it might be)
            slicer.util.warningDisplay(
                "There is a solution, but no associated solution analysis. The analysis will be computed now.",
                "Missing analysis",
            )
            analysis = self.logic.compute_analysis_from_solution(solution)
            if analysis is None: # This could happen for example if the user deletes the transducer from the scene after computing the solution
                slicer.util.errorDisplay(
                    "Could not compute analysis because OpenLIFU objects that were used to generate the solution are missing.",
                    "Cannot compute analysis",
                )
                self.clear_solution_analysis_tables()
                self.ui.analysisStackedWidget.setCurrentIndex(2) # set the page to show that this is an error state
                return
            self._parameterNode.solution_analysis = analysis

        self.populate_solution_analysis_table()
        self.ui.analysisStackedWidget.setCurrentIndex(1) # set the page to analysis

    def clear_solution_analysis_tables(self) -> None:
        """Clear out the solution analysis tables, removing all rows and column headers"""
        self.focusAnalysisTableModel.removeRows(0,self.focusAnalysisTableModel.rowCount())
        self.focusAnalysisTableModel.setColumnCount(0)
        self.globalAnalysisTableModel.removeRows(0,self.globalAnalysisTableModel.rowCount())
        self.globalAnalysisTableModel.setColumnCount(0)

    def populate_solution_analysis_table(self) -> None:
        """Fill the solution analysis table models with the information from the current solution analysis.
        Assumes that there is a valid solution analysis, raises error if not.
        """
        analysis = self._parameterNode.solution_analysis
        if analysis is None:
            raise RuntimeError("Cannot populate solution analysis tables because there is no solution analysis.")
        analysis_openlifu = analysis.analysis

        self.clear_solution_analysis_tables()

        # Max length of list type fields in the dataclass
        max_len = max(
            len(getattr(analysis_openlifu, f.name))
            for f in fields(analysis_openlifu)
            if get_origin(f.type) is list
        )

        self.focusAnalysisTableModel.setHorizontalHeaderLabels(['Metric'] + [f"Focus {i+1}" for  i in range(max_len)])
        self.globalAnalysisTableModel.setHorizontalHeaderLabels(['Metric', 'Value'])
        self.ui.focusAnalysisTableView.setColumnWidth(0, 200) # widen the metrcs column
        self.ui.globalAnalysisTableView.setColumnWidth(0, 200) # widen the metrcs column

        for field in fields(analysis_openlifu):

            # we expect field.type could be either "List[float]" or "Optional[float]" which is actually "Union[float,NoneType]"
            # here `origin`` would be the "List" or "Union" part
            # and `args`` would be the "float" or "None" part
            origin = get_origin(field.type)
            args = get_args(field.type)

            # lists of floats go into the focusAnalysisTableModel
            if origin is list and args == (float,):
                values = getattr(analysis_openlifu,field.name)
                value_strs = [
                    str(values[i]) if i<len(values) else ""
                    for i in range(max_len)
                ]
                self.focusAnalysisTableModel.appendRow(list(map(
                    create_noneditable_QStandardItem,
                    [field.name, *value_strs]
                )))

            # individual floats go into the globalAnalysisTableModel
            elif origin is Union and len(args)==2 and float in args and type(None) in args:
                value = getattr(analysis_openlifu,field.name)
                value_str = str(value) if value is not None else ""
                self.globalAnalysisTableModel.appendRow(list(map(
                    create_noneditable_QStandardItem,
                    [field.name, value_str]
                )))

            else:
                raise RuntimeError(f"Not sure what to do with the SolutionAnalysis field {field.name}")




#
# Solution computation function using openlifu
#

def compute_solution_openlifu(
        protocol: "openlifu.Protocol",
        transducer:SlicerOpenLIFUTransducer,
        target_node:vtkMRMLMarkupsFiducialNode,
        volume_node:vtkMRMLScalarVolumeNode
    ) -> "Tuple[openlifu.Solution, xarray.DataArray, xarray.DataArray, openlifu.plan.SolutionAnalysis]":
    """Run openlifu beamforming and k-wave simulation

    Returns:
        solution: the generated openlifu Solution
        pnp_aggregated: Peak negative pressure volume, a simulation output. This is max-aggregated over all focus points.
        intensity_aggregated: Time-averaged intensity, a simulation output. This is mean-aggregated over all focus points.
            Note: It should be weighted by the number of times each focus point is focused on, but this functionality is not yet represented by openlifu.
    """
    session = get_openlifu_data_parameter_node().loaded_session
    solution, simulation_result_aggregated, scaled_solution_analysis = protocol.calc_solution(
        transducer=transducer.transducer.transducer,
        volume=make_xarray_in_transducer_coords_from_volume(volume_node, transducer, protocol),
        target=fiducial_to_openlifu_point_in_transducer_coords(target_node, transducer, name = 'sonication target'),
        session=session.session.session if session is not None else None,
    )
    return solution, simulation_result_aggregated["p_min"], simulation_result_aggregated["ita"], scaled_solution_analysis


#
# OpenLIFUSonicationPlannerLogic
#


class OpenLIFUSonicationPlannerLogic(ScriptedLoadableModuleLogic):
    """This class should implement all the actual
    computation done by your module.  The interface
    should be such that other python code can import
    this class and make use of the functionality without
    requiring an instance of the Widget.
    Uses ScriptedLoadableModuleLogic base class, available at:
    https://github.com/Slicer/Slicer/blob/main/Base/Python/slicer/ScriptedLoadableModule.py
    """

    def __init__(self) -> None:
        """Called when the logic class is instantiated. Can be used for initializing member variables."""
        ScriptedLoadableModuleLogic.__init__(self)

    def getParameterNode(self):
        return OpenLIFUSonicationPlannerParameterNode(super().getParameterNode())

    def computeSolution(
            self,
            inputVolume: vtkMRMLScalarVolumeNode,
            inputTarget: vtkMRMLMarkupsFiducialNode,
            inputTransducer : SlicerOpenLIFUTransducer,
            inputProtocol: SlicerOpenLIFUProtocol) -> Tuple[SlicerOpenLIFUSolution, SlicerOpenLIFUSolutionAnalysis]:
        """Compute solution for the given volume, target, transducer, and protocol, setting the solution as the active solution.
        Note that setting the solution will trigger a write of the solution to the databse if there is an active session.
        """
        solution_openlifu, pnp_aggregated, intensity_aggregated, analysis_openlifu = compute_solution_openlifu(
            inputProtocol.protocol,
            inputTransducer,
            inputTarget,
            inputVolume,
        )
        solution = SlicerOpenLIFUSolution.initialize_from_openlifu_data(
            solution = solution_openlifu,
            pnp_datarray=pnp_aggregated,
            intensity_dataarray=intensity_aggregated,
            transducer=inputTransducer,
        )
        analysis = SlicerOpenLIFUSolutionAnalysis(analysis_openlifu)
        slicer.util.getModuleLogic('OpenLIFUData').set_solution(solution)
        self.getParameterNode().solution_analysis = analysis
        return solution, analysis

    def get_pnp(self) -> Optional[vtkMRMLScalarVolumeNode]:
        """Get the PNP volume of the active solution, if there is an active solution. Return None if there isn't."""
        solution : SlicerOpenLIFUSolution = get_openlifu_data_parameter_node().loaded_solution
        if solution is None:
            return None
        return solution.pnp

    def render_pnp(self) -> None:
        pnp = self.get_pnp()
        if pnp is None:
            raise RuntimeError("Cannot render PNP as there is no active solution.")
        pnp.GetDisplayNode().SetAndObserveColorNodeID("vtkMRMLColorTableNodeFilePlasma.txt")
        volRenLogic = slicer.modules.volumerendering.logic()
        displayNode = volRenLogic.GetFirstVolumeRenderingDisplayNode(pnp)
        if not displayNode:
            displayNode = volRenLogic.CreateDefaultVolumeRenderingNodes(pnp)
        volRenLogic.CopyDisplayToVolumeRenderingDisplayNode(displayNode)
        for view_node in slicer.util.getNodesByClass("vtkMRMLViewNode"):
            view_node.SetRaycastTechnique(slicer.vtkMRMLViewNode.MaximumIntensityProjection)
        displayNode.SetVisibility(True)
        scalar_opacity_mapping = displayNode.GetVolumePropertyNode().GetVolumeProperty().GetScalarOpacity()
        scalar_opacity_mapping.RemoveAllPoints()
        vmin, vmax = pnp.GetImageData().GetScalarRange()
        scalar_opacity_mapping.AddPoint(vmin,0.0)
        scalar_opacity_mapping.AddPoint(vmax,1.0)

    def hide_pnp(self) -> None:
        """Hide the PNP volume from the 3D view, if it is displayed. If there is no PNP volume then just do nothing."""
        pnp = self.get_pnp()
        if pnp is None:
            return
        volRenLogic = slicer.modules.volumerendering.logic()
        displayNode = volRenLogic.GetFirstVolumeRenderingDisplayNode(pnp)
        if not displayNode:
            displayNode = volRenLogic.CreateDefaultVolumeRenderingNodes(pnp)
        displayNode.SetVisibility(False)

    def toggle_solution_approval(self):
        """Approve the currently active solution if it was not approved. Revoke approval if it was approved.
        This will write the approval to the solution in memory and, if there is an active session from which
        the active solution was generated, then it will also write the solution approval to the database.
        """
        slicer.util.getModuleLogic('OpenLIFUData').toggle_solution_approval()

    def compute_analysis_from_solution(self, solution:SlicerOpenLIFUSolution) -> Optional[SlicerOpenLIFUSolutionAnalysis]:
        """Compute solution analysis from a given solution.
        Returns the SlicerOpenLIFUSolutionAnalysis on success.
        If the protocol or transducer used to compute the solution are not present, then this returns None.
        """
        solution_openlifu = solution.solution.solution
        data_parameter_node = get_openlifu_data_parameter_node()
        if (
            solution_openlifu.transducer_id not in data_parameter_node.loaded_transducers
            or solution_openlifu.protocol_id not in data_parameter_node.loaded_protocols
        ):
            return None
        transducer = data_parameter_node.loaded_transducers[solution_openlifu.transducer_id]
        protocol = data_parameter_node.loaded_protocols[solution_openlifu.protocol_id]
        analysis_openlifu = solution_openlifu.analyze(
            transducer=transducer.transducer.transducer,
            options=protocol.protocol.analysis_options
        )
        return SlicerOpenLIFUSolutionAnalysis(analysis_openlifu)


#
# OpenLIFUSonicationPlannerTest
#

class OpenLIFUSonicationPlannerTest(ScriptedLoadableModuleTest):
    """
    This is the test case for your scripted module.
    Uses ScriptedLoadableModuleTest base class, available at:
    https://github.com/Slicer/Slicer/blob/main/Base/Python/slicer/ScriptedLoadableModule.py
    """

    def setUp(self):
        """Do whatever is needed to reset the state - typically a scene clear will be enough."""
        slicer.mrmlScene.Clear()

    def runTest(self):
        """Run as few or as many tests as needed here."""
        self.setUp()
