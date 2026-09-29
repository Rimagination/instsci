import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import MagicMock, patch

import requests

from instsci.config import Config
from instsci.fetcher import PaperFetcher
from instsci.models import Paper
from instsci.sources.openaire import extract_oa_locations, find_oa_locations
from instsci.sources.unpaywall import OAResult

DOI = "10.1038/s41586-020-2649-2"
PDF = b"%PDF-1.7\n" + b"x" * 9000
TEXT = DOI + " Array programming with NumPy. " * 80


def product(doi=DOI, instances=None):
    return {"pids": [{"scheme": "doi", "value": doi}], "mainTitle": "Array programming with NumPy",
            "instances": instances or []}


def instance(urls, code="c_abf2"):
    return {"accessRight": {"code": code}, "urls": urls}


class OpenAireSourceTests(unittest.TestCase):
    def test_graph_shape_checks_identity_and_instance_rights(self):
        payload = {"results": [
            product("10.1002/another", [instance(["https://repo.example/wrong.pdf"])]),
            {**product(instances=[
                instance(["https://repo.example/item", "https://repo.example/one.pdf"]),
                instance(["https://repo.example/closed.pdf"], code="c_14cb"),
                {"urls": ["https://repo.example/unknown.pdf"]},
                instance(["https://repo.example/one.pdf", "https://doi.org/" + DOI]),
                instance(["file:///tmp/a.pdf", "https://secret:token@repo.example/a.pdf"]),
            ]), "bestAccessRight": {"code": "c_abf2"},
             "projects": [{"url": "https://repo.example/unrelated.pdf"}]},
        ]}
        locations = extract_oa_locations(payload, DOI)
        self.assertEqual([loc.pdf_url or loc.html_url for loc in locations],
                         ["https://repo.example/one.pdf", "https://repo.example/item"])

    def test_doi_urls_and_null_nodes_are_supported(self):
        payload = {"results": [None, product("https://doi.org/" + DOI.upper(), [
            None, {"accessRight": None}, {"accessRight": {"label": "Open Access"}, "urls": [None, "https://repo.example/a.pdf"]}
        ])]}
        self.assertEqual(len(extract_oa_locations(payload, DOI)), 1)
        for invalid in ([], None, {"results": {}}, {"results": [product(instances="broken")]}, {"results": [{"pids": "broken"}]}):
            with self.subTest(invalid=invalid):
                self.assertEqual(extract_oa_locations(invalid, DOI), [])

    def test_candidate_probes_are_bounded(self):
        payload = {"results": [product(instances=[instance([f"https://repo.example/{i}.pdf" for i in range(10)])])]}
        self.assertEqual(len(extract_oa_locations(payload, DOI)), 5)

    def test_lookup_uses_graph_v3_and_fails_soft_on_network_or_json_errors(self):
        resp = MagicMock()
        resp.__enter__.return_value = resp
        resp.json.return_value = {"results": [product(instances=[instance(["https://repo.example/a.pdf"])])]}
        with patch("instsci.sources.openaire.request_with_retry", return_value=resp) as request:
            self.assertEqual(len(find_oa_locations(DOI)), 1)
        self.assertIn("/graph/v3/research-products", request.call_args.args[1])
        self.assertEqual(request.call_args.kwargs["params"]["pid"], DOI)
        for error in (requests.Timeout(), ValueError("invalid JSON")):
            with patch("instsci.sources.openaire.request_with_retry", side_effect=error):
                self.assertEqual(find_oa_locations(DOI), [])


class OpenAireFetchTests(unittest.TestCase):
    def setUp(self):
        self.temp = TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name)
        self.fetcher = PaperFetcher(Config(output_dir=str(self.base / "pdfs"), cache_dir=str(self.base / "cache")))
        self.addCleanup(self.fetcher.close)
        rate = patch.object(self.fetcher, "_rate_limit")
        rate.start()
        self.addCleanup(rate.stop)

    def response(self, body=PDF, content_type="application/octet-stream"):
        resp = MagicMock()
        resp.content = body
        resp.text = body.decode("ascii", errors="replace")
        resp.headers = {"content-type": content_type}
        resp.url = "https://repo.example/item"
        return resp

    def test_openaire_is_lazy_after_successful_unpaywall(self):
        with patch("instsci.fetcher.unpaywall.check_oa", return_value=OAResult(is_oa=True, pdf_url="https://repo.example/a.pdf")), \
             patch("instsci.fetcher.request_with_retry", return_value=self.response()), \
             patch("instsci.fetcher.pdf_extractor.extract_from_bytes", return_value=TEXT), \
             patch("instsci.fetcher.openaire.find_oa_locations") as lookup:
            paper = self.fetcher._try_open_access(DOI)
        self.assertEqual(paper.full_text, TEXT)
        lookup.assert_not_called()

    def test_openaire_fallback_saves_pdf_even_with_generic_content_type(self):
        candidate = OAResult(is_oa=True, pdf_url="https://repo.example/a.pdf", title="Array programming with NumPy")
        with patch("instsci.fetcher.unpaywall.check_oa", return_value=OAResult(title="Original metadata")), \
             patch("instsci.fetcher.openaire.find_oa_locations", return_value=[candidate]), \
             patch("instsci.fetcher.request_with_retry", return_value=self.response()), \
             patch("instsci.fetcher.pdf_extractor.extract_from_bytes", return_value=TEXT):
            paper = self.fetcher._try_open_access(DOI)
        self.assertEqual(paper.full_text, TEXT)
        self.assertEqual(paper.title, "Original metadata")
        self.assertEqual(Path(paper.pdf_path).read_bytes(), PDF)

    def test_bad_bytes_and_wrong_article_do_not_get_saved(self):
        for body, text in (
            (b"<html>" + b"x" * 9000, TEXT),
            (PDF, "Unrelated material " * 100),
            (PDF, "10.9999/different\n" + "Other research " * 600 + "\nReferences\n" + DOI),
        ):
            with self.subTest(body=body[:5]), \
                 patch("instsci.fetcher.unpaywall.check_oa", return_value=OAResult()), \
                 patch("instsci.fetcher.openaire.find_oa_locations", return_value=[OAResult(is_oa=True, pdf_url="https://repo.example/a.pdf")]), \
                 patch("instsci.fetcher.request_with_retry", return_value=self.response(body, "application/pdf")), \
                 patch("instsci.fetcher.pdf_extractor.extract_from_bytes", return_value=text), \
                 patch.object(self.fetcher, "_save_pdf") as save:
                paper = self.fetcher._try_open_access(DOI)
                self.assertFalse(paper.pdf_path)
                self.assertFalse(paper.full_text)
                save.assert_not_called()

    def test_title_identity_requires_complete_header_lines(self):
        paper = Paper(doi=DOI, title="Array programming with NumPy")
        self.assertTrue(self.fetcher._oa_text_matches("Array programming\nwith NumPy\nAuthors\n" + "Body " * 500, paper))
        self.assertFalse(self.fetcher._oa_text_matches("Different article\nWe use array programming with NumPy.\n" + "Body " * 500, paper))
        self.assertFalse(self.fetcher._oa_text_matches("Different article\nReferences\n" + DOI, paper))

    def test_landing_page_can_return_pdf_or_lead_to_pdf(self):
        candidate = OAResult(is_oa=True, html_url="https://repo.example/item")
        for direct_binary in (True, False):
            responses = [self.response()] if direct_binary else [self.response(b"<html>item</html>", "text/html"), self.response()]
            with self.subTest(direct_binary=direct_binary), \
                 patch("instsci.fetcher.unpaywall.check_oa", return_value=OAResult()), \
                 patch("instsci.fetcher.openaire.find_oa_locations", return_value=[candidate]), \
                 patch("instsci.fetcher.request_with_retry", side_effect=responses), \
                 patch("instsci.fetcher.pdf_extractor.extract_from_bytes", return_value=TEXT), \
                 patch("instsci.fetcher.html_extractor.extract", return_value={"full_text": "short"}), \
                 patch.object(self.fetcher, "_find_pdf_link", return_value="https://repo.example/paper.pdf"):
                paper = self.fetcher._try_open_access(DOI)
                self.assertEqual(paper.full_text, TEXT)

    def test_failed_candidate_allows_next_candidate(self):
        candidates = [OAResult(is_oa=True, pdf_url=f"https://repo.example/{i}.pdf") for i in range(2)]
        with patch("instsci.fetcher.unpaywall.check_oa", return_value=OAResult()), \
             patch("instsci.fetcher.openaire.find_oa_locations", return_value=candidates), \
             patch("instsci.fetcher.request_with_retry", side_effect=[requests.Timeout(), self.response()]) as request, \
             patch("instsci.fetcher.pdf_extractor.extract_from_bytes", return_value=TEXT):
            paper = self.fetcher._try_open_access(DOI)
        self.assertEqual(request.call_count, 2)
        self.assertEqual(paper.full_text, TEXT)


if __name__ == "__main__":
    unittest.main()
