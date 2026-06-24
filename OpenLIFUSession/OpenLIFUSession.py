# Standard library imports
from typing import Optional, TYPE_CHECKING

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
from OpenLIFULib.util import display_errors

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
            self.ui.photoscansListLabel,
            "Photoscans",
            photoscans,
        )
        self._update_count_collapsible(
            self.ui.solutionsCollapsible,
            self.ui.solutionsListLabel,
            "Solutions",
            solutions,
        )
        self._update_count_collapsible(
            self.ui.runsCollapsible,
            self.ui.runsListLabel,
            "Runs",
            runs,
        )

        self._set_workflow_proceedable()

    def _update_count_collapsible(self, collapsible, body_label, title: str, ids) -> None:
        """Refresh a Photoscans/Solutions/Runs collapsible section.

        - Header text shows ``f"{title} ({len(ids)})"``.
        - Body lists the IDs, one per line.
        - When the count is zero, the section is force-collapsed and the
          button is disabled so the user cannot expand an empty list.
        """
        ids = list(ids or [])
        count = len(ids)
        collapsible.text = f"{title} ({count})"
        if count == 0:
            body_label.setText("-")
            # ctkCollapsibleButton exposes ``collapsed`` as a Qt property; there
            # is no ``setCollapsed`` setter in the PythonQt wrapper.
            collapsible.collapsed = True
            collapsible.setEnabled(False)
        else:
            body_label.setText("\n".join(ids))
            collapsible.setEnabled(True)

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
        self.test_dashboard_with_no_session()

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
        currently connected database.
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
