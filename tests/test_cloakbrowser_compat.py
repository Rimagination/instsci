import os
import platform
from pathlib import Path
import types
import unittest
from unittest.mock import patch

from tests.project_guards import find_project_reference_offenders


class CloakBrowserCompatTests(unittest.TestCase):
    def test_empty_windows_machine_is_mapped_to_windows_x64(self):
        from instsci.cloakbrowser_compat import ensure_cloakbrowser_platform_compatible

        fake_config = types.SimpleNamespace(
            SUPPORTED_PLATFORMS={
                ("Windows", "AMD64"): "windows-x64",
                ("Windows", "x86_64"): "windows-x64",
            }
        )

        with (
            patch.object(platform, "system", return_value="Windows"),
            patch.object(platform, "machine", return_value=""),
            patch.dict(os.environ, {"ProgramFiles(x86)": r"C:\Program Files (x86)"}, clear=False),
        ):
            changed = ensure_cloakbrowser_platform_compatible(fake_config)

        self.assertTrue(changed)
        self.assertEqual(fake_config.SUPPORTED_PLATFORMS[("Windows", "")], "windows-x64")

    def test_configures_cloakbrowser_cache_inside_instsci_package_when_unset(self):
        from instsci.cloakbrowser_compat import configure_builtin_cloakbrowser

        with patch.dict(os.environ, {}, clear=True):
            cache_dir = configure_builtin_cloakbrowser(create_dir=False)

        self.assertEqual(os.environ["CLOAKBROWSER_CACHE_DIR"], str(cache_dir))
        self.assertIn("instsci", cache_dir.parts)
        self.assertEqual(cache_dir.parts[-2:], ("_browsers", "cloakbrowser"))
        self.assertTrue(cache_dir.is_absolute())

    def test_respects_explicit_cloakbrowser_cache_override(self):
        from instsci.cloakbrowser_compat import configure_builtin_cloakbrowser

        override = Path("D:/custom/cloakbrowser-cache")
        with patch.dict(os.environ, {"CLOAKBROWSER_CACHE_DIR": str(override)}, clear=True):
            cache_dir = configure_builtin_cloakbrowser(create_dir=False)
            self.assertEqual(cache_dir, override)
            self.assertEqual(os.environ["CLOAKBROWSER_CACHE_DIR"], str(override))

    def test_project_has_no_legacy_browser_references(self):
        root = Path(__file__).resolve().parents[1]
        legacy_names = ("camo" + "fox", "camou" + "fox")
        offenders = find_project_reference_offenders(root, legacy_names)

        self.assertEqual(offenders, [])

    def test_project_has_no_removed_challenge_service_references(self):
        root = Path(__file__).resolve().parents[1]
        removed_names = ("flare" + "solverr",)
        offenders = find_project_reference_offenders(root, removed_names)

        self.assertEqual(offenders, [])

    def test_pyproject_requires_current_cloakbrowser_release(self):
        root = Path(__file__).resolve().parents[1]
        pyproject = (root / "pyproject.toml").read_text(encoding="utf-8")

        self.assertIn('"cloakbrowser>=0.3.31"', pyproject)


if __name__ == "__main__":
    unittest.main()
