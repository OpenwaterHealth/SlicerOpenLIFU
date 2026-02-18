#-----------------------------------------------------------------------------
set(MODULE_NAME OpenLIFUHome)

#-----------------------------------------------------------------------------
set(MODULE_PYTHON_SCRIPTS
  ${MODULE_NAME}.py
  )

set(MODULE_PYTHON_RESOURCES
  Resources/Icons/${MODULE_NAME}.png
  Resources/UI/${MODULE_NAME}.ui
  )

#-----------------------------------------------------------------------------
slicerMacroBuildScriptedModule(
  NAME ${MODULE_NAME}
  SCRIPTS ${MODULE_PYTHON_SCRIPTS}
  RESOURCES ${MODULE_PYTHON_RESOURCES}
  WITH_GENERIC_TESTS
  )

#-----------------------------------------------------------------------------
if(BUILD_TESTING)

  # Judge generic test pass/fail from unittest output instead of exit code.
  set_tests_properties(py_nomainwindow_qSlicer${MODULE_NAME}ModuleGenericTest PROPERTIES
    PASS_REGULAR_EXPRESSION "Ran [0-9]+ tests? in [0-9]"
    FAIL_REGULAR_EXPRESSION "FAILED"
  )

  # Register the unittest subclass in the main script as a ctest.
  # Note that the test will also be available at runtime.
  slicer_add_python_unittest(SCRIPT ${MODULE_NAME}.py)

  set_tests_properties(py_${MODULE_NAME} PROPERTIES
    ENVIRONMENT "GDRIVE_CREDENTIALS_DATA=${DVC_GDRIVE_JSON_CONTENT};DVC_REPO_DIR=${DVC_REPO_DIR}"
    # Use output matching instead of exit code, because vtkDebugLeaks
    # reports OpenGL rendering object leaks at shutdown (issue #463)
    # causing a non-zero exit even when all tests pass.
    PASS_REGULAR_EXPRESSION "Ran [0-9]+ tests? in [0-9]"
    FAIL_REGULAR_EXPRESSION "FAILED"
  )

  # Additional build-time testing
  add_subdirectory(Testing)
endif()