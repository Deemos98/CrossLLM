"""模型池与配置（architecture_v0.1.md §12、phase2_real_llm.md §5/§6/§8/§23）。

配置结构遵循 **Provider / Model / Credential 解耦**：

    provider:             ← 调用方式与凭据（API Key 属于 Provider，不属于 Model）
      name / type
      base_url
      api_key_env         ← 只写环境变量名，不写密钥
    models:               ← 只描述"有哪些模型"
      - id                ← CrossLLM 内部身份（会进文件名，需文件名安全）
        display_name      ← UI 展示名
        model             ← OpenRouter model slug
        enabled           ← 配置层是否可用

职责边界（phase2 §23）：
  - **ModelRegistry 不发起任何 API 请求**，只持有"这个模型是谁、怎么调"的配置
  - API 请求由 providers/ 下的 Provider 负责
  - RoundManager 不直接调用厂商 SDK

关于 enabled 与 ACTIVE/CLOSED（phase2 §6 相关）：
  - `enabled` 是**配置层**开关：这个模型是否进入 CrossLLM 模型池
  - `ACTIVE` / `CLOSED` 是**Session 运行时**状态，由用户决定
  两者互不影响，前者不替代后者。

关于凭据校验时机：
  - **配置结构错误 → 启动时硬失败**（重复 id、未知 provider type、缺 base_url 等）
  - **凭据缺失 → 延迟到真正构造 Provider 时才失败**，启动时只打印告警。
    这样在还没填 key 的环境下，工程依然可以导入、启动、跑测试。
"""

from __future__ import annotations

import os
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .state import ModelParticipant

# 失败注入开关（phase1_mock.md §23 Test 7）。保留：
# 无需消耗真实 token 就能验证错误隔离路径。
FAIL_ENV = "CROSSLLM_FAIL_MODEL"

# 支持的 Provider 类型。Phase 2 第一版只有一个真实类型（phase2 §25：不引入 LiteLLM）。
PROVIDER_MOCK = "mock"
PROVIDER_OPENAI_COMPATIBLE = "openai_compatible"
KNOWN_PROVIDERS = (PROVIDER_MOCK, PROVIDER_OPENAI_COMPATIBLE)

DEFAULT_TIMEOUTS = {
    "connect": 10.0,  # 连接/请求建立（phase2 §16）
    "read": 60.0,  # 两个 chunk 之间的最大间隔
    "total": 300.0,  # 单模型总时长上限
}

# context_limit 的 **fallback** 兜底值（仅在 YAML 未写且 Provider 查询失败时使用）。
#
# 权威来源是 Provider / 模型元数据（core/model_capability.py 联网获取）；
# models.yaml 只是 fallback，因此：
#   - 允许把 context_limit 写成 null / 省略（→ None，交给 Provider 自动发现）
#   - 写具体数字时只当作"查询失败时的备用值"，source 会诚实标成 config_static
#   绝不再用一个静态常量去冒充"模型能力"。
DEFAULT_CONTEXT_LIMIT = 8192  # 模型总上下文窗口（token）fallback 值


@dataclass(frozen=True)
class ProviderSettings:
    """Provider 级配置：调用方式 + 端点 + 凭据位置。

    **API Key 属于 Provider，不属于 Model。** 所有模型共用同一份凭据。

    刻意不保存 api_key 本身，只保存环境变量名 ——
    即使配置对象被打印或误序列化，也不会泄露密钥。
    """

    name: str  # 例如 openrouter
    type: str  # mock | openai_compatible
    base_url: str = ""
    api_key_env: str = ""


@dataclass(frozen=True)
class ProviderConfig:
    """模型级配置：只描述模型身份，不含任何凭据或端点信息。"""

    model_id: str
    display_name: str
    model: str  # OpenRouter model slug，例如 minimax/minimax-m2.7:free
    enabled: bool = True
    # 模型总上下文窗口（token）。
    #   - 权威来源：Provider / 模型元数据（core/model_capability.py，联网获取）
    #   - 本字段只是 **fallback**：Provider 查询失败时才使用；允许为 None（= 不提供）
    # 预算链路（§21）：history_budget = context_limit − fixed_input − reserved_output，
    # 由 core/context_budget.py 计算，能力值取 Provider 解析结果而非本字段。
    # 注意：RealProvider 侧的 §24 兜底算术（input + output ≤ context_limit 且
    # 预算层未给出 reserved 时）仍读本字段，因此它同时充当 Provider 请求期的
    # 最后一道保守上界。二者不一致时本字段只会**更保守**，不会放宽约束。
    context_limit: int | None = DEFAULT_CONTEXT_LIMIT
    connect_timeout: float = DEFAULT_TIMEOUTS["connect"]
    read_timeout: float = DEFAULT_TIMEOUTS["read"]
    total_timeout: float = DEFAULT_TIMEOUTS["total"]
    params: dict[str, Any] = field(default_factory=dict)
    # 该模型允许使用的 MCP 工具名列表（mcp执行确认.md §10/§11）。
    # 空列表表示该模型不使用 MCP，保持与 Phase 2 行为一致。
    tools: list[str] = field(default_factory=list)


DEFAULT_PROVIDER = ProviderSettings(name="mock", type=PROVIDER_MOCK)


def default_pool() -> list[ModelParticipant]:
    """Phase 1 的 Mock 模型池，作为无配置 / Mock 配置时的兜底。"""
    return [
        ModelParticipant(
            id="qwen", display_name="Qwen", provider=PROVIDER_MOCK, model="mock-qwen", enabled=True
        ),
        ModelParticipant(
            id="deepseek",
            display_name="DeepSeek",
            provider=PROVIDER_MOCK,
            model="mock-deepseek",
            enabled=True,
        ),
        ModelParticipant(
            id="gpt", display_name="GPT", provider=PROVIDER_MOCK, model="mock-gpt", enabled=True
        ),
    ]


def _default_configs() -> dict[str, ProviderConfig]:
    return {
        "qwen": ProviderConfig(model_id="qwen", display_name="Qwen", model="mock-qwen"),
        "deepseek": ProviderConfig(
            model_id="deepseek", display_name="DeepSeek", model="mock-deepseek"
        ),
        "gpt": ProviderConfig(model_id="gpt", display_name="GPT", model="mock-gpt"),
    }


def _log(message: str) -> None:
    print(f"[crossllm] {message}", file=sys.stderr)


# --------------------------------------------------------------------------
# .env 加载
# --------------------------------------------------------------------------


def load_dotenv(path: str | Path) -> None:
    """极简 .env 加载器（标准库实现，不引 python-dotenv）。

    真实环境变量优先：.env 只用 setdefault 补位，不覆盖已有值。
    本项目定位为个人研究工具，仅需支持 KEY=value 与 # 注释，够用即可。
    """
    p = Path(path)
    if not p.exists():
        return
    for raw in p.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        if not key:
            continue
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        os.environ.setdefault(key, value)


# --------------------------------------------------------------------------
# Registry
# --------------------------------------------------------------------------


class ModelRegistry:
    """模型池注册表。

    持有真实身份（不变量 A1），向 anonymizer 暴露身份词元用于泄露拦截（A3），
    并向 Provider Factory 提供调用配置。**自身不发起任何 API 请求。**
    """

    def __init__(
        self,
        participants: list[ModelParticipant] | None = None,
        provider: ProviderSettings | None = None,
        configs: dict[str, ProviderConfig] | None = None,
    ):
        pool = participants if participants is not None else default_pool()
        self._pool = [p for p in pool if p.enabled]
        self._by_id = {p.id: p for p in self._pool}
        self._provider = provider or DEFAULT_PROVIDER
        if configs is not None:
            self._configs = configs
        else:
            self._configs = _default_configs() if participants is None else {}

    # ---------- 构造 ----------

    @classmethod
    def from_yaml(cls, path: str | Path) -> ModelRegistry:
        """从 YAML 配置构建注册表。

        结构错误在启动时硬失败；凭据缺失只告警，延迟到真正调用时才失败。
        """
        import yaml  # 局部导入：无配置时不需要该依赖

        p = Path(path)
        if not p.exists():
            raise FileNotFoundError(f"模型配置不存在: {p}")

        data = yaml.safe_load(p.read_text(encoding="utf-8")) or {}

        provider = cls._parse_provider(data.get("provider"), path)

        raw_models = data.get("models") or []
        if not raw_models:
            raise ValueError(f"{p} 中未定义任何模型")

        participants: list[ModelParticipant] = []
        configs: dict[str, ProviderConfig] = {}
        seen: set[str] = set()

        for item in raw_models:
            model_id = str(item.get("id", "")).strip()
            if not model_id:
                raise ValueError(f"{p} 中存在缺少 id 的模型定义")
            if model_id in seen:
                raise ValueError(f"{p} 中 model_id 重复: {model_id!r}")
            seen.add(model_id)

            slug = str(item.get("model", "")).strip()
            if not slug:
                raise ValueError(f"模型 {model_id!r} 缺少 model（OpenRouter model slug）")

            enabled = bool(item.get("enabled", True))
            timeouts = item.get("timeout") or {}

            # context_limit 允许留空（None）：能力由 Provider 元数据提供，
            # YAML 只是 fallback。留空 = 不提供 fallback，不报错。
            raw_ctx = item.get("context_limit")
            context_limit = None if raw_ctx is None else int(raw_ctx)
            if context_limit is not None and context_limit <= 0:
                raise ValueError(f"模型 {model_id!r} 的 context_limit 必须为正整数")
            if "max_output_tokens" in item:
                raise ValueError(
                    f"模型 {model_id!r} 配置了已废弃的 max_output_tokens。"
                    "模型输出能力只能来自 Provider 元数据（§22 / §二），"
                    "本字段不再被读取，请从配置中移除。"
                )

            tools = [str(t).strip() for t in (item.get("tools") or []) if str(t).strip()]

            configs[model_id] = ProviderConfig(
                model_id=model_id,
                # display_name 与 name 都接受，方便直接照文档示例书写
                display_name=str(item.get("display_name") or item.get("name") or model_id),
                model=slug,
                enabled=enabled,
                context_limit=context_limit,
                connect_timeout=float(timeouts.get("connect", DEFAULT_TIMEOUTS["connect"])),
                read_timeout=float(timeouts.get("read", DEFAULT_TIMEOUTS["read"])),
                total_timeout=float(timeouts.get("total", DEFAULT_TIMEOUTS["total"])),
                params=dict(item.get("params") or {}),
                tools=tools,
            )
            participants.append(
                ModelParticipant(
                    id=model_id,
                    display_name=configs[model_id].display_name,
                    provider=provider.type,
                    model=slug,
                    enabled=enabled,
                    tools=list(tools),
                )
            )

        if not any(c.enabled for c in configs.values()):
            raise ValueError(f"{p} 中没有启用任何模型")

        cls._warn_if_credentials_missing(provider)

        return cls(participants, provider, configs)

    @staticmethod
    def _parse_provider(raw: Any, path: Path) -> ProviderSettings:
        if not raw:
            raise ValueError(f"{path} 缺少 provider 配置块")
        ptype = str(raw.get("type", "")).strip()
        if ptype not in KNOWN_PROVIDERS:
            raise ValueError(f"{path} 的 provider.type 未知: {ptype!r}，可选 {KNOWN_PROVIDERS}")
        settings = ProviderSettings(
            name=str(raw.get("name") or ptype),
            type=ptype,
            base_url=str(raw.get("base_url", "")).strip(),
            api_key_env=str(raw.get("api_key_env", "")).strip(),
        )
        if settings.type != PROVIDER_MOCK and not settings.base_url:
            raise ValueError(f"{path} 的 provider 缺少 base_url")
        return settings

    @staticmethod
    def _warn_if_credentials_missing(provider: ProviderSettings) -> None:
        """凭据缺失只告警，不阻断启动 —— 否则在还没填 key 的环境下无法跑测试。"""
        if provider.type == PROVIDER_MOCK:
            return
        if not provider.api_key_env:
            _log(f"告警：provider {provider.name!r} 未配置 api_key_env，真实调用将失败")
            return
        if not os.environ.get(provider.api_key_env):
            _log(
                f"告警：环境变量 {provider.api_key_env} 未设置，"
                f"provider {provider.name!r} 暂不可用（请在 .env 或系统环境变量中提供）"
            )

    # ---------- 查询 ----------

    @property
    def provider_settings(self) -> ProviderSettings:
        return self._provider

    @property
    def participants(self) -> list[ModelParticipant]:
        return list(self._pool)

    def ids(self) -> list[str]:
        return [p.id for p in self._pool]

    def get(self, model_id: str) -> ModelParticipant:
        try:
            return self._by_id[model_id]
        except KeyError:
            raise KeyError(f"未知 model_id: {model_id!r}") from None

    def has(self, model_id: str) -> bool:
        return model_id in self._by_id

    def participant_for(self, model_id: str) -> ModelParticipant | None:
        """按 model_id 查找参与者，返回 None 表示不存在。"""
        for p in self.participants:
            if p.id == model_id:
                return p
        return None

    def config_for(self, model_id: str) -> ProviderConfig:
        try:
            return self._configs[model_id]
        except KeyError:
            raise KeyError(f"模型 {model_id!r} 无调用配置") from None

    def api_key(self) -> str:
        """取 Provider 级凭据。**所有模型共用同一份密钥。**

        只在真正构造 Provider 时调用，不缓存进任何可序列化的结构。
        """
        settings = self._provider
        if settings.type == PROVIDER_MOCK:
            return ""
        if not settings.api_key_env:
            raise ValueError(f"Provider {settings.name!r} 未配置 api_key_env")
        value = os.environ.get(settings.api_key_env, "")
        if not value:
            raise ValueError(
                f"环境变量 {settings.api_key_env} 未设置。"
                f"请在 .env 或系统环境变量中提供，不要写进配置文件或代码。"
            )
        return value

    def failing_models(self) -> set[str]:
        raw = os.environ.get(FAIL_ENV, "")
        return {s.strip() for s in raw.split(",") if s.strip() and self.has(s.strip())}

    def identity_tokens(self) -> tuple[str, ...]:
        """所有需要被拦截的身份词元（A3、phase2 §22）。

        这里会包含 OpenRouter model slug（如 minimax/minimax-m2.7:free）与展示名。
        这类词在正常技术讨论中可能自然出现，因此**注入正文**的匹配只做告警，
        不阻断整轮（见 core/prompt_builder.py 的软/硬分离）。
        """
        tokens: list[str] = []
        for p in self._pool:
            tokens.extend(p.identity_tokens())
        return tuple(t for t in tokens if t)
