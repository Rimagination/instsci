from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from instsci.pdf_bytes import describe_non_pdf_bytes, is_plausible_pdf_bytes
from instsci.sources import arxiv, elsevier_api


class PdfBytesTests(unittest.TestCase):
    def test_pdf_bytes_rejects_html_payload(self):
        payload = b"<!doctype html><html><body>login</body></html>" + b"x" * 12000

        self.assertFalse(is_plausible_pdf_bytes(payload))
        self.assertEqual(describe_non_pdf_bytes(payload), "html_response")

    def test_arxiv_download_pdf_rejects_html_payload(self):
        payload = b"<html><body>not found</body></html>" + b"x" * 12000

        class FakeResponse:
            def raise_for_status(self):
                return None

            def iter_content(self, chunk_size=8192):
                yield payload[:chunk_size]
                yield payload[chunk_size:]

        with TemporaryDirectory() as tmp, \
             patch("instsci.sources.arxiv.request_with_retry", return_value=FakeResponse()):
            output = Path(tmp) / "paper.pdf"

            ok = arxiv.download_pdf("2601.00001", str(output))

            self.assertFalse(ok)
            self.assertFalse(output.exists())

    def test_elsevier_fetch_pdf_rejects_html_even_with_pdf_content_type(self):
        class FakeResponse:
            status_code = 200
            content = b"<html><body>access denied</body></html>" + b"x" * 12000
            headers = {"content-type": "application/pdf"}

        class FakeSession:
            trust_env = True

            def get(self, *_args, **_kwargs):
                return FakeResponse()

        with patch("instsci.sources.elsevier_api.requests.Session", return_value=FakeSession()):
            result = elsevier_api.fetch_pdf("10.1016/example", api_key="key")

        self.assertIsNone(result)


if __name__ == "__main__":
    unittest.main()
