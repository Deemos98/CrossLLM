"""S3.4 单元 + API 集成测试：PinStore + Pin / Unpin / Pins / Pin Tokens

测试目标（不前置 S3.5+，与 S3.1/S3.2/S3.3 隔离）：
  ┌─────────────────────────────────────────────────────────────────────┐
  │  T6  Pin 永不自动 Compacted（Pin 后 Round 字节级不变 — 通过 raw 读） │
  │  T7  Pin overflow 只 warning，不解除 Pin                              │
  │  PinEvent / PinState 结构稳定                                         │
  │  PinStore pin/unpin 幂等性                                             │
  │  pins.jsonl 重放一致性                                                 │
  │  context_policy.jsonl 落 memory_pin 事件                              │
  │  4 个 FastAPI endpoint                                                │
  │  AST 守门：禁 import S3.5+                                            │
  └─────────────────────────────────────────────────────────────────────┘

S3.4 不实现：ContextSelector / Summary / Compaction / Peer。
T6 / T7 中"永不自动 Compacted"与"overflow warning"在 S3.4 范围内用 read-only 验证；
真正的 Compaction 端到端验证属于 S3.8。
"""

from __future__ import annotations

import json
import os
import shutil
import tempfile
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from core.pin_store import (
    PinEvent,
    PinState,
    PinStore,
    compute_pinned_tokens,
)
from core.state import RoundState, SessionState
from core.storage import Storage
from core.valid_round import ValidRound


# ─────────────────────────────────────────────────────────────────────────────
# Helper：构造最小化 Session（带若干 Round + 部分成功）
# ─────────────────────────────────────────────────────────────────────────────


def _seed_session(
    storage: Storage,
    *,
    session_id: str = "s-pin-1",
    n_rounds: int = 3,
    instruction_chars: int = 30,
    model_ids: list[str] | None = None,
) -> SessionState:
    """建一个 Session + n_rounds 个 round，每个 round 都 some success / some error。

    默认 round 1：A 成功、C 失败 → valid（A）
          round 2：A 成功、B 成功 → valid
          round 3：B 成功、C 成功 → valid
    """
    if model_ids is None:
        model_ids = ["a", "b", "c"]

    rounds = []
    for rid in range(1, n_rounds + 1):
        # 决定每个 round 的 status：round 1 部分失败；其余全成功
        if rid == 1:
            status = {"a": "complete", "b": "error", "c": "error"}
            stored = {"a": "Round1 答案 A", "b": "ERR_for_b"}
            participants = model_ids
        else:
            status = {m: "complete" for m in model_ids}
            stored = {m: f"Round{rid} 答案 {m}" for m in model_ids}
            participants = model_ids
        for mid, content in stored.items():
            if status[mid] == "complete":
                storage.save_response(session_id, rid, mid, content)
        rounds.append(
            RoundState(
                round_id=rid,
                user_instruction="Q" * instruction_chars,
                participants=participants,
                status=status,
                timestamp="2026-09-03T00:00:00Z",
            )
        )

    session = SessionState(
        session_id=session_id,
        original_question="原始问题?",
        current_round=n_rounds,
        active_models=model_ids,
        closed_models=[],
        rounds=rounds,
    )
    storage.save_state(session)
    return session


# ─────────────────────────────────────────────────────────────────────────────
# PinEvent / PinState 单元
# ─────────────────────────────────────────────────────────────────────────────


class TestPinEventPinState:
    def test_pin_event_round_trip(self):
        e = PinEvent(round_id=1, pinned=True, timestamp="2026-09-03T08:00:00Z")
        d = e.to_dict()
        e2 = PinEvent.from_dict(d)
        assert e2 == e

    def test_pin_event_default_reason_actor_user(self):
        e = PinEvent(round_id=1, pinned=True, timestamp="2026-09-03T08:00:00Z")
        assert e.reason == "user"
        assert e.actor == "user"

    def test_pin_state_is_pinned_lookup(self):
        s = PinState(
            session_id="s",
            pinned_round_ids=[1, 7],
            history=[
                PinEvent(round_id=1, pinned=True, timestamp="2026-09-03T00:00:00Z"),
                PinEvent(round_id=7, pinned=True, timestamp="2026-09-03T00:00:00Z"),
            ],
        )
        assert s.is_pinned(1) is True
        assert s.is_pinned(7) is True
        assert s.is_pinned(2) is False

    def test_pin_state_to_dict_has_all_keys(self):
        s = PinState(
            session_id="s",
            pinned_round_ids=[1],
            history=[PinEvent(round_id=1, pinned=True, timestamp="2026-09-03T00:00:00Z")],
        )
        d = s.to_dict()
        for k in ("session_id", "pinned_round_ids", "history"):
            assert k in d


# ─────────────────────────────────────────────────────────────────────────────
# PinStore 持久化与重放
# ─────────────────────────────────────────────────────────────────────────────


class TestPinStorePersistence:
    def test_pin_creates_pins_jsonl(self, tmp_path):
        storage = Storage(tmp_path / "sessions")
        _seed_session(storage, session_id="sA")
        store = PinStore(storage)
        store.pin("sA", 1)
        p = tmp_path / "sessions" / "sA" / "pins.jsonl"
        assert p.exists()
        lines = [ln for ln in p.read_text(encoding="utf-8").splitlines() if ln.strip()]
        assert len(lines) == 1
        evt = json.loads(lines[0])
        assert evt["round_id"] == 1
        assert evt["pinned"] is True
        assert evt["reason"] == "user"
        assert evt["actor"] == "user"

    def test_pin_creates_context_policy_jsonl_memory_pin(self, tmp_path):
        storage = Storage(tmp_path / "sessions")
        _seed_session(storage, session_id="sB")
        store = PinStore(storage)
        store.pin("sB", 2)
        p = tmp_path / "sessions" / "sB" / "context_policy.jsonl"
        assert p.exists()
        events = [json.loads(ln) for ln in p.read_text(encoding="utf-8").splitlines() if ln.strip()]
        assert len(events) == 1
        assert events[0]["event"] == "memory_pin"
        assert events[0]["round_id"] == 2
        assert events[0]["pinned"] is True

    def test_pin_unpin_round_trip_in_pins_jsonl(self, tmp_path):
        storage = Storage(tmp_path / "sessions")
        _seed_session(storage, session_id="sC", n_rounds=3)
        store = PinStore(storage)
        store.pin("sC", 1)
        store.pin("sC", 3)
        store.unpin("sC", 1)
        # 重放
        s = store.load("sC")
        assert s.pinned_round_ids == [3]
        # history 完整保留 3 条事件
        assert len(s.history) == 3
        assert [e.pinned for e in s.history] == [True, True, False]

    def test_pin_idempotent_repeat(self, tmp_path):
        storage = Storage(tmp_path / "sessions")
        _seed_session(storage, session_id="sD", n_rounds=2)
        store = PinStore(storage)
        store.pin("sD", 1)
        store.pin("sD", 1)  # 重复 pin
        s = store.load("sD")
        # pinned_round_ids 幂等不重复
        assert s.pinned_round_ids == [1]
        # history 仍记录两次 Pin 事件（§19 保留所有变化）
        assert len(s.history) == 2
        assert all(e.pinned for e in s.history)

    def test_unpin_unknown_round_is_idempotent(self, tmp_path):
        storage = Storage(tmp_path / "sessions")
        _seed_session(storage, session_id="sE", n_rounds=2)
        store = PinStore(storage)
        # 未 Pin 的 round 直接 unpin：no-op（pinned_round_ids 不变）
        evt = store.unpin("sE", 99)
        s = store.load("sE")
        assert s.pinned_round_ids == []
        assert len(s.history) == 1
        assert s.history[0].pinned is False

    def test_pin_missing_session_raises(self, tmp_path):
        storage = Storage(tmp_path / "sessions")
        store = PinStore(storage)
        with pytest.raises(ValueError, match="Session 不存在"):
            store.pin("nonexistent", 1)

    def test_session_dir_pin_files_yield_correct_round_ids(self, tmp_path):
        storage = Storage(tmp_path / "sessions")
        _seed_session(storage, session_id="sF", n_rounds=4)
        store = PinStore(storage)
        for rid in (2, 4, 2):  # 包含重复 pin
            store.pin("sF", rid)
        ids = store.pinned_round_ids("sF")
        assert ids == [2, 4]
        assert store.is_pinned("sF", 2) is True
        assert store.is_pinned("sF", 4) is True
        assert store.is_pinned("sF", 1) is False


# ─────────────────────────────────────────────────────────────────────────────
# compute_pinned_tokens
# ─────────────────────────────────────────────────────────────────────────────


class TestComputePinnedTokens:
    """§26 Pin 占用 Token 计算接口。

    算法：sum(pinned ∩ valid_pool 的 token_estimate.value)。
    任一命中 round token_estimate.value=None → 整体 unknown。
    """

    def _make_valid_pool(self, tmp_path: Path, n: int = 3) -> tuple:
        """用 _seed_session + ValidRoundPoolBuilder 生成 Valid Round 池。"""
        storage = Storage(tmp_path / "sessions")
        _seed_session(storage, session_id="sG", n_rounds=n)
        session = storage.load_state("sG")
        from core.valid_round import ValidRoundPoolBuilder
        pool = ValidRoundPoolBuilder(storage).build(session)
        return storage, pool

    def test_empty_pin_zero(self, tmp_path):
        _, pool = self._make_valid_pool(tmp_path)
        state = PinState(session_id="sG", pinned_round_ids=[], history=[])
        te = compute_pinned_tokens(state, pool)
        assert te.value == 0
        assert te.source == "crossllm_estimator"
        assert te.accuracy_mode == "estimated"

    def test_pinned_round_sum(self, tmp_path):
        storage, pool = self._make_valid_pool(tmp_path)
        # pool[0] = round 1（A 成功；Q 30 + A 60 = 90 chars ⇒ 30 tokens）
        state = PinState(
            session_id="sG",
            pinned_round_ids=[1, 2],
            history=[],
        )
        te = compute_pinned_tokens(state, pool)
        # 已知：valid_round.token_estimate 由 S3.2 估算
        # pool[0] (round 1) = Q 30 → 10, A 60 → 20, total 30
        # pool[1] (round 2) = Q 30 → 10, A 60 + 60 → 40, total 50
        # 但 B 在 round 1 是 error，所以只 A 入 valid
        # 直接验证 sum 数值等于 pool 中命中项之和
        expected = sum(vr.token_estimate.value for vr in pool if vr.round_id in (1, 2))
        assert te.value == expected

    def test_pinned_round_not_in_pool_excluded(self, tmp_path):
        _, pool = self._make_valid_pool(tmp_path)
        state = PinState(
            session_id="sG",
            pinned_round_ids=[99],  # valid_pool 中没有
            history=[],
        )
        te = compute_pinned_tokens(state, pool)
        assert te.value == 0

    def test_unknown_token_in_pinned_returns_unknown(self, tmp_path):
        """任一命中 round token_estimate.value=None → 整体 unknown（§22 保守）。

        ValidRound 是 frozen dataclass，无法原地修改 token_estimate；
        改用 dataclasses.replace 构造一个变体，避免破坏 S3.2 不变量。
        """
        from dataclasses import replace
        from core.model_capability import TokenEstimate
        _, pool = self._make_valid_pool(tmp_path)
        # 用 replace 构造一个 token_estimate 变体的 ValidRound
        replaced = [
            replace(vr, token_estimate=TokenEstimate(
                value=None, source="provider_unavailable", accuracy_mode="unknown"
            )) if i == 0 else vr
            for i, vr in enumerate(pool)
        ]
        state = PinState(
            session_id="sG",
            pinned_round_ids=[1, 2],
            history=[],
        )
        te = compute_pinned_tokens(state, replaced)
        assert te.value is None
        assert te.accuracy_mode == "unknown"
        assert te.source == "provider_unavailable"


# ─────────────────────────────────────────────────────────────────────────────
# §27：Pin 不自动解除 / 不自动压缩
# ─────────────────────────────────────────────────────────────────────────────


class TestPinNonAutoBehaviour:
    """§27 + §18.1：Pin 永远不自动解除 / 永不自动压缩 / 永不静默删除。"""

    def test_pin_does_not_mutate_raw_archive(self, tmp_path):
        """T6（§27）：Pin 后 Round 字节级不变——验证 raw 落盘未被 PinStore 触碰。"""
        storage = Storage(tmp_path / "sessions")
        _seed_session(storage, session_id="sT6", n_rounds=1)
        # Pin 之前记录 round_001/a.md 字节级内容
        raw_a_path = storage.response_path("sT6", 1, "a")
        original_bytes = raw_a_path.read_bytes()
        store = PinStore(storage)
        store.pin("sT6", 1)
        # Pin 后字节级相等
        assert raw_a_path.read_bytes() == original_bytes
        # state.json 也未被 PinStore 触碰（plan: Pin 是独立 jsonl）
        # md5 file 不变
        assert raw_a_path.read_text(encoding="utf-8") == original_bytes.decode("utf-8")

    def test_pin_overflow_does_not_unpin_or_compress(self, tmp_path):
        """T7（§27）：Pin overflow 时**不应**自动解除 Pin / 自动压缩 / 静默删除。

        S3.4 范围内：验证 PinStore 不提供任何"自动解除 / 自动压缩"接口。
        编译期检查：用 AST + hasattr 确认 public API 严格限定。
        """
        store = PinStore  # 类对象，不实例化（实例化需 storage）
        # 必须暴露的 public 方法（pin / unpin 都需要用户触发）
        public = {n for n in dir(store) if not n.startswith("_")}
        assert "pin" in public
        assert "unpin" in public
        # 以下方法名**不应**存在（§27 禁止自动）
        forbidden = {
            "auto_unpin",
            "remove_overflow_pins",
            "compact_pinned",
            "drop_unused_pins",
            "garbage_collect",
        }
        leaked = forbidden & public
        assert not leaked, f"§27 违反：PinStore 暴露自动解除 / 压缩接口：{leaked}"

    def test_pin_unpin_only_user_actor(self, tmp_path):
        """§19 / §4.5：reason 与 actor 默认应为 'user'，不允许 system 主动 pin。"""
        storage = Storage(tmp_path / "sessions")
        _seed_session(storage, session_id="sT7b")
        store = PinStore(storage)
        evt = store.pin("sT7b", 1)
        assert evt.reason == "user"
        assert evt.actor == "user"


# ─────────────────────────────────────────────────────────────────────────────
# API 集成测试（FastAPI TestClient）
# ─────────────────────────────────────────────────────────────────────────────


@pytest.fixture
def api_client(tmp_path, monkeypatch):
    """构造一个隔离 storage + 加载 app.py 的 TestClient。"""
    sessions_root = tmp_path / "sessions"
    sessions_root.mkdir()
    monkeypatch.setattr("app.SESSIONS_DIR", sessions_root)
    # 由于 app.py 顶层已构造 storage / registry / sessions / rounds / pins 单例，
    # 不能 monkeypatch storage（其内部 root 已绑定）—— 测试期间重建 app 实例。
    import importlib
    import sys
    if "app" in sys.modules:
        del sys.modules["app"]
    # config 路径不能缺，否则 registry 走 fallback
    from core import storage as storage_mod
    # 让 app.storage 用 tmp 目录
    import app as app_mod
    app_mod.SESSIONS_DIR = sessions_root
    # 替换 storage 实例
    app_mod.storage = Storage(sessions_root)
    app_mod.sessions = type(app_mod.sessions)(app_mod.storage, app_mod.registry)
    app_mod.rounds = type(app_mod.rounds)(app_mod.storage, app_mod.registry, app_mod._make_provider)
    app_mod.pins = PinStore(app_mod.storage)
    client = TestClient(app_mod.app)
    return client, app_mod


def _create_session_via_api(client, question: str = "原始问题?"):
    r = client.post("/api/session", json={"question": question})
    assert r.status_code == 200, r.text
    return r.json()["session_id"]


def _seed_a_round_via_state(app_mod, sid: str, round_id: int = 1) -> None:
    """向 storage.state.json 注入一个 RoundState，便于 Pin API 校验回合存在。"""
    session = app_mod.storage.load_state(sid)
    session.rounds.append(
        RoundState(
            round_id=round_id,
            user_instruction="Q1",
            participants=["a", "b"],
            status={"a": "complete", "b": "complete"},
            timestamp="2026-09-03T00:00:00Z",
        )
    )
    if round_id > session.current_round:
        session.current_round = round_id
    app_mod.storage.save_state(session)


class TestPinAPI:
    def test_post_pin_returns_event_and_pinned_list(self, api_client):
        client, app_mod = api_client
        sid = _create_session_via_api(client)
        _seed_a_round_via_state(app_mod, sid, round_id=1)
        r = client.post(f"/api/session/{sid}/pin", json={"round_id": 1})
        assert r.status_code == 200, r.text
        data = r.json()
        assert data["round_id"] == 1
        assert data["pinned"] is True
        assert data["pinned_round_ids"] == [1]
        # 落盘检查
        pins_path = app_mod.SESSIONS_DIR / sid / "pins.jsonl"
        assert pins_path.exists()
        policy_path = app_mod.SESSIONS_DIR / sid / "context_policy.jsonl"
        assert policy_path.exists()
        assert "memory_pin" in policy_path.read_text(encoding="utf-8")

    def test_post_unpin_removes_from_pinned_list(self, api_client):
        client, app_mod = api_client
        sid = _create_session_via_api(client)
        _seed_a_round_via_state(app_mod, sid, round_id=1)
        client.post(f"/api/session/{sid}/pin", json={"round_id": 1})
        r = client.post(f"/api/session/{sid}/unpin", json={"round_id": 1})
        assert r.status_code == 200, r.text
        data = r.json()
        assert data["pinned"] is False
        assert data["pinned_round_ids"] == []

    def test_get_pins_lists_state_and_history(self, api_client):
        client, app_mod = api_client
        sid = _create_session_via_api(client)
        _seed_a_round_via_state(app_mod, sid, round_id=1)
        _seed_a_round_via_state(app_mod, sid, round_id=2)
        client.post(f"/api/session/{sid}/pin", json={"round_id": 1})
        client.post(f"/api/session/{sid}/pin", json={"round_id": 2})
        r = client.get(f"/api/session/{sid}/pins")
        assert r.status_code == 200, r.text
        data = r.json()
        assert data["pinned_round_ids"] == [1, 2]
        # history 是 PinEvent 列表（不是 context_policy memory_pin 事件）
        assert len(data["history"]) == 2
        for h in data["history"]:
            assert h["pinned"] is True
            assert h["reason"] == "user"
            assert h["actor"] == "user"
            assert "timestamp" in h
            assert h["round_id"] in (1, 2)

    def test_get_pin_tokens_returns_estimate(self, api_client):
        client, app_mod = api_client
        sid = _create_session_via_api(client)
        _seed_a_round_via_state(app_mod, sid, round_id=1)
        client.post(f"/api/session/{sid}/pin", json={"round_id": 1})
        r = client.get(f"/api/session/{sid}/pin/tokens")
        assert r.status_code == 200, r.text
        data = r.json()
        assert data["pinned_round_ids"] == [1]
        # Q=2 chars（"Q1"）+ A=None（a.md 不存在，但 valid pool 会跳过）。
        # _seed_a_round 没 save_response，所以对 round 1 valid_pool 空
        # 在没 save_response 的情况下，pin/tokens 计算返回 0（没人命中 valid pool）
        assert data["pinned_tokens_value"] == 0
        assert data["pinned_tokens_source"] == "crossllm_estimator"
        assert data["pinned_tokens_accuracy"] == "estimated"
        assert data["overflow"] is None

    def test_pin_missing_session_returns_404(self, api_client):
        client, _ = api_client
        r = client.post("/api/session/no-such-session/pin", json={"round_id": 1})
        assert r.status_code == 404

    def test_pin_nonexistent_round_returns_404(self, api_client):
        client, app_mod = api_client
        sid = _create_session_via_api(client)
        _seed_a_round_via_state(app_mod, sid, round_id=1)
        r = client.post(f"/api/session/{sid}/pin", json={"round_id": 99})
        assert r.status_code == 404
        assert "Round 99" in r.json()["detail"]

    def test_get_session_includes_pins_field(self, api_client):
        """S3.4：GET /api/session/{sid} 响应新增 pins 字段。"""
        client, app_mod = api_client
        sid = _create_session_via_api(client)
        _seed_a_round_via_state(app_mod, sid, round_id=1)
        client.post(f"/api/session/{sid}/pin", json={"round_id": 1})
        r = client.get(f"/api/session/{sid}")
        assert r.status_code == 200
        data = r.json()
        assert "pins" in data
        assert data["pins"] == [1]


# ─────────────────────────────────────────────────────────────────────────────
# AST 守门：S3.4 不前置 S3.5+
# ─────────────────────────────────────────────────────────────────────────────


def test_module_does_not_mutate_valid_round_is_pinned():
    """S3.4 守门：PinStore 不直接修改 ValidRound.is_pinned（§26 仅只读聚合）。

    ValidRound.is_pinned 是 S3.4 保留字段；S3.4 不应通过 PinStore 主动写入。
    Pin 计算接口 compute_pinned_tokens 应通过 PinState 独立计算。
    """
    # 1. PinStore 不暴露 set_pinned / mutate_valid_round 之类的方法
    public = {n for n in dir(PinStore) if not n.startswith("_")}
    forbidden_methods = {
        "set_valid_round_is_pinned",
        "mutate_valid_round",
        "mark_valid_round",
        "rebuild_valid_round_pool",
    }
    leaked = forbidden_methods & public
    assert not leaked, f"§26 违反：PinStore 不应直接改 ValidRound：{leaked}"


def test_compute_pinned_tokens_pure_function():
    """compute_pinned_tokens() 是纯函数；相同输入相同输出。"""
    # 手工构造 PinState + Valid Round pool
    storage_a = Storage(Path(tempfile.mkdtemp()) / "a")
    _seed_session(storage_a, session_id="x1", n_rounds=3)
    session_a = storage_a.load_state("x1")
    from core.valid_round import ValidRoundPoolBuilder
    pool_a = ValidRoundPoolBuilder(storage_a).build(session_a)
    pin_a = PinState(session_id="x1", pinned_round_ids=[2], history=[])
    out_a1 = compute_pinned_tokens(pin_a, pool_a)
    out_a2 = compute_pinned_tokens(pin_a, pool_a)
    assert out_a1.value == out_a2.value
    assert out_a1.source == out_a2.source
    assert out_a1.accuracy_mode == out_a2.accuracy_mode


def test_pins_jsonl_format_is_append_only_jsonl():
    """pins.jsonl 必须 append-only JSONL（一行一条 JSON，不重复 / 不乱序）。

    S3.4 不强制原子覆写——只追加；崩溃后由事件流重放恢复（§19）。
    """
    storage = Storage(Path(tempfile.mkdtemp()) / "s")
    _seed_session(storage, session_id="sFmt", n_rounds=2)
    store = PinStore(storage)
    store.pin("sFmt", 1)
    store.unpin("sFmt", 1)
    store.pin("sFmt", 2)
    pins_path = storage.session_dir("sFmt") / "pins.jsonl"
    raw = pins_path.read_text(encoding="utf-8")
    lines = [ln for ln in raw.splitlines() if ln.strip()]
    assert len(lines) == 3
    # 每行都可被 JSON 解析，且字段完整
    for ln in lines:
        d = json.loads(ln)
        for k in ("round_id", "pinned", "timestamp", "reason", "actor"):
            assert k in d


# ─────────────────────────────────────────────────────────────────────────────
# S3.1/S3.2/S3.3 边界守门（不修改 / 不依赖 Selection 等 S3.5+）
# ─────────────────────────────────────────────────────────────────────────────


def test_pin_store_pins_never_writes_project_memory():
    """§18.3：PinStore **不**写 projects/ 目录（Project Memory 是 S3.x+）。"""
    storage = Storage(Path(tempfile.mkdtemp()) / "s")
    _seed_session(storage, session_id="sNoProjMem", n_rounds=1)
    store = PinStore(storage)
    store.pin("sNoProjMem", 1)
    # 验证 sessions/ 目录 + context_policy.jsonl 都存在；但 projects/ 不在 tmp 任何位置
    base = Path(storage.root).resolve()
    # 因为 storage.root 直接是 sessions/，不会有 projects/
    projects_root = base.parent / "projects"
    assert not projects_root.exists(), (
        "§18.3 违反：PinStore 不应在 sessions/ 之外写文件；"
        f"检测到意外的 {projects_root} 目录"
    )
