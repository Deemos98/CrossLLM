"""S3.2：ValidRound + ValidRoundPoolBuilder（PHASE4_REFACTOR_PLAN §4.2）

严格范围（不前置 S3.3+）：
  - ValidRound 数据结构（含两条不变量）
  - ValidRoundPoolBuilder：从 SessionState + Storage 构造 Valid Round 池
  - 有效回答判定：status == "complete" && 文件存在 → successful_answers
  - 部分失败：保留 failed_models，但 successful_answers 只含成功者
  - 全部失败：Invalid Round，**不进** Pool（Raw Archive 不删）

明确不在本模块范围：
  - Pin / is_pinned 实际接线                   —— S3.4
  - k / ContextBudget / ContextSelector          —— S3.3+
  - Summary / Compaction / Peer Context         —— S3.6+
  - Project Memory                              —— Phase 4.6
  - PromptBuilder / Provider                    —— S3.5+

开工三检（2026-09-03 S3.2）：
  1. ValidRound = User Question + 成功 Final Answers（§5/§31）
     → 强制不变量：successful_answers 与 failed_models 无交集
  2. is_valid ⟺ len(successful_answers) > 0
  3. 失败模型的 Error 文本不进 successful_answers，
     但 Raw Archive（round_*.md / state.json）不动
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from core.model_capability import TokenEstimate

if TYPE_CHECKING:
    from core.state import SessionState
    from core.storage import Storage


# ─────────────────────────────────────────────────────────────────────────────
# §4.2 ValidRound
# ─────────────────────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class ValidRound:
    """Round = User Question + 成功 Final Answers（§5/§31）。

    字段来源：
      - round_id            ← state.rounds[].round_id
      - user_question       ← state.rounds[].user_instruction
      - successful_answers  ← storage.load_responses() ∩ status=complete
      - failed_models       ← participants − successful
      - token_estimate      ← TokenEstimate(len(Q + As) // 3, crossllm_estimator, estimated)
      - is_pinned           ← 默认 False；S3.4 PinStore 会从 pins.jsonl 重放后置 True

    不变量（__post_init__ 强制）：
      I1. is_valid ⟺ len(successful_answers) > 0       （§2.1）
      I2. set(failed_models) ∩ set(successful_answers) == ∅  （§4 部分失败语义）
    """

    round_id: int
    user_question: str
    successful_answers: dict[str, str]
    failed_models: list[str]
    is_valid: bool
    token_estimate: TokenEstimate

    # S3.4 才实际接线，S3.2 保留字段以保持数据形状稳定
    is_pinned: bool = False

    def __post_init__(self) -> None:
        # I1
        if self.is_valid != (len(self.successful_answers) > 0):
            raise ValueError(
                f"ValidRound 不变量 I1 违反：is_valid={self.is_valid} "
                f"但 len(successful_answers)={len(self.successful_answers)}；"
                "is_valid 必须 ⟺ 至少 1 个成功 Final Answer（§2.1）"
            )
        # I2
        overlap = set(self.failed_models) & set(self.successful_answers.keys())
        if overlap:
            raise ValueError(
                f"ValidRound 不变量 I2 违反：以下模型同时出现在 "
                f"failed_models 和 successful_answers：{sorted(overlap)}；"
                "失败模型的 Error 不得进入正常历史（§4）"
            )


# ─────────────────────────────────────────────────────────────────────────────
# ValidRoundPoolBuilder
# ─────────────────────────────────────────────────────────────────────────────


def _ceil_div(n: int, d: int) -> int:
    """向上取整除（与项目已有 estimate_tokens 风格一致）。"""
    return -(-n // d)


def _estimate_text_tokens(text: str) -> int:
    """与现有 estimate_tokens 一致的 len//3（向上）。空文本 = 0。"""
    if not text:
        return 0
    return _ceil_div(len(text), 3)


class ValidRoundPoolBuilder:
    """从 SessionState + Storage 构造 ValidRound 池（§4.2 + §29）。

    关键行为：
      - 遍历 session.rounds（**不依赖** HISTORY_WINDOW；S3.2 不引入硬窗口）
      - 仅当 round 至少 1 个模型 status=complete 且文件存在 → ValidRound
      - 全失败 round **不进** Pool（§2.1：Invalid Round 不进 Context）
      - 失败模型不进 successful_answers（§4）
      - **不动** Raw Archive：storage.save_response 禁止覆盖；本类只读
    """

    def __init__(self, storage: "Storage") -> None:
        self._storage = storage

    def build(self, session: "SessionState") -> list[ValidRound]:
        """返回**已过滤** Valid Round 池，按 round_id 升序。"""
        pool: list[ValidRound] = []

        for rs in session.rounds:
            responses = self._storage.load_responses(session.session_id, rs.round_id)
            successful, failed = self._partition(rs.participants, rs.status, responses)
            if not successful:
                # Invalid Round：Raw Archive 不动，仅不进 Pool（§2.1 / §4）
                continue

            # token 估算：Q + 全部成功 A（与失败无关）
            token_total = _estimate_text_tokens(rs.user_instruction)
            for ans in successful.values():
                token_total += _estimate_text_tokens(ans)

            pool.append(
                ValidRound(
                    round_id=rs.round_id,
                    user_question=rs.user_instruction,
                    successful_answers=successful,
                    failed_models=failed,
                    is_valid=True,
                    token_estimate=TokenEstimate(
                        value=token_total,
                        source="crossllm_estimator",
                        accuracy_mode="estimated",
                    ),
                    # is_pinned 留默认 False，S3.4 接 PinStore 后再覆盖
                )
            )

        return pool

    @staticmethod
    def _partition(
        participants: list[str],
        status: dict[str, str],
        responses: dict[str, str],
    ) -> tuple[dict[str, str], list[str]]:
        """把 (participants × responses) 按 status 分成 (successful, failed)。

        规则：
          successful ⟺ status[m] == "complete" AND m ∈ responses
          否则（status 缺失 / status != complete / 文件缺失）→ failed

        注意：失败模型**不读** responses[m] 内容。即便 storage 中残留了
        任何 .md 文件（如 debug 输出），也不进 successful_answers。
        """
        successful: dict[str, str] = {}
        failed: list[str] = []
        for mid in participants:
            if status.get(mid) == "complete" and mid in responses:
                successful[mid] = responses[mid]
            else:
                failed.append(mid)
        return successful, failed