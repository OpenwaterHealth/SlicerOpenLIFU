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
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import TYPE_CHECKING, Callable, List, Optional

from slicer.ScriptedLoadableModule import ScriptedLoadableModuleLogic

if TYPE_CHECKING:
    import openlifu.db


DbChangedCallback = Callable[[Optional["openlifu.db.Database"]], None]


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
        """
        import openlifu.db

        path = Path(path)
        if not path.exists():
            raise FileNotFoundError(f"Database path does not exist: {path}")

        self.db = openlifu.db.Database(str(path))
        logging.info("DatabaseLogic: loaded database at %s", path)
        self._fire_db_changed()
        return self.db

    def unload_database(self) -> None:
        """Drop the current database reference and fire callbacks."""
        self.db = None
        self._fire_db_changed()

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
