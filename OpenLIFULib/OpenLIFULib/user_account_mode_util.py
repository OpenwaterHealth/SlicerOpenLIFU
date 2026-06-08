from types import SimpleNamespace
from typing import Optional, TYPE_CHECKING

import qt
import slicer

from OpenLIFULib.util import (
        get_openlifu_login_parameter_node,
        get_openlifu_login_logic,
        )

if TYPE_CHECKING:
    import openlifu.db

_restricted_anonymous_user = SimpleNamespace(
    id="anonymous",
    password_hash="",
    roles=[],
    name="Anonymous",
    description="Restricted fallback user before OpenLIFU dependencies are available.",
)

def get_current_user() -> "openlifu.db.User":
    """Get the active openlifu user. If no user is logged in or user account
    mode is off, a default user is returned, with the intention of being the most
    restricted"""
    return get_openlifu_login_logic().active_user or _restricted_anonymous_user

def get_user_account_mode_state() -> bool:
    """Get user account mode state from the OpenLIFU Login module's parameter node"""
    return get_openlifu_login_parameter_node().user_account_mode

def set_user_account_mode_state(new_user_account_mode_state: bool):
    """Set user account mode state in OpenLIFU Login module's parameter node"""
    get_openlifu_login_parameter_node().user_account_mode = new_user_account_mode_state


class UserAccountBanner(qt.QWidget):
    """A compact, read-only status bar shown at the top of OpenLIFU modules.

    Displays two status chips side by side:

    * Database chip (📁) – red outline when no database is loaded, green
      when one is connected.
    * User chip (👤) – green outline when a real user is signed in,
      no outline otherwise. Red outline when "User" permissions mode is on
      but no real user is signed in (i.e. login is required).

    A secondary warning row is shown when the active user has admin role.

    The banner does not expose any interactive controls; navigation to the
    database / login popups is done from the OpenLIFU Data module's toolbar
    instead. On the Data module itself this banner is typically hidden
    because the toolbar provides the same information in interactive form.
    """

    # Color constants kept in sync with ModuleHeaderWidget.updateStatusButtons
    _GREEN = "#2e7d32"
    _RED = "#c62828"

    def __init__(
            self,
            parent: qt.QWidget,
        ):
        """Compact OpenLIFU status banner.

        Args:
            parent: Parent QWidget
        """
        super().__init__(parent)

        top_level_layout = qt.QVBoxLayout(self)
        top_level_layout.setContentsMargins(0, 0, 0, 0)
        top_level_layout.setSpacing(2)

        # ---- status row: hidden in guided mode ----
        self._status_row = qt.QWidget()
        self._status_row.setProperty("slicer.openlifu.hide-in-guided-mode", True)
        row_layout = qt.QHBoxLayout(self._status_row)
        row_layout.setContentsMargins(0, 0, 0, 0)
        row_layout.setSpacing(6)

        # Database chip
        self._db_chip = qt.QLabel("\U0001F4C1  No database")
        self._db_chip.setObjectName("userAccountBannerDbChip")
        self._db_chip.setAlignment(qt.Qt.AlignLeft | qt.Qt.AlignVCenter)
        self._db_chip.setToolTip("Database status")
        row_layout.addWidget(self._db_chip)

        # User chip
        self._user_chip = qt.QLabel("\U0001F464  Not signed in")
        self._user_chip.setObjectName("userAccountBannerUserChip")
        self._user_chip.setAlignment(qt.Qt.AlignLeft | qt.Qt.AlignVCenter)
        self._user_chip.setToolTip("Active user")
        row_layout.addWidget(self._user_chip)

        row_layout.addStretch(1)

        top_level_layout.addWidget(self._status_row)

        # ---- admin warning row (not hidden in guided mode) ----
        self.warning_widget = qt.QWidget()
        warning_layout = qt.QHBoxLayout(self.warning_widget)
        warning_layout.setContentsMargins(0, 0, 0, 0)

        warning_icon = qt.QLabel()
        warning_icon.setPixmap(
            qt.QApplication.style().standardIcon(qt.QStyle.SP_MessageBoxWarning).pixmap(16, 16)
        )
        warning_icon.setFixedSize(16, 16)
        warning_layout.addWidget(warning_icon)

        self.warning_label = qt.QLabel("")
        self.warning_label.setAlignment(qt.Qt.AlignLeft | qt.Qt.AlignVCenter)
        self.warning_label.setStyleSheet("color: #cc7a00;")
        self.warning_label.setWordWrap(True)
        warning_layout.addWidget(self.warning_label, 1)

        self.warning_widget.visible = False
        top_level_layout.addWidget(self.warning_widget)

        # cached state used to repaint chips when either side changes
        self._current_user: Optional["openlifu.db.User"] = None
        self._has_database: bool = False
        self._database_label: str = ""
        # Cached "User permissions mode" state. The banner deliberately does
        # NOT read this from the Login parameter node on each repaint - doing
        # so calls ``slicer.util.getModuleLogic("OpenLIFULogin")`` which can
        # re-enter the Login widget while it is still being instantiated and
        # produces a "Failed to instantiate scripted pythonqt class
        # OpenLIFULoginWidget" error. The Login widget pushes UAM changes to
        # the banner via ``change_user_account_mode`` instead.
        self._user_account_mode: bool = False

        # Backwards-compatibility alias: callers (notably Login widget) still
        # reference ``active_user_label``; expose the user chip under that name.
        self.active_user_label = self._user_chip

        # NOTE: we deliberately do NOT call ``self._repaint()`` here. ``_repaint``
        # reads ``get_user_account_mode_state()`` which in turn calls
        # ``slicer.util.getModuleLogic("OpenLIFULogin")``. If this banner is
        # being constructed during another OpenLIFU module's ``setup()`` (which
        # is exactly what happens when the OpenLIFU Data widget is brought up
        # for the first time), forcing Login widget instantiation here would
        # re-enter the in-progress module setup and crash. Instead, the chips
        # are left with their default "no database" / "Not signed in" text and
        # neutral styling; ``OpenLIFULoginWidget.cacheAllLoginRelatedWidgets``
        # seeds the proper state immediately after construction.
        self._db_chip.setStyleSheet(
            self._chip_style("userAccountBannerDbChip", None)
        )
        self._user_chip.setStyleSheet(
            self._chip_style("userAccountBannerUserChip", None)
        )

    # ----- chip styling helpers -----

    @staticmethod
    def _chip_style(object_name: str, color: Optional[str]) -> str:
        if color is None:
            return (
                f"QLabel#{object_name} {{"
                f" padding: 2px 8px;"
                f" border: 1px solid palette(mid);"
                f" border-radius: 8px;"
                f" }}"
            )
        return (
            f"QLabel#{object_name} {{"
            f" padding: 2px 8px;"
            f" border: 2px solid {color};"
            f" border-radius: 8px;"
            f" }}"
        )

    def _repaint(self) -> None:
        # Database chip
        if self._has_database:
            text = "\U0001F4C1  " + (self._database_label or "Database connected")
            self._db_chip.setText(text)
            self._db_chip.setStyleSheet(
                self._chip_style("userAccountBannerDbChip", self._GREEN)
            )
            self._db_chip.setToolTip(text)
        else:
            self._db_chip.setText("\U0001F4C1  No database")
            self._db_chip.setStyleSheet(
                self._chip_style("userAccountBannerDbChip", self._RED)
            )
            self._db_chip.setToolTip(
                "No database is loaded. Open the OpenLIFU Data module to "
                "connect to one."
            )

        # User chip
        user = self._current_user
        user_id = getattr(user, "id", None)
        is_real_user = user is not None and user_id not in (None, "anonymous", "default_admin")
        uam = self._user_account_mode
        if is_real_user:
            name = getattr(user, "name", "") or user_id
            self._user_chip.setText(f"\U0001F464  {name}")
            self._user_chip.setStyleSheet(
                self._chip_style("userAccountBannerUserChip", self._GREEN)
            )
            self._user_chip.setToolTip(f"Signed in as {name}")
        elif uam:
            self._user_chip.setText("\U0001F464  Not signed in")
            self._user_chip.setStyleSheet(
                self._chip_style("userAccountBannerUserChip", self._RED)
            )
            self._user_chip.setToolTip(
                "Permissions is set to 'User' but no user is signed in."
            )
        else:
            self._user_chip.setText("\U0001F464  Not signed in")
            self._user_chip.setStyleSheet(
                self._chip_style("userAccountBannerUserChip", None)
            )
            self._user_chip.setToolTip("Not signed in")

    # ----- public update API -----

    def change_active_user(self, new_active_user: Optional["openlifu.db.User"]) -> None:
        """Update the user chip and admin warning based on the new active user."""
        self._current_user = new_active_user
        if new_active_user is not None and 'admin' in (getattr(new_active_user, "roles", None) or []):
            self.warning_label.setText(
                "You are logged in with admin privileges and have access to "
                "high-risk features."
            )
            self.warning_widget.visible = True
        else:
            self.warning_widget.visible = False
        self._repaint()

    def change_database_status(
            self,
            database: Optional["openlifu.db.Database"],
            label: Optional[str] = None,
        ) -> None:
        """Update the database chip.

        Args:
            database: The current ``openlifu.db.Database`` instance, or
                ``None`` if no database is loaded.
            label: Optional short label to display (e.g. the database name
                or path). When ``None`` a default message is used.
        """
        self._has_database = database is not None
        self._database_label = label or ""
        self._repaint()

    def change_user_account_mode(self, user_account_mode: bool) -> None:
        """Update the cached "User permissions mode" flag and repaint.

        The Login widget calls this whenever its parameter node changes so
        the banner can decide whether to paint a red outline on the user
        chip when no real user is signed in.
        """
        self._user_account_mode = bool(user_account_mode)
        self._repaint()
