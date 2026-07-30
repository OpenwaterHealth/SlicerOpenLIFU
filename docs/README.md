# SlicerOpenLIFU documentation

Living documentation set for the split-session refactor and the
IEC 62304 traceability path.

## Contents

* [`architecture.md`](architecture.md) — high-level block diagram,
  page navigation, session lifecycle, enter/refresh contract,
  file layout, IEC 62304 traceability plan.
* [`data-model.md`](data-model.md) — the split-session data model:
  class diagrams for the openlifu-python dataclasses and the Slicer
  parameter packs, database directory layout, extension naming
  convention, save semantics.
* [`coding-standards.md`](coding-standards.md) — rules for module
  size, class/function naming, docstring policy, cross-page
  communication policy, dependency direction.
* [`pages/README.md`](pages/README.md) — per-page documentation index.
  * [`pages/host.md`](pages/host.md) — the host module: entry point,
    subpackage structure, startup flow, host widget + host logic
    public API.
  * [`pages/home.md`](pages/home.md) — Home page.
  * [`pages/data-manager.md`](pages/data-manager.md) — Data Manager
    page.
  * [`pages/planning-session-overview.md`](pages/planning-session-overview.md)
    — Planning Session Overview page (hosts the **Finalize Plan** action).
  * [`pages/sonication-session-overview.md`](pages/sonication-session-overview.md)
    — Sonication Session Overview page.

## Related

* [`../SESSION_SPLIT_DESIGN.md`](../SESSION_SPLIT_DESIGN.md) — the
  design-decision record for the refactor. Read this alongside
  `architecture.md` and `data-model.md`.

## Editing conventions

* Diagrams are Mermaid, rendered inline by any Mermaid-aware
  viewer (VS Code + Markdown Preview, GitHub, most static-site
  generators).
* Documents are "living" — updated as pages land or design
  decisions change. Each document's top block records the last
  major update date and what changed.
