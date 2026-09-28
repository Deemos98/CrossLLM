# CrossLLM UI Naming & Phase 4 Boundary

## 1. Purpose

本文固定 CrossLLM Phase 4 的 UI / 测试命名，
用于避免 Session、Round、Summary、Project Memory 等概念混用。

本文只定义命名和职责边界，不定义具体实现方案。

---

## 2. Core Concepts

CrossLLM 中以下概念必须保持独立：

- Session
- Session History
- Round
- Summary
- Project Memory

基本关系：

```text
Session
├── Session History（导航入口，不属于 Session 内容）
├── Round
│   ├── Round 1
│   ├── Round 2
│   └── Round N
└── Summary

Project Memory 是跨 Session 的独立层：

Project
└── Project Memory
    ├── Memory 001
    ├── Memory 002
    └── ...
3. Session
Definition

Session 是一个完整的 CrossLLM 对话工作空间。

推荐术语：

Session
Session ID
Current Session
New Session
Switch Session

不使用：

Chat History
Conversation History
Session Memory
4. Session History
Definition

Session History 是历史 Session 的只读导航列表。

职责：

获取历史 Session
显示 Session 摘要
按 updated_at 倒序显示
点击历史 Session
切换当前 Session
新建 Session 后刷新列表
高亮当前 Session

API：

GET /api/sessions

单个 Session：

GET /api/session/{session_id}
UI Naming
session-history
session-history-list
session-history-refresh

session-item
session-item-question
session-item-round
session-item-meta
session-item-tag
is-current

语义职责：

```text
session-item           单个 Session 条目（button）
session-item-question  问题摘要（独立节点，不得依赖 children[0] 定位）
session-item-round     Round 数（独立节点，不得拼接进 meta 串）
session-item-meta      更新时间（相对时间）
session-item-tag       “当前”文字徽标（仅当前项；与 is-current 状态类互补）
is-current             当前正在显示的 Session（状态类，非 active）
```
Boundary

Session History：

不负责 Memory
不负责 RAG
不负责检索
不负责向量化
不参与 Prompt 构造
不进行上下文注入

它只是导航。

5. Round
Definition

Round 是一次用户发送指令后形成的一轮模型执行。

例如：

Round 1
Round 2
Round 3

一个 Session 可以存在多个 Round。

多个 Round 可以同时执行。

UI Naming
rounds
round
round-header
round-title
round-status
round-content
round-content-label
round-content-text

语义职责：

```text
rounds                 Round 列表容器
round                  单个 Round Card（外层）
round-header           Round 头部：编号 + 状态 + Pin 操作
round-title            Round 编号（R1），round-header 的子元素
round-status           Round 状态 badge 组（候选数 / 互评开关）
round-content          Round 正文：本轮指令
round-content-label    round-content 的小标题（“本轮指令”）
round-content-text     round-content 的正文文本
```

Round Card 的实际 DOM 结构：

```text
section.round[data-round]
├── div.round-header
│   ├── span.round-title     R1
│   ├── span.round-status    badge：N 个候选 / 互评 开·关
│   └── button.pin-toggle    Pin / Unpin
├── div.round-content
│   ├── div.round-content-label
│   └── div.round-content-text
└── div.grid                 内层 Candidate Card
```
Concurrency

多个 Round 并发时：

Round 1 ── SSE 1
Round 2 ── SSE 2
Round 3 ── SSE 3

每个 Round 必须拥有独立：

状态
Response
SSE
UI Card
Event routing

一个 Round 完成不得关闭其他 Round 的 SSE。

6. Round Concurrency Test

测试文件：

test_round_concurrency.py

该文件负责：

Backend
多个 Round 真正并发执行
慢 Round / 快 Round 可以不同顺序完成
多个 Round 的状态不互相覆盖
不同 Round 的响应不串台
后发 Round 不读取尚未完成 Round 的正文
Frontend
连续发送多个 Round 后，各自保留 SSE
token 事件写入对应 Round Card
某个 Round 完成只关闭自己的 SSE
不通过禁用发送按钮阻止并发 Round
7. Summary
Definition

Summary 是用于控制上下文长度的压缩结果。

Summary 不等于完整历史。

Summary 不等于 Project Memory。

Summary 不等于 Session History。

Recommended Names
Summary
Session Summary
Model Summary
Latest Summary

代码命名：

summary
model_summary
latest_summary

避免将 Summary 命名为：

memory
project_memory
history
retrieval
context

除非变量实际表示对应概念。

8. Summary UI

测试文件：

test_phase4_summary_ui.py

该文件只负责 Summary UI 行为。

可能包括：

Summary 面板存在
Summary 正确加载
Summary 正确显示
Summary 可编辑
Summary 可保存
Session 切换后加载对应 Summary
最新 Summary 正确显示
Summary 不被误当作 Project Memory

该文件不负责 Round Concurrency。

9. Project Memory

Project Memory 是跨 Session 的长期确认信息。

它与 Session History、Round、Summary 分离。

Session History
    ↓
导航历史 Session

Round
    ↓
当前 Session 的执行历史

Summary
    ↓
上下文压缩

Project Memory
    ↓
跨 Session 的确认信息

不得因为 UI 上都出现“历史信息”而将它们合并。

10. Naming Rules
Use
Session
Session History
Current Session
Round
Round Concurrency
Summary
Project Memory
Avoid ambiguous naming
History
Memory
Context
Chat
Conversation
Active

除非上下文已经明确。

11. active vs current

对于 Session History：

推荐：

is-current

不推荐：

active

原因：

active 在 CrossLLM 中已经用于：

active_models
active model

而：

is-current

明确表示：

当前正在显示的 Session。

因此推荐：

.session-item.is-current
12. Phase 4 Test File Naming

实际结构（**以代码为准**）：项目采用扁平测试文件结构，全部位于仓库根目录，
不存在 `test/_phase4/` 子目录，也不使用下划线前缀文件名。

```text
test_phase4_session_history.py
test_phase4_summary_ui.py
test_round_concurrency.py
```

职责：

| File | Responsibility |
|---|---|
| `test_phase4_session_history.py` | Session History navigation |
| `test_round_concurrency.py` | Concurrent Round execution |
| `test_phase4_summary_ui.py` | Summary UI behavior |

命名规范面向**职责划分**，不要求代码迁移到不存在的目录结构。
新增 Phase 4 测试沿用同目录扁平命名：`test_phase4_<职责>.py`。
13. Existing File Rename（已完成）

历史上曾出现「文件名叫 Summary UI、实际内容却是 Round Concurrency」的错位。
该错位**已经消除**，当前实际状态：

| 文件 | 实际内容 | 是否一致 |
|---|---|---|
| `test_round_concurrency.py` | Round Concurrency 测试 | 一致 |
| `test_phase4_summary_ui.py` | Summary UI 测试 | 一致 |

两个文件职责已分离、各自正确，**无需再重命名**。
若后续再出现「文件名与实际内容不符」，仍按本条处理：
改名使文件名与实际职责一致，而不是反过来迁就目录结构。
14. Final Boundary

最终 UI 概念边界：

CrossLLM
│
├── Session
│   │
│   ├── Session History
│   │   └── 历史 Session 导航
│   │
│   ├── Rounds
│   │   ├── Round 1
│   │   ├── Round 2
│   │   └── Round N
│   │
│   └── Summary
│       └── 上下文压缩
│
└── Project Memory
    └── 跨 Session 确认信息

核心原则：

Session History ≠ Memory
Round ≠ Summary
Summary ≠ Project Memory
Session ≠ Session History

这些概念必须在 UI、JS、API、测试文件命名中保持一致。


### 最终落地

已按以下结果收口（**与实际代码结构一致**）：

```text
test_phase4_session_history.py
        ↓ 保持不变（Session History navigation）

test_round_concurrency.py
        ↓ 保持不变（Round Concurrency）

test_phase4_summary_ui.py
        ↓ 保持不变（Summary UI，与文件名一致）

docs/ui_naming.md
        ↓ 已新增，并校正为实际扁平结构
```

本次同步完成的命名收口（旧命名 → 规范命名）：

```text
Session History
  session-item-q        → session-item-question
  （rounds 串）         → session-item-round（从 session-item-meta 拆出为独立节点）

Round
  round-head            → round-header
  round-meta            → round-status
  round-q               → round-content
  round-q-label         → round-content-label
  round-q-text          → round-content-text
```

测试断言同步：不再通过 `children[0]` 定位问题摘要，
改为按 `session-item-question` 语义节点查找（§4）。