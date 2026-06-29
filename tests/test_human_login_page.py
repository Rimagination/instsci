import types
import unittest

from instsci.publisher_batch import PublisherBatchDownloader


def _page(url):
    return types.SimpleNamespace(url=url)


def _shim():
    # _is_human_login_page only calls self._title/self._body_text as a last
    # resort; the cases below resolve from the URL/host before that.
    s = types.SimpleNamespace()
    s._title = lambda page: ""
    s._body_text = lambda page, n=0: ""
    return s


def _is_human(url):
    return PublisherBatchDownloader._is_human_login_page(_shim(), _page(url))


class HumanLoginPageTests(unittest.TestCase):
    """The login loop must YIELD on real login/IdP pages, not re-click them.

    Regression: login.openathens.net was not recognized, so the loop thrashed
    the page and the user could never enter institution/credentials.
    """

    def test_openathens_waits(self):
        self.assertTrue(
            _is_human("https://login.openathens.net/auth/gen?t=%2Fsaml%2F2%2Fsso")
        )

    def test_seamlessaccess_waits(self):
        self.assertTrue(_is_human("https://seamlessaccess.org/ds/"))

    def test_azure_ad_waits(self):
        self.assertTrue(
            _is_human("https://login.microsoftonline.com/common/oauth2/authorize")
        )

    def test_duo_2fa_waits(self):
        self.assertTrue(_is_human("https://api-abc.duosecurity.com/frame/web/v1/auth"))

    def test_au_university_idp_waits(self):
        self.assertTrue(
            _is_human("https://idp.une.edu.au/idp/profile/SAML2/Redirect/SSO")
        )

    def test_uk_university_idp_waits(self):
        self.assertTrue(_is_human("https://sso.cam.ac.uk/login"))

    def test_publisher_pages_do_not_wait(self):
        # Publisher article / SSO-entry pages must NOT be treated as human-login,
        # so the loop still clicks the institutional-access entry button.
        self.assertFalse(_is_human("https://onlinelibrary.wiley.com/doi/10.1002/bdm.2118"))
        self.assertFalse(_is_human("https://www.tandfonline.com/doi/full/10.1080/x"))


if __name__ == "__main__":
    unittest.main()
