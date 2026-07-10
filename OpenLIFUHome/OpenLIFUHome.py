"""Thin Slicer-module shell for the OpenLIFU Home page.

The actual widget / logic / test / parameter-node classes live in
``OpenLIFUApp.pages.home_page`` (part of the OpenLIFU host module). This
file exists only so that Slicer still registers ``OpenLIFUHome`` as a
loadable module, allowing ``slicer.util.selectModule("OpenLIFUHome")``,
``slicer.util.getModuleLogic("OpenLIFUHome")``, and downstream
``openlifu-desktop-application/Modules/Scripted/Home/Home.py`` references
to keep working during the Round-5 migration (DEMODULING.md). Round 5c
will retire this shell entirely once the host owns Home.
"""

from __future__ import annotations

from slicer.ScriptedLoadableModule import ScriptedLoadableModule
from slicer.i18n import tr as _
from slicer.i18n import translate

# Re-export the extracted classes so Slicer's module discovery
# (which looks up ``<Module>Widget``, ``<Module>Logic``, ``<Module>Test``
# by attribute on this file) finds them, and so external callers like
# ``from OpenLIFUHome import OpenLIFUHomeTest`` and
# ``slicer.util.getModuleLogic("OpenLIFUHome")`` keep working.
from OpenLIFUApp.pages.home_page import (  # noqa: F401
    OpenLIFUHomeParameterNode,
    OpenLIFUHomeWidget,
    OpenLIFUHomeLogic,
    OpenLIFUHomeTest,
)


class OpenLIFUHome(ScriptedLoadableModule):
    """Operator-facing landing page for the OpenLIFU workflow.

    Shows live database / user / device status and offers New Session /
    Load Session / Data Manager entry points.
    """

    def __init__(self, parent):
        ScriptedLoadableModule.__init__(self, parent)
        self.parent.title = _("OpenLIFU Home")
        self.parent.categories = [translate("qSlicerAbstractCoreModule", "OpenLIFU")]
        # NOTE: do NOT depend on "OpenLIFU" here -- the host module already
        # depends on OpenLIFUHome, and adding a reverse dep would cycle
        # and hang Slicer at "Loading OpenLIFU...". The ``OpenLIFUApp``
        # subpackage is importable regardless of load order because Slicer
        # adds every scripted-module directory to sys.path during discovery,
        # before any module is instantiated.
        self.parent.dependencies = []
        self.parent.contributors = ["Ebrahim Ebrahim (Kitware), Sadhana Ravikumar (Kitware), Peter Hollender (Openwater), Sam Horvath (Kitware), Brad Moore (Kitware)"]
        self.parent.helpText = _(
            "This is the home module of the OpenLIFU extension for focused ultrasound. "
            "More information at <a href=\"https://github.com/OpenwaterHealth/SlicerOpenLIFU\">github.com/OpenwaterHealth/SlicerOpenLIFU</a>."
        )
        self.parent.acknowledgementText = _(
            "This is part of Openwater's OpenLIFU, an open-source "
            "hardware and software platform for Low Intensity Focused Ultrasound (LIFU) research "
            "and development."
        )
        # Embedded as a page of the OpenLIFU host module; hide from the modules menu.
        self.parent.hidden = True
