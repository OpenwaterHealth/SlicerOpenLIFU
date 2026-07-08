"""Thin Slicer-module shell for the OpenLIFU Pre-Planning page.

The actual widget / logic / test / parameter-node classes live in
``OpenLIFUApp.pages.preplanning_page`` (part of the OpenLIFU host module).
This file exists only so that Slicer still registers ``OpenLIFUPrePlanning``
as a loadable module, allowing ``slicer.util.selectModule(...)``,
``slicer.modules.OpenLIFUPrePlanningWidget``, and
``slicer.util.getModuleLogic("OpenLIFUPrePlanning")`` to keep working during
the de-moduling migration (rounds 2-4 of DEMODULING.md). Round 5 retires
this shell once the host owns page navigation.

Do NOT add ``"OpenLIFU"`` to ``dependencies`` -- the OpenLIFU host module
already depends on OpenLIFUPrePlanning; a reverse dep cycles and hangs
Slicer at "Loading OpenLIFU...". ``OpenLIFUApp`` is importable regardless of
load order (Slicer adds every scripted-module directory to sys.path during
discovery).
"""

from __future__ import annotations

from slicer.ScriptedLoadableModule import ScriptedLoadableModule
from slicer.i18n import tr as _
from slicer.i18n import translate

# Re-export the extracted classes so Slicer's module discovery
# (which looks up ``<Module>Widget``, ``<Module>Logic``, ``<Module>Test``
# by attribute on this file) finds them, and so external callers like
# ``from OpenLIFUPrePlanning import OpenLIFUPrePlanningTest`` keep working.
from OpenLIFUApp.pages.preplanning_page import (  # noqa: F401
    ImportTargetDialog,
    OpenLIFUPrePlanningParameterNode,
    OpenLIFUPrePlanningWidget,
    OpenLIFUPrePlanningLogic,
    OpenLIFUPrePlanningTest,
)


class OpenLIFUPrePlanning(ScriptedLoadableModule):
    """Uses ScriptedLoadableModule base class, available at:
    https://github.com/Slicer/Slicer/blob/main/Base/Python/slicer/ScriptedLoadableModule.py
    """

    def __init__(self, parent):
        ScriptedLoadableModule.__init__(self, parent)
        self.parent.title = _("OpenLIFU Pre-Planning")
        self.parent.categories = [translate("qSlicerAbstractCoreModule", "OpenLIFU.OpenLIFU Modules")]
        self.parent.dependencies = ["OpenLIFUHome"]
        self.parent.contributors = ["Ebrahim Ebrahim (Kitware), Sadhana Ravikumar (Kitware), Peter Hollender (Openwater), Sam Horvath (Kitware), Brad Moore (Kitware)"]
        self.parent.helpText = _(
            "This is the pre-planning module of the OpenLIFU extension for focused ultrasound. "
            "More information at <a href=\"https://github.com/OpenwaterHealth/SlicerOpenLIFU\">github.com/OpenwaterHealth/SlicerOpenLIFU</a>."
        )
        self.parent.acknowledgementText = _(
            "This is part of Openwater's OpenLIFU, an open-source "
            "hardware and software platform for Low Intensity Focused Ultrasound (LIFU) research "
            "and development."
        )
        # Embedded as a page of the OpenLIFU host module; hide from the modules menu.
        self.parent.hidden = True
