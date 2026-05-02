"""arXiv paper fetching."""

import logging
import re
import xml.etree.ElementTree as ET

import requests

logger = logging.getLogger(__name__)

ARXIV_API = "http://export.arxiv.org/api/query"
ARXIV_PDF_BASE = "https://arxiv.org/pdf"
ARXIV_ABS_BASE = "https://arxiv.org/abs"

# Matches arXiv IDs like 2301.08745 or hep-ph/0601001
ARXIV_ID_PATTERN = re.compile(r"(\d{4}\.\d{4,5}(?:v\d+)?|[a-z-]+/\d{7}(?:v\d+)?)")


def extract_arxiv_id(text: str) -> str | None:
    """Extract an arXiv ID from a string (URL, DOI, or raw ID).

    Args:
        text: Input that may contain an arXiv ID.

    Returns:
        arXiv ID string or None.
    """
    # Remove version suffix for matching
    match = ARXIV_ID_PATTERN.search(text)
    return match.group(1) if match else None


def get_pdf_url(arxiv_id: str) -> str:
    """Get the PDF download URL for an arXiv paper."""
    # Strip version for clean URL
    clean_id = re.sub(r"v\d+$", "", arxiv_id)
    return f"{ARXIV_PDF_BASE}/{clean_id}.pdf"


def fetch_metadata(arxiv_id: str) -> dict:
    """Fetch metadata for an arXiv paper via the arXiv API.

    Args:
        arxiv_id: The arXiv ID (e.g., "2301.08745").

    Returns:
        Dict with title, authors, abstract, year, etc.
    """
    clean_id = re.sub(r"v\d+$", "", arxiv_id)
    params = {"id_list": clean_id, "max_results": 1}

    try:
        resp = requests.get(ARXIV_API, params=params, timeout=10)
        resp.raise_for_status()
    except requests.RequestException as e:
        logger.warning("arXiv API request failed for %s: %s", arxiv_id, e)
        return {}

    # Parse Atom XML
    ns = {
        "atom": "http://www.w3.org/2005/Atom",
        "arxiv": "http://arxiv.org/schemas/atom",
    }

    try:
        root = ET.fromstring(resp.text)
    except ET.ParseError as e:
        logger.warning("Failed to parse arXiv API response: %s", e)
        return {}

    entry = root.find("atom:entry", ns)
    if entry is None:
        return {}

    title_el = entry.find("atom:title", ns)
    summary_el = entry.find("atom:summary", ns)
    published_el = entry.find("atom:published", ns)

    authors = []
    for author_el in entry.findall("atom:author", ns):
        name_el = author_el.find("atom:name", ns)
        if name_el is not None and name_el.text:
            authors.append(name_el.text.strip())

    title = title_el.text.strip().replace("\n", " ") if title_el is not None and title_el.text else ""
    abstract = summary_el.text.strip().replace("\n", " ") if summary_el is not None and summary_el.text else ""

    year = None
    if published_el is not None and published_el.text:
        year_match = re.match(r"(\d{4})", published_el.text)
        if year_match:
            year = int(year_match.group(1))

    return {
        "title": title,
        "authors": authors,
        "abstract": abstract,
        "year": year,
        "arxiv_id": clean_id,
        "pdf_url": get_pdf_url(clean_id),
        "url": f"{ARXIV_ABS_BASE}/{clean_id}",
    }


def download_pdf(arxiv_id: str, output_path: str) -> bool:
    """Download an arXiv paper's PDF.

    Args:
        arxiv_id: The arXiv ID.
        output_path: Where to save the PDF.

    Returns:
        True if download succeeded.
    """
    url = get_pdf_url(arxiv_id)
    try:
        resp = requests.get(url, timeout=60, stream=True)
        resp.raise_for_status()
        with open(output_path, "wb") as f:
            for chunk in resp.iter_content(chunk_size=8192):
                f.write(chunk)
        logger.info("Downloaded arXiv PDF to %s", output_path)
        return True
    except (requests.RequestException, OSError) as e:
        logger.error("Failed to download arXiv PDF %s: %s", arxiv_id, e)
        return False
