"""HTML extractor for Wiley Online Library articles."""

import re

from bs4 import BeautifulSoup


def can_handle(url: str) -> bool:
    """Check if this adapter can handle the given URL."""
    return "wiley.com" in url.lower() or "onlinelibrary.wiley" in url.lower()


def extract(html: str, url: str = "") -> dict:
    """Extract paper content from Wiley Online Library HTML."""
    soup = BeautifulSoup(html, "lxml")

    for tag in soup.find_all(["script", "style", "nav"]):
        tag.decompose()

    return {
        "title": _extract_title(soup),
        "authors": _extract_authors(soup),
        "abstract": _extract_abstract(soup),
        "full_text": _extract_body(soup),
        "figures": _extract_figures(soup),
        "references": _extract_references(soup),
    }


def _extract_title(soup: BeautifulSoup) -> str:
    for selector in [
        "h1.citation__title",
        ".article-header__title",
        "meta[name='citation_title']",
    ]:
        el = soup.select_one(selector)
        if el:
            if el.name == "meta":
                return el.get("content", "").strip()
            return el.get_text(strip=True)
    return ""


def _extract_authors(soup: BeautifulSoup) -> list[str]:
    authors = []
    for meta in soup.select("meta[name='citation_author']"):
        name = meta.get("content", "").strip()
        if name:
            authors.append(name)
    if not authors:
        for el in soup.select(".loa-authors .author-name span"):
            name = el.get_text(strip=True)
            if name:
                authors.append(name)
    return authors


def _extract_abstract(soup: BeautifulSoup) -> str:
    for selector in [
        "section.article-section__abstract",
        "div.abstract-group",
        "#abstract",
    ]:
        el = soup.select_one(selector)
        if el:
            return _clean(el.get_text())
    return ""


def _extract_body(soup: BeautifulSoup) -> str:
    parts = []

    # Wiley uses article-section__content divs
    for section in soup.select("section.article-section__content"):
        heading = section.find_previous("h2")
        heading_text = heading.get_text(strip=True) if heading else ""

        # Skip references and abstract sections
        if heading_text.lower() in ("abstract", "references", "supporting information"):
            continue

        content = _clean(section.get_text())
        if heading_text and content:
            parts.append(f"## {heading_text}\n\n{content}")
        elif content:
            parts.append(content)

    # Fallback
    if not parts:
        body = soup.select_one("article.article__body") or soup.select_one(".article-body-section")
        if body:
            parts.append(_clean(body.get_text()))

    return "\n\n".join(parts)


def _extract_figures(soup: BeautifulSoup) -> list[str]:
    captions = []
    for fig in soup.select("figure"):
        cap = fig.select_one("figcaption")
        if cap:
            text = _clean(cap.get_text())
            if text and len(text) > 10:
                captions.append(text)
    return captions


def _extract_references(soup: BeautifulSoup) -> list[str]:
    refs = []
    ref_section = soup.select_one("section#references-section")
    if ref_section:
        for li in ref_section.find_all("li"):
            text = _clean(li.get_text())
            if text and len(text) > 20:
                refs.append(text)
    return refs


def _clean(text: str) -> str:
    return re.sub(r"\s+", " ", text).strip()
