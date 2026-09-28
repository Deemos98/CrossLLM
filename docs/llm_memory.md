# CrossLLM LLM Memory 机制设计

## 1. 目的

CrossLLM 的 LLM Memory 用于解决长期多轮对话中的上下文膨胀问题。

目标不是复刻 ChatGPT、TRAE 或完整 Agent Memory 系统，而是：

> **尽可能保留历史信息，同时控制发送给 LLM 的上下文长度。**

核心原则：

```text
原始历史永远保留
        ↓
近期历史优先原文保留
        ↓
历史过长时压缩较早内容
        ↓
压缩结果单独保存
        ↓
后续请求使用“近期原文 + 压缩记忆”
```

---

# 2. Memory 与当前对话的关系

CrossLLM 的上下文分为两类：

### Self History

当前模型自己的历史对话。

无论是否开启互评，都可以作为普通连续对话上下文。

### Peer History

其他模型的最终回答。

只有用户开启“互评”时才进入当前模型上下文。

因此：

```text
互评 OFF：

当前模型
= 自己的历史
+ 当前用户输入

互评 ON：

当前模型
= 自己的历史
+ 其他 ACTIVE 模型的历史回答
+ 当前用户输入
```

Peer History 仍然遵循相同的历史长度控制规则。

---

# 3. 第一版工作上下文

第一版采用简单的有限历史窗口。

默认：

```text
最近 N 轮原文
```

当前测试阶段可以使用：

```text
N = 3
```

但：

> **3 不是经过实验得出的理论最优值，只是第一版 baseline。**

未来根据实际使用情况可以调整为 5、10 或动态窗口。

因此代码中不要把 `3` 写死在多个地方，应集中为一个配置常量。

---

# 4. 为什么优先保留原文

只要上下文没有接近模型限制：

> **优先直接使用原文。**

因为原文的信息损失为零。

例如：

```text
用户：
测试 threshold=0.1、0.05、0.01

模型：
0.01 漏检降低，但误检明显增加。

用户：
后来加入 FSMN 后，漏词下降约 18%。
```

原文比摘要更可靠。

因此不要为了“Memory”每轮都调用 LLM 做摘要。

---

# 5. Markdown 与原文格式

保存模型回答时：

> **原样保存。**

不要修改模型真实输出。

原因：

* 便于回溯；
* 便于调试；
* 便于重新压缩；
* 便于人工核对；
* 避免压缩过程损坏代码。

---

# 6. Context 传输前的轻量整理

模型回答进入其他模型 Context 前，可以进行非常轻量的格式整理。

允许：

* 删除连续多余空行；
* 删除明显无意义的尾部空白；
* 统一换行符；
* 保留 Markdown 标题；
* 保留 Markdown 列表；
* 保留代码块；
* 保留代码缩进；
* 保留代码换行。

禁止：

* 把完整代码强制压成一行；
* 删除 Markdown 结构；
* 改写模型原文；
* 修改代码语义；
* 根据字符数量随意删词。

原则：

> **减少无意义空白，而不是压缩有语义的信息。**

---

# 7. 为什么不直接删除 Markdown 或所有换行

Markdown 标记、空格、换行确实可能占用 token，但通常不是主要成本来源。

真正占用大量 token 的通常是：

```text
历史回答正文
长代码
搜索结果
工具返回内容
多轮上下文
```

因此：

```text
删除几十个 #
```

通常没有实际意义。

相反：

```text
删除大量有意义的历史内容
```

会直接损失模型理解能力。

所以 Context 优化优先级为：

```text
减少无关历史
>
压缩重复内容
>
语义压缩旧历史
>
最后才考虑空白字符优化
```

---

# 8. Context 长度判断

Context 构建前应估算当前输入长度。

目标不是得到精确账单，而是避免明显超过模型 Context Limit。

优先使用模型配置中的：

```text
context_limit
```

发送前：

```text
estimated_input_tokens
```

用于保护。

---

# 9. 为什么不能只看字符数

不同模型 tokenizer 不同。

例如：

```text
中文
English
代码
数字
Markdown
```

token 化方式可能不同。

因此第一版：

> **只允许粗略估算，不把字符数当精确 token 数。**

如果 Provider 可以返回真实 usage：

```json
{
  "input_tokens": 18240,
  "output_tokens": 2138
}
```

则真实 usage 优先用于记录和后续优化。

---

# 10. 历史压缩触发条件

第一版不按固定轮数强制压缩。

原则：

```text
当前 Context 没有明显接近上限
→ 不压缩

接近 Context 上限
→ 触发 Compaction
```

这意味着：

```text
3 轮、5 轮、10 轮
```

只是历史窗口参数，而：

```text
是否进行压缩
```

主要由 Context Token Budget 决定。

---

# 11. Compaction 基本策略

采用类似 Aider 的：

> **旧历史压缩 + 最近历史原文保留**

基本流程：

```text
历史：

R1 R2 R3 R4 R5 R6 R7 R8 R9 R10

                ↓

保留最近：

R8 R9 R10

较早历史：

R1~R7

                ↓

LLM 压缩

                ↓

Summary(R1~R7)
+
R8 R9 R10
```

如果压缩后仍然超过 Context Limit：

```text
Summary + R8 + R9 + R10

        ↓

仍超限

        ↓

进一步压缩旧 Summary
或减少更旧原文
```

但第一版不要求实现复杂递归策略。

---

# 12. 为什么压缩本身可以节省 Token

压缩会产生一次额外的 LLM 调用：

```text
旧历史
→ LLM Summary
```

所以压缩本身会产生额外 token 成本。

但如果这份历史以后会被重复使用很多次：

```text
不压缩：

100K
100K
100K
100K
...

压缩后：

5K
5K
5K
5K
...
```

那么一次压缩成本可能被后续大量请求节省的 Input Token 抵消。

因此：

> **不能每轮压缩。**

只有历史接近 Context 上限时才压缩。

---

# 13. Compaction 的输出格式

第一版采用结构化 Markdown，而不是自由散文。

示例：

```markdown
# Memory Summary

## Topic
ASR VAD 漏词问题

## Facts
- Silero VAD 在部分场景存在漏检。
- 已测试多个 threshold。

## Solutions
- 降低 threshold
- 引入 FSMN 补漏

## Experiments
- threshold=0.01：漏检下降，但误检增加。
- 加入 FSMN 后：漏词下降约 18%。

## Decisions
- 当前采用 Silero + FSMN。

## Rejected
- 仅降低 threshold：误检增加。

## Open Questions
- 复杂噪声环境下仍需继续验证。

## Reusable
- unknown
```

---

# 14. Memory 字段

压缩结果至少包含：

```text
id
time
topic
solution_type
solution
result
reusable
rejection_reason
```

推荐完整结构：

```text
id
time
topic
facts
solutions
experiments
decision
result
reusable
rejection_reason
open_questions
```

---

# 15. `result` 的定义

不要让 LLM 随意把“模型认为合理”写成“已经有效”。

建议使用：

```text
validated
failed
unknown
```

其中：

### validated

必须存在现实证据，例如：

* 用户明确反馈有效；
* 程序实际运行成功；
* 数据测试有效；
* Benchmark 有结果。

### failed

存在明确证据：

* 用户测试失败；
* 程序报错；
* 数据结果明显变差；
* 实验否定方案。

### unknown

只有讨论，没有现实验证。

例如：

```text
LLM：
“我认为方案 A 很合理。”
```

只能：

```text
result = unknown
```

不能：

```text
result = validated
```

---

# 16. `reusable` 的处理

第一版不要求自动准确判断。

允许：

```text
yes
no
unknown
```

默认：

```text
unknown
```

原因：

> 一个方案是否可以复用，通常依赖具体项目环境，单纯让摘要模型判断容易误导。

后续可以根据实际使用数据再设计判断规则。

---

# 17. “方案意见”与“实验结论”必须区分

这是 Memory 最重要的可靠性原则之一。

例如：

```text
模型 A：
方案 X 很合理。

模型 B：
方案 X 可能有问题。

用户：
我最后采用方案 Y。

实验：
Y 在测试集提升 18%。
```

Memory 不应变成：

```text
方案 X 有效。
```

而应保留：

```text
Opinion：
A 认为 X 合理。

Decision：
用户最终采用 Y。

Evidence：
Y 测试集提升 18%。

Result：
validated
```

原则：

```text
LLM Opinion
≠
User Decision
≠
Experimental Evidence
```

---

# 18. 原始历史永远保留

压缩不会删除原始历史。

例如：

```text
history/
├── round_001.md
├── round_002.md
├── ...
├── round_020.md
```

另外生成：

```text
memory/
├── summary_001.md
├── summary_002.md
```

压缩结果属于：

> **原始历史的派生数据。**

如果未来发现摘要错误，可以重新生成。

---

# 19. Memory 的使用

当前请求构建 Context 时：

```text
当前用户问题
+
最近 N 轮原文
+
相关 Memory（未来实现）
```

第一版可以暂时不做 Memory Retrieval。

真正的长期检索属于后续阶段。

---

# 20. 建议的演进路线

## Phase A：当前

```text
最近 3 轮原文
```

目标：

> 先跑通。

## Phase B：Context Compaction

```text
最近若干轮原文
+
旧历史 LLM Summary
```

触发：

```text
Context 接近上限
```

目标：

> 解决长对话。

## Phase C：Project Memory

```text
Facts
Decisions
Experiments
Rejected
Open Questions
```

目标：

> 支持长期 ASR / MTU 项目记忆。

## Phase D：历史检索

```text
当前问题
↓
BM25 / Embedding
↓
相关历史
↓
Memory + Raw History
```

目标：

> 找回几周或几个月以前与当前问题相关的信息。

---

# 21. 第一版不做

明确不实现：

* EMA 时间衰减；
* 按时间直接删除历史；
* 每轮自动摘要；
* 每轮自动提取长期 Memory；
* 自动判断方案正确性；
* 自动判断方案可复用性；
* 自动删除旧 Memory；
* 复杂 tokenizer 系统；
* 注意力权重分析；
* reasoning 过程评估；
* 搜索质量评估；
* MCP 过程评估；
* Tool-use 过程评估。

---

# 22. 参考开源项目

## Aider

用途：

> AI Coding 工具。

参考：

```text
aider/history.py
```

重点学习：

```text
Token Budget
→
检测过长
→
旧历史摘要
→
近期历史保留
```

这是本项目最直接的 Context Compaction 参考。

Aider 当前采用 `ChatSummary` 管理历史摘要，并以 token budget 作为是否压缩的重要依据。
Repository：`https://github.com/Aider-AI/aider`

许可证：

```text
Apache-2.0
```

---

## OpenAI Agents SDK

用途：

> Agent 开发 SDK。

参考：

```text
src/agents/memory/session.py
OpenAIResponsesCompactionSession
```

重点学习：

```text
Session
→
保存 / 读取历史

Compaction
→
达到条件
→
压缩历史
```

这里值得借鉴的是：

> **Session 与 Compaction 分离。**

本项目不需要直接引入 OpenAI Agents SDK。

Repository：

`https://github.com/openai/openai-agents-python`

许可证：

```text
MIT
```

---

## Mem0

用途：

> 长期 AI Memory 层。

重点学习：

```text
add()
→
LLM extraction
→
Memory
→
search()
→
Retrieval
```

主要参考：

* 什么信息值得长期保存；
* 如何把对话转换成 Fact / Decision 等结构化 Memory；
* 如何按需取回长期信息。

本项目第一版不直接引入 Mem0。

Repository：

`https://github.com/mem0ai/mem0`

许可证：

```text
Apache-2.0
```

---

# 23. CrossLLM 最终 Memory 架构

长期目标：

```text
                         当前用户问题
                              │
             ┌────────────────┼────────────────┐
             ↓                ↓                ↓
       Recent Raw        Project Memory      Raw Archive
       最近历史原文        结构化长期记忆       全部原始历史
             │                │                │
             │                │                └── 按需检索
             │                │
             └────────────────┴────────────────┐
                                                ↓
                                           Context Builder
                                                ↓
                                               LLM
```

其中：

```text
Recent Raw
```

解决：

> 当前连续对话。

```text
Compacted Summary
```

解决：

> 历史太长。

```text
Project Memory
```

解决：

> 长期重要事实、决定和实验结果。

```text
Raw Archive
```

解决：

> 以后发现原摘要遗漏时，可以回到原始资料。

---

# 24. 最终原则

CrossLLM Memory 的核心不是：

> **“让模型记住尽可能多的内容。”**

而是：

> **“让模型在有限 Context 中看到尽可能有价值的内容。”**

因此优先级：

```text
原文
↓
近期原文优先

太长
↓
压缩旧历史

长期重要信息
↓
结构化 Memory

很久以前的详细内容
↓
原始历史按需检索
```

任何压缩、提取或检索都不得删除原始历史。

第一版优先简单、可解释、可验证；只有实际使用出现 Context 不足或 Memory 不够用时，再增加下一层机制。
