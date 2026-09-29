import json
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import MagicMock, patch

import requests
from typer.testing import CliRunner

from instsci.cli import app
from instsci.config import Config
from instsci.http_utils import request_with_retry
from instsci.job_store import retry_records
from instsci.publisher_batch import DownloadResult, PaperRecord, PublisherBatchDownloader
from instsci.retry_policy import failure_category


def response(status, retry_after=""):
    result = requests.Response()
    result.status_code = status
    result._content = b"{}"
    result._content_consumed = True
    result.headers["Retry-After"] = retry_after
    return result


class HttpRetryTests(unittest.TestCase):
    def test_retry_after_seconds_and_http_date(self):
        for value in ("12", "Thu, 01 Jan 1970 00:01:52 GMT"):
            with self.subTest(value=value), \
                 patch("instsci.http_utils.time.time", return_value=100), \
                 patch("instsci.http_utils.time.sleep") as sleep, \
                 patch("instsci.http_utils.requests.request", side_effect=[response(429, value), response(200)]) as request:
                self.assertEqual(request_with_retry("GET", "https://example.org").status_code, 200)
                sleep.assert_called_once_with(12)
                self.assertEqual(request.call_count, 2)

    def test_long_server_cooldown_does_not_retry_early(self):
        with patch("instsci.http_utils.requests.request", return_value=response(503, "120")) as request, \
             patch("instsci.http_utils.time.sleep") as sleep:
            self.assertEqual(request_with_retry("GET", "https://example.org").status_code, 503)
        request.assert_called_once()
        sleep.assert_not_called()

    def test_invalid_header_uses_backoff_and_closes_discarded_response(self):
        first = response(503, "invalid")
        with patch.object(first, "close") as close, \
             patch("instsci.http_utils.requests.request", side_effect=[first, response(200)]), \
             patch("instsci.http_utils.time.sleep") as sleep:
            request_with_retry("GET", "https://example.org")
        close.assert_called_once()
        sleep.assert_called_once_with(1)

    def test_access_errors_and_mutations_are_not_retried(self):
        for method, status in (("GET", 401), ("GET", 403), ("GET", 404), ("GET", 501), ("POST", 503)):
            with self.subTest(method=method, status=status), \
                 patch("instsci.http_utils.requests.request", return_value=response(status)) as request, \
                 patch("instsci.http_utils.time.sleep") as sleep:
                self.assertEqual(request_with_retry(method, "https://example.org").status_code, status)
                request.assert_called_once()
                sleep.assert_not_called()

    def test_timeout_is_bounded_and_invalid_url_is_not_retried(self):
        with patch("instsci.http_utils.requests.request", side_effect=requests.Timeout()) as request, \
             patch("instsci.http_utils.time.sleep") as sleep:
            with self.assertRaises(requests.Timeout):
                request_with_retry("GET", "https://example.org", max_retries=1)
        self.assertEqual(request.call_count, 2)
        sleep.assert_called_once_with(1)
        with patch("instsci.http_utils.requests.request", side_effect=requests.exceptions.MissingSchema()) as request:
            with self.assertRaises(requests.exceptions.MissingSchema):
                request_with_retry("GET", "bad")
        request.assert_called_once()

    def test_semantic_scholar_uses_shared_policy(self):
        from instsci.sources import semantic_scholar
        with patch("instsci.sources.semantic_scholar.request_with_retry", return_value=response(200)) as request:
            self.assertEqual(semantic_scholar._request_with_retry("https://example.org", {"query": "a"}), {})
        self.assertEqual(request.call_args.kwargs["max_retries"], 2)


class BrowserRetryTests(unittest.TestCase):
    def test_human_state_overrides_a_transient_reason(self):
        self.assertEqual(failure_category("pdf_not_captured", "challenge_or_viewer_timeout"), "human_action")
        self.assertEqual(failure_category("institution_pdf_entitlement_missing"), "access_denied")
        self.assertEqual(failure_category("unknown failure"), "attention_required")

    def test_both_entry_points_cool_down_and_preserve_latest_failure(self):
        for retained_context in (False, True):
            with self.subTest(retained_context=retained_context), TemporaryDirectory() as tmp:
                downloader = PublisherBatchDownloader(Config())
                records = [PaperRecord(doi=f"10.1021/{name}") for name in ("network", "captcha", "access", "timeout")]
                primary = [
                    DownloadResult(doi=records[0].doi, status="failed", reason="navigation_error"),
                    DownloadResult(doi=records[1].doi, status="failed", reason="challenge_or_viewer_timeout"),
                    DownloadResult(doi=records[2].doi, status="failed", reason="institution_pdf_entitlement_missing"),
                    DownloadResult(doi=records[3].doi, status="failed", reason="TimeoutError: navigation"),
                ]
                retried = [
                    DownloadResult(doi=records[0].doi, status="failed", reason="sso_required"),
                    DownloadResult(doi=records[3].doi, status="success", verified_match=True),
                ]
                method = "_run_once_in_context" if retained_context else "_run_once"
                context = object()
                with patch.object(downloader, method, side_effect=[primary, retried]) as run, \
                     patch.object(downloader, "_preauthenticate_carsi_portal"), \
                     patch.object(downloader, "_write_complete_artifacts", return_value={}), \
                     patch("instsci.publisher_batch.time.sleep") as sleep:
                    summary = (downloader.run_records_with_context(context, records, tmp) if retained_context
                               else downloader.run_records(records, tmp, concurrency=2))
                sleep.assert_called_once_with(30)
                args = run.call_args_list[1]
                self.assertEqual(args.args[1 if retained_context else 0], [records[0], records[3]])
                if retained_context:
                    self.assertIs(args.args[0], context)
                else:
                    self.assertEqual(args.kwargs["concurrency"], 1)
                self.assertEqual(summary["retry_attempted"], 2)
                self.assertEqual(summary["retry_success"], 1)
                self.assertEqual(summary["failure_categories"], {"human_action": 2, "access_denied": 1})

    def test_disabled_retries_report_zero_attempts(self):
        downloader = PublisherBatchDownloader(Config())
        record = PaperRecord(doi="10.1021/a")
        with TemporaryDirectory() as tmp, \
             patch.object(downloader, "_run_once", return_value=[DownloadResult(doi=record.doi, status="failed", reason="pdf_not_captured")]) as run, \
             patch.object(downloader, "_write_complete_artifacts", return_value={}), \
             patch("instsci.publisher_batch.time.sleep") as sleep:
            summary = downloader.run_records([record], tmp, retry_failed=False)
        self.assertEqual(summary["retry_eligible"], 1)
        self.assertEqual(summary["retry_attempted"], 0)
        run.assert_called_once()
        sleep.assert_not_called()

    def test_retry_pass_paces_items_and_retains_context(self):
        downloader = PublisherBatchDownloader(Config())
        context = MagicMock()
        records = [PaperRecord(doi=f"10.1021/{i}") for i in range(2)]
        with TemporaryDirectory() as tmp, \
             patch.object(downloader, "fetch_one", side_effect=lambda ctx, rec, out: DownloadResult(doi=rec.doi, status="failed")), \
             patch("instsci.publisher_batch.time.sleep") as sleep:
            downloader._run_once_in_context(context, records, Path(tmp), phase="retry")
        sleep.assert_called_once_with(5)
        context.close.assert_not_called()


class JobRecoveryTests(unittest.TestCase):
    def test_partial_manifest_keeps_unattempted_records_and_filters_transients(self):
        with TemporaryDirectory() as tmp:
            complete = Path(tmp) / "complete"
            complete.mkdir()
            records = [{"doi": f"10.1021/{key}"} for key in ("done", "timeout", "login", "pending")]
            (complete / "manifest.json").write_text(json.dumps([
                {**records[0], "status": "success", "verified_match": True},
                {**records[1], "status": "failed", "reason": "navigation_error"},
                {**records[2], "status": "failed", "reason": "sso_required"},
            ]))
            job = {"output_dir": tmp, "records": records}
            self.assertEqual(retry_records(job), records[1:])
            self.assertEqual(retry_records(job, transient_only=True), [records[1]])

    def test_resume_selection_is_not_suppressed_by_old_attempt_cache(self):
        job = {"id": "old", "broker_publisher": "acs", "records": [{"doi": "10.1021/a"}],
               "institution": "Example University", "skip_attempted": True}
        with patch("instsci.job_store.load_job", return_value=job), \
             patch("instsci.job_store.refresh_job", return_value=job), \
             patch("instsci.job_store.retry_records", return_value=job["records"]) as select, \
             patch("instsci.cli.Config.load", return_value=Config()), \
             patch("instsci.cli._ensure_session_broker", return_value=True), \
             patch("instsci.cli._enqueue_papers_job", return_value={"id": "new", "output_dir": "out"}) as enqueue:
            result = CliRunner().invoke(app, ["jobs", "resume", "old", "--transient-only"])
        self.assertEqual(result.exit_code, 0, result.output)
        select.assert_called_once_with(job, transient_only=True)
        self.assertFalse(enqueue.call_args.kwargs["skip_attempted"])


if __name__ == "__main__":
    unittest.main()
