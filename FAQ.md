# FAQ

## 1. 使用 InstSci 会不会触发出版社风控？

搜索阶段走 Semantic Scholar、Unpaywall、arXiv 等官方 API，不爬搜索网页。获取订阅论文时，InstSci 会通过你的学校/机构访问入口打开论文页面，并带有 2-5 秒随机延迟，尽量模拟正常人工访问。

## 2. 支持哪些学校？

内置 100+ 中国高校和图书馆入口配置，包括清华、北大、复旦、浙大、上海交大、大连理工、东北大学、吉林大学等。运行：

```bash
instsci schools
```

即可查看完整列表。

## 3. 怎么配置学校？

```bash
instsci config-cmd --school 大连理工大学
```

程序会自动设置学校入口和所需参数。也可以手动指定入口地址：

```bash
instsci config-cmd --access-url https://your-school-access.example.edu.cn
```

## 4. Elsevier / ScienceDirect 获取失败怎么办？

Elsevier 对自动化访问比较严格。InstSci 会优先尝试开放版本和 Elsevier API；如果仍无法获取，可以在浏览器中完成机构登录后重试。ScienceDirect 建议先配置并验证 Elsevier API，因为稳定路线是 `view=FULL` XML -> MAIN PDF `attachment-eid/object-eid` -> Content Object API，不是直接请求网页 PDF。

先配置项目级全局 Elsevier API Key，并让 InstSci 做一次真实下载验证。这个 key 会保存到本机 InstSci 配置中，后续所有 Elsevier / ScienceDirect DOI 都会复用；验证 DOI 只是 smoke test，不会把配置绑定到某一篇文章。

```bash
instsci elsevier-setup --api-key YOUR_KEY --validate
```

如果你的机构提供 Elsevier institutional token，也可以一并配置：

```bash
instsci elsevier-setup --api-key YOUR_KEY --inst-token YOUR_TOKEN
```

闭源全文授权和请求 IP 强相关。验证时 InstSci 会优先走 direct route，让 `api.elsevier.com` 使用校园网、学校 VPN、规则 VPN 或图书馆出口；只有 direct 失败或无授权时，才会尝试已配置的 connector/proxy。

## 5. 机构访问 session 会过期吗？

会。学校/机构入口通常会在数小时到数天后过期。过期后重新登录即可：

```bash
instsci login --force
```

开放获取论文不依赖机构 session，仍然可以正常获取。

## 6. 少数学校为什么需要本地连接器？

部分学校要求先通过学校客户端建立本地访问环境。InstSci 不负责登录凭据，也不保存账号密码；你只需要在学校要求的客户端或兼容容器中完成登录，然后告诉 InstSci 本地 SOCKS5 地址：

```bash
instsci config-cmd --connector-url socks5://127.0.0.1:1080
```

## 7. 如果我的学校不在列表里？

可以先用学校提供的图书馆或机构入口地址手动配置：

```bash
instsci config-cmd --access-url https://your-school-access.example.edu.cn
```

如果你愿意补充学校入口配置，可以在项目里添加一条数据并提交 PR。

## 8. MCP 怎么注册？

```bash
claude mcp add instsci -- instsci-mcp
```
