
#slicer_add_python_unittest(SCRIPT ${MODULE_NAME}ModuleTest.py)
slicer_add_python_unittest(SCRIPT TransducerManagerTest.py)

add_test(NAME py_TransducerStore
  COMMAND ${PYTHON_EXECUTABLE} -m pytest -q ${CMAKE_CURRENT_SOURCE_DIR}/test_transducer_store.py)
set_tests_properties(py_TransducerStore PROPERTIES
  ENVIRONMENT "PYTHONPATH=${CMAKE_CURRENT_SOURCE_DIR}/../.."
)
set_tests_properties(py_TransducerManagerTest py_TransducerStore PROPERTIES
  LABELS "transducer-manager"
)
