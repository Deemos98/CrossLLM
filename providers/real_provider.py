"""OpenAI-compatible 真实 Provider（phase2_real_llm.md §4/§13/§15/§16、
mcp执行确认.md §12）。

Phase 2 第一版只实现一种真实协议：OpenAI-compatible `/chat/completions` + `stream=true`。
Phase 3 在此基础上增加 tool calling 循环：模型返回 tool_calls 时，通过 MCP Client
执行工具，把结果放回当前上下文再次请求，直到产出 Final Answer。

这么选的原因（D1 结论）：
  - Qwen(DashScope 兼容模式) / DeepSeek / GLM / Doubao / OpenAI 全部兼容这个协议，
    一个适配器覆盖 5 家，不违反 architecture §30 D9「不要为每家厂商重复实现一套 API Client」
  - 直接接真实 HTTP streaming，才能观察各 Provider 的实际差异（phase2 §28 问题 2）
  - 第一版不引入 LiteLLM；等需要接 Gemini / Anthropic 这类非兼容协议、
    适配成本明显变高时再评估（phase2 §25）

请求层复用已装的 httpx，**不新增 openai SDK** ——
SDK 会替我们归一化掉差异，而观察差异正是本阶段的目的之一。
"""

from __future__ import annotations

import json
import logging
import time
from collections.abc import AsyncIterator
from typing import Any

import httpx

from core.model_manager import ProviderConfig, ProviderSettings

from .base import LLMProvider, ProviderError

_log = logging.getLogger("crossllm")

# 部分厂商会在流里插入非 JSON 的保活行，容忍它们而不是直接报错
_DONE_MARKER = "[DONE]"

# 粗略 token 估算，与 core/round_manager.py 的 CHARS_PER_TOKEN 保持一致。
# 仅用于估算当前 messages 占用，真正的硬约束是 context_limit。
CHARS_PER_TOKEN = 3


async def _parse_sse_events(
    lines: AsyncIterator[str],
    raw: list[str] | None = None,
) -> AsyncIterator[dict]:
    """把 OpenAI-compatible 的 SSE 行解析成结构化事件。

    事件类型：
      {"type": "content", "content": str}
      {"type": "tool_call_delta", "delta": {"index": int, "id": str, "name": str, "arguments": str}}
      {"type": "finish", "reason": str | None}
      {"type": "usage", "usage": dict}

    解析不出来的行一律跳过（保活注释、空 choices、未知字段），
    这样单个异常行不会打断整条流。

    raw 不为 None 时，会把解析失败的前几行收进去，供空响应时诊断 ——
    真实 Provider 偶尔会返回 200 + 错误体，没有这段信息几乎无法排查。
    """
    async for line in lines:
        line = line.strip()
        if not line or not line.startswith("data:"):
            continue
        payload = line[len("data:") :].strip()
        if not payload:
            continue
        if payload == _DONE_MARKER:
            continue
        try:
            data = json.loads(payload)
        except json.JSONDecodeError:
            if raw is not None and len(raw) < 5:
                raw.append(payload[:200])
            continue

        if isinstance(data, dict):
            usage = data.get("usage")
            if isinstance(usage, dict):
                yield {"type": "usage", "usage": dict(usage)}

            choices = data.get("choices")
            if isinstance(choices, list) and choices:
                finish_reason = choices[0].get("finish_reason")
                if finish_reason is not None:
                    yield {"type": "finish", "reason": finish_reason}

            try:
                delta = data["choices"][0]["delta"]
            except (KeyError, IndexError, TypeError):
                continue

            if not isinstance(delta, dict):
                continue

            content = delta.get("content")
            if content:
                yield {"type": "content", "content": content}

            tool_calls = delta.get("tool_calls")
            if isinstance(tool_calls, list):
                for tc in tool_calls:
                    if not isinstance(tc, dict):
                        continue
                    func = tc.get("function") or {}
                    yield {
                        "type": "tool_call_delta",
                        "delta": {
                            "index": int(tc.get("index", 0)),
                            "id": tc.get("id") or "",
                            "name": func.get("name") or "",
                            "arguments": func.get("arguments") or "",
                        },
                    }


class RealProvider:
    """OpenAI-compatible Provider。

    负责（phase2 §4）：API 地址、API Key、model name、HTTP 调用、
    streaming、Provider 特有错误、超时、响应解析。

    Phase 3 新增：接收可选 tools 列表，在 Provider 内部完成 tool-call 循环，
    只把 Final Answer 的文本 chunk 流给调用方。
    """

    def __init__(
        self,
        settings: ProviderSettings,
        config: ProviderConfig,
        api_key: str,
        *,
        client: httpx.AsyncClient | None = None,
        mcp_registry: Any | None = None,
    ):
        self._settings = settings
        self._cfg = config
        self._api_key = api_key
        self._client = client  # 测试注入用；为 None 时自行创建并关闭
        # 真实 token 用量，generate() 结束后由调用方读取（phase1_1 §6.2）。
        # Mock Provider 有同名属性但恒为 None，调用方无需区分。
        self.usage: dict | None = None
        # MCP 工具执行器，需实现 tool_schemas(names) 与 call_tool(name, arguments)。
        # 为 None 时模型即使返回 tool_calls 也无法执行，会返回错误提示给模型。
        self._mcp_registry = mcp_registry
        # §一：本轮由 Provider 元数据解析出的 context_limit（RoundState 注入）。
        # None = 未接线 / 本轮解析不出 → 回退 self._cfg.context_limit（YAML fallback）。
        self._context_limit: int | None = None

    def __repr__(self) -> str:
        # 绝不回显 api_key
        return (
            f"RealProvider(provider={self._settings.name!r}, "
            f"model_id={self._cfg.model_id!r}, model={self._cfg.model!r})"
        )

    # ── §一：能力来源（Provider 元数据优先，YAML 仅 fallback）────────────

    def set_round_context_limit(self, context_limit: int | None) -> None:
        """注入**本轮**由 Provider 元数据解析出的 context_limit（§一）。

        RoundManager 在构造出本 Provider 之后、调用 ``generate()`` 之前调用本方法，
        因此 Provider 侧的 §24 算术（``input + output ≤ context_limit``）与预算层
        使用**同一个**值，不再读 models.yaml 的静态快照。

        刻意**不**改 Provider Protocol / ``generate()`` 签名：
        `reserved_output_tokens` 仍通过既有 ``max_output_tokens`` 参数传递，
        本方法只传"这一轮的上界是多少"。

        None = 本轮解析不出（Provider 查不到且 YAML 也没有）→ 回退
        ``_cfg.context_limit``；两者都没有时 ``_dynamic_max_tokens`` 返回 None，
        即**不下发** max_tokens（unknown / degraded 语义保持不变）。
        """
        self._context_limit = context_limit

    def _effective_context_limit(self) -> int | None:
        """§一：Provider 元数据解析值优先；缺失时回退 models.yaml（fallback）。"""
        if self._context_limit is not None:
            return self._context_limit
        return self._cfg.context_limit

    @staticmethod
    def _estimate_messages_tokens(messages: list[dict], tools: list[dict] | None = None) -> int:
        """粗略估算 messages + tools 的总 token 数。

        使用与 round_manager.py 一致的 CHARS_PER_TOKEN=3 估算，
        只用于避免超过 context_limit，不作为计费依据。
        """
        total_chars = 0

        for msg in messages:
            # message role + content
            if isinstance(msg.get("content"), str):
                total_chars += len(msg["content"])
            elif isinstance(msg.get("content"), list):
                # 多模态 content（暂只算文本部分）
                for part in msg["content"]:
                    if isinstance(part, dict) and isinstance(part.get("text"), str):
                        total_chars += len(part["text"])

            # tool_calls (assistant message)
            for tc in msg.get("tool_calls", []):
                func = tc.get("function", {})
                total_chars += len(func.get("name", "")) + len(func.get("arguments", ""))

            # tool result messages
            if msg.get("role") == "tool":
                total_chars += len(str(msg.get("content", "")))

        # tool definitions (每个约 200-500 chars)
        if tools:
            for tool in tools:
                func = tool.get("function", {})
                total_chars += len(func.get("name", "")) + len(func.get("description", ""))
                params_str = str(func.get("parameters", {}))
                total_chars += len(params_str)

        return -(-total_chars // CHARS_PER_TOKEN)  # ceil div

    def _dynamic_max_tokens(
        self,
        messages: list[dict],
        tools: list[dict] | None = None,
        # S3.11（MD §22/§23）：CrossLLM 预算层的有效输出上限
        # = ContextBudget.reserved_output_tokens = min(model_max_output, request_max)
        # None = 预算层给不出（degraded）
        reserved_output_tokens: int | None = None,
    ) -> int | None:
        """每次请求前计算输出上限，保证 input + output <= context_limit。

        硬约束（OpenRouter 语义）：
            context_limit 是 input + output 的合计上限

        四个上限的严格区分（MD §22 / §三 / §四）：
            model_max_output       —— 模型自身能力上限（**只**来自 Provider 元数据）
            request_max_output     —— 本次请求显式上限（RoundState）
            reserved_output_tokens —— 预算层 min(model_max, request)，CrossLLM 侧预留
            actual_output_limit    —— 本函数产物，真正发给 Provider 的 max_tokens

        context_limit 的来源（§一）走 ``_effective_context_limit()``：
        RoundState 注入的 Provider 元数据解析值优先，YAML 只在解析不出时 fallback。

        本函数只做 Provider 该做的一件事：把预算层的上限与 §24 的剩余空间求交。

        - context_limit 已知 → remaining = context_limit − actual_input；
          若 remaining <= 0，说明输入已占满窗口，**明确报错**（不猜一个值）。
        - reserved 已知      → actual = min(reserved, remaining)（或 reserved 本身）
        - reserved 未知      → **不虚构**：若 context_limit 已知，只下发 remaining
          （这是 §24 的算术上界，不是经验系数）；两者都未知 → 返回 None，
          表示本次请求**不下发** max_tokens（由 Provider 自身默认语义决定）。

        禁止恢复固定 16000 / 80% ratio / context_limit × 0.9 / 任何经验系数。
        Provider **不得**重新计算另一套预算（S3.11 验收项 5）。
        """
        actual_input = self._estimate_messages_tokens(messages, tools)
        ctx_limit = self._effective_context_limit()

        remaining: int | None = None
        if ctx_limit is not None:
            remaining = ctx_limit - actual_input
            if remaining <= 0:
                # 输入已占满（或超出）整个窗口，无法再生成任何输出。
                # 这是真实的超限情况，明确报错而不是猜测一个值。
                raise ProviderError(
                    f"输入已超出上下文窗口: input={actual_input} >= "
                    f"context_limit={ctx_limit}"
                )

        if reserved_output_tokens is not None and reserved_output_tokens > 0:
            actual_output: int | None = (
                reserved_output_tokens
                if remaining is None
                else min(reserved_output_tokens, remaining)
            )
            source = "budget_reserved"
        elif remaining is not None:
            actual_output = remaining
            source = "context_limit_remaining"
        else:
            actual_output = None
            source = "unknown_no_cap"

        _log.info(
            "context budget: input=%d + output=%s (limit=%s) "
            "reserved=%s reserved_source=%s remaining=%s",
            actual_input,
            actual_output,
            ctx_limit,
            reserved_output_tokens,
            source,
            remaining,
        )

        return actual_output

    async def generate(
        self,
        *,
        round_id: int,
        prompt: str,
        tools: list[dict] | None = None,
        # S3.11（MD §22/§23）：预算层有效输出上限 = ContextBudget.reserved_output_tokens
        max_output_tokens: int | None = None,
    ) -> AsyncIterator[str]:
        """逐 chunk 产出 Final Answer。

        round_id 在 Phase 2/3 的 OpenAI-compatible 调用中不使用（每轮都是一次独立
        请求，上下文由 prompt 携带），保留参数是为了维持统一接口形状。

        max_output_tokens（S3.11）= CrossLLM 预算层的
        ``ContextBudget.reserved_output_tokens``（= min(model_max_output, request_max)）。
        本轮内所有请求（含 tool call 后的再请求）统一采用该上限，
        Provider 不重算预算；最终请求值再与 context_limit 求交（MD §24）。
        None = 预算层给不出（能力与请求上限都未知）→ 不伪造固定值：
        context_limit 已知时下发剩余空间，否则不下发 max_tokens。

        如果提供了 tools 且模型产生 tool_calls，会在 Provider 内部完成：
          tool call → MCP Client → tool result → 再次请求 LLM
        直到模型返回 Final Answer。工具调用过程本身不会流给调用方。
        """
        # 供 _stream_completion（可能被调用多次：tool call 后再请求）复用
        self._reserved_output_tokens = max_output_tokens
        messages: list[dict] = [{"role": "user", "content": prompt}]
        deadline = time.monotonic() + self._cfg.total_timeout

        # 如果调用方没显式传 tools，但模型配置允许使用某些 MCP 工具，则自动解析 schema。
        if tools is None and self._cfg.tools and self._mcp_registry is not None:
            try:
                _log.info("[mcp] 正在获取工具定义: %s", self._cfg.tools)
                tools = await self._mcp_registry.tool_schemas(self._cfg.tools)
                if tools:
                    _log.info("[mcp] 工具定义已加载 (%d 个): %s", len(tools), [t.get('function', {}).get('name', '?') for t in tools])
                else:
                    _log.warning("[mcp] ⚠ 工具定义为空（MCP Server 可能未启动）")
            except Exception as exc:  # noqa: BLE001 - MCP 未就绪时回退到无工具模式
                _log.warning("[mcp] ✗ 获取工具定义失败: %s", exc)
                tools = None

        # 首次请求即携带 tools；后续 tool-result 循环也保留 tools，
        # 让模型在需要时可以再次调用。
        # 限制最大工具调用次数，避免模型在信息收集阶段无限循环。
        max_total_tool_calls = 15
        total_tool_calls = 0
        while True:
            content_buffer: list[str] = []
            tool_calls_by_index: dict[int, dict] = {}
            produced = False
            last_usage: dict | None = None
            finish_reason: str | None = None

            async for event in self._stream_completion(messages, tools, deadline):
                produced = True
                etype = event.get("type")
                if etype == "content":
                    yield event["content"]
                    content_buffer.append(event["content"])
                elif etype == "tool_call_delta":
                    self._accumulate_tool_call(tool_calls_by_index, event["delta"])
                elif etype == "usage":
                    last_usage = event["usage"]
                elif etype == "finish":
                    finish_reason = event.get("reason")

            if not produced:
                raise ProviderError(self._scrub("响应无有效内容"))

            # 没有 tool_calls，本次产出即为 Final Answer。
            if not tool_calls_by_index:
                if last_usage:
                    self.usage = dict(last_usage)
                return

            # 模型要求调用工具：构造 assistant message + tool result messages，继续循环。
            tool_round = total_tool_calls + len(tool_calls_by_index)
            _log.info("[mcp] 🔧 检测到 %d 个 tool_call（累计将 %d/%d）", len(tool_calls_by_index), tool_round, max_total_tool_calls)
            tool_calls = self._finalize_tool_calls(tool_calls_by_index)
            for tc in tool_calls:
                args_preview = json.dumps(tc.get('function', {}).get('arguments', {}), ensure_ascii=False)[:200]
                _log.info("[mcp] → 调用工具: %s(%s)", tc.get('function', {}).get('name', '?'), args_preview)
            messages.append(
                {
                    "role": "assistant",
                    "content": "".join(content_buffer) or None,
                    "tool_calls": tool_calls,
                }
            )

            for tc in tool_calls:
                total_tool_calls += 1
                if total_tool_calls > max_total_tool_calls:
                    _log.warning(
                        "[mcp] ⚠ 工具调用次数已达上限 %d/%d，不再实际调用 %s",
                        total_tool_calls, max_total_tool_calls, tc.get("function", {}).get("name", "?")
                    )
                    result = "[已达到最大工具调用次数上限。请基于已收集的信息直接给出最终结论，不要再调用工具。]"
                else:
                    result = await self._execute_tool_call(tc)
                    _log.info("[mcp] ← 工具返回: %d 字符 | 预览: %s", len(result), result[:150])
                messages.append(
                    {"role": "tool", "tool_call_id": tc["id"], "content": result}
                )

            # 记录最近一次 usage；多轮工具循环的精确求和留待后续优化。
            if last_usage:
                self.usage = dict(last_usage)

    # ---------- tool calling 内部方法 ----------

    @staticmethod
    def _accumulate_tool_call(by_index: dict[int, dict], delta: dict) -> None:
        idx = int(delta.get("index", 0))
        entry = by_index.setdefault(idx, {"id": "", "name": "", "arguments": ""})
        if delta.get("id"):
            entry["id"] = delta["id"]
        if delta.get("name"):
            entry["name"] = delta["name"]
        entry["arguments"] += delta.get("arguments", "")

    @staticmethod
    def _finalize_tool_calls(by_index: dict[int, dict]) -> list[dict]:
        out = []
        for idx in sorted(by_index):
            tc = by_index[idx]
            out.append(
                {
                    "id": tc["id"] or f"call_{idx}",
                    "type": "function",
                    "function": {
                        "name": tc["name"],
                        "arguments": tc["arguments"],
                    },
                }
            )
        return out

    async def _execute_tool_call(self, tc: dict) -> str:
        """调用一个 tool，返回文本结果。失败时把错误信息作为结果返回，让模型处理。"""
        name = tc["function"]["name"]
        raw_args = tc["function"].get("arguments", "")
        try:
            arguments = json.loads(raw_args) if raw_args else {}
        except json.JSONDecodeError:
            return f"工具参数解析失败: {raw_args[:200]}"

        if self._mcp_registry is None:
            return "MCP client 未配置，无法执行工具"

        try:
            return await self._mcp_registry.call_tool(name, arguments)
        except Exception as exc:  # noqa: BLE001 - 工具失败隔离在模型内
            return f"工具调用失败: {type(exc).__name__}: {exc}"

    # ---------- HTTP streaming ----------

    async def _stream_completion(
        self,
        messages: list[dict],
        tools: list[dict] | None,
        deadline: float,
    ) -> AsyncIterator[dict]:
        """发起一次流式请求，产出结构化事件。"""
        url = self._settings.base_url.rstrip("/") + "/chat/completions"
        headers = {
            "Authorization": f"Bearer {self._api_key}",
            "Content-Type": "application/json",
        }
        body = {
            "model": self._cfg.model,
            "messages": messages,
            "stream": True,
            # 让 Provider 在流末尾回传真实 token 用量（phase1_1 §6.2）
            "stream_options": {"include_usage": True},
        }
        # S3.11：动态计算输出上限 —— 采用预算层 reserved_output_tokens，
        # 再与 context_limit 的剩余空间求交（MD §24）。
        # 返回 None = 预算层给不出且 context_limit 也未知 → **不下发** max_tokens
        # （不虚构固定值；见 _dynamic_max_tokens 的四个上限说明）。
        _max_tokens = self._dynamic_max_tokens(
            messages, tools, getattr(self, "_reserved_output_tokens", None)
        )
        if _max_tokens is not None:
            body["max_tokens"] = _max_tokens
        if tools:
            body["tools"] = tools
        body.update(self._cfg.params)

        # connect：建连超时；read：两个 chunk 之间的最大间隔（phase2 §16）
        timeout = httpx.Timeout(
            connect=self._cfg.connect_timeout,
            read=self._cfg.read_timeout,
            write=30.0,
            pool=30.0,
        )
        client = self._client or httpx.AsyncClient(timeout=timeout)

        try:
            async with client.stream("POST", url, headers=headers, json=body) as response:
                if response.status_code >= 400:
                    raise await self._http_error(response)

                raw: list[str] = []
                yielded = False
                async for event in _parse_sse_events(response.aiter_lines(), raw):
                    yielded = True
                    if time.monotonic() > deadline:
                        raise ProviderError(
                            f"总时长超过上限 {self._cfg.total_timeout:g}s"
                        )
                    yield event

                if not yielded:
                    hint = f"；原始片段={raw}" if raw else ""
                    raise ProviderError(
                        self._scrub(f"HTTP {response.status_code} 响应无有效内容{hint}")
                    )
        except ProviderError:
            raise
        except Exception as exc:  # noqa: BLE001 - 厂商错误一律归一化为 ProviderError
            raise ProviderError(self._scrub(f"请求失败: {type(exc).__name__}")) from None
        finally:
            if self._client is None:
                await client.aclose()

    async def _http_error(self, response: httpx.Response) -> ProviderError:
        """把 HTTP 错误转成 ProviderError。

        只取响应体里的 error.message —— **绝不**带上请求头、URL 查询串或密钥
        （phase2 §8/§20：错误消息会进日志和 error 事件）。
        """
        detail = ""
        try:
            data = json.loads(await response.aread())
            if isinstance(data, dict):
                err = data.get("error")
                detail = err.get("message") if isinstance(err, dict) else str(err or "")
        except Exception:  # noqa: BLE001
            detail = ""
        text = f"HTTP {response.status_code}"
        if detail:
            text += f": {detail}"
        return ProviderError(self._scrub(text))

    def _scrub(self, text: str) -> str:
        """兜底脱敏：万一某处拼进了密钥，也不让它流出 Provider。"""
        if self._api_key:
            text = text.replace(self._api_key, "***")
        return text


# 显式声明实现统一接口，便于回归检查
_: type[LLMProvider] = RealProvider  # noqa: F401 - 仅用于类型自检（Protocol 为 duck typing）
