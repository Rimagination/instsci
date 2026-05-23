"""WebVPN proxy authentication management using CloakBrowser."""

import binascii
import json
import logging
import os
import time
from pathlib import Path
from urllib.parse import urlparse

import requests
from Crypto.Cipher import AES

try:
    from cloakbrowser import launch
    _HAS_CLOAKBROWSER = True
except ImportError:
    launch = None  # type: ignore[assignment]
    _HAS_CLOAKBROWSER = False

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
        self._browser = None
        self._context = None
        self._page = None
        self._webvpn_base = self.config.webvpn_base_url.rstrip("/")

    @property
    def browser_context(self):
        """Get the live CloakBrowser context (if browser session is active)."""
        return self._context

    @property
    def browser_page(self):
        """Get the live CloakBrowser page (the one with the active WebVPN session)."""
        return self._page

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
            # Auto-detect proxy and disable SSL verification if behind a proxy
            if os.environ.get("HTTP_PROXY") or os.environ.get("HTTPS_PROXY") or \
               os.environ.get("http_proxy") or os.environ.get("https_proxy"):
                self._session.verify = False
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

        For EasyConnect with proxy_url (e.g. zju-connect): no login needed,
        the SOCKS5 proxy handles authentication at the network level.

        For WebVPN or EasyConnect without proxy: opens browser for CAS login.

        Args:
            force: If True, ignore saved cookies and force re-login.

        Returns:
            True if authentication succeeded.
        """
        # EasyConnect with SOCKS5 proxy: skip login, proxy handles auth
        if self.config.proxy_url:
            logger.info("Proxy mode: skipping login (proxy handles auth).")
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

        # Filter out expired cookies
        now = time.time()
        valid_cookies = [
            c for c in cookies
            if self._is_cookie_valid(c, now)
        ]

        if not valid_cookies:
            logger.info("All saved cookies have expired.")
            return False

        # Load cookies into session
        for cookie in valid_cookies:
            self.session.cookies.set(
                cookie["name"],
                cookie["value"],
                domain=cookie.get("domain", ""),
                path=cookie.get("path", "/"),
            )

        # Validate by making a test request
        return self._validate_session()

    @staticmethod
    def _is_cookie_valid(cookie: dict, now: float | None = None) -> bool:
        """Check if a cookie is not expired. expires=0 means session cookie (always valid)."""
        expires = cookie.get("expires", 0)
        if not expires or expires == 0:
            return True
        if now is None:
            now = time.time()
        return expires > now

    def _validate_session(self) -> bool:
        """Check if the current session can access content through the gateway."""
        # For EasyConnect, try fetching through the gateway directly
        # For WebVPN, convert URL first
        if self.config.proxy_url:
            # EasyConnect: no URL conversion needed, proxy handles routing
            test_url = TEST_URL
        else:
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
        """Open CloakBrowser for manual login via WebVPN or EasyConnect portal.

        Uses a persistent browser context so the session survives across runs.
        After the first login, subsequent runs reuse the existing browser profile
        with WebVPN session intact — no re-login needed.
        """
        if not _HAS_CLOAKBROWSER:
            logger.error("cloakbrowser not installed. Run: pip install cloakbrowser")
            return False

        try:
            # Use persistent context to keep WebVPN session alive across runs
            from cloakbrowser import launch_persistent_context
            profile_dir = self.config.chrome_profile_dir
            Path(profile_dir).mkdir(parents=True, exist_ok=True)
            self._context = launch_persistent_context(
                user_data_dir=profile_dir,
                headless=False, humanize=True,
                args=["--disable-features=CrossOriginOpenerPolicy"],
            )
            self._browser = None  # persistent context manages its own browser
            self._page = self._context.new_page()
        except Exception as e:
            logger.error("Failed to start CloakBrowser: %s", e)
            return False

        # Test if persistent context has a valid session by navigating to WebVPN base
        # (just having cookies is not enough — WebVPN sessions are tied to TLS state)
        # Use networkidle to wait for all CAS redirects to complete
        self._page.goto(self._webvpn_base, wait_until="networkidle", timeout=30000)
        current_url = self._page.url
        logger.info("Session test: navigated to WebVPN base, landed on %s", current_url[:80])

        # Check if we're on CAS/IdP (session invalid) or on the gateway (session valid)
        parsed = urlparse(current_url)
        url_host = (parsed.hostname or "").lower()
        on_login_page = (
            "cas" in current_url.lower()
            or "login" in current_url.lower()
            or "/oauth/" in current_url.lower()
            or "/sso/" in current_url.lower()
            or "/wayf" in current_url.lower()
            or "/shibboleth" in current_url.lower()
        )
        is_idp = "id.tsinghua" in url_host or "idp." in url_host or "auth." in url_host

        if not on_login_page and not is_idp:
            logger.info("Persistent context has valid session! URL=%s", current_url[:60])
            self._save_browser_cookies()
            return True

        # Session invalid — need to log in
        # Navigate to WebVPN base for login prompt
        self._page.goto(self._webvpn_base, wait_until="domcontentloaded")

        print("\n" + "=" * 60)
        print(f"  Please log in at {self._webvpn_base}")
        print("  in the browser window that just opened.")
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
                # Check if user closed the browser
                if not self._context.pages:
                    logger.info("Browser closed by user.")
                    self._browser = None
                    self._context = None
                    self._page = None
                    return False

                current_url = self._page.url

                if current_url != last_url:
                    logger.info("Browser URL: %s", current_url)
                    last_url = current_url

                # Detection 1: WebVPN session cookie (WebVPN schools)
                # This cookie appears after CAS SSO completes and redirects back
                cookies = self._context.cookies()
                vpn_cookies = [
                    c for c in cookies
                    if "webvpn" in c.get("domain", "").lower()
                    and c.get("name", "").startswith("wengine_vpn_ticket")
                ]
                if vpn_cookies:
                    # Check if we're back on the WebVPN gateway (not still on CAS)
                    parsed_url = urlparse(current_url)
                    url_host = (parsed_url.hostname or "").lower()
                    on_webvpn = url_host and "webvpn" in url_host
                    if on_webvpn:
                        logger.info("Login confirmed! VPN cookie + on gateway. URL=%s", current_url[:60])
                        self._save_browser_cookies()
                        print("\n  Login successful! Cookies saved. Browser kept alive for PDF download.\n")
                        return True

                # Detection 2: URL left login/CAS page (works for both WebVPN and EasyConnect)
                on_login_page = (
                    "/login" in current_url.lower()
                    or "cas" in current_url.lower()
                    or "/oauth/" in current_url.lower()
                    or "/sso/" in current_url.lower()
                    or "/wayf" in current_url.lower()
                    or "/shibboleth" in current_url.lower()
                )
                if not on_login_page:
                    is_gateway = (
                        self._webvpn_base in current_url
                        or "otrust" in current_url.lower()
                        or "/portal/" in current_url.lower()
                    )
                    if is_gateway:
                        logger.info("Login detected via URL! (url=%s)", current_url[:60])
                        self._save_browser_cookies()
                        print("\n  Login successful! Cookies saved. Browser kept alive for PDF download.\n")
                        return True

            except Exception:
                logger.warning("Browser connection lost.")
                self._browser = None
                self._context = None
                self._page = None
                return False

        print("\n  Login timed out after 10 minutes.\n")
        self._close_browser()
        return False

    def _save_browser_cookies(self):
        """Save cookies from CloakBrowser to file and load into requests session."""
        if not self._context:
            return

        cookies = self._context.cookies()
        # Normalize expires values: -1 (session) → 0
        for c in cookies:
            if c.get("expires", 0) < 0:
                c["expires"] = 0
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
        """Close the CloakBrowser."""
        if self._context:
            try:
                self._context.close()
            except Exception:
                pass
        if self._browser:
            try:
                self._browser.close()
            except Exception:
                pass
        self._browser = None
        self._context = None
        self._page = None

    def fetch(self, url: str, **kwargs) -> requests.Response:
        """Fetch a URL through the WebVPN, EasyConnect, or proxy session.

        Routing priority:
        1. SOCKS5 proxy (if proxy_url configured) — direct fetch
        2. EasyConnect gateway (if school_type is easyconnect) — fetch via gateway
        3. WebVPN — convert URL and fetch via WebVPN
        """
        kwargs.setdefault("timeout", 30)
        kwargs.setdefault("allow_redirects", True)

        # If SOCKS5 proxy is configured (e.g. zju-connect), use it directly
        if self.config.proxy_url:
            return self.session.get(url, **kwargs)

        # WebVPN mode: convert URL
        if self._webvpn_base in url:
            proxied = url
        else:
            proxied = self.convert_url(url)

        return self.session.get(proxied, **kwargs)

    def close(self):
        """Clean up resources."""
        self._close_browser()
        if self._session:
            self._session.close()
            self._session = None


class EZProxyAuth:
    """Manages EZproxy authentication and URL proxying.

    EZproxy works by prepending a proxy URL prefix to the target URL.
    Example: http://eproxy.lib.hku.hk/login?url=https://www.nature.com/...
    """

    def __init__(
        self,
        config: Config | None = None,
        proxy_base: str = "",
    ):
        self.config = config or Config()
        self.config.ensure_dirs()
        self._proxy_base = proxy_base or self.config.ezproxy_base_url
        self._session: requests.Session | None = None
        self._browser = None
        self._context = None
        self._page = None

    @property
    def browser_context(self):
        """Get the live CloakBrowser context (if browser session is active)."""
        return self._context

    @property
    def session(self) -> requests.Session:
        if self._session is None:
            self._session = requests.Session()
            self._session.headers.update({
                "User-Agent": (
                    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) "
                    "Chrome/120.0.0.0 Safari/537.36"
                )
            })
        return self._session

    def login(self, force: bool = False) -> bool:
        """Ensure we have a valid EZproxy session."""
        if not force and self._try_load_cookies():
            logger.info("Loaded saved EZproxy cookies.")
            return True

        logger.info("No valid EZproxy session. Opening browser for login...")
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

        for cookie in cookies:
            self.session.cookies.set(
                cookie["name"],
                cookie["value"],
                domain=cookie.get("domain", ""),
                path=cookie.get("path", "/"),
            )

        return self._validate_session()

    def _validate_session(self) -> bool:
        """Check if the current EZproxy session is still valid."""
        try:
            resp = self.session.get(self._proxy_base + TEST_URL, timeout=15, allow_redirects=True)
            if "login" in resp.url.lower() or "cas" in resp.url.lower():
                return False
            return resp.status_code == 200
        except requests.RequestException:
            return False

    def _browser_login(self) -> bool:
        """Open CloakBrowser for manual EZproxy login."""
        if not _HAS_CLOAKBROWSER:
            logger.error("cloakbrowser not installed. Run: pip install cloakbrowser")
            return False

        try:
            self._browser = launch(
                headless=False, humanize=True,
                args=["--disable-features=CrossOriginOpenerPolicy"],
            )
            self._context = self._browser.new_context()
            self._page = self._context.new_page()
        except Exception as e:
            logger.error("Failed to start CloakBrowser: %s", e)
            return False

        self._page.goto(self._proxy_base + TEST_URL, wait_until="domcontentloaded")

        print("\n" + "=" * 60)
        print(f"  Please log in at the EZproxy page.")
        print("  The tool will detect when login is complete.")
        print("=" * 60 + "\n")

        max_wait = 600
        poll_interval = 3
        elapsed = 0
        last_url = ""

        while elapsed < max_wait:
            time.sleep(poll_interval)
            elapsed += poll_interval

            try:
                # Check if user closed the browser
                if not self._context.pages:
                    logger.info("Browser closed by user.")
                    self._browser = None
                    self._context = None
                    self._page = None
                    return False

                current_url = self._page.url

                if current_url != last_url:
                    logger.info("Browser URL: %s", current_url)
                    last_url = current_url

                # Detection: left login page and on a publisher page
                on_login = "login" in current_url.lower() or "cas" in current_url.lower()
                if not on_login and self._proxy_base not in current_url:
                    logger.info("EZproxy login detected! URL: %s", current_url)
                    self._save_browser_cookies()
                    print("\n  Login successful! Cookies saved. Browser kept alive for PDF download.\n")
                    return True

            except Exception:
                logger.warning("Browser connection lost.")
                self._browser = None
                self._context = None
                self._page = None
                return False

        print("\n  Login timed out after 10 minutes.\n")
        self._close_browser()
        return False

    def _save_browser_cookies(self):
        """Save cookies from CloakBrowser to file."""
        if not self._context:
            return
        cookies = self._context.cookies()
        cookie_path = Path(self.config.cookie_path)
        cookie_path.write_text(
            json.dumps(cookies, indent=2, ensure_ascii=False), encoding="utf-8"
        )
        logger.info("Saved %d cookies to %s", len(cookies), cookie_path)

        for cookie in cookies:
            self.session.cookies.set(
                cookie["name"],
                cookie["value"],
                domain=cookie.get("domain", ""),
                path=cookie.get("path", "/"),
            )

    def _close_browser(self):
        if self._browser:
            try:
                self._browser.close()
            except Exception:
                pass
            self._browser = None
            self._context = None
            self._page = None

    def get_proxied_url(self, url: str) -> str:
        """Wrap a URL with the EZproxy prefix."""
        # Don't double-proxy
        if self._proxy_base and self._proxy_base.rstrip("/").split("//")[-1].split("/")[0] in url:
            return url
        return self._proxy_base + url

    def fetch(self, url: str, **kwargs) -> requests.Response:
        """Fetch a URL through the EZproxy."""
        kwargs.setdefault("timeout", 30)
        kwargs.setdefault("allow_redirects", True)
        proxied = self.get_proxied_url(url)
        return self.session.get(proxied, **kwargs)

    def close(self):
        self._close_browser()
        if self._session:
            self._session.close()
            self._session = None
