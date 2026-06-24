# Standard library imports
from __future__ import annotations

from typing import Callable, Dict, List, Optional, Set, Tuple, TYPE_CHECKING

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
    """

    def __init__(self, key: str, label: str, on_timeline: bool) -> None:
        self.key = key
        self.label = label
        self.on_timeline = on_timeline
        self.container: Optional[qt.QWidget] = None


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
        self._current_page_key: Optional[str] = None
        self._embedding_done: bool = False
        # Timeline keys the user has navigated to at least once. Persists across
        # back-navigation so completed steps don't "unfill" when revisited.
        self._visited_timeline_keys: Set[str] = set()
        # Custom-painted timeline footer widget; created in _build_timeline_footer.
        self._timeline_widget: Optional[_TimelineWidget] = None

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

        # ---- Next (advance) ----
        self.ui.hostNextButton.clicked.connect(self.onNextClicked)

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
        # Mark the page as visited (only timeline pages count; Home is not).
        if page.on_timeline:
            self._visited_timeline_keys.add(module_name)
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
