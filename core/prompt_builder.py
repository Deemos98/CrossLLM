"""Prompt 组装（architecture_v0.1.md P2/P3、phase1_mock.md §8/§14、phase1_1 §3/§4/§5）。

S3.5（2026-09-03）改造：
  - 不再使用"最近 3 轮"硬窗口（§11）；改由 ContextSelector 预算驱动倒序装载
  - HistoricalContextItem 包含 user_question；render 时同时输出 Q + As（§31 不可分）
  - 历史单位即 Round：User Question + 该 Round 所有成功 Final Answers

本模块只消费**已经由 anonymizer 标准化**的上下文（per-round [AnonymousEntry]），
不自行拼装任何跨模型的身份信息。

互评开关（phase1_1 §3）：
  互评只决定是否加入 peer context，**不影响模型自己的普通聊天上下文**。
    OFF = 正常多轮对话，各模型只看到自己的历史
    ON  = 自己的历史 + 其他 ACTIVE 模型的匿名历史
  单模型时即使开关打开也没有 peer context，等价于 OFF（§3.3）。

身份校验分两类（D2 结论）：

  1. **CrossLLM 自己生成的系统身份信息** —— 硬校验，发现泄漏直接失败。
  2. **真实 LLM 返回的回答正文** —— 软扫描，命中只记 warning，不阻断整轮。

校验不覆盖用户输入的问题与指令 —— 用户完全可能提出「比较两个模型的差异」，
那不是系统注入的身份信息。
"""

from __future__ import annotations

import typing
from collections.abc import Sequence
from dataclasses import replace

from .anonymizer import (
    AnonymousEntry,
    assert_no_identity_leak,
    find_identity_tokens,
)
from .compaction import compacted_system_header
from .evidence_index import (
    McpEvidenceEntry,
    render_evidence_block,
    split_evidence_by_identity,
)
from .state import ModelParticipant

if typing.TYPE_CHECKING:  # pragma: no cover
    from core.context_selector import HistoricalContextItem
    from core.peer_context import PeerContextItem

# 系统公共指令（architecture_v0.1.md P2：Round 1 只收 原始问题 + 系统公共指令）
# 内容中立，不含任何模型身份信息。
SYSTEM_INSTRUCTION = """你是一名平级参与者，正在与其他参与者共同回答同一个问题。

规则：
1. 所有参与者地位相等，不存在裁判、主席或最终裁决者。
2. 你可以评价、质疑、吸收其他参与者的观点，也可以修改自己的方案。
3. 请直接给出你的分析与结论，使用 Markdown 格式。"""


def render_anonymous_context(entries: Sequence[AnonymousEntry]) -> str:
    """把单个来源轮的匿名条目渲染成上下文块。

    输出中只出现 candidate_id，不出现任何 model_id / display_name / provider model 名。
    candidate_id 为空串时表示"互评关闭"模式下的自己的历史，不渲染任何标签。
    """
    parts: list[str] = []
    for e in entries:
        if e.is_self:
            label = "你的回答" + (f" [{e.candidate_id}]" if e.candidate_id else "")
            parts.append(f"{label}：\n{e.content}")
        else:
            parts.append(f"[{e.candidate_id}]\n{e.content}")
    return "\n\n".join(parts)


def render_history(rounds_entries: Sequence[tuple[int, Sequence[AnonymousEntry]]]) -> str:
    """按来源轮次分组渲染历史上下文，由新到旧。

    rounds_entries: [(round_id, entries), ...]，已按新→旧排好。
    """
    blocks: list[str] = []
    for round_id, entries in rounds_entries:
        block = render_anonymous_context(entries)
        if block:
            blocks.append(f"## 第 {round_id} 轮\n\n{block}")
    return "\n\n".join(blocks)


def build_round1_prompt(
    *,
    original_question: str,
    participants: Sequence[ModelParticipant],
) -> str:
    """Round 1 prompt（architecture_v0.1.md P2、phase2 §11）。

    只有「原始问题 + 系统公共指令」，**禁止**注入任何其他模型的回答。
    """
    assert_no_identity_leak(SYSTEM_INSTRUCTION, participants)
    return f"{SYSTEM_INSTRUCTION}\n\n原始问题：\n{original_question}"


def build_round_n_prompt(
    *,
    original_question: str,
    user_instruction: str,
    rounds_entries: Sequence[tuple[int, Sequence[AnonymousEntry]]],
    participants: Sequence[ModelParticipant],
) -> tuple[str, list[str]]:
    """Round 2+ prompt（architecture_v0.1.md P3、phase1_mock.md §14、phase1_1 §3/§4）。

    rounds_entries 由调用方按「已过滤、已按新→旧排序、已做预算裁剪」准备好；
    本函数不再接触任何 model_id —— 匿名化已在上游完成（不变量 A2）。

    返回 (prompt, warnings)。warnings 是**注入正文中**命中的身份词，
    仅供记录，调用方不应据此中断流程。
    """
    # 软扫描：回答正文由模型生成，命中只告警
    warnings: list[str] = []
    for round_id, entries in rounds_entries:
        for e in entries:
            for token in find_identity_tokens(e.content, participants):
                warnings.append(
                    f"第 {round_id} 轮 {e.candidate_id or '自己'} 的回答正文"
                    f"命中身份词 {token!r}（模型自我披露，非系统泄露）"
                )

    # 硬校验：只校验系统生成的骨架（正文已剥离）
    skeleton_entries = [
        (round_id, [replace(e, content="") for e in entries])
        for round_id, entries in rounds_entries
    ]
    skeleton = render_history(skeleton_entries)
    assert_no_identity_leak(f"{SYSTEM_INSTRUCTION}\n\n{skeleton}", participants)

    history_block = render_history(rounds_entries)

    parts = [SYSTEM_INSTRUCTION, f"原始问题：\n{original_question}"]
    if history_block:
        parts.append(f"历史上下文（由新到旧）：\n\n{history_block}")
    parts.append(f"用户本轮指令：\n{user_instruction}")
    return "\n\n".join(parts), warnings


# ─────────────────────────────────────────────────────────────────────────────
# S3.5：新版 Round 2+ Prompt —— 接受 HistoricalContextItem（含 user_question）
# ─────────────────────────────────────────────────────────────────────────────


def render_historical_item_block(
    item: "HistoricalContextItem",
    *,
    self_model_id: str,
    candidate_ids: dict[str, str] | None,
) -> tuple[str, list[AnonymousEntry]]:
    """把 HistoricalContextItem → (渲染块, AnonymousEntry 列表)。

    渲染块包含（§31 强制）：
      ## 第 N 轮 [source]
      用户问题：Q
      [你的回答] / [Candidate-X]：
        A1

    S3.8 例外 —— ``source == "compacted"``：
      Session Compaction 产物**不是**某个 Round 的 Q&A，
      而是多参与者历史的共享压缩表示。它的归属声明已由
      ``CompactionService.render_compacted_block()`` 内置（F3 防线），
      故此处**整体透传**，不再套"第 N 轮"标题、也不做 Q/A 拆分。

    同步返回 AnonymousEntry list（用于 anonymizer 链路）。
    """
    # S3.8：Compaction 块整体透传（不拆 Q&A、不加轮次标题）
    if item.source == "compacted":
        content = item.answers[0][1] if item.answers else ""
        return content, []

    cand = candidate_ids or {}
    entries: list[AnonymousEntry] = []
    answer_blocks: list[str] = []

    for mid, content in item.answers:
        if mid == self_model_id:
            cid = cand.get(mid, "")  # self 是本轮参与者，必有 candidate
            # 格式沿用 anonymizer.render_anonymous_context：self 也带 candidate 标签
            label = "你的回答" + (f" [{cid}]" if cid else "")
            entries.append(AnonymousEntry(
                candidate_id=cid, content=content, is_self=True,
            ))
            answer_blocks.append(f"{label}：\n{content}")
        else:
            # CLOSED / 不在本轮 candidate_ids 中的模型天然被排除（§P3）。
            # 若 fallback 成 Candidate-<model_id> 会泄漏真实身份（不变量 A3）。
            if mid not in cand:
                continue
            cid = cand[mid]
            entries.append(AnonymousEntry(
                candidate_id=cid, content=content, is_self=False,
            ))
            # peer 格式沿用 anonymizer：`[Candidate A]\n<content>`（无中文冒号）
            answer_blocks.append(f"[{cid}]\n{content}")
    source_tag = {
        "pinned": "[Pinned]",
        "recent_raw": "[Recent]",
        "compacted": "[Compacted]",
    }.get(item.source, "")

    block = (
        f"## 第 {item.round_id} 轮 {source_tag}\n\n"
        f"用户问题：\n{item.user_question}\n\n"
        + "\n\n".join(answer_blocks)
    )
    return block, entries


def render_history_with_questions(
    items: "Sequence[tuple[int, HistoricalContextItem]]",
    *,
    self_model_id: str,
    candidate_ids: dict[str, str] | None,
    peer_review: bool = True,
) -> tuple[str, list[tuple[int, list[AnonymousEntry]]]]:
    """按 (Pinned → Recent → Compacted) 顺序渲染历史，并返回 AnonymousEntry 列表。

    每个 HistoricalContextItem 必同时含 Q 与 As（§31 不可分）。
    peer_review=False：每个 item 只保留 self_model_id 的回答（互评关闭）。
    """
    blocks: list[str] = []
    all_entries: list[tuple[int, list[AnonymousEntry]]] = []
    for round_id, item in items:
        # S3.8：Compacted 块是 Session 级共享产物，不属于任何单一模型，
        # 因此**不受** peer_review 的 self-only 过滤影响（否则会被误剔除）。
        if item.source == "compacted":
            block, entries = render_historical_item_block(
                item, self_model_id=self_model_id, candidate_ids=candidate_ids,
            )
            if block:
                blocks.append(block)
            if entries:
                all_entries.append((round_id, entries))
            continue
        # 互评关闭：每个 item 只留 self 自己的回答
        if not peer_review:
            item = type(item)(
                round_id=item.round_id,
                user_question=item.user_question,
                answers=[
                    (mid, ans) for mid, ans in item.answers if mid == self_model_id
                ],
                source=item.source,
                token_estimate=item.token_estimate,
            )
        block, entries = render_historical_item_block(
            item, self_model_id=self_model_id, candidate_ids=candidate_ids,
        )
        if block:
            blocks.append(block)
        if entries:
            all_entries.append((round_id, entries))
    return "\n\n".join(blocks), all_entries


def build_round_n_prompt_v2(
    *,
    original_question: str,
    user_instruction: str,
    historical_items: "Sequence[tuple[int, HistoricalContextItem]]",
    self_model_id: str,
    candidate_ids: dict[str, str] | None,
    peer_review: bool,
    participants: Sequence[ModelParticipant],
) -> tuple[str, list[str]]:
    """S3.5 入口：Round 2+ Prompt 由 ContextSelector 输出的 HistoricalContextItem 直接驱动。

    历史单位是完整 Round（User Question + 该 Round 所有成功 Final Answers），§31
    强制不可分；不再有 HISTORY_WINDOW=3 硬裁。

    peer_review=False：每个历史 item 只显示 self 自己的回答（phase1_1 §3.1）。
    """
    history_block, all_entries = render_history_with_questions(
        historical_items,
        self_model_id=self_model_id,
        candidate_ids=candidate_ids,
        peer_review=peer_review,
    )

    warnings: list[str] = []
    for round_id, entries in all_entries:
        for e in entries:
            for token in find_identity_tokens(e.content, participants):
                warnings.append(
                    f"第 {round_id} 轮 {e.candidate_id or '自己'} 的回答正文"
                    f"命中身份词 {token!r}（模型自我披露，非系统泄露）"
                )

    # 硬校验：骨架必须**完全**剥离 answer content（不变量 A3 仅扫描 label 本身）
    skeleton_items = []
    for round_id, item in historical_items:
        if not peer_review:
            skel_answers = [
                (mid, "") for mid, _ in item.answers if mid == self_model_id
            ]
        else:
            skel_answers = [(mid, "") for mid, _ in item.answers]
        skel_item = type(item)(
            round_id=item.round_id,
            user_question="",
            answers=skel_answers,
            source=item.source,
            token_estimate=item.token_estimate,
        )
        skeleton_items.append((round_id, skel_item))
    skeleton, _ = render_history_with_questions(
        skeleton_items, self_model_id=self_model_id, candidate_ids=candidate_ids,
        peer_review=peer_review,
    )

    assert_no_identity_leak(f"{SYSTEM_INSTRUCTION}\n\n{skeleton}", participants)

    parts = [SYSTEM_INSTRUCTION, f"原始问题：\n{original_question}"]
    if history_block:
        parts.append(f"历史上下文（由新到旧，Pin / Recent / Compacted 顺序）：\n\n{history_block}")
    parts.append(f"用户本轮指令：\n{user_instruction}")
    return "\n\n".join(parts), warnings


# ─────────────────────────────────────────────────────────────────────────────
# S3.7：Peer Context 渲染（§12-§17 / Plan §4.8 §7 §8）
# ─────────────────────────────────────────────────────────────────────────────


def render_peer_context_block(
    items: "Sequence[PeerContextItem]",
) -> tuple[str, list[AnonymousEntry]]:
    """把 PeerContextItem 序列 → (渲染块, AnonymousEntry 列表)。

    渲染格式沿用 anonymizer 的 peer 约定（`[Candidate-X]\\n<content>`），
    并显式标注来源类型（§42 可审计）：

        ## 其他参与者当前状态（Peer Context）

        [Candidate-B]（来源：final_answer / round_001）
        <content>

        [Candidate-C]（来源：conversation_summary / summary_003）
        <content>

    合法来源**只有** final_answer 与 conversation_summary（§16/§43）；
    PeerContextPreparator 已在数据层用白名单阻断其他来源。
    """
    if not items:
        return "", []

    blocks: list[str] = []
    entries: list[AnonymousEntry] = []
    for it in items:
        cid = it.candidate_id or ""
        label = f"[{cid}]" if cid else f"[{it.source_model_id}]"
        # 格式沿用 anonymizer 的 peer 约定 `[Candidate-X]\n<content>`（S3.5 已验收）。
        # 来源元信息（§42 可审计）放在内容**之后**的独立行，
        # 避免打断 `[Candidate-X]\n<content>` 的既有匹配。
        blocks.append(
            f"{label}\n{it.content}\n\n"
            f"（来源：{it.source_type} / {it.source_id}，"
            f"有效回答数={it.valid_answer_count}）"
        )
        entries.append(AnonymousEntry(
            candidate_id=cid, content=it.content, is_self=False,
        ))
    header = "## 其他参与者当前状态（Peer Context）"
    return header + "\n\n" + "\n\n".join(blocks), entries


# ─────────────────────────────────────────────────────────────────────────────
# Phase 4.5：MCP Evidence Reference 渲染（§4.5.6 / §4.5.11）
#
# 渲染本身由 evidence_index.render_evidence_block 负责（只输出 reference）；
# 本模块只负责**最后防线**：带真实身份的 reference 一律不渲染（§4.5.11）。
# ─────────────────────────────────────────────────────────────────────────────


def build_round_n_prompt_v3(
    *,
    original_question: str,
    user_instruction: str,
    historical_items: "Sequence[tuple[int, HistoricalContextItem]]",
    peer_context_items: "Sequence[PeerContextItem]",
    self_model_id: str,
    candidate_ids: dict[str, str] | None,
    peer_review: bool,
    participants: Sequence[ModelParticipant],
    # Phase 4.5（§4.5.3 A1）：只可能是**当前模型自己**的历史 Evidence；
    # 默认 () = 旧行为（无 Evidence 块），既有调用方与测试零影响。
    evidence_items: "Sequence[McpEvidenceEntry]" = (),
) -> tuple[str, list[str]]:
    """S3.7 入口：历史（只含自己）+ Peer Context（其他模型）分离渲染。

    与 v2 的关键差异（§30 "最近一轮不再是唯一来源"）：

      v2：历史 item 内**混渲**其他模型的 Final Answer（旧 Answer-only peer history）
      v3：历史 item **只渲染 self**，其他模型信息统一由 PeerContextPreparator
          产出的 PeerContextItem 提供（只可能是 Final Answer 或 Conversation Summary）

    peer_review=False → peer_context_items 应为空，历史也只显示 self（§3.1）。

    Phase 4.5 追加（§4.5.6）：``evidence_items`` 以独立块形式附在 Peer Context
    之后、本轮指令之前，只含 reference；Evidence 与 Peer Context / Summary /
    Compaction 三者互不派生（§4.5.9 / §4.5.12）。

    Phase 4.5（§4.5.11）：``evidence_items`` 进渲染前先过身份判定 ——
    命中真实身份词元的 reference **整条剔除**（不渲染、不降级为告警），
    与选择层用的是同一个判定函数。
    """
    # 历史：v3 始终只渲染 self（peer 信息走独立块）
    history_block, all_entries = render_history_with_questions(
        historical_items,
        self_model_id=self_model_id,
        candidate_ids=candidate_ids,
        peer_review=False,  # ← 关键：历史不再混渲 peer
    )

    # Peer Context：独立块（peer_review=False 时调用方传空）
    peer_block, peer_entries = render_peer_context_block(
        peer_context_items if peer_review else [],
    )

    # Phase 4.5（§4.5.6 / §4.5.11）：Evidence Reference 独立块（只含 reference）。
    # 最后防线：正常路径下选择层（EvidenceIndexService.select_for_context）已把
    # 命中真实身份词元的 reference 整条剔除；这里对"直接调用本函数"的场景再拦
    # 一次，用的是同一个判定函数 —— 保证带真实身份的 reference 永不进入模型
    # 可见内容，而不是降级成一条告警后照样渲染出去。
    evidence_items, leaky_evidence = split_evidence_by_identity(
        tuple(evidence_items or ()), participants,
    )
    evidence_block = render_evidence_block(evidence_items)

    # 软扫描：历史正文 + peer context 正文
    warnings: list[str] = []
    for round_id, entries in all_entries:
        for e in entries:
            for token in find_identity_tokens(e.content, participants):
                warnings.append(
                    f"第 {round_id} 轮 {e.candidate_id or '自己'} 的回答正文"
                    f"命中身份词 {token!r}（模型自我披露，非系统泄露）"
                )
    for e in peer_entries:
        for token in find_identity_tokens(e.content, participants):
            warnings.append(
                f"Peer Context [{e.candidate_id}] 正文"
                f"命中身份词 {token!r}（模型自我披露，非系统泄露）"
            )
    # §4.5.11：被最后防线剔除的 Evidence 记一条告警（正常路径下选择层已剔除，
    # 因此这里通常为空；非空即说明调用方绕过了选择层）。
    for it, hits in leaky_evidence:
        warnings.append(
            f"MCP Evidence（第 {it.round_id} 轮 {it.mcp}/{it.tool}）引用文本"
            f"命中身份词 {hits}（已整条剔除，不进入 Prompt）"
        )

    # 硬校验：骨架（剥离全部正文）不得泄漏身份
    skeleton_items = []
    for round_id, item in historical_items:
        if item.source == "compacted":
            # A3 终检（Phase 4.5 修复）：Compacted 块的**系统生成头部**必须进入
            # 硬校验骨架，不能因 answers 的 mid="__compacted__" 而绕过检查。
            # LLM 生成的正文不进骨架 —— 它只做软扫描（沿用 anonymizer 的范围约定）。
            _body = item.answers[0][1] if item.answers else ""
            skel_answers = [("__compacted__", compacted_system_header(_body))]
        else:
            skel_answers = [(mid, "") for mid, _ in item.answers if mid == self_model_id]
        skel_item = type(item)(
            round_id=item.round_id,
            user_question="",
            answers=skel_answers,
            source=item.source,
            token_estimate=item.token_estimate,
        )
        skeleton_items.append((round_id, skel_item))
    skeleton, _ = render_history_with_questions(
        skeleton_items, self_model_id=self_model_id, candidate_ids=candidate_ids,
        peer_review=False,
    )
    # peer 骨架：只保留标签（content 清空），确保 candidate_id 本身不泄漏真实 id
    peer_skeleton_entries = [
        replace(e, content="") for e in peer_entries
    ]
    peer_skeleton_blocks = []
    for it, e in zip(peer_context_items if peer_review else [], peer_skeleton_entries):
        cid = e.candidate_id or ""
        label = f"[{cid}]" if cid else f"[{it.source_model_id}]"
        peer_skeleton_blocks.append(
            f"{label}（来源：{it.source_type} / {it.source_id}）"
        )
    if peer_skeleton_blocks:
        skeleton = (
            skeleton
            + "\n\n## 其他参与者当前状态（Peer Context）\n\n"
            + "\n\n".join(peer_skeleton_blocks)
        )

    # §4.5.11：Evidence 块的**系统生成骨架**（标题 / 说明 / 定位行）进硬校验；
    # reference 正文已被上面的身份判定整条剔除（不是软扫描放过）。
    if evidence_items:
        skeleton = (
            skeleton + "\n\n"
            + render_evidence_block(tuple(evidence_items), skeleton=True)
        )

    assert_no_identity_leak(f"{SYSTEM_INSTRUCTION}\n\n{skeleton}", participants)

    parts = [SYSTEM_INSTRUCTION, f"原始问题：\n{original_question}"]
    if history_block:
        parts.append(f"历史上下文（由新到旧，Pin / Recent / Compacted 顺序）：\n\n{history_block}")
    if peer_block:
        parts.append(peer_block)
    # §63 典型结构：必要 MCP Evidence 位于 Compacted History 之后
    if evidence_block:
        parts.append(evidence_block)
    parts.append(f"用户本轮指令：\n{user_instruction}")
    return "\n\n".join(parts), warnings
