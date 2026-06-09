from __future__ import annotations

import argparse
import json
from pathlib import Path

import requests

from instsci.extractors import pdf_extractor


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--url", required=True)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--meta", required=True, type=Path)
    parser.add_argument("--doi", required=True)
    parser.add_argument("--verify-word", default="")
    parser.add_argument("--cookie", default="")
    args = parser.parse_args()

    headers = {"User-Agent": "Mozilla/5.0", "Accept": "application/pdf,*/*"}
    if args.cookie:
        headers["Cookie"] = args.cookie
    response = requests.get(
        args.url,
        timeout=60,
        headers=headers,
        allow_redirects=True,
    )
    print(response.status_code, response.url, response.headers.get("content-type"), len(response.content))
    response.raise_for_status()
    if not response.content.lstrip().startswith(b"%PDF-"):
        raise SystemExit("not a PDF")

    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_bytes(response.content)
    text = pdf_extractor.extract_text(args.output)
    verified = bool(args.verify_word and args.verify_word.lower() in (text or "").lower())
    args.meta.parent.mkdir(parents=True, exist_ok=True)
    args.meta.write_text(
        json.dumps(
            {
                "doi": args.doi,
                "source_url": response.url,
                "pdf_path": str(args.output),
                "size_bytes": args.output.stat().st_size,
                "text_length": len(text or ""),
                "verified_match": verified,
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )
    print(args.output)
    print(len(text or ""), verified)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
