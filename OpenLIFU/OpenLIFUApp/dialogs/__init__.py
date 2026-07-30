"""Shared QDialog subclasses for the OpenLIFU host module.

Per ``docs/coding-standards.md`` rule 7: any QDialog used from more
than one place gets its own file under ``OpenLIFUApp/dialogs/``.
Individual dialog modules import from :mod:`OpenLIFULib` and
:mod:`OpenLIFUApp.logic.session_actions`; they do not reach into
any page's ``Logic`` class.

Also owns :func:`make_ok_cancel_button_box`, a PythonQt-safe factory
for the standard Ok / Cancel ``QDialogButtonBox`` shape. Every
QDialog in the OpenLIFU app should use it instead of building
``QDialogButtonBox(Ok | Cancel)`` directly (see docstring for the
underlying PythonQt bug).
"""

from __future__ import annotations

import qt


def make_ok_cancel_button_box(
    *, ok_label: str = "OK", cancel_label: str = "Cancel",
) -> qt.QDialogButtonBox:
    """Build an Ok / Cancel ``QDialogButtonBox`` that actually shows buttons.

    PythonQt under Slicer does not reliably materialise standard
    buttons from the ``QDialogButtonBox(Ok | Cancel)`` flag constructor
    -- the dialog renders with an empty button strip and users have
    no way to accept or reject the dialog. ``QDialogButtonBox.button(role)``
    also returns ``None`` in this environment, so we cannot look up
    the standard buttons after construction to rename or disable them.

    Fix: build the two buttons as plain ``QPushButton`` s and attach
    them via ``QDialogButtonBox.addButton(button, role)``. That path
    does not depend on any of the broken PythonQt accessors and the
    resulting dialog renders correctly.

    Callers should still connect ``buttons.accepted`` / ``buttons.rejected``
    to their handlers -- the returned box wires ``AcceptRole`` /
    ``RejectRole`` for the standard signal fan-out.

    Args:
        ok_label: text on the accept button; defaults to ``"OK"``.
            Use e.g. ``"Load Planning Session"`` when the semantic
            action is more specific than "OK".
        cancel_label: text on the reject button.

    Returns:
        A ``QDialogButtonBox`` with two buttons already added. The
        caller wires up the signals.
    """
    buttons = qt.QDialogButtonBox()
    ok_button = qt.QPushButton(ok_label)
    buttons.addButton(ok_button, qt.QDialogButtonBox.AcceptRole)
    cancel_button = qt.QPushButton(cancel_label)
    buttons.addButton(cancel_button, qt.QDialogButtonBox.RejectRole)
    return buttons
