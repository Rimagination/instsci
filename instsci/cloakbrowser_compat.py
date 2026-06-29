"""Compatibility helpers for CloakBrowser runtime quirks."""

from __future__ import annotations

import logging
import os
import platform
import sys
from pathlib import Path
from typing import Any

_CLOAKBROWSER_CACHE_ENV = "CLOAKBROWSER_CACHE_DIR"
_INSTSCI_CACHE_ENV = "INSTSCI_CLOAKBROWSER_CACHE_DIR"
_BUILTIN_CACHE_DIR = Path(__file__).resolve().parent / "_browsers" / "cloakbrowser"

logger = logging.getLogger(__name__)

# Playwright's sync API (driven by CloakBrowser) does not run on Python 3.14,
# where it raises "Sync API inside the asyncio loop" and breaks every browser
# fetch. Guard the browser path so the failure is explained, not cryptic.
_MAX_BROWSER_PYTHON = (3, 13)
_python_warning_emitted = False


def configure_builtin_cloakbrowser(
    cache_dir: str | os.PathLike[str] | None = None,
    *,
    create_dir: bool = True,
) -> Path:
    """Point CloakBrowser at InstSci's project-managed browser cache.

    CloakBrowser downloads its Chromium binary on first use. InstSci keeps that
    binary under the project package by default so publisher workflows use the
    same built-in browser instead of an unrelated user-level cache.
    """
    existing = os.environ.get(_CLOAKBROWSER_CACHE_ENV)
    if existing:
        return Path(existing)

    target = Path(cache_dir or os.environ.get(_INSTSCI_CACHE_ENV, "") or _BUILTIN_CACHE_DIR)
    target = target.expanduser().resolve()
    if create_dir:
        target.mkdir(parents=True, exist_ok=True)
    os.environ[_CLOAKBROWSER_CACHE_ENV] = str(target)
    return target


def browser_python_warning(version: tuple[int, ...] | None = None) -> str | None:
    """Return a message if the running Python is too new for the browser path.

    Playwright's sync API (driven by CloakBrowser) fails on Python >= 3.14 with
    "Sync API inside the asyncio loop", breaking every institutional/browser
    fetch. Open Access and arXiv (HTTP) fetches are unaffected.
    """
    ver = tuple((version or sys.version_info[:2])[:2])
    if ver > _MAX_BROWSER_PYTHON:
        return (
            f"Python {ver[0]}.{ver[1]} is not supported for InstSci's browser "
            "(CloakBrowser/Playwright) workflows, which require Python 3.10-3.13. "
            "Use a 3.12/3.13 environment for institutional access. Open Access and "
            "arXiv fetches still work on any supported Python."
        )
    return None


def prepare_cloakbrowser_runtime(config_module: Any | None = None) -> Path:
    """Configure InstSci's CloakBrowser runtime before importing launch APIs."""
    global _python_warning_emitted
    warning = browser_python_warning()
    if warning and not _python_warning_emitted:
        logger.warning("%s", warning)
        _python_warning_emitted = True
    cache_dir = configure_builtin_cloakbrowser()
    ensure_cloakbrowser_platform_compatible(config_module)
    return cache_dir


def ensure_cloakbrowser_platform_compatible(config_module: Any | None = None) -> bool:
    """Patch CloakBrowser platform detection when Windows reports no machine.

    Some Windows Python environments return an empty string from
    ``platform.machine()``. CloakBrowser supports Windows x64, but its lookup
    table cannot match that empty architecture value. We add the narrow missing
    lookup entry at runtime instead of modifying the third-party package.
    """
    if platform.system() != "Windows" or platform.machine():
        return False

    try:
        config = config_module
        if config is None:
            from cloakbrowser import config as config  # type: ignore[no-redef]
    except Exception:
        return False

    supported = getattr(config, "SUPPORTED_PLATFORMS", None)
    if not isinstance(supported, dict):
        return False

    if ("Windows", "") in supported:
        return False

    is_64bit_windows = bool(os.environ.get("ProgramFiles(x86)")) or bool(
        os.environ.get("PROCESSOR_ARCHITEW6432")
    )
    if not is_64bit_windows:
        return False

    supported[("Windows", "")] = supported.get(("Windows", "AMD64"), "windows-x64")
    return True
