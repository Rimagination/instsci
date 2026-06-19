"""MCP server exposing InstSci tools for AI agents supporting MCP protocol."""

import asyncio
import json
import logging
import sys
from pathlib import Path
from typing import Any

from mcp.server.fastmcp import FastMCP

from .config import Config
from .fetcher import PaperFetcher
from .publisher_access import (
    load_institutional_identity_policy,
    load_publisher_access_catalog,
    load_publisher_browser_verification_matrix,
)
from .publisher_profiles import get_publisher_profile, list_publisher_profiles
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


def _json_response(payload: Any) -> str:
    return json.dumps(payload, ensure_ascii=False, indent=2)


def _publisher_key(publisher: str) -> str:
    profile = get_publisher_profile(publisher)
    for key in list_publisher_profiles():
        if get_publisher_profile(key) is profile:
            return key
    raise ValueError(f"Unknown publisher profile: {publisher}")


def _unknown_publisher_payload(publisher: str) -> dict[str, Any]:
    return {
        "status": "unknown_publisher",
        "publisher": publisher,
        "known_publishers": list_publisher_profiles(),
    }


def _institution_from_config(config: Config) -> tuple[str, str]:
    if config.carsi_idp_name.strip():
        return config.carsi_idp_name.strip(), "config.carsi_idp_name"
    if config.school.strip():
        return config.school.strip(), "config.school"
    return "", ""


def _quote_cli_arg(value: str) -> str:
    value = str(value)
    if not value:
        return "''"
    if not any(char.isspace() or char in "'\"" for char in value):
        return value
    return "'" + value.replace("'", "''") + "'"


def _command_from_argv(argv: list[str]) -> str:
    return " ".join(_quote_cli_arg(arg) for arg in argv)


def _format_policy_markdown(policy: dict[str, Any]) -> str:
    institution = policy.get("subscription_institution", {})
    resolution = institution.get("resolution_order", [])
    lines = [
        "# InstSci Institutional Identity Policy",
        "",
        f"- Default mode: {policy.get('default_mode', '')}",
        f"- Default identity: {policy.get('default_identity', '')}",
        f"- Preferred off-campus access: {policy.get('preferred_off_campus_access', '')}",
        f"- Final publisher PDF verdict requires: {policy.get('final_pdf_verdict_requires', '')}",
        f"- Institution resolution order: {', '.join(resolution)}",
        "",
        "Use this as MCP context only. Closed-access publisher PDF verdicts still require the visible CloakBrowser workflow.",
    ]
    return "\n".join(lines)


def _format_catalog_markdown(payload: dict[str, Any]) -> str:
    if payload.get("status") == "unknown_publisher":
        return (
            f"# Unknown Publisher\n\nPublisher: {payload.get('publisher', '')}\n\n"
            f"Known publishers: {', '.join(payload.get('known_publishers', []))}"
        )
    publishers = payload.get("publishers") or {payload["publisher"]["profile_key"]: payload["publisher"]}
    lines = [
        "# InstSci Publisher Access Catalog",
        "",
        "This catalog is route knowledge and HTTP preflight context, not final PDF evidence.",
        "",
    ]
    for key, entry in publishers.items():
        identity = entry.get("identity", {})
        lines.extend(
            [
                f"## {entry.get('display_name', key)}",
                f"- Profile key: {key}",
                f"- PDF route strategy: {', '.join(entry.get('pdf_route_strategy', []))}",
                f"- Closed access requires: {', '.join(identity.get('closed_access_requires', []))}",
                f"- Login hints: {', '.join(identity.get('login_entry_hints', []))}",
                "",
            ]
        )
    return "\n".join(lines).rstrip()


def _format_matrix_markdown(payload: dict[str, Any]) -> str:
    if payload.get("status") == "unknown_publisher":
        return (
            f"# Unknown Publisher\n\nPublisher: {payload.get('publisher', '')}\n\n"
            f"Known publishers: {', '.join(payload.get('known_publishers', []))}"
        )
    publishers = payload.get("publishers") or {payload["publisher"]["profile_key"]: payload["publisher"]}
    lines = [
        "# InstSci Browser Verification Matrix",
        "",
        f"Source: {payload.get('verdict_source', '')}",
        f"Scope: {payload.get('scope', '')}",
        "",
    ]
    for key, entry in publishers.items():
        lines.extend(
            [
                f"## {entry.get('display_name', key)}",
                f"- Profile key: {key}",
                f"- Browser verified: {entry.get('browser_verified')}",
                f"- Status: {entry.get('status', '')}",
                f"- State: {entry.get('state', '')}",
                "",
            ]
        )
    return "\n".join(lines).rstrip()


def _format_plan_markdown(payload: dict[str, Any]) -> str:
    lines = [
        "# InstSci Publisher PDF Workflow Plan",
        "",
        f"- Status: {payload.get('status', '')}",
        f"- Workflow: {payload.get('workflow', '')}",
        f"- Evidence standard: {payload.get('evidence_standard', '')}",
        f"- Command: `{payload.get('command', payload.get('command_template', ''))}`",
        "",
        payload.get("next_action", ""),
    ]
    return "\n".join(line for line in lines if line != "")


@mcp.tool()
async def get_institutional_identity_policy(format: str = "json") -> str:
    """Return InstSci's institutional access route policy for MCP clients.

    Use this before planning closed-access publisher PDF work. The policy tells
    clients how to choose an institution route and when visible CloakBrowser
    evidence is required.

    Args:
        format: Output format - "json" (default) or "markdown".
    """
    policy = load_institutional_identity_policy()
    if format.lower() == "markdown":
        return _format_policy_markdown(policy)
    return _json_response(policy)


@mcp.tool()
async def get_publisher_access_catalog(publisher: str = "", format: str = "json") -> str:
    """Return route knowledge for official publisher PDF workflows.

    This is reusable context for planning and HTTP preflight diagnostics. It is
    not final evidence that a closed-access PDF can be retrieved.

    Args:
        publisher: Optional publisher key, display name, or alias.
        format: Output format - "json" (default) or "markdown".
    """
    catalog = load_publisher_access_catalog()
    if publisher.strip():
        try:
            key = _publisher_key(publisher)
        except ValueError:
            payload = _unknown_publisher_payload(publisher)
        else:
            payload = {
                "version": catalog.get("version"),
                "scope": "route knowledge and HTTP preflight context only",
                "publisher": catalog["publishers"][key],
            }
    else:
        payload = catalog
    if format.lower() == "markdown":
        return _format_catalog_markdown(payload)
    return _json_response(payload)


@mcp.tool()
async def get_publisher_browser_verification_matrix(
    publisher: str = "",
    format: str = "json",
) -> str:
    """Return browser-backed publisher PDF verification results.

    The matrix records prior visible CloakBrowser evidence. New final verdicts
    still require a fresh visible CloakBrowser workflow for the current DOI/run.

    Args:
        publisher: Optional publisher key, display name, or alias.
        format: Output format - "json" (default) or "markdown".
    """
    matrix = load_publisher_browser_verification_matrix()
    if publisher.strip():
        try:
            key = _publisher_key(publisher)
        except ValueError:
            payload = _unknown_publisher_payload(publisher)
        else:
            payload = {
                "version": matrix.get("version"),
                "last_browser_verification": matrix.get("last_browser_verification"),
                "verdict_source": matrix.get("verdict_source"),
                "scope": matrix.get("scope"),
                "publisher": matrix["publishers"][key],
            }
    else:
        payload = matrix
    if format.lower() == "markdown":
        return _format_matrix_markdown(payload)
    return _json_response(payload)


@mcp.tool()
async def plan_publisher_pdf_workflow(
    doi_file: str,
    publisher: str = "auto",
    institution: str = "",
    output: str = "runs/papers",
    speed: str = "balanced",
    concurrency: int = 1,
    format: str = "json",
) -> str:
    """Plan the visible CloakBrowser workflow for closed-access publisher PDFs.

    This tool does not download PDFs. It returns the command and guardrails a
    client should use to start InstSci's visible browser-backed workflow.

    Args:
        doi_file: Path to a DOI text file, one DOI per line.
        publisher: "auto" for instsci papers, or a publisher profile key/alias.
        institution: Subscription institution search text. If empty, config is checked.
        output: Run output directory.
        speed: Browser speed preset - "careful", "balanced", or "fast".
        concurrency: Requested browser workers. Only speed="fast" uses more than one worker.
        format: Output format - "json" (default) or "markdown".
    """
    if not doi_file.strip():
        payload = {
            "status": "doi_file_required",
            "next_action": "Provide a DOI text file path, one DOI per line.",
            "known_publishers": list_publisher_profiles(),
        }
        return _format_plan_markdown(payload) if format.lower() == "markdown" else _json_response(payload)

    policy = load_institutional_identity_policy()
    config = Config.load()
    if institution.strip():
        resolved_institution = institution.strip()
        institution_source = "explicit"
    else:
        resolved_institution, institution_source = _institution_from_config(config)

    publisher_value = publisher.strip() or "auto"
    speed_value = (speed or "balanced").strip().lower()
    if speed_value not in {"careful", "balanced", "fast"}:
        payload = {
            "status": "unsupported_speed",
            "speed": speed,
            "known_speeds": ["careful", "balanced", "fast"],
            "next_action": "Choose careful, balanced, or fast. Balanced keeps one live CloakBrowser context by default.",
        }
        return _format_plan_markdown(payload) if format.lower() == "markdown" else _json_response(payload)
    try:
        requested_concurrency = int(concurrency or 1)
    except (TypeError, ValueError):
        requested_concurrency = 1
    requested_concurrency = max(1, requested_concurrency)
    effective_concurrency = min(requested_concurrency, 2) if speed_value == "fast" else 1

    if publisher_value.lower() == "auto":
        workflow_command = "instsci papers"
        publisher_arg = "auto"
        argv = [
            "instsci",
            "papers",
            doi_file,
            "--publisher",
            "auto",
            "--institution",
            resolved_institution or "Institution Name",
            "--output",
            output,
            "--speed",
            speed_value,
        ]
    else:
        try:
            publisher_arg = _publisher_key(publisher_value)
        except ValueError:
            payload = _unknown_publisher_payload(publisher_value)
            payload.update(
                {
                    "workflow": "visible_cloakbrowser",
                    "evidence_standard": "browser verified",
                    "next_action": "Choose one of the known publisher profile keys or use publisher=auto.",
                }
            )
            return _format_plan_markdown(payload) if format.lower() == "markdown" else _json_response(payload)
        workflow_command = "instsci publisher-batch"
        argv = [
            "instsci",
            "publisher-batch",
            doi_file,
            "--publisher",
            publisher_arg,
            "--institution",
            resolved_institution or "Institution Name",
            "--output",
            output,
            "--speed",
            speed_value,
        ]
    if speed_value == "fast" and effective_concurrency != 1:
        argv.extend(["--concurrency", str(effective_concurrency)])

    command = _command_from_argv(argv)
    status = "ready" if resolved_institution else "institution_required"
    next_action = (
        "Run the command in a visible desktop session and keep CloakBrowser open for SSO, 2FA, CAPTCHA, and PDF verification."
        if resolved_institution
        else "Ask the user for their own subscription institution, then rerun with --institution. Do not use a hard-coded default."
    )
    payload = {
        "status": status,
        "workflow": "visible_cloakbrowser",
        "workflow_command": workflow_command,
        "requires_visible_cloakbrowser": True,
        "evidence_standard": "browser verified",
        "http_preflight_scope": "MCP planning, DOI resolution, requests, curl, and publisher-doctor are HTTP preflight only.",
        "final_pdf_verdict_requires": policy.get("final_pdf_verdict_requires"),
        "institution": {
            "value": resolved_institution,
            "source": institution_source,
            "required_for_closed_access": policy.get("subscription_institution", {}).get(
                "required_for_closed_access"
            ),
        },
        "publisher": publisher_arg,
        "speed": speed_value,
        "concurrency": effective_concurrency,
        "doi_file": doi_file,
        "output": output,
        "next_action": next_action,
        "visual_checkpoints": [
            "PDF controls",
            "Institutional Access or OpenAthens/Shibboleth/CARSI controls",
            "cookie or verification prompts",
            "PDF viewer download controls",
        ],
    }
    if resolved_institution:
        payload["argv"] = argv
        payload["command"] = command
    else:
        payload["argv_template"] = argv
        payload["command_template"] = command
    if format.lower() == "markdown":
        return _format_plan_markdown(payload)
    return _json_response(payload)


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
