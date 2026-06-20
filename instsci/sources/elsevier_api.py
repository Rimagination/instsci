"""Elsevier RetrievalAPI integration for fetching full-text articles."""

from dataclasses import dataclass
import logging
import os
import re
import xml.etree.ElementTree as ET
from urllib.parse import quote

import requests

from ..pdf_bytes import describe_non_pdf_bytes, is_plausible_pdf_bytes

logger = logging.getLogger(__name__)

ELSEVIER_API = "https://api.elsevier.com/content"
ELSEVIER_OBJECT_API = f"{ELSEVIER_API}/object/eid"
FULL_VIEW_PARAMS = {"view": "FULL"}
MIN_ELSEVIER_PDF_BYTES = 10_000
_EID_RE = re.compile(r"\b1-s2\.0-[A-Za-z0-9]+(?:-[A-Za-z0-9_.]+)?(?:\.pdf)?\b", re.IGNORECASE)
_SUPPLEMENT_HINTS = (
    "supplement",
    "supplementary",
    "mmc",
    "appendix",
    "graphical",
    "thumbnail",
    "image",
    "figure",
)
_MAIN_HINTS = ("main", "web-pdf", "full-text", "fulltext", "pdf", "attachment")


@dataclass(frozen=True)
class _Route:
    name: str
    proxies: dict[str, str] | None = None


def get_api_key(config_key: str = "") -> str:
    """Get Elsevier API key from config or environment."""
    return config_key or os.environ.get("ELSEVIER_API_KEY", "")


def fetch_pdf(
    doi: str,
    api_key: str,
    inst_token: str = "",
    *,
    proxy_url: str = "",
    pdf_eids: list[str] | None = None,
    preferred_route: str = "",
) -> bytes | None:
    """Download the MAIN PDF through Elsevier's XML/object-eid API route.

    The stable full-text route is:
    Article Retrieval API with ``view=FULL`` -> MAIN PDF ``attachment-eid`` /
    ``object-eid`` -> Content Object API ``/content/object/eid/{eid}``.
    Direct ``Accept: application/pdf`` article retrieval can return previews, so
    it is intentionally not used as the primary path.
    """
    if not api_key:
        return None

    for route in _route_options(proxy_url, preferred_route=preferred_route):
        eids = list(pdf_eids or [])
        if not eids:
            resp = _get_article_xml_response(doi, api_key, inst_token, route)
            if resp is None:
                continue
            if resp.status_code != 200:
                _log_api_response("XML", doi, route, resp)
                continue
            eids = _extract_main_pdf_eids(resp.text)
            if not eids:
                logger.info("Elsevier API XML route=%s returned no MAIN PDF object EID for %s", route.name, doi)
                continue

        pdf = _fetch_object_pdf(doi, eids, api_key, inst_token, route)
        if pdf:
            return pdf

    return None


def fetch_fulltext(
    doi: str,
    api_key: str,
    inst_token: str = "",
    *,
    proxy_url: str = "",
) -> dict | None:
    """Fetch article full text via Elsevier RetrievalAPI.

    Args:
        doi: The article DOI.
        api_key: Elsevier API key.
        inst_token: Optional institutional token for enhanced access.

    Returns:
        Dict with title, authors, abstract, full_text, figures, references,
        pdf_eids, api_route, or None if fetch failed.
    """
    if not api_key:
        return None

    for route in _route_options(proxy_url):
        resp = _get_article_xml_response(doi, api_key, inst_token, route)
        if resp is None:
            continue

        if resp.status_code in (401, 403):
            _log_api_response("XML", doi, route, resp, level=logging.WARNING)
            continue
        if resp.status_code == 404:
            logger.info("Elsevier API XML route=%s DOI %s not found", route.name, doi)
            continue
        if resp.status_code != 200:
            _log_api_response("XML", doi, route, resp)
            continue

        parsed = _parse_xml(resp.text)
        if parsed is None:
            continue

        parsed["pdf_eids"] = _extract_main_pdf_eids(resp.text)
        parsed["api_route"] = route.name
        parsed["els_status"] = resp.headers.get("X-ELS-Status", "")
        return parsed

    return None


def _get_article_xml_response(
    doi: str,
    api_key: str,
    inst_token: str,
    route: _Route,
) -> requests.Response | None:
    url = f"{ELSEVIER_API}/article/doi/{quote(doi, safe='')}"
    headers = {
        "X-ELS-APIKey": api_key,
        "Accept": "application/xml",
    }
    if inst_token:
        headers["X-ELS-Insttoken"] = inst_token

    return _api_get(url, headers=headers, params=FULL_VIEW_PARAMS, route=route, timeout=30)


def _fetch_object_pdf(
    doi: str,
    eids: list[str],
    api_key: str,
    inst_token: str,
    route: _Route,
) -> bytes | None:
    headers = {
        "X-ELS-APIKey": api_key,
        "Accept": "application/pdf",
    }
    if inst_token:
        headers["X-ELS-Insttoken"] = inst_token

    for eid in _dedupe(eids):
        url = f"{ELSEVIER_OBJECT_API}/{quote(eid, safe='')}"
        resp = _api_get(url, headers=headers, route=route, timeout=45)
        if resp is None:
            continue
        if resp.status_code != 200:
            _log_api_response("object", doi, route, resp, eid=eid)
            continue

        content_type = resp.headers.get("content-type", "")
        if not is_plausible_pdf_bytes(resp.content, min_bytes=MIN_ELSEVIER_PDF_BYTES):
            logger.info(
                "Elsevier object route=%s eid=%s returned non-PDF (%s, %s)",
                route.name,
                eid,
                content_type[:50],
                describe_non_pdf_bytes(resp.content, min_bytes=MIN_ELSEVIER_PDF_BYTES),
            )
            continue

        page_count = _pdf_page_count(resp.content)
        if page_count == 1:
            logger.warning("Elsevier object route=%s eid=%s returned a one-page PDF preview; rejecting", route.name, eid)
            continue

        logger.info(
            "Elsevier API object route=%s downloaded %d bytes for %s via %s",
            route.name,
            len(resp.content),
            doi,
            eid,
        )
        return resp.content

    return None


def _api_get(
    url: str,
    *,
    headers: dict[str, str],
    route: _Route,
    params: dict[str, str] | None = None,
    timeout: int = 30,
) -> requests.Response | None:
    try:
        session = requests.Session()
        session.trust_env = False
        if route.proxies:
            session.proxies = route.proxies
        return session.get(url, headers=headers, params=params, timeout=timeout, allow_redirects=True)
    except requests.exceptions.SSLError:
        # Retry without SSL verification (e.g. behind corporate proxy).
        try:
            return session.get(
                url,
                headers=headers,
                params=params,
                timeout=timeout,
                allow_redirects=True,
                verify=False,
            )
        except requests.RequestException as e:
            logger.warning("Elsevier API request failed on route=%s: %s", route.name, e)
            return None
    except requests.RequestException as e:
        logger.warning("Elsevier API request failed on route=%s: %s", route.name, e)
        return None


def _route_options(proxy_url: str = "", *, preferred_route: str = "") -> list[_Route]:
    routes = [_Route("direct")]
    if proxy_url:
        routes.append(_Route("configured_proxy", {"http": proxy_url, "https": proxy_url}))
    if preferred_route:
        routes.sort(key=lambda route: 0 if route.name == preferred_route else 1)
    return routes


def _log_api_response(
    stage: str,
    doi: str,
    route: _Route,
    resp: requests.Response,
    *,
    eid: str = "",
    level: int = logging.INFO,
) -> None:
    els_status = resp.headers.get("X-ELS-Status", "")
    content_type = resp.headers.get("content-type", "")
    extra = f" eid={eid}" if eid else ""
    logger.log(
        level,
        "Elsevier API %s route=%s HTTP %d for %s%s (X-ELS-Status=%s, content-type=%s, bytes=%d)",
        stage,
        route.name,
        resp.status_code,
        doi,
        extra,
        els_status or "-",
        content_type[:60] or "-",
        len(resp.content or b""),
    )


def _extract_main_pdf_eids(xml_text: str) -> list[str]:
    """Extract likely MAIN PDF Content Object EIDs from Elsevier XML."""
    try:
        root = ET.fromstring(xml_text)
    except (ET.ParseError, TypeError):
        return []

    parent_map = {child: parent for parent in root.iter() for child in parent}
    candidates: list[tuple[int, str]] = []

    for el in root.iter():
        context = _candidate_context(el, parent_map)
        values = list(el.attrib.values())
        if el.text:
            values.append(el.text)
        for value in values:
            for raw_eid in _EID_RE.findall(value or ""):
                normalized = _normalize_pdf_eid(raw_eid)
                if _looks_supplementary(normalized, context):
                    continue
                candidates.append((_score_pdf_eid(normalized, context), normalized))

    if not candidates:
        return []

    candidates.sort(key=lambda item: item[0], reverse=True)
    return _dedupe([eid for _, eid in candidates])


def _candidate_context(el: ET.Element, parent_map: dict[ET.Element, ET.Element]) -> str:
    parts: list[str] = []
    current: ET.Element | None = el
    for _ in range(3):
        if current is None:
            break
        parts.append(_local_name(current.tag))
        parts.extend(_local_name(k) for k in current.attrib)
        parts.extend(str(v) for v in current.attrib.values())
        if current.text:
            parts.append(current.text)
        current = parent_map.get(current)
    return " ".join(parts).lower()


def _normalize_pdf_eid(eid: str) -> str:
    eid = eid.strip()
    if eid.lower().endswith(".pdf"):
        return eid
    if eid.lower().endswith("-main"):
        return f"{eid}.pdf"
    return f"{eid}-main.pdf"


def _score_pdf_eid(eid: str, context: str) -> int:
    score = 0
    lower = eid.lower()
    if lower.endswith("-main.pdf"):
        score += 80
    if lower.endswith(".pdf"):
        score += 20
    for hint in _MAIN_HINTS:
        if hint in context:
            score += 10
    return score


def _looks_supplementary(eid: str, context: str) -> bool:
    haystack = f"{eid} {context}".lower()
    return any(hint in haystack for hint in _SUPPLEMENT_HINTS)


def _local_name(tag: str) -> str:
    return tag.split("}")[-1] if "}" in tag else tag


def _dedupe(values: list[str]) -> list[str]:
    seen: set[str] = set()
    result: list[str] = []
    for value in values:
        key = value.lower()
        if key in seen:
            continue
        seen.add(key)
        result.append(value)
    return result


def _pdf_page_count(pdf_bytes: bytes) -> int | None:
    try:
        import fitz

        with fitz.open(stream=pdf_bytes, filetype="pdf") as doc:
            return doc.page_count
    except Exception:
        return None


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
