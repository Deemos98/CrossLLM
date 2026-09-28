<br />

````markdown
# CrossLLM Phase 1：Mock Core Loop
## Version: v0.1

## 1. 任务

本阶段开始实现 CrossLLM。

必须先阅读：

```text
docs/architecture_v0.1.md
docs/phase1_mock.md
reference_analysis.md
````

其中：

- `architecture_v0.1.md`：项目总规范，优先级最高。
- `reference_analysis.md`：Phase 0 源码分析结果。
- `phase1_mock.md`：本阶段具体任务。

本阶段目标：

>使用 3 个 Mock Model 跑通：

并发回答 → 持久化 → 用户关闭模型 → 下一轮 → 读取上一轮回答 → 匿名交叉注入 → 再回答

的完整闭环。

Phase 1 的目的不是验证真实 LLM 的回答质量，而是验证 CrossLLM 的核心状态、Session、Model 生命周期、Round、持久化以及跨模型信息传递架构是否成立。

***

# 2. 本阶段技术范围

使用：

```text
Python
FastAPI
asyncio
SSE
Vanilla HTML/CSS/JS
SQLite（仅在确有必要时）
Filesystem
Markdown
JSON
```

允许使用的第三方依赖保持最少。

本阶段不使用：

```text
LangGraph
AutoGen
CrewAI
LiteLLM
MCP
Google
Bing
Yandex
GitHub MCP
Zhihu MCP
真实 LLM API
```

***

# 3. Reference Project 使用原则

Phase 0 已完成三个项目分析。

只参考：

```text
ensemble
→ 文件系统持久化
→ state
→ Round 组织
→ 原子写
→ 历史不覆盖

Local-LLM-Arena
→ asyncio.gather
→ 多模型并发
→ token streaming
→ StreamingResponse
→ 错误隔离
→ 多模型 Card

llm-council
→ 原生 JS
→ CSS Grid
→ SSE
→ 多模型状态展示
→ Model Selector
```

不要复制三个项目中的：

```text
vote
winner
consensus
final judge
chairman
Elo
自动 drop
自动 synthesis
```

不要修改 `reference/` 中的任何仓库。

***

# 4. 目标目录

最终至少包含：

```text
CrossLLM/
├── docs/
│   ├── architecture_v0.1.md
│   ├── phase0_research.md
│   ├── phase1_mock.md
│   └── reference_analysis.md
│
├── reference/
│   ├── ensemble/
│   ├── llm-council/
│   └── Local-LLM-Arena/
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
│   └── mock_provider.py
│
├── sessions/
│
├── static/
│   ├── index.html
│   ├── style.css
│   └── app.js
│
├── app.py
└── requirements.txt
```

可以根据实际实现调整文件数量，但职责必须保持清晰。

***

# 5. Phase 1 Model

只创建三个 Mock Model：

```text
qwen
deepseek
gpt
```

暂时不要求真实调用这些厂商。

它们只是三个独立的 Model Participant。

例如：

```text
Qwen
Mock Answer A

DeepSeek
Mock Answer B

GPT
Mock Answer C
```

每个 Mock Model 必须产生不同的回答内容，方便验证 UI 和 Round 2 是否正确注入其他模型答案。

***

# 6. Mock Streaming

Mock Provider 必须支持流式输出。

要求：

```text
一个回答
→ 拆分成多个 chunk
→ 延迟发送
→ 前端逐步显示
```

推荐：

```python
async generator
```

模拟：

```text
50ms ~ 200ms
```

之间的随机延迟。

不同模型可以使用不同延迟，以验证前端是否能够真正并行处理。

例如：

```text
Qwen      开始
GPT       开始
DeepSeek  开始

GPT       完成
Qwen      继续
DeepSeek  完成
Qwen       完成
```

前端不得要求模型按照固定顺序返回。

***

# 7. SSE

使用 Server-Sent Events。

推荐事件结构：

```text
event: token
data: {...}
```

统一 JSON：

```json
{
  "model": "qwen",
  "type": "token",
  "content": "这是"
}
```

完成：

```json
{
  "model": "qwen",
  "type": "done"
}
```

错误：

```json
{
  "model": "qwen",
  "type": "error",
  "error": "..."
}
```

必须能够做到：

```text
一个模型错误
≠
整个 Round 失败
```

***

# 8. Round 1

用户输入：

```text
测试问题
```

点击发送后：

```text
Round 1
```

并发调用：

```text
qwen
deepseek
gpt
```

所有模型只能收到：

```text
原始问题
+
系统公共 Prompt
```

Round 1 禁止注入其他模型回答。

***

# 9. Round 1 UI

前端必须显示三个模型 Card：

```text
┌────────────────────┐
│ Qwen               │
│ Streaming...       │
│                    │
│ 回答内容……         │
│                    │
│ [ACTIVE]           │
└────────────────────┘
```

DeepSeek、GPT 同理。

必须显示至少：

```text
模型名称
当前状态
回答内容
```

状态：

```text
pending
streaming
complete
error
```

***

# 10. 持久化

Round 1 完成后立即保存。

使用：

```text
sessions/<session_id>/
```

推荐：

```text
sessions/
└── <session_id>/
    ├── state.json
    ├── question.md
    └── round_001/
        ├── qwen.md
        ├── deepseek.md
        └── gpt.md
```

模型回答必须保存完整 Markdown。

不要因为 Round 2 覆盖 Round 1。

***

# 11. Session State

至少保存：

```json
{
  "session_id": "...",
  "original_question": "...",
  "current_round": 1,
  "active_models": [
    "qwen",
    "deepseek",
    "gpt"
  ],
  "closed_models": []
}
```

允许未来扩展，但本阶段不要提前增加复杂字段。

***

# 12. ACTIVE / CLOSED

每个模型可以被用户设置为：

```text
ACTIVE
CLOSED
```

默认全部 ACTIVE。

例如：

```text
Qwen       ACTIVE
DeepSeek   CLOSED
GPT        ACTIVE
```

CLOSED 的含义：

> 当前停止参与后续 Round。

CLOSED：

- 不再产生下一轮 API/Mock 调用。
- 不从历史中删除。
- Round 1 回答继续保留。
- UI 可以继续查看。

禁止将 CLOSED 理解为模型错误。

***

# 13. Round 2

用户输入追加指令：

```text
请评价其他模型的方案，并说明哪些内容值得吸收。
```

点击：

```text
Next Round
```

如果当前：

```text
ACTIVE:
Qwen
GPT
```

则只调用 Qwen 和 GPT。

***

# 14. Round 2 Prompt

对于 Qwen：

```text
原始问题：
<Original Question>

你的上一轮回答：
<Qwen Round 1>

其他 ACTIVE 模型上一轮回答：

[GPT]
<GPT Round 1>

用户本轮指令：
<Instruction>
```

对于 GPT：

```text
原始问题：
<Original Question>

你的上一轮回答：
<GPT Round 1>

其他 ACTIVE 模型上一轮回答：

[Qwen]
<Qwen Round 1>

用户本轮指令：
<Instruction>
```

每个模型必须：

- 可以看到自己的上一轮回答。
- 可以看到其他 ACTIVE 模型上一轮回答。
- 不应该看到 CLOSED 模型上一轮回答。
- 可以评价其他模型。
- 可以吸收合理观点。
- 可以修改自己的方案。

***

# 15. Round 2 持久化

保存：

```text
sessions/<session_id>/
└── round_002/
    ├── qwen.md
    └── gpt.md
```

如果 DeepSeek 在 Round 1 被 CLOSED：

```text
round_001/deepseek.md
```

必须继续存在。

Round 2 不生成：

```text
round_002/deepseek.md
```

***

# 16. Session Recovery

关闭浏览器后重新打开：

必须能够：

```text
读取 Session
↓
读取 state.json
↓
显示已有 Round
↓
显示 ACTIVE / CLOSED
↓
继续进行下一 Round
```

Phase 1 至少验证：

```text
Round 1
→ 刷新浏览器
→ 数据仍存在
→ 可以继续 Round 2
```

***

# 17. Frontend

必须使用：

```text
HTML
CSS
Vanilla JS
```

不要创建：

```text
package.json
node_modules
React
Vue
Vite
Next.js
```

使用 CSS Grid 实现多模型布局。

推荐：

```css
grid-template-columns:
repeat(auto-fit, minmax(380px, 1fr));
```

模型数量增加时允许自动换行。

***

# 18. 前端交互

至少提供：

```text
[新建 Session]

问题输入框

[发送 Round 1]

模型 Card

每个 Card：
[ACTIVE / CLOSED]

追加指令输入框

[Next Round]
```

Next Round 必须使用当前 ACTIVE 模型列表。

***

# 19. Backend API

至少实现：

```text
POST /api/session
```

创建 Session。

```text
GET /api/session/{session_id}
```

读取 Session。

```text
POST /api/session/{session_id}/round
```

执行下一轮。

请求：

```json
{
  "instruction": "评价其他模型方案"
}
```

```text
POST /api/session/{session_id}/selection
```

修改 ACTIVE / CLOSED。

例如：

```json
{
  "active_models": [
    "qwen",
    "gpt"
  ]
}
```

```text
GET /api/session/{session_id}/events/{round_id}
```

获得 Round Streaming。

***

# 20. 错误处理

单模型失败必须隔离。

例如：

```text
Qwen      complete
DeepSeek  error
GPT       complete
```

整个 Round 仍然完成。

保存错误状态。

不要无限重试。

同一个错误最多自动重试 2 次。

***

# 21. Token / 输出控制

开发阶段继续遵守：

```text
不要输出大量安装日志
不要 cat 大文件
不要递归读取整个 reference
不要重复读取文件
不要运行无关测试
```

命令可能超过约 100 行输出时：

```text
过滤
分页
head/tail
grep/rg
```

测试：

```text
pytest -q
```

或仅输出失败部分。

不要为了证明“测试成功”打印完整日志。

***

# 22. 不要提前优化

本阶段不要实现：

```text
SQLite 复杂数据模型
复杂权限
用户系统
模型配置 UI
Evidence
MCP
Search
自动争议检测
自动摘要
模型评分
模型排名
Token 成本面板
复杂 Prompt 模板系统
```

先把核心 Round 闭环跑通。

***

# 23. 测试场景

必须手动或自动验证：

### Test 1

发送：

```text
测试问题
```

确认：

```text
Qwen / DeepSeek / GPT
```

同时开始 Streaming。

***

### Test 2

确认：

```text
round_001/
```

生成三个 Markdown。

***

### Test 3

关闭：

```text
DeepSeek
```

再次进入 Round 2。

确认：

```text
Qwen 调用
GPT 调用
DeepSeek 不调用
```

***

### Test 4

检查 Round 2 Prompt。

确认：

Qwen 看到了：

```text
自己的 Round 1
GPT Round 1
```

但没有：

```text
DeepSeek Round 1
```

GPT 同理。

***

### Test 5

确认：

```text
round_001/
```

内容未被修改。

***

### Test 6

刷新浏览器。

确认 Session、Round 1、Round 2、ACTIVE/CLOSED 状态仍然存在。

***

### Test 7

制造一个 Mock 模型错误：

```text
DeepSeek = error
```

确认 Qwen/GPT 仍能完成。

***

# 24. Phase 1 完成标准

全部满足：

```text
[ ] FastAPI 正常启动
[ ] Web UI 正常打开
[ ] 3 个 Mock Model 可以并发
[ ] 3 个 Card 可以同时 Streaming
[ ] SSE 正常工作
[ ] Round 1 自动持久化
[ ] Session 状态持久化
[ ] ACTIVE/CLOSED 正常工作
[ ] CLOSED 模型不会进入下一 Round
[ ] Round 2 可以读取 Round 1
[ ] Round 2 正确注入其他 ACTIVE 模型
[ ] Round 2 自动持久化
[ ] 浏览器刷新后 Session 可恢复
[ ] 单模型失败不会导致整个 Round 失败
```

***

# 25. 开发顺序

严格按以下顺序：

```text
1. FastAPI 基础服务
↓
2. Mock Provider
↓
3. Session / Storage
↓
4. Round 1
↓
5. SSE Streaming
↓
6. Vanilla JS 多模型 Card
↓
7. ACTIVE / CLOSED
↓
8. Round 2 Prompt Builder
↓
9. Round 2
↓
10. Session Recovery
↓
11. 测试
```

每完成一个阶段，先测试，再继续。

不要一次创建大量未经验证的代码。

***

# 26. 最终要求

不要因为 Phase 0 中发现的 reference 项目具有更多能力，就把这些能力提前加入 Phase 1。

Phase 1 的成功标准只有：

> **证明 CrossLLM 的核心机制：多个平级模型并行回答、回答持久化、用户控制模型存活状态，并让下一轮 ACTIVE 模型读取上一轮其他 ACTIVE 模型的答案后继续分析。**

完成 Phase 1 后停止。

不要自动进入 Phase 2。

````

然后你给 TRAE 的实际聊天 Prompt 就可以极短：

```text
请读取并严格遵守：

docs/architecture_v0.1.md
docs/phase1_mock.md
reference_analysis.md

开始执行 Phase 1。

严格按照 phase1_mock.md 的开发顺序实现。
完成 Phase 1 后停止，不要进入 Phase 2。
````

### 我建议你这次尤其不要让 TRAE 一口气“全部完成”

Phase 0 已经暴露出一个很好的事实：三个参考项目的技术实现不完全一样——`ensemble` 偏文件状态，`Local-LLM-Arena` 的流式实际上是 NDJSON，`llm-council` 更接近 SSE + 原生 JS。

所以 Phase 1 **让 Agent 按 11 个小步骤逐步实现、逐步测试**，比一句“完成 Phase 1”更稳。尤其先做到：

```text
Mock 3 模型
↓
并发
↓
SSE
↓
多卡片
```

确认没问题以后，再做：

```text
持久化
↓
ACTIVE/CLOSED
↓
Round 2
```

否则一旦最后测试失败，AI 很容易面对几百行新代码一起排错。

还有一点值得保留：Phase 0 已经确认 `ensemble` 和 `Local-LLM-Arena` 都是 MIT，但 `llm-council` 虽 README 声明 MIT、仓库却没有 LICENSE 文件，因此 Phase 1 中**只把 llm-council 当实现参考，不直接复制其源码**。

**你下一步就可以把这个** **`phase1_mock.md`** **放进** **`docs/`，然后让 TRAE 开工。**
