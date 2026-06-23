"""Persistent job records for long-running InstSci browser work."""

from __future__ import annotations

from datetime import datetime
import json
from pathlib import Path
from typing import Any
from uuid import uuid4

from .config import DEFAULT_BASE_DIR
from .session_broker import broker_dir, broker_is_running


JOBS_ROOT = DEFAULT_BASE_DIR / "jobs"

TERMINAL_STATUSES = {"completed", "failed", "canceled", "needs_attention"}


def now_iso() -> str:
    return datetime.now().isoformat(timespec="seconds")


def new_job_id() -> str:
    return f"{datetime.now():%Y%m%d-%H%M%S}-{uuid4().hex[:8]}"


def job_path(job_id: str) -> Path:
    return JOBS_ROOT / f"{job_id}.json"


def save_job(job: dict[str, Any]) -> None:
    job["updated_at"] = now_iso()
    JOBS_ROOT.mkdir(parents=True, exist_ok=True)
    job_path(str(job["id"])).write_text(json.dumps(job, ensure_ascii=False, indent=2), encoding="utf-8")


def load_job(job_id: str) -> dict[str, Any]:
    path = job_path(job_id)
    if not path.exists():
        raise FileNotFoundError(f"Job not found: {job_id}")
    return json.loads(path.read_text(encoding="utf-8"))


def list_jobs(limit: int = 50) -> list[dict[str, Any]]:
    if not JOBS_ROOT.exists():
        return []
    paths = sorted(JOBS_ROOT.glob("*.json"), key=lambda path: path.stat().st_mtime, reverse=True)
    return [load_job(path.stem) for path in paths[: max(1, limit)]]


def create_job(
    *,
    publisher: str,
    broker_publisher: str,
    records: list[dict[str, str]],
    output_dir: str,
    institution: str,
    institution_aliases: list[str],
    browser_profile: str,
    broker_job: dict[str, Any],
    command: str,
    login_timeout: int,
    pdf_timeout: int,
    post_login_hold: int,
    post_run_hold: int,
    carsi_portal_preauth: bool = False,
    parent_job_id: str = "",
) -> dict[str, Any]:
    job = {
        "id": new_job_id(),
        "kind": "publisher_papers",
        "status": "queued",
        "publisher": publisher,
        "broker_publisher": broker_publisher,
        "records": records,
        "record_count": len(records),
        "output_dir": output_dir,
        "institution": institution,
        "institution_aliases": institution_aliases,
        "browser_profile": browser_profile,
        "broker_job_id": broker_job["id"],
        "queue_job_path": broker_job["job_path"],
        "done_path": broker_job["done_path"],
        "command": command,
        "login_timeout": login_timeout,
        "pdf_timeout": pdf_timeout,
        "post_login_hold": post_login_hold,
        "post_run_hold": post_run_hold,
        "carsi_portal_preauth": bool(carsi_portal_preauth),
        "parent_job_id": parent_job_id,
        "created_at": now_iso(),
        "updated_at": now_iso(),
    }
    save_job(job)
    return job


def refresh_job(job: dict[str, Any], *, persist: bool = True) -> dict[str, Any]:
    status = str(job.get("status") or "queued")
    if status == "canceled":
        return job

    done_path = Path(str(job.get("done_path") or ""))
    queue_path = Path(str(job.get("queue_job_path") or ""))
    output_dir = Path(str(job.get("output_dir") or ""))
    summary_path = output_dir / "summary.json"
    partial_path = output_dir / "primary" / "summary_partial.json"
    summary: dict[str, Any] | None = None

    if done_path.exists():
        summary = _read_json(done_path)
    elif summary_path.exists():
        summary = _read_json(summary_path)

    if summary is not None:
        job["summary"] = summary
        job["summary_path"] = str(summary_path if summary_path.exists() else done_path)
        if summary.get("error"):
            job["status"] = "failed"
        elif int(summary.get("missing") or 0) or int(summary.get("unverified") or 0):
            job["status"] = "needs_attention"
        else:
            job["status"] = "completed"
    elif status in TERMINAL_STATUSES:
        job["status"] = status
    elif partial_path.exists():
        job["status"] = "running"
        partial = _read_json(partial_path)
        if isinstance(partial, list):
            job["partial_count"] = len(partial)
    elif queue_path.exists():
        job["status"] = "queued"
        queued = _read_json(queue_path)
        if isinstance(queued, dict) and queued.get("started_at"):
            job["status"] = "running"
    elif broker_is_running(str(job.get("broker_publisher") or job.get("publisher") or "")):
        job["status"] = "running"
    elif status not in TERMINAL_STATUSES:
        job["status"] = "stalled"

    if persist:
        save_job(job)
    return job


def cancel_job(job: dict[str, Any]) -> dict[str, Any]:
    queue_path = Path(str(job.get("queue_job_path") or ""))
    if queue_path.exists():
        queue_path.unlink()
    job["status"] = "canceled"
    save_job(job)
    return job


def retry_records(job: dict[str, Any]) -> list[dict[str, str]]:
    records = list(job.get("records") or [])
    manifest_path = Path(str(job.get("output_dir") or "")) / "complete" / "manifest.json"
    if not manifest_path.exists():
        return records

    manifest = _read_json(manifest_path)
    if not isinstance(manifest, list):
        return records

    retry_dois = {
        str(item.get("doi") or "").lower()
        for item in manifest
        if item.get("status") != "success" or not item.get("verified_match")
    }
    if not retry_dois:
        return []
    return [record for record in records if str(record.get("doi") or "").lower() in retry_dois]


def broker_log_paths(publisher: str) -> list[Path]:
    root = broker_dir(publisher)
    return [root / "broker.out.log", root / "broker.err.log"]


def read_tail(path: Path, *, lines: int = 40) -> list[str]:
    if not path.exists():
        return []
    text = path.read_text(encoding="utf-8", errors="replace")
    return text.splitlines()[-max(1, lines):]


def _read_json(path: Path) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return None
