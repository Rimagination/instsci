import os
import unittest

from instsci.publisher_access import verify_publishers


LIVE_ENABLED = os.environ.get("INSTSCI_LIVE_PUBLISHER_TESTS") == "1"


@unittest.skipUnless(
    LIVE_ENABLED,
    "set INSTSCI_LIVE_PUBLISHER_TESTS=1 to run publisher network smoke tests",
)
class PublisherLiveSmokeTests(unittest.TestCase):
    def test_catalog_sample_dois_resolve_and_build_pdf_candidates(self):
        results = verify_publishers(probe_pdf=False, timeout=30)

        for result in results:
            with self.subTest(publisher=result["profile_key"], doi=result["sample_doi"]):
                self.assertNotEqual(result["landing_status"], 404, result["redirect_chain"])
                self.assertTrue(result["resolved_to_expected_domain"], result["redirect_chain"])
                self.assertTrue(result["pdf_candidates"], result["landing_url"])


if __name__ == "__main__":
    unittest.main()
