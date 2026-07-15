"""Shared header / scrollable body / footer layout for OpenLIFU modules.

Provides:

* :class:`ModuleHeaderWidget` -- a single-row toolbar with the database / login
  / device popup buttons. Knows how to repaint itself from global state
  (``updateStatusButtons``).

* :func:`apply_module_layout` -- restructure a module's loaded UI so the header
  stays pinned at the top, the workflow-controls placeholder stays pinned at
  the bottom, and everything in between scrolls. Returns the inserted
  :class:`ModuleHeaderWidget`.

* :func:`wire_passive_module_header` -- install the observers needed to keep a
  read-only :class:`ModuleHeaderWidget` in sync with global state (Login,
  OpenLIFUSonicationControl, OpenLIFUDatabase, cloud-sync logic).

The Data module owns the *interactive* header (``read_only=False``) and wires
its own slots; every other workflow module gets a *read-only* header so the
user can see the same status indicators without being able to change them
from inside a deep workflow step.
"""
from __future__ import annotations

import logging
import os
from typing import List, Optional

import qt
import slicer
import vtk

# ---------------------------------------------------------------------------
# Icon tinting
# ---------------------------------------------------------------------------
# Header PNGs in ``OpenLIFU/Resources/Icons/`` (see
# scripts/generate_header_icons.py) ship as pure-white silhouettes with alpha.
# We composite a per-state colour through the icon at load time using
# ``QPainter.CompositionMode_SourceIn`` so a single PNG serves every visual
# state (dim / active / success / warning / danger) on both Slicer's light
# and dark themes without shipping a variant per state.
_WHITE_PIXMAP_CACHE: dict = {}
_TINTED_ICON_CACHE: dict = {}

# Status colour palette. Matched to the ``_outline_style`` outline colours
# so the icon glyph and its outline reinforce each other visually.
ICON_COLOR_SUCCESS = "#2e7d32"   # Material green 800  (connected / logged in)
ICON_COLOR_WARNING = "#f9a825"   # Material yellow 800 (partial / cloud broken)
ICON_COLOR_DANGER = "#c62828"    # Material red 800    (missing / locked out)
ICON_COLOR_INFO = "#ff84f9"      # Material pink 500   (simulated hardware)


def _icon_path(filename: str) -> Optional[str]:
    try:
        host_module_dir = os.path.dirname(slicer.util.modulePath("OpenLIFU"))
        path = os.path.join(host_module_dir, "Resources", "Icons", filename)
        return path if os.path.exists(path) else None
    except Exception:  # noqa: BLE001
        return None


def _load_white_pixmap(filename: str) -> "qt.QPixmap":
    cached = _WHITE_PIXMAP_CACHE.get(filename)
    if cached is not None:
        return cached
    path = _icon_path(filename)
    if not path:
        logging.warning("Could not locate header icon %s", filename)
        pm = qt.QPixmap()
    else:
        pm = qt.QPixmap(path)
    _WHITE_PIXMAP_CACHE[filename] = pm
    return pm


def icon_color_neutral() -> "qt.QColor":
    """Palette's normal button-text colour: light on dark themes, dark on light."""
    return slicer.app.palette().color(qt.QPalette.Active, qt.QPalette.ButtonText)


def icon_color_dim() -> "qt.QColor":
    """Palette's disabled button-text colour: muted regardless of theme."""
    return slicer.app.palette().color(qt.QPalette.Disabled, qt.QPalette.ButtonText)


def tinted_icon(filename: str, color) -> "qt.QIcon":
    """Return a cached ``qt.QIcon`` for ``filename`` re-tinted to ``color``.

    ``filename`` refers to a white silhouette PNG in the host module's
    ``Resources/Icons/`` folder. ``color`` may be a hex string, a
    ``qt.QColor``, or any value ``qt.QColor()`` accepts.
    """
    qcolor = qt.QColor(color)
    key = (filename, qcolor.name())
    cached = _TINTED_ICON_CACHE.get(key)
    if cached is not None:
        return cached
    base = _load_white_pixmap(filename)
    if base.isNull():
        icon = qt.QIcon()
    else:
        canvas = qt.QPixmap(base.size())
        canvas.fill(qt.Qt.transparent)
        painter = qt.QPainter(canvas)
        try:
            painter.drawPixmap(0, 0, base)
            painter.setCompositionMode(qt.QPainter.CompositionMode_SourceIn)
            painter.fillRect(canvas.rect(), qcolor)
        finally:
            painter.end()
        icon = qt.QIcon(canvas)
    _TINTED_ICON_CACHE[key] = icon
    return icon


def _outline_style(object_name: str, hex_color: str) -> str:
    """A 2px coloured border around a header button without overriding padding."""
    return (
        f"QPushButton#{object_name} {{ "
        f"border: 2px solid {hex_color}; "
        f"border-radius: 4px; "
        f"}}"
    )


def _strip_click_to(tooltip: str) -> str:
    """Drop any trailing 'Click to ...' / 'Click for ...' sentence from a status tooltip.

    The interactive header tooltips end with phrases like
    ``"... Click to view or change."`` or ``"... Click for details."``. On a
    read-only header those phrases are misleading because the buttons are
    disabled, so we strip them before appending the read-only note.
    """
    if not tooltip:
        return ""
    # Walk backwards for the last "Click to ..." / "Click for ..." sentence
    # (case-insensitive) and drop everything from there onward.
    lower = tooltip.lower()
    idx = max(lower.rfind("click to "), lower.rfind("click for "))
    if idx == -1:
        return tooltip.rstrip()
    return tooltip[:idx].rstrip().rstrip(".").rstrip()


class _DatabaseStatusDialog(qt.QDialog):
    """Read-only database status popup shown from non-Data pages.

    The Data page opens the full interactive ``OpenLIFUDatabase`` widget in
    its own popup; from every other page the database button instead opens
    this lightweight dialog so the user can see the database location,
    connection status, and cloud-sync status without being able to change
    them.
    """

    def __init__(self, parent: Optional[qt.QWidget] = None) -> None:
        qt.QDialog.__init__(self, parent or slicer.util.mainWindow())
        self.setWindowTitle("Database")
        self.setWindowModality(qt.Qt.ApplicationModal)
        self.setMinimumWidth(420)

        from OpenLIFULib import get_cur_db
        try:
            cur_db = get_cur_db()
        except (AttributeError, RuntimeError):
            cur_db = None
        db_path = getattr(cur_db, "path", None) if cur_db is not None else None
        db_status_text = "Database is connected." if cur_db is not None else "No database is connected."

        try:
            from OpenLIFUApp.logic.cloud_sync import getCloudSyncLogic
            cs_logic = getCloudSyncLogic()
            cs_enabled = cs_logic.is_service_enabled()
            cs_running = cs_logic.is_service_running()
            cs_logged_in = cs_logic.is_logged_in()
            cs_failed = cs_logic.did_service_fail()
        except Exception:  # noqa: BLE001
            cs_enabled = cs_running = cs_logged_in = cs_failed = False

        if not cs_enabled:
            cloud_status_text = "Cloud synchronization is disabled."
        elif cs_failed or not cs_running:
            cloud_status_text = "Cloud synchronization is enabled but the service failed to start."
        elif not cs_logged_in:
            cloud_status_text = "Cloud synchronization is enabled but not signed in."
        else:
            cloud_status_text = "Cloud synchronization is enabled."

        layout = qt.QVBoxLayout(self)
        layout.setContentsMargins(12, 12, 12, 12)
        layout.setSpacing(8)

        dir_label = qt.QLabel("Database directory:")
        layout.addWidget(dir_label)
        dir_field = qt.QLineEdit(str(db_path) if db_path else "(no database connected)")
        dir_field.setReadOnly(True)
        dir_field.setCursorPosition(0)
        layout.addWidget(dir_field)

        db_status_label = qt.QLabel(db_status_text)
        layout.addWidget(db_status_label)

        cloud_status_label = qt.QLabel(cloud_status_text)
        layout.addWidget(cloud_status_label)

        hint_label = qt.QLabel("To change the database, go to the Data Manager page.")
        hint_label.setStyleSheet("color: gray;")
        layout.addWidget(hint_label)

        button_row = qt.QHBoxLayout()
        button_row.addStretch(1)
        done_button = qt.QPushButton("Done")
        done_button.clicked.connect(lambda _checked=False: self.accept())
        button_row.addWidget(done_button)
        layout.addLayout(button_row)


class ModuleHeaderWidget(qt.QWidget):
    """Single-row status header shared across OpenLIFU module pages.

    Children (also exposed as Python attributes by the same name):

    * ``databasePopupButton``   - QPushButton, opens DB popup (Data) / status (others)
    * ``loginPopupButton``      - QPushButton, opens Account popup (Data) / status (others)
    * ``devicePopupButton``     - QPushButton, opens Device popup (Data) / status (others)

    When ``read_only=True`` (the default for non-Data modules), every control
    is ``setEnabled(False)``: the user can see live status but cannot change
    global state from within a workflow step. The Data page passes
    ``read_only=False`` and connects its own click slots.
    """

    def __init__(
        self,
        *,
        read_only: bool,
        keep_login_button_active: bool = False,
        parent: Optional[qt.QWidget] = None,
    ) -> None:
        super().__init__(parent)
        self.read_only = read_only
        self._keep_login_button_active = bool(keep_login_button_active)
        # Cache of the previous (tx_conn, hv_conn) tuple so we only log device
        # connection state changes (matches the Data module's behaviour).
        self._last_device_conn_state = "unset"
        self._build_ui()
        if read_only:
            self._apply_read_only()

    # ------------------------------------------------------------------
    # construction
    # ------------------------------------------------------------------

    def _build_ui(self) -> None:
        layout = qt.QHBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(4)

        def _make_button(name: str, text: str, tooltip: str) -> qt.QPushButton:
            btn = qt.QPushButton(text, self)
            btn.setObjectName(name)
            btn.setToolTip(tooltip)
            btn.setMinimumSize(qt.QSize(40, 32))
            btn.setMaximumSize(qt.QSize(48, 32))
            return btn

        # Version-info button: OpenLIFU logo on the far left, click for a
        # popup listing component versions. Always interactive, including
        # in read-only headers.
        self.versionInfoButton = _make_button(
            "versionInfoButton",
            "",
            "Show OpenLIFU component versions",
        )
        try:
            openlifu_module_dir = os.path.dirname(
                slicer.util.modulePath("OpenLIFU")
            )
            icon_path = os.path.join(openlifu_module_dir, "Resources", "Icons", "OpenLIFU.png")
            if os.path.exists(icon_path):
                self.versionInfoButton.setIcon(qt.QIcon(icon_path))
                self.versionInfoButton.setIconSize(qt.QSize(24, 24))
        except Exception as e:  # noqa: BLE001
            logging.warning("Could not load version-info button icon: %s", e)
        self.versionInfoButton.clicked.connect(self._open_version_info_popup)
        layout.addWidget(self.versionInfoButton)

        self.databasePopupButton = _make_button(
            "databasePopupButton",
            "",
            "Open the database configuration panel",
        )
        self.databasePopupButton.setIcon(tinted_icon("db.png", icon_color_neutral()))
        self.databasePopupButton.setIconSize(qt.QSize(20, 20))
        layout.addWidget(self.databasePopupButton)

        self.loginPopupButton = _make_button(
            "loginPopupButton",
            "",
            "Open the user login / account panel",
        )
        self.loginPopupButton.setIcon(tinted_icon("user.png", icon_color_neutral()))
        self.loginPopupButton.setIconSize(qt.QSize(20, 20))
        layout.addWidget(self.loginPopupButton)

        self.devicePopupButton = _make_button(
            "devicePopupButton",
            "",
            "Hardware device connection status",
        )
        self.devicePopupButton.setIcon(tinted_icon("device.png", icon_color_neutral()))
        self.devicePopupButton.setIconSize(qt.QSize(20, 20))
        layout.addWidget(self.devicePopupButton)

        layout.addStretch(1)

    def _apply_read_only(self) -> None:
        # The login button stays disabled on workflow pages unless the host
        # opted in to keep it active (e.g. Home). The database and device
        # buttons stay live everywhere so the popup info is always reachable;
        # editability inside the database popup is enforced by the caller.
        if not self._keep_login_button_active:
            self.loginPopupButton.setEnabled(False)

        # Wire the device-info button to the Data module's popup handler so
        # the same dialog is reachable from every workflow page.
        self.devicePopupButton.clicked.connect(self._open_device_popup_via_data_module)

        # Database popup is always reachable; the popup itself decides
        # whether the mutating controls are interactive based on context.
        self.databasePopupButton.clicked.connect(self._open_database_popup_via_data_module)

        # Modules that opt in (e.g. Home) keep the sign-in icon live and
        # delegate to Data's existing login popup handler.
        if self._keep_login_button_active:
            self.loginPopupButton.clicked.connect(self._open_login_popup_via_data_module)

    def _open_device_popup_via_data_module(self, _checked: bool = False) -> None:
        """Delegate to ``OpenLIFUDataWidget.onOpenDevicePopup`` (read-only header)."""
        try:
            data_widget = slicer.util.getModuleWidget("OpenLIFU").get_page_widget("OpenLIFUData")
        except Exception as e:  # noqa: BLE001
            logging.warning("Could not resolve OpenLIFUData widget: %s", e)
            return
        handler = getattr(data_widget, "onOpenDevicePopup", None)
        if handler is None:
            logging.warning(
                "OpenLIFUData widget has no onOpenDevicePopup handler."
            )
            return
        handler()

    def _open_login_popup_via_data_module(self, _checked: bool = False) -> None:
        """Delegate to ``OpenLIFUDataWidget.onOpenLoginPopup`` (read-only header opt-in)."""
        try:
            data_widget = slicer.util.getModuleWidget("OpenLIFU").get_page_widget("OpenLIFUData")
        except Exception as e:  # noqa: BLE001
            logging.warning("Could not resolve OpenLIFUData widget: %s", e)
            return
        handler = getattr(data_widget, "onOpenLoginPopup", None)
        if handler is None:
            logging.warning(
                "OpenLIFUData widget has no onOpenLoginPopup handler."
            )
            return
        handler()

    def _open_database_popup_via_data_module(self, _checked: bool = False) -> None:
        """Open the database popup.

        From the Data Manager page (whether the standalone Data module or the
        embedded Data page inside the OpenLIFU host) the user expects the
        full interactive popup; from any other page they expect the
        read-only status dialog. We detect "currently on Data" by asking the
        OpenLIFU host for its active page key (the host owns the only
        read-only header that can sit on top of the Data page); when no host
        is present, fall back to the read-only dialog.
        """
        if self._host_active_page_is_data():
            try:
                data_widget = slicer.util.getModuleWidget("OpenLIFU").get_page_widget("OpenLIFUData")
            except Exception as e:  # noqa: BLE001
                logging.warning("Could not resolve OpenLIFUData widget: %s", e)
                data_widget = None
            handler = getattr(data_widget, "onOpenDatabasePopup", None) if data_widget else None
            if handler is not None:
                handler()
                return
        dialog = _DatabaseStatusDialog(parent=slicer.util.mainWindow())
        dialog.exec_()

    @staticmethod
    def _host_active_page_is_data() -> bool:
        """True iff the OpenLIFU host module exists and its current page is OpenLIFUData."""
        try:
            host_widget = slicer.util.getModuleWidget("OpenLIFU")
        except Exception:  # noqa: BLE001
            return False
        if host_widget is None:
            return False
        return getattr(host_widget, "_current_page_key", None) == "OpenLIFUData"

    def _open_version_info_popup(self, _checked: bool = False) -> None:
        """Show a small dialog listing the versions of every OpenLIFU
        component the application can identify."""
        import html
        from OpenLIFULib.dependency_utils import get_openlifu_versions
        versions = get_openlifu_versions()
        rows = "".join(
            f"<tr>"
            f"<td style='padding-right:12px; white-space:nowrap;'><b>{html.escape(name)}</b></td>"
            f"<td style='font-family:monospace;'>{html.escape(value)}</td>"
            f"</tr>"
            for name, value in versions.items()
        )
        text = (
            "<h3>OpenLIFU component versions</h3>"
            f"<table cellspacing='4'>{rows}</table>"
        )
        box = qt.QMessageBox(self)
        box.setWindowTitle("OpenLIFU versions")
        box.setTextFormat(qt.Qt.RichText)
        box.setText(text)
        box.setStandardButtons(qt.QMessageBox.Ok)
        box.exec_()

    # ------------------------------------------------------------------
    # update methods (display-only; safe to call any time)
    # ------------------------------------------------------------------

    def updateNavigationModeComboBox(self) -> None:
        """Deprecated no-op (the Navigation dropdown was removed)."""
        return None

    def updatePermissionsModeComboBox(self) -> None:
        """Deprecated no-op (the Permissions dropdown was removed)."""
        return None

    def updateStatusButtons(self) -> None:
        """Reflect database / login / device / cloud-sync state in the icon buttons.

        Safe to call before any of the sibling modules are instantiated.
        """
        from OpenLIFULib import get_cur_db
        from OpenLIFULib.user_account_mode_util import (
            get_current_user,
            get_user_account_mode_state,
        )

        # --- Database / cloud sync ---
        try:
            cur_db = get_cur_db()
        except (AttributeError, RuntimeError):
            cur_db = None
        db_connected = cur_db is not None

        try:
            from OpenLIFUApp.logic.cloud_sync import getCloudSyncLogic
            cs_logic = getCloudSyncLogic()
            cs_enabled = cs_logic.is_service_enabled()
            cs_running = cs_logic.is_service_running()
            cs_logged_in = cs_logic.is_logged_in()
            cs_failed = cs_logic.did_service_fail()
        except Exception:  # noqa: BLE001
            cs_enabled = cs_running = cs_logged_in = cs_failed = False

        cloud_ok = cs_enabled and cs_running and cs_logged_in and not cs_failed
        cloud_broken = cs_enabled and not cloud_ok

        # Tint the folder glyph to match the outline colour so the icon
        # itself reinforces the status signal.
        if db_connected:
            db_color = ICON_COLOR_WARNING if cloud_broken else ICON_COLOR_SUCCESS
            if cs_enabled and cloud_ok:
                db_icon_name = "db-cloud.png"
            elif cloud_broken:
                db_icon_name = "db-cloud-broken.png"
            else:
                db_icon_name = "db.png"
        else:
            db_color = ICON_COLOR_DANGER
            db_icon_name = "db.png"
        self.databasePopupButton.setIcon(tinted_icon(db_icon_name, db_color))
        self.databasePopupButton.setStyleSheet(
            _outline_style("databasePopupButton", db_color)
        )

        if db_connected:
            db_path = getattr(cur_db, "path", None) or "(unknown location)"
            tip = f"Database is connected at:\n{db_path}"
            if cs_enabled:
                if cloud_ok:
                    tip += "\n\nCloud sync: running"
                elif not cs_logged_in:
                    tip += "\n\nCloud sync: enabled but not logged in"
                elif cs_failed or not cs_running:
                    tip += "\n\nCloud sync: service failed to start"
            tip += "\n\nClick to view or change."
            self.databasePopupButton.setToolTip(tip)
        else:
            self.databasePopupButton.setToolTip(
                "No database is connected. Click to choose a database directory."
            )

        # --- Login ---
        try:
            cur_user = get_current_user()
        except (AttributeError, RuntimeError):
            cur_user = None
        try:
            uam = bool(get_user_account_mode_state())
        except (AttributeError, RuntimeError):
            uam = False
        user_id = getattr(cur_user, "id", None)
        is_real_user = (
            cur_user is not None
            and user_id not in (None, "anonymous", "default_admin")
        )
        user_roles = list(getattr(cur_user, "roles", None) or [])
        is_admin = is_real_user and "admin" in user_roles
        # Crown badge for admins, plain user silhouette otherwise. The crown
        # makes admin sessions visually distinct from operator sessions
        # without needing a separate "admin warning" banner. Icon tint
        # reinforces the outline: green when logged in, red when the user
        # is expected to log in (UAM=on) but hasn't, neutral otherwise.
        icon_file = "user-admin.png" if is_admin else "user.png"
        if is_real_user:
            login_color = ICON_COLOR_SUCCESS
            login_outline: Optional[str] = ICON_COLOR_SUCCESS
            who = getattr(cur_user, 'name', '') or user_id
            login_tooltip = f"Logged in as {who}. Click for details."
        elif uam:
            login_color = ICON_COLOR_DANGER
            login_outline = ICON_COLOR_DANGER
            login_tooltip = "Not Logged in. Click for details."
        else:
            login_color = icon_color_neutral()
            login_outline = None
            login_tooltip = "Not Logged in. Click for details."
        self.loginPopupButton.setIcon(tinted_icon(icon_file, login_color))
        self.loginPopupButton.setStyleSheet(
            _outline_style("loginPopupButton", login_outline) if login_outline else ""
        )
        self.loginPopupButton.setToolTip(login_tooltip)
        # Login requires a database to be useful. This applies even in
        # the read-only header for the opt-in modules (e.g. Home) that keep
        # the login button live; for fully read-only headers the button is
        # already disabled so we just leave the tooltip accurate.
        if not self.read_only or self._keep_login_button_active:
            self.loginPopupButton.setEnabled(db_connected)
        if not db_connected:
            self.loginPopupButton.setToolTip(
                "Connect a database first; account features look up users from the database."
            )

        # --- Device ---
        try:
            sc_logic = slicer.util.getModuleLogic("OpenLIFU").sonication_control_logic
            iface = getattr(sc_logic, "cur_lifu_interface", None)
            tx_conn, hv_conn = (
                iface.is_device_connected() if iface is not None else (False, False)
            )
        except (AttributeError, RuntimeError):
            sc_logic = None
            iface = None
            tx_conn, hv_conn = False, False
        is_simulated = bool(getattr(iface, "is_simulated", False))
        in_use_pid = getattr(sc_logic, "lifu_hw_in_use_pid", None) if sc_logic else None

        # The Data module owns the device-state log line (it has its own
        # ``_last_device_conn_state`` cache and emits the
        # ``[LIFUInterface]`` info message on transitions). We deliberately
        # do NOT log here, otherwise every module page re-emits the same
        # transition message.
        # Pick a status colour once and apply to both outline and icon
        # tint. The "no device" case gets no outline (previously used
        # ``#000000`` which is invisible on Slicer's dark theme) and a
        # neutral tint that reads on both themes.
        if iface is None and in_use_pid is not None:
            device_color: Optional[str] = ICON_COLOR_DANGER
            device_tooltip = (
                f"LIFU hardware is in use by another process (PID {in_use_pid}). "
                "Click for details and to retry connecting."
            )
        elif is_simulated:
            device_color = ICON_COLOR_INFO
            device_tooltip = (
                "Simulated hardware device connected (no real device). "
                "Click for details."
            )
        elif tx_conn and hv_conn:
            device_color = ICON_COLOR_SUCCESS
            device_tooltip = (
                "Hardware device fully connected (TX + HV). Click for details."
            )
        elif tx_conn or hv_conn:
            device_color = ICON_COLOR_WARNING
            half = "TX only" if tx_conn else "HV only"
            device_tooltip = (
                f"Hardware partially connected ({half}). Click for details."
            )
        else:
            device_color = None
            device_tooltip = "No hardware device connected. Click for details."

        if device_color is not None:
            self.devicePopupButton.setStyleSheet(
                _outline_style("devicePopupButton", device_color)
            )
            self.devicePopupButton.setIcon(tinted_icon("device.png", device_color))
        else:
            self.devicePopupButton.setStyleSheet("")
            self.devicePopupButton.setIcon(
                tinted_icon("device.png", icon_color_neutral())
            )
        self.devicePopupButton.setToolTip(device_tooltip)

        # In read-only mode (every module page except OpenLIFU Data), the
        # login button is disabled (unless the host opted to keep it active).
        # The database and device buttons stay live everywhere: they open
        # informational popups whose interactive controls are enforced by
        # the popup itself. Tooltip rewriting only applies to the disabled
        # login button.
        if self.read_only and not self._keep_login_button_active:
            login_tip = self.loginPopupButton.toolTip
            self.loginPopupButton.setToolTip(
                _strip_click_to(login_tip) + "\n\nChange in the Data module."
            )

    def refresh_all(self) -> None:
        """Repaint every status indicator from current global state."""
        self.updateStatusButtons()


# ----------------------------------------------------------------------
# layout restructuring
# ----------------------------------------------------------------------

# Names of placeholder widgets recognised at the boundaries of the body.
_HEADER_BOUNDARY_NAMES = ("userAccountBannerPlaceholder",)
_FOOTER_BOUNDARY_NAMES = ("workflowControlsPlaceholder",)


def apply_module_layout(
    top_widget: qt.QWidget,
    *,
    ui_namespace: object,
    header_read_only: bool,
    keep_login_button_active: bool = False,
) -> ModuleHeaderWidget:
    """Restructure ``top_widget``'s top-level QVBoxLayout into header + body + footer.

    Expected pre-state: ``top_widget`` is the qMRMLWidget loaded from a module's
    ``.ui`` file. Its ``layout()`` is a ``QVBoxLayout`` containing, in order:

    * ``userAccountBannerPlaceholder`` (kept at the top, untouched)
    * any number of body widgets / spacers
    * ``workflowControlsPlaceholder`` (kept at the bottom, untouched)

    On return, the layout contains:

    * a fresh :class:`ModuleHeaderWidget` (replaces the legacy
      user-account banner placeholder, which is hidden and detached)
    * a ``QScrollArea`` (named ``bodyScrollArea``) wrapping the original body
      widgets
    * the workflow-controls placeholder

    The header widget's children are exposed on ``ui_namespace`` (typically
    ``self.ui``) under the same names they have inside ``ModuleHeaderWidget``,
    so existing ``self.ui.databasePopupButton`` / ``self.ui.loginPopupButton``
    / ``self.ui.devicePopupButton`` references continue to work without
    modification.

    Returns the new :class:`ModuleHeaderWidget`.
    """
    layout = top_widget.layout()
    if not isinstance(layout, qt.QVBoxLayout):
        raise RuntimeError(
            f"apply_module_layout: expected a QVBoxLayout on {top_widget.objectName()!r}, "
            f"got {type(layout).__name__}"
        )

    # Snapshot existing items, then drain the layout.
    items: List[qt.QLayoutItem] = []
    while layout.count() > 0:
        items.append(layout.takeAt(0))

    footer_item: Optional[qt.QLayoutItem] = None
    body_items: List[qt.QLayoutItem] = []
    for it in items:
        w = it.widget()
        name = w.objectName if w is not None else ""
        if name in _HEADER_BOUNDARY_NAMES:
            # The legacy ``userAccountBannerPlaceholder`` (and the
            # UserAccountBanner widget that some modules historically
            # replaced it with) has been retired in favour of the shared
            # header. Hide and orphan it so any lingering reference does
            # not show in the page or take up layout space.
            if w is not None:
                w.hide()
                w.setParent(None)  # detach from top_widget
        elif name in _FOOTER_BOUNDARY_NAMES and footer_item is None:
            footer_item = it
        else:
            body_items.append(it)

    # 1) Insert the shared header at the top.
    header = ModuleHeaderWidget(
        read_only=header_read_only,
        keep_login_button_active=keep_login_button_active,
        parent=top_widget,
    )
    layout.addWidget(header)

    # 2) Wrap the body in a vertical-only scroll area.
    scroll = qt.QScrollArea(top_widget)
    scroll.setObjectName("bodyScrollArea")
    scroll.setFrameShape(qt.QFrame.NoFrame)
    scroll.setHorizontalScrollBarPolicy(qt.Qt.ScrollBarAlwaysOff)
    scroll.setWidgetResizable(True)
    body = qt.QWidget()
    body.setObjectName("bodyContents")
    body_layout = qt.QVBoxLayout(body)
    body_layout.setContentsMargins(0, 0, 0, 0)
    for it in body_items:
        w = it.widget()
        if w is not None:
            body_layout.addWidget(w)
        else:
            # spacer / layout item -- transplant the QLayoutItem itself so we
            # preserve its sizeHint / orientation without rebuilding it.
            body_layout.addItem(it)
    scroll.setWidget(body)
    layout.addWidget(scroll, 1)  # body gets the stretch

    # 3) Workflow controls stay pinned at the bottom.
    if footer_item is not None and footer_item.widget() is not None:
        layout.addWidget(footer_item.widget())

    # 5) Expose the header children on ui_namespace so existing self.ui.X
    #    references keep working.
    if ui_namespace is not None:
        for attr in (
            "databasePopupButton",
            "loginPopupButton",
            "devicePopupButton",
        ):
            setattr(ui_namespace, attr, getattr(header, attr))

    # 6) Schedule an initial paint after the current setup() finishes so
    #    accessing other modules' logic does not re-enter their setup.
    qt.QTimer.singleShot(0, header.refresh_all)

    return header


# ----------------------------------------------------------------------
# passive observer wiring (read-only header)
# ----------------------------------------------------------------------

def wire_passive_module_header(widget_owner, header: ModuleHeaderWidget) -> None:
    """Install observers so a *read-only* header stays in sync with global state.

    Every observer registration is deferred one event-loop tick. This
    matters because ``setup()`` for these modules can be triggered from
    inside other OpenLIFU page setups. If we touched
    ``slicer.util.getModuleLogic("OpenLIFU<X>")`` synchronously here we
    could re-enter another module while its own ``setup()`` is still on
    the stack and trigger a "Failed to instantiate scripted pythonqt
    class" recursion.

    All callbacks accept ``*args, **kwargs`` because the various
    ``call_on_*`` callback registries pass different argument shapes
    (some pass the new value, some pass nothing).
    """

    def _wire_deferred():
        from OpenLIFULib.util import register_module_callback

        # --- Database ---
        try:
            db_logic = slicer.util.getModuleLogic("OpenLIFU").database_logic
            register_module_callback(
                widget_owner,
                db_logic.call_on_db_changed,
                db_logic.remove_db_changed_callback,
                lambda *_a, **_kw: header.updateStatusButtons(),
            )
        except Exception:  # noqa: BLE001
            pass

        # --- Login (active user only; the permissions dropdown was removed) ---
        try:
            login_pn = slicer.util.getModuleLogic("OpenLIFU").login_logic.getParameterNode()
            widget_owner.addObserver(
                login_pn,
                vtk.vtkCommand.ModifiedEvent,
                lambda caller, event: header.updateStatusButtons(),
            )
        except Exception:  # noqa: BLE001
            pass
        try:
            login_logic = slicer.util.getModuleLogic("OpenLIFU").login_logic
            register_module_callback(
                widget_owner,
                login_logic.call_on_active_user_changed,
                login_logic.remove_active_user_changed_callback,
                lambda *_a, **_kw: header.updateStatusButtons(),
            )
        except Exception:  # noqa: BLE001
            pass

        # --- Hardware device connect / disconnect ---
        try:
            sc_logic = slicer.util.getModuleLogic("OpenLIFU").sonication_control_logic
            register_module_callback(
                widget_owner,
                sc_logic.call_on_lifu_device_connected,
                sc_logic.remove_callback,
                lambda *_a, **_kw: header.updateStatusButtons(),
            )
            register_module_callback(
                widget_owner,
                sc_logic.call_on_lifu_device_disconnected,
                sc_logic.remove_callback,
                lambda *_a, **_kw: header.updateStatusButtons(),
            )
        except Exception:  # noqa: BLE001
            pass

        # --- Cloud sync state ---
        try:
            from OpenLIFUApp.logic.cloud_sync import getCloudSyncLogic
            cs_logic = getCloudSyncLogic()
            register_module_callback(
                widget_owner,
                cs_logic.call_on_state_changed,
                cs_logic.remove_state_changed_callback,
                lambda *_a, **_kw: header.updateStatusButtons(),
            )
        except Exception:  # noqa: BLE001
            pass

        # Final paint once every wiring is in place.
        try:
            header.refresh_all()
        except Exception:  # noqa: BLE001
            pass

    qt.QTimer.singleShot(0, _wire_deferred)


# ----------------------------------------------------------------------
# Navigation between OpenLIFU host pages
# ----------------------------------------------------------------------

def navigate_to_page(module_name: str) -> None:
    """Switch the OpenLIFU host module to its embedded page for ``module_name``.

    Drop-in replacement for :func:`slicer.util.selectModule` when navigating
    between embedded pages. Equivalent to::

        slicer.util.selectModule("OpenLIFU")
        slicer.util.getModuleWidget("OpenLIFU").show_page(module_name)

    Silently no-ops if the host module is not loaded.
    """
    slicer.util.selectModule("OpenLIFU")
    try:
        host_widget = slicer.util.getModuleWidget("OpenLIFU")
    except Exception:  # noqa: BLE001
        return
    show_page = getattr(host_widget, "show_page", None)
    if show_page is None:
        return
    show_page(module_name)

