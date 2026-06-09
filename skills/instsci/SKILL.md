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
3. For continuation, recall, migration, or "previous task" questions, use the `chatmem` skill/MCP first. Treat indexed history as evidence, not approved startup rules.
4. For publisher PDF, closed-access, institution-login, or capability-matrix tasks, also read `instsci/data/institutional_identity_policy.json` or run:

```powershell
instsci identity-policy
```

## Evidence Standard

Final publisher PDF verdicts require the visible built-in CloakBrowser workflow. `curl`, `requests`, DOI resolution, `publisher-doctor`, route construction, logs, DOM state, URLs, and cookie exports are HTTP preflight only.

Accepted browser-backed routes include:

```powershell
instsci papers dois.txt --publisher auto --institution "Institution Name" --output .\runs\papers
instsci publisher-batch dois.txt --publisher acs --institution "Institution Name" --output .\runs\acs
```

Code-level work may use `PublisherBatchDownloader`, `ACSCloakBatchDownloader`, or the same visible built-in browser context.

## Institution Route

- Do not default to Tsinghua University or any other school.
- Resolve subscription institution in this order: explicit `--institution`, `config.carsi_idp_name`, `config.school`, then ask the user.
- Prefer publisher broker, Shibboleth, OpenAthens, CARSI, or configured WAYFless institution links before WebVPN.
- Use WebVPN only when the configured institution has a WebVPN gateway and that route is browser-verified for the publisher.
- Do not treat `cookies.json` or `carsi_cookie_dir/*.json` as a full reusable login state; they are preflight/supporting assets, not final evidence.

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
