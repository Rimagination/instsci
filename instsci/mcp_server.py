"""MCP server exposing InstSci tools for AI agents supporting MCP protocol."""

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

mcp = FastMCP("instsci")

# Lazy-initialized shared fetcher instance
_fetcher: PaperFetcher | None = None


def _get_fetcher() -> PaperFetcher:
    """Get or create the fetcher singleton."""
    global _fetcher
    config = Config.load()
    if _fetcher is None:
        _fetcher = PaperFetcher(config)
    return _fetcher


def _reset_fetcher():
    """Reset the fetcher singleton (called after reconfiguring school)."""
    global _fetcher
    if _fetcher is not None:
        _fetcher.close()
        _fetcher = None


@mcp.tool()
async def configure_school(school_name: str) -> str:
    """Configure which university to use for institutional paper access.

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
            f"请确认学校名称，或使用 instsci schools 搜索支持的学校列表。"
        )

    config = Config.load()
    config.school = entry.name
    if entry.school_type == "ezproxy":
        config.ezproxy_base_url = entry.host
        config.webvpn_base_url = ""
    else:
        config.webvpn_base_url = entry.host
        config.ezproxy_base_url = ""
    config.save()

    # Reset fetcher so it picks up the new config
    _reset_fetcher()

    # Provide school-type-specific guidance
    type_guidance = ""
    if entry.school_type == "easyconnect":
        type_guidance = (
            "\n\n⚠️ **该校需要本地校园连接器**，首次使用前请先完成学校客户端登录：\n"
            "1. 启动学校要求的 EasyConnect 客户端或兼容容器\n"
            "2. 完成登录，并确认本地 SOCKS5 入口可用\n"
            "3. 设置连接器地址：`instsci config-cmd --connector-url socks5://127.0.0.1:1080`\n\n"
            "如果你已经有可用的 zju-connect 等轻量方案，也可以直接设置本地连接器地址。"
        )
    elif entry.school_type == "atrust":
        type_guidance = (
            "\n\n⚠️ **该校需要 aTrust 校园连接器**，首次使用前请先完成学校客户端登录：\n"
            "1. 启动学校要求的 aTrust 客户端或兼容容器\n"
            "2. 完成登录，并确认本地 SOCKS5 入口可用\n"
            "3. 设置连接器地址：`instsci config-cmd --connector-url socks5://127.0.0.1:1080`\n\n"
            "如果需要容器方案，请按学校入口地址配置 docker-easyconnect。"
        )
    elif entry.school_type == "ezproxy":
        type_guidance = (
            "\n\n📚 **该校使用图书馆入口**。首次获取论文时会弹出浏览器，"
            "完成学校图书馆登录即可。"
        )

    type_label = {
        "webvpn": "CampusPortal",
        "easyconnect": "CampusConnector",
        "atrust": "CampusConnector",
        "ezproxy": "LibraryPortal",
    }.get(entry.school_type, entry.school_type)

    return (
        f"✅ 已配置为 **{entry.name}**（{entry.province}）\n"
        f"入口地址: {entry.host}\n"
        f"类型: {type_label}{type_guidance}\n\n"
        f"现在可以开始搜索和获取论文了。"
    )


@mcp.tool()
async def fetch_paper(identifier: str, format: str = "markdown") -> str:
    """Fetch an academic paper's full text by DOI or URL.

    Uses Open Access sources (Unpaywall, arXiv) first, then falls back
    to institutional access gateways for paywalled content. Results are cached locally.

    Args:
        identifier: DOI (e.g. "10.1038/nphys1509") or article URL.
        format: Output format - "markdown" (default), "json", or "text".
    """
    fetcher = _get_fetcher()

    result = await asyncio.to_thread(fetcher.fetch_with_result, identifier)

    if format == "json":
        return result.to_json()
    elif format == "text":
        return result.to_text()
    else:
        return result.to_markdown(include_pdf_path=True)


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
