# Split-session data model

Living document. Complements
[`../SESSION_SPLIT_DESIGN.md`](../SESSION_SPLIT_DESIGN.md) (the
design-decision record) with concrete diagrams and field-level
references.

## Class diagram — openlifu-python side

```mermaid
classDiagram
    class Plan {
        +id: str
        +name: str
        +subject_id: str
        +volume_id: str
        +protocol_id: str
        +transducer_id: str
        +target: Point
        +array_transform: ArrayTransform
        +pre_solutions: List~SolutionInfo~
        +parent_planning_session_id: str
        +date_created: datetime
        +notes: str
        +attrs: dict
    }

    class PlanningSession {
        +id: str
        +name: str
        +subject_id: str
        +volume_id: str
        +protocol_id: str
        +transducer_id: str
        +date_created: datetime
        +date_modified: datetime
        +targets: List~Point~
        +virtual_fit_results: Dict~str, List~
        +pre_solutions: List~SolutionInfo~
        +finalized_plan_ids: List~str~
        +attrs: dict
    }

    class SonicationSession {
        +id: str
        +name: str
        +subject_id: str
        +plan_id: str
        +date_created: datetime
        +date_modified: datetime
        +photoscan_ids: List~str~
        +photoscan_registrations: List~PhotoscanRegistration~
        +transducer_tracking_results: List~TransducerTrackingResult~
        +solution: Optional~SolutionInfo~
        +run_ids: List~str~
        +attrs: dict
    }

    class SolutionInfo {
        +solution_id: str
        +protocol_id: str
        +target_id: str
        +transducer_id: str
        +transducer_transform_source: str
        +transducer_transform_source_id: str
        +approved: bool
        +computed_at: datetime
        +array_transform: ArrayTransform
    }

    class Solution {
        +id: str
        +...
    }

    PlanningSession "1" --> "*" Plan : finalizes (finalized_plan_ids)
    Plan "*" --> "1" PlanningSession : parent (parent_planning_session_id)
    Plan "1" --> "*" SonicationSession : referenced by (plan_id)
    PlanningSession o-- "*" SolutionInfo : pre_solutions
    Plan o-- "*" SolutionInfo : pre_solutions
    SonicationSession o-- "1" SolutionInfo : solution
    SolutionInfo --> Solution : refers to by solution_id
```

Key invariants (enforced or asserted by design):

* `Plan.parent_planning_session_id` is provenance only. A Plan does
  not depend on its parent PlanningSession still existing.
* A single Solution (on disk at
  `subjects/{sid}/solutions/{solution_id}/{solution_id}.solution.json`)
  can be referenced by any number of `SolutionInfo` records across
  a PlanningSession, a Plan finalized from it, and a
  SonicationSession that consumed it. Solution files are not
  duplicated per referrer.
* `SonicationSession.solution` is `Optional[SolutionInfo]` — at
  most one final solution per session. Recompute replaces in place.

## Class diagram — Slicer side (parameter packs)

```mermaid
classDiagram
    class SlicerOpenLIFUPlanWrapper {
        +plan: openlifu.db.Plan
    }
    class SlicerOpenLIFUPlanningSessionWrapper {
        +planning_session: openlifu.db.PlanningSession
    }
    class SlicerOpenLIFUSonicationSessionWrapper {
        +sonication_session: openlifu.db.SonicationSession
    }

    class SlicerOpenLIFUPlan {
        +plan: SlicerOpenLIFUPlanWrapper
        --
        +get_plan_id()
        +get_subject_id()
        +get_volume_id()
        +get_target()
    }
    class SlicerOpenLIFUPlanningSession {
        +session: SlicerOpenLIFUPlanningSessionWrapper
        +volume_node: vtkMRMLScalarVolumeNode
        +target_nodes: List~vtkMRMLMarkupsFiducialNode~
        --
        +get_planning_session_id()
        +get_target_nodes()
    }
    class SlicerOpenLIFUSonicationSession {
        +session: SlicerOpenLIFUSonicationSessionWrapper
        +plan: SlicerOpenLIFUPlanWrapper
        +volume_node: vtkMRMLScalarVolumeNode
        --
        +get_sonication_session_id()
        +get_plan_id()
        +get_volume_id()
    }

    SlicerOpenLIFUPlan *-- SlicerOpenLIFUPlanWrapper
    SlicerOpenLIFUPlanningSession *-- SlicerOpenLIFUPlanningSessionWrapper
    SlicerOpenLIFUSonicationSession *-- SlicerOpenLIFUSonicationSessionWrapper
    SlicerOpenLIFUSonicationSession *-- SlicerOpenLIFUPlanWrapper : plan (frozen)
```

Why the two-level nesting (parameterPack → thin wrapper → openlifu
object):

* Slicer's `@parameterNodeWrapper` machinery uses `typing.get_type_hints()`
  at class-definition time. The thin wrapper (`SlicerOpenLIFU*Wrapper`)
  gives Slicer a class name it can resolve without importing
  `openlifu.db` — important because `openlifu` may not yet be
  installed when Slicer first imports our modules.
* The parameterPack layer adds the Slicer scene fields (volume node,
  target fiducials) that don't belong on the openlifu dataclass.

Access pattern: `slicer_session.session.planning_session.<field>`.
Three levels: pack → wrapper → openlifu object.

## Database directory layout

```
{db_root}/
  users/
    users.json
    {user_id}/{user_id}.json
  protocols/
    protocols.json
    {protocol_id}/{protocol_id}.json
  transducers/
    transducers.json
    {transducer_id}/{transducer_id}.json
    {transducer_id}/{transducer_id}_gridweights_{hash}.h5
  subjects/
    subjects.json
    {subject_id}/
      {subject_id}.json
      volumes/
        volumes.json
        {volume_id}/{volume_id}.json + {volume_id}.nii
      photoscans/
        photoscans.json
        {photoscan_id}/{photoscan_id}.json + .obj + .mtl + .png
      photocollections/
        photocollections.json
        {ref_number}/{ref_number}_*.jpg
      solutions/
        solutions.json
        {solution_id}/{solution_id}.solution.json + .solution_analysis.json + .nc
      plans/
        plans.json
        {plan_id}/{plan_id}.plan.json
      planning_sessions/
        planning_sessions.json
        {planning_session_id}/{planning_session_id}.planning.json
      sonication_sessions/
        sonication_sessions.json
        {sonication_session_id}/
          {sonication_session_id}.sonication.json
          runs/{rid}/{rid}.json
```

Heavy artifacts (volumes, photoscans, solutions) live subject-scoped.
Sessions and plans hold `SolutionInfo` refs and id lists; the same
underlying file is never duplicated per referrer.

## Extension naming convention

The type discriminator lives in the filename extension, not in an
id suffix:

| Kind               | Filename                          | Rationale |
|--------------------|-----------------------------------|-----------|
| PlanningSession    | `{id}.planning.json`              | Distinguishes from Plan / SonicationSession sharing the same id. |
| Plan               | `{id}.plan.json`                  | Same. |
| SonicationSession  | `{id}.sonication.json`            | Same. |
| Solution           | `{id}.solution.json`              | Distinguishes from analysis JSON. |
| SolutionAnalysis   | `{id}.solution_analysis.json`     | Companion to `.solution.json`. |

The same id (e.g. `neuromod_1x_demo`) appears on the PlanningSession,
the Plan finalized from it, and the SonicationSession launched from
that Plan.

## Save semantics

**Session JSON is written to disk ONLY on explicit user save. Memory
is the source of truth for the loaded session.**
See SlicerOpenLIFU#636 for the design rationale and the split
build / open / save API in `OpenLIFUApp.logic.session_actions`.

* "New Planning Session" and "New Sonication Session" (Home + Data
  Manager) build the openlifu object in memory via
  `session_actions.build_*_session(...)`, open it into the app state
  via `session_actions.open_*_session_into_app(...)`, and set
  `OpenLIFUAppState.session_is_dirty = True` so that a later Save
  actually writes. No disk write during creation.
* `set_solution` / `clear_solution` / target moves / photoscan
  add / photoscan remove all mutate the in-memory session and set
  `OpenLIFUAppState.session_is_dirty = True` (via
  `OpenLIFULib.util.mark_session_dirty`), but do NOT touch disk.
* The host's toolbar "Save" and Data Manager's "Save" button both
  call `session_actions.save_loaded_session()`, which writes the
  loaded PlanningSession + SonicationSession (whichever are non-null)
  via `db.write_*_session(..., on_conflict="overwrite")` and clears
  `session_is_dirty`.
* Exit / Back-to-Home / any other "replace the loaded session"
  action calls `session_actions.prompt_save_before_replacing_loaded_session()`
  first, so unsaved changes surface a Save / Discard / Cancel
  dialog rather than silently vanishing.
* `session_actions.close_loaded_sessions()` NEVER writes -- it's the
  "Discard" primitive. This is what makes memory-first work: if the
  user builds a new session and exits without saving, close_loaded
  drops it and disk stays untouched.
* Solution binary artifacts (`.nc`, `.solution_analysis.json`) are
  derived data. They CAN be written eagerly at compute time; the
  session JSON's `SolutionInfo` entries reference them.

## Related documents

* [`architecture.md`](architecture.md) — module-level layout.
* [`coding-standards.md`](coding-standards.md) — file organisation,
  naming, docstrings.
* [`../SESSION_SPLIT_DESIGN.md`](../SESSION_SPLIT_DESIGN.md) — design
  decisions, staging plan, open questions.
