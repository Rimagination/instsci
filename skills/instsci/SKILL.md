---
name: instsci
description: Use when working with the InstSci project, publisher PDF retrieval, closed-access article verification, DOI batch downloads, CloakBrowser evidence, CARSI, Shibboleth, OpenAthens, WebVPN, publisher capability matrices, or InstSci CLI workflows.
---

# instsci

## Core Rule

Use this skill as the project entry point for InstSci work. The implementation and project-specific rules live in the repository root containing `AGENTS.md` and `pyproject.toml`.

## Startup

1. Work from the InstSci repository root unless the user explicitly names another checkout.
2. Read `AGENTS.md` before changing behavior or reporting publisher PDF results.
3. Before changing browser automation, human-assist handoff, session broker persistence, publisher profiles, attach-only control tools, or download-speed behavior, read `docs/browser-execution-layer.md`.
4. For continuation, recall, migration, or "previous task" questions, use the `chatmem` skill/MCP first. Treat indexed history as evidence, not approved startup rules.
5. For publisher PDF, closed-access, institution-login, or capability-matrix tasks, also read `instsci/data/institutional_identity_policy.json` or run:

```powershell
instsci identity-policy
```

6. For DOI batch recovery, browser stalls, PDF viewer recovery, publisher-specific gotchas, or final delivery repair, also read `references/publisher-pdf-workflow.md`.

## MCP Coordination

When InstSci MCP tools are available, use them as the structured context bridge before reading raw JSON files by hand:

- `get_institutional_identity_policy`: load route-selection policy before closed-access planning.
- `get_publisher_access_catalog`: inspect publisher route templates, login hints, persistence stores, and HTTP preflight limits.
- `get_publisher_browser_verification_matrix`: inspect prior browser-backed publisher evidence.
- `plan_publisher_pdf_workflow`: build the correct visible CLI command and identify whether a subscription institution is still required.

Use MCP `search_papers`, `get_paper_metadata`, and `fetch_paper` for metadata, Open Access lookup, DOI resolution, or non-final retrieval attempts. For publisher PDF downloads, closed-access verification, capability matrices, or final support verdicts, MCP is planning/context only; the actual evidence must come from the visible CloakBrowser workflow started by `instsci papers`, `instsci publisher-batch`, `PublisherBatchDownloader`, or `ACSCloakBatchDownloader`.

If MCP output and repository files disagree, treat `AGENTS.md` plus `instsci/data/*.json` as the source of truth and mention the mismatch.

## Evidence Standard

Final publisher PDF verdicts require the visible built-in CloakBrowser workflow. `curl`, `requests`, DOI resolution, `publisher-doctor`, route construction, logs, DOM state, URLs, and cookie exports are HTTP preflight only.

Accepted browser-backed routes include:

```powershell
instsci papers dois.txt --publisher auto --institution "Institution Name" --output .\runs\papers
instsci publisher-batch dois.txt --publisher acs --institution "Institution Name" --output .\runs\acs
```

Code-level work may use `PublisherBatchDownloader`, `ACSCloakBatchDownloader`, or the same visible built-in browser context.

## Default Publisher Workflow

Use the current long-lived defaults for closed-access publisher work:

- `instsci papers` and `instsci publisher-batch` default to the persistent publisher broker.
- `--human-assist` is enabled by default for publisher browser runs.
- Manual login and broker TTL defaults are three days (`259200` seconds).
- `--overnight` is a compatibility flag and a way to force careful single-context settings; do not present it as required for normal long runs.
- Use `--no-broker --keep-browser-open` only for deliberate one-shot repairs where the broker is unsuitable.
- Use `session-broker-status -p <publisher>` and `session-broker-status -p <publisher> --json` to inspect live broker state before starting large supplement runs.

Prefer grouping DOI batches by publisher so one broker can reuse the same visible CloakBrowser profile and institution state. If the publisher, institution, profile dir, proxy identity, or extension identity differs, do not reuse the broker.

## Agent-Browser Coordination

Use `agent-browser` only as an optional control layer for an already running InstSci CloakBrowser. CloakBrowser remains the browser runtime, fingerprint/profile owner, cookie/storage owner, and lifecycle owner.

Use it only when the tool is actually available and the current InstSci run has emitted a CDP port or WebSocket endpoint. Do not invent a port, do not rely on auto-discovery, and do not install or start a second browser for publisher PDF evidence.

Allowed pattern:

```powershell
agent-browser --session instsci-<publisher> connect <cdp-port-or-url>
agent-browser --session instsci-<publisher> snapshot -i
agent-browser --session instsci-<publisher> screenshot diagnostics\checkpoint.png --annotate
agent-browser --session instsci-<publisher> click @e1
```

Use the CDP port or WebSocket URL emitted by the running InstSci/CloakBrowser process; do not invent a port. If the run does not expose a CDP endpoint, use the normal Playwright/CloakBrowser workflow or the Windows UIA fallback from `references/publisher-pdf-workflow.md`.

Do not use `agent-browser` to launch a second browser, choose a profile, load/save auth state, clear cookies/storage, or close CloakBrowser. Avoid unqualified `agent-browser open`, `--profile`, `--state`, `--session-name`, `auth save/login`, `cookies clear`, `storage clear`, `--auto-connect`, and `agent-browser close` for InstSci publisher work unless the user explicitly asks for a separate non-InstSci browser test.

When attached, use `agent-browser` for accessibility snapshots, annotated screenshots, stable `@eN` refs, public page controls, and page-state inspection. It may click controls such as `Access through your organization`, institution search results, `PDF`, or viewer `Download` only when the user-selected institution and current browser state are clear. Never use it to enter passwords, OTPs, recovery codes, CAPTCHA answers, or institution credentials.

## BrowserAct And OpenCLI Boundary

BrowserAct's free/open-source surface was evaluated as a design reference, not as an InstSci dependency. Do not install, configure, or run BrowserAct by default for InstSci publisher PDF work. The useful ideas are already being internalized as InstSci-native browser actions, human-handoff state, session inventory, and publisher skills.

OpenCLI Browser Bridge and `agent-browser` are optional attach-only diagnostics. They may help observe a page, collect annotated screenshots, discover selectors, or click safe public controls inside an InstSci-owned CloakBrowser session. They must not own login state, enter secrets, solve CAPTCHA, replace CloakBrowser, capture PDFs, verify PDFs, or mark final success.

## Institution Route

- Do not default to Tsinghua University or any other school.
- Resolve subscription institution in this order: explicit `--institution`, `config.carsi_idp_name`, `config.school`, then ask the user.
- Prefer publisher broker, Shibboleth, OpenAthens, CARSI, or configured WAYFless institution links before WebVPN.
- Use WebVPN only when the configured institution has a WebVPN gateway and that route is browser-verified for the publisher.
- Do not treat `cookies.json` or `carsi_cookie_dir/*.json` as a full reusable login state; they are preflight/supporting assets, not final evidence.

## KeePassXC Credential Assist

KeePassXC Auto-Type is the approved free local path for reducing repeated
institution-password typing without exposing credentials to InstSci.

- Users store their own institution IdP entry locally in KeePassXC; InstSci
  must not read entries, export passwords, inspect password fields, or use the
  clipboard for credentials.
- Trigger the configured global Auto-Type hotkey only after the user confirms
  KeePassXC is unlocked and the correct login field is focused.
- In the KeePassXC Chinese UI, global Auto-Type settings are under
  `齿轮设置 -> 常规 -> 自动输入`. Do not confuse this with the left-sidebar
  `浏览器集成` page, which is for the KeePassXC-Browser extension and is not
  required for CloakBrowser Auto-Type.
- The `全局自动输入快捷键` field must be non-empty. If it is blank, KeePassXC
  has not registered a global Auto-Type hotkey, so sending `Ctrl+Alt+A` from
  InstSci will do nothing unless the user first configures that exact shortcut
  or passes the configured shortcut with `--hotkey`.
- The entry URL should be the institution IdP host, such as
  `https://idp.example.edu/`, not the publisher article URL.
- For first-time users, point them to `docs/keepassxc-autotype.md` and the
  human-assist `first_time_setup_steps`. They need: one local KeePassXC entry,
  IdP URL, entry-level Auto-Type enabled, `{USERNAME}{TAB}{PASSWORD}`, a window
  association, and a non-empty global Auto-Type hotkey.
- Entry-level Auto-Type is configured by selecting the institution entry, then
  `条目 -> 编辑条目 -> 自动输入` in the edit-entry sidebar. The entry should
  have `为此条目启用自动输入` checked, use `{USERNAME}{TAB}{PASSWORD}`, and
  include a window association such as `*清华大学用户电子身份服务系统*`.
- For global Auto-Type, also configure a window association matching the
  visible CloakBrowser login title. Use the non-secret hint from
  `human_assist/assist_state.json`, such as `*清华大学用户电子身份服务系统*`.
- Prefer `{USERNAME}{TAB}{PASSWORD}` until the user has verified the login
  page. The login/submit button, SMS, TOTP, push approval, CAPTCHA, recovery
  prompts, and final submit remain user actions.

## Reporting

For publisher PDF work, report each DOI or publisher with `publisher`, `doi`, `route_attempted`, `institution`, `result`, `evidence`, and `next_action`.

Use these status meanings:

- `browser verified`: PDF captured or blocker verified in visible CloakBrowser with screenshot-backed checkpoints.
- `HTTP preflight`: HTTP-only evidence; not a final capability verdict.
- `auth_required`: user must complete SSO, 2FA, CAPTCHA, or institution selection.
- `blocked`: visible browser evidence shows a challenge, error, or publisher-side blocker.
- `unsupported`: only after browser-verified evidence rules out the route.

For final manifests, keep Markdown, CSV, and JSON counts consistent. `success` means downloaded and verified; `unverified` means a PDF exists but DOI/text verification is insufficient; `missing` means no PDF was captured.

## Detailed Reference

For recent gotchas, publisher-specific notes, visible-browser UI fallback steps, report-count rules, and verification commands, read `references/publisher-pdf-workflow.md` when the task touches publisher PDFs or DOI batches.

## Safety

- Keep CloakBrowser visible for SSO, CAPTCHA, WAF, Cloudflare, and publisher verification.
- After clicking PDF, institutional access, OpenAthens/Shibboleth/CARSI, cookie prompts, or verification prompts, inspect a screenshot before concluding success or failure.
- Visible UI fallback may click public publisher controls such as `Access through your organization`, institution search results, or PDF viewer `Download`, but never fill passwords, OTPs, or account credentials.
- Do not manually call Xiaozhi notification scripts.
- Never write Xiaozhi MCP endpoints, tokens, institution credentials, cookies, or other secrets into docs, code, logs, skills, or commits.
