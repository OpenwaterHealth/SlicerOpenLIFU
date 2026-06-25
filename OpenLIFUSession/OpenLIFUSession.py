# Standard library imports
from functools import partial
from typing import Dict, Optional, TYPE_CHECKING

# Third-party imports
import qt
import vtk

# Slicer imports
import slicer
from slicer.ScriptedLoadableModule import *
from slicer.i18n import tr as _
from slicer.i18n import translate
from slicer.parameterNodeWrapper import parameterNodeWrapper
from slicer.util import VTKObservationMixin

# OpenLIFULib imports
from OpenLIFULib import get_openlifu_data_parameter_node
from OpenLIFULib.guided_mode_util import GuidedWorkflowMixin
from OpenLIFULib.module_layout import apply_module_layout, wire_passive_module_header
from OpenLIFULib.util import BusyCursor, display_errors

# These imports are done only for IDE and static analysis purposes
if TYPE_CHECKING:
    from OpenLIFULib.session import SlicerOpenLIFUSession


#
# OpenLIFUSession
#

class OpenLIFUSession(ScriptedLoadableModule):
    """Read-only landing page for an active OpenLIFU session.

    Acts as the dashboard between the Home page (where a session is created
    or loaded) and the workflow modules (Pre-Planning onward).
    """

    def __init__(self, parent):
        ScriptedLoadableModule.__init__(self, parent)
        self.parent.title = _("OpenLIFU Session")
        self.parent.categories = [translate("qSlicerAbstractCoreModule", "OpenLIFU.OpenLIFU Modules")]
        self.parent.dependencies = ["OpenLIFUHome"]
        self.parent.contributors = ["Peter Hollender (Openwater), Ebrahim Ebrahim (Kitware)"]
        self.parent.helpText = _(
            "This is the session dashboard module of the OpenLIFU extension for focused ultrasound. "
            "It displays read-only information about the currently loaded session. "
            "More information at <a href=\"https://github.com/OpenwaterHealth/SlicerOpenLIFU\">github.com/OpenwaterHealth/SlicerOpenLIFU</a>."
        )
        self.parent.acknowledgementText = _(
            "This is part of Openwater's OpenLIFU, an open-source "
            "hardware and software platform for Low Intensity Focused Ultrasound (LIFU) research "
            "and development."
        )
        # Embedded as a page of the OpenLIFU host module; hide from the modules menu.
        self.parent.hidden = True


#
# OpenLIFUSessionParameterNode
#


@parameterNodeWrapper
class OpenLIFUSessionParameterNode:
    """Empty parameter node -- all state for this read-only view is derived
    from the OpenLIFUData parameter node and the loaded session."""


#
# OpenLIFUSessionWidget
#


class OpenLIFUSessionWidget(ScriptedLoadableModuleWidget, VTKObservationMixin, GuidedWorkflowMixin):

    def __init__(self, parent=None) -> None:
        ScriptedLoadableModuleWidget.__init__(self, parent)
        VTKObservationMixin.__init__(self)
        self.logic: Optional[OpenLIFUSessionLogic] = None
        self._parameterNode = None
        self._parameterNodeGuiTag = None
        # Per-session bookkeeping so each count collapsible is auto-expanded
        # at most once per session (on the first entry where it has items).
        self._auto_expanded_collapsibles_for_session: Dict[str, set] = {}

    def setup(self) -> None:
        ScriptedLoadableModuleWidget.setup(self)

        uiWidget = slicer.util.loadUI(self.resourcePath("UI/OpenLIFUSession.ui"))
        self.layout.addWidget(uiWidget)
        self.uiWidget = uiWidget
        self.ui = slicer.util.childWidgetVariables(uiWidget)

        # Shared header (read-only) + scrollable body + footer.
        self.module_header = apply_module_layout(
            uiWidget, ui_namespace=self.ui, header_read_only=True
        )

        uiWidget.setMRMLScene(slicer.mrmlScene)

        self.logic = OpenLIFUSessionLogic()

        # Prevent two OpenLIFUData widgets from being created --
        # see https://github.com/OpenwaterHealth/SlicerOpenLIFU/issues/120
        slicer.util.getModule("OpenLIFUData").widgetRepresentation()

        # ---- Inject guided mode workflow controls ----
        self.inject_workflow_controls_into_placeholder()

        # ---- Passive header observers (DB / login / device status) ----
        wire_passive_module_header(self, self.module_header)

        # ---- Connections ----
        self.addObserver(slicer.mrmlScene, slicer.mrmlScene.StartCloseEvent, self.onSceneStartClose)
        self.addObserver(slicer.mrmlScene, slicer.mrmlScene.EndCloseEvent, self.onSceneEndClose)

        self.initializeParameterNode()

        # Observe Data's parameter node so the dashboard refreshes when the
        # loaded session, loaded photoscans, solution, or run change.
        self.addObserver(
            get_openlifu_data_parameter_node().parameterNode,
            vtk.vtkCommand.ModifiedEvent,
            self.onDataParameterNodeModified,
        )

        # ---- View buttons: open the same preview popups as the Data Manager.
        # Protocol/Transducer are 1:1 with the session and get a section-level
        # View button; photoscans / solutions / runs are 0:N and get one View
        # button per item, populated dynamically in ``_update_count_collapsible``.
        self.ui.protocolViewButton.clicked.connect(self.onPreviewProtocol)
        self.ui.transducerViewButton.clicked.connect(self.onPreviewTransducer)

        self.updateSessionDashboard()

    def cleanup(self) -> None:
        self.removeObservers()

    def enter(self) -> None:
        self.initializeParameterNode()
        self.updateSessionDashboard()

    def exit(self) -> None:
        if self._parameterNode:
            self._parameterNode.disconnectGui(self._parameterNodeGuiTag)
            self._parameterNodeGuiTag = None

    def onSceneStartClose(self, caller, event) -> None:
        self.setParameterNode(None)

    def onSceneEndClose(self, caller, event) -> None:
        if self.parent.isEntered:
            self.initializeParameterNode()
            self.updateSessionDashboard()

    def initializeParameterNode(self) -> None:
        self.setParameterNode(self.logic.getParameterNode())

    def setParameterNode(self, inputParameterNode: Optional[OpenLIFUSessionParameterNode]) -> None:
        if self._parameterNode:
            self._parameterNode.disconnectGui(self._parameterNodeGuiTag)

        self._parameterNode = inputParameterNode
        if self._parameterNode:
            self._parameterNodeGuiTag = self._parameterNode.connectGui(self.ui)

    @display_errors
    def onDataParameterNodeModified(self, caller=None, event=None) -> None:
        self.updateSessionDashboard()

    # ---- View / preview popups ------------------------------------------

    @display_errors
    def _on_item_view_clicked(self, preview_callback, item_id, checked: bool = False) -> None:
        """Common click handler for per-item View buttons (photoscan / solution / run)."""
        preview_callback(item_id)

    @display_errors
    def onPreviewProtocol(self, checked: bool = False) -> None:
        loaded_session = get_openlifu_data_parameter_node().loaded_session
        if loaded_session is None or not loaded_session.protocol_is_valid():
            slicer.util.errorDisplay("No protocol is loaded in the current session.")
            return
        protocol = loaded_session.get_protocol().protocol
        from OpenLIFUData import ProtocolPreviewDialog
        ProtocolPreviewDialog(protocol).exec_()

    @display_errors
    def onPreviewTransducer(self, checked: bool = False) -> None:
        loaded_session = get_openlifu_data_parameter_node().loaded_session
        if loaded_session is None or not loaded_session.transducer_is_valid():
            slicer.util.errorDisplay("No transducer is loaded in the current session.")
            return
        transducer_openlifu = loaded_session.get_transducer().transducer.transducer
        body_path = None
        registration_path = None
        try:
            db = slicer.util.getModuleLogic("OpenLIFUDatabase").db
            if db is not None:
                abspaths = db.get_transducer_absolute_filepaths(transducer_openlifu.id) or {}
                body_path = abspaths.get("transducer_body_abspath")
                registration_path = abspaths.get("registration_surface_abspath")
        except Exception:
            pass
        from OpenLIFUData import TransducerPreviewDialog
        TransducerPreviewDialog(
            transducer_openlifu,
            body_abspath=body_path,
            registration_surface_abspath=registration_path,
        ).exec_()

    def _preview_photoscan(self, pid: str) -> None:
        loaded_session = get_openlifu_data_parameter_node().loaded_session
        if loaded_session is None:
            slicer.util.errorDisplay("No session is loaded.")
            return
        subject_id = loaded_session.get_subject_id()
        session_id = loaded_session.get_session_id()
        # Prefer the rich 3D preview dialog used by Transducer Localization /
        # the Photoscan manager; fall back to the JSON tree if loading fails.
        slicer_photoscan = get_openlifu_data_parameter_node().loaded_photoscans.get(pid)
        if slicer_photoscan is None:
            try:
                data_logic = slicer.util.getModuleLogic("OpenLIFUData")
                db = slicer.util.getModuleLogic("OpenLIFUDatabase").db
                openlifu_photoscan = db.load_photoscan(subject_id, session_id, pid)
                slicer_photoscan = data_logic.load_photoscan_from_openlifu(
                    openlifu_photoscan,
                    load_from_active_session=True,
                )
            except Exception:
                slicer_photoscan = None
        if slicer_photoscan is None:
            self._json_preview_photoscan(pid, subject_id, session_id)
            return
        from OpenLIFUTransducerLocalization import PhotoscanPreviewDialog
        with BusyCursor():
            dialog = PhotoscanPreviewDialog(slicer_photoscan)
        dialog.exec_()
        dialog.deleteLater()

    def _json_preview_photoscan(self, pid: str, subject_id: str, session_id: str) -> None:
        from OpenLIFUData import _JsonTreeDialog
        try:
            db = slicer.util.getModuleLogic("OpenLIFUDatabase").db
            obj = db.load_photoscan(subject_id, session_id, pid)
            data = obj.to_dict()
        except Exception as e:
            data = {"error": f"Could not load photoscan {pid}: {e}"}
        _JsonTreeDialog(f"Photoscan {pid}", {pid: data}).exec_()

    def _preview_solution(self, sid: str) -> None:
        loaded_session = get_openlifu_data_parameter_node().loaded_session
        if loaded_session is None:
            slicer.util.errorDisplay("No session is loaded.")
            return
        from OpenLIFUData import _JsonTreeDialog
        try:
            import openlifu.plan
            db = slicer.util.getModuleLogic("OpenLIFUDatabase").db
            json_filepath = db.get_solution_filepath(
                loaded_session.get_subject_id(),
                loaded_session.get_session_id(),
                sid,
            )
            obj = openlifu.plan.Solution.from_files(json_filepath)
            data = obj.to_dict()
        except Exception as e:
            data = {"error": f"Could not load solution {sid}: {e}"}
        _JsonTreeDialog(f"Solution {sid}", {sid: data}).exec_()

    def _preview_run(self, rid: str) -> None:
        loaded_session = get_openlifu_data_parameter_node().loaded_session
        if loaded_session is None:
            slicer.util.errorDisplay("No session is loaded.")
            return
        subject_id = loaded_session.get_subject_id()
        session_id = loaded_session.get_session_id()
        from OpenLIFUData import _JsonTreeDialog
        tree_data = {}
        try:
            import openlifu.plan
            db = slicer.util.getModuleLogic("OpenLIFUDatabase").db
            run_filepath = db.get_run_filepath(subject_id, session_id, rid)
            run = openlifu.plan.Run.from_file(run_filepath)
            tree_data["run"] = run.to_dict()
        except Exception as e:
            tree_data["run"] = {"error": f"Could not load run {rid}: {e}"}
        try:
            db = slicer.util.getModuleLogic("OpenLIFUDatabase").db
            session_snap = db.load_session_snapshot(subject_id, session_id, rid)
            tree_data["session_snapshot"] = session_snap.to_dict()
        except Exception as e:
            tree_data["session_snapshot"] = {"error": str(e)}
        try:
            db = slicer.util.getModuleLogic("OpenLIFUDatabase").db
            protocol_snap = db.load_protocol_snapshot(subject_id, session_id, rid)
            tree_data["protocol_snapshot"] = protocol_snap.to_dict()
        except Exception as e:
            tree_data["protocol_snapshot"] = {"error": str(e)}
        _JsonTreeDialog(f"Run {rid}", tree_data).exec_()

    # ---- Dashboard refresh ------------------------------------------------

    def updateSessionDashboard(self) -> None:
        """Repopulate every read-only section from the currently loaded session."""
        data_param = get_openlifu_data_parameter_node()
        loaded_session: "Optional[SlicerOpenLIFUSession]" = data_param.loaded_session

        if loaded_session is None:
            self.ui.noSessionLabel.setVisible(True)
            self.ui.sessionContentsWidget.setVisible(False)
            self._clear_workflow_status()
            return

        self.ui.noSessionLabel.setVisible(False)
        self.ui.sessionContentsWidget.setVisible(True)

        session_openlifu = loaded_session.session.session
        session_name = getattr(session_openlifu, "name", None) or session_openlifu.id
        self.ui.sessionTitleLabel.setText(f"Session: {session_name}")

        # --- Subject ---
        self.ui.subjectNameValueLabel.setText(self._subject_display_name(loaded_session))
        self.ui.subjectIdValueLabel.setText(loaded_session.get_subject_id() or "-")

        # --- Protocol ---
        protocol_name, protocol_id = self._protocol_display(loaded_session)
        self.ui.protocolNameValueLabel.setText(protocol_name)
        self.ui.protocolIdValueLabel.setText(protocol_id)

        # --- Volume ---
        volume_name, volume_id = self._volume_display(loaded_session)
        self.ui.volumeNameValueLabel.setText(volume_name)
        self.ui.volumeIdValueLabel.setText(volume_id)

        # --- Transducer ---
        transducer_name, transducer_id = self._transducer_display(loaded_session)
        self.ui.transducerNameValueLabel.setText(transducer_name)
        self.ui.transducerIdValueLabel.setText(transducer_id)

        # --- Counts ---
        photoscans, solutions, runs = self._collection_counts(loaded_session)
        self._update_count_collapsible(
            self.ui.photoscansCollapsible,
            self.ui.photoscansEmptyLabel,
            self.ui.photoscansItemsContainer.layout(),
            "Photoscans",
            photoscans,
            self._preview_photoscan,
        )
        self._update_count_collapsible(
            self.ui.solutionsCollapsible,
            self.ui.solutionsEmptyLabel,
            self.ui.solutionsItemsContainer.layout(),
            "Solutions",
            solutions,
            self._preview_solution,
        )
        self._update_count_collapsible(
            self.ui.runsCollapsible,
            self.ui.runsEmptyLabel,
            self.ui.runsItemsContainer.layout(),
            "Runs",
            runs,
            self._preview_run,
        )

        self._set_workflow_proceedable()

    def _update_count_collapsible(
        self,
        collapsible,
        empty_label,
        items_layout,
        title: str,
        ids,
        preview_callback,
    ) -> None:
        """Refresh a Photoscans/Solutions/Runs collapsible section.

        - Header text shows ``f"{title} ({len(ids)})"``.
        - When empty, the section is force-collapsed (but always remains
          clickable). When non-empty, ``items_layout`` is repopulated with
          one row per ID: a label on the left and a ``View`` button on the
          right that invokes ``preview_callback(id)``.
        - The first time a section becomes non-empty within a given session,
          it is auto-expanded so the loaded data is immediately visible.
        """
        ids = list(ids or [])
        count = len(ids)
        collapsible.text = f"{title} ({count})"

        # Always keep the collapsible itself clickable; we only manage the
        # body contents and the collapsed property.
        collapsible.setEnabled(True)

        # Clear any rows from a previous refresh.
        while items_layout.count():
            item = items_layout.takeAt(0)
            child = item.widget() if item is not None else None
            if child is not None:
                child.setParent(None)
                child.deleteLater()

        items_container = items_layout.parentWidget()
        if items_container is None:
            # Fallback: PythonQt sometimes returns None for parentWidget on
            # nested layouts; resolve via the layout's parent attribute.
            items_container = items_layout.parent()

        if count == 0:
            empty_label.setText("-")
            empty_label.setVisible(True)
            if items_container is not None:
                items_container.setVisible(False)
            collapsible.collapsed = True
            return

        empty_label.setVisible(False)
        if items_container is not None:
            items_container.setVisible(True)

        # One-shot auto-expand per session per section, so the user sees
        # loaded data without needing to click the header.
        try:
            loaded_session = get_openlifu_data_parameter_node().loaded_session
            session_key = (
                f"{loaded_session.get_subject_id()}|{loaded_session.get_session_id()}"
                if loaded_session is not None else None
            )
        except Exception:
            session_key = None
        if session_key is not None:
            already = self._auto_expanded_collapsibles_for_session.setdefault(session_key, set())
            if title not in already:
                collapsible.collapsed = False
                already.add(title)

        for item_id in ids:
            row = qt.QWidget(items_container)
            row_layout = qt.QHBoxLayout(row)
            row_layout.setContentsMargins(0, 0, 0, 0)
            id_label = qt.QLabel(item_id, row)
            id_label.setTextInteractionFlags(qt.Qt.TextSelectableByMouse)
            row_layout.addWidget(id_label, 1)
            view_button = qt.QPushButton("View", row)
            view_button.setToolTip(f"Open a preview of this {title.rstrip('s').lower()}.")
            # Bind loop vars via default args; ``clicked`` always passes ``checked: bool``.
            view_button.clicked.connect(
                lambda checked=False, cb=preview_callback, iid=item_id: self._on_item_view_clicked(cb, iid)
            )
            row_layout.addWidget(view_button, 0)
            items_layout.addWidget(row)
            row.show()

        if items_container is not None:
            items_container.updateGeometry()

    def _subject_display_name(self, loaded_session: "SlicerOpenLIFUSession") -> str:
        subject_id = loaded_session.get_subject_id()
        if not subject_id:
            return "-"
        try:
            db = slicer.util.getModuleLogic("OpenLIFUDatabase").db
            if db is not None:
                subject = db.load_subject_info(subject_id)
                name = getattr(subject, "name", None)
                if name:
                    return name
        except Exception:
            pass
        return subject_id

    def _protocol_display(self, loaded_session: "SlicerOpenLIFUSession") -> tuple:
        protocol_id = loaded_session.get_protocol_id() or "-"
        if loaded_session.protocol_is_valid():
            protocol = loaded_session.get_protocol().protocol
            name = getattr(protocol, "name", None) or protocol_id
            return name, protocol_id
        return "-", protocol_id

    def _volume_display(self, loaded_session: "SlicerOpenLIFUSession") -> tuple:
        volume_id = loaded_session.get_volume_id() or "-"
        if loaded_session.volume_is_valid():
            name = loaded_session.volume_node.GetName() or volume_id
            return name, volume_id
        return "-", volume_id

    def _transducer_display(self, loaded_session: "SlicerOpenLIFUSession") -> tuple:
        transducer_id = loaded_session.get_transducer_id() or "-"
        if loaded_session.transducer_is_valid():
            transducer = loaded_session.get_transducer().transducer.transducer
            name = getattr(transducer, "name", None) or transducer_id
            return name, transducer_id
        return "-", transducer_id

    def _collection_counts(self, loaded_session: "SlicerOpenLIFUSession") -> tuple:
        """Return ``(photoscan_ids, solution_ids, run_ids)`` for the session.

        Each element is a list (possibly empty). Callers compute counts via ``len()``.
        """
        photoscan_ids = list(loaded_session.get_affiliated_photoscan_ids() or [])
        solution_ids: list = []
        run_ids: list = []
        try:
            db = slicer.util.getModuleLogic("OpenLIFUDatabase").db
            if db is not None:
                solution_ids = list(
                    db.get_solution_ids(
                        loaded_session.get_subject_id(),
                        loaded_session.get_session_id(),
                    ) or []
                )
                run_ids = list(
                    db.get_run_ids(
                        loaded_session.get_subject_id(),
                        loaded_session.get_session_id(),
                    ) or []
                )
        except Exception:
            pass
        return photoscan_ids, solution_ids, run_ids

    def _set_workflow_proceedable(self) -> None:
        if not hasattr(self, "workflow_controls"):
            return
        self.workflow_controls.can_proceed = True
        self.workflow_controls.status_text = "Proceed to pre-planning."

    def _clear_workflow_status(self) -> None:
        if not hasattr(self, "workflow_controls"):
            return
        self.workflow_controls.can_proceed = False
        self.workflow_controls.status_text = "Load a session from the Home page to continue."


#
# OpenLIFUSessionLogic
#


class OpenLIFUSessionLogic(ScriptedLoadableModuleLogic):

    def __init__(self) -> None:
        ScriptedLoadableModuleLogic.__init__(self)

    def getParameterNode(self):
        return OpenLIFUSessionParameterNode(super().getParameterNode())


#
# OpenLIFUSessionTest
#


class OpenLIFUSessionTest(ScriptedLoadableModuleTest):

    def setUp(self):
        slicer.mrmlScene.Clear()

    def runTest(self):
        self.setUp()
        self.test_preview_dialog_imports()
        self.test_dashboard_with_no_session()

    def test_preview_dialog_imports(self):
        """Smoke-test that every dialog used by the Session-page View buttons is importable.

        Slicer scripted modules are loaded as flat top-level modules, not
        packages, so ``from OpenLIFUData.OpenLIFUData import X`` raises
        ImportError at runtime. This test catches that class of mistake
        without needing a loaded database or session.
        """
        from OpenLIFUData import ProtocolPreviewDialog  # noqa: F401
        from OpenLIFUData import TransducerPreviewDialog  # noqa: F401
        from OpenLIFUData import _JsonTreeDialog  # noqa: F401
        from OpenLIFUTransducerLocalization import PhotoscanPreviewDialog  # noqa: F401

    def test_dashboard_with_no_session(self):
        """The dashboard must render gracefully when no session is loaded."""
        slicer.util.selectModule("OpenLIFUSession")
        widget = slicer.modules.OpenLIFUSessionWidget
        widget.updateSessionDashboard()
        assert widget.ui.noSessionLabel.visible is True
        assert widget.ui.sessionContentsWidget.visible is False

    def workflow_session_dashboard(self):
        """Integration helper: assert dashboard reflects the currently loaded session.

        Called from OpenLIFUHomeTest._OpenLIFU_FullTest1 after a session has
        been loaded by the Data module. Verified counts come from the
        currently connected database. Also clicks every View button so that
        broken imports / signal wiring in the preview handlers fail the test
        instead of slipping through to live UI use.
        """
        slicer.util.selectModule("OpenLIFUSession")
        widget = slicer.modules.OpenLIFUSessionWidget
        widget.updateSessionDashboard()

        loaded_session = get_openlifu_data_parameter_node().loaded_session
        assert loaded_session is not None, "Expected a loaded session before exercising the dashboard."

        assert widget.ui.noSessionLabel.visible is False
        assert widget.ui.sessionContentsWidget.visible is True
        assert widget.ui.subjectIdValueLabel.text == loaded_session.get_subject_id()
        protocol_id = loaded_session.get_protocol_id() or "-"
        assert widget.ui.protocolIdValueLabel.text == protocol_id
        transducer_id = loaded_session.get_transducer_id() or "-"
        assert widget.ui.transducerIdValueLabel.text == transducer_id
        volume_id = loaded_session.get_volume_id() or "-"
        assert widget.ui.volumeIdValueLabel.text == volume_id

        # Per-collection sections: verify one "View" row per id, then exercise each button.
        photoscan_ids, solution_ids, run_ids = widget._collection_counts(loaded_session)
        for container_name, ids in (
            ("photoscansItemsContainer", photoscan_ids),
            ("solutionsItemsContainer", solution_ids),
            ("runsItemsContainer", run_ids),
        ):
            container = getattr(widget.ui, container_name)
            view_buttons = [
                child for child in container.findChildren(qt.QPushButton)
                if child.text == "View"
            ]
            assert len(view_buttons) == len(ids), (
                f"{container_name}: expected {len(ids)} View buttons, found {len(view_buttons)}"
            )

        # Click every View button while auto-dismissing each modal it opens.
        buttons_to_click = []
        if loaded_session.protocol_is_valid():
            buttons_to_click.append(widget.ui.protocolViewButton)
        if loaded_session.transducer_is_valid():
            buttons_to_click.append(widget.ui.transducerViewButton)
        for container_name in ("photoscansItemsContainer", "solutionsItemsContainer", "runsItemsContainer"):
            container = getattr(widget.ui, container_name)
            buttons_to_click.extend(
                child for child in container.findChildren(qt.QPushButton) if child.text == "View"
            )
        for button in buttons_to_click:
            self._click_and_dismiss_modal(button)

    @staticmethod
    def _click_and_dismiss_modal(button) -> None:
        """Click ``button`` and accept whichever modal dialog it pops up.

        ``QDialog.exec_()`` blocks until the dialog is closed, so we schedule
        a retrying timer that finds the active modal widget and accepts it.
        """
        attempts_left = [10]  # boxed so the nested closure can mutate it

        def attempt():
            modal = slicer.app.activeModalWidget()
            if modal is not None:
                try:
                    modal.accept()
                except Exception:
                    modal.close()
                return
            attempts_left[0] -= 1
            if attempts_left[0] > 0:
                qt.QTimer.singleShot(50, attempt)

        qt.QTimer.singleShot(50, attempt)
        button.click()
