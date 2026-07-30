# Per-page documentation

Living documentation for each embedded page in the OpenLIFU host module.
Each document describes the page's purpose, its screen layout, the
signals and state transitions it drives, and its acceptance tests.

## Fresh split-session pages

* [`home.md`](home.md) — Landing page. Auto-connects to the
  last-used database and hosts the primary launch buttons for
  New / Continue × Planning / Sonication sessions
  (SlicerOpenLIFU#635). Also read-only status labels and a link to
  the Data Manager for admin actions.
* [`data-manager.md`](data-manager.md) — CRUD surface for
  PlanningSessions / Plans / SonicationSessions / Solutions (subject
  tab) and Protocols / Transducers / Users (database tabs).
* [`planning-session-overview.md`](planning-session-overview.md) —
  Information-only status card for a loaded PlanningSession.
* [`sonication-session-overview.md`](sonication-session-overview.md) —
  Information-only status card for a loaded SonicationSession, with
  a read-only view of its frozen Plan.

Planned (pages not yet built):

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
