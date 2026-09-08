"""Tools for checking and installing the extension's Python requirements."""

from pathlib import Path
from OpenLIFULib.install_asset_dialog import InstallAssetDialog
import slicer
import importlib
import json
import qt
import re
from urllib.parse import urlsplit
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

def _get_required_openlifu_spec() -> "Optional[tuple[str, Optional[str]]]":
    """Return the version/revision and its Git repository URL, if any."""
    requirements_path = Path(__file__).parent / 'Resources/python-requirements.txt'
    for line in requirements_path.read_text().splitlines():
        line = line.split('#', 1)[0].strip()
        if not line:
            continue
        openlifu_pin = re.fullmatch(r'openlifu(?:\[[^\]]+\])?\s*==\s*(\S+)', line, re.IGNORECASE)
        if openlifu_pin:
            return openlifu_pin.group(1), None
        git_pin = re.fullmatch(
            r'(?:(openlifu(?:\[[^\]]+\])?)\s*@\s*)?git\+(\S+)@([^\s]+)',
            line, re.IGNORECASE,
        )
        if git_pin:
            package, repository, revision = git_pin.groups()
            if package or re.search(r'/openlifu-python(?:\.git)?$', repository, re.IGNORECASE):
                return revision, repository
    return None

def get_required_openlifu_version() -> "Optional[str]":
    """Return the required release version or Git revision for display."""
    spec = _get_required_openlifu_spec()
    return spec[0] if spec is not None else None

def _normalize_git_repository_url(url):
    if not isinstance(url, str):
        return None
    try:
        parsed = urlsplit(url)
        path = parsed.path.rstrip('/')
        if parsed.hostname == 'github.com':
            path = path.lower()
        return parsed.scheme.lower(), parsed.netloc.lower(), path.removesuffix('.git'), parsed.query
    except ValueError:
        return None

def openlifu_version_matches() -> bool:
    """Check the release version or recorded Git installation source.

    Named refs match the ref recorded by pip, without checking its remote tip.
    Commit pins match the installed commit rather than its generated version.
    """
    import importlib.metadata
    try:
        installed = importlib.metadata.distribution('openlifu')
    except importlib.metadata.PackageNotFoundError:
        return False
    spec = _get_required_openlifu_spec()
    if spec is None:
        return True
    required, repository = spec
    if repository is None:
        return installed.version == required or installed.version == required.lstrip('v')

    try:
        source = json.loads(installed.read_text('direct_url.json') or 'null')
    except (OSError, ValueError):
        return False
    if not isinstance(source, dict):
        return False
    if _normalize_git_repository_url(source.get('url')) != _normalize_git_repository_url(repository):
        return False
    vcs_info = source.get('vcs_info')
    if not isinstance(vcs_info, dict) or vcs_info.get('vcs') != 'git':
        return False
    commit_id = vcs_info.get('commit_id')
    if not isinstance(commit_id, str) or not commit_id:
        return False
    if re.fullmatch(r'[0-9a-f]{7,40}', required, re.IGNORECASE):
        return commit_id.lower().startswith(required.lower())
    return vcs_info.get('requested_revision') == required

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
