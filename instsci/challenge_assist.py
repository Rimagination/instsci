"""Challenge detection and human-assist helpers for visible browser workflows."""

from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import datetime
from html import escape
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
import re
import threading
from typing import Any
from urllib.parse import urlparse

from .browser_actions import normalize_handoff_state


@dataclass(frozen=True)
class ChallengeDetection:
    kind: str
    label: str
    action: str
    confidence: str = "medium"

    def to_dict(self) -> dict[str, str]:
        return asdict(self)


_ACTION_VISIBLE_BROWSER = (
    "Complete the verification manually in the visible CloakBrowser window; "
    "InstSci will keep waiting and resume automatically."
)

KEEPASSXC_SETUP_DOC = "docs/keepassxc-autotype.md"


def build_keepassxc_credential_assist(
    *,
    login_url: str = "",
    expected_domain: str = "",
    window_title: str = "",
) -> dict[str, Any]:
    """Return a non-secret KeePassXC Auto-Type handoff payload."""
    domain = _safe_hostname(expected_domain) or _safe_hostname(login_url)
    domain_arg = domain or "<institution-idp-host>"
    url_hint = f"https://{domain_arg}/"
    assist: dict[str, Any] = {
        "provider": "keepassxc",
        "mode": "auto_type",
        "expected_domain": domain,
        "trigger_command": f"instsci keepassxc-autotype --expected-domain {domain_arg} --trigger",
        "setup_doc": KEEPASSXC_SETUP_DOC,
        "first_time_setup_steps": [
            "Create one KeePassXC entry for this institution IdP, not for the publisher site.",
            f"Set the entry URL to {url_hint}.",
            "Set the entry username and password locally in KeePassXC; do not paste them into InstSci.",
            "Entry Auto-Type: 条目 -> 编辑条目 -> 自动输入; enable it and use {USERNAME}{TAB}{PASSWORD}.",
            "Add the window association hint if shown below.",
            "Global settings: 齿轮设置 -> 常规 -> 自动输入; set 全局自动输入快捷键, for example Ctrl+Alt+A.",
            "Save and unlock the database, focus the username field in CloakBrowser, then run the trigger command.",
        ],
        "steps": [
            "Unlock KeePassXC locally.",
            "Verify the visible browser address bar belongs to the institution IdP.",
            "Focus the username field in the visible CloakBrowser window.",
            "Run the trigger command or press the configured KeePassXC Auto-Type hotkey.",
            "Complete MFA, CAPTCHA, and final sign-in prompts manually in CloakBrowser.",
        ],
    }
    window_hint = _window_association_hint(window_title)
    if window_hint:
        assist["window_association_hint"] = window_hint
        assist["steps"].insert(
            3,
            "If Auto-Type does not match, add the window association hint to the KeePassXC entry.",
        )
    return assist


def detect_challenge(*, url: str = "", title: str = "", text: str = "") -> ChallengeDetection | None:
    """Classify visible browser challenge pages without solving or bypassing them."""
    haystack = _normalize(" ".join([url or "", title or "", text or ""]))

    if _has_any(haystack, ("crasolve", "els-captcha", "elsevier challenge")):
        return ChallengeDetection("crasolve", "Elsevier interactive challenge", _ACTION_VISIBLE_BROWSER, "high")

    if _has_any(haystack, ("validate.perfdrive.com", "perfdrive")):
        return ChallengeDetection("perfdrive", "PerfDrive browser validation", _ACTION_VISIBLE_BROWSER, "high")

    if _has_any(haystack, ("cf-turnstile", "cloudflare turnstile", "turnstile")):
        return ChallengeDetection("turnstile", "Cloudflare Turnstile verification", _ACTION_VISIBLE_BROWSER, "high")

    if _has_any(haystack, ("g-recaptcha", "grecaptcha", "google recaptcha", "recaptcha")):
        return ChallengeDetection("recaptcha", "reCAPTCHA verification", _ACTION_VISIBLE_BROWSER, "high")

    if _has_any(haystack, ("h-captcha", "hcaptcha")):
        return ChallengeDetection("hcaptcha", "hCaptcha verification", _ACTION_VISIBLE_BROWSER, "high")

    if "cloudflare" in haystack and _has_any(
        haystack,
        ("ray id:", "security verification", "security service", "not a robot", "checking your browser"),
    ):
        return ChallengeDetection("cloudflare", "Cloudflare browser challenge", _ACTION_VISIBLE_BROWSER, "high")

    generic_markers = (
        "just a moment",
        "attention required",
        "verify you are human",
        "checking your browser",
        "are you a robot",
        "please confirm you are a human",
        "complete the security check",
        "browser validation",
    )
    if _has_any(haystack, generic_markers):
        return ChallengeDetection("generic", "Browser verification challenge", _ACTION_VISIBLE_BROWSER)

    return None


def _normalize(value: str) -> str:
    return re.sub(r"\s+", " ", value).strip().lower()


def _has_any(haystack: str, needles: tuple[str, ...]) -> bool:
    return any(needle in haystack for needle in needles)


def _safe_hostname(value: str) -> str:
    text = str(value or "").strip()
    if not text:
        return ""
    parsed = urlparse(text if "://" in text else f"https://{text}")
    host = (parsed.hostname or "").strip().lower().rstrip(".")
    if not host or not re.fullmatch(r"[a-z0-9.-]+", host):
        return ""
    return host


def _window_association_hint(title: str) -> str:
    text = re.sub(r"\s+", " ", str(title or "")).strip()
    if not text:
        return ""
    text = re.sub(r"\s+-\s+(?:Chromium|Google Chrome|Microsoft Edge|Mozilla Firefox|Firefox)$", "", text).strip()
    if not text or text.lower() in {"about:blank", "new tab"}:
        return ""
    if any(marker in text.lower() for marker in ("password", "otp", "recovery code")):
        return ""
    return f"*{text[:80]}*"


def _safe_display_url(value: str) -> str:
    text = str(value or "").strip()
    if not text:
        return ""
    parsed = urlparse(text)
    if not parsed.scheme or not parsed.netloc:
        return text
    return parsed._replace(query="", fragment="").geturl()


class HumanAssistServer:
    """Small local status page for manual verification in visible CloakBrowser."""

    def __init__(self, *, host: str = "127.0.0.1", port: int = 0, state_dir: str | Path):
        self.host = host or "127.0.0.1"
        self.port = int(port or 0)
        self.state_dir = Path(state_dir)
        self._server: ThreadingHTTPServer | None = None
        self._thread: threading.Thread | None = None
        self._state: dict[str, Any] = {
            "status": "waiting",
            "action": _ACTION_VISIBLE_BROWSER,
        }
        self._lock = threading.Lock()

    @property
    def url(self) -> str:
        if not self._server:
            return ""
        host = "127.0.0.1" if self.host in ("0.0.0.0", "::") else self.host
        return f"http://{host}:{self._server.server_port}"

    def start(self) -> str:
        if self._server:
            return self.url
        self.state_dir.mkdir(parents=True, exist_ok=True)

        owner = self

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self) -> None:  # noqa: N802 - required by BaseHTTPRequestHandler
                if self.path.split("?", 1)[0] == "/status.json":
                    owner._send_json(self)
                    return
                if self.path.split("?", 1)[0] == "/latest-screenshot":
                    owner._send_screenshot(self)
                    return
                owner._send_html(self)

            def log_message(self, _format: str, *_args: Any) -> None:
                return

        self._server = ThreadingHTTPServer((self.host, self.port), Handler)
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)
        self._thread.start()
        self.update({"assist_url": self.url})
        return self.url

    def stop(self) -> None:
        if self._server:
            self._server.shutdown()
            self._server.server_close()
            self._server = None
        if self._thread:
            self._thread.join(timeout=5)
            self._thread = None

    def update(self, state: dict[str, Any]) -> None:
        self.state_dir.mkdir(parents=True, exist_ok=True)
        clear_fields_raw = state.pop("clear_fields", ())
        if isinstance(clear_fields_raw, str):
            clear_fields = [clear_fields_raw]
        else:
            clear_fields = [str(key) for key in clear_fields_raw or ()]
        with self._lock:
            for key in clear_fields:
                self._state.pop(key, None)
            state = {key: value for key, value in state.items() if value not in (None, "")}
            if "status" in state:
                raw_status = str(state.get("status") or "")
                normalized = normalize_handoff_state(raw_status).value
                if raw_status and raw_status != normalized and not state.get("status_reason"):
                    state["status_reason"] = raw_status
                state["status"] = normalized
            if state.get("credential_warning") and "credential_assist" not in state:
                state["credential_assist"] = build_keepassxc_credential_assist(
                    login_url=str(state.get("url") or ""),
                    expected_domain=str(state.get("expected_domain") or ""),
                    window_title=str(state.get("title") or ""),
                )
            state["updated_at"] = datetime.now().isoformat(timespec="seconds")
            self._state.update(state)
            snapshot = dict(self._state)
        (self.state_dir / "assist_state.json").write_text(
            json.dumps(snapshot, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )

    def snapshot(self) -> dict[str, Any]:
        disk_state = self._read_state_file()
        with self._lock:
            if disk_state:
                self._state.update(disk_state)
            return dict(self._state)

    def _read_state_file(self) -> dict[str, Any]:
        path = self.state_dir / "assist_state.json"
        try:
            loaded = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return {}
        return loaded if isinstance(loaded, dict) else {}

    def _send_json(self, handler: BaseHTTPRequestHandler) -> None:
        payload = json.dumps(self.snapshot(), ensure_ascii=False, indent=2).encode("utf-8")
        handler.send_response(200)
        handler.send_header("Content-Type", "application/json; charset=utf-8")
        handler.send_header("Content-Length", str(len(payload)))
        handler.end_headers()
        handler.wfile.write(payload)

    def _send_html(self, handler: BaseHTTPRequestHandler) -> None:
        state = self.snapshot()
        challenge = state.get("challenge") if isinstance(state.get("challenge"), dict) else {}
        screenshot = ""
        if state.get("screenshot_path"):
            screenshot = '<img alt="latest challenge screenshot" src="/latest-screenshot" />'
        resume = ""
        if state.get("resume_command"):
            resume = (
                "<section><h2>Resume</h2>"
                f"<p><code>{escape(str(state.get('resume_command', '')))}</code></p></section>"
            )
        diagnostic = ""
        if state.get("diagnostic_path"):
            diagnostic = (
                "<section><h2>Diagnostic</h2>"
                f"<p><code>{escape(str(state.get('diagnostic_path', '')))}</code></p></section>"
            )
        warning = ""
        if state.get("credential_warning"):
            warning = (
                "<p><strong>Credential safety:</strong> enter passwords, OTPs, recovery codes, "
                "and CAPTCHA answers only in the visible CloakBrowser window.</p>"
            )
        credential_assist = ""
        if isinstance(state.get("credential_assist"), dict):
            assist = state["credential_assist"]
            command = escape(str(assist.get("trigger_command") or ""))
            expected_domain = escape(str(assist.get("expected_domain") or ""))
            setup_doc = escape(str(assist.get("setup_doc") or KEEPASSXC_SETUP_DOC))
            window_hint = escape(str(assist.get("window_association_hint") or ""))
            window_hint_html = ""
            if window_hint:
                window_hint_html = (
                    "<p><strong>Window association hint:</strong> "
                    f"<code>{window_hint}</code></p>"
                )
            setup_steps = assist.get("first_time_setup_steps")
            setup_steps_html = ""
            if isinstance(setup_steps, list) and setup_steps:
                setup_steps_html = (
                    "<details open><summary>First-time KeePassXC setup checklist</summary>"
                    f"{_html_ordered_list(setup_steps)}</details>"
                )
            credential_assist = (
                "<section><h2>KeePassXC Auto-Type</h2>"
                "<p>Unlock KeePassXC, verify the visible institution login domain, "
                "focus the username field, then run:</p>"
                f"<p><code>{command}</code></p>"
                f"<p><strong>Expected domain:</strong> <code>{expected_domain}</code></p>"
                f"{window_hint_html}"
                f"{setup_steps_html}"
                f"<p><strong>Setup:</strong> <code>{setup_doc}</code></p></section>"
            )
        display_url = escape(_safe_display_url(str(state.get("url", ""))))
        body = f"""<!doctype html>
<html>
<head>
  <meta charset="utf-8" />
  <meta http-equiv="refresh" content="5" />
  <title>InstSci Human Assist</title>
  <style>
    body {{ font-family: system-ui, sans-serif; margin: 2rem; line-height: 1.45; }}
    h1 {{ margin-bottom: .25rem; }}
    h2 {{ margin: 1.25rem 0 .25rem; font-size: 1rem; }}
    code {{ background: #f3f4f6; padding: .1rem .25rem; border-radius: 4px; }}
    .meta {{ color: #4b5563; }}
    img {{ max-width: 100%; border: 1px solid #d1d5db; margin-top: 1rem; }}
  </style>
</head>
<body>
  <h1>InstSci Human Assist</h1>
  <p class="meta">This local page refreshes automatically.</p>
  <p><strong>Status:</strong> <code>{escape(str(state.get("status", "waiting")))}</code></p>
  <p><strong>Reason:</strong> <code>{escape(str(state.get("status_reason", "")))}</code></p>
  <p><strong>Publisher:</strong> {escape(str(state.get("publisher", "")))}</p>
  <p><strong>DOI:</strong> <code>{escape(str(state.get("doi", "")))}</code></p>
  <p><strong>Page:</strong> {escape(str(state.get("title", "")))}</p>
  <p><strong>URL:</strong> <code>{display_url}</code></p>
  <p><strong>Challenge:</strong> {escape(str(challenge.get("label", state.get("status", "waiting"))))}</p>
  <p>{escape(str(state.get("action", _ACTION_VISIBLE_BROWSER)))}</p>
  {warning}
  {credential_assist}
  {resume}
  {diagnostic}
  {screenshot}
</body>
</html>"""
        payload = body.encode("utf-8")
        handler.send_response(200)
        handler.send_header("Content-Type", "text/html; charset=utf-8")
        handler.send_header("Content-Length", str(len(payload)))
        handler.end_headers()
        handler.wfile.write(payload)

    def _send_screenshot(self, handler: BaseHTTPRequestHandler) -> None:
        path = Path(str(self.snapshot().get("screenshot_path") or ""))
        if not path.exists() or not path.is_file():
            handler.send_response(404)
            handler.end_headers()
            return
        payload = path.read_bytes()
        handler.send_response(200)
        handler.send_header("Content-Type", "image/png")
        handler.send_header("Content-Length", str(len(payload)))
        handler.end_headers()
        handler.wfile.write(payload)


def _html_ordered_list(items: list[Any]) -> str:
    safe_items = "".join(f"<li>{escape(str(item))}</li>" for item in items)
    return f"<ol>{safe_items}</ol>" if safe_items else ""
