from typing import Optional, TYPE_CHECKING, Dict
import qt
import slicer
from OpenLIFULib.util import display_errors, replace_widget
from OpenLIFULib.algorithm_input_widget import OpenLIFUAlgorithmInputWidget

if TYPE_CHECKING:
    from OpenLIFUData.OpenLIFUData import OpenLIFUDataLogic
    from OpenLIFUHome.OpenLIFUHome import OpenLIFUHomeLogic

def get_guided_mode_state() -> bool:
    """Get guided mode state from the OpenLIFU Home module's parameter node"""
    openlifu_home_parameter_node = slicer.util.getModuleLogic('OpenLIFUHome').getParameterNode()
    return openlifu_home_parameter_node.guided_mode

def set_guided_mode_state(new_guided_mode_state: bool):
    """Set guided mode state in OpenLIFU Home module's parameter node"""
    home_module_logic : OpenLIFUHomeLogic = slicer.util.getModuleLogic('OpenLIFUHome')
    openlifu_home_parameter_node = home_module_logic.getParameterNode()
    openlifu_home_parameter_node.guided_mode = new_guided_mode_state
    home_module_logic.workflow.update_all()

class WorkflowControls(qt.QWidget):
    """ Guided mode workflow controls widget

    The widget can be used whether in or out of guided mode, but the guardrails are active only when in guided mode.
    Guardrails here means that the "can_proceed" property controls whether the next button is enabled.

    Example usage to test it out in the Slicer python console:
        from OpenLIFULib.guided_mode_util import WorkflowControls
        workflow_controls = WorkflowControls(
            parent=None,
            previous_module_name = "OpenLIFUData",
            next_module_name = "OpenLIFUPrePlanning",
            include_session_controls = True,
        )
        workflow_controls.show()

        # then try things like this:
        workflow_controls.status_text = "Blah blah"
        workflow_controls.can_proceed = False
    """

    def __init__(
            self,
            parent:qt.QWidget,
            previous_module_name:Optional[str],
            next_module_name:Optional[str],
            include_session_controls:bool=False,
        ):
        """Guided mode controls QWidget

        Args:
            parent: Parent QWidget
            previous_module_name: Name of the slicer module that precedes the current one in the workflow. If None then there is no previous module,
                and in that case there will not be a back button.
            next_module_name: Name of the slicer module that is next in the workflow. If None then there is no next module, and in that case
                there will not be a next button and the "save and close" button will instead be labeled "Finish"
                (and will still have the effect of saving and closing). So if you set `next_module_name` to `None` then you probably
                want to enable `include_session_controls`.
            include_session_controls: Whether to include the buttons for saving and closing the session. The buttons do not set their enabled/disabled
                state based on whether there is a session or whether there is a database, so only include session controls if you know that
                in the guided workflow there will definitely be a database connection and an active session during the modules in the workflow
                that this widget is being added to.
        """
        super().__init__(parent)

        self._can_proceed:bool = True
        self._status_text:str = ""

        self.next_module_name = next_module_name
        self.previous_module_name = previous_module_name

        main_layout = qt.QVBoxLayout()
        self.setLayout(main_layout)

        main_group_box = qt.QGroupBox("Workflow Controls")
        main_group_box_layout = qt.QVBoxLayout()
        main_group_box.setLayout(main_group_box_layout)
        main_layout.addWidget(main_group_box)

        self.status_label = qt.QLabel("")
        main_group_box_layout.addWidget(self.status_label)

        button_row1_layout = qt.QHBoxLayout()
        main_group_box_layout.addLayout(button_row1_layout)

        if self.previous_module_name is not None:
            self.back_button = qt.QPushButton("Back")
            button_row1_layout.addWidget(self.back_button)
            self.back_button.clicked.connect(self.on_back)

        if self.next_module_name is not None:
            self.next_button = qt.QPushButton("Next")
            button_row1_layout.addWidget(self.next_button)
            self.next_button.clicked.connect(self.on_next)
            self.jump_ahead_button = qt.QPushButton(">>")
            button_row1_layout.addWidget(self.jump_ahead_button)
            self.jump_ahead_button.clicked.connect(self.on_jump_ahead)
            self.jump_ahead_button.setToolTip("Jump ahead to the nearest unfinished step of the workflow")


        if include_session_controls:
            button_row2_layout = qt.QHBoxLayout()
            self.save_button = qt.QPushButton("Save" if self.next_module_name is not None else "Finish")
            self.exit_button = qt.QPushButton("Exit")
            button_row2_layout.addWidget(self.save_button)
            button_row2_layout.addWidget(self.exit_button)
            main_group_box_layout.addLayout(button_row2_layout)
            self.save_button.clicked.connect(self.on_save)
            self.exit_button.clicked.connect(self.on_exit)
            self.save_button.setToolTip("Save the active session")
            self.exit_button.setToolTip("Close the active session")

        self.update()

    def update(self):
        self.update_status_label()
        self.update_back_button_enabledness()
        self.update_next_button()

    def on_next(self):
        slicer.util.selectModule(self.next_module_name)

    def on_back(self):
        slicer.util.selectModule(self.previous_module_name)
    
    def on_jump_ahead(self, clicked:bool):
        home_module_logic : OpenLIFUHomeLogic = slicer.util.getModuleLogic('OpenLIFUHome')
        home_module_logic.workflow_jump_ahead()

    @display_errors
    def on_save(self, clicked:bool):
        data_module_parameter_node = slicer.util.getModuleLogic('OpenLIFUData').getParameterNode()

        if data_module_parameter_node.loaded_session is None:
            slicer.util.errorDisplay("There is no loaded session.")
            return

        self.save_session()

    @display_errors
    def on_exit(self, clicked:bool):
        data_module_parameter_node = slicer.util.getModuleLogic('OpenLIFUData').getParameterNode()

        if data_module_parameter_node.loaded_session is None:
            slicer.util.errorDisplay("There is no loaded session.")
            return

        if slicer.util.confirmYesNoDisplay("Do you want to save your progress before exiting?", windowTitle="Save Confirmation"):
            self.close_session(save=True)
        else:
            self.close_session(save=False)

    def save_session(self):
        data_module_logic : OpenLIFUDataLogic = slicer.util.getModuleLogic('OpenLIFUData')
        data_module_logic.save_session()

    def close_session(self, save:bool):
        """Close the session, saving it or not depending on `save`"""
        data_module_logic : OpenLIFUDataLogic = slicer.util.getModuleLogic('OpenLIFUData')
        home_module_logic : OpenLIFUHomeLogic = slicer.util.getModuleLogic('OpenLIFUHome')
        if save:
            self.save_session()
        data_module_logic.clear_session(clean_up_scene=True)
        home_module_logic.workflow_go_to_start()

    def update_next_button(self):
        """Update next button enabledness and tooltip"""
        if not hasattr(self, "next_button"):
            return
        enabled = self.can_proceed or not get_guided_mode_state()
        self.next_button.setEnabled(enabled)
        if enabled:
            self.next_button.setToolTip(f"Go to the {self.next_module_name} module.")
        else:
            self.next_button.setToolTip(self.status_text)

    def update_back_button_enabledness(self):
        if not hasattr(self, "back_button"):
            return
        self.back_button.setEnabled(True)
        self.back_button.setToolTip(f"Go to the {self.previous_module_name} module.")

    def update_status_label(self):
        self.status_label.setText(self.status_text)
        self.update_next_button() # Ensures the tooltip gets updated

    @property
    def can_proceed(self) -> bool:
        """Whether the next step of the workflow should be available"""
        return self._can_proceed

    @can_proceed.setter
    def can_proceed(self, new_val : bool):
        self._can_proceed = new_val
        self.update_next_button()

    @property
    def status_text(self) -> str:
        """Status text explaining what the next step is"""
        return self._status_text

    @status_text.setter
    def status_text(self, new_val : str):
        self._status_text = new_val
        self.update_status_label()

class Workflow:
    """A class that holds the ordered dictionary of WorkflowControls widgets.
    The widgets are eventually still owned by (i.e. parented to) the module widget that contains them,
    assuming the modules use `GuidedModeMixin` appropriately to reparent the WorkflowControls into themselves.
    But this class provides a convenient way to construct the widgets and convenient access to the widgets.
    """

    modules = [
        "OpenLIFUDatabase",
        "OpenLIFULogin",
        "OpenLIFUData",
        "OpenLIFUPrePlanning",
        "OpenLIFUTransducerTracker",
        "OpenLIFUSonicationPlanner",
        "OpenLIFUSonicationControl",
    ]
    """Defines the order of the guided workflow."""

    def __init__(self):
        
        self.workflow_controls : Dict[str,WorkflowControls] = {}
        
        for previous_module, current_module, next_module in zip(
            [None] + self.modules,
            self.modules,
            self.modules[1:] + [None],
        ):
            self.workflow_controls[current_module] = WorkflowControls(
                parent = None, # The widget will be parented when it is injected into a module
                previous_module_name = previous_module,
                next_module_name = next_module,
                include_session_controls = current_module not in ["OpenLIFUDatabase", "OpenLIFULogin", "OpenLIFUData"],
            )

    def starting_module(self) -> str:
        """Get the name of the first module in the guided workflow."""
        return self.modules[0]

    def furthest_module_to_which_can_proceed(self) -> str:
        """Get the name of the furthest module along the workflow to which we `can_proceed`."""
        for module_name in self.modules:
            if not self.workflow_controls[module_name].can_proceed:
                return module_name
        return self.modules[-1]
    
    def update_all(self):
        for workflow_controls in self.workflow_controls.values():
            workflow_controls.update()

    def enforceGuidedModeVisibility(self, enforced: bool = False):

        # ---- Locate widgets of interest ----

        hide_in_guided_mode_widgets = []  # widgets with dynamic property
        call_enforce_in_guided_mode_widgets = []  # widgets with their own defined enforceGuidedModeVisibility()
        for moduleName in self.modules + ["OpenLIFUProtocolConfig"]:
            module = slicer.util.getModule(moduleName)
            widgetRepresentation = module.widgetRepresentation()
            all_widgets = slicer.util.findChildren(widgetRepresentation)
            for widget in all_widgets:
                if widget.property("slicer.openlifu.hide-in-guided-mode") is not None:
                    # A QVariant() is returned set to None if the widget does
                    # not have the property
                    hide_in_guided_mode_widgets.append(widget)
                elif isinstance(widget, OpenLIFUAlgorithmInputWidget):
                    # OpenLIFUAlgorithmInputWidget is an example of a widget implementing enforceGuidedModeVisibility()
                    call_enforce_in_guided_mode_widgets.append(widget)
            
        # ---- Enforce visibility of widgets / call function to enforce ----

        for widget in hide_in_guided_mode_widgets:
            widget.visible = not enforced

        for widget in call_enforce_in_guided_mode_widgets:
            widget.enforceGuidedModeVisibility(enforced)


class GuidedWorkflowMixin:
    """A mixin class to add guided mode workflow related methods to a ScriptedLoadableModuleWidget"""

    def inject_workflow_controls_into_placeholder(self):
        """Assuming the ScriptedLoadableModuleWidget UI has a widget named `workflowControlsPlaceholder`,
        replace it by the actual workflow controls widget tracked by the `Workflow` in the OpenLIFUHome module.
        An attribute self.workflow_controls can then be used to conveniently access the `WorkflowControls` widget.
        """
        home_module_logic : OpenLIFUHomeLogic = slicer.util.getModuleLogic('OpenLIFUHome')
        self.workflow_controls = home_module_logic.workflow.workflow_controls[self.moduleName]
        replace_widget(self.ui.workflowControlsPlaceholder, self.workflow_controls, self.ui)

