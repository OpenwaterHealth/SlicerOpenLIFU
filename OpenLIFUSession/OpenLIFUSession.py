"""Thin Slicer-module shell for the OpenLIFU Session page.

The actual widget / logic / test / parameter-node classes live in
``OpenLIFUApp.pages.session_page`` (part of the OpenLIFU host module). This
file exists only so that Slicer still registers ``OpenLIFUSession`` as a
loadable module, allowing ``slicer.util.selectModule("OpenLIFUSession")``
and ``slicer.modules.OpenLIFUSessionWidget`` to keep working during the
de-moduling migration (rounds 1-4 of DEMODULING.md). Round 5 will retire
this shell entirely once the host owns page navigation.
"""

from __future__ import annotations

from slicer.ScriptedLoadableModule import ScriptedLoadableModule
from slicer.i18n import tr as _
from slicer.i18n import translate

# Re-export the extracted classes so Slicer's module discovery
# (which looks up ``<Module>Widget``, ``<Module>Logic``, ``<Module>Test``
# by attribute on this file) finds them, and so external callers like
# ``from OpenLIFUSession import OpenLIFUSessionTest`` keep working.
from OpenLIFUApp.pages.session_page import (  # noqa: F401
    OpenLIFUSessionParameterNode,
    OpenLIFUSessionWidget,
    OpenLIFUSessionLogic,
    OpenLIFUSessionTest,
)


class OpenLIFUSession(ScriptedLoadableModule):
    """Read-only landing page for an active OpenLIFU session.

    Acts as the dashboard between the Home page (where a session is created
    or loaded) and the workflow modules (Pre-Planning onward).
    """

    def __init__(self, parent):
        ScriptedLoadableModule.__init__(self, parent)
        self.parent.title = _("OpenLIFU Session")
        self.parent.categories = [translate("qSlicerAbstractCoreModule", "OpenLIFU.OpenLIFU Modules")]
        # NOTE: do NOT depend on "OpenLIFU" here -- the host module already
        # depends on OpenLIFUSession, and adding a reverse dep would cycle
        # and hang Slicer at "Loading OpenLIFU...". The ``OpenLIFUApp``
        # subpackage is importable regardless of load order because Slicer
        # adds every scripted-module directory to sys.path during discovery,
        # before any module is instantiated.
        self.parent.dependencies = ["OpenLIFUHome"]
        self.parent.contributors = ["Peter Hollender (Openwater), Ebrahim Ebrahim (Kitware)"]
        self.parent.helpText = _(
            "This is the session dashboard module of the OpenLIFU extension for focused ultrasound. "
            "It displays read-only information about the currently loaded session. "
            "More information at <a href=\"https://github.com/OpenwaterHealth/SlicerOpenLIFU\">github.com/OpenwaterHealth/SlicerOpenLIFU</a>."
        )
        self.parent.acknowledgementText = _(
            "This is part of Openwater's OpenLIFU, an open-source "
            "hardware and software platform for Low Intensity Focused Ultrasound (LIFU) research "
            "and development."
        )
        # Embedded as a page of the OpenLIFU host module; hide from the modules menu.
        self.parent.hidden = True
