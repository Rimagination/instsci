import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from typer.testing import CliRunner

from instsci import job_store
from instsci.config import Config
from instsci.cli import app
from instsci.session_broker import BrokerState, broker_key, pid_is_running, write_broker_state


class SessionBrokerTests(unittest.TestCase):
    def test_broker_key_normalizes_publisher_name(self):
        self.assertEqual(broker_key("Science Direct"), "science-direct")

    def test_pid_is_running_uses_windows_process_probe_without_signal(self):
        with patch("instsci.session_broker.sys.platform", "win32"), \
             patch("instsci.session_broker.os.kill", side_effect=AssertionError("do not signal Windows PIDs")), \
             patch("instsci.session_broker._pid_is_running_windows", return_value=True) as windows_probe:
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
        self.assertEqual(payload["institution"], "Example University")
        self.assertIn("Example University", payload["institution_aliases"])
        self.assertIn("broker", result.output.lower())

    def test_papers_records_latest_explicit_institution_choice(self):
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
                output_dir=str(Path(tmp) / "out"),
                cache_dir=str(Path(tmp) / "cache"),
                cookie_path=str(Path(tmp) / "cookies.json"),
                chrome_profile_dir=str(Path(tmp) / "profile"),
                carsi_cookie_dir=str(Path(tmp) / "carsi"),
            )
            save_calls: list[bool] = []
            config.save = lambda *args, **kwargs: save_calls.append(True)  # type: ignore[method-assign]

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
                    [
                        "papers",
                        str(doi_file),
                        "-p",
                        "elsevier",
                        "--institution",
                        "New Example University",
                    ],
                )

        self.assertEqual(result.exit_code, 0, result.output)
        self.assertTrue(save_calls)
        self.assertTrue(config.carsi_enabled)
        self.assertEqual(config.carsi_idp_name, "New Example University")
        self.assertEqual(config.institution_name_en, "New Example University")
        self.assertEqual(submit.call_args.kwargs["institution"], "New Example University")
        self.assertIn("New Example University", submit.call_args.kwargs["institution_aliases"])

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
                    input="Example University\n\n\n",
                )

        self.assertEqual(result.exit_code, 0, result.output)
        self.assertIn("Subscription institution", result.output)
        self.assertNotIn("Tsinghua", result.output)
        self.assertEqual(submit.call_args.kwargs["institution"], "Example University")
        self.assertEqual(config.carsi_idp_name, "Example University")

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

    def test_session_broker_status_json_reports_running_broker(self):
        runner = CliRunner()
        with TemporaryDirectory() as tmp:
            root = Path(tmp) / "brokers"
            state = BrokerState(
                publisher="elsevier",
                profile_dir=str(Path(tmp) / "profile"),
                pid=12345,
                queue_dir=str(Path(tmp) / "queue"),
                started_at="2026-06-07T00:00:00",
                ttl_seconds=259200,
            )
            with patch("instsci.session_broker.BROKER_ROOT", root), \
                 patch("instsci.session_broker.pid_is_running", return_value=True):
                write_broker_state(state)

                result = runner.invoke(app, ["session-broker-status", "-p", "elsevier", "--json"])

        self.assertEqual(result.exit_code, 0, result.output)
        payload = json.loads(result.output)
        self.assertEqual(payload["status"], "running")
        self.assertEqual(payload["publisher"], "elsevier")

    def test_papers_detach_enqueues_job_without_waiting(self):
        runner = CliRunner()
        with TemporaryDirectory() as tmp:
            base = Path(tmp)
            doi_file = base / "dois.txt"
            doi_file.write_text("10.1016/j.watres.2024.121507\n", encoding="utf-8")
            broker_root = base / "brokers"
            jobs_root = base / "jobs"
            queue_dir = base / "queue"
            queue_dir.mkdir()
            state = BrokerState(
                publisher="elsevier",
                profile_dir=str(base / "profile"),
                pid=12345,
                queue_dir=str(queue_dir),
                started_at="2026-06-07T00:00:00",
                ttl_seconds=259200,
            )
            config = Config(
                school="Example University",
                output_dir=str(base / "out"),
                cache_dir=str(base / "cache"),
                cookie_path=str(base / "cookies.json"),
                chrome_profile_dir=str(base / "profile"),
                carsi_cookie_dir=str(base / "carsi"),
            )

            with patch("instsci.cli.Config.load", return_value=config), \
                 patch("instsci.session_broker.BROKER_ROOT", broker_root), \
                 patch("instsci.job_store.JOBS_ROOT", jobs_root), \
                 patch("instsci.session_broker.pid_is_running", return_value=True):
                write_broker_state(state)

                result = runner.invoke(
                    app,
                    [
                        "papers",
                        str(doi_file),
                        "-p",
                        "elsevier",
                        "--detach",
                        "--output",
                        str(base / "run"),
                    ],
                )

                self.assertEqual(result.exit_code, 0, result.output)
                self.assertIn("Job submitted", result.output)
                queued = list(queue_dir.glob("*.json"))
                self.assertEqual(len(queued), 1)
                job_files = list(jobs_root.glob("*.json"))
                self.assertEqual(len(job_files), 1)
                job = json.loads(job_files[0].read_text(encoding="utf-8"))
                self.assertEqual(job["status"], "queued")
                self.assertEqual(job["record_count"], 1)
                self.assertEqual(job["broker_publisher"], "elsevier")

    def test_papers_detach_enqueues_carsi_portal_preauth(self):
        runner = CliRunner()
        with TemporaryDirectory() as tmp:
            base = Path(tmp)
            doi_file = base / "dois.txt"
            doi_file.write_text("10.1016/j.watres.2024.121507\n", encoding="utf-8")
            broker_root = base / "brokers"
            jobs_root = base / "jobs"
            queue_dir = base / "queue"
            queue_dir.mkdir()
            state = BrokerState(
                publisher="elsevier",
                profile_dir=str(base / "profile"),
                pid=12345,
                queue_dir=str(queue_dir),
                started_at="2026-06-07T00:00:00",
                ttl_seconds=259200,
            )
            config = Config(
                school="Example University",
                output_dir=str(base / "out"),
                cache_dir=str(base / "cache"),
                cookie_path=str(base / "cookies.json"),
                chrome_profile_dir=str(base / "profile"),
                carsi_cookie_dir=str(base / "carsi"),
            )

            with patch("instsci.cli.Config.load", return_value=config), \
                 patch("instsci.session_broker.BROKER_ROOT", broker_root), \
                 patch("instsci.job_store.JOBS_ROOT", jobs_root), \
                 patch("instsci.session_broker.pid_is_running", return_value=True):
                write_broker_state(state)

                result = runner.invoke(
                    app,
                    [
                        "papers",
                        str(doi_file),
                        "-p",
                        "elsevier",
                        "--detach",
                        "--carsi-portal-preauth",
                        "--output",
                        str(base / "run"),
                    ],
                )

                self.assertEqual(result.exit_code, 0, result.output)
                queued = json.loads(next(queue_dir.glob("*.json")).read_text(encoding="utf-8"))
                self.assertTrue(queued["carsi_portal_preauth"])
                job = json.loads(next(jobs_root.glob("*.json")).read_text(encoding="utf-8"))
                self.assertTrue(job["carsi_portal_preauth"])

    def test_jobs_status_marks_done_job_completed(self):
        runner = CliRunner()
        with TemporaryDirectory() as tmp:
            base = Path(tmp)
            jobs_root = base / "jobs"
            done_path = base / "queue" / "abc.done.json"
            done_path.parent.mkdir()
            done_path.write_text(
                json.dumps({"count": 1, "success": 1, "missing": 0, "unverified": 0}),
                encoding="utf-8",
            )
            job = {
                "id": "job-complete",
                "status": "queued",
                "publisher": "Elsevier",
                "broker_publisher": "elsevier",
                "records": [{"doi": "10.1016/example", "title": "", "published": "", "url": ""}],
                "record_count": 1,
                "output_dir": str(base / "run"),
                "institution": "Example University",
                "browser_profile": str(base / "profile"),
                "broker_job_id": "abc",
                "queue_job_path": str(base / "queue" / "abc.json"),
                "done_path": str(done_path),
                "created_at": "2026-06-20T00:00:00",
                "updated_at": "2026-06-20T00:00:00",
            }
            with patch("instsci.job_store.JOBS_ROOT", jobs_root):
                job_store.save_job(job)

                result = runner.invoke(app, ["jobs", "status", "job-complete", "--json"])

        self.assertEqual(result.exit_code, 0, result.output)
        payload = json.loads(result.output)
        self.assertEqual(payload["status"], "completed")

    def test_jobs_resume_requeues_only_missing_or_unverified_records(self):
        runner = CliRunner()
        with TemporaryDirectory() as tmp:
            base = Path(tmp)
            jobs_root = base / "jobs"
            broker_root = base / "brokers"
            queue_dir = base / "queue"
            queue_dir.mkdir()
            state = BrokerState(
                publisher="elsevier",
                profile_dir=str(base / "profile"),
                pid=12345,
                queue_dir=str(queue_dir),
                started_at="2026-06-07T00:00:00",
                ttl_seconds=259200,
            )
            output_dir = base / "old-run"
            complete_dir = output_dir / "complete"
            complete_dir.mkdir(parents=True)
            records = [
                {"doi": "10.1016/good", "title": "", "published": "", "url": ""},
                {"doi": "10.1016/missing", "title": "", "published": "", "url": ""},
            ]
            (complete_dir / "manifest.json").write_text(
                json.dumps(
                    [
                        {"doi": "10.1016/good", "status": "success", "verified_match": True},
                        {"doi": "10.1016/missing", "status": "missing", "verified_match": False},
                    ]
                ),
                encoding="utf-8",
            )
            job = {
                "id": "job-old",
                "status": "needs_attention",
                "publisher": "Elsevier",
                "broker_publisher": "elsevier",
                "records": records,
                "record_count": 2,
                "output_dir": str(output_dir),
                "institution": "Example University",
                "browser_profile": str(base / "profile"),
                "broker_job_id": "old",
                "queue_job_path": str(queue_dir / "old.json"),
                "done_path": str(queue_dir / "old.done.json"),
                "login_timeout": 900,
                "pdf_timeout": 90,
                "post_login_hold": 0,
                "post_run_hold": 0,
                "created_at": "2026-06-20T00:00:00",
                "updated_at": "2026-06-20T00:00:00",
            }
            config = Config(
                school="Example University",
                output_dir=str(base / "out"),
                cache_dir=str(base / "cache"),
                cookie_path=str(base / "cookies.json"),
                chrome_profile_dir=str(base / "profile"),
                carsi_cookie_dir=str(base / "carsi"),
            )

            with patch("instsci.cli.Config.load", return_value=config), \
                 patch("instsci.session_broker.BROKER_ROOT", broker_root), \
                 patch("instsci.job_store.JOBS_ROOT", jobs_root), \
                 patch("instsci.session_broker.pid_is_running", return_value=True):
                write_broker_state(state)
                job_store.save_job(job)

                result = runner.invoke(
                    app,
                    ["jobs", "resume", "job-old", "--output", str(base / "resume-run")],
                )

                self.assertEqual(result.exit_code, 0, result.output)
                self.assertIn("Job submitted", result.output)
                queue_jobs = [path for path in queue_dir.glob("*.json") if not path.name.endswith(".done.json")]
                self.assertEqual(len(queue_jobs), 1)
                queued = json.loads(queue_jobs[0].read_text(encoding="utf-8"))
                self.assertEqual([record["doi"] for record in queued["records"]], ["10.1016/missing"])
                resumed_jobs = [
                    json.loads(path.read_text(encoding="utf-8"))
                    for path in jobs_root.glob("*.json")
                    if path.stem != "job-old"
                ]
                self.assertEqual(len(resumed_jobs), 1)
                self.assertEqual(resumed_jobs[0]["parent_job_id"], "job-old")


if __name__ == "__main__":
    unittest.main()
