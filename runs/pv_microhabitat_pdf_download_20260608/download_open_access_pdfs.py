from __future__ import annotations

import argparse
import csv
import hashlib
import json
import re
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from urllib.parse import urlparse

import requests

from instsci.extractors import pdf_extractor
from instsci.sources import unpaywall


PDF_MIN_BYTES = 5_000


@dataclass
class Record:
    index: int
    doi: str
    title: str = ""
    authors: str = ""
    year: str = ""
    journal: str = ""
    priority: str = ""
    pdf_url: str = ""
    is_oa: str = ""
    oa_status: str = ""
    url: str = ""


@dataclass
class Attempt:
    url: str
    source: str
    status: str
    detail: str = ""
    content_type: str = ""
    size_bytes: int = 0


@dataclass
class Result:
    index: int
    doi: str
    title: str
    journal: str
    year: str
    priority: str
    result: str
    route_attempted: str
    evidence: str = ""
    next_action: str = ""
    pdf_path: str = ""
    source_url: str = ""
    size_bytes: int = 0
    text_length: int = 0
    verified_match: bool = False
    attempts: list[Attempt] = field(default_factory=list)


def safe_piece(value: str, limit: int = 88) -> str:
    value = re.sub(r"\s+", " ", value or "").strip()
    value = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", value)
    value = re.sub(r"_+", "_", value).strip(" ._")
    return (value[:limit].rstrip(" ._") or "untitled")


def safe_doi(doi: str) -> str:
    return re.sub(r"[^\w.-]", "_", doi)


def normalize_doi(doi: str) -> str:
    doi = (doi or "").strip()
    doi = re.sub(r"^https?://(?:dx\.)?doi\.org/", "", doi, flags=re.I)
    return doi.rstrip(".,;")


def load_doi_list(path: Path) -> list[str]:
    seen: set[str] = set()
    dois: list[str] = []
    for line in path.read_text(encoding="utf-8-sig").splitlines():
        doi = normalize_doi(line)
        if not doi or doi.startswith("#"):
            continue
        key = doi.lower()
        if key not in seen:
            seen.add(key)
            dois.append(doi)
    return dois


def load_csv_records(csv_path: Path) -> dict[str, dict[str, str]]:
    records: dict[str, dict[str, str]] = {}
    with csv_path.open("r", encoding="utf-8-sig", newline="") as handle:
        reader = csv.DictReader(handle)
        for row in reader:
            doi = normalize_doi(row.get("doi", ""))
            if doi:
                records.setdefault(doi.lower(), row)
    return records


def record_from_row(index: int, doi: str, row: dict[str, str] | None) -> Record:
    row = row or {}
    return Record(
        index=index,
        doi=doi,
        title=row.get("title", ""),
        authors=row.get("authors", ""),
        year=row.get("year", ""),
        journal=row.get("journal", ""),
        priority=row.get("priority", ""),
        pdf_url=row.get("pdf_url", ""),
        is_oa=row.get("is_oa", ""),
        oa_status=row.get("oa_status", ""),
        url=row.get("url", ""),
    )


def extra_candidate_urls(record: Record) -> list[tuple[str, str]]:
    doi = record.doi
    lower = doi.lower()
    suffix = doi.split("/", 1)[-1] if "/" in doi else doi
    candidates: list[tuple[str, str]] = []
    if lower.startswith("10.3389/"):
        candidates.append((f"https://www.frontiersin.org/articles/{doi}/pdf", "frontiers_pattern"))
    if lower.startswith("10.1038/"):
        candidates.append((f"https://www.nature.com/articles/{suffix}.pdf", "nature_pattern"))
    if lower.startswith("10.1088/"):
        candidates.append((f"https://iopscience.iop.org/article/{doi}/pdf", "iop_pattern"))
    if lower.startswith("10.3390/"):
        candidates.append((f"https://www.mdpi.com/search?doi={doi}", "mdpi_landing"))
    if lower.startswith(("10.1002/", "10.1111/")):
        candidates.append((f"https://onlinelibrary.wiley.com/doi/pdfdirect/{doi}", "wiley_pattern"))
        candidates.append((f"https://onlinelibrary.wiley.com/doi/pdf/{doi}", "wiley_pattern"))
    if lower.startswith("10.1073/"):
        candidates.append((f"https://www.pnas.org/doi/epdf/{doi}", "pnas_pattern"))
        candidates.append((f"https://www.pnas.org/doi/pdf/{doi}", "pnas_pattern"))
    if lower.startswith("10.1186/"):
        candidates.append((f"https://link.springer.com/content/pdf/{doi}.pdf", "springer_pattern"))
    if lower.startswith("10.1007/"):
        candidates.append((f"https://link.springer.com/content/pdf/{doi}.pdf", "springer_pattern"))
    if lower.startswith("10.12911/"):
        candidates.append((f"https://www.jeeng.net/pdf-153456-82999?filename=Solar%20Park%20-%20Opportunity.pdf", "journal_pattern"))
    if lower.startswith("10.2172/"):
        candidates.append((f"https://www.osti.gov/servlets/purl/{suffix}", "osti_pattern"))
    return candidates


def unique_candidates(items: list[tuple[str, str]]) -> list[tuple[str, str]]:
    seen: set[str] = set()
    out: list[tuple[str, str]] = []
    for url, source in items:
        url = (url or "").strip()
        if not url:
            continue
        key = url.lower()
        if key not in seen:
            seen.add(key)
            out.append((url, source))
    return out


def build_candidates(record: Record, email: str) -> tuple[list[tuple[str, str]], dict[str, str]]:
    candidates: list[tuple[str, str]] = []
    oa_meta: dict[str, str] = {}
    if record.pdf_url:
        candidates.append((record.pdf_url, "csv_pdf_url"))
    try:
        oa = unpaywall.check_oa(record.doi, email=email)
        oa_meta = {
            "is_oa": str(oa.is_oa),
            "source": oa.source,
            "pdf_url": oa.pdf_url,
            "html_url": oa.html_url,
            "title": oa.title,
            "journal": oa.journal,
            "year": str(oa.year or ""),
        }
        if oa.pdf_url:
            candidates.append((oa.pdf_url, "unpaywall_pdf"))
        if oa.html_url and oa.html_url.lower().endswith(".pdf"):
            candidates.append((oa.html_url, "unpaywall_html_pdf"))
    except Exception as exc:
        oa_meta = {"error": f"{type(exc).__name__}: {exc}"}
    candidates.extend(extra_candidate_urls(record))
    return unique_candidates(candidates), oa_meta


def looks_like_pdf(content: bytes, content_type: str) -> bool:
    head = content[:1024].lstrip()
    return head.startswith(b"%PDF-") or (b"%PDF-" in head[:200] and len(content) >= PDF_MIN_BYTES) or (
        "pdf" in (content_type or "").lower() and len(content) >= PDF_MIN_BYTES
    )


def title_words(title: str) -> list[str]:
    stop = {
        "article",
        "journal",
        "research",
        "science",
        "photovoltaic",
        "solar",
        "effects",
        "effect",
        "using",
        "based",
        "review",
    }
    words = re.findall(r"[A-Za-z0-9]{5,}", title.lower())
    return [word for word in words if word not in stop][:10]


def verify_text(text: str, record: Record) -> bool:
    lower = (text or "").lower()
    if record.doi.lower() in lower:
        return True
    words = title_words(record.title)
    if not words:
        return len(text or "") >= 1_000
    required = min(3, len(words))
    return sum(1 for word in words if word in lower) >= required


def fetch_pdf(url: str, session: requests.Session, timeout: int) -> tuple[bytes | None, Attempt]:
    headers = {
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125 Safari/537.36"
        ),
        "Accept": "application/pdf,application/octet-stream;q=0.9,*/*;q=0.8",
    }
    try:
        response = session.get(url, headers=headers, timeout=timeout, allow_redirects=True)
        content_type = response.headers.get("content-type", "")
        body = response.content
        attempt = Attempt(
            url=response.url,
            source="",
            status=f"http_{response.status_code}",
            content_type=content_type,
            size_bytes=len(body),
        )
        if response.status_code >= 400:
            attempt.detail = response.text[:300] if "text" in content_type.lower() else ""
            return None, attempt
        if not looks_like_pdf(body, content_type):
            attempt.status = "not_pdf"
            attempt.detail = response.text[:300] if "text" in content_type.lower() else ""
            return None, attempt
        return body, attempt
    except Exception as exc:
        return None, Attempt(url=url, source="", status="error", detail=f"{type(exc).__name__}: {exc}")


def download_record(
    record: Record,
    output_root: Path,
    email: str,
    timeout: int,
    pause: float,
    session: requests.Session,
) -> tuple[Result, dict[str, str]]:
    result = Result(
        index=record.index,
        doi=record.doi,
        title=record.title,
        journal=record.journal,
        year=record.year,
        priority=record.priority,
        result="HTTP preflight",
        route_attempted="open_access_direct",
    )
    candidates, oa_meta = build_candidates(record, email)
    if not candidates:
        result.evidence = "No OA PDF URL found in CSV or Unpaywall"
        result.next_action = "Run InstSci visible CloakBrowser workflow if publisher access is needed"
        return result, oa_meta

    pdf_dir = output_root / "pdfs"
    pdf_dir.mkdir(parents=True, exist_ok=True)
    meta_dir = output_root / "metadata"
    meta_dir.mkdir(parents=True, exist_ok=True)

    for url, source in candidates:
        body, attempt = fetch_pdf(url, session, timeout)
        attempt.source = source
        result.attempts.append(attempt)
        time.sleep(pause)
        if body is None:
            continue

        text = pdf_extractor.extract_from_bytes(body)
        verified = verify_text(text, record)
        title_part = safe_piece(record.title)
        pdf_name = f"{record.index:03d}_{safe_doi(record.doi)}_{title_part}.pdf"
        pdf_path = pdf_dir / pdf_name
        if len(str(pdf_path)) > 235:
            digest = hashlib.sha1(record.doi.encode("utf-8")).hexdigest()[:10]
            pdf_path = pdf_dir / f"{record.index:03d}_{safe_doi(record.doi)}_{digest}.pdf"
        pdf_path.write_bytes(body)
        meta_path = meta_dir / f"{record.index:03d}_{safe_doi(record.doi)}.json"
        meta_path.write_text(
            json.dumps(
                {
                    "record": asdict(record),
                    "oa_meta": oa_meta,
                    "source_url": attempt.url,
                    "text_length": len(text or ""),
                    "verified_match": verified,
                    "attempts": [asdict(item) for item in result.attempts],
                },
                ensure_ascii=False,
                indent=2,
            ),
            encoding="utf-8",
        )
        result.result = "open_access_downloaded"
        result.evidence = str(pdf_path)
        result.pdf_path = str(pdf_path)
        result.source_url = attempt.url
        result.size_bytes = len(body)
        result.text_length = len(text or "")
        result.verified_match = verified
        result.next_action = "" if verified else "Inspect PDF manually; text/title match was not verified"
        return result, oa_meta

    result.evidence = "; ".join(
        f"{attempt.source}:{attempt.status}" for attempt in result.attempts[:5]
    )
    result.next_action = "Run InstSci visible CloakBrowser workflow for browser-verified publisher PDF evidence"
    return result, oa_meta


def write_reports(output_root: Path, results: list[Result], records: list[Record], oa_meta_by_doi: dict[str, dict[str, str]]) -> None:
    reports_dir = output_root / "reports"
    reports_dir.mkdir(parents=True, exist_ok=True)
    manifest_json = reports_dir / "download_manifest.json"
    manifest_csv = reports_dir / "download_manifest.csv"
    manifest_md = reports_dir / "download_report.md"
    need_browser = output_root / "need_cloakbrowser_verification_dois.txt"

    payload = []
    for result in results:
        item = asdict(result)
        item["attempts"] = [asdict(attempt) for attempt in result.attempts]
        item["oa_meta"] = oa_meta_by_doi.get(result.doi.lower(), {})
        payload.append(item)
    manifest_json.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")

    fieldnames = [
        "index",
        "priority",
        "year",
        "title",
        "journal",
        "doi",
        "result",
        "route_attempted",
        "verified_match",
        "size_bytes",
        "text_length",
        "pdf_path",
        "source_url",
        "evidence",
        "next_action",
    ]
    with manifest_csv.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        writer.writeheader()
        for result in results:
            writer.writerow({key: getattr(result, key) for key in fieldnames})

    missing = [result for result in results if not result.pdf_path]
    need_browser.write_text("\n".join(result.doi for result in missing) + ("\n" if missing else ""), encoding="utf-8")

    counts: dict[str, int] = {}
    for result in results:
        counts[result.result] = counts.get(result.result, 0) + 1
    lines = [
        "# PDF download report",
        "",
        f"- Total DOI records: {len(records)}",
        f"- Downloaded PDFs: {sum(1 for result in results if result.pdf_path)}",
        f"- Verified by text/DOI match: {sum(1 for result in results if result.verified_match)}",
        f"- Need CloakBrowser/publisher verification: {len(missing)}",
        "",
        "## Status counts",
        "",
    ]
    for key in sorted(counts):
        lines.append(f"- {key}: {counts[key]}")
    if missing:
        lines.extend(["", "## Need browser verification", ""])
        for result in missing:
            label = f"{result.priority} " if result.priority else ""
            lines.append(f"- {label}{result.doi} - {result.title or result.evidence}")
    manifest_md.write_text("\n".join(lines) + "\n", encoding="utf-8")


def load_existing_results(output_root: Path) -> dict[str, Result]:
    manifest_json = output_root / "reports" / "download_manifest.json"
    if not manifest_json.exists():
        return {}
    try:
        data = json.loads(manifest_json.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return {}
    existing: dict[str, Result] = {}
    for item in data:
        attempts = [Attempt(**attempt) for attempt in item.get("attempts", [])]
        clean = {
            key: value
            for key, value in item.items()
            if key in Result.__dataclass_fields__ and key != "attempts"
        }
        result = Result(**clean)
        result.attempts = attempts
        existing[result.doi.lower()] = result
    return existing


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--doi-list", required=True, type=Path)
    parser.add_argument("--csv", required=True, type=Path)
    parser.add_argument("--output-root", required=True, type=Path)
    parser.add_argument("--email", default="instsci@example.com")
    parser.add_argument("--timeout", type=int, default=60)
    parser.add_argument("--pause", type=float, default=0.25)
    parser.add_argument("--no-resume", action="store_true")
    args = parser.parse_args()

    args.output_root.mkdir(parents=True, exist_ok=True)
    dois = load_doi_list(args.doi_list)
    rows = load_csv_records(args.csv)
    records = [record_from_row(index, doi, rows.get(doi.lower())) for index, doi in enumerate(dois, 1)]

    session = requests.Session()
    results: list[Result] = []
    oa_meta_by_doi: dict[str, dict[str, str]] = {}
    existing = {} if args.no_resume else load_existing_results(args.output_root)
    for record in records:
        prior = existing.get(record.doi.lower())
        if prior is not None:
            results.append(prior)
            print(f"[{record.index}/{len(records)}] {record.doi} SKIP {prior.result}", flush=True)
            continue
        print(f"[{record.index}/{len(records)}] {record.doi} {record.title[:70]}", flush=True)
        result, oa_meta = download_record(
            record,
            args.output_root,
            args.email,
            args.timeout,
            args.pause,
            session,
        )
        results.append(result)
        oa_meta_by_doi[record.doi.lower()] = oa_meta
        print(f"  -> {result.result} {result.evidence[:120]}", flush=True)
        write_reports(args.output_root, results, records, oa_meta_by_doi)

    write_reports(args.output_root, results, records, oa_meta_by_doi)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
