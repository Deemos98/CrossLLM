# Reference Project Analysis

> CrossLLM (MAD-Hub) Phase 0 输出 — 参考项目源码侦察报告。
> 依据：`architecture_v0.1.md.md`（总架构规范，本阶段未做任何修改）。
> 本报告只描述参考项目的现状，所有"是否采用"的结论均以 architecture_v0.1 为唯一依据。
> 克隆方式：全部使用 `git clone --depth 1`（浅克隆），reference 仓库未做任何修改。

| Repo | Path | HEAD commit |
|---|---|---|
| raiyanyahya/ensemble | `reference/ensemble` | `47a8f14` |
| niveshdandyan/llm-council | `reference/llm-council` | `9f369a5` |
| sammy995/Local-LLM-Arena | `reference/Local-LLM-Arena` | `17be0db` |

---

## 1. ensemble

### Architecture

- **模式**：多轮"文件系统辩论"。每个模型是一个独立 asyncio agent 循环，外加一个 coordinator 循环；模型之间**不直接对话**，全部通过共享目录下的 Markdown 文件通信（`~/.ensemble/debates/<debate-id>/`）。
- **Phase 状态机**：`PROPOSING → REVIEWING → REBUTTAL → VOTING → (SYNTHESIS → CONFIRM) → DONE/DEADLOCKED`。每一轮完整走前四个 phase；仅当投票出现多数 ENDORSE 时才进入 SYNTHESIS（胜者作者合并答案）和 CONFIRM（全员 APPROVE/REJECT）。
- **Round 1 独立性**：第一轮模型只读"原始问题 + 系统公共指令"，看不到彼此（`build_agent_prompt` 中 PROPOSING 阶段不注入其他模型内容）。从 REVIEWING 起模型能看到其他参与者（匿名为 Participant A/B/C）的本轮完整内容，过去轮次被截断（proposal 6000 字符、review 3000 字符）以控制上下文。
- **结束条件**：无固定轮数。多数（n//2+1）FINALIZE 同一 proposal → finalize；连续两轮投票+背书完全相同 → stable disagreement deadlock（写 plurality 最佳努力答案）；另有 `--rounds`（默认 50）与 `--budget` 两个安全阀。单模型 API 反复失败会被 **drop**（只要剩余 ≥2 模型）。
- **入口**：CLI（`chat`/`debate`/`list`/`status`/`show`/`resume`）+ MCP server（`ensemble-mcp`）。无 Web UI。

### Relevant Modules

- `src/state.py` — 核心数据结构：`Phase`、`Vote`（FINALIZE/REVISE/SPLIT）、`ModelOutput`（proposal/reviews/rebuttal/vote/ranking/synthesis/confirm）、`RoundState`、`DebateState`；`merge_from`（跨 phase 累加不覆盖）、`save_state`（tempfile + os.replace 原子写）。
- `src/coordinator.py` — phase 推进、投票统计、背书解析、Borda 计分、deadlock 判定、final.md 渲染。
- `src/agent.py` — 每个模型一个 agent：轮询 state.json → 按当前 phase 构建 prompt → 调用 provider → 原子写贡献文件 + usage sidecar；失败 4 次写 `.failed` sentinel。
- `src/orchestrator.py` — CLI/chat/MCP 共用的 build + run 编排，`--roles` 反群体思维立场库。
- `src/models.py` — Provider 注册表（gpt4o/claude/deepseek/sonnet），每种一个 `call` 函数（非 LiteLLM），重试 + 指数退避，token/缓存/成本核算。
- `src/mcp_server.py` — FastMCP stdio server，暴露单一 `ensemble_debate` 工具。
- `src/search.py` — Tavily web search grounding（可选，`TAVILY_API_KEY`）。
- `src/ui.py` — 极简终端进度面板。

### Persistence

- **全部落盘**：`prompt.md`、`state.json`（含投票/背书/Borda/synthesis/confirm 的完整状态，原子写、可恢复）、`round-XXX/<model>.<phase>.md`（每个 phase 单独一个文件，历史不被覆盖）、`*.usage.json` 用量 sidecar、`*.failed` 失败哨兵、`final.md`（共识/最佳努力答案 + 全部 verbatim 提案 + Borda + 成本 + 来源）。
- **Resume**：`ensemble resume <debate-id>` —— 由于 coordinator 与 agents 都从 `state.json` 轮询恢复，中断后可继续。

### MCP / Web

- **MCP**：是。stdio 传输的 FastMCP server，一个工具 `ensemble_debate(prompt, quick, rounds, models, ground, budget)`；面向 Claude Code / Cursor / Cline / Kilo 等任何 MCP client。
- **Web**：否。无 HTTP 服务，只有 CLI + MCP。
- **Grounding**：是（可选）。Tavily 搜索 → `Source(title/url/snippet)` 列表写入 state 并注入所有模型 prompt，要求以 `[n]` 引用，来源附在 final.md。

### Reusable Ideas

适合 MAD-Hub 借鉴的部分：

1. **文件系统持久化 + 原子写**：`tempfile.mkstemp + os.replace` 保证轮询读者永远看不到半写文件 —— 直接对应 architecture 中 storage.py 的 "SQLite + 文件系统"。
2. **Round 状态建模**：`rounds[]` 列表 + 每轮 `RoundState(round_num, phase, model_outputs, votes, …)`，按 round 目录存储、历史只增不覆盖 —— 与 v0.1 的 RoundState/SessionState 高度同构。
3. **Round 1 隔离 + 上下文控制**：PROPOSING 不注入他模型内容；过去轮次截断、当前轮完整展示 —— 正好对应 v0.1 P2/P3 与 §27 Context Control。
4. **跨 phase 累加合并**（`merge_from`）：模型某 phase 输出只取该 phase 字段，防止"提前泄露的投票"被误计 —— 对 MAD-Hub 的多轮状态合并有参考价值。
5. **失败容忍**：单模型失败写 sentinel、coordinator 继续推进（v0.1 §25 要求"单模型失败不得取消整个 Round"）。注意：ensemble 是**自动 drop**，MAD-Hub 的 CLOSED 是**用户驱动**，只能借鉴"失败不阻塞"这一层。
6. **成本/用量核算**：per-call usage sidecar + 汇总 —— 对应 v0.1 §28 数据完整性。
7. **Roles / 反群体思维立场**：`--roles diverse/redteam` —— 未来可选，v0.1 未禁止。
8. **匿名化标签**（Participant A/B/C）只在模型间隐藏身份 —— 与 MAD-Hub 无关，但思路可作 UI 可选项。

### Conflicts with MAD-Hub

与 architecture_v0.1 明确冲突、**不得采用**的部分：

- **投票定正确性**：FINALIZE/REVISE/SPLIT 投票 + 多数背书判定共识 → 违反 P4（不使用多数投票直接判断正确性）。
- **自动 Consensus / Deadlock 判定**：多数一致 → finalize；位置冻结 → deadlock 写最佳努力答案 → 违反 P8（不自动强制 Consensus、允许保留分歧）。
- **Synthesis-as-candidate + CONFIRM**：胜者作者合并答案、全员批准 → 接近 P7 禁止的自动综合/终裁（"endorsed author merges"）。
- **Borda 排名**：模型间排名信号（即使只破平局）→ 属于"模型排行榜/权重"灰色地带，v0.1 §11 明确禁止模型信誉分/排行榜。
- **自动 drop 模型**：providers 失联自动剔除 —— MAD-Hub 只允许用户 CLOSED，模型状态 CLOSED ≠ WRONG（P6）。
- **无 Web UI、无流式**：与 v0.1 Phase 1 的 SSE 多列 UI 不兼容，只能参考不能照搬。

### License

- Project: raiyanyahya/ensemble
- License: **MIT**（仓库内 `LICENSE` 文件，Copyright (c) 2026 Raiyan Yahya）
- Relevant restrictions: 需保留版权声明与许可文本；无其他限制。
- Reuse recommendation: **Reuse if license permits**（源码可复制，但复制内容必须剔除上述与架构冲突的逻辑；许可证要求保留 MIT 版权声明）。

---

## 2. llm-council

### Architecture

- **模式**：零依赖（纯 Python stdlib `ThreadingHTTPServer` + 原生 JavaScript 前端）。浏览器 POST `/api/council/stream` → 后端用 `ThreadPoolExecutor` 并行请求多个模型 → 通过 SSE 逐模型推送事件 → 前端卡片实时刷新。
- **单轮模型**：无多轮辩论。一次会话 = 一次并行问答（可选的后续动作：匿名投票、共识综合、主席终裁）。
- **API 形态**：所有模型请求统一走第三方 "AI Gateway"（`ai-gateway.happycapy.ai/api/v1`，Bearer token + 固定 Origin 头），模型 ID 用点号表示法（`anthropic/claude-sonnet-4.5`）。前端 6+3 个模型硬编码在 `AVAILABLE_MODELS`。
- **入口**：`python server.py` → http://localhost:8787 仪表盘。

### Relevant Modules

- `scripts/server.py` — ThreadingHTTPServer + SSE 端点：`/api/council/stream`（队列 + 后台线程转发 SSE）、`/api/council/vote`、`/api/council/synthesize`、`/api/council/chairman`；显式发送 `{"status":"done"}`；vote/chairman 用 `: keepalive` 注释防代理超时。
- `scripts/ai_gateway.py` — 单网关客户端：`query_models_parallel`（ThreadPoolExecutor）、`query_models_stream`（队列 + 生成器，每模型 2 个事件 started/complete|error）、`anonymous_model_vote`（每模型匿名打分 1-10，最高分即 winner）、`synthesize_responses`（Claude 综合共识/分歧/最佳答案/置信度）、`chairman_review`（主席"权威最终答案"）。
- `scripts/static/app.js` — fetch + ReadableStream 手工解析 `data:` 行；`handleSSEEvent` 按 started/streaming/complete/error 更新卡片；模型下拉选择器；`localStorage` 历史（最近 10 条摘要）。
- `scripts/static/index.html` — CSS Grid `repeat(auto-fill, minmax(380px, 1fr))` 响应式卡片网格，按 provider 着色。

### Persistence

- **仅浏览器 localStorage**：`saveToHistory` 只保存摘要（prompt + 模型列表 + 每模型 status/tokenCount/duration/120 字 preview），最多 10 条，点击历史只回填输入框。
- **服务端无持久化**：响应内容、会话状态不落盘；刷新即丢，不可 resume。

### MCP / Web

- **MCP**：否。无任何 MCP 实现。
- **Web**：是（这是它的核心形态），但无 grounding / 无 Web Search。
- 依赖第三方 AI Gateway，密钥是单一网关密钥，不是各厂商直连。

### Reusable Ideas

适合 MAD-Hub 借鉴的部分（主要在前端与 SSE 传输细节）：

1. **零依赖 SSE-over-POST 服务端模式**：后台线程 + `queue.Queue` 转发 + 显式 `{"status":"done"}` 结束标记 —— 与 v0.1 Phase 1（Mock 模型 + SSE + 原生前端）直接可用。
2. **SSE keepalive**：长任务（投票）每 5s 发 `: keepalive` 注释防代理超时 —— MAD-Hub 后期证据检索可借鉴。
3. **原生 JS 流式解析**：`ReadableStream` + `TextDecoder` + 行缓冲 + `streamDone` 提前断开 —— 直接适配 v0.1 的 `static/app.js`。
4. **多模型卡片网格**：CSS Grid + 每卡状态（pending/streaming/complete/error）+ 每卡 token 数与耗时 + provider 颜色区分 —— 对应 v0.1 §22-23 UI 要求。
5. **模型下拉选择器**：checkbox 多选 + 点击外部关闭 —— 对应 v0.1 模型池选择。
6. **错误隔离**：`query_model` 对每个模型独立 try/except，单个模型失败返回 error 事件而不影响整轮 —— 符合 v0.1 §25。

### Conflicts with MAD-Hub

- **匿名投票 → 最高分即 winner**：`anonymous_model_vote` 每模型给其他模型打 1-10 分、聚合选 winner → 违反 P4/P7（投票定正确性 + 自动 winner）。
- **共识综合**：`synthesize_responses` 让一个模型产出 consensus/divergence/最佳综合答案 → 违反 P7/P8。
- **Chairman（主席终裁）**：`chairman_review` 明确写着 "You are the CHAIRMAN … final definitive answer" → 正是 P7 禁止的 Final Judge / Arbiter。
- **单轮无 Round 概念**：没有 peer review 式多轮讨论，与 MAD-Hub 核心（Round 2+ 交叉阅读）不匹配。
- **无服务端持久化**：刷新即丢，不符合 v0.1"刷新页面后仍可恢复 Session"。
- **单一第三方网关**：v0.1 要求各厂商平级、Model Manager + (未来 LiteLLM)，不依赖单一网关。

### License

- Project: niveshdandyan/llm-council
- License: **README 声明 MIT，但仓库内无 LICENSE 文件**（唯一 License 证据是 README 徽章与末尾 "## License MIT" 文字）。法律上无法确认授权，默认视为"保留所有权利"。
- Relevant restrictions: 未确认授权前不得假设可直接复制；README 提及 UI 设计系统 HappyCapy、AI Gateway 服务等第三方元素。
- Reuse recommendation: **Reference only**（可参考其 SSE 传输与前端交互模式，**不得直接复制源码**；如需复制其代码，必须先向作者确认授权）。

---

## 3. Local-LLM-Arena

### Architecture

- **模式**：FastAPI（asyncio）后端 + React 19（Vite + zustand）前端。核心是"一次 prompt → 最多 6 个本地模型实例并行回答 → 卡片并排流式展示 → 人工盲评 👍/👎 + 可选 LLM-as-judge 自动打分 → 私有 Elo 排行榜"。
- **生成只走本地 Ollama**：`AsyncClient(host=127.0.0.1:11434)`，模型实例可带各自超参数（temperature/top_p/top_k/repeat_penalty/num_predict/seed），同一模型可用不同参数作为多个实例。
- **Judge 可本地可云端**：`/api/judge` 支持 local（Ollama structured output）、anthropic、openai/openrouter（API key 只在内存，不落盘不写日志）；候选答案匿名为 A/B/C，顺序每次打乱防位置偏差，temperature=0 保证可复现。
- **单轮无辩论**：一次 turn = 一次并行问答；各实例只读自己的 history（`historyFor`），**不读取其他模型的答案**。
- **入口**：`uvicorn` 后端 + Vite 前端（生产模式由 FastAPI 直接托管 `frontend/dist`）。

### Relevant Modules

- `backend/app/routers/chat.py` — `/api/chat`（`asyncio.gather` 并行非流式）、`/api/chat/stream`（`asyncio.Queue` + `StreamingResponse` 逐 token NDJSON：`{"type":"token","instance_id":…,"token":…}` / metrics / done / error / sentinel）；client 断连时 cancel 全部 generation。
- `backend/app/services/ollama.py` — Ollama 唯一入口：`chat_stream`、`chat_json`（JSON Schema structured output，temperature 0 + seed 42 固定）、`list_models`、`pull/delete`、`_as_messages`（history 截断 `history_limit=40`）。
- `backend/app/services/cloud.py` — Anthropic / OpenAI-compatible judge 调用。
- `backend/app/routers/judge.py` — 匿名 judge：内联 JSON Schema（无 $ref，小模型更易遵守）、`_coerce` 容错修复弱模型坏 JSON、prompt 注入防护（分隔栏 + "untrusted DATA" 指令）、`winner` 回退到最高分。
- `backend/app/security.py` / `config.py` — bearer auth、same-origin、CSP 安全头；`ARENA_*` env 配置。
- `frontend/src/store/arena.ts` — zustand + `persist`：Session（instances + turns[]，每 turn 的 responses 以 instanceId 为 key）、盲评 `BlindState`、AbortController 取消、`persistedState` 刻意排除 API key。
- `frontend/src/lib/sse.ts` — NDJSON 流读取（`readNdjson`，行缓冲）。
- `frontend/src/components/arena/*` — `ComparisonView`（`sm:grid-cols-2 xl:grid-cols-3` 网格）、`ArenaCard`（名称/超参数/指标/FAST 皇冠/流式光标/👍👎）、`JudgeBar`、`ModelManagerDialog`（pull/delete Ollama 模型）、`LeaderboardDialog`（Elo + bootstrap 95% CI）、`BenchmarkDialog`。
- `frontend/src/lib/elo.ts` — 成对 Elo 计算 + 置信区间（judge 分数与人工投票共同驱动）。

### Persistence

- **前端 localStorage（zustand persist）**：完整保存 sessions（含每 turn 每模型的全文响应、指标、投票、盲评状态），key `arena_state_v1`；rehydrate 时清理残留的 streaming 标志并修正 currentId；API key 不持久化。
- **导出**：`exportJSON` 导出整次比较为 JSON（盲评未 reveal 时用假名）。
- **服务端无持久化**（无数据库），Ollama 模型本身在本地。

### MCP / Web

- **MCP**：否。
- **Web**：是（完整 SPA），但无 Web Search / Grounding —— 数据完全本地。
- 云端只用于可选的 judge（自带 key），生成从不离机。

### Reusable Ideas

适合 MAD-Hub 借鉴的部分：

1. **真正的逐 token 流式**：`asyncio.Queue` + `StreamingResponse(NDJSON)` 扇出多模型 token，客户端 `readNdjson` 行缓冲 → 逐卡刷新 —— 这是三个项目里唯一"token 级"流式实现，是 v0.1 Phase 1 SSE 的最佳蓝本（把 NDJSON 换成 SSE 格式即可）。
2. **并行扇出与取消**：`asyncio.gather` + 每实例 AbortController + 客户端断连时 `task.cancel()` —— 对应 v0.1 §25 单模型失败不拖垮整轮 + 用户可停止。
3. **Session/Turn 前端数据模型**：`instances`（模型池）+ `turns[]`（每轮每实例 responses）—— 与 MAD-Hub 的 Session/Round 结构同构，可在原生 JS 前端中对应实现。
4. **实例级超参数**：同一模型不同参数并行对比 —— v0.1 未禁止，Phase 4 "更细粒度模型选择"可参考。
5. **持久化安全细节**：API key 不落盘；rehydrate 清理 streaming 标志 —— 良好实践。
6. **盲评 UI 与指标**：盲评（Model A/B/C）+ reveal + 👍/👎、tokens/sec、TTFT —— 其中**指标展示**符合 v0.1 §19 token/cost/latency 统计；**盲评本身**是人工筛选的辅助手段，可作为 UI 可选项（v0.1 未禁止，但注意 MAD-Hub 的模型间交叉阅读是实名+匿名的混合场景，需自行判断）。
7. **Judge 的注入防护写法**（"untrusted DATA" + 分隔栏 + 内联 JSON Schema + `_coerce` 容错）—— 如果未来做 Evidence/争议检测等辅助功能，这套防注入模板可参考；但"选 winner"逻辑本身不可用。

### Conflicts with MAD-Hub

- **LLM-as-judge 自动 winner**：`/api/judge` 选最高分、定 winner → 违反 P7（自动 Final Judge / Automatic Winner）。
- **Elo 排行榜**：`elo.ts` + Leaderboard → 违反 §11（模型排行榜/模型信誉分明令禁止）。
- **投票定胜负**：👍/👎 聚合 + judge 分数驱动排行榜 → 违反 P4 精神（以票数/分数作为正确性依据）。
- **无 Round / 无交叉阅读**：实例间不互读答案，与 MAD-Hub 核心的 Round 2+ 交叉评价无关，只能提供 UI/流式参考。
- **依赖 Ollama**：v0.1 Phase 1 是 Mock、Phase 2 是 LiteLLM 多厂商，Ollama 只是可能的可选 provider 之一，不是架构依赖。

### License

- Project: sammy995/Local-LLM-Arena
- License: **MIT**（仓库内 `LICENSE` 文件，Copyright (c) 2026 Shubham Lagad），附第三方声明：Ollama 为独立产品，不随 MIT 授权。
- Relevant restrictions: 保留 MIT 版权声明；Ollama 许可条款另行适用。
- Reuse recommendation: **Reuse if license permits**（流式/前端代码可按 MIT 复用，但必须剔除 judge/winner/Elo 等与架构冲突逻辑）。

---

## 4. Comparison

| Capability | ensemble | llm-council | Local-LLM-Arena |
|---|---|---|---|
| Multi-model | 3 家直连（OpenAI/Anthropic/DeepSeek 注册表） | 9 模型经单一 AI Gateway | 本地 Ollama，最多 6 实例（可同模型多参数） |
| Parallel | asyncio 每模型 agent 循环 | ThreadPoolExecutor 并行 | asyncio.gather 扇出 |
| Streaming | 无（文件批处理，无 token 流） | 弱（SSE 逐模型完成事件，非 token） | 强（NDJSON 逐 token 流） |
| Round | 多轮 phase 状态机（propose→review→rebuttal→vote…） | 无（单轮 + vote/synthesize/chairman） | 无（单 turn，实例不互读） |
| Persistence | 文件系统全量落盘 + resume | 仅 localStorage 摘要历史 | localStorage 全量 Session（zustand persist） |
| MCP | 有（FastMCP stdio，1 个工具） | 无 | 无 |
| Web | 无（CLI + MCP） | 有（vanilla JS 仪表盘） | 有（React SPA） |
| UI | 终端面板 | CSS Grid 卡片 + 下拉多选 | React Grid 卡片 + 盲评/Judge/Elo/模型管理 |

其他要点：

- **Grounding**：仅 ensemble 有（Tavily 可选）；其余两者无 Web Search。
- **vote / consensus / final judge / winner / synthesis**：三个项目**全部存在**自动裁决类逻辑（ensemble=投票+背书+Borda+synthesis+confirm；llm-council=匿名投票选 winner+共识综合+chairman；Arena=LLM-as-judge 选 winner+Elo）。按 architecture_v0.1（P4/P7/P8、§11 禁止项），这些逻辑 **MAD-Hub 一律不采用**。

---

## 5. CrossLLM Recommendation

### Reuse if license permits

- **ensemble（MIT）**：文件系统持久化与原子写（`save_state`/`atomic_write_text` 的 tempfile + os.replace）、Round/Phase 状态建模（`RoundState`/`DebateState`、`merge_from` 累加合并）、Round 1 隔离与上下文截断的 prompt 构建思路、失败 sentinel 不阻塞整轮、用量/成本 sidecar。复制时需保留 MIT 版权声明。
- **Local-LLM-Arena（MIT，注意 Ollama 第三方声明）**：逐 token NDJSON 流式扇出（`asyncio.Queue` + `StreamingResponse`）、客户端 `readNdjson` 行缓冲解析、`asyncio.gather` + AbortController 取消、Session/Turn 前端数据模型、指标（TTFT/tokens-per-sec）采集、persist 时不落盘 API key、rehydrate 清理 streaming 标志。

### Adapt

- **llm-council（license 未确认 → 只可"参考后重写"）**：零依赖 SSE-over-POST 服务端模式（后台线程 + 队列转发 + 显式 done 标记 + keepalive）与原生 JS 卡片流式刷新，可作为 v0.1 `static/app.js` 的**交互模式蓝本**，但代码需自行重写（不能直接复制其源码）。
- **ensemble**：MCP server 形态（FastMCP stdio、单一工具暴露）可作为 v0.1 Phase 3 的 MCP Client 连接模式参考；grounding 的 "sources → [n] 引用" 注入方式可参考（v0.1 要求 Evidence Pack 由用户决定注入，而非自动注入）。
- **Local-LLM-Arena**：judge 的防注入模板（untrusted data 分隔栏、内联 JSON Schema、坏 JSON 容错）——去掉 winner 逻辑后可复用到未来的争议检测/Evidence 辅助。

### Reference only

- **llm-council** 的全部源码（license 未确认）。
- 三个项目的**自动裁决/投票/排名/共识逻辑**整体只作为"反面教材"参考：确认 MAD-Hub 为何不做（P4/P7/P8/§11），不采用任何实现。
- ensemble 的自动 drop、Borda、synthesis-as-candidate；Arena 的 Elo/排行榜/LLM-judge winner；llm-council 的 chairman —— 全部只读不搬。

### Do not use

- 所有 vote / consensus / final judge / winner selection / automatic synthesis / Elo / 模型排行榜 / 模型信誉分 逻辑（与 architecture_v0.1 P4-P8、§11 直接冲突）。
- 单一第三方网关依赖（llm-council 的 ai-gateway.happycapy.ai）与固定 Origin 头 —— v0.1 要求平级多厂商 + Model Manager 抽象。
- 自动剔除模型的机制（ensemble 的 provider drop；MAD-Hub 只允许用户 CLOSED）。
- 无多轮能力的单轮/单 turn 流程（llm-council / Arena 的流程模型不能作为 MAD-Hub 主流程）。

---

## 6. Phase 1 Recommendation

以 architecture_v0.1 §15/§31/§32 为准，Phase 1 只做 Mock 3 模型主链验证。据此组合：

1. **后端（core/storage.py 等）**：采用 ensemble 的**文件系统 + 原子写 + Round 目录（round_001/…）+ state.json 可恢复**模式；Round/Session 状态结构按其 `RoundState`/`DebateState` 思路用 v0.1 §13/§14 的数据结构落地；历史轮次只增不覆盖。
2. **并行与流式（app.py / round_manager.py）**：采用 Local-LLM-Arena 的 `asyncio.gather` 扇出 + `asyncio.Queue` 转发 + `StreamingResponse` 逐 token 流式；传输协议按 v0.1 用 **SSE**（Arena 的 NDJSON 事件结构改为 SSE 事件，并把 llm-council 的显式 `{"status":"done"}` 结束标记加入）；单模型失败独立收尾，不取消整轮（v0.1 §25）。
3. **前端（static/index.html|style.css|app.js）**：以 llm-council 的原生 JS + CSS Grid 卡片布局与下拉多选为蓝本（自行重写），卡片状态机沿用 pending/streaming/complete/error；吸收 Arena 的逐 token 行缓冲解析思路与"会话刷新后可恢复"的持久化要求（但 MAD-Hub 用后端文件恢复，而非仅 localStorage）。
4. **本轮明确不实现**（v0.1 §15 不实现清单）：真实 LLM API、LiteLLM、MCP、Web Search、Evidence、Judge/Consensus、以及三个参考项目中的任何投票/裁决逻辑。

完成标准核对：三个 repo 已浅克隆 ✓；LICENSE 已检查（ensemble MIT、Arena MIT+Ollama 声明、llm-council 仅 README 声明 MIT 无 LICENSE 文件）✓；必要源码分析完成 ✓；本报告已生成 ✓；reference 仓库未修改 ✓；未实现 MAD-Hub 业务代码 ✓；未引入大型 Agent Framework（无 LangGraph/AutoGen/CrewAI 安装）✓；未做无意义的大规模安装/测试 ✓。

**Phase 0 到此结束，不进入 Phase 1。**
