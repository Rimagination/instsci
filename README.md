<p align="center">
  <img src="assets/brand/instsci-logo-ai.png" alt="InstSci" width="720">
</p>

<p align="center">
  <a href="pyproject.toml"><img src="https://img.shields.io/badge/version-0.1.0-blue.svg" alt="Version 0.1.0"></a>
  <a href="pyproject.toml"><img src="https://img.shields.io/badge/python-%3E%3D3.10-blue.svg" alt="Python >=3.10"></a>
  <a href="LICENSE"><img src="https://img.shields.io/badge/license-MIT-green.svg" alt="License: MIT"></a>
  <img src="https://img.shields.io/badge/MCP-compatible-blue.svg" alt="MCP compatible">
</p>

[中文](#中文) | [English](#english)

## 中文

InstSci 是一个面向科研用户和 AI Agent 的论文获取工具。它优先查找开放获取全文；遇到需要订阅权限的论文时，会通过可见浏览器复用你自己的学校、图书馆或机构访问权限。

### 主要能力

- 开放获取优先：Unpaywall、arXiv、OpenAIRE 候选来源；支持 Semantic Scholar 检索与出版社元数据。
- 机构访问辅助：支持 Shibboleth、OpenAthens、CARSI、WebVPN、EZproxy 等常见路径。
- 出版社工作流：覆盖 ACM、ACS、AIP、IEEE、IOP、Nature、Oxford、RSC、ScienceDirect、Springer、Wiley 等。
- 批量下载：可保持可见 CloakBrowser 会话，减少重复登录。
- Agent 友好：提供 `instsci-mcp`，可接入支持 MCP 的 AI 工具。

### 安装

推荐用户安装（发布到 PyPI 前请从 GitHub 安装）：

```bash
pipx install git+https://github.com/Rimagination/instsci.git
# or
uv tool install git+https://github.com/Rimagination/instsci.git
```

开发者安装：

```bash
git clone https://github.com/Rimagination/instsci.git
cd instsci
pip install -e .
```

当前命令和包名都是 `instsci`。

一句话安装 MCP 和 skill：

```text
帮我安装这个 mcp 和 skill：https://github.com/Rimagination/instsci
```

CloakBrowser 浏览器由 InstSci 缓存在项目内的 `instsci/_browsers/cloakbrowser`，该目录不会提交到 Git。

### 快速开始

Elsevier API key 是项目级全局配置，配置一次后会用于后续所有 ScienceDirect DOI；`--validate` 只用样例 DOI 做下载验证。Inst Token 不是必需的，只有图书馆明确提供 Elsevier institutional token 时才需要配置。Elsevier 下载优先走 `view=FULL XML -> object/eid -> PDF`，并先用 direct route 让 `api.elsevier.com` 走校园网、学校 VPN、规则 VPN 或图书馆出口。

```bash
instsci setup --school "你的学校或机构"
instsci elsevier-setup --api-key YOUR_ELSEVIER_KEY --validate
instsci search "perovskite solar cells" --limit 10
instsci fetch "10.1038/s41586-020-2649-2"
instsci papers dois.txt --publisher auto --output ./runs/papers
instsci papers dois.txt --publisher auto --output ./runs/papers --detach
instsci jobs status
instsci jobs tail <job-id>
instsci jobs resume <job-id>
```

MCP：

```bash
instsci-mcp
```

### 文献清单与离线队列

`batch`、`papers` 和 `publisher-batch` 共用 TXT/CSV/TSV 输入解析：支持 DOI、doi.org 链接和含一个 DOI 的引文行，按 DOI 去重。CSV/TSV 可用 `doi,title,published,url` 表头；`identifier`、`year`、`标题`、`题名`、`年份` 也可识别。表格中的标题会传递给浏览器下载流程。

混合出版社清单可先离线拆分，全程无需登录或发起下载：

```bash
instsci queue references.csv --output ./runs/reading-queue
instsci papers ./runs/reading-queue/acs.csv --publisher acs --institution "你的学校或机构"
```

输出目录须为新目录。`queue.json` 记录输入数、重复数、未识别行及各组命令参数 `argv`，每组 CSV 可直接交给 `papers`。未识别行保留原字段和行号；未知出版社 DOI 存入 `unknown.csv`，需补充选择出版社。直接下载遇到未识别行会先停止，便于纠正输入。

出版社分组来自 DOI 前缀预测，权限与 PDF 获取结果仍由浏览器流程确认。当前支持 UTF-8 TXT/CSV/TSV；XLSX、BibTeX、RIS 请先导出 DOI 表格。ScanSci 队列中的 `channel`、`oa_url` 列不会改变 InstSci 的访问路线。

与 ScanSci PDF 的对照及后续优先级见 [借鉴报告](docs/scansci-adoption.md)。

### 失败恢复与 OA 回退

已知临时失败会等待 30 秒后以单线程重试一轮，重试条目之间间隔 5 秒；`--no-retry` 可关闭。登录、验证码、权限和未知失败需按报告处理。broker 重试复用同一个可见浏览器上下文。

```bash
instsci jobs resume JOB_ID --transient-only
```

普通 `jobs resume` 保留所有未完成记录；`--transient-only` 只选择已识别的临时失败。HTTP 元数据请求会遵守 `Retry-After`；等待超出上限时返回错误供上层处理。

`fetch`、`batch` 和 MCP `fetch_paper` 在 Unpaywall 未取得足够正文后查询 OpenAIRE Graph API v3。候选必须匹配目标 DOI，并在实例级别标注开放访问；最多尝试 5 个，复用 OA 下载和字节验证，检查新增候选的 DOI/标题匹配。无需新增 key 或依赖。出版社专用 `papers` 命令继续走现有浏览器流程。

### 访问与合规

InstSci 不接收你的密码。需要 SSO、2FA、验证码或出版社验证时，请在弹出的 CloakBrowser 中手动完成。HTTP 诊断工具只能作为预检查；最终能否拿到出版社 PDF，应以可见浏览器工作流的结果为准。

### 许可证

[MIT](LICENSE)

<a id="english"></a>

## English

InstSci is a paper retrieval toolkit for researchers and AI agents. It looks for Open Access full text first; when a paper requires subscription access, it helps reuse your own university, library, or institutional entitlement through a visible browser session.

### Highlights

- Open Access first: Unpaywall, arXiv, and OpenAIRE candidates; Semantic Scholar search and publisher metadata.
- Institutional access support: Shibboleth, OpenAthens, CARSI, WebVPN, EZproxy, and similar routes.
- Publisher workflows: ACM, ACS, AIP, IEEE, IOP, Nature, Oxford, RSC, ScienceDirect, Springer, Wiley, and more.
- Batch downloads with a visible CloakBrowser session to reduce repeated sign-ins.
- Agent-ready MCP server via `instsci-mcp`.

### Install

Recommended user install (until the PyPI package is published):

```bash
pipx install git+https://github.com/Rimagination/instsci.git
# or
uv tool install git+https://github.com/Rimagination/instsci.git
```

Development install:

```bash
git clone https://github.com/Rimagination/instsci.git
cd instsci
pip install -e .
```

The command and package name are both `instsci`.

One-line MCP and skill install:

```text
Install this MCP and skill: https://github.com/Rimagination/instsci
```

InstSci caches the CloakBrowser binary inside `instsci/_browsers/cloakbrowser`; that directory is not committed to Git.

### Quick Start

The Elsevier API key is a project-wide global setting. Configure it once for later ScienceDirect DOI retrieval; `--validate` only uses a sample DOI as a download smoke test. Inst Token is optional and should be set only when your library explicitly provides an Elsevier institutional token. Elsevier downloads prefer `view=FULL XML -> object/eid -> PDF`, using the direct route first so `api.elsevier.com` can use your campus, school VPN, rule VPN, or library exit.

```bash
instsci setup --school "Your Institution"
instsci elsevier-setup --api-key YOUR_ELSEVIER_KEY --validate
instsci search "perovskite solar cells" --limit 10
instsci fetch "10.1038/s41586-020-2649-2"
instsci papers dois.txt --publisher auto --output ./runs/papers
instsci papers dois.txt --publisher auto --output ./runs/papers --detach
instsci jobs status
instsci jobs tail <job-id>
instsci jobs resume <job-id>
```

MCP:

```bash
instsci-mcp
```

### Lists And Offline Queues

`batch`, `papers`, and `publisher-batch` accept UTF-8 TXT/CSV/TSV inputs, normalize DOI links, and deduplicate DOIs. Tables support `doi,title,published,url` columns, with `identifier` and `year` aliases. Titles are passed to the browser workflow.

```bash
instsci queue references.csv --output ./runs/reading-queue
instsci papers ./runs/reading-queue/acs.csv --publisher acs --institution "Your Institution"
```

Use a new output directory. The offline planner writes publisher CSVs and `queue.json` with counts, unresolved rows, and command argument arrays. Unknown publisher DOIs go to `unknown.csv` for explicit publisher selection. Prefix predictions are routing hints; access evidence comes from the visible browser workflow. Export XLSX/BibTeX/RIS to a DOI table first. ScanSci `channel` and `oa_url` columns do not change InstSci access routes. Download commands stop on unresolved rows so they can be corrected before a run.

### Access And Compliance

Known transient publisher failures receive one sequential retry pass after a 30-second cooldown, with 5 seconds between records. `--no-retry` disables it. Broker retries retain the same visible browser context. Use `instsci jobs resume JOB_ID --transient-only` to select temporary failures; ordinary resume keeps all unfinished records. Login, CAPTCHA, entitlement and unknown failures require attention.

Generic fetching falls back to OpenAIRE Graph API v3 when Unpaywall does not yield sufficient text. Candidates must match the DOI and carry instance-level Open Access rights. Up to five candidates pass through the existing OA pipeline, byte validation and DOI/title matching. HTTP metadata requests respect bounded `Retry-After` waits. Publisher-specific `papers` runs keep their browser route.

InstSci never asks for your password. When SSO, 2FA, CAPTCHA, or publisher verification appears, complete it manually in the visible CloakBrowser window. HTTP diagnostics are preflight checks only; final publisher PDF availability should come from a visible browser-backed workflow.

Login persistence is local. InstSci reuses the persistent CloakBrowser profile directory and the long-lived publisher broker so cookies, localStorage, IndexedDB, cache, and live browser state can survive normal batch work. It also remembers the latest configured subscription institution name and optional Chinese/English aliases in local config so later closed-access runs do not ask again.

InstSci does not store your institution password, and exported cookie files are not treated as a complete login state. Runtime state such as `config.json`, `cookies.json`, `chrome-profile/`, `carsi_cookies/`, broker queues, and `runs/` output should remain local and is ignored by Git.

### License

[MIT](LICENSE)
