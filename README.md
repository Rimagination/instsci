# vpnsci

学术论文全文获取工具，支持 100+ 中国高校。通过 MCP 协议接入 AI Agent，用自然语言搜索和获取论文。

**获取原理**：
1. 先找免费来源（Unpaywall、arXiv）
2. 找不到则通过学校代理访问机构订阅资源（支持 WebVPN、EZproxy、EasyConnect、aTrust 四种方式）
3. 都不行则返回元数据（标题、摘要、引用数）

## 使用方式

### 通过 AI Agent（推荐）

vpnsci 通过 [MCP](https://modelcontextprotocol.io/) 协议接入 AI Agent，直接用自然语言交互即可。

**安装**：

对你的 AI Agent 说：

> 帮我安装这个 MCP 包：https://github.com/Rimagination/vpnsci

**使用示例**：

> 帮我搜几篇钙钛矿太阳能电池的最新论文

> 这篇论文的全文是什么？DOI: 10.1038/s41566-024-01234-5

> 帮我找 2023 年以后关于有机光伏的高引论文，下载 PDF

首次使用时 Agent 会询问你的学校，告诉它即可自动配置。

### 通过命令行

```bash
# 安装
pip install git+https://github.com/Rimagination/vpnsci

# 配置学校
vpnsci config-cmd --school 你的学校

# 搜索论文
vpnsci search "perovskite solar cells"

# 获取论文全文
vpnsci fetch "10.1038/s41566-024-01234-5"

# 批量获取
vpnsci batch dois.txt --output ./papers

# 查看支持的学校
vpnsci schools
```

## 支持的学校

内置 100+ 高校配置，包括清华、北大、复旦、浙大、上海交大等。大部分学校可直接使用。

少数学校需要额外配置 VPN 代理，首次使用时 Agent 会自动提示。支持 WebVPN、EZproxy、EasyConnect、aTrust 四种接入方式。

## 环境要求

- Python >= 3.10
- Chrome 浏览器（首次校园网登录需要）
- Docker（仅部分学校需要，Agent 会自动提示）

## 免责声明

本项目是学术论文获取工具，帮助高校师生合法访问机构已订阅的学术资源。不包含任何 VPN 协议实现，不提供 VPN 连接功能。使用者应遵守相关法律法规和学校网络使用规范。

## 致谢

- [webvpn-converter](https://github.com/lcandy2/webvpn-converter) — 学校配置数据
- [Tuna-Erha-Bot](https://github.com/Konano/Tuna-Erha-Bot) — WebVPN 加密算法
- [ZJUWebVPN](https://github.com/eWloYW8/ZJUWebVPN) — 动态密钥方案
- [CASPaperTunneling](https://github.com/qiyang-ustc/CASPaperTunneling) — CAS 认证流程

## License

[MIT](LICENSE)
