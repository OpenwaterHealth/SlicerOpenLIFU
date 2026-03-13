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
        import segno
    except ModuleNotFoundError:
        return False
    return importlib.util.find_spec('openlifu') is not None # openlifu import causes a delay so we check for it without actually importing yet

def check_and_install_python_requirements(prompt_if_found = False) -> None:
    """Check whether python requirements are installed and at the required version, and prompt to install/update if not.

    Args:
        prompt_if_found: If this is enabled then in the event that python requirements are found
            and at the correct version, there is a further prompt asking whether to run the install anyway.
    """
    want_install = False
    if not python_requirements_exist():
        want_install = slicer.util.confirmYesNoDisplay(
            text = "Some OpenLIFU python dependencies were not found. Install them now?",
            windowTitle = "Install python dependencies?",
        )
    elif not openlifu_version_matches() and prompt_if_found:
        want_install = slicer.util.confirmYesNoDisplay(
            text = f"The installed openlifu version does not match the required version ({get_required_openlifu_version()}). Update now?",
            windowTitle = "Update openlifu?",
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

def get_required_openlifu_version() -> "Optional[str]":
    """Return the required openlifu version pinned in 
    python-requirements.txt, or None."""
    requirements_path = Path(__file__).parent / 'Resources/python-requirements.txt'
    for line in requirements_path.read_text().splitlines():    
        if line.startswith('openlifu=='):
            return line.split('==', 1)[1].strip()
    return None

def openlifu_version_matches() -> bool:
    """Return True if the installed openlifu version matches
     the required version. Returns False if openlifu is not 
     installed or versions don't match."""
    import importlib.metadata
    try:
        installed = importlib.metadata.version('openlifu')     
        required = get_required_openlifu_version()
        if required is None:
            return True  # No version constraint
        # Handle optional 'v' prefix (e.g. 'v0.18.0' vs '0.18.0')
        return installed == required or installed == required.lstrip('v')
    except importlib.metadata.PackageNotFoundError:
        return False

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
        if slicer.app.testingEnabled():
            if not python_requirements_exist():
                install_python_requirements()
        else:
            check_and_install_python_requirements(prompt_if_found=False)

        with BusyCursor():
            import openlifu

        if slicer.app.testingEnabled():
            # Ensure kwave assets are present (no-op if already installed)
            from openlifu.util.assets import download_and_install_kwave_assets
            download_and_install_kwave_assets()
        elif not check_and_install_kwave_binaries():
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

def segno_lz() -> "segno":
    """Import segno and return the module, checking that it is installed along the way."""
    if "segno" not in sys.modules:
        check_and_install_python_requirements(prompt_if_found=False)
        import segno
    return sys.modules["segno"]