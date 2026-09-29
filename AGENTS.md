# InstSci Agent Rules

These rules define the project-level behavior expected from AI agents working in this repository.

## Rule Scope

- `AGENTS.md` contains reusable InstSci project rules, not a transcript of one user's session.
- Keep rules institution-neutral and parameterized. Do not encode a personal institution, login state, run directory, cookie, token, credential, or one-off troubleshooting step as a general rule.
- Store dated smoke-test evidence, per-publisher sample results, and detailed run artifacts in `instsci/data/*matrix*`, `runs/`, or task reports. In this file, keep only stable workflow lessons and references to those artifacts.
- Publisher-specific rules are acceptable here only when they describe a reusable strategy for all users of that publisher, such as route order, evidence standard, or known false-positive class.

## Agent Workflow

Use this workflow whenever a user asks to download PDFs, test publisher access, build a publisher capability matrix, diagnose closed-access retrieval, or make a final statement about whether a publisher can provide a PDF.

1. Classify the task.
   - Metadata search, Open Access lookup, DOI resolution, or route discovery may use normal HTTP tools.
   - Publisher PDF download, closed-access verification, or publisher capability verdicts require the built-in visible CloakBrowser workflow.
2. Load the access policy before choosing an identity route.
   - Read `instsci/data/institutional_identity_policy.json`, or run `instsci identity-policy`.
   - If `--institution`, `config.carsi_idp_name`, and `config.school` are all empty, ask for the user's own subscription institution.
3. Choose the least surprising route.
   - Prefer publisher broker / Shibboleth / OpenAthens / CARSI institution login when supported.
   - Use WebVPN only when the configured institution has a WebVPN gateway and that publisher path is browser-verified through the gateway.
   - If WebVPN fails, try the publisher article-page institutional login flow before marking the publisher failed.
4. Run a browser-backed workflow for final PDF evidence.
   - DOI list or auto publisher selection: `instsci papers dois.txt --publisher auto --institution "Institution Name" --output ./runs/papers`.
   - Known publisher profile: `instsci publisher-batch dois.txt --publisher acs --institution "Institution Name" --output ./runs/acs`.
   - Code-level automation may use `PublisherBatchDownloader`, `ACSCloakBatchDownloader`, or the same built-in browser context.
5. Keep the browser visible.
   - Do not hide the CloakBrowser window during SSO, 2FA, CAPTCHA, Cloudflare, WAF checks, or publisher verification.
   - Let the user complete institution checks manually. Wait, resume, and reuse `browser_profile_dir`, `carsi_cookie_dir/<publisher>.json`, and `attempt_cache`.
6. Verify with visual evidence.
   - After clicking `PDF`, `Institutional Access`, `Institutional Sign In`, OpenAthens, cookie prompts, or verification prompts, inspect a screenshot of the visible CloakBrowser window.
   - Do not conclude success or failure from DOM events, URLs, logs, cookies, or HTTP responses alone.

## Evidence Standard

| Label | Allowed Evidence | Final Publisher PDF Verdict |
| --- | --- | --- |
| `HTTP preflight` | `publisher-doctor`, `requests`, `curl`, DOI resolution, route templates, candidate URL construction | No |
| `browser verified` | PDF captured or blocked in the visible built-in CloakBrowser workflow, with screenshot-backed interaction checkpoints | Yes |

Do not mark a publisher unsupported, failed, or verified unless the conclusion comes from `browser verified` evidence.

## Report Template

For publisher PDF work, report each publisher or DOI with:

- `publisher`
- `doi`
- `route_attempted`
- `institution`
- `result`: `browser verified`, `HTTP preflight`, `auth_required`, `blocked`, or `unsupported`
- `evidence`: captured PDF path, screenshot path, diagnostic path, or exact blocker
- `next_action`: what the user or agent should try next

## Mandatory Publisher PDF Browser Rule

- For any publisher PDF download, publisher PDF capability matrix, closed-access verification, or final statement about whether a publisher can provide a PDF, agents MUST use InstSci's built-in CloakBrowser workflow.
- Accepted browser-backed routes include `instsci papers`, `instsci publisher-batch`, `PublisherBatchDownloader`, `ACSCloakBatchDownloader`, or explicit automation of the same built-in browser context.
- `publisher-doctor`, `requests`, `curl`, and other direct HTTP probes are HTTP preflight only. They may verify DOI resolution, route templates, and candidate URL construction, but MUST NOT be presented as the final publisher PDF capability verdict.
- If SSO, 2FA, or CAPTCHA appears, the user completes it manually in the built-in browser. Agents may wait, resume, and reuse `browser_profile_dir`, `carsi_cookie_dir/<publisher>.json`, and `attempt_cache`; agents must not bypass publisher or institution verification.
- Browser-backed SSO runs must keep the built-in CloakBrowser visible and foregroundable. Do not wrap these runs with launchers that hide the browser window from the user.
- Do not run Science/AAAS, Cloudflare, or WAF-gated publisher login tests in ad hoc temporary Playwright contexts or one-shot scripts that are closed after login. Use the long-lived publisher session broker, keep the visible CloakBrowser alive, and submit follow-up PDF or supplementary jobs into that broker. Browser profiles, exported cookies, and attempt caches are supporting artifacts; they are not a replacement for the live broker context after CAPTCHA, SSO, or CARSI.
- Browser-backed UI actions require visual checkpoints. After clicking publisher controls such as `PDF`, `Institutional Access`, `Institutional Sign In`, OpenAthens, or a cookie/verification prompt, agents MUST inspect a screenshot of the visible CloakBrowser window before concluding the click worked or failed. DOM events, URL strings, and logs are supporting evidence, not substitutes for visual confirmation.
- When reporting results, label HTTP-only findings as `HTTP preflight` and browser-backed findings as `browser verified`.

## Supplementary Materials Rule

- Supplementary material, appendix, supporting information, data file, and figure-asset downloads are opt-in. Do not download them during normal paper/PDF runs unless the user explicitly asks for supplementary materials or passes `--include-supplementary`.
- Supplementary capability verdicts follow the same evidence rule as publisher PDFs: final success, blocker, or unsupported statements require visible built-in CloakBrowser evidence with screenshots or captured files.
- Supplementary discovery should prefer structured publisher metadata over URL guessing. Use the order: publisher API/XML/object metadata, then visible article-page semantic links/buttons, then publisher-specific URL markers as a final fallback. Do not rely on broad strings such as `figure`, `image`, or generic `download` unless another signal proves the file is supplementary/supporting material.
- For Elsevier/ScienceDirect supplementary files, use the API-first structured route: `view=FULL` XML -> supplementary `object` / `attachment` refs such as `mmc`, `supplementary-material`, `supporting`, or `appendix` -> `/content/object/eid/{eid}`. Save files by object EID, not by `Content-Disposition`, because Elsevier may return generic names such as `f.txt`. Treat PDFs, Office files, ZIP/data bundles, media, and other non-HTML binary files as valid supplementary assets when the structured metadata marks them as supplementary.
- The 2026-06-23 smoke-test matrix is recorded in `instsci/data/publisher_supplementary_smoke_matrix.md` and `instsci/data/publisher_supplementary_smoke_matrix.json`. Treat short-run missing candidates as inconclusive unless the visible browser evidence rules out supplementary materials.
- For Science/AAAS supplementary batches, use the long-lived `science` broker and start from publisher article pages. Do not use CARSI portal preauth for this normal article route unless the task is explicitly testing the CARSI portal itself; preauth can park the browser on resource pages instead of the publisher article flow.
- After user-selected institution login for Science/AAAS, verify reusable article-page access signals and supplementary controls before considering the broker ready. Avoid manual navigation to federation/resource pages, arbitrary `action/ssostart` URLs, malformed DOI URLs, or same-page skip anchors; if the browser path is confused, enqueue a clean article-page broker job. Detailed dated examples belong in the supplementary smoke matrix, not here.

## Institutional Identity / Access Route Rule

- Load `instsci/data/institutional_identity_policy.json` before choosing a closed-access PDF identity route. The CLI view is `instsci identity-policy`.
- Default route selection is `auto`, not universal WebVPN and not any hard-coded school. Ask for the user's own subscription institution at the point of use when `--institution`, `config.carsi_idp_name`, and `config.school` are all empty.
- For off-campus publisher access, prefer Shibboleth/OpenAthens institutional authentication when the publisher supports it. Use institution-specific WAYFless links when configured; if they fail, fall back to the standard publisher institution-selection flow.
- Standard federated login flow is publisher login page -> Institutional/Shibboleth/OpenAthens/CARSI option -> federation group or institution search when shown -> user's own institution -> institution IdP. Do not assume any institution unless the user configured or selected it.
- Prefer the publisher broker first; use WebVPN only when the configured institution has a WebVPN gateway and that publisher path is browser-verified through the gateway.
- Do not claim WebVPN cookies are a full reusable login state. `cookies.json` and exported cookie jars are `HTTP preflight` assets only; they do not preserve all browser storage, WebVPN in-memory state, TLS sessions, browser fingerprint/challenge state, or page-generated PDF tokens.
- If WebVPN is attempted, keep the visible CloakBrowser context alive as a WebVPN broker. Reopening a profile may preserve cookies/localStorage/IndexedDB/cache, but it may still lose non-exportable state such as TLS session tickets and Cloudflare/WAF challenge state.
- If WebVPN fails to capture the PDF, fall back to the publisher-specific article-page institutional login flow before marking the publisher failed.
