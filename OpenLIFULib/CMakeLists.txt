#-----------------------------------------------------------------------------
set(MODULE_NAME OpenLIFULib)

#-----------------------------------------------------------------------------
set(MODULE_PYTHON_SCRIPTS
  OpenLIFULib/__init__.py
  OpenLIFULib/busycursor.py
  OpenLIFULib/lazyimport.py
  OpenLIFULib/parameter_node_utils.py
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