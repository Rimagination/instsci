# InstSci

Language / 语言: [English](#english) | [中文](#chinese)

## English

InstSci is an academic paper retrieval toolkit for researchers who already have legitimate access through a university, library, or research institution.

InstSci was formerly named `vpnsci`. The current package and command name are `instsci`; legacy `vpnsci.*` Python imports are kept as compatibility aliases.

### What It Does

InstSci tries the least intrusive route first, then escalates only when needed:

1. Look for Open Access copies through Unpaywall, arXiv, Semantic Scholar, and publisher metadata.
2. If the paper is closed-access, reuse your own institutional entitlement through a visible browser session.
3. Keep browser sessions locally so repeated downloads usually do not require repeated sign-in.
4. If full text cannot be retrieved, return useful metadata, diagnostics, and the next action to try.

InstSci is designed for AI-agent workflows as well as command-line use. It exposes an MCP server, so MCP-capable agents can search, fetch, and diagnose papers through natural-language requests.

### Key Features

- Open Access first: Unpaywall, arXiv, Semantic Scholar, and publisher DOI metadata.
- Browser-backed institutional access through CloakBrowser.
- Federated login support for Shibboleth, OpenAthens, CARSI-style flows, and publisher institution pickers.
- Campus access configuration for 100+ Chinese universities and library portals, including WebVPN, EZproxy, EasyConnect, and aTrust-style connector setups.
- Publisher-specific PDF workflows for ACM, ACS, AIP, AMS, Annual Reviews, Copernicus, Frontiers, IEEE, IOP, MDPI, Oxford Academic, PLOS, PNAS, Royal Society Publishing, RSC, Science, Springer Nature, Wiley, World Scientific, and more.
- Long-lived browser session broker for batch publisher downloads.
- Session diagnostics with `session-doctor`.
- HTTP route preflight with `publisher-doctor`, clearly separated from browser-verified access.

### Install

```bash
git clone https://github.com/Rimagination/instsci.git
cd instsci
pip install -e .
```

After installation, the primary commands are:

```bash
instsci --help
instsci-mcp
```

### First Setup

Configure your institution once:

```bash
instsci setup --school "Your Institution"
```

If your institution name in publisher login pages differs from the campus access name, set it explicitly:

```bash
instsci setup --school "Your Institution" --federated-school "Institution Name In OpenAthens"
```

Check the local setup without changing it:

```bash
instsci setup --check
```

For institutions that need a local campus connector, connect with your institution-approved client first, then set the local connector:

```bash
instsci config-cmd --connector-url socks5://127.0.0.1:1080
```

InstSci does not receive your password. When SSO, 2FA, CAPTCHA, or publisher verification appears, complete it manually in the visible CloakBrowser window.

### Common Workflows

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

### MCP Usage

Register the MCP server with an MCP-capable agent:

```bash
claude mcp add instsci -- instsci-mcp
```

Example agent requests:

```text
Find recent papers about perovskite solar cells.
What is the full text of DOI: 10.1038/s41586-020-2649-2?
Download this DOI list through institutional access and summarize failures.
```

### Institutional Access Policy

InstSci uses `auto` identity routing for closed-access PDFs:

- Prefer publisher and federated institutional login when the publisher supports it.
- Use WebVPN only when the configured institution provides a WebVPN gateway and that route works in the active browser context.
- Do not treat exported WebVPN cookies as a full reusable login state.
- Keep the visible CloakBrowser context alive for SSO, CAPTCHA, Cloudflare, WAF checks, and publisher-generated PDF tokens.

In practice, WebVPN is useful but not universal. For many publishers, the most reliable route is one institutional login through the publisher's own OpenAthens/Shibboleth flow, then reuse the persistent browser profile for later downloads.

### Local Data

By default, InstSci stores local runtime state under `~/.instsci`:

- `config.json`: user configuration.
- `chrome-profile/`: persistent CloakBrowser profile.
- `carsi_cookies/`: per-publisher session artifacts.
- `papers/`: default output directory.
- `cache/`: metadata and retrieval cache.

For compatibility with older installs, InstSci can still read legacy `~/.vpnsci` configuration when no new config exists.

### Requirements

- Python 3.10 or newer.
- CloakBrowser, installed through the Python dependency set.
- A legitimate institutional subscription or Open Access source for closed-access content.
- Optional: Docker or a local connector only for institutions that require EasyConnect/aTrust-style access.

### Compliance Notes

InstSci is a research utility for accessing academic resources that you are authorized to use. It is not a VPN service, does not implement network tunneling protocols, and does not bypass publisher or institution verification. Users are responsible for following local law, institutional network policy, and publisher license terms.

### License

[MIT](LICENSE)

[Back to language selector](#instsci)

<a id="chinese"></a>

## 中文

InstSci 是一个面向高校、图书馆和科研机构用户的学术论文获取工具。它优先寻找开放获取版本；如果论文需要订阅权限，则通过你自己已有的机构访问权限，在可见浏览器中完成机构登录后获取全文。

InstSci 原名 `vpnsci`。当前包名和命令名已经改为 `instsci`，旧的 `vpnsci.*` Python 导入路径会继续作为兼容别名保留。

### 它能做什么

InstSci 会按低干扰到高干预的顺序尝试：

1. 先通过 Unpaywall、arXiv、Semantic Scholar 和出版社 DOI 元数据寻找开放获取版本。
2. 如果是闭源论文，再复用你的学校或机构订阅权限，通过可见浏览器访问出版社页面。
3. 将浏览器会话保存在本地，后续批量下载通常不需要反复登录。
4. 如果无法获取全文，返回元数据、诊断信息和下一步建议。

InstSci 同时适合命令行和 AI Agent 工作流。它提供 MCP Server，因此支持 MCP 的 Agent 可以直接用自然语言搜索、下载和诊断论文。

### 核心特性

- 开放获取优先：Unpaywall、arXiv、Semantic Scholar、出版社 DOI 元数据。
- 通过 CloakBrowser 进行浏览器级机构访问。
- 支持 Shibboleth、OpenAthens、CARSI 风格流程和出版社机构选择器。
- 内置 100+ 中国高校和图书馆入口配置，覆盖 WebVPN、EZproxy、EasyConnect、aTrust 等接入方式。
- 出版社专用 PDF 流程，覆盖 ACM、ACS、AIP、AMS、Annual Reviews、Copernicus、Frontiers、IEEE、IOP、MDPI、Oxford Academic、PLOS、PNAS、Royal Society Publishing、RSC、Science、Springer Nature、Wiley、World Scientific 等。
- 长生命周期浏览器会话 broker，适合批量出版社下载。
- `session-doctor` 可检查本地浏览器会话。
- `publisher-doctor` 可做 HTTP 路由预检，但与浏览器验证结论明确分离。

### 安装

```bash
git clone https://github.com/Rimagination/instsci.git
cd instsci
pip install -e .
```

安装后主要命令是：

```bash
instsci --help
instsci-mcp
```

### 首次配置

先配置你的机构：

```bash
instsci setup --school "你的学校或机构"
```

如果出版社登录页显示的机构名和校园访问入口名不一致，可以单独设置：

```bash
instsci setup --school "你的学校或机构" --federated-school "OpenAthens 中显示的机构名"
```

检查当前环境：

```bash
instsci setup --check
```

如果你的学校需要本地校园连接器，请先用学校认可的客户端完成连接，再配置本地代理地址：

```bash
instsci config-cmd --connector-url socks5://127.0.0.1:1080
```

InstSci 不接收你的密码。遇到 SSO、二次验证、CAPTCHA 或出版社验证时，请在打开的 CloakBrowser 可见窗口中手动完成。

### 常用工作流

搜索论文：

```bash
instsci search "perovskite solar cells" --limit 10
```

获取单篇 DOI，优先开放获取，必要时使用已配置的机构访问：

```bash
instsci fetch "10.1038/s41586-020-2649-2"
```

用推荐的浏览器流程批量获取闭源出版社 PDF：

```bash
instsci papers dois.txt --publisher auto --output ./runs/papers
```

指定出版社 profile：

```bash
instsci publisher-batch dois.txt --publisher acs --output ./runs/acs
```

检查可复用的浏览器会话：

```bash
instsci session-doctor
instsci session-doctor --publisher ieee
```

查看机构身份路由策略：

```bash
instsci identity-policy
```

执行 HTTP-only 路由预检：

```bash
instsci publisher-doctor --publisher all
```

`publisher-doctor` 只是预检工具，可以检查 DOI 跳转、PDF 候选路径和潜在阻塞，但不能作为出版社 PDF 能力的最终结论。最终结论必须来自 `instsci papers`、`instsci publisher-batch` 等可见 CloakBrowser 流程。

### MCP 使用

注册 MCP Server：

```bash
claude mcp add instsci -- instsci-mcp
```

示例 Agent 请求：

```text
帮我找几篇钙钛矿太阳能电池的最新论文。
这篇论文的全文是什么？DOI: 10.1038/s41586-020-2649-2
把这个 DOI 列表用机构访问下载成 PDF，并给我失败诊断。
```

### 机构访问策略

InstSci 对闭源 PDF 使用 `auto` 身份路由：

- 出版社支持时，优先走出版社自己的机构登录、Shibboleth 或 OpenAthens。
- 只有在机构提供 WebVPN 且该出版社路径已经能在活动浏览器上下文中验证时，才使用 WebVPN。
- 不把导出的 WebVPN cookie 当作完整可复用登录状态。
- SSO、CAPTCHA、Cloudflare、WAF 检查和出版社动态 PDF token 流程，需要保留可见 CloakBrowser 活动上下文。

实际使用中，WebVPN 有用但不是万能路线。对很多出版社来说，更可靠的是通过出版社自己的 OpenAthens/Shibboleth 流程完成一次机构登录，然后复用持久浏览器 profile。

### 本地数据

默认情况下，InstSci 将运行状态保存在 `~/.instsci`：

- `config.json`：用户配置。
- `chrome-profile/`：持久 CloakBrowser profile。
- `carsi_cookies/`：按出版社保存的会话产物。
- `papers/`：默认输出目录。
- `cache/`：元数据和获取缓存。

为了兼容旧版本，如果新的配置不存在，InstSci 仍会读取旧的 `~/.vpnsci` 配置。

### 环境要求

- Python 3.10 或更新版本。
- CloakBrowser，会随 Python 依赖安装。
- 对闭源内容，需要你自己拥有合法的机构订阅权限或开放获取来源。
- 可选：只有部分需要 EasyConnect/aTrust 风格入口的机构才需要 Docker 或本地连接器。

### 合规说明

InstSci 是用于访问你有权使用的学术资源的研究工具。它不是 VPN 服务，不实现网络隧道协议，也不会绕过出版社或机构验证。使用者需要遵守当地法律、机构网络政策和出版社授权条款。

### 许可证

[MIT](LICENSE)

[返回语言选择](#instsci)
