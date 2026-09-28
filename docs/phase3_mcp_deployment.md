# CrossLLM Phase 3 MCP 批量部署记录

本文件记录 CrossLLM Phase 3 接入的所有 MCP Server 的部署细节、验证结果、已知问题与维护说明。

部署与验收依据：`docs/mcp中期部署.md`。

---

## 0. 本次部署概览

- **部署时间**：2026-09-01
- **全量健康检查脚本**：`scripts/manual/run_mcp_health.py`
- **配置中心**：`config/mcp.yaml`
- **核心 Client**：`core/mcp_registry.py`
- **当前状态**：16 个 MCP Server 已配置，全部 READY（含 8 个第一版 + 8 个本次新增）
- **全量测试**：88/88 通过（`pytest -q --tb=short`，耗时约 8s）
- **批次**：第一批（08-30）openwebsearch/github/fetch/filesystem/context7/playwright/stackoverflow/zhihu；第二批（09-01）trendshub/openalex/everything/pdf2md/excel/yt_transcript/huggingface/reddit。

### 0.1 验收结论速览

```text
MCP 总数：16

READY（16）：
- openwebsearch    fetch           filesystem       github
- playwright       stackoverflow   context7         zhihu
- trendshub        openalex        everything       pdf2md
- excel            yt_transcript   huggingface      reddit

AUTH_REQUIRED：0
DEGRADED：0
FAILED：0
```

注意：huggingface 与 reddit 需要在 `.env` 中配置 `HF_TOKEN` / `REDDIT_CLIENT_ID`+`REDDIT_CLIENT_SECRET` 才能让 smoke test 返回实质数据；未配置时仍可连接、列出工具，但实际调用会被服务端拒绝。

### 0.2 设计原则

- **不重新设计 MCP Registry**：继续使用 `core/mcp_registry.py` 的最小 Client。
- **单实例共享**：每个 MCP Server 在 CrossLLM 生命周期内只启动一次，所有模型共享。
- **Tool 白名单**：每个 Server 通过 `tool_name` 只暴露一个只读工具，不开放写入/删除/创建能力。
- **密钥不落地**：所有 API Key / Token 从 `.env` 读取，通过环境变量注入子进程，不写入 `mcp.yaml`、`models.yaml`、日志或 Prompt。
- **健康检查独立**：`scripts/manual/run_mcp_health.py` 不依赖真实 LLM，只做 `connect → tools/list → smoke test → disconnect`。

---

## 1. MCP 部署详情

### 1.1 Open-WebSearch

#### 官方/仓库

- **MCP Server**：Open-WebSearch
- **NPM 包**：`open-websearch`
- **GitHub**：Aas-ee/open-websearch
- **腾讯 MCP 广场**：https://cloud.tencent.com/developer/mcp/server/11739
- **许可证**：Apache-2.0
- **维护状态**：活跃（社区维护）

#### 用途

多引擎 Web Search + 公开网页/站点内容获取。

#### Transport

`stdio`（CrossLLM 本地直连，不启动 HTTP server）。

#### 安装

已全局通过 npm 安装，使用绝对 npx 路径启动：


#### API Key

无需 API Key。

#### Windows 注意事项

- 默认 `MODE=both` 会同时启动 HTTP server 占用 3000 端口；与 GitHub MCP 等多 Server 并行时会导致 `EADDRINUSE`。
- CrossLLM 配置强制 `MODE=stdio`，关闭 HTTP server。

#### MCP Tools

实际暴露 6 个工具（health check 实测）：

```text
search
fetchLinuxDoArticle
fetchCsdnArticle
fetchGithubReadme
fetchWebContent
...（其余被 tool_name 过滤）
```

#### CrossLLM 配置

```yaml
openwebsearch:
  enabled: true
  transport: stdio
  command: ""
  args: []
  env:
    MODE: stdio
    DEFAULT_SEARCH_ENGINE: bing
  tool_name: search
```

#### Smoke Test

搜索 `faster-whisper VAD`，返回约 1168 chars。

#### 当前状态

READY。

#### 已知问题

- `MODE=both` 时会占用 3000 端口，已用 `MODE=stdio` 规避。

---

### 1.2 GitHub

#### 官方/仓库

- **MCP Server**：GitHub MCP
- **NPM 包**：`@modelcontextprotocol/server-github`
- **官方仓库**：https://github.com/github/github-mcp-server
- **当前使用包**：社区版 `@modelcontextprotocol/server-github`（npm 提示 deprecated 但功能正常）
- **许可证**：MIT（社区版）
- **维护状态**：社区版 deprecated；官方版需 Docker 或 Go 编译，Windows 无预构建二进制

#### 用途

仓库、README、代码搜索、Issue、PR、文件读取。

#### Transport

`stdio`。

#### 安装

```text
npx -y @modelcontextprotocol/server-github
```

#### API Key

- `.env` 中的 `GITHUB_PERSONAL_ACCESS_TOKEN`。
- 只读公共仓库需 `public_repo` scope。

#### Windows 注意事项

- 社区版 `GITHUB_READ_ONLY=1` 实测无效，26 个工具仍全暴露（含 12 个写入类）。
- 通过 `tool_name: search_issues` 白名单强制只暴露只读搜索工具。

#### MCP Tools

实际暴露 26 个工具，CrossLLM 只暴露：

```text
search_issues
```

#### CrossLLM 配置

```yaml
github:
  enabled: true
  transport: stdio
  command: ""
  args: ["-y", "@modelcontextprotocol/server-github"]
  env:
    GITHUB_READ_ONLY: "1"  # 语义声明，实际只读由 tool_name 保证
  tool_name: search_issues
```

#### Smoke Test

搜索 `is:issue repo:SYSTRAN/faster-whisper VAD`，返回约 138743 chars。

#### 当前状态

READY。

#### 已知问题

- 社区版 npm 包已 deprecated；长期建议迁移官方 `github-mcp-server`（需 Docker 或 Go）。
- `search_issues` 查询必须包含 `is:issue` 或 `is:pull-request`，否则返回 422。

---

### 1.3 Fetch

#### 官方/仓库

- **MCP Server**：mcp-fetch
- **NPM 包**：`@mokei/mcp-fetch`
- **许可证**：MIT（社区实现）
- **维护状态**：活跃
- **说明**：官方 `@modelcontextprotocol/server-fetch` 不存在，`mcp-server-fetch` 为占位；`@mokei/mcp-fetch` 是实测可用的只读 Fetch MCP。

#### 用途

已知 URL → HTML → Markdown 转换。与 Open-WebSearch 互补：

```text
Open-WebSearch = 找
Fetch = 读
```

#### Transport

`stdio`。

#### 安装

```text
npx -y @mokei/mcp-fetch
```

#### API Key

无需。

#### Windows 注意事项

无特殊注意事项。

#### MCP Tools

实际暴露 1 个工具：

```text
get_markdown
```

参数示例：

```json
{
  "url": "https://example.com",
  "max_length": 5000,
  "start_index": 0,
  "raw": false
}
```

#### CrossLLM 配置

```yaml
fetch:
  enabled: true
  transport: stdio
  command: ""
  args: ["-y", "@mokei/mcp-fetch"]
  env: {}
  tool_name: get_markdown
```

#### Smoke Test

抓取 `https://example.com`，返回 18 chars。

#### 当前状态

READY。

#### 已知问题

- 该 Server 可访问任意公网 URL；第一版不限制 URL，后续如需可加入白名单。

---

### 1.4 Filesystem

#### 官方/仓库

- **MCP Server**：Filesystem
- **NPM 包**：`@modelcontextprotocol/server-filesystem`
- **官方仓库**：https://github.com/modelcontextprotocol/servers/tree/main/src/filesystem
- **许可证**：MIT
- **维护状态**：官方维护

#### 用途

本地文件只读访问（read / list / search / metadata）。

#### Transport

`stdio`。

#### 安装

```text
npx -y @modelcontextprotocol/server-filesystem
```

#### API Key

无需。

#### Windows 注意事项

- 命令行第一个参数为允许访问的目录白名单。
- 该 Server 暴露 14 个工具，含 `write_file`、`delete_file` 等写入类。
- 通过 `tool_name: read_file` 二次过滤，只向 LLM 暴露读取能力。

#### MCP Tools

实际暴露 14 个工具，CrossLLM 只暴露：

```text
read_file
```

#### CrossLLM 配置

```yaml
filesystem:
  enabled: true
  transport: stdio
  command: ""
  args:
    - "-y"
    - "@modelcontextprotocol/server-filesystem"
    - ""
  env: {}
  tool_name: read_file
```

#### Smoke Test

读取 ``，返回 20681 chars。

#### 当前状态

READY。

#### 已知问题

- Server 本身暴露写入/删除工具；`tool_name` 白名单是第一道防线，但仍需确保 `args` 中目录白名单范围合理。

---

### 1.5 Context7

#### 官方/仓库

- **MCP Server**：Context7
- **NPM 包**：`@upstash/context7-mcp`
- **官方端点**：https://mcp.context7.com/mcp
- **许可证**：MIT
- **维护状态**：官方维护

#### 用途

直接获取指定开源库的当前文档、特定版本文档、代码示例，解决 LLM 使用旧版或虚构 API 的问题。

#### Transport

`stdio`。

#### 安装

```text
npx -y @upstash/context7-mcp
```

#### API Key

- `.env` 中的 `CONTEXT7_API_KEY`（可选）。
- 无 Key 时可用但 rate limit 更严格。

#### Windows 注意事项

无特殊注意事项。

#### MCP Tools

实际暴露 2 个工具：

```text
resolve-library-id
query-docs
```

CrossLLM 只暴露：

```text
query-docs
```

#### CrossLLM 配置

```yaml
context7:
  enabled: true
  transport: stdio
  command: ""
  args: ["-y", "@upstash/context7-mcp"]
  env:
    CONTEXT7_API_KEY: "${CONTEXT7_API_KEY}"
  tool_name: query-docs
```

#### Smoke Test

查询 `faster-whisper` 的 `VAD` 文档，返回 124 chars。

#### 当前状态

READY（当前未配置 API Key，使用免费额度）。

#### 已知问题

- 无 API Key 时可能遇到 rate limit；生产/高频使用建议配置 Key。

---

### 1.6 Playwright

#### 官方/仓库

- **MCP Server**：Playwright MCP
- **NPM 包**：`@playwright/mcp`
- **官方仓库**：https://github.com/microsoft/playwright-mcp
- **许可证**：Apache-2.0
- **维护状态**：Microsoft 官方维护

#### 用途

真实浏览器渲染动态网页，用于 Open-WebSearch / Fetch 无法直接获取的 JS 渲染内容。

#### Transport

`stdio`。

#### 安装

```text
npx -y @playwright/mcp
```

首次运行会自动安装 Chromium。

#### API Key

无需。

#### Windows 注意事项

无特殊注意事项。

#### MCP Tools

实际暴露 24 个工具，CrossLLM 只暴露：

```text
browser_snapshot
```

（health check 实测：使用该工具可成功获取页面快照）

#### CrossLLM 配置

```yaml
playwright:
  enabled: true
  transport: stdio
  command: ""
  args: ["-y", "@playwright/mcp"]
  env: {}
  tool_name: browser_snapshot
```

#### Smoke Test

导航到 `https://example.com` 并快照，返回 197 chars。

#### 当前状态

READY。

#### 已知问题

- 浏览器首次启动可能需要下载 Chromium；已自动完成。
- 该 Server 含 `browser_evaluate` 等可执行 JS 的工具；当前通过 `tool_name` 白名单限制为只读快照。

---

### 1.7 Stack Overflow

#### 官方/仓库

- **MCP Server**：Stack Overflow MCP
- **NPM 包**：`@luqezman/stackoverflow-mcp`
- **替代已不可用包**：`@gscalzo/stackoverflow-mcp`（npm 404）
- **许可证**：未明确，社区实现
- **维护状态**：活跃（FastMCP 4.0.0）

#### 用途

编程问答搜索，特别适合 Python / PyTorch / CUDA / faster-whisper 等实际报错。

#### Transport

`stdio`。

#### 安装

```text
npx -y @luqezman/stackoverflow-mcp
```

#### API Key

- `.env` 中的 `STACKOVERFLOW_API_KEY`（可选）。
- 无 Key 时可用但 rate limit 更严格。

#### Windows 注意事项

- 该 Server 内部有一个 `run_in_thread` 的 async Future 异常，但不影响 stdio MCP 协议调用。
- 工具名不是 `search_by_error`，而是 `search_questions`。

#### MCP Tools

实际暴露 7 个工具，CrossLLM 只暴露：

```text
search_questions
```

可用工具列表：

```text
search_questions
search_by_tags
get_question
get_question_with_answers
get_rate_limit_status
...
```

#### CrossLLM 配置

```yaml
stackoverflow:
  enabled: true
  transport: stdio
  command: ""
  args: ["-y", "@luqezman/stackoverflow-mcp"]
  env:
    STACKOVERFLOW_API_KEY: "${STACKOVERFLOW_API_KEY}"
  tool_name: search_questions
```

#### Smoke Test

搜索 `python list index out of range`，返回 253 chars。

#### 当前状态

READY。

#### 已知问题

- 启动日志尾部有一个非致命异常：`TypeError: An asyncio.Future, a coroutine or an awaitable is required`，不影响工具调用。
- 原 `@gscalzo/stackoverflow-mcp` 已不可用，已替换为 `@luqezman/stackoverflow-mcp`。

---

### 1.8 知乎

#### 官方/仓库

- **MCP Server**：知乎搜索 MCP
- **官方文档**：https://developer.zhihu.com/docs
- **传输方式**：MCP over SSE
- **SSE URL**：https://developer.zhihu.com/api/mcp/zhihu_search/v1/sse
- **Message URL**：https://developer.zhihu.com/api/mcp/zhihu_search/v1/message
- **工具名**：`zhihu_search`
- **维护状态**：官方维护

#### 用途

知乎站内搜索，适合中文社区经验、技术讨论。

#### Transport

`sse`。

#### 安装

无需本地安装，远程 SSE 服务。

#### API Key

- `.env` 中的 `ZHIHU_ACCESS_SECRET`。
- 通过 SSE `Authorization: Bearer <token>` 鉴权。

#### Windows 注意事项

- 需要 `core/mcp_registry.py` 支持 SSE headers（已实现）。

#### MCP Tools

实际暴露 1 个工具：

```text
zhihu_search
```

参数：

```json
{
  "query": "搜索关键词",
  "count": 10
}
```

#### CrossLLM 配置

```yaml
zhihu:
  enabled: true
  transport: sse
  url: "https://developer.zhihu.com/api/mcp/zhihu_search/v1/sse"
  headers:
    Authorization: "Bearer ${ZHIHU_ACCESS_SECRET}"
  tool_name: zhihu_search
```

#### Smoke Test

搜索 `RAG`，返回 4564 chars。

#### 当前状态

READY。

#### 已知问题

- 当前未把 `zhihu` 分配给任何模型；需要时可在 `config/models.yaml` 中按需追加。

### 1.9 中文趋势聚合（trendshub）

- **NPM 包**：`mcp-trends-hub`（1.7.0，腾讯云 MCP 广场 11422，baranwang 维护）
- **官方仓库**：https://github.com/baranwang/mcp-trends-hub
- **工具**：21 个（get_36kr_trending / get_bilibili_rank / get_weibo_trending / get_zhihu_trending 等）
- **API Key**：无需
- **第一版只暴露**：`get_zhihu_trending`（只读）

### 1.10 OpenAlex 学术文献（openalex）

- **NPM 包**：`openalex-mcp-server`（1.0.5，SecretRichGarden/openAlex-mcp）
- **官方仓库**：https://github.com/SecretRichGarden/openAlex-mcp
- **数据源**：OpenAlex 开放学术数据库（替代已不可用的 `arxiv-mcp-server`）
- **工具**：8 个（openalex_search / openalex_get_work / openalex_get_fulltext_sections 等）
- **API Key**：无需
- **第一版只暴露**：`openalex_search`（只读）

### 1.11 Everything Search（everything）

- **NPM 包**：`everything-search-mcp`（1.1.0，kanishka-namdeo 维护）
- **官方仓库**：https://github.com/kanishka-namdeo/everything-search-mcp
- **工具**：3 个（search_files / get_file_info / check_status）
- **平台**：Windows 依赖 Everything 服务；macOS Spotlight / Linux ripgrep-locate 兜底
- **API Key**：无需
- **第一版只暴露**：`search_files`（只读）

### 1.12 PDF → Markdown（pdf2md）

- **NPM 包**：`@pdf2md/mcp`（0.1.0，MIT，纯本地处理）
- **官方仓库**：https://github.com/pdf2md/mcp
- **工具**：1 个 `convert_pdf`
- **API Key**：无需
- **已知问题**：`pdf-mcp-server`（2.3.0）依赖 `pdf-lib` 模块缺失，已弃用；`mcp-pdf`（1.1.0）握手超时，已弃用。

### 1.13 Excel 读取（excel）

- **NPM 包**：`@negokaz/excel-mcp-server`（0.12.0，Kazuki Negoro，MIT）
- **官方仓库**：https://github.com/negokaz/excel-mcp-server
- **工具**：7 个（含 excel_write_to_sheet / excel_create_table 等写入类）
- **API Key**：无需
- **第一版只暴露**：`excel_read_sheet`（只读）
- **已知问题**：原 `excel-mcp-server`（Python 实现）依赖 `pandas`，未安装；已替换为 `@negokaz/excel-mcp-server`（纯 Node）。
- **测试文件**：`.pytest_tmp/sample.xlsx`（zipfile 构造的最小 xlsx，专供 smoke test 使用）。

### 1.14 YouTube Transcript（yt_transcript）

- **NPM 包**：`@fabriqa.ai/youtube-transcript-mcp`（1.0.3，Cengiz Han 维护）
- **官方仓库**：https://github.com/hancengiz/youtube-transcript-mcp
- **工具**：2 个（get-transcript / get-transcript-languages）
- **API Key**：无需
- **已知问题**：YouTube Search 需 YOUTUBE_DATA_API_KEY，但 `youtube-mcp-server` 包无 bin（`npm error could not determine executable`）；`@umbertotancorre/youtube-mcp`（1.1.1）首次下载 yt-dlp+ffmpeg 超时。如需视频搜索，需用户自行部署支持 bin 的替代包。

### 1.15 Hugging Face（huggingface）

- **NPM 包**：`@llmindset/hf-mcp-server`（0.4.16，Hugging Face 官方）
- **官方仓库**：https://github.com/huggingface/hf-mcp-server
- **工具**：默认 4 个（hf_whoami / hub_repo_search / hub_repo_details / hf_fs）
- **API Key**：需 `HF_TOKEN`（从 `.env` 读取）
- **第一版只暴露**：`hub_repo_search`（只读）
- **已知问题**：未配置 `HF_TOKEN` 时仍可连接、列出工具，但实际调用受限。

### 1.16 Reddit（reddit）

- **PyPI 包**：`mcp-server-reddit`（0.2.1，Hawstein 维护，MIT）
- **官方仓库**：https://github.com/hawstein/mcp-server-reddit
- **底层依赖**：`redditwarp`（PyPI 1.3.0，调用 Reddit 公共 API）
- **工具**：6 个（get_frontpage_posts / get_subreddit_info / get_post_content / get_post_comments / get_subreddit_hot_posts / get_subreddit_mods），全部只读
- **API Key**：无需（公共 API）
- **第一版只暴露**：`get_frontpage_posts`（只读）
- **兼容垫片**：`scripts/wrappers/reddit_server.py`——`mcp-server-reddit` 0.2.1 用了较新 mcp 的 API（McpError、`Server.list_tools` 装饰器），本项目用 mcp 1.29.1；垫片只导入并 `asyncio.run(server.serve())`，未做 monkey-patch（mcp 1.29.1 API 与包内用法兼容）。
- **历史**：曾用 `mcp-reddit`（nitaiaharoni1）20 工具 + OAuth；本版本无需凭证、全部只读、更契合"个人研究工具"定位。

---

## 2. CrossLLM 模型工具配置

当前只给 `minimax_m2_7` 配置完整工具池，其余模型保持现状，待多 MCP 验证稳定后再决定是否扩大。

```yaml
# config/models.yaml
- id: minimax_m2_7
  tools:
    - openwebsearch
    - github
    - fetch
    - filesystem
    - context7
    - playwright
    - stackoverflow
```

其他模型仍只配置 `openwebsearch`。

设计说明：
- 不是给每个模型固定一小部分 MCP，而是先让一个稳定模型拥有可控工具池，由模型在任务中自行选择是否调用。
- 当前第一版不做复杂的 tool-group/router；确认单模型多 MCP 工作后再扩大工具池。

---

## 3. 健康检查报告

### 3.1 检查命令

```powershell
.venv\Scripts\python scripts\manual\run_mcp_health.py
```

### 3.2 最新结果（2026-09-01 20:08）

```text
MCP HEALTH CHECK
Time: 2026-09-01 20:08
Config: .\config\mcp.yaml
------------------------------------------------------------

READY: 16
  - context7        - everything      - excel           - fetch
  - filesystem      - github          - huggingface     - openalex
  - openwebsearch   - pdf2md          - playwright      - reddit
  - stackoverflow   - trendshub       - yt_transcript   - zhihu

Total: 16 | READY: 16
```

注：
- `huggingface` 与 `reddit` 在 `.env` 未配置对应 Token 时，仍可连接 + 列工具，smoke 也返回数据；实际搜索/调用需配置 Token 后才能得到实质结果。
- `pdf2md` 的 smoke 用 `README.md` 作为占位文件，返回的是"非 PDF"错误文本（被归类为 smoke 成功但内容无意义）；真实 PDF 转换需在 E2E 阶段传 `.pdf` 文件。

完整 JSON 报告：`.\.mcp_health_report.json`（每次运行覆盖）。

---

## 4. 真实 LLM 综合 E2E

### 4.1 测试问题

```text
请调查 faster-whisper 的 VAD 问题。要求：
1) 搜索 GitHub Issue 看常见故障；
2) 用网页搜索找官方文档或社区资料；
3) 读取本项目 docs/mcp.md 了解当前 MCP 配置；
4) 如果 Stack Overflow 有类似讨论也参考进来；
5) 给出综合结论。
```

### 4.2 测试命令

```powershell
.venv\Scripts\python scripts\manual\run_mcp_e2e.py
```

### 4.3 预期链路

```text
LLM → tool_call → MCP → tool_result → LLM → Final Answer
```

### 4.4 当前状态

已执行成功。完整链路 `LLM → tool_call → MCP → tool_result → LLM → Final Answer` 已跑通。

- **测试模型**：`minimax_m2_7`
- **分配工具**：`openwebsearch`、`github`、`fetch`、`filesystem`、`context7`、`playwright`、`stackoverflow`（7 个）
- **实际调用工具**：`github`、`openwebsearch`、`fetch`（模型自行选择，未强制使用全部工具）
- **工具结果截断**：单个 tool_result 超过 12,000 字符时自动截断，避免撑爆上下文。
- **工具调用上限**：单轮总调用次数上限 15 次，防止模型无限收集信息。
- **Final Answer**：约 3732 字符，包含 `# faster-whisper 常见 VAD 问题调查报告`、GitHub Issues 汇总、问题分类表格与综合结论。
- **耗时**：约 87.92s（含多次工具调用与 LLM 生成）。

典型 tool_call 示例（来自日志）：

```text
github       q='repo:SYSTRAN/faster-whisper VAD is:issue' per_page=30 sort='comments'  → 12017 chars
openwebsearch query='faster-whisper VAD voice activity detection issues problems' limit=15  → 5203 chars
fetch         url='https://github.com/SYSTRAN/faster-whisper/issues?q=vad+is%3Aissue'  → 12017 chars
```

### 4.5 后续建议

- 当前 `minimax_m2_7` 已验证多 MCP Tool Calling；后续可按需扩大工具池到其他模型。
- 工具调用上限（15 次）和结果截断（12,000 字符）是第一版保护机制，后续可根据模型 context_limit 和实际效果调优。

---

## 5. 代码改动摘要

| 文件 | 改动 | 原因 |
|------|------|------|
| `config/mcp.yaml` | 新增 fetch / filesystem / context7 / playwright / stackoverflow / zhihu | 批量接入剩余 MCP |
| `config/models.yaml` | minimax_m2_7 配置 7 个工具；其他模型保持现状 | 先验证单模型多 MCP Tool Calling |
| `core/mcp_registry.py` | 新增 `headers` 字段；支持 `${ENV}` 占位符解析；新增 tool_result 截断（12,000 chars） | SSE 鉴权；防止工具结果撑爆上下文 |
| `providers/real_provider.py` | 新增单轮最大工具调用次数限制（15 次） | 防止模型在信息收集阶段无限循环 |
| `scripts/manual/run_mcp_health.py` | 新增；独立加载 `.env` | 批量健康检查脚本 |
| `scripts/manual/run_mcp_e2e.py` | 从 `test_mcp_e2e.py` 重命名并移动；修复 SSE 事件类型 | 避免被 pytest 误收集；适配真实事件流 |
| `conftest.py` | 新增；自定义 `tmp_path` fixture 保留临时目录 | 避免 pytest 默认清理触发沙箱 safe-delete 导致退出码 1 |
| `scripts/wrappers/reddit_server.py` | 新增；mcp-server-reddit (PyPI) 启动入口 | Reddit MCP 切换到 Hawstein 版本，通过 wrapper 接入 |
| `requirements.txt` | 新增 `mcp-server-reddit` / `redditwarp` | Reddit MCP 依赖 |
| `core/mcp_runtime_log.py` | 新增；MCP Runtime 调试日志（JSONL） | 点对点追踪每轮 tool_call；详见 §6 |
| `core/mcp_registry.py` | `call_tool` 旁路记录 runtime 日志 | 不改变调用行为；失败路径也记录 |
| `core/round_manager.py` | `_run_one` 设置 runtime 上下文（contextvars） | 同轮多模型互不串扰 |
| `test_mcp_runtime_log.py` | 新增；14 个测试 | 覆盖脱敏 / JSONL / 成功失败路径 / 行为不变性 |

---

## 6. MCP Runtime 调试日志

在**不改变现有 MCP 调用逻辑**的前提下，为每次 tool_call 落一条 JSONL 记录。

### 6.1 路径与格式

```text
sessions/<session_id>/round_<NNN>/mcp_runtime.jsonl
```

每行一个 JSON 对象，字段对应需求 1–11：

| 字段 | 说明 |
|------|------|
| `tool_call` | LLM 发出的原始 tool_call（registry 层为 name + arguments） |
| `mcp` | MCP Server 名称（如 `github`） |
| `tool` | Server 内实际工具名（如 `search_issues`） |
| `arguments` | 调用参数（已脱敏） |
| `started_at` | call 开始时间（ISO 8601） |
| `ok` | MCP 返回成功 / 失败 |
| `result_chars` | 返回字符数（截断后） |
| `result_preview` | 返回内容前 200 字符（已脱敏） |
| `error_type` / `error_message` | 异常类型与信息（已脱敏） |
| `returned_at` | tool_result 回传给 LLM 的时间 |
| `log_path` | 本日志文件路径（自我说明，便于检索） |

### 6.2 实现约束（全部满足）

- **不改变 MCP 行为**：`_record_runtime_log` 整体 try/except，日志失败静默吞掉；`call_tool` 返回值与异常传播不变。
- **不修改 provider**：日志在 `core/mcp_registry.call_tool` 旁路完成；`round_manager._run_one` 只负责用 contextvars 设置轮次目录。
- **不修改工具选择 / context budget**：无任何涉及。
- **脱敏**：敏感 key（Authorization/Token/Cookie/API Key 等）整值替换 `***REDACTED***`；已知凭证形态（`Bearer …`、`sk-…`、`ghp_…`、`github_pat_…`、`hf_…`）正则兜底。
- **与 health check 分开**：健康检查不依赖本模块；无轮次上下文时不落盘。
- **JSONL**：每行独立 JSON，可用 `core.mcp_runtime_log.read_records(dir)` 读取。
- **无新框架**：只用标准库 `json` / `pathlib` / `datetime` / `contextvars`。

### 6.3 验证（2026-09-02 E2E 实测）

```text
sessions/20260902-080006-25b907/round_001/mcp_runtime.jsonl  # 15 条记录
  github / search_issues    ok=False err=McpError   ← 失败也被捕获
  github / search_issues    ok=True  chars=12072
  openwebsearch / search    ok=True  chars=3484
  zhihu / zhihu_search      ok=True  chars=10700  ← 模型首次自行调用知乎
  fetch / get_markdown      ok=True  chars=12057
credential leak check: clean
```

### 6.4 session 失效自动重连（ClosedResourceError 最小修复）

**根因**（2026-09-02 E2E `sessions/20260902-072452-129383/round_003` 中 zhihu_search
出现 `ClosedResourceError`、`duration_ms=0`）：

- `_get_session` 创建 `(session, cm)` 后**永久缓存**，代码从不主动关闭（除退出时 `close_all`）。
- 知乎是远程 SSE：服务端/网络断开后底层流关闭，缓存的 `ClientSession` 随之失效；
  `duration_ms=0` 说明请求发出前 session 已死。
- 失效 session **仍留在 `self._sessions` 缓存中**，后续每次调用都立刻抛
  `ClosedResourceError`——这与健康检查全绿并不矛盾（健康检查每次都是全新连接）。

**最小修复**（`core/mcp_registry.call_tool`）：

```text
捕获 ClosedResourceError
  → _drop_session(server)：从缓存移除并 _safe_aexit 旧 session/cm
  → _get_session(server)：重建（失败则记 reconnect ok=False 后原样抛出）
  → 用新 session 重试一次（重试仍失败则原样向外抛，不再重连）
runtime JSONL 追加一条 {"event": "reconnect", "mcp":..., "reason":...,
"attempt":1, "ok": true/false} 记录
```

- 不引入 Playwright / 不更换知乎 MCP / 不修改 MCP Server / 不引入连接池。
- 单元测试（`test_mcp_runtime_log.py`）：
  - `test_reconnect_after_session_closed`：第一次调用后人为关闭 cache 中的 session，
    第二次调用自动重连并成功，cache 已替换为新 session，JSONL 有一条 `ok=true` 的 reconnect。
  - `test_retry_only_once_and_reraise`：重试一次仍失败 → 原样抛出，只创建 2 个 session
    （初始 + 重连），tool_call 记录 `error_type=ClosedResourceError`。
  - `test_reconnect_without_runtime_context_still_works`：无轮次上下文时重连照常工作、不落日志。

### 6.5 验证（2026-09-02 E2E 实测 ×2）

```text
sessions/20260902-080006-25b907/round_001  # 15 条记录（含 zhihu 10700 chars）
sessions/20260902-083230-d647a8/round_001  # 15 条记录（模型调用 github/openwebsearch/
                                           # openalex/fetch/stackoverflow，Final Answer 3903 chars）
credential leak check: clean（两次）
```

注：ClosedResourceError 是间歇性的（取决于远程 SSE 连接是否在两次调用间被服务端断开），
第二次 E2E 未触发重连属正常；重连路径已由单元测试完整覆盖。

---

## 7. 遗留问题

1. **Stack Overflow 内部异常**：启动日志尾部有非致命 async Future 异常，不影响功能，可长期观察。
2. **GitHub 社区版 deprecated**：建议后续迁移到官方 `github-mcp-server`（需 Docker 或 Go 编译）。
3. **Context7 免费额度**：未配置 `CONTEXT7_API_KEY`，高频使用可能触发 rate limit。
4. **知乎未分配给模型**：知乎 MCP 已 READY，但当前未分配给任何模型；需要中文社区搜索时可在 `config/models.yaml` 中追加。
5. **工具调用策略待调优**：当前单轮上限 15 次、结果截断 12,000 字符是第一版保守值，后续可根据效果调整。
6. **HuggingFace 需配 Token**：reddit 已切换到无需凭证的 mcp-server-reddit（redditwarp 公共 API）；huggingface 仍需 `HF_TOKEN`，未配置时实际调用受限。
7. **Word 暂未接入**：候选 `docxmcp`（mammoth 依赖损坏）、`mcp-word`（micromark 依赖损坏）、`mcp-server-anydoc`（Windows 启动问题）均不可用；如需 docx 读取，建议用户自行实现或选用其他包。
8. **YouTube Search 未接入**：需要 `YOUTUBE_API_KEY`，且 `youtube-mcp-server` 包无 bin；如需视频搜索，需用户自行部署支持 bin 的替代包。
9. **arxiv-mcp-server 不可用**：包能安装但 stdio 握手不响应；已用 `openalex-mcp-server` 替代学术搜索。
10. **未做 E2E 验证**：本次新增 8 个 MCP 仅通过 health check，未跑 LLM 端到端；按需可后续追加 E2E。
