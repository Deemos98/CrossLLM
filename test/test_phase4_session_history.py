"""Session History 最小 UI（只读导航）测试。

目标只有一个：让用户看到已持久化的历史 Session，并点击切换到任意历史 Session。

明确**不是** Project Memory M3，不做检索 / 上下文注入 / 向量化 / 记忆搜索。
本文件只测两件事：

后端（新增只读 API）
    1. GET /api/sessions 返回多个 Session 摘要
    2. 按 updated_at 倒序
    3. 无效 / 损坏的单个 Session 不导致整体失败
    4. 纯读：不写任何 Session 数据（反向证明）

前端（新增历史列表 + 切换）
    5. 点击历史 Session 能加载该 Session
    6. 切换 Session 会关闭旧 SSE
    7. 切换后 localStorage 指向新 Session
    8. 新建 Session 后列表刷新并高亮新 Session
    9. 反向证明：发送 Round 不会碰历史列表 API（不做任何注入）
"""

from __future__ import annotations

import hashlib
import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest

STATIC = Path(__file__).resolve().parent.parent / "static"
INDEX_HTML = STATIC / "index.html"
APP_JS = STATIC / "app.js"
ROOT = Path(__file__).resolve().parent.parent

# 本阶段新区块的起点标记（守门只扫这段代码，不影响文件其余部分）
JS_BLOCK_MARKER = "// ---------- Session History（只读导航）----------"


def _node() -> str:
    node = shutil.which("node")
    if node is None:
        pytest.skip("未安装 node，跳过 JS 行为验证")
    return node


# =========================================================================
# A. 后端：GET /api/sessions（只读）
# =========================================================================


@pytest.fixture
def api(tmp_path, monkeypatch):
    """隔离 storage 的 TestClient（只替换 app.sessions，不重建整个 app）。"""
    from fastapi.testclient import TestClient

    from core.session_manager import SessionManager
    from core.storage import Storage

    import app as app_module

    root = tmp_path / "sessions"
    root.mkdir()
    storage = Storage(root)
    monkeypatch.setattr(
        app_module, "sessions", SessionManager(storage, app_module.registry), raising=False
    )
    return TestClient(app_module.app), app_module, storage


def _create(client, question: str) -> str:
    r = client.post("/api/session", json={"question": question})
    assert r.status_code == 200, r.text
    return r.json()["session_id"]


def _set_updated_at(storage, sid: str, updated_at: str) -> None:
    state = storage.load_state(sid)
    state.updated_at = updated_at
    storage.save_state(state)


def test_api_sessions_lists_multiple_sessions(api):
    """需求 1：返回多个 Session 摘要，字段齐全。"""
    client, _, _ = api
    ids = [_create(client, f"问题 {i}") for i in range(3)]

    r = client.get("/api/sessions")
    assert r.status_code == 200, r.text
    data = r.json()
    assert isinstance(data, list), f"应直接返回数组，实际 {type(data)}"
    assert len(data) == 3
    assert {d["session_id"] for d in data} == set(ids)
    for d in data:
        for field in ("session_id", "created_at", "updated_at",
                      "original_question", "current_round"):
            assert field in d, f"摘要缺少字段 {field}"


def test_api_sessions_sorted_by_updated_at_desc(api):
    """需求 2：按 updated_at 倒序（最新在前）。"""
    client, _, storage = api
    a = _create(client, "最早")
    b = _create(client, "中间")
    c = _create(client, "最新")
    _set_updated_at(storage, a, "2026-01-01T10:00:00+08:00")
    _set_updated_at(storage, c, "2026-03-01T10:00:00+08:00")
    _set_updated_at(storage, b, "2026-02-01T10:00:00+08:00")

    data = client.get("/api/sessions").json()
    assert [d["session_id"] for d in data] == [c, b, a]
    ups = [d["updated_at"] for d in data]
    assert ups == sorted(ups, reverse=True)


def test_api_sessions_skips_invalid_sessions(api):
    """需求 3：无效 / 损坏的单个 Session 不影响整体列表。"""
    client, _, storage = api
    good = _create(client, "好的 Session")
    root = storage.root

    (root / "broken-1").mkdir()
    (root / "broken-1" / "state.json").write_text("{ 这不是 JSON", encoding="utf-8")
    (root / "broken-2").mkdir()                       # 空目录：无 state.json
    (root / "stray.txt").write_text("噪声", encoding="utf-8")

    r = client.get("/api/sessions")
    assert r.status_code == 200, r.text
    data = r.json()
    assert [d["session_id"] for d in data] == [good], f"损坏项应被跳过：{data}"


def test_api_sessions_empty_when_no_sessions(api):
    client, _, _ = api
    assert client.get("/api/sessions").json() == []


def test_api_sessions_is_read_only(api):
    """需求 4（反向证明）：列表接口不写入任何 Session 数据。"""
    client, _, storage = api
    _create(client, "原始问题 A")

    def snapshot() -> dict[str, str]:
        out = {}
        for p in sorted(storage.root.rglob("*")):
            if p.is_file():
                out[str(p.relative_to(storage.root))] = hashlib.md5(
                    p.read_bytes()
                ).hexdigest()
        return out

    before = snapshot()
    assert before, "应至少有 state.json"
    client.get("/api/sessions")
    client.get("/api/sessions")
    assert snapshot() == before, "GET /api/sessions 不得改动任何文件"


# =========================================================================
# B. 前端：历史列表 + 切换（加载真实 app.js，DOM / fetch 打桩）
# =========================================================================

SESSIONS = [
    {
        "session_id": "s-old",
        "created_at": "2026-09-01T10:00:00+08:00",
        "updated_at": "2026-09-01T10:05:00+08:00",
        "original_question": "旧问题：OCR 架构是否合理？",
        "current_round": 3,
    },
    {
        "session_id": "s-mid",
        "created_at": "2026-09-10T10:00:00+08:00",
        "updated_at": "2026-09-10T11:00:00+08:00",
        "original_question": "缓存策略如何取舍？",
        "current_round": 1,
    },
]

NEW_SESSION_ID = "s-new"


def _harness() -> str:
    return r"""
    const fs = require('fs');
    const src = fs.readFileSync('static/app.js', 'utf8');
    const SESSIONS = __SESSIONS__;
    const NEW_SESSION_ID = __NEW_ID__;

    const REQS = [];      // 捕获所有请求
    const LS = {};        // localStorage 打桩
    const ES = [];        // 所有 EventSource 实例
    const byId = {};
    const created = [];
    const listeners = new Map();

    function mkEl(tag) {
      const cls = new Set();
      const node = {
        tagName: tag, id: '', value: '', checked: false, hidden: false,
        textContent: '', innerHTML: '', className: '', type: '',
        dataset: {}, style: {}, children: [], _cls: cls,
        classList: {
          add: (c) => cls.add(c),
          remove: (c) => cls.delete(c),
          toggle: (c, on) => {
            if (on === undefined) { cls.has(c) ? cls.delete(c) : cls.add(c); }
            else { on ? cls.add(c) : cls.delete(c); }
          },
          contains: (c) => cls.has(c),
        },
        appendChild(c) { node.children.push(c); return c; },
        remove(){}, focus(){},
        querySelector: () => mkEl('div'),
        querySelectorAll: () => [],
        addEventListener(ev, fn) {
          if (!listeners.has(node)) listeners.set(node, {});
          listeners.get(node)[ev] = fn;
        },
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
      constructor(url) { this.url = url; this.closed = false; ES.push(this); }
      addEventListener(){}
      close() { this.closed = true; }
    };
    global.alert = () => {};

    const jsonRes = (obj) => ({ ok: true, json: async () => obj });

    global.fetch = async (path, opts) => {
      const p = String(path);
      REQS.push({ path: p, method: (opts && opts.method) || 'GET' });
      if (p === '/api/sessions') return jsonRes(SESSIONS);
      if (p === '/api/session') {
        const body = opts && opts.body ? JSON.parse(opts.body) : {};
        const entry = {
          session_id: NEW_SESSION_ID,
          created_at: '2026-09-15T09:00:00+08:00',
          updated_at: '2026-09-15T09:00:00+08:00',
          original_question: body.question || '',
          current_round: 0,
        };
        SESSIONS.unshift(entry);   // 后端创建后自然出现在列表里
        return jsonRes({
          session_id: NEW_SESSION_ID,
          state: { session_id: NEW_SESSION_ID, original_question: body.question || '',
                   active_models: ['qwen'], closed_models: [], current_round: 0 },
        });
      }
      if (p.endsWith('/round')) {
        return jsonRes({ round_id: 9, participants: ['qwen'], peer_review: false });
      }
      if (p.startsWith('/api/session/')) {
        const sid = p.split('/')[3];
        return jsonRes({
          state: { session_id: sid, original_question: '加载回来的问题',
                   active_models: ['qwen'], closed_models: [], current_round: 3 },
          rounds: [], pins: [],
        });
      }
      if (p.includes('/evidence')) return jsonRes({ total: 0, used_count: 0, entries: [] });
      if (p.includes('/memory/candidates')) return jsonRes({ total: 0, candidates: [] });
      if (p.endsWith('/memory')) return jsonRes({ total: 0, entries: [] });
      return jsonRes({});
    };

    const api = new Function(
      src + "\nreturn { state, els, loadSession, switchSession, refreshSessionHistory,"
          + " renderSessionHistory, createSession, sendRound, closeEventStream };"
    )();

    global.__api = api;
    global.__REQS = REQS;
    global.__LS = LS;
    global.__ES = ES;
    global.__created = created;
    global.__listeners = listeners;
    global.__byId = byId;
    global.__SESSIONS = SESSIONS;
    """


def _run(script_tail: str) -> dict:
    script = (
        _harness()
        .replace("__SESSIONS__", json.dumps(SESSIONS, ensure_ascii=False))
        .replace("__NEW_ID__", json.dumps(NEW_SESSION_ID))
        + script_tail
    )
    proc = subprocess.run(
        [_node(), "-e", script],
        capture_output=True,
        text=True,
        cwd=str(ROOT),
        timeout=60,
    )
    assert proc.returncode == 0, proc.stderr
    return json.loads(proc.stdout)


# ── 静态契约：页面存在历史列表容器 ──────────────────────────────────────


def test_page_has_session_history_container():
    html = INDEX_HTML.read_text(encoding="utf-8")
    for elem in ("session-history", "session-history-list", "session-history-refresh"):
        assert f'id="{elem}"' in html, f"页面缺少 {elem}"


def test_app_js_wires_session_history_elements():
    js = APP_JS.read_text(encoding="utf-8")
    assert "sessionHistory" in js
    assert "sessionHistoryList" in js
    assert "'/api/sessions'" in js, "前端未调用 GET /api/sessions"


# ── 需求 5：点击历史 Session 能加载已有 Session ─────────────────────────


def test_click_history_item_loads_that_session():
    out = _run(r"""
    (async () => {
      await __api.refreshSessionHistory();
      let clicked = false;
      const qOf = (n) => { const c = n.children.find((x) => x.className === 'session-item-question'); return c ? c.textContent : null; };
      for (const [node, hs] of __listeners.entries()) {
        if (node.className === 'session-item' && hs.click
            && qOf(node) === '旧问题：OCR 架构是否合理？') {
          hs.click(); clicked = true; break;
        }
      }
      await new Promise((r) => setTimeout(r, 120));
      process.stdout.write(JSON.stringify({
        clicked,
        sessionGets: __REQS.filter((r) => r.path === '/api/session/s-old').length,
        currentSession: __api.state.sessionId,
        questionShown: __byId['question'].value,
      }));
    })();
    """)
    assert out["clicked"], "未找到可点击的历史 Session 项"
    assert out["sessionGets"] >= 1, "点击后未加载该 Session"
    assert out["currentSession"] == "s-old"


# ── 需求 6：切换 Session 会关闭旧 SSE ───────────────────────────────────


def test_switch_session_closes_old_sse():
    out = _run(r"""
    (async () => {
      __api.state.sessionId = 's-mid';
      __api.state.models = [{ id: 'qwen', displayName: 'Qwen',
                              selected: true, active: true }];
      await __api.sendRound();                 // 建立一条 SSE 连接
      __api.state.pending = true;              // 模拟仍在流式输出中
      const before = { count: __ES.length, closed: __ES.map((e) => e.closed) };
      await __api.switchSession('s-old');
      process.stdout.write(JSON.stringify({
        before,
        after: { count: __ES.length, closed: __ES.map((e) => e.closed) },
        stateEs: __api.state.streams.size,
        pending: __api.state.pending,
      }));
    })();
    """)
    assert out["before"]["count"] == 1, "未建立旧 SSE 连接"
    assert out["before"]["closed"] == [False]
    assert out["after"]["closed"] == [True], "切换后旧 SSE 仍处于连接状态"
    assert out["stateEs"] == 0, "切换后仍残留未关闭的流"
    assert out["pending"] is False, "旧流已断开，pending 必须复位（否则发送按钮卡死）"


# ── 需求 7：切换后 localStorage 指向新 Session ──────────────────────────


def test_switch_session_updates_localstorage():
    out = _run(r"""
    (async () => {
      __api.state.sessionId = 's-mid';
      __LS['crossllm.sessionId'] = 's-mid';
      await __api.switchSession('s-old');
      process.stdout.write(JSON.stringify({
        stored: __LS['crossllm.sessionId'],
        current: __api.state.sessionId,
        sessionIdLabel: __byId['session-id'].textContent,
      }));
    })();
    """)
    assert out["stored"] == "s-old", "localStorage 未指向新 Session"
    assert out["current"] == "s-old"
    assert out["sessionIdLabel"] == "s-old", "顶栏 Session 未更新"


# ── 需求 8：新建 Session 后列表刷新并高亮新 Session ─────────────────────


def test_create_session_refreshes_list_and_highlights_new():
    out = _run(r"""
    (async () => {
      __api.state.sessionId = 's-old';
      __byId['question'].value = '全新问题';
      await __api.createSession();
      await new Promise((r) => setTimeout(r, 120));
      const items = __created.filter((n) => n.className === 'session-item');
      const current = items.filter((n) => n._cls.has('is-current'));
      const qOf = (n) => { const c = n.children.find((x) => x.className === 'session-item-question'); return c ? c.textContent : null; };
      process.stdout.write(JSON.stringify({
        listCalls: __REQS.filter((r) => r.path === '/api/sessions').length,
        itemCount: items.length,
        currentCount: current.length,
        currentText: current.length ? qOf(current[0]) : null,
        stored: __LS['crossllm.sessionId'],
      }));
    })();
    """)
    assert out["listCalls"] >= 1, "新建后未刷新历史列表"
    assert out["itemCount"] == 3, f"列表应包含新 Session：{out['itemCount']}"
    assert out["currentCount"] == 1, "当前 Session 未唯一高亮"
    assert out["currentText"] == "全新问题", f"高亮项不是新 Session：{out['currentText']}"
    assert out["stored"] == "s-new"


def test_current_session_is_highlighted_in_list():
    out = _run(r"""
    (async () => {
      __api.state.sessionId = 's-mid';
      await __api.refreshSessionHistory();
      const items = __created.filter((n) => n.className === 'session-item');
      const qOf = (n) => { const c = n.children.find((x) => x.className === 'session-item-question'); return c ? c.textContent : null; };
      process.stdout.write(JSON.stringify({
        texts: items.map(qOf),
        marks: items.map((n) => n._cls.has('is-current')),
        tags: items.map((n) => n.children.map((c) => c.textContent)),
      }));
    })();
    """)
    # 列表顺序由后端 updated_at 倒序决定（s-mid 更新），前端原样渲染
    assert out["texts"][1].startswith("缓存策略"), out["texts"]
    assert out["marks"] == [False, True], out["marks"]
    assert "当前" in out["tags"][1], "当前项缺少『当前』标记"


# ── 需求 9（反向证明）：发送 Round 不碰历史列表 API ─────────────────────


def test_send_round_does_not_touch_session_history_api():
    out = _run(r"""
    (async () => {
      __api.state.sessionId = 's-old';
      __api.state.models = [{ id: 'qwen', displayName: 'Qwen',
                              selected: true, active: true }];
      await __api.sendRound();
      process.stdout.write(JSON.stringify({
        paths: __REQS.map((r) => r.path),
        listCalls: __REQS.filter((r) => r.path === '/api/sessions').length,
      }));
    })();
    """)
    assert out["listCalls"] == 0, f"发送 Round 不得触发历史列表：{out['paths']}"


# ── 守门：本区块不得越界（无检索 / 注入 / 向量化 / 记忆搜索）───────────


def test_session_history_block_has_no_out_of_scope_semantics():
    js = APP_JS.read_text(encoding="utf-8")
    assert JS_BLOCK_MARKER in js, "未找到 Session History 区块"
    block = js[js.index(JS_BLOCK_MARKER):].lower()
    # 词边界匹配：避免 localStorage 这类正常词被 'rag' 之类的子串误伤
    banned = ("retrieval", "embedding", "vector", "rag", "rerank",
              "similarity", "cosine", "faiss")
    hit = re.search(r"\b(" + "|".join(banned) + r")\b", block)
    assert hit is None, f"Session History 区块出现越界语义 {hit.group(0)!r}"
    assert "project_memory" not in block, "Session History 不得触碰 Project Memory"
    assert "prompt" not in block, "Session History 不得参与 prompt 构造"


def test_session_history_block_writes_nothing_back():
    """列表只读：区块内不得出现 POST / PUT / DELETE 类写请求。"""
    js = APP_JS.read_text(encoding="utf-8")
    block = js[js.index(JS_BLOCK_MARKER):]
    assert "method: 'POST'" not in block
    assert "method: 'PUT'" not in block
    assert "method: 'DELETE'" not in block
