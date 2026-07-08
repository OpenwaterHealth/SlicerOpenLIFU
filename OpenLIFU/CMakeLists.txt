#-----------------------------------------------------------------------------
set(MODULE_NAME OpenLIFU)

#-----------------------------------------------------------------------------
set(MODULE_PYTHON_SCRIPTS
  ${MODULE_NAME}.py
  OpenLIFUApp/__init__.py
  OpenLIFUApp/pages/__init__.py
  OpenLIFUApp/pages/session_page.py
  OpenLIFUApp/pages/preplanning_page.py
  OpenLIFUApp/pages/transducer_localization_page.py
  OpenLIFUApp/pages/sonication_planner_page.py
  OpenLIFUApp/pages/sonication_control_page.py
  OpenLIFUApp/pages/database_page.py
  OpenLIFUApp/pages/login_page.py
  OpenLIFUApp/pages/data_page.py
  OpenLIFUApp/logic/__init__.py
  OpenLIFUApp/logic/app_state.py
  OpenLIFUApp/logic/cloud_sync.py
  )

set(MODULE_PYTHON_RESOURCES
  Resources/UI/${MODULE_NAME}.ui
  Resources/Icons/${MODULE_NAME}.png
  )

#-----------------------------------------------------------------------------
slicerMacroBuildScriptedModule(
  NAME ${MODULE_NAME}
  SCRIPTS ${MODULE_PYTHON_SCRIPTS}
  RESOURCES ${MODULE_PYTHON_RESOURCES}
  WITH_GENERIC_TESTS
  )

#-----------------------------------------------------------------------------
# CLI subprocess used by OpenLIFUApp/logic/cloud_sync.py. Installed to
# ``<lib>/bin/OpenLIFUCloudSyncCLI.py`` (see the subdir's CMakeLists).
add_subdirectory(OpenLIFUCloudSyncEngine)

#-----------------------------------------------------------------------------
if(BUILD_TESTING)

  # Judge generic test pass/fail from unittest output instead of exit code.
  set_tests_properties(py_nomainwindow_qSlicer${MODULE_NAME}ModuleGenericTest PROPERTIES
    PASS_REGULAR_EXPRESSION "Ran [0-9]+ tests? in [0-9]"
    FAIL_REGULAR_EXPRESSION "FAILED"
  )

endif()
