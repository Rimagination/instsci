"""Shared HTTP utilities with retry logic."""

import logging
import math
import os
import time
from datetime import timezone
from email.utils import parsedate_to_datetime
import urllib3

import requests

from .retry_policy import HTTP_RETRY_STATUSES

logger = logging.getLogger(__name__)


def _retry_after_seconds(value: str) -> float | None:
    value = str(value or "").strip()
    if value.isdigit():
        return float(value)
    try:
        date = parsedate_to_datetime(value)
        if date.tzinfo is None:
            date = date.replace(tzinfo=timezone.utc)
        return max(0.0, date.timestamp() - time.time())
    except (TypeError, ValueError, OverflowError):
        return None

# Auto-detect local HTTP connector and disable SSL verification warnings.
if os.environ.get("HTTP_PROXY") or os.environ.get("HTTPS_PROXY") or \
   os.environ.get("http_proxy") or os.environ.get("https_proxy"):
    # Local network connectors often use self-signed certificates.
    _SSL_VERIFY = False
    urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
else:
    _SSL_VERIFY = True


def request_with_retry(
    method: str,
    url: str,
    *,
    max_retries: int = 3,
    retry_backoff: float = 2.0,
    max_retry_delay: float = 60.0,
    **kwargs,
) -> requests.Response:
    """Retry safe reads after transient failures, respecting Retry-After.

    Args:
        method: HTTP method ("GET", "POST", etc.).
        url: Target URL.
        max_retries: Maximum number of retry attempts (default 3).
        retry_backoff: Base for exponential backoff in seconds (default 2.0).
        max_retry_delay: Maximum wait per retry. A longer Retry-After returns
            the response to the caller without retrying ahead of the server.
        **kwargs: Passed to requests.request (timeout, headers, etc.).

    Returns:
        The final Response object (even if it's a 4xx error — caller decides).
    """
    if max_retries < 0 or not math.isfinite(retry_backoff) or retry_backoff < 0:
        raise ValueError("Retries and backoff must be non-negative and finite.")
    if not math.isfinite(max_retry_delay) or max_retry_delay < 0:
        raise ValueError("max_retry_delay must be non-negative and finite.")
    if method.upper() not in {"GET", "HEAD", "OPTIONS"}:
        max_retries = 0
    kwargs.setdefault("timeout", 30)
    kwargs.setdefault("verify", _SSL_VERIFY)

    for attempt in range(max_retries + 1):
        try:
            resp = requests.request(method, url, **kwargs)
            if resp.status_code in HTTP_RETRY_STATUSES:
                if attempt < max_retries:
                    wait = min(max_retry_delay, retry_backoff ** attempt)
                    server_wait = _retry_after_seconds(resp.headers.get("Retry-After", ""))
                    if server_wait is not None:
                        if server_wait > max_retry_delay:
                            return resp
                        wait = max(wait, server_wait)
                    logger.warning(
                        "HTTP %d for %s, retrying in %.1fs (attempt %d/%d)",
                        resp.status_code, url, wait, attempt + 1, max_retries,
                    )
                    resp.close()
                    time.sleep(wait)
                    continue
            return resp
        except (requests.Timeout, requests.ConnectionError) as e:
            if attempt < max_retries:
                wait = min(max_retry_delay, retry_backoff ** attempt)
                logger.warning(
                    "Request error for %s: %s, retrying in %.1fs (attempt %d/%d)",
                    url, e, wait, attempt + 1, max_retries,
                )
                time.sleep(wait)
            else:
                raise

    # Should not reach here, but just in case
    return requests.request(method, url, **kwargs)
