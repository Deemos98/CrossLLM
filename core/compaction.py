"""Session Compaction（Plan §4.9 §14 + docs/phase4_memory.md §32-§40 §60）。

# ⚠ 与 Conversation Summary 的严格区分（修订 #1，§37 明禁冒充）

| 维度 | **Conversation Summary** | **Session Compaction** |
|---|---|---|
| 隔离粒度 | 按 `model_id` 严格隔离（每模型一份） | **Session 级共享**（所有模型共用一份） |
| 表达对象 | 该模型**自己**的工作状态 | 压缩区内**多个模型**的历史 |
| 人称 | 第 2 人称（"你已经形成的观点…"） | 第 3 人称 + **归属标注** |
| 落盘 | `summaries/<model>/summary_NNN.md` | `compactions/compact_NNN.md` |
| artifact_kind | `"conversation_summary"` | `"session_compaction"` |

**物理保证**：两个目录**没有任何代码路径互相写入**（§4.9.2）。

# 三层防线防止"无来源统一结论"

    F1 Prompt 层  —— COMPACTION_PROMPT 强制"参与者 X 认为"句式
    F2 后置校验层 —— _check_viewpoint_attribution()：无归属标记 → status="failed"
    F2b 身份校验层 —— find_attributed_identity_leak()：真实 model_id 被当作
                     参与者身份归属 → status="failed"（不变量 A3，见下）
    F3 渲染层     —— render_compacted_block() 强制声明来源前缀

# 不变量 A3：Compacted History 不得暴露真实参与模型身份

Compacted History 是 Session 级共享产物，会进入**每一个**模型的 Prompt。
因此它属于"系统生成的跨模型上下文"，必须满足 architecture_v0.1.md A1-A4：

    L1 输入层  _render_input()             —— 用 assign_candidate_ids 的
                                              Candidate 标签替代真实 model_id
    L2 输出层  find_attributed_identity_leak()
                                          —— LLM 输出若仍把真实 model_id 当
                                             参与者身份 → 走 F2 同一降级路径
    L3 渲染层  render_compacted_block()     —— 标题不写入真实 contributor_models
    L4 终检    prompt_builder 的 A3 骨架硬校验
                                          —— 覆盖 Compacted 块的系统生成头部，
                                             不再因 mid="__compacted__" 绕过

内部 provenance（日志 / frontmatter / CompactionRecord.contributor_models）
仍保留真实 model_id：本不变量只约束**模型可见内容**。

# 递归压缩（§38）

    若 compacted + recent_raw + pinned + fixed + reserved > context_limit
    → 扩大压缩区（把更多旧 Round 纳入）继续压缩
    → 最多 MAX_COMPACTION_ROUNDS 次，超出则降级（保留 raw，记 overflow）

# 明确禁止恢复的旧逻辑

    ❌ 80% ratio trigger（§10.4 已删）
    ❌ HISTORY_WINDOW=3（§11.1 已删）
    ❌ MCP Result → 自动 Fact（§45/§46）
"""

from __future__ import annotations

import json
import re
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal, Sequence

from .anonymizer import assign_candidate_ids
from .model_capability import TokenEstimate
from .storage import atomic_write_text

# ── 递归终止条件（§38）──────────────────────────────────────────────────
MAX_COMPACTION_ROUNDS = 3

# ── 落盘目录（§4.9.2：与 summaries/ 严格分离）──────────────────────────
COMPACTIONS_DIRNAME = "compactions"
COMPACT_FILE_PREFIX = "compact_"
COMPACT_FILE_SUFFIX = ".md"
COMPACT_NAME_RE = re.compile(r"^compact_(\d{3})\.md$")

ARTIFACT_KIND = "session_compaction"


def _iso_now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def _estimate_tokens(text: str) -> TokenEstimate:
    """粗略 token 估算（与 ValidRound / SummaryService 同口径）。"""
    return TokenEstimate(
        value=max(1, -(-len(text) // 3)),
        source="crossllm_estimator",
        accuracy_mode="estimated",
    )


# ── A3：Compacted History 的真实身份判定（Phase 4.5 单点修复）────────────
#
# 问题：Compaction 输入曾用真实 model_id 标注参与者（`参与者 qwen-plus：…`），
# `render_compacted_block()` 又把真实 contributor_models 写进标题。Compacted
# History 会进入**每一个**模型的 Prompt ⇒ 违反不变量 A3。
#
# 为什么本层不用 `find_identity_tokens` 做全文子串判定：该函数是子串语义，
# 1-2 字符的 model_id（测试环境常见的 "a"/"b"）会命中任意含该字母的文本，
# 产生确定性假阳性（Phase 4.2 Summary 修复时已踩过同一坑）。因此本层只判定
# **归属位置**上的名称与原身份词元**完全相等**的情形：精确、可解释、无系数。
#
# 正文中偶然提到模型名不属于本层职责 —— 那由 prompt_builder 的软扫描负责
# （沿用 anonymizer 模块说明中"系统生成文本硬校验 / LLM 正文软扫描"的范围约定）。

#: 系统生成的 Compacted 块标题前缀（F3 来源声明，prompt_builder 终检依赖它）。
COMPACTED_HEADER_PREFIX = "## 历史压缩摘要"

#: 归属位置：`参与者 X` / `模型 X` / `Model X` / `Participant X`。
#: 捕获 1 = 紧随其后的名称词元；捕获 2 = 可选的第二个词元（覆盖 "Qwen Plus"
#: 这类带空格的 display_name）。刻意不做通配吞噬 —— 否则后接的英文动词会被
#: 一起吞掉（"Participant qwen-plus says" 必须仍能命中 qwen-plus）。
_ATTRIBUTED_NAME_RE = re.compile(
    r"(?:参与者|Participant|模型|Model)\s*[:：]?\s*"
    r"([A-Za-z0-9][A-Za-z0-9._/:+-]*)"
    r"(?:\s+([A-Za-z0-9][A-Za-z0-9._/:+-]*))?"
)
#: 归属名尾部可能粘连的标点
_NAME_TAIL_RE = re.compile(r"[._/:+,;、]+$")
_WS_RUN_RE = re.compile(r"\s+")


def _canon_name(text: str) -> str:
    """身份比较用的规范化：去首尾空白与尾标点、压缩内部空白、统一小写。"""
    s = _NAME_TAIL_RE.sub("", (text or "").strip())
    return _WS_RUN_RE.sub(" ", s).lower()


def find_attributed_identity_leak(
    text: str, model_ids: Sequence[str],
) -> list[str]:
    """返回被当作**参与者身份归属**写进 `text` 的真实 model_id 列表。

    只判定 `参与者 <name>` 这类归属位置，且 `<name>` 归一化后与某个真实
    model_id **完全相等**（大小写 / 空白不敏感）。`<name>` 按单 token 与
    "两个 token 组成的名字"两种形态各试一次，因此 `Qwen Plus` 这类带空格的
    display_name 也能命中，而后接的英文动词不会造成误判。
    """
    if not text or not model_ids:
        return []
    known = {_canon_name(m): m for m in model_ids if m}
    hits: list[str] = []
    for m in _ATTRIBUTED_NAME_RE.finditer(text):
        first, second = m.group(1), m.group(2)
        candidates = [_canon_name(first)]
        if second:
            candidates.append(_canon_name(f"{first} {second}"))
        for cand in candidates:
            real = known.get(cand)
            if real is not None and real not in hits:
                hits.append(real)
    return hits


def compacted_system_header(rendered_block: str) -> str:
    """取出渲染后 Compacted 块中**系统生成**的头部（首个空行之前）。

    仅用于让 prompt_builder 的最终 A3 硬校验覆盖 Compacted 块的骨架；
    LLM 生成的正文不是系统生成文本，只做软扫描。
    """
    return (rendered_block or "").split("\n\n", 1)[0].strip()


# ── 数据结构（Plan §4.9）────────────────────────────────────────────────


@dataclass
class CompactionRecord:
    """一次压缩尝试的完整元数据（§60 可审计）。

    Field：
      attempt                   §38 递归次数（1 = 第一次）
      source_rounds             参与本次压缩的 Round 列表
      source_summary_files      递归时上一轮的产物（compact_NNN.md 路径）
      contributor_models        参与压缩区的模型 id 列表（修订 #1）
      viewpoint_attribution     压缩文本是否保留观点归属标注（False ⇒ 失败降级）
      artifact_kind             恒为 "session_compaction"（≠ conversation_summary）
      input_tokens              输入侧估算
      expected_summary_tokens   §40 预算估算（压缩前期望输出大小）
      actual_summary_tokens     实际输出估算
      summary_file              相对路径，可追溯
      status                    "success" / "failed"
      observed_compression_ratio §39 实测压缩比（仅作预算估计，非理论比例）
      error_message             失败原因
    """

    attempt: int
    source_rounds: list[int]
    contributor_models: list[str]
    viewpoint_attribution: bool
    input_tokens: TokenEstimate
    expected_summary_tokens: TokenEstimate
    actual_summary_tokens: TokenEstimate
    status: Literal["success", "failed"]
    artifact_kind: str = ARTIFACT_KIND
    source_summary_files: list[str] = field(default_factory=list)
    summary_file: str | None = None
    observed_compression_ratio: float | None = None
    error_message: str | None = None

    def __post_init__(self) -> None:
        # 防冒充：artifact_kind 绝不能是 conversation_summary
        assert self.artifact_kind == ARTIFACT_KIND, (
            f"CompactionRecord.artifact_kind 必须恒为 "
            f"{ARTIFACT_KIND!r}；got {self.artifact_kind!r}"
        )
        assert self.attempt >= 1, f"attempt 必须 >= 1；got {self.attempt}"
        assert self.source_rounds, "CompactionRecord.source_rounds 不能为空"

    def to_dict(self) -> dict:
        return {
            "attempt": self.attempt,
            "source_rounds": list(self.source_rounds),
            "source_summary_files": list(self.source_summary_files),
            "contributor_models": list(self.contributor_models),
            "viewpoint_attribution": self.viewpoint_attribution,
            "artifact_kind": self.artifact_kind,
            "input_tokens": self.input_tokens.value,
            "expected_summary_tokens": self.expected_summary_tokens.value,
            "actual_summary_tokens": self.actual_summary_tokens.value,
            "summary_file": self.summary_file,
            "status": self.status,
            "observed_compression_ratio": self.observed_compression_ratio,
            "error_message": self.error_message,
        }


@dataclass
class CompactedBlock:
    """压缩产物（可含多条 record —— 递归时每次 attempt 一条）。"""

    content: str
    source_files: list[str]
    records: list[CompactionRecord]
    contributor_models: list[str]
    is_shared: bool = True  # Session 级共享标记

    @property
    def artifact_kind(self) -> str:
        return ARTIFACT_KIND

    @property
    def latest(self) -> CompactionRecord | None:
        return self.records[-1] if self.records else None

    def token_estimate(self) -> TokenEstimate:
        return _estimate_tokens(self.content)


# ── COMPACTION_PROMPT（§36 结构 + F1 归属强制指令）──────────────────────

COMPACTION_PROMPT = """请将下面这段**多参与者对话历史**压缩为一份更短的 Session 级共享摘要。

【最重要 —— 观点归属，违反即视为压缩失败】
- 必须保留每个观点的来源归属，使用"参与者 X 认为 / 参与者 Y 反对"这类句式。
- 不得将多方观点合并为无主语的陈述句（例如"大家最终认为 X"）。
- 若多个参与者观点相同，也要分别列出各自的表述与依据，不能合并成统一结论。
- 保留分歧：只要存在不同意见，就必须显式写出谁持什么立场、依据是什么。

【参与者标识 —— 不变量 A3，违反即视为压缩失败】
- 输入里的参与者已用匿名标签（如 "Candidate A"）标注，**原样沿用**即可。
- 不得推测、补写或还原任何真实模型名称 / 厂商名 / 模型 ID；
  摘要中也不得出现这类真实身份（无论是你自己推断的还是从正文里抄的）。
- 你只需表达"这是不同参与者的观点"，不需要知道它们对应哪个真实模型。

【输出结构建议（可按实际情况增删，不作强制 schema）】
- 用户目标（User Goals）
- 已确立信息（Established Information）
- 各参与者观点（Model Opinions）—— **逐人标注**
- 观点归属（Viewpoint Attribution）—— 明确"谁说的"
- 推理依据（Reasoning / Basis）
- 分歧（Disagreements）
- 未验证假设（Unverified Assumptions）
- 已尝试 / 已排除（Tried / Rejected）
- 未解决问题（Open Questions）
- 当前方向（Current Direction）
- MCP 外部证据引用（MCP Evidence References，如存在）

【语义红线】
- 不要把参与者观点升级为已确认事实。
- 不要把 MCP 工具返回的原始结果当作已证实的事实，只能作为"引用的外部证据"标注来源。
- 允许保留：观点、假设、分歧、未验证内容、未知。不要为了简洁而消除这些状态。
- 不要引入原始对话中不存在的新事实。

【输入】
下面是要压缩的对话历史（多参与者，含用户问题与各参与者的成功回答）：
"""


# ── F2 后置校验：观点归属检测 ───────────────────────────────────────────

# 归属标记：参与者名 / Candidate 编号 / 模型发言标记
_ATTRIBUTION_PATTERNS = (
    re.compile(r"参与者\s*[A-Za-z0-9]", re.IGNORECASE),
    re.compile(r"Participant\s*[A-Za-z0-9]", re.IGNORECASE),
    re.compile(r"Candidate[- ]?[A-Za-z0-9]", re.IGNORECASE),
    re.compile(r"\[Candidate[- ]?[A-Za-z0-9]\]"),
    re.compile(r"模型\s*[A-Za-z0-9]", re.IGNORECASE),
    re.compile(r"Model\s*[A-Za-z0-9]", re.IGNORECASE),
)


def check_viewpoint_attribution(text: str) -> bool:
    """§4.9 落地约束 2：压缩文本中必须出现归属标记。

    无任何归属标记 → 视为"无来源统一结论" → 调用方应标 failed 并降级。
    """
    if not text or not text.strip():
        return False
    return any(p.search(text) for p in _ATTRIBUTION_PATTERNS)


# ── F3 渲染层：强制来源声明前缀 ─────────────────────────────────────────

def render_compacted_block(block: CompactedBlock) -> str:
    """§4.9 落地约束 3：Compacted 块标题必须声明来源（**不含真实身份**）。

        ## 历史压缩摘要（Session 级共享，含多位参与者的观点，
           非本模型个人总结）

    ⚠ 不变量 A3（Phase 4.5 修复）：本块进入**所有**模型的 Prompt，因此标题
    绝不写入真实 `contributor_models`。PHASE4_REFACTOR_PLAN §4.9.1 示例中的
    "参与者 A/B/C" 只是示意；实际参与者标签由压缩正文内的 Candidate 标签承担
    （见 `_render_input()`）。标题不再列举标签，避免标签与真实 id 建立对应关系。
    """
    header = (
        f"{COMPACTED_HEADER_PREFIX}"
        "（Session 级共享，含多位参与者的观点，非本模型个人总结）"
    )
    src = ""
    if block.source_files:
        src = f"\n\n（压缩产物：{', '.join(block.source_files)}）"
    return f"{header}\n\n{block.content}{src}"


# ── CompactionService ───────────────────────────────────────────────────


class CompactionService:
    """Session 级共享压缩（§14）。

    依赖注入：
      storage           core.storage.Storage
      provider_factory  Callable[[str], object]（复用底层 LLM 调用能力，§7.5 允许）
      model_id          用哪个模型执行压缩（默认取 participants 中第一个）

    递归：
      attempt 从 1 开始，每次失败/超限 → attempt+1，最多 MAX_COMPACTION_ROUNDS。
      终止条件（三者任一）：
        ① attempt > MAX_COMPACTION_ROUNDS
        ② 压缩产物满足预算
        ③ 压缩区已无可压缩内容（source_rounds 为空）
    """

    def __init__(
        self,
        storage,
        provider_factory=None,
        *,
        max_rounds: int = MAX_COMPACTION_ROUNDS,
    ) -> None:
        self.storage = storage
        self._provider_factory = provider_factory
        self.max_rounds = max_rounds

    # ── 路径（§4.9.2：compactions/ 独立目录）────────────────────────

    def compactions_dir(self, session_id: str) -> Path:
        d = self.storage.session_dir(session_id) / COMPACTIONS_DIRNAME
        d.mkdir(parents=True, exist_ok=True)
        return d

    def compact_path(self, session_id: str, index: int) -> Path:
        return (
            self.compactions_dir(session_id)
            / f"{COMPACT_FILE_PREFIX}{index:03d}{COMPACT_FILE_SUFFIX}"
        )

    def _next_index(self, session_id: str) -> int:
        d = self.compactions_dir(session_id)
        idx = 0
        for p in d.glob(f"{COMPACT_FILE_PREFIX}*{COMPACT_FILE_SUFFIX}"):
            m = COMPACT_NAME_RE.match(p.name)
            if m and int(m.group(1)) > idx:
                idx = int(m.group(1))
        return idx + 1

    # ── 主入口（递归）───────────────────────────────────────────────

    async def compact(
        self,
        *,
        session_id: str,
        rounds: Sequence,
        compaction_budget: int | None = None,
        model_id: str | None = None,
        trigger_round_id: int | None = None,
        attempt_start: int = 1,
        previous_content: str | None = None,
        previous_files: Sequence[str] | None = None,
        recursive_within_call: bool = True,
        covered_rounds: Sequence[int] | None = None,
    ) -> CompactedBlock:
        """压缩 `rounds`（ValidRound 序列）→ CompactedBlock。

        递归：若产物仍超 `compaction_budget`，把更旧内容纳入再压一次，
        直到满足预算或达到 `max_rounds`。

        `rounds` 只允许 ValidRound（失败 Round 由调用方在 ValidRoundPoolBuilder
        阶段已过滤，§14 输入约束）。

        §38 Session 级递归（本次新增，均为可选参数，默认行为不变）：
          ``attempt_start``     本次调用在 Session 级递归中的起始 attempt 编号
                                （全局 attempt 必须递增，受 MAX_COMPACTION_ROUNDS 约束）
          ``previous_content``  上一轮 Compacted History 正文
          ``previous_files``    上一轮产物文件（写入 source_summary_files 以便审计）
          ``recursive_within_call``
                                True（默认，保持既有行为）：本次调用内若产物仍超
                                compaction_budget → 继续压**上一轮产物**（summary of
                                summary）。
                                False：只压一次，把"重新构造 Context → 仍超限 →
                                纳入更旧 Valid Round 再压"的递归交由调用方
                                （RoundManager）实现（§38 Session 级递归）。
          ``covered_rounds``    本次产物**累计覆盖**的 Round 集合（含上一轮已覆盖的）。
                                默认 None → 只记本次传入的 rounds（既有行为）。
                                Session 级递归时由调用方传入，使日志中的
                                source_rounds 体现"覆盖范围扩大"（§38）。

        调用方（RoundManager）负责"纳入更旧 Valid Round"，本服务只忠实压缩
        传入的 rounds（+ 可选上一轮产物），不自己挑选历史。
        """
        if not rounds:
            raise ValueError("CompactionService.compact 要求至少一个 ValidRound")

        ordered = sorted(rounds, key=lambda r: r.round_id)
        records: list[CompactionRecord] = []
        source_files: list[str] = list(previous_files or [])
        # §38：日志中的 source_rounds = 本次产物累计覆盖范围（默认为本次 rounds）
        _source_rounds: list[int] = sorted(
            {int(x) for x in covered_rounds}
            if covered_rounds is not None
            else {r.round_id for r in ordered}
        )

        attempt = max(1, int(attempt_start))
        last_block: CompactedBlock | None = None
        # §38 递归：pending_text=None 表示压原始 rounds；
        # 非 None 表示压**上一轮产物**（summary of summary），
        # 对应 CompactionRecord.source_summary_files。
        pending_text: str | None = None

        while attempt <= self.max_rounds:
            # 构造输入文本
            if pending_text is None:
                # 首轮：渲染原始 rounds（归属标注使用**匿名** Candidate 标签，A3）
                input_text = self._render_input(ordered)
                if previous_content:
                    # §38：上一轮 Compacted History + 更旧 Raw Rounds 一起压
                    input_text = (
                        "## 上一轮压缩产物（Compacted History）\n"
                        f"{previous_content}\n\n"
                        "## 更旧历史（尚未压缩的 Valid Rounds）\n"
                        f"{input_text}"
                    )
            else:
                # 递归：压缩上一轮产物
                input_text = pending_text
            contributor_models = self._collect_contributors(ordered)
            in_te = _estimate_tokens(input_text)

            # §40 预算估算：期望输出 ≈ min(budget, input/3)
            if compaction_budget is not None:
                expected_v = max(1, min(compaction_budget, (in_te.value or 1) // 3))
            else:
                expected_v = max(1, (in_te.value or 1) // 3)
            expected_te = TokenEstimate(
                value=expected_v,
                source="crossllm_estimator",
                accuracy_mode="estimated",
            )

            # 调 LLM（失败降级不抛异常）
            content, err = await self._invoke(model_id=model_id, input_text=input_text)

            if err is not None:
                rec = CompactionRecord(
                    attempt=attempt,
                    source_rounds=list(_source_rounds),
                    contributor_models=contributor_models,
                    viewpoint_attribution=False,
                    input_tokens=in_te,
                    expected_summary_tokens=expected_te,
                    actual_summary_tokens=TokenEstimate(
                        0, "crossllm_estimator", "estimated"
                    ),
                    status="failed",
                    source_summary_files=list(source_files),
                    error_message=err,
                )
                records.append(rec)
                self._log(
                    session_id=session_id, record=rec,
                    trigger_round_id=trigger_round_id, reused=False,
                )
                # 降级：返回已有产物（无则空 content），不中断 Round
                return CompactedBlock(
                    content=last_block.content if last_block else "",
                    source_files=source_files,
                    records=records,
                    contributor_models=contributor_models,
                )

            # F2：归属校验
            attributed = check_viewpoint_attribution(content)
            # F2b：A3 身份校验 —— LLM 输出若仍把真实 model_id 当作参与者身份
            # 归属（例如把输入里的答案正文原样回抄），则视为失败：不落盘、
            # 不注入，复用 F2 同一条降级路径（§14.5）。
            identity_leaks = find_attributed_identity_leak(
                content, contributor_models,
            )
            ok = attributed and not identity_leaks
            actual_te = _estimate_tokens(content)
            ratio = (
                round((in_te.value or 0) / max(1, actual_te.value), 3)
                if in_te.value else None
            )

            # 落盘（仅成功且归属 + 身份校验均通过才落，§14.2）
            summary_file: str | None = None
            if ok:
                idx = self._next_index(session_id)
                rel = (
                    f"{COMPACTIONS_DIRNAME}/"
                    f"{COMPACT_FILE_PREFIX}{idx:03d}{COMPACT_FILE_SUFFIX}"
                )
                full = self.storage.session_dir(session_id) / rel
                atomic_write_text(
                    full,
                    "---\n"
                    + json.dumps(
                        {
                            "artifact_kind": ARTIFACT_KIND,
                            "session_id": session_id,
                            "attempt": attempt,
                            "source_rounds": list(_source_rounds),
                            "contributor_models": contributor_models,
                            "viewpoint_attribution": attributed,
                            "created_at": _iso_now(),
                        },
                        ensure_ascii=False,
                        indent=2,
                    )
                    + "\n---\n\n"
                    + content
                    + "\n",
                )
                summary_file = rel
                source_files.append(rel)

            rec = CompactionRecord(
                attempt=attempt,
                source_rounds=list(_source_rounds),
                contributor_models=contributor_models,
                viewpoint_attribution=attributed,
                input_tokens=in_te,
                expected_summary_tokens=expected_te,
                actual_summary_tokens=actual_te,
                status="success" if ok else "failed",
                source_summary_files=list(source_files[:-1]),
                summary_file=summary_file,
                observed_compression_ratio=ratio,
                error_message=(
                    None if ok
                    else (
                        "identity_leak_in_compacted_content"
                        if identity_leaks
                        else "viewpoint_attribution_missing"
                    )
                ),
            )
            records.append(rec)
            self._log(
                session_id=session_id, record=rec,
                trigger_round_id=trigger_round_id, reused=False,
            )

            if not ok:
                # F2 / F2b 失败 → 降级（不重试：Prompt 已强制归属与匿名，
                # 重试意义有限；原始历史不受影响，§69）
                return CompactedBlock(
                    content=last_block.content if last_block else "",
                    source_files=source_files,
                    records=records,
                    contributor_models=contributor_models,
                )

            block = CompactedBlock(
                content=content,
                source_files=list(source_files),
                records=records,
                contributor_models=contributor_models,
            )
            last_block = block

            # §38 递归终止判定：产物是否满足预算
            if compaction_budget is None or actual_te.value <= compaction_budget:
                return block

            if not recursive_within_call:
                # Session 级递归（RoundManager）：本调用只压一次，
                # 是否纳入更旧历史由调用方决定。
                return block

            # 仍超预算 → 递归：把**本次产物**作为下一轮输入再压一次
            pending_text = content
            attempt += 1

        # 达到 MAX_COMPACTION_ROUNDS：降级返回最后一次产物
        return CompactedBlock(
            content=last_block.content if last_block else "",
            source_files=source_files,
            records=records,
            contributor_models=self._collect_contributors(ordered),
        )

    # ── 复用（§14.4）────────────────────────────────────────────────

    def find_reusable(
        self, *, session_id: str, source_rounds: Sequence[int],
    ) -> CompactedBlock | None:
        """§14.4：compactions/ 已有产物且其 source_rounds 覆盖当前压缩区 → 复用。

        ⚠ 复用时仍必须保持 `viewpoint_attribution=True`（修订 #1 归属校验）。
        """
        d = self.compactions_dir(session_id)
        want = set(source_rounds)
        best: CompactedBlock | None = None
        best_cover = -1
        for p in sorted(d.glob(f"{COMPACT_FILE_PREFIX}*{COMPACT_FILE_SUFFIX}")):
            obj = self._read_frontmatter(p)
            if obj is None:
                continue
            if obj.get("artifact_kind") != ARTIFACT_KIND:
                continue  # 防冒充：只认 session_compaction
            if obj.get("viewpoint_attribution") is not True:
                continue  # 归属缺失的产物不可复用
            cover = len(want & set(obj.get("source_rounds", [])))
            if cover > best_cover:
                best_cover = cover
                best = self._load_block(session_id, p, obj)
        if best is None or best_cover <= 0:
            return None
        return best

    # ── 内部 ────────────────────────────────────────────────────────

    def _render_input(self, rounds: Sequence) -> str:
        """把 ValidRound 序列渲染成压缩输入（含**匿名**归属标注）。

        ⚠ 不变量 A3：真实 model_id 绝不进入压缩 Prompt。参与者改用
        ``anonymizer.assign_candidate_ids`` 生成的 Candidate 标签 ——
        标签只表达"这是不同参与者的观点"，不携带任何真实身份（§二.2）。
        """
        labels = assign_candidate_ids(self._collect_contributors(rounds))
        lines: list[str] = []
        for r in rounds:
            lines.append(f"## 第 {r.round_id} 轮")
            lines.append(f"用户问题：{r.user_question}")
            for mid, ans in sorted(r.successful_answers.items()):
                # 兜底也绝不回退到真实 model_id（宁可中性，也不泄漏）
                lines.append(f"参与者 {labels.get(mid, '某参与者')}：{ans}")
            lines.append("")
        return "\n".join(lines)

    def _collect_contributors(self, rounds: Sequence) -> list[str]:
        models: set[str] = set()
        for r in rounds:
            models.update(r.successful_answers.keys())
        return sorted(models)

    async def _invoke(
        self, *, model_id: str | None, input_text: str,
    ) -> tuple[str, str | None]:
        """调 LLM 压缩。失败不抛异常，返回 (content, error_message)。"""
        if self._provider_factory is None:
            # 无 provider：返回带归属标注的占位（便于单测）
            stub = (
                "参与者 stub 认为：占位压缩结果。\n"
                f"（输入长度 {len(input_text)} chars）"
            )
            return stub, None
        try:
            mid = model_id or "default"
            provider = self._provider_factory(mid)
            buf: list[str] = []
            agen = provider.generate(
                round_id=-1, prompt=f"{COMPACTION_PROMPT}\n\n{input_text}",
            )
            if hasattr(agen, "__aiter__"):
                async for chunk in agen:
                    if chunk:
                        buf.append(chunk)
            else:
                for chunk in agen:
                    if chunk:
                        buf.append(chunk)
            content = "".join(buf)
            if not content.strip():
                return "", "model_returned_empty_content"
            return content, None
        except Exception as exc:  # noqa: BLE001 - 失败降级
            return "", f"{type(exc).__name__}: {exc}"

    def _read_frontmatter(self, path: Path) -> dict | None:
        try:
            raw = path.read_text(encoding="utf-8")
        except (FileNotFoundError, OSError):
            return None
        if not raw.startswith("---\n"):
            return None
        end = raw.find("\n---\n", 4)
        if end < 0:
            return None
        try:
            return json.loads(raw[4:end])
        except json.JSONDecodeError:
            return None

    def _load_block(
        self, session_id: str, path: Path, obj: dict,
    ) -> CompactedBlock:
        raw = path.read_text(encoding="utf-8")
        end = raw.find("\n---\n", 4)
        content = raw[end + 5:].lstrip("\n") if end >= 0 else raw
        rel = f"{COMPACTIONS_DIRNAME}/{path.name}"
        record = CompactionRecord(
            attempt=int(obj.get("attempt", 1)),
            source_rounds=list(obj.get("source_rounds", [])),
            contributor_models=list(obj.get("contributor_models", [])),
            viewpoint_attribution=bool(obj.get("viewpoint_attribution")),
            input_tokens=_estimate_tokens(content),
            expected_summary_tokens=_estimate_tokens(content),
            actual_summary_tokens=_estimate_tokens(content),
            status="success",
            summary_file=rel,
        )
        return CompactedBlock(
            content=content,
            source_files=[rel],
            records=[record],
            contributor_models=record.contributor_models,
        )

    # ── 日志（§60 / Plan §17.2）─────────────────────────────────────

    def _log(
        self,
        *,
        session_id: str,
        record: CompactionRecord,
        trigger_round_id: int | None,
        reused: bool,
    ) -> None:
        line = {
            "event": "compaction",
            "session_id": session_id,
            "round_id": trigger_round_id,
            "source_rounds": list(record.source_rounds),
            "input_tokens": record.input_tokens.value,
            "expected_summary_tokens": record.expected_summary_tokens.value,
            "actual_summary_tokens": record.actual_summary_tokens.value,
            "summary_file": record.summary_file,
            "attempt": record.attempt,
            "status": record.status,
            # 修订 #1：来源语义保留的三个可审计字段
            "contributor_models": list(record.contributor_models),
            "viewpoint_attribution": record.viewpoint_attribution,
            "artifact_kind": record.artifact_kind,
            "observed_compression_ratio": record.observed_compression_ratio,
            "error_message": record.error_message,
            "reused": reused,
            "timestamp": _iso_now(),
        }
        policy_path = (
            self.storage.session_dir(session_id) / "context_policy.jsonl"
        )
        policy_path.parent.mkdir(parents=True, exist_ok=True)
        with policy_path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(line, ensure_ascii=False) + "\n")
