"""Diagnostics for the optional OpenCLI Browser Bridge extension."""

from __future__ import annotations

from contextlib import suppress
from dataclasses import asdict, dataclass
import json
from pathlib import Path
import re
from tempfile import TemporaryDirectory
import time
from typing import Any
from urllib.parse import urlencode
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from .browser_identity import browser_extension_paths, browser_launch_args
from .config import Config


OPENCLI_DAEMON_HOST = "localhost"
OPENCLI_DAEMON_PORT = 19825
OPENCLI_EXTENSION_NAME = "OpenCLI"
OPENCLI_REQUIRED_PERMISSIONS = {
    "debugger",
    "tabs",
    "cookies",
    "storage",
    "downloads",
}


@dataclass
class OpenCliExtensionInfo:
    path: str
    exists: bool
    manifest_ok: bool = False
    name: str = ""
    version: str = ""
    description: str = ""
    homepage_url: str = ""
    permissions: list[str] | None = None
    host_permissions: list[str] | None = None
    required_permissions_present: bool = False
    daemon_host: str = OPENCLI_DAEMON_HOST
    daemon_port: int = OPENCLI_DAEMON_PORT
    websocket_url: str = ""
    status_message_supported: bool = False
    command_actions: list[str] | None = None
    error: str = ""

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["permissions"] = data["permissions"] or []
        data["host_permissions"] = data["host_permissions"] or []
        data["command_actions"] = data["command_actions"] or []
        return data


def configured_opencli_extension_infos(config: Config) -> list[dict[str, Any]]:
    """Inspect configured extension directories and identify OpenCLI-like entries."""
    return [
        inspect_opencli_extension_dir(path).to_dict()
        for path in browser_extension_paths(config)
    ]


def inspect_opencli_extension_dir(path: str | Path) -> OpenCliExtensionInfo:
    """Read an unpacked OpenCLI extension directory without executing it."""
    extension_dir = Path(path).expanduser()
    info = OpenCliExtensionInfo(
        path=str(extension_dir),
        exists=extension_dir.is_dir(),
        permissions=[],
        host_permissions=[],
        command_actions=[],
    )
    if not info.exists:
        info.error = "extension directory does not exist"
        return info

    manifest_path = extension_dir / "manifest.json"
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        info.error = f"manifest read failed: {exc}"
        return info

    info.manifest_ok = True
    info.name = str(manifest.get("name", "") or "")
    info.version = str(manifest.get("version", "") or "")
    info.description = str(manifest.get("description", "") or "")
    info.homepage_url = str(manifest.get("homepage_url", "") or "")
    info.permissions = [str(value) for value in manifest.get("permissions", [])]
    info.host_permissions = [str(value) for value in manifest.get("host_permissions", [])]
    info.required_permissions_present = OPENCLI_REQUIRED_PERMISSIONS.issubset(set(info.permissions or []))

    background = (
        manifest.get("background", {}).get("service_worker")
        if isinstance(manifest.get("background"), dict)
        else ""
    )
    background_path = extension_dir / str(background or "")
    if background_path.exists():
        with suppress(OSError):
            source = background_path.read_text(encoding="utf-8", errors="replace")
            info.daemon_host = _regex_const(source, "DAEMON_HOST", OPENCLI_DAEMON_HOST)
            port_text = _regex_const(source, "DAEMON_PORT", str(OPENCLI_DAEMON_PORT))
            with suppress(ValueError):
                info.daemon_port = int(port_text)
            info.websocket_url = f"ws://{info.daemon_host}:{info.daemon_port}/ext"
            info.status_message_supported = 'type === "getStatus"' in source or "getStatus" in source
            actions = sorted(set(re.findall(r'case "([a-z][a-z0-9-]*)"', source)))
            info.command_actions = [
                action
                for action in actions
                if action
                in {
                    "bind",
                    "cdp",
                    "close-window",
                    "cookies",
                    "exec",
                    "frames",
                    "insert-text",
                    "navigate",
                    "network-capture-read",
                    "network-capture-start",
                    "screenshot",
                    "set-file-input",
                    "tabs",
                    "wait-download",
                }
            ]
    return info


def check_opencli_daemon(
    host: str = OPENCLI_DAEMON_HOST,
    port: int = OPENCLI_DAEMON_PORT,
    timeout_sec: float = 2.0,
    context_id: str = "",
) -> dict[str, Any]:
    """Check the local OpenCLI daemon without sending browser commands."""
    base = f"http://{host}:{port}"
    result: dict[str, Any] = {
        "host": host,
        "port": port,
        "requested_context_id": context_id,
        "ping_ok": False,
        "status_ok": False,
        "daemon_version": "",
        "extension_connected": False,
        "extension_version": "",
        "context_id": "",
        "profile_required": False,
        "profile_disconnected": False,
        "profiles": [],
        "error": "",
    }
    ping = _read_json(f"{base}/ping", timeout_sec=timeout_sec)
    if ping.get("ok"):
        result["ping_ok"] = True
    elif ping.get("error"):
        result["error"] = ping["error"]
        return result

    status_url = f"{base}/status"
    if context_id:
        status_url += "?" + urlencode({"contextId": context_id})
    status = _read_json(
        status_url,
        timeout_sec=timeout_sec,
        headers={"X-OpenCLI": "1"},
    )
    if status.get("ok"):
        result.update({
            "status_ok": True,
            "daemon_version": str(status.get("daemonVersion", "") or ""),
            "extension_connected": bool(status.get("extensionConnected")),
            "extension_version": str(status.get("extensionVersion", "") or ""),
            "context_id": str(status.get("contextId", "") or ""),
            "profile_required": bool(status.get("profileRequired")),
            "profile_disconnected": bool(status.get("profileDisconnected")),
            "profiles": status.get("profiles", []) if isinstance(status.get("profiles"), list) else [],
        })
    elif status.get("error") and not result["error"]:
        result["error"] = status["error"]
    return result


def build_opencli_bridge_diagnostics(
    config: Config,
    *,
    runtime_probe: bool = False,
    use_config_profile: bool = False,
    timeout_sec: float = 15.0,
    keep_open: bool = False,
) -> dict[str, Any]:
    """Build a user-facing diagnostic packet for OpenCLI Browser Bridge."""
    extension_infos = configured_opencli_extension_infos(config)
    daemon = check_opencli_daemon(timeout_sec=min(timeout_sec, 5.0))
    diagnostics: dict[str, Any] = {
        "configured_extension_count": len(extension_infos),
        "opencli_configured": any(_looks_like_opencli(info) for info in extension_infos),
        "extensions": extension_infos,
        "daemon": daemon,
        "runtime_probe": None,
        "verdict": "",
    }
    if runtime_probe:
        diagnostics["runtime_probe"] = probe_opencli_extension_runtime(
            config,
            use_config_profile=use_config_profile,
            timeout_sec=timeout_sec,
            keep_open=keep_open,
        )
    diagnostics["verdict"] = _diagnostic_verdict(diagnostics)
    return diagnostics


def probe_opencli_extension_runtime(
    config: Config,
    *,
    use_config_profile: bool = False,
    timeout_sec: float = 15.0,
    keep_open: bool = False,
) -> dict[str, Any]:
    """Launch CloakBrowser and read the OpenCLI popup status page.

    The default uses a temporary profile so diagnostics do not disturb the
    publisher login profile. Pass use_config_profile=True only when the caller
    intentionally wants to inspect the configured profile.
    """
    extension_paths = browser_extension_paths(config)
    result: dict[str, Any] = {
        "launched": False,
        "profile": "config" if use_config_profile else "temporary",
        "extension_service_worker": "",
        "extension_id": "",
        "popup_status_text": "",
        "popup_context_id": "",
        "popup_extension_version": "",
        "popup_daemon_version": "",
        "popup_connected": False,
        "runtime_message_status": {},
        "runtime_message_connected": False,
        "daemon_profile_registered": False,
        "daemon_status_for_context": {},
        "connected": False,
        "error": "",
    }
    if not extension_paths:
        result["error"] = "no browser extensions configured"
        return result

    def run_with_profile(profile_dir: Path) -> None:
        from .cloakbrowser_compat import prepare_cloakbrowser_runtime

        prepare_cloakbrowser_runtime()
        from cloakbrowser import launch_persistent_context

        context = launch_persistent_context(
            user_data_dir=str(profile_dir),
            headless=False,
            humanize=True,
            accept_downloads=True,
            extension_paths=extension_paths,
            args=browser_launch_args(config),
        )
        result["launched"] = True
        try:
            worker = _wait_for_opencli_worker(context, timeout_sec=timeout_sec)
            if not worker:
                result["error"] = "OpenCLI extension service worker not found"
                return
            result["extension_service_worker"] = worker.url
            match = re.match(r"chrome-extension://([^/]+)/", worker.url)
            if not match:
                result["error"] = f"unexpected service worker url: {worker.url}"
                return
            extension_id = match.group(1)
            result["extension_id"] = extension_id
            page = context.new_page()
            page.goto(f"chrome-extension://{extension_id}/popup.html", wait_until="load", timeout=int(timeout_sec * 1000))
            deadline = time.time() + timeout_sec
            while time.time() < deadline:
                result["popup_status_text"] = (page.locator("#status").text_content(timeout=1000) or "").strip()
                result["popup_context_id"] = (page.locator("#contextId").text_content(timeout=1000) or "").strip()
                result["popup_extension_version"] = (page.locator("#extVersion").text_content(timeout=1000) or "").strip()
                with suppress(Exception):
                    result["popup_daemon_version"] = (page.locator("#daemonVersion").text_content(timeout=1000) or "").strip()
                result["popup_connected"] = "Connected to daemon" in result["popup_status_text"]
                with suppress(Exception):
                    runtime_status = page.evaluate(
                        """
                        () => new Promise((resolve) => {
                          try {
                            chrome.runtime.sendMessage({ type: 'getStatus' }, (resp) => {
                              resolve({
                                response: resp || null,
                                lastError: chrome.runtime.lastError ? chrome.runtime.lastError.message : ''
                              });
                            });
                          } catch (err) {
                            resolve({ response: null, error: String(err) });
                          }
                        })
                        """
                    )
                    if isinstance(runtime_status, dict):
                        result["runtime_message_status"] = runtime_status
                        response = runtime_status.get("response")
                        if isinstance(response, dict):
                            result["runtime_message_connected"] = bool(response.get("connected"))
                            result["popup_connected"] = result["popup_connected"] or bool(response.get("connected"))
                            if response.get("contextId"):
                                result["popup_context_id"] = str(response.get("contextId"))
                            if response.get("extensionVersion"):
                                result["popup_extension_version"] = f"v{response.get('extensionVersion')}"
                            if response.get("daemonVersion"):
                                result["popup_daemon_version"] = f"daemon v{response.get('daemonVersion')}"
                            if response.get("connected"):
                                result["popup_status_text"] = "Connected to daemon"
                if result["popup_connected"]:
                    break
                page.wait_for_timeout(500)
            if result["popup_context_id"]:
                daemon_for_context = check_opencli_daemon(
                    timeout_sec=min(3.0, timeout_sec),
                    context_id=result["popup_context_id"],
                )
                result["daemon_status_for_context"] = daemon_for_context
                result["daemon_profile_registered"] = (
                    bool(daemon_for_context.get("extension_connected"))
                    and daemon_for_context.get("context_id") == result["popup_context_id"]
                )
            result["connected"] = bool(result["daemon_profile_registered"])
        except Exception as exc:  # pragma: no cover - exercised by real browser doctor
            result["error"] = str(exc)
        finally:
            if not keep_open:
                with suppress(Exception):
                    context.close()

    if use_config_profile:
        run_with_profile(Path(config.chrome_profile_dir))
    else:
        with TemporaryDirectory(prefix="instsci-opencli-bridge-") as tmp:
            run_with_profile(Path(tmp))
    return result


def _wait_for_opencli_worker(context: Any, *, timeout_sec: float) -> Any:
    deadline = time.time() + timeout_sec
    while time.time() < deadline:
        for worker in context.service_workers:
            if "dist/background.js" in worker.url:
                return worker
        remaining = max(0.1, min(1.0, deadline - time.time()))
        with suppress(Exception):
            worker = context.wait_for_event("serviceworker", timeout=int(remaining * 1000))
            if "dist/background.js" in worker.url:
                return worker
    return None


def _regex_const(source: str, name: str, default: str) -> str:
    match = re.search(rf"const\s+{re.escape(name)}\s*=\s*([\"']?)([^\"';]+)\1\s*;", source)
    return match.group(2) if match else default


def _read_json(url: str, *, timeout_sec: float, headers: dict[str, str] | None = None) -> dict[str, Any]:
    request = Request(url, headers=headers or {})
    try:
        with urlopen(request, timeout=timeout_sec) as response:
            return json.loads(response.read().decode("utf-8"))
    except HTTPError as exc:
        return {"ok": False, "error": f"HTTP {exc.code}"}
    except (OSError, URLError, TimeoutError, json.JSONDecodeError) as exc:
        return {"ok": False, "error": str(exc)}


def _looks_like_opencli(info: dict[str, Any]) -> bool:
    return (
        info.get("name") == OPENCLI_EXTENSION_NAME
        or "opencli" in str(info.get("homepage_url", "")).lower()
        or "opencli" in str(info.get("description", "")).lower()
    )


def _diagnostic_verdict(diagnostics: dict[str, Any]) -> str:
    if not diagnostics["opencli_configured"]:
        return "not_configured"
    daemon = diagnostics.get("daemon", {})
    if not daemon.get("ping_ok"):
        return "extension_configured_daemon_down"
    if not daemon.get("extension_connected"):
        return "extension_configured_daemon_up_not_connected"
    runtime = diagnostics.get("runtime_probe")
    if isinstance(runtime, dict) and runtime.get("launched") and not runtime.get("connected"):
        return "runtime_probe_not_connected"
    return "connected"
