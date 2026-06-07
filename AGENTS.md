# InstSci Agent Rules

These rules define the project-level behavior expected from AI agents working in this repository.

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
- Browser-backed UI actions require visual checkpoints. After clicking publisher controls such as `PDF`, `Institutional Access`, `Institutional Sign In`, OpenAthens, or a cookie/verification prompt, agents MUST inspect a screenshot of the visible CloakBrowser window before concluding the click worked or failed. DOM events, URL strings, and logs are supporting evidence, not substitutes for visual confirmation.
- When reporting results, label HTTP-only findings as `HTTP preflight` and browser-backed findings as `browser verified`.

## Institutional Identity / Access Route Rule

- Load `instsci/data/institutional_identity_policy.json` before choosing a closed-access PDF identity route. The CLI view is `instsci identity-policy`.
- Default route selection is `auto`, not universal WebVPN and not any hard-coded school. Ask for the user's own subscription institution at the point of use when `--institution`, `config.carsi_idp_name`, and `config.school` are all empty.
- For off-campus publisher access, prefer Shibboleth/OpenAthens institutional authentication when the publisher supports it. Use institution-specific WAYFless links when configured; if they fail, fall back to the standard publisher institution-selection flow.
- Standard federated login flow is publisher login page -> Institutional/Shibboleth/OpenAthens/CARSI option -> federation group or institution search when shown -> user's own institution -> institution IdP. Do not assume any institution unless the user configured or selected it.
- Prefer the publisher broker first; use WebVPN only when the configured institution has a WebVPN gateway and that publisher path is browser-verified through the gateway.
- Do not claim WebVPN cookies are a full reusable login state. `cookies.json` and exported cookie jars are `HTTP preflight` assets only; they do not preserve all browser storage, WebVPN in-memory state, TLS sessions, browser fingerprint/challenge state, or page-generated PDF tokens.
- If WebVPN is attempted, keep the visible CloakBrowser context alive as a WebVPN broker. Reopening a profile may preserve cookies/localStorage/IndexedDB/cache, but it may still lose non-exportable state such as TLS session tickets and Cloudflare/WAF challenge state.
- If WebVPN fails to capture the PDF, fall back to the publisher-specific article-page institutional login flow before marking the publisher failed.
