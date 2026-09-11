
foreach(scenario automatic-sonication automatic-cloud automatic-all recreate home sonication cloud wizard wizard-shutdown transducer-preview-shutdown)
  slicer_add_python_test(
    SCRIPT OpenLIFUTeardownTest.py
    TESTNAME_PREFIX "${scenario}_"
    SLICER_ARGS --disable-settings --ignore-slicerrc
    SCRIPT_ARGS ${scenario}
  )
  set_tests_properties(py_${scenario}_OpenLIFUTeardownTest PROPERTIES
    TIMEOUT 90
    FAIL_REGULAR_EXPRESSION "Traceback;Segmentation fault;vtkDebugLeaks has detected LEAKS;TEARDOWN FAILED"
  )
endforeach()

set_tests_properties(py_transducer-preview-shutdown_OpenLIFUTeardownTest PROPERTIES
  LABELS "transducer-manager"
)
