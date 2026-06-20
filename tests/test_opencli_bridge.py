import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from instsci.config import Config
from instsci.opencli_bridge import (
    build_opencli_bridge_diagnostics,
    check_opencli_daemon,
    inspect_opencli_extension_dir,
)


def write_opencli_extension(base: Path) -> Path:
    extension_dir = base / "opencli-1.0.20"
    (extension_dir / "dist").mkdir(parents=True)
    (extension_dir / "manifest.json").write_text(
        json.dumps(
            {
                "manifest_version": 3,
                "name": "OpenCLI",
                "version": "1.0.20",
                "description": "Browser automation bridge for OpenCLI.",
                "permissions": [
                    "debugger",
                    "tabs",
                    "cookies",
                    "activeTab",
                    "alarms",
                    "storage",
                    "tabGroups",
                    "downloads",
                ],
                "host_permissions": ["<all_urls>"],
                "background": {"service_worker": "dist/background.js"},
            }
        ),
        encoding="utf-8",
    )
    (extension_dir / "dist" / "background.js").write_text(
        """
        const DAEMON_HOST = "localhost";
        const DAEMON_PORT = 19825;
        if (message.type === "getStatus") {}
        switch (action) {
          case "exec": break;
          case "navigate": break;
          case "cookies": break;
          case "screenshot": break;
          case "wait-download": break;
          case "unknown-action": break;
        }
        """,
        encoding="utf-8",
    )
    return extension_dir


class OpenCliBridgeTests(unittest.TestCase):
    def test_inspect_opencli_extension_reads_manifest_and_actions(self):
        with TemporaryDirectory() as tmp:
            extension_dir = write_opencli_extension(Path(tmp))

            info = inspect_opencli_extension_dir(extension_dir).to_dict()

        self.assertTrue(info["exists"])
        self.assertTrue(info["manifest_ok"])
        self.assertEqual(info["name"], "OpenCLI")
        self.assertEqual(info["version"], "1.0.20")
        self.assertTrue(info["required_permissions_present"])
        self.assertEqual(info["daemon_host"], "localhost")
        self.assertEqual(info["daemon_port"], 19825)
        self.assertEqual(info["websocket_url"], "ws://localhost:19825/ext")
        self.assertTrue(info["status_message_supported"])
        self.assertIn("exec", info["command_actions"])
        self.assertIn("wait-download", info["command_actions"])
        self.assertNotIn("unknown-action", info["command_actions"])

    def test_missing_extension_reports_error(self):
        with TemporaryDirectory() as tmp:
            info = inspect_opencli_extension_dir(Path(tmp) / "missing").to_dict()

        self.assertFalse(info["exists"])
        self.assertIn("does not exist", info["error"])

    def test_check_opencli_daemon_reads_ping_and_status(self):
        class FakeResponse:
            def __init__(self, payload):
                self.payload = payload

            def __enter__(self):
                return self

            def __exit__(self, *_args):
                return False

            def read(self):
                return json.dumps(self.payload).encode("utf-8")

        def fake_urlopen(request, timeout):
            if request.full_url.endswith("/ping"):
                return FakeResponse({"ok": True})
            if request.full_url.endswith("/status"):
                return FakeResponse(
                    {
                        "ok": True,
                        "daemonVersion": "1.8.4",
                        "extensionConnected": True,
                        "extensionVersion": "1.0.20",
                        "contextId": "ctx-test",
                        "profiles": [{"name": "default"}],
                    }
                )
            raise AssertionError(request.full_url)

        with patch("instsci.opencli_bridge.urlopen", side_effect=fake_urlopen):
            result = check_opencli_daemon(timeout_sec=0.1)

        self.assertTrue(result["ping_ok"])
        self.assertTrue(result["status_ok"])
        self.assertTrue(result["extension_connected"])
        self.assertEqual(result["daemon_version"], "1.8.4")
        self.assertEqual(result["extension_version"], "1.0.20")
        self.assertEqual(result["context_id"], "ctx-test")
        self.assertEqual(len(result["profiles"]), 1)

    def test_build_diagnostics_reports_connected_without_runtime_probe(self):
        with TemporaryDirectory() as tmp:
            extension_dir = write_opencli_extension(Path(tmp))
            cfg = Config(browser_extension_dirs=str(extension_dir))
            daemon = {
                "ping_ok": True,
                "status_ok": True,
                "extension_connected": True,
                "daemon_version": "1.8.4",
                "extension_version": "1.0.20",
                "context_id": "ctx-test",
                "profiles": [],
            }

            with patch("instsci.opencli_bridge.check_opencli_daemon", return_value=daemon):
                diagnostics = build_opencli_bridge_diagnostics(cfg)

        self.assertTrue(diagnostics["opencli_configured"])
        self.assertEqual(diagnostics["configured_extension_count"], 1)
        self.assertEqual(diagnostics["verdict"], "connected")


if __name__ == "__main__":
    unittest.main()
