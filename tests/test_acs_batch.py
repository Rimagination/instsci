import json
from pathlib import Path
from tempfile import TemporaryDirectory
import threading
import time
import unittest
from unittest.mock import patch

from instsci.acs_batch import DownloadResult, PaperRecord, fetch_est_records, safe_name
from instsci.config import Config
from instsci.publisher_batch import ACSCloakBatchDownloader, MIN_PDF_BYTES, PublisherBatchDownloader
from instsci.publisher_profiles import (
    ACS_PROFILE,
    ELSEVIER_PROFILE,
    RSC_PROFILE,
    SPRINGER_PROFILE,
    WORLD_SCIENTIFIC_PROFILE,
    WILEY_PROFILE,
    PublisherProfile,
    get_publisher_profile,
    infer_publisher_profile,
    list_publisher_profiles,
)


class FakeResponse:
    def __init__(self, payload):
        self.payload = payload

    def raise_for_status(self):
        return None

    def json(self):
        return self.payload


class FakeSession:
    def __init__(self, payload):
        self.payload = payload
        self.last_url = ""

    def get(self, url, **kwargs):
        self.last_url = url
        return FakeResponse(self.payload)


class ACSBatchTests(unittest.TestCase):
    def test_safe_name_keeps_doi_readable(self):
        self.assertEqual(safe_name("10.1021/acs.est.6c00693"), "10.1021_acs.est.6c00693")

    def test_crossref_records_are_normalized(self):
        session = FakeSession(
            {
                "message": {
                    "items": [
                        {
                            "DOI": "10.1021/acs.est.test",
                            "title": ["  A   Test   Paper  "],
                            "published-online": {"date-parts": [[2026, 5, 31]]},
                            "URL": "https://doi.org/10.1021/acs.est.test",
                        }
                    ]
                }
            }
        )

        records = fetch_est_records(year=2026, limit=20, email="me@example.com", session=session)

        self.assertIn("issn%3A1520-5851", session.last_url)
        self.assertEqual(len(records), 1)
        self.assertEqual(records[0].doi, "10.1021/acs.est.test")
        self.assertEqual(records[0].title, "A Test Paper")
        self.assertEqual(records[0].published, "2026-05-31")

    def test_acs_profile_builds_article_and_pdf_urls(self):
        doi = "10.1021/acs.est.6c00693"

        self.assertEqual(ACS_PROFILE.article_url(doi), f"https://pubs.acs.org/doi/{doi}")
        self.assertIn(f"https://pubs.acs.org/doi/pdf/{doi}", ACS_PROFILE.pdf_urls(doi))

    def test_aps_profile_builds_concrete_article_url(self):
        doi = "10.1103/PhysRevLett.128.161102"

        self.assertEqual(
            get_publisher_profile("aps").article_url(doi),
            "https://journals.aps.org/prl/abstract/10.1103/PhysRevLett.128.161102",
        )

    def test_aps_success_url_excludes_accepted_papers(self):
        cfg = Config(
            output_dir="out",
            cache_dir="cache",
            cookie_path="cookies.json",
            chrome_profile_dir="profile",
            carsi_cookie_dir="carsi",
        )
        downloader = PublisherBatchDownloader(cfg, profile=get_publisher_profile("aps"))

        self.assertTrue(
            downloader._is_success_article_url(
                "https://journals.aps.org/prb/abstract/10.1103/PhysRevB.123.456"
            )
        )
        self.assertTrue(
            downloader._is_success_article_url(
                "https://journals.aps.org/prl/pdf/10.1103/PhysRevLett.128.161102"
            )
        )
        self.assertFalse(downloader._is_success_article_url("https://journals.aps.org/prl/accepted"))

    def test_aps_return_to_record_article_from_accepted_page(self):
        class FakePage:
            url = "https://journals.aps.org/prl/accepted"

            def goto(self, url, **_kwargs):
                self.url = url

        cfg = Config(
            output_dir="out",
            cache_dir="cache",
            cookie_path="cookies.json",
            chrome_profile_dir="profile",
            carsi_cookie_dir="carsi",
        )
        downloader = PublisherBatchDownloader(cfg, profile=get_publisher_profile("aps"))
        downloader._wait_for_challenge = lambda _page, _result: True  # type: ignore[method-assign]
        downloader._dismiss_cookie_banners = lambda _page, _result: False  # type: ignore[method-assign]
        result = DownloadResult(
            doi="10.1103/PhysRevLett.128.161102",
            status="failed",
            article_url="https://journals.aps.org/prl/abstract/10.1103/PhysRevLett.128.161102",
        )
        page = FakePage()

        with patch("instsci.publisher_batch.time.sleep", return_value=None):
            self.assertTrue(downloader._return_to_record_article_if_needed(page, result, result.doi))

        self.assertEqual(page.url, result.article_url)
        self.assertIn("record_article_return", [event["state"] for event in result.events])

    def test_aps_click_pdf_entry_prefers_current_doi_pdf_button(self):
        class FakePage:
            clicked_doi = ""
            goto_url = ""

            def evaluate(self, _script, doi):
                self.clicked_doi = doi
                return {
                    "selector": "aps-current-doi-pdf",
                    "text": "PDF",
                    "href": "https://journals.aps.org/prl/pdf/10.1103/PhysRevLett.128.161102",
                }

            def locator(self, _selector):
                raise AssertionError("generic PDF selectors should not run after APS DOI-specific click")

            def goto(self, url, **_kwargs):
                self.goto_url = url

        cfg = Config(
            output_dir="out",
            cache_dir="cache",
            cookie_path="cookies.json",
            chrome_profile_dir="profile",
            carsi_cookie_dir="carsi",
        )
        downloader = PublisherBatchDownloader(cfg, profile=get_publisher_profile("aps"))
        result = DownloadResult(doi="10.1103/PhysRevLett.128.161102", status="failed")
        page = FakePage()

        self.assertTrue(downloader._click_pdf_entry(page, result, doi=result.doi))
        self.assertEqual(page.clicked_doi, result.doi)
        self.assertEqual(page.goto_url, "https://journals.aps.org/prl/pdf/10.1103/PhysRevLett.128.161102")
        self.assertIn("aps-current-doi-pdf", result.events[-1]["detail"])

    def test_aps_article_auth_wall_opens_institution_username_login(self):
        class FakePage:
            url = "https://journals.aps.org/prl/abstract/10.1103/PhysRevLett.128.161102"
            goto_url = ""

            def evaluate(self, _script):
                return {
                    "text": "Log in with username/password provided by your institution",
                    "href": (
                        "https://journals.aps.org/login_inst_user?"
                        "rt=https%3A%2F%2Fjournals.aps.org%2Fprl%2Fabstract%2F10.1103%2FPhysRevLett.128.161102"
                    ),
                }

            def goto(self, url, **_kwargs):
                self.goto_url = url
                self.url = url

        cfg = Config(
            output_dir="out",
            cache_dir="cache",
            cookie_path="cookies.json",
            chrome_profile_dir="profile",
            carsi_cookie_dir="carsi",
        )
        downloader = PublisherBatchDownloader(cfg, profile=get_publisher_profile("aps"))
        result = DownloadResult(doi="10.1103/PhysRevLett.128.161102", status="failed")
        page = FakePage()

        self.assertTrue(downloader._open_aps_article_institution_login(page, result))
        self.assertIn("/login_inst_user?", page.goto_url)
        self.assertEqual(result.events[-1]["state"], "sso_entry_clicked")

    def test_profile_registry_resolves_aliases(self):
        self.assertIs(get_publisher_profile("acs"), ACS_PROFILE)
        self.assertIs(get_publisher_profile("ACS"), ACS_PROFILE)
        self.assertIs(get_publisher_profile("wiley"), WILEY_PROFILE)
        self.assertIs(get_publisher_profile("sciencedirect"), ELSEVIER_PROFILE)
        self.assertIs(get_publisher_profile("royal-society-of-chemistry"), RSC_PROFILE)
        self.assertIs(get_publisher_profile("nature"), SPRINGER_PROFILE)
        self.assertEqual(get_publisher_profile("acm").name, "ACM")
        self.assertEqual(get_publisher_profile("annual-reviews").name, "Annual Reviews")
        self.assertEqual(get_publisher_profile("frontiers").name, "Frontiers")
        self.assertEqual(get_publisher_profile("ieee").name, "IEEE")
        self.assertEqual(get_publisher_profile("iop").name, "IOP")
        self.assertEqual(get_publisher_profile("world-scientific").name, "World Scientific")
        self.assertEqual(get_publisher_profile("aip").name, "AIP Publishing")
        self.assertEqual(get_publisher_profile("ams").name, "AMS")
        self.assertEqual(get_publisher_profile("copernicus").name, "Copernicus")
        self.assertEqual(get_publisher_profile("mdpi").name, "MDPI")
        self.assertEqual(get_publisher_profile("oxfordacademic").name, "Oxford Academic")
        self.assertEqual(get_publisher_profile("plos").name, "PLOS")
        self.assertEqual(get_publisher_profile("pnas").name, "PNAS")
        self.assertEqual(get_publisher_profile("royalsocietypublishing").name, "Royal Society Publishing")
        self.assertEqual(get_publisher_profile("science").name, "Science")
        self.assertIn("acs", list_publisher_profiles())

    def test_profile_registry_infers_common_doi_prefixes(self):
        self.assertIs(infer_publisher_profile("10.1021/acs.est.6c00693"), ACS_PROFILE)
        self.assertIs(infer_publisher_profile("10.1002/example"), WILEY_PROFILE)
        self.assertIs(infer_publisher_profile("10.1016/j.watres.2024.121507"), ELSEVIER_PROFILE)
        self.assertIs(infer_publisher_profile("10.1039/example"), RSC_PROFILE)
        self.assertIs(infer_publisher_profile("10.1038/example"), SPRINGER_PROFILE)
        self.assertEqual(infer_publisher_profile("10.1145/example").name, "ACM")
        self.assertEqual(infer_publisher_profile("10.1103/example").name, "APS")
        self.assertEqual(infer_publisher_profile("10.1146/example").name, "Annual Reviews")
        self.assertEqual(infer_publisher_profile("10.3389/example").name, "Frontiers")
        self.assertEqual(infer_publisher_profile("10.1109/example").name, "IEEE")
        self.assertEqual(infer_publisher_profile("10.1088/example").name, "IOP")
        self.assertEqual(infer_publisher_profile("10.1142/example").name, "World Scientific")
        self.assertEqual(infer_publisher_profile("10.1063/example").name, "AIP Publishing")
        self.assertEqual(infer_publisher_profile("10.1175/example").name, "AMS")
        self.assertEqual(infer_publisher_profile("10.5194/acp-24-1-2024").name, "Copernicus")
        self.assertEqual(infer_publisher_profile("10.3390/foods10081757").name, "MDPI")
        self.assertEqual(infer_publisher_profile("10.1093/example").name, "Oxford Academic")
        self.assertEqual(infer_publisher_profile("10.1371/journal.pone.0000001").name, "PLOS")
        self.assertEqual(infer_publisher_profile("10.1073/example").name, "PNAS")
        self.assertEqual(infer_publisher_profile("10.1098/rsos.150470").name, "Royal Society Publishing")
        self.assertEqual(infer_publisher_profile("10.1126/science.ady3136").name, "Science")
        self.assertIsNone(infer_publisher_profile("10.9999/unknown"))

    def test_aps_and_iop_auth_markers_are_publisher_specific(self):
        aps = get_publisher_profile("aps")
        iop = get_publisher_profile("iop")

        self.assertNotIn("myiopscience.iop.org/signin", aps.auth_url_markers)
        self.assertNotIn("sesame.cld.iop.org", aps.auth_url_markers)
        self.assertIn("provided by your institution", aps.sso_text_markers)
        self.assertIn("myiopscience.iop.org/signin", iop.auth_url_markers)
        self.assertIn("sesame.cld.iop.org", iop.auth_url_markers)

    def test_high_roi_profiles_ship_sample_dois_for_live_smoke_tests(self):
        for profile in [WILEY_PROFILE, ELSEVIER_PROFILE, RSC_PROFILE, SPRINGER_PROFILE]:
            with self.subTest(profile=profile.name):
                self.assertGreaterEqual(len(profile.sample_dois), 1)
                self.assertIs(infer_publisher_profile(profile.sample_dois[0]), profile)

    def test_elsevier_profile_accepts_linkinghub_resolution(self):
        self.assertIn("linkinghub.elsevier.com/retrieve/pii", ELSEVIER_PROFILE.success_url_markers)

    def test_publisher_article_title_is_not_treated_as_auth_wall(self):
        class FakeBody:
            def inner_text(self, **_kwargs):
                return "Article abstract, supplementary files, citation links, and purchase options."

        class FakePage:
            url = "https://pubs.rsc.org/en/content/articlelanding/2026/cc/d5cc06607g"

            def title(self):
                return "Enantioselective C-H bond oxidation - Chemical Communications (RSC Publishing)"

            def locator(self, _selector):
                return FakeBody()

        cfg = Config(
            output_dir="out",
            cache_dir="cache",
            cookie_path="cookies.json",
            chrome_profile_dir="profile",
            carsi_cookie_dir="carsi",
        )
        downloader = PublisherBatchDownloader(cfg, profile=RSC_PROFILE)

        self.assertFalse(downloader._looks_logged_out(FakePage()))

    def test_elsevier_organization_access_prompt_is_auth_wall(self):
        class FakeBody:
            def inner_text(self, **_kwargs):
                return "Article preview Abstract References Access through your organization Purchase PDF"

        class FakePage:
            url = "https://www.sciencedirect.com/science/article/abs/pii/S0043135426003957"

            def title(self):
                return "Water Research article - ScienceDirect"

            def locator(self, _selector):
                return FakeBody()

        cfg = Config(
            output_dir="out",
            cache_dir="cache",
            cookie_path="cookies.json",
            chrome_profile_dir="profile",
            carsi_cookie_dir="carsi",
        )
        downloader = PublisherBatchDownloader(cfg, profile=ELSEVIER_PROFILE)

        self.assertTrue(downloader._looks_logged_out(FakePage()))

    def test_elsevier_full_text_access_is_not_auth_wall(self):
        class FakeBody:
            def inner_text(self, **_kwargs):
                return "Brought to you by:Tsinghua University Full text access View PDF Download full issue"

        class FakePage:
            url = "https://www.sciencedirect.com/science/article/pii/S0043135424004093"

            def title(self):
                return "Water Research article - ScienceDirect"

            def locator(self, _selector):
                return FakeBody()

        cfg = Config(
            output_dir="out",
            cache_dir="cache",
            cookie_path="cookies.json",
            chrome_profile_dir="profile",
            carsi_cookie_dir="carsi",
        )
        downloader = PublisherBatchDownloader(cfg, profile=ELSEVIER_PROFILE)

        self.assertFalse(downloader._looks_logged_out(FakePage()))
        self.assertTrue(downloader._article_access_available(FakePage()))

    def test_elsevier_tsinghua_purchase_pdf_requires_institution_refresh(self):
        class FakeBody:
            def inner_text(self, **_kwargs):
                return "Access through Tsinghua University Article preview Abstract Purchase PDF Access through another organization"

        class FakePage:
            url = "https://www.sciencedirect.com/science/article/abs/pii/S0043135424004093"

            def title(self):
                return "Water Research article - ScienceDirect"

            def locator(self, _selector):
                return FakeBody()

        cfg = Config(
            output_dir="out",
            cache_dir="cache",
            cookie_path="cookies.json",
            chrome_profile_dir="profile",
            carsi_cookie_dir="carsi",
        )
        downloader = PublisherBatchDownloader(cfg, profile=ELSEVIER_PROFILE)

        self.assertTrue(downloader._looks_logged_out(FakePage()))
        self.assertFalse(downloader._has_publisher_institution_session(FakePage()))
        self.assertEqual(downloader._login_block_reason(FakePage()), "")

    def test_elsevier_identity_authorization_title_is_auth_wall(self):
        class FakeBody:
            def inner_text(self, **_kwargs):
                return ""

        class FakePage:
            url = "https://linkinghub.elsevier.com/retrieve/pii/S0043135424004093"

            def title(self):
                return "Loading https://id.elsevier.com/as/authorization.oauth2?client_id=SDFE-v4"

            def locator(self, _selector):
                return FakeBody()

        cfg = Config(
            output_dir="out",
            cache_dir="cache",
            cookie_path="cookies.json",
            chrome_profile_dir="profile",
            carsi_cookie_dir="carsi",
        )
        downloader = PublisherBatchDownloader(cfg, profile=ELSEVIER_PROFILE)

        self.assertTrue(downloader._looks_logged_out(FakePage()))

    def test_article_page_authorization_required_is_auth_wall(self):
        class FakeBody:
            def inner_text(self, **_kwargs):
                return "Abstract PDF Authorization Required Log in via your institution provide your credentials"

        class FakePage:
            url = "https://journals.aps.org/prl/abstract/10.1103/PhysRevLett.128.161102"

            def title(self):
                return "Constraints on the Maximum Densities of Neutron Stars | Phys. Rev. Lett."

            def locator(self, _selector):
                return FakeBody()

        cfg = Config(
            output_dir="out",
            cache_dir="cache",
            cookie_path="cookies.json",
            chrome_profile_dir="profile",
            carsi_cookie_dir="carsi",
        )
        downloader = PublisherBatchDownloader(cfg, profile=get_publisher_profile("aps"))

        self.assertTrue(downloader._looks_logged_out(FakePage()))

    def test_ieee_institutional_sign_in_nav_alone_is_not_auth_wall(self):
        class FakeBody:
            def inner_text(self, **_kwargs):
                return "Create Account Personal Sign In Institutional Sign In PDF Abstract"

        class FakePage:
            url = "https://ieeexplore.ieee.org/document/11493918"

            def title(self):
                return "Inverse Design of Photonic Crystal Lasers | IEEE Xplore"

            def locator(self, _selector):
                return FakeBody()

        cfg = Config(
            output_dir="out",
            cache_dir="cache",
            cookie_path="cookies.json",
            chrome_profile_dir="profile",
            carsi_cookie_dir="carsi",
        )
        downloader = PublisherBatchDownloader(cfg, profile=get_publisher_profile("ieee"))

        self.assertFalse(downloader._looks_logged_out(FakePage()))

    def test_ieee_pdf_no_access_button_is_auth_wall(self):
        class FakeBody:
            def inner_text(self, **_kwargs):
                return "PDF You do not have access to this PDF Sign in to Continue Reading Abstract"

        class FakePage:
            url = "https://ieeexplore.ieee.org/document/11493918"

            def title(self):
                return "Inverse Design of Photonic Crystal Lasers | IEEE Xplore"

            def locator(self, _selector):
                return FakeBody()

        cfg = Config(
            output_dir="out",
            cache_dir="cache",
            cookie_path="cookies.json",
            chrome_profile_dir="profile",
            carsi_cookie_dir="carsi",
        )
        downloader = PublisherBatchDownloader(cfg, profile=get_publisher_profile("ieee"))

        self.assertTrue(downloader._looks_logged_out(FakePage()))

    def test_ieee_wayf_not_enabled_is_institution_not_registered(self):
        class FakeBody:
            def inner_text(self, **_kwargs):
                return "Institution Sign In Can't find your institution? Your institution may not be enabled for this type of authentication."

        class FakePage:
            def title(self):
                return ""

            def locator(self, _selector):
                return FakeBody()

        cfg = Config(
            output_dir="out",
            cache_dir="cache",
            cookie_path="cookies.json",
            chrome_profile_dir="profile",
            carsi_cookie_dir="carsi",
        )
        downloader = PublisherBatchDownloader(cfg, profile=get_publisher_profile("ieee"))

        self.assertEqual(downloader._login_block_reason(FakePage()), "institution_not_registered")

    def test_ieee_temporarily_unavailable_is_specific_block_reason(self):
        class FakeBody:
            def inner_text(self, **_kwargs):
                return "IEEE Xplore is temporarily unavailable We are working to restore service. onlinesupport@ieee.org"

        class FakePage:
            url = "https://ieeexplore.ieee.org/document/11493918/"

            def title(self):
                return "IEEE Xplore - Temporarily Unavailable"

            def locator(self, _selector):
                return FakeBody()

        cfg = Config(
            output_dir="out",
            cache_dir="cache",
            cookie_path="cookies.json",
            chrome_profile_dir="profile",
            carsi_cookie_dir="carsi",
        )
        downloader = PublisherBatchDownloader(cfg, profile=get_publisher_profile("ieee"))

        self.assertEqual(downloader._login_block_reason(FakePage()), "publisher_temporarily_unavailable")

    def test_ieee_pdf_no_access_uses_in_page_institution_entry(self):
        class FakeBody:
            def inner_text(self, **_kwargs):
                return "PDF You do not have access to this PDF Sign in to Continue Reading Institutional Sign In"

        class FakePage:
            url = "https://ieeexplore.ieee.org/document/11493918"

            def __init__(self):
                self.clicked = []

            def locator(self, _selector):
                return FakeBody()

            def evaluate(self, _script):
                self.clicked.append("Institutional Sign In")
                return {"text": "Institutional Sign In", "href": ""}

        cfg = Config(
            output_dir="out",
            cache_dir="cache",
            cookie_path="cookies.json",
            chrome_profile_dir="profile",
            carsi_cookie_dir="carsi",
        )
        page = FakePage()
        downloader = PublisherBatchDownloader(cfg, profile=get_publisher_profile("ieee"))
        result = DownloadResult(doi="10.1109/example", status="failed")

        self.assertTrue(downloader._click_sso_entry(page, result))
        self.assertEqual(page.clicked, ["Institutional Sign In"])
        self.assertEqual(result.events[-1]["state"], "sso_entry_clicked")
        self.assertNotIn("servlet/wayf.jsp", result.events[-1]["detail"])

    def test_ieee_profile_has_seamlessaccess_institution_search(self):
        profile = get_publisher_profile("ieee")

        self.assertIn("input[aria-label='Search for your Institution']", profile.institution_input_selectors)
        self.assertIn("xpath=(//*[normalize-space()='Search for your Institution']/following::input[1])", profile.institution_input_selectors)
        self.assertNotIn("input[type='search']", profile.institution_input_selectors)
        self.assertNotIn("input", profile.institution_input_selectors)
        self.assertTrue(any("Tsinghua University" in selector for selector in profile.institution_result_selectors))

    def test_wiley_clicks_read_full_text_entry(self):
        class FakePage:
            def evaluate(self, _script):
                return {
                    "selector": "wiley-read-full-text",
                    "text": "Read the full text",
                    "href": "https://onlinelibrary.wiley.com/doi/full/10.1002/ldr.5372",
                }

        cfg = Config(
            output_dir="out",
            cache_dir="cache",
            cookie_path="cookies.json",
            chrome_profile_dir="profile",
            carsi_cookie_dir="carsi",
        )
        downloader = PublisherBatchDownloader(cfg, profile=get_publisher_profile("wiley"))
        result = DownloadResult(doi="10.1002/ldr.5372", status="failed")

        self.assertTrue(downloader._click_wiley_read_full_text_entry(FakePage(), result))
        self.assertEqual(result.events[-1]["state"], "wiley_read_full_text_clicked")
        self.assertIn("Read the full text", result.events[-1]["detail"])

    def test_wiley_clicks_institutional_login_entry(self):
        class FakePage:
            def evaluate(self, _script):
                return {
                    "selector": "wiley-institutional-login",
                    "text": "Institutional Login",
                    "href": "",
                }

        cfg = Config(
            output_dir="out",
            cache_dir="cache",
            cookie_path="cookies.json",
            chrome_profile_dir="profile",
            carsi_cookie_dir="carsi",
        )
        downloader = PublisherBatchDownloader(cfg, profile=get_publisher_profile("wiley"))
        result = DownloadResult(doi="10.1002/ldr.5372", status="failed")

        self.assertTrue(downloader._click_wiley_institution_login_entry(FakePage(), result))
        self.assertEqual(result.events[-1]["state"], "sso_entry_clicked")
        self.assertIn("Institutional Login", result.events[-1]["detail"])

    def test_wiley_rejects_institution_help_pdf_for_record(self):
        cfg = Config(
            output_dir="out",
            cache_dir="cache",
            cookie_path="cookies.json",
            chrome_profile_dir="profile",
            carsi_cookie_dir="carsi",
        )
        downloader = PublisherBatchDownloader(cfg, profile=get_publisher_profile("wiley"))

        self.assertFalse(
            downloader._is_record_pdf_url(
                "https://id.tsinghua.edu.cn/res/pdf/SMRZ_guide_cn.pdf",
                "10.1002/ldr.5372",
            )
        )
        self.assertTrue(
            downloader._is_record_pdf_url(
                "https://onlinelibrary.wiley.com/doi/pdfdirect/10.1002/ldr.5372",
                "10.1002/ldr.5372",
            )
        )

    def test_select_institution_uses_recent_institution_without_query(self):
        class FakeLocator:
            first = None

            def __init__(self):
                self.first = self

            def is_visible(self, **_kwargs):
                return False

        class FakePage:
            url = "https://onlinelibrary.wiley.com/action/ssostart"

            def locator(self, _selector):
                return FakeLocator()

            def evaluate(self, _script, _options=None):
                return {
                    "selector": "recent-institution",
                    "text": "Example University",
                }

        cfg = Config(
            output_dir="out",
            cache_dir="cache",
            cookie_path="cookies.json",
            chrome_profile_dir="profile",
            carsi_cookie_dir="carsi",
        )
        downloader = PublisherBatchDownloader(cfg, profile=get_publisher_profile("wiley"))
        result = DownloadResult(doi="10.1002/ldr.5372", status="failed")

        self.assertTrue(downloader._select_institution(FakePage(), result))
        self.assertEqual(result.events[-1]["state"], "institution_selected")
        self.assertIn("Example University", result.events[-1]["detail"])

    def test_select_recent_institution_prefers_matching_openathens_card(self):
        class FakePage:
            url = "https://onlinelibrary.wiley.com/action/ssostart"

            def __init__(self):
                self.options = None

            def evaluate(self, script, options=None):
                self.options = options
                if options != {"institutionQuery": "清华大学"}:
                    return None
                if "queryMatches" not in script or "score += 100" not in script or "clickTargetFor" not in script:
                    return None
                return {
                    "selector": "recent-institution",
                    "text": "Tsinghua University (OpenAthens)",
                    "score": 200,
                }

        cfg = Config(
            output_dir="out",
            cache_dir="cache",
            cookie_path="cookies.json",
            chrome_profile_dir="profile",
            carsi_cookie_dir="carsi",
        )
        page = FakePage()
        downloader = PublisherBatchDownloader(
            cfg,
            profile=get_publisher_profile("wiley"),
            institution_query="清华大学",
        )
        result = DownloadResult(doi="10.1002/ldr.5372", status="failed")

        self.assertTrue(downloader._select_recent_institution(page, result))
        self.assertEqual(page.options, {"institutionQuery": "清华大学"})
        self.assertEqual(result.events[-1]["state"], "institution_selected")
        self.assertIn("Tsinghua University (OpenAthens)", result.events[-1]["detail"])

    def test_wiley_tsinghua_wayfless_uses_redirect_from_ssostart(self):
        class FakePage:
            url = "https://onlinelibrary.wiley.com/action/ssostart?redirectUri=%2Fdoi%2Ffull%2F10.1002%2Fldr.4101%3Fsaml_referrer"

            def __init__(self):
                self.goto_url = ""

            def goto(self, url, **_kwargs):
                self.goto_url = url

        cfg = Config(
            output_dir="out",
            cache_dir="cache",
            cookie_path="cookies.json",
            chrome_profile_dir="profile",
            carsi_cookie_dir="carsi",
        )
        page = FakePage()
        downloader = PublisherBatchDownloader(
            cfg,
            profile=get_publisher_profile("wiley"),
            institution_query="清华大学",
        )
        result = DownloadResult(doi="10.1002/ldr.4101", status="failed")

        self.assertTrue(downloader._select_wiley_tsinghua_openathens_wayfless(page, result))
        self.assertIn("idp=https%3A%2F%2Fidp.tsinghua.edu.cn%2Fopenathens", page.goto_url)
        self.assertIn("redirectUri=%2Fdoi%2Ffull%2F10.1002%2Fldr.4101%3Fsaml_referrer", page.goto_url)
        self.assertIn("wiley-tsinghua-openathens-wayfless", result.events[-1]["detail"])

    def test_wiley_clicks_current_record_pdf_entry(self):
        class FakePage:
            def __init__(self):
                self.options = None

            def evaluate(self, script, options=None):
                self.options = options
                if options != {"doi": "10.1002/ldr.4101"}:
                    return None
                if "wiley-pdf-entry" not in script or "/doi/pdfdirect/" not in script:
                    return None
                return {
                    "selector": "wiley-pdf-entry",
                    "text": "PDF",
                    "href": "https://onlinelibrary.wiley.com/doi/pdfdirect/10.1002/ldr.4101",
                    "score": 200,
                }

        cfg = Config(
            output_dir="out",
            cache_dir="cache",
            cookie_path="cookies.json",
            chrome_profile_dir="profile",
            carsi_cookie_dir="carsi",
        )
        page = FakePage()
        downloader = PublisherBatchDownloader(cfg, profile=get_publisher_profile("wiley"))
        result = DownloadResult(doi="10.1002/ldr.4101", status="failed")

        self.assertTrue(downloader._click_pdf_entry(page, result, doi=result.doi))
        self.assertEqual(page.options, {"doi": "10.1002/ldr.4101"})
        self.assertEqual(result.events[-1]["state"], "pdf_button_clicked")
        self.assertIn("wiley-pdf-entry", result.events[-1]["detail"])

    def test_ieee_institution_selection_uses_typeahead_result(self):
        class FakeLocator:
            def __init__(self, page, selector):
                self.page = page
                self.selector = selector
                self.first = self

            def is_visible(self, **_kwargs):
                return self.selector in {
                    "input[aria-label='Search for your Institution']",
                    "text=Example University",
                }

            def fill(self, value, **_kwargs):
                self.page.actions.append(("fill", self.selector, value))

            def type(self, value, **_kwargs):
                self.page.actions.append(("type", self.selector, value))

            def click(self, **_kwargs):
                self.page.actions.append(("click", self.selector))

        class FakePage:
            url = "https://ieeexplore.ieee.org/document/11493918"

            def __init__(self):
                self.actions = []

            def locator(self, selector):
                return FakeLocator(self, selector)

        cfg = Config(
            output_dir="out",
            cache_dir="cache",
            cookie_path="cookies.json",
            chrome_profile_dir="profile",
            carsi_cookie_dir="carsi",
        )
        page = FakePage()
        downloader = PublisherBatchDownloader(
            cfg,
            profile=get_publisher_profile("ieee"),
            institution_query="Example University",
        )
        result = DownloadResult(doi="10.1109/example", status="failed")

        self.assertTrue(downloader._select_institution(page, result))
        self.assertIn(("type", "input[aria-label='Search for your Institution']", "Example University"), page.actions)
        self.assertIn(("click", "text=Example University"), page.actions)
        self.assertEqual(result.events[-1]["state"], "institution_selected")

    def test_world_scientific_no_access_article_page_is_auth_wall(self):
        class FakeBody:
            def inner_text(self, **_kwargs):
                return "Sign in Institutional Access No Access Dual Multi-RAG Abstract"

        class FakePage:
            url = "https://www.worldscientific.com/doi/10.1142/s0218194026500348"

            def title(self):
                return "Dual Multi-RAG | World Scientific"

            def locator(self, _selector):
                return FakeBody()

        cfg = Config(
            output_dir="out",
            cache_dir="cache",
            cookie_path="cookies.json",
            chrome_profile_dir="profile",
            carsi_cookie_dir="carsi",
        )
        downloader = PublisherBatchDownloader(cfg, profile=get_publisher_profile("world-scientific"))

        self.assertTrue(downloader._looks_logged_out(FakePage()))

    def test_world_scientific_ssostart_stall_is_specific_reason(self):
        class FakeBody:
            def inner_text(self, **_kwargs):
                return "World Scientific Cookies Notification"

        class FakePage:
            url = "https://www.worldscientific.com/action/ssostart?redirectUri=%2F"

            def title(self):
                return "World Scientific"

            def locator(self, _selector):
                return FakeBody()

        cfg = Config(
            output_dir="out",
            cache_dir="cache",
            cookie_path="cookies.json",
            chrome_profile_dir="profile",
            carsi_cookie_dir="carsi",
        )
        downloader = PublisherBatchDownloader(cfg, profile=get_publisher_profile("world-scientific"))

        self.assertEqual(downloader._login_block_reason(FakePage()), "sso_redirect_stalled")

    def test_cloudflare_security_verification_is_challenge(self):
        class FakeBody:
            def inner_text(self, **_kwargs):
                return "www.worldscientific.com 正在进行安全验证 Ray ID: abc123 由 Cloudflare 提供"

        class FakePage:
            url = "https://www.worldscientific.com/action/ssostart?redirectUri=%2F"

            def title(self):
                return "请稍候"

            def locator(self, _selector):
                return FakeBody()

        cfg = Config(
            output_dir="out",
            cache_dir="cache",
            cookie_path="cookies.json",
            chrome_profile_dir="profile",
            carsi_cookie_dir="carsi",
        )
        downloader = PublisherBatchDownloader(cfg, profile=get_publisher_profile("world-scientific"))

        self.assertEqual(downloader._login_block_reason(FakePage()), "challenge_or_viewer_timeout")

    def test_tsinghua_unsupported_request_is_institution_not_registered(self):
        class FakeBody:
            def inner_text(self, **_kwargs):
                return "Web Login Service - Unsupported Request The application you have accessed is not registered for use with this service."

        class FakePage:
            def title(self):
                return "Web Login Service - Unsupported Request"

            def locator(self, _selector):
                return FakeBody()

        cfg = Config(
            output_dir="out",
            cache_dir="cache",
            cookie_path="cookies.json",
            chrome_profile_dir="profile",
            carsi_cookie_dir="carsi",
        )
        downloader = PublisherBatchDownloader(cfg, profile=get_publisher_profile("aps"))

        self.assertEqual(downloader._login_block_reason(FakePage()), "institution_not_registered")

    def test_article_page_get_access_is_auth_wall(self):
        class FakeBody:
            def inner_text(self, **_kwargs):
                return "Review Article Abstract vpn_key Get Access build Tools share Share"

        class FakePage:
            url = "https://www.annualreviews.org/content/journals/10.1146/example"

            def title(self):
                return "Annual Reviews article"

            def locator(self, _selector):
                return FakeBody()

        cfg = Config(
            output_dir="out",
            cache_dir="cache",
            cookie_path="cookies.json",
            chrome_profile_dir="profile",
            carsi_cookie_dir="carsi",
        )
        downloader = PublisherBatchDownloader(cfg, profile=get_publisher_profile("annual-reviews"))

        self.assertTrue(downloader._looks_logged_out(FakePage()))

    def test_article_page_with_soft_openathens_link_is_not_auth_wall(self):
        class FakeBody:
            def inner_text(self, **_kwargs):
                return "Article abstract, references, PDF, citations, OpenAthens, institutional login help."

        class FakePage:
            url = "https://www.annualreviews.org/content/journals/10.1146/annurev-phyto-011325-012824"

            def title(self):
                return "Extracellular Antagonists: Offense and Counter-Defense in the Apoplast | Annual Reviews"

            def locator(self, _selector):
                return FakeBody()

        cfg = Config(
            output_dir="out",
            cache_dir="cache",
            cookie_path="cookies.json",
            chrome_profile_dir="profile",
            carsi_cookie_dir="carsi",
        )
        downloader = PublisherBatchDownloader(cfg, profile=get_publisher_profile("annual-reviews"))

        self.assertFalse(downloader._looks_logged_out(FakePage()))

    def test_elsevier_profile_can_drive_organization_search(self):
        self.assertTrue(ELSEVIER_PROFILE.institution_input_selectors)
        self.assertTrue(ELSEVIER_PROFILE.institution_result_selectors)
        self.assertTrue(
            any("tsinghua" in selector.lower() for selector in ELSEVIER_PROFILE.institution_result_selectors)
        )

    def test_elsevier_prefers_real_tsinghua_institution_anchor(self):
        class FakePage:
            def __init__(self):
                self.clicked = False

            def evaluate(self, script):
                self.clicked = "auth.elsevier.com/shibauth/institutionlogin" in script.lower()
                return {
                    "selector": "elsevier-institution-access",
                    "text": "Access through Tsinghua University",
                    "href": "https://auth.elsevier.com/ShibAuth/institutionLogin?entityID=https%3A%2F%2Fidp.tsinghua.edu.cn%2Fidp%2Fshibboleth",
                    "score": 190,
                }

        cfg = Config(
            output_dir="out",
            cache_dir="cache",
            cookie_path="cookies.json",
            chrome_profile_dir="profile",
            carsi_cookie_dir="carsi",
        )
        page = FakePage()
        result = DownloadResult(doi="10.1016/example", status="failed")
        downloader = PublisherBatchDownloader(cfg, profile=ELSEVIER_PROFILE)

        self.assertTrue(downloader._click_sso_entry(page, result))
        self.assertTrue(page.clicked)
        self.assertIn("auth.elsevier.com/ShibAuth/institutionLogin", result.events[-1]["detail"])

    def test_elsevier_does_not_click_homepage_as_institution_entry(self):
        class FakeLocator:
            first = None

            def __init__(self):
                self.first = self

            def is_visible(self, **_kwargs):
                return False

        class FakePage:
            def evaluate(self, script):
                if "go to elsevier homepage" in script and "!matched" in script and ".filter(Boolean)" in script:
                    return None
                return {
                    "selector": "elsevier-institution-access",
                    "text": "Go to Elsevier Homepage",
                    "href": "http://www.elsevier.com/",
                    "score": 10,
                }

            def locator(self, _selector):
                return FakeLocator()

        cfg = Config(
            output_dir="out",
            cache_dir="cache",
            cookie_path="cookies.json",
            chrome_profile_dir="profile",
            carsi_cookie_dir="carsi",
        )
        result = DownloadResult(doi="10.1016/example", status="failed")
        downloader = PublisherBatchDownloader(cfg, profile=ELSEVIER_PROFILE)

        self.assertFalse(downloader._click_sso_entry(FakePage(), result))

    def test_elsevier_tsinghua_wayfless_uses_oauth_return_url(self):
        class FakePage:
            url = (
                "https://id.elsevier.com/as/authorization.oauth2?"
                "state=retryCounter%3D0%26returnUrl%3Dhttps%253A%252F%252Fwww.sciencedirect.com%252Fscience%252Farticle%252Fpii%252FS0929139326003835"
            )

            def __init__(self):
                self.goto_url = ""

            def goto(self, url, **_kwargs):
                self.goto_url = url

        cfg = Config(
            output_dir="out",
            cache_dir="cache",
            cookie_path="cookies.json",
            chrome_profile_dir="profile",
            carsi_cookie_dir="carsi",
        )
        page = FakePage()
        result = DownloadResult(doi="10.1016/j.apsoil.2026.107163", status="failed")
        downloader = PublisherBatchDownloader(
            cfg,
            profile=ELSEVIER_PROFILE,
            institution_query="Tsinghua University",
        )

        self.assertTrue(downloader._select_elsevier_tsinghua_shibauth_wayfless(page, result))
        self.assertIn("entityID=https%3A%2F%2Fidp.tsinghua.edu.cn%2Fidp%2Fshibboleth", page.goto_url)
        self.assertIn("appReturnURL=https%3A%2F%2Fwww.sciencedirect.com%2Fscience%2Farticle%2Fpii%2FS0929139326003835", page.goto_url)
        self.assertIn("elsevier-tsinghua-shibauth-wayfless", result.events[-1]["detail"])

    def test_world_scientific_profile_can_drive_institution_search(self):
        self.assertTrue(WORLD_SCIENTIFIC_PROFILE.institution_input_selectors)
        self.assertTrue(WORLD_SCIENTIFIC_PROFILE.institution_result_selectors)
        self.assertTrue(
            any("tsinghua" in selector.lower() for selector in WORLD_SCIENTIFIC_PROFILE.institution_result_selectors)
        )

    def test_world_scientific_picker_is_not_stalled_before_selection(self):
        class FakeBody:
            def inner_text(self, **_kwargs):
                return "Find your institution Type the name of your institution"

        class FakePage:
            url = "https://www.worldscientific.com/action/ssostart?redirectUri=%2F"

            def title(self):
                return "World Scientific"

            def locator(self, _selector):
                return FakeBody()

        cfg = Config(
            output_dir="out",
            cache_dir="cache",
            cookie_path="cookies.json",
            chrome_profile_dir="profile",
            carsi_cookie_dir="carsi",
        )
        downloader = PublisherBatchDownloader(cfg, profile=get_publisher_profile("world-scientific"))

        self.assertEqual(downloader._login_block_reason(FakePage()), "")

    def test_world_scientific_institution_banner_is_logged_in(self):
        class FakeBody:
            def inner_text(self, **_kwargs):
                return "brought to you by TSINGHUA UNIVERSITY CHINA Search My Cart Sign in Institutional Access"

        class FakePage:
            url = "https://www.worldscientific.com/"

            def title(self):
                return "World Scientific Publishing Co Pte Ltd"

            def locator(self, _selector):
                return FakeBody()

        cfg = Config(
            output_dir="out",
            cache_dir="cache",
            cookie_path="cookies.json",
            chrome_profile_dir="profile",
            carsi_cookie_dir="carsi",
        )
        downloader = PublisherBatchDownloader(cfg, profile=get_publisher_profile("world-scientific"))

        self.assertTrue(downloader._has_publisher_institution_session(FakePage()))
        self.assertFalse(downloader._looks_logged_out(FakePage()))

    def test_world_scientific_article_banner_overrides_nav_institutional_access(self):
        class FakeBody:
            def inner_text(self, **_kwargs):
                return (
                    "brought to you by TSINGHUA UNIVERSITY CHINA "
                    "Search My Cart Sign in Institutional Access Abstract Full Text References"
                )

        class FakePage:
            url = "https://www.worldscientific.com/doi/10.1142/s0218194026500348"

            def title(self):
                return "Dual Multi-RAG | World Scientific"

            def locator(self, _selector):
                return FakeBody()

        cfg = Config(
            output_dir="out",
            cache_dir="cache",
            cookie_path="cookies.json",
            chrome_profile_dir="profile",
            carsi_cookie_dir="carsi",
        )
        downloader = PublisherBatchDownloader(cfg, profile=get_publisher_profile("world-scientific"))

        self.assertFalse(downloader._looks_logged_out(FakePage()))

    def test_acs_profile_does_not_click_generic_institution_list_items(self):
        self.assertTrue(ACS_PROFILE.institution_result_selectors)
        self.assertNotIn("li", ACS_PROFILE.institution_result_selectors)
        self.assertTrue(
            any("tsinghua" in selector.lower() or "清华" in selector for selector in ACS_PROFILE.institution_result_selectors)
        )

    def test_institution_selection_requires_explicit_institution(self):
        class FakeLocator:
            def __init__(self, page, selector):
                self.page = page
                self.selector = selector
                self.first = self

            def is_visible(self, **_kwargs):
                return self.selector == "input" or "提交" in self.selector

            def fill(self, value, **_kwargs):
                self.page.filled.append((self.selector, value))

            def click(self, **_kwargs):
                self.page.clicked.append(self.selector)

        class FakePage:
            def __init__(self):
                self.filled = []
                self.clicked = []

            def locator(self, selector):
                return FakeLocator(self, selector)

        profile = PublisherProfile(
            name="Example",
            article_url_template="https://example.org/doi/{doi}",
            pdf_url_templates=(),
            success_url_markers=("example.org/doi/",),
            auth_url_markers=(),
            auth_title_markers=(),
            sso_text_markers=(),
            institution_input_selectors=("input",),
            institution_result_selectors=("button:has-text('Tsinghua')",),
        )
        cfg = Config(
            output_dir="out",
            cache_dir="cache",
            cookie_path="cookies.json",
            chrome_profile_dir="profile",
            carsi_cookie_dir="carsi",
        )
        downloader = PublisherBatchDownloader(cfg, profile=profile)
        result = DownloadResult(doi="10.0000/demo", status="failed")
        page = FakePage()

        self.assertFalse(downloader._select_institution(page, result))

        self.assertEqual(page.filled, [])
        self.assertEqual(page.clicked, [])
        self.assertEqual(result.events[-1]["state"], "institution_required")

    def test_challenge_wait_uses_login_timeout_budget(self):
        class FakeBody:
            def inner_text(self, **_kwargs):
                return "Are you a robot? Please confirm you are a human."

        class FakePage:
            def title(self):
                return "Please wait"

            def locator(self, _selector):
                return FakeBody()

        cfg = Config(
            output_dir="out",
            cache_dir="cache",
            cookie_path="cookies.json",
            chrome_profile_dir="profile",
            carsi_cookie_dir="carsi",
        )
        downloader = PublisherBatchDownloader(cfg, profile=ELSEVIER_PROFILE, login_timeout_sec=60)
        result = DownloadResult(doi="10.1016/j.watres.test", status="failed")

        with patch("instsci.publisher_batch.time.sleep") as sleep:
            downloader._wait_for_challenge(FakePage(), result)

        self.assertGreaterEqual(sleep.call_count, 10)
        self.assertEqual(result.events[-1]["state"], "challenge_wait")

    def test_challenge_wait_ignores_article_text_with_verified_word(self):
        class FakeBody:
            def inner_text(self, **_kwargs):
                return "Article text with verified methods, references, and supplementary information."

        class FakePage:
            def title(self):
                return "Water Research article - ScienceDirect"

            def locator(self, _selector):
                return FakeBody()

        cfg = Config(
            output_dir="out",
            cache_dir="cache",
            cookie_path="cookies.json",
            chrome_profile_dir="profile",
            carsi_cookie_dir="carsi",
        )
        downloader = PublisherBatchDownloader(cfg, profile=ELSEVIER_PROFILE, login_timeout_sec=60)
        result = DownloadResult(doi="10.1016/j.watres.test", status="failed")

        with patch("instsci.publisher_batch.time.sleep") as sleep:
            downloader._wait_for_challenge(FakePage(), result)

        self.assertFalse(sleep.called)
        self.assertNotIn("challenge_wait", [event["state"] for event in result.events])

    def test_challenge_wait_continues_after_manual_resolution(self):
        class FakeBody:
            def __init__(self, page):
                self.page = page

            def inner_text(self, **_kwargs):
                return self.page.body_text

        class FakePage:
            url = "https://www.worldscientific.com/action/ssostart?redirectUri=%2F"
            body_text = "www.worldscientific.com Ray ID: abc Cloudflare security verification"

            def title(self):
                return "Please wait"

            def locator(self, _selector):
                return FakeBody(self)

        cfg = Config(
            output_dir="out",
            cache_dir="cache",
            cookie_path="cookies.json",
            chrome_profile_dir="profile",
            carsi_cookie_dir="carsi",
        )
        downloader = PublisherBatchDownloader(cfg, profile=get_publisher_profile("world-scientific"), login_timeout_sec=20)
        result = DownloadResult(doi="10.1142/example", status="failed")
        page = FakePage()

        def resolve_after_first_wait(_seconds):
            page.url = "https://www.worldscientific.com/doi/10.1142/example"
            page.body_text = "Article abstract brought to you by TSINGHUA UNIVERSITY CHINA"

        with patch("instsci.publisher_batch.time.sleep", side_effect=resolve_after_first_wait):
            self.assertTrue(downloader._wait_for_challenge(page, result))

        states = [event["state"] for event in result.events]
        self.assertIn("challenge_manual_wait", states)
        self.assertIn("challenge_resolved", states)

    def test_complete_login_waits_for_cloudflare_manual_resolution(self):
        class FakeBody:
            def __init__(self, page):
                self.page = page

            def inner_text(self, **_kwargs):
                return self.page.body_text

        class FakePage:
            url = "https://www.worldscientific.com/action/ssostart?redirectUri=%2F"
            body_text = "www.worldscientific.com Ray ID: abc Cloudflare security verification"

            def title(self):
                return "Please wait" if "ssostart" in self.url else "World Scientific article"

            def locator(self, _selector):
                return FakeBody(self)

            def goto(self, url, **_kwargs):
                self.url = url
                self.body_text = "Article abstract brought to you by TSINGHUA UNIVERSITY CHINA"

        cfg = Config(
            output_dir="out",
            cache_dir="cache",
            cookie_path="cookies.json",
            chrome_profile_dir="profile",
            carsi_cookie_dir="carsi",
        )
        downloader = PublisherBatchDownloader(cfg, profile=get_publisher_profile("world-scientific"), login_timeout_sec=20)
        downloader._dismiss_cookie_banners = lambda _page, _result: False  # type: ignore[method-assign]
        downloader._click_sso_entry = lambda _page, _result: False  # type: ignore[method-assign]
        downloader._click_openathens_entry = lambda _page, _result: False  # type: ignore[method-assign]
        downloader._select_institution = lambda _page, _result: False  # type: ignore[method-assign]
        downloader._click_optional_continue = lambda _page, _result: None  # type: ignore[method-assign]
        result = DownloadResult(
            doi="10.1142/example",
            status="failed",
            article_url="https://www.worldscientific.com/doi/10.1142/example",
        )
        page = FakePage()

        def resolve_challenge(_page, _result, **_kwargs):
            page.url = "https://www.worldscientific.com/"
            page.body_text = "brought to you by TSINGHUA UNIVERSITY CHINA"
            return True

        downloader._wait_for_challenge = resolve_challenge  # type: ignore[method-assign]
        with patch("instsci.publisher_batch.time.sleep", return_value=None):
            self.assertTrue(downloader._complete_login_from_current_page(page, result))

        states = [event["state"] for event in result.events]
        self.assertIn("institution_session_return_article", states)

    def test_sso_click_ignores_navigation_context_race(self):
        class FakePage:
            def evaluate(self, *_args):
                raise RuntimeError("Page.evaluate: Execution context was destroyed")

        cfg = Config(
            output_dir="out",
            cache_dir="cache",
            cookie_path="cookies.json",
            chrome_profile_dir="profile",
            carsi_cookie_dir="carsi",
        )
        downloader = PublisherBatchDownloader(cfg, profile=ELSEVIER_PROFILE)

        downloader._click_sso_entry(FakePage())

    def test_iop_access_through_institution_button_is_clicked(self):
        class FakePage:
            def evaluate(self, _script, _markers):
                return {"text": "access through your institution", "href": ""}

        cfg = Config(
            output_dir="out",
            cache_dir="cache",
            cookie_path="cookies.json",
            chrome_profile_dir="profile",
            carsi_cookie_dir="carsi",
        )
        downloader = PublisherBatchDownloader(cfg, profile=get_publisher_profile("iop"))
        result = DownloadResult(doi="10.1088/example", status="failed")

        self.assertTrue(downloader._click_sso_entry(FakePage(), result))
        self.assertEqual(result.events[-1]["state"], "sso_entry_clicked")
        self.assertIn("access through your institution", result.events[-1]["detail"])

    def test_visible_pdf_button_is_clicked_before_url_fallbacks(self):
        class FakePage:
            def evaluate(self, *_args):
                return {"text": "PDF PDF You do not have access to this PDF", "href": "javascript:void()"}

        cfg = Config(
            output_dir="out",
            cache_dir="cache",
            cookie_path="cookies.json",
            chrome_profile_dir="profile",
            carsi_cookie_dir="carsi",
        )
        downloader = PublisherBatchDownloader(cfg, profile=get_publisher_profile("ieee"))
        result = DownloadResult(doi="10.1109/example", status="failed")

        self.assertTrue(downloader._click_pdf_entry(FakePage(), result))
        self.assertEqual(result.events[-1]["state"], "pdf_button_clicked")

    def test_cookie_banner_dismissal_records_safe_click(self):
        class FakePage:
            def evaluate(self, *_args):
                return "accept cookies"

        cfg = Config(
            output_dir="out",
            cache_dir="cache",
            cookie_path="cookies.json",
            chrome_profile_dir="profile",
            carsi_cookie_dir="carsi",
        )
        downloader = PublisherBatchDownloader(cfg, profile=get_publisher_profile("world-scientific"))
        result = DownloadResult(doi="10.1142/example", status="failed")

        self.assertTrue(downloader._dismiss_cookie_banners(FakePage(), result))
        self.assertEqual(result.events[-1]["state"], "cookie_banner_dismissed")

    def test_optional_continue_clicks_yes_confirmation(self):
        class FakeLocator:
            def __init__(self, page, selector):
                self.page = page
                self.selector = selector
                self.first = self

            def is_visible(self, **_kwargs):
                return self.selector == "button:has-text('Yes')"

            def click(self, **_kwargs):
                self.page.clicked.append(self.selector)

        class FakePage:
            def __init__(self):
                self.clicked = []

            def locator(self, selector):
                return FakeLocator(self, selector)

        cfg = Config(
            output_dir="out",
            cache_dir="cache",
            cookie_path="cookies.json",
            chrome_profile_dir="profile",
            carsi_cookie_dir="carsi",
        )
        downloader = PublisherBatchDownloader(cfg, profile=get_publisher_profile("world-scientific"))
        result = DownloadResult(doi="10.1142/example", status="failed")
        page = FakePage()

        downloader._click_optional_continue(page, result)

        self.assertEqual(page.clicked, ["button:has-text('Yes')"])
        self.assertEqual(result.events[-1]["state"], "institution_continue")

    def test_optional_continue_does_not_click_continue_reading(self):
        class FakeLocator:
            def __init__(self, page, selector):
                self.page = page
                self.selector = selector
                self.first = self

            def is_visible(self, **_kwargs):
                return self.selector == "button:has-text('Continue')"

            def inner_text(self, **_kwargs):
                return "Sign in to Continue Reading"

            def click(self, **_kwargs):
                self.page.clicked.append(self.selector)

        class FakePage:
            def __init__(self):
                self.clicked = []

            def locator(self, selector):
                return FakeLocator(self, selector)

        cfg = Config(
            output_dir="out",
            cache_dir="cache",
            cookie_path="cookies.json",
            chrome_profile_dir="profile",
            carsi_cookie_dir="carsi",
        )
        downloader = PublisherBatchDownloader(cfg, profile=get_publisher_profile("ieee"))
        result = DownloadResult(doi="10.1109/example", status="failed")
        page = FakePage()

        downloader._click_optional_continue(page, result)

        self.assertEqual(page.clicked, [])
        self.assertEqual(result.events, [])

    def test_iop_access_wall_uses_signin_deeplink(self):
        class FakeBody:
            def inner_text(self, **_kwargs):
                return "Access this article Login Access through your institution"

        class FakePage:
            url = "https://iopscience.iop.org/article/10.1088/1361-648x/ae72dd"

            def __init__(self):
                self.goto_url = ""

            def locator(self, _selector):
                return FakeBody()

            def goto(self, url, **_kwargs):
                self.goto_url = url
                self.url = url

        cfg = Config(
            output_dir="out",
            cache_dir="cache",
            cookie_path="cookies.json",
            chrome_profile_dir="profile",
            carsi_cookie_dir="carsi",
        )
        page = FakePage()
        downloader = PublisherBatchDownloader(cfg, profile=get_publisher_profile("iop"))
        result = DownloadResult(doi="10.1088/1361-648x/ae72dd", status="failed")

        self.assertTrue(downloader._click_iop_access_wall(page, result))
        self.assertIn("https://myiopscience.iop.org/signin?", page.goto_url)
        self.assertIn("origin=deeplink", page.goto_url)
        self.assertIn("target=https%3A%2F%2Fiopscience.iop.org%2Farticle%2F10.1088%2F1361-648x%2Fae72dd", page.goto_url)

    def test_sso_entry_script_guards_matched_condition(self):
        class FakePage:
            def evaluate(self, script, _markers):
                self.script = script
                return {"text": "access through your institution", "href": ""}

        cfg = Config(
            output_dir="out",
            cache_dir="cache",
            cookie_path="cookies.json",
            chrome_profile_dir="profile",
            carsi_cookie_dir="carsi",
        )
        page = FakePage()
        downloader = PublisherBatchDownloader(cfg, profile=get_publisher_profile("iop"))
        result = DownloadResult(doi="10.1088/example", status="failed")

        self.assertTrue(downloader._click_sso_entry(page, result))
        self.assertIn("if (matched)", page.script)
        self.assertNotIn("href.includes('ssostart')) {", page.script)

    def test_aps_provider_credentials_link_is_sso_entry(self):
        class FakePage:
            url = "https://journals.aps.org/prl/abstract/10.1103/example"
            goto_url = ""

            def evaluate(self, script, markers=None):
                self.script = script
                self.markers = markers
                return {
                    "text": "log in with username/password provided by your institution",
                    "href": "https://journals.aps.org/login_inst_user?rt=example",
                }

            def goto(self, url, **_kwargs):
                self.goto_url = url

        cfg = Config(
            output_dir="out",
            cache_dir="cache",
            cookie_path="cookies.json",
            chrome_profile_dir="profile",
            carsi_cookie_dir="carsi",
        )
        page = FakePage()
        downloader = PublisherBatchDownloader(cfg, profile=get_publisher_profile("aps"))
        result = DownloadResult(doi="10.1103/example", status="failed")

        self.assertTrue(downloader._click_sso_entry(page, result))
        self.assertIn("login_inst_user", page.script)
        self.assertIn("login_inst_user", page.goto_url)
        self.assertEqual(result.events[-1]["state"], "sso_entry_clicked")

    def test_aps_sso_does_not_fall_back_to_generic_clicks(self):
        class FakePage:
            url = "https://journals.aps.org/prl/abstract/10.1103/example"

            def evaluate(self, _script):
                return None

            def locator(self, _selector):
                raise AssertionError("APS must not use generic SSO selectors")

        cfg = Config(
            output_dir="out",
            cache_dir="cache",
            cookie_path="cookies.json",
            chrome_profile_dir="profile",
            carsi_cookie_dir="carsi",
        )
        downloader = PublisherBatchDownloader(cfg, profile=get_publisher_profile("aps"))
        result = DownloadResult(doi="10.1103/example", status="failed")

        self.assertFalse(downloader._click_sso_entry(FakePage(), result))
        self.assertEqual(result.events[-1]["state"], "aps_institution_entry_missing")

    def test_cookie_dismiss_does_not_match_accepted_by_accept_substring(self):
        class FakePage:
            def evaluate(self, script):
                self.script = script
                return None

        cfg = Config(
            output_dir="out",
            cache_dir="cache",
            cookie_path="cookies.json",
            chrome_profile_dir="profile",
            carsi_cookie_dir="carsi",
        )
        page = FakePage()
        downloader = PublisherBatchDownloader(cfg, profile=get_publisher_profile("aps"))

        self.assertFalse(downloader._dismiss_cookie_banners(page))
        self.assertIn("exactOnly = ['accept', 'close']", page.script)
        self.assertIn("!exactOnly.includes(pattern)", page.script)

    def test_annual_reviews_shibboleth_page_switches_to_openathens(self):
        class FakePage:
            url = "https://www.annualreviews.org/session/ext/shib?url=%2Fcontent%2Fjournals%2F10.1146%2Fexample"

            def __init__(self):
                self.goto_url = ""

            def goto(self, url, **_kwargs):
                self.goto_url = url
                self.url = url

        cfg = Config(
            output_dir="out",
            cache_dir="cache",
            cookie_path="cookies.json",
            chrome_profile_dir="profile",
            carsi_cookie_dir="carsi",
        )
        downloader = PublisherBatchDownloader(
            cfg,
            profile=get_publisher_profile("annual-reviews"),
            institution_query="Tsinghua",
        )
        result = DownloadResult(doi="10.1146/example", status="failed")
        page = FakePage()

        self.assertTrue(downloader._click_openathens_entry(page, result))
        self.assertIn("/session/ext/athens?", page.goto_url)
        self.assertIn("athensWayfSearch=Tsinghua", page.goto_url)
        self.assertIn("openathens_entry", [event["state"] for event in result.events])

    def test_annual_reviews_openathens_page_selects_tsinghua(self):
        class FakePage:
            url = "https://www.annualreviews.org/session/ext/athens?url=%2Fcontent%2Fjournals%2F10.1146%2Fexample&athensWayfSearch=Tsinghua"

            def evaluate(self, _script, query):
                return {"action": "openathens_go", "result": f"{query} University (OpenAthens)"}

        cfg = Config(
            output_dir="out",
            cache_dir="cache",
            cookie_path="cookies.json",
            chrome_profile_dir="profile",
            carsi_cookie_dir="carsi",
        )
        downloader = PublisherBatchDownloader(
            cfg,
            profile=get_publisher_profile("annual-reviews"),
            institution_query="Tsinghua",
        )
        result = DownloadResult(doi="10.1146/example", status="failed")

        self.assertTrue(downloader._select_annual_reviews_openathens(FakePage(), result))
        self.assertEqual(result.events[-1]["state"], "institution_selected")
        self.assertIn("openathens_go", result.events[-1]["detail"])

    def test_openathens_wayfinder_is_not_clicked_as_openathens_entry(self):
        class FakePage:
            url = "https://wayfinder.openathens.net/?return=https%3A%2F%2Fconnect.openathens.net%2Foidc%2Fauth"

            def evaluate(self, *_args):
                raise AssertionError("should not click generic OpenAthens links on wayfinder")

        cfg = Config(
            output_dir="out",
            cache_dir="cache",
            cookie_path="cookies.json",
            chrome_profile_dir="profile",
            carsi_cookie_dir="carsi",
        )
        downloader = PublisherBatchDownloader(cfg, profile=get_publisher_profile("iop"))
        result = DownloadResult(doi="10.1088/example", status="failed")

        self.assertFalse(downloader._click_openathens_entry(FakePage(), result))
        self.assertEqual(result.events, [])

    def test_openathens_wayfinder_selects_tsinghua_entity(self):
        class FakePage:
            url = "https://wayfinder.openathens.net/?return=https%3A%2F%2Fconnect.openathens.net%2Fsaml%2F2%2Fauth%3Fr%3Dhttps%253A%252F%252Fconnect.openathens.net%252Foidc%252Fauth"

            def __init__(self):
                self.goto_url = ""

            def goto(self, url, **_kwargs):
                self.goto_url = url
                self.url = url

        cfg = Config(
            output_dir="out",
            cache_dir="cache",
            cookie_path="cookies.json",
            chrome_profile_dir="profile",
            carsi_cookie_dir="carsi",
        )
        page = FakePage()
        downloader = PublisherBatchDownloader(
            cfg,
            profile=get_publisher_profile("iop"),
            institution_query="Tsinghua",
        )
        result = DownloadResult(doi="10.1088/example", status="failed")

        self.assertTrue(downloader._select_openathens_wayfinder(page, result))
        self.assertIn("https://connect.openathens.net/saml/2/auth", page.goto_url)
        self.assertIn("entityID=https%3A%2F%2Fidp.tsinghua.edu.cn%2Fidp%2Fshibboleth", page.goto_url)
        self.assertEqual(result.events[-1]["state"], "institution_selected")

    def test_openathens_wayfinder_does_not_use_tsinghua_entity_for_other_institution(self):
        class FakePage:
            url = "https://wayfinder.openathens.net/?return=https%3A%2F%2Fconnect.openathens.net%2Fsaml%2F2%2Fauth%3Fr%3Dhttps%253A%252F%252Fconnect.openathens.net%252Foidc%252Fauth"

            def goto(self, *_args, **_kwargs):
                raise AssertionError("should not navigate to Tsinghua WAYFless entity")

        cfg = Config(
            output_dir="out",
            cache_dir="cache",
            cookie_path="cookies.json",
            chrome_profile_dir="profile",
            carsi_cookie_dir="carsi",
        )
        downloader = PublisherBatchDownloader(
            cfg,
            profile=get_publisher_profile("iop"),
            institution_query="Example University",
        )
        result = DownloadResult(doi="10.1088/example", status="failed")

        self.assertFalse(downloader._select_openathens_wayfinder(FakePage(), result))
        self.assertEqual(result.events, [])

    def test_select_institution_does_not_fill_tsinghua_login_page(self):
        class FakePage:
            url = "https://id.tsinghua.edu.cn/do/off/ui/auth/login/form/example/1"

            def locator(self, _selector):
                raise AssertionError("should not locate inputs on Tsinghua login page")

        cfg = Config(
            output_dir="out",
            cache_dir="cache",
            cookie_path="cookies.json",
            chrome_profile_dir="profile",
            carsi_cookie_dir="carsi",
        )
        downloader = PublisherBatchDownloader(cfg, profile=get_publisher_profile("iop"))
        result = DownloadResult(doi="10.1088/example", status="failed")

        downloader._select_institution(FakePage(), result)
        self.assertEqual(result.events, [])

    def test_sso_entry_does_not_click_tsinghua_login_page(self):
        class FakePage:
            url = "https://id.tsinghua.edu.cn/do/off/ui/auth/login/form/example/1"

            def evaluate(self, *_args, **_kwargs):
                raise AssertionError("should not evaluate or click on human login page")

            def locator(self, _selector):
                raise AssertionError("should not locate controls on human login page")

        cfg = Config(
            output_dir="out",
            cache_dir="cache",
            cookie_path="cookies.json",
            chrome_profile_dir="profile",
            carsi_cookie_dir="carsi",
        )
        downloader = PublisherBatchDownloader(cfg, profile=ELSEVIER_PROFILE)
        result = DownloadResult(doi="10.1016/example", status="failed")

        self.assertTrue(downloader._is_human_login_page(FakePage()))
        self.assertFalse(downloader._click_sso_entry(FakePage(), result))
        self.assertEqual(result.events, [])

    def test_fetch_one_rechecks_async_auth_wall_after_login(self):
        class FakePage:
            url = "https://journals.aps.org/prl/abstract/10.1103/PhysRevLett.128.161102"

            def title(self):
                return "Constraints on the Maximum Densities of Neutron Stars | Phys. Rev. Lett."

            def locator(self, _selector):
                class FakeBody:
                    def inner_text(self, **_kwargs):
                        return "Abstract PDF Authorization Required Log in via your institution"

                return FakeBody()

            def close(self):
                return None

        class FakeContext:
            def new_page(self):
                return FakePage()

        cfg = Config(
            output_dir="out",
            cache_dir="cache",
            cookie_path="cookies.json",
            chrome_profile_dir="profile",
            carsi_cookie_dir="carsi",
        )
        downloader = PublisherBatchDownloader(cfg, profile=get_publisher_profile("aps"))
        downloader._ensure_login = lambda _page, _result: True  # type: ignore[method-assign]
        downloader._complete_login_from_current_page = lambda _page, _result: False  # type: ignore[method-assign]
        record = PaperRecord(doi="10.1103/PhysRevLett.128.161102")

        with TemporaryDirectory() as tmp:
            result = downloader.fetch_one(FakeContext(), record, Path(tmp))

        self.assertEqual(result.reason, "sso_required")
        self.assertEqual(result.state, "sso_required")
        self.assertIn("auth_wall_after_article_load", [event["state"] for event in result.events])

    def test_fetch_one_rechecks_auth_wall_after_pdf_attempt(self):
        class FakePage:
            url = "https://www.sciencedirect.com/science/article/pii/S0043135424004093"

            def title(self):
                if "id.elsevier.com" in self.url:
                    return "Loading https://id.elsevier.com/as/authorization.oauth2"
                return "Water Research article - ScienceDirect"

            def locator(self, _selector):
                class FakeBody:
                    def inner_text(self, **_kwargs):
                        return "Water Research article abstract"

                return FakeBody()

            def close(self):
                return None

        class FakeContext:
            def __init__(self):
                self.page = FakePage()

            def new_page(self):
                return self.page

        cfg = Config(
            output_dir="out",
            cache_dir="cache",
            cookie_path="cookies.json",
            chrome_profile_dir="profile",
            carsi_cookie_dir="carsi",
        )
        context = FakeContext()
        downloader = PublisherBatchDownloader(cfg, profile=ELSEVIER_PROFILE)
        downloader._ensure_login = lambda _page, _result: True  # type: ignore[method-assign]

        def fake_capture(_page, _doi, _result):
            context.page.url = "https://id.elsevier.com/as/authorization.oauth2"
            return None, ""

        downloader._capture_pdf = fake_capture  # type: ignore[method-assign]
        downloader._complete_login_from_current_page = lambda _page, _result: False  # type: ignore[method-assign]
        record = PaperRecord(doi="10.1016/j.watres.2024.121507")

        with TemporaryDirectory() as tmp:
            result = downloader.fetch_one(context, record, Path(tmp))

        self.assertEqual(result.reason, "sso_required")
        self.assertEqual(result.state, "sso_required")
        self.assertIn("auth_wall_after_pdf_attempt", [event["state"] for event in result.events])

    def test_fetch_one_continues_when_elsevier_login_finishes_at_timeout(self):
        class FakeBody:
            def inner_text(self, **_kwargs):
                return "Brought to you by:Tsinghua University Full text access View PDF"

        class FakePage:
            url = "https://www.sciencedirect.com/science/article/pii/S0043135424004093"

            def title(self):
                return "Water Research article - ScienceDirect"

            def locator(self, _selector):
                return FakeBody()

            def close(self):
                return None

        class FakeContext:
            def __init__(self):
                self.page = FakePage()

            def new_page(self):
                return self.page

        cfg = Config(
            output_dir="out",
            cache_dir="cache",
            cookie_path="cookies.json",
            chrome_profile_dir="profile",
            carsi_cookie_dir="carsi",
        )
        downloader = PublisherBatchDownloader(cfg, profile=ELSEVIER_PROFILE)
        downloader._ensure_login = lambda _page, _result: False  # type: ignore[method-assign]
        downloader._capture_pdf = lambda _page, _doi, _result: (  # type: ignore[method-assign]
            b"%PDF-" + b"0" * MIN_PDF_BYTES,
            "https://www.sciencedirect.com/science/article/pii/S0043135424004093/pdfft",
        )
        record = PaperRecord(doi="10.1016/j.watres.2024.121507")

        with TemporaryDirectory() as tmp:
            with patch(
                "instsci.publisher_batch.pdf_extractor.extract_from_bytes",
                return_value="DOI 10.1016/j.watres.2024.121507",
            ):
                result = downloader.fetch_one(FakeContext(), record, Path(tmp))

        self.assertEqual(result.status, "success")
        self.assertIn("login_completed_after_timeout", [event["state"] for event in result.events])

    def test_fetch_one_retries_pdf_after_auth_wall_login(self):
        class FakeBody:
            def __init__(self, page):
                self.page = page

            def inner_text(self, **_kwargs):
                return self.page.body_text

        class FakePage:
            url = "https://journals.aps.org/prl/abstract/10.1103/PhysRevLett.128.161102"
            body_text = "Article abstract"

            def title(self):
                return "Constraints on the Maximum Densities of Neutron Stars | Phys. Rev. Lett."

            def locator(self, _selector):
                return FakeBody(self)

            def close(self):
                return None

        class FakeContext:
            def __init__(self):
                self.page = FakePage()

            def new_page(self):
                return self.page

        cfg = Config(
            output_dir="out",
            cache_dir="cache",
            cookie_path="cookies.json",
            chrome_profile_dir="profile",
            carsi_cookie_dir="carsi",
        )
        context = FakeContext()
        downloader = PublisherBatchDownloader(cfg, profile=get_publisher_profile("aps"))
        downloader._ensure_login = lambda _page, _result: True  # type: ignore[method-assign]
        captures = []

        def fake_capture(page, _doi, _result):
            captures.append(page.url)
            if len(captures) == 1:
                page.body_text = "PDF Authorization Required Log in with username/password provided by your institution"
                return None, ""
            return b"%PDF-" + b"0" * MIN_PDF_BYTES, "https://journals.aps.org/prl/pdf/10.1103/PhysRevLett.128.161102"

        def fake_complete_login(page, _result):
            page.body_text = "Article abstract PDF"
            return True

        downloader._capture_pdf = fake_capture  # type: ignore[method-assign]
        downloader._complete_login_from_current_page = fake_complete_login  # type: ignore[method-assign]
        record = PaperRecord(doi="10.1103/PhysRevLett.128.161102")

        with TemporaryDirectory() as tmp:
            with patch(
                "instsci.publisher_batch.pdf_extractor.extract_from_bytes",
                return_value="DOI 10.1103/PhysRevLett.128.161102",
            ):
                result = downloader.fetch_one(context, record, Path(tmp))

        self.assertEqual(result.status, "success")
        self.assertEqual(len(captures), 2)
        self.assertIn("auth_wall_after_pdf_attempt", [event["state"] for event in result.events])

    def test_fetch_one_reports_institution_not_registered_after_pdf_auth_login(self):
        class FakeBody:
            def __init__(self, page):
                self.page = page

            def inner_text(self, **_kwargs):
                return self.page.body_text

        class FakePage:
            url = "https://journals.aps.org/prl/abstract/10.1103/PhysRevLett.128.161102"
            body_text = "Article abstract"

            def title(self):
                return "APS article"

            def locator(self, _selector):
                return FakeBody(self)

            def close(self):
                return None

        class FakeContext:
            def __init__(self):
                self.page = FakePage()

            def new_page(self):
                return self.page

        cfg = Config(
            output_dir="out",
            cache_dir="cache",
            cookie_path="cookies.json",
            chrome_profile_dir="profile",
            carsi_cookie_dir="carsi",
        )
        context = FakeContext()
        downloader = PublisherBatchDownloader(cfg, profile=get_publisher_profile("aps"))
        downloader._ensure_login = lambda _page, _result: True  # type: ignore[method-assign]

        def fake_capture(page, _doi, _result):
            page.body_text = "PDF Authorization Required Log in with username/password provided by your institution"
            return None, ""

        def fake_complete_login(page, _result):
            page.url = "https://idp.tsinghua.edu.cn/idp/profile/SAML2/Redirect/SSO"
            page.body_text = (
                "Web Login Service - Unsupported Request "
                "The application you have accessed is not registered for use with this service."
            )
            return False

        downloader._capture_pdf = fake_capture  # type: ignore[method-assign]
        downloader._complete_login_from_current_page = fake_complete_login  # type: ignore[method-assign]

        with TemporaryDirectory() as tmp:
            result = downloader.fetch_one(context, PaperRecord(doi="10.1103/PhysRevLett.128.161102"), Path(tmp))

        self.assertEqual(result.reason, "institution_not_registered")
        self.assertEqual(result.state, "institution_not_registered")

    def test_text_match_uses_fallback_article_title_when_record_title_empty(self):
        cfg = Config(
            output_dir="out",
            cache_dir="cache",
            cookie_path="cookies.json",
            chrome_profile_dir="profile",
            carsi_cookie_dir="carsi",
        )
        downloader = PublisherBatchDownloader(cfg, profile=get_publisher_profile("annual-reviews"))
        record = PaperRecord(doi="10.1146/annurev-phyto-011325-012824", title="")
        text = "Annual Review of Phytopathology Extracellular Antagonists: Offense and Counter-Defense in the Apoplast"

        self.assertTrue(
            downloader._text_matches_record(
                text,
                record,
                fallback_title="Extracellular Antagonists: Offense and Counter-Defense in the Apoplast | Annual Reviews",
            )
        )

    def test_iop_article_pdf_with_accepted_for_publication_text_still_matches(self):
        cfg = Config(
            output_dir="out",
            cache_dir="cache",
            cookie_path="cookies.json",
            chrome_profile_dir="profile",
            carsi_cookie_dir="carsi",
        )
        downloader = PublisherBatchDownloader(cfg, profile=get_publisher_profile("iop"))
        record = PaperRecord(doi="10.1088/1361-648x/ae72dd", title="")
        text = (
            "Journal of Physics: Condensed Matter PAPER A route to fully-compensated ferrimagnetic metal "
            "accepted for publication DOI 10.1088/1361-648x/ae72dd"
        )

        self.assertTrue(downloader._text_matches_record(text, record))

    def test_generic_batch_downloader_keeps_acs_compatibility(self):
        cfg = Config(
            output_dir="out",
            cache_dir="cache",
            cookie_path="cookies.json",
            chrome_profile_dir="profile",
            carsi_cookie_dir="carsi",
        )

        downloader = ACSCloakBatchDownloader(cfg)

        self.assertIsInstance(downloader, PublisherBatchDownloader)
        self.assertIs(downloader.profile, ACS_PROFILE)

    def test_pdf_candidates_are_driven_by_profile_rules(self):
        class FakePage:
            def evaluate(self, *_args):
                return [
                    "https://example.org/article/suppl_file/demo.pdf",
                    "https://example.org/download/primary.pdf",
                    "https://example.org/ignore/html",
                ]

        profile = PublisherProfile(
            name="Example",
            article_url_template="https://example.org/doi/{doi}",
            pdf_url_templates=("https://example.org/pdf/{doi}",),
            success_url_markers=("example.org/doi/",),
            auth_url_markers=(),
            auth_title_markers=(),
            sso_text_markers=(),
            pdf_url_markers=("/pdf/", "/download/"),
            supplementary_url_markers=("suppl_file",),
        )
        cfg = Config(
            output_dir="out",
            cache_dir="cache",
            cookie_path="cookies.json",
            chrome_profile_dir="profile",
            carsi_cookie_dir="carsi",
        )
        downloader = PublisherBatchDownloader(cfg, profile=profile)

        candidates = downloader._pdf_candidates(FakePage(), "10.0000/demo")

        self.assertEqual(candidates[0], "https://example.org/pdf/10.0000/demo")
        self.assertIn("https://example.org/download/primary.pdf", candidates)
        self.assertNotIn("https://example.org/article/suppl_file/demo.pdf", candidates)
        self.assertNotIn("https://example.org/ignore/html", candidates)

    def test_elsevier_pdf_candidates_are_limited_to_current_article(self):
        class FakePage:
            url = "https://www.sciencedirect.com/science/article/pii/S0043135426003957"

            def evaluate(self, *_args):
                return [
                    "https://www.sciencedirect.com/science/article/pii/S0043135426003957/pdfft",
                    "https://www.sciencedirect.com/science/article/pii/S0013935123006151/pdfft",
                    "https://www.sciencedirect.com/science/article/pii/S0043135426003957/pdfft?md5=abc",
                ]

        cfg = Config(
            output_dir="out",
            cache_dir="cache",
            cookie_path="cookies.json",
            chrome_profile_dir="profile",
            carsi_cookie_dir="carsi",
        )
        downloader = PublisherBatchDownloader(cfg, profile=ELSEVIER_PROFILE)

        candidates = downloader._pdf_candidates(FakePage(), "10.1016/j.watres.2026.125713")

        self.assertIn("https://www.sciencedirect.com/science/article/pii/S0043135426003957/pdfft", candidates)
        self.assertNotIn("https://www.sciencedirect.com/science/article/pii/S0013935123006151/pdfft", candidates)

    def test_sciencedirect_signed_asset_url_is_pdf_candidate(self):
        cfg = Config(
            output_dir="out",
            cache_dir="cache",
            cookie_path="cookies.json",
            chrome_profile_dir="profile",
            carsi_cookie_dir="carsi",
        )
        downloader = PublisherBatchDownloader(cfg, profile=ELSEVIER_PROFILE)

        self.assertTrue(
            downloader._is_pdf_candidate_url(
                "https://pdf.sciencedirectassets.com/271768/1-s2.0-S0043135426X20036/"
                "1-s2.0-S0043135426003957/main.pdf?X-Amz-Signature=abc"
            )
        )

    def test_elsevier_mmc_links_are_not_main_pdf_candidates(self):
        class FakePage:
            url = "https://www.sciencedirect.com/science/article/pii/S0043135426007505"

            def evaluate(self, *_args):
                return [
                    "https://ars.els-cdn.com/content/image/1-s2.0-S0043135426007505-mmc2.pdf",
                    "https://www.sciencedirect.com/science/article/pii/S0043135426007505/pdfft",
                ]

        cfg = Config(
            output_dir="out",
            cache_dir="cache",
            cookie_path="cookies.json",
            chrome_profile_dir="profile",
            carsi_cookie_dir="carsi",
        )
        downloader = PublisherBatchDownloader(cfg, profile=ELSEVIER_PROFILE)

        candidates = downloader._pdf_candidates(FakePage(), "10.1016/j.watres.2026.126069")

        self.assertIn("https://www.sciencedirect.com/science/article/pii/S0043135426007505/pdfft", candidates)
        self.assertNotIn(
            "https://ars.els-cdn.com/content/image/1-s2.0-S0043135426007505-mmc2.pdf",
            candidates,
        )

    def test_elsevier_pii_extraction_handles_signed_assets(self):
        self.assertEqual(
            PublisherBatchDownloader._extract_elsevier_pii(
                "https://pdf.sciencedirectassets.com/x/1-s2.0-S0043135426003957/main.pdf?pii=S0043135426003957"
            ),
            "S0043135426003957",
        )

    def test_rsc_suppdata_links_are_not_main_pdf_candidates(self):
        class FakePage:
            def evaluate(self, *_args):
                return [
                    "https://www.rsc.org/suppdata/d5/cc/d5cc06607g/d5cc06607g1.pdf",
                    "https://pubs.rsc.org/en/content/articlepdf/2026/cc/d5cc06607g",
                ]

        cfg = Config(
            output_dir="out",
            cache_dir="cache",
            cookie_path="cookies.json",
            chrome_profile_dir="profile",
            carsi_cookie_dir="carsi",
        )
        downloader = PublisherBatchDownloader(cfg, profile=RSC_PROFILE)

        candidates = downloader._pdf_candidates(FakePage(), "10.1039/d5cc06607g")

        self.assertIn("https://pubs.rsc.org/en/content/articlepdf/2026/cc/d5cc06607g", candidates)
        self.assertNotIn("https://www.rsc.org/suppdata/d5/cc/d5cc06607g/d5cc06607g1.pdf", candidates)

    def test_wiley_marketing_asset_is_not_main_pdf_candidate(self):
        class FakePage:
            def evaluate(self, *_args):
                return [
                    "https://chemistry-europe.onlinelibrary.wiley.com/pb-assets/assets/vch/wechat-wiley-chem-1660202483563.pdf",
                    "https://chemistry-europe.onlinelibrary.wiley.com/doi/pdf/10.1002/cctc.70819",
                ]

        cfg = Config(
            output_dir="out",
            cache_dir="cache",
            cookie_path="cookies.json",
            chrome_profile_dir="profile",
            carsi_cookie_dir="carsi",
        )
        downloader = PublisherBatchDownloader(cfg, profile=WILEY_PROFILE)

        candidates = downloader._pdf_candidates(FakePage(), "10.1002/cctc.70819")

        self.assertIn("https://chemistry-europe.onlinelibrary.wiley.com/doi/pdf/10.1002/cctc.70819", candidates)
        self.assertNotIn(
            "https://chemistry-europe.onlinelibrary.wiley.com/pb-assets/assets/vch/wechat-wiley-chem-1660202483563.pdf",
            candidates,
        )

    def test_capture_pdf_falls_back_to_direct_url_fetch(self):
        class FakePage:
            url = "https://pubs.rsc.org/en/content/articlepdf/2026/nj/d5nj03688g"

            def __init__(self):
                self.listeners = {}
                self.goto_kwargs = []

            def on(self, event, callback):
                self.listeners[event] = callback

            def remove_listener(self, event, _callback):
                self.listeners.pop(event, None)

            def evaluate(self, *_args):
                return [self.url]

            def goto(self, url, **_kwargs):
                self.url = url
                self.goto_kwargs.append(_kwargs)
                return None

            def locator(self, _selector):
                raise RuntimeError("no body")

            def title(self):
                return ""

        cfg = Config(
            output_dir="out",
            cache_dir="cache",
            cookie_path="cookies.json",
            chrome_profile_dir="profile",
            carsi_cookie_dir="carsi",
        )
        downloader = PublisherBatchDownloader(cfg, profile=RSC_PROFILE)
        payload = b"%PDF-" + (b"x" * MIN_PDF_BYTES)

        def fake_fetch(url):
            if "/2026/nj/" in url:
                return payload, url
            return None, url

        downloader._fetch_pdf_url = fake_fetch  # type: ignore[method-assign]
        result = DownloadResult(doi="10.1039/d5nj03688g", status="failed")

        pdf_bytes, pdf_url = downloader._capture_pdf(FakePage(), result.doi, result)

        self.assertEqual(pdf_bytes, payload)
        self.assertEqual(pdf_url, "https://pubs.rsc.org/en/content/articlepdf/2026/nj/d5nj03688g")

    def test_capture_pdf_falls_back_to_sciencedirect_signed_asset_page_url(self):
        pdfft_url = "https://www.sciencedirect.com/science/article/pii/S0043135426003957/pdfft"
        signed_url = (
            "https://pdf.sciencedirectassets.com/271768/1-s2.0-S0043135426X20036/"
            "1-s2.0-S0043135426003957/main.pdf?X-Amz-Signature=abc"
        )

        class FakePage:
            url = "https://www.sciencedirect.com/science/article/pii/S0043135426003957"

            def __init__(self):
                self.listeners = {}

            def on(self, event, callback):
                self.listeners[event] = callback

            def remove_listener(self, event, _callback):
                self.listeners.pop(event, None)

            def evaluate(self, _script, arg=None):
                if isinstance(arg, dict):
                    return [pdfft_url]
                if arg == pdfft_url:
                    self.url = signed_url
                return []

            def goto(self, _url, **_kwargs):
                self.url = signed_url
                return None

            def locator(self, _selector):
                raise RuntimeError("no body")

            def title(self):
                return ""

        cfg = Config(
            output_dir="out",
            cache_dir="cache",
            cookie_path="cookies.json",
            chrome_profile_dir="profile",
            carsi_cookie_dir="carsi",
        )
        downloader = PublisherBatchDownloader(cfg, profile=ELSEVIER_PROFILE)
        payload = b"%PDF-" + (b"x" * MIN_PDF_BYTES)
        downloader._fetch_pdf_url = lambda url: (payload, url) if "sciencedirectassets" in url else (None, url)  # type: ignore[method-assign]
        result = DownloadResult(doi="10.1016/j.watres.2026.125713", status="failed")

        pdf_bytes, pdf_url = downloader._capture_pdf(FakePage(), result.doi, result)

        self.assertEqual(pdf_bytes, payload)
        self.assertEqual(pdf_url, signed_url)

    def test_capture_pdf_falls_back_to_sciencedirect_signed_asset_title(self):
        signed_url = (
            "https://pdf.sciencedirectassets.com/271768/1-s2.0-S0043135424X00068/"
            "1-s2.0-S0043135424004093/main.pdf?X-Amz-Signature=abc"
        )

        class FakePage:
            url = "https://www.sciencedirect.com/science/article/pii/S0043135424004093/pdfft"

            def __init__(self):
                self.listeners = {}

            def on(self, event, callback):
                self.listeners[event] = callback

            def remove_listener(self, event, _callback):
                self.listeners.pop(event, None)

            def evaluate(self, *_args):
                return [self.url]

            def goto(self, _url, **_kwargs):
                return None

            def locator(self, _selector):
                raise RuntimeError("no body")

            def title(self):
                return f"Loading {signed_url}"

        cfg = Config(
            output_dir="out",
            cache_dir="cache",
            cookie_path="cookies.json",
            chrome_profile_dir="profile",
            carsi_cookie_dir="carsi",
        )
        downloader = PublisherBatchDownloader(cfg, profile=ELSEVIER_PROFILE)
        payload = b"%PDF-" + (b"x" * MIN_PDF_BYTES)
        downloader._fetch_pdf_url = lambda url: (payload, url) if url == signed_url else (None, url)  # type: ignore[method-assign]
        result = DownloadResult(doi="10.1016/j.watres.2024.121507", status="failed")

        with patch("instsci.publisher_batch.time.sleep"):
            pdf_bytes, pdf_url = downloader._capture_pdf(FakePage(), result.doi, result)

        self.assertEqual(pdf_bytes, payload)
        self.assertEqual(pdf_url, signed_url)

    def test_capture_pdf_rechecks_page_url_after_viewer_wait(self):
        pdfft_url = "https://www.sciencedirect.com/science/article/pii/S0043135424004093/pdfft"
        signed_url = (
            "https://pdf.sciencedirectassets.com/271768/1-s2.0-S0043135424X00068/"
            "1-s2.0-S0043135424004093/main.pdf?X-Amz-Signature=abc"
        )

        class FakePage:
            url = "https://www.sciencedirect.com/science/article/pii/S0043135424004093"

            def __init__(self):
                self.listeners = {}

            def on(self, event, callback):
                self.listeners[event] = callback

            def remove_listener(self, event, _callback):
                self.listeners.pop(event, None)

            def evaluate(self, _script, arg=None):
                if isinstance(arg, dict):
                    return [pdfft_url]
                if arg == pdfft_url:
                    self.url = signed_url
                return []

            def goto(self, url, **_kwargs):
                self.url = url
                return None

            def locator(self, _selector):
                raise RuntimeError("no body")

            def title(self):
                return "请稍候..."

        cfg = Config(
            output_dir="out",
            cache_dir="cache",
            cookie_path="cookies.json",
            chrome_profile_dir="profile",
            carsi_cookie_dir="carsi",
        )
        downloader = PublisherBatchDownloader(cfg, profile=ELSEVIER_PROFILE)
        payload = b"%PDF-" + (b"x" * MIN_PDF_BYTES)
        downloader._fetch_pdf_url = lambda url: (payload, url) if url == signed_url else (None, url)  # type: ignore[method-assign]
        page = FakePage()
        downloader._wait_for_challenge = lambda page_arg, _result: setattr(page_arg, "url", signed_url)  # type: ignore[method-assign]
        result = DownloadResult(doi="10.1016/j.watres.2024.121507", status="failed")

        with patch("instsci.publisher_batch.time.sleep"):
            pdf_bytes, pdf_url = downloader._capture_pdf(page, result.doi, result)

        self.assertEqual(pdf_bytes, payload)
        self.assertEqual(pdf_url, signed_url)

    def test_elsevier_async_navigation_waits_for_signed_asset_challenge(self):
        pdfft_url = "https://www.sciencedirect.com/science/article/pii/S0043135424004093/pdfft"
        signed_url = (
            "https://pdf.sciencedirectassets.com/271768/1-s2.0-S0043135424X00068/"
            "1-s2.0-S0043135424004093/main.pdf?X-Amz-Signature=abc"
        )

        class FakeBody:
            def __init__(self, page):
                self.page = page

            def inner_text(self, **_kwargs):
                if self.page.challenge:
                    return "Are you a robot? Please confirm you are a human."
                return ""

        class FakePage:
            url = "https://www.sciencedirect.com/science/article/pii/S0043135424004093"

            def __init__(self):
                self.listeners = {}
                self.challenge = False

            def on(self, event, callback):
                self.listeners[event] = callback

            def remove_listener(self, event, _callback):
                self.listeners.pop(event, None)

            def evaluate(self, _script, arg=None):
                if isinstance(arg, dict):
                    return [pdfft_url]
                if arg == pdfft_url:
                    self.url = signed_url
                    self.challenge = True
                    return None
                return []

            def goto(self, *_args, **_kwargs):
                raise AssertionError("Elsevier pdfft should use async navigation")

            def locator(self, _selector):
                return FakeBody(self)

            def title(self):
                if self.challenge:
                    return "Are you a robot?"
                return ""

        cfg = Config(
            output_dir="out",
            cache_dir="cache",
            cookie_path="cookies.json",
            chrome_profile_dir="profile",
            carsi_cookie_dir="carsi",
        )
        downloader = PublisherBatchDownloader(cfg, profile=ELSEVIER_PROFILE)
        payload = b"%PDF-" + (b"x" * MIN_PDF_BYTES)

        def fake_fetch(url):
            if url == signed_url:
                return payload, url
            return None, url

        def resolve_challenge(page_arg, _result, **_kwargs):
            page_arg.challenge = False
            return True

        downloader._fetch_pdf_url = fake_fetch  # type: ignore[method-assign]
        downloader._wait_for_challenge = resolve_challenge  # type: ignore[method-assign]
        page = FakePage()
        result = DownloadResult(doi="10.1016/j.watres.2024.121507", status="failed")

        with patch("instsci.publisher_batch.time.sleep"):
            pdf_bytes, pdf_url = downloader._capture_pdf(page, result.doi, result)

        self.assertEqual(pdf_bytes, payload)
        self.assertEqual(pdf_url, signed_url)
        self.assertIn("pdf_state_candidate", [event["state"] for event in result.events])

    def test_elsevier_click_pdf_entry_prefers_visible_view_pdf(self):
        class FakePage:
            def __init__(self):
                self.clicked = False

            def evaluate(self, script):
                self.clicked = "elsevier-view-pdf" in script
                return {
                    "selector": "elsevier-view-pdf",
                    "text": "View PDF",
                    "href": "https://www.sciencedirect.com/science/article/pii/S0043135424004093/pdfft",
                    "score": 180,
                }

        cfg = Config(
            output_dir="out",
            cache_dir="cache",
            cookie_path="cookies.json",
            chrome_profile_dir="profile",
            carsi_cookie_dir="carsi",
        )
        downloader = PublisherBatchDownloader(cfg, profile=ELSEVIER_PROFILE)
        page = FakePage()
        result = DownloadResult(doi="10.1016/j.watres.2024.121507", status="failed")

        self.assertTrue(downloader._click_pdf_entry(page, result, doi=result.doi))
        self.assertTrue(page.clicked)
        self.assertIn("elsevier-view-pdf", result.events[-1]["detail"])

    def test_fetch_pdf_url_uses_browser_state_headers(self):
        signed_url = (
            "https://pdf.sciencedirectassets.com/271768/1-s2.0-S0043135424X00068/"
            "1-s2.0-S0043135424004093/main.pdf?X-Amz-Signature=abc"
        )

        class FakeContext:
            requested_url = ""

            def cookies(self, url):
                self.requested_url = url
                return [
                    {"name": "cf_clearance", "value": "clear"},
                    {"name": "sd_session", "value": "session"},
                ]

        class FakePage:
            url = "https://www.sciencedirect.com/science/article/pii/S0043135424004093/pdfft"

            def __init__(self):
                self.context = FakeContext()

            def evaluate(self, _script):
                return "Mozilla/5.0 BrowserProfile"

        class FakeResponse:
            content = b"%PDF-" + (b"x" * MIN_PDF_BYTES)
            url = signed_url

        cfg = Config(
            output_dir="out",
            cache_dir="cache",
            cookie_path="cookies.json",
            chrome_profile_dir="profile",
            carsi_cookie_dir="carsi",
        )
        downloader = PublisherBatchDownloader(cfg, profile=ELSEVIER_PROFILE)
        page = FakePage()

        with patch("instsci.publisher_batch.requests.get", return_value=FakeResponse()) as get:
            pdf_bytes, pdf_url = downloader._fetch_pdf_url(signed_url, page=page)

        self.assertEqual(pdf_bytes, FakeResponse.content)
        self.assertEqual(pdf_url, signed_url)
        self.assertEqual(page.context.requested_url, signed_url)
        headers = get.call_args.kwargs["headers"]
        self.assertEqual(headers["User-Agent"], "Mozilla/5.0 BrowserProfile")
        self.assertEqual(headers["Referer"], page.url)
        self.assertIn("cf_clearance=clear", headers["Cookie"])
        self.assertIn("sd_session=session", headers["Cookie"])

    def test_elsevier_capture_pdf_uses_async_navigation_for_pdfft(self):
        pdfft_url = "https://www.sciencedirect.com/science/article/pii/S0043135424004093/pdfft"
        signed_url = (
            "https://pdf.sciencedirectassets.com/271768/1-s2.0-S0043135424X00068/"
            "1-s2.0-S0043135424004093/main.pdf?X-Amz-Signature=abc"
        )

        class FakePage:
            url = "https://www.sciencedirect.com/science/article/pii/S0043135424004093"

            def __init__(self):
                self.listeners = {}
                self.goto_called = False

            def on(self, event, callback):
                self.listeners[event] = callback

            def remove_listener(self, event, _callback):
                self.listeners.pop(event, None)

            def evaluate(self, _script, arg=None):
                if isinstance(arg, dict):
                    return [pdfft_url]
                if arg == pdfft_url:
                    self.url = signed_url
                    return None
                return []

            def goto(self, *_args, **_kwargs):
                self.goto_called = True
                raise AssertionError("Elsevier pdfft should use async navigation")

            def locator(self, _selector):
                raise RuntimeError("no body")

            def title(self):
                return "请稍候..."

        cfg = Config(
            output_dir="out",
            cache_dir="cache",
            cookie_path="cookies.json",
            chrome_profile_dir="profile",
            carsi_cookie_dir="carsi",
        )
        downloader = PublisherBatchDownloader(cfg, profile=ELSEVIER_PROFILE)
        payload = b"%PDF-" + (b"x" * MIN_PDF_BYTES)
        downloader._fetch_pdf_url = lambda url: (payload, url) if url == signed_url else (None, url)  # type: ignore[method-assign]
        page = FakePage()
        result = DownloadResult(doi="10.1016/j.watres.2024.121507", status="failed")

        with patch("instsci.publisher_batch.time.sleep"):
            pdf_bytes, pdf_url = downloader._capture_pdf(page, result.doi, result)

        self.assertEqual(pdf_bytes, payload)
        self.assertEqual(pdf_url, signed_url)
        self.assertFalse(page.goto_called)

    def test_elsevier_capture_pdf_falls_back_to_pdf_viewer_download(self):
        with TemporaryDirectory() as tmp:
            pdfft_url = "https://www.sciencedirect.com/science/article/pii/S0043135424004093/pdfft"
            signed_url = (
                "https://pdf.sciencedirectassets.com/271768/1-s2.0-S0043135424X00068/"
                "1-s2.0-S0043135424004093/main.pdf?X-Amz-Signature=abc"
            )
            download_path = Path(tmp) / "viewer.pdf"
            payload = b"%PDF-" + (b"x" * MIN_PDF_BYTES)
            download_path.write_bytes(payload)

            class FakeDownload:
                url = signed_url

                def path(self):
                    return str(download_path)

            class FakeDownloadContext:
                value = FakeDownload()

                def __enter__(self):
                    return self

                def __exit__(self, *_args):
                    return False

            class FakePage:
                url = "https://www.sciencedirect.com/science/article/pii/S0043135424004093"

                def __init__(self):
                    self.listeners = {}
                    self.viewer_clicks = 0

                def on(self, event, callback):
                    self.listeners[event] = callback

                def remove_listener(self, event, _callback):
                    self.listeners.pop(event, None)

                def evaluate(self, script, arg=None):
                    if isinstance(arg, dict) and "pdf-viewer-download" in script:
                        if arg.get("click"):
                            self.viewer_clicks += 1
                        return {"selector": "pdf-viewer-download", "text": "Download"}
                    if isinstance(arg, dict):
                        return [pdfft_url]
                    if arg == pdfft_url:
                        self.url = signed_url
                        return None
                    if "elsevier-view-pdf" in script:
                        return {"selector": "elsevier-view-pdf", "text": "View PDF", "score": 180}
                    return []

                def expect_download(self, **_kwargs):
                    return FakeDownloadContext()

                def goto(self, *_args, **_kwargs):
                    raise AssertionError("Elsevier pdfft should use async navigation")

                def locator(self, _selector):
                    raise RuntimeError("no body")

                def title(self):
                    return ""

            cfg = Config(
                output_dir="out",
                cache_dir="cache",
                cookie_path="cookies.json",
                chrome_profile_dir="profile",
                carsi_cookie_dir="carsi",
            )
            downloader = PublisherBatchDownloader(cfg, profile=ELSEVIER_PROFILE)
            downloader._fetch_pdf_url = lambda url: (None, url)  # type: ignore[method-assign]
            page = FakePage()
            result = DownloadResult(doi="10.1016/j.watres.2024.121507", status="failed")

            with patch("instsci.publisher_batch.time.sleep"):
                pdf_bytes, pdf_url = downloader._capture_pdf(page, result.doi, result)

            self.assertEqual(pdf_bytes, payload)
            self.assertEqual(pdf_url, signed_url)
            self.assertEqual(page.viewer_clicks, 1)
            self.assertIn("pdf_viewer_download_captured", [event["state"] for event in result.events])

    def test_elsevier_capture_pdf_falls_back_to_pdf_viewer_toolbar_download(self):
        with TemporaryDirectory() as tmp:
            pdfft_url = "https://www.sciencedirect.com/science/article/pii/S0043135424004093/pdfft"
            signed_url = (
                "https://pdf.sciencedirectassets.com/271768/1-s2.0-S0043135424X00068/"
                "1-s2.0-S0043135424004093/main.pdf?X-Amz-Signature=abc"
            )
            download_path = Path(tmp) / "viewer.pdf"
            payload = b"%PDF-" + (b"x" * MIN_PDF_BYTES)
            download_path.write_bytes(payload)

            class FakeDownload:
                url = signed_url

                def path(self):
                    return str(download_path)

            class FakeDownloadContext:
                value = FakeDownload()

                def __enter__(self):
                    return self

                def __exit__(self, *_args):
                    return False

            class FakeMouse:
                def __init__(self):
                    self.clicks = []

                def click(self, x, y):
                    self.clicks.append((x, y))

            class FakePage:
                url = "https://www.sciencedirect.com/science/article/pii/S0043135424004093"

                def __init__(self):
                    self.listeners = {}
                    self.mouse = FakeMouse()

                def on(self, event, callback):
                    self.listeners[event] = callback

                def remove_listener(self, event, _callback):
                    self.listeners.pop(event, None)

                def evaluate(self, script, arg=None):
                    if isinstance(arg, dict) and "pdf-viewer-download" in script:
                        return None
                    if script.startswith("() => ({width:"):
                        return {"width": 1919, "height": 960}
                    if isinstance(arg, dict):
                        return [pdfft_url]
                    if arg == pdfft_url:
                        self.url = signed_url
                        return None
                    if "elsevier-view-pdf" in script:
                        return {"selector": "elsevier-view-pdf", "text": "View PDF", "score": 180}
                    return []

                def expect_download(self, **_kwargs):
                    return FakeDownloadContext()

                def goto(self, *_args, **_kwargs):
                    raise AssertionError("Elsevier pdfft should use async navigation")

                def locator(self, _selector):
                    raise RuntimeError("no body")

                def title(self):
                    return ""

            cfg = Config(
                output_dir="out",
                cache_dir="cache",
                cookie_path="cookies.json",
                chrome_profile_dir="profile",
                carsi_cookie_dir="carsi",
            )
            downloader = PublisherBatchDownloader(cfg, profile=ELSEVIER_PROFILE)
            downloader._fetch_pdf_url = lambda url: (None, url)  # type: ignore[method-assign]
            page = FakePage()
            result = DownloadResult(doi="10.1016/j.watres.2024.121507", status="failed")

            with patch("instsci.publisher_batch.time.sleep"):
                pdf_bytes, pdf_url = downloader._capture_pdf(page, result.doi, result)

            self.assertEqual(pdf_bytes, payload)
            self.assertEqual(pdf_url, signed_url)
            self.assertEqual(page.mouse.clicks, [(1817, 28)])
            self.assertIn("pdf_viewer_toolbar_download_captured", [event["state"] for event in result.events])

    def test_capture_pdf_defers_sciencedirect_asset_response_body(self):
        pdfft_url = "https://www.sciencedirect.com/science/article/pii/S0043135424004093/pdfft"
        signed_url = (
            "https://pdf.sciencedirectassets.com/271768/1-s2.0-S0043135424X00068/"
            "1-s2.0-S0043135424004093/main.pdf?X-Amz-Signature=abc"
        )

        class FakeScienceDirectResponse:
            url = signed_url
            headers = {"content-type": "application/pdf"}

            def body(self):
                raise AssertionError("ScienceDirect asset body should be fetched outside response callback")

        class FakePage:
            url = "https://www.sciencedirect.com/science/article/pii/S0043135424004093"

            def __init__(self):
                self.listeners = {}

            def on(self, event, callback):
                self.listeners[event] = callback

            def remove_listener(self, event, _callback):
                self.listeners.pop(event, None)

            def evaluate(self, _script, arg=None):
                if isinstance(arg, dict):
                    return [pdfft_url]
                if arg == pdfft_url:
                    self.url = signed_url
                return []

            def goto(self, _url, **_kwargs):
                self.url = signed_url
                self.listeners["response"](FakeScienceDirectResponse())
                return FakeScienceDirectResponse()

            def locator(self, _selector):
                raise RuntimeError("no body")

            def title(self):
                return "请稍候..."

        cfg = Config(
            output_dir="out",
            cache_dir="cache",
            cookie_path="cookies.json",
            chrome_profile_dir="profile",
            carsi_cookie_dir="carsi",
        )
        downloader = PublisherBatchDownloader(cfg, profile=ELSEVIER_PROFILE)
        payload = b"%PDF-" + (b"x" * MIN_PDF_BYTES)
        downloader._fetch_pdf_url = lambda url: (payload, url) if url == signed_url else (None, url)  # type: ignore[method-assign]
        result = DownloadResult(doi="10.1016/j.watres.2024.121507", status="failed")

        pdf_bytes, pdf_url = downloader._capture_pdf(FakePage(), result.doi, result)

        self.assertEqual(pdf_bytes, payload)
        self.assertEqual(pdf_url, signed_url)
        self.assertTrue(downloader._should_defer_response_body(signed_url))

    def test_capture_pdf_defers_elsevier_pdfft_response_body(self):
        pdfft_url = "https://www.sciencedirect.com/science/article/pii/S0043135424004093/pdfft"
        signed_url = (
            "https://pdf.sciencedirectassets.com/271768/1-s2.0-S0043135424X00068/"
            "1-s2.0-S0043135424004093/main.pdf?X-Amz-Signature=abc"
        )

        class FakePdfftResponse:
            url = pdfft_url
            headers = {"content-type": "text/html"}

            def body(self):
                raise AssertionError("Elsevier pdfft response body should be fetched outside response callback")

        class FakePage:
            url = "https://www.sciencedirect.com/science/article/pii/S0043135424004093"

            def __init__(self):
                self.listeners = {}

            def on(self, event, callback):
                self.listeners[event] = callback

            def remove_listener(self, event, _callback):
                self.listeners.pop(event, None)

            def evaluate(self, _script, arg=None):
                if isinstance(arg, dict):
                    return [pdfft_url]
                if arg == pdfft_url:
                    self.url = signed_url
                return []

            def goto(self, _url, **_kwargs):
                self.url = signed_url
                self.listeners["response"](FakePdfftResponse())
                return FakePdfftResponse()

            def locator(self, _selector):
                raise RuntimeError("no body")

            def title(self):
                return "请稍候..."

        cfg = Config(
            output_dir="out",
            cache_dir="cache",
            cookie_path="cookies.json",
            chrome_profile_dir="profile",
            carsi_cookie_dir="carsi",
        )
        downloader = PublisherBatchDownloader(cfg, profile=ELSEVIER_PROFILE)
        payload = b"%PDF-" + (b"x" * MIN_PDF_BYTES)
        downloader._fetch_pdf_url = lambda url: (payload, url) if url == signed_url else (None, url)  # type: ignore[method-assign]
        result = DownloadResult(doi="10.1016/j.watres.2024.121507", status="failed")

        pdf_bytes, pdf_url = downloader._capture_pdf(FakePage(), result.doi, result)

        self.assertEqual(pdf_bytes, payload)
        self.assertEqual(pdf_url, signed_url)
        self.assertTrue(downloader._should_defer_response_body(pdfft_url))

    def test_capture_pdf_tries_page_url_when_response_url_is_not_fetchable(self):
        pdfft_url = "https://www.sciencedirect.com/science/article/pii/S0043135426003957/pdfft"
        signed_url = (
            "https://pdf.sciencedirectassets.com/271768/1-s2.0-S0043135426X20036/"
            "1-s2.0-S0043135426003957/main.pdf?X-Amz-Signature=abc"
        )

        class FakeResponse:
            url = pdfft_url

            def body(self):
                return b"<html></html>"

        class FakePage:
            url = "https://www.sciencedirect.com/science/article/pii/S0043135426003957"

            def __init__(self):
                self.listeners = {}

            def on(self, event, callback):
                self.listeners[event] = callback

            def remove_listener(self, event, _callback):
                self.listeners.pop(event, None)

            def evaluate(self, _script, arg=None):
                if isinstance(arg, dict):
                    return [pdfft_url]
                if arg == pdfft_url:
                    self.url = signed_url
                return []

            def goto(self, _url, **_kwargs):
                self.url = signed_url
                return FakeResponse()

            def locator(self, _selector):
                raise RuntimeError("no body")

            def title(self):
                return ""

        cfg = Config(
            output_dir="out",
            cache_dir="cache",
            cookie_path="cookies.json",
            chrome_profile_dir="profile",
            carsi_cookie_dir="carsi",
        )
        downloader = PublisherBatchDownloader(cfg, profile=ELSEVIER_PROFILE)
        payload = b"%PDF-" + (b"x" * MIN_PDF_BYTES)
        downloader._fetch_pdf_url = lambda url: (payload, url) if url == signed_url else (None, url)  # type: ignore[method-assign]
        result = DownloadResult(doi="10.1016/j.watres.2026.125713", status="failed")

        with patch("instsci.publisher_batch.time.sleep"):
            pdf_bytes, pdf_url = downloader._capture_pdf(FakePage(), result.doi, result)

        self.assertEqual(pdf_bytes, payload)
        self.assertEqual(pdf_url, signed_url)

    def test_pdf_viewer_navigation_waits_only_for_commit(self):
        class FakePage:
            url = "https://advanced.onlinelibrary.wiley.com/doi/epdf/10.1002/adem.70982"

            def __init__(self):
                self.listeners = {}
                self.goto_kwargs = []

            def on(self, event, callback):
                self.listeners[event] = callback

            def remove_listener(self, event, _callback):
                self.listeners.pop(event, None)

            def evaluate(self, *_args):
                return [self.url]

            def goto(self, url, **kwargs):
                self.url = url
                self.goto_kwargs.append(kwargs)
                return None

            def locator(self, _selector):
                raise RuntimeError("no body")

            def title(self):
                return ""

        cfg = Config(
            output_dir="out",
            cache_dir="cache",
            cookie_path="cookies.json",
            chrome_profile_dir="profile",
            carsi_cookie_dir="carsi",
        )
        downloader = PublisherBatchDownloader(cfg, profile=WILEY_PROFILE, pdf_timeout_sec=17)
        payload = b"%PDF-" + (b"x" * MIN_PDF_BYTES)
        downloader._fetch_pdf_url = lambda url: (payload, url)  # type: ignore[method-assign]
        page = FakePage()
        result = DownloadResult(doi="10.1002/adem.70982", status="failed")

        pdf_bytes, _pdf_url = downloader._capture_pdf(page, result.doi, result)

        self.assertEqual(pdf_bytes, payload)
        self.assertTrue(page.goto_kwargs)
        self.assertTrue(all(kwargs["wait_until"] == "commit" for kwargs in page.goto_kwargs))
        self.assertTrue(all(kwargs["timeout"] == 17_000 for kwargs in page.goto_kwargs))

    def test_capture_pdf_captures_browser_download_event_after_download_start_error(self):
        with TemporaryDirectory() as tmp:
            download_path = Path(tmp) / "download.pdf"
            payload = b"%PDF-" + (b"x" * MIN_PDF_BYTES)
            download_path.write_bytes(payload)

            class FakeDownload:
                url = "https://www.frontiersin.org/articles/10.3389/example/pdf"

                def path(self):
                    return str(download_path)

            class FakeDownloadContext:
                value = FakeDownload()

                def __enter__(self):
                    return self

                def __exit__(self, *_args):
                    return False

            class FakePage:
                url = "https://www.frontiersin.org/articles/10.3389/example/full"

                def __init__(self):
                    self.listeners = {}
                    self.goto_calls = []
                    self.expect_download_calls = 0

                def on(self, event, callback):
                    self.listeners[event] = callback

                def remove_listener(self, event, _callback):
                    self.listeners.pop(event, None)

                def evaluate(self, *_args):
                    return ["https://www.frontiersin.org/articles/10.3389/example/pdf"]

                def goto(self, url, **kwargs):
                    self.goto_calls.append((url, kwargs))
                    raise RuntimeError("Page.goto: Download is starting")

                def expect_download(self, **_kwargs):
                    self.expect_download_calls += 1
                    return FakeDownloadContext()

                def locator(self, _selector):
                    raise RuntimeError("no body")

                def title(self):
                    return ""

            cfg = Config(
                output_dir="out",
                cache_dir="cache",
                cookie_path="cookies.json",
                chrome_profile_dir="profile",
                carsi_cookie_dir="carsi",
            )
            downloader = PublisherBatchDownloader(cfg, profile=get_publisher_profile("frontiers"), pdf_timeout_sec=17)
            page = FakePage()
            result = DownloadResult(doi="10.3389/example", status="failed")

            pdf_bytes, pdf_url = downloader._capture_pdf(page, result.doi, result)

            self.assertEqual(pdf_bytes, payload)
            self.assertEqual(pdf_url, FakeDownload.url)
            self.assertEqual(page.expect_download_calls, 1)

    def test_capture_pdf_captures_browser_download_event_after_err_aborted(self):
        with TemporaryDirectory() as tmp:
            download_path = Path(tmp) / "download.pdf"
            payload = b"%PDF-" + (b"x" * MIN_PDF_BYTES)
            download_path.write_bytes(payload)
            pdf_url = "https://www.frontiersin.org/articles/10.3389/example/pdf"

            class FakeDownload:
                url = pdf_url

                def path(self):
                    return str(download_path)

            class FakeDownloadContext:
                value = FakeDownload()

                def __enter__(self):
                    return self

                def __exit__(self, *_args):
                    return False

            class FakePage:
                url = "https://www.frontiersin.org/articles/10.3389/example/full"

                def __init__(self):
                    self.listeners = {}
                    self.expect_download_calls = 0

                def on(self, event, callback):
                    self.listeners[event] = callback

                def remove_listener(self, event, _callback):
                    self.listeners.pop(event, None)

                def evaluate(self, *_args):
                    return [pdf_url]

                def goto(self, _url, **_kwargs):
                    raise RuntimeError("Page.goto: net::ERR_ABORTED")

                def expect_download(self, **_kwargs):
                    self.expect_download_calls += 1
                    return FakeDownloadContext()

                def locator(self, _selector):
                    raise RuntimeError("no body")

                def title(self):
                    return ""

            cfg = Config(
                output_dir="out",
                cache_dir="cache",
                cookie_path="cookies.json",
                chrome_profile_dir="profile",
                carsi_cookie_dir="carsi",
            )
            downloader = PublisherBatchDownloader(cfg, profile=get_publisher_profile("frontiers"), pdf_timeout_sec=17)
            page = FakePage()
            result = DownloadResult(doi="10.3389/example", status="failed")

            pdf_bytes, pdf_url = downloader._capture_pdf(page, result.doi, result)

            self.assertEqual(pdf_bytes, payload)
            self.assertEqual(pdf_url, FakeDownload.url)
            self.assertEqual(page.expect_download_calls, 1)

    def test_complete_manifest_prefers_retry_success(self):
        with TemporaryDirectory() as tmp:
            base = Path(tmp)
            pdf_dir = base / "retry" / "pdfs"
            pdf_dir.mkdir(parents=True)
            pdf = pdf_dir / "10.1021_acs.est.test.pdf"
            pdf.write_bytes(b"%PDF- fake enough for manifest copy")

            cfg = Config(
                output_dir=str(base / "out"),
                cache_dir=str(base / "cache"),
                cookie_path=str(base / "cookies.json"),
                chrome_profile_dir=str(base / "profile"),
                carsi_cookie_dir=str(base / "carsi"),
            )
            downloader = ACSCloakBatchDownloader(cfg)

            def fake_match(text, record, **_kwargs):
                return record.doi == "10.1021/acs.est.test"

            downloader._text_matches_record = fake_match  # type: ignore[method-assign]
            records = [PaperRecord(doi="10.1021/acs.est.test", title="A Test Paper")]
            results = [
                DownloadResult(
                    doi="10.1021/acs.est.test",
                    status="success",
                    pdf_path=str(pdf),
                    size_bytes=pdf.stat().st_size,
                )
            ]

            summary = downloader._write_complete_artifacts(records, results, base)

            self.assertEqual(summary["success"], 1)
            manifest = json.loads((base / "complete" / "manifest.json").read_text(encoding="utf-8"))
            self.assertEqual(manifest[0]["status"], "success")
            self.assertTrue(Path(manifest[0]["pdf_path"]).exists())

    def test_complete_manifest_marks_unverified_pdf_separately(self):
        with TemporaryDirectory() as tmp:
            base = Path(tmp)
            pdf_dir = base / "primary" / "pdfs"
            pdf_dir.mkdir(parents=True)
            pdf = pdf_dir / "10.1111_dmcn.70356.pdf"
            pdf.write_bytes(b"%PDF- fake enough for manifest copy")

            cfg = Config(
                output_dir=str(base / "out"),
                cache_dir=str(base / "cache"),
                cookie_path=str(base / "cookies.json"),
                chrome_profile_dir=str(base / "profile"),
                carsi_cookie_dir=str(base / "carsi"),
            )
            downloader = PublisherBatchDownloader(cfg, profile=WILEY_PROFILE)
            downloader._text_matches_record = lambda _text, _record, **_kwargs: False  # type: ignore[method-assign]
            records = [PaperRecord(doi="10.1111/dmcn.70356", title="")]
            results = [
                DownloadResult(
                    doi="10.1111/dmcn.70356",
                    status="success",
                    pdf_path=str(pdf),
                    size_bytes=pdf.stat().st_size,
                )
            ]

            summary = downloader._write_complete_artifacts(records, results, base)

            self.assertEqual(summary["success"], 0)
            self.assertEqual(summary["unverified"], 1)
            manifest = json.loads((base / "complete" / "manifest.json").read_text(encoding="utf-8"))
            self.assertEqual(manifest[0]["status"], "unverified")
            self.assertFalse(manifest[0]["verified_match"])
            self.assertTrue(Path(manifest[0]["pdf_path"]).exists())

    def test_run_records_stops_after_target_verified_count(self):
        class FakeContext:
            def close(self):
                return None

        class FakeDownloader(PublisherBatchDownloader):
            def __init__(self, config):
                super().__init__(config, profile=WILEY_PROFILE)
                self.fetched = []

            def _launch_context(self):
                return FakeContext()

            def fetch_one(self, _context, record, run_dir):
                self.fetched.append(record.doi)
                pdf_dir = run_dir / "pdfs"
                pdf_dir.mkdir(parents=True, exist_ok=True)
                pdf_path = pdf_dir / f"{safe_name(record.doi)}.pdf"
                pdf_path.write_bytes(b"%PDF- fake enough for manifest copy")
                return DownloadResult(
                    doi=record.doi,
                    status="success",
                    state="pdf_response_captured",
                    pdf_path=str(pdf_path),
                    size_bytes=pdf_path.stat().st_size,
                    verified_match=True,
                )

        with TemporaryDirectory() as tmp:
            base = Path(tmp)
            cfg = Config(
                output_dir=str(base / "out"),
                cache_dir=str(base / "cache"),
                cookie_path=str(base / "cookies.json"),
                chrome_profile_dir=str(base / "profile"),
                carsi_cookie_dir=str(base / "carsi"),
            )
            downloader = FakeDownloader(cfg)
            downloader._text_matches_record = lambda _text, _record, **_kwargs: True  # type: ignore[method-assign]
            records = [
                PaperRecord(doi="10.1002/one"),
                PaperRecord(doi="10.1002/two"),
                PaperRecord(doi="10.1002/three"),
            ]

            summary = downloader.run_records(
                records,
                base,
                retry_failed=False,
                target_verified=1,
            )

            self.assertEqual(downloader.fetched, ["10.1002/one"])
            self.assertEqual(summary["success"], 1)
            self.assertTrue(summary["target_reached"])
            self.assertEqual(summary["skipped"], 2)
            self.assertEqual(summary["browser_profile_dir"], str(base / "profile"))
            manifest = json.loads((base / "complete" / "manifest.json").read_text(encoding="utf-8"))
            self.assertEqual([item["status"] for item in manifest], ["success", "missing", "missing"])
            self.assertEqual(manifest[1]["reason"], "target_verified_reached")

    def test_run_records_parallel_uses_requested_concurrency(self):
        class FakeContext:
            def close(self):
                return None

        class FakeDownloader(PublisherBatchDownloader):
            def __init__(self, config):
                super().__init__(config, profile=WILEY_PROFILE)
                self.fetched = []
                self.active = 0
                self.max_active = 0
                self.lock = threading.Lock()

            def _launch_context(self, profile_dir=None):
                return FakeContext()

            def _prepare_worker_profile(self, _source, target):
                target.mkdir(parents=True, exist_ok=True)
                return target

            def fetch_one(self, _context, record, _run_dir):
                with self.lock:
                    self.active += 1
                    self.max_active = max(self.max_active, self.active)
                time.sleep(0.05)
                with self.lock:
                    self.fetched.append(record.doi)
                    self.active -= 1
                return DownloadResult(
                    doi=record.doi,
                    status="failed",
                    reason="pdf_not_captured",
                    state="pdf_not_captured",
                )

        with TemporaryDirectory() as tmp:
            base = Path(tmp)
            cfg = Config(
                output_dir=str(base / "out"),
                cache_dir=str(base / "cache"),
                cookie_path=str(base / "cookies.json"),
                chrome_profile_dir=str(base / "profile"),
                carsi_cookie_dir=str(base / "carsi"),
            )
            downloader = FakeDownloader(cfg)
            records = [
                PaperRecord(doi="10.1002/one"),
                PaperRecord(doi="10.1002/two"),
                PaperRecord(doi="10.1002/three"),
                PaperRecord(doi="10.1002/four"),
            ]

            summary = downloader.run_records(
                records,
                base / "run",
                retry_failed=False,
                concurrency=2,
            )

            self.assertEqual(sorted(downloader.fetched), sorted(record.doi for record in records))
            self.assertEqual(downloader.max_active, 2)
            self.assertEqual(summary["concurrency"], 2)

    def test_attempt_cache_skips_attempted_dois_and_appends_new_attempts(self):
        class FakeContext:
            def close(self):
                return None

        class FakeDownloader(PublisherBatchDownloader):
            def __init__(self, config):
                super().__init__(config, profile=WILEY_PROFILE)
                self.fetched = []

            def _launch_context(self):
                return FakeContext()

            def fetch_one(self, _context, record, _run_dir):
                self.fetched.append(record.doi)
                return DownloadResult(
                    doi=record.doi,
                    status="failed",
                    reason="pdf_not_captured",
                    state="pdf_not_captured",
                )

        with TemporaryDirectory() as tmp:
            base = Path(tmp)
            cache_path = base / "attempts.jsonl"
            cache_path.write_text(
                json.dumps({"doi": "10.1002/skip", "status": "failed"}) + "\n",
                encoding="utf-8",
            )
            cfg = Config(
                output_dir=str(base / "out"),
                cache_dir=str(base / "cache"),
                cookie_path=str(base / "cookies.json"),
                chrome_profile_dir=str(base / "profile"),
                carsi_cookie_dir=str(base / "carsi"),
            )
            downloader = FakeDownloader(cfg)

            summary = downloader.run_records(
                [PaperRecord(doi="10.1002/skip"), PaperRecord(doi="10.1002/new")],
                base / "run",
                retry_failed=False,
                attempt_cache=cache_path,
                skip_attempted=True,
            )

            self.assertEqual(downloader.fetched, ["10.1002/new"])
            self.assertEqual(summary["cached_skipped"], 1)
            lines = [json.loads(line) for line in cache_path.read_text(encoding="utf-8").splitlines()]
            self.assertEqual(lines[-1]["doi"], "10.1002/new")
            self.assertEqual(lines[-1]["status"], "failed")
            manifest = json.loads((base / "run" / "complete" / "manifest.json").read_text(encoding="utf-8"))
            self.assertEqual(manifest[0]["reason"], "skipped_cached_attempt")

    def test_text_match_requires_doi_or_title_evidence(self):
        record = PaperRecord(doi="10.1111/dmcn.70356", title="")

        self.assertFalse(
            PublisherBatchDownloader._text_matches_record(
                "Dev Med Child Neurol. P L A I N L A N G U A G E S U M M A R Y.",
                record,
            )
        )
        self.assertTrue(
            PublisherBatchDownloader._text_matches_record(
                "Full article text with DOI 10.1111/dmcn.70356 and methods.",
                record,
            )
        )

    def test_text_match_rejects_obvious_non_article_pdfs(self):
        record = PaperRecord(
            doi="10.1002/ajoc.70456",
            title="Direct Mechanochemical Oxidation Sustainable Access to Azine N-oxides",
        )

        self.assertFalse(
            PublisherBatchDownloader._text_matches_record(
                "Electronic Supporting Information Direct Mechanochemical Oxidation "
                "Sustainable Access to Azine N-oxides DOI 10.1002/ajoc.70456",
                record,
            )
        )
        self.assertFalse(
            PublisherBatchDownloader._text_matches_record(
                "Dear Dr. Quintana, we are delighted to inform you that your manuscript "
                "has been accepted for publication. DOI 10.1002/ajoc.70456",
                record,
            )
        )


if __name__ == "__main__":
    unittest.main()
