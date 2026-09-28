"""Phase 4.6 / M2 最小 UI 测试（只测前端，不改后端）。

覆盖用户要求的 8 项：
  1. 页面存在"生成 Memory Candidate"入口
  2. Candidate 创建后可以显示
  3. Candidate 创建后不调用 confirm（确认前不产生正式 Memory）
  4. 点击"确认保存"会调用 confirm endpoint
  5. duplicate 正确显示（含 duplicate_of，不伪装成功）
  6. conflict 正确显示（含 conflicts + 不自动覆盖）
  7. 普通发送 Round 不会自动调用 Candidate API
  8. 页面不存在 Memory 自动注入 Context 的逻辑
  9. 普通可见文本不出现内部 ID（memory_id / candidate_id）

方式：静态契约（HTML/JS 源码）+ node 加载真实 app.js 的行为验证
（DOM / fetch 打桩，捕获实际请求与渲染结果）。
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest

STATIC = Path(__file__).resolve().parent.parent / "static"
INDEX_HTML = STATIC / "index.html"
APP_JS = STATIC / "app.js"


def _node() -> str:
    node = shutil.which("node")
    if node is None:
        pytest.skip("未安装 node，跳过 JS 行为验证")
    return node


# ── 前端 harness：加载真实 app.js，DOM / fetch 打桩 ─────────────────────


def _harness(confirm_response: dict | None = None) -> str:
    return r"""
    const fs = require('fs');
    const src = fs.readFileSync('static/app.js', 'utf8');

    const CONFIRM_RESPONSE = __CONFIRM__;

    const REQS = [];            // 捕获所有请求
    const created = [];         // 创建出的 DOM 元素（用于断言渲染）
    const listeners = new Map();// 元素 id -> handlers

    function mkEl(tag) {
      const node = {
        tagName: tag, id: '', value: '', checked: false, hidden: false,
        textContent: '', innerHTML: '', className: '', type: '',
        dataset: {}, style: {}, children: [],
        classList: { add(){}, remove(){}, toggle(){}, contains: () => false },
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

    const byId = {};
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
    global.localStorage = { getItem: () => 'sid-x', removeItem(){}, setItem(){} };
    global.EventSource = class { addEventListener(){} close(){} };
    global.alert = () => {};

    function jsonRes(obj) {
      return { ok: true, json: async () => obj };
    }

    global.fetch = async (path, opts) => {
      const p = String(path);
      REQS.push({ path: p, method: (opts && opts.method) || 'GET',
                  body: opts && opts.body ? JSON.parse(opts.body) : null });
      if (p.endsWith('/memory/candidates')) {
        return jsonRes({ project_id: 'default', total: REQS.filter(
          r => r.path.endsWith('/memory/candidate') && r.method === 'POST').length,
          candidates: CANDIDATES });
      }
      if (/\/memory\/candidate\/[^/]+\/confirm$/.test(p)) {
        return jsonRes(CONFIRM_RESPONSE);
      }
      if (p.endsWith('/memory/candidate')) return jsonRes({ candidate_id: 'candidate_001' });
      if (p.endsWith('/memory')) {
        return jsonRes({ project_id: 'default', total: MEMORIES.length, entries: MEMORIES });
      }
      if (p.endsWith('/round')) return jsonRes({ round_id: 8, participants: ['qwen'], peer_review: false });
      if (p.endsWith('/evidence')) return jsonRes({ total: 0, used_count: 0, entries: [] });
      return jsonRes({});
    };

    let CANDIDATES = [];
    let MEMORIES = [];

    const api = new Function(
      src + "\nreturn { state, sendRound, refreshMemory, createCandidate,"
          + " confirmCandidate, els, MEMORY_PROJECT_ID };"
    )();

    global.__api = api;
    global.__REQS = REQS;
    global.__created = created;
    global.__listeners = listeners;
    global.__byId = byId;
    global.__setCandidates = (c) => { CANDIDATES = c; };
    global.__setMemories = (m) => { MEMORIES = m; };
    """


def _run(script_tail: str, confirm_response: dict | None = None) -> dict:
    script = _harness().replace("__CONFIRM__", json.dumps(confirm_response or {})) + script_tail
    proc = subprocess.run(
        [_node(), "-e", script],
        capture_output=True,
        text=True,
        cwd=str(Path(__file__).resolve().parent.parent),
        timeout=60,
    )
    assert proc.returncode == 0, proc.stderr
    return json.loads(proc.stdout)


# ── 1. 页面存在入口 ─────────────────────────────────────────────────────


def test_page_has_candidate_entry():
    html = INDEX_HTML.read_text(encoding="utf-8")
    assert 'id="memory-compose-toggle"' in html
    assert "生成记忆候选" in html
    assert 'id="memory-candidate-submit"' in html


def test_page_has_candidate_and_memory_display_areas():
    html = INDEX_HTML.read_text(encoding="utf-8")
    for elem in ("memory-candidates", "memory-list", "memory-notice",
                 "memory-content", "memory-kind", "memory-semantics",
                 "memory-source", "memory-refs"):
        assert f'id="{elem}"' in html, f"页面缺少 {elem}"


# ── 2. Candidate 创建后可以显示 ─────────────────────────────────────────


def test_candidate_is_rendered_after_create():
    out = _run(r"""
    __setCandidates([{
      candidate_id: 'candidate_001', kind: 'decision',
      source_semantics: 'decision', content: '最终选择方案 Y。',
      source: 'round', source_refs: ['session:s1/round:003'],
    }]);
    (async () => {
      await __api.refreshMemory();
      const texts = __created.map(n => n.textContent).filter(Boolean);
      process.stdout.write(JSON.stringify({
        rendered: texts,
        hasContent: texts.includes('最终选择方案 Y。'),
        // 用户可见文本必须是中文标签，不得直显内部枚举值（decision / round）
        hasMeta: texts.some(t => String(t).includes('用户决策'))
              && texts.some(t => String(t).includes('轮次回答')),
        hasRefs: texts.some(t => String(t).includes('session:s1/round:003')),
        hasConfirmBtn: texts.includes('确认保存'),
      }));
    })();
    """)
    assert out["hasContent"], f"未渲染 Candidate 内容：{out['rendered']}"
    assert out["hasMeta"], "未渲染 kind / source_semantics / source"
    assert out["hasRefs"], "未渲染 source_refs"
    assert out["hasConfirmBtn"], "缺少确认保存按钮"


# ── 2b. 普通可见文本不出现内部 ID（memory_id / candidate_id）──────────────


def test_internal_ids_are_not_in_visible_text():
    """memory_id / candidate_id 不得出现在普通可见文本里。

    内部 ID 允许保留在 DOM data 属性与 title（内部可查），但不参与普通阅读。
    """
    out = _run(r"""
    __setCandidates([{
      candidate_id: 'candidate_001', kind: 'decision',
      source_semantics: 'decision', content: '最终选择方案 Y。',
      source: 'round', source_refs: ['session:s1/round:003'],
    }]);
    __setMemories([{
      memory_id: 'memory_001', kind: 'decision', source_semantics: 'decision',
      content: '验收记录：统一语义色板。',
    }]);
    (async () => {
      await __api.refreshMemory();
      const texts = __created.map(n => String(n.textContent || '')).filter(Boolean);
      const idLike = texts.filter(t => /(memory_|candidate_)\d/.test(t));
      const candCard = __created.find(n => n.dataset && n.dataset.candidateId);
      const memCard = __created.find(n => n.dataset && n.dataset.memoryId);
      process.stdout.write(JSON.stringify({
        idLike,
        hasCandTitle: texts.includes('记忆候选'),
        hasMemTitle: texts.includes('已确认记忆'),
        candData: candCard ? candCard.dataset.candidateId : null,
        candTitle: candCard ? String(candCard.title || '') : '',
        memData: memCard ? memCard.dataset.memoryId : null,
        memTitle: memCard ? String(memCard.title || '') : '',
      }));
    })();
    """)
    assert out["idLike"] == [], f"普通可见文本泄漏了内部 ID：{out['idLike']}"
    assert out["hasCandTitle"], "候选卡标题应为「记忆候选」"
    assert out["hasMemTitle"], "已确认记忆标题应为「已确认记忆」"
    # ID 仍可从 DOM 取到：data 属性 + title（不占可见正文）
    assert out["candData"] == "candidate_001", out["candData"]
    assert "candidate_001" in out["candTitle"], out["candTitle"]
    assert out["memData"] == "memory_001", out["memData"]
    assert "memory_001" in out["memTitle"], out["memTitle"]


# ── 3. Candidate 创建后不调用 confirm ───────────────────────────────────


def test_create_candidate_does_not_call_confirm():
    out = _run(r"""
    (async () => {
      __byId['memory-content'].value = '最终选择方案 Y。';
      __byId['memory-kind'].value = 'decision';
      __byId['memory-semantics'].value = 'decision';
      __byId['memory-source'].value = 'round';
      __byId['memory-refs'].value = 'session:s1/round:003';
      await __api.createCandidate();
      const paths = __REQS.map(r => r.path);
      process.stdout.write(JSON.stringify({
        paths,
        created: paths.filter(p => /\/memory\/candidate$/.test(p)).length,
        confirmed: paths.filter(p => p.includes('/confirm')).length,
        body: (__REQS.find(r => /\/memory\/candidate$/.test(r.path)) || {}).body,
      }));
    })();
    """)
    assert out["created"] == 1, f"未调用创建接口：{out['paths']}"
    assert out["confirmed"] == 0, "创建阶段不得调用 confirm"
    assert out["body"]["kind"] == "decision"
    assert out["body"]["source_semantics"] == "decision"
    assert out["body"]["source"] == "round"
    assert out["body"]["source_refs"] == ["session:s1/round:003"]


# ── 4. 点击确认调用 confirm endpoint ────────────────────────────────────


def test_confirm_button_calls_confirm_endpoint():
    out = _run(r"""
    __setCandidates([{
      candidate_id: 'candidate_007', kind: 'fact', source_semantics: 'result',
      content: 'Y 提升 18%。', source: 'user_input', source_refs: [],
    }]);
    (async () => {
      await __api.refreshMemory();
      // 找到"确认保存"按钮并触发其 click handler
      let clicked = false;
      for (const [node, hs] of __listeners.entries()) {
        if (node.textContent === '确认保存' && hs.click) { hs.click(); clicked = true; break; }
      }
      const paths = __REQS.map(r => r.path);
      process.stdout.write(JSON.stringify({
        clicked,
        confirmCalls: paths.filter(p => p.includes('/candidate/candidate_007/confirm')),
      }));
    })();
    """)
    assert out["clicked"], "未找到可点击的确认保存按钮"
    assert out["confirmCalls"], "点击确认未调用 confirm endpoint"


# ── 5. duplicate 正确显示 ───────────────────────────────────────────────


def test_duplicate_display():
    out = _run(
        r"""
    (async () => {
      await __api.confirmCandidate('candidate_002');
      process.stdout.write(JSON.stringify({
        notice: __byId['memory-notice'].textContent,
        hidden: __byId['memory-notice'].hidden,
        cls: __byId['memory-notice'].className,
      }));
    })();
    """,
        confirm_response={
            "entry": None,
            "status": "duplicate",
            "warnings": ["内容与已有 Memory memory_001 重复，未创建新条目"],
            "duplicate_of": "memory_001",
            "conflicts": [],
        },
    )
    assert out["hidden"] is False
    assert "重复" in out["notice"], out["notice"]
    assert "memory_001" in out["notice"], "未显示 duplicate_of"
    assert "duplicate" in out["cls"]


# ── 6. conflict 正确显示 ────────────────────────────────────────────────


def test_conflict_display():
    out = _run(
        r"""
    (async () => {
      await __api.confirmCandidate('candidate_003');
      process.stdout.write(JSON.stringify({
        notice: __byId['memory-notice'].textContent,
        hidden: __byId['memory-notice'].hidden,
        cls: __byId['memory-notice'].className,
      }));
    })();
    """,
        confirm_response={
            "entry": {"memory_id": "memory_002", "kind": "decision",
                      "source_semantics": "decision", "content": "改用全量重算"},
            "status": "created",
            "warnings": ["与以下已有 Memory 同 kind 同来源但内容不同，请人工核对（系统不裁决）：memory_001"],
            "duplicate_of": None,
            "conflicts": ["memory_001"],
        },
    )
    assert out["hidden"] is False
    assert "冲突" in out["notice"], out["notice"]
    assert "memory_001" in out["notice"], "未显示 conflicts"
    assert "系统不会自动覆盖旧内容" in out["notice"], "未声明不自动覆盖旧记忆"
    assert "memory_002" in out["notice"], "冲突时新 Memory 创建成功也应显示"


# ── 7. 普通发送 Round 不自动调用 Candidate API ──────────────────────────


def test_send_round_does_not_touch_memory_api():
    out = _run(r"""
    (async () => {
      __api.state.sessionId = 'sid-x';
      __api.state.models = [{ id: 'qwen', displayName: 'Q', selected: true, active: true }];
      await __api.sendRound();
      const paths = __REQS.map(r => r.path);
      process.stdout.write(JSON.stringify({
        paths,
        memoryCalls: paths.filter(p => p.includes('/memory')),
      }));
    })();
    """)
    assert out["memoryCalls"] == [], f"普通 Round 不得触碰 Memory API：{out['paths']}"


# ── 8. 无 Memory 自动注入 Context 的逻辑 ────────────────────────────────


def test_no_memory_context_injection_in_frontend():
    js = APP_JS.read_text(encoding="utf-8")
    # 前端只做展示与显式操作；不得把 Memory 拼进 sendRound 的 prompt
    body = js[js.index("async function sendRound()"):]
    body = body[: body.index("\nfunction createCard(")]
    assert "memory" not in body.lower(), "sendRound() 中出现 Memory 相关逻辑"
    # 也不存在任何读取 Memory 后回填 Context 的通道
    assert "inject" not in js.lower()
    assert "prompt +=" not in js


def test_read_round_policy_unchanged_by_memory_ui():
    js = APP_JS.read_text(encoding="utf-8")
    fn = re.search(r"function readRoundPolicy\(\) \{[\s\S]*?\n\}", js).group(0)
    assert "memory" not in fn.lower(), "readRoundPolicy() 不得混入 Memory"
