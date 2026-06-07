import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from instsci.config import Config
from instsci.profile_health import (
    CHROME_EPOCH_OFFSET_SECONDS,
    candidate_profile_dirs,
    inspect_browser_profile,
)


class ProfileHealthTests(unittest.TestCase):
    def test_inspect_browser_profile_reports_cookie_hosts_without_values(self):
        with TemporaryDirectory() as tmp:
            profile = Path(tmp) / "profile"
            network = profile / "Default" / "Network"
            network.mkdir(parents=True)
            db = network / "Cookies"
            conn = sqlite3.connect(db)
            try:
                conn.execute("create table cookies (host_key text, name text, encrypted_value blob)")
                conn.execute(
                    "insert into cookies (host_key, name, encrypted_value) values (?, ?, ?)",
                    ("idp.tsinghua.edu.cn", "secret_session", b"not-returned"),
                )
                conn.execute(
                    "insert into cookies (host_key, name, encrypted_value) values (?, ?, ?)",
                    ("journals.aps.org", "aps_session", b"not-returned"),
                )
                conn.commit()
            finally:
                conn.close()

            report = inspect_browser_profile(profile, ("tsinghua.edu.cn", "aps.org"))

        self.assertTrue(report["exists"])
        self.assertEqual(report["domains"]["tsinghua.edu.cn"]["cookie_count"], 1)
        self.assertEqual(report["domains"]["aps.org"]["cookie_count"], 1)
        serialized = str(report)
        self.assertIn("idp.tsinghua.edu.cn", serialized)
        self.assertNotIn("secret_session", serialized)
        self.assertNotIn("not-returned", serialized)

    def test_inspect_browser_profile_reports_cookie_expiry_summary(self):
        expiry = int((datetime(2026, 6, 8, tzinfo=timezone.utc).timestamp() + CHROME_EPOCH_OFFSET_SECONDS) * 1_000_000)
        with TemporaryDirectory() as tmp:
            profile = Path(tmp) / "profile"
            network = profile / "Default" / "Network"
            network.mkdir(parents=True)
            db = network / "Cookies"
            conn = sqlite3.connect(db)
            try:
                conn.execute("create table cookies (host_key text, name text, expires_utc integer, encrypted_value blob)")
                conn.execute(
                    "insert into cookies (host_key, name, expires_utc, encrypted_value) values (?, ?, ?, ?)",
                    (".sciencedirect.com", "secret_session", expiry, b"not-returned"),
                )
                conn.execute(
                    "insert into cookies (host_key, name, expires_utc, encrypted_value) values (?, ?, ?, ?)",
                    ("www.sciencedirect.com", "session_only", 0, b"not-returned"),
                )
                conn.commit()
            finally:
                conn.close()

            report = inspect_browser_profile(profile, ("sciencedirect.com",))

        info = report["domains"]["sciencedirect.com"]
        self.assertEqual(info["cookie_count"], 2)
        self.assertEqual(info["session_cookie_count"], 1)
        self.assertEqual(info["persistent_cookie_count"], 1)
        self.assertEqual(info["latest_expires_at"], "2026-06-08T00:00:00+00:00")
        serialized = str(report)
        self.assertNotIn("secret_session", serialized)
        self.assertNotIn("not-returned", serialized)

    def test_candidate_profile_dirs_prioritizes_configured_profile_once(self):
        cfg = Config(
            output_dir="out",
            cache_dir="cache",
            cookie_path="cookies.json",
            chrome_profile_dir=str(Path("chosen-profile")),
            carsi_cookie_dir="carsi",
        )

        candidates = candidate_profile_dirs(cfg, workspace=Path("workspace"))

        self.assertEqual(candidates[0], Path("chosen-profile"))
        self.assertEqual(len(candidates), len({str(path) for path in candidates}))
        self.assertIn(Path("workspace") / ".chrome-sciencedirect", candidates)


if __name__ == "__main__":
    unittest.main()
