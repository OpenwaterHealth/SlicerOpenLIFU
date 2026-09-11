"""Transducer manager dialogs."""

import copy
import json
import logging
from pathlib import Path
import re
from typing import Dict, List, Optional, Tuple, TYPE_CHECKING

import numpy as np
import qt
import slicer
import vtk
from slicer.i18n import tr as _

from OpenLIFULib.user_account_mode_util import get_current_user, get_user_account_mode_state
from OpenLIFULib.util import display_errors, get_cur_db, get_openlifu_data_parameter_node

from .transducer_store import (
    delete_transducer,
    ensure_transducer_not_referenced,
    save_transducer,
    validate_transducer_id,
)

if TYPE_CHECKING:
    import openlifu.db
    import openlifu.xdc


# ---------------------------------------------------------------------------
# Transducer Manager
# ---------------------------------------------------------------------------

# Canonical template IDs. Deletion prompts explain their use in device imports.
PROTECTED_TRANSDUCER_IDS = frozenset({
    "openlifu_1x155",
    "openlifu_1x400",
    "openlifu_2x155",
    "openlifu_2x400",
})

# (n_modules, freq_khz) -> template id. Mirrors openlifu.xdc.transducerarray._DEFAULT_TEMPLATE_IDS.
_TEMPLATE_IDS_BY_COUNT_FREQ: Dict[Tuple[int, int], str] = {
    (1, 155): "openlifu_1x155",
    (1, 400): "openlifu_1x400",
    (2, 155): "openlifu_2x155",
    (2, 400): "openlifu_2x400",
}


def _read_connected_user_configs(iface) -> List[dict]:
    """Pull a ``user_config`` dict from every connected TX module.

    Returns the list in module-index order. Raises ``RuntimeError`` if no
    modules are connected or any ``read_config`` call returns ``None``.
    """
    if iface is None:
        raise RuntimeError(
            "The LIFU interface has not been initialized. Open the OpenLIFU "
            "Sonication Control module to initialize the hardware interface."
        )
    txdevice = getattr(iface, "txdevice", None)
    if txdevice is None:
        raise RuntimeError("The LIFU interface has no TX device.")
    count = int(txdevice.get_tx_module_count())
    if count <= 0:
        raise RuntimeError("No TX modules are connected.")
    user_configs: List[dict] = []
    for i in range(count):
        cfg = txdevice.read_config(module=i)
        if cfg is None:
            raise RuntimeError(
                f"Failed to read user_config from TX module {i}. "
                f"The module may not have been provisioned."
            )
        data = json.loads(cfg.get_json_str())
        if not isinstance(data, dict):
            raise ValueError(f"TX module {i} did not return a configuration object.")
        user_configs.append(data)
    return user_configs


def _module_count_from_template_id(template_id: str) -> Optional[int]:
    """Parse the leading module count out of a template id like ``openlifu_2x400``.

    Returns ``None`` if the id does not match the expected ``..._<N>x<freq>``
    convention.
    """
    if not isinstance(template_id, str):
        return None
    match = re.search(r"(?:^|_)(\d+)x\d+(?:_|$)", template_id)
    return int(match.group(1)) if match else None


def _connected_frequency_khz(user_configs: List[dict]) -> Optional[float]:
    """Validate reported frequencies before using a recorded template."""
    if not user_configs:
        raise ValueError("No TX module configurations were read.")
    frequencies = []
    for index, config in enumerate(user_configs):
        reported = config.get("freq")
        module = config.get("module") or {}
        calibrated = module.get("frequency") if isinstance(module, dict) else None
        values = []
        for value, divisor in ((reported, 1.0), (calibrated, 1000.0)):
            if value is None:
                continue
            frequency = float(value) / divisor
            if not np.isfinite(frequency) or frequency <= 0:
                raise ValueError(f"TX module {index} reports an invalid frequency.")
            values.append(frequency)
        if len(values) == 2 and not np.isclose(values[0], values[1], rtol=1e-6, atol=0):
            raise ValueError(f"TX module {index} reports inconsistent device and calibration frequencies.")
        if values:
            frequencies.append(values[0])
    if frequencies and not np.allclose(frequencies, frequencies[0], rtol=1e-6, atol=0):
        raise ValueError(f"Connected TX modules report mismatched frequencies: {frequencies} kHz")
    return frequencies[0] if frequencies else None


def _resolve_template_id_for_user_configs(user_configs: List[dict]) -> Tuple[Optional[str], Optional[dict]]:
    """Use a valid recorded template, or infer a canonical template when known."""
    frequency = _connected_frequency_khz(user_configs)
    device_block = user_configs[0].get("device") or None
    if device_block is not None and not isinstance(device_block, dict):
        raise ValueError("The lead module's device configuration must be an object.")
    if device_block and device_block.get("template") is not None:
        template_id = device_block["template"]
        validate_transducer_id(template_id)
        recorded_count = _module_count_from_template_id(template_id)
        if recorded_count is not None and recorded_count != len(user_configs):
            raise ValueError(
                f"Recorded template '{template_id}' describes {recorded_count} module(s), "
                f"but {len(user_configs)} are connected. Correct the device configuration first."
            )
        return template_id, device_block
    template_id = None
    if frequency is not None and float(frequency).is_integer():
        template_id = _TEMPLATE_IDS_BY_COUNT_FREQ.get((len(user_configs), int(frequency)))
    return template_id, device_block


class DeviceConfigEditDialog(qt.QDialog):
    """Prompt the user for ``id`` + ``name`` of a transducer assembled from a connected device.

    Shown by :class:`TransducerManagerDialog` whenever the user clicks
    *Add from Device*, regardless of whether the lead module already carries
    a ``device`` block (existing values are pre-populated and remain
    editable). The template id and the per-module HWID list are read-only so
    the user can confirm what is being saved.
    """

    def __init__(
        self,
        template_id: str,
        module_hwids: List[str],
        initial_id: str = "",
        initial_name: str = "",
        parent="mainWindow",
    ):
        super().__init__(slicer.util.mainWindow() if parent == "mainWindow" else parent)
        self.setWindowTitle("Add Transducer from Device")
        self.setWindowModality(qt.Qt.WindowModal)
        # Drop the unimplemented ``?`` help button from the title bar.
        self.setWindowFlags(self.windowFlags() & ~qt.Qt.WindowContextHelpButtonHint)
        self.template_id = template_id
        self.module_hwids = list(module_hwids)
        self._initial_id = initial_id
        self._initial_name = initial_name
        self._setup()

    def _setup(self) -> None:
        # Outer VBox so we can place the form on top and the button row at the
        # bottom; QFormLayout.addWidget does not span the form as a row, which
        # is why the original button box wasn't showing up.
        outer = qt.QVBoxLayout()
        self.setLayout(outer)

        form = qt.QFormLayout()
        outer.addLayout(form)

        self.idEdit = qt.QLineEdit()
        self.idEdit.setPlaceholderText("e.g. my_array_001")
        self.idEdit.setText(self._initial_id)
        form.addRow(_("Transducer ID:"), self.idEdit)

        self.nameEdit = qt.QLineEdit()
        self.nameEdit.setPlaceholderText("e.g. My Array #1")
        self.nameEdit.setText(self._initial_name)
        form.addRow(_("Transducer Name:"), self.nameEdit)

        templateLabel = qt.QLabel(self.template_id)
        templateLabel.setStyleSheet("color: #888;")
        templateLabel.setToolTip(
            "Saved array template providing the placement and mesh files for these modules."
        )
        form.addRow(_("Template:"), templateLabel)

        hwidList = qt.QListWidget()
        hwidList.setSelectionMode(qt.QAbstractItemView.NoSelection)
        hwidList.setFocusPolicy(qt.Qt.NoFocus)
        for idx, hwid in enumerate(self.module_hwids):
            hwidList.addItem(f"Module {idx}: {hwid}")
        hwidList.setFixedHeight(min(120, 22 * max(1, len(self.module_hwids)) + 10))
        form.addRow(_("Modules:"), hwidList)

        # NOTE: PythonQt's QDialogButtonBox flag-constructor binding is flaky;
        # construct empty and then setStandardButtons, like the other dialogs
        # in this module (AddNewSubjectDialog, CreateNewSessionDialog).
        self.buttonBox = qt.QDialogButtonBox()
        self.buttonBox.setStandardButtons(
            qt.QDialogButtonBox.Ok | qt.QDialogButtonBox.Cancel
        )
        # Per UX request: call the accept button "Finish".
        finishBtn = self.buttonBox.button(qt.QDialogButtonBox.Ok)
        if finishBtn is not None:
            finishBtn.setText("Finish")
        outer.addWidget(self.buttonBox)
        self.buttonBox.accepted.connect(self._validate)
        self.buttonBox.rejected.connect(self.reject)

    def _validate(self) -> None:
        try:
            validate_transducer_id(self.idEdit.text.strip())
        except ValueError as error:
            slicer.util.errorDisplay(str(error), parent=self)
            return
        if not self.nameEdit.text.strip():
            slicer.util.errorDisplay("Transducer name is required.", parent=self)
            return
        self.accept()

    def customexec_(self) -> Tuple[int, str, str]:
        try:
            rc = self.exec_()
            return rc, self.idEdit.text.strip(), self.nameEdit.text.strip()
        finally:
            self.deleteLater()


# ---- Transducer preview helpers ------------------------------------------

def _eval_sensitivity(
    sens: "float | list[tuple[float, float]] | None",
    frequency: float,
) -> float:
    """Evaluate a (possibly frequency-dependent) sensitivity at ``frequency``.

    Mirrors :py:func:`openlifu.xdc.element.sensitivity_at_frequency` so the
    preview dialog can compute effective per-element sensitivity without
    depending on import-time access to that helper.
    """
    if sens is None:
        return 0.0
    if isinstance(sens, list):
        if not sens:
            return 0.0
        freqs = [float(f) for f, _ in sens]
        values = [float(v) for _, v in sens]
        return float(np.interp(float(frequency), freqs, values))
    return float(sens)


def _format_num(value: float, precision: int = 3) -> str:
    try:
        return np.format_float_positional(float(value), precision=precision, trim="-")
    except Exception:
        return str(value)


def _format_sensitivity(sens, frequency: float) -> str:
    """Human-readable summary of a sensitivity, evaluated at ``frequency`` if needed."""
    if isinstance(sens, list):
        if not sens:
            return "[]"
        eff = _eval_sensitivity(sens, frequency)
        return f"{_format_num(eff)} Pa/V (interp @ {_format_num(frequency, 0)} Hz, {len(sens)} pts)"
    return f"{_format_num(sens)} Pa/V"


class TransducerPreviewDialog(qt.QDialog):
    """Standalone two-panel preview of an openlifu Transducer or TransducerArray.

    Left panel: a :class:`QTreeWidget` showing the transducer hierarchy
    (array → modules → elements) with native expand/collapse. Right panel:
    an isolated 3D view showing the transducer geometry built from
    ``transducer.get_polydata()`` plus optional body and registration
    surface meshes. Nothing is added to the data parameter node, and all
    temporary nodes are removed when the dialog closes.
    """

    def __init__(
        self,
        transducer: "openlifu.xdc.Transducer",
        body_abspath: Optional[str] = None,
        registration_surface_abspath: Optional[str] = None,
        parent="mainWindow",
    ):
        super().__init__(slicer.util.mainWindow() if parent == "mainWindow" else parent)
        self.transducer = transducer
        self._body_abspath = body_abspath
        self._registration_surface_abspath = registration_surface_abspath
        self._main_scene = slicer.mrmlScene
        self._scene = slicer.vtkMRMLScene()
        self._owned_nodes = []
        self._model_node = None
        self._body_model_node = None
        self._registration_surface_model_node = None
        self._view_node = None
        self._view_owner_node = None
        self._scene_close_tag = None
        self._closed = False
        self.setWindowTitle(f"Transducer Preview - {getattr(transducer, 'name', transducer.id)}")
        self.setWindowModality(qt.Qt.WindowModal)
        self.finished.connect(self._cleanup)
        try:
            # The private scene keeps session objects out of this view.
            self._scene.AddNewNodeByClass("vtkMRMLSelectionNode")
            self._scene.AddNewNodeByClass("vtkMRMLInteractionNode")
            self._setup()
            self._setup_view_node()
            self._setup_model_node()
            self._setup_body_model_node()
            self._setup_registration_surface_node()
            self.viewWidget.threeDView().resetFocalPoint()
            self.viewWidget.threeDView().resetCamera()
            self._scene_close_tag = self._main_scene.AddObserver(
                self._main_scene.StartCloseEvent, self._on_scene_close,
            )
        except Exception:
            self._cleanup()
            self.deleteLater()
            raise

    def _setup(self) -> None:
        screen = qt.QDesktopWidget().screenGeometry()
        self.resize(int(screen.width() * 0.55), int(screen.height() * 0.55))
        self.setMinimumWidth(700)
        self.setMinimumHeight(450)

        outer = qt.QVBoxLayout()
        self.setLayout(outer)

        splitter = qt.QSplitter(qt.Qt.Horizontal, self)
        outer.addWidget(splitter, 1)

        # Left panel: native Qt tree with collapsible sections. This avoids
        # spinning up a Chromium runtime just to render <details>/<summary>.
        self.infoTree = qt.QTreeWidget(splitter)
        self.infoTree.setColumnCount(2)
        self.infoTree.setHeaderLabels(["Field", "Value"])
        self.infoTree.setMinimumWidth(360)
        self.infoTree.setSizePolicy(qt.QSizePolicy.Preferred, qt.QSizePolicy.Expanding)
        self.infoTree.setAlternatingRowColors(True)
        self.infoTree.setRootIsDecorated(True)
        self.infoTree.setUniformRowHeights(True)
        self.infoTree.setSelectionMode(qt.QAbstractItemView.NoSelection)
        # Wider "Field" column by default; user can still drag to resize.
        header = self.infoTree.header()
        header.setSectionResizeMode(0, qt.QHeaderView.Interactive)
        header.setSectionResizeMode(1, qt.QHeaderView.Stretch)
        header.setStretchLastSection(True)
        self.infoTree.setColumnWidth(0, 200)
        try:
            self._populate_info_tree()
        except Exception as e:
            logging.warning("TransducerPreviewDialog: failed to populate info tree: %s", e)
            self.infoTree.clear()
            err_item = qt.QTreeWidgetItem(["error", str(e)])
            self.infoTree.addTopLevelItem(err_item)
        self.infoTree.expandToDepth(0)

        # Right panel: 3D view widget bound to a private view node
        self.viewWidget = slicer.qMRMLThreeDWidget(splitter)
        self.viewWidget.setMRMLScene(self._scene)
        self.viewWidget.setMinimumHeight(300)
        self.viewWidget.setSizePolicy(qt.QSizePolicy.Expanding, qt.QSizePolicy.Expanding)

        splitter.addWidget(self.infoTree)
        splitter.addWidget(self.viewWidget)
        splitter.setStretchFactor(0, 1)
        splitter.setStretchFactor(1, 2)
        splitter.setSizes([440, 560])

        # Close button
        bb = qt.QDialogButtonBox()
        bb.setStandardButtons(qt.QDialogButtonBox.Close)
        bb.rejected.connect(self.reject)
        bb.accepted.connect(self.accept)
        outer.addWidget(bb)

    # ---- Info tree population --------------------------------------------

    def _populate_info_tree(self) -> None:
        """Build the left-panel tree from the transducer object.

        Handles both ``TransducerArray`` (has ``modules``) and a plain
        ``Transducer`` (has ``elements``). Per-element sensitivity values
        are shown as the *effective* product of the element's stored
        sensitivity and the parent module's sensitivity, evaluated at the
        module's center frequency when frequency-dependent.
        """
        tree = self.infoTree
        tree.clear()

        obj = self.transducer
        is_array = hasattr(obj, "modules")

        def _make_item(key: str, value: str) -> qt.QTreeWidgetItem:
            """Create a tree item with key/value strings and tooltips on each cell."""
            item = qt.QTreeWidgetItem([str(key), str(value)])
            # Tooltips show the full text on hover so users can read content
            # that is clipped by the column width.
            item.setToolTip(0, str(key))
            item.setToolTip(1, str(value))
            return item

        def add_kv(parent, key: str, value: str) -> qt.QTreeWidgetItem:
            item = _make_item(key, value)
            if parent is None:
                tree.addTopLevelItem(item)
            else:
                parent.addChild(item)
            return item

        def add_branch(parent, key: str, value: str) -> qt.QTreeWidgetItem:
            """Create a tree item intended to hold children (gets tooltips too)."""
            item = _make_item(key, value)
            if parent is None:
                tree.addTopLevelItem(item)
            else:
                parent.addChild(item)
            return item

        def add_attrs(parent, attrs: dict) -> None:
            if not attrs:
                return
            attrs_item = add_branch(parent, "Attrs", f"{len(attrs)} key(s)")
            for k in sorted(str(k) for k in attrs):
                v = attrs[k]
                # Compact display for arrays / lists / dicts
                if isinstance(v, np.ndarray):
                    val_str = f"ndarray shape={v.shape} dtype={v.dtype}"
                elif isinstance(v, (list, tuple)):
                    val_str = f"{type(v).__name__}(len={len(v)})"
                elif isinstance(v, dict):
                    val_str = f"dict({len(v)} key(s))"
                else:
                    val_str = str(v)
                add_kv(attrs_item, k, val_str)

        def add_element(parent_item, element, module_freq: float, module_sens) -> None:
            module_sens_at_f = _eval_sensitivity(module_sens, module_freq)
            try:
                eff_sens = (
                    [(f, v * module_sens_at_f) for f, v in element.sensitivity]
                    if isinstance(element.sensitivity, list)
                    else float(element.sensitivity) * module_sens_at_f
                )
            except Exception:
                eff_sens = element.sensitivity
            sens_summary = _format_sensitivity(eff_sens, module_freq)
            elem_label = f"#{element.index} (pin {element.pin})"
            elem_summary = (
                f"pos [{_format_num(element.position[0])}, "
                f"{_format_num(element.position[1])}, "
                f"{_format_num(element.position[2])}]  "
                f"size [{_format_num(element.size[0])}, {_format_num(element.size[1])}]"
            )
            elem_item = add_branch(parent_item, elem_label, elem_summary)
            add_kv(elem_item, "Index", str(element.index))
            add_kv(elem_item, "Pin", str(element.pin))
            add_kv(elem_item, "Units", str(getattr(element, "units", "")))
            add_kv(
                elem_item,
                "Position",
                f"[{_format_num(element.position[0])}, "
                f"{_format_num(element.position[1])}, "
                f"{_format_num(element.position[2])}]",
            )
            try:
                az_deg, el_deg, roll_deg = np.degrees([element.az, element.el, element.roll])
                add_kv(
                    elem_item,
                    "Orientation (deg)",
                    f"az={_format_num(az_deg)}, el={_format_num(el_deg)}, roll={_format_num(roll_deg)}",
                )
            except Exception:
                pass
            add_kv(
                elem_item,
                "Size",
                f"[{_format_num(element.size[0])}, {_format_num(element.size[1])}]",
            )
            add_kv(elem_item, "Sensitivity (effective)", sens_summary)
            if isinstance(element.sensitivity, list):
                stored_item = add_branch(
                    elem_item,
                    "Sensitivity (stored)",
                    f"{len(element.sensitivity)} (freq, value) point(s)",
                )
                for f, v in element.sensitivity:
                    add_kv(stored_item, f"{_format_num(f, 0)} Hz", f"{_format_num(v)}")
            else:
                add_kv(elem_item, "Sensitivity (stored)", _format_num(element.sensitivity))

        def add_transducer(parent_item, t) -> None:
            """Populate fields and elements of a Transducer-like object.

            ``parent_item`` may be ``None`` to add directly at the top level
            of the tree.
            """
            n_elements = t.numelements() if hasattr(t, "numelements") else len(getattr(t, "elements", []))
            add_kv(parent_item, "ID", str(getattr(t, "id", "")))
            add_kv(parent_item, "Name", str(getattr(t, "name", "")))
            add_kv(parent_item, "Frequency", f"{_format_num(getattr(t, 'frequency', 0.0), 0)} Hz")
            add_kv(parent_item, "Units", str(getattr(t, "units", "")))
            add_kv(parent_item, "Sensitivity", _format_sensitivity(getattr(t, "sensitivity", 1.0), getattr(t, "frequency", 0.0)))
            add_kv(
                parent_item,
                "Crosstalk",
                f"frac={_format_num(getattr(t, 'crosstalk_frac', 0.0))}, "
                f"dist={_format_num(getattr(t, 'crosstalk_dist', 0.0))} m",
            )
            add_kv(parent_item, "Registration mesh", str(getattr(t, "registration_surface_filename", None)))
            add_kv(parent_item, "Body mesh", str(getattr(t, "transducer_body_filename", None)))

            elements_item = add_branch(parent_item, "Elements", f"{n_elements} element(s)")
            for el in getattr(t, "elements", []):
                add_element(elements_item, el, getattr(t, "frequency", 0.0), getattr(t, "sensitivity", 1.0))

            add_attrs(parent_item, getattr(t, "attrs", {}) or {})

        if is_array:
            total_elements = sum(m.numelements() for m in obj.modules)
            # Top-level summary items (no redundant root "TransducerArray" node)
            add_kv(None, "ID", str(getattr(obj, "id", "")))
            add_kv(None, "Name", str(getattr(obj, "name", "")))
            add_kv(None, "Total Elements", str(total_elements))

            modules_item = add_branch(None, "Modules", f"{len(obj.modules)} module(s)")
            for i, m in enumerate(obj.modules):
                hwid = (m.attrs or {}).get("hwid") if hasattr(m, "attrs") else None
                tx, ty, tz = (
                    m.transform[0, 3], m.transform[1, 3], m.transform[2, 3]
                ) if hasattr(m, "transform") else (0.0, 0.0, 0.0)
                module_summary = (
                    f"{getattr(m, 'id', '')} | {m.numelements()} els | "
                    f"HWID={hwid} | "
                    f"t=[{_format_num(tx)}, {_format_num(ty)}, {_format_num(tz)}]"
                )
                module_item = add_branch(modules_item, f"Module {i}", module_summary)
                add_transducer(module_item, m)

            add_attrs(None, getattr(obj, "attrs", {}) or {})
            modules_item.setExpanded(True)
        else:
            # Plain Transducer: populate top-level fields directly.
            add_transducer(None, obj)

    def _track_node(self, node):
        if node is not None and node not in self._owned_nodes:
            node.SetSaveWithScene(False)
            self._owned_nodes.append(node)
        return node

    def _setup_view_node(self) -> None:
        tid = self.transducer.id
        self._view_owner_node = self._track_node(
            self._scene.AddNewNodeByClass("vtkMRMLScriptedModuleNode")
        )
        view_node = slicer.vtkMRMLViewNode()
        view_node.SetLayoutName(f"TransducerPreview-{self._view_owner_node.GetID()}")
        view_node.SetLayoutLabel("XDC")
        view_node.SetLayoutColor([0.30, 0.55, 0.85])
        view_node.SetName(f"view-preview-{tid}")
        # Set the owner before insertion so the main layout does not create another view.
        view_node.SetAndObserveParentLayoutNodeID(self._view_owner_node.GetID())
        self._view_node = self._track_node(self._scene.AddNode(view_node))
        self._view_node.SetAttribute("isWizardViewNode", "true")
        self._view_node.SetBackgroundColor(0.20, 0.25, 0.35)
        self._view_node.SetBackgroundColor2(0.10, 0.12, 0.18)
        self._view_node.SetBoxVisible(False)
        self._view_node.SetAxisLabelsVisible(False)
        self.viewWidget.setMRMLViewNode(self._view_node)
        self._track_node(self.viewWidget.threeDView().cameraNode())

    def _configure_model(self, model, color, opacity) -> None:
        model.CreateDefaultDisplayNodes()
        display = self._track_node(model.GetDisplayNode())
        display.SetScalarVisibility(False)
        display.SetColor(*color)
        display.SetOpacity(opacity)
        display.SetViewNodeIDs([self._view_node.GetID()])
        display.SetVisibility(True)

    def _setup_model_node(self) -> None:
        self._preview_transducer = (
            self.transducer.to_transducer() if hasattr(self.transducer, "to_transducer") else self.transducer
        )
        polydata = self._preview_transducer.get_polydata(units="mm")
        self._model_node = self._track_node(self._scene.AddNewNodeByClass(
            "vtkMRMLModelNode", f"TransducerPreview-{self.transducer.id}",
        ))
        self._model_node.SetAndObservePolyData(polydata)
        self._configure_model(self._model_node, (0.0, 0.8, 1.0), 1.0)

    def _load_preview_mesh(self, path, name, color, opacity):
        from openlifu.util.units import getunitconversion

        if not Path(path).is_file():
            raise ValueError(f"Transducer mesh does not exist: {path}")
        model = self._track_node(self._scene.AddNewNodeByClass("vtkMRMLModelNode", name))
        storage = self._track_node(self._scene.AddNewNodeByClass("vtkMRMLModelStorageNode"))
        storage.SetFileName(str(path))
        model.SetAndObserveStorageNodeID(storage.GetID())
        if not storage.ReadData(model):
            raise ValueError(f"Could not read transducer mesh: {path}")
        scale = getunitconversion(self._preview_transducer.units, "mm")
        if scale != 1:
            transform = vtk.vtkTransform()
            transform.Scale(scale, scale, scale)
            scaled = vtk.vtkTransformPolyDataFilter()
            scaled.SetInputData(model.GetPolyData())
            scaled.SetTransform(transform)
            scaled.Update()
            polydata = vtk.vtkPolyData()
            polydata.DeepCopy(scaled.GetOutput())
            model.SetAndObservePolyData(polydata)
        self._configure_model(model, color, opacity)
        return model

    def _setup_body_model_node(self) -> None:
        if self._body_abspath:
            self._body_model_node = self._load_preview_mesh(
                self._body_abspath, f"TransducerPreviewBody-{self.transducer.id}", (0.85, 0.85, 0.88), 0.45,
            )

    def _setup_registration_surface_node(self) -> None:
        if self._registration_surface_abspath:
            self._registration_surface_model_node = self._load_preview_mesh(
                self._registration_surface_abspath,
                f"TransducerPreviewSurface-{self.transducer.id}", (0.45, 0.85, 0.55), 0.55,
            )

    def _on_scene_close(self, *args) -> None:
        self._cleanup()
        self.reject()

    def _cleanup(self, *args) -> None:
        if self._closed:
            return
        self._closed = True
        if self._scene_close_tag is not None:
            self._main_scene.RemoveObserver(self._scene_close_tag)
            self._scene_close_tag = None
        if self._view_node is not None:
            layout_name = self._view_node.GetLayoutName()
            for index in range(self._scene.GetNumberOfNodes()):
                node = self._scene.GetNthNode(index)
                if node.IsA("vtkMRMLCameraNode") and node.GetLayoutName() == layout_name:
                    self._track_node(node)
        view_widget = getattr(self, "viewWidget", None)
        if view_widget is not None:
            view_widget.threeDView().setMRMLViewNode(None)
            view_widget.setMRMLScene(None)
        # Remove the view before its camera so camera logic does not recreate it.
        nodes = [self._view_node, *reversed(self._owned_nodes)]
        for node in nodes:
            if node is not None and node.GetScene() == self._scene:
                self._scene.RemoveNode(node)
        self._scene.Clear(1)
        self._owned_nodes.clear()
        self._model_node = None
        self._body_model_node = None
        self._registration_surface_model_node = None
        self._view_node = None
        self._view_owner_node = None


class TransducerManagerDialog(qt.QDialog):
    """Browse, import, preview and delete database transducer definitions."""

    def __init__(self, db: "openlifu.db.Database", parent="mainWindow"):
        super().__init__(slicer.util.mainWindow() if parent == "mainWindow" else parent)
        self.setWindowTitle("Manage Transducers")
        self.setWindowModality(qt.Qt.WindowModal)
        self.db = db
        self._setup()
        self.refresh()

    # ---- UI ----
    def _setup(self) -> None:
        layout = qt.QVBoxLayout()
        self.setLayout(layout)

        cols = ["", "ID", "Name", "Modules"]
        self.table = qt.QTableWidget(self)
        self.table.setColumnCount(len(cols))
        self.table.setHorizontalHeaderLabels(cols)
        self.table.setSelectionBehavior(qt.QAbstractItemView.SelectRows)
        self.table.setSelectionMode(qt.QAbstractItemView.SingleSelection)
        self.table.setEditTriggers(qt.QAbstractItemView.NoEditTriggers)
        self.table.verticalHeader().setVisible(False)
        self.table.setShowGrid(False)
        self.table.horizontalHeader().setHighlightSections(False)
        self.table.horizontalHeader().setStretchLastSection(True)
        header = self.table.horizontalHeader()
        header.setSectionResizeMode(0, qt.QHeaderView.Fixed)
        self.table.setColumnWidth(0, 22)
        header.setSectionResizeMode(1, qt.QHeaderView.ResizeToContents)
        header.setSectionResizeMode(2, qt.QHeaderView.ResizeToContents)
        header.setSectionResizeMode(3, qt.QHeaderView.Stretch)
        self.table.setSortingEnabled(True)
        layout.addWidget(self.table)

        # Action buttons
        actionRow = qt.QHBoxLayout()
        self.addFileButton = qt.QPushButton("Add from File")
        self.addFileButton.setToolTip("Import a transducer definition and its meshes into the database")
        self.addDeviceButton = qt.QPushButton("Add from Device")
        self.addDeviceButton.setToolTip(
            "Assemble a transducer definition from the connected TX modules' user_configs"
        )
        self.previewButton = qt.QPushButton("Preview")
        self.previewButton.setToolTip("Open a standalone preview of the selected transducer")
        self.deleteButton = qt.QPushButton("Delete")
        self.deleteButton.setToolTip("Remove the selected transducer from the database")
        for b in (self.addFileButton, self.addDeviceButton, self.previewButton, self.deleteButton):
            b.setSizePolicy(qt.QSizePolicy.Expanding, qt.QSizePolicy.Preferred)
            actionRow.addWidget(b)
        layout.addLayout(actionRow)

        # Close
        bb = qt.QDialogButtonBox()
        bb.addButton("Close", qt.QDialogButtonBox.RejectRole)
        bb.rejected.connect(self.reject)
        layout.addWidget(bb)

        self.addFileButton.clicked.connect(self.onAddFromFile)
        self.addDeviceButton.clicked.connect(self.onAddFromDevice)
        self.previewButton.clicked.connect(self.onPreview)
        self.deleteButton.clicked.connect(self.onDelete)
        self.table.doubleClicked.connect(self.onPreview)

        screen = qt.QDesktopWidget().screenGeometry()
        self.resize(int(screen.width() * 0.45), int(screen.height() * 0.35))

    # ---- LED helpers ----
    @staticmethod
    def _make_led_icon(color: str) -> qt.QIcon:
        """Build a small filled-circle icon in the requested CSS color."""
        pix = qt.QPixmap(16, 16)
        pix.fill(qt.Qt.transparent)
        painter = qt.QPainter(pix)
        try:
            painter.setRenderHint(qt.QPainter.Antialiasing, True)
            painter.setPen(qt.QPen(qt.QColor("#444"), 1))
            painter.setBrush(qt.QBrush(qt.QColor(color)))
            painter.drawEllipse(2, 2, 12, 12)
        finally:
            painter.end()
        return qt.QIcon(pix)

    def _connected_device_id(self) -> Optional[str]:
        """Return the ``device.id`` from the lead-module user_config of the connected device.

        Returns ``None`` if no device is connected, or the lead module carries
        no ``device`` block, or any read fails.
        """
        try:
            sc_logic = slicer.util.getModuleLogic("OpenLIFUSonicationControl")
            iface = getattr(sc_logic, "cur_lifu_interface", None)
            if iface is None:
                return None
            tx_conn, _hv = iface.is_device_connected()
            if not tx_conn:
                return None
            cfg = iface.txdevice.read_config(module=0)
            if cfg is None:
                return None
            data = json.loads(cfg.get_json_str())
            dev = data.get("device") or {}
            did = dev.get("id")
            return did if isinstance(did, str) and did else None
        except Exception as e:
            logging.debug("Transducer manager: could not query connected device id: %s", e)
            return None

    # ---- Table population ----
    def refresh(self) -> None:
        self.table.setSortingEnabled(False)
        self.table.clearContents()
        try:
            ids = list(self.db.get_transducer_ids() or [])
        except Exception as e:
            logging.warning("Could not list transducer ids: %s", e)
            ids = []
        connected_id = self._connected_device_id()
        on_icon = self._make_led_icon("#2ecc71")   # green
        off_icon = self._make_led_icon("#444")     # dim gray

        self.table.setRowCount(len(ids))
        for row, tid in enumerate(ids):
            # Resolve metadata via a non-converting load so we get the TransducerArray.
            try:
                obj = self.db.load_transducer(tid, convert_array=False)
                name = getattr(obj, "name", tid)
                if hasattr(obj, "modules"):
                    n_mods = len(obj.modules)
                else:
                    n_mods = 1
            except Exception as e:
                logging.warning("Could not load transducer %s for listing: %s", tid, e)
                name = tid
                n_mods = 0

            led_item = qt.QTableWidgetItem()
            led_item.setIcon(on_icon if (connected_id and tid == connected_id) else off_icon)
            led_item.setFlags(qt.Qt.ItemIsEnabled | qt.Qt.ItemIsSelectable)
            led_item.setToolTip("Connected" if (connected_id and tid == connected_id) else "")
            self.table.setItem(row, 0, led_item)
            self.table.setItem(row, 1, qt.QTableWidgetItem(str(tid)))
            self.table.setItem(row, 2, qt.QTableWidgetItem(str(name)))
            self.table.setItem(row, 3, qt.QTableWidgetItem(str(n_mods)))

        self.table.resizeRowsToContents()
        self.table.setSortingEnabled(True)
        self._update_permissions()

    def _selected_transducer_id(self) -> Optional[str]:
        items = self.table.selectedItems()
        if not items:
            return None
        row = items[0].row()
        idItem = self.table.item(row, 1)
        return idItem.text() if idItem else None

    def _can_mutate(self) -> bool:
        return get_cur_db() is self.db and (
            not get_user_account_mode_state() or "admin" in get_current_user().roles
        )

    def _update_permissions(self) -> None:
        allowed = self._can_mutate()
        for button in (self.addFileButton, self.addDeviceButton, self.deleteButton):
            button.setEnabled(allowed)

    def _mutation_context(self) -> dict:
        if get_cur_db() is not self.db:
            raise RuntimeError("The database connection changed. Close and reopen the transducer manager.")
        if get_user_account_mode_state() and "admin" not in get_current_user().roles:
            raise PermissionError("An administrator account is required to change transducers.")
        state = get_openlifu_data_parameter_node()
        return {
            "loaded_ids": tuple(state.loaded_transducers),
            "active_transducer_id": (
                state.loaded_session.get_transducer_id() if state.loaded_session is not None else None
            ),
        }

    def _confirm_overwrite(self, transducer) -> Optional[bool]:
        context = self._mutation_context()
        transducer_id = validate_transducer_id(transducer.id)
        ensure_transducer_not_referenced(self.db, transducer_id, **context)
        if transducer_id not in self.db.get_transducer_ids():
            return False
        confirmed = slicer.util.confirmYesNoDisplay(
            f"Transducer '{transducer_id}' already exists in the database. Overwrite it?",
            "Overwrite transducer",
            parent=self,
        )
        return True if confirmed else None

    def _save_definition(self, transducer, source_dir) -> bool:
        overwrite = self._confirm_overwrite(transducer)
        if overwrite is None:
            return False
        warning = save_transducer(
            self.db, transducer, source_dir, overwrite=overwrite, **self._mutation_context(),
        )
        self.refresh()
        if warning:
            slicer.util.warningDisplay(warning, parent=self)
        return True

    @display_errors
    def onAddFromFile(self, checked: bool = False) -> None:
        import openlifu.xdc.util

        self._mutation_context()
        filepath = qt.QFileDialog.getOpenFileName(
            self, "Import transducer", qt.QSettings().value("OpenLIFU/databaseDirectory", "."),
            "Transducers (*.json);;All Files (*)",
        )
        if not filepath:
            return
        self._mutation_context()
        transducer = openlifu.xdc.util.load_transducer_from_file(filepath, convert_array=False)
        self._save_definition(transducer, Path(filepath).parent)

    def _load_template(self, template_id: str, user_configs: List[dict]):
        from openlifu.xdc import TransducerArray

        validate_transducer_id(template_id)
        template = self.db.load_transducer(template_id, convert_array=False)
        if not isinstance(template, TransducerArray):
            raise ValueError(f"Template '{template_id}' must be a transducer array.")
        if len(template.modules) != len(user_configs):
            raise ValueError(
                f"Template '{template_id}' has {len(template.modules)} modules, "
                f"but {len(user_configs)} are connected."
            )
        frequency = _connected_frequency_khz(user_configs)
        if frequency is not None:
            for module in template.modules:
                template_frequency = getattr(module, "frequency", None)
                if template_frequency and not np.isclose(float(template_frequency), frequency * 1000, rtol=1e-6, atol=0):
                    raise ValueError(f"Template '{template_id}' does not match the connected {frequency:g} kHz modules.")
        source_dir = Path(self.db.get_transducer_filename(template_id)).parent
        for obj in (template, *template.modules):
            for field in ("registration_surface_filename", "transducer_body_filename"):
                filename = getattr(obj, field, None)
                if filename is not None and (not filename or not (source_dir / filename).is_file()):
                    raise ValueError(f"Template '{template_id}' references a missing mesh: {filename}")
        return template, source_dir

    def _choose_template_id(self, template_ids: List[str], inferred_template_id: Optional[str]) -> Optional[str]:
        dialog = qt.QDialog(self)
        dialog.setWindowTitle("Choose Transducer Template")
        layout = qt.QVBoxLayout(dialog)
        text = "Choose a saved array template for the connected modules."
        if inferred_template_id:
            text = f"Template '{inferred_template_id}' is not in this database. " + text
        label = qt.QLabel(text)
        label.setWordWrap(True)
        layout.addWidget(label)
        combo = qt.QComboBox()
        for template_id in template_ids:
            template = self.db.load_transducer(template_id, convert_array=False)
            combo.addItem(f"{template.name} [{template_id}]")
        combo.setCurrentIndex(-1)
        layout.addWidget(combo)
        buttons = qt.QDialogButtonBox()
        buttons.setStandardButtons(qt.QDialogButtonBox.Ok | qt.QDialogButtonBox.Cancel)
        buttons.button(qt.QDialogButtonBox.Ok).setEnabled(False)
        combo.currentIndexChanged.connect(
            lambda index: buttons.button(qt.QDialogButtonBox.Ok).setEnabled(index >= 0)
        )
        buttons.accepted.connect(dialog.accept)
        buttons.rejected.connect(dialog.reject)
        layout.addWidget(buttons)
        try:
            if not dialog.exec_():
                return None
            return template_ids[combo.currentIndex] if combo.currentIndex >= 0 else None
        finally:
            dialog.deleteLater()

    def _select_template_id(self, user_configs: List[dict], inferred_template_id: Optional[str]) -> Optional[str]:
        candidates = []
        for template_id in self.db.get_transducer_ids():
            try:
                self._load_template(template_id, user_configs)
            except Exception as error:
                logging.debug("Template %s is not suitable: %s", template_id, error)
                continue
            candidates.append(template_id)
        if not candidates:
            raise ValueError(
                "No saved array template matches these modules. Import a template with the "
                "correct module count, frequency and mesh files, then try again."
            )
        return self._choose_template_id(sorted(candidates), inferred_template_id)

    @display_errors
    def onAddFromDevice(self, checked: bool = False) -> None:
        from openlifu.xdc import TransducerArray

        self._mutation_context()
        control = slicer.util.getModuleLogic("OpenLIFUSonicationControl")
        iface = getattr(control, "cur_lifu_interface", None)
        if iface is None:
            raise RuntimeError("Open Sonication Control to initialize the hardware interface first.")
        if not iface.is_device_connected()[0]:
            raise RuntimeError("No TX device is currently connected.")
        user_configs = _read_connected_user_configs(iface)
        template_id, device_block = _resolve_template_id_for_user_configs(user_configs)
        recorded_template = device_block.get("template") if device_block else None
        if template_id not in self.db.get_transducer_ids():
            if recorded_template is not None:
                raise ValueError(f"Recorded template '{template_id}' is not in the loaded database.")
            template_id = self._select_template_id(user_configs, template_id)
            if template_id is None:
                return
        self._mutation_context()
        template, source_dir = self._load_template(template_id, user_configs)
        dialog = DeviceConfigEditDialog(
            template_id=template_id,
            module_hwids=[str(config.get("hwid")) for config in user_configs],
            initial_id=str((device_block or {}).get("id") or ""),
            initial_name=str((device_block or {}).get("name") or ""),
            parent=self,
        )
        accepted, transducer_id, transducer_name = dialog.customexec_()
        if not accepted:
            return
        self._mutation_context()
        validate_transducer_id(transducer_id)
        transducer = TransducerArray.from_module_user_configs(
            user_configs, template=template, arr_id=transducer_id, arr_name=transducer_name,
        )
        if not self._save_definition(transducer, source_dir):
            return
        slicer.util.infoDisplay(
            f"Saved transducer '{transducer.id}' to the database.",
            windowTitle="Add from Device", parent=self,
        )
        if slicer.util.confirmYesNoDisplay(
            "Write this transducer's device definition back to module 0? "
            "This changes the identity reported by the connected device.",
            "Write device config", parent=self,
        ):
            try:
                self._write_device_block_to_module0(iface, transducer, template_id, user_configs)
            except Exception as error:
                raise RuntimeError(
                    f"The transducer was saved in the database, but device writeback failed: {error}"
                ) from error
            finally:
                self.refresh()

    def _write_device_block_to_module0(self, iface, arr, template_id: str, user_configs: List[dict]) -> None:
        """Verify the captured hardware, then update only its current device block."""
        self._mutation_context()
        control = slicer.util.getModuleLogic("OpenLIFUSonicationControl")
        if getattr(control, "cur_lifu_interface", None) is not iface or not iface.is_device_connected()[0]:
            raise RuntimeError("The hardware connection changed. Device configuration was not written.")
        current = _read_connected_user_configs(iface)
        identity_fields = ("hwid", "sn", "freq", "module", "device")
        snapshot = lambda configs: [
            {field: config.get(field) for field in identity_fields} for config in configs
        ]
        if snapshot(current) != snapshot(user_configs):
            raise RuntimeError("Connected module identity or calibration changed. Device configuration was not written.")
        if any(not isinstance(config.get("hwid"), str) or not config["hwid"] for config in current):
            raise RuntimeError("Cannot verify hardware identity because a module has no hardware ID.")
        new_device = arr.to_device_config()
        new_device["template"] = template_id
        updated = copy.deepcopy(current[0])
        updated["device"] = new_device
        self._mutation_context()
        result = iface.txdevice.write_config_json(json.dumps(updated), module=0)
        if result is None or result is False:
            raise RuntimeError("The hardware did not confirm the device configuration write.")
        if hasattr(result, "get_json_str"):
            returned = json.loads(result.get_json_str())
            if returned.get("device") != new_device:
                raise RuntimeError("The hardware returned a different device definition after writing.")
        logging.info("Updated device definition for transducer %s", arr.id)

    @display_errors
    def onPreview(self, *args) -> None:
        tid = self._selected_transducer_id()
        if not tid:
            slicer.util.errorDisplay("Select a transducer first.", parent=self)
            return
        try:
            obj = self.db.load_transducer(tid, convert_array=False)
        except Exception as e:
            slicer.util.errorDisplay(f"Could not load transducer '{tid}': {e}", parent=self)
            return
        try:
            abspaths = self.db.get_transducer_absolute_filepaths(tid) or {}
        except Exception:
            abspaths = {}
        body_path = abspaths.get("transducer_body_abspath")
        registration_path = abspaths.get("registration_surface_abspath")
        dialog = TransducerPreviewDialog(
            obj,
            body_abspath=body_path,
            registration_surface_abspath=registration_path,
            parent=self,
        )
        try:
            dialog.exec_()
        finally:
            dialog.deleteLater()

    @display_errors
    def onDelete(self, *args) -> None:
        transducer_id = self._selected_transducer_id()
        if not transducer_id:
            raise ValueError("Select a transducer first.")
        ensure_transducer_not_referenced(self.db, transducer_id, **self._mutation_context())
        message = f"Delete transducer '{transducer_id}' from the database?"
        if transducer_id in PROTECTED_TRANSDUCER_IDS:
            message += " This built-in definition is used as a template when importing connected devices."
        if not slicer.util.confirmYesNoDisplay(message, "Delete transducer", parent=self):
            return
        self._delete_transducer_from_db(transducer_id)
        self.refresh()

    def _delete_transducer_from_db(self, transducer_id: str) -> None:
        warning = delete_transducer(self.db, transducer_id, **self._mutation_context())
        if warning:
            slicer.util.warningDisplay(warning, parent=self)
