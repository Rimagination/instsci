# OpenCLI Browser Bridge

InstSci can load the OpenCLI unpacked Chrome extension inside the visible
CloakBrowser context. The bridge is optional: CloakBrowser still owns the
browser profile, fingerprints, cookies, storage, downloads, and publisher
verification flow.
The general attach-only control-layer rules live in
`docs/browser-execution-layer.md`; this document records the OpenCLI-specific
configuration and observed behavior.

## Configure

```powershell
instsci config-cmd --opencli-extension-dir "C:\Users\Liang\.opencli\browser-extension\opencli-1.0.20"
```

Use `--browser-extension-dirs` instead when multiple unpacked extensions should
be loaded. Separate paths with semicolons.

## Verify

Check static configuration and the local OpenCLI daemon:

```powershell
instsci opencli-bridge-doctor
```

Launch a temporary visible CloakBrowser profile, load the extension, and read
the OpenCLI popup status:

```powershell
instsci opencli-bridge-doctor --runtime-probe --output runs\opencli_bridge_doctor.json
```

The diagnostic JSON records extension metadata, daemon status, runtime popup
status, and a final verdict such as `connected`.

For runtime probes, treat `daemon_profile_registered: true` as the strongest
local bridge evidence: it means the temporary or configured CloakBrowser
extension context appeared in the OpenCLI daemon profile list for that run.

## A/B Test During Publisher Runs

Run with the configured bridge:

```powershell
instsci publisher-batch dois.txt --publisher elsevier --institution "Your Institution" --output runs\with_bridge
```

Run the same DOI list with all configured browser extensions disabled:

```powershell
instsci publisher-batch dois.txt --publisher elsevier --institution "Your Institution" --output runs\without_bridge --disable-browser-extensions
```

For `instsci papers`, the same switch is available:

```powershell
instsci papers dois.txt --publisher auto --institution "Your Institution" --output runs\papers_without_bridge --disable-browser-extensions
```

`--disable-browser-extensions` also disables the session broker for that run so
the broker cannot silently reload extensions from the global config.

## Evidence In Run Summaries

Every publisher run summary now includes extension evidence without storing
local extension paths:

```json
{
  "browser_extensions_enabled": true,
  "browser_extension_count": 1,
  "browser_extension_hash": "..."
}
```

Compare these fields together with `success`, `missing`, `unverified`,
`retry_attempted`, `concurrency`, and diagnostic screenshots when deciding
whether the bridge helped a publisher workflow.

## Tested Value For InstSci

Validated locally on 2026-06-19 with OpenCLI daemon `1.8.4` and extension
`1.0.20`.

Works well:

- DOM state snapshots with stable numeric refs, useful after a page surprises
  the publisher-specific automation.
- Selector and semantic element discovery through `browser find`.
- Ordinary UI actions such as `fill`, `click`, `wait text`, and tab/session
  cleanup.
- Annotated screenshots that show clickable refs on the visible page.
- Network shape previews for page XHR/fetch responses.
- Real publisher-page triage: on a Springer article page, OpenCLI exposed the
  `log in via an institution` links, `Preview of subscription content`, and
  `Buy article PDF` controls without custom Springer selectors.

Not yet reliable enough:

- `browser wait download` did not observe two controlled CloakBrowser PDF
  attachment downloads in local tests, including a delayed download where the
  wait command was started before the click. Do not replace InstSci's current
  PDF response, viewer, and filesystem capture logic with OpenCLI download
  waiting.

Recommended integration:

- Use OpenCLI as a fallback observation/control layer for visible CloakBrowser
  sessions: after InstSci reaches an unexpected page, ask OpenCLI for `state`,
  `find`, screenshot refs, and possibly one safe public click.
- Promote selectors discovered through OpenCLI back into publisher profiles or
  deterministic workflow code instead of repeatedly relying on ad hoc agent
  exploration.
- Keep InstSci's Playwright/CloakBrowser workflow as the source of truth for
  publisher login, PDF capture, PDF verification, retries, and final reports.
- Never use OpenCLI to enter credentials, OTPs, recovery codes, or CAPTCHA
  answers.

## Safety Boundary

OpenCLI Bridge may help an agent observe or control ordinary page actions, but
it does not replace the visible CloakBrowser evidence standard. When SSO, 2FA,
CAPTCHA, Cloudflare, WAF, or publisher verification appears, the user completes
that step manually in the visible browser. InstSci should not automate credential
entry or CAPTCHA solving.
