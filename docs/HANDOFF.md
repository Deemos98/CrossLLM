# CrossLLM Agent Handoff

> 生成时间：2026-09-05 14:30（GMT+8）
> 生成者：上一任开发 Agent（Phase 4 / S3.1～S3.10 会话）
> 接收者：下一任开发 Agent（无当前会话上下文）

---

## 0. ⚠ 必读前置声明（Conflicts 先行）

1. **`docs/CrossLLM Agent Handoff Specification` 在仓库中不存在。**
   已全仓库检索（docs/ 15 个文件、根目录、子目录）均无此文件。
   本 Handoff 按用户指令中的 12 项必录清单 + 行业惯例构建。
   **新 Agent 的第一件事可以是：与用户确认规范是否应被创建/补交。**

2. **项目不是 Git 仓库**（`git status` → `fatal: not a git repository`）。
   无提交历史、无分支、无远端。回滚手段 = 手工备份。
   ⚠ 新 Agent 若依赖 git diff 审查改动，需先 `git init`（需用户同意）。

3. **文档与实现的目录结构冲突（实测确认，见 §14 冲突清单）。**
   `docs/phase4_memory.md` §62 的目录布局与实际落盘不一致。
   实现是**正确**的一侧（S3.8/S3.9 用户任务书明确改了设计），MD 待更新。

---

## 1. 当前 Phase / S 阶段

| 项 | 状态 |
|---|---|
| 项目 | CrossLLM —— Multi-LLM Cross-Review Workbench |
| 当前 Phase | **Phase 4：Context Management & Long-Term Memory** |
| 当前 Step | **S3.10（全链路整合验收）已完成** —— Phase 4 Step3 全部结束 |
| 下一步 | 等待用户指令（候选：S4 RoundManager 重写 / MD §62 更新 / 5 个 xfail 清理）。**上一任未被授权开始任何下一阶段** |

**权威文档**（按优先级）：
1. `docs/phase4_memory.md`（3380 行，71 节，需求规范，§69 是验收标准）
2. `PHASE4_REFACTOR_PLAN.md`（2077 行，实现计划 + 修订记录，§19 有测试迁移表）
3. `PHASE4_AUDIT.md`（Phase 4 开工前审计，历史参考）
4. 本文档（当前状态快照）

---

## 2. 已完成阶段（全部经实测验证）

| 阶段 | 内容 | 验证依据 |
|---|---|---|
| Phase 1 | 基础 Round/Session/多模型/匿名化/Candidate | `test_phase1.py` 28 passed |
| Phase 2 | 真实 LLM 接入 | `test_phase2.py` 46 passed + 3 xfail |
| Phase 3 | MCP 部署 + Runtime Log | `test_phase3.py` 10 passed + `test_mcp_runtime_log.py` 17 passed |
| S3.1 | ModelCapability（TokenEstimate/ProviderModelMetadata/上下文限制解析） | `test_model_capability.py` 25 passed |
| S3.2 | ValidRound / ValidRoundPoolBuilder（失败 Round 过滤，I1/I2 不变量） | `test_valid_round.py` 22 passed |
| S3.3 | ContextBudget（compute_budget 纯函数，K_MIN=0.01/K_MAX=0.90） | `test_phase4_budget.py` 30 passed |
| S3.4 | PinStore（Pin/Unpin/持久化/pinned_tokens） | `test_phase4_pin.py` 31 passed |
| S3.5 | ContextSelector（Pin 优先→Recent Raw 倒序 while-break→预算驱动） | `test_phase4_context_selection.py` 13 passed |
| S3.6 | ConversationSummary（派生制品三不变量 D1/D2/D3 + SummaryReusePolicy） | `test_phase4_summary.py` 30 passed |
| S3.7 | PeerContextPreparator（valid_answer_count 分流 + ALLOWED_SOURCE_TYPES 数据层白名单） | `test_phase4_peer_context.py` 23 passed |
| S3.8 | SessionCompaction（CompactionRecord/CompactedBlock/递归/观点归属三层防线） | `test_phase4_compaction_service.py` 32 passed |
| S3.9 | MCP Evidence Index（used_in_final_answer 判定 + Runtime 追溯） | `test_phase4_evidence.py` 24 passed |
| S3.10 | 全链路整合（k/summary_mode/request_max API 贯通 + context_policy 事件 + E2E） | `test_phase4_e2e.py` 11 passed + `scripts/run_phase4_e2e.py` 14/14 PASS |

**当前测试基线（2026-09-05 实测）**：
```text
358 passed, 5 xfailed in ~123s（pytest -p no:cacheprovider，Windows/Python 3.13.14）
E2E：scripts/run_phase4_e2e.py → 14/14 PASS（exit=0）
```

---

## 3. 当前阶段（S3.10）实际完成情况

全部为**已实现 + 已测试**：

1. **API 参数全链路**：`RunRoundIn` 增 `k` / `summary_mode` / `request_max_output`；
   `POST /api/session/{sid}/round` 回显后端实际采用值；
   `prepare_round` 夹紧 k∈[0.01, 0.90]；`RoundState` 增三字段（含序列化）；
   `_build_prompts_async` 消费 `round_state.k`（原硬编码 0.5 已删）。
2. **context_policy 事件**（MD §55）：每次 `execute_round` 落
   `{event, session_id, round_id, k, request_max_output, summary_mode, peer_review, timestamp}`。
3. **日志字段补全**（MD §12）：`context_budget` / `history_selection` /
   `context_budget_warning` 事件补 `timestamp` + `session_id` + `round_id`。
4. **E2E 重写**：旧 `scripts/run_phase4_e2e.py` 因 `import HISTORY_WINDOW` 已损坏
   （S3.5 删常量后未同步），彻底重写为 14 步黑盒验收。
5. **UI**：k 滑块（0.01–0.90）→ `readRoundPolicy()` 只返回 `{k}`。
   ⚠ 2026-09-25 更新：`req-max-output` 输入框**已移除**（§四：request 输出上限
   不由 UI 手工配置）；模型能力上限来自 Provider 元数据。

---

## 4. 最近修改的文件（S3.10 触及）

```text
app.py                          RunRoundIn 三参数 + 回显；evidence API
core/state.py                   RoundState + k/request_max_output/summary_mode
core/round_manager.py           prepare_round 三参数；k 消费；context_policy 事件；
                                _build_evidence_for_round（S3.9）
core/context_selector.py        _log_selection(round_id) + timestamp/session_id
core/compaction.py              S3.8 新建（本轮未改，md5 b347e6b8…）
core/evidence_index.py          S3.9 新建（本轮未改）
core/prompt_builder.py          build_round_n_prompt_v3 + compacted 透传（S3.7/S3.8）
core/peer_context.py            S3.7 新建（本轮未改）
core/summary_service.py         S3.6 新建（本轮未改）
static/index.html|app.js|style.css  k 滑块 / Evidence 面板 / Summary UI
scripts/run_phase4_e2e.py       彻底重写
test_phase4_e2e.py              新建
test_phase4_{summary,peer_context,compaction_service,evidence}.py
                                md5 守门基线随计划内接线更新
docs/HANDOFF.md                 本文档
```

**md5 基线**（新 Agent 验证未被人动过核心语义）：
```text
core/model_capability.py  ced60c151c0387fd45a0ae8c087eabd4   (S3.1)
core/valid_round.py       e181b3d352a796ab865cd6d5e7688b9b   (S3.2)
core/context_budget.py    3c4b189bfbec6896825b88d673057397   (S3.3)
core/pin_store.py         18bc22909415627b089dea757f210642   (S3.4)
core/summary_service.py   aca62f6ad660d3c5b987bd91a457b153   (S3.6)
core/summary_prompt.py    36940511858a59b3cd8c540437961b94   (S3.6)
core/peer_context.py      fdd6d491e9bbeca33f7705c69da9b17d   (S3.7)
core/context_selector.py  e3316c6daa33363909a285b36f33d0ec   (S3.5 算法未动；S3.10 仅加日志字段)
core/compaction.py        b347e6b8fa7f…                      (S3.8)
```
这些 md5 被 4 个守门测试持续断言（`test_guard_md5_*`），改核心模块会先红测试。

## 5. 最近修改的代码逻辑（新 Agent 必须理解的接线）

1. **k 链路**：UI 滑块 → `readRoundPolicy()` → `POST /round {k}` →
   `prepare_round(k=…)`（夹紧）→ `RoundState.k` → `ContextSelector.build(k=…)` →
   `ContextBudget.k` → `lossless_budget = history_budget × k` →
   `context_policy` / `context_budget` 事件同值。**禁止任何一环重新硬编码**。
2. **request_max_output**：API 值优先，缺省用 Provider 能力（`min_mo`）
   兜底；`reserved = min(provider, request)`（`compute_budget` §9.2 四分支）。
3. **Peer Context 流**：`execute_round → _build_prompts_async →
   PeerContextPreparator.prepare() → build_round_n_prompt_v3`。
   历史 item 只渲染 self（v3），peer 信息走独立块。
4. **Compaction 流**：probe（不落日志）拿 `dropped` 区 → `find_reusable` /
   `compact` → `_make_compacted_item` → 正式 `build(compacted_items=…)` →
   `build_round_n_prompt_v3` 对 `source=="compacted"` 整体透传（F3 归属标题内置于 content）。
5. **Evidence 流**：`execute_round` gather 后 → `_build_evidence_for_round`（每个
   complete 模型）→ `evidence/mcp_evidence.jsonl` 追加 + `mcp_evidence_index` 事件。

---

## 6. 当前测试结果（实测 2026-09-05）

```text
15 个测试文件，358 passed + 5 xfailed（~123s）
运行：.venv/Scripts/python.exe -m pytest -p no:cacheprovider
环境：Windows 10 / Python 3.13.14 / pytest 9.1.1
```

**5 个 xfail 明细**（全部是"旧语义已删、测试待重写"，不是 bug）：
```text
test_phase2.py::test_history_window_is_three_rounds        （HISTORY_WINDOW 已删）
test_phase2.py::test_budget_trim_drops_oldest_first        （_trim_to_budget 已删）
test_phase2.py::test_truncate_when_single_round_still_too_long（_truncate_entries 已删）
test_phase4_compaction.py::test_maybe_compact_history_triggers_and_persists（80% 阈值已删）
test_phase4_compaction.py::test_build_prompts_with_compact_summary_skips_history_block
```
前 3 个 xfail 的 reason 写明"待 S3.8/S3.9 重写"——S3.8/S3.9 已完成，
**这 3 个测试的重写是遗留债务**（对应新语义已被
`test_phase4_e2e.py::test_history_no_window_and_pin_priority` 等覆盖）。

## 7. 当前 E2E 结果（实测 2026-09-05）

```text
scripts/run_phase4_e2e.py → 14/14 PASS（exit=0，~15s）
S1.session / S2.api_k_link / S3.mcp_evidence / S3b.runtime_intact
S4.compaction / S4b.budget_k_link / S5.pin_lossless / S6.peer_source_legal
S7.summary_chain / S8.budget_identity / S9.raw_archive / S10.logs_complete
S11.dir_isolation / S12.prompt_blackbox
```
关键实测数（k=0.7, request=4096, provider_max=2048）：
`reserved=2048=min(2048,4096)`；`history=6100=8192−44−2048`；
`lossless+compaction=history`；`round_NNN/*` 全程 md5 不变。

---

## 8. 已实现（含证据）

见 §2 表格 —— 每行都有对应测试文件与通过数。补充跨模块语义：

- Conversation Summary 严格按 model_id 隔离（D1 不参与 Round 编号 /
  D2 不进 Valid Round Count / D3 不写 Final Answer），`__post_init__` 强制。
- Peer Review 隔离三层：L1 不落盘（`rglob("*peer_context*")` 为空）、
  L2 `ALLOWED_SOURCE_TYPES` 白名单、L3 日志 source_id 前缀审计。
- Session Compaction 与 Summary 三重防冒充：artifact_kind 断言、
  目录隔离（`compactions/` vs `summaries/`）、复用时拒绝非 compaction 产物。
- Evidence 永不 Fact：AST 守门禁 `is_fact/confidence/score` 标识符。

## 9. 已测试（覆盖方式）

- 单元不变量：dataclass `__post_init__` / 复用判定表 / 预算恒等式。
- 服务行为：复用降级、递归 attempt、失败降级（不抛异常）。
- API 集成：FastAPI TestClient（storage monkeypatch 到 tmp）。
- 真链路：SessionManager+RoundManager+注入式 provider 跑真实 Round，
  断言**落盘的 `round_NNN/*.prompt.md`**（黑盒）与 `context_policy.jsonl` 事件流。
- 守门：AST 扫描（禁旧常量/禁越界 import/禁 Fact 语义）+ md5 基线。

## 10. 设计已确定但尚未实现

| 项 | 出处 | 状态 |
|---|---|---|
| Project Memory（memory_001.md / memory_index.jsonl / projects/ 树） | MD §49-§53 §62 | 明确不做（S3.11 候选，未获授权） |
| Evidence Index 升级为 LLM 判定 | MD §16.3 | 当前为字符串匹配（可解释第一版），规范明许 |
| Evidence 进 Prompt 的"必要 MCP Evidence" | MD §63 | 当前 Evidence 只进 API/UI，不进模型 context |
| summary_mode 的 UI 行为日志（summary_mode_enabled/disabled） | MD §56 | 第一版非必须，未实现 |
| 3 个 test_phase2 xfail 的新语义重写 | PLAN §19.1 | 语义已由 S3.5/S3.10 测试覆盖，旧文件待删/重写 |
| MD §62 目录结构图更新 | 见 §14 冲突 | MD 待改（实现已变） |

## 11. 已知问题（不影响当前验收）

1. `_maybe_compact_history` 仍是 S3.3 stub（每次落
   `compaction_trigger_skipped reason=s33_stub` 日志）。真实触发由
   ContextSelector 驱动。stub 可在 S4 删除。
2. R1（`build_round1_prompt`）不产生 `context_budget` 事件（无历史）。
   MD §57"每次建立 Prompt 必须记录"在 R1 上不满足——如需满足须单独接线。
3. 全量 pytest ~123s（真链路测试多）；CI 可按文件并行。
4. Token 估算为 `len/3` 字符粗估（accuracy_mode='estimated'，§25 声明不假装精确）；
   Provider 真实 usage 只记 `round_state.usage`，未反馈进预算。
5. 沙箱环境下对项目内 `.pytest_tmp` 的 `unlink` 有保护（safe-delete），
   需要 unlink 的测试应使用 OS 临时目录（`test_phase4_evidence.py::test_4` 是范例）。
6. 全量测试需显式 `timeout≥300s`（默认 120s 会误判崩溃）。

## 12. 未解决问题（需用户决策）

1. Handoff Specification 文件缺失（见 §0.1）。
2. 是否 `git init`（见 §0.2）——当前无版本控制。
3. MD §62 目录图与实现不一致，**改 MD 还是改代码**需用户裁决
   （建议改 MD，理由见 §14）。
4. 下一步阶段选择：S4（RoundManager 重写）/ MD 修订 / xfail 清理，均未授权。

## 13. 当前 Scope

**在 Scope 内（Phase 4 已授权并完成）**：
Context Selection（k/Pin/Budget/Selector）、Conversation Summary、
Peer Context、Session Compaction、MCP Evidence Index、全链路整合验收。

**明确不在当前 Scope（禁止擅自实现）**：
Project Memory 及其任何形式、Evidence Ranking/Vector DB/Fact 管理、
Memory Graph/Memory Agent、自动重要性评分、每次 tool call 的额外 LLM 调用、
Peer Review 结果写回其他模型 Summary、无限递归 Compaction、
强制 Summary 固定 schema（MD §67 第一版明确不做清单 —— 全部仍然有效）。

## 14. 文档与代码冲突清单（不得隐瞒）

实测确认（2026-09-05）：

| # | 冲突 | MD 说法 | 实际实现 | 判定 |
|---|---|---|---|---|
| C1 | 响应文件布局 | §62：`round_NNN/responses/` | `round_NNN/<model>.md` 平铺（phase1 起即如此） | **实现正确**；MD §62 图过时 |
| C2 | Compaction 落盘 | §62：session 根 `compact_001.md` | `compactions/compact_NNN.md`（S3.8 用户任务书明确要求独立目录） | **实现正确**（用户任务书 > MD）；MD 待更新 |
| C3 | Evidence 目录 | §62：无此目录 | `evidence/mcp_evidence.jsonl`（S3.9 新增） | MD 缺失，待补 |
| C4 | prompt 文件名 | §62：`round_NNN/prompt.md` | `round_NNN/<model>.prompt.md`（每模型一份） | 实现正确；MD 简写 |
| C5 | R1 budget 事件 | §57"每次建立 Prompt 必须记录" | R1 走 round1 prompt 无 budget 事件 | 已知偏差（§11.2），待用户裁决 |
| C6 | Handoff Spec | 用户指令引用 | 文件不存在 | 见 §0.1 |

**一致项**（抽查通过）：k 范围 0.01–0.90（§20/§55 = K_MIN/K_MAX）；
§21 预算恒等式；§29 倒序装载；§31 Q&A 不可分；§46/§47 Evidence 语义；
§48 生成时机；§57/§59/§60/§42 日志字段；§69 验收标准逐条可对应测试。

## 15. 已锁定的架构不变量（新 Agent 不得破坏）

```text
A1  Raw Archive 永不删改：round_NNN/* 字节级只增不改（E2E S9 md5 证明）
A2  失败 Round 不进正常 Context（ValidRoundPoolBuilder I1/I2）
A3  模型 context 中禁止真实身份信息（anonymizer IdentityLeakError 硬校验）
A4  candidate_ids 不进模型 context / 不写 round_NNN/ / 不出 API
A5  Summary 三不变量：is_derived_artifact=True / creates_round=False /
    contributes_valid_round_count=False（__post_init__ assert）
A6  Compaction artifact_kind 恒 "session_compaction" ≠ "conversation_summary"（assert）
A7  Peer Context 来源白名单 {final_answer, conversation_summary}（数据层 ValueError）
A8  Evidence 只有 used_in_final_answer，永不 is_fact（AST 守门）
A9  失败降级不阻断：Summary/Compaction/Evidence 失败只降级记日志
A10 预算恒等式：history = limit − fixed − reserved；lossless+compaction = history
A11 k ∈ [0.01, 0.90]（K_MIN/K_MAX 夹紧）
A12 Pin 不解除不压缩（§27：pinned_history_exceeds_budget 只告警）
```

**重要设计决策及原因**（防止新 Agent "好心改回去"）：
- **Peer Context 数据层白名单而非 Prompt 文案**（§8.2）：即使 DB 里有
  Peer Review Result 也没有 loader 能读它——无对应 source_type 分支。
- **历史 v3 只渲染 self + peer 独立块**（§30"最近一轮不再是唯一来源"）；
  compacted 块不受 peer_review self-only 过滤影响（曾因此丢失压缩块，已修）。
- **Compaction 递归输入 = 上一轮产物**（§38 summary-of-summary），
  `source_summary_files` 记录链路。
- **Summary 复用按 `source_valid_round`**；`trigger='user'` 绕过复用总是新建
  （§8"我点了总结却没变化"体验问题）。
- **Evidence 判定 = 字符串匹配**（tool_call_id 显式引用 ∨ preview 子句级
  ≥12 字符逐字引用），MIN_MATCH_LEN=12 防短串误判；§16.3 明许第一版不调 LLM。
- **`conversation_summary` 事件无 round_id、时间字段叫 `created_at`**：
  D1 派生制品语义，不是遗漏。

## 16. 下一步应该做什么（候选，均未授权）

按建议优先序：
1. **与用户确认 Handoff Spec 缺失** + 是否 git init（§0）。
2. **更新 MD §62** 目录图为实际布局（§14 C1-C3），消除文档漂移。
3. **清理 3 个 test_phase2 xfail**：删除或重写为新语义断言
  （新语义已被 e2e 覆盖，重写是低风险收尾）。
4. **删除 `_maybe_compact_history` stub**（连带 2 个
   test_phase4_compaction.py xfail），S4 一并处理。
5. **R1 budget 事件**（若用户裁定 §57 必须覆盖 R1）。
6. 之后才进入 MD §68 的下一大阶段（S4 / Project Memory 等）。

## 17. 明确禁止做什么

1. ❌ 未经用户授权实现 Project Memory / Evidence Ranking / Vector DB /
   Fact 管理 / Memory Agent（MD §67 清单）。
2. ❌ 修改 §15 任何不变量对应的 assert / 常量 / 守门测试来"让测试通过"。
3. ❌ 恢复 `HISTORY_WINDOW` / `should_compact` / 80% ratio /
   `_trim_to_budget` / `_truncate_entries`（AST 守门会红）。
4. ❌ 把 Peer Review Result 接入 Peer Context（哪怕加一个
   `source_type="peer_review_result"` 分支——白名单就是防线）。
5. ❌ 让 Evidence / Summary / Compaction 任一产物互相冒充
   （目录、artifact_kind、字段三者都是防线）。
6. ❌ 在模型 context 中引入真实 model_id（A3/A4）。
7. ❌ 删除或修改 `mcp_runtime.jsonl` 的既有字段/脱敏逻辑（§45）。
8. ❌ 绕过 API 直接在 Prompt 里拼政策参数（k 必须走链路）。
9. ❌ 假设 Unix 路径分隔符（Windows 项目：一律 `pathlib`，禁
   `startswith("foo/")` 判层级）。
10. ❌ 在授权前开始 S4 / 下一 Phase。

## 18. 快速上手指南（新 Agent 第一小时）

```bash
# 1. 环境确认
cd /c/pythonProject/CrossLLM
.venv/Scripts/python.exe --version          # 3.13.14

# 2. 跑全量（≥300s 超时！）
.venv/Scripts/python.exe -m pytest -p no:cacheprovider -q
# 期望：358 passed, 5 xfailed

# 3. 跑 E2E
.venv/Scripts/python.exe scripts/run_phase4_e2e.py
# 期望：14/14 PASS，exit=0

# 4. 读文档顺序
docs/phase4_memory.md §1-§3 §20-§29 §62-§69   # 需求主线
PHASE4_REFACTOR_PLAN.md §1 矩阵 + §19 测试迁移表
docs/HANDOFF.md（本文档）

# 5. 启动 API（如需手测 UI）
.venv/Scripts/python.exe -m uvicorn app:app --port 8000
# 浏览器 http://localhost:8000 （static/ 前端）
```

**启动入口**：`app.py`（FastAPI，单例 storage/sessions/rounds/pins/
summaries/evidence_index）；`config/` 下模型配置；`.env` 有 Key（勿提交）。

---

## 附：本 Handoff 的验证声明

本文档中所有"已实现/已测试"结论均在 2026-09-05 14:15–14:30 通过
实际运行验证（358 passed / E2E 14/14 / 目录 rglob 实拍 / md5 现算），
非转录历史聊天。若新 Agent 复跑结果不符，以上文 md5 基线为准先排查
代码是否被改动，再排查环境。
