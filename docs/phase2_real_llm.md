# CrossLLM Phase 2：Real LLM Provider Integration

## 1. 阶段定位

Phase 2 是 CrossLLM 在 Phase 1 核心机制验证完成后的第一次真实模型接入阶段。

Phase 1 已使用 3 个 Mock Model 验证：

* Session 生命周期
* Model ACTIVE / CLOSED 生命周期
* Round 独立性
* 回答持久化
* Session Recovery
* Round 之间的信息传递
* ACTIVE 模型之间的匿名交叉注入
* SSE Streaming
* 单模型失败隔离

Phase 2 不重新设计上述机制。

本阶段唯一核心变化：

> **将 Phase 1 的 Mock Provider 替换为真实 LLM Provider，并验证现有 CrossLLM 核心机制在真实 LLM 场景下仍然成立。**

---

# 2. Phase 2 核心目标

使用真实 LLM 替代 Mock Model，跑通：

```text
用户问题
    ↓
多个真实 LLM 并发回答
    ↓
Round 1
    ↓
持久化
    ↓
关闭一个模型
    ↓
用户追加 Round 2 指令
    ↓
剩余 ACTIVE 模型读取 Round 1
    ↓
匿名交叉注入
    ↓
真实 LLM 再回答
    ↓
Round 2 持久化
    ↓
刷新浏览器
    ↓
恢复 Session
```

Phase 2 的成功标准不是“模型能回答问题”，而是：

> **Phase 1 已验证的状态、轮次、持久化、匿名化和模型生命周期机制，在真实 LLM Provider 下无需改变核心逻辑即可继续工作。**

---

# 3. Phase 2 非目标

本阶段明确不做：

* Judge
* Vote
* Consensus
* Winner
* Final Answer Synthesis
* Elo / 模型信誉分
* 模型排行榜
* 多 Agent 层级
* RAG
* MCP
* Skill
* Connector
* 自动化
* 用户认证
* 多用户
* 云端部署
* 数据库
* 完整产品 UI
* 模型质量评测系统
* 自动选择最佳模型
* 自动选择 Provider
* 无限 Round
* 复杂成本优化
* Prompt A/B Testing

本阶段不改变 CrossLLM 当前的核心交互模式。

---

# 4. Provider 抽象目标

Phase 1 已存在：

```text
RoundManager
      ↓
provider_factory(model_id)
      ↓
MockProvider
```

Phase 2 将：

```text
MockProvider
      ↓
Real LLM Provider
```

但 `RoundManager` 不应知道具体厂商 API。

RoundManager 只关心统一能力：

```python
async for chunk in provider.generate(
    round_id=...,
    prompt=...
):
    ...
```

Provider 层负责处理：

* API 地址
* API Key
* Model Name
* HTTP / SDK 调用
* Streaming
* Provider 特有错误
* 超时
* 响应解析

核心 Round / Session / Storage / Anonymizer 不直接调用厂商 API。

---

# 5. 第一阶段真实模型范围

Phase 2 首次实验使用 3 个真实模型。

模型名称、Provider 和 API 参数不硬编码进核心业务逻辑。

通过配置指定：

```text
Model A
Model B
Model C
```

例如：

```yaml
models:
  - id: model_a
    provider: ...
    model: ...

  - id: model_b
    provider: ...
    model: ...

  - id: model_c
    provider: ...
    model: ...
```

具体选择哪些真实模型属于部署配置，不属于核心架构。

---

# 6. 配置要求

真实模型配置不得写死在 Python 源码中。

至少需要能够配置：

* model_id
* display_name
* provider
* model
* API endpoint
* API key
* 是否启用

API Key 不得：

* 提交进 Git
* 写入 Session
* 写入 Round 文件
* 出现在 API 响应
* 出现在 Prompt
* 出现在日志

推荐使用：

```text
.env
```

或系统环境变量。

例如：

```text
CROSSLLM_API_KEY_xxx
CROSSLLM_API_KEY_yyy
```

实际命名由实现确定。

---

# 7. Provider 接口

Phase 2 应定义统一 Provider 接口。

最小接口：

```python
class LLMProvider(Protocol):
    async def generate(
        self,
        *,
        round_id: int,
        prompt: str,
    ) -> AsyncIterator[str]:
        ...
```

核心逻辑只能依赖该统一接口。

Provider 具体实现负责把真实 LLM 的 Streaming 响应转换为：

```text
chunk -> str
```

因此 RoundManager 不关心：

* OpenAI Responses API
* Chat Completions
* Anthropic API
* Qwen API
* DeepSeek API
* OpenAI-compatible API

这些属于 Provider 实现细节。

---

# 8. Model ID 与真实模型名称

CrossLLM 内部存在两个不同概念：

```text
model_id
```

和：

```text
provider / model
```

例如：

```yaml
id: qwen_main
provider: openai_compatible
model: Qwen...
```

其中：

* `model_id`：CrossLLM 内部身份
* `provider`：调用方式
* `model`：实际 API 模型名称

这些内部信息可以存在于：

* 配置
* Session
* state.json
* UI
* 日志

但除非属于模型自己的必要系统说明，否则不得自动进入其他参与者的匿名 Peer Context。

---

# 9. 匿名互评约束

Phase 1 的匿名机制全部保留。

真实 LLM 接入后必须继续满足：

```text
真实 model_id
      ↓
内部 candidate_id
      ↓
进入 Peer Context
```

其他参与者看到：

```text
Candidate A
Candidate B
```

不得看到：

```text
Qwen
DeepSeek
GPT
```

也不得看到：

```text
model_id
provider
provider model name
```

匿名化逻辑继续由：

```text
core/anonymizer.py
```

负责。

Phase 2 不重新实现匿名化。

---

# 10. 关于模型是否知道自己的身份

Phase 2 不要求隐藏模型自己的身份。

模型可以知道：

> 当前调用的是它自己。

需要隐藏的是：

> **其他 Candidate 对应哪个真实模型。**

因此：

```text
Self
Candidate A
Candidate B
```

可以存在。

但是：

```text
Candidate B = GPT
```

不得进入模型 Context。

---

# 11. Round 1 约束

Round 1 仍然严格独立。

每个模型只能收到：

```text
原始问题
+
系统公共指令
+
必要的 Provider / 调用层控制信息
```

不得收到其他模型的回答。

不得因为接入真实 Provider 而改变 Round 1 逻辑。

---

# 12. Round 2 约束

Round 2 仍然：

```text
自己的 Round 1
+
其他 ACTIVE 模型的 Round 1
+
用户本轮指令
```

只读取：

```text
Round N-1
```

不得把所有历史 Round 全部注入。

CLOSED 模型不得进入交叉上下文。

错误模型的无效/不完整回答不得被当成正常 Candidate 内容引用。

---

# 13. 真实 LLM Streaming

Provider 必须支持 Streaming。

真实模型返回的内容应被转换为统一的：

```text
chunk
```

再交给既有：

```text
RoundBus
    ↓
SSE
    ↓
浏览器
```

Phase 2 不重新设计 SSE。

目标：

```text
真实 LLM
    ↓
Provider
    ↓
chunk
    ↓
RoundManager
    ↓
RoundBus
    ↓
SSE
```

---

# 14. 并发

三个真实模型应继续并发执行。

原则保持：

```python
await asyncio.gather(...)
```

单模型不能因为：

* 慢
* timeout
* API error
* HTTP error
* streaming 中断

导致其他模型被取消。

---

# 15. 错误隔离

Phase 1 的：

> 单模型失败，不影响其他模型

必须继续成立。

至少覆盖：

* HTTP 非 2xx
* API 返回错误
* API Key 无效
* Provider timeout
* Streaming 中途断开
* 响应格式异常

例如：

```text
Qwen     → complete
DeepSeek → error
GPT      → complete
```

整轮仍视为可继续的 Round。

失败模型不得被自动判定为“错误答案”或“低质量模型”。

---

# 16. Timeout

真实 Provider 必须具备超时保护。

至少区分：

```text
连接/请求超时
Streaming 超时
```

具体默认值由实现阶段根据实际 Provider 情况确定。

不得无限等待某一个模型。

---

# 17. Retry

Phase 2 不要求复杂重试系统。

默认：

> **不自动无限重试。**

如果实现重试：

* 必须有明确次数上限；
* 必须避免一个模型的重试阻塞整个 Round；
* 不得因此产生重复写入；
* 不得改变 Round 逻辑。

第一版建议最多实现最小重试，或暂不实现自动 Retry。

---

# 18. 数据持久化

Phase 1 数据布局继续使用：

```text
sessions/
└── <session_id>/
    ├── state.json
    ├── question.md
    ├── round_001/
    │   ├── <model>.md
    │   ├── <model>.prompt.md
    │   └── ...
    └── round_002/
        └── ...
```

真实 LLM 接入不得改变：

* Round 历史结构
* 文件覆盖规则
* Session 恢复方式
* ACTIVE / CLOSED 状态

API Key 不得持久化。

---

# 19. Prompt 留痕

Phase 1 已保存：

```text
<model>.prompt.md
```

Phase 2 继续保存。

用于检查：

* Round 1 是否独立；
* Round 2 是否读取上一轮；
* CLOSED 模型是否被排除；
* Candidate 标签是否正确；
* 真实模型身份是否泄露。

Prompt 文件属于调试和实验数据。

其中不得出现：

* API Key
* Authorization Header
* Secret
* Cookie

---

# 20. 日志

日志可以记录：

* session_id
* round_id
* model_id
* 请求开始时间
* 首 token 时间
* 完成时间
* error
* token/chunk 数量（若容易取得）

日志不得记录：

* API Key
* Authorization Header
* 完整 Secret
* 不必要的用户隐私信息

---

# 21. 成本与 Token 记录

Phase 2 第一版：

> **可以记录，但不是成功条件。**

若 Provider 容易取得：

* input tokens
* output tokens
* total tokens
* cost

可以加入运行日志或实验元数据。

如果不同 Provider 返回格式不同，不为了统一成本统计而引入复杂抽象。

---

# 22. 真实模型身份与匿名身份的边界

系统内部：

```text
model_id
display_name
provider
model
candidate_id
```

可以共存。

进入其他模型的 Peer Context 后：

```text
candidate_id
answer
```

只保留模型可见的匿名信息。

不能因为真实 Provider 接入而直接把配置对象序列化进 Prompt。

例如禁止：

```json
{
  "model_id": "qwen",
  "provider": "dashscope",
  "model": "Qwen..."
}
```

进入 Peer Context。

---

# 23. Provider 配置与 Model Registry

Phase 2 可以扩展：

```text
ModelRegistry
```

使它从固定 Mock 注册表升级为：

```text
配置
 ↓
Model Registry
 ↓
Provider Factory
 ↓
具体 LLM Provider
```

但：

> **ModelRegistry 不负责发起 API 请求。**

> **Provider 负责 API 请求。**

> **RoundManager 不应该直接调用厂商 SDK。**

---

# 24. 推荐实现结构

在 Phase 1 基础上做最小调整：

```text
CrossLLM/
├── core/
│   ├── state.py
│   ├── storage.py
│   ├── session_manager.py
│   ├── model_manager.py
│   ├── prompt_builder.py
│   ├── anonymizer.py
│   └── round_manager.py
│
├── providers/
│   ├── mock_provider.py
│   ├── base.py
│   └── real_provider.py
│
├── config/
│   └── models.yaml
│
├── static/
├── sessions/
├── app.py
├── requirements.txt
├── .env
└── test_phase2.py
```

实际是否拆成多个 Provider 文件，以真实 Provider 数量和接口差异决定。

---

# 25. 不要过早引入 LiteLLM

Phase 2 的目标是：

> **验证 CrossLLM 自己的 Provider 边界是否成立。**

因此第一版不强制使用 LiteLLM。

优先验证：

```text
CrossLLM
    ↓
自己的统一 Provider Interface
    ↓
真实 Provider
```

如果真实模型接入后发现：

* 多 Provider API 差异过大；
* 自己维护适配层成本明显过高；

再在后续阶段评估 LiteLLM 等统一层。

LiteLLM 不能因为“以后可能需要”而提前成为 Phase 2 的架构核心。

---

# 26. Phase 2 测试重点

至少保留 Phase 1 的全部核心测试。

在此基础上新增：

### Provider

* [ ] 真实 Provider 可以正常建立请求
* [ ] Streaming 可以正常转换为 chunk
* [ ] API Error 能正确转换为 error
* [ ] timeout 不会阻塞其他模型

### 并发

* [ ] 多个真实模型并发运行
* [ ] 一个模型变慢不阻塞其他模型
* [ ] 一个模型失败不取消其他模型

### Round

* [ ] Round 1 仍然完全独立
* [ ] Round 2 可以读取上一轮
* [ ] 只读取上一轮，不读取更早 Round

### Model Lifecycle

* [ ] CLOSED 模型不会产生新的 API 请求
* [ ] CLOSED 模型历史回答仍存在
* [ ] ACTIVE 模型继续运行

### Anonymous Peer Context

* [ ] Peer Context 不包含真实 peer model_id
* [ ] 不包含真实 peer display_name
* [ ] 不包含真实 provider/model name
* [ ] candidate 标签正确
* [ ] candidate 标签轮内一致
* [ ] CLOSED 模型不进入 Context

### Persistence

* [ ] Real LLM Round 1 正常落盘
* [ ] Real LLM Round 2 正常落盘
* [ ] 历史 Round 不覆盖
* [ ] 浏览器刷新后 Session 可以恢复

---

# 27. Phase 2 核心人工验收

必须至少人工完成一次完整流程：

```text
建立 Session
    ↓
3 个真实模型
    ↓
Round 1
    ↓
确认三个真实模型均正常返回
    ↓
关闭其中一个模型
    ↓
Round 2
    ↓
确认 CLOSED 模型无新请求
    ↓
检查两个 ACTIVE 模型实际 Prompt
    ↓
确认只有匿名 Candidate
    ↓
确认包含自己的 Round 1
    ↓
确认包含其他 ACTIVE 模型 Round 1
    ↓
确认不包含 CLOSED 模型
    ↓
刷新浏览器
    ↓
恢复 Session
```

---

# 28. Phase 2 实验问题

Phase 2 重点观察：

### 问题 1：真实模型 Streaming 是否符合 Phase 1 假设？

Mock Provider 的 chunk 行为是人工控制的。

真实 LLM 会出现：

* chunk 很小；
* chunk 很大；
* 长时间无 token；
* 中途断开。

需要确认现有 SSE / RoundBus 是否仍然稳定。

### 问题 2：不同 Provider 的 Streaming 接口能否被统一？

如果统一接口非常自然，则维持当前抽象。

如果出现明显差异，记录差异后再决定是否扩展 Provider Interface。

### 问题 3：真实回答内容是否会造成匿名身份泄漏？

例如模型自己主动说：

```text
我是……
```

或者引用：

```text
根据另一模型……
```

需要观察。

Phase 2 首先记录问题，不自动引入复杂的身份清洗系统。

### 问题 4：真实模型是否会因为 Candidate 上下文而产生身份推断？

即使系统没有显式告诉模型：

```text
Candidate B = GPT
```

模型仍可能通过回答风格、格式、知识倾向推测身份。

这种情况属于：

> **模型推断**

而不是：

> **系统身份泄露**

Phase 2 暂不试图完全消除模型推断。

---

# 29. Phase 2 的成功标准

Phase 2 成功必须同时满足：

```text
真实 LLM
   ↓
统一 Provider
   ↓
并发
   ↓
Round 1
   ↓
Persistence
   ↓
ACTIVE / CLOSED
   ↓
Round 2
   ↓
Anonymous Peer Context
   ↓
Streaming
   ↓
Persistence
   ↓
Session Recovery
```

并且 Phase 1 已验证的核心架构无需重写。

最终证明：

> **CrossLLM 的核心 Session / Round / Model Lifecycle / Persistence / Anonymous Peer Context 架构不仅能运行 Mock Model，也能承载真实 LLM。**

---

# 30. Phase 2 完成后的停止条件

Phase 2 完成后立即停止。

不得因为真实模型已经接入而自动继续：

* Judge
* Consensus
* 多轮 Debate
* Model Ranking
* RAG
* MCP
* Agent
* Cost Optimizer
* Auto Model Selection
* 产品化 UI

这些机制需要根据 Phase 2 的真实运行结果重新定义。

---

# 31. Phase 2 与 Phase 1 的关系

Phase 1：

```text
Mock Provider
     ↓
验证核心架构
```

Phase 2：

```text
Real LLM Provider
     ↓
验证真实运行环境
```

两阶段之间唯一重要变化：

```text
Mock → Real
```

Session / Round / Persistence / Lifecycle / Anonymous Peer Context 不应因为接入真实模型而重写。

---

# 32. 最终原则

CrossLLM 当前开发顺序保持：

```text
Phase 0
源码研究
    ↓
Phase 1
Mock Core Mechanism Validation
    ↓
Phase 2
Real LLM Provider Integration
    ↓
根据实验结果决定下一阶段
```

不要提前假设 Phase 3。

CrossLLM 的每个阶段都应该通过实际运行结果决定下一步，而不是为了追求“完整架构”提前实现未来功能。
