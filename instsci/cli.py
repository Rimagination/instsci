"""CLI interface for InstSci."""

import json
import logging
import os
import sys
import time
from datetime import datetime
from pathlib import Path
from urllib.parse import urlparse

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
    name="instsci",
    help="Fetch academic papers via institutional access, Open Access, or arXiv.",
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


def _school_type_label(school_type: str) -> str:
    return {
        "webvpn": "CampusPortal",
        "easyconnect": "CampusConnector",
        "atrust": "CampusConnector",
        "ezproxy": "LibraryPortal",
    }.get(school_type, school_type)


def _apply_school_config(cfg: Config, school: str):
    entry = get_school(school)
    cfg.school = entry.name
    if entry.school_type == "ezproxy":
        cfg.ezproxy_base_url = entry.host
        cfg.webvpn_base_url = ""
    else:
        cfg.webvpn_base_url = entry.host
        cfg.ezproxy_base_url = ""
    return entry


def _access_url(cfg: Config) -> str:
    return cfg.ezproxy_base_url or cfg.webvpn_base_url


def _mask_secret(value: str) -> str:
    if not value:
        return "(not set)"
    if len(value) <= 8:
        return "****"
    return f"{value[:4]}...{value[-4:]}"


def _configured_subscription_institution(cfg: Config) -> str:
    """Return the configured subscription institution search text, if any."""
    return (cfg.carsi_idp_name or cfg.school or "").strip()


def _resolve_subscription_institution(
    cfg: Config,
    institution: str,
    *,
    prompt: bool = True,
) -> str:
    """Resolve institution text without hard-coding any school as the default."""
    explicit = institution.strip()
    if explicit:
        return explicit

    configured = _configured_subscription_institution(cfg)
    if configured:
        return configured

    if not prompt:
        console.print(
            "[red]Subscription institution is required.[/red] "
            "Pass --institution or run: instsci setup --school \"Your Institution\""
        )
        raise typer.Exit(1)

    console.print(
        "[yellow]Subscription institution is required for closed-access publisher PDFs.[/yellow]"
    )
    console.print(
        "[dim]Use the institution that owns your subscription, e.g. the name shown in "
        "OpenAthens/Shibboleth/CARSI login pages.[/dim]"
    )
    value = typer.prompt("Subscription institution").strip()
    if not value:
        console.print("[red]Subscription institution cannot be empty.[/red]")
        raise typer.Exit(1)

    cfg.carsi_enabled = True
    cfg.carsi_idp_name = value
    cfg.save()
    return value


def _path_status(path_value: str) -> tuple[str, str]:
    if not path_value:
        return "missing", ""
    path = Path(path_value)
    return ("ok" if path.exists() else "missing", str(path))


def _show_setup_check(cfg: Config) -> bool:
    checks: list[tuple[str, str, str]] = []
    checks.append(("School", "ok" if cfg.school else "missing", cfg.school or "set with --school"))
    checks.append(("Access URL", "ok" if _access_url(cfg) else "missing", _access_url(cfg) or "derived from --school"))
    federated_ready = (not cfg.carsi_enabled) or bool(cfg.carsi_idp_name)
    checks.append((
        "Federated login",
        "ok" if federated_ready else "missing",
        cfg.carsi_idp_name or ("disabled" if not cfg.carsi_enabled else "set with --federated-school"),
    ))
    for label, path_value in [
        ("Output dir", cfg.output_dir),
        ("Cache dir", cfg.cache_dir),
        ("Chrome profile", cfg.chrome_profile_dir),
        ("Session dir", cfg.carsi_cookie_dir),
    ]:
        status, detail = _path_status(path_value)
        checks.append((label, status, detail))

    table = Table(title="InstSci Environment Check")
    table.add_column("Item", width=18)
    table.add_column("Status", width=10)
    table.add_column("Detail", overflow="fold")
    ready = True
    for label, status, detail in checks:
        if status != "ok":
            ready = False
        style = "green" if status == "ok" else "yellow"
        table.add_row(label, f"[{style}]{status}[/{style}]", detail)
    console.print(table)
    return ready


@app.command()
def setup(
    school: str = typer.Option("", "--school", help="Set institution by school name or partial match."),
    email: str = typer.Option("", "--email", help="Set email for Open Access metadata services."),
    output_dir: str = typer.Option("", "--output-dir", help="Set the default PDF output directory."),
    federated: bool = typer.Option(True, "--federated/--no-federated", help="Enable browser federated institutional login."),
    federated_school: str = typer.Option("", "--federated-school", help="Override the school name shown in publisher login pages."),
    check: bool = typer.Option(False, "--check", help="Check environment without changing configuration."),
):
    """One-step environment setup for institutional paper downloads."""
    cfg = Config.load()
    changed = False
    school_entry = None

    has_setter = any([school, email, output_dir, federated_school]) or not federated
    if check and not has_setter:
        if not _show_setup_check(cfg):
            raise typer.Exit(2)
        return

    if school:
        try:
            school_entry = _apply_school_config(cfg, school)
        except ValueError as exc:
            console.print(f"[red]{exc}[/red]")
            raise typer.Exit(1) from exc
        changed = True

    if email:
        cfg.email = email
        changed = True

    if output_dir:
        cfg.output_dir = output_dir
        changed = True

    if federated and (school or federated_school or cfg.school):
        cfg.carsi_enabled = True
        if federated_school:
            cfg.carsi_idp_name = federated_school
        elif school_entry is not None:
            cfg.carsi_idp_name = school_entry.name
        elif cfg.school and not cfg.carsi_idp_name:
            cfg.carsi_idp_name = cfg.school
        changed = True
    elif not federated:
        cfg.carsi_enabled = False
        changed = True

    cfg.ensure_dirs()
    if changed:
        cfg.save()

    ready = bool(cfg.school and _access_url(cfg) and ((not cfg.carsi_enabled) or cfg.carsi_idp_name))
    if ready:
        console.print("[green]Environment ready.[/green]")
    else:
        console.print("[yellow]Environment prepared, but institution access is incomplete.[/yellow]")
    if school_entry is not None:
        type_label = _school_type_label(school_entry.school_type)
        console.print(f"  School:       {school_entry.name} ({type_label})")
        console.print(f"  Access URL:   {_access_url(cfg)}")
        if school_entry.school_type in {"easyconnect", "atrust"}:
            console.print("[yellow]This school needs a local campus connector before downloading.[/yellow]")
            console.print("  Set it with: [cyan]instsci config-cmd --connector-url socks5://127.0.0.1:1080[/cyan]")
    console.print(f"  Output dir:   {cfg.output_dir}")
    console.print(f"  Browser dir:  {cfg.chrome_profile_dir}")
    console.print(f"  Sessions dir: {cfg.carsi_cookie_dir}")
    console.print("[dim]Next: instsci papers dois.txt --publisher auto[/dim]")
    console.print("[dim]If SSO, 2FA, or CAPTCHA appears, complete it once in the opened browser window.[/dim]")

    if (check or not ready) and not _show_setup_check(cfg):
        raise typer.Exit(2)


@app.command()
def login(
    force: bool = typer.Option(False, "--force", "-f", help="Force re-login even if session is valid."),
    verbose: bool = typer.Option(False, "--verbose", "-v", help="Verbose logging."),
):
    """Initialize or refresh institutional access session."""
    _setup_logging(verbose)
    config = Config.load()
    fetcher = PaperFetcher(config)

    console.print("[bold]Checking institutional access session...[/bold]")
    if fetcher.auth.login(force=force):
        console.print("[green]Institutional access session is active.[/green]")
    else:
        console.print("[red]Failed to authenticate institutional access.[/red]")
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
        result = fetcher.fetch_with_result(identifier, use_cache=not no_cache)
        paper = result.paper

        if result.status != "success":
            console.print(f"[yellow]Status: {result.status} ({result.reason or result.quality})[/yellow]")
            if result.next_action:
                console.print(f"[yellow]Next: {result.next_action.message}[/yellow]")
                if result.next_action.command:
                    console.print(f"[dim]{result.next_action.command}[/dim]")

        if text_only:
            console.print(result.to_text())
        elif format == "markdown":
            console.print(result.to_markdown())
        elif format == "text":
            console.print(result.to_text())
        else:
            console.print(result.to_json())

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


@app.command("est-batch")
def est_batch(
    year: int = typer.Option(2026, "--year", help="Publication year."),
    limit: int = typer.Option(20, "--limit", "-n", help="Number of EST articles."),
    output: str = typer.Option("", "--output", "-o", help="Run output directory."),
    retry_failed: bool = typer.Option(True, "--retry/--no-retry", help="Retry transient failures in a fresh browser context."),
    institution: str = typer.Option("", "--institution", help="Subscription institution search text. Omit to use configured institution or prompt."),
    login_timeout: int = typer.Option(900, "--login-timeout", help="Seconds to wait for manual SSO/2FA completion."),
    pdf_timeout: int = typer.Option(60, "--pdf-timeout", help="Seconds to wait for each candidate PDF navigation."),
    post_login_hold: int = typer.Option(0, "--post-login-hold", help="Seconds to keep the authorized article page open before PDF capture."),
    post_run_hold: int = typer.Option(0, "--post-run-hold", help="Seconds to keep the browser page open after capture or failure."),
    target_verified: int = typer.Option(0, "--target-verified", help="Stop after this many verified PDFs. Zero disables early stop."),
    attempt_cache: str = typer.Option("", "--attempt-cache", help="JSONL attempt cache path. Defaults to attempts.jsonl in the run directory."),
    skip_attempted: bool = typer.Option(False, "--skip-attempted", help="Skip DOIs already present in the attempt cache."),
):
    """Download recent Environmental Science & Technology articles through ACS/CloakBrowser."""
    from .acs_batch import ACSCloakBatchDownloader, fetch_est_records

    cfg = Config.load()
    institution = _resolve_subscription_institution(cfg, institution)
    run_dir = Path(output) if output else Path("downloads") / f"est_{year}_{limit}" / f"acs_cloak_{datetime.now():%Y%m%d_%H%M%S}"
    console.print(f"[bold]Fetching EST metadata:[/bold] year={year}, limit={limit}")
    records = fetch_est_records(year=year, limit=limit, email=cfg.email)
    if not records:
        console.print("[red]No EST records found.[/red]")
        raise typer.Exit(1)

    console.print(f"[green]Found {len(records)} DOI records.[/green]")
    console.print(f"[bold]Output:[/bold] {run_dir}")
    console.print("[dim]If a CloakBrowser window stops on SSO or 2FA, complete it there and leave the window open.[/dim]")

    downloader = ACSCloakBatchDownloader(
        cfg,
        institution_query=institution,
        login_timeout_sec=login_timeout,
        pdf_timeout_sec=pdf_timeout,
        post_login_hold_sec=post_login_hold,
        post_run_hold_sec=post_run_hold,
    )
    summary = downloader.run_records(
        records,
        run_dir,
        retry_failed=retry_failed,
        target_verified=target_verified or None,
        attempt_cache=attempt_cache or None,
        skip_attempted=skip_attempted,
    )
    console.print(
        f"[bold]Done:[/bold] {summary['success']}/{summary['count']} verified PDFs, "
        f"{summary.get('unverified', 0)} unverified PDFs."
    )
    console.print(f"[dim]PDF dir: {summary['pdf_dir']}[/dim]")
    console.print(f"[dim]Manifest: {summary['manifest']}[/dim]")
    console.print(f"[dim]Attempt cache: {summary['attempt_cache']}[/dim]")
    if summary["missing"] or summary.get("unverified", 0):
        console.print("[yellow]Some items failed or were unverified; see the run manifest and diagnostics folders.[/yellow]")
        raise typer.Exit(2)


@app.command("publisher-batch")
def publisher_batch(
    file: Path = typer.Argument(help="File containing DOI values (one per line)."),
    publisher: str = typer.Option("acs", "--publisher", "-p", help="Publisher profile key, e.g. acs, elsevier, wiley, or ieee."),
    output: str = typer.Option("", "--output", "-o", help="Run output directory."),
    browser_profile: str = typer.Option("", "--browser-profile", help="Override the persistent CloakBrowser profile directory."),
    retry_failed: bool = typer.Option(True, "--retry/--no-retry", help="Retry transient failures in a fresh browser context."),
    institution: str = typer.Option("", "--institution", help="Subscription institution search text. Omit to use configured institution or prompt."),
    login_timeout: int = typer.Option(900, "--login-timeout", help="Seconds to wait for manual SSO/2FA completion."),
    pdf_timeout: int = typer.Option(60, "--pdf-timeout", help="Seconds to wait for each candidate PDF navigation."),
    target_verified: int = typer.Option(0, "--target-verified", help="Stop after this many verified PDFs. Zero disables early stop."),
    attempt_cache: str = typer.Option("", "--attempt-cache", help="JSONL attempt cache path. Defaults to attempts.jsonl in the run directory."),
    skip_attempted: bool = typer.Option(False, "--skip-attempted", help="Skip DOIs already present in the attempt cache."),
):
    """Download a DOI list through a named publisher profile and CloakBrowser."""
    from .publisher_batch import PaperRecord, PublisherBatchDownloader
    from .publisher_profiles import get_publisher_profile

    if not file.exists():
        console.print(f"[red]File not found: {file}[/red]")
        raise typer.Exit(1)

    records = [
        PaperRecord(doi=line.strip())
        for line in file.read_text(encoding="utf-8").splitlines()
        if line.strip() and not line.strip().startswith("#")
    ]
    if not records:
        console.print("[yellow]No DOIs found in file.[/yellow]")
        raise typer.Exit(0)

    try:
        profile = get_publisher_profile(publisher)
    except ValueError as exc:
        console.print(f"[red]{exc}[/red]")
        raise typer.Exit(1) from exc

    cfg = Config.load()
    if browser_profile:
        cfg.chrome_profile_dir = browser_profile
    institution = _resolve_subscription_institution(cfg, institution)
    profile_key = publisher.strip().lower().replace(" ", "-")
    run_dir = Path(output) if output else Path("downloads") / f"{profile_key}_{len(records)}" / f"cloak_{datetime.now():%Y%m%d_%H%M%S}"
    console.print(f"[bold]Publisher profile:[/bold] {profile.name}")
    console.print(f"[bold]Found {len(records)} DOI records.[/bold]")
    console.print(f"[bold]Output:[/bold] {run_dir}")
    console.print(f"[bold]Browser profile:[/bold] {cfg.chrome_profile_dir}")
    console.print("[dim]If a CloakBrowser window stops on SSO or 2FA, complete it there and leave the window open.[/dim]")

    downloader = PublisherBatchDownloader(
        cfg,
        profile=profile,
        institution_query=institution,
        login_timeout_sec=login_timeout,
        pdf_timeout_sec=pdf_timeout,
    )
    summary = downloader.run_records(
        records,
        run_dir,
        retry_failed=retry_failed,
        target_verified=target_verified or None,
        attempt_cache=attempt_cache or None,
        skip_attempted=skip_attempted,
    )
    console.print(
        f"[bold]Done:[/bold] {summary['success']}/{summary['count']} verified PDFs, "
        f"{summary.get('unverified', 0)} unverified PDFs."
    )
    console.print(f"[dim]PDF dir: {summary['pdf_dir']}[/dim]")
    console.print(f"[dim]Manifest: {summary['manifest']}[/dim]")
    console.print(f"[dim]Attempt cache: {summary['attempt_cache']}[/dim]")
    if summary["missing"] or summary.get("unverified", 0):
        console.print("[yellow]Some items failed or were unverified; see the run manifest and diagnostics folders.[/yellow]")
        raise typer.Exit(2)


@app.command("papers")
def papers(
    file: Path = typer.Argument(help="File containing DOI values (one per line)."),
    publisher: str = typer.Option("auto", "--publisher", "-p", help="Publisher profile, or 'auto' to infer from DOI prefixes."),
    output: str = typer.Option("", "--output", "-o", help="Run output directory."),
    browser_profile: str = typer.Option("", "--browser-profile", help="Override the persistent CloakBrowser profile directory."),
    institution: str = typer.Option("", "--institution", help="Subscription institution search text. Omit to use configured institution or prompt."),
    login_timeout: int = typer.Option(900, "--login-timeout", help="Seconds to wait for manual SSO/CAPTCHA completion."),
    pdf_timeout: int = typer.Option(90, "--pdf-timeout", help="Seconds to wait for each PDF navigation."),
    post_login_hold: int = typer.Option(0, "--post-login-hold", help="Seconds to keep the authorized article page open before PDF capture."),
    post_run_hold: int = typer.Option(0, "--post-run-hold", help="Seconds to keep the browser page open after capture or failure."),
    retry_failed: bool = typer.Option(True, "--retry/--no-retry", help="Retry transient failures in a fresh browser context."),
    concurrency: int = typer.Option(1, "--concurrency", "-j", min=1, max=4, help="Parallel browser workers. Use 2 for ScienceDirect; higher values may trigger publisher checks."),
    broker: bool = typer.Option(True, "--broker/--no-broker", help="Use the long-lived publisher session broker by default."),
    broker_ttl: int = typer.Option(86400, "--broker-ttl", help="Seconds to keep an auto-started broker alive."),
):
    """Recommended browser workflow for closed-access publisher PDFs."""
    from .publisher_batch import PaperRecord, PublisherBatchDownloader
    from .publisher_profiles import get_publisher_profile, infer_publisher_profile, list_publisher_profiles

    if not file.exists():
        console.print(f"[red]File not found: {file}[/red]")
        raise typer.Exit(1)

    records = [
        PaperRecord(doi=line.strip())
        for line in file.read_text(encoding="utf-8").splitlines()
        if line.strip() and not line.strip().startswith("#")
    ]
    if not records:
        console.print("[yellow]No DOIs found in file.[/yellow]")
        raise typer.Exit(0)

    if publisher.strip().lower() == "auto":
        inferred = [infer_publisher_profile(record.doi) for record in records]
        profiles = {profile for profile in inferred if profile is not None}
        if len(profiles) != 1 or len(profiles) != len(set(inferred)):
            console.print("[red]Could not infer one publisher for all DOIs.[/red]")
            console.print(f"[yellow]Use --publisher with one of: {', '.join(list_publisher_profiles())}.[/yellow]")
            raise typer.Exit(1)
        profile = profiles.pop()
    else:
        try:
            profile = get_publisher_profile(publisher)
        except ValueError as exc:
            console.print(f"[red]{exc}[/red]")
            raise typer.Exit(1) from exc

    cfg = Config.load()
    if browser_profile:
        cfg.chrome_profile_dir = browser_profile
    institution = _resolve_subscription_institution(cfg, institution)
    profile_key = profile.name.lower().replace(" ", "-")
    run_dir = Path(output) if output else Path("downloads") / f"papers_{profile_key}_{len(records)}" / f"browser_{datetime.now():%Y%m%d_%H%M%S}"

    console.print(f"[bold]Recommended route:[/bold] browser-based publisher workflow ({profile.name})")
    console.print("[dim]Complete SSO, 2FA, or CAPTCHA in the opened browser window; InstSci continues automatically.[/dim]")
    console.print(f"[bold]Found {len(records)} DOI records.[/bold]")
    console.print(f"[bold]Output:[/bold] {run_dir}")
    console.print(f"[bold]Browser profile:[/bold] {cfg.chrome_profile_dir}")

    profile_key_arg = publisher.strip().lower().replace(" ", "-")
    broker_publisher = profile_key_arg if profile_key_arg != "auto" else profile.name.lower().replace(" ", "-")
    if broker:
        from . import session_broker

        if not session_broker.broker_is_running(broker_publisher):
            console.print(f"[dim]Starting publisher session broker: {broker_publisher}[/dim]")
            session_broker.start_broker_process(
                publisher=broker_publisher,
                browser_profile=cfg.chrome_profile_dir,
                institution=institution,
                ttl_seconds=broker_ttl,
                cwd=Path.cwd(),
            )
            deadline = time.time() + 30
            while time.time() < deadline and not session_broker.broker_is_running(broker_publisher):
                time.sleep(1)
        if session_broker.broker_is_running(broker_publisher):
            console.print(f"[bold]Session broker:[/bold] running ({broker_publisher})")
            timeout_seconds = max(
                120,
                login_timeout + len(records) * (pdf_timeout + post_login_hold + post_run_hold + 60),
            )
            summary = session_broker.submit_broker_job(
                publisher=broker_publisher,
                records=[{"doi": record.doi, "title": record.title, "published": record.published, "url": record.url} for record in records],
                output_dir=str(run_dir),
                institution=institution,
                login_timeout=login_timeout,
                pdf_timeout=pdf_timeout,
                post_login_hold=post_login_hold,
                post_run_hold=post_run_hold,
                timeout_seconds=timeout_seconds,
            )
            console.print(
                f"[bold]Done:[/bold] {summary['success']}/{summary['count']} verified PDFs, "
                f"{summary.get('unverified', 0)} unverified PDFs."
            )
            console.print(f"[dim]PDF dir: {summary['pdf_dir']}[/dim]")
            console.print(f"[dim]Manifest: {summary['manifest']}[/dim]")
            if summary["missing"] or summary.get("unverified", 0):
                console.print("[yellow]Some items need manual CAPTCHA/login attention; rerun the same command after completing it.[/yellow]")
                raise typer.Exit(2)
            return
        console.print("[yellow]Session broker did not start; falling back to one-shot browser workflow.[/yellow]")

    downloader = PublisherBatchDownloader(
        cfg,
        profile=profile,
        institution_query=institution,
        login_timeout_sec=login_timeout,
        pdf_timeout_sec=pdf_timeout,
        post_login_hold_sec=post_login_hold,
        post_run_hold_sec=post_run_hold,
    )
    summary = downloader.run_records(
        records,
        run_dir,
        retry_failed=retry_failed,
        concurrency=concurrency,
    )
    console.print(
        f"[bold]Done:[/bold] {summary['success']}/{summary['count']} verified PDFs, "
        f"{summary.get('unverified', 0)} unverified PDFs."
    )
    console.print(f"[dim]PDF dir: {summary['pdf_dir']}[/dim]")
    console.print(f"[dim]Manifest: {summary['manifest']}[/dim]")
    if summary["missing"] or summary.get("unverified", 0):
        console.print("[yellow]Some items need manual CAPTCHA/login attention; rerun the same command after completing it.[/yellow]")
        raise typer.Exit(2)


@app.command("session-broker-status")
def session_broker_status(
    publisher: str = typer.Option("elsevier", "--publisher", "-p", help="Publisher broker key."),
):
    """Show a long-lived publisher browser session broker."""
    from . import session_broker

    state = session_broker.load_broker_state(publisher)
    running = session_broker.broker_is_running(publisher)
    table = Table(title="InstSci Session Broker")
    table.add_column("Publisher")
    table.add_column("Status")
    table.add_column("PID")
    table.add_column("Profile", overflow="fold")
    table.add_column("Queue", overflow="fold")
    table.add_row(
        publisher,
        "running" if running else "stopped",
        str(state.get("pid", "")) if state else "",
        str(state.get("profile_dir", "")) if state else "",
        str(state.get("queue_dir", "")) if state else "",
    )
    console.print(table)


@app.command("session-broker-stop")
def session_broker_stop(
    publisher: str = typer.Option("elsevier", "--publisher", "-p", help="Publisher broker key."),
):
    """Ask a long-lived publisher broker to stop."""
    from . import session_broker

    session_broker.broker_stop_path(publisher).parent.mkdir(parents=True, exist_ok=True)
    session_broker.broker_stop_path(publisher).write_text("stop", encoding="utf-8")
    console.print(f"[green]Stop requested for broker:[/green] {publisher}")


@app.command("session-broker-run", hidden=True)
def session_broker_run(
    publisher: str = typer.Option(..., "--publisher", "-p"),
    browser_profile: str = typer.Option("", "--browser-profile"),
    institution: str = typer.Option("", "--institution"),
    ttl: int = typer.Option(86400, "--ttl"),
):
    """Run the long-lived broker loop. Internal command."""
    from .publisher_batch import PaperRecord, PublisherBatchDownloader
    from .publisher_profiles import get_publisher_profile
    from .session_broker import BrokerState, broker_dir, broker_stop_path, write_broker_state

    cfg = Config.load()
    if browser_profile:
        cfg.chrome_profile_dir = browser_profile
    institution = _resolve_subscription_institution(cfg, institution, prompt=False)
    profile = get_publisher_profile(publisher)
    root = broker_dir(publisher)
    queue_dir = root / "queue"
    queue_dir.mkdir(parents=True, exist_ok=True)
    state = BrokerState(
        publisher=publisher,
        profile_dir=cfg.chrome_profile_dir,
        pid=os.getpid(),
        queue_dir=str(queue_dir),
        started_at=datetime.now().isoformat(timespec="seconds"),
        ttl_seconds=ttl,
        heartbeat_at=datetime.now().isoformat(timespec="seconds"),
    )
    write_broker_state(state)
    downloader = PublisherBatchDownloader(
        cfg,
        profile=profile,
        institution_query=institution,
        login_timeout_sec=900,
        pdf_timeout_sec=90,
    )
    context = downloader._launch_context()
    deadline = time.time() + max(1, ttl)
    try:
        while time.time() < deadline and not broker_stop_path(publisher).exists():
            state.heartbeat_at = datetime.now().isoformat(timespec="seconds")
            write_broker_state(state)
            jobs = sorted(queue_dir.glob("*.json"))
            for job_path in jobs:
                if job_path.name.endswith(".done.json"):
                    continue
                try:
                    job = json.loads(job_path.read_text(encoding="utf-8"))
                    run_dir = Path(str(job["output_dir"]))
                    primary_dir = run_dir / "primary"
                    primary_dir.mkdir(parents=True, exist_ok=True)
                    job_downloader = PublisherBatchDownloader(
                        cfg,
                        profile=profile,
                        institution_query=str(job.get("institution") or institution),
                        login_timeout_sec=int(job.get("login_timeout") or 900),
                        pdf_timeout_sec=int(job.get("pdf_timeout") or 90),
                        post_login_hold_sec=int(job.get("post_login_hold") or 0),
                        post_run_hold_sec=int(job.get("post_run_hold") or 0),
                    )
                    records = [PaperRecord(**record) for record in job.get("records", [])]
                    results = []
                    for record in records:
                        results.append(job_downloader.fetch_one(context, record, primary_dir))
                        job_downloader._write_results(primary_dir / "summary_partial.json", results)
                    job_downloader._write_results(primary_dir / "summary.json", results)
                    summary = job_downloader._write_complete_artifacts(records, results, run_dir)
                    summary["publisher"] = profile.name
                    summary["broker"] = True
                    summary["browser_profile_dir"] = cfg.chrome_profile_dir
                    (run_dir / "summary.json").write_text(
                        json.dumps(summary, ensure_ascii=False, indent=2),
                        encoding="utf-8",
                    )
                    (queue_dir / f"{job['id']}.done.json").write_text(
                        json.dumps(summary, ensure_ascii=False, indent=2),
                        encoding="utf-8",
                    )
                except Exception as exc:
                    payload = {"count": 0, "success": 0, "missing": 1, "unverified": 0, "error": f"{type(exc).__name__}: {exc}"}
                    done_name = f"{job_path.stem}.done.json"
                    (queue_dir / done_name).write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
                finally:
                    job_path.unlink(missing_ok=True)
            time.sleep(2)
    finally:
        try:
            context.close()
        except Exception:
            pass


@app.command("session-doctor")
def session_doctor(
    publisher: str = typer.Option("", "--publisher", "-p", help="Publisher profile key to include publisher domains."),
    browser_profile: str = typer.Option("", "--browser-profile", help="Inspect one browser profile instead of known candidates."),
    output: str = typer.Option("", "--output", "-o", help="Optional JSON report path."),
):
    """Inspect local browser profiles for institution/publisher session presence."""
    from .profile_health import DEFAULT_SESSION_DOMAINS, candidate_profile_dirs, inspect_browser_profile
    from .publisher_profiles import get_publisher_profile

    cfg = Config.load()
    profile = None
    domains = list(DEFAULT_SESSION_DOMAINS)
    if publisher:
        try:
            profile = get_publisher_profile(publisher)
        except ValueError as exc:
            console.print(f"[red]{exc}[/red]")
            raise typer.Exit(1) from exc
        domains.extend(profile.base_domains)
    domains = list(dict.fromkeys(domain for domain in domains if domain))

    profiles = [Path(browser_profile)] if browser_profile else candidate_profile_dirs(cfg, workspace=Path.cwd())
    reports = [inspect_browser_profile(path, domains) for path in profiles]

    table = Table(title="InstSci Browser Session Doctor")
    table.add_column("Profile", overflow="fold")
    table.add_column("Exists", width=8)
    table.add_column("Session Hosts", overflow="fold")
    table.add_column("Latest Expiry", overflow="fold")
    table.add_column("Notes", overflow="fold")
    for report in reports:
        present = []
        expiries = []
        seen_hosts: set[str] = set()
        for domain, info in report["domains"].items():
            latest = str(info.get("latest_expires_at") or "")
            if latest:
                expiries.append(f"{domain}: {latest}")
            for host in info.get("hosts", []):
                host_name = str(host.get("host") or "")
                if host_name in seen_hosts:
                    continue
                seen_hosts.add(host_name)
                count = int(host.get("cookie_count") or 0)
                if count:
                    session_count = int(host.get("session_cookie_count") or 0)
                    suffix = f", session={session_count}" if session_count else ""
                    present.append(f"{host_name}({count}{suffix})")
        notes = report.get("error") or ("cookie DB missing" if report["exists"] and not report["cookies_db_exists"] else "")
        table.add_row(
            report["profile_dir"],
            "yes" if report["exists"] else "no",
            ", ".join(present) or "-",
            ", ".join(expiries) or "-",
            notes,
        )
    console.print(table)

    if output:
        output_path = Path(output)
        output_path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "publisher": profile.name if profile else "",
            "domains": domains,
            "reports": reports,
        }
        output_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        console.print(f"[dim]Report: {output_path}[/dim]")


@app.command("publisher-doctor")
def publisher_doctor(
    publisher: str = typer.Option("all", "--publisher", "-p", help="Publisher profile key, or 'all'."),
    output: str = typer.Option("", "--output", "-o", help="Optional JSON report path."),
    probe_pdf: bool = typer.Option(True, "--probe-pdf/--no-probe-pdf", help="Probe PDF candidate URLs without saving files."),
    max_candidates: int = typer.Option(4, "--max-candidates", min=0, max=10, help="Maximum PDF candidates to probe per publisher."),
    timeout: int = typer.Option(20, "--timeout", min=3, max=120, help="Network timeout in seconds."),
):
    """HTTP preflight to verify reusable publisher PDF routes.

    Browser-backed InstSci workflows are authoritative for publisher PDF
    capability verdicts; this command only checks route templates and blockers.
    """
    from .publisher_access import verify_publishers
    from .publisher_profiles import list_publisher_profiles

    keys = list_publisher_profiles() if publisher.strip().lower() == "all" else [publisher.strip()]
    console.print(f"[bold]Verifying publisher access assets:[/bold] {', '.join(keys)}")
    console.print(
        "[yellow]HTTP preflight only:[/yellow] use the built-in browser workflow "
        "for final publisher PDF capability verdicts."
    )
    results = verify_publishers(
        keys,
        probe_pdf=probe_pdf,
        max_candidates=max_candidates,
        timeout=timeout,
    )

    table = Table(title="Publisher Access Verification")
    table.add_column("Publisher", width=18)
    table.add_column("Landing", width=8)
    table.add_column("PDF Links", width=9, justify="right")
    table.add_column("Observed", width=22)
    table.add_column("Final Host", overflow="fold")
    needs_attention = False
    for result in results:
        if result["landing_status"] == 404 or not result["pdf_candidates"]:
            needs_attention = True
        table.add_row(
            result["profile_key"],
            str(result["landing_status"]),
            str(len(result["pdf_candidates"])),
            result["observed_access"],
            urlparse(result["landing_url"]).hostname or result["landing_url"],
        )
    console.print(table)

    if output:
        output_path = Path(output)
        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_text(json.dumps(results, ensure_ascii=False, indent=2), encoding="utf-8")
        console.print(f"[dim]Report: {output_path}[/dim]")

    if needs_attention:
        raise typer.Exit(2)


@app.command("identity-policy")
def identity_policy(
    output: str = typer.Option("", "--output", "-o", help="Optional JSON report path."),
):
    """Show the institutional identity routing policy for publisher PDFs."""
    from .publisher_access import load_institutional_identity_policy

    policy = load_institutional_identity_policy()
    console.print("[bold]InstSci Institutional Identity Policy[/bold]")
    console.print(f"Default mode: [cyan]{policy['default_mode']}[/cyan]")
    console.print(f"Default identity: [cyan]{policy['default_identity']}[/cyan]")
    required = "required" if policy["subscription_institution"]["required_for_closed_access"] else "optional"
    console.print(f"Subscription institution: [cyan]{required}[/cyan]")
    console.print(f"Preferred off-campus access: [cyan]{policy['preferred_off_campus_access']}[/cyan]")
    console.print(f"Final PDF verdict requires: [cyan]{policy['final_pdf_verdict_requires']}[/cyan]")

    table = Table(title="Identity Route Order")
    table.add_column("Order", width=5, justify="right")
    table.add_column("Identity", width=22)
    table.add_column("Role", overflow="fold")
    table.add_column("Global default", width=14)
    for index, identity_key in enumerate(policy["identity_order"], 1):
        section_key = "webvpn" if identity_key == "webvpn_broker" else identity_key
        identity = policy["identities"].get(section_key, {})
        table.add_row(
            str(index),
            identity_key,
            str(identity.get("recommended_role", "")).replace("_", " "),
            "yes" if identity.get("global_default") else "no",
        )
    console.print(table)

    webvpn = policy["identities"]["webvpn"]
    console.print(
        "[yellow]WebVPN is optional:[/yellow] "
        f"{webvpn['persistence_limits']['cookie_store']['notes']}"
    )
    console.print(
        "[yellow]Use visible CloakBrowser:[/yellow] "
        "keep the same live context for SSO, CAPTCHA, Cloudflare, and PDF-token flows."
    )

    if output:
        output_path = Path(output)
        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_text(json.dumps(policy, ensure_ascii=False, indent=2), encoding="utf-8")
        console.print(f"[dim]Report: {output_path}[/dim]")


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
    table.add_column("Type", width=12)
    table.add_column("Host", max_width=40)
    table.add_column("Custom Key", width=5, justify="center")

    from .schools import WEBVPN_DEFAULT_KEY
    for i, s in enumerate(results, 1):
        has_custom = "Y" if s.key != WEBVPN_DEFAULT_KEY else ""
        type_label = {
            "webvpn": "CampusPortal",
            "easyconnect": "CampusConnector",
            "atrust": "CampusConnector",
            "ezproxy": "LibraryPortal",
        }.get(s.school_type, s.school_type)
        table.add_row(str(i), s.province, s.name, type_label, s.host, has_custom)

    console.print(table)


@app.command()
def config_cmd(
    show: bool = typer.Option(True, "--show", help="Show current config."),
    set_email: str = typer.Option("", "--email", help="Set email for Unpaywall API."),
    set_output: str = typer.Option("", "--output-dir", help="Set default output directory."),
    set_access_url: str = typer.Option("", "--access-url", help="Set institutional access gateway URL."),
    set_webvpn_url: str = typer.Option("", "--webvpn-url", help="Legacy gateway URL option.", hidden=True),
    set_school: str = typer.Option("", "--school", help="Set school (use 'instsci schools' to list)."),
    set_connector_url: str = typer.Option("", "--connector-url", help="Set local SOCKS5 connector URL for EasyConnect."),
    set_proxy_url: str = typer.Option("", "--proxy-url", help="Legacy local connector URL option.", hidden=True),
    set_elsevier_key: str = typer.Option("", "--elsevier-api-key", help="Set Elsevier API key."),
    set_elsevier_token: str = typer.Option("", "--elsevier-inst-token", help="Set Elsevier institutional token."),
    set_federated_enable: bool = typer.Option(False, "--federated-enable", help="Enable federated institutional auth."),
    set_federated_disable: bool = typer.Option(False, "--federated-disable", help="Disable federated institutional auth."),
    set_federated_school: str = typer.Option("", "--federated-school", help="Set school name for federated login."),
    set_carsi_enable: bool = typer.Option(False, "--carsi-enable", help="Legacy federated auth option.", hidden=True),
    set_carsi_disable: bool = typer.Option(False, "--carsi-disable", help="Legacy federated auth option.", hidden=True),
    set_carsi_school: str = typer.Option("", "--carsi-school", help="Legacy federated school option.", hidden=True),
):
    """View or update configuration."""
    cfg = Config.load()
    changed = False

    if set_email:
        cfg.email = set_email
        changed = True
        console.print(f"[green]Email set to: {set_email}[/green]")

    if set_output:
        cfg.output_dir = set_output
        changed = True
        console.print(f"[green]Output dir set to: {set_output}[/green]")

    access_url = set_access_url or set_webvpn_url
    if access_url:
        cfg.webvpn_base_url = access_url.rstrip("/")
        changed = True
        console.print(f"[green]Institutional access URL set to: {access_url}[/green]")

    if set_school:
        try:
            entry = _apply_school_config(cfg, set_school)
            changed = True
            type_label = _school_type_label(entry.school_type)
            console.print(f"[green]School set to: {entry.name} ({type_label}, {entry.host})[/green]")
            if entry.school_type == "easyconnect":
                console.print("[yellow]This school uses a local campus connector. Please:[/yellow]")
                console.print("  1. Connect via zju-connect: [cyan]zju-connect -server {0}[/cyan]".format(entry.host))
                console.print("  2. Set connector: [cyan]instsci config-cmd --connector-url socks5://127.0.0.1:1080[/cyan]")
        except ValueError as e:
            console.print(f"[red]{e}[/red]")
            raise typer.Exit(1)

    connector_url = set_connector_url or set_proxy_url
    if connector_url:
        cfg.proxy_url = connector_url
        changed = True
        console.print(f"[green]Connector URL set to: {connector_url}[/green]")

    if set_elsevier_key:
        cfg.elsevier_api_key = set_elsevier_key
        changed = True
        console.print("[green]Elsevier API key saved.[/green]")

    if set_elsevier_token:
        cfg.elsevier_inst_token = set_elsevier_token
        changed = True
        console.print("[green]Elsevier institutional token saved.[/green]")

    federated_enable = set_federated_enable or set_carsi_enable
    federated_disable = set_federated_disable or set_carsi_disable
    federated_school = set_federated_school or set_carsi_school

    if federated_enable:
        cfg.carsi_enabled = True
        changed = True
        console.print("[green]Federated institutional auth enabled.[/green]")

    if federated_disable:
        cfg.carsi_enabled = False
        changed = True
        console.print("[yellow]Federated institutional auth disabled.[/yellow]")

    if federated_school:
        cfg.carsi_idp_name = federated_school
        changed = True
        console.print(f"[green]Federated login school set to: {federated_school}[/green]")

    if changed:
        cfg.save()

    has_setter = any([set_email, set_output, set_access_url, set_webvpn_url, set_school,
                      set_connector_url, set_proxy_url,
                       set_elsevier_key, set_elsevier_token,
                       set_federated_enable, set_federated_disable, set_federated_school,
                       set_carsi_enable, set_carsi_disable, set_carsi_school])
    if show and not has_setter:
        # Determine school type
        try:
            from .schools import get_school as _get_school
            school_entry = _get_school(cfg.school)
            school_type = school_entry.school_type
        except ValueError:
            school_type = "unknown"

        console.print("[bold]Current configuration:[/bold]")
        console.print(f"  School:            {cfg.school} ({school_type})")
        console.print(f"  Access URL:        {_access_url(cfg)}")
        console.print(f"  Connector URL:     {cfg.proxy_url or '(not set)'}")
        console.print(f"  Email:             {cfg.email}")
        console.print(f"  Elsevier API key:  {'****' if cfg.elsevier_api_key else '(not set)'}")
        console.print(f"  Elsevier inst tok: {'****' if cfg.elsevier_inst_token else '(not set)'}")
        console.print(f"  Federated login:   {'Yes' if cfg.carsi_enabled else 'No'}")
        console.print(f"  Federated school:  {cfg.carsi_idp_name or '(not set)'}")
        console.print(f"  Output dir:        {cfg.output_dir}")
        console.print(f"  Cache dir:         {cfg.cache_dir}")
        console.print(f"  Cookie path:       {cfg.cookie_path}")


def _run_federated_login(
    publisher: str,
    url: str,
    force: bool,
    verbose: bool,
) -> None:
    """Run the federated institutional login flow."""
    _setup_logging(verbose)
    config = Config.load()

    if not config.carsi_enabled:
        console.print("[red]Federated login is not enabled. Run: instsci config-cmd --federated-enable --federated-school \"你的学校名\"[/red]")
        raise typer.Exit(1)

    if not config.carsi_idp_name:
        console.print("[red]Federated login school not set. Run: instsci config-cmd --federated-school \"你的学校名\"[/red]")
        raise typer.Exit(1)

    if not publisher and url:
        from .carsi import detect_publisher
        publisher = detect_publisher(url) or ""

    if not publisher:
        console.print("[yellow]Available publishers:[/yellow]")
        console.print("  sciencedirect, springer, wiley, ieee, tandfonline, nature")
        publisher = typer.prompt("Enter publisher name")

    from .carsi import CARSIClient
    carsi = CARSIClient(config)
    try:
        console.print(f"[bold]Federated login for: {publisher}[/bold]")
        console.print(f"[dim]School: {config.carsi_idp_name}[/dim]")
        if carsi.login(publisher, force=force):
            console.print("[green]Federated access session established![/green]")
        else:
            console.print("[red]Federated login failed.[/red]")
            raise typer.Exit(1)
    finally:
        carsi.close()


@app.command("federated-login")
def federated_login(
    publisher: str = typer.Option("", "--publisher", "-p", help="Publisher (sciencedirect, springer, wiley, ieee, tandfonline, nature). Omit to pick from article URL."),
    url: str = typer.Option("", "--url", "-u", help="Article URL to auto-detect publisher."),
    force: bool = typer.Option(False, "--force", "-f", help="Force re-login."),
    verbose: bool = typer.Option(False, "--verbose", "-v", help="Verbose logging."),
):
    """Authenticate via federated institutional login."""
    _run_federated_login(publisher, url, force, verbose)


@app.command("carsi-login", hidden=True)
def carsi_login(
    publisher: str = typer.Option("", "--publisher", "-p", help="Publisher (sciencedirect, springer, wiley, ieee, tandfonline, nature). Omit to pick from article URL."),
    url: str = typer.Option("", "--url", "-u", help="Article URL to auto-detect publisher."),
    force: bool = typer.Option(False, "--force", "-f", help="Force re-login."),
    verbose: bool = typer.Option(False, "--verbose", "-v", help="Verbose logging."),
):
    """Legacy alias for federated-login."""
    _run_federated_login(publisher, url, force, verbose)


@app.command()
def elsevier_setup(
    api_key: str = typer.Option("", "--api-key", help="Global Elsevier API key saved in the InstSci config."),
    inst_token: str = typer.Option("", "--inst-token", help="Global Elsevier institutional token, if your library provides one."),
    validate: bool = typer.Option(False, "--validate", help="Validate the saved global Elsevier API configuration."),
    test_doi: str = typer.Option(
        "10.1016/j.watres.2024.121507",
        "--test-doi",
        help="Validation-only Elsevier DOI. This does not bind the config to one article.",
    ),
):
    """Save the global Elsevier API config for ScienceDirect XML/object-eid PDF download.

    Get a free key at: https://dev.elsevier.com/
    """
    cfg = Config.load()

    if api_key:
        cfg.elsevier_api_key = api_key
        cfg.save()
        console.print("[green]Global Elsevier API key saved.[/green]")

    if inst_token:
        cfg.elsevier_inst_token = inst_token
        cfg.save()
        console.print("[green]Global Elsevier institutional token saved.[/green]")

    key = cfg.elsevier_api_key
    if not key:
        console.print("[yellow]No Elsevier API key configured.[/yellow]")
        console.print()
        console.print("Configure the project-wide Elsevier API key before testing ScienceDirect API retrieval:")
        console.print("  1. Go to [cyan]https://dev.elsevier.com/[/cyan]")
        console.print("  2. Register or sign in")
        console.print("  3. My API Key / API Key Settings -> create an API key")
        console.print("  4. If prompted, choose ScienceDirect / Article Retrieval permissions")
        console.print("  5. Run once: [cyan]instsci elsevier-setup --api-key YOUR_KEY --validate[/cyan]")
        console.print()
        console.print("Institutional token is optional and should be configured only if your library provides it:")
        console.print("  [cyan]instsci elsevier-setup --api-key KEY --inst-token TOKEN[/cyan]")
        raise typer.Exit(1 if validate else 0)

    if validate:
        from .sources import elsevier_api

        console.print("[bold]Validating global Elsevier XML/object-eid PDF retrieval...[/bold]")
        if cfg.proxy_url:
            console.print("[dim]Route order: direct first, configured connector fallback.[/dim]")
        else:
            console.print("[dim]Route order: direct route.[/dim]")
        console.print(f"[dim]Validation DOI only: {test_doi}[/dim]")

        data = elsevier_api.fetch_fulltext(
            test_doi,
            api_key=key,
            inst_token=cfg.elsevier_inst_token,
            proxy_url=cfg.proxy_url,
        )
        if not data:
            console.print("[red]XML retrieval failed.[/red]")
            console.print(
                "[yellow]Check that the API key is valid and that api.elsevier.com "
                "uses your campus, library VPN, rule VPN, or institutional exit.[/yellow]"
            )
            raise typer.Exit(2)

        eids = data.get("pdf_eids", [])
        route = data.get("api_route", "")
        console.print(f"[green]XML retrieval: OK[/green] route={route or 'unknown'}")
        console.print(f"  Title: {data.get('title') or '(unknown)'}")
        console.print(f"  MAIN PDF object EIDs: {len(eids)}")
        if not eids:
            console.print("[red]No MAIN PDF object EID found in XML.[/red]")
            raise typer.Exit(2)

        pdf = elsevier_api.fetch_pdf(
            test_doi,
            api_key=key,
            inst_token=cfg.elsevier_inst_token,
            proxy_url=cfg.proxy_url,
            pdf_eids=eids,
            preferred_route=route,
        )
        if not pdf:
            console.print("[red]Object PDF retrieval failed.[/red]")
            console.print(
                "[yellow]If XML worked but object/eid failed, the current route is usually "
                "not entitled for this closed-access PDF. Prefer direct campus/rule VPN "
                "routing before configured connector fallback.[/yellow]"
            )
            raise typer.Exit(2)

        console.print(f"[green]Object PDF retrieval: OK ({len(pdf)} bytes)[/green]")

    console.print()
    console.print(f"  API Key:        {_mask_secret(key)}")
    console.print(f"  Inst Token:     {'****' if cfg.elsevier_inst_token else '(not set)'}")


if __name__ == "__main__":
    app()
