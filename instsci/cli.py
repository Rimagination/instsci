"""CLI interface for InstSci."""

import importlib.metadata
import json
import logging
import os
import shutil
import subprocess
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
from .download_defaults import (
    DEFAULT_BROWSER_SPEED,
    DEFAULT_BROKER_TTL_SECONDS,
    DEFAULT_KEEPALIVE_INTERVAL_SECONDS,
    DEFAULT_LOGIN_TIMEOUT_SECONDS,
)
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


_BROWSER_SPEED_MODES = ("careful", "balanced", "fast")
OVERNIGHT_LOGIN_TIMEOUT_SECONDS = DEFAULT_LOGIN_TIMEOUT_SECONDS
OVERNIGHT_BROKER_TTL_SECONDS = DEFAULT_BROKER_TTL_SECONDS


def _normalize_speed_mode(speed: str) -> str:
    value = (speed or DEFAULT_BROWSER_SPEED).strip().lower()
    if value not in _BROWSER_SPEED_MODES:
        allowed = ", ".join(_BROWSER_SPEED_MODES)
        raise ValueError(f"Unsupported speed mode '{speed}'. Available: {allowed}")
    return value


def _effective_browser_concurrency(speed: str, requested: int) -> int:
    """Return browser worker count without surprising users with extra sessions."""
    speed_mode = _normalize_speed_mode(speed)
    try:
        requested_value = int(requested)
    except (TypeError, ValueError):
        requested_value = 1
    requested_value = max(1, requested_value)
    if speed_mode == "fast":
        return min(requested_value, 2)
    return 1


def _apply_overnight_mode(
    *,
    login_timeout: int,
    broker: bool,
    broker_ttl: int,
    speed_mode: str,
    concurrency: int,
) -> tuple[int, bool, int, str, int]:
    """Use a conservative long-lived browser preset for unattended batches."""
    return (
        max(int(login_timeout or 0), OVERNIGHT_LOGIN_TIMEOUT_SECONDS),
        True,
        max(int(broker_ttl or 0), OVERNIGHT_BROKER_TTL_SECONDS),
        "careful",
        1,
    )


def _print_overnight_notice(login_timeout: int, broker_ttl: int) -> None:
    console.print(
        "[yellow]Overnight mode keeps a live broker browser and waits longer for manual re-auth; "
        "it does not store institution credentials, OTPs, or CAPTCHA answers.[/yellow]"
    )
    console.print(
        f"[dim]Overnight mode: login wait {login_timeout}s, broker TTL {broker_ttl}s, browser workers 1.[/dim]"
    )


def _publisher_profile_key(profile) -> str:
    from .publisher_profiles import PUBLISHER_PROFILES

    for key, candidate in PUBLISHER_PROFILES.items():
        if candidate == profile:
            return key
    return profile.name.strip().lower().replace(" ", "-")


def _group_records_by_publisher(records, publisher: str):
    from .publisher_profiles import get_publisher_profile, infer_publisher_profile, list_publisher_profiles

    requested = (publisher or "auto").strip()
    if requested.lower() != "auto":
        profile = get_publisher_profile(requested)
        return [(_publisher_profile_key(profile), profile, list(records))]

    groups: dict[str, list] = {}
    unresolved: list[str] = []
    for record in records:
        doi = getattr(record, "doi", "")
        profile = infer_publisher_profile(doi)
        if profile is None:
            unresolved.append(doi)
            continue
        key = _publisher_profile_key(profile)
        if key not in groups:
            groups[key] = [profile, []]
        groups[key][1].append(record)

    if unresolved:
        available = ", ".join(list_publisher_profiles())
        sample = ", ".join(unresolved[:5])
        more = f" and {len(unresolved) - 5} more" if len(unresolved) > 5 else ""
        raise ValueError(
            f"Could not infer publisher for DOI(s): {sample}{more}. "
            f"Use --publisher with one of: {available}."
        )
    return [(key, values[0], values[1]) for key, values in groups.items()]


def _broker_records(records) -> list[dict[str, str]]:
    return [
        {
            "doi": record.doi,
            "title": record.title,
            "published": record.published,
            "url": record.url,
        }
        for record in records
    ]


def _read_doi_lines(file: Path) -> list[str]:
    dois: list[str] = []
    for line in file.read_text(encoding="utf-8-sig").splitlines():
        doi = line.strip().lstrip("\ufeff").strip()
        if doi and not doi.startswith("#"):
            dois.append(doi)
    return dois


def _print_speed_notice(speed_mode: str, requested: int, effective: int, *, broker: bool = False) -> None:
    if broker:
        console.print("[dim]Session broker reuses one live CloakBrowser context per publisher.[/dim]")
    if speed_mode != "fast" and requested > 1:
        console.print(
            f"[dim]Speed mode '{speed_mode}' keeps one browser context; "
            f"requested --concurrency {requested} is held at 1.[/dim]"
        )
    elif speed_mode == "fast" and requested > effective:
        console.print(
            f"[yellow]Fast mode caps browser workers at {effective} to reduce publisher challenge risk.[/yellow]"
        )


def _print_download_summary(summary: dict, *, include_attempt_cache: bool = False) -> None:
    console.print(
        f"[bold]Done:[/bold] {summary['success']}/{summary['count']} verified PDFs, "
        f"{summary.get('unverified', 0)} unverified PDFs."
    )
    if summary.get("broker_status") == "reauth_required":
        console.print("[yellow]Broker paused for manual institution re-authentication.[/yellow]")
        if summary.get("paused_job_id"):
            console.print(f"[dim]Paused job: {summary['paused_job_id']} ({summary.get('paused_record_count', 0)} records)[/dim]")
        if summary.get("resume_command"):
            console.print(f"[dim]Resume after completing the visible browser prompt: {summary['resume_command']}[/dim]")
        credential_assist = summary.get("credential_assist") if isinstance(summary.get("credential_assist"), dict) else {}
        trigger_command = str(credential_assist.get("trigger_command") or "").strip()
        if trigger_command:
            console.print(f"[dim]KeePassXC Auto-Type assist: {trigger_command}[/dim]")
    if summary.get("pdf_dir"):
        console.print(f"[dim]PDF dir: {summary['pdf_dir']}[/dim]")
    if summary.get("manifest"):
        console.print(f"[dim]Manifest: {summary['manifest']}[/dim]")
    if include_attempt_cache and summary.get("attempt_cache"):
        console.print(f"[dim]Attempt cache: {summary['attempt_cache']}[/dim]")
    if summary.get("human_assist_url"):
        console.print(f"[dim]Human assist: {summary['human_assist_url']}[/dim]")


def _run_papers_group(
    *,
    cfg: Config,
    profile,
    publisher_key: str,
    records,
    run_dir: Path,
    institution: str,
    login_timeout: int,
    pdf_timeout: int,
    post_login_hold: int,
    post_run_hold: int,
    retry_failed: bool,
    concurrency: int,
    broker: bool,
    broker_ttl: int,
    target_verified: int | None = None,
    attempt_cache: str | None = None,
    skip_attempted: bool = False,
    keep_browser_open: bool = False,
    human_assist: bool = False,
    human_assist_host: str = "127.0.0.1",
    human_assist_port: int = 0,
) -> dict:
    from .publisher_batch import PublisherBatchDownloader

    if broker:
        from . import session_broker
        from .browser_identity import browser_extension_hash, browser_proxy_hash

        requested_proxy_hash = browser_proxy_hash(cfg)
        requested_extension_hash = browser_extension_hash(cfg)
        running = session_broker.broker_is_running(publisher_key)
        if running:
            broker_matches, mismatch_reason = session_broker.broker_identity_matches(
                publisher_key,
                profile_dir=cfg.chrome_profile_dir,
                institution=institution,
                browser_proxy_url_hash=requested_proxy_hash,
                browser_extension_hash=requested_extension_hash,
            )
            if not broker_matches:
                console.print(
                    f"[yellow]Existing session broker not reused: {mismatch_reason}. "
                    "Falling back to a one-shot browser workflow.[/yellow]"
                )
                broker = False
                running = False

        if broker and not running:
            console.print(f"[dim]Starting publisher session broker: {publisher_key}[/dim]")
            session_broker.start_broker_process(
                publisher=publisher_key,
                browser_profile=cfg.chrome_profile_dir,
                institution=institution,
                ttl_seconds=broker_ttl,
                cwd=Path.cwd(),
                human_assist=human_assist,
                human_assist_host=human_assist_host,
                human_assist_port=human_assist_port,
            )
            deadline = time.time() + 30
            while time.time() < deadline and not session_broker.broker_is_running(publisher_key):
                time.sleep(1)

        if broker and session_broker.broker_is_running(publisher_key):
            broker_matches, mismatch_reason = session_broker.broker_identity_matches(
                publisher_key,
                profile_dir=cfg.chrome_profile_dir,
                institution=institution,
                browser_proxy_url_hash=requested_proxy_hash,
                browser_extension_hash=requested_extension_hash,
            )
            if not broker_matches:
                console.print(
                    f"[yellow]Session broker started but identity was not reusable: {mismatch_reason}. "
                    "Falling back to a one-shot browser workflow.[/yellow]"
                )
                broker = False
            else:
                console.print(f"[bold]Session broker:[/bold] running ({publisher_key})")
                timeout_seconds = max(
                    120,
                    login_timeout + len(records) * (pdf_timeout + post_login_hold + post_run_hold + 60),
                )
                return session_broker.submit_broker_job(
                    publisher=publisher_key,
                    records=_broker_records(records),
                    output_dir=str(run_dir),
                    institution=institution,
                    login_timeout=login_timeout,
                    pdf_timeout=pdf_timeout,
                    post_login_hold=post_login_hold,
                    post_run_hold=post_run_hold,
                    timeout_seconds=timeout_seconds,
                    retry_failed=retry_failed,
                    target_verified=target_verified,
                    attempt_cache=attempt_cache or "",
                    skip_attempted=skip_attempted,
                )
        if broker:
            console.print("[yellow]Session broker did not start; falling back to one-shot browser workflow.[/yellow]")

    downloader = PublisherBatchDownloader(
        cfg,
        profile=profile,
        institution_query=institution,
        login_timeout_sec=login_timeout,
        pdf_timeout_sec=pdf_timeout,
        post_login_hold_sec=post_login_hold,
        post_run_hold_sec=post_run_hold,
        keep_browser_open=keep_browser_open,
        human_assist=human_assist,
        human_assist_host=human_assist_host,
        human_assist_port=human_assist_port,
    )
    return downloader.run_records(
        records,
        run_dir,
        retry_failed=retry_failed,
        concurrency=concurrency,
        target_verified=target_verified,
        attempt_cache=attempt_cache,
        skip_attempted=skip_attempted,
    )


def _path_status(path_value: str) -> tuple[str, str]:
    if not path_value:
        return "missing", ""
    path = Path(path_value)
    return ("ok" if path.exists() else "missing", str(path))


def _disable_browser_extensions(cfg: Config, *, label: str = "Browser extensions") -> None:
    if cfg.browser_extension_dirs:
        console.print(f"[yellow]{label} disabled for this run:[/yellow] {cfg.browser_extension_dirs}")
    cfg.browser_extension_dirs = ""


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
    from .browser_identity import browser_extension_paths

    extension_paths = browser_extension_paths(cfg)
    if extension_paths:
        missing_extensions = [path for path in extension_paths if not Path(path).is_dir()]
        checks.append((
            "Browser extensions",
            "missing" if missing_extensions else "ok",
            "; ".join(extension_paths),
        ))
    else:
        checks.append(("Browser extensions", "ok", "not configured"))

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


def _installed_package_version(name: str) -> str:
    try:
        return importlib.metadata.version(name)
    except importlib.metadata.PackageNotFoundError:
        return ""


def _run_pip_check() -> tuple[str, str]:
    try:
        result = subprocess.run(
            [sys.executable, "-m", "pip", "check"],
            text=True,
            encoding="utf-8",
            errors="replace",
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=30,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return "warning", f"pip check unavailable: {exc}"

    output = (result.stdout or result.stderr or "").strip()
    if result.returncode == 0:
        return "ok", output or "No dependency conflicts"
    first_line = output.splitlines()[0] if output else f"pip check exited {result.returncode}"
    return "warning", f"dependency conflicts: {first_line}"


def _doctor_checks() -> list[tuple[str, str, str]]:
    checks: list[tuple[str, str, str]] = [
        ("Python runtime", "ok", sys.executable),
        (
            "User install",
            "info",
            "Current: pipx install git+https://github.com/Rimagination/instsci.git; PyPI after release: pipx install instsci",
        ),
    ]

    for command in ("instsci", "instsci-mcp"):
        path = shutil.which(command)
        checks.append((command, "ok" if path else "warning", path or "not found on PATH"))

    for package in ("instsci", "pymupdf", "cloakbrowser"):
        version = _installed_package_version(package)
        checks.append((f"package: {package}", "ok" if version else "warning", version or "not installed"))

    try:
        from .cloakbrowser_compat import configure_builtin_cloakbrowser

        cache_dir = configure_builtin_cloakbrowser(create_dir=False)
        status = "ok" if cache_dir.exists() else "warning"
        detail = str(cache_dir) if cache_dir.exists() else f"not downloaded yet: {cache_dir}"
    except Exception as exc:
        status = "warning"
        detail = f"cache check failed: {exc}"
    checks.append(("CloakBrowser cache", status, detail))

    pip_status, pip_detail = _run_pip_check()
    checks.append(("Dependencies", pip_status, pip_detail))
    return checks


@app.command("doctor")
def doctor():
    """Inspect InstSci runtime, dependencies, command shims, and browser cache."""
    table = Table(title="InstSci Doctor")
    table.add_column("Item", width=24)
    table.add_column("Status", width=10)
    table.add_column("Detail", overflow="fold")

    styles = {"ok": "green", "warning": "yellow", "info": "cyan"}
    for label, status, detail in _doctor_checks():
        style = styles.get(status, "white")
        table.add_row(label, f"[{style}]{status}[/{style}]", detail)

    console.print(table)


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


@app.command("keepassxc-autotype")
def keepassxc_autotype(
    expected_domain: str = typer.Option("", "--expected-domain", help="Expected login domain before Auto-Type is triggered."),
    login_url: str = typer.Option("", "--login-url", help="Optional current/login URL to validate against --expected-domain."),
    hotkey: str = typer.Option("ctrl+alt+a", "--hotkey", help="KeePassXC global Auto-Type hotkey."),
    trigger: bool = typer.Option(False, "--trigger", help="After confirmation, send the KeePassXC Auto-Type hotkey to the focused window."),
    countdown: int = typer.Option(3, "--countdown", min=0, max=30, help="Seconds to wait after confirmation before sending the hotkey."),
):
    """Assist KeePassXC Auto-Type without reading institution credentials."""
    from .keepassxc_autotype import (
        domain_matches,
        format_hotkey,
        normalize_hotkey,
        trigger_keepassxc_autotype,
    )

    try:
        normalized_hotkey = normalize_hotkey(hotkey)
    except ValueError as exc:
        console.print(f"[red]Invalid hotkey:[/red] {exc}")
        raise typer.Exit(1)

    if expected_domain and login_url and not domain_matches(login_url, expected_domain):
        console.print(
            f"[red]Login URL does not match expected domain:[/red] "
            f"{login_url} != {expected_domain}"
        )
        raise typer.Exit(1)

    hotkey_label = format_hotkey(normalized_hotkey)
    canonical_hotkey = "+".join(normalized_hotkey)
    console.print("[bold]KeePassXC Auto-Type assist[/bold]")
    console.print("[dim]InstSci does not read, print, store, or retrieve your credentials.[/dim]")
    if expected_domain:
        console.print(f"  Expected domain: [cyan]{expected_domain}[/cyan]")
    if login_url:
        console.print(f"  Login URL:        [cyan]{login_url}[/cyan]")
    console.print(f"  Auto-Type hotkey: [cyan]{hotkey_label}[/cyan]")
    console.print()
    console.print("Before triggering:")
    console.print("  1. Verify the visible browser URL is the intended institution login domain.")
    console.print("  2. Unlock KeePassXC and make sure the matching entry has an Auto-Type sequence.")
    console.print("  3. Focus the username field in the visible CloakBrowser window.")

    if not trigger:
        console.print()
        console.print("[dim]Run again with --trigger when the browser is focused and you are ready.[/dim]")
        return

    if not typer.confirm(f"Send {hotkey_label} to the currently focused window now?", default=False):
        console.print("[yellow]Auto-Type trigger cancelled.[/yellow]")
        raise typer.Exit(1)

    for remaining in range(countdown, 0, -1):
        console.print(f"[dim]Sending hotkey in {remaining}...[/dim]")
        time.sleep(1)

    try:
        trigger_keepassxc_autotype(canonical_hotkey)
    except Exception as exc:
        console.print(f"[red]Failed to send KeePassXC Auto-Type hotkey:[/red] {exc}")
        raise typer.Exit(1)
    console.print("[green]Auto-Type hotkey sent.[/green]")


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

    dois = _read_doi_lines(file)

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
    login_timeout: int = typer.Option(DEFAULT_LOGIN_TIMEOUT_SECONDS, "--login-timeout", help="Seconds to wait for manual SSO/2FA completion."),
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
    if summary.get("human_assist_url"):
        console.print(f"[dim]Human assist: {summary['human_assist_url']}[/dim]")
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
    login_timeout: int = typer.Option(DEFAULT_LOGIN_TIMEOUT_SECONDS, "--login-timeout", help="Seconds to wait for manual SSO/2FA completion."),
    pdf_timeout: int = typer.Option(60, "--pdf-timeout", help="Seconds to wait for each candidate PDF navigation."),
    post_login_hold: int = typer.Option(0, "--post-login-hold", help="Seconds to keep the authorized article page open before PDF capture."),
    post_run_hold: int = typer.Option(0, "--post-run-hold", help="Seconds to keep the browser page open after capture or failure."),
    speed: str = typer.Option(DEFAULT_BROWSER_SPEED, "--speed", help="Speed preset: careful/balanced reuse one browser context; fast permits up to 2 workers."),
    concurrency: int = typer.Option(1, "--concurrency", "-j", min=1, max=4, help="Requested browser workers. Only --speed fast uses more than one context."),
    broker: bool = typer.Option(True, "--broker/--no-broker", help="Use the long-lived publisher session broker by default."),
    broker_ttl: int = typer.Option(DEFAULT_BROKER_TTL_SECONDS, "--broker-ttl", help="Seconds to keep an auto-started broker alive."),
    overnight: bool = typer.Option(False, "--overnight", help="Compatibility flag: long-lived broker defaults are already enabled; also forces careful single-context settings."),
    keep_browser_open: bool = typer.Option(False, "--keep-browser-open", help="In --no-broker mode, keep the one-shot CloakBrowser open until Ctrl+C."),
    target_verified: int = typer.Option(0, "--target-verified", help="Stop after this many verified PDFs. Zero disables early stop."),
    attempt_cache: str = typer.Option("", "--attempt-cache", help="JSONL attempt cache path. Defaults to attempts.jsonl in the run directory."),
    skip_attempted: bool = typer.Option(False, "--skip-attempted", help="Skip DOIs already present in the attempt cache."),
    human_assist: bool = typer.Option(True, "--human-assist/--no-human-assist", help="Expose a local status page while waiting for manual CAPTCHA/SSO checks."),
    human_assist_host: str = typer.Option("127.0.0.1", "--human-assist-host", help="Host for the human-assist status page. Use 0.0.0.0 only on trusted LANs."),
    human_assist_port: int = typer.Option(0, "--human-assist-port", help="Port for the human-assist status page. Zero picks a free port."),
    disable_browser_extensions: bool = typer.Option(False, "--disable-browser-extensions", help="Temporarily run CloakBrowser without configured extensions, useful for OpenCLI Bridge A/B tests."),
):
    """Download a DOI list through a named publisher profile and CloakBrowser."""
    from .publisher_batch import PaperRecord, PublisherBatchDownloader
    from .publisher_profiles import get_publisher_profile

    if not file.exists():
        console.print(f"[red]File not found: {file}[/red]")
        raise typer.Exit(1)

    records = [PaperRecord(doi=doi) for doi in _read_doi_lines(file)]
    if not records:
        console.print("[yellow]No DOIs found in file.[/yellow]")
        raise typer.Exit(0)

    try:
        profile = get_publisher_profile(publisher)
    except ValueError as exc:
        console.print(f"[red]{exc}[/red]")
        raise typer.Exit(1) from exc

    try:
        speed_mode = _normalize_speed_mode(speed)
    except ValueError as exc:
        console.print(f"[red]{exc}[/red]")
        raise typer.Exit(1) from exc
    if overnight:
        login_timeout, broker, broker_ttl, speed_mode, concurrency = _apply_overnight_mode(
            login_timeout=login_timeout,
            broker=broker,
            broker_ttl=broker_ttl,
            speed_mode=speed_mode,
            concurrency=concurrency,
        )
    effective_concurrency = _effective_browser_concurrency(speed_mode, concurrency)

    cfg = Config.load()
    if browser_profile:
        cfg.chrome_profile_dir = browser_profile
    if disable_browser_extensions:
        _disable_browser_extensions(cfg)
        if broker:
            console.print("[yellow]Session broker disabled so the extension-off A/B run cannot reload extensions from global config.[/yellow]")
            broker = False
        if overnight:
            console.print("[yellow]Overnight mode disabled for this extension-off one-shot run.[/yellow]")
            overnight = False
    institution = _resolve_subscription_institution(cfg, institution)
    profile_key = _publisher_profile_key(profile)
    run_dir = Path(output) if output else Path("downloads") / f"{profile_key}_{len(records)}" / f"cloak_{datetime.now():%Y%m%d_%H%M%S}"
    if keep_browser_open and broker:
        console.print("[dim]Session broker already keeps the publisher CloakBrowser alive after this command returns.[/dim]")
        keep_browser_open = False
    if keep_browser_open and effective_concurrency > 1:
        console.print("[yellow]--keep-browser-open keeps a single one-shot browser context; browser workers capped at 1.[/yellow]")
        effective_concurrency = 1
    console.print(f"[bold]Publisher profile:[/bold] {profile.name}")
    console.print(f"[bold]Found {len(records)} DOI records.[/bold]")
    console.print(f"[bold]Output:[/bold] {run_dir}")
    console.print(f"[bold]Browser profile:[/bold] {cfg.chrome_profile_dir}")
    console.print(f"[bold]Browser extensions:[/bold] {cfg.browser_extension_dirs or '(disabled/not configured)'}")
    console.print(f"[bold]Speed:[/bold] {speed_mode} (browser workers: {effective_concurrency})")
    if overnight:
        _print_overnight_notice(login_timeout, broker_ttl)
    _print_speed_notice(speed_mode, concurrency, effective_concurrency, broker=broker)
    console.print("[dim]If a CloakBrowser window stops on SSO or 2FA, complete it there and leave the window open.[/dim]")

    summary = _run_papers_group(
        cfg=cfg,
        profile=profile,
        publisher_key=profile_key,
        records=records,
        run_dir=run_dir,
        institution=institution,
        login_timeout=login_timeout,
        pdf_timeout=pdf_timeout,
        post_login_hold=post_login_hold,
        post_run_hold=post_run_hold,
        retry_failed=retry_failed,
        concurrency=effective_concurrency,
        broker=broker,
        broker_ttl=broker_ttl,
        target_verified=target_verified or None,
        attempt_cache=attempt_cache or None,
        skip_attempted=skip_attempted,
        keep_browser_open=keep_browser_open,
        human_assist=human_assist,
        human_assist_host=human_assist_host,
        human_assist_port=human_assist_port,
    )
    _print_download_summary(summary, include_attempt_cache=True)
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
    login_timeout: int = typer.Option(DEFAULT_LOGIN_TIMEOUT_SECONDS, "--login-timeout", help="Seconds to wait for manual SSO/CAPTCHA completion."),
    pdf_timeout: int = typer.Option(90, "--pdf-timeout", help="Seconds to wait for each PDF navigation."),
    post_login_hold: int = typer.Option(0, "--post-login-hold", help="Seconds to keep the authorized article page open before PDF capture."),
    post_run_hold: int = typer.Option(0, "--post-run-hold", help="Seconds to keep the browser page open after capture or failure."),
    retry_failed: bool = typer.Option(True, "--retry/--no-retry", help="Retry transient failures in a fresh browser context."),
    speed: str = typer.Option(DEFAULT_BROWSER_SPEED, "--speed", help="Speed preset: careful/balanced reuse one broker browser; fast permits up to 2 fallback workers."),
    concurrency: int = typer.Option(1, "--concurrency", "-j", min=1, max=4, help="Requested browser workers. Broker mode stays single-context; only --speed fast uses more than one fallback worker."),
    broker: bool = typer.Option(True, "--broker/--no-broker", help="Use the long-lived publisher session broker by default."),
    broker_ttl: int = typer.Option(DEFAULT_BROKER_TTL_SECONDS, "--broker-ttl", help="Seconds to keep an auto-started broker alive."),
    overnight: bool = typer.Option(False, "--overnight", help="Compatibility flag: long-lived broker defaults are already enabled; also forces careful single-context settings."),
    human_assist: bool = typer.Option(True, "--human-assist/--no-human-assist", help="Expose a local status page while waiting for manual CAPTCHA/SSO checks."),
    human_assist_host: str = typer.Option("127.0.0.1", "--human-assist-host", help="Host for the human-assist status page. Use 0.0.0.0 only on trusted LANs."),
    human_assist_port: int = typer.Option(0, "--human-assist-port", help="Port for the human-assist status page. Zero picks a free port."),
    disable_browser_extensions: bool = typer.Option(False, "--disable-browser-extensions", help="Temporarily run one-shot CloakBrowser without configured extensions, useful for OpenCLI Bridge A/B tests."),
):
    """Recommended browser workflow for closed-access publisher PDFs."""
    from .publisher_batch import PaperRecord

    if not file.exists():
        console.print(f"[red]File not found: {file}[/red]")
        raise typer.Exit(1)

    records = [PaperRecord(doi=doi) for doi in _read_doi_lines(file)]
    if not records:
        console.print("[yellow]No DOIs found in file.[/yellow]")
        raise typer.Exit(0)

    try:
        speed_mode = _normalize_speed_mode(speed)
    except ValueError as exc:
        console.print(f"[red]{exc}[/red]")
        raise typer.Exit(1) from exc
    if overnight:
        login_timeout, broker, broker_ttl, speed_mode, concurrency = _apply_overnight_mode(
            login_timeout=login_timeout,
            broker=broker,
            broker_ttl=broker_ttl,
            speed_mode=speed_mode,
            concurrency=concurrency,
        )
    effective_concurrency = _effective_browser_concurrency(speed_mode, concurrency)

    try:
        publisher_groups = _group_records_by_publisher(records, publisher)
    except ValueError as exc:
        console.print(f"[red]{exc}[/red]")
        raise typer.Exit(1) from exc

    cfg = Config.load()
    if browser_profile:
        cfg.chrome_profile_dir = browser_profile
    if disable_browser_extensions:
        _disable_browser_extensions(cfg)
        if broker:
            console.print("[yellow]Session broker disabled so the extension-off A/B run cannot reload extensions from global config.[/yellow]")
            broker = False
        if overnight:
            console.print("[yellow]Overnight mode disabled for this extension-off one-shot run.[/yellow]")
            overnight = False
    institution = _resolve_subscription_institution(cfg, institution)

    multi_publisher = len(publisher_groups) > 1
    if multi_publisher:
        run_dir = Path(output) if output else Path("downloads") / f"papers_auto_{len(records)}" / f"browser_{datetime.now():%Y%m%d_%H%M%S}"
    else:
        profile_key, _profile, _group_records = publisher_groups[0]
        run_dir = Path(output) if output else Path("downloads") / f"papers_{profile_key}_{len(records)}" / f"browser_{datetime.now():%Y%m%d_%H%M%S}"

    console.print("[bold]Recommended route:[/bold] browser-based publisher workflow")
    console.print("[dim]Complete SSO, 2FA, or CAPTCHA in the opened browser window; InstSci continues automatically.[/dim]")
    console.print(f"[bold]Found {len(records)} DOI records.[/bold]")
    if multi_publisher:
        group_labels = ", ".join(f"{profile.name}={len(group_records)}" for _key, profile, group_records in publisher_groups)
        console.print(f"[bold]Publisher groups:[/bold] {group_labels}")
    console.print(f"[bold]Output:[/bold] {run_dir}")
    console.print(f"[bold]Browser profile:[/bold] {cfg.chrome_profile_dir}")
    console.print(f"[bold]Browser extensions:[/bold] {cfg.browser_extension_dirs or '(disabled/not configured)'}")
    console.print(f"[bold]Speed:[/bold] {speed_mode} (fallback browser workers: {effective_concurrency})")
    if overnight:
        _print_overnight_notice(login_timeout, broker_ttl)
    _print_speed_notice(speed_mode, concurrency, effective_concurrency, broker=broker)

    group_summaries: list[tuple[str, str, Path, dict]] = []
    for publisher_key, profile, group_records in publisher_groups:
        group_run_dir = run_dir / publisher_key if multi_publisher else run_dir
        if multi_publisher:
            console.print(f"[bold]Publisher group:[/bold] {profile.name} ({len(group_records)} DOI records)")
        else:
            console.print(f"[bold]Publisher profile:[/bold] {profile.name}")
        summary = _run_papers_group(
            cfg=cfg,
            profile=profile,
            publisher_key=publisher_key,
            records=group_records,
            run_dir=group_run_dir,
            institution=institution,
            login_timeout=login_timeout,
            pdf_timeout=pdf_timeout,
            post_login_hold=post_login_hold,
            post_run_hold=post_run_hold,
            retry_failed=retry_failed,
            concurrency=effective_concurrency,
            broker=broker,
            broker_ttl=broker_ttl,
            human_assist=human_assist,
            human_assist_host=human_assist_host,
            human_assist_port=human_assist_port,
        )
        group_summaries.append((publisher_key, profile.name, group_run_dir, summary))
        _print_download_summary(summary)

    if multi_publisher:
        aggregate = {
            "publisher": "auto",
            "grouped_by_publisher": True,
            "speed": speed_mode,
            "concurrency": effective_concurrency,
            "browser_profile_dir": cfg.chrome_profile_dir,
            "count": sum(int(summary.get("count", 0)) for _key, _name, _dir, summary in group_summaries),
            "success": sum(int(summary.get("success", 0)) for _key, _name, _dir, summary in group_summaries),
            "missing": sum(int(summary.get("missing", 0)) for _key, _name, _dir, summary in group_summaries),
            "unverified": sum(int(summary.get("unverified", 0)) for _key, _name, _dir, summary in group_summaries),
            "groups": [
                {
                    "publisher_key": publisher_key,
                    "publisher": publisher_name,
                    "output_dir": str(group_dir),
                    "summary": str(group_dir / "summary.json"),
                    "count": summary.get("count", 0),
                    "success": summary.get("success", 0),
                    "missing": summary.get("missing", 0),
                    "unverified": summary.get("unverified", 0),
                }
                for publisher_key, publisher_name, group_dir, summary in group_summaries
            ],
        }
        run_dir.mkdir(parents=True, exist_ok=True)
        aggregate_path = run_dir / "summary.json"
        aggregate_path.write_text(json.dumps(aggregate, ensure_ascii=False, indent=2), encoding="utf-8")
        console.print(f"[dim]Aggregate summary: {aggregate_path}[/dim]")

    missing_total = sum(int(summary.get("missing", 0)) for _key, _name, _dir, summary in group_summaries)
    unverified_total = sum(int(summary.get("unverified", 0)) for _key, _name, _dir, summary in group_summaries)
    if missing_total or unverified_total:
        console.print("[yellow]Some items need manual CAPTCHA/login attention; rerun the same command after completing it.[/yellow]")
        raise typer.Exit(2)


@app.command("session-broker-status")
def session_broker_status(
    publisher: str = typer.Option("elsevier", "--publisher", "-p", help="Publisher broker key."),
    json_output: bool = typer.Option(False, "--json", help="Print broker inventory as JSON."),
):
    """Show a long-lived publisher browser session broker."""
    from . import session_broker

    state = session_broker.load_broker_state(publisher)
    running = session_broker.broker_is_running(publisher)
    queued_jobs = session_broker.list_queued_jobs(publisher)
    paused_jobs = session_broker.list_paused_jobs(publisher)
    if json_output:
        payload = _session_broker_status_payload(
            publisher=publisher,
            state=state,
            running=running,
            queued_jobs=queued_jobs,
            paused_jobs=paused_jobs,
        )
        console.print_json(json.dumps(payload, ensure_ascii=False))
        return

    table = Table(title="InstSci Session Broker")
    table.add_column("Publisher")
    table.add_column("Status")
    table.add_column("Broker state")
    table.add_column("PID")
    table.add_column("Institution", overflow="fold")
    table.add_column("Profile", overflow="fold")
    table.add_column("Browser proxy", overflow="fold")
    table.add_column("Extensions", overflow="fold")
    table.add_column("Active job", overflow="fold")
    table.add_column("Paused job", overflow="fold")
    table.add_column("Health", overflow="fold")
    table.add_column("Last job", overflow="fold")
    table.add_column("Queue", overflow="fold")
    extension_label = ""
    broker_status = str(state.get("status", "") or "") if state else ""
    active_job = ""
    paused_job = ""
    health = ""
    last_job = ""
    if state:
        extension_hash = str(state.get("browser_extension_hash", "") or "")
        try:
            extension_count = int(state.get("browser_extension_count") or 0)
        except (TypeError, ValueError):
            extension_count = 0
        extension_label = "none" if not extension_count and not extension_hash else str(extension_count)
        if extension_hash:
            extension_label = f"{extension_label} ({extension_hash[:12]})"
        active_job_id = str(state.get("active_job_id", "") or "")
        active_count = str(state.get("active_record_count", "") or "")
        active_job = f"{active_job_id} ({active_count})" if active_job_id else ""
        paused_job_id = str(state.get("paused_job_id", "") or "")
        paused_count = str(state.get("paused_record_count", "") or "")
        paused_job = f"{paused_job_id} ({paused_count})" if paused_job_id else ""
        last_health_status = str(state.get("last_health_status", "") or "")
        last_health_at = str(state.get("last_health_at", "") or "")
        health = f"{last_health_status} ({last_health_at})" if last_health_status else ""
        last_job_id = str(state.get("last_job_id", "") or "")
        last_job_status = str(state.get("last_job_status", "") or "")
        last_job = f"{last_job_id} ({last_job_status})" if last_job_id else ""
    table.add_row(
        publisher,
        "running" if running else "stopped",
        broker_status,
        str(state.get("pid", "")) if state else "",
        str(state.get("institution", "")) if state else "",
        str(state.get("profile_dir", "")) if state else "",
        str(state.get("browser_proxy_url", "")) if state else "",
        extension_label,
        active_job,
        paused_job,
        health,
        last_job,
        str(state.get("queue_dir", "")) if state else "",
    )
    console.print(table)
    console.print(f"[dim]Process status: {'running' if running else 'stopped'}[/dim]")
    if state and state.get("institution"):
        console.print(f"[dim]Institution: {state['institution']}[/dim]")
    if state and state.get("browser_proxy_url"):
        console.print(f"[dim]Browser proxy: {state['browser_proxy_url']}[/dim]")
    if state and extension_label and extension_label != "none":
        console.print(f"[dim]Browser extensions: {extension_label}[/dim]")
    if state and state.get("human_assist_url"):
        console.print(f"[dim]Human assist: {state['human_assist_url']}[/dim]")
    if state and state.get("status"):
        console.print(f"[dim]Broker state: {state['status']}[/dim]")
    if state and state.get("active_job_id"):
        console.print(
            f"[dim]Active job: {state['active_job_id']} "
            f"({state.get('active_record_count') or 0} records)[/dim]"
        )
    if state and state.get("active_output_dir"):
        console.print(f"[dim]Active output: {state['active_output_dir']}[/dim]")
    if state and state.get("paused_job_id"):
        console.print(
            f"[dim]Paused job: {state['paused_job_id']} "
            f"({state.get('paused_record_count') or 0} records)[/dim]"
        )
        console.print(
            f"[dim]Resume: instsci session-broker-resume -p {publisher} "
            f"--job-id {state['paused_job_id']}[/dim]"
        )
    if state and state.get("paused_job_path"):
        console.print(f"[dim]Paused job file: {state['paused_job_path']}[/dim]")
    if queued_jobs:
        console.print(f"[dim]Queued jobs: {len(queued_jobs)}[/dim]")
        for queued in queued_jobs[:10]:
            console.print(
                f"[dim]  - {queued['job_id']} "
                f"({queued.get('record_count') or 0} records) "
                f"{queued.get('created_at', '')} {queued.get('output_dir', '')}[/dim]"
            )
    if paused_jobs:
        console.print(f"[dim]Paused jobs: {len(paused_jobs)}[/dim]")
        for paused in paused_jobs[:10]:
            console.print(
                f"[dim]  - {paused['job_id']} "
                f"({paused.get('record_count') or 0} records) "
                f"{paused.get('paused_at', '')} {paused.get('pause_reason', '')}[/dim]"
            )
    if state and state.get("last_health_status"):
        console.print(
            f"[dim]Last health: {state['last_health_status']} "
            f"at {state.get('last_health_at', '')}[/dim]"
        )
    if state and state.get("last_health_url"):
        console.print(f"[dim]Health URL: {state['last_health_url']}[/dim]")
    if state and state.get("last_health_error"):
        console.print(f"[dim]Health detail: {state['last_health_error']}[/dim]")
    if state and state.get("keepalive_interval_seconds"):
        console.print(f"[dim]Keepalive interval: {state['keepalive_interval_seconds']}s[/dim]")
    if state and state.get("last_job_id"):
        console.print(f"[dim]Last job: {state['last_job_id']} ({state.get('last_job_status', '')})[/dim]")
    if state and state.get("last_summary_path"):
        console.print(f"[dim]Last summary: {state['last_summary_path']}[/dim]")
    if state and state.get("last_error"):
        console.print(f"[yellow]Last attention: {state['last_error']}[/yellow]")


def _session_broker_status_payload(
    *,
    publisher: str,
    state: dict | None,
    running: bool,
    queued_jobs: list[dict],
    paused_jobs: list[dict],
) -> dict:
    state = state or {}
    paused_job_id = str(state.get("paused_job_id") or "")
    resume_command = (
        f"instsci session-broker-resume -p {publisher} --job-id {paused_job_id}"
        if paused_job_id
        else ""
    )
    return {
        "publisher": publisher,
        "running": bool(running),
        "status": str(state.get("status") or ""),
        "pid": int(state.get("pid") or 0),
        "started_at": str(state.get("started_at") or ""),
        "ttl_seconds": int(state.get("ttl_seconds") or 0),
        "institution": str(state.get("institution") or ""),
        "profile_dir": str(state.get("profile_dir") or ""),
        "browser_proxy": str(state.get("browser_proxy_url") or ""),
        "browser_proxy_hash": str(state.get("browser_proxy_url_hash") or ""),
        "browser_extensions": {
            "count": int(state.get("browser_extension_count") or 0),
            "hash": str(state.get("browser_extension_hash") or ""),
        },
        "human_assist_url": str(state.get("human_assist_url") or ""),
        "active_job": {
            "job_id": str(state.get("active_job_id") or ""),
            "output_dir": str(state.get("active_output_dir") or ""),
            "record_count": int(state.get("active_record_count") or 0),
        },
        "paused_job": {
            "job_id": paused_job_id,
            "path": str(state.get("paused_job_path") or ""),
            "record_count": int(state.get("paused_record_count") or 0),
        },
        "queued_jobs": queued_jobs,
        "paused_jobs": paused_jobs,
        "resume_command": resume_command,
        "health": {
            "status": str(state.get("last_health_status") or ""),
            "checked_at": str(state.get("last_health_at") or ""),
            "url": str(state.get("last_health_url") or ""),
            "reason": str(state.get("last_health_error") or ""),
            "keepalive_interval_seconds": int(state.get("keepalive_interval_seconds") or 0),
        },
        "last_job": {
            "job_id": str(state.get("last_job_id") or ""),
            "status": str(state.get("last_job_status") or ""),
            "summary_path": str(state.get("last_summary_path") or ""),
            "attention": str(state.get("last_error") or ""),
        },
        "queue_dir": str(state.get("queue_dir") or ""),
    }


@app.command("session-broker-stop")
def session_broker_stop(
    publisher: str = typer.Option("elsevier", "--publisher", "-p", help="Publisher broker key."),
):
    """Ask a long-lived publisher broker to stop."""
    from . import session_broker

    session_broker.broker_stop_path(publisher).parent.mkdir(parents=True, exist_ok=True)
    session_broker.broker_stop_path(publisher).write_text("stop", encoding="utf-8")
    console.print(f"[green]Stop requested for broker:[/green] {publisher}")


@app.command("session-broker-resume")
def session_broker_resume(
    publisher: str = typer.Option("elsevier", "--publisher", "-p", help="Publisher broker key."),
    job_id: str = typer.Option("", "--job-id", help="Paused broker job id. Omit to resume the newest paused job."),
    resume_all: bool = typer.Option(False, "--all/--latest", help="Resume all paused jobs for this publisher."),
    timeout: int = typer.Option(DEFAULT_LOGIN_TIMEOUT_SECONDS, "--timeout", help="Seconds to wait for the resumed broker job."),
):
    """Resume a broker job paused for manual institution re-authentication."""
    from . import session_broker

    try:
        if resume_all:
            summary = session_broker.resume_all_broker_jobs(
                publisher=publisher,
                timeout_seconds=timeout,
            )
        else:
            summary = session_broker.resume_broker_job(
                publisher=publisher,
                job_id=job_id,
                timeout_seconds=timeout,
            )
    except Exception as exc:
        console.print(f"[red]Could not resume broker job:[/red] {type(exc).__name__}: {exc}")
        raise typer.Exit(1) from exc

    console.print(f"[green]Session broker job resumed:[/green] {publisher}")
    if summary.get("resumed_job_count"):
        console.print(f"[dim]Resumed {summary['resumed_job_count']} paused jobs.[/dim]")
    _print_download_summary(summary, include_attempt_cache=True)
    if summary.get("missing") or summary.get("unverified", 0):
        console.print("[yellow]Some items still need manual attention; keep CloakBrowser open and resume again after completing the prompt.[/yellow]")
        raise typer.Exit(2)


@app.command("session-broker-run", hidden=True)
def session_broker_run(
    publisher: str = typer.Option(..., "--publisher", "-p"),
    browser_profile: str = typer.Option("", "--browser-profile"),
    institution: str = typer.Option("", "--institution"),
    ttl: int = typer.Option(DEFAULT_BROKER_TTL_SECONDS, "--ttl"),
    keepalive_interval: int = typer.Option(DEFAULT_KEEPALIVE_INTERVAL_SECONDS, "--keepalive-interval"),
    human_assist: bool = typer.Option(False, "--human-assist"),
    human_assist_host: str = typer.Option("127.0.0.1", "--human-assist-host"),
    human_assist_port: int = typer.Option(0, "--human-assist-port"),
):
    """Run the long-lived broker loop. Internal command."""
    from .publisher_batch import PaperRecord, PublisherBatchDownloader
    from .publisher_profiles import get_publisher_profile
    from .session_broker import (
        BrokerState,
        broker_dir,
        broker_stop_path,
        broker_summary_status,
        pause_broker_job,
        write_broker_state,
    )

    cfg = Config.load()
    if browser_profile:
        cfg.chrome_profile_dir = browser_profile
    institution = _resolve_subscription_institution(cfg, institution, prompt=False)
    profile = get_publisher_profile(publisher)
    from .browser_identity import (
        browser_extension_hash,
        browser_extension_paths,
        browser_proxy_hash,
        mask_secret_url,
    )
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
        institution=institution,
        heartbeat_at=datetime.now().isoformat(timespec="seconds"),
        browser_proxy_url=mask_secret_url(cfg.browser_proxy_url),
        browser_proxy_url_hash=browser_proxy_hash(cfg),
        browser_extension_count=len(browser_extension_paths(cfg)),
        browser_extension_hash=browser_extension_hash(cfg),
        keepalive_interval_seconds=max(0, int(keepalive_interval or 0)),
    )
    write_broker_state(state)
    downloader = PublisherBatchDownloader(
        cfg,
        profile=profile,
        institution_query=institution,
        login_timeout_sec=DEFAULT_LOGIN_TIMEOUT_SECONDS,
        pdf_timeout_sec=90,
        human_assist=human_assist,
        human_assist_host=human_assist_host,
        human_assist_port=human_assist_port,
    )
    downloader._start_human_assist(root)
    if downloader.human_assist_url:
        state.human_assist_url = downloader.human_assist_url
        write_broker_state(state)
    context = downloader._launch_context()
    state.status = "idle"
    write_broker_state(state)
    deadline = time.time() + max(1, ttl)
    next_keepalive_at = time.time() + max(0, int(keepalive_interval or 0))
    try:
        while time.time() < deadline and not broker_stop_path(publisher).exists():
            state.heartbeat_at = datetime.now().isoformat(timespec="seconds")
            write_broker_state(state)
            jobs = sorted(queue_dir.glob("*.json"))
            processed_job = False
            for job_path in jobs:
                if job_path.name.endswith(".done.json"):
                    continue
                processed_job = True
                try:
                    job = json.loads(job_path.read_text(encoding="utf-8"))
                    run_dir = Path(str(job["output_dir"]))
                    records_payload = job.get("records", [])
                    state.status = "processing"
                    state.active_job_id = str(job.get("id") or job_path.stem)
                    state.active_output_dir = str(run_dir)
                    state.active_record_count = len(records_payload) if isinstance(records_payload, list) else 0
                    state.last_error = ""
                    state.heartbeat_at = datetime.now().isoformat(timespec="seconds")
                    write_broker_state(state)
                    job_downloader = PublisherBatchDownloader(
                        cfg,
                        profile=profile,
                        institution_query=str(job.get("institution") or institution),
                        login_timeout_sec=int(job.get("login_timeout") or DEFAULT_LOGIN_TIMEOUT_SECONDS),
                        pdf_timeout_sec=int(job.get("pdf_timeout") or 90),
                        post_login_hold_sec=int(job.get("post_login_hold") or 0),
                        post_run_hold_sec=int(job.get("post_run_hold") or 0),
                    )
                    job_downloader.share_human_assist_from(downloader)
                    records = [PaperRecord(**record) for record in records_payload]
                    target_verified = int(job.get("target_verified") or 0)
                    summary = job_downloader.run_records_in_context(
                        context,
                        records,
                        run_dir,
                        retry_failed=bool(job.get("retry_failed", True)),
                        target_verified=target_verified or None,
                        attempt_cache=str(job.get("attempt_cache") or "") or None,
                        skip_attempted=bool(job.get("skip_attempted") or False),
                    )
                    summary["broker"] = True
                    summary["browser_profile_dir"] = cfg.chrome_profile_dir
                    job_downloader._add_browser_extension_summary(summary)
                    if job_downloader.human_assist_url:
                        summary["human_assist_url"] = job_downloader.human_assist_url
                    job_status = broker_summary_status(summary)
                    summary["broker_status"] = job_status
                    if job_status == "reauth_required":
                        paused = pause_broker_job(publisher, job, summary)
                        summary["paused_job_id"] = paused["job_id"]
                        summary["paused_job_path"] = paused["path"]
                        summary["paused_record_count"] = paused["record_count"]
                        summary["resume_command"] = (
                            f"instsci session-broker-resume -p {publisher} "
                            f"--job-id {paused['job_id']}"
                        )
                    summary_path = run_dir / "summary.json"
                    summary_path.write_text(
                        json.dumps(summary, ensure_ascii=False, indent=2),
                        encoding="utf-8",
                    )
                    if job_status == "reauth_required" and summary.get("resume_command"):
                        job_downloader.publish_resume_handoff(
                            run_dir,
                            status="reauth_required",
                            resume_command=str(summary.get("resume_command") or ""),
                            paused_job_id=str(summary.get("paused_job_id") or ""),
                            paused_record_count=int(summary.get("paused_record_count") or 0),
                            summary_path=str(summary_path),
                        )
                    state.status = "idle" if job_status == "complete" else job_status
                    state.active_job_id = ""
                    state.active_output_dir = ""
                    state.active_record_count = 0
                    state.last_job_id = str(job.get("id") or job_path.stem)
                    state.last_job_status = job_status
                    state.last_summary_path = str(summary_path)
                    state.last_error = _broker_attention_message(summary) if job_status != "complete" else ""
                    if job_status == "reauth_required":
                        state.paused_job_id = str(summary.get("paused_job_id") or "")
                        state.paused_job_path = str(summary.get("paused_job_path") or "")
                        state.paused_record_count = int(summary.get("paused_record_count") or 0)
                    elif job_status == "complete":
                        state.paused_job_id = ""
                        state.paused_job_path = ""
                        state.paused_record_count = 0
                    state.heartbeat_at = datetime.now().isoformat(timespec="seconds")
                    write_broker_state(state)
                    (queue_dir / f"{job['id']}.done.json").write_text(
                        json.dumps(summary, ensure_ascii=False, indent=2),
                        encoding="utf-8",
                    )
                except Exception as exc:
                    payload = {"count": 0, "success": 0, "missing": 1, "unverified": 0, "error": f"{type(exc).__name__}: {exc}"}
                    state.status = "error"
                    state.active_job_id = ""
                    state.active_output_dir = ""
                    state.active_record_count = 0
                    state.last_job_id = job_path.stem
                    state.last_job_status = "error"
                    state.last_error = str(payload["error"])
                    state.heartbeat_at = datetime.now().isoformat(timespec="seconds")
                    write_broker_state(state)
                    done_name = f"{job_path.stem}.done.json"
                    (queue_dir / done_name).write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
                finally:
                    job_path.unlink(missing_ok=True)
            if (
                not processed_job
                and keepalive_interval
                and keepalive_interval > 0
                and time.time() >= next_keepalive_at
            ):
                health = downloader.check_session_health(context)
                state.last_health_status = str(health.get("status") or "")
                state.last_health_at = str(health.get("checked_at") or datetime.now().isoformat(timespec="seconds"))
                state.last_health_url = str(health.get("url") or "")
                state.last_health_error = str(health.get("reason") or "")
                state.heartbeat_at = datetime.now().isoformat(timespec="seconds")
                if state.last_health_status == "reauth_required":
                    state.status = "reauth_required"
                    state.last_error = (
                        "Publisher session health check requires manual re-authentication. "
                        "Complete the visible CloakBrowser prompt before submitting more work."
                    )
                    downloader.publish_resume_handoff(
                        root,
                        status="reauth_required",
                        resume_command=f"instsci session-broker-status -p {publisher}",
                        summary_path=str(root / "state.json"),
                    )
                elif state.status == "reauth_required" and not state.paused_job_id:
                    state.status = "idle"
                    state.last_error = ""
                write_broker_state(state)
                next_keepalive_at = time.time() + max(1, int(keepalive_interval))
            time.sleep(2)
    finally:
        try:
            context.close()
        except Exception:
            pass


def _broker_attention_message(summary: dict) -> str:
    if summary.get("error"):
        return str(summary.get("error") or "")
    missing = int(summary.get("missing") or 0)
    unverified = int(summary.get("unverified") or 0)
    if summary.get("broker_status") == "reauth_required":
        resume = str(summary.get("resume_command") or "").strip()
        suffix = f" Then run: {resume}" if resume else " Then run instsci session-broker-resume for this publisher."
        return (
            f"{missing} missing and {unverified} unverified PDFs after an institution/CAPTCHA/login checkpoint. "
            f"Complete the visible CloakBrowser prompt and leave the browser open.{suffix}"
        )
    if missing or unverified:
        return (
            f"{missing} missing and {unverified} unverified PDFs. "
            "Complete any visible SSO/CAPTCHA/institution prompt in CloakBrowser, then rerun with the same profile."
        )
    return ""


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
    set_browser_proxy_url: str = typer.Option("", "--browser-proxy-url", help="Set CloakBrowser-only static proxy URL for publisher workflows."),
    set_browser_extension_dirs: str = typer.Option("", "--browser-extension-dirs", help="Set semicolon-separated unpacked Chrome extension dirs for CloakBrowser."),
    set_opencli_extension_dir: str = typer.Option("", "--opencli-extension-dir", help="Set OpenCLI Browser Bridge unpacked extension dir for CloakBrowser."),
    set_browser_challenge_mode: str = typer.Option("", "--browser-challenge-mode", help="Set challenge handling mode: manual or assist."),
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

    if set_browser_proxy_url:
        from .browser_identity import mask_secret_url

        cfg.browser_proxy_url = set_browser_proxy_url
        changed = True
        console.print(f"[green]Browser proxy URL set to: {mask_secret_url(set_browser_proxy_url)}[/green]")

    extension_dirs = set_browser_extension_dirs
    if set_opencli_extension_dir:
        extension_dirs = (
            f"{set_opencli_extension_dir};{extension_dirs}"
            if extension_dirs
            else set_opencli_extension_dir
        )
    if extension_dirs:
        cfg.browser_extension_dirs = extension_dirs
        changed = True
        console.print(f"[green]Browser extension dirs set to: {extension_dirs}[/green]")

    if set_browser_challenge_mode:
        challenge_mode = set_browser_challenge_mode.strip().lower()
        if challenge_mode not in {"manual", "assist"}:
            console.print("[red]Supported browser challenge modes: manual, assist. CAPTCHA solving/bypass is not built in.[/red]")
            raise typer.Exit(1)
        cfg.browser_challenge_mode = challenge_mode
        changed = True
        console.print(f"[green]Browser challenge mode set to: {challenge_mode}[/green]")

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
                      set_connector_url, set_proxy_url, set_browser_proxy_url,
                      set_browser_extension_dirs, set_opencli_extension_dir, set_browser_challenge_mode,
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
        from .browser_identity import mask_secret_url
        console.print(f"  Browser proxy URL: {mask_secret_url(cfg.browser_proxy_url) or '(not set)'}")
        console.print(f"  Browser extensions: {cfg.browser_extension_dirs or '(not set)'}")
        console.print(f"  Browser challenge: {cfg.browser_challenge_mode or 'manual'}")
        console.print(f"  Email:             {cfg.email}")
        console.print(f"  Elsevier API key:  {'****' if cfg.elsevier_api_key else '(not set)'}")
        console.print(f"  Elsevier inst tok: {'****' if cfg.elsevier_inst_token else '(not set)'}")
        console.print(f"  Federated login:   {'Yes' if cfg.carsi_enabled else 'No'}")
        console.print(f"  Federated school:  {cfg.carsi_idp_name or '(not set)'}")
        console.print(f"  Output dir:        {cfg.output_dir}")
        console.print(f"  Cache dir:         {cfg.cache_dir}")
        console.print(f"  Cookie path:       {cfg.cookie_path}")


@app.command("opencli-bridge-doctor")
def opencli_bridge_doctor(
    runtime_probe: bool = typer.Option(False, "--runtime-probe", help="Launch a temporary CloakBrowser and read the OpenCLI extension popup status."),
    use_config_profile: bool = typer.Option(False, "--use-config-profile", help="Use the configured CloakBrowser profile for the runtime probe instead of a temporary profile."),
    keep_open: bool = typer.Option(False, "--keep-open", help="Keep the runtime-probe CloakBrowser window open."),
    timeout: float = typer.Option(15.0, "--timeout", min=3.0, max=120.0, help="Runtime probe timeout in seconds."),
    output: str = typer.Option("", "--output", "-o", help="Write diagnostics JSON to this path."),
    json_output: bool = typer.Option(False, "--json", help="Print diagnostics as JSON."),
):
    """Diagnose whether the OpenCLI Browser Bridge is configured and connected."""
    from .opencli_bridge import build_opencli_bridge_diagnostics

    cfg = Config.load()
    diagnostics = build_opencli_bridge_diagnostics(
        cfg,
        runtime_probe=runtime_probe,
        use_config_profile=use_config_profile,
        timeout_sec=timeout,
        keep_open=keep_open,
    )
    if output:
        output_path = Path(output)
        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_text(json.dumps(diagnostics, ensure_ascii=False, indent=2), encoding="utf-8")

    if json_output:
        console.print_json(json.dumps(diagnostics, ensure_ascii=False))
        return

    table = Table(title="OpenCLI Browser Bridge Doctor")
    table.add_column("Check", width=24)
    table.add_column("Status", width=18)
    table.add_column("Detail", overflow="fold")

    table.add_row(
        "Configured extensions",
        str(diagnostics["configured_extension_count"]),
        cfg.browser_extension_dirs or "(not configured)",
    )
    extensions = diagnostics.get("extensions", [])
    for index, info in enumerate(extensions, start=1):
        status = "ok" if info.get("manifest_ok") and info.get("required_permissions_present") else "check"
        detail = (
            f"{info.get('name') or '(unknown)'} {info.get('version') or ''}; "
            f"daemon={info.get('daemon_host')}:{info.get('daemon_port')}; "
            f"actions={', '.join(info.get('command_actions') or []) or '(none parsed)'}"
        )
        if info.get("error"):
            detail += f"; error={info['error']}"
        table.add_row(f"Extension {index}", status, detail)

    daemon = diagnostics.get("daemon", {})
    daemon_status = "connected" if daemon.get("extension_connected") else ("up" if daemon.get("ping_ok") else "down")
    table.add_row(
        "OpenCLI daemon",
        daemon_status,
        (
            f"port={daemon.get('port')}; daemon={daemon.get('daemon_version') or '(unknown)'}; "
            f"extension={daemon.get('extension_version') or '(not connected)'}; "
            f"context={daemon.get('context_id') or '(none)'}; "
            f"profiles={len(daemon.get('profiles') or [])}"
        ),
    )

    runtime = diagnostics.get("runtime_probe")
    if isinstance(runtime, dict):
        runtime_status = "connected" if runtime.get("connected") else ("launched" if runtime.get("launched") else "not launched")
        table.add_row(
            "Runtime probe",
            runtime_status,
            (
                f"profile={runtime.get('profile')}; extension_id={runtime.get('extension_id') or '(none)'}; "
                f"popup={runtime.get('popup_status_text') or '(none)'}; "
                f"context={runtime.get('popup_context_id') or '(none)'}; "
                f"registered={runtime.get('daemon_profile_registered')}; "
                f"error={runtime.get('error') or '(none)'}"
            ),
        )
    else:
        table.add_row("Runtime probe", "skipped", "Pass --runtime-probe to launch a temporary CloakBrowser.")

    table.add_row("Verdict", diagnostics.get("verdict", ""), output_path_text(output))
    console.print(table)
    if output:
        console.print(f"[dim]Diagnostics written to: {output}[/dim]")


def output_path_text(output: str) -> str:
    return f"json={output}" if output else ""


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
    api_key: str = typer.Option("", "--api-key", help="Elsevier API key (32-char hex)."),
    inst_token: str = typer.Option("", "--inst-token", help="Elsevier institutional token."),
    validate: bool = typer.Option(False, "--validate", help="Validate an existing API key."),
):
    """Set up Elsevier API key for direct PDF download.

    Get a free key at: https://dev.elsevier.com/
    """
    cfg = Config.load()

    if api_key:
        cfg.elsevier_api_key = api_key
        cfg.save()
        console.print("[green]Elsevier API key saved.[/green]")

    if inst_token:
        cfg.elsevier_inst_token = inst_token
        cfg.save()
        console.print("[green]Elsevier institutional token saved.[/green]")

    key = cfg.elsevier_api_key
    if not key:
        console.print("[yellow]No Elsevier API key configured.[/yellow]")
        console.print()
        console.print("To get a free API key:")
        console.print("  1. Go to [cyan]https://dev.elsevier.com/[/cyan]")
        console.print("  2. Register and create an API key")
        console.print("  3. Run: [cyan]instsci elsevier-setup --api-key YOUR_KEY[/cyan]")
        console.print()
        console.print("With an institutional token, you get full-text PDF access:")
        console.print("  [cyan]instsci elsevier-setup --api-key KEY --inst-token TOKEN[/cyan]")
        raise typer.Exit(0)

    if validate:
        console.print("Validating Elsevier API key...")
        import requests
        try:
            resp = requests.get(
                "https://api.elsevier.com/content/serial/title",
                headers={"X-ELS-APIKey": key, "Accept": "application/json"},
                params={"issn": "0043-1354"},  # Water Research
                timeout=15,
            )
            if resp.status_code == 200:
                console.print("[green]API key is valid![/green]")
                data = resp.json()
                titles = data.get("search-results", {}).get("entry", [])
                if titles:
                    console.print(f"  Test query returned: {titles[0].get('dc:title', 'N/A')[:60]}")
            elif resp.status_code == 401:
                console.print("[red]API key is invalid (HTTP 401).[/red]")
            else:
                console.print(f"[yellow]Unexpected response: HTTP {resp.status_code}[/yellow]")
        except Exception as e:
            console.print(f"[red]Validation failed: {e}[/red]")

        # Check PDF access
        console.print()
        console.print("Testing PDF access...")
        try:
            resp = requests.get(
                "https://api.elsevier.com/content/article/doi/10.1016/j.watres.2024.121507",
                headers={"X-ELS-APIKey": key, "Accept": "application/pdf"},
                timeout=30,
            )
            ct = resp.headers.get("content-type", "")
            if resp.status_code == 200 and "pdf" in ct:
                console.print(f"[green]PDF access: YES ({len(resp.content)} bytes)[/green]")
            elif resp.status_code == 200:
                console.print(f"[yellow]PDF access: NO (got {ct[:40]}, need institutional token)[/yellow]")
            else:
                console.print(f"[yellow]PDF access: HTTP {resp.status_code}[/yellow]")
        except Exception as e:
            console.print(f"[red]PDF test failed: {e}[/red]")

    console.print()
    console.print(f"  API Key:        {key[:8]}...{key[-4:]}" if len(key) > 12 else f"  API Key:        {key}")
    console.print(f"  Inst Token:     {cfg.elsevier_inst_token or '(not set)'}")


if __name__ == "__main__":
    app()
