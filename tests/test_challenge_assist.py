import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch
from urllib.request import urlopen
from io import StringIO

from rich.console import Console
from typer.testing import CliRunner

import instsci.cli as cli_module
from instsci.browser_actions import HumanHandoffState
from instsci.challenge_assist import HumanAssistServer, detect_challenge
from instsci.cli import app
from instsci.config import Config
from instsci.publisher_batch import DownloadResult, PublisherBatchDownloader
from instsci.publisher_profiles import ELSEVIER_PROFILE, get_publisher_profile


class ChallengeAssistTests(unittest.TestCase):
    def test_human_assist_server_serves_status_json_and_page(self):
        with TemporaryDirectory() as tmp:
            server = HumanAssistServer(host="127.0.0.1", port=0, state_dir=Path(tmp))
            server.start()
            try:
                server.update({
                    "doi": "10.1016/j.watres.test",
                    "publisher": "Elsevier",
                    "status": "reauth_required",
                    "challenge": {"kind": "cloudflare", "label": "Cloudflare browser challenge"},
                    "action": "Complete verification in the visible CloakBrowser window.",
                    "resume_command": "instsci session-broker-resume -p elsevier --job-id abc123",
                    "diagnostic_path": str(Path(tmp) / "diagnostic.json"),
                })

                status = json.loads(urlopen(server.url + "/status.json", timeout=5).read().decode("utf-8"))
                page = urlopen(server.url + "/", timeout=5).read().decode("utf-8")
            finally:
                server.stop()

        self.assertEqual(status["doi"], "10.1016/j.watres.test")
        self.assertEqual(status["challenge"]["kind"], "cloudflare")
        self.assertIn("visible CloakBrowser", page)
        self.assertIn("10.1016/j.watres.test", page)
        self.assertIn("reauth_required", page)
        self.assertIn("instsci session-broker-resume", page)
        self.assertIn("diagnostic.json", page)

    def test_human_assist_server_picks_up_external_state_file_updates(self):
        with TemporaryDirectory() as tmp:
            state_path = Path(tmp) / "assist_state.json"
            server = HumanAssistServer(host="127.0.0.1", port=0, state_dir=Path(tmp))
            server.start()
            try:
                state_path.write_text(
                    json.dumps(
                        {
                            "status": "reauth_required",
                            "publisher": "Science",
                            "paused_job_id": "job-from-submit",
                            "resume_command": "instsci session-broker-resume -p science --job-id job-from-submit",
                            "updated_at": "2026-06-19T23:30:00",
                        },
                        ensure_ascii=False,
                    ),
                    encoding="utf-8",
                )

                status = json.loads(urlopen(server.url + "/status.json", timeout=5).read().decode("utf-8"))
                page = urlopen(server.url + "/", timeout=5).read().decode("utf-8")
            finally:
                server.stop()

        self.assertEqual(status["paused_job_id"], "job-from-submit")
        self.assertIn("job-from-submit", page)
        self.assertIn("session-broker-resume", page)

    def test_human_assist_server_normalizes_reason_to_handoff_state(self):
        with TemporaryDirectory() as tmp:
            server = HumanAssistServer(host="127.0.0.1", port=0, state_dir=Path(tmp))
            server.start()
            try:
                server.update({
                    "status": "sso_required",
                    "doi": "10.1126/science.test",
                    "publisher": "Science",
                })

                status = json.loads(urlopen(server.url + "/status.json", timeout=5).read().decode("utf-8"))
                page = urlopen(server.url + "/", timeout=5).read().decode("utf-8")
            finally:
                server.stop()

        self.assertEqual(status["status"], HumanHandoffState.REAUTH_REQUIRED.value)
        self.assertEqual(status["status_reason"], "sso_required")
        self.assertIn("reauth_required", page)
        self.assertIn("sso_required", page)

    def test_human_assist_status_includes_keepassxc_credential_assist(self):
        with TemporaryDirectory() as tmp:
            server = HumanAssistServer(host="127.0.0.1", port=0, state_dir=Path(tmp))
            server.start()
            try:
                server.update({
                    "status": "sso_required",
                    "url": "https://idp.example.edu/login?RelayState=secret-token",
                    "title": "Example University Login - Chromium",
                    "credential_warning": True,
                })

                status = json.loads(urlopen(server.url + "/status.json", timeout=5).read().decode("utf-8"))
                page = urlopen(server.url + "/", timeout=5).read().decode("utf-8")
            finally:
                server.stop()

        assist = status["credential_assist"]
        self.assertEqual(assist["provider"], "keepassxc")
        self.assertEqual(assist["mode"], "auto_type")
        self.assertEqual(assist["expected_domain"], "idp.example.edu")
        self.assertEqual(assist["window_association_hint"], "*Example University Login*")
        self.assertIn("first_time_setup_steps", assist)
        self.assertIn("https://idp.example.edu/", json.dumps(assist))
        self.assertIn("{USERNAME}{TAB}{PASSWORD}", json.dumps(assist, ensure_ascii=False))
        self.assertIn("Ctrl+Alt+A", json.dumps(assist, ensure_ascii=False))
        self.assertIn("keepassxc-autotype --expected-domain idp.example.edu --trigger", assist["trigger_command"])
        self.assertIn("KeePassXC", page)
        self.assertIn("idp.example.edu", page)
        self.assertIn("*Example University Login*", page)
        self.assertIn("First-time KeePassXC setup checklist", page)
        self.assertIn("条目 -&gt; 编辑条目 -&gt; 自动输入", page)
        self.assertNotIn("secret-token", json.dumps(assist))
        self.assertNotIn("secret-token", page)

    def test_cli_exposes_human_assist_options_for_browser_workflows(self):
        runner = CliRunner()

        publisher_help = runner.invoke(app, ["publisher-batch", "--help"]).output
        papers_help = runner.invoke(app, ["papers", "--help"]).output
        publisher_parse = runner.invoke(
            app,
            [
                "publisher-batch",
                "--human-assist",
                "--human-assist-host",
                "127.0.0.1",
                "--human-assist-port",
                "0",
                "missing-dois.txt",
            ],
        )
        papers_parse = runner.invoke(
            app,
            [
                "papers",
                "--human-assist",
                "--human-assist-host",
                "127.0.0.1",
                "--human-assist-port",
                "0",
                "missing-dois.txt",
            ],
        )

        self.assertIn("--human-assist", publisher_help)
        self.assertIn("--no-human-ass", publisher_help)
        self.assertIn("--human-assist", papers_help)
        self.assertIn("--no-human-ass", papers_help)
        self.assertIn("File not found", publisher_parse.output)
        self.assertNotIn("No such option", publisher_parse.output)
        self.assertIn("File not found", papers_parse.output)
        self.assertNotIn("No such option", papers_parse.output)

    def test_download_summary_prints_keepassxc_assist_command(self):
        buffer = StringIO()
        test_console = Console(file=buffer, force_terminal=False, width=120)

        with patch.object(cli_module, "console", test_console):
            cli_module._print_download_summary({
                "count": 1,
                "success": 0,
                "unverified": 0,
                "broker_status": "reauth_required",
                "credential_assist": {
                    "trigger_command": (
                        "instsci keepassxc-autotype --expected-domain <institution-idp-host> --trigger"
                    ),
                },
            })

        output = buffer.getvalue()
        self.assertIn("keepassxc-autotype", output)
        self.assertIn("<institution-idp-host>", output)

    def test_cli_enables_human_assist_by_default_for_broker_runs(self):
        runner = CliRunner()
        with TemporaryDirectory() as tmp:
            doi_file = Path(tmp) / "dois.txt"
            doi_file.write_text("10.1126/science.test\n", encoding="utf-8")
            config = Config(
                school="Example University",
                output_dir=str(Path(tmp) / "out"),
                cache_dir=str(Path(tmp) / "cache"),
                cookie_path=str(Path(tmp) / "cookies.json"),
                chrome_profile_dir=str(Path(tmp) / "profile"),
                carsi_cookie_dir=str(Path(tmp) / "carsi"),
            )

            def run_command(*args):
                with patch("instsci.cli.Config.load", return_value=config), \
                     patch("instsci.session_broker.broker_is_running", side_effect=[False, True, True]), \
                     patch("instsci.session_broker.broker_identity_matches", return_value=(True, "")), \
                     patch("instsci.session_broker.start_broker_process") as start, \
                     patch("instsci.session_broker.submit_broker_job") as submit:
                    submit.return_value = {
                        "count": 1,
                        "success": 1,
                        "missing": 0,
                        "unverified": 0,
                        "verified_match": 1,
                        "pdf_dir": str(Path(tmp) / "pdfs"),
                        "manifest": str(Path(tmp) / "manifest.csv"),
                        "publisher": "Science",
                    }
                    result = runner.invoke(app, ["publisher-batch", str(doi_file), "-p", "science", *args])
                    return result, start

            default_result, default_start = run_command()
            disabled_result, disabled_start = run_command("--no-human-assist")

        self.assertEqual(default_result.exit_code, 0, default_result.output)
        self.assertTrue(default_start.call_args.kwargs["human_assist"])
        self.assertEqual(disabled_result.exit_code, 0, disabled_result.output)
        self.assertFalse(disabled_start.call_args.kwargs["human_assist"])

    def test_detects_named_challenge_families_without_false_positive(self):
        cases = [
            (
                "https://www.sciencedirect.com/science/article/pii/example/pdfft",
                "Are you a robot?",
                "Cloudflare Ray ID: abc Please verify you are human.",
                "cloudflare",
            ),
            (
                "https://example.org/login",
                "Security check",
                '<div class="cf-turnstile" data-sitekey="x">Turnstile</div>',
                "turnstile",
            ),
            (
                "https://example.org/login",
                "Security check",
                '<div class="g-recaptcha">reCAPTCHA</div>',
                "recaptcha",
            ),
            (
                "https://www.sciencedirect.com/crasolve",
                "ScienceDirect challenge",
                "crasolve challenge: complete the verification to continue",
                "crasolve",
            ),
            (
                "https://validate.perfdrive.com/session",
                "Checking access",
                "PerfDrive validates your browser before continuing",
                "perfdrive",
            ),
        ]

        for url, title, text, expected in cases:
            with self.subTest(expected=expected):
                detection = detect_challenge(url=url, title=title, text=text)
                self.assertIsNotNone(detection)
                self.assertEqual(detection.kind, expected)
                self.assertIn("visible CloakBrowser", detection.action)

        self.assertIsNone(
            detect_challenge(
                url="https://www.sciencedirect.com/science/article/pii/example",
                title="Article",
                text="Article text with verified methods, references, and supplementary information.",
            )
        )

    def test_wait_for_challenge_writes_checkpoint_screenshot_and_json(self):
        class FakeBody:
            def __init__(self, page):
                self.page = page

            def inner_text(self, **_kwargs):
                return self.page.body_text

        class FakePage:
            url = "https://www.sciencedirect.com/science/article/pii/example/pdfft"
            body_text = "Cloudflare Ray ID: abc Are you a robot? Please verify you are human."

            def __init__(self):
                self.screenshots = []
                self.front_calls = 0

            def title(self):
                return "Are you a robot?"

            def locator(self, _selector):
                return FakeBody(self)

            def bring_to_front(self):
                self.front_calls += 1

            def screenshot(self, path, full_page=True):
                self.screenshots.append((path, full_page))
                Path(path).write_bytes(b"png")

        cfg = Config(
            output_dir="out",
            cache_dir="cache",
            cookie_path="cookies.json",
            chrome_profile_dir="profile",
            carsi_cookie_dir="carsi",
        )
        downloader = PublisherBatchDownloader(cfg, profile=ELSEVIER_PROFILE, login_timeout_sec=5)
        result = DownloadResult(doi="10.1016/j.watres.test", status="failed")
        page = FakePage()

        with TemporaryDirectory() as tmp, patch("instsci.publisher_batch.time.sleep", return_value=None):
            self.assertFalse(downloader._wait_for_challenge(page, result, run_dir=Path(tmp)))
            diag_dir = Path(tmp) / "diagnostics" / "10.1016_j.watres.test"
            checkpoint = json.loads((diag_dir / "challenge_001.json").read_text(encoding="utf-8"))

        states = [event["state"] for event in result.events]
        self.assertIn("challenge_manual_wait", states)
        self.assertIn("challenge_window_front", states)
        self.assertIn("challenge_checkpoint", states)
        self.assertIn("challenge_timeout", states)
        self.assertEqual(checkpoint["challenge"]["kind"], "cloudflare")
        self.assertTrue(checkpoint["foregrounded"])
        self.assertTrue(checkpoint["screenshot_path"].endswith("challenge_001.png"))
        self.assertEqual(page.screenshots[0][1], True)
        self.assertEqual(page.front_calls, 1)

    def test_challenge_human_assist_status_records_foreground_attempt(self):
        class FakeBody:
            def inner_text(self, **_kwargs):
                return "Cloudflare Ray ID: abc Are you a robot? Please verify you are human."

        class FakePage:
            url = "https://example.org/challenge"

            def __init__(self):
                self.front_calls = 0

            def title(self):
                return "Are you a robot?"

            def locator(self, _selector):
                return FakeBody()

            def bring_to_front(self):
                self.front_calls += 1

            def screenshot(self, path, full_page=True):
                Path(path).write_bytes(b"png")

        cfg = Config(
            output_dir="out",
            cache_dir="cache",
            cookie_path="cookies.json",
            chrome_profile_dir="profile",
            carsi_cookie_dir="carsi",
        )
        downloader = PublisherBatchDownloader(
            cfg,
            profile=get_publisher_profile("world-scientific"),
            login_timeout_sec=5,
            human_assist=True,
        )
        result = DownloadResult(doi="10.1142/example", status="failed")

        with TemporaryDirectory() as tmp, patch("instsci.publisher_batch.time.sleep", return_value=None):
            self.assertFalse(downloader._wait_for_challenge(FakePage(), result, run_dir=Path(tmp)))
            status = json.loads((Path(tmp) / "human_assist" / "assist_state.json").read_text(encoding="utf-8"))

        self.assertTrue(status["foregrounded"])
        self.assertTrue(status["screenshot_path"].endswith("challenge_001.png"))
        self.assertIn("visible CloakBrowser", status["action"])

    def test_sso_login_page_publishes_human_handoff_without_body_excerpt(self):
        class FakeBody:
            def inner_text(self, **_kwargs):
                return "Username Password OTP"

        class FakePage:
            url = "https://idp.example.edu/login"

            def __init__(self):
                self.front_calls = 0

            def title(self):
                return "Institution login"

            def locator(self, _selector):
                return FakeBody()

            def bring_to_front(self):
                self.front_calls += 1

        class FakeDownloader(PublisherBatchDownloader):
            def _dismiss_cookie_banners(self, _page, _result):
                return None

            def _click_sso_entry(self, _page, _result=None):
                return False

            def _click_openathens_entry(self, _page, _result=None):
                return False

            def _select_institution(self, _page, _result):
                return False

            def _is_human_login_page(self, _page):
                return True

            def _has_publisher_institution_session(self, _page):
                return False

        cfg = Config(
            output_dir="out",
            cache_dir="cache",
            cookie_path="cookies.json",
            chrome_profile_dir="profile",
            carsi_cookie_dir="carsi",
        )
        downloader = FakeDownloader(
            cfg,
            profile=get_publisher_profile("science"),
            login_timeout_sec=0,
            human_assist=True,
        )
        result = DownloadResult(doi="10.1126/science.test", status="failed")
        page = FakePage()

        with TemporaryDirectory() as tmp:
            self.assertFalse(downloader._complete_login_from_current_page(page, result, Path(tmp)))
            status = json.loads((Path(tmp) / "human_assist" / "assist_state.json").read_text(encoding="utf-8"))

        self.assertEqual(status["status"], "institution_login_required")
        self.assertEqual(status["doi"], "10.1126/science.test")
        self.assertEqual(status["publisher"], "Science")
        self.assertEqual(status["title"], "Institution login")
        self.assertTrue(status["credential_warning"])
        self.assertTrue(status["foregrounded"])
        self.assertNotIn("body_excerpt", status)
        self.assertEqual(page.front_calls, 1)

    def test_diagnostic_redacts_login_body_excerpt(self):
        class FakeBody:
            def inner_text(self, **_kwargs):
                return "Username reader@example.edu Password Hunter2 OTP 123456 Recovery code ABCD"

        class FakePage:
            url = "https://idp.example.edu/login"

            def title(self):
                return "Institution Login"

            def locator(self, _selector):
                return FakeBody()

            def screenshot(self, path, full_page=True):
                Path(path).write_bytes(b"png")

        cfg = Config(
            output_dir="out",
            cache_dir="cache",
            cookie_path="cookies.json",
            chrome_profile_dir="profile",
            carsi_cookie_dir="carsi",
        )
        downloader = PublisherBatchDownloader(cfg, profile=get_publisher_profile("science"))
        result = DownloadResult(
            doi="10.1126/science.test",
            status="failed",
            reason="sso_required",
            state="sso_required",
        )

        with TemporaryDirectory() as tmp:
            downloader._write_diagnostic(FakePage(), result, Path(tmp))
            packet = json.loads(Path(result.diagnostic_path).read_text(encoding="utf-8"))

        self.assertTrue(packet["body_excerpt_redacted"])
        self.assertNotIn("Hunter2", packet["body_excerpt"])
        self.assertNotIn("123456", packet["body_excerpt"])
        self.assertNotIn("reader@example.edu", packet["body_excerpt"])
        self.assertIn("[redacted", packet["body_excerpt"].lower())

    def test_session_health_probe_marks_logged_out_without_body_excerpt(self):
        class FakeBody:
            def inner_text(self, **_kwargs):
                return "Access through your institution Username Password"

        class FakePage:
            def __init__(self):
                self.url = ""
                self.closed = False

            def goto(self, url, **_kwargs):
                self.url = url

            def title(self):
                return "Article"

            def locator(self, _selector):
                return FakeBody()

            def close(self):
                self.closed = True

        class FakeContext:
            def __init__(self):
                self.page = FakePage()

            def new_page(self):
                return self.page

        cfg = Config(
            output_dir="out",
            cache_dir="cache",
            cookie_path="cookies.json",
            chrome_profile_dir="profile",
            carsi_cookie_dir="carsi",
        )
        downloader = PublisherBatchDownloader(cfg, profile=get_publisher_profile("science"))
        context = FakeContext()

        health = downloader.check_session_health(context)

        self.assertEqual(health["status"], "reauth_required")
        self.assertEqual(health["reason"], "logged_out")
        self.assertNotIn("body_excerpt", health)
        self.assertTrue(context.page.closed)

    def test_browser_challenge_assist_mode_enables_human_assist_by_default(self):
        cfg = Config(
            output_dir="out",
            cache_dir="cache",
            cookie_path="cookies.json",
            chrome_profile_dir="profile",
            carsi_cookie_dir="carsi",
            browser_challenge_mode="assist",
        )

        downloader = PublisherBatchDownloader(cfg, profile=get_publisher_profile("world-scientific"))

        self.assertTrue(downloader.human_assist)
        self.assertEqual(downloader.browser_challenge_mode, "assist")

    def test_publisher_handoff_publishes_browser_observation(self):
        class FakeBody:
            def inner_text(self, **_kwargs):
                return "PDF\nAccess through your organization\nPassword should not be stored"

        class FakePage:
            url = "https://www.science.org/doi/10.1126/science.test"

            def title(self):
                return "Science article"

            def locator(self, _selector):
                return FakeBody()

        cfg = Config(
            output_dir="out",
            cache_dir="cache",
            cookie_path="cookies.json",
            chrome_profile_dir="profile",
            carsi_cookie_dir="carsi",
        )
        with TemporaryDirectory() as tmp:
            downloader = PublisherBatchDownloader(
                cfg,
                profile=get_publisher_profile("science"),
                human_assist=True,
            )
            try:
                downloader._publish_human_handoff(
                    FakePage(),
                    DownloadResult(doi="10.1126/science.test", status="failed"),
                    Path(tmp),
                    status="sso_required",
                )

                state = json.loads((Path(tmp) / "human_assist" / "assist_state.json").read_text(encoding="utf-8"))
            finally:
                if downloader._human_assist_server:
                    downloader._human_assist_server.stop()

        self.assertEqual(state["status"], HumanHandoffState.REAUTH_REQUIRED.value)
        self.assertEqual(state["status_reason"], "sso_required")
        self.assertEqual(state["browser_action"], "pause_for_user")
        self.assertEqual(state["observation"]["url"], "https://www.science.org/doi/10.1126/science.test")
        self.assertEqual(state["observation"]["title"], "Science article")
        self.assertIn("pdf", state["observation"]["text_markers"])
        self.assertIn("access through your organization", state["observation"]["text_markers"])
        self.assertNotIn("password", json.dumps(state["observation"]).lower())
        self.assertEqual(state["credential_assist"]["provider"], "keepassxc")


if __name__ == "__main__":
    unittest.main()
