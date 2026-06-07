import asyncio
import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

import requests

from instsci.config import Config
from instsci.fetcher import PaperFetcher
from instsci.models import FetchResult, NextAction, Paper


def _config(base: Path, *, school: str = "") -> Config:
    return Config(
        school=school,
        email="test@example.com",
        output_dir=str(base / "papers"),
        cache_dir=str(base / "cache"),
        cookie_path=str(base / "cookies.json"),
        chrome_profile_dir=str(base / "chrome-profile"),
        carsi_cookie_dir=str(base / "carsi-cookies"),
        request_delay_min=0,
        request_delay_max=0,
    )


class FetchResultModelTests(unittest.TestCase):
    def test_full_text_paper_result_is_success_without_next_action(self):
        paper = Paper(
            doi="10.1002/example",
            title="A complete paper",
            full_text="Full text " * 200,
            pdf_path="papers/example.pdf",
            source="open_access",
        )

        result = FetchResult.from_paper(paper, min_fulltext_len=1000)

        self.assertEqual(result.status, "success")
        self.assertEqual(result.quality, "full_text")
        self.assertEqual(result.reason, "")
        self.assertIsNone(result.next_action)
        data = result.to_dict()
        self.assertEqual(data["paper"]["doi"], "10.1002/example")
        self.assertEqual(data["paper"]["source"], "open_access")

    def test_abstract_only_result_is_partial_with_login_next_action(self):
        paper = Paper(
            doi="10.1002/example",
            title="A partial paper",
            abstract="Only the abstract is available.",
            url="https://onlinelibrary.wiley.com/doi/10.1002/example",
        )

        result = FetchResult.from_paper(
            paper,
            min_fulltext_len=1000,
            institution_configured=True,
        )

        self.assertEqual(result.status, "partial")
        self.assertEqual(result.quality, "abstract_only")
        self.assertEqual(result.reason, "insufficient_full_text")
        self.assertIsNotNone(result.next_action)
        assert result.next_action is not None
        self.assertEqual(result.next_action.kind, "login")
        self.assertIn("instsci login", result.next_action.command)

    def test_pdf_only_result_suggests_pdf_inspection_not_login(self):
        paper = Paper(
            doi="10.1002/example",
            title="Downloaded PDF without extractable text",
            pdf_path="papers/example.pdf",
        )

        result = FetchResult.from_paper(
            paper,
            min_fulltext_len=1000,
            institution_configured=True,
        )

        self.assertEqual(result.status, "blocked")
        self.assertEqual(result.quality, "pdf_only")
        self.assertEqual(result.reason, "pdf_extraction_failed")
        self.assertIsNotNone(result.next_action)
        assert result.next_action is not None
        self.assertEqual(result.next_action.kind, "inspect_pdf")
        self.assertIn("papers/example.pdf", result.next_action.message)

    def test_markdown_result_includes_fetch_attempts(self):
        result = FetchResult(
            status="partial",
            quality="metadata_only",
            reason="insufficient_full_text",
            paper=Paper(doi="10.1002/example", title="Metadata only"),
            attempts=[
                {"stage": "open_access", "status": "partial", "reason": "metadata_only"},
                {
                    "stage": "doi_resolve",
                    "status": "success",
                    "detail": "https://publisher.example/articles/example",
                },
            ],
        )

        markdown = result.to_markdown()

        self.assertIn("**Attempts:**", markdown)
        self.assertIn("- open_access: partial (metadata_only)", markdown)
        self.assertIn("- doi_resolve: success - https://publisher.example/articles/example", markdown)


class FetcherResultTests(unittest.TestCase):
    def test_fetch_with_result_explains_missing_institution_config(self):
        with TemporaryDirectory() as tmp:
            fetcher = PaperFetcher(_config(Path(tmp), school=""))
            self.addCleanup(fetcher.close)
            paper = Paper(
                doi="10.1002/example",
                url="https://onlinelibrary.wiley.com/doi/10.1002/example",
            )

            with patch.object(fetcher, "fetch", return_value=paper):
                result = fetcher.fetch_with_result("10.1002/example", use_cache=False)

        self.assertEqual(result.status, "config_needed")
        self.assertEqual(result.reason, "institution_not_configured")
        self.assertIsNotNone(result.next_action)
        assert result.next_action is not None
        self.assertEqual(result.next_action.kind, "configure_institution")
        self.assertIn("instsci config-cmd --school", result.next_action.command)

    def test_fetch_with_result_records_attempts_for_provider_failures(self):
        with TemporaryDirectory() as tmp:
            fetcher = PaperFetcher(_config(Path(tmp), school="Configured University"))
            self.addCleanup(fetcher.close)
            doi = "10.1002/example"
            url = "https://publisher.example/articles/example"
            metadata = Paper(doi=doi, title="Metadata only")
            final = Paper(doi=doi, title="Metadata only", url=url)

            with (
                patch.object(fetcher, "_try_open_access", return_value=metadata),
                patch.object(fetcher, "_try_elsevier_api", return_value=None),
                patch.object(fetcher, "_resolve_doi", return_value=url),
                patch.object(fetcher, "_try_publisher_pdf", return_value=None),
                patch.object(fetcher, "_try_browser_pdf_download", return_value=None),
                patch.object(fetcher, "_fetch_via_webvpn", return_value=final),
            ):
                result = fetcher.fetch_with_result(doi, use_cache=False)

        self.assertEqual(result.status, "auth_required")
        self.assertEqual(result.reason, "institution_login_required")
        self.assertEqual(
            result.attempts,
            [
                {"stage": "open_access", "status": "partial", "reason": "metadata_only"},
                {"stage": "elsevier_api", "status": "miss", "reason": "no_result"},
                {"stage": "doi_resolve", "status": "success", "detail": url},
                {"stage": "publisher_pdf", "status": "miss", "reason": "no_result"},
                {"stage": "browser_pdf", "status": "miss", "reason": "no_result"},
                {"stage": "institutional_access", "status": "partial", "reason": "metadata_only"},
            ],
        )

    def test_fetch_with_result_suggests_identifier_check_when_doi_resolution_fails(self):
        with TemporaryDirectory() as tmp:
            fetcher = PaperFetcher(_config(Path(tmp), school="Configured University"))
            self.addCleanup(fetcher.close)
            doi = "10.1002/bad-doi"
            metadata = Paper(doi=doi, title="Metadata only")

            with (
                patch.object(fetcher, "_try_open_access", return_value=metadata),
                patch.object(fetcher, "_try_elsevier_api", return_value=None),
                patch.object(fetcher, "_resolve_doi", return_value=None),
            ):
                result = fetcher.fetch_with_result(doi, use_cache=False)

        self.assertEqual(result.status, "blocked")
        self.assertEqual(result.quality, "metadata_only")
        self.assertEqual(result.reason, "doi_resolution_failed")
        self.assertIsNotNone(result.next_action)
        assert result.next_action is not None
        self.assertEqual(result.next_action.kind, "check_identifier")
        self.assertIn("instsci search", result.next_action.command)
        self.assertEqual(result.attempts[-1], {"stage": "doi_resolve", "status": "miss", "reason": "no_url"})

    def test_fetch_with_result_records_pdf_only_attempt_when_pdf_text_extraction_fails(self):
        with TemporaryDirectory() as tmp:
            fetcher = PaperFetcher(_config(Path(tmp), school="Configured University"))
            self.addCleanup(fetcher.close)
            doi = "10.1002/example"
            url = "https://publisher.example/articles/example"
            metadata = Paper(doi=doi, title="Metadata only")
            final = Paper(doi=doi, title="Metadata only", url=url, pdf_path="papers/example.pdf")

            with (
                patch.object(fetcher, "_try_open_access", return_value=metadata),
                patch.object(fetcher, "_try_elsevier_api", return_value=None),
                patch.object(fetcher, "_resolve_doi", return_value=url),
                patch.object(fetcher, "_try_publisher_pdf", return_value=None),
                patch.object(fetcher, "_try_browser_pdf_download", return_value=None),
                patch.object(fetcher, "_fetch_via_webvpn", return_value=final),
            ):
                result = fetcher.fetch_with_result(doi, use_cache=False)

        self.assertEqual(result.status, "blocked")
        self.assertEqual(result.reason, "pdf_extraction_failed")
        self.assertEqual(
            result.attempts[-1],
            {"stage": "institutional_access", "status": "partial", "reason": "pdf_only"},
        )

    def test_fetch_with_result_diagnoses_institutional_gateway_network_errors(self):
        with TemporaryDirectory() as tmp:
            fetcher = PaperFetcher(_config(Path(tmp), school="Configured University"))
            self.addCleanup(fetcher.close)
            doi = "10.1002/example"
            url = "https://publisher.example/articles/example"
            metadata = Paper(doi=doi, title="Metadata only")

            with (
                patch.object(fetcher, "_try_open_access", return_value=metadata),
                patch.object(fetcher, "_try_elsevier_api", return_value=None),
                patch.object(fetcher, "_resolve_doi", return_value=url),
                patch.object(fetcher, "_try_publisher_pdf", return_value=None),
                patch.object(fetcher, "_try_browser_pdf_download", return_value=None),
                patch.object(
                    fetcher,
                    "_fetch_via_webvpn",
                    side_effect=requests.ConnectionError("gateway down"),
                ),
            ):
                result = fetcher.fetch_with_result(doi, use_cache=False)

        self.assertEqual(result.status, "blocked")
        self.assertEqual(result.reason, "gateway_unreachable")
        self.assertIsNotNone(result.next_action)
        assert result.next_action is not None
        self.assertEqual(result.next_action.kind, "diagnose_gateway")
        self.assertIn("instsci config-cmd --show", result.next_action.command)
        self.assertEqual(
            result.attempts[-1],
            {"stage": "institutional_access", "status": "error", "reason": "gateway_unreachable"},
        )

    def test_fetch_with_result_diagnoses_institutional_login_required_for_metadata_page(self):
        with TemporaryDirectory() as tmp:
            fetcher = PaperFetcher(_config(Path(tmp), school="Configured University"))
            self.addCleanup(fetcher.close)
            doi = "10.1002/example"
            url = "https://publisher.example/articles/example"
            metadata = Paper(doi=doi, title="Metadata only")
            final = Paper(doi=doi, title="Metadata only", url=url)

            with (
                patch.object(fetcher, "_try_open_access", return_value=metadata),
                patch.object(fetcher, "_try_elsevier_api", return_value=None),
                patch.object(fetcher, "_resolve_doi", return_value=url),
                patch.object(fetcher, "_try_publisher_pdf", return_value=None),
                patch.object(fetcher, "_try_browser_pdf_download", return_value=None),
                patch.object(fetcher, "_fetch_via_webvpn", return_value=final),
            ):
                result = fetcher.fetch_with_result(doi, use_cache=False)

        self.assertEqual(result.status, "auth_required")
        self.assertEqual(result.reason, "institution_login_required")
        self.assertIsNotNone(result.next_action)
        assert result.next_action is not None
        self.assertEqual(result.next_action.kind, "login")
        self.assertIn("instsci login", result.next_action.command)
        self.assertEqual(
            result.attempts[-1],
            {"stage": "institutional_access", "status": "partial", "reason": "metadata_only"},
        )


class MCPFetchResultTests(unittest.TestCase):
    def test_fetch_paper_json_returns_structured_result(self):
        from instsci import mcp_server

        class FakeFetcher:
            def fetch_with_result(self, identifier: str, use_cache: bool = True) -> FetchResult:
                return FetchResult(
                    status="auth_required",
                    quality="metadata_only",
                    reason="publisher_login_required",
                    paper=Paper(doi=identifier, title="Needs login"),
                    next_action=NextAction(
                        kind="login",
                        command=f"instsci login --identifier {identifier}",
                        message="Complete institutional login and retry.",
                    ),
                )

        with patch.object(mcp_server, "_get_fetcher", return_value=FakeFetcher()):
            payload = json.loads(
                asyncio.run(mcp_server.fetch_paper("10.1002/example", format="json"))
            )

        self.assertEqual(payload["status"], "auth_required")
        self.assertEqual(payload["reason"], "publisher_login_required")
        self.assertEqual(payload["next_action"]["kind"], "login")
        self.assertEqual(payload["paper"]["title"], "Needs login")

    def test_fetch_paper_json_lets_fetcher_explain_missing_institution_config(self):
        from instsci import mcp_server

        class FakeFetcher:
            def fetch_with_result(self, identifier: str, use_cache: bool = True) -> FetchResult:
                return FetchResult(
                    status="config_needed",
                    quality="none",
                    reason="institution_not_configured",
                    paper=Paper(doi=identifier),
                    next_action=NextAction(
                        kind="configure_institution",
                        command="instsci config-cmd --school YOUR_SCHOOL",
                        message="Configure your school or institution before retrying.",
                    ),
                )

        with TemporaryDirectory() as tmp:
            config = _config(Path(tmp), school="")
            with (
                patch.object(mcp_server.Config, "load", return_value=config),
                patch.object(mcp_server, "PaperFetcher", return_value=FakeFetcher()),
                patch.object(mcp_server, "_fetcher", None),
            ):
                payload = json.loads(
                    asyncio.run(mcp_server.fetch_paper("10.1002/example", format="json"))
                )

        self.assertEqual(payload["status"], "config_needed")
        self.assertEqual(payload["reason"], "institution_not_configured")
        self.assertEqual(payload["next_action"]["kind"], "configure_institution")


if __name__ == "__main__":
    unittest.main()
