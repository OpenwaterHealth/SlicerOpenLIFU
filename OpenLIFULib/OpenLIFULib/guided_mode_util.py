from typing import Callable, Optional, TYPE_CHECKING, Dict
import qt
import slicer
from OpenLIFULib.util import display_errors, replace_widget
from OpenLIFULib.algorithm_input_widget import OpenLIFUAlgorithmInputWidget

if TYPE_CHECKING:
    import openlifu
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

def confirm_exit_session_dialog(
    message: str = "Save your progress before exiting the session?",
    window_title: str = "Exit Session",
) -> str:
    """Show a Save & Exit / Discard & Exit / Cancel dialog for exiting a session.

    Returns one of:
        "save":    user chose to save and exit
        "discard": user chose to discard changes and exit
        "cancel":  user cancelled; caller should treat as no-op
    """
    mb = qt.QMessageBox()
    mb.setIcon(qt.QMessageBox.Question)
    mb.setWindowTitle(window_title)
    mb.setText(message)
    save_btn = mb.addButton("Save && Exit", qt.QMessageBox.AcceptRole)
    discard_btn = mb.addButton("Discard && Exit", qt.QMessageBox.DestructiveRole)
    cancel_btn = mb.addButton("Cancel", qt.QMessageBox.RejectRole)
    mb.setDefaultButton(cancel_btn)
    mb.exec_()
    clicked = mb.clickedButton()
    if clicked is save_btn:
        return "save"
    if clicked is discard_btn:
        return "discard"
    return "cancel"

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
            enforce_can_proceed_always:bool=False,
            on_back_override:Optional[Callable[[], None]]=None,
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
            enforce_can_proceed_always: When True, the Next button is gated on ``can_proceed`` even when the user is
                not in guided mode. Useful for off-workflow footers (e.g. the Data Manager) where the precondition
                ("a session must be loaded before navigating into the workflow") is real regardless of guided mode.
            on_back_override: Optional callable invoked when Back is clicked instead of the default
                ``selectModule(previous_module_name)``. Lets host modules run cleanup (e.g. close the
                active session) before navigating away.
        """
        super().__init__(parent)

        self._can_proceed:bool = True
        self._status_text:str = ""
        self._enforce_can_proceed_always:bool = bool(enforce_can_proceed_always)
        self._on_back_override:Optional[Callable[[], None]] = on_back_override

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

        # Add back button

        self.back_button = qt.QPushButton("Back")
        button_row1_layout.addWidget(self.back_button)
        self.back_button.clicked.connect(self.on_back)

        if self.previous_module_name is None:
            self.back_button.enabled = False

        # Add forward button

        self.next_button = qt.QPushButton("Next")
        button_row1_layout.addWidget(self.next_button)
        self.next_button.clicked.connect(self.on_next)

        if self.next_module_name is None:
            self.next_button.enabled = False

        # Add session controls

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
        if self._on_back_override is not None:
            self._on_back_override()
            return
        slicer.util.selectModule(self.previous_module_name)

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

        choice = confirm_exit_session_dialog()
        if choice == "cancel":
            return
        self.close_session(save=(choice == "save"))

        # Always return to Home after exiting a session. (workflow_jump_ahead would land
        # the user on Session, which is the workflow's starting module post-PR-5.)
        slicer.util.selectModule("OpenLIFUHome")

    def save_session(self):
        data_module_logic : OpenLIFUDataLogic = slicer.util.getModuleLogic('OpenLIFUData')
        data_module_logic.save_session()

    def close_session(self, save:bool):
        """Close the session, saving it or not depending on `save`"""
        data_module_logic : OpenLIFUDataLogic = slicer.util.getModuleLogic('OpenLIFUData')
        if save:
            self.save_session()
        data_module_logic.clear_session(clean_up_scene=True)

    def update_next_button(self):
        """Update next button enabledness and tooltip"""
        if not hasattr(self, "next_button"):
            return
        # NB: ``get_guided_mode_state`` must be evaluated lazily because this
        # method runs from ``__init__`` (via ``self.update()``) while the
        # OpenLIFUHome logic / parameter node is itself being constructed.
        # Look it up only on the branch that needs it.
        enabled = (
            self.can_proceed
            or (not self._enforce_can_proceed_always and not get_guided_mode_state())
        ) and self.next_module_name is not None
        self.next_button.setEnabled(enabled)
        if enabled:
            self.next_button.setToolTip(f"Go to the {self.next_module_name} module.")
        else:
            self.next_button.setToolTip(self.status_text)

    def update_back_button_enabledness(self):
        if not hasattr(self, "back_button"):
            return
        enabled = self.previous_module_name is not None
        self.back_button.setEnabled(enabled)
        if enabled:
            self.back_button.setToolTip(f"Go to the {self.previous_module_name} module.")
        else:
            self.back_button.setToolTip("")

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
        "OpenLIFUSession",
        "OpenLIFUPrePlanning",
        "OpenLIFUTransducerLocalization",
        "OpenLIFUSonicationPlanner",
        "OpenLIFUSonicationControl",
    ]
    """Defines the order of the guided workflow.

    OpenLIFUDatabase and OpenLIFUData are deliberately *not* in this list
    -- they live outside the guided chain (Home jumps straight to Session)
    and grow their own off-workflow footers via
    :py:meth:`GuidedWorkflowMixin.inject_custom_workflow_controls_into_placeholder`.
    """

    def __init__(self):
        
        self._global_session : "Optional[openlifu.db.session.Session]" = None
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
                include_session_controls = True,  # All modules have session controls now, disabled conditionally.
            )

        self.update_all()  # init state-related updates in workflow controls

    @property
    def global_session(self) ->  "openlifu.db.session.Session":
        """The openlifu session recognized by the workflow. If a session is
        always supposed to be global, global_session must still be manually set
        in a syncing routine with the other session. It is recommended to set
        up the sync routine in the object responsible for holding the Workflow
        object"""
        return self._global_session

    @global_session.setter
    def global_session(self, new_val : "openlifu.db.session.Session"):
        self._global_session = new_val
        self.update_save_buttons_enabledness()
        self.update_exit_buttons_enabledness()

    def starting_module(self) -> str:
        """Get the name of the first module in the guided workflow."""
        return "OpenLIFUSession"

    def furthest_module_to_which_can_proceed(self) -> str:
        """Get the name of the furthest module along the workflow to which we `can_proceed`."""
        for module_name in self.modules:
            if not self.workflow_controls[module_name].can_proceed:
                return module_name
        return self.modules[-1]

    def update_save_buttons_enabledness(self):
        """Update save button enabledness for all workflow controls at once"""
        if self.global_session is None:
            enabled = False
            tooltip = "There is no loaded session to save."
        else:
            enabled = True
            tooltip = "Save the currently loaded session."

        for module_name in self.modules:
            controls = self.workflow_controls[module_name]
            if not hasattr(controls, "save_button"):
                return

            controls.save_button.setEnabled(enabled)
            controls.save_button.setToolTip(tooltip)

    def update_exit_buttons_enabledness(self):
        """Update exit button enabledness for all workflow controls at once"""
        if self.global_session is None:
            enabled = False
            tooltip = "There is no loaded session to exit/unload."
        else:
            enabled = True
            tooltip = "Exit the currently loaded session."

        for module_name in self.modules:
            controls = self.workflow_controls[module_name]
            if not hasattr(controls, "exit_button"):
                return

            controls.exit_button.setEnabled(enabled)
            controls.exit_button.setToolTip(tooltip)
    
    def update_all(self):
        self.update_save_buttons_enabledness()
        self.update_exit_buttons_enabledness()

        for workflow_controls in self.workflow_controls.values():
            workflow_controls.update()

    def enforceGuidedModeVisibility(self, enforced: bool = False):

        # ---- Locate widgets of interest ----

        hide_in_guided_mode_widgets = []  # widgets with dynamic property
        call_enforce_in_guided_mode_widgets = []  # widgets with their own defined enforceGuidedModeVisibility()
        for moduleName in self.modules:
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

        If the module is not registered in :attr:`Workflow.modules` (e.g. Database / Data after the PR-5
        restructure) the placeholder is hidden instead and ``self.workflow_controls`` is set to ``None``.
        Off-workflow modules that still want a footer should call
        :py:meth:`inject_custom_workflow_controls_into_placeholder` instead.
        """
        home_module_logic : OpenLIFUHomeLogic = slicer.util.getModuleLogic('OpenLIFUHome')
        controls = home_module_logic.workflow.workflow_controls.get(self.moduleName)
        if controls is None:
            self.workflow_controls = None
            placeholder = getattr(self.ui, "workflowControlsPlaceholder", None)
            if placeholder is not None:
                placeholder.hide()
            return
        self.workflow_controls = controls
        replace_widget(self.ui.workflowControlsPlaceholder, self.workflow_controls, self.ui)

    def inject_custom_workflow_controls_into_placeholder(
        self,
        previous_module_name: Optional[str],
        next_module_name: Optional[str],
        include_session_controls: bool = False,
        enforce_can_proceed_always: bool = False,
        on_back_override: Optional[Callable[[], None]] = None,
    ):
        """Build a standalone :class:`WorkflowControls` for a module that is *not* in
        :attr:`Workflow.modules`, and inject it into ``workflowControlsPlaceholder``.

        The widget is not tracked by the :class:`Workflow` registry, so the host
        module owns its lifecycle and is responsible for keeping ``can_proceed``
        and ``status_text`` up to date.
        """
        controls = WorkflowControls(
            parent=None,
            previous_module_name=previous_module_name,
            next_module_name=next_module_name,
            include_session_controls=include_session_controls,
            enforce_can_proceed_always=enforce_can_proceed_always,
            on_back_override=on_back_override,
        )
        self.workflow_controls = controls
        replace_widget(self.ui.workflowControlsPlaceholder, controls, self.ui)

