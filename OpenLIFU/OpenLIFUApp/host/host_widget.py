"""Main widget for the OpenLIFU host module.

Owns the QStackedWidget of pages, the timeline strip at the bottom,
the shared save/exit toolbar, and the enter/exit delegation to
individual page widgets.

Split out of the original monolithic ``OpenLIFU.py`` per the
size-ceiling rule in ``docs/coding-standards.md``. Kept as a single
file because every method here operates on the same widget instance
and threading them apart would just add more indirection.

TODO(coding-standards): the internal helper methods on
``OpenLIFUHostWidget`` still carry leading underscores
(``_embed_all_pages``, ``_refresh_timeline_state`` etc.). Renamed
to public in a follow-up commit.
"""

from __future__ import annotations

import logging
from typing import Callable, Dict, Optional, Set

import qt
import slicer
from slicer.ScriptedLoadableModule import ScriptedLoadableModuleWidget
from slicer.util import VTKObservationMixin

from OpenLIFULib import ensure_python_requirements_for_module_enter
from OpenLIFULib.guided_mode_util import (
    confirm_exit_session_dialog,
    Workflow,
)
from OpenLIFULib.module_layout import (
    ModuleHeaderWidget,
    icon_color_neutral,
    tinted_icon,
    wire_passive_module_header,
)
from OpenLIFULib.util import display_errors, session_is_dirty

from OpenLIFUApp.logic.app_state import OpenLIFUAppState, get_app_state_signals
from OpenLIFUApp.logic.session_actions import (
    close_loaded_sessions,
    save_loaded_session,
)
from OpenLIFUApp.host.host_logic import OpenLIFUHostLogic
from OpenLIFUApp.host.page_registry import PAGE_DEFS, Page
from OpenLIFUApp.host.timeline_widget import TimelineWidget


class OpenLIFUHostWidget(ScriptedLoadableModuleWidget, VTKObservationMixin):
    """Main Slicer widget for the OpenLIFU host module.

    Loads ``Resources/UI/OpenLIFU.ui`` (the host chrome: title, save/exit
    toolbar, page stack, timeline footer), embeds each ``PAGE_DEFS`` entry
    into the page stack at startup, and dispatches ``enter()``/``exit()``
    to page widgets as the user navigates.
    """

    def __init__(self, parent=None) -> None:
        ScriptedLoadableModuleWidget.__init__(self, parent)
        VTKObservationMixin.__init__(self)
        # Slicer's ``ScriptedLoadableModuleWidget.__init__`` derives
        # ``self.moduleName`` from this class name by stripping the trailing
        # ``Widget``. Since we live under ``OpenLIFUApp/host/`` and are
        # re-exported from ``OpenLIFU.py`` as ``OpenLIFUWidget``, the class
        # name here is ``OpenLIFUHostWidget`` and the default derivation
        # would give ``"OpenLIFUHost"``. That name has no matching Slicer
        # module and ``self.resourcePath("UI/OpenLIFUHost.ui")`` would then
        # crash in ``setupDeveloperSection`` inside the base ``setup()``.
        # Set it explicitly so every path (resource lookup, developer
        # section UI, embedded-page dispatch) resolves through the real
        # Slicer module registered by ``OpenLIFU.py``.
        self.moduleName = "OpenLIFU"
        self.logic: Optional[OpenLIFUHostLogic] = None
        self._parameterNode = None
        self._parameterNodeGuiTag = None

        # Page registry, keyed by module name.
        self._pages: Dict[str, Page] = {
            p.key: Page(p.key, p.label, p.on_timeline) for p in PAGE_DEFS
        }
        # Owned page-widget instances, keyed by module name. Populated in
        # ``_embed_all_pages``.
        self._page_widgets: Dict[str, ScriptedLoadableModuleWidget] = {}
        self._current_page_key: Optional[str] = None
        self._embedding_done: bool = False
        # Timeline keys the user has navigated to at least once. Persists
        # across back-navigation so completed steps do not "unfill" when
        # revisited.
        self._visited_timeline_keys: Set[str] = set()
        # Custom-painted timeline footer widget; created in
        # ``_build_timeline_footer``.
        self._timeline_widget: Optional[TimelineWidget] = None
        # Bookkeeping for the workflow.update_all wrapper installed inline
        # from setup().
        self._workflow_with_hook: Optional[Workflow] = None
        self._workflow_original_update_all: Optional[Callable[[], None]] = None

    # ------------------------------------------------------------------
    # Slicer widget lifecycle
    # ------------------------------------------------------------------

    def setup(self) -> None:
        ScriptedLoadableModuleWidget.setup(self)

        uiWidget = slicer.util.loadUI(self.resourcePath("UI/OpenLIFU.ui"))
        self.layout.addWidget(uiWidget)
        self.uiWidget = uiWidget
        self.ui = slicer.util.childWidgetVariables(uiWidget)
        uiWidget.setMRMLScene(slicer.mrmlScene)

        self.logic = OpenLIFUHostLogic()

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
        # The button icons are set from ``_refresh_save_exit_state`` so they
        # retint as ``dim`` when the button is disabled (no session) and
        # ``neutral`` (theme-appropriate button text colour) when enabled.
        self.ui.hostSaveButton.setIconSize(qt.QSize(20, 20))
        self.ui.hostExitButton.setIconSize(qt.QSize(20, 20))
        self.ui.hostSaveButton.clicked.connect(self.onSaveClicked)
        self.ui.hostExitButton.clicked.connect(self.onExitClicked)

        # ---- Next (advance) ----
        self.ui.hostNextButton.clicked.connect(self.onNextClicked)

        # ---- Back to Home (only shown on non-timeline pages like Data) ----
        self.ui.hostBackToHomeButton.clicked.connect(self.onBackToHomeClicked)
        self.ui.hostBackToHomeButton.setVisible(False)

        # ---- Build the timeline footer ----
        self._build_timeline_footer()

        # Defer the actual embedding pass to the next event-loop tick so
        # Slicer has fully finished loading the host module before we start
        # instantiating page Widget classes.
        qt.QTimer.singleShot(0, self._embed_all_pages)

        # Observe Data's parameter node so Save / Exit / status track session state.
        qt.QTimer.singleShot(0, self._wire_session_observers)

        # Hook Workflow.update_all so timeline + status repaint after every
        # workflow state push.
        workflow = self._get_workflow()
        if workflow is not None:
            original_update_all = workflow.update_all
            host = self

            def update_all_with_host_refresh():
                original_update_all()
                try:
                    host._refresh_timeline_state()
                except Exception:  # noqa: BLE001
                    pass

            workflow.update_all = update_all_with_host_refresh
            self._workflow_with_hook = workflow
            self._workflow_original_update_all = original_update_all

        # Initial Save/Exit state: no session yet, so disabled + dim icons.
        self._refresh_save_exit_state()

    def cleanup(self) -> None:
        self.removeObservers()
        try:
            get_app_state_signals().dataChanged.disconnect(self._on_app_state_changed)
        except Exception:  # noqa: BLE001
            pass
        # Restore workflow.update_all if we wrapped it in setup().
        workflow = self._workflow_with_hook
        original = self._workflow_original_update_all
        if workflow is not None and original is not None:
            try:
                workflow.update_all = original
            except Exception:  # noqa: BLE001
                pass
        self._workflow_with_hook = None
        self._workflow_original_update_all = None
        # Call cleanup() on each owned page widget so their observers /
        # module-callbacks unwind cleanly.
        for key, widget in list(self._page_widgets.items()):
            try:
                widget.cleanup()
            except Exception:  # noqa: BLE001
                logging.exception("[OpenLIFU host] cleanup() failed for %s", key)
        self._page_widgets.clear()

    def enter(self) -> None:
        ensure_python_requirements_for_module_enter()
        self.initializeParameterNode()
        if self._embedding_done and self._current_page_key is not None:
            self._delegate_enter(self._current_page_key)
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

    def setParameterNode(self, inputParameterNode: Optional[OpenLIFUAppState]) -> None:
        if self._parameterNode:
            self._parameterNode.disconnectGui(self._parameterNodeGuiTag)
        self._parameterNode = inputParameterNode
        if self._parameterNode:
            self._parameterNodeGuiTag = self._parameterNode.connectGui(self.ui)

    # ------------------------------------------------------------------
    # Public navigation API
    # ------------------------------------------------------------------

    def show_page(self, module_name: str) -> None:
        """Switch the host's stacked widget to the page that hosts ``module_name``.

        No-op if the host is not yet embedded or the page is unknown.
        Should be called only AFTER ``slicer.util.selectModule("OpenLIFU")``
        has made the host the active module.
        """
        if not self._embedding_done:
            # Remember the desired page; the embedding-finish callback will honour it.
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
        # Mark the page as visited (only timeline pages count; Home is not).
        if page.on_timeline:
            self._visited_timeline_keys.add(module_name)
        self._delegate_enter(module_name)
        self._refresh_timeline_state()
        self._refresh_save_exit_state()

    def get_page_widget(self, module_name: str):
        """Public accessor for a page's ``ScriptedLoadableModuleWidget``."""
        return self._page_widgets.get(module_name)

    # ------------------------------------------------------------------
    # Embedding
    # ------------------------------------------------------------------

    def _embed_all_pages(self) -> None:
        """Instantiate every page's ``Widget`` class and reparent its body
        into the host's ``QStackedWidget``. Runs exactly once."""
        if self._embedding_done:
            return
        logging.info("[OpenLIFU host] _embed_all_pages: starting embedding pass.")

        # Local imports here to avoid load-time circulars: each page module
        # imports symbols from OpenLIFULib/other pages at load time and
        # OpenLIFU.py is imported early by Slicer's module discovery pass.
        from OpenLIFUApp.pages.home_page import OpenLIFUHomeWidget
        from OpenLIFUApp.pages.data_manager_page import OpenLIFUDataManagerWidget
        from OpenLIFUApp.pages.planning_session_overview_page import (
            OpenLIFUPlanningSessionOverviewWidget,
        )
        from OpenLIFUApp.pages.sonication_session_overview_page import (
            OpenLIFUSonicationSessionOverviewWidget,
        )
        from OpenLIFUApp.pages.target_selection_page import (
            OpenLIFUTargetSelectionWidget,
        )

        widget_classes = {
            "OpenLIFUHome":                      OpenLIFUHomeWidget,
            "OpenLIFUDataManager":               OpenLIFUDataManagerWidget,
            "OpenLIFUPlanningSessionOverview":   OpenLIFUPlanningSessionOverviewWidget,
            "OpenLIFUSonicationSessionOverview": OpenLIFUSonicationSessionOverviewWidget,
            "OpenLIFUTargetSelection":           OpenLIFUTargetSelectionWidget,
        }

        for page_def in PAGE_DEFS:
            page = self._pages[page_def.key]
            widget_class = widget_classes[page_def.key]
            try:
                logging.info("[OpenLIFU host] embedding %s...", page.key)
                widget = self._instantiate_page_widget(widget_class)
                self._page_widgets[page.key] = widget

                container = self._embed_page_widget_into_stack(widget)
                page.container = container

                logging.info(
                    "[OpenLIFU host] embedded %s: container=%s children=%d",
                    page.key, container, container.layout().count(),
                )
            except Exception as exc:  # noqa: BLE001
                logging.exception("[OpenLIFU host] embedding failed for %s", page.key)
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

    def _instantiate_page_widget(self, widget_class):
        """Construct a page widget and call ``setup()`` on it in the right order.

        ``ScriptedLoadableModuleWidget.__init__`` auto-creates a
        ``qMRMLWidget`` parent AND auto-calls ``setup()`` **when parent is
        None**. Every page widget's ``__init__`` sets
        ``self.moduleName = "OpenLIFU"`` *after* the super ``__init__``, so
        the parent-less path invokes ``setup()`` before ``moduleName`` has
        been redirected -- which makes ``self.resourcePath("UI/...")`` look
        under the (deleted) per-page module directory and raise
        ``Could not load UI file``.

        We work around that by supplying a real (but invisible) parent so
        the base class skips its auto-setup path, then explicitly calling
        ``widget.setup()`` after ``moduleName`` has been assigned.
        """
        parent_qwidget = qt.QWidget()
        qt.QVBoxLayout(parent_qwidget)  # base __init__ captures self.layout = parent.layout()
        parent_qwidget.setVisible(False)
        widget = widget_class(parent=parent_qwidget)
        widget.setup()
        return widget

    def _embed_page_widget_into_stack(self, widget) -> qt.QWidget:
        """Reparent ``widget.uiWidget`` into a new page of ``self.ui.pageStack``.

        Each embedded page's ``setup()`` must expose ``self.uiWidget`` (the
        top-level widget it built) so the host can take ownership of it.
        Per-page shared headers and workflow-controls placeholders are hidden;
        the host module provides a single shared header and footer.
        """
        ui_widget = getattr(widget, "uiWidget", None)
        if ui_widget is None:
            raise RuntimeError(
                f"Page widget {type(widget).__name__!r} did not expose "
                "self.uiWidget; add `self.uiWidget = uiWidget` in its "
                "setup() to enable embedding."
            )

        page = qt.QWidget(self.ui.pageStack)
        page_layout = qt.QVBoxLayout(page)
        page_layout.setContentsMargins(0, 0, 0, 0)
        page_layout.setSpacing(0)
        page_layout.addWidget(ui_widget)
        self.ui.pageStack.addWidget(page)

        module_header = getattr(widget, "module_header", None)
        if module_header is not None:
            module_header.setVisible(False)

        workflow_controls = getattr(widget, "workflow_controls", None)
        if workflow_controls is not None:
            workflow_controls.setVisible(False)
        placeholder = ui_widget.findChild(qt.QWidget, "workflowControlsPlaceholder")
        if placeholder is not None and workflow_controls is None:
            placeholder.setVisible(False)

        ui_widget.show()

        return page

    # ------------------------------------------------------------------
    # Per-page enter/exit delegation
    # ------------------------------------------------------------------

    def _delegate_enter(self, module_name: str) -> None:
        widget = self._page_widgets.get(module_name)
        if widget is None:
            return
        try:
            widget.enter()
        except Exception as exc:  # noqa: BLE001
            slicer.util.errorDisplay(f"Error entering {module_name}: {exc}")

    def _delegate_exit(self, module_name: str) -> None:
        widget = self._page_widgets.get(module_name)
        if widget is None:
            return
        try:
            widget.exit()
        except Exception:  # noqa: BLE001
            pass

    # ------------------------------------------------------------------
    # Timeline footer
    # ------------------------------------------------------------------

    def _build_timeline_footer(self) -> None:
        layout: qt.QHBoxLayout = self.ui.hostTimelineContainer.layout()
        self._timeline_widget = TimelineWidget(self.ui.hostTimelineContainer)
        items = [(p.key, self._pages[p.key].label) for p in PAGE_DEFS if p.on_timeline]
        self._timeline_widget.setItems(items)
        self._timeline_widget.setOnClick(self._onTimelineClicked)
        layout.addWidget(self._timeline_widget, 1)

    def _onTimelineClicked(self, key: str) -> None:
        page = self._pages.get(key)
        if page is None:
            return
        self.show_page(key)

    def _refresh_timeline_state(self) -> None:
        """Push the current visited/reachable/current state into the timeline
        widget, and update the Next button + status label.

        "Reachable" mirrors :py:meth:`Workflow.furthest_module_to_which_can_proceed`:
        a page is reachable iff every workflow page strictly before it has
        ``can_proceed=True``. The current page also counts as reachable. The
        visited set is monotonic across the host lifetime, updated in
        :py:meth:`show_page`.
        """
        workflow = self._get_workflow()
        if workflow is None:
            return

        timeline_keys = [p.key for p in PAGE_DEFS if p.on_timeline]

        # Furthest reachable index.
        reachable_index = 0  # first workflow page is always reachable
        for i, k in enumerate(timeline_keys):
            controls = workflow.workflow_controls.get(k)
            if controls is not None and controls.can_proceed:
                reachable_index = max(reachable_index, i + 1)
            else:
                break
        reachable_keys = {
            timeline_keys[i]
            for i in range(min(reachable_index + 1, len(timeline_keys)))
        }
        # Visited keys are always reachable (the user got there somehow).
        reachable_keys |= self._visited_timeline_keys

        current_key = (
            self._current_page_key
            if self._current_page_key in timeline_keys else None
        )

        if self._timeline_widget is not None:
            self._timeline_widget.setState(
                visited=self._visited_timeline_keys,
                reachable=reachable_keys,
                current_key=current_key,
            )

        # Next button: enabled when the current page has can_proceed and there
        # is a next page to advance to. Hidden when not on a timeline page.
        next_button = self.ui.hostNextButton
        current_index = (
            timeline_keys.index(current_key) if current_key is not None else -1
        )
        if current_index < 0 or current_index >= len(timeline_keys) - 1:
            next_button.setEnabled(False)
            next_button.setVisible(current_index >= 0)
            next_button.setText("Next \u25b6")
            next_button.setToolTip(
                "You are on the final step." if current_index == len(timeline_keys) - 1
                else "Advance to the next step"
            )
        else:
            current_controls = workflow.workflow_controls.get(self._current_page_key)
            can_proceed = bool(current_controls and current_controls.can_proceed)
            next_button.setVisible(True)
            next_button.setEnabled(can_proceed)
            next_page = self._pages.get(timeline_keys[current_index + 1])
            next_label = next_page.label if next_page is not None else ""
            next_button.setText(
                f"Next: {next_label} \u25b6" if next_label else "Next \u25b6"
            )
            next_button.setToolTip(
                f"Advance to {next_label}." if can_proceed
                else "Complete the current step to advance."
            )

        # Status label = current page's status text (if any).
        status_text = ""
        if self._current_page_key is not None:
            controls = workflow.workflow_controls.get(self._current_page_key)
            if controls is not None:
                status_text = controls.status_text
        self.ui.hostStatusLabel.setText(status_text)

        # Footer visibility per page:
        #   * Home:              footer fully hidden (nowhere to go "back" to)
        #   * Timeline page:     footer visible with timeline + Next + status
        #                        + Back-to-Home (so the user can bail out of
        #                        the workflow without waiting for a session
        #                        prompt they may not have if the toolbar Exit
        #                        button is unavailable). See SlicerOpenLIFU#641.
        #   * Any other page
        #     (Data Manager,
        #      Session Overviews): footer visible with only the Back-to-Home button
        #
        # The "other page" bucket used to be hard-coded to the legacy
        # ``OpenLIFUData`` module key, which meant the current
        # ``OpenLIFUDataManager`` page never got a Back-to-Home button and
        # users with no loaded session (where the toolbar Exit is disabled)
        # had no way off it -- SlicerOpenLIFU#637.
        on_home = self._current_page_key == "OpenLIFUHome"
        on_timeline_page = current_key is not None
        show_back_button = (
            self._current_page_key is not None
            and not on_home
        )
        footer_visible = on_timeline_page or show_back_button
        self.ui.hostFooterContainer.setVisible(footer_visible)
        self.ui.hostFooterRule.setVisible(footer_visible)
        self.ui.hostBackToHomeButton.setVisible(show_back_button)
        self.ui.hostTimelineContainer.setVisible(on_timeline_page)
        self.ui.hostStatusLabel.setVisible(on_timeline_page)

    # ------------------------------------------------------------------
    # Save / Exit
    # ------------------------------------------------------------------

    @display_errors
    def onSaveClicked(self, checked: bool = False) -> None:
        """Save the currently-loaded PlanningSession or SonicationSession, if any."""
        try:
            save_loaded_session()
        except RuntimeError:
            slicer.util.errorDisplay("There is no loaded session.")

    @display_errors
    def onNextClicked(self, checked: bool = False) -> None:
        timeline_keys = [p.key for p in PAGE_DEFS if p.on_timeline]
        if self._current_page_key not in timeline_keys:
            return
        current_index = timeline_keys.index(self._current_page_key)
        if current_index >= len(timeline_keys) - 1:
            return
        workflow = self._get_workflow()
        if workflow is None:
            return
        current_controls = workflow.workflow_controls.get(self._current_page_key)
        if current_controls is None or not current_controls.can_proceed:
            return
        self.show_page(timeline_keys[current_index + 1])

    @display_errors
    def onExitClicked(self, checked: bool = False) -> None:
        """Close the loaded session (planning or sonication) and return Home."""
        state = self.logic.getParameterNode()
        if state.loaded_planning_session is None and state.loaded_sonication_session is None:
            slicer.util.errorDisplay("There is no loaded session.")
            return
        choice = confirm_exit_session_dialog()
        if choice == "cancel":
            return
        if choice == "save":
            try:
                save_loaded_session()
            except RuntimeError:
                pass
        close_loaded_sessions()
        self.show_page("OpenLIFUHome")

    @display_errors
    def onBackToHomeClicked(self, checked: bool = False) -> None:
        """Return to the Home page, prompting to save or discard any loaded
        session first."""
        state = self.logic.getParameterNode()
        if state.loaded_planning_session is not None or state.loaded_sonication_session is not None:
            choice = confirm_exit_session_dialog()
            if choice == "cancel":
                return
            if choice == "save":
                try:
                    save_loaded_session()
                except RuntimeError:
                    pass
            close_loaded_sessions()
        self.show_page("OpenLIFUHome")

    def _refresh_save_exit_state(self) -> None:
        """Update the host's Save/Exit button state from app state.

        Save button:
            Visible + enabled iff a session is loaded AND has unsaved
            in-memory changes (``session_is_dirty``). Hidden entirely
            when there's nothing to save -- a greyed-out button in the
            primary header slot is more visually distracting than
            informative (SlicerOpenLIFU#638).

        Exit button:
            Visible + enabled iff a session is loaded. Hidden when
            there's no session to exit -- the footer's Back-to-Home
            button already gives the user a navigation escape on
            non-Home pages (SlicerOpenLIFU#637 broadened its
            visibility).
        """
        try:
            state = self.logic.getParameterNode()
            has_session = (
                state.loaded_planning_session is not None
                or state.loaded_sonication_session is not None
            )
        except Exception:  # noqa: BLE001
            has_session = False
        is_dirty = has_session and session_is_dirty()

        self.ui.hostSaveButton.setVisible(is_dirty)
        self.ui.hostSaveButton.setEnabled(is_dirty)
        self.ui.hostExitButton.setVisible(has_session)
        self.ui.hostExitButton.setEnabled(has_session)

        # Icons + tooltips still get set for the visible cases so a
        # session-loaded-then-cleaned transition doesn't briefly flash a
        # stale icon before the visibility toggle applies.
        self.ui.hostSaveButton.setIcon(tinted_icon("save.png", icon_color_neutral()))
        self.ui.hostExitButton.setIcon(tinted_icon("exit.png", icon_color_neutral()))
        self.ui.hostSaveButton.setToolTip("Save unsaved changes to disk.")
        self.ui.hostExitButton.setToolTip("Exit the active session.")

    # ------------------------------------------------------------------
    # Session observers (drive Save/Exit + timeline refresh)
    # ------------------------------------------------------------------

    def _wire_session_observers(self) -> None:
        try:
            get_app_state_signals().dataChanged.connect(self._on_app_state_changed)
        except Exception:  # noqa: BLE001
            pass
        self._refresh_save_exit_state()
        self._refresh_timeline_state()

    def _on_app_state_changed(self) -> None:
        self._refresh_save_exit_state()
        self._refresh_timeline_state()

    # ------------------------------------------------------------------
    # Utility
    # ------------------------------------------------------------------

    def _get_workflow(self) -> Optional[Workflow]:
        """Return the host's Workflow instance."""
        try:
            return self.logic.workflow
        except Exception:  # noqa: BLE001
            return None
