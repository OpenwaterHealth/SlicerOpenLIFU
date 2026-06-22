"""Tools for checking and installing the extension's Python requirements."""

from pathlib import Path
from OpenLIFULib.install_asset_dialog import InstallAssetDialog
import slicer
import importlib
import qt
import re
from OpenLIFULib.util import BusyCursor

_openlifu_version_mismatch_warning_shown = False

def install_python_requirements() -> None:
    """Install python requirements"""
    requirements_path = Path(__file__).parent / 'Resources/python-requirements.txt'
    with BusyCursor():
        slicer.util.pip_install(['-r', requirements_path])

def python_requirements_exist() -> bool:
    """Check and return whether python requirements are installed."""
    try:
        import bcrypt  # noqa: F401
        import requests  # noqa: F401
        import segno  # noqa: F401
        import threadpoolctl  # noqa: F401
        import xarray  # noqa: F401
    except ModuleNotFoundError:
        return False
    # These imports can cause a delay, so check for them without importing.
    return (
        importlib.util.find_spec('openlifu') is not None
        and importlib.util.find_spec('openlifu_sdk') is not None
        and importlib.util.find_spec('openlifu_sdk.ui.simulated_interface') is not None
    )

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

def ensure_python_requirements_for_module_enter() -> bool:
    """Check/install Python requirements when a module is entered.

    Returns True when requirements are available after the check. In testing mode,
    missing requirements are installed automatically. In interactive mode, users are
    prompted to install missing requirements. Version mismatches are only warned
    about once per application session.
    """
    global _openlifu_version_mismatch_warning_shown

    if slicer.app.testingEnabled():
        if not python_requirements_exist():
            install_python_requirements()
        return python_requirements_exist()

    check_and_install_python_requirements(prompt_if_found=False)
    requirements_exist = python_requirements_exist()
    if not requirements_exist:
        return False

    if not openlifu_version_matches() and not _openlifu_version_mismatch_warning_shown:
        required = get_required_openlifu_version() or "unknown"
        try:
            import importlib.metadata
            installed = importlib.metadata.version('openlifu')
        except importlib.metadata.PackageNotFoundError:
            installed = "unknown"
        slicer.util.warningDisplay(
            text=(
                f"The installed openlifu version ({installed}) does not match "
                f"the required version ({required}). Use the Login module's "
                "Python requirements button to update it."
            ),
            windowTitle="OpenLIFU version mismatch",
        )
        _openlifu_version_mismatch_warning_shown = True

    return True

def get_required_openlifu_version() -> "Optional[str]":
    """Return the required openlifu version pinned in
    python-requirements.txt, or None."""
    requirements_path = Path(__file__).parent / 'Resources/python-requirements.txt'
    for line in requirements_path.read_text().splitlines():
        line = line.split('#', 1)[0].strip()
        if not line:
            continue
        openlifu_pin = re.match(r'^openlifu(?:\[[^\]]+\])?\s*==\s*(\S+)$', line)
        if openlifu_pin:
            return openlifu_pin.group(1).strip()
        if 'OpenLIFU-python.git@' in line:
            commit_hash = line.split('@')[-1].strip()
            return f"dev+g{commit_hash[:9]}"
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
        if 'dev' in required:
            if '+g' not in installed:
                return False
            required_hash = required.split('+g')[-1]
            installed_hash = installed.split('+g')[-1]
            return required_hash.startswith(installed_hash) or installed_hash.startswith(required_hash)
        else:
            # Handle optional 'v' prefix (e.g. 'v0.18.0' vs '0.18.0')
            return installed == required or installed == required.lstrip('v')
    except importlib.metadata.PackageNotFoundError:
        return False

def kwave_binaries_exist() -> bool:
    """Return True if all of openlifu's kwave binaries/assets are already
    installed. Returns False (without prompting) if any are missing or if
    openlifu is not importable yet.
    """
    try:
        from openlifu.util.assets import get_kwave_paths
    except ImportError:
        return False
    return all(p.exists() for p, _ in get_kwave_paths())

def check_and_install_kwave_binaries() -> bool:
    """Check if the kwave binaries are present, and if not then ask the user how they want to install them.
    Returns whether they were successfully installed (or just already present).
    This assumes that openlifu can be imported already, so do not call this function until after that is assured.
    """
    import openlifu.util.assets

    if slicer.app.testingEnabled():
        openlifu.util.assets.download_and_install_kwave_assets()
        return True

    from openlifu.util.assets import get_kwave_paths
    kwave_paths = get_kwave_paths()
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
