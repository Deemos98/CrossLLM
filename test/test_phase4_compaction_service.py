"""S3.8 Session Compaction 测试（Plan §4.9 §14 + MD §32-§40 §60）。

严格区分（修订 #1 / §37 明禁冒充）：

    Conversation Summary ≠ Session Compaction
    summaries/<model>/   ≠  compactions/
    "conversation_summary" ≠ "session_compaction"

覆盖用户任务书 13 项要求：

  1. Session 级共享，可含多模型内容，但必须保留 viewpoint attribution
  2. Persistence 与 Conversation Summary 严格分离（目录 + artifact_kind）
  3. 输入只允许 Valid Round + 已有旧 Compacted Block；Raw Archive 不变
  4. 输出保留 User Goals / Model Opinions / Viewpoint Attribution / …
     允许 Opinion / Hypothesis / Disagreement / Unknown 继续存在
  5. 真正接入 ContextSelector，消费 compaction_budget（不重算预算）
  6. Recursive Compaction + MAX_COMPACTION_ROUNDS + 明确终止条件
     禁止恢复 80% ratio trigger / HISTORY_WINDOW
  7. 代码隔离：CompactionRecord / CompactedBlock ≠ ConversationSummary
  8. 观点归属可辨认（R1 A=X B=Y → R2 A 支持 X / B 反对 X）
  9. Raw Archive 不变（round_NNN/* + state.json）
 10. Recursive Compaction attempt=1 / attempt=2 日志正确
 11. MCP Evidence 不自动变 Fact（本阶段不实现 Evidence Index）
 12. 不改 S3.1~S3.7 核心语义
 13. 真实行为证明（多 Round Compaction + 预算恒等式 + 归属可辨认）
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import tempfile
from pathlib import Path

import pytest

from core.compaction import (
    ARTIFACT_KIND,
    COMPACTED_HEADER_PREFIX,
    COMPACTIONS_DIRNAME,
    COMPACTION_PROMPT,
    CompactedBlock,
    CompactionRecord,
    CompactionService,
    check_viewpoint_attribution,
    find_attributed_identity_leak,
    render_compacted_block,
)
from core.model_capability import TokenEstimate
from core.storage import Storage

from _capability_stub import StubCapabilityResolver


# ── helpers ─────────────────────────────────────────────────────────────


class _VR:
    """最小 ValidRound 替身。"""

    def __init__(self, rid: int, succ: dict[str, str], q: str = "Q?"):
        self.round_id = rid
        self.successful_answers = succ
        self.user_question = q
        self.failed_models = []
        self.is_valid = bool(succ)


CALL_LOG: list[str] = []


def _attr_provider_factory(mid):
    """压缩输出**带**归属标注（正常路径）。

    ⚠ A3（Phase 4.5 修复）：压缩输入已用匿名 Candidate 标签标注参与者，
    因此"守规矩"的输出必须回抄**标签**；回抄真实 model_id 会被 F2b
    身份校验判为泄漏（见 test_phase4_compaction_identity.py）。
    标签映射（assign_candidate_ids，按 model_id 字典序）：
      {a, b}                     → A=a,            B=b
      {gpt-4o-mini, qwen-plus}   → A=gpt-4o-mini,  B=qwen-plus
    """

    class _P:
        def __init__(self, mid):
            self.mid = mid

        def generate(self, round_id, prompt, max_output_tokens=None):
            CALL_LOG.append(mid)
            yield "## 用户目标\n设计一个 OCR 架构。\n\n"
            yield "## 各参与者观点\n"
            yield "参与者 Candidate B 认为：方案 X（分层缓存）更稳妥。\n"
            yield "参与者 Candidate A 认为：方案 Y（全量重算）更简单。\n"
            yield "参与者 Candidate A 反对方案 X，理由是运维复杂度上升。\n\n"
            yield "## 分歧\n方案 X vs 方案 Y，未收敛。\n\n"
            yield "## 未验证假设\n长尾延迟的实际占比尚无数据（未验证）。\n"

    return _P(mid)


def _leak_provider_factory(mid):
    """压缩输出把**真实 model_id** 当参与者身份回抄（A3 泄漏路径）。"""

    class _P:
        def __init__(self, mid):
            self.mid = mid

        def generate(self, round_id, prompt="", max_output_tokens=None):
            CALL_LOG.append(mid)
            yield "## 各参与者观点\n"
            yield "参与者 qwen-plus 认为：方案 X（分层缓存）更稳妥。\n"
            yield "参与者 gpt-4o-mini 认为：方案 Y（全量重算）更简单。\n"

    return _P(mid)


def _no_attr_provider_factory(mid):
    """压缩输出**无**归属标注（触发 F2 失败降级）。"""

    class _P:
        def __init__(self, mid):
            self.mid = mid

        def generate(self, round_id, prompt, max_output_tokens=None):
            CALL_LOG.append(mid)
            yield "大家最终认为方案 X 更好，这是统一的结论。\n"

    return _P(mid)


class _ShrinkingFactory:
    """第一次输出超预算，第二次满足预算（驱动递归）。"""

    def __init__(self, long_text: str, short_text: str):
        self.long_text = long_text
        self.short_text = short_text
        self.calls = 0

    def __call__(self, mid):
        outer = self

        class _P:
            def __init__(self, mid):
                self.mid = mid

            def generate(self, round_id, prompt, max_output_tokens=None):
                outer.calls += 1
                CALL_LOG.append(mid)
                if outer.calls == 1:
                    yield outer.long_text
                else:
                    yield outer.short_text

        return _P(mid)


@pytest.fixture(autouse=True)
def _clear_calls():
    CALL_LOG.clear()
    yield
    CALL_LOG.clear()


def _te(v: int) -> TokenEstimate:
    return TokenEstimate(v, "crossllm_estimator", "estimated")


def _make_svc(tmp_path, factory=None, **kw):
    from core.storage import Storage

    storage = Storage(tmp_path)
    return CompactionService(storage, provider_factory=factory, **kw), storage


def _top_dir(rel_key: str) -> str:
    """取相对路径的首段目录（兼容 Windows 反斜杠）。"""
    return Path(rel_key).parts[0] if Path(rel_key).parts else rel_key

def _snapshot(root: Path) -> dict[str, str]:
    return {
        str(p.relative_to(root)): hashlib.md5(p.read_bytes()).hexdigest()
        for p in root.rglob("*")
        if p.is_file()
    }


# ── 1. 数据结构与防冒充 ──────────────────────────────────────────────────


def test_artifact_kind_never_equals_conversation_summary():
    """§4.9.1 核心：artifact_kind 绝不等于 conversation_summary。"""
    assert ARTIFACT_KIND == "session_compaction"
    # Conversation Summary 的产物标记是 is_derived_artifact（summary_service），
    # 全项目不存在 conversation_summary 这个 artifact_kind → 二者永不相等。
    assert ARTIFACT_KIND != "conversation_summary"


def test_compaction_record_rejects_impersonation():
    """构造时若冒充 conversation_summary → AssertionError。"""
    with pytest.raises(AssertionError, match="artifact_kind"):
        CompactionRecord(
            attempt=1,
            source_rounds=[1],
            contributor_models=["a"],
            viewpoint_attribution=True,
            input_tokens=_te(1),
            expected_summary_tokens=_te(1),
            actual_summary_tokens=_te(1),
            status="success",
            artifact_kind="conversation_summary",  # 冒充
        )


def test_compaction_record_requires_attempt_ge_1_and_rounds():
    with pytest.raises(AssertionError, match="attempt"):
        CompactionRecord(
            attempt=0, source_rounds=[1], contributor_models=["a"],
            viewpoint_attribution=True, input_tokens=_te(1),
            expected_summary_tokens=_te(1), actual_summary_tokens=_te(1),
            status="success",
        )
    with pytest.raises(AssertionError, match="source_rounds"):
        CompactionRecord(
            attempt=1, source_rounds=[], contributor_models=["a"],
            viewpoint_attribution=True, input_tokens=_te(1),
            expected_summary_tokens=_te(1), actual_summary_tokens=_te(1),
            status="success",
        )


def test_compacted_block_is_shared_and_typed():
    rec = CompactionRecord(
        attempt=1, source_rounds=[1, 2], contributor_models=["a", "b"],
        viewpoint_attribution=True, input_tokens=_te(10),
        expected_summary_tokens=_te(3), actual_summary_tokens=_te(4),
        status="success", summary_file="compactions/compact_001.md",
    )
    blk = CompactedBlock(
        content="参与者 a 认为 X；参与者 b 反对 X。",
        source_files=["compactions/compact_001.md"],
        records=[rec], contributor_models=["a", "b"],
    )
    assert blk.is_shared is True           # Session 级共享
    assert blk.artifact_kind == ARTIFACT_KIND
    assert blk.latest is rec
    assert blk.token_estimate().value > 0


def test_type_distinct_from_conversation_summary():
    """§7.5：CompactionRecord / CompactedBlock ≠ ConversationSummary。"""
    from core.summary_service import ConversationSummary

    assert CompactionRecord is not ConversationSummary
    assert CompactedBlock is not ConversationSummary
    # ConversationSummary 没有 artifact_kind 语义为 session_compaction 的字段
    s = ConversationSummary(
        summary_id="summary_001", session_id="s", model_id="a",
        source_rounds=[1], source_valid_round=1,
        prompt="p", content="c", created_at="t",
        trigger="user", status="success",
    )
    assert s.is_derived_artifact is True
    # Session Compaction 的关键字段在 ConversationSummary 上不存在
    assert not hasattr(s, "contributor_models")
    assert not hasattr(s, "viewpoint_attribution")
    assert not hasattr(s, "attempt")


# ── 2. 观点归属（F1/F2/F3 三层防线）─────────────────────────────────────


def test_viewpoint_attribution_detector():
    assert check_viewpoint_attribution("参与者 A 认为 X") is True
    assert check_viewpoint_attribution("[Candidate-B] 反对") is True
    assert check_viewpoint_attribution("Participant C says") is True
    # 无来源统一结论 → False
    assert check_viewpoint_attribution("大家最终认为 X") is False
    assert check_viewpoint_attribution("综上所述，方案 X 更好。") is False
    assert check_viewpoint_attribution("") is False


def test_compaction_prompt_forces_attribution():
    """F1：COMPACTION_PROMPT 含显式归属指令。"""
    assert "保留每个观点的来源归属" in COMPACTION_PROMPT
    assert "不得将多方观点合并为无主语的陈述句" in COMPACTION_PROMPT
    # 允许 Opinion / Hypothesis / Disagreement / Unknown 继续存在
    assert "未验证假设" in COMPACTION_PROMPT
    assert "分歧" in COMPACTION_PROMPT
    assert "观点" in COMPACTION_PROMPT
    # 禁止 MCP Result 自动 Fact
    assert "不要把 MCP 工具返回的原始结果当作已证实的事实" in COMPACTION_PROMPT


def test_render_compacted_block_declares_source():
    """F3：渲染标题必须声明 Session 级共享 + 多位参与者（**不含真实 id**）。"""
    rec = CompactionRecord(
        attempt=1, source_rounds=[1], contributor_models=["a", "b"],
        viewpoint_attribution=True, input_tokens=_te(1),
        expected_summary_tokens=_te(1), actual_summary_tokens=_te(1),
        status="success",
    )
    blk = CompactedBlock(
        content="X", source_files=["compactions/compact_001.md"],
        records=[rec], contributor_models=["a", "b"],
    )
    out = render_compacted_block(blk)
    assert "Session 级共享" in out
    assert "非本模型个人总结" in out
    assert "含多位参与者" in out
    # A3：标题不得写入真实 contributor_models
    assert "a, b" not in out
    for mid in ("a", "b"):
        assert f"参与者 {mid}" not in out, f"标题泄漏真实 model_id: {mid}"


def test_no_attribution_fails_and_degrades(tmp_path):
    """F2：无归属标记 → status="failed" + 降级（不落盘可用产物）。"""
    svc, storage = _make_svc(tmp_path, factory=_no_attr_provider_factory)
    sid = "s-noattr"
    storage.ensure_session_dir(sid)
    rounds = [_VR(1, {"a": "A1", "b": "B1"}), _VR(2, {"a": "A2", "b": "B2"})]

    blk = asyncio.run(svc.compact(session_id=sid, rounds=rounds,
                                  compaction_budget=1000))
    assert blk.records, "应至少有一条 record"
    rec = blk.records[0]
    assert rec.viewpoint_attribution is False
    assert rec.status == "failed"
    assert rec.error_message == "viewpoint_attribution_missing"
    # 归属失败 → 不产出可注入内容
    assert blk.content == ""
    # 也不落盘
    assert not list(svc.compactions_dir(sid).glob("compact_*.md"))


def test_model_opinions_stay_distinguishable(tmp_path):
    """任务书 §8：R1 A=X / B=Y → R2 A 支持 X / B 反对 X。
    压缩后必须仍能区分，不得压成"大家最终认为 X"。
    """
    svc, storage = _make_svc(tmp_path, factory=_attr_provider_factory)
    sid = "s-attr"
    storage.ensure_session_dir(sid)
    rounds = [
        _VR(1, {
            "qwen-plus": "我建议方案 X：分层缓存。",
            "gpt-4o-mini": "我建议方案 Y：全量重算。",
        }, "OCR 架构怎么设计？"),
        _VR(2, {
            "qwen-plus": "继续支持方案 X，延迟可控。",
            "gpt-4o-mini": "我反对方案 X，运维太复杂。",
        }, "请再评估一下？"),
    ]

    blk = asyncio.run(svc.compact(session_id=sid, rounds=rounds,
                                  compaction_budget=1000))

    assert blk.records[-1].status == "success"
    assert blk.records[-1].viewpoint_attribution is True
    # contributor_models 完整记录两个模型（**内部 provenance**，允许保留真实 id）
    assert blk.contributor_models == ["gpt-4o-mini", "qwen-plus"]
    # 内容中 A / B 观点各自可辨认（匿名标签，非真实 model_id）
    rendered = render_compacted_block(blk)
    assert "参与者 Candidate B 认为：方案 X" in rendered
    assert "参与者 Candidate A 认为：方案 Y" in rendered
    assert "参与者 Candidate A 反对方案 X" in rendered
    # A3：模型可见内容中不得出现真实 model_id（标签 B/A 分别对应 qwen-plus/gpt-4o-mini）
    assert "qwen-plus" not in rendered
    assert "gpt-4o-mini" not in rendered
    # 绝不能出现无来源统一结论
    assert "大家最终认为" not in rendered
    # 允许未验证 / 分歧继续存在（§4 输出要求）
    assert "未验证" in rendered or "尚无数据" in rendered


# ── 3. Persistence 严格分离 ──────────────────────────────────────────────


def test_persistence_directory_isolation(tmp_path):
    """§4.9.2：compactions/ 与 summaries/ 严格分目录。"""
    svc, storage = _make_svc(tmp_path, factory=_attr_provider_factory)
    sid = "s-dirs"
    storage.ensure_session_dir(sid)
    rounds = [_VR(1, {"a": "A1", "b": "B1"}), _VR(2, {"a": "A2", "b": "B2"})]
    asyncio.run(svc.compact(session_id=sid, rounds=rounds,
                            compaction_budget=1000))

    sdir = storage.session_dir(sid)
    assert (sdir / COMPACTIONS_DIRNAME).is_dir(), "compactions/ 应存在"
    assert not (sdir / "summaries").exists(), (
        "CompactionService 绝不能创建 summaries/（§4.9.2 目录隔离）"
    )
    files = sorted(p.name for p in (sdir / COMPACTIONS_DIRNAME).glob("*.md"))
    assert files == ["compact_001.md"]


def test_compaction_artifact_file_kind_field(tmp_path):
    """落盘文件的 frontmatter artifact_kind 必须是 session_compaction。"""
    svc, storage = _make_svc(tmp_path, factory=_attr_provider_factory)
    sid = "s-kind"
    storage.ensure_session_dir(sid)
    asyncio.run(svc.compact(
        session_id=sid,
        rounds=[_VR(1, {"a": "A1"}), _VR(2, {"a": "A2"})],
        compaction_budget=1000,
    ))
    p = svc.compact_path(sid, 1)
    assert p.exists()
    raw = p.read_text(encoding="utf-8")
    assert raw.startswith("---\n")
    end = raw.find("\n---\n", 4)
    meta = json.loads(raw[4:end])
    assert meta["artifact_kind"] == ARTIFACT_KIND
    assert meta["artifact_kind"] != "conversation_summary"
    assert meta["viewpoint_attribution"] is True


def test_no_cross_write_between_compaction_and_summary(tmp_path):
    """T22：两个目录没有任何代码路径互相写入。"""
    svc, storage = _make_svc(tmp_path, factory=_attr_provider_factory)
    sid = "s-cross"
    storage.ensure_session_dir(sid)
    before = _snapshot(storage.session_dir(sid))

    # Compaction 写入
    asyncio.run(svc.compact(
        session_id=sid,
        rounds=[_VR(1, {"a": "A1"}), _VR(2, {"a": "A2"})],
        compaction_budget=1000,
    ))

    after = _snapshot(storage.session_dir(sid))
    new_keys = set(after) - set(before)
    # 新增项只能是 compactions/ 下文件或 context_policy.jsonl
    for k in new_keys:
        assert _top_dir(k) == COMPACTIONS_DIRNAME or k == "context_policy.jsonl", (
            f"Compaction 越界写入: {k}"
        )


# ── 4. Raw Archive 不变 ──────────────────────────────────────────────────


def test_raw_archive_untouched_by_compaction(tmp_path):
    """§9：Compaction 前后 round_NNN/* 与 state.json 字节级不变。"""
    from core.state import RoundState, SessionState

    svc, storage = _make_svc(tmp_path, factory=_attr_provider_factory)
    sid = "s-raw"
    storage.ensure_session_dir(sid)
    storage.save_question(sid, "原始问题？")

    # 构造 2 轮 Raw Archive
    sess = SessionState(
        session_id=sid, original_question="原始问题？",
        current_round=2, active_models=["a", "b"], closed_models=[],
        rounds=[
            RoundState(round_id=1, user_instruction="i1",
                       participants=["a", "b"],
                       status={"a": "complete", "b": "complete"},
                       timestamp="2026-09-04T00:00:00Z"),
            RoundState(round_id=2, user_instruction="i2",
                       participants=["a", "b"],
                       status={"a": "complete", "b": "complete"},
                       timestamp="2026-09-04T00:00:00Z"),
        ],
    )
    storage.save_state(sess)
    for rid in (1, 2):
        storage.save_response(sid, rid, "a", f"A{rid}")
        storage.save_response(sid, rid, "b", f"B{rid}")

    sdir = storage.session_dir(sid)
    before = _snapshot(sdir)

    rounds = [
        _VR(1, {"a": "A1", "b": "B1"}, "原始问题？"),
        _VR(2, {"a": "A2", "b": "B2"}, "追问？"),
    ]
    asyncio.run(svc.compact(session_id=sid, rounds=rounds,
                            compaction_budget=1000))

    after = _snapshot(sdir)
    # 所有压缩前存在的文件，内容必须完全不变
    changed = [k for k, h in before.items() if after.get(k) != h]
    assert changed == [], f"Raw Archive 被修改: {changed}"

    # 新增只允许 compactions/ 与 context_policy.jsonl
    added = set(after) - set(before)
    for k in added:
        assert _top_dir(k) == COMPACTIONS_DIRNAME or k == "context_policy.jsonl", (
            f"Compaction 只能新增 compactions/* 与日志；实际新增 {k}"
        )


# ── 5. Recursive Compaction ──────────────────────────────────────────────


def test_recursive_compaction_attempt_1_then_2(tmp_path):
    """任务书 §10：第一次仍超限 → 第二次 → 满足预算；attempt 日志正确。"""
    # A3：压缩输入的参与者标签是匿名 Candidate 标签，输出按同一契约回抄
    long_text = "参与者 Candidate A 认为：方案 X 需要更多验证。" * 12   # 超预算
    short_text = "参与者 Candidate A 认为 X；参与者 Candidate B 反对 X。"  # 满足预算
    factory = _ShrinkingFactory(long_text, short_text)
    svc, storage = _make_svc(tmp_path, factory=factory)
    sid = "s-rec"
    storage.ensure_session_dir(sid)

    rounds = [_VR(r, {"a": f"A{r}", "b": f"B{r}"}) for r in range(1, 4)]
    blk = asyncio.run(svc.compact(session_id=sid, rounds=rounds,
                                  compaction_budget=30))

    assert factory.calls == 2, f"应递归 2 次；实际 {factory.calls}"
    assert len(blk.records) == 2
    assert [r.attempt for r in blk.records] == [1, 2]
    # attempt=2 的 source_summary_files 记录 attempt=1 的产物
    assert blk.records[1].source_summary_files == ["compactions/compact_001.md"]
    # 最终产物满足预算
    assert blk.token_estimate().value <= 30
    # 两次都通过归属校验
    assert all(r.viewpoint_attribution for r in blk.records)

    # 日志 attempt 递增
    policy = (storage.session_dir(sid) / "context_policy.jsonl").read_text(
        encoding="utf-8"
    )
    evs = [
        json.loads(l) for l in policy.splitlines() if l.strip()
        and json.loads(l)["event"] == "compaction"
    ]
    assert [e["attempt"] for e in evs] == [1, 2]
    assert all(e["artifact_kind"] == ARTIFACT_KIND for e in evs)


def test_recursion_terminates_at_max_rounds(tmp_path):
    """§38：达到 MAX_COMPACTION_ROUNDS 必须终止，不能无限循环。"""
    # 永远超预算的输出 → 必须靠 max_rounds 终止
    class _AlwaysLong:
        def __call__(self, mid):
            outer_calls = []

            class _P:
                def generate(self, round_id, prompt, max_output_tokens=None):
                    CALL_LOG.append(mid)
                    yield "参与者 Candidate A 认为：" + "内容。" * 200

            return _P()

    svc, storage = _make_svc(tmp_path, factory=_AlwaysLong(), max_rounds=2)
    sid = "s-max"
    storage.ensure_session_dir(sid)
    rounds = [_VR(r, {"a": f"A{r}", "b": f"B{r}"}) for r in range(1, 4)]

    blk = asyncio.run(svc.compact(session_id=sid, rounds=rounds,
                                  compaction_budget=10))
    # 恰好 max_rounds 次尝试后终止
    assert len(blk.records) == 2
    assert [r.attempt for r in blk.records] == [1, 2]
    assert len(CALL_LOG) == 2, f"应恰好终止于 max_rounds=2；实际 {len(CALL_LOG)}"


# ── 6. 复用与降级 ────────────────────────────────────────────────────────


def test_reuse_existing_compaction(tmp_path):
    """§14.4：compactions/ 已有覆盖产物 → 复用，不重复调 LLM。"""
    svc, storage = _make_svc(tmp_path, factory=_attr_provider_factory)
    sid = "s-reuse"
    storage.ensure_session_dir(sid)
    rounds = [_VR(1, {"a": "A1", "b": "B1"}), _VR(2, {"a": "A2", "b": "B2"})]

    asyncio.run(svc.compact(session_id=sid, rounds=rounds,
                            compaction_budget=1000))
    calls_after_first = len(CALL_LOG)
    assert calls_after_first == 1

    reused = svc.find_reusable(session_id=sid, source_rounds=[1, 2])
    assert reused is not None
    assert reused.contributor_models == ["a", "b"]
    assert reused.records[0].viewpoint_attribution is True
    # find_reusable 是纯读，不调 LLM
    assert len(CALL_LOG) == calls_after_first


def test_reuse_rejects_non_compaction_artifacts(tmp_path):
    """防冒充：artifact_kind 不是 session_compaction 的产物不可复用。"""
    svc, storage = _make_svc(tmp_path, factory=_attr_provider_factory)
    sid = "s-fake"
    storage.ensure_session_dir(sid)
    d = svc.compactions_dir(sid)
    fake = d / "compact_001.md"
    fake.write_text(
        "---\n"
        + json.dumps({
            "artifact_kind": "conversation_summary",  # 冒充
            "source_rounds": [1, 2],
            "viewpoint_attribution": True,
            "contributor_models": ["a"],
        })
        + "\n---\n\n内容\n",
        encoding="utf-8",
    )
    assert svc.find_reusable(session_id=sid, source_rounds=[1, 2]) is None


def test_provider_failure_degrades_without_raising(tmp_path):
    """§69 / Plan §1957：压缩失败降级，不中断 Round。"""

    class _Boom:
        def __call__(self, mid):
            class _P:
                def generate(self, round_id, prompt, max_output_tokens=None):
                    raise RuntimeError("provider down")
                    yield  # pragma: no cover
            return _P()

    svc, storage = _make_svc(tmp_path, factory=_Boom())
    sid = "s-fail"
    storage.ensure_session_dir(sid)
    blk = asyncio.run(svc.compact(
        session_id=sid,
        rounds=[_VR(1, {"a": "A1"}), _VR(2, {"a": "A2"})],
        compaction_budget=100,
    ))
    assert blk.records[-1].status == "failed"
    assert blk.content == ""          # 降级：无内容，调用方保留 raw
    assert blk.records[-1].error_message


def test_compact_requires_at_least_one_round(tmp_path):
    svc, _ = _make_svc(tmp_path, factory=_attr_provider_factory)
    with pytest.raises(ValueError, match="至少一个 ValidRound"):
        asyncio.run(svc.compact(session_id="s", rounds=[],
                                compaction_budget=10))


# ── 7. ContextSelector 消费 compaction_budget ────────────────────────────


def _budget_scaffold(tmp_path):
    from core.context_budget import make_fixed_input_tokens
    from core.context_selector import ContextSelector, HistoricalContextItem
    from core.model_capability import ProviderModelMetadata
    from core.pin_store import PinState

    meta = ProviderModelMetadata(
        model_slug="fake/m",
        context_limit=TokenEstimate(4000, "config_static", "estimated"),
        model_max_output=TokenEstimate(
            512, "config_static", "estimated"
        ),
        fetched_at="2026-09-04T00:00:00Z",
        fetch_status="ok",
    )
    req = TokenEstimate(512, "config_static", "estimated")
    fixed = make_fixed_input_tokens(system_text="sys", current_user_text="u")
    pin_state = PinState(session_id="s", pinned_round_ids=[], history=[])
    return ContextSelector(), meta, req, fixed, pin_state, HistoricalContextItem


def test_context_selector_accepts_compacted_items(tmp_path):
    """S3.8：ContextSelector 把 compacted_items 放进 result.compacted
    并回填 compacted_tokens（默认 None = S3.5 行为不变）。"""
    (sel, meta, req, fixed, pin_state, HCI) = _budget_scaffold(tmp_path)

    item = HCI(
        round_id=1, user_question="",
        answers=[("__compacted__", "参与者 a 认为 X。")],
        source="compacted", token_estimate=_te(50),
    )
    res = sel.build(
        metadata=meta, request_max_output=req,
        valid_pool=[], pin_state=pin_state, fixed=fixed, k=0.5,
        compacted_items=[item],
    )
    assert len(res.compacted) == 1
    assert res.budget.compacted_tokens.value >= 50
    assert res.budget.compaction_budget.value > 0


def test_context_selector_default_has_no_compaction():
    """不传 compacted_items → S3.5 行为（compacted 空、compacted_tokens=0）。"""
    (sel, meta, req, fixed, pin_state, HCI) = _budget_scaffold(Path(tempfile.mkdtemp()))
    res = sel.build(
        metadata=meta, request_max_output=req,
        valid_pool=[], pin_state=pin_state, fixed=fixed, k=0.5,
    )
    assert res.compacted == []
    assert res.budget.compacted_tokens.value == 0


def test_compaction_budget_overflow_flagged(tmp_path):
    """compacted 超出 compaction_budget → 记 overflow 并裁剪（不静默丢弃）。"""
    (sel, meta, req, fixed, pin_state, HCI) = _budget_scaffold(tmp_path)

    # 构造一个远超 compaction_budget 的压缩块
    huge = HCI(
        round_id=1, user_question="",
        answers=[("__compacted__", "参与者 a 认为：" + "内容。" * 5000)],
        source="compacted", token_estimate=_te(999999),
    )
    res = sel.build(
        metadata=meta, request_max_output=req,
        valid_pool=[], pin_state=pin_state, fixed=fixed, k=0.5,
        compacted_items=[huge],
    )
    assert res.overflow.get("compacted_exceeds_budget") is True
    assert "compaction_budget" in res.overflow
    # 超限 → 被裁剪掉
    assert res.compacted == []


# ── 8. Prompt 渲染：Compacted 块不被 peer_review 过滤剔除 ───────────────


def test_compacted_block_survives_peer_review_off(tmp_path):
    """回归：peer_review=False 的 self-only 过滤不能剔除 Session 级共享块。"""
    from core.context_selector import HistoricalContextItem
    from core.prompt_builder import build_round_n_prompt_v3
    from core.state import ModelParticipant

    rec = CompactionRecord(
        attempt=1, source_rounds=[1, 2],
        contributor_models=["qwen-plus", "gpt-4o-mini"],
        viewpoint_attribution=True, input_tokens=_te(10),
        expected_summary_tokens=_te(3), actual_summary_tokens=_te(4),
        status="success", summary_file="compactions/compact_001.md",
    )
    blk = CompactedBlock(
        content=(
            "## 各参与者观点\n"
            "参与者 Candidate B 认为：方案 X。\n"
            "参与者 Candidate A 认为：方案 Y。\n"
        ),
        source_files=["compactions/compact_001.md"],
        records=[rec],
        contributor_models=["qwen-plus", "gpt-4o-mini"],
    )
    item = HistoricalContextItem(
        round_id=1, user_question="",
        answers=[("__compacted__", render_compacted_block(blk))],
        source="compacted", token_estimate=_te(20),
    )
    parts = [
        ModelParticipant(id="qwen-plus", display_name="Qwen Plus",
                         provider="mock", model="qwen-plus", enabled=True),
        ModelParticipant(id="gpt-4o-mini", display_name="GPT-4o mini",
                         provider="mock", model="gpt-4o-mini", enabled=True),
    ]
    for pr in (True, False):
        prompt, _ = build_round_n_prompt_v3(
            original_question="原始问题", user_instruction="本轮指令",
            historical_items=[(1, item)], peer_context_items=[],
            self_model_id="qwen-plus",
            candidate_ids={
                "qwen-plus": "Candidate-A",
                "gpt-4o-mini": "Candidate-B",
            },
            peer_review=pr, participants=parts,
        )
        # Session 级共享块在两种 peer_review 设置下都必须保留
        assert "历史压缩摘要" in prompt, f"peer_review={pr} 时 Compacted 块丢失"
        assert "参与者 Candidate B 认为" in prompt
        assert "Session 级共享" in prompt
        # A3：Compacted 块不得把真实参与者身份带进任何模型的 Prompt
        for real_id in ("qwen-plus", "gpt-4o-mini"):
            assert real_id not in prompt, (
                f"peer_review={pr} 时 Prompt 泄漏真实 model_id: {real_id}"
            )


# ── 9. 真链路行为证明 ────────────────────────────────────────────────────


def _run_real_rounds(n_rounds: int, peer_review: bool = True, post_hook=None):
    """真实链路：SessionManager + RoundManager 跑 N 轮（大内容触发 Compaction）。

    ⚠ TemporaryDirectory 在本函数返回时销毁 —— 所有文件系统检查必须在
      with 块**内**完成。返回值：
      (prompts, events, dir_state, hook_result)
      dir_state 在块内收集；post_hook 在块内执行（供 raw-archive 快照对比）。
    """
    os.environ.setdefault("CROSSLLM_MOCK_DELAY_SCALE", "0")
    from core.model_manager import ModelRegistry
    from core.round_manager import RoundManager
    from core.session_manager import SessionManager
    from core.storage import Storage

    class _Hybrid:
        """回答输出大内容（挤爆 lossless_budget）；压缩输出带归属。

        ⚠ A3（Phase 4.5 修复）：压缩输入用匿名 Candidate 标签标注参与者
        （mock 模型集 {deepseek, gpt, qwen} → A/B/C），输出按同一契约回抄标签；
        回抄真实 model_id 会被 F2b 身份校验判为泄漏。
        """

        def __init__(self, mid):
            self.mid = mid

        async def generate(self, round_id, prompt, max_output_tokens=None):
            if "多参与者对话历史" in prompt:
                yield "## 用户目标\n设计 OCR 架构。\n\n## 各参与者观点\n"
                yield "参与者 Candidate A 认为：方案 X（分层缓存）更稳妥。\n"
                yield "参与者 Candidate B 认为：方案 Y（全量重算）更简单。\n"
                yield "参与者 Candidate C 反对方案 X，理由是运维复杂度上升。\n\n"
                yield "## 分歧\nX vs Y 未收敛。\n\n"
                yield "## 未验证假设\n长尾占比尚无数据（未验证）。\n"
            else:
                yield f"[{self.mid}] 详细分析：" + ("论证内容。" * 1100) + "\n"

    with tempfile.TemporaryDirectory() as td:
        storage = Storage(Path(td) / "s")
        registry = ModelRegistry()
        # §一：注入确定性模型能力（生产链路走 Provider 元数据）。数值沿用改动前的
        # 内建默认，因此本文件既有的预算断言数字不变。
        rounds = RoundManager(
            storage, registry, _Hybrid,
            capability_resolver=StubCapabilityResolver(
                context_limit=8192, model_max_output=2048,
            ),
        )
        sessions = SessionManager(storage, registry)
        sess = sessions.create("OCR 多模态文档解析架构怎样设计？")
        sid = sess.session_id

        async def _drive(s):
            for i in range(n_rounds):
                instr = "请给出方案。" if i == 0 else f"第{i + 1}轮：请评价。"
                rs, bus = rounds.prepare_round(s, instr, peer_review=peer_review)
                await rounds.execute_round(s.session_id, rs, bus)
                s = sessions.get(s.session_id)
            return s

        sess = asyncio.run(_drive(sess))

        prompts = {}
        pdir = storage.round_dir(sid, n_rounds)
        for pf in sorted(pdir.glob("*.prompt.md")):
            prompts[pf.name.replace(".prompt.md", "")] = pf.read_text(
                encoding="utf-8"
            )
        policy_path = storage.session_dir(sid) / "context_policy.jsonl"
        events = []
        if policy_path.exists():
            events = [
                json.loads(l)
                for l in policy_path.read_text(encoding="utf-8").splitlines()
                if l.strip()
            ]

        # ── 块内收集目录状态（TemporaryDirectory 返回后即销毁）────────
        sdir = storage.session_dir(sid)
        comp_dir = sdir / COMPACTIONS_DIRNAME
        comp_files = {}
        if comp_dir.is_dir():
            for p in sorted(comp_dir.glob("compact_*.md")):
                meta = None
                raw = p.read_text(encoding="utf-8")
                if raw.startswith("---\n"):
                    end = raw.find("\n---\n", 4)
                    if end >= 0:
                        try:
                            meta = json.loads(raw[4:end])
                        except json.JSONDecodeError:
                            meta = None
                comp_files[p.name] = meta
        summaries_state = {}
        sm = sdir / "summaries"
        if sm.is_dir():
            for d in sm.iterdir():
                if d.is_dir():
                    summaries_state[d.name] = sorted(
                        f.name for f in d.glob("*.md")
                    )
        dir_state = {
            "compactions_exists": comp_dir.is_dir(),
            "compactions_files": comp_files,
            "summaries_exists": sm.is_dir(),
            "summaries": summaries_state,
            "raw_snapshot": {
                k: v for k, v in _snapshot(sdir).items()
                if k.startswith("round_") or k == "state.json"
            },
        }

        hook_result = post_hook(storage, sid) if post_hook else None
        return prompts, events, dir_state, hook_result


def _val(d):
    return d.get("value") if isinstance(d, dict) else d


def test_real_chain_compaction_produces_compacted_block_in_prompt():
    """任务书 §13：真链路多 Round → Prompt 中可观察到 Compacted 块 + 归属。"""
    prompts, events, _dir_state, _hook = _run_real_rounds(6)
    assert prompts, "应至少落盘一个 prompt"

    comp = [e for e in events if e["event"] == "compaction"]
    assert comp, "多轮大内容应触发 Compaction"

    body = next(iter(prompts.values()))
    assert "## 历史压缩摘要" in body
    assert "Session 级共享" in body
    assert "非本模型个人总结" in body
    # 观点归属可辨认（任务书 §13 第 3 项）—— 匿名标签，非真实 model_id
    assert "参与者 Candidate A 认为" in body
    assert "参与者 Candidate B 认为" in body
    assert "参与者 Candidate C 反对方案 X" in body
    assert "大家最终认为" not in body
    # A3：模型可见 Prompt 中不得出现任何真实参与者身份
    for real_id in ("qwen", "gpt", "deepseek"):
        assert f"参与者 {real_id}" not in body, (
            f"Compacted History 泄漏真实 model_id: {real_id}"
        )


def test_real_chain_budget_identity_holds():
    """任务书 §13：Input + Reserved Output <= Context Limit。"""
    prompts, events, _dir_state, _hook = _run_real_rounds(6)

    budgets = [e for e in events if e["event"] == "context_budget"]
    assert budgets
    last = budgets[-1]

    cl = _val(last["context_limit"])
    fi = _val(last["fixed_input_tokens"])
    ro = _val(last["reserved_output_tokens"])
    pt = _val(last["pinned_tokens"])
    rt = _val(last["recent_raw_tokens"])
    ct = _val(last["compacted_tokens"])

    # history_budget 恒等式（§21）
    assert _val(last["history_budget"]) == cl - fi - ro
    # 最终约束
    total = fi + pt + rt + ct + ro
    assert total <= cl, (
        f"Input({total}) 超过 Context Limit({cl})；"
        f"fixed={fi} pinned={pt} recent_raw={rt} compacted={ct} reserved={ro}"
    )
    # S3.8 确实消费了 compaction_budget（compacted_tokens > 0 且 ≤ budget）
    assert ct > 0, "S3.8 应真正把 Compacted 块计入 ContextBudget"
    assert ct <= _val(last["compaction_budget"])


def test_real_chain_compaction_dir_isolated_from_summaries():
    """真链路下 compactions/ 与 summaries/ 并存且互不污染。"""
    prompts, events, dir_state, _hook = _run_real_rounds(6)

    assert dir_state["compactions_exists"], "compactions/ 应存在"
    assert dir_state["compactions_files"], "应落盘 compact_NNN.md"

    # 每个落盘文件的 artifact_kind 必须是 session_compaction
    for name, meta in dir_state["compactions_files"].items():
        assert meta is not None, f"{name} frontmatter 解析失败"
        assert meta["artifact_kind"] == ARTIFACT_KIND
        assert meta["artifact_kind"] != "conversation_summary"

    # summaries/ 若存在（Peer Review 生成），其文件必须是 summary_NNN.md
    if dir_state["summaries_exists"]:
        for model, files in dir_state["summaries"].items():
            for fname in files:
                assert fname.startswith("summary_"), (
                    f"summaries/{model}/ 下出现非 summary 文件: {fname}"
                )


def test_real_chain_raw_archive_not_modified_within_round():
    """真链路：Compaction 只新增 compactions/* 与日志，不改 round_NNN/*。

    post_hook 在 TemporaryDirectory 存活期内执行快照对比。
    """
    def _hook(storage, sid):
        sdir = storage.session_dir(sid)
        before = {
            k: v for k, v in _snapshot(sdir).items()
            if k.startswith("round_") or k == "state.json"
        }
        svc = CompactionService(storage, provider_factory=None)
        rounds = [
            _VR(1, {"qwen": "A1", "gpt": "B1"}),
            _VR(2, {"qwen": "A2", "gpt": "B2"}),
        ]
        asyncio.run(svc.compact(session_id=sid, rounds=rounds,
                                compaction_budget=1000))
        after = {
            k: v for k, v in _snapshot(sdir).items()
            if k.startswith("round_") or k == "state.json"
        }
        changed = [k for k, h in before.items() if after.get(k) != h]
        added = set(after) - set(before)
        return changed, added

    prompts, events, _dir_state, (changed, added) = _run_real_rounds(
        4, post_hook=_hook
    )
    assert changed == [], f"Compaction 修改了 Raw Archive: {changed}"
    for k in added:
        assert (
            Path(k).parts[0] == COMPACTIONS_DIRNAME
            or k == "context_policy.jsonl"
        ), f"Compaction 越界写入: {k}"


# ── 10. 守门 ─────────────────────────────────────────────────────────────


# ==========================================================================
# Phase 4 A3 身份泄漏四层防线 —— 原 test_phase4_compaction_identity.py
# Phase 4 — Compaction A3 身份泄漏修复专项测试（单点 Bug 修复）。
#
# 规范依据：
#   architecture_v0.1.md  A1-A4（匿名交叉注入不变量）
#   core/anonymizer.py    模块说明："系统生成文本硬校验 / LLM 正文软扫描"
#   docs/phase4_memory.md §37（Compaction 是 Session 级共享产物，会进入所有模型）
#
# 修复前的确定性泄漏链：
#
#     compaction._render_input()           真实 model_id → 压缩输入
#     compaction.render_compacted_block()  真实 contributor_models → 块标题
#     prompt_builder._make_item()          mid="__compacted__" → 骨架为空
#                                          → assert_no_identity_leak 完全绕过
#
# 修复后的四层：
#
#     L1 输入层  _render_input()            → assign_candidate_ids 的匿名标签
#     L2 输出层  find_attributed_identity_leak()
#                                          → 回抄真实 id ⇒ F2 同一降级路径
#     L3 渲染层  render_compacted_block()   → 标题不写真实 contributor_models
#     L4 终检    prompt_builder 骨架硬校验   → 覆盖 Compacted 块的系统生成头部
#
# 覆盖（对应任务书 §五 的 Test 1-5）：
#   Test 1  压缩输入不含真实 model_id
#   Test 2  压缩输出回抄真实 id → 被拒绝（不落盘 / 不可注入）
#   Test 3  匿名标签仍保留"不同参与者观点"的区分度
#   Test 4  每个模型的最终 Prompt 都看不到真实参与者身份
#   Test 5  正常（无泄漏）的 Compaction 不被错误拒绝
# ==========================================================================

MODELS = ["gpt-4o-mini", "qwen-plus"]


def _labels_for(model_ids: list[str]) -> dict[str, str]:
    """与 compaction 输入保持同源的标签映射（assign_candidate_ids 同规则）。"""
    from core.anonymizer import assign_candidate_ids

    return assign_candidate_ids(model_ids)


class _CaptureFactory:
    """回抄**匿名标签**的压缩 provider，并记录收到的全部 prompt。"""

    def __init__(self, model_ids: list[str]):
        labels = _labels_for(model_ids)
        self.prompts: list[str] = []
        self._labels = labels

    def __call__(self, mid):
        outer = self

        class _P:
            def generate(self, round_id, prompt="", max_output_tokens=None):
                outer.prompts.append(prompt)
                lines = ["## 各参与者观点"]
                for i, label in enumerate(outer._labels.values()):
                    lines.append(f"参与者 {label} 认为：方案 {chr(88 - i)} 更稳妥。")
                yield "\n".join(lines) + "\n"

        return _P()


class _LeakFactory:
    """把**真实 model_id** 当参与者身份回抄（修复前的 LLM 行为 / 不守规矩的 LLM）。"""

    def __init__(self, model_ids: list[str]):
        self.model_ids = list(model_ids)
        self.prompts: list[str] = []

    def __call__(self, mid):
        outer = self

        class _P:
            def generate(self, round_id, prompt="", max_output_tokens=None):
                outer.prompts.append(prompt)
                yield "## 各参与者观点\n"
                for i, real in enumerate(outer.model_ids):
                    yield f"参与者 {real} 认为：方案 {chr(88 - i)} 更稳妥。\n"

        return _P()


def _make_svc_id(tmp_path: Path, factory):
    storage = Storage(tmp_path / "sessions")
    return CompactionService(storage, provider_factory=factory), storage


def _rounds(model_ids: list[str], n: int = 2) -> list[_VR]:
    """构造 ValidRound 替身。

    ⚠ 回答正文刻意**不含** model_id：正文是模型自己写的内容，不属于系统注入的
    身份信息（anonymizer 的校验范围约定）。本文件要证明的是**系统生成部分**
    （`## 第 N 轮` / 参与者归属标签）不泄漏身份。
    """
    return [
        _VR(r, {m: f"第 {r} 轮的回答正文（{i}）" for i, m in enumerate(model_ids)},
            f"第 {r} 轮问题")
        for r in range(1, n + 1)
    ]


def test_compaction_input_has_no_real_model_id(tmp_path):
    """压缩 Prompt（含 ValidRound 渲染）中不得出现任何真实 model_id。"""
    factory = _CaptureFactory(MODELS)
    svc, storage = _make_svc_id(tmp_path, factory)
    sid = "s-in"
    storage.ensure_session_dir(sid)

    asyncio.run(svc.compact(session_id=sid, rounds=_rounds(MODELS),
                            compaction_budget=1000))

    assert factory.prompts, "压缩 provider 未被调用"
    labels = _labels_for(MODELS)
    for prompt in factory.prompts:
        for real in MODELS:
            assert real not in prompt, (
                f"压缩输入泄漏真实 model_id {real!r}"
            )
        # 系统生成的归属标注必须是匿名标签（否则观点归属语义也会丢失）
        for label in labels.values():
            assert f"参与者 {label}：" in prompt
        assert f"参与者 {MODELS[0]}：" not in prompt


def test_compaction_input_labels_are_shared_and_deterministic(tmp_path):
    """标签必须是轮内全局共享的同一套（A2：模型间标签不能各分配一套）。"""
    factory = _CaptureFactory(MODELS)
    svc, storage = _make_svc_id(tmp_path, factory)
    sid = "s-labels"
    storage.ensure_session_dir(sid)

    asyncio.run(svc.compact(session_id=sid, rounds=_rounds(MODELS),
                            compaction_budget=10000))

    labels = _labels_for(MODELS)
    assert set(labels.values()) == {"Candidate A", "Candidate B"}
    prompt = factory.prompts[0]
    for label in labels.values():
        assert f"参与者 {label}：" in prompt, f"输入缺少标签 {label}"


def test_leaky_compaction_output_is_rejected(tmp_path):
    """LLM 回抄真实 model_id → status=failed、不落盘、无内容可注入。"""
    factory = _LeakFactory(MODELS)
    svc, storage = _make_svc_id(tmp_path, factory)
    sid = "s-leak"
    storage.ensure_session_dir(sid)

    blk = asyncio.run(svc.compact(session_id=sid, rounds=_rounds(MODELS),
                                  compaction_budget=1000))

    assert blk.records, "应至少有一条 record"
    rec = blk.records[-1]
    assert rec.status == "failed"
    assert rec.error_message == "identity_leak_in_compacted_content"
    # 不可注入内容
    assert blk.content == ""
    # 不落盘（原始历史不受影响，§69）
    assert not list(svc.compactions_dir(sid).glob("compact_*.md"))
    # 日志里明确记录原因
    policy = (storage.session_dir(sid) / "context_policy.jsonl").read_text(
        encoding="utf-8"
    )
    events = [
        json.loads(line) for line in policy.splitlines()
        if line.strip() and json.loads(line).get("event") == "compaction"
    ]
    assert events[-1]["status"] == "failed"
    assert events[-1]["error_message"] == "identity_leak_in_compacted_content"


def test_leaky_output_never_reaches_any_prompt(tmp_path):
    """被拒绝的产物不含任何内容 ⇒ 不可能被注入任何模型的 Prompt。"""
    from core.context_selector import HistoricalContextItem
    from core.prompt_builder import build_round_n_prompt_v3

    factory = _LeakFactory(MODELS)
    svc, storage = _make_svc_id(tmp_path, factory)
    sid = "s-leak-prompt"
    storage.ensure_session_dir(sid)

    blk = asyncio.run(svc.compact(session_id=sid, rounds=_rounds(MODELS),
                                  compaction_budget=1000))
    item = HistoricalContextItem(
        round_id=1, user_question="",
        answers=[("__compacted__", render_compacted_block(blk))],
        source="compacted", token_estimate=_te(1),
    )
    assert factory.model_ids[0] not in item.answers[0][1]


def test_labels_preserve_distinguishable_viewpoints(tmp_path):
    """压缩产物必须仍能区分不同参与者（不得压成"大家最终认为"）。"""
    factory = _CaptureFactory(MODELS)
    svc, storage = _make_svc_id(tmp_path, factory)
    sid = "s-distinguish"
    storage.ensure_session_dir(sid)

    blk = asyncio.run(svc.compact(session_id=sid, rounds=_rounds(MODELS),
                                  compaction_budget=1000))

    assert blk.records[-1].status == "success"
    labels = sorted(_labels_for(MODELS).values())
    assert len(labels) == 2
    for label in labels:
        assert f"参与者 {label} 认为" in blk.content, (
            f"压缩产物丢失参与者 {label} 的观点归属"
        )
    # 无来源统一结论仍被禁止
    assert "大家最终认为" not in blk.content
    # 内部 provenance 仍保留真实 model_id（只约束模型可见内容）
    assert blk.contributor_models == sorted(MODELS)


def _compacted_item(content: str, token: int = 20):
    from core.context_selector import HistoricalContextItem

    rec = CompactionRecord(
        attempt=1, source_rounds=[1, 2], contributor_models=list(MODELS),
        viewpoint_attribution=True, input_tokens=_te(10),
        expected_summary_tokens=_te(3), actual_summary_tokens=_te(4),
        status="success", summary_file="compactions/compact_001.md",
    )
    blk = CompactedBlock(
        content=content, source_files=["compactions/compact_001.md"],
        records=[rec], contributor_models=list(MODELS),
    )
    return HistoricalContextItem(
        round_id=1, user_question="",
        answers=[("__compacted__", render_compacted_block(blk))],
        source="compacted", token_estimate=_te(token),
    )


def test_no_model_sees_real_identity_in_compacted_history(tmp_path):
    """三个模型各自构造 Prompt：都不得出现真实参与者身份。"""
    from core.prompt_builder import build_round_n_prompt_v3
    from core.state import ModelParticipant

    factory = _CaptureFactory(MODELS)
    svc, storage = _make_svc_id(tmp_path, factory)
    sid = "s-per-model"
    storage.ensure_session_dir(sid)
    blk = asyncio.run(svc.compact(session_id=sid, rounds=_rounds(MODELS),
                                  compaction_budget=1000))

    item = _compacted_item(blk.content)
    participants = [
        ModelParticipant(id=m, display_name=m.upper(), provider="mock",
                         model=f"mock-{m}", enabled=True)
        for m in MODELS
    ]
    for self_id in MODELS:
        prompt, _ = build_round_n_prompt_v3(
            original_question="原始问题", user_instruction="本轮指令",
            historical_items=[(1, item)], peer_context_items=[],
            self_model_id=self_id, candidate_ids=_labels_for(MODELS),
            peer_review=True, participants=participants,
        )
        for real in MODELS:
            assert real not in prompt, (
                f"{self_id} 的 Prompt 出现真实身份 {real!r}"
            )
        assert COMPACTED_HEADER_PREFIX in prompt


def test_final_guard_covers_compacted_item_skeleton():
    """L4 回归：Compacted 块的**系统生成头部**必须进硬校验，不能绕过。

    用"旧版 / 外来渲染器"产生的、标题里带真实 id 的块模拟历史遗留产物：
    修复前 mid="__compacted__" 会让骨架为空 → 泄漏被静默放过；
    修复后骨架含头部 → 抛 IdentityLeakError。
    """
    from core.anonymizer import IdentityLeakError
    from core.context_selector import HistoricalContextItem
    from core.prompt_builder import build_round_n_prompt_v3
    from core.state import ModelParticipant

    legacy_rendered = (
        "## 历史压缩摘要（Session 级共享，含参与者 qwen-plus, gpt-4o-mini "
        "的观点，非本模型个人总结）\n\n正文（LLM 生成）。"
    )
    # 刻意**不**经 render_compacted_block：模拟修复前已落盘的遗留产物
    item = HistoricalContextItem(
        round_id=1, user_question="",
        answers=[("__compacted__", legacy_rendered)],
        source="compacted", token_estimate=_te(20),
    )
    participants = [
        ModelParticipant(id=m, display_name=m.upper(), provider="mock",
                         model=f"mock-{m}", enabled=True)
        for m in MODELS
    ]
    with pytest.raises(IdentityLeakError):
        build_round_n_prompt_v3(
            original_question="原始问题", user_instruction="本轮指令",
            historical_items=[(1, item)], peer_context_items=[],
            self_model_id=MODELS[0], candidate_ids=_labels_for(MODELS),
            peer_review=True, participants=participants,
        )


def test_clean_compaction_is_not_falsely_rejected(tmp_path):
    """无泄漏的正常压缩：success + 落盘 + 可注入。"""
    factory = _CaptureFactory(MODELS)
    svc, storage = _make_svc_id(tmp_path, factory)
    sid = "s-clean"
    storage.ensure_session_dir(sid)

    blk = asyncio.run(svc.compact(session_id=sid, rounds=_rounds(MODELS),
                                  compaction_budget=1000))

    assert blk.records[-1].status == "success"
    assert blk.records[-1].error_message is None
    assert blk.content
    files = sorted(p.name for p in svc.compactions_dir(sid).glob("compact_*.md"))
    assert files == ["compact_001.md"]


def test_short_model_id_labels_are_not_falsely_rejected(tmp_path):
    """短 model_id（'a'/'b'）不得因标签含同名字母而被误判为泄漏。

    这是 Phase 4.2 Summary 修复踩过的坑：`find_identity_tokens` 是子串语义，
    1 字符词元会命中任意含该字母的文本。本修复只做**归属位置完全相等**判定。
    """
    short = ["a", "b"]

    assert find_attributed_identity_leak(
        "参与者 Candidate A 认为：X", short,
    ) == []
    # 但真正的回抄仍然拦住
    assert find_attributed_identity_leak("参与者 a 认为：X", short) == ["a"]

    factory = _CaptureFactory(short)
    svc, storage = _make_svc_id(tmp_path, factory)
    sid = "s-short"
    storage.ensure_session_dir(sid)
    blk = asyncio.run(svc.compact(session_id=sid, rounds=_rounds(short),
                                  compaction_budget=1000))
    assert blk.records[-1].status == "success"


def test_render_compacted_block_header_has_no_real_id():
    """L3：标题对任意（哪怕奇怪的）contributor_models 都不泄漏。"""
    rec = CompactionRecord(
        attempt=1, source_rounds=[1], contributor_models=["Qwen", "DeepSeek"],
        viewpoint_attribution=True, input_tokens=_te(1),
        expected_summary_tokens=_te(1), actual_summary_tokens=_te(1),
        status="success",
    )
    blk = CompactedBlock(
        content="正文", source_files=["compactions/compact_001.md"],
        records=[rec], contributor_models=["Qwen", "DeepSeek"],
    )
    out = render_compacted_block(blk)
    assert out.startswith(COMPACTED_HEADER_PREFIX)
    assert "Qwen" not in out
    assert "DeepSeek" not in out
    assert "Session 级共享" in out
    assert "非本模型个人总结" in out


def test_find_attributed_identity_leak_scope():
    """判定范围：只认"归属位置 + 完全相等"，不误伤正文里的偶发提及。"""
    models = ["qwen-plus", "gpt-4o-mini"]
    # 归属位置回抄 → 命中
    assert find_attributed_identity_leak(
        "参与者 qwen-plus 认为：X", models,
    ) == ["qwen-plus"]
    assert find_attributed_identity_leak(
        "参与者：gpt-4o-mini 反对", models,
    ) == ["gpt-4o-mini"]
    assert find_attributed_identity_leak(
        "Participant qwen-plus says", models,
    ) == ["qwen-plus"]
    # 匿名标签 → 不命中
    assert find_attributed_identity_leak(
        "参与者 Candidate A 认为：X", models,
    ) == []
    # 正文偶发提及（非归属位置）→ 不命中（由 prompt_builder 软扫描负责）
    assert find_attributed_identity_leak(
        "可以借鉴 qwen-plus 的做法。", models,
    ) == []
    assert find_attributed_identity_leak("", models) == []
    assert find_attributed_identity_leak("参与者 X", []) == []
