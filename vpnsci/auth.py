"""WebVPN proxy authentication management using Selenium."""

import binascii
import json
import logging
import time
from pathlib import Path
from urllib.parse import urlparse

import requests
from Crypto.Cipher import AES
from selenium import webdriver
from selenium.webdriver.chrome.options import Options

from .config import Config

logger = logging.getLogger(__name__)

# URL used to test if proxy session is valid
TEST_URL = "https://www.nature.com"

# Default WebVPN encryption key (same for both AES key and IV)
WEBVPN_DEFAULT_KEY = b"wrdvpnisthebest!"


class WebVPNAuth:
    """Manages WebVPN authentication and URL conversion.

    Supports Chinese university WebVPN systems (e.g. Tsinghua, ZJU).
    URL conversion uses AES-CFB encryption on the hostname.
    """

    def __init__(
        self,
        config: Config | None = None,
        key: bytes | None = None,
        iv: bytes | None = None,
    ):
        self.config = config or Config()
        self.config.ensure_dirs()
        self._encrypt_key = key or WEBVPN_DEFAULT_KEY
        self._encrypt_iv = iv or self._encrypt_key
        self._session: requests.Session | None = None
        self._driver: webdriver.Chrome | None = None
        self._webvpn_base = self.config.webvpn_base_url.rstrip("/")

    @property
    def session(self) -> requests.Session:
        """Get an authenticated requests session."""
        if self._session is None:
            self._session = requests.Session()
            self._session.headers.update({
                "User-Agent": (
                    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) "
                    "Chrome/120.0.0.0 Safari/537.36"
                )
            })
            # Configure SOCKS5 proxy if set (for EasyConnect)
            if self.config.proxy_url:
                self._session.proxies = {
                    "http": self.config.proxy_url,
                    "https": self.config.proxy_url,
                }
                logger.info("Using proxy: %s", self.config.proxy_url)
        return self._session

    def convert_url(self, url: str) -> str:
        """Convert a regular URL to a WebVPN URL using AES-CFB encryption.

        Encrypts only the hostname; path and query are kept as-is.
        Output: {webvpn_base}/{scheme}[-{port}]/{hex(IV)+hex(encrypted_host)}{path}?{query}
        """
        parsed = urlparse(url)
        scheme = parsed.scheme.lower()
        hostname = parsed.hostname
        port = parsed.port
        path = parsed.path
        query = parsed.query

        if not hostname:
            return url

        # Encrypt hostname with AES-CFB
        cipher = AES.new(self._encrypt_key, AES.MODE_CFB, self._encrypt_iv, segment_size=128)
        encrypted = cipher.encrypt(hostname.encode("utf-8"))

        # Build encrypted hex string: IV (16 bytes = 32 hex chars) + ciphertext
        encrypted_hex = binascii.hexlify(self._encrypt_iv).decode() + binascii.hexlify(encrypted).decode()

        # Build scheme part (include port if non-standard)
        scheme_part = scheme
        if port:
            scheme_part = f"{scheme}-{port}"

        # Construct final URL
        result = f"{self._webvpn_base}/{scheme_part}/{encrypted_hex}{path}"
        if query:
            result += f"?{query}"
        return result

    def login(self, force: bool = False) -> bool:
        """Ensure we have a valid session.

        For EasyConnect (proxy_url set): no login needed, the SOCKS5 proxy
        handles authentication at the network level via zju-connect.

        For WebVPN: reuses saved cookies or opens browser for CAS login.

        Args:
            force: If True, ignore saved cookies and force re-login.

        Returns:
            True if authentication succeeded.
        """
        # EasyConnect mode: proxy handles auth, no login needed
        if self.config.proxy_url:
            logger.info("EasyConnect mode: proxy handles authentication, skipping login.")
            return True

        if not force and self._try_load_cookies():
            logger.info("Loaded saved cookies - session is valid.")
            return True

        logger.info("No valid session found. Opening browser for login...")
        return self._browser_login()

    def _try_load_cookies(self) -> bool:
        """Try to load cookies from file and validate them."""
        cookie_path = Path(self.config.cookie_path)
        if not cookie_path.exists():
            return False

        try:
            cookies = json.loads(cookie_path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError) as e:
            logger.warning("Failed to read cookies: %s", e)
            return False

        # Load cookies into session
        for cookie in cookies:
            self.session.cookies.set(
                cookie["name"],
                cookie["value"],
                domain=cookie.get("domain", ""),
                path=cookie.get("path", "/"),
            )

        # Validate by making a test request
        return self._validate_session()

    def _validate_session(self) -> bool:
        """Check if the current session can access content through WebVPN."""
        test_url = self.convert_url(TEST_URL)
        try:
            resp = self.session.get(test_url, timeout=15, allow_redirects=True)
            # If redirected to CAS login page, session is expired
            if "cas" in resp.url.lower() or "login" in resp.url.lower():
                logger.info("Session expired - redirected to login page.")
                return False
            if resp.status_code == 200:
                return True
        except requests.RequestException as e:
            logger.warning("Session validation failed: %s", e)
        return False

    def _browser_login(self) -> bool:
        """Open Chrome for manual CAS login via WebVPN."""
        options = Options()
        options.add_argument("--no-first-run")
        options.add_argument("--no-default-browser-check")
        options.add_argument("--remote-allow-origins=*")
        options.add_experimental_option("excludeSwitches", ["enable-automation"])

        try:
            self._driver = webdriver.Chrome(options=options)
        except Exception as e:
            logger.error("Failed to start Chrome: %s", e)
            logger.error(
                "Make sure Chrome is installed and no other ChromeDriver "
                "instances are running."
            )
            return False

        # Navigate to WebVPN login page
        self._driver.get(self._webvpn_base)

        print("\n" + "=" * 60)
        print(f"  Please log in to WebVPN ({self._webvpn_base})")
        print("  via CAS in the browser window.")
        print("  The tool will detect when login is complete.")
        print("=" * 60 + "\n")

        # Poll until login succeeds
        max_wait = 600  # 10 minutes
        poll_interval = 3
        elapsed = 0
        last_url = ""

        while elapsed < max_wait:
            time.sleep(poll_interval)
            elapsed += poll_interval

            try:
                current_url = self._driver.current_url

                if current_url != last_url:
                    logger.info("Browser URL: %s", current_url)
                    last_url = current_url

                # Detection: URL is back on WebVPN main page (not CAS login)
                if self._webvpn_base in current_url and "cas" not in current_url.lower() and "login" not in current_url.lower():
                    logger.info("Login detected! URL: %s", current_url)
                    self._save_browser_cookies()
                    print("\n  Login successful! Cookies saved.\n")
                    self._close_browser()
                    return True

                # Detection: check for WebVPN session cookies
                cookies = self._driver.get_cookies()
                vpn_cookies = [c for c in cookies if "webvpn" in c.get("domain", "").lower() and c.get("name", "").startswith("wengine_vpn_ticket")]
                if vpn_cookies:
                    logger.info("Login detected via WebVPN session cookie.")
                    self._save_browser_cookies()
                    print("\n  Login successful! Cookies saved.\n")
                    self._close_browser()
                    return True

            except Exception:
                logger.warning("Browser connection lost.")
                self._driver = None
                return False

        print("\n  Login timed out after 10 minutes.\n")
        self._close_browser()
        return False

    def _save_browser_cookies(self):
        """Save cookies from Selenium browser to file and load into requests session."""
        if not self._driver:
            return

        cookies = self._driver.get_cookies()
        cookie_path = Path(self.config.cookie_path)
        cookie_path.write_text(
            json.dumps(cookies, indent=2, ensure_ascii=False), encoding="utf-8"
        )
        logger.info("Saved %d cookies to %s", len(cookies), cookie_path)

        # Also load into requests session
        for cookie in cookies:
            self.session.cookies.set(
                cookie["name"],
                cookie["value"],
                domain=cookie.get("domain", ""),
                path=cookie.get("path", "/"),
            )

    def _close_browser(self):
        """Close the Selenium browser."""
        if self._driver:
            try:
                self._driver.quit()
            except Exception:
                pass
            self._driver = None

    def fetch(self, url: str, **kwargs) -> requests.Response:
        """Fetch a URL through the WebVPN or proxy session.

        For WebVPN: converts URL and fetches via WebVPN.
        For EasyConnect/proxy: fetches directly through SOCKS5 proxy.
        """
        # If proxy is configured (EasyConnect mode), fetch directly
        if self.config.proxy_url:
            kwargs.setdefault("timeout", 30)
            kwargs.setdefault("allow_redirects", True)
            return self.session.get(url, **kwargs)

        # WebVPN mode: convert URL
        if self._webvpn_base in url:
            proxied = url
        else:
            proxied = self.convert_url(url)

        kwargs.setdefault("timeout", 30)
        kwargs.setdefault("allow_redirects", True)
        return self.session.get(proxied, **kwargs)

    def close(self):
        """Clean up resources."""
        self._close_browser()
        if self._session:
            self._session.close()
            self._session = None


# ──────────────────────────────────────────────────────────────
# Reference: EZProxyAuth (for HKU-style EZproxy systems)
# Not used by vpnsci directly, kept for reference.
# ──────────────────────────────────────────────────────────────
#
# class EZProxyAuth:
#     """Manages EZproxy authentication via Selenium and cookie persistence."""
#
#     EZPROXY_DOMAIN = "eproxy.lib.hku.hk"
#
#     def __init__(self, config: Config | None = None):
#         self.config = config or Config()
#         self.config.ensure_dirs()
#         self._session: requests.Session | None = None
#         self._driver: webdriver.Chrome | None = None
#
#     @property
#     def session(self) -> requests.Session:
#         if self._session is None:
#             self._session = requests.Session()
#             self._session.headers.update({
#                 "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) ..."
#             })
#         return self._session
#
#     def login(self, force: bool = False) -> bool:
#         if not force and self._try_load_cookies():
#             return True
#         return self._browser_login()
#
#     def get_proxied_url(self, url: str) -> str:
#         if self.EZPROXY_DOMAIN in url:
#             return url
#         return self.config.proxy_base + url
#
#     def fetch(self, url: str, **kwargs) -> requests.Response:
#         proxied = self.get_proxied_url(url)
#         kwargs.setdefault("timeout", 30)
#         kwargs.setdefault("allow_redirects", True)
#         return self.session.get(proxied, **kwargs)
#
#     def close(self):
#         self._close_browser()
#         if self._session:
#             self._session.close()
#             self._session = None
