cmake_minimum_required(VERSION 3.16.3...3.19.7 FATAL_ERROR)

project(SlicerOpenLIFU)

#-----------------------------------------------------------------------------
# Extension meta-information
set(EXTENSION_HOMEPAGE "https://github.com/OpenwaterHealth/SlicerOpenLIFU")
set(EXTENSION_CONTRIBUTORS "Ebrahim Ebrahim (Kitware), Peter Hollender (Openwater), Sam Horvath (Kitware), Andrew Howe (Kitware), Sadhana Ravikumar (Kitware), Brad Moore (Kitware)")
set(EXTENSION_DESCRIPTION "A 3D Slicer extension for Openwater’s OpenLIFU (Low Intensity Focused Ultrasound) research platform. Licensed under AGPL (a strong copyleft license that may impose restrictions on combined works).")
set(EXTENSION_ICONURL "https://github.com/OpenwaterHealth/SlicerOpenLIFU/blob/main/SlicerOpenLIFU.png")
set(EXTENSION_SCREENSHOTURLS "https://github.com/OpenwaterHealth/SlicerOpenLIFU/blob/main/screenshots/1.png")
set(EXTENSION_DEPENDS "NA") # Specified as a list or "NA" if no dependencies

#-----------------------------------------------------------------------------
# Extension dependencies
find_package(Slicer REQUIRED)
include(${Slicer_USE_FILE})

#-----------------------------------------------------------------------------
# Extension modules
add_subdirectory(OpenLIFUHome)
add_subdirectory(OpenLIFULib)
add_subdirectory(OpenLIFUDatabase)
add_subdirectory(OpenLIFUData)
add_subdirectory(OpenLIFUPrePlanning)
add_subdirectory(OpenLIFUSonicationControl)
add_subdirectory(OpenLIFUProtocolConfig)
add_subdirectory(OpenLIFUSonicationPlanner)
add_subdirectory(OpenLIFUTransducerTracker)
add_subdirectory(OpenLIFULogin)
## NEXT_MODULE

#-----------------------------------------------------------------------------
include(${Slicer_EXTENSION_GENERATE_CONFIG})
include(${Slicer_EXTENSION_CPACK})
