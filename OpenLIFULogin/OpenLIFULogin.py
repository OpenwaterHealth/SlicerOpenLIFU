"""Thin Slicer-module shell for the OpenLIFU Login page.

The actual widget / logic / dialog / parameter-node classes live in
``OpenLIFUApp.pages.login_page`` (part of the OpenLIFU host module). This
file exists only so that Slicer still registers ``OpenLIFULogin`` as a
loadable module, allowing ``slicer.util.getModuleLogic('OpenLIFULogin')``
and ``slicer.util.getModuleWidget('OpenLIFULogin')`` to keep working during
the de-moduling migration (rounds 3-4 of DEMODULING.md). Round 5 retires
this shell once the host owns page navigation.

Do NOT add ``"OpenLIFU"`` to ``dependencies`` -- Round 4 will add
``OpenLIFULogin`` to the host's ``dependencies``; a reverse dep would cycle
and hang Slicer at "Loading OpenLIFU...". ``OpenLIFUApp`` is importable
regardless of load order.
"""

from __future__ import annotations

from slicer.ScriptedLoadableModule import ScriptedLoadableModule
from slicer.i18n import tr as _
from slicer.i18n import translate

# Re-export the extracted classes so external
# ``from OpenLIFULogin.OpenLIFULogin import OpenLIFULoginParameterNode`` /
# ``OpenLIFULoginLogic`` (used by OpenLIFULib.util under TYPE_CHECKING) and
# ``from OpenLIFUDatabase import ...`` sibling patterns keep working, and
# Slicer can discover the Widget/Logic classes via attribute lookup. Also
# re-export ``UsernamePasswordDialog`` which the Database page imports.
from OpenLIFUApp.pages.login_page import (  # noqa: F401
    ChangePasswordDialog,
    CreateNewAccountDialog,
    LoginState,
    ManageAccountsDialog,
    OpenLIFULoginLogic,
    OpenLIFULoginParameterNode,
    OpenLIFULoginWidget,
    UsernamePasswordDialog,
)


class OpenLIFULogin(ScriptedLoadableModule):
    """Uses ScriptedLoadableModule base class, available at:
    https://github.com/Slicer/Slicer/blob/main/Base/Python/slicer/ScriptedLoadableModule.py
    """

    def __init__(self, parent):
        ScriptedLoadableModule.__init__(self, parent)
        self.parent.title = _("OpenLIFU Login")
        self.parent.categories = [translate("qSlicerAbstractCoreModule", "OpenLIFU.OpenLIFU Modules")]
        self.parent.dependencies = [
            "OpenLIFUDatabase",
            "OpenLIFUData",
            "OpenLIFUHome",
            "OpenLIFUPrePlanning",
            "OpenLIFUSonicationControl",
            "OpenLIFUSonicationPlanner",
            "OpenLIFUTransducerLocalization",
        ]
        self.parent.contributors = ["Andrew Howe (Kitware), Ebrahim Ebrahim (Kitware), Sadhana Ravikumar (Kitware), Peter Hollender (Openwater), Sam Horvath (Kitware), Brad Moore (Kitware)"]
        self.parent.helpText = _(
            "This is the login module of the OpenLIFU extension for focused ultrasound. "
            "More information at <a href=\"https://github.com/OpenwaterHealth/SlicerOpenLIFU\">github.com/OpenwaterHealth/SlicerOpenLIFU</a>."
        )
        self.parent.acknowledgementText = _(
            "This is part of Openwater's OpenLIFU, an open-source "
            "hardware and software platform for Low Intensity Focused Ultrasound (LIFU) research "
            "and development."
        )
        # Hide this module from the module selector panel. Its functionality is
        # surfaced via the Login popup on the Data page; the module's logic and
        # widget representation remain accessible to other modules through
        # ``slicer.util.getModuleLogic`` / ``slicer.util.getModuleWidget``.
        self.parent.hidden = True
