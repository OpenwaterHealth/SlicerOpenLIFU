# Session Split Design (openlifu-python & SlicerOpenLIFU)

Status: **APPROVED** — 2026-07-29. Design decisions locked (see §12).
Reviewers: @pjh7 (and any OpenLIFU app team members)
Tracking: SlicerOpenLIFU#631, openlifu-python#493
Target branches: `v2_ui_refactor` (SlicerOpenLIFU) and `v2_refactor` (openlifu-python). Commits land directly on these branches; no `session_split` sub-branch.

## 1. Motivation

The current `openlifu.db.Session` mixes:

* Planning-phase data (targets, virtual-fit results, pre-solutions)
* Sonication-phase data (photoscan registrations, transducer-tracking
  results, the final solution, runs)

Every cross-page cascade in SlicerOpenLIFU has to reason about "which of
these am I invalidating, and does the currently-active thing care?"
That's the root of `deleteSolutionAndSolutionAnalysisIfAny` clobbering
unrelated solutions (#630), of PNP leaking between pages, of implicit
`write_session` calls persisting exploratory work (#626 followup), of
"a second later the analysis reverts to No Solution" mystery
invalidations. Every attempt to patch a symptom exposes another one.

The connective tissue is a disaster. The algorithms and the individual
UI widgets are fine.

## 2. The split

**Two new session types + one output artifact.**

* **`PlanningSession`** — the working document for developing a treatment
  plan. Owns targets, virtual-fit results, and pre-solutions. Mutable.
  Multiple planning sessions may exist per subject.
* **`Plan`** — the finalized, immutable output of a PlanningSession.
  Contains everything a SonicationSession needs to know to sonicate:
  target, transducer array-to-volume transform, protocol reference,
  optional pre-solutions for expected-outcome QA. Written to disk when
  the user clicks "Finalize Plan" on a PlanningSession.
* **`SonicationSession`** — the at-treatment-time session. Loads a Plan
  by id (frozen input). Owns photoscan registrations, transducer-tracking
  results, the final Solution, and Runs. Multiple sonication sessions
  may reference the same Plan (e.g. re-treatment).

**Cascade responsibilities become sharply scoped.**

| User action | Invalidates |
|---|---|
| Move a target in PrePlanning | This PlanningSession's VFs for that target + pre-solutions for that target |
| Revoke a VF approval | This PlanningSession's pre-solutions that used that VF |
| Modify a target after Plan finalization | The Plan is now stale; user must create a new Plan (old one is immutable) |
| Delete a photoscan in Localization | Registrations and TT results in THIS SonicationSession that use it |
| Revoke a TT approval | The final Solution in THIS SonicationSession |

No cascade crosses session types. No cascade ever affects an object
outside the currently-loaded session.

## 3. Data model

### 3.1 `openlifu.db.Plan`

```python
@dataclass
class Plan(DictMixin):
    """Immutable finalized treatment plan produced by a PlanningSession.

    A Plan pins down: which target, which transducer at which pose (the VF
    that was approved), and against which protocol. Optionally carries the
    pre-solutions computed at planning time so the sonication operator can
    review expected pressures before treatment.

    Frozen once written. Editing means "create a new Plan from an updated
    PlanningSession".
    """
    id: str
    subject_id: str
    volume_id: str
    protocol_id: str
    transducer_id: str
    target: Point
    array_transform: ArrayTransform          # the VF pose the plan committed to
    pre_solutions: List[SolutionInfo] = []   # optional; empty if user finalized without computing
    date_created: datetime
    notes: str = ""
    parent_planning_session_id: Optional[str] = None  # provenance back to the PlanningSession
```

### 3.2 `openlifu.db.PlanningSession`

```python
@dataclass
class PlanningSession(DictMixin):
    """A working document for developing a Plan.

    Mutable throughout its lifetime; explicit `save` writes the JSON.
    Explicit `finalize_plan()` produces a Plan record and writes it to disk
    (does not modify the PlanningSession itself; the same session can be
    re-finalized as a different Plan later if desired).
    """
    id: str
    name: str
    subject_id: str
    volume_id: str
    protocol_id: str
    transducer_id: str

    targets: List[Point] = []
    virtual_fit_results: Dict[str, List[Tuple[bool, ArrayTransform]]] = {}
    pre_solutions: List[SolutionInfo] = []

    finalized_plan_ids: List[str] = []       # every Plan this session has produced
    date_created: datetime
    date_modified: datetime
    attrs: dict = {}
```

Notes:
* `virtual_fit_results` and `pre_solutions` use the existing `ArrayTransform`
  and `SolutionInfo` types unchanged.
* No `photoscans` field — photoscans are Sonication-side.
* No `solution_id` field — there is no "the" solution here; the finalized
  Plan's `pre_solutions` list is the record.

### 3.3 `openlifu.db.SonicationSession`

```python
@dataclass
class SonicationSession(DictMixin):
    """At-treatment-time session.

    Loads a Plan by id (immutable input). Session-owned data is the
    photoscan registrations, transducer-tracking results, the final
    Solution reference, and Runs. The Plan's target, volume, protocol,
    and array_transform are the reference "what we're trying to hit";
    the SonicationSession tracks "what we actually did" via TT + Solution
    + Run.
    """
    id: str
    name: str
    subject_id: str
    plan_id: str                             # frozen reference; volume + target + VF pose come from here

    photoscan_ids: List[str] = []            # subject-scoped storage, session-scoped ownership
    photoscan_registrations: List[PhotoscanRegistration] = []
    transducer_tracking_results: List[TransducerTrackingResult] = []

    solution: Optional[SolutionInfo] = None  # the ONE final solution (metadata ref; actual files at subject scope)
    runs: List[Run] = []

    date_created: datetime
    date_modified: datetime
    attrs: dict = {}
```

Notes:
* **No `volume_id` field.** Volume comes from the Plan. Starting a
  Sonication Session involves loading a Plan, which directly loads the
  volume, target, and VF pose. If the treatment-time workflow ever needs
  a re-scan, the user finalizes a new Plan against that new volume
  (correct semantics: a different volume is a different plan).
* `solution` is `Optional[SolutionInfo]`, not a list. A SonicationSession
  computes at most one final solution (recomputes replace it in-place).
  Multi-solution generation + selection (akin to virtual fitting and
  picking a favorite) is a plausible future feature but out of scope for
  this refactor. If the user needs to try a different pose, they either
  (a) recompute in the same session (previous solution ref is discarded,
  `run` history stays), or (b) create a new SonicationSession from the
  same Plan.
* `photoscan_ids` list is the SonicationSession's owned photoscans. Two
  sonication sessions on the same subject would each capture their own
  photoscan(s) at treatment time.

### 3.4 Field-level changes to existing types

* `SolutionInfo.array_transform` (already exists, #491) is now
  authoritative in both Plan.pre_solutions and SonicationSession.solution.
* `SolutionInfo.transducer_transform_source` and
  `SolutionInfo.transducer_transform_source_id` stay as-is (#492).
* `PhotoscanRegistration` and `TransducerTrackingResult` are unchanged.

## 4. Database layout

All heavy artifacts (volumes, photoscans, plans, solutions) live at
subject scope. Sessions and plans reference them by id. This is a clean
separation of storage (subject-owned) from ownership (session-owned) and
sets us up cleanly for a future migration to a relational database.

```
{db_root}/
  users/                                  # unchanged
  protocols/                              # unchanged
  transducers/                            # unchanged
  subjects/
    subjects.json
    {subject_id}/
      {subject_id}.json
      volumes/                            # unchanged (already subject-scoped)

      photoscans/                         # subject-scoped storage; ownership per SonicationSession
        photoscans.json
        {photoscan_id}/
          {photoscan_id}.json
          {photoscan_id}.obj
          {photoscan_id}.mtl
          {photoscan_id}.png
          ...

      solutions/                          # subject-scoped storage; ownership per PlanningSession / Plan / SonicationSession
        solutions.json
        {solution_id}/
          {solution_id}.json
          {solution_id}.nc
          analysis.json

      plans/                              # subject-scoped storage; referenced by PlanningSession (produced) and SonicationSession (consumed)
        plans.json
        {plan_id}/
          {plan_id}.json                  # holds SolutionInfo list referencing solutions/{sid}/

      planning_sessions/
        planning_sessions.json
        {planning_session_id}/
          {planning_session_id}.json      # holds SolutionInfo list referencing solutions/{sid}/

      sonication_sessions/
        sonication_sessions.json
        {sonication_session_id}/
          {sonication_session_id}.json    # holds solution SolutionInfo ref + photoscan_ids
          runs/
            runs.json
            {rid}/{rid}.json
```

**Storage vs. ownership** (design principle): heavy binary artifacts
(volumes, photoscans, solution `.nc` files, analysis JSON) are stored at
subject scope so they're not duplicated across sessions and don't churn
when sessions are deleted. Sessions and plans hold lightweight
`SolutionInfo` / photoscan-id references. `SolutionInfo` already carries
the metadata (id, protocol_id, target_id, transducer_id, approved,
computed_at, array_transform, transducer_transform_source[_id]) needed
for UI filtering and provenance without loading the pressure field.

**Solution ownership rules**:
* A solution can be referenced by exactly one PlanningSession's
  `pre_solutions` (the session that computed it) OR one Plan's
  `pre_solutions` (copied by reference at finalize time) OR one
  SonicationSession's `solution` field.
* Deleting a session/plan does NOT delete the solution files
  automatically (safer default; user does a separate cleanup action or
  orphan sweep).

**Photoscan file storage**: same principle. `db.get_photoscan_ids(subject_id)`
is subject-scoped; each SonicationSession filters that list to its own
owned subset.

Old `subjects/{subject_id}/sessions/` directory is left alone; existing
sessions live there as read-only. No migration.

## 5. Page architecture

### 5.1 Enter/refresh contract (applies to every page)

* `enter()` fully reconstructs the visible state from the currently-loaded
  session (Planning or Sonication), the openlifu Session object, and the
  Slicer scene. It is the SOLE source of truth for first-render.
* Signal handlers (button clicks, combobox changes, table selection) do
  the local state mutation and then call `refresh()` directly. No
  `dataChanged.connect` observers, no cross-page fanouts.
* `refresh()` re-runs the same construction as `enter()`. Idempotent.
* `exit()` disconnects any GUI bindings but does NOT tear down state.

### 5.2 Slicer-object drift detection

Legitimate use case for automatic detection: the user modifies a Slicer
object (transform matrix, fiducial position) outside our UI — via the
Slicer Markups module, the Python console, or a scripted extension.
When this happens, we want to catch it on the next page entry and
propagate the change into the session state.

**Mechanism** (page-level, called from `enter()`):

```python
def _reconcile_slicer_state_into_session(self):
    """Detect drift between Slicer scene objects and their session-state
    representations. Update session state accordingly, cascading through
    the session data model (NOT through UI observers)."""
    # Example: check every session target's fiducial node position.
    for target in self._session.targets:
        node = _find_fiducial_for_target(target)
        if node is None:
            continue
        actual = node.GetNthControlPointPosition(0)
        stored = target.position
        if not _positions_equal(actual, stored):
            # Drift detected. Apply cascade at the SESSION LEVEL:
            self._session.on_target_moved(target.id, new_position=actual)
            # PlanningSession.on_target_moved -> revokes VFs for this target,
            # deletes pre-solutions for this target. All in one function on
            # the openlifu data model. UI doesn't need to know.

    # Same idea for VF / TT transform matrices vs stored array_transforms.
```

No global observers. No `TransformModifiedEvent` handlers. No
`onNodeRemoved` handlers wired to session bookkeeping.

### 5.3 Page-by-page contract

| Page | Session type | Owns |
|---|---|---|
| **Home** | either or none | Landing + workflow launch |
| **Data Manager** | either or none | CRUD on planning sessions, plans, sonication sessions |
| **Planning Session Overview** | PlanningSession | Status card for a PlanningSession |
| **Sonication Session Overview** | SonicationSession | Status card for a SonicationSession + linked Plan |
| **PrePlanning** | PlanningSession | Target placement, VF |
| **Solution Generator** | either | Given (volume, protocol, target, pose) → compute a Solution. Pre-solution when caller is PlanningSession; final Solution when caller is SonicationSession. |
| **Localization** | SonicationSession | Photoscan registration + TT |
| **Sonication Control** | SonicationSession | Load solution to device, run, records |

**Solution Generator** is the renamed / refactored current
`sonication_planner_page.py`. It takes its inputs from the session:

* PlanningSession context: target selection + VF selection → pre-solution
  goes into `PlanningSession.pre_solutions`.
* SonicationSession context: target and pose come from
  `SonicationSession.plan.target` + a TT result selection → final Solution
  goes into `SonicationSession.solution`.

The page itself doesn't hardcode "am I in planning or sonication mode".
It reads `session.kind` (or checks isinstance) and adjusts the picker
options and destination.

### 5.4 Guided workflows

**Planning workflow**:

```
Home ("New Planning Session" | "Continue Planning Session")
     -> Planning Session Overview
     -> PrePlanning (place target, run VF, approve VF)
     -> Solution Generator (optional: compute pre-solutions)
     -> back to Planning Session Overview
     -> "Finalize Plan" button -> writes a Plan; user can continue editing
```

**Sonication workflow**:

```
Home ("New Sonication Session" | "Continue Sonication Session")
     -> Sonication Session Overview
     -> Localization (capture photoscan, register, run TT, approve TT)
     -> Solution Generator (compute final Solution)
     -> back to Sonication Session Overview
     -> Sonication Control (send to device, run, record)
```

Data Manager is available as an admin CRUD panel and can also
launch a session (SlicerOpenLIFU#635 kept both entry points), but
Home is the intended primary route.

## 6. Save semantics

**In-memory session is 1:1 with disk at load time.** Disk is only touched
on explicit save. Solution binary files (`.nc`, analysis JSON) are
per-solution artifacts and CAN be written eagerly on compute (they don't
represent user intent, just derived data). But the *session JSON* (which
solutions are affiliated, which is the final one) never touches disk
outside an explicit `save`.

**Dirty flag** (already added in #626) tracks in-memory changes. Save
clears it. Exit / switch prompts if dirty.

**Removes the implicit `write_session` in**:

* `set_solution` (was: writes session inline)
* `clear_solution` (was: writes session inline when unlinking)
* `update_underlying_openlifu_session` (should never touch disk; keep as
  in-memory-only sync)
* Photoscan add / remove (should mark dirty; save on Save)

## 7. Photoscan ownership

**Storage**: `subjects/{subject_id}/photoscans/` (physical files).
**Ownership**: per-SonicationSession, tracked in
`SonicationSession.photoscan_ids`.

Reasoning: photoscans are captured at treatment time. Loading a photoscan
from a prior treatment into a new SonicationSession is uncommon and
requires explicit user intent (an Import action, not the default flow).
Storing subject-side simplifies filesystem management. Deleting a
SonicationSession removes its photoscan_ids reference but does NOT
delete the underlying files (safer default; user can bulk-delete via a
separate cleanup action if desired).

## 8. Plan finalization

Finalizing a plan writes an immutable `Plan` record derived from the
currently-loaded PlanningSession. The `finalize_plan(...)` function
lives in `OpenLIFU/OpenLIFUApp/plan_finalization.py` so any page can
invoke it. Design decision on WHERE the button lives is being
revised under SlicerOpenLIFU#634:

* Originally proposed to live on Planning Session Overview
  (SlicerOpenLIFU#633, commit 108ccd7).
* Revised (SlicerOpenLIFU#634): Session Overview is information-only.
  Finalize Plan needs a new home. Leading candidate is Pre-Planning
  ("Finalize this Plan" appears once the user has approved a VF for
  a target); alternates are Solution Generator (planning mode) or an
  always-visible workflow-toolbar action. Concrete decision lands
  with the next commit implementing the button.

Semantics:

1. Validate: current session has an approved VF, at least one target,
   volume + protocol + transducer are set.
2. Construct a new `Plan` with:
   * fresh `id` (subject_id + timestamp)
   * subject/volume/protocol/transducer from the session
   * `target` = the target the approved VF is for
   * `array_transform` = the approved VF's transform
   * `pre_solutions` = the session's pre-solutions FILTERED to those that
     used this approved VF (`transducer_transform_source_id` matches).
     Note: these are `SolutionInfo` refs to the subject-scoped solution
     files, not copies of the underlying `.nc`.
   * `parent_planning_session_id` = session id
3. Write the Plan JSON to disk.
4. Append the new Plan's id to `PlanningSession.finalized_plan_ids`.
5. The PlanningSession remains mutable; the user can keep editing and
   finalize a different Plan later.

**Multiple Plans per PlanningSession is expected and supported.** The
Planning Session Overview page shows all finalized Plans in a list
("Plan A — finalized 2026-07-29, Plan B — finalized 2026-07-30, ...").
Each finalize action produces a new immutable Plan; nothing is
overwritten.

Immutability: once written, a Plan's JSON never changes. If the user
wants a modified Plan, they finalize a new one. Deleting a Plan is a
separate operation and only removes the Plan record — pre-solutions it
referenced remain on the PlanningSession.

## 9. Slicer-side pack structures

Mirroring the openlifu-python types:

```python
@parameterPack
class SlicerOpenLIFUPlanningSession:
    session: SlicerOpenLIFUPlanningSessionWrapper  # thin JSON wrapper
    volume_node: vtkMRMLScalarVolumeNode
    target_nodes: List[vtkMRMLMarkupsFiducialNode]
    # No affiliated_photoscans field. No `solutions` field either --
    # pre-solutions live on the openlifu Session's `pre_solutions` list.

@parameterPack
class SlicerOpenLIFUSonicationSession:
    session: SlicerOpenLIFUSonicationSessionWrapper
    plan: SlicerOpenLIFUPlanWrapper       # frozen input, read-only
    volume_node: vtkMRMLScalarVolumeNode
    # No target_nodes -- target is on the Plan, immutable.
```

`OpenLIFUAppState` gets:

```python
loaded_planning_session: Optional[SlicerOpenLIFUPlanningSession]
loaded_sonication_session: Optional[SlicerOpenLIFUSonicationSession]
# Exactly one of these is populated at any time (or neither).
```

`loaded_session` is retired. `loaded_solutions` shrinks to just
"currently-loaded solution objects for the current session" —
Planning-side has `List[SlicerOpenLIFUSolution]` (pre-solutions, loaded
from subject-scoped `solutions/`); Sonication-side has
`Optional[SlicerOpenLIFUSolution]` (the final one).

## 10. What we're NOT doing

* **No migration** of legacy `Session` objects. The old `sessions/`
  directory is left alone. Old sessions are read-only imports if we
  bother; simplest is "start fresh".
* **No cross-session references besides Plan**. A SonicationSession
  references a Plan; that's it. Sonication does not reference
  PlanningSession directly.
* **No shared VFs or pre-solutions between planning sessions**. If two
  PlanningSessions target the same subject, each has its own VFs. Copy
  by hand if needed.
* **No `openlifu.db.Session` compat shim**. Callers of the old
  `db.load_session` get an error asking them to migrate.

## 11. Staging plan

Each row is a commit. Land directly on `v2_ui_refactor` (SlicerOpenLIFU)
and `v2_refactor` (openlifu-python). No sub-branch — since we know
everything comes back to `v2_*` anyway, the sub-branch adds only
overhead.

| # | Commit | Repo | Description |
|---|---|---|---|
| 1 | Add subject-scoped `solutions/` layout for the new types | openlifu-python | `db.write_solution` / `db.load_solution` accept `subject_id`; add index file scheme. Old session `solutions/` path untouched. Tests. |
| 2 | Add `Plan` / `PlanningSession` / `SonicationSession` dataclasses | openlifu-python | Data model + tests. Old `Session` untouched. |
| 3 | Add DB read/write for the three types | openlifu-python | `write_plan`, `load_planning_session`, `load_sonication_session`, subject-scoped photoscan storage. Tests. |
| 4 | Add Slicer-side wrappers + parameter node fields | SlicerOpenLIFU | `SlicerOpenLIFUPlanningSession`, `SlicerOpenLIFUSonicationSession`, `SlicerOpenLIFUPlan`. App state carries both new fields. |
| 5 | Rewrite Data Manager for new types | SlicerOpenLIFU | Two lists (Planning / Sonication), one list of Plans. Old session UI collapsed. |
| 6 | Rewrite Home for two-workflow launch | SlicerOpenLIFU | Two big buttons. |
| 7 | Split Session Overview into Planning Overview + Sonication Overview | SlicerOpenLIFU | Both minimal at first. |
| 8 | Rewrite PrePlanning against PlanningSession | SlicerOpenLIFU | No cross-page cascades. Enter-only refresh. Drift detection at enter(). |
| 9 | Rewrite Solution Generator (formerly Sonication Planner) | SlicerOpenLIFU | Mode-agnostic. Reads session type; writes to right destination. |
| 10 | Rewrite Localization against SonicationSession | SlicerOpenLIFU | No cross-page cascades. |
| 11 | Rewrite Sonication Control against SonicationSession | SlicerOpenLIFU | Load solution from `SonicationSession.solution`. |
| 12 | Add Finalize Plan button + logic | SlicerOpenLIFU | Planning Overview → Finalize → creates Plan (supports multiple Plans per PlanningSession). |
| 13 | Delete old `Session` code paths | SlicerOpenLIFU + openlifu-python | Purge, tests, sample data update. |
| 14 | Update sample database | openlifu-sample-database | Convert one sample subject to have a PlanningSession + Plan + SonicationSession under the new layout. |

Each commit references its issue. Small enough to review. Tests pass at
every commit.

Final merge from `v2_*` to `main` follows the normal process.

## 12. Answered design decisions (2026-07-29)

1. **Multiple Plans per PlanningSession** — SUPPORTED. Planning
   Overview lists all Plans finalized from this session; "Finalize Plan"
   is always available and always produces a new immutable Plan.

2. **Photoscan / Plan / Solution storage location** — SUBJECT-SCOPED.
   All heavy artifacts (photoscans, plans, solutions) live under
   `subjects/{sid}/`. Sessions and plans hold lightweight refs by id.
   This sets us up cleanly for a future relational-DB migration.

3. **SonicationSession volume** — COMES FROM THE PLAN. No `volume_id`
   field on SonicationSession. Loading a Plan directly loads volume +
   target + VF pose. A fresh-scan-at-treatment-time workflow means
   finalizing a new Plan against the new volume (correct semantics).

4. **Solution history on SonicationSession** — ONE SOLUTION PER SESSION.
   `SonicationSession.solution: Optional[SolutionInfo]` (not a list).
   Recompute replaces in place. `Run` history captures what was actually
   delivered. Multi-solution generation + selection (akin to VF favorite
   picking) is a plausible future feature but explicitly out of scope
   for this refactor.

5. **Branching** — Commit directly to `v2_ui_refactor` / `v2_refactor`.
   No `session_split` sub-branch.

## 13. Followups (not in this refactor)

* Full removal of `dataChanged.connect` observers app-wide (some might
  still be legitimate WITHIN a page — evaluate case-by-case).
* Retirement of `deleteSolutionAndSolutionAnalysisIfAny` (target-based
  filter is a stopgap; ideal is fully explicit per-page handling).
* Dirty-flag UI indicators (asterisk on session name).
* PNP shown/hidden by page's `enter()` / `exit()`, not by a persistent
  checkbox setting.
* Yellow color for transducer at an unapproved TT pose (raised in the
  bug tracking that led to this design).
* **Multi-solution generation and selection** on a SonicationSession,
  akin to computing multiple virtual fits and picking a favorite. Would
  change `SonicationSession.solution` from `Optional[SolutionInfo]` to
  a list plus an `active_solution_id` selector.
* Migration to a relational database. The subject-scoped-storage +
  reference-by-id layout in this refactor is designed to be trivially
  portable to a relational schema (`subjects`, `plans`, `solutions`,
  `planning_sessions`, `sonication_sessions`, `runs`, `photoscans` all
  become tables with FK relationships).

## 14. References

* Bug reports and design discussion: PR chain leading to this doc.
* openlifu-python data model files: `src/openlifu/db/session.py`,
  `src/openlifu/db/database.py`.
* SlicerOpenLIFU page files: `OpenLIFU/OpenLIFUApp/pages/*.py`.
* Legacy `Session` remains referenced but deprecated post-split.
