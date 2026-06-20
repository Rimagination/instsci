"""Core paper fetching logic."""

from contextvars import ContextVar
import hashlib
import json
import logging
import random
import re
import time
from pathlib import Path
from urllib.parse import urlparse

import requests

from .auth import EZProxyAuth, WebVPNAuth
from .http_utils import request_with_retry
from .carsi import CARSIClient, detect_publisher
from .config import Config
from .extractors import html_extractor, pdf_extractor
from .models import FetchResult, NextAction, Paper
from .pdf_bytes import describe_non_pdf_bytes, is_plausible_pdf_bytes
from .publisher_pdf_router import build_pdf_candidates, discover_pdf_candidates_from_html
from .publisher_profiles import infer_publisher_profile, infer_publisher_profile_from_url
from .sources import arxiv, unpaywall

logger = logging.getLogger(__name__)

DOI_PATTERN = re.compile(r"^10\.\d{4,9}/[^\s]+$")

# Minimum full_text length to consider a fetch "successful"
MIN_FULLTEXT_LEN = 1000

_ATTEMPT_LOG: ContextVar[list[dict[str, str]] | None] = ContextVar(
    "instsci_fetch_attempt_log",
    default=None,
)


def _record_attempt(stage: str, status: str, reason: str = "", detail: str = "") -> None:
    attempts = _ATTEMPT_LOG.get()
    if attempts is None:
        return
    attempt = {"stage": stage, "status": status}
    if reason:
        attempt["reason"] = reason
    if detail:
        attempt["detail"] = detail
    attempts.append(attempt)


def _record_paper_attempt(stage: str, paper: Paper | None) -> None:
    if paper is None:
        _record_attempt(stage, "miss", reason="no_result")
        return
    if len(paper.full_text or "") >= MIN_FULLTEXT_LEN:
        _record_attempt(stage, "success", reason="full_text")
        return
    _record_attempt(stage, "partial", reason=_paper_quality(paper))


def _paper_quality(paper: Paper) -> str:
    if paper.full_text:
        return "short_text"
    if paper.pdf_path:
        return "pdf_only"
    if paper.abstract:
        return "abstract_only"
    if paper.title or paper.authors or paper.journal or paper.year:
        return "metadata_only"
    return "none"


def _apply_attempt_diagnostics(result: FetchResult, identifier: str) -> None:
    """Refine the final next action using provider-level failure provenance."""
    if result.status != "success" and any(
        attempt.get("stage") == "elsevier_api"
        and attempt.get("status") == "error"
        and attempt.get("reason") == "api_key_missing"
        for attempt in result.attempts
    ):
        result.status = "config_needed"
        result.reason = "elsevier_api_key_missing"
        result.next_action = NextAction(
            kind="configure_elsevier_api",
            command="instsci elsevier-setup --api-key YOUR_KEY --validate",
            message=(
                "Configure the global Elsevier API key once, then validate that "
                "the XML/object-eid route can retrieve a full PDF."
            ),
        )
        return

    for attempt in result.attempts:
        if (
            attempt.get("stage") == "doi_resolve"
            and attempt.get("status") == "miss"
            and attempt.get("reason") == "no_url"
        ):
            query = (identifier or result.paper.title or result.paper.doi).replace('"', '\\"')
            result.status = "blocked"
            result.reason = "doi_resolution_failed"
            result.next_action = NextAction(
                kind="check_identifier",
                command=f'instsci search "{query}"',
                message=(
                    "DOI did not resolve to an article URL. Check the identifier or search "
                    "for the paper, then retry with a DOI or publisher URL."
                ),
            )
            return

    if (
        result.status == "partial"
        and result.next_action
        and result.next_action.kind == "login"
        and any(
            attempt.get("stage") == "institutional_access"
            and attempt.get("status") == "partial"
            and attempt.get("reason") in {"none", "metadata_only", "abstract_only", "short_text"}
            for attempt in result.attempts
        )
    ):
        result.status = "auth_required"
        result.reason = "institution_login_required"



class PaperFetcher:
    """Main class for fetching academic papers."""

    def __init__(self, config: Config | None = None):
        self.config = config or Config.load()
        self.config.ensure_dirs()
        self._auth: WebVPNAuth | EZProxyAuth | None = None
        self._carsi: CARSIClient | None = None
        self._last_request_time = 0.0

    @property
    def auth(self) -> WebVPNAuth | EZProxyAuth:
        if self._auth is None:
            from .schools import get_school
            entry = get_school(self.config.school)
            if entry.school_type == "ezproxy":
                self._auth = EZProxyAuth(self.config, proxy_base=entry.host)
            else:
                self._auth = WebVPNAuth(self.config, key=entry.key, iv=entry.iv)
        return self._auth

    @property
    def carsi(self) -> CARSIClient:
        if self._carsi is None:
            self._carsi = CARSIClient(self.config)
        return self._carsi

    def fetch(self, identifier: str, use_cache: bool = True) -> Paper:
        """Fetch a paper by DOI or URL.

        Args:
            identifier: DOI, article URL, or EZproxy URL.
            use_cache: Whether to check/use cached results.

        Returns:
            Paper object with extracted content.
        """
        doi = self._parse_doi(identifier)
        url = self._parse_url(identifier)

        # Check cache — only return if the cached result has real full text
        if use_cache and doi:
            cached = self._load_cache(doi)
            if cached and len(cached.full_text or "") >= MIN_FULLTEXT_LEN:
                _record_paper_attempt("cache", cached)
                logger.info("Loaded from cache (good full text): %s", doi)
                return cached
            elif cached:
                _record_paper_attempt("cache", cached)
                logger.info("Cache hit but full text too short (%d chars), re-fetching: %s",
                            len(cached.full_text or ""), doi)
            else:
                _record_attempt("cache", "miss", reason="not_found")

        paper = Paper(doi=doi or "", url=url or "")

        # Step 1: Try Open Access sources first (if we have a DOI)
        if doi:
            oa_paper = self._try_open_access(doi)
            _record_paper_attempt("open_access", oa_paper)
            if oa_paper and len(oa_paper.full_text or "") >= MIN_FULLTEXT_LEN:
                self._save_cache(oa_paper)
                return oa_paper
            # Even if OA didn't get full text, preserve metadata
            if oa_paper:
                paper = oa_paper

        # Step 2: Try clean publisher APIs before any institutional/browser flow.
        if doi and not paper.pdf_path:
            api_paper = self._try_elsevier_api(doi, paper)
            if api_paper is None and self._elsevier_api_key_missing(doi):
                _record_attempt(
                    "elsevier_api",
                    "error",
                    reason="api_key_missing",
                    detail="global config: instsci elsevier-setup --api-key YOUR_KEY --validate",
                )
            else:
                _record_paper_attempt("elsevier_api", api_paper)
            if api_paper and len(api_paper.full_text or "") >= MIN_FULLTEXT_LEN:
                self._save_cache(api_paper)
                return api_paper

        # Step 3: Resolve DOI to URL if needed
        if doi and not url:
            url = self._resolve_doi(doi)
            if url:
                _record_attempt("doi_resolve", "success", detail=url)
            else:
                _record_attempt("doi_resolve", "miss", reason="no_url")
            paper.url = url or ""

        if not url:
            logger.error("Could not determine URL for: %s", identifier)
            return paper

        # Step 4: Try federated publisher access before campus gateway access.
        if self.config.carsi_enabled and doi:
            carsi_paper = self._try_carsi_pdf(doi, url, paper)
            _record_paper_attempt("carsi_pdf", carsi_paper)
            if carsi_paper and len(carsi_paper.full_text or "") >= MIN_FULLTEXT_LEN:
                self._save_cache(carsi_paper)
                return carsi_paper
            if self.config.carsi_enabled and url:
                carsi_paper = self._try_carsi_html(url, paper)
                _record_paper_attempt("carsi_html", carsi_paper)
                if carsi_paper and len(carsi_paper.full_text or "") >= MIN_FULLTEXT_LEN:
                    self._save_cache(carsi_paper)
                    return carsi_paper

        # Step 5: Try direct publisher PDF URL construction before campus gateway HTML.
        if doi and not paper.pdf_path:
            pdf_paper = self._try_publisher_pdf(doi, url, paper)
            _record_paper_attempt("publisher_pdf", pdf_paper)
            if pdf_paper and len(pdf_paper.full_text or "") >= MIN_FULLTEXT_LEN:
                self._save_cache(pdf_paper)
                return pdf_paper

        # Step 6: Try the profile-driven visible CloakBrowser PDF workflow.
        if doi and not paper.pdf_path:
            browser_paper = self._try_browser_pdf_download(doi, url, paper)
            _record_paper_attempt("browser_pdf", browser_paper)
            if browser_paper and len(browser_paper.full_text or "") >= MIN_FULLTEXT_LEN:
                self._save_cache(browser_paper)
                return browser_paper

        # Step 7: Fetch via institutional campus access.
        self._rate_limit()
        try:
            paper = self._fetch_via_webvpn(url, paper)
        except ValueError:
            _record_attempt("institutional_access", "error", reason="config_needed")
            raise
        except requests.RequestException:
            _record_attempt("institutional_access", "error", reason="gateway_unreachable")
            raise
        _record_paper_attempt("institutional_access", paper)

        # Save to cache only if we got real full text
        if paper.doi and len(paper.full_text or "") >= MIN_FULLTEXT_LEN:
            self._save_cache(paper)

        return paper

    def fetch_with_result(self, identifier: str, use_cache: bool = True) -> FetchResult:
        """Fetch a paper and return a structured, agent-friendly outcome."""
        attempts: list[dict[str, str]] = []
        token = _ATTEMPT_LOG.set(attempts)
        try:
            paper = self.fetch(identifier, use_cache=use_cache)
        except ValueError as exc:
            doi = self._parse_doi(identifier) or ""
            url = self._parse_url(identifier) or ""
            return FetchResult(
                status="config_needed",
                quality="none",
                reason="institution_not_configured",
                paper=Paper(doi=doi, url=url),
                next_action=NextAction(
                    kind="configure_institution",
                    command="instsci config-cmd --school YOUR_SCHOOL",
                    message=f"Configure your school or institution before retrying. Detail: {exc}",
                ),
                attempts=attempts,
            )
        except requests.RequestException as exc:
            doi = self._parse_doi(identifier) or ""
            url = self._parse_url(identifier) or ""
            gateway_error = any(
                attempt.get("stage") == "institutional_access"
                and attempt.get("status") == "error"
                and attempt.get("reason") == "gateway_unreachable"
                for attempt in attempts
            )
            reason = "gateway_unreachable" if gateway_error else "network_error"
            kind = "diagnose_gateway" if gateway_error else "diagnose"
            message = (
                "Institutional gateway could not be reached. Check VPN/proxy/CARSI/WebVPN "
                f"configuration, then retry. Detail: {exc}"
                if gateway_error
                else f"Check network and institutional access configuration. Detail: {exc}"
            )
            return FetchResult(
                status="blocked",
                quality="none",
                reason=reason,
                paper=Paper(doi=doi, url=url),
                next_action=NextAction(
                    kind=kind,
                    command="instsci config-cmd --show",
                    message=message,
                ),
                attempts=attempts,
            )
        finally:
            _ATTEMPT_LOG.reset(token)

        result = FetchResult.from_paper(
            paper,
            min_fulltext_len=MIN_FULLTEXT_LEN,
            institution_configured=self._institution_configured(),
            identifier=identifier,
        )
        result.attempts = attempts
        _apply_attempt_diagnostics(result, identifier)
        return result

    def _institution_configured(self) -> bool:
        """Return whether any legal institutional access path is configured."""
        return bool(
            self.config.school
            or self.config.webvpn_base_url
            or self.config.ezproxy_base_url
            or self.config.proxy_url
            or (self.config.carsi_enabled and self.config.carsi_idp_name)
        )

    def _elsevier_api_key_missing(self, doi: str) -> bool:
        """Return whether an Elsevier DOI needs API setup before API retrieval."""
        if not doi.startswith("10.1016/"):
            return False
        from .sources import elsevier_api

        return not bool(elsevier_api.get_api_key(self.config.elsevier_api_key))

    def _try_open_access(self, doi: str) -> Paper | None:
        """Try to fetch paper from Open Access sources.

        Priority: arXiv PDF > OA PDF > OA HTML.
        If HTML extraction is too short, attempt PDF fallback from HTML page.
        """
        logger.info("Checking Unpaywall for OA version of %s...", doi)
        oa = unpaywall.check_oa(doi, email=self.config.email)

        paper = Paper(
            doi=doi,
            title=oa.title,
            authors=oa.authors or [],
            journal=oa.journal,
            year=oa.year,
        )

        if not oa.is_oa:
            logger.info("No OA version found for %s.", doi)
            return paper

        # Check if it's an arXiv paper
        arxiv_id = None
        if oa.source == "arxiv" or "arxiv" in (oa.pdf_url or "").lower():
            arxiv_id = arxiv.extract_arxiv_id(oa.pdf_url or oa.html_url or "")

        if arxiv_id:
            return self._fetch_arxiv(arxiv_id, paper)

        # Try direct OA PDF download FIRST (always prefer PDF over HTML)
        if oa.pdf_url:
            logger.info("Downloading OA PDF: %s", oa.pdf_url)
            paper.source = "open_access"
            self._rate_limit()
            try:
                resp = request_with_retry("GET", oa.pdf_url, timeout=60, stream=True)
                resp.raise_for_status()
                ct = resp.headers.get("content-type", "").lower()
                if "pdf" in ct:
                    pdf_bytes = resp.content
                    paper.full_text = pdf_extractor.extract_from_bytes(pdf_bytes)
                    paper.figures = pdf_extractor.extract_figures_from_text(
                        paper.full_text
                    ) if hasattr(pdf_extractor, 'extract_figures_from_text') else []
                    # Save PDF
                    pdf_path = self._save_pdf(doi, pdf_bytes)
                    paper.pdf_path = str(pdf_path) if pdf_path else ""
                    if len(paper.full_text or "") >= MIN_FULLTEXT_LEN:
                        return paper
                    else:
                        logger.warning(
                            "OA PDF text too short (%d chars), continuing...",
                            len(paper.full_text or ""),
                        )
                else:
                    logger.warning("OA PDF URL returned non-PDF content-type: %s", ct)
            except requests.RequestException as e:
                logger.warning("Failed to download OA PDF: %s", e)

        # Try OA HTML (but don't return immediately — check quality first)
        if oa.html_url:
            logger.info("Fetching OA HTML: %s", oa.html_url)
            paper.source = "open_access"
            self._rate_limit()
            try:
                resp = request_with_retry("GET", oa.html_url, timeout=30)
                resp.raise_for_status()
                extracted = html_extractor.extract(resp.text, oa.html_url)
                self._apply_extracted(paper, extracted)

                # If HTML extraction got enough text, return
                if len(paper.full_text or "") >= MIN_FULLTEXT_LEN:
                    return paper

                # HTML extraction was too short — try to find PDF link in the page
                logger.info(
                    "OA HTML extraction too short (%d chars), looking for PDF link...",
                    len(paper.full_text or ""),
                )
                pdf_url = self._find_pdf_link(resp.text, resp.url)
                if pdf_url:
                    logger.info("Found PDF link in OA HTML page: %s", pdf_url)
                    self._rate_limit()
                    try:
                        pdf_resp = request_with_retry("GET", pdf_url, timeout=60)
                        pdf_resp.raise_for_status()
                        if "pdf" in pdf_resp.headers.get("content-type", "").lower():
                            pdf_bytes = pdf_resp.content
                            paper.full_text = pdf_extractor.extract_from_bytes(pdf_bytes)
                            pdf_path = self._save_pdf(doi, pdf_bytes)
                            paper.pdf_path = str(pdf_path) if pdf_path else ""
                            if len(paper.full_text or "") >= MIN_FULLTEXT_LEN:
                                return paper
                    except requests.RequestException as e:
                        logger.warning("Failed to download PDF from HTML link: %s", e)

            except requests.RequestException as e:
                logger.warning("Failed to fetch OA HTML: %s", e)

        return paper

    def _fetch_arxiv(self, arxiv_id: str, paper: Paper) -> Paper:
        """Fetch paper from arXiv."""
        logger.info("Fetching from arXiv: %s", arxiv_id)
        paper.source = "arxiv"

        # Get metadata
        meta = arxiv.fetch_metadata(arxiv_id)
        if meta:
            paper.title = paper.title or meta.get("title", "")
            paper.authors = paper.authors or meta.get("authors", [])
            paper.abstract = meta.get("abstract", "")
            paper.year = paper.year or meta.get("year")
            paper.url = meta.get("url", "")

        # Download PDF
        pdf_path = Path(self.config.output_dir) / f"arxiv_{arxiv_id.replace('/', '_')}.pdf"
        if arxiv.download_pdf(arxiv_id, str(pdf_path)):
            paper.pdf_path = str(pdf_path)
            paper.full_text = pdf_extractor.extract_text(pdf_path)
            paper.figures = pdf_extractor.extract_figures(pdf_path)

        return paper

    @staticmethod
    def _build_publisher_pdf_url(doi: str, resolved_url: str) -> str | None:
        """Construct a direct PDF URL from known publisher patterns.

        Returns the PDF URL string, or None if the publisher is not recognized.
        """
        profile = infer_publisher_profile_from_url(resolved_url) or infer_publisher_profile(doi)
        if profile is None:
            return None
        candidates = build_pdf_candidates(profile, doi, source_url=resolved_url)
        return candidates[0] if candidates else None

    def _try_publisher_pdf(self, doi: str, resolved_url: str, paper: Paper) -> Paper | None:
        """Try to directly construct and download the publisher PDF URL via campus access."""
        pdf_url = self._build_publisher_pdf_url(doi, resolved_url)
        if not pdf_url:
            return None

        logger.info("Trying constructed publisher PDF URL: %s", pdf_url)

        if not self.auth.login():
            logger.error("Institutional access authentication failed.")
            return None

        self._rate_limit()
        try:
            resp = self.auth.fetch(pdf_url)
            resp.raise_for_status()
            ct = resp.headers.get("content-type", "").lower()

            if "pdf" in ct and len(resp.content) > 10000:
                pdf_bytes = resp.content
                paper.full_text = pdf_extractor.extract_from_bytes(pdf_bytes)
                paper.figures = pdf_extractor.extract_figures_from_text(
                    paper.full_text
                ) if hasattr(pdf_extractor, 'extract_figures_from_text') else []
                pdf_path = self._save_pdf(doi, pdf_bytes)
                paper.pdf_path = str(pdf_path) if pdf_path else ""
                paper.source = "institutional"
                logger.info(
                    "Publisher PDF downloaded successfully (%d bytes, %d chars text)",
                    len(pdf_bytes), len(paper.full_text or ""),
                )
                return paper
            else:
                logger.info(
                    "Publisher PDF URL returned non-PDF or too small (ct=%s, size=%d)",
                    ct, len(resp.content),
                )
        except requests.RequestException as e:
            logger.warning("Failed to fetch publisher PDF: %s", e)

        return None

    def _try_browser_pdf_download(self, doi: str, resolved_url: str, paper: Paper) -> Paper | None:
        """Delegate browser PDF capture to the profile-driven CloakBrowser workflow."""
        profile = infer_publisher_profile_from_url(resolved_url) or infer_publisher_profile(doi)
        if profile is None:
            return None

        from .publisher_batch import PaperRecord, PublisherBatchDownloader, safe_name

        logger.info("Trying profile-driven CloakBrowser PDF workflow: %s", profile.name)
        run_dir = Path(self.config.cache_dir) / "browser_pdf" / safe_name(doi)
        downloader = PublisherBatchDownloader(
            self.config,
            profile=profile,
            institution_query=self.config.carsi_idp_name or self.config.school,
            pdf_timeout_sec=90,
        )
        try:
            summary = downloader.run_records(
                [PaperRecord(doi=doi, title=paper.title, url=resolved_url)],
                run_dir,
                retry_failed=True,
                target_verified=1,
            )
        except ImportError as exc:
            logger.warning("CloakBrowser workflow unavailable: %s", exc)
            return None
        except Exception as exc:
            logger.warning("CloakBrowser workflow failed: %s", exc)
            return None

        manifest_json = Path(summary.get("manifest") or run_dir / "complete" / "manifest.csv").with_suffix(".json")
        if not manifest_json.exists():
            logger.info("CloakBrowser workflow did not write a complete manifest: %s", manifest_json)
            return None

        try:
            manifest = json.loads(manifest_json.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError) as exc:
            logger.warning("Failed to read CloakBrowser manifest: %s", exc)
            return None

        item = next(
            (
                entry
                for entry in manifest
                if str(entry.get("doi", "")).lower() == doi.lower()
                and entry.get("status") == "success"
                and entry.get("pdf_path")
            ),
            None,
        )
        if not item:
            logger.info("CloakBrowser workflow did not verify a PDF for %s", doi)
            return None

        pdf_path = Path(str(item["pdf_path"]))
        if not pdf_path.exists():
            logger.info("CloakBrowser manifest PDF path is missing: %s", pdf_path)
            return None
        try:
            pdf_bytes = pdf_path.read_bytes()
        except OSError as exc:
            logger.warning("Failed to read CloakBrowser PDF path %s: %s", pdf_path, exc)
            return None
        if not is_plausible_pdf_bytes(pdf_bytes):
            logger.info(
                "CloakBrowser manifest path is not a PDF: %s (%s)",
                pdf_path,
                describe_non_pdf_bytes(pdf_bytes),
            )
            return None

        paper.full_text = pdf_extractor.extract_text(pdf_path)
        paper.pdf_path = str(pdf_path)
        paper.source = "browser"
        return paper

    def _try_carsi_pdf(self, doi: str, resolved_url: str, paper: Paper) -> Paper | None:
        """Try to download publisher PDF via CARSI-authenticated session."""
        pdf_url = self._build_publisher_pdf_url(doi, resolved_url)
        if not pdf_url:
            return None

        logger.info("Trying CARSI publisher PDF: %s", pdf_url)
        self._rate_limit()
        try:
            resp = self.carsi.fetch(pdf_url)
            resp.raise_for_status()
            ct = resp.headers.get("content-type", "").lower()
            if "pdf" in ct and len(resp.content) > 10000:
                paper_copy = Paper(
                    doi=paper.doi, title=paper.title, authors=paper.authors,
                    journal=paper.journal, year=paper.year, abstract=paper.abstract,
                    url=pdf_url,
                )
                paper_copy.full_text = pdf_extractor.extract_from_bytes(resp.content)
                pdf_path = self._save_pdf(doi, resp.content)
                paper_copy.pdf_path = str(pdf_path) if pdf_path else ""
                paper_copy.source = "carsi"
                logger.info("CARSI PDF downloaded (%d bytes)", len(resp.content))
                return paper_copy
        except requests.RequestException as e:
            logger.warning("CARSI PDF failed: %s", e)
        return None

    def _try_carsi_html(self, url: str, paper: Paper) -> Paper | None:
        """Try to fetch and extract content via CARSI-authenticated session."""
        logger.info("Trying CARSI HTML: %s", url)
        self._rate_limit()
        try:
            resp = self.carsi.fetch(url)
            resp.raise_for_status()
            ct = resp.headers.get("content-type", "").lower()
            if "pdf" in ct:
                paper_copy = Paper(
                    doi=paper.doi, title=paper.title, authors=paper.authors,
                    journal=paper.journal, year=paper.year, abstract=paper.abstract,
                    url=url,
                )
                paper_copy.full_text = pdf_extractor.extract_from_bytes(resp.content)
                pdf_path = self._save_pdf(paper.doi, resp.content) if paper.doi else None
                paper_copy.pdf_path = str(pdf_path) if pdf_path else ""
                paper_copy.source = "carsi"
                return paper_copy

            extracted = html_extractor.extract(resp.text, url)
            paper_copy = Paper(
                doi=paper.doi, title=paper.title, authors=paper.authors,
                journal=paper.journal, year=paper.year, abstract=paper.abstract,
                url=url,
            )
            self._apply_extracted(paper_copy, extracted)
            paper_copy.source = "carsi"

            if len(paper_copy.full_text or "") >= MIN_FULLTEXT_LEN:
                return paper_copy
        except requests.RequestException as e:
            logger.warning("CARSI HTML failed: %s", e)
        return None

    def _fetch_via_webvpn(self, url: str, paper: Paper) -> Paper:
        """Fetch paper through an authenticated institutional access session."""
        # Ensure we're authenticated
        if not self.auth.login():
            logger.error("Institutional access authentication failed.")
            return paper

        paper.source = "institutional"

        try:
            resp = self.auth.fetch(url)
            resp.raise_for_status()
        except requests.RequestException as e:
            logger.error("Failed to fetch via institutional access: %s", e)
            return paper

        content_type = resp.headers.get("content-type", "").lower()

        # If response is PDF directly
        if "pdf" in content_type:
            pdf_bytes = resp.content
            paper.full_text = pdf_extractor.extract_from_bytes(pdf_bytes)
            pdf_path = self._save_pdf(paper.doi or "unknown", pdf_bytes)
            paper.pdf_path = str(pdf_path) if pdf_path else ""
            return paper

        # HTML response - extract content
        extracted = html_extractor.extract(resp.text, resp.url)
        self._apply_extracted(paper, extracted)

        # Always try to find and download PDF for local storage
        pdf_url = self._find_pdf_link(resp.text, resp.url)
        if pdf_url:
            logger.info("Found PDF link in HTML, downloading: %s", pdf_url)
            self._rate_limit()
            try:
                pdf_resp = self.auth.fetch(pdf_url)
                pdf_resp.raise_for_status()
                ct = pdf_resp.headers.get("content-type", "").lower()
                if "pdf" in ct and len(pdf_resp.content) > 10000:
                    pdf_bytes = pdf_resp.content
                    pdf_path = self._save_pdf(paper.doi or "unknown", pdf_bytes)
                    paper.pdf_path = str(pdf_path) if pdf_path else ""
                    # If HTML extraction was poor, use PDF text
                    if len(paper.full_text or "") < MIN_FULLTEXT_LEN:
                        paper.full_text = pdf_extractor.extract_from_bytes(pdf_bytes)
                        logger.info("Replaced HTML text with PDF text (%d chars)",
                                    len(paper.full_text or ""))
            except requests.RequestException as e:
                logger.warning("Failed to download PDF: %s", e)

        # Fallback: Elsevier API for ScienceDirect papers
        if (len(paper.full_text or "") < MIN_FULLTEXT_LEN
                and paper.doi
                and ("elsevier" in url.lower() or "sciencedirect" in url.lower())):
            api_paper = self._try_elsevier_api(paper.doi, paper)
            if api_paper and len(api_paper.full_text or "") >= MIN_FULLTEXT_LEN:
                return api_paper

        return paper

    def _try_elsevier_api(self, doi: str, paper: Paper) -> Paper | None:
        """Fetch paper via Elsevier API (scansci-pdf pattern).

        Two strategies:
        1. XML full-text with view=FULL: extracts article body text and MAIN PDF EIDs
        2. Content Object API PDF: downloads the MAIN PDF via object/eid

        Both bypass ScienceDirect website and its anti-bot detection.
        """
        from .sources import elsevier_api

        api_key = elsevier_api.get_api_key(self.config.elsevier_api_key)
        if not api_key:
            logger.info("Elsevier API key is not configured; run instsci elsevier-setup first")
            return None

        # Only for Elsevier DOIs
        if not doi.startswith("10.1016/"):
            return None

        logger.info("Trying Elsevier API for %s", doi)

        # Strategy 1: Try XML full text extraction and MAIN PDF EID discovery.
        data = elsevier_api.fetch_fulltext(
            doi,
            api_key=api_key,
            inst_token=self.config.elsevier_inst_token,
            proxy_url=self.config.proxy_url,
        )

        # Strategy 2: Try object/eid PDF download, preferring the route that got XML.
        pdf_bytes = elsevier_api.fetch_pdf(
            doi,
            api_key=api_key,
            inst_token=self.config.elsevier_inst_token,
            proxy_url=self.config.proxy_url,
            pdf_eids=data.get("pdf_eids") if data else None,
            preferred_route=data.get("api_route", "") if data else "",
        )
        if pdf_bytes:
            paper.title = (data or {}).get("title", "") or paper.title
            paper.authors = (data or {}).get("authors", []) or paper.authors
            paper.abstract = (data or {}).get("abstract", "") or paper.abstract
            paper.full_text = pdf_extractor.extract_from_bytes(pdf_bytes)
            if len(paper.full_text or "") < MIN_FULLTEXT_LEN and data and data.get("full_text"):
                paper.full_text = data["full_text"]
            pdf_path = self._save_pdf(doi, pdf_bytes)
            paper.pdf_path = str(pdf_path) if pdf_path else ""
            paper.source = "elsevier_api"
            logger.info("Elsevier API PDF: %d bytes", len(pdf_bytes))
            return paper

        if data and data.get("full_text"):
            result = Paper(
                doi=doi,
                url=paper.url,
                source="elsevier_api",
                title=data.get("title", "") or paper.title,
                authors=data.get("authors", []) or paper.authors,
                abstract=data.get("abstract", "") or paper.abstract,
                full_text=data["full_text"],
            )
            logger.info("Elsevier API XML: %d chars of full text", len(data["full_text"]))
            return result

        return None

    def _apply_extracted(self, paper: Paper, extracted: dict):
        """Apply extracted content to a Paper object."""
        paper.title = paper.title or extracted.get("title", "")
        paper.authors = paper.authors or extracted.get("authors", [])
        paper.abstract = paper.abstract or extracted.get("abstract", "")
        paper.full_text = extracted.get("full_text", "")
        paper.figures = extracted.get("figures", [])
        paper.references = extracted.get("references", [])

    def _find_pdf_link(self, html: str, base_url: str) -> str | None:
        """Find a PDF download link in an HTML page.

        Tries multiple strategies:
        1. Look for <a> tags with PDF-related text/class/href
        2. Look for <meta> citation_pdf_url
        3. Construct publisher-specific PDF URLs from the page URL
        """
        candidates = discover_pdf_candidates_from_html(html, base_url)
        if candidates:
            return candidates[0]

        from bs4 import BeautifulSoup

        soup = BeautifulSoup(html, "lxml")
        parsed = urlparse(base_url)
        base = f"{parsed.scheme}://{parsed.netloc}"
        hostname = parsed.netloc.lower()

        # Strategy 1: <meta name="citation_pdf_url">
        meta_pdf = soup.find("meta", attrs={"name": "citation_pdf_url"})
        if meta_pdf and meta_pdf.get("content"):
            pdf_url = meta_pdf["content"]
            logger.info("Found PDF URL in <meta citation_pdf_url>: %s", pdf_url)
            return self._resolve_url(pdf_url, base)

        # Strategy 2: Common <a> tag patterns
        for a in soup.find_all("a", href=True):
            href = a["href"]
            text = a.get_text(strip=True).lower()
            classes = " ".join(a.get("class", []))

            if any(kw in text for kw in ["pdf", "download pdf", "full text pdf",
                                          "view pdf", "get pdf"]):
                return self._resolve_url(href, base)
            if any(kw in classes for kw in ["pdf", "download-pdf", "pdf-download",
                                             "article-pdf", "article__pdf"]):
                return self._resolve_url(href, base)
            if href.endswith(".pdf"):
                return self._resolve_url(href, base)
            # ACS-specific: /doi/pdf/ links
            if "/doi/pdf/" in href:
                return self._resolve_url(href, base)
            # Wiley-specific: /doi/pdfdirect/ or /doi/epdf/
            if "/doi/pdfdirect/" in href or "/doi/epdf/" in href:
                return self._resolve_url(href, base)

        # Strategy 3: Construct from known publisher URL patterns
        path = parsed.path
        if "pubs.acs.org" in hostname and "/doi/" in path and "/pdf/" not in path:
            # /doi/10.1021/xxx → /doi/pdf/10.1021/xxx
            doi_part = path.split("/doi/")[-1]
            if doi_part:
                return f"{base}/doi/pdf/{doi_part}"

        if "onlinelibrary.wiley.com" in hostname and "/doi/" in path and "/pdfdirect/" not in path:
            doi_part = path.split("/doi/")[-1]
            if doi_part:
                return f"{base}/doi/pdfdirect/{doi_part}"

        if "pubs.rsc.org" in hostname and "/articlelanding/" in path:
            return base_url.replace("/articlelanding/", "/articlepdf/")

        if "tandfonline.com" in hostname and "/doi/" in path and "/pdf/" not in path:
            # /doi/full/10.xxx → /doi/pdf/10.xxx
            doi_part = re.sub(r"/doi/(?:full|abs)/", "/doi/pdf/", path)
            if doi_part != path:
                return f"{base}{doi_part}"

        # Elsevier/ScienceDirect: /retrieve/pii/{PII} → /science/article/pii/{PII}/pdfft
        if ("elsevier.com" in hostname or "sciencedirect.com" in hostname):
            pii_match = re.search(r"pii/([A-Z0-9]+)", path)
            if pii_match:
                pii = pii_match.group(1)
                return f"https://www.sciencedirect.com/science/article/pii/{pii}/pdfft"

        return None

    def _resolve_url(self, href: str, base: str) -> str:
        """Resolve a relative URL against a base."""
        if href.startswith("http"):
            return href
        if href.startswith("//"):
            return "https:" + href
        if href.startswith("/"):
            return base + href
        return base + "/" + href

    def _parse_doi(self, identifier: str) -> str | None:
        """Extract DOI from identifier."""
        identifier = identifier.strip()

        # Direct DOI
        if DOI_PATTERN.match(identifier):
            return identifier

        # DOI URL
        for prefix in ["https://doi.org/", "http://doi.org/", "https://dx.doi.org/"]:
            if identifier.lower().startswith(prefix):
                return identifier[len(prefix):]

        # Try to extract DOI from URL path
        doi_match = re.search(r"(10\.\d{4,9}/[^\s&?#]+)", identifier)
        if doi_match:
            return doi_match.group(1)

        return None

    def _parse_url(self, identifier: str) -> str | None:
        """Extract URL from identifier."""
        identifier = identifier.strip()
        if identifier.startswith("http"):
            return identifier
        if DOI_PATTERN.match(identifier):
            return None  # Pure DOI, not a URL
        return None

    def _resolve_doi(self, doi: str) -> str | None:
        """Resolve a DOI to its target URL."""
        try:
            resp = request_with_retry(
                "GET",
                f"https://doi.org/{doi}",
                allow_redirects=True,
                timeout=15,
                headers={"User-Agent": "instsci/0.1"},
                stream=True,  # Don't download full body
            )
            resp.close()
            # Many publishers return 403/401 for non-browser GETs, but we still get the resolved URL
            if resp.url and resp.url != f"https://doi.org/{doi}":
                logger.info("Resolved DOI %s → %s (status=%d)", doi, resp.url, resp.status_code)
                return resp.url
        except requests.RequestException as e:
            logger.warning("Failed to resolve DOI %s: %s", doi, e)
        return None

    def _rate_limit(self):
        """Apply rate limiting between requests."""
        elapsed = time.time() - self._last_request_time
        delay = random.uniform(self.config.request_delay_min, self.config.request_delay_max)
        if elapsed < delay:
            sleep_time = delay - elapsed
            logger.debug("Rate limiting: sleeping %.1fs", sleep_time)
            time.sleep(sleep_time)
        self._last_request_time = time.time()

    def _save_pdf(self, doi: str, pdf_bytes: bytes) -> Path | None:
        """Save PDF to output directory."""
        if not is_plausible_pdf_bytes(pdf_bytes):
            logger.warning(
                "Refusing to save non-PDF payload for %s: %s",
                doi,
                describe_non_pdf_bytes(pdf_bytes),
            )
            return None

        safe_name = re.sub(r"[^\w\-.]", "_", doi)
        pdf_path = Path(self.config.output_dir) / f"{safe_name}.pdf"
        try:
            pdf_path.parent.mkdir(parents=True, exist_ok=True)
            pdf_path.write_bytes(pdf_bytes)
            logger.info("Saved PDF to %s", pdf_path)
            return pdf_path
        except OSError as e:
            logger.error("Failed to save PDF: %s", e)
            return None

    def _cache_key(self, doi: str) -> Path:
        """Get cache file path for a DOI."""
        h = hashlib.md5(doi.encode()).hexdigest()
        return Path(self.config.cache_dir) / f"{h}.json"

    def _load_cache(self, doi: str) -> Paper | None:
        """Load a cached paper result."""
        path = self._cache_key(doi)
        if not path.exists():
            return None
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            return Paper.from_json(data)
        except (json.JSONDecodeError, OSError) as e:
            logger.warning("Failed to load cache for %s: %s", doi, e)
            return None

    def _save_cache(self, paper: Paper):
        """Save paper result to cache.

        Only caches results with meaningful full text (>= MIN_FULLTEXT_LEN chars)
        to avoid caching abstract-only failures.
        """
        if not paper.doi:
            return
        if len(paper.full_text or "") < MIN_FULLTEXT_LEN:
            logger.info(
                "Skipping cache save for %s: full_text too short (%d chars)",
                paper.doi, len(paper.full_text or ""),
            )
            return
        path = self._cache_key(paper.doi)
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(paper.to_json(), encoding="utf-8")
            logger.info("Cached result for %s (%d chars)", paper.doi, len(paper.full_text or ""))
        except OSError as e:
            logger.warning("Failed to save cache for %s: %s", paper.doi, e)

    def clear_cache(self):
        """Clear all cached results."""
        cache_dir = Path(self.config.cache_dir)
        if cache_dir.exists():
            for f in cache_dir.glob("*.json"):
                f.unlink()
            logger.info("Cache cleared.")

    def close(self):
        """Clean up resources."""
        if self._auth:
            self._auth.close()
        if self._carsi:
            self._carsi.close()
