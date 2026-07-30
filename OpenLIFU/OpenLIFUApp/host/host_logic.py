"""Logic class for the OpenLIFU host module.

Owns the guided-workflow state, the host-level ``OpenLIFUAppState``
parameter node, and one instance of each active page's ``*Logic``
class. Everything else in the module reaches through here for shared
state (e.g. ``get_cur_db()`` walks
``getModuleLogic("OpenLIFU").database_logic.db``).

Split out of the original monolithic ``OpenLIFU.py`` per the
size-ceiling rule in ``docs/coding-standards.md``.
"""

from __future__ import annotations

from typing import Optional

import slicer
from slicer.ScriptedLoadableModule import ScriptedLoadableModuleLogic

from OpenLIFULib.guided_mode_util import (
    set_guided_mode_state,
    Workflow,
)

from OpenLIFUApp.logic.app_state import OpenLIFUAppState
from OpenLIFUApp.logic.database_logic import DatabaseLogic


class OpenLIFUHostLogic(ScriptedLoadableModuleLogic):
    """Host module's Logic class.

    Owns:

    * The guided-workflow state (``self.workflow``).
    * A cached wrapper around the MRML parameter node backing
      ``OpenLIFUAppState``.
    * One instance of each active page's Logic class:
      ``home_logic``, ``data_manager_logic``, ``database_logic``.

    Session-split refactor (SlicerOpenLIFU#631): legacy page-logic
    classes (data_logic / session_logic / preplanning_logic /
    transducer_localization_logic / sonication_planner_logic /
    sonication_control_logic / login_logic) are not instantiated during
    the transition. The code that consumed them lives in
    ``OpenLIFUApp/pages_legacy/`` and is not embedded.
    """

    def __init__(self) -> None:
        ScriptedLoadableModuleLogic.__init__(self)
        self.workflow = Workflow()
        self._app_state_cache: Optional[OpenLIFUAppState] = None
        self._app_state_cache_node = None

        # Local imports here avoid load-time circulars: each page module
        # imports symbols from OpenLIFULib and other pages, and this file is
        # imported by ``OpenLIFU.py`` (which Slicer imports before
        # ``OpenLIFUApp`` is on sys.path).
        from OpenLIFUApp.pages.home_page import OpenLIFUHomeLogic
        from OpenLIFUApp.pages.data_manager_page import OpenLIFUDataManagerLogic

        self.home_logic = OpenLIFUHomeLogic()
        self.data_manager_logic = OpenLIFUDataManagerLogic()
        self.database_logic = DatabaseLogic()

    def getParameterNode(self):
        """Return the OpenLIFU app-state wrapper (cached).

        Construction of :class:`OpenLIFUAppState` runs
        ``parameterNodeWrapper._initMethod``, which writes defaults for any
        unset fields. Each write fires ``ModifiedEvent`` on the underlying
        MRML node; observers (page ``onDataParameterNodeModified`` handlers,
        the ``AppStateSignals.dataChanged`` re-emit lambda) can call
        ``get_app_state()`` again before the wrapper is fully constructed.
        We batch the default-writes inside ``NodeModify`` so a single
        ``ModifiedEvent`` fires *after* construction finishes and after the
        cache is populated -- observers then get the fully-initialised
        wrapper and never re-enter ``OpenLIFUAppState(...)``.
        """
        mrml_node = super().getParameterNode()
        if self._app_state_cache is None or self._app_state_cache_node is not mrml_node:
            with slicer.util.NodeModify(mrml_node):
                wrapper = OpenLIFUAppState(mrml_node)
            self._app_state_cache = wrapper
            self._app_state_cache_node = mrml_node
        return self._app_state_cache

    # ------------------------------------------------------------------
    # Guided-workflow entry points
    # ------------------------------------------------------------------

    def start_guided_mode(self) -> None:
        """Enter guided mode and jump the user to the workflow start."""
        set_guided_mode_state(True)
        self.workflow_go_to_start()

    def workflow_jump_ahead(self) -> None:
        """Jump ahead in the guided workflow to the furthest reachable step."""
        slicer.util.selectModule(self.workflow.furthest_module_to_which_can_proceed())

    def workflow_go_to_start(self) -> None:
        """Navigate to the starting module of the workflow."""
        slicer.util.selectModule(self.workflow.starting_module())
