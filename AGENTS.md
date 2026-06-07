# InstSci Agent Rules

These rules define the project-level behavior expected from AI agents working in this repository.

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
