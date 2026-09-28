"""CrossLLM 核心数据结构。

依据：
- architecture_v0.1.md §12 Model Participant / §13 Session State / §14 Round State
- phase1_mock.md §11 Session State
- architecture_v0.1.md §29「v0.1 不允许为了未来功能提前增加复杂框架」

字段刻意最小化。`architecture_v0.1.md` §13 中的 disputes / evidence_ids / tools
属 Phase 3/4，本阶段不实现。

匿名化不变量 A1：本模块中的 model_id / display_name / model 均为系统内部真实身份，
一律禁止进入任何模型的 context（由 core/anonymizer.py 强制）。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass
class ModelParticipant:
    """平级模型参与者（architecture_v0.1.md P1：无 Leader / Judge / Manager）。"""

    id: str  # "qwen"       内部真实身份，禁止进入任何模型 context
    display_name: str  # "Qwen"       仅 UI 展示，禁止进入任何模型 context
    provider: str  # "mock"       内部，禁止进入任何模型 context
    model: str  # "mock-qwen"  内部，禁止进入任何模型 context
    enabled: bool
    # 该模型允许使用的 MCP 工具名列表（mcp执行确认.md §10/§11）。
    # 空列表表示该模型不使用任何 MCP。
    tools: list[str] = field(default_factory=list)

    def identity_tokens(self) -> tuple[str, ...]:
        """会被 anonymizer 拦截的身份词元（不变量 A3）。"""
        return (self.id, self.display_name, self.model)

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "display_name": self.display_name,
            "provider": self.provider,
            "model": self.model,
            "enabled": self.enabled,
            "tools": list(self.tools),
        }


@dataclass
class RoundState:
    """一轮的元数据。

    注意：不保存 responses 正文。正文以 sessions/<sid>/round_NNN/<model>.md
    为唯一存储，避免同一份内容在 JSON 与 md 中双写导致不一致。
    需要正文时由 core/storage.py 按需读取后组装。
    """

    round_id: int
    user_instruction: str
    participants: list[str]  # 本轮实际参与者（真实 model_id）
    # candidate_id 映射：model_id -> candidate_id，轮内全局共享。
    # 系统内部专用（不变量 A4）：不进模型 context、不写进 round_NNN/ 下任何文件、
    # 不通过 API 返回。互评关闭或单模型时不分配（phase1_1 §3.3：不产生无意义 Candidate）。
    candidate_ids: dict[str, str] = field(default_factory=dict)
    # 互评开关（phase1_1 §3）。只决定是否加入 peer context，
    # **不影响模型自己的普通聊天上下文**。单模型时等价于关闭。
    peer_review: bool = False
    # 实际注入了哪几轮（由新到旧）。预算保护丢弃最旧轮后这里会相应变短，
    # 便于核对 phase1_1 §5.2 的丢弃逻辑是否按预期生效。
    injected_rounds: list[int] = field(default_factory=list)
    # 各模型真实 token 用量，取自 Provider 返回值（phase1_1 §6.2）。
    # Mock Provider 无此数据。仅用于记录，不作为计费或质量指标。
    usage: dict[str, dict] = field(default_factory=dict)
    status: dict[str, str] = field(default_factory=dict)  # model_id -> complete|error
    timestamp: str = ""
    # ── S3.10 接线（MD §20 §55 §22）：Round 级政策参数 ──────────────
    # k：无损历史保留比例（0.01–0.90），由 UI / API 传入并贯穿到
    #    ContextBudget.k → lossless_budget / compaction_budget。
    #    禁止 UI 显示一个值、后端用另一个值（用户任务书 §3）。
    k: float = 0.5
    # request_max_output：本次请求**显式**的输出上限。
    #   None = 未显式指定 → 本次请求的有效输出上限即模型能力上限
    #   （model_max_output，来自 Provider 元数据）；模型能力也未知时
    #   ContextBudget 记 unknown / degraded，**不**用任何静态值兜底。
    request_max_output: int | None = None
    # summary_mode：用户是否在本轮主动生成 Summary（S3.6 §8）
    summary_mode: bool = False
    # S3.11（MD §22）：预算层为输出预留的额度
    # = ContextBudget.reserved_output_tokens = min(provider_max, request_max)
    # 由 _build_prompts_async 写入，_run_one 读取后传给 Provider —— 保证
    # Provider 实际请求值与 ContextBudget 日志中的 reserved 完全同源
    # （禁止 Provider 自行重算另一套预算）。
    reserved_output_tokens: int | None = None
    # §一：本轮实际采用的 context_limit（Provider 元数据解析结果；
    # 解析不出时才用 models.yaml.context_limit 作 fallback，两者皆无 = None）。
    # 由 _build_prompts_async 从 _resolve_round_capability() 写入，_run_one 注入
    # Provider（RealProvider.set_round_context_limit），保证 Provider 侧的 §24 算术
    # 与预算层使用**同一个** context_limit —— 而不是读 models.yaml 的静态快照。
    # None 不表示"能力为零"：Provider 会回退自身 YAML 值，仍取不到则不下发 max_tokens。
    context_limit: int | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "round_id": self.round_id,
            "user_instruction": self.user_instruction,
            "participants": list(self.participants),
            "candidate_ids": dict(self.candidate_ids),
            "peer_review": self.peer_review,
            "injected_rounds": list(self.injected_rounds),
            "usage": {k: dict(v) for k, v in self.usage.items()},
            "status": dict(self.status),
            "timestamp": self.timestamp,
            # S3.10
            "k": self.k,
            "request_max_output": self.request_max_output,
            "summary_mode": self.summary_mode,
            # S3.11
            "reserved_output_tokens": self.reserved_output_tokens,
            # S3.12（§一）：本轮解析出的 context_limit（与 context_policy.jsonl 同源）
            "context_limit": self.context_limit,
        }

    def public_dict(self) -> dict[str, Any]:
        """对外投影（API 响应）。剔除 candidate_ids —— 不变量 A4。"""
        d = self.to_dict()
        d.pop("candidate_ids", None)
        return d

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> RoundState:
        return cls(
            round_id=int(d["round_id"]),
            user_instruction=d.get("user_instruction", ""),
            participants=list(d.get("participants", [])),
            candidate_ids=dict(d.get("candidate_ids", {})),
            peer_review=bool(d.get("peer_review", False)),
            injected_rounds=[int(x) for x in d.get("injected_rounds", [])],
            usage={k: dict(v) for k, v in (d.get("usage") or {}).items()},
            status=dict(d.get("status", {})),
            timestamp=d.get("timestamp", ""),
            # S3.10：k / request_max_output / summary_mode
            k=float(d.get("k", 0.5)),
            request_max_output=(
                int(d["request_max_output"])
                if d.get("request_max_output") is not None else None
            ),
            summary_mode=bool(d.get("summary_mode", False)),
            # S3.11：reserved_output_tokens
            reserved_output_tokens=(
                int(d["reserved_output_tokens"])
                if d.get("reserved_output_tokens") is not None else None
            ),
            # S3.12（§一）：context_limit
            context_limit=(
                int(d["context_limit"])
                if d.get("context_limit") is not None else None
            ),
        )


@dataclass
class SessionState:
    """一次讨论的完整状态。"""

    session_id: str
    original_question: str
    current_round: int
    active_models: list[str]
    closed_models: list[str]
    rounds: list[RoundState] = field(default_factory=list)
    created_at: str = ""
    updated_at: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "session_id": self.session_id,
            "original_question": self.original_question,
            "current_round": self.current_round,
            "active_models": list(self.active_models),
            "closed_models": list(self.closed_models),
            "rounds": [r.to_dict() for r in self.rounds],
            "created_at": self.created_at,
            "updated_at": self.updated_at,
        }

    def public_dict(self, with_rounds: bool = True) -> dict[str, Any]:
        """对外投影（API 响应）。rounds 使用 public_dict —— 不变量 A4。"""
        return {
            "session_id": self.session_id,
            "original_question": self.original_question,
            "current_round": self.current_round,
            "active_models": list(self.active_models),
            "closed_models": list(self.closed_models),
            "rounds": [r.public_dict() for r in self.rounds] if with_rounds else [],
            "created_at": self.created_at,
            "updated_at": self.updated_at,
        }

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> SessionState:
        return cls(
            session_id=d["session_id"],
            original_question=d.get("original_question", ""),
            current_round=int(d.get("current_round", 0)),
            active_models=list(d.get("active_models", [])),
            closed_models=list(d.get("closed_models", [])),
            rounds=[RoundState.from_dict(r) for r in d.get("rounds", [])],
            created_at=d.get("created_at", ""),
            updated_at=d.get("updated_at", ""),
        )

    def round(self, round_id: int) -> RoundState | None:
        for r in self.rounds:
            if r.round_id == round_id:
                return r
        return None
