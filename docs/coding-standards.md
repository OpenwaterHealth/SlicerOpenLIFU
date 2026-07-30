# Coding standards — SlicerOpenLIFU

Living document. Adopted as the split-session refactor
(SlicerOpenLIFU#631) reworks the module bottom-up. The rules here
apply to every file created or substantially rewritten from
2026-07-30 onward. Legacy files under `OpenLIFUApp/pages_legacy/`
are frozen and will be deleted whole; they are not being retrofitted
to this guide.

## Why this document exists

SlicerOpenLIFU will need to pass IEC 62304 conformance for the
device class it eventually ships in. Every source-file organisation
choice, every naming convention, and every docstring policy in this
document is chosen to make that certification path easier:

* **Traceability** — every function is discoverable and callable
  from a test.
* **Readability** — a reviewer without prior familiarity should be
  able to open any single file and understand it top-to-bottom.
* **Simplicity** — fewer moving parts, smaller files, one clear
  purpose per file / class / function.
* **Testability** — logic and UI are separable; scripted tests
  never need to hunt through 6000-line files to find a helper.

Failure modes we are trying to prevent:

* "Where does this feature live?" — one page file shouldn't own
  1700 lines of unrelated dialog code.
* "Is this method private or just underscore-prefixed?" — the
  answer should be *"if it's on the class, it's callable; the
  underscore is just noise"*.
* "Can I unit-test this behaviour?" — yes, every method that
  changes state has a name a test can invoke.

## Rule 1: one topic per file

Rough size ceilings for new files:

* Page files (widget + logic + dialogs it uses): **≤ 1500 lines**.
  Prefer 500-800.
* Helper modules under `OpenLIFULib`: **≤ 500 lines**.
* Host-module internals (`OpenLIFUApp/host/*.py`): **≤ 500 lines**
  each.

If a file threatens to cross these limits, split it. A shared dialog
belongs in its own file under a `dialogs/` directory, not in the
page that first opened it.

## Rule 2: naming conventions

### Modules and packages

`snake_case`, no leading underscores. Example:
`data_manager_page.py`, not `_data_manager.py`.

### Classes

`PascalCase`, no leading underscores. Example:
`OpenLIFUDataManagerWidget`, `SignalBlocker`, not `_SignalBlocker`.

If a helper class is used only inside one file, it is still a
concrete class the code depends on. Name it clearly and drop the
underscore.

### Methods and functions

`snake_case`, no leading underscores.

`_leading_underscore` in Python signals "private by convention, do
not import from outside". In this codebase every method on a
class is called from somewhere; there is no external API to hide
things from. The underscore is misleading noise. Drop it.

Genuine exceptions:

* `__init__`, `__enter__`, `__exit__`, and other Python protocol
  dunders. These are language-level, not by-convention.
* Names that shadow a builtin or a Qt signal (e.g. `list`, `id`)
  may prefix with a single underscore to disambiguate.
* Module-level constants meant not to be re-exported may use
  `_UNDERSCORE_CONSTANT` (rare).

### Variables

Local variables and instance attributes: `snake_case`, no leading
underscores.

Private instance attributes are still `snake_case` — the
"privateness" is signalled by keeping them out of the class's public
docstring, not by a name mangling convention.

### Booleans

Prefer `is_x` / `has_y` / `should_z` over bare adjectives.
Example: `is_entered`, not `entered`.

### Signal handlers

Handlers connected to Qt signals get an `on_` prefix and match the
widget or event they react to. Example:

```python
self.load_button.clicked.connect(self.on_load_button_clicked)
```

Not `_on_load_clicked` (that's the old underscore convention) and
not `handle_load` (unclear which widget).

## Rule 3: docstring policy

Every public class and every public method gets a docstring. The
docstring answers **why**, not **what**. The name should tell you
what; the docstring tells you why it exists.

Bad:

```python
def refresh_subjects(self):
    """Refresh the subjects."""
```

Good:

```python
def refresh_subjects(self):
    """Repopulate the subject combo box from the loaded database.

    Called from ``enter()`` and after every database load / refresh.
    Preserves the current selection when possible so navigating away
    and back does not reset the user's context.
    """
```

Module-level docstrings at the top of every file explain the file's
one-topic scope and the design rationale for its existence as a
separate file.

## Rule 4: enter / refresh contract for pages

Every page follows this contract (also documented in
[`architecture.md`](architecture.md), section 5):

* `enter()` is the SOLE source of first-render truth. Every
  widget's contents are constructed from the current app state and
  database at `enter()` time.
* `exit()` marks the page as not entered but does NOT tear down
  state — leaves everything in place for the next `enter()`.
* Signal handlers (button clicks, combo box changes) do the local
  mutation and call `refresh_local()` directly. No
  `dataChanged.connect(...)` observers wired across pages.
* Drift between Slicer scene objects and their session-state
  representations is detected in `enter()` and reconciled at the
  session-data-model layer, not through a UI observer chain.

## Rule 5: logic separated from widgets

Every page has a `Logic` class that owns its business logic and can
be constructed / driven without the widget existing. Widget code
translates user actions into logic calls; nothing else.

Verification tests import the `Logic` class directly and drive it.
They do NOT click through widgets.

## Rule 6: no cross-page reach-ins

A page NEVER reaches into another page's widget or logic. If two
pages need to coordinate, they do it through:

* `OpenLIFUAppState` (the parameter node) for shared state.
* `openlifu.db.Database` (via `get_cur_db()`) for persisted state.
* `slicer.mrmlScene` for scene objects.

Ping-pong via `slicer.util.getModuleLogic("OpenLIFU").<page>_logic`
into another page's methods is not an approved coordination
mechanism.

## Rule 7: shared dialogs go in a `dialogs/` package

If a QDialog is used from more than one place, extract it into
`OpenLIFUApp/dialogs/{name}_dialog.py`. Dialogs shared across
multiple pages are the single biggest source of the "the page file
owns 6000 lines" problem in the legacy tree.

Dialogs used from exactly one place may live in that place's file,
but with a class-level docstring saying so, so future readers know
the encapsulation contract.

## Rule 8: cleanup is the caller's job

Slicer's lifecycle model gives us `enter()` and `exit()` per page
and `cleanup()` per widget. We use them:

* `enter()` doesn't allocate persistent resources — it just reads
  state and paints.
* Anything explicitly created (nodes added to `slicer.mrmlScene`)
  is torn down by the code path that unloaded the session (e.g.
  `session_actions.close_loaded_sessions`).
* `cleanup()` is only for module-teardown / Slicer-shutdown wind-up.

## Rule 9: dependencies flow one direction

```
openlifu (data)  <---  OpenLIFULib (wrappers)  <---  OpenLIFUApp (pages)
```

Nothing in `openlifu-python` imports from SlicerOpenLIFU.
Nothing in `OpenLIFULib` imports from `OpenLIFUApp`.
Pages may import from `OpenLIFULib` and from `openlifu`.

Cycles are a hard bug in this codebase and cost hours to unwind.

## Applies-to reference

* Fresh files (post 2026-07-30): full compliance required.
* Legacy files under `OpenLIFUApp/pages_legacy/`: frozen; will be
  deleted when the last fresh replacement lands.
* `OpenLIFULib` files: to be brought into compliance file-by-file as
  they are touched. Nothing there is being retrofitted preemptively.
