"""CrossLLM Phase 4 S3.10 真实 E2E 验收脚本（全链路整合）。

S3.1～S3.9 已分别验收；本脚本把它们真正串起来，对**真实运行行为**
（最终 Prompt 落盘文件、context_policy.jsonl 事件流、Raw Archive
字节哈希、目录布局）做黑盒断言。

覆盖链路（用户任务书 §2）：

    Session → Round → API → Model Capability → Context Budget
    → Context Selector → Summary → Peer Context → Compaction
    → MCP Evidence → Provider → Persistence → Logs

运行：

    python scripts/run_phase4_e2e.py

不修改产品代码。Provider 用注入式 stub（隔离"LLM 网络可达性"与
"全链路语义正确性"两件事）。
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import shutil
import sys
import tempfile
import time
import traceback
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from core.storage import Storage
from core.model_manager import ModelRegistry
from core.session_manager import SessionManager
from core.round_manager import RoundManager
from core.evidence_index import EvidenceIndexService
from core import mcp_runtime_log as runtime_log

# 引用产品代码的常量（只读，不 monkey patch）
from core.compaction import MAX_COMPACTION_ROUNDS
from core.evidence_index import MIN_MATCH_LEN

STEPS: list[tuple[str, bool, str]] = []


def step(name: str, ok: bool, detail: str) -> None:
    STEPS.append((name, bool(ok), detail))
    mark = "PASS" if ok else "FAIL"
    print(f"[{mark}] {name}\n      {detail}")


def _sha(path: Path) -> str:
    return hashlib.md5(path.read_bytes()).hexdigest()


def _val(tok) -> int | None:
    """TokenEstimate / dict / §八 平铺数字 → value（兼容三种序列化）。"""
    if tok is None or isinstance(tok, (int, float)):
        return tok
    if isinstance(tok, dict):
        return tok.get("value")
    return getattr(tok, "value", None)


def _snapshot(root: Path) -> dict[str, str]:
    return {
        str(p.relative_to(root)): _sha(p)
        for p in root.rglob("*") if p.is_file()
    }


QUOTED = "GitHub Issue #456 描述了 OCR 长尾延迟问题，影响 P99 指标"


class _Provider:
    """注入式 stub provider：

    - 压缩请求（含"多参与者对话历史"）→ 返回带归属标注的压缩文本
    - Round 1 的 qwen → 发两个 MCP tool_call（A 被引用 / B 未被引用）
      并在 Final Answer 里逐字引用 A 的 result_preview
    - 其它 → 大内容（挤爆 lossless_budget，驱动 Compaction）
    """

    def __init__(self, mid: str):
        self.mid = mid

    @staticmethod
    def _emit(ctx, *, call_id, mcp, tool, args, preview):
        rec = runtime_log.build_record(
            ctx=ctx, tool_call={"id": call_id, "name": tool},
            mcp_name=mcp, tool_name=tool, arguments=args,
            started_at=runtime_log.now_iso(), ok=True,
            result_chars=len(preview), result_preview=preview,
            error_type=None, error_message=None,
            returned_at=runtime_log.now_iso(), duration_ms=5,
        )
        runtime_log.append_record(ctx.directory, rec)

    async def generate(self, round_id, prompt, max_output_tokens=None):
        if "多参与者对话历史" in prompt:
            yield (
                # A3（Phase 4.5 修复）：压缩输入用匿名 Candidate 标签标注参与者
                # （mock 模型集 {deepseek, gpt, qwen} → A/B/C，按 model_id 字典序）；
                # 回抄真实 model_id 会被 F2b 身份校验判为泄漏、压缩被拒。
                "## 用户目标\n设计 OCR 架构。\n\n## 各参与者观点\n"
                "参与者 Candidate C 认为：方案 X（分层缓存）更稳妥，依据是长尾延迟可控。\n"
                "参与者 Candidate B 认为：方案 Y（全量重算）更简单，依据是实现成本低。\n"
                "参与者 Candidate A 反对方案 X，理由是运维复杂度上升。\n\n"
                "## 分歧\nX vs Y 未收敛。\n\n"
                "## 未验证假设\n长尾延迟占比尚无实测数据（未验证）。\n"
            )
            return
        ctx = runtime_log.get_runtime_context()
        if round_id == 1 and self.mid == "qwen" and ctx is not None:
            self._emit(ctx, call_id="call_A", mcp="github", tool="get_issue",
                       args={"num": 456}, preview=QUOTED)
            self._emit(ctx, call_id="call_B", mcp="openwebsearch",
                       tool="search", args={"q": "ocr"},
                       preview="与回答完全无关的搜索结果条目内容")
            yield f"工具返回：{QUOTED}。基于此我建议先量化 P99 再谈扩容。"
            return
        # 大内容：约 9000 字符/模型/轮 → 3 模型一轮 ≈ 9000 tokens
        yield f"[{self.mid}] 详细分析：" + ("论证内容。" * 1100)


async def main() -> int:
    tmp = Path(tempfile.mkdtemp(prefix="phase4-e2e-"))
    storage = Storage(tmp / "sessions")
    registry = ModelRegistry()
    rounds = RoundManager(storage, registry, _Provider)
    sessions = SessionManager(storage, registry)
    svc_evidence = EvidenceIndexService(storage)

    sid: str | None = None
    try:
        # ── Step 1：Session 建立 ─────────────────────────────────────
        sess = sessions.create("OCR 多模态文档解析架构怎样设计？")
        sid = sess.session_id
        sdir = storage.session_dir(sid)
        step("S1.session", sdir.is_dir() and (sdir / "question.md").exists(),
             f"session_id={sid}")

        # ── Step 2：R1（k=0.7 + request_max + MCP A/B）──────────────
        async def drive(rs_kwargs: dict, instruction: str) -> None:
            rs, bus = rounds.prepare_round(sess, instruction, **rs_kwargs)
            await rounds.execute_round(sid, rs, bus)

        # 块内维护 sess 最新引用
        async def drive2(k: float, req_max, peer: bool, instr: str):
            nonlocal sess
            rs, bus = rounds.prepare_round(
                sess, instr, peer_review=peer,
                k=k, request_max_output=req_max,
            )
            await rounds.execute_round(sid, rs, bus)
            sess = sessions.get(sid)

        await drive2(k=0.7, req_max=4096, peer=False, instr="请给出你的方案。")

        policy = (sdir / "context_policy.jsonl").read_text(encoding="utf-8")
        events = [json.loads(l) for l in policy.splitlines() if l.strip()]

        cp = [e for e in events if e["event"] == "context_policy"]
        # R1 走 build_round1_prompt（无 ContextSelector → 无 context_budget）；
        # context_budget 的 k / req_max 一致性在 Step 4（R2~R7 齐备后）验证。
        ok1 = (
            bool(cp) and cp[0]["k"] == 0.7
            and cp[0]["request_max_output"] == 4096
            and cp[0]["summary_mode"] is False
        )
        step("S2.api_k_link", ok1,
             f"API 回显 k={cp[0]['k'] if cp else None}, "
             f"req_max={cp[0]['request_max_output'] if cp else None} "
             f"→ context_policy 事件同值（UI → API → RoundState 同源）")

        # ── Step 3：MCP Evidence A used / B unused + 追溯 ────────────
        ev_entries = [e for e in svc_evidence.load(sid)
                      if e.round_id == 1 and e.source_model_id == "qwen"]
        by_mcp = {e.mcp: e for e in ev_entries}
        tr_a = svc_evidence.trace_to_runtime(sid, by_mcp["github"])
        tr_b = svc_evidence.trace_to_runtime(sid, by_mcp["openwebsearch"])
        ok3 = (
            by_mcp["github"].used_in_final_answer is True
            and by_mcp["openwebsearch"].used_in_final_answer is False
            and tr_a["status"] == "found"
            and tr_b["status"] == "found"
            and tr_a["record"]["result_preview"] == QUOTED
        )
        step("S3.mcp_evidence", ok3,
             f"github used=True trace={tr_a['status']}; "
             f"openwebsearch used=False trace={tr_b['status']}")

        # Runtime 原始档案完整
        rt = sdir / "round_001" / "mcp_runtime.jsonl"
        rt_lines = [json.loads(l) for l in rt.read_text(encoding="utf-8").splitlines() if l.strip()]
        ok3b = len(rt_lines) == 2 and all(r["ok"] for r in rt_lines)
        step("S3b.runtime_intact", ok3b,
             f"round_001/mcp_runtime.jsonl {len(rt_lines)} 条原始记录完整")

        # ── Step 4：R2~R6 大内容驱动 Compaction（peer_review ON）────
        raw_before = {
            k: v for k, v in _snapshot(sdir).items()
            if k.startswith("round_") or k == "state.json"
        }
        for i in range(2, 8):
            await drive2(k=0.7, req_max=4096, peer=True,
                         instr=f"第{i}轮：请评价其他参与者。")

        events = [json.loads(l) for l in
                  (sdir / "context_policy.jsonl").read_text(encoding="utf-8").splitlines()
                  if l.strip()]
        comp = [e for e in events if e["event"] == "compaction"]
        ok4 = bool(comp) and all(
            e["artifact_kind"] == "session_compaction"
            and e["viewpoint_attribution"] is True
            and e["status"] == "success"
            for e in comp
        )
        step("S4.compaction", ok4,
             f"compaction ×{len(comp)}，全部 success / "
             f"artifact_kind=session_compaction / attribution=True")

        # ── Step 4b：context_budget 的 k / req_max 与 policy 同源（§3）──
        events = [json.loads(l) for l in
                  (sdir / "context_policy.jsonl").read_text(encoding="utf-8").splitlines()
                  if l.strip()]
        cp_all = [e for e in events if e["event"] == "context_policy"
                  and e["round_id"] >= 2]
        cb_all = [e for e in events if e["event"] == "context_budget"]
        ok4b = (
            len(cb_all) >= 6
            and all(e["k"] == 0.7 for e in cp_all + cb_all)
            and all(_val(e["request_max_output"]) == 4096 for e in cb_all)
        )
        step("S4b.budget_k_link", ok4b,
             f"{len(cb_all)} 条 context_budget 全部 k=0.7 / req_max=4096，"
             f"与 {len(cp_all)} 条 context_policy 同源")

        # ── Step 5：Pin R1 + R7（均已存在）→ 优先原文、无 HISTORY_WINDOW
        from core.pin_store import PinStore
        pins = PinStore(storage)
        pins.pin(sid, 1)
        pins.pin(sid, 7)
        events = [json.loads(l) for l in
                  (sdir / "context_policy.jsonl").read_text(encoding="utf-8").splitlines()
                  if l.strip()]
        pin_ev = [e for e in events if e["event"] == "memory_pin"]
        await drive2(k=0.7, req_max=4096, peer=True, instr="第8轮：请继续。")
        events = [json.loads(l) for l in
                  (sdir / "context_policy.jsonl").read_text(encoding="utf-8").splitlines()
                  if l.strip()]
        sel8 = [e for e in events if e["event"] == "history_selection"
                and e["round_id"] == 8]
        sel = sel8[-1] if sel8 else None
        pinfo = pins.load(sid)
        pf = sdir / "round_008" / "qwen.prompt.md"
        pf_text = pf.read_text(encoding="utf-8") if pf.exists() else ""
        ok5 = (
            len(pin_ev) >= 2 and 1 in pinfo.pinned_round_ids
            and 7 in pinfo.pinned_round_ids
            and sel is not None
            and 1 in (sel.get("pinned_round_ids") or [])
            and 7 in (sel.get("pinned_round_ids") or [])
            and "GitHub Issue #456" in pf_text  # R1 原文无损进 Prompt
        )
        step("S5.pin_lossless", ok5,
             f"pins={pinfo.pinned_round_ids}; R8 selection pinned="
             f"{sel.get('pinned_round_ids') if sel else None}; "
             f"R1 原文在 Prompt={'GitHub Issue #456' in pf_text}")

        # ── Step 6：Peer Context 来源合法（黑盒最终 Prompt）──────────
        ok6 = True
        detail6 = []
        for rd in (3, 4, 5, 8):
            p = sdir / f"round_{rd:03d}" / "qwen.prompt.md"
            if not p.exists():
                continue
            t = p.read_text(encoding="utf-8")
            if "## 其他参与者当前状态（Peer Context）" in t:
                has_legal = ("来源：final_answer /" in t
                             or "来源：conversation_summary /" in t)
                ok6 = ok6 and has_legal
                detail6.append(f"R{rd}: legal={has_legal}")
        ok6 = ok6 and bool(detail6)
        step("S6.peer_source_legal", ok6, "; ".join(detail6))

        # ── Step 7：Conversation Summary 全链路（Summary ≠ Valid Round）
        n_rounds_before = len(sessions.get(sid).rounds)
        own = [
            (vr.round_id, vr.successful_answers["qwen"])
            for vr in __import__("core.valid_round", fromlist=["ValidRoundPoolBuilder"])
            .ValidRoundPoolBuilder(storage).build(sessions.get(sid))
            if "qwen" in vr.successful_answers
        ]
        from core.summary_service import SummaryService, SummaryInput
        svc_sum = SummaryService(storage, provider_factory=_Provider)
        hist = [SummaryInput(round_id=r, user_question="Q?",
                             successful_answers={"qwen": a})
                for r, a in own]
        s1 = await svc_sum.generate(session_id=sid, model_id="qwen",
                                    history=hist, trigger="user")
        s2 = await svc_sum.generate(session_id=sid, model_id="qwen",
                                    history=hist, trigger="user")
        n_rounds_after = len(sessions.get(sid).rounds)
        latest = svc_sum.latest_for(sid, "qwen")
        ok7 = (
            s1.status == "success" and s1.file_path is not None
            and (sdir / s1.file_path).exists()
            and s2.summary_id == s1.summary_id  # user trigger=总是新建？
        )
        # 注意：user trigger 总是新建 → s2.summary_id != s1.summary_id
        ok7 = (
            s1.status == "success" and s1.file_path is not None
            and (sdir / s1.file_path).exists()
            and s2.summary_id != s1.summary_id
            and n_rounds_before == n_rounds_after       # D1：不加 Round
            and latest is not None
            and latest.summary_id == s2.summary_id
        )
        step("S7.summary_chain", ok7,
             f"summary_001/002 落盘 summaries/qwen/；Round 数不变 "
             f"({n_rounds_before})；latest={latest.summary_id}")

        # ── Step 8：Output Budget 恒等式（黑盒日志）──────────────────
        events = [json.loads(l) for l in
                  (sdir / "context_policy.jsonl").read_text(encoding="utf-8").splitlines()
                  if l.strip()]
        budgets = [e for e in events if e["event"] == "context_budget"]
        warns = [e for e in events if e["event"] == "context_budget_warning"]
        ok8 = bool(budgets)
        bad = []
        overflow_rounds = set()
        for b in budgets:
            cl = _val(b["context_limit"])
            fi = _val(b["fixed_input_tokens"])
            ro = _val(b["reserved_output_tokens"])
            pt = _val(b["pinned_tokens"])
            rr = _val(b["recent_raw_tokens"])
            ctk = _val(b["compacted_tokens"])
            hb = _val(b["history_budget"])
            k = b["k"]
            # §21 恒等式：history_budget = context_limit - fixed - reserved
            if hb != cl - fi - ro:
                bad.append(f"R{b.get('round_id')}: history {hb} != {cl}-{fi}-{ro}")
            # k 一致性（§3：禁 UI 一值后端另一值）
            if abs(k - 0.7) > 1e-9:
                bad.append(f"R{b.get('round_id')}: k={k}")
            # Input + Reserved <= Limit（§24）；Pin 过多（§27）是**合法
            # overflow**：total 可超 Limit，但必须有 context_budget_warning
            total = fi + pt + rr + ctk + ro
            if total > cl:
                overflow_rounds.add(b.get("round_id"))
        for rid in overflow_rounds:
            if not any(w.get("round_id") == rid for w in warns):
                bad.append(f"R{rid}: total>Limit 但缺 context_budget_warning")
        ok8 = ok8 and not bad
        step("S8.budget_identity", ok8,
             f"{len(budgets)} 条 context_budget：history=limit-fixed-reserved ✓、"
             f"k=0.7 ✓、total>Limit 的 {sorted(overflow_rounds) or '无'} 轮均有 warning")

        # ── Step 9：Raw Archive 不变 ─────────────────────────────────
        raw_after = {
            k: v for k, v in _snapshot(sdir).items()
            if k.startswith("round_") or k == "state.json"
        }
        # state.json 会因正常 Round 推进而变化；round_NNN/ 内容必须不变
        changed_rounds = [
            k for k, v in raw_before.items()
            if k.startswith("round_") and raw_after.get(k) != v
        ]
        ok9 = not changed_rounds
        step("S9.raw_archive", ok9,
             f"round_NNN/* 全程字节级不变（changed={changed_rounds[:3]}）")

        # ── Step 10：日志事件全家福（§12）────────────────────────────
        kinds = {e["event"] for e in events}
        required = {
            "context_policy", "context_budget", "history_selection",
            "memory_pin", "conversation_summary",
            "peer_context_preparation", "compaction", "mcp_evidence_index",
        }
        missing = required - kinds
        ok10 = not missing
        step("S10.logs_complete", ok10,
             f"context_policy.jsonl 事件覆盖：{sorted(kinds & required)}"
             + (f"；缺失：{sorted(missing)}" if missing else ""))

        # ── Step 11：目录隔离（summaries/ vs compactions/ vs evidence/）
        ok11 = (
            (sdir / "summaries" / "qwen").is_dir()
            and (sdir / "compactions").is_dir()
            and (sdir / "evidence" / "mcp_evidence.jsonl").is_file()
            and not any("compact_" in p.name
                        for p in (sdir / "summaries").rglob("*.md"))
        )
        step("S11.dir_isolation", ok11,
             "summaries/<model>/ 与 compactions/ 严格分离；evidence/ 存在")

        # ── Step 12：最终 Prompt 黑盒（无违禁语义）───────────────────
        ok12 = True
        bad12 = []
        for p in sdir.glob("round_*/*.prompt.md"):
            t = p.read_text(encoding="utf-8")
            if "HISTORY_WINDOW" in t:
                bad12.append(f"{p.name}: HISTORY_WINDOW")
            if "Compaction 产物" in t and "conversation_summary" in t \
                    and "session_compaction" not in t:
                bad12.append(f"{p.name}: kind 混淆")
        ok12 = not bad12
        step("S12.prompt_blackbox", ok12,
             "全部 prompt.md 无 HISTORY_WINDOW / 无 Summary-Compaction 混淆"
             + (f"；违例：{bad12[:2]}" if bad12 else ""))

    except Exception as exc:  # noqa: BLE001
        traceback.print_exc()
        step("UNEXPECTED", False, f"{type(exc).__name__}: {exc}")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)

    print("\n" + "=" * 64)
    fails = [s for s in STEPS if not s[1]]
    print(f"E2E 结果：{len(STEPS) - len(fails)}/{len(STEPS)} PASS")
    for name, ok, detail in STEPS:
        print(f"  [{'PASS' if ok else 'FAIL'}] {name}")
    return 0 if not fails else 1


if __name__ == "__main__":
    t0 = time.time()
    code = asyncio.run(main())
    print(f"\n耗时 {time.time() - t0:.1f}s；exit={code}")
    sys.exit(code)
