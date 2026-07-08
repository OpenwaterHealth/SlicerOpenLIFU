# De-moduling plan

Scratch working doc for collapsing SlicerOpenLIFU's N Slicer scripted sub-modules into a
single host `OpenLIFU` module with plain `QWidget` pages.

**Status:** planning / not started. Iterate here before any code touches.

---

## Motivation

Every host-embedding bug we chase is downstream of the multi-module architecture:
`connectGui`/`disconnectGui` observer leak, `ctkCollapsibleButton` state corruption from
generic subtree visibility recipes, `cacheAllLoginRelatedWidgets` pre-creation dance,
cross-module parameter-node observer graph, `slicer.util.selectModule` shim, guided-workflow
injection scaffolding, module-picker filtering, `_hook_workflow_updates` monkey-patching.

We are abandoning support for using any OpenLIFU sub-module as a standalone Slicer module;
they are only ever used through the host. The whole reason those modules were separate is
gone.

## Goals

- One Slicer scripted module (`OpenLIFU`) with N plain `QWidget` pages.
- One host `Logic` class owning shared state (currently spread across N parameter nodes).
- Widgets talk to other widgets via direct method calls on the host, not via
  `getModuleLogic("OpenLIFUData")` / `getModuleWidget(...)` or MRML-observer choreography.
- One integration test target (`py_OpenLIFU`) with subtests, replacing the per-module
  `py_OpenLIFU<Xxx>` targets.
- Remove: `_install_select_module_shim`, `_hook_workflow_updates`, `register_module_callback`,
  `cacheAllLoginRelatedWidgets`, `apply_module_layout`, `wire_passive_module_header`,
  `embed_module_body_into`, `navigate_to_page`, and the `connectGui`/`disconnectGui` observer
  workaround in every `setParameterNode`.

## Non-goals

- No behavior changes. Same UI, same workflow, same tests pass.
- No openlifu-python changes. Session dataclass changes belong to the separate
  session-owns-all Phase 2 (see `/memories/repo/session-ownership-refactor.md`).
- No visual redesign.

---

## Current state (facts, not opinion)

### Built modules (12)

Top-level `SlicerOpenLIFU/CMakeLists.txt` adds:

```
OpenLIFULib, OpenLIFU, OpenLIFUHome, OpenLIFUDatabase, OpenLIFUData, OpenLIFUSession,
OpenLIFUPrePlanning, OpenLIFUSonicationControl, OpenLIFUSonicationPlanner,
OpenLIFUTransducerLocalization, OpenLIFULogin, OpenLIFUCloudSync
```

`OpenLIFUProtocolConfig/` and `OpenLIFUTransducerTracker/` exist on disk but are NOT built.
Delete-candidates before starting.

### Ownership of state

Almost all shared state lives on Data's parameter node:

| Field | Type |
|---|---|
| `loaded_protocols` | `Dict[str, SlicerOpenLIFUProtocol]` |
| `loaded_transducers` | `Dict[str, SlicerOpenLIFUTransducer]` |
| `loaded_solution` | `Optional[SlicerOpenLIFUSolution]` |
| `loaded_session` | `Optional[SlicerOpenLIFUSession]` |
| `loaded_run` | `Optional[SlicerOpenLIFURun]` |
| `loaded_photoscans` | `Dict[str, SlicerOpenLIFUPhotoscan]` |

Other modules' parameter nodes are essentially empty:

- Home: `guided_mode: bool`
- Database: `databaseDirectory: Path`
- Login: `user_account_mode: bool`
- Session / PrePlanning / SonicationPlanner / SonicationControl: no fields (state is derived).

### Cross-module coupling (concrete)

Observers of `get_openlifu_data_parameter_node().parameterNode` (VTK `ModifiedEvent` →
`onDataParameterNodeModified`):

- OpenLIFUSession
- OpenLIFUPrePlanning
- OpenLIFUTransducerLocalization
- OpenLIFUSonicationPlanner

Callback registrations (`register_module_callback`, `call_on_db_changed`,
`call_on_active_user_changed`) — call graph:

- module_layout.py::wire_passive_module_header — subscribes any passive header to DB + Login
- OpenLIFUData → Database, Login, SonicationControl
- OpenLIFUHome → Database, Login
- OpenLIFULogin → Database (and re-registers on user change)
- OpenLIFUSonicationControl → several

Cross-module widget / logic lookups:

- `getModuleLogic("OpenLIFUData")` — from PrePlanning (many), OpenLIFU host, others
- `getModuleLogic("OpenLIFULogin")` — from Data
- `getModuleLogic("OpenLIFUSonicationControl")` — from Data
- `getModuleLogic("OpenLIFUHome")` — from OpenLIFU host, desktop app Home
- `getModuleWidget("OpenLIFUSonicationPlanner")` — from PrePlanning (`deleteSolutionAndSolutionAnalysisIfAny()`)
- `getModuleWidget("OpenLIFUTransducerLocalization")` — from PrePlanning
- `getModuleWidget("OpenLIFUSonicationControl")` — from Data
- `getModule("OpenLIFUData").widgetRepresentation()` — from PrePlanning, Session
- `getModule("OpenLIFUTransducerLocalization").widgetRepresentation()` — from PrePlanning

`slicer.util.selectModule("OpenLIFU<X>")` call sites (all internal after this pass):

- OpenLIFUPrePlanning test → `selectModule("OpenLIFUPrePlanning")`
- OpenLIFUDatabase resource helper → `selectModule("OpenLIFUDatabase")`
- OpenLIFUDatabase test → `selectModule("OpenLIFUDatabase")`

Everything else routes through the `_install_select_module_shim` that this pass deletes.

### Downstream coupling

`openlifu-desktop-application/Modules/Scripted/Home/Home.py` references specific sub-modules
by name in `startupCompleted` callbacks:

- `getModule("OpenLIFU")` — force host widget creation (survives)
- `getModuleLogic("OpenLIFUHome").start_guided_mode()` / `workflow_jump_ahead()` / `workflow.enforceGuidedModeVisibility(True)`
- `getModuleLogic("OpenLIFULogin").start_user_account_mode()`
- `getModuleWidget("OpenLIFUDatabase")`, `getModuleLogic("OpenLIFUDatabase")` — auto-connect
- `getModuleWidget("OpenLIFULogin").onParameterNodeModified(None, None)` — show login banners

Post-migration these become methods on `OpenLIFU`'s host logic / widget. The desktop app's
`Home.py` needs matching updates in the same PR (or a companion PR sequenced together).

`openlifu-test-app` and `openlifu-operator-interface`: neither depends on SlicerOpenLIFU
(both use `openlifu-sdk` directly). Not in scope.

### Tests

- `py_OpenLIFUHome::runTest()` is the master orchestrator: DVC pull, kwave install, then
  calls each sub-module test's workflow method in sequence. Tests share state — not
  independent.
- Standalone tests not called by the orchestrator: `py_OpenLIFULogin`, `py_OpenLIFUCloudSync`.
- DVC env plumbed in `SlicerOpenLIFU/CMakeLists.txt` and `OpenLIFUHome/CMakeLists.txt`
  (`GDRIVE_CREDENTIALS_DATA`, `DVC_REPO_DIR`).

---

## Target architecture

```
SlicerOpenLIFU/
  OpenLIFULib/           # shared utilities, parameter wrappers, dialogs library — stays
  OpenLIFU/
    CMakeLists.txt
    OpenLIFU.py          # single ScriptedLoadableModule; thin shell that imports OpenLIFUApp
    Resources/
      UI/
        OpenLIFU.ui      # shell (header, stack, footer)
        HomePage.ui
        SessionPage.ui
        PrePlanningPage.ui
        ...              # one .ui per page, loaded directly by page widget classes
        DatabasePopup.ui
        LoginPopup.ui
    OpenLIFUApp/         # importable Python subpackage (namespaced to avoid collisions
                         # with generic names like `pages`/`logic` on Slicer's sys.path)
      __init__.py
      pages/             # plain QWidget subclasses; NOT ScriptedLoadableModuleWidgets
        __init__.py
        home_page.py
        database_page.py   # a QDialog or QWidget shown as popup
        login_page.py
        data_page.py
        session_page.py
        preplanning_page.py
        transducer_localization_page.py
        sonication_planner_page.py
        sonication_control_page.py
      logic/             # plain classes (formerly *Logic); no ScriptedLoadableModuleLogic
        __init__.py
        app_state.py     # single parameter-node wrapper replacing N wrappers
        data_logic.py
        database_logic.py
        login_logic.py
        preplanning_logic.py
        ...
      tests/
        __init__.py
        test_orchestration.py  # runs former runTest bodies as subtests via unittest
```

### Host state (`OpenLIFUAppState` parameter node)

Single `@parameterNodeWrapper` on the host with the union of what today's Data /
Home / Database / Login parameter nodes hold. Fields keep their current wrapper types
(`SlicerOpenLIFUSession`, etc.) so serialization is unchanged. Adding a session-owned list
of Volumes/Transducers/Protocols is out of scope for this pass (session Phase 2).

### Cross-widget communication

Widgets get a reference to the host on construction:

```python
class PrePlanningPage(QWidget):
    def __init__(self, host: "OpenLIFUWidget", parent=None):
        super().__init__(parent)
        self.host = host          # for host.state, host.pages, host.logic
```

Everywhere we used to write:

- `getModuleLogic("OpenLIFUData").something()` → `self.host.logic.data.something()`
- `getModuleWidget("OpenLIFUSonicationPlanner").deleteSolutionAndSolutionAnalysisIfAny()` →
  `self.host.pages.sonication_planner.deleteSolutionAndSolutionAnalysisIfAny()`
- `get_openlifu_data_parameter_node()` observed via VTK ModifiedEvent →
  same, but observing `self.host.state.parameterNode` (still MRML while parameter node
  wrappers exist). Later cleanup: replace with direct method calls or Qt signals emitted
  by host logic on state changes.
- `register_module_callback(...)` → direct subscription to a Qt signal on host logic
  (e.g. `self.host.logic.database.dbChanged.connect(self.onDatabaseChanged)`).

### Guided workflow

`OpenLIFUHome`'s `Workflow` class moves into host logic as `self.host.logic.workflow`.
`workflow.update_all` becomes a Qt signal (`workflowUpdated`) that the host widget
connects to `_refresh_timeline_state`. No more monkey-patching.

### Popups

`Database` and `Login` are already hidden modules surfaced via popups from the Data page.
Post-migration they are `QDialog` subclasses in `pages/`. `CloudSync` becomes a plain
helper class in `logic/` — its widget is deprecated placeholder text.

---

## Migration order

Route: keep Data's parameter node and shim in place through the entire migration; fold
Data last. This keeps `get_openlifu_data_parameter_node()` working for every module we
haven't touched yet.

**Round 0 — scaffolding (single small PR)**

- Introduce namespaced `OpenLIFU/OpenLIFUApp/` importable subpackage with empty
  `pages/` and `logic/` sub-subpackages.
- Introduce `OpenLIFUAppState` parameter node wrapper (`OpenLIFUApp/logic/app_state.py`)
  — empty for now, no fields moved yet. Not yet wired to the host's parameter node.
- Update `OpenLIFU/CMakeLists.txt` `MODULE_PYTHON_SCRIPTS` to install the new files.
- Smoke import from `OpenLIFU.py` to prove packaging works.
- `self.host` reference plumbing on page classes is deferred to Round 1 (introduced when
  the first real page class lands — nothing to do on the host itself yet).
- Delete the un-built `OpenLIFUProtocolConfig/` and `OpenLIFUTransducerTracker/`
  directories (they aren't in CMake anyway) and remove their stale mentions from
  `OpenLIFUData.py`'s module-header refresh loop.

**Round 1 — smallest, most isolated pages first (validate the pattern)**

Pick ONE module and land it end-to-end before proceeding. Recommended: **OpenLIFUCloudSync**.
- Empty parameter node, near-empty widget (placeholder), one logic singleton that
  Database observes via Qt signals.
- Landing this proves: pages/logic layout, replacement of `apply_module_layout`, deleting
  a Slicer scripted module (remove from CMake, delete dir, verify build).

- [x] **Round 1a done (CloudSync):** `OpenLIFUCloudSync/` deleted. Logic moved to
  `OpenLIFU/OpenLIFUApp/logic/cloud_sync.py` (singleton `getCloudSyncLogic()` +
  `CloudStatusHelper` unchanged in API). CLI subprocess moved to
  `OpenLIFU/OpenLIFUCloudSyncEngine/OpenLIFUCloudSyncCLI.py` (still installed to
  `<lib>/bin/OpenLIFUCloudSyncCLI.py`). Widget class dropped — CloudSync had no
  visible page, only the controls hosted in Database's popup. All four callers
  (module_layout.py x3, TransducerLocalization, Home, Database) rewritten to
  `from OpenLIFUApp.logic.cloud_sync import getCloudSyncLogic`. Top-level
  `add_subdirectory(OpenLIFUCloudSync)` removed.

Then **OpenLIFUSession** (empty parameter node, read-only observer of Data — pure UI
that reflects state).

- [x] **Round 1b done (Session):** Widget/Logic/Test/ParameterNode class bodies extracted
  to `OpenLIFU/OpenLIFUApp/pages/session_page.py`. `OpenLIFUSession/OpenLIFUSession.py`
  reduced to module metadata + re-export of the four classes, so
  `slicer.util.selectModule("OpenLIFUSession")` and
  `slicer.modules.OpenLIFUSessionWidget` keep working during rounds 2-4. `Resources/UI`
  and `Resources/Icons` remain in `OpenLIFUSession/` (Slicer resolves `self.resourcePath`
  via the module directory). External caller `from OpenLIFUSession import
  OpenLIFUSessionTest` in `OpenLIFUHome.py` still works via the re-export. The shell
  module is deleted in Round 5 when the host owns page navigation.

**Round 2 — passive workflow pages**

- OpenLIFUPrePlanning
- OpenLIFUTransducerLocalization
- OpenLIFUSonicationPlanner
- OpenLIFUSonicationControl

Each of these currently observes Data's parameter node and calls `getModuleLogic("OpenLIFUData")`.
Post-migration they hold `self.host` and call `self.host.logic.data.*` directly. Their
`onDataParameterNodeModified` handlers keep working against Data's still-live parameter
node until Round 4.

Order within round 2: PrePlanning first (touches Localization + SonicationPlanner via
`getModuleWidget`; unwinding those coupling points early flushes out issues). Then
Localization, Planner, Control.

**Round 3 — modal / hidden modules**

- OpenLIFUDatabase → `pages/database_page.py` (QDialog) + `logic/database_logic.py`.
  `call_on_db_changed` → `dbChanged` Qt signal on the logic.
- OpenLIFULogin → same treatment. `call_on_active_user_changed` → Qt signal. The
  "iterate every module's parameter node to enforce `slicer.openlifu.allowed-roles`" walk
  becomes a walk over `self.host.pages.*` widgets.
- Delete `cacheAllLoginRelatedWidgets` (all page widgets already exist at host construct
  time — that's the whole point of the migration).

**Round 4 — the central hub**

- Fold OpenLIFUData last. Move its `loaded_*` fields onto `OpenLIFUAppState`. Move
  interactive-header responsibilities into the host's shell UI. Delete Data's parameter
  node. Deleted at same time: `get_openlifu_data_parameter_node()` helper and every
  remaining VTK `ModifiedEvent` observer on it — Round 2 pages that still observe it get
  swapped over to Qt signals (`self.host.state.dataChanged`) in this round.

**Round 5 — OpenLIFUHome + desktop app**

- Fold OpenLIFUHome (workflow logic) into host logic.
- Update `openlifu-desktop-application/Modules/Scripted/Home/Home.py`: replace every
  `getModuleLogic("OpenLIFU<Xxx>")` call with `getModuleLogic("OpenLIFU")` +
  attribute access on the host (`host_logic.workflow.start_guided_mode()` etc.).
- Delete `_install_select_module_shim`, `_hook_workflow_updates`, `apply_module_layout`,
  `wire_passive_module_header`, `embed_module_body_into`, `navigate_to_page` from
  module_layout.py — that file is now down to just the shared `ModuleHeaderWidget` (or
  it disappears entirely if the host owns the header directly).
- Rip out the `_connectGuiVtkObserverTag` workaround in every `setParameterNode` —
  there's only ONE parameter node now, on the host, and its lifetime matches the host's.

---

## Tests

Per-module `py_OpenLIFU<Xxx>` targets go away round-by-round as each module is folded.
Replace with a single `py_OpenLIFU` unittest that runs the same body of assertions as
subtests (using `unittest.TestCase.subTest` or ordered methods). The existing
`_OpenLIFU_FullTest1` orchestration in `OpenLIFUHome.py` already does this sequencing;
lift it into the new `OpenLIFU/tests/test_orchestration.py`.

Standalone tests (`py_OpenLIFULogin`, `py_OpenLIFUCloudSync`) either move into the same
orchestration or become independent tests in `OpenLIFU/tests/`.

DVC data / env vars: move the CMake plumbing from `OpenLIFUHome/CMakeLists.txt` to
`OpenLIFU/CMakeLists.txt`. Keep `db_dvc_slicertesting.dvc` in the repo root.

---

## Branch and PR strategy

- Single long-lived branch `demodule` off `main`. Rebase weekly to avoid drift.
- Round 0 lands as one PR (scaffolding + directory deletes; low risk).
- Each subsequent round is a PR against `main` (or a stacked chain, TBD by reviewer
  bandwidth). Every intermediate main is buildable and passes `py_OpenLIFUHome`.
- We do NOT ship a broken intermediate — the shim + module_layout helpers stay in place
  and Data's parameter node stays live until Round 4.
- Rollback for any round: revert the round's PR; earlier rounds are independent.

## Cheap ride-alongs

Fold in during Round 2 wherever they arise for free:

- Drop `if loaded_session is None:` guards in TL/Planner/Control (from
  session-owns-all Phase 2 backlog) — trivial once the widget knows its host is only
  visible when a session exists.
- Add `load_session` try/finally around the ~200-line body in Data (from Phase 2 backlog).

Defer to future work (not this pass):

- Volume/Protocol/Transducer as session-owned lists — requires openlifu-python Session
  dataclass changes, out of scope.

---

## Decisions

1. **`OpenLIFULib` stays as its own package** for this pass. It has a lot of shared
   utilities and thin wrappers used by external code paths and tests. Weird enough to
   deserve its own future cleanup pass; not in scope here.
2. **`SlicerOpenLIFU*` parameter-node wrapper classes stay** (in `OpenLIFULib/`). They
   still bridge openlifu ↔ MRML serialization and are unchanged by de-moduling.
3. **Keep the `slicer.openlifu.allowed-roles` Qt-property tagging mechanism**, but
   simplify to a single-tier opt-in and replace the current per-module tree walk with
   a single walk over the host widget:
   - Today: widget must have `objectName == "permissionsWidget*"` AND
     `slicer.openlifu.allowed-roles` property. Login walks every module's widget tree
     (requires `cacheAllLoginRelatedWidgets`).
   - Post-migration: drop the naming convention. Any widget in any page `.ui` with the
     `slicer.openlifu.allowed-roles` property set is restricted. Login's logic walks
     `self.host.findChildren(QWidget)` once on user change. Widgets without the property
     are visible/enabled to everyone (matches today's semantics for unmarked widgets).
   - Semantics preserved: `setEnabled(True/False)` (grayed out), not `setVisible`.
   - `cacheAllLoginRelatedWidgets` is deleted regardless — all pages already exist as
     children of the host at construction time.
4. The desktop app keeps `Slicer_DEFAULT_HOME_MODULE "Home"` (its own Home). No change
   to the CMake default. Its `Home.py` calls into the host via cleaner method access
   (Round 5).

## Deferred to a follow-up (not this pass)

- Guided-mode timeline visual polish (from Phase 1 backlog).
- Any further collapse of `OpenLIFULib`.
- Session-owns-all Phase 2 items that require openlifu-python Session dataclass changes
  (Volume/Protocol/Transducer as session-owned lists).
