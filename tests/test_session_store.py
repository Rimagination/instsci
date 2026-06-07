from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

import requests

from instsci.session_store import CookieStore


class CookieStoreTests(unittest.TestCase):
    def test_save_normalizes_and_filters_expired_cookies(self):
        with TemporaryDirectory() as tmp:
            path = Path(tmp) / "cookies.json"
            store = CookieStore(path)

            saved = store.save([
                {"name": "session", "value": "abc", "domain": ".example.com", "path": "/", "expires": -1},
                {"name": "old", "value": "stale", "domain": ".example.com", "path": "/", "expires": 10},
                {"name": "missing_value", "domain": ".example.com", "path": "/", "expires": 0},
            ], now=100)

            self.assertEqual(len(saved), 1)
            self.assertEqual(saved[0]["name"], "session")
            self.assertEqual(saved[0]["expires"], 0)

            loaded = store.load(now=100)
            self.assertEqual(loaded, saved)

    def test_load_into_session_returns_false_for_malformed_cookie_file(self):
        with TemporaryDirectory() as tmp:
            path = Path(tmp) / "cookies.json"
            path.write_text("{bad json", encoding="utf-8")
            session = requests.Session()

            with self.assertLogs("instsci.session_store", level="WARNING"):
                loaded = CookieStore(path).load_into(session)

            self.assertFalse(loaded)
            self.assertEqual(len(session.cookies), 0)

    def test_load_into_session_applies_cookie_domain_and_path(self):
        with TemporaryDirectory() as tmp:
            path = Path(tmp) / "cookies.json"
            store = CookieStore(path)
            store.save([
                {"name": "sid", "value": "123", "domain": ".example.com", "path": "/library", "expires": 0},
            ])
            session = requests.Session()

            loaded = store.load_into(session)

            self.assertTrue(loaded)
            cookie = next(iter(session.cookies))
            self.assertEqual(cookie.name, "sid")
            self.assertEqual(cookie.value, "123")
            self.assertEqual(cookie.domain, ".example.com")
            self.assertEqual(cookie.path, "/library")


if __name__ == "__main__":
    unittest.main()
