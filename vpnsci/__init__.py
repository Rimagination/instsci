"""VpnSci - Academic paper fetcher with multi-university WebVPN support."""

from .config import Config
from .fetcher import PaperFetcher
from .models import Paper

__all__ = ["PaperFetcher", "Paper", "Config"]
__version__ = "0.1.0"
