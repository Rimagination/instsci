# vpnsci

多校 WebVPN 学术论文全文获取工具，支持 100+ 中国高校。提供标准 [MCP (Model Context Protocol)](https://modelcontextprotocol.io/) Server，可与任何支持 MCP 的 AI Agent 集成使用（如 Claude Code、OpenCode、Cursor、Windsurf 等）。

## 工作原理

vpnsci 采用三层策略获取论文全文：

```
Layer 1: Open Access (Unpaywall + arXiv)    ← 免费，无需登录
Layer 2: WebVPN 机构代理                     ← 需要校园网账号登录
Layer 3: 元数据 (Semantic Scholar)           ← 始终可用
```

## 支持的学校

内置 100+ 高校 WebVPN 配置，包括清华、北大、复旦、浙大、上海交大、大连理工、东北大学等。查看完整列表：

```bash
vpnsci schools          # 列出所有学校
vpnsci schools 北京      # 按省份搜索
vpnsci schools 大连      # 按名称搜索
```

## 安装

```bash
git clone <repo-url>
cd vpnsci
pip install -e .
```

## 快速开始

```bash
# 1. 设置学校
vpnsci config-cmd --school 清华大学

# 2. 设置邮箱（Unpaywall OA 检测需要）
vpnsci config-cmd --email your@email.com

# 3. 登录 WebVPN（浏览器会弹出，完成 CAS 认证）
vpnsci login

# 4. 获取论文
vpnsci fetch "10.1038/s41566-024-01234-5"
```

## CLI 用法

### 登录 WebVPN

```bash
vpnsci login              # 首次登录或 session 过期时使用
vpnsci login --force      # 强制重新登录
```

### 获取论文

```bash
# 按 DOI
vpnsci fetch "10.1038/s41566-024-01234-5"

# 按 URL
vpnsci fetch "https://www.nature.com/articles/s41566-024-01234-5"

# 输出 markdown 格式
vpnsci fetch "10.1038/s41566-024-01234-5" --format markdown

# 纯文本（节省 token）
vpnsci fetch "10.1038/s41566-024-01234-5" --text-only
```

### 批量获取

```bash
# 创建一个 DOI 文件（每行一个 DOI）
vpnsci batch dois.txt --format markdown --output ./papers
```

### 搜索论文

```bash
vpnsci search "perovskite solar cells"
vpnsci search "organic photovoltaics" --limit 20 --year 2022-2025
vpnsci search "silver nanowire" --fetch  # 搜索并获取全文
```

### 切换学校

```bash
vpnsci config-cmd --school 大连理工大学
```

## MCP 集成

vpnsci 提供标准 MCP Server（命令：`vpnsci-mcp`），可接入任何支持 MCP 协议的 AI Agent。

### Claude Code

```bash
claude mcp add vpnsci -- vpnsci-mcp
```

### OpenCode / Cursor / Windsurf 等

在对应工具的 MCP 配置文件中添加：

```json
{
  "mcpServers": {
    "vpnsci": {
      "command": "vpnsci-mcp"
    }
  }
}
```

注册后重启 Agent，首次使用时告诉 Agent 你的学校即可自动配置：

> 用户: "帮我搜几篇关于钙钛矿太阳能电池的最新论文"
> Agent: "你还没配置学校，请告诉我你的学校名称"
> 用户: "兰州大学"
> Agent: [自动调用 configure_school 完成配置]
> Agent: "已配置为兰州大学，现在帮你搜索..."

之后正常使用即可，无需再次配置。

## 配置

配置文件位于 `~/.vpnsci/config.json`：

| 字段 | 说明 | 默认值 |
|------|------|--------|
| `school` | 学校名称 | `清华大学` |
| `webvpn_base_url` | WebVPN 地址（自动从学校解析） | `""` |
| `email` | Unpaywall API 邮箱 | `""` |
| `output_dir` | PDF 保存目录 | `~/.vpnsci/papers` |
| `cache_dir` | 缓存目录 | `~/.vpnsci/cache` |

## 项目结构

```
vpnsci/
├── vpnsci/
│   ├── mcp_server.py          # MCP Server
│   ├── cli.py                 # CLI (Typer)
│   ├── fetcher.py             # 核心获取逻辑
│   ├── auth.py                # WebVPN 认证 (Selenium + AES)
│   ├── config.py              # 配置管理
│   ├── schools.py             # 学校数据库
│   ├── models.py              # Paper 数据模型
│   ├── data/webvpn.json       # 100+ 高校 WebVPN 配置
│   ├── sources/
│   │   ├── semantic_scholar.py
│   │   ├── unpaywall.py
│   │   └── arxiv.py
│   └── extractors/
│       ├── html_extractor.py
│       ├── pdf_extractor.py
│       └── publisher_adapters/
├── tests/
├── pyproject.toml
└── README.md
```

## 环境要求

- Python >= 3.10
- Chrome 浏览器（WebVPN CAS 登录需要）

## 致谢

本项目参考了以下开源项目：

- [lcandy2/webvpn-converter](https://github.com/lcandy2/webvpn-converter) — 100+ 高校 WebVPN 配置数据库，本项目的学校数据来源
- [Konano/Tuna-Erha-Bot](https://github.com/Konano/Tuna-Erha-Bot) — 清华 WebVPN URL 加密算法参考
- [eWloYW8/ZJUWebVPN](https://github.com/eWloYW8/ZJUWebVPN) — 浙大 WebVPN 动态密钥方案参考
- [qiyang-ustc/CASPaperTunneling](https://github.com/qiyang-ustc/CASPaperTunneling) — CAS 认证流程参考
- [fermionoid/paper-fetcher](https://github.com/fermionoid/paper-fetcher) — 本项目的前身，论文获取架构参考

## License

[MIT](LICENSE)
