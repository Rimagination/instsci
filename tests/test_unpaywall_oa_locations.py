import unittest

from instsci.sources.unpaywall import collect_pdf_urls


class CollectPdfUrlsTests(unittest.TestCase):
    """Every OA location should be offered as a fallback candidate.

    Regression: a forbidden publisher link must not hide an available
    repository copy (best location first, then the rest).
    """

    def test_best_first_then_other_locations_deduped(self):
        best = {"url_for_pdf": "https://pub.example/best.pdf", "host_type": "publisher"}
        locs = [
            {"url_for_pdf": "https://pub.example/best.pdf"},          # dup of best
            {"url_for_pdf": "https://repo.example/copy.pdf"},         # repository fallback
            {"url_for_pdf": ""},                                     # empty -> ignored
            {"url_for_landing_page": "https://repo.example/landing"},  # no pdf -> ignored
        ]
        self.assertEqual(
            collect_pdf_urls(best, locs),
            ["https://pub.example/best.pdf", "https://repo.example/copy.pdf"],
        )

    def test_no_best_location(self):
        locs = [{"url_for_pdf": "https://repo.example/a.pdf"}]
        self.assertEqual(collect_pdf_urls({}, locs), ["https://repo.example/a.pdf"])

    def test_empty(self):
        self.assertEqual(collect_pdf_urls({}, []), [])

    def test_strips_whitespace(self):
        self.assertEqual(collect_pdf_urls({"url_for_pdf": "  x.pdf  "}, []), ["x.pdf"])


if __name__ == "__main__":
    unittest.main()
