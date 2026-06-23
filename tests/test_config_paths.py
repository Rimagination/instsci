from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

import instsci.config as config_module
from instsci.config import Config


class ConfigPathTests(unittest.TestCase):
    def test_default_paths_use_inst_sci_directory(self):
        with TemporaryDirectory() as tmp:
            base = Path(tmp) / ".instsci"
            with patch.object(config_module, "DEFAULT_BASE_DIR", base):
                cfg = Config()

            self.assertEqual(Path(cfg.output_dir), base / "papers")
            self.assertEqual(Path(cfg.cache_dir), base / "cache")
            self.assertEqual(Path(cfg.cookie_path), base / "cookies.json")
            self.assertEqual(Path(cfg.chrome_profile_dir), base / "chrome-profile")
            self.assertEqual(Path(cfg.carsi_cookie_dir), base / "carsi_cookies")

    def test_load_uses_inst_sci_config_path(self):
        with TemporaryDirectory() as tmp:
            base = Path(tmp) / ".instsci"
            base.mkdir()
            (base / "config.json").write_text(
                '{"school": "InstSci University", "email": "reader@example.edu"}',
                encoding="utf-8",
            )

            with patch.object(config_module, "DEFAULT_BASE_DIR", base):
                cfg = Config.load()
                cfg.save()

            self.assertEqual(cfg.school, "InstSci University")
            self.assertEqual(cfg.email, "reader@example.edu")
            self.assertTrue((base / "config.json").exists())


if __name__ == "__main__":
    unittest.main()
