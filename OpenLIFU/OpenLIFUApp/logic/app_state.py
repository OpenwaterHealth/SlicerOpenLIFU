from __future__ import annotations

from slicer.parameterNodeWrapper import parameterNodeWrapper


@parameterNodeWrapper
class OpenLIFUAppState:
    """Consolidated host parameter node — populated in later de-moduling rounds.

    Round 4 folds Data's ``loaded_*`` fields onto this class and deletes the
    per-module parameter nodes. Currently empty scaffolding.
    """
