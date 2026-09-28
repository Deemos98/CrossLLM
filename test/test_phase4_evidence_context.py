"""Phase 4.5：MCP Evidence → Context 测试（docs/phase4_memory.md §4.5.1 - §4.5.12 / §69）。

严格语义分离（§4.5.1 / §4.5.9 / §4.5.10 / §4.5.12）：

    MCP Runtime ≠ MCP Tool Result ≠ MCP Evidence ≠ Fact
    Evidence ≠ Peer Context
    Evidence ≠ Project Memory
    Evidence 不替代 Compaction

覆盖 §69「Phase 4.5 / MCP Evidence」19 条验收项，覆盖不到的用反向证明补齐：

  1  MCP Runtime 保留                       → test_69_runtime_and_index_untouched_by_selection
  2  区分"调用过"/"Final Answer 使用过"        → test_69_used_false_is_not_injected
  3  Evidence Index 可以定位 Runtime          → test_69_traceable_to_session_round_runtime_record
  4  MCP Result 不自动成为 Fact                → test_69_evidence_is_never_a_fact
  5  Evidence Index 有日志                     → test_69_skip_is_logged_for_budget
  6  Evidence 只来自已完成的历史 Round          → test_69_only_completed_historical_rounds
  7  第一版只注入当前模型自己的历史 Evidence     → test_69_a1_only_own_evidence
  8  Raw History 仍在无损 Context 时不重复注入   → test_69_dedup_when_round_still_lossless
  9  Pin 不需要额外注入 Evidence                → test_69_pinned_round_needs_no_evidence
 10  Evidence 第一版只以 Reference 形式进入      → test_69_only_reference_enters_prompt
 11  Runtime 原始 Tool Result 不复制进 Prompt   → test_69_runtime_result_body_not_copied
 12  Evidence Reference 与 Compacted 共用预算    → test_69_shares_compaction_budget
 13  Evidence 不建立独立 Budget                 → test_69_no_separate_evidence_budget
 14  Budget 不足可跳过，不删 Runtime / Index     → test_69_skip_is_logged_for_budget
 15  Evidence 不自动进入 Peer Context           → test_69_evidence_never_enters_peer_context
 16  Evidence 不自动进入 Project Memory         → test_69_real_chain_raw_archive_and_project_memory_untouched
 17  Evidence 模型可见内容不暴露真实 model_id     → test_69_source_model_id_never_visible
 18  Evidence 可以追溯到 Session/Round/Runtime   → test_69_traceable_to_session_round_runtime_record
 19  Evidence 进入 Prompt 前通过身份泄漏检查      → test_69_skeleton_hard_check_covers_evidence_block +
                                                    test_69_leaky_reference_is_skipped_for_identity +
                                                    test_69_prompt_last_line_drops_leaky_reference
     （§4.5.11 语义：命中真实身份词元的 reference **整条剔除** + 记
       mcp_evidence_skipped_for_identity；不是软扫描告警）

真链路（§64 最终历史选择算法 → Prompt）：
    test_69_real_chain_injects_own_evidence_only
    test_69_real_chain_leaky_reference_is_never_injected
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import tempfile
from pathlib import Path

import pytest

from core.evidence_index import (
    EVIDENCE_BLOCK_HEADER,
    EVIDENCE_BLOCK_NOTE,
    RUNTIME_LOG_NAME,
    EvidenceIndexService,
    McpEvidenceEntry,
    render_evidence_block,
)

from _capability_stub import StubCapabilityResolver

QUOTED = "GitHub Issue #456 描述了 OCR 长尾延迟问题，影响 P99 指标"
#: Runtime 原始正文（远超 reference 的 60 字符上限）—— 绝不允许进 Prompt
RAW_TAIL = "工具返回的完整原始正文。" * 400


# ── helpers ─────────────────────────────────────────────────────────────


def _entry(
    *,
    round_id: int = 1,
    mcp: str = "github",
    tool: str = "search_issues",
    reference: str = QUOTED,
    used: bool = True,
    model: str = "qwen",
    call_id: str = "call_A",
) -> McpEvidenceEntry:
    return McpEvidenceEntry(
        round_id=round_id, mcp=mcp, tool=tool, runtime_record_id=call_id,
        reference=reference, used_in_final_answer=used,
        created_at=f"2026-09-16T00:00:{round_id % 60:02d}Z",
        source_model_id=model,
    )


def _write_evidence(storage, sid: str, entries: list[McpEvidenceEntry]) -> Path:
    d = storage.session_dir(sid) / "evidence"
    d.mkdir(parents=True, exist_ok=True)
    p = d / "mcp_evidence.jsonl"
    with p.open("w", encoding="utf-8") as fh:
        for e in entries:
            fh.write(json.dumps(e.to_dict(), ensure_ascii=False) + "\n")
    return p


def _write_runtime(storage, sid: str, round_id: int, records: list[dict]) -> Path:
    rdir = storage.round_dir(sid, round_id)
    rdir.mkdir(parents=True, exist_ok=True)
    p = rdir / RUNTIME_LOG_NAME
    with p.open("w", encoding="utf-8") as fh:
        for r in records:
            fh.write(json.dumps(r, ensure_ascii=False) + "\n")
    return p


def _rec(*, call_id: str, model_id: str, mcp: str, tool: str, preview: str) -> dict:
    return {
        "ts": "2026-09-16T00:00:00.000",
        "session_id": "s", "round_id": 1, "model_id": model_id,
        "tool_call": {"id": call_id, "name": tool},
        "mcp": mcp, "tool": tool, "arguments": {"q": "ocr"},
        "ok": True, "result_chars": len(preview), "result_preview": preview,
        "error_type": None, "error_message": None,
    }


def _sha(p: Path) -> str:
    return hashlib.md5(p.read_bytes()).hexdigest()


def _svc(tmp_path, sid: str = "s-ev"):
    from core.storage import Storage

    storage = Storage(tmp_path)
    storage.session_dir(sid).mkdir(parents=True, exist_ok=True)
    return EvidenceIndexService(storage), storage, sid


def _events(storage, sid: str) -> list[dict]:
    p = storage.session_dir(sid) / "context_policy.jsonl"
    if not p.exists():
        return []
    return [json.loads(l) for l in p.read_text(encoding="utf-8").splitlines()
            if l.strip()]


def _participants(ids=("qwen", "deepseek", "gpt")):
    from core.state import ModelParticipant

    return [
        ModelParticipant(id=m, display_name=m.upper(), provider="mock",
                         model=f"mock-{m}", enabled=True)
        for m in ids
    ]


# ── §4.5.3 A1：只注入当前模型自己的历史 Evidence ─────────────────────────


def test_69_a1_only_own_evidence(tmp_path):
    """§4.5.3：只读当前生成模型自己的历史 Evidence，不碰其他模型。"""
    svc, storage, sid = _svc(tmp_path)
    _write_evidence(storage, sid, [
        _entry(round_id=1, model="qwen", reference="第一轮的外部检索结果"),
        _entry(round_id=2, model="gpt", reference="gpt 的证据",
               call_id="call_g", mcp="openalex", tool="search"),
        _entry(round_id=3, model="deepseek", reference="deepseek 的证据",
               call_id="call_d", mcp="fetch", tool="get"),
    ])

    sel = svc.select_for_context(
        session_id=sid, model_id="qwen", participants=_participants(),
        current_round_id=9,
        lossless_round_ids=[], budget_tokens=100000,
    )
    assert [e.source_model_id for e in sel.entries] == ["qwen"]
    assert "第一轮的外部检索结果" in render_evidence_block(sel.entries)
    assert "gpt 的证据" not in render_evidence_block(sel.entries)
    assert "deepseek 的证据" not in render_evidence_block(sel.entries)


def test_69_used_false_is_not_injected(tmp_path):
    """§4.5.2 / §69-2：区分"调用过"与"Final Answer 使用过"。"""
    svc, storage, sid = _svc(tmp_path)
    _write_evidence(storage, sid, [
        _entry(round_id=1, used=True, reference="被使用的证据"),
        _entry(round_id=2, used=False, reference="只是调用过、未被引用",
               call_id="call_B"),
    ])
    sel = svc.select_for_context(
        session_id=sid, model_id="qwen", participants=_participants(),
        current_round_id=9,
        lossless_round_ids=[], budget_tokens=100000,
    )
    assert [e.reference for e in sel.entries] == ["被使用的证据"]
    assert all(e.used_in_final_answer for e in sel.entries)


def test_69_only_completed_historical_rounds(tmp_path):
    """§4.5.4：只允许已完成的历史 Round（当前轮不得反向注入自身）。"""
    svc, storage, sid = _svc(tmp_path)
    _write_evidence(storage, sid, [
        _entry(round_id=5, reference="历史 Round 的 Evidence"),
        _entry(round_id=6, reference="当前 Round 的 Evidence",
               call_id="call_C"),
    ])
    sel = svc.select_for_context(
        session_id=sid, model_id="qwen", participants=_participants(),
        current_round_id=6,
        lossless_round_ids=[], budget_tokens=100000,
    )
    assert [e.round_id for e in sel.entries] == [5]


def test_69_dedup_when_round_still_lossless(tmp_path):
    """§4.5.5：Raw History 仍在无损 Context → 不重复注入对应 Evidence。"""
    svc, storage, sid = _svc(tmp_path)
    _write_evidence(storage, sid, [
        _entry(round_id=1, reference="仍在无损区的轮次"),
        _entry(round_id=2, reference="已离开无损区的轮次", call_id="call_B"),
    ])
    sel = svc.select_for_context(
        session_id=sid, model_id="qwen", participants=_participants(),
        current_round_id=9,
        lossless_round_ids=[1], budget_tokens=100000,
    )
    assert [e.round_id for e in sel.entries] == [2]
    assert sel.deduped_round_ids == [1]


def test_69_pinned_round_needs_no_evidence(tmp_path):
    """§4.5.5：Pinned Raw History 已受无损保护 → 不需要额外注入 Evidence。"""
    svc, storage, sid = _svc(tmp_path)
    _write_evidence(storage, sid, [
        _entry(round_id=1, reference="Pin 轮次的证据"),
        _entry(round_id=4, reference="普通旧轮次的证据", call_id="call_B"),
    ])
    # lossless_round_ids 由 ContextSelector 的 pinned ∪ recent_raw 给出
    sel = svc.select_for_context(
        session_id=sid, model_id="qwen", participants=_participants(),
        current_round_id=9,
        lossless_round_ids=[1], budget_tokens=100000,
    )
    assert [e.round_id for e in sel.entries] == [4]
    assert 1 in sel.deduped_round_ids


# ── §4.5.7 / §4.5.8：预算共用与跳过 ─────────────────────────────────────


def test_69_shares_compaction_budget(tmp_path):
    """§4.5.7：Evidence Reference 与 Compacted History 共用 CompactionBudget。

    预算只够装下较新的那条 → 旧的那条被跳过，且占用不超过预算。
    """
    svc, storage, sid = _svc(tmp_path)
    _write_evidence(storage, sid, [
        _entry(round_id=1, reference="旧证据" + "甲" * 60, call_id="call_1"),
        _entry(round_id=7, reference="新证据" + "乙" * 60, call_id="call_7"),
    ])
    one = render_evidence_block([_entry(round_id=7, reference="新证据" + "乙" * 60)])
    budget = -(-len(one) // 3)  # 恰好只够一条

    sel = svc.select_for_context(
        session_id=sid, model_id="qwen", participants=_participants(),
        current_round_id=9,
        lossless_round_ids=[], budget_tokens=budget,
    )
    assert [e.round_id for e in sel.entries] == [7]
    assert sel.used_tokens <= budget
    assert [e.round_id for e in sel.skipped] == [1]


def test_69_no_separate_evidence_budget(tmp_path):
    """§4.5.7 / §69-13：不得新增独立 Evidence Budget，也不得改 k 的定义。"""
    from core.context_budget import ContextBudget, compute_budget, make_fixed_input_tokens
    from core.model_capability import (
        ProviderModelMetadata,
        TokenEstimate,
        _iso_now,
    )

    # 反向证明：ContextBudget 里没有任何 evidence 字段
    assert not [f for f in ContextBudget.__dataclass_fields__ if "evidence" in f]

    b = compute_budget(
        metadata=ProviderModelMetadata(
            model_slug="m",
            context_limit=TokenEstimate(8192, "config_static", "estimated"),
            model_max_output=TokenEstimate(2048, "openrouter_metadata", "exact"),
            fetched_at=_iso_now(),
            fetch_status="ok",
        ),
        request_max_output=TokenEstimate(1024, "config_static", "estimated"),
        fixed=make_fixed_input_tokens(system_text="S", current_user_text="U"),
        k=0.5,
    )
    # 预算公式仍是 history = limit - fixed - reserved；k 只做二分
    assert b.history_budget.value is not None
    assert b.lossless_budget.value + b.compaction_budget.value == b.history_budget.value


def test_69_skip_is_logged_for_budget(tmp_path):
    """§4.5.8 / §69-1 / §69-14：装不下 → 跳过并记 mcp_evidence_skipped_for_budget。

    同时反向证明 Runtime / Evidence Index 均未被修改或删除。
    """
    svc, storage, sid = _svc(tmp_path)
    ev_path = _write_evidence(storage, sid, [
        _entry(round_id=1, reference="很长的证据" + "丙" * 500),
    ])
    rt_path = _write_runtime(storage, sid, 1, [
        _rec(call_id="call_A", model_id="qwen", mcp="github",
             tool="search_issues", preview=QUOTED),
    ])
    ev_before, rt_before = _sha(ev_path), _sha(rt_path)

    sel = svc.select_for_context(
        session_id=sid, model_id="qwen", participants=_participants(),
        current_round_id=9,
        lossless_round_ids=[], budget_tokens=1,
    )
    assert sel.entries == []
    assert sel.reason == "budget_insufficient"

    skipped = [e for e in _events(storage, sid)
               if e["event"] == "mcp_evidence_skipped_for_budget"]
    assert skipped and skipped[0]["reason"] == "budget_insufficient"
    assert skipped[0]["evidence_round_id"] == 1

    # 反向证明：Runtime / Index 原样保留（跳过 ≠ 删除）
    assert _sha(ev_path) == ev_before
    assert _sha(rt_path) == rt_before
    assert rt_path.exists() and ev_path.exists()


def test_69_budget_unknown_is_conservative(tmp_path):
    """§4.5.7：预算不可知时不假装能装（沿用 unknown → 保守降级约定）。"""
    svc, storage, sid = _svc(tmp_path)
    _write_evidence(storage, sid, [_entry(round_id=1)])
    sel = svc.select_for_context(
        session_id=sid, model_id="qwen", participants=_participants(),
        current_round_id=9,
        lossless_round_ids=[], budget_tokens=None,
    )
    assert sel.entries == []
    assert sel.reason == "budget_unknown"


# ── §4.5.6 / §4.5.11：只注入 Reference、不泄漏身份 ───────────────────────


def test_69_only_reference_enters_prompt():
    """§4.5.6：只渲染 reference；runtime_record_id 等内部字段不进模型可见内容。"""
    block = render_evidence_block([_entry(round_id=3)])
    assert QUOTED in block
    assert "call_A" not in block            # runtime_record_id 是内部 provenance
    assert EVIDENCE_BLOCK_HEADER in block


def test_69_source_model_id_never_visible():
    """§4.5.11 / §69-17：模型可见内容不得暴露真实 source_model_id。"""
    e = _entry(round_id=1, model="qwen-plus", reference="外部结果")
    block = render_evidence_block([e])
    assert "qwen-plus" not in block
    assert e.source_model_id == "qwen-plus"          # 内部仍保留（A1）
    assert "runtime_record_id" not in block
    # to_dict 内部保留 provenance，但渲染层不输出
    assert e.to_dict()["source_model_id"] == "qwen-plus"


def test_69_runtime_result_body_not_copied(tmp_path):
    """§4.5.6 / §69-11：Runtime 原始 Tool Result 不直接复制进 Prompt。"""
    svc, storage, sid = _svc(tmp_path)
    preview = QUOTED + "\n" + RAW_TAIL
    _write_runtime(storage, sid, 1, [
        _rec(call_id="call_A", model_id="qwen", mcp="github",
             tool="search_issues", preview=preview),
    ])
    # 用真实 build_index 生成 Index（reference = 首行前 60 字符）
    report = svc.build_index(
        session_id=sid, round_id=1, model_id="qwen", final_answer=QUOTED,
    )
    assert report.entries and report.entries[0].used_in_final_answer

    block = render_evidence_block(report.entries)
    assert QUOTED in block
    assert RAW_TAIL not in block          # 原始正文没有进 Prompt
    assert "工具返回的完整原始正文" not in block


def test_69_skeleton_hard_check_covers_evidence_block():
    """§4.5.11 / §69-19：Evidence 块的系统生成骨架进 A3 硬校验。

    若系统自己把真实 model_id 写进骨架（如 mcp 名），必须抛 IdentityLeakError，
    而不是只记 warning。
    """
    from core.anonymizer import IdentityLeakError
    from core.prompt_builder import build_round_n_prompt_v3

    parts = _participants()
    leaky = _entry(round_id=1, mcp="qwen", tool="search", reference="外部结果")
    with pytest.raises(IdentityLeakError):
        build_round_n_prompt_v3(
            original_question="Q", user_instruction="I",
            historical_items=[], peer_context_items=[],
            self_model_id="qwen", candidate_ids={},
            peer_review=False, participants=parts,
            evidence_items=[leaky],
        )


#: 含真实 model_id 的外部引用文本
LEAKY_REF = "某评测提到 qwen 的延迟表现"
#: 含真实 display_name（大小写不同）的外部引用文本
LEAKY_REF_UPPER = "DeepSeek 的检索结果更快"


def test_69_prompt_last_line_drops_leaky_reference():
    """§4.5.11 最后防线：即使调用方绕开选择层，泄漏的 reference 也不渲染。

    旧语义（软扫描告警）已废除：带真实身份的 reference 不再"告警后照样渲染出去"，
    而是整条剔除。本用例直接调用 prompt_builder，模拟绕过选择层的调用方。
    """
    from core.prompt_builder import build_round_n_prompt_v3

    parts = _participants()
    clean = _entry(round_id=1, mcp="github", tool="search", reference="外部结果")
    leaky = _entry(round_id=2, mcp="github", tool="search", reference=LEAKY_REF,
                   call_id="call_leak")
    prompt, warns = build_round_n_prompt_v3(
        original_question="Q", user_instruction="I",
        historical_items=[], peer_context_items=[],
        self_model_id="qwen", candidate_ids={},
        peer_review=False, participants=parts,
        evidence_items=[clean, leaky],
    )
    block = prompt.split(EVIDENCE_BLOCK_HEADER)[1]
    assert "外部结果" in block           # 干净的那条照常渲染
    assert LEAKY_REF not in block        # 泄漏的那条没有渲染
    assert "[E2]" not in block           # 只剩一条 → 编号不会出现 E2
    assert any("已整条剔除" in w for w in warns)


# ── §4.5.11：reference 身份泄漏 → 整条剔除（不是告警）────────────────────


def test_69_leaky_reference_is_skipped_for_identity(tmp_path):
    """§4.5.11：reference 命中真实身份词元 → 跳过该条 + 记 identity 事件。

    取代旧的"软扫描告警"语义：泄漏的 reference 不再进入模型可见内容。
    同时反向证明"没有一律不注入"——干净的那条照常注入。
    """
    svc, storage, sid = _svc(tmp_path)
    _write_evidence(storage, sid, [
        _entry(round_id=1, reference="干净的外部检索结果", call_id="call_clean"),
        _entry(round_id=2, reference=LEAKY_REF, call_id="call_leak"),
        _entry(round_id=3, reference=LEAKY_REF_UPPER, call_id="call_leak2"),
    ])
    sel = svc.select_for_context(
        session_id=sid, model_id="qwen", participants=_participants(),
        current_round_id=9, lossless_round_ids=[], budget_tokens=100000,
    )

    assert [e.round_id for e in sel.entries] == [1]
    assert sorted(e.round_id for e in sel.skipped_for_identity) == [2, 3]
    block = render_evidence_block(sel.entries)
    assert LEAKY_REF not in block and LEAKY_REF_UPPER not in block
    assert "干净的外部检索结果" in block

    ev = [e for e in _events(storage, sid)
          if e["event"] == "mcp_evidence_skipped_for_identity"]
    assert sorted(e["evidence_round_id"] for e in ev) == [2, 3]
    for e in ev:
        assert e["reason"] == "identity_leak_in_reference"
        assert e["model_id"] == "qwen"
    by_round = {e["evidence_round_id"]: e for e in ev}
    assert "qwen" in by_round[2]["identity_hits"]
    assert by_round[2]["reference"] == LEAKY_REF
    # display_name 也在判定范围内（大小写不敏感）
    assert "deepseek" in [t.lower() for t in by_round[3]["identity_hits"]]


def test_69_identity_skip_is_not_budget_skip(tmp_path):
    """反向证明：身份剔除 ≠ 预算跳过 —— 事件名 / 字段 / reason 互不冒充。"""
    svc, storage, sid = _svc(tmp_path)
    _write_evidence(storage, sid, [_entry(round_id=2, reference=LEAKY_REF)])
    sel = svc.select_for_context(
        session_id=sid, model_id="qwen", participants=_participants(),
        current_round_id=9, lossless_round_ids=[], budget_tokens=1,  # 预算也不够
    )
    assert sel.entries == []
    assert [e.round_id for e in sel.skipped_for_identity] == [2]
    assert sel.skipped == []        # 没有被记成预算跳过
    assert sel.reason is None       # reason 只描述预算层状态

    events = _events(storage, sid)
    assert [e["event"] for e in events] == ["mcp_evidence_skipped_for_identity"]
    assert not [e for e in events
                if e["event"] == "mcp_evidence_skipped_for_budget"]


def test_69_identity_skip_keeps_runtime_and_index(tmp_path):
    """§4.5.11：剔除 ≠ 删除 —— Runtime / Evidence Index 字节不变且仍可追溯。"""
    svc, storage, sid = _svc(tmp_path)
    ev_path = _write_evidence(storage, sid, [_entry(round_id=2, reference=LEAKY_REF)])
    rt_path = _write_runtime(storage, sid, 2, [
        _rec(call_id="call_A", model_id="qwen", mcp="github",
             tool="search_issues", preview=LEAKY_REF),
    ])
    ev_before, rt_before = _sha(ev_path), _sha(rt_path)

    svc.select_for_context(
        session_id=sid, model_id="qwen", participants=_participants(),
        current_round_id=9, lossless_round_ids=[], budget_tokens=100000,
    )

    assert _sha(ev_path) == ev_before, "Evidence Index 被修改"
    assert _sha(rt_path) == rt_before, "Runtime 被修改"
    # 被剔除的 Evidence 仍留在 Index 中，原文仍可从 Runtime 定位
    assert svc.trace_to_runtime(sid, svc.load(sid)[0])["status"] == "found"


def test_69_identity_filter_does_not_over_block(tmp_path):
    """反向证明：不含身份词的 reference 不被误剔。"""
    svc, storage, sid = _svc(tmp_path)
    _write_evidence(storage, sid, [
        _entry(round_id=1, reference="GitHub Issue #456 长尾延迟", call_id="c1"),
        _entry(round_id=2, reference="OpenAlex 论文摘要 2024", call_id="c2"),
    ])
    sel = svc.select_for_context(
        session_id=sid, model_id="qwen", participants=_participants(),
        current_round_id=9, lossless_round_ids=[], budget_tokens=100000,
    )
    assert [e.round_id for e in sel.entries] == [2, 1]
    assert sel.skipped_for_identity == []
    assert not [e for e in _events(storage, sid)
                if e["event"] == "mcp_evidence_skipped_for_identity"]


# ── §4.5.9 / §4.5.10 / §4.5.1：语义隔离的反向证明 ────────────────────────


def test_69_evidence_never_enters_peer_context():
    """§4.5.9 / §69-15：Evidence 不自动成为 Peer Context。"""
    from core.peer_context import PeerContextItem
    from core.prompt_builder import build_round_n_prompt_v3

    peer = PeerContextItem(
        source_model_id="gpt", mode="final_answer", source_type="final_answer",
        source_id="round_001", content="Peer 的 Final Answer",
        valid_answer_count=1, candidate_id="Candidate B",
        created_at="2026-09-16T00:00:00Z",
    )
    prompt, _ = build_round_n_prompt_v3(
        original_question="Q", user_instruction="I",
        historical_items=[], peer_context_items=[peer],
        self_model_id="qwen", candidate_ids={"gpt": "Candidate B"},
        peer_review=True, participants=_participants(),
        evidence_items=[_entry(round_id=1, reference=QUOTED)],
    )
    # 切出 Peer Context 块（到 Evidence 块之前为止）
    peer_section = prompt.split("## 其他参与者当前状态（Peer Context）")[1]
    peer_section = peer_section.split(EVIDENCE_BLOCK_HEADER)[0]
    assert "Peer 的 Final Answer" in peer_section    # 切分正确
    assert QUOTED not in peer_section               # Evidence 不在 Peer Context 里
    assert EVIDENCE_BLOCK_HEADER in prompt          # 它是独立块


def test_69_evidence_is_never_a_fact():
    """§4.5.1 / §4.5.10 / §69-4：Evidence ≠ Fact，且不得自动写入 Project Memory。

    反向证明：Entry 没有 is_fact / confidence / score 字段；
    模型可见块显式声明"不代表已确认事实"。
    """
    e = _entry(round_id=1)
    d = e.to_dict()
    for banned in ("is_fact", "fact", "confidence", "score", "verified"):
        assert banned not in d
        assert not hasattr(e, banned)
    assert "不代表已确认事实" in EVIDENCE_BLOCK_NOTE
    assert "不代表已确认事实" in render_evidence_block([e])


def test_69_traceable_to_session_round_runtime_record(tmp_path):
    """§4.5.6 / §69-3 / §69-18：Reference 可回溯到 Round / Runtime Record。"""
    svc, storage, sid = _svc(tmp_path)
    _write_runtime(storage, sid, 1, [
        _rec(call_id="call_A", model_id="qwen", mcp="github",
             tool="search_issues", preview=QUOTED),
        _rec(call_id="call_B", model_id="gpt", mcp="openalex",
             tool="search", preview="别的模型的记录"),
    ])
    svc.build_index(session_id=sid, round_id=1, model_id="qwen",
                    final_answer=QUOTED)
    sel = svc.select_for_context(
        session_id=sid, model_id="qwen", participants=_participants(),
        current_round_id=9,
        lossless_round_ids=[], budget_tokens=100000,
    )
    assert len(sel.entries) == 1
    traced = svc.trace_to_runtime(sid, sel.entries[0])
    assert traced["status"] == "found"
    assert traced["record"]["result_preview"] == QUOTED   # 原始 Result 完整


def test_69_runtime_and_index_untouched_by_selection(tmp_path):
    """§4.5.1 / §69-1：选择过程只读，Runtime / Index 字节不变。"""
    svc, storage, sid = _svc(tmp_path)
    ev = _write_evidence(storage, sid, [_entry(round_id=1)])
    rt = _write_runtime(storage, sid, 1, [
        _rec(call_id="call_A", model_id="qwen", mcp="github",
             tool="search_issues", preview=QUOTED),
    ])
    before = {str(p.relative_to(storage.session_dir(sid))): _sha(p)
              for p in storage.session_dir(sid).rglob("*") if p.is_file()}
    svc.select_for_context(
        session_id=sid, model_id="qwen", participants=_participants(),
        current_round_id=9,
        lossless_round_ids=[], budget_tokens=100000,
    )
    after = {str(p.relative_to(storage.session_dir(sid))): _sha(p)
             for p in storage.session_dir(sid).rglob("*") if p.is_file()}
    for k, v in before.items():
        if k == "context_policy.jsonl":
            continue                      # 允许追加日志（§4.5.8）
        assert after.get(k) == v, f"{k} 被修改"


# ── 真链路：RoundManager 接线 ───────────────────────────────────────────


class _Chain:
    """真链路 harness：R1 让 qwen 用 MCP 工具并在回答里引用其结果。

    ``quoted`` 可替换 R1 的 tool result 首行（= Evidence reference 的来源），
    用于构造"reference 带真实身份"的场景。
    """

    def __init__(self, tmp: Path, *, quoted: str = QUOTED):
        os.environ.setdefault("CROSSLLM_MOCK_DELAY_SCALE", "0")
        from core.model_manager import ModelRegistry
        from core.round_manager import RoundManager
        from core.session_manager import SessionManager
        from core.storage import Storage
        from core import mcp_runtime_log as runtime_log

        self.quoted = quoted
        self.runtime_log = runtime_log
        self.storage = Storage(tmp / "sessions")
        self.registry = ModelRegistry()
        # §一：注入确定性模型能力（数值沿用改动前的内建默认，预算数字不变）
        self.rounds = RoundManager(
            self.storage, self.registry, self._factory,
            capability_resolver=StubCapabilityResolver(
                context_limit=8192, model_max_output=2048,
            ),
        )
        self.sessions = SessionManager(self.storage, self.registry)
        sess = self.sessions.create("OCR 多模态文档解析架构怎样设计？")
        self.sid, self.sess = sess.session_id, sess
        self.sdir = self.storage.session_dir(self.sid)

    def _factory(self, mid: str):
        outer = self

        class _P:
            def __init__(self, mid):
                self.mid = mid

            async def generate(self, round_id, prompt, max_output_tokens=None):
                # Compaction 请求：返回**短**的带归属标注摘要，
                # 让 compaction_budget 留有余量给 Evidence（§4.5.7 共用预算）
                if "多参与者对话历史" in prompt:
                    yield "参与者 Candidate C 认为：方案 X 更稳妥。\n"
                    return
                if round_id == 1 and self.mid == "qwen":
                    ctx = outer.runtime_log.get_runtime_context()
                    if ctx is not None:
                        rec = outer.runtime_log.build_record(
                            ctx=ctx,
                            tool_call={"id": "call_A", "name": "search_issues"},
                            mcp_name="github", tool_name="search_issues",
                            arguments={"q": "ocr"}, started_at=outer.runtime_log.now_iso(),
                            ok=True, result_chars=len(outer.quoted) + len(RAW_TAIL),
                            result_preview=outer.quoted + "\n" + RAW_TAIL,
                            error_type=None, error_message=None,
                            returned_at=outer.runtime_log.now_iso(), duration_ms=5,
                        )
                        outer.runtime_log.append_record(ctx.directory, rec)
                    yield f"工具返回：{outer.quoted}。基于此我建议先量化 P99 再谈扩容。"
                    return
                # 大内容：挤掉 lossless，使 R1 离开无损区（§4.5.5 才允许注入）
                yield f"[{self.mid}] 详细分析：" + ("论证内容。" * 1100)

        return _P(mid)

    async def drive(self, k=0.5, peer=False, instr="请继续。"):
        rs, bus = self.rounds.prepare_round(self.sess, instr, peer_review=peer, k=k)
        await self.rounds.execute_round(self.sid, rs, bus)
        self.sess = self.sessions.get(self.sid)
        return rs

    def prompt(self, round_id: int, model: str) -> str:
        p = self.sdir / f"round_{round_id:03d}" / f"{model}.prompt.md"
        return p.read_text(encoding="utf-8") if p.exists() else ""

    def events(self) -> list[dict]:
        p = self.sdir / "context_policy.jsonl"
        if not p.exists():
            return []
        return [json.loads(l) for l in p.read_text(encoding="utf-8").splitlines()
                if l.strip()]


def test_69_real_chain_injects_own_evidence_only(tmp_path):
    """真链路：只注入"自己"的 Evidence，且只以 reference 形式进入 Prompt。"""
    h = _Chain(tmp_path)
    asyncio.run(h.drive(k=0.5, instr="R1：给我方案"))
    # R1 有 MCP Evidence；R2 大内容把 R1 挤出无损区 → 允许注入
    r2 = asyncio.run(h.drive(k=0.5, instr="R2：继续"))
    asyncio.run(h.drive(k=0.5, instr="R3：继续"))

    # 1) 被注入（qwen 的 prompt 出现 Evidence 块 + reference）
    qwen_prompts = [h.prompt(r, "qwen") for r in (2, 3)]
    hit = [p for p in qwen_prompts if EVIDENCE_BLOCK_HEADER in p]
    assert hit, "qwen 的 Prompt 未出现 Evidence 块"
    assert any(QUOTED in p for p in hit)

    # 2) 原始 Runtime 正文没有进 Prompt（§4.5.6）
    assert all(RAW_TAIL not in p for p in qwen_prompts)

    # 3) A1：其他模型的 Prompt 没有 Evidence 块
    for other in ("deepseek", "gpt"):
        for r in (2, 3):
            assert EVIDENCE_BLOCK_HEADER not in h.prompt(r, other)


def test_69_real_chain_raw_archive_and_project_memory_untouched(tmp_path):
    """反向证明：注入 Evidence 不碰 Raw Archive，也不写 Project Memory。"""
    h = _Chain(tmp_path)
    asyncio.run(h.drive(k=0.5, instr="R1"))
    r1_dir = h.sdir / "round_001"
    r1_before = {str(p.relative_to(r1_dir)): _sha(p)
                 for p in r1_dir.rglob("*") if p.is_file()}
    projects = h.storage.projects_root()
    proj_before = {str(p.relative_to(projects)): _sha(p)
                   for p in projects.rglob("*") if p.is_file()} if projects.exists() else {}

    asyncio.run(h.drive(k=0.5, instr="R2"))
    asyncio.run(h.drive(k=0.5, instr="R3"))

    r1_after = {str(p.relative_to(r1_dir)): _sha(p)
                for p in r1_dir.rglob("*") if p.is_file()}
    assert r1_after == r1_before, "Raw Archive 被修改"

    proj_after = {str(p.relative_to(projects)): _sha(p)
                  for p in projects.rglob("*") if p.is_file()} if projects.exists() else {}
    assert proj_after == proj_before, "Evidence 自动写入了 Project Memory"


def test_69_real_chain_evidence_respects_lossless_dedup(tmp_path):
    """真链路：注入 ↔ 无损区去重必须严格互斥（§4.5.4 / §4.5.5）。

    可证伪断言：只要某轮 Prompt 出现 Evidence 块，该轮的无损区就**不得**包含
    被注入 Evidence 所属的 Round（否则就是重复注入）。
    """
    h = _Chain(tmp_path)
    asyncio.run(h.drive(k=0.5, instr="R1"))

    injected_any = False
    for r in (2, 3):
        asyncio.run(h.drive(k=0.5, instr=f"R{r}"))
        prompt = h.prompt(r, "qwen")
        sel = [e for e in h.events()
               if e["event"] == "history_selection" and e.get("round_id") == r]
        assert sel, f"R{r} 缺少 history_selection 事件"
        lossless = set(sel[-1]["pinned_round_ids"]) | set(
            sel[-1]["recent_raw_round_ids"]
        )
        if EVIDENCE_BLOCK_HEADER in prompt:
            injected_any = True
            assert 1 not in lossless, f"R{r}：R1 仍在无损区却注入了其 Evidence"
            assert QUOTED in prompt
        else:
            # 未注入只允许两种原因：Round 仍在无损区，或预算不足
            skipped = [e for e in h.events()
                       if e["event"] == "mcp_evidence_skipped_for_budget"
                       and e.get("round_id") == r]
            assert 1 in lossless or skipped, f"R{r}：既未注入也无任何解释"

    assert injected_any, "整个链路从未注入 Evidence，该测试未触及目标路径"


def test_69_real_chain_leaky_reference_is_never_injected(tmp_path):
    """真链路（§4.5.11）：R1 的 reference 带真实 model_id → 任何模型 Prompt 都拿不到。

    与 test_69_real_chain_injects_own_evidence_only 是**同一套链路、同一批轮次**，
    只把 R1 的 tool result 首行换成含 "qwen" 的文本：
    该用例证明"此时本该注入"，本用例证明"此时必须整条剔除并留 identity 事件"。
    """
    leaky_text = "评测对比：qwen 的延迟表现最好，值得参考"
    h = _Chain(tmp_path, quoted=leaky_text)
    asyncio.run(h.drive(k=0.5, instr="R1：给我方案"))

    checked = 0
    for r in (2, 3):
        asyncio.run(h.drive(k=0.5, instr=f"R{r}：继续"))
        sel = [e for e in h.events()
               if e["event"] == "history_selection" and e.get("round_id") == r]
        assert sel, f"R{r} 缺少 history_selection 事件"
        lossless = set(sel[-1]["pinned_round_ids"]) | set(
            sel[-1]["recent_raw_round_ids"]
        )
        if 1 in lossless:
            continue                      # 仍在无损区：本来就不该注入，不算目标轮
        checked += 1
        # 1) R1 已离开无损区 → 不该有任何 Evidence 块进入任何模型的 Prompt
        for m in ("qwen", "deepseek", "gpt"):
            prompt = h.prompt(r, m)
            assert EVIDENCE_BLOCK_HEADER not in prompt, f"{m} 在 R{r} 拿到了 Evidence 块"
            assert leaky_text not in prompt, f"{m} 在 R{r} 拿到了泄漏文本"
        # 2) 必须留下 identity 跳过事件（而不是静默丢弃）
        ev = [e for e in h.events()
              if e["event"] == "mcp_evidence_skipped_for_identity"
              and e.get("round_id") == r]
        assert ev, f"R{r}：泄漏的 Evidence 被丢弃却没有任何记录"
        assert ev[0]["evidence_round_id"] == 1
        assert ev[0]["model_id"] == "qwen"
        assert "qwen" in [t.lower() for t in ev[0]["identity_hits"]]

    assert checked, "整条链路没有出现 R1 离开无损区的轮次，断言未生效"
