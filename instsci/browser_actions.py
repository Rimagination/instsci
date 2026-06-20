"""Small browser action primitives for InstSci's visible CloakBrowser flow."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from enum import Enum
import re
from typing import Any


class BrowserActionKind(str, Enum):
    OBSERVE = "observe"
    FIND = "find"
    CLICK_PUBLIC = "click_public"
    WAIT_STABLE = "wait_stable"
    CAPTURE_PDF = "capture_pdf"
    VERIFY_PDF = "verify_pdf"
    PAUSE_FOR_USER = "pause_for_user"
    RESUME = "resume"


class HumanHandoffState(str, Enum):
    RUNNING = "running"
    CHECKPOINT_DETECTED = "checkpoint_detected"
    INSTITUTION_LOGIN_REQUIRED = "institution_login_required"
    REAUTH_REQUIRED = "reauth_required"
    READY_TO_RESUME = "ready_to_resume"
    RESUMING = "resuming"
    ATTENTION_REQUIRED = "attention_required"
    COMPLETE = "complete"
    WAITING = "waiting"


_REAUTH_REASONS = {
    "sso_required",
    "challenge_or_viewer_timeout",
    "sso_redirect_stalled",
    "institution_required",
    "logged_out",
    "crasolve",
    "perfdrive",
    "turnstile",
    "recaptcha",
    "hcaptcha",
    "cloudflare",
    "generic",
}

_ATTENTION_REASONS = {
    "institution_pdf_entitlement_missing",
    "pdf_not_captured",
    "pdf_integrity_failed",
    "html_response",
}

_PUBLIC_CLICK_MARKERS = {
    "pdf",
    "download",
    "access through your organization",
    "access through your institution",
    "institutional access",
    "institutional sign in",
    "log in via an institution",
    "view pdf",
    "article pdf",
    "accept",
    "continue",
}

_SECRET_CLICK_MARKERS = {
    "password",
    "passcode",
    "otp",
    "one-time",
    "one time",
    "verification code",
    "recovery code",
    "authenticator",
    "captcha",
    "not a robot",
    "username",
    "email",
}


@dataclass(frozen=True)
class BrowserObservation:
    publisher: str = ""
    doi: str = ""
    url: str = ""
    title: str = ""
    action: str = BrowserActionKind.OBSERVE.value
    challenge: dict[str, Any] = field(default_factory=dict)
    screenshot_path: str = ""
    text_markers: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def normalize_handoff_state(status: str | HumanHandoffState) -> HumanHandoffState:
    value = str(status.value if isinstance(status, HumanHandoffState) else status or "").strip().lower()
    if not value:
        return HumanHandoffState.WAITING
    for state in HumanHandoffState:
        if value == state.value:
            return state
    if value in _REAUTH_REASONS:
        return HumanHandoffState.REAUTH_REQUIRED
    if value in _ATTENTION_REASONS:
        return HumanHandoffState.ATTENTION_REQUIRED
    return HumanHandoffState.ATTENTION_REQUIRED


def is_safe_public_click_label(label: str) -> bool:
    text = _normalize(label)
    if not text:
        return False
    if any(marker in text for marker in _SECRET_CLICK_MARKERS):
        return False
    return any(marker in text for marker in _PUBLIC_CLICK_MARKERS)


def observe_page(
    page: Any,
    *,
    publisher: str = "",
    doi: str = "",
    action: BrowserActionKind | str = BrowserActionKind.OBSERVE,
    challenge: Any = None,
    screenshot_path: str = "",
    max_marker_chars: int = 800,
) -> BrowserObservation:
    """Capture non-secret page state for diagnostics and handoff surfaces."""
    title = ""
    try:
        title = str(page.title() or "")
    except Exception:
        title = ""
    body_text = ""
    try:
        body_text = str(page.locator("body").inner_text(timeout=1000) or "")
    except Exception:
        body_text = ""
    return BrowserObservation(
        publisher=publisher,
        doi=doi,
        url=str(getattr(page, "url", "") or ""),
        title=title,
        action=str(action.value if isinstance(action, BrowserActionKind) else action or BrowserActionKind.OBSERVE.value),
        challenge=_challenge_to_dict(challenge),
        screenshot_path=str(screenshot_path or ""),
        text_markers=_safe_text_markers(body_text, max_chars=max_marker_chars),
    )


def _challenge_to_dict(challenge: Any) -> dict[str, Any]:
    if challenge is None:
        return {}
    if hasattr(challenge, "to_dict"):
        data = challenge.to_dict()
        return data if isinstance(data, dict) else {}
    return challenge if isinstance(challenge, dict) else {}


def _safe_text_markers(text: str, *, max_chars: int) -> list[str]:
    markers: list[str] = []
    for line in re.split(r"[\r\n]+", text or ""):
        normalized = _normalize(line)
        if not normalized:
            continue
        if any(secret in normalized for secret in _SECRET_CLICK_MARKERS):
            continue
        if is_safe_public_click_label(normalized):
            markers.append(normalized[:max_chars])
    return markers[:20]


def _normalize(value: str) -> str:
    return re.sub(r"\s+", " ", str(value or "")).strip().lower()
