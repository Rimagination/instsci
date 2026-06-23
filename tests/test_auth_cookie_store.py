import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from instsci.auth import EZProxyAuth, WebVPNAuth
from instsci.carsi import CARSIClient, _institution_result_click_script
from instsci.config import Config


class FakeContext:
    def __init__(self, cookies):
        self._cookies = cookies

    def cookies(self):
        return self._cookies


def temp_config(base: Path) -> Config:
    return Config(
        school="",
        output_dir=str(base / "papers"),
        cache_dir=str(base / "cache"),
        cookie_path=str(base / "cookies.json"),
        chrome_profile_dir=str(base / "chrome-profile"),
        carsi_cookie_dir=str(base / "carsi-cookies"),
    )


class AuthCookieStoreTests(unittest.TestCase):
    def test_webvpn_login_browser_bypasses_windows_proxy(self):
        with TemporaryDirectory() as tmp:
            cfg = temp_config(Path(tmp))
            auth = WebVPNAuth(cfg)

            self.assertIn("--no-proxy-server", auth._browser_launch_args())

    def test_carsi_save_preserves_browser_session_cookie(self):
        with TemporaryDirectory() as tmp:
            cfg = temp_config(Path(tmp))
            client = CARSIClient(cfg)
            client._save_cookies("sciencedirect", [
                {"name": "sid", "value": "1", "domain": ".sciencedirect.com", "path": "/", "expires": -1},
            ])

            saved = json.loads((Path(cfg.carsi_cookie_dir) / "sciencedirect.json").read_text(encoding="utf-8"))

            self.assertEqual(len(saved), 1)
            self.assertEqual(saved[0]["expires"], 0)

    def test_carsi_institution_result_script_does_not_click_first_unmatched_item(self):
        script = _institution_result_click_script("button, a, [role='button']", "Example University")

        self.assertIn("\"button, a, [role='button']\"", script)
        self.assertIn("Example University", script)
        self.assertNotIn("items[0].click", script)

    def test_carsi_institution_result_script_uses_explicit_aliases(self):
        script = _institution_result_click_script("button", "示例大学", ["Example University"])

        self.assertIn("Example University(OpenAthens)", script)
        self.assertIn("示例大学", script)
        self.assertNotIn("Tsinghua University", script)

    def test_ezproxy_save_preserves_browser_session_cookie(self):
        with TemporaryDirectory() as tmp:
            cfg = temp_config(Path(tmp))
            auth = EZProxyAuth(cfg, proxy_base="https://proxy.example/login?url=")
            auth._context = FakeContext([
                {"name": "ez", "value": "1", "domain": ".proxy.example", "path": "/", "expires": -1},
            ])

            auth._save_browser_cookies()
            saved = json.loads(Path(cfg.cookie_path).read_text(encoding="utf-8"))

            self.assertEqual(len(saved), 1)
            self.assertEqual(saved[0]["expires"], 0)


if __name__ == "__main__":
    unittest.main()
