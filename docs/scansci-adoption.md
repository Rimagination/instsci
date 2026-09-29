# 从 ScanSci PDF 借鉴的改进

对照日期：2026-09-08。ScanSci PDF 远端 `main` 为 `3172af6`，最近功能版本为 `1.16.0`（`0034024`）。InstSci 基线为 `836cd6b`，工作区已有的附件、broker、Elsevier 改动也纳入现状判断。本轮以读取源码为依据；未执行论文下载或发布新版。

## 借鉴顺序

| 优先级 | ScanSci PDF 的实践 | InstSci 现状与适配方式 | 本轮结果 |
| --- | --- | --- | --- |
| P0 | 统一任务输入、保留未解析条目 | 三个批量入口各自按文本行读取；统一为一个解析器，记录原始行、规范 DOI、去重、保留标题 | 已实现 TXT/CSV/TSV |
| P0 | 下载前形成渠道队列 | `papers --publisher auto` 要求单一出版社；复用现有 profile，将混合列表拆为可直接执行的出版社 CSV | 已实现离线 `queue` 命令 |
| P1 | 瞬时失败冷却后低速重试 | 共用失败分类，HTTP 遵守 Retry-After，浏览器冷却后单线程重试；复用 attempt cache 和 `jobs resume` | 第二批已实现 |
| P1 | 来源 × 出版社负缓存 | 对跨论文重复失败设置短期冷却；机构、代理、浏览器会话变化时需失效，避免把临时失败缓存成长期无权限 | 后续建议，需专项会话测试 |
| P1 | OpenAIRE 仓储补充来源 | 接入 Graph API v3，匹配 DOI 与实例级开放访问标记，复用 OA 正文/PDF 验证 | 第二批已实现 |
| P1 | OA 直链的浏览器回退 | 复用 InstSci 所有的可见 CloakBrowser 与 broker，并用已有 PDF 验证判断结果 | 后续建议，需浏览器实测 |
| P2 | 一种用户意图对应一个 MCP 工具 | InstSci 当前只有 8 个工具，暂时没有同等规模的工具膨胀；保持现有兼容性，优先改善返回内容 | 保持现状 |
| P2 | 明确的 Elsevier 自检与定期样本巡检 | 已有全局 key 配置、HTTP 预检查与浏览器证据矩阵；可整理自检输出，增加显式启用的回归样本 | 后续建议 |

来源：[统一管线及队列契约](https://github.com/Rimagination/scansci-pdf/blob/0034024/src/scansci_pdf/pipeline.py)、[冷却重试提交](https://github.com/Rimagination/scansci-pdf/commit/19b32ff)、[来源负缓存提交](https://github.com/Rimagination/scansci-pdf/commit/a0777cf)、[OpenAIRE 实现](https://github.com/Rimagination/scansci-pdf/blob/0034024/src/scansci_pdf/sources/openaire.py)、[MCP 精简提交](https://github.com/Rimagination/scansci-pdf/commit/ce5fef3)、[Elsevier 自检提交](https://github.com/Rimagination/scansci-pdf/commit/9d55311)。

## 适配判断

最值得学习的是将输入整理、路线选择、执行与结果恢复分开。InstSci 已有持久会话、人工登录接续、后台任务和验证矩阵，统一输入可以直接接到这些能力上。第一批改动使用标准库并复用 `PaperRecord` 与 publisher profiles，无新增依赖。

ScanSci 的多来源竞速和四车道调度适用于它的来源集合。InstSci 的机构登录、浏览器所有权、人工验证与最终证据有明确约束；未来引入并发时，应先区分可并行的 OA 元数据查询与需要独占会话的出版社任务。来源范围与身份路线按 InstSci 现有策略维护。

DOI 解析采用保守的后缀保留规则，保留大小写与成对括号，避免通过删掉尾部字母来猜测 DOI。两条大小写不同的相同 DOI 合并时保留首次出现顺序，并只补充首次记录缺失的标题、日期、URL。

相邻 DOI 和带重复 DOI 的 Markdown 链接包装会保留为未识别行，可改成单个 DOI 或 doi.org URL 后继续。带表头表格中的 `#` 开头标题会完整保留。

## 已交付的使用方式

```bash
instsci queue references.csv --output ./runs/reading-queue
instsci papers ./runs/reading-queue/acs.csv --publisher acs --institution "Your Institution"
```

- 输入：UTF-8（含 BOM）TXT/CSV/TSV，支持 DOI、doi.org 链接、含单一 DOI 的引文行。
- 表头：`doi` / `identifier`，`title` / `标题` / `题名`，`published` / `year` / `年份`，`url` / `article_url`。CSV 引号内逗号、换行由标准库解析。
- 无表头 CSV/TSV 取第一列 DOI；其它列不作为访问指令。ScanSci 的 `channel`、`oa_url` 提示不会触发新下载路线。
- 输出：每个已知出版社一个 CSV；未知出版社为 `unknown.csv`；`queue.json` 保存计数、未识别行和各组的 `argv`。
- `argv` 是命令参数数组，调用者需按平台传递参数或按 README 示例运行。生成阶段不执行命令、不加载机构配置。
- 输入计数满足：原始有效行数 = 唯一 DOI 数 + 重复行数 + 未识别行数。空行、注释、表头不计入有效行。
- 出版社键取自现有 profile 注册表，分组只声明 `offline_doi_prefix_prediction`；不声明 OA 状态、权限或 PDF 下载成功。
- 三个下载入口遇到未识别行会报告行号并停止；`queue` 则保留这些行，允许先整理有效部分。
- 输出使用新目录，避免覆盖已有队列或源文件。XLSX、BibTeX、RIS 和无 DOI 条目的在线补全留待后续，当前可先导出 DOI 表格。

同时修正共享出版社推断的前缀边界：`10.10380/...` 不会被误判为 `10.1038/...` 对应的出版社。

## 验证范围

本批为输入、CLI 接入和离线规划改动，风险中等。定向测试覆盖解析边界、全部条目计数、导出后重新读入、重复记录补全、坏行前置拦截、既有 browser broker 提交参数和 CLI 帮助。

- 最终输入模块测试：`python -m unittest tests.test_paper_input -q`，14 项通过。
- 早期接入验证：`python -m unittest tests.test_paper_input tests.test_session_broker tests.test_public_language -q`，当时共 48 项通过。
- 最终交付验证：全套 `python -m unittest discover -s tests -q`，共 274 项、273 项通过、1 项需显式开启的联网测试跳过，耗时 64.561 秒；日志保存在项目 `.tmp/scansci-adoption-tests-final.log`。最终 diff 空白检查通过。
- wheel 构建：`python -m pip wheel . --no-deps --no-build-isolation --disable-pip-version-check --wheel-dir runs/scansci-adoption-build` 通过，新增模块已进入构建包。
- 按项目最终交付规则进行一次独立综合审查，修复两项问题：表格 `#` 标题被误当注释、相邻 DOI 被拼成错误标识。补充定向测试后重新执行全量验证与构建，未开启重复审查。
- 没有增加依赖、单独执行哈希检查或反复重跑未受影响的定向测试；wheel 构建器自带摘要输出不作为额外完整性检查。

浏览器下载、SSO、附件和出版社可用性没有在本轮实测。第二批已根据 Git 历史中的执行层设计与当前代码，补齐 `docs/browser-execution-layer.md`，明确当前执行层约定。

## 第二批：失败恢复与 OpenAIRE

### 调度与恢复

- `failure_category` 区分临时故障、人工待办、访问权限不足、待检查四类。`sso_required` 原本已排除于自动重试；本批进一步移出含挑战含义的 `challenge_or_viewer_timeout`，补充超时、网络和出版社暂时不可用。
- 浏览器只对已知临时故障再试一轮：批次等待 30 秒，单线程，条目间等待 5 秒。broker 分支保持同一 context；独立运行继续使用原有 context 所有权规则。
- `retry_eligible` 记录可重试条目，`retry_attempted` 记录实际执行数，`failure_categories` 描述剩余失败；重试失败时保留最近一次原因，避免用旧网络错误掩盖新登录待办。
- `jobs resume --transient-only` 筛选已知临时故障。普通恢复保留最终 manifest 中尚未出现的记录，恢复时关闭旧的 skip-attempted 设置，保证选中条目能执行。
- HTTP 的 GET/HEAD/OPTIONS 对 408、429、500、502、503、504 与网络连接/超时进行有界重试。支持秒数或 HTTP 日期形式的 `Retry-After`；服务端要求等待超过上限时返回当前错误，不提前重试。Semantic Scholar 也复用此实现。

### OpenAIRE 接入选择

ScanSci 当时的实现使用旧 XML Search API。官方已公布该 API 的停用安排；本批按现行 Graph API v3 接入，未直接移植旧接口。[官方迁移说明](https://openaire.flarum.cloud/d/74-important-update-deprecation-of-xml-based-search-api)、[Graph API v3 文档](https://graph.openaire.eu/docs/apis/graph-api/research-products/)

`fetch`、普通 `batch` 与 MCP 的通用获取流程在 Unpaywall 未取得足够正文时才查询 OpenAIRE。使用 `pid` 按 DOI 查找；返回记录的 DOI 必须匹配，且候选所在 instance 须标注开放访问。忽略项目、参考文献等嵌套位置的其它 URL，也不依据整篇记录的最佳 OA 状态放行未标明开放的实例。[过滤参数](https://graph.openaire.eu/docs/apis/graph-api/research-products/filtering/)、[实例与 PID 模型](https://graph.openaire.eu/docs/data-model/entities/research-product/)

最多尝试五个候选，PDF 链接优先；跳过刚由 Unpaywall 尝试过的相同 URL。复用 OA 下载/HTML 查找路径，按实际 PDF 字节判定。新增候选须匹配首部第一个 DOI，或首部独立行上的完整标题；排除参考文献段落及零散标题词命中。超时、坏 JSON、HTML 冒充 PDF、内容不匹配都保留回退机会。无新增依赖、key 或浏览器运行时。

### 第二批证据与边界

- 重试定向测试：12 项通过。
- OpenAIRE、FetchResult、Elsevier fetch 与 PDF 字节验证定向测试：39 项通过。
- 最终 OA 验证修复后：`tests.test_openaire` 的 10 项测试通过，包含仅参考文献引用目标 DOI、正文只提到目标标题时的拒绝用例。
- 完整构建产物：`runs/scansci-adoption-round2-build/instsci-0.1.1-py3-none-any.whl`；新模块已进入 wheel。
- 全套测试初轮为 295 项（294 项通过、1 项跳过）；修复后最终共 296 项，295 项通过、1 项联网测试按默认设置跳过，耗时 80.843 秒。最终日志：`.tmp/scansci-adoption-round2-tests-final.log`；最终构建与 diff 空白检查通过。
- 独立综合审查发现 OA 复用宽松身份匹配会接收引用目标论文的其它文章；已改为首部身份验证并补充回归。之后只重跑受影响测试，再进行最终全量与构建；未开启复审或独立哈希检查。
- OpenAIRE 元数据请求得到 HTTP 200；保存的真实 v3 响应可提取候选，简要证据在 `.tmp/openaire-preflight.json`。这属于 HTTP preflight，未下载论文或据此判断出版社访问权限。
- 当前仅覆盖单次请求/单批次的冷却；来源 × 出版社负缓存与跨任务冷却仍为后续工作。真实浏览器下载与院校登录仍需独立实测。
- OA 身份检查使用首部 5,000 字符、前 30 行的保守规则；短标题、复杂版式或缺少 DOI 的副本可能无法确认，将继续其它候选或访问路线。
