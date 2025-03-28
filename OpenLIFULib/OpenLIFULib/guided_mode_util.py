from typing import Optional, TYPE_CHECKING
import qt
import slicer
from OpenLIFULib.util import display_errors

if TYPE_CHECKING:
    from OpenLIFUData.OpenLIFUData import OpenLIFUDataLogic
    from OpenLIFUHome.OpenLIFUHome import OpenLIFUHomeLogic

def get_guided_mode_state() -> bool:
    """Get guided mode state from the OpenLIFU Home module's parameter node"""
    openlifu_home_parameter_node = slicer.util.getModuleLogic('OpenLIFUHome').getParameterNode()
    return openlifu_home_parameter_node.guided_mode

def set_guided_mode_state(new_guided_mode_state: bool):
    """Set guided mode state in OpenLIFU Home module's parameter node"""
    openlifu_home_parameter_node = slicer.util.getModuleLogic('OpenLIFUHome').getParameterNode()
    openlifu_home_parameter_node.guided_mode = new_guided_mode_state

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


        if include_session_controls:
            button_row2_layout = qt.QHBoxLayout()
            self.save_close_button = qt.QPushButton("Save and close" if self.next_module_name is not None else "Finish")
            self.close_button = qt.QPushButton("Close without saving")
            button_row2_layout.addWidget(self.save_close_button)
            button_row2_layout.addWidget(self.close_button)
            main_group_box_layout.addLayout(button_row2_layout)
            self.save_close_button.clicked.connect(self.on_save_close)
            self.close_button.clicked.connect(self.on_close)

        self.update_back_button_enabledness()
        self.update_next_button_enabledness()
        self.update_status_label()

    def on_next(self):
        slicer.util.selectModule(self.next_module_name)

    def on_back(self):
        slicer.util.selectModule(self.previous_module_name)

    @display_errors
    def on_save_close(self, clicked:bool):
        self.close_session(save=True)

    @display_errors
    def on_close(self, clicked:bool):
        self.close_session(save=False)

    def close_session(self, save:bool):
        """Close the session, saving it or not depending on `save`"""
        data_module_logic : OpenLIFUDataLogic = slicer.util.getModuleLogic('OpenLIFUData')
        home_module_logic : OpenLIFUHomeLogic = slicer.util.getModuleLogic('OpenLIFUHome')
        if save:
            data_module_logic.save_session()
        data_module_logic.clear_session(clean_up_scene=True)
        home_module_logic.start_guided_mode()

    def update_next_button_enabledness(self):
        if not hasattr(self, "next_button"):
            return
        enabled = (
            (
                (self.next_module_name is not None)
                and self.can_proceed
            )
            or not get_guided_mode_state()
        )
        self.next_button.setEnabled(enabled)
        if enabled:
            self.next_button.setToolTip(f"Go to the {self.next_module_name} module.")
        else:
            self.next_button.setToolTip(self.status_text)


    def update_back_button_enabledness(self):
        if not hasattr(self, "back_button"):
            return
        self.back_button.setEnabled(self.previous_module_name is not None)

    def update_status_label(self):
        self.status_label.setText(self.status_text)

    @property
    def can_proceed(self) -> bool:
        """Whether the next step of the workflow should be available"""
        return self._can_proceed

    @can_proceed.setter
    def can_proceed(self, new_val : bool):
        self._can_proceed = new_val
        self.update_next_button_enabledness()

    @property
    def status_text(self) -> str:
        """Status text explaining what the next step is"""
        return self._status_text

    @status_text.setter
    def status_text(self, new_val : str):
        self._status_text = new_val
        self.update_status_label()


