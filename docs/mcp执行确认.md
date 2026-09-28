可以，建议这份不要再写成长文，而是作为**给 Agent 的执行确认单**，把已经确定的东西锁死，避免它继续发散。

# CrossLLM Phase 3：Agent 理解修正与执行确认

## 1. 总体判断

你的总体理解基本正确，但需要做以下修正后再开始开发。

CrossLLM Phase 3 的目标仍然非常简单：

> **给真实 LLM 增加 MCP 工具能力，让模型可以获取外部信息或读取资料；CrossLLM 核心仍然只负责模型调用、Session、Round、Persistence、可选匿名互评。**

不要把 Phase 3 扩展成通用 Agent Framework。

---

# 2. 本阶段核心链路

最终链路：

```text
用户问题
    ↓
选择一个或多个模型
    ↓
CrossLLM Provider
    ↓
LLM
    ↓
可选 Tool / MCP
    ↓
Tool Result
    ↓
LLM 继续生成
    ↓
Final Answer
    ↓
CrossLLM 保存
    ↓
可选 Peer Review
```

必须明确：

### Tool / MCP 的当前作用

```text
帮助模型获取信息
```

### Peer Review 的作用

```text
比较模型最终回答
```

因此：

> **Tool Call 和 Tool Result 属于当前模型调用过程，但不作为跨模型 Peer Context。**

例如：

```text
Qwen
→ Search
→ Search Result
→ Final Answer
```

其他模型在互评时只看到：

```text
Qwen Final Answer
```

不会看到：

```text
Qwen 搜了什么
Qwen 调用了几次工具
Qwen 的 Tool Result
Qwen 的 reasoning
```

---

# 3. 第一版只接 Open-WebSearch

本阶段第一步：

```text
Open-WebSearch
```

不要同时部署：

```text
Fetch
Playwright
GitHub
Stack Overflow
Context7
...
```

先验证一个 MCP Server 的完整闭环：

```text
LLM
↓
tool call
↓
Open-WebSearch
↓
search result
↓
LLM
↓
Final Answer
```

成功后再按实际需求增加其他 MCP。

---

# 4. Open-WebSearch 的正确定位

项目：

```text
Open-WebSearch
```

腾讯 MCP 广场：

```text
https://cloud.tencent.com/developer/mcp/server/11739
```

GitHub：

```text
https://github.com/aas-ee/open-websearch
```

它不是单纯的一个搜索引擎。

它是：

```text
Multi-engine Web Search
+
部分内容抓取
```

支持选择不同搜索来源。

示例：

```json
{
  "query": "faster-whisper VAD",
  "limit": 3,
  "engines": ["bing", "juejin"]
}
```

因此 CrossLLM 不需要为每个搜索引擎分别实现 MCP。

---

# 5. 不要把 Open-WebSearch 和 Search Engine 混为一谈

CrossLLM 看到的是：

```text
Open-WebSearch
```

而不是：

```text
Bing Tool
Google Tool
Yandex Tool
```

Open-WebSearch 内部可以根据参数选择搜索引擎。

因此当前架构：

```text
CrossLLM
    ↓
Open-WebSearch MCP
    ↓
Bing / Baidu / DDG / Brave / Exa / Juejin / CSDN ...
```

不需要把这些底层引擎全部暴露成独立 Tool。

---

# 6. Open-WebSearch 与独立 Fetch 的关系

不要先假设必须同时安装：

```text
Open-WebSearch
+
Fetch
```

当前 Open-WebSearch 本身已经提供部分网页内容获取能力。

因此第一版：

```text
先只部署 Open-WebSearch
```

实际启动后检查：

```text
tools/list
```

确认它当前版本实际提供哪些工具。

如果 Open-WebSearch 自带的抓取能力已经满足普通 URL 阅读需求：

> **不再额外部署 Fetch。**

只有实际发现能力不足时才增加独立 Fetch。

---

# 7. Open-WebSearch 与 Playwright 的关系

Playwright 也不要第一版加入。

两者边界：

```text
Open-WebSearch
→ 搜索 / 普通内容获取

Playwright
→ JavaScript 动态网页
→ 真实浏览器访问
→ 需要交互的网页
```

所以：

```text
普通网页
→ Open-WebSearch 优先

Open-WebSearch 无法正常获得内容
→ 后续再考虑 Playwright
```

---

# 8. MCP 配置方式

Phase 3 需要新增一个最小 MCP 配置，例如：

```text
config/mcp.yaml
```

第一版只配置：

```text
open-websearch
```

配置至少表达：

```yaml
name: open-websearch
enabled: true
transport: ...
command: ...
args: ...
env: ...
```

具体 transport / command 以 Open-WebSearch 当前官方部署方式为准。

不要预先设计复杂的 MCP 配置体系。

---

# 9. MCP Client

如果当前实现需要 Python MCP SDK：

```text
mcp
```

可以加入依赖。

但只实现最小能力：

```text
connect
tools/list
call_tool
disconnect
```

不要增加：

```text
Tool Router
Tool Ranking
Tool Planner
Tool Cache
Tool Supervisor
Agent Manager
```

这些全部不属于 Phase 3。

---

# 10. Tool 选择粒度

不要只设计：

```yaml
mcp_enabled: true
```

更合理的是：

```yaml
tools:
  - openwebsearch
```

含义：

> **这个模型本次/当前配置允许使用哪些工具。**

第一版甚至可以只支持：

```yaml
tools:
  - openwebsearch
```

以后增加 GitHub 时再：

```yaml
tools:
  - openwebsearch
  - github
```

不要为了未来需求提前设计几十种权限体系。

---

# 11. Model 与 Tool 的关系

模型可以有不同 Tool 配置。

例如：

```yaml
models:
  - id: qwen
    tools:
      - openwebsearch

  - id: gpt
    tools:
      - openwebsearch

  - id: local_model
    tools: []
```

这样可以实现：

```text
Qwen → 可以搜索
GPT → 可以搜索
Local Model → 不使用 MCP
```

但第一版 UI 不需要做复杂的工具管理面板。

---

# 12. RealProvider 的职责

RealProvider 内部负责：

```text
LLM Request
↓
发现 tool_calls
↓
调用 MCP tool
↓
把 tool result 放回当前模型上下文
↓
再次请求 LLM
↓
直到 Final Answer
```

伪代码：

```python
async def generate(prompt):

    messages = build_messages(prompt)

    while True:

        response = await llm(messages)

        if not response.tool_calls:
            yield response.final_text
            break

        for tool_call in response.tool_calls:
            result = await mcp.call_tool(tool_call)

            messages.append(tool_call)
            messages.append(result)
```

这只是逻辑示意。

第一版不要求复杂 Agent loop。

---

# 13. RoundManager 原则

优先保持：

```text
core/round_manager.py
```

不改核心编排逻辑。

它仍然只负责：

```text
Round
Participant
Provider
Persistence
SSE
```

Tool calling 尽量隐藏在 Provider / MCP Client 层。

因此：

```text
RoundManager
    ↓
Provider.generate()
       ↓
       LLM
       ↓
       MCP
```

而不是：

```text
RoundManager
    ↓
MCP Planner
    ↓
Tool Router
    ↓
LLM
```

后者不要做。

---

# 14. MCP Tool Result 的上下文边界

工具结果必须进入：

```text
当前模型本次调用上下文
```

否则模型无法使用搜索结果。

例如：

```text
User
↓
LLM
↓
Search
↓
Search Result
↓
LLM
↓
Final Answer
```

但是结束以后：

```text
Peer Review
```

只传：

```text
Final Answer
```

这是两个不同的上下文。

---

# 15. Persistence

第一版不要求保存完整 MCP Trace。

至少保存：

```text
Final Answer
```

以及已有：

```text
Round
status
usage
prompt
```

如果 MCP 调用出现错误：

```text
tool error
```

可以保留日志用于调试。

但不要为了保存 Tool Trace 新增复杂数据库。

---

# 16. MCP 错误隔离

必须保持 Phase 1 / Phase 2 已有的：

> 单模型失败不影响其他模型。

例如：

```text
Qwen
→ Search 成功
→ complete

GPT
→ Search 失败
→ error

GLM
→ 不使用 Search
→ complete
```

不能因为一个 MCP Server 或一个模型的 Tool Call 出错而让整轮失败。

---

# 17. Phase 3 第一版测试

测试重点：

### T1

模型正常回答，不调用 Tool。

### T2

模型主动产生 Tool Call。

### T3

CrossLLM 正确调用 Open-WebSearch。

### T4

Search Result 返回给模型。

### T5

模型使用 Search Result 继续生成 Final Answer。

### T6

Final Answer 正常保存。

### T7

多模型并发时，某模型 Tool Call 不影响其他模型。

### T8

Peer Review ON 时，只传递 Final Answer。

### T9

Peer Review OFF 时，不传递其他模型 Final Answer。

### T10

Tool 失败只影响当前模型。

---

# 18. 第一版自动化测试不要调用真实搜索

测试可以使用：

```text
Mock MCP Server
```

或 mock transport。

例如模拟：

```text
LLM
→ search("test")
→ MCP 返回固定结果
→ LLM 返回 Final Answer
```

这样：

* 不依赖网络；
* 不消耗额度；
* 测试稳定；
* 可以重复执行。

真实 Open-WebSearch 只用于人工验收。

---

# 19. 人工验收

完成自动化测试后，用真实 Open-WebSearch 做最小测试：

```text
问题：
“faster-whisper 当前有什么常见 VAD 问题？”
```

确认：

```text
LLM
↓
Open-WebSearch
↓
搜索结果
↓
Final Answer
```

然后再验证：

```text
两个真实模型同时使用工具
↓
最终回答
↓
互评
```

---

# 20. 当前明确不做

Phase 3 第一版禁止主动增加：

```text
Agent Planner
Agent Supervisor
Tool Router
Tool Ranking
Tool Benchmark
Search Quality Score
MCP Quality Score
Reasoning Evaluation
自动工具选择系统
多 Agent 层级
复杂 Tool Trace
数据库
RAG
Memory
```

尤其不要因为 MCP 接入而把项目改造成：

```text
通用 Agent Framework
```

---

# 21. 关于后续 MCP

第一版 Open-WebSearch 成功后，再按实际需求逐个加入：

```text
Fetch
Filesystem
Everything
GitHub
Stack Overflow
Context7
Hugging Face
Reddit
Academic Search
YouTube
PDF / Excel / Word
...
```

每增加一个 MCP：

```text
部署
↓
单独测试
↓
确认实际有用
↓
再保留
```

不要一次性全部挂上。

---

# 22. 最终边界

CrossLLM 核心：

```text
Session
Round
Model Selection
ACTIVE / CLOSED
Peer Review
Persistence
Memory（未来）
```

MCP：

```text
模型获取外部信息的能力
```

Provider：

```text
连接 LLM + 处理 Tool Calling
```

因此最终：

```text
                    CrossLLM
                        │
                   RoundManager
                        │
                 RealProvider
                   /        \
                 LLM        MCP Client
                            │
                      Open-WebSearch
                            │
                  Bing / DDG / Brave ...
```

**MCP 是模型的外部信息获取能力，不是 CrossLLM 的核心业务逻辑。**

---

# 23. 本阶段执行决定

现在可以直接开始开发，采用以下结论：

```text
✅ Phase 3 第一版只接 Open-WebSearch
✅ 使用 tools 配置，不设计复杂 mcp_enabled 体系
✅ MCP Client 只实现最小连接 / 列工具 / 调工具
✅ Tool Calling 放在 RealProvider
✅ RoundManager 尽量零改动
✅ Tool Result 只属于当前模型调用上下文
✅ Peer Review 只交换 Final Answer
✅ 单模型 Tool 失败隔离
✅ 自动化测试使用 Mock MCP
✅ 最后用真实 Open-WebSearch 人工验收
✅ 实际需要时再增加其他 MCP
```

**现在不需要继续扩展设计，可以进入实现。**
