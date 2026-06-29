"""Open Access detection via Unpaywall API."""

import logging
from dataclasses import dataclass, field

import requests

from ..http_utils import request_with_retry

logger = logging.getLogger(__name__)

UNPAYWALL_API = "https://api.unpaywall.org/v2"


@dataclass
class OAResult:
    """Result from Unpaywall OA lookup."""

    is_oa: bool = False
    pdf_url: str = ""
    html_url: str = ""
    source: str = ""  # "arxiv", "publisher", "repository"
    title: str = ""
    authors: list[str] | None = None
    journal: str = ""
    year: int | None = None
    pdf_urls: list[str] = field(default_factory=list)


def collect_pdf_urls(best_oa: dict, oa_locations: list[dict]) -> list[str]:
    """Return ordered, de-duplicated candidate PDF URLs.

    The best OA location comes first, followed by every other OA location, so a
    caller can fall back to a repository copy when the preferred (often
    publisher) link is forbidden or broken.
    """
    urls: list[str] = []
    candidates = [best_oa, *oa_locations] if best_oa else list(oa_locations)
    for loc in candidates:
        pdf = (loc.get("url_for_pdf") or "").strip()
        if pdf and pdf not in urls:
            urls.append(pdf)
    return urls


def check_oa(doi: str, email: str = "instsci@example.com") -> OAResult:
    """Check if a DOI has an Open Access version via Unpaywall.

    Args:
        doi: The DOI to check.
        email: Email for Unpaywall API (required by their ToS).

    Returns:
        OAResult with OA URLs if available.
    """
    url = f"{UNPAYWALL_API}/{doi}?email={email}"
    try:
        resp = request_with_retry("GET", url, timeout=10)
        if resp.status_code == 404:
            logger.info("DOI %s not found in Unpaywall.", doi)
            return OAResult()
        resp.raise_for_status()
        data = resp.json()
    except requests.RequestException as e:
        logger.warning("Unpaywall API request failed for %s: %s", doi, e)
        return OAResult()

    result = OAResult(
        is_oa=data.get("is_oa", False),
        title=data.get("title", ""),
        journal=data.get("journal_name", ""),
        year=data.get("year"),
    )

    # Extract authors
    authorships = data.get("z_authors") or []
    result.authors = []
    for a in authorships:
        name_parts = []
        if a.get("given"):
            name_parts.append(a["given"])
        if a.get("family"):
            name_parts.append(a["family"])
        if name_parts:
            result.authors.append(" ".join(name_parts))

    if not result.is_oa:
        return result

    # Find best OA location
    best_oa = data.get("best_oa_location") or {}
    oa_locations = data.get("oa_locations") or []

    # Check best OA location first
    if best_oa:
        result.pdf_url = best_oa.get("url_for_pdf", "") or ""
        result.html_url = best_oa.get("url_for_landing_page", "") or ""
        host_type = best_oa.get("host_type", "")
        repo_inst = best_oa.get("repository_institution", "") or ""

        if "arxiv" in (result.pdf_url + result.html_url + repo_inst).lower():
            result.source = "arxiv"
        elif host_type == "publisher":
            result.source = "publisher"
        else:
            result.source = "repository"

    # Collect every candidate PDF URL (best first, then all locations) so a
    # forbidden or broken link can fall back to a repository copy.
    result.pdf_urls = collect_pdf_urls(best_oa, oa_locations)
    if not result.pdf_url and result.pdf_urls:
        result.pdf_url = result.pdf_urls[0]

    return result
