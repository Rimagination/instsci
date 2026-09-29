"""Regression tests for instsci issues #9, #13, #15 and #10/#6 adapter drift."""

import json
import unittest
from collections import namedtuple
from pathlib import Path
from unittest.mock import patch

from instsci.carsi import _looks_like_error_page
from instsci.cloakbrowser_compat import (
    BrowserPythonUnsupportedError,
    ensure_browser_python_supported,
)
from instsci.config import Config
from instsci.publisher_batch import (
    NON_ARTICLE_PDF_MARKERS,
    DownloadResult,
    PublisherBatchDownloader,
    PaperRecord,
    _is_purchase_control,
)
from instsci.publisher_profiles import RSC_PROFILE, SPRINGER_PROFILE


def _make_config() -> Config:
    return Config(
        output_dir="out",
        cache_dir="cache",
        cookie_path="cookies.json",
        chrome_profile_dir="profile",
        carsi_cookie_dir="carsi",
    )


def _page(url: str, body: str, title: str = "Article"):
    class FakeBody:
        def inner_text(self, **_kwargs):
            return body

    class FakePage:
        pass

    page = FakePage()
    page.url = url
    page.title = lambda: title
    page.locator = lambda _selector: FakeBody()
    return page


class SpringerBuyPaywallTests(unittest.TestCase):
    """#13: 'Buy article PDF USD 39.95' is a hard auth wall, not a logged-in page."""

    def setUp(self):
        self.downloader = PublisherBatchDownloader(_make_config(), profile=SPRINGER_PROFILE)

    def test_buy_article_pdf_is_auth_wall(self):
        page = _page(
            "https://www.nature.com/articles/s11630-023-1652-y",
            "Journal of Thermal Science. Buy article PDF USD 39.95. "
            "Access through your institution.",
            title="Journal of Thermal Science | Home",
        )
        self.assertTrue(self.downloader._looks_logged_out(page))

    def test_buy_article_pdf_alone_is_auth_wall(self):
        page = _page(
            "https://link.springer.com/article/10.1007/s11837-026-08536-7",
            "Buy article PDF USD 39.95",
        )
        self.assertTrue(self.downloader._looks_logged_out(page))

    def test_springer_login_via_institution_card_is_auth_wall(self):
        page = _page(
            "https://link.springer.com/article/10.1007/s11837-026-08536-7",
            "Log in via an institution",
        )
        self.assertTrue(self.downloader._looks_logged_out(page))

    def test_rsc_purchase_options_stays_logged_in(self):
        downloader = PublisherBatchDownloader(_make_config(), profile=RSC_PROFILE)
        page = _page(
            "https://pubs.rsc.org/en/content/articlelanding/2026/cc/d5cc06607g",
            "Article abstract, supplementary files, citation links, and purchase options.",
            title="Enantioselective C-H bond oxidation - Chemical Communications (RSC Publishing)",
        )
        self.assertFalse(downloader._looks_logged_out(page))


class PdfEntryPurchaseDenylistTests(unittest.TestCase):
    """#13: generic PDF selectors must not click Buy/Cart/price controls."""

    def test_purchase_control_detector(self):
        self.assertTrue(_is_purchase_control("Buy article PDF USD 39.95", ""))
        self.assertTrue(_is_purchase_control("Purchase PDF", ""))
        self.assertTrue(_is_purchase_control("", "https://x.com/buy/article"))
        self.assertFalse(_is_purchase_control("Download PDF", "/doi/pdf/10.1007/x"))

    def _downloader_with_page(self, page):
        downloader = PublisherBatchDownloader(_make_config(), profile=SPRINGER_PROFILE)
        page.locator = lambda _selector: _FakePurchaseLocator()
        return downloader

    def test_buy_button_is_skipped_not_clicked(self):
        clicked: list[str] = []

        class _FakePurchaseLocator:
            def is_visible(self, timeout=None):
                return True

            def inner_text(self, timeout=None):
                return "Buy article PDF USD 39.95"

            def get_attribute(self, name, timeout=None):
                return ""

            def click(self, **_kwargs):
                clicked.append("purchase-control")

        class FakePage:
            url = "https://link.springer.com/article/10.1007/x"

            def title(self):
                return "Article"

            def locator(self, selector):
                return _FakePurchaseLocator()

            def evaluate(self, *_args, **_kwargs):
                return None

        downloader = self._downloader_with_page(FakePage())
        result = DownloadResult(doi="10.1007/x", status="failed")
        self.assertFalse(downloader._click_pdf_entry(FakePage(), result))
        self.assertEqual(clicked, [])


class EsmNotArticleTests(unittest.TestCase):
    """#15 bug 3: Springer ESM cover sheets must not verify as full text."""

    def test_supplementary_cover_sheet_is_non_article(self):
        text = (
            "Supplementary Material for: Deformation behavior and constitutive model "
            "modification with friction correction of TC4 Titanium alloy during hot "
            "compression. 10.1007/s11837-026-08536-7"
        )
        record = PaperRecord(
            doi="10.1007/s11837-026-08536-7",
            title="Deformation behavior and constitutive model modification",
        )
        self.assertFalse(
            PublisherBatchDownloader._text_matches_record(text, record, fallback_title=record.title)
        )

    def test_article_text_still_matches(self):
        record = PaperRecord(doi="10.1007/s11837-026-08536-7", title="Deformation behavior")
        text = "Abstract. Introduction. References. doi.org/10.1007/s11837-026-08536-7"
        self.assertTrue(
            PublisherBatchDownloader._text_matches_record(text, record, fallback_title="")
        )

    def test_marker_table_covers_esm(self):
        for marker in ("supplementary material for", "moesm", "mediaobjects"):
            self.assertIn(marker, NON_ARTICLE_PDF_MARKERS)

    def test_springer_profile_marks_esm_urls_supplementary(self):
        from instsci.publisher_pdf_router import is_supplementary_url

        for url in (
            "https://link.springer.com/content/pdf/10.1007/s11837-026-08536-7/MediaObjects/"
            "11837_2026_8536_MOESM1_ESM.pdf",
            "https://link.springer.com/content/esm/10.1007/x.pdf",
        ):
            self.assertTrue(is_supplementary_url(SPRINGER_PROFILE, url), url)
        self.assertFalse(
            is_supplementary_url(
                SPRINGER_PROFILE, "https://link.springer.com/content/pdf/10.1007/x.pdf"
            )
        )


class CarsiAdapterDriftTests(unittest.TestCase):
    """#6/#10/#15 bug 1: no stale /institutional-login entries; 404 lands fail fast."""

    def _configs(self):
        path = Path(__file__).resolve().parent.parent / "instsci" / "data" / "publisher_carsi.json"
        return json.loads(path.read_text(encoding="utf-8"))

    def test_no_stale_institutional_login_urls(self):
        for key, cfg in self._configs().items():
            self.assertNotIn(
                "/institutional-login", cfg["login_url"], f"{key} login_url is stale"
            )

    def test_springer_nature_use_wayf(self):
        configs = self._configs()
        self.assertIn("wayf.springernature.com", configs["springer"]["login_url"])
        self.assertIn("wayf.springernature.com", configs["nature"]["login_url"])

    def test_error_page_detection(self):
        page = _page("https://link.springer.com/institutional-login", "x", title="Page not found")
        self.assertTrue(_looks_like_error_page(page))
        ok = _page(
            "https://wayf.springernature.com/?redirect_uri=https://link.springer.com",
            "Search for your institution",
            title="Access through your institution",
        )
        self.assertFalse(_looks_like_error_page(ok))


class PythonGuardTests(unittest.TestCase):
    """#9: browser path refuses 3.14+ with a clear message."""

    def test_guard_passes_on_313(self):
        with patch("instsci.cloakbrowser_compat.sys.version_info", (3, 13, 0)):
            ensure_browser_python_supported()

    def test_guard_raises_on_314(self):
        version = namedtuple("_Version", "major minor micro")(3, 14, 0)
        with patch("instsci.cloakbrowser_compat.sys.version_info", version):
            with self.assertRaises(BrowserPythonUnsupportedError):
                ensure_browser_python_supported()


if __name__ == "__main__":
    unittest.main()
