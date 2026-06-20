# InstSci Publisher PDF Workflow

This reference is for InstSci publisher PDF retrieval, closed-access verification, publisher capability matrices, and DOI batch recovery tasks.

## Decision Ladder

1. Classify the request.
   - Metadata search, DOI normalization, OA lookup, and route discovery can use normal HTTP tools.
   - Publisher PDF download, closed-access verification, final publisher support verdicts, and capability matrices require visible CloakBrowser evidence.
2. Load policy before identity decisions.
   - Read `instsci/data/institutional_identity_policy.json` from the repository root or run `instsci identity-policy`.
3. Choose identity route.
   - Prefer explicit `--institution`.
   - Then use configured `carsi_idp_name`.
   - Then use configured `school`.
   - If none exists, ask for the user's subscription institution.
4. Use least-surprising access.
   - Prefer publisher broker, Shibboleth, OpenAthens, CARSI, and configured WAYFless links.
   - Use WebVPN only for configured institutions with browser-verified WebVPN routes.
   - If WebVPN fails, try the publisher article-page institutional login flow before marking failure.
5. Preserve visible browser context.
   - Reuse `browser_profile_dir`, `carsi_cookie_dir/<publisher>.json`, and `attempt_cache` when available.
   - Do not hide CloakBrowser while the user may need to complete SSO, 2FA, CAPTCHA, or WAF checks.
6. Verify visually.
   - Screenshots are required after important UI actions.
   - DOM events, URLs, logs, and cookies are supporting evidence only.

## MCP And Skill Handoff

- Let the skill decide whether a task is metadata/OA work, HTTP preflight, or final publisher PDF evidence.
- Use MCP tools for structured context first: `get_institutional_identity_policy`, `get_publisher_access_catalog`, and `get_publisher_browser_verification_matrix`.
- Use `plan_publisher_pdf_workflow` to produce the visible CLI command and to detect missing subscription institution context.
- Do not use MCP `fetch_paper`, HTTP probes, cookie replay, or direct request results as final closed-access publisher PDF verdicts.
- Once a task needs final PDF evidence, start or resume `instsci papers` / `instsci publisher-batch` and keep the CloakBrowser visible for user login and visual checkpoints.
- If MCP context differs from `AGENTS.md` or `instsci/data/*.json`, prefer the repository files and report the mismatch.

## Current CLI Defaults

For normal closed-access publisher batches, rely on the built-in long-lived
defaults instead of adding extra flags by habit:

- `instsci papers` and `instsci publisher-batch` use the publisher session
  broker by default.
- `--human-assist` is enabled by default for publisher browser workflows.
- `--login-timeout` and `--broker-ttl` default to three days (`259200`
  seconds).
- `--overnight` is now a compatibility flag and an explicit way to force
  careful single-context settings; it is not required for ordinary long runs.
- `--no-broker --keep-browser-open` is for deliberate one-shot repair runs
  where the broker is unsuitable.

Control-console commands:

```powershell
instsci session-broker-status -p science
instsci session-broker-status -p science --json
instsci session-broker-resume -p science --all
instsci session-broker-stop -p science
```

Before a large supplement run, check broker identity and queued/paused jobs.
Reuse a broker only when publisher, institution, profile dir, proxy identity,
and extension identity match the intended job.

## BrowserAct And OpenCLI Boundary

BrowserAct free/open-source features were evaluated as a design reference, not
as a dependency for publisher PDF retrieval. Do not install or run BrowserAct by
default for InstSci PDF work, and do not use BrowserAct results as PDF success
evidence. BrowserAct-style concepts that are worth keeping should be
implemented in InstSci's own CloakBrowser workflow: explicit browser actions,
human-handoff states, session inventory, and reusable publisher skills.

OpenCLI Browser Bridge and `agent-browser` remain optional attach-only
diagnostics. They may observe, screenshot, discover selectors, and click safe
public controls after attaching to an InstSci-owned CloakBrowser session. They
must not launch a replacement browser, own auth state, enter secrets, solve
CAPTCHA, capture PDFs, verify PDFs, or declare final publisher support.

## Agent-Browser Attachment Playbook

`agent-browser` can improve page inspection and fallback clicking, but in
InstSci it must be a controller attached to CloakBrowser, not a second browser.
CloakBrowser owns the runtime, fingerprint, profile, cookies, storage,
institution session, downloads, and process lifetime.

Use this only when the tool is installed and the current InstSci run exposes a
confirmed CDP port or WebSocket URL. If no CDP endpoint is available, use the
built-in Playwright/CloakBrowser workflow or the Windows UIA fallback below.
Do not invent a port, rely on broad auto-discovery, or start a second browser.

Allowed attach pattern:

```powershell
agent-browser --session instsci-<publisher> connect <cdp-port-or-url>
agent-browser --session instsci-<publisher> snapshot -i
agent-browser --session instsci-<publisher> screenshot "$out\diagnostics\agent_browser_checkpoint.png" --annotate
```

After connecting, normal commands operate on the attached browser:

```powershell
agent-browser --session instsci-<publisher> get title
agent-browser --session instsci-<publisher> get url
agent-browser --session instsci-<publisher> snapshot -i
agent-browser --session instsci-<publisher> click @e3
agent-browser --session instsci-<publisher> wait --load networkidle
agent-browser --session instsci-<publisher> snapshot -i
```

Safe uses:

- capture compact accessibility snapshots with stable `@eN` refs;
- take annotated screenshots for visual checkpoints;
- inspect page title, URL, visible text, and interactive controls;
- click public publisher controls such as `Access through your organization`,
  `Institutional Sign In`, the explicitly selected institution result, `PDF`,
  or viewer `Download`;
- re-snapshot after every click, navigation, modal, or dynamic page change
  because refs become stale.

Forbidden uses in InstSci publisher workflows:

- do not run unqualified `agent-browser open <url>` before attaching to CloakBrowser;
- do not use `--profile`, `--state`, `--session-name`, `auth save`, `auth login`, `cookies clear`, `storage clear`, or any state import/export that could override CloakBrowser's profile;
- do not rely on `--auto-connect` unless the exact CloakBrowser CDP endpoint has been confirmed;
- do not run `agent-browser close` against the attached CloakBrowser; InstSci owns the lifecycle;
- do not use `agent-browser chat` for SSO, CAPTCHA, publisher access, or credential pages because it may choose actions outside the InstSci evidence rules;
- never fill passwords, OTPs, recovery codes, CAPTCHA answers, or institution credentials.

Concurrency rule: avoid letting Playwright automation and `agent-browser` click
the same tab at the same time. Use `agent-browser` as a diagnostic or fallback
controller when automation stalls, or while the InstSci listener is waiting for
a known user/public control action. After an `agent-browser` click, confirm
progress from screenshot/snapshot plus InstSci artifacts such as
`summary_partial.json`, `attempts.jsonl`, output PDFs, and the final PDF
integrity gate.

Potential code shape for future implementation:

```text
BrowserRuntime
  -> CloakBrowserRuntime      # launch, persistent profile, login state, downloads
  -> AgentBrowserController   # attach-only CDP snapshots/clicks/screenshots
```

Do not implement `AgentBrowserController` as a launcher. It should accept an
existing CDP endpoint from `CloakBrowserRuntime` and refuse to run if no
endpoint is available.

## Recent Gotchas

- Science/AAAS institution login can expose the organization field as `Type the name of your institution` below `Find your institution`. Add publisher-profile selectors for that placeholder plus a nearby XPath fallback before assuming the login page is unsupported.
- For Science/AAAS, institution result names such as `Tsinghua University` are valid only when the user explicitly selected that institution. Never turn a successful Tsinghua run into a global default.
- Science/AAAS `pdfdirect` responses may look like PDFs at the URL level but contain text-decoded or truncated binary bytes. Treat `status=success` as provisional until the final file passes the PDF integrity gate below.
- Science/AAAS batches can hit Cloudflare or browser-integrity checks mid-run. Keep the visible CloakBrowser open, use `--human-assist`, capture the checkpoint screenshot, and let the user handle any verification or institution challenge.
- A stale session broker queue can replay old Science jobs into the browser while the current repair output directory stays unchanged. If that happens, stop the broker/main Python processes, archive stale queue JSON files under `~/.instsci/brokers/<publisher>/queue/stale_YYYYMMDD_HHMMSS`, then rerun the repair with `--no-broker`.
- Elsevier institution entry must not click `Go to Elsevier Homepage` while trying to select an institution.
- Elsevier Tsinghua ShibAuth/WAYFless can bypass unstable organization search when the user explicitly selected or configured Tsinghua.
- Elsevier article pages may stall on a visible `Access through your organization` button even while the CLI process is alive. If browser automation does not advance, inspect the visible CloakBrowser window and click the public institution-access control.
- ScienceDirect may show a Chrome PDF viewer while automated download capture times out. Label this as viewer/download-capture failure with screenshot evidence, not as publisher unsupported.
- When the ScienceDirect PDF viewer displays `PDF loaded` but the Playwright download event is still waiting, use the viewer toolbar `Download` button as a browser-verified fallback; this can let `publisher-batch` capture the download event and save the PDF.
- If `publisher-batch` has already written `summary.json` with `pdf_not_captured` and the Python process has exited, clicking the still-open viewer's `Download` button will not be captured by InstSci. Start a new short single-DOI run with the same browser profile, then click `Download` while that listener is alive.
- ScienceDirect may return `CPE00001 / There was a problem providing the content you requested`; report the exact blocker and screenshot path.
- ACS has worked with explicit English institution name such as `Tsinghua University`; do not generalize that into a default institution.
- Wiley institutional login, full-text entry, and PDF entry paths have had recent fixes; verify with visible browser evidence before changing status.
- DOI input files created by Windows PowerShell may start with a UTF-8 BOM. InstSci CLI now strips BOMs from DOI files before building publisher URLs; if an old run shows `%EF%BB%BF` in the Wiley URL, rerun with the current CLI.
- Wiley pages can contain正文/full-text links to other Wiley articles. The Wiley full-text click helper must pass the current DOI into the page script and only click exact `Read the full text` controls or `/doi/full/` links that match the current DOI; otherwise let the current-record PDF candidate path handle capture.
- `accept_downloads=True` is required in the built-in browser context for reliable Playwright download capture.
- Browser PDF viewer toolbar download fallback can matter when publisher PDF links open in a viewer instead of triggering a direct download.

## Science/AAAS Batch Recovery

Use this playbook after a visible Science/AAAS run produces PDFs that are
missing, unverified, corrupted, or not copied into the final delivery directory.

1. Keep the original run assets.
   - Preserve `candidate_metadata.json`, `dois.txt`, `primary/summary.json`,
     `primary/attempts.jsonl`, diagnostics, and screenshots.
   - Treat Crossref no-license or missing-license metadata as a candidate
     signal only; final closed-access evidence still requires browser-verified
     PDF capture.
2. Build a repair DOI file from `summary.json`.
   - Include rows where `status` is not `success`, `verified_match` is false,
     final PDF is absent, file size is suspiciously small, or the PDF integrity
     gate fails.
3. Prefer a direct repair run when broker state is uncertain.

```powershell
python -m instsci.cli papers "$doiFile" --publisher science --institution "Institution Name" --output "$repairOut" --login-timeout 900 --pdf-timeout 180 --human-assist --no-broker
```

4. If the browser asks for institution login, Cloudflare, CAPTCHA, or 2FA, pause
   for the user and save a screenshot checkpoint before continuing.
5. Assemble a clean final directory only from files that pass verification.
   - Copy verified PDFs into `final_verified_pdfs` or the task's final PDF
     directory.
   - Write aligned `final_manifest.json`, `final_manifest.csv`, and
     `final_report.md`.
   - Report the route as `Science.org institutional login -> user-selected
     institution -> Science PDF route`.

This sequence has recovered Science/AAAS batches where initial `pdfdirect`
files were corrupted. It does not imply any default institution; use only the
institution selected or configured for the current run.

## Visible UI Fallback

Use this only for the already visible InstSci CloakBrowser window when normal browser automation stalls. Prefer text-visible controls over raw coordinates. Do not use this to enter account passwords, OTPs, or other credentials. CAPTCHA or Cloudflare checkbox clicks require explicit user confirmation, or the user should click them manually.

Typical sequence for Elsevier:

1. Confirm the `publisher-batch` process is still alive and the window title belongs to the target article or Elsevier page.
2. Use UI Automation to click public controls:
   - `Access through your organization`
   - institution search input such as `Organization name or email`
   - the user-selected institution result for this run
   - `Submit and continue`
   - Chrome PDF viewer toolbar `Download`
3. Hand off to the user for institution account login, password, 2FA, and CAPTCHA unless the user explicitly confirms a CAPTCHA checkbox click.
4. After the user completes verification, continue watching the run directory for `summary_partial.json`, `attempts.jsonl`, and `primary/pdfs/*.pdf`.

PowerShell patterns that worked on Windows:

```powershell
Add-Type -AssemblyName UIAutomationClient
Add-Type -AssemblyName UIAutomationTypes
$p = Get-Process chrome | Where-Object { $_.Path -match 'cloakbrowser' -and $_.MainWindowTitle -match 'ScienceDirect|Chromium|Find your organization' } | Select-Object -First 1
$w = [System.Windows.Automation.AutomationElement]::FromHandle($p.MainWindowHandle)
$all = $w.FindAll([System.Windows.Automation.TreeScope]::Descendants, [System.Windows.Automation.Condition]::TrueCondition)
```

Find by visible text and invoke:

```powershell
$target = $null
for ($i = 0; $i -lt $all.Count; $i++) {
  $e = $all.Item($i)
  if ([string]$e.Current.Name -eq 'Access through your organization' -and $e.Current.IsEnabled) {
    $target = $e
    break
  }
}
$target.GetCurrentPattern([System.Windows.Automation.InvokePattern]::Pattern).Invoke()
```

For institution search boxes exposed as `ComboBox`, set text through `ValuePattern`:

```powershell
$comboCond = New-Object System.Windows.Automation.PropertyCondition(
  [System.Windows.Automation.AutomationElement]::ControlTypeProperty,
  [System.Windows.Automation.ControlType]::ComboBox
)
$combo = $w.FindFirst([System.Windows.Automation.TreeScope]::Descendants, $comboCond)
$combo.GetCurrentPattern([System.Windows.Automation.ValuePattern]::Pattern).SetValue('Institution Name')
```

Do not fall back to hard-coded coordinates until text-based UIA has failed. If coordinates are necessary, first read the window bounds and take a visual checkpoint.

## Windows Page-Control Playbook

Use this when the visible CloakBrowser is on the right page but the normal Playwright/CloakBrowser automation is stalled, or when the user asks not to use Computer Use. UI Automation is suitable for public page controls and browser chrome controls; do not use it to enter passwords, OTPs, recovery codes, or other account secrets.

Workflow:

1. Find the real CloakBrowser window by process path and title.
   - Prefer `Get-Process chrome | Where-Object { $_.Path -match 'cloakbrowser' -and $_.MainWindowTitle }`.
   - If the target title is missing, enumerate all CloakBrowser windows before deciding the browser closed.
2. Restore and foreground the window before reading controls.
   - `Bounds=Empty` usually means minimized, hidden, or not foregroundable yet.
   - Use `ShowWindowAsync(..., 9)` and `SetForegroundWindow(...)`, then wait briefly.
3. Enumerate descendants and inspect names, types, enabled state, and bounds.
   - Capture a compact table before clicking when the page state is uncertain.
   - Match by visible `Name`, `ControlType`, and `IsEnabled`; prefer this over DOM guesses when the browser automation has stalled.
4. Operate by UIA pattern.
   - Buttons: `InvokePattern`.
   - Search boxes / comboboxes: `ValuePattern.SetValue(...)`.
   - If a combo result appears, enumerate list items and invoke the exact user-selected institution result.
5. Re-read page state after each click.
   - A successful click should change visible controls, title, loaded-PDF markers, run logs, or output files.
   - Do not conclude from click success alone.
6. Keep the InstSci listener state in mind.
   - UIA can click a viewer download button, but `publisher-batch` only captures the file if the Python listener is still waiting for a download event.

Window restore helper:

```powershell
Add-Type @'
using System;
using System.Runtime.InteropServices;
public class Win32Show {
  [DllImport("user32.dll")] public static extern bool ShowWindowAsync(IntPtr hWnd, int nCmdShow);
  [DllImport("user32.dll")] public static extern bool SetForegroundWindow(IntPtr hWnd);
}
'@
$p = Get-Process chrome | Where-Object { $_.Path -match 'cloakbrowser' -and $_.MainWindowTitle -match 'ScienceDirect|Chromium' } | Select-Object -First 1
[Win32Show]::ShowWindowAsync($p.MainWindowHandle, 9) | Out-Null
[Win32Show]::SetForegroundWindow($p.MainWindowHandle) | Out-Null
Start-Sleep -Milliseconds 700
```

Control inventory helper:

```powershell
Add-Type -AssemblyName UIAutomationClient
Add-Type -AssemblyName UIAutomationTypes
$w = [System.Windows.Automation.AutomationElement]::FromHandle($p.MainWindowHandle)
$all = $w.FindAll([System.Windows.Automation.TreeScope]::Descendants, [System.Windows.Automation.Condition]::TrueCondition)
$institutionPattern = [regex]::Escape('Institution Name')
$rows = @()
for ($i = 0; $i -lt $all.Count; $i++) {
  $e = $all.Item($i)
  $n = [string]$e.Current.Name
  if ($n -match 'Access|organization|Institution|PDF|Download|Sign|ScienceDirect' -or $n -match $institutionPattern) {
    $rows += [pscustomobject]@{
      Index = $i
      Name = $n
      Type = $e.Current.ControlType.ProgrammaticName
      Enabled = $e.Current.IsEnabled
      Bounds = $e.Current.BoundingRectangle.ToString()
    }
  }
}
$rows | Format-Table -AutoSize
```

Localized labels can be built with `[char]` codes to avoid mojibake in non-UTF-8 consoles. For example, the Chinese `Download` label in Chrome PDF viewer is:

```powershell
$downloadLabel = -join ([char]0x4e0b, [char]0x8f7d)
```

## Already At PDF Viewer

When the user says the visible CloakBrowser is already on the PDF page, preserve that state and recover the file without closing the browser.

1. Check whether the InstSci listener is still alive.
   - Look for the `python -m instsci.cli publisher-batch ...` process for that run.
   - If `primary/summary.json` or `attempts.jsonl` already says `pdf_not_captured` and no Python process remains, the listener is gone.
2. If the listener is alive, use UIA to click the PDF viewer localized `Download` button after visual/UIA evidence shows the PDF is loaded.
3. If the listener is gone, start a fresh single-DOI run with the same institution/profile and a new output directory, then click `Download` while it is waiting.
4. Confirm success from `primary/pdfs/*.pdf`, `complete/pdfs/*.pdf`, `attempts.jsonl`, and `primary/summary_partial.json`; require `status=success`, nonzero size, and preferably `verified_match=true`.

PowerShell launch pattern for a fresh single-DOI listener:

```powershell
$doiFile = ".\.tmp\one_doi.txt"
$out = ".\runs\browser_runs_resume\elsevier_retry_YYYYMMDD"
$argString = '-m instsci.cli publisher-batch "' + $doiFile + '" --publisher elsevier --institution "Institution Name" --output "' + $out + '" --login-timeout 600 --pdf-timeout 900'
Start-Process -FilePath "python" -ArgumentList $argString -WorkingDirectory (Get-Location) -RedirectStandardOutput "$out\publisher-batch.stdout.log" -RedirectStandardError "$out\publisher-batch.stderr.log" -WindowStyle Hidden
```

Quote institution names inside the argument string; passing `@(..., "Institution Name", ...)` to `Start-Process -ArgumentList` can split the institution into two CLI arguments.

UIA loop for the PDF viewer download button:

```powershell
Add-Type -AssemblyName UIAutomationClient
Add-Type -AssemblyName UIAutomationTypes
$p = Get-Process chrome | Where-Object { $_.Path -match 'cloakbrowser' -and $_.MainWindowTitle -match 'Simulation-Based|ScienceDirect|Applied Energy|Chromium' } | Select-Object -First 1
$w = [System.Windows.Automation.AutomationElement]::FromHandle($p.MainWindowHandle)
$all = $w.FindAll([System.Windows.Automation.TreeScope]::Descendants, [System.Windows.Automation.Condition]::TrueCondition)
$hasPdf = $false
$target = $null
$downloadLabel = -join ([char]0x4e0b, [char]0x8f7d)
$pdfLoadedLabel = "PDF " + (-join ([char]0x5df2, [char]0x52a0, [char]0x8f7d, [char]0x5b8c, [char]0x6bd5))
$pageContentMarker = -join ([char]0x9875, [char]0x5185, [char]0x5bb9)
for ($i = 0; $i -lt $all.Count; $i++) {
  $e = $all.Item($i)
  $n = [string]$e.Current.Name
  if ($n -eq $pdfLoadedLabel -or $n -match 'PDF loaded' -or ($n -match 'PDF' -and $n.Contains($pageContentMarker))) { $hasPdf = $true }
  if (-not $target -and $n -in @($downloadLabel, 'Download') -and $e.Current.IsEnabled) { $target = $e }
}
if ($hasPdf -and $target) {
  $target.GetCurrentPattern([System.Windows.Automation.InvokePattern]::Pattern).Invoke()
}
```

The `[char]` expressions avoid mojibake when the Markdown is read through a non-UTF-8 PowerShell console.

After recovery, update the final delivery layer, not only the run folder:

- copy the verified PDF into `final_pdfs` using the existing naming convention;
- update `reports/final_manifest.csv`;
- update `reports/final_manifest.json` without changing its current top-level shape;
- update `reports/final_report.md` counts. On Windows, write Markdown with UTF-8 BOM if PowerShell needs to display Chinese paths correctly.

## Command Patterns

Run from the repository root.

```powershell
instsci identity-policy
instsci papers .\dois.txt --publisher auto --institution "Institution Name" --output .\runs\papers
instsci publisher-batch .\dois.txt --publisher elsevier --institution "Institution Name" --output .\runs\elsevier
python -m unittest tests.test_acs_batch
git diff --check
```

If R is needed in this repo, use:

```powershell
& "D:\Program Files\R\R-4.5.3\bin\Rscript.exe" script.R
```

## Report Template

Use this structure for each DOI or publisher:

```text
publisher:
doi:
route_attempted:
institution:
result:
evidence:
next_action:
```

Allowed `result` labels:

- `browser verified`
- `HTTP preflight`
- `auth_required`
- `blocked`
- `unsupported`

Do not mark `unsupported`, `failed`, or `verified` from HTTP-only evidence.

## Manifest Consistency

Keep `final_report.md`, `final_manifest.csv`, and `final_manifest.json` aligned.

- `success`: PDF exists and verification passed.
- `unverified`: PDF exists but DOI/text verification did not pass or was inconclusive.
- `missing`: no final PDF was captured.

When a PDF is present but automation cannot prove DOI/text match, do not count it as missing.

## PDF Integrity Gate

For final delivery, especially after Science/AAAS or browser-viewer fallback
captures, require more than a saved file path:

- file bytes start with `%PDF-`;
- no UTF-8 replacement bytes (`EF BF BD`) near the header;
- if `%%EOF` appears, it is near the file tail rather than followed by a large
  trailing payload;
- PyMuPDF can open the file;
- page count is greater than zero;
- the first few pages contain extractable text or another task-appropriate
  verification signal;
- `verified_match=true` when the pipeline can match DOI/text metadata.

If an old run predates this gate, revalidate the files before reporting
success. A PDF that fails the gate is `unverified` or `missing` for final
delivery, even if the original attempt log says `success`.

## Security And State

- Do not write tokens, MCP endpoints, cookies, exported credentials, SSO screenshots with sensitive fields, or institutional secrets into skill files, docs, logs, or commits.
- Cookie jars are not full browser login state. Full state may include localStorage, IndexedDB, service workers, cache, TLS sessions, WAF challenge state, browser fingerprint state, and page-generated PDF tokens.
- Keep live CloakBrowser context open while it is serving as the active access broker.
- Do not manually call Xiaozhi notification scripts; task status notification is handled elsewhere.
