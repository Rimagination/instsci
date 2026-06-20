# InstSci Browser Execution Layer

InstSci should learn the product mechanisms from modern browser-agent tools,
but not replace the closed-access PDF workflow with a separate browser runtime.
The useful lesson is the shape of the execution layer: clear browser actions,
human handoff, durable sessions, publisher-specific skills, and explicit
identity boundaries.

## Goal

Make publisher PDF downloads feel like one continuous workflow:

1. InstSci plans the route and opens the visible CloakBrowser session.
2. Deterministic publisher logic performs known steps.
3. A small browser action layer handles safe page observation and public clicks.
4. If an institution login, CAPTCHA, WAF, or MFA checkpoint appears, InstSci
   pauses with clear state, the user completes it in CloakBrowser, and the same
   broker session resumes.
5. PDF capture and final verdicts remain owned by InstSci's CloakBrowser
   downloader and PDF verifier.

This document turns the BrowserAct-style ideas into InstSci-native work. It is
not a BrowserAct integration plan.

## Non-Negotiable Boundaries

- CloakBrowser is the browser runtime for publisher PDFs.
- `browser_profile_dir`, browser fingerprint, cookies, storage, download
  listeners, PDF byte capture, and final reports belong to InstSci.
- OpenCLI or `agent-browser` may only attach to an already running InstSci
  CloakBrowser session for observation and safe public controls.
- No external control layer may launch a second browser, import/export auth
  state, clear cookies/storage, close CloakBrowser, or become the PDF download
  engine.
- Passwords, OTPs, recovery codes, CAPTCHA answers, and institution credentials
  are entered only by the user in the visible CloakBrowser window.
- Final closed-access verdicts require visible CloakBrowser evidence. DOM state,
  URLs, cookies, HTTP responses, and external-agent logs are supporting
  diagnostics only.

## Capability Layers

| Layer | Owner | Purpose | Final PDF Evidence |
| --- | --- | --- | --- |
| Planning | CLI/MCP | Resolve publisher, institution, route, output, broker identity | No |
| Browser runtime | CloakBrowser | Persist profile, keep visible session alive, own downloads | Yes |
| Publisher skill | Publisher profiles and deterministic code | Known login/PDF selectors, URL templates, blockers, retries | Supporting |
| Assistive control | OpenCLI or `agent-browser` attached to CloakBrowser | Snapshot, annotated screenshot, safe public click, element discovery | No |
| Human handoff | Human-assist page and broker pause/resume | Let user clear SSO/CAPTCHA/MFA in the same browser session | Supporting |
| Verification | PDF byte/text/DOI/title checks | Reject HTML, corrupted bytes, wrong article, unverified PDFs | Yes |

## Browser Action Model

InstSci should expose browser work as a small set of observable actions instead
of burying every action inside publisher-specific scripts:

- `observe`: capture URL, title, challenge classification, visible text markers,
  screenshot path, and key interactive controls.
- `find`: identify public controls such as `PDF`, `Download`, `Access through
  your organization`, institution search inputs, and cookie prompts.
- `click_public`: click only non-secret page controls selected by deterministic
  profile logic or an attached assistive control layer.
- `wait_stable`: wait for navigation, network quiet, viewer load, or configured
  marker text.
- `capture_pdf`: listen for PDF responses/downloads/viewers and save bytes.
- `verify_pdf`: run the PDF integrity and article-match gates before declaring
  success.
- `pause_for_user`: publish a handoff state, keep the browser alive, and stop
  sending work into a stale page.
- `resume`: continue from the same broker profile and remaining DOI queue.

The key design rule is that `click_public` may help reach the PDF, but
`capture_pdf` and `verify_pdf` decide success.

First-pass implementation lives in `instsci.browser_actions`. It defines the
shared action names, safe public-click classification, non-secret page
observation, and human-handoff state normalization used by the human-assist
surface. Publisher-specific code can adopt the primitives incrementally without
changing the CloakBrowser ownership boundary.

## Human Handoff State Machine

Human intervention should be explicit and resumable, not a vague timeout.

| State | Meaning | Required State Data | Next Action |
| --- | --- | --- | --- |
| `running` | Browser is processing a DOI normally | publisher, DOI, output dir, current action | Keep processing |
| `checkpoint_detected` | SSO, CAPTCHA, WAF, MFA, or institution prompt was seen | challenge kind, URL, title, screenshot, action text | Publish human-assist state |
| `reauth_required` | Broker should not process more records until user clears the visible prompt | paused job id, remaining count, resume command, browser identity | User acts in CloakBrowser |
| `ready_to_resume` | User reports the visible prompt is done or health check no longer sees the blocker | broker id, paused jobs, latest health | Run resume |
| `resuming` | Paused records are requeued into the same broker | job id, output dir, attempt cache | Continue deterministic workflow |
| `attention_required` | Failure is not obviously fixed by login, such as entitlement or verification mismatch | manifest path, reasons, evidence paths | Inspect report before retrying |

The local human-assist page should remain a status surface, not a credential
surface. It should show the visible screenshot, pause reason, current DOI,
remaining count, output path, and exact resume command.

KeePassXC Auto-Type is the approved free local credential-assist path when the
user wants reduced typing without exposing passwords to InstSci. InstSci may
show domain checks, focus the visible login field, and trigger the configured
KeePassXC global Auto-Type hotkey after user confirmation. KeePassXC remains
the credential owner. InstSci must not read KeePassXC entries, export
passwords, inspect password field values, or move credentials through the
clipboard. See `docs/keepassxc-autotype.md`.

This credential-assist flow must remain institution-neutral. Users create their
own KeePassXC entries for their own identity-provider host, not for a hard-coded
school and not for publisher domains. If the IdP host is unknown, pause at the
visible institution login page and use only the address-bar hostname as the
configuration clue; do not store full redirect URLs that may contain transient
tokens. Prefer Auto-Type sequences without `{ENTER}` until the user has verified
the page, and keep the login/submit button, SMS, TOTP, push approval, CAPTCHA,
recovery prompts, and final login confirmation as user actions.

For global KeePassXC Auto-Type, include a window-association hint derived from
the visible login page title whenever possible. This is often more reliable
than relying on the URL alone because the focused target is a CloakBrowser
window. For example, a Tsinghua IdP page with title
`清华大学用户电子身份服务系统 - Chromium` should surface the non-secret hint
`*清华大学用户电子身份服务系统*`.

Implementation detail: human-assist state packets include a structured
`credential_assist` object when `credential_warning` is active. The local status
page, `/status.json`, broker re-auth pause files, and reauth terminal summaries
can all surface the KeePassXC command from that object. The object must contain
only provider/mode, expected hostname, optional window-association hint, command
template, setup doc, concise action steps, and a first-time setup checklist for
users who have never configured KeePassXC Auto-Type.

Human-assist updates now normalize known checkpoint reasons such as
`sso_required` and `challenge_or_viewer_timeout` into `reauth_required` while
preserving compatibility states such as `institution_login_required`. The
original reason is kept as `status_reason` so operators can see both the coarse
state and the precise blocker.

## Session And Browser Inventory

The project should make session state easy to inspect before long runs.
`instsci session-broker-status` is the right foundation and should behave like
a control console:

- show running/stopped broker state per publisher;
- show profile dir, institution, proxy identity, extension identity, and PID;
- show active job, queued jobs, paused jobs, and remaining record counts;
- show latest health check, health URL, and short non-secret reason;
- show last summary path and last attention message;
- show whether resume should use `--job-id` or `--all`.

The status command should never print cookies, passwords, tokens, OTPs, full
page text, or raw institution secrets.

## Publisher Skills

Each publisher should have a small, reusable "skill" encoded in project data
and deterministic code. The large model should only handle surprise states; it
should not rediscover the same selectors on every run.

A publisher skill should cover:

- canonical article and PDF URL patterns;
- institutional login entry points;
- institution search selectors and placeholder text;
- access markers that indicate entitlement or subscription preview;
- PDF buttons, viewer download controls, and response URL filters;
- common blocker signatures such as Cloudflare, PerfDrive, CRA Solve, or
  publisher-specific interstitials;
- retry rules, post-login waits, and when to pause for user;
- sample DOI smoke cases for route and selector checks;
- known failure repairs such as corrupted `pdfdirect` bytes or stale broker
  queues.

Publisher skills must not contain a hard-coded default institution. Institution
text comes from `--institution`, config, or a user prompt.

## Multi-Task And Identity Rules

InstSci should optimize for fewer logins without mixing identities:

- Reuse a running broker only when publisher, profile dir, institution, proxy
  identity, and extension identity match.
- Group DOI batches by publisher so one broker handles a coherent queue.
- Keep one browser context in careful/balanced modes; use faster concurrency
  only after the publisher is already logged in and stable.
- Park new jobs as paused when a broker is already `reauth_required`.
- Do not reopen a one-shot browser and assume it preserved sessionStorage,
  TLS/WAF state, IdP transient state, or page-generated PDF tokens.
- Record attempt cache and paused jobs so supplement runs do not repeat known
  failures blindly.

## OpenCLI And Agent-Browser Use

Use attach-only control when it helps inspect or click a page that deterministic
publisher logic does not understand:

- collect accessibility or DOM snapshots;
- generate annotated screenshots with stable refs;
- discover candidate selectors for later deterministic publisher profiles;
- click public controls such as `PDF`, `Download`, cookie prompts, or the
  already-selected institution result;
- verify progress by returning to screenshot plus InstSci artifacts.

Do not use attach-only tools to enter secrets, solve CAPTCHA, close the browser,
replace download waiting, or declare final PDF success.

## Implementation Checklist

- Keep `docs/architecture.md` as the high-level map and link this document as
  the browser execution detail.
- Keep `docs/performance.md` focused on persistence, broker reuse, speed knobs,
  and handoff recovery commands.
- Keep `docs/opencli-bridge.md` focused on OpenCLI as an optional attached
  observation/control layer.
- Make every publisher workflow summary include non-secret browser identity,
  extension identity, broker status, attempt cache, and human-assist URL when
  present.
- Improve publisher profiles before adding new agent-driven behavior.
- Add tests for every new state transition, summary field, pause/resume rule,
  and safety boundary.

## Current Status

| Area | Status | Evidence |
| --- | --- | --- |
| Visible browser runtime | Implemented | `instsci publisher-batch`, `instsci papers`, `PublisherBatchDownloader` |
| Long-lived publisher broker | Implemented | `session-broker-status`, broker state, queue, paused jobs |
| Manual handoff page | Implemented | `--human-assist`, `assist_state.json`, screenshot route |
| Reauth pause/resume | Implemented | `reauth_required`, paused job files, `session-broker-resume --all` |
| PDF byte integrity gate | Implemented | PDF header/HTML/corruption checks and text verification |
| OpenCLI attach-only diagnostics | Implemented as optional layer | `opencli-bridge-doctor`, extension identity summary |
| Formal browser action API | Implemented first pass | `instsci.browser_actions`, browser observation, safe click labels, action names |
| Explicit handoff states | Implemented first pass | normalized human-assist status, `status_reason`, pause/resume browser actions |
| Publisher skills | Partially implemented | Publisher profiles hold selectors and URL rules; skill checklist should guide expansion |
| Session inventory UX | Implemented and still improving | `session-broker-status` table output plus `--json` inventory |

## Next Engineering Improvements

- Add publisher-profile fields for sample DOI smoke tests, known blocker
  signatures, and repair hints.
- Promote selector discoveries from OpenCLI or `agent-browser` snapshots back
  into publisher profiles with tests.
- Add regression tests that ensure attach-only tools cannot mark final PDFs as
  `browser verified`.
- Add compact control-console docs for common operator decisions: resume latest
  paused job, resume all, stop stale broker, rerun with disabled extensions,
  and retry with the same profile.

## Done Criteria

A browser execution improvement is complete only when:

- the behavior is documented here or in a linked md file;
- the CLI exposes a clear command or status surface;
- tests cover the state transition or safety boundary;
- final PDF success still depends on CloakBrowser PDF capture and verification;
- no default institution, credential, cookie value, token, OTP, or CAPTCHA
  answer is stored or printed.
