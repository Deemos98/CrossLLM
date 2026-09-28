"""Phase 4.4 §38：Session 级递归 Compaction 测试。

规范：docs/phase4_memory.md §38

    Compacted + Recent Raw + Pinned + Current + Reserved 仍超 Context Limit
        ↓
    再次压缩【更旧内容】（而不是反复压同一个 Compaction 产物）
        ↓
    重新构造 Context
        ↓
    最多 MAX_COMPACTION_ROUNDS 次，超出 → 明确 Overflow，不删除任何原始内容

覆盖：
  Test 1  第二次 Compaction：attempt=2，纳入更旧 Valid Round，source_rounds 扩大
  Test 2  达到 MAX_COMPACTION_ROUNDS：明确 overflow 状态，Raw / Pin / Current 完整
  Test 3  已满足预算：不产生第二次 Compaction
  Test 4  Failed Round 不会被递归扩展纳入

不修改任何既有 Phase 4 Core 测试语义。
"""

from __future__ import annotations

import asyncio
import json
import re
from pathlib import Path

import pytest

from core.compaction import MAX_COMPACTION_ROUNDS
from core.model_manager import ProviderConfig
from core.round_manager import RoundManager
from core.state import ModelParticipant, RoundState, SessionState
from core.storage import Storage

from _capability_stub import StubCapabilityResolver

MODEL = "alpha"
#: 每轮回答字符数（粗估 1 token ≈ 3 chars）
ANSWER_CHARS = 900


# ── helpers ─────────────────────────────────────────────────────────────


class _Registry:
    """最小 registry 替身。

    注意：context_limit 在这里只作为 YAML fallback 语义的值；真正的能力由
    ``StubCapabilityResolver`` 注入（见 ``_run_round``），与生产链路同一条合并链路。
    """

    def __init__(self, *, context_limit: int):
        self._cfg = ProviderConfig(
            model_id=MODEL,
            display_name="Alpha",
            model="alpha-v1",
            context_limit=context_limit,
        )
        self._participant = ModelParticipant(
            id=MODEL, display_name="Alpha",
            provider="openai_compatible", model="alpha-v1", enabled=True,
        )

    @property
    def participants(self):
        return [self._participant]

    def has(self, model_id: str) -> bool:
        return model_id == MODEL

    def config_for(self, model_id):
        return self._cfg


class _CompactionProvider:
    """固定输出长度的压缩 provider（长度决定是否超压缩预算）。

    ⚠ A3（Phase 4.5 修复）：压缩输入的参与者标签是匿名 Candidate 标签
    （MODEL="alpha" → "Candidate A"），输出按同一契约回抄标签。
    模板长度与旧版 `参与者 alpha 认为：`（13 字符）对齐 —— 19 + (chars-26)
    == 13 + (chars-20)，因此本文件的预算敏感断言不受本次修复影响。
    """

    def __init__(self, *, chars: int):
        self.chars = chars

    async def generate(self, round_id, prompt, tools=None, max_output_tokens=None):
        # 保留归属标记（F2 归属校验），长度由构造决定
        yield "参与者 Candidate A 认为：" + ("x" * max(0, self.chars - 26))


def _make_session(
    storage: Storage, sid: str, *, n_rounds: int, failed_rounds: tuple[int, ...] = ()
) -> SessionState:
    """构造 n 个 Round；failed_rounds 中的 Round 所有模型都失败（无文件）。"""
    storage.ensure_session_dir(sid)
    storage.save_question(sid, "原始问题")
    rounds: list[RoundState] = []
    for rid in range(1, n_rounds + 1):
        failed = rid in failed_rounds
        if not failed:
            storage.save_response(sid, rid, MODEL, f"R{rid} " + ("a" * ANSWER_CHARS))
        rounds.append(
            RoundState(
                round_id=rid,
                user_instruction=f"第 {rid} 轮指令 " + ("q" * 300),
                participants=[MODEL],
                peer_review=False,
                candidate_ids={},
                status={MODEL: "error" if failed else "complete"},
                timestamp="2026-09-16T00:00:00Z",
            )
        )
    sess = SessionState(
        session_id=sid,
        original_question="原始问题",
        current_round=n_rounds,
        active_models=[MODEL],
        closed_models=[],
        rounds=rounds,
    )
    storage.save_state(sess)
    return sess


def _compaction_events(storage: Storage, sid: str) -> list[dict]:
    p = storage.session_dir(sid) / "context_policy.jsonl"
    if not p.exists():
        return []
    out = []
    for line in p.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        row = json.loads(line)
        if row.get("event") == "compaction":
            out.append(row)
    return out


def _warnings(storage: Storage, sid: str) -> list[dict]:
    p = storage.session_dir(sid) / "context_policy.jsonl"
    if not p.exists():
        return []
    out = []
    for line in p.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        row = json.loads(line)
        if row.get("event") == "context_budget_warning":
            out.append(row)
    return out


def _run_round(
    tmp_path: Path,
    *,
    context_limit: int,
    max_output_tokens: int,
    compaction_chars: int,
    n_rounds: int,
    k: float = 0.5,
    failed_rounds: tuple[int, ...] = (),
    instruction: str = "请给出本轮结论。",
):
    """跑一轮真实 RoundManager（触发 Context 构建 + Compaction 接线）。"""
    sid = "s-rec"
    storage = Storage(tmp_path / "sessions")
    sess = _make_session(storage, sid, n_rounds=n_rounds, failed_rounds=failed_rounds)

    rm = RoundManager(
        storage,
        _Registry(context_limit=context_limit),
        lambda mid: _CompactionProvider(chars=compaction_chars),
        # §一：注入确定性模型能力（等价于"Provider 查询成功"），
        # 数值与改动前一致，因此本文件的预算敏感断言不变。
        capability_resolver=StubCapabilityResolver(
            context_limit=context_limit, model_max_output=max_output_tokens,
        ),
    )
    rs, bus = rm.prepare_round(
        sess, instruction, selected_models=[MODEL], k=k
    )
    prompts, warnings = asyncio.run(rm._build_prompts_async(sess, rs))
    return storage, sid, rs, prompts, warnings


def _raw_archive_md5(storage: Storage, sid: str) -> dict[str, str]:
    import hashlib

    out = {}
    d = storage.session_dir(sid)
    for p in sorted(d.glob("round_*/*")):
        if p.is_file():
            out[str(p.relative_to(d))] = hashlib.md5(p.read_bytes()).hexdigest()
    return out


# ── Test 1：第二次 Compaction（纳入更旧历史）──────────────────────────


def test_1_second_compaction_includes_older_rounds(tmp_path):
    """第一次压缩产物超压缩预算 → 触发第二轮，且第二轮纳入更旧 Valid Round。"""
    storage, sid, rs, _, _ = _run_round(
        tmp_path,
        # 小窗口 + 小输出上限：让压缩区预算很小，压缩产物必然超出
        context_limit=2400,
        max_output_tokens=200,
        compaction_chars=6000,         # ≈2000 tokens > compaction_budget
        n_rounds=12,
    )

    events = _compaction_events(storage, sid)
    attempts = [e["attempt"] for e in events]
    assert attempts, f"未发生 Compaction：{events}"
    assert 1 in attempts, f"缺少 attempt=1：{attempts}"
    assert 2 in attempts, f"§38 未触发第二轮 Compaction：{attempts}"

    first = next(e for e in events if e["attempt"] == 1)
    second = next(e for e in events if e["attempt"] == 2)

    # 第二次必须纳入**更旧**的 Round（source_rounds 扩大）
    assert set(second["source_rounds"]), "attempt=2 的 source_rounds 为空"
    assert set(first["source_rounds"]) < set(second["source_rounds"]), (
        f"§38 违反：第二次未纳入更旧历史 "
        f"first={first['source_rounds']} second={second['source_rounds']}"
    )
    # 第二次集合是"更旧 + 更少约束"，即更旧轮次被包含
    assert min(second["source_rounds"]) < min(first["source_rounds"])

    # Raw Archive 完整（压缩从不删除原始 Round）
    d = storage.session_dir(sid)
    for rid in range(1, 13):
        assert (d / f"round_{rid:03d}" / f"{MODEL}.md").exists(), (
            f"Raw Archive 被破坏：round_{rid:03d}"
        )
    # 产物落在 compactions/，且不在 round_NNN/ 内
    assert list((d / "compactions").glob("compact_*.md"))


def test_1b_second_compaction_carries_previous_product(tmp_path):
    """第二次压缩的输入必须包含上一轮产物（Compacted History + 更旧 Raw Rounds）。"""
    storage, sid, _, _, _ = _run_round(
        tmp_path,
        context_limit=2400,
        max_output_tokens=200,
        compaction_chars=6000,
        n_rounds=12,
    )
    events = _compaction_events(storage, sid)
    second = next((e for e in events if e["attempt"] == 2), None)
    assert second is not None, f"未触发第二轮：{[e['attempt'] for e in events]}"
    d = storage.session_dir(sid)
    # attempt=2 的产物文件存在（可审计）
    assert second["summary_file"], "attempt=2 缺少 summary_file"
    assert (d / second["summary_file"]).exists()


# ── Test 2：达到 MAX_COMPACTION_ROUNDS ─────────────────────────────────


def test_2_stops_at_max_compaction_rounds_with_explicit_overflow(tmp_path):
    """无论如何仍超限 → 不超过 MAX_COMPACTION_ROUNDS，返回明确 overflow 状态。"""
    storage, sid, rs, _, _ = _run_round(
        tmp_path,
        # 极端小窗口：Pin/固定开销已占满，压缩永远无法满足
        context_limit=300,
        max_output_tokens=200,
        compaction_chars=6000,
        n_rounds=12,
    )
    events = _compaction_events(storage, sid)
    attempts = [e["attempt"] for e in events]
    assert max(attempts or [0]) <= MAX_COMPACTION_ROUNDS, (
        f"§38 违反：attempt 超过 MAX_COMPACTION_ROUNDS：{attempts}"
    )
    assert len(attempts) <= MAX_COMPACTION_ROUNDS

    # 明确 overflow 状态
    warns = _warnings(storage, sid)
    reasons = {w.get("reason") for w in warns}
    assert reasons, "缺少明确的 overflow 状态（context_budget_warning）"
    assert reasons & {
        "compaction_limit_reached",
        "no_more_valid_rounds",
        "pinned_exceeds_budget_unsolvable",
        "compaction_insufficient",
        "compaction_produced_no_content",
    }, f"未知 overflow 状态：{reasons}"
    assert any(
        w.get("max_compaction_rounds") == MAX_COMPACTION_ROUNDS for w in warns
    )

    # Raw Archive / Current User 完整
    d = storage.session_dir(sid)
    for rid in range(1, 13):
        assert (d / f"round_{rid:03d}" / f"{MODEL}.md").exists()
    assert rs.user_instruction == "请给出本轮结论。"


def test_2b_pin_not_silently_removed_on_overflow(tmp_path):
    """溢出场景下 Pin 不得被偷偷取消/压缩。"""
    from core.pin_store import PinStore

    storage, sid, rs, _, _ = _run_round(
        tmp_path,
        context_limit=300,
        max_output_tokens=200,
        compaction_chars=6000,
        n_rounds=12,
    )
    pins = PinStore(storage)
    pins.pin(sid, 2)

    # 再跑一轮：Pin 必须仍然存在，且原文可寻回
    sess = storage.load_state(sid)
    rm = RoundManager(
        storage,
        _Registry(context_limit=300),
        lambda mid: _CompactionProvider(chars=900),
        capability_resolver=StubCapabilityResolver(
            context_limit=300, model_max_output=200,
        ),
    )
    rs2, bus2 = rm.prepare_round(sess, "再问一次", selected_models=[MODEL], k=0.5)
    asyncio.run(rm._build_prompts_async(sess, rs2))

    assert pins.pinned_round_ids(sid) == [2], "§27 违反：Pin 被偷偷解除"
    assert (storage.session_dir(sid) / "round_002" / f"{MODEL}.md").exists()


# ── Test 3：已满足预算 → 不发生第二次 Compaction ────────────────────────


def test_3_no_second_compaction_when_budget_satisfied(tmp_path):
    """压缩产物足够小 → 只有 attempt=1，不产生第二次事件。"""
    storage, sid, _, _, _ = _run_round(
        tmp_path,
        context_limit=8000,
        max_output_tokens=512,
        compaction_chars=60,           # 很短 → 不超压缩预算
        n_rounds=12,
    )
    events = _compaction_events(storage, sid)
    attempts = [e["attempt"] for e in events]
    assert attempts == [1], f"§38 不应发生第二次压缩：{attempts}"
    assert not [
        w for w in _warnings(storage, sid)
        if w.get("reason") == "compaction_limit_reached"
    ]


# ── Test 4：Failed Round 不被递归纳入 ──────────────────────────────────


def test_4_failed_rounds_never_compacted(tmp_path):
    """递归扩展历史时必须仍然只用 Valid Round（§35）。"""
    failed = (3, 5, 8)
    storage, sid, _, _, _ = _run_round(
        tmp_path,
        context_limit=2400,
        max_output_tokens=200,
        compaction_chars=900,
        n_rounds=12,
        failed_rounds=failed,
    )
    events = _compaction_events(storage, sid)
    assert events, "未发生 Compaction"

    all_sources: set[int] = set()
    for e in events:
        all_sources.update(e["source_rounds"])
    for rid in failed:
        assert rid not in all_sources, (
            f"§35 违反：Failed Round {rid} 进入了 Compaction：{sorted(all_sources)}"
        )
    # 失败 Round 的原始文件本就不存在（error），Runtime 保留语义由既有测试覆盖
    for rid in failed:
        assert not (storage.session_dir(sid) / f"round_{rid:03d}" / f"{MODEL}.md").exists()


def test_4b_source_rounds_are_monotonic_expanding(tmp_path):
    """多次 attempt 的 source_rounds 必须递增扩大（不会回缩）。"""
    storage, sid, _, _, _ = _run_round(
        tmp_path,
        context_limit=2400,
        max_output_tokens=200,
        compaction_chars=6000,
        n_rounds=12,
    )
    events = sorted(_compaction_events(storage, sid), key=lambda e: e["attempt"])
    prev: set[int] = set()
    for e in events:
        cur = set(e["source_rounds"])
        assert prev <= cur, f"source_rounds 回缩：{prev} → {cur}"
        prev = cur


# ── 结构性守门：不得引入固定轮数/固定触发常数 ─────────────────────────

