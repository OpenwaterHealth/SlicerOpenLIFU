from typing import Tuple, Any, List
from dataclasses import dataclass
import qt
import slicer
from slicer import vtkMRMLScalarVolumeNode, vtkMRMLMarkupsFiducialNode
from OpenLIFULib.parameter_node_utils import SlicerOpenLIFUProtocol
from OpenLIFULib.util import get_openlifu_data_parameter_node
from OpenLIFULib.transducer import SlicerOpenLIFUTransducer
from OpenLIFULib.targets import get_target_candidates

@dataclass
class AlgorithmInput:
    name : str
    combo_box : qt.QComboBox
    most_recent_selection : Any = None

    def indicate_no_options(self):
        """Disable and set a message indicating that there are no objects"""
        self.combo_box.addItem(f"No {self.name} objects")
        self.combo_box.setDisabled(True)

class OpenLIFUAlgorithmInputWidget(qt.QWidget):
    def __init__(self, algorithm_input_names : List[str], parent=None):
        super().__init__(parent)
        """
        Args:
            algorithm_input_names: Names of inputs required for the algorithm i.e. "Volume", "Transducer" etc
        """

        layout = qt.QFormLayout(self)
        self.setLayout(layout)

        self.algorithm_input_names = algorithm_input_names
        self.inputs = []
        for input_name in self.algorithm_input_names:
            if input_name == "Protocol":
                self.protocol_input = AlgorithmInput("Protocol", qt.QComboBox(self))
                self.inputs.append(self.protocol_input)
            elif input_name == "Transducer":
                self.transducer_input = AlgorithmInput("Transducer", qt.QComboBox(self))
                self.inputs.append(self.transducer_input)
            elif input_name == "Volume":
                self.volume_input = AlgorithmInput("Volume", qt.QComboBox(self))
                self.inputs.append(self.volume_input)
            elif input_name == "Target":
                self.target_input = AlgorithmInput("Target", qt.QComboBox(self))
                self.inputs.append(self.target_input)
            else:
                raise RuntimeError("Invalid algorithm input specified.")
                
        for input in self.inputs:
            layout.addRow(f"{input.name}:", input.combo_box)

    def add_protocol_to_combobox(self, protocol : SlicerOpenLIFUProtocol) -> None:
        self.protocol_input.combo_box.addItem("{} (ID: {})".format(protocol.protocol.name,protocol.protocol.id), protocol)

    def add_transducer_to_combobox(self, transducer : SlicerOpenLIFUTransducer) -> None:
        transducer_openlifu = transducer.transducer.transducer
        self.transducer_input.combo_box.addItem("{} (ID: {})".format(transducer_openlifu.name,transducer_openlifu.id), transducer)

    def add_volume_to_combobox(self, volume_node : vtkMRMLScalarVolumeNode) -> None:
        self.volume_input.combo_box.addItem("{} (ID: {})".format(volume_node.GetName(),volume_node.GetID()), volume_node)

    def set_session_related_combobox_tooltip(self, text:str):
        """Set tooltip on the transducer and volume comboboxes."""
        for input in [
            self.protocol_input,
            self.transducer_input,
            self.volume_input,
        ]:
            input.combo_box.setToolTip(text)

    def _clear_input_options(self):
        """Clear out input options, remembering what was most recently selected in order to be able to set that again later"""
        for input in self.inputs:
            input.most_recent_selection = input.combo_box.currentData
            input.combo_box.clear()

    def _set_most_recent_selections(self):
        """Set input options to their most recent selections when possible."""
        for input in self.inputs:
            if input.most_recent_selection is not None:
                most_recent_selection_index = input.combo_box.findData(input.most_recent_selection)
                if most_recent_selection_index != -1:
                    input.combo_box.setCurrentIndex(most_recent_selection_index)


    def _populate_from_loaded_objects(self) -> None:
        """" Update protocol, transducer, and volume comboboxes based on the OpenLIFU objects loaded into the scene.
        Adds the items only; does not clear the ComboBoxes."""

        dataParameterNode = get_openlifu_data_parameter_node()

        # Update protocol combo box
        if len(dataParameterNode.loaded_protocols) == 0:
            self.protocol_input.indicate_no_options()
        else:
            self.protocol_input.combo_box.setEnabled(True)
            for protocol in dataParameterNode.loaded_protocols.values():
                self.add_protocol_to_combobox(protocol)

        # Update transducer combo box
        if len(dataParameterNode.loaded_transducers) == 0:
            self.transducer_input.indicate_no_options()
        else:
            self.transducer_input.combo_box.setEnabled(True)
            for transducer in dataParameterNode.loaded_transducers.values():
                self.add_transducer_to_combobox(transducer)

        # Update volume combo box
        if len(slicer.util.getNodesByClass('vtkMRMLScalarVolumeNode')) == 0:
            self.volume_input.indicate_no_options()
        else:
            self.volume_input.combo_box.setEnabled(True)
            for volume_node in slicer.util.getNodesByClass('vtkMRMLScalarVolumeNode'):
                # Check that the volume is not an OpenLIFUSolution output volume
                if volume_node.GetAttribute('isOpenLIFUSolution') is None:
                    self.add_volume_to_combobox(volume_node)

        self.set_session_related_combobox_tooltip("")

    def _populate_from_session(self) -> None:
        """Update protocol, transducer, and volume comboboxes based on the active session, and lock them.

        Does not check that the session is still valid and everything it needs is there in the scene; make sure to
        check before using this.

        Adds the items only; does not clear the ComboBoxes.
        """
        session = get_openlifu_data_parameter_node().loaded_session

        # These are the protocol, transducer, and volume that will be used
        protocol : SlicerOpenLIFUProtocol = session.get_protocol()
        transducer : SlicerOpenLIFUTransducer = session.get_transducer()
        volume_node : vtkMRMLScalarVolumeNode = session.volume_node

        # Update protocol combo box
        self.protocol_input.combo_box.setDisabled(True)
        self.add_protocol_to_combobox(protocol)

        # Update transducer combo box
        self.transducer_input.combo_box.setDisabled(True)
        self.add_transducer_to_combobox(transducer)

        # Update volume combo box
        self.volume_input.combo_box.setDisabled(True)
        self.add_volume_to_combobox(volume_node)

        self.set_session_related_combobox_tooltip("This choice is fixed by the active session")

    def update(self):
        """Update the comboboxes, forcing some of them to take values derived from the active session if there is one"""

        self._clear_input_options()

        # Update protocol, transducer, and volume comboboxes
        if slicer.util.getModuleLogic('OpenLIFUData').validate_session():
            self._populate_from_session()
        else:
            self._populate_from_loaded_objects()

        # Update target combo box if part of the algorithm inputs
        if "Target" in self.algorithm_input_names:
            target_nodes = get_target_candidates()
            if len(target_nodes) == 0:
                self.target_input.indicate_no_options()
            else:
                self.target_input.combo_box.setEnabled(True)
                for target_node in target_nodes:
                    self.target_input.combo_box.addItem(target_node.GetName(), target_node)

        # Set selections to the previous ones when they exist
        self._set_most_recent_selections()

    def has_valid_selections(self) -> bool:
        """Whether all options have been selected, so that get_current_data would return
        a complete set of data with no `None`s."""
        return all(input.combo_box.currentData is not None for input in self.inputs)

    def get_current_data(self) -> Tuple[SlicerOpenLIFUProtocol,SlicerOpenLIFUTransducer,vtkMRMLScalarVolumeNode,vtkMRMLMarkupsFiducialNode]:
        """Get the current selections as a tuple"""
        return tuple(input.combo_box.currentData for input in self.inputs)
