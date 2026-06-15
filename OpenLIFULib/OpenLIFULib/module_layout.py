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
  OpenLIFUSonicationControl, OpenLIFUDatabase, OpenLIFUCloudSync).

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


def _outline_style(object_name: str, hex_color: str) -> str:
    """A 2px coloured border around an emoji button without overriding padding."""
    return (
        f"QPushButton#{object_name} {{ "
        f"border: 2px solid {hex_color}; "
        f"border-radius: 4px; "
        f"}}"
    )


def _strip_click_to(tooltip: str) -> str:
    """Drop any trailing 'Click to ...' sentence from a status tooltip.

    The interactive header tooltips end with phrases like
    ``"... Click to open Account."`` or ``"... Click to view or change."``.
    On a read-only header those phrases are misleading because the buttons
    are disabled, so we strip them before appending the read-only note.
    """
    if not tooltip:
        return ""
    # Walk backwards for the last "Click to ..." sentence (case-insensitive).
    lower = tooltip.lower()
    idx = lower.rfind("click to ")
    if idx == -1:
        return tooltip.rstrip()
    return tooltip[:idx].rstrip().rstrip(".").rstrip()


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

        self.databasePopupButton = _make_button(
            "databasePopupButton",
            "📁",
            "Open the database configuration panel",
        )
        layout.addWidget(self.databasePopupButton)

        self.loginPopupButton = _make_button(
            "loginPopupButton",
            "👤",
            "Open the user login / account panel",
        )
        layout.addWidget(self.loginPopupButton)

        self.devicePopupButton = _make_button(
            "devicePopupButton",
            "",
            "Hardware device connection status",
        )
        # The device button uses the PNG that ships with OpenLIFUData (single
        # piece of art shared across modules). We resolve the path through
        # the OpenLIFUData module so the icon survives any path layout.
        try:
            data_module_dir = os.path.dirname(
                slicer.util.modulePath("OpenLIFUData")
            )
            icon_path = os.path.join(data_module_dir, "Resources", "Icons", "device.png")
            if os.path.exists(icon_path):
                self.devicePopupButton.setIcon(qt.QIcon(icon_path))
                self.devicePopupButton.setIconSize(qt.QSize(20, 20))
        except Exception as e:  # noqa: BLE001
            logging.warning("Could not load device button icon: %s", e)
        layout.addWidget(self.devicePopupButton)

        layout.addStretch(1)

    def _apply_read_only(self) -> None:
        # Database / login global state should not be changed from inside
        # a workflow step. Disable the database button (and the login button,
        # unless the host opted in to keep it active), but leave the device
        # (transducer info) button live - it is purely informational and
        # useful from any page.
        disabled = [self.databasePopupButton]
        if not self._keep_login_button_active:
            disabled.append(self.loginPopupButton)
        for w in disabled:
            w.setEnabled(False)

        # Wire the device-info button to the Data module's popup handler so
        # the same dialog is reachable from every workflow page.
        self.devicePopupButton.clicked.connect(self._open_device_popup_via_data_module)

        # Modules that opt in (e.g. Home) keep the sign-in icon live and
        # delegate to Data's existing login popup handler.
        if self._keep_login_button_active:
            self.loginPopupButton.clicked.connect(self._open_login_popup_via_data_module)

    def _open_device_popup_via_data_module(self, _checked: bool = False) -> None:
        """Delegate to ``OpenLIFUDataWidget.onOpenDevicePopup`` (read-only header)."""
        try:
            data_widget = slicer.util.getModuleWidget("OpenLIFUData")
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
            data_widget = slicer.util.getModuleWidget("OpenLIFUData")
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
            from OpenLIFUCloudSync import getCloudSyncLogic
            cs_logic = getCloudSyncLogic()
            cs_enabled = cs_logic.is_service_enabled()
            cs_running = cs_logic.is_service_running()
            cs_logged_in = cs_logic.is_logged_in()
            cs_failed = cs_logic.did_service_fail()
        except Exception:  # noqa: BLE001
            cs_enabled = cs_running = cs_logged_in = cs_failed = False

        cloud_ok = cs_enabled and cs_running and cs_logged_in and not cs_failed
        cloud_broken = cs_enabled and not cloud_ok

        if cs_enabled and cloud_ok:
            self.databasePopupButton.setText("☁📁")
        elif cloud_broken:
            self.databasePopupButton.setText("🌩📁")
        else:
            self.databasePopupButton.setText("📁")

        if db_connected:
            outline_color = "#f9a825" if cloud_broken else "#2e7d32"  # yellow / green
            self.databasePopupButton.setStyleSheet(
                _outline_style("databasePopupButton", outline_color)
            )
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
            self.databasePopupButton.setStyleSheet(
                _outline_style("databasePopupButton", "#c62828")
            )
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
        # Crown for admins, plain bust silhouette otherwise. The crown
        # makes admin sessions visually distinct from operator sessions
        # without needing a separate "admin warning" banner.
        self.loginPopupButton.setText("\U0001F451" if is_admin else "\U0001F464")
        if is_real_user:
            self.loginPopupButton.setStyleSheet(
                _outline_style("loginPopupButton", "#2e7d32")
            )
            who = getattr(cur_user, 'name', '') or user_id
            if is_admin:
                self.loginPopupButton.setToolTip(
                    f"Signed in as {who} (admin).\n"
                    f"You have access to high-risk features. "
                    f"Click to open Account."
                )
            else:
                self.loginPopupButton.setToolTip(
                    f"Signed in as {who}. Click to open Account."
                )
        elif uam:
            self.loginPopupButton.setStyleSheet(
                _outline_style("loginPopupButton", "#c62828")
            )
            self.loginPopupButton.setToolTip(
                "Permissions is set to 'User'. Click to open Account and log in."
            )
        else:
            self.loginPopupButton.setStyleSheet("")
            self.loginPopupButton.setToolTip(
                "Not signed in. Click to open Account."
            )
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
            sc_logic = slicer.util.getModuleLogic("OpenLIFUSonicationControl")
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
        if iface is None and in_use_pid is not None:
            # Locked out: another process owns the hardware. Red outline
            # (Material red 800) and a tooltip pointing the user at the
            # device popup, where they can read the offending PID and
            # click Retry once the other application has been closed.
            self.devicePopupButton.setStyleSheet(
                _outline_style("devicePopupButton", "#c62828")
            )
            self.devicePopupButton.setToolTip(
                f"LIFU hardware is in use by another process (PID {in_use_pid}). "
                "Click for details and to retry connecting."
            )
        elif is_simulated:
            # Pink outline so the simulated interface is visually
            # distinct from a real connected device. (Material pink 500.)
            self.devicePopupButton.setStyleSheet(
                _outline_style("devicePopupButton", "#ff84f9")
            )
            self.devicePopupButton.setToolTip(
                "Simulated hardware device connected (no real device). "
                "Click for details."
            )
        elif tx_conn and hv_conn:
            self.devicePopupButton.setStyleSheet(
                _outline_style("devicePopupButton", "#2e7d32")
            )
            self.devicePopupButton.setToolTip(
                "Hardware device fully connected (TX + HV). Click for details."
            )
        elif tx_conn or hv_conn:
            self.devicePopupButton.setStyleSheet(
                _outline_style("devicePopupButton", "#f9a825")
            )
            half = "TX only" if tx_conn else "HV only"
            self.devicePopupButton.setToolTip(
                f"Hardware partially connected ({half}). Click for details."
            )
        else:
            self.devicePopupButton.setStyleSheet(
                _outline_style("devicePopupButton", "#000000")
            )
            self.devicePopupButton.setToolTip(
                "No hardware device connected. Click for details."
            )

        # In read-only mode (every module page except OpenLIFU Data), the
        # database and login buttons are disabled. Rewrite the
        # "Click to ..." tooltips that ``updateStatusButtons`` just installed
        # so they reflect that, and tell the user where to go to change
        # those settings. The device button stays interactive on every
        # page so its tooltip is left untouched. When the host module opted
        # in to keep the login button active (e.g. Home), leave its tooltip
        # alone too.
        if self.read_only:
            db_tip = self.databasePopupButton.toolTip
            self.databasePopupButton.setToolTip(
                _strip_click_to(db_tip) + "\n\nChange in the Data module."
            )
            if not self._keep_login_button_active:
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
    inside ``OpenLIFULoginWidget.cacheAllLoginRelatedWidgets`` (which calls
    ``widgetRepresentation()`` on every OpenLIFU module). If we touched
    ``slicer.util.getModuleLogic("OpenLIFU<X>")`` synchronously here we
    could re-enter Login (or other modules) while their own ``setup()`` is
    still on the stack and trigger the "Failed to instantiate scripted
    pythonqt class OpenLIFULoginWidget" recursion.

    All callbacks accept ``*args, **kwargs`` because the various
    ``call_on_*`` callback registries pass different argument shapes
    (some pass the new value, some pass nothing).
    """

    def _wire_deferred():
        from OpenLIFULib.util import register_module_callback

        # --- Database ---
        try:
            db_logic = slicer.util.getModuleLogic("OpenLIFUDatabase")
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
            login_pn = slicer.util.getModuleLogic("OpenLIFULogin").getParameterNode()
            widget_owner.addObserver(
                login_pn,
                vtk.vtkCommand.ModifiedEvent,
                lambda caller, event: header.updateStatusButtons(),
            )
        except Exception:  # noqa: BLE001
            pass
        try:
            login_logic = slicer.util.getModuleLogic("OpenLIFULogin")
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
            sc_logic = slicer.util.getModuleLogic("OpenLIFUSonicationControl")
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
            from OpenLIFUCloudSync import getCloudSyncLogic
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
