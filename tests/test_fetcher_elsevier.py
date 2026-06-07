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


if __name__ == "__main__":
    unittest.main()
