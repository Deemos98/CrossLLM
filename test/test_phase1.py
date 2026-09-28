"""CrossLLM Phase 1 测试（phase1_mock.md §21/§23）。

覆盖：
  T1  三模型并发 streaming
  T2  Round 1 持久化
  T3  CLOSED 模型不参与下一轮
  T4a 可见自己的上一轮
  T4b 可见其他 ACTIVE 模型的匿名上一轮
  T4c 不可见 CLOSED 模型
  T4d context 中零身份信息
  T4e candidate 标签轮内全局一致
  T4f candidate <-> model 映射不外泄
  T4g Mock 语料内容中立
  T5  Round 1 未被 Round 2 覆盖
  T6  刷新后 Session 可恢复
  T7  单模型失败不影响整轮
  T8  匿名化单元

关于「不为测试方便而泄露身份」：
  测试代码可以直接读 state.json 里的 candidate_ids 来知道 Candidate A 是谁 ——
  那是系统内部视角，完全允许。但被测模型只能看到匿名身份，因此断言一律通过
  candidate_ids 反查，绝不在 prompt 里写 [Qwen] / [GPT] 这类真实标签。
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import time
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

import app as app_module
from core.anonymizer import (
    IdentityLeakError,
    assert_no_identity_leak,
    assign_candidate_ids,
    build_anonymous_context,
)
from core.model_manager import ModelRegistry
from core.round_manager import RoundManager
from core.session_manager import SessionManager
from core.storage import Storage

from _capability_stub import StubCapabilityResolver
from providers.mock_provider import MockProvider, content_for

# 测试用问题与指令：本身不含任何模型身份词，
# 这样 T4d 才能对整个 prompt（含问题部分）做断言。
QUESTION = "这个 OCR 架构是否合理？"
INSTRUCTION_R2 = "请评价其他参与者的方案，并说明哪些内容值得吸收。"

# 各模型 Round 1 语料中的独有片段，用于逐字比对
SNIPPET = {
    "qwen": "小规模压测",
    "deepseek": "逐阶段计时",
    "gpt": "连接数上限",
}
DEEPSEEK_MARKERS = ["逐阶段计时", "先补齐观测", "在拿到证据之前"]


# --------------------------------------------------------------------------
# fixtures
# --------------------------------------------------------------------------


@pytest.fixture
def env(tmp_path, monkeypatch):
    """直接操作 core 层，不走 HTTP。"""
    monkeypatch.setenv("CROSSLLM_MOCK_DELAY_SCALE", "0")
    storage = Storage(tmp_path / "sessions")
    registry = ModelRegistry()

    def factory(model_id: str) -> MockProvider:
        return MockProvider(
            model_id,
            fail=model_id in registry.failing_models(),
            delay_scale=0.0,
        )

    return SimpleNamespace(
        storage=storage,
        registry=registry,
        sessions=SessionManager(storage, registry),
        # §一：注入确定性模型能力（生产链路走 Provider 元数据；数值沿用改动前的
        # 内建默认，因此 R2+ 的历史预算数字与行为不变）。
        rounds=RoundManager(
            storage, registry, factory,
            capability_resolver=StubCapabilityResolver(
                context_limit=8192, model_max_output=2048,
            ),
        ),
        root=tmp_path / "sessions",
    )


@pytest.fixture
def client(tmp_path, monkeypatch):
    """HTTP 层测试。把 app 的全局依赖指向临时目录。"""
    monkeypatch.setenv("CROSSLLM_MOCK_DELAY_SCALE", "0")
    storage = Storage(tmp_path / "sessions")
    registry = ModelRegistry()
    monkeypatch.setattr(app_module, "storage", storage)
    monkeypatch.setattr(app_module, "sessions", SessionManager(storage, registry))
    monkeypatch.setattr(
        app_module,
        "rounds",
        RoundManager(
            storage, registry, lambda m: MockProvider(m, delay_scale=0.0),
            capability_resolver=StubCapabilityResolver(
                context_limit=8192, model_max_output=2048,
            ),
        ),
    )
    with TestClient(app_module.app) as c:
        yield c


def run_round(env, session, instruction: str):
    """同步地跑完一轮（prepare + execute）。"""
    round_state, bus = env.rounds.prepare_round(session, instruction)
    asyncio.run(env.rounds.execute_round(session.session_id, round_state, bus))
    return round_state, bus


def two_rounds(env, close_model: str | None = None):
    """跑完 Round 1，可选关闭一个模型，再跑 Round 2。"""
    session = env.sessions.create(QUESTION)
    run_round(env, session, QUESTION)
    if close_model:
        session = env.sessions.set_selection(
            session.session_id,
            [m for m in session.active_models if m != close_model],
        )
    round2, bus2 = run_round(env, session, INSTRUCTION_R2)
    return session, round2, bus2


def digest(path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


# --------------------------------------------------------------------------
# T8 匿名化单元
# --------------------------------------------------------------------------


def test_t8_assign_is_deterministic_and_sorted():
    assert assign_candidate_ids(["gpt", "qwen", "deepseek"]) == {
        "deepseek": "Candidate A",
        "gpt": "Candidate B",
        "qwen": "Candidate C",
    }


def test_t8_assign_is_compact_when_participant_dropped():
    """参与者减少时必须重新紧凑分配，不能留下 A、C 这种空洞。"""
    assert assign_candidate_ids(["qwen", "gpt"]) == {
        "gpt": "Candidate A",
        "qwen": "Candidate B",
    }


def test_t8_build_context_marks_self_and_drops_closed():
    cids = assign_candidate_ids(["qwen", "gpt"])
    entries = build_anonymous_context(
        self_model_id="qwen",
        candidate_ids=cids,
        previous_answers={"qwen": "Q1", "gpt": "G1", "deepseek": "D1"},
    )
    assert len(entries) == 2, "CLOSED 模型不得出现在交叉上下文中"
    by_cid = {e.candidate_id: e for e in entries}
    assert by_cid[cids["qwen"]].is_self is True
    assert by_cid[cids["qwen"]].content == "Q1"
    assert by_cid[cids["gpt"]].is_self is False
    assert by_cid[cids["gpt"]].content == "G1"


def test_t8_missing_previous_answer_is_placeholder():
    cids = assign_candidate_ids(["qwen", "gpt"])
    entries = build_anonymous_context(
        self_model_id="qwen", candidate_ids=cids, previous_answers={}
    )
    assert all(e.content for e in entries)


def test_t8_identity_leak_is_detected(env):
    assert_no_identity_leak("这是一个完全中立的句子。", env.registry.participants)
    with pytest.raises(IdentityLeakError):
        assert_no_identity_leak("我参考了 Qwen 的说法。", env.registry.participants)
    with pytest.raises(IdentityLeakError):
        assert_no_identity_leak("provider 是 mock-gpt。", env.registry.participants)


# --------------------------------------------------------------------------
# T4g Mock 语料内容中立
# --------------------------------------------------------------------------


def test_t4g_mock_corpus_is_identity_free(env):
    for model_id in env.registry.ids():
        for round_id in (1, 2):
            assert_no_identity_leak(content_for(model_id, round_id), env.registry.participants)


def test_mock_corpora_are_distinct(env):
    r1 = {m: content_for(m, 1) for m in env.registry.ids()}
    assert len(set(r1.values())) == 3, "三个 Mock 模型必须产出不同内容"


# --------------------------------------------------------------------------
# T1 / T2 并发与持久化
# --------------------------------------------------------------------------


def test_t1_three_models_stream_concurrently(env):
    session = env.sessions.create(QUESTION)
    round_state, bus = run_round(env, session, QUESTION)

    token_events = [e for e in bus.events if e["type"] == "token"]
    assert token_events, "应当产生 token 事件"

    # 并发判据：第一个 done 之前，已经有多个模型产出过 token。
    # 串行执行时这个值只会是 1。
    first_done = next(i for i, e in enumerate(bus.events) if e["type"] == "done")
    models_before_first_done = {
        e["model"] for e in bus.events[:first_done] if e["type"] == "token"
    }
    assert len(models_before_first_done) >= 2

    assert round_state.status == {m: "complete" for m in env.registry.ids()}


def test_t2_round1_persisted(env):
    session = env.sessions.create(QUESTION)
    run_round(env, session, QUESTION)
    round_dir = env.storage.round_dir(session.session_id, 1)
    for model_id in env.registry.ids():
        assert (round_dir / f"{model_id}.md").exists(), "回答正文必须落盘"
        assert (round_dir / f"{model_id}.prompt.md").exists(), "prompt 必须落盘"
    assert (env.storage.session_dir(session.session_id) / "question.md").exists()


def test_round1_prompt_is_independent(env):
    """P2：Round 1 不得注入任何其他模型的回答。"""
    session = env.sessions.create(QUESTION)
    run_round(env, session, QUESTION)
    for model_id in env.registry.ids():
        prompt = env.storage.load_prompt(session.session_id, 1, model_id)
        for other in env.registry.ids():
            if other != model_id:
                assert content_for(other, 1) not in prompt


# --------------------------------------------------------------------------
# T3 CLOSED 不参与
# --------------------------------------------------------------------------


def test_t3_closed_model_does_not_participate(env):
    session, round2, bus = two_rounds(env, close_model="deepseek")

    assert round2.participants == ["qwen", "gpt"]
    assert "deepseek" not in round2.status
    assert {e["model"] for e in bus.events} == {"qwen", "gpt"}

    round2_dir = env.storage.round_dir(session.session_id, 2)
    assert (round2_dir / "qwen.md").exists()
    assert (round2_dir / "gpt.md").exists()
    assert not (round2_dir / "deepseek.md").exists(), "CLOSED 模型不得产生新调用"

    # P6：历史回答永久保留
    assert (env.storage.round_dir(session.session_id, 1) / "deepseek.md").exists()


# --------------------------------------------------------------------------
# T4a-T4f 匿名交叉注入
# --------------------------------------------------------------------------


def test_t4a_self_previous_answer_visible(env):
    session, round2, _ = two_rounds(env, close_model="deepseek")
    cid_qwen = round2.candidate_ids["qwen"]
    prompt = env.storage.load_prompt(session.session_id, 2, "qwen")
    own_r1 = env.storage.load_responses(session.session_id, 1)["qwen"]
    # 历史按来源轮次分组渲染；块内自己的回答排在最前
    assert "## 第 1 轮" in prompt
    assert f"你的回答 [{cid_qwen}]：\n{own_r1}" in prompt


def test_t4b_peer_previous_answer_visible_under_candidate_id(env):
    session, round2, _ = two_rounds(env, close_model="deepseek")
    cids = round2.candidate_ids
    prompt_qwen = env.storage.load_prompt(session.session_id, 2, "qwen")
    gpt_r1 = env.storage.load_responses(session.session_id, 1)["gpt"]
    assert f"[{cids['gpt']}]\n{gpt_r1}" in prompt_qwen


def test_t4c_closed_model_invisible(env):
    session, round2, _ = two_rounds(env, close_model="deepseek")
    for model_id in ("qwen", "gpt"):
        prompt = env.storage.load_prompt(session.session_id, 2, model_id)
        for marker in DEEPSEEK_MARKERS:
            assert marker not in prompt, f"CLOSED 模型内容泄露进 {model_id} 的 prompt"


def test_t4d_no_identity_leak_in_context(env):
    session, round2, _ = two_rounds(env, close_model="deepseek")
    for model_id in round2.participants:
        prompt = env.storage.load_prompt(session.session_id, 2, model_id)
        assert_no_identity_leak(prompt, env.registry.participants)


def test_t4e_candidate_labels_are_globally_consistent(env):
    """同一个 candidate_id 在所有模型的 prompt 中必须指向同一个模型。

    这是本阶段最容易出错的地方：若按每个模型各自分配标签，
    会出现「Qwen 说 GPT 是 B、而 GPT 自称 A」的错位，交叉讨论无法对齐。
    """
    session, round2, _ = two_rounds(env, close_model="deepseek")
    cids = round2.candidate_ids
    r1 = env.storage.load_responses(session.session_id, 1)

    prompt_qwen = env.storage.load_prompt(session.session_id, 2, "qwen")
    prompt_gpt = env.storage.load_prompt(session.session_id, 2, "gpt")

    # 自己的标签
    assert f"你的回答 [{cids['qwen']}]：\n{r1['qwen']}" in prompt_qwen
    assert f"你的回答 [{cids['gpt']}]：\n{r1['gpt']}" in prompt_gpt
    # 他人标签：两个 prompt 中指向一致
    assert f"[{cids['gpt']}]\n{r1['gpt']}" in prompt_qwen
    assert f"[{cids['qwen']}]\n{r1['qwen']}" in prompt_gpt


def test_t4f_candidate_mapping_not_leaked(env):
    session, round2, _ = two_rounds(env, close_model="deepseek")

    # 1) 映射确实存在，且只在 state.json
    raw = json.loads(env.storage.state_path(session.session_id).read_text(encoding="utf-8"))
    saved_round2 = next(r for r in raw["rounds"] if r["round_id"] == 2)
    assert saved_round2["candidate_ids"] == round2.candidate_ids

    # 2) round_NNN/ 下所有文件都不得含任何身份词元
    #    （映射不外泄的充分条件：映射必然包含 model_id）
    for rid in (1, 2):
        for path in env.storage.round_files(session.session_id, rid):
            assert_no_identity_leak(path.read_text(encoding="utf-8"), env.registry.participants)


# --------------------------------------------------------------------------
# T5 历史不被覆盖
# --------------------------------------------------------------------------


def test_t5_round1_unchanged_after_round2(env):
    session = env.sessions.create(QUESTION)
    run_round(env, session, QUESTION)
    round_dir = env.storage.round_dir(session.session_id, 1)
    before = {p.name: digest(p) for p in sorted(round_dir.iterdir())}

    run_round(env, session, INSTRUCTION_R2)

    after = {p.name: digest(p) for p in sorted(round_dir.iterdir())}
    assert before == after


# --------------------------------------------------------------------------
# T6 Session 恢复
# --------------------------------------------------------------------------


def test_t6_session_recoverable_after_reload(env):
    session, round2, _ = two_rounds(env, close_model="deepseek")

    # 模拟进程重启：用全新的 manager 指向同一目录
    fresh = SessionManager(Storage(env.root), ModelRegistry())
    restored = fresh.get(session.session_id)

    assert restored.session_id == session.session_id
    assert restored.original_question == QUESTION
    assert restored.current_round == 2
    assert restored.active_models == ["qwen", "gpt"]
    assert restored.closed_models == ["deepseek"]
    assert [r.round_id for r in restored.rounds] == [1, 2]
    assert restored.round(2).candidate_ids == round2.candidate_ids

    # Round 1 的三个模型回答都还在（CLOSED 模型的也不删除）
    assert set(env.storage.load_responses(session.session_id, 1)) == set(env.registry.ids())


# --------------------------------------------------------------------------
# T7 单模型失败隔离
# --------------------------------------------------------------------------


def test_t7_single_model_failure_isolated(env, monkeypatch):
    monkeypatch.setenv("CROSSLLM_FAIL_MODEL", "deepseek")
    session = env.sessions.create(QUESTION)
    round_state, bus = run_round(env, session, QUESTION)

    assert round_state.status["deepseek"] == "error"
    assert round_state.status["qwen"] == "complete"
    assert round_state.status["gpt"] == "complete"

    errors = [e for e in bus.events if e["type"] == "error"]
    assert [e["model"] for e in errors] == ["deepseek"]

    # 失败模型已产出的部分内容仍然保留（D5 / §28）
    assert (env.storage.round_dir(session.session_id, 1) / "deepseek.md").exists()

    # 整轮仍然可以继续：Round 2 时 deepseek 因为 status=error
    # 不会被注入给其他模型，只渲染成"无可用回答"
    round2, _ = run_round(env, session, INSTRUCTION_R2)
    assert round2.status["qwen"] == "complete"
    assert round2.status["gpt"] == "complete"


# --------------------------------------------------------------------------
# 用户输入含模型名时不误报
# --------------------------------------------------------------------------


def test_user_question_may_contain_model_names(env):
    """泄露校验只覆盖系统生成部分。用户自己写下模型名不应导致报错。"""
    question = "比较 Qwen 和 GPT 在这个场景下的差异"
    session = env.sessions.create(question)
    round1, _ = run_round(env, session, question)
    assert round1.status == {m: "complete" for m in env.registry.ids()}

    round2, _ = run_round(env, session, INSTRUCTION_R2)
    assert round2.status == {m: "complete" for m in env.registry.ids()}


# --------------------------------------------------------------------------
# HTTP 层
# --------------------------------------------------------------------------


def wait_round_done(client, session_id: str, round_id: int, timeout: float = 15.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        data = client.get(f"/api/session/{session_id}").json()
        r = next((x for x in data["rounds"] if x["round_id"] == round_id), None)
        if r and all(s in ("complete", "error") for s in r["status"].values()):
            return r
        time.sleep(0.02)
    raise AssertionError(f"Round {round_id} 未在超时内完成")


def test_api_create_and_get_session(client):
    res = client.post("/api/session", json={"question": QUESTION})
    assert res.status_code == 200
    sid = res.json()["session_id"]

    res = client.get(f"/api/session/{sid}")
    assert res.status_code == 200
    body = res.json()
    assert body["state"]["original_question"] == QUESTION
    assert body["state"]["active_models"] == ["qwen", "deepseek", "gpt"]
    assert body["state"]["closed_models"] == []


def test_api_round1_and_sse(client):
    sid = client.post("/api/session", json={"question": QUESTION}).json()["session_id"]

    res = client.post(f"/api/session/{sid}/round", json={"instruction": ""})
    assert res.status_code == 202
    body = res.json()
    assert body["round_id"] == 1
    assert body["participants"] == ["qwen", "deepseek", "gpt"]

    round_data = wait_round_done(client, sid, 1)
    assert round_data["status"] == {m: "complete" for m in ("qwen", "deepseek", "gpt")}
    assert set(round_data["responses"]) == {"qwen", "deepseek", "gpt"}


def test_api_sse_replays_events_for_late_subscriber(client):
    """迟到的订阅者不能漏 chunk（回放缓冲）。"""
    sid = client.post("/api/session", json={"question": QUESTION}).json()["session_id"]
    rid = client.post(f"/api/session/{sid}/round", json={"instruction": ""}).json()["round_id"]
    wait_round_done(client, sid, rid)

    # 整轮结束后才连上来，仍然应当拿到完整事件流
    with client.stream("GET", f"/api/session/{sid}/events/{rid}") as stream:
        payload = "".join(stream.iter_text())

    assert 'event: token' in payload
    assert 'event: done' in payload
    assert '"model": "qwen"' in payload
    assert '"model": "deepseek"' in payload
    assert '"model": "gpt"' in payload


def test_api_selection_and_round2(client):
    sid = client.post("/api/session", json={"question": QUESTION}).json()["session_id"]
    rid1 = client.post(f"/api/session/{sid}/round", json={"instruction": ""}).json()["round_id"]
    wait_round_done(client, sid, rid1)

    res = client.post(f"/api/session/{sid}/selection", json={"active_models": ["qwen", "gpt"]})
    assert res.status_code == 200
    assert res.json()["state"]["closed_models"] == ["deepseek"]

    rid2 = client.post(
        f"/api/session/{sid}/round", json={"instruction": INSTRUCTION_R2}
    ).json()["round_id"]
    round2 = wait_round_done(client, sid, rid2)

    assert round2["participants"] == ["qwen", "gpt"]
    assert set(round2["responses"]) == {"qwen", "gpt"}


def test_api_rounds_exclude_candidate_mapping(client):
    """不变量 A4：candidate_ids 不得通过 API 外泄。"""
    sid = client.post("/api/session", json={"question": QUESTION}).json()["session_id"]
    rid1 = client.post(f"/api/session/{sid}/round", json={"instruction": ""}).json()["round_id"]
    wait_round_done(client, sid, rid1)
    rid2 = client.post(
        f"/api/session/{sid}/round", json={"instruction": INSTRUCTION_R2}
    ).json()["round_id"]
    wait_round_done(client, sid, rid2)

    body = client.get(f"/api/session/{sid}").json()
    for r in body["rounds"]:
        assert "candidate_ids" not in r
    assert "candidate_ids" not in body["state"]


def test_api_unknown_session_returns_404(client):
    assert client.get("/api/session/does-not-exist").status_code == 404


def test_api_rejects_empty_question(client):
    assert client.post("/api/session", json={"question": "   "}).status_code == 400
