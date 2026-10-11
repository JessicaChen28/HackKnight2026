"""Offline checks for frozen-build paths and packaging manifests."""

import os
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

import runtime_paths


class PackagingTests(unittest.TestCase):
    def test_development_default_exports_stay_in_source_tree(self):
        with patch.object(runtime_paths, "is_frozen", return_value=False):
            self.assertEqual(runtime_paths.default_cli_export_root(),
                             Path(runtime_paths.__file__).resolve().parent / "exports")

    def test_frozen_browser_path_is_configured_before_playwright_import(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            browser = root / "playwright-browsers"
            browser.mkdir()
            with patch.object(runtime_paths, "bundle_root", return_value=root), \
                 patch.dict(os.environ, {"PLAYWRIGHT_BROWSERS_PATH": "old-path"}, clear=False):
                self.assertEqual(runtime_paths.configure_playwright_browser(), browser)
                self.assertEqual(os.environ["PLAYWRIGHT_BROWSERS_PATH"], str(browser))

    def test_frozen_missing_browser_has_actionable_error(self):
        with TemporaryDirectory() as directory:
            with patch.object(runtime_paths, "is_frozen", return_value=True), \
                 patch.object(runtime_paths, "bundle_root", return_value=Path(directory)):
                self.assertIn("bundled Chromium", runtime_paths.playwright_browser_error())

    def test_installer_is_per_user_and_preserves_app_data(self):
        source = (Path(__file__).resolve().parents[1] / "packaging" / "SOLAR_RPG.iss").read_text(encoding="utf-8")
        self.assertIn("PrivilegesRequired=lowest", source)
        self.assertIn("DefaultDirName={localappdata}", source)
        self.assertNotIn("[UninstallDelete]\nType: filesandordirs; Name: \"{localappdata}", source)


if __name__ == "__main__":
    unittest.main()
