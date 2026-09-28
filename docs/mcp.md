# 知乎搜索 MCP

## 接口说明

该服务通过 MCP over SSE 提供知乎站内搜索能力，适合接入支持 MCP 的 Agent、助手或工作流系统。

当前服务仅提供工具能力，不提供资源（resources）与提示词（prompts）能力。

## 接口信息

| 说明 | 值 |
| :- | :- |
| SSE URL | `https://developer.zhihu.com/api/mcp/zhihu_search/v1/sse` |
| Message URL | `https://developer.zhihu.com/api/mcp/zhihu_search/v1/message` |
| 传输方式 | `MCP over SSE` |
| 工具名 | `zhihu_search` |

说明：

- 客户端先连接 `sse` 端点。
- 服务端会通过 `endpoint` 事件返回实际可用的 `message` 地址，地址中会带 `sessionId`。
- 后续 `initialize`、`tools/list`、`tools/call` 请求均发送到该 `message` 地址。

## 鉴权

请求头：

- `Authorization: Bearer <your_access_secret>`

说明：

- 建议在 `sse` 连接和后续 `message` 请求中均携带同一份鉴权信息。

## 工具定义

### `zhihu_search`

#### 入参

| 名称 | 类型 | 必填 | 说明 |
| :- | :- | :- | :- |
| `query` | String | 是 | 搜索关键词，长度 2-100 个字符 |
| `count` | Number | 否 | 返回条数，取值范围 1-10，默认 `10` |

#### 返回

工具调用结果为 `text` 类型内容，正文为面向大模型消费的结构化文本。

返回主体示例：

```text
<zhihu_search query="RAG">
<search_item title="RAG 评测方法综述" content_type="Article" url="https://..." author_name="张三" author_avatar="https://..." author_badge_text="" edit_time="2025-03-01 10:00:00 +0000 UTC" authority_level="2" ranking_score="0.9800">
本文介绍了主流 RAG 评测框架，包括 RAGAS、TruLens ...
</search_item>
</zhihu_search>
```

说明：

- 返回值外层是 MCP `text` 类型，文本内容为 XML。
- 建议将整段 XML 原样交给模型消费，不建议自行裁剪字段。

## 接入流程

### 1. 建立 SSE 连接

```bash
curl -N 'https://developer.zhihu.com/api/mcp/zhihu_search/v1/sse' \
  -H 'Authorization: Bearer <your_access_secret>' \
  -H 'Accept: text/event-stream'
```

服务端会先返回一条 `endpoint` 事件，例如：

```text
event: endpoint
data: /api/mcp/zhihu_search/v1/message?sessionId=xxx
```

### 2. 初始化 MCP 会话

将上一步拿到的 `message` 地址记为 `MESSAGE_URL`。

```bash
curl -X POST "$MESSAGE_URL" \
  -H 'Authorization: Bearer <your_access_secret>' \
  -H 'Content-Type: application/json' \
  -d '{
    "jsonrpc": "2.0",
    "id": 1,
    "method": "initialize",
    "params": {
      "protocolVersion": "2024-11-05",
      "clientInfo": {
        "name": "demo-client",
        "version": "1.0.0"
      },
      "capabilities": {}
    }
  }'
```

说明：

- `message` 端点通常会先返回 HTTP `202 Accepted`。
- 实际 JSON-RPC 响应会通过已建立的 SSE 通道异步返回。

### 3. 获取工具列表

```bash
curl -X POST "$MESSAGE_URL" \
  -H 'Authorization: Bearer <your_access_secret>' \
  -H 'Content-Type: application/json' \
  -d '{
    "jsonrpc": "2.0",
    "id": 2,
    "method": "tools/list"
  }'
```

### 4. 调用搜索工具

```bash
curl -X POST "$MESSAGE_URL" \
  -H 'Authorization: Bearer <your_access_secret>' \
  -H 'Content-Type: application/json' \
  -d '{
    "jsonrpc": "2.0",
    "id": 3,
    "method": "tools/call",
    "params": {
      "name": "zhihu_search",
      "arguments": {
        "query": "RAG",
        "count": 5
      }
    }
  }'
```

SSE 通道中会收到对应响应，例如：

```text
event: message
data: {"jsonrpc":"2.0","id":3,"result":{"content":[{"type":"text","text":"<zhihu_search query=\"RAG\">..."}]}}
```

## 注意事项

1. 该服务采用标准 MCP 工具调用模式，推荐直接使用现成 MCP Client 接入。
2. `tools/call` 的结果通过已建立的 SSE 通道返回，而不是直接同步返回在 POST 响应体中。
3. `query` 建议尽量具体，以获得更稳定的搜索结果。



# CrossLLM Phase 3：MCP 部署文档

## 0. 适用范围

CrossLLM 运行环境：

```text
Windows
Python
Node.js / npm
uv / uvx
```

MCP 主要采用：

```text
本地 MCP Server
    ↓
stdio
    ↓
CrossLLM MCP Client
```

部分服务也提供远程 HTTP / SSE。

本文件只记录：

* 安装；
* 环境变量；
* MCP Server 启动方式；
* 基本配置；
* Windows 注意事项；
* CrossLLM 后续接入所需的连接信息。

不在本阶段讨论 Tool 质量评分、Agent Planner、MCP Router 等高级机制。

---## 1.1 Open-WebSearch 网络搜索 MCP ### 项目 - MCP Server：Open-WebSearch - GitHub：Aas-ee/open-websearch - 腾讯 MCP 广场：https://cloud.tencent.com/developer/mcp/server/11739 ### 用途多引擎 Web Search + 部分网页/站点内容获取。当前支持的搜索来源以服务器实际版本为准。腾讯云页面当前列出：- Bing - Baidu - DuckDuckGo - Exa - Brave - CSDN - Juejin Linux.do 当前页面说明暂不支持。无需 API Key。:contentReference[oaicite:9]{index=9} ### Windows 安装推荐：```powershell npx open-websearch@latest

或者：

npm install -g open-websearch

然后：

open-websearch
HTTP / MCP 模式

服务默认端口：

3000

Streamable HTTP：

http://localhost:3000/mcp

SSE：

http://localhost:3000/sse

腾讯云页面同时提供两种 MCP 客户端配置方式。

主要环境变量
DEFAULT_SEARCH_ENGINE=bing
ENABLE_CORS=false
CORS_ORIGIN=*
USE_PROXY=false
PROXY_URL=http://127.0.0.1:7890
PORT=3000

其中：

DEFAULT_SEARCH_ENGINE：默认搜索引擎
USE_PROXY：是否使用 HTTP 代理
PROXY_URL：代理地址
PORT：HTTP 服务端口

官方默认值以当前版本为准。

Windows PowerShell
$env:DEFAULT_SEARCH_ENGINE="bing"
$env:PORT="3000"
npx open-websearch@latest

启用代理：

$env:USE_PROXY="true"
$env:PROXY_URL="http://127.0.0.1:7890"
npx open-websearch@latest

注意：

npm 下载代理和open-websearch 运行时代理是两个不同问题。安装阶段必要时使用 npm 自己的 --proxy / --https-proxy 或 npm config；USE_PROXY / PROXY_URL 只控制 Open-WebSearch 运行阶段的网络请求。

MCP 工具

腾讯页面当前列出：

search
fetchLinuxDoArticle
fetchCsdnArticle
fetchGithubReadme

GitHub 当前版本还提供更通用的网页抓取能力，具体工具列表以实际启动后的 MCP tools/list 为准。
search

调用格式：

{
  "query": "搜索内容",
  "limit": 3,
  "engines": ["bing", "juejin"]
}

其中：

query：搜索词
limit：结果数量
engines：指定一个或多个搜索来源

腾讯页面当前给出的示例就是多引擎组合搜索。

CrossLLM 使用原则

Open-WebSearch 可以选择多个搜索来源，但不要默认所有来源同时搜索。

优先根据问题选择有限数量的来源，例如：

普通实时信息
→ Bing

中文技术内容
→ Bing + Juejin

代码/开源
→ Bing + GitHub

特定站点内容
→ 对应站点搜索/抓取

# 1.2 Fetch

### 用途

已知 URL：

```text
URL
↓
HTML
↓
Markdown
```

与 OpenWebSearch 的关系：

```text
OpenWebSearch = 找
Fetch = 读
```

服务器来自 MCP 官方 servers，支持 `fetch`，可通过 `start_index` 分块读取长网页。

### 推荐安装

使用 uvx：

```powershell
uvx mcp-server-fetch
```

也可以：

```powershell
pip install mcp-server-fetch
```

然后：

```powershell
python -m mcp_server_fetch
```

### 核心工具

```text
fetch
```

参数：

```json
{
  "url": "https://example.com",
  "max_length": 5000,
  "start_index": 0,
  "raw": false
}
```

### 注意

官方页面特别提醒：

> 该服务器可以访问本地/内部 IP，因此存在 SSRF / 内部网络暴露风险。

CrossLLM 第一版如果只需要公网资料，应限制访问范围，避免任意内网 URL。

---

# 1.3 Playwright

### 用途

普通 Fetch 解决不了的动态网页：

```text
网页
↓
JavaScript
↓
XHR/API
↓
动态内容
```

使用真实浏览器加载。

腾讯 MCP 广场目前有多个 Playwright 实现，其中 ExecuteAutomation 版本提供：

* 页面抓取；
* JavaScript 执行；
* 截图；
* 浏览器交互；
* 设备模拟等。

### 推荐安装

```powershell
npx -y @executeautomation/playwright-mcp-server
```

该版本首次运行可以自动安装浏览器，也可以手动：

```powershell
npx playwright install chromium
```

或：

```powershell
npx playwright install
```

### CrossLLM 用途

优先：

```text
navigate
read
screenshot
```

不要默认开放：

```text
login
payment
delete
arbitrary write
```

### 使用位置

```text
OpenWebSearch
↓
发现动态网页
↓
Playwright
↓
提取内容
```

---

# 2. 技术开发

# 2.1 GitHub MCP

### 用途

* Repository
* README
* Code Search
* Issue
* PR
* 文件读取

腾讯 MCP 广场当前 GitHub MCP Server Plus 支持代码搜索、Issue / PR 搜索等 GitHub 查询语法。

### 官方本地安装方式

Docker：

```json
{
  "mcpServers": {
    "github": {
      "command": "docker",
      "args": [
        "run",
        "-i",
        "--rm",
        "-e",
        "GITHUB_PERSONAL_ACCESS_TOKEN",
        "mcp/github"
      ],
      "env": {
        "GITHUB_PERSONAL_ACCESS_TOKEN": "<TOKEN>"
      }
    }
  }
}
```

或者使用 npm：

```json
{
  "mcpServers": {
    "github": {
      "command": "npx",
      "args": [
        "-y",
        "@modelcontextprotocol/server-github"
      ],
      "env": {
        "GITHUB_PERSONAL_ACCESS_TOKEN": "<TOKEN>"
      }
    }
  }
}
```

### Token

只处理公共仓库：

```text
public_repo
```

如果要访问私有仓库：

```text
repo
```

### CrossLLM 建议

只读功能优先：

```text
search_code
search_issues
get_file_contents
get_repo
```

没有需求时不要给模型写仓库权限。

---

# 2.2 Stack Overflow MCP

Repository：

```text
https://github.com/gscalzo/stackoverflow-mcp
```

当前仓库主要支持：

```text
search_by_error
search_by_tags
analyze_stack_trace
```

并支持：

* 最低 score；
* comments；
* JSON / Markdown；
* result limit。

### 安装

无需 clone：

```powershell
npx -y @gscalzo/stackoverflow-mcp
```

或者：

```powershell
npm install -g @gscalzo/stackoverflow-mcp
```

### 配置

```json
{
  "mcpServers": {
    "stackoverflow": {
      "command": "npx",
      "args": [
        "-y",
        "@gscalzo/stackoverflow-mcp"
      ],
      "env": {
        "STACKOVERFLOW_API_KEY": "your-key-optional"
      }
    }
  }
}
```

API Key 是可选的。没有 Key 也能工作，但存在 rate limit。官方 README 建议需要更高限额时从 Stack Apps 获取 API Key。

### 最适合你的工具

```text
search_by_error
analyze_stack_trace
```

特别适合：

```text
Python
PyTorch
CUDA
faster-whisper
```

之类的实际报错。

---

# 2.3 Context7

### 用途

直接获取指定库的：

* 当前文档；
* 特定版本文档；
* 代码示例。

它主要解决 LLM 用旧版本 API 或虚构 API 的问题。

### 推荐远程 MCP

Endpoint：

```text
https://mcp.context7.com/mcp
```

配置：

```json
{
  "mcp": {
    "context7": {
      "type": "remote",
      "url": "https://mcp.context7.com/mcp",
      "headers": {
        "CONTEXT7_API_KEY": "YOUR_API_KEY"
      },
      "enabled": true
    }
  }
}
```

Context7 支持免费 API Key，官方建议需要更高速率限制时配置 Key。

### 本地方式

```json
{
  "mcpServers": {
    "context7": {
      "command": "npx",
      "args": [
        "-y",
        "@upstash/context7-mcp",
        "--api-key",
        "YOUR_API_KEY"
      ]
    }
  }
}
```

### 工具

```text
resolve-library-id
query-docs
```

典型：

```text
resolve-library-id("faster-whisper")
↓
query-docs(...)
```

---

# 3. AI / 模型

# 3.1 Hugging Face MCP

### 用途

只读访问 Hugging Face Hub：

```text
Models
Datasets
Spaces
Papers
Collections
```

支持搜索和获取详情。

### 安装

可以直接使用 uv：

```powershell
uv run huggingface_mcp_server.py
```

如果使用仓库目录：

```json
{
  "mcpServers": {
    "huggingface": {
      "command": "uv",
      "args": [
        "--directory",
        "C:\\path\\to\\huggingface-mcp-server",
        "run",
        "huggingface_mcp_server.py"
      ],
      "env": {
        "HF_TOKEN": "YOUR_HF_TOKEN"
      }
    }
  }
}
```

HF_TOKEN 是可选的。

作用：

```text
没有 Token
→ 公共资源

有 Token
→ 更高 rate limit
→ 私有仓库
```

### 工具

```text
search-models
get-model-info
search-datasets
get-dataset-info
search-spaces
get-space-info
get-paper-info
get-daily-papers
search-collections
get-collection-info
```

对你的 ASR / OCR / VLM 场景非常直接。

---

# 4. 社区

# 4.1 Reddit MCP

腾讯 MCP 广场存在多个 Reddit Server。

推荐使用只读 Reddit Reader。

当前一种实现支持：

```text
subreddit 热帖
帖子正文
评论
```

可以通过：

```powershell
npx -y @smithery/cli install @adhikasp/mcp-reddit --client claude
```

安装。

另一种 Reddit Server 基于 Reddit 公共 API / redditwarp，提供首页、上升帖子、帖子内容、评论等工具。

### CrossLLM 用途

```text
实际使用体验
工具评价
社区争论
模型体验
```

不是官方技术文档。

---

# 4.2 知乎

已完成。

不要再安装前面找到的第三方 Cookie/逆向版本。

CrossLLM 使用你已经获得的**知乎官方 MCP**。

配置及授权信息以：

```text
https://developer.zhihu.com/docs
```

为准。

---

# 4.3 腾讯开发者社区

### 重要说明

目前未找到一个明确独立的：

> “腾讯开发者社区站内文章/问答搜索 MCP Server”

因此不能直接编造一个 MCP 配置。

腾讯官方 MCP 仓库确实提供：

```text
TencentCloud MCP Servers
```

以及官方：

```text
WebSearchMCP
```

但后者是**全网联网搜索**，底层为腾讯云 Web Search API / 搜狗搜索，并非腾讯开发者社区站内搜索。

腾讯开发者社区本身有：

```text
文章
问答
视频
技术专区
```

等内容搜索。

因此当前处理：

```text
腾讯开发者社区
→ 没有明确独立 MCP
→ 通过 OpenWebSearch + site:cloud.tencent.com/developer
```

或者以后找到官方站内 MCP 再补。

---

# 4.4 MCP 中文趋势聚合

### 用途

不是技术知识库。

主要作用：

> **发现新的 AI 术语、项目、热点。**

当前服务聚合 20+ 数据源，包括：

* 36Kr
* Bilibili
* 抖音
* 腾讯新闻
* 澎湃
* 微博
* 知乎
* 少数派
* BBC
* The Verge
  等。

### 安装

```powershell
npx -y mcp-trends-hub@1.6.2
```

### 配置

```json
{
  "mcpServers": {
    "trends-hub": {
      "command": "npx",
      "args": [
        "-y",
        "mcp-trends-hub@1.6.2"
      ]
    }
  }
}
```

### 自定义 RSS

```json
{
  "mcpServers": {
    "trends-hub": {
      "command": "npx",
      "args": [
        "-y",
        "mcp-trends-hub"
      ],
      "env": {
        "TRENDS_HUB_CUSTOM_RSS_URL": "https://example.com/rss"
      }
    }
  }
}
```

### CrossLLM 定位

```text
趋势中心
↓
发现新术语
↓
OpenWebSearch / GitHub / HF
↓
验证真实含义
```

不要直接把热榜当作权威资料。

---

# 5. Academic Paper Search

### 当前候选

腾讯 MCP 广场的 Academic Paper Search 支持：

```text
search_papers
fetch_paper_details
search_by_topic
```

数据源包括：

```text
Semantic Scholar
Crossref
```

并可以在可用时返回论文全文/PDF URL。

### 手动部署

需要：

```powershell
uv add "mcp[cli]" httpx
```

然后：

```powershell
uv run server.py
```

### 配置

```json
{
  "mcpServers": {
    "academic-search": {
      "command": "uv",
      "args": [
        "run",
        "C:\\path\\to\\server.py"
      ],
      "env": {
        "SEMANTIC_SCHOLAR_API_KEY": "YOUR_KEY",
        "CROSSREF_API_KEY": "YOUR_KEY"
      }
    }
  }
}
```

其中当前页面注明：

```text
SEMANTIC_SCHOLAR_API_KEY
→ 文档虽列出，但当前实现没有真正使用

CROSSREF_API_KEY
→ optional but recommended
```

### 许可证

AGPL-3.0。

如果未来 CrossLLM 发布到 GitHub，需要特别考虑许可证问题。

---

# 6. 本地文件

# 6.1 Everything Search

### 用途

利用 Windows Everything 索引高速找文件。

关系：

```text
Everything
→ 找

Filesystem
→ 读
```

Windows 前提：

1. 安装 Everything；
2. 确保 Everything 服务正在运行；
3. 安装 Everything SDK；
4. 获得：

```text
Everything64.dll
```

### 安装

```powershell
pip install mcp-server-everything-search
```

或者：

```powershell
uvx mcp-server-everything-search
```

### Windows 环境变量

```text
EVERYTHING_SDK_PATH=C:\path\to\Everything64.dll
```

例如：

```json
{
  "mcpServers": {
    "everything-search": {
      "command": "uvx",
      "args": [
        "mcp-server-everything-search"
      ],
      "env": {
        "EVERYTHING_SDK_PATH": "C:\\Tools\\Everything-SDK\\dll\\Everything64.dll"
      }
    }
  }
}
```

---

# 6.2 Filesystem

### 用途

读取本地文件：

```text
read_file
read_multiple_files
list
search
metadata
```

官方 filesystem server 可以限制允许访问的目录。

### 推荐

CrossLLM 第一版只允许：

```text
read
list
search
metadata
```

不要默认：

```text
write
delete
move
```

### 使用

本地 filesystem server 需要指定允许目录。

概念：

```text
filesystem-server
  C:\AI
  D:\Project
```

服务器只允许操作配置中的目录。

---

# 7. 文档

# 7.1 PDF2MD

### 用途

```text
PDF
↓
MinerU API
↓
Markdown
```

支持：

* 本地 PDF；
* URL PDF；
* 批量；
* 表格；
* 多栏；
* 结构化 Markdown。

### 安装

源码方式：

```powershell
git clone <repository>
cd mcp-pdf2md
uv sync
```

### 配置

`.env`：

```text
MINERU_API_KEY=YOUR_KEY
```

或者 MCP 配置：

```json
{
  "mcpServers": {
    "pdf2md": {
      "command": "uv",
      "args": [
        "--directory",
        "C:\\path\\to\\mcp-pdf2md",
        "run",
        "pdf2md",
        "--output-dir",
        "C:\\path\\to\\output"
      ],
      "env": {
        "MINERU_API_KEY": "YOUR_KEY"
      }
    }
  }
}
```

工具：

```text
convert_pdf_url
convert_pdf_file
```

### 注意

这是：

> **云端 MinerU API**

不是完全本地 PDF 解析。

当前文档还说明 MinerU API 测试资格需要申请。

---

# 7.2 Excel MCP

推荐使用无需安装 Excel 的 `Excel MCP Server`。

### 前提

```text
Python >= 3.10
```

### stdio

stdio 模式下每次工具调用直接传文件路径，不需要设置专门的文件目录变量。

### 安装

```powershell
uvx excel-mcp-server
```

### SSE

如果使用 SSE：

```powershell
$env:EXCEL_FILES_PATH="E:\MyExcelFiles"
$env:FASTMCP_PORT="8080"
uvx excel-mcp-server sse
```

### 工具

支持：

```text
读取
写入
格式
图表
透视表
工作表
```

第一版 CrossLLM 主要使用：

```text
读取
分析
```

### 许可证

MIT。

---

# 7.3 Word

当前腾讯 MCP 广场存在多个 Word MCP。

其中 `Office-Word-MCP-Server`：

### 前提

```text
Python >= 3.8
pip
```

### 安装

```powershell
git clone https://github.com/GongRzhe/Office-Word-MCP-Server.git
cd Office-Word-MCP-Server
pip install -r requirements.txt
```

也可以：

```powershell
python setup_mcp.py
```

或者使用 uvx：

```json
{
  "mcpServers": {
    "word-document-server": {
      "command": "uvx",
      "args": [
        "--from",
        "office-word-mcp-server",
        "word_mcp_server"
      ]
    }
  }
}
```

第一版主要使用：

```text
read
analyze
```

写入功能暂时不需要。

---

# 8. 视频

# 8.1 YouTube Search + Transcript

推荐直接使用功能较完整的 YouTube MCP，而不是分别维护两个完全不同的 Server。

当前 `YouTube Data MCP`：

```text
searchVideos
getVideoDetails
getTranscripts
getRelatedVideos
getChannelStatistics
getChannelTopVideos
getTrendingVideos
compareVideos
```

同时支持：

```text
搜索
字幕
频道
播放列表
```

### 安装

```powershell
npm install youtube-data-mcp-server
```

或者：

```powershell
npx -y youtube-data-mcp-server
```

### API Key

必须：

```text
YOUTUBE_API_KEY
```

Google Cloud：

```text
Google Cloud Console
↓
新建/选择项目
↓
启用 YouTube Data API v3
↓
创建 API Key
```

配置：

```json
{
  "mcpServers": {
    "youtube": {
      "command": "npx",
      "args": [
        "-y",
        "youtube-data-mcp-server"
      ],
      "env": {
        "YOUTUBE_API_KEY": "YOUR_API_KEY",
        "YOUTUBE_TRANSCRIPT_LANG": "en"
      }
    }
  }
}
```

### 优势

一个 MCP 同时解决：

```text
YouTube Search
+
Video Info
+
Transcript
```

因此不需要再额外部署一个 Search MCP 和一个 Transcript MCP。

---

# 8.2 只要字幕、不需要 YouTube API 的方案

另一个 `mcp-youtube-transcript`：

```text
get_transcript
get_timed_transcript
get_video_info
```

使用：

```powershell
uvx --from git+https://github.com/jkawamoto/mcp-youtube-transcript mcp-youtube-transcript
```

配置：

```json
{
  "mcpServers": {
    "youtube-transcript": {
      "command": "uvx",
      "args": [
        "--from",
        "git+https://github.com/jkawamoto/mcp-youtube-transcript",
        "mcp-youtube-transcript"
      ]
    }
  }
}
```

### 长视频

这个实现专门处理长字幕：

```text
超过 50,000 字符
↓
分页
↓
next_cursor
↓
下一页
```

也可以：

```text
--response-limit 15000
```

控制每次响应大小。

因此：

> **可以读取很长的视频字幕，但不是任意 YouTube 视频都能自动 ASR。**

没有字幕时，这类 Transcript MCP 不能代替你的 Whisper。

---

# 9. MCP 与你现有 ASR 的关系

YouTube：

```text
有字幕
↓
YouTube Transcript MCP
```

没有字幕：

```text
YouTube
↓
获取媒体
↓
你自己的 Whisper / faster-whisper
↓
Transcript
```

因此不要把 YouTube Transcript MCP 当成你的 ASR 替代品。

---

# 10. 推荐的最终 CrossLLM MCP 配置目录

建议项目外统一管理：

```text
C:\CrossLLM-Tools\
│
├── openwebsearch\
├── stackoverflow\
├── huggingface\
├── reddit\
├── academic-search\
├── everything-search\
├── filesystem\
├── pdf2md\
├── excel\
├── word\
├── youtube\
└── playwright\
```

各 Server 独立。

CrossLLM 本身只保存：

```text
MCP Server 名称
启动命令
参数
环境变量
启用状态
```

---

# 11. API Key 统一原则

不要写进 Git。

例如：

```text
.env
```

统一管理：

```text
GITHUB_PERSONAL_ACCESS_TOKEN=
STACKOVERFLOW_API_KEY=
CONTEXT7_API_KEY=
HF_TOKEN=
CROSSREF_API_KEY=
MINERU_API_KEY=
YOUTUBE_API_KEY=
```

Filesystem / Everything / OpenWebSearch 一类无需 Key 的服务不需要填写。

---

# 12. 第一版 CrossLLM 接入顺序

不要求一次启动全部 MCP。

推荐按照：

```text
Step 1
OpenWebSearch

Step 2
Fetch

Step 3
Filesystem
Everything

Step 4
GitHub
Stack Overflow
Context7

Step 5
Hugging Face
Reddit

Step 6
知乎
腾讯开发者社区（若确认站内 MCP）

Step 7
Academic Search

Step 8
YouTube

Step 9
PDF2MD
Excel
Word

Step 10
Playwright
```

每完成一个就实际调用一次。

---

# 13. 最终信息获取结构

```text
                          CrossLLM
                              │
        ┌─────────────────────┼────────────────────┐
        ↓                     ↓                    ↓
    Web Search            专业信息源            本地资料
        │                     │                    │
  OpenWebSearch        GitHub / SO / HF       Everything
        │              Context7 / Reddit        │
      Fetch            Academic / Zhihu      Filesystem
        │                                      PDF/Excel/Word
    Playwright
        │
        └────────────────────┬────────────────────┘
                             ↓
                          LLM
                             ↓
                       Final Answer
                             ↓
                    CrossLLM 保存 / 互评
```

工具只是帮助模型获取输入。

**最终仍然只有 Final Answer 进入 CrossLLM 的核心互评机制。**

---

# 14. 当前未解决/不强行部署

以下不在本文件中伪造配置：

```text
腾讯开发者社区“站内搜索 MCP”
```

当前只能确认腾讯官方 MCP 生态和官方联网搜索 MCP，不能确认一个独立官方的“开发者社区搜索 MCP”。

因此暂时：

```text
腾讯开发者社区
→ OpenWebSearch 搜索
```

以后发现稳定的官方站内 MCP，再单独增加。

---

# 15. 最重要的部署原则

不要把 MCP 全部一次性挂到每一个模型。

CrossLLM 应该允许：

```text
模型 A
→ GitHub + Context7

模型 B
→ Web Search + Reddit

模型 C
→ Hugging Face + Academic

模型 D
→ Filesystem + Everything
```

只有真正需要的模型才获得对应工具。

这样可以减少：

* Tool definitions；
* Context 占用；
* 工具选择干扰；
* 不必要的 API 调用。

工具是能力，不是模型回答本身。

最终评价仍然以：

> **LLM 获得信息后的 Final Answer**

为准。
