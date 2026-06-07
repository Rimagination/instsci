import json
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

    def test_load_falls_back_to_legacy_config_when_new_config_missing(self):
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            new_base = root / ".instsci"
            legacy_base = root / ".vpnsci"
            legacy_base.mkdir()
            (legacy_base / "config.json").write_text(
                json.dumps({"school": "Legacy University", "email": "old@example.edu"}),
                encoding="utf-8",
            )

            with patch.object(config_module, "DEFAULT_BASE_DIR", new_base), \
                 patch.object(config_module, "LEGACY_BASE_DIR", legacy_base):
                cfg = Config.load()
                cfg.save()

            self.assertEqual(cfg.school, "Legacy University")
            self.assertEqual(cfg.email, "old@example.edu")
            self.assertTrue((new_base / "config.json").exists())


if __name__ == "__main__":
    unittest.main()
