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

#-----------------------------------------------------------------------------
# Download test data from dvc
if(BUILD_TESTING)
    set(DATA_SRC "${CMAKE_CURRENT_SOURCE_DIR}/")
    set(DATA_BIN "${CMAKE_CURRENT_BINARY_DIR}/")

    # Create a custom target to pull dvc data during the build phase
    add_custom_target(dvc_pull_data ALL
        COMMAND ${DVC_EXE} pull
        WORKING_DIRECTORY ${CMAKE_CURRENT_SOURCE_DIR}
        COMMENT "DVC: Pulling test data from Google Drive..."
        VERBATIM
    )

    # This uses 'cmake -E copy_directory' which is platform-independent
    add_custom_target(dvc_copy_data ALL
        COMMAND ${CMAKE_COMMAND} -E copy_directory ${DATA_SRC}/db_dvc_slicertesting ${DATA_BIN}/db_dvc_slicertesting
        DEPENDS dvc_pull_data
        COMMENT "Syncing test data from source to build directory..."
        VERBATIM
    )

endif()