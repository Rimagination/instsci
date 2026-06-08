"""Browser profile session diagnostics without exposing cookie values."""

from __future__ import annotations

import shutil
import sqlite3
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .config import DEFAULT_BASE_DIR, Config


DEFAULT_SESSION_DOMAINS = (
    "tsinghua.edu.cn",
    "id.tsinghua.edu.cn",
    "idp.tsinghua.edu.cn",
    "openathens.net",
    "wayfinder.openathens.net",
    "connect.openathens.net",
    "login.openathens.net",
)

CHROME_EPOCH_OFFSET_SECONDS = 11_644_473_600


def chrome_time_to_iso(value: int | None) -> str:
    """Convert Chromium cookie expires_utc to ISO text.

    Chromium stores cookie expiry as microseconds since 1601-01-01 UTC.
    A value of 0 means the cookie expires when the browser session ends.
    """
    if not value:
        return "session"
    try:
        seconds = (int(value) / 1_000_000) - CHROME_EPOCH_OFFSET_SECONDS
        return datetime.fromtimestamp(seconds, tz=timezone.utc).isoformat(timespec="seconds")
    except Exception:
        return ""


def candidate_profile_dirs(config: Config, *, workspace: Path | None = None) -> list[Path]:
    """Return known local browser profile candidates in priority order."""
    candidates = [
        Path(config.chrome_profile_dir) if config.chrome_profile_dir else None,
        DEFAULT_BASE_DIR / "chrome-profile",
    ]
    if workspace is not None:
        candidates.append(workspace / ".chrome-sciencedirect")

    seen: set[str] = set()
    out: list[Path] = []
    for candidate in candidates:
        if candidate is None:
            continue
        resolved = str(candidate)
        if resolved in seen:
            continue
        seen.add(resolved)
        out.append(candidate)
    return out


def inspect_browser_profile(profile_dir: str | Path, domains: list[str] | tuple[str, ...]) -> dict[str, Any]:
    """Inspect cookie host presence for a Chromium profile.

    Cookie values are never read or returned. The report only includes host names
    and counts, enough to diagnose whether a profile likely contains a session.
    """
    profile = Path(profile_dir)
    cookies_db = profile / "Default" / "Network" / "Cookies"
    report: dict[str, Any] = {
        "profile_dir": str(profile),
        "exists": profile.exists(),
        "cookies_db": str(cookies_db),
        "cookies_db_exists": cookies_db.exists(),
        "domains": {},
        "error": "",
    }
    if not cookies_db.exists():
        return report

    try:
        with tempfile.TemporaryDirectory() as tmp:
            copy_path = Path(tmp) / "Cookies"
            shutil.copy2(cookies_db, copy_path)
            conn = sqlite3.connect(copy_path)
            try:
                columns = {
                    str(row[1])
                    for row in conn.execute("pragma table_info(cookies)").fetchall()
                }
                has_expires = "expires_utc" in columns
                for domain in domains:
                    if has_expires:
                        rows = conn.execute(
                            """
                            select
                              host_key,
                              count(*),
                              sum(case when expires_utc = 0 then 1 else 0 end),
                              sum(case when expires_utc != 0 then 1 else 0 end),
                              max(expires_utc),
                              sum(case
                                when expires_utc != 0 and expires_utc < ((strftime('%s','now') + ?) * 1000000)
                                then 1 else 0 end)
                            from cookies
                            where host_key like ?
                            group by host_key
                            order by host_key
                            """,
                            (CHROME_EPOCH_OFFSET_SECONDS, f"%{domain}"),
                        ).fetchall()
                        hosts = [
                            {
                                "host": row[0],
                                "cookie_count": int(row[1] or 0),
                                "session_cookie_count": int(row[2] or 0),
                                "persistent_cookie_count": int(row[3] or 0),
                                "latest_expires_utc": int(row[4] or 0),
                                "latest_expires_at": chrome_time_to_iso(int(row[4] or 0)),
                                "expired_cookie_count": int(row[5] or 0),
                            }
                            for row in rows
                        ]
                    else:
                        rows = conn.execute(
                            """
                            select host_key, count(*)
                            from cookies
                            where host_key like ?
                            group by host_key
                            order by host_key
                            """,
                            (f"%{domain}",),
                        ).fetchall()
                        hosts = [
                            {
                                "host": row[0],
                                "cookie_count": int(row[1]),
                                "session_cookie_count": 0,
                                "persistent_cookie_count": 0,
                                "latest_expires_utc": 0,
                                "latest_expires_at": "",
                                "expired_cookie_count": 0,
                            }
                            for row in rows
                        ]
                    report["domains"][domain] = {
                        "cookie_count": sum(int(host["cookie_count"]) for host in hosts),
                        "session_cookie_count": sum(int(host["session_cookie_count"]) for host in hosts),
                        "persistent_cookie_count": sum(int(host["persistent_cookie_count"]) for host in hosts),
                        "expired_cookie_count": sum(int(host["expired_cookie_count"]) for host in hosts),
                        "latest_expires_at": max(
                            (
                                str(host["latest_expires_at"])
                                for host in hosts
                                if host["latest_expires_at"] not in ("", "session")
                            ),
                            default="",
                        ),
                        "hosts": hosts,
                    }
            finally:
                conn.close()
    except Exception as exc:
        report["error"] = f"{type(exc).__name__}: {exc}"
    return report
