"""并发 Round 回归测试（问题 1：未等待上一轮完成就发送下一轮）。

诊断结论见报告；本文件只锁定行为契约：

后端（core 层，真实 RoundManager + 人为延迟 stub provider）
    1. 两个 Round 真正并发执行（总耗时 ≈ max，不是 sum）
    2. R1 慢 / R2 快 → R2 可以先完成，R1 之后仍然正确完成
    3. 两个 Round 最终都 complete，state.json 两个 Round 状态都在（不互相覆盖）
    4. 两个 Round 的响应不串台（R1/R2 正文各自独立）
    5. R2 的 prompt 不含 R1 尚未完成的正文

前端（加载真实 static/app.js，DOM / EventSource 打桩）
    6. 连续发送两轮 → 存在两条 SSE 连接，R1 的连接不被 R2 关闭
    7. R1 的 token 事件只写进 R1 的卡片（不串台）
    8. R2 先 done → 只关闭 R2 的连接；R1 继续
    9. 不通过"禁用发送按钮"规避：流仍开着时按钮可用
"""

from __future__ import annotations

import asyncio
import json
import shutil
import subprocess
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from core.model_manager import ModelRegistry
from core.round_manager import RoundManager
from core.session_manager import SessionManager
from core.storage import Storage

ROOT = Path(__file__).resolve().parent.parent
APP_JS = ROOT / "static" / "app.js"


def _node() -> str:
    node = shutil.which("node")
    if node is None:
        pytest.skip("未安装 node，跳过 JS 行为验证")
    return node


# =========================================================================
# A. 后端
# =========================================================================


class DelayedStubProvider:
    """人为延迟的 stub provider（只实现 LLMProvider.generate）。

    按 round_id 设定不同耗时，用于制造「R1 慢 / R2 快」；
    正文带 round_id 标记，用于验证两个 Round 的响应不串台。
    另外记录每次 generate 的 [start, end] 区间 —— 用"区间是否重叠"
    判定并发，避免依赖绝对路径耗时（磁盘慢会让耗时断言误判）。
    """

    def __init__(self, delay_by_round: dict[int, float], spans: dict[int, tuple] | None = None):
        self._delay_by_round = delay_by_round
        self._spans = spans if spans is not None else {}
        # phase1_1 §6.2：usage 由 Provider 提供；这里给最小形状
        self.usage = {"prompt_tokens": 1, "completion_tokens": 3}

    async def generate(self, *, round_id: int, prompt: str, tools=None,
                       max_output_tokens=None):
        start = time.monotonic()
        await asyncio.sleep(self._delay_by_round.get(round_id, 0.0))
        for i in range(3):
            await asyncio.sleep(0.01)
            yield f"[R{round_id}-chunk{i}]"
        self._spans[round_id] = (start, time.monotonic())


@pytest.fixture
def cenv(tmp_path):
    storage = Storage(tmp_path / "sessions")
    registry = ModelRegistry()
    delays: dict[int, float] = {}
    spans: dict[int, tuple] = {}

    def factory(model_id: str) -> DelayedStubProvider:
        return DelayedStubProvider(delays, spans)

    return SimpleNamespace(
        storage=storage,
        registry=registry,
        sessions=SessionManager(storage, registry),
        rounds=RoundManager(storage, registry, factory),
        delays=delays,
        spans=spans,
    )


def _launch(cenv, sid: str, instruction: str):
    """与 app.py 一致：每次 POST /round 都重新 load state 再 prepare。"""
    session = cenv.sessions.get(sid)
    round_state, bus = cenv.rounds.prepare_round(
        session, instruction, selected_models=["qwen"]
    )
    return round_state, bus, cenv.rounds.launch(sid, round_state, bus)


def test_two_rounds_actually_run_concurrently(cenv):
    """R1 / R2 各 0.30s：两段 provider 调用区间必须重叠 = 真并发。

    不用"总耗时 < X"判据 —— 那会被磁盘写 state.json 的速度影响
    （.pytest_tmp 上一次原子写要 ~0.15s，足以让并发看起来像串行）。
    """
    cenv.delays.update({1: 0.30, 2: 0.30})
    session = cenv.sessions.create("并发问题？")
    sid = session.session_id

    async def main():
        r1, _, t1 = _launch(cenv, sid, "第 1 轮")
        r2, _, t2 = _launch(cenv, sid, "第 2 轮")   # 不等待 R1
        await asyncio.gather(t1, t2)
        return r1, r2

    r1, r2 = asyncio.run(main())
    (s1, e1), (s2, e2) = cenv.spans[1], cenv.spans[2]
    overlap = min(e1, e2) - max(s1, s2)
    assert overlap > 0.15, (
        f"两轮被串行执行：R1=[{s1:.3f},{e1:.3f}] R2=[{s2:.3f},{e2:.3f}] "
        f"overlap={overlap:.3f}s"
    )
    assert r1.status["qwen"] == "complete"
    assert r2.status["qwen"] == "complete"


def test_slow_r1_and_fast_r2_both_complete_and_r2_finishes_first(cenv):
    """R1 慢 / R2 快：R2 可以先完成，R1 之后仍然正确完成。"""
    cenv.delays.update({1: 0.50, 2: 0.05})
    session = cenv.sessions.create("并发问题？")
    sid = session.session_id

    async def main():
        r1, bus1, t1 = _launch(cenv, sid, "第 1 轮")
        r2, bus2, t2 = _launch(cenv, sid, "第 2 轮")
        order: list[int] = []

        async def track(rid: int, task):
            await task
            order.append(rid)

        await asyncio.gather(track(1, t1), track(2, t2))
        return order, r1, r2, bus1, bus2

    order, r1, r2, bus1, bus2 = asyncio.run(main())
    assert order == [2, 1], f"快的一轮应先完成：{order}"
    assert r1.status["qwen"] == "complete"
    assert r2.status["qwen"] == "complete"
    # 两个 Round 的事件总线各自收到自己的 done
    assert any(e.get("type") == "done" for e in bus1.events)
    assert any(e.get("type") == "done" for e in bus2.events)


def test_state_keeps_both_rounds_and_responses_do_not_mix(cenv):
    """state.json 两个 Round 都在；R1/R2 正文各自独立、不串台。"""
    cenv.delays.update({1: 0.40, 2: 0.05})
    session = cenv.sessions.create("并发问题？")
    sid = session.session_id

    async def main():
        _, _, t1 = _launch(cenv, sid, "第 1 轮")
        _, _, t2 = _launch(cenv, sid, "第 2 轮")
        await asyncio.gather(t1, t2)

    asyncio.run(main())

    state = cenv.storage.load_state(sid)
    assert [r.round_id for r in state.rounds] == [1, 2]
    assert state.current_round == 2
    assert state.round(1).status["qwen"] == "complete", "R1 状态被 R2 覆盖"
    assert state.round(2).status["qwen"] == "complete"

    a1 = cenv.storage.load_responses(sid, 1)["qwen"]
    a2 = cenv.storage.load_responses(sid, 2)["qwen"]
    assert "[R1-" in a1 and "[R2-" not in a1, f"R1 正文串入 R2：{a1!r}"
    assert "[R2-" in a2 and "[R1-" not in a2, f"R2 正文串入 R1：{a2!r}"

    # R2 建立 prompt 时 R1 尚未完成 → R1 正文不得出现在 R2 的 prompt 里
    p2 = cenv.storage.load_prompt(sid, 2, "qwen")
    assert "[R1-" not in p2, "未完成 Round 的正文被注入了下一轮 prompt"


# =========================================================================
# B. 前端（真实 app.js + 打桩 DOM / EventSource）
# =========================================================================


def _harness() -> str:
    return r"""
    const fs = require('fs');
    const src = fs.readFileSync('static/app.js', 'utf8');

    const created = [];
    const byId = {};
    const ES = [];       // 所有 EventSource 实例
    const REQS = [];
    const LS = {};

    function mkEl(tag) {
      const cls = new Set();
      const qs = new Map();     // 同一节点同一选择器返回同一子节点（可断言写入位置）
      const node = {
        tagName: tag, id: '', value: '', checked: false, hidden: false,
        textContent: '', innerHTML: '', className: '', type: '',
        dataset: {}, style: {}, children: [],
        classList: {
          add: (c) => cls.add(c), remove: (c) => cls.delete(c),
          toggle: () => {}, contains: (c) => cls.has(c),
        },
        appendChild(c) { node.children.push(c); return c; },
        remove(){}, focus(){},
        querySelector(sel) {
          if (!qs.has(sel)) qs.set(sel, mkEl('div'));
          return qs.get(sel);
        },
        querySelectorAll: () => [],
        addEventListener(){},
      };
      created.push(node);
      return node;
    }

    global.document = {
      getElementById: (id) => {
        if (!byId[id]) { const n = mkEl('div'); n.id = id; byId[id] = n; }
        return byId[id];
      },
      createElement: (tag) => mkEl(tag),
      addEventListener(){},
      querySelectorAll: () => [],
      querySelector: () => mkEl('div'),
    };
    global.localStorage = {
      getItem: (k) => (k in LS ? LS[k] : null),
      setItem: (k, v) => { LS[k] = String(v); },
      removeItem: (k) => { delete LS[k]; },
    };
    global.EventSource = class {
      constructor(url) {
        this.url = url; this.closed = false; this.handlers = {};
        ES.push(this);
      }
      addEventListener(ev, fn) { (this.handlers[ev] = this.handlers[ev] || []).push(fn); }
      close() { this.closed = true; }
      emit(ev, payload) {
        (this.handlers[ev] || []).forEach((fn) => fn(
          payload === undefined ? {} : { data: JSON.stringify(payload) }
        ));
      }
    };
    global.alert = () => {};

    let nextRound = 1;
    global.fetch = async (path) => {
      REQS.push(String(path));
      if (String(path).endsWith('/round')) {
        const rid = nextRound++;
        return { ok: true, json: async () => ({
          round_id: rid, participants: ['qwen'], peer_review: false,
        }) };
      }
      return { ok: true, json: async () => ({}) };
    };

    const api = new Function(
      src + "\nreturn { state, els, sendRound, updateSendState, loadSession };"
    )();
    global.__api = api;
    global.__ES = ES;
    global.__byId = byId;
    global.__REQS = REQS;
    """


def _run(script_tail: str) -> dict:
    proc = subprocess.run(
        [_node(), "-e", _harness() + script_tail],
        capture_output=True,
        text=True,
        cwd=str(ROOT),
        timeout=60,
    )
    assert proc.returncode == 0, proc.stderr
    return json.loads(proc.stdout)


def test_sending_round2_does_not_close_round1_stream():
    """连续发送两轮：两条 SSE 同时存在，R1 的连接不被 R2 掐断。"""
    out = _run(r"""
    (async () => {
      __api.state.sessionId = 'sid-x';
      __api.state.models = [{ id: 'qwen', displayName: 'Q',
                              selected: true, active: true }];
      await __api.sendRound();                 // R1（仍在生成）
      const afterFirst = {
        streams: __api.state.streams.size,
        btnDisabled: __api.els.btnSend.disabled,   // 不得靠禁用按钮规避
      };
      await __api.sendRound();                 // 不等待 R1 → R2
      process.stdout.write(JSON.stringify({
        afterFirst,
        urls: __ES.map((e) => e.url),
        closedFlags: __ES.map((e) => e.closed),
        streams: __api.state.streams.size,
      }));
    })();
    """)
    assert out["afterFirst"]["streams"] == 1
    assert out["afterFirst"]["btnDisabled"] is False, "不得用禁用发送按钮规避并发"
    assert out["urls"][0].endswith("/events/1"), out["urls"]
    assert out["urls"][1].endswith("/events/2"), out["urls"]
    assert out["closedFlags"] == [False, False], "R2 发送后 R1 的 SSE 被关闭"
    assert out["streams"] == 2


def test_events_are_routed_to_their_own_round_card():
    """R1 的 token 只写进 R1 的卡片；R2 的 token 只写进 R2 的卡片。"""
    out = _run(r"""
    (async () => {
      __api.state.sessionId = 'sid-x';
      __api.state.models = [{ id: 'qwen', displayName: 'Q',
                              selected: true, active: true }];
      await __api.sendRound();
      await __api.sendRound();
      __ES[0].emit('token', { model: 'qwen', type: 'token', content: 'R1TOKEN' });
      __ES[1].emit('token', { model: 'qwen', type: 'token', content: 'R2TOKEN' });
      const shells = __byId['rounds'].children;
      const body = (sh) => sh.querySelector('.card[data-model="qwen"]')
                              .querySelector('.card-body').textContent;
      process.stdout.write(JSON.stringify({
        card1: body(shells[0]),
        card2: body(shells[1]),
      }));
    })();
    """)
    assert out["card1"] == "R1TOKEN", f"R1 事件写错位置：{out}"
    assert out["card2"] == "R2TOKEN", f"R2 事件写错位置：{out}"


# ── 失败态不得残留「等待输出…」占位 ──────────────────────────────────────
# style.css 对空的 .card-body 会显示「等待输出…」占位；失败模型本就没有正文，
# 因此失败终态必须写入明确文案，否则会是「失败」徽标 + 等待占位的自相矛盾画面。


def test_error_event_writes_explicit_failure_text():
    """实时 SSE error 且后端未给错误详情 → 正文必须是明确失败文案。"""
    out = _run(r"""
    (async () => {
      __api.state.sessionId = 'sid-x';
      __api.state.models = [{ id: 'qwen', displayName: 'Q',
                              selected: true, active: true }];
      await __api.sendRound();
      // payload.error 缺失：既不能留空，也不得落下 "[错误] undefined"
      __ES[0].emit('error', { model: 'qwen', type: 'error' });
      const card = __byId['rounds'].children[0]
                     .querySelector('.card[data-model="qwen"]');
      process.stdout.write(JSON.stringify({
        body: card.querySelector('.card-body').textContent,
        badge: card.querySelector('.badge').textContent,
      }));
    })();
    """)
    assert out["badge"] == "失败", out
    assert out["body"].strip(), "失败态正文不得为空（否则 CSS :empty 会显示「等待输出…」）"
    assert "等待输出" not in out["body"], out["body"]
    assert "未能生成回答" in out["body"], out["body"]


def test_history_replay_error_card_has_failure_text():
    """回放历史会话：status=error 且后端未保存正文 → 正文必须是明确失败文案。"""
    out = _run(r"""
    const SESSION = {
      state: { original_question: 'Q', active_models: ['qwen'], closed_models: [] },
      rounds: [{ round_id: 1, participants: ['qwen'], user_instruction: 'I',
                 peer_review: false, status: { qwen: 'error' },
                 responses: { qwen: '' } }],
      pins: [],
    };
    (async () => {
      global.fetch = async (path) => {
        if (/\/api\/session\/[^/]+$/.test(String(path))) {
          return { ok: true, json: async () => SESSION };
        }
        return { ok: true, json: async () => ({}) };
      };
      await __api.loadSession('sid-x');
      const shell = __byId['rounds'].children[0];
      const grid = shell.querySelector('.grid');
      const card = grid.children[0];
      process.stdout.write(JSON.stringify({
        cards: grid.children.length,
        body: card.querySelector('.card-body').textContent,
      }));
    })();
    """)
    assert out["cards"] == 1, out
    assert out["body"].strip(), "回放的失败卡片正文不得为空"
    assert "等待输出" not in out["body"], out["body"]
    assert "未能生成回答" in out["body"], out["body"]


def test_round2_done_closes_only_its_own_stream():
    """R2 先完成 → 只关 R2；R1 继续，之后完成时再关自己。"""
    out = _run(r"""
    (async () => {
      __api.state.sessionId = 'sid-x';
      __api.state.models = [{ id: 'qwen', displayName: 'Q',
                              selected: true, active: true }];
      await __api.sendRound();
      await __api.sendRound();
      __ES[1].emit('done', { model: 'qwen', type: 'done' });   // R2 先完成
      const afterR2 = {
        closed: __ES.map((e) => e.closed),
        streams: __api.state.streams.size,
      };
      __ES[0].emit('done', { model: 'qwen', type: 'done' });   // R1 后完成
      const afterR1 = {
        closed: __ES.map((e) => e.closed),
        streams: __api.state.streams.size,
      };
      process.stdout.write(JSON.stringify({ afterR2, afterR1 }));
    })();
    """)
    assert out["afterR2"]["closed"] == [False, True]
    assert out["afterR2"]["streams"] == 1
    assert out["afterR1"]["closed"] == [True, True]
    assert out["afterR1"]["streams"] == 0


def test_connect_events_does_not_close_other_streams():
    """契约：connectEvents() 内不得再调用 closeEventStream()（反向证明）。"""
    js = APP_JS.read_text(encoding="utf-8")
    start = js.index("function connectEvents(")
    end = js.index("// ---------- 初始化 ----------")
    body = js[start:end]
    assert "closeEventStream()" not in body, "connectEvents 又去关别的流了"
    assert "state.streams.set(roundId, es)" in body
