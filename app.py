"""CrossLLM Phase 1 —— FastAPI 应用入口（architecture_v0.1.md §20/§21）。

HTTP 路由 + SSE Streaming。业务逻辑一律委托给 core/ 下的模块，本文件不做编排。

API 清单（phase1_mock.md §19）：
    POST /api/session
    GET  /api/session/{session_id}
    POST /api/session/{session_id}/round
    POST /api/session/{session_id}/selection
    GET  /api/session/{session_id}/events/{round_id}
    GET  /api/sessions                  Session 摘要列表（只读导航）
    GET  /api/config/summary-prompt     默认 Conversation Summary Prompt（单一真源）

注意：SSE 事件里的 `model` 字段是**真实 model_id** —— 那是给人类用户看的。
匿名化只作用于进入模型 context 的内容（不变量 A3 / A4）。

S3.6（docs/phase4_memory.md §8/§9/§66 + PHASE4_REFACTOR_PLAN §3.2/§18.2）：
    勾选 Summary 后，输入框（= 本轮指令框）里的文本就是**用户可编辑的
    Summary Prompt**，点"发送"发出的是 **Summary 请求**：
        POST /api/session/{sid}/round {summary_mode: true, instruction: <prompt>}
    → SummaryService.generate(trigger="user", prompt=instruction)，
    **不创建 Round**（D1：Summary 不参与 Round 编号），不写 responses/。
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator
from pathlib import Path

from fastapi import FastAPI, HTTPException
from fastapi.responses import JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from core.model_manager import (
    PROVIDER_MOCK,
    PROVIDER_OPENAI_COMPATIBLE,
    ModelRegistry,
    load_dotenv,
)
from core.mcp_registry import MCPRegistry
from core.pin_store import PinStore
from core.summary_service import SummaryService, SummaryInput
from core.evidence_index import EvidenceIndexService
from core.project_memory import ProjectMemoryService
from core.round_manager import RoundManager
from core.session_manager import SessionManager
from core.storage import Storage
from providers.base import LLMProvider
from providers.mock_provider import MockProvider
from providers.real_provider import RealProvider

BASE_DIR = Path(__file__).resolve().parent
SESSIONS_DIR = BASE_DIR / "sessions"
STATIC_DIR = BASE_DIR / "static"
CONFIG_PATH = BASE_DIR / "config" / "models.yaml"
MCP_CONFIG_PATH = BASE_DIR / "config" / "mcp.yaml"
DOTENV_PATH = BASE_DIR / ".env"

# SSE 空闲心跳间隔（秒）。真实 LLM 首 token 可能要几十秒，期间没有任何数据，
# 中间的代理或浏览器可能判定连接超时。只插入注释行，不改变事件结构。
HEARTBEAT_SECONDS = 15.0

# 密钥只从环境变量来（phase2 §6/§8）。.env 只是本地便利层，
# 且内部用 setdefault —— 系统环境变量优先。
load_dotenv(DOTENV_PATH)

# 有配置就用配置；没有则回落到 Phase 1 的 Mock 池，
# 这样不配任何真实模型时工程依然能直接跑起来。
if CONFIG_PATH.exists():
    registry = ModelRegistry.from_yaml(CONFIG_PATH)
else:
    registry = ModelRegistry()

# MCP 配置：解析但不立即连接，避免未部署的 Server 拖垮应用启动。
mcp_registry = MCPRegistry.from_yaml(MCP_CONFIG_PATH) if MCP_CONFIG_PATH.exists() else None

storage = Storage(SESSIONS_DIR)


def _make_provider(model_id: str) -> LLMProvider:
    """按配置构造 Provider。

    RoundManager 只拿到 LLMProvider 接口，不知道底层是 Mock 还是真实 HTTP
    （phase2 §4/§7）。失败注入开关继续保留（phase1 §23 Test 7）——
    这样不必消耗真实 token 就能验证错误隔离路径。
    """
    settings = registry.provider_settings
    config = registry.config_for(model_id)

    if settings.type == PROVIDER_MOCK:
        return MockProvider(model_id, fail=model_id in registry.failing_models())

    if settings.type == PROVIDER_OPENAI_COMPATIBLE:
        # 凭据属于 Provider，所有模型共用同一份（Key 不按模型绑定）
        return RealProvider(
            settings,
            config,
            api_key=registry.api_key(),
            mcp_registry=mcp_registry,
        )

    raise ValueError(f"Provider 类型不支持: {settings.type!r}")


sessions = SessionManager(storage, registry)
rounds = RoundManager(storage, registry, _make_provider)
pins = PinStore(storage)  # S3.4：Pin / Unpin / Pin Tokens 持久化
summaries = SummaryService(storage, provider_factory=_make_provider)  # S3.6：Conversation Summary（按 model 隔离）
evidence_index = EvidenceIndexService(storage)  # S3.9：MCP Evidence Index（不替代 Runtime）
# Phase 4.6 / M1：Project Memory（只读访问；无写入 API）
project_memory = ProjectMemoryService(storage)

app = FastAPI(title="CrossLLM", version="0.2.0")


# ---------- 请求体 ----------


class CreateSessionIn(BaseModel):
    question: str


class RunRoundIn(BaseModel):
    instruction: str = ""
    # 本次发送选中的模型（ACTIVE 的子集）。None / 省略 = 全部 ACTIVE。
    # phase1_1 §2：这与 Session 级的 ACTIVE / CLOSED 生命周期是两件事。
    selected_models: list[str] | None = None
    # 互评开关。只决定是否加入 peer context，不影响模型自己的聊天上下文。
    # phase1_1 §3：单模型时自动等价于关闭。
    # 默认开启 —— phase1_1 §8 保留了「Round 2 可以读取上一轮」这一原核心验收。
    peer_review: bool = True
    # ── S3.10（MD §20 §22 §55）：Round 级政策参数全链路 ────────────
    # k：无损历史保留比例，范围 0.01–0.90（MD §20）。
    #    链路：UI 滑块 → POST k → RoundState.k → ContextBudget.k
    #    → lossless_budget = history_budget × k
    #    → context_policy.jsonl / context_budget 记录同一 k。
    k: float = 0.5
    # request_max_output：本次请求**显式**的输出上限（MD §22 / §四）。
    #    None / 省略 = 未显式指定 → 本次请求的有效输出上限即模型能力上限
    #    （model_max_output，来自 Provider 元数据）。
    #    reserved_output_tokens = min(model_max_output, request_max_output)。
    #    当前 UI 不提供该输入框；保留字段供 API / 脚本显式指定。
    request_max_output: int | None = None
    # summary_mode：用户是否在本轮主动生成 Conversation Summary（MD §8 / S3.6）。
    summary_mode: bool = False
    # ── S3.10 接线（MD §20 §55 §22）──────────────────────────────────
    # k：无损历史保留比例（0.01–0.90）。UI → API → RoundState.k →
    #    ContextBudget.k → lossless/compaction budget → 日志，全程同一个值。
    k: float = 0.5
    # 本次请求输出上限（MD §22）。None = 用 Provider 能力兜底。
    request_max_output: int | None = None
    # 用户是否在本轮主动生成 Summary（MD §8；由前端 Summary 开关映射）
    summary_mode: bool = False


class SelectionIn(BaseModel):
    active_models: list[str]


# S3.4：Pin 操作的请求体（§18 + §19）
class PinIn(BaseModel):
    round_id: int


class PinTokensOut(BaseModel):
    pinned_round_ids: list[int]
    pinned_tokens_value: int | None
    pinned_tokens_source: str
    pinned_tokens_accuracy: str
    # S3.5 接管 overflow 判断前的 placeholder
    overflow: dict | None = None


# S3.6：Conversation Summary 请求体（§8/§9 — 用户主动 trigger，可编辑 Prompt）
class SummaryIn(BaseModel):
    # 用户可编辑的 Summary Prompt；None / 空 = 使用 default prompt。
    prompt: str | None = None
    # 覆盖默认的 user trigger 路径，peer_review 由 S3.7 内部调用，
    # **不允许**通过 API 设置（避免 §16 闭环）。
    trigger: str | None = None  # 校验：只能 None / "user"


class SummaryEntry(BaseModel):
    summary_id: str
    model_id: str
    status: str
    trigger: str
    created_at: str
    source_rounds: list[int]
    source_valid_round: int
    input_tokens_value: int | None
    output_tokens_value: int | None
    file_path: str | None
    reused_from: str | None
    reused_reason: str | None
    prompt: str
    content: str


# ---------- API ----------

# ---------- API ----------


@app.post("/api/session")
def create_session(body: CreateSessionIn):
    try:
        session = sessions.create(body.question)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {"session_id": session.session_id, "state": session.public_dict()}


@app.get("/api/sessions")
def list_sessions():
    """列出已持久化 Session 的摘要（只读；按 updated_at 倒序）。

    只读取每个 Session 的 state.json；单个 Session 损坏会被跳过，不影响整体。
    不写任何数据，也不触发 Summary / Compaction / Evidence / Memory —— 本接口
    只服务于「看到并打开历史 Session」这一导航需求，与 Context Injection 无关。
    """
    return sessions.list_sessions()


@app.get("/api/session/{session_id}")
def get_session(session_id: str):
    """读取 Session（含全部历史 Round），用于页面刷新后的状态恢复。"""
    try:
        session = sessions.get(session_id)
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail="Session 不存在") from exc

    # rounds 使用 public_dict：不含 candidate_ids（不变量 A4）
    payload_rounds = []
    for r in session.rounds:
        d = r.public_dict()
        d["responses"] = storage.load_responses(session_id, r.round_id)
        payload_rounds.append(d)
    # S3.4：pins 同步返回便于页面刷新后恢复 Pin 状态
    pin_state = pins.load(session_id)
    # S3.6：summaries 同步返回（仅 model_id 列表 + 各模型 latest metadata
    # 摘要；完整 content 走 GET /api/session/{sid}/summary/{model}）
    latest_map = summaries.latest_all_models(session_id)
    summaries_payload = {
        mid: {
            "summary_id": s.summary_id,
            "status": s.status,
            "trigger": s.trigger,
            "created_at": s.created_at,
            "source_valid_round": s.source_valid_round,
            "file_path": s.file_path,
        } if s else None
        for mid, s in latest_map.items()
    }
    return {
        "state": session.public_dict(with_rounds=False),
        "rounds": payload_rounds,
        "pins": pin_state.pinned_round_ids,
        "summaries": summaries_payload,
    }


@app.post("/api/session/{session_id}/round", status_code=202)
async def run_round(session_id: str, body: RunRoundIn):
    """触发下一轮。立即返回 202，生成在后台任务中进行。

    S3.6 / MD §8：``summary_mode=True`` 时本接口**不创建 Round**，
    而是发出一次 Conversation Summary 请求（用户主动 trigger="user"），
    输入框里的文本即用户可编辑的 Summary Prompt（§9）。
    返回 200 + Summary 结果，而不是 Round 句柄。
    """
    try:
        session = sessions.get(session_id)
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail="Session 不存在") from exc

    # ── S3.6：Summary 请求（D1：不创建 round_NNN/、不写 responses/）──────
    if body.summary_mode:
        return await _run_summary_request(session_id, session, body)

    # Round 1 未显式给出指令时，使用原始问题
    instruction = (body.instruction or "").strip() or session.original_question
    try:
        round_state, bus = rounds.prepare_round(
            session,
            instruction,
            selected_models=body.selected_models,
            peer_review=body.peer_review,
            # S3.10：政策参数全链路透传（禁止前后端值不一致）
            k=body.k,
            request_max_output=body.request_max_output,
            summary_mode=body.summary_mode,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except KeyError as exc:
        raise HTTPException(status_code=400, detail=f"未知 model_id: {exc}") from exc

    rounds.launch(session_id, round_state, bus)
    return {
        "round_id": round_state.round_id,
        "participants": round_state.participants,
        "peer_review": round_state.peer_review,
        # S3.10：回显后端**实际采用**的政策参数（k 已被夹紧到 0.01–0.90），
        # 便于前端 / E2E 验证"UI 值与后端实际值一致"。
        "k": round_state.k,
        "request_max_output": round_state.request_max_output,
        "summary_mode": round_state.summary_mode,
    }


@app.post("/api/session/{session_id}/selection")
def set_selection(session_id: str, body: SelectionIn):
    """切换 ACTIVE / CLOSED。

    CLOSED 只代表停止参与后续 Round，不代表该模型错误（architecture_v0.1.md P6）。
    """
    try:
        session = sessions.set_selection(session_id, body.active_models)
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail="Session 不存在") from exc
    except KeyError as exc:
        raise HTTPException(status_code=400, detail=f"未知 model_id: {exc}") from exc
    return {"state": session.public_dict()}


# ---------- S3.4：Pin API（§18 / §19）----------


@app.post("/api/session/{session_id}/pin", status_code=200)
def pin_round(session_id: str, body: PinIn):
    """Pin 一个 Valid Round（§18）。

    Pin = 用户对某个历史 Round 的无损优先级声明。被 Pin 的 Round 在
    后续 Context Selection 中优先占用 lossless_budget（§26），永远
    不被自动 Compaction（§27）。幂等：已 Pin 重复 Pin 仅追加 history 事件。

    写入：pins.jsonl + context_policy.jsonl（memory_pin 事件）。
    """
    try:
        if not storage.session_exists(session_id):
            raise FileNotFoundError(session_id)
        _ensure_round_exists(storage, session_id, body.round_id)
        evt = pins.pin(session_id, body.round_id)
    except FileNotFoundError as exc:
        raise HTTPException(
            status_code=404,
            detail=(
                "Session 不存在" if str(exc) == session_id
                else f"Round {body.round_id} 不存在"
            ),
        ) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {
        "round_id": evt.round_id,
        "pinned": evt.pinned,
        "timestamp": evt.timestamp,
        "reason": evt.reason,
        "actor": evt.actor,
        "pinned_round_ids": pins.pinned_round_ids(session_id),
    }


@app.post("/api/session/{session_id}/unpin", status_code=200)
def unpin_round(session_id: str, body: PinIn):
    """Unpin（§18 / §27）。

    仅由用户触发——服务端**不自动解除**。幂等：未 Pin 也写 unpin 事件以保留审计。
    """
    try:
        if not storage.session_exists(session_id):
            raise FileNotFoundError(session_id)
        evt = pins.unpin(session_id, body.round_id)
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail="Session 不存在") from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {
        "round_id": evt.round_id,
        "pinned": evt.pinned,
        "timestamp": evt.timestamp,
        "reason": evt.reason,
        "actor": evt.actor,
        "pinned_round_ids": pins.pinned_round_ids(session_id),
    }


@app.get("/api/session/{session_id}/pins")
def list_pins(session_id: str):
    """列出当前 Pin 状态 + 全部 history（§19）。"""
    try:
        if not storage.session_exists(session_id):
            raise FileNotFoundError(session_id)
        state = pins.load(session_id)
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail="Session 不存在") from exc
    return {
        "session_id": state.session_id,
        "pinned_round_ids": list(state.pinned_round_ids),
        "history": [e.to_dict() for e in state.history],
    }


@app.get("/api/session/{session_id}/pin/tokens", response_model=PinTokensOut)
def pin_tokens(session_id: str):
    """§26 Pin 占用 Token 计算接口（S3.5 ContextSelector 会调用回填 ContextBudget）。

    算法：sum( valid_pool ∩ pinned_round_ids 的 token_estimate.value )。
    若 valid_pool 中至少 1 个 round 的 token_estimate.value is None（unknown），
    整体 unknown（§22 保守策略）。
    """
    try:
        if not storage.session_exists(session_id):
            raise FileNotFoundError(session_id)
        session = storage.load_state(session_id)
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail="Session 不存在") from exc

    # 延迟 import 防循环；本步仅用 S3.2 ValidRoundPoolBuilder 构造 pin 范围
    from core.pin_store import compute_pinned_tokens  # noqa: WPS433
    from core.valid_round import ValidRoundPoolBuilder  # noqa: WPS433

    valid_pool: list = ValidRoundPoolBuilder(storage).build(session)
    pin_state = pins.load(session_id)
    pinned_te = compute_pinned_tokens(pin_state, valid_pool)

    return PinTokensOut(
        pinned_round_ids=list(pin_state.pinned_round_ids),
        pinned_tokens_value=pinned_te.value,
        pinned_tokens_source=pinned_te.source,
        pinned_tokens_accuracy=pinned_te.accuracy_mode,
        overflow=None,  # S3.5 接管后回填 pinned > lossless 判断
    )


def _ensure_round_exists(storage_obj: Storage, session_id: str, round_id: int) -> None:
    """校验 Session 中是否存在该 round（Pin 不能引用未完成的 round）。"""
    session = storage_obj.load_state(session_id)
    for r in session.rounds:
        if r.round_id == round_id:
            return
    raise FileNotFoundError(round_id)


# ---------- S3.6：Conversation Summary API ----------


def _collect_summary_inputs(session_id: str, model_id: str, session_state) -> list["SummaryInput"]:
    """收集「该模型在自己历史中的 User Question + 所有成功 Final Answers」。

    S3.6 §14/§1092：SummaryService.generate() 的输入**只包含 model_id 自己的历史**，
    **不**消费 peer context，避免 §16 反馈闭环。
    """
    from core.valid_round import ValidRoundPoolBuilder
    pool = ValidRoundPoolBuilder(storage).build(session_state)
    inputs: list[SummaryInput] = []
    for vr in pool:
        if vr.user_question:
            # 只取该模型自己成功的 Final Answer（vr.successful_answers 是 dict）
            own_ans = vr.successful_answers.get(model_id)
            if own_ans is not None:
                inputs.append(SummaryInput(
                    round_id=vr.round_id,
                    user_question=vr.user_question,
                    successful_answers={model_id: own_ans},
                ))
    return inputs


# ---------- S3.6：默认 Summary Prompt（单一真源，Plan §18.2）----------


@app.get("/api/config/summary-prompt", response_model=dict)
def get_default_summary_prompt():
    """返回默认 Summary Prompt（MD §9 原文）。

    单一真源在 ``core/summary_prompt.py``；前端**不得**再硬编码一份，
    否则会出现"UI 显示的 Prompt 与后端实际用的不一致"（Plan §18.2 禁止项）。
    """
    from core.summary_prompt import DEFAULT_SUMMARY_PROMPT

    return {"prompt": DEFAULT_SUMMARY_PROMPT, "source": "core/summary_prompt.py"}


def _resolve_summary_targets(
    session_state, selected_models: list[str] | None
) -> list[str]:
    """Summary 只作用于 ACTIVE 模型（CLOSED 不再参与，phase1_1 §2.2）。"""
    known = list(session_state.active_models) + list(session_state.closed_models)
    if selected_models is None or not selected_models:
        return list(session_state.active_models)
    unknown = [m for m in selected_models if m not in known]
    if unknown:
        raise HTTPException(status_code=400, detail=f"未知 model_id: {unknown}")
    not_active = [m for m in selected_models if m not in session_state.active_models]
    if not_active:
        raise HTTPException(
            status_code=400, detail=f"CLOSED 模型不能被选中: {not_active}"
        )
    seen: set[str] = set()
    return [m for m in selected_models if not (m in seen or seen.add(m))]


async def _run_summary_request(session_id: str, session_state, body: "RunRoundIn"):
    """用户主动 Summary 的完整链路（MD §8/§9/§66；Plan §3.2）。

        POST /round {summary_mode: true, instruction: <可编辑 Summary Prompt>}
          → SummaryService.generate(trigger="user", prompt=instruction)
          → summaries/<model_id>/summary_NNN.md
          → 该模型 latest_conversation_summary

    硬约束（D1/D2/D3）：**不创建 round_NNN/**、不增加 current_round、
    不写 responses/、不改 state.json（Summary 不是新 Round）。
    失败按模型降级（status="failed"），不影响其它模型；也不会覆盖旧的
    成功 Summary（失败不落盘，latest 仍指向最近一次 success）。
    """
    targets = _resolve_summary_targets(session_state, body.selected_models)
    if not targets:
        raise HTTPException(status_code=400, detail="未选中任何模型，无法生成 Summary")

    instruction = (body.instruction or "").strip()
    # 空输入 → 用 §9 默认 Prompt（不把空串当成 Prompt）
    prompt_arg = instruction or None

    results: list[dict] = []
    for model_id in targets:
        inputs = _collect_summary_inputs(session_id, model_id, session_state)
        if not inputs:
            results.append({
                "model_id": model_id,
                "status": "skipped",
                "summary_id": None,
                "source_rounds": [],
                "source_valid_round": None,
                "file_path": None,
                "prompt": None,
                "content": "",
                "error_message": "no_successful_final_answer",
            })
            continue
        summary = await summaries.generate(
            session_id=session_id,
            model_id=model_id,
            history=inputs,
            trigger="user",
            prompt=prompt_arg,
        )
        d = summary.to_dict()
        results.append({
            "model_id": model_id,
            "status": d["status"],
            "summary_id": d["summary_id"],
            "session_id": session_id,
            "trigger": d["trigger"],
            "created_at": d["created_at"],
            "source_rounds": d["source_rounds"],
            "source_valid_round": d["source_valid_round"],
            "is_derived_artifact": d["is_derived_artifact"],
            "creates_round": d["creates_round"],
            "contributes_valid_round_count": d["contributes_valid_round_count"],
            "input_tokens_value": d["input_tokens"]["value"],
            "output_tokens_value": d["output_tokens"]["value"],
            "file_path": d["file_path"],
            "reused_from": d["reused_from"],
            "reused_reason": d["reused_reason"],
            "prompt": d["prompt"],
            "content": d["content"],
            "error_message": d["error_message"],
        })

    # 实际用于本次请求的 Prompt（所有模型共用同一个；仅用于审计展示）
    effective_prompt = next(
        (r["prompt"] for r in results if r["prompt"]), None
    ) or (prompt_arg or "")
    return JSONResponse(status_code=200, content={
        "summary_mode": True,
        "round_created": False,          # D1：Summary 不占用 Round 编号
        "model_ids": targets,
        "instruction": instruction,
        "prompt_used": effective_prompt,
        "results": results,
    })


@app.post("/api/session/{session_id}/summary/{model_id}", response_model=SummaryEntry)
async def generate_summary(session_id: str, model_id: str, body: SummaryIn):
    """用户主动触发 Conversation Summary（§8）。

    校验：
      - trigger 仅允许 None / "user"（peer_review 由 S3.7 内部触发）
      - Session / Model 必须存在
    """
    if body.trigger not in (None, "user"):
        raise HTTPException(
            status_code=400,
            detail=(
                "trigger 仅允许 user；peer_review 由 S3.7 PeerContextPreparator 内部触发，"
                "不得通过 API 调用（避免 §16 闭环）。"
            ),
        )
    try:
        session_state = sessions.get(session_id)
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail="Session 不存在") from exc
    if model_id not in session_state.active_models + session_state.closed_models:
        raise HTTPException(status_code=404, detail=f"model_id {model_id!r} 不在 Session 中")

    inputs = _collect_summary_inputs(session_id, model_id, session_state)
    if not inputs:
        raise HTTPException(
            status_code=400,
            detail="该模型尚无任何成功 Final Answer；无法生成 Summary",
        )
    # provider_factory 可能没真实 provider：在 _make_provider 已注册
    summary = await summaries.generate(
        session_id=session_id,
        model_id=model_id,
        history=inputs,
        trigger="user",
        prompt=body.prompt,
    )
    d = summary.to_dict()
    return SummaryEntry(**{
        "summary_id": d["summary_id"],
        "model_id": d["model_id"],
        "status": d["status"],
        "trigger": d["trigger"],
        "created_at": d["created_at"],
        "source_rounds": d["source_rounds"],
        "source_valid_round": d["source_valid_round"],
        "input_tokens_value": d["input_tokens"]["value"],
        "output_tokens_value": d["output_tokens"]["value"],
        "file_path": d["file_path"],
        "reused_from": d["reused_from"],
        "reused_reason": d["reused_reason"],
        "prompt": d["prompt"],
        "content": d["content"],
    })


@app.get("/api/session/{session_id}/summary", response_model=dict)
async def list_summaries(session_id: str):
    """列出该 Session 全部已生成 Summary 的 model_id 列表 + latest 摘要。"""
    try:
        sessions.get(session_id)  # 404 校验
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail="Session 不存在") from exc
    latest_map = summaries.latest_all_models(session_id)
    return {
        "models_with_summary": list(latest_map.keys()),
        "latest": {
            mid: {
                "summary_id": s.summary_id,
                "status": s.status,
                "trigger": s.trigger,
                "created_at": s.created_at,
                "source_valid_round": s.source_valid_round,
                "file_path": s.file_path,
            }
            for mid, s in latest_map.items() if s
        },
    }


@app.get("/api/session/{session_id}/summary/{model_id}", response_model=SummaryEntry)
async def get_latest_summary(session_id: str, model_id: str):
    """读取指定模型在该 Session 的 latest Conversation Summary。"""
    try:
        sessions.get(session_id)
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail="Session 不存在") from exc
    s = summaries.latest_for(session_id, model_id)
    if s is None:
        raise HTTPException(
            status_code=404,
            detail=f"model_id {model_id!r} 在 Session {session_id!r} 中尚无 Summary",
        )
    d = s.to_dict()
    return SummaryEntry(**{
        "summary_id": d["summary_id"],
        "model_id": d["model_id"],
        "status": d["status"],
        "trigger": d["trigger"],
        "created_at": d["created_at"],
        "source_rounds": d["source_rounds"],
        "source_valid_round": d["source_valid_round"],
        "input_tokens_value": d["input_tokens"]["value"],
        "output_tokens_value": d["output_tokens"]["value"],
        "file_path": d["file_path"],
        "reused_from": d["reused_from"],
        "reused_reason": d["reused_reason"],
        "prompt": d["prompt"],
        "content": d["content"],
    })


# ---------- S3.9：MCP Evidence Index API ----------


@app.get("/api/session/{session_id}/evidence")
async def list_evidence(session_id: str):
    """查询 Evidence Index（§46 / 用户任务书 §六）。

    每条 entry 附 trace 状态，证明 Evidence Index → Runtime 可定位：
      found / runtime_missing / runtime_corrupted / record_not_found
    本接口只读；不做任何 Fact 判定 / Ranking / Scoring（§47）。
    """
    try:
        sessions.get(session_id)  # 404 校验
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail="Session 不存在") from exc

    entries = evidence_index.load(session_id)
    out = []
    for e in entries:
        tr = evidence_index.trace_to_runtime(session_id, e)
        d = e.to_dict()
        d["trace_status"] = tr.get("status", "unknown")
        out.append(d)
    return {
        "entries": out,
        "used_count": sum(1 for e in entries if e.used_in_final_answer),
        "total": len(entries),
        # §47 明示：Evidence ≠ Fact —— 本 API 不提供 is_fact / confidence 字段
    }


# ---------- Phase 4.6 / M1+M2：Project Memory API（§49–§53 / §62）----------
#
# M2 写入边界：Candidate 创建与确认**全部为用户显式 POST**；
# 不存在自动 Candidate / 自动 Confirm / 自动重要性评分（§67）。


class CreateCandidateIn(BaseModel):
    kind: str
    source_semantics: str
    content: str
    source: str                      # round / conversation_summary / mcp_evidence / user_input
    source_refs: list[str] = []


@app.get("/api/project/{project_id}/memory/candidates")
async def list_project_memory_candidates(project_id: str):
    """列出待确认的 Candidate Memory（只读）。"""
    candidates = project_memory.list_candidates(project_id)
    return {
        "project_id": project_id,
        "candidates": [c.to_dict() for c in candidates],
        "total": len(candidates),
    }


@app.post("/api/project/{project_id}/memory/candidate", status_code=201)
async def create_project_memory_candidate(project_id: str, body: CreateCandidateIn):
    """用户显式创建 Candidate Memory（§49：历史 → Candidate → 确认 → Memory）。

    不做自动候选、不做重要性评分（§67）；content 与来源由用户给定。
    """
    try:
        candidate = project_memory.create_candidate(
            project_id,
            kind=body.kind,
            source_semantics=body.source_semantics,
            content=body.content,
            source=body.source,
            source_refs=body.source_refs,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return candidate.to_dict()


@app.post("/api/project/{project_id}/memory/candidate/{candidate_id}/confirm")
async def confirm_project_memory_candidate(project_id: str, candidate_id: str):
    """用户显式确认 → 正式 MemoryEntry 落盘（memory_NNN.md + index + 日志）。

    重复内容 → status="duplicate"，不新建；语义冲突 → 仍创建但返回
    conflicts 提示，系统不自动裁决 / 覆盖 / 删除（§51）。
    """
    try:
        result = project_memory.confirm_candidate(project_id, candidate_id)
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    return result.to_dict()


@app.get("/api/project/{project_id}/memory")
async def list_project_memory(project_id: str):
    """列出该 Project 的 Memory（只读）。

    不提供检索算法（无 embedding / vector DB / ranking，§67），
    也不做任何过滤与排序打分 —— 只按 memory_id 升序返回。
    """
    entries = project_memory.list_memories(project_id)
    return {
        "project_id": project_id,
        "entries": [e.to_dict() for e in entries],
        "total": len(entries),
        # §51：保留来源语义，本 API 不提供 is_fact / confidence 字段
    }


@app.get("/api/project/{project_id}/memory/{memory_id}")
async def get_project_memory(project_id: str, memory_id: str):
    """读取单条 Memory（只读）。不存在返回 404。"""
    entry = project_memory.get_memory(project_id, memory_id)
    if entry is None:
        raise HTTPException(status_code=404, detail="Memory 不存在")
    return entry.to_dict()


@app.get("/api/session/{session_id}/events/{round_id}")
async def round_events(session_id: str, round_id: int):
    """SSE 流（phase1_mock.md §7）。

    事件名与 payload 内的 type 同时给出，两种读法都兼容：

        event: token
        data: {"model": "qwen", "type": "token", "content": "这是"}

        event: done
        data: {"model": "qwen", "type": "done"}

        event: error
        data: {"model": "deepseek", "type": "error", "error": "..."}
    """
    bus = rounds.bus(session_id, round_id)
    if bus is None:
        raise HTTPException(status_code=404, detail="Round 不存在")
    return StreamingResponse(
        _sse_stream(bus),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",  # 禁止 nginx 缓冲
        },
    )


async def _sse_stream(bus) -> AsyncIterator[bytes]:
    queue = bus.subscribe()  # 订阅时已回放历史事件，不会漏 chunk
    while True:
        try:
            event = await asyncio.wait_for(queue.get(), timeout=HEARTBEAT_SECONDS)
        except TimeoutError:
            # 空闲期插入注释行保活。不是事件，前端 EventSource 不会触发任何回调，
            # 仅防止中间代理因长时间无数据而断开（真实 LLM 首 token 可能很慢）。
            yield b": ping\n\n"
            continue
        if event is None:  # 整轮结束哨兵
            break
        etype = event.get("type", "message")
        payload = json.dumps(event, ensure_ascii=False)
        yield f"event: {etype}\ndata: {payload}\n\n".encode("utf-8")


# ---------- 静态前端 ----------

app.mount("/", StaticFiles(directory=str(STATIC_DIR), html=True), name="static")
