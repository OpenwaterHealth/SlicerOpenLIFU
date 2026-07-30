"""Minimal database-loading logic for the split-session app.

Fresh rewrite; replaces (during the transition) the ``OpenLIFUDataba
seLogic`` that lived in the legacy ``database_page.py``. Only the
surface :func:`OpenLIFULib.util.get_cur_db` needs is provided:

* ``.db`` -- the currently-loaded :class:`openlifu.db.Database`, or ``None``.
* ``.load_database(path)`` -- open a Database on disk.

Callbacks and MRML-parameter-node bookkeeping are deliberately omitted;
the Data Manager page does not need them. If future pages need to
react to database changes, they should refresh on ``enter()`` rather
than take out a persistent observer.

Follows ``docs/coding-standards.md``: no underscore-prefixed backing
attribute, no unused property boilerplate.
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
        # Public: read directly. get_cur_db() in OpenLIFULib/util.py
        # relies on this attribute name.
        self.db: "Optional[openlifu.db.Database]" = None

    def load_database(self, path) -> "openlifu.db.Database":
        """Open a Database at ``path`` and set it as the current database.

        Raises ``FileNotFoundError`` if the path does not exist.
        """
        import openlifu.db

        path = Path(path)
        if not path.exists():
            raise FileNotFoundError(f"Database path does not exist: {path}")

        self.db = openlifu.db.Database(str(path))
        logging.info("DatabaseLogic: loaded database at %s", path)
        return self.db

    def unload_database(self) -> None:
        """Drop the current database reference."""
        self.db = None
