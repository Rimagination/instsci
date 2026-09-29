"""Profile-driven CloakBrowser batch downloader with retryable machine states."""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor, as_completed
from collections import Counter
import csv
import hashlib
import inspect
import json
import math
import mimetypes
import re
import shutil
import tempfile
import threading
import time
from dataclasses import asdict, dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlencode, urljoin, urlparse

import requests

from .config import Config
from .desktop_alerts import show_desktop_alert
from .extractors import pdf_extractor
from .institution_identity import (
    institution_aliases as build_institution_aliases,
    institution_result_selectors,
)
from .pdf_bytes import MIN_PDF_BYTES, is_plausible_pdf_bytes
from .publisher_pdf_router import (
    belongs_to_current_article,
    build_pdf_candidates,
    extract_elsevier_pii,
    is_pdf_candidate_url,
    is_supplementary_url,
)
from .publisher_profiles import ACS_PROFILE, PublisherProfile
from .retry_policy import (
    BROWSER_RETRY_COOLDOWN_SEC, BROWSER_RETRY_INTERVAL_SEC,
    TRANSIENT_REASONS, failure_category,
)

EST_ISSN = "1520-5851"
MAX_BROWSER_CONCURRENCY = 4
PDF_URL_RE = re.compile(r"https?://[^\s\"'<>]+", re.IGNORECASE)
CARSI_PORTAL_LOGIN_URL = "https://ds.carsi.edu.cn/login/index.html"
CHALLENGE_QUIET_WAIT_SEC = 30.0

NON_ARTICLE_PDF_MARKERS = (
    "plain language summary",
    "p l a i n l a n g u a g e s u m m a r y",
    "electronic supporting information",
    "we are delighted to inform you that your manuscript",
    "department of health and human services food and drug administration",
    "new drug application",
    # Publisher supplementary-material cover sheets must never verify as the
    # article full text — Springer ESM literally opens with this line (#15)
    "supplementary material for",
    "supplementary materials for",
    "supplementary information for",
    "supplementary material:",
    "moesm",
    "mediaobjects",
)

RETRYABLE_REASONS = TRANSIENT_REASONS

# Controls matching this pattern sell access — clicking them as a "PDF
# entry" either purchases nothing or dead-ends on a paywall 404 (#13).
_PURCHASE_CONTROL_RE = re.compile(
    r"buy\b|purchase|add[- ]to[- ]cart|shopping[- ]cart|checkout"
    r"|(?:usd|eur|gbp|cny|rmb)\s?\d|\$\s?\d|￥\s?\d|¥\s?\d",
    re.IGNORECASE,
)


def _is_purchase_control(text: str, href: str) -> bool:
    return bool(_PURCHASE_CONTROL_RE.search(str(text or ""))) or bool(
        _PURCHASE_CONTROL_RE.search(str(href or ""))
    )


@dataclass
class PaperRecord:
    doi: str
    title: str = ""
    published: str = ""
    url: str = ""


@dataclass
class DownloadResult:
    doi: str
    status: str
    reason: str = ""
    state: str = ""
    article_url: str = ""
    final_url: str = ""
    title: str = ""
    pdf_url: str = ""
    pdf_path: str = ""
    text_length: int = 0
    size_bytes: int = 0
    verified_match: bool = False
    diagnostic_path: str = ""
    supplementary_paths: list[str] = field(default_factory=list)
    supplementary_urls: list[str] = field(default_factory=list)
    events: list[dict[str, str]] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return self.status == "success"


def safe_name(doi: str) -> str:
    return re.sub(r"[^\w\-.]", "_", doi)


def fetch_est_records(
    *,
    year: int,
    limit: int,
    email: str = "",
    session: requests.Session | None = None,
) -> list[PaperRecord]:
    """Fetch EST records from Crossref in newest-first publication order."""
    params = {
        "filter": (
            f"issn:{EST_ISSN},type:journal-article,"
            f"from-pub-date:{year}-01-01,until-pub-date:{year}-12-31"
        ),
        "sort": "published",
        "order": "desc",
        "rows": str(limit),
    }
    if email:
        params["mailto"] = email
    url = "https://api.crossref.org/works?" + urlencode(params)
    sess = session or requests.Session()
    resp = sess.get(
        url,
        timeout=30,
        headers={"User-Agent": f"instsci/0.1 ({email or 'local'})"},
    )
    resp.raise_for_status()
    records: list[PaperRecord] = []
    for item in resp.json().get("message", {}).get("items", []):
        doi = str(item.get("DOI", "")).strip()
        if not doi:
            continue
        title = " ".join(item.get("title") or [])
        published = item.get("published-print") or item.get("published-online") or item.get("published") or {}
        date_parts = published.get("date-parts") or [[]]
        published_text = ""
        if date_parts and date_parts[0]:
            published_text = "-".join(
                f"{part:02d}" if idx else str(part)
                for idx, part in enumerate(date_parts[0])
            )
        records.append(
            PaperRecord(
                doi=doi,
                title=re.sub(r"\s+", " ", title).strip(),
                published=published_text,
                url=str(item.get("URL", "")),
            )
        )
    return records[:limit]


class PublisherBatchDownloader:
    """Deterministic publisher workflow with diagnostic packets for surprises."""

    def __init__(
        self,
        config: Config | None = None,
        *,
        profile: PublisherProfile = ACS_PROFILE,
        institution_query: str = "",
        login_timeout_sec: int = 900,
        pdf_timeout_sec: int = 60,
        post_login_hold_sec: int = 0,
        post_run_hold_sec: int = 0,
        carsi_portal_preauth: bool = False,
        institution_aliases: tuple[str, ...] = (),
        include_supplementary: bool = False,
        supplementary_only: bool = False,
        supplementary_fast: bool = False,
    ) -> None:
        self.config = config or Config.load()
        self.profile = profile
        self.institution_query = institution_query.strip()
        config_aliases = (
            self.config.carsi_idp_name,
            self.config.institution_name_en,
            self.config.institution_name_zh,
            self.config.school,
        )
        self.institution_aliases = build_institution_aliases(
            self.institution_query,
            (*institution_aliases, *config_aliases),
        )
        self.login_timeout_sec = login_timeout_sec
        self.pdf_timeout_ms = max(1, pdf_timeout_sec) * 1_000
        self.post_login_hold_sec = max(0, int(post_login_hold_sec or 0))
        self.post_run_hold_sec = max(0, int(post_run_hold_sec or 0))
        self.carsi_portal_preauth = bool(carsi_portal_preauth)
        self.supplementary_only = bool(supplementary_only)
        self.supplementary_fast = bool(supplementary_fast)
        self.include_supplementary = bool(include_supplementary or self.supplementary_only)
        self._carsi_portal_preauth_attempted = False
        self._carsi_portal_preauth_done = False
        self._carsi_portal_preauth_events: list[dict[str, str]] = []
        self._manual_challenge_alert_last_at = 0.0

    def run_records(
        self,
        records: list[PaperRecord],
        run_dir: str | Path,
        *,
        retry_failed: bool = True,
        target_verified: int | None = None,
        attempt_cache: str | Path | None = None,
        skip_attempted: bool = False,
        concurrency: int = 1,
    ) -> dict[str, Any]:
        """Download all records and write summary/manifest artifacts."""
        run_path = Path(run_dir)
        run_path.mkdir(parents=True, exist_ok=True)
        target = target_verified if target_verified and target_verified > 0 else None
        worker_count = min(max(1, int(concurrency or 1)), MAX_BROWSER_CONCURRENCY)
        if self.carsi_portal_preauth:
            worker_count = 1
        if target:
            worker_count = 1
        attempt_cache_path = Path(attempt_cache) if attempt_cache else run_path / "attempts.jsonl"
        (run_path / "records.json").write_text(
            json.dumps([asdict(r) for r in records], ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        (run_path / "dois.txt").write_text(
            "\n".join(r.doi for r in records) + "\n",
            encoding="utf-8",
        )

        missing_reasons: dict[str, str] = {}
        records_to_run = records
        cached_skipped = 0
        if skip_attempted:
            attempted_dois = self._read_attempted_dois(attempt_cache_path)
            records_to_run = []
            for record in records:
                if record.doi.lower() in attempted_dois:
                    cached_skipped += 1
                    missing_reasons[record.doi.lower()] = "skipped_cached_attempt"
                else:
                    records_to_run.append(record)

        results = self._run_once(
            records_to_run,
            run_path / "primary",
            target_verified=target,
            attempt_cache_path=attempt_cache_path,
            phase="primary",
            concurrency=worker_count,
        )
        primary_counts = self._count_results(results)
        target_reached = bool(target and self._count_verified(results) >= target)
        if target_reached and len(results) < len(records_to_run):
            for record in records_to_run[len(results):]:
                missing_reasons[record.doi.lower()] = "target_verified_reached"

        failed_records = self._retry_candidates(records_to_run, results)

        retry_results: list[DownloadResult] = []
        if retry_failed and failed_records and not target_reached:
            remaining_target = target - self._count_verified(results) if target else None
            time.sleep(BROWSER_RETRY_COOLDOWN_SEC)
            retry_results = self._run_once(
                failed_records,
                run_path / "retry",
                target_verified=remaining_target,
                attempt_cache_path=attempt_cache_path,
                phase="retry",
                concurrency=1,
            )
            retry_by_doi = {result.doi.lower(): result for result in retry_results}
            results = [
                retry_by_doi.get(result.doi.lower(), result)
                for result in results
            ]
            target_reached = bool(target and self._count_verified(results) >= target)

        summary = self._write_complete_artifacts(records, results, run_path, missing_reasons=missing_reasons)
        summary["publisher"] = self.profile.name
        summary["primary"] = primary_counts
        summary["final"] = self._count_results(results)
        summary.update(self._retry_summary(results, retry_results, len(failed_records)))
        summary["target_verified"] = target
        summary["target_reached"] = target_reached
        summary["skipped"] = max(0, len(records) - len(results) - cached_skipped)
        summary["cached_skipped"] = cached_skipped
        summary["carsi_portal_preauth"] = self.carsi_portal_preauth
        summary["carsi_portal_preauth_attempted"] = self._carsi_portal_preauth_attempted
        summary["carsi_portal_preauth_done"] = self._carsi_portal_preauth_done
        summary["attempt_cache"] = str(attempt_cache_path)
        summary["concurrency"] = worker_count
        summary["include_supplementary"] = self.include_supplementary
        summary["supplementary_only"] = self.supplementary_only
        summary["supplementary_fast"] = self.supplementary_fast
        summary["browser_profile_dir"] = str(Path(self.config.chrome_profile_dir))
        (run_path / "summary.json").write_text(
            json.dumps(summary, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        return summary

    def run_records_with_context(
        self,
        context: Any,
        records: list[PaperRecord],
        run_dir: str | Path,
        *,
        retry_failed: bool = True,
        target_verified: int | None = None,
        attempt_cache: str | Path | None = None,
        skip_attempted: bool = False,
    ) -> dict[str, Any]:
        """Download records through an already-open CloakBrowser context."""
        run_path = Path(run_dir)
        run_path.mkdir(parents=True, exist_ok=True)
        target = target_verified if target_verified and target_verified > 0 else None
        attempt_cache_path = Path(attempt_cache) if attempt_cache else run_path / "attempts.jsonl"
        (run_path / "records.json").write_text(
            json.dumps([asdict(r) for r in records], ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        (run_path / "dois.txt").write_text(
            "\n".join(r.doi for r in records) + "\n",
            encoding="utf-8",
        )

        self._preauthenticate_carsi_portal(context)

        missing_reasons: dict[str, str] = {}
        records_to_run = records
        cached_skipped = 0
        if skip_attempted:
            attempted_dois = self._read_attempted_dois(attempt_cache_path)
            records_to_run = []
            for record in records:
                if record.doi.lower() in attempted_dois:
                    cached_skipped += 1
                    missing_reasons[record.doi.lower()] = "skipped_cached_attempt"
                else:
                    records_to_run.append(record)

        results = self._run_once_in_context(
            context,
            records_to_run,
            run_path / "primary",
            target_verified=target,
            attempt_cache_path=attempt_cache_path,
            phase="primary",
        )
        primary_counts = self._count_results(results)
        target_reached = bool(target and self._count_verified(results) >= target)
        if target_reached and len(results) < len(records_to_run):
            for record in records_to_run[len(results):]:
                missing_reasons[record.doi.lower()] = "target_verified_reached"

        failed_records = self._retry_candidates(records_to_run, results)

        retry_results: list[DownloadResult] = []
        if retry_failed and failed_records and not target_reached:
            remaining_target = target - self._count_verified(results) if target else None
            time.sleep(BROWSER_RETRY_COOLDOWN_SEC)
            retry_results = self._run_once_in_context(
                context,
                failed_records,
                run_path / "retry",
                target_verified=remaining_target,
                attempt_cache_path=attempt_cache_path,
                phase="retry",
            )
            retry_by_doi = {result.doi.lower(): result for result in retry_results}
            results = [
                retry_by_doi.get(result.doi.lower(), result)
                for result in results
            ]
            target_reached = bool(target and self._count_verified(results) >= target)

        summary = self._write_complete_artifacts(records, results, run_path, missing_reasons=missing_reasons)
        summary["publisher"] = self.profile.name
        summary["primary"] = primary_counts
        summary["final"] = self._count_results(results)
        summary.update(self._retry_summary(results, retry_results, len(failed_records)))
        summary["target_verified"] = target
        summary["target_reached"] = target_reached
        summary["skipped"] = max(0, len(records) - len(results) - cached_skipped)
        summary["cached_skipped"] = cached_skipped
        summary["carsi_portal_preauth"] = self.carsi_portal_preauth
        summary["carsi_portal_preauth_attempted"] = self._carsi_portal_preauth_attempted
        summary["carsi_portal_preauth_done"] = self._carsi_portal_preauth_done
        summary["attempt_cache"] = str(attempt_cache_path)
        summary["concurrency"] = 1
        summary["include_supplementary"] = self.include_supplementary
        summary["supplementary_only"] = self.supplementary_only
        summary["supplementary_fast"] = self.supplementary_fast
        summary["browser_profile_dir"] = str(Path(self.config.chrome_profile_dir))
        (run_path / "summary.json").write_text(
            json.dumps(summary, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        return summary

    @staticmethod
    def _retry_candidates(records: list[PaperRecord], results: list[DownloadResult]) -> list[PaperRecord]:
        eligible = {
            result.doi.lower() for result in results
            if result.status == "failed" and failure_category(result.reason, result.state) == "transient"
        }
        return [record for record in records if record.doi.lower() in eligible]

    @staticmethod
    def _retry_summary(results: list[DownloadResult], retries: list[DownloadResult], eligible: int) -> dict:
        return {
            "retry_eligible": eligible,
            "retry_attempted": len(retries),
            "retry_success": sum(1 for result in retries if result.ok),
            "failure_categories": dict(Counter(
                failure_category(result.reason, result.state)
                for result in results if result.status == "failed"
            )),
        }

    def _run_once(
        self,
        records: list[PaperRecord],
        run_dir: Path,
        *,
        target_verified: int | None = None,
        attempt_cache_path: Path | None = None,
        phase: str = "primary",
        concurrency: int = 1,
    ) -> list[DownloadResult]:
        run_dir.mkdir(parents=True, exist_ok=True)
        worker_count = min(max(1, int(concurrency or 1)), len(records) or 1)
        if worker_count > 1 and not target_verified:
            return self._run_once_parallel(
                records,
                run_dir,
                worker_count=worker_count,
                attempt_cache_path=attempt_cache_path,
                phase=phase,
            )

        results: list[DownloadResult] = []
        verified_count = 0

        context = self._launch_context()
        try:
            self._preauthenticate_carsi_portal(context)
            for record in records:
                if phase == "retry" and results:
                    time.sleep(BROWSER_RETRY_INTERVAL_SEC)
                result = self.fetch_one(context, record, run_dir)
                results.append(result)
                if result.ok and result.verified_match:
                    verified_count += 1
                self._append_attempt(attempt_cache_path, result, phase)
                self._write_results(run_dir / "summary_partial.json", results)
                if target_verified and verified_count >= target_verified:
                    break
        finally:
            try:
                context.close()
            except Exception:
                pass

        self._write_results(run_dir / "summary.json", results)
        return results

    def _run_once_in_context(
        self,
        context: Any,
        records: list[PaperRecord],
        run_dir: Path,
        *,
        target_verified: int | None = None,
        attempt_cache_path: Path | None = None,
        phase: str = "primary",
    ) -> list[DownloadResult]:
        run_dir.mkdir(parents=True, exist_ok=True)
        results: list[DownloadResult] = []
        verified_count = 0
        for record in records:
            if phase == "retry" and results:
                time.sleep(BROWSER_RETRY_INTERVAL_SEC)
            result = self.fetch_one(context, record, run_dir)
            results.append(result)
            if result.ok and result.verified_match:
                verified_count += 1
            self._append_attempt(attempt_cache_path, result, phase)
            self._write_results(run_dir / "summary_partial.json", results)
            if target_verified and verified_count >= target_verified:
                break

        self._write_results(run_dir / "summary.json", results)
        return results

    def _run_once_parallel(
        self,
        records: list[PaperRecord],
        run_dir: Path,
        *,
        worker_count: int,
        attempt_cache_path: Path | None = None,
        phase: str = "primary",
    ) -> list[DownloadResult]:
        run_dir.mkdir(parents=True, exist_ok=True)
        indexed_records = list(enumerate(records))
        chunks = [indexed_records[index::worker_count] for index in range(worker_count)]
        profile_root = run_dir / "worker-profiles"
        source_profile = Path(self.config.chrome_profile_dir)
        worker_profiles = [
            self._prepare_worker_profile(source_profile, profile_root / f"{phase}-{index + 1}")
            for index in range(worker_count)
        ]
        results_by_index: dict[int, DownloadResult] = {}
        results_lock = threading.Lock()
        attempt_lock = threading.Lock()

        def record_result(index: int, result: DownloadResult) -> None:
            with results_lock:
                results_by_index[index] = result
                partial = [results_by_index[item_index] for item_index in sorted(results_by_index)]
                self._write_results(run_dir / "summary_partial.json", partial)
            with attempt_lock:
                self._append_attempt(attempt_cache_path, result, phase)

        def run_worker(worker_index: int, items: list[tuple[int, PaperRecord]]) -> None:
            if not items:
                return
            context = self._launch_context(profile_dir=worker_profiles[worker_index])
            try:
                for item_index, record in items:
                    record_result(item_index, self.fetch_one(context, record, run_dir))
            finally:
                try:
                    context.close()
                except Exception:
                    pass

        with ThreadPoolExecutor(max_workers=worker_count) as executor:
            futures = [
                executor.submit(run_worker, worker_index, chunk)
                for worker_index, chunk in enumerate(chunks)
                if chunk
            ]
            for future in as_completed(futures):
                future.result()

        results = [results_by_index[index] for index in range(len(records)) if index in results_by_index]
        self._write_results(run_dir / "summary.json", results)
        return results

    def _prepare_worker_profile(self, source: Path, target: Path) -> Path:
        if target.exists():
            shutil.rmtree(target, ignore_errors=True)
        target.parent.mkdir(parents=True, exist_ok=True)
        if not source.exists():
            target.mkdir(parents=True, exist_ok=True)
            return target
        ignored_names = {
            "SingletonCookie",
            "SingletonLock",
            "SingletonSocket",
            "BrowserMetrics",
            "Cache",
            "Code Cache",
            "Crashpad",
            "DawnCache",
            "GPUCache",
            "GrShaderCache",
            "ShaderCache",
        }

        def ignore(_directory: str, names: list[str]) -> set[str]:
            return {name for name in names if name in ignored_names}

        try:
            shutil.copytree(source, target, ignore=ignore)
        except Exception:
            target.mkdir(parents=True, exist_ok=True)
        return target

    def _launch_context(self, profile_dir: str | Path | None = None):
        from .cloakbrowser_compat import (
            BrowserPythonUnsupportedError,
            ensure_browser_python_supported,
            prepare_cloakbrowser_runtime,
        )

        prepare_cloakbrowser_runtime()
        try:
            ensure_browser_python_supported()
        except BrowserPythonUnsupportedError as exc:
            logger.error("Browser workflow unavailable: %s", exc)
            raise
        from cloakbrowser import launch_persistent_context

        profile_path = Path(profile_dir) if profile_dir else Path(self.config.chrome_profile_dir)
        profile_path.mkdir(parents=True, exist_ok=True)
        return launch_persistent_context(
            user_data_dir=str(profile_path),
            headless=False,
            humanize=True,
            accept_downloads=True,
            args=["--disable-features=CrossOriginOpenerPolicy"],
        )

    def _preauthenticate_carsi_portal(self, context: Any) -> bool:
        if not self.carsi_portal_preauth:
            return False
        if self._carsi_portal_preauth_attempted:
            return self._carsi_portal_preauth_done
        self._carsi_portal_preauth_attempted = True
        page = context.new_page()
        result = DownloadResult(doi="_carsi_portal", status="failed", state="carsi_portal_preauth")
        try:
            page.goto(CARSI_PORTAL_LOGIN_URL, wait_until="domcontentloaded", timeout=60_000)
            self._event(result, "carsi_portal_open", CARSI_PORTAL_LOGIN_URL)
            self._select_carsi_portal_institution(page, result)
            deadline = time.time() + max(1, self.login_timeout_sec)
            while time.time() < deadline:
                if self._carsi_portal_authenticated(page):
                    self._event(result, "carsi_portal_authenticated", getattr(page, "url", ""))
                    self._carsi_portal_preauth_done = True
                    return True
                time.sleep(3)
            self._event(result, "carsi_portal_timeout", getattr(page, "url", ""))
            return False
        except Exception as exc:
            self._event(result, "carsi_portal_error", f"{type(exc).__name__}: {exc}")
            return False
        finally:
            self._carsi_portal_preauth_events = list(result.events)
            try:
                page.close()
            except Exception:
                pass

    def _select_carsi_portal_institution(self, page: Any, result: DownloadResult) -> bool:
        if not self.institution_query:
            return False
        try:
            detail = page.evaluate(
                """
                (query) => {
                  const needle = (query || '').toString().replace(/\\s+/g, ' ').trim().toLowerCase();
                  if (!needle) return null;
                  const visible = (el) => {
                    const rect = el.getBoundingClientRect();
                    const style = window.getComputedStyle(el);
                    return rect.width > 0 && rect.height > 0 && style.visibility !== 'hidden' && style.display !== 'none';
                  };
                  const textOf = (el) => [
                    el.innerText || '',
                    el.textContent || '',
                    el.value || '',
                    el.getAttribute('aria-label') || '',
                    el.getAttribute('title') || '',
                    el.getAttribute('placeholder') || ''
                  ].join(' ').replace(/\\s+/g, ' ').trim();
                  const setInput = (input) => {
                    input.focus();
                    input.value = query;
                    input.dispatchEvent(new Event('input', {bubbles: true}));
                    input.dispatchEvent(new Event('change', {bubbles: true}));
                  };
                  const inputs = [...document.querySelectorAll('input[type="search"],input[type="text"],input:not([type])')]
                    .filter(visible)
                    .sort((a, b) => a.getBoundingClientRect().top - b.getBoundingClientRect().top);
                  if (inputs[0]) setInput(inputs[0]);
                  const controls = [...document.querySelectorAll('a,button,[role="button"],[role="option"],li,div')]
                    .filter(visible)
                    .map((el) => ({el, text: textOf(el)}))
                    .filter((item) => item.text && item.text.toLowerCase().includes(needle) && item.text.length < 300)
                    .sort((a, b) => a.text.length - b.text.length);
                  if (!controls[0]) return inputs[0] ? {action: 'typed', query} : null;
                  const target = controls[0].el.closest('a,button,[role="button"],[role="option"],li') || controls[0].el;
                  target.click();
                  return {action: 'selected', text: controls[0].text.slice(0, 180)};
                }
                """,
                self.institution_query,
            )
            if detail:
                self._event(result, "carsi_portal_institution", json.dumps(detail, ensure_ascii=False))
                return True
        except Exception as exc:
            self._event(result, "carsi_portal_institution_error", f"{type(exc).__name__}: {exc}")
        return False

    def _carsi_portal_authenticated(self, page: Any) -> bool:
        current_url = str(getattr(page, "url", "") or "")
        parsed = urlparse(current_url)
        host = (parsed.hostname or parsed.netloc or "").lower()
        if host != "ds.carsi.edu.cn":
            return False
        text = self._body_text(page, 3_000).lower()
        title_text = f"{self._title(page)} {text}".lower()
        resource_markers = (
            "application resources",
            "my resources",
            "logout",
            "sign out",
            "\u5e94\u7528\u8d44\u6e90",
            "\u6211\u7684\u8d44\u6e90",
            "\u9000\u51fa",
            "\u6ce8\u9500",
        )
        if any(marker in title_text for marker in resource_markers):
            return True
        if self._is_human_login_page(page):
            return False
        if "/login/" not in parsed.path.lower():
            return True
        return any(
            marker in text
            for marker in (
                "退出",
                "注销",
                "本校已购",
                "我的资源",
                "logout",
                "sign out",
                "my resources",
            )
        )

    def fetch_one(self, context: Any, record: PaperRecord, run_dir: Path) -> DownloadResult:
        page: Any | None = None
        result = DownloadResult(
            doi=record.doi,
            status="failed",
            state="started",
            article_url=self.profile.article_url(record.doi),
        )
        try:
            if self.supplementary_only and self.profile.name.lower() == "elsevier":
                saved_paths = self._download_elsevier_api_supplementary_materials(record, run_dir, result)
                if saved_paths:
                    result.status = "success"
                    result.reason = ""
                    result.state = "supplementary_downloaded"
                    result.final_url = result.article_url
                    result.size_bytes = sum(
                        Path(path).stat().st_size
                        for path in saved_paths
                        if Path(path).exists()
                    )
                    (run_dir / f"{safe_name(record.doi)}.json").write_text(
                        json.dumps({**asdict(result), "full_text": ""}, ensure_ascii=False, indent=2),
                        encoding="utf-8",
                    )
                    return result
                self._event(result, "elsevier_api_supplementary_fallback", result.reason or "no_api_attachments")

            page = context.new_page()
            self._event(result, "article_open", result.article_url)
            if not self._ensure_login(page, result):
                if self._article_access_available(page):
                    self._event(result, "login_completed_after_timeout", getattr(page, "url", ""))
                else:
                    result.reason = self._login_block_reason(page) or "sso_required"
                    result.state = result.reason
                    self._write_diagnostic(page, result, run_dir)
                    return result

            time.sleep(2)
            if self._looks_logged_out(page):
                self._event(result, "auth_wall_after_article_load", getattr(page, "url", ""))
                if not self._complete_login_from_current_page(page, result):
                    if self._article_access_available(page):
                        self._event(result, "login_completed_after_timeout", getattr(page, "url", ""))
                    else:
                        result.reason = self._login_block_reason(page) or "sso_required"
                        result.state = result.reason
                        self._write_diagnostic(page, result, run_dir)
                        return result
                time.sleep(2)
                if self._looks_logged_out(page):
                    result.reason = self._login_block_reason(page) or "sso_required"
                    result.state = result.reason
                    self._write_diagnostic(page, result, run_dir)
                    return result

            result.final_url = page.url
            result.title = self._title(page)
            result.state = "article_loaded"
            self._hold_after_login(page, result)
            supplementary_candidates = self._supplementary_candidates(page, record.doi)
            if self.supplementary_only:
                saved_paths = self._download_supplementary_materials(
                    page,
                    record,
                    run_dir,
                    result,
                    candidates=supplementary_candidates,
                )
                result.final_url = page.url
                result.title = self._title(page)
                if saved_paths:
                    result.status = "success"
                    result.reason = ""
                    result.state = "supplementary_downloaded"
                    if not result.supplementary_paths:
                        result.supplementary_paths = saved_paths
                    result.size_bytes = sum(
                        Path(path).stat().st_size
                        for path in saved_paths
                        if Path(path).exists()
                    )
                    (run_dir / f"{safe_name(record.doi)}.json").write_text(
                        json.dumps({**asdict(result), "full_text": ""}, ensure_ascii=False, indent=2),
                        encoding="utf-8",
                    )
                    return result
                result.reason = "supplementary_not_found" if not supplementary_candidates else "supplementary_not_captured"
                result.state = result.reason
                self._write_diagnostic(page, result, run_dir)
                return result
            pdf_bytes, pdf_url = self._capture_pdf(page, record.doi, result)
            if not pdf_bytes:
                if self._looks_logged_out(page):
                    self._event(result, "auth_wall_after_pdf_attempt", getattr(page, "url", ""))
                    if self._complete_login_from_current_page(page, result):
                        result.final_url = page.url
                        result.title = self._title(page)
                        result.state = "article_loaded_after_sso"
                        pdf_bytes, pdf_url = self._capture_pdf(page, record.doi, result)
                    elif self._article_access_available(page):
                        self._event(result, "login_completed_after_timeout", getattr(page, "url", ""))
                        result.final_url = page.url
                        result.title = self._title(page)
                        result.state = "article_loaded_after_sso"
                        pdf_bytes, pdf_url = self._capture_pdf(page, record.doi, result)
                    block_reason = self._login_block_reason(page)
                    if not pdf_bytes and (block_reason or self._looks_logged_out(page)):
                        result.reason = block_reason or "sso_required"
                        result.state = result.reason
                        result.final_url = page.url
                        result.title = self._title(page)
                        self._write_diagnostic(page, result, run_dir)
                        return result
                    if not pdf_bytes:
                        result.reason = "pdf_not_captured"
                        result.state = "pdf_not_captured"
                        result.final_url = page.url
                        result.title = self._title(page)
                        self._write_diagnostic(page, result, run_dir)
                        return result
                else:
                    block_reason = self._login_block_reason(page)
                    result.reason = block_reason or "pdf_not_captured"
                    result.state = result.reason
                    result.final_url = page.url
                    result.title = self._title(page)
                    self._write_diagnostic(page, result, run_dir)
                    return result

            if not pdf_bytes:
                result.reason = "pdf_not_captured"
                result.state = "pdf_not_captured"
                result.final_url = page.url
                result.title = self._title(page)
                self._write_diagnostic(page, result, run_dir)
                return result

            pdf_dir = run_dir / "pdfs"
            pdf_dir.mkdir(parents=True, exist_ok=True)
            pdf_path = pdf_dir / f"{safe_name(record.doi)}.pdf"
            pdf_path.write_bytes(pdf_bytes)
            text = pdf_extractor.extract_from_bytes(pdf_bytes)
            result.status = "success"
            result.reason = ""
            result.state = "pdf_response_captured"
            result.pdf_url = pdf_url
            result.pdf_path = str(pdf_path)
            result.size_bytes = len(pdf_bytes)
            result.text_length = len(text or "")
            result.verified_match = self._text_matches_record(text, record, fallback_title=result.title)
            if supplementary_candidates:
                self._download_supplementary_materials(
                    page,
                    record,
                    run_dir,
                    result,
                    candidates=supplementary_candidates,
                )
            (run_dir / f"{safe_name(record.doi)}.json").write_text(
                json.dumps({**asdict(result), "full_text": text}, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
            return result
        except Exception as exc:
            result.reason = f"{type(exc).__name__}: {exc}"
            result.state = "unexpected_error"
            if page is not None:
                self._write_diagnostic(page, result, run_dir)
            return result
        finally:
            if page is not None:
                self._hold_after_run(page, result)
                try:
                    page.close()
                except Exception:
                    pass

    def _hold_after_login(self, page: Any, result: DownloadResult) -> None:
        if not self.post_login_hold_sec:
            return
        self._event(result, "post_login_hold", f"{self.post_login_hold_sec}s")
        deadline = time.time() + self.post_login_hold_sec
        while time.time() < deadline:
            if self._is_human_login_page(page):
                self._event(result, "post_login_hold_login_page", getattr(page, "url", ""))
            elif self._looks_logged_out(page):
                self._event(result, "post_login_hold_auth_wall", getattr(page, "url", ""))
            else:
                self._event(result, "post_login_hold_article_access", getattr(page, "url", ""))
            time.sleep(min(5, max(1, int(deadline - time.time()))))

    def _hold_after_run(self, page: Any, result: DownloadResult) -> None:
        if not self.post_run_hold_sec:
            return
        self._event(result, "post_run_hold", f"{self.post_run_hold_sec}s")
        deadline = time.time() + self.post_run_hold_sec
        while time.time() < deadline:
            time.sleep(min(5, max(1, int(deadline - time.time()))))

    def _ensure_login(self, page: Any, result: DownloadResult) -> bool:
        page.goto(result.article_url, wait_until="domcontentloaded", timeout=60_000)
        time.sleep(3)
        if not self._wait_for_challenge(page, result):
            return False
        self._dismiss_cookie_banners(page, result)
        if not self._looks_logged_out(page):
            return True

        return self._complete_login_from_current_page(page, result)

    def _complete_login_from_current_page(self, page: Any, result: DownloadResult) -> bool:
        result.state = "sso_required"
        self._event(result, "sso_start", page.url)
        self._dismiss_cookie_banners(page, result)
        self._click_sso_entry(page, result)
        time.sleep(5)
        if not self._is_human_login_page(page):
            self._dismiss_cookie_banners(page, result)
            self._click_openathens_entry(page, result)
            time.sleep(2)
        if not self._is_human_login_page(page):
            self._select_institution(page, result)

        deadline = time.time() + self.login_timeout_sec
        last_state = ""
        last_auto_action = ""
        last_auto_action_at = 0.0
        while time.time() < deadline:
            time.sleep(3)
            marker = f"{self._title(page)} | {getattr(page, 'url', '')[:160]}"
            if marker != last_state:
                self._event(result, "login_state", marker)
                last_state = marker
            if self._is_human_login_page(page):
                continue
            self._dismiss_cookie_banners(page, result)
            self._click_optional_continue(page, result)
            if self._is_challenge_page(page):
                if self._wait_for_challenge(page, result, deadline=deadline):
                    continue
                self._event(result, "challenge_or_viewer_timeout", self._body_text(page, 500))
                return False
            if self._return_to_record_article_if_needed(page, result, result.doi):
                if not self._looks_logged_out(page):
                    return True
                continue
            if self._article_access_available(page):
                return True
            if self._has_publisher_institution_session(page) and not self._is_success_article_url(getattr(page, "url", "")):
                try:
                    page.goto(result.article_url, wait_until="domcontentloaded", timeout=60_000)
                    self._event(result, "institution_session_return_article", result.article_url)
                    time.sleep(3)
                    self._dismiss_cookie_banners(page, result)
                    if not self._looks_logged_out(page):
                        return True
                    continue
                except Exception as exc:
                    self._event(result, "institution_session_return_error", f"{type(exc).__name__}: {exc}")
            block_reason = self._login_block_reason(page)
            if block_reason:
                self._event(result, block_reason, self._body_text(page, 500))
                return False
            if marker != last_auto_action or time.time() - last_auto_action_at > 10:
                if self._select_institution(page, result):
                    time.sleep(2)
                    last_auto_action = marker
                    last_auto_action_at = time.time()
                    continue
                if self._looks_logged_out(page) and self._click_sso_entry(page, result):
                    time.sleep(2)
                    if not self._is_human_login_page(page):
                        self._select_institution(page, result)
                    last_auto_action = marker
                    last_auto_action_at = time.time()
                    continue
                if self._click_openathens_entry(page, result):
                    time.sleep(2)
                    if not self._is_human_login_page(page):
                        self._select_institution(page, result)
                    last_auto_action = marker
                    last_auto_action_at = time.time()
                    continue
                self._select_institution(page, result)
                last_auto_action = marker
                last_auto_action_at = time.time()
            if self._article_access_available(page):
                return True
        if self._has_publisher_institution_session(page):
            try:
                page.goto(result.article_url, wait_until="domcontentloaded", timeout=60_000)
                self._event(result, "institution_session_return_article", result.article_url)
                time.sleep(3)
                self._dismiss_cookie_banners(page, result)
                return not self._looks_logged_out(page)
            except Exception as exc:
                self._event(result, "institution_session_return_error", f"{type(exc).__name__}: {exc}")
        return False

    def _login_block_reason(self, page: Any) -> str:
        current_url = str(getattr(page, "url", "") or "").lower()
        haystack = f"{self._title(page)} {self._body_text(page, 2_000)}".lower()
        if self._is_challenge_page(page):
            return "challenge_or_viewer_timeout"
        if (
            "are you a robot" in haystack
            or "verify you are human" in haystack
            or "checking your browser" in haystack
            or "\u6b63\u5728\u8fdb\u884c\u5b89\u5168\u9a8c\u8bc1" in haystack
            or "\u8bf7\u9a8c\u8bc1\u60a8\u662f\u771f\u4eba" in haystack
            or "\u771f\u4eba\u9a8c\u8bc1" in haystack
            or "正在进行安全验证" in haystack
            or ("ray id:" in haystack and "cloudflare" in haystack)
        ):
            return "challenge_or_viewer_timeout"
        if self.profile.name.lower() == "world scientific" and "worldscientific.com/action/ssostart" in current_url:
            if "find your institution" in haystack or "type the name of your institution" in haystack:
                return ""
            return "sso_redirect_stalled"
        if (
            "ieee xplore is temporarily unavailable" in haystack
            or ("temporarily unavailable" in haystack and "onlinesupport@ieee.org" in haystack)
            ):
            return "publisher_temporarily_unavailable"
        if self._elsevier_has_configured_institution_access_entry(haystack):
            return ""
        if self._elsevier_lacks_pdf_entitlement(haystack):
            return "institution_pdf_entitlement_missing"
        if "unsupported request" in haystack and "not registered for use with this service" in haystack:
            return "institution_not_registered"
        if "application you have accessed is not registered" in haystack:
            return "institution_not_registered"
        if "your institution may not be enabled for this type of authentication" in haystack:
            return "institution_not_registered"
        if "can't find your institution" in haystack and "institution sign in" in haystack:
            return "institution_not_registered"
        return ""

    def _click_sso_entry(self, page: Any, result: DownloadResult | None = None) -> bool:
        if self._is_human_login_page(page):
            return False
        if self._open_aps_article_institution_login(page, result):
            return True
        if self.profile.name.lower() == "aps":
            if result is not None:
                self._event(result, "aps_institution_entry_missing", getattr(page, "url", ""))
            return False
        if self._click_iop_access_wall(page, result):
            return True
        if self._click_elsevier_institution_entry(page, result):
            return True
        if self._click_wiley_institution_login_entry(page, result):
            return True
        sso_selectors = (
            "button:has-text('Access Through Your Institution')",
            "button:has-text('Access through your institution')",
            "a:has-text('Access Through Your Institution')",
            "a:has-text('Access through your institution')",
            "[role='button']:has-text('Access Through Your Institution')",
            "[role='button']:has-text('Access through your institution')",
            "button:has-text('Institutional Access')",
            "a:has-text('Institutional Access')",
            "button:has-text('Institutional Sign In')",
        )
        for selector in sso_selectors:
            try:
                control = page.locator(selector).first
                if not control.is_visible(timeout=1_500):
                    continue
                text = ""
                href = ""
                try:
                    text = control.inner_text(timeout=1_000)
                except Exception:
                    pass
                try:
                    href = control.get_attribute("href", timeout=1_000) or ""
                except Exception:
                    pass
                detail = {"selector": selector, "text": text[:200], "href": href[:300]}
                if self._is_non_sso_navigation_detail(detail, getattr(page, "url", "")):
                    if result is not None:
                        self._event(result, "sso_entry_ignored", json.dumps(detail, ensure_ascii=False))
                    continue
                try:
                    control.click(timeout=10_000, no_wait_after=True)
                except Exception:
                    control.click(timeout=10_000, no_wait_after=True, force=True)
                if result is not None:
                    self._event(result, "sso_entry_clicked", json.dumps(detail, ensure_ascii=False))
                return True
            except Exception:
                continue
        if self._click_ieee_institution_entry(page, result):
            return True
        markers = [marker.lower() for marker in self.profile.sso_text_markers]
        try:
            clicked = page.evaluate(
                """
                (markers) => {
                  const visible = (el) => {
                    const rect = el.getBoundingClientRect();
                    const style = window.getComputedStyle(el);
                    return rect.width > 0 && rect.height > 0 && style.visibility !== 'hidden' && style.display !== 'none';
                  };
                  const candidates = [...document.querySelectorAll('a,button,[role="button"],input[type="button"],input[type="submit"],div,span')]
                    .filter(visible);
                  const exactMarkers = [
                    'access through your institution',
                    'access through institution',
                    'access through your organization',
                    'institutional login',
                    'log in through your institution',
                    'log in via your institution',
                    'provided by your institution',
                    'username/password provided by your institution',
                    'log in with username/password provided by your institution'
                  ];
                  const clickableSelector = 'a,button,[role="button"],input[type="button"],input[type="submit"]';
                  const textOf = (el) => [
                    el.innerText || '',
                    el.textContent || '',
                    el.value || '',
                    el.getAttribute('aria-label') || '',
                    el.getAttribute('title') || ''
                  ].join(' ').replace(/\\s+/g, ' ').trim().toLowerCase();
                  const rawHrefOf = (el) => ((el.getAttribute('href') || el.getAttribute('formaction') || '') + '').trim();
                  const hrefOf = (el) => {
                    const raw = rawHrefOf(el);
                    const href = ((el.href || raw || '') + '').trim();
                    if (!href) return '';
                    try { return new URL(href, location.href).href; } catch { return href; }
                  };
                  const isSamePageAnchor = (el) => {
                    const raw = rawHrefOf(el);
                    if (!raw) return false;
                    if (raw.startsWith('#')) return true;
                    try {
                      const here = new URL(location.href);
                      const target = new URL(raw, location.href);
                      return Boolean(target.hash)
                        && target.origin === here.origin
                        && target.pathname === here.pathname
                        && target.search === here.search;
                    } catch {
                      return false;
                    }
                  };
                  const isNonSsoNavigation = (el) => {
                    const text = textOf(el);
                    const raw = rawHrefOf(el).toLowerCase();
                    const href = hrefOf(el).toLowerCase();
                    return text.includes('skip to main')
                        || text.includes('skip navigation')
                        || raw.startsWith('#')
                        || href.includes('#main-content-focus')
                        || isSamePageAnchor(el);
                  };
                  const detailOf = (el) => ({
                    text: textOf(el).slice(0, 180),
                    href: hrefOf(el).toLowerCase().slice(0, 240),
                    raw_href: rawHrefOf(el).toLowerCase().slice(0, 240)
                  });
                  const matches = (el) => {
                    const text = textOf(el);
                    const href = hrefOf(el).toLowerCase();
                    if (isNonSsoNavigation(el)) return false;
                    if (text.length > 300 && !href.includes('ssostart')) return false;
                    return exactMarkers.some(marker => text.includes(marker) || href.includes(marker))
                        || markers.some(marker => text.includes(marker) || href.includes(marker))
                        || href.includes('ssostart');
                  };
                  const clickMatched = (el) => {
                    const target = el.matches(clickableSelector)
                      ? el
                      : (el.closest(clickableSelector) || el.querySelector(clickableSelector));
                    if (!target) return null;
                    if (isNonSsoNavigation(target)) return null;
                    const detail = detailOf(el);
                    target.click();
                    return detail;
                  };
                  for (const el of candidates.filter((candidate) => candidate.matches(clickableSelector))) {
                    const matched = matches(el);
                    if (matched) {
                      const detail = clickMatched(el);
                      if (detail) return detail;
                    }
                  }
                  return null;
                }
                """,
                markers,
            )
            if clicked:
                if self._is_elsevier_homepage_entry(clicked):
                    return False
                if self._is_non_sso_navigation_detail(clicked, getattr(page, "url", "")):
                    if result is not None:
                        self._event(result, "sso_entry_ignored", json.dumps(clicked, ensure_ascii=False))
                    return False
                if result is not None:
                    self._event(result, "sso_entry_clicked", json.dumps(clicked, ensure_ascii=False))
                return True
        except Exception as exc:
            if result is not None:
                self._event(result, "sso_entry_error", f"{type(exc).__name__}: {exc}")
            return False
        return False

    def _click_wiley_institution_login_entry(self, page: Any, result: DownloadResult | None = None) -> bool:
        if self.profile.name.lower() != "wiley":
            return False
        try:
            detail = page.evaluate(
                """
                (options) => {
                  const norm = (value) => (value || '').toString().replace(/\\s+/g, ' ').trim();
                  const visible = (el) => {
                    const rect = el.getBoundingClientRect();
                    const style = window.getComputedStyle(el);
                    return rect.width > 0 && rect.height > 0 && style.visibility !== 'hidden' && style.display !== 'none';
                  };
                  const hrefOf = (el) => {
                    const raw = el.href || el.getAttribute('href') || el.getAttribute('formaction') || '';
                    if (!raw) return '';
                    try { return new URL(raw, location.href).href; } catch { return raw; }
                  };
                  const controls = [...document.querySelectorAll('a,button,[role="button"],input[type="button"],input[type="submit"]')]
                    .filter(visible)
                    .map((el) => {
                      const text = norm(el.innerText || el.textContent || el.value || el.getAttribute('aria-label') || el.getAttribute('title') || '');
                      const href = hrefOf(el);
                      const haystack = `${text} ${href} ${el.className || ''} ${el.id || ''}`.toLowerCase();
                      const exact = /^institutional login$/i.test(text);
                      const institutionLogin = exact || haystack.includes('institutional login');
                      if (!institutionLogin) return null;
                      if (haystack.includes('personal') || haystack.includes('account') || haystack.includes('register') || haystack.includes('login / register')) return null;
                      const rect = el.getBoundingClientRect();
                      return {el, text, href, rect, score: (exact ? 100 : 20) - Math.max(0, rect.top / 1000)};
                    })
                    .filter(Boolean)
                    .sort((a, b) => b.score - a.score);
                  const target = controls[0];
                  if (!target) return null;
                  target.el.scrollIntoView({block: 'center', inline: 'center'});
                  target.el.click();
                  return {
                    selector: 'wiley-institutional-login',
                    text: target.text.slice(0, 200),
                    href: target.href.slice(0, 500),
                    score: Math.round(target.score)
                  };
                }
                """
            )
            if isinstance(detail, dict) and detail.get("text"):
                if result is not None:
                    self._event(result, "sso_entry_clicked", json.dumps(detail, ensure_ascii=False))
                return True
        except Exception as exc:
            if result is not None:
                self._event(result, "sso_entry_error", f"{type(exc).__name__}: {exc}")
        return False

    def _click_elsevier_institution_entry(self, page: Any, result: DownloadResult | None = None) -> bool:
        if self.profile.name.lower() != "elsevier":
            return False
        if self._is_human_login_page(page):
            return False
        try:
            detail = page.evaluate(
                """
                (options) => {
                  const visible = (el) => {
                    const rect = el.getBoundingClientRect();
                    const style = window.getComputedStyle(el);
                    return rect.width > 0 && rect.height > 0 && style.visibility !== 'hidden' && style.display !== 'none';
                  };
                  const textOf = (el) => [
                    el.innerText || '',
                    el.textContent || '',
                    el.value || '',
                    el.getAttribute('aria-label') || '',
                    el.getAttribute('title') || ''
                  ].join(' ').replace(/\\s+/g, ' ').trim();
                  const hrefOf = (el) => ((el.href || el.getAttribute('href') || el.getAttribute('formaction') || '') + '');
                  const controls = [...document.querySelectorAll('a,button,[role="button"],input[type="button"],input[type="submit"]')]
                    .filter(visible)
                    .map((el) => {
                      const text = textOf(el);
                      const href = hrefOf(el);
                      const haystack = `${text} ${href}`.toLowerCase();
                      let score = 0;
                      let matched = false;
                      if (href.toLowerCase().includes('auth.elsevier.com/shibauth/institutionlogin')) score += 100;
                      if (href.toLowerCase().includes('auth.elsevier.com/shibauth/institutionlogin')) matched = true;
                      const aliases = (options && options.institutionAliases || []).map((value) => (value || '').toString().toLowerCase()).filter(Boolean);
                      if (aliases.some((alias) => haystack.includes(alias))) { score += 80; matched = true; }
                      if (haystack.includes('access through your organization')) { score += 40; matched = true; }
                      if (haystack.includes('access through another organization')) score -= 60;
                      if (haystack.includes('purchase pdf')) score -= 80;
                      if (haystack.includes('go to elsevier homepage') || href.toLowerCase().replace(/\\/$/, '') === 'http://www.elsevier.com') return null;
                      if (!matched) return null;
                      if (el.tagName === 'A') score += 10;
                      return {el, text, href, score};
                    })
                    .filter(Boolean)
                    .filter((item) => item.score > 0)
                    .sort((a, b) => b.score - a.score);
                  const target = controls[0];
                  if (!target) return null;
                  target.el.scrollIntoView({block: 'center', inline: 'center'});
                  target.el.click();
                  return {
                    selector: 'elsevier-institution-access',
                    text: target.text.slice(0, 220),
                    href: target.href.slice(0, 500),
                    score: target.score
                  };
                }
                """,
                {"institutionAliases": list(self.institution_aliases)},
            )
            if isinstance(detail, dict) and detail and not self._is_elsevier_homepage_entry(detail):
                if result is not None:
                    self._event(result, "sso_entry_clicked", json.dumps(detail, ensure_ascii=False))
                return True
        except Exception as exc:
            if result is not None:
                self._event(result, "elsevier_institution_entry_error", f"{type(exc).__name__}: {exc}")
        return False

    def _is_elsevier_homepage_entry(self, detail: Any) -> bool:
        if self.profile.name.lower() != "elsevier" or not isinstance(detail, dict):
            return False
        text = str(detail.get("text") or "").strip().lower()
        href = str(detail.get("href") or "").strip().lower().rstrip("/")
        return "go to elsevier homepage" in text or href in {
            "http://www.elsevier.com",
            "https://www.elsevier.com",
        }

    def _is_non_sso_navigation_detail(self, detail: Any, current_url: str = "") -> bool:
        if not isinstance(detail, dict):
            return False
        text = str(detail.get("text") or "").strip().lower()
        hrefs = [
            str(detail.get("raw_href") or detail.get("rawHref") or "").strip(),
            str(detail.get("href") or "").strip(),
        ]
        if "skip to main" in text or "skip navigation" in text:
            return True
        for href in hrefs:
            lower_href = href.lower()
            if not lower_href:
                continue
            if lower_href.startswith("#") or "#main-content-focus" in lower_href:
                return True
        if not current_url:
            return False
        try:
            current = urlparse(current_url)
            for href in hrefs:
                if not href:
                    continue
                target = urlparse(urljoin(current_url, href))
                if (
                    target.fragment
                    and target.scheme == current.scheme
                    and target.netloc == current.netloc
                    and target.path == current.path
                    and target.query == current.query
                ):
                    return True
        except Exception:
            return False
        return False

    def _is_human_login_page(self, page: Any) -> bool:
        current_url = str(getattr(page, "url", "") or "")
        parsed = urlparse(current_url)
        host = (parsed.hostname or parsed.netloc or "").lower()
        if host.endswith("id.tsinghua.edu.cn") or host.endswith("idp.tsinghua.edu.cn"):
            return True
        institution_host_suffixes = (
            ".edu.cn",
            ".edu",
            ".ac.cn",
            ".edu.hk",
            ".edu.tw",
            ".edu.mo",
        )
        if not any(host.endswith(suffix) for suffix in institution_host_suffixes):
            return False
        host_labels = [label for label in host.split(".") if label]
        host_markers = (
            "idp",
            "sso",
            "cas",
            "auth",
            "authserver",
            "ids",
            "id",
            "login",
            "pass",
        )
        if any(marker in host_labels for marker in host_markers):
            return True
        path_query = f"{parsed.path}?{parsed.query}".lower()
        url_markers = (
            "/idp",
            "/sso",
            "/cas",
            "/auth",
            "oauth",
            "login",
            "shibboleth",
            "saml",
        )
        if any(marker in path_query for marker in url_markers):
            return True
        haystack = f"{self._title(page)} {self._body_text(page, 1_500)}".lower()
        body_markers = (
            "统一身份认证",
            "统一认证",
            "身份认证",
            "账号登录",
            "学号",
            "工号",
            "密码",
            "验证码",
            "北京航空航天大学",
            "single sign-on",
            "identity provider",
            "university login",
            "campus login",
            "beihang",
            "buaa",
            "student id",
            "employee id",
            "verification code",
        )
        return any(marker in haystack for marker in body_markers)

    def _open_aps_article_institution_login(self, page: Any, result: DownloadResult | None = None) -> bool:
        if self.profile.name.lower() != "aps":
            return False
        current_url = str(getattr(page, "url", "") or "").lower()
        if "/login_inst_user" in current_url:
            return False
        try:
            detail = page.evaluate(
                """
                () => {
                  const root = document.querySelector('#inline-unauthorized-page') || document.body;
                  const norm = (value) => (value || '').toString().replace(/\\s+/g, ' ').trim();
                  const link = [...root.querySelectorAll('a')].find((el) => {
                    const href = (el.href || el.getAttribute('href') || '').toLowerCase();
                    const text = norm(el.innerText || el.textContent || '').toLowerCase();
                    return href.includes('/login_inst_user')
                      || text.includes('username/password provided by your institution');
                  });
                  if (!link) return null;
                  return {
                    text: norm(link.innerText || link.textContent || '').slice(0, 200),
                    href: link.href || link.getAttribute('href') || ''
                  };
                }
                """
            )
            if not isinstance(detail, dict) or not detail.get("href"):
                return False
            page.goto(str(detail["href"]), wait_until="domcontentloaded", timeout=30_000)
            if result is not None:
                self._event(result, "sso_entry_clicked", json.dumps(detail, ensure_ascii=False))
            return True
        except Exception as exc:
            if result is not None:
                self._event(result, "sso_entry_error", f"{type(exc).__name__}: {exc}")
            return False

    def _click_ieee_institution_entry(self, page: Any, result: DownloadResult | None = None) -> bool:
        if self.profile.name.lower() != "ieee":
            return False
        current_url = str(getattr(page, "url", "") or "")
        if "ieeexplore.ieee.org/servlet/wayf" in current_url.lower():
            return False
        if "ieeexplore.ieee.org" not in current_url.lower():
            return False
        text = self._body_text(page, 5_000).lower()
        ieee_entry_markers = (
            "you do not have access to this pdf",
            "sign in to continue reading",
            "institutional sign in",
            "access through your institution",
            "search for your institution",
        )
        if not any(marker in text for marker in ieee_entry_markers):
            return False
        try:
            clicked = page.evaluate(
                """
                () => {
                  const visible = (el) => {
                    const rect = el.getBoundingClientRect();
                    const style = window.getComputedStyle(el);
                    return rect.width > 0 && rect.height > 0 && style.visibility !== 'hidden' && style.display !== 'none';
                  };
                  const textOf = (el) => [
                    el.innerText || '',
                    el.textContent || '',
                    el.value || '',
                    el.getAttribute('aria-label') || '',
                    el.getAttribute('title') || ''
                  ].join(' ').replace(/\\s+/g, ' ').trim();
                  const controls = [...document.querySelectorAll('button,a,[role="button"],input[type="button"],input[type="submit"]')]
                    .filter(visible);
                  const wanted = [
                    'access through your institution',
                    'institutional sign in',
                    'institutional login'
                  ];
                  for (const marker of wanted) {
                    const target = controls.find((el) => textOf(el).toLowerCase().includes(marker));
                    if (!target) continue;
                    const detail = {
                      text: textOf(target).slice(0, 200),
                      href: (target.href || target.getAttribute('href') || '').slice(0, 300)
                    };
                    target.click();
                    return detail;
                  }
                  return null;
                }
                """
            )
            if not clicked:
                return False
            if result is not None:
                self._event(result, "sso_entry_clicked", json.dumps(clicked, ensure_ascii=False))
            return True
        except Exception as exc:
            if result is not None:
                self._event(result, "sso_entry_error", f"{type(exc).__name__}: {exc}")
        return False

    def _dismiss_cookie_banners(self, page: Any, result: DownloadResult | None = None) -> bool:
        try:
            clicked = page.evaluate(
                """
                () => {
                  const visible = (el) => {
                    const rect = el.getBoundingClientRect();
                    const style = window.getComputedStyle(el);
                    return rect.width > 0 && rect.height > 0 && style.visibility !== 'hidden' && style.display !== 'none';
                  };
                  const textOf = (el) => [
                    el.innerText || '',
                    el.textContent || '',
                    el.value || '',
                    el.getAttribute('aria-label') || '',
                    el.getAttribute('title') || ''
                  ].join(' ').replace(/\\s+/g, ' ').trim().toLowerCase();
                  const controls = [...document.querySelectorAll('button,a,[role="button"],input[type="button"],input[type="submit"]')]
                    .filter(visible);
                  const patterns = [
                    'accept all',
                    'accept cookies',
                    'accept',
                    'agree',
                    'i agree',
                    'got it',
                    'close',
                    '×'
                  ];
                  const exactOnly = ['accept', 'close'];
                  const target = controls.find((el) => {
                    const text = textOf(el);
                    if (!text) return false;
                    return patterns.some((pattern) => text === pattern || (!exactOnly.includes(pattern) && text.includes(pattern)));
                  });
                  if (!target) return null;
                  const detail = textOf(target).slice(0, 120);
                  target.click();
                  return detail;
                }
                """
            )
            if clicked:
                if result is not None:
                    self._event(result, "cookie_banner_dismissed", str(clicked))
                return True
        except Exception:
            return False
        return False

    def _click_iop_access_wall(self, page: Any, result: DownloadResult | None = None) -> bool:
        if self.profile.name.lower() != "iop":
            return False
        current_url = str(getattr(page, "url", "") or "")
        if "iopscience.iop.org/article/" not in current_url.lower():
            return False
        text = self._body_text(page, 5_000).lower()
        if "access this article" not in text and "access through your institution" not in text:
            return False
        signin_url = "https://myiopscience.iop.org/signin?" + urlencode(
            {"origin": "deeplink", "target": current_url}
        )
        try:
            page.goto(signin_url, wait_until="domcontentloaded", timeout=30_000)
            if result is not None:
                self._event(result, "sso_entry_clicked", signin_url)
            return True
        except Exception as exc:
            if result is not None:
                self._event(result, "sso_entry_error", f"{type(exc).__name__}: {exc}")
            return False

    def _click_openathens_entry(self, page: Any, result: DownloadResult) -> bool:
        if self._is_human_login_page(page):
            return False
        current_url = str(getattr(page, "url", "") or "")
        lower_url = current_url.lower()
        parsed = urlparse(current_url)
        host = (parsed.netloc or "").lower()
        if (
            host.endswith("openathens.net")
            or host.endswith("tsinghua.edu.cn")
            or "id.tsinghua.edu.cn" in lower_url
            or "idp.tsinghua.edu.cn" in lower_url
        ):
            return False
        if parsed.netloc.endswith("annualreviews.org") and parsed.path.lower().startswith("/session/ext/shib"):
            target_url = parse_qs(parsed.query).get("url", ["/"])[0] or "/"
            openathens_url = f"{parsed.scheme or 'https'}://{parsed.netloc}/session/ext/athens?{urlencode({'url': target_url, 'athensWayfSearch': self.institution_query})}"
            try:
                page.goto(openathens_url, wait_until="domcontentloaded", timeout=30_000)
                self._event(result, "openathens_entry", openathens_url)
                return True
            except Exception as exc:
                self._event(result, "openathens_entry_error", f"{type(exc).__name__}: {exc}")
                return False

        if parsed.netloc.endswith("annualreviews.org") and parsed.path.lower().startswith("/session/ext/athens"):
            return False

        try:
            clicked = page.evaluate(
                """
                () => {
                  const candidates = [...document.querySelectorAll('a,button,input[type="button"],input[type="submit"]')];
                  for (const el of candidates) {
                    const text = [
                      el.innerText || '',
                      el.textContent || '',
                      el.value || '',
                      el.getAttribute('aria-label') || '',
                      el.getAttribute('title') || ''
                    ].join(' ').toLowerCase();
                    const href = ((el.href || el.getAttribute('formaction') || '') + '').toLowerCase();
                    if ((text.includes('openathens') || href.includes('openathens'))
                        && !text.includes('shibboleth')) {
                      el.click();
                      return {text: text.slice(0, 160), href: href.slice(0, 240)};
                    }
                  }
                  return null;
                }
                """
            )
            if clicked:
                self._event(result, "openathens_entry", json.dumps(clicked, ensure_ascii=False))
                return True
        except Exception:
            return False
        return False

    def _select_institution(self, page: Any, result: DownloadResult) -> bool:
        if self._is_human_login_page(page):
            return False
        current_url = str(getattr(page, "url", "") or "")
        host = (urlparse(current_url).netloc or "").lower()
        if host.endswith("tsinghua.edu.cn"):
            return False
        if self._select_recent_institution(page, result):
            return True
        if not self.institution_query:
            self._event(result, "institution_required", "No subscription institution was configured for publisher login.")
            return False
        if self._select_openathens_wayfinder(page, result):
            return True
        if self._select_annual_reviews_openathens(page, result):
            return True
        if self._select_ieee_institution(page, result):
            return True
        for selector in self.profile.institution_input_selectors:
            try:
                inp = page.locator(selector).first
                if inp.is_visible(timeout=5_000):
                    inp.fill(self.institution_query)
                    self._event(result, "institution_search", selector)
                    time.sleep(2)
                    if self.profile.name.lower() == "acs" and "pubs.acs.org/action/ssostart" in current_url.lower():
                        try:
                            inp.press("Enter", timeout=3_000)
                        except TypeError:
                            inp.press("Enter")
                        self._event(result, "institution_search_submitted", selector)
                        time.sleep(3)
                        self._click_optional_continue(page, result)
                        return True
                    if self._click_institution_search_result(page, result):
                        return True
                    try:
                        inp.press("Enter", timeout=3_000)
                        self._event(result, "institution_search_submitted", selector)
                        time.sleep(3)
                        if self._click_institution_search_result(page, result):
                            return True
                        self._click_optional_continue(page, result)
                        return True
                    except Exception:
                        continue
            except Exception:
                continue
        return False

    def _elsevier_auth_return_url(self, current_url: str) -> str:
        parsed = urlparse(current_url)
        query = parse_qs(parsed.query)
        for key in ("targetUrl", "targetURL", "returnUrl"):
            value = query.get(key, [""])[0]
            if value:
                return value
        state = query.get("state", [""])[0]
        if state:
            state_query = parse_qs(state)
            value = state_query.get("returnUrl", [""])[0]
            if value:
                return value
        pii = self._extract_elsevier_pii(current_url)
        if pii:
            return f"https://www.sciencedirect.com/science/article/pii/{pii}"
        return current_url if current_url.startswith("http") else "https://www.sciencedirect.com/"

    def _select_recent_institution(self, page: Any, result: DownloadResult) -> bool:
        try:
            detail = page.evaluate(
                """
                (options) => {
                  const norm = (value) => (value || '').toString().replace(/\\s+/g, ' ').trim();
                  const visible = (el) => {
                    const rect = el.getBoundingClientRect();
                    const style = window.getComputedStyle(el);
                    return rect.width > 0 && rect.height > 0 && style.visibility !== 'hidden' && style.display !== 'none';
                  };
                  const textOf = (el) => norm([
                    el.innerText || '',
                    el.textContent || '',
                    el.value || '',
                    el.getAttribute('aria-label') || '',
                    el.getAttribute('title') || ''
                  ].join(' '));
                  const exactBad = [
                    'recent institutions',
                    'search for your institution',
                    'search your institution',
                    'institutional login',
                    'login',
                    'log in',
                    'edit',
                    'change',
                    'remove',
                    'continue'
                  ];
                  const containsBad = [
                    'openathens account',
                    'sign in with your openathens account',
                    'register',
                    'create account',
                    'personal account',
                    'username',
                    'password',
                    'email'
                  ];
                  const aliases = (options && options.institutionAliases || [])
                    .map((value) => norm(value).toLowerCase())
                    .filter(Boolean);
                  const queryTokens = aliases
                    .flatMap((value) => value.split(/[\\s,;，；()（）]+/))
                    .map((token) => token.trim())
                    .filter((token) => token.length >= 3);
                  const aliasMatches = (lower) => aliases.some((alias) => alias && lower.includes(alias));
                  const queryMatches = (lower) => {
                    if (!aliases.length) return true;
                    if (aliasMatches(lower)) return true;
                    if (queryTokens.some((token) => lower.includes(token))) return true;
                    return false;
                  };
                  const clickableSelector = 'a,button,[role="button"],[role="option"],input[type="button"],input[type="submit"]';
                  const clickTargetFor = (control, cardText, cardLower) => {
                    if (control.matches(clickableSelector)) return {el: control, text: cardText, penalty: 0};
                    const children = [...control.querySelectorAll(clickableSelector)]
                      .filter(visible)
                      .map((child) => {
                        const childText = textOf(child);
                        const lower = (childText || cardText).toLowerCase();
                        const ownLower = childText.toLowerCase();
                        if (exactBad.some((marker) => ownLower === marker)) return null;
                        if (containsBad.some((marker) => ownLower.includes(marker))) return null;
                        if (!queryMatches(lower)) return null;
                        const href = ((child.href || child.getAttribute('href') || child.getAttribute('formaction') || '') + '').toLowerCase();
                        let score = 0;
                        if (childText) score += 10;
                        if (href) score += 15;
                        if (aliasMatches(lower)) score += 100;
                        if (lower.includes('university')) score += 70;
                        if (lower.includes('openathens') || lower.includes('shibboleth') || lower.includes('carsi')) score += 30;
                        return {el: child, text: childText || cardText, score};
                      })
                      .filter(Boolean)
                      .sort((a, b) => b.score - a.score);
                    if (children[0]) return {el: children[0].el, text: children[0].text, penalty: 0};
                    return {el: control, text: cardText, penalty: 30};
                  };
                  const sections = [...document.querySelectorAll('section,div,ul,ol')]
                    .filter(visible)
                    .filter((el) => {
                      const sectionText = textOf(el).toLowerCase();
                      if (!sectionText.includes('recent institution')) return false;
                      if (sectionText.includes('no recent institution')) return false;
                      if (sectionText.includes('find institution via federation')) return false;
                      return sectionText.length < 5000;
                    })
                    .sort((a, b) => {
                      const ar = a.getBoundingClientRect();
                      const br = b.getBoundingClientRect();
                      return (ar.width * ar.height) - (br.width * br.height);
                    });
                  for (const section of sections) {
                    const controls = [...section.querySelectorAll('a,button,[role="button"],[role="option"],li,article')]
                      .filter(visible)
                      .map((control) => {
                      const text = textOf(control);
                      const lower = text.toLowerCase();
                        if (text.length < 3 || text.length > 260) return null;
                        if (exactBad.some((marker) => lower === marker)) return null;
                        if (containsBad.some((marker) => lower.includes(marker))) return null;
                        if (!queryMatches(lower)) return null;
                        const rect = control.getBoundingClientRect();
                        let score = 1;
                        if (aliasMatches(lower)) score += 100;
                        if (lower.includes('university')) score += 70;
                        if (lower.includes('institute') || lower.includes('college') || lower.includes('academy')) score += 40;
                        if (lower.includes('openathens') || lower.includes('shibboleth') || lower.includes('carsi')) score += 30;
                        if (lower.includes('edit') || lower.includes('change') || lower.includes('remove')) score -= 25;
                        const clickTarget = clickTargetFor(control, text, lower);
                        return {control: clickTarget.el, text: clickTarget.text, rect, score: score - clickTarget.penalty};
                      })
                      .filter(Boolean)
                      .sort((a, b) => b.score - a.score || a.rect.top - b.rect.top);
                    const target = controls[0];
                    if (!target) continue;
                    target.control.scrollIntoView({block: 'center', inline: 'center'});
                    if (typeof target.control.focus === 'function') {
                      try { target.control.focus({preventScroll: true}); } catch (_) { target.control.focus(); }
                    }
                    target.control.click();
                    return {selector: 'recent-institution', text: target.text.slice(0, 200), score: Math.round(target.score)};
                  }
                  return null;
                }
                """,
                {"institutionAliases": list(self.institution_aliases)},
            )
            if isinstance(detail, dict) and detail.get("text"):
                self._event(result, "institution_selected", json.dumps(detail, ensure_ascii=False))
                self._confirm_recent_institution_selection(page, result)
                self._click_optional_continue(page, result)
                return True
        except Exception as exc:
            self._event(result, "recent_institution_error", f"{type(exc).__name__}: {exc}")
        return False

    def _confirm_recent_institution_selection(self, page: Any, result: DownloadResult) -> None:
        current_url = str(getattr(page, "url", "") or "").lower()
        if "pubs.acs.org/action/ssostart" not in current_url:
            return
        keyboard = getattr(page, "keyboard", None)
        press = getattr(keyboard, "press", None)
        if not callable(press):
            return
        try:
            press("Enter")
            self._event(result, "institution_confirm_enter", "recent-institution")
            time.sleep(2)
        except Exception as exc:
            self._event(result, "institution_confirm_error", f"{type(exc).__name__}: {exc}")

    def _select_ieee_institution(self, page: Any, result: DownloadResult) -> bool:
        if self.profile.name.lower() != "ieee":
            return False
        input_selectors = (
            "input[aria-label='Search for your Institution']",
            "input[aria-label*='Institution']",
            "input.inst-typeahead-input",
            "xpath=(//*[normalize-space()='Search for your Institution']/following::input[1])",
        )
        input_box = None
        input_selector = ""
        for selector in input_selectors:
            try:
                candidate = page.locator(selector).first
                if candidate.is_visible(timeout=1_500):
                    input_box = candidate
                    input_selector = selector
                    break
            except Exception:
                continue
        if input_box is None:
            return False
        try:
            input_box.fill("")
            try:
                input_box.type(self.institution_query, timeout=5_000)
            except Exception:
                input_box.fill(self.institution_query)
            self._event(result, "institution_search", input_selector)
            time.sleep(2)
        except Exception as exc:
            self._event(result, "institution_select_error", f"{type(exc).__name__}: {exc}")
            return False

        result_selectors = list(self._institution_result_selectors())
        for selector in result_selectors:
            try:
                option = page.locator(selector).first
                if option.is_visible(timeout=2_000):
                    option.click(timeout=5_000, no_wait_after=True)
                    self._event(result, "institution_selected", selector)
                    return True
            except Exception:
                continue
        try:
            input_box.press("ArrowDown", timeout=2_000)
            input_box.press("Enter", timeout=2_000)
            self._event(result, "institution_selected", "IEEE typeahead ArrowDown+Enter")
            return True
        except Exception as exc:
            self._event(result, "institution_select_error", f"{type(exc).__name__}: {exc}")
            return False

    def _click_institution_search_result(self, page: Any, result: DownloadResult) -> bool:
        for result_selector in self._institution_result_selectors():
            try:
                page.locator(result_selector).first.click(timeout=5_000)
                self._event(result, "institution_selected", result_selector)
                self._click_optional_continue(page, result)
                return True
            except Exception:
                continue
        try:
            clicked = page.evaluate(
                """
                (aliases) => {
                  const needles = (aliases || []).map((value) => (value || '').toString().toLowerCase()).filter(Boolean);
                  if (!needles.length) return null;
                  const visible = (el) => {
                    const rect = el.getBoundingClientRect();
                    const style = window.getComputedStyle(el);
                    return rect.width > 0 && rect.height > 0 && style.visibility !== 'hidden' && style.display !== 'none';
                  };
                  const textOf = (el) => [
                    el.innerText || '',
                    el.textContent || '',
                    el.getAttribute('aria-label') || '',
                    el.getAttribute('title') || ''
                  ].join(' ').replace(/\\s+/g, ' ').trim();
                  const candidates = [...document.querySelectorAll('a,button,[role="button"],[role="option"],li,div,span')]
                    .filter(visible)
                    .filter((el) => {
                      const text = textOf(el).toLowerCase();
                      return needles.some((needle) => text.includes(needle)) && text.length < 300;
                    });
                  const target = candidates.find((el) => needles.some((needle) => textOf(el).toLowerCase().includes(needle)));
                  if (!target) return null;
                  const clickable = target.closest('a,button,[role="button"],[role="option"],li') || target;
                  const detail = textOf(clickable).slice(0, 160);
                  clickable.click();
                  return detail;
                }
                """,
                list(self.institution_aliases),
            )
            if clicked:
                self._event(result, "institution_selected", str(clicked))
                self._click_optional_continue(page, result)
                return True
        except Exception:
            return False
        return False

    def _institution_result_selectors(self) -> tuple[str, ...]:
        if not self.institution_aliases:
            return ()
        selectors: list[str] = []
        for alias in self.institution_aliases:
            selectors.extend(institution_result_selectors(alias))
        return tuple(dict.fromkeys(selectors))

    def _select_openathens_wayfinder(self, page: Any, result: DownloadResult) -> bool:
        current_url = str(getattr(page, "url", "") or "")
        parsed = urlparse(current_url)
        host = (parsed.netloc or "").lower()
        if not host.endswith("wayfinder.openathens.net"):
            return False
        return self._click_institution_search_result(page, result)

    def _select_annual_reviews_openathens(self, page: Any, result: DownloadResult) -> bool:
        current_url = str(getattr(page, "url", "") or "").lower()
        if "annualreviews.org/session/ext/athens" not in current_url:
            return False
        if self._click_institution_search_result(page, result):
            return True
        try:
            input_box = page.locator(
                "xpath=(//*[contains(normalize-space(.), 'Option 2: Sign-in with OpenAthens')]/following::input[contains(@placeholder, 'organization')])[1]"
            ).first
            if input_box.is_visible(timeout=1_500):
                input_box.fill(self.institution_query)
                page.locator("text=Find Your Organization").last.click(timeout=5_000, no_wait_after=True)
                self._event(result, "institution_search", f"OpenAthens: {self.institution_query}")
                time.sleep(2)
                return self._click_institution_search_result(page, result) or True
        except Exception:
            pass
        try:
            action = page.evaluate(
                """
                (query) => {
                  const needle = (query || '').toLowerCase();
                  if (!needle) return null;
                  const visible = (el) => {
                    const rect = el.getBoundingClientRect();
                    const style = window.getComputedStyle(el);
                    return rect.width > 0 && rect.height > 0 && style.visibility !== 'hidden' && style.display !== 'none';
                  };
                  const textOf = (el) => [
                    el.innerText || '',
                    el.textContent || '',
                    el.value || '',
                    el.getAttribute('aria-label') || '',
                    el.getAttribute('title') || ''
                  ].join(' ').trim();
                  const controls = [...document.querySelectorAll('a,button,input[type="button"],input[type="submit"]')].filter(visible);
                  const goButtons = controls.filter((el) => textOf(el).toLowerCase().includes('go to sign-in'));
                  const resultEls = [...document.querySelectorAll('a,button,li,div,span,option')]
                    .filter(visible)
                    .filter((el) => textOf(el).toLowerCase().includes(needle));
                  if (resultEls.length) {
                    resultEls[resultEls.length - 1].click();
                    if (goButtons.length) {
                      goButtons[goButtons.length - 1].click();
                      return {action: 'openathens_go', result: textOf(resultEls[resultEls.length - 1]).slice(0, 160)};
                    }
                    return {action: 'openathens_result', result: textOf(resultEls[resultEls.length - 1]).slice(0, 160)};
                  }
                  const inputs = [...document.querySelectorAll('input')]
                    .filter(visible)
                    .filter((el) => {
                      const haystack = [el.placeholder || '', el.name || '', el.id || '', el.getAttribute('aria-label') || ''].join(' ').toLowerCase();
                      return haystack.includes('organization') || haystack.includes('institution');
                    });
                  if (inputs.length) {
                    const input = inputs[inputs.length - 1];
                    input.focus();
                    input.value = query;
                    input.dispatchEvent(new Event('input', {bubbles: true}));
                    input.dispatchEvent(new Event('change', {bubbles: true}));
                    const findButtons = controls.filter((el) => textOf(el).toLowerCase().includes('find your organization'));
                    if (findButtons.length) {
                      findButtons[findButtons.length - 1].click();
                    }
                    return {action: 'openathens_search', query};
                  }
                  if (goButtons.length) {
                    goButtons[goButtons.length - 1].click();
                    return {action: 'openathens_go'};
                  }
                  return null;
                }
                """,
                self.institution_query,
            )
            if action:
                self._event(result, "institution_selected", json.dumps(action, ensure_ascii=False))
                return True
        except Exception as exc:
            self._event(result, "institution_select_error", f"{type(exc).__name__}: {exc}")
        return False

    def _click_optional_continue(self, page: Any, result: DownloadResult) -> None:
        if self._is_human_login_page(page):
            return
        selectors = (
            "button:has-text('提交并继续')",
            "button:has-text('继续')",
            "button:has-text('Submit and continue')",
            "button:has-text('Continue')",
            "button:has-text('Yes')",
            "[role='button']:has-text('Yes')",
            "a:has-text('Yes')",
            "input[value='Yes']",
            "input[type='submit']",
        )
        for selector in selectors:
            try:
                button = page.locator(selector).first
                if button.is_visible(timeout=1_500):
                    try:
                        text = (button.inner_text(timeout=1_000) or "").strip().lower()
                    except Exception:
                        text = ""
                    if "continue reading" in text:
                        continue
                    button.click(timeout=5_000, no_wait_after=True)
                    self._event(result, "institution_continue", selector)
                    return
            except Exception:
                continue

    def _capture_pdf(self, page: Any, doi: str, result: DownloadResult) -> tuple[bytes | None, str]:
        captured: dict[str, Any] = {"bytes": None, "url": "", "deferred_url": ""}

        def on_response(response: Any) -> None:
            if captured["bytes"]:
                return
            try:
                url = response.url
                content_type = (response.headers.get("content-type") or "").lower()
                if self._is_supplementary_url(url):
                    return
                if not self._is_record_pdf_url(url, doi):
                    return
                if "pdf" not in content_type and not self._is_pdf_candidate_url(url):
                    return
                if self._should_defer_response_body(url):
                    captured["deferred_url"] = url
                    return
                body = response.body()
                if is_plausible_pdf_bytes(body):
                    captured["bytes"] = body
                    captured["url"] = url
            except Exception:
                return

        page.on("response", on_response)
        try:
            self._return_to_record_article_if_needed(page, result, doi)
            if self.profile.name.lower() == "wiley" and self._click_wiley_read_full_text_entry(page, result):
                time.sleep(3)
                self._wait_for_challenge(page, result)
                self._dismiss_cookie_banners(page, result)
                if self._looks_logged_out(page) or self._wiley_institution_login_visible(page):
                    self._complete_login_from_current_page(page, result)
                    self._return_to_record_article_if_needed(page, result, doi)
            body, final_url = self._capture_pdf_with_browser_cookies(page, doi, result)
            if body:
                captured["bytes"] = body
                captured["url"] = final_url
                return captured["bytes"], str(captured["url"])
            clicked_pdf_entry = self._click_pdf_entry(page, result, doi=doi)
            if clicked_pdf_entry:
                time.sleep(5)
                self._wait_for_challenge(page, result)
                if not captured["bytes"]:
                    body, final_url = self._fetch_page_state_pdf(page, doi=doi)
                    if body:
                        captured["bytes"] = body
                        captured["url"] = final_url
                if self.profile.name.lower() == "acs" and not captured["bytes"]:
                    self._event(result, "acs_pdf_click_no_capture", "skipped direct PDF fallback after article-page click")
                    return captured["bytes"], str(captured["url"])
            for pdf_url in self._pdf_candidates(page, doi):
                if captured["bytes"]:
                    break
                self._event(result, "pdf_candidate", pdf_url)
                if self._should_use_async_pdf_navigation(pdf_url):
                    body, final_url = self._capture_pdf_via_async_navigation(page, pdf_url, result)
                    if body:
                        captured["bytes"] = body
                        captured["url"] = final_url
                        break
                    continue
                try:
                    response = page.goto(pdf_url, wait_until="commit", timeout=self.pdf_timeout_ms)
                    if response is not None and not captured["bytes"]:
                        response_url = str(getattr(response, "url", "") or "")
                        if self._should_defer_response_body(response_url):
                            captured["deferred_url"] = response_url
                        else:
                            body = response.body()
                            if is_plausible_pdf_bytes(body):
                                captured["bytes"] = body
                                captured["url"] = response.url
                    if not captured["bytes"]:
                        body, final_url = self._fetch_page_state_pdf(page, response, [str(captured["deferred_url"])], doi=doi)
                        if body:
                            captured["bytes"] = body
                            captured["url"] = final_url
                except Exception as exc:
                    self._event(result, "pdf_navigation_error", f"{type(exc).__name__}: {exc}")
                    if self._is_download_navigation_abort(exc):
                        body, final_url = self._capture_browser_download(page, pdf_url, result)
                        if body:
                            captured["bytes"] = body
                            captured["url"] = final_url
                self._wait_for_challenge(page, result)
                time.sleep(3)
                if not captured["bytes"]:
                    body, final_url = self._fetch_page_state_pdf(page, extra_urls=[str(captured["deferred_url"])], doi=doi)
                    if body:
                        captured["bytes"] = body
                        captured["url"] = final_url
                        break
        finally:
            try:
                page.remove_listener("response", on_response)
            except Exception:
                pass
        return captured["bytes"], str(captured["url"])

    def _capture_pdf_with_browser_cookies(
        self,
        page: Any,
        doi: str,
        result: DownloadResult,
    ) -> tuple[bytes | None, str]:
        for pdf_url in self._pdf_candidates(page, doi):
            if not self._is_pdf_candidate_url(pdf_url) or self._is_supplementary_url(pdf_url):
                continue
            if not self._is_record_pdf_url(pdf_url, doi):
                continue
            body, final_url = self._fetch_pdf_url_with_browser_cookies(pdf_url, page)
            if body:
                self._event(result, "cookie_fast_path_pdf_captured", final_url)
                return body, final_url
        return None, ""

    def _fetch_pdf_url_with_browser_cookies(self, url: str, page: Any) -> tuple[bytes | None, str]:
        return self._fetch_pdf_url_with_browser_state(url, page)

    def _click_wiley_read_full_text_entry(self, page: Any, result: DownloadResult) -> bool:
        """Use Wiley's full-text access path before trying PDF/ePDF links."""
        try:
            detail = page.evaluate(
                """
                () => {
                  const norm = (value) => (value || '').toString().replace(/\\s+/g, ' ').trim();
                  const visible = (el) => {
                    const rect = el.getBoundingClientRect();
                    const style = window.getComputedStyle(el);
                    return rect.width > 0 && rect.height > 0 && style.visibility !== 'hidden' && style.display !== 'none';
                  };
                  const hrefOf = (el) => {
                    const raw = el.href || el.getAttribute('href') || el.getAttribute('formaction') || '';
                    if (!raw) return '';
                    try { return new URL(raw, location.href).href; } catch { return raw; }
                  };
                  const controls = [...document.querySelectorAll('a,button,[role="button"],input[type="button"],input[type="submit"]')]
                    .filter(visible)
                    .map((el) => {
                      const text = norm(el.innerText || el.textContent || el.value || el.getAttribute('aria-label') || el.getAttribute('title') || '');
                      const href = hrefOf(el);
                      const haystack = `${text} ${href} ${el.className || ''} ${el.id || ''}`.toLowerCase();
                      const readFullText = /\\bread the full text\\b/i.test(text) || href.toLowerCase().includes('/doi/full/');
                      if (!readFullText) return null;
                      if (haystack.includes('pdf') || haystack.includes('account') || haystack.includes('register')) return null;
                      const rect = el.getBoundingClientRect();
                      const exactScore = /^read the full text$/i.test(text) ? 100 : 0;
                      const hrefScore = href.toLowerCase().includes('/doi/full/') ? 30 : 0;
                      return {el, text, href, rect, score: exactScore + hrefScore - Math.max(0, rect.top / 1000)};
                    })
                    .filter(Boolean)
                    .sort((a, b) => b.score - a.score);
                  const target = controls[0];
                  if (!target) return null;
                  target.el.scrollIntoView({block: 'center', inline: 'center'});
                  target.el.click();
                  return {
                    selector: 'wiley-read-full-text',
                    text: target.text.slice(0, 200),
                    href: target.href.slice(0, 500),
                    score: Math.round(target.score)
                  };
                }
                """
            )
            if isinstance(detail, dict) and detail.get("text"):
                self._event(result, "wiley_read_full_text_clicked", json.dumps(detail, ensure_ascii=False))
                return True
        except Exception as exc:
            self._event(result, "wiley_read_full_text_error", f"{type(exc).__name__}: {exc}")
        return False

    def _wiley_institution_login_visible(self, page: Any) -> bool:
        if self.profile.name.lower() != "wiley":
            return False
        haystack = f"{self._title(page)} {self._body_text(page, 2_000)}".lower()
        return any(
            marker in haystack
            for marker in (
                "institutional login",
                "search for your institution",
                "recent institutions",
                "log in through your institution",
                "access through your institution",
            )
        )

    def _click_wiley_pdf_entry(self, page: Any, result: DownloadResult, doi: str) -> bool:
        if self.profile.name.lower() != "wiley":
            return False
        try:
            detail = page.evaluate(
                """
                (options) => {
                  const doi = (options && options.doi || '').toLowerCase();
                  const encodedDoi = encodeURIComponent(doi);
                  const norm = (value) => (value || '').toString().replace(/\\s+/g, ' ').trim();
                  const visible = (el) => {
                    const rect = el.getBoundingClientRect();
                    const style = window.getComputedStyle(el);
                    return rect.width > 0 && rect.height > 0 && style.visibility !== 'hidden' && style.display !== 'none';
                  };
                  const hrefOf = (el) => {
                    const raw = el.href || el.getAttribute('href') || el.getAttribute('formaction') || '';
                    if (!raw) return '';
                    try { return new URL(raw, location.href).href; } catch { return raw; }
                  };
                  const controls = [...document.querySelectorAll('a,button,[role="button"],input[type="button"],input[type="submit"]')]
                    .filter(visible)
                    .map((el) => {
                      const rect = el.getBoundingClientRect();
                      const text = norm(el.innerText || el.textContent || el.value || el.getAttribute('aria-label') || el.getAttribute('title') || '');
                      const href = hrefOf(el);
                      const lowerText = text.toLowerCase();
                      const lowerHref = href.toLowerCase();
                      const haystack = `${lowerText} ${lowerHref} ${el.className || ''} ${el.id || ''}`.toLowerCase();
                      if (haystack.includes('reference') || haystack.includes('citation') || haystack.includes('supporting information')) return null;
                      if (haystack.includes('download references')) return null;
                      const recordHref = doi && (lowerHref.includes(doi) || lowerHref.includes(encodedDoi));
                      const pdfHref = lowerHref.includes('/doi/pdfdirect/')
                        || lowerHref.includes('/doi/pdf/')
                        || lowerHref.includes('/doi/epdf/');
                      const pdfText = /^pdf$/i.test(text) || /^download pdf$/i.test(text) || /\\bdownload pdf\\b/i.test(text);
                      if (href && pdfHref && !recordHref) return null;
                      if (!pdfHref && !pdfText) return null;
                      const rightRailPenalty = rect.left > Math.max(900, window.innerWidth * 0.70) ? 120 : 0;
                      let score = 1;
                      if (recordHref) score += 120;
                      if (lowerHref.includes('/doi/pdfdirect/')) score += 50;
                      if (lowerHref.includes('/doi/pdf/')) score += 40;
                      if (lowerHref.includes('/doi/epdf/')) score += 30;
                      if (/^pdf$/i.test(text)) score += 30;
                      if (/^download pdf$/i.test(text)) score += 35;
                      if (rect.top < window.innerHeight * 0.65) score += 10;
                      return {el, text, href, rect, score: score - rightRailPenalty};
                    })
                    .filter(Boolean)
                    .sort((a, b) => b.score - a.score || a.rect.top - b.rect.top);
                  const target = controls[0];
                  if (!target || target.score <= 0) return null;
                  target.el.scrollIntoView({block: 'center', inline: 'center'});
                  target.el.removeAttribute('target');
                  target.el.click();
                  return {
                    selector: 'wiley-pdf-entry',
                    text: target.text.slice(0, 200),
                    href: target.href.slice(0, 500),
                    score: Math.round(target.score)
                  };
                }
                """,
                {"doi": doi},
            )
            if isinstance(detail, dict) and (detail.get("text") or detail.get("href")):
                self._event(result, "pdf_button_clicked", json.dumps(detail, ensure_ascii=False))
                return True
        except Exception as exc:
            self._event(result, "pdf_button_error", f"{type(exc).__name__}: {exc}")
        return False

    def _click_pdf_entry(self, page: Any, result: DownloadResult, *, doi: str = "") -> bool:
        if self.profile.name.lower() == "elsevier":
            if self._click_elsevier_view_pdf_entry(page, result):
                return True
        if doi and self.profile.name.lower() == "aps":
            if self._click_current_doi_pdf_entry(page, result, doi):
                return True
        if doi and self.profile.name.lower() == "acs":
            if self._click_acs_current_doi_pdf_entry(page, result, doi):
                return True
        if doi and self.profile.name.lower() == "wiley":
            if self._click_wiley_pdf_entry(page, result, doi):
                return True

        selectors = (
            "a.xpl-btn-pdf",
            ".xpl-btn-pdf",
            "a.stats-document-lh-action-downloadPdf_3",
            "[title*='PDF']",
            "[aria-label*='PDF']",
            "button:has-text('PDF')",
            "a:has-text('PDF')",
        )
        for selector in selectors:
            try:
                control = page.locator(selector).first
                if not control.is_visible(timeout=1_500):
                    continue
                text = ""
                href = ""
                try:
                    text = control.inner_text(timeout=1_000)
                except Exception:
                    pass
                try:
                    href = control.get_attribute("href", timeout=1_000) or ""
                except Exception:
                    pass
                if href and self._is_supplementary_url(href):
                    continue
                # Never treat a Buy/Cart/price CTA as the PDF entry — "Buy
                # article PDF USD 39.95" matches every generic PDF selector
                # (#13).
                if _is_purchase_control(text, href):
                    self._event(result, "pdf_entry_skipped_purchase", json.dumps(
                        {"selector": selector, "text": text[:120], "href": href[:200]},
                        ensure_ascii=False))
                    continue
                detail = {"selector": selector, "text": text[:200], "href": href[:300]}
                control.click(timeout=10_000, no_wait_after=True)
                self._event(result, "pdf_button_clicked", json.dumps(detail, ensure_ascii=False))
                return True
            except Exception:
                continue

        try:
            clicked = page.evaluate(
                """
                () => {
                  const visible = (el) => {
                    const rect = el.getBoundingClientRect();
                    const style = window.getComputedStyle(el);
                    return rect.width > 0 && rect.height > 0 && style.visibility !== 'hidden' && style.display !== 'none';
                  };
                  const textOf = (el) => [
                    el.innerText || '',
                    el.textContent || '',
                    el.value || '',
                    el.getAttribute('aria-label') || '',
                    el.getAttribute('title') || '',
                    el.className || '',
                    el.id || ''
                  ].join(' ').replace(/\\s+/g, ' ').trim();
                  const controls = [...document.querySelectorAll('a,button,[role="button"],input[type="button"],input[type="submit"]')]
                    .filter(visible);
                  const isPdfControl = (el) => {
                    const haystack = textOf(el).toLowerCase();
                    const href = ((el.href || el.getAttribute('href') || el.getAttribute('formaction') || '') + '').toLowerCase();
                    if (haystack.includes('reference') || haystack.includes('citation')) return false;
                    if (haystack.includes('download references')) return false;
                    if (href.includes('/doi/suppl/') || href.includes('/suppl_file/') || haystack.includes('supporting information')) return false;
                    const purchase = /\\bbuy\\b|purchase|add[- ]to[- ]cart|shopping[- ]cart|checkout|(?:usd|eur|gbp|cny|rmb)\\s?\\d|\\$\\s?\\d|￥\\s?\\d|¥\\s?\\d/i;
                    if (purchase.test(haystack) || purchase.test(href)) return false;
                    return /(^|\\s)pdf(\\s|$)/i.test(textOf(el))
                      || haystack.includes('download pdf')
                      || haystack.includes('xpl-btn-pdf')
                      || href.includes('/pdf')
                      || href.includes('stamppdf')
                      || href.includes('/stamp/');
                  };
                  const target = controls.find(isPdfControl);
                  if (!target) return null;
                  const detail = {
                    text: textOf(target).slice(0, 200),
                    href: ((target.href || target.getAttribute('href') || target.getAttribute('formaction') || '') + '').slice(0, 300)
                  };
                  target.click();
                  return detail;
                }
                """
            )
            if isinstance(clicked, (dict, str)) and clicked:
                self._event(result, "pdf_button_clicked", json.dumps(clicked, ensure_ascii=False))
                return True
        except Exception as exc:
            self._event(result, "pdf_button_error", f"{type(exc).__name__}: {exc}")
        return False

    def _click_elsevier_view_pdf_entry(self, page: Any, result: DownloadResult) -> bool:
        try:
            detail = page.evaluate(
                """
                () => {
                  const norm = (value) => (value || '').toString().replace(/\\s+/g, ' ').trim();
                  const visible = (el) => {
                    const rect = el.getBoundingClientRect();
                    const style = window.getComputedStyle(el);
                    return rect.width > 0 && rect.height > 0 && style.visibility !== 'hidden' && style.display !== 'none';
                  };
                  const hrefOf = (el) => {
                    const raw = el.href || el.getAttribute('href') || el.getAttribute('formaction') || '';
                    if (!raw) return '';
                    try { return new URL(raw, location.href).href; } catch { return raw; }
                  };
                  const controls = [...document.querySelectorAll('a,button,[role="button"],input[type="button"],input[type="submit"]')]
                    .filter(visible)
                    .map((el) => {
                      const rect = el.getBoundingClientRect();
                      const text = norm(el.innerText || el.textContent || el.value || el.getAttribute('aria-label') || el.getAttribute('title') || '');
                      const href = hrefOf(el);
                      const haystack = `${text} ${href} ${el.className || ''} ${el.id || ''}`.toLowerCase();
                      const isPdf = /\\bview pdf\\b/.test(haystack)
                        || /\\bdownload pdf\\b/.test(haystack)
                        || haystack.includes('/pdfft')
                        || haystack.includes('pdfreader');
                      if (!isPdf) return null;
                      const isRecommendedColumn = rect.left > Math.max(900, window.innerWidth * 0.70);
                      const exactScore = /^view pdf$/i.test(text) ? 120 : 0;
                      const topBarScore = rect.top < 160 ? 60 : 0;
                      const hrefScore = href.toLowerCase().includes('/pdfft') ? 30 : 0;
                      const recommendedPenalty = isRecommendedColumn ? 150 : 0;
                      return {el, text, href, rect, score: exactScore + topBarScore + hrefScore - recommendedPenalty - Math.max(0, rect.top / 1000)};
                    })
                    .filter(Boolean)
                    .sort((a, b) => b.score - a.score);
                  const target = controls[0];
                  if (!target || target.score < 0) return null;
                  const detail = {
                    selector: 'elsevier-view-pdf',
                    text: target.text.slice(0, 200),
                    href: target.href.slice(0, 500),
                    score: Math.round(target.score)
                  };
                  target.el.click();
                  return detail;
                }
                """
            )
            if isinstance(detail, dict) and detail.get("text"):
                self._event(result, "pdf_button_clicked", json.dumps(detail, ensure_ascii=False))
                return True
        except Exception as exc:
            self._event(result, "pdf_button_error", f"{type(exc).__name__}: {exc}")
        return False

    def _click_acs_current_doi_pdf_entry(self, page: Any, result: DownloadResult, doi: str) -> bool:
        try:
            detail = page.evaluate(
                """
                (doi) => {
                  const norm = (value) => (value || '').toString().replace(/\\s+/g, ' ').trim();
                  const visible = (el) => {
                    const rect = el.getBoundingClientRect();
                    const style = window.getComputedStyle(el);
                    return rect.width > 0 && rect.height > 0 && style.visibility !== 'hidden' && style.display !== 'none';
                  };
                  const hrefOf = (el) => {
                    const raw = el.href || el.getAttribute('href') || el.getAttribute('formaction') || '';
                    if (!raw) return '';
                    try { return new URL(raw, location.href).href; } catch { return raw; }
                  };
                  const doiLower = doi.toLowerCase();
                  const doiEscaped = encodeURIComponent(doi).toLowerCase();
                  const controls = [...document.querySelectorAll('a,button,[role="button"]')]
                    .filter(visible)
                    .map((el) => {
                      const rect = el.getBoundingClientRect();
                      const text = norm(el.innerText || el.textContent || el.getAttribute('aria-label') || el.getAttribute('title') || '');
                      const href = hrefOf(el);
                      const lowerHref = href.toLowerCase();
                      const haystack = `${text} ${href} ${el.className || ''} ${el.id || ''}`.toLowerCase();
                      const currentDoi = lowerHref.includes(doiLower) || lowerHref.includes(doiEscaped);
                      const currentPdf = currentDoi && (lowerHref.includes('/doi/pdf/') || lowerHref.includes('/doi/epdf/'));
                      if (!currentPdf) return null;
                      if (/supporting|supplement|references|citation/i.test(haystack)) return null;
                      const textScore = /^pdf$/i.test(text) ? 120 : (/pdf/i.test(text) ? 70 : 0);
                      const hrefScore = lowerHref.includes('/doi/pdf/') ? 50 : 25;
                      const topScore = rect.top < window.innerHeight * 0.70 ? 10 : 0;
                      return {
                        text,
                        href,
                        x: rect.left + rect.width / 2,
                        y: rect.top + rect.height / 2,
                        score: textScore + hrefScore + topScore - Math.max(0, rect.top / 1000)
                      };
                    })
                    .filter(Boolean)
                    .sort((a, b) => b.score - a.score);
                  const target = controls[0];
                  if (!target) return null;
                  return {
                    selector: 'acs-current-doi-pdf',
                    text: target.text.slice(0, 200),
                    href: target.href.slice(0, 500),
                    x: target.x,
                    y: target.y,
                    score: Math.round(target.score)
                  };
                }
                """,
                doi,
            )
            if not (isinstance(detail, dict) and detail.get("href")):
                return False
            x = detail.get("x")
            y = detail.get("y")
            if isinstance(x, (int, float)) and isinstance(y, (int, float)) and hasattr(page, "mouse"):
                page.mouse.click(float(x), float(y))
            else:
                page.evaluate(
                    """
                    (href) => {
                      const target = [...document.querySelectorAll('a,button,[role="button"]')]
                        .find((el) => ((el.href || el.getAttribute('href') || el.getAttribute('formaction') || '') + '') === href);
                      if (!target) return false;
                      target.click();
                      return true;
                    }
                    """,
                    str(detail["href"]),
                )
            self._event(result, "pdf_button_clicked", json.dumps(detail, ensure_ascii=False))
            return True
        except Exception as exc:
            self._event(result, "pdf_button_error", f"{type(exc).__name__}: {exc}")
        return False

    def _click_current_doi_pdf_entry(self, page: Any, result: DownloadResult, doi: str) -> bool:
        try:
            detail = page.evaluate(
                """
                (doi) => {
                  const norm = (value) => (value || '').toString().replace(/\\s+/g, ' ').trim();
                  const visible = (el) => {
                    const rect = el.getBoundingClientRect();
                    const style = window.getComputedStyle(el);
                    return rect.width > 0 && rect.height > 0 && style.visibility !== 'hidden' && style.display !== 'none';
                  };
                  const hrefOf = (el) => {
                    const raw = el.href || el.getAttribute('href') || el.getAttribute('formaction') || '';
                    if (!raw) return '';
                    try { return new URL(raw, location.href).href; } catch { return raw; }
                  };
                  const doiLower = doi.toLowerCase();
                  const doiEscaped = encodeURIComponent(doi).toLowerCase();
                  const controls = [...document.querySelectorAll('a,button,[role="button"],input[type="button"],input[type="submit"]')]
                    .filter(visible)
                    .map((el) => {
                      const rect = el.getBoundingClientRect();
                      const text = norm(el.innerText || el.textContent || el.value || el.getAttribute('aria-label') || el.getAttribute('title') || '');
                      const href = hrefOf(el);
                      const haystack = `${text} ${href} ${el.className || ''} ${el.id || ''}`.toLowerCase();
                      const isCurrentPdf = href.toLowerCase().includes('/pdf/')
                        && (href.toLowerCase().includes(doiLower) || href.toLowerCase().includes(doiEscaped));
                      if (!isCurrentPdf) return null;
                      const textScore = /^pdf$/i.test(text) ? 100 : 0;
                      const referencePenalty = /references|citation/i.test(text) ? 50 : 0;
                      const primaryScore = /primary|button|pdf/i.test(`${el.className || ''} ${el.id || ''}`) ? 20 : 0;
                      return {el, text, href, rect, score: textScore + primaryScore - referencePenalty - Math.max(0, rect.y / 1000)};
                    })
                    .filter(Boolean)
                    .sort((a, b) => b.score - a.score);
                  const target = controls[0];
                  if (!target) return null;
                  return {
                    selector: 'aps-current-doi-pdf',
                    text: target.text.slice(0, 200),
                    href: target.href.slice(0, 300)
                  };
                }
                """,
                doi,
            )
            if isinstance(detail, dict) and detail.get("href"):
                try:
                    page.goto(str(detail["href"]), wait_until="commit", timeout=self.pdf_timeout_ms)
                except Exception as exc:
                    if not self._is_download_navigation_abort(exc):
                        raise
                self._event(result, "pdf_button_clicked", json.dumps(detail, ensure_ascii=False))
                return True
        except Exception as exc:
            self._event(result, "pdf_button_error", f"{type(exc).__name__}: {exc}")
        return False

    def _fetch_page_state_pdf(
        self,
        page: Any,
        response: Any | None = None,
        extra_urls: list[str] | None = None,
        doi: str = "",
    ) -> tuple[bytes | None, str]:
        for fallback_url in self._page_state_pdf_urls(page, response, extra_urls):
            if not self._is_pdf_candidate_url(fallback_url) or self._is_supplementary_url(fallback_url):
                continue
            if not self._is_record_pdf_url(fallback_url, doi):
                continue
            body, final_url = self._fetch_pdf_url_with_browser_state(fallback_url, page)
            if body:
                return body, final_url
        return None, ""

    def _page_state_pdf_urls(
        self,
        page: Any,
        response: Any | None = None,
        extra_urls: list[str] | None = None,
    ) -> list[str]:
        raw_values: list[str] = list(extra_urls or [])
        if response is not None:
            raw_values.append(str(getattr(response, "url", "") or ""))
        raw_values.append(str(getattr(page, "url", "") or ""))
        raw_values.append(self._title(page))

        urls: list[str] = []
        for value in raw_values:
            if value.startswith("http"):
                urls.append(value)
            urls.extend(match.group(0).rstrip(").,;") for match in PDF_URL_RE.finditer(value))
        return list(dict.fromkeys(url for url in urls if url.startswith("http")))

    def _should_defer_response_body(self, url: str) -> bool:
        return self.profile.name.lower() == "elsevier" and self._is_pdf_candidate_url(url)

    def _capture_browser_download(self, page: Any, pdf_url: str, result: DownloadResult) -> tuple[bytes | None, str]:
        expect_download = getattr(page, "expect_download", None)
        if expect_download is None:
            return None, pdf_url
        try:
            with expect_download(timeout=self.pdf_timeout_ms) as download_info:
                try:
                    page.goto(pdf_url, wait_until="commit", timeout=self.pdf_timeout_ms)
                except Exception as exc:
                    if not self._is_download_navigation_abort(exc):
                        raise
            download = download_info.value
            path = download.path()
            body = Path(path).read_bytes()
            if is_plausible_pdf_bytes(body):
                return body, str(getattr(download, "url", "") or pdf_url)
        except Exception as exc:
            self._event(result, "download_capture_error", f"{type(exc).__name__}: {exc}")
        return None, pdf_url

    def _capture_pdf_viewer_download(self, page: Any, result: DownloadResult) -> tuple[bytes | None, str]:
        expect_download = getattr(page, "expect_download", None)
        if expect_download is None:
            return None, ""

        script = """
            (options) => {
              const click = !!(options && options.click);
              const norm = (value) => (value || '').toString().replace(/\\s+/g, ' ').trim();
              const visible = (el) => {
                const rect = el.getBoundingClientRect();
                const style = window.getComputedStyle(el);
                return rect.width > 0 && rect.height > 0 && style.visibility !== 'hidden' && style.display !== 'none';
              };
              const controls = [];
              const visit = (root) => {
                if (!root) return;
                for (const el of root.querySelectorAll('*')) {
                  if (el.shadowRoot) visit(el.shadowRoot);
                  const text = norm([
                    el.innerText || '',
                    el.textContent || '',
                    el.value || '',
                    el.getAttribute('aria-label') || '',
                    el.getAttribute('title') || '',
                    el.id || '',
                    el.className || ''
                  ].join(' '));
                  const lower = text.toLowerCase();
                  const isControl = ['BUTTON', 'A'].includes(el.tagName) || el.getAttribute('role') === 'button';
                  if (!isControl || !visible(el)) continue;
                  if (!lower.includes('download') && !lower.includes('下载')) continue;
                  if (lower.includes('print') || lower.includes('打印')) continue;
                  const rect = el.getBoundingClientRect();
                  controls.push({el, text, rect, score: (el.id || '').toLowerCase().includes('download') ? 100 : 10});
                }
              };
              visit(document);
              controls.sort((a, b) => b.score - a.score || a.rect.top - b.rect.top);
              const target = controls[0];
              if (!target) return null;
              if (click) target.el.click();
              return {selector: 'pdf-viewer-download', text: target.text.slice(0, 200), score: Math.round(target.score)};
            }
        """
        try:
            detail = page.evaluate(script, {"click": False})
            if not isinstance(detail, dict) or not detail.get("selector"):
                return self._capture_pdf_viewer_toolbar_download(page, result)
            try:
                with expect_download(timeout=self.pdf_timeout_ms) as download_info:
                    page.evaluate(script, {"click": True})
                download = download_info.value
                path = download.path()
                body = Path(path).read_bytes()
                if is_plausible_pdf_bytes(body):
                    self._event(result, "pdf_viewer_download_captured", json.dumps(detail, ensure_ascii=False))
                    return body, str(getattr(download, "url", "") or getattr(page, "url", "") or "")
            except Exception as exc:
                self._event(result, "pdf_viewer_download_error", f"{type(exc).__name__}: {exc}")
        except Exception as exc:
            self._event(result, "pdf_viewer_download_error", f"{type(exc).__name__}: {exc}")
        return self._capture_pdf_viewer_toolbar_download(page, result)

    def _capture_pdf_viewer_toolbar_download(self, page: Any, result: DownloadResult) -> tuple[bytes | None, str]:
        expect_download = getattr(page, "expect_download", None)
        mouse = getattr(page, "mouse", None)
        if expect_download is None or mouse is None or not hasattr(mouse, "click"):
            return None, ""
        try:
            size = page.evaluate("() => ({width: window.innerWidth, height: window.innerHeight})")
        except Exception:
            size = {}
        try:
            width = int((size or {}).get("width") or 1920)
        except Exception:
            width = 1920
        x = max(40, width - 102)
        y = 28
        try:
            with expect_download(timeout=self.pdf_timeout_ms) as download_info:
                mouse.click(x, y)
            download = download_info.value
            path = download.path()
            body = Path(path).read_bytes()
            if is_plausible_pdf_bytes(body):
                detail = {"selector": "pdf-viewer-toolbar-download", "x": x, "y": y}
                self._event(result, "pdf_viewer_toolbar_download_captured", json.dumps(detail, ensure_ascii=False))
                return body, str(getattr(download, "url", "") or getattr(page, "url", "") or "")
        except Exception as exc:
            self._event(result, "pdf_viewer_toolbar_download_error", f"{type(exc).__name__}: {exc}")
        return None, ""

    def _capture_pdf_via_async_navigation(
        self,
        page: Any,
        pdf_url: str,
        result: DownloadResult,
    ) -> tuple[bytes | None, str]:
        try:
            page.evaluate("(url) => { window.location.href = url; }", pdf_url)
            self._event(result, "pdf_async_navigation", pdf_url)
        except Exception as exc:
            self._event(result, "pdf_async_navigation_error", f"{type(exc).__name__}: {exc}")
            return None, ""

        timeout_sec = max(10, int(self.pdf_timeout_ms / 1000))
        deadline = time.time() + timeout_sec
        attempted: set[str] = set()
        viewer_download_attempted = False
        while time.time() < deadline:
            if self._is_challenge_page(page):
                if not self._wait_for_challenge_with_deadline(page, result, deadline):
                    return None, str(getattr(page, "url", "") or "")
                attempted.clear()
                viewer_download_attempted = False
                continue
            for candidate_url in self._page_state_pdf_urls(page):
                if candidate_url in attempted:
                    continue
                attempted.add(candidate_url)
                if not self._is_pdf_candidate_url(candidate_url) or self._is_supplementary_url(candidate_url):
                    continue
                self._event(result, "pdf_state_candidate", candidate_url)
                body, final_url = self._fetch_pdf_url_with_browser_state(candidate_url, page)
                if body:
                    return body, final_url
            if not viewer_download_attempted and self._page_state_pdf_urls(page):
                viewer_download_attempted = True
                body, final_url = self._capture_pdf_viewer_download(page, result)
                if body:
                    return body, final_url
            time.sleep(2)
        return None, ""

    def _should_use_async_pdf_navigation(self, url: str) -> bool:
        return self.profile.name.lower() == "elsevier" and self._is_pdf_candidate_url(url)

    @staticmethod
    def _is_download_navigation_abort(exc: Exception) -> bool:
        message = str(exc)
        return "Download is starting" in message or "net::ERR_ABORTED" in message

    def _fetch_pdf_url_with_browser_state(self, url: str, page: Any) -> tuple[bytes | None, str]:
        try:
            signature = inspect.signature(self._fetch_pdf_url)
        except (TypeError, ValueError):
            return self._fetch_pdf_url(url)
        accepts_page = "page" in signature.parameters or any(
            param.kind == inspect.Parameter.VAR_KEYWORD for param in signature.parameters.values()
        )
        if accepts_page:
            return self._fetch_pdf_url(url, page=page)
        return self._fetch_pdf_url(url)

    def _wait_for_challenge_with_deadline(
        self,
        page: Any,
        result: DownloadResult,
        deadline: float,
    ) -> bool:
        try:
            signature = inspect.signature(self._wait_for_challenge)
        except (TypeError, ValueError):
            return self._wait_for_challenge(page, result)
        accepts_deadline = "deadline" in signature.parameters or any(
            param.kind == inspect.Parameter.VAR_KEYWORD for param in signature.parameters.values()
        )
        if accepts_deadline:
            return self._wait_for_challenge(page, result, deadline=deadline)
        return self._wait_for_challenge(page, result)

    def _fetch_pdf_url(self, url: str, *, page: Any | None = None) -> tuple[bytes | None, str]:
        headers = self._pdf_request_headers(url, page)
        try:
            resp = requests.get(
                url,
                headers=headers,
                timeout=(10, 60),
                allow_redirects=True,
            )
            body = resp.content
            if is_plausible_pdf_bytes(body):
                return body, resp.url
        except Exception:
            return None, url
        return None, resp.url

    def _pdf_request_headers(self, url: str, page: Any | None = None) -> dict[str, str]:
        headers: dict[str, str] = {"User-Agent": "instsci/0.1"}
        if page is None:
            return headers

        try:
            user_agent = page.evaluate("() => navigator.userAgent")
            if isinstance(user_agent, str) and user_agent.strip():
                headers["User-Agent"] = user_agent.strip()
        except Exception:
            pass

        referer = str(getattr(page, "url", "") or "")
        if referer.startswith("http"):
            headers["Referer"] = referer

        try:
            context = getattr(page, "context", None)
            cookies = context.cookies(url) if context is not None else []
            cookie_header = "; ".join(
                f"{cookie.get('name')}={cookie.get('value')}"
                for cookie in cookies
                if cookie.get("name") and cookie.get("value") is not None
            )
            if cookie_header:
                headers["Cookie"] = cookie_header
        except Exception:
            pass
        return headers

    def _supplementary_candidates(self, page: Any, doi: str) -> list[str]:
        if not self.include_supplementary:
            return []
        candidates: list[str] = []
        try:
            found = page.evaluate(
                """
                (rules) => {
                  const out = [];
                  const supplementaryMarkers = rules.supplementaryMarkers.map(marker => marker.toLowerCase());
                  const textMarkers = [
                    'appendix',
                    'appendices',
                    'additional file',
                    'electronic supplementary information',
                    'supplement',
                    'supplemental',
                    'supplementary',
                    'supporting information',
                    'supporting material'
                  ];
                  const pushUrl = (raw, supplementary) => {
                    if (!raw) return;
                    let url = raw;
                    try { url = new URL(raw, location.href).href; } catch {}
                    out.push({url, supplementary: Boolean(supplementary)});
                  };
                  const textOf = (el) => [
                    el.innerText || '',
                    el.textContent || '',
                    el.getAttribute('aria-label') || '',
                    el.getAttribute('title') || '',
                    el.getAttribute('download') || ''
                  ].join(' ').replace(/\\s+/g, ' ').trim().toLowerCase();
                  for (const meta of document.querySelectorAll('meta')) {
                    const key = ((meta.getAttribute('name') || meta.getAttribute('property') || '') + '').toLowerCase();
                    const content = meta.getAttribute('content') || '';
                    const haystack = `${key} ${content}`.toLowerCase();
                    const supplementary = textMarkers.some(marker => haystack.includes(marker))
                      || supplementaryMarkers.some(marker => haystack.includes(marker));
                    if (supplementary) {
                      pushUrl(content, true);
                    }
                  }
                  for (const node of document.querySelectorAll('a,link,iframe,embed,object')) {
                    const raw = node.href || node.src || node.getAttribute('href') || node.getAttribute('src') || node.getAttribute('data') || '';
                    if (!raw) continue;
                    const href = raw.toLowerCase();
                    const haystack = `${href} ${textOf(node)}`;
                    const supplementary = textMarkers.some(marker => haystack.includes(marker))
                      || supplementaryMarkers.some(marker => haystack.includes(marker));
                    if (supplementary) {
                      pushUrl(raw, true);
                    }
                  }
                  const seen = new Set();
                  return out.filter((item) => {
                    if (!item.url || seen.has(item.url)) return false;
                    seen.add(item.url);
                    return true;
                  });
                }
                """,
                {"supplementaryMarkers": list(self.profile.supplementary_url_markers)},
            )
            if isinstance(found, list):
                for item in found:
                    if isinstance(item, dict):
                        url = str(item.get("url") or "")
                        supplementary = bool(item.get("supplementary"))
                    else:
                        url = str(item or "") if isinstance(item, str) else ""
                        supplementary = False
                    if not url.startswith("http"):
                        continue
                    if not supplementary and not self._is_supplementary_url(url):
                        continue
                    if not belongs_to_current_article(
                        self.profile,
                        url,
                        doi=doi,
                        source_url=str(getattr(page, "url", "") or ""),
                    ):
                        continue
                    candidates.append(url)
        except Exception:
            pass
        if self.supplementary_fast:
            candidates.extend(self._science_standard_supplementary_candidates(doi))
        return list(dict.fromkeys(candidates))

    def _science_standard_supplementary_candidates(self, doi: str) -> list[str]:
        if self.profile.name.lower() != "science":
            return []
        normalized = doi.strip()
        doi_suffix = normalized.rsplit("/", 1)[-1]
        suffix_lower = doi_suffix.lower()
        aaas_suffix_prefixes = (
            "science.",
            "sciadv.",
            "sciimmunol.",
            "scirobotics.",
            "scisignal.",
            "scitranslmed.",
        )
        if not normalized.lower().startswith("10.1126/"):
            return []
        if not doi_suffix or not suffix_lower.startswith(aaas_suffix_prefixes):
            return []
        filenames = [f"{doi_suffix}_sm.pdf"]
        short_suffix = doi_suffix.split(".", 1)[-1] if "." in doi_suffix else ""
        if short_suffix:
            filenames.append(f"{short_suffix}_sm.pdf")
        return [
            f"https://www.science.org/doi/suppl/{normalized}/suppl_file/{filename}"
            for filename in dict.fromkeys(filenames)
        ]

    def _download_supplementary_materials(
        self,
        page: Any,
        record: PaperRecord,
        run_dir: Path,
        result: DownloadResult,
        *,
        candidates: list[str] | None = None,
    ) -> list[str]:
        if not self.include_supplementary:
            return []
        urls = list(candidates) if candidates is not None else self._supplementary_candidates(page, record.doi)
        if not urls:
            return []
        supplementary_dir = run_dir / "supplementary" / safe_name(record.doi)
        supplementary_dir.mkdir(parents=True, exist_ok=True)
        saved_paths: list[str] = []
        saved_urls: list[str] = []
        seen_final_urls: set[str] = set()
        seen_body_hashes: set[str] = set()
        for url in urls:
            if self._is_page_anchor_only(url):
                continue
            body, final_url, content_type = self._fetch_supplementary_url_with_browser_state(url, page)
            if not body:
                body, final_url, content_type = self._fetch_supplementary_url_via_browser_click(url, page)
            if not body:
                self._event(result, "supplementary_download_failed", url)
                continue
            normalized_final_url = (final_url or url).strip()
            body_hash = hashlib.sha256(body).hexdigest()
            if normalized_final_url in seen_final_urls or body_hash in seen_body_hashes:
                self._event(result, "supplementary_duplicate_skipped", normalized_final_url or url)
                continue
            seen_final_urls.add(normalized_final_url)
            seen_body_hashes.add(body_hash)
            index = len(saved_paths) + 1
            filename = self._supplementary_filename(record.doi, index, final_url or url, content_type)
            path = supplementary_dir / filename
            path.write_bytes(body)
            saved_paths.append(str(path))
            saved_urls.append(final_url or url)
            self._event(result, "supplementary_downloaded", str(path))
        result.supplementary_paths = saved_paths
        result.supplementary_urls = saved_urls
        return saved_paths

    def _download_elsevier_api_supplementary_materials(
        self,
        record: PaperRecord,
        run_dir: Path,
        result: DownloadResult,
    ) -> list[str]:
        try:
            from .sources import elsevier_api
        except Exception as exc:
            result.reason = f"elsevier_api_unavailable: {type(exc).__name__}"
            return []

        api_key = elsevier_api.get_api_key(self.config.elsevier_api_key)
        if not api_key:
            result.reason = "elsevier_api_key_missing"
            return []

        try:
            objects = elsevier_api.fetch_supplementary_objects(
                record.doi,
                api_key=api_key,
                inst_token=self.config.elsevier_inst_token,
                proxy_url=self.config.proxy_url,
            )
        except Exception as exc:
            result.reason = f"elsevier_api_supplementary_error: {type(exc).__name__}: {exc}"
            return []

        if not objects:
            result.reason = "elsevier_api_supplementary_not_found"
            return []

        supplementary_dir = run_dir / "supplementary" / safe_name(record.doi)
        supplementary_dir.mkdir(parents=True, exist_ok=True)
        saved_paths: list[str] = []
        saved_urls: list[str] = []
        seen_body_hashes: set[str] = set()
        for obj in objects:
            body_hash = hashlib.sha256(obj.data).hexdigest()
            if body_hash in seen_body_hashes:
                self._event(result, "supplementary_duplicate_skipped", obj.eid)
                continue
            seen_body_hashes.add(body_hash)
            filename = self._safe_supplementary_asset_filename(obj.filename, len(saved_paths) + 1)
            path = supplementary_dir / filename
            path.write_bytes(obj.data)
            saved_paths.append(str(path))
            saved_urls.append(obj.url)
            self._event(result, "supplementary_downloaded", str(path))

        result.supplementary_paths = saved_paths
        result.supplementary_urls = saved_urls
        return saved_paths

    @staticmethod
    def _safe_supplementary_asset_filename(filename: str, index: int) -> str:
        safe = re.sub(r"[^A-Za-z0-9._-]+", "_", filename.strip())
        if not safe:
            return f"supplementary_{index:02d}.bin"
        if "." not in Path(safe).name:
            return f"{safe}.bin"
        return safe

    @staticmethod
    def _is_page_anchor_only(url: str) -> bool:
        parsed = urlparse(url)
        if not parsed.fragment:
            return False
        path = parsed.path.lower()
        downloadable_suffixes = (
            ".pdf",
            ".zip",
            ".doc",
            ".docx",
            ".xls",
            ".xlsx",
            ".csv",
            ".txt",
            ".ppt",
            ".pptx",
            ".png",
            ".jpg",
            ".jpeg",
            ".tif",
            ".tiff",
        )
        return not path.endswith(downloadable_suffixes)

    def _fetch_supplementary_url_with_browser_state(self, url: str, page: Any) -> tuple[bytes | None, str, str]:
        try:
            signature = inspect.signature(self._fetch_supplementary_url)
        except (TypeError, ValueError):
            return self._fetch_supplementary_url(url)
        accepts_page = "page" in signature.parameters or any(
            param.kind == inspect.Parameter.VAR_KEYWORD for param in signature.parameters.values()
        )
        if accepts_page:
            return self._fetch_supplementary_url(url, page=page)
        return self._fetch_supplementary_url(url)

    def _fetch_supplementary_url(self, url: str, *, page: Any | None = None) -> tuple[bytes | None, str, str]:
        headers = self._pdf_request_headers(url, page)
        try:
            resp = requests.get(
                url,
                headers=headers,
                timeout=(10, 60),
                allow_redirects=True,
            )
            if resp.status_code >= 400:
                return None, resp.url, resp.headers.get("content-type", "")
            content_type = resp.headers.get("content-type", "")
            if "text/html" in content_type.lower() and not urlparse(resp.url).path.lower().endswith((".html", ".htm")):
                return None, resp.url, content_type
            if resp.content:
                return resp.content, resp.url, content_type
        except Exception:
            return None, url, ""
        return None, resp.url, resp.headers.get("content-type", "")

    def _supplementary_timeout_ms(self) -> int:
        timeout_ms = max(30_000, int(self.pdf_timeout_ms or 0))
        if self.supplementary_fast:
            return min(timeout_ms, 90_000)
        return timeout_ms

    def _fetch_supplementary_url_via_browser_click(
        self,
        url: str,
        page: Any | None,
    ) -> tuple[bytes | None, str, str]:
        if page is None:
            return None, url, ""
        required_attrs = ("evaluate", "locator", "expect_download")
        if any(not hasattr(page, attr) for attr in required_attrs):
            return None, url, ""

        captured: dict[str, Any] = {}

        def capture_pdf_response(response: Any) -> None:
            try:
                response_url = str(getattr(response, "url", "") or "")
                content_type = str(getattr(response, "headers", {}).get("content-type", "") or "")
                status = int(getattr(response, "status", 0) or 0)
                if status >= 400 or "application/pdf" not in content_type.lower():
                    return
                if not self._same_supplementary_download(url, response_url):
                    return
                body = response.body()
                if body:
                    captured["body"] = body
                    captured["url"] = response_url
                    captured["content_type"] = content_type
            except Exception:
                return

        attached = False
        try:
            if hasattr(page, "on"):
                page.on("response", capture_pdf_response)
                attached = True
            found = page.evaluate(
                """
                (target) => {
                  const attr = 'data-instsci-supplementary-click-target';
                  for (const node of document.querySelectorAll(`[${attr}]`)) {
                    node.removeAttribute(attr);
                  }
                  const normalize = (value) => {
                    try { return decodeURIComponent(value || '').toLowerCase(); }
                    catch { return String(value || '').toLowerCase(); }
                  };
                  const targetUrl = normalize(target);
                  const anchors = Array.from(document.querySelectorAll('a[href]'));
                  const match = anchors.find((anchor) => {
                    const href = anchor.href || '';
                    const raw = anchor.getAttribute('href') || '';
                    return normalize(href) === targetUrl || normalize(raw) === targetUrl;
                  });
                  if (!match) return false;
                  match.setAttribute(attr, '1');
                  try { match.scrollIntoView({block: 'center', inline: 'center'}); } catch {}
                  return true;
                }
                """,
                url,
            )
            timeout_ms = self._supplementary_timeout_ms()
            selectors: list[str] = []
            source_name = Path(urlparse(url).path).name
            if source_name:
                selectors.append(f'a[href*="{self._css_attr_value(source_name)}"]')
            source_path = urlparse(url).path
            if source_path:
                selectors.append(f'a[href*="{self._css_attr_value(source_path)}"]')
            if found:
                selectors.append('[data-instsci-supplementary-click-target="1"]')
            for selector in list(dict.fromkeys(selectors)):
                locator = page.locator(selector)
                try:
                    if hasattr(locator, "count") and locator.count() < 1:
                        continue
                except Exception:
                    pass
                temp_path: Path | None = None
                try:
                    with page.expect_download(timeout=timeout_ms) as download_info:
                        self._first_locator(locator).click(timeout=30_000)
                    download = download_info.value
                    suggested_filename = str(getattr(download, "suggested_filename", "") or "")
                    suffix = Path(suggested_filename).suffix or ".bin"
                    with tempfile.NamedTemporaryFile(delete=False, suffix=suffix) as tmp:
                        temp_path = Path(tmp.name)
                    download.save_as(str(temp_path))
                    body = temp_path.read_bytes()
                    final_url = str(getattr(download, "url", "") or captured.get("url") or url)
                    content_type = str(
                        captured.get("content_type")
                        or mimetypes.guess_type(suggested_filename)[0]
                        or mimetypes.guess_type(final_url)[0]
                        or ""
                    )
                    if body:
                        return body, final_url, content_type
                except Exception:
                    if captured.get("body"):
                        return (
                            captured["body"],
                            str(captured.get("url") or url),
                            str(captured.get("content_type") or ""),
                        )
                    try:
                        self._click_supplementary_link_with_script(page, url)
                    except Exception:
                        pass
                    try:
                        if hasattr(page, "wait_for_timeout"):
                            page.wait_for_timeout(min(30_000, timeout_ms))
                    except Exception:
                        pass
                    if captured.get("body"):
                        return (
                            captured["body"],
                            str(captured.get("url") or url),
                            str(captured.get("content_type") or ""),
                        )
                finally:
                    if temp_path is not None:
                        try:
                            temp_path.unlink(missing_ok=True)
                        except Exception:
                            pass
        finally:
            if attached and hasattr(page, "off"):
                try:
                    page.off("response", capture_pdf_response)
                except Exception:
                    pass
        return None, url, ""

    @staticmethod
    def _first_locator(locator: Any) -> Any:
        first = getattr(locator, "first", None)
        if first is None:
            return locator
        if callable(first):
            return first()
        return first

    @staticmethod
    def _click_supplementary_link_with_script(page: Any, url: str) -> bool:
        return bool(
            page.evaluate(
                """
                (target) => {
                  const normalize = (value) => {
                    try { return decodeURIComponent(value || '').toLowerCase(); }
                    catch { return String(value || '').toLowerCase(); }
                  };
                  let filename = '';
                  try { filename = new URL(target, location.href).pathname.split('/').pop().toLowerCase(); }
                  catch {}
                  const targetUrl = normalize(target);
                  const anchors = Array.from(document.querySelectorAll('a[href]'));
                  const match = anchors.find((anchor) => {
                    const href = anchor.href || '';
                    const raw = anchor.getAttribute('href') || '';
                    const haystack = `${normalize(href)} ${normalize(raw)}`;
                    return normalize(href) === targetUrl
                      || normalize(raw) === targetUrl
                      || (filename && haystack.includes(filename));
                  });
                  if (!match) return false;
                  try { match.scrollIntoView({block: 'center', inline: 'center'}); } catch {}
                  match.click();
                  return true;
                }
                """,
                url,
            )
        )

    @staticmethod
    def _same_supplementary_download(source_url: str, response_url: str) -> bool:
        source = source_url.lower()
        response = response_url.lower()
        if source == response:
            return True
        source_path = urlparse(source_url).path.lower()
        response_path = urlparse(response_url).path.lower()
        source_name = Path(source_path).name
        if source_name and source_name in response:
            return True
        if "downloadsupplement" in response:
            source_qs = parse_qs(urlparse(source_url).query)
            response_qs = parse_qs(urlparse(response_url).query)
            source_file = source_name or (source_qs.get("file") or [""])[0].lower()
            response_file = (response_qs.get("file") or [""])[0].lower()
            return bool(source_file and source_file == response_file)
        return False

    @staticmethod
    def _css_attr_value(value: str) -> str:
        return value.replace("\\", "\\\\").replace('"', '\\"')

    def _supplementary_filename(self, doi: str, index: int, url: str, content_type: str = "") -> str:
        path_suffix = Path(urlparse(url).path).suffix.lower()
        if not re.fullmatch(r"\.[a-z0-9]{1,8}", path_suffix or ""):
            guessed = mimetypes.guess_extension((content_type or "").split(";", 1)[0].strip().lower())
            path_suffix = guessed if guessed and re.fullmatch(r"\.[a-z0-9]{1,8}", guessed) else ".bin"
        return f"{safe_name(doi)}_supplementary_{index:02d}{path_suffix}"

    def _pdf_candidates(self, page: Any, doi: str) -> list[str]:
        candidates: list[str] = []
        try:
            found = page.evaluate(
                """
                (rules) => {
                  const out = [];
                  const urlMarkers = rules.urlMarkers.map(marker => marker.toLowerCase());
                  const textMarkers = rules.textMarkers.map(marker => marker.toLowerCase());
                  for (const meta of document.querySelectorAll('meta[name="citation_pdf_url"]')) {
                    if (meta.content) out.push(meta.content);
                  }
                  for (const a of document.querySelectorAll('a')) {
                    const href = a.href || '';
                    const lowerHref = href.toLowerCase();
                    const text = (a.textContent || '').toLowerCase();
                    const aria = (a.getAttribute('aria-label') || '').toLowerCase();
                    const title = (a.getAttribute('title') || '').toLowerCase();
                    if (href && (
                      urlMarkers.some(marker => lowerHref.includes(marker)) ||
                      textMarkers.some(marker => (
                        text.includes(marker) || aria.includes(marker) || title.includes(marker)
                      ))
                    )) out.push(href);
                  }
                  return Array.from(new Set(out));
                }
                """,
                {
                    "urlMarkers": list(self.profile.pdf_url_markers),
                    "textMarkers": list(self.profile.pdf_link_text_markers),
                },
            )
            if isinstance(found, list):
                candidates.extend(
                    str(url)
                    for url in found
                    if isinstance(url, str)
                    and url.startswith("http")
                    and self._is_pdf_candidate_url(url)
                    and not self._is_supplementary_url(url)
                )
        except Exception:
            pass

        return build_pdf_candidates(
            self.profile,
            doi,
            source_url=str(getattr(page, "url", "") or ""),
            discovered_urls=candidates,
        )

    def _filter_pdf_candidates_for_current_article(self, urls: list[str], page: Any, doi: str) -> list[str]:
        return build_pdf_candidates(
            self.profile,
            doi,
            source_url=str(getattr(page, "url", "") or ""),
            discovered_urls=urls,
        )

    @staticmethod
    def _extract_elsevier_pii(url: str) -> str:
        return extract_elsevier_pii(url)

    def _is_pdf_candidate_url(self, url: str) -> bool:
        return is_pdf_candidate_url(self.profile, url)

    def _is_supplementary_url(self, url: str) -> bool:
        return is_supplementary_url(self.profile, url)

    def _is_record_pdf_url(self, url: str, doi: str) -> bool:
        if not doi or self.profile.name.lower() != "wiley":
            return True
        if self._url_matches_record(url, doi):
            return True
        host = (urlparse(str(url or "")).netloc or "").lower()
        if self.profile.base_domains and not any(host == domain or host.endswith(f".{domain}") for domain in self.profile.base_domains):
            return False
        return False

    def _return_to_record_article_if_needed(self, page: Any, result: DownloadResult, doi: str) -> bool:
        if self.profile.name.lower() != "aps":
            return False
        current_url = str(getattr(page, "url", "") or "")
        if not current_url or self._url_matches_record(current_url, doi):
            return False
        host = (urlparse(current_url).netloc or "").lower()
        if not any(host == domain or host.endswith(f".{domain}") for domain in self.profile.base_domains):
            return False
        try:
            page.goto(result.article_url, wait_until="domcontentloaded", timeout=60_000)
            self._event(result, "record_article_return", result.article_url)
            time.sleep(3)
            self._wait_for_challenge(page, result)
            self._dismiss_cookie_banners(page, result)
            return True
        except Exception as exc:
            self._event(result, "record_article_return_error", f"{type(exc).__name__}: {exc}")
            return False

    @staticmethod
    def _url_matches_record(url: str, doi: str) -> bool:
        lower_url = str(url or "").lower()
        doi_lower = doi.lower()
        return doi_lower in lower_url or doi_lower.replace("/", "%2f") in lower_url

    def _wait_for_challenge(self, page: Any, result: DownloadResult, *, deadline: float | None = None) -> bool:
        wait_interval_sec = CHALLENGE_QUIET_WAIT_SEC
        started_at = time.time()
        if deadline is None:
            end_at = started_at + max(0.0, float(self.login_timeout_sec))
        else:
            end_at = deadline
        budget_sec = max(0.0, end_at - started_at)
        max_checks = max(1, int(math.ceil(budget_sec / wait_interval_sec))) if budget_sec else 1
        waited = False
        for index in range(max_checks):
            if self._is_challenge_page(page):
                if not waited:
                    self._event(result, "challenge_manual_wait", "complete verification in visible browser")
                    self._notify_manual_challenge(page, result)
                waited = True
                result.state = "challenge_or_viewer_timeout"
                self._event(result, "challenge_wait", str(index + 1))
                remaining = max(0.0, end_at - time.time())
                if remaining <= 0:
                    break
                # Some WAF pages restart if browser automation polls them too aggressively.
                time.sleep(min(wait_interval_sec, remaining))
                continue
            if waited:
                self._event(result, "challenge_resolved", getattr(page, "url", ""))
            return True
        cleared = not self._is_challenge_page(page)
        if cleared and waited:
            self._event(result, "challenge_resolved", getattr(page, "url", ""))
        return cleared

    def _notify_manual_challenge(self, page: Any, result: DownloadResult) -> bool:
        now = time.monotonic()
        if now - self._manual_challenge_alert_last_at < 300:
            return False
        self._manual_challenge_alert_last_at = now
        host = urlparse(str(getattr(page, "url", "") or "")).hostname or self.profile.name
        message = (
            f"{self.profile.name} \u51fa\u73b0 Cloudflare/CAPTCHA/\u771f\u4eba\u9a8c\u8bc1\uff0c"
            "\u9700\u8981\u4eba\u5de5\u5904\u7406\u3002\n\n"
            f"DOI: {result.doi or 'unknown'}\n"
            f"\u7ad9\u70b9: {host}\n\n"
            "\u8bf7\u5728\u53ef\u89c1 CloakBrowser \u7a97\u53e3\u4e2d\u5b8c\u6210\u9a8c\u8bc1\uff1b"
            "\u5b8c\u6210\u540e\u4e0d\u8981\u5173\u95ed\u6d4f\u89c8\u5668\uff0cInstSci \u4f1a\u7ee7\u7eed\u3002"
        )
        if show_desktop_alert("InstSci \u9700\u8981\u4eba\u5de5\u9a8c\u8bc1", message):
            self._event(result, "desktop_alert", "manual_challenge")
            return True
        return False

    def _is_challenge_page(self, page: Any) -> bool:
        haystack = f"{self._title(page)} {self._body_text(page, 1_200)}".lower()
        direct_markers = (
            "just a moment",
            "attention required",
            "verify you are human",
            "\u8bf7\u9a8c\u8bc1\u60a8\u662f\u771f\u4eba",
            "checking your browser",
            "\u6b63\u5728\u8fdb\u884c\u5b89\u5168\u9a8c\u8bc1",
            "\u771f\u4eba\u9a8c\u8bc1",
            "are you a robot",
            "please confirm you are a human",
        )
        if any(marker in haystack for marker in direct_markers):
            return True
        return "cloudflare" in haystack and (
            "ray id:" in haystack
            or "security verification" in haystack
            or "security service" in haystack
            or "not a robot" in haystack
            or "\u5b89\u5168\u8d28\u8be2" in haystack
            or "\u5b89\u5168\u9a8c\u8bc1" in haystack
            or "\u771f\u4eba" in haystack
            or "瀹夊叏楠岃瘉" in haystack
        )

    def _looks_logged_out(self, page: Any) -> bool:
        url = getattr(page, "url", "").lower()
        title = self._title(page).lower()
        text = self._body_text(page, 5_000).lower()
        if any(marker in url for marker in self.profile.auth_url_markers):
            return True
        if any(marker in title for marker in self.profile.auth_url_markers):
            return True
        if any(marker.lower() in title for marker in self.profile.auth_title_markers):
            return True
        if self._elsevier_has_full_text_access(text):
            return False
        if self._elsevier_has_configured_institution_access_entry(text):
            return True
        if self._elsevier_lacks_pdf_entitlement(text):
            return True
        if self._has_publisher_institution_session(page):
            return False
        if self._is_success_article_url(getattr(page, "url", "")):
            # Hard paywall CTAs (Nature's "Buy article PDF USD 39.95") are a
            # login wall just as much as an SSO link — a paywall page must
            # send the flow to institutional login, not to the buy button
            # (#13). Soft merchandising/footer text like RSC's "purchase
            # options" or a stray OpenAthens help link deliberately matches
            # none of these: on an article URL only the hard walls decide.
            hard_wall_markers = (
                "access this article",
                "not registered by an institution",
                "authorization required",
                "provide your credentials",
                "get access",
                "log in via your institution",
                "log in via an institution",
                "access through your organization",
                "access through your institution",
                "access through institution",
                "institutional access",
                "no access",
                "purchase pdf",
                "purchase this article",
                "buy article pdf",
                "buy pdf",
                "buy this article",
                "add to cart",
                "shopping cart",
                "sign in to continue reading",
                "subscribe to unlock",
                "you do not have access to this pdf",
            )
            if any(marker in text for marker in hard_wall_markers):
                return True
            return bool(re.search(r"(?:usd|eur|gbp|cny|rmb)\s?\d|\$\s?\d+\.|￥\s?\d|¥\s?\d", text))
        return any(marker in text for marker in self.profile.sso_text_markers)

    def _article_access_available(self, page: Any) -> bool:
        return self._is_success_article_url(getattr(page, "url", "")) and not self._looks_logged_out(page)

    def _elsevier_has_full_text_access(self, text: str) -> bool:
        if self.profile.name.lower() != "elsevier":
            return False
        return "full text access" in text or "view pdf" in text

    def _elsevier_has_configured_institution_access_entry(self, text: str) -> bool:
        if self.profile.name.lower() != "elsevier":
            return False
        lower = text.lower()
        return "access through" in lower and self._text_mentions_configured_institution(lower)

    def _elsevier_lacks_pdf_entitlement(self, text: str) -> bool:
        if self.profile.name.lower() != "elsevier":
            return False
        lower = text.lower()
        if "purchase pdf" not in lower or "article preview" not in lower:
            return False
        return "brought to you by" in lower or self._text_mentions_configured_institution(lower)

    def _has_publisher_institution_session(self, page: Any) -> bool:
        current_url = str(getattr(page, "url", "") or "")
        host = (urlparse(current_url).netloc or "").lower()
        if self.profile.base_domains and not any(host == domain or host.endswith(f".{domain}") for domain in self.profile.base_domains):
            return False
        text = self._body_text(page, 5_000).lower()
        return (
            "brought to you by" in text
            or self._text_mentions_configured_institution(text)
        )

    def _text_mentions_configured_institution(self, text: str) -> bool:
        lower = str(text or "").lower()
        return any(alias.lower() in lower for alias in self.institution_aliases if alias)

    def _is_success_article_url(self, url: str) -> bool:
        lower = url.lower()
        if self.profile.name.lower() == "aps":
            parsed = urlparse(lower)
            host = parsed.netloc
            path = parsed.path
            if host.endswith("link.aps.org") and path.startswith("/doi/"):
                return True
            return host.endswith("journals.aps.org") and ("/abstract/" in path or "/pdf/" in path)
        return any(marker in lower for marker in self.profile.success_url_markers)

    @staticmethod
    def _title(page: Any) -> str:
        try:
            return str(page.title() or "")
        except Exception:
            return ""

    @staticmethod
    def _body_text(page: Any, limit: int = 2_000) -> str:
        try:
            text = page.locator("body").inner_text(timeout=3_000)
        except Exception:
            return ""
        return re.sub(r"\s+", " ", text).strip()[:limit]

    @staticmethod
    def _event(result: DownloadResult, state: str, detail: str = "") -> None:
        result.events.append({"state": state, "detail": detail[:500]})

    def _write_diagnostic(self, page: Any, result: DownloadResult, run_dir: Path) -> None:
        diag_dir = run_dir / "diagnostics" / safe_name(result.doi)
        diag_dir.mkdir(parents=True, exist_ok=True)
        result.final_url = getattr(page, "url", result.final_url)
        result.title = self._title(page)
        packet = {
            **asdict(result),
            "publisher": self.profile.name,
            "browser_profile_dir": str(Path(self.config.chrome_profile_dir)),
            "body_excerpt": self._body_text(page, 2_000),
            "created_at": datetime.now().isoformat(timespec="seconds"),
        }
        packet_path = diag_dir / "diagnostic.json"
        packet_path.write_text(json.dumps(packet, ensure_ascii=False, indent=2), encoding="utf-8")
        try:
            page.screenshot(path=str(diag_dir / "screenshot.png"), full_page=True)
        except Exception:
            pass
        result.diagnostic_path = str(packet_path)

    @staticmethod
    def _write_results(path: Path, results: list[DownloadResult]) -> None:
        summary = {
            "count": len(results),
            "success": sum(1 for result in results if result.status == "success"),
            "partial": sum(1 for result in results if result.status == "partial"),
            "failed": sum(1 for result in results if result.status == "failed"),
            "results": [asdict(result) for result in results],
        }
        path.write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")

    @staticmethod
    def _count_results(results: list[DownloadResult]) -> dict[str, int]:
        return {
            "count": len(results),
            "success": sum(1 for result in results if result.status == "success"),
            "failed": sum(1 for result in results if result.status == "failed"),
        }

    @staticmethod
    def _count_verified(results: list[DownloadResult]) -> int:
        return sum(1 for result in results if result.ok and result.verified_match)

    @staticmethod
    def _read_attempted_dois(path: Path) -> set[str]:
        if not path.exists():
            return set()
        attempted: set[str] = set()
        for line in path.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            try:
                item = json.loads(line)
            except json.JSONDecodeError:
                continue
            doi = str(item.get("doi", "")).strip().lower()
            if doi:
                attempted.add(doi)
        return attempted

    @staticmethod
    def _append_attempt(path: Path | None, result: DownloadResult, phase: str) -> None:
        if path is None:
            return
        path.parent.mkdir(parents=True, exist_ok=True)
        item = {
            "created_at": datetime.now().isoformat(timespec="seconds"),
            "phase": phase,
            "doi": result.doi,
            "status": result.status,
            "reason": result.reason,
            "state": result.state,
            "verified_match": result.verified_match,
            "size_bytes": result.size_bytes,
            "text_length": result.text_length,
            "pdf_path": result.pdf_path,
        }
        with path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(item, ensure_ascii=False) + "\n")

    def _write_complete_artifacts(
        self,
        records: list[PaperRecord],
        results: list[DownloadResult],
        run_dir: Path,
        *,
        missing_reasons: dict[str, str] | None = None,
    ) -> dict[str, Any]:
        complete_dir = run_dir / "complete"
        pdf_dir = complete_dir / "pdfs"
        supplementary_root = complete_dir / "supplementary"
        pdf_dir.mkdir(parents=True, exist_ok=True)
        missing_reasons = missing_reasons or {}
        result_by_doi = {result.doi.lower(): result for result in results if result.ok}
        manifest: list[dict[str, Any]] = []

        for record in records:
            result = result_by_doi.get(record.doi.lower())
            item = {
                "doi": record.doi,
                "published": record.published,
                "title": record.title,
                "status": "missing",
                "reason": missing_reasons.get(record.doi.lower(), ""),
                "pdf_path": "",
                "size_bytes": 0,
                "text_length": 0,
                "verified_match": False,
                "supplementary_count": 0,
                "supplementary_paths": [],
            }
            if result and result.pdf_path:
                src = Path(result.pdf_path)
                dst = pdf_dir / src.name
                if src.exists():
                    dst.write_bytes(src.read_bytes())
                    text = pdf_extractor.extract_text(dst)
                    verified_match = self._text_matches_record(text, record, fallback_title=result.title)
                    item.update(
                        {
                            "status": "success" if verified_match else "unverified",
                            "reason": result.reason,
                            "pdf_path": str(dst),
                            "size_bytes": dst.stat().st_size,
                            "text_length": len(text or ""),
                            "verified_match": verified_match,
                        }
                    )
            if result and result.supplementary_paths:
                supplementary_paths: list[str] = []
                for supplementary_path in result.supplementary_paths:
                    supplementary_src = Path(supplementary_path)
                    if not supplementary_src.exists():
                        continue
                    supplementary_dst_dir = supplementary_root / safe_name(record.doi)
                    supplementary_dst_dir.mkdir(parents=True, exist_ok=True)
                    supplementary_dst = supplementary_dst_dir / supplementary_src.name
                    supplementary_dst.write_bytes(supplementary_src.read_bytes())
                    supplementary_paths.append(str(supplementary_dst))
                item["supplementary_count"] = len(supplementary_paths)
                item["supplementary_paths"] = supplementary_paths
                if self.supplementary_only and supplementary_paths and not item["pdf_path"]:
                    item.update(
                        {
                            "status": "success",
                            "reason": result.reason,
                            "size_bytes": sum(Path(path).stat().st_size for path in supplementary_paths),
                        }
                    )
            manifest.append(item)

        complete_dir.mkdir(parents=True, exist_ok=True)
        (complete_dir / "manifest.json").write_text(
            json.dumps(manifest, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        with (complete_dir / "manifest.csv").open("w", newline="", encoding="utf-8-sig") as handle:
            writer = csv.DictWriter(handle, fieldnames=list(manifest[0].keys()) if manifest else [])
            writer.writeheader()
            writer.writerows(manifest)

        return {
            "count": len(manifest),
            "success": sum(1 for item in manifest if item["status"] == "success"),
            "missing": sum(1 for item in manifest if item["status"] == "missing"),
            "unverified": sum(1 for item in manifest if item["status"] == "unverified"),
            "verified_match": sum(1 for item in manifest if item["verified_match"]),
            "supplementary_count": sum(int(item.get("supplementary_count") or 0) for item in manifest),
            "pdf_dir": str(pdf_dir),
            "manifest": str(complete_dir / "manifest.csv"),
        }

    @staticmethod
    def _text_matches_record(text: str, record: PaperRecord, fallback_title: str = "") -> bool:
        lower = (text or "").lower()
        head = re.sub(r"\s+", " ", lower[:5_000]).strip()
        if any(marker in head for marker in NON_ARTICLE_PDF_MARKERS):
            return False
        doi = record.doi.lower()
        if doi and doi in lower:
            return True
        title_source = record.title.strip()
        if not title_source and fallback_title:
            title_source = re.split(r"\s+\|\s+|\s+-\s+", fallback_title.strip(), maxsplit=1)[0]
            if title_source.lower().startswith("loading "):
                title_source = ""
        stop_words = {
            "article",
            "journal",
            "journals",
            "research",
            "science",
            "sciencedirect",
            "annual",
            "reviews",
            "loading",
            "https",
            "content",
        }
        title_words = [
            word.lower()
            for word in re.findall(r"[A-Za-z0-9]{5,}", title_source)[:10]
            if word.lower() not in stop_words
        ]
        required_title_hits = min(3, len(title_words))
        if required_title_hits == 0:
            return False
        return sum(1 for word in title_words if word in lower) >= required_title_hits


class ACSCloakBatchDownloader(PublisherBatchDownloader):
    """Compatibility wrapper for the original ACS/EST downloader API."""

    def __init__(
        self,
        config: Config | None = None,
        *,
        institution_query: str = "",
        institution_aliases: tuple[str, ...] = (),
        login_timeout_sec: int = 900,
        pdf_timeout_sec: int = 60,
        post_login_hold_sec: int = 0,
        post_run_hold_sec: int = 0,
        carsi_portal_preauth: bool = False,
        include_supplementary: bool = False,
        supplementary_only: bool = False,
        supplementary_fast: bool = False,
    ) -> None:
        super().__init__(
            config,
            profile=ACS_PROFILE,
            institution_query=institution_query,
            institution_aliases=institution_aliases,
            login_timeout_sec=login_timeout_sec,
            pdf_timeout_sec=pdf_timeout_sec,
            post_login_hold_sec=post_login_hold_sec,
            post_run_hold_sec=post_run_hold_sec,
            carsi_portal_preauth=carsi_portal_preauth,
            include_supplementary=include_supplementary,
            supplementary_only=supplementary_only,
            supplementary_fast=supplementary_fast,
        )
