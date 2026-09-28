"""Mock Model Provider（phase1_mock.md §5/§6）。

Phase 1 不接入真实 LLM。三个 Mock 模型各自返回不同的固定语料，
以便验证前端并行显示，以及 Round 2 是否正确注入了其他参与者的上一轮内容。

**语料必须内容中立**：不得出现任何模型身份词（不变量 A3）。
即使 Prompt 模板本身是干净的，只要 Round 1 的回答正文里写了自我介绍，
Round 2 把它注入给其他模型时身份就泄露了。
`core/anonymizer.assert_no_identity_leak` 在完整 prompt 上运行，
会兜住这类泄露；test_phase1.py 另有一条独立断言直接检查语料。
"""

from __future__ import annotations

import asyncio
import os
import random
from collections.abc import AsyncIterator

# 延迟缩放，仅供测试加速。生产/演示时保持默认 1.0。
DELAY_SCALE_ENV = "CROSSLLM_MOCK_DELAY_SCALE"

# 每个模型不同的延迟区间（秒），用于验证前端是否真正并行处理
# phase1_mock.md §6：50ms ~ 200ms，不同模型使用不同延迟
_DELAY_RANGES: dict[str, tuple[float, float]] = {
    "qwen": (0.050, 0.090),
    "deepseek": (0.090, 0.140),
    "gpt": (0.140, 0.200),
}
_DEFAULT_DELAY_RANGE = (0.080, 0.160)

# 每次 yield 的字符数
_CHUNK_SIZE = 10

# Round 1 语料：独立分析，不引用任何"其他参与者"
_ROUND_1: dict[str, str] = {
    "qwen": """先给出结论：这个方案整体可行，但在动手之前需要先明确边界条件。

我的分析：

1. 核心链路较短，主要成本集中在检索阶段。建议先量化这一阶段的耗时占比。
2. 缓存策略应当按数据变更频率分层，而不是给所有数据套同一个过期时间。
3. 重试必须设置次数上限，否则局部故障会被放大成雪崩。

主要风险在于：如果上游依赖的响应延迟不稳定，整体长尾会被显著拉高。

我的建议是先做一次小规模压测，拿到真实数据之后再决定是否扩容。""",
    "deepseek": """我倾向于先从另一个角度切入：先确认问题是否真实存在。

具体判断依据：

1. 当前描述缺少量化指标。没有基线数据，任何优化都无法验证效果。
2. 真正的瓶颈可能不在计算，而在数据搬运。建议先做一次逐阶段计时。
3. 引入新组件会增加运维复杂度，这部分成本需要被明确计入评估。

我的保留意见是：在拿到证据之前，不建议直接上更重的方案。

如果必须现在给出建议，我会选择先补齐观测，再谈优化。""",
    "gpt": """我同意需要先量化，这里补充几点容易被忽略的地方。

1. 除了平均值，更应该关注长尾。恶劣情况下的延迟往往是用户最先感知到的。
2. 分层缓存的失效顺序需要仔细设计，否则会出现级联穿透。
3. 灰度发布必须保留快速回滚能力，这是压测之外最重要的一道保险。

我额外关注的是容量模型：单节点的连接数上限可能比算力更早成为约束。

因此我建议把容量规划与缓存策略放在一起评估，而不是分开决策。""",
}

# Round 2+ 语料：体现"看过其他参与者之后修正自己的方案"
_ROUND_N: dict[str, str] = {
    "qwen": """综合其他参与者的意见之后，我调整如下。

1. 我原本认为应该先压测，现在同意先补观测更合理 —— 没有基线就无法判断优化是否生效。
2. 关于长尾的那一点我接受，把它并入我的方案：除了平均耗时，需要单独盯住恶劣情况。
3. 我仍然坚持分层缓存，但接受"失效顺序需要设计"这条补充，否则会出现级联穿透。

仍然存在的分歧：容量是否现在就纳入评估。我认为可以推迟到拿到数据之后。""",
    "deepseek": """看完其他参与者的方案，我的修正如下。

1. 我坚持先补观测，这一点上我们的看法是一致的。
2. 我接受把长尾指标单独列出，这比我原来的平均耗时口径更贴近用户感受。
3. 我不同意把容量规划推迟 —— 连接数上限如果早期不考虑，后期改造代价更大。

尚未解决的疑问：缓存分层的粒度按什么维度切，目前几个方案都没有给出具体标准。""",
    "gpt": """参考其他参与者的意见之后，我的更新如下。

1. 我保留对容量模型的主张：连接数上限应当尽早纳入评估。
2. 我接受"先补观测再优化"的顺序，这比我原来直接建议压测更稳妥。
3. 关于缓存，我认同分层，并补充一条：每一层都需要独立的降级开关。

我认为目前的主要分歧已经收敛到"容量什么时候评估"这一点上。""",
}


def content_for(model_id: str, round_id: int) -> str:
    """取该模型该轮的完整 Mock 语料。"""
    table = _ROUND_1 if round_id <= 1 else _ROUND_N
    try:
        return table[model_id]
    except KeyError:
        raise KeyError(f"Mock 语料未定义: model={model_id!r} round={round_id}") from None


class MockProvider:
    """单个 Mock 模型。

    Phase 2 替换为真实 Provider 时，上层只依赖 `generate()` 这个 async generator 接口
    （architecture_v0.1.md §17：保持上层调用接口不变）。
    """

    def __init__(self, model_id: str, *, fail: bool = False, delay_scale: float | None = None):
        self.model_id = model_id
        self.fail = fail
        if delay_scale is None:
            delay_scale = float(os.environ.get(DELAY_SCALE_ENV, "1.0"))
        self.delay_scale = max(0.0, delay_scale)
        self._lo, self._hi = _DELAY_RANGES.get(model_id, _DEFAULT_DELAY_RANGE)
        # 与 RealProvider 同名属性，调用方无需区分（phase1_1 §6.2）。
        # Mock 没有真实 token 数据，恒为 None。
        self.usage: dict | None = None

    async def generate(
        self,
        *,
        round_id: int,
        prompt: str,
        tools: list[dict] | None = None,
        # S3.11：Mock 无真实输出预算概念，接受并忽略（保持 Protocol 兼容）。
        max_output_tokens: int | None = None,
    ) -> AsyncIterator[str]:
        """逐 chunk 产出回答。

        prompt 参数在 Phase 1 不使用（Mock 返回固定语料），保留它是为了让
        调用方接口与 Phase 2 的真实 Provider 保持一致。

        max_output_tokens（S3.11）：Mock 不施加输出上限，仅接受参数以兼容
        `LLMProvider` Protocol。
        tools 参数在 Mock 中不起作用，仅保持接口一致。
        """
        text = content_for(self.model_id, round_id)
        chunks = [text[i : i + _CHUNK_SIZE] for i in range(0, len(text), _CHUNK_SIZE)]
        # 失败注入：产出约四成内容后抛错，模拟"部分输出后失败"的真实场景
        fail_at = len(chunks) * 2 // 5 if self.fail else None

        for index, chunk in enumerate(chunks):
            if fail_at is not None and index == fail_at:
                raise RuntimeError(f"Mock provider {self.model_id!r} 注入失败")
            # 即便 delay_scale=0 也要 await —— asyncio.sleep(0) 是一次真正的让出，
            # 多个模型借此交错推进。若跳过 await，生成器会一路跑到结束，
            # 三个模型就退化成串行，并发流式的能力也就无法被验证。
            await asyncio.sleep(random.uniform(self._lo, self._hi) * self.delay_scale)
            yield chunk
