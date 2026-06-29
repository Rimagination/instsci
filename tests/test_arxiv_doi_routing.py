import unittest

from instsci.fetcher import PaperFetcher


class ArxivDoiRoutingTests(unittest.TestCase):
    """arXiv DataCite DOIs (10.48550/arXiv.*) must route to the arXiv source.

    Unpaywall does not index these, so without direct routing they escalate to
    institutional access and never download.
    """

    def test_arxiv_datacite_doi(self):
        self.assertEqual(
            PaperFetcher._arxiv_id_from_doi("10.48550/arXiv.2504.06435"),
            "2504.06435",
        )

    def test_case_insensitive_prefix(self):
        self.assertEqual(
            PaperFetcher._arxiv_id_from_doi("10.48550/ARXIV.2602.11522"),
            "2602.11522",
        )

    def test_non_arxiv_doi_returns_none(self):
        self.assertIsNone(
            PaperFetcher._arxiv_id_from_doi("10.1080/10447318.2023.2301250")
        )
        self.assertIsNone(
            PaperFetcher._arxiv_id_from_doi("10.1177/0018720810376055")
        )

    def test_blank(self):
        self.assertIsNone(PaperFetcher._arxiv_id_from_doi(""))


if __name__ == "__main__":
    unittest.main()
