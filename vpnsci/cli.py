"""CLI interface for vpnsci."""

import logging
import os
import sys
from pathlib import Path

# Fix Windows console encoding for Unicode output
if sys.platform == "win32":
    os.environ.setdefault("PYTHONIOENCODING", "utf-8")
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

import typer
from rich.console import Console
from rich.table import Table

from .config import Config
from .fetcher import PaperFetcher
from .schools import get_school, list_schools, search_schools
from .sources import semantic_scholar

app = typer.Typer(
    name="vpnsci",
    help="Fetch academic papers via WebVPN, Open Access, or arXiv.",
    no_args_is_help=True,
)
console = Console()


def _setup_logging(verbose: bool = False):
    level = logging.DEBUG if verbose else logging.INFO
    logging.basicConfig(
        level=level,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        datefmt="%H:%M:%S",
    )


def _ensure_email(config: Config):
    """Prompt user to set email if not configured (needed for Unpaywall)."""
    if not config.email:
        console.print("[yellow]Email not configured (needed for Unpaywall OA detection).[/yellow]")
        email = typer.prompt("Enter your email address")
        config.email = email
        config.save()
        console.print(f"[green]Email saved: {email}[/green]")


@app.command()
def login(
    force: bool = typer.Option(False, "--force", "-f", help="Force re-login even if session is valid."),
    verbose: bool = typer.Option(False, "--verbose", "-v", help="Verbose logging."),
):
    """Initialize or refresh WebVPN session."""
    _setup_logging(verbose)
    config = Config.load()
    fetcher = PaperFetcher(config)

    console.print("[bold]Checking WebVPN session...[/bold]")
    if fetcher.auth.login(force=force):
        console.print("[green]WebVPN session is active.[/green]")
    else:
        console.print("[red]Failed to authenticate with WebVPN.[/red]")
        raise typer.Exit(1)


@app.command()
def fetch(
    identifier: str = typer.Argument(help="DOI or URL of the paper to fetch."),
    output: str = typer.Option("", "--output", "-o", help="Output directory for PDFs."),
    format: str = typer.Option("json", "--format", "-f", help="Output format: json, markdown, text."),
    text_only: bool = typer.Option(False, "--text-only", "-t", help="Output only plain text (minimal tokens)."),
    no_cache: bool = typer.Option(False, "--no-cache", help="Bypass cache."),
    verbose: bool = typer.Option(False, "--verbose", "-v", help="Verbose logging."),
):
    """Fetch a single paper by DOI or URL."""
    _setup_logging(verbose)
    config = Config.load()
    _ensure_email(config)
    if output:
        config.output_dir = output

    fetcher = PaperFetcher(config)
    try:
        console.print(f"[bold]Fetching:[/bold] {identifier}")
        paper = fetcher.fetch(identifier, use_cache=not no_cache)

        if not paper.full_text and not paper.abstract:
            console.print("[yellow]Warning: Could not extract full text.[/yellow]")

        if text_only:
            console.print(paper.to_text())
        elif format == "markdown":
            console.print(paper.to_markdown())
        elif format == "text":
            console.print(paper.to_text())
        else:
            console.print(paper.to_json())

        if paper.pdf_path:
            console.print(f"\n[dim]PDF saved to: {paper.pdf_path}[/dim]")
        console.print(f"[dim]Source: {paper.source}[/dim]")

    finally:
        fetcher.close()


@app.command()
def batch(
    file: Path = typer.Argument(help="File containing DOIs (one per line)."),
    output: str = typer.Option("", "--output", "-o", help="Output directory."),
    format: str = typer.Option("json", "--format", "-f", help="Output format: json, markdown, text."),
    verbose: bool = typer.Option(False, "--verbose", "-v", help="Verbose logging."),
):
    """Fetch multiple papers from a file of DOIs."""
    _setup_logging(verbose)

    if not file.exists():
        console.print(f"[red]File not found: {file}[/red]")
        raise typer.Exit(1)

    dois = [
        line.strip()
        for line in file.read_text(encoding="utf-8").splitlines()
        if line.strip() and not line.strip().startswith("#")
    ]

    if not dois:
        console.print("[yellow]No DOIs found in file.[/yellow]")
        raise typer.Exit(0)

    console.print(f"[bold]Found {len(dois)} DOIs to fetch.[/bold]")

    config = Config.load()
    if output:
        config.output_dir = output

    fetcher = PaperFetcher(config)
    results_dir = Path(config.output_dir)
    results_dir.mkdir(parents=True, exist_ok=True)

    succeeded = 0
    failed = 0

    try:
        for i, doi in enumerate(dois, 1):
            console.print(f"\n[bold][{i}/{len(dois)}][/bold] Fetching: {doi}")
            try:
                paper = fetcher.fetch(doi)
                if paper.full_text:
                    succeeded += 1
                    # Save result
                    safe_name = doi.replace("/", "_").replace(":", "_")
                    if format == "markdown":
                        out_file = results_dir / f"{safe_name}.md"
                        out_file.write_text(paper.to_markdown(), encoding="utf-8")
                    elif format == "text":
                        out_file = results_dir / f"{safe_name}.txt"
                        out_file.write_text(paper.to_text(), encoding="utf-8")
                    else:
                        out_file = results_dir / f"{safe_name}.json"
                        out_file.write_text(paper.to_json(), encoding="utf-8")
                    console.print(f"  [green]OK[/green] → {out_file.name}")
                else:
                    failed += 1
                    console.print("  [yellow]No full text extracted[/yellow]")
            except Exception as e:
                failed += 1
                console.print(f"  [red]Error: {e}[/red]")

        console.print(f"\n[bold]Done:[/bold] {succeeded} succeeded, {failed} failed out of {len(dois)}.")

    finally:
        fetcher.close()


@app.command()
def search(
    query: str = typer.Argument(help="Search query."),
    limit: int = typer.Option(10, "--limit", "-n", help="Maximum results."),
    year: str = typer.Option("", "--year", "-y", help="Year range, e.g., '2020-2024' or '2020-'."),
    do_fetch: bool = typer.Option(False, "--fetch", help="Also fetch full text for results with DOIs."),
    verbose: bool = typer.Option(False, "--verbose", "-v", help="Verbose logging."),
):
    """Search for papers via Semantic Scholar."""
    _setup_logging(verbose)

    console.print(f"[bold]Searching:[/bold] {query}")
    results = semantic_scholar.search(query, limit=limit, year_range=year or None)

    if not results:
        console.print("[yellow]No results found.[/yellow]")
        raise typer.Exit(0)

    # Display results in a table
    table = Table(title=f"Search Results ({len(results)})")
    table.add_column("#", style="dim", width=3)
    table.add_column("Year", width=5)
    table.add_column("Title", max_width=60)
    table.add_column("Authors", max_width=30)
    table.add_column("DOI", max_width=25)
    table.add_column("Cites", width=5, justify="right")

    for i, r in enumerate(results, 1):
        authors_str = ", ".join(r.authors[:3])
        if len(r.authors) > 3:
            authors_str += " et al."
        table.add_row(
            str(i),
            str(r.year or ""),
            r.title[:60],
            authors_str[:30],
            r.doi[:25] if r.doi else r.arxiv_id[:25] if r.arxiv_id else "",
            str(r.citation_count),
        )

    console.print(table)

    # Optionally fetch full texts
    if do_fetch:
        fetchable = [r for r in results if r.doi or r.arxiv_id]
        if fetchable:
            console.print(f"\n[bold]Fetching {len(fetchable)} papers...[/bold]")
            config = Config.load()
            fetcher = PaperFetcher(config)
            try:
                for r in fetchable:
                    identifier = r.doi or f"arxiv:{r.arxiv_id}"
                    console.print(f"  Fetching: {identifier}")
                    try:
                        paper = fetcher.fetch(identifier)
                        status = "[green]OK[/green]" if paper.full_text else "[yellow]No text[/yellow]"
                        console.print(f"    {status}")
                    except Exception as e:
                        console.print(f"    [red]Error: {e}[/red]")
            finally:
                fetcher.close()


@app.command()
def cache(
    action: str = typer.Argument(help="Action: 'clear' to remove cached results."),
):
    """Manage the paper cache."""
    if action == "clear":
        config = Config.load()
        fetcher = PaperFetcher(config)
        fetcher.clear_cache()
        console.print("[green]Cache cleared.[/green]")
    else:
        console.print(f"[red]Unknown action: {action}. Use 'clear'.[/red]")
        raise typer.Exit(1)


@app.command()
def schools(
    query: str = typer.Argument("", help="Search query (name, province, or host). Omit to list all."),
):
    """List or search supported universities."""
    if query:
        results = search_schools(query)
    else:
        results = list_schools()

    if not results:
        console.print(f"[yellow]No schools found matching '{query}'.[/yellow]")
        raise typer.Exit(0)

    table = Table(title=f"Supported Schools ({len(results)})")
    table.add_column("#", style="dim", width=4)
    table.add_column("Province", width=10)
    table.add_column("School", max_width=25)
    table.add_column("WebVPN Host", max_width=40)
    table.add_column("Custom Key", width=5, justify="center")

    from .schools import WEBVPN_DEFAULT_KEY
    for i, s in enumerate(results, 1):
        has_custom = "Y" if s.key != WEBVPN_DEFAULT_KEY else ""
        table.add_row(str(i), s.province, s.name, s.host, has_custom)

    console.print(table)


@app.command()
def config_cmd(
    show: bool = typer.Option(True, "--show", help="Show current config."),
    set_email: str = typer.Option("", "--email", help="Set email for Unpaywall API."),
    set_output: str = typer.Option("", "--output-dir", help="Set default output directory."),
    set_webvpn_url: str = typer.Option("", "--webvpn-url", help="Set WebVPN base URL."),
    set_school: str = typer.Option("", "--school", help="Set school (use 'vpnsci schools' to list)."),
):
    """View or update configuration."""
    cfg = Config.load()

    if set_email:
        cfg.email = set_email
        cfg.save()
        console.print(f"[green]Email set to: {set_email}[/green]")

    if set_output:
        cfg.output_dir = set_output
        cfg.save()
        console.print(f"[green]Output dir set to: {set_output}[/green]")

    if set_webvpn_url:
        cfg.webvpn_base_url = set_webvpn_url.rstrip("/")
        cfg.save()
        console.print(f"[green]WebVPN base URL set to: {set_webvpn_url}[/green]")

    if set_school:
        try:
            entry = get_school(set_school)
            cfg.school = entry.name
            cfg.webvpn_base_url = entry.host
            cfg.save()
            console.print(f"[green]School set to: {entry.name} ({entry.host})[/green]")
        except ValueError as e:
            console.print(f"[red]{e}[/red]")
            raise typer.Exit(1)

    if show and not set_email and not set_output and not set_webvpn_url and not set_school:
        console.print("[bold]Current configuration:[/bold]")
        console.print(f"  School:         {cfg.school}")
        console.print(f"  WebVPN base:    {cfg.webvpn_base_url}")
        console.print(f"  Email:          {cfg.email}")
        console.print(f"  Output dir:     {cfg.output_dir}")
        console.print(f"  Cache dir:      {cfg.cache_dir}")
        console.print(f"  Cookie path:    {cfg.cookie_path}")


if __name__ == "__main__":
    app()
