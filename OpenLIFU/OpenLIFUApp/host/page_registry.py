"""Page registry for the OpenLIFU host module.

Defines the ``Page`` descriptor (one per stacked page in the host's
QStackedWidget) and the ordered ``PAGE_DEFS`` list. The host widget
constructs its own copy of each ``Page`` at startup and drives page
transitions through ``show_page(module_name)``.

Split-session refactor (SlicerOpenLIFU#631): only new-model pages are
active in ``PAGE_DEFS``. Legacy pages live in
``OpenLIFU/OpenLIFUApp/pages_legacy/`` for reference until the fresh
replacements land.

See ``docs/architecture.md`` section 3 (page navigation) and section 6
(file layout).
"""

from __future__ import annotations

from typing import List, Optional

import qt


class Page:
    """One embedded page inside the host's ``QStackedWidget``.

    Attributes:
        key: the underlying Slicer module name (e.g. ``"OpenLIFUHome"``).
             Used as the lookup key in the host widget's page dict and
             in ``show_page(module_name)`` calls.
        label: short human-readable label. Shown on the timeline strip
               for on-timeline pages; ignored for off-timeline pages
               (Home, Data Manager) that are reached via in-page navigation.
        on_timeline: True if the page participates in the workflow timeline
                     shown along the bottom of the host module. Off-timeline
                     pages are embedded but not shown on the strip.
        container: the ``QWidget`` inserted into the QStackedWidget. Set
                   by ``embed_all_pages`` once the widget has been
                   instantiated. ``None`` before embedding.
    """

    def __init__(self, key: str, label: str, on_timeline: bool) -> None:
        self.key = key
        self.label = label
        self.on_timeline = on_timeline
        self.container: Optional[qt.QWidget] = None


# Ordered list of pages the host module embeds. Anything ``on_timeline=True``
# appears in the footer timeline in the order given here. Pages with
# ``on_timeline=False`` are embedded but not shown on the workflow timeline
# (Home, Data Manager) -- they are reachable via the in-module navigation
# that lives on those pages.
#
# Legacy pages have been retired; when a fresh replacement for a legacy
# timeline page lands (Planning Session Overview, Sonication Session
# Overview, PrePlanning, Solution Generator, Localization, Sonication
# Control) it will be appended here.
#
# Timeline registration for the Session Overview pages is deferred until
# the timeline strip supports mode switching (planning-workflow vs
# sonication-workflow strips) -- for now the Overview pages are
# ``on_timeline=False`` and are reached from the Data Manager after a
# successful load (SlicerOpenLIFU#633).
PAGE_DEFS: List[Page] = [
    Page("OpenLIFUHome",                       "Home",                        on_timeline=False),
    Page("OpenLIFUDataManager",                "Data Manager",                on_timeline=False),
    Page("OpenLIFUPlanningSessionOverview",    "Planning Session Overview",   on_timeline=False),
    Page("OpenLIFUSonicationSessionOverview",  "Sonication Session Overview", on_timeline=False),
    # First page of the Planning workflow timeline (SlicerOpenLIFU#641).
    # Virtual Fit and Solution Generator will join it as they land; the
    # timeline strip shows the ordered sequence and the Next button
    # advances through it.
    Page("OpenLIFUTargetSelection",            "Target Selection",            on_timeline=True),
]
