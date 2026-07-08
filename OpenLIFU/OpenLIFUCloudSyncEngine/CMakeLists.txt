# Define the engine script name
set(ENGINE_SCRIPT "OpenLIFUCloudSyncCLI.py")

# We use 'bin' instead of 'cli-modules' to prevent Slicer 
# from trying to parse this script for XML descriptions at startup.
set(DESTINATION_DIR "${Slicer_INSTALL_LIB_DIR}/bin")

# Copy the file during the build process for developer testing
file(COPY ${ENGINE_SCRIPT} 
     DESTINATION "${CMAKE_BINARY_DIR}/${DESTINATION_DIR}")

# Install the file with explicit execution permissions for Linux/macOS.
# COMPONENT RuntimeLibraries is required so that CPack includes this file
# in the packaged application (Slicer only packages RuntimeLibraries,
# RuntimePlugins, and Runtime components).
install(
  FILES ${ENGINE_SCRIPT}
  DESTINATION ${DESTINATION_DIR}
  COMPONENT RuntimeLibraries
  PERMISSIONS
    OWNER_READ OWNER_WRITE OWNER_EXECUTE
    GROUP_READ GROUP_EXECUTE
    WORLD_READ WORLD_EXECUTE
)