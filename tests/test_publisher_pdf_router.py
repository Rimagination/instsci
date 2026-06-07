import unittest

from instsci.publisher_pdf_router import (
    build_pdf_candidates,
    discover_pdf_candidates_from_html,
    extract_elsevier_pii,
)
from instsci.publisher_profiles import (
    ELSEVIER_PROFILE,
    RSC_PROFILE,
    SPRINGER_PROFILE,
    WILEY_PROFILE,
    get_publisher_profile,
)


class PublisherPdfRouterTests(unittest.TestCase):
    def test_elsevier_pii_routes_to_pdfft_first_and_filters_mmc(self):
        candidates = build_pdf_candidates(
            ELSEVIER_PROFILE,
            "10.1016/j.watres.2026.126069",
            source_url="https://www.sciencedirect.com/science/article/pii/S0043135426007505",
            discovered_urls=[
                "https://ars.els-cdn.com/content/image/1-s2.0-S0043135426007505-mmc2.pdf",
                "https://www.sciencedirect.com/science/article/pii/S0043135426007505/pdfft?md5=abc",
            ],
        )

        self.assertEqual(
            candidates[0],
            "https://www.sciencedirect.com/science/article/pii/S0043135426007505/pdfft",
        )
        self.assertIn(
            "https://www.sciencedirect.com/science/article/pii/S0043135426007505/pdfft?md5=abc",
            candidates,
        )
        self.assertNotIn(
            "https://ars.els-cdn.com/content/image/1-s2.0-S0043135426007505-mmc2.pdf",
            candidates,
        )

    def test_elsevier_pii_extraction_handles_retrieve_and_signed_asset_urls(self):
        self.assertEqual(
            extract_elsevier_pii("https://linkinghub.elsevier.com/retrieve/pii/S0043135426007505"),
            "S0043135426007505",
        )
        self.assertEqual(
            extract_elsevier_pii(
                "https://pdf.sciencedirectassets.com/x/1-s2.0-S0043135426007505/main.pdf?pii=S0043135426007505"
            ),
            "S0043135426007505",
        )

    def test_rsc_article_landing_routes_to_article_pdf_path(self):
        candidates = build_pdf_candidates(
            RSC_PROFILE,
            "10.1039/d5cc06607g",
            source_url="https://pubs.rsc.org/en/content/articlelanding/2026/cc/d5cc06607g",
        )

        self.assertEqual(
            candidates[0],
            "https://pubs.rsc.org/en/content/articlepdf/2026/cc/d5cc06607g",
        )

    def test_springer_nature_templates_include_nature_pdf_suffix(self):
        candidates = build_pdf_candidates(SPRINGER_PROFILE, "10.1038/s41586-020-2649-2")

        self.assertIn("https://www.nature.com/articles/s41586-020-2649-2.pdf", candidates)

    def test_wiley_prefers_pdfdirect_before_pdf_and_epdf(self):
        candidates = build_pdf_candidates(WILEY_PROFILE, "10.1002/adfm.202525261")

        self.assertEqual(candidates[0], "https://onlinelibrary.wiley.com/doi/pdfdirect/10.1002/adfm.202525261")
        self.assertIn("https://onlinelibrary.wiley.com/doi/pdf/10.1002/adfm.202525261", candidates)
        self.assertIn("https://onlinelibrary.wiley.com/doi/epdf/10.1002/adfm.202525261", candidates)

    def test_html_discovery_extracts_meta_embed_and_query_param_pdf_urls(self):
        html = """
        <html><head>
          <meta name="citation_pdf_url" content="/pdf/main.pdf">
        </head><body>
          <a href="/viewer?file=https%3A%2F%2Fexample.org%2Farticle.pdf">View PDF</a>
          <embed type="application/pdf" src="/embedded.pdf">
        </body></html>
        """

        candidates = discover_pdf_candidates_from_html(html, "https://example.org/article")

        self.assertEqual(candidates[0], "https://example.org/pdf/main.pdf")
        self.assertIn("https://example.org/article.pdf", candidates)
        self.assertIn("https://example.org/embedded.pdf", candidates)

    def test_ieee_document_url_routes_to_stamp_pdf(self):
        profile = get_publisher_profile("ieee")

        candidates = build_pdf_candidates(
            profile,
            "10.1109/example",
            source_url="https://ieeexplore.ieee.org/document/9876543",
        )

        self.assertEqual(
            candidates[0],
            "https://ieeexplore.ieee.org/stampPDF/getPDF.jsp?tp=&isnumber=&arnumber=9876543",
        )
        self.assertIn(
            "https://ieeexplore.ieee.org/stamp/stamp.jsp?tp=&arnumber=9876543",
            candidates,
        )

    def test_aps_abstract_url_routes_to_pdf_path(self):
        profile = get_publisher_profile("aps")

        candidates = build_pdf_candidates(
            profile,
            "10.1103/PhysRevLett.123.456",
            source_url="https://journals.aps.org/prl/abstract/10.1103/PhysRevLett.123.456",
        )

        self.assertEqual(
            candidates[0],
            "https://journals.aps.org/prl/pdf/10.1103/PhysRevLett.123.456",
        )

    def test_aps_doi_has_link_aps_pdf_candidate(self):
        profile = get_publisher_profile("aps")

        candidates = build_pdf_candidates(profile, "10.1103/PhysRevLett.128.161102")

        self.assertIn("https://link.aps.org/pdf/10.1103/PhysRevLett.128.161102", candidates)

    def test_aps_section_page_does_not_route_to_fake_pdf(self):
        profile = get_publisher_profile("aps")

        candidates = build_pdf_candidates(
            profile,
            "10.1103/PhysRevLett.128.161102",
            source_url="https://journals.aps.org/prl/accepted",
        )

        self.assertNotIn("https://journals.aps.org/prl/accepted/pdf", candidates)

    def test_aps_discovered_pdf_links_must_match_current_doi(self):
        profile = get_publisher_profile("aps")

        candidates = build_pdf_candidates(
            profile,
            "10.1103/PhysRevLett.128.161102",
            source_url="https://journals.aps.org/prl/abstract/10.1103/PhysRevLett.128.161102",
            discovered_urls=[
                "https://journals.aps.org/prl/accepted/pdf",
                "https://journals.aps.org/prl/pdf/10.1103/PhysRevLett.999.000001",
                "https://journals.aps.org/prl/pdf/10.1103/PhysRevLett.128.161102",
            ],
        )

        self.assertIn(
            "https://journals.aps.org/prl/pdf/10.1103/PhysRevLett.128.161102",
            candidates,
        )
        self.assertNotIn("https://journals.aps.org/prl/accepted/pdf", candidates)
        self.assertNotIn(
            "https://journals.aps.org/prl/pdf/10.1103/PhysRevLett.999.000001",
            candidates,
        )

    def test_plos_doi_routes_to_printable_article_file(self):
        profile = get_publisher_profile("plos")

        candidates = build_pdf_candidates(profile, "10.1371/journal.pone.0000001")

        self.assertEqual(
            candidates[0],
            "https://journals.plos.org/plosone/article/file?id=10.1371/journal.pone.0000001&type=printable",
        )

    def test_copernicus_doi_routes_to_journal_pdf_path(self):
        profile = get_publisher_profile("copernicus")

        candidates = build_pdf_candidates(profile, "10.5194/acp-24-1-2024")

        self.assertEqual(
            candidates[0],
            "https://acp.copernicus.org/articles/24/1/2024/acp-24-1-2024.pdf",
        )

    def test_mdpi_landing_url_routes_to_pdf_path(self):
        profile = get_publisher_profile("mdpi")

        candidates = build_pdf_candidates(
            profile,
            "10.3390/foods10081757",
            source_url="https://www.mdpi.com/2304-8158/10/8/1757",
        )

        self.assertEqual(candidates[0], "https://www.mdpi.com/2304-8158/10/8/1757/pdf")

    def test_mdpi_doi_routes_to_known_issn_landing_pdf_path(self):
        profile = get_publisher_profile("mdpi")

        candidates = build_pdf_candidates(profile, "10.3390/foods10081757")

        self.assertEqual(candidates[0], "https://www.mdpi.com/2304-8158/10/8/1757/pdf")

    def test_atypon_profiles_build_direct_pdf_routes(self):
        expectations = {
            "aip": "https://pubs.aip.org/doi/epdf/10.1063/5.0237567",
            "ams": "https://journals.ametsoc.org/doi/epdf/10.1175/aies-d-23-0093.1",
            "pnas": "https://www.pnas.org/doi/epdf/10.1073/pnas.2309123120",
            "science": "https://www.science.org/doi/epdf/10.1126/sciadv.adp3964",
        }

        for key, expected_first in expectations.items():
            with self.subTest(publisher=key):
                doi = expected_first.rsplit("/doi/epdf/", 1)[1]
                candidates = build_pdf_candidates(get_publisher_profile(key), doi)
                self.assertEqual(candidates[0], expected_first)

    def test_ams_view_xml_url_routes_to_downloadpdf_path(self):
        profile = get_publisher_profile("ams")

        candidates = build_pdf_candidates(
            profile,
            "10.1175/aies-d-23-0093.1",
            source_url="https://journals.ametsoc.org/view/journals/aies/3/4/AIES-D-23-0093.1.xml",
        )

        self.assertEqual(
            candidates[0],
            "https://journals.ametsoc.org/downloadpdf/view/journals/aies/3/4/AIES-D-23-0093.1.pdf",
        )

    def test_direct_http_profiles_build_declared_pdf_routes(self):
        expectations = {
            "oxfordacademic": "https://academic.oup.com/doi/pdf/10.1093/nar/gkaa892",
            "royalsocietypublishing": "https://royalsocietypublishing.org/doi/pdf/10.1098/rsos.150470",
        }

        for key, expected_first in expectations.items():
            with self.subTest(publisher=key):
                doi = expected_first.rsplit("/doi/pdf/", 1)[1]
                candidates = build_pdf_candidates(get_publisher_profile(key), doi)
                self.assertEqual(candidates[0], expected_first)

    def test_oxford_article_page_routes_to_article_pdf_path(self):
        profile = get_publisher_profile("oxfordacademic")

        candidates = build_pdf_candidates(
            profile,
            "10.1093/nar/gkaa892",
            source_url="https://academic.oup.com/nar/article/49/D1/D10/5937080",
        )

        self.assertEqual(
            candidates[0],
            "https://academic.oup.com/nar/article-pdf/49/D1/D10/5937080/gkaa892.pdf",
        )


if __name__ == "__main__":
    unittest.main()
