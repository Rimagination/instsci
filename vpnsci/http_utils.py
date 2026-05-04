"""Shared HTTP utilities with retry logic."""

import logging
import time

import requests

logger = logging.getLogger(__name__)


def request_with_retry(
    method: str,
    url: str,
    *,
    max_retries: int = 3,
    retry_backoff: float = 2.0,
    **kwargs,
) -> requests.Response:
    """HTTP request with exponential backoff retry on 429/5xx/network errors.

    Args:
        method: HTTP method ("GET", "POST", etc.).
        url: Target URL.
        max_retries: Maximum number of retry attempts (default 3).
        retry_backoff: Base for exponential backoff in seconds (default 2.0).
        **kwargs: Passed to requests.request (timeout, headers, etc.).

    Returns:
        The final Response object (even if it's a 4xx error — caller decides).
    """
    kwargs.setdefault("timeout", 30)

    for attempt in range(max_retries + 1):
        try:
            resp = requests.request(method, url, **kwargs)
            if resp.status_code == 429 or resp.status_code >= 500:
                if attempt < max_retries:
                    wait = retry_backoff ** attempt
                    logger.warning(
                        "HTTP %d for %s, retrying in %.1fs (attempt %d/%d)",
                        resp.status_code, url, wait, attempt + 1, max_retries,
                    )
                    time.sleep(wait)
                    continue
            return resp
        except requests.RequestException as e:
            if attempt < max_retries:
                wait = retry_backoff ** attempt
                logger.warning(
                    "Request error for %s: %s, retrying in %.1fs (attempt %d/%d)",
                    url, e, wait, attempt + 1, max_retries,
                )
                time.sleep(wait)
            else:
                raise

    # Should not reach here, but just in case
    return requests.request(method, url, **kwargs)
