from __future__ import annotations

from typing import Dict, Optional

from slicer.parameterNodeWrapper import parameterNodeWrapper

from OpenLIFULib import (
    SlicerOpenLIFUPhotoscan,
    SlicerOpenLIFUProtocol,
    SlicerOpenLIFURun,
    SlicerOpenLIFUSession,
    SlicerOpenLIFUSolution,
    SlicerOpenLIFUTransducer,
)


@parameterNodeWrapper
class OpenLIFUAppState:
    """Consolidated host parameter node for the OpenLIFU app.

    Owns the ``loaded_*`` fields formerly declared on
    ``OpenLIFUDataParameterNode`` (Round 4b of DEMODULING.md). Additional
    workflow state migrates here in later rounds; ``get_app_state()``
    and ``OpenLIFUDataLogic.getParameterNode()`` both return an
    ``OpenLIFUAppState`` wrapper during the transition.
    """
    loaded_protocols : "Dict[str,SlicerOpenLIFUProtocol]"
    loaded_transducers : "Dict[str,SlicerOpenLIFUTransducer]"
    loaded_solution : "Optional[SlicerOpenLIFUSolution]"
    loaded_session : "Optional[SlicerOpenLIFUSession]"
    loaded_run: "Optional[SlicerOpenLIFURun]"
    loaded_photoscans: "Dict[str,SlicerOpenLIFUPhotoscan]"
