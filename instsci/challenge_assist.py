"""Challenge detection and human-assist helpers for visible browser workflows."""

from __future__ import annotations

from dataclasses import asdict, dataclass
from html import escape
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
import re
import threading
from typing import Any


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
        with self._lock:
            self._state.update(state)
            snapshot = dict(self._state)
        (self.state_dir / "assist_state.json").write_text(
            json.dumps(snapshot, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            return dict(self._state)

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
        body = f"""<!doctype html>
<html>
<head>
  <meta charset="utf-8" />
  <meta http-equiv="refresh" content="5" />
  <title>InstSci Human Assist</title>
  <style>
    body {{ font-family: system-ui, sans-serif; margin: 2rem; line-height: 1.45; }}
    code {{ background: #f3f4f6; padding: .1rem .25rem; border-radius: 4px; }}
    img {{ max-width: 100%; border: 1px solid #d1d5db; margin-top: 1rem; }}
  </style>
</head>
<body>
  <h1>InstSci Human Assist</h1>
  <p><strong>DOI:</strong> <code>{escape(str(state.get("doi", "")))}</code></p>
  <p><strong>Challenge:</strong> {escape(str(challenge.get("label", state.get("status", "waiting"))))}</p>
  <p>{escape(str(state.get("action", _ACTION_VISIBLE_BROWSER)))}</p>
  <p>Complete verification in the visible CloakBrowser window. This page refreshes automatically.</p>
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
