from __future__ import annotations

import argparse
import csv
import json
import re
import shutil
from dataclasses import dataclass
from pathlib import Path


@dataclass
class Item:
    index: int
    doi: str
    title: str
    journal: str
    year: str
    priority: str
    status: str = "missing"
    result: str = "not_downloaded"
    evidence: str = ""
    pdf_path: str = ""
    source_pdf_path: str = ""
    size_bytes: int = 0
    text_length: int = 0
    verified_match: bool = False
    route_attempted: str = ""
    next_action: str = ""


def safe_piece(value: str, limit: int = 92) -> str:
    value = re.sub(r"\s+", " ", value or "").strip()
    value = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", value)
    value = re.sub(r"_+", "_", value).strip(" ._")
    return (value[:limit].rstrip(" ._") or "untitled")


def safe_doi(doi: str) -> str:
    return re.sub(r"[^\w.-]", "_", doi or "no_doi")


def normalize_doi(doi: str) -> str:
    doi = (doi or "").strip()
    doi = re.sub(r"^https?://(?:dx\.)?doi\.org/", "", doi, flags=re.I)
    return doi.rstrip(".,;")


def load_rows(csv_path: Path, doi_list: Path) -> dict[str, Item]:
    rows_by_doi: dict[str, dict[str, str]] = {}
    with csv_path.open("r", encoding="utf-8-sig", newline="") as handle:
        for row in csv.DictReader(handle):
            doi = normalize_doi(row.get("doi", ""))
            if doi:
                rows_by_doi.setdefault(doi.lower(), row)
    items: dict[str, Item] = {}
    for index, line in enumerate(doi_list.read_text(encoding="utf-8-sig").splitlines(), 1):
        doi = normalize_doi(line)
        if not doi or doi.startswith("#"):
            continue
        row = rows_by_doi.get(doi.lower(), {})
        items[doi.lower()] = Item(
            index=index,
            doi=doi,
            title=row.get("title", ""),
            journal=row.get("journal", ""),
            year=row.get("year", ""),
            priority=row.get("priority", ""),
        )
    return items


def copy_pdf(item: Item, src: Path, final_dir: Path) -> None:
    if not src.exists():
        return
    name = f"{item.index:03d}_{safe_doi(item.doi)}_{safe_piece(item.title)}.pdf"
    if len(name) > 180:
        name = f"{item.index:03d}_{safe_doi(item.doi)}.pdf"
    dst = final_dir / name
    final_dir.mkdir(parents=True, exist_ok=True)
    shutil.copy2(src, dst)
    item.pdf_path = str(dst)
    item.source_pdf_path = str(src)
    item.size_bytes = dst.stat().st_size


def apply_open_access(items: dict[str, Item], manifest: Path, final_dir: Path) -> None:
    if not manifest.exists():
        return
    with manifest.open("r", encoding="utf-8-sig", newline="") as handle:
        for row in csv.DictReader(handle):
            doi = normalize_doi(row.get("doi", ""))
            item = items.get(doi.lower())
            if not item or not row.get("pdf_path"):
                continue
            copy_pdf(item, Path(row["pdf_path"]), final_dir)
            item.status = "success"
            item.result = "open_access_downloaded"
            item.evidence = item.pdf_path
            item.route_attempted = "open_access_direct"
            item.text_length = int(row.get("text_length") or 0)
            item.verified_match = str(row.get("verified_match", "")).lower() == "true"


def apply_browser_run(items: dict[str, Item], run_dir: Path, publisher: str, final_dir: Path) -> None:
    manifest = run_dir / "complete" / "manifest.csv"
    if not manifest.exists():
        return
    with manifest.open("r", encoding="utf-8-sig", newline="") as handle:
        for row in csv.DictReader(handle):
            doi = normalize_doi(row.get("doi", ""))
            item = items.get(doi.lower())
            if not item:
                continue
            status = row.get("status", "")
            if row.get("pdf_path") and status in {"success", "unverified"}:
                copy_pdf(item, Path(row["pdf_path"]), final_dir)
                item.status = status
                item.result = f"browser_{status}_{publisher}"
                item.evidence = item.pdf_path
                item.route_attempted = f"visible_cloakbrowser_{publisher}"
                item.text_length = int(row.get("text_length") or 0)
                item.verified_match = str(row.get("verified_match", "")).lower() == "true"
                if status == "unverified":
                    item.next_action = "PDF downloaded, but automatic text/DOI match failed; inspect manually."
            elif status == "missing" and not item.pdf_path:
                item.status = "missing"
                item.result = f"browser_missing_{publisher}"
                item.route_attempted = f"visible_cloakbrowser_{publisher}"
                item.next_action = "Retry in visible CloakBrowser after completing SSO/CAPTCHA, or inspect diagnostics."


def apply_manual_meta(items: dict[str, Item], meta_path: Path, final_dir: Path, *, index: int | None = None, title: str = "", priority: str = "") -> None:
    if not meta_path.exists():
        return
    data = json.loads(meta_path.read_text(encoding="utf-8"))
    doi = normalize_doi(data.get("doi", ""))
    if not doi:
        return
    item = items.get(doi.lower())
    if item is None:
        item = Item(index=index or (max((x.index for x in items.values()), default=0) + 1), doi=doi, title=title, journal="", year="", priority=priority)
        items[doi.lower()] = item
    src = Path(data["pdf_path"])
    copy_pdf(item, src, final_dir)
    item.status = "success" if data.get("verified_match") else "unverified"
    item.result = "manual_downloaded" if item.status == "success" else "manual_unverified"
    item.evidence = item.pdf_path
    item.route_attempted = str(data.get("source", "manual_http_or_browser"))
    item.text_length = int(data.get("text_length") or 0)
    item.verified_match = bool(data.get("verified_match"))
    if item.status == "unverified":
        item.next_action = "PDF downloaded, but automatic text/DOI match failed; inspect manually."


def write_reports(items: dict[str, Item], output_root: Path) -> None:
    reports = output_root / "reports"
    reports.mkdir(parents=True, exist_ok=True)
    ordered = sorted(items.values(), key=lambda item: item.index)
    fieldnames = [
        "index",
        "priority",
        "year",
        "title",
        "journal",
        "doi",
        "status",
        "result",
        "verified_match",
        "size_bytes",
        "text_length",
        "pdf_path",
        "source_pdf_path",
        "route_attempted",
        "evidence",
        "next_action",
    ]
    with (reports / "final_manifest.csv").open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        writer.writeheader()
        for item in ordered:
            writer.writerow({key: getattr(item, key) for key in fieldnames})
    (reports / "final_manifest.json").write_text(
        json.dumps([item.__dict__ for item in ordered], ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    total = len(ordered)
    downloaded = sum(1 for item in ordered if item.pdf_path)
    verified = sum(1 for item in ordered if item.pdf_path and item.verified_match)
    unverified = sum(1 for item in ordered if item.pdf_path and not item.verified_match)
    missing = total - downloaded
    lines = [
        "# Final PDF download report",
        "",
        f"- Total mentioned records: {total}",
        f"- Downloaded PDFs: {downloaded}",
        f"- Verified PDFs: {verified}",
        f"- Downloaded but unverified: {unverified}",
        f"- Not downloaded: {missing}",
        "",
        "## Not downloaded",
        "",
    ]
    for item in ordered:
        if not item.pdf_path:
            lines.append(f"- {item.priority} {item.doi} - {item.title} ({item.result})")
    if unverified:
        lines.extend(["", "## Downloaded but unverified", ""])
        for item in ordered:
            if item.pdf_path and not item.verified_match:
                lines.append(f"- {item.priority} {item.doi} - {item.title} ({item.result})")
    (reports / "final_report.md").write_text("\n".join(lines) + "\n", encoding="utf-8")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--csv", required=True, type=Path)
    parser.add_argument("--doi-list", required=True, type=Path)
    parser.add_argument("--output-root", required=True, type=Path)
    args = parser.parse_args()
    final_dir = args.output_root / "final_pdfs"
    if final_dir.exists():
        shutil.rmtree(final_dir)
    items = load_rows(args.csv, args.doi_list)
    apply_open_access(items, args.output_root / "reports" / "download_manifest.csv", final_dir)
    for publisher in ["mdpi", "wiley", "iop", "elsevier", "acs", "pnas"]:
        apply_browser_run(items, args.output_root / "browser_runs" / publisher, publisher, final_dir)
    apply_manual_meta(items, args.output_root / "manual_pdfs" / "059_10.7275_15996616.json", final_dir)
    apply_manual_meta(items, args.output_root / "manual_pdfs" / "PNAS_10.1073_pnas.2501605122.json", final_dir)
    apply_manual_meta(items, args.output_root / "manual_pdfs" / "Authorea_10.22541_au.162300877.73953918_v1.json", final_dir)
    apply_manual_meta(items, args.output_root / "manual_pdfs" / "ESSOAr_10.22541_essoar.168298689.90273191_v1.json", final_dir)
    apply_manual_meta(
        items,
        args.output_root / "manual_pdfs" / "HAL_tel-04851385.json",
        final_dir,
        index=max((x.index for x in items.values()), default=0) + 1,
        title="Analyse des effets de l'ombrage combiné à du stress hydrique sur la croissance et la consommation en eau du maïs cultivé sous systèmes AgriVoltaïques",
        priority="B_relevant",
    )
    write_reports(items, args.output_root)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
