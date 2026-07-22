from typing import Dict, Any, List, Callable, Literal, TYPE_CHECKING, Optional
from dataclasses import dataclass

import ctk
from enum import Enum
import qt
import slicer

from slicer import vtkMRMLMarkupsFiducialNode, vtkMRMLScalarVolumeNode, vtkMRMLTransformNode
from OpenLIFULib.parameter_node_utils import SlicerOpenLIFUProtocol
from OpenLIFULib.util import get_app_state
from OpenLIFULib import SlicerOpenLIFUTransducer

if TYPE_CHECKING:
    import openlifu
    import openlifu.nav.photoscan

class InputType(Enum):
    PROTOCOL = "Protocol"
    TRANSDUCER = "Transducer"
    VOLUME = "Volume"
    TARGET = "Target"
    PHOTOSCAN = "Photoscan"

@dataclass
class TargetSelection:
    """A single selectable row in the Target combobox.

    Bundles the target fiducial with the transducer transform that should be used
    when computing a solution for this row. See issue #609.

    - ``kind="TT"``: the transform node is the live transducer's transform, whose
      matrix reflects whichever transducer-tracking result is currently applied.
    - ``kind="VF"``: the transform node is an approved virtual-fit result node.
      Selecting this row is a deviation from the standard user-mode-enforced flow;
      it is only offered when ``OpenLIFULib.kiosk_util.get_user_mode()`` is False.
      Computing against a VF row yields a "pre-solution" (see #609).
    """
    target_node: vtkMRMLMarkupsFiducialNode
    transform_node: vtkMRMLTransformNode
    kind: Literal["TT", "VF"] = "TT"

@dataclass
class AlgorithmInput:
    name : str
    label : qt.QLabel
    combo_box : qt.QComboBox
    most_recent_selection : Any = None
    refresh_button : qt.QToolButton = None

    def disable_with_tooltip(self, tooltip_message:str) -> None:
        self.combo_box.setDisabled(True)
        self.combo_box.setToolTip(tooltip_message)

    def indicate_no_options(self):
        """Disable and set a message indicating that there are no objects"""
        self.combo_box.addItem(f"No {self.name} objects")
        self.combo_box.setDisabled(True)

class OpenLIFUAlgorithmInputWidget(qt.QWidget):
    def __init__(
        self,
        algorithm_input_names : List[str],
        parent=None,
        hide_singleton_inputs: bool = False,
        use_target_selection: bool = False,
    ):
        super().__init__(parent)
        """
        Creates a widget containing QComboBoxes for each of the input types specified by the user.
        Args:
            algorithm_input_names: Names of inputs required for the algorithm i.e. "Volume", "Transducer" etc
            hide_singleton_inputs: If True, `update()` collapses the whole form row for any
                input whose combo box holds exactly one real (data-bearing) option, so the
                user is not shown a locked single-choice dropdown. Rows with zero valid options
                (i.e. the disabled "No X objects" placeholder) remain visible so the user still
                sees why the input is unavailable.
            use_target_selection: If True (opt-in), the Target combobox stores each row's user
                data as a ``TargetSelection`` (bundling target fiducial + transform + kind), and
                when ``OpenLIFULib.kiosk_util.get_user_mode()`` is False the combobox additionally
                lists a "Virtual Fit for {label}" row per target that has an approved virtual-fit
                result. When False (default), each Target row's user data is just the fiducial
                node (legacy behavior).
        """

        self._hide_singleton_inputs = hide_singleton_inputs
        self._use_target_selection = use_target_selection

        layout = qt.QFormLayout(self)
        self.setLayout(layout)

        self.inputs_dict : Dict[str,AlgorithmInput] = {}
        for input_name in algorithm_input_names:
            if input_name not in [item.value for item in InputType]:
                raise ValueError("Invalid algorithm input specified.")
            elif input_name == "Photoscan":
                refreshButton = qt.QToolButton()
                refreshButton.setIcon(slicer.app.style().standardIcon(qt.QStyle.SP_BrowserReload))
                refreshButton.setToolTip("Refresh")
                self.inputs_dict[input_name] = AlgorithmInput(
                    input_name, qt.QLabel(f"{input_name}", self), 
                    ctk.ctkComboBox(self), refresh_button= refreshButton
                    )
            else:
                self.inputs_dict[input_name] = AlgorithmInput(input_name, qt.QLabel(f"{input_name}", self), ctk.ctkComboBox(self))

        # Track the QFormLayout row for each input via its label + combo (+ optional refresh
        # button). We hide those directly in `_refresh_input_visibility` instead of using
        # QFormLayout.setRowVisible (which is not exposed by Slicer's PythonQt binding).
        for input in self.inputs_dict.values():
            if input.refresh_button is not None:
                specialRow = qt.QHBoxLayout()
                specialRow.addWidget(input.combo_box, 1)
                specialRow.addWidget(input.refresh_button, 0) # No Stretch
                layout.addRow(input.label, specialRow)
            else:
                layout.addRow(input.label, input.combo_box)

    def add_protocol_to_combobox(self, protocol : SlicerOpenLIFUProtocol) -> None:
        self.inputs_dict["Protocol"].combo_box.addItem("{} (ID: {})".format(protocol.protocol.name,protocol.protocol.id), protocol)

    def add_transducer_to_combobox(self, transducer : SlicerOpenLIFUTransducer) -> None:
        transducer_openlifu = transducer.transducer.transducer
        self.inputs_dict["Transducer"].combo_box.addItem("{} (ID: {})".format(transducer_openlifu.name,transducer_openlifu.id), transducer)

    def add_volume_to_combobox(self, volume_node : vtkMRMLScalarVolumeNode) -> None:
        self.inputs_dict["Volume"].combo_box.addItem("{} (ID: {})".format(volume_node.GetName(),volume_node.GetID()), volume_node)

    def add_photoscan_to_combobox(self, photoscan_openlifu: "openlifu.nav.photoscan.Photoscan") -> None:
        self.inputs_dict["Photoscan"].combo_box.addItem("{} (ID: {})".format(photoscan_openlifu.name, photoscan_openlifu.id), photoscan_openlifu)

    def set_session_related_combobox_tooltip(self, text:str):
        """Set tooltip on the transducer, protocol and volume comboboxes."""

        for input in ["Protocol", "Transducer", "Volume"]:
            if input in self.inputs_dict:
                self.inputs_dict[input].combo_box.setToolTip(text)

    def enforceGuidedModeVisibility(self, enforced: bool):
        """Enforce visibility of widgets when in guided mode. This function is
        defined for this Widget because when guided mode is activated, we want
        to let the parent widget *choose* which sub-widgets to hide. In this
        specific case, it is simple, but some widgets may be more picky"""

        # In this case we just want to hide these three combo box widgets
        for widget_key in ["Protocol", "Transducer", "Volume"]:
            if widget_key in self.inputs_dict:
                self.inputs_dict[widget_key].label.visible = not enforced
                self.inputs_dict[widget_key].combo_box.visible = not enforced

    def _clear_input_options(self):
        """Clear out input options, remembering what was most recently selected in order to be able to set that again later"""
        for input in self.inputs_dict.values():
            input.most_recent_selection = input.combo_box.currentText 
            input.combo_box.clear()

    def _set_most_recent_selections(self):
        """Set input options to their most recent selections when possible."""
        for input in self.inputs_dict.values():
            if input.most_recent_selection is not None:
                most_recent_selection_index = input.combo_box.findText(input.most_recent_selection)
                if most_recent_selection_index != -1:
                    input.combo_box.setCurrentIndex(most_recent_selection_index)

    def _populate_from_loaded_objects(self) -> None:
        """" Update protocol, transducer, and volume comboboxes if present based on the OpenLIFU objects loaded into the scene.
        Adds the items only; does not clear the ComboBoxes."""
        dataParameterNode = get_app_state()

        # Update protocol combo box
        if "Protocol" in self.inputs_dict:
            if len(dataParameterNode.loaded_protocols) == 0:
                self.inputs_dict["Protocol"].indicate_no_options()
            else:
                self.inputs_dict["Protocol"].combo_box.setEnabled(True)
                for protocol in dataParameterNode.loaded_protocols.values():
                    self.add_protocol_to_combobox(protocol)

        # Update transducer combo box
        if "Transducer" in self.inputs_dict:
            if len(dataParameterNode.loaded_transducers) == 0:
                self.inputs_dict["Transducer"].indicate_no_options()
            else:
                self.inputs_dict["Transducer"].combo_box.setEnabled(True)
                for transducer in dataParameterNode.loaded_transducers.values():
                    self.add_transducer_to_combobox(transducer)

        # Update volume combo box
        valid_input_volumes = 0
        if "Volume" in self.inputs_dict:
            self.inputs_dict["Volume"].combo_box.setEnabled(True)
            for volume_node in slicer.util.getNodesByClass('vtkMRMLScalarVolumeNode'):
                # Check that the volume is not an OpenLIFUSolution output volume
                if volume_node.GetAttribute('isOpenLIFUSolution') is None and volume_node.GetAttribute('isOpenLIFUPhotoscan') is None :
                    self.add_volume_to_combobox(volume_node)
                    valid_input_volumes += 1
            if valid_input_volumes == 0:
                self.inputs_dict["Volume"].indicate_no_options()
        
        # Update photoscans combobox 
        if "Photoscan" in self.inputs_dict:
            if len(dataParameterNode.loaded_photoscans) == 0:
                self.inputs_dict["Photoscan"].indicate_no_options()
            else:
                self.inputs_dict["Photoscan"].combo_box.setEnabled(True)
                for photoscan in dataParameterNode.loaded_photoscans.values():
                    photoscan_openlifu = photoscan.photoscan.photoscan
                    self.add_photoscan_to_combobox(photoscan_openlifu)
            self.inputs_dict["Photoscan"].combo_box.setToolTip("")

        self.set_session_related_combobox_tooltip("")

    def _populate_from_session(self) -> None:
        """Update protocol, transducer and volume comboboxes if present based on the active session, and lock them.
        
        Populate the photoscan combobox if present with any photoscans saved under the session. The combobox should
        not be locked since there can be multiple photoscans associated with a session. 

        Does not check that the session is still valid and everything it needs is there in the scene; make sure to
        check before using this.

        Adds the items only; does not clear the ComboBoxes.
        """
        session = get_app_state().loaded_session

        # These are the protocol, transducer, photoscans and and volume that will be used
        protocol : SlicerOpenLIFUProtocol = session.get_protocol()
        transducer : SlicerOpenLIFUTransducer = session.get_transducer()
        volume_node : vtkMRMLScalarVolumeNode = session.volume_node
        affiliated_photoscans_list : List["openlifu.nav.photoscan.Photoscan"] = session.get_affiliated_photoscans()

        # Update protocol combo box
        if "Protocol" in self.inputs_dict:
            self.inputs_dict["Protocol"].combo_box.setDisabled(True)
            self.add_protocol_to_combobox(protocol)

        # Update transducer combo box
        if "Transducer" in self.inputs_dict:
            self.inputs_dict["Transducer"].combo_box.setDisabled(True)
            self.add_transducer_to_combobox(transducer)

        # Update volume combo box
        if "Volume" in self.inputs_dict:
            self.inputs_dict["Volume"].combo_box.setDisabled(True)
            self.add_volume_to_combobox(volume_node)

        self.set_session_related_combobox_tooltip("This choice is fixed by the active session")

        # Update photoscan combo box
        if "Photoscan" in self.inputs_dict:
            if len(affiliated_photoscans_list) == 0:
                self.inputs_dict["Photoscan"].indicate_no_options()
                self.inputs_dict["Photoscan"].combo_box.setToolTip("There are no photoscans affiliated with the active session. Add a photoscan to the session using the OpenLIFU Data module.")
            else:
                self.inputs_dict["Photoscan"].combo_box.setEnabled(True)
                for photoscan_openlifu in affiliated_photoscans_list:
                    self.add_photoscan_to_combobox(photoscan_openlifu) 
                self.inputs_dict["Photoscan"].combo_box.setToolTip("These are the photoscans affiliated with the active session")

    def update(self):
        """Update the comboboxes, forcing some of them to take values derived from the active session if there is one"""

        self._clear_input_options()

        # Update protocol, transducer, and volume comboboxes
        if slicer.util.getModuleLogic("OpenLIFU").data_logic.validate_session():
            self._populate_from_session()
        else:
            self._populate_from_loaded_objects()

        # Update target combo box if part of the algorithm inputs. Targets are session-owned:
        # the combo lists only fiducials that have been explicitly registered with the
        # session via SlicerOpenLIFUSession.add_target (loose scene fiducials are ignored).
        # Use get_target_nodes() so we skip any stale entries the parameterPack may hold
        # for MRML nodes that have already been removed from the scene.
        if "Target" in self.inputs_dict:
            session = get_app_state().loaded_session
            target_nodes = session.get_target_nodes() if session is not None else []
            if len(target_nodes) == 0:
                self.inputs_dict["Target"].indicate_no_options()
            else:
                self.inputs_dict["Target"].combo_box.setEnabled(True)
                # Local imports to avoid circular imports at module load time.
                from OpenLIFULib.targets import fiducial_to_openlifu_point_id, label_for_target_id
                # Snapshot the live transducer's transform node once; every TT row shares it.
                # When ``use_target_selection`` is off this is unused.
                tt_transform_node = None
                approved_vf_by_target: Dict[str, vtkMRMLTransformNode] = {}
                if self._use_target_selection and session is not None:
                    from OpenLIFULib.kiosk_util import get_user_mode
                    from OpenLIFULib.virtual_fit_results import get_virtual_fit_result_nodes
                    tt_transform_node = session.get_transducer().transform_node
                    if not get_user_mode():
                        # Precompute {target_id -> approved VF transform node} for this session.
                        # sort=True is ascending rank (best first); keep only the first hit per target.
                        for vf_node in get_virtual_fit_result_nodes(
                            session_id=session.get_session_id(),
                            approved_only=True,
                            sort=True,
                        ):
                            approved_vf_by_target.setdefault(
                                vf_node.GetAttribute("VF:targetID"), vf_node,
                            )
                for target_node in target_nodes:
                    target_id = fiducial_to_openlifu_point_id(target_node)
                    # Show the user-facing display label (via label_for_target_id) alongside the
                    # internal id. Previously used target_node.GetName(), which returns the openlifu
                    # Point id itself -- so the entry read "Target_1 (ID: Target_1)" until the user
                    # renamed the target, at which point the label went stale (#594).
                    label = label_for_target_id(target_id)
                    if self._use_target_selection:
                        self.inputs_dict["Target"].combo_box.addItem(
                            "{} (ID: {})".format(label, target_id),
                            TargetSelection(
                                target_node=target_node,
                                transform_node=tt_transform_node,
                                kind="TT",
                            ),
                        )
                        # If pre-solutions are enabled for this session and this target has an
                        # approved VF result, add an extra "Virtual Fit for ..." row that will
                        # snap the transducer to the VF transform on selection (#609).
                        if target_id in approved_vf_by_target:
                            self.inputs_dict["Target"].combo_box.addItem(
                                "Virtual Fit for {} (ID: {})".format(label, target_id),
                                TargetSelection(
                                    target_node=target_node,
                                    transform_node=approved_vf_by_target[target_id],
                                    kind="VF",
                                ),
                            )
                    else:
                        self.inputs_dict["Target"].combo_box.addItem(
                            "{} (ID: {})".format(label, target_id),
                            target_node,
                        )

        # Set selections to the previous ones when they exist
        self._set_most_recent_selections()

        # Optionally collapse form rows whose combo is a locked single-option dropdown.
        if self._hide_singleton_inputs:
            self._refresh_input_visibility()

    def _refresh_input_visibility(self) -> None:
        """Hide/show each input's form row based on how many real options its combo box holds.

        A row is hidden iff the combo has exactly one item and that item carries real user data
        (i.e. it is not the disabled "No X objects" placeholder installed by `indicate_no_options`).
        This lets pages that opt in via `hide_singleton_inputs=True` avoid presenting the user
        with locked single-choice dropdowns while still surfacing the "no options" state.

        Implementation note: we hide the label and combo (and refresh button, if any) directly
        rather than calling QFormLayout.setRowVisible, because PythonQt in Slicer does not
        currently expose setRowVisible from Qt 5.14+. QFormLayout collapses the row height when
        both role widgets are hidden, which is the effect we want.
        """
        for input in self.inputs_dict.values():
            count = input.combo_box.count
            # A single item with non-None user data means one real, selectable option.
            is_singleton_real = (count == 1 and input.combo_box.itemData(0) is not None)
            show = not is_singleton_real
            input.label.setVisible(show)
            input.combo_box.setVisible(show)
            if input.refresh_button is not None:
                input.refresh_button.setVisible(show)

    def has_valid_selections(self) -> bool:
        """Whether all options have been selected, so that get_current_data would return
        a complete set of data with no `None`s."""
        return all(input.combo_box.currentData is not None for input in self.inputs_dict.values())

    def get_current_data(self) -> Dict[str, Any]:
        """Get the current selections as a Dictionary. Potential output data types are:
            Protocol: SlicerOpenLIFUProtocol
            Transducer: SlicerOpenLIFUTransducer
            Volume: vtkMRMLScalarVolumeNode
            Target: vtkMRMLMarkupsFiducialNode
            Photoscan: "openlifu.nav.photoscan.Photoscan"
        """
        current_data_dict = {
            input.name : input.combo_box.currentData
            for input in self.inputs_dict.values()
        }
        return current_data_dict
    
    def connect_combobox_indexchanged_signal(self, function_call: Callable, input_type: Optional[str] = None) -> None:
        """Connect the `currentIndexChanged` signal on the input combobox(es) to a callable function.
        This is helpful for when changes to the input combo boxes need to trigger certain checks for
        valid selections to run algorithms.
        If input_type is specified, connects only that input's combo box. 
        Otherwise, connects all combo boxes."""

        if input_type is not None:
            if input_type not in [item.value for item in InputType]:
                raise ValueError("Invalid algorithm input specified.")
            combo_box = self.inputs_dict[input_type].combo_box
            combo_box.currentIndexChanged.connect(function_call)
        else:
            # Connect all combo boxes
            for input in self.inputs_dict.values():
                input.combo_box.currentIndexChanged.connect(function_call)
    
    def set_photoscan_selection(self, photoscan_openlifu: "openlifu.nav.photoscan.Photoscan") -> None:
        """Set the photoscan combobox selection to the specified photoscan."""
        if "Photoscan" not in self.inputs_dict:
            return
        photoscan_combo_box = self.inputs_dict["Photoscan"].combo_box
        for i in range(photoscan_combo_box.count):
            if photoscan_combo_box.itemData(i) == photoscan_openlifu:
                photoscan_combo_box.setCurrentIndex(i)
                break

    def connect_refresh_button_signal(self, function_call: Callable, input_type: Optional[str] = None) -> None:
        """Connect refresh button(s) clicked signal to a callable function.
        If input_type is specified, connects only that input's button. 
        Otherwise, connects all refresh buttons."""
        
        if input_type is not None:
            if input_type not in [item.value for item in InputType]:
                raise ValueError("Invalid algorithm input specified.")
            refresh_button = self.inputs_dict[input_type].refresh_button
            if refresh_button is None: # Optional attribute
                raise ValueError(f"No refresh button associated with input '{input_type}'.")
            refresh_button.clicked.connect(function_call)
        else:
            # Connect all refresh buttons
            for input in self.inputs_dict.values():
                if input.refresh_button is not None:
                    input.refresh_button.clicked.connect(function_call)