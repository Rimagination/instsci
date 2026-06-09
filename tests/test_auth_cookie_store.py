import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from instsci.auth import EZProxyAuth, WebVPNAuth
from instsci.carsi import CARSIClient
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

    def test_carsi_browser_login_polls_until_publisher_page_and_saves_cookies(self):
        class FakePage:
            url = "https://www.sciencedirect.com/science/article/pii/S0000000000000000"

            def goto(self, url, wait_until=None):
                return None

        class FakeBrowserContext:
            def __init__(self):
                self.page = FakePage()
                self.pages = [self.page]

            def new_page(self):
                return self.page

            def cookies(self):
                return [
                    {"name": "sid", "value": "1", "domain": ".sciencedirect.com", "path": "/", "expires": -1},
                ]

        class FakeBrowser:
            def __init__(self):
                self.context = FakeBrowserContext()
                self.closed = False

            def new_context(self):
                return self.context

            def close(self):
                self.closed = True

        with TemporaryDirectory() as tmp:
            cfg = temp_config(Path(tmp))
            client = CARSIClient(cfg)
            browser = FakeBrowser()

            with (
                patch("instsci.carsi._HAS_CLOAKBROWSER", True),
                patch("instsci.carsi.launch", return_value=browser),
                patch("instsci.carsi.time.sleep", return_value=None),
            ):
                self.assertTrue(client._browser_login("sciencedirect"))

            saved = json.loads((Path(cfg.carsi_cookie_dir) / "sciencedirect.json").read_text(encoding="utf-8"))

            self.assertTrue(browser.closed)
            self.assertEqual(saved[0]["name"], "sid")

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
