"""Custom-painted timeline strip used by the OpenLIFU host module.

Renders a horizontal sequence of labelled circles joined by connector
lines, matching a standard "step progress" indicator. Owned and driven
by the host widget (``OpenLIFUApp/host/host_widget.py``).

Three pieces of state are pushed in by the host on every workflow
update:

* ``visited``: filled circle. The user has navigated to this page at
  least once during the current session; visited state is monotonic
  and never reverts.
* ``reachable``: clickable circle. Workflow gating allows the user to
  land on this page. Read from
  ``Workflow.furthest_module_to_which_can_proceed``.
* ``current_key``: the active page. Drawn with a second outer ring.

Connector ``i <-> i+1`` is drawn "completed" (thicker, coloured) iff
both endpoint keys are in ``visited``.

See ``docs/architecture.md`` section 3 (page navigation) for how the
host uses this to visualise workflow progress.
"""

from __future__ import annotations

from typing import Callable, Dict, List, Optional, Set, Tuple

import qt


class TimelineWidget(qt.QWidget):
    """Renders a sequence of labelled circles joined by connector lines.

    Owned by the host widget. Consumers push state in via
    :meth:`set_items`, :meth:`set_state`, and :meth:`set_on_click`;
    the widget repaints itself on every state change.
    """

    # Geometry constants (public per docs/coding-standards.md).
    R_FILLED = 9        # filled-circle radius
    R_HOLLOW = 7        # hollow-circle radius
    RING_GAP = 4        # gap between the filled circle and the outer "current" ring
    LINE_WIDTH_DONE = 4
    LINE_WIDTH_LOCKED = 2
    LABEL_GAP = 6       # vertical gap between circle and label
    MIN_STEP_PX = 60    # minimum horizontal spacing between circle centres

    def __init__(self, parent: Optional[qt.QWidget] = None) -> None:
        super().__init__(parent)
        self.items: List[Tuple[str, str]] = []           # (key, label)
        self.visited: Set[str] = set()
        self.reachable: Set[str] = set()
        self.current_key: Optional[str] = None
        self.on_click: Optional[Callable[[str], None]] = None
        self.setSizePolicy(qt.QSizePolicy.Expanding, qt.QSizePolicy.Preferred)
        self.setMouseTracking(True)

    # ------------------------------------------------------------------
    # Public API (Qt convention: PascalCase methods to match QWidget)
    # ------------------------------------------------------------------

    def setItems(self, items: List[Tuple[str, str]]) -> None:
        """Replace the sequence of ``(key, label)`` pairs to be drawn."""
        self.items = list(items)
        self.updateGeometry()
        self.update()

    def setState(
        self,
        *,
        visited: Optional[Set[str]] = None,
        reachable: Optional[Set[str]] = None,
        current_key: Optional[str] = None,
    ) -> None:
        """Push the three logical state axes into the widget and repaint."""
        if visited is not None:
            self.visited = set(visited)
        if reachable is not None:
            self.reachable = set(reachable)
        self.current_key = current_key
        self.update()

    def setOnClick(self, callback: Callable[[str], None]) -> None:
        """Set the callback invoked when the user clicks a reachable circle."""
        self.on_click = callback

    # ------------------------------------------------------------------
    # Qt overrides
    # ------------------------------------------------------------------

    def sizeHint(self) -> qt.QSize:
        fm = self.fontMetrics()
        h = 2 * (self.R_FILLED + self.RING_GAP + 2) + self.LABEL_GAP + fm.height() + 6
        w = max(200, self.MIN_STEP_PX * max(1, len(self.items)))
        return qt.QSize(w, h)

    def minimumSizeHint(self) -> qt.QSize:
        fm = self.fontMetrics()
        h = 2 * (self.R_FILLED + self.RING_GAP + 2) + self.LABEL_GAP + fm.height() + 6
        return qt.QSize(120, h)

    def paintEvent(self, event) -> None:  # noqa: ARG002
        if not self.items:
            return
        painter = qt.QPainter(self)
        painter.setRenderHint(qt.QPainter.Antialiasing, True)
        try:
            self.paint(painter)
        finally:
            painter.end()

    def mousePressEvent(self, event) -> None:
        if event.button() != qt.Qt.LeftButton:
            return
        key = self.hit_test(event.pos())
        if key is not None and key in self.reachable and self.on_click is not None:
            self.on_click(key)

    def mouseMoveEvent(self, event) -> None:
        key = self.hit_test(event.pos())
        if key is not None:
            if key in self.reachable:
                self.setCursor(qt.QCursor(qt.Qt.PointingHandCursor))
            else:
                self.unsetCursor()
            label = next((lbl for k, lbl in self.items if k == key), key)
            if key == self.current_key:
                tip = f"You are on {label}."
            elif key in self.reachable:
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

    def circle_centres(self) -> List[Tuple[str, str, int, int]]:
        """Return ``[(key, label, cx, cy)]`` for the current widget size."""
        n = len(self.items)
        if n == 0:
            return []
        # PythonQt exposes Qt's Q_PROPERTYs (``width``, ``height``, ``palette``,
        # etc.) as auto-resolved attributes, so calling them as methods raises
        # ``TypeError: 'int' object is not callable``.
        w = self.width
        # Reserve enough horizontal margin that the first / last labels fit.
        fm = self.fontMetrics()
        first_half = fm.width(self.items[0][1]) // 2
        last_half = fm.width(self.items[-1][1]) // 2
        edge = self.R_FILLED + self.RING_GAP + 2
        left = max(edge, first_half + 4)
        right = w - max(edge, last_half + 4)
        if n == 1 or right <= left:
            xs = [w // 2 for _ in range(n)]
        else:
            step = (right - left) / float(n - 1)
            xs = [int(round(left + i * step)) for i in range(n)]
        cy = self.R_FILLED + self.RING_GAP + 2
        return [(self.items[i][0], self.items[i][1], xs[i], cy) for i in range(n)]

    def hit_test(self, point: qt.QPoint) -> Optional[str]:
        """Return the key of the circle under ``point``, or ``None``."""
        # Generous hit radius: cover the outer ring and the label below.
        hit_r2 = (self.R_FILLED + self.RING_GAP + 6) ** 2
        for key, _label, cx, cy in self.circle_centres():
            dx = point.x() - cx
            dy = point.y() - cy
            if dx * dx + dy * dy <= hit_r2:
                return key
        return None

    # ------------------------------------------------------------------
    # Painting
    # ------------------------------------------------------------------

    def theme_colors(self) -> Dict[str, qt.QColor]:
        """Pick colours that contrast in both light and dark palettes."""
        # PythonQt exposes Qt's Q_PROPERTYs as attributes, so ``palette`` is
        # already a QPalette object -- calling it would raise TypeError.
        palette = self.palette
        window = palette.color(qt.QPalette.Window)
        is_dark = (window.red() + window.green() + window.blue()) // 3 < 128
        if is_dark:
            return {
                "locked":         qt.QColor("#b0b6bd"),
                "done":           qt.QColor("#5aa9ff"),
                "current_ring":   qt.QColor("#5aa9ff"),
                "label_done":     palette.color(qt.QPalette.WindowText),
                "label_locked":   qt.QColor("#9aa0a6"),
                "label_current":  qt.QColor("#82c1ff"),
            }
        return {
            "locked":         qt.QColor("#5f6368"),
            "done":           qt.QColor("#1976d2"),
            "current_ring":   qt.QColor("#1976d2"),
            "label_done":     palette.color(qt.QPalette.WindowText),
            "label_locked":   qt.QColor("#5f6368"),
            "label_current":  qt.QColor("#1565c0"),
        }

    def paint(self, painter: qt.QPainter) -> None:
        """Draw the strip. Called from ``paintEvent``."""
        colors = self.theme_colors()
        centres = self.circle_centres()
        if not centres:
            return

        # 1. Connector lines (drawn first so circles sit on top).
        for i in range(len(centres) - 1):
            k1, _label1, x1, y = centres[i]
            k2, _label2, x2, _y2 = centres[i + 1]
            completed = (k1 in self.visited and k2 in self.visited)
            pen = qt.QPen(colors["done"] if completed else colors["locked"])
            pen.setWidth(self.LINE_WIDTH_DONE if completed else self.LINE_WIDTH_LOCKED)
            pen.setCapStyle(qt.Qt.RoundCap)
            painter.setPen(pen)
            # Inset so the line doesn't dive into the circles.
            inset = self.R_FILLED + 1
            painter.drawLine(x1 + inset, y, x2 - inset, y)

        # 2. Circles + labels
        fm = self.fontMetrics()
        for key, label, cx, cy in centres:
            is_visited = key in self.visited
            is_current = (key == self.current_key)

            if is_visited:
                # Filled circle
                painter.setBrush(qt.QBrush(colors["done"]))
                painter.setPen(qt.QPen(colors["done"], 2))
                painter.drawEllipse(qt.QPoint(cx, cy), self.R_FILLED, self.R_FILLED)
            else:
                # Hollow circle
                painter.setBrush(qt.QBrush(qt.Qt.NoBrush))
                pen = qt.QPen(colors["locked"])
                pen.setWidth(2)
                painter.setPen(pen)
                painter.drawEllipse(qt.QPoint(cx, cy), self.R_HOLLOW, self.R_HOLLOW)

            # Outer "double-outline" ring for the current step.
            if is_current:
                ring_r = self.R_FILLED + self.RING_GAP
                pen = qt.QPen(colors["current_ring"])
                pen.setWidth(2)
                painter.setBrush(qt.QBrush(qt.Qt.NoBrush))
                painter.setPen(pen)
                painter.drawEllipse(qt.QPoint(cx, cy), ring_r, ring_r)

            # Label below circle
            if is_current:
                painter.setPen(colors["label_current"])
            elif is_visited:
                painter.setPen(colors["label_done"])
            else:
                painter.setPen(colors["label_locked"])
            tw = fm.width(label)
            text_y = cy + self.R_FILLED + self.RING_GAP + 2 + fm.ascent()
            painter.drawText(cx - tw // 2, text_y, label)
