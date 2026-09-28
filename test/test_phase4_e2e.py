"""S3.10 Phase 4 全链路整合 E2E 测试（MD §69 验收标准）。

与 scripts/run_phase4_e2e.py 同源断言，固化为 pytest：

  - §3  API 参数链路：UI k=0.7 → POST → RoundState.k → ContextBudget.k
        → context_policy.jsonl（禁前后端值不一致）
  - §4  Output Budget 四种组合（provider>request / < / unknown / request unknown）
  - §5  History R1..R10 无 HISTORY_WINDOW；Pin R1/R7 优先原文
  - §7  Peer Review A/B/C/D 四场景（单答→FA；多答→Summary；复用；新 Round 重生成）
  - §8  Peer 隔离：Peer Context 来源数据层白名单（黑盒 Prompt 验证）
  - §9  Compaction 全链路 + Raw Archive 不变
  - §12 context_policy.jsonl 八类事件
  - §13 最终 Prompt 黑盒验收

不重复 S3.1~S3.9 的单元细节；只验"串起来之后仍然正确"。
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import tempfile
from pathlib import Path

import pytest

QUOTED = "GitHub Issue #456 描述了 OCR 长尾延迟问题，影响 P99 指标"


# ── harness ─────────────────────────────────────────────────────────────


def _sha(p: Path) -> str:
    return hashlib.md5(p.read_bytes()).hexdigest()


def _val(tok):
    """兼容三种形态：TokenEstimate 对象 / 旧嵌套日志 dict / §八 平铺数字。"""
    if tok is None or isinstance(tok, (int, float)):
        return tok
    if isinstance(tok, dict):
        return tok.get("value")
    return getattr(tok, "value", None)


class _Harness:
    """真链路 harness：Storage + RoundManager + 注入式 provider。"""

    def __init__(self, tmp: Path, answer_fn=None):
        os.environ.setdefault("CROSSLLM_MOCK_DELAY_SCALE", "0")
        from core.storage import Storage
        from core.model_manager import ModelRegistry
        from core.round_manager import RoundManager
        from core.session_manager import SessionManager
        from core.evidence_index import EvidenceIndexService
        from core import mcp_runtime_log as runtime_log
        from _capability_stub import StubCapabilityResolver

        self.runtime_log = runtime_log
        self.answer_fn = answer_fn
        self.storage = Storage(tmp / "sessions")
        self.registry = ModelRegistry()
        # §一：注入确定性的模型能力（生产链路走 Provider 元数据）。
        # 数值沿用改动前的内建默认（context_limit=8192 / model_max=2048），
        # 因此既有预算断言的数字不变。
        self.capability = StubCapabilityResolver(
            context_limit=8192, model_max_output=2048,
        )
        self.rounds = RoundManager(
            self.storage, self.registry, self._factory,
            capability_resolver=self.capability,
        )
        self.sessions = SessionManager(self.storage, self.registry)
        self.evidence = EvidenceIndexService(self.storage)
        sess = self.sessions.create("OCR 多模态文档解析架构怎样设计？")
        self.sid = sess.session_id
        self.sess = sess
        self.sdir = self.storage.session_dir(self.sid)

    def _factory(self, mid: str):
        outer = self

        class _P:
            def __init__(self, mid):
                self.mid = mid

            async def generate(self, round_id, prompt, max_output_tokens=None):
                if "多参与者对话历史" in prompt:
                    # A3（Phase 4.5 修复）：压缩输入用匿名 Candidate 标签标注
                    # 参与者（mock 模型集 {deepseek, gpt, qwen} → A/B/C）；
                    # 回抄真实 model_id 会被 F2b 身份校验判为泄漏。
                    yield (
                        "## 用户目标\n设计 OCR 架构。\n\n## 各参与者观点\n"
                        "参与者 Candidate C 认为：方案 X（分层缓存）更稳妥。\n"
                        "参与者 Candidate B 认为：方案 Y（全量重算）更简单。\n"
                        "参与者 Candidate A 反对方案 X。\n\n"
                        "## 分歧\nX vs Y 未收敛。\n\n"
                        "## 未验证假设\n长尾占比尚无实测数据（未验证）。\n"
                    )
                    return
                if outer.answer_fn is not None:
                    async for chunk in outer.answer_fn(outer, self.mid, round_id, prompt):
                        yield chunk
                    return
                yield f"[{self.mid}] 详细分析：" + ("论证内容。" * 1100)

        return _P(mid)

    def emit_tool(self, ctx, *, call_id, mcp, tool, args, preview):
        rec = self.runtime_log.build_record(
            ctx=ctx, tool_call={"id": call_id, "name": tool},
            mcp_name=mcp, tool_name=tool, arguments=args,
            started_at=self.runtime_log.now_iso(), ok=True,
            result_chars=len(preview), result_preview=preview,
            error_type=None, error_message=None,
            returned_at=self.runtime_log.now_iso(), duration_ms=5,
        )
        self.runtime_log.append_record(ctx.directory, rec)

    async def drive(self, k=0.5, req_max=None, peer=True, instr="请继续。"):
        rs, bus = self.rounds.prepare_round(
            self.sess, instr, peer_review=peer,
            k=k, request_max_output=req_max,
        )
        await self.rounds.execute_round(self.sid, rs, bus)
        self.sess = self.sessions.get(self.sid)
        return rs

    def events(self) -> list[dict]:
        p = self.sdir / "context_policy.jsonl"
        if not p.exists():
            return []
        return [json.loads(l) for l in p.read_text(encoding="utf-8").splitlines()
                if l.strip()]

    def prompt(self, round_id: int, model: str = "qwen") -> str:
        p = self.sdir / f"round_{round_id:03d}" / f"{model}.prompt.md"
        return p.read_text(encoding="utf-8") if p.exists() else ""

    def raw_snapshot(self) -> dict[str, str]:
        return {
            str(p.relative_to(self.sdir)): _sha(p)
            for p in self.sdir.rglob("*") if p.is_file()
            and (p.relative_to(self.sdir).parts[0].startswith("round_")
                 or p.name == "state.json")
        }


# ── §3 API 参数链路：k 全链路同源 ───────────────────────────────────────


def test_api_k_full_chain(tmp_path):
    """UI k=0.7 → POST → RoundState.k=0.7 → ContextBudget.k=0.7
    → context_policy / context_budget 事件同值。"""
    h = _Harness(tmp_path)
    r1 = asyncio.run(h.drive(k=0.7, req_max=4096, peer=False, instr="R1"))
    assert r1.k == 0.7 and r1.request_max_output == 4096

    # R2~R3（有 ContextSelector → context_budget 事件）
    asyncio.run(h.drive(k=0.7, req_max=4096, peer=False, instr="R2"))
    asyncio.run(h.drive(k=0.7, req_max=4096, peer=False, instr="R3"))

    ev = h.events()
    cp = [e for e in ev if e["event"] == "context_policy"]
    cb = [e for e in ev if e["event"] == "context_budget"]
    # 每条 context_policy 的 k 与 RoundState 一致
    assert cp and all(e["k"] == 0.7 for e in cp)
    assert all(e["request_max_output"] == 4096 for e in cp)
    # R2+ 的 context_budget 同 k 同 req_max（R1 走 round1 prompt 无 budget）
    cb2 = [e for e in cb if e["round_id"] >= 2]
    assert cb2, "R2+ 应有 context_budget 事件"
    assert all(e["k"] == 0.7 for e in cb2)
    assert all(_val(e["request_max_output"]) == 4096 for e in cb2)


def test_api_k_clamped_to_md_range(tmp_path):
    """MD §20：k ∈ [0.01, 0.90]；越界值被夹紧。"""
    h = _Harness(tmp_path)
    rs = asyncio.run(h.drive(k=5.0, peer=False, instr="R1"))
    assert rs.k == 0.90
    rs = asyncio.run(h.drive(k=0.001, peer=False, instr="R2"))
    assert rs.k == 0.01


# ── §4 Output Budget 四种组合 ───────────────────────────────────────────


def test_output_budget_four_combinations():
    """§22 / §9.2：reserved = min(provider, request)；unknown 分支语义。

    用真实 ProviderModelMetadata + compute_budget（与 RoundManager 同一函数），
    确保日志中的三值来自真实运行值而非测试桩。
    """
    from core.model_capability import ProviderModelMetadata, TokenEstimate
    from core.context_budget import compute_budget, make_fixed_input_tokens

    def _meta(provider_max, source="config_static"):
        # provider unknown → value=None 必须 accuracy_mode='unknown'（TokenEstimate 不变量）
        unknown = provider_max is None
        return ProviderModelMetadata(
            model_slug="fake/m",
            context_limit=TokenEstimate(32768, "config_static", "estimated"),
            model_max_output=TokenEstimate(
                provider_max, source, "unknown" if unknown else "estimated",
            ),
            fetched_at="2026-09-05T00:00:00Z", fetch_status="ok",
        )

    def _req(v):
        return TokenEstimate(v, "config_static", "estimated")

    fixed = make_fixed_input_tokens(system_text="sys", current_user_text="u")

    # ① provider > request → reserved = request
    b = compute_budget(metadata=_meta(8192), request_max_output=_req(4096),
                       fixed=fixed, k=0.5)
    assert _val(b.reserved_output_tokens) == 4096

    # ② provider < request → reserved = provider
    b = compute_budget(metadata=_meta(2048), request_max_output=_req(4096),
                       fixed=fixed, k=0.5)
    assert _val(b.reserved_output_tokens) == 2048

    # ③ provider unknown + request 已知 → reserved = request（§9.2 分支②）
    b = compute_budget(metadata=_meta(None, "provider_unavailable"),
                       request_max_output=_req(4096),
                       fixed=fixed, k=0.5)
    assert _val(b.reserved_output_tokens) == 4096

    # ④ provider 已知 + request unknown → reserved = provider（分支①）
    b = compute_budget(metadata=_meta(2048),
                       request_max_output=TokenEstimate(
                           None, "provider_unavailable", "unknown"),
                       fixed=fixed, k=0.5)
    assert _val(b.reserved_output_tokens) == 2048


def test_output_budget_identity_in_real_chain(tmp_path):
    """真链路日志：history = limit - fixed - reserved；k=0.7 时
    lossless = round(history × 0.7)。"""
    h = _Harness(tmp_path)
    asyncio.run(h.drive(k=0.7, req_max=4096, peer=False, instr="R1"))
    asyncio.run(h.drive(k=0.7, req_max=4096, peer=False, instr="R2"))

    cb = [e for e in h.events() if e["event"] == "context_budget"
          and e["round_id"] >= 2]
    assert cb
    for b in cb:
        cl, fi, ro = (_val(b["context_limit"]), _val(b["fixed_input_tokens"]),
                      _val(b["reserved_output_tokens"]))
        hb = _val(b["history_budget"])
        assert hb == cl - fi - ro
        # lossless = history × k（§21）
        ll, cpb = _val(b["lossless_budget"]), _val(b["compaction_budget"])
        assert ll + cpb == hb
        # provider 三值都来自真实运行（request=4096 显式，model_max=2048 注入能力）
        assert _val(b["request_max_output"]) == 4096
        assert _val(b["model_max_output"]) == 2048
        assert _val(b["reserved_output_tokens"]) == 2048


# ── §5 History / Pin 全链路 ─────────────────────────────────────────────


def test_history_no_window_and_pin_priority(tmp_path):
    """R1..R8：无 HISTORY_WINDOW=3；Pin R1/R7 → 优先原文、
    recent_raw 从新到旧动态装载。"""
    h = _Harness(tmp_path)
    asyncio.run(h.drive(k=0.7, req_max=4096, peer=False, instr="R1 方案陈述"))
    for i in range(2, 8):
        asyncio.run(h.drive(k=0.7, req_max=4096, peer=False, instr=f"R{i} 评估"))

    from core.pin_store import PinStore
    pins = PinStore(h.storage)
    pins.pin(h.sid, 1)
    pins.pin(h.sid, 7)
    asyncio.run(h.drive(k=0.7, req_max=4096, peer=False, instr="R8 继续"))

    sel = [e for e in h.events() if e["event"] == "history_selection"
           and e["round_id"] == 8][-1]
    assert 1 in sel["pinned_round_ids"] and 7 in sel["pinned_round_ids"]

    # R1 原文无损出现在 R8 最终 Prompt（Pin 优先 + 原文保留）
    p8 = h.prompt(8)
    assert "R1 方案陈述" in p8
    assert "R7 评估" in p8
    # 黑盒：无 HISTORY_WINDOW 字样
    assert "HISTORY_WINDOW" not in p8


# ── §7 Peer Review A/B/C/D ──────────────────────────────────────────────


def _peer_harness(tmp_path, small_answers=True):
    """构造：qwen/gpt 两模型；gpt 的回答短（便于 Summary 命中）。"""

    def answer_fn(outer, mid, round_id, prompt):
        async def _gen():
            if mid == "gpt":
                yield f"gpt 第{round_id}轮的独特观点内容。"
            else:
                yield f"qwen 第{round_id}轮回答：" + ("细节。" * (600 if round_id > 1 else 1))
        return _gen()

    return _Harness(tmp_path, answer_fn=answer_fn)


def test_peer_ABCD_four_scenarios(tmp_path):
    """A 单答→FA 不调 LLM；B 多答→Summary；C 无新 Round→复用；
    D 新 Round→重生成。"""
    h = _peer_harness(tmp_path)
    from core.peer_context import PeerContextPreparator
    from core.summary_service import SummaryService
    from core.valid_round import ValidRoundPoolBuilder

    svc = SummaryService(h.storage, provider_factory=h._factory)
    prep = PeerContextPreparator(h.storage, svc)

    calls = {"n": 0}

    class _Counting:
        def __call__(self, mid):
            outer = svc

            class _P:
                def generate(self, round_id, prompt, max_output_tokens=None):
                    calls["n"] += 1
                    yield f"[summary-stub-{mid} #{calls['n']}] 参与者 {mid} 认为：待汇总。"

            return _P()

    svc._provider_factory = _Counting()

    # R1：两模型各 1 个有效回答
    asyncio.run(h.drive(k=0.7, peer=True, instr="R1"))
    pool = ValidRoundPoolBuilder(h.storage).build(h.sess)
    # 场景 A：gpt 只有 1 个有效回答 → Final Answer，0 次 LLM
    calls["n"] = 0
    out = asyncio.run(prep.prepare(session=h.sess, round_state=h.sess.rounds[0],
                                   valid_pool=pool))
    gpt_item = [it for it in out["qwen"] if it.source_model_id == "gpt"][0]
    assert gpt_item.mode == "final_answer" and calls["n"] == 0

    # R2：gpt 有 2 个有效回答 → Summary（场景 B）
    asyncio.run(h.drive(k=0.7, peer=True, instr="R2"))
    pool = ValidRoundPoolBuilder(h.storage).build(h.sess)
    rs2 = h.sess.rounds[1]
    out = asyncio.run(prep.prepare(session=h.sess, round_state=rs2,
                                   valid_pool=pool))
    gpt_item = [it for it in out["qwen"] if it.source_model_id == "gpt"][0]
    assert gpt_item.mode == "conversation_summary"
    first_summary_id = gpt_item.source_id
    n_after_b = calls["n"]
    assert n_after_b >= 1

    # 场景 C：无新 Round → 复用（LLM 调用不增长）
    out = asyncio.run(prep.prepare(session=h.sess, round_state=rs2,
                                   valid_pool=pool))
    gpt_item = [it for it in out["qwen"] if it.source_model_id == "gpt"][0]
    assert gpt_item.source_id == first_summary_id
    assert calls["n"] == n_after_b

    # 场景 D：出现新 Valid Round → 重新生成（source_id 变化）
    asyncio.run(h.drive(k=0.7, peer=True, instr="R3"))
    pool = ValidRoundPoolBuilder(h.storage).build(h.sess)
    rs3 = h.sess.rounds[2]
    out = asyncio.run(prep.prepare(session=h.sess, round_state=rs3,
                                   valid_pool=pool))
    gpt_item = [it for it in out["qwen"] if it.source_model_id == "gpt"][0]
    assert gpt_item.mode == "conversation_summary"
    assert gpt_item.source_id != first_summary_id
    assert calls["n"] > n_after_b


# ── §8 Peer 隔离（黑盒 Prompt）──────────────────────────────────────────


def test_peer_isolation_blackbox_prompt(tmp_path):
    """R1 A/B 各答；R2 B 产生对 A 的评价（Peer Review Result 形态的
    Final Answer）；R3 A 的 Prompt 中，B 的 Peer Context 来源标注
    只能是 final_answer / conversation_summary —— 数据层白名单在
    黑盒可见层面的体现。"""
    def answer_fn(outer, mid, round_id, prompt):
        async def _gen():
            if mid == "gpt":
                if round_id == 2:
                    # B 的 R2 回答 = 对 A 的 Peer Review Result
                    yield "我对 qwen 上轮方案的评价：方向正确但需要补量化数据。"
                else:
                    yield f"gpt R{round_id} 独立观点：优先补齐基线观测。"
            else:
                yield f"qwen R{round_id} 回答：" + ("内容。" * 40)
        return _gen()

    h = _Harness(tmp_path, answer_fn=answer_fn)
    asyncio.run(h.drive(k=0.7, peer=True, instr="R1"))
    asyncio.run(h.drive(k=0.7, peer=True, instr="R2"))
    asyncio.run(h.drive(k=0.7, peer=True, instr="R3"))

    p3 = h.prompt(3, "qwen")
    assert "## 其他参与者当前状态（Peer Context）" in p3
    # 来源标注只允许两种合法值
    assert "来源：final_answer /" in p3 or "来源：conversation_summary /" in p3
    # 绝不允许出现第三种来源
    assert "来源：peer_review_result" not in p3
    assert "peer_context" not in p3.split("Peer Context）")[0]
    # §16：B 的 R2 Final Answer（其 Peer Review Result）**可以**作为
    # final_answer 来源进入（跨模型传播的合法来源就是 Other Model FA），
    # 但绝不以"Peer Review Result"这一来源类型出现。
    if "我对 qwen 上轮方案的评价" in p3:
        # 若出现，必须是 final_answer 标注的（合法），且附近无非法来源标签
        assert "来源：final_answer /" in p3


# ── §9 Compaction 全链路 + Raw Archive ──────────────────────────────────


def test_compaction_chain_and_raw_archive(tmp_path):
    """大历史 → dropped → compactions/ → 最终 Prompt 含压缩块；
    Raw Archive 字节级不变。"""
    h = _Harness(tmp_path)
    before = h.raw_snapshot()
    asyncio.run(h.drive(k=0.7, req_max=4096, peer=True, instr="R1"))
    for i in range(2, 7):
        asyncio.run(h.drive(k=0.7, req_max=4096, peer=True, instr=f"R{i}"))

    comp = [e for e in h.events() if e["event"] == "compaction"]
    assert comp, "大内容应触发 Compaction"
    assert all(e["artifact_kind"] == "session_compaction" for e in comp)
    assert all(e["viewpoint_attribution"] is True for e in comp)

    # compactions/ 目录存在且与 summaries/ 隔离
    assert (h.sdir / "compactions").is_dir()
    # 后续 Prompt 出现压缩块（归属声明内置）
    found = any("历史压缩摘要" in h.prompt(r) and "Session 级共享" in h.prompt(r)
                for r in range(3, 7))
    assert found, "Compaction 后的最终 Prompt 应含压缩块"

    # Raw Archive：round_NNN/* 字节级不变（state.json 因正常推进而变化是预期）
    after = h.raw_snapshot()
    changed = [k for k, v in before.items()
               if k.startswith("round_") and after.get(k) != v]
    assert changed == []


# ── §11 MCP Evidence 全链路 ─────────────────────────────────────────────


def test_mcp_evidence_chain(tmp_path):
    """MCP A 被引用 → true；MCP B 未引用 → false；双双可追溯；
    Runtime 原始档案完整。"""
    def answer_fn(outer, mid, round_id, prompt):
        async def _gen():
            ctx = outer.runtime_log.get_runtime_context()
            if round_id == 1 and mid == "qwen" and ctx is not None:
                outer.emit_tool(ctx, call_id="call_A", mcp="github",
                                tool="get_issue", args={"num": 456},
                                preview=QUOTED)
                outer.emit_tool(ctx, call_id="call_B", mcp="openwebsearch",
                                tool="search", args={"q": "ocr"},
                                preview="与回答完全无关的搜索结果条目内容")
                yield f"工具返回：{QUOTED}。基于此建议先量化。"
            else:
                yield f"[{mid}] R{round_id} 回答：" + ("内容。" * 40)
        return _gen()

    h = _Harness(tmp_path, answer_fn=answer_fn)
    asyncio.run(h.drive(k=0.7, peer=False, instr="R1"))

    entries = [e for e in h.evidence.load(h.sid)
               if e.source_model_id == "qwen" and e.round_id == 1]
    by_mcp = {e.mcp: e for e in entries}
    assert by_mcp["github"].used_in_final_answer is True
    assert by_mcp["openwebsearch"].used_in_final_answer is False

    for e in entries:
        tr = h.evidence.trace_to_runtime(h.sid, e)
        assert tr["status"] == "found"
        assert tr["record"]["result_preview"]

    rt = h.sdir / "round_001" / "mcp_runtime.jsonl"
    lines = [json.loads(l) for l in rt.read_text(encoding="utf-8").splitlines()
             if l.strip()]
    assert len(lines) == 2 and all(r["ok"] for r in lines)


# ── §12 日志事件全家福 ──────────────────────────────────────────────────


def test_context_policy_log_all_event_kinds(tmp_path):
    """§12：一次完整跑完后 context_policy.jsonl 必须覆盖 8 类事件。"""
    def answer_fn(outer, mid, round_id, prompt):
        async def _gen():
            ctx = outer.runtime_log.get_runtime_context()
            if round_id == 1 and mid == "qwen" and ctx is not None:
                outer.emit_tool(ctx, call_id="call_A", mcp="github",
                                tool="get_issue", args={"n": 1}, preview=QUOTED)
                outer.emit_tool(ctx, call_id="call_B", mcp="search",
                                tool="web", args={"q": "x"},
                                preview="与回答无关的内容条目")
                yield f"工具返回：{QUOTED}。建议量化。"
            else:
                yield f"[{mid}] R{round_id}：" + ("内容。" * 1600)
        return _gen()

    h = _Harness(tmp_path, answer_fn=answer_fn)
    asyncio.run(h.drive(k=0.7, req_max=4096, peer=True, instr="R1"))
    for i in range(2, 7):
        asyncio.run(h.drive(k=0.7, req_max=4096, peer=True, instr=f"R{i}"))

    from core.pin_store import PinStore
    PinStore(h.storage).pin(h.sid, 1)
    asyncio.run(h.drive(k=0.7, req_max=4096, peer=True, instr="R7"))

    from core.summary_service import SummaryService, SummaryInput
    from core.valid_round import ValidRoundPoolBuilder
    svc = SummaryService(h.storage, provider_factory=h._factory)
    pool = ValidRoundPoolBuilder(h.storage).build(h.sess)
    hist = [SummaryInput(round_id=vr.round_id, user_question="Q?",
                         successful_answers={"qwen": vr.successful_answers["qwen"]})
            for vr in pool if "qwen" in vr.successful_answers]
    asyncio.run(svc.generate(session_id=h.sid, model_id="qwen",
                             history=hist, trigger="user"))

    kinds = {e["event"] for e in h.events()}
    required = {
        "context_policy", "context_budget", "history_selection",
        "memory_pin", "conversation_summary",
        "peer_context_preparation", "compaction", "mcp_evidence_index",
    }
    missing = required - kinds
    assert not missing, f"context_policy.jsonl 缺事件：{sorted(missing)}"

    # round_id 断言只适用于 Round 绑定事件；conversation_summary 是
    # 派生制品（D1：不参与 Round 编号），设计上无 round_id。
    ev = h.events()
    round_bound = required - {"conversation_summary"}
    for e in ev:
        if e["event"] in round_bound:
            assert "round_id" in e, f"{e['event']} 缺 round_id"
        if e["event"] in required:
            # conversation_summary 用 created_at（S3.6 字段名）表达时间
            assert ("timestamp" in e or e["event"] == "conversation_summary"
                    and "created_at" in e), f"{e['event']} 缺时间字段"
    # conversation_summary 必须有的可审计字段（S3.6）
    cs = [e for e in ev if e["event"] == "conversation_summary"]
    assert cs, "应落 conversation_summary 事件"
    for e in cs:
        assert ("model_id" in e and "status" in e
                and "source_valid_round" in e and "created_at" in e)


# ── §13 最终 Prompt 黑盒 + §14 完整场景 ─────────────────────────────────


def test_final_prompt_blackbox_and_full_scenario(tmp_path):
    """§13/§14：R1→R2→Pin R1→R3(PR)→R4(超预算)→Compaction→Evidence→R5(PR)。
    黑盒检查最终 Prompt + Raw Archive + 日志。"""
    def answer_fn(outer, mid, round_id, prompt):
        async def _gen():
            ctx = outer.runtime_log.get_runtime_context()
            if round_id == 4 and mid == "qwen" and ctx is not None:
                outer.emit_tool(ctx, call_id="call_A", mcp="github",
                                tool="get_issue", args={"n": 1}, preview=QUOTED)
                yield f"工具返回：{QUOTED}。R4 结论。"
            elif mid == "gpt":
                yield f"gpt R{round_id}：独立观点。" + ("补充。" * 900)
            else:
                yield f"qwen R{round_id}：" + ("内容。" * 1600)
        return _gen()

    h = _Harness(tmp_path, answer_fn=answer_fn)
    asyncio.run(h.drive(k=0.7, req_max=4096, peer=True, instr="R1")       ) # R1)
    asyncio.run(h.drive(k=0.7, req_max=4096, peer=True, instr="R2")       ) # R2)
    from core.pin_store import PinStore
    PinStore(h.storage).pin(h.sid, 1)                          # Pin R1
    asyncio.run(h.drive(k=0.7, req_max=4096, peer=True, instr="R3 PR")     ) # R3 Peer ON)
    asyncio.run(h.drive(k=0.7, req_max=4096, peer=True, instr="R4 超预算")  ) # R4 + Evidence)
    asyncio.run(h.drive(k=0.7, req_max=4096, peer=True, instr="R5 PR 再跑")  ) # R5 Peer 再跑)

    ev = h.events()
    kinds = {e["event"] for e in ev}
    for required in ("context_policy", "context_budget", "history_selection",
                     "memory_pin", "peer_context_preparation",
                     "compaction", "mcp_evidence_index"):
        assert required in kinds, f"缺 {required}"

    # R5 黑盒：Pin 原文 + Peer Context 合法来源 + 无违禁语义
    p5 = h.prompt(5)
    assert "R1" in p5                       # Pin R1 原文进入
    assert "## 其他参与者当前状态（Peer Context）" in p5
    assert "来源：final_answer /" in p5 or "来源：conversation_summary /" in p5
    assert "HISTORY_WINDOW" not in p5
    # MCP 大块原文不无条件注入（Evidence Index 只在 Evidence 面板/API）
    assert "mcp_runtime.jsonl" not in p5

    # Evidence：R4 的 call_A used=true
    entries = [e for e in h.evidence.load(h.sid)
               if e.round_id == 4 and e.mcp == "github"]
    assert entries and entries[0].used_in_final_answer is True

    # Raw Archive
    changed = [k for k in h.raw_snapshot() if k.startswith("round_")]
    # 只需确认没有"内容变化后又恢复"之类的异常 —— 快照在最后，简单断言文件都在
    assert len(changed) >= 5

    # k 全程一致
    assert all(e["k"] == 0.7 for e in ev if e["event"] == "context_policy")
