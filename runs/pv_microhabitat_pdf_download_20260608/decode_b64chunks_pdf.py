from __future__ import annotations

import argparse
import base64
import json
from pathlib import Path

from instsci.extractors import pdf_extractor


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--chunks", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--meta", required=True, type=Path)
    parser.add_argument("--doi", required=True)
    parser.add_argument("--verify-word", default="")
    args = parser.parse_args()

    args.output.parent.mkdir(parents=True, exist_ok=True)
    with args.chunks.open("r", encoding="ascii") as handle, args.output.open("wb") as out:
        for line in handle:
            line = line.strip()
            if line:
                out.write(base64.b64decode(line))

    text = pdf_extractor.extract_text(args.output)
    verified = bool(args.verify_word and args.verify_word.lower() in (text or "").lower())
    args.meta.parent.mkdir(parents=True, exist_ok=True)
    args.meta.write_text(
        json.dumps(
            {
                "doi": args.doi,
                "source": "browser_context_pdfdirect_chunks",
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
    print(args.output.stat().st_size, len(text or ""), verified)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
