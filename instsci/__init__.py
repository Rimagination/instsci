"""InstSci - academic paper fetcher with institutional access support."""

from .config import Config
from .fetcher import PaperFetcher
from .models import FetchResult, NextAction, Paper

__all__ = ["PaperFetcher", "Paper", "FetchResult", "NextAction", "Config"]
__version__ = "0.1.1"
