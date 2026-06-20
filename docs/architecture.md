# InstSci Architecture

InstSci has one primary closed-access workflow: plan the route, keep a visible
CloakBrowser session alive, let the user complete institution checks, capture
the publisher PDF in that browser context, then verify and report evidence.
For the detailed browser execution model, human handoff state machine, and
publisher-skill checklist, see `docs/browser-execution-layer.md`.

## Layers

1. CLI and MCP planning
   - `instsci/cli.py` owns user-facing commands.
   - `instsci/mcp_server.py` exposes planning and context tools for agents.
   - MCP tools do not provide final closed-access PDF verdicts; they produce
     context and visible-browser commands.

2. Institutional identity
   - `instsci/config.py` stores the user-selected institution and browser
     identity settings.
   - `instsci/institution_identity.py` maps the selected institution text to
     page-visible aliases and selectors.
   - Publisher profiles must not hard-code a default institution.

3. Publisher knowledge
   - `instsci/publisher_profiles.py` describes publisher URL templates, auth
     markers, input controls, and PDF markers.
   - `instsci/publisher_pdf_router.py` builds official PDF candidates and
     filters discovered URLs to the current article.

4. Browser runtime
   - `instsci/browser_identity.py` builds CloakBrowser launch identity without
     leaking secrets.
   - `instsci/session_broker.py` keeps one publisher browser context alive
     across jobs when the requested browser profile, institution, proxy, and
     extension identity match the running broker.
   - Broker state records non-secret persistence diagnostics such as active
     job id, output directory, last job status, summary path, and manual
     attention messages.
   - Idle brokers also record lightweight session health fields: last health
     status, check time, checked URL, short reason, and keepalive interval.
     Health probes do not save page body text or credentials.
   - If a broker is already marked `reauth_required`, later job submissions
     are persisted as paused jobs instead of being sent into a stale login
     state. The broker state and human-assist state file record the resume
     command without storing credentials.
   - When a batch reaches an institution login, CAPTCHA, or browser challenge
     checkpoint, the broker can mark `reauth_required`, save the remaining
     DOI records as a paused job, keep CloakBrowser alive, and let
     `session-broker-resume` or `session-broker-resume --all` continue after
     the user finishes the visible prompt.
   - `instsci/opencli_bridge.py` is optional diagnostics and observation; it
     does not replace PDF response/download capture.
   - Attached control layers must follow the `observe` -> `find` ->
     `click_public` -> screenshot/PDF verification pattern in
     `docs/browser-execution-layer.md`.

5. PDF state machine
   - `instsci/publisher_batch.py` opens the article, enters institution flows,
     waits for manual checks, clicks PDF controls, captures PDF bytes, and
     writes evidence artifacts.
   - HTTP probes, route construction, cookies, URLs, and DOM state are
     supporting evidence only.

6. Open-access and legacy fetch API
   - `instsci/fetcher.py` tries OA/API/direct publisher routes and can delegate
     browser PDF capture to the profile-driven downloader.
   - For closed-access publisher batch work, prefer `instsci papers` or
     `instsci publisher-batch`.

## Design Rules

- Never default to a specific institution.
- Keep institution-specific aliases out of publisher profiles.
- Prefer publisher broker, Shibboleth, OpenAthens, and CARSI before WebVPN.
- Keep CloakBrowser visible for SSO, 2FA, CAPTCHA, WAF, and publisher checks.
- Treat OpenCLI as an optional control/diagnostic bridge, not the download
  engine.
- Prefer `instsci papers` for repeated closed-access batches because it defaults
  to one long-lived CloakBrowser broker context per publisher.
- `instsci publisher-batch` also defaults to the long-lived publisher broker
  for single-publisher runs; use `--no-broker --keep-browser-open` only when a
  diagnostic one-shot browser must remain visible.
- Long-lived defaults extend broker and manual re-auth wait windows, but must
  not store institution credentials, OTPs, CAPTCHA answers, or recovery codes.
  The default persistence window is 3 days; `--overnight` remains only as a
  compatibility/force-conservative flag.
- Idle broker health checks may detect logged-out or challenge states and
  surface `reauth_required`, but they never bypass institution checks or enter
  credentials for the user.
- Publisher-specific knowledge should be promoted into reusable publisher
  profiles and deterministic workflow code before asking a model to rediscover
  the same page controls.
- Verify final PDFs from captured bytes and text/DOI/title evidence.
