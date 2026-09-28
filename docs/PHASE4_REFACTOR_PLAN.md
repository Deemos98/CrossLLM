# PHASE4_REFACTOR_PLAN.md

**日期**：2026-09-03
**规范源**：`docs/phase4_memory.md`（**3380 行 / 49,520 字节 / md5 `855dcfe7…`**，§0–§71）
**前置产物**：`PHASE4_AUDIT.md`（Step 1，§A–§J）
**本轮范围**：**Step 2 — 只设计，不修改任何业务代码**
**定位**：与 `phase4_memory.md` 合起来，构成 Step 3 实现的**工程设计契约**

> 本文档的每个设计决策都必须能回答 MD §71 末尾的五个问题：
> 为什么需要？解决什么实际问题？是否已有成熟项目验证？为什么现有规则不够？能否复用已有机制？

---

## 0. 阅读约定

1. **MD > 旧代码 > 旧测试**。凡冲突，以 `phase4_memory.md` 为准（MD §54、用户任务书 §一）。
2. 文中 `🔴` = 语义冲突必须改；`🟠` = 能力缺失必须建；`🟢` = 可保留。
3. 所有 `§N` 引用默认指 `docs/phase4_memory.md` 的章节号。
4. 所有行号引用指 **审计时**的代码状态（`PHASE4_AUDIT.md` 基准），实现时以实际为准。

---

# 1. 需求映射矩阵

> 19 项 MD 需求 × 9 个落地维度。这是本 Plan 的核心，Step 3 逐行验收。

| # | MD Requirement | MD § | Current Code | Problem | Target Module | Data Model | API | UI | Persistence | Log | Tests |
|---|---|---|---|---|---|---|---|---|---|---|---|
| 1 | **Valid Round** | §2.1 §4 | `round_manager.py:461` 按**单模型**过滤 `status=="complete"`，无 Round 级 Valid 抽象 | 🔴 无 Valid 概念；无法回答"为何此 Round 被排除"(§59) | `core/valid_round.py` | `ValidRound{is_valid, successful_answers, failed_models}` | — | — | `state.json` 增 `is_valid` | `history_selection.dropped_rounds[].reason` | T4 T5 |
| 2 | **Round History Unit**<br>User Q + 成功 Answers | §5 §31 | `storage.py:121` `load_responses` **跳过 `.prompt.md`**，历史只有 Answer；C.4 | 🔴 违反 §31"不要 Question 被移除、Answer 单独留下" | `core/valid_round.py` + `prompt_builder.render_history` | `HistoricalContextItem{user_question, answers[]}` | — | — | 从 `state.json` 读 `rounds[].user_instruction` | — | T2 T3 |
| 3 | **History Selection**<br>动态，非 N 轮 | §29 §54 §64 | `round_manager.py:46` **`HISTORY_WINDOW=3`** + `:453` `range(1, HISTORY_WINDOW+1)` | 🔴 硬上限，直接违反 §54"不得再作为正式硬上限" | `core/context_selector.py` | `ContextSelectionResult` | — | — | — | `history_selection{selected_rounds, dropped_rounds, reason}` | T1 T2 T3 |
| 4 | **Pin** | §18 §26 §27 | 全局搜索 `pinned\|pin_round` → **0 命中** | 🔴 能力完全缺失 | `core/pin_store.py` | `PinState` + `PinEvent` | `POST /api/session/{sid}/pin` | Pin 控件（每个 Round 卡片） | `sessions/<sid>/pins.jsonl` | `memory_pin{round_id, pinned, reason}` | T6 T7 |
| 5 | **k**（0.01–0.90） | §20 §33 §55 | `context_budget.py:32` `DEFAULT_COMPACTION_RATIO=0.8` 用作**触发阈值** | 🔴 **语义反向**：MD 明令"k 不代表什么时候触发 Compaction" | `core/context_budget.py`（重写） | `ContextBudget{k, lossless_budget, compaction_budget}` | `RunRoundIn.k` | `k` 滑块 0.01–0.90 + 当前值显示 | `state.json` 增 `k` | `context_policy.k` | T8 |
| 6 | **Context Budget** | §21 §24 | `context_budget.py:54-58` 明说"故意忽略 tool definitions" | 🔴 `fixed_input_tokens` 漏算 Tools | `core/context_budget.py` | `ContextBudget` 全字段 | — | Context 状态面板 | — | `context_budget{全部字段}` | T8 T16 |
| 7 | **Reserved Output**<br>三值分离 | §22 §23 §24 | `real_provider.py:225` `min(cfg.max_output_tokens, remaining)`，事后计算 | 🔴 静态值 + 事后；MD 要求事前预留；且未区分 provider / request | `core/model_capability.py` + `ContextSelector` | `ProviderModelMetadata.model_max_output`；`ContextBudget{model_max_output, request_max_output, reserved_output_tokens}` | `RunRoundIn.request_max_output`(可选) | — | `.model_metadata_cache.json` | `context_budget{model_max_output, request_max_output, reserved_output_tokens}`（**三值同时记录**） | T9 T20 T21 |
| 8 | **Provider Metadata** | §22 §23 §25 | 全局搜索 `api/v1/models` → **0 命中**；全部来自 models.yaml 静态值 | 🔴 无法区分 provider/request；无 `unknown` 标记 | `core/model_capability.py` | `ProviderModelMetadata` + `TokenEstimate` | — | — | `.model_metadata_cache.json` | `token_source` + `token_exactness` | T9 |
| 9 | **Conversation Summary**<br>= Derived Artifact | §6 §9 §10 §11 | 只有 `compactor.py` 的 compaction summary，无独立类型/存储/触发 | 🔴 拿 Compaction 冒充 Summary（§37 明禁）。**必须按 `model_id` 隔离**，且明确为派生产物，不是新 Round | `core/summary_service.py` | `ConversationSummary{trigger, status, source_valid_round, is_derived_artifact}` | — | — | `summaries/<model>/summary_NNN.md` | `conversation_summary{trigger, input_tokens, output_tokens, status, reused}` | T14 T17 T18 T19 |
| 10 | **Summary UI** | §8 §56 | 前端只有 `peerReview` 一个开关（`app.js:33`） | 🔴 用户无法触达 | `static/app.js` 状态机 | `SummaryMode{draft_backup}` | — | `[ ] Summary` 开关 + 自动填充 Prompt | — | `summary_mode_enabled`（可选） | T10 |
| 11 | **Summary Persistence**<br>+ 复用规则 | §11 §62 | `compact_<K>.md` 平铺在 session 根 | 🔴 与 compaction 混同，无模型隔离；且无复用判断会导致 Peer Review 每轮重算 | `core/summary_service.py` | `ConversationSummary` + `SummaryReusePolicy` | `GET /api/session/{sid}/summary/{model}` | — | `summaries/<model>/summary_NNN.md` + `latest` 软链 | `conversation_summary{source_valid_round, reused}` | T14 T18 |
| 12 | **Peer Context Preparation** | §13 §14 §30 | `round_manager.py:480-486` 永远 `build_anonymous_context(previous_answers=…)` | 🔴 无 `valid_answer_count` 判断；§30"最近一轮不再是唯一来源"；**且无 Summary 复用 → 每轮重复从头总结** | `core/peer_context.py` | `PeerContextItem{mode, source_type, source_id}` | — | `[ ] Peer Review` 保留 | **不持久化**（§17） | `peer_context_preparation{valid_answer_count, mode, source_id, summary_reused}` | T11 T12 T18 |
| 13 | **Peer Review Isolation** | §15 §16 §43 | 无隔离机制，无测试 | 🔴 无保证 | `core/peer_context.py` 数据层白名单 | `ALLOWED_SOURCE_TYPES` | — | — | **不落 peer_context 文件**（天然不可回读） | `peer_context_preparation` 可证 | T13 |
| 14 | **Compaction**<br>（Session 级共享，保留来源语义） | §32 §34 §35 §36 | `compactor.py` 单次；触发条件为 80% 阈值 | 🔴 触发语义错误 + 无 `attempt` 元数据 + **可共享但未保留不同模型观点/来源语义，有冒充某模型 Summary 的风险** | `core/compaction.py`（**替掉** `compactor.py`） | `CompactionRecord{attempt, source_rounds, contributor_models[], viewpoint_attribution}` | — | — | `compact_<K>.md`（Session 级共享，**与 `summaries/<model>/` 严格分目录**） | `compaction{attempt, input_tokens, actual_summary_tokens, summary_file, status, contributor_models}` | T3 T15 T22 |
| 15 | **Recursive Compaction** | §38 §39 | 无循环，超限回退 `_trim_to_budget` **硬裁** | 🔴 违反 §38 + §1-1 | `core/compaction.py` | `MAX_COMPACTION_ROUNDS=3` | — | — | — | `compaction.attempt` 递增 + `context_budget_warning` | T15 |
| 16 | **MCP Runtime** | §45 | `core/mcp_runtime_log.py`（17 测试覆盖） | 🟢 **保留，不改语义** | 原样 | — | — | — | `round_NNN/mcp_runtime.jsonl` | `mcp_runtime`（保留） | 原 17 测试保留 |
| 17 | **MCP Evidence Index** | §44 §46 §47 §48 | 全局搜索 `evidence_index` → **0 命中** | 🔴 能力缺失 | `core/evidence_index.py` | `McpEvidenceEntry{used_in_final_answer}` | — | — | `round_NNN/mcp_evidence.jsonl` | `mcp_evidence_index{references[]}` | T16 |
| 18 | **Project Memory** | §49–§53 §67 | 未实现 | 🟢 **Phase 4.6，本轮不做**。仅埋目录约定，禁止自动写入 | — | — | — | — | `projects/<pid>/memory/`（**本轮不创建**） | — | 反向测试：Summary→PM 无自动路径 |
| 19 | **机器可读日志** | §57–§61 | `mcp_runtime_log.py:31` 文件名写死，事件仅 `tool_call`/`reconnect` | 🔴 缺 8 类事件；token 无 `source`/`accuracy_mode` | `core/context_log.py`（通用，不绑 MCP） | `LogEvent`（见 §17） | — | Context 状态面板读此日志 | `sessions/<sid>/context_policy.jsonl` | **10 类事件**（见 §17） | T16 |

**矩阵统计**：🔴 语义冲突 **13 项** ／ 🟠 能力缺失（含于 🔴）／ 🟢 保留 **3 项**（MCP Runtime、匿名化骨架、Project Memory 留白）

---

# 2. 旧架构 → 新架构迁移表

> 格式：`旧逻辑` → `为什么废弃` → `新逻辑` → `新逻辑由哪个模块负责`

| 旧逻辑 | 位置 | 为什么废弃 | 新逻辑 | 负责模块 |
|---|---|---|---|---|
| **`HISTORY_WINDOW = 3`** | `round_manager.py:46` | §54 明令禁止；§29 改为 budget 驱动。保留它则任何新逻辑都只是补丁 | **删除常量**。`ContextSelector` 从最新 Valid Round 向旧倒序，累计 token ≤ `remaining_lossless_budget` 则加入完整 Round | `core/context_selector.py` |
| **`range(1, HISTORY_WINDOW+1)` 循环** | `round_manager.py:453` | 同上，是常量的实际执行点 | 改为 `while remaining > 0 and idx >= 0` 倒序遍历 Valid Round Pool | `core/context_selector.py` |
| **`DEFAULT_COMPACTION_RATIO = 0.8`** | `context_budget.py:32` | §33"k 不代表什么时候触发 Compaction"。与 §20 的 k **同名不同义**，并存必然误导 | **删除**。k 只做预算分配，范围 0.01–0.90，默认 0.5 | `core/context_budget.py` |
| **`should_compact(estimated, context_limit, ratio)`** | `context_budget.py:73-85` | 就是"达到 80% 开始压缩"，§33 逐字禁止 | **删除**。压缩触发改为：Round 装不进 `remaining_lossless_budget` → 进压缩区 | `core/context_selector.py` |
| **固定 16000 output reserve** | （`real_provider.py` 用 models.yaml 静态值，等价固定） | §22"不应该自行规定这种固定值" | `reserved = min(model_max_output, request_max_output)`，三者分开记录，取不到记 `unknown` | `core/model_capability.py` |
| **旧 history loader `_load_history`** | `round_manager.py:449-466` | 硬窗口 + 只返回 answers + 无 Valid 抽象 | **重写**为 `ValidRoundPoolBuilder.build()` → `list[ValidRound]`（含 user_question + successful_answers + is_valid） | `core/valid_round.py` |
| **`_trim_to_budget`（丢弃最旧轮 / 截断内容）** | `round_manager.py:384+` | 违反 §1-1（信息不因超限被裁）与 §29。MD 要求超限进 Compaction，不是丢弃 | **删除该语义**。降级顺序改为 §27：current user → pin 保原文 → 递减 recent raw → 记 `Context Overflow` | `core/context_selector.py` |
| **旧 prompt builder `render_history`** | `prompt_builder.py:64-74` | 只渲染 answers；`build_round_n_prompt` 的 `user_instruction` 只传本轮 | **重写**：`render_history_item(item: HistoricalContextItem)` 同时渲染 `用户问题` + `你的回答/参与者回答` | `core/prompt_builder.py` |
| **旧 compactor `compactor.py`** | `core/compactor.py` 整个文件 | 触发语义错、单次、无 attempt、落盘与 Summary 混同 | **删除文件**，新建 `core/compaction.py`（Session 级共享压缩 + 递归 + `CompactionRecord`） | `core/compaction.py` |
| **旧 peer history `_entries_for_model`** | `round_manager.py:480-486` | 永远 `previous_answers`（上一轮 Final Answer），§30 明禁 | **重写**为 `PeerContextPreparator.prepare()`：按 `valid_answer_count` 分流 | `core/peer_context.py` |
| **`build_anonymous_context(previous_answers=…)`** | `anonymizer.py:78-83` | 参数名暗示"只会是 Final Answer"，与 §13 的 Summary 来源冲突 | **保留匿名化算法**，参数改名为 `peer_items: Sequence[PeerContextItem]`，由 item 自报 `mode` | `core/anonymizer.py` |
| **`input_budget_for()` 返回完整窗口** | `round_manager.py:54-71` | 与 §21 的 `history_budget`（已扣 fixed + reserved）**同名不同义** | **改名** `context_limit_for()`；`history_budget` 由 `ContextBudget` 单独给出 | `core/round_manager.py` |
| **`CHARS_PER_TOKEN = 3`（两份）** | `round_manager.py:43` / `context_budget.py:23` | §25 要求记录 `source` + `accuracy_mode`，裸整数会被当精确值 | **收敛为一份**，包进 `TokenEstimate{value, source, accuracy_mode}` | `core/context_budget.py` |
| **`RUNTIME_LOG_NAME = "mcp_runtime.jsonl"` 写死** | `mcp_runtime_log.py:31` | 文件名绑死 MCP，无法承载 §57 的 context_budget 等事件 | **保留 MCP 专用文件**；新建 `core/context_log.py` 写 `context_policy.jsonl`（通用事件） | `core/context_log.py` |
| **`load_responses` 靠 `.prompt.md` 后缀排除** | `storage.py:127-129` | 脆弱；文件名规则一变就把 prompt 当 answer 注入 | 新增 `load_round_bundle()` 显式读 `responses/` 子目录（§62）；旧路径保留兼容至迁移完成 | `core/storage.py` |
| **`RoundState.injected_rounds`** | `state.py:79` | 语义是"注入哪几轮"，无法表达 §59 要求的 `dropped_rounds` + `reason` | **扩展**：`selected_rounds` / `dropped_rounds` / `selection_reason`；`injected_rounds` 保留为别名一个版本后删 | `core/state.py` |
| **`config.max_output_tokens` 当模型能力** | `real_provider.py:225` | 静态配置伪装成模型能力（§22）；该字段**已从 models.yaml / ProviderConfig 删除**（§二） | 拆为 `model_max_output`（**只**来自 Provider 元数据）/ `request_max_output`（本次请求，仅 API / 脚本显式指定） | `core/model_capability.py` |
| **`_dynamic_max_tokens` 在 `remaining<=0` 时 raise** | `real_provider.py:217-223` | §27 要求"明确记录 Context Overflow / Cannot Fit"并降级，而非中断整轮 | Provider **不再自行 raise**，改为 clamp + 日志；Overflow 由 `ContextSelector` 在选路阶段处理 | `providers/real_provider.py` |

---

# 3. 真实数据流

## 3.1 普通对话（Peer Review OFF）

```
User 输入 instruction（+ 可选 k / selected_models）
  ↓
POST /api/session/{sid}/round  {instruction, selected_models, peer_review:false, k:0.5}
  ↓
app.py:run_round → rounds.prepare_round(...)
  ↓
RoundManager.execute_round(session_id, round_state, bus)
  ↓
【Round 级，执行一次】
  ├─ ModelCapabilityResolver.resolve(model_slug)
  │     → OpenRouter /api/v1/models（带缓存 + 失败降级）
  │     → ProviderModelMetadata{context_limit, model_max_output, fetch_status}
  │
  ├─ ValidRoundPoolBuilder.build(session, round_state)
  │     → 遍历 state.rounds：
  │         successful_answers = {m: ans for m in status if status[m]=="complete"}
  │         is_valid = len(successful_answers) > 0        # §2.1
  │         user_question = round.user_instruction        # §5
  │     → list[ValidRound]（升序），全失败 Round 不进 Pool（Runtime Log 仍保留）
  │
  ├─ PinStore.load(session_id) → PinState.pinned_round_ids
  │
  └─ PeerContextPreparator.prepare(session, round_state)   # peer_review=false → 空
  ↓
【Model 级，每个参与模型各执行一次】asyncio.gather 扇出
  ContextSelector.build_context(
      session, round_state, model_id=mid,
      valid_round_pool, pin_state, k, metadata, peer_items[mid]
  )
  ↓
  ┌─────────────────────────────────────────────────────────┐
  │ ① fixed_input_tokens = system + tools + current_user     │
  │ ② request_max_output = 请求配置 or models.yaml           │
  │ ③ reserved_output = min(model_max_output, request_max)   │
  │ ④ history_budget = context_limit − fixed − reserved  §21 │
  │ ⑤ lossless = history_budget × k                          │
  │    compaction = history_budget × (1−k)              §20  │
  │                                                          │
  │ ⑥ Pin 优先占用（不检查预算）                       §26    │
  │    pinned_tokens > lossless → warning，不解除/不压缩 §27  │
  │                                                          │
  │ ⑦ Recent Raw 倒序：                                 §29   │
  │    for r in reversed(valid_pool):                        │
  │        if r.tokens <= remaining: add to recent_raw       │
  │        else: 整个 r → compaction_zone（不拆）  §31/§13   │
  │                                                          │
  │ ⑧ compaction_zone 非空：                                 │
  │    已有可用 compact_NNN.md → 复用                        │
  │    否则 → CompactionService.compact(attempt=1)     §34    │
  │                                                          │
  │ ⑨ 最终校验 input + reserved ≤ context_limit        §24    │
  │    仍超限 → Recursive Compaction（见 §15）         §38    │
  └─────────────────────────────────────────────────────────┘
  ↓
ContextSelectionResult{
    pinned: list[HistoricalContextItem],
    recent_raw: list[HistoricalContextItem],
    compacted: CompactedBlock | None,
    peer_context: list[PeerContextItem],
    budget: ContextBudget,
    warnings: list[str],
}
  ↓
PromptBuilder.build(selection_result, current_user_question)
  ↓ （System + 当前问题 + Pin 块 + Recent Raw 块 + Compacted 块 + Peer 块）
Provider.generate(prompt, max_output_tokens=budget.reserved_output_tokens.value, tools=…)
  ↓
Final Answer（流式）
  ↓
Persistence：round_NNN/responses/<model>.md  +  round_NNN/<model>.prompt.md
  ↓
Logs：context_policy.jsonl（context_policy / context_budget / history_selection /
      memory_pin / compaction / peer_context_preparation）
      + round_NNN/mcp_runtime.jsonl（MCP，已有）
```

## 3.2 用户主动 Summary

```
User 勾选 [✓ Summary]
  ↓
【前端状态机，见 §18.2】
  1. 备份当前输入框内容 → draft_backup（仅首次勾选时）
  2. 清空输入框
  3. 填入 DEFAULT_SUMMARY_PROMPT（MD §9 原文 10 条）
  ↓
User 可编辑（"可修改 Prompt 再发送" §8）
  ↓
User 点发送
  ↓
POST /api/session/{sid}/round {instruction, summary:true, selected_models:[mid], peer_review:false}
  ↓
app.py → rounds.prepare_round(..., summary_mode=True)
  ↓
RoundManager.execute_round(...)
  ↓
SummaryService.generate(
    session_id, model_id=mid,
    trigger="user",                       # §41 三选一
    prompt=body.instruction,              # 用户可编辑后的实际文本
    source_rounds=<该模型自己参与的全部 Valid Round>，
)
  ├─ 组装输入：该模型自己的 Q + 自己的 A（不注入其他模型内容）§15
  ├─ Provider.generate(summary_prompt)    # 独立 LLM 调用
  ├─ 落盘 summaries/<mid>/summary_NNN.md  # §62，旧 Summary 不删除 §11
  └─ 更新 latest 指针
  ↓
ConversationSummary{summary_id, session_id, model_id, source_rounds,
                    prompt, content, created_at, trigger:"user",
                    status, input_tokens, output_tokens, file_path}
  ↓
保存（§11：旧 Summary 不删，仍属 Raw Archive）
  ↓
latest_conversation_summary = 本次（若 status=success）
  ↓
Log：conversation_summary{trigger:"user", source_rounds, input_tokens,
                          output_tokens, status}                §41
  ↓
UI：Summary 卡片独立渲染，标记为 conversation_summary，
    **不进入** 普通 final_answer 列表
```

**取消 Summary 的规则**（用户任务书 §十八要求"必须有明确规则并有测试"）：

| 场景 | 行为 |
|---|---|
| 勾选 → 从未编辑 → 取消 | 恢复 `draft_backup`（若非空）；否则清空 |
| 勾选 → 已编辑 → 取消 | **保留用户编辑后的文本**（不静默丢弃用户输入），仅关闭 Summary 模式标记 |
| 勾选 → 切换模型选择 → 取消 | 同上，按是否已编辑判定 |
| 发送后 | Summary 模式自动关闭，清空 `draft_backup`，输入框清空 |

## 3.3 Peer Review

```
Current Round，Peer Review = ON
  ↓
RoundManager.execute_round（Round 级，扇出之前执行一次）
  ↓
PeerContextPreparator.prepare(session, round_state)
  ↓
for each other_model in ACTIVE \ {current_model}:
  ↓
   valid_answer_count = 该模型在 Valid Round Pool 中
                        自己成功回答的**累计轮数**        §13
  ↓
  ┌──────────────────────────────┐
  │ valid_answer_count == 0      │
  │   → 不产生 PeerContextItem   │
  │     （无有效信息可给）        │
  ├──────────────────────────────┤
  │ valid_answer_count == 1      │
  │   → mode = final_answer      │
  │   → 直接读该唯一 Final Answer│
  │   → **不额外调用 Summary**   │  §13-A
  ├──────────────────────────────┤
  │ valid_answer_count > 1       │
  │   → mode = conversation_summary │
  │   → SummaryService.generate( │
  │        trigger="peer_review")│  §13-B / §14
  │   → 取最新成功 Summary        │
  │   → 若 Summary 失败 → 降级    │
  │     mode=final_answer(最新一轮)│
  │     并记 warning              │
  └──────────────────────────────┘
  ↓
PeerContextItem{
    source_model_id, mode, source_type,
    source_id,      # round_id 或 summary_id
    content,
    valid_answer_count,
    candidate_id,   # 匿名化后（复用 anonymizer）
}
  ↓
组装 Peer Context（当前模型视角）
  ↓
注入 ContextSelector → PromptBuilder → 当前模型
  ↓
当前模型生成 Final Answer
```

### 3.4 ⛔ 被禁止的自动传播路径（必须在数据中不存在）

```
Round N:  PeerContext[A] = B_summary
              ↓
          A Final Answer（受 peer context 影响）
              ↓  ✗ 禁止
Round N+1: PeerContext[C] = Round N 的 PeerContext 内容
```

**实现方式（数据层，非 Prompt 层）**：

1. Peer Context **不落盘为独立文件**（§17"默认不要求保留所有历史 Peer Context"）。
   → 磁盘上不存在可被回读的 peer context 实体。
2. `PeerContextPreparator` **无状态**：每次调用只从 Raw Archive（`round_NNN/responses/<model>.md`、`summaries/<model>/`）重新构建。
3. `ALLOWED_SOURCE_TYPES = frozenset({"final_answer", "conversation_summary"})` —— 代码级白名单，无第三种来源。
4. Peer Context 只出现在 `context_policy.jsonl` 的日志事件里，而日志是**只追加、不被 ContextSelector 读取**的。

> 关于 §43"peer_review_result = current_round_output 而不是 future_peer_memory"的解读：
> 模型在 peer-review 轮中产出的**仍然是该模型自己的 Final Answer**（§16 示例里明确称其为
> "Model A Final Answer"）。§43 禁止的是把它**另存为跨轮复用的 peer memory**，
> 而不是禁止它作为普通历史进入下一轮（后者由普通 Context 策略管辖，§16 允许）。
> 本设计通过"不落盘 + 无状态重建"满足该约束。此解读记入 §21 Custom Rule。

---

# 4. 数据结构定义

> 全部为 `@dataclass`，放 `core/`。命名与 MD §4 建议的 8 个概念对齐。

## 4.1 `TokenEstimate`（MD §25 / §58）

解决"不得把估算值伪装为精确值"。

```python
TokenSource = Literal[
    "provider_tokenizer",   # Provider/SDK 真实 tokenizer
    "openrouter_metadata",  # OpenRouter /api/v1/models 元数据
    "crossllm_estimator",   # CrossLLM len//CHARS_PER_TOKEN 估算
    "config_static",        # models.yaml 静态配置
    "provider_unavailable", # 取不到
]

AccuracyMode = Literal["exact", "estimated", "unknown"]

@dataclass(frozen=True)
class TokenEstimate:
    value: int | None              # 取不到时为 None，禁止填 0
    source: TokenSource
    accuracy_mode: AccuracyMode

    def __post_init__(self):
        # 不变量：value is None ⟺ accuracy_mode == "unknown"
        assert (self.value is None) == (self.accuracy_mode == "unknown")

    def to_log(self) -> dict:      # §58 三元组
        return {"value": self.value, "token_source": self.source,
                "token_exactness": self.accuracy_mode}
```

## 4.2 `ValidRound`（MD §2.1 / §4 / §5）

```python
@dataclass
class ValidRound:
    round_id: int
    user_question: str                      # §5 历史单位的一半
    successful_answers: dict[str, str]      # model_id -> Final Answer 原文
    failed_models: list[str]                # §4 失败模型不进 Context，但保留在记录里
    is_valid: bool                          # len(successful_answers) > 0
    token_estimate: TokenEstimate           # 整个 Round 的 token（Q + 全部成功 A）
    is_pinned: bool = False

    # 构造约束（§4）：
    #   A success / B timeout / C success  →  is_valid=True, successful={A,C}, failed=[B]
    #   全部失败                            →  is_valid=False，不进 Pool
```

## 4.3 `HistoricalContextItem`（MD §5 / §31）

进入 Prompt 的最小渲染单元。**Question 与 Answers 永不分离**。

```python
@dataclass
class HistoricalContextItem:
    round_id: int
    user_question: str
    answers: list[tuple[str, str]]   # (展示名, 原文)；展示名 = "你的回答" 或 匿名 Candidate-X
    source: Literal["pinned", "recent_raw"]
    token_estimate: TokenEstimate
```

## 4.4 `ContextBudget`（MD §21 / §24 / §57）

```python
@dataclass(frozen=True)
class ContextBudget:
    # —— 模型能力（§22）——
    context_limit: TokenEstimate
    # 模型**能**输出多少。来自 Provider 元数据
    # OpenRouter top_provider.max_completion_tokens。
    # ⚠ 命名统一（2026-09-25）：能力字段叫 model_max_output，
    #   旧名 provider_max_output_tokens / model_max_output_tokens 已废弃。
    #   与 request_max_output / reserved_output_tokens 构成一致的三元组。
    model_max_output: TokenEstimate
    # —— 本次请求（§22）——
    # CrossLLM **要求**本次最多输出多少。来自 API body 或 models.yaml。
    request_max_output: TokenEstimate
    # —— 实际预留（§22）——
    # 由「模型能力 + 本次请求配置」共同确定：min(provider, request)。
    # ⛔ 严禁退化为固定 16000 之类的全局常量。
    reserved_output_tokens: TokenEstimate
    # —— 固定输入（§21）——
    system_tokens: TokenEstimate
    tool_tokens: TokenEstimate          # 旧实现"故意忽略"，本版必须计入
    current_user_tokens: TokenEstimate
    peer_context_reserve: TokenEstimate
    fixed_input_tokens: TokenEstimate   # = system + tool + current_user + peer_reserve
    # —— 历史预算（§21）——
    history_budget: TokenEstimate       # = context_limit − fixed − reserved
    # —— k 分配（§20 / §33）——
    k: float                            # 0.01 ~ 0.90
    lossless_budget: TokenEstimate      # = history_budget × k
    compaction_budget: TokenEstimate    # = history_budget × (1 − k)
    # —— 实际占用（§57）——
    pinned_tokens: TokenEstimate
    recent_raw_tokens: TokenEstimate
    compacted_tokens: TokenEstimate
```

**三值关系不变量（Step 2 修订 #2，必须可测）**：

```python
# ① provider 与 request 必须分开存在，不允许合并成一个"max_output"
assert budget.model_max_output is not None
assert budget.request_max_output is not None

# ② reserved 由两者共同确定，且不超过任一者
if (p := budget.model_max_output.value) is not None:
    assert budget.reserved_output_tokens.value <= p
if (r := budget.request_max_output.value) is not None:
    assert budget.reserved_output_tokens.value <= r

# ③ reserved 不是常量 —— 若出现硬编码 16000 则该断言失败（见 T21）
assert budget.reserved_output_tokens.value != 16000 or (
    budget.reserved_output_tokens.value == min(
        budget.model_max_output.value or 16000,
        budget.request_max_output.value or 16000,
    )
)

# ④ 硬约束（§24）
assert (fixed + history_used + reserved) <= context_limit
```

> **T21 反固定值测试**：构造两个 `model_max_output` 不同的 fake 模型
> （如 32768 / 8192），断言两者的 `reserved_output_tokens` **不相等**。
> 若代码退回固定常量，两个值会相同 → 测试失败。

## 4.5 `PinState` / `PinEvent`（MD §18 / §19 / §26 / §27）

```python
@dataclass
class PinEvent:
    round_id: int
    pinned: bool
    timestamp: str
    reason: Literal["user"]      # §19 只由用户触发
    actor: str = "user"

@dataclass
class PinState:
    session_id: str
    pinned_round_ids: list[int]          # 有序，按 pin 时间
    history: list[PinEvent]              # §19 "必须保留历史变化记录"

    def is_pinned(self, round_id: int) -> bool: ...
```

**持久化**：`sessions/<sid>/pins.jsonl`，一行一个 `PinEvent`（追加写，与 §19 的日志要求天然一致）。
`PinState` 由事件流重放得到，**不做就地修改**。

## 4.6 `ProviderModelMetadata`（MD §22 / §23 / §25）

```python
@dataclass(frozen=True)
class ProviderModelMetadata:
    model_slug: str                      # "minimax/minimax-m2.7:free"
    context_limit: TokenEstimate         # OpenRouter context_length

    # ⚠ 命名统一（2026-09-25）：能力字段叫 model_max_output
    #   （旧名 provider_max_output_tokens 已废弃）。
    #   底层来源仍是 OpenRouter 的 top_provider.max_completion_tokens，
    #   但**字段名不再沿用 Provider 的原始命名**，改用语义命名，
    #   以便与 request_max_output / reserved_output_tokens 形成一致的三元组。
    model_max_output: TokenEstimate

    fetched_at: str
    fetch_status: Literal["ok", "cached", "failed", "not_fetched"]
    raw: dict | None = None              # 原始响应，便于排查
```

**命名映射表（实现时按此映射，禁止把 Provider 原始名直接当字段名）**：

| OpenRouter 原始字段 | CrossLLM 字段名 | 语义 |
|---|---|---|
| `top_provider.max_completion_tokens` | **`model_max_output`** | 模型**能**输出多少（Provider 报告的能力） |
| — | `request_max_output` | CrossLLM **要求**本次最多输出多少（API body / models.yaml） |
| — | `reserved_output_tokens` | 本次预算**实际预留**多少 = `min(provider, request)` |

**§22 元数据来源（已实测确认）**：

```
GET https://openrouter.ai/api/v1/models
  data[].id                                     → 匹配 model slug
  data[].context_length                         → context_limit
  data[].top_provider.context_length            → 备用（可能与顶层不同！）
  data[].top_provider.max_completion_tokens     → model_max_output
```

> **实测证据（2026-09-03）**：
> ```
> minimax/minimax-m2.7:free  context_length=196608  top_provider.max_completion_tokens=176947
> minimax/minimax-m2.5       context_length=204800  top_provider.context_length=200000
>                                                   top_provider.max_completion_tokens=128000
> ```
> 注意 minimax-m2.5 的**顶层与 top_provider 的 context_length 不一致**（204800 vs 200000）。
> **CrossLLM 规则 C9**：取两者 **min**（保守），并记录 `source=openrouter_metadata`。

**降级链**（§23"无法获得时记 unknown，不得伪装"）：

| 情况 | context_limit | model_max_output | accuracy_mode |
|---|---|---|---|
| 元数据获取成功 | min(顶层, top_provider) | `top_provider.max_completion_tokens` | `exact`（来自 Provider）→ 记 `source=openrouter_metadata` |
| 元数据缓存命中（网络失败但有缓存） | 缓存值 | 缓存值 | `exact` + `fetch_status=cached` |
| 元数据获取失败，无缓存 | models.yaml `context_limit`（fallback） | **unknown**（models.yaml 不提供输出能力，§二） | **`unknown`** |
| models.yaml 也缺 | `None` | `None` | **`unknown`** + `source=provider_unavailable` |

## 4.7 `ConversationSummary`（MD §11 / §41）

```python
SummaryTrigger = Literal["user", "peer_review", "compaction"]

@dataclass
class ConversationSummary:
    # ── 身份 ──────────────────────────────────────────────
    summary_id: str              # "summary_001"
    session_id: str
    model_id: str                # 模型级（§53："这个模型到目前为止怎么理解"）
                                 # ⚠ 必须按 model_id 隔离 —— 修订 #1
    # ── Derived Artifact 标记（修订 #4）──────────────────
    # Summary 是对已有 Round 的派生视图，**不是新的一轮**。
    is_derived_artifact: bool = True        # 恒为 True，禁止构造时传 False
    creates_round: bool = False             # 恒为 False
    contributes_valid_round_count: bool = False  # 恒为 False

    # ── 来源与复用（修订 #3）────────────────────────────
    source_rounds: list[int]     # §41：参与总结的 Round 列表
    source_valid_round: int      # 覆盖到的**最新有效 Round id** ← 复用判断的唯一依据

    # ── 内容 ─────────────────────────────────────────────
    prompt: str                  # 实际使用的 Prompt（用户可编辑 / 系统默认）
    content: str
    created_at: str
    trigger: SummaryTrigger
    status: Literal["success", "failed"]

    # ── 度量 ─────────────────────────────────────────────
    input_tokens: TokenEstimate
    output_tokens: TokenEstimate
    file_path: str | None        # §60 要求 summary_file 可追溯
    error_message: str | None = None

    # 复用标记：本次是新建还是复用了上一条（T18 断言用）
    reused_from: str | None = None   # 复用时填被复用的 summary_id
```

### 4.7.1 Derived Artifact 不变量（修订 #4）

```python
def __post_init__(self):
    # Summary 永远不是新的 Round
    assert self.is_derived_artifact is True
    assert self.creates_round is False
    assert self.contributes_valid_round_count is False
    # 只能派生自**已有**的 Round，不能凭空产生
    assert self.source_rounds, "Summary 必须派生自至少一个 Round"
    assert self.source_valid_round == max(self.source_rounds)
```

**由此推出的三条硬性后果**：

| # | 后果 | 落地约束 |
|---|---|---|
| **D1** | Summary **不参与 Round 编号** | 生成 Summary 时 `session.current_round` **不变**；不创建 `round_NNN/` 目录 |
| **D2** | Summary **不增加 Valid Round Count** | `ValidRoundPoolBuilder` 只扫 `state.rounds`，**不扫** `summaries/`；因此 `valid_answer_count` 不受 Summary 影响 |
| **D3** | Summary **不产生 Final Answer** | 不写 `round_NNN/responses/<model>.md`；只写 `summaries/<model>/summary_NNN.md` |

> **为什么必须显式声明**：§13 的 Peer Context 分流依赖 `valid_answer_count`。
> 若 Summary 被计入 Round / Valid Round Count，则每生成一次 Summary 就会让
> `valid_answer_count` +1，导致 Peer Context 在 `final_answer` 与 `conversation_summary`
> 之间来回跳变，且会形成"Summary 触发更多 Summary"的自激循环。

### 4.7.2 Summary 复用规则（修订 #3）

> 目标：**避免 Peer Review 每轮重复从头总结，控制成本**。

```python
class SummaryReusePolicy:
    def should_reuse(
        self, latest_summary: ConversationSummary | None,
        current_latest_valid_round: int | None,
    ) -> tuple[bool, str]:
        """
        返回 (是否复用, 理由)。理由写进 conversation_summary 日志的 reused_reason。
        """
        if latest_summary is None:
            return False, "no_existing_summary"
        if latest_summary.status != "success":
            return False, "latest_summary_failed"
        if current_latest_valid_round is None:
            return False, "no_valid_round"
        if latest_summary.source_valid_round >= current_latest_valid_round:
            # 已有 Summary 已覆盖当前最新有效 Round → 直接复用
            return True, "source_valid_round_covered"
        # 出现了新的有效 Round → 才重新生成
        return False, "new_valid_round_appeared"
```

**判定表**：

| 场景 | `latest.source_valid_round` | `current_latest_valid_round` | 行为 | 日志 `reused` |
|---|---|---|---|---|
| 首次生成 | — | 5 | 新建 | `false`（reason=`no_existing_summary`） |
| **无新 Round**（连续两轮 Peer Review 之间没产生新有效回答） | 5 | 5 | **复用** | `true`（reason=`source_valid_round_covered`） |
| **有新 Round** | 5 | 7 | 重新生成 | `false`（reason=`new_valid_round_appeared`） |
| 上一条失败 | 5（failed） | 5 | 重新生成 | `false`（reason=`latest_summary_failed`） |
| 用户主动触发（`trigger="user"`） | 任意 | 任意 | **总是新建**（用户明确要求重新整理） | `false`（reason=`user_explicit`） |

> **`trigger="user"` 例外**：§8 说用户可以主动生成 Summary，
> 若此时复用旧结果会让用户困惑（"我点了总结但没变化"）。故用户触发**绕过复用**。

**成本效果**（修订 #3 的直接收益）：

```
未复用：  Round N peer review → 对 B 全量重算 Summary（~15K input tokens）
          Round N+1 peer review → 再全量重算一次（B 的历史没变，纯浪费）

复用后：  Round N   → 生成 summary_001（source_valid_round=19）
          Round N+1 → valid_round 仍是 19 → 复用，0 次 LLM 调用
          Round N+2 → 出现 valid_round 20 → 重算 summary_002
```

**落盘时 `latest` 指针更新规则**：只有 `status == "success"` 才更新 latest；
复用时**不产生新文件**，`latest` 仍指向被复用的那条，`reused_from` 记本次引用。

## 4.8 `PeerContextItem`（MD §13 / §42）

```python
PeerContextMode = Literal["final_answer", "conversation_summary"]

@dataclass(frozen=True)
class PeerContextItem:
    source_model_id: str
    mode: PeerContextMode
    source_type: PeerContextMode     # 与 mode 同值；冗余字段用于日志自证
    source_id: str                   # "round_019" 或 "summary_003"
    content: str
    valid_answer_count: int
    candidate_id: str                # 匿名化后的展示名
    created_at: str
```

## 4.9 `CompactionRecord` / `CompactedBlock`（MD §36 / §39 / §60）

```python
@dataclass
class CompactionRecord:
    attempt: int                     # §38 递归次数
    source_rounds: list[int]
    source_summary_files: list[str]  # 递归时上一轮的产物

    # ── 来源语义保留（修订 #1）────────────────────────────
    # Session Compaction 是共享的，因此**必须**记录它混合了哪些模型的观点，
    # 否则压缩产物会冒充成"某个模型自己的 Summary"，违反 §37。
    contributor_models: list[str]        # 参与压缩区的模型 id 列表
    viewpoint_attribution: bool          # 压缩文本是否保留了观点归属标注
                                         # （必须 True；False 视为压缩失败并降级）
    artifact_kind: str = "session_compaction"   # 与 "conversation_summary" 区分

    input_tokens: TokenEstimate
    expected_summary_tokens: TokenEstimate    # §40 预算估算
    actual_summary_tokens: TokenEstimate
    summary_file: str | None
    status: Literal["success", "failed"]
    observed_compression_ratio: float | None  # §39：仅作预算估计，不作理论比例
    error_message: str | None = None

@dataclass
class CompactedBlock:
    content: str                     # 压缩后文本
    source_files: list[str]
    records: list[CompactionRecord]  # 递归时有多条
    contributor_models: list[str]    # 向上冒泡，供 Prompt 渲染时声明
    is_shared: bool = True           # Session 级共享标记
```

### 4.9.1 Session Compaction ≠ Conversation Summary（修订 #1，核心区分）

| 维度 | **Conversation Summary** | **Session Compaction** |
|---|---|---|
| **隔离粒度** | **按 `model_id` 严格隔离** —— 每个模型一份，互不可见 | **Session 级共享** —— 所有模型共用一份 |
| **表达对象** | 该模型**自己**的工作状态（"我是怎么理解的"） | 压缩区内**多个模型**的历史（"当时讨论了什么"） |
| **人称** | 第 2 人称（"你已经形成的观点…"，§9） | 第 3 人称 + **归属标注**（"参与者 A 认为…；参与者 C 反对…"） |
| **能否冒充对方** | — | ⛔ **绝不能**。共享产物不得被任何模型当作自己的 Summary |
| **落盘** | `summaries/<model>/summary_NNN.md` | `compact_<K>.md`（Session 根，与 `summaries/` 严格分目录） |
| **字段标记** | `artifact_kind="conversation_summary"` | `artifact_kind="session_compaction"` |
| **更新 latest** | ✅ 更新 `latest_conversation_summary` | ❌ 从不更新（不是工作状态） |

**为什么共享的 Compaction 必须保留来源语义**（这是修订 #1 的关键要求）：

```
❌ 错误示范（观点被抹平、归属丢失）：
   "结论：应采用方案 B。"
   → 读者无法知道这是 A 的观点、C 的反对，还是用户的决策。
   → 若该文本被某模型当作"自己之前的判断"，就构成冒充。

✅ 正确示范（保留观点与来源）：
   "## 讨论中的观点分布
    - 参与者 A：倾向方案 B，依据是理论分析（未实验验证）
    - 参与者 C：反对方案 B，理由是显存不足
    - 用户决策：采用方案 B（Round 7）
    → 归属清晰，任何模型都不会误认为是自己的既有判断"
```

**落地约束**：

1. `COMPACTION_PROMPT` 必须包含显式指令：*"保留每个观点的来源归属，使用'参与者 X 认为'句式，
   不得将多方观点合并为无主语的陈述句"*。
2. `CompactionRecord.viewpoint_attribution` 生成后置校验：
   若压缩文本中**没有**出现任何归属标记（参与者名 / Candidate 编号 / 模型发言标记），
   则记 `viewpoint_attribution=False` + `status="failed"`，走降级路径（§14.5）。
3. Prompt 渲染时，Compacted 块的标题必须声明来源：
   `## 历史压缩摘要（Session 级共享，含参与者 A/B/C 的观点，非本模型个人总结）`

### 4.9.2 目录隔离（防止混同的物理保证）

```
sessions/<sid>/
├── summaries/                  ← Conversation Summary（模型级，隔离）
│   ├── model_a/summary_001.md
│   └── model_b/summary_001.md
├── compact_001.md              ← Session Compaction（共享）
└── compact_002.md
```

两个目录**没有任何代码路径互相写入**（T22 断言）。

## 4.10 `McpEvidenceEntry`（MD §46 / §47）

```python
@dataclass
class McpEvidenceEntry:
    mcp_name: str
    tool_name: str
    tool_call_id: str                # 回指 mcp_runtime.jsonl 的定位键
    reference: str                   # 人类可读定位，如 "Issue #123"
    used_in_final_answer: bool       # §46 核心字段
```

## 4.11 `ContextSelectionResult`（本 Plan 新增，MD §63 的最终 Context 结构）

```python
@dataclass
class ContextSelectionResult:
    model_id: str
    current_user_question: str
    pinned: list[HistoricalContextItem]
    recent_raw: list[HistoricalContextItem]
    compacted: CompactedBlock | None
    peer_context: list[PeerContextItem]
    budget: ContextBudget
    warnings: list[str]              # §27 pinned_history_exceeds_budget / context_overflow
    selected_rounds: list[int]       # §59
    dropped_rounds: list[int]        # §59 —— 不表示删除，只表示未进 Context
    selection_reason: str
```

---

# 5. Summary 语义边界（MD §6 / §7 / §10）

## 5.1 四重不等号

```text
ConversationSummary ≠ Fact Database
ConversationSummary ≠ Project Memory
ConversationSummary ≠ Peer Review
ConversationSummary ≠ Compaction
```

## 5.2 Summary 允许且必须能表达的内容

| 允许 | 说明 |
|---|---|
| 观点 / 判断 / 倾向 | §7"我的判断 / 倾向" |
| 推测 / 假设 | §7"未经验证" |
| 争议 | §7"存在争议" |
| 未验证内容 | §10 明确允许 |
| 已尝试 / 已排除 | §9 第 6 条 |
| 未解决问题 | §9 第 7 条 |

## 5.3 明确禁止

```text
❌ Model Opinion → Confirmed Fact          （§6"尤其禁止"，§9 第 4 条）
❌ Summary → 自动永久 Project Memory        （§49 / §67）
❌ Compaction → 自动 Project Memory         （§52）
❌ Peer Review → 自动 Project Memory        （§67）
❌ 强制 schema：Facts + Decisions + Experiments + Rejected + Open Questions（§7 明禁）
```

## 5.4 字段为 optional，非强制

MD §7 推荐的语义结构（当前理解 / 我的判断 / 依据 / 存在争议 / 未经验证 / 已经尝试 / 已经排除 / 未解决问题）
**在 Prompt 中作为"推荐表达顺序"给出，不作为输出 schema 校验**。理由（§7）：

> 很多真实对话只有一个观点、一个假设、一个尚未验证的推断。
> 强制 schema 会把"我认为 X 可能更好"扭曲成"Fact: X 优于 B"。

**代码层面**：`ConversationSummary.content` 是**自由文本**，`status` 只有 `success`/`failed`，
**没有** `facts[]` / `decisions[]` 这样的结构化字段。这是刻意的——结构化只在 Project Memory（§50）出现。

## 5.5 默认 Summary Prompt（MD §9 原文，逐字采用）

```
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

- 常量名：`DEFAULT_SUMMARY_PROMPT`，位置 `core/summary_prompt.py`
- **默认可编辑**（§9"系统不要求用户必须使用默认 Prompt"）
- 存在独立文件而非内联，是为了让 Test 14 能直接断言"Prompt 含第 4 条反升格指令"

## 5.6 Conversation Summary = Derived Artifact（修订 #4）

> Summary 是对**已有 Round** 的派生视图，**不是新的一轮**。

```text
Raw Rounds (R1..R19)  ──派生──►  ConversationSummary
                                 （不产生 R20，不计入 Valid Round Count）
```

**三条硬性不变量**（已在 §4.7.1 落到 `__post_init__` 断言）：

| # | 不变量 | 违反后果 |
|---|---|---|
| **D1** | 不参与 Round 编号 | `session.current_round` 不变；不创建 `round_NNN/` |
| **D2** | 不增加 Valid Round Count | `valid_answer_count` 不受影响（§13 分流依赖它） |
| **D3** | 不产生 Final Answer | 不写 `round_NNN/responses/<model>.md` |

**为什么必须显式声明**（自检用的因果链）：

```
若 Summary 计入 Valid Round Count：
  生成 summary_001 → valid_answer_count +1
                   → §13 分流从 final_answer 跳到 conversation_summary
                   → 下一轮 Peer Review 又生成 summary_002
                   → valid_answer_count 再 +1
                   → ……形成"Summary 触发更多 Summary"的自激循环
```

**持久化隔离**（物理保证）：

```
sessions/<sid>/
├── round_001/ … round_019/     ← Round 与 Final Answer（参与编号与计数）
└── summaries/model_a/          ← Derived Artifacts（不参与、不计数）
```

`ValidRoundPoolBuilder` **只扫 `state.rounds`**，不扫 `summaries/` → 结构上无法把 Summary 计成 Round。

## 5.7 Conversation Summary ≠ Session Compaction（修订 #1）

两者是**不同的产物类型**，不能互相冒充。完整区分表见 **§4.9.1**，此处只记三条最容易踩的红线：

```text
⛔ 红线 1：Session Compaction 不得被任何模型当作"自己的 Summary"
           → 压缩块在 Prompt 中必须显式声明来源与参与者范围

⛔ 红线 2：Session Compaction 必须保留观点归属
           → "结论：应采用方案 B"（无主语）不合法
           → "参与者 A 认为应采用 B；参与者 C 反对"（有归属）才合法
           → viewpoint_attribution=False 视为压缩失败，走降级

⛔ 红线 3：两者目录不得互写
           → summaries/<model>/  与  compact_<K>.md 之间无代码路径
```

---

# 6. Summary 与 Compaction 的关系（MD §37）

> `Same mechanism family ≠ Same lifecycle`
>
> **修订 #1 补充**：二者不仅是"生命周期不同"，更是**隔离粒度与归属语义不同**。
> Conversation Summary **按 `model_id` 严格隔离**；Session Compaction **可以共享**，
> 但共享的前提是**保留不同模型的观点与来源语义**——否则就是冒充。

| 维度 | Conversation Summary | Session Compaction |
|---|---|---|
| **目的** | 形成模型自己的工作状态 | 让旧历史占用更少 Context |
| **隔离粒度** | **按 `model_id` 严格隔离**（每模型一份） | **Session 级共享**（所有模型共用一份） |
| **Trigger** | `user` / `peer_review` | `compaction` |
| **触发者** | 用户主动 / Peer Review 自动 | Context Budget 自动 |
| **输入** | 该模型**自己**的历史（Q + 自己的 A） | compaction_zone 内的**全部** Valid Rounds（多模型） |
| **Prompt** | `DEFAULT_SUMMARY_PROMPT`（§9 十条，**第 2 人称**"你已经形成"） | `COMPACTION_PROMPT`（§36 结构，**第 3 人称 + 归属标注**） |
| **观点归属** | 天然单一（都是"我"的观点） | ⚠ **必须显式标注归属于哪个参与者**，否则视为失败 |
| **产物性质** | **Derived Artifact**（不产生 Round，不计数） | Context 压缩表示（同样不产生 Round） |
| **落盘** | `summaries/<model>/summary_NNN.md` | `compact_<K>.md`（Session 根） |
| **`artifact_kind`** | `conversation_summary` | `session_compaction` |
| **更新 latest** | ✅ 更新 `latest_conversation_summary` | ❌ **从不更新**（共享产物不是任何模型的工作状态） |
| **失败影响** | Summary 卡片显示失败，不影响对话 | 降级：保留更多 raw / 记 warning，不阻断 Round |

## 6.0 为什么共享的 Compaction 不会冒充成模型自己的 Summary

| 防线 | 机制 |
|---|---|
| **目录隔离** | `summaries/<model>/` vs `compact_<K>.md`，无互写代码路径（T22） |
| **字段标记** | `artifact_kind` 不同值，类型层可区分 |
| **latest 指针** | Compaction **从不**更新 `latest_conversation_summary` → Peer Context 的 `source_id` 永远不可能是 `compact_*` |
| **Prompt 声明** | 压缩块标题强制写明"Session 级共享，含参与者 X/Y/Z 的观点，非本模型个人总结" |
| **归属校验** | `viewpoint_attribution=False` → 压缩失败降级（§14.5） |

## 6.1 复用什么

| 复用项 | 说明 |
|---|---|
| **LLM 调用骨架** | `compactor.py:105-110` 的 `provider.generate()` 流式 chunk 收集 → 提取为 `SummaryLLMCaller.call()`，两者共用 |
| **原子写** | `storage.atomic_write_text` |
| **文件名自增** | `_next_index(dir, prefix, suffix)` 工具函数 |
| **TokenEstimate 包装** | 输入/输出 token 都带 `source` + `accuracy_mode` |

## 6.2 不复用什么（必须分开）

| 项 | 原因 |
|---|---|
| **Prompt 模板** | §9（第 2 人称、个人工作状态） vs §36（第 3 人称、多模型旧历史）。共用会混淆"谁的总结" |
| **落盘目录** | 混同会导致 `latest_conversation_summary` 指到压缩产物上——正是 §37 禁止的 |
| **trigger 字段** | §41 要求日志能区分 user / peer_review / compaction |
| **粒度** | 模型级 vs Session 级，见上表 |

## 6.3 ❌ 明确禁止的做法

> "直接把现有 `compactor.py` 改个名字然后称为 Summary 功能"

`core/compactor.py` **整个删除**，新建两个文件：`core/summary_service.py` + `core/compaction.py`。

---

# 7. Peer Context Preparation（MD §13 / §14 / §30）

## 7.1 接口

```python
class PeerContextPreparator:
    ALLOWED_SOURCE_TYPES = frozenset({"final_answer", "conversation_summary"})

    async def prepare(
        self,
        session: SessionState,
        round_state: RoundState,
        valid_pool: list[ValidRound],
        summary_service: "SummaryService",
    ) -> dict[str, list[PeerContextItem]]:
        """返回 {current_model_id: [其他模型的 PeerContextItem]}。无状态，每次重建。"""
```

## 7.2 输入 / 输出

| 项 | 内容 |
|---|---|
| **输入** | `source_model`（其他 ACTIVE 模型）、`active_models`、`valid_answer_count` |
| **输出** | `PeerContextItem{mode, source_type, source_id, content, valid_answer_count}` |

## 7.3 规则（写死，§13）

```python
if valid_answer_count == 0:
    return None                      # 无有效信息，不产生 item
elif valid_answer_count == 1:
    mode = "final_answer"            # 不额外调用 Summary
    content = 该唯一 Final Answer 原文
    source_id = f"round_{rid:03d}"
else:                                # > 1
    mode = "conversation_summary"

    # ── 修订 #3：Summary 复用规则，避免每轮重复从头总结 ──
    current_latest_valid_round = max(vr.round_id for vr in valid_pool)
    latest = summary_service.get_latest(source_model)    # 按 model_id 隔离查
    reuse, reason = SummaryReusePolicy().should_reuse(
        latest_summary=latest,
        current_latest_valid_round=current_latest_valid_round,
    )
    if reuse and latest is not None:
        summary = latest
        log("conversation_summary", reused=True, reused_reason=reason,
            source_valid_round=latest.source_valid_round)
    else:
        summary = await summary_service.get_or_create(
            model_id=source_model, trigger="peer_review",
            source_rounds=<source_model 自己的全部 Valid Rounds>,
            force_new=(reason == "new_valid_round_appeared"),
        )
        log("conversation_summary", reused=False, reused_reason=reason, …)
    # ── 修订 #3 结束 ──

    if summary is None:              # Summary 失败降级
        mode = "final_answer"
        content = 最新一轮 Final Answer
        warnings.append("peer_summary_failed_degraded_to_final_answer")
    else:
        content = summary.content
        source_id = summary.summary_id
```

**复用 vs 重新生成的成本差**（修订 #3 量化收益）：

```
无复用：  Round N   → 1 次 LLM 调用（~15K input tokens）
          Round N+1 → 1 次 LLM 调用（~15K input tokens，无新内容）
          Round N+2 → 1 次 LLM 调用（~15K input tokens，无新内容）
          合计：3 次 LLM 调用 / 3 轮 ≈ 45K input tokens

有复用：  Round N   → 1 次 LLM 调用（生成 summary_001）
          Round N+1 → 0 次（latest.source_valid_round 已覆盖）
          Round N+2 → 0 次（同上）
          出现新 valid_round 时 → 1 次 LLM 调用
          合计：1~2 次 LLM 调用 / 3 轮 ≈ 15~30K input tokens
```

**边界（必须在代码里写死的）**：

```python
# user trigger 不复用 —— 用户明确点"总结"必须有新动作
if trigger == "user":
    reuse = False
    reason = "user_explicit"

# 只有 status=success 的 latest 才能被复用
if latest and latest.status != "success":
    reuse = False
    reason = "latest_summary_failed"
```

## 7.4 不再允许的表述

```text
❌ Peer Context = 上一轮其他模型 Final Answer     （§30 明禁）
❌ 默认上一轮 Final Answer                        （用户任务书 §八）
✅ Peer Context = PeerContextPreparator.prepare() 的输出
```

## 7.5 复用而非新建

- ✅ **复用** `SummaryService`（§14"复用已有 Summary 能力"）
- ❌ **禁止** `PeerMemoryGenerator` / `PeerSummaryGenerator` 之类平行系统（§67"不再建立另一套独立 Memory Framework"）

---

# 8. Peer Review 隔离（MD §15 / §16 / §43）

> 用户任务书 §九：**必须设计成数据层约束，不能只靠 Prompt 告诉模型**。

## 8.1 三层防御

| 层 | 机制 | 证明方式 |
|---|---|---|
| **L1 存储层** | Peer Context **不落盘为独立文件**。磁盘上不存在可回读实体 | 测试断言 `round_NNN/` 下无 `peer_context*` 文件 |
| **L2 构建层** | `PeerContextPreparator` 无状态，只从 Raw Archive 重建；`ALLOWED_SOURCE_TYPES` 白名单 | 测试断言 `source_type ∈ ALLOWED_SOURCE_TYPES` |
| **L3 日志层** | `peer_context_preparation` 事件记录 `mode` + `source_id`，可回溯审计 | 测试读日志断言 `source_id` 以 `round_` 或 `summary_` 开头 |

## 8.2 代码级白名单

```python
class PeerContextPreparator:
    # 唯一合法的两种来源。没有第三种。
    ALLOWED_SOURCE_TYPES = frozenset({"final_answer", "conversation_summary"})

    def _resolve_source(self, source_type: str, source_id: str) -> str:
        if source_type not in self.ALLOWED_SOURCE_TYPES:
            raise ValueError(
                f"非法 Peer Context 来源: {source_type!r}。"
                f"合法来源: {sorted(self.ALLOWED_SOURCE_TYPES)}。"
                "Peer Review Result 不得作为 Peer Context 来源（phase4 §16/§43）。"
            )
        ...
```

**关键点**：即使数据库里存了 Peer Review Result，`PeerContextPreparator` 也**没有 loader 能读它**——
因为没有对应的 `source_type` 分支。

## 8.3 测试要求（用户任务书 §九）

```python
def test_peer_review_result_never_becomes_next_peer_context():
    # 前置：Round N 开启 peer_review，A 看到 B 的 summary，产出 A 的回答
    # 断言：Round N+1 为 C 构建 peer context 时，
    #      所有 PeerContextItem.source_type ∈ {"final_answer", "conversation_summary"}
    #      且没有任何 item 的 source_id 指向 "peer_context"
    #      且磁盘上不存在 peer_context 持久化文件
```

## 8.4 相邻约束：Peer Review 不写回其他模型 Summary（§15）

`SummaryService.generate(model_id=M)` 的输入**只包含 M 自己的历史**：

```
inputs = [ (round.user_question, round.successful_answers[M])   for round in source_rounds if M in round.successful_answers ]
```

不包含：
- ❌ 其他模型的 Final Answer
- ❌ 其他模型的 Summary
- ❌ M 自己曾经收到的 Peer Context

→ 从数据上杜绝 `A Summary → B Review → A Summary` 反馈循环（§15）。

---

# 9. 输出预算：模型级，非全局常量（MD §22 / §23）

## 9.1 数据流

```
OpenRouter /api/v1/models          models.yaml            API 请求体
        ↓                              ↓                      ↓
  model_max_output    request_max_output     request_max_output(可选)
  context_limit (顶层/tp_provider)  (兜底)                request_max_output(可选)
        ↓                              ↓                      ↓
        └──────────────► ModelCapabilityResolver ◄───────────┘
                                  ↓
                     ProviderModelMetadata{model_max_output}
                                  ↓
              reserved_output_tokens = min(provider, request)

⚠ 命名统一（2026-09-25，与 §八 日志字段一致）：
   `model_max_output` / `request_max_output` / `reserved_output_tokens`
   三件套；旧名 `provider_max_output_tokens`、`model_max_output_tokens`、
   `request_max_output_tokens` 全部废弃。
```

## 9.2 计算规则（§22 逐条落地）

```python
# 1) 模型能力上限（Provider 元数据优先）—— ⚠ 改名 #2
provider_max = metadata.model_max_output
               # 取不到 → 降级链：config_static / unknown

# 2) 本次请求上限 —— 只来自 API / 脚本**显式**指定；未指定 = unknown
#    注：models.yaml 已不再提供任何输出能力字段（§二），故没有 cfg.* 兜底。
request_max = TokenEstimate(
    body.request_max_output,
    source="config_static",
    accuracy_mode="estimated",
)

# 3) 有效值：min(provider, request)，由「模型能力 + 本次请求配置」共同确定
#    ⛔ 严禁退化为固定 16000 之类的全局常量（修订 #2 核心）
if provider_max.value is None:
    reserved = TokenEstimate(request_max.value, request_max.source, "estimated")
    warnings.append("provider_max_output_unknown_using_request_limit")
elif request_max.value is None:
    reserved = provider_max
else:
    reserved_value = min(provider_max.value, request_max.value)
    # 精度取两者最低
    accuracy = "exact" if provider_max.accuracy_mode == "exact" else "estimated"
    reserved = TokenEstimate(reserved_value, "provider_metadata+request", accuracy)
    if request_max.value > provider_max.value:
        warnings.append("request_max_output_exceeds_provider_capability_clamped")
```

## 9.3 三者必须分开记录（§22"第一版必须把…分开记录"，修订 #2）

| 字段 | 含义 | 来源 | accuracy |
|---|---|---|---|
| **`model_max_output`** | 模型**能**输出多少 | OpenRouter `top_provider.max_completion_tokens` | `exact` / `estimated` / `unknown` |
| **`request_max_output`** | CrossLLM **要求**本次最多输出多少 | API body / models.yaml | `exact` |
| **`reserved_output_tokens`** | 本次预算**实际预留**多少 | `min(provider, request)` | 随参与计算的最低精度 |

> **字段名（2026-09-25 统一，与 §八 日志一致）**：
> `model_max_output` / `request_max_output` / `reserved_output_tokens`。
> 旧名 `provider_max_output_tokens` / `model_max_output_tokens` /
> `request_max_output_tokens` 全部废弃。
> `max_completion_tokens`（OpenRouter 元数据字段）与 `max_output_tokens`
> （Provider Protocol 参数）只是**底层 Provider/API 参数名**，
> 不是 CrossLLM 的抽象能力/预算字段，也不是 models.yaml 字段。

## 9.4 reasoning tokens 注意事项（§23）

> 不假设 `16K completion == 16K visible answer`。

- `reserved_output_tokens` 按 **completion budget** 预留，与可见文本长度无关。
- 若 Provider 返回 `usage.completion_tokens_details.reasoning_tokens`，记入 `context_policy.jsonl`。
- 预算计算**不使用**任何 reasoning 折减系数（无法预先知道）。

## 9.5 缓存

`ModelCapabilityResolver` 带进程内缓存 + 磁盘缓存 `.model_metadata_cache.json`（含 `fetched_at`，TTL 24h）。
缓存命中记 `fetch_status=cached`，**accuracy 不变**（`exact`）。

---

# 10. Context Budget 公式与调用位置（MD §21 / §24）

## 10.1 公式（逐字对应 §21）

```python
fixed_input_tokens = system_tokens + tool_tokens + current_user_tokens + peer_context_reserve

history_budget = context_limit − fixed_input_tokens − reserved_output_tokens

lossless_budget   = history_budget × k
compaction_budget = history_budget × (1 − k)
```

## 10.2 硬约束（§24）

```python
assert input_tokens + reserved_output_tokens <= context_limit
```

**校验点**：`ContextSelector.build_context()` 末尾。失败 → 触发 Recursive Compaction（§15）。

## 10.3 调用位置（明确，非"某处"）

| 步骤 | 位置 | 说明 |
|---|---|---|
| `fixed_input_tokens` 计算 | `core/context_selector.py::ContextSelector._estimate_fixed_input()` | 在 `_run_one` 内、prompt 拼装**之前** |
| `history_budget` / `lossless` / `compaction` | `core/context_budget.py::compute_budget()` | 纯函数，无 I/O，易测 |
| Pin 占用 | `core/context_selector.py::_apply_pins()` | lossless 之内优先扣减 |
| Recent Raw 装载 | `core/context_selector.py::_fill_recent_raw()` | 倒序 while，非 for-N |
| 最终校验 | `core/context_selector.py::_validate_and_compact()` | 递归压缩入口 |
| 传给 Provider | `providers/real_provider.py::generate(max_output_tokens=…)` | 由 `ContextSelectionResult.budget.reserved_output_tokens` 提供 |

## 10.4 ❌ 再次确认禁止的旧语义

```text
❌ context >= 80% → compact
❌ k = Compaction Trigger Ratio
❌ k = 总 Context 使用比例
```

`k` 在本设计中**只出现在** `lossless_budget = history_budget × k` 这一行。
代码中不允许出现任何 `k` 与"触发"相关的比较。

---

# 11. Context Selection：取消硬窗口（MD §29 / §54 / §64）

## 11.1 删除确认

| 项 | 动作 |
|---|---|
| `HISTORY_WINDOW = 3`（`round_manager.py:46`） | **删除常量** |
| `for offset in range(1, HISTORY_WINDOW + 1)`（`:453`） | **删除循环**，改 while |
| `scripts/run_phase4_e2e.py:46` 的 import | 同步删除 |
| `prompt_builder.py:6` "最近 3 轮"注释 | 同步更新 |

## 11.2 动态算法

```python
def _fill_recent_raw(self, valid_pool, remaining_lossless, pin_ids):
    recent, dropped = [], []
    for vr in reversed(valid_pool):              # 由新 → 旧
        if vr.round_id in pin_ids:
            continue                              # Pin 已在上一阶段占用，不重复计
        if vr.token_estimate.value is not None and vr.token_estimate.value <= remaining_lossless:
            recent.append(vr)
            remaining_lossless -= vr.token_estimate.value
        else:
            dropped.append(vr.round_id)           # §59：不代表删除
            # 注意：不是 break —— 见 11.3 的"跳过 vs 停止"决策
            break
    return recent, dropped, remaining_lossless
```

**"跳过 vs 停止"决策**：MD §29 的示例在遇到装不下的 R15 时**停止**（`超过预算，于是停止`）。
→ 本设计采用 **break（停止）**。理由：更旧的 Round 只会更大或相当，继续跳过收益极低而复杂度高。
记入 §21 Custom Rule。

## 11.3 完整顺序（§64 逐条对应）

```
Raw Archive
 ↓ 过滤无有效回答的失败 Round                    → Valid Round Pool
 ↓ 确定当前 Model
 ↓ 获取 Provider Model Metadata                  → context_limit / model_max_output
 ↓ 确定 Request Output Limit                     → reserved_output_tokens
 ↓ 计算 Fixed Input                              → system + tools + current_user + peer_reserve
 ↓ 计算 History Budget                           → §21 公式
 ↓ 读取 k
 ↓ 计算 Lossless Budget
 ↓ 读取 Pin → 加入 Pin（优先占用，不检查预算）    → §26
 ↓ 从最新 Valid Round 向旧倒序加入完整 Q&A        → §29
 ↓ 超出 Lossless Budget 的旧历史进压缩区          → §34
 ↓ 压缩区已有可用 Compact？复用 : 执行 Compaction
 ↓ Peer Review = ON？→ PeerContextPreparator      → §13
 ↓ 最终检查 Input + Reserved ≤ Context Limit      → §24
 ↓ 仍超限 → Recursive Compaction                  → §38
 ↓ 发送 LLM
```

---

# 12. 超大 Round 的处理（MD §31）

## 12.1 规则

```text
Round > remaining_lossless_budget
  → 整个 Round 不拆
  → 进入 Compaction 区
```

**默认不做任意拆分**（§31 "默认不做任意拆分"）。

## 12.2 三种边界情况

| 情况 | 处理 |
|---|---|
| **Pin 的 Round 超大** | §27：**保持原文，不压缩、不解除**。占多少记多少，触发 `pinned_history_exceeds_budget` warning |
| **最新一轮 Valid Round 本身就超限** | 仍进压缩区（不拆）。极端情况下 recent_raw 为空，Context 只有 current_user + pin + compacted |
| **单个 Round > history_budget 全部** | 压缩该 Round 的旧部分无意义 → 压缩区只做"摘要该 Round"；若摘要后仍超限 → Recursive Compaction（§15） |

## 12.3 ❌ 禁止

```text
❌ Question 被移除，Answer 单独留下       （§31 逐字禁止）
❌ 某个 Answer 单独保留，但失去对应问题    （§31 逐字禁止）
```

`HistoricalContextItem` 强制 `user_question` + `answers[]` 同生同死——数据结构层面无法只留 Answer。

---

# 13. Pin 完整生命周期（MD §18 / §19 / §26 / §27）

## 13.1 生命周期矩阵

| 阶段 | 接口 | 持久化 | 日志 | 测试 |
|---|---|---|---|---|
| **Pin（创建）** | `PinStore.pin(session_id, round_id, reason="user")` | 追加 `pins.jsonl` | `memory_pin{pinned:true, round_id, reason}` | T6 |
| **Unpin** | `PinStore.unpin(session_id, round_id)` | 追加 `pins.jsonl`（不删历史行） | `memory_pin{pinned:false, …}` | T6 |
| **Load** | `PinStore.load(session_id)` → 重放事件流 | 读 `pins.jsonl` | — | T6 |
| **Selection** | `ContextSelector._apply_pins()` | — | `history_selection.pinned_rounds` | T6 |
| **Overflow** | 见 13.2 | — | `context_budget_warning{reason:"pinned_history_exceeds_budget"}` | T7 |
| **Logging** | — | — | §19 事件结构 | T16 |

## 13.2 Overflow 处理（§27 顺序，逐条落地）

```text
① Current User 必须保留          → 无论如何保留
② Pinned History 保持原文         → 不压缩、不解除、不静默删除
③ 尽可能减少普通 Recent Raw       → recent_raw 可减到 0
④ 如果仍然超限                    → 明确记录 Context Overflow / Cannot Fit
```

```python
if pinned_tokens > lossless_budget:
    warnings.append("pinned_history_exceeds_budget")
    log("context_budget_warning", reason="pinned_history_exceeds_budget",
        lossless_budget=…, pinned_tokens=…)
    # 不解除、不压缩、不删除 —— 继续照常装载（会挤占 recent_raw 空间）
if total_input + reserved > context_limit and recent_raw:
    recent_raw = []                       # ③ step：减到 0
    log("context_budget_warning", reason="recent_raw_dropped_for_pin")
if total_input + reserved > context_limit:
    warnings.append("context_overflow_cannot_fit")
    log("context_budget_warning", reason="context_overflow_cannot_fit", …)
    # ④ 不 raise，不中断 Round（修正 audit I.2 的 raise 行为）
```

## 13.3 Pin 内容的定义（§18.1）

```text
Pin 内容 = User Question + 该 Round 所有成功 Final Answers
```

即 Pin 一个 Round = Pin 一个完整的 `ValidRound`（与 §5 的历史单位一致）。

## 13.4 三条铁律（代码注释中原文标注）

```text
Pin 不得自动压缩
Pin 不得静默删除
Pin 不得自动解除
```

---

# 14. Compaction：旧历史替换，非随意总结（MD §32 / §34 / §35 / §36）

## 14.1 定位

```text
Compaction 不是删除
Compaction 不是 Memory Database
Compaction 不是 Project Memory
Compaction 不是 Summary UI 功能
Compaction = Context Budget 压缩机制
```

## 14.2 数据流

```
Old Raw History（compaction_zone 内的 Valid Rounds）
        ↓
CompactionService.compact(rounds, attempt=N)
        ↓  LLM 摘要（COMPACTION_PROMPT，§36 结构）
Compacted Representation（compact_<K>.md）
        ↓
注入 Prompt 的 Compacted 块
        ↓
Raw Archive 不变 ✅（round_NNN/ 下文件字节级不动）
```

## 14.3 必须记录的元数据（MD §60）

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

`CompactionRecord` 数据类字段与上面一一对应（见 §4.9）。

## 14.4 压缩区已有产物时复用（§34）

```text
如果 Compaction 区已有可用 Summary → 使用 Summary（不重复调用 LLM）
如果没有 → 执行 Compaction
```

**"可用"判定**：`compact_<K>.md` 存在 且 其 `source_rounds` 覆盖当前 compaction_zone 的最大子集。
第一版简化：存在即复用，并在日志记 `reused=true`。

**⚠ 复用时的归属校验（修订 #1）**：

```python
# 即便复用旧压缩文件，仍必须在 Prompt 中重新声明归属
rendered = render_compacted_block(
    old_summary,
    prefix="## 历史压缩摘要（Session 级共享，含参与者 X/Y/Z 的观点，非本模型个人总结）",
)
# viewpoint_attribution 必须仍为 True
assert old_summary.viewpoint_attribution is True
```

## 14.5 失败降级（§69 Phase 4.4）

```text
Compaction 失败
  → 不破坏原始历史（Raw Archive 字节不变）
  → 记 compaction{status:"failed"}
  → 降级：保留尽可能多的 recent_raw（即使超 lossless），记 warning
  → 不中断 Round
```

**失败判定（修订 #1 加严）**：

```python
def validate_compaction_viewpoint(compacted_text: str) -> bool:
    """压缩产物必须包含至少一处归属标记，否则视为冒充某模型 Summary 的风险。"""
    attribution_patterns = [
        r"参与者\s*\w+",                    # "参与者 A 认为…"
        r"Candidate[\s-]*[A-Z0-9]+",       # "Candidate-B 反对…"
        r"(Alpha|Glm|Laguna|LFM|Qwen)",    # 模型名（Prompt 中给出）
        r"用户(?:决策|认为|确认)",           # 用户决策（与模型观点区分）
    ]
    return any(re.search(p, compacted_text) for p in attribution_patterns)

record.viewpoint_attribution = validate_compaction_viewpoint(record.content)
if not record.viewpoint_attribution:
    record.status = "failed"
    log("compaction", reason="viewpoint_attribution_missing",
        status="failed")
    # 降级到保留更多 recent_raw
```

---

# 15. Recursive Compaction 与终止条件（MD §38 / §39）

## 15.1 触发与循环

```python
MAX_COMPACTION_ROUNDS = 3          # 见 15.2 取值理由

attempt = 0
while attempt < MAX_COMPACTION_ROUNDS:
    if total_input + reserved <= context_limit:
        break                                        # ✅ 终止条件 1：装得下
    attempt += 1
    record = await compaction_service.compact(zone, attempt)
    if record.status == "failed":
        break                                        # ✅ 终止条件 2：压缩失败
    new_total = recompute(record)
    if new_total >= total_input:
        break                                        # ✅ 终止条件 3：压缩无收益
    total_input = new_total
else:
    # ✅ 终止条件 4：达到 MAX_COMPACTION_ROUNDS 仍超限
    log("context_budget_warning", reason="max_compaction_rounds_reached",
        attempt=attempt, total_input=total_input, context_limit=context_limit)
    degrade_to_minimal_context()                     # 见 15.3
```

## 15.2 `MAX_COMPACTION_ROUNDS = 3` 的取值理由

> MD §38："第一版必须设置 MAX_COMPACTION_ROUNDS 防止无限循环。**不能直接照搬其他项目的固定值。**"

| 依据 | 值 |
|---|---|
| E2E 实测压缩比（2026-09-02，`.phase4_e2e_sessions/`） | 15,946 chars → 111 chars 摘要的极端案例；常规场景约 **4:1 ~ 6:1** |
| 单次压缩可处理的超限倍数 | 保守取 **4:1** |
| 3 次递归的理论覆盖 | 4³ ≈ **64:1**，远超个人研究场景的历史增长 |
| 成本上限 | 3 次 LLM 摘要调用（每次约 1 次请求），可接受 |

→ 取 **3**。若为 0 会导致"超限即降级丢弃"，违反 §38；若 ≥ 5 则成本与延迟不可控。
**每次实际 attempt 数必须记录**（§38"必须记录实际次数"）。

## 15.3 超限仍无法容纳时的降级（§27 第 4 步）

```
仍超限（attempt == MAX_COMPACTION_ROUNDS）
  → 不 raise，不中断 Round
  → 降级顺序：
     ① 保留 current_user（必须）
     ② 保留 pin 原文（必须）
     ③ recent_raw 减到 0
     ④ 仍超限 → 记 context_overflow_cannot_fit
        并按 Provider 侧 clamp：reserved_output = max(0, context_limit − actual_input)
        若 reserved == 0 → 记 warning 后仍发送（由 Provider 报真实错误）
```

> 注：此路径修正 audit I.2（当前 `_dynamic_max_tokens` 在 `remaining<=0` 直接 raise 中断整轮）。

## 15.4 无限循环的防护

| 防护 | 机制 |
|---|---|
| 硬上限 | `while attempt < MAX_COMPACTION_ROUNDS` |
| 无收益检测 | `new_total >= total_input` → break |
| 失败检测 | `status == "failed"` → break |
| 装得下检测 | `total_input + reserved <= context_limit` → break |

四个 break 条件中任意一个都能退出，不存在只靠计数器的裸循环。

---

# 16. MCP Runtime × MCP Evidence Index（MD §44–§48）

## 16.1 职责分离

| | MCP Runtime | MCP Evidence Index |
|---|---|---|
| **是什么** | 原始运行档案 | 轻量导航索引 |
| **内容** | Tool / Arguments / Result / Error / Duration / Reconnect / Retry | 哪些 Tool Result **实际被 Final Answer 使用** |
| **生成时机** | 每次 tool call 实时 | Round 完成后 或 Compaction 时（§48） |
| **文件** | `round_NNN/mcp_runtime.jsonl` | `round_NNN/mcp_evidence.jsonl` |
| **是否压缩** | ❌ **不因 Context Compaction 删除**（§45） | 可随 Round 归档 |
| **能否互相替代** | — | ❌ Evidence Index **不能**代替 Runtime（§17） |

## 16.2 Runtime 保留确认

```text
✅ core/mcp_runtime_log.py 保留，语义不改
✅ 17 个现有测试保留
✅ 文件名、字段、脱敏逻辑全保留
✅ 新增的 reconnect 事件保留
```

**唯一改动**：`mcp_runtime_log.py` 不再承担通用日志职责——通用事件走新的 `core/context_log.py`。
这是**加法不是修改**（§十七"Runtime 与 health report 分开"的延续）。

## 16.3 Evidence Index 生成（§48）

```python
async def build_evidence_index(session_id, round_id, model_id, runtime_records, final_answer):
    """Round 完成后调用。第一版不要求每次 tool call 立即调用 LLM（§48 明许）。"""
```

**第一版判定策略**（避免每次 tool call 额外 LLM 调用）：

1. 从 `mcp_runtime.jsonl` 读本轮全部 `tool_call` 记录（含 `result_preview`）
2. 对每个记录，在 Final Answer 中做**可追溯匹配**：
   - `tool_call_id` 显式引用
   - `result_preview` 中的高显著片段（≥ 12 字符连续子串）出现在 Final Answer 中
3. 匹配上 → `used_in_final_answer=true`；否则 `false`
4. 写 `mcp_evidence.jsonl` + 记 `mcp_evidence_index` 事件

> 这是 **CrossLLM Custom Rule**（见 §21）：MD §48 只规定"在 Round 完成后或 Compaction 时生成"
> 与"减少额外调用成本"，未规定匹配算法。字符串匹配是第一版最简可解释方案，
> 后续可在出现误判证据时升级为 LLM 判定。

## 16.4 ❌ 禁止

```text
❌ Tool Call 直接当 Evidence                （§46 逐字）
❌ Evidence = Fact                          （§47 逐字）
❌ MCP Result 自动升级为 Fact               （§47）
❌ Evidence Index 代替 Runtime              （用户任务书 §十七）
```

> §47 语义："知乎文章说 X" 最多说明"存在关于 X 的该说法"，不能自动升级为"X 已验证"。
> 落地：Evidence Index 只记 `used_in_final_answer: bool`，**不记** `is_fact: bool`。

---

# 17. 日志设计（MD §57–§61）

## 17.1 通用日志模块 `core/context_log.py`

```python
@dataclass
class LogEvent:
    event: str                    # 10 类之一
    session_id: str
    round_id: int | None
    model_id: str | None
    timestamp: str                # ISO8601，毫秒
    status: Literal["ok", "warning", "failed", "skipped"]
    payload: dict                 # 事件专属字段，扁平化后与上面 5 个字段同层

def append_event(session_dir: Path, event: LogEvent) -> bool:
    """追加写 sessions/<sid>/context_policy.jsonl。任何失败静默吞掉，绝不抛异常。"""
```

**必须字段（所有事件）**：`event` / `session_id` / `round_id` / `model_id` / `timestamp` / `status`

## 17.2 10 类事件

| # | event | MD § | 触发时机 | 关键 payload |
|---|---|---|---|---|
| 1 | `context_policy` | §55 | 每轮 Round 开始 | `k` |
| 2 | `context_budget` | §57 | 每次建立 Prompt | `context_limit`, **`model_max_output`**（修订 #2）, **`request_max_output`**, `reserved_output_tokens`, `fixed_input_tokens`, `history_budget`, `k`, `lossless_budget`, `compaction_budget`, `pinned_tokens`, `recent_raw_tokens`, `compacted_tokens` |
| 3 | `history_selection` | §59 | 每次选择历史后 | `current_user_tokens`, `pinned_rounds[]`, `selected_rounds[]`, `dropped_rounds[]`, `peer_context_source{model:mode}`, `reason` |
| 4 | `memory_pin` | §19 | 每次 Pin/Unpin | `round_id`, `pinned`, `reason` |
| 5 | `conversation_summary` | §41 | 每次生成 Summary | `model_id`, `trigger`(user/peer_review/compaction), `source_rounds[]`, **`source_valid_round`（修订 #3）**, **`reused`（修订 #3）**, **`reused_reason`**, `input_tokens`, `output_tokens`, `status`, `summary_file` |
| 6 | `peer_context_preparation` | §42 | 每次准备 Peer Context | `source_model`, `valid_answer_count`, `mode`(final_answer/conversation_summary), `source_id`, **`summary_reused`**（修订 #3） |
| 7 | `compaction` | §60 | 每次 Compaction | `source_rounds[]`, **`contributor_models[]`（修订 #1）**, **`viewpoint_attribution`（修订 #1）**, **`artifact_kind:"session_compaction"`**, `input_tokens`, `expected_summary_tokens`, `actual_summary_tokens`, `summary_file`, `attempt`, `status` |
| 8 | `context_budget_warning` | §27 §40 | 预算异常 | `reason`(pinned_history_exceeds_budget / context_overflow_cannot_fit / max_compaction_rounds_reached / …), 相关数值 |
| 9 | `mcp_runtime` | §45 | 每次 tool call（**已有**） | 保持现有字段 + `reconnect` 事件 |
| 10 | `mcp_evidence_index` | §46 | Round 完成后 | `references[{mcp, tool, reference, used_in_final_answer}]` |

## 17.3 Token 三元组的落地（§58）

所有 token 字段在日志中展开为三元组：

```json
{
  "history_budget": 92160,
  "history_budget_source": "crossllm_estimator",
  "history_budget_exactness": "estimated"
}
```

或取不到时：

```json
{
  "model_max_output": null,
  "model_max_output_source": "provider_unavailable",
  "model_max_output_exactness": "unknown"
}
```

> **字段名已同步统一（2026-09-25）**：`model_max_output`
> （旧名 `provider_max_output_tokens` / `model_max_output_tokens` 废弃）。
> **不变量 T21 反固定值**：如果某个 `request_max_output=16000` 的请求却得到
> `reserved_output_tokens=16000` 而 `model_max_output` 是 32768（说明 clamped），
> 仍然合法；但若所有请求都得到 `reserved_output_tokens=16000` 且与 provider/request 无关，
> 则说明代码退化成了固定常量。

> `null` + `unknown` 是**合法且被期望**的状态。填 0 或填猜测值属于违规。

## 17.4 落盘位置

| 文件 | 内容 |
|---|---|
| `sessions/<sid>/context_policy.jsonl` | 事件 1–8（Context / Summary / Peer / Compaction / Warning） |
| `sessions/<sid>/round_NNN/mcp_runtime.jsonl` | 事件 9（已有，不改） |
| `sessions/<sid>/round_NNN/mcp_evidence.jsonl` | 事件 10 的持久化副本 |

## 17.5 可回答性自检（§57）

日志必须能回答 MD 明确点名的 7 个问题：

| 问题 | 由哪个事件+字段回答 |
|---|---|
| 为什么这些历史被保留？ | `history_selection.selected_rounds` + `context_budget.lossless_budget` |
| 为什么那些历史没有被保留？ | `history_selection.dropped_rounds` + `reason` |
| 为什么执行了 Summary？ | `conversation_summary.trigger` |
| 为什么 Peer Context 使用 Summary？ | `peer_context_preparation{valid_answer_count, mode}` |
| 为什么没有执行 Summary？ | `peer_context_preparation{valid_answer_count:1, mode:"final_answer"}` |
| 为什么发生 Compaction？ | `history_selection.dropped_rounds` 非空 + `compaction.source_rounds` |
| 为什么某些 Round 被排除？ | `history_selection.dropped_rounds` + `compaction.status` |

---

# 18. 前端与 API

## 18.1 API 变更

| 端点 | 变更 |
|---|---|
| `POST /api/session/{sid}/round` | `RunRoundIn` 增加：<br>`k: float = 0.5`（校验 0.01–0.90）<br>`summary: bool = False`<br>`request_max_output: int \| None = None`（**UI 不提供**，仅 API/脚本可显式指定） |
| `POST /api/session/{sid}/pin` | **新增**。`body: {round_id:int, pinned:bool}` → 追加 `pins.jsonl` + 记 `memory_pin` |
| `GET /api/session/{sid}/pin` | **新增**。返回 `PinState.pinned_round_ids` |
| `GET /api/session/{sid}/summary/{model_id}` | **新增**。返回该模型全部 Summary 元信息（不含正文）+ latest |
| `GET /api/session/{sid}/context/{round_id}` | **新增**。返回该轮 `context_policy.jsonl` 解析结果，供 UI Context 面板展示 |
| `GET /api/session/{sid}` | `rounds[]` 增加 `is_valid` / `selected_rounds` / `dropped_rounds` / `pinned` 字段 |

**链路完整性检查**（用户任务书 §十九）：

```
Frontend k=0.7
   ↓ POST /round {k:0.7}
API RunRoundIn.k = 0.7
   ↓ prepare_round(..., k=0.7)
RoundManager → round_state.k = 0.7
   ↓ ContextSelector.build_context(k=round_state.k)
ContextBudget.k = 0.7 → lossless_budget = history_budget × 0.7
   ↓ log context_policy{k:0.7} + context_budget{k:0.7, lossless_budget:…}
Persistence state.json.rounds[].k
   ↓ GET /api/session/{sid}
Frontend 显示 k=0.7 ✅
```

## 18.2 前端控件（`static/index.html` + `static/app.js`）

| 控件 | ID | 行为 |
|---|---|---|
| Peer Review 开关 | `#peer-review`（已有） | 保留 |
| **Summary 开关** | `#summary-mode` | 见 §3.2 状态机 |
| **k 滑块** | `#k-slider` | `min=0.01 max=0.90 step=0.01 value=0.5`，旁显示 `#k-value` 当前值 |
| **Context 状态面板** | `#context-status` | 显示 `lossless / compaction / pinned / selected / dropped`，读 `/context/{round_id}` |
| **Pin 按钮** | 每个 Round 卡片 | 切换 Pin，调 `/pin` |

### Summary 状态机（明确规则，对应 §3.2 表格）

```javascript
const summaryState = {
    enabled: false,
    draftBackup: null,   // 勾选前输入框内容
    userEdited: false,   // 勾选后用户是否改过
};

function onSummaryToggle(checked) {
    const box = instructionBox;
    if (checked) {
        summaryState.draftBackup = box.value;   // 备份（仅首次）
        summaryState.userEdited = false;
        box.value = DEFAULT_SUMMARY_PROMPT;     // 由后端 /api/config/summary-prompt 提供
    } else {
        if (summaryState.userEdited) {
            // 保留用户编辑内容 —— 不静默丢弃
        } else {
            box.value = summaryState.draftBackup || "";
        }
        summaryState.draftBackup = null;
    }
    summaryState.enabled = checked;
}

instructionBox.addEventListener("input", () => {
    if (summaryState.enabled) summaryState.userEdited = true;
});
```

`DEFAULT_SUMMARY_PROMPT` 由后端 `GET /api/config/summary-prompt` 提供（单一真源在 `core/summary_prompt.py`），
**不在前端硬编码**——否则会出现"UI 显示的 Prompt 与后端实际用的不一致"（§十九 禁止项）。

---

# 19. 测试迁移与新增

## 19.1 旧测试 → 新测试迁移表

| 旧测试 | 位置 | 验证的旧行为 | 为什么废弃 | 新测试 |
|---|---|---|---|---|
| `test_history_window_is_three_rounds` | `test_phase2.py:730` | 断言 `injected_rounds == [4,3,2]`；断言 `"## 第 1 轮" not in prompt` | §29/§54 直接冲突；MD 明说"不得因 3 轮限制删除 R1–R7" | `test_no_hard_three_round_window`（T1 反向断言 R1–R7 仍在） |
| `test_budget_trim_drops_oldest_first` | `test_phase2.py:748+` | 超限时从最旧一轮开始丢 | 与 §29 冲突：应进 Compaction 而非丢弃 | `test_oversized_round_goes_to_compaction`（T3） |
| `test_context_budget.py`（7 个） | `test_context_budget.py` | 验证 `should_compact` 的 80% 阈值 | §20/§33 直接冲突（k 不是触发阈值） | `test_phase4_budget.py`（T8/T9） |
| `test_compactor.py`（10 个） | `test_compactor.py` | 单次压缩 + `compact_<K>.md` 平铺落盘 | 与 §38（递归）、§62（目录）冲突 | `test_phase4_compaction.py`（T15） |
| `test_phase4_compaction.py`（5 个） | `test_phase4_compaction.py` | "触发后 prompt 含 summary 块**不含 raw history 块**" | 与 §29/§31 冲突：应尽可能保留 Recent Raw，只有超出 lossless 的才压缩 | `test_phase4_context_selection.py`（T2/T3） |
| `test_phase1.py`（28 个） | — | Phase 1 基础行为 | 与 Phase 4 无关 | **保留**，回归验证 |
| `test_phase2.py` 其余（48 个） | — | 匿名化 / 事件总线 / 参与者过滤 | 大部分与 Phase 4 无关 | **逐条审查**，非冲突的保留 |
| `test_phase3.py`（10 个） | — | MCP Tool Calling | 无关 | **保留** |
| `test_mcp_runtime_log.py`（17 个） | — | MCP Runtime JSONL + 脱敏 | Runtime 保留（§45） | **保留**，另增 Evidence 测试 |

**统计**：废弃 **2 个**；重写 **3 个文件 22 个用例**；保留 **55 个**。

## 19.2 新增测试（16 项，对应用户任务书 §二十三）

| # | 测试名 | 文件 | 验证内容 |
|---|---|---|---|
| **T1** | `test_no_hard_three_round_window` | `test_phase4_context_selection.py` | R1–R10 存在且 budget 充足时，**不因 3 轮限制删除 R1–R7** |
| **T2** | `test_context_sufficient_keeps_all_valid_rounds` | 同上 | Context 足够 → 保留**全部**完整 Valid Round（含 User Question） |
| **T3** | `test_oversized_round_goes_to_compaction` | 同上 | Context 不足 → 旧 Round 进 Compaction 区，recent_raw 保留能装下的 |
| **T4** | `test_partial_failure_round_stays_valid` | `test_phase4_valid_round.py` | A success / B timeout / C success → Round Valid，历史含 A/C，**不含 B Error** |
| **T5** | `test_all_failed_round_excluded_from_context` | 同上 | 全失败 → 不进 Context，但 `mcp_runtime.jsonl` 存在 |
| **T6** | `test_pin_never_auto_compacted` | `test_phase4_pin.py` | Pin 的 Round 在任何压缩后**字节级不变** |
| **T7** | `test_pin_overflow_warns_without_unpin` | 同上 | Pin 超 lossless → 有 warning，Pin **仍在 Context**，未被解除 |
| **T8** | `test_k_allocates_lossless_and_compaction` | `test_phase4_budget.py` | k=0.2 / 0.5 / 0.9 三档，`lossless` 与 `compaction` 随之变化且和为 `history_budget` |
| **T9** | `test_provider_metadata_drives_reserved_output` | 同上 | `model_max` + `request_max` 参与 `reserved_output` 计算；含 `unknown` 降级分支 |
| **T10** | `test_summary_ui_autofill_and_send` | `test_phase4_summary_ui.py` | 勾选 → 输入框获得默认 Prompt → 可直接发送；取消规则三种场景 |
| **T11** | `test_peer_single_answer_uses_final_answer` | `test_phase4_peer.py` | `valid_answer_count==1` → `mode="final_answer"`，**LLM 调用次数为 0**（未调 Summary） |
| **T12** | `test_peer_multi_answer_uses_summary` | 同上 | `valid_answer_count>1` → `mode="conversation_summary"`，`source_id` 形如 `summary_NNN` |
| **T13** | `test_peer_review_result_never_becomes_next_peer_context` | 同上 | 见 §8.3：三层断言 |
| **T14** | `test_summary_does_not_promote_opinion_to_fact` | `test_phase4_summary.py` | Prompt 含 §9 第 4 条；输入"我认为 X 可能更好" → 输出不出现断言性表述；落盘 `summaries/<model>/` |
| **T15** | `test_recursive_compaction_second_pass` | `test_phase4_compaction.py` | 第一次压缩后仍超限 → 第二次 Compaction，`attempt=2`，有日志 |
| **T16** | `test_log_completeness` | `test_phase4_logging.py` | 每次 Context 构建后 `context_budget` + `history_selection` 必存在；Summary/Peer/Pin/Compaction 发生时对应事件存在 |

## 19.3 附加测试（Plan 补充，非任务书要求但必要）

| 测试 | 验证 |
|---|---|
| `test_summary_not_auto_written_to_project_memory` | §49：Summary 后 `projects/` 目录**不产生**任何文件 |
| `test_compaction_does_not_delete_raw_archive` | §1-1：压缩后 `round_NNN/` 全部文件 md5 不变 |
| `test_mcp_evidence_index_traces_runtime` | §46：Evidence 的 `tool_call_id` 能在 `mcp_runtime.jsonl` 中定位到原始记录 |
| `test_token_estimate_unknown_not_zero` | §25：取不到时 `value is None` 且 `accuracy_mode=="unknown"`，不是 0 |
| `test_k_out_of_range_rejected` | k=0.005 / 0.95 → API 422 |
| `test_reserved_output_never_exceeds_model_capability` | §22：request > model 时 clamp 并 warning |

**新增计数**：16（任务书）+ 6（补充）= **22 个核心新测试**；考虑参数化（k 三档等）实际用例约 **40+**。

## 19.4 Step 2 修订专项测试（5 项修订必须各有测试覆盖）

| # | 测试 | 文件 | 验证修订 | 关键断言 |
|---|---|---|---|---|
| **T17** | `test_summary_reuse_when_no_new_valid_round` | `test_phase4_summary.py` | 修订 #3 | 连续两轮 Peer Review 之间无新 valid round → `summary_service.get_or_create` **只调用 LLM 一次**；第二次命中复用，`reused=True`、`reused_reason="source_valid_round_covered"` |
| **T18** | `test_summary_regenerate_when_new_valid_round` | 同上 | 修订 #3 | 出现新 valid round → 重新生成；`reused=False`、`reused_reason="new_valid_round_appeared"`；最新 summary 的 `source_valid_round` = 新 round_id |
| **T19** | `test_summary_is_derived_artifact_not_round` | `test_phase4_summary.py` + `test_phase4_valid_round.py` | 修订 #4 | ① Summary 生成后 `session.current_round` 不变；② 不创建 `round_NNN/` 目录；③ `valid_answer_count` 不变；④ `state.rounds` 长度不变 |
| **T20** | `test_compaction_preserves_viewpoint_attribution` | `test_phase4_compaction.py` | 修订 #1 | 压缩文本必须含"参与者 X / Candidate-B / 模型名 / 用户决策"中至少一个标记；否则 `viewpoint_attribution=False`、`status="failed"`、降级路径触发 |
| **T21** | `test_reserved_output_depends_on_provider_and_request` | `test_phase4_budget.py` | 修订 #2 | 构造 `model_max_output=32768` + `request=8000` 与 `provider=8000` + `request=32768` 两个 fake 模型，断言两者 `reserved_output_tokens` **相等且都等于 8000**（min）；并断言**绝不等于固定 16000** |
| **T22** | `test_session_compaction_never_pretends_to_be_summary` | `test_phase4_compaction.py` + `test_phase4_summary.py` | 修订 #1 | ① 任何 `compact_*.md` 文件**不可被** `summary_service.get_latest()` 检索到；② `latest_conversation_summary` 永不被 compaction 更新；③ Compaction 产物不写在 `summaries/<model>/` 目录下；④ `PeerContextItem.source_id` 永不以 `compact_` 开头 |

## 19.5 Step 2 修订后测试总账

| 来源 | 数量 |
|---|---|
| 任务书要求 T1–T16 | 16 |
| Plan 补充 §19.3 | 6 |
| 修订专项 T17–T22（**新增**） | 6 |
| **合计** | **28 个核心新测试** |
| 考虑参数化（k 三档、provider/request 两组、T17 多种复用场景等）实际用例 | 约 **50+** |

---

# 20. 模块依赖图

## 20.1 主链路

```
                        core/model_capability.py
                     (OpenRouter /api/v1/models + cache)
                                  ↓
                        ProviderModelMetadata
                     {context_limit, model_max_output}   ← 修订 #2 改名
                                  ↓
        ┌─────────────────────────┴─────────────────────────┐
        │                                                   │
   models.yaml / API body                          core/context_budget.py
   (request_max_output, k)                         compute_budget()  §21
        │                                                   │
        └─────────────────────────┬─────────────────────────┘
                                  ↓
                          ContextBudget
                    {history_budget, k,
                     lossless_budget, compaction_budget}
                                  ↓
                      core/context_selector.py
                                  │
        ┌─────────────────────────┼─────────────────────────┐
        ↓                         ↓                         ↓
  core/pin_store.py       core/valid_round.py      core/peer_context.py
   PinState (pins.jsonl)   ValidRoundPool           PeerContextPreparator
        │                         │                         │
        │                         │                  ┌──────┴──────┐
        │                         │                  ↓             ↓
        │                         │        final_answer    conversation_summary
        │                         │          (raw md)      (SummaryService)
        │                         │                  └──────┬──────┘
        │                         │                         ↓
        │                         │                  PeerContextItem
        │                         │                  (不落盘)
        └────────────┬────────────┴─────────────────────────┘
                     ↓
              ┌──────┴──────┐
              │  装得下      │  装不下
              ↓             ↓
        recent_raw    core/compaction.py
        (完整 Q&A)     CompactionService
                       (递归, attempt≤3)
                             ↓
                      CompactedBlock
                             ↓
        ┌────────────────────┴────────────────────┐
        ↓                                          ↓
  ContextSelectionResult                  core/context_log.py
        ↓                                  context_policy.jsonl
  core/prompt_builder.py                   (10 类事件)
        ↓
  Provider.generate(max_output_tokens=budget.reserved_output_tokens)
        ↓
  Final Answer → core/storage.py → round_NNN/responses/<model>.md
        ↓
  core/evidence_index.py → round_NNN/mcp_evidence.jsonl
```

## 20.2 Peer 子图（隔离必须可见）

```
  Conversation History (该模型自己的 Q + 自己的 A)
              ↓
     core/summary_service.py  ──────► summaries/<model>/summary_NNN.md
              ↓                              │
      ConversationSummary                    │
              ↓                              │
   core/peer_context.py                      │
   PeerContextPreparator                     │
              │                              │
      ┌───────┴───────┐                      │
      ↓               ↓                      │
 final_answer   conversation_summary ────────┘
      │               │
      └───────┬───────┘
              ↓
        PeerContextItem
              ↓
        PromptBuilder
              ↓
        Current Model → Final Answer
              │
              ╳  ← 此处断开，无回边
              │
      （不写回其他模型 Summary；不落盘为 peer memory）

  ⛔ Peer Review Result ──X──► 下一次 Peer Context
     数据层无此路径：Peer Context 从不落盘
```

## 20.3 三条 Memory 的分层（§十二）

```
Conversation Summary  →  summaries/<model>/summary_NNN.md   （模型工作状态）
Compaction            →  compact_<K>.md                     （Context 压缩）
Project Memory        →  projects/<pid>/memory/memory_NNN.md（本轮不创建）

        ⛔ 三者之间无任何自动写入路径
        ⛔ 无 code path 从 A 目录写文件到 B 目录
```

---

# 21. 借用 vs 自定义声明（MD §二十一 / §71）

> "不要在文档中只写'参考 Aider'，而代码实际上没有采用任何相关机制。"

## 21.1 Borrowed Mechanism（明确借用的成熟机制）

| 机制 | 来源 | 借用了什么 | 落地位置 |
|---|---|---|---|
| **Recent raw tail 保留** | Aider `ChatSummary` | "保留最近消息原文"的**原则** | `ContextSelector._fill_recent_raw()` |
| **预算驱动的 tail 长度** | Aider `half_max_tokens = max_tokens // 2` | "用预算而非固定条数决定 tail"的**思路** | 但 CrossLLM 用 `lossless_budget`（= `history_budget × k`），**不是** `max_tokens//2`（见 21.3 差异说明） |
| **失败降级不阻断** | Aider `except ValueError → 继续全历史` | Summary/Compaction 失败时不中断 Round | `CompactionService` / `SummaryService` 的 try/except 降级 |
| **失败恢复语义** | OpenAI Agents SDK | 压缩失败后恢复可用状态 | `CompactionRecord.status="failed"` → 降级保留 raw |
| **原子写** | 项目既有 `storage.atomic_write_text` | 直接复用 | Summary / Pin / Evidence 全部走它 |
| **脱敏 + 追加写静默失败** | 项目既有 `mcp_runtime_log` | 抽为新 `context_log` 的基础 | `core/context_log.py` |

## 21.2 CrossLLM Custom Rule（本项目特有，MD 未规定或无成熟先例）

| # | 规则 | 为什么需要（MD §71 五问） |
|---|---|---|
| **C1** | **k 只做预算分配**，绝不作触发阈值 | MD §33 明令。Aider 无此概念，SDK 用项数阈值 |
| **C2** | **Pin 永不自动压缩/解除/删除**，超预算只 warning | MD §27。三个参考项目**都没有** Pin 概念 |
| **C3** | **Compaction 为 Session 级共享，Summary 为模型级** | MD §37 要求两者生命周期不同。Aider 是单模型，无此问题；SDK 是单 Agent Session |
| **C4** | **Peer Context 不落盘**（天然不可回读） | MD §16/§43 隔离要求。参考项目无 Peer Review，无先例 |
| **C5** | **Historical Unit = Round（Q+A 不可分）** | MD §5/§31。Aider/SDK 都是 user+assistant 交替消息流，无"Round"聚合概念 |
| **C6** | **倒序装载遇到装不下的 Round 即 break** | MD §29 示例即 break。Aider 是累加到预算为止（可能跳过小消息），语义不同 |
| **C7** | **MAX_COMPACTION_ROUNDS = 3** | MD §38 禁止照搬固定值。理由见 §15.2（实测 4:1 × 3 ≈ 64:1 覆盖） |
| **C8** | **Evidence Index 用字符串可追溯匹配**（第一版不调 LLM） | MD §48 允许"Round 完成后生成，减少额外调用成本"。MD 未规定匹配算法 |
| **C9** | **context_length 取 min(顶层, top_provider)** | 实测 minimax-m2.5 两者不一致（204800 vs 200000）。保守取向 |
| **C10** | **Peer Review 轮的模型输出仍视为该模型 Final Answer** | MD §16 示例原文称其为 "Model A Final Answer"；§43 禁止的是将其另存为跨轮 peer memory |
| **C11** | **Summary 输出为自由文本，无 facts[]/decisions[] 结构化字段** | MD §7 禁止强制 schema。Project Memory（§50）才有结构化字段 |

## 21.3 Borrowed 机制的具体差异（防止"假装采用"）

| Aider 机制 | Aider 实际值 | CrossLLM 取值 | 差异原因 |
|---|---|---|---|
| Tail 保留比例 | `max_tokens // 2`（固定 50%） | `history_budget × k`（k 用户可调 0.01–0.90） | Aider 是单模型编码会话，50% 是经验值；CrossLLM 由用户控制（§20/§55） |
| Token 估算 | `self.models[0].token_count(msg)` **真实 tokenizer** | `len // CHARS_PER_TOKEN` + `TokenEstimate{accuracy_mode:"estimated"}` | CrossLLM 多 Provider，无法统一 tokenizer。**但用 accuracy_mode 诚实标记**（§25），不伪装精确 |
| 递归 | `summarize_real(..., depth+1)`，`depth > 3` 停止，语义是"**摘要本身太长**" | `MAX_COMPACTION_ROUNDS=3`，语义是"**整体仍超限**" | 场景不同：Aider 递归处理单次摘要超限；CrossLLM 递归处理 Pin+Raw+Compacted 合计超限（§38） |
| 多模型 fallback | `[weak_model, main_model]` | 第一版：用 `SUMMARY_MODEL` 配置（默认 participants[0]），失败降级 | CrossLLM 模型来自用户选择，强制 fallback 会引入未选模型 |
| 历史替换 | `done_messages = tail + [summary_msg]`（**原文丢失**） | Raw Archive **永不删**，只换 Current Context 的表示 | MD §1-1 / §3.1 / §20 明确要求，与 Aider 相反 |

---

# 22. 实施顺序（对应 MD §68 Phase 4.1–4.6）

| Step | 内容 | 涉及文件 | 前置 |
|---|---|---|---|
| **S3.1** | `TokenEstimate` + `ProviderModelMetadata` + `ModelCapabilityResolver` | 新建 `core/model_capability.py`；改 `core/context_budget.py` | — |
| **S3.2** | `ValidRound` + `ValidRoundPoolBuilder` | 新建 `core/valid_round.py`；改 `core/storage.py`、`core/state.py` | S3.1 |
| **S3.3** | `ContextBudget` + `compute_budget()`（删 `should_compact`） | 重写 `core/context_budget.py` | S3.1 |
| **S3.4** | `PinStore` + Pin API + Pin UI | 新建 `core/pin_store.py`；改 `app.py`、`static/` | S3.2 |
| **S3.5** | `ContextSelector`（删 `HISTORY_WINDOW`、`_trim_to_budget` 丢弃语义） | 新建 `core/context_selector.py`；改 `core/round_manager.py`、`core/prompt_builder.py` | S3.2 S3.3 S3.4 |
| **S3.6** | `SummaryService` + `summary_prompt.py` + Summary UI + 持久化 | 新建 `core/summary_service.py`、`core/summary_prompt.py`；改 `app.py`、`static/` | S3.5 |
| **S3.7** | `PeerContextPreparator` | 新建 `core/peer_context.py`；改 `core/anonymizer.py`（参数改名） | S3.6 |
| **S3.8** | `CompactionService`（递归）+ 删 `core/compactor.py` | 新建 `core/compaction.py`；**删除** `core/compactor.py` | S3.5 |
| **S3.9** | `EvidenceIndex` | 新建 `core/evidence_index.py` | S3.5 |
| **S3.10** | `context_log` + 10 类事件接线 | 新建 `core/context_log.py`；改各模块 | S3.1–S3.9 |
| **S3.11** | Provider 接线（`max_output_tokens` 来自 budget） | 改 `providers/real_provider.py` | S3.3 |
| **S3.12** | 测试迁移 + 22 个新测试 | 删/改 `test_context_budget.py`、`test_compactor.py`、`test_phase4_compaction.py`、`test_phase2.py`（2 个）；新建 8 个文件 | S3.1–S3.11 |

**每步完成后必须**：跑全量 pytest + 检查无新引入的 `HISTORY_WINDOW` / `should_compact` / 固定 output reserve。

> **S3.10 实现归属说明（2026-09-05 追加，用户裁定）**
>
> 本表 S3.10 原定义为「`context_log` + 10 类事件接线，新建 `core/context_log.py`」。
> 实际实施时按用户任务书执行为「Phase 4 全链路整合验收 / E2E 重接线」，
> 10 类事件由**各模块直接写** `sessions/<sid>/context_policy.jsonl`
> （`ContextSelector._log_selection` / `PinStore` / `SummaryService._log_event` /
> `PeerContextPreparator._log` / `CompactionService._log` / `EvidenceIndexService._log` /
> `RoundManager` context_policy 事件）。
>
> **结论**：这属于「模块归属差异」，不是业务语义缺失。当前行为满足
> MD §12 / §57 / §59 / §60 / §42 的日志语义与可追溯要求（E2E S10 验证 8 类事件齐备）。
> 因此**不再回补** `core/context_log.py` 统一出口，不为形式一致返工 S3.1～S3.10。
> 若未来需要统一出口，应在 S3.12（测试迁移）或更晚阶段作为独立重构处理。


---

# 23. 已知设计风险

| # | 风险 | 影响 | 缓解 |
|---|---|---|---|
| **R1** | **Token 估算精度**：`len//3` 与实际 tokenizer 可能差 20–30% | lossless_budget 不准；可能实际超限或浪费空间 | ① `accuracy_mode="estimated"` 诚实标记 ② Provider 返回真实 `usage` 后回填校准 ③ 最终由 Provider 侧 clamp 兜底 |
| **R2** | **Peer Review 开启时 valid_answer_count 增长快**，很快走 Summary 分支，LLM 调用成本上升 | 每轮每个 peer 模型 1 次摘要调用 | ① `get_or_create` 缓存（同 source_rounds 不重复生成）② 失败降级不重试 |
| **R3** | **Compaction Session 级共享**：多模型共用一份压缩历史，可能混入其他模型的观点 | 某模型看到非自己的观点被当作"历史" | ① 压缩块在 Prompt 中明确标注为"历史压缩摘要（多模型混合）"② 与模型级 Summary 严格分目录 |
| **R4** | **Evidence Index 字符串匹配误判** | 误标 `used_in_final_answer` | ① 只标 `bool`，不标 `is_fact` ② 高显著片段阈值 ≥12 字符 ③ 出现误判证据时升级为 LLM 判定 |
| **R5** | **Pin 超预算无解**（§27 只要求记录，不解决） | 极端情况 Context 溢出 | ① 不 raise，记 warning ② UI 显示 Pin 占用提示 ③ 用户可手动 Unpin（唯一解法，符合 §27） |
| **R6** | **`k` 未提供时默认 0.5**，可能与用户预期不符 | 历史保留量不匹配 | ① 前端显示当前值 ② 每次 Round 记 `context_policy.k` ③ 用户可调 |
| **R7** | **OpenRouter 元数据获取失败**（网络/限流） | context_limit 降级为 estimated | ① 磁盘缓存 24h ② 降级链清晰（§9.2）③ accuracy 标记 |
| **R8** | **递归压缩 3 次仍超限** | 该轮 Context 质量下降 | ① 记 `max_compaction_rounds_reached` ② 降级而非中断 ③ 日志可诊断 |
| **R9** | **本 Plan 未做真实 LLM E2E**（与上次 V2 验收同样限制） | 无法证明真实 LLM 的 Summary 质量 | Step 3 完成后用 `minimax_m2_7` 做真实 E2E；免费层 429 时需重试或换模型 |
| **R10** | **`storage.load_responses` 的 `.prompt.md` 后缀 hack 未立即移除** | 迁移期脆弱性 | 新增 `load_round_bundle()` 显式读 `responses/`，旧路径保留兼容一个版本后删 |

---

# 24. 闭环自检表

> 用户任务书 §二十一：只有 Plan 自身能证明全链路闭环，才允许进入代码重构。

| 环节 | 覆盖位置 | 状态 |
|---|---|---|
| MD Requirement | §1 矩阵 19 行 × 9 列 | ✅ |
| Target Module | §1 矩阵 + §20 依赖图 | ✅ |
| Data Model | §4（11 个数据结构） | ✅ |
| API | §18.1（6 个端点，含链路完整性 trace） | ✅ |
| UI | §18.2（5 个控件 + Summary 状态机） | ✅ |
| Persistence | §1 矩阵 Persistence 列 + §17.4 | ✅ |
| Logging | §17（10 类事件 × 必须字段 × 7 问可回答性） | ✅ |
| Tests | §19（16 项任务书测试 + 6 项补充 + 旧测试迁移表） | ✅ |
| 旧逻辑清理 | §2 迁移表（18 项） | ✅ |
| 数据流 | §3（3 条正常流 + 1 条禁止路径） | ✅ |
| 公式 | §10（§21 公式 + 调用位置表） | ✅ |
| 终止条件 | §15（4 个 break 条件） | ✅ |
| 借用声明 | §21（6 项 Borrowed + 11 项 Custom + 差异说明） | ✅ |
| 风险 | §23（10 项） | ✅ |

---

# 25. Step 2 交付结论

| 项 | 计数 |
|---|---|
| **需求映射矩阵** | **19 / 19** |
| **数据结构** | **11 / 11**（`TokenEstimate`, `ValidRound`, `HistoricalContextItem`, `ContextBudget`, `PinState`+`PinEvent`, `ProviderModelMetadata`, `ConversationSummary`, `PeerContextItem`, `CompactionRecord`+`CompactedBlock`, `McpEvidenceEntry`, `ContextSelectionResult`） |
| **调用链** | **4 / 4**（普通对话 / 用户 Summary / Peer Review / ⛔禁止路径） |
| **旧逻辑清理** | **18 / 18**（含 `HISTORY_WINDOW=3`、`should_compact` ratio、固定 16000 reserve、旧 history loader、旧 prompt builder、旧 compactor、旧 peer history） |
| **测试迁移计划** | **28 新增（含修订专项 6 个） + 2 废弃 + 22 重写 + 55 保留** |
| **日志事件** | **10 / 10** |
| **借用 vs 自定义** | **6 Borrowed / 11 Custom / 5 差异说明** |

## 25.1 修订专项同步落点（2026-09-03 Step 2 修订 #1–#5）

| 修订 | 影响的章节 | 落点（已同步） |
|---|---|---|
| **#1 Summary ≠ Session Compaction** | §1 矩阵 row 9/14、§4.9 `CompactionRecord`+§4.9.1 完整区分表、§5.7 红线 3 条、§6 表 + §6.0 五条防线、§14.4 复用时归属校验、§17.2 event 7 新增 `contributor_models[]` `viewpoint_attribution` `artifact_kind` | ✅ |
| **#2 命名 `model_max_output`** | §1 矩阵 row 7/8、§4.4 `ContextBudget` + 不变量块（T21）、§4.6 `ProviderModelMetadata` + 命名映射表、§9.1/9.2/9.3 全量改名 + 反固定值规则、§17.2 event 2 / §17.3 示例、§20 依赖图 | ✅ |
| **#3 Summary 复用规则** | §1 矩阵 row 11/12、§4.7 `ConversationSummary.source_valid_round` + `reused_from`、§4.7.2 `SummaryReusePolicy` 判定表 + 成本对比、§7.3 Peer 集成复用分支 + 边界规则 | ✅ |
| **#4 Derived Artifact** | §1 矩阵 row 9、§4.7.1 不变量 D1/D2/D3 + `__post_init__`、§5.6 三条硬性不变量、§17.2 event 5 新增 `source_valid_round` | ✅ |
| **#5 同步到矩阵/数据流/测试** | §1 矩阵 6 行标记（7、9、11、12、14）、§3 数据流（在 §4.7.1 流程图与 §6 表格中体现）、§19.4 新增 T17–T22（6 个专项测试） | ✅ |

**本轮修订未修改任何业务代码、未新增依赖。**

---

**下一步**：Step 3（S3.1 → S3.12）。按用户任务书 §二十一，**等待确认后再进入代码修改**。
