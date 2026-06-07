import os
import platform
import types
import unittest
from unittest.mock import patch


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


if __name__ == "__main__":
    unittest.main()
