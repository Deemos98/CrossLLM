# CrossLLM UI Refactor Specification

> 文档性质：UI 产品改造与验收规范
>
> 本文定义的是“用户实际看到和使用的 CrossLLM UI 应达到什么状态”，不是 CSS class 命名规范。
>
> `docs/ui_naming.md` 继续负责命名契约；
> 本文负责视觉、布局、用户可见文本、交互、状态同步和最终人工验收。
>
> 本文是本次 UI 改造的主要验收依据。
> 不得用 `ui_naming.md` 的完成情况代替本文的完成情况。

---

## 0. 核心目标

本次任务不是：

- CSS class 重命名
- JS 变量重命名
- grep 一批英文关键词
- 让 pytest 通过
- 修改后声明“UI 已完成”

本次任务是：

> **把 CrossLLM 当前实际网页界面改造成面向最终用户的中文工作台，并清理内部实现细节，同时保持现有 Phase 4 功能和数据语义不变。**

必须同时满足：

1. 用户可见界面发生实际、可观察的视觉变化；
2. 用户可见文本完成中文化；
3. 内部实现概念不直接暴露给用户；
4. 页面布局按照本规范重新整理；
5. 已有 Session / Round / Pin / Summary / Evidence / Memory 等功能继续工作；
6. Summary 在切换 Session 后正确刷新；
7. Memory / Candidate UI 与当前 M2 功能保持一致；
8. 必须进行浏览器级人工验收；
9. **仅 pytest 通过不得视为 UI 改造完成。**

---

# 1. 开发优先级

执行任务时必须遵守：

```text
本任务要求
    ↓
CROSSLLM_UI_REFACTOR_SPEC.md
    ↓
ui_naming.md
    ↓
现有代码
    ↓
现有测试
````

解释：

* 本文决定“用户看到什么”；
* `ui_naming.md` 决定内部命名契约；
* 现有代码是实现基础，不是最终 UI 标准；
* 旧测试不能成为阻止 UI 改造的理由；
* 若旧测试检查的是旧 UI 文本/旧 DOM，应同步修改测试，而不是为了测试保留错误 UI。

---

# 2. 严禁缩小任务范围

以下行为不得视为完成任务：

### 2.1 只修改 class 名

例如仅完成：

```text
session-item-q
→ session-item-question

round-head
→ round-header
```

但用户实际看到的主题、布局、文本几乎没有变化。

这种情况只能报告为：

> Naming Contract 完成

不能报告：

> UI Refactor 完成

### 2.2 仅以 pytest 全绿作为 UI 完成证明

以下结论均无效：

```text
533 passed
→ UI 完成

UI 测试通过
→ UI 完成
```

UI 必须增加浏览器级验收。

### 2.3 不得因为 MD 未写某个视觉细节就自动保留现状

本文的目标是完整 UI 改造。

若现有界面明显存在：

* 内部字段泄漏
* 英文实现术语
* 调试字段
* 混乱布局
* 重复信息
* 不一致视觉层级

必须处理。

### 2.4 不得自行宣布未完成项已经完成

最终报告必须逐项说明：

```text
完成
未完成
无法完成 + 原因
```

任何 P0 项未完成，都不能报告“整个 UI Refactor 完成”。

---

# 3. 用户可见语言规范

## 3.1 核心原则

内部代码变量、API 字段、JSON 字段不要求改成中文。

例如内部可以继续：

```text
session_id
round_id
memory_id
source_semantics
request_max_output
model_max_output
```

但是这些**不得直接暴露在普通用户 UI 中**。

---

## 3.2 用户界面显示映射

| 内部概念                 | 用户显示          |
| -------------------- | ------------- |
| Session              | 会话            |
| Session ID           | 会话编号（普通界面可隐藏） |
| Round                | 第 N 轮 / 轮次    |
| Pin                  | 固定            |
| Unpin                | 取消固定          |
| Summary              | 摘要            |
| Conversation Summary | 对话摘要          |
| Peer Review          | 平级互评          |
| Evidence             | 证据            |
| MCP Evidence         | MCP 证据        |
| Memory               | 项目记忆          |
| Project Memory       | 项目记忆          |
| Candidate            | 记忆候选          |
| Memory Candidate     | 记忆候选          |
| Decision             | 决策            |
| Fact                 | 已确认事实         |
| Experiment           | 实验结果          |
| Rejected             | 已排除方案         |
| Open Question        | 待解决问题         |
| Evidence Reference   | 证据引用          |
| Opinion              | 模型观点          |
| User Decision        | 用户决策          |
| Result               | 实验结果/结果       |

内部概念仍然保留，但普通用户看到的文本必须优先使用上面的中文。

---

# 4. 禁止暴露内部实现字段

以下字段不应出现在普通用户界面：

```text
provide
request_max_output
model_max_output
reserved_output_tokens
source_semantics
artifact_kind
peer_context_reserve
token_exactness
token_source
context_limit
fixed_input_tokens
history_budget
lossless_budget
compaction_budget
runtime_record_id
model_id
```

除非某个页面明确属于：

> 调试 / 开发者信息

否则不得直接展示。

如果业务上确实需要显示，应转换成人类可理解的中文描述。

例如：

```text
lossless_budget
```

不要直接显示：

```text
lossless_budget: 3725
```

而应显示：

```text
原文历史预算：约 3725 token
```

普通界面不应要求用户理解 Python / API / JSON 字段名称。

---

# 5. 视觉主题

本次必须进行实际视觉改造。

要求：

1. 页面具有统一主题；
2. 页面背景、卡片、边框、文字、按钮、状态提示具有明确层级；
3. 不得只是修改几个 class 名后保留原来的视觉结果；
4. 不得出现明显的默认浏览器控件风格；
5. 各面板视觉语言必须统一；
6. Summary、Evidence、Memory 等面板不得各自使用完全不同的视觉体系；
7. 颜色必须通过统一 CSS 变量管理；
8. 不得在各个组件中散落大量硬编码颜色。

### 验收

浏览器实际检查：

* 背景层级
* 面板层级
* 卡片层级
* 主按钮
* 次按钮
* 成功
* 警告
* 错误
* 固定状态
* 候选状态
* 已确认 Memory 状态

都必须存在明显区分。

---

# 6. 页面布局

## 6.1 禁止保留旧的“机械卡片堆叠”

当前页面若仍然存在：

```text
卡片卡片卡片卡片
卡片卡片卡片卡片
```

且仅按固定 4 列机械排列，不视为完成。

布局必须根据实际页面信息密度重新组织。

要求：

* 模型回答区域应该突出主要内容；
* Session / 历史属于辅助导航；
* Summary / Evidence / Memory 属于工具面板；
* 当前问题与回答应有明显主次；
* 操作按钮不能与大量元数据争夺视觉焦点；
* 小屏/窗口缩小时不得出现明显溢出。

### 具体列数

不得由 Agent 自行以“现状就是这样”为理由保持原 4 列。

实际目标列数必须以当前任务的 UI 目标图/用户指定值为准。

如果本任务未提供目标截图，则不得声称“布局已按目标完成”，必须在最终报告中明确：

```text
布局目标缺少明确参考，因此仅完成结构整理，未宣称视觉验收完成。
```

---

# 7. Session / Round UI

## 7.1 Session

用户看到：

```text
会话
```

而不是：

```text
Session
```

除调试信息外，不直接展示 Session ID。

---

## 7.2 Round

用户看到：

```text
第 1 轮
第 2 轮
第 3 轮
```

而不是：

```text
Round 1
round_id=...
```

---

## 7.3 Pin

功能必须保留。

显示：

```text
固定
取消固定
```

而不是直接显示：

```text
Pin
Unpin
```

固定状态必须有明显视觉反馈。

---

# 8. Summary UI

Summary 属于用户功能，而不是内部调试信息。

用户看到：

```text
对话摘要
```

相关按钮和状态必须中文化。

## 8.1 Session 切换问题

切换 Session 后：

* 必须重新加载对应 Session 的 Summary；
* 如果该 Session 有最新 Summary，不得显示为空；
* 如果该 Session 没有 Summary，才显示明确的“暂无摘要”；
* 不得因为前一个 Session 的状态残留导致显示错误。

### 强制验收

至少测试：

```text
Session A 有 Summary
↓
切换 Session B
↓
显示 B 的 Summary

切回 Session A
↓
恢复 A 的 Summary
```

---

# 9. Evidence UI

用户界面显示：

```text
证据
MCP 证据
```

不得直接暴露：

```text
mcp_evidence_index
runtime_record_id
used_in_final_answer
```

除非用户主动展开“详细信息”。

普通用户首先应该看到：

* 来源
* 工具
* 简要内容
* 是否被回答引用

内部 ID 属于详情信息，不应占据主要视觉空间。

---

# 10. Memory UI

当前 M2 已经具有：

```text
Candidate
Confirm
Duplicate
Conflict
```

UI 必须继续保留。

用户看到：

```text
项目记忆

＋ 生成记忆候选

记忆候选
已确认记忆
```

---

## 10.1 Candidate

显示：

```text
候选内容
类型
来源
```

内部：

```text
candidate_id
source_semantics
source_refs
```

不应成为主要视觉内容。

---

## 10.2 Confirm

按钮：

```text
确认保存
```

确认后正式进入：

```text
项目记忆
```

不得让用户误以为 Candidate 已经自动成为正式 Memory。

---

## 10.3 Duplicate

显示明确中文：

```text
与已有项目记忆重复，未创建新的记忆。
```

并显示对应条目。

---

## 10.4 Conflict

显示：

```text
检测到可能冲突的项目记忆。

系统不会自动覆盖旧内容，请人工核对。
```

不得显示成“系统已经裁决”。

---

# 11. 本轮 k

当前正式设计：

> 只保留一个 Session 级 k 控件。

UI 必须只存在一个用户可见的 k Slider。

显示：

```text
无损历史保留比例 k
```

说明：

```text
k 越高，越多历史保留原文；压缩区越小。
```

不得再出现第二个“本轮 k”。

用户修改 Session k 后，后续发送必须使用该值。

不得把以下内部字段展示给用户：

```text
lossless_budget
compaction_budget
history_budget
```

如需要调试显示，应放入开发者详情区域。

---

# 12. 用户可见内容与内部结构分离

必须遵守：

```text
内部数据模型
        ≠
用户显示文本
```

例如：

```text
内部：
source_semantics = decision

UI：
来源语义：用户决策
```

例如：

```text
内部：
status = candidate

UI：
状态：候选
```

不得直接把 API JSON dump 到 UI。

---

# 13. 必须进行浏览器级验收

这是本次任务的强制要求。

不能只运行：

```text
pytest
```

必须：

1. 启动实际 Uvicorn；
2. 打开真实页面；
3. 创建/切换 Session；
4. 执行至少一轮正常对话；
5. 检查 Session / Round；
6. 检查 Summary；
7. 检查 Evidence；
8. 检查 Memory；
9. 检查 k；
10. 检查中文显示；
11. 检查布局；
12. 检查主题。

可使用 Playwright/MCP 或实际浏览器。

---

# 14. 浏览器验收清单

最终必须逐项填写：

| 项目                                     | 验收 |
| -------------------------------------- | -- |
| 主题发生实际变化                               | ☐  |
| 页面视觉层级清晰                               | ☐  |
| 布局已重新整理                                | ☐  |
| 不再固定机械 4 列                             | ☐  |
| Session 中文化                            | ☐  |
| Round 中文化                              | ☐  |
| Pin 中文化                                | ☐  |
| Summary 中文化                            | ☐  |
| Evidence 中文化                           | ☐  |
| Memory 中文化                             | ☐  |
| Candidate 中文化                          | ☐  |
| Decision 中文化                           | ☐  |
| 内部 token/API 字段不泄露                     | ☐  |
| `provide` 不出现在普通 UI                    | ☐  |
| `request_max_output` 不出现在普通 UI | ☐  |
| Summary 切换 Session 正确                  | ☐  |
| Memory Candidate 正常                    | ☐  |
| Memory Confirm 正常                      | ☐  |
| Duplicate UI 正常                        | ☐  |
| Conflict UI 正常                         | ☐  |
| k 控件只有一个                               | ☐  |
| k 修改后实际生效                              | ☐  |

**任何 P0 项没有勾选，不得报告“UI Refactor 完成”。**

---

# 15. 测试要求

自动化测试仍然必须保留。

至少：

```text
pytest 全量
Phase 4 E2E
已有 UI 测试
新增 UI 行为测试
```

但是：

> 自动化测试通过 ≠ UI 完成。

必须同时有浏览器验收记录。

---

# 16. 修改范围

优先允许：

```text
static/index.html
static/app.js
static/style.css
```

如果现有功能 bug 的根因确实在后端，可以修改必要后端文件。

但是：

* 不得为了 UI 重构 Phase 4 核心架构；
* 不得改变 Memory 业务语义；
* 不得改变 Context Selection 语义；
* 不得增加新的 Memory Agent；
* 不得增加无关依赖。

---

# 17. 开发流程

开发 Agent 必须按以下顺序执行。

### Phase A：现状审计

读取：

* `CROSSLLM_UI_REFACTOR_SPEC.md`
* `ui_naming.md`
* 当前 UI 相关文件

先输出：

```text
需求项
当前状态
目标状态
涉及文件
```

不得直接开始修改。

### Phase B：实现

严格按 P0 → P1 顺序。

### Phase C：浏览器验收

必须实际打开页面检查。

### Phase D：自动化回归

运行：

```text
pytest
Phase 4 E2E
UI tests
```

### Phase E：最终报告

必须逐项回答：

```text
1. 哪些 UI 实际发生变化？
2. 哪些文件修改？
3. 哪些用户可见文本发生变化？
4. 哪些布局发生变化？
5. 哪些内部字段被隐藏？
6. Summary Session 切换是否验证？
7. 浏览器验收截图/结果在哪里？
8. pytest 结果？
9. E2E 结果？
10. 未完成项？
```

---

# 18. 最重要的完成定义

只有同时满足：

```text
真实 UI 改造
+
用户可见文本改造
+
布局改造
+
主题改造
+
内部字段隐藏
+
交互行为正确
+
浏览器人工验收
+
自动化测试通过
```

才能报告：

> CrossLLM UI Refactor 完成。

如果只完成：

```text
class rename
+
pytest
```

则只能报告：

> UI Naming Contract 完成。

不得使用“UI 改造完成”这一结论。

````

## 然后给 WorkBuddy 的启动提示不要再写得像普通开发任务

直接用这个：

> **以 `docs/CROSSLLM_UI_REFACTOR_SPEC.md` 作为本次 UI 改造的验收规范。**
>
> 上一次执行存在明显范围收缩：Agent 将整体 UI 改造缩减成 CSS class 命名重构，并以 pytest 通过宣布完成。本次禁止重复这种行为。
>
> 先只读检查：
> - `docs/CROSSLLM_UI_REFACTOR_SPEC.md`
> - `docs/ui_naming.md`
> - `static/index.html`
> - `static/app.js`
> - `static/style.css`
> - 与 Summary / Evidence / Memory UI 直接相关的必要文件
>
> **第一步不要修改代码。**
>
> 输出一张“需求 → 当前状态 → 目标 → 修改文件”的矩阵。
>
> 特别检查：
> 1. 主题是否真的需要改；
> 2. 布局是否仍为旧的固定 4 列；
> 3. 用户可见英文术语；
> 4. 内部 API/config 字段是否泄漏到 UI；
> 5. Summary 切换 Session 后是否正确刷新；
> 6. Memory/Candidate UI；
> 7. 当前 k 控件是否只有一个；
> 8. 当前 UI 是否与本文的用户可见要求一致。
>
> **禁止把任务缩减成命名重构。**
>
> 第二步再实施。
>
> 完成后必须启动真实 Uvicorn，通过浏览器/Playwright 做实际 UI 验收，并提供至少一份实际页面截图或等价的浏览器验收结果。
>
> 自动化测试必须通过，但不能用 pytest 代替浏览器验收。
>
> 最终报告必须逐项对应本 MD 的验收表。
>
> **任何 P0 项未完成，都不得报告“UI Refactor 完成”；必须明确列为未完成。**
>
> 不修改 Phase 4 核心业务语义，不借 UI 任务顺带重构后端。

### 这次和以前最大的区别

以前 Agent 可以走：

```text
MD
↓
grep
↓
改 class
↓
pytest 通过
↓
“完成”
````

新的规范强制变成：

```text
用户看到什么
↓
逐项需求矩阵
↓
实际 UI 修改
↓
浏览器验收
↓
自动化测试
↓
逐项关闭需求
↓
才能说完成
```

而且**不要再把“Session / Round / Pin / Decision”要求删除**。这些是产品概念；真正需要的是：

```text
Session   → 会话
Round     → 第 N 轮
Pin       → 固定
Decision  → 决策
Memory    → 项目记忆
Candidate → 记忆候选
```

即**保留内部语义，改变用户显示层**。

还有一点我建议你在真正写这份 MD 前补死：**“卡片到底几列”“主题具体是什么颜色”最好写成硬指标或直接附目标截图**。这两个如果继续保持模糊，Agent 仍然可能用“我调整过布局/主题”来糊弄验收。
