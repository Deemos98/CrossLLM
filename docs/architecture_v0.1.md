可以。下面这个版本我会把**架构边界、Round 语义、GitHub 项目只是参考还是实际依赖、API 契约、Phase 1 开发范围、禁止事项**都写死，方便后续直接交给 Claude Code / Cline / Cursor 做 AI Coding。

````markdown
# Multi-LLM Peer Review Hub (MAD-Hub)
## Architecture & Development Specification
### Version: v0.1

---

# 0. 文档定位

本文档是 MAD-Hub v0.1 的基础架构与开发规范。

本文档的目标不是实现一个通用 Multi-Agent Framework，而是实现一个：

> **多厂商 LLM 平级参与、并行回答、跨模型 Peer Review、人工模型池筛选、可持续多轮讨论，并支持 MCP / Web Search 外部求证的个人 AI 工作台。**

本项目优先服务以下场景：

- Coding 架构设计
- 算法设计与方案比较
- 技术路线选择
- 技术资料研究
- 日常知识查询
- 对存在明显分歧的问题进行交叉验证

---

# 1. 核心产品定义

MAD-Hub 不把 Qwen、DeepSeek、GPT、Gemini、GLM 等模型视为不同“角色”。

它们是：

> **平级 Model Participant（模型参与者）**

例如：

```text
Qwen
DeepSeek
GPT
Gemini
GLM
Doubao
````

这些模型之间没有：

* Leader
* Manager
* Judge
* Final Judge
* Final Arbiter

除非用户未来明确要求加入。

---

# 1.1 核心工作模式

系统采用：

```text
独立发散
    ↓
模型互相评价
    ↓
人工筛选模型
    ↓
继续讨论
    ↓
发现争议
    ↓
外部检索 / MCP 求证
    ↓
继续讨论
    ↓
人工再次筛选
    ↓
……
```

核心特点：

> **人类用户负责管理模型池，而不是让某一个模型代替人类进行最终裁决。**

---

# 1.2 不可违反的架构原则

以下原则属于项目硬约束。

## P1. 模型平级

Qwen、DeepSeek、GPT、Gemini、GLM 等模型均为平级参与者。

不得默认任何一个模型是“最可靠模型”。

不得因为模型厂商、benchmark 排名或模型名称自动提高其权重。

---

## P2. Round 1 必须独立

Round 1 中：

```text
Qwen
DeepSeek
GPT
Gemini
GLM
```

分别只接收：

```text
用户原始问题
+
系统公共指令
```

不得读取其他模型的 Round 1 回答。

目的：

> 获得真正独立的初始观点。

---

## P3. Round 2+ 允许交叉阅读

从 Round 2 开始：

每个 ACTIVE 模型可以看到：

```text
原始用户问题
自己的上一轮回答
其他 ACTIVE 模型上一轮回答
用户当前追加指令
可选 Evidence Pack
```

模型可以：

* 评价其他模型
* 指出错误
* 发现遗漏
* 吸收其他模型的合理观点
* 修改自己的方案
* 提出新的方案

这是系统的核心功能。

---

## P4. 不使用多数投票直接判断正确性

不得采用：

```text
3 个模型支持
2 个模型反对

=> 3:2，所以正确
```

作为事实正确性的直接依据。

原因：

* 多个模型可能拥有相同错误知识来源
* 模型之间可能存在训练数据重叠
* 模型可能互相影响后形成错误共识
* 少数模型可能提出正确但小众的方案

---

## P5. 少数观点不得因为票数少自动删除

例如：

```text
Qwen：
提出 PHMB 方案

其他四个模型：
没有提到 PHMB
```

不得自动认为 PHMB 错误。

应视为：

```text
独特候选观点
```

必要时进入：

```text
DISPUTE
或
NEEDS_EVIDENCE
```

状态。

---

## P6. CLOSED 不等于错误

用户关闭模型只代表：

> 当前不再让该模型消耗后续调用资源。

不代表：

> 该模型已经被证明错误。

因此：

```text
ACTIVE
CLOSED
```

只是参与状态。

历史回答必须永久保存。

---

## P7. 不自动设置 Final Judge

系统默认不得创建：

```text
Judge Agent
Final Judge
Master Agent
Arbiter Agent
Consensus Agent
```

也不得自动将某个模型指定为最终正确答案来源。

---

## P8. 不自动强制 Consensus

系统不得为了得到“唯一答案”而强行消除分歧。

允许最终状态为：

```text
方案 A 更可能正确
方案 B 也有一定依据
某个关键事实仍无法确认
```

---

## P9. Round 数量没有固定上限

正常流程为：

```text
Round 1
Round 2
Round 3
...
Round N
```

N 由用户决定。

系统不得强制 Round 3 为最终轮。
)

上层 Round Manager 不应直接处理各厂商不同的 API 格式。

如果某个 Provider 无法通过 LiteLLM 满足特定功能，再单独实现 Provider Adapter。

10.5 modelcontextprotocol/python-sdk

这是：

实际依赖候选。

用途：

MCP Client
MCP Server connection
Tool discovery
Tool invocation
MCP 返回结果处理

首先用于连接：

GitHub MCP
Zhihu MCP

后续允许增加其他 MCP Server。

MCP Client 与模型调用层保持独立：

Model Manager
    │
    └── LiteLLM

Tool Manager
    │
    └── MCP Client

不得因为某一个模型支持 MCP Tool Calling，就把 MCP Client 与具体模型 Provider 强耦合。

10.6 GitHub / Zhihu MCP Server

GitHub 与 Zhihu MCP Server 不固定为 MAD-Hub 自己实现的 Server。

MAD-Hub 作为 MCP Client，负责：

连接
发现 Tool
调用 Tool
获取结果
保存结果

具体使用哪个 MCP Server，由配置决定。

推荐配置结构：

{
  "mcp_servers": {
    "github": {
      "enabled": true,
      "transport": "stdio",
      "command": "..."
    },
    "zhihu": {
      "enabled": false,
      "transport": "stdio",
      "command": "..."
    }
  }
}

必须支持 MCP Server 独立启用/关闭。

11. 严禁的自动化逻辑

AI Coding Agent 不得自行加入以下逻辑：

Judge Agent
Final Judge
Master Agent
Leader Agent
Consensus Agent
Automatic Winner
多数投票直接决定正确性
自动删除少数观点
自动根据模型数量决定方案正确性
固定 Round 3 为 Final Answer
强制所有模型最终达成一致
自动选择某个厂商模型作为最高优先级

也不得自行添加以下模型层面的长期权重：

模型权重
模型信誉分
模型排行榜
自动模型优先级
根据历史结果自动淘汰模型

除非用户未来明确要求。

12. Model Participant 的数据结构

模型必须作为平级 Model Participant 管理。

建议：

class ModelParticipant:
    id: str
    display_name: str
    provider: str
    model: str
    enabled: bool

示例：

{
  "id": "qwen",
  "display_name": "Qwen",
  "provider": "dashscope",
  "model": "qwen-max",
  "enabled": true
}

另一个示例：

{
  "id": "gpt",
  "display_name": "GPT",
  "provider": "openai",
  "model": "gpt-5",
  "enabled": true
}

模型配置与 Session 状态必须分离。

模型配置负责：

模型是谁
Provider 是谁
API 配置
默认模型参数

Session 状态负责：

当前是否 ACTIVE
是否 CLOSED
参与哪些 Round

一个模型可以在一个 Session 中 CLOSED，同时在另一个 Session 中 ACTIVE。

13. Session State

建议：

class SessionState:
    session_id: str
    original_question: str
    current_round: int

    active_models: list[str]
    closed_models: list[str]

    disputes: list[str]

    evidence_ids: list[str]

    tools: dict

示例：

{
  "session_id": "2026-08-30-001",
  "original_question": "这个 OCR 架构是否合理？",
  "current_round": 2,
  "active_models": [
    "qwen",
    "deepseek",
    "gpt"
  ],
  "closed_models": [
    "gemini",
    "glm"
  ],
  "disputes": [
    "BGE-M3 是否真的有必要"
  ],
  "evidence_ids": [],
  "tools": {
    "mcp": {
      "enabled": true,
      "github": true,
      "zhihu": false
    },
    "web_search": {
      "enabled": true,
      "google": true,
      "bing": false,
      "yandex": true
    }
  }
}
14. Round State

建议：

class RoundState:
    round_id: int
    user_instruction: str

    participants: list[str]

    responses: dict[str, str]

    tools_enabled: dict

    evidence_ids: list[str]

    timestamp: str

例如：

{
  "round_id": 2,
  "user_instruction": "评价其他方案，并重点分析复杂度",
  "participants": [
    "qwen",
    "deepseek",
    "gpt"
  ],
  "responses": {
    "qwen": "...",
    "deepseek": "...",
    "gpt": "..."
  },
  "tools_enabled": {
    "mcp": {
      "github": true,
      "zhihu": false
    },
    "web_search": {
      "google": true,
      "bing": false,
      "yandex": true
    }
  },
  "evidence_ids": [],
  "timestamp": "2026-08-30T10:00:00"
}
15. 第一阶段开发范围
Phase 1：只验证核心闭环

必须实现：

Mock 3 Models
    ↓
并发调用
    ↓
SSE Streaming
    ↓
多列 UI
    ↓
保存 Round 1
    ↓
ACTIVE / CLOSED
    ↓
用户关闭一个模型
    ↓
输入第二轮指令
    ↓
点击 Next Round
    ↓
读取 Round 1 文件
    ↓
为每个 ACTIVE 模型构建独立 Prompt
    ↓
注入其他 ACTIVE 模型匿名化 Round 1 答案
    ↓
Mock Round 2
    ↓
保存 Round 2
    ↓
刷新页面后仍可恢复 Session

Phase 1 不实现：

真实 LLM API
LiteLLM
MCP
GitHub
Zhihu
Google
Bing
Yandex
复杂 Web Search
Evidence Pack
自动争议检测
LangGraph
AutoGen
CrewAI
自动 Judge
自动 Consensus
16. Phase 1 目标

测试以下完整流程：

用户：
测试问题

        ↓

Mock Qwen
Mock DeepSeek
Mock GPT

        ↓

Round 1

        ↓

用户关闭一个模型

        ↓

用户追加 Round 2 指令

        ↓

Round 2

        ↓

剩余模型读取：

自己的 Round 1
+
其他 ACTIVE 模型 Round 1
+
用户当前指令

        ↓

生成 Round 2

        ↓

保存 Round 2

        ↓

刷新浏览器

        ↓

恢复 Session

必须验证：

ACTIVE 模型继续参与。

CLOSED 模型不产生新调用。

CLOSED 模型历史答案仍然存在。

Round 1 不被 Round 2 覆盖。

Round 2 可以读取 Round 1。


如果这个流程成功：

Phase 1 核心架构成立。

17. Phase 2

接入：

LiteLLM

替换：

Mock Model

为真实模型：

Qwen
DeepSeek
GPT
Gemini
GLM
Doubao

保持上层调用接口不变：

await model_manager.call(
    model_id,
    messages,
    stream=True
)

推荐内部结构：

Model Manager
      ↓
LiteLLM Client
      ↓
Provider
      ↓
Specific Model

上层：

Round Manager
Prompt Builder
Session Manager

不得直接依赖某个厂商的特殊 API。

如果某个模型需要特殊参数或特殊 API 能力：

Model Manager
    ↓
Provider Adapter

处理，而不是修改 Round Manager。

18. Phase 3

接入：

MCP Client

首先支持：

GitHub
Zhihu

必须提供工具权限开关。

MCP 总开关
MCP
ON / OFF

关闭后：

所有 MCP Tool Call = DENIED

即使模型主动要求调用，也不能绕过后端权限。

MCP 子工具开关
GitHub
ON / OFF

Zhihu
ON / OFF

只有启用的 MCP Server / Tool 才能使用。

Web Search

再增加统一 Search Gateway：

Google
Bing
Yandex

搜索引擎必须独立启用：

Google = ON
Bing = OFF
Yandex = ON

Search Gateway 对上层统一提供：

search(
    query: str,
    engine: str
)

统一返回：

{
  "title": "...",
  "url": "...",
  "snippet": "...",
  "source": "google"
}

MCP 与 Search Gateway 必须保持独立。

Evidence 权限

工具可以启用，但不代表结果必须自动注入下一轮。

推荐流程：

用户打开工具
        ↓
执行 Search / MCP
        ↓
保存 Evidence
        ↓
生成 Evidence Pack
        ↓
用户决定是否注入下一轮

默认不把大量搜索结果自动追加到所有模型上下文。

19. Phase 4

增加以下功能：

Evidence Pack
Dispute tracking
历史 Evidence
更细粒度的模型选择
Round 对比
工具调用历史
来源引用
模型回答版本
Token / Cost / Latency 统计
19.1 Evidence Pack

Evidence Pack 必须保留：

来源标题
URL
来源类型
来源工具
抓取时间
原始摘要或相关正文
具体位置

例如：

evidence/
└── evidence_001/
    ├── manifest.json
    ├── google_001.md
    ├── github_001.md
    └── zhihu_001.md
19.2 来源引用

模型使用外部资料形成结论时，回答应尽可能提供可追溯引用。

Web Search：

标题
URL
来源
具体页面位置（如果可以定位）

GitHub：

Repository
URL
File
Line / Section
Commit / Version（如果可以获得）

优先保存可直接定位到具体文件行的 GitHub URL。

Zhihu：

问题 / 文章标题
回答作者（如果可获得）
URL
内容位置

必须区分：

官方资料
研究资料
GitHub 源码
社区经验
模型自身知识

不得把知乎个人经验直接表述为官方事实。

19.3 Evidence 持久化

每一次工具调用都应该保存：

tool
provider
query
parameters
timestamp
result
sources

建议：

tool_calls/
└── round_002/
    ├── github_001.json
    ├── google_001.json
    └── zhihu_001.json

这样可以追溯：

某个结论来自哪一次工具调用、哪一个来源。

19.4 Round 对比

允许前端比较：

Round 1
vs
Round 2
vs
Round 3

例如：

Qwen

Round 1
原始方案

Round 2
吸收 DeepSeek / GPT 观点后的方案

Round 3
根据 Evidence 修正后的方案

不得覆盖历史版本。

19.5 模型状态

模型状态至少包含：

ACTIVE
CLOSED

后续可以扩展：

ERROR
TIMEOUT
DISABLED

但：

CLOSED ≠ WRONG

关闭只是：

用户暂时停止让该模型参与后续 Round。

19.6 Round 不自动结束

系统允许：

Round 1
Round 2
Round 3
Round 4
...
Round N

只要用户继续，就允许继续。

不得默认：

Round 3 = Final

也不得自动生成所谓最终裁决。

最终结束动作由用户执行。

19.7 Phase 4 完成后的完整流程
                         用户问题
                            ↓
                  ┌───────────────────┐
                  │ Round 1           │
                  │ 独立回答           │
                  └─────────┬─────────┘
                            ↓
                       用户筛选模型
                            ↓
                  ┌───────────────────┐
                  │ Round 2           │
                  │ Peer Review       │
                  │ 相互评价/吸收      │
                  └─────────┬─────────┘
                            ↓
                       用户筛选模型
                            ↓
                     发现争议点
                            ↓
                  ┌───────────────────┐
                  │ Evidence Stage    │
                  │ Search / MCP      │
                  └─────────┬─────────┘
                            ↓
                     Evidence Pack
                            ↓
                  ┌───────────────────┐
                  │ Round 3+          │
                  │ 重新评价/修正      │
                  └─────────┬─────────┘
                            ↓
                       用户继续筛选
                            ↓
                         Round N
                            ↓
                       用户结束讨论

# 20. 推荐工程目录

```text
mad_hub/
│
├── app.py
├── requirements.txt
│
├── core/
│   ├── state.py
│   ├── session_manager.py
│   ├── round_manager.py
│   ├── model_manager.py
│   ├── prompt_builder.py
│   └── storage.py
│
├── providers/
│   └── litellm_client.py
│
├── tools/
│   ├── search_gateway.py
│   └── mcp_client.py
│
├── sessions/
│
└── static/
    ├── index.html
    ├── style.css
    └── app.js
```

---

# 21. 模块职责边界

```text
app.py
    HTTP / SSE

session_manager.py
    Session 生命周期

round_manager.py
    Round 编排
    ACTIVE 模型过滤
    下一轮触发

model_manager.py
    模型池
    模型配置

litellm_client.py
    实际 LLM API 调用

prompt_builder.py
    Prompt 组装

storage.py
    SQLite + 文件系统

search_gateway.py
    Google / Bing / Yandex

mcp_client.py
    GitHub / Zhihu MCP

static/
    前端 UI
```

---

# 22. 第一版前端交互

UI 至少包含：

```text
顶部：
Session 标题

中间：
Multi-column Model Cards

每张 Card：
模型名称
状态
Streaming / Completed / Error
Markdown 内容
ACTIVE / CLOSED

底部：
用户追加指令
[下一轮]

可选：
[Evidence Search]
```

---

# 23. 前端布局要求

第一版采用：

```text
CSS Grid / Flex
```

桌面端优先。

模型数量增加时：

```text
5模型
→ 5列或自适应布局

10模型
→ 横向滚动或自动折行
```

回答长度很长时：

```text
卡片内部独立滚动
```

避免某个模型的长答案导致整个页面布局失控。

---

# 24. Streaming

采用：

```text
Server-Sent Events (SSE)
```

优先于 WebSocket。

原因：

第一版通信模式主要是：

```text
Server
 ↓
Streaming model tokens
 ↓
Browser
```

并不需要复杂双向实时通信。

后续如果需求发生变化，再考虑 WebSocket。

---

# 25. Error Handling

单个模型失败不得导致整个 Round 失败。

例如：

```text
Qwen      SUCCESS
DeepSeek  SUCCESS
GPT       TIMEOUT
Gemini    SUCCESS
GLM       ERROR
```

系统应保存：

```text
GPT:
status = timeout

GLM:
status = error
```

其他模型继续完成。

---

# 26. Cost Control

默认策略：

```text
Round 1：
用户主动启用的所有模型

Round 2+：
只调用 ACTIVE 模型
```

关闭模型不得继续产生 API 消耗。

搜索/MCP 默认关闭。

---

# 27. Context Control

不要无限把所有历史 Round 全部注入模型。

默认 Round N 只注入：

```text
Original Question
+
用户当前指令
+
自己的上一轮答案
+
其他 ACTIVE 模型上一轮答案
+
需要使用的 Evidence Pack
```

只有用户明确要求回顾更早历史时，才读取更早 Round。

---

# 28. 数据完整性

任何 API 调用都建议保存：

```text
model
round
prompt
response
status
timestamp
```

如果 API 支持，还可保存：

```text
usage
input_tokens
output_tokens
latency
model_version
```

这些信息主要用于后续统计，不用于自动判断模型正确性。

---

# 29. 未来扩展，但 v0.1 不实现

未来可以增加：

```text
模型历史表现统计
模型成本统计
平均响应时间
不同类型问题的模型表现
人工评价记录
Evidence 引用图
争议点追踪
模型版本追踪
```

但是：

> v0.1 不允许为了未来功能提前增加复杂框架。

---

# 30. AI Coding Agent 开发纪律

AI Coding Agent 必须遵守：

## D1

先理解本文档，再修改代码。

## D2

不要自行引入大型框架。

除非明确需要，不要加入：

```text
LangChain
LangGraph
AutoGen
CrewAI
```

## D3

不要自行改变核心架构。

尤其不得擅自加入：

```text
Judge
Consensus
Voting
Final Agent
```

## D4

不要把所有历史 Round 无限制塞入 Prompt。

## D5

不要覆盖历史回答。

## D6

不要因为一个模型失败而取消整个 Round。

## D7

不要把 CLOSED 模型从历史文件中删除。

## D8

不要把 MCP 与 Search Gateway 强行设计成同一个模块。

## D9

不要为每个厂商重复实现一套 API Client，优先使用 LiteLLM。

## D10

任何新增依赖必须说明：

```text
为什么需要
解决什么问题
是否可以用标准库替代
```

---

# 31. Phase 1 First Task

第一步只实现以下内容：

```text
app.py
core/state.py
core/session_manager.py
core/round_manager.py
core/storage.py
static/index.html
static/style.css
static/app.js
```

实现 Mock API：

```text
Mock Qwen
Mock DeepSeek
Mock GPT
```

模拟：

```text
0.1 秒 ~ 0.3 秒
```

随机 chunk streaming。

前端同时显示：

```text
Qwen
DeepSeek
GPT
```

---

# 32. Phase 1 验收标准

必须能够完成：

```text
1. 输入问题
2. 三个 Mock 模型同时开始回答
3. 三个窗口实时显示
4. 回答完成后自动保存 round_001
5. 用户关闭一个模型
6. 点击“下一轮”
7. 后端读取 round_001
8. 剩余两个模型获取彼此上一轮答案
9. 模拟 Round 2 回答
10. 保存 round_002
11. 刷新浏览器后 Session 状态仍存在
12. 可以读取之前的 Round
```

如果以上全部完成：

> Phase 1 完成。

---

# 33. 最终设计原则

MAD-Hub 不是：

```text
一个超级 Agent
```

也不是：

```text
一个 Judge
```

而是：

```text
用户问题
    ↓
多个平级 LLM 独立回答
    ↓
保存原始答案
    ↓
用户决定哪些继续
    ↓
ACTIVE LLM 互相评价
    ↓
吸收合理方案
    ↓
发现争议
    ↓
MCP / Web Search 求证
    ↓
继续交叉评价
    ↓
用户再次筛选
    ↓
……
```

最终决定权始终属于用户。

项目的核心价值不是：

> “让模型自动得出一个答案。”

而是：

> **让多个不同模型帮助用户快速暴露方案之间的差异、漏洞、遗漏和争议点，并让用户以最低阅读成本逐步收敛到自己认可的方案。**

---

# 34. v0.1 明确不解决的问题

v0.1 不保证：

```text
模型一定正确
搜索一定正确
多数模型一定正确
Evidence Pack 一定正确
某个模型一定比其他模型优秀
```

系统只负责：

```text
组织
并发
记录
交叉阅读
检索
筛选
继续讨论
```

最终事实判断与方案采用由用户决定。

````

这个版本我认为已经可以作为 **AI Coding 的“总规范”** 了。

尤其是我把你真正的核心写死成了四条：

```text
Round 1 = 独立

Round 2+ = 允许互相看、互相质疑、互相吸收

CLOSED = 停止消耗，不代表错误

没有自动 Final Judge / Consensus
````

这样 Claude Code / Cline 后面即使看到 `ensemble`、AutoGen、LangGraph 之类的代码，也不应该擅自把你的系统改造成“投票 → Judge → Final Answer”。

另外我把 **Phase 1 故意压到 Mock 3 模型**，先验证“并行 → 保存 → 淘汰 → 读取 → 第二轮”这条主链。这个主链跑通后，接 LiteLLM、MCP、Google/Bing/Yandex 都只是往外加模块，不会动核心架构。
