可以。现在这个状态说明 **GitHub MCP 已经真实调用成功**，而且 Agent 至少发现了一个很重要的问题：它使用的是一个已经 deprecated 的社区版 `@modelcontextprotocol/server-github`。所以接下来给 Agent 的 prompt，我建议重点是**批量部署其余 MCP + 自动逐个验收 + 严格区分“Server 可用”和“LLM 实际调用成功”**，同时不要继续重构核心架构。

下面这份可以直接发给 Agent。

# CrossLLM Phase 3：批量 MCP 部署与自动验收 Prompt

## 你的任务

继续完成 CrossLLM Phase 3 的 MCP 接入。

当前状态：

* Phase 1 已完成
* Phase 2 Real LLM 已完成
* Open-WebSearch MCP 已完成真实 E2E 验证
* GitHub MCP 已完成真实调用验证
* CrossLLM 当前 88/88 测试通过
* MCP 核心连接、stdio、tool calling、tool result、Final Answer 链路已经成立

现在不要重新设计 CrossLLM。

目标是：

> **一次性接入剩余确定需要的 MCP，并建立一个轻量的批量健康检查机制，让我能够明确知道每个 MCP 是“启动失败 / tools/list 失败 / Tool 调用失败 / 调用成功”。**

---

# 一、已确定的 MCP

继续使用以下工具池：

```text
Web
├── Open-WebSearch
├── Fetch
└── Playwright

技术开发
├── GitHub
├── Stack Overflow
└── Context7

AI / 模型
└── Hugging Face

社区
├── Reddit
├── 知乎
└── MCP 中文趋势聚合

学术
└── Academic Paper Search

文件与文档
├── Everything Search
├── Filesystem
├── PDF2MD
├── Excel
└── Word

视频平台
├── YouTube Search
└── YouTube Transcript
```

知乎已经单独配置完成，不要重复部署或替换。

腾讯开发者社区、稀土掘金没有确认存在独立官方站内 MCP：

```text
腾讯开发者社区 → Web Search + Fetch
稀土掘金 → Web Search + Fetch
```

不要为了“完整性”自行寻找第三方逆向 MCP。

---

# 二、最重要的开发原则

## 1. 不重新设计 MCP 架构

继续使用现有：

```text
core/mcp_registry.py
```

不要新增：

```text
Tool Router
Tool Planner
Agent Supervisor
Tool Ranking
MCP Manager Framework
MCP Orchestrator
```

MCP Registry 只负责：

```text
connect
tools/list
call_tool
disconnect
```

必要时增加：

```text
health_check
```

即可。

---

# 三、批量部署方式

可以一次性把剩余 MCP 加入：

```text
config/mcp.yaml
```

但不要一次让所有模型全部使用所有 MCP。

配置只表达：

```yaml
enabled: true
transport: stdio / sse / http
command: ...
args: ...
env: ...
```

如 MCP 是远程服务：

```text
url
headers
```

密钥必须从环境变量读取：

```text
.env
```

禁止把真实 API Key 写进：

```text
mcp.yaml
models.yaml
session
state.json
prompt
日志
```

---

# 四、第三方 MCP 必须先核对当前实际部署方式

此前 GitHub MCP 已经暴露出：

```text
社区版 npm 包存在 deprecated 问题
```

因此接下来每一个 MCP 在部署前必须先确认：

1. 官方仓库/官方文档
2. 当前实际版本
3. 当前安装方式
4. transport
5. 是否需要 API Key
6. API Key 从哪里获得
7. 是否存在 deprecated
8. 是否为官方项目还是社区项目
9. 许可证
10. Windows 是否支持

禁止根据记忆猜测安装命令。

---

# 五、不要因为“官方”而强行增加环境复杂度

优先级：

```text
官方且易部署
>
成熟社区实现且稳定
>
复杂源码编译
>
为了一个 MCP 引入 Go / Docker / 编译环境
```

如果已经存在一个实际可用、简单、只读、稳定的实现，不要为了“官方”而重构整个 CrossLLM。

但是如果第三方版本：

```text deprecated
停止维护
功能异常
认证不安全
```

必须明确标记，不要假装它是长期方案。

---

# 六、批量健康检查：必须增加

新增：

```text
test_mcp_health.py
```

或者等价的轻量工具。

它不是完整业务测试，只做 MCP Server 体检。

每个 MCP 至少执行：

```text
1. 启动/连接
2. tools/list
3. 选一个只读 Tool 做 smoke test
4. 记录结果
5. disconnect
```

输出必须类似：

```text
MCP HEALTH CHECK
────────────────────────────

openwebsearch
  connect       ✅
  tools/list    ✅ 1 tools
  smoke test    ✅
  status        READY

github
  connect       ✅
  tools/list    ✅ 26 tools
  smoke test    ✅
  status        READY

stackoverflow
  connect       ✅
  tools/list    ✅ 3 tools
  smoke test    ✅
  status        READY

context7
  connect       ✅
  tools/list    ✅
  smoke test    ❌ API limit
  status        DEGRADED

youtube
  connect       ✅
  tools/list    ✅
  smoke test    ❌ API key missing
  status        AUTH_REQUIRED

everything
  connect       ❌ Everything SDK not found
  status        FAILED
```

不要只输出：

```text
MCP connected
```

必须能够区分：

```text
connect
tools/list
call_tool
```

三个阶段。

---

# 七、每个 MCP 的 smoke test

为每个 Server 指定最简单、最确定的只读测试。

例如：

```text
Open-WebSearch
→ 搜索 faster-whisper

GitHub
→ 搜索 Aider-AI/aider

Stack Overflow
→ 搜索一个已知 Python 错误

Context7
→ resolve FastAPI / 查询文档

Hugging Face
→ 搜索 whisper 模型

Reddit
→ 查询一个已知 subreddit

Academic Search
→ 搜索 Whisper ASR

Everything
→ 搜索一个电脑中确定存在的文件

Filesystem
→ 读取一个确定存在的 .md

PDF2MD
→ 转换一个测试 PDF

Excel
→ 读取一个测试 xlsx

Word
→ 读取一个测试 docx

YouTube
→ 搜索一个确定存在的视频 / 获取其字幕
```

smoke test 不得执行：

```text
写文件
删除文件
修改 GitHub
创建 Issue
提交 PR
发送消息
发布内容
```

---

# 八、真实 LLM E2E 测试与健康检查分开

必须区分：

## Server 健康

```text
MCP → tools/list → smoke test
```

和：

## LLM 是否会使用

```text
LLM
↓
tool_call
↓
MCP
↓
tool_result
↓
LLM
↓
Final Answer
```

不要因为：

```text
tools/list ✅
```

就宣称：

```text
LLM MCP 已成功
```

也不要因为：

```text
LLM 最终回答成功
```

就宣称：

```text
GitHub MCP 成功
```

必须有明确的：

```text
call_tool(...)
returned ...
```

证据。

---

# 九、真实 LLM 综合测试

批量健康检查全部通过后，只选择一个当前没有严重 429 的模型做综合测试。

不要同时让 5 个免费模型测试全部 MCP。

测试问题：

```text
请调查 faster-whisper 当前常见的 VAD 问题。

要求：
1. 搜索 GitHub Issue；
2. 查找网页资料；
3. 如果存在相关论文，查询论文；
4. 综合资料后给出结论。
```

预期模型可能选择：

```text
GitHub
Open-WebSearch
Academic Search
```

不要求它一定调用所有工具。

必须记录：

```text
tool_call 顺序
tool name
arguments
tool result 大小
最终回答
```

最终确认：

```text
LLM
→ Tool Call
→ MCP
→ Tool Result
→ LLM
→ Final Answer
```

---

# 十、Context Budget 处理

继续使用当前已经修正的原则：

```text
actual_input + actual_output <= context_limit
```

每次 tool_result 加入 messages 后重新计算。

不要重新引入：

```text
context_limit * 0.95
context_limit - max_output_tokens
DEFAULT_MAX_OUTPUT_TOKENS = 某个拍脑袋值
```

当前：

```text
context_limit
```

是硬总上限。

```text
model_max_output
```

是模型**能**输出的上限，只来自 Provider 元数据
（OpenRouter `top_provider.max_completion_tokens`）；取不到 = unknown。
`max_output_tokens` 只是 Provider Protocol / API 的**参数名**，
不是 models.yaml 的抽象能力字段（已删除，§二）。

最终：

```python
# reserved_output_tokens = min(model_max_output, request_max_output)（预算层给出）
actual_output = min(
    reserved_output_tokens,
    context_limit - actual_input
)
```

输入 token 目前仍允许使用估算函数。

但必须明确：

> token 估算不是精确 tokenizer。

OpenRouter 返回的 usage 才是实际 token 统计。

---

# 十一、429 / 400 必须分开

测试输出必须明确分类。

```text
HTTP 429
→ Provider / OpenRouter 限流

HTTP 400 context length
→ Context Budget / 请求非法

MCP connection error
→ MCP Server / transport

WinError 2
→ 本地启动命令 / PATH

auth error
→ API Key / OAuth

tool error
→ MCP Tool 自身
```

禁止把所有错误都归因于 MCP。

---

# 十二、stdio Server 生命周期

不要让同一个 MCP Server 因为每个模型调用而无限重复启动。

目标：

```text
CrossLLM
   ↓
MCP Registry
   ↓
Server Instance
   ↓
多个模型按需使用
```

如果当前实现为了每个模型建立独立 Client，不要立即大规模重构。

先确认：

```text
是否实际产生资源泄漏
是否导致重复进程
是否产生端口冲突
```

只有真实出现问题才优化生命周期。

特别注意：

```text
Open-WebSearch
```

当前以：

```text
MODE=stdio
```

为 CrossLLM 本地连接方式。

不要无意义启动 HTTP 3000。

---

# 十三、Tool 白名单

保持现在的安全思路。

如果一个 MCP 提供几十个 Tool：

```text
github
```

而 CrossLLM 当前只需要：

```text
search_issues
```

可以使用现有的 Tool 白名单，只暴露必要 Tool。

不要默认向所有模型暴露：

```text
write
delete
push
create PR
```

原则：

> **第一版只暴露信息获取和只读能力。**

---

# 十四、许可证与发布风险

每个第三方 MCP 在记录中增加：

```text
name
repository
version
license
maintenance status
auth
transport
```

特别注意：

```text
AGPL
GPL
MIT
Apache-2.0
```

如果未来 CrossLLM 发布到 GitHub：

> 不要因为现在是个人使用，就假设第三方代码许可证永远没有影响。

尤其不要把 AGPL MCP Server 的源码直接复制进 CrossLLM。

优先：

```text
独立运行 MCP Server
+
CrossLLM 通过 MCP 协议调用
```

---

# 十五、文档要求

更新：

```text
phase3_mcp_deployment.md
```

每个 MCP 独立记录：

```text
## MCP 名称

### 官方/仓库

### 用途

### Transport

### 安装

### API Key

### Windows 注意事项

### MCP Tools

### Smoke Test

### CrossLLM 配置

### 当前状态

### 已知问题
```

不要混淆：

```text
MCP Server 名称
Tool 名称
Provider
API
```

例如：

```text
MCP Server:
github

Tool:
search_issues

Backend:
GitHub API
```

---

# 十六、不要修改核心业务

以下文件原则上不要因为接入 MCP 而改动：

```text
core/anonymizer.py
core/storage.py
core/session_manager.py
core/prompt_builder.py
```

`round_manager.py` 也尽量不动。

如果必须修改，必须明确说明：

```text
为什么必须改
改动是什么
是否影响 Phase 1 / Phase 2
```

MCP Tool Result 只属于当前模型调用链。

Peer Review 仍然只交换：

```text
Final Answer
```

不会把：

```text
Tool Call
Tool Result
Search History
Reasoning
```

注入其他模型。

---

# 十七、最终验收标准

Phase 3 MCP 第一阶段满足：

```text
✅ 多个 MCP 可以统一注册
✅ 每个 MCP 可以独立健康检查
✅ 可以准确知道哪个 MCP 失败
✅ connect / tools/list / call_tool 状态分开
✅ API Key 不泄漏
✅ Tool Result 可以正常进入当前模型上下文
✅ Final Answer 正常产生
✅ Peer Review 仍只看到 Final Answer
✅ MCP 失败不影响其他模型
✅ 不发生 Context Overflow
```

不要把：

```text
“所有 MCP 都安装好了”
```

当成成功标准。

真正成功标准是：

```text
需要什么工具
→ 能找到
→ 能调用
→ 能返回结果
→ LLM 能利用结果
→ Final Answer 正常
```

---

# 十八、执行顺序

现在直接按以下顺序执行：

```text
Step 1
确认现有 Open-WebSearch / GitHub 不回归

Step 2
一次性补充 mcp.yaml 中其余 MCP

Step 3
实现/完善 MCP health check

Step 4
逐个自动执行 connect + tools/list + smoke test

Step 5
输出 MCP 状态报告

Step 6
解决失败项

Step 7
只选择一个稳定模型做综合真实 E2E

Step 8
确认 tool_call → tool_result → Final Answer

Step 9
运行全量测试

Step 10
停止
```

不要因为一个 MCP 失败就重新设计整个 MCP Registry。

不要在本阶段加入：

```text
MCP Router
Tool Planner
Agent Supervisor
Tool Ranking
Memory
RAG
自动 Search Quality Score
```

这些不属于当前任务。

---

# 十九、完成后的汇报格式

完成后只报告：

```text
MCP 总数：N

READY：
- xxx
- xxx

DEGRADED：
- xxx（原因）

FAILED：
- xxx（原因）

AUTH_REQUIRED：
- xxx

LLM E2E：
- xxx → ✅
- 使用工具：xxx
- Tool Result：xxx chars
- Final Answer：✅

Tests：
N/N passed

遗留问题：
...
```

不要只说：

> “全部完成。”

必须给出每个 MCP 的实际状态和失败原因。
