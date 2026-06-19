import json
from pathlib import Path
import sys
from tempfile import TemporaryDirectory
import types
import unittest
from unittest.mock import patch

import instsci.browser_identity as browser_identity
from instsci.browser_identity import (
    browser_launch_args,
    build_profile_identity,
    ensure_profile_identity,
    mask_secret_url,
)
from instsci.config import Config
from instsci.publisher_batch import PublisherBatchDownloader
from instsci.publisher_profiles import get_publisher_profile


class BrowserIdentityTests(unittest.TestCase):
    def test_browser_launch_args_use_browser_proxy_not_connector_proxy(self):
        cfg = Config(
            proxy_url="socks5://127.0.0.1:1080",
            browser_proxy_url="socks5://reader:secret@example.proxy:1080",
        )

        args = browser_launch_args(cfg)

        self.assertIn("--disable-features=CrossOriginOpenerPolicy", args)
        self.assertIn("--proxy-server=socks5://reader:secret@example.proxy:1080", args)
        self.assertNotIn("--proxy-server=socks5://127.0.0.1:1080", args)

    def test_browser_extension_paths_parse_configured_directories(self):
        cfg = Config(browser_extension_dirs=r" D:\opencli\bridge ; C:\tools\reader-ext ")

        self.assertTrue(hasattr(browser_identity, "browser_extension_paths"))
        paths = browser_identity.browser_extension_paths(cfg)

        self.assertEqual(paths, [r"D:\opencli\bridge", r"C:\tools\reader-ext"])

    def test_mask_secret_url_hides_proxy_password(self):
        masked = mask_secret_url("socks5://reader:secret@example.proxy:1080")

        self.assertEqual(masked, "socks5://reader:****@example.proxy:1080")
        self.assertNotIn("secret", masked)

    def test_profile_identity_manifest_records_hash_without_proxy_secret(self):
        with TemporaryDirectory() as tmp:
            profile = Path(tmp) / "profile"
            cfg = Config(browser_proxy_url="socks5://reader:secret@example.proxy:1080")

            identity = build_profile_identity(
                cfg,
                publisher="elsevier",
                institution="Example University",
            )
            ensure_profile_identity(profile, identity)
            ensure_profile_identity(
                profile,
                build_profile_identity(cfg, publisher="acs", institution="Example University"),
            )

            manifest = json.loads((profile / ".instsci-browser-identity.json").read_text(encoding="utf-8"))

        self.assertEqual(manifest["institution"], "Example University")
        self.assertEqual(manifest["browser_proxy_url"], "socks5://reader:****@example.proxy:1080")
        self.assertEqual(len(manifest["browser_proxy_url_hash"]), 64)
        self.assertEqual(manifest["publishers"], ["acs", "elsevier"])
        self.assertNotIn("secret", json.dumps(manifest))

    def test_publisher_launch_context_passes_proxy_and_writes_identity_manifest(self):
        captured = {}

        def launch_persistent_context(**kwargs):
            captured.update(kwargs)
            return "context"

        fake_cloakbrowser = types.SimpleNamespace(launch_persistent_context=launch_persistent_context)

        with TemporaryDirectory() as tmp:
            profile = Path(tmp) / "profile"
            cfg = Config(
                chrome_profile_dir=str(profile),
                browser_proxy_url="socks5://reader:secret@example.proxy:1080",
                browser_extension_dirs=str(Path(tmp) / "opencli-extension"),
            )
            downloader = PublisherBatchDownloader(
                cfg,
                profile=get_publisher_profile("elsevier"),
                institution_query="Example University",
            )

            with patch.dict(sys.modules, {"cloakbrowser": fake_cloakbrowser}), \
                 patch("instsci.cloakbrowser_compat.prepare_cloakbrowser_runtime"):
                context = downloader._launch_context()

            manifest = json.loads((profile / ".instsci-browser-identity.json").read_text(encoding="utf-8"))

        self.assertEqual(context, "context")
        self.assertEqual(captured["user_data_dir"], str(profile))
        self.assertIn("--proxy-server=socks5://reader:secret@example.proxy:1080", captured["args"])
        self.assertEqual(captured["extension_paths"], [str(Path(tmp) / "opencli-extension")])
        self.assertEqual(manifest["institution"], "Example University")
        self.assertEqual(manifest["browser_extension_count"], 1)
        self.assertEqual(len(manifest["browser_extension_hash"]), 64)
        self.assertEqual(manifest["publishers"], ["elsevier"])
        self.assertNotIn("opencli-extension", json.dumps(manifest))
        self.assertNotIn("secret", json.dumps(manifest))


if __name__ == "__main__":
    unittest.main()
