"""CloakBrowser network identity helpers."""

from __future__ import annotations

from datetime import datetime
import hashlib
import importlib.metadata
import json
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit, urlunsplit

from .config import Config


IDENTITY_MANIFEST_NAME = ".instsci-browser-identity.json"
BASE_BROWSER_ARGS = ["--disable-features=CrossOriginOpenerPolicy"]


def mask_secret_url(url: str) -> str:
    """Mask URL userinfo while preserving enough detail to identify the proxy."""
    value = str(url or "").strip()
    if not value:
        return ""
    try:
        parsed = urlsplit(value)
    except ValueError:
        return value
    if not parsed.username:
        return value

    userinfo = parsed.username
    if parsed.password is not None:
        userinfo += ":****"
    host = parsed.hostname or ""
    if ":" in host and not host.startswith("["):
        host = f"[{host}]"
    if parsed.port is not None:
        host = f"{host}:{parsed.port}"
    netloc = f"{userinfo}@{host}"
    return urlunsplit((parsed.scheme, netloc, parsed.path, parsed.query, parsed.fragment))


def browser_proxy_hash(config: Config) -> str:
    proxy_url = str(getattr(config, "browser_proxy_url", "") or "").strip()
    if not proxy_url:
        return ""
    return hashlib.sha256(proxy_url.encode("utf-8")).hexdigest()


def browser_launch_args(config: Config, *, bypass_proxy: bool = False) -> list[str]:
    """Return CloakBrowser launch args for the configured browser identity."""
    args = list(BASE_BROWSER_ARGS)
    if bypass_proxy:
        return ["--no-proxy-server", *args]

    proxy_url = str(getattr(config, "browser_proxy_url", "") or "").strip()
    if proxy_url:
        args.append(f"--proxy-server={proxy_url}")
    return args


def build_profile_identity(config: Config, *, publisher: str, institution: str = "") -> dict[str, Any]:
    proxy_url = str(getattr(config, "browser_proxy_url", "") or "").strip()
    return {
        "version": 1,
        "institution": str(institution or "").strip(),
        "browser_proxy_url": mask_secret_url(proxy_url),
        "browser_proxy_url_hash": browser_proxy_hash(config),
        "publishers": [_normalize_publisher(publisher)] if publisher else [],
        "cloakbrowser_version": _cloakbrowser_version(),
    }


def ensure_profile_identity(profile_dir: str | Path, identity: dict[str, Any]) -> dict[str, Any]:
    """Create or update the profile identity manifest without storing secrets."""
    profile_path = Path(profile_dir)
    profile_path.mkdir(parents=True, exist_ok=True)
    manifest_path = profile_path / IDENTITY_MANIFEST_NAME
    existing: dict[str, Any] = {}
    if manifest_path.exists():
        try:
            loaded = json.loads(manifest_path.read_text(encoding="utf-8"))
            if isinstance(loaded, dict):
                existing = loaded
        except (OSError, json.JSONDecodeError):
            existing = {}

    now = datetime.now().isoformat(timespec="seconds")
    publishers = {
        _normalize_publisher(publisher)
        for publisher in existing.get("publishers", [])
        if str(publisher or "").strip()
    }
    publishers.update(
        _normalize_publisher(publisher)
        for publisher in identity.get("publishers", [])
        if str(publisher or "").strip()
    )

    identity_change = _identity_change(existing, identity, now)
    manifest = {
        **existing,
        **identity,
        "publishers": sorted(publishers),
        "created_at": existing.get("created_at") or now,
        "updated_at": now,
    }
    if identity_change:
        manifest["last_identity_change"] = identity_change

    manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    return manifest


def _identity_change(existing: dict[str, Any], identity: dict[str, Any], changed_at: str) -> dict[str, Any] | None:
    if not existing:
        return None
    changed_fields = []
    for field in ("institution", "browser_proxy_url_hash"):
        old = str(existing.get(field, "") or "")
        new = str(identity.get(field, "") or "")
        if old and new and old != new:
            changed_fields.append(field)
    if not changed_fields:
        return None
    return {
        "changed_at": changed_at,
        "fields": changed_fields,
        "previous_browser_proxy_url": existing.get("browser_proxy_url", ""),
        "new_browser_proxy_url": identity.get("browser_proxy_url", ""),
    }


def _cloakbrowser_version() -> str:
    try:
        return importlib.metadata.version("cloakbrowser")
    except importlib.metadata.PackageNotFoundError:
        return "unknown"


def _normalize_publisher(value: str) -> str:
    text = str(value or "").strip().lower()
    return "".join(ch if ch.isalnum() or ch in ("-", "_") else "-" for ch in text)
