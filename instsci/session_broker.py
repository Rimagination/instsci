"""Long-lived publisher browser session broker.

The broker keeps one CloakBrowser context alive per publisher/profile and
accepts DOI batch jobs through a small file queue. It intentionally stores no
cookie values in the broker state.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import datetime
import json
import os
from pathlib import Path
import subprocess
import sys
import time
from typing import Any
from uuid import uuid4

from .challenge_assist import build_keepassxc_credential_assist
from .config import DEFAULT_BASE_DIR


BROKER_ROOT = DEFAULT_BASE_DIR / "brokers"

REAUTH_REQUIRED_REASONS = {
    "reauth_required",
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


@dataclass
class BrokerState:
    publisher: str
    profile_dir: str
    pid: int
    queue_dir: str
    started_at: str
    ttl_seconds: int
    institution: str = ""
    heartbeat_at: str = ""
    browser_proxy_url: str = ""
    browser_proxy_url_hash: str = ""
    browser_extension_count: int = 0
    browser_extension_hash: str = ""
    human_assist_url: str = ""
    status: str = "starting"
    active_job_id: str = ""
    active_output_dir: str = ""
    active_record_count: int = 0
    last_job_id: str = ""
    last_job_status: str = ""
    last_summary_path: str = ""
    last_error: str = ""
    paused_job_id: str = ""
    paused_job_path: str = ""
    paused_record_count: int = 0
    keepalive_interval_seconds: int = 0
    last_health_status: str = ""
    last_health_at: str = ""
    last_health_url: str = ""
    last_health_error: str = ""


def broker_key(publisher: str) -> str:
    return "".join(ch if ch.isalnum() or ch in ("-", "_") else "-" for ch in publisher.strip().lower())


def broker_dir(publisher: str) -> Path:
    return BROKER_ROOT / broker_key(publisher)


def broker_state_path(publisher: str) -> Path:
    return broker_dir(publisher) / "state.json"


def broker_stop_path(publisher: str) -> Path:
    return broker_dir(publisher) / "stop"


def broker_paused_dir(publisher: str) -> Path:
    return broker_dir(publisher) / "paused"


def load_broker_state(publisher: str) -> dict[str, Any] | None:
    path = broker_state_path(publisher)
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return None


def write_broker_state(state: BrokerState) -> None:
    path = broker_state_path(state.publisher)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(asdict(state), ensure_ascii=False, indent=2), encoding="utf-8")


def _write_broker_state_payload(publisher: str, state: dict[str, Any]) -> None:
    path = broker_state_path(publisher)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")


def pid_is_running(pid: int) -> bool:
    if pid <= 0:
        return False
    if sys.platform == "win32":
        return _pid_is_running_windows(pid)
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


def _pid_is_running_windows(pid: int) -> bool:
    """Return whether a Windows process is still active without signaling it."""
    try:
        import ctypes
        from ctypes import wintypes
    except ImportError:
        return False

    process_query_limited_information = 0x1000
    still_active = 259

    try:
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
        kernel32.OpenProcess.restype = wintypes.HANDLE
        kernel32.GetExitCodeProcess.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD)]
        kernel32.GetExitCodeProcess.restype = wintypes.BOOL
        kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
        kernel32.CloseHandle.restype = wintypes.BOOL

        handle = kernel32.OpenProcess(process_query_limited_information, False, int(pid))
        if not handle:
            return False
        try:
            exit_code = wintypes.DWORD()
            if not kernel32.GetExitCodeProcess(handle, ctypes.byref(exit_code)):
                return False
            return exit_code.value == still_active
        finally:
            kernel32.CloseHandle(handle)
    except (AttributeError, OSError, ValueError):
        return False


def broker_is_running(publisher: str) -> bool:
    state = load_broker_state(publisher)
    if not state:
        return False
    return pid_is_running(int(state.get("pid") or 0))


def broker_summary_status(summary: dict[str, Any]) -> str:
    """Classify a broker job summary into a persistent broker status."""
    if summary.get("error"):
        return "error"
    if broker_summary_requires_reauth(summary):
        return "reauth_required"
    if _summary_count(summary, "missing") or _summary_count(summary, "unverified"):
        return "attention_required"
    return "complete"


def broker_summary_requires_reauth(summary: dict[str, Any]) -> bool:
    """Return whether a summary needs manual institution/challenge recovery."""
    reasons = _summary_attention_reasons(summary)
    return any(reason in REAUTH_REQUIRED_REASONS for reason in reasons)


def _summary_attention_reasons(summary: dict[str, Any]) -> dict[str, int]:
    reasons: dict[str, int] = {}
    raw = summary.get("attention_reasons")
    if isinstance(raw, dict):
        for key, value in raw.items():
            reason = str(key or "").strip()
            if not reason:
                continue
            try:
                count = int(value or 0)
            except (TypeError, ValueError):
                count = 0
            if count > 0:
                reasons[reason] = reasons.get(reason, 0) + count

    for item in _summary_manifest_items(summary):
        status = str(item.get("status") or "").strip().lower()
        reason = str(item.get("reason") or "").strip()
        if status == "success" or not reason:
            continue
        reasons[reason] = reasons.get(reason, 0) + 1
    return reasons


def _summary_manifest_items(summary: dict[str, Any]) -> list[dict[str, Any]]:
    raw_items = summary.get("manifest_items")
    if isinstance(raw_items, list):
        return [item for item in raw_items if isinstance(item, dict)]

    manifest_path = str(summary.get("manifest") or "")
    if not manifest_path:
        return []
    path = Path(manifest_path)
    json_path = path.with_suffix(".json")
    if not json_path.exists():
        return []
    try:
        items = json.loads(json_path.read_text(encoding="utf-8"))
    except Exception:
        return []
    if not isinstance(items, list):
        return []
    return [item for item in items if isinstance(item, dict)]


def _summary_count(summary: dict[str, Any], key: str) -> int:
    try:
        return max(0, int(summary.get(key) or 0))
    except (TypeError, ValueError):
        return 0


def broker_identity_matches(
    publisher: str,
    *,
    profile_dir: str,
    institution: str,
    browser_proxy_url_hash: str = "",
    browser_extension_hash: str = "",
) -> tuple[bool, str]:
    """Return whether a running broker is safe to reuse for this browser identity."""
    state = load_broker_state(publisher)
    if not state:
        return False, "no broker state"
    if not pid_is_running(int(state.get("pid") or 0)):
        return False, "broker process is not running"

    running_profile = str(state.get("profile_dir") or "")
    requested_profile = str(profile_dir or "")
    if running_profile and requested_profile and not _same_path(running_profile, requested_profile):
        return False, "browser profile differs"

    running_institution = _normalize_identity_text(state.get("institution"))
    requested_institution = _normalize_identity_text(institution)
    if running_institution and requested_institution and running_institution != requested_institution:
        return False, "subscription institution differs"

    running_proxy_hash = str(state.get("browser_proxy_url_hash") or "")
    if running_proxy_hash and browser_proxy_url_hash and running_proxy_hash != browser_proxy_url_hash:
        return False, "browser proxy identity differs"

    running_extension_hash = str(state.get("browser_extension_hash") or "")
    if running_extension_hash and browser_extension_hash and running_extension_hash != browser_extension_hash:
        return False, "browser extension set differs"

    return True, ""


def _normalize_identity_text(value: Any) -> str:
    return " ".join(str(value or "").strip().casefold().split())


def _same_path(left: str, right: str) -> bool:
    try:
        return Path(left).expanduser().resolve() == Path(right).expanduser().resolve()
    except (OSError, RuntimeError, ValueError):
        return os.path.normcase(os.path.abspath(left)) == os.path.normcase(os.path.abspath(right))


def start_broker_process(
    *,
    publisher: str,
    browser_profile: str,
    institution: str,
    ttl_seconds: int,
    cwd: str | Path,
    human_assist: bool = False,
    human_assist_host: str = "127.0.0.1",
    human_assist_port: int = 0,
) -> subprocess.Popen[Any]:
    root = broker_dir(publisher)
    root.mkdir(parents=True, exist_ok=True)
    broker_stop_path(publisher).unlink(missing_ok=True)
    stdout = root / "broker.out.log"
    stderr = root / "broker.err.log"
    args = [
        sys.executable,
        "-m",
        "instsci.cli",
        "session-broker-run",
        "--publisher",
        publisher,
        "--browser-profile",
        browser_profile,
        "--institution",
        institution,
        "--ttl",
        str(ttl_seconds),
    ]
    if human_assist:
        args.extend([
            "--human-assist",
            "--human-assist-host",
            human_assist_host,
            "--human-assist-port",
            str(human_assist_port),
        ])
    return subprocess.Popen(
        args,
        cwd=str(cwd),
        stdout=stdout.open("a", encoding="utf-8"),
        stderr=stderr.open("a", encoding="utf-8"),
        stdin=subprocess.DEVNULL,
    )


def pause_broker_job(publisher: str, job: dict[str, Any], summary: dict[str, Any]) -> dict[str, Any]:
    """Persist the remaining DOI records for a later same-browser resume."""
    job_id = str(job.get("id") or uuid4().hex)
    remaining_records = _remaining_records_for_summary(job, summary)
    paused_dir = broker_paused_dir(publisher)
    paused_dir.mkdir(parents=True, exist_ok=True)
    paused_path = paused_dir / f"{job_id}.json"
    payload = dict(job)
    payload["records"] = remaining_records
    payload["resume_source_job_id"] = job_id
    payload["pause_reason"] = broker_summary_status(summary)
    payload["paused_at"] = datetime.now().isoformat(timespec="seconds")
    payload["paused_summary"] = {
        "count": summary.get("count", 0),
        "success": summary.get("success", 0),
        "missing": summary.get("missing", 0),
        "unverified": summary.get("unverified", 0),
        "attention_reasons": summary.get("attention_reasons", {}),
        "manifest": summary.get("manifest", ""),
    }
    paused_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    return {
        "job_id": job_id,
        "path": str(paused_path),
        "record_count": len(remaining_records),
    }


def list_paused_jobs(publisher: str) -> list[dict[str, Any]]:
    """Return non-secret metadata for paused broker jobs, newest first."""
    paused_dir = broker_paused_dir(publisher)
    if not paused_dir.exists():
        return []
    jobs: list[dict[str, Any]] = []
    for path in paused_dir.glob("*.json"):
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            continue
        if not isinstance(payload, dict):
            continue
        records = payload.get("records")
        record_count = len(records) if isinstance(records, list) else 0
        jobs.append({
            "job_id": str(payload.get("resume_source_job_id") or payload.get("id") or path.stem),
            "path": str(path),
            "record_count": record_count,
            "paused_at": str(payload.get("paused_at") or ""),
            "pause_reason": str(payload.get("pause_reason") or ""),
        })
    return sorted(
        jobs,
        key=lambda item: (str(item.get("paused_at") or ""), str(item.get("job_id") or "")),
        reverse=True,
    )


def list_queued_jobs(publisher: str) -> list[dict[str, Any]]:
    """Return non-secret metadata for queued broker jobs, oldest first."""
    state = load_broker_state(publisher)
    queue_dir = Path(str(state.get("queue_dir") or "")) if state else broker_dir(publisher) / "queue"
    if not queue_dir.exists():
        return []
    jobs: list[dict[str, Any]] = []
    for path in queue_dir.glob("*.json"):
        if path.name.endswith(".done.json"):
            continue
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            continue
        if not isinstance(payload, dict):
            continue
        records = payload.get("records")
        record_count = len(records) if isinstance(records, list) else 0
        jobs.append({
            "job_id": str(payload.get("id") or path.stem),
            "path": str(path),
            "record_count": record_count,
            "created_at": str(payload.get("created_at") or ""),
            "output_dir": str(payload.get("output_dir") or ""),
        })
    return sorted(
        jobs,
        key=lambda item: (str(item.get("created_at") or ""), str(item.get("job_id") or "")),
    )


def _remaining_records_for_summary(job: dict[str, Any], summary: dict[str, Any]) -> list[dict[str, Any]]:
    records = [record for record in job.get("records", []) if isinstance(record, dict)]
    if not records:
        return []
    manifest_items = _summary_manifest_items(summary)
    if not manifest_items:
        return records
    retry_dois = {
        str(item.get("doi") or "").strip().lower()
        for item in manifest_items
        if str(item.get("status") or "").strip().lower() != "success"
    }
    retry_dois.discard("")
    if not retry_dois:
        return []
    return [
        record
        for record in records
        if str(record.get("doi") or "").strip().lower() in retry_dois
    ]


def _select_paused_job(publisher: str, job_id: str = "") -> tuple[Path, dict[str, Any]]:
    paused_dir = broker_paused_dir(publisher)
    if job_id:
        candidates = [paused_dir / f"{job_id}.json"]
    else:
        candidates = sorted(paused_dir.glob("*.json"), key=lambda path: path.stat().st_mtime, reverse=True)
    for path in candidates:
        if not path.exists():
            continue
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            continue
        if isinstance(payload, dict):
            return path, payload
    raise RuntimeError(f"No paused broker job found for {publisher}")


def resume_broker_job(
    *,
    publisher: str,
    job_id: str = "",
    timeout_seconds: int,
) -> dict[str, Any]:
    """Requeue a paused broker job and wait for its summary."""
    state = load_broker_state(publisher)
    if not state:
        raise RuntimeError(f"No broker state for {publisher}")
    if not broker_is_running(publisher):
        raise RuntimeError(f"Broker for {publisher} is not running")

    paused_path, payload = _select_paused_job(publisher, job_id)
    queue_dir = Path(str(state["queue_dir"]))
    queue_dir.mkdir(parents=True, exist_ok=True)
    original_id = str(payload.get("resume_source_job_id") or payload.get("id") or paused_path.stem)
    resumed_id = uuid4().hex
    payload["id"] = resumed_id
    payload["resume_source_job_id"] = original_id
    payload["resumed_from_paused_job_id"] = paused_path.stem
    payload["resumed_at"] = datetime.now().isoformat(timespec="seconds")
    payload["skip_attempted"] = False
    job_path = queue_dir / f"{resumed_id}.json"
    done_path = queue_dir / f"{resumed_id}.done.json"
    job_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    paused_path.unlink(missing_ok=True)

    deadline = time.time() + max(1, timeout_seconds)
    while time.time() < deadline:
        if done_path.exists():
            return json.loads(done_path.read_text(encoding="utf-8"))
        if not broker_is_running(publisher):
            raise RuntimeError(f"Broker for {publisher} stopped before resumed job completed")
        time.sleep(2)
    raise TimeoutError(f"Broker resume timed out after {timeout_seconds}s: {resumed_id}")


def resume_all_broker_jobs(
    *,
    publisher: str,
    timeout_seconds: int,
) -> dict[str, Any]:
    """Resume every paused broker job for a publisher, oldest first."""
    paused_jobs = list_paused_jobs(publisher)
    if not paused_jobs:
        raise RuntimeError(f"No paused broker job found for {publisher}")
    summaries: list[dict[str, Any]] = []
    for paused in sorted(paused_jobs, key=lambda item: str(item.get("paused_at") or "")):
        summaries.append(
            resume_broker_job(
                publisher=publisher,
                job_id=str(paused.get("job_id") or ""),
                timeout_seconds=timeout_seconds,
            )
        )
    return _aggregate_resume_summaries(publisher, summaries)


def _aggregate_resume_summaries(publisher: str, summaries: list[dict[str, Any]]) -> dict[str, Any]:
    total = {
        "count": 0,
        "success": 0,
        "missing": 0,
        "unverified": 0,
        "verified_match": 0,
        "publisher": publisher,
        "broker": True,
        "resumed_job_count": len(summaries),
        "broker_status": "complete",
        "summaries": summaries,
    }
    for summary in summaries:
        total["count"] += _summary_count(summary, "count")
        total["success"] += _summary_count(summary, "success")
        total["missing"] += _summary_count(summary, "missing")
        total["unverified"] += _summary_count(summary, "unverified")
        total["verified_match"] += _summary_count(summary, "verified_match")
        status = str(summary.get("broker_status") or "") or broker_summary_status(summary)
        if status == "reauth_required":
            total["broker_status"] = "reauth_required"
        elif status != "complete" and total["broker_status"] == "complete":
            total["broker_status"] = status
    return total


def submit_broker_job(
    *,
    publisher: str,
    records: list[dict[str, str]],
    output_dir: str,
    institution: str,
    login_timeout: int,
    pdf_timeout: int,
    post_login_hold: int,
    post_run_hold: int,
    timeout_seconds: int,
    retry_failed: bool = True,
    target_verified: int | None = None,
    attempt_cache: str = "",
    skip_attempted: bool = False,
) -> dict[str, Any]:
    state = load_broker_state(publisher)
    if not state:
        raise RuntimeError(f"No broker state for {publisher}")
    if not pid_is_running(int(state.get("pid") or 0)):
        raise RuntimeError(f"Broker for {publisher} is not running")
    job_id = uuid4().hex
    job = {
        "id": job_id,
        "publisher": publisher,
        "records": records,
        "output_dir": str(Path(output_dir).resolve()),
        "institution": institution,
        "login_timeout": login_timeout,
        "pdf_timeout": pdf_timeout,
        "post_login_hold": post_login_hold,
        "post_run_hold": post_run_hold,
        "retry_failed": retry_failed,
        "target_verified": target_verified or 0,
        "attempt_cache": str(Path(attempt_cache).resolve()) if attempt_cache else "",
        "skip_attempted": skip_attempted,
        "created_at": datetime.now().isoformat(timespec="seconds"),
    }
    if _broker_state_requires_reauth_before_submit(state):
        return _pause_submitted_job_for_reauth(publisher, state, job)

    queue_dir = Path(str(state["queue_dir"]))
    queue_dir.mkdir(parents=True, exist_ok=True)
    job_path = queue_dir / f"{job_id}.json"
    done_path = queue_dir / f"{job_id}.done.json"
    job_path.write_text(json.dumps(job, ensure_ascii=False, indent=2), encoding="utf-8")
    deadline = time.time() + max(1, timeout_seconds)
    while time.time() < deadline:
        if done_path.exists():
            return json.loads(done_path.read_text(encoding="utf-8"))
        if not broker_is_running(publisher):
            raise RuntimeError(f"Broker for {publisher} stopped before job completed")
        time.sleep(2)
    raise TimeoutError(f"Broker job timed out after {timeout_seconds}s: {job_id}")


def _broker_state_requires_reauth_before_submit(state: dict[str, Any]) -> bool:
    status = str(state.get("status") or "").strip().lower()
    last_health = str(state.get("last_health_status") or "").strip().lower()
    if status == "processing":
        return False
    return status == "reauth_required" or last_health == "reauth_required"


def _pause_submitted_job_for_reauth(
    publisher: str,
    state: dict[str, Any],
    job: dict[str, Any],
) -> dict[str, Any]:
    records = [record for record in job.get("records", []) if isinstance(record, dict)]
    reason = str(state.get("last_health_error") or "").strip() or "reauth_required"
    summary = {
        "count": len(records),
        "success": 0,
        "missing": len(records),
        "unverified": 0,
        "verified_match": 0,
        "publisher": publisher,
        "broker": True,
        "broker_status": "reauth_required",
        "attention_reasons": {reason: len(records)} if records else {reason: 1},
        "manifest_items": [
            {
                "doi": str(record.get("doi") or ""),
                "title": str(record.get("title") or ""),
                "status": "missing",
                "reason": reason,
            }
            for record in records
        ],
        "human_assist_url": str(state.get("human_assist_url") or ""),
    }
    paused = pause_broker_job(publisher, job, summary)
    summary["paused_job_id"] = paused["job_id"]
    summary["paused_job_path"] = paused["path"]
    summary["paused_record_count"] = paused["record_count"]
    summary["resume_command"] = f"instsci session-broker-resume -p {publisher} --job-id {paused['job_id']}"
    summary["credential_assist"] = build_keepassxc_credential_assist()

    run_dir = Path(str(job.get("output_dir") or "")).resolve()
    run_dir.mkdir(parents=True, exist_ok=True)
    summary_path = run_dir / "summary.json"
    summary["summary"] = str(summary_path)
    summary_path.write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")

    state["status"] = "reauth_required"
    state["paused_job_id"] = paused["job_id"]
    state["paused_job_path"] = paused["path"]
    state["paused_record_count"] = paused["record_count"]
    state["last_job_id"] = str(job.get("id") or "")
    state["last_job_status"] = "reauth_required"
    state["last_summary_path"] = str(summary_path)
    state["last_error"] = (
        "Publisher session needs manual re-authentication; new work was saved "
        "as a paused broker job instead of running in a stale login state."
    )
    state["heartbeat_at"] = datetime.now().isoformat(timespec="seconds")
    _write_broker_state_payload(publisher, state)
    _write_external_human_assist_state(publisher, state, summary)
    return summary


def _write_external_human_assist_state(
    publisher: str,
    state: dict[str, Any],
    summary: dict[str, Any],
) -> None:
    assist_dir = broker_dir(publisher) / "human_assist"
    assist_dir.mkdir(parents=True, exist_ok=True)
    payload = {
        "status": "reauth_required",
        "publisher": publisher,
        "action": (
            "Complete the visible CloakBrowser institution or verification prompt, "
            "leave the browser open, then run the resume command."
        ),
        "resume_command": str(summary.get("resume_command") or ""),
        "paused_job_id": str(summary.get("paused_job_id") or ""),
        "paused_record_count": int(summary.get("paused_record_count") or 0),
        "diagnostic_path": str(summary.get("summary") or ""),
        "credential_warning": True,
        "credential_assist": build_keepassxc_credential_assist(),
        "assist_url": str(state.get("human_assist_url") or ""),
        "updated_at": datetime.now().isoformat(timespec="seconds"),
    }
    (assist_dir / "assist_state.json").write_text(
        json.dumps(payload, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
