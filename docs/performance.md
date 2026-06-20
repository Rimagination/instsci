# InstSci Download Speed And Persistence

Closed-access publisher downloads are fastest when InstSci avoids rebuilding
the same authenticated browser state. The default recommendation is:

```powershell
instsci papers dois.txt --publisher auto --institution "Your Institution"
```

This document covers speed, persistence, and recovery commands. The broader
browser execution model and handoff state machine are documented in
`docs/browser-execution-layer.md`.

## What Is Persisted

- `browser_profile_dir` persists browser storage such as cookies,
  localStorage, IndexedDB, service workers, and cache between browser starts.
- The session broker keeps one visible CloakBrowser context alive per publisher
  while the broker TTL is active.
- `carsi_cookie_dir/<publisher>.json` and exported cookies are supporting
  HTTP-preflight assets only. They are not a full replacement for the live
  browser context.
- `attempts.jsonl` records prior attempts for one-shot batch retries and
  skip-attempted workflows.

## Broker Reuse Rules

InstSci only reuses a running publisher broker when the stored identity still
matches the requested run:

- publisher key
- browser profile directory
- subscription institution, when recorded
- browser proxy identity, when configured
- browser extension set, when configured

If a broker is running but the identity differs, InstSci falls back to a
one-shot visible CloakBrowser workflow instead of sending work into a stale or
wrong login state.

## Speed Knobs

- Use `instsci papers` for repeated closed-access runs; it defaults to broker
  mode and groups mixed DOI lists by publisher.
- Use `instsci publisher-batch ...` for focused single-publisher batches. It
  also defaults to the live publisher broker, so supplement runs reuse the same
  authenticated browser when the identity matches.
- Keep the same `--institution`, `--browser-profile`, proxy, and extension
  settings when you want to reuse a login.
- The default broker TTL is 3 days. Use `--broker-ttl` only when you need a
  different active-session window.
- Use `--speed fast -j 2` only after the publisher is already logged in and
  stable. It opens more fallback browser contexts, which can increase challenge
  risk.
- Prefer grouping by publisher. `--publisher auto` already batches records by
  inferred publisher, so each broker can process its own DOI group.

## Long-Lived Runs

Long-lived broker runs are the default for publisher PDFs. This is the normal
mode for large batches where institution login may expire while the run is
unattended:

```powershell
instsci publisher-batch science-dois.txt -p science --institution "Your Institution"
instsci papers mixed-dois.txt -p auto --institution "Your Institution"
```

The default long-lived preset:

- uses broker mode
- keeps one browser worker
- keeps the broker alive for at least 3 days
- waits up to 3 days at an SSO/CAPTCHA/institution page before failing that
  login step

`--overnight` remains as a compatibility flag and as an explicit way to force
the conservative single-context settings if other speed flags were provided,
but it is no longer required for normal long downloads.

It does not save institution passwords, OTPs, CAPTCHA answers, or recovery
codes. If the institution or publisher ends the session, InstSci can keep the
visible browser open and wait for manual re-authentication; it cannot bypass or
silently refresh credentials after the provider revokes the session.

## Broker Health Checks

While a publisher broker is idle, InstSci runs a lightweight session health
check about every 30 minutes by default. The check reuses the already visible
CloakBrowser context and opens one publisher sample or article page for the
current profile. It does not download PDFs, submit forms, solve challenges, or
save page body text.

The broker only records non-secret health fields: status, checked-at time,
checked URL, and a short reason. If the health check sees a logged-out,
institution-login, or challenge state, the broker status becomes
`reauth_required`. Check the status command, complete the visible prompt in
CloakBrowser, then resume queued work when a paused job is present.

If new DOI work is submitted while the broker is already marked
`reauth_required`, InstSci does not run that batch through a stale login state.
It saves the DOI records as a paused broker job, writes a `summary.json` in the
requested output directory, and updates the human-assist state file with the
resume command.

## Human Handoff

Publisher browser commands expose a local human-assist page by default while
the visible CloakBrowser workflow is running. The page is bound to localhost by
default and records non-secret state such as publisher, DOI, page title, current
action, checkpoint screenshots, diagnostic paths, and broker resume commands.
This maps to the browser execution states in
`docs/browser-execution-layer.md`: `checkpoint_detected`,
`reauth_required`, `ready_to_resume`, and `resuming`.

Use the visible CloakBrowser window for institution passwords, OTPs, recovery
codes, and CAPTCHA answers. Do not enter credentials into the InstSci
human-assist page. To disable the local handoff page for a run:

```powershell
instsci publisher-batch dois.txt -p science --no-human-assist
```

Check a live broker with:

```powershell
instsci session-broker-status -p science
```

Agents or dashboards can request the same non-secret inventory as JSON:

```powershell
instsci session-broker-status -p science --json
```

The status output includes the broker process state, active job id, active
output directory, queued jobs, paused jobs, last job status, last summary path,
latest session health check, and any attention message from the previous run.
If the last job or health check says `reauth_required`, complete the visible
SSO/CAPTCHA/institution prompt in CloakBrowser and leave that browser open. If
InstSci has kept the remaining DOI records in a paused broker job, resume that
paused work:

```powershell
instsci session-broker-resume -p science --job-id <paused-job-id>
```

When several batches have been parked behind the same manual login checkpoint,
complete the visible prompt once and resume them together:

```powershell
instsci session-broker-resume -p science --all
```

If the last job says `attention_required` rather than `reauth_required`, inspect
the manifest reason first. It may be an entitlement or verification problem,
not a login checkpoint that can be fixed by re-authentication.

For large supplement runs, treat `session-broker-status` as the control
console. Check it before launching new work so the next batch either reuses the
right live identity or parks behind the existing `reauth_required` checkpoint
instead of forcing another login.

## One-Shot Browser Mode

`--no-broker` intentionally runs a one-shot CloakBrowser context and closes it
when the command exits. A later command may reuse the same profile directory,
but it cannot rely on process-local state such as sessionStorage, IdP temporary
state, TLS/session connection state, WAF challenge state, or page-generated
PDF tokens.

For diagnostics where a one-shot browser must stay alive, pass:

```powershell
instsci publisher-batch dois.txt -p science --no-broker --keep-browser-open
```

That keeps the CLI process and visible CloakBrowser open until Ctrl+C. For
normal supplement runs, prefer the broker instead because it lets the command
return while the publisher browser context remains alive in the broker process.
