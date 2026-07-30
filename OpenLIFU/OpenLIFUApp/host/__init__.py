"""OpenLIFU host module internals.

The host module (``OpenLIFU``, defined in ``OpenLIFU/OpenLIFU.py``)
delegates its widget, logic, and test class implementations to this
subpackage:

* ``page_registry``: the ``Page`` descriptor and ``PAGE_DEFS`` list.
* ``timeline_widget``: the custom-painted timeline strip drawn along
  the bottom of the host module.
* ``host_widget``: the main widget that owns the QStackedWidget of
  pages.
* ``host_logic``: the module-level Logic class exposing per-page
  logic instances (``home_logic``, ``data_manager_logic``,
  ``database_logic``).
* ``host_test``: the module's automated test entry point.

See ``docs/architecture.md`` and ``docs/coding-standards.md`` for the
design rationale.
"""
