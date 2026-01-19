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

  # Register the unittest subclass in the main script as a ctest.
  # Note that the test will also be available at runtime.
  set(DVC_DATA_DIR "${CMAKE_CURRENT_BINARY_DIR}/db_dvc_slicertesting")

  slicer_add_python_unittest(SCRIPT ${MODULE_NAME}.py)

  set_tests_properties(py_${MODULE_NAME} PROPERTIES ENVIRONMENT 
    "SLICER_DVC_DATA_DIR=${CMAKE_BINARY_DIR}/db_dvc_slicertesting")

  # Additional build-time testing
  add_subdirectory(Testing)
endif()
