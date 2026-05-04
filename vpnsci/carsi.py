"""CARSI (Shibboleth/SAML) federated authentication for publisher access."""

import json
import logging
import re
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlparse

import requests

from .config import Config

logger = logging.getLogger(__name__)

_PUBLISHER_CONFIGS_FILE = Path(__file__).parent / "data" / "publisher_carsi.json"


@dataclass
class PublisherCARSIConfig:
    name: str
    domains: list[str]
    login_url: str
    search_selector: str
    result_selector: str
    success_url_pattern: str
    pdf_pattern: str


def _load_publisher_configs() -> dict[str, PublisherCARSIConfig]:
    if not _PUBLISHER_CONFIGS_FILE.exists():
        return {}
    data = json.loads(_PUBLISHER_CONFIGS_FILE.read_text(encoding="utf-8"))
    configs = {}
    for key, val in data.items():
        configs[key] = PublisherCARSIConfig(**val)
    return configs


def detect_publisher(url: str) -> str | None:
    """Detect publisher key from a URL."""
    hostname = urlparse(url).hostname or ""
    configs = _load_publisher_configs()
    for key, cfg in configs.items():
        for domain in cfg.domains:
            if domain in hostname:
                return key
    return None


class CARSIClient:
    """Manages CARSI/Shibboleth federated authentication with academic publishers."""

    def __init__(self, config: Config):
        self.config = config
        self.config.ensure_dirs()
        self._sessions: dict[str, requests.Session] = {}
        self._publisher_configs = _load_publisher_configs()

    def _cookie_path(self, publisher: str) -> Path:
        return Path(self.config.carsi_cookie_dir) / f"{publisher}.json"

    def _get_session(self, publisher: str) -> requests.Session:
        if publisher not in self._sessions:
            sess = requests.Session()
            sess.headers.update({
                "User-Agent": (
                    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) "
                    "Chrome/120.0.0.0 Safari/537.36"
                )
            })
            self._sessions[publisher] = sess
        return self._sessions[publisher]

    def login(self, publisher: str, force: bool = False) -> bool:
        """Ensure we have a valid CARSI session for the given publisher."""
        if not force and self._try_load_cookies(publisher):
            logger.info("Loaded saved CARSI cookies for %s", publisher)
            return True
        logger.info("No valid CARSI session for %s. Opening browser...", publisher)
        return self._browser_login(publisher)

    def fetch(self, url: str, **kwargs) -> requests.Response:
        """Fetch a URL using CARSI-authenticated session."""
        publisher = detect_publisher(url)
        if publisher:
            self.login(publisher)
            sess = self._get_session(publisher)
        else:
            sess = self._get_session("_default")

        kwargs.setdefault("timeout", 30)
        kwargs.setdefault("allow_redirects", True)
        return sess.get(url, **kwargs)

    def _try_load_cookies(self, publisher: str) -> bool:
        cookie_file = self._cookie_path(publisher)
        if not cookie_file.exists():
            return False
        try:
            cookies = json.loads(cookie_file.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError) as e:
            logger.warning("Failed to read CARSI cookies for %s: %s", publisher, e)
            return False

        sess = self._get_session(publisher)
        for cookie in cookies:
            sess.cookies.set(
                cookie["name"],
                cookie["value"],
                domain=cookie.get("domain", ""),
                path=cookie.get("path", "/"),
            )
        return self._validate_session(publisher)

    def _validate_session(self, publisher: str) -> bool:
        cfg = self._publisher_configs.get(publisher)
        if not cfg:
            return False
        sess = self._get_session(publisher)
        try:
            resp = sess.get(cfg.login_url, timeout=15, allow_redirects=True)
            url_lower = resp.url.lower()
            if "login" in url_lower and "institutional" not in url_lower:
                return False
            if resp.status_code == 200 and "institutional-login" not in url_lower:
                return True
        except requests.RequestException as e:
            logger.warning("CARSI session validation failed for %s: %s", publisher, e)
        return False

    def _browser_login(self, publisher: str) -> bool:
        """Login via CARSI by opening the publisher's institutional login page in the default browser.

        Since Selenium-based approaches have visibility issues on some systems,
        we use the system's default browser and ask the user to complete the login manually.
        After login, the user pastes the final URL and we extract cookies from Chrome's profile.
        """
        import webbrowser

        cfg = self._publisher_configs.get(publisher)
        if not cfg:
            logger.error("Unknown publisher: %s", publisher)
            return False

        print("\n" + "=" * 60)
        print(f"  CARSI Login: {cfg.name}")
        print(f"  Opening your default browser...")
        print(f"  ")
        print(f"  Steps:")
        print(f"  1. Search for your university: {self.config.carsi_idp_name}")
        print(f"  2. Select it and log in with your campus credentials")
        print(f"  3. After login, you should be on the publisher's main site")
        print(f"  4. Copy the URL from your browser and paste it below")
        print("=" * 60 + "\n")

        webbrowser.open(cfg.login_url)

        print(f"  Browser opened at: {cfg.login_url}")
        print(f"  Waiting for you to complete login...")
        print()

        # Wait for user to complete login and paste the final URL
        try:
            final_url = input("  Paste the final URL after login (or press Enter to skip): ").strip()
        except (EOFError, KeyboardInterrupt):
            print("\n  Login cancelled.")
            return False

        if not final_url:
            print("  No URL provided. Login skipped.")
            return False

        # Check if the URL indicates successful login
        on_publisher = any(d in final_url for d in cfg.domains)
        on_login_page = any(x in final_url.lower() for x in ("login", "institutional", "wayf", "saml", "shibboleth"))

        if on_publisher and not on_login_page:
            logger.info("CARSI login confirmed. URL: %s", final_url)
            print("\n  CARSI login successful!")
            # Try to extract cookies from Chrome's cookie database
            self._extract_chrome_cookies(publisher)
            return True
        else:
            print(f"\n  URL doesn't look like a successful login: {final_url}")
            print("  Expected to be on the publisher's main site, not a login page.")
            return False

    def _extract_chrome_cookies(self, publisher: str) -> None:
        """Try to extract cookies from Chrome's cookie database for the publisher domains."""
        cfg = self._publisher_configs.get(publisher)
        if not cfg:
            return

        # Try to find Chrome's cookie database
        cookie_paths = [
            Path.home() / "AppData/Local/Google/Chrome/User Data/Default/Cookies",
            Path.home() / "AppData/Local/Google/Chrome/User Data/Default/Network/Cookies",
        ]

        for cookie_path in cookie_paths:
            if cookie_path.exists():
                try:
                    import sqlite3
                    # Copy the cookie file to avoid locking issues
                    import shutil
                    tmp_cookie = Path(self.config.cache_dir) / "chrome_cookies_tmp.db"
                    shutil.copy2(cookie_path, tmp_cookie)

                    conn = sqlite3.connect(str(tmp_cookie))
                    cursor = conn.cursor()

                    # Query cookies for the publisher domains
                    cookies = []
                    for domain in cfg.domains:
                        cursor.execute(
                            "SELECT name, value, host_key, path FROM cookies WHERE host_key LIKE ?",
                            (f"%{domain}%",)
                        )
                        cookies.extend(cursor.fetchall())

                    conn.close()
                    tmp_cookie.unlink(missing_ok=True)

                    if cookies:
                        # Save cookies to the CARSI cookie file
                        cookie_file = self._cookie_path(publisher)
                        cookie_file.parent.mkdir(parents=True, exist_ok=True)

                        cookie_data = []
                        for name, value, host_key, path in cookies:
                            cookie_data.append({
                                "name": name,
                                "value": value,
                                "domain": host_key,
                                "path": path,
                            })

                        cookie_file.write_text(
                            json.dumps(cookie_data, indent=2, ensure_ascii=False),
                            encoding="utf-8",
                        )
                        logger.info("Extracted %d cookies from Chrome for %s", len(cookie_data), publisher)
                        print(f"  Extracted {len(cookie_data)} cookies from Chrome.")
                        return

                except Exception as e:
                    logger.warning("Failed to extract Chrome cookies: %s", e)

        print("  Note: Could not extract cookies automatically.")
        print("  The fetcher will try to use your browser session directly.")

    def close(self):
        for sess in self._sessions.values():
            sess.close()
        self._sessions.clear()
