"""DOI-matched OA candidates from the OpenAIRE Graph API v3."""

import logging
from urllib.parse import unquote, urlsplit

import requests

from ..http_utils import request_with_retry
from .unpaywall import OAResult

logger = logging.getLogger(__name__)
OPENAIRE_API = "https://api.openaire.eu/graph/v3/research-products"


def _doi_key(value: str) -> str:
    value = str(value or "").strip()
    if value.lower().startswith(("https://doi.org/", "http://doi.org/", "https://dx.doi.org/", "http://dx.doi.org/")):
        value = unquote(urlsplit(value).path.lstrip("/"))
    return value.lower()


def extract_oa_locations(payload: dict, doi: str, *, limit: int = 5) -> list[OAResult]:
    """Use instance-level access rights; unrelated nested URLs are ignored."""
    candidates = []
    seen = set()
    products = payload.get("results") if isinstance(payload, dict) else None
    if not isinstance(products, list):
        return []
    for product in products:
        if not isinstance(product, dict):
            continue
        pids = product.get("pids") or []
        if not isinstance(pids, list) or not any(
            isinstance(pid, dict) and str(pid.get("scheme", "")).lower() == "doi"
            and _doi_key(pid.get("value", "")) == _doi_key(doi) for pid in pids
        ):
            continue
        instances = product.get("instances") or []
        if not isinstance(instances, list):
            continue
        for instance in instances:
            if not isinstance(instance, dict):
                continue
            access = instance.get("accessRight") or {}
            if not isinstance(access, dict):
                continue
            if access.get("code") != "c_abf2" and str(access.get("label", "")).lower() not in {"open", "open access"}:
                continue
            urls = instance.get("urls") or []
            if not isinstance(urls, list):
                continue
            for url in urls:
                if not isinstance(url, str):
                    continue
                url = url.strip()
                try:
                    parsed = urlsplit(url)
                    if parsed.scheme not in {"http", "https"} or not parsed.hostname or parsed.username or parsed.password:
                        continue
                except ValueError:
                    continue
                if parsed.hostname in {"doi.org", "dx.doi.org"} or url in seen:
                    continue
                seen.add(url)
                is_pdf = unquote(parsed.path).lower().endswith(".pdf")
                candidates.append(OAResult(
                    is_oa=True,
                    pdf_url=url if is_pdf else "",
                    html_url="" if is_pdf else url,
                    source="arxiv" if parsed.hostname in {"arxiv.org", "www.arxiv.org"} else "openaire",
                    title=str(product.get("mainTitle") or ""),
                ))
    # ponytail: bound slow repository probes to five candidates by default;
    # raise the limit or rank by measured host health if recall needs it.
    return sorted(candidates, key=lambda candidate: not bool(candidate.pdf_url))[:max(0, limit)]


def find_oa_locations(doi: str) -> list[OAResult]:
    """Return candidates only; an API hit is not a PDF success verdict."""
    try:
        with request_with_retry(
            "GET", OPENAIRE_API, params={"pid": doi, "type": "publication", "pageSize": 10},
            timeout=10, max_retries=1, max_retry_delay=10,
        ) as response:
            response.raise_for_status()
            return extract_oa_locations(response.json(), doi)
    except (requests.RequestException, ValueError) as exc:
        logger.warning("OpenAIRE lookup failed for %s: %s", doi, type(exc).__name__)
        return []
