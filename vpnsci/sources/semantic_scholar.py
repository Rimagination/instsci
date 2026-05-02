"""Paper search via Semantic Scholar API."""

import logging
import time
from dataclasses import dataclass, field

import requests

logger = logging.getLogger(__name__)

S2_API = "https://api.semanticscholar.org/graph/v1"
S2_FIELDS = "title,authors,year,abstract,externalIds,journal,citationCount,url"
MAX_RETRIES = 3


def _request_with_retry(url: str, params: dict) -> dict | None:
    """Make a GET request with retry on 429 rate limit."""
    for attempt in range(MAX_RETRIES):
        try:
            resp = requests.get(url, params=params, timeout=15)
            if resp.status_code == 404:
                return None
            if resp.status_code == 429:
                wait = 2 ** (attempt + 1)
                logger.warning("Rate limited by Semantic Scholar, retrying in %ds...", wait)
                time.sleep(wait)
                continue
            resp.raise_for_status()
            return resp.json()
        except requests.RequestException as e:
            logger.error("Semantic Scholar request failed: %s", e)
            if attempt < MAX_RETRIES - 1:
                time.sleep(2)
            else:
                return None
    return None


@dataclass
class SearchResult:
    """A single search result from Semantic Scholar."""

    title: str = ""
    authors: list[str] = field(default_factory=list)
    year: int | None = None
    abstract: str = ""
    doi: str = ""
    arxiv_id: str = ""
    journal: str = ""
    citation_count: int = 0
    s2_url: str = ""
    paper_id: str = ""


def search(
    query: str,
    limit: int = 10,
    year_range: str | None = None,
    fields_of_study: list[str] | None = None,
) -> list[SearchResult]:
    """Search for papers on Semantic Scholar.

    Args:
        query: Search query string.
        limit: Maximum number of results (max 100).
        year_range: Optional year filter, e.g., "2020-2024" or "2020-".
        fields_of_study: Optional list of fields, e.g., ["Physics", "Materials Science"].

    Returns:
        List of SearchResult objects.
    """
    params = {
        "query": query,
        "limit": min(limit, 100),
        "fields": S2_FIELDS,
    }
    if year_range:
        params["year"] = year_range
    if fields_of_study:
        params["fieldsOfStudy"] = ",".join(fields_of_study)

    data = _request_with_retry(f"{S2_API}/paper/search", params)
    if data is None:
        return []

    results = []
    for item in data.get("data", []):
        ext_ids = item.get("externalIds") or {}
        authors_data = item.get("authors") or []
        journal_data = item.get("journal") or {}

        result = SearchResult(
            title=item.get("title", ""),
            authors=[a.get("name", "") for a in authors_data if a.get("name")],
            year=item.get("year"),
            abstract=item.get("abstract") or "",
            doi=ext_ids.get("DOI", ""),
            arxiv_id=ext_ids.get("ArXiv", ""),
            journal=journal_data.get("name", "") if isinstance(journal_data, dict) else str(journal_data),
            citation_count=item.get("citationCount", 0),
            s2_url=item.get("url", ""),
            paper_id=item.get("paperId", ""),
        )
        results.append(result)

    return results


def get_paper(paper_id: str) -> SearchResult | None:
    """Get details for a specific paper by Semantic Scholar ID or DOI.

    Args:
        paper_id: S2 paper ID, DOI (prefixed with "DOI:"), or arXiv ID (prefixed with "ARXIV:").

    Returns:
        SearchResult or None if not found.
    """
    item = _request_with_retry(f"{S2_API}/paper/{paper_id}", {"fields": S2_FIELDS})
    if item is None:
        return None

    ext_ids = item.get("externalIds") or {}
    authors_data = item.get("authors") or []
    journal_data = item.get("journal") or {}

    return SearchResult(
        title=item.get("title", ""),
        authors=[a.get("name", "") for a in authors_data if a.get("name")],
        year=item.get("year"),
        abstract=item.get("abstract") or "",
        doi=ext_ids.get("DOI", ""),
        arxiv_id=ext_ids.get("ArXiv", ""),
        journal=journal_data.get("name", "") if isinstance(journal_data, dict) else str(journal_data),
        citation_count=item.get("citationCount", 0),
        s2_url=item.get("url", ""),
        paper_id=item.get("paperId", ""),
    )
