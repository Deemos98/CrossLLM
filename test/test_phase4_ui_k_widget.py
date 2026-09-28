"""S3.12：Phase 4 前端 k 控件契约测试（统一 Session k，无 per-request 覆盖）。

背景：
    页面曾出现两组 k 控件（顶部 Session k + "本轮 k"），``getElementById``
    只命中第一个，导致"UI 显示一个值、请求发另一个值"（context_policy.k=0.19
    而 UI 显示 0.50）。引入独立 ID 后仍有双 k 状态，故最终决定：

    **删除"本轮 k"，只保留一个 Session k 控件**，它同时是每次发送使用的 k。

契约：
    - 页面只有一个 k slider（``#k-slider``）和一个 k 显示（``#k-value``）
    - 不存在 round-k-* 相关控件或代码
    - ``readRoundPolicy()`` 读取 ``els.kSlider``
    - ``sendRound()`` 的 POST body.k == 当前 Session k 控件值（0.19 → 0.19、
      改为 0.50 → 0.50）

后端 k 逻辑不在本文件范围内（由 test_phase4_core_units.py / E2E 覆盖）。
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

STATIC = Path(__file__).resolve().parent.parent / "static"
INDEX_HTML = STATIC / "index.html"
APP_JS = STATIC / "app.js"


def _ids(html: str) -> list[str]:
    return re.findall(r'\sid="([^"]+)"', html)


def _extract_function(js: str, name: str) -> str:
    m = re.search(rf"function {name}\(\) \{{[\s\S]*?\n\}}", js)
    assert m, f"app.js 中未找到 function {name}()"
    return m.group(0)


def _node() -> str:
    node = shutil.which("node")
    if node is None:
        pytest.skip("未安装 node，跳过 JS 行为验证")
    return node


# --------------------------------------------------------------------------
# 1. 页面只有一个 k 控件
# --------------------------------------------------------------------------


def test_exactly_one_k_slider_in_page():
    """全页只有一个 #k-slider 与一个 #k-value（双 k 已从 UI 删除）。"""
    html = INDEX_HTML.read_text(encoding="utf-8")
    ids = _ids(html)
    assert ids.count("k-slider") == 1, f"k-slider 出现 {ids.count('k-slider')} 次"
    assert ids.count("k-value") == 1, f"k-value 出现 {ids.count('k-value')} 次"


# --------------------------------------------------------------------------
# 2. readRoundPolicy() 读取 Session k 控件
# --------------------------------------------------------------------------


def test_read_round_policy_reads_session_k_slider():
    fn = _extract_function(APP_JS.read_text(encoding="utf-8"), "readRoundPolicy")
    assert "els.kSlider" in fn, "readRoundPolicy() 未读取 Session k 控件"
    assert "roundK" not in fn, "readRoundPolicy() 仍残留本轮 k 引用"


def test_send_round_submits_read_round_policy():
    """sendRound() 的 POST body 必须展开 readRoundPolicy()。"""
    js = APP_JS.read_text(encoding="utf-8")
    body = js[js.index("async function sendRound()"):]
    body = body[: body.index("\nfunction createCard(")]
    assert "...readRoundPolicy()" in body, "sendRound() 未提交政策参数"


# --------------------------------------------------------------------------
# 3. 行为验证：实际执行 readRoundPolicy()（node）
# --------------------------------------------------------------------------


def _run_round_policy(slider_values: dict[str, str]) -> dict:
    fn_src = _extract_function(APP_JS.read_text(encoding="utf-8"), "readRoundPolicy")
    stub = "\n".join(
        f"  {k}: {{ value: {json.dumps(v)} }}," for k, v in slider_values.items()
    )
    script = (
        f"const els = {{\n{stub}\n}};\n"
        f"{fn_src}\n"
        "process.stdout.write(JSON.stringify(readRoundPolicy()));\n"
    )
    proc = subprocess.run(
        [_node(), "-e", script], capture_output=True, text=True, timeout=60
    )
    assert proc.returncode == 0, proc.stderr
    return json.loads(proc.stdout)


def test_session_k_019_is_sent_as_019():
    """Session k=0.19 → POST body.k 必须是 0.19（与后端 context_policy.k 同源）。"""
    policy = _run_round_policy({"kSlider": "0.19"})
    assert policy["k"] == 0.19, f"§55 违反：Session k=0.19，实际发送 {policy['k']}"
    # 输出上限不再由 UI 提供（模型能力来自 Provider 元数据，§四）
    assert "request_max_output" not in policy


def test_session_k_changed_to_050_is_sent_as_050():
    """把 Session k 改为 0.50 → 下一次发送 body.k 必须是 0.50。"""
    policy = _run_round_policy({"kSlider": "0.50"})
    assert policy["k"] == 0.50, f"§55 违反：Session k=0.50，实际发送 {policy['k']}"


def test_k_widget_absent_falls_back_to_half():
    """控件缺失时兜底 0.5，行为不变。"""
    assert _run_round_policy({})["k"] == 0.5


# --------------------------------------------------------------------------
# 4. 端到端（前端内）：加载真实 app.js → sendRound() → 捕获 POST body
# --------------------------------------------------------------------------


def _send_round_and_capture(k_value: str) -> dict:
    script = r"""
    const fs = require('fs');
    const src = fs.readFileSync('static/app.js', 'utf8');
    const vals = { 'k-slider': __K__ };
    const mkEl = (id) => ({
      id, value: vals[id] !== undefined ? vals[id] : '', checked: false,
      textContent: '', innerHTML: '', hidden: false, disabled: false,
      dataset: {},
      classList: { add(){}, remove(){}, toggle(){}, contains: () => false },
      style: {},
      addEventListener(){}, appendChild(){}, remove(){}, focus(){},
      querySelector: () => mkEl('x'), querySelectorAll: () => [],
    });
    global.document = {
      getElementById: (id) => mkEl(id),
      createElement: () => mkEl('c'),
      addEventListener(){},
      querySelectorAll: () => [],
      querySelector: () => mkEl('x'),
    };
    global.localStorage = { getItem: () => 'sid-x', removeItem(){} };
    global.EventSource = class { addEventListener(){} close(){} };
    let captured = null;
    global.fetch = async (path, opts) => {
      if (String(path).endsWith('/round')) captured = JSON.parse(opts.body);
      return { ok: true, json: async () => ({ round_id: 8, participants: ['qwen'],
                                              peer_review: false }) };
    };
    const api = new Function(src + "\nreturn { state, sendRound };")();
    api.state.sessionId = 'sid-x';
    api.state.models = [{ id: 'qwen', displayName: 'Qwen', selected: true,
                          active: true }];
    api.sendRound();
    setTimeout(() => {
      process.stdout.write(JSON.stringify(captured));
      process.exit(0);
    }, 80);
    """.replace("__K__", json.dumps(k_value))
    proc = subprocess.run(
        [_node(), "-e", script],
        capture_output=True,
        text=True,
        cwd=str(Path(__file__).resolve().parent.parent),
        timeout=60,
    )
    assert proc.returncode == 0, proc.stderr
    body = json.loads(proc.stdout)
    assert body is not None, "sendRound() 未发出 /round 请求"
    return body


def test_e2e_send_round_posts_session_k_019():
    assert _send_round_and_capture("0.19")["k"] == 0.19


def test_e2e_send_round_posts_session_k_050():
    assert _send_round_and_capture("0.50")["k"] == 0.50


if __name__ == "__main__":  # pragma: no cover
    sys.exit(pytest.main([__file__, "-v"]))
