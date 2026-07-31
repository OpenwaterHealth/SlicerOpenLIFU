#-----------------------------------------------------------------------------
set(MODULE_NAME OpenLIFU)

#-----------------------------------------------------------------------------
set(MODULE_PYTHON_SCRIPTS
  ${MODULE_NAME}.py
  OpenLIFUApp/__init__.py
  OpenLIFUApp/plan_finalization.py
  OpenLIFUApp/table_widgets.py
  OpenLIFUApp/dialogs/__init__.py
  OpenLIFUApp/dialogs/session_dialogs.py
  OpenLIFUApp/host/__init__.py
  OpenLIFUApp/host/host_logic.py
  OpenLIFUApp/host/host_test.py
  OpenLIFUApp/host/host_widget.py
  OpenLIFUApp/host/page_registry.py
  OpenLIFUApp/host/timeline_widget.py
  OpenLIFUApp/pages/__init__.py
  OpenLIFUApp/pages/data_manager_page.py
  OpenLIFUApp/pages/home_page.py
  OpenLIFUApp/pages/planning_session_overview_page.py
  OpenLIFUApp/pages/sonication_session_overview_page.py
  OpenLIFUApp/pages/target_selection_page.py
  OpenLIFUApp/logic/__init__.py
  OpenLIFUApp/logic/app_state.py
  OpenLIFUApp/logic/cloud_sync.py
  OpenLIFUApp/logic/database_logic.py
  OpenLIFUApp/logic/session_actions.py
  )

set(MODULE_PYTHON_RESOURCES
  Resources/UI/${MODULE_NAME}.ui
  Resources/UI/OpenLIFUData.ui
  Resources/UI/OpenLIFUDatabase.ui
  Resources/UI/OpenLIFUHome.ui
  Resources/UI/OpenLIFULogin.ui
  Resources/UI/OpenLIFUPrePlanning.ui
  Resources/UI/OpenLIFUSession.ui
  Resources/UI/OpenLIFUSonicationControl.ui
  Resources/UI/OpenLIFUSonicationPlanner.ui
  Resources/UI/OpenLIFUTransducerLocalization.ui
  Resources/UI/TransducerLocalizationWizard.ui
  Resources/Icons/${MODULE_NAME}.png
  Resources/Icons/device.png
  Resources/Icons/qrcode.png
  Resources/Icons/sync.png
  Resources/Icons/db.png
  Resources/Icons/db-cloud.png
  Resources/Icons/db-cloud-broken.png
  Resources/Icons/user.png
  Resources/Icons/user-admin.png
  Resources/Icons/save.png
  Resources/Icons/exit.png
  Resources/openlifu-database/empty_db/protocols/protocols.json
  Resources/openlifu-database/empty_db/subjects/subjects.json
  Resources/openlifu-database/empty_db/transducers/transducers.json
  Resources/openlifu-database/empty_db/users/users.json
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

  # Full end-to-end workflow integration test. Round 5c-3 Commit D folded
  # the former ``py_OpenLIFUHome`` ctest into this host-owned ``py_OpenLIFU``
  # target (see OpenLIFUTest in ${MODULE_NAME}.py). DVC env vars are read
  # from CACHE variables set at the top-level CMakeLists when BUILD_TESTING
  # is on.
  slicer_add_python_unittest(SCRIPT ${MODULE_NAME}.py)

  set_tests_properties(py_${MODULE_NAME} PROPERTIES
    ENVIRONMENT "GDRIVE_CREDENTIALS_DATA=${DVC_GDRIVE_JSON_CONTENT};DVC_REPO_DIR=${DVC_REPO_DIR}"
    # Use output matching instead of exit code, because vtkDebugLeaks
    # reports OpenGL rendering object leaks at shutdown (issue #463)
    # causing a non-zero exit even when all tests pass.
    PASS_REGULAR_EXPRESSION "Ran [0-9]+ tests? in [0-9]"
    FAIL_REGULAR_EXPRESSION "FAILED"
  )

endif()
