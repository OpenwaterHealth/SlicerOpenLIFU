"""Shared QDialog subclasses for the OpenLIFU host module.

Per ``docs/coding-standards.md`` rule 7: any QDialog used from more
than one place gets its own file under ``OpenLIFUApp/dialogs/``.
Individual dialog modules import from :mod:`OpenLIFULib` and
:mod:`OpenLIFUApp.logic.session_actions`; they do not reach into
any page's ``Logic`` class.
"""

from __future__ import annotations
