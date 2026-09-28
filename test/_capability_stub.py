"""测试用：确定性的模型能力来源（注入缝）。

生产链路的能力来自 Provider 元数据（联网 + 24h 磁盘缓存）。离线 / CI 里联网
既慢又不可复现，因此通过 ``RoundManager(capability_resolver=...)`` 注入本 stub。

关键：它走的是与生产 **完全相同** 的合并链路 —— resolve() 的产物仍会经过
``core.model_capability.apply_context_limit_fallback()``，等价于"Provider
查询成功"，因此日志里 ``context_limit_source == "openrouter_metadata"``，
``context_limit_fetch_status == "ok"``。

数值刻意沿用改动前的内建默认（context_limit=8192 / model_max_output=2048），
这样依赖旧预算数字的既有用例无需改动断言。
"""

from __future__ import annotations

from core.model_capability import ProviderModelMetadata, TokenEstimate


def _te(value: int | None) -> TokenEstimate:
    if value is None:
        return TokenEstimate(None, "provider_unavailable", "unknown")
    return TokenEstimate(int(value), "openrouter_metadata", "exact")


class StubCapabilityResolver:
    """按 slug 返回固定能力（不联网、不写磁盘缓存）。"""

    def __init__(
        self,
        *,
        context_limit: int | None = 8192,
        model_max_output: int | None = 2048,
        fetch_status: str = "ok",
    ) -> None:
        self.context_limit = context_limit
        self.model_max_output = model_max_output
        self.fetch_status = fetch_status
        self.calls: list[str] = []

    async def resolve(self, model_slug: str) -> ProviderModelMetadata:
        self.calls.append(model_slug)
        return ProviderModelMetadata(
            model_slug=model_slug,
            context_limit=_te(self.context_limit),
            model_max_output=_te(self.model_max_output),
            fetched_at="2026-09-25T00:00:00Z",
            fetch_status=self.fetch_status,  # type: ignore[arg-type]
            raw=None,
        )


class FailingCapabilityResolver:
    """Provider 查询失败（用于验证"失败 → YAML fallback"）。"""

    def __init__(self) -> None:
        self.calls: list[str] = []

    async def resolve(self, model_slug: str) -> ProviderModelMetadata:
        self.calls.append(model_slug)
        return ProviderModelMetadata(
            model_slug=model_slug,
            context_limit=TokenEstimate(None, "provider_unavailable", "unknown"),
            model_max_output=TokenEstimate(None, "provider_unavailable", "unknown"),
            fetched_at="2026-09-25T00:00:00Z",
            fetch_status="failed",
            raw=None,
        )
