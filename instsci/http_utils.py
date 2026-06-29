"""Shared HTTP utilities with retry logic."""

import logging
import os
import time

import requests

logger = logging.getLogger(__name__)

# TLS verification policy.
#
# Verification stays ON by default -- including behind an HTTP(S) proxy. The
# previous behaviour disabled certificate verification for *every* request as
# soon as any proxy env var was set, which silently exposed authenticated
# institutional sessions and publisher traffic to interception. To trust a
# self-signed connector CA, set REQUESTS_CA_BUNDLE (honoured natively by
# requests). Verification is only disabled by an explicit, deliberate opt-in.
_INSECURE_TLS_ENV = "INSTSCI_INSECURE_TLS"


def _resolve_ssl_verify() -> bool:
    opt_in = os.environ.get(_INSECURE_TLS_ENV, "").strip().lower()
    if opt_in in {"1", "true", "yes", "on"}:
        import urllib3

        urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
        logger.warning(
            "%s is set: TLS certificate verification is DISABLED for all HTTP "
            "requests. This exposes traffic -- including authenticated sessions "
            "-- to interception. Prefer REQUESTS_CA_BUNDLE to trust a "
            "self-signed connector CA instead.",
            _INSECURE_TLS_ENV,
        )
        return False
    return True


_SSL_VERIFY = _resolve_ssl_verify()


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
    kwargs.setdefault("verify", _SSL_VERIFY)

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
