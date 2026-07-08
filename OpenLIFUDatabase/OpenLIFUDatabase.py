"""Thin Slicer-module shell for the OpenLIFU Database page.

The actual widget / logic / test / parameter-node classes live in
``OpenLIFUApp.pages.database_page`` (part of the OpenLIFU host module). This
file exists only so that Slicer still registers ``OpenLIFUDatabase`` as a
loadable module, allowing ``slicer.util.getModuleLogic('OpenLIFUDatabase')``
and ``slicer.util.getModuleWidget('OpenLIFUDatabase').resourcePath(...)`` to
keep working during the de-moduling migration (rounds 3-4 of DEMODULING.md).
Round 5 retires this shell once the host owns page navigation.

Do NOT add ``"OpenLIFU"`` to ``dependencies`` -- Round 4 will add
``OpenLIFUDatabase`` to the host's ``dependencies``; a reverse dep would
cycle and hang Slicer at "Loading OpenLIFU...". ``OpenLIFUApp`` is
importable regardless of load order.
"""

from __future__ import annotations

from slicer.ScriptedLoadableModule import ScriptedLoadableModule
from slicer.i18n import tr as _
from slicer.i18n import translate

# Re-export the extracted classes so external ``from OpenLIFUDatabase import
# OpenLIFUDatabaseTest`` (used by OpenLIFUHome) and
# ``from OpenLIFUDatabase.OpenLIFUDatabase import OpenLIFUDatabaseParameterNode``
# (used by OpenLIFULib.util under TYPE_CHECKING) keep working, and Slicer can
# discover the Widget/Logic/Test classes via attribute lookup.
from OpenLIFUApp.pages.database_page import (  # noqa: F401
    OpenLIFUDatabaseLogic,
    OpenLIFUDatabaseParameterNode,
    OpenLIFUDatabaseTest,
    OpenLIFUDatabaseWidget,
)


class OpenLIFUDatabase(ScriptedLoadableModule):
    """Uses ScriptedLoadableModule base class, available at:
    https://github.com/Slicer/Slicer/blob/main/Base/Python/slicer/ScriptedLoadableModule.py
    """

    def __init__(self, parent):
        ScriptedLoadableModule.__init__(self, parent)
        self.parent.title = _("OpenLIFU Database")
        self.parent.categories = [translate("qSlicerAbstractCoreModule", "OpenLIFU.OpenLIFU Modules")]
        self.parent.dependencies = ["OpenLIFUHome"]
        self.parent.contributors = ["Andrew Howe (Kitware), Ebrahim Ebrahim (Kitware), Sadhana Ravikumar (Kitware), Peter Hollender (Openwater), Sam Horvath (Kitware), Brad Moore (Kitware)"]
        self.parent.helpText = _(
            "This is the database module of the OpenLIFU extension for focused ultrasound. "
            "More information at <a href=\"https://github.com/OpenwaterHealth/SlicerOpenLIFU\">github.com/OpenwaterHealth/SlicerOpenLIFU</a>."
        )
        self.parent.acknowledgementText = _(
            "This is part of Openwater's OpenLIFU, an open-source "
            "hardware and software platform for Low Intensity Focused Ultrasound (LIFU) research "
            "and development."
        )
        # Hide this module from the module selector panel. Its functionality is
        # surfaced via the Database popup on the Data page; the module's logic
        # and widget representation remain accessible to other modules through
        # ``slicer.util.getModuleLogic`` / ``slicer.util.getModuleWidget``.
        self.parent.hidden = True
