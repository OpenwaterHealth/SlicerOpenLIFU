"""Check dependency provenance without starting Slicer or installing packages."""

import importlib.metadata
import importlib.util
import json
from pathlib import Path
import sys
import tempfile
from types import ModuleType
import unittest
from unittest.mock import patch


def load_dependency_utils():
    source = Path(__file__).parents[2] / "OpenLIFULib" / "dependency_utils.py"
    stubs = {name: ModuleType(name) for name in (
        "slicer", "qt", "OpenLIFULib", "OpenLIFULib.install_asset_dialog", "OpenLIFULib.util",
    )}
    stubs["OpenLIFULib.install_asset_dialog"].InstallAssetDialog = object
    stubs["OpenLIFULib.util"].BusyCursor = object
    spec = importlib.util.spec_from_file_location("dependency_utils_under_test", source)
    module = importlib.util.module_from_spec(spec)
    with patch.dict(sys.modules, stubs):
        spec.loader.exec_module(module)
    return module


dependency_utils = load_dependency_utils()


class DependencyVersionTest(unittest.TestCase):
    repository = "https://github.com/OpenwaterHealth/openlifu-python.git"
    branch = "transducer-manager-library-components"
    commit = "1ca1c07d9869290136699848fd2cb08396864e78"

    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        root = Path(directory.name)
        resources = root / "Resources"
        resources.mkdir()
        self.requirements = resources / "python-requirements.txt"
        self.requirements.write_text("")
        distribution_path = root / "openlifu-0.22.0.dist-info"
        distribution_path.mkdir()
        self.metadata_path = distribution_path / "METADATA"
        self.metadata_path.write_text("Name: openlifu\nVersion: 0.22.0.dev5+g1ca1c07\n")
        self.source_path = distribution_path / "direct_url.json"
        installed = importlib.metadata.PathDistribution(distribution_path)
        for context in (
            patch.object(dependency_utils, "__file__", str(root / "dependency_utils.py")),
            patch("importlib.metadata.distribution", return_value=installed),
        ):
            context.start()
            self.addCleanup(context.stop)

    def require_git(self, revision=None):
        self.requirements.write_text(f"openlifu[app] @ git+{self.repository}@{revision or self.branch}\n")

    def record_git(self, revision=None, commit=None, repository=None):
        self.source_path.write_text(json.dumps({
            "url": repository or self.repository,
            "vcs_info": {
                "vcs": "git",
                "requested_revision": revision or self.branch,
                "commit_id": commit or self.commit,
            },
        }))

    def test_release_versions_and_extras(self):
        for required, installed, matches in (
            ("v0.21.1", "0.21.1", True),
            ("0.21.1", "0.21.0", False),
            ("0.22.0.dev5", "0.22.0.dev5", True),
        ):
            with self.subTest(required=required, installed=installed):
                self.requirements.write_text(f"# Dependencies\nopenlifu[app] == {required} # pinned\n")
                self.metadata_path.write_text(f"Name: openlifu\nVersion: {installed}\n")
                self.assertEqual(required, dependency_utils.get_required_openlifu_version())
                self.assertEqual(matches, dependency_utils.openlifu_version_matches())

    def test_branch_uses_recorded_source_instead_of_version_string(self):
        self.require_git()
        self.assertEqual(self.branch, dependency_utils.get_required_openlifu_version())
        self.assertFalse(dependency_utils.openlifu_version_matches())
        self.record_git()
        self.assertTrue(dependency_utils.openlifu_version_matches())
        self.record_git(revision="another-branch")
        self.assertFalse(dependency_utils.openlifu_version_matches())
        self.record_git(repository="https://github.com/another-owner/openlifu-python.git")
        self.assertFalse(dependency_utils.openlifu_version_matches())

    def test_github_repository_case_and_git_suffix(self):
        self.require_git()
        self.record_git(repository="https://GitHub.com/openwaterhealth/OpenLIFU-python/")
        self.assertTrue(dependency_utils.openlifu_version_matches())

    def test_legacy_bare_git_requirement(self):
        self.requirements.write_text(f"git+https://github.com/OpenwaterHealth/OpenLIFU-python.git@{self.branch}\n")
        self.record_git()
        self.assertEqual(self.branch, dependency_utils.get_required_openlifu_version())
        self.assertTrue(dependency_utils.openlifu_version_matches())

    def test_commit_pin_uses_resolved_commit(self):
        self.record_git(revision="main")
        for revision in (self.commit, self.commit[:9], self.commit.upper()):
            with self.subTest(revision=revision):
                self.require_git(revision)
                self.assertTrue(dependency_utils.openlifu_version_matches())
        self.require_git(self.commit)
        self.record_git(revision=self.commit, commit="a" * 40)
        self.assertFalse(dependency_utils.openlifu_version_matches())

    def test_missing_or_unusable_source_metadata(self):
        self.require_git()
        for contents in (
            "", "invalid json", "null", "[]", "{}",
            json.dumps({"url": self.repository, "vcs_info": []}),
            json.dumps({"url": self.repository, "vcs_info": {"vcs": "hg"}}),
            json.dumps({"url": self.repository, "vcs_info": {"vcs": "git", "requested_revision": self.branch}}),
        ):
            with self.subTest(contents=contents):
                self.source_path.write_text(contents)
                self.assertFalse(dependency_utils.openlifu_version_matches())

    def test_package_not_installed(self):
        self.require_git()
        with patch("importlib.metadata.distribution", side_effect=importlib.metadata.PackageNotFoundError("openlifu")):
            self.assertFalse(dependency_utils.openlifu_version_matches())

    def test_other_git_dependencies_are_not_openlifu_requirements(self):
        self.requirements.write_text("git+https://github.com/example/another-package.git@main\n")
        self.assertIsNone(dependency_utils.get_required_openlifu_version())


if __name__ == "__main__":
    unittest.main()
