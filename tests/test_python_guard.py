import unittest

from instsci.cloakbrowser_compat import browser_python_warning


class BrowserPythonGuardTests(unittest.TestCase):
    """Browser path requires Python 3.10-3.13; 3.14+ must warn clearly."""

    def test_supported_versions_no_warning(self):
        for v in [(3, 10), (3, 11), (3, 12), (3, 13), (3, 13, 2)]:
            with self.subTest(v=v):
                self.assertIsNone(browser_python_warning(v))

    def test_unsupported_versions_warn(self):
        for v in [(3, 14), (3, 14, 1), (3, 15), (4, 0)]:
            with self.subTest(v=v):
                msg = browser_python_warning(v)
                self.assertIsNotNone(msg)
                self.assertIn(f"{v[0]}.{v[1]}", msg)

    def test_default_uses_running_interpreter(self):
        # Must not raise regardless of the interpreter running the suite.
        browser_python_warning()


if __name__ == "__main__":
    unittest.main()
