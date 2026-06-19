from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

import instsci.config as config_module
from instsci.cli import app
from instsci.config import Config
from typer.testing import CliRunner


class ConfigPathTests(unittest.TestCase):
    def test_default_paths_use_inst_sci_directory(self):
        with TemporaryDirectory() as tmp:
            base = Path(tmp) / ".instsci"
            with patch.object(config_module, "DEFAULT_BASE_DIR", base):
                cfg = Config()

            self.assertEqual(Path(cfg.output_dir), base / "papers")
            self.assertEqual(Path(cfg.cache_dir), base / "cache")
            self.assertEqual(Path(cfg.cookie_path), base / "cookies.json")

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

    def test_config_cmd_saves_browser_proxy_without_printing_secret(self):
        runner = CliRunner()
        with TemporaryDirectory() as tmp:
            base = Path(tmp) / ".instsci"
            proxy = "socks5://reader:secret@example.proxy:1080"
            with patch.object(config_module, "DEFAULT_BASE_DIR", base):
                result = runner.invoke(app, ["config-cmd", "--browser-proxy-url", proxy])
                cfg = Config.load()

        self.assertEqual(result.exit_code, 0, result.output)
        self.assertEqual(cfg.browser_proxy_url, proxy)
        self.assertIn("socks5://reader:****@example.proxy:1080", result.output)
        self.assertNotIn("secret", result.output)

    def test_config_cmd_saves_opencli_extension_dir(self):
        runner = CliRunner()
        with TemporaryDirectory() as tmp:
            base = Path(tmp) / ".instsci"
            extension_dir = str(Path(tmp) / "opencli-extension")
            with patch.object(config_module, "DEFAULT_BASE_DIR", base):
                result = runner.invoke(app, ["config-cmd", "--opencli-extension-dir", extension_dir])
                cfg = Config.load()

        self.assertEqual(result.exit_code, 0, result.output)
        self.assertEqual(cfg.browser_extension_dirs, extension_dir)
        self.assertIn(extension_dir, result.output)


if __name__ == "__main__":
    unittest.main()
