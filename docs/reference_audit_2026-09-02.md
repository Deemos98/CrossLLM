# Context Budget / Compaction — Reference Audit

**日期**：2026-09-02
**范围**：核查 CrossLLM Phase 4.1 / V2（`core/context_budget.py` + `core/compactor.py` + `round_manager._maybe_compact_history`）的机制出处
**约束**：不修改代码 / 不引入新依赖 / 不扩展 Phase 4
**方法**：逐项目拉取 GitHub `main` 分支原始源码比对（非凭记忆、非凭二手资料）

---

## 0. 结论摘要（先给答案）

> **CrossLLM 当前实现中，代码级直接采用成熟项目实现的部分极少。**
> 三个项目主要提供了**概念层面的方向确认**，而不是可直接照搬的代码。
>
> - **真正借鉴的思路**：Aider 的「超限才压缩 + 失败降级不阻断」、OpenAI SDK 的「压缩后替换历史」。
> - **刻意不采用的**：Aider 的 tail 保留一半、递归压缩、后台线程；OpenAI SDK 的 clear→add（与 phase4「原始历史不删除」冲突）；Mem0 的全部（属 V3/V4 范围）。
> - **自行设计的**：token 估算（`len//3`）、阈值 `0.8`、摘要 prompt 内容、`compact_<K>.md` 追加式持久化。

**因此 `docs/phase4_memory.md` 与本文档之前的 "参考 Aider" 表述应当理解为「参考了 Aider 的问题分解方式」，而不是「照搬了 Aider 代码」。**

---

# 1. Aider

## 1.1 实际查看的源码文件

| 文件 | URL |
|---|---|
| `aider/history.py` | https://raw.githubusercontent.com/Aider-AI/aider/main/aider/history.py |
| `aider/prompts.py` | https://raw.githubusercontent.com/Aider-AI/aider/main/aider/prompts.py |
| `aider/coders/base_coder.py` | https://raw.githubusercontent.com/Aider-AI/aider/main/aider/coders/base_coder.py |

## 1.2 具体机制（代码级）

### (a) Token 估算 — 用**真实 tokenizer**

```python
class ChatSummary:
    def __init__(self, models=None, max_tokens=1024):
        self.token_count = self.models[0].token_count   # ← 模型自带 tokenizer

    def too_big(self, messages):
        sized = self.tokenize(messages)
        total = sum(tokens for tokens, _msg in sized)
        return total > self.max_tokens
```

### (b) 触发：`total > max_tokens`（**无 ratio**）

`base_coder.py` 里每轮结束后检查：

```python
def move_back_cur_messages(self, message):
    self.done_messages += self.cur_messages
    self.summarize_start()          # ← 每轮检查

def summarize_start(self):
    if not self.summarizer.too_big(self.done_messages):
        return                       # 不超限 → 不压缩
    self.summarize_end()
    self.summarizer_thread = threading.Thread(target=self.summarize_worker)
    self.summarizer_thread.start()   # ← 后台线程压缩
```

实例化（`base_coder.py` `Coder.__init__`）：

```python
self.summarizer = summarizer or ChatSummary(
    [self.main_model.weak_model, self.main_model],   # ← 弱模型优先，主模型兜底
    self.main_model.max_chat_history_tokens,          # ← 阈值来自模型属性
)
```

### (c) **保留最近一半**（Aider 最关键的设计）

```python
def summarize_real(self, messages, depth=0):
    sized = self.tokenize(messages)
    total = sum(tokens for tokens, _msg in sized)
    if total <= self.max_tokens and depth == 0:
        return messages                       # 不超限直接返回

    min_split = 4
    if len(messages) <= min_split or depth > 3:
        return self.summarize_all(messages)   # 太短/递归太深 → 全量压缩

    tail_tokens = 0
    split_index = len(messages)
    half_max_tokens = self.max_tokens // 2    # ← 一半预算留给最近消息

    for i in range(len(sized) - 1, -1, -1):   # ← 从尾部倒序累加
        tokens, _msg = sized[i]
        if tail_tokens + tokens < half_max_tokens:
            tail_tokens += tokens
            split_index = i
        else:
            break

    while messages[split_index - 1]["role"] != "assistant" and split_index > 1:
        split_index -= 1                       # 保证 head 以 assistant 结尾

    if split_index <= min_split:
        return self.summarize_all(messages)

    tail = messages[split_index:]              # ← tail 原文保留
    ...
    summary = self.summarize_all(keep)         # head 压缩
    summary_tokens = self.token_count(summary)
    tail_tokens = sum(tokens for tokens, _ in sized[split_index:])
    if summary_tokens + tail_tokens < self.max_tokens:
        return summary + tail                  # ← 摘要 + 原文 tail
    return self.summarize_real(summary + tail, depth + 1)   # ← 递归，depth ≤ 3
```

### (d) 安全缓冲

```python
model_max_input_tokens = self.models[0].info.get("max_input_tokens") or 4096
model_max_input_tokens -= 512  # reserve buffer for safety
```

### (e) 多模型 fallback + 失败降级

```python
def summarize_all(self, messages):
    content = ""
    for msg in messages:
        role = msg["role"].upper()
        if role not in ("USER", "ASSISTANT"):
            continue
        content += f"# {role}\n" + msg["content"] + "\n"

    summarize_messages = [
        dict(role="system", content=prompts.summarize),
        dict(role="user", content=content),
    ]
    for model in self.models:                  # ← 逐个模型尝试
        try:
            summary = model.simple_send_with_retries(summarize_messages)
            if summary is not None:
                summary = prompts.summary_prefix + summary
                return [dict(role="user", content=summary)]
        except Exception as e:
            print(f"Summarization failed for model {model.name}: {str(e)}")
    raise ValueError("summarizer unexpectedly failed for all models")
```

调用方降级（`base_coder.py`）：

```python
try:
    done_messages = from_coder.summarizer.summarize_all(done_messages)
except ValueError:
    io.tool_warning("Chat history summarization failed, continuing with full history")
```

### (f) 乐观并发检查

```python
def summarize_end(self):
    if self.summarizer_thread is None:
        return
    self.summarizer_thread.join()
    self.summarizer_thread = None
    if self.summarizing_messages == self.done_messages:    # ← 期间历史是否被改动
        self.done_messages = self.summarized_done_messages
```

### (g) 摘要 prompt（`aider/prompts.py`）

```python
summarize = """*Briefly* summarize this partial conversation about programming.
Include less detail about older parts and more detail about the most recent messages.
Start a new paragraph every time the topic changes!
This is only part of a longer conversation so *DO NOT* conclude the summary with language like "Finally, ...".
The summary *MUST* include the function names, libraries, packages that are being discussed.
The summary *MUST* include the filenames that are being referenced by the assistant inside the ```...``` fenced code blocks!
The summaries *MUST NOT* include ```...``` fenced code blocks!
Phrase the summary with the USER in first person, telling the ASSISTANT about the conversation.
Write *as* the user.
The user should refer to the assistant as *you*.
Start the summary with "I asked you...".
"""

summary_prefix = "I spoke to you previously about a number of things.\n"
```

## 1.3 CrossLLM 对应实现

| Aider 机制 | CrossLLM 实现 | 采用? | 为什么 |
|---|---|---|---|
| 真实 tokenizer (`model.token_count`) | `len(text)//3`（`context_budget._estimate_tokens`，继承 phase1_1 的 `CHARS_PER_TOKEN=3`） | ❌ | CrossLLM 显式要求「不引入复杂 tokenizer」（phase1_1 §6.3 / phase4 §29）。且多 provider 抽象下取不到统一 tokenizer |
| `too_big`: `total > max_tokens` | `should_compact`: `>= ctx_limit * 0.8` | ⚠️ 思路 | 阈值判断结构相同；**ratio 是 CrossLLM 自加**（Aider 无 ratio 概念） |
| `half_max_tokens = max_tokens//2` + 倒序 tail | **无**。全量 history 压成单条 summary | ❌ | **最重要的未采用项**。见下方 §1.4 |
| `reserve buffer -= 512` | ratio 0.8 隐含留 20% | ⚠️ 思路 | 都为了"防止压完又超限"，但 Aider 是绝对值、CrossLLM 是比例 |
| 递归 `depth ≤ 3` | **无**（单次） | ❌ | CrossLLM 无 `min_split` 保护、无递归。见 §1.4 |
| `[weak_model, main_model]` fallback | 单模型（`participants[0]`） | ❌ | CrossLLM 是**多模型平级讨论**，用某个模型给另一个做摘要会引入偏见且破坏匿名化不变量 |
| `except ValueError → 继续全历史` | `except Exception → return None` 降级 | ✅ **采用** | 思路完全一致：压缩失败绝不阻断主流程。这是**本次唯一代码级对齐的点** |
| 后台线程 `threading.Thread` | 同步 `await` | ❌ | CrossLLM 已是 async 架构；且 `_maybe_compact_history` 在 `asyncio.gather` **之前**串行执行，无并发窗口可优化 |
| `summarize_end()` 乐观并发检查 | 无 | N/A | 串行执行，期间 history 不可能被改 |
| `prompts.summarize`（编程专用） | `SUMMARY_PROMPT_TEMPLATE`（5 条规则） | ❌ 自行设计 | 见 §1.4 |
| 内存替换 `done_messages` | `compact_<K>.md` **追加**落盘 | ❌ 相反 | 见 §1.4 |

## 1.4 关键差异的原因

1. **为什么不保留最近一半（tail）**
   Aider 是**单模型编码会话**：最近消息 = 用户刚下的指令和刚改的代码，**必须原文保留**才能继续编辑。
   CrossLLM 是**多轮多模型讨论**：一轮的内容是各模型的完整回答，且 `HISTORY_WINDOW=3` 已天然只注入最近 3 轮。压缩时把「最近 3 轮」整体压成 summary 是 V2 baseline 的刻意简化（phase4 §10 的 "R1~R7 压缩 + R8/R9/R10 原文"是**未来的优化方向**，本次未做）。

2. **为什么不做递归**
   Aider 递归是因为它要保证压完一定 `< max_tokens`。CrossLLM 有 `_trim_to_budget` 作为**第二道兜底**——即使 compaction 后仍超限，phase1_1 §5 的裁剪逻辑仍会丢弃最旧轮。所以递归的必要性被下游兜底吸收了。

3. **为什么摘要 prompt 不同**
   Aider 的 prompt 是**编程会话专用**：要求保留函数名、文件名、禁止代码块、用第一人称重写。这些对 CrossLLM **全部不适用**——CrossLLM 讨论的是方案/实验/决策，且有一条 Aider 完全没有的硬约束：**匿名化**（不能出现"模型认为"等指代身份的措辞，见不变量 A1/A2）。所以 CrossLLM 的 5 条规则是自行设计。

4. **为什么用文件持久化而不用内存**
   phase4_memory.md §20 明确「原始历史永远保留」「Memory 错误可重新生成」。Aider 内存替换后原文即丢失；CrossLLM 追加 `compact_<K>.md` 且**从不删除 round 文件**，是为了满足 §20 的可回溯要求。

---

# 2. OpenAI Agents SDK

## 2.1 实际查看的源码文件

| 文件 | URL |
|---|---|
| `src/agents/memory/openai_responses_compaction_session.py` | https://raw.githubusercontent.com/openai/openai-agents-python/main/src/agents/memory/openai_responses_compaction_session.py |

## 2.2 具体机制（代码级）

### (a) 触发阈值 — **按项数，不按 token**

```python
DEFAULT_COMPACTION_THRESHOLD = 10
_ALL_SESSION_ITEMS_LIMIT = 2_147_483_647

def default_should_trigger_compaction(context: dict[str, Any]) -> bool:
    """Default decision: compact when >= 10 candidate items exist."""
    return len(context["compaction_candidate_items"]) >= DEFAULT_COMPACTION_THRESHOLD
```

### (b) 候选筛选 — 排除 user 消息

```python
def select_compaction_candidate_items(items):
    """Excludes user messages and compaction items."""
    def _is_user_message(item):
        if item.get("type") == "message":
            return item.get("role") == "user"
        return item.get("role") == "user" and "content" in item
    return [
        item for item in items
        if not (_is_user_message(item) or (isinstance(item, dict) and item.get("type") == "compaction"))
    ]
```

### (c) 可注入触发钩子

```python
should_trigger_compaction: Callable[[dict[str, Any]], bool] | None = None
...
self.should_trigger_compaction = (
    should_trigger_compaction if should_trigger_compaction is not None
    else default_should_trigger_compaction
)
...
force = args.get("force", False) if args else False
should_compact = force or self.should_trigger_compaction({...})
```

### (d) 压缩实现 — **服务端 API，不是 prompt**

```python
compacted = await self.client.responses.compact(**compact_kwargs)
# compact_kwargs = {"model": self.model} + ("previous_response_id" | "input")
output_items = _strip_orphaned_assistant_ids(
    _normalize_compaction_output_items(compacted.output or [])
)
```

`model: str = "gpt-4.1"` — **专用压缩模型**（可不同于会话模型）。

### (e) 替换历史 — **clear → add 事务 + 失败恢复**

```python
async def _replace_underlying_session_items(self, *, output_items, previous_items):
    cleared = False
    try:
        await self.underlying_session.clear_session()      # ← 先清空
        cleared = True
        if output_items:
            await self.underlying_session.add_items(output_items)
    except Exception as error:
        await self._recover_from_failed_replacement(previous_items=previous_items, error=error, cleared=cleared)
        raise
    except asyncio.CancelledError as error:
        await self._recover_from_failed_replacement(previous_items=previous_items, error=error, cleared=cleared)
        raise
```

### (f) 并发保护 — `asyncio.Lock` + `shield` 循环 await

```python
self._mutation_lock = asyncio.Lock()
...
async with self._mutation_lock:
    previous_items = await self._get_all_underlying_session_items()
    await self._replace_underlying_session_items(output_items=output_items, previous_items=previous_items)
    self._compaction_candidate_items = select_compaction_candidate_items(output_items)
    self._session_items = output_items
```

```python
async def _await_restore_despite_cancellation(self, restore):
    restore_task = asyncio.ensure_future(restore)
    try:
        await asyncio.shield(restore_task)
    except asyncio.CancelledError:
        while not restore_task.done():        # ← 反复取消也要等恢复完成
            try:
                await asyncio.shield(restore_task)
            except asyncio.CancelledError:
                continue
        _ = restore_task.exception() if not restore_task.cancelled() else None
        raise
```

### (g) Session 抽象（从用法推断）

底层 `Session` 需提供 4 个异步方法：
- `async def get_items(limit) -> list[TResponseInputItem]`
- `async def add_items(items) -> None`
- `async def pop_item() -> TResponseInputItem | None`
- `async def clear_session() -> None`

## 2.3 CrossLLM 对应实现

| SDK 机制 | CrossLLM 实现 | 采用? | 为什么 |
|---|---|---|---|
| `Session` ABC（4 方法接口） | 无此抽象；用 `Storage` + `SessionState` 直连 | ❌ | CrossLLM 是**一次性轮次编排**，不是长驻会话对象；且引入 ABC 会与现有 `storage.py` 大量重复 |
| `DEFAULT_COMPACTION_THRESHOLD=10`（项数） | `>= ctx_limit * 0.8`（token） | ❌ | **维度不同**。CrossLLM 单轮可能产生 12KB MCP tool result，项数少但 token 巨大——项数阈值对 CrossLLM 无效（phase4 §9 明确「即使只有一轮，也可能因巨大 Tool Result 触发」） |
| `select_compaction_candidate_items`（排除 user msg） | 无 | ❌ | CrossLLM 压缩的是**已落盘的历史回答**（`_load_history` 已过滤 status=complete），不含 user 轮次指令 |
| `should_trigger_compaction` 可注入钩子 | 固定 `should_compact(...)` | ❌ | V2 刻意不做可配置（phase4 §29「先跑起来」）。`ratio` 参数已预留扩展点 |
| 服务端 `responses.compact` API | **prompt + LLM** | ❌ | **OpenRouter 无 compact API**。CrossLLM 必须自己发 prompt（这是 provider 约束，不是设计选择） |
| 专用压缩模型 `"gpt-4.1"` | 同轮 `participants[0]` | ❌ | 见 §1.4 第 3 点：多模型平级 + 匿名化约束 |
| clear → add 替换 | **只追加** `compact_<K>.md`，**从不 clear** | ❌ **明确不采用** | 与 phase4 §20「原始历史永远保留」**直接冲突**。SDK 允许删旧历史，CrossLLM 不允许 |
| `asyncio.Lock` + `shield` 恢复 | 无（串行执行） | N/A | `_maybe_compact_history` 在 `asyncio.gather` 之前调用，**不存在并发窗口**。SDK 需要锁是因为它是可并发访问的 Session 对象 |
| 失败恢复 previous_items | 返回 `None` 降级 | ✅ 思路 | 目标一致（失败不丢历史），但 CrossLLM 靠"从不 clear"根本不会发生"需要恢复"的情况——**这是更简单也更安全的路径** |

## 2.4 关键差异的原因

1. **项数 vs token**：SDK 面向「对话轮次」，CrossLLM 面向「可能含巨大 tool result 的轮次」。phase4 §9 已论证必须按 token 判。
2. **clear vs 追加**：这是**价值观差异**而非技术差异。SDK 优化目标是最省 token；CrossLLM 优化目标是可回溯（§20「原始历史永远保留 / Memory 是原始历史的派生数据」）。
3. **服务端 API vs prompt**：provider 能力约束，非设计选择。

---

# 3. Mem0

## 3.1 实际查看的源码文件

| 文件 | URL |
|---|---|
| `mem0/memory/main.py` | https://raw.githubusercontent.com/mem0ai/mem0/main/mem0/memory/main.py |

## 3.2 具体机制（代码级）

### (a) `add()` — V3 分阶段事实抽取流水线

```python
# Phase 0: 上下文收集
last_messages = self.db.get_last_messages(session_scope, limit=10)
parsed_messages = parse_messages(messages)

# Phase 1: 已有记忆召回（向量库相似性搜索）
query_embedding = self.embedding_model.embed(parsed_messages, "search")
existing_results = self.vector_store.search(
    query=parsed_messages, vectors=query_embedding, top_k=10, filters=search_filters,
)
# UUID → 整数映射（防 LLM 幻觉引用 UUID）
uuid_mapping = {}
for idx, mem in enumerate(existing_results):
    uuid_mapping[str(idx)] = mem.id
    existing_memories.append({"id": str(idx), "text": mem.payload.get("data", "")})

# Phase 2: 单次 LLM 抽取，强制 JSON
system_prompt = ADDITIVE_EXTRACTION_PROMPT
user_prompt = generate_additive_extraction_prompt(
    existing_memories=existing_memories,
    new_messages=parsed_messages,
    last_k_messages=last_messages,
    custom_instructions=custom_instr,
)
response = self.llm.generate_response(
    messages=[{"role": "system", "content": system_prompt},
              {"role": "user", "content": user_prompt}],
    response_format={"type": "json_object"},
)
extracted_memories = json.loads(response, strict=False).get("memory", [])
```

### (b) 去重 — **MD5 hash**

```python
# Phase 4/5: Hash dedup
existing_hashes = set()
for mem in existing_results:
    h = mem.payload.get("hash")
    if h:
        existing_hashes.add(h)

seen_hashes = set()
for mem in extracted_memories:
    text = mem.get("text")
    mem_hash = hashlib.md5(text.encode()).hexdigest()
    if mem_hash in existing_hashes or mem_hash in seen_hashes:
        logger.debug(f"Skipping duplicate memory (hash match): {text[:50]}")
        continue                                  # ← 命中则跳过，不更新
    seen_hashes.add(mem_hash)
    ...
```

**重要**：`add()` 路径**只产生 ADD 事件**，不产生 UPDATE/DELETE（去重命中是 skip 而非 update）。UPDATE/DELETE 来自独立的 `update()` / `delete()` API。

### (c) 持久化 — 向量库 + SQLite 历史

```python
# Phase 6
self.vector_store.insert(vectors=all_vectors, ids=all_ids, payloads=all_payloads)
self.db.batch_add_history(history_records)     # event="ADD", old_memory=None
```

payload 字段：`data` / `hash` / `created_at` / `updated_at` / `text_lemmatized` / `user_id` / `agent_id` / `run_id` / `actor_id` / `role` / `attributed_to`。

### (d) `search()` — 混合检索

```python
def _search_vector_store(self, query, filters, limit, threshold=0.1, ...):
    query_lemmatized = lemmatize_for_bm25(query)
    query_entities = extract_entities(query)

    embeddings = self.embedding_model.embed(query, "search")

    # 语义检索（over-fetch）
    internal_limit = max(limit * 4, 60)
    semantic_results = self.vector_store.search(
        query=query, vectors=embeddings, top_k=internal_limit, filters=filters
    )

    # BM25 关键词检索
    keyword_results = self.vector_store.keyword_search(
        query=query_lemmatized, top_k=internal_limit, filters=filters
    )
    midpoint, steepness = get_bm25_params(query, lemmatized=query_lemmatized)
    for mem in keyword_results:
        bm25_scores[mem_id] = normalize_bm25(mem.score, midpoint, steepness)

    # 实体 boost
    entity_boosts = self._compute_entity_boosts(query_entities, filters)

    # 融合排序
    scored_results = score_and_rank(
        semantic_results=candidates, bm25_scores=bm25_scores,
        entity_boosts=entity_boosts, threshold=threshold, top_k=limit,
    )
```

### (e) 作用域隔离

`filters` **必须**含 `user_id` / `agent_id` / `run_id` 之一；`_strip_identity_keys` 剔除 metadata 里的身份键防止越权。

## 3.3 CrossLLM 对应实现

| Mem0 机制 | CrossLLM 实现 | 采用? | 为什么 |
|---|---|---|---|
| `add()` 事实抽取（LLM + JSON） | **无**。compaction 是**整段文本摘要**，不是事实抽取 | ❌ | **职责不同层**。Mem0 是「跨 Session 长期记忆」= phase4 **V3** 范围；CrossLLM V2 是「本 Session 上下文压缩」。本次范围明确不含 Project Memory |
| MD5 hash 去重 | 无 | ❌ | 同上，属 V3 |
| `vector_store.insert` | 无 | ❌ | phase4 §29 明确「不引入复杂 Vector Database 作为第一版前提」；用户本次亦明确「不引入 RAG / 向量数据库 / BM25」 |
| `search()` 混合检索（embedding + BM25 + entity boost + score_and_rank） | 无 | ❌ | 属 phase4 **V4**（Memory Retrieval）。§25 明确「第一版用关键词 + 简单文本匹配，之后再考虑 BM25」 |
| SQLite history（ADD/UPDATE/DELETE） | 无 | ❌ | 属 V3 |
| 作用域隔离（user_id/agent_id/run_id filters） | `session_id` 目录隔离 + `compact_<K>.md` | ⚠️ 思路 | 都做 scope 隔离，但 CrossLLM 是「一 session 一目录」的**文件系统隔离**，无多租户需求 |

## 3.4 关键差异的原因

**Mem0 与 CrossLLM V2 根本不在同一层**：

```
Mem0         → Long-term Memory（跨 Session，需要抽取/去重/检索）
CrossLLM V2  → Context Compaction（本 Session，只需要缩短当前 prompt）
```

phase4_memory.md §26 已明确区分：

> Compaction ≠ Project Memory
> - Compaction：让**当前对话**继续进行
> - Project Memory：让**未来完全不同的问题**仍能利用过去经验

用户本次验收范围也明确「不实现 Project Memory」。因此 Mem0 的机制**全部落在本次范围之外**，不是"没采用"，而是"还轮不到"。

---

# 4. 用户特别指定的 4 项逐一定性

## 4.1 `context_budget.py` 的 token 估算（`len(text) // 3`）

**结论：自行设计，非来自成熟实现。**

| 项目 | 估算方式 |
|---|---|
| Aider | `self.models[0].token_count(msg)` — **真实 tokenizer** |
| OpenAI Agents SDK | **完全不估算**（按项数 `>= 10`） |
| Mem0 | `self.embedding_model.embed(...)` — embedding 模型 |
| CrossLLM | `-(-len(text) // 3)` — 字符数 ÷ 3 |

**出处追溯**：`CHARS_PER_TOKEN = 3` 来自 CrossLLM **phase1_1 §6.3** 的既有约定（"粗略估算用，不引 tokenizer，刻意高估"），`context_budget.py` 只是为了**避免循环导入**内联了一份副本（见该文件 21-23 行注释）。

**为何不采用 Aider 的真实 tokenizer**：phase1_1 §6.3 / phase4 §29 明确「不引入复杂 tokenizer」。且 CrossLLM 走 OpenRouter 多 provider，无法统一取 tokenizer。

## 4.2 `should_compact` 的阈值 `0.8`

**结论：自行设计，三个项目均无对应 ratio。**

| 项目 | 阈值语义 |
|---|---|
| Aider | `total > max_tokens`（绝对值，**无 ratio**） |
| OpenAI SDK | `len(candidates) >= 10`（项数，**无 ratio**） |
| Mem0 | `threshold=0.1`（**语义完全不同**——相似度下限，不是预算比例） |

**0.8 的实际来源是 phase4_memory.md §9 的原则推导**：

> 核心原则：`actual_input + actual_output <= context_limit`
> Compaction 的触发依据应该是**实际请求 Context**

即：估算只覆盖 input 侧，必须给 output + tool definitions + tool results 留余量，因此取 80% 作为保守值。**这是从 phase4 文档原则推导出的工程取值，不是抄来的。**

## 4.3 `compactor.py` 的摘要策略

**结论：结构借鉴 Aider，内容自行设计。**

**借鉴的结构**（与 Aider `summarize_all` 一致）：

```
[system: 摘要指令] + [user: 待压缩内容] → 单次 LLM 调用 → 返回文本
```

Aider:
```python
summarize_messages = [
    dict(role="system", content=prompts.summarize),
    dict(role="user", content=content),
]
summary = model.simple_send_with_retries(summarize_messages)
```

CrossLLM:
```python
SUMMARY_PROMPT_TEMPLATE = """请把下面的多轮对话历史压缩为一份 Markdown 摘要，遵循以下规则：
1. 仅保留事实、决策、实验结果、已被证伪的方案、未解决问题；
2. 保留每个观点 / 结论对应的轮次编号（"第 N 轮 ..."）；
3. 总长度控制在 {max_chars} 字以内；
4. 不要加入对话中未出现的新事实；
5. 不要使用"模型认为"等指代身份的措辞，统一用"对话参与者"。
---
历史：{history}"""
```

**内容自行设计的原因**：

| 约束 | Aider | CrossLLM |
|---|---|---|
| 领域 | 编程会话 | 方案/决策/实验讨论 |
| 必留信息 | 函数名、库名、文件名 | 事实、决策、实验结果、被证伪方案、未解决问题 |
| 禁止 | ``` 代码块 | 「模型认为」等身份措辞（**匿名化不变量 A1/A2**） |
| 人称 | USER 第一人称重写 | 统一「对话参与者」 |
| 长度控制 | 无显式限制 | `{max_chars} = 1200` |

其中**匿名化约束是 CrossLLM 独有**——Aider 单模型会话不存在此问题。

## 4.4 `compact_<K>.md` 持久化方式

**结论：自行设计，且比三个参考项目都更保守。**

| 项目 | 持久化 | 是否保留原文 |
|---|---|---|
| Aider | **内存**（`done_messages = summarized_done_messages`） | ❌ 原文丢失 |
| OpenAI SDK | 替换 underlying session（**clear → add**） | ❌ 旧历史被清空（失败时尝试恢复） |
| Mem0 | 向量库 + SQLite history | ✅ 但存的是**抽取后的事实**，非原始对话 |
| **CrossLLM** | `sessions/<sid>/compact_<K>.md` **只追加** | ✅ **round 文件永不删除** |

**为何自行设计**：三个参考项目**没有一个**满足 phase4 §20 的硬约束「原始历史永远保留」。因此 CrossLLM 采用了比三者都保守的方案——**压缩产物独立落盘、原文零修改**。

`_next_compact_index` 的序号自增（`compact_001.md` → `compact_002.md`…）也是为满足 §20「可重新生成 Memory」而自行设计，三个项目均无对应机制（Aider 内存、SDK 覆盖、Mem0 向量库）。

---

# 5. 总表：采用 / 未采用 / 为什么

## 5.1 直接采用（思路或结构层面对齐）

| # | 机制 | 来源 | CrossLLM 位置 |
|---|---|---|---|
| 1 | 超限才压缩（不超限零开销） | Aider `too_big` / `summarize_start` | `_maybe_compact_history` L344 `if not should_compact(...): return None` |
| 2 | 压缩失败 → 降级不阻断主流程 | Aider `except ValueError → 继续全历史`；SDK `_recover_from_failed_replacement` | `compactor.compact_history` L111 `except Exception: return None` |
| 3 | `[system 指令] + [user 内容]` 单次 LLM 调用做摘要 | Aider `summarize_all` | `SUMMARY_PROMPT_TEMPLATE` + `provider.generate()` |
| 4 | 给 output/其他部分预留安全余量 | Aider `model_max_input_tokens -= 512` | `DEFAULT_COMPACTION_RATIO = 0.8` |
| 5 | Scope 隔离 | Mem0 `filters(user_id/agent_id/run_id)` | `sessions/<sid>/` 目录隔离 |

## 5.2 刻意未采用（业务不同）

| # | 机制 | 来源 | 未采用原因 |
|---|---|---|---|
| 1 | **保留最近一半原文（tail）** | Aider `half_max_tokens = max_tokens//2` | V2 baseline 刻意简化；`HISTORY_WINDOW=3` 已限制范围。是 **V2.1 首要优化候选** |
| 2 | 递归压缩 depth ≤ 3 | Aider `summarize_real(..., depth+1)` | 下游有 `_trim_to_budget` 作为第二道兜底 |
| 3 | 后台线程压缩 | Aider `threading.Thread` | 已 async 且在 `gather` 前串行，无并发窗口 |
| 4 | 弱模型优先 + 主模型兜底 | Aider `[weak_model, main_model]` | 多模型平级讨论，用某模型给他人做摘要破坏匿名化不变量 |
| 5 | 专用压缩模型 `"gpt-4.1"` | OpenAI SDK | 同上 |
| 6 | 按项数触发（>= 10） | OpenAI SDK `DEFAULT_COMPACTION_THRESHOLD` | 单轮可能 12KB tool result，项数少但 token 巨大（phase4 §9） |
| 7 | `clear → add` 替换历史 | OpenAI SDK | 与 phase4 §20「原始历史永远保留」**直接冲突** |
| 8 | `asyncio.Lock` + `shield` 恢复 | OpenAI SDK | 串行执行无并发窗口；且"从不 clear"使"需恢复"场景不存在 |
| 9 | Session ABC（get/add/pop/clear） | OpenAI SDK | 一次性轮次编排，非长驻会话对象 |
| 10 | 可注入触发钩子 | OpenAI SDK `should_trigger_compaction` | V2 刻意不做可配置（`ratio` 参数已预留扩展点） |
| 11 | 事实抽取 + MD5 去重 + 向量库 | Mem0 `add()` | 属 phase4 **V3**（Project Memory），本次范围外 |
| 12 | 混合检索（embedding + BM25 + entity boost） | Mem0 `search()` | 属 phase4 **V4**（Memory Retrieval）；§29 明确第一版不做 |
| 13 | 服务端 compact API | OpenAI SDK `responses.compact` | **OpenRouter 无此 API**（provider 能力约束，非设计选择） |

## 5.3 自行设计（无成熟实现出处）

| # | 项 | 说明 |
|---|---|---|
| 1 | `len(text)//3` token 估算 | 继承 phase1_1 §6.3 既有约定 |
| 2 | `DEFAULT_COMPACTION_RATIO = 0.8` | 从 phase4 §9 原则推导的工程取值 |
| 3 | 摘要 prompt 的 5 条规则 | 含 CrossLLM 独有的**匿名化约束** |
| 4 | `compact_<K>.md` 序号自增追加 | 为 §20 可回溯要求设计，三个项目均无 |
| 5 | `PromptTokenBreakdown` 分块明细 | 为测试断言设计 |
| 6 | 「取最严格模型的 context_limit」 | `min(budgets)` — 多模型平级场景独有，三个项目都是单模型 |

---

# 6. 发现的问题（供后续决策，本次不改）

1. **`docs/phase4_memory.md` §30 写「参考 Aider」表述过强**——实际只有思路层面借鉴，无代码级采用。建议在 V2.1 时把该表述修正为「参考了 Aider 的问题分解方式」。

2. **Aider 的 tail 保留是 V2 最明显的缺失**——Aider 用 `max_tokens//2` 保最近消息，CrossLLM 全量压成单条。这导致长会话下最近一轮的原文细节会在下一轮丢失。这是 **V2.1 的首要优化候选**。

3. **无递归/无 min_split 保护**——若单轮 history 极大（如 100KB tool result），一次压缩后仍可能超限，只能靠 `_trim_to_budget` 硬裁。

4. **压缩模型选择有偏见风险**——固定用 `participants[0]` 做摘要。虽然避免了多模型 fallback 的匿名化问题，但该模型的表述习惯会进入所有模型的上下文。

---

**审计完成。未修改任何代码，未引入任何新依赖。**
