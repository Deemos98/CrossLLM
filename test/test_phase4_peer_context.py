"""S3.7 Peer Context Preparation 测试（Plan §4.8 §7 §8 + MD §12-§17）。

覆盖用户任务书 5 个场景：

  A  Model B 只有 1 个 Valid Answer
     → 不发生 Summary LLM Call；Peer Context = B Final Answer
  B  Model B 有多个 Valid Answer
     → 调 SummaryService；Peer Context = B 最新 Summary
  C  B 曾收到/产生 Peer Review Result
     → 下一次给 A 的 Peer Context **不包含** B 的 Peer Review Result
  D  B 有旧 Summary 且没有新的 Valid Round
     → 复用 Summary，不重复调用 LLM
  E  B 有旧 Summary 但出现新的 Valid Round
     → 重新生成 Summary，使用新 Summary

**必须做真实链路验证**（用户任务书「不要只验证 build()」）：

    Round Manager
    → ContextSelector
    → PeerContextPreparator
    → Prompt Builder
    → 最终 Prompt

最终 Prompt 必须可观察到 Peer Context，并确认
`1-answer → Final Answer` / `>1-answer → Conversation Summary`。

**三层防御**（§8.1）：
    L1 存储层 —— Peer Context 不落盘为独立文件
    L2 构建层 —— ALLOWED_SOURCE_TYPES 白名单 + 无状态重建
    L3 日志层 —— peer_context_preparation 记录 mode + source_id
"""

from __future__ import annotations

import asyncio
import json
import os
import tempfile
from pathlib import Path

import pytest

from core.model_capability import TokenEstimate
from core.peer_context import (
    PeerContextItem,
    PeerContextPreparator,
)
from core.prompt_builder import (
    build_round_n_prompt_v3,
    render_peer_context_block,
)


# ── helpers ─────────────────────────────────────────────────────────────

class _FakeVR:
    """最小 ValidRound 替身（只含 PeerContextPreparator 用到的字段）。"""

    def __init__(self, rid: int, succ: dict[str, str], q: str = "Q?"):
        self.round_id = rid
        self.successful_answers = succ
        self.user_question = q
        self.failed_models = []
        self.is_valid = bool(succ)


class _FakeRS:
    def __init__(self, rid, participants, peer_review, candidate_ids):
        self.round_id = rid
        self.participants = participants
        self.peer_review = peer_review
        self.candidate_ids = candidate_ids


class _FakeSess:
    def __init__(self, sid):
        self.session_id = sid


CALL_LOG: list[str] = []


class _CountingProvider:
    """同步 generator stub；每次调用记录到 CALL_LOG。"""

    def __init__(self, mid):
        self.mid = mid

    def generate(self, round_id, prompt):
        CALL_LOG.append(self.mid)
        yield f"[Summary::{self.mid}]\n"
        yield f"工作状态总结（第 {len(CALL_LOG)} 次生成）\n"


def _counting_factory(mid):
    return _CountingProvider(mid)


@pytest.fixture(autouse=True)
def _clear_calls():
    CALL_LOG.clear()
    yield
    CALL_LOG.clear()


def _make_prep(tmp_path):
    from core.storage import Storage
    from core.summary_service import SummaryService

    storage = Storage(tmp_path)
    svc = SummaryService(storage, provider_factory=_counting_factory)
    return PeerContextPreparator(storage, svc), storage, svc


# ── L2 构建层：白名单 ────────────────────────────────────────────────────


def test_whitelist_blocks_peer_review_result():
    """§8.2：Peer Review Result 在数据层被拒绝，不是靠 Prompt 文案。"""
    prep, _, _ = _make_prep(tempfile.mkdtemp())
    with pytest.raises(ValueError, match="非法 Peer Context 来源"):
        prep._resolve_source("peer_review_result", "pr_001")
    with pytest.raises(ValueError, match="非法 Peer Context 来源"):
        prep._resolve_source("compaction", "compact_001")


def test_whitelist_allows_only_two_sources():
    prep, _, _ = _make_prep(tempfile.mkdtemp())
    assert prep.ALLOWED_SOURCE_TYPES == frozenset(
        {"final_answer", "conversation_summary"}
    )
    assert prep._resolve_source("final_answer", "round_019") == "final_answer"
    assert (
        prep._resolve_source("conversation_summary", "summary_003")
        == "conversation_summary"
    )


def test_whitelist_rejects_bad_source_id_prefix():
    """§42 可审计：source_id 必须是 round_NNN 或 summary_NNN。"""
    prep, _, _ = _make_prep(tempfile.mkdtemp())
    with pytest.raises(ValueError, match="前缀非法"):
        prep._resolve_source("final_answer", "bogus_001")


# ── 场景 A：1 个 Valid Answer → Final Answer，不调 LLM ──────────────────


def test_A_single_valid_answer_uses_final_answer_no_llm(tmp_path):
    """场景 A：count==1 → Final Answer；**不发生** Summary LLM Call。"""
    prep, storage, _ = _make_prep(tmp_path)
    sid = "sA"
    storage.ensure_session_dir(sid)
    pool = [_FakeVR(1, {"a": "A1", "b": "B1-THE-ONLY-ANSWER"})]
    rs = _FakeRS(2, ["a", "b"], True, {"a": "Candidate-A", "b": "Candidate-B"})
    sess = _FakeSess(sid)

    out = asyncio.run(prep.prepare(session=sess, round_state=rs, valid_pool=pool))

    # a 看到 b 的 Final Answer
    a_items = out["a"]
    assert len(a_items) == 1
    it = a_items[0]
    assert it.source_model_id == "b"
    assert it.mode == "final_answer"
    assert it.source_type == "final_answer"
    assert it.source_id == "round_001"
    assert it.content == "B1-THE-ONLY-ANSWER"
    assert it.valid_answer_count == 1
    assert it.candidate_id == "Candidate-B"
    # 关键：没有产生任何 LLM 调用
    assert CALL_LOG == [], f"count==1 不应调 LLM；实际调用了 {CALL_LOG}"


# ── 场景 B：>1 个 Valid Answer → Conversation Summary ───────────────────


def test_B_multi_valid_answer_uses_conversation_summary(tmp_path):
    """场景 B：count>1 → 调 SummaryService；Peer Context = 最新 Summary。"""
    prep, storage, _ = _make_prep(tmp_path)
    sid = "sB"
    storage.ensure_session_dir(sid)
    pool = [_FakeVR(r, {"a": f"A{r}", "b": f"B{r}"}) for r in range(1, 4)]
    rs = _FakeRS(4, ["a", "b"], True, {"a": "Candidate-A", "b": "Candidate-B"})
    sess = _FakeSess(sid)

    out = asyncio.run(prep.prepare(session=sess, round_state=rs, valid_pool=pool))

    a_items = out["a"]
    assert len(a_items) == 1
    it = a_items[0]
    assert it.source_model_id == "b"
    assert it.mode == "conversation_summary"
    assert it.source_type == "conversation_summary"
    assert it.source_id.startswith("summary_")
    assert it.valid_answer_count == 3
    assert "[Summary::b]" in it.content
    # 确实发生了 LLM 调用（a 和 b 各一次）
    assert len(CALL_LOG) == 2


# ── 场景 D：无新 Valid Round → 复用，不重复调 LLM ───────────────────────


def test_D_no_new_valid_round_reuses_summary(tmp_path):
    """场景 D：连续多次 prepare，无新 Round → 复用，LLM 调用次数不增长。"""
    prep, storage, _ = _make_prep(tmp_path)
    sid = "sD"
    storage.ensure_session_dir(sid)
    pool = [_FakeVR(r, {"a": f"A{r}", "b": f"B{r}"}) for r in range(1, 4)]
    rs = _FakeRS(4, ["a", "b"], True, {"a": "Candidate-A", "b": "Candidate-B"})
    sess = _FakeSess(sid)

    r1 = asyncio.run(prep.prepare(session=sess, round_state=rs, valid_pool=pool))
    calls_after_1 = len(CALL_LOG)
    assert calls_after_1 == 2

    r2 = asyncio.run(prep.prepare(session=sess, round_state=rs, valid_pool=pool))
    r3 = asyncio.run(prep.prepare(session=sess, round_state=rs, valid_pool=pool))

    # LLM 调用次数不增长（复用生效）
    assert len(CALL_LOG) == calls_after_1, (
        f"无新 Round 时应复用；LLM 调用从 {calls_after_1} 增长到 {len(CALL_LOG)}"
    )
    # summary_id 不变
    assert r1["a"][0].source_id == r2["a"][0].source_id == r3["a"][0].source_id


# ── 场景 E：出现新 Valid Round → 重新生成 ───────────────────────────────


def test_E_new_valid_round_regenerates_summary(tmp_path):
    """场景 E：新 Round 出现 → 重新生成，使用新 Summary。"""
    prep, storage, _ = _make_prep(tmp_path)
    sid = "sE"
    storage.ensure_session_dir(sid)
    pool = [_FakeVR(r, {"a": f"A{r}", "b": f"B{r}"}) for r in range(1, 4)]
    rs = _FakeRS(4, ["a", "b"], True, {"a": "Candidate-A", "b": "Candidate-B"})
    sess = _FakeSess(sid)

    r1 = asyncio.run(prep.prepare(session=sess, round_state=rs, valid_pool=pool))
    first_id = r1["a"][0].source_id
    calls_after_1 = len(CALL_LOG)

    # 出现新 Valid Round
    pool2 = pool + [_FakeVR(4, {"a": "A4", "b": "B4"})]
    r2 = asyncio.run(prep.prepare(session=sess, round_state=rs, valid_pool=pool2))

    # LLM 调用次数增长（重新生成）
    assert len(CALL_LOG) > calls_after_1, "有新 Round 时应重新生成 Summary"
    # 使用了新的 summary_id
    assert r2["a"][0].source_id != first_id
    assert r2["a"][0].valid_answer_count == 4


# ── 场景 C：Peer Review Result 数据层阻断 ───────────────────────────────


def test_C_peer_review_result_never_becomes_next_peer_context(tmp_path):
    """场景 C（用户任务书 §九）：三层防御。

    L1 磁盘上不存在 peer_context 持久化文件
    L2 所有产出 item 的 source_type ∈ ALLOWED_SOURCE_TYPES
    L3 peer_context_preparation 日志中 source_id 以 round_/summary_ 开头
    """
    prep, storage, _ = _make_prep(tmp_path)
    sid = "sC"
    storage.ensure_session_dir(sid)
    pool = [_FakeVR(r, {"a": f"A{r}", "b": f"B{r}"}) for r in range(1, 4)]
    rs = _FakeRS(4, ["a", "b"], True, {"a": "Candidate-A", "b": "Candidate-B"})
    sess = _FakeSess(sid)

    out = asyncio.run(prep.prepare(session=sess, round_state=rs, valid_pool=pool))

    # L2：source_type 全在白名单
    all_items = [it for items in out.values() for it in items]
    assert all_items, "应至少产出一条 Peer Context"
    for it in all_items:
        assert it.source_type in prep.ALLOWED_SOURCE_TYPES, (
            f"非法 source_type: {it.source_type}"
        )
        assert not it.source_id.startswith("peer_context"), (
            f"source_id 不应指向 peer_context: {it.source_id}"
        )

    # L1：磁盘上不存在 peer_context 文件
    pc_files = list(Path(tmp_path).rglob("*peer_context*"))
    assert pc_files == [], f"Peer Context 不应落盘；发现 {pc_files}"

    # L3：日志 source_id 前缀合法
    policy = (Path(tmp_path) / sid / "context_policy.jsonl").read_text(
        encoding="utf-8"
    )
    events = [json.loads(l) for l in policy.splitlines() if l.strip()]
    pc_events = [e for e in events if e["event"] == "peer_context_preparation"]
    assert pc_events, "应落 peer_context_preparation 日志"
    for e in pc_events:
        if e["source_id"] is None:
            continue
        assert e["source_id"].startswith(("round_", "summary_")), (
            f"source_id 前缀非法: {e['source_id']}"
        )


def test_peer_review_off_produces_no_peer_context(tmp_path):
    """§3.1：peer_review=False → prepare() 返回空 dict。"""
    prep, storage, _ = _make_prep(tmp_path)
    sid = "sOff"
    storage.ensure_session_dir(sid)
    pool = [_FakeVR(r, {"a": f"A{r}", "b": f"B{r}"}) for r in range(1, 4)]
    rs = _FakeRS(4, ["a", "b"], False, {"a": "Candidate-A", "b": "Candidate-B"})
    sess = _FakeSess(sid)

    out = asyncio.run(prep.prepare(session=sess, round_state=rs, valid_pool=pool))
    assert out == {}
    assert CALL_LOG == [], "peer_review=False 不应产生任何 LLM 调用"


def test_single_model_produces_no_peer_context(tmp_path):
    """§3.3：单模型无互评对象 → 返回空。"""
    prep, storage, _ = _make_prep(tmp_path)
    sid = "sSingle"
    storage.ensure_session_dir(sid)
    pool = [_FakeVR(1, {"a": "A1"})]
    rs = _FakeRS(2, ["a"], True, {"a": "Candidate-A"})
    sess = _FakeSess(sid)

    out = asyncio.run(prep.prepare(session=sess, round_state=rs, valid_pool=pool))
    assert out == {}


def test_target_never_sees_own_output_as_peer_context(tmp_path):
    """Peer Context 只来自**其他**模型；target 自己不出现在自己的 items 中。"""
    prep, storage, _ = _make_prep(tmp_path)
    sid = "sSelf"
    storage.ensure_session_dir(sid)
    pool = [_FakeVR(r, {"a": f"A{r}", "b": f"B{r}", "c": f"C{r}"})
            for r in range(1, 3)]
    rs = _FakeRS(3, ["a", "b", "c"], True,
                 {"a": "Candidate-A", "b": "Candidate-B", "c": "Candidate-C"})
    sess = _FakeSess(sid)

    out = asyncio.run(prep.prepare(session=sess, round_state=rs, valid_pool=pool))
    for target, items in out.items():
        sources = {it.source_model_id for it in items}
        assert target not in sources, (
            f"target={target} 的 Peer Context 不应包含自己"
        )
        # 每个 target 看到其他 2 个模型
        assert sources == set(rs.participants) - {target}


# ── 日志字段（§42）─────────────────────────────────────────────────────


def test_peer_context_preparation_log_fields(tmp_path):
    """§42：日志至少含 round_id / target_model_id / source_model_id /
    valid_answer_count / mode / source_id / status。"""
    prep, storage, _ = _make_prep(tmp_path)
    sid = "sLog"
    storage.ensure_session_dir(sid)
    pool = [_FakeVR(r, {"a": f"A{r}", "b": f"B{r}"}) for r in range(1, 4)]
    rs = _FakeRS(7, ["a", "b"], True, {"a": "Candidate-A", "b": "Candidate-B"})
    sess = _FakeSess(sid)

    asyncio.run(prep.prepare(session=sess, round_state=rs, valid_pool=pool))

    policy = (Path(tmp_path) / sid / "context_policy.jsonl").read_text(
        encoding="utf-8"
    )
    events = [json.loads(l) for l in policy.splitlines() if l.strip()]
    pc_events = [e for e in events if e["event"] == "peer_context_preparation"]
    assert len(pc_events) == 2  # a→b, b→a

    required = {
        "event", "session_id", "round_id", "target_model_id",
        "source_model_id", "valid_answer_count", "mode", "source_id",
        "status", "summary_reused", "reused_reason", "timestamp",
    }
    for e in pc_events:
        assert required <= set(e.keys()), (
            f"日志缺字段: {required - set(e.keys())}"
        )
        assert e["round_id"] == 7
        assert e["mode"] in ("final_answer", "conversation_summary")
    # mode 与 count 一致
    for e in pc_events:
        if e["valid_answer_count"] == 1:
            assert e["mode"] == "final_answer"
        else:
            assert e["mode"] == "conversation_summary"


# ── Prompt Builder：Peer Context 渲染 ───────────────────────────────────


def test_render_peer_context_block_includes_source_meta():
    """渲染块含 `[Candidate-X]\\n<content>`（S3.5 已验收格式）+ 来源元信息。"""
    items = [
        PeerContextItem(
            source_model_id="b", mode="final_answer",
            source_type="final_answer", source_id="round_001",
            content="B 的唯一回答", valid_answer_count=1,
            candidate_id="Candidate-B", created_at="2026-09-03T00:00:00Z",
        ),
    ]
    block, entries = render_peer_context_block(items)
    assert "## 其他参与者当前状态（Peer Context）" in block
    # S3.5 已验收格式：标签 + 换行 + content
    assert "[Candidate-B]\nB 的唯一回答" in block
    # §42 可审计元信息
    assert "来源：final_answer / round_001" in block
    assert len(entries) == 1
    assert entries[0].candidate_id == "Candidate-B"
    assert entries[0].is_self is False


def test_render_peer_context_block_empty():
    assert render_peer_context_block([]) == ("", [])


def test_prompt_v3_history_shows_only_self_and_peer_block_separate():
    """v3 关键语义：历史只显示 self；peer 信息走独立 Peer Context 块。"""
    from core.context_selector import HistoricalContextItem

    # 用真实感 model_id（单字母会被 anonymizer 判为身份词，见不变量 A3）
    MID_A, MID_B = "qwen-plus", "gpt-4o-mini"
    hist_item = HistoricalContextItem(
        round_id=1,
        user_question="Q1?",
        answers=[
            (MID_A, "A1-self"),
            (MID_B, "B1-peer-should-not-be-in-history"),
        ],
        source="recent_raw",
        token_estimate=TokenEstimate(10, "crossllm_estimator", "estimated"),
    )
    peer_items = [
        PeerContextItem(
            source_model_id=MID_B, mode="final_answer",
            source_type="final_answer", source_id="round_001",
            content="B1-PEER-CONTENT", valid_answer_count=1,
            candidate_id="Candidate-B", created_at="2026-09-03T00:00:00Z",
        ),
    ]
    from core.state import ModelParticipant

    parts = [
        ModelParticipant(id=MID_A, display_name="Qwen Plus", provider="mock",
                         model=MID_A, enabled=True),
        ModelParticipant(id=MID_B, display_name="GPT-4o mini", provider="mock",
                         model=MID_B, enabled=True),
    ]
    prompt, warns = build_round_n_prompt_v3(
        original_question="原始问题",
        user_instruction="本轮指令",
        historical_items=[(1, hist_item)],
        peer_context_items=peer_items,
        self_model_id=MID_A,
        candidate_ids={MID_A: "Candidate-A", MID_B: "Candidate-B"},
        peer_review=True,
        participants=parts,
    )
    # 历史区块只含 self
    hist_start = prompt.find("历史上下文")
    peer_start = prompt.find("## 其他参与者当前状态（Peer Context）")
    assert hist_start >= 0 and peer_start > hist_start
    history_section = prompt[hist_start:peer_start]
    assert "A1-self" in history_section
    assert "B1-peer-should-not-be-in-history" not in history_section
    # Peer Context 区块含 peer content
    peer_section = prompt[peer_start:]
    assert "B1-PEER-CONTENT" in peer_section
    assert "来源：final_answer / round_001" in peer_section


def test_prompt_v3_peer_review_off_excludes_peer_block():
    """peer_review=False → 即便传入 items 也不渲染 Peer Context 块。"""
    from core.context_selector import HistoricalContextItem
    from core.state import ModelParticipant

    MID_A, MID_B = "qwen-plus", "gpt-4o-mini"
    hist_item = HistoricalContextItem(
        round_id=1, user_question="Q1?",
        answers=[(MID_A, "A1")], source="recent_raw",
        token_estimate=TokenEstimate(10, "crossllm_estimator", "estimated"),
    )
    peer_items = [
        PeerContextItem(
            source_model_id=MID_B, mode="final_answer",
            source_type="final_answer", source_id="round_001",
            content="SECRET-PEER", valid_answer_count=1,
            candidate_id="Candidate-B", created_at="2026-09-03T00:00:00Z",
        ),
    ]
    parts = [
        ModelParticipant(id=MID_A, display_name="Qwen Plus", provider="mock",
                         model=MID_A, enabled=True),
        ModelParticipant(id=MID_B, display_name="GPT-4o mini", provider="mock",
                         model=MID_B, enabled=True),
    ]
    prompt, _ = build_round_n_prompt_v3(
        original_question="Q", user_instruction="I",
        historical_items=[(1, hist_item)],
        peer_context_items=peer_items,
        self_model_id=MID_A,
        candidate_ids={MID_A: "Candidate-A", MID_B: "Candidate-B"},
        peer_review=False,
        participants=parts,
    )
    assert "SECRET-PEER" not in prompt
    assert "## 其他参与者当前状态（Peer Context）" not in prompt


# ── 真链路：RoundManager → ContextSelector → Preparator → Prompt ────────


def _run_real_rounds(n_rounds: int, peer_review: bool):
    """真实链路：SessionManager + RoundManager + MockProvider 跑 N 轮。"""
    os.environ.setdefault("CROSSLLM_MOCK_DELAY_SCALE", "0")
    from core.model_manager import ModelRegistry
    from core.round_manager import RoundManager
    from core.session_manager import SessionManager
    from core.storage import Storage
    from providers.mock_provider import MockProvider

    with tempfile.TemporaryDirectory() as td:
        storage = Storage(Path(td) / "s")
        registry = ModelRegistry()
        rounds = RoundManager(
            storage, registry, lambda m: MockProvider(m, delay_scale=0.0)
        )
        sessions = SessionManager(storage, registry)
        sess = sessions.create("OCR 多模态架构怎样设计?")

        async def _drive():
            nonlocal sess
            for i in range(n_rounds):
                instr = ("请回答。" if i == 0
                         else f"第{i + 1}轮：请评价其他参与者。")
                rs, bus = rounds.prepare_round(
                    sess, instr, peer_review=peer_review
                )
                await rounds.execute_round(sess.session_id, rs, bus)
                sess = sessions.get(sess.session_id)

        asyncio.run(_drive())

        # 读最后一轮落盘的 prompt
        pdir = storage.round_dir(sess.session_id, n_rounds)
        prompts = {
            pf.name.replace(".prompt.md", ""): pf.read_text(encoding="utf-8")
            for pf in sorted(pdir.glob("*.prompt.md"))
        }
        policy_path = (
            storage.session_dir(sess.session_id) / "context_policy.jsonl"
        )
        events = []
        if policy_path.exists():
            events = [
                json.loads(l)
                for l in policy_path.read_text(encoding="utf-8").splitlines()
                if l.strip()
            ]
        return prompts, events, storage


def test_real_chain_1_answer_uses_final_answer_in_prompt():
    """真链路：2 轮（B 仅有 1 个 Valid Answer）→ 最终 Prompt 用 Final Answer。"""
    prompts, events, _ = _run_real_rounds(2, peer_review=True)
    assert prompts, "应至少落盘一个 prompt"
    body = next(iter(prompts.values()))
    assert "## 其他参与者当前状态（Peer Context）" in body
    assert "来源：final_answer / round_001" in body
    assert "来源：conversation_summary" not in body

    # 日志必须有对应 peer_context_preparation 记录
    pc = [e for e in events if e["event"] == "peer_context_preparation"]
    assert pc, "应落 peer_context_preparation 日志"
    assert all(e["mode"] == "final_answer" for e in pc if e["source_id"])
    assert all(e["valid_answer_count"] == 1 for e in pc if e["source_id"])


def test_real_chain_multi_answer_uses_summary_in_prompt():
    """真链路：4 轮（B 有多个 Valid Answer）→ 最终 Prompt 用 Conversation Summary。"""
    prompts, events, _ = _run_real_rounds(4, peer_review=True)
    assert prompts
    body = next(iter(prompts.values()))
    assert "## 其他参与者当前状态（Peer Context）" in body
    assert "来源：conversation_summary / summary_" in body
    assert "来源：final_answer" not in body

    pc = [e for e in events if e["event"] == "peer_context_preparation"]
    multi = [e for e in pc if e["valid_answer_count"] > 1]
    assert multi, "多有效回答应产生 conversation_summary 分流"
    assert all(e["mode"] == "conversation_summary" for e in multi)


def test_real_chain_peer_review_off_has_no_peer_block():
    """真链路：peer_review=False → 最终 Prompt 无 Peer Context 块。"""
    prompts, events, _ = _run_real_rounds(3, peer_review=False)
    assert prompts
    for body in prompts.values():
        assert "## 其他参与者当前状态（Peer Context）" not in body


def test_real_chain_summary_reuse_across_rounds():
    """真链路：多轮跑完后，同一 model 的 Summary 在 valid_round 未变时复用。

    判据：peer_context_preparation 日志中出现 summary_reused=True 的记录。
    """
    prompts, events, _ = _run_real_rounds(5, peer_review=True)
    pc = [e for e in events if e["event"] == "peer_context_preparation"]
    reused = [e for e in pc if e.get("summary_reused") is True]
    # 5 轮中至少有一轮复用了 Summary（同一 source_valid_round 连续出现）
    assert reused, (
        "多轮跑完后应至少出现一次 Summary 复用；"
        f"实际事件: {[(e['round_id'], e['summary_reused']) for e in pc]}"
    )


# ── 守门 ────────────────────────────────────────────────────────────────


def test_guard_peer_context_not_persisted_in_round_dir():
    """L1：round_NNN/ 下不存在 peer_context 文件。"""
    prompts, events, storage = _run_real_rounds(3, peer_review=True)
    # storage 是临时目录内的，直接用 session_dir 扫
    for p in storage.root.rglob("*peer_context*"):
        pytest.fail(f"发现 peer_context 持久化文件: {p}")
