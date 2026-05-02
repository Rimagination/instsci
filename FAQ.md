# FAQ / 常见问题

## 1. 主流期刊都有反爬虫功能，会不会受影响？

基本不会。搜索走的是 Semantic Scholar、Unpaywall、arXiv 的**官方 API**，不是爬网页。获取付费论文走的是学校 WebVPN 代理，从出版商角度看就是一个正常用户在浏览器里打开论文。代码里还做了 2-5 秒随机延迟限速，不会触发风控。

## 2. 支持哪些学校？

内置 100+ 中国高校配置，包括清华、北大、复旦、浙大、上海交大、大连理工、东北大学、吉林大学等。运行 `vpnsci schools` 查看完整列表。大部分学校使用默认加密密钥，少数学校（如大连理工、东北大学）使用自定义密钥，已内置支持。

## 3. 如何切换学校？

```bash
vpnsci config-cmd --school 大连理工大学
```

会自动设置 WebVPN 地址和加密密钥。用 `vpnsci schools` 查看可选学校。

## 4. Elsevier 网站经常提示人机验证，能顺利跳过吗？

Elsevier 的反爬比较严格，通过 WebVPN 获取 Elsevier 论文可能会遇到 403。建议优先尝试 Unpaywall 的 OA 版本（程序会自动尝试）。如果 OA 没有，可以手动在浏览器中通过 WebVPN 下载 PDF。

## 5. WebVPN session 会过期吗？

会过期，通常几小时到一天（取决于学校设置）。过期后需要重新登录：

```bash
vpnsci login --force
```

可以在出门前运行 `vpnsci login` 预先登录保存 cookies。如果 session 过期，Open Access 和 arXiv 的论文仍然可以正常获取。

## 6. 浙大为什么不在列表里？

浙大的 WebVPN 使用动态密钥方案（登录后从 `/user/info` API 获取密钥），与其他学校的静态密钥不同。目前 vpnsci 只支持静态密钥的学校，动态密钥支持计划在未来版本中添加。

## 7. 加密密钥是怎么来的？

大部分学校使用默认密钥 `wrdvpnisthebest!`。部分学校使用自定义密钥，这些数据来自开源项目 `lcandy2/webvpn-converter`。如果你的学校不在列表中但你知道它的密钥，可以手动编辑 `~/.vpnsci/config.json` 设置 `webvpn_base_url`。

## 8. 需要安装什么环境？

- Python >= 3.10
- Chrome 浏览器（WebVPN CAS 登录需要）
- 安装命令：`pip install -e .`
- 注册到 Claude Code：`claude mcp add vpnsci -- vpnsci-mcp`
