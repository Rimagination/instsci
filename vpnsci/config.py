"""Configuration management for vpnsci."""

import json
import logging
from dataclasses import asdict, dataclass, field
from pathlib import Path

logger = logging.getLogger(__name__)

DEFAULT_BASE_DIR = Path.home() / ".vpnsci"


@dataclass
class Config:
    """VpnSci configuration."""

    school: str = ""  # School name (use 'vpnsci schools' to list, or configure via MCP)
    webvpn_base_url: str = ""  # Auto-resolved from school if empty
    proxy_url: str = ""  # SOCKS5 proxy for EasyConnect (e.g. socks5://127.0.0.1:1080)
    email: str = ""  # Set via 'vpnsci config-cmd --email your@email.com'
    elsevier_api_key: str = ""  # Elsevier Developer Portal API key
    elsevier_inst_token: str = ""  # Optional Elsevier institutional token
    flaresolverr_url: str = "http://127.0.0.1:8191/v1"  # FlareSolverr service URL
    output_dir: str = ""
    cache_dir: str = ""
    cookie_path: str = ""
    chrome_profile_dir: str = ""
    carsi_enabled: bool = False  # Enable CARSI/Shibboleth federated auth
    carsi_idp_name: str = ""  # University name for CARSI WAYF (e.g. "中国海洋大学")
    carsi_cookie_dir: str = ""  # Per-publisher CARSI cookies
    request_delay_min: float = 2.0
    request_delay_max: float = 5.0

    def __post_init__(self):
        base = DEFAULT_BASE_DIR
        if not self.output_dir:
            self.output_dir = str(base / "papers")
        if not self.cache_dir:
            self.cache_dir = str(base / "cache")
        if not self.cookie_path:
            self.cookie_path = str(base / "cookies.json")
        if not self.chrome_profile_dir:
            self.chrome_profile_dir = str(base / "chrome-profile")
        if not self.carsi_cookie_dir:
            self.carsi_cookie_dir = str(base / "carsi_cookies")
        # Auto-resolve webvpn_base_url from school if not set
        if not self.webvpn_base_url and self.school:
            try:
                from .schools import get_school
                entry = get_school(self.school)
                self.webvpn_base_url = entry.host
            except ValueError:
                pass  # School not found; user must set webvpn_base_url manually

    def ensure_dirs(self):
        """Create all necessary directories."""
        for d in [self.output_dir, self.cache_dir, self.chrome_profile_dir, self.carsi_cookie_dir]:
            Path(d).mkdir(parents=True, exist_ok=True)
        Path(self.cookie_path).parent.mkdir(parents=True, exist_ok=True)

    def save(self, path: Path | None = None):
        """Save config to JSON file."""
        path = path or (DEFAULT_BASE_DIR / "config.json")
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(asdict(self), indent=2), encoding="utf-8")

    @classmethod
    def load(cls, path: Path | None = None) -> "Config":
        """Load config from JSON file, falling back to defaults."""
        path = path or (DEFAULT_BASE_DIR / "config.json")
        if path.exists():
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
                return cls(**{k: v for k, v in data.items() if k in cls.__dataclass_fields__})
            except (json.JSONDecodeError, TypeError) as e:
                logger.warning("Failed to load config from %s: %s. Using defaults.", path, e)
        return cls()
