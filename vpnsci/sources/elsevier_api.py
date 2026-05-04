"""Elsevier RetrievalAPI integration for fetching full-text articles."""

import logging
import os
import re
import xml.etree.ElementTree as ET

import requests

from ..http_utils import request_with_retry

logger = logging.getLogger(__name__)

ELSEVIER_API = "https://api.elsevier.com/content"


def get_api_key(config_key: str = "") -> str:
    """Get Elsevier API key from config or environment."""
    return config_key or os.environ.get("ELSEVIER_API_KEY", "")


def fetch_fulltext(doi: str, api_key: str, inst_token: str = "") -> dict | None:
    """Fetch article full text via Elsevier RetrievalAPI.

    Args:
        doi: The article DOI.
        api_key: Elsevier API key.
        inst_token: Optional institutional token for enhanced access.

    Returns:
        Dict with title, authors, abstract, full_text, figures, references,
        or None if fetch failed.
    """
    if not api_key:
        return None

    url = f"{ELSEVIER_API}/article/doi/{doi}"
    headers = {
        "X-ELS-APIKey": api_key,
        "Accept": "application/xml",
    }
    if inst_token:
        headers["X-ELS-Insttoken"] = inst_token

    try:
        resp = request_with_retry("GET", url, headers=headers, timeout=30)
        if resp.status_code == 401:
            logger.warning("Elsevier API: invalid API key")
            return None
        if resp.status_code == 404:
            logger.info("Elsevier API: DOI %s not found", doi)
            return None
        resp.raise_for_status()
    except requests.RequestException as e:
        logger.warning("Elsevier API request failed: %s", e)
        return None

    return _parse_xml(resp.text)


def _parse_xml(xml_text: str) -> dict | None:
    """Parse Elsevier XML response into structured data."""
    try:
        root = ET.fromstring(xml_text)
    except ET.ParseError as e:
        logger.warning("Failed to parse Elsevier XML: %s", e)
        return None

    # Handle namespace
    ns = _detect_namespace(root)
    nsmap = {"ns": ns} if ns else {}

    result = {
        "title": "",
        "authors": [],
        "abstract": "",
        "full_text": "",
        "figures": [],
        "references": [],
    }

    # Title
    result["title"] = _find_text(root, [
        "dc:title", "title", "ns:dc:title", "ns:title"
    ], nsmap)

    # Authors
    result["authors"] = _extract_authors(root, nsmap)

    # Abstract
    result["abstract"] = _extract_abstract(root, nsmap)

    # Body
    result["full_text"] = _extract_body(root, nsmap)

    # References
    result["references"] = _extract_references(root, nsmap)

    return result


def _detect_namespace(root: ET.Element) -> str:
    """Extract namespace from root element tag."""
    m = re.match(r"\{(.+?)\}", root.tag)
    return m.group(1) if m else ""


def _find_text(el: ET.Element, tags: list[str], nsmap: dict) -> str:
    """Find first matching tag's text content."""
    for tag in tags:
        found = el.find(tag, nsmap) if nsmap else el.find(tag)
        if found is not None and found.text:
            return found.text.strip()
    return ""


def _extract_authors(root: ET.Element, nsmap: dict) -> list[str]:
    """Extract author names from XML."""
    authors = []
    # Try dc:creator first
    for tag in ["dc:creator", "creator", "ns:dc:creator", "ns:creator"]:
        for el in root.iter(tag):
            if el.text:
                authors.append(el.text.strip())
        if authors:
            return authors

    # Try structured author elements
    for el in root.iter("author"):
        given = ""
        surname = ""
        for child in el:
            if "given-name" in child.tag or "given" in child.tag:
                given = (child.text or "").strip()
            elif "surname" in child.tag or "last" in child.tag:
                surname = (child.text or "").strip()
        if given or surname:
            authors.append(f"{given} {surname}".strip())

    return authors


def _extract_abstract(root: ET.Element, nsmap: dict) -> str:
    """Extract abstract text."""
    for tag in ["abstract", "ns:abstract", "dc:description", "ns:dc:description"]:
        el = root.find(f".//{tag}", nsmap) if nsmap else root.find(f".//{tag}")
        if el is not None:
            return _collect_text(el).strip()
    return ""


def _extract_body(root: ET.Element, nsmap: dict) -> str:
    """Extract article body text with section structure."""
    parts = []

    # Find body element
    body = None
    for tag in ["body", "ns:body", "originalText", "ns:originalText"]:
        body = root.find(f".//{tag}", nsmap) if nsmap else root.find(f".//{tag}")
        if body is not None:
            break

    if body is None:
        return ""

    # Recursively extract sections
    for section in body.iter("section"):
        heading = ""
        content_parts = []

        for child in section:
            tag = child.tag.split("}")[-1] if "}" in child.tag else child.tag
            if tag in ("section-title", "sectiontitle", "heading"):
                heading = _collect_text(child).strip()
            elif tag == "para":
                text = _collect_text(child).strip()
                if text:
                    content_parts.append(text)

        if heading and content_parts:
            parts.append(f"## {heading}\n\n{' '.join(content_parts)}")
        elif content_parts:
            parts.append(" ".join(content_parts))

    # Fallback: collect all para text if no sections found
    if not parts:
        for para in body.iter("para"):
            text = _collect_text(para).strip()
            if text:
                parts.append(text)

    return "\n\n".join(parts)


def _extract_references(root: ET.Element, nsmap: dict) -> list[str]:
    """Extract bibliography references."""
    refs = []

    for tag in ["bibliography", "ns:bibliography", "bib-reference", "ns:bib-reference"]:
        for el in root.iter(tag):
            for ref in el:
                text = _collect_text(ref).strip()
                if text and len(text) > 10:
                    refs.append(text)
            if refs:
                return refs

    return refs


def _collect_text(el: ET.Element) -> str:
    """Recursively collect all text content from an element."""
    parts = []
    if el.text:
        parts.append(el.text)
    for child in el:
        parts.append(_collect_text(child))
        if child.tail:
            parts.append(child.tail)
    return " ".join(parts)
