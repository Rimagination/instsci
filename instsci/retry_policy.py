"""Failure categories for automatic retries and explicit job recovery."""

HTTP_RETRY_STATUSES = {408, 429, 500, 502, 503, 504}
BROWSER_RETRY_COOLDOWN_SEC = 30
BROWSER_RETRY_INTERVAL_SEC = 5
TRANSIENT_REASONS = {
    "pdf_not_captured", "navigation_error", "network_error", "timeout",
    "rate_limited", "publisher_temporarily_unavailable",
}
HUMAN_ACTION_REASONS = {
    "sso_required", "auth_required", "reauth_required", "sso_redirect_stalled",
    "captcha_required", "challenge_or_viewer_timeout",
}


def failure_category(reason: str, state: str = "") -> str:
    """Keep access and verification failures out of automatic retries."""
    reason, state = str(reason or "").lower(), str(state or "").lower()
    if reason in HUMAN_ACTION_REASONS or state in HUMAN_ACTION_REASONS:
        return "human_action"
    if reason in {"institution_pdf_entitlement_missing", "not_entitled", "access_denied"}:
        return "access_denied"
    if reason in TRANSIENT_REASONS:
        return "transient"
    if reason.split(":", 1)[0] in {"timeouterror", "connecttimeout", "readtimeout", "connectionerror"}:
        return "transient"
    return "attention_required"
