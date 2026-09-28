"""Round 编排（architecture_v0.1.md §21、phase1_mock.md §13/§14/§20、phase2 §4/§14）。

职责边界：
- 决定本轮谁参与（本次发送选中的模型 ∩ ACTIVE）
- 分配 candidate_id（委派给 core/anonymizer）
- 组装 prompt（委派给 core/prompt_builder）
- asyncio.gather 并发扇出，单模型失败隔离
- 落盘 + SSE 事件广播

**不做** 投票 / 共识 / 综合 / 裁决（architecture_v0.1.md P4/P7/P8、§11）。
**不直接调用任何厂商 SDK**（phase2 §4/§23）—— 只通过 provider_factory 拿到
实现了 LLMProvider 接口的对象。Phase 2 从 Mock 换成真实 Provider 时，
本文件的编排逻辑不需要改变，这正是本阶段要验证的命题。

关于"本次发送选择"与 ACTIVE/CLOSED 的区别（phase1_1 §2）：
  - ACTIVE / CLOSED 是 **Session 生命周期**状态，由用户长期管理
  - 本次发送选择是**单次发送**发给谁，是 ACTIVE 的子集
  两者互不替代：CLOSED 不可被选中，但选中的 ACTIVE 模型不一定要全部发送。
"""

from __future__ import annotations

import asyncio
import json
import sys
import time
import traceback
from collections.abc import Callable
from datetime import datetime

from .anonymizer import assign_candidate_ids
# S3.3 Note（2026-09-03）：旧 ``DEFAULT_COMPACTION_RATIO`` / ``estimate_prompt_tokens`` /
# ``should_compact`` 已删除（§10.4 §11.1）。80% 触发阈值语义作废。
# S3.5 Note（2026-09-03）：HISTORY_WINDOW=3 硬裁已删除；_load_history / _trim_to_budget
# 已替换为 ContextSelector（context_selector.build）；prompt 路径消费
# HistoricalContextItem（user_question + answers 不可分，§31）。
from .mcp_runtime_log import RuntimeContext  # noqa: F401 - 供类型标注与测试引用
from . import mcp_runtime_log as runtime_log
from .model_manager import ModelRegistry, ProviderConfig
from .prompt_builder import (
    SYSTEM_INSTRUCTION,
    build_round1_prompt,
    build_round_n_prompt,
    build_round_n_prompt_v2,
    build_round_n_prompt_v3,
)
from .context_selector import (
    ContextSelector,
    HistoricalContextItem,
    ContextSelectionResult,
)
from .valid_round import ValidRoundPoolBuilder
# S3.7：Peer Context Preparation（§12-§17）—— 数据层白名单阻断 Peer Review Result
from .peer_context import PeerContextPreparator, PeerContextItem
from .summary_service import SummaryService
# S3.8：Session Compaction（§32-§40）—— 与 Conversation Summary 严格分目录分类型
from .compaction import (
    CompactedBlock,
    CompactionService,
    render_compacted_block,
)
# S3.9：MCP Evidence Index（§44-§48）—— 轻量导航索引，不替代 Runtime
from .evidence_index import EvidenceIndexService
from .pin_store import PinStore, PinState
from .state import RoundState, SessionState
from .storage import Storage
from .context_budget import _token  # S3.5：TypeSafe helper
from .model_capability import (
    CapabilityResolver,          # §一：能力来源协议（注入缝）
    FetchStatus,
    ProviderModelMetadata,
    TokenEstimate,
    UNKNOWN_TOKEN,
    _iso_now,
    apply_context_limit_fallback,
    fallback_only_metadata,
    get_default_resolver,
)

# 粗略估算用（phase1_1 §6.3）：不引 tokenizer，刻意高估，宁可多裁一点。
CHARS_PER_TOKEN = 3


def estimate_tokens(text: str) -> int:
    """粗略 token 估算，**仅用于避免明显超过 context 上限**。

    不作为计费依据，不作为模型质量指标，不引入复杂 tokenizer 依赖。
    """
    return -(-len(text) // CHARS_PER_TOKEN)  # ceil div


# S3.5（Plan §11.1 改名）：原 ``input_budget_for`` 与 §21 ``history_budget`` 同名不同义；
# 改为 ``context_limit_for``。返回的仍是 Provider 报告的 context_limit 原始值；
# ``history_budget`` 由 ContextBudget 单独给出（扣除 fixed_input_tokens + reserved_output_tokens）。
def context_limit_for(config) -> int:
    """输入侧硬上限 = 模型的 context_limit。

    context_limit 是模型总上下文窗口（input + output 合计上限），
    由 OpenRouter / config 提供，是原始 context window 数值，无任何折扣。

    不替代真正约束（Provider 请求前 actual_input + actual_output ≤ context_limit）。
    本函数仅用于兼容读取配置，不参与预算分配（§21）。"""
    return config.context_limit


# 为 phase2 旧测试 / e2e 脚本保留薄别名（与 S3.5 行为一致）
input_budget_for = context_limit_for


def _now() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


def _log(message: str) -> None:
    """极简日志（phase2 §20）。

    只记 session_id / round_id / model_id / 时间 / 错误摘要。
    **绝不**记录 API Key、Authorization Header、请求头（phase2 §8）。
    本项目是个人研究工具，不引入日志框架。
    """
    print(f"[crossllm] {message}", file=sys.stderr)


class RoundBus:
    """单个 Round 的事件总线。

    解决 POST /round 与 GET /events 之间的竞态：订阅者连上来时，先把已经产生的
    事件全部回放给它，再接收实时事件。因此任意时刻连上都不会漏 chunk。

    约 40 行，不引入 Redis / 数据库等额外设施（phase1_mock.md §22 不要提前优化）。
    """

    def __init__(self) -> None:
        self._events: list[dict] = []
        self._subscribers: list[asyncio.Queue] = []
        self.finished = False
        self._completed = asyncio.Event()

    @property
    def events(self) -> list[dict]:
        """已产生的全部事件（只读快照），供测试断言事件时序。"""
        return list(self._events)

    def publish(self, event: dict) -> None:
        self._events.append(event)
        for q in list(self._subscribers):
            q.put_nowait(event)

    def subscribe(self) -> asyncio.Queue:
        """返回一个已灌入历史事件的队列。整轮结束后队列里会有一个 None 哨兵。"""
        q: asyncio.Queue = asyncio.Queue()
        for event in self._events:
            q.put_nowait(event)
        if self.finished:
            q.put_nowait(None)
        else:
            self._subscribers.append(q)
        return q

    def finish(self) -> None:
        if self.finished:
            return
        self.finished = True
        for q in list(self._subscribers):
            q.put_nowait(None)
        self._subscribers.clear()
        self._completed.set()

    async def wait(self) -> None:
        await self._completed.wait()


class RoundManager:
    def __init__(
        self,
        storage: Storage,
        registry: ModelRegistry,
        provider_factory: Callable[[str], object],
        *,
        capability_resolver: CapabilityResolver | None = None,
    ):
        """capability_resolver：模型能力来源（§一）。默认走真实 Provider 元数据
        （``get_default_resolver()``，带 24h 磁盘缓存）；离线 / 测试可注入确定性实现。
        Mock 模型不查询（没有 Provider 元数据概念），只走 YAML fallback。"""
        self.storage = storage
        self.registry = registry
        self._provider_factory = provider_factory
        self._capability_resolver = capability_resolver
        self._buses: dict[tuple[str, int], RoundBus] = {}
        self._tasks: set[asyncio.Task] = set()
        # S3.7：SummaryService（S3.6，按 model_id 隔离）+ PeerContextPreparator
        self._summary_service = SummaryService(
            storage, provider_factory=provider_factory,
        )
        self._peer_preparator = PeerContextPreparator(
            storage, self._summary_service,
        )
        # S3.8：CompactionService（Session 级共享；落盘 compactions/，
        #       与 summaries/ 严格分离）
        self._compaction_service = CompactionService(
            storage, provider_factory=provider_factory,
        )
        # S3.9：EvidenceIndexService（落盘 evidence/mcp_evidence.jsonl；
        #       Runtime mcp_runtime.jsonl 保留不动，Index 不替代 Runtime）
        self._evidence_service = EvidenceIndexService(storage)

    # ---------- 事件总线 ----------

    def bus(self, session_id: str, round_id: int) -> RoundBus | None:
        return self._buses.get((session_id, round_id))

    async def wait_round(self, session_id: str, round_id: int) -> None:
        bus = self.bus(session_id, round_id)
        if bus is not None:
            await bus.wait()

    # ---------- Round 生命周期 ----------

    def prepare_round(
        self,
        session: SessionState,
        instruction: str,
        selected_models: list[str] | None = None,
        peer_review: bool = True,
        # ── S3.10 接线（MD §20 §55 §22）：Round 级政策参数 ──────────
        # k / request_max_output / summary_mode 由 API 传入，
        # 存进 RoundState 后被 _build_prompts_async 消费，
        # 确保 UI → API → RoundState → ContextBudget 使用同一个值。
        k: float = 0.5,
        request_max_output: int | None = None,
        summary_mode: bool = False,
    ) -> tuple[RoundState, RoundBus]:
        """同步地准备下一轮：分配轮次号、确定参与者、分配 candidate_id、落盘。

        拆成同步的 prepare + 异步的 execute，是为了让 POST /round 在返回之前
        事件总线就已经存在 —— 否则前端紧接着连 GET /events 会 404。

        selected_models：本次发送选中的模型（ACTIVE 的子集）。
            None 表示"全部 ACTIVE"（兼容 Phase 1 的调用方式）。
        peer_review：互评开关。只决定是否加入 peer context，
            不影响模型自己的普通聊天上下文（phase1_1 §3）。

            默认 **开启**：phase1_1 §8 把「Round 2 可以读取上一轮」列为
            保留的原核心验收，若默认关闭这条就不成立了。
        """
        round_id = session.current_round + 1

        if selected_models is None:
            participants = list(session.active_models)
        else:
            unknown = [m for m in selected_models if not self.registry.has(m)]
            if unknown:
                raise KeyError(f"未知 model_id: {unknown}")
            # phase1_1 §2.2：CLOSED 模型不得被本次发送选中
            not_active = [m for m in selected_models if m not in session.active_models]
            if not_active:
                raise ValueError(f"CLOSED 模型不能被选中: {not_active}")
            # 保序去重，保持用户选择顺序
            seen: set[str] = set()
            participants = [m for m in selected_models if not (m in seen or seen.add(m))]

        if not participants:
            raise ValueError("未选中任何模型，无法发送（phase1_1 §2.3）")

        # 互评只在「开启 + 至少两个模型」时生效。
        # 单模型即使开关打开也没有 peer context，等价于关闭（phase1_1 §3.3）。
        peer_review_effective = bool(peer_review) and len(participants) >= 2

        # Round 1 无历史可注入；互评关闭时不产生 Candidate（§3.3）
        need_candidates = round_id > 1 and peer_review_effective
        candidate_ids = assign_candidate_ids(participants) if need_candidates else {}

        # S3.10：k 夹紧到 MD §20 的 0.01–0.90 区间
        from .context_budget import K_MAX, K_MIN
        k_effective = max(K_MIN, min(K_MAX, float(k)))

        round_state = RoundState(
            round_id=round_id,
            user_instruction=instruction,
            participants=participants,
            candidate_ids=candidate_ids,
            peer_review=peer_review_effective,
            status={m: "pending" for m in participants},
            timestamp=_now(),
            # S3.10 接线：k / request_max_output / summary_mode
            k=k_effective,
            request_max_output=request_max_output,
            summary_mode=summary_mode,
        )
        session.rounds.append(round_state)
        session.current_round = round_id
        session.updated_at = _now()
        self.storage.save_state(session)

        # ── S3.10：context_policy 事件（MD §55）─────────────────────
        # 每次真正执行 Round 必须记录 k —— 日志中的 k 与本次实际使用的
        # k 必须是同一个值（禁止 UI 显示一个值、后端用另一个值）。
        self._log_policy(round_state, session.session_id)

        bus = RoundBus()
        self._buses[(session.session_id, round_id)] = bus
        return round_state, bus

    def launch(self, session_id: str, round_state: RoundState, bus: RoundBus) -> asyncio.Task:
        task = asyncio.create_task(self.execute_round(session_id, round_state, bus))
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)
        return task

    async def execute_round(self, session_id: str, round_state: RoundState, bus: RoundBus) -> None:
        """执行一轮：构建 prompt → 并发调用 → 落盘 → 广播事件。

        Phase 4.1 / V2：在 ``_build_prompts`` 之前评估 context budget，
        触发则先把历史压缩为 summary 注入 prompt，
        原始 round 文件 (sessions/<sid>/round_<NNN>/*.md) 永不删除。
        """
        session = self.storage.load_state(session_id)

        # ── S3.10（MD §55）：每次真正执行 Round 落 context_policy 事件 ──
        # 记录本轮政策参数（k / request_max_output / summary_mode /
        # peer_review），与 context_budget / history_selection 事件共同构成
        # 可回放的核心决策链。
        try:
            _policy_path = self.storage.session_dir(session_id) / "context_policy.jsonl"
            _policy_path.parent.mkdir(parents=True, exist_ok=True)
            with _policy_path.open("a", encoding="utf-8") as _fh:
                _fh.write(json.dumps({
                    "event": "context_policy",
                    "session_id": session_id,
                    "round_id": round_state.round_id,
                    "k": round_state.k,
                    "request_max_output": (
                        round_state.request_max_output
                    ),
                    "summary_mode": round_state.summary_mode,
                    "peer_review": round_state.peer_review,
                    "timestamp": _now(),
                }, ensure_ascii=False) + "\n")
        except Exception as _exc:  # noqa: BLE001 - 日志失败不影响 Round
            _log(f"session={session_id} round={round_state.round_id} "
                 f"context_policy_log_failed type={type(_exc).__name__}")

        # phase4 V2：上下文预算判断与 Summary 注入。
        # 不超限 → compact_summary = None，_build_prompts 完全走原路径；
        # 超限   → compact_summary = summary 文本，prompt 只携带 summary，
        #          原始 raw 仍位于 sessions/<sid>/round_*/ 中随时可回溯。
        compact_summary = await self._maybe_compact_history(session_id, session, round_state)

        prompts, warnings = await self._build_prompts_async(
            session,
            round_state,
            compact_summary=compact_summary,
        )

        for model_id, prompt in prompts.items():
            self.storage.save_prompt(session_id, round_state.round_id, model_id, prompt)

        # 匿名化软告警：只记录，不阻断（D2 结论、phase2 §28 问题 3）
        for model_id, items in warnings.items():
            for item in items:
                _log(f"session={session_id} round={round_state.round_id} model={model_id} {item}")

        # 并发扇出。单模型失败由 _run_one 内部隔离，不取消整轮
        # （architecture_v0.1.md §25、D6）
        await asyncio.gather(
            *[
                self._run_one(session_id, round_state, model_id, prompt, bus)
                for model_id, prompt in prompts.items()
            ],
            return_exceptions=True,
        )

        # 重新加载后写回本轮状态（避免被其它写操作覆盖）
        session = self.storage.load_state(session_id)
        target = session.round(round_state.round_id)
        if target is not None:
            target.status = dict(round_state.status)
            target.injected_rounds = list(round_state.injected_rounds)
            target.usage = {k: dict(v) for k, v in round_state.usage.items()}
            target.timestamp = _now()
        session.updated_at = _now()
        self.storage.save_state(session)

        # ── S3.9：Round 完成后生成 MCP Evidence Index（§48）──────────
        # 生成时机 = Round 完成后（不做"每次 tool call 额外调 LLM"）。
        # 任何失败只降级记日志，绝不影响 Round 结果。
        try:
            self._build_evidence_for_round(session_id, round_state)
        except Exception as exc:  # noqa: BLE001 - 降级不阻断
            _log(
                f"session={session_id} round={round_state.round_id} "
                f"evidence_index_failed type={type(exc).__name__}: {exc}"
            )
        bus.finish()

    async def _maybe_compact_history(
        self,
        session_id: str,
        session: SessionState,
        round_state: RoundState,
    ) -> str | None:
        """Phase 4.1 / V2 旧入口（80% 触发阈值）已废弃 —— S3.3 stub。

        变更（2026-09-03）：
          - 旧实现基于 ``should_compact(estimated, ctx_limit, ratio=0.8)`` 触发，
            与 phase4_memory.md §33 明令"k 不代表什么时候触发 Compaction"直接冲突
            （修订 #1 + Custom Rule C1）。
          - 删除原因见 PHASE4_REFACTOR_PLAN §11.1 + §19.1 + §19.2。
          - 真正的 Compaction 触发由 S3.5 ContextSelector 在
            "history 装不下 lossless_budget" 时驱动（§§ 11 / 38）。

        行为：S3.3 阶段一律返回 ``None``，原 prompt 路径继续走，
        保留 Compactor / 接线的代码形态以便 S3.5 复接。
        不删除本函数，避免 S3.11+ 重新引入触发点时类型/接口抖动。
        """
        _log(
            f"session={session_id} round={round_state.round_id} "
            f"compaction_trigger_skipped reason=s33_stub "
            f"old_threshold=80_percent_removed "
            f"replaced_by=s35_context_selector"
        )
        return None

    def _build_prompts(
        self,
        session: SessionState,
        round_state: RoundState,
        *,
        compact_summary: str | None = None,
    ) -> tuple[dict[str, str], dict[str, list[str]]]:
        """同步兼容入口（供无 event loop 的调用方使用，如单元测试）。

        S3.7 起真实工作移入 ``_build_prompts_async``（需 await PeerContextPreparator）。
        本包装在**无运行中 event loop** 时直接 asyncio.run 之；
        若已处于 event loop 中，必须由调用方 ``await self._build_prompts_async(...)``。
        """
        try:
            asyncio.get_running_loop()
        except RuntimeError:
            return asyncio.run(
                self._build_prompts_async(
                    session, round_state, compact_summary=compact_summary,
                )
            )
        raise RuntimeError(
            "_build_prompts 已在运行中 event loop 内被调用；"
            "请使用 `await self._build_prompts_async(...)`"
        )

    async def _resolve_round_capability(
        self, round_state: RoundState
    ) -> ProviderModelMetadata:
        """§一 能力来源链：Provider 元数据 → models.yaml.context_limit → unknown。

        逐个参与模型取能力，再按"多模型共用一份预算"取最严格（min）：

          context_limit    —— 已知值取 min；全部未知 → unknown
          model_max_output —— 已知值取 min；**只有 Provider 能给**（YAML 不提供）

        Mock 模型（provider.type == "mock"）默认不走 Provider 查询：它们没有
        Provider 元数据概念，直接走 YAML fallback 并把 fetch_status 诚实标为
        not_fetched。因此离线 / 测试路径不会联网。

        显式注入的 resolver **优先**（此时不做 mock 判断）——离线 / 测试用它
        注入确定性能力，与生产链路走完全相同的合并逻辑。
        """
        settings = getattr(self.registry, "provider_settings", None)
        real_provider_registry = bool(
            settings is not None and getattr(settings, "type", "") != "mock"
        )
        resolver = self._capability_resolver
        if resolver is None and real_provider_registry:
            resolver = get_default_resolver()

        resolved: list[ProviderModelMetadata] = []
        slugs: list[str] = []
        for model_id in round_state.participants:
            try:
                cfg = self.registry.config_for(model_id)
            except KeyError:
                continue
            slug = str(getattr(cfg, "model", "") or "") or model_id
            slugs.append(slug)
            fallback_ctx = getattr(cfg, "context_limit", None)
            if resolver is not None:
                provider_meta = await resolver.resolve(slug)
            else:
                provider_meta = fallback_only_metadata(slug, None)
            resolved.append(
                apply_context_limit_fallback(provider_meta, fallback_ctx)
            )

        if not resolved:
            return fallback_only_metadata(";".join(round_state.participants), None)

        ctx_known = [m.context_limit for m in resolved if m.context_limit.value is not None]
        out_known = [
            m.model_max_output for m in resolved if m.model_max_output.value is not None
        ]
        statuses = {m.fetch_status for m in resolved}
        if statuses == {"failed"}:
            fetch_status: FetchStatus = "failed"
        elif "ok" in statuses:
            fetch_status = "ok"
        elif "cached" in statuses:
            fetch_status = "cached"
        else:
            fetch_status = "not_fetched"

        return ProviderModelMetadata(
            model_slug=";".join(slugs),
            context_limit=(
                min(ctx_known, key=lambda t: t.value) if ctx_known else UNKNOWN_TOKEN
            ),
            model_max_output=(
                min(out_known, key=lambda t: t.value) if out_known else UNKNOWN_TOKEN
            ),
            fetched_at=_iso_now(),
            fetch_status=fetch_status,
            raw=None,
        )

    async def _build_prompts_async(
        self,
        session: SessionState,
        round_state: RoundState,
        *,
        compact_summary: str | None = None,
    ) -> tuple[dict[str, str], dict[str, list[str]]]:
        """返回 (每个模型的 prompt, 每个模型的匿名化软告警)。

        S3.5 算法（PHASE4_REFACTOR_PLAN §11 / §13）：
          1. Round 1：只收 原始问题 + 系统公共指令（无 Pin / 无历史）
          2. Round 2+：
             a. ValidRoundPoolBuilder.build(session)            §11.3 step 1
             b. PinStore.load(session_id) → PinState            §11.3 step 6
             c. ContextSelector.build(...) → ContextSelectionResult
                Pin 优先 / Recent Raw 倒序 while break / dropped / overflow
             d. round_state.injected_rounds = pinned_ids + recent_raw_ids
             e. 每个模型 → build_round_n_prompt_v2（user_question + answers 不可分）

        兼容性参数 compact_summary：S3.5 stub 仍返 None；S3.8 Recursive Compaction
        接 ContextSelector 的 overflow 分支后才实现，目前不参与 prompt 路径。
        """
        participants = self.registry.participants

        # ── §一 能力来源（Provider 元数据 → models.yaml fallback → unknown）──
        # 刻意放在 Round 1 分支**之前**：R1 虽无历史 / 无预算，但 Provider 侧的
        # §24 算术（input + output ≤ context_limit）同样需要 context_limit；
        # 若 R1 不解析能力，就变成"无条件读 models.yaml"，那仍是静态值冒充能力。
        # 这不影响 R1 的独立性：prompt 内容仍只含原始问题 + 系统公共指令。
        metadata = await self._resolve_round_capability(round_state)
        round_state.context_limit = metadata.context_limit.value

        # Round 1：严格独立（architecture P2）
        if round_state.round_id == 1:
            prompt = build_round1_prompt(
                original_question=session.original_question,
                participants=participants,
            )
            return {m: prompt for m in round_state.participants}, {}

        # Round 2+：构造 Valid Round Pool（§11.3 step 1）+ Pin State
        valid_pool = ValidRoundPoolBuilder(self.storage).build(session)
        pin_store = PinStore(self.storage)
        pin_state = pin_store.load(session.session_id)

        # ── 本次请求输出上限（§四）：只有显式给出才算已知 ──
        # 未显式给出 = unknown，**不再**用任何静态值兜底；此时 reserved 取模型
        # 能力上限（§22：reserved 不能超过模型允许的最大输出能力）。
        if round_state.request_max_output:
            req_max = _token(
                int(round_state.request_max_output),
                "config_static", "estimated",
            )
        else:
            req_max = TokenEstimate(None, "provider_unavailable", "unknown")

        # §21 fixed：粗估 system + 当前 user instruction（无 tool defs / 无 peer）
        from .context_budget import make_fixed_input_tokens
        fixed = make_fixed_input_tokens(
            system_text=SYSTEM_INSTRUCTION,
            tool_text="",  # Phase 3 RealProvider 注入，本估算未含
            current_user_text=round_state.user_instruction,
            peer_context_reserve_text="",
        )

        # ── S3.8：Session Compaction（§14 / §34 / §38）──────────────
        # probe：先算一次（**不落日志**）拿到 dropped 区 + compaction_budget。
        # ContextSelector 的预算由 S3.3 compute_budget() 给出，此处**不重算**。
        probe = ContextSelector().build(
            metadata=metadata,
            request_max_output=req_max,
            valid_pool=valid_pool,
            pin_state=pin_state,
            fixed=fixed,
            # S3.10 接线（MD §20 §55）：k 来自 UI → API → RoundState，
            # 不再硬编码，保证前后端使用同一值。
            k=round_state.k,
            storage=None,      # probe 不落日志
            session_id=None,
        )

        compacted_items: list[HistoricalContextItem] = []
        dropped_ids = set(probe.dropped)
        # §14 输入约束：只压 Valid Round（失败 Round 已被 ValidRoundPoolBuilder 过滤）
        # §34：ContextSelector 的 dropped 只**显式记录首个装不下的 Round**
        #      （倒序 while-break 的断点）；压缩区 = 该断点及其更旧的全部 Valid Round，
        #      排除已装入的 Recent Raw 与 Pin（Pin 永不压缩，§27）。
        _recent_ids = {it.round_id for it in probe.recent_raw}
        _pinned_ids = set(pin_state.pinned_round_ids)
        _cutoff = max(dropped_ids) if dropped_ids else None
        compaction_rounds = [
            vr
            for vr in valid_pool
            if _cutoff is not None
            and vr.round_id <= _cutoff
            and vr.round_id not in _recent_ids
            and vr.round_id not in _pinned_ids
        ]
        cb_v = probe.budget.compaction_budget.value
        # §38 Session 级递归状态（用于明确 overflow 与日志）
        compaction_attempts = 0
        compaction_overflow: str | None = None

        # ── S3.8 / §38：Session 级递归 Compaction ──────────────────────
        # 递归对象是"**更旧的历史**"，不是反复压同一产物：
        #   Compacted(#1) + 更旧 Valid Rounds → Compaction #2 → 重新构造 Context
        # 每次重新构造后检查 Input + Reserved 是否 ≤ Context Limit；
        # 仍超限且还有更旧 Valid Round → 继续（全局 attempt 受
        # MAX_COMPACTION_ROUNDS 约束）。Pin / Recent Raw / Current User /
        # Raw Archive 在任何分支下都不被删除或压缩。
        if compaction_rounds and cb_v:
            compressed_ids: set[int] = set()
            attempt_no = 1
            prev_content: str | None = None
            prev_files: list[str] = []
            _compact_model_id = (
                round_state.participants[0] if round_state.participants else None
            )
            while attempt_no <= self._compaction_service.max_rounds:
                candidates = [
                    vr for vr in compaction_rounds if vr.round_id not in compressed_ids
                ]
                if not candidates:
                    # 条件 C：已无更多可压缩的 Valid Round
                    compaction_overflow = "no_more_valid_rounds"
                    break

                # §38 分批（预算驱动，无固定轮数）：
                # 单次压缩的输入不超过 history_budget（= 一个 Context 窗口的
                # 历史容量）。超出部分留给下一轮"纳入更旧历史"再压 —— 这样
                # 才存在"第二次 Compaction 覆盖更旧历史"的语义。
                _history_v = probe.budget.history_budget.value or 0
                batch: list = []
                _acc = 0
                for _vr in sorted(candidates, key=lambda r: r.round_id, reverse=True):
                    _t = _vr.token_estimate.value or 0
                    if batch and _history_v and _acc + _t > _history_v:
                        break
                    batch.append(_vr)
                    _acc += _t

                try:
                    block = None
                    if attempt_no == 1:
                        # §14.4 复用：compactions/ 已有覆盖产物 → 不重复调 LLM
                        block = self._compaction_service.find_reusable(
                            session_id=session.session_id,
                            source_rounds=[vr.round_id for vr in batch],
                        )
                    if block is None:
                        block = await self._compaction_service.compact(
                            session_id=session.session_id,
                            rounds=batch,
                            compaction_budget=cb_v,
                            model_id=_compact_model_id,
                            trigger_round_id=round_state.round_id,
                            # §38：全局 attempt 递增 + 携带上一轮产物；
                            # 调用内不再做 summary-of-summary —— Session 级递归
                            # （纳入更旧 Valid Round）由本循环接管。
                            attempt_start=attempt_no,
                            previous_content=prev_content,
                            previous_files=prev_files,
                            recursive_within_call=False,
                            # §38：日志 source_rounds = 累计覆盖范围（含上一轮已覆盖）
                            covered_rounds=sorted(
                                compressed_ids | {vr.round_id for vr in batch}
                            ),
                        )
                except Exception as exc:  # noqa: BLE001 - 失败降级不阻断
                    _log(
                        f"session={session.session_id} "
                        f"round={round_state.round_id} "
                        f"compaction_failed_degraded_to_raw "
                        f"type={type(exc).__name__}: {exc}"
                    )
                    compaction_overflow = f"compaction_failed:{type(exc).__name__}"
                    break

                if block is None or not block.content:
                    compaction_overflow = "compaction_produced_no_content"
                    break

                compacted_items = [self._make_compacted_item(block)]
                compressed_ids |= {vr.round_id for vr in batch}
                prev_content = block.content
                prev_files = list(block.source_files)
                _last = block.latest
                attempt_no = (_last.attempt if _last is not None else attempt_no) + 1
                compaction_attempts = attempt_no - 1

                # 重新构造 Context（不落日志）→ 条件 A：已满足预算则结束
                recheck = ContextSelector().build(
                    metadata=metadata,
                    request_max_output=req_max,
                    valid_pool=valid_pool,
                    pin_state=pin_state,
                    fixed=fixed,
                    k=round_state.k,
                    storage=None,
                    session_id=None,
                    compacted_items=compacted_items,
                )
                if not self._needs_more_compaction(recheck):
                    break
                if recheck.overflow.get("pinned_history_exceeds_budget"):
                    # Pin 不可压缩：继续压更旧历史无法解决（§27 不偷删 Pin）
                    compaction_overflow = "pinned_exceeds_budget_unsolvable"
                    break
            else:
                # 条件 B：达到 MAX_COMPACTION_ROUNDS 仍超限
                compaction_overflow = "compaction_limit_reached"

        # §11.3 主调用（S3.8：传入 compacted_items，消费 compaction_budget）
        result = ContextSelector().build(
            metadata=metadata,
            request_max_output=req_max,
            valid_pool=valid_pool,
            pin_state=pin_state,
            fixed=fixed,
            # S3.10 接线（MD §20 §55）：k 来自 UI → API → RoundState，
            # 不再硬编码，保证前后端使用同一值。
            k=round_state.k,
            storage=self.storage,
            session_id=session.session_id,
            compacted_items=compacted_items,
            # S3.10（MD §57）：预算/选择事件带 round_id
            round_id=round_state.round_id,
        )

        # 记录实际注入的轮次（Pinned + Recent Raw；§57）
        round_state.injected_rounds = (
            [it.round_id for it in result.pinned]
            + [it.round_id for it in result.recent_raw]
        )

        # ── §38 终止状态：把"仍超限 / 无可压"明确暴露 ──────────────────
        # 不改动任何已选内容，只在 overflow 与日志中记录明确状态。
        if self._needs_more_compaction(result) and compaction_overflow is None:
            compaction_overflow = "compaction_insufficient"
        if compaction_overflow:
            result.overflow.setdefault("compaction_overflow", compaction_overflow)
            result.overflow["compaction_attempts"] = compaction_attempts
            self._log_compaction_overflow(
                session.session_id, round_state.round_id,
                compaction_overflow, compaction_attempts,
            )

        # ── S3.11（MD §22/§23）：把预算层的输出预留额写回 RoundState ──
        # reserved = ContextBudget.reserved_output_tokens
        #          = min(model_max_output, request_max_output)
        # _run_one 会把它原样传给 Provider，保证"实际请求 max_tokens"与
        # context_budget 日志中的 reserved_output_tokens 同源（验收项 5/8）。
        # Provider 不得再自行重算另一套预算。
        if result.budget is not None:
            _reserved = result.budget.reserved_output_tokens
            round_state.reserved_output_tokens = (
                _reserved.value
                if _reserved is not None and _reserved.value is not None
                else None
            )

        # S3.7：Peer Context Preparation（§13 §14）
        #   peer_review=True  → 为每个 target 模型准备其他 ACTIVE 模型的上下文
        #                       （1 个有效回答 → Final Answer；>1 → Conversation Summary）
        #   peer_review=False → 返回 {}（§3.1）
        peer_context_map = await self._peer_preparator.prepare(
            session=session,
            round_state=round_state,
            valid_pool=valid_pool,
        )
        # 每个模型构造 prompt
        prompts: dict[str, str] = {}
        warnings: dict[str, list[str]] = {}

        # ── Phase 4.5（§4.5.3 - §4.5.8）：MCP Evidence → Context ────────
        # A1（§4.5.3）：每个模型只注入**自己**的历史 Evidence，
        # 因此选择必须逐模型进行，不能像 Compacted History 那样全体共享。
        # 预算（§4.5.7）：与 Compacted History 共用 CompactionBudget
        #   Evidence ≤ CompactionBudget - Compacted History
        # 不新增独立预算、不改 k（§4.5.7 明令）。
        _budget = result.budget
        if _budget is not None and _budget.compaction_budget.value is not None:
            evidence_budget: int | None = max(
                0,
                int(_budget.compaction_budget.value)
                - int(_budget.compacted_tokens.value or 0),
            )
        else:
            evidence_budget = None
        # §4.5.5：仍在无损 Context（Pin + Recent Raw）的 Round → 不重复注入
        lossless_round_ids = (
            [it.round_id for it in result.pinned]
            + [it.round_id for it in result.recent_raw]
        )

        for model_id in round_state.participants:
            # 历史项：[（round_id, HistoricalContextItem）] 由 Pinned → Recent → Compacted
            historical_items: list[tuple[int, HistoricalContextItem]] = [
                (it.round_id, it) for it in result.all_items
            ]
            # S3.7：peer context 由 PeerContextPreparator 提供（数据层白名单已生效）
            peer_items = peer_context_map.get(model_id, [])
            # Phase 4.5：本模型自己的历史 Evidence（§4.5.2 / §4.5.4 / §4.5.5）
            # §4.5.11：身份判定需要真实词元全集 → 传 registry.participants
            # （与 build_round_n_prompt_v3 的 A3 硬校验同源，不是另一套名单）
            evidence_items = self._evidence_service.select_for_context(
                session_id=session.session_id,
                model_id=model_id,
                participants=participants,
                current_round_id=round_state.round_id,
                lossless_round_ids=lossless_round_ids,
                budget_tokens=evidence_budget,
            ).entries
            prompt, warns = build_round_n_prompt_v3(
                original_question=session.original_question,
                user_instruction=round_state.user_instruction,
                historical_items=historical_items,
                peer_context_items=peer_items,
                self_model_id=model_id,
                candidate_ids=round_state.candidate_ids,
                peer_review=round_state.peer_review,
                participants=participants,
                evidence_items=evidence_items,
            )
            prompts[model_id] = prompt
            warnings[model_id] = warns

        # §34：Pin overflow / context_overflow 已在 ContextSelector._log_selection 落日志
        # §42：peer_context_preparation 已在 PeerContextPreparator._log 落日志
        return prompts, warnings

    # 注：S3.5 删除（PHASE4_REFACTOR_PLAN §11.1）：
    #   _load_history / _trim_to_budget / _entries_for_model / _budget_for
    #   / _truncate_entries — 全部由 core.context_selector.ContextSelector.build()
    #   代替。HISTORY_WINDOW=3 硬裁常量已删除。预算驱动倒序装载见 §11.2 Custom Rule C6。

    @staticmethod
    def _needs_more_compaction(result) -> bool:
        """§38：重新构造 Context 后，整体是否仍超过 Context Limit。

        只**读取**已有预算数值（fixed / pinned / recent_raw / compacted / reserved），
        不重算任何预算（预算唯一来源仍是 ContextBudget）。

        两个"仍超预算"信号（§38）：
          ① fixed + pinned + recent_raw + compacted + reserved > context_limit
             —— 整体装不下（Pin 超预算除外，见调用方的专门分支）
          ② compaction 区溢出（compacted_exceeds_budget）
             —— 压缩产物比压缩区预算还大 ⇒ 压缩不足，应纳入更旧历史再压
        """
        budget = getattr(result, "budget", None)
        if budget is None:
            return False
        overflow = getattr(result, "overflow", {}) or {}
        if overflow.get("compacted_exceeds_budget"):
            return True
        limit = budget.context_limit.value
        if limit is None:
            return False
        total = (
            (budget.fixed_input_tokens.value or 0)
            + (budget.pinned_tokens.value or 0)
            + (budget.recent_raw_tokens.value or 0)
            + (budget.compacted_tokens.value or 0)
        )
        reserved = budget.reserved_output_tokens.value
        if reserved is None:
            return False
        return total + reserved > limit

    def _log_compaction_overflow(
        self, session_id: str, round_id: int, reason: str, attempts: int
    ) -> None:
        """§38：压缩无法满足预算时的明确状态（沿用现有 context_budget_warning 事件）。

        只记录状态 —— 不删除任何内容（Raw Archive / Pin / Recent Raw / Current User）。
        """
        line = {
            "event": "context_budget_warning",
            "session_id": session_id,
            "round_id": round_id,
            "reason": reason,
            "compaction_attempts": attempts,
            "max_compaction_rounds": self._compaction_service.max_rounds,
            "timestamp": _now(),
        }
        policy_path = self.storage.session_dir(session_id) / "context_policy.jsonl"
        policy_path.parent.mkdir(parents=True, exist_ok=True)
        with policy_path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(line, ensure_ascii=False) + "\n")

    def _log_policy(self, round_state: RoundState, session_id: str) -> None:
        """S3.10：context_policy 事件（MD §55）。

        k 取自 round_state.k（与 _build_prompts_async 消费的是同一字段），
        因此日志中的 k 就是本次 Round 实际使用的 k。
        """
        line = {
            "event": "context_policy",
            "session_id": session_id,
            "round_id": round_state.round_id,
            "k": round_state.k,
            "peer_review": round_state.peer_review,
            "summary_mode": round_state.summary_mode,
            "request_max_output": round_state.request_max_output,
            "participants": list(round_state.participants),
            "timestamp": _now(),
        }
        policy_path = (
            self.storage.session_dir(session_id) / "context_policy.jsonl"
        )
        policy_path.parent.mkdir(parents=True, exist_ok=True)
        with policy_path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(line, ensure_ascii=False) + "\n")

    def _build_evidence_for_round(
        self, session_id: str, round_state: RoundState,
    ) -> None:
        """S3.9：Round 完成后，为每个成功的 (round, model) 生成 Evidence Index。

        只覆盖 status=="complete" 且有正文的模型；错误模型没有 Final
        Answer，无可判定的 used_in_final_answer（失败 call 本身也不入
        Index —— Error 不是 Evidence，§46）。
        """
        responses = self.storage.load_responses(session_id, round_state.round_id)
        for model_id in round_state.participants:
            if round_state.status.get(model_id) != "complete":
                continue
            answer = responses.get(model_id)
            if not answer:
                continue
            self._evidence_service.build_index(
                session_id=session_id,
                round_id=round_state.round_id,
                model_id=model_id,
                final_answer=answer,
            )

    def _make_compacted_item(self, block: "CompactedBlock") -> HistoricalContextItem:
        """把 CompactedBlock → HistoricalContextItem(source="compacted")。

        §4.9.1：Compaction 是 Session 级共享产物，**不是**某个模型的 Summary。
        归属声明（含参与者 X/Y/Z 的观点）由 ``render_compacted_block()``
        内置在 content 里（F3 防线），prompt_builder 对 compacted 整体透传。

        round_id 取压缩区**最小** round_id（仅用于排序与展示定位）；
        user_question 为空 —— 压缩块不对应单一问题。
        """
        src_rounds: list[int] = []
        for rec in block.records:
            src_rounds.extend(rec.source_rounds)
        anchor = min(src_rounds) if src_rounds else 0
        return HistoricalContextItem(
            round_id=anchor,
            user_question="",
            answers=[("__compacted__", render_compacted_block(block))],
            source="compacted",
            token_estimate=block.token_estimate(),
        )

    async def _run_one(
        self,
        session_id: str,
        round_state: RoundState,
        model_id: str,
        prompt: str,
        bus: RoundBus,
    ) -> None:
        """跑单个模型。任何异常都在这里被吃掉，只影响这一个模型。

        phase2 §15：慢、timeout、API error、HTTP error、streaming 中断，
        都不得导致其他模型被取消。

        Phase 3：按模型配置的 tools 列表传递给 Provider，
        让支持 tool calling 的真实 LLM 能自动调用 MCP 工具。
        """
        ctx = f"session={session_id} round={round_state.round_id} model={model_id}"
        started = time.monotonic()
        first_token: float | None = None
        chunks = 0
        _log(f"{ctx} start")

        # Phase 3：不在此处提取 tools，由 RealProvider 根据 self._cfg.tools
        # 自动调用 mcp_registry.tool_schemas() 转换为 OpenAI 格式。
        # 这样避免传递 list[str] 类型导致 Provider 跳过 schema 转换（见 real_provider.py:163）。
        buf: list[str] = []
        provider = None

        # MCP Runtime 调试日志：把本模型本轮的 tool_call 落到轮次目录。
        # contextvars 保证 asyncio.gather 下各模型互不串扰（Task 会复制 context）。
        runtime_ctx = runtime_log.RuntimeContext(
            session_id=session_id,
            round_id=round_state.round_id,
            model_id=model_id,
            directory=self.storage.round_dir(session_id, round_state.round_id),
        )
        runtime_token = runtime_log.set_runtime_context(runtime_ctx)
        try:
            provider = self._provider_factory(model_id)
            # §一：把本轮解析出的 context_limit 注入 Provider —— Provider 侧的
            # §24 算术必须与预算层同源（Provider 元数据优先，YAML 仅 fallback）。
            # 刻意不动 Provider Protocol / generate() 签名：用可选注入点，
            # Mock Provider / 测试替身没有该方法时自动跳过，行为不变。
            _inject_limit = getattr(provider, "set_round_context_limit", None)
            if callable(_inject_limit):
                _inject_limit(round_state.context_limit)
            # S3.11：把预算层输出上限交给 Provider（None=未接线→Provider 回退自身能力）
            async for chunk in provider.generate(
                round_id=round_state.round_id,
                prompt=prompt,
                max_output_tokens=round_state.reserved_output_tokens,
            ):
                if first_token is None:
                    first_token = time.monotonic() - started
                    _log(f"{ctx} first_token={first_token:.2f}s")
                buf.append(chunk)
                chunks += 1
                bus.publish({"model": model_id, "type": "token", "content": chunk})

            content = "".join(buf)
            self.storage.save_response(session_id, round_state.round_id, model_id, content)
            round_state.status[model_id] = "complete"
            bus.publish({"model": model_id, "type": "done"})
            _log(
                f"{ctx} done chunks={chunks} "
                f"chars={len(content)} elapsed={time.monotonic() - started:.2f}s"
            )
        except Exception as exc:  # noqa: BLE001 - 隔离单模型失败，不拖垮整轮
            content = "".join(buf)
            if content:
                # 保留已经产出的部分内容（architecture_v0.1.md D5 / §28）
                try:
                    self.storage.save_response(session_id, round_state.round_id, model_id, content)
                except Exception:  # noqa: BLE001
                    pass
            round_state.status[model_id] = "error"
            # 错误信息来自 Provider，已做脱敏（不含 Key / Header）
            bus.publish({"model": model_id, "type": "error", "error": str(exc)})
            _log(
                f"{ctx} error chunks={chunks} "
                f"elapsed={time.monotonic() - started:.2f}s type={type(exc).__name__}: {exc}"
            )
            traceback.print_exc(file=sys.stderr)
        finally:
            runtime_log.reset_runtime_context(runtime_token)
            # phase1_1 §6.2：优先使用 Provider 返回的真实 usage。
            # 失败时也记录 —— 那部分 token 是真实消耗掉的。
            # 仅用于记录，不作为计费依据或模型质量指标。
            usage = getattr(provider, "usage", None) if provider is not None else None
            if usage:
                round_state.usage[model_id] = dict(usage)
