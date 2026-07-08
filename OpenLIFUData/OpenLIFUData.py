"""Thin Slicer-module shell for the OpenLIFU Data page.

The actual widget / logic / test / dialog / form-widget / parameter-node
classes live in ``OpenLIFUApp.pages.data_page`` (part of the OpenLIFU host
module). This file exists only so that Slicer still registers
``OpenLIFUData`` as a loadable module, allowing
``slicer.util.selectModule('OpenLIFUData')``,
``slicer.util.getModuleLogic('OpenLIFUData')`` (used throughout
OpenLIFULib, module_layout, the host module, all page files), and
``slicer.util.getModuleWidget('OpenLIFUData')`` to keep working during the
de-moduling migration (round 4a of DEMODULING.md). Round 5 retires this
shell once the host owns page navigation.

Do NOT add ``"OpenLIFU"`` to ``dependencies`` -- the host ``OpenLIFU``
module already declares ``dependencies=[..., "OpenLIFUData", ...]``; a
reverse dep cycles and hangs Slicer at "Loading OpenLIFU...".
``OpenLIFUApp`` is importable regardless of load order.

Rounds 4b/4c will migrate ``loaded_*`` fields onto ``OpenLIFUAppState``,
delete ``get_openlifu_data_parameter_node()``, and convert the remaining
VTK ``ModifiedEvent`` observers in the Round-2 pages to Qt signals.
"""

from __future__ import annotations

from slicer.ScriptedLoadableModule import ScriptedLoadableModule
from slicer.i18n import tr as _
from slicer.i18n import translate

# Re-export the Slicer-discovery classes (Widget/Logic/Test/ParameterNode)
# and every OpenLIFUData symbol that other modules import by name via
# ``from OpenLIFUData import <name>`` or
# ``from OpenLIFUData.OpenLIFUData import <name>``. This list is minimal:
# add here only what an external caller actually imports. Everything else
# stays internal to ``OpenLIFUApp.pages.data_page``.
from OpenLIFUApp.pages.data_page import (  # noqa: F401
    CreateNewSessionDialog,
    LoadSubjectDialog,
    OpenLIFUDataLogic,
    OpenLIFUDataParameterNode,
    OpenLIFUDataTest,
    OpenLIFUDataWidget,
    ProtocolPreviewDialog,
    RunManagerDialog,
    TransducerPreviewDialog,
    _JsonTreeDialog,
    _ModuleWidgetPopupDialog,
)


class OpenLIFUData(ScriptedLoadableModule):
    """Uses ScriptedLoadableModule base class, available at:
    https://github.com/Slicer/Slicer/blob/main/Base/Python/slicer/ScriptedLoadableModule.py
    """

    def __init__(self, parent):
        ScriptedLoadableModule.__init__(self, parent)
        self.parent.title = _("OpenLIFU Data")
        self.parent.categories = [translate("qSlicerAbstractCoreModule", "OpenLIFU.OpenLIFU Modules")]
        self.parent.dependencies = ["OpenLIFUHome"]
        self.parent.contributors = ["Ebrahim Ebrahim (Kitware), Sadhana Ravikumar (Kitware), Peter Hollender (Openwater), Sam Horvath (Kitware), Brad Moore (Kitware)"]
        self.parent.helpText = _(
            "This is the data module of the OpenLIFU extension for focused ultrasound. "
            "More information at <a href=\"https://github.com/OpenwaterHealth/SlicerOpenLIFU\">github.com/OpenwaterHealth/SlicerOpenLIFU</a>."
        )
        self.parent.acknowledgementText = _(
            "This is part of Openwater's OpenLIFU, an open-source "
            "hardware and software platform for Low Intensity Focused Ultrasound (LIFU) research "
            "and development."
        )
        # Embedded as a page of the OpenLIFU host module; hide from the modules menu.
        self.parent.hidden = True
