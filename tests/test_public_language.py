import tomllib
import unittest
from pathlib import Path
import subprocess
import sys
from tempfile import TemporaryDirectory
from unittest.mock import patch

from typer.testing import CliRunner

import instsci.config as config_module
from instsci.cli import app
from instsci.config import Config
from tests.project_guards import find_project_reference_offenders


class PublicLanguageTests(unittest.TestCase):
    def setUp(self):
        self.runner = CliRunner()

    def test_cli_help_uses_institutional_access_branding(self):
        result = self.runner.invoke(app, ["--help"])

        self.assertEqual(result.exit_code, 0)
        self.assertIn("instsci", result.output)
        self.assertIn("institutional access", result.output.lower())
        self.assertIn("federated-login", result.output)
        self.assertNotIn("WebVPN", result.output)
        self.assertNotIn("carsi-login", result.output)

    def test_config_help_prefers_access_url_over_legacy_gateway_option(self):
        result = self.runner.invoke(app, ["config-cmd", "--help"])

        self.assertEqual(result.exit_code, 0)
        self.assertIn("--access-url", result.output)
        self.assertIn("--federated-enable", result.output)
        self.assertIn("--federated-school", result.output)
        self.assertNotIn("--webvpn-url", result.output)
        self.assertNotIn("--carsi-enable", result.output)

    def test_publisher_batch_help_exposes_profile_selection(self):
        result = self.runner.invoke(app, ["publisher-batch", "--help"])

        self.assertEqual(result.exit_code, 0)
        self.assertIn("--publisher", result.output)
        self.assertIn("--institution", result.output)
        self.assertIn("profile", result.output.lower())

    def test_papers_help_exposes_recommended_browser_workflow(self):
        result = self.runner.invoke(app, ["papers", "--help"])

        self.assertEqual(result.exit_code, 0)
        self.assertIn("recommended", result.output.lower())
        self.assertIn("--publisher", result.output)
        self.assertIn("--institution", result.output)

    def test_publisher_doctor_help_exposes_reusable_verification_asset(self):
        result = self.runner.invoke(app, ["publisher-doctor", "--help"])

        self.assertEqual(result.exit_code, 0)
        self.assertIn("--publisher", result.output)
        self.assertIn("--output", result.output)
        self.assertIn("verify", result.output.lower())
        self.assertIn("HTTP preflight", result.output)
        self.assertIn("browser", result.output.lower())

    def test_agents_requires_builtin_browser_for_publisher_pdf_verdicts(self):
        text = Path("AGENTS.md").read_text(encoding="utf-8")

        self.assertIn("MUST use InstSci's built-in CloakBrowser", text)
        self.assertIn("publisher-doctor", text)
        self.assertIn("HTTP preflight", text)

    def test_agents_exposes_actionable_publisher_pdf_workflow(self):
        text = Path("AGENTS.md").read_text(encoding="utf-8")

        self.assertIn("## Agent Workflow", text)
        self.assertIn("Classify the task", text)
        self.assertIn("instsci papers", text)
        self.assertIn("instsci publisher-batch", text)
        self.assertIn("Evidence Standard", text)
        self.assertIn("Report Template", text)

    def test_project_has_no_retired_package_name_references(self):
        root = Path(__file__).resolve().parents[1]
        retired_names = ("vpn" + "sci",)
        offenders = find_project_reference_offenders(root, retired_names, include_paths=True)

        self.assertEqual(offenders, [])

    def test_setup_help_exposes_one_step_environment_setup(self):
        result = self.runner.invoke(app, ["setup", "--help"])

        self.assertEqual(result.exit_code, 0)
        self.assertIn("--school", result.output)
        self.assertIn("--check", result.output)
        self.assertIn("environment", result.output.lower())

    def test_setup_configures_school_federated_login_and_directories(self):
        with TemporaryDirectory() as tmp:
            base = Path(tmp) / ".instsci"
            output_dir = Path(tmp) / "papers"
            with patch.object(config_module, "DEFAULT_BASE_DIR", base):
                result = self.runner.invoke(
                    app,
                    [
                        "setup",
                        "--school",
                        "清华大学",
                        "--email",
                        "reader@example.edu",
                        "--output-dir",
                        str(output_dir),
                    ],
                )
                cfg = Config.load()

            self.assertEqual(result.exit_code, 0, result.output)
            self.assertEqual(cfg.school, "清华大学")
            self.assertEqual(cfg.email, "reader@example.edu")
            self.assertTrue(cfg.carsi_enabled)
            self.assertEqual(cfg.carsi_idp_name, "清华大学")
            self.assertIn("webvpn.tsinghua.edu.cn", cfg.webvpn_base_url)
            self.assertTrue(output_dir.exists())
            self.assertIn("Environment ready", result.output)

    def test_setup_check_reports_missing_school_without_saving_new_config(self):
        with TemporaryDirectory() as tmp:
            base = Path(tmp) / ".instsci"
            with patch.object(config_module, "DEFAULT_BASE_DIR", base):
                result = self.runner.invoke(app, ["setup", "--check"])

            self.assertEqual(result.exit_code, 2, result.output)
            self.assertIn("School", result.output)
            self.assertIn("missing", result.output.lower())

    def test_package_exposes_inst_sci_console_scripts(self):
        pyproject = tomllib.loads(Path("pyproject.toml").read_text(encoding="utf-8"))

        scripts = pyproject["project"]["scripts"]
        self.assertIn("instsci", scripts)
        self.assertIn("instsci-mcp", scripts)
        self.assertEqual(scripts["instsci"], "instsci.cli:app")
        self.assertEqual(scripts["instsci-mcp"], "instsci.mcp_server:main")
        self.assertNotIn("VPN", pyproject["project"]["description"])

    def test_inst_sci_module_entrypoint_is_available(self):
        result = subprocess.run(
            [sys.executable, "-m", "instsci.cli", "--help"],
            cwd=Path(__file__).resolve().parents[1],
            text=True,
            encoding="utf-8",
            errors="replace",
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=10,
        )

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("institutional access", result.stdout.lower())
        self.assertNotIn("WebVPN", result.stdout)


if __name__ == "__main__":
    unittest.main()
