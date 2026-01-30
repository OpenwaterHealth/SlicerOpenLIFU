"""Tools for lazy installing and lazy importing of the extension's python requirements"""

from typing import TYPE_CHECKING
from pathlib import Path
from OpenLIFULib.install_asset_dialog import InstallAssetDialog
import slicer
import importlib
import sys
import qt
from OpenLIFULib.util import BusyCursor
if TYPE_CHECKING:
    import openlifu # This import is deferred at runtime, but it is done here for IDE and static analysis purposes
    import xarray

def install_python_requirements() -> None:
    """Install python requirements"""
    requirements_path = Path(__file__).parent / 'Resources/python-requirements.txt'
    with BusyCursor():
        slicer.util.pip_install(['-r', requirements_path])

def python_requirements_exist() -> bool:
    """Check and return whether python requirements are installed."""
    try:
        import threadpoolctl
        import bcrypt
    except ModuleNotFoundError:
        return False
    return importlib.util.find_spec('openlifu') is not None # openlifu import causes a delay so we check for it without actually importing yet

def check_and_install_python_requirements(prompt_if_found = False) -> None:
    """Check whether python requirements are installed and prompt to install them if not.

    Args:
        prompt_if_found: If this is enabled then in the event that python requirements are found,
            there is a further prompt asking whether to run the install anyway.
    """
    want_install = False
    if not python_requirements_exist():
        want_install = slicer.util.confirmYesNoDisplay(
            text = "Some OpenLIFU python dependencies were not found. Install them now?",
            windowTitle = "Install python dependencies?",
        )
    elif prompt_if_found:
        want_install = slicer.util.confirmYesNoDisplay(
            text = "All OpenLIFU python dependencies were found. Re-run the install command?",
            windowTitle = "Reinstall python dependencies?",
        )
    if want_install:
        install_python_requirements()
        if python_requirements_exist():
            slicer.util.infoDisplay(
                text="Python requirements installed. Please restart the application to ensure it takes effect.",
                windowTitle="Success"
            )
        else:
            slicer.util.errorDisplay(
                text="OpenLIFU python dependencies are still not found. The install may have failed.",
                windowTitle="Python dependencies still not found"
            )

def check_and_install_kwave_binaries() -> bool:
    """Check if the kwave binaries are present, and if not then ask the user how they want to install them.
    Returns whether they were successfully installed (or just already present).
    This assumes that openlifu can be imported already, so do not call this function until after that is assured.
    """
    import openlifu
    kwave_paths = openlifu.util.assets.get_kwave_paths()
    if all(p.exists() for p,_ in kwave_paths):
        return True
    
    for install_path, url in kwave_paths:
        if not install_path.exists(): # If the user ever chooses the "download" option, then *all* the kwave assets will be retrieved, so this check prevents asking again for each file.
            install_dialog = InstallAssetDialog(install_path.name, parent = slicer.util.mainWindow())
            if install_dialog.exec_() != qt.QDialog.Accepted:
                return False
            action, path = install_dialog.get_result()
            if action == "download":
                try:
                    openlifu.util.assets.download_and_install_kwave_assets()
                except Exception as e:
                    slicer.util.errorDisplay(
                        text = f"An error occurred while downloading {install_path.name}: {e}",
                        windowTitle = f"Error downloading {install_path.name}"
                    )
                    raise e
            elif action =="browse":
                openlifu.util.assets.install_kwave_asset_from_file(path)
            else:
                raise RuntimeError("Unrecognized dialog action") # should never happen
    return all(p.exists() for p,_ in kwave_paths)

def openlifu_lz() -> "openlifu":
    """Import openlifu and return the module, checking that it is installed along the way."""
    if "openlifu" not in sys.modules:
        # In testing mode, automatically install missing requirements without prompting
        if slicer.app.testingEnabled() and not python_requirements_exist():
            install_python_requirements()
        else:
            check_and_install_python_requirements(prompt_if_found=False)

        with BusyCursor():
            import openlifu

        if not check_and_install_kwave_binaries():
            raise RuntimeError("The openlifu library requires kwave binaries to be installed. There may be issues trying to run simulations.")

    return sys.modules["openlifu"]

def xarray_lz() -> "xarray":
    """Import xarray and return the module, checking that openlifu is installed along the way."""
    if "openlifu" not in sys.modules:
        check_and_install_python_requirements(prompt_if_found=False)
        import xarray
    return sys.modules["xarray"]

def bcrypt_lz() -> "bcrypt":
    """Import bcrypt and return the module, checking that it is installed along the way."""
    if "bcrypt" not in sys.modules:
        check_and_install_python_requirements(prompt_if_found=False)
        with BusyCursor():
            import bcrypt
    return sys.modules["bcrypt"]

def threadpoolctl_lz() -> "threadpoolctl":
    """Import threadpoolctl and return the module, checking that it is installed along the way."""
    if "threadpoolctl" not in sys.modules:
        check_and_install_python_requirements(prompt_if_found=False)
        import threadpoolctl
    return sys.modules["threadpoolctl"]