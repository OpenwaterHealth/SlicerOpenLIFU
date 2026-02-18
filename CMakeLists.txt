cmake_minimum_required(VERSION 3.16.3...3.19.7 FATAL_ERROR)

project(OpenLIFU)

#-----------------------------------------------------------------------------
# Extension meta-information
set(EXTENSION_HOMEPAGE "https://github.com/OpenwaterHealth/SlicerOpenLIFU")
set(EXTENSION_CONTRIBUTORS "Ebrahim Ebrahim (Kitware), Peter Hollender (Openwater), Sam Horvath (Kitware), Andrew Howe (Kitware), Sadhana Ravikumar (Kitware), Brad Moore (Kitware)")
set(EXTENSION_DESCRIPTION "A 3D Slicer extension for Openwater’s OpenLIFU (Low Intensity Focused Ultrasound) research platform. Licensed under AGPL (a strong copyleft license that may impose restrictions on combined works).")
set(EXTENSION_ICONURL "https://github.com/OpenwaterHealth/SlicerOpenLIFU/raw/main/SlicerOpenLIFU.png")
set(EXTENSION_SCREENSHOTURLS "https://github.com/OpenwaterHealth/SlicerOpenLIFU/raw/main/screenshots/1.png")
set(EXTENSION_DEPENDS "NA") # Specified as a list or "NA" if no dependencies

#-----------------------------------------------------------------------------
# Extension dependencies
find_package(Slicer REQUIRED)
include(${Slicer_USE_FILE})

#-----------------------------------------------------------------------------
# DVC configuration for running integration tests
if(BUILD_TESTING)

    set(DVC_GDRIVE_KEY_PATH "" CACHE FILEPATH "Path to GDrive Service Account JSON key for DVC")

    if(NOT EXISTS "${DVC_GDRIVE_KEY_PATH}")
        message(FATAL_ERROR 
            "DVC_GDRIVE_KEY_PATH is not set. Testing is enabled, "
            "the path to the Google Drive Service Account JSON key must be provided."
            )
    endif()

    if(EXISTS "${DVC_GDRIVE_KEY_PATH}")
        file(READ "${DVC_GDRIVE_KEY_PATH}" RAW_CONTENT)
        string(REPLACE "\n" "" STRIPPED_CONTENT "${RAW_CONTENT}")
        set(DVC_GDRIVE_JSON_CONTENT "${STRIPPED_CONTENT}" CACHE INTERNAL "DVC JSON Key Content")
    endif()
    
    # Specify directory containing .dvc files
    set(DVC_REPO_DIR "${CMAKE_SOURCE_DIR}" CACHE INTERNAL "DVC data folder")
endif()

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
add_subdirectory(OpenLIFUTransducerLocalization)
add_subdirectory(OpenLIFULogin)
## NEXT_MODULE

#-----------------------------------------------------------------------------
include(${Slicer_EXTENSION_GENERATE_CONFIG})
include(${Slicer_EXTENSION_CPACK})