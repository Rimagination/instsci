"""School database for multi-university WebVPN support."""

import json
from dataclasses import dataclass
from pathlib import Path

_DEFAULT_KEY = b"wrdvpnisthebest!"
WEBVPN_DEFAULT_KEY = _DEFAULT_KEY  # Public alias for CLI display

_DATA_FILE = Path(__file__).parent / "data" / "webvpn.json"


@dataclass
class SchoolEntry:
    """A university's WebVPN configuration."""

    name: str       # e.g. "清华大学"
    province: str   # e.g. "北京"
    host: str       # e.g. "https://webvpn.tsinghua.edu.cn"
    key: bytes      # AES encryption key
    iv: bytes       # AES encryption IV (often same as key)


def _load_db() -> dict:
    """Load the webvpn.json database."""
    if not _DATA_FILE.exists():
        return {}
    return json.loads(_DATA_FILE.read_text(encoding="utf-8"))


def _parse_entry(name: str, province: str, info: dict) -> SchoolEntry | None:
    """Parse a single school entry from the JSON database."""
    host = info.get("host", "").strip()
    if not host:
        return None

    # Ensure host has scheme
    if not host.startswith("http"):
        host = f"https://{host}"

    key_str = info.get("crypto_key", "")
    iv_str = info.get("crypto_iv", "")

    key = key_str.encode("utf-8") if key_str else _DEFAULT_KEY
    iv = iv_str.encode("utf-8") if iv_str else key

    return SchoolEntry(name=name, province=province, host=host, key=key, iv=iv)


def list_schools() -> list[SchoolEntry]:
    """List all schools in the database."""
    db = _load_db()
    result = []
    for province, schools in db.items():
        for name, info in schools.items():
            entry = _parse_entry(name, province, info)
            if entry:
                result.append(entry)
    return result


def search_schools(query: str) -> list[SchoolEntry]:
    """Search schools by name, province, or host.

    Supports partial matching. Returns matching schools.
    """
    query_lower = query.lower()
    results = []
    for entry in list_schools():
        if (query_lower in entry.name.lower()
                or query_lower in entry.province.lower()
                or query_lower in entry.host.lower()):
            results.append(entry)
    return results


def get_school(name: str) -> SchoolEntry:
    """Get a school by exact name or best fuzzy match.

    Raises ValueError if no match found.
    """
    schools = list_schools()

    # Exact match
    for s in schools:
        if s.name == name:
            return s

    # Partial match (name contains query)
    matches = [s for s in schools if name in s.name]
    if len(matches) == 1:
        return matches[0]
    if len(matches) > 1:
        # Return shortest match (most specific)
        matches.sort(key=lambda s: len(s.name))
        return matches[0]

    # Fuzzy: query contains school name
    for s in schools:
        if s.name in name:
            return s

    raise ValueError(
        f"School not found: '{name}'. "
        f"Use 'vpnsci schools' to list available schools."
    )
