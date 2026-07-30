"""Automated test entry point for the OpenLIFU host module.

The legacy end-to-end workflow test is preserved in
:meth:`OpenLIFUHostTest._OpenLIFU_FullTest1` and reads pages from
``OpenLIFUApp/pages_legacy/``. It will be rewritten to drive the fresh
split-session pages once each of them lands.

Split out of the original monolithic ``OpenLIFU.py`` per the
size-ceiling rule in ``docs/coding-standards.md``.
"""

from __future__ import annotations

import slicer
from slicer.ScriptedLoadableModule import ScriptedLoadableModuleTest

from OpenLIFULib import ensure_python_requirements_for_module_enter


class OpenLIFUHostTest(ScriptedLoadableModuleTest):
    """Full end-to-end workflow integration test.

    Formerly ``OpenLIFUTest`` in the monolithic ``OpenLIFU.py``.

    Split-session refactor status:

    * The ``get_test_database()`` helper (DVC + Google Drive) is
      unchanged and still works.
    * ``_OpenLIFU_FullTest1`` walks the legacy page-test methods under
      ``OpenLIFUApp.pages_legacy.*``. It will fail until each of those
      pages is either rewritten in ``pages/`` or its test is retired.
    """

    def setUp(self) -> None:
        """Reset state before each test -- a scene clear is typically enough."""
        slicer.mrmlScene.Clear()

    def _ensure_dvc_gdrive_support(self) -> None:
        import importlib.util

        dvc_installed = importlib.util.find_spec("dvc") is not None
        gdrive_installed = importlib.util.find_spec("pydrive2") is not None

        if not dvc_installed or not gdrive_installed:
            slicer.util.pip_install("dvc[gdrive]")

    def get_test_database(self) -> str:
        """Download the test database from Google Drive via DVC.

        Setup Requirements:
            * DVC with Google Drive support must be installed in the Slicer
              environment.
            * The path to a Service Account JSON key must be provided via
              the ``DVC_GDRIVE_KEY_PATH`` CMake variable during
              configuration.

        Authentication Flow:
            At configuration time, CMake reads the key file's content into
            the ``GDRIVE_CREDENTIALS_DATA`` environment variable. This
            allows DVC to authenticate headlessly during runtime without
            requiring a manual browser login.
        """
        self._ensure_dvc_gdrive_support()
        import os
        from pathlib import Path
        from dvc.repo import Repo

        dvc_repo_path = os.environ.get("DVC_REPO_DIR")
        if not dvc_repo_path:
            raise OSError("DVC_REPO_DIR environment variable is not set.")

        dvc_repo_path = Path(dvc_repo_path)
        dvc_file = dvc_repo_path / "db_dvc_slicertesting.dvc"
        dvc_config_file = dvc_repo_path / ".dvc" / "config"

        assert dvc_config_file.exists() and dvc_file.exists(), (
            f"DVC file not found at expected location: {dvc_file}"
        )

        try:
            creds = os.environ.get("GDRIVE_CREDENTIALS_DATA")
            if not creds:
                raise OSError(
                    "GDRIVE_CREDENTIALS_DATA environment variable is not set."
                    " DVC cannot authenticate with Google Drive."
                )
            repo = Repo(str(dvc_repo_path), uninitialized=True)
            repo.pull(targets=[str(dvc_file)], force=True)
        except Exception as e:
            raise RuntimeError(f"An error occurred during dvc pull: {e}") from e

        return str(dvc_repo_path / "db_dvc_slicertesting")

    def runTest(self) -> None:
        """Run the full workflow integration test."""
        from OpenLIFULib import check_and_install_kwave_binaries

        ensure_python_requirements_for_module_enter()
        check_and_install_kwave_binaries()

        self.setUp()

        db_path = self.get_test_database()

        self._OpenLIFU_FullTest1(db_path=db_path)

    def _OpenLIFU_FullTest1(self, db_path: str) -> None:
        """Legacy full workflow test.

        Walks the pre-split page-test methods under ``pages_legacy``.
        Will be replaced by a fresh test suite as each split-session page
        lands (Planning Session Overview + Sonication Session Overview +
        PrePlanning + Solution Generator + Localization + Sonication
        Control).
        """
        from OpenLIFUApp.pages_legacy.database_page import OpenLIFUDatabaseTest
        dbt = OpenLIFUDatabaseTest()
        dbt.connect_database(database_dir=db_path)

        from OpenLIFUApp.pages_legacy.data_page import OpenLIFUDataTest
        dt = OpenLIFUDataTest()
        dt.load_subject_session()

        from OpenLIFUApp.pages_legacy.session_page import OpenLIFUSessionTest
        st = OpenLIFUSessionTest()
        st.workflow_session_dashboard()

        from OpenLIFUApp.pages_legacy.preplanning_page import OpenLIFUPrePlanningTest
        pt = OpenLIFUPrePlanningTest()
        pt._workflow_virtual_fit()

        from OpenLIFUApp.pages_legacy.transducer_localization_page import OpenLIFUTransducerLocalizationTest
        tlt = OpenLIFUTransducerLocalizationTest()
        tlt._workflow_localization()

        from OpenLIFUApp.pages_legacy.sonication_planner_page import OpenLIFUSonicationPlannerTest
        spt = OpenLIFUSonicationPlannerTest()
        spt._workflow_planning()

        from OpenLIFUApp.pages_legacy.sonication_control_page import OpenLIFUSonicationControlTest
        sct = OpenLIFUSonicationControlTest()
        sct._workflow_sonication_control()
