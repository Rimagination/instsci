import json
import unittest
from pathlib import Path

from typer.testing import CliRunner

from instsci.cli import app
from instsci.publisher_profiles import get_publisher_profile, list_publisher_profiles


class FakeResponse:
    def __init__(self, url, status_code=200, text="", headers=None, history=None):
        self.url = url
        self.status_code = status_code
        self.text = text
        self.headers = headers or {"content-type": "text/html"}
        self.history = history or []

    def close(self):
        return None


class FakeSession:
    def __init__(self):
        self.urls = []

    def get(self, url, **_kwargs):
        self.urls.append(url)
        if url.startswith("https://doi.org/"):
            return FakeResponse(
                "https://ieeexplore.ieee.org/document/9876543/",
                text="<html><a href='/stampPDF/getPDF.jsp?tp=&isnumber=&arnumber=9876543'>PDF</a></html>",
            )
        return FakeResponse(
            url,
            status_code=200,
            headers={"content-type": "application/pdf"},
        )


class PublisherAccessCatalogTests(unittest.TestCase):
    def test_access_catalog_covers_every_publisher_profile(self):
        from instsci.publisher_access import load_publisher_access_catalog

        catalog = load_publisher_access_catalog()

        self.assertEqual(set(catalog["publishers"]), set(list_publisher_profiles()))
        for key, entry in catalog["publishers"].items():
            profile = get_publisher_profile(key)
            self.assertEqual(entry["profile_key"], key)
            self.assertEqual(entry["verification"]["sample_doi"], profile.sample_dois[0])
            self.assertTrue(entry["pdf_route_strategy"])
            self.assertTrue(entry["identity"]["closed_access_requires"])
            self.assertIn("browser_profile_dir", entry["persistence"]["stores"])
            self.assertTrue(entry["link_characteristics"])

    def test_browser_verification_matrix_is_project_asset(self):
        from instsci.publisher_access import load_publisher_browser_verification_matrix

        matrix = load_publisher_browser_verification_matrix()

        self.assertEqual(set(matrix["publishers"]), set(list_publisher_profiles()))
        self.assertEqual(matrix["verdict_source"], "InstSci built-in CloakBrowser workflow")
        self.assertIn("not HTTP preflight", matrix["scope"])
        self.assertEqual(matrix["summary"]["total_count"], len(list_publisher_profiles()))
        self.assertEqual(
            matrix["summary"]["browser_verified_count"],
            sum(1 for entry in matrix["publishers"].values() if entry["browser_verified"]),
        )
        self.assertGreaterEqual(matrix["summary"]["browser_verified_count"], 14)
        self.assertIn(
            "https://www.sciencedirect.com/science/article/pii/S0043135424004093/pdfft",
            matrix["publishers"]["elsevier"]["observed_pdf_candidates"],
        )
        self.assertNotIn(
            "crossmark.crossref.org",
            " ".join(matrix["publishers"]["iop"]["observed_pdf_candidates"]).lower(),
        )

    def test_institutional_identity_policy_records_webvpn_limits(self):
        from instsci.publisher_access import load_institutional_identity_policy

        policy = load_institutional_identity_policy()

        self.assertEqual(policy["default_mode"], "auto")
        self.assertNotEqual(policy["default_identity"], "webvpn")
        self.assertEqual(policy["preferred_off_campus_access"], "shibboleth_or_openathens")
        self.assertTrue(policy["subscription_institution"]["required_for_closed_access"])
        self.assertEqual(policy["subscription_institution"]["hardcoded_default"], "")
        self.assertEqual(policy["final_pdf_verdict_requires"], "visible_cloakbrowser")
        self.assertIn("publisher_broker", policy["identity_order"])
        self.assertIn("webvpn_broker", policy["identity_order"])
        self.assertLess(
            policy["login_method_order"].index("wayfless_federated_sso"),
            policy["login_method_order"].index("webvpn_broker"),
        )
        self.assertIn("standard_federated_sso", policy["federated_login_methods"])
        self.assertIn("wayfless_federated_sso", policy["federated_login_methods"])

        webvpn = policy["identities"]["webvpn"]
        self.assertFalse(webvpn["global_default"])
        self.assertEqual(webvpn["recommended_role"], "optional_identity_layer")
        self.assertIn("cookie_store", webvpn["persistence_limits"])
        self.assertEqual(
            webvpn["persistence_limits"]["cookie_store"]["verdict_scope"],
            "HTTP preflight only",
        )
        self.assertIn("tls_session", webvpn["non_exportable_state"])
        self.assertIn("browser_fingerprint", webvpn["non_exportable_state"])
        self.assertIn(
            "same_live_cloakbrowser_context",
            webvpn["recommended_persistence"],
        )

        tsinghua_probe = policy["institutional_findings"]["tsinghua_webvpn_2026_06_07"]
        self.assertEqual(tsinghua_probe["verdict"], "not_universal_pdf_route")
        self.assertIn("elsevier", tsinghua_probe["publisher_results"])
        self.assertIn("wiley", tsinghua_probe["publisher_results"])
        self.assertIn(
            "downloads\\webvpn_live_context_probe_20260607\\summary.json",
            tsinghua_probe["browser_verified_artifacts"],
        )

    def test_identity_policy_command_exposes_webvpn_as_optional(self):
        runner = CliRunner()

        result = runner.invoke(app, ["identity-policy"])

        self.assertEqual(result.exit_code, 0, result.output)
        self.assertIn("Default mode: auto", result.output)
        self.assertIn("Default identity: publisher_broker", result.output)
        self.assertIn("Subscription institution: required", result.output)
        self.assertIn("Preferred off-campus access: shibboleth_or_openathens", result.output)
        self.assertIn("WebVPN is optional", result.output)
        self.assertIn("visible_cloakbrowser", result.output)

    def test_legacy_publisher_carsi_config_does_not_default_to_tsinghua(self):
        config_path = Path(__file__).parents[1] / "instsci" / "data" / "publisher_carsi.json"
        configs = json.loads(config_path.read_text(encoding="utf-8"))

        for key, entry in configs.items():
            selector = entry.get("result_selector", "")
            self.assertNotIn("Tsinghua", selector, key)
            self.assertNotIn("清华", selector, key)

    def test_verify_publisher_access_builds_pdf_candidates_from_catalog(self):
        from instsci.publisher_access import verify_publisher_access

        result = verify_publisher_access("ieee", session=FakeSession(), probe_pdf=True)

        self.assertEqual(result["profile_key"], "ieee")
        self.assertEqual(result["landing_status"], 200)
        self.assertIn("ieeexplore.ieee.org/document/9876543", result["landing_url"])
        self.assertEqual(
            result["pdf_candidates"][0],
            "https://ieeexplore.ieee.org/stampPDF/getPDF.jsp?tp=&isnumber=&arnumber=9876543",
        )
        self.assertEqual(result["candidate_probes"][0]["classification"], "pdf_accessible")


if __name__ == "__main__":
    unittest.main()
