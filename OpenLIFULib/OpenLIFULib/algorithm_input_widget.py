from typing import Tuple
import qt
import slicer
from slicer import vtkMRMLScalarVolumeNode, vtkMRMLMarkupsFiducialNode
from OpenLIFULib.parameter_node_utils import SlicerOpenLIFUProtocol
from OpenLIFULib.util import get_openlifu_data_parameter_node
from OpenLIFULib.transducer import SlicerOpenLIFUTransducer
from OpenLIFULib.targets import get_target_candidates

class OpenLIFUAlgorithmInputWidget(qt.QWidget):
    def __init__(self, parent=None):
        super().__init__(parent)

        layout = qt.QVBoxLayout(self)
        self.setLayout(layout)

        self.protocolComboBox = qt.QComboBox(self)
        self.transducerComboBox = qt.QComboBox(self)
        self.volumeComboBox = qt.QComboBox(self)
        self.targetComboBox = qt.QComboBox(self)

        self.combo_boxes = [
            self.protocolComboBox,
            self.transducerComboBox,
            self.volumeComboBox,
            self.targetComboBox,
        ]

        for combo_box in self.combo_boxes:
            layout.addWidget(combo_box)

    def add_protocol_to_combobox(self, protocol : SlicerOpenLIFUProtocol) -> None:
        self.protocolComboBox.addItem("{} (ID: {})".format(protocol.protocol.name,protocol.protocol.id), protocol)

    def add_transducer_to_combobox(self, transducer : SlicerOpenLIFUTransducer) -> None:
        transducer_openlifu = transducer.transducer.transducer
        self.transducerComboBox.addItem("{} (ID: {})".format(transducer_openlifu.name,transducer_openlifu.id), transducer)

    def add_volume_to_combobox(self, volume_node : vtkMRMLScalarVolumeNode) -> None:
        self.volumeComboBox.addItem("{} (ID: {})".format(volume_node.GetName(),volume_node.GetID()), volume_node)

    def set_session_related_combobox_tooltip(self, text:str):
        """Set tooltip on the transducer and volume comboboxes."""
        self.protocolComboBox.setToolTip(text)
        self.transducerComboBox.setToolTip(text)
        self.volumeComboBox.setToolTip(text)

    def _populate_from_loaded_objects(self) -> None:
        """" Update protocol, transducer, and volume comboboxes based on the OpenLIFU objects loaded into the scene."""

        dataParameterNode = get_openlifu_data_parameter_node()

        # Update protocol combo box
        self.protocolComboBox.clear()
        if len(dataParameterNode.loaded_protocols) == 0:
            self.protocolComboBox.addItem("Select a Protocol")
            self.protocolComboBox.setDisabled(True)
        else:
            self.protocolComboBox.setEnabled(True)
            for protocol in dataParameterNode.loaded_protocols.values():
                self.add_protocol_to_combobox(protocol)

        # Update transducer combo box
        self.transducerComboBox.clear()
        if len(dataParameterNode.loaded_transducers) == 0:
            self.transducerComboBox.addItem("Select a Transducer")
            self.transducerComboBox.setDisabled(True)
        else:
            self.transducerComboBox.setEnabled(True)
            for transducer in dataParameterNode.loaded_transducers.values():
                self.add_transducer_to_combobox(transducer)

        # Update volume combo box
        self.volumeComboBox.clear()
        if len(slicer.util.getNodesByClass('vtkMRMLScalarVolumeNode')) == 0:
            self.volumeComboBox.addItem("Select a Volume")
            self.volumeComboBox.setDisabled(True)
        else:
            self.volumeComboBox.setEnabled(True)
            for volume_node in slicer.util.getNodesByClass('vtkMRMLScalarVolumeNode'):
                self.add_volume_to_combobox(volume_node)

        self.set_session_related_combobox_tooltip("")

    def _populate_from_session(self) -> None:
        """Update protocol, transducer, and volume comboboxes based on the active session, and lock them.

        Does not check that the session is still valid and everything it needs is there in the scene; make sure to
        check before using this.
        """
        session = get_openlifu_data_parameter_node().loaded_session

        # These are the protocol, transducer, and volume that will be used
        protocol : SlicerOpenLIFUProtocol = session.get_protocol()
        transducer : SlicerOpenLIFUTransducer = session.get_transducer()
        volume_node : vtkMRMLScalarVolumeNode = session.volume_node

        # Update protocol combo box
        self.protocolComboBox.clear()
        self.protocolComboBox.setDisabled(True)
        self.add_protocol_to_combobox(protocol)

        # Update transducer combo box
        self.transducerComboBox.clear()
        self.transducerComboBox.setDisabled(True)
        self.add_transducer_to_combobox(transducer)

        # Update volume combo box
        self.volumeComboBox.clear()
        self.volumeComboBox.setDisabled(True)
        self.add_volume_to_combobox(volume_node)

        self.set_session_related_combobox_tooltip("This choice is fixed by the active session")

    def update(self):
        """Update the comboboxes, forcing some of them to take values derived from the active session if there is one"""

        # Update protocol, transducer, and volume comboboxes
        if slicer.util.getModuleLogic('OpenLIFUData').validate_session():
            self._populate_from_session()
        else:
            self._populate_from_loaded_objects()

        # Update target combo box
        self.targetComboBox.clear()
        target_nodes = get_target_candidates()
        if len(target_nodes) == 0:
            self.targetComboBox.addItem("Select a Target")
            self.targetComboBox.setDisabled(True)
        else:
            self.targetComboBox.setEnabled(True)
            for target_node in target_nodes:
                self.targetComboBox.addItem("{} (ID: {})".format(target_node.GetName(),target_node.GetID()), target_node)

    def has_valid_selections(self) -> bool:
        """Whether all options have been selected, so that get_current_data would return
        a complete set of data with no `None`s."""
        return all(combo_box.currentData is not None for combo_box in self.combo_boxes)

    def get_current_data(self) -> Tuple[SlicerOpenLIFUProtocol,SlicerOpenLIFUTransducer,vtkMRMLScalarVolumeNode,vtkMRMLMarkupsFiducialNode]:
        """Get the current selections as a tuple"""
        return tuple(combo_box.currentData() for combo_box in self.combo_boxes)