#-----------------------------------------------------------------------------
set(MODULE_NAME OpenLIFULib)

#-----------------------------------------------------------------------------
set(MODULE_PYTHON_SCRIPTS
  OpenLIFULib/__init__.py
  OpenLIFULib/class_definition_widgets.py
  OpenLIFULib/util.py
  OpenLIFULib/guided_mode_util.py
  OpenLIFULib/user_account_mode_util.py
  OpenLIFULib/lazyimport.py
  OpenLIFULib/parameter_node_utils.py
  OpenLIFULib/session.py
  OpenLIFULib/transducer.py
  OpenLIFULib/targets.py
  OpenLIFULib/simulation.py
  OpenLIFULib/solution.py
  OpenLIFULib/algorithm_input_widget.py
  OpenLIFULib/coordinate_system_utils.py
  OpenLIFULib/photoscan.py
  OpenLIFULib/virtual_fit_results.py
  OpenLIFULib/transform_conversion.py
  OpenLIFULib/transducer_tracking_results.py
  OpenLIFULib/skinseg.py
  OpenLIFULib/transducer_tracking_wizard_utils.py
  OpenLIFULib/events.py
  OpenLIFULib/notifications.py
  OpenLIFULib/volume_thresholding.py
  OpenLIFULib/install_asset_dialog.py
)

set(MODULE_PYTHON_RESOURCES 
  OpenLIFULib/Resources/python-requirements.txt
  )

#-----------------------------------------------------------------------------
slicerMacroBuildScriptedModule(
  NAME ${MODULE_NAME}
  SCRIPTS ${MODULE_PYTHON_SCRIPTS}
  RESOURCES ${MODULE_PYTHON_RESOURCES}
  )