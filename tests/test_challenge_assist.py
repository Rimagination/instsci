import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch
from urllib.request import urlopen

from typer.testing import CliRunner

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
                    "challenge": {"kind": "cloudflare", "label": "Cloudflare browser challenge"},
                    "action": "Complete verification in the visible CloakBrowser window.",
                })

                status = json.loads(urlopen(server.url + "/status.json", timeout=5).read().decode("utf-8"))
                page = urlopen(server.url + "/", timeout=5).read().decode("utf-8")
            finally:
                server.stop()

        self.assertEqual(status["doi"], "10.1016/j.watres.test")
        self.assertEqual(status["challenge"]["kind"], "cloudflare")
        self.assertIn("visible CloakBrowser", page)
        self.assertIn("10.1016/j.watres.test", page)

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
        self.assertIn("--human-assist", papers_help)
        self.assertIn("File not found", publisher_parse.output)
        self.assertNotIn("No such option", publisher_parse.output)
        self.assertIn("File not found", papers_parse.output)
        self.assertNotIn("No such option", papers_parse.output)

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


if __name__ == "__main__":
    unittest.main()
