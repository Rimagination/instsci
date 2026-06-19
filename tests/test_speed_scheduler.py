import unittest

from typer.testing import CliRunner

from instsci.cli import (
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
        self.assertIn("--speed", papers_help)


if __name__ == "__main__":
    unittest.main()
