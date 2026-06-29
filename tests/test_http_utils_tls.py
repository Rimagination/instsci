import importlib
import os
import unittest

import instsci.http_utils as http_utils

_PROXY_ENV = ("HTTP_PROXY", "HTTPS_PROXY", "http_proxy", "https_proxy")
_INSECURE_ENV = "INSTSCI_INSECURE_TLS"


class TlsVerifyPolicyTests(unittest.TestCase):
    """TLS verification must stay on unless explicitly opted out.

    Regression guard: a proxy env var alone must never disable certificate
    verification (which previously exposed authenticated sessions).
    """

    def setUp(self):
        self._saved = {k: os.environ.get(k) for k in (*_PROXY_ENV, _INSECURE_ENV)}
        for k in self._saved:
            os.environ.pop(k, None)

    def tearDown(self):
        for k, v in self._saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        importlib.reload(http_utils)

    def _verify(self):
        return importlib.reload(http_utils)._SSL_VERIFY

    def test_verify_on_by_default(self):
        self.assertTrue(self._verify())

    def test_verify_stays_on_behind_proxy(self):
        os.environ["HTTPS_PROXY"] = "http://127.0.0.1:8888"
        self.assertTrue(self._verify())

    def test_verify_disabled_only_with_explicit_optin(self):
        os.environ[_INSECURE_ENV] = "1"
        self.assertFalse(self._verify())

    def test_optin_accepts_common_truthy_values(self):
        for val in ("true", "YES", "on"):
            with self.subTest(val=val):
                os.environ[_INSECURE_ENV] = val
                self.assertFalse(self._verify())

    def test_optin_ignores_falsey_values(self):
        for val in ("", "0", "false", "no"):
            with self.subTest(val=val):
                os.environ[_INSECURE_ENV] = val
                self.assertTrue(self._verify())


if __name__ == "__main__":
    unittest.main()
