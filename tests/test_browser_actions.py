import unittest

from instsci.browser_actions import (
    BrowserActionKind,
    BrowserObservation,
    HumanHandoffState,
    is_safe_public_click_label,
    observe_page,
    normalize_handoff_state,
)
from instsci.challenge_assist import ChallengeDetection


class BrowserActionTests(unittest.TestCase):
    def test_observe_page_captures_non_secret_page_state(self):
        class FakeBody:
            def inner_text(self, **_kwargs):
                return (
                    "Access through your organization\n"
                    "PDF\n"
                    "Password: super-secret should not be retained\n"
                    "One-time code 123456 should not be retained"
                )

        class FakePage:
            url = "https://example.publisher.org/article/10.1000/test"

            def title(self):
                return "Example article"

            def locator(self, selector):
                self.selector = selector
                return FakeBody()

        detection = ChallengeDetection(
            "cloudflare",
            "Cloudflare browser challenge",
            "Complete the verification manually.",
            "high",
        )

        observation = observe_page(
            FakePage(),
            publisher="Example Publisher",
            doi="10.1000/test",
            action=BrowserActionKind.OBSERVE,
            challenge=detection,
            screenshot_path="diagnostics/screenshot.png",
        )

        self.assertIsInstance(observation, BrowserObservation)
        self.assertEqual(observation.publisher, "Example Publisher")
        self.assertEqual(observation.doi, "10.1000/test")
        self.assertEqual(observation.url, "https://example.publisher.org/article/10.1000/test")
        self.assertEqual(observation.title, "Example article")
        self.assertEqual(observation.action, "observe")
        self.assertEqual(observation.challenge["kind"], "cloudflare")
        self.assertEqual(observation.screenshot_path, "diagnostics/screenshot.png")
        self.assertIn("access through your organization", observation.text_markers)
        self.assertIn("pdf", observation.text_markers)
        self.assertNotIn("super-secret", " ".join(observation.text_markers))
        self.assertNotIn("123456", " ".join(observation.text_markers))

    def test_safe_public_click_labels_exclude_credentials_and_captcha(self):
        self.assertTrue(is_safe_public_click_label("PDF"))
        self.assertTrue(is_safe_public_click_label("Access through your organization"))
        self.assertTrue(is_safe_public_click_label("Download article"))
        self.assertFalse(is_safe_public_click_label("Password"))
        self.assertFalse(is_safe_public_click_label("Enter verification code"))
        self.assertFalse(is_safe_public_click_label("Solve CAPTCHA"))

    def test_handoff_state_normalization_preserves_known_states(self):
        self.assertEqual(
            normalize_handoff_state("checkpoint_detected"),
            HumanHandoffState.CHECKPOINT_DETECTED,
        )
        self.assertEqual(
            normalize_handoff_state("sso_required"),
            HumanHandoffState.REAUTH_REQUIRED,
        )
        self.assertEqual(
            normalize_handoff_state("challenge_or_viewer_timeout"),
            HumanHandoffState.REAUTH_REQUIRED,
        )
        self.assertEqual(
            normalize_handoff_state("institution_pdf_entitlement_missing"),
            HumanHandoffState.ATTENTION_REQUIRED,
        )


if __name__ == "__main__":
    unittest.main()
