# AGENTS.md

This file provides guidance for AI coding agents and other automated development assistants working with code in this repository.

## Project Overview

SlicerOpenLIFU is a 3D Slicer extension for Openwater's OpenLIFU (Low Intensity Focused Ultrasound) research platform. It provides a GUI for focused ultrasound treatment planning, simulation, and hardware control. Licensed under AGPL.

The extension depends on the `openlifu` Python library from the [`openlifu-python`](https://github.com/OpenwaterHealth/openlifu-python) repository (formerly named `OpenLIFU-python`), which provides the core computational engine (beamforming, simulation via k-Wave, data model classes). SlicerOpenLIFU wraps `openlifu` objects with Slicer UI, visualization, and persistence.

## Build and Test Commands

### Building

The extension is built against a 3D Slicer superbuild. Let `<slicer-superbuild>` denote the superbuild directory. Key paths within it:
- Slicer build: `<slicer-superbuild>/Slicer-build/`
- Slicer's bundled Python: `<slicer-superbuild>/python-install/bin/PythonSlicer`
- Run Python in Slicer env: `<slicer-superbuild>/Slicer-build/Slicer --python-code "..." --no-splash --no-main-window --exit-after-startup`

```bash
# Configure (from the repo root)
cmake -DSlicer_DIR=<slicer-superbuild>/Slicer-build \
      -DBUILD_TESTING=ON \
      -DDVC_GDRIVE_KEY_PATH=/path/to/gdrive-service-account.json \
      -B build

# Build
cmake --build build
```

`BUILD_TESTING=ON` requires `DVC_GDRIVE_KEY_PATH` pointing to a Google Drive service account JSON key (used for downloading test data via DVC).

### Running Slicer with the Extension

```bash
<slicer-superbuild>/Slicer-build/Slicer
```

The extension modules are automatically loaded from the build directory.

### Running Tests

Tests are integration tests that run inside Slicer's environment using CTest. They use Slicer's `ScriptedLoadableModuleTest` framework (not pytest).

```bash
# Run all tests
cd build && ctest --verbose

# Run a single module's test
cd build && ctest -R py_OpenLIFUHome --verbose

# Run just one module test (e.g. sonication planner)
cd build && ctest -R py_OpenLIFUSonicationPlanner --verbose
```

Test names follow the pattern `py_<ModuleName>`. The main integration test is `py_OpenLIFUHome`, which orchestrates a full workflow through all modules sequentially.

Test data is downloaded at runtime from Google Drive via DVC. The CMake config passes `GDRIVE_CREDENTIALS_DATA` and `DVC_REPO_DIR` as environment variables to CTest.

### Python Version

Slicer 5.10 embeds Python 3.12. This means PEP 701 f-string syntax (nested quotes) is valid, but the project has not yet adopted it widely.

### No Linter/Formatter Configuration

There is currently no configured linter or formatter (no flake8, ruff, black, or pre-commit setup).

## Architecture

### Module Structure

The extension has 9 feature modules plus a shared library, all as Slicer scripted (Python) modules:

| Module | Purpose |
|--------|---------|
| **OpenLIFUHome** | Entry point, guided workflow orchestration, python dependency installation |
| **OpenLIFUDatabase** | Connect to/create local file-based openlifu databases |
| **OpenLIFULogin** | User authentication, role-based access (operator/admin) |
| **OpenLIFUData** | Load subjects, sessions, protocols, transducers, volumes |
| **OpenLIFUPrePlanning** | Target placement, skin segmentation, virtual fit validation |
| **OpenLIFUTransducerLocalization** | Photogrammetry-based transducer tracking |
| **OpenLIFUSonicationPlanner** | Solution computation (beamforming + k-Wave simulation) |
| **OpenLIFUSonicationControl** | Hardware interface, run execution and recording |
| **OpenLIFULib** | Shared utility library (not a UI module) |

### Workflow Data Flow

```
Home → Database → Login → Data → PrePlanning → TransducerLocalization → SonicationPlanner → SonicationControl
                                       ↕
                                 ProtocolConfig
```

Each module can operate standalone, but in guided mode they follow this sequence.

### Standard Module Pattern (Slicer Convention)

Every module Python file contains four classes:

```python
class OpenLIFU<Name>(ScriptedLoadableModule):           # Module registration/metadata
class OpenLIFU<Name>ParameterNode(@parameterNodeWrapper): # Persistent state via MRML
class OpenLIFU<Name>Widget(ScriptedLoadableModuleWidget): # Qt UI
class OpenLIFU<Name>Logic(ScriptedLoadableModuleLogic):   # Business logic
class OpenLIFU<Name>Test(ScriptedLoadableModuleTest):     # Integration test
```

UI is defined in `Resources/UI/<ModuleName>.ui` (Qt Designer XML) and loaded in the Widget class.

### OpenLIFULib Key Patterns

**Lazy importing** (`lazyimport.py`): The `openlifu` library and its dependencies are heavy and may not be installed yet. All imports go through lazy-import functions:
- `openlifu_lz()` — returns the `openlifu` module, installing it first if needed
- `xarray_lz()`, `bcrypt_lz()`, `threadpoolctl_lz()` — same pattern
- For IDE type-checking, real imports are guarded under `if TYPE_CHECKING:`

**Parameter node wrappers** (`parameter_node_utils.py`): Thin wrapper classes (`SlicerOpenLIFUProtocol`, `SlicerOpenLIFUTransducer`, `SlicerOpenLIFUSession`, etc.) exist solely to enable Slicer's `@parameterNodeWrapper` serialization of `openlifu` types without importing `openlifu` at module load time. Each wrapper has a corresponding `@parameterNodeSerializer` class that serializes to/from JSON via the wrapped object's `to_json()`/`from_json()` methods.

**`@parameterNodeWrapper` internals**: The wrapper stores each parameter via `_CachedParameterWrapper` or `_ParameterWrapper`. `_CachedParameterWrapper` adds its own `ModifiedEvent` observer on the MRML node to invalidate its cache when the node changes. This means the MRML parameter node can have multiple `ModifiedEvent` observers from different subsystems (connectGui, cached params, cross-module observers). Never blindly remove all `ModifiedEvent` observers from a parameter MRML node.

**`SlicerOpenLIFUTransducer`** (`transducer.py`): A `@parameterPack` with fields `name`, `transducer` (a `SlicerOpenLIFUTransducerWrapper`), `model_node`, `transform_node`, etc. Access the underlying `openlifu.Transducer` via `slicer_transducer.transducer.transducer` (three levels: parameterPack → thin wrapper → openlifu object).

**Cross-module communication**: Modules access each other's state through:
- `get_openlifu_data_parameter_node()` / `get_openlifu_database_parameter_node()` — access parameter nodes from other modules
- `get_cur_db()` — get the currently loaded `openlifu.db.Database`
- Callback registration: `logic.call_on_db_changed(callback)`, `logic.call_on_active_user_changed(callback)`
- VTK observers for MRML scene changes

**Guided workflow** (`guided_mode_util.py`): `GuidedWorkflowMixin` provides Back/Next/Jump navigation. Modules implement this mixin to participate in the guided workflow.

**User account mode** (`user_account_mode_util.py`): Widgets can be tagged with `slicer.openlifu.allowed-roles` Qt property. The Login module enforces visibility based on the current user's role.

### Relationship to openlifu Library

`openlifu` (installed as the `openlifu` Python package from [`openlifu-python`](https://github.com/OpenwaterHealth/openlifu-python)) provides:
- **Data model**: `Protocol`, `Transducer`, `Point`, `Session`, `Solution`, `Run`, `SolutionAnalysis`, `Photoscan`
- **Database**: File-based JSON database (`openlifu.db.Database`)
- **Beamforming**: Delay/apodization calculation (`openlifu.bf`)
- **Simulation**: k-Wave acoustic simulation (`openlifu.sim`)
- **Hardware I/O**: `openlifu.io.LIFUInterface` for device communication
- **Virtual fit**: `openlifu.vf` for transducer virtual fitting

SlicerOpenLIFU wraps these with Slicer MRML nodes, VTK visualization, Qt widgets, and parameter node persistence. The wrapper classes in `parameter_node_utils.py` bridge between openlifu's data classes and Slicer's parameter node system.

The pinned version is in `OpenLIFULib/OpenLIFULib/Resources/python-requirements.txt`.

### Testing Architecture

Tests are integration tests embedded in each module's main `.py` file as a `Test` class. `OpenLIFUHomeTest.runTest()` is the master test that:
1. Installs Python requirements if missing
2. Downloads test database from Google Drive via DVC
3. Calls each module's test methods sequentially (database → data → preplanning → localization → planning → control)

Individual module tests create state needed by subsequent tests — they are not independent.

## Development Gotchas

### Adding new pip dependencies
Adding a package to `python-requirements.txt` is not enough. You must also add an `import` check for it in `python_requirements_exist()` in `lazyimport.py`. Otherwise, users who already have the other deps installed will never trigger a reinstall, and the new package won't be found. Follow the existing `bcrypt`/`threadpoolctl` pattern.

### Updating the pinned openlifu version
When updating the pinned `openlifu` version in `OpenLIFULib/OpenLIFULib/Resources/python-requirements.txt`, consider whether the sample database tags in `OpenLIFULib/OpenLIFULib/sample_data.py` need to be updated. Remind about this.

### Qt signal slot signatures
Qt's `clicked` signal always passes a `checked: bool` argument. Button slot handlers must accept it: `def on_foo_clicked(self, checked: bool)`. Omitting it causes a "takes 1 positional argument but 2 were given" error at runtime.

### Displaying dynamic images in Qt
Generate PNG bytes in memory (`io.BytesIO`), then `qt.QPixmap().loadFromData(buf.getvalue())` → `QLabel.setPixmap()`. No temp files needed. Pillow is available in Slicer's bundled Python. `QPixmap.loadFromData()` accepts raw Python `bytes` directly.

### Adding icon resources to scripted modules
Three steps: (1) place PNG in `<Module>/Resources/Icons/`, (2) list it in `MODULE_PYTHON_RESOURCES` in that module's `CMakeLists.txt`, (3) load at runtime via `self.resourcePath("Icons/foo.png")`. Missing step 2 means the file exists in source but won't be copied to the build tree.

### Reactive widget enable/disable pattern
`@parameterNodeWrapper` fires a generic `vtkCommand.ModifiedEvent` for any field change — no per-field signals. Modules observe it via `self.addObserver(get_openlifu_data_parameter_node().parameterNode, vtk.vtkCommand.ModifiedEvent, self.onDataParameterNodeModified)`. To add a new reactive widget, write an `updateFoo()` method and hook it into an existing dispatch method (e.g., `updatePhotoscanGenerationButtons()`).

### Session state access
- Check existence: `get_openlifu_data_parameter_node().loaded_session is None`
- Get IDs: `loaded_session.get_subject_id()`, `loaded_session.get_session_id()` (methods, not attributes)
- `get_openlifu_data_parameter_node()` is in `OpenLIFULib/util.py`, calls `slicer.util.getModuleLogic('OpenLIFUData').getParameterNode()`

### Dialogs in module files
Module dialogs (e.g., `PhotoscanPreviewDialog`, `AddNewPhotoscanDialog`) are `qt.QDialog` subclasses defined in the module's `.py` file. UI is built programmatically in `__init__`, shown with `dialog.exec_()`. No `.ui` files for dialogs.

### `@display_errors` decorator
All slot handlers should use `@display_errors` so exceptions are displayed to the user rather than silently swallowed by Qt's signal/slot mechanism.

## Custom Application (`openlifu-desktop-application`)

The [`openlifu-desktop-application`](https://github.com/OpenwaterHealth/openlifu-desktop-application) repository builds a custom Slicer-based application that bundles SlicerOpenLIFU as a built-in extension. The checkout and superbuild locations are developer-specific; use placeholders such as `<openlifu-desktop-application>` and `<custom-app-superbuild>` in local notes or commands.

Key differences from the vanilla extension build:
- **Default home module**: The custom app sets `Slicer_DEFAULT_HOME_MODULE "Home"` (its own Home module, not OpenLIFUHome).
- **All widgets created at startup**: The custom app's Home module connects to `startupCompleted()` and calls `enforceGuidedModeVisibility()` and Login's `cacheAllLoginRelatedWidgets()`, both of which call `widgetRepresentation()` on ALL OpenLIFU modules. This forces `setup()` (and thus `connectGui()`) to run on every module at startup, unlike vanilla Slicer where widgets are only created when a module is first selected.
- **Tests**: Run against the custom application's Slicer build, for example `ctest -R py_OpenLIFUHome -VV --test-dir <custom-app-superbuild>/Slicer-build/`. The test executable is `OpenLIFU` not `Slicer`.
- **File paths**: Installed module files live under the custom application's `lib/OpenLIFU-<version>/qt-scripted-modules/` tree (not a stock `lib/Slicer-<version>/...` tree). The bundled SlicerOpenLIFU source copy is fetched at a pinned revision during the custom application superbuild.

### connectGui/disconnectGui bug (Slicer upstream issue)

`@parameterNodeWrapper.connectGui()` adds a VTK `ModifiedEvent` observer on the underlying MRML parameter node, but `disconnectGui()` never removes it. This is a bug in Slicer's `slicer/parameterNodeWrapper/wrapper.py`. The stale observer's lambda captures the old wrapper instance.

This is harmless in vanilla Slicer (widgets are created after scene clears), but causes segfaults in the custom app where all widgets are pre-created: after `slicer.mrmlScene.Clear()`, singleton parameter nodes survive but lose their `ModuleName` attribute. When `getParameterNode()` re-sets `ModuleName`, `ModifiedEvent` fires, the stale observer calls `_updateGUIFromParameterNode()` with the old wrapper, and it crashes.

**Workaround** (in each module's `setParameterNode`): Track the VTK observer tag from `connectGui` by temporarily monkey-patching `mrml_node.AddObserver`, then `RemoveObserver(tag)` when `disconnectGui` is called. See the `_connectGuiVtkObserverTag` pattern in any module's `setParameterNode`.

**Important constraint**: Do NOT use `parameterNode.RemoveObservers(vtk.vtkCommand.ModifiedEvent)` — it also removes:
- `_CachedParameterWrapper` observers needed for cached parameter reads
- Cross-module observers (e.g., PrePlanning/TransducerLocalization/SonicationPlanner/ProtocolConfig all observe Data's parameter node via `onDataParameterNodeModified`)

### Cross-module parameter node observers

Four modules observe the **Data** module's MRML parameter node for `ModifiedEvent` (set up once in `setup()`, never re-added):
- PrePlanning → `self.onDataParameterNodeModified` → `updateInputOptions()`
- TransducerLocalization → `self.onDataParameterNodeModified`
- SonicationPlanner → `self.onDataParameterNodeModified`
- ProtocolConfig → `self.onDataParameterNodeModified`

These observers are critical for reactive UI updates when data is loaded/changed.

## Commit Guidelines

- **Every commit must reference a relevant GitHub issue number** in the title or body (e.g. `Fix target placement crash (#42)` or with `Fixes #42` / `Relates to #42` in the body).
- When creating commits, always verify an issue number is included before finalizing.
- When reviewing code or PRs, check that every commit references an issue number and flag any that don't.
