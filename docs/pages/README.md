# Per-page documentation

Living documentation for each embedded page in the OpenLIFU host module.
Each document describes the page's purpose, its screen layout, the
signals and state transitions it drives, and its acceptance tests.

## Fresh split-session pages

* [`home.md`](home.md) — Landing page. Read-only status of database
  + loaded sessions; single button navigating to the Data Manager.
* [`data-manager.md`](data-manager.md) — CRUD surface for
  PlanningSessions / Plans / SonicationSessions / Solutions (subject
  tab) and Protocols / Transducers / Users (database tabs).

Planned (pages not yet built):

* `planning-session-overview.md` — status card + "Finalize Plan"
  button for a loaded `PlanningSession`.
* `sonication-session-overview.md` — status card + Plan reference
  for a loaded `SonicationSession`.
* `pre-planning.md` — target placement + virtual-fit UI for a
  `PlanningSession`.
* `solution-generator.md` — compute Solutions (mode-agnostic;
  callable from either overview).
* `localization.md` — photoscan registration + transducer
  tracking for a `SonicationSession`.
* `sonication-control.md` — hardware interface for a
  `SonicationSession`.

## Related

* [`../architecture.md`](../architecture.md) — module-level layout
  and page navigation flow.
* [`../data-model.md`](../data-model.md) — the split-session data
  model that pages consume.
* [`../coding-standards.md`](../coding-standards.md) — page-level
  rules (enter/refresh contract, logic-vs-widget separation).
