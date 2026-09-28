"""Phase 4.2 / S3.6：Summary UI 验收测试（PHASE4_REFACTOR_PLAN T10）。

覆盖用户报告的四个症状及其根因：

  症状 1  点击 Summary 后没有出现可编辑的 Summary Prompt 输入框
  症状 2  可以直接点提交（Prompt 尚未就位也能发出去）
  症状 3  LLM 返回内容不像真正的 Summary
  症状 4  Round 记录里找不到本次使用的 Summary Prompt

根因：``applyPinStyles()`` 的函数体把 Summary 的 4 个函数包了进去
      → 它们在全局作用域是 ``undefined`` → 勾选 Summary 抛
      ``ReferenceError: openSummaryPanel is not defined`` → Prompt 从不出现。

本文件按 `docs/phase4_memory.md`（§8 §9 §10 §11 §41 §66）+
PHASE4_REFACTOR_PLAN（§3.2 §18.2 T10）验收：

  UI / 请求
    A1  Summary 的 4 个函数必须是顶层函数（反向证明作用域根因）
    A2  勾选 Summary → 输入框自动清空并填入**后端**默认 Prompt
    A3  取消勾选：未编辑 → 恢复草稿；已编辑 → 保留用户文本
    A4  修改后的 Prompt 被提交，且本次发送是 Summary 请求（summary_mode=True）
    A5  结果独立渲染为 conversation_summary（不进 #rounds）
    A6  反向证明：不开 Summary 时普通发送行为不变（summary_mode=False + 建 SSE）

  后端
    B1  GET /api/config/summary-prompt 与 core/summary_prompt.py 完全一致（单一真源）
    B2  summary_mode=True → 模型收到的是 **Summary Prompt + 自己的历史**
        （不把普通 Round Prompt 当成 Summary Prompt）
    B3  summary_mode=True 不创建 Round、不改 state.json（D1）
    B4  summary_mode=True 不写 round_NNN/responses/（D3）
    B5  Summary Prompt / Summary Result 不与普通 Round 的 prompt / final answer 混用
    B6  失败的 Summary 不覆盖上一次成功 Summary（latest 仍是 success）
    B7  无有效回答的模型被跳过（不产生文件）
    B8  空 Prompt → 回退到 §9 默认 Prompt（而不是空串）
    B9  下一轮（Peer Context 路径）使用最新成功 Summary（§11/§13）
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
STATIC = ROOT / "static"
INDEX_HTML = STATIC / "index.html"
APP_JS = STATIC / "app.js"

from core.summary_prompt import DEFAULT_SUMMARY_PROMPT  # noqa: E402


def _node() -> str:
    node = shutil.which("node")
    if node is None:
        pytest.skip("未安装 node，跳过 JS 行为验证")
    return node


# =========================================================================
# 后端 fixture：临时 storage + 可记录 / 可失败注入的 stub provider
# =========================================================================

RECORDED: list[dict] = []


class _RecorderProvider:
    """记录模型实际收到的 prompt；可注入失败。"""

    def __init__(self, model_id: str, fail: bool = False) -> None:
        self.model_id = model_id
        self.fail = fail
        self.usage = {"prompt_tokens": 1, "completion_tokens": 3}

    async def generate(self, *, round_id, prompt, tools=None, max_output_tokens=None):
        RECORDED.append({
            "model_id": self.model_id,
            "round_id": round_id,
            "prompt": prompt,
        })
        await asyncio.sleep(0)  # 必须 await，否则并发退化成串行
        if self.fail:
            raise RuntimeError("injected_provider_failure")
        yield f"[stub::{self.model_id}] 工作状态：观点（未经验证）\n"


def _factory(model_id: str):
    fail = model_id in FAIL_MODELS
    return _RecorderProvider(model_id, fail=fail)


FAIL_MODELS: set[str] = set()


@pytest.fixture
def env(tmp_path, monkeypatch):
    """隔离 storage + stub provider 的 app 模块（不触碰真实 sessions/）。"""
    from core.storage import Storage

    RECORDED.clear()
    FAIL_MODELS.clear()

    storage = Storage(tmp_path / "sessions")
    storage.root.mkdir(parents=True, exist_ok=True)

    import app as app_mod

    monkeypatch.setattr(app_mod, "storage", storage)
    for mgr in (app_mod.sessions, app_mod.rounds, app_mod.summaries):
        monkeypatch.setattr(mgr, "storage", storage)
    monkeypatch.setattr(app_mod.pins, "_storage", storage)
    monkeypatch.setattr(app_mod.rounds, "_provider_factory", _factory)
    monkeypatch.setattr(app_mod.summaries, "_provider_factory", _factory)
    return app_mod, storage


@pytest.fixture
def client(env):
    from fastapi.testclient import TestClient

    app_mod, _ = env
    return TestClient(app_mod.app)


def _new_session(client, question="我们现在讨论的是香蕉。请记住这个信息。"):
    r = client.post("/api/session", json={"question": question})
    assert r.status_code == 200, r.text
    return r.json()["session_id"], r.json()["state"]["active_models"]


def _seed_rounds(storage, sid: str, models: list[str], k: int = 2, tag: str = ""):
    """直接落盘 k 个「全员成功」的 Round（绕过 provider，聚焦 Summary 语义）。"""
    from core.state import RoundState, SessionState

    rounds = []
    for i in range(1, k + 1):
        rounds.append(RoundState(
            round_id=i,
            user_instruction=f"第 {i} 轮用户问题",
            participants=list(models),
            status={m: "complete" for m in models},
            timestamp=f"2026-09-{10 + i:02d}T00:00:00Z",
            peer_review=False,
        ))
        for m in models:
            storage.save_response(sid, i, m, f"{tag}R{i} {m} 的最终回答")
    sess = SessionState(
        session_id=sid,
        original_question="我们现在讨论的是香蕉。请记住这个信息。",
        current_round=k,
        active_models=list(models),
        closed_models=[],
        rounds=rounds,
    )
    storage.save_state(sess)
    return sess


def _snapshot(root: Path) -> dict[str, str]:
    return {
        str(p.relative_to(root)).replace("\\", "/"): hashlib.md5(p.read_bytes()).hexdigest()
        for p in sorted(root.rglob("*")) if p.is_file()
    }


# =========================================================================
# A. 前端：Summary 开关 / 自动填充 / 提交（真实 app.js）
# =========================================================================

BRIDGE_SID = "s-summary"


def _harness() -> str:
    return r"""
    const fs = require('fs');
    const src = fs.readFileSync('static/app.js', 'utf8');
    const DEFAULT_PROMPT = __DEFAULT_PROMPT__;

    const REQS = [];
    const LS = {};
    const ES = [];
    const ALERTS = [];
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
        appendChild(c) { node.children.push(c); if (node._append) node._append(c); return c; },
        remove(){}, focus(){},
        // 浅层查找（支持 .class）；找不到就返回一个游离节点，避免污染 children
        querySelector(sel) {
          const cls = sel.startsWith('.') ? sel.slice(1) : null;
          if (cls) {
            const hit = node.children.find(
              (c) => c.className && c.className.split(' ').includes(cls)
            );
            if (hit) return hit;
          }
          return mkEl('div');
        },
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
    global.alert = (m) => { ALERTS.push(String(m)); };

    const jsonRes = (obj) => ({ ok: true, json: async () => obj });

    global.fetch = async (path, opts) => {
      const p = String(path);
      REQS.push({ path: p, method: (opts && opts.method) || 'GET',
                  body: (opts && opts.body) || null });
      if (p === '/api/config/summary-prompt') {
        return jsonRes({ prompt: DEFAULT_PROMPT, source: 'core/summary_prompt.py' });
      }
      if (p === '/api/sessions') return jsonRes([]);
      if (p === '/api/session') {
        return jsonRes({
          session_id: 's-summary',
          state: { session_id: 's-summary', original_question: '香蕉',
                   active_models: ['qwen'], closed_models: [], current_round: 0 },
        });
      }
      if (p.endsWith('/round')) {
        const reqBody = (opts && opts.body) ? JSON.parse(opts.body) : {};
        if (reqBody.summary_mode) {
          return jsonRes({
            summary_mode: true, round_created: false, model_ids: ['qwen'],
            instruction: reqBody.instruction || '',
            prompt_used: reqBody.instruction || '',
            results: [{ model_id: 'qwen', status: 'success', summary_id: 'summary_001',
                        source_rounds: [1], source_valid_round: 1,
                        file_path: 'summaries/qwen/summary_001.md',
                        prompt: reqBody.instruction || '', content: '当前理解：香蕉主题。',
                        error_message: null }],
          });
        }
        return jsonRes({ round_id: 9, participants: ['qwen'], peer_review: false });
      }
      if (p.endsWith('/summary')) return jsonRes({ models_with_summary: [], latest: {} });
      if (p.includes('/evidence')) return jsonRes({ total: 0, used_count: 0, entries: [] });
      if (p.includes('/memory/candidates')) return jsonRes({ total: 0, candidates: [] });
      if (p.endsWith('/memory')) return jsonRes({ total: 0, entries: [] });
      return jsonRes({});
    };

    const api = new Function(
      src + "\nreturn { state, els, onSummaryToggle, sendRound, sendSummary,"
          + " renderSummaryResults, renderSummaryBanner, refreshSessionSummaries,"
          + " loadSummaryDefaultPrompt, resetSummaryMode, updateSendState,"
          + " renderPinBanner, applyPinStyles };"
    )();

    function textOf(n) {
      let out = (n && n.textContent) ? n.textContent : '';
      if (n && n.children) { n.children.forEach((c) => { out += ' ' + textOf(c); }); }
      return out;
    }

    global.__api = api;
    global.__REQS = REQS;
    global.__LS = LS;
    global.__ES = ES;
    global.__ALERTS = ALERTS;
    global.__byId = byId;
    global.__created = created;
    global.__listeners = listeners;
    global.__DEFAULT_PROMPT = DEFAULT_PROMPT;
    """


def _run(tail: str) -> dict:
    script = (
        _harness().replace("__DEFAULT_PROMPT__", json.dumps(DEFAULT_SUMMARY_PROMPT))
        + tail
    )
    proc = subprocess.run(
        [_node(), "-e", script],
        capture_output=True, text=True, cwd=str(ROOT), timeout=60,
    )
    assert proc.returncode == 0, proc.stderr
    return json.loads(proc.stdout)


# ── A1：根因反向证明 —— Summary 函数必须是顶层函数 ──────────────────────


def test_summary_functions_are_top_level():
    """反向证明：函数被包进别的函数体时，这里会直接 ReferenceError。"""
    out = _run(r"""
      const fns = ['onSummaryToggle', 'sendSummary', 'renderSummaryResults',
                   'renderSummaryBanner', 'refreshSessionSummaries',
                   'loadSummaryDefaultPrompt', 'resetSummaryMode',
                   'applyPinStyles', 'renderPinBanner', 'sendRound'];
      const info = {};
      fns.forEach((f) => { info[f] = typeof __api[f]; });
      process.stdout.write(JSON.stringify({ info }));
    """)
    for fn, t in out["info"].items():
        assert t == "function", f"{fn} 不是顶层函数（got {t}）—— 作用域根因未修"


# ── A2/A3：勾选自动填充 + 取消规则 ──────────────────────────────────────


def test_toggle_fills_backend_default_prompt_and_backs_up_draft():
    out = _run(r"""
    (async () => {
      const box = __byId['instruction'];
      box.value = '我原本写好的普通指令';
      await __api.onSummaryToggle(true);
      process.stdout.write(JSON.stringify({
        instruction: box.value,
        expected: __DEFAULT_PROMPT,
        draftBackup: __api.state.summaryMode.draftBackup,
        enabled: __api.state.summaryMode.enabled,
        hintHidden: __byId['summary-hint'].hidden,
        alerts: __ALERTS,
        errors: 0,
      }));
    })();
    """)
    assert out["instruction"] == out["expected"], (
        "勾选 Summary 后输入框应清空并填入**后端**默认 Summary Prompt"
    )
    assert out["draftBackup"] == "我原本写好的普通指令", "原输入应被备份为 draft_backup"
    assert out["enabled"] is True
    assert out["hintHidden"] is False, "应显示『已进入 Summary 模式』提示"
    assert out["alerts"] == [], f"不应有 alert：{out['alerts']}"


def test_toggle_off_restores_draft_when_not_edited():
    out = _run(r"""
    (async () => {
      const box = __byId['instruction'];
      box.value = '原始草稿';
      await __api.onSummaryToggle(true);
      const filled = box.value;
      await __api.onSummaryToggle(false);
      process.stdout.write(JSON.stringify({
        filled, afterOff: box.value, enabled: __api.state.summaryMode.enabled,
        draftBackup: __api.state.summaryMode.draftBackup,
      }));
    })();
    """)
    assert out["filled"] == DEFAULT_SUMMARY_PROMPT
    assert out["afterOff"] == "原始草稿", "未编辑就取消 → 应恢复 draft_backup"
    assert out["enabled"] is False
    assert out["draftBackup"] is None


def test_toggle_off_keeps_user_edited_text():
    out = _run(r"""
    (async () => {
      const box = __byId['instruction'];
      box.value = '原始草稿';
      await __api.onSummaryToggle(true);
      box.value = '我自己改过的总结要求';
      __listeners.get(box)['input']();          // 触发 input → userEdited=true
      await __api.onSummaryToggle(false);
      process.stdout.write(JSON.stringify({
        afterOff: box.value, userEdited: __api.state.summaryMode.userEdited,
      }));
    })();
    """)
    assert out["afterOff"] == "我自己改过的总结要求", (
        "已编辑就取消 → 必须保留用户文本（不静默丢弃）"
    )


# ── A4/A5：提交的是修改后的 Prompt，且结果独立渲染 ──────────────────────


def _prepare_session_js() -> str:
    return r"""
      __api.state.sessionId = 's-summary';
      __api.state.models = [{ id: 'qwen', displayName: 'Qwen',
                              selected: true, active: true }];
    """


def test_edited_prompt_is_submitted_as_summary_request():
    out = _run(r"""
    (async () => {
      __SESSION__
      const box = __byId['instruction'];
      await __api.onSummaryToggle(true);
      box.value = '请只总结已确认信息';
      __listeners.get(box)['input']();
      await __api.sendRound();                 // 走主"发送"按钮
      const roundReqs = __REQS.filter((r) => r.path === '/api/session/s-summary/round');
      const last = roundReqs[roundReqs.length - 1];
      process.stdout.write(JSON.stringify({
        body: last ? JSON.parse(last.body) : null,
        paths: __REQS.map((r) => r.path),
        esCount: __ES.length,
        toggleChecked: __byId['summary-toggle'].checked,
        instructionCleared: box.value === '',
        hintHidden: __byId['summary-hint'].hidden,
        results: __byId['summary-results'].children.length,
        alerts: __ALERTS,
      }));
    })();
    """.replace("__SESSION__", _prepare_session_js()))
    assert out["body"] is not None, f"未发出 Summary 请求：{out['paths']}"
    assert out["body"]["summary_mode"] is True, "Summary 请求必须带 summary_mode=True"
    assert out["body"]["instruction"] == "请只总结已确认信息", (
        "提交的必须是用户修改后的 Prompt"
    )
    assert out["esCount"] == 0, "Summary 不创建 Round → 不应建立 SSE 连接"
    assert out["toggleChecked"] is False, "发送后 Summary 模式应自动关闭"
    assert out["hintHidden"] is True
    assert out["instructionCleared"] is True, "发送后输入框应清空"
    assert out["alerts"] == [], f"不应有 alert：{out['alerts']}"


def test_summary_result_rendered_separately_from_rounds():
    out = _run(r"""
    (async () => {
      __SESSION__
      const box = __byId['instruction'];
      await __api.onSummaryToggle(true);
      box.value = '请只总结已确认信息';
      __listeners.get(box)['input']();
      await __api.sendRound();
      const host = __byId['summary-results'];
      const first = host.children[0];
      process.stdout.write(JSON.stringify({
        sections: host.children.length,
        text: textOf(host),
        kindAttr: first && first.dataset ? first.dataset.kind : null,
        roundsText: textOf(__byId['rounds']),
        roundsChildren: __byId['rounds'].children.length,
        bannerHidden: __byId['summary-banner'].hidden,
      }));
    })();
    """.replace("__SESSION__", _prepare_session_js()))
    assert out["sections"] == 1, "Summary 结果应渲染成一个独立区块"
    # 用户可见文本必须是「对话摘要」；内部语义标记留在 data-kind 属性上
    assert "对话摘要" in out["text"], "摘要区块必须使用用户可见的中文语义标记"
    assert out["kindAttr"] == "conversation_summary", (
        "内部语义标记必须保留在 data-kind 属性上（供机器识别，不占用户可见文本）"
    )
    assert "请只总结已确认信息" in out["text"], (
        "结果里必须能追溯本次使用的 Summary Prompt"
    )
    assert "当前理解：香蕉主题。" in out["text"], "必须展示 Summary 正文"
    assert "summary_001" in out["text"] and "summaries/qwen/summary_001.md" in out["text"]
    assert out["roundsChildren"] == 0, "Summary 结果不得混进 #rounds"


# ── A6：反向证明 —— 不开 Summary 时普通发送行为不变 ─────────────────────


def test_plain_send_is_unchanged_when_summary_off():
    out = _run(r"""
    (async () => {
      __SESSION__
      const box = __byId['instruction'];
      box.value = '普通的一轮指令';
      await __api.sendRound();
      const roundReqs = __REQS.filter((r) => r.path === '/api/session/s-summary/round');
      const last = roundReqs[roundReqs.length - 1];
      process.stdout.write(JSON.stringify({
        body: last ? JSON.parse(last.body) : null,
        esCount: __ES.length,
        summaryResults: __byId['summary-results'].children.length,
        toggleChecked: __byId['summary-toggle'].checked,
      }));
    })();
    """.replace("__SESSION__", _prepare_session_js()))
    assert out["body"]["summary_mode"] is False, "普通 Round 不得携带 summary_mode=True"
    assert out["body"]["instruction"] == "普通的一轮指令"
    assert out["esCount"] == 1, "普通 Round 必须建立 SSE 连接"
    assert out["summaryResults"] == 0, "普通 Round 不得往 Summary 区渲染"


# ── 静态契约：页面元素 + 单一真源 ───────────────────────────────────────


def test_page_has_summary_controls():
    html = INDEX_HTML.read_text(encoding="utf-8")
    for elem in ("summary-toggle", "summary-hint", "summary-results",
                 "summary-banner", "summary-banner-text"):
        assert f'id="{elem}"' in html, f"页面缺少 {elem}"


def test_app_js_does_not_hardcode_default_prompt():
    """默认 Prompt 的单一真源必须只在 core/summary_prompt.py。"""
    js = APP_JS.read_text(encoding="utf-8")
    assert "请总结我们截至目前的完整对话" not in js, (
        "app.js 不得硬编码默认 Summary Prompt（会与后端实际使用者不一致）"
    )
    assert "/api/config/summary-prompt" in js, "前端必须从后端取默认 Prompt"
    # 后端模块里必须仍然存在（真源没被删）
    assert "请总结我们截至目前的完整对话" in DEFAULT_SUMMARY_PROMPT


def test_app_js_fetches_default_prompt_at_startup():
    js = APP_JS.read_text(encoding="utf-8")
    init_body = re.search(r"function init\(\) \{[\s\S]*?\n\}", js)
    assert init_body, "app.js 中未找到 init()"
    assert "loadSummaryDefaultPrompt" in init_body.group(0), (
        "init() 应在启动时拉取默认 Summary Prompt"
    )


# =========================================================================
# B. 后端：summary_mode 语义 / 落盘隔离 / 失败保护 / 后续上下文
# =========================================================================


def test_b1_config_endpoint_is_single_source(client):
    r = client.get("/api/config/summary-prompt")
    assert r.status_code == 200, r.text
    assert r.json()["prompt"] == DEFAULT_SUMMARY_PROMPT


def test_b2_summary_mode_sends_summary_prompt_with_own_history(client, env):
    app_mod, storage = env
    sid, models = _new_session(client)
    _seed_rounds(storage, sid, models, k=2)
    RECORDED.clear()

    prompt = "请只总结目前对话中已经明确确认的信息。不要把模型推测写成事实。"
    r = client.post(f"/api/session/{sid}/round", json={
        "instruction": prompt, "summary_mode": True,
        "selected_models": [models[0]],
    })
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["summary_mode"] is True
    assert body["round_created"] is False, "（D1）Summary 请求不得创建 Round"
    assert body["prompt_used"] == prompt

    # 模型实际收到：Summary Prompt 开头 + 该模型自己的历史（不是普通 Round Prompt）
    assert len(RECORDED) == 1, f"应恰好调用 1 次模型：{RECORDED}"
    sent = RECORDED[0]["prompt"]
    assert sent.startswith(prompt), "模型收到的 prompt 必须以用户（可编辑）的 Summary Prompt 开头"
    assert RECORDED[0]["round_id"] == -1, "Summary 是独立 LLM 调用，不属于任何 Round"
    assert "第 1 轮用户问题" in sent and "第 2 轮用户问题" in sent, (
        "Summary 必须带上该模型自己的历史（§13）"
    )
    # 不得出现普通 Round 的指令（不把普通 User Prompt 当成 Summary Prompt）
    assert "你是一名平级参与者" not in sent, "不得使用普通 Round Prompt 模板"

    res0 = body["results"][0]
    assert res0["status"] == "success" and res0["trigger"] == "user"
    assert res0["summary_id"] == "summary_001"
    assert res0["source_valid_round"] == 2
    assert res0["is_derived_artifact"] is True
    assert res0["creates_round"] is False
    assert res0["contributes_valid_round_count"] is False
    assert res0["prompt"] == prompt, "落盘必须能追溯本次使用的 Prompt（症状 4）"


def test_b3_summary_mode_does_not_create_round_or_touch_state(client, env):
    app_mod, storage = env
    sid, models = _new_session(client)
    _seed_rounds(storage, sid, models, k=1)
    before_state = _snapshot(storage.root)

    r = client.post(f"/api/session/{sid}/round", json={
        "instruction": "总结一下", "summary_mode": True, "selected_models": models,
    })
    assert r.status_code == 200, r.text

    state = storage.load_state(sid)
    assert state.current_round == 1, "Summary 不得推进 current_round（D1）"
    assert [x.round_id for x in state.rounds] == [1]
    assert not (storage.session_dir(sid) / "round_002").exists(), (
        "Summary 不得创建 round_NNN/"
    )
    after_state = _snapshot(storage.root)
    changed = {k for k in set(before_state) | set(after_state)
               if before_state.get(k) != after_state.get(k)}
    # state.json 必须字节级不变
    state_keys = [k for k in before_state if k.endswith("/state.json")]
    assert state_keys, "快照里应有 state.json"
    for k in state_keys:
        assert before_state[k] == after_state.get(k), (
            f"Summary 请求修改了 {k}（Summary 不是新 Round，不得改 Session 状态）"
        )
    # 只允许新增 summaries/ 与追溯日志
    for path in sorted(changed):
        rel = path.split("/", 1)[1] if "/" in path else path
        assert rel.startswith("summaries/") or rel.endswith("context_policy.jsonl"), (
            f"Summary 请求产生了越界写入：{path}"
        )


def test_b4_summary_mode_does_not_write_round_responses(client, env):
    app_mod, storage = env
    sid, models = _new_session(client)
    _seed_rounds(storage, sid, models, k=1)
    round_dir = storage.session_dir(sid) / "round_001"
    before = {p.name: hashlib.md5(p.read_bytes()).hexdigest()
              for p in sorted(round_dir.rglob("*.md"))}

    client.post(f"/api/session/{sid}/round", json={
        "instruction": "总结", "summary_mode": True, "selected_models": models,
    })

    after = {p.name: hashlib.md5(p.read_bytes()).hexdigest()
             for p in sorted(round_dir.rglob("*.md"))}
    assert after == before, "（D3）Summary 不得写 round_NNN/（responses/ 亦然）"


def test_b5_prompt_and_result_not_mixed_with_round_files(client, env):
    app_mod, storage = env
    sid, models = _new_session(client)
    _seed_rounds(storage, sid, models, k=1)

    summary_prompt = "只总结已确认信息，别把推测写成事实。"
    client.post(f"/api/session/{sid}/round", json={
        "instruction": summary_prompt, "summary_mode": True,
        "selected_models": [models[0]],
    })

    # 普通 Round 的 prompt 文件里不得出现 Summary Prompt
    for p in (storage.session_dir(sid) / "round_001").glob("*.prompt.md"):
        assert summary_prompt not in p.read_text(encoding="utf-8"), (
            f"{p.name} 混入了 Summary Prompt"
        )
    # Summary Prompt / Result 只落在 summaries/<model>/summary_NNN.md
    sfile = storage.session_dir(sid) / "summaries" / models[0] / "summary_001.md"
    assert sfile.exists()
    text = sfile.read_text(encoding="utf-8")
    assert summary_prompt in text, "Summary Prompt 必须落盘可审计"
    assert "工作状态" in text, "Summary 正文应落盘"
    # 反向：Summary 文件里不得混入普通 Round 的 final answer
    round_answer = storage.load_responses(sid, 1)[models[0]]
    assert round_answer not in text, "Summary 文件混入了 Round 的 final answer"
    # 不得污染其它模型
    other = [m for m in models if m != models[0]]
    for m in other:
        assert not (storage.session_dir(sid) / "summaries" / m).exists(), (
            f"Summary 必须按 model_id 隔离，却写了 {m}"
        )


def test_b6_failed_summary_does_not_overwrite_previous_success(client, env):
    app_mod, storage = env
    sid, models = _new_session(client)
    _seed_rounds(storage, sid, models, k=1)
    mid = models[0]

    # 第一次成功
    r1 = client.post(f"/api/session/{sid}/round", json={
        "instruction": "第一次总结", "summary_mode": True, "selected_models": [mid],
    })
    assert r1.json()["results"][0]["status"] == "success"
    latest1 = app_mod.summaries.latest_for(sid, mid)
    assert latest1 is not None and latest1.summary_id == "summary_001"
    file1 = storage.session_dir(sid) / latest1.file_path
    bytes_before = file1.read_bytes()

    # 第二次注入失败
    FAIL_MODELS.add(mid)
    r2 = client.post(f"/api/session/{sid}/round", json={
        "instruction": "第二次总结", "summary_mode": True, "selected_models": [mid],
    })
    assert r2.status_code == 200, r2.text
    res2 = r2.json()["results"][0]
    assert res2["status"] == "failed", f"失败必须降级为 failed：{res2}"
    assert "injected_provider_failure" in (res2["error_message"] or "")

    # 旧的成功 Summary 未被覆盖，latest 仍指向它
    latest2 = app_mod.summaries.latest_for(sid, mid)
    assert latest2 is not None, "失败不得让 latest 变空"
    assert latest2.summary_id == "summary_001" == latest1.summary_id
    assert file1.read_bytes() == bytes_before, "旧的成功 Summary 文件被改写了"

    # 失败不得落盘新 summary 文件
    files = sorted(p.name for p in (
        storage.session_dir(sid) / "summaries" / mid).glob("summary_*.md"))
    assert files == ["summary_001.md"], f"失败结果不应落盘：{files}"


def test_b7_model_without_valid_answer_is_skipped(client, env):
    app_mod, storage = env
    sid, models = _new_session(client)      # 没有任何 Round
    r = client.post(f"/api/session/{sid}/round", json={
        "instruction": "总结", "summary_mode": True, "selected_models": [models[0]],
    })
    assert r.status_code == 200, r.text
    res = r.json()["results"][0]
    assert res["status"] == "skipped"
    assert res["error_message"] == "no_successful_final_answer"
    assert res["file_path"] is None
    assert not (storage.session_dir(sid) / "summaries").exists()
    assert RECORDED == [], "无有效回答时不得调用模型"


def test_b8_empty_prompt_falls_back_to_default(client, env):
    app_mod, storage = env
    sid, models = _new_session(client)
    _seed_rounds(storage, sid, models, k=1)
    RECORDED.clear()
    r = client.post(f"/api/session/{sid}/round", json={
        "instruction": "   ", "summary_mode": True, "selected_models": [models[0]],
    })
    assert r.status_code == 200, r.text
    res = r.json()["results"][0]
    assert res["prompt"] == DEFAULT_SUMMARY_PROMPT, "空 Prompt 应回退到 §9 默认 Prompt"
    assert RECORDED and RECORDED[0]["prompt"].startswith(DEFAULT_SUMMARY_PROMPT)


def test_b9_next_round_uses_latest_successful_summary(client, env):
    """§11/§13：最新成功 Summary 成为后续（Peer Context）工作状态。"""
    app_mod, storage = env
    sid, models = _new_session(client)
    assert len(models) >= 2, "本用例需要至少两个模型才能构造 Peer Context"
    a, b = models[0], models[1]
    _seed_rounds(storage, sid, models, k=2)

    # 用户主动为 B 生成 Summary
    r = client.post(f"/api/session/{sid}/round", json={
        "instruction": "总结 B 的状态", "summary_mode": True, "selected_models": [b],
    })
    summary_b = r.json()["results"][0]
    assert summary_b["status"] == "success"
    content = summary_b["content"]

    # 构造下一轮（A 视角）的 Peer Context：必须复用 B 的最新 Summary
    from core.peer_context import PeerContextPreparator
    from core.state import RoundState
    from core.valid_round import ValidRoundPoolBuilder

    session = storage.load_state(sid)
    round_state = RoundState(
        round_id=3, user_instruction="继续", participants=[a, b],
        candidate_ids={a: "A", b: "B"}, peer_review=True,
        status={a: "pending", b: "pending"}, timestamp="2026-09-20T00:00:00Z",
    )
    pool = ValidRoundPoolBuilder(storage).build(session)
    prep = PeerContextPreparator(storage, app_mod.summaries)

    async def run():
        return await prep.prepare(session=session, round_state=round_state, valid_pool=pool)

    result = asyncio.run(run())
    items = result.get(a, [])
    assert items, "A 应拿到 B 的 Peer Context"
    item = items[0]
    assert item.mode == "conversation_summary", (
        f"B 有 2 个有效回答 → 必须走 Summary（got {item.mode}）"
    )
    assert item.source_id == "summary_001", f"source_id 应为最新 Summary：{item.source_id}"
    assert item.content == content, "下一轮 Peer Context 必须使用最新成功 Summary"
