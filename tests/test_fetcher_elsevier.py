import json
import inspect
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from instsci.config import Config
from instsci.fetcher import PaperFetcher
from instsci.models import Paper


class RecordingFetcher(PaperFetcher):
    def __init__(self):
        self._tmp = TemporaryDirectory()
        base = Path(self._tmp.name)
        super().__init__(
            Config(
                school="",
                email="test@example.com",
                elsevier_api_key="test-key",
                elsevier_inst_token="test-token",
                output_dir=str(base / "papers"),
                cache_dir=str(base / "cache"),
                cookie_path=str(base / "cookies.json"),
                chrome_profile_dir=str(base / "chrome-profile"),
                carsi_cookie_dir=str(base / "carsi-cookies"),
                request_delay_min=0,
                request_delay_max=0,
            )
        )
        self.cache_saves = 0
        self.saved_pdf: tuple[str, bytes] | None = None

    def close(self):
        super().close()
        self._tmp.cleanup()

    def _save_cache(self, paper: Paper):
        self.cache_saves += 1

    def _save_pdf(self, doi: str, pdf_bytes: bytes):
        self.saved_pdf = (doi, pdf_bytes)
        return None


class FetcherElsevierApiTests(unittest.TestCase):
    def test_fetcher_has_one_elsevier_api_helper(self):
        source = inspect.getsource(PaperFetcher)
        self.assertEqual(source.count("def _try_elsevier_api"), 1)

    def test_elsevier_xml_result_does_not_save_cache_inside_helper(self):
        fetcher = RecordingFetcher()
        self.addCleanup(fetcher.close)
        paper = Paper(doi="10.1016/example", url="https://www.sciencedirect.com/science/article/pii/S123")

        with patch("instsci.sources.elsevier_api.fetch_fulltext") as fetch_fulltext, \
             patch("instsci.sources.elsevier_api.fetch_pdf") as fetch_pdf:
            fetch_fulltext.return_value = {
                "title": "API title",
                "authors": ["Author One"],
                "abstract": "Abstract",
                "full_text": "Full text " * 200,
            }
            fetch_pdf.return_value = None

            result = fetcher._try_elsevier_api("10.1016/example", paper)

        self.assertIsNotNone(result)
        assert result is not None
        self.assertEqual(result.source, "elsevier_api")
        self.assertEqual(result.title, "API title")
        self.assertEqual(fetcher.cache_saves, 0)

    def test_elsevier_pdf_fallback_extracts_and_saves_pdf(self):
        fetcher = RecordingFetcher()
        self.addCleanup(fetcher.close)
        paper = Paper(doi="10.1016/example", url="https://www.sciencedirect.com/science/article/pii/S123")
        pdf_bytes = b"%PDF-" + b"x" * 12000

        with patch("instsci.sources.elsevier_api.fetch_fulltext", return_value=None), \
             patch("instsci.sources.elsevier_api.fetch_pdf", return_value=pdf_bytes), \
             patch("instsci.fetcher.pdf_extractor.extract_from_bytes", return_value="Extracted text " * 100):
            result = fetcher._try_elsevier_api("10.1016/example", paper)

        self.assertIs(result, paper)
        self.assertEqual(result.source, "elsevier_api")
        self.assertIn("Extracted text", result.full_text)
        self.assertEqual(fetcher.saved_pdf, ("10.1016/example", pdf_bytes))

    def test_save_pdf_rejects_html_payload(self):
        with TemporaryDirectory() as tmp:
            base = Path(tmp)
            fetcher = PaperFetcher(
                Config(
                    school="",
                    output_dir=str(base / "papers"),
                    cache_dir=str(base / "cache"),
                    cookie_path=str(base / "cookies.json"),
                    chrome_profile_dir=str(base / "chrome-profile"),
                    carsi_cookie_dir=str(base / "carsi-cookies"),
                    request_delay_min=0,
                    request_delay_max=0,
                )
            )
            self.addCleanup(fetcher.close)

            self.assertIsNone(fetcher._save_pdf("10.1016/html", b"<html>" + b"x" * 12000))
            self.assertEqual(list((base / "papers").glob("*.pdf")), [])

    def test_fetch_tries_elsevier_api_before_institutional_pdf_download(self):
        fetcher = RecordingFetcher()
        self.addCleanup(fetcher.close)
        calls: list[str] = []

        def fake_api(doi: str, paper: Paper) -> Paper:
            calls.append("api")
            paper.full_text = "API full text " * 100
            paper.source = "elsevier_api"
            return paper

        def fake_publisher_pdf(doi: str, resolved_url: str, paper: Paper):
            calls.append("publisher_pdf")
            return None

        with patch.object(fetcher, "_try_open_access", return_value=None), \
             patch.object(fetcher, "_resolve_doi", return_value="https://www.sciencedirect.com/science/article/pii/S123"), \
             patch.object(fetcher, "_try_elsevier_api", side_effect=fake_api), \
             patch.object(fetcher, "_try_publisher_pdf", side_effect=fake_publisher_pdf):
            result = fetcher.fetch("10.1016/example", use_cache=False)

        self.assertEqual(result.source, "elsevier_api")
        self.assertEqual(calls, ["api"])

    def test_browser_pdf_fallback_uses_profile_downloader(self):
        fetcher = RecordingFetcher()
        self.addCleanup(fetcher.close)
        paper = Paper(
            doi="10.1016/j.watres.2024.121507",
            url="https://www.sciencedirect.com/science/article/pii/S0043135424004093",
        )
        captured: dict[str, object] = {}

        class FakeAuth:
            browser_context = None

            def login(self, force: bool = False) -> bool:
                captured["legacy_login_force"] = force
                return False

            def close(self) -> None:
                pass

        class FakeDownloader:
            def __init__(self, config, *, profile, institution_query="", **kwargs):
                captured["config"] = config
                captured["profile_name"] = profile.name
                captured["institution_query"] = institution_query
                captured["kwargs"] = kwargs

            def run_records(self, records, run_dir, **kwargs):
                captured["records"] = records
                captured["run_dir"] = Path(run_dir)
                captured["run_kwargs"] = kwargs
                pdf_path = Path(run_dir) / "complete" / "pdfs" / "paper.pdf"
                pdf_path.parent.mkdir(parents=True, exist_ok=True)
                pdf_path.write_bytes(b"%PDF-" + b"x" * 12000)
                manifest_path = Path(run_dir) / "complete" / "manifest.json"
                manifest_path.write_text(
                    json.dumps(
                        [
                            {
                                "doi": paper.doi,
                                "status": "success",
                                "pdf_path": str(pdf_path),
                                "text_length": 1200,
                                "verified_match": True,
                            }
                        ]
                    ),
                    encoding="utf-8",
                )
                return {"manifest": str(Path(run_dir) / "complete" / "manifest.csv")}

        fetcher._auth = FakeAuth()

        with patch("instsci.publisher_batch.PublisherBatchDownloader", FakeDownloader), \
             patch("instsci.fetcher.pdf_extractor.extract_text", return_value="browser text " * 120):
            result = fetcher._try_browser_pdf_download(paper.doi, paper.url, paper)

        self.assertIs(result, paper)
        self.assertEqual(result.source, "browser")
        self.assertIn("browser text", result.full_text)
        self.assertTrue(result.pdf_path.endswith("paper.pdf"))
        self.assertEqual(captured["profile_name"], "Elsevier")
        self.assertEqual(captured["records"][0].doi, paper.doi)
        self.assertEqual(captured["run_kwargs"]["target_verified"], 1)
        self.assertNotIn("legacy_login_force", captured)

    def test_browser_pdf_fallback_rejects_non_pdf_manifest_path(self):
        fetcher = RecordingFetcher()
        self.addCleanup(fetcher.close)
        paper = Paper(
            doi="10.1016/j.watres.2024.121507",
            url="https://www.sciencedirect.com/science/article/pii/S0043135424004093",
        )

        class FakeDownloader:
            def __init__(self, *args, **kwargs):
                pass

            def run_records(self, records, run_dir, **kwargs):
                pdf_path = Path(run_dir) / "complete" / "pdfs" / "paper.pdf"
                pdf_path.parent.mkdir(parents=True, exist_ok=True)
                pdf_path.write_bytes(b"<html>" + b"x" * 12000)
                manifest_path = Path(run_dir) / "complete" / "manifest.json"
                manifest_path.write_text(
                    json.dumps(
                        [
                            {
                                "doi": paper.doi,
                                "status": "success",
                                "pdf_path": str(pdf_path),
                                "text_length": 1200,
                                "verified_match": True,
                            }
                        ]
                    ),
                    encoding="utf-8",
                )
                return {"manifest": str(Path(run_dir) / "complete" / "manifest.csv")}

        with patch("instsci.publisher_batch.PublisherBatchDownloader", FakeDownloader), \
             patch("instsci.fetcher.pdf_extractor.extract_text") as extract_text:
            result = fetcher._try_browser_pdf_download(paper.doi, paper.url, paper)

        self.assertIsNone(result)
        self.assertEqual(paper.pdf_path, "")
        extract_text.assert_not_called()


if __name__ == "__main__":
    unittest.main()
