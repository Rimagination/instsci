import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from typer.testing import CliRunner

from instsci.config import Config
from instsci.cli import OVERNIGHT_LOGIN_TIMEOUT_SECONDS, app
from instsci.session_broker import (
    BrokerState,
    broker_identity_matches,
    broker_key,
    broker_summary_status,
    list_queued_jobs,
    list_paused_jobs,
    pause_broker_job,
    pid_is_running,
    resume_all_broker_jobs,
    submit_broker_job,
    write_broker_state,
)


class SessionBrokerTests(unittest.TestCase):
    def test_broker_key_normalizes_publisher_name(self):
        self.assertEqual(broker_key("Science Direct"), "science-direct")

    def test_pid_is_running_uses_windows_process_query_without_signal(self):
        with patch("instsci.session_broker.sys.platform", "win32"), \
             patch("instsci.session_broker.os.kill", side_effect=AssertionError("os.kill should not probe Windows PIDs")), \
             patch("instsci.session_broker._pid_is_running_windows", return_value=True, create=True) as windows_probe:
            self.assertTrue(pid_is_running(12345))

        windows_probe.assert_called_once_with(12345)

    def test_broker_identity_matches_recorded_institution(self):
        with TemporaryDirectory() as tmp:
            state = BrokerState(
                publisher="elsevier",
                profile_dir=str(Path(tmp) / "profile"),
                pid=12345,
                queue_dir=str(Path(tmp) / "queue"),
                started_at="2026-06-07T00:00:00",
                ttl_seconds=86400,
                institution="Example University",
                browser_proxy_url_hash="proxy-a",
                browser_extension_hash="ext-a",
            )
            with patch("instsci.session_broker.BROKER_ROOT", Path(tmp)), \
                 patch("instsci.session_broker.pid_is_running", return_value=True):
                write_broker_state(state)

                matches, reason = broker_identity_matches(
                    "elsevier",
                    profile_dir=str(Path(tmp) / "profile"),
                    institution="Example University",
                    browser_proxy_url_hash="proxy-a",
                    browser_extension_hash="ext-a",
                )
                mismatch, mismatch_reason = broker_identity_matches(
                    "elsevier",
                    profile_dir=str(Path(tmp) / "profile"),
                    institution="Other University",
                    browser_proxy_url_hash="proxy-a",
                    browser_extension_hash="ext-a",
                )

        self.assertTrue(matches, reason)
        self.assertFalse(mismatch)
        self.assertIn("institution", mismatch_reason)

    def test_broker_summary_status_marks_manual_attention(self):
        self.assertEqual(broker_summary_status({"count": 3, "success": 3, "missing": 0, "unverified": 0}), "complete")
        self.assertEqual(broker_summary_status({"count": 3, "success": 2, "missing": 1, "unverified": 0}), "attention_required")
        self.assertEqual(broker_summary_status({"count": 3, "success": 2, "missing": 0, "unverified": 1}), "attention_required")
        self.assertEqual(broker_summary_status({"error": "SSO timed out"}), "error")

    def test_broker_summary_status_marks_reauth_required(self):
        self.assertEqual(
            broker_summary_status({
                "count": 3,
                "success": 2,
                "missing": 1,
                "unverified": 0,
                "attention_reasons": {"sso_required": 1},
            }),
            "reauth_required",
        )
        self.assertEqual(
            broker_summary_status({
                "count": 3,
                "success": 2,
                "missing": 1,
                "unverified": 0,
                "attention_reasons": {"challenge_or_viewer_timeout": 1},
            }),
            "reauth_required",
        )
        self.assertEqual(
            broker_summary_status({
                "count": 3,
                "success": 2,
                "missing": 1,
                "unverified": 0,
                "attention_reasons": {"institution_pdf_entitlement_missing": 1},
            }),
            "attention_required",
        )

    def test_pause_broker_job_keeps_remaining_records_for_resume(self):
        with TemporaryDirectory() as tmp:
            job = {
                "id": "job-original",
                "publisher": "science",
                "records": [
                    {"doi": "10.1126/science.done", "title": "Done"},
                    {"doi": "10.1126/science.login", "title": "Login"},
                    {"doi": "10.1126/science.unverified", "title": "Unverified"},
                ],
                "output_dir": str(Path(tmp) / "run"),
                "login_timeout": 900,
            }
            summary = {
                "count": 3,
                "success": 1,
                "missing": 1,
                "unverified": 1,
                "attention_reasons": {"sso_required": 1},
                "manifest_items": [
                    {"doi": "10.1126/science.done", "status": "success", "reason": ""},
                    {"doi": "10.1126/science.login", "status": "missing", "reason": "sso_required"},
                    {"doi": "10.1126/science.unverified", "status": "unverified", "reason": ""},
                ],
            }
            with patch("instsci.session_broker.BROKER_ROOT", Path(tmp)):
                paused = pause_broker_job("science", job, summary)
                payload = json.loads(Path(paused["path"]).read_text(encoding="utf-8"))

        self.assertEqual(paused["job_id"], "job-original")
        self.assertEqual(paused["record_count"], 2)
        self.assertEqual(
            [record["doi"] for record in payload["records"]],
            ["10.1126/science.login", "10.1126/science.unverified"],
        )
        self.assertEqual(payload["resume_source_job_id"], "job-original")
        self.assertEqual(payload["pause_reason"], "reauth_required")
        self.assertNotIn("cookie", json.dumps(payload).lower())

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
                institution="Example University",
                browser_extension_count=1,
                browser_extension_hash="abcdef1234567890",
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
        self.assertEqual(payload["login_timeout"], OVERNIGHT_LOGIN_TIMEOUT_SECONDS)
        self.assertIn("broker", result.output.lower())

    def test_publisher_batch_defaults_to_broker_submission_when_available(self):
        runner = CliRunner()
        with TemporaryDirectory() as tmp:
            doi_file = Path(tmp) / "dois.txt"
            doi_file.write_text("10.1126/science.test\n", encoding="utf-8")
            attempt_cache = Path(tmp) / "attempts.jsonl"
            state = BrokerState(
                publisher="science",
                profile_dir=str(Path(tmp) / "profile"),
                pid=12345,
                queue_dir=str(Path(tmp) / "queue"),
                started_at="2026-06-07T00:00:00",
                ttl_seconds=86400,
                institution="Example University",
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
                    "attempt_cache": str(attempt_cache),
                    "publisher": "Science",
                }

                result = runner.invoke(
                    app,
                    [
                        "publisher-batch",
                        str(doi_file),
                        "-p",
                        "science",
                        "--target-verified",
                        "1",
                        "--attempt-cache",
                        str(attempt_cache),
                        "--skip-attempted",
                    ],
                )

        self.assertEqual(result.exit_code, 0, result.output)
        submit.assert_called_once()
        payload = submit.call_args.kwargs
        self.assertEqual(payload["publisher"], "science")
        self.assertEqual(payload["target_verified"], 1)
        self.assertEqual(payload["attempt_cache"], str(attempt_cache))
        self.assertTrue(payload["skip_attempted"])
        self.assertEqual(payload["login_timeout"], OVERNIGHT_LOGIN_TIMEOUT_SECONDS)

    def test_publisher_batch_strips_utf8_bom_from_first_doi(self):
        runner = CliRunner()
        with TemporaryDirectory() as tmp:
            doi_file = Path(tmp) / "dois.txt"
            doi_file.write_bytes(b"\xef\xbb\xbf10.1002/adfm.202525261\r\n")
            state = BrokerState(
                publisher="wiley",
                profile_dir=str(Path(tmp) / "profile"),
                pid=12345,
                queue_dir=str(Path(tmp) / "queue"),
                started_at="2026-06-20T00:00:00",
                ttl_seconds=86400,
                institution="Example University",
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
                    "publisher": "Wiley",
                }

                result = runner.invoke(app, ["publisher-batch", str(doi_file), "-p", "wiley"])

        self.assertEqual(result.exit_code, 0, result.output)
        self.assertEqual(submit.call_args.kwargs["records"][0]["doi"], "10.1002/adfm.202525261")

    def test_publisher_batch_overnight_passes_long_login_wait_to_broker(self):
        runner = CliRunner()
        with TemporaryDirectory() as tmp:
            doi_file = Path(tmp) / "dois.txt"
            doi_file.write_text("10.1126/science.test\n", encoding="utf-8")
            state = BrokerState(
                publisher="science",
                profile_dir=str(Path(tmp) / "profile"),
                pid=12345,
                queue_dir=str(Path(tmp) / "queue"),
                started_at="2026-06-07T00:00:00",
                ttl_seconds=86400,
                institution="Example University",
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
                    "publisher": "Science",
                }

                result = runner.invoke(
                    app,
                    [
                        "publisher-batch",
                        str(doi_file),
                        "-p",
                        "science",
                        "--overnight",
                        "--speed",
                        "fast",
                        "--concurrency",
                        "4",
                        "--login-timeout",
                        "900",
                    ],
                )

        self.assertEqual(result.exit_code, 0, result.output)
        payload = submit.call_args.kwargs
        self.assertEqual(payload["login_timeout"], OVERNIGHT_LOGIN_TIMEOUT_SECONDS)
        self.assertIn("Overnight mode", result.output)

    def test_publisher_batch_no_broker_can_keep_one_shot_browser_open(self):
        runner = CliRunner()
        captured: dict[str, object] = {}

        class FakeDownloader:
            def __init__(self, _cfg, **kwargs):
                captured["kwargs"] = kwargs

            def run_records(self, records, run_dir, **kwargs):
                captured["records"] = records
                captured["run_dir"] = run_dir
                captured["run_kwargs"] = kwargs
                return {
                    "count": 1,
                    "success": 1,
                    "missing": 0,
                    "unverified": 0,
                    "verified_match": 1,
                    "pdf_dir": "pdfs",
                    "manifest": "manifest.csv",
                    "publisher": "Elsevier",
                }

        with TemporaryDirectory() as tmp:
            doi_file = Path(tmp) / "dois.txt"
            doi_file.write_text("10.1016/j.watres.2024.121507\n", encoding="utf-8")
            config = Config(
                school="Example University",
                output_dir=str(Path(tmp) / "out"),
                cache_dir=str(Path(tmp) / "cache"),
                cookie_path=str(Path(tmp) / "cookies.json"),
                chrome_profile_dir=str(Path(tmp) / "profile"),
                carsi_cookie_dir=str(Path(tmp) / "carsi"),
            )

            with patch("instsci.cli.Config.load", return_value=config), \
                 patch("instsci.publisher_batch.PublisherBatchDownloader", FakeDownloader):
                result = runner.invoke(
                    app,
                    [
                        "publisher-batch",
                        str(doi_file),
                        "-p",
                        "elsevier",
                        "--no-broker",
                        "--keep-browser-open",
                    ],
                )

        self.assertEqual(result.exit_code, 0, result.output)
        init_kwargs = captured["kwargs"]
        assert isinstance(init_kwargs, dict)
        self.assertTrue(init_kwargs["keep_browser_open"])
        self.assertEqual(captured["run_kwargs"]["concurrency"], 1)  # type: ignore[index]

    def test_papers_does_not_submit_to_mismatched_broker_identity(self):
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
                institution="Other University",
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
            fallback_summary = {
                "count": 1,
                "success": 1,
                "missing": 0,
                "unverified": 0,
                "verified_match": 1,
                "pdf_dir": str(Path(tmp) / "complete" / "pdfs"),
                "manifest": str(Path(tmp) / "complete" / "manifest.csv"),
                "publisher": "Elsevier",
            }

            with patch("instsci.cli.Config.load", return_value=config), \
                 patch("instsci.session_broker.BROKER_ROOT", Path(tmp)), \
                 patch("instsci.session_broker.pid_is_running", return_value=True), \
                 patch("instsci.session_broker.submit_broker_job") as submit, \
                 patch("instsci.publisher_batch.PublisherBatchDownloader.run_records", return_value=fallback_summary) as run_records:
                write_broker_state(state)

                result = runner.invoke(app, ["papers", str(doi_file), "-p", "elsevier"])

        self.assertEqual(result.exit_code, 0, result.output)
        submit.assert_not_called()
        run_records.assert_called_once()
        self.assertIn("not reused", result.output)

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
                institution="Example University",
                browser_extension_count=1,
                browser_extension_hash="abcdef1234567890",
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
                institution="Example University",
                browser_extension_count=1,
                browser_extension_hash="abcdef1234567890",
            )
            with patch("instsci.session_broker.BROKER_ROOT", Path(tmp)), \
                 patch("instsci.session_broker.pid_is_running", return_value=True):
                write_broker_state(state)

                result = runner.invoke(app, ["session-broker-status", "-p", "elsevier"])

        self.assertEqual(result.exit_code, 0, result.output)
        self.assertIn("running", result.output.lower())
        self.assertIn("Example University", result.output)
        self.assertIn("abcdef123456", result.output)

    def test_session_broker_state_command_reports_persistence_job_status(self):
        runner = CliRunner()
        with TemporaryDirectory() as tmp:
            summary_path = Path(tmp) / "run" / "summary.json"
            state = BrokerState(
                publisher="elsevier",
                profile_dir=str(Path(tmp) / "profile"),
                pid=12345,
                queue_dir=str(Path(tmp) / "queue"),
                started_at="2026-06-07T00:00:00",
                ttl_seconds=259200,
                institution="Example University",
                status="attention_required",
                active_job_id="job-active",
                active_output_dir=str(Path(tmp) / "active-run"),
                active_record_count=12,
                last_job_id="job-last",
                last_job_status="attention_required",
                last_summary_path=str(summary_path),
                last_error="Manual institution re-authentication required.",
                paused_job_id="job-paused",
                paused_job_path=str(Path(tmp) / "paused" / "job-paused.json"),
                paused_record_count=5,
            )
            with patch("instsci.session_broker.BROKER_ROOT", Path(tmp)), \
                 patch("instsci.session_broker.pid_is_running", return_value=True):
                write_broker_state(state)

                result = runner.invoke(app, ["session-broker-status", "-p", "elsevier"])

        self.assertEqual(result.exit_code, 0, result.output)
        self.assertIn("attention_required", result.output)
        self.assertIn("job-active", result.output)
        self.assertIn("12", result.output)
        self.assertIn("job-last", result.output)
        self.assertIn("summary.json", result.output)
        self.assertIn("Manual institution re-authentication required.", result.output)
        self.assertIn("job-paused", result.output)
        self.assertIn("5 records", result.output)

    def test_session_broker_state_command_reports_last_health_check(self):
        runner = CliRunner()
        with TemporaryDirectory() as tmp:
            state = BrokerState(
                publisher="science",
                profile_dir=str(Path(tmp) / "profile"),
                pid=12345,
                queue_dir=str(Path(tmp) / "queue"),
                started_at="2026-06-19T00:00:00",
                ttl_seconds=259200,
                status="idle",
                last_health_status="reauth_required",
                last_health_at="2026-06-19T22:00:00",
                last_health_url="https://www.science.org/doi/10.1126/science.test",
                last_health_error="logged_out",
                keepalive_interval_seconds=1800,
            )
            with patch("instsci.session_broker.BROKER_ROOT", Path(tmp)), \
                 patch("instsci.session_broker.pid_is_running", return_value=True):
                write_broker_state(state)

                result = runner.invoke(app, ["session-broker-status", "-p", "science"])

        self.assertEqual(result.exit_code, 0, result.output)
        self.assertIn("reauth_required", result.output)
        self.assertIn("2026-06-19T22:00:00", result.output)
        self.assertIn("logged_out", result.output)
        self.assertIn("1800", result.output)

    def test_session_broker_state_command_can_emit_json_inventory(self):
        runner = CliRunner()
        with TemporaryDirectory() as tmp:
            broker_dir = Path(tmp) / "science"
            queue_dir = broker_dir / "queue"
            paused_dir = broker_dir / "paused"
            queue_dir.mkdir(parents=True)
            paused_dir.mkdir(parents=True)
            (queue_dir / "job-queued.json").write_text(
                json.dumps({
                    "id": "job-queued",
                    "created_at": "2026-06-20T00:00:00",
                    "records": [{"doi": "10.1000/queued"}],
                    "output_dir": str(Path(tmp) / "queued-run"),
                }),
                encoding="utf-8",
            )
            (paused_dir / "job-paused.json").write_text(
                json.dumps({
                    "job_id": "job-paused",
                    "paused_at": "2026-06-20T00:10:00",
                    "pause_reason": "reauth_required",
                    "records": [{"doi": "10.1000/paused"}],
                    "output_dir": str(Path(tmp) / "paused-run"),
                }),
                encoding="utf-8",
            )
            state = BrokerState(
                publisher="science",
                profile_dir=str(Path(tmp) / "profile"),
                pid=12345,
                queue_dir=str(queue_dir),
                started_at="2026-06-19T00:00:00",
                ttl_seconds=259200,
                institution="Example University",
                status="reauth_required",
                paused_job_id="job-paused",
                paused_job_path=str(paused_dir / "job-paused.json"),
                paused_record_count=1,
                last_health_status="reauth_required",
                last_health_at="2026-06-19T22:00:00",
                last_health_error="logged_out",
                browser_extension_count=1,
                browser_extension_hash="abcdef1234567890",
            )
            with patch("instsci.session_broker.BROKER_ROOT", Path(tmp)), \
                 patch("instsci.session_broker.pid_is_running", return_value=True):
                write_broker_state(state)

                result = runner.invoke(app, ["session-broker-status", "-p", "science", "--json"])

        self.assertEqual(result.exit_code, 0, result.output)
        payload = json.loads(result.output)
        self.assertEqual(payload["publisher"], "science")
        self.assertTrue(payload["running"])
        self.assertEqual(payload["status"], "reauth_required")
        self.assertEqual(payload["institution"], "Example University")
        self.assertEqual(payload["paused_job"]["job_id"], "job-paused")
        self.assertIn("session-broker-resume -p science", payload["resume_command"])
        self.assertEqual(payload["health"]["status"], "reauth_required")
        self.assertEqual(payload["health"]["reason"], "logged_out")
        self.assertEqual(payload["browser_extensions"]["count"], 1)
        self.assertEqual(payload["browser_extensions"]["hash"], "abcdef1234567890")
        self.assertEqual(len(payload["queued_jobs"]), 1)
        self.assertEqual(len(payload["paused_jobs"]), 1)
        self.assertNotIn("cookie", result.output.lower())
        self.assertNotIn("token", result.output.lower())

    def test_submit_broker_job_pauses_new_work_when_broker_requires_reauth(self):
        with TemporaryDirectory() as tmp:
            queue_dir = Path(tmp) / "queue"
            queue_dir.mkdir()
            state = BrokerState(
                publisher="science",
                profile_dir=str(Path(tmp) / "profile"),
                pid=12345,
                queue_dir=str(queue_dir),
                started_at="2026-06-19T00:00:00",
                ttl_seconds=259200,
                status="reauth_required",
                human_assist_url="http://127.0.0.1:9999",
                last_health_status="reauth_required",
                last_health_error="logged_out",
            )
            records = [
                {"doi": "10.1126/science.one", "title": "One"},
                {"doi": "10.1126/science.two", "title": "Two"},
            ]

            with patch("instsci.session_broker.BROKER_ROOT", Path(tmp)), \
                 patch("instsci.session_broker.pid_is_running", return_value=True):
                write_broker_state(state)
                summary = submit_broker_job(
                    publisher="science",
                    records=records,
                    output_dir=str(Path(tmp) / "run"),
                    institution="Example University",
                    login_timeout=900,
                    pdf_timeout=90,
                    post_login_hold=0,
                    post_run_hold=0,
                    timeout_seconds=1,
                )
                paused = list_paused_jobs("science")
                reloaded = json.loads((Path(tmp) / "science" / "state.json").read_text(encoding="utf-8"))
                assist_state = json.loads(
                    (Path(tmp) / "science" / "human_assist" / "assist_state.json").read_text(encoding="utf-8")
                )

        self.assertEqual(summary["broker_status"], "reauth_required")
        self.assertEqual(summary["missing"], 2)
        self.assertEqual(summary["paused_record_count"], 2)
        self.assertIn("session-broker-resume -p science", summary["resume_command"])
        self.assertEqual(len(paused), 1)
        self.assertEqual(paused[0]["record_count"], 2)
        self.assertFalse(list(queue_dir.glob("*.json")), "reauth-gated work should not enter the live queue")
        self.assertEqual(reloaded["status"], "reauth_required")
        self.assertEqual(reloaded["paused_record_count"], 2)
        self.assertEqual(assist_state["paused_job_id"], summary["paused_job_id"])
        self.assertEqual(assist_state["paused_record_count"], 2)
        self.assertTrue(assist_state["credential_warning"])
        self.assertEqual(assist_state["credential_assist"]["provider"], "keepassxc")
        self.assertIn("<institution-idp-host>", assist_state["credential_assist"]["trigger_command"])

    def test_submit_broker_job_does_not_pause_for_dead_broker(self):
        with TemporaryDirectory() as tmp:
            queue_dir = Path(tmp) / "queue"
            queue_dir.mkdir()
            state = BrokerState(
                publisher="science",
                profile_dir=str(Path(tmp) / "profile"),
                pid=12345,
                queue_dir=str(queue_dir),
                started_at="2026-06-19T00:00:00",
                ttl_seconds=259200,
                status="reauth_required",
                last_health_status="reauth_required",
            )

            with patch("instsci.session_broker.BROKER_ROOT", Path(tmp)), \
                 patch("instsci.session_broker.pid_is_running", return_value=False):
                write_broker_state(state)
                with self.assertRaisesRegex(RuntimeError, "not running"):
                    submit_broker_job(
                        publisher="science",
                        records=[{"doi": "10.1126/science.dead", "title": "Dead"}],
                        output_dir=str(Path(tmp) / "run"),
                        institution="Example University",
                        login_timeout=900,
                        pdf_timeout=90,
                        post_login_hold=0,
                        post_run_hold=0,
                        timeout_seconds=1,
                    )

        self.assertFalse((Path(tmp) / "science" / "paused").exists())

    def test_session_broker_status_reports_multiple_paused_jobs(self):
        runner = CliRunner()
        with TemporaryDirectory() as tmp:
            paused_dir = Path(tmp) / "science" / "paused"
            paused_dir.mkdir(parents=True)
            (paused_dir / "job-one.json").write_text(
                json.dumps({"id": "job-one", "records": [{"doi": "10.1126/one"}], "paused_at": "2026-06-19T10:00:00"}),
                encoding="utf-8",
            )
            (paused_dir / "job-two.json").write_text(
                json.dumps({
                    "id": "job-two",
                    "records": [{"doi": "10.1126/two"}, {"doi": "10.1126/three"}],
                    "paused_at": "2026-06-19T11:00:00",
                }),
                encoding="utf-8",
            )
            state = BrokerState(
                publisher="science",
                profile_dir=str(Path(tmp) / "profile"),
                pid=12345,
                queue_dir=str(Path(tmp) / "science" / "queue"),
                started_at="2026-06-19T00:00:00",
                ttl_seconds=259200,
                status="reauth_required",
            )

            with patch("instsci.session_broker.BROKER_ROOT", Path(tmp)), \
                 patch("instsci.session_broker.pid_is_running", return_value=True):
                write_broker_state(state)
                result = runner.invoke(app, ["session-broker-status", "-p", "science"])

        self.assertEqual(result.exit_code, 0, result.output)
        self.assertIn("Paused jobs: 2", result.output)
        self.assertIn("job-one", result.output)
        self.assertIn("job-two", result.output)
        self.assertIn("2 records", result.output)

    def test_session_broker_status_reports_queued_jobs(self):
        runner = CliRunner()
        with TemporaryDirectory() as tmp:
            queue_dir = Path(tmp) / "science" / "queue"
            queue_dir.mkdir(parents=True)
            (queue_dir / "job-one.json").write_text(
                json.dumps({
                    "id": "job-one",
                    "records": [{"doi": "10.1126/one"}],
                    "created_at": "2026-06-19T10:00:00",
                }),
                encoding="utf-8",
            )
            (queue_dir / "job-two.json").write_text(
                json.dumps({
                    "id": "job-two",
                    "records": [{"doi": "10.1126/two"}, {"doi": "10.1126/three"}],
                    "created_at": "2026-06-19T11:00:00",
                }),
                encoding="utf-8",
            )
            (queue_dir / "job-done.done.json").write_text("{}", encoding="utf-8")
            state = BrokerState(
                publisher="science",
                profile_dir=str(Path(tmp) / "profile"),
                pid=12345,
                queue_dir=str(queue_dir),
                started_at="2026-06-19T00:00:00",
                ttl_seconds=259200,
                status="idle",
            )

            with patch("instsci.session_broker.BROKER_ROOT", Path(tmp)), \
                 patch("instsci.session_broker.pid_is_running", return_value=True):
                write_broker_state(state)
                queued = list_queued_jobs("science")
                result = runner.invoke(app, ["session-broker-status", "-p", "science"])

        self.assertEqual([job["job_id"] for job in queued], ["job-one", "job-two"])
        self.assertEqual(result.exit_code, 0, result.output)
        self.assertIn("Queued jobs: 2", result.output)
        self.assertIn("job-one", result.output)
        self.assertIn("job-two", result.output)
        self.assertIn("2 records", result.output)

    def test_session_broker_resume_command_submits_paused_job(self):
        runner = CliRunner()
        with patch("instsci.session_broker.resume_broker_job") as resume:
            resume.return_value = {
                "count": 1,
                "success": 1,
                "missing": 0,
                "unverified": 0,
                "verified_match": 1,
                "pdf_dir": "pdfs",
                "manifest": "manifest.csv",
                "publisher": "Science",
            }

            result = runner.invoke(
                app,
                ["session-broker-resume", "-p", "science", "--job-id", "job-paused", "--timeout", "30"],
            )

        self.assertEqual(result.exit_code, 0, result.output)
        resume.assert_called_once_with(publisher="science", job_id="job-paused", timeout_seconds=30)
        self.assertIn("resumed", result.output.lower())

    def test_session_broker_resume_all_command_resumes_all_paused_jobs(self):
        runner = CliRunner()
        with patch("instsci.session_broker.resume_all_broker_jobs") as resume_all:
            resume_all.return_value = {
                "count": 3,
                "success": 3,
                "missing": 0,
                "unverified": 0,
                "verified_match": 3,
                "publisher": "Science",
                "broker": True,
                "resumed_job_count": 2,
            }

            result = runner.invoke(
                app,
                ["session-broker-resume", "-p", "science", "--all", "--timeout", "30"],
            )

        self.assertEqual(result.exit_code, 0, result.output)
        resume_all.assert_called_once_with(publisher="science", timeout_seconds=30)
        self.assertIn("2 paused jobs", result.output)

    def test_resume_all_preserves_reauth_status_from_child_summary(self):
        paused_jobs = [
            {"job_id": "job-one", "paused_at": "2026-06-19T10:00:00"},
            {"job_id": "job-two", "paused_at": "2026-06-19T11:00:00"},
        ]
        child_summaries = [
            {"count": 1, "success": 1, "missing": 0, "unverified": 0, "verified_match": 1},
            {
                "count": 2,
                "success": 0,
                "missing": 2,
                "unverified": 0,
                "verified_match": 0,
                "broker_status": "reauth_required",
            },
        ]

        with patch("instsci.session_broker.list_paused_jobs", return_value=paused_jobs), \
             patch("instsci.session_broker.resume_broker_job", side_effect=child_summaries) as resume:
            summary = resume_all_broker_jobs(publisher="science", timeout_seconds=30)

        self.assertEqual(summary["resumed_job_count"], 2)
        self.assertEqual(summary["count"], 3)
        self.assertEqual(summary["broker_status"], "reauth_required")
        self.assertEqual([call.kwargs["job_id"] for call in resume.call_args_list], ["job-one", "job-two"])

    def test_session_broker_state_command_reports_masked_browser_proxy(self):
        runner = CliRunner()
        with TemporaryDirectory() as tmp:
            state = BrokerState(
                publisher="elsevier",
                profile_dir=str(Path(tmp) / "profile"),
                pid=12345,
                queue_dir=str(Path(tmp) / "queue"),
                started_at="2026-06-07T00:00:00",
                ttl_seconds=86400,
                browser_proxy_url="socks5://reader:****@example.proxy:1080",
                browser_proxy_url_hash="a" * 64,
            )
            with patch("instsci.session_broker.BROKER_ROOT", Path(tmp)), \
                 patch("instsci.session_broker.pid_is_running", return_value=True):
                write_broker_state(state)

                result = runner.invoke(app, ["session-broker-status", "-p", "elsevier"])

        self.assertEqual(result.exit_code, 0, result.output)
        self.assertIn("socks5://reader:****@example.proxy:1080", result.output)
        self.assertNotIn("secret", result.output)


if __name__ == "__main__":
    unittest.main()
