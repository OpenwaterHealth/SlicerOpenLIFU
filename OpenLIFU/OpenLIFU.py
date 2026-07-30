"""OpenLIFU host module -- Slicer module entry point.

Slicer's module discovery expects four names at the top of this file:

* ``OpenLIFU``       -- the ``ScriptedLoadableModule`` registration class.
* ``OpenLIFUWidget`` -- the ``ScriptedLoadableModuleWidget`` implementation.
* ``OpenLIFULogic``  -- the ``ScriptedLoadableModuleLogic`` implementation.
* ``OpenLIFUTest``   -- the ``ScriptedLoadableModuleTest`` implementation.

Everything they do is implemented in the ``OpenLIFUApp/host/``
subpackage; this file is a thin re-export shim.

Structure of the host subpackage:

* ``OpenLIFUApp/host/page_registry.py``  -- ``Page``, ``PAGE_DEFS``
* ``OpenLIFUApp/host/timeline_widget.py`` -- ``TimelineWidget``
* ``OpenLIFUApp/host/host_widget.py``    -- ``OpenLIFUHostWidget``
* ``OpenLIFUApp/host/host_logic.py``     -- ``OpenLIFUHostLogic``
* ``OpenLIFUApp/host/host_test.py``      -- ``OpenLIFUHostTest``

See ``docs/architecture.md`` for the design rationale.
"""

from __future__ import annotations

from slicer.ScriptedLoadableModule import ScriptedLoadableModule
from slicer.i18n import tr as _
from slicer.i18n import translate

# Re-exports for Slicer's module-discovery machinery. Slicer looks up
# ``<ModuleName>Widget`` / ``Logic`` / ``Test`` via ``getattr(module, ...)``,
# so the aliases below (renaming ``OpenLIFUHost*`` to ``OpenLIFU*``) satisfy
# that convention without duplicating class implementations.
from OpenLIFUApp.host.host_logic import OpenLIFUHostLogic as OpenLIFULogic
from OpenLIFUApp.host.host_test import OpenLIFUHostTest as OpenLIFUTest
from OpenLIFUApp.host.host_widget import OpenLIFUHostWidget as OpenLIFUWidget

# Preserve public names Slicer's static analysers may look up on the module.
__all__ = ["OpenLIFU", "OpenLIFUWidget", "OpenLIFULogic", "OpenLIFUTest"]


class OpenLIFU(ScriptedLoadableModule):
    """Single-page host module that embeds the OpenLIFU workflow pages as
    stacked pages with one shared header (DB / Login / Device / Save /
    Exit) and one footer timeline."""

    def __init__(self, parent):
        ScriptedLoadableModule.__init__(self, parent)
        self.parent.title = _("OpenLIFU")
        self.parent.categories = [translate("qSlicerAbstractCoreModule", "OpenLIFU")]
        # The host now owns every Widget/Logic/Test class; no cross-module
        # Slicer dependencies remain.
        self.parent.dependencies = []
        self.parent.contributors = [
            "Peter Hollender (Openwater), Ebrahim Ebrahim (Kitware)",
        ]
        self.parent.helpText = _(
            "Unified entry point for the OpenLIFU extension. Hosts the full "
            "treatment-planning workflow as a single module with stacked pages."
        )
        self.parent.acknowledgementText = _(
            "This is part of Openwater's OpenLIFU, an open-source hardware "
            "and software platform for Low Intensity Focused Ultrasound "
            "(LIFU) research and development."
        )
