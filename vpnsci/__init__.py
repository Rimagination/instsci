"""Legacy compatibility package for the renamed InstSci implementation."""

from importlib import import_module
import sys

_impl = import_module("instsci")

__all__ = list(getattr(_impl, "__all__", []))
__version__ = getattr(_impl, "__version__", "")

for name in __all__:
    globals()[name] = getattr(_impl, name)

_ALIASES = [
    "auth",
    "carsi",
    "cli",
    "config",
    "extractors",
    "extractors.html_extractor",
    "extractors.pdf_extractor",
    "extractors.publisher_adapters",
    "extractors.publisher_adapters.acs",
    "extractors.publisher_adapters.elsevier",
    "extractors.publisher_adapters.generic",
    "extractors.publisher_adapters.nature",
    "extractors.publisher_adapters.rsc",
    "extractors.publisher_adapters.tandfonline",
    "extractors.publisher_adapters.wiley",
    "fetcher",
    "flaresolverr",
    "http_utils",
    "mcp_server",
    "models",
    "schools",
    "session_store",
    "sources",
    "sources.arxiv",
    "sources.elsevier_api",
    "sources.semantic_scholar",
    "sources.unpaywall",
]

for alias in _ALIASES:
    module = import_module(f"instsci.{alias}")
    sys.modules[f"{__name__}.{alias}"] = module
    if "." not in alias:
        globals()[alias] = module
