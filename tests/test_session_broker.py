import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from typer.testing import CliRunner

from instsci.config import Config
from instsci.cli import app
from instsci.session_broker import BrokerState, broker_key, pid_is_running, write_broker_state


class SessionBrokerTests(unittest.TestCase):
    def test_broker_key_normalizes_publisher_name(self):
        self.assertEqual(broker_key("Science Direct"), "science-direct")

    def test_pid_is_running_uses_windows_process_query_without_signal(self):
        with patch("instsci.session_broker.sys.platform", "win32"), \
             patch("instsci.session_broker.os.kill", side_effect=AssertionError("os.kill should not probe Windows PIDs")), \
             patch("instsci.session_broker._pid_is_running_windows", return_value=True, create=True) as windows_probe:
            self.assertTrue(pid_is_running(12345))

        windows_probe.assert_called_once_with(12345)

    def test_papers_defaults_to_broker_submission_when_available(self):
        runner = CliRunner()
        with TemporaryDirectory() as tmp:
            doi_file = Path(tmp) / "dois.txt"
            doi_file.write_text("10.1016/j.watres.2024.121507\n", encoding="utf-8")
            state = BrokerState(
                publisher="elsevier",
                profile_dir=str(Path(tmp) / "profile"),
                pid=12345,
                queue_dir=str(Path(tmp) / "queue"),
                started_at="2026-06-07T00:00:00",
                ttl_seconds=86400,
            )
            (Path(tmp) / "queue").mkdir()
            config = Config(
                school="Example University",
                output_dir=str(Path(tmp) / "out"),
                cache_dir=str(Path(tmp) / "cache"),
                cookie_path=str(Path(tmp) / "cookies.json"),
                chrome_profile_dir=str(Path(tmp) / "profile"),
                carsi_cookie_dir=str(Path(tmp) / "carsi"),
            )

            with patch("instsci.cli.Config.load", return_value=config), \
                 patch("instsci.session_broker.BROKER_ROOT", Path(tmp)), \
                 patch("instsci.session_broker.pid_is_running", return_value=True), \
                 patch("instsci.session_broker.submit_broker_job") as submit:
                write_broker_state(state)
                submit.return_value = {
                    "count": 1,
                    "success": 1,
                    "missing": 0,
                    "unverified": 0,
                    "verified_match": 1,
                    "pdf_dir": str(Path(tmp) / "complete" / "pdfs"),
                    "manifest": str(Path(tmp) / "complete" / "manifest.csv"),
                    "publisher": "Elsevier",
                }

                result = runner.invoke(app, ["papers", str(doi_file), "-p", "elsevier"])

        self.assertEqual(result.exit_code, 0, result.output)
        submit.assert_called_once()
        payload = submit.call_args.kwargs
        self.assertEqual(payload["publisher"], "elsevier")
        self.assertEqual(payload["records"][0]["doi"], "10.1016/j.watres.2024.121507")
        self.assertIn("broker", result.output.lower())

    def test_papers_prompts_for_subscription_institution_without_default_tsinghua(self):
        runner = CliRunner()
        with TemporaryDirectory() as tmp:
            doi_file = Path(tmp) / "dois.txt"
            doi_file.write_text("10.1016/j.watres.2024.121507\n", encoding="utf-8")
            config = Config(
                output_dir=str(Path(tmp) / "out"),
                cache_dir=str(Path(tmp) / "cache"),
                cookie_path=str(Path(tmp) / "cookies.json"),
                chrome_profile_dir=str(Path(tmp) / "profile"),
                carsi_cookie_dir=str(Path(tmp) / "carsi"),
            )
            config.save = lambda *args, **kwargs: None  # type: ignore[method-assign]
            state = BrokerState(
                publisher="elsevier",
                profile_dir=str(Path(tmp) / "profile"),
                pid=12345,
                queue_dir=str(Path(tmp) / "queue"),
                started_at="2026-06-07T00:00:00",
                ttl_seconds=86400,
            )
            (Path(tmp) / "queue").mkdir()

            with patch("instsci.cli.Config.load", return_value=config), \
                 patch("instsci.session_broker.BROKER_ROOT", Path(tmp)), \
                 patch("instsci.session_broker.pid_is_running", return_value=True), \
                 patch("instsci.session_broker.submit_broker_job") as submit:
                write_broker_state(state)
                submit.return_value = {
                    "count": 1,
                    "success": 1,
                    "missing": 0,
                    "unverified": 0,
                    "verified_match": 1,
                    "pdf_dir": str(Path(tmp) / "complete" / "pdfs"),
                    "manifest": str(Path(tmp) / "complete" / "manifest.csv"),
                    "publisher": "Elsevier",
                }

                result = runner.invoke(
                    app,
                    ["papers", str(doi_file), "-p", "elsevier"],
                    input="Example University\n",
                )

        self.assertEqual(result.exit_code, 0, result.output)
        self.assertIn("Subscription institution", result.output)
        self.assertNotIn("Tsinghua", result.output)
        self.assertEqual(submit.call_args.kwargs["institution"], "Example University")

    def test_papers_institution_help_does_not_default_to_tsinghua(self):
        result = CliRunner().invoke(app, ["papers", "--help"])

        self.assertEqual(result.exit_code, 0, result.output)
        self.assertIn("--institution", result.output)
        self.assertNotIn("Tsinghua", result.output)

    def test_session_broker_state_command_reports_running_broker(self):
        runner = CliRunner()
        with TemporaryDirectory() as tmp:
            state = BrokerState(
                publisher="elsevier",
                profile_dir=str(Path(tmp) / "profile"),
                pid=12345,
                queue_dir=str(Path(tmp) / "queue"),
                started_at="2026-06-07T00:00:00",
                ttl_seconds=86400,
            )
            with patch("instsci.session_broker.BROKER_ROOT", Path(tmp)), \
                 patch("instsci.session_broker.pid_is_running", return_value=True):
                write_broker_state(state)

                result = runner.invoke(app, ["session-broker-status", "-p", "elsevier"])

        self.assertEqual(result.exit_code, 0, result.output)
        self.assertIn("running", result.output.lower())


if __name__ == "__main__":
    unittest.main()
