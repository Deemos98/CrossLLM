# CrossLLM Phase 3：MCP / Web Search

## 0. Phase 3 一句话目标

**让参与 CrossLLM 的真实 LLM 可以在生成最终回答前获取外部信息，同时不改变 CrossLLM 已有的多模型、Round、匿名互评和历史机制。**

本阶段解决的是：

> **模型如何获得信息。**

不是：

> **CrossLLM 如何评价搜索过程、MCP 调用过程或模型思考过程。**

---

# 1. 项目边界

CrossLLM 是个人研究/头脑风暴工具。

本阶段优先：

* 简单；
* 低依赖；
* 可运行；
* 可验证；
* 实际有用。

只有实际使用遇到问题时才增加复杂度。

---

# 2. Phase 3 与已有架构的关系

现有核心：

```text
用户问题
    ↓
选择模型
    ↓
单模型 / 群发
    ↓
LLM
    ↓
最终回答
    ↓
保存
    ↓
可选匿名互评
```

Phase 3 变成：

```text
用户问题
    ↓
选择模型
    ↓
单模型 / 群发
    ↓
LLM
    │
    ├── Web Search
    ├── MCP
    └── 文件等外部信息能力
    │
    ↓
最终回答
    ↓
保存
    ↓
可选匿名互评
```

核心 Round / Persistence / Anonymous Peer Context 不因工具加入而改变。

---

# 3. 工具的定位

工具是**模型生成最终回答时的信息获取能力**。

CrossLLM 只把模型最终回答作为核心结果。

不把以下内容作为核心比较对象：

* 搜索了多少次；
* 搜索了几个网页；
* 使用了几个 MCP；
* 调用了什么工具；
* 工具调用顺序；
* 模型内部 reasoning；
* 搜索结果数量；
* 单次工具调用耗时。

原则：

> **能搜不是最终能力，能否正确利用外部信息形成高质量最终回答才是能力。**

---

# 4. Web Search

## 4.1 目标

允许模型在需要实时信息时主动获取网络信息。

典型场景：

```text
某个库现在最新版本是什么？
某个 GitHub 项目最近是否修复了某问题？
某 API 当前文档怎么写？
某模型现在支持什么能力？
```

## 4.2 第一版原则

第一版只实现：

```text
模型
↓
Web Search
↓
搜索结果
↓
模型继续生成
↓
Final Answer
```

不实现复杂 Search Agent。

不做：

* 多轮搜索策略优化；
* Search Ranking；
* 搜索质量评分；
* 自动搜索 Benchmark；
* 搜索结果长期知识库；
* 搜索结果跨模型共享。

---

# 5. MCP

## 5.1 目标

允许模型调用实际工具。

例如未来：

```text
MCP
├── 文件
├── 数据库
├── Git
├── 本地脚本
└── 其他用户工具
```

## 5.2 MCP 在 CrossLLM 中的位置

MCP 只属于：

```text
LLM Provider / Model Tooling
```

不属于：

```text
RoundManager
Anonymizer
Peer Review
```

概念上：

```text
CrossLLM
    ↓
Model
    ↓
Tool / MCP
    ↓
Model
    ↓
Final Answer
```

---

# 6. 工具结果是否进入互评

第一版不把工具过程本身作为互评对象。

例如：

```text
Model A
→ 搜索 5 个网页
→ Final A

Model B
→ 使用 MCP
→ Final B

Model C
→ 不使用工具
→ Final C
```

互评只交换：

```text
Final A
Final B
Final C
```

而不是：

```text
Search results
MCP calls
Reasoning trace
Tool logs
```

这样保持 CrossLLM 的核心模型：

> **比较最终回答，而不是比较模型内部工作过程。**

---

# 7. 工具与匿名互评的边界

工具调用发生在模型获得最终回答之前。

因此：

```text
Tool
↓
Model
↓
Final Answer
↓
CrossLLM
↓
匿名互评
```

最终回答进入 Peer Context 时继续遵守已有匿名规则。

工具结果本身不需要转成 Candidate。

---

# 8. 工具失败

单模型工具失败不得直接导致其他模型失败。

例如：

```text
Qwen → Search 成功 → complete

GPT → Search 失败 → error

GLM → 无需 Search → complete
```

仍然保持：

> 单模型失败隔离。

工具错误应该归属于对应模型本次生成过程。

---

# 9. 工具与模型选择

模型仍然由用户单次选择决定：

```text
ACTIVE 模型
    ↓
可选
```

CLOSED 模型：

```text
不可选择
```

工具能力不是新的“模型状态”。

例如：

```text
☑ Qwen
☑ GPT
☑ GLM

互评：ON
```

是否使用 Search / MCP：

> 由模型自身决定，或者由用户明确指定。

第一版不自动给所有模型强制注入同一工具。

---

# 10. 与群发的关系

群发仍然只是：

```text
selected_models >= 2
```

例如：

```text
Qwen + GPT
```

两者可以：

```text
Qwen → 使用 Search
GPT  → 不使用 Search
```

仍然属于一次正常群发。

不新增：

```text
Tool Mode
Search Mode
MCP Mode
```

---

# 11. 与互评开关的关系

互评依然独立。

```text
互评 OFF
→ 只保留各自正常对话上下文

互评 ON
→ 在正常上下文基础上加入其他模型最终回答
```

工具不改变这一规则。

---

# 12. 数据保存

原始模型最终回答继续保存。

工具过程第一版不要求完整持久化。

必要时允许记录最少的错误信息，例如：

```json
{
  "tool_error": "search timeout"
}
```

但不建立复杂 Tool Trace 数据结构。

---

# 13. 第一版建议实施顺序

### Step 1：Web Search

先让一个真实模型完成：

```text
问题
↓
Search
↓
Final Answer
```

### Step 2：多模型

确认：

```text
Model A → Search
Model B → Search
Model C → 无 Search
```

不会互相干扰。

### Step 3：与互评结合

确认：

```text
Final A
Final B
Final C
↓
匿名 Peer Context
```

流程正常。

### Step 4：MCP

选择一个简单、稳定、实际有用的 MCP。

例如优先：

```text
文件
Git
本地脚本
```

不要一开始同时接多个 MCP。

---

# 14. Phase 3 验收

必须验证：

* Web Search 可以真实返回结果；
* 模型可以利用搜索结果形成最终回答；
* MCP 可以真实执行至少一个工具；
* 工具失败只影响对应模型；
* 单模型 / 群发继续正常；
* 互评 OFF 正常；
* 互评 ON 正常；
* 工具增强后的 Final Answer 可以进入匿名 Peer Context；
* CLOSED 模型仍然无法参与；
* Session / Round / Persistence 不被破坏。

---

# 15. Phase 3 不做

明确不实现：

* Tool-use 质量评分；
* Search 质量评分；
* MCP 调用质量评分；
* reasoning 质量评分；
* 自动判断是否应该搜索；
* 自动优化搜索策略；
* 搜索结果跨模型强制共享；
* Tool Trace 分析系统；
* Agent Supervisor；
* Agent Planner；
* 多 Agent 层级；
* Judge；
* Consensus；
* 自动模型选择；
* 成本优化系统。

---

# 16. 完成条件

Phase 3 完成的定义：

> **我可以在 CrossLLM 中选择一个或多个模型；模型可以在需要时使用 Web Search / MCP 获得外部信息，并正常产生 Final Answer；这个 Final Answer 仍然可以按照已有 Round 和匿名互评机制继续使用。**

Phase 3 完成后停止，不主动扩展为完整 Agent Framework。
