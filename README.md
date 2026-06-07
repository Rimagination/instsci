# InstSci

InstSci is an academic paper retrieval toolkit for researchers who already have legitimate access through a university, library, or research institution.

This project used to be called `vpnsci`. The GitHub repository is still `Rimagination/vpnsci`, but the current package and command name are `instsci`. Legacy `vpnsci.*` Python imports are kept as compatibility aliases.

## What It Does

InstSci tries the least intrusive route first, then escalates only when needed:

1. Look for Open Access copies through Unpaywall, arXiv, Semantic Scholar, and publisher metadata.
2. If the paper is closed-access, reuse your own institutional entitlement through a visible browser session.
3. Keep browser sessions locally so repeated downloads usually do not require repeated sign-in.
4. If full text cannot be retrieved, return useful metadata, diagnostics, and the next action to try.

InstSci is designed for AI-agent workflows as well as command-line use. It exposes an MCP server, so tools such as Claude Code, Cursor, OpenCode, and other MCP-capable agents can search, fetch, and diagnose papers through natural-language requests.

## Key Features

- Open Access first: Unpaywall, arXiv, Semantic Scholar, publisher DOI metadata.
- Browser-backed institutional access through CloakBrowser.
- Federated login support for Shibboleth, OpenAthens, CARSI-style flows, and publisher institution pickers.
- Campus access configuration for 100+ Chinese universities and library portals, including WebVPN, EZproxy, EasyConnect, and aTrust-style connector setups.
- Publisher-specific PDF workflows for ACM, ACS, AIP, AMS, Annual Reviews, Copernicus, Frontiers, IEEE, IOP, MDPI, Oxford Academic, PLOS, PNAS, Royal Society Publishing, RSC, Science, Springer Nature, Wiley, World Scientific, and more.
- Long-lived browser session broker for batch publisher downloads.
- Session diagnostics with `session-doctor`.
- HTTP route preflight with `publisher-doctor`, clearly separated from browser-verified access.

## Install

```bash
git clone https://github.com/Rimagination/vpnsci.git
cd vpnsci
pip install -e .
```

After installation, the primary commands are:

```bash
instsci --help
instsci-mcp
```

## First Setup

Configure your institution once:

```bash
instsci setup --school "Your Institution"
```

If your institution name in publisher login pages differs from the campus access name, set it explicitly:

```bash
instsci setup --school "Your Institution" --federated-school "Institution Name In OpenAthens"
```

You can check the local setup without changing it:

```bash
instsci setup --check
```

For institutions that need a local campus connector, connect with your institution-approved client first, then set the local connector:

```bash
instsci config-cmd --connector-url socks5://127.0.0.1:1080
```

InstSci does not receive your password. When SSO, 2FA, CAPTCHA, or publisher verification appears, complete it manually in the visible CloakBrowser window.

## Common Workflows

Search papers:

```bash
instsci search "perovskite solar cells" --limit 10
```

Fetch one DOI, preferring Open Access and falling back to configured institutional access:

```bash
instsci fetch "10.1038/s41586-020-2649-2"
```

Fetch a DOI list with the recommended browser-backed workflow for closed-access publisher PDFs:

```bash
instsci papers dois.txt --publisher auto --output ./runs/papers
```

Use a specific publisher profile:

```bash
instsci publisher-batch dois.txt --publisher acs --output ./runs/acs
```

Inspect reusable browser sessions:

```bash
instsci session-doctor
instsci session-doctor --publisher ieee
```

Inspect the identity-routing policy:

```bash
instsci identity-policy
```

Run HTTP-only route diagnostics:

```bash
instsci publisher-doctor --publisher all
```

`publisher-doctor` is only a preflight tool. It can identify route templates and likely blockers, but a final claim that a publisher PDF can or cannot be retrieved must come from a visible CloakBrowser workflow such as `instsci papers` or `instsci publisher-batch`.

## MCP Usage

Register the MCP server with an MCP-capable agent:

```bash
claude mcp add instsci -- instsci-mcp
```

Example agent requests:

```text
帮我找几篇钙钛矿太阳能电池的最新论文。
这篇论文的全文是什么？DOI: 10.1038/s41586-020-2649-2
把这个 DOI 列表用机构访问下载成 PDF，并给我失败诊断。
```

## Institutional Access Policy

InstSci uses `auto` identity routing for closed-access PDFs:

- Prefer publisher and federated institutional login when the publisher supports it.
- Use WebVPN only when the configured institution provides a WebVPN gateway and that route works in the active browser context.
- Do not treat exported WebVPN cookies as a full reusable login state.
- Keep the visible CloakBrowser context alive for SSO, CAPTCHA, Cloudflare, WAF checks, and publisher-generated PDF tokens.

In practice, this means WebVPN is useful but not universal. For many publishers, the most reliable route is one institutional login through the publisher's own OpenAthens/Shibboleth flow, then reuse the persistent browser profile for later downloads.

## Configuration Files And Local Data

By default, InstSci stores local runtime state under `~/.instsci`:

- `config.json`: user configuration.
- `chrome-profile/`: persistent CloakBrowser profile.
- `carsi_cookies/`: per-publisher session artifacts.
- `papers/`: default output directory.
- `cache/`: metadata and retrieval cache.

For compatibility with older installs, InstSci can still read legacy `~/.vpnsci` configuration when no new config exists.

## Requirements

- Python 3.10 or newer.
- CloakBrowser, installed through the Python dependency set.
- A legitimate institutional subscription or Open Access source for closed-access content.
- Optional: Docker or a local connector only for institutions that require EasyConnect/aTrust-style access.

## Compliance Notes

InstSci is a research utility for accessing academic resources that you are authorized to use. It is not a VPN service, does not implement network tunneling protocols, and does not bypass publisher or institution verification. Users are responsible for following local law, institutional network policy, and publisher license terms.

## License

[MIT](LICENSE)
