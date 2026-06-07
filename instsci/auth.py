"""Campus institutional access authentication management using CloakBrowser."""

import binascii
import logging
import os
import time
from pathlib import Path
from urllib.parse import urlparse

import requests
from Crypto.Cipher import AES

try:
    from .cloakbrowser_compat import prepare_cloakbrowser_runtime
    prepare_cloakbrowser_runtime()
    from cloakbrowser import launch
    _HAS_CLOAKBROWSER = True
except ImportError:
    launch = None  # type: ignore[assignment]
    _HAS_CLOAKBROWSER = False

from .config import Config
from .session_store import CookieStore

logger = logging.getLogger(__name__)

# URL used to test if institutional access session is valid.
TEST_URL = "https://www.nature.com"

# Default campus gateway encryption key (same for both AES key and IV).
WEBVPN_DEFAULT_KEY = b"wrdvpnisthebest!"


class WebVPNAuth:
    """Manages campus access authentication and URL conversion.

    Supports Chinese university campus gateway systems.
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
        """Get the live CloakBrowser page with the active campus access session."""
        return self._page

    def _browser_launch_args(self) -> list[str]:
        """Arguments for the WebVPN login browser."""
        return [
            "--no-proxy-server",
            "--disable-features=CrossOriginOpenerPolicy",
        ]

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
            # Auto-detect local connector and disable SSL verification if needed.
            if os.environ.get("HTTP_PROXY") or os.environ.get("HTTPS_PROXY") or \
               os.environ.get("http_proxy") or os.environ.get("https_proxy"):
                self._session.verify = False
            # Configure SOCKS5 connector if set (for EasyConnect).
            if self.config.proxy_url:
                self._session.proxies = {
                    "http": self.config.proxy_url,
                    "https": self.config.proxy_url,
                }
                logger.info("Using connector: %s", self.config.proxy_url)
        return self._session

    def convert_url(self, url: str) -> str:
        """Convert a regular URL to a campus gateway URL using AES-CFB encryption.

        Encrypts only the hostname; path and query are kept as-is.
        Output: {access_base}/{scheme}[-{port}]/{hex(IV)+hex(encrypted_host)}{path}?{query}
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

        For EasyConnect with connector URL (e.g. zju-connect): no login needed,
        the SOCKS5 connector handles authentication at the network level.

        For campus gateways or EasyConnect without connector: opens browser for CAS login.

        Args:
            force: If True, ignore saved cookies and force re-login.

        Returns:
            True if authentication succeeded.
        """
        # EasyConnect with SOCKS5 connector: skip login, connector handles auth.
        if self.config.proxy_url:
            logger.info("Connector mode: skipping login (connector handles auth).")
            return True

        if not force and self._try_load_cookies():
            logger.info("Loaded saved cookies - session is valid.")
            return True

        logger.info("No valid session found. Opening browser for login...")
        return self._browser_login()

    def _try_load_cookies(self) -> bool:
        """Try to load cookies from file and validate them."""
        if not CookieStore(self.config.cookie_path).load_into(self.session):
            logger.info("All saved cookies have expired.")
            return False

        return self._validate_session()

    def _validate_session(self) -> bool:
        """Check if the current session can access content through the gateway."""
        # For EasyConnect, try fetching through the gateway directly.
        # For campus gateways, convert URL first.
        if self.config.proxy_url:
            # EasyConnect: no URL conversion needed, connector handles routing.
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
        """Open CloakBrowser for manual login via campus or EasyConnect portal.

        Uses a persistent browser context so the session survives across runs.
        After the first login, subsequent runs reuse the existing browser profile
        with the campus access session intact.
        """
        if not _HAS_CLOAKBROWSER:
            logger.error("cloakbrowser not installed. Run: pip install cloakbrowser")
            return False

        try:
            # Use persistent context to keep the campus session alive across runs.
            prepare_cloakbrowser_runtime()
            from cloakbrowser import launch_persistent_context
            profile_dir = self.config.chrome_profile_dir
            Path(profile_dir).mkdir(parents=True, exist_ok=True)
            self._context = launch_persistent_context(
                user_data_dir=profile_dir,
                headless=False, humanize=True,
                args=self._browser_launch_args(),
            )
            self._browser = None  # persistent context manages its own browser
            self._page = self._context.new_page()
        except Exception as e:
            logger.error("Failed to start CloakBrowser: %s", e)
            return False

        # Test if persistent context has a valid session by navigating to the gateway base
        # (just having cookies is not enough; some sessions are tied to TLS state).
        # Use networkidle to wait for all CAS redirects to complete
        self._page.goto(self._webvpn_base, wait_until="networkidle", timeout=30000)
        current_url = self._page.url
        logger.info("Session test: navigated to campus gateway, landed on %s", current_url[:80])

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
        # Navigate to campus access base for login prompt.
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

                # Detection 1: campus gateway session cookie.
                # This cookie appears after CAS SSO completes and redirects back
                cookies = self._context.cookies()
                vpn_cookies = [
                    c for c in cookies
                    if "webvpn" in c.get("domain", "").lower()
                    and c.get("name", "").startswith("wengine_vpn_ticket")
                ]
                if vpn_cookies:
                    # Check if we're back on the campus gateway (not still on CAS).
                    parsed_url = urlparse(current_url)
                    url_host = (parsed_url.hostname or "").lower()
                    on_webvpn = url_host and "webvpn" in url_host
                    if on_webvpn:
                        logger.info("Login confirmed: campus session cookie and gateway URL. URL=%s", current_url[:60])
                        self._save_browser_cookies()
                        print("\n  Login successful! Cookies saved. Browser kept alive for PDF download.\n")
                        return True

                # Detection 2: URL left login/CAS page.
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

        store = CookieStore(self.config.cookie_path)
        cookies = store.save(self._context.cookies())
        logger.info("Saved %d cookies to %s", len(cookies), store.path)
        store.apply_to_session(self.session, cookies)

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
        """Fetch a URL through the campus, EasyConnect, or connector session.

        Routing priority:
        1. SOCKS5 connector (if configured) — direct fetch
        2. EasyConnect gateway (if school_type is easyconnect) — fetch via gateway
        3. Campus gateway — convert URL and fetch through the gateway
        """
        kwargs.setdefault("timeout", 30)
        kwargs.setdefault("allow_redirects", True)

        # If SOCKS5 connector is configured (e.g. zju-connect), use it directly.
        if self.config.proxy_url:
            return self.session.get(url, **kwargs)

        # Campus gateway mode: convert URL.
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
        if not CookieStore(self.config.cookie_path).load_into(self.session):
            return False

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
        store = CookieStore(self.config.cookie_path)
        cookies = store.save(self._context.cookies())
        logger.info("Saved %d cookies to %s", len(cookies), store.path)
        store.apply_to_session(self.session, cookies)

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
