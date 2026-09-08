"""Transducer manager dialogs."""

import json
import logging
from pathlib import Path
from typing import Dict, List, Optional, Tuple, TYPE_CHECKING

import numpy as np
import qt
import slicer
from slicer.i18n import tr as _

from OpenLIFULib.util import display_errors

if TYPE_CHECKING:
    import openlifu.db
    import openlifu.xdc


# ---------------------------------------------------------------------------
# Transducer Manager
# ---------------------------------------------------------------------------

# IDs that ship as part of the openlifu reference database and are required
# as templates when constructing a TransducerArray from a connected device.
# Deleting one of these is gated behind a confirmation dialog.
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
        user_configs.append(json.loads(cfg.get_json_str()))
    return user_configs


def _module_count_from_template_id(template_id: str) -> Optional[int]:
    """Parse the leading module count out of a template id like ``openlifu_2x400``.

    Returns ``None`` if the id does not match the expected ``..._<N>x<freq>``
    convention.
    """
    if not isinstance(template_id, str):
        return None
    tail = template_id.rsplit("_", 1)[-1]  # e.g. "2x400"
    if "x" not in tail:
        return None
    head = tail.split("x", 1)[0]
    try:
        return int(head)
    except ValueError:
        return None


def _resolve_template_id_for_user_configs(user_configs: List[dict]) -> Tuple[str, Optional[dict]]:
    """Return ``(template_id, device_block)`` for a list of module user_configs.

    Prefers ``user_configs[0]["device"]["template"]`` if present **and** its
    encoded module count agrees with ``len(user_configs)``. Otherwise falls
    back to ``(n_modules, freq)``. ``device_block`` is the parsed ``device``
    sub-dict of the lead module or ``None`` if no such block exists.

    The consistency check guards against stale device metadata: e.g. a 2x
    transducer whose module 0 was once provisioned with a ``openlifu_1x400``
    template would otherwise be reported as 1x400 even after the second
    module is connected.

    Raises:
        RuntimeError: if neither source can resolve a template id (e.g. all
            modules omit ``freq``).
    """
    device_block = user_configs[0].get("device") or None
    if device_block:
        tid = device_block.get("template")
        if isinstance(tid, str) and tid:
            recorded_count = _module_count_from_template_id(tid)
            if recorded_count is None or recorded_count == len(user_configs):
                return tid, device_block
            # Stale / mismatched template id on module 0 -- ignore it and
            # fall through to the (count, freq) inference below.
            logging.warning(
                "Ignoring stale device.template=%r on module 0: it claims %d "
                "module(s) but %d are currently connected. Falling back to "
                "count+freq inference.",
                tid, recorded_count, len(user_configs),
            )

    freqs = {c.get("freq") for c in user_configs if c.get("freq") is not None}
    if len(freqs) > 1:
        raise RuntimeError(
            f"Connected TX modules report mismatched frequencies: {sorted(freqs)}"
        )
    freq = next(iter(freqs)) if freqs else None
    if freq is None:
        raise RuntimeError(
            "Connected TX modules do not report a frequency; cannot infer "
            "a default template id."
        )
    tid = _TEMPLATE_IDS_BY_COUNT_FREQ.get((len(user_configs), int(freq)))
    if tid is None:
        raise RuntimeError(
            f"No default template is defined for {len(user_configs)} module(s) "
            f"at {int(freq)} kHz."
        )
    return tid, device_block


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
            "Inferred from the number of connected modules and their reported "
            "frequency. The template provides the mesh files."
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
        if not self.idEdit.text.strip():
            slicer.util.errorDisplay("Transducer ID is required.", parent=self)
            return
        if not self.nameEdit.text.strip():
            slicer.util.errorDisplay("Transducer name is required.", parent=self)
            return
        self.accept()

    def customexec_(self) -> Tuple[int, str, str]:
        rc = self.exec_()
        return rc, self.idEdit.text.strip(), self.nameEdit.text.strip()


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
        self._model_node = None
        self._body_model_node = None
        self._registration_surface_model_node = None
        self._view_node = None
        self._view_owner_node = None
        self.setWindowTitle(f"Transducer Preview - {getattr(transducer, 'name', getattr(transducer, 'id', ''))}")
        self.setWindowModality(qt.Qt.WindowModal)
        self._setup()
        self._setup_view_node()
        self._setup_model_node()
        self._setup_body_model_node()
        self._setup_registration_surface_node()
        # Reset the camera to fit the transducer in the preview view
        try:
            view_widget_3d = self.viewWidget.threeDView() if hasattr(self.viewWidget, "threeDView") else None
            if view_widget_3d is not None:
                view_widget_3d.resetFocalPoint()
                view_widget_3d.resetCamera()
        except Exception as e:
            logging.debug("TransducerPreviewDialog: could not reset camera: %s", e)
        self.finished.connect(self._cleanup)

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
        self.viewWidget.setMRMLScene(slicer.mrmlScene)
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

    def _setup_view_node(self) -> None:
        tid = getattr(self.transducer, "id", "transducer")
        layoutName = f"TransducerPreview-{tid}"
        # Owner node so the layout manager doesn't manage this view
        self._view_owner_node = slicer.mrmlScene.AddNewNodeByClass("vtkMRMLScriptedModuleNode")

        viewLogic = slicer.vtkMRMLViewLogic()
        viewLogic.SetMRMLScene(slicer.mrmlScene)
        viewNode = viewLogic.AddViewNode(layoutName)
        viewNode.SetLayoutLabel("XDC")
        viewNode.SetLayoutColor([0.30, 0.55, 0.85])
        viewNode.SetName(f"view-preview-{tid}")
        viewNode.SetAndObserveParentLayoutNodeID(self._view_owner_node.GetID())
        viewNode.SetAttribute("isWizardViewNode", "true")
        viewNode.SetBackgroundColor(0.20, 0.25, 0.35)
        viewNode.SetBackgroundColor2(0.10, 0.12, 0.18)
        viewNode.SetBoxVisible(False)
        viewNode.SetAxisLabelsVisible(False)
        self._view_node = viewNode
        self.viewWidget.setMRMLViewNode(viewNode)

    def _setup_model_node(self) -> None:
        try:
            # If we were handed a TransducerArray, convert to a flat Transducer
            # just for the purpose of building the preview polydata.
            if hasattr(self.transducer, "to_transducer"):
                mesh_source = self.transducer.to_transducer()
            else:
                mesh_source = self.transducer
            polydata = mesh_source.get_polydata(units="mm", facecolor=[0.0, 0.8, 1.0, 1.0])
        except Exception as e:
            logging.warning("TransducerPreviewDialog: failed to build polydata: %s", e)
            return
        modelNode = slicer.mrmlScene.AddNewNodeByClass(
            "vtkMRMLModelNode", f"TransducerPreview-{getattr(self.transducer, 'id', 'transducer')}"
        )
        modelNode.SetAndObservePolyData(polydata)
        modelNode.CreateDefaultDisplayNodes()
        displayNode = modelNode.GetDisplayNode()
        if displayNode is not None:
            # The polydata carries an unsigned-char color array as point
            # scalars; disable scalar coloring so VTK doesn't try to compute a
            # scalar range from it (which logs "Bad table range: [0, -1]").
            displayNode.SetScalarVisibility(False)
            displayNode.SetColor(0.0, 0.8, 1.0)
            displayNode.SetOpacity(1.0)
            displayNode.SetVisibility(True)
            # Restrict visibility to our private view node only
            displayNode.SetViewNodeIDs([self._view_node.GetID()])
        self._model_node = modelNode

    def _setup_body_model_node(self) -> None:
        """Optionally load the transducer body mesh into the preview view."""
        if not self._body_abspath:
            return
        try:
            expected_name = getattr(self.transducer, "transducer_body_filename", None)
            if expected_name and Path(self._body_abspath).name != expected_name:
                logging.warning(
                    "TransducerPreviewDialog: body file name mismatch (got %s, expected %s); skipping",
                    Path(self._body_abspath).name,
                    expected_name,
                )
                return
            bodyNode = slicer.util.loadModel(self._body_abspath)
        except Exception as e:
            logging.warning("TransducerPreviewDialog: could not load body mesh '%s': %s", self._body_abspath, e)
            return
        if bodyNode is None:
            return
        bodyNode.SetName(f"TransducerPreviewBody-{getattr(self.transducer, 'id', 'transducer')}")
        displayNode = bodyNode.GetDisplayNode()
        if displayNode is None:
            bodyNode.CreateDefaultDisplayNodes()
            displayNode = bodyNode.GetDisplayNode()
        if displayNode is not None:
            displayNode.SetColor(0.85, 0.85, 0.88)
            displayNode.SetOpacity(0.45)
            displayNode.SetVisibility(True)
            if self._view_node is not None:
                displayNode.SetViewNodeIDs([self._view_node.GetID()])
        self._body_model_node = bodyNode

    def _setup_registration_surface_node(self) -> None:
        """Optionally load the transducer registration surface mesh."""
        if not self._registration_surface_abspath:
            return
        try:
            expected_name = getattr(self.transducer, "registration_surface_filename", None)
            if expected_name and Path(self._registration_surface_abspath).name != expected_name:
                logging.warning(
                    "TransducerPreviewDialog: registration surface name mismatch (got %s, expected %s); skipping",
                    Path(self._registration_surface_abspath).name,
                    expected_name,
                )
                return
            surfNode = slicer.util.loadModel(self._registration_surface_abspath)
        except Exception as e:
            logging.warning(
                "TransducerPreviewDialog: could not load registration surface '%s': %s",
                self._registration_surface_abspath, e,
            )
            return
        if surfNode is None:
            return
        surfNode.SetName(
            f"TransducerPreviewSurface-{getattr(self.transducer, 'id', 'transducer')}"
        )
        displayNode = surfNode.GetDisplayNode()
        if displayNode is None:
            surfNode.CreateDefaultDisplayNodes()
            displayNode = surfNode.GetDisplayNode()
        if displayNode is not None:
            displayNode.SetColor(0.45, 0.85, 0.55)
            displayNode.SetOpacity(0.55)
            displayNode.SetVisibility(True)
            if self._view_node is not None:
                displayNode.SetViewNodeIDs([self._view_node.GetID()])
        self._registration_surface_model_node = surfNode

    def _cleanup(self, *args) -> None:
        for node_attr in (
            "_model_node",
            "_body_model_node",
            "_registration_surface_model_node",
            "_view_node",
            "_view_owner_node",
        ):
            node = getattr(self, node_attr, None)
            if node is not None:
                try:
                    slicer.mrmlScene.RemoveNode(node)
                except Exception as e:
                    logging.debug("TransducerPreviewDialog: cleanup failed for %s: %s", node_attr, e)
                setattr(self, node_attr, None)


class TransducerManagerDialog(qt.QDialog):
    """Tabular manager for transducer definitions stored in the loaded database.

    Columns: ``[LED, ID, Name, Modules]``. The LED on a row is lit green when
    the row's ID matches the ``device.id`` reported by the currently
    connected hardware's lead module. Actions:

    * **Add from File** -- existing ``load_transducer_from_file`` path.
    * **Add from Device** -- calls
      :py:meth:`openlifu.xdc.TransducerArray.get_connected` against the
      OpenLIFUSonicationControl module's ``LIFUInterface``; surfaces the
      "device config / hardware" validation errors from openlifu-python and
      prompts the user for ``id`` + ``name`` when the lead module carries no
      ``device`` block.
    * **Preview** -- opens a standalone two-panel dialog rendering the
      transducer geometry alongside its ``_repr_html_`` summary, without
      loading it into the main Slicer scene.
    * **Delete** -- removes the selected transducer from the database, with
      a special confirmation when one of the four built-in templates is
      targeted.
    """

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
        self.addFileButton.setToolTip("Load a transducer definition from a JSON file on disk")
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

    def _selected_transducer_id(self) -> Optional[str]:
        items = self.table.selectedItems()
        if not items:
            return None
        row = items[0].row()
        idItem = self.table.item(row, 1)
        return idItem.text() if idItem else None

    # ---- Actions ----
    @display_errors
    def onAddFromFile(self, checked: bool = False) -> None:
        qsettings = qt.QSettings()
        filepath: str = qt.QFileDialog.getOpenFileName(
            slicer.util.mainWindow(),
            "Load transducer",
            qsettings.value("OpenLIFU/databaseDirectory", "."),
            "Transducers (*.json);;All Files (*)",
        )
        if not filepath:
            return
        # Delegate to the existing module logic so the load is consistent with
        # the legacy "Manual Object Load > Load Transducer" path. After loading
        # into the scene, also persist into the database.
        logic = slicer.util.getModuleLogic("OpenLIFUData")
        loaded = logic.load_transducer_from_file(filepath)
        if loaded is None:
            # load_transducer_from_file currently returns None; fall back to
            # parsing the file again to get something we can write to the db.
            try:
                import openlifu.xdc.util

                obj = openlifu.xdc.util.load_transducer_from_file(filepath, convert_array=False)
            except Exception as e:
                slicer.util.errorDisplay(f"Failed to read transducer file: {e}", parent=self)
                return
        else:
            obj = loaded
        try:
            self.db.write_transducer(obj, on_conflict="overwrite")
        except Exception as e:
            slicer.util.errorDisplay(f"Failed to write transducer to database: {e}", parent=self)
            return
        self.refresh()

    @display_errors
    def onAddFromDevice(self, checked: bool = False) -> None:
        try:
            sc_logic = slicer.util.getModuleLogic("OpenLIFUSonicationControl")
            iface = getattr(sc_logic, "cur_lifu_interface", None)
        except (AttributeError, RuntimeError):
            iface = None
        if iface is None:
            slicer.util.errorDisplay(
                "The LIFU interface has not been initialized. Open the OpenLIFU "
                "Sonication Control module to initialize the hardware interface.",
                parent=self,
            )
            return
        try:
            tx_conn, _hv = iface.is_device_connected()
        except Exception as e:
            slicer.util.errorDisplay(f"Could not query device connection: {e}", parent=self)
            return
        if not tx_conn:
            slicer.util.errorDisplay("No TX device is currently connected.", parent=self)
            return

        # 1. Pull user_configs.
        try:
            user_configs = _read_connected_user_configs(iface)
        except Exception as e:
            slicer.util.errorDisplay(
                f"Could not read user_config from the connected device:\n\n{e}",
                parent=self,
            )
            return

        # 2. Resolve the template id (device.template if present, else count+freq).
        try:
            template_id, device_block = _resolve_template_id_for_user_configs(user_configs)
        except Exception as e:
            slicer.util.errorDisplay(str(e), parent=self)
            return

        # 3. Template MUST exist in the database -- the meshless fallback is
        #    not acceptable in the Slicer UI (the meshes are needed for
        #    visualization and registration).
        try:
            db_ids = list(self.db.get_transducer_ids() or [])
        except Exception:
            db_ids = []
        if template_id not in db_ids:
            slicer.util.errorDisplay(
                f"The required template transducer '{template_id}' is not in the loaded "
                f"database. Add it (e.g. from the openlifu sample database) and try again.",
                parent=self,
            )
            return

        # 4. Always show the edit dialog (prepopulated from any existing
        #    device block) so the user can confirm / change the id and name
        #    before we assemble + write.
        hwids = [str(c.get("hwid")) for c in user_configs]
        initial_id = ""
        initial_name = ""
        if device_block is not None:
            initial_id = str(device_block.get("id") or "")
            initial_name = str(device_block.get("name") or "")
        dlg = DeviceConfigEditDialog(
            template_id=template_id,
            module_hwids=hwids,
            initial_id=initial_id,
            initial_name=initial_name,
            parent=self,
        )
        rc, arr_id, arr_name = dlg.customexec_()
        if not rc:
            return
        # Detect changes that warrant pushing the new device block back to the
        # connected hardware: either there was no device block at all, or the
        # user edited the id or name.
        id_changed = device_block is None or arr_id != initial_id
        name_changed = device_block is None or arr_name != initial_name
        should_offer_writeback = device_block is None or id_changed or name_changed

        # 5. Assemble. Use ``use_default_template=False`` so any future db
        #    lookup failure becomes an error rather than a silent meshless
        #    fallback. Capture the db-mismatch UserWarning emitted by
        #    openlifu-python so we can surface it.
        import warnings as _warnings
        with _warnings.catch_warnings(record=True) as caught:
            _warnings.simplefilter("always")
            try:
                import openlifu.xdc

                arr = openlifu.xdc.TransducerArray.get_connected(
                    interface=iface,
                    db=self.db,
                    arr_id=arr_id,
                    arr_name=arr_name,
                    use_default_template=False,
                )
            except Exception as e:
                slicer.util.errorDisplay(
                    f"Failed to assemble transducer from device:\n\n{e}",
                    parent=self,
                )
                return
        mismatch_warning = next(
            (str(w.message) for w in caught if "differs from the version" in str(w.message)),
            None,
        )
        if mismatch_warning:
            confirmed = slicer.util.confirmYesNoDisplay(
                f"{mismatch_warning}\n\nOverwrite the database copy with the version "
                f"assembled from the connected device?",
                "Database mismatch",
                parent=self,
            )
            if not confirmed:
                return

        # 6. Write to db, copying mesh files in from the template's directory.
        try:
            paths = self.db.get_transducer_absolute_filepaths(template_id) or {}
            reg_path = paths.get("registration_surface_abspath") or None
            body_path = paths.get("transducer_body_abspath") or None
            self.db.write_transducer(
                arr,
                registration_surface_model_filepath=reg_path,
                transducer_body_model_filepath=body_path,
                on_conflict="overwrite",
            )
        except Exception as e:
            slicer.util.errorDisplay(
                f"Failed to write transducer to database:\n\n{e}",
                parent=self,
            )
            return

        slicer.util.infoDisplay(
            f"Saved transducer '{arr.id}' to the database.",
            windowTitle="Add from Device",
            parent=self,
        )

        # 7. If the device block didn't exist (or the user edited id/name),
        #    offer to push the new device block down to module 0 so the
        #    physical device starts reporting it on the next read.
        if should_offer_writeback:
            confirmed = slicer.util.confirmYesNoDisplay(
                "Write this transducer's device definition (id, name, modules) "
                "back to the connected device's module 0?\n\n"
                "This updates the on-device user_config so the device will "
                "report itself as this transducer on subsequent connects.",
                "Write device config",
                parent=self,
            )
            if confirmed:
                try:
                    self._write_device_block_to_module0(iface, arr, user_configs[0])
                except Exception as e:
                    slicer.util.errorDisplay(
                        f"Failed to write device config to module 0:\n\n{e}",
                        parent=self,
                    )

        self.refresh()

    def _write_device_block_to_module0(self, iface, arr, lead_user_config: dict) -> None:
        """Overwrite the ``device`` section of module 0's user_config and write it back.

        Reads module 0's current user_config dict (passed in as
        ``lead_user_config`` to avoid an extra round-trip), splices in the
        new ``device`` block derived from the just-assembled
        :class:`TransducerArray`, and calls
        ``txdevice.write_config_json(json_str, module=0)``.
        """
        new_device = arr.to_device_config()
        # ``to_device_config`` records the assembled template id when present
        # so future ``get_connected`` calls can prefer it over the (count, freq)
        # default mapping.
        try:
            template_id, _ = _resolve_template_id_for_user_configs([lead_user_config])
            new_device.setdefault("template", template_id)
        except Exception:
            pass
        updated = dict(lead_user_config)
        updated["device"] = new_device
        json_str = json.dumps(updated)
        iface.txdevice.write_config_json(json_str, module=0)
        logging.info(
            "Wrote new device block (id=%s) to TX module 0 user_config.",
            new_device.get("id"),
        )

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
        dialog.exec_()

    @display_errors
    def onDelete(self, *args) -> None:
        tid = self._selected_transducer_id()
        if not tid:
            slicer.util.errorDisplay("Select a transducer first.", parent=self)
            return
        if tid in PROTECTED_TRANSDUCER_IDS:
            confirmed = slicer.util.confirmYesNoDisplay(
                "You are about to delete a built-in transducer definition. "
                "This is used as a template when connecting new transducers of "
                "this type. Are you sure you want to delete it?",
                "Delete built-in transducer",
                parent=self,
            )
        else:
            confirmed = slicer.util.confirmYesNoDisplay(
                f"Delete transducer '{tid}' from the database?",
                "Delete transducer",
                parent=self,
            )
        if not confirmed:
            return
        try:
            self._delete_transducer_from_db(tid)
        except Exception as e:
            slicer.util.errorDisplay(f"Failed to delete transducer '{tid}': {e}", parent=self)
            return
        self.refresh()

    def _delete_transducer_from_db(self, transducer_id: str) -> None:
        """Remove ``transducer_id`` from the database.

        ``openlifu.db.Database`` does not currently expose a ``delete_transducer``
        method, so we mirror the on-disk layout produced by ``write_transducer``:
        drop the id from ``transducers.json`` and remove the per-transducer
        directory.
        """
        import shutil
        ids = list(self.db.get_transducer_ids() or [])
        if transducer_id not in ids:
            return
        ids = [i for i in ids if i != transducer_id]
        self.db.write_transducer_ids(ids)
        try:
            transducer_dir = Path(self.db.get_transducer_filename(transducer_id)).parent
            if transducer_dir.is_dir():
                shutil.rmtree(transducer_dir)
        except Exception as e:
            logging.warning(
                "Removed transducer '%s' from index but could not remove its "
                "directory: %s",
                transducer_id, e,
            )
