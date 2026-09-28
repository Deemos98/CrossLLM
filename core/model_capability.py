"""Provider Model Capability（phase4_memory.md §21–§26 / PHASE4_REFACTOR_PLAN §4.1 / §4.6 / §9）

本模块是 **模型能力** 的唯一来源。能力 ≠ 本次请求配置，两者严格分离：

    context_limit      ：模型上下文窗口（input + output 合计上限）
    model_max_output   ：模型自身允许的最大输出（Provider 元数据）
    ─────────────────────────────────────────────
    request_max_output ：**某一次请求**的输出上限（由 RoundState / API 给出）
    reserved_output    ：本次预算实际预留 = min(model_max_output, request_max_output)

§21–§26 / 用户任务书 §一的硬要求：
  1. context_limit 的取值优先级 **必须** 是
         Provider / 模型元数据  →  models.yaml.context_limit（fallback）  →  unknown
     查询成功必须覆盖 YAML；只有查询失败才允许用 YAML fallback；
     **不允许把静态 YAML 值伪装成 Provider 能力**（source 字段必须诚实标注）。
  2. model_max_output **不得** 由 models.yaml 提供，也不得用
     ``context_limit * 0.9`` / 80% / 固定 8192 / 固定 16000 之类的经验值代替。
     取不到就是 unknown（source="provider_unavailable"）。
  3. 每个数值都必须带 source / exactness / fetch_status，取不到就写 unknown，
     不得伪装成精确值（§23 / §25）。

对外只暴露三个东西：
  - ``TokenEstimate`` / ``ProviderModelMetadata``：带来源与精度的数值容器
  - ``ModelCapabilityResolver``：真实 Provider 元数据查询器（GET {base}/models + 两层缓存）
  - ``apply_context_limit_fallback()``：把 Provider 结果与 YAML fallback 合成一个诚实的元数据

注入缝：``CapabilityResolver`` 是 Protocol，RoundManager 接受任何实现该协议的对象，
离线环境 / 测试可以注入确定性的元数据，无需联网。

明确不在本模块范围：
  - ContextBudget / compute_budget         —— S3.3
  - reserved_output_tokens 计算             —— S3.3
  - request_max_output 解析                 —— S3.10 / RoundState
  - Pin / Valid Round / ContextSelector    —— S3.2+
  - Summary / Compaction                   —— S3.6+ / S3.8
"""

from __future__ import annotations

import asyncio
import json
import time
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any, Literal, Protocol, runtime_checkable

import httpx


# ─────────────────────────────────────────────────────────────────────────────
# §4.1 TokenEstimate
# ─────────────────────────────────────────────────────────────────────────────

TokenSource = Literal[
    "provider_tokenizer",     # Provider/SDK 真实 tokenizer（本模块不生成）
    "openrouter_metadata",    # OpenRouter /api/v1/models 元数据（§22 / §25）
    "crossllm_estimator",     # CrossLLM len//CHARS_PER_TOKEN（未来 S3.3+）
    "config_static",          # models.yaml 等静态配置（**只允许作为 context_limit fallback**）
    "provider_unavailable",   # 全部取不到（终态）
]

AccuracyMode = Literal["exact", "estimated", "unknown"]


@dataclass(frozen=True)
class TokenEstimate:
    """单个 token 数值的来源/精度三元组（§58）。

    不变量（__post_init__ 强制）：
      ① value is None ⟺ accuracy_mode == "unknown"
         → 不知道就是不知道，绝不填 0 或猜测值伪装精确。
      ② accuracy_mode == "unknown" ⟹ source == "provider_unavailable"
         → unknown 的来源必须是"拿不到"。
      ③ accuracy_mode == "exact" ⟹ source ∈ {"provider_tokenizer", "openrouter_metadata"}
         → "exact" 必须来自可信源（Provider 报告或真实 tokenizer），
           不允许用估算值标 exact。
    """

    value: int | None
    source: TokenSource
    accuracy_mode: AccuracyMode

    def __post_init__(self) -> None:
        # ① value 与 accuracy_mode 联动
        if (self.value is None) != (self.accuracy_mode == "unknown"):
            raise ValueError(
                f"TokenEstimate 不变量违反：value={self.value!r} "
                f"accuracy={self.accuracy_mode!r}；"
                "value is None 必须 ⟺ accuracy_mode == 'unknown'"
            )
        # ② unknown 的来源必须是 provider_unavailable
        if self.accuracy_mode == "unknown" and self.source != "provider_unavailable":
            raise ValueError(
                f"TokenEstimate 不变量违反：accuracy=unknown 时 "
                f"source 必须为 provider_unavailable，得到 {self.source!r}"
            )
        # ③ exact 的来源必须是可信源
        if self.accuracy_mode == "exact" and self.source not in {
            "provider_tokenizer", "openrouter_metadata",
        }:
            raise ValueError(
                f"TokenEstimate 不变量违反：accuracy=exact 时 "
                f"source 必须 ∈ {{provider_tokenizer, openrouter_metadata}}，"
                f"得到 {self.source!r}"
            )

    def to_log(self) -> dict[str, Any]:
        """§58 三元组落日志：value + source + accuracy_mode 同行。"""
        return {
            "value": self.value,
            "token_source": self.source,
            "token_exactness": self.accuracy_mode,
        }


UNKNOWN_TOKEN = TokenEstimate(None, "provider_unavailable", "unknown")
"""unknown 单例语义（frozen dataclass，可安全共享）。"""


# ─────────────────────────────────────────────────────────────────────────────
# §4.6 ProviderModelMetadata
# ─────────────────────────────────────────────────────────────────────────────

FetchStatus = Literal["ok", "cached", "failed", "not_fetched"]


@dataclass(frozen=True)
class ProviderModelMetadata:
    """某个模型的 **能力** 封装（§4.6 / §22 / §9）。

    字段语义（严格区分"能力"与"本次请求"）：
      context_limit     ：模型上下文窗口。来源见 ``context_limit.source``：
                          openrouter_metadata（Provider 查询成功，覆盖 YAML）
                          config_static（Provider 查询失败 → YAML fallback）
                          provider_unavailable（两者都没有 → unknown）
      model_max_output  ：模型自身允许的最大输出。**只可能来自 Provider 元数据**，
                          取不到就是 unknown（禁止 YAML / 经验值替代）。
      fetch_status      ：ok / cached / failed / not_fetched —— 能力查询本身的状态。
    """

    model_slug: str
    context_limit: TokenEstimate
    model_max_output: TokenEstimate
    fetched_at: str                       # ISO8601 UTC
    fetch_status: FetchStatus
    raw: dict[str, Any] | None = None     # Provider 原始记录，便于排查


@runtime_checkable
class CapabilityResolver(Protocol):
    """能力来源协议（注入缝）。

    RoundManager 只依赖这一个协议：默认走 ``get_default_resolver()``（真实
    Provider 元数据）；离线环境或测试可以注入确定性实现，从而在不联网的前提下
    验证 §21–§26 的全部语义。
    """

    async def resolve(self, model_slug: str) -> ProviderModelMetadata:
        ...


# ─────────────────────────────────────────────────────────────────────────────
# §一 能力来源优先级：Provider 元数据 → YAML fallback → unknown
# ─────────────────────────────────────────────────────────────────────────────


def fallback_only_metadata(
    model_slug: str,
    fallback_context_limit: int | None,
    *,
    fetch_status: FetchStatus = "not_fetched",
) -> ProviderModelMetadata:
    """不查 Provider（或没有可查的 Provider）时的元数据。

    context_limit 取自 YAML fallback（标 ``config_static / estimated``，**不伪装**）；
    model_max_output 一律 unknown —— YAML 不是模型能力来源（§二 / §三）。
    """
    if fallback_context_limit is not None:
        ctx = TokenEstimate(int(fallback_context_limit), "config_static", "estimated")
    else:
        ctx = UNKNOWN_TOKEN
    return ProviderModelMetadata(
        model_slug=model_slug,
        context_limit=ctx,
        model_max_output=UNKNOWN_TOKEN,
        fetched_at=_iso_now(),
        fetch_status=fetch_status,
        raw=None,
    )


def apply_context_limit_fallback(
    provider_meta: ProviderModelMetadata,
    fallback_context_limit: int | None,
) -> ProviderModelMetadata:
    """把 Provider 查询结果与 YAML fallback 合成 **诚实** 的元数据（§一）。

    规则（逐字对应任务书 §一）：
      - Provider 查到了 context_limit → **直接用，覆盖 YAML**，source 保持
        ``openrouter_metadata``（绝不改写成 config_static 来"降级"）。
      - Provider 没查到（查询失败 / 字段缺失）且 YAML 有值 → 用 YAML，
        source 标 ``config_static``（fallback 就是 fallback，不伪装成能力）。
      - 两者都没有 → unknown。
      - model_max_output **永不** fallback。
    """
    if provider_meta.context_limit.value is not None:
        return provider_meta
    if fallback_context_limit is None:
        return provider_meta
    return replace(
        provider_meta,
        context_limit=TokenEstimate(
            int(fallback_context_limit), "config_static", "estimated"
        ),
    )


# ─────────────────────────────────────────────────────────────────────────────
# §9 ModelCapabilityResolver
# ─────────────────────────────────────────────────────────────────────────────

_DEFAULT_BASE_URL = "https://openrouter.ai/api/v1"
_DEFAULT_TTL_SECONDS = 86400               # §9.5 24h

# 磁盘缓存位置：项目根（与 .gitignore 的忽略规则对应）。
# 只在真实查询成功时写出，因此它的存在本身就是"resolver 真的被调用过"的证据。
DEFAULT_DISK_CACHE_PATH = Path(__file__).resolve().parent.parent / ".model_metadata_cache.json"


class ModelCapabilityResolver:
    """Provider /api/v1/models 元数据解析器（§9 / §4.6）。

    查找顺序（每 model_slug）：
       in-memory cache
           ↓ miss
       disk cache（fresh, _fetched_at_unix + ttl）
           ↓ miss / stale
       network（Provider 公开端点，GET /api/v1/models 无需 auth）
           ↓ network OK → 写回两层 cache → return
           ↓ network fail
                有 disk cache（即使 stale）→ 用之，fetch_status="cached"
                无 disk cache → 全部 unknown，fetch_status="failed"

    注意：
      - 本类只解析 **模型能力**，**不**解析 request_max_output（§3.3 范围）
      - 本类**不**计算 reserved_output_tokens（§3.3 范围）
      - 并发：同一 Resolver 上的并发 resolve 用 asyncio.Lock 串行化内存层；
        网络请求层并发安全（OpenRouter 是无状态的 GET）。
    """

    def __init__(
        self,
        *,
        openrouter_base_url: str = _DEFAULT_BASE_URL,
        disk_cache_path: Path | None = None,
        ttl_seconds: int = _DEFAULT_TTL_SECONDS,
        http_timeout: float = 30.0,
    ) -> None:
        self._base_url = openrouter_base_url.rstrip("/")
        self._disk_cache_path = disk_cache_path
        self._ttl = ttl_seconds
        self._http_timeout = http_timeout
        self._memory_cache: dict[str, ProviderModelMetadata] = {}
        self._lock = asyncio.Lock()

    # ── 公开 API ──

    async def resolve(self, model_slug: str) -> ProviderModelMetadata:
        """获取某个模型的能力元数据。

        返回值永不为 None；最坏情况返回 fetch_status="failed" 的 unknown 元数据。
        本方法**幂等**：同一 slug 多次调用在缓存有效期内只触发一次网络拉取。
        """
        # 1. in-memory
        cached = self._memory_cache.get(model_slug)
        if cached is not None:
            return cached

        async with self._lock:
            # double-check（拿到锁后再确认，避免 race）
            cached = self._memory_cache.get(model_slug)
            if cached is not None:
                return cached

            # 2. disk fresh
            disk_entry = self._read_disk_entry(model_slug)
            if disk_entry is not None and not self._is_stale(disk_entry):
                meta = self._deserialize(model_slug, disk_entry, fetch_status="cached")
                self._memory_cache[model_slug] = meta
                return meta

            # 3. network
            try:
                meta = await self._fetch_online(model_slug)
            except Exception:
                # 4. fallback: even stale disk is better than nothing
                if disk_entry is not None:
                    meta = self._deserialize(model_slug, disk_entry, fetch_status="cached")
                else:
                    meta = self._failed_metadata(model_slug)
            else:
                if meta.fetch_status == "ok":
                    self._write_disk_entry(model_slug, meta)

            self._memory_cache[model_slug] = meta
            return meta

    def invalidate(self, model_slug: str | None = None) -> None:
        """测试用：清除缓存（None = 清全部）。"""
        if model_slug is None:
            self._memory_cache.clear()
        else:
            self._memory_cache.pop(model_slug, None)

    # ── 网络 ──

    async def _fetch_online(self, model_slug: str) -> ProviderModelMetadata:
        url = f"{self._base_url}/models"
        async with httpx.AsyncClient(timeout=self._http_timeout) as client:
            resp = await client.get(url)
        resp.raise_for_status()
        payload = resp.json()
        models = payload.get("data", [])
        match = next((m for m in models if m.get("id") == model_slug), None)
        if match is None:
            # 端点 OK 但没这个模型 —— 视同"取不到"，不进 disk cache
            return self._failed_metadata(model_slug)

        return self._from_openrouter_model(model_slug, match)

    def _from_openrouter_model(
        self, model_slug: str, m: dict[str, Any]
    ) -> ProviderModelMetadata:
        # context_limit: min(顶层, top_provider) —— Plan C9 保守规则
        ctx_top = m.get("context_length")
        top = m.get("top_provider") or {}
        ctx_tp = top.get("context_length")
        if ctx_top is not None and ctx_tp is not None:
            ctx_value: int | None = min(ctx_top, ctx_tp)
        elif ctx_top is not None:
            ctx_value = ctx_top
        elif ctx_tp is not None:
            ctx_value = ctx_tp
        else:
            ctx_value = None

        # model_max_output: top_provider.max_completion_tokens
        max_out_raw = top.get("max_completion_tokens")
        max_out: int | None = max_out_raw if isinstance(max_out_raw, int) else None

        # TokenEstimate 不变量 #2：value is None ⟺ source="provider_unavailable"。
        # Provider 响应了但字段缺失时，按"provider 给不出"处理（不新增 source 值）。
        return ProviderModelMetadata(
            model_slug=model_slug,
            context_limit=(
                TokenEstimate(ctx_value, "openrouter_metadata", "exact")
                if ctx_value is not None
                else UNKNOWN_TOKEN
            ),
            model_max_output=(
                TokenEstimate(max_out, "openrouter_metadata", "exact")
                if max_out is not None
                else UNKNOWN_TOKEN
            ),
            fetched_at=_iso_now(),
            fetch_status="ok",
            raw=m,
        )

    @staticmethod
    def _failed_metadata(model_slug: str) -> ProviderModelMetadata:
        return ProviderModelMetadata(
            model_slug=model_slug,
            context_limit=UNKNOWN_TOKEN,
            model_max_output=UNKNOWN_TOKEN,
            fetched_at=_iso_now(),
            fetch_status="failed",
            raw=None,
        )

    # ── 磁盘缓存 ──

    def _read_disk_cache(self) -> dict[str, dict[str, Any]]:
        if self._disk_cache_path is None or not self._disk_cache_path.exists():
            return {}
        try:
            return json.loads(self._disk_cache_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return {}

    def _read_disk_entry(self, model_slug: str) -> dict[str, Any] | None:
        return self._read_disk_cache().get(model_slug)

    def _write_disk_entry(self, model_slug: str, meta: ProviderModelMetadata) -> None:
        if self._disk_cache_path is None:
            return
        all_data = self._read_disk_cache()
        all_data[model_slug] = self._serialize(meta)
        self._atomic_write_json(self._disk_cache_path, all_data)

    def _is_stale(self, entry: dict[str, Any]) -> bool:
        ts = entry.get("_fetched_at_unix")
        if not isinstance(ts, (int, float)):
            return True
        return (time.time() - ts) > self._ttl

    def _serialize(self, meta: ProviderModelMetadata) -> dict[str, Any]:
        return {
            "context_limit": meta.context_limit.to_log(),
            "model_max_output": meta.model_max_output.to_log(),
            "_fetched_at_unix": time.time(),
        }

    def _deserialize(
        self,
        model_slug: str,
        entry: dict[str, Any],
        *,
        fetch_status: FetchStatus,
    ) -> ProviderModelMetadata:
        ctx = entry.get("context_limit", {})
        ctx_te = TokenEstimate(
            value=ctx.get("value"),
            source=ctx.get("token_source", "provider_unavailable"),
            accuracy_mode=ctx.get("token_exactness", "unknown"),
        )
        mo = entry.get("model_max_output", {})
        mo_te = TokenEstimate(
            value=mo.get("value"),
            source=mo.get("token_source", "provider_unavailable"),
            accuracy_mode=mo.get("token_exactness", "unknown"),
        )
        fetched_at_unix = entry.get("_fetched_at_unix")
        fetched_at = (
            _iso_from_unix(fetched_at_unix)
            if isinstance(fetched_at_unix, (int, float))
            else _iso_now()
        )
        return ProviderModelMetadata(
            model_slug=model_slug,
            context_limit=ctx_te,
            model_max_output=mo_te,
            fetched_at=fetched_at,
            fetch_status=fetch_status,
            raw=None,
        )

    @staticmethod
    def _atomic_write_json(path: Path, data: dict[str, Any]) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(path.suffix + ".tmp")
        tmp.write_text(
            json.dumps(data, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        tmp.replace(path)


# ─────────────────────────────────────────────────────────────────────────────
# 共享实例（生产链路默认使用；一次进程内共用两层缓存）
# ─────────────────────────────────────────────────────────────────────────────

_shared_resolver: ModelCapabilityResolver | None = None


def get_default_resolver() -> ModelCapabilityResolver:
    """进程级共享 resolver（带磁盘缓存）。生产链路默认走它。"""
    global _shared_resolver
    if _shared_resolver is None:
        _shared_resolver = ModelCapabilityResolver(
            disk_cache_path=DEFAULT_DISK_CACHE_PATH
        )
    return _shared_resolver


# ─────────────────────────────────────────────────────────────────────────────
# 时间辅助（避免依赖 datetime import）
# ─────────────────────────────────────────────────────────────────────────────


def _iso_now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def _iso_from_unix(ts: float) -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(ts))
