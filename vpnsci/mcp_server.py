"""MCP server exposing vpnsci tools for AI agents supporting MCP protocol."""

import asyncio
import logging
import sys
from pathlib import Path

from mcp.server.fastmcp import FastMCP

from .config import Config
from .fetcher import PaperFetcher
from .sources import semantic_scholar

# Logging must go to stderr (stdout is used by MCP stdio transport)
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    datefmt="%H:%M:%S",
    stream=sys.stderr,
)
logger = logging.getLogger(__name__)

mcp = FastMCP("vpnsci")

# Lazy-initialized shared fetcher instance
_fetcher: PaperFetcher | None = None


def _get_fetcher() -> PaperFetcher | None:
    """Get or create the fetcher singleton. Returns None if school not configured."""
    global _fetcher
    config = Config.load()
    if not config.school:
        return None
    if _fetcher is None:
        _fetcher = PaperFetcher(config)
    return _fetcher


def _reset_fetcher():
    """Reset the fetcher singleton (called after reconfiguring school)."""
    global _fetcher
    if _fetcher is not None:
        _fetcher.close()
        _fetcher = None


_SCHOOL_NOT_CONFIGURED = (
    "⚠️ 尚未配置学校。请先告诉我你的学校名称（如「兰州大学」），"
    "我会帮你配置好再进行操作。\n\n"
    "你也可以手动运行: vpnsci config-cmd --school 你的学校名称"
)


@mcp.tool()
async def configure_school(school_name: str) -> str:
    """Configure which university to use for WebVPN paper access.

    Call this when the user tells you their school name.
    Supports fuzzy matching (e.g. "兰大" will match "兰州大学").

    Args:
        school_name: The university name (e.g. "兰州大学", "清华大学").
    """
    from .schools import get_school

    try:
        entry = get_school(school_name)
    except ValueError:
        return (
            f"未找到学校「{school_name}」。"
            f"请确认学校名称，或使用 vpnsci schools 搜索支持的学校列表。"
        )

    config = Config.load()
    config.school = entry.name
    config.webvpn_base_url = entry.host
    config.save()

    # Reset fetcher so it picks up the new config
    _reset_fetcher()

    return (
        f"✅ 已配置为 **{entry.name}**（{entry.province}）\n"
        f"WebVPN 地址: {entry.host}\n\n"
        f"现在可以开始搜索和获取论文了。首次使用 WebVPN 获取付费论文时，"
        f"会弹出浏览器让你完成校园网登录认证。"
    )


@mcp.tool()
async def fetch_paper(identifier: str, format: str = "markdown") -> str:
    """Fetch an academic paper's full text by DOI or URL.

    Uses Open Access sources (Unpaywall, arXiv) first, then falls back
    to WebVPN/EZproxy for paywalled content. Results are cached locally.

    Args:
        identifier: DOI (e.g. "10.1038/nphys1509") or article URL.
        format: Output format - "markdown" (default), "json", or "text".
    """
    fetcher = _get_fetcher()
    if fetcher is None:
        return _SCHOOL_NOT_CONFIGURED

    paper = await asyncio.to_thread(fetcher.fetch, identifier)

    if not paper.full_text and not paper.abstract:
        return f"Could not extract full text for: {identifier}\nTitle: {paper.title}\nURL: {paper.url}"

    if format == "json":
        return paper.to_json()
    elif format == "text":
        return paper.to_text()
    else:
        return paper.to_markdown(include_pdf_path=True)


@mcp.tool()
async def search_papers(query: str, limit: int = 10, year_range: str = "") -> str:
    """Search for academic papers via Semantic Scholar.

    Returns a list of papers with titles, authors, DOIs, and citation counts.
    Use the DOIs from results with fetch_paper to get full text.

    Args:
        query: Search query (e.g. "organic photovoltaics silver nanowire").
        limit: Maximum number of results (1-100, default 10).
        year_range: Optional year filter (e.g. "2020-2024" or "2020-").
    """
    results = await asyncio.to_thread(
        semantic_scholar.search, query, limit=limit, year_range=year_range or None
    )

    if not results:
        return "No results found."

    lines = [f"Found {len(results)} results:\n"]
    for i, r in enumerate(results, 1):
        authors_str = ", ".join(r.authors[:3])
        if len(r.authors) > 3:
            authors_str += " et al."

        lines.append(f"### {i}. {r.title}")
        lines.append(f"- **Authors:** {authors_str}")
        if r.year:
            lines.append(f"- **Year:** {r.year}")
        if r.journal:
            lines.append(f"- **Journal:** {r.journal}")
        if r.doi:
            lines.append(f"- **DOI:** {r.doi}")
        elif r.arxiv_id:
            lines.append(f"- **arXiv:** {r.arxiv_id}")
        lines.append(f"- **Citations:** {r.citation_count}")
        if r.abstract:
            lines.append(f"- **Abstract:** {r.abstract[:200]}...")
        lines.append("")

    return "\n".join(lines)


@mcp.tool()
async def get_paper_metadata(doi: str) -> str:
    """Get metadata for a paper by DOI from Semantic Scholar.

    Returns title, authors, year, abstract, citation count, and identifiers.
    Lighter than fetch_paper - does not download full text.

    Args:
        doi: The DOI of the paper (e.g. "10.1038/nphys1509").
    """
    result = await asyncio.to_thread(semantic_scholar.get_paper, f"DOI:{doi}")
    if result is None:
        return f"Paper not found for DOI: {doi}"

    lines = [f"# {result.title}"]
    if result.authors:
        lines.append(f"**Authors:** {', '.join(result.authors)}")
    if result.year:
        lines.append(f"**Year:** {result.year}")
    if result.journal:
        lines.append(f"**Journal:** {result.journal}")
    lines.append(f"**DOI:** {result.doi}")
    if result.arxiv_id:
        lines.append(f"**arXiv:** {result.arxiv_id}")
    lines.append(f"**Citations:** {result.citation_count}")
    if result.abstract:
        lines.append(f"\n## Abstract\n\n{result.abstract}")

    return "\n".join(lines)


def main():
    mcp.run(transport="stdio")


if __name__ == "__main__":
    main()
