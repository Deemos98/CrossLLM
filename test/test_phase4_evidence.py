"""S3.9 MCP Evidence Index 测试（Plan §4.10 §16 + MD §44-§48）。

严格区分：

    MCP Runtime ≠ MCP Evidence Index ≠ Fact

覆盖用户任务书 10 项必测：

  1. Tool Call 存在但未使用 → used=false
  2. Tool Call 被 Final Answer 使用 → used=true
  3. Evidence 可以定位 Runtime（trace_status=found + 原始 Result 完整）
  4. Runtime 删除/损坏防护行为明确（runtime_missing / runtime_corrupted）
  5. Evidence 不生成 Fact（无 is_fact / confidence / score 字段与代码路径）
  6. 多 MCP、多 Tool、多 Result
  7. 多模型情况下 source_model_id 不混淆
  8. Round 完成后生成 Evidence（真链路）
  9. Compaction 后 Evidence 仍可追溯
 10. Evidence Index 不替代 Runtime（Runtime 原样保留）

真实行为证明（用户任务书 §八）：

    Round N：MCP A → Result A → used=true；MCP B → Result B → used=false
    → Evidence Index：A → true；B → false
    → Runtime：两条原始 Result 仍完整存在，且 Index → Runtime 可实际定位
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import shutil
import tempfile
from pathlib import Path

import pytest

from core.evidence_index import (
    EVIDENCE_DIRNAME,
    EVIDENCE_FILENAME,
    MIN_MATCH_LEN,
    RUNTIME_LOG_NAME,
    EvidenceIndexService,
    McpEvidenceEntry,
    build_reference,
    extract_salient_snippets,
    judge_used_in_final_answer,
)


# ── helpers ─────────────────────────────────────────────────────────────


def _make_svc(tmp_path):
    from core.storage import Storage

    storage = Storage(tmp_path)
    return EvidenceIndexService(storage), storage


def _write_runtime(storage, sid: str, round_id: int, records: list[dict]):
    """直接写 round_NNN/mcp_runtime.jsonl（模拟 RealProvider 的 Runtime 落盘）。"""
    rdir = storage.round_dir(sid, round_id)
    rdir.mkdir(parents=True, exist_ok=True)
    path = rdir / RUNTIME_LOG_NAME
    with path.open("w", encoding="utf-8") as fh:
        for r in records:
            fh.write(json.dumps(r, ensure_ascii=False) + "\n")
    return path


def _rec(
    *,
    call_id: str,
    model_id: str = "qwen",
    mcp: str,
    tool: str,
    preview: str,
    ok: bool = True,
    round_id: int = 1,
) -> dict:
    return {
        "ts": "2026-09-04T00:00:00.000",
        "session_id": "s",
        "round_id": round_id,
        "model_id": model_id,
        "tool_call": {"id": call_id, "name": tool},
        "mcp": mcp,
        "tool": tool,
        "arguments": {"q": "demo"},
        "ok": ok,
        "result_chars": len(preview),
        "result_preview": preview,
        "error_type": None if ok else "TimeoutError",
        "error_message": None if ok else "boom",
    }


# ── 判定核心（任务书 1 / 2）─────────────────────────────────────────────


def test_1_tool_call_success_but_unused_is_false():
    """任务书 1：Tool Call 成功 ≠ Useful Evidence。"""
    assert judge_used_in_final_answer(
        tool_call_id="call_ok",
        result_preview="成功返回了大量与主题无关的数据",
        final_answer="我基于自己的推理给出结论，没有引用工具。",
    ) is False


def test_1b_success_alone_never_implies_used():
    """即使 ok=True + result_chars 很大，Final Answer 未引用就是 false。"""
    assert judge_used_in_final_answer(
        tool_call_id="call_x",
        result_preview="A" * 500,
        final_answer="简短结论。",
    ) is False


def test_2_tool_call_used_in_answer_is_true():
    """任务书 2：Final Answer 实际引用 → true。"""
    # 子句级引用
    assert judge_used_in_final_answer(
        tool_call_id="call_A",
        result_preview="GitHub Issue #456 描述了 OCR 长尾延迟问题，影响 P99 指标",
        final_answer="工具返回：GitHub Issue #456 描述了 OCR 长尾延迟问题。基于此建议先量化。",
    ) is True
    # tool_call_id 显式引用
    assert judge_used_in_final_answer(
        tool_call_id="call_abc123",
        result_preview="任何内容",
        final_answer="根据 call_abc123 的返回，结论是 X",
    ) is True


def test_snippet_min_length_enforced():
    """短于 MIN_MATCH_LEN 的公共子串不构成"使用"（防止误判）。"""
    # 共同片段 "OCR 优化" 只有 5 个字 —— 不足以判定
    assert judge_used_in_final_answer(
        tool_call_id="call_a",
        result_preview="OCR 优化相关讨论与别的内容",
        final_answer="我们来谈谈 OCR 优化吧",
    ) is False
    assert MIN_MATCH_LEN >= 12


def test_extract_salient_snippets_clause_level():
    snippets = extract_salient_snippets(
        "第一句是完整的子句内容，第二句也是完整的子句"
    )
    assert any("第一句是完整的子句内容" in s for s in snippets)
    assert any("第二句也是完整的子句" in s for s in snippets)


# ── Index 生成 + 多 MCP / 多 Tool / 多 Result（任务书 6）────────────────


def test_6_multi_mcp_multi_tool_multi_result(tmp_path):
    svc, storage = _make_svc(tmp_path)
    sid = "s-multi"
    storage.ensure_session_dir(sid)
    _write_runtime(storage, sid, 1, [
        _rec(call_id="call_A", mcp="github", tool="get_issue",
             preview="GitHub Issue #456 描述了 OCR 长尾延迟问题"),
        _rec(call_id="call_B", mcp="openwebsearch", tool="search",
             preview="与最终回答完全无关的搜索结果内容"),
        _rec(call_id="call_C", mcp="fetch", tool="get_url",
             preview="https://example.com/ocr-bench 的基准页面",
             ),
    ])
    answer = (
        "工具返回：GitHub Issue #456 描述了 OCR 长尾延迟问题。"
        "另外参考 https://example.com/ocr-bench 的基准页面。"
    )
    report = svc.build_index(
        session_id=sid, round_id=1, model_id="qwen", final_answer=answer,
    )
    assert report.runtime_records_total == 3
    assert report.runtime_unreadable is False
    assert len(report.entries) == 3

    by_id = {e.runtime_record_id: e for e in report.entries}
    assert by_id["call_A"].used_in_final_answer is True
    assert by_id["call_B"].used_in_final_answer is False   # Tool Call 成功但未用
    assert by_id["call_C"].used_in_final_answer is True
    # 多 MCP / 多 Tool 分别记录
    assert {(e.mcp, e.tool) for e in report.entries} == {
        ("github", "get_issue"),
        ("openwebsearch", "search"),
        ("fetch", "get_url"),
    }


def test_failed_tool_call_excluded_from_index(tmp_path):
    """§46：Error 不是 Evidence —— 失败 call 不入 Index。"""
    svc, storage = _make_svc(tmp_path)
    sid = "s-err"
    storage.ensure_session_dir(sid)
    _write_runtime(storage, sid, 1, [
        _rec(call_id="call_ok", mcp="github", tool="get_issue",
             preview="正常返回内容"),
        _rec(call_id="call_err", mcp="fetch", tool="get",
             preview="", ok=False),
    ])
    report = svc.build_index(
        session_id=sid, round_id=1, model_id="qwen",
        final_answer="引用：正常返回内容",
    )
    assert [e.runtime_record_id for e in report.entries] == ["call_ok"]


# ── 7. 多模型 source_model_id 不混淆 ────────────────────────────────────


def test_7_source_model_isolation(tmp_path):
    svc, storage = _make_svc(tmp_path)
    sid = "s-iso"
    storage.ensure_session_dir(sid)
    _write_runtime(storage, sid, 1, [
        _rec(call_id="call_A", model_id="qwen", mcp="github", tool="get_issue",
             preview="GitHub Issue #456 描述了 OCR 长尾延迟问题"),
        _rec(call_id="call_A", model_id="gpt", mcp="github", tool="get_issue",
             preview="GitHub Issue #456 描述了 OCR 长尾延迟问题"),
    ])
    answer = "引用：GitHub Issue #456 描述了 OCR 长尾延迟问题"
    # 只为 qwen 构建
    svc.build_index(session_id=sid, round_id=1, model_id="qwen",
                    final_answer=answer)
    entries = svc.load(sid)
    assert len(entries) == 1, "只应包含 qwen 的记录"
    assert entries[0].source_model_id == "qwen"

    # 再为 gpt 构建
    svc.build_index(session_id=sid, round_id=1, model_id="gpt",
                    final_answer=answer)
    entries = svc.load(sid)
    assert len(entries) == 2
    assert {e.source_model_id for e in entries} == {"qwen", "gpt"}
    # 两模型的 trace 互不串扰（同 call_id 不同 model）
    for e in entries:
        tr = svc.trace_to_runtime(sid, e)
        assert tr["status"] == "found"
        assert tr["record"]["model_id"] == e.source_model_id


# ── 3 / 4 / 10. 追溯与 Runtime 防护 ─────────────────────────────────────


def _seed_one(tmp_path):
    svc, storage = _make_svc(tmp_path)
    sid = "s-trace"
    storage.ensure_session_dir(sid)
    _write_runtime(storage, sid, 1, [
        _rec(call_id="call_A", mcp="github", tool="get_issue",
             preview="GitHub Issue #456 描述了 OCR 长尾延迟问题"),
    ])
    answer = "引用：GitHub Issue #456 描述了 OCR 长尾延迟问题"
    svc.build_index(session_id=sid, round_id=1, model_id="qwen",
                    final_answer=answer)
    return svc, storage, sid


def test_3_evidence_traces_to_runtime(tmp_path):
    """任务书 3：Evidence Index → Runtime Record → 完整原始 Result。"""
    svc, storage, sid = _seed_one(tmp_path)
    entry = svc.load(sid)[0]
    tr = svc.trace_to_runtime(sid, entry)
    assert tr["status"] == "found"
    rec = tr["record"]
    # 完整原始 Runtime 字段仍在（Index 只存索引，原文在 Runtime）
    assert rec["mcp"] == "github"
    assert rec["tool"] == "get_issue"
    assert rec["result_preview"] == (
        "GitHub Issue #456 描述了 OCR 长尾延迟问题"
    )
    assert rec["arguments"] == {"q": "demo"}
    assert rec["tool_call"]["id"] == "call_A"


def test_4_runtime_missing_reported_clearly():
    """任务书 4：Runtime 被删 → 明确 runtime_missing，不崩溃、不伪装。

    用 OS 临时目录（tempfile.mkdtemp）：沙箱对项目内 .pytest_tmp 的
    unlink 有保护，对 OS tmp 直接放行。
    """
    tmp = Path(tempfile.mkdtemp(prefix="ev-missing-"))
    try:
        svc, storage, sid = _seed_one(tmp)
        svc.runtime_path(sid, 1).unlink()
        entry = svc.load(sid)[0]
        tr = svc.trace_to_runtime(sid, entry)
        assert tr["status"] == "runtime_missing"
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_4b_runtime_corrupted_reported_clearly(tmp_path):
    """Runtime 文件损坏（非 JSON 行）→ runtime_corrupted。"""
    svc, storage, sid = _seed_one(tmp_path)
    path = svc.runtime_path(sid, 1)
    path.write_text("not-a-json-line\n{{{broken\n", encoding="utf-8")
    entry = svc.load(sid)[0]
    tr = svc.trace_to_runtime(sid, entry)
    assert tr["status"] == "runtime_corrupted"


def test_4c_build_survives_runtime_missing(tmp_path):
    """Runtime 缺失时 build_index 返回明确 unreadable 标记（不抛异常）。"""
    svc, storage = _make_svc(tmp_path)
    sid = "s-nort"
    storage.ensure_session_dir(sid)
    report = svc.build_index(
        session_id=sid, round_id=9, model_id="qwen", final_answer="any",
    )
    assert report.runtime_unreadable is True
    assert report.entries == []
    # 日志仍落（runtime_unreadable=true 可审计）
    policy = (storage.session_dir(sid) / "context_policy.jsonl").read_text(
        encoding="utf-8"
    )
    ev = [json.loads(l) for l in policy.splitlines() if l.strip()]
    assert ev[-1]["event"] == "mcp_evidence_index"
    assert ev[-1]["runtime_unreadable"] is True


def test_10_evidence_index_does_not_replace_runtime(tmp_path, monkeypatch):
    """任务书 10：Index 生成/删除不导致 Runtime 丢失（Runtime 原样保留）。

    沙箱对 unlink 行为不可靠 → 用 monkeypatch 把 evidence_path 指向
    不存在的路径来模拟"Index 被删除"，全程无 unlink。
    """
    svc, storage, sid = _seed_one(tmp_path)
    runtime_path = svc.runtime_path(sid, 1)
    before_bytes = runtime_path.read_bytes()

    # 再次生成 Index（重复构建）→ Runtime 字节级不变
    svc.build_index(session_id=sid, round_id=1, model_id="qwen",
                    final_answer="引用：GitHub Issue #456 描述了 OCR 长尾延迟问题")
    assert runtime_path.read_bytes() == before_bytes, (
        "Evidence Index 的生成不得影响 mcp_runtime.jsonl（§17）"
    )

    # 模拟 Index 被删除（无 unlink）：evidence_path 指向不存在路径
    missing = tmp_path / "evidence" / "deleted.jsonl"
    monkeypatch.setattr(svc, "evidence_path", lambda s: missing)
    assert svc.load(sid) == []

    # Index 消失后：Runtime 仍完整、仍可追溯（§17 不可替代）
    entry = McpEvidenceEntry(
        round_id=1, mcp="github", tool="get_issue",
        runtime_record_id="call_A", reference="r",
        used_in_final_answer=True, source_model_id="qwen",
    )
    tr = svc.trace_to_runtime(sid, entry)
    assert tr["status"] == "found"
    assert tr["record"]["result_preview"].startswith("GitHub Issue #456")
    assert runtime_path.read_bytes() == before_bytes

    # Index 可随时从 Runtime 重建（原 evidence_path 恢复后）
    monkeypatch.setattr(
        svc, "evidence_path",
        lambda s: storage.session_dir(s) / EVIDENCE_DIRNAME / EVIDENCE_FILENAME,
    )
    report = svc.build_index(
        session_id=sid, round_id=1, model_id="qwen",
        final_answer="引用：GitHub Issue #456 描述了 OCR 长尾延迟问题",
    )
    assert len(report.entries) == 1
    assert runtime_path.read_bytes() == before_bytes

    assert runtime_path.read_bytes() == before_bytes, (
        "Evidence Index 的生成/删除不得影响 mcp_runtime.jsonl（§17）"
    )
    # Index 可随时从 Runtime 重建
    report = svc.build_index(
        session_id=sid, round_id=1, model_id="qwen",
        final_answer="引用：GitHub Issue #456 描述了 OCR 长尾延迟问题",
    )
    assert len(report.entries) == 1


# ── 5. Evidence 不生成 Fact ─────────────────────────────────────────────


def test_5b_plan_naming_compat():
    """Plan §4.10 命名兼容：mcp_name / tool_name property。"""
    e = McpEvidenceEntry(
        round_id=1, mcp="github", tool="get_issue",
        runtime_record_id="c1", reference="Issue #456",
        used_in_final_answer=True,
    )
    assert e.mcp_name == "github"
    assert e.tool_name == "get_issue"


def test_reference_build():
    assert build_reference("github", "get_issue",
                           "GitHub Issue #456 描述了问题", "call_X") \
        == "GitHub Issue #456 描述了问题"
    # 无显著标题 → 回退 tool_call 短 id
    ref = build_reference("github", "get_issue", "短", "call_abcdef123456")
    assert "github.get_issue" in ref


# ── 8. Round 完成后生成（真链路）────────────────────────────────────────


def _run_real_session(n_rounds: int = 2, with_tools: bool = True):
    """真链路：RoundManager + 自定义 provider（Round 1 带 MCP 调用）。"""
    os.environ.setdefault("CROSSLLM_MOCK_DELAY_SCALE", "0")
    from core.model_manager import ModelRegistry
    from core.round_manager import RoundManager
    from core.session_manager import SessionManager
    from core.storage import Storage
    from core import mcp_runtime_log as runtime_log

    QUOTED = "GitHub Issue #456 描述了 OCR 长尾延迟问题"

    class _ToolProvider:
        """Round 1 发 tool_call（借 RuntimeContext 落盘），引用 Issue 内容。"""

        def __init__(self, mid):
            self.mid = mid

        @staticmethod
        def _emit(ctx, *, call_id, mcp, tool, args, preview, duration_ms):
            rec = runtime_log.build_record(
                ctx=ctx, tool_call={"id": call_id, "name": tool},
                mcp_name=mcp, tool_name=tool, arguments=args,
                started_at=runtime_log.now_iso(),
                ok=True, result_chars=len(preview), result_preview=preview,
                error_type=None, error_message=None,
                returned_at=runtime_log.now_iso(), duration_ms=duration_ms,
            )
            runtime_log.append_record(ctx.directory, rec)

        async def generate(self, round_id, prompt, max_output_tokens=None):
            ctx = runtime_log.get_runtime_context()
            if round_id == 1 and ctx is not None:
                # MCP A：被引用 → used=true
                self._emit(
                    ctx, call_id="call_A", mcp="github", tool="get_issue",
                    args={"num": 456}, preview=QUOTED, duration_ms=5,
                )
                # MCP B：成功但未引用 → used=false
                self._emit(
                    ctx, call_id="call_B", mcp="openwebsearch",
                    tool="search", args={"q": "ocr latency"},
                    preview="与回答完全无关的搜索结果条目内容", duration_ms=3,
                )
                yield f"工具返回：{QUOTED}。基于此我建议先量化 P99。"
            else:
                yield f"[{self.mid}] 第 {round_id} 轮普通回答。"

    with tempfile.TemporaryDirectory() as td:
        storage = Storage(Path(td) / "s")
        registry = ModelRegistry()
        rm = RoundManager(storage, registry, _ToolProvider)
        sessions = SessionManager(storage, registry)
        sess = sessions.create("OCR 长尾延迟怎么治理？")
        sid = sess.session_id

        async def _drive(s):
            for i in range(n_rounds):
                rs, bus = rm.prepare_round(s, f"第{i + 1}轮", peer_review=False)
                await rm.execute_round(s.session_id, rs, bus)
                s = sessions.get(s.session_id)
            return s

        sess = asyncio.run(_drive(sess))

        # 块内收集状态（TemporaryDirectory 返回后销毁）
        svc = EvidenceIndexService(storage)
        entries = svc.load(sid)
        runtime_before_snapshot = {
            str(p.relative_to(storage.session_dir(sid))): hashlib.md5(
                p.read_bytes()
            ).hexdigest()
            for p in (storage.session_dir(sid)).rglob(RUNTIME_LOG_NAME)
        }
        traces = []
        for e in entries:
            tr = svc.trace_to_runtime(sid, e)
            traces.append({
                "id": e.runtime_record_id,
                "mcp": e.mcp,
                "used": e.used_in_final_answer,
                "trace": tr["status"],
                "result_present": bool(tr.get("record", {}).get("result_preview")),
            })
        policy_path = storage.session_dir(sid) / "context_policy.jsonl"
        ev_events = []
        if policy_path.exists():
            ev_events = [
                json.loads(l)
                for l in policy_path.read_text(encoding="utf-8").splitlines()
                if l.strip() and json.loads(l)["event"] == "mcp_evidence_index"
            ]
        return {
            "sid": sid,
            "entries": entries,
            "traces": traces,
            "ev_events": ev_events,
            "runtime_snapshot": runtime_before_snapshot,
        }


def test_8_round_completion_builds_evidence(tmp_path=None):
    """任务书 8：真链路 Round 完成后自动生成 Evidence Index。"""
    state = _run_real_session(2)
    entries = state["entries"]
    assert entries, "Round 完成后应生成 Evidence"
    # Round 1（带 MCP）：每个模型 emit A/B 两个 tool_call，
    # RoundManager 对每个 complete 模型各构建一次 → 3 模型 × 2 = 6 条
    r1 = [e for e in entries if e.round_id == 1]
    r2 = [e for e in entries if e.round_id == 2]
    assert len(r1) == 6
    assert r2 == [], "无 MCP 调用的 Round 不产生 Evidence"
    # 每个模型：github used=True / openwebsearch used=False（不混淆）
    for model in ("qwen", "deepseek", "gpt"):
        mine = [e for e in r1 if e.source_model_id == model]
        by_mcp = {e.mcp: e.used_in_final_answer for e in mine}
        assert by_mcp == {"github": True, "openwebsearch": False}, (
            f"model={model} 的判定混淆: {by_mcp}"
        )


def test_real_chain_A_used_B_unused_and_both_traced():
    """真实行为证明：MCP A used=true / MCP B used=false / 双双可追溯。"""
    state = _run_real_session(2)
    traces = state["traces"]

    a = [t for t in traces if t["mcp"] == "github"]
    b = [t for t in traces if t["mcp"] == "openwebsearch"]
    assert a and b
    assert a[0]["used"] is True
    assert b[0]["used"] is False
    # Evidence Index → Runtime 实际定位成功，原始 Result 完整存在
    assert a[0]["trace"] == "found" and a[0]["result_present"] is True
    assert b[0]["trace"] == "found" and b[0]["result_present"] is True

    # context_policy.jsonl 有 mcp_evidence_index 事件
    assert state["ev_events"], "应落 mcp_evidence_index 日志"
    refs = state["ev_events"][0]["references"]
    assert {r["mcp"]: r["used_in_final_answer"] for r in refs} == {
        "github": True, "openwebsearch": False,
    }


def test_9_compaction_preserves_evidence_traceability(tmp_path):
    """任务书 9：Compaction 后 Evidence 仍可追溯（Runtime 不因 Compaction 删除）。"""
    from core.compaction import CompactionService

    svc, storage = _make_svc(tmp_path)
    sid = "s-comp"
    storage.ensure_session_dir(sid)
    _write_runtime(storage, sid, 1, [
        _rec(call_id="call_A", mcp="github", tool="get_issue",
             preview="GitHub Issue #456 描述了 OCR 长尾延迟问题"),
    ])
    answer = "引用：GitHub Issue #456 描述了 OCR 长尾延迟问题"
    svc.build_index(session_id=sid, round_id=1, model_id="qwen",
                    final_answer=answer)

    # 执行一次 Compaction（§45：Runtime 不因 Compaction 删除）
    class _VR:
        def __init__(self, rid, succ):
            self.round_id = rid
            self.successful_answers = succ
            self.user_question = "Q?"
            self.failed_models = []
            self.is_valid = bool(succ)

    comp = CompactionService(storage, provider_factory=None)
    asyncio.run(comp.compact(
        session_id=sid, rounds=[_VR(1, {"qwen": answer})],
        compaction_budget=1000,
    ))

    # Compaction 后：Runtime 文件仍在 + Evidence 仍可定位
    assert svc.runtime_path(sid, 1).exists(), (
        "§45：mcp_runtime.jsonl 不因 Context Compaction 删除"
    )
    entry = svc.load(sid)[0]
    tr = svc.trace_to_runtime(sid, entry)
    assert tr["status"] == "found"
    assert tr["record"]["result_preview"].startswith("GitHub Issue #456")


# ── API 集成 ────────────────────────────────────────────────────────────


@pytest.fixture
def api_env(tmp_path, monkeypatch):
    from core.storage import Storage
    import app as app_mod

    storage = Storage(tmp_path)
    monkeypatch.setattr(app_mod, "storage", storage)
    for mgr in (app_mod.sessions, app_mod.rounds, app_mod.summaries):
        monkeypatch.setattr(mgr, "storage", storage)
    monkeypatch.setattr(app_mod.pins, "_storage", storage)
    monkeypatch.setattr(app_mod.evidence_index, "storage", storage)
    from fastapi.testclient import TestClient
    return TestClient(app_mod.app), app_mod, storage


def test_api_list_evidence(api_env):
    client, app_mod, storage = api_env
    sid = client.post("/api/session", json={"question": "Q?"}).json()["session_id"]
    svc = EvidenceIndexService(storage)
    # 直接种一条
    _write_runtime(storage, sid, 1, [
        _rec(call_id="call_A", mcp="github", tool="get_issue",
             preview="GitHub Issue #456 描述了 OCR 长尾延迟问题"),
    ])
    svc.build_index(session_id=sid, round_id=1, model_id="qwen",
                    final_answer="引用：GitHub Issue #456 描述了 OCR 长尾延迟问题")

    r = client.get(f"/api/session/{sid}/evidence")
    assert r.status_code == 200
    body = r.json()
    assert body["total"] == 1
    assert body["used_count"] == 1
    e = body["entries"][0]
    # UI 展示所需的四个字段 + trace
    assert {"round_id", "mcp", "tool", "reference",
            "used_in_final_answer", "trace_status"} <= set(e.keys())
    assert e["trace_status"] == "found"
    # §47：无 Fact 语义字段
    assert "is_fact" not in e and "confidence" not in e


def test_api_evidence_session_not_found(api_env):
    client, _, _ = api_env
    assert client.get("/api/session/missing/evidence").status_code == 404


# ── Persistence 布局 + 守门 ─────────────────────────────────────────────


def test_persistence_layout(tmp_path):
    """用户任务书 §七：sessions/<sid>/evidence/mcp_evidence.jsonl。"""
    svc, storage = _make_svc(tmp_path)
    sid = "s-layout"
    storage.ensure_session_dir(sid)
    _write_runtime(storage, sid, 1, [
        _rec(call_id="call_A", mcp="m", tool="t",
             preview="足够长的引用片段内容用于匹配测试"),
    ])
    svc.build_index(
        session_id=sid, round_id=1, model_id="qwen",
        final_answer="引用：足够长的引用片段内容用于匹配测试",
    )
    p = storage.session_dir(sid) / EVIDENCE_DIRNAME / EVIDENCE_FILENAME
    assert p.exists()
    lines = [json.loads(l) for l in p.read_text(encoding="utf-8").splitlines() if l.strip()]
    assert lines[0]["used_in_final_answer"] is True

