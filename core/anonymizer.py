"""匿名交叉注入的唯一执行点。

架构不变量（architecture_v0.1.md P1-P8、phase1_mock.md §14）：

  A1 系统内部（内存 / state.json / 文件命名 / UI）一律保留真实 model_id
  A2 任何内容进入某个模型的 context 之前，来源模型标识必须已转换为 candidate_id
  A3 模型收到的 context 中不得出现真实模型名称 / model_id / provider model 名
  A4 candidate_id <-> model_id 的映射只存在于系统内部
     —— 不进模型 context、不写入 round_NNN/ 下任何文件、不通过 API 返回

本模块是 A2 / A3 的唯一实现处。任何其它模块都不得自行拼装模型可见的跨模型上下文，
否则不变量会被绕过。

关于 A3 的校验范围（重要）：
  校验只覆盖「系统生成的部分」（系统公共指令 + 渲染后的匿名上下文块），
  不覆盖用户自己输入的问题与指令。
  因为用户完全可能提出「比较 Qwen 和 GPT 的差异」这类问题 —— 那是用户自己写下的
  内容，不属于系统注入的身份信息，拦截它会让系统在这类核心场景上直接报错。
  用户写下模型名也不会破坏匿名性：模型无法据此把 Candidate A 映射到某个真实模型。
"""

from __future__ import annotations

import string
from collections.abc import Iterable, Sequence
from dataclasses import dataclass

from .state import ModelParticipant

CANDIDATE_PREFIX = "Candidate "

# 无上一轮可用回答时的占位文本（不泄露任何身份）
NO_PREVIOUS_ANSWER = "（该参与者在上一轮没有可用回答）"


class IdentityLeakError(RuntimeError):
    """模型 context 中出现真实身份信息，违反不变量 A3。"""


def _label(index: int) -> str:
    """0 -> A, 1 -> B, ... 超出 26 个参与者直接报错，避免静默出错。"""
    if index >= len(string.ascii_uppercase):
        raise ValueError(f"参与者数量超出单字母标签上限（{len(string.ascii_uppercase)}）")
    return string.ascii_uppercase[index]


def assign_candidate_ids(participants: Iterable[str]) -> dict[str, str]:
    """为当轮参与者分配 candidate_id。

    按 model_id 字典序确定性分配，三个设计要点：

    1. **轮内全局共享**：返回的是「一轮一份」的映射，本轮所有模型的 prompt 都必须
       复用同一份。若每个模型各生成一套标签，会出现
       「Qwen 说 GPT 是 Candidate B、而 GPT 自称 Candidate A」的错位，
       模型之间对同一对象的称呼不一致，交叉讨论就无法对齐。
    2. **每轮重新紧凑分配**：Round 3 只剩 2 人时分配 A/B，而不是保留 A/C。
       标签空洞（有 A 有 C 没有 B）会泄露「曾经有第三个参与者」。
    3. **确定性**：保证测试可复现；参与者不变时标签跨轮稳定。

    返回值仅供系统内部使用（A4）。
    """
    ordered = sorted(set(participants))
    return {model_id: f"{CANDIDATE_PREFIX}{_label(i)}" for i, model_id in enumerate(ordered)}


@dataclass(frozen=True)
class AnonymousEntry:
    """一条可直接注入模型的匿名上下文条目。

    只携带 candidate_id 与正文，不含 model_id / display_name / provider model 名。
    """

    candidate_id: str
    content: str
    is_self: bool


def build_anonymous_context(
    *,
    self_model_id: str,
    candidate_ids: dict[str, str],
    previous_answers: dict[str, str],
) -> list[AnonymousEntry]:
    """把上一轮回答转换为匿名条目。

    CLOSED 模型不在 candidate_ids 中，因此天然被排除（architecture_v0.1.md P3）。
    previous_answers 中缺失的模型（例如上一轮 error、无可用回答）以占位文本渲染。

    条目顺序按 candidate_id 字典序，保证同一轮内所有模型看到的顺序一致。
    """
    if self_model_id not in candidate_ids:
        raise KeyError(f"自身 model_id {self_model_id!r} 不在本轮 candidate_ids 中")

    entries: list[AnonymousEntry] = []
    for model_id, candidate_id in sorted(candidate_ids.items(), key=lambda kv: kv[1]):
        entries.append(
            AnonymousEntry(
                candidate_id=candidate_id,
                content=previous_answers.get(model_id) or NO_PREVIOUS_ANSWER,
                is_self=(model_id == self_model_id),
            )
        )
    return entries


def assert_no_identity_leak(text: str, participants: Sequence[ModelParticipant]) -> None:
    """校验文本中不含任何真实身份词元（不变量 A3，大小写不敏感）。

    Phase 2 起，这个硬校验**只用于 CrossLLM 自己生成的系统身份信息**
    （系统公共指令 + Candidate 标注骨架）。

    对真实 LLM 的回答正文请改用 find_identity_tokens 做软扫描 ——
    否则模型一句自我介绍就会让整轮失败。
    """
    hits = find_identity_tokens(text, participants)
    if hits:
        raise IdentityLeakError(f"模型 context 中出现真实身份信息 {hits}（违反不变量 A3）")


def find_identity_tokens(text: str, participants: Sequence[ModelParticipant]) -> list[str]:
    """返回文本中命中的真实身份词元（大小写不敏感）。**不抛异常**。

    用于软扫描：真实 LLM 的回答正文可能主动冒出身份词（"我是…"），
    那是模型行为，不是系统 bug —— 只记录告警，不阻断整轮。

    phase2_real_llm.md §28 问题 3：先记录问题，不自动引入复杂的身份清洗系统。
    """
    lowered = text.lower()
    hits: list[str] = []
    for p in participants:
        for token in p.identity_tokens():
            if token and token.lower() in lowered and token not in hits:
                hits.append(token)
    return hits
