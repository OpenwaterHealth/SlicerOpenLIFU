# Standard library imports
from __future__ import annotations

from typing import Dict, List, Optional, TYPE_CHECKING

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
from OpenLIFULib import ensure_python_requirements_for_module_enter
from OpenLIFULib.guided_mode_util import (
    confirm_exit_session_dialog,
    Workflow,
)
from OpenLIFULib.module_layout import (
    ModuleHeaderWidget,
    embed_module_body_into,
    force_embedded_body_visible,
    wire_passive_module_header,
)
from OpenLIFULib.util import display_errors

if TYPE_CHECKING:
    from OpenLIFUData.OpenLIFUData import OpenLIFUDataLogic
    from OpenLIFUHome.OpenLIFUHome import OpenLIFUHomeLogic


# ---------------------------------------------------------------------------
# Module class registration
# ---------------------------------------------------------------------------

class OpenLIFU(ScriptedLoadableModule):
    """Single-page host module that embeds the existing OpenLIFU* workflow
    modules as stacked pages with one shared header (DB / Login / Device /
    Save / Exit) and one footer timeline (Session → … → Control)."""

    def __init__(self, parent):
        ScriptedLoadableModule.__init__(self, parent)
        self.parent.title = _("OpenLIFU")
        self.parent.categories = [translate("qSlicerAbstractCoreModule", "OpenLIFU")]
        self.parent.dependencies = [
            "OpenLIFUHome",
            "OpenLIFUData",
            "OpenLIFUSession",
            "OpenLIFUPrePlanning",
            "OpenLIFUTransducerLocalization",
            "OpenLIFUSonicationPlanner",
            "OpenLIFUSonicationControl",
        ]
        self.parent.contributors = [
            "Peter Hollender (Openwater), Ebrahim Ebrahim (Kitware)",
        ]
        self.parent.helpText = _(
            "Unified entry point for the OpenLIFU extension. Hosts the full "
            "treatment-planning workflow as a single module with stacked pages."
        )
        self.parent.acknowledgementText = _(
            "This is part of Openwater's OpenLIFU, an open-source "
            "hardware and software platform for Low Intensity Focused Ultrasound (LIFU) "
            "research and development."
        )


# ---------------------------------------------------------------------------
# Parameter node
# ---------------------------------------------------------------------------

@parameterNodeWrapper
class OpenLIFUParameterNode:
    """The host module's own parameter node currently holds no state — every
    workflow piece still owns its own parameter node. Reserved for future
    use (e.g. the active page id)."""


# ---------------------------------------------------------------------------
# Page descriptor
# ---------------------------------------------------------------------------

class _Page:
    """A single embedded page inside the host's QStackedWidget.

    Attributes:
        key:         the underlying Slicer module name (e.g. "OpenLIFUSession").
        label:       short label shown on the timeline footer.
        on_timeline: True if the page is part of the workflow timeline.
        container:   the QWidget actually inserted into the QStackedWidget.
        timeline_button: the QPushButton that navigates to this page (or None).
    """

    def __init__(self, key: str, label: str, on_timeline: bool) -> None:
        self.key = key
        self.label = label
        self.on_timeline = on_timeline
        self.container: Optional[qt.QWidget] = None
        self.timeline_button: Optional[qt.QPushButton] = None


# Ordered list of pages. Anything `on_timeline=True` appears in the footer
# timeline in the order given here.
_PAGE_DEFS: List[_Page] = [
    _Page("OpenLIFUHome",                  "Home",         on_timeline=False),
    _Page("OpenLIFUSession",               "Session",      on_timeline=True),
    _Page("OpenLIFUPrePlanning",           "Pre-Planning", on_timeline=True),
    _Page("OpenLIFUTransducerLocalization","Localization", on_timeline=True),
    _Page("OpenLIFUSonicationPlanner",     "Solution",     on_timeline=True),
    _Page("OpenLIFUSonicationControl",     "Control",      on_timeline=True),
]


# ---------------------------------------------------------------------------
# Widget
# ---------------------------------------------------------------------------

class OpenLIFUWidget(ScriptedLoadableModuleWidget, VTKObservationMixin):

    def __init__(self, parent=None) -> None:
        ScriptedLoadableModuleWidget.__init__(self, parent)
        VTKObservationMixin.__init__(self)
        self.logic: Optional[OpenLIFULogic] = None
        self._parameterNode = None
        self._parameterNodeGuiTag = None

        # Page registry, keyed by module name.
        self._pages: Dict[str, _Page] = {p.key: _Page(p.key, p.label, p.on_timeline) for p in _PAGE_DEFS}
        self._current_page_key: Optional[str] = None
        self._embedding_done: bool = False

    # ------------------------------------------------------------------
    # Slicer lifecycle
    # ------------------------------------------------------------------

    def setup(self) -> None:
        ScriptedLoadableModuleWidget.setup(self)

        uiWidget = slicer.util.loadUI(self.resourcePath("UI/OpenLIFU.ui"))
        self.layout.addWidget(uiWidget)
        self.uiWidget = uiWidget
        self.ui = slicer.util.childWidgetVariables(uiWidget)
        uiWidget.setMRMLScene(slicer.mrmlScene)

        self.logic = OpenLIFULogic()

        # ---- Header status icons (DB / Login / Device) ----
        self.module_header = ModuleHeaderWidget(
            read_only=True,
            keep_login_button_active=True,
            parent=self.ui.hostHeaderStatusPlaceholder,
        )
        self.ui.hostHeaderStatusPlaceholder.layout().addWidget(self.module_header)

        # Hook the same observers used by every other module so the icons
        # stay in sync with global state.
        wire_passive_module_header(self, self.module_header)

        # ---- Save / Exit ----
        self.ui.hostSaveButton.clicked.connect(self.onSaveClicked)
        self.ui.hostExitButton.clicked.connect(self.onExitClicked)

        # ---- Build the timeline footer ----
        self._build_timeline_footer()

        # ---- Install the selectModule redirect shim so external code that
        #      calls slicer.util.selectModule("OpenLIFU<X>") for a hidden
        #      embedded child module instead lands on the host page. ----
        self._install_select_module_shim()

        # Defer the actual embedding pass to the next event-loop tick so that
        # every sibling module's setup() can complete first (avoids the
        # OpenLIFULogin.cacheAllLoginRelatedWidgets recursion path).
        qt.QTimer.singleShot(0, self._embed_all_pages)

        # Observe Data's parameter node so Save / Exit / status track session state.
        qt.QTimer.singleShot(0, self._wire_session_observers)

        # Hook Workflow.update_all so timeline + status repaint after every
        # workflow state push.
        qt.QTimer.singleShot(0, self._hook_workflow_updates)

        # Initial state: nothing to do until embedding finishes.
        self.ui.hostSaveButton.setEnabled(False)
        self.ui.hostExitButton.setEnabled(False)

    def cleanup(self) -> None:
        self.removeObservers()
        self._uninstall_select_module_shim()
        self._unhook_workflow_updates()

    def enter(self) -> None:
        ensure_python_requirements_for_module_enter()
        self.initializeParameterNode()
        if self._embedding_done and self._current_page_key is not None:
            self._delegate_enter(self._current_page_key)
            mw = slicer.util.getModuleWidget(self._current_page_key)
            if mw is not None and getattr(mw, "uiWidget", None) is not None:
                force_embedded_body_visible(mw.uiWidget, module_name=self._current_page_key)
        self._refresh_timeline_state()
        self._refresh_save_exit_state()

    def exit(self) -> None:
        if self._current_page_key is not None:
            self._delegate_exit(self._current_page_key)
        if self._parameterNode:
            self._parameterNode.disconnectGui(self._parameterNodeGuiTag)
            self._parameterNodeGuiTag = None

    def initializeParameterNode(self) -> None:
        if self.logic is not None:
            self.setParameterNode(self.logic.getParameterNode())

    def setParameterNode(self, inputParameterNode: Optional[OpenLIFUParameterNode]) -> None:
        if self._parameterNode:
            self._parameterNode.disconnectGui(self._parameterNodeGuiTag)
        self._parameterNode = inputParameterNode
        if self._parameterNode:
            self._parameterNodeGuiTag = self._parameterNode.connectGui(self.ui)

    # ------------------------------------------------------------------
    # Public navigation API
    # ------------------------------------------------------------------

    def show_page(self, module_name: str) -> None:
        """Switch the host's stacked widget to the page that hosts
        ``module_name``. No-op if the host is not yet embedded or the page
        is unknown.

        Should be called only AFTER ``slicer.util.selectModule("OpenLIFU")``
        has made the host the active module. Use
        :func:`OpenLIFULib.module_layout.navigate_to_page` from external code.
        """
        if not self._embedding_done:
            # Remember the desired page; the embedding finish callback will
            # honour it.
            self._pending_page_key = module_name
            return
        page = self._pages.get(module_name)
        if page is None or page.container is None:
            return
        if self._current_page_key == module_name:
            return
        # Hand off enter/exit so the underlying widgets keep working.
        if self._current_page_key is not None:
            self._delegate_exit(self._current_page_key)
        self.ui.pageStack.setCurrentWidget(page.container)
        self._current_page_key = module_name
        self._delegate_enter(module_name)
        # QStackedWidget page-switch and the module's enter() can both re-set
        # WState_Hidden on body chrome that embed_module_body_into originally
        # un-hid; re-clear it after every page show so the page actually paints.
        mw = slicer.util.getModuleWidget(module_name)
        if mw is not None and getattr(mw, "uiWidget", None) is not None:
            force_embedded_body_visible(mw.uiWidget, module_name=module_name)
        self._refresh_timeline_state()
        self._refresh_save_exit_state()

    # ------------------------------------------------------------------
    # Embedding
    # ------------------------------------------------------------------

    def _embed_all_pages(self) -> None:
        """Force each child module to set up and reparent its body into the
        host's QStackedWidget. Runs exactly once."""
        if self._embedding_done:
            return
        import logging
        logging.info("[OpenLIFU host] _embed_all_pages: starting embedding pass.")
        for page_def in _PAGE_DEFS:
            page = self._pages[page_def.key]
            try:
                logging.info("[OpenLIFU host] embedding %s...", page.key)
                container = embed_module_body_into(
                    module_name=page.key,
                    stacked_widget=self.ui.pageStack,
                )
                logging.info(
                    "[OpenLIFU host] embedded %s: container=%s children=%d",
                    page.key, container, container.layout().count(),
                )
            except Exception as exc:  # noqa: BLE001
                logging.exception("[OpenLIFU host] embed_module_body_into failed for %s", page.key)
                # Fall back to a placeholder so the host still loads.
                container = qt.QWidget(self.ui.pageStack)
                lay = qt.QVBoxLayout(container)
                lbl = qt.QLabel(
                    f"Failed to embed {page.key}:\n{exc}", container
                )
                lbl.setWordWrap(True)
                lay.addWidget(lbl)
                self.ui.pageStack.addWidget(container)
            page.container = container

        self._embedding_done = True
        logging.info(
            "[OpenLIFU host] embedding done; pageStack count=%d",
            self.ui.pageStack.count,
        )

        # Land on Home by default, unless something asked for a specific page
        # before we finished embedding.
        target = getattr(self, "_pending_page_key", None) or "OpenLIFUHome"
        self._pending_page_key = None
        self._current_page_key = None  # force show_page to do the swap
        self.show_page(target)
        logging.info("[OpenLIFU host] show_page(%r) done, current=%s", target, self._current_page_key)

    # ------------------------------------------------------------------
    # Per-page enter/exit delegation
    # ------------------------------------------------------------------

    def _delegate_enter(self, module_name: str) -> None:
        widget = self._get_embedded_widget(module_name)
        if widget is None:
            return
        try:
            widget.enter()
        except Exception as exc:  # noqa: BLE001
            slicer.util.errorDisplay(f"Error entering {module_name}: {exc}")

    def _delegate_exit(self, module_name: str) -> None:
        widget = self._get_embedded_widget(module_name)
        if widget is None:
            return
        try:
            widget.exit()
        except Exception:  # noqa: BLE001
            pass

    @staticmethod
    def _get_embedded_widget(module_name: str):
        try:
            return slicer.util.getModuleWidget(module_name)
        except Exception:  # noqa: BLE001
            return None

    # ------------------------------------------------------------------
    # Timeline footer
    # ------------------------------------------------------------------

    def _build_timeline_footer(self) -> None:
        layout: qt.QHBoxLayout = self.ui.hostTimelineContainer.layout()
        # Walk pages in order; insert button per timeline page, separator
        # between consecutive ones.
        first = True
        for page_def in _PAGE_DEFS:
            if not page_def.on_timeline:
                continue
            page = self._pages[page_def.key]
            if not first:
                sep = qt.QLabel("—", self.ui.hostTimelineContainer)
                sep.setAlignment(qt.Qt.AlignCenter)
                sep.setStyleSheet("color: palette(mid);")
                layout.addWidget(sep, 0)
            first = False

            btn = qt.QPushButton(page.label, self.ui.hostTimelineContainer)
            btn.setFlat(True)
            btn.setCursor(qt.QCursor(qt.Qt.PointingHandCursor))
            btn.clicked.connect(lambda _checked=False, k=page.key: self._onTimelineClicked(k))
            page.timeline_button = btn
            layout.addWidget(btn, 0)

        layout.addStretch(1)

    def _onTimelineClicked(self, key: str) -> None:
        page = self._pages.get(key)
        if page is None or page.timeline_button is None or not page.timeline_button.isEnabled():
            return
        self.show_page(key)

    def _refresh_timeline_state(self) -> None:
        """Color the timeline links and gate their enabledness.

        Gating mirrors the existing ``Workflow.furthest_module_to_which_can_proceed``
        logic: a page is reachable iff every workflow page strictly before
        it has ``can_proceed=True``. The current page is bold + colored;
        completed (reachable + strictly before current) is green; future
        unreachable is gray.
        """
        workflow = self._get_workflow()
        if workflow is None:
            return

        # Determine the furthest reachable page index along the timeline.
        timeline_keys = [p.key for p in _PAGE_DEFS if p.on_timeline]
        reachable_index = 0  # the first workflow page is always reachable
        for i, k in enumerate(timeline_keys):
            controls = workflow.workflow_controls.get(k)
            if controls is not None and controls.can_proceed:
                reachable_index = max(reachable_index, i + 1)
            else:
                break

        current_index = -1
        if self._current_page_key in timeline_keys:
            current_index = timeline_keys.index(self._current_page_key)

        for i, k in enumerate(timeline_keys):
            page = self._pages[k]
            btn = page.timeline_button
            if btn is None:
                continue
            enabled = i <= reachable_index
            btn.setEnabled(enabled)
            if i == current_index:
                # current page
                btn.setStyleSheet(
                    "QPushButton { font-weight: bold; color: #1976d2; }"
                )
            elif i < current_index:
                # completed
                btn.setStyleSheet(
                    "QPushButton { color: #2e7d32; }"
                )
            elif enabled:
                # available but not yet visited
                btn.setStyleSheet("")
            else:
                # locked
                btn.setStyleSheet(
                    "QPushButton { color: palette(mid); }"
                )
            btn.setToolTip(self._tooltip_for_timeline_page(k, enabled, i == current_index))

        # Status label = current page's status text (if any).
        status_text = ""
        if self._current_page_key is not None:
            controls = workflow.workflow_controls.get(self._current_page_key)
            if controls is not None:
                status_text = controls.status_text
        self.ui.hostStatusLabel.setText(status_text)

        # Hide the entire footer when on Home (it's not a workflow page).
        on_home = self._current_page_key == "OpenLIFUHome"
        self.ui.hostFooterContainer.setVisible(not on_home)
        self.ui.hostFooterRule.setVisible(not on_home)

    def _tooltip_for_timeline_page(self, key: str, enabled: bool, current: bool) -> str:
        page = self._pages.get(key)
        if page is None:
            return ""
        if current:
            return f"You are on {page.label}."
        if not enabled:
            workflow = self._get_workflow()
            if workflow is not None:
                # Surface the gating status text from the first locked page.
                for p_key in [p.key for p in _PAGE_DEFS if p.on_timeline]:
                    controls = workflow.workflow_controls.get(p_key)
                    if controls is not None and not controls.can_proceed:
                        return controls.status_text or f"Complete the earlier step to unlock {page.label}."
            return f"Complete the earlier step to unlock {page.label}."
        return f"Go to {page.label}."

    # ------------------------------------------------------------------
    # Save / Exit
    # ------------------------------------------------------------------

    @display_errors
    def onSaveClicked(self, checked: bool = False) -> None:
        data_logic: "OpenLIFUDataLogic" = slicer.util.getModuleLogic("OpenLIFUData")
        if data_logic.getParameterNode().loaded_session is None:
            slicer.util.errorDisplay("There is no loaded session.")
            return
        data_logic.save_session()

    @display_errors
    def onExitClicked(self, checked: bool = False) -> None:
        data_logic: "OpenLIFUDataLogic" = slicer.util.getModuleLogic("OpenLIFUData")
        if data_logic.getParameterNode().loaded_session is None:
            slicer.util.errorDisplay("There is no loaded session.")
            return
        choice = confirm_exit_session_dialog()
        if choice == "cancel":
            return
        if choice == "save":
            data_logic.save_session()
        data_logic.clear_session(clean_up_scene=True)
        self.show_page("OpenLIFUHome")

    def _refresh_save_exit_state(self) -> None:
        try:
            data_pn = slicer.util.getModuleLogic("OpenLIFUData").getParameterNode()
            has_session = data_pn.loaded_session is not None
        except Exception:  # noqa: BLE001
            has_session = False
        self.ui.hostSaveButton.setEnabled(has_session)
        self.ui.hostExitButton.setEnabled(has_session)
        self.ui.hostSaveButton.setToolTip(
            "Save the active session." if has_session else "No loaded session to save."
        )
        self.ui.hostExitButton.setToolTip(
            "Exit the active session." if has_session else "No loaded session to exit."
        )

    # ------------------------------------------------------------------
    # Cross-module redirect: hidden child modules → host page
    # ------------------------------------------------------------------

    def _install_select_module_shim(self) -> None:
        """Wrap ``slicer.util.selectModule`` so a request to switch to one of
        the embedded child modules is rewritten as "switch to OpenLIFU and
        change page". Other module names pass through untouched."""
        embedded_keys = set(self._pages.keys())
        original = slicer.util.selectModule
        # Don't double-install.
        if getattr(original, "_openlifu_host_shim", False):
            self._original_select_module = None
            return
        host = self

        def select_module_shim(module_name):
            if module_name in embedded_keys:
                original("OpenLIFU")
                host.show_page(module_name)
                return
            return original(module_name)

        select_module_shim._openlifu_host_shim = True  # type: ignore[attr-defined]
        slicer.util.selectModule = select_module_shim
        self._original_select_module = original

    def _uninstall_select_module_shim(self) -> None:
        original = getattr(self, "_original_select_module", None)
        if original is not None:
            slicer.util.selectModule = original
            self._original_select_module = None

    # ------------------------------------------------------------------
    # Workflow hook: repaint footer timeline on every state push
    # ------------------------------------------------------------------

    def _hook_workflow_updates(self) -> None:
        workflow = self._get_workflow()
        if workflow is None:
            # OpenLIFUHomeLogic may not be ready yet on a cold start; retry once.
            qt.QTimer.singleShot(100, self._hook_workflow_updates)
            return
        if getattr(workflow, "_openlifu_host_hooked", False):
            return
        original_update_all = workflow.update_all
        host = self

        def update_all_with_host_refresh():
            original_update_all()
            try:
                host._refresh_timeline_state()
            except Exception:  # noqa: BLE001
                pass

        workflow.update_all = update_all_with_host_refresh
        workflow._openlifu_host_hooked = True
        self._workflow_with_hook = workflow
        self._workflow_original_update_all = original_update_all

    def _unhook_workflow_updates(self) -> None:
        workflow = getattr(self, "_workflow_with_hook", None)
        original = getattr(self, "_workflow_original_update_all", None)
        if workflow is not None and original is not None:
            workflow.update_all = original
            try:
                delattr(workflow, "_openlifu_host_hooked")
            except AttributeError:
                pass
        self._workflow_with_hook = None
        self._workflow_original_update_all = None

    # ------------------------------------------------------------------
    # Session observers (drive Save/Exit + timeline refresh)
    # ------------------------------------------------------------------

    def _wire_session_observers(self) -> None:
        try:
            data_pn = slicer.util.getModuleLogic("OpenLIFUData").getParameterNode().parameterNode
            self.addObserver(
                data_pn,
                vtk.vtkCommand.ModifiedEvent,
                lambda *_: (self._refresh_save_exit_state(), self._refresh_timeline_state()),
            )
        except Exception:  # noqa: BLE001
            pass
        self._refresh_save_exit_state()
        self._refresh_timeline_state()

    # ------------------------------------------------------------------
    # Utility
    # ------------------------------------------------------------------

    @staticmethod
    def _get_workflow() -> Optional[Workflow]:
        try:
            home_logic: "OpenLIFUHomeLogic" = slicer.util.getModuleLogic("OpenLIFUHome")
            return getattr(home_logic, "workflow", None)
        except Exception:  # noqa: BLE001
            return None


# ---------------------------------------------------------------------------
# Logic
# ---------------------------------------------------------------------------

class OpenLIFULogic(ScriptedLoadableModuleLogic):
    """Host module's logic class. Most domain logic still lives in each
    child module; this class is reserved for shell-level state."""

    def __init__(self) -> None:
        ScriptedLoadableModuleLogic.__init__(self)

    def getParameterNode(self):
        return OpenLIFUParameterNode(super().getParameterNode())


# ---------------------------------------------------------------------------
# Test
# ---------------------------------------------------------------------------

class OpenLIFUTest(ScriptedLoadableModuleTest):
    """Minimal smoke test — full workflow integration lives in
    OpenLIFUHomeTest."""

    def setUp(self) -> None:
        slicer.mrmlScene.Clear()

    def runTest(self) -> None:
        self.setUp()
        widget_module = slicer.util.getModule("OpenLIFU")
        widget = widget_module.widgetRepresentation()
        self.assertIsNotNone(widget)
