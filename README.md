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
vpnsci config-cmd --school 兰州大学

# 2. 设置邮箱（Unpaywall OA 检测需要）
vpnsci config-cmd --email your@email.com

# 3. 登录 WebVPN（浏览器会弹出，完成 CAS 认证）
vpnsci login

# 4. 获取论文
vpnsci fetch "10.1038/s41566-024-01234-5"
```

## VPN 类型说明

不同高校使用不同的 VPN 系统，vpnsci 根据学校类型自动选择获取方式：

| 类型 | 说明 | vpnsci 获取方式 |
|------|------|----------------|
| **WebVPN** | 网页反向代理，URL 改写（大多数高校） | 浏览器 CAS 登录后直接获取 |
| **EasyConnect** | 深信服 SSL VPN 客户端（浙大、南大等） | 需要 SOCKS5 代理 |
| **aTrust** | 深信服零信任 VPN（海大、哈工深等） | 需要 SOCKS5 代理 |

### WebVPN 学校（直接使用）

大部分学校属于 WebVPN 类型，直接使用即可：

```bash
vpnsci config-cmd --school 清华大学
vpnsci login    # 浏览器弹出，完成 CAS 认证
vpnsci fetch "10.1038/xxx"
```

### EasyConnect / aTrust 学校（需要代理）

这类学校需要先建立 VPN 隧道，再通过 SOCKS5 代理获取论文。

**方案一：docker-easyconnect（推荐，兼容性最好）**

```bash
# 1. 启动 Docker 容器
docker run --rm -d --name easyconnect --privileged \
  -p 127.0.0.1:1080:1080 -p 127.0.0.1:8888:8888 \
  -e EC_VER=7.6.3 -e VPN_ADDR=vpn.ouc.edu.cn \
  hagb/docker-easyconnect

# 2. 浏览器打开 http://127.0.0.1:8888 完成登录

# 3. 配置 vpnsci 使用代理
vpnsci config-cmd --proxy-url socks5://127.0.0.1:1080

# 4. 直接获取论文（无需再次登录）
vpnsci fetch "10.1038/xxx"
```

> 将 `vpn.ouc.edu.cn` 替换为你学校的 VPN 地址。

**方案二：zju-connect（轻量，仅限部分学校）**

[zju-connect](https://github.com/mythologyli/zju-connect) 是 Go 语言实现的 EasyConnect 替代客户端，无需 Docker。但仅兼容部分学校的 EasyConnect 服务器（已知兼容：浙江大学）。

```bash
# 1. 下载并运行 zju-connect
./zju-connect -server vpn.zju.edu.cn -username 学号 -password 密码 -disable-zju-config

# 2. 配置 vpnsci
vpnsci config-cmd --proxy-url socks5://127.0.0.1:1080
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
| `school` | 学校名称 | `""`（首次使用需配置） |
| `webvpn_base_url` | WebVPN/VPN 地址（自动从学校解析） | `""` |
| `email` | Unpaywall API 邮箱 | `""` |
| `proxy_url` | SOCKS5 代理地址（EasyConnect/aTrust 学校使用） | `""` |
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

## 免责声明

- 本项目是一个**学术论文获取工具**，仅用于帮助高校师生合法访问其所在机构已订阅的学术资源。
- 本项目**不包含**任何深信服（Sangfor）的代码、二进制文件或专有协议实现。
- 本项目**不提供**VPN 连接功能。对于需要 VPN 的学校，用户需自行配置合法的 VPN 客户端（如学校官方提供的 EasyConnect 客户端）。
- 本项目通过标准 HTTP 协议访问公开或机构授权的学术资源，与浏览器手动访问无本质区别。
- 使用者应遵守所在学校的网络使用规范和相关法律法规。本项目不对滥用行为负责。

## License

[MIT](LICENSE)
