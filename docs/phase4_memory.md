# CrossLLM Phase 4：Context Management & Long-Term Memory

## 0. Phase 4 一句话目标

解决 CrossLLM 长期使用时历史越来越长的问题：

> **只要上下文空间允许，就尽可能无损保留有效历史；超过无损保留预算后，将较旧历史转为压缩表示；用户可以手动 Pin 重要历史，并可以主动生成模型自己的对话 Summary；Peer Review 在需要时自动复用 Summary；MCP 外部信息始终保持可追溯。**

本阶段同时处理：

```text
Context Management
        ↓
决定当前这一轮应该把哪些历史交给 LLM

Summary
        ↓
让用户主动把当前模型的长期对话整理成可继续使用的工作状态

Peer Review Preparation
        ↓
互评前决定直接使用最新有效回答，还是先生成该模型的 Summary

Compaction
        ↓
当前 Session 历史太长时，压缩较旧内容

Long-Term Project Memory
        ↓
让不同 Session 以后复用已经确认的重要经验
```

三种 Memory / History 能力必须严格分开：

```text
Context Management
≠
Conversation Summary
≠
Compaction
≠
Project Memory
```

其中：

* Context Management = 当前请求应该放什么；
* Conversation Summary = 某个模型自己目前“知道什么、认为什么、还不确定什么”；
* Compaction = 为了 Context Budget 对旧历史做压缩；
* Project Memory = 跨 Session 长期保存、经过明确确认的项目知识。

---

# 1. Phase 4 核心原则

CrossLLM 长期历史管理遵循以下原则：

1. **原始历史不因为 Context 超限而删除。**
2. **不再使用固定“最近 N 轮”作为历史可见性的硬上限。**
3. **有效历史优先保持 User Question + 成功 Final Answer 的完整语义单元。**
4. **用户能 Pin 重要历史，Pin 历史不得被自动 Compaction。**
5. **k 只控制历史预算在“无损历史”和“压缩历史”之间如何分配。**
6. **当前请求必须为模型输出保留明确预算。**
7. **输出预算优先来自模型能力和本次请求配置，不允许由 CrossLLM 随意猜测固定值。**
8. **失败 Round 不进入正常 Conversation Context，但运行档案保留。**
9. **MCP Tool Result 不自动等于 Fact。**
10. **Summary 不自动等于 Fact，也不要求所有内容必须已经验证。**
11. **Peer Review 结果不作为下一次 Peer Review 的输入材料。**
12. **所有影响 Context、Summary、Peer Review 的重要决策必须写入结构化日志。**
13. **Project Memory 第一版不自动永久写入，必须经过明确确认。**

---

# 2. 基本术语

## 2.1 有效 Round

一个 Round 是用户的一次提问以及这一轮模型产生的结果。

对于单模型：

```text
Round N

├── User Question
└── Model Final Answer
```

对于多个模型：

```text
Round N

├── User Question
├── Model A Final Answer
├── Model B Final Answer
└── Model C Final Answer
```

只要至少存在一个成功 Final Answer：

```text
Valid Round = true
```

如果所有参与模型都失败：

```text
Round N

User Question

Model A → 429
Model B → 400
Model C → timeout
```

则：

```text
Valid Round = false
```

该 Round 不进入后续正常 Conversation Context。

但：

```text
Runtime Log
```

继续保留。

---

# 3. Conversation Context 与 Raw Archive

必须严格区分：

```text
Raw Archive
```

和：

```text
Current Context
```

## 3.1 Raw Archive

保存完整原始信息：

```text
User Question
Model Final Answer
Peer Review / Peer Context
Summary
Tool Call
Tool Result
Runtime Log
```

原始数据默认保留，不因为 Context 超限删除。

## 3.2 Current Context

只是当前准备发送给某个 LLM 的内容。

可能包含：

```text
Current User Question

Pin History

Recent Valid Q&A

Peer Context

Conversation Summary

Compacted History

必要 MCP Evidence
```

因此：

> **“本轮没有进入 Current Context”不等于“原始数据被删除”。**

---

# 4. 失败 Round 的处理

失败 Round 不进入正常 Conversation Context。

例如：

```text
R10

User：
分析方案 X

LLM：
429
```

下一轮：

```text
R11

User：
分析方案 X

LLM：
成功回答
```

当前 Context 默认只保留：

```text
R11 User
R11 Answer
```

而不携带：

```text
R10 User
R10 Error
```

原因：

* 失败 Round 没有产生有效模型分析；
* 用户通常会重新提交；
* 错误信息主要用于诊断；
* 避免错误日志持续占用 Context。

但：

```text
R10 Runtime Log
```

仍然保留。

如果一个 Round 中部分模型成功、部分模型失败：

```text
R20 User

A → success
B → timeout
C → success
```

则该 Round 仍然是：

```text
Valid Round = true
```

正常 Context 只使用：

```text
R20 User
R20 A Final Answer
R20 C Final Answer
```

B 的失败记录仍只进入运行档案。

---

# 5. Round 内部的基本历史单位

CrossLLM 不再将单独 Model Answer 作为主要历史单位。

一个有效 Round 至少包含：

```text
User Question
+
成功 Model Final Answer
```

多个模型成功时：

```text
User Question
+
所有成功 Final Answers
```

因此：

```text
Historical Unit = Round
```

而不是：

```text
Historical Unit = Answer
```

原因：

> Model Answer 离开对应 User Question 后，可能无法正确理解其上下文。

默认情况下，Round 不应无理由拆开。

---

# 6. Conversation Summary：模型自己的工作状态

## 6.1 Summary 的定义

Conversation Summary 是：

> **某一个模型截至当前对话所形成的“工作状态压缩表示”。**

它的目标是减少历史长度，同时尽量保留：

```text
用户正在解决什么
模型已经知道什么
模型提出过什么观点
模型为什么形成这些观点
哪些观点已经被支持
哪些观点存在争议
哪些内容仍未验证
哪些方案已经尝试
哪些方案已经失败或被否定
当前尚未解决什么
模型当前倾向什么
```

Summary：

```text
不是数据库中的 Fact 集合
不是自动验证系统
不是 Project Memory
不是 Peer Review
```

尤其禁止将：

```text
Model Opinion
```

自动升级为：

```text
Confirmed Fact
```

---

# 7. Summary 不强制使用固定知识分类

不能规定所有 Summary 都必须输出：

```text
Facts
Decisions
Experiments
Rejected
Open Questions
```

因为很多真实对话只有：

```text
一个观点
一个假设
一个尚未验证的推断
```

例如：

```text
模型认为方案 A 可能比方案 B 更好，
目前依据主要来自理论分析，
没有实际实验验证。
```

Summary 必须允许忠实表达这种状态，而不能自动变成：

```text
Fact:
方案 A 优于方案 B
```

更推荐的语义结构是：

```text
当前理解：
...

我的判断 / 倾向：
...

依据：
...

存在争议：
...

未经验证：
...

已经尝试：
...

已经排除：
...

未解决问题：
...
```

这些字段可以按实际情况出现，不要求全部存在。

核心原则：

> **Summary 的任务是忠实保留模型当前状态，而不是替模型创造确定性。**

---

# 8. Summary 功能 UI

Summary 是用户可以主动使用的独立功能。

界面提供：

```text
[ ] Summary / 总结
```

当用户勾选 Summary：

```text
输入框
```

自动清空，并填入系统生成的 Summary Prompt。

用户可以：

```text
直接发送
```

也可以：

```text
修改 Prompt
再发送
```

因此 Summary 功能同时满足：

```text
减少重复输入
+
提供统一总结模板
+
允许用户修改总结要求
```

---

# 9. Summary Prompt

第一版提供默认 Prompt。

建议默认：

```text
请总结我们截至目前的完整对话，生成一份可供后续继续讨论使用的工作状态总结。

要求：

1. 忠实保留用户当前要解决的问题和目标。
2. 保留你已经形成的主要观点、判断、推理依据和当前倾向。
3. 区分事实、实验结果、外部证据、模型推测和未经验证的假设。
4. 不要因为你之前说过某件事，就自动把它表述成已确认事实。
5. 保留存在争议的观点以及不同可能性。
6. 保留已经尝试、失败、排除或暂时放弃的方案。
7. 保留仍然没有解决的问题。
8. 删除纯重复、无信息量的闲聊，但不要为了简洁而删除重要依据。
9. 不要引入原始对话中不存在的新事实。
10. 该总结将用于后续继续讨论，因此应优先保留对未来决策有价值的信息。
```

该 Prompt：

```text
默认可编辑
```

系统不要求用户必须使用默认 Prompt。

---

# 10. Summary 与普通模型回答的关系

用户手动执行 Summary 时：

```text
历史 Conversation
        ↓
Summary Prompt
        ↓
Model
        ↓
Summary
```

Summary 是一个新的有效模型输出，但其语义必须明确标记为：

```text
conversation_summary
```

不能和普通：

```text
final_answer
```

混为一谈。

---

# 11. Summary 的最新状态

一个模型可以拥有：

```text
Summary_001
Summary_002
Summary_003
```

默认以后续最新成功 Summary 作为当前模型的：

```text
latest_conversation_summary
```

旧 Summary 不删除。

它们仍属于 Raw Archive。

---

# 12. Peer Review 的重新定义

Peer Review 是：

> **让当前模型看到其他 ACTIVE 模型截至当前阶段的主要思考状态，并据此继续回答。**

Peer Review 不再简单定义为：

```text
拿其他模型上一轮 Final Answer
```

而是采用：

```text
Peer Context Preparation
```

先决定其他模型应该提供什么形式的信息。

---

# 13. Peer Context Preparation

对于每个其他 ACTIVE 模型：

### 情况 A：只有 1 个有效回答

直接使用该模型唯一成功的 Final Answer：

```text
Peer Context = Final Answer
```

不额外生成 Summary。

原因：

```text
历史太短
Summary 信息增益有限
额外 LLM 调用没有必要
```

### 情况 B：存在多个有效回答

执行该模型的 Summary 流程：

```text
该模型历史
     ↓
Summary Prompt
     ↓
该模型
     ↓
最新 Conversation Summary
```

然后：

```text
Peer Context = latest Conversation Summary
```

因此：

```text
有效回答数 = 1
→ Final Answer

有效回答数 > 1
→ Conversation Summary
```

---

# 14. Peer Review 自动调用 Summary

当：

```text
Peer Review = ON
```

并且其他模型满足：

```text
Valid Answer Count > 1
```

CrossLLM 自动调用已经实现的 Summary 能力。

该过程不要求用户再次输入 Summary Prompt。

执行逻辑：

```text
Peer Review
    ↓
检查其他模型有效回答数
    ↓
只有一个有效回答？
    ├── 是 → 直接使用 Final Answer
    │
    └── 否 → 自动执行 Summary
                 ↓
           使用最新 Summary
                 ↓
             Peer Context
```

因此：

> **Peer Review 复用 Summary 能力，而不是另外实现一套 Peer Memory。**

---

# 15. Peer Review 与 Summary 的隔离

Peer Review 不修改模型自己的 Conversation Summary。

例如：

```text
Model A Summary
```

提供给：

```text
Model B
```

B 生成：

```text
Peer Review / 当前回答
```

但：

```text
Peer Review
```

不能写回：

```text
Model A Summary
```

不能形成：

```text
A Summary
→ B Review
→ A Summary
→ C Review
→ A Summary
```

这样的反馈循环。

---

# 16. Peer Review 结果的生命周期

Peer Review 结果可以成为当前模型本轮正常对话的一部分。

例如：

```text
Current Round

User Question

Peer Context:
B Summary
C Summary

Model A Final Answer
```

下一轮 Model A 可以看到自己之前的：

```text
A Final Answer
```

也可以根据普通 Context 策略看到：

```text
Peer Feedback
```

但是：

> **Peer Review 结果不得自动成为其他模型下一次 Peer Review 的输入材料。**

也就是说，跨模型传播的来源只能是：

```text
Other Model Final Answer
```

或：

```text
Other Model Conversation Summary
```

不能是：

```text
Other Model 对 Peer Review 的回应
```

---

# 17. Peer Context 的范围

Peer Context 只服务于当前互评/当前 Round。

它不是：

```text
永久 Peer Memory
```

默认不要求保留所有历史 Peer Context。

更旧的内容仍然可能通过：

```text
普通历史
+
Conversation Summary
+
Compaction
```

进入当前模型。

这样避免：

```text
R1 Peer
R2 Peer
R3 Peer
...
R19 Peer
```

全部独立占用 Context。

---

# 18. Pin：用户控制的无损历史

## 18.1 Pin 是什么

Pin 表示：

> **用户明确要求某个历史 Round 在自动 Context 管理中保持原文，不参与自动 Compaction。**

例如：

```text
R1 → Pin
R7 → Pin
```

则：

```text
R1 User + 成功 Answers

R7 User + 成功 Answers
```

属于高优先级无损历史。

## 18.2 Pin 不是 Summary

Pin：

```text
保留原文
```

Summary：

```text
压缩当前模型的对话状态
```

两者不能混为一谈。

## 18.3 Pin 不是 Project Memory

Pin：

```text
当前 Context 优先级
```

Project Memory：

```text
跨 Session 长期知识
```

---

# 19. Pin 的日志要求

每次 Pin / Unpin 必须记录：

```json
{
  "event": "memory_pin",
  "round_id": 1,
  "pinned": true,
  "timestamp": "...",
  "reason": "user"
}
```

取消：

```json
{
  "event": "memory_pin",
  "round_id": 1,
  "pinned": false,
  "timestamp": "..."
}
```

必须保留历史变化记录。

---

# 20. k：用户控制的无损保留比例

`k` 是用户可调参数：

```text
0.01 ~ 0.90
```

定义：

> **k 决定历史预算中有多少空间优先用于无损历史，以及多少空间留给压缩后的旧历史。**

例如：

```text
k = 0.5
```

表示：

```text
约 50% → Lossless History
约 50% → Compacted History
```

注意：

> **这不是模型 Context 总量的 50%。**

---

# 21. Context Budget 的计算

context_limit 是模型能力数据，不是 CrossLLM 的用户策略参数。

获取优先级：

1. Provider / 模型能力元数据
2. models.yaml 中的 context_limit 兜底值
3. 两者都不可得 → unknown

如果 Provider 元数据获取成功，即使 models.yaml 中存在 context_limit，
也必须以 Provider 元数据为准。

models.yaml 中的 context_limit 仅作为：
- Provider 无法获取能力时的 fallback
- 用户明确知道模型能力并希望提供本地 fallback 时的配置

用户不要求理解或手动修改 context_limit。

首先获取：

```text
context_limit
```

然后确定本次请求必须占用的 Token：

```text
System Prompt
+
Tool Definitions / Tool Context
+
Current User Question
+
其他必须保留当前内容
+
Reserved Output
```

于是：

```text
history_budget
=
context_limit
-
fixed_input_tokens
-
reserved_output_tokens
```

其中：

```text
fixed_input_tokens
=
System
+
Tools
+
Current User
+
其他当前请求内容
```

然后：

```text
lossless_budget
=
history_budget × k

compaction_budget
=
history_budget × (1-k)
```

---

# 22. 输出预留：使用模型能力与本次请求配置

### CrossLLM 请求输出上限原则

CrossLLM 用户界面不要求用户手动设置 request_max_output。

CrossLLM 不得自行使用：

context_limit × 固定比例

或：

固定 16000
固定 8192
固定 80%
固定 90%

等经验值作为 request_max_output 或 reserved_output_tokens。

如果用户没有显式提供本次请求输出上限：

优先使用 Provider / 模型能力提供的合法最大输出能力；
如果无法取得该能力，不得伪造精确值；
由 Provider 自身的默认输出策略处理，或按明确记录的 unknown / degraded policy 处理。

k 只负责：

history_budget

在：

lossless history

与：

compaction history

之间的比例分配。

k 不负责决定模型最多输出多少 Token。

models.yaml 不得使用名为 max_output_tokens 的字段表示模型能力。

不得出现：

max_output_tokens: 8192
max_output_tokens: 0.9 × context_limit
request_max_output 不是 CrossLLM 的常驻模型配置。

它只表示某一次请求实际传给 Provider 的输出上限。
默认不由用户设置。

如果 CrossLLM 没有明确请求输出上限：
request_max_output = null / provider_default / unknown
具体由 Provider 的请求语义决定。

CrossLLM 不得自行创造固定值。
```text
reserved_output = 16000
```

这种固定值。

模型通常存在：

```text
Model Context Limit
Model Maximum Output Limit
```

并且当前 API 请求还可能明确设置：

```text
max_completion_tokens
```

因此需要区分：

```text
model_max_output
```

和：

```text
request_max_output
```

建议优先获取 Provider / OpenRouter 的模型元数据，例如：

```json
{
  "context_limit": 131072,
  "model_max_output": 32768
}
```

本次请求如果显式配置：

```text
max_completion_tokens = 16384
```

则：

```text
request_max_output = 16384
```

本次预算中的：

```text
reserved_output_tokens
```

应基于本次请求实际采用的输出上限，并且不能超过模型允许的最大输出能力。

因此：

```text
reserved_output_tokens
=
有效 request output limit
```

第一版必须把：

```text
模型能力上限
```

和：

```text
CrossLLM 本次请求上限
```

分开记录。

---

# 23. 输出预算的特殊注意事项

某些模型可能包含：

```text
reasoning tokens
```

因此：

```text
completion budget
```

不一定等同于最终可见文本长度。

CrossLLM 不应该自行假设：

```text
16K completion
=
16K visible answer
```

应以 Provider / OpenRouter 实际语义为准。

日志中至少记录：

```text
model_max_output
request_max_output
reserved_output_tokens
```

如果 Provider 无法提供某项信息：

```text
unknown
```

不得伪装成精确值。

---

# 24. Context Budget 硬约束

始终必须满足：

```text
Input Tokens
+
Reserved Output Tokens
≤
Context Limit
```

例如：

```text
Context Limit = 128K

Fixed Input = 20K

Reserved Output = 16K
```

则：

```text
History Budget
=
128K - 20K - 16K
=
92K
```

如果：

```text
k = 0.5
```

则：

```text
Lossless Budget = 46K

Compaction Budget = 46K
```

目标：

```text
Current User
+
Fixed Context
+
≤ 46K Lossless History
+
≤ 46K Compaction
+
16K Reserved Output
≤ 128K
```

---
当有效输出上限未知时：

CrossLLM 不得伪造 reserved_output_tokens。

此时记录：
reserved_output_tokens = unknown
budget_status = degraded

Context 管理只能证明当前已知 Input Budget，
最终输出长度约束由 Provider / 上游 API 自身能力负责。

只有在有效输出上限已知时，
CrossLLM 才能对：

Input Tokens + Reserved Output Tokens ≤ Context Limit

进行完整预算校验。
# 25. Provider Token 数据不能假装精确

如果某个 Provider 无法在建立 Prompt 前准确提供：

```text
System token count
Tool token count
```

则系统必须记录：

```text
estimated
```

或：

```text
unknown
```

不能在日志里写成：

```text
exact
```

第一版可以使用：

```text
provider tokenizer
```

或者：

```text
CrossLLM token estimator
```

但必须记录其来源。

---

# 26. k 与 Pin 的关系

Pin 从：

```text
lossless_budget
```

中优先占用空间。

例如：

```text
History Budget = 80K
k = 0.5
Lossless Budget = 40K
```

Pin：

```text
R1 = 10K
R7 = 8K
```

则：

```text
Pinned Tokens = 18K

Remaining Lossless Budget = 22K
```

剩余空间用于：

```text
Recent Valid History
```

因此：

> **Pin 不额外创造 Context 空间，而是优先占用无损预算。**

---

# 27. Pin 过多时处理

如果：

```text
Pinned History > Lossless Budget
```

不能偷偷压缩 Pin。

正常处理顺序：

```text
Current User 必须保留

↓

Pinned History 保持原文

↓

尽可能减少普通 Recent Raw

↓

如果仍然超限

↓

明确记录 Context Overflow / Cannot Fit
```

不能：

```text
自动解除 Pin
```

不能：

```text
自动压缩 Pin
```

不能：

```text
静默丢弃 Pin
```

日志：

```json
{
  "event": "context_budget_warning",
  "reason": "pinned_history_exceeds_budget",
  "lossless_budget": 40000,
  "pinned_tokens": 52000
}
```

---

# 28. 无损历史选择顺序

正常无损历史的优先级：

```text
1. Current User Question
2. Pinned Rounds
3. Recent Valid Rounds
4. Peer Context
```

其中：

```text
Peer Context
```

是当前 Peer Review 场景下的临时高价值输入。

它不能变成：

```text
永久独立 Peer History
```

---

# 29. 最近有效历史的加载方式

禁止：

```text
HISTORY_WINDOW = 3
```

作为正式硬限制。

改为：

> **从最新 Valid Round 向历史方向倒序遍历，在剩余 Lossless Budget 允许的情况下，尽可能加入完整 Round。**

例如：

```text
R19 Q&A = 4K
R18 Q&A = 7K
R17 Q&A = 3K
R16 Q&A = 6K
R15 Q&A = 5K
```

剩余：

```text
24K
```

则加入：

```text
R19 = 4K
R18 = 7K
R17 = 3K
R16 = 6K
```

共：

```text
20K
```

加入 R15 后：

```text
25K
```

超过预算，于是停止。

---

# 30. 不应该固定“最近一轮 Peer Answer”

旧设计曾规定：

```text
Peer Context = 上一轮其他模型 Final Answer
```

Phase 4 改为：

```text
Peer Context
=
Peer Context Preparation 的输出
```

其来源根据情况决定：

```text
有效回答数 = 1
→ Final Answer

有效回答数 > 1
→ 最新 Conversation Summary
```

因此“最近一轮”不再是 Peer Context 的唯一语义来源。

---

# 31. 完整 Round 尽量作为整体加入

默认：

```text
User Question
+
所有成功 Final Answers
```

作为一个逻辑整体。

不要出现：

```text
Question 被移除
Answer 单独留下
```

也不要：

```text
某个 Answer 单独保留
但失去对应问题
```

如果整个 Round 太大：

```text
该 Round
→ 不进入无损 Raw
→ 进入 Compaction
```

默认不做任意拆分。

---

# 32. Compaction 的角色

Compaction 解决：

> **旧历史无法全部以无损形式继续保留时，如何保留其中仍有价值的信息。**

Compaction：

```text
不是删除
不是 Memory Database
不是 Project Memory
不是 Summary UI 功能
```

它是：

```text
Context Budget 压缩机制
```

---

# 33. k 与 Compaction 的关系

`k` 将：

```text
history_budget
```

划分为：

```text
lossless_budget
compaction_budget
```

因此：

```text
k 高
→ 更多原始历史
→ 更小 Compaction 空间

k 低
→ 更少原始历史
→ 更大 Compaction 空间
```

但：

> **k 不代表“什么时候触发 Compaction”。**

也不代表：

```text
达到 k 就开始压缩
```

k 是预算分配参数。

---

# 34. Compaction 的触发与历史选择

每次建立 Context 时：

```text
计算总 Context Budget

↓

确定 Lossless Budget

↓

加入 Pin

↓

从最新 Valid Round 向旧加入完整 Raw

↓

超过 Lossless Budget 的旧历史进入 Compaction 区域

↓

如果 Compaction 区已有可用 Summary
    → 使用 Summary

如果没有
    → 执行 Compaction
```

因此：

> **只要历史没有进入压缩区，就尽量保持原文；一旦历史超出无损预算，旧历史才需要 Summary。**

---

# 35. Compaction 的输入

Compaction 可以包含：

```text
更旧 Valid Rounds
+
旧 Summary
+
必要历史信息
```

不要求：

```text
失败 Round
```

进入正常历史压缩。

失败 Round 只保留在 Runtime Log。

---

# 36. Compaction 输出

Compaction 的目标：

> **用更少 Token 表达旧历史中仍然有价值的信息。**

第一版允许采用：

```text
Conversation Summary
```

风格，而不是强制知识库格式。

可以包含：

```markdown
# Compacted History

## User Goals
...

## Established Information
...

## Model Opinions
...

## Reasoning / Basis
...

## Disagreements
...

## Unverified Assumptions
...

## Tried / Rejected Approaches
...

## Open Questions
...

## Current Direction
...
```

字段按内容动态出现。

必须遵守：

> **Summary / Compaction 只能重述输入历史，不得创造原始历史不存在的新事实。**

---

# 37. Summary 与 Compaction 的区别

虽然两者都可能调用：

```text
Summary Prompt
```

但语义不同。

## 用户主动 Summary

目标：

```text
让当前模型形成自己的工作状态
```

来源：

```text
该模型自己的全部可用历史
```

调用方式：

```text
用户勾选 Summary
→ Prompt 自动填充
→ 用户发送
```

## Compaction

目标：

```text
让旧历史占用更少 Context
```

来源：

```text
旧历史
```

调用方式：

```text
Context Budget 需要压缩
→ 系统自动执行
```

因此：

```text
Same mechanism family
≠
Same lifecycle
```

---

# 38. Recursive Compaction

如果：

```text
Compacted Summary
+
Recent Raw
+
Pinned History
+
Current Request
+
Reserved Output
```

仍然超过 Context Limit：

```text
再次压缩更旧内容
```

流程：

```text
第一次 Compaction
      ↓
仍然超限
      ↓
第二次 Compaction
      ↓
仍然超限
      ↓
继续
```

第一版必须设置：

```text
MAX_COMPACTION_ROUNDS
```

防止无限循环。

不能直接照搬其他项目的固定值。

必须记录实际次数。

---

# 39. Compaction 实际压缩比例

系统应记录：

```text
input_tokens
actual_summary_tokens
```

然后形成：

```text
observed_compression_ratio
```

例如：

```text
20K → 5K
30K → 7K
```

系统可以得到历史实际经验。

该数据只能作为：

```text
预算估计
```

不能当作：

```text
理论固定压缩比例
```

因为不同主题、Prompt、模型的 Summary 长度不同。

---

# 40. k 的动态预算检查

如果预计：

```text
expected_summary_tokens
```

而：

```text
compaction_budget
```

不足：

```text
expected_summary_tokens > compaction_budget
```

系统应该：

```text
尝试进一步压缩
```

或：

```text
提示当前 k 可能导致压缩空间不足
```

可以计算估算值：

```text
k_max
=
1
-
expected_summary_tokens / history_budget
```

例如：

```text
history_budget = 80K
expected_summary = 24K
```

则：

```text
k_max = 0.70
```

这个值：

```text
只用于估算 / 提示
```

不能作为绝对安全保证。

---

# 41. Summary 是否真的成功必须可观测

每次 Summary 必须记录：

```text
summary_type
summary_trigger
source_rounds
input_tokens
output_tokens
status
```

例如：

```json
{
  "event": "conversation_summary",
  "model_id": "model_a",
  "trigger": "user",
  "source_rounds": [1,2,3,4,5],
  "input_tokens": 18000,
  "output_tokens": 4200,
  "status": "success"
}
```

Peer Review 自动生成：

```json
{
  "event": "conversation_summary",
  "model_id": "model_b",
  "trigger": "peer_review",
  "source_rounds": [1,2,3,4,5],
  "input_tokens": 21000,
  "output_tokens": 4800,
  "status": "success"
}
```

这样可以区分：

```text
用户主动总结
```

和：

```text
Peer Review 自动触发总结
```

---

# 42. Peer Context 日志

每次准备 Peer Context 必须记录：

```json
{
  "event": "peer_context_preparation",
  "round_id": 20,
  "source_model": "model_b",
  "valid_answer_count": 4,
  "mode": "conversation_summary",
  "summary_id": "summary_003"
}
```

只有一个有效回答时：

```json
{
  "event": "peer_context_preparation",
  "round_id": 20,
  "source_model": "model_b",
  "valid_answer_count": 1,
  "mode": "final_answer",
  "source_round_id": 19
}
```

必须能够回答：

> 为什么这一次 Peer Context 使用了 Summary，而不是直接使用 Final Answer？

---

# 43. Peer Review 的严格隔离日志

每次 Peer Review 必须记录：

```text
输入来源：
Final Answer
或
Conversation Summary
```

同时明确：

```text
peer_review_result = current_round_output
```

而不是：

```text
peer_review_result = future_peer_memory
```

日志必须能证明：

```text
上一轮 Peer Review
```

没有被自动注入到：

```text
下一轮其他模型的 Peer Context
```

---

# 44. MCP Runtime 与 MCP Evidence Index

MCP 信息拆成两个层次：

```text
MCP Runtime
=
原始运行记录

MCP Evidence Index
=
哪些外部结果实际被 Final Answer 使用
```

二者不能混淆。

---

# 45. MCP Runtime

Runtime 保存：

```text
MCP
Tool
Arguments
Call Status
Result
Error
Duration
Reconnect
Retry
```

例如：

```text
zhihu
zhihu_search
query="Dify"
success=true
result_chars=12012
```

Runtime 是：

> **原始运行档案。**

默认不参加：

```text
自动 Compaction
```

也不因为：

```text
Context 压缩
```

而被删除。

---

# 46. MCP Evidence Index

：

```text
Tool Call
≠
Useful Evidence
```

因为工具成功返回并不代表最终答案真的使用了它。

Evidence Index 的作用是记录：

```text
哪些 MCP Result 被最终 Final Answer 使用 / 引用
```

例如：

```json
{
  "event": "mcp_evidence_index",
  "round_id": 19,
  "references": [
    {
      "mcp": "github",
      "tool": "search_issues",
      "reference": "Issue #123",
      "used_in_final_answer": true
    },
    {
      "mcp": "openalex",
      "tool": "search",
      "reference": "result-set-001",
      "used_in_final_answer": false
    }
  ]
}
```

---

# 47. MCP Evidence 不能自动成为 Fact

必须保持：

```text
MCP Result
≠
Fact
```

例如：

```text
知乎文章说 X
```

最多说明：

```text
存在关于 X 的该说法
```

不能自动升级为：

```text
X 已验证
```

只有经过：

```text
实验
交叉验证
用户确认
其他可靠证据
```

等进一步过程后，才可能形成：

```text
Fact
Decision
Experiment Result
```

---

# 48. MCP Evidence Index 的生成时机

第一版不要求：

```text
每次 Tool Call
→ 立即再调用一个 LLM
```

优先在：

```text
Round 完成后
```

或：

```text
Compaction 时
```

生成。

原因：

此时已经同时拥有：

```text
MCP Runtime
+
Final Answer
```

可以判断哪些 Tool Result 实际被使用。

这样减少额外调用成本。

---

# 49. Project Memory

Project Memory 是跨 Session 长期知识。

它不属于本次 Context Selection 的直接历史层。

第一版建议：

```text
历史对话
+
必要 MCP Evidence
+
实验结果
+
用户决策
↓
Candidate Memory
↓
用户确认
↓
Project Memory
```

禁止：

```text
每轮对话
↓
自动永久写入 Project Memory
```

---

# 50. Project Memory 的信息类型

可以采用：

```text
Facts
Decisions
Experiments
Rejected
Open Questions
Evidence References
```

Project Memory 与 Conversation Summary 不同：

```text
Conversation Summary
=
模型当前工作状态

Project Memory
=
项目长期复用知识
```

---

# 51. Memory 可靠性

必须严格区分：

```text
Model Opinion
User Decision
Experiment
External Evidence
Fact
```

例如：

```text
Model A：
X 很好。

Model B：
X 不好。

GitHub：
存在相关 Issue。

用户：
最终选择 Y。

实验：
Y 提升 18%。
```

不能自动写成：

```text
X = failed
```

应该保留来源语义：

```text
Opinion
Decision
Evidence
Result
```

---

# 52. Compaction 与 Project Memory 的区别

## Compaction

解决：

```text
当前 Session 太长
```

输出：

```text
Compressed History
```

## Project Memory

解决：

```text
未来 Session 也要复用过去的重要知识
```

输出：

```text
Confirmed Project Memory
```

因此：

```text
Compaction ≠ Project Memory
```

---

# 53. Summary 与 Project Memory 的区别

这是 Phase 4 必须明确的第三层区分。

## Conversation Summary

回答：

> **“这个模型到目前为止是怎么理解这个问题的？”**

允许：

```text
观点
推测
争议
未验证假设
当前倾向
```

## Project Memory

回答：

> **“这个项目长期确认下来，哪些东西值得以后直接复用？”**

要求更高：

```text
事实
实验结果
决策
长期经验
证据
```

因此：

```text
Summary
不能自动变成
Project Memory
```

---

# 54. 旧 HISTORY_WINDOW=3 的处理

Phase 1–3：

```text
HISTORY_WINDOW=3
```

只是早期 baseline。

Phase 4：

> **不得再作为正式历史可见性硬上限。**

禁止：

```text
超过 3 轮
→ 自动丢弃
```

第一版甚至可以直接删除该常量。

如果代码中必须保留：

```text
RECENT_RAW_TARGET
```

也不能将其解释为：

```text
hard limit
```

---

# 55. k 的前端设计

前端显示：

```text
无损历史保留比例 k
```

范围：

```text
0.01 - 0.90
```

说明：

```text
k 越高：
更多历史以原文保留，
压缩区空间越小。

k 越低：
更少历史保留原文，
压缩区空间越大。
```

每次真正执行 Round 时记录：

```json
{
  "event": "context_policy",
  "round_id": 20,
  "k": 0.5
}
```

---

# 56. Summary UI 的日志

用户打开 / 关闭 Summary 开关不等于已经生成 Summary。

只有真正发送 Summary 请求时：

```text
生成 conversation_summary event
```

如果用户：

```text
勾选 Summary
→ 修改 Prompt
→ 取消发送
```

不生成 Summary Runtime。

如需要记录 UI 行为，可以单独记录：

```text
summary_mode_enabled
summary_mode_disabled
```

但第一版不是必须项。

---

# 57. Context Budget 日志

每次真正建立 LLM Prompt 必须记录至少：

```json
{
  "event": "context_budget",
  "round_id": 20,
  "model_id": "model_a",

  "context_limit": 131072,

  "model_max_output": 32768,
  "request_max_output": 16384,
  "reserved_output_tokens": 16384,

  "fixed_input_tokens": 20000,
  "history_budget": 92160,

  "k": 0.5,
  "lossless_budget": 46080,
  "compaction_budget": 46080,

  "pinned_tokens": 18000,
  "recent_raw_tokens": 27000,
  "compacted_tokens": 21000
}
```

必须能回答：

> 这一轮为什么保留这些历史？

以及：

> 为什么没有更多历史进入 Context？

---

# 58. Token 估算日志

每个主要 Token 值必须尽可能记录：

```text
value
source
accuracy_mode
```

例如：

```json
{
  "fixed_input_tokens": 20000,
  "token_source": "provider_tokenizer",
  "token_exactness": "estimated"
}
```

或：

```json
{
  "tool_tokens": null,
  "token_source": "provider_unavailable",
  "token_exactness": "unknown"
}
```

不得把估算值伪装为精确值。

---

# 59. History Selection 日志

每轮必须记录：

```json
{
  "event": "history_selection",
  "round_id": 20,

  "current_user_tokens": 1000,

  "pinned_rounds": [1,7],

  "selected_rounds": [19,18,17,16],

  "peer_context_source": {
    "model_b": "conversation_summary",
    "model_c": "final_answer"
  },

  "dropped_rounds": [15,14,13],

  "reason": "lossless_budget"
}
```

其中：

```text
dropped_rounds
```

表示：

> 本轮没有进入无损 Context。

不表示原始数据被删除。

---

# 60. Compaction 日志

每次 Compaction 必须记录：

```json
{
  "event": "compaction",
  "round_id": 20,
  "source_rounds": [1,2,3,4,5,6,7,8],
  "input_tokens": 42000,
  "expected_summary_tokens": 11000,
  "actual_summary_tokens": 9600,
  "summary_file": "compact_001.md",
  "attempt": 1,
  "status": "success"
}
```

第二次：

```json
{
  "event": "compaction",
  "round_id": 20,
  "source": "compact_001.md + older history",
  "input_tokens": 18000,
  "actual_summary_tokens": 6200,
  "attempt": 2,
  "status": "success"
}
```

---

# 61. 所有关键行为必须机器可读

至少记录：

```text
1. k
2. context_limit
3. model_max_output
4. request_max_output
5. reserved_output_tokens
6. fixed_input_tokens
7. history_budget
8. lossless_budget
9. compaction_budget
10. Pin 状态变化
11. Pin 占用 Token
12. History Selection
13. 哪些 Round 未进入 Context
14. Summary 是否执行
15. Summary 触发来源
16. Summary 输入/输出 Token
17. Peer Context 采用 Final Answer 还是 Summary
18. Compaction 输入 Token
19. Compaction 输出 Token
20. Compaction 次数
21. Compaction 文件
22. MCP Runtime
23. MCP Evidence Index
24. 失败 Round
25. Context Overflow
```

要求：

> **用户界面看到的状态必须可以由这些日志解释。**

---

# 62. 推荐目录结构

保持简单：

```text
sessions/

└── <session_id>/

    ├── round_001/
    │   ├── prompt.md
    │   ├── responses/
    │   ├── summary/
    │   └── mcp_runtime.jsonl

    ├── round_002/
    │   ├── ...
    │
    ├── summaries/
    │   ├── model_a/
    │   │   ├── summary_001.md
    │   │   └── summary_002.md
    │   │
    │   └── model_b/
    │       └── summary_001.md
    │
    ├── compact_001.md
    ├── compact_002.md
    └── context_policy.jsonl
```

Project Memory：

```text
projects/

└── <project_id>/

    └── memory/

        ├── memory_001.md
        ├── memory_002.md
        └── memory_index.jsonl
```

第一版不强制数据库。

---

# 63. 最终 Context 的典型结构

例如当前：

```text
R20
```

某模型请求可能包含：

```text
System
+
Tools
+
Current R20 User Question
+
Pinned Rounds
+
Recent Valid R19 Q&A
+
Recent Valid R18 Q&A
+
Recent Valid R17 Q&A
+
Peer Context
    ├── Model B Summary
    └── Model C Final Answer
+
Compacted History
+
必要 MCP Evidence
```

然后为：

```text
Reserved Output
```

留下预算。

必须满足：

```text
Input
+
Reserved Output
≤
Context Limit
```

---

# 64. 最终历史选择算法

```text
Raw Archive
    ↓
过滤无有效回答的失败 Round
    ↓
得到 Valid Round Pool
    ↓
确定当前 Model
    ↓
获取 Provider Model Metadata
    ↓
确定 Context Limit
    ↓
确定 Request Output Limit
    ↓
计算 Reserved Output
    ↓
计算 Fixed Input
    ↓
计算 History Budget
    ↓
读取 k
    ↓
计算 Lossless Budget
    ↓
读取 Pin
    ↓
加入 Pin
    ↓
从最新 Valid Round 向旧倒序加入完整 Q&A
    ↓
超出 Lossless Budget 的旧历史进入 Compaction 区
    ↓
判断 Compacted History 是否已有可用结果
    ↓
必要时执行 Compaction
    ↓
Peer Review = ON？
    ↓
检查其他 ACTIVE 模型有效回答数
    ↓
单个有效回答
    → 直接使用 Final Answer
    ↓
多个有效回答
    → 自动执行 Conversation Summary
    ↓
生成 Peer Context
    ↓
加入当前模型 Context
    ↓
最终检查 Input + Reserved Output
    ↓
必要时 Recursive Compaction
    ↓
发送 LLM
```

---

# 65. Peer Review 完整流程

```text
Current Round
    ↓
用户问题
    ↓
选择 ACTIVE Models
    ↓
Peer Review = ON
    ↓
收集其他模型历史状态
    ↓
┌──────────────────────────────┐
│ 每个其他模型分别判断          │
│                              │
│ 有效回答 = 1                 │
│ → 直接 Final Answer          │
│                              │
│ 有效回答 > 1                │
│ → 自动执行 Summary           │
│ → 使用最新 Conversation Summary│
└──────────────────────────────┘
    ↓
组成 Peer Context
    ↓
当前模型获得 Peer Context
    ↓
当前模型生成本轮 Final Answer
```

Peer Review 结果：

```text
留在当前 Round
```

但：

```text
不能自动成为下一轮 Peer Context 来源
```

---

# 66. Summary 主动功能完整流程

```text
用户勾选：

[✓ Summary]

        ↓

输入框自动清空

        ↓

填入默认 Summary Prompt

        ↓

用户可修改

        ↓

发送

        ↓

模型读取当前可用历史

        ↓

生成 Conversation Summary

        ↓

保存 summary_XXX.md

        ↓

成为该模型最新 Summary

        ↓

后续普通对话可继续使用
```

---

# 67. 第一版明确不做

第一版不实现：

* 自动重要性评分；
* 自动永久保留 R1；
* 自动删除有效历史；
* 自动永久写入 Project Memory；
* 自动把 MCP Result 当 Fact；
* 自动把 Summary 当 Fact；
* Memory Graph；
* 复杂 Vector Database；
* Memory Agent；
* 复杂 Ranking；
* Memory Benchmark；
* 每次 MCP 调用额外调用 LLM；
* Peer Review 结果自动写回其他模型 Summary；
* Peer Review 结果自动传播到下一次 Peer Review；
* 无限递归 Compaction；
* 强制所有 Summary 使用固定知识库字段；
* 为 Summary / Peer Review 再建立另一套独立 Memory Framework。

---

# 68. Phase 4 开发顺序

## Phase 4.1：Context Selection

先实现：

```text
Valid Round
+
完整 User + Answer
+
Pin
+
k
+
Context Budget
+
Provider Output Metadata
+
最近历史尽可能装入
```

证明：

> **没有超出预算时，系统确实尽可能保留完整有效历史。**

---

## Phase 4.2：Summary UI + Conversation Summary

实现：

```text
Summary 开关
+
自动填充 Prompt
+
用户可编辑 Prompt
+
Summary 结果保存
+
latest Summary
```

证明：

> **用户可以用最少输入快速整理某个模型当前完整对话状态。**

---

## Phase 4.3：Peer Review Preparation

实现：

```text
Peer Review ON
+
判断其他模型有效回答数
+
1 个 → Final Answer
+
>1 个 → 自动 Summary
+
最新 Summary → Peer Context
```

证明：

> **互评不再依赖固定的“上一轮 Final Answer”，而是根据历史长度选择合适的信息表示。**

---

## Phase 4.4：Compaction

实现：

```text
超过 Lossless Budget
↓
旧历史 Summary
↓
Summary + 无损历史
```

加入：

```text
Recursive Compaction
```

证明：

> **历史过长时不会直接丢弃，而是转换为压缩表示。**

---
## Phase 4.5：MCP Evidence Index

实现：

```text
MCP Runtime
    +
Final Answer
    ↓
Evidence Index
    ↓
历史 Context 中按需引用 Evidence
```

目标：

> **MCP 原始结果可追溯，同时可以知道哪些外部结果实际被 Final Answer 使用；当对应历史 Round 已离开无损历史区后，可以按需将其 Evidence 的 reference 纳入当前 Context。**

### 4.5.1 Evidence 与 Runtime 的关系

MCP Runtime 是原始运行记录，Evidence Index 是从 Runtime + Final Answer 中提取出的“实际被回答使用的外部证据”索引。

必须区分：

```text
MCP Tool Call
    ≠
MCP Tool Result
    ≠
MCP Evidence
    ≠
Fact
```

其中：

* MCP Runtime：保留完整原始调用与返回记录。
* MCP Tool Result：工具实际返回的原始结果。
* MCP Evidence：能够确认被 Final Answer 实际引用 / 使用的外部结果。
* Fact：已经被系统确认的项目事实。

**MCP Result / Evidence 均不得自动视为 Fact。**

### 4.5.2 Evidence 生成条件

Evidence Index 在 Round 完成后生成。

Evidence 必须满足：

```text
Round 已完成
+
Final Answer 成功
+
对应 MCP Tool Result 实际被 Final Answer 使用
```

Evidence Index 至少需要能够：

* 定位对应 Session；
* 定位对应 Round；
* 定位对应模型；
* 定位对应 MCP Runtime 记录；
* 标记该结果是否实际被 Final Answer 使用；
* 保留必要的来源 / reference 信息。

失败的 MCP Tool Call / Result 不得作为正常 Evidence 注入 Context。

### 4.5.3 Evidence → Context 范围

Phase 4.5 第一版采用 **A1：当前生成模型自己的历史 Evidence**。

即：

```text
当前正在生成 Final Answer 的 Model
        ↓
仅读取该 Model 自己的历史 Evidence
```

不得在第一版中：

* 自动读取其他模型的 Evidence；
* 将 Evidence 扩展为 Peer Context；
* 将所有模型的 Evidence 聚合后注入；
* 引入 Evidence ranking / embedding / RAG；
* 引入 Project Memory M3。

### 4.5.4 只允许历史已完成 Round

Evidence 在 Round 完成后生成，因此当前正在执行的 Round 不可能反向注入自身 Prompt。

因此 Evidence → Context 只针对：

> **已经完成的历史 Round。**

当前 Round 的 MCP Runtime / Tool Result 仍按当前 Round 的正常运行流程处理，不作为当前 Prompt 的历史 Evidence。

### 4.5.5 Raw History 与 Evidence 去重

Evidence 不是 Raw History 的替代品，而是历史信息离开无损区后的可追溯补充。

如果某个历史 Round 的完整 User Question + Final Answer 仍然处于当前 Lossless Context：

```text
Raw History 在 Context
        ↓
不再重复注入对应 Evidence
```

如果该历史 Round 已离开当前 Lossless History：

```text
Raw History 不再直接进入 Context
        ↓
如果该 Round 存在符合条件的 Evidence
        ↓
允许注入 Evidence Reference
```

Pinned Round 已经受到无损保护，因此：

```text
Pinned Raw History
        ↓
不需要额外注入对应 Evidence
```

### 4.5.6 Evidence 第一版只使用 Reference

Phase 4.5 第一版：

> **只向模型提供 Evidence Reference，不直接注入完整 MCP Runtime 原始结果。**

即：

```text
Runtime 原始记录
    ↓
内部保留

Evidence Index
    ↓
生成可追溯 Reference

Reference
    ↓
按需进入模型 Context
```

Runtime 原始 Tool Result 继续保存在内部 Runtime Log 中，不因为 Evidence → Context 而复制到 Prompt。

Evidence Reference 应能够让系统内部重新定位：

```text
Session
Round
Model
MCP Tool / Runtime Record
```

但模型可见内容不得暴露真实 `source_model_id`。

### 4.5.7 Evidence 与 Compaction Budget

Evidence 不建立独立的 Context Budget。

继续使用：

```text
FixedInputTokens
    =
System
+ Tools
+ Current User
+ Peer Context Reserve

HistoryBudget
    =
ContextLimit
- FixedInputTokens
- ReservedOutput

LosslessBudget
    =
HistoryBudget × k

CompactionBudget
    =
HistoryBudget × (1-k)
```

其中：

```text
Compacted History
+
MCP Evidence Reference
≤
CompactionBudget
```

即：

> **MCP Evidence Reference 与 Compacted History 共用压缩区预算。**

不得因为加入 Evidence 而修改 `k` 的定义，也不得在第一版中增加独立的 Evidence Budget。

### 4.5.8 Evidence Budget 不足

如果当前 `CompactionBudget` 已经不足以容纳某个 Evidence Reference：

```text
跳过该 Evidence
```

而不是：

* 删除 Runtime；
* 删除 Evidence Index；
* 修改 Raw Archive；
* 自动取消 Pin；
* 强制压缩 Pin；
* 修改 k；
* 破坏已有 Compacted History。

可以记录：

```text
mcp_evidence_skipped_for_budget
```

用于解释为什么该 Evidence 没有进入当前 Context。

Evidence 被跳过不等同于 Context Overflow。

### 4.5.9 Evidence 与 Peer Review

Evidence → Context 第一版不改变 Phase 4.3 Peer Review 规则。

Evidence：

* 不自动成为 Peer Context；
* 不自动成为下一次 Peer Review 输入；
* 不修改其他模型 Summary；
* 不因为 Peer Review 而扩大到其他模型的历史 Evidence。

### 4.5.10 Evidence 与 Project Memory

Evidence 不自动写入 Project Memory。

即：

```text
MCP Evidence
    ≠
Project Memory
```

如果未来需要把 Evidence 转换为 Project Memory，仍必须遵守：

```text
Candidate Memory
    ↓
User Confirm
    ↓
Project Memory
```

不得因为 Evidence 已经被 Final Answer 使用，就自动认为它已经是确认事实。

### 4.5.11 Identity / Anonymization

Evidence 进入模型 Prompt 前必须遵守 Phase 4 的模型匿名化规则。

模型可见内容：

```text
不得暴露真实 model_id
```

内部 provenance 可以保留：

```text
source_model_id
runtime_record_id
round_id
```

但这些内部字段不得直接成为模型可见身份信息。

Evidence Reference 中的外部文本也必须经过必要的身份泄漏检查。

检查对象是 Reference 正文（外部工具文本）；定位行等系统生成的骨架由既有 A3 硬校验负责。

命中真实身份词元时：

```text
Evidence Reference 命中真实身份词元
        ↓
不得注入模型 Prompt
        ↓
整条跳过该 Evidence
        ↓
记录 mcp_evidence_skipped_for_identity
```

跳过只作用于“本次是否进入 Context”，不删除任何原始档案：

```text
不删除 MCP Runtime
不删除 Evidence Index
```

被跳过的 Evidence 仍然可以通过内部 provenance 追溯到原始 Runtime。

身份过滤只约束模型可见内容，不改变内部 provenance：被跳过的 Evidence 在索引中继续保存真实 `source_model_id` 与 `runtime_record_id`，只是这部分内容不进入 Prompt。

### 4.5.12 与 Compaction 的关系

Evidence 不替代 Compaction。

两者职责：

```text
Compaction
    ↓
压缩较旧的历史 Q&A

Evidence
    ↓
为已经离开无损区的历史 MCP 外部证据
提供可追溯 Reference
```

Compaction 不删除 MCP Runtime。

MCP Runtime 仍然保留在原始运行记录中。

Evidence Reference 可以与 Compacted History 同时存在，但必须遵守共同的 `CompactionBudget`。

---

## Phase 4.6：Project Memory

之后再做：

```text
Candidate Memory
        ↓
User Confirm
        ↓
Project Memory
```

暂不提前实现 Project Memory M3 Retrieval / Context Injection。

Phase 4.5 Evidence → Context 不得绕过 User Confirm 机制直接写入 Project Memory。

---

# 69. 验收标准

## Phase 4.1 / Context Selection

```text
✅ 失败 Round 不进入正常 Context

✅ 有效 Round = User + 成功 Final Answer

✅ 不再因为固定 3 轮直接丢弃历史

✅ Pin 保持无损优先级

✅ k 可以控制无损 / 压缩预算

✅ Context Budget 考虑 Reserved Output

✅ Reserved Output 来自模型能力 + 本次请求配置

✅ Token 来源 / 估算状态有日志

✅ 所有预算计算有日志
```

## Phase 4.2 / Summary

```text
✅ UI 有独立 Summary 开关

✅ 开启后输入框自动填充默认 Summary Prompt

✅ 用户可以直接发送

✅ 用户可以修改 Prompt

✅ Summary 独立保存

✅ Summary 不自动把观点变成 Fact

✅ Summary 可以表达未经验证的观点 / 假设 / 争议

✅ 最新成功 Summary 可以作为后续工作状态
```

## Phase 4.3 / Peer Review

```text
✅ Peer Review 可以单独开关

✅ 只有一个有效回答时直接使用 Final Answer

✅ 多个有效回答时自动执行 Summary

✅ Peer Context 使用最新 Summary

✅ 不需要用户重新输入 Summary Prompt

✅ Peer Review 结果不写回其他模型 Summary

✅ Peer Review 结果不作为下一次 Peer Review 的自动输入
```

## Phase 4.4 / Compaction

```text
✅ 超出无损预算后进入压缩区

✅ Raw Archive 不删除

✅ Summary / Compaction 可落盘

✅ Summary + 无损历史不超过预算

✅ 超限可以再次压缩

✅ Compaction 失败不会直接破坏原始历史

✅ Compaction 有完整日志

✅ Compacted History 不向模型暴露真实 model_id

✅ Compaction 递归过程中保持身份匿名

✅ 检测到 Compaction 身份泄漏时不落盘、不注入
```

## Phase 4.5 / MCP Evidence

```text
✅ MCP Runtime 保留

✅ 可以区分“调用过”和“Final Answer 使用过”

✅ Evidence Index 可以定位 Runtime

✅ MCP Result 不自动成为 Fact

✅ Evidence Index 有日志

✅ Evidence 只来自已完成的历史 Round

✅ 第一版只注入当前模型自己的历史 Evidence

✅ Raw History 仍在无损 Context 时，不重复注入对应 Evidence

✅ Pin 不需要额外注入 Evidence

✅ Evidence 第一版只以 Reference 形式进入 Context

✅ Runtime 原始 Tool Result 不直接复制进 Prompt

✅ Evidence Reference 与 Compacted History 共用 CompactionBudget

✅ Evidence 不建立独立 Budget

✅ Budget 不足时可以跳过 Evidence，不删除 Runtime / Index

✅ Evidence 不自动进入 Peer Context

✅ Evidence 不自动进入 Project Memory

✅ Evidence 模型可见内容不暴露真实 model_id

✅ Evidence Reference 命中真实身份词元时整条跳过，并记录 mcp_evidence_skipped_for_identity

✅ 被身份过滤跳过的 Evidence 不删除 MCP Runtime / Evidence Index，且仍可追溯

✅ Evidence 可以追溯到 Session / Round / Runtime Record

✅ Evidence 进入 Prompt 前通过身份泄漏检查
```


---

# 70. 最终架构

```text
                           CrossLLM
                               │
             ┌─────────────────┼─────────────────┐
             │                 │                 │
             ↓                 ↓                 ↓
      Current Context     Raw Archive      Model Working State
             │                 │                 │
             │                 │          Conversation Summary
             │                 │                 │
      ┌──────┼──────┐          │                 │
      │      │      │          │                 │
   Current  Pin   Recent       │                 │
    User          Valid        │                 │
                  Rounds       │                 │
                     │         │                 │
                     └────┬────┘                 │
                          ↓                      │
                   Context Budget               │
                          │                      │
                   ┌──────┴──────┐              │
                   │             │              │
                放得下          放不下          │
                   │             │              │
                   ↓             ↓              │
                Raw 保留     Compaction         │
                                 │              │
                                 ↓              │
                              Summary            │
                                                │
                    ┌───────────────────────────┘
                    │
                    ↓
             Peer Context Preparation
                    │
          ┌─────────┴─────────┐
          │                   │
      1 Valid Answer      >1 Valid Answer
          │                   │
          ↓                   ↓
     Final Answer      Conversation Summary
          │                   │
          └─────────┬─────────┘
                    ↓
               Peer Context
                    │
                    ↓
              Current Model
                    │
                    ↓
               Final Answer
```

MCP：

```text
MCP Runtime
     │
     ├── Raw Tool Call / Result
     │
     └── Evidence Index
             │
             ├───────────────────────────────┐
             │                               │
             ↓                               ↓
        追溯路径                          注入路径
   Evidence → Runtime              Evidence Reference
   原始 Tool Result                （第一版只注入 Reference，
   （必要时定位原始证据）             按需注入；与 Compacted History
                                    共用 CompactionBudget）
                                          │
                                          ↓
                                    Current Context
```

Project Memory 独立：

```text
Multiple Sessions
       ↓
Candidate Memory
       ↓
User Confirmation
       ↓
Project Memory
```

---

# 71. 最终原则

CrossLLM 的目标不是：

> **让模型永远看到更多历史。**

而是：

> **在有限 Context 中，以最低的信息损失保留真正有价值的历史，并且让模型自己的当前工作状态、跨模型交流、外部证据和长期项目知识彼此隔离。**

核心关系：

```text
Raw Archive
    =
完整原始事实来源

Conversation Summary
    =
某个模型当前工作状态

Peer Context
    =
当前跨模型交流材料

Compaction
    =
Context 压缩机制

MCP Evidence Index
    =
外部证据索引

Project Memory
    =
经确认的跨 Session 长期知识
```

优先级：

```text
Current User
>
Pinned History
>
Recent Valid Q&A
>
必要 Peer Context
>
Compacted History
```

同时：

```text
失败 Round
→ 不占正常 Context

MCP Runtime
→ 原始档案保留

MCP Evidence Index
→ 只做证据索引，不替代 Runtime

Conversation Summary
→ 不自动等于 Fact

Peer Review Result
→ 不进入下一次 Peer Review 输入

Project Memory
→ 独立于当前 Session

Raw Archive
→ 不因 Compaction 删除
```

第一版优先：

> **简单、低成本、可解释、可恢复、可追溯、少做一层就少做一层。**

任何新增策略必须回答：

```text
为什么需要？

解决什么实际问题？

是否已经有成熟项目验证？

为什么现有规则不够？

是否可以复用已有机制，而不是重新发明？
```
