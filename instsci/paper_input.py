"""Offline DOI-list ingestion shared by CLI batch entry points."""

import csv
import json
import re
from dataclasses import asdict, dataclass, field
from pathlib import Path
from urllib.parse import unquote, urlsplit

from .publisher_batch import PaperRecord


_DOI = re.compile(r"10\.\d{4,9}/[^\s\"{}]+", re.IGNORECASE)
_DOI_URL = re.compile(r"https?://(?:dx\.)?doi\.org/[^\s<>\"]+", re.IGNORECASE)
_COLUMNS = {
    "doi": "doi", "identifier": "doi", "doi号": "doi",
    "title": "title", "标题": "title", "题名": "title",
    "published": "published", "year": "published", "年份": "published",
    "url": "url", "article_url": "url",
}


@dataclass
class PaperInput:
    records: list[PaperRecord] = field(default_factory=list)
    unresolved: list[dict] = field(default_factory=list)
    duplicates: int = 0
    total: int = 0


def extract_doi(value: str) -> str:
    """Extract one DOI, retaining meaningful suffix case and balanced brackets."""
    value = _DOI_URL.sub(lambda m: unquote(urlsplit(m.group()).path.lstrip("/")), value)
    matches = {}
    for match in _DOI.finditer(value):
        # Adjacent citations or link wrappers must not become a fabricated DOI.
        if len(re.findall(r"10\.\d{4,9}/", match.group())) > 1:
            return ""
        doi = match.group().rstrip(".,")
        # Citation wrappers may end after the DOI; SICI DOIs can contain brackets.
        for opening, closing in (("(", ")"), ("[", "]"), ("<", ">")):
            while doi.endswith(closing) and doi.count(closing) > doi.count(opening):
                doi = doi[:-1]
        if doi.partition("/")[2]:
            matches.setdefault(doi.casefold(), doi)
    return next(iter(matches.values())) if len(matches) == 1 else ""


def read_paper_input(path: Path) -> PaperInput:
    """Read UTF-8 TXT/CSV/TSV, preserving unresolved rows and first-seen order.

    Tables accept doi/identifier, title, published/year and url columns. Headerless
    tables use their first column as the DOI; routing hints are not executed.
    """
    path = Path(path)
    if path.suffix.lower() not in {".txt", ".csv", ".tsv", ".tab", ""}:
        raise ValueError("Use a UTF-8 TXT, CSV or TSV file; export other formats to CSV first.")
    result = PaperInput()
    seen = {}
    with path.open(encoding="utf-8-sig", newline="") as stream:
        if path.suffix.lower() in {".csv", ".tsv", ".tab"}:
            delimiter = "," if path.suffix.lower() == ".csv" else "\t"
            reader = csv.reader(stream, delimiter=delimiter, strict=True)
            rows = ((reader.line_num, cells) for cells in reader)
        else:
            rows = ((number, [line.strip()]) for number, line in enumerate(stream, 1))
        columns = None
        first = True
        for number, cells in rows:
            if not cells or not any(cell.strip() for cell in cells):
                continue
            if columns is None and len(cells) == 1 and cells[0].lstrip().startswith("#"):
                continue
            if first:
                first = False
                names = [_COLUMNS.get(cell.strip().casefold(), "") for cell in cells]
                if "doi" in names:
                    if len(set(n for n in names if n)) != len([n for n in names if n]):
                        raise ValueError("Duplicate input columns; use one column per field.")
                    columns = names
                    continue
            result.total += 1
            if columns and len(cells) != len(columns):
                result.unresolved.append({"line": number, "cells": cells, "reason": "column_count_mismatch"})
                continue
            row = dict(zip(columns, cells)) if columns else {"doi": cells[0]}
            doi = extract_doi(row.get("doi", ""))
            if not doi:
                result.unresolved.append({"line": number, "cells": cells, "reason": "expected_one_doi"})
                continue
            record = PaperRecord(doi=doi, **{
                key: row.get(key, "").strip() for key in ("title", "published", "url")
            })
            key = doi.casefold()
            if key in seen:
                result.duplicates += 1
                for attr in ("title", "published", "url"):
                    if not getattr(seen[key], attr):
                        setattr(seen[key], attr, getattr(record, attr))
            else:
                seen[key] = record
                result.records.append(record)
    return result


def write_publisher_queue(parsed: PaperInput, output: Path) -> dict:
    """Export publisher groups for the existing visible-browser commands.

    Prefix inference is an offline routing hint, with no access verdict. Use a
    new directory so an earlier queue or the user's input cannot be overwritten.
    """
    from .publisher_profiles import PUBLISHER_PROFILES, infer_publisher_profile

    groups = {}
    keys = {profile: key for key, profile in PUBLISHER_PROFILES.items()}
    for record in parsed.records:
        key = keys.get(infer_publisher_profile(record.doi), "unknown")
        groups.setdefault(key, []).append(record)

    output = Path(output).resolve()
    output.mkdir(parents=True, exist_ok=False)
    manifest = {
        "schema_version": 1,
        "routing_basis": "offline_doi_prefix_prediction",
        "counts": {
            "input_rows": parsed.total,
            "unique_dois": len(parsed.records),
            "duplicates": parsed.duplicates,
            "unresolved": len(parsed.unresolved),
            "unknown_publisher": len(groups.get("unknown", [])),
        },
        "groups": [],
        "unresolved": parsed.unresolved,
    }
    for key, records in groups.items():
        path = output / f"{key}.csv"
        with path.open("w", encoding="utf-8", newline="") as stream:
            writer = csv.DictWriter(stream, fieldnames=["doi", "title", "published", "url"])
            writer.writeheader()
            writer.writerows(asdict(record) for record in records)
        group = {"publisher": key, "count": len(records), "input_file": str(path)}
        if key == "unknown":
            group["next_action"] = "Resolve the publisher and select --publisher explicitly."
        else:
            group["argv"] = [
                "instsci", "papers", str(path), "--publisher", key,
                "--output", str(output / "downloads" / key),
            ]
            group["next_action"] = "Run argv with your configured institution or add --institution."
        manifest["groups"].append(group)
    (output / "queue.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    return manifest
