"""Thin Slicer-module shell for the OpenLIFU Transducer Localization page.

The actual widget / logic / test / dialog / wizard / parameter-node classes
live in ``OpenLIFUApp.pages.transducer_localization_page`` (part of the
OpenLIFU host module). This file exists only so that Slicer still registers
``OpenLIFUTransducerLocalization`` as a loadable module, allowing
``slicer.util.selectModule(...)``, ``slicer.util.getModuleWidget(...)``, and
``slicer.util.getModuleLogic(...)`` to keep working during the de-moduling
migration (rounds 2-4 of DEMODULING.md). Round 5 retires this shell once the
host owns page navigation.

Do NOT add ``"OpenLIFU"`` to ``dependencies`` -- the OpenLIFU host module
already depends on OpenLIFUTransducerLocalization; a reverse dep cycles and
hangs Slicer at "Loading OpenLIFU...". ``OpenLIFUApp`` is importable
regardless of load order.
"""

from __future__ import annotations

from slicer.ScriptedLoadableModule import ScriptedLoadableModule
from slicer.i18n import tr as _
from slicer.i18n import translate

# Re-export the extracted classes. Slicer's module discovery finds
# ``<Module>Widget``/``Logic``/``Test`` via attribute lookup on this file,
# and external callers do ``from OpenLIFUTransducerLocalization import
# PhotoscanPreviewDialog`` / ``OpenLIFUTransducerLocalizationTest``.
from OpenLIFUApp.pages.transducer_localization_page import (  # noqa: F401
    AddFromAppDialog,
    AddNewPhotoscanDialog,
    FacialLandmarksMarkupPageBase,
    ImportPhotocollectionFromDiskDialog,
    OpenLIFUTransducerLocalizationLogic,
    OpenLIFUTransducerLocalizationParameterNode,
    OpenLIFUTransducerLocalizationTest,
    OpenLIFUTransducerLocalizationWidget,
    PhotocollectionPreviewDialog,
    PhotoscanFromPhotocollectionDialog,
    PhotoscanGenerationOptionsDialog,
    PhotoscanMarkupPage,
    PhotoscanPreviewDialog,
    PhotoscanRegistrationWizard,
    PhotoscanVolumeTrackingPage,
    SessionQRCodeDialog,
    SkinSegmentationMarkupPage,
    TransducerPhotoscanTrackingPage,
    TransducerTrackingWizard,
)


class OpenLIFUTransducerLocalization(ScriptedLoadableModule):
    """Uses ScriptedLoadableModule base class, available at:
    https://github.com/Slicer/Slicer/blob/main/Base/Python/slicer/ScriptedLoadableModule.py
    """

    def __init__(self, parent):
        ScriptedLoadableModule.__init__(self, parent)
        self.parent.title = _("OpenLIFU Transducer Localization")
        self.parent.categories = [translate("qSlicerAbstractCoreModule", "OpenLIFU.OpenLIFU Modules")]
        self.parent.dependencies = ['OpenLIFUData', "OpenLIFUHome"]
        self.parent.contributors = ["Ebrahim Ebrahim (Kitware), Sadhana Ravikumar (Kitware), Peter Hollender (Openwater), Sam Horvath (Kitware)"]
        self.parent.helpText = _(
            "This is the transducer localization module of the OpenLIFU extension for focused ultrasound. "
            "More information at <a href=\"https://github.com/OpenwaterHealth/SlicerOpenLIFU\">github.com/OpenwaterHealth/SlicerOpenLIFU</a>."
        )
        self.parent.acknowledgementText = _(
            "This is part of Openwater's OpenLIFU, an open-source "
            "hardware and software platform for Low Intensity Focused Ultrasound (LIFU) research "
            "and development."
        )
        # Embedded as a page of the OpenLIFU host module; hide from the modules menu.
        self.parent.hidden = True
