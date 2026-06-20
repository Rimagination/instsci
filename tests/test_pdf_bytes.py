from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import Mock, patch

from instsci.pdf_bytes import MIN_PDF_BYTES, describe_non_pdf_bytes, is_plausible_pdf_bytes
from instsci.sources import arxiv, elsevier_api


class PdfBytesTests(unittest.TestCase):
    def test_plausible_pdf_requires_header_and_size(self):
        self.assertTrue(is_plausible_pdf_bytes(b"%PDF-" + b"x" * (MIN_PDF_BYTES + 1)))
        self.assertFalse(is_plausible_pdf_bytes(b"%PDF-" + b"x" * 100))
        self.assertFalse(is_plausible_pdf_bytes(b"<html>" + b"x" * (MIN_PDF_BYTES + 1)))

    def test_describe_non_pdf_bytes_reports_common_failures(self):
        self.assertEqual(describe_non_pdf_bytes(b""), "empty")
        self.assertEqual(describe_non_pdf_bytes(b"<html>" + b"x" * (MIN_PDF_BYTES + 1)), "html_response")
        self.assertEqual(describe_non_pdf_bytes(b"%PDF-" + b"x" * 100), "too_small")
        self.assertEqual(describe_non_pdf_bytes(b"not a pdf" + b"x" * (MIN_PDF_BYTES + 1)), "missing_pdf_header")

    def test_rejects_early_eof_with_trailing_payload(self):
        payload = b"%PDF-" + b"x" * 100 + b"%%EOF" + b"x" * (MIN_PDF_BYTES + 9000)

        self.assertFalse(is_plausible_pdf_bytes(payload))
        self.assertEqual(describe_non_pdf_bytes(payload), "early_eof_with_trailing_payload")

    def test_arxiv_download_rejects_html_payload(self):
        class FakeResponse:
            def raise_for_status(self):
                pass

            def iter_content(self, chunk_size=8192):
                yield b"<html>"
                yield b"x" * (MIN_PDF_BYTES + 1)

        with TemporaryDirectory() as tmp:
            output = Path(tmp) / "paper.pdf"

            with patch("instsci.sources.arxiv.request_with_retry", return_value=FakeResponse()):
                self.assertFalse(arxiv.download_pdf("2301.08745", str(output)))

            self.assertFalse(output.exists())
            self.assertFalse((Path(tmp) / "paper.pdf.tmp").exists())

    def test_elsevier_api_rejects_html_even_with_pdf_content_type(self):
        response = Mock()
        response.status_code = 200
        response.headers = {"content-type": "application/pdf"}
        response.content = b"<html>" + b"x" * (MIN_PDF_BYTES + 1)
        session = Mock()
        session.get.return_value = response

        with patch("instsci.sources.elsevier_api.requests.Session", return_value=session):
            self.assertIsNone(elsevier_api.fetch_pdf("10.1016/example", "key"))


if __name__ == "__main__":
    unittest.main()
