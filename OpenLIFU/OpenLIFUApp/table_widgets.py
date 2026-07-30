"""Qt table widget helpers shared across split-session pages.

Extracted from ``pages/data_manager_page.py`` when the Planning /
Sonication Session Overview pages needed the same fixed-width table +
tooltip convention. Per ``docs/coding-standards.md`` rule 9 these live
under ``OpenLIFUApp`` (not ``OpenLIFULib``) because they only serve
pages inside this Slicer extension.

Exports:

* ``ColumnSpec`` -- type alias for a list of ``(header, width_px)``
  tuples describing a table's columns.
* ``make_fixed_width_table(columns)`` -- create a ``QTableWidget`` with
  fixed default column widths, interactive resize mode, single-row
  selection, alternating row colours.
* ``fill_table_with_tooltips(table, rows)`` -- populate a table +
  set each cell's tooltip to its full text.
* ``SignalBlocker(obj)`` -- context manager that blocks Qt signals.
"""

from __future__ import annotations

from typing import List, Optional, Sequence

import qt


# Column-config type. ``(header, default_width_px)`` per column.
ColumnSpec = List[tuple]


class SignalBlocker:
    """Context manager that blocks Qt signals on a ``QObject``.

    Use around programmatic mutation of a combo box or other widget
    when you do NOT want your own signal handlers to fire.
    """

    def __init__(self, obj):
        self.obj = obj
        self.previous: Optional[bool] = None

    def __enter__(self):
        self.previous = self.obj.blockSignals(True)
        return self.obj

    def __exit__(self, *exc_info):
        if self.previous is not None:
            self.obj.blockSignals(self.previous)


def make_fixed_width_table(columns: ColumnSpec) -> qt.QTableWidget:
    """Create a ``QTableWidget`` with fixed-by-default resizable column widths.

    Column headers use ``QHeaderView.Interactive`` resize mode so the
    user can drag to resize but nothing auto-adjusts to content -- this
    avoids the "content changed, columns jumped" problem you get with
    the default ``ResizeToContents`` mode.

    Args:
        columns: list of ``(header, default_width_px)`` tuples, one per column.
    """
    table = qt.QTableWidget()
    table.setColumnCount(len(columns))
    table.setHorizontalHeaderLabels([header for header, _ in columns])
    table.setSelectionBehavior(qt.QAbstractItemView.SelectRows)
    table.setSelectionMode(qt.QAbstractItemView.SingleSelection)
    table.setEditTriggers(qt.QAbstractItemView.NoEditTriggers)
    table.verticalHeader().setVisible(False)
    table.setAlternatingRowColors(True)

    header = table.horizontalHeader()
    header.setSectionResizeMode(qt.QHeaderView.Interactive)
    header.setStretchLastSection(True)
    for index, (_, width) in enumerate(columns):
        table.setColumnWidth(index, int(width))

    table.setMinimumHeight(120)
    return table


def fill_table_with_tooltips(table: qt.QTableWidget, rows: Sequence[tuple]) -> None:
    """Populate ``table`` with ``rows`` and put each cell's full text
    into its tooltip so long strings can be hovered."""
    table.setRowCount(len(rows))
    for row_index, row in enumerate(rows):
        for col_index, value in enumerate(row):
            text = "" if value is None else str(value)
            item = qt.QTableWidgetItem(text)
            item.setToolTip(text)
            table.setItem(row_index, col_index, item)
