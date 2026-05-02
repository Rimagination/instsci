"""HTML extractor for ACS Publications articles."""

import re

from bs4 import BeautifulSoup


def can_handle(url: str) -> bool:
    """Check if this adapter can handle the given URL."""
    return "pubs.acs.org" in url.lower()


def extract(html: str, url: str = "") -> dict:
    """Extract paper content from ACS Publications HTML."""
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
        "h1.article_header-title",
        ".article-title",
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
        for el in soup.select(".loa li .hlFld-ContribAuthor"):
            name = el.get_text(strip=True)
            if name:
                authors.append(name)
    return authors


def _extract_abstract(soup: BeautifulSoup) -> str:
    for selector in [
        "div.article_abstract-content",
        "#abstractBox",
        "p.articleBody_abstractText",
    ]:
        el = soup.select_one(selector)
        if el:
            return _clean(el.get_text())
    return ""


def _extract_body(soup: BeautifulSoup) -> str:
    parts = []

    # ACS uses div.article_content or .NLM_sec elements
    article_content = soup.select_one("div.article_content")
    if article_content:
        for section in article_content.select(".NLM_sec"):
            heading = section.find(re.compile(r"h[2-4]"))
            heading_text = heading.get_text(strip=True) if heading else ""

            if heading_text.lower() in ("abstract", "references", "supporting information"):
                continue

            content = _clean(section.get_text())
            if heading_text and content:
                parts.append(f"## {heading_text}\n\n{content}")
            elif content:
                parts.append(content)

    if not parts and article_content:
        parts.append(_clean(article_content.get_text()))

    # Fallback
    if not parts:
        body = soup.select_one("article") or soup.select_one("#article-body")
        if body:
            parts.append(_clean(body.get_text()))

    return "\n\n".join(parts)


def _extract_figures(soup: BeautifulSoup) -> list[str]:
    captions = []
    for fig in soup.select("figure, .article_figure"):
        cap = fig.select_one("figcaption, .article_figure-caption")
        if cap:
            text = _clean(cap.get_text())
            if text and len(text) > 10:
                captions.append(text)
    return captions


def _extract_references(soup: BeautifulSoup) -> list[str]:
    refs = []
    ref_section = soup.select_one("#references") or soup.select_one(".article_references")
    if ref_section:
        for li in ref_section.find_all("li"):
            text = _clean(li.get_text())
            if text and len(text) > 20:
                refs.append(text)
    return refs


def _clean(text: str) -> str:
    return re.sub(r"\s+", " ", text).strip()
