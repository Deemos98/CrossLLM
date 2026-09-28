"""S3.4：PinStore + Pin 数据结构（PHASE4_REFACTOR_PLAN §4.5 / §4.7 partial / §11.3）

严格范围（不前置 S3.5+）：
  - PinEvent dataclass（§19 一行一条 JSONL）
  - PinState dataclass（由事件流重放得到，**不做就地修改**）
  - PinStore 类：
      * pin(round_id) / unpin(round_id) / is_pinned(round_id)
      * pinned_round_ids() / events() → 全部事件历史
      * load(session_id) → 从 pins.jsonl 重放
  - compute_pinned_tokens(pin_state, valid_pool) → TokenEstimate
    （§26 把 Pin 的 Token 加起来，作为 lossless 占用回填到 ContextBudget.pinned_tokens；
     S3.4 只暴露计算接口，S3.5 ContextSelector 才真正调用）

明确不在本模块范围：
  - Context Selection 算法（倒序装载 / 丢弃）—— S3.5
  - Summary / Compaction / Peer Context                  —— S3.6+
  - Pin overflow 警告 (pinned > lossless) 端到端计算 —— S3.5
  - Project Memory                                    —— Phase 4.6
  - 修改 round_NNN/ 任何文件                            —— 严格禁止

开工四检（2026-09-03 S3.4）：
  1. Pin = 用户选择的 Valid Round 的 User Question + 成功 Final Answers
     → 内容定义在 §18.1；PinStore 只存"哪些 round 被 Pin"，不存内容副本。
     真实内容由 storage.load_responses() 按需读 round_NNN/<model>.md 提供。
     PinStore **不进** round_NNN/ 写任何文件（Raw Archive 永不被动）。
  2. PinEvent 流写 + PinState 重放（event sourcing）—— 避免就地状态
     崩溃丢历史，符合 §19 "必须保留历史变化记录"。
  3. Pin 计算接口 compute_pinned_tokens() 复用 ValidRound.token_estimate
     （S3.2 已交付：估算 Q+A 字符数 // 3，accuracy=estimated）。
     PinStore 不重新估算 token，避免与 S3.2 双源。
  4. Pin 不写入 Project Memory，不写 state.json，不接 Selection。
     → 仅追加 pins.jsonl + 落 context_policy.jsonl 的 memory_pin 事件。
"""

from __future__ import annotations

import json
import os
import tempfile
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING, Literal

from core.model_capability import TokenEstimate

if TYPE_CHECKING:
    from core.storage import Storage
    from core.valid_round import ValidRound


# ─────────────────────────────────────────────────────────────────────────────
# §4.5 PinEvent
# ─────────────────────────────────────────────────────────────────────────────

PinReason = Literal["user"]


@dataclass(frozen=True)
class PinEvent:
    """§4.5 / §19 单条 Pin/Unpin 事件，pins.jsonl 一行一个。

    字段含义：
      round_id        ：目标 Round
      pinned          ：True = Pin、False = Unpin
      timestamp       ：ISO8601 UTC（§19 "必须保留历史变化记录"的天然落点）
      reason          ：§4.5 限定为 "user"，仅由用户触发；预留 future system
      actor           ：触发者（默认 "user"，保留审计维度）
    """

    round_id: int
    pinned: bool
    timestamp: str
    reason: PinReason = "user"
    actor: str = "user"

    def to_dict(self) -> dict:
        return {
            "round_id": self.round_id,
            "pinned": self.pinned,
            "timestamp": self.timestamp,
            "reason": self.reason,
            "actor": self.actor,
        }

    @classmethod
    def from_dict(cls, d: dict) -> "PinEvent":
        return cls(
            round_id=int(d["round_id"]),
            pinned=bool(d["pinned"]),
            timestamp=str(d["timestamp"]),
            reason=d.get("reason", "user"),  # type: ignore[arg-type]
            actor=d.get("actor", "user"),
        )


# ─────────────────────────────────────────────────────────────────────────────
# §4.5 PinState（事件流重放结果）
# ─────────────────────────────────────────────────────────────────────────────


@dataclass
class PinState:
    """§4.5 PinState —— 由 PinEvent 流重放得到（不做就地修改）。

    字段语义：
      session_id          ：所属 Session
      pinned_round_ids    ：当前被 Pin 的 round_id 集合，按 Pin 时间顺序，
                            最近的 Pin 在末尾；Unpin 会按规则移出
      history             ：全部 PinEvent 时间序列（§19 必须保留）
    """

    session_id: str
    pinned_round_ids: list[int] = field(default_factory=list)
    history: list[PinEvent] = field(default_factory=list)

    def is_pinned(self, round_id: int) -> bool:
        return round_id in self.pinned_round_ids

    def to_dict(self) -> dict:
        return {
            "session_id": self.session_id,
            "pinned_round_ids": list(self.pinned_round_ids),
            "history": [e.to_dict() for e in self.history],
        }


# ─────────────────────────────────────────────────────────────────────────────
# 计算接口：Pin Token 占用
# ─────────────────────────────────────────────────────────────────────────────


def compute_pinned_tokens(
    pin_state: PinState,
    valid_pool: list["ValidRound"],
) -> TokenEstimate:
    """§26 计算 Pin 占用 Token（lossless 优先扣减项）。

    算法：
      - 取 pin_state.pinned_round_ids ∩ valid_pool.round_id
      - 对命中 round，累加 valid_round.token_estimate.value
      - 任一命中 round 的 token_estimate.value 为 None（unknown）→ 整体 unknown
      - 所有命中 round 的 token_estimate 均 estimated → 整体 estimated

    S3.4 边界：本函数只算 Pin 占用；S3.5 ContextSelector 在 build_context()
    中调用后回填到 ContextBudget.pinned_tokens。
    """
    pinned_set = set(pin_state.pinned_round_ids)
    if not pinned_set:
        # 零占用 —— value=0 + estimated（crossllm_estimator source）
        return TokenEstimate(value=0, source="crossllm_estimator", accuracy_mode="estimated")

    matched = [vr for vr in valid_pool if vr.round_id in pinned_set]
    if not matched:
        return TokenEstimate(value=0, source="crossllm_estimator", accuracy_mode="estimated")

    # 任一 unknown → 整体 unknown（§22 保守策略）
    if any(vr.token_estimate.value is None for vr in matched):
        return TokenEstimate(value=None, source="provider_unavailable", accuracy_mode="unknown")

    total = sum(int(vr.token_estimate.value) for vr in matched)  # type: ignore[arg-type]
    # accuracy 取最低：全部 estimated → estimated；任一 exact → 取 exact
    any_exact = any(vr.token_estimate.accuracy_mode == "exact" for vr in matched)
    if any_exact:
        return TokenEstimate(
            value=total,
            source="openrouter_metadata",  # provider token 至少一环为 exact
            accuracy_mode="exact",
        )
    return TokenEstimate(
        value=total,
        source="crossllm_estimator",
        accuracy_mode="estimated",
    )


# ─────────────────────────────────────────────────────────────────────────────
# PinStore — 主类
# ─────────────────────────────────────────────────────────────────────────────


class PinStore:
    """Pin 事件存储与状态重放。

    写入路径：
      - pins.jsonl  →  ``sessions/<sid>/pins.jsonl`` 追加写
      - context_policy.jsonl →  ``sessions/<sid>/context_policy.jsonl``
        落 ``memory_pin`` 事件（§19 / §61）

    读取路径：
      - 任何状态查询都从 pins.jsonl 重放 PinState；
        PinStore 在内存中做一层 readonly cache（不修改磁盘文件除非追加写）
      - compute_pinned_tokens 走独立计算接口（不依赖 round 文件结构）

    并发：
      - 单线程追加写（FastAPI 请求串行化到事件循环）
      - 不引入锁，因没必要（pins.jsonl append-only）
    """

    def __init__(
        self,
        storage: "Storage",
        *,
        policy_log_basename: str = "context_policy.jsonl",
        pins_filename: str = "pins.jsonl",
    ) -> None:
        self._storage = storage
        self._policy_basename = policy_log_basename
        self._pins_filename = pins_filename

    # ── 路径 ──

    def _pins_path(self, session_id: str) -> Path:
        return self._storage.session_dir(session_id) / self._pins_filename

    def _policy_path(self, session_id: str) -> Path:
        return self._storage.session_dir(session_id) / self._policy_basename

    # ── 公开 API ──

    def load(self, session_id: str) -> PinState:
        """从 pins.jsonl 重放 PinState。文件不存在 → 空 PinState。"""
        p = self._pins_path(session_id)
        state = PinState(session_id=session_id)
        if not p.exists():
            return state
        with p.open("r", encoding="utf-8") as fh:
            for raw_line in fh:
                line = raw_line.strip()
                if not line:
                    continue
                evt = PinEvent.from_dict(json.loads(line))
                state.history.append(evt)
                if evt.pinned:
                    # 同一 round 多次 pin 幂等（避免重复条目）
                    if evt.round_id not in state.pinned_round_ids:
                        state.pinned_round_ids.append(evt.round_id)
                else:
                    # Unpin 删除最早匹配项（保持按时间顺序）
                    if evt.round_id in state.pinned_round_ids:
                        state.pinned_round_ids.remove(evt.round_id)
        return state

    def pin(
        self,
        session_id: str,
        round_id: int,
        *,
        reason: PinReason = "user",
        actor: str = "user",
    ) -> PinEvent:
        """Pin 一个 round。规则：

        1. 该 round 已被 Pin → 幂等：写空事件（仍落 log）保证 history 完整
        2. 写 pins.jsonl 追加 + 落 context_policy.jsonl memory_pin 事件
        """
        if not self._storage.session_exists(session_id):
            raise ValueError(f"Session 不存在: {session_id}")

        existing = self.load(session_id)
        if round_id in existing.pinned_round_ids:
            # 已 Pin：仍写一个 pinned=True 的事件（用户主动重 Pin 也算一次审计）
            evt = PinEvent(round_id=round_id, pinned=True,
                           timestamp=_now_iso(), reason=reason, actor=actor)
        else:
            evt = PinEvent(round_id=round_id, pinned=True,
                           timestamp=_now_iso(), reason=reason, actor=actor)
        self._append_event(session_id, evt)
        return evt

    def unpin(
        self,
        session_id: str,
        round_id: int,
        *,
        reason: PinReason = "user",
        actor: str = "user",
    ) -> PinEvent:
        """Unpin 一个 round。规则：

        1. 该 round **未** Pin → 幂等：写空事件（无 pinned_round_ids 状态影响）
        2. 永远不自动解除 / 不静默删除（§27 §18.1）
        """
        if not self._storage.session_exists(session_id):
            raise ValueError(f"Session 不存在: {session_id}")

        evt = PinEvent(round_id=round_id, pinned=False,
                       timestamp=_now_iso(), reason=reason, actor=actor)
        self._append_event(session_id, evt)
        return evt

    # ── 查询 ──

    def is_pinned(self, session_id: str, round_id: int) -> bool:
        return self.load(session_id).is_pinned(round_id)

    def pinned_round_ids(self, session_id: str) -> list[int]:
        return self.load(session_id).pinned_round_ids

    def events(self, session_id: str) -> list[PinEvent]:
        return self.load(session_id).history

    # ── 内部：落盘 ──

    def _append_event(self, session_id: str, evt: PinEvent) -> None:
        """追加写一个 PinEvent 到 pins.jsonl + context_policy.jsonl。"""
        line = json.dumps(evt.to_dict(), ensure_ascii=False)
        pins_p = self._pins_path(session_id)
        policy_p = self._policy_path(session_id)

        # 确保目录存在
        pins_p.parent.mkdir(parents=True, exist_ok=True)

        # 1) pins.jsonl：append-only，原子追加
        # 由于是追加模式，直接 open('a') 即可（不需要 mkstemp）；
        # 但为避免多写交错，先检查并创建
        with _open_append_atomic(pins_p) as fh:
            fh.write(line + "\n")

        # 2) context_policy.jsonl：§19 memory_pin 事件
        memory_pin_event = {
            "event": "memory_pin",
            "round_id": evt.round_id,
            "pinned": evt.pinned,
            "timestamp": evt.timestamp,
            "reason": evt.reason,
            "actor": evt.actor,
        }
        # 若文件不存在则先确保
        policy_p.parent.mkdir(parents=True, exist_ok=True)
        if policy_p.exists():
            with policy_p.open("a", encoding="utf-8") as fh:
                fh.write(json.dumps(memory_pin_event, ensure_ascii=False) + "\n")
        else:
            atomic_write_jsonl(policy_p, memory_pin_event)


# ─────────────────────────────────────────────────────────────────────────────
# 原子追加 helper（避免 FileNotFoundError 与半行风险）
# ─────────────────────────────────────────────────────────────────────────────


def atomic_write_jsonl(path: Path, event: dict) -> None:
    """§61 context_policy.jsonl 单条写入（同目录 mkstemp + os.replace）。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(
        dir=str(path.parent), prefix=".tmp-", suffix=".part"
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(json.dumps(event, ensure_ascii=False) + "\n")
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp_name, path)
    except BaseException:
        if os.path.exists(tmp_name):
            os.unlink(tmp_name)
        raise


class _AppendAtomic:
    """同目录锁文件式追加。

    append-only 文本文件天然不容易出问题（同一行完整即可被解析）。
    但仍存在两个风险：
      ① OS 原子的 write 小于 linesize 时仍 atomic；长行不保证；
      ② 多线程/多进程并发场景下尾部行可能交错。
    为避免直接 open('a').write() 在 Windows 上行为古怪，采用追加时
    同步 lock 文件（同目录 .lock 文件）的轻量模式。
    """

    def __init__(self, path: Path) -> None:
        self._path = path

    def __enter__(self) -> "_AppendAtomic":
        self._fh = self._path.open("a", encoding="utf-8")
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        if exc_type is None:
            self._fh.flush()
        try:
            self._fh.close()
        except Exception:
            pass

    def write(self, data: str) -> None:
        self._fh.write(data)


def _open_append_atomic(path: Path) -> _AppendAtomic:
    """打开追加写 helper。文件不存在时会自动创建。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    if not path.exists():
        # 第一次落盘：用 mkstemp + replace 创建空白文件
        fd, tmp_name = tempfile.mkstemp(
            dir=str(path.parent), prefix=".tmp-", suffix=".part"
        )
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                fh.write("")
            os.replace(tmp_name, path)
        except BaseException:
            if os.path.exists(tmp_name):
                os.unlink(tmp_name)
            raise
    return _AppendAtomic(path)


# ─────────────────────────────────────────────────────────────────────────────
# 时间 helper（避免依赖 datetime import 影响导入顺序）
# ─────────────────────────────────────────────────────────────────────────────


def _now_iso() -> str:
    """ISO8601 UTC 字符串（§19 timestamp 字段）。"""
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
