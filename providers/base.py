"""统一 Provider 接口（phase2_real_llm.md §4/§7）。

RoundManager 只依赖这一个接口，不关心底层是：

    OpenAI Responses API / Chat Completions / Anthropic / Qwen / DeepSeek / ...

这些全部属于 Provider 实现细节（phase2 §7）。

接口形状与 Phase 1 的 MockProvider 完全一致，因此 Phase 2 替换 Provider
时 core/round_manager.py 不需要改编排逻辑 —— 这正是本阶段要验证的命题。
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Protocol, runtime_checkable


class ProviderError(RuntimeError):
    """Provider 层归一化的错误。

    所有厂商特有的失败（HTTP 非 2xx、API 错误、无效 Key、超时、流中断、
    响应格式异常）都应转成这个类型再往上抛（phase2 §15）。

    构造时**不得**携带 API Key、Authorization Header 或任何请求头 ——
    错误消息会被写进日志和 error 事件（phase2 §8/§20）。
    """


@runtime_checkable
class LLMProvider(Protocol):
    """最小 Provider 接口。

    generate 是 async generator：调用它返回 AsyncIterator，不返回 coroutine。
    因此这里声明为普通 def。
    """

    def generate(
        self,
        *,
        round_id: int,
        prompt: str,
        tools: list[dict] | None = None,
        # S3.11（MD §22/§23）：CrossLLM 预算层的有效输出上限
        # = ContextBudget.reserved_output_tokens = min(model_max_output, request_max)
        # None = 预算层给不出（degraded）→ Provider 不得自行发明一个固定值。
        max_output_tokens: int | None = None,
    ) -> AsyncIterator[str]:
        """逐 chunk 产出回答文本。

        tools: OpenAI-compatible function 定义列表。Provider 自行决定是否/如何
        调用；RoundManager 只把最终文本 chunk 交给调用方。

        max_output_tokens: CrossLLM 预算层给出的**有效输出上限**（S3.11）。
        Provider 必须**直接采用**该值作为生成上限，而不应自行重算另一套
        预算逻辑（MD §22："禁止固定 16000"；S3.11 验收项 5）。

        该值**只**来自预算层，不是模型能力，也不是 CrossLLM 的固定配置：
          模型能力上限（model_max_output）由 core/model_capability.py 从
          Provider 元数据取得；本参数是 min(模型能力, 本次请求显式上限)。

        None 表示预算层也给不出该值（能力与请求上限都未知）。此时 Provider
        **不得**伪造一个固定值：唯一的额外约束是 §24 硬约束
        ``input + output <= context_limit``（在 context_limit 已知时求交，
        否则不下发 max_tokens）。
        """
        ...
