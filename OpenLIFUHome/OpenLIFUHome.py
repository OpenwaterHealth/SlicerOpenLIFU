# Standard library imports
from typing import Optional, TYPE_CHECKING

import os
# Third-party imports
import vtk

# Slicer imports
import slicer
from slicer.ScriptedLoadableModule import *
from slicer.i18n import tr as _
from slicer.i18n import translate
from slicer.parameterNodeWrapper import parameterNodeWrapper
from slicer.util import VTKObservationMixin

# OpenLIFULib imports
from OpenLIFULib import (
    get_openlifu_data_parameter_node,
)
from OpenLIFULib.guided_mode_util import set_guided_mode_state, Workflow
from OpenLIFULib.lazyimport import (
    check_and_install_python_requirements,
    python_requirements_exist,
)

#
# OpenLIFUHome
#

class OpenLIFUHome(ScriptedLoadableModule):
    """Uses ScriptedLoadableModule base class, available at:
    https://github.com/Slicer/Slicer/blob/main/Base/Python/slicer/ScriptedLoadableModule.py
    """

    def __init__(self, parent):
        ScriptedLoadableModule.__init__(self, parent)
        self.parent.title = _("OpenLIFU Home")
        self.parent.categories = [translate("qSlicerAbstractCoreModule", "OpenLIFU")]
        self.parent.dependencies = []  # add here list of module names that this module requires
        self.parent.contributors = ["Ebrahim Ebrahim (Kitware), Sadhana Ravikumar (Kitware), Peter Hollender (Openwater), Sam Horvath (Kitware), Brad Moore (Kitware)"]
        # short description of the module and a link to online module documentation
        # _() function marks text as translatable to other languages
        self.parent.helpText = _(
            "This is the home module of the OpenLIFU extension for focused ultrasound. "
            "More information at <a href=\"https://github.com/OpenwaterHealth/SlicerOpenLIFU\">github.com/OpenwaterHealth/SlicerOpenLIFU</a>."
        )
        # organization, grant, and thanks
        self.parent.acknowledgementText = _(
            "This is part of Openwater's OpenLIFU, an open-source "
            "hardware and software platform for Low Intensity Focused Ultrasound (LIFU) research "
            "and development."
        )

#
# OpenLIFUHomeParameterNode
#


@parameterNodeWrapper
class OpenLIFUHomeParameterNode:
    guided_mode : bool = False

#
# OpenLIFUHomeWidget
#


class OpenLIFUHomeWidget(ScriptedLoadableModuleWidget, VTKObservationMixin):
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

    def setup(self) -> None:
        """Called when the user opens the module the first time and the widget is initialized."""
        ScriptedLoadableModuleWidget.setup(self)

        # Load widget from .ui file (created by Qt Designer).
        # Additional widgets can be instantiated manually and added to self.layout.
        uiWidget = slicer.util.loadUI(self.resourcePath("UI/OpenLIFUHome.ui"))
        self.layout.addWidget(uiWidget)
        self.ui = slicer.util.childWidgetVariables(uiWidget)

        # Set scene in MRML widgets. Make sure that in Qt designer the top-level qMRMLWidget's
        # "mrmlSceneChanged(vtkMRMLScene*)" signal in is connected to each MRML widget's.
        # "setMRMLScene(vtkMRMLScene*)" slot.
        uiWidget.setMRMLScene(slicer.mrmlScene)

        # Create logic class. Logic implements all computations that should be possible to run
        # in batch mode, without a graphical user interface.
        self.logic = OpenLIFUHomeLogic()

        # === Connections and UI setup =======

        # These connections ensure that we update parameter node when scene is closed
        self.addObserver(slicer.mrmlScene, slicer.mrmlScene.StartCloseEvent, self.onSceneStartClose)
        self.addObserver(slicer.mrmlScene, slicer.mrmlScene.EndCloseEvent, self.onSceneEndClose)
        # Make sure parameter node is initialized (needed for module reload)
        self.initializeParameterNode()

        # Buttons
        self.ui.installPythonReqsButton.connect("clicked()", self.onInstallPythonRequirements)
        self.ui.guidedModePushButton.connect("clicked()", self.onGuidedModeClicked)
        self.updateInstallButtonText()
        self.updateGuidedModeButton()

        # Switch modules
        self.ui.databasePushButton.clicked.connect(lambda : self.switchModule(self.ui.databasePushButton.text))
        self.ui.loginPushButton.clicked.connect(lambda : self.switchModule(self.ui.loginPushButton.text))
        self.ui.dataPushButton.clicked.connect(lambda : self.switchModule(self.ui.dataPushButton.text))
        self.ui.prePlanningPushButton.clicked.connect(lambda : self.switchModule(self.ui.prePlanningPushButton.text))
        self.ui.sonicationControlPushButton.clicked.connect(lambda : self.switchModule(self.ui.sonicationControlPushButton.text))
        self.ui.sonicationPlanningPushButton.clicked.connect(lambda : self.switchModule(self.ui.sonicationPlanningPushButton.text))
        self.ui.transducerTrackingPushButton.clicked.connect(lambda : self.switchModule(self.ui.transducerTrackingPushButton.text))
        self.ui.protocolConfigPushButton.clicked.connect(lambda : self.switchModule(self.ui.protocolConfigPushButton.text))

    def switchModule(self, moduleButtonText: str) -> None:
        moduleButtonText = moduleButtonText.replace(" ", "")
        moduleButtonText = moduleButtonText.replace("-", "")

        # For certain modules, the module name in the GUI doesn't match the programmatic module name
        # This is due to max path character limits on longer module names
        if (moduleButtonText == "OpenLIFUSonicationPlanning"):
            moduleButtonText = moduleButtonText[:-3] + "er"

        if (moduleButtonText == "OpenLIFUProtocolConfiguration"):
            moduleButtonText = moduleButtonText[:-7]  # strip to -Config

        slicer.util.selectModule(moduleButtonText)


    def updateInstallButtonText(self) -> None:
        """Update the text of the install button based on whether it's 'install' or 'reinstall'"""
        if python_requirements_exist():
            self.ui.installPythonReqsButton.text = 'Reinstall Python Requirements'
        else:
            self.ui.installPythonReqsButton.text = 'Install Python Requirements'

    def onInstallPythonRequirements(self) -> None:
        """Install python requirements button action"""
        check_and_install_python_requirements(prompt_if_found=True)
        self.updateInstallButtonText()

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

    def setParameterNode(self, inputParameterNode: Optional[OpenLIFUHomeParameterNode]) -> None:
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

    def onGuidedModeClicked(self):
        new_guided_mode_state = not self._parameterNode.guided_mode
        if new_guided_mode_state:
            self.logic.start_guided_mode()
        else:
            set_guided_mode_state(new_guided_mode_state)

    def updateGuidedModeButton(self):
        if self._parameterNode is None:
            # This case occurs briefly when trying to close the slicer scene
            # It is an invalid state (e.g. guided mode is neither on nor off),
            # but it's momentary until the scene clean up is done.
            return
        if self._parameterNode.guided_mode:
            self.ui.guidedModePushButton.setText("Exit Guided Mode")
        else:
            self.ui.guidedModePushButton.setText("Start Guided Mode")
            self.ui.guidedModePushButton.setToolTip(
                    "Guided mode will take you step-by-step through the treatment workflow"
                )

    def onParameterNodeModified(self, caller, event) -> None:
        self.updateGuidedModeButton()
        if self._parameterNode is not None:
            self.logic.workflow.enforceGuidedModeVisibility(self._parameterNode.guided_mode)

        # Update whether transducer localization is enabled/disabled based on guided_mode state
        transducer_tracking_widget = slicer.util.getModule('OpenLIFUTransducerLocalization').widgetRepresentation()
        transducer_tracking_widget.self().checkCanRunTracking()


#
# OpenLIFUHomeLogic
#


class OpenLIFUHomeLogic(ScriptedLoadableModuleLogic):
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

        self.workflow = Workflow()


    def getParameterNode(self):
        return OpenLIFUHomeParameterNode(super().getParameterNode())

    def clear_session(self) -> None:
        self.current_session = None

    def start_guided_mode(self):
        set_guided_mode_state(True)
        self.workflow_go_to_start()

    def workflow_jump_ahead(self):
        """Jump ahead in the guided workflow to the furthest step for which `can_proceed` is True."""
        slicer.util.selectModule(self.workflow.furthest_module_to_which_can_proceed())

    def workflow_go_to_start(self):
        """Go to the starting module of the workflow"""
        slicer.util.selectModule(self.workflow.starting_module())

#
# OpenLIFUHomeTest
#


class OpenLIFUHomeTest(ScriptedLoadableModuleTest):
    """
    This is the test case for your scripted module.
    Uses ScriptedLoadableModuleTest base class, available at:
    https://github.com/Slicer/Slicer/blob/main/Base/Python/slicer/ScriptedLoadableModule.py
    """

    def setUp(self):
        """Do whatever is needed to reset the state - typically a scene clear will be enough."""
        slicer.mrmlScene.Clear()

    def _ensure_dvc_gdrive_support(self):
        
        import importlib.util

        # Check if dvc is installed with gdrive support
        dvc_installed = importlib.util.find_spec("dvc") is not None
        gdrive_installed = importlib.util.find_spec("pydrive2") is not None

        if not dvc_installed or not gdrive_installed:
            slicer.util.pip_install("dvc[gdrive]")

    def get_test_database(self):
        """
        Downloads the test database from Google Drive via DVC.
    
        Setup Requirements:
            - DVC with Google Drive support must be installed in the Slicer environment.
            - The path to a Service Account JSON key must be provided via the 
            DVC_GDRIVE_KEY_PATH CMake variable during configuration.
        
        Authentication Flow:
            At configuration time, CMake reads the key file's content into the 
            GDRIVE_CREDENTIALS_DATA environment variable. This allows DVC to 
            authenticate headlessly during runtime without requiring a manual browser login.
        """

        self._ensure_dvc_gdrive_support()
        import os
        from pathlib import Path
        from dvc.repo import Repo

        dvc_repo_path = os.environ.get('DVC_REPO_DIR')
        if not dvc_repo_path:
            raise EnvironmentError("DVC_REPO_DIR environment variable is not set." )
    
        dvc_repo_path = Path(dvc_repo_path)
        dvc_file = dvc_repo_path / 'db_dvc_slicertesting.dvc'
        dvc_config_file = dvc_repo_path / '.dvc' / 'config'
        
        assert dvc_config_file.exists() and dvc_file.exists(), f"DVC file not found at expected location: {dvc_file}"

        try: 
            import os
            creds = os.environ.get('GDRIVE_CREDENTIALS_DATA')
            if not creds:
                raise EnvironmentError("GDRIVE_CREDENTIALS_DATA environment variable is not set." \
                " DVC cannot authenticate with Google Drive.")
            # Point to directory containing .dvc files
            # unitialized=True allows working in a directory that is not a git repo
            repo = Repo(str(dvc_repo_path), uninitialized=True)
            repo.pull(targets=[str(dvc_file)])
        except Exception as e:
            print(f"An error occurred during dvc pull: {e}")
        
        return str(dvc_repo_path / 'db_dvc_slicertesting')
    
    def runTest(self):
        """Run as few or as many tests as needed here."""
        
        from OpenLIFULib.lazyimport import python_requirements_exist, install_python_requirements
        
        if not python_requirements_exist():
            install_python_requirements()

        self.setUp()
        
        # Download test database using dvc
        db_path = self.get_test_database()

        self._OpenLIFU_FullTest1(db_path = db_path)
            
    def _OpenLIFU_FullTest1(self, db_path:str) -> None:

        from OpenLIFUDatabase import OpenLIFUDatabaseTest
        dbt = OpenLIFUDatabaseTest()
        dbt.connect_database(database_dir = db_path)

        from OpenLIFUData import OpenLIFUDataTest
        dt = OpenLIFUDataTest()
        dt.load_subject_session()

        from OpenLIFUPrePlanning import OpenLIFUPrePlanningTest
        pt = OpenLIFUPrePlanningTest()
        pt._workflow_virtual_fit()

        from OpenLIFUTransducerLocalization import OpenLIFUTransducerLocalizationTest
        tlt = OpenLIFUTransducerLocalizationTest()
        tlt._workflow_localization()

        from OpenLIFUSonicationPlanner import OpenLIFUSonicationPlannerTest
        spt = OpenLIFUSonicationPlannerTest()
        spt._workflow_planning()

        from OpenLIFUSonicationControl import OpenLIFUSonicationControlTest
        sct = OpenLIFUSonicationControlTest()
        sct._workflow_sonication_control()



        

