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
# timeline page lands (Virtual Fit, Solution Generator, Localization,
# Sonication Control) it will be appended here.
#
# Planning Session Overview is the FIRST timeline step for a
# PlanningSession (SlicerOpenLIFU#642) -- it's the info-only status
# card users land on after "New / Continue Planning Session", and the
# timeline strip then advances into Target Selection etc.
#
# Sonication Session Overview stays off-timeline for now: the timeline
# strip currently renders a single ordered sequence, and mixing the
# Planning and Sonication workflows in one strip would confuse users
# (deferred per SlicerOpenLIFU#633). When the Sonication workflow pages
# (Localization / Sonication Control) land, we'll decide whether to
# switch the strip based on the loaded session type or add a per-type
# timeline separator.
PAGE_DEFS: List[Page] = [
    Page("OpenLIFUHome",                       "Home",                        on_timeline=False),
    Page("OpenLIFUDataManager",                "Data Manager",                on_timeline=False),
    # Sonication workflow -- overview stays off-timeline until the
    # sonication pages (Localization, Sonication Control) land and we
    # decide how to switch the timeline strip between the Planning and
    # Sonication workflow shapes. Until then, users on a
    # SonicationSession still navigate via the footer's Back-to-Home.
    Page("OpenLIFUSonicationSessionOverview",  "Sonication Session Overview", on_timeline=False),
    # Planning workflow timeline. Overview is the first stop; the
    # timeline strip's second circle advances into Target Selection.
    # Virtual Fit / Solution Generator join here as they land
    # (SlicerOpenLIFU#642).
    Page("OpenLIFUPlanningSessionOverview",    "Planning Session Overview",   on_timeline=True),
    Page("OpenLIFUTargetSelection",            "Target Selection",            on_timeline=True),
]
