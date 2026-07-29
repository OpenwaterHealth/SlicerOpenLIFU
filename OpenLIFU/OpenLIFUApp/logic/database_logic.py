"""Minimal database-loading logic for the split-session app.

Fresh rewrite; replaces (during the transition) the ``OpenLIFUDatabaseLogic``
that lived in the legacy ``database_page.py``. Only the surface
:func:`OpenLIFULib.util.get_cur_db` needs is provided:

* ``.db`` -- the currently-loaded :class:`openlifu.db.Database`, or ``None``.
* ``.load_database(path)`` -- open a Database on disk.

Callbacks and MRML-parameter-node bookkeeping are deliberately omitted; the
Data Manager page does not need them. If future pages need to react to
database changes, they should refresh on ``enter()`` rather than take out a
persistent observer.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import TYPE_CHECKING, Optional

from slicer.ScriptedLoadableModule import ScriptedLoadableModuleLogic

if TYPE_CHECKING:
    import openlifu.db


class DatabaseLogic(ScriptedLoadableModuleLogic):
    """Owns the currently-loaded :class:`openlifu.db.Database`, if any."""

    def __init__(self) -> None:
        ScriptedLoadableModuleLogic.__init__(self)
        self._db: "Optional[openlifu.db.Database]" = None

    @property
    def db(self) -> "Optional[openlifu.db.Database]":
        return self._db

    @db.setter
    def db(self, value: "Optional[openlifu.db.Database]") -> None:
        self._db = value

    def load_database(self, path: Path) -> "openlifu.db.Database":
        """Open a Database at ``path`` and set it as the current database.

        Raises ``FileNotFoundError`` if the path does not exist.
        """
        import openlifu.db

        path = Path(path)
        if not path.exists():
            raise FileNotFoundError(f"Database path does not exist: {path}")

        self._db = openlifu.db.Database(str(path))
        logging.info("DatabaseLogic: loaded database at %s", path)
        return self._db

    def unload_database(self) -> None:
        """Drop the current database reference."""
        self._db = None
