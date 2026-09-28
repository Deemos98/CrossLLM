"""MCP Evidence Index（Plan §4.10 §16 + docs/phase4_memory.md §44-§48）。

Phase 4.5（docs/phase4_memory.md §4.5.1 - §4.5.12）追加 Evidence → Context：

    select_for_context()  ——  选出可进入**当前模型** Prompt 的 Evidence
    render_evidence_block() —— 只渲染 reference 的模型可见块

严格范围（§4.5.3 A1 / §4.5.7 / §4.5.9 / §4.5.10）：

    ✅ 只读当前模型自己的历史 Evidence（不聚合、不跨模型）
    ✅ 只来自已完成的历史 Round（当前轮不得反向注入自身，§4.5.4）
    ✅ 与 Compacted History **共用** CompactionBudget，不建独立预算
    ✅ reference 命中真实身份词元 → 整条剔除（§4.5.11）
    ❌ 不进入 Peer Context
    ❌ 不写入 Project Memory
    ❌ 不做证据打分 / 向量检索 / 语义召回（§67 第一版明确不做）

# 严格区分（§16.1）

    MCP Runtime        ≠ MCP Evidence Index ≠ Fact

| | MCP Runtime | MCP Evidence Index |
|---|---|---|
| 是什么 | 原始运行档案 | 轻量导航索引 |
| 内容 | Tool/Arguments/Result/Error/Duration | 哪些 Tool Result **实际被 Final Answer 使用** |
| 生成时机 | 每次 tool call 实时 | Round 完成后 或 Compaction 时（§48） |
| 文件 | round_NNN/mcp_runtime.jsonl | evidence/mcp_evidence.jsonl |

# 四条红线（§16.4 逐字）

    ❌ Tool Call 直接当 Evidence            —— Tool Call ≠ Useful Evidence
    ❌ Evidence = Fact
    ❌ MCP Result 自动升级为 Fact           —— 不记 is_fact，不评分，不验证
    ❌ Evidence Index 代替 Runtime          —— Runtime 永不因 Index 删除/重建而丢失

# used_in_final_answer 判定（§16.3 CrossLLM Custom Rule）

第一版**不做**每次 tool call 的额外 LLM 调用（§48 明许）。判定策略：

    1. tool_call_id 显式引用：Final Answer 中出现该 id 字符串 → true
    2. result_preview 高显著片段（≥ MIN_MATCH_LEN 连续字符，归一化空白后）
       出现在 Final Answer 中 → true
    3. 都不满足 → false

字符串匹配是最简可解释方案；出现误判证据时可升级为 LLM 判定。
"""

from __future__ import annotations

import json
import re
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any, Sequence

from .anonymizer import find_identity_tokens

if TYPE_CHECKING:  # pragma: no cover
    from .state import ModelParticipant

# ── 常量 ────────────────────────────────────────────────────────────────

EVIDENCE_DIRNAME = "evidence"
EVIDENCE_FILENAME = "mcp_evidence.jsonl"
RUNTIME_LOG_NAME = "mcp_runtime.jsonl"

# result_preview 片段视为"高显著"的最小连续长度（§16.3 策略 2）
MIN_MATCH_LEN = 12

_WS_RE = re.compile(r"\s+")


def _now_iso() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def _normalize(text: str) -> str:
    """归一化空白，降低换行/缩进差异造成的误判。"""
    return _WS_RE.sub("", text or "")


# ── 数据结构（Plan §4.10 + 用户任务书扩展）──────────────────────────────


@dataclass(frozen=True)
class McpEvidenceEntry:
    """一条 Evidence 索引行。

    Field：
      round_id             哪个 Round
      mcp                  哪个 MCP（= Plan §4.10 的 mcp_name）
      tool                 哪个 Tool（= Plan §4.10 的 tool_name）
      runtime_record_id    回指 mcp_runtime.jsonl 的定位键（tool_call id）
      reference            人类可读定位（如 "Issue #123" / tool_call 短 id）
      used_in_final_answer §46 核心字段 —— **不因调用成功自动 true**（§16.4）
      created_at           ISO8601 UTC
      source_model_id      哪个模型的 Final Answer（多模型不混淆）
    """

    round_id: int
    mcp: str
    tool: str
    runtime_record_id: str
    reference: str
    used_in_final_answer: bool
    created_at: str = ""
    source_model_id: str = ""

    # Plan §4.10 命名兼容
    @property
    def mcp_name(self) -> str:
        return self.mcp

    @property
    def tool_name(self) -> str:
        return self.tool

    # ❌ 故意不提供 is_fact / confidence / score 字段（§47 / 用户任务书 §五）
    def to_dict(self) -> dict:
        return {
            "round_id": self.round_id,
            "mcp": self.mcp,
            "tool": self.tool,
            "runtime_record_id": self.runtime_record_id,
            "reference": self.reference,
            "used_in_final_answer": self.used_in_final_answer,
            "created_at": self.created_at,
            "source_model_id": self.source_model_id,
        }

    @classmethod
    def from_dict(cls, obj: dict) -> "McpEvidenceEntry":
        return cls(
            round_id=int(obj["round_id"]),
            mcp=str(obj["mcp"]),
            tool=str(obj["tool"]),
            runtime_record_id=str(obj["runtime_record_id"]),
            reference=str(obj.get("reference", "")),
            used_in_final_answer=bool(obj["used_in_final_answer"]),
            created_at=str(obj.get("created_at", "")),
            source_model_id=str(obj.get("source_model_id", "")),
        )


# ── 判定核心（§16.3，纯函数便于单测）──────────────────────────────────


# 子句切分：按常见中英文标点切断，得到更细粒度的引用单元
_CLAUSE_SPLIT_RE = re.compile(r"[，。；、！？,.;:：!?()\[\]（）【】\"'""'']")


def extract_salient_snippets(result_preview: str) -> list[str]:
    """从 result_preview 提取候选"高显著片段"（§16.3 策略 2）。

    策略：按行 → 按标点切子句 → 归一化空白 → 保留长度 ≥ MIN_MATCH_LEN。
    行级整体也保留（模型可能整行引用）；每个片段截断到前 80 字符。
    """
    out: list[str] = []
    for line in (result_preview or "").splitlines():
        norm_line = _normalize(line)
        if len(norm_line) >= MIN_MATCH_LEN:
            out.append(norm_line[:80])
        # 子句级：模型通常只引用标题 / 关键短语，而不是整行
        for clause in _CLAUSE_SPLIT_RE.split(line):
            norm = _normalize(clause)
            if len(norm) >= MIN_MATCH_LEN and norm not in out:
                out.append(norm[:80])
    return out


def judge_used_in_final_answer(
    *,
    tool_call_id: str,
    result_preview: str,
    final_answer: str,
) -> bool:
    """§16.3 判定。Tool Call 成功本身**绝不**构成 used=true。"""
    if not final_answer:
        return False
    answer_norm = _normalize(final_answer)

    # 策略 1：tool_call_id 显式引用
    if tool_call_id and len(tool_call_id) >= 6 and tool_call_id in answer_norm:
        return True

    # 策略 2：result_preview 高显著片段
    for snippet in extract_salient_snippets(result_preview):
        if snippet in answer_norm:
            return True

    return False


def build_reference(mcp: str, tool: str, result_preview: str, call_id: str) -> str:
    """生成人类可读定位（如 "Issue #123"；无显著标题时回退 tool_call 短 id）。"""
    for line in (result_preview or "").splitlines():
        s = line.strip()
        if len(s) >= 6:
            return s[:60]
    short = (call_id or "")[:12]
    return f"{mcp}.{tool}" + (f" @ {short}" if short else "")


# ── Evidence → Context 渲染（§4.5.6 / §4.5.11）─────────────────────────

#: 模型可见块标题（系统生成骨架，进 prompt_builder 的 A3 硬校验）
EVIDENCE_BLOCK_HEADER = "## 外部证据引用（MCP Evidence）"

#: §4.5.6 + §44/§47：只提供 Reference，且明确"不等于已确认事实"
EVIDENCE_BLOCK_NOTE = (
    "以下为你此前轮次使用过的 MCP 外部结果索引，仅供定位与追溯。"
    "原文保存在系统运行档案中，本索引只是引用，**不代表已确认事实**。"
)

#: §4.5.8 事件：预算不足 → 跳过该 Evidence
EVIDENCE_SKIP_EVENT_BUDGET = "mcp_evidence_skipped_for_budget"

#: §4.5.11 事件：reference 命中真实身份词元 → 跳过该 Evidence
EVIDENCE_SKIP_EVENT_IDENTITY = "mcp_evidence_skipped_for_identity"

#: 跳过原因文案（写进日志，便于与预算原因区分）
EVIDENCE_SKIP_REASON_BUDGET = "budget_insufficient"
EVIDENCE_SKIP_REASON_BUDGET_UNKNOWN = "budget_unknown"
EVIDENCE_SKIP_REASON_IDENTITY = "identity_leak_in_reference"


def render_evidence_block(
    entries: "Sequence[McpEvidenceEntry]", *, skeleton: bool = False,
) -> str:
    """把 Evidence 索引渲染成模型可见块（§4.5.6）。

    模型可见内容**只**含 ``round_id / mcp / tool / reference``：
    ``source_model_id`` 与 ``runtime_record_id`` 是内部 provenance（§4.5.11），
    绝不进入本块。

    ``skeleton=True`` 时剥离 reference 正文，只保留系统生成骨架
    （标题 / 说明 / 定位行）—— 供 prompt_builder 做 A3 硬校验使用，
    与 ``compaction.compacted_system_header()`` 同一套路。
    """
    if not entries:
        return ""
    lines = [EVIDENCE_BLOCK_HEADER, "", EVIDENCE_BLOCK_NOTE, ""]
    for i, e in enumerate(entries, start=1):
        lines.append(f"[E{i}] 第 {e.round_id} 轮 · {e.mcp} / {e.tool}")
        if not skeleton:
            lines.append(f"      {e.reference}")
    return "\n".join(lines)


def split_evidence_by_identity(
    entries: "Sequence[McpEvidenceEntry]",
    participants: "Sequence[ModelParticipant]",
) -> tuple[list[McpEvidenceEntry], list[tuple[McpEvidenceEntry, list[str]]]]:
    """§4.5.11：把 Evidence 分成「可注入」与「因身份泄漏被剔除」。

    判定**只覆盖 ``reference``**（外部工具文本）。``round_id / mcp / tool``
    是 CrossLLM 自己生成的骨架，由 prompt_builder 的 A3 硬校验负责 ——
    两条防线职责不重叠，同一个 reference 也不会被两套规则各判一次。

    判定函数沿用 ``anonymizer.find_identity_tokens``（大小写不敏感），
    与 ``assert_no_identity_leak`` 是同一个「身份词元」定义，不另造一套匹配规则。

    返回 (kept, rejected)，rejected 每项带命中的词元，供日志审计。
    """
    kept: list[McpEvidenceEntry] = []
    rejected: list[tuple[McpEvidenceEntry, list[str]]] = []
    for e in entries or ():
        hits = find_identity_tokens(e.reference, participants)
        if hits:
            rejected.append((e, hits))
        else:
            kept.append(e)
    return kept, rejected


def _estimate_tokens(text: str) -> int:
    """与全项目一致的粗估 ceil(len/3)（ContextSelector 同款）。"""
    if not text:
        return 0
    return -(-len(text) // 3)


# ── Service ─────────────────────────────────────────────────────────────


@dataclass
class EvidenceContextSelection:
    """一次 Evidence → Context 选择结果（§4.5.3 - §4.5.11）。

    entries              ：实际进入 Prompt 的 Evidence（只渲染 reference）
    skipped              ：CompactionBudget 不足被跳过的 Evidence（§4.5.8）
    skipped_for_identity ：reference 命中真实身份词元被剔除的 Evidence（§4.5.11）
                           —— 与 skipped **分开**，两者原因与日志事件都不同
    deduped_round_ids    ：因仍处于无损 Context 而不重复注入的 Round（§4.5.5）
    used_tokens          ：本块实际占用（= 渲染后估算）
    budget_tokens        ：可用的压缩区余量
    reason               ：块级预算状态 None / "budget_unknown" / "budget_insufficient"
                           （身份剔除是**逐条**事实，不写进 reason —— 见 skipped_for_identity）
    """

    entries: list[McpEvidenceEntry] = field(default_factory=list)
    skipped: list[McpEvidenceEntry] = field(default_factory=list)
    skipped_for_identity: list[McpEvidenceEntry] = field(default_factory=list)
    deduped_round_ids: list[int] = field(default_factory=list)
    used_tokens: int = 0
    budget_tokens: int | None = None
    reason: str | None = None


@dataclass
class EvidenceBuildReport:
    """一次构建的结果（供日志与测试断言）。"""

    round_id: int
    model_id: str
    entries: list[McpEvidenceEntry] = field(default_factory=list)
    runtime_records_total: int = 0
    runtime_unreadable: bool = False  # Runtime 丢失/损坏时的明确标记（§45）


class EvidenceIndexService:
    """Evidence Index 生命周期（Round 完成后 / Compaction 时生成，§48）。"""

    def __init__(self, storage) -> None:
        self.storage = storage

    # ── 路径 ────────────────────────────────────────────────────────

    def evidence_path(self, session_id: str) -> Path:
        d = self.storage.session_dir(session_id) / EVIDENCE_DIRNAME
        d.mkdir(parents=True, exist_ok=True)
        return d / EVIDENCE_FILENAME

    def runtime_path(self, session_id: str, round_id: int) -> Path:
        return (
            self.storage.round_dir(session_id, round_id) / RUNTIME_LOG_NAME
        )

    # ── 读取 Runtime（追溯源头，§16.1：Runtime 是唯一原始档案）──────

    def read_runtime_records(
        self, session_id: str, round_id: int
    ) -> tuple[list[dict], bool]:
        """读 round_NNN/mcp_runtime.jsonl。

        返回 (records, unreadable)。**任何损坏都返回明确的 unreadable 标记**，
        不抛异常 —— Evidence Index 的生成不能因 Runtime 损坏而失败
        （但追溯会如实报告 untraceable）。
        """
        path = self.runtime_path(session_id, round_id)
        if not path.exists():
            return [], True
        records: list[dict] = []
        had_lines = False
        had_bad_lines = False
        try:
            for line in path.read_text(encoding="utf-8").splitlines():
                line = line.strip()
                if not line:
                    continue
                had_lines = True
                try:
                    records.append(json.loads(line))
                except json.JSONDecodeError:
                    # 单行损坏：跳过该行，其余保留
                    had_bad_lines = True
                    continue
        except (OSError, UnicodeDecodeError):
            return [], True
        # 有行但全部损坏 → 明确 unreadable（不能假装"没有 tool call"）
        if had_lines and had_bad_lines and not records:
            return [], True
        return records, False

    # ── 生成（Round 完成后调用）─────────────────────────────────────

    def build_index(
        self,
        *,
        session_id: str,
        round_id: int,
        model_id: str,
        final_answer: str,
    ) -> EvidenceBuildReport:
        """Round 完成后为 (round, model) 构建 Evidence Index（§48）。

        步骤：
          1. 读本轮 mcp_runtime.jsonl 全部 tool_call 记录
          2. 对每条**成功**记录做 used_in_final_answer 判定
             （失败 call 不入 Index —— Error 不是 Evidence）
          3. 追加写 evidence/mcp_evidence.jsonl
          4. 落 context_policy.jsonl 的 mcp_evidence_index 事件
        """
        records, unreadable = self.read_runtime_records(session_id, round_id)
        report = EvidenceBuildReport(
            round_id=round_id, model_id=model_id,
            runtime_records_total=len(records),
            runtime_unreadable=unreadable,
        )

        if unreadable:
            # §45：Runtime 丢失不伪装 —— 生成空报告并在日志中明确标记
            self._log(session_id=session_id, report=report, entries=[])
            return report

        # 只取本模型的记录（多模型不混淆）
        mine = [
            r for r in records
            if str(r.get("model_id", "")) == model_id
        ]

        entries: list[McpEvidenceEntry] = []
        for rec in mine:
            # Error / 失败 call 不是 Evidence（§46：Evidence 只来自成功的 Result）
            if not rec.get("ok"):
                continue
            tool_call = rec.get("tool_call") or {}
            call_id = str(tool_call.get("id", "") or "")
            preview = str(rec.get("result_preview", "") or "")
            mcp = str(rec.get("mcp", "") or "")
            tool = str(rec.get("tool", "") or "")

            used = judge_used_in_final_answer(
                tool_call_id=call_id,
                result_preview=preview,
                final_answer=final_answer,
            )
            entries.append(McpEvidenceEntry(
                round_id=round_id,
                mcp=mcp,
                tool=tool,
                runtime_record_id=call_id or f"{mcp}:{tool}",
                reference=build_reference(mcp, tool, preview, call_id),
                used_in_final_answer=used,
                created_at=_now_iso(),
                source_model_id=model_id,
            ))

        report.entries = entries
        self._append_entries(session_id, entries)
        self._log(session_id=session_id, report=report, entries=entries)
        return report

    # ── 读取 Index ──────────────────────────────────────────────────

    def load(self, session_id: str) -> list[McpEvidenceEntry]:
        path = self.evidence_path(session_id)
        if not path.exists():
            return []
        out: list[McpEvidenceEntry] = []
        for line in path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                out.append(McpEvidenceEntry.from_dict(json.loads(line)))
            except (json.JSONDecodeError, KeyError, ValueError):
                continue  # 单行损坏跳过
        return out

    def load_for_round(self, session_id: str, round_id: int) -> list[McpEvidenceEntry]:
        return [e for e in self.load(session_id) if e.round_id == round_id]

    # ── Evidence → Context 选择（§4.5.3 - §4.5.8）───────────────────

    def select_for_context(
        self,
        *,
        session_id: str,
        model_id: str,
        participants: "Sequence[ModelParticipant]",
        current_round_id: int,
        lossless_round_ids: "Sequence[int] | None" = None,
        budget_tokens: int | None = None,
    ) -> EvidenceContextSelection:
        """选出可以进入**当前模型** Prompt 的 Evidence（§4.5）。

        规则逐条对应规范：

          §4.5.3 A1  ：只读 ``source_model_id == model_id`` 的 Evidence
          §4.5.2     ：必须 ``used_in_final_answer is True``
          §4.5.4     ：只允许已完成的历史 Round（``round_id < current``）
          §4.5.5     ：Round 仍在无损 Context（含 Pin）→ 不重复注入
          §4.5.11    ：reference 命中真实身份词元 → 整条剔除（不降级为告警）
          §4.5.7     ：与 Compacted History 共用 CompactionBudget
          §4.5.8     ：装不下 → 跳过（不删 Runtime / Index、不改 k）

        ``participants`` 必填：身份判定需要真实词元全集，给一个"默认为空"的
        缺省值等于放弃校验，因此不提供默认值（与 prompt_builder 的 A3 硬校验
        同源，调用方传的都是 registry.participants）。

        本方法**只读** Evidence Index 与 Runtime，不写任何原始档案。
        """
        lossless = set(lossless_round_ids or ())
        sel = EvidenceContextSelection(budget_tokens=budget_tokens)

        pool = [
            e for e in self.load(session_id)
            if e.source_model_id == model_id          # A1：仅自己的 Evidence
            and e.used_in_final_answer                # §4.5.2：确实被 Final Answer 使用
            and e.round_id < current_round_id         # §4.5.4：已完成的历史 Round
        ]
        # 与历史上下文一致的"由新到旧"
        pool.sort(key=lambda e: (e.round_id, e.created_at), reverse=True)

        injectable: list[McpEvidenceEntry] = []
        for e in pool:
            if e.round_id in lossless:
                if e.round_id not in sel.deduped_round_ids:
                    sel.deduped_round_ids.append(e.round_id)
                continue
            injectable.append(e)

        # §4.5.11：reference 是外部工具文本，可能带出真实身份词元。
        # 命中即整条剔除（跳过 ≠ 删除：Runtime / Index / k / Pin 一律不动），
        # 并落 mcp_evidence_skipped_for_identity 说明原因。
        injectable, rejected = split_evidence_by_identity(injectable, participants)
        if rejected:
            sel.skipped_for_identity = [e for e, _ in rejected]
            self._log_skipped(
                session_id=session_id, model_id=model_id,
                current_round_id=current_round_id,
                skipped=sel.skipped_for_identity,
                reason=EVIDENCE_SKIP_REASON_IDENTITY,
                budget_tokens=None,
                event=EVIDENCE_SKIP_EVENT_IDENTITY,
                hits_by_record={e.runtime_record_id: h for e, h in rejected},
            )

        if not injectable:
            return sel

        if budget_tokens is None:
            # 预算不可知 → 保守不注入（沿用 ContextSelector 的 unknown 降级约定）
            sel.skipped = list(injectable)
            sel.reason = EVIDENCE_SKIP_REASON_BUDGET_UNKNOWN
            self._log_skipped(
                session_id=session_id, model_id=model_id,
                current_round_id=current_round_id,
                skipped=injectable, reason=sel.reason, budget_tokens=None,
            )
            return sel

        limit = max(0, int(budget_tokens))
        for e in injectable:
            trial = sel.entries + [e]
            cost = _estimate_tokens(render_evidence_block(trial))
            if cost <= limit:
                sel.entries = trial
                sel.used_tokens = cost
            else:
                # §4.5.8：跳过该 Evidence（不动 Runtime / Index / k / Pin）
                sel.skipped.append(e)

        if sel.skipped:
            sel.reason = EVIDENCE_SKIP_REASON_BUDGET
            self._log_skipped(
                session_id=session_id, model_id=model_id,
                current_round_id=current_round_id,
                skipped=sel.skipped, reason=sel.reason,
                budget_tokens=budget_tokens,
            )
        return sel

    def _log_skipped(
        self,
        *,
        session_id: str,
        model_id: str,
        current_round_id: int,
        skipped: "Sequence[McpEvidenceEntry]",
        reason: str,
        budget_tokens: int | None,
        event: str = EVIDENCE_SKIP_EVENT_BUDGET,
        hits_by_record: dict[str, list[str]] | None = None,
    ) -> None:
        """§4.5.8 ``mcp_evidence_skipped_for_budget``：解释该 Evidence 为何未进入 Context。

        ``event`` 可切换为 §4.5.11 的 ``mcp_evidence_skipped_for_identity``：
        两种"跳过"的原因与事件名严格区分，互不冒充。

        旁路日志铁律：整体 try/except 且**绝不抛出** —— 日志失败不得改变
        Prompt 构建行为。
        """
        try:
            policy_path = self.storage.session_dir(session_id) / "context_policy.jsonl"
            policy_path.parent.mkdir(parents=True, exist_ok=True)
            with policy_path.open("a", encoding="utf-8") as fh:
                for e in skipped:
                    rec = {
                        "event": event,
                        "session_id": session_id,
                        "round_id": current_round_id,
                        "model_id": model_id,
                        "evidence_round_id": e.round_id,
                        "mcp": e.mcp,
                        "tool": e.tool,
                        "reason": reason,
                        "budget_tokens": budget_tokens,
                        "timestamp": _now_iso(),
                    }
                    if hits_by_record is not None:
                        # 身份剔除：留下被剔除的文本与命中的词元，便于事后审计
                        rec["reference"] = e.reference
                        rec["identity_hits"] = list(
                            hits_by_record.get(e.runtime_record_id, ())
                        )
                    fh.write(json.dumps(rec, ensure_ascii=False) + "\n")
        except Exception:  # noqa: BLE001 - 日志是旁路，绝不抛出
            pass

    # ── 追溯：Evidence Index → Runtime Record（§44/§47 可追溯链）────

    def trace_to_runtime(
        self, session_id: str, entry: McpEvidenceEntry,
    ) -> dict[str, Any]:
        """定位 entry 对应的完整原始 Runtime 记录。

        返回：
          {"status": "found", "record": {...}}      定位成功（含完整原始 Result）
          {"status": "runtime_missing"}              Runtime 文件不存在
          {"status": "runtime_corrupted"}            Runtime 文件存在但无法解析
          {"status": "record_not_found"}             文件在但无匹配记录
        不抛异常 —— 追溯失败必须是明确状态而不是崩溃。
        """
        path = self.runtime_path(session_id, entry.round_id)
        if not path.exists():
            return {"status": "runtime_missing"}
        records, unreadable = self.read_runtime_records(session_id, entry.round_id)
        if unreadable and not records:
            return {"status": "runtime_corrupted"}

        # 精确匹配：tool_call.id + model_id（多模型不混淆）
        candidates = [
            r for r in records
            if str((r.get("tool_call") or {}).get("id", "")) == entry.runtime_record_id
            and str(r.get("model_id", "")) == entry.source_model_id
        ]
        if not candidates:
            # 兜底：mcp+tool+model 组合（tool_call id 缺失的场景）
            candidates = [
                r for r in records
                if str(r.get("mcp", "")) == entry.mcp
                and str(r.get("tool", "")) == entry.tool
                and str(r.get("model_id", "")) == entry.source_model_id
            ]
        if not candidates:
            return {"status": "record_not_found"}
        return {"status": "found", "record": candidates[0]}

    # ── 内部 ────────────────────────────────────────────────────────

    def _append_entries(
        self, session_id: str, entries: list[McpEvidenceEntry],
    ) -> None:
        if not entries:
            return
        path = self.evidence_path(session_id)
        with path.open("a", encoding="utf-8") as fh:
            for e in entries:
                fh.write(json.dumps(e.to_dict(), ensure_ascii=False) + "\n")

    def _log(
        self,
        *,
        session_id: str,
        report: EvidenceBuildReport,
        entries: list[McpEvidenceEntry],
    ) -> None:
        """context_policy.jsonl 的 mcp_evidence_index 事件（Plan §17.2 row 10）。"""
        line = {
            "event": "mcp_evidence_index",
            "session_id": session_id,
            "round_id": report.round_id,
            "model_id": report.model_id,
            "runtime_records_total": report.runtime_records_total,
            "runtime_unreadable": report.runtime_unreadable,
            # references[]：轻量索引（不含 result 原文 —— 原文只在 Runtime）
            "references": [
                {
                    "mcp": e.mcp,
                    "tool": e.tool,
                    "reference": e.reference,
                    "used_in_final_answer": e.used_in_final_answer,
                }
                for e in entries
            ],
            "timestamp": _now_iso(),
        }
        policy_path = (
            self.storage.session_dir(session_id) / "context_policy.jsonl"
        )
        policy_path.parent.mkdir(parents=True, exist_ok=True)
        with policy_path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(line, ensure_ascii=False) + "\n")
