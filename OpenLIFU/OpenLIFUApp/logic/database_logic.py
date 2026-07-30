"""Database-loading logic for the split-session app.

Owns the currently-loaded :class:`openlifu.db.Database` and a callback
list that ``OpenLIFULib.module_layout.wire_passive_module_header``
subscribes to so the shared header status icon can react to
load / unload events (SlicerOpenLIFU#632).

Callback surface (``call_on_db_changed`` /
``remove_db_changed_callback``) is kept minimal and lives here rather
than on a full Slicer parameter node because the header widget is
chrome, not domain state -- see ``docs/coding-standards.md`` rule 6
for the header-vs-page coordination policy. Pages should still read
the database directly via ``get_cur_db()`` on ``enter()`` rather than
taking out their own observers.

Also owns the ``OpenLIFU/databaseDirectory`` ``QSettings`` key. Every
successful :meth:`load_database` persists the loaded path; every
:meth:`unload_database` clears it. The Home page's ``enter()`` calls
:meth:`try_auto_connect` at each landing to re-open the last-used
database (SlicerOpenLIFU#635). No other page should reach for
``QSettings("OpenLIFU/databaseDirectory")`` directly.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import TYPE_CHECKING, Callable, List, Optional

import qt
from slicer.ScriptedLoadableModule import ScriptedLoadableModuleLogic

if TYPE_CHECKING:
    import openlifu.db


DbChangedCallback = Callable[[Optional["openlifu.db.Database"]], None]

# Single source of truth for the persistence key so mis-typing it in one
# place cannot silently break auto-connect. ``QSettings`` groups the key
# under ``OpenLIFU/`` in the platform-specific store (e.g. Windows registry
# under ``HKCU\\Software\\OpenLIFU``); see SlicerOpenLIFU#635.
DATABASE_DIRECTORY_SETTING_KEY = "OpenLIFU/databaseDirectory"


class DatabaseLogic(ScriptedLoadableModuleLogic):
    """Owns the currently-loaded :class:`openlifu.db.Database`, if any.

    Exposes:

    * ``.db`` -- the current database, or ``None``.
    * ``.load_database(path)`` -- open a database on disk. Fires
      registered callbacks.
    * ``.unload_database()`` -- drop the current database reference.
      Fires registered callbacks.
    * ``.call_on_db_changed(callback)`` /
      ``.remove_db_changed_callback(callback)`` -- callback registration
      surface used by the shared module header
      (``OpenLIFULib.module_layout.wire_passive_module_header``).
    """

    def __init__(self) -> None:
        ScriptedLoadableModuleLogic.__init__(self)
        # Public: read directly. get_cur_db() in OpenLIFULib/util.py
        # relies on this attribute name.
        self.db: "Optional[openlifu.db.Database]" = None
        # Header-icon-refresh callbacks. Fired on every load / unload.
        # See SlicerOpenLIFU#632 for background.
        self._on_db_changed_callbacks: List[DbChangedCallback] = []

    # ------------------------------------------------------------------
    # Database load / unload
    # ------------------------------------------------------------------

    def load_database(self, path) -> "openlifu.db.Database":
        """Open a Database at ``path`` and set it as the current database.

        Fires every callback registered via :meth:`call_on_db_changed`.
        Raises ``FileNotFoundError`` if the path does not exist.

        On success, persists ``path`` to
        ``QSettings("OpenLIFU/databaseDirectory")`` so
        :meth:`try_auto_connect` can re-open it on the next Slicer launch.
        """
        import openlifu.db

        path = Path(path)
        if not path.exists():
            raise FileNotFoundError(f"Database path does not exist: {path}")

        self.db = openlifu.db.Database(str(path))
        logging.info("DatabaseLogic: loaded database at %s", path)
        self._persist_database_path(path)
        self._fire_db_changed()
        return self.db

    def unload_database(self) -> None:
        """Drop the current database reference and fire callbacks.

        Also clears the persisted ``OpenLIFU/databaseDirectory`` setting
        so the user's explicit disconnect survives a Slicer restart --
        auto-connect won't fire back on next launch.
        """
        self.db = None
        self._clear_persisted_database_path()
        self._fire_db_changed()

    def try_auto_connect(self) -> bool:
        """Re-open the last-used database from ``QSettings``, if any.

        Returns ``True`` on a successful load, ``False`` otherwise. Every
        error path is swallowed and logged -- a missing / moved database
        directory or a corrupt setting must never break the caller's
        UI flow.

        No-ops (and returns ``False``) if a database is already loaded,
        so calling this from every ``Home.enter()`` cannot clobber a
        session in progress.

        Called from ``OpenLIFUHomeWidget.enter()`` (see
        SlicerOpenLIFU#635). Do not call from other pages -- database
        connect / disconnect is an app-wide state change and should
        happen at a single, predictable trigger (landing on Home).
        """
        if self.db is not None:
            return False
        try:
            qsettings = qt.QSettings()
            path_str = qsettings.value(DATABASE_DIRECTORY_SETTING_KEY, "")
            if not path_str:
                return False
            path = Path(str(path_str))
        except Exception:  # noqa: BLE001
            logging.exception("DatabaseLogic: could not read persisted db path")
            return False

        # Validate the persisted path still points at a real openlifu
        # database root before we hand it to the ``openlifu.db.Database``
        # constructor. ``Database(...)`` would raise a less helpful error
        # if the folder was deleted / renamed / repointed to something
        # unrelated between launches.
        from OpenLIFULib import sample_data
        if not sample_data.path_is_openlifu_database_root(path):
            logging.info(
                "DatabaseLogic: skipping auto-connect; %s is not a "
                "valid openlifu database root.", path,
            )
            return False

        try:
            self.load_database(path)
            logging.info("DatabaseLogic: auto-connected to database at %s", path)
            return True
        except Exception:  # noqa: BLE001
            logging.exception("DatabaseLogic: auto-connect failed for %s", path)
            return False

    # ------------------------------------------------------------------
    # QSettings persistence
    # ------------------------------------------------------------------

    def _persist_database_path(self, path: Path) -> None:
        """Write ``path`` to ``QSettings("OpenLIFU/databaseDirectory")``.

        Errors here are non-fatal: the database is already loaded in
        memory and the app can proceed; only the auto-connect on next
        launch is at risk. Log and continue.
        """
        try:
            qt.QSettings().setValue(DATABASE_DIRECTORY_SETTING_KEY, str(path))
        except Exception:  # noqa: BLE001
            logging.exception("DatabaseLogic: could not persist db path")

    def _clear_persisted_database_path(self) -> None:
        """Remove ``OpenLIFU/databaseDirectory`` from ``QSettings``."""
        try:
            qt.QSettings().remove(DATABASE_DIRECTORY_SETTING_KEY)
        except Exception:  # noqa: BLE001
            logging.exception("DatabaseLogic: could not clear persisted db path")

    # ------------------------------------------------------------------
    # Callback registration
    # ------------------------------------------------------------------

    def call_on_db_changed(self, callback: DbChangedCallback) -> None:
        """Register a callback to be fired on database load / unload.

        The callback receives the new database (or ``None`` on unload).
        Silently no-ops if the callback is already registered.
        """
        if callback not in self._on_db_changed_callbacks:
            self._on_db_changed_callbacks.append(callback)

    def remove_db_changed_callback(self, callback: DbChangedCallback) -> None:
        """Unregister a callback previously registered via
        :meth:`call_on_db_changed`. Silently no-ops if not registered."""
        try:
            self._on_db_changed_callbacks.remove(callback)
        except ValueError:
            pass

    def _fire_db_changed(self) -> None:
        """Notify every registered callback of the current database."""
        for callback in list(self._on_db_changed_callbacks):
            try:
                callback(self.db)
            except Exception:  # noqa: BLE001
                logging.exception("DatabaseLogic: db-changed callback raised")
