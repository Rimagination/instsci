"""CARSI (Shibboleth/SAML) federated authentication for publisher access."""

import json
import logging
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlparse

import requests

try:
    from cloakbrowser import launch
    from .cloakbrowser_compat import ensure_cloakbrowser_platform_compatible
    ensure_cloakbrowser_platform_compatible()
    _HAS_CLOAKBROWSER = True
except ImportError:
    launch = None  # type: ignore[assignment]
    _HAS_CLOAKBROWSER = False

from .config import Config
from .session_store import CookieStore

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
            # Auto-detect proxy and disable SSL verification if behind a proxy
            import os
            if os.environ.get("HTTP_PROXY") or os.environ.get("HTTPS_PROXY") or \
               os.environ.get("http_proxy") or os.environ.get("https_proxy"):
                sess.verify = False
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
        sess = self._get_session(publisher)
        if not CookieStore(cookie_file).load_into(sess):
            return False
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
        """Login via CARSI using CloakBrowser to automate the SSO flow.

        Opens the publisher's institutional login page, searches for the user's
        university, and waits for them to complete SSO authentication.
        Cookies are automatically captured and saved.
        """
        if not _HAS_CLOAKBROWSER:
            logger.error("cloakbrowser not installed. Run: pip install cloakbrowser")
            return False

        cfg = self._publisher_configs.get(publisher)
        if not cfg:
            logger.error("Unknown publisher: %s", publisher)
            return False

        print("\n" + "=" * 60)
        print(f"  CARSI Login: {cfg.name}")
        print(f"  ")
        print(f"  Steps:")
        print(f"  1. The browser will open the institutional login page")
        print(f"  2. Search for your university: {self.config.carsi_idp_name}")
        print(f"  3. Select it and log in with your campus credentials")
        print(f"  4. After login, the tool will automatically capture cookies")
        print("=" * 60 + "\n")

        browser = None
        try:
            browser = launch(
                headless=False, humanize=True,
                args=["--disable-features=CrossOriginOpenerPolicy"],
            )
            context = browser.new_context()
            page = context.new_page()

            # Navigate to publisher's institutional login page
            page.goto(cfg.login_url, wait_until="domcontentloaded")
            print(f"  Browser opened at: {cfg.login_url}")

            # Try to search for the university if a search selector is available
            if cfg.search_selector and self.config.carsi_idp_name:
                try:
                    page.wait_for_selector(cfg.search_selector, timeout=10000)
                    page.fill(cfg.search_selector, self.config.carsi_idp_name)
                    logger.info("Filled university search: %s", self.config.carsi_idp_name)

                    # Wait for search results and try to click the matching one
                    if cfg.result_selector:
                        try:
                            page.wait_for_selector(cfg.result_selector, timeout=5000)
                            # Try clicking the result that matches the university name
                            result_text = page.evaluate(f"""
                                (() => {{
                                    const items = document.querySelectorAll('{cfg.result_selector}');
                                    for (const el of items) {{
                                        const text = el.textContent || '';
                                        if (text.includes('{self.config.carsi_idp_name}')) {{
                                            el.click();
                                            return text.trim().substring(0, 60);
                                        }}
                                    }}
                                    // If no exact match, click the first result
                                    if (items.length > 0) {{
                                        items[0].click();
                                        return items[0].textContent.trim().substring(0, 60);
                                    }}
                                    return null;
                                }})()
                            """)
                            if result_text:
                                logger.info("Clicked institution: %s", result_text)
                        except Exception:
                            pass  # User may need to click manually
                except Exception:
                    logger.info("Could not auto-fill search, user will select manually")

            print("  Waiting for SSO login to complete...")
            print("  (up to 5 minutes)\n")

            # Wait for user to complete SSO — poll until URL indicates success
            max_wait = 300  # 5 minutes
            poll_interval = 3
            elapsed = 0
            last_url = ""

            while elapsed < max_wait:
                time.sleep(poll_interval)
                elapsed += poll_interval

                try:
                    # Check if user closed the browser
                    if not context.pages:
                        logger.info("Browser closed by user.")
                        return False

                    current_url = page.url

                    if current_url != last_url:
                        logger.info("Browser URL: %s", current_url[:80])
                        last_url = current_url

                    # Check if we're back on the publisher site (SSO complete)
                    on_publisher = any(d in current_url for d in cfg.domains)
                    on_login_page = any(x in current_url.lower() for x in (
                        "login", "institutional", "wayf", "saml", "shibboleth", "sso", "cas",
                    ))

                    if on_publisher and not on_login_page:
                        logger.info("CARSI login confirmed. URL: %s", current_url)
                        print("  CARSI login successful!")

                        # Save cookies
                        cookies = context.cookies()
                        self._save_cookies(publisher, cookies)
                        print(f"  Saved {len(cookies)} cookies.")
                        return True

                except Exception:
                    logger.warning("Browser connection lost.")
                    return False

            print("  Login timed out after 5 minutes.")
            return False

        except Exception as e:
            logger.error("CARSI browser login failed: %s", e)
            return False
        finally:
            if browser:
                try:
                    browser.close()
                except Exception:
                    pass

    def _save_cookies(self, publisher: str, cookies: list[dict]) -> None:
        """Save cookies to the CARSI cookie file for the given publisher."""
        cookie_file = self._cookie_path(publisher)
        valid_cookies = CookieStore(cookie_file).save(cookies)
        logger.info("Saved %d CARSI cookies for %s", len(valid_cookies), publisher)

    def close(self):
        for sess in self._sessions.values():
            sess.close()
        self._sessions.clear()
