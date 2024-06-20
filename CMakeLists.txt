cmake_minimum_required(VERSION 3.16.3...3.19.7 FATAL_ERROR)

project(SlicerOpenLIFU)

#-----------------------------------------------------------------------------
# Extension meta-information
set(EXTENSION_HOMEPAGE "https://github.com/OpenwaterHealth/SlicerOpenLIFU")
set(EXTENSION_CONTRIBUTORS "Ebrahim Ebrahim (Kitware), , Peter Hollender (Openwater), Sam Horvath (Kitware), Brad Moore (Kitware)")
set(EXTENSION_DESCRIPTION "An extension for Openwater's Open-LIFU project")
set(EXTENSION_ICONURL "https://github.com/OpenwaterHealth/SlicerOpenLIFU/blob/main/SlicerOpenLIFU.png")
set(EXTENSION_SCREENSHOTURLS "https://github.com/OpenwaterHealth/SlicerOpenLIFU/blob/main/Screenshots/1.png")
set(EXTENSION_DEPENDS "NA") # Specified as a list or "NA" if no dependencies

#-----------------------------------------------------------------------------
# Extension dependencies
find_package(Slicer REQUIRED)
include(${Slicer_USE_FILE})

#-----------------------------------------------------------------------------
# Extension modules
add_subdirectory(OpenLIFUHome)
## NEXT_MODULE

#-----------------------------------------------------------------------------
include(${Slicer_EXTENSION_GENERATE_CONFIG})
include(${Slicer_EXTENSION_CPACK})
