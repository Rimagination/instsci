import unittest
from unittest.mock import patch

from typer.testing import CliRunner

from instsci.cli import app
from instsci.keepassxc_autotype import (
    domain_matches,
    normalize_hotkey,
)


class KeePassXCAutoTypeTests(unittest.TestCase):
    def test_domain_matches_exact_host_and_subdomain_only(self):
        self.assertTrue(domain_matches("https://idp.example.edu/login", "idp.example.edu"))
        self.assertTrue(domain_matches("https://login.idp.example.edu/sso", "idp.example.edu"))
        self.assertFalse(domain_matches("https://idp.example.edu.evil.test/login", "idp.example.edu"))
        self.assertFalse(domain_matches("https://evil-idp.example.edu/login", "idp.example.edu"))

    def test_normalize_hotkey_accepts_common_keepassxc_combo(self):
        self.assertEqual(normalize_hotkey("Ctrl+Alt+A"), ("ctrl", "alt", "a"))

    def test_normalize_hotkey_rejects_unsafe_or_incomplete_values(self):
        with self.assertRaises(ValueError):
            normalize_hotkey("ctrl+alt")
        with self.assertRaises(ValueError):
            normalize_hotkey("ctrl+alt+a;calc")
        with self.assertRaises(ValueError):
            normalize_hotkey("ctrl+alt+a+b")

    def test_cli_exposes_keepassxc_autotype_guidance_without_triggering(self):
        runner = CliRunner()

        with patch("instsci.keepassxc_autotype.trigger_keepassxc_autotype") as trigger:
            result = runner.invoke(
                app,
                [
                    "keepassxc-autotype",
                    "--expected-domain",
                    "idp.example.edu",
                    "--login-url",
                    "https://idp.example.edu/login",
                ],
            )

        self.assertEqual(result.exit_code, 0, result.output)
        trigger.assert_not_called()
        self.assertIn("KeePassXC Auto-Type", result.output)
        self.assertIn("idp.example.edu", result.output)
        self.assertIn("Ctrl+Alt+A", result.output)
        self.assertNotIn("secret-password", result.output)

    def test_cli_trigger_requires_confirmation_before_sending_hotkey(self):
        runner = CliRunner()

        with patch("instsci.keepassxc_autotype.trigger_keepassxc_autotype") as trigger:
            result = runner.invoke(
                app,
                [
                    "keepassxc-autotype",
                    "--expected-domain",
                    "idp.example.edu",
                    "--trigger",
                    "--countdown",
                    "0",
                ],
                input="y\n",
            )

        self.assertEqual(result.exit_code, 0, result.output)
        trigger.assert_called_once_with("ctrl+alt+a")
        self.assertIn("Auto-Type hotkey sent", result.output)

    def test_cli_refuses_mismatched_login_url_when_domain_is_expected(self):
        runner = CliRunner()

        result = runner.invoke(
            app,
            [
                "keepassxc-autotype",
                "--expected-domain",
                "idp.example.edu",
                "--login-url",
                "https://idp.example.edu.evil.test/login",
            ],
        )

        self.assertNotEqual(result.exit_code, 0)
        self.assertIn("does not match", result.output)


if __name__ == "__main__":
    unittest.main()
