import unittest

from typer.testing import CliRunner

from instsci.cli import (
    OVERNIGHT_BROKER_TTL_SECONDS,
    OVERNIGHT_LOGIN_TIMEOUT_SECONDS,
    _apply_overnight_mode,
    _effective_browser_concurrency,
    _group_records_by_publisher,
    app,
)
from instsci.publisher_batch import PaperRecord


class SpeedSchedulerTests(unittest.TestCase):
    def test_balanced_and_careful_keep_one_browser_context(self):
        self.assertEqual(_effective_browser_concurrency("careful", 4), 1)
        self.assertEqual(_effective_browser_concurrency("balanced", 4), 1)

    def test_fast_caps_browser_workers(self):
        self.assertEqual(_effective_browser_concurrency("fast", 1), 1)
        self.assertEqual(_effective_browser_concurrency("fast", 4), 2)

    def test_overnight_mode_uses_long_single_context_broker_preset(self):
        login_timeout, broker, broker_ttl, speed_mode, concurrency = _apply_overnight_mode(
            login_timeout=900,
            broker=False,
            broker_ttl=600,
            speed_mode="fast",
            concurrency=4,
        )

        self.assertEqual(login_timeout, OVERNIGHT_LOGIN_TIMEOUT_SECONDS)
        self.assertTrue(broker)
        self.assertEqual(broker_ttl, OVERNIGHT_BROKER_TTL_SECONDS)
        self.assertEqual(speed_mode, "careful")
        self.assertEqual(concurrency, 1)

    def test_long_lived_defaults_cover_multiday_persistence(self):
        three_days = 3 * 24 * 60 * 60

        self.assertGreaterEqual(OVERNIGHT_LOGIN_TIMEOUT_SECONDS, three_days)
        self.assertGreaterEqual(OVERNIGHT_BROKER_TTL_SECONDS, three_days)

    def test_auto_publisher_groups_mixed_dois_without_school_default(self):
        groups = _group_records_by_publisher(
            [
                PaperRecord(doi="10.1021/acs.est.test"),
                PaperRecord(doi="10.1016/j.watres.2024.121507"),
                PaperRecord(doi="10.1002/adfm.202525261"),
            ],
            "auto",
        )

        self.assertEqual([key for key, _profile, _records in groups], ["acs", "elsevier", "wiley"])
        self.assertEqual([len(records) for _key, _profile, records in groups], [1, 1, 1])

    def test_browser_commands_expose_speed_options(self):
        runner = CliRunner()

        publisher_help = runner.invoke(app, ["publisher-batch", "--help"]).output
        papers_help = runner.invoke(app, ["papers", "--help"]).output

        self.assertIn("--speed", publisher_help)
        self.assertIn("--concurrency", publisher_help)
        self.assertIn("--overnight", publisher_help)
        self.assertIn("--speed", papers_help)
        self.assertIn("--overnight", papers_help)


if __name__ == "__main__":
    unittest.main()
