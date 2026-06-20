"""Elsevier RetrievalAPI integration for fetching full-text articles."""

import logging
import os
import xml.etree.ElementTree as ET

import requests

from ..pdf_bytes import describe_non_pdf_bytes, is_plausible_pdf_bytes

logger = logging.getLogger(__name__)

ELSEVIER_API = "https://api.elsevier.com/content"


def get_api_key(config_key: str = "") -> str:
    """Get Elsevier API key from config or environment."""
    return config_key or os.environ.get("ELSEVIER_API_KEY", "")


def fetch_pdf(doi: str, api_key: str, inst_token: str = "") -> bytes | None:
    """Download PDF directly via Elsevier API (scansci-pdf pattern).

    Uses Accept: application/pdf to get the PDF binary directly.
    Requires API key; institutional token enables full-text campus access.
    """
    if not api_key:
        return None

    url = f"{ELSEVIER_API}/article/doi/{doi}"
    headers = {
        "X-ELS-APIKey": api_key,
        "Accept": "application/pdf",
    }
    if inst_token:
        headers["X-ELS-InstToken"] = inst_token

    try:
        session = requests.Session()
        session.trust_env = False
        resp = session.get(url, headers=headers, timeout=30, allow_redirects=True)
    except requests.exceptions.SSLError:
        # Retry without SSL verification (e.g. behind corporate proxy)
        try:
            resp = session.get(url, headers=headers, timeout=30, allow_redirects=True, verify=False)
        except requests.RequestException as e:
            logger.warning("Elsevier API PDF request failed: %s", e)
            return None
    except requests.RequestException as e:
        logger.warning("Elsevier API PDF request failed: %s", e)
        return None

    if resp.status_code != 200:
        if resp.status_code in (401, 403):
            logger.warning("Elsevier API: HTTP %d (key invalid or insufficient)", resp.status_code)
        elif resp.status_code == 429:
            logger.warning("Elsevier API: rate limited")
        else:
            logger.info("Elsevier API: HTTP %d for %s", resp.status_code, doi)
        return None

    if not is_plausible_pdf_bytes(resp.content):
        content_type = resp.headers.get("content-type", "")
        logger.info(
            "Elsevier API returned non-PDF (%s, %s); key may lack full-text access",
            content_type[:50],
            describe_non_pdf_bytes(resp.content),
        )
        return None

    content_type = resp.headers.get("content-type", "")
    is_pdf = "pdf" in content_type or resp.content[:5] == b"%PDF-"

    if not is_pdf:
        logger.info("Elsevier API returned non-PDF (%s) — key may lack full-text access", content_type[:50])
        return None

    if len(resp.content) < 10000:
        logger.warning("Elsevier API returned suspiciously small PDF (%d bytes)", len(resp.content))
        return None

    logger.info("Elsevier API: downloaded %d bytes for %s", len(resp.content), doi)
    return resp.content


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
        session = requests.Session()
        session.trust_env = False
        resp = session.get(url, headers=headers, timeout=30, allow_redirects=True)
    except requests.exceptions.SSLError:
        try:
            resp = session.get(url, headers=headers, timeout=30, allow_redirects=True, verify=False)
        except requests.RequestException as e:
            logger.warning("Elsevier API request failed: %s", e)
            return None
    except requests.RequestException as e:
        logger.warning("Elsevier API request failed: %s", e)
        return None

    if resp.status_code == 401:
        logger.warning("Elsevier API: invalid API key")
        return None
    if resp.status_code == 404:
        logger.info("Elsevier API: DOI %s not found", doi)
        return None
    if resp.status_code != 200:
        logger.info("Elsevier API: HTTP %d for %s", resp.status_code, doi)
        return None

    return _parse_xml(resp.text)


def _parse_xml(xml_text: str) -> dict | None:
    """Parse Elsevier XML response into structured data.

    Uses local name matching (split on '}') to handle the multiple namespaces
    in Elsevier XML (dc, prism, xocs, etc.).
    """
    try:
        root = ET.fromstring(xml_text)
    except ET.ParseError as e:
        logger.warning("Failed to parse Elsevier XML: %s", e)
        return None

    result = {
        "title": "",
        "authors": [],
        "abstract": "",
        "full_text": "",
        "figures": [],
        "references": [],
    }

    # Title — find by local name
    for el in root.iter():
        local = el.tag.split("}")[-1] if "}" in el.tag else el.tag
        if local == "title" and el.text and el.text.strip():
            result["title"] = el.text.strip()
            break

    # Authors — find by local name
    for el in root.iter():
        local = el.tag.split("}")[-1] if "}" in el.tag else el.tag
        if local in ("creator", "author"):
            if el.text and el.text.strip():
                result["authors"].append(el.text.strip())
    # Also try structured author elements
    if not result["authors"]:
        for el in root.iter():
            local = el.tag.split("}")[-1] if "}" in el.tag else el.tag
            if local == "author":
                given = ""
                surname = ""
                for child in el:
                    child_local = child.tag.split("}")[-1] if "}" in child.tag else child.tag
                    if "given" in child_local:
                        given = (child.text or "").strip()
                    elif "surname" in child_local or "last" in child_local:
                        surname = (child.text or "").strip()
                if given or surname:
                    result["authors"].append(f"{given} {surname}".strip())

    # Abstract
    result["abstract"] = _extract_abstract(root, {})

    # Body
    result["full_text"] = _extract_body(root, {})

    # References
    result["references"] = _extract_references(root, {})

    return result


def _extract_abstract(root: ET.Element, nsmap: dict) -> str:
    """Extract abstract text by local name."""
    for el in root.iter():
        local = el.tag.split("}")[-1] if "}" in el.tag else el.tag
        if local in ("abstract", "description") and _collect_text(el).strip():
            return _collect_text(el).strip()
    return ""


def _extract_body(root: ET.Element, nsmap: dict) -> str:
    """Extract article body text with section structure.

    Elsevier XML nests the body under originalText > doc > serial-item > article > body.
    The body element is in the xocs namespace, so we use iter() to find by local name.
    """
    parts = []

    # Find body element — search by local name to handle any namespace
    body = None
    for el in root.iter():
        local = el.tag.split("}")[-1] if "}" in el.tag else el.tag
        if local == "body":
            body = el
            break

    if body is None:
        return ""

    # Extract sections — search by local name to handle any namespace
    def _find_sections(el):
        """Recursively find all section elements."""
        local = el.tag.split("}")[-1] if "}" in el.tag else el.tag
        if local == "section":
            yield el
        for child in el:
            yield from _find_sections(child)

    for section in _find_sections(body):
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
        for child in body.iter():
            tag = child.tag.split("}")[-1] if "}" in child.tag else child.tag
            if tag == "para":
                text = _collect_text(child).strip()
                if text:
                    parts.append(text)

    return "\n\n".join(parts)


def _extract_references(root: ET.Element, nsmap: dict) -> list[str]:
    """Extract bibliography references."""
    refs = []

    for el in root.iter():
        local = el.tag.split("}")[-1] if "}" in el.tag else el.tag
        if local in ("bib-reference", "reference"):
            text = " ".join(_collect_text(el).split())
            if text and len(text) > 10:
                refs.append(text)

    if refs:
        return refs

    for el in root.iter():
        local = el.tag.split("}")[-1] if "}" in el.tag else el.tag
        if local == "bibliography":
            for ref in el:
                text = " ".join(_collect_text(ref).split())
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
