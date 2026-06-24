#-----------------------------------------------------------------------------
set(MODULE_NAME OpenLIFU)

#-----------------------------------------------------------------------------
set(MODULE_PYTHON_SCRIPTS
  ${MODULE_NAME}.py
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
if(BUILD_TESTING)

  # Judge generic test pass/fail from unittest output instead of exit code.
  set_tests_properties(py_nomainwindow_qSlicer${MODULE_NAME}ModuleGenericTest PROPERTIES
    PASS_REGULAR_EXPRESSION "Ran [0-9]+ tests? in [0-9]"
    FAIL_REGULAR_EXPRESSION "FAILED"
  )

endif()
