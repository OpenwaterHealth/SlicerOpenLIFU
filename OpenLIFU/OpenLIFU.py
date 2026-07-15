# Standard library imports
from __future__ import annotations

import logging
import os
from typing import Callable, Dict, List, Optional, Set, Tuple, TYPE_CHECKING

# Third-party imports
import qt
import vtk

# Slicer imports
import slicer
from slicer.ScriptedLoadableModule import *
from slicer.i18n import tr as _
from slicer.i18n import translate
from slicer.util import VTKObservationMixin

# OpenLIFULib imports
from OpenLIFULib import ensure_python_requirements_for_module_enter
from OpenLIFULib.guided_mode_util import (
    confirm_exit_session_dialog,
    set_guided_mode_state,
    Workflow,
)
from OpenLIFULib.module_layout import (
    ModuleHeaderWidget,
    icon_color_dim,
    icon_color_neutral,
    tinted_icon,
    wire_passive_module_header,
)
from OpenLIFULib.util import display_errors

# Host's parameter node type — relocated from OpenLIFUData in Round 5b.
from OpenLIFUApp.logic.app_state import OpenLIFUAppState, get_app_state_signals

if TYPE_CHECKING:
    from OpenLIFUApp.pages.data_page import OpenLIFUDataLogic


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
        # Round 5c-3 Commit D deleted the nine per-page shim modules; the host
        # now owns every Widget/Logic/Test. No inter-module Slicer
        # ``dependencies`` remain.
        self.parent.dependencies = []
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

# The host module's parameter-node type is :class:`OpenLIFUAppState`, imported
# above. Round 5b of DEMODULING.md relocated the AppState's underlying MRML
# singleton from ``OpenLIFUData`` to this module; ``OpenLIFULogic``,
# ``OpenLIFUDataLogic.getParameterNode()``, and ``get_app_state()`` all
# resolve to the same wrapper around the OpenLIFU host module's node.


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
    """

    def __init__(self, key: str, label: str, on_timeline: bool) -> None:
        self.key = key
        self.label = label
        self.on_timeline = on_timeline
        self.container: Optional[qt.QWidget] = None


# Ordered list of pages. Anything `on_timeline=True` appears in the footer
# timeline in the order given here. Pages with `on_timeline=False` are
# embedded but not shown on the workflow timeline (Home, Data Manager) --
# they are reachable via the in-module navigation that lives on those pages.
_PAGE_DEFS: List[_Page] = [
    _Page("OpenLIFUHome",                  "Home",         on_timeline=False),
    _Page("OpenLIFUData",                  "Data",         on_timeline=False),
    _Page("OpenLIFUSession",               "Session",      on_timeline=True),
    _Page("OpenLIFUPrePlanning",           "Pre-Planning", on_timeline=True),
    _Page("OpenLIFUTransducerLocalization","Localization", on_timeline=True),
    _Page("OpenLIFUSonicationPlanner",     "Solution",     on_timeline=True),
    _Page("OpenLIFUSonicationControl",     "Control",      on_timeline=True),
]


# ---------------------------------------------------------------------------
# Timeline widget (custom-painted "O---O---O---o" progress strip)
# ---------------------------------------------------------------------------

class _TimelineWidget(qt.QWidget):
    """Renders a sequence of labelled circles joined by connector lines.

    Each step has three pieces of state pushed in from the host:

    - ``visited``: filled circle (the user has navigated through this step).
    - ``reachable``: clickable circle (workflow gating allows landing here).
    - ``current``: the active page — drawn with a second outer ring.

    Connector ``i ↔ i+1`` is drawn "completed" (thicker, colored) iff both
    endpoint keys are in ``visited``. Once visited, a step never reverts to
    hollow, so clicking back through completed steps doesn't undo progress.
    """

    # Geometry constants
    _R_FILLED = 9       # filled-circle radius
    _R_HOLLOW = 7       # hollow-circle radius
    _RING_GAP = 4       # gap between the filled circle and the outer "current" ring
    _LINE_WIDTH_DONE = 4
    _LINE_WIDTH_LOCKED = 2
    _LABEL_GAP = 6      # vertical gap between circle and label
    _MIN_STEP_PX = 60   # minimum horizontal spacing between circle centers

    def __init__(self, parent: Optional[qt.QWidget] = None) -> None:
        super().__init__(parent)
        self._items: List[Tuple[str, str]] = []          # (key, label)
        self._visited: Set[str] = set()
        self._reachable: Set[str] = set()
        self._current_key: Optional[str] = None
        self._on_click: Optional[Callable[[str], None]] = None
        self.setSizePolicy(qt.QSizePolicy.Expanding, qt.QSizePolicy.Preferred)
        self.setMouseTracking(True)

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def setItems(self, items: List[Tuple[str, str]]) -> None:
        self._items = list(items)
        self.updateGeometry()
        self.update()

    def setState(
        self,
        *,
        visited: Optional[Set[str]] = None,
        reachable: Optional[Set[str]] = None,
        current_key: Optional[str] = None,
    ) -> None:
        if visited is not None:
            self._visited = set(visited)
        if reachable is not None:
            self._reachable = set(reachable)
        self._current_key = current_key
        self.update()

    def setOnClick(self, callback: Callable[[str], None]) -> None:
        self._on_click = callback

    # ------------------------------------------------------------------
    # Qt overrides
    # ------------------------------------------------------------------

    def sizeHint(self) -> qt.QSize:
        fm = self.fontMetrics()
        h = 2 * (self._R_FILLED + self._RING_GAP + 2) + self._LABEL_GAP + fm.height() + 6
        w = max(200, self._MIN_STEP_PX * max(1, len(self._items)))
        return qt.QSize(w, h)

    def minimumSizeHint(self) -> qt.QSize:
        fm = self.fontMetrics()
        h = 2 * (self._R_FILLED + self._RING_GAP + 2) + self._LABEL_GAP + fm.height() + 6
        return qt.QSize(120, h)

    def paintEvent(self, event) -> None:  # noqa: ARG002
        if not self._items:
            return
        painter = qt.QPainter(self)
        painter.setRenderHint(qt.QPainter.Antialiasing, True)
        try:
            self._paint(painter)
        finally:
            painter.end()

    def mousePressEvent(self, event) -> None:
        if event.button() != qt.Qt.LeftButton:
            return
        key = self._hit_test(event.pos())
        if key is not None and key in self._reachable and self._on_click is not None:
            self._on_click(key)

    def mouseMoveEvent(self, event) -> None:
        key = self._hit_test(event.pos())
        if key is not None:
            if key in self._reachable:
                self.setCursor(qt.QCursor(qt.Qt.PointingHandCursor))
            else:
                self.unsetCursor()
            label = next((lbl for k, lbl in self._items if k == key), key)
            if key == self._current_key:
                tip = f"You are on {label}."
            elif key in self._reachable:
                tip = f"Go to {label}."
            else:
                tip = f"Complete the earlier step to unlock {label}."
            self.setToolTip(tip)
        else:
            self.unsetCursor()
            self.setToolTip("")

    # ------------------------------------------------------------------
    # Geometry / hit-testing
    # ------------------------------------------------------------------

    def _circle_centers(self) -> List[Tuple[str, str, int, int]]:
        """Return [(key, label, cx, cy)] for the current widget size."""
        n = len(self._items)
        if n == 0:
            return []
        # PythonQt exposes Qt's Q_PROPERTYs (``width``, ``height``, ``palette``,
        # etc.) as auto-resolved attributes, so calling them as methods raises
        # ``TypeError: 'int' object is not callable``.
        w = self.width
        # Reserve enough horizontal margin that the first/last labels fit.
        fm = self.fontMetrics()
        # Use width() (compatible across Qt 5.x) rather than horizontalAdvance.
        first_half = fm.width(self._items[0][1]) // 2
        last_half = fm.width(self._items[-1][1]) // 2
        edge = self._R_FILLED + self._RING_GAP + 2
        left = max(edge, first_half + 4)
        right = w - max(edge, last_half + 4)
        if n == 1 or right <= left:
            xs = [w // 2 for _ in range(n)]
        else:
            step = (right - left) / float(n - 1)
            xs = [int(round(left + i * step)) for i in range(n)]
        cy = self._R_FILLED + self._RING_GAP + 2
        return [(self._items[i][0], self._items[i][1], xs[i], cy) for i in range(n)]

    def _hit_test(self, point: qt.QPoint) -> Optional[str]:
        # Generous hit radius: cover the outer ring and the label below.
        hit_r2 = (self._R_FILLED + self._RING_GAP + 6) ** 2
        for key, _, cx, cy in self._circle_centers():
            dx = point.x() - cx
            dy = point.y() - cy
            if dx * dx + dy * dy <= hit_r2:
                return key
        return None

    # ------------------------------------------------------------------
    # Painting
    # ------------------------------------------------------------------

    def _theme_colors(self) -> Dict[str, qt.QColor]:
        """Pick colors that contrast in both light and dark palettes."""
        # PythonQt exposes Qt's Q_PROPERTYs as attributes, so ``palette`` is
        # already a QPalette object — calling it would raise TypeError.
        palette = self.palette
        window = palette.color(qt.QPalette.Window)
        is_dark = (window.red() + window.green() + window.blue()) // 3 < 128
        if is_dark:
            return {
                "locked": qt.QColor("#b0b6bd"),    # bright enough on dark bg
                "done": qt.QColor("#5aa9ff"),
                "current_ring": qt.QColor("#5aa9ff"),
                "label_done": palette.color(qt.QPalette.WindowText),
                "label_locked": qt.QColor("#9aa0a6"),
                "label_current": qt.QColor("#82c1ff"),
            }
        return {
            "locked": qt.QColor("#5f6368"),
            "done": qt.QColor("#1976d2"),
            "current_ring": qt.QColor("#1976d2"),
            "label_done": palette.color(qt.QPalette.WindowText),
            "label_locked": qt.QColor("#5f6368"),
            "label_current": qt.QColor("#1565c0"),
        }

    def _paint(self, p: qt.QPainter) -> None:
        colors = self._theme_colors()
        centers = self._circle_centers()
        if not centers:
            return

        # 1. Connector lines (drawn first so circles sit on top).
        for i in range(len(centers) - 1):
            k1, _, x1, y = centers[i]
            k2, _, x2, _ = centers[i + 1]
            completed = (k1 in self._visited and k2 in self._visited)
            pen = qt.QPen(colors["done"] if completed else colors["locked"])
            pen.setWidth(self._LINE_WIDTH_DONE if completed else self._LINE_WIDTH_LOCKED)
            pen.setCapStyle(qt.Qt.RoundCap)
            p.setPen(pen)
            # Inset so the line doesn't dive into the circles
            inset = self._R_FILLED + 1
            p.drawLine(x1 + inset, y, x2 - inset, y)

        # 2. Circles + labels
        fm = self.fontMetrics()
        for key, label, cx, cy in centers:
            visited = key in self._visited
            current = (key == self._current_key)

            if visited:
                # Filled circle
                p.setBrush(qt.QBrush(colors["done"]))
                p.setPen(qt.QPen(colors["done"], 2))
                p.drawEllipse(qt.QPoint(cx, cy), self._R_FILLED, self._R_FILLED)
            else:
                # Hollow circle
                p.setBrush(qt.QBrush(qt.Qt.NoBrush))
                pen = qt.QPen(colors["locked"])
                pen.setWidth(2)
                p.setPen(pen)
                p.drawEllipse(qt.QPoint(cx, cy), self._R_HOLLOW, self._R_HOLLOW)

            # Outer "double-outline" ring for the current step
            if current:
                ring_r = self._R_FILLED + self._RING_GAP
                pen = qt.QPen(colors["current_ring"])
                pen.setWidth(2)
                p.setBrush(qt.QBrush(qt.Qt.NoBrush))
                p.setPen(pen)
                p.drawEllipse(qt.QPoint(cx, cy), ring_r, ring_r)

            # Label below circle
            if current:
                p.setPen(colors["label_current"])
            elif visited:
                p.setPen(colors["label_done"])
            else:
                p.setPen(colors["label_locked"])
            tw = fm.width(label)
            text_y = cy + self._R_FILLED + self._RING_GAP + 2 + fm.ascent()
            p.drawText(cx - tw // 2, text_y, label)


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
        # Owned page-widget instances, keyed by module name. Populated in
        # ``_embed_all_pages`` for stacked pages (Home, Data, Session,
        # PrePlanning, TransducerLocalization, SonicationPlanner,
        # SonicationControl) and for popup-only pages (Database, Login).
        self._page_widgets: Dict[str, ScriptedLoadableModuleWidget] = {}
        self._current_page_key: Optional[str] = None
        self._embedding_done: bool = False
        # Timeline keys the user has navigated to at least once. Persists across
        # back-navigation so completed steps don't "unfill" when revisited.
        self._visited_timeline_keys: Set[str] = set()
        # Custom-painted timeline footer widget; created in _build_timeline_footer.
        self._timeline_widget: Optional[_TimelineWidget] = None
        # Bookkeeping for the workflow.update_all wrapper installed inline
        # from setup() (formerly done by _hook_workflow_updates).
        self._workflow_with_hook: Optional[Workflow] = None
        self._workflow_original_update_all: Optional[Callable[[], None]] = None

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
        # The button icons are set from ``_refresh_save_exit_state`` so
        # they retint as ``dim`` when the button is disabled (no session)
        # and ``neutral`` (theme-appropriate button text colour) when
        # enabled. Emoji text kept its full colour when the button was
        # disabled, making the buttons look clickable even when Save/Exit
        # had nothing to act on -- switching to a bitmap glyph lets Qt
        # apply its native disabled greyscaling, and re-tinting on state
        # changes gives us high-contrast icons when actionable.
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
        # that Slicer has fully finished loading the host module before we
        # start instantiating page Widget classes. (Historically this was
        # also needed to avoid re-entrancy through the shim modules; those
        # are gone as of 5c-3 but the deferral is still nice for startup
        # perf and to keep the Slicer main-window paint responsive.)
        qt.QTimer.singleShot(0, self._embed_all_pages)

        # Observe Data's parameter node so Save / Exit / status track session state.
        qt.QTimer.singleShot(0, self._wire_session_observers)

        # Hook Workflow.update_all so timeline + status repaint after every
        # workflow state push. Do this inline (no external helper) because
        # the wrap is a one-liner and the workflow is guaranteed to exist
        # (it's constructed in OpenLIFULogic.__init__ above).
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
        # The session observers wired below will re-run this on every state
        # change, so tint stays in sync.
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
        # module-callbacks unwind cleanly. Failures are logged but not
        # fatal — Slicer is tearing the module down.
        import logging
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
        # Mark the page as visited (only timeline pages count; Home is not).
        if page.on_timeline:
            self._visited_timeline_keys.add(module_name)
        self._delegate_enter(module_name)
        self._refresh_timeline_state()
        self._refresh_save_exit_state()

    # ------------------------------------------------------------------
    # Embedding
    # ------------------------------------------------------------------

    def _embed_all_pages(self) -> None:
        """Instantiate every page's ``Widget`` class and reparent its body
        into the host's ``QStackedWidget`` (for pages in ``_PAGE_DEFS``) or
        keep it as an off-screen widget for popup-only pages (Database,
        Login). Runs exactly once.

        Round 5c-3 replaced the former ``embed_module_body_into`` call
        (which round-tripped through Slicer's module registry and pulled
        the widget out from under the shim modules) with direct
        instantiation using the imported ``OpenLIFU<X>Widget`` classes.
        """
        if self._embedding_done:
            return
        import logging
        logging.info("[OpenLIFU host] _embed_all_pages: starting embedding pass.")

        # Local imports here to avoid load-time circulars: each page module
        # imports symbols from OpenLIFULib/other pages at load time and
        # OpenLIFU.py is imported early by Slicer's module discovery pass.
        from OpenLIFUApp.pages.home_page import OpenLIFUHomeWidget
        from OpenLIFUApp.pages.data_page import OpenLIFUDataWidget
        from OpenLIFUApp.pages.session_page import OpenLIFUSessionWidget
        from OpenLIFUApp.pages.preplanning_page import OpenLIFUPrePlanningWidget
        from OpenLIFUApp.pages.transducer_localization_page import OpenLIFUTransducerLocalizationWidget
        from OpenLIFUApp.pages.sonication_planner_page import OpenLIFUSonicationPlannerWidget
        from OpenLIFUApp.pages.sonication_control_page import OpenLIFUSonicationControlWidget
        from OpenLIFUApp.pages.database_page import OpenLIFUDatabaseWidget
        from OpenLIFUApp.pages.login_page import OpenLIFULoginWidget

        widget_classes = {
            "OpenLIFUHome":                   OpenLIFUHomeWidget,
            "OpenLIFUData":                   OpenLIFUDataWidget,
            "OpenLIFUSession":                OpenLIFUSessionWidget,
            "OpenLIFUPrePlanning":            OpenLIFUPrePlanningWidget,
            "OpenLIFUTransducerLocalization": OpenLIFUTransducerLocalizationWidget,
            "OpenLIFUSonicationPlanner":      OpenLIFUSonicationPlannerWidget,
            "OpenLIFUSonicationControl":      OpenLIFUSonicationControlWidget,
            "OpenLIFUDatabase":               OpenLIFUDatabaseWidget,
            "OpenLIFULogin":                  OpenLIFULoginWidget,
        }

        # -- Stacked pages (visible timeline; reachable via show_page) --
        for page_def in _PAGE_DEFS:
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

        # -- Popup-only pages (Database, Login): instantiate + setup but
        #    leave the widget off-screen. ``_ModuleWidgetPopupDialog`` will
        #    reparent ``widget.uiWidget`` into a QDialog on demand and put
        #    it back on close. --
        for key in ("OpenLIFUDatabase", "OpenLIFULogin"):
            widget_class = widget_classes[key]
            try:
                logging.info("[OpenLIFU host] setting up popup-only %s...", key)
                widget = self._instantiate_page_widget(widget_class)
                self._page_widgets[key] = widget
            except Exception:  # noqa: BLE001
                logging.exception("[OpenLIFU host] setup failed for popup-only %s", key)

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
        been redirected — which makes ``self.resourcePath("UI/...")`` look
        under the (deleted) per-page module directory and raise
        ``Could not load UI file``.

        We work around that by supplying a real (but invisible) parent so
        the base class skips its auto-setup path, then explicitly calling
        ``widget.setup()`` after ``moduleName`` has been assigned. This is
        equivalent to the flow Slicer uses when instantiating shim modules
        (parent = a qMRMLWidget with a layout, deferred setup call).

        The dummy parent must have a layout because the base
        ``__init__`` captures ``self.layout = self.parent.layout()``, and
        every page's ``setup()`` does ``self.layout.addWidget(uiWidget)``.
        """
        parent_qwidget = qt.QWidget()
        # Give the parent a layout so `self.layout = self.parent.layout()`
        # inside ScriptedLoadableModuleWidget.__init__ is not None.
        qt.QVBoxLayout(parent_qwidget)
        parent_qwidget.setVisible(False)
        widget = widget_class(parent=parent_qwidget)
        # ``moduleName`` has now been set to "OpenLIFU" by the widget's
        # __init__, so resourcePath() will resolve under the host module's
        # Resources/ directory.
        widget.setup()
        return widget

    def _embed_page_widget_into_stack(self, widget) -> qt.QWidget:
        """Reparent ``widget.uiWidget`` into a new page of ``self.ui.pageStack``.

        Each embedded page's ``setup()`` must expose ``self.uiWidget`` (the
        qMRMLWidget loaded via :func:`slicer.util.loadUI`) so the host can
        take ownership of it. The per-page shared header
        (:class:`ModuleHeaderWidget`) is hidden, and the per-page workflow
        controls placeholder is hidden, because the host module provides a
        single shared header and footer.

        This is the direct in-host replacement for the former
        ``OpenLIFULib.module_layout.embed_module_body_into`` helper (deleted
        in Round 5c-3).
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

        # PythonQt's findChildren does not reliably filter by Python subclass
        # type (returns every QObject descendant), so use the direct reference
        # the module stored during apply_module_layout().
        module_header = getattr(widget, "module_header", None)
        if module_header is not None:
            module_header.setVisible(False)

        workflow_controls = getattr(widget, "workflow_controls", None)
        if workflow_controls is not None:
            workflow_controls.setVisible(False)
        placeholder = ui_widget.findChild(qt.QWidget, "workflowControlsPlaceholder")
        if placeholder is not None and workflow_controls is None:
            # The placeholder is empty (module never injected controls). Hide
            # it so it does not occupy space in the host page.
            placeholder.setVisible(False)

        # The uiWidget's top-level visibility flag was off (its Widget was
        # never made the active Slicer module). Show it once now;
        # QStackedWidget handles visibility across subsequent page swaps.
        ui_widget.show()

        return page

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

    def _get_embedded_widget(self, module_name: str):
        return self._page_widgets.get(module_name)

    def get_page_widget(self, module_name: str):
        """Public accessor for a page's ``ScriptedLoadableModuleWidget``.

        Replacement for the pre-refactor ``slicer.modules.OpenLIFU<X>Widget``
        and ``slicer.util.getModuleWidget("OpenLIFU<X>")`` call sites. As
        of Round 5c-3 this returns the host-owned instance from
        ``self._page_widgets`` (the shim modules are gone).
        """
        return self._page_widgets.get(module_name)

    # ------------------------------------------------------------------
    # Timeline footer
    # ------------------------------------------------------------------

    def _build_timeline_footer(self) -> None:
        layout: qt.QHBoxLayout = self.ui.hostTimelineContainer.layout()
        self._timeline_widget = _TimelineWidget(self.ui.hostTimelineContainer)
        items = [(p.key, self._pages[p.key].label) for p in _PAGE_DEFS if p.on_timeline]
        self._timeline_widget.setItems(items)
        self._timeline_widget.setOnClick(self._onTimelineClicked)
        layout.addWidget(self._timeline_widget, 1)

    def _onTimelineClicked(self, key: str) -> None:
        page = self._pages.get(key)
        if page is None:
            return
        self.show_page(key)

    def _refresh_timeline_state(self) -> None:
        """Push the current visited / reachable / current state into the
        custom-painted timeline widget, and update the Next button + status
        label.

        "Reachable" mirrors :py:meth:`Workflow.furthest_module_to_which_can_proceed`:
        a page is reachable iff every workflow page strictly before it has
        ``can_proceed=True``. The current page also counts as reachable. The
        visited set is monotonic across the host lifetime, updated in
        :py:meth:`show_page`.
        """
        workflow = self._get_workflow()
        if workflow is None:
            return

        timeline_keys = [p.key for p in _PAGE_DEFS if p.on_timeline]

        # Furthest reachable index.
        reachable_index = 0  # first workflow page is always reachable
        for i, k in enumerate(timeline_keys):
            controls = workflow.workflow_controls.get(k)
            if controls is not None and controls.can_proceed:
                reachable_index = max(reachable_index, i + 1)
            else:
                break
        reachable_keys = {timeline_keys[i] for i in range(min(reachable_index + 1, len(timeline_keys)))}
        # Visited keys are always reachable (the user got there somehow).
        reachable_keys |= self._visited_timeline_keys

        current_key = self._current_page_key if self._current_page_key in timeline_keys else None

        if self._timeline_widget is not None:
            self._timeline_widget.setState(
                visited=self._visited_timeline_keys,
                reachable=reachable_keys,
                current_key=current_key,
            )

        # Next button: enabled when the current page has can_proceed and there
        # is a next page to advance to. Hidden when not on a timeline page.
        next_button = self.ui.hostNextButton
        current_index = timeline_keys.index(current_key) if current_key is not None else -1
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
            next_button.setText(f"Next: {next_label} \u25b6" if next_label else "Next \u25b6")
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
        #   * Home:            footer fully hidden
        #   * Data Manager:    footer visible with only the Back-to-Home button
        #   * Timeline page:   footer visible with timeline + Next + status
        on_data = self._current_page_key == "OpenLIFUData"
        on_timeline_page = current_key is not None
        footer_visible = on_timeline_page or on_data
        self.ui.hostFooterContainer.setVisible(footer_visible)
        self.ui.hostFooterRule.setVisible(footer_visible)
        self.ui.hostBackToHomeButton.setVisible(on_data)
        self.ui.hostTimelineContainer.setVisible(on_timeline_page)
        self.ui.hostStatusLabel.setVisible(on_timeline_page)
        if on_data:
            next_button.setVisible(False)

    def _tooltip_for_timeline_page(self, key: str, enabled: bool, current: bool) -> str:
        page = self._pages.get(key)
        if page is None:
            return ""
        if current:
            return f"You are on {page.label}."
        if not enabled:
            return f"Complete the earlier step to unlock {page.label}."
        return f"Go to {page.label}."

    # ------------------------------------------------------------------
    # Save / Exit
    # ------------------------------------------------------------------

    @display_errors
    def onSaveClicked(self, checked: bool = False) -> None:
        data_logic: "OpenLIFUDataLogic" = self.logic.data_logic
        if data_logic.getParameterNode().loaded_session is None:
            slicer.util.errorDisplay("There is no loaded session.")
            return
        data_logic.save_session()

    @display_errors
    def onNextClicked(self, checked: bool = False) -> None:
        timeline_keys = [p.key for p in _PAGE_DEFS if p.on_timeline]
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
        data_logic: "OpenLIFUDataLogic" = self.logic.data_logic
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

    @display_errors
    def onBackToHomeClicked(self, checked: bool = False) -> None:
        """Return to the Home page from the Data Manager, prompting to save
        or discard any loaded session first."""
        data_logic: "OpenLIFUDataLogic" = self.logic.data_logic
        if data_logic.getParameterNode().loaded_session is not None:
            choice = confirm_exit_session_dialog()
            if choice == "cancel":
                return
            if choice == "save":
                data_logic.save_session()
            data_logic.clear_session(clean_up_scene=True)
        self.show_page("OpenLIFUHome")

    def _refresh_save_exit_state(self) -> None:
        # IMPORTANT: use ``self.logic.getParameterNode()`` here, NOT
        # ``self.logic.data_logic.getParameterNode()``. The Data logic's
        # ``getParameterNode`` delegates through
        # ``slicer.util.getModuleLogic('OpenLIFU').getParameterNode()``,
        # which forces Slicer to lazily construct its own OpenLIFULogic
        # instance (separate from ``self.logic`` above) the first time
        # it's called. That triggers every page-logic ``__init__``
        # (including the expensive ``OpenLIFUSonicationControlLogic`` USB /
        # monitor setup) a second time and, worse, can recurse into a
        # third construction during startup before Slicer's cache is set.
        # ``self.logic`` is the same OpenLIFULogic that Slicer will
        # eventually cache, so calling its ``getParameterNode`` directly
        # short-circuits the whole re-entrancy.
        try:
            data_pn = self.logic.getParameterNode()
            has_session = data_pn.loaded_session is not None
        except Exception:  # noqa: BLE001
            has_session = False
        self.ui.hostSaveButton.setEnabled(has_session)
        self.ui.hostExitButton.setEnabled(has_session)
        icon_color = icon_color_neutral() if has_session else icon_color_dim()
        self.ui.hostSaveButton.setIcon(tinted_icon("save.png", icon_color))
        self.ui.hostExitButton.setIcon(tinted_icon("exit.png", icon_color))
        self.ui.hostSaveButton.setToolTip(
            "Save the active session." if has_session else "No loaded session to save."
        )
        self.ui.hostExitButton.setToolTip(
            "Exit the active session." if has_session else "No loaded session to exit."
        )

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
        """Return the host's Workflow instance. Post 5c-1 this lives on
        ``OpenLIFULogic`` directly (was formerly owned by ``OpenLIFUHomeLogic``)."""
        try:
            return self.logic.workflow
        except Exception:  # noqa: BLE001
            return None


# ---------------------------------------------------------------------------
# Logic
# ---------------------------------------------------------------------------

class OpenLIFULogic(ScriptedLoadableModuleLogic):
    """Host module's logic class.

    Owns the guided-workflow state (formerly on ``OpenLIFUHomeLogic``), the
    host-level ``OpenLIFUAppState`` parameter node, and one instance of each
    page's ``*Logic`` class. Round 5c-3 replaced the former ``slicer.util
    .getModuleLogic("OpenLIFU<X>")`` delegation with direct-attribute
    ownership so the shim modules can be deleted.
    """

    def __init__(self) -> None:
        ScriptedLoadableModuleLogic.__init__(self)
        # Workflow was formerly owned by OpenLIFUHomeLogic; folded in here
        # in Round 5c-1. OpenLIFUHomeLogic.workflow now delegates to this.
        self.workflow = Workflow()
        self._app_state_cache: Optional[OpenLIFUAppState] = None
        self._app_state_cache_node = None

        # Owned page-logic instances. Round 5c-3 replaced the former
        # ``slicer.util.getModuleLogic("OpenLIFU<X>")`` property accessors
        # with direct-attribute ownership; the shim modules have been
        # deleted so the host is the single source of truth for logic
        # instances. Local imports here avoid circulars: each page module
        # imports symbols from ``OpenLIFULib``/other pages at load time and
        # ``OpenLIFU.py`` is imported before ``OpenLIFUApp`` is on sys.path.
        from OpenLIFUApp.pages.home_page import OpenLIFUHomeLogic
        from OpenLIFUApp.pages.data_page import OpenLIFUDataLogic
        from OpenLIFUApp.pages.session_page import OpenLIFUSessionLogic
        from OpenLIFUApp.pages.preplanning_page import OpenLIFUPrePlanningLogic
        from OpenLIFUApp.pages.transducer_localization_page import OpenLIFUTransducerLocalizationLogic
        from OpenLIFUApp.pages.sonication_planner_page import OpenLIFUSonicationPlannerLogic
        from OpenLIFUApp.pages.sonication_control_page import OpenLIFUSonicationControlLogic
        from OpenLIFUApp.pages.database_page import OpenLIFUDatabaseLogic
        from OpenLIFUApp.pages.login_page import OpenLIFULoginLogic

        self.home_logic = OpenLIFUHomeLogic()
        self.data_logic = OpenLIFUDataLogic()
        self.session_logic = OpenLIFUSessionLogic()
        self.preplanning_logic = OpenLIFUPrePlanningLogic()
        self.transducer_localization_logic = OpenLIFUTransducerLocalizationLogic()
        self.sonication_planner_logic = OpenLIFUSonicationPlannerLogic()
        self.sonication_control_logic = OpenLIFUSonicationControlLogic()
        self.database_logic = OpenLIFUDatabaseLogic()
        self.login_logic = OpenLIFULoginLogic()

    def getParameterNode(self):
        """Return the OpenLIFU app-state wrapper (cached).

        Construction of :class:`OpenLIFUAppState` runs
        ``parameterNodeWrapper._initMethod``, which writes defaults for any
        unset fields. Each write fires ``ModifiedEvent`` on the underlying
        MRML node; observers (page ``onDataParameterNodeModified`` handlers,
        the ``AppStateSignals.dataChanged`` re-emit lambda) can call
        ``get_app_state()`` again before the wrapper is fully constructed.
        We batch the default-writes inside ``NodeModify`` so a single
        ``ModifiedEvent`` fires *after* construction finishes and after the
        cache is populated — observers then get the fully-initialised
        wrapper and never re-enter ``OpenLIFUAppState(...)``.
        """
        mrml_node = super().getParameterNode()
        if self._app_state_cache is None or self._app_state_cache_node is not mrml_node:
            with slicer.util.NodeModify(mrml_node):
                wrapper = OpenLIFUAppState(mrml_node)
            self._app_state_cache = wrapper
            self._app_state_cache_node = mrml_node
        return self._app_state_cache

    # ------------------------------------------------------------------
    # Guided-workflow entry points (formerly on OpenLIFUHomeLogic)
    # ------------------------------------------------------------------

    def start_guided_mode(self) -> None:
        set_guided_mode_state(True)
        self.workflow_go_to_start()

    def workflow_jump_ahead(self) -> None:
        """Jump ahead in the guided workflow to the furthest step for which
        ``can_proceed`` is True."""
        slicer.util.selectModule(self.workflow.furthest_module_to_which_can_proceed())

    def workflow_go_to_start(self) -> None:
        """Go to the starting module of the workflow."""
        slicer.util.selectModule(self.workflow.starting_module())

    # ------------------------------------------------------------------
    # Sub-logic access
    #
    # As of Round 5c-3 all page logic instances are owned directly as
    # attributes on this class (``self.home_logic``, ``self.data_logic``,
    # ...) — see ``__init__``. The former ``getModuleLogic("OpenLIFU<X>")``
    # delegation was deleted along with the shim modules.
    # ------------------------------------------------------------------


# ---------------------------------------------------------------------------
# Test
# ---------------------------------------------------------------------------

class OpenLIFUTest(ScriptedLoadableModuleTest):
    """Full end-to-end workflow integration test.

    Round 5c-3 Commit D folded the former ``OpenLIFUHomeTest`` orchestration
    into this class (the ``OpenLIFUHome`` shim module and its ``py_OpenLIFUHome``
    ctest were deleted). CMake wires this to the ``py_OpenLIFU`` ctest with
    the DVC ``GDRIVE_CREDENTIALS_DATA`` / ``DVC_REPO_DIR`` environment.
    """

    def setUp(self) -> None:
        """Reset state before each test — a scene clear is typically enough."""
        slicer.mrmlScene.Clear()

    def _ensure_dvc_gdrive_support(self) -> None:
        import importlib.util

        # Check if dvc is installed with gdrive support
        dvc_installed = importlib.util.find_spec("dvc") is not None
        gdrive_installed = importlib.util.find_spec("pydrive2") is not None

        if not dvc_installed or not gdrive_installed:
            slicer.util.pip_install("dvc[gdrive]")

    def get_test_database(self) -> str:
        """Download the test database from Google Drive via DVC.

        Setup Requirements:
            - DVC with Google Drive support must be installed in the Slicer environment.
            - The path to a Service Account JSON key must be provided via the
              DVC_GDRIVE_KEY_PATH CMake variable during configuration.

        Authentication Flow:
            At configuration time, CMake reads the key file's content into the
            GDRIVE_CREDENTIALS_DATA environment variable. This allows DVC to
            authenticate headlessly during runtime without requiring a manual
            browser login.
        """
        self._ensure_dvc_gdrive_support()
        import os
        from pathlib import Path
        from dvc.repo import Repo

        dvc_repo_path = os.environ.get("DVC_REPO_DIR")
        if not dvc_repo_path:
            raise EnvironmentError("DVC_REPO_DIR environment variable is not set.")

        dvc_repo_path = Path(dvc_repo_path)
        dvc_file = dvc_repo_path / "db_dvc_slicertesting.dvc"
        dvc_config_file = dvc_repo_path / ".dvc" / "config"

        assert dvc_config_file.exists() and dvc_file.exists(), (
            f"DVC file not found at expected location: {dvc_file}"
        )

        try:
            creds = os.environ.get("GDRIVE_CREDENTIALS_DATA")
            if not creds:
                raise EnvironmentError(
                    "GDRIVE_CREDENTIALS_DATA environment variable is not set."
                    " DVC cannot authenticate with Google Drive."
                )
            # Point to directory containing .dvc files.
            # uninitialized=True allows working in a directory that is not a git repo.
            repo = Repo(str(dvc_repo_path), uninitialized=True)
            repo.pull(targets=[str(dvc_file)], force=True)
        except Exception as e:
            raise RuntimeError(f"An error occurred during dvc pull: {e}") from e

        return str(dvc_repo_path / "db_dvc_slicertesting")

    def runTest(self) -> None:
        """Run the full workflow integration test."""
        from OpenLIFULib import check_and_install_kwave_binaries

        ensure_python_requirements_for_module_enter()
        check_and_install_kwave_binaries()

        self.setUp()

        # Download test database using dvc
        db_path = self.get_test_database()

        self._OpenLIFU_FullTest1(db_path=db_path)

    def _OpenLIFU_FullTest1(self, db_path: str) -> None:
        from OpenLIFUApp.pages.database_page import OpenLIFUDatabaseTest
        dbt = OpenLIFUDatabaseTest()
        dbt.connect_database(database_dir=db_path)

        from OpenLIFUApp.pages.data_page import OpenLIFUDataTest
        dt = OpenLIFUDataTest()
        dt.load_subject_session()

        from OpenLIFUApp.pages.session_page import OpenLIFUSessionTest
        st = OpenLIFUSessionTest()
        st.workflow_session_dashboard()

        from OpenLIFUApp.pages.preplanning_page import OpenLIFUPrePlanningTest
        pt = OpenLIFUPrePlanningTest()
        pt._workflow_virtual_fit()

        from OpenLIFUApp.pages.transducer_localization_page import OpenLIFUTransducerLocalizationTest
        tlt = OpenLIFUTransducerLocalizationTest()
        tlt._workflow_localization()

        from OpenLIFUApp.pages.sonication_planner_page import OpenLIFUSonicationPlannerTest
        spt = OpenLIFUSonicationPlannerTest()
        spt._workflow_planning()

        from OpenLIFUApp.pages.sonication_control_page import OpenLIFUSonicationControlTest
        sct = OpenLIFUSonicationControlTest()
        sct._workflow_sonication_control()
