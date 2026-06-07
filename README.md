# InstSci

语言 / Language: [中文](#中文) | [English](#english)

## 中文

InstSci（原 `vpnsci`）是一个面向科研用户和 AI Agent 的论文获取工具。它优先查找开放获取全文；遇到需要订阅权限的论文时，会通过可见浏览器复用你自己的学校、图书馆或机构访问权限。

### 主要能力

- 开放获取优先：Unpaywall、arXiv、Semantic Scholar、出版社元数据。
- 机构访问辅助：支持 Shibboleth、OpenAthens、CARSI、WebVPN、EZproxy 等常见路径。
- 出版社工作流：覆盖 ACM、ACS、AIP、IEEE、IOP、Nature、Oxford、RSC、ScienceDirect、Springer、Wiley 等。
- 批量下载：可保持可见 CloakBrowser 会话，减少重复登录。
- Agent 友好：提供 `instsci-mcp`，可接入支持 MCP 的 AI 工具。

### 安装

```bash
git clone https://github.com/Rimagination/instsci.git
cd instsci
pip install -e .
```

当前命令和包名是 `instsci`；旧的 `vpnsci.*` Python import 仍保留兼容。

CloakBrowser 浏览器由 InstSci 缓存在项目内的 `instsci/_browsers/cloakbrowser`，该目录不会提交到 Git。

### 快速开始

```bash
instsci setup --school "你的学校或机构"
instsci search "perovskite solar cells" --limit 10
instsci fetch "10.1038/s41586-020-2649-2"
instsci papers dois.txt --publisher auto --output ./runs/papers
```

MCP：

```bash
instsci-mcp
```

### 访问与合规

InstSci 不接收你的密码。需要 SSO、2FA、验证码或出版社验证时，请在弹出的 CloakBrowser 中手动完成。HTTP 诊断工具只能作为预检查；最终能否拿到出版社 PDF，应以可见浏览器工作流的结果为准。

### 许可证

[MIT](LICENSE)

<a id="english"></a>

## English

InstSci, formerly `vpnsci`, is a paper retrieval toolkit for researchers and AI agents. It looks for Open Access full text first; when a paper requires subscription access, it helps reuse your own university, library, or institutional entitlement through a visible browser session.

### Highlights

- Open Access first: Unpaywall, arXiv, Semantic Scholar, and publisher metadata.
- Institutional access support: Shibboleth, OpenAthens, CARSI, WebVPN, EZproxy, and similar routes.
- Publisher workflows: ACM, ACS, AIP, IEEE, IOP, Nature, Oxford, RSC, ScienceDirect, Springer, Wiley, and more.
- Batch downloads with a visible CloakBrowser session to reduce repeated sign-ins.
- Agent-ready MCP server via `instsci-mcp`.

### Install

```bash
git clone https://github.com/Rimagination/instsci.git
cd instsci
pip install -e .
```

The current command and package name is `instsci`; legacy `vpnsci.*` Python imports remain compatible.

InstSci caches the CloakBrowser binary inside `instsci/_browsers/cloakbrowser`; that directory is not committed to Git.

### Quick Start

```bash
instsci setup --school "Your Institution"
instsci search "perovskite solar cells" --limit 10
instsci fetch "10.1038/s41586-020-2649-2"
instsci papers dois.txt --publisher auto --output ./runs/papers
```

MCP:

```bash
instsci-mcp
```

### Access And Compliance

InstSci never asks for your password. When SSO, 2FA, CAPTCHA, or publisher verification appears, complete it manually in the visible CloakBrowser window. HTTP diagnostics are preflight checks only; final publisher PDF availability should come from a visible browser-backed workflow.

### License

[MIT](LICENSE)
