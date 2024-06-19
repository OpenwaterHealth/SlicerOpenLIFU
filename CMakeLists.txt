cmake_minimum_required(VERSION 3.16.3...3.19.7 FATAL_ERROR)

project(SlicerOpenLIFU)

#-----------------------------------------------------------------------------
# Extension meta-information
set(EXTENSION_HOMEPAGE "https://www.slicer.org/wiki/Documentation/Nightly/Extensions/SlicerOpenLIFU")
set(EXTENSION_CONTRIBUTORS "Ebrahim Ebrahim (Kitware), Sam Horvath (Kitware), Brad Moore (Kitware)")
set(EXTENSION_DESCRIPTION "An extension for Openwater's Open-LIFU project")
set(EXTENSION_ICONURL "https://www.example.com/Slicer/Extensions/SlicerOpenLIFU.png")
set(EXTENSION_SCREENSHOTURLS "https://www.example.com/Slicer/Extensions/SlicerOpenLIFU/Screenshots/1.png")
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
