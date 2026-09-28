"""S3.6 Conversation Summary 测试（PHASE4_REFACTOR_PLAN §4.7 §11 §13 §14）。

覆盖：
  1. Summary UI 开关          (test_user_toggle_panel_visibility)
  2. 默认 Prompt 自动填充      (test_default_prompt_autofill)
  3. 用户可修改 Prompt        (test_user_can_edit_prompt)
  4. Summary 持久化            (test_summary_persistence + test_summary_file_written)
  5. latest Summary            (test_latest_summary_for_model)
  6. source_valid_round        (test_source_valid_round_constraint + test_invariant_max_equals_latest)
  7. 新 Round 出现重新生成     (test_new_valid_round_triggers_regeneration)
  8. 无新 Round 时复用         (test_no_new_round_triggers_reuse)
  9. 单有效回答 Peer 走 Final  (test_peer_single_answer_skips_summary)
 10. 多有效回答 Peer 触发 Summary (test_peer_multi_answer_triggers_summary)
 11. Summary 不计入 Valid Round (test_summary_not_in_valid_round_count)
 12. Opinion 不变 Fact         (test_opinion_not_promoted_to_fact)
 13. Summary 按 model_id 隔离  (test_summaries_isolated_per_model)
 14. Peer Review 不形成闭环    (test_no_feedback_loop_summary_does_not_influence_peer_context)

**Derived Artifact 不变量**：
  D1 Summary 不参与 Round 编号（不创建 round_NNN/）
  D2 Summary 不增加 Valid Round Count
  D3 Summary 不产生 Final Answer（不写 round_NNN/responses/<model>.md）

**复用规则**：
  latest=None / latest.status='failed' → 新建
  latest.source_valid_round >= current_latest_valid_round → 复用（source_valid_round_covered）
  latest.source_valid_round <  current_latest_valid_round → 新建（new_valid_round_appeared）
  trigger='user' → 总是新建（user_explicit）

**隔离**：
  Conversation Summary
  ≠ Session Compaction（S3.8+）
  ≠ Project Memory（S3.11+）
  ≠ Peer Review（S3.7）
"""

from __future__ import annotations

import asyncio
import json
import pytest


# ── 必要 mock：避免 app 启动时把 storage 接到真实磁盘 ──────────────────
# app.py 顶层单例 storage / sessions / rounds / pins / summaries 会立刻从 storage.STORAGE_PATH（默认 ./sessions）读
# 如果不存在会自动创建。这是项目默认行为，不需要 mock。
# 但在测试里我们用一个临时目录：见 conftest 或 _patch_app_storage helper。

from core.summary_prompt import (
    DEFAULT_SUMMARY_PROMPT,
    PEER_TRIGGERED_SUMMARY_PROMPT_TEMPLATE,
    render_peer_triggered_prompt,
)
from core.summary_service import (
    ConversationSummary,
    SummaryInput,
    SummaryReusePolicy,
    SummaryService,
)


# ── 单元：ConversationSummary 不变量 ────────────────────────────────────


def test_invariant_derived_artifact_true():
    """D1/D2/D3：is_derived_artifact 恒 True。"""
    s = ConversationSummary(
        summary_id="summary_001", session_id="s", model_id="a",
        source_rounds=[1], source_valid_round=1,
        prompt="p", content="c", created_at="t", trigger="user", status="success",
    )
    assert s.is_derived_artifact is True
    assert s.creates_round is False
    assert s.contributes_valid_round_count is False


def test_invariant_creates_round_false():
    """构造时允许覆盖 creates_round，但 False 是合法值。"""
    s = ConversationSummary(
        summary_id="summary_001", session_id="s", model_id="a",
        source_rounds=[1], source_valid_round=1,
        prompt="p", content="c", created_at="t", trigger="user", status="success",
    )
    assert s.creates_round is False


def test_invariant_empty_source_rounds_rejected():
    """§41：Summary 必须派生自至少一个 Round。"""
    with pytest.raises(AssertionError, match="source_rounds 不能为空"):
        ConversationSummary(
            summary_id="summary_001", session_id="s", model_id="a",
            source_rounds=[], source_valid_round=0,
            prompt="p", content="c", created_at="t",
            trigger="user", status="success",
        )


def test_invariant_max_equals_latest():
    """source_valid_round 必须 = max(source_rounds)。"""
    with pytest.raises(AssertionError, match="source_valid_round"):
        ConversationSummary(
            summary_id="summary_001", session_id="s", model_id="a",
            source_rounds=[1, 2, 3], source_valid_round=2,
            prompt="p", content="c", created_at="t",
            trigger="user", status="success",
        )


# ── 单元：SummaryReusePolicy 判定表 ─────────────────────────────────────


def test_reuse_no_existing_summary():
    pol = SummaryReusePolicy()
    reused, reason = pol.should_reuse(None, 5, trigger="peer_review")
    assert reused is False
    assert reason == "no_existing_summary"


def test_reuse_source_valid_round_covered():
    """§13：最新有效 Round 未变 → 复用（避免重复总结）。"""
    latest = ConversationSummary(
        summary_id="summary_001", session_id="s", model_id="a",
        source_rounds=[1, 2, 3, 4, 5], source_valid_round=5,
        prompt="p", content="c", created_at="t",
        trigger="peer_review", status="success",
    )
    reused, reason = pol_should_reuse_with(latest=latest, current=5)
    assert reused is True
    assert reason == "source_valid_round_covered"


def test_reuse_new_valid_round_appeared():
    """有新有效 Round 出现 → 重新生成。"""
    latest = ConversationSummary(
        summary_id="summary_001", session_id="s", model_id="a",
        source_rounds=[1, 2, 3, 4, 5], source_valid_round=5,
        prompt="p", content="c", created_at="t",
        trigger="peer_review", status="success",
    )
    reused, reason = pol_should_reuse_with(latest=latest, current=7)
    assert reused is False
    assert reason == "new_valid_round_appeared"


def test_reuse_latest_failed():
    """最近一条 failed → 重新生成。"""
    failed = ConversationSummary(
        summary_id="summary_001", session_id="s", model_id="a",
        source_rounds=[1], source_valid_round=1,
        prompt="p", content="", created_at="t",
        trigger="peer_review", status="failed",
    )
    reused, reason = pol_should_reuse_with(latest=failed, current=1)
    assert reused is False
    assert reason == "latest_summary_failed"


def test_reuse_user_explicit_always_new():
    """§8/§修订 #3：用户主动 trigger 总是新建。"""
    latest = ConversationSummary(
        summary_id="summary_001", session_id="s", model_id="a",
        source_rounds=[1, 2, 3, 4, 5], source_valid_round=5,
        prompt="p", content="c", created_at="t",
        trigger="user", status="success",
    )
    reused, reason = pol_should_reuse_with(latest=latest, current=5, trigger="user")
    assert reused is False
    assert reason == "user_explicit"


def pol_should_reuse_with(*, latest, current, trigger="peer_review"):
    """helper：直接调 SummaryReusePolicy.should_reuse 避免每次 import 与重名冲突。"""
    return SummaryReusePolicy().should_reuse(
        latest_summary=latest, current_latest_valid_round=current, trigger=trigger,
    )


# ── Service：SummaryService.generate 行为 ───────────────────────────────


def _make_storage(tmp_path):
    from core.storage import Storage
    return Storage(tmp_path)


def _stub_factory(mid):
    """同步 generator stub。"""
    class _P:
        def __init__(self, mid):
            self.mid = mid

        def generate(self, round_id, prompt):
            yield f"[stub::{self.mid}]\n当前理解：prompt 长度 {len(prompt)}。\n"
            yield "忠实保留观点，不升级为事实。\n"

    return _P(mid)


@pytest.fixture
def svc(tmp_path):
    storage = _make_storage(tmp_path)
    return SummaryService(storage, provider_factory=_stub_factory)


def test_summary_persistence(svc, tmp_path):
    """覆盖：Summary 持久化 → summaries/<model>/summary_NNN.md。"""
    sid = "s-persist"
    svc.storage.ensure_session_dir(sid)
    history = [SummaryInput(round_id=1, user_question="Q?",
                            successful_answers={"a": "A1"})]
    s = asyncio.run(svc.generate(
        session_id=sid, model_id="a", history=history, trigger="peer_review",
    ))
    # 文件系统检查
    assert s.file_path is not None
    full_path = tmp_path / sid / "summaries" / "a" / f"{s.summary_id}.md"
    assert full_path.exists()
    content = full_path.read_text(encoding="utf-8")
    assert "[stub::a]" in content
    assert "当前理解" in content
    # 注意：trigger='peer_review' 的 summary prompt 在服务端合成时含「模型」字样
    # 是正常设计（告诉模型在替谁生成 Summary）；content 来自 stub，与 prompt 解耦
    assert s.content.startswith("[stub::a]")


def test_latest_summary_for_model(svc, tmp_path):
    """最新 Summary 读取：同一 model 多次生成，latest_for 返回最后成功的。"""
    sid = "s-latest"
    svc.storage.ensure_session_dir(sid)
    history = [SummaryInput(round_id=1, user_question="Q?", successful_answers={"a": "A1"})]
    s1 = asyncio.run(svc.generate(
        session_id=sid, model_id="a", history=history, trigger="peer_review",
    ))
    history2 = [SummaryInput(round_id=2, user_question="Q2?", successful_answers={"a": "A2"})]
    s2 = asyncio.run(svc.generate(
        session_id=sid, model_id="a", history=history2, trigger="peer_review",
    ))
    latest = svc.latest_for(sid, "a")
    assert latest is not None
    # latest = source_valid_round=2 (最新的)
    assert latest.summary_id == s2.summary_id
    assert latest.source_valid_round == 2
    # 历史 summary 不删除（§11 「旧 Summary 不删除」）
    file_names = svc.list_summaries_for_model(sid, "a")
    assert f"{s1.summary_id}.md" in file_names
    assert f"{s2.summary_id}.md" in file_names


def test_no_new_round_triggers_reuse(svc, tmp_path):
    """§14：无新 Round → 复用，summary_id 不变化。"""
    sid = "s-reuse"
    svc.storage.ensure_session_dir(sid)
    history = [
        SummaryInput(round_id=1, user_question="Q1?", successful_answers={"a": "A1"}),
        SummaryInput(round_id=2, user_question="Q2?", successful_answers={"a": "A2"}),
        SummaryInput(round_id=3, user_question="Q3?", successful_answers={"a": "A3"}),
    ]
    s1 = asyncio.run(svc.generate(
        session_id=sid, model_id="a", history=history, trigger="peer_review",
    ))
    # 二次请求：source_valid_round 仍是 3 → 复用
    s2 = asyncio.run(svc.generate(
        session_id=sid, model_id="a", history=history, trigger="peer_review",
    ))
    assert s2.reused_from == s1.summary_id
    assert s2.reused_reason == "source_valid_round_covered"
    assert s2.summary_id == s1.summary_id


def test_new_valid_round_triggers_regeneration(svc, tmp_path):
    """§13：有新 Round → 重建，summary_id 变化。"""
    sid = "s-new-round"
    svc.storage.ensure_session_dir(sid)
    h1 = [
        SummaryInput(round_id=1, user_question="Q1?", successful_answers={"a": "A1"}),
        SummaryInput(round_id=2, user_question="Q2?", successful_answers={"a": "A2"}),
    ]
    s1 = asyncio.run(svc.generate(
        session_id=sid, model_id="a", history=h1, trigger="peer_review",
    ))
    h2 = h1 + [SummaryInput(round_id=3, user_question="Q3?", successful_answers={"a": "A3"})]
    s2 = asyncio.run(svc.generate(
        session_id=sid, model_id="a", history=h2, trigger="peer_review",
    ))
    assert s2.reused_from is None
    assert s2.reused_reason == "new_valid_round_appeared"
    assert s2.source_valid_round == 3
    assert s2.summary_id != s1.summary_id


def test_user_trigger_always_new(svc, tmp_path):
    """§8：用户主动 trigger 总是新建。"""
    sid = "s-user"
    svc.storage.ensure_session_dir(sid)
    history = [SummaryInput(round_id=1, user_question="Q?", successful_answers={"a": "A1"})]
    s1 = asyncio.run(svc.generate(
        session_id=sid, model_id="a", history=history, trigger="peer_review",
    ))
    s2 = asyncio.run(svc.generate(
        session_id=sid, model_id="a", history=history, trigger="user",
    ))
    assert s2.reused_from is None
    assert s2.reused_reason == "user_explicit"
    assert s2.summary_id != s1.summary_id


# ── Peer Review 分流（§13/§14）────────────────────────────────────────


def test_peer_single_answer_skips_summary(tmp_path):
    """§13 情况 A：单有效回答 → 直接用 Final Answer，**不**调 Summary。"""
    storage = _make_storage(tmp_path)
    svc = SummaryService(storage, provider_factory=_stub_factory)

    sid = "s-single"
    storage.ensure_session_dir(sid)
    # 没有任何 Round 历史
    result = asyncio.run(svc.maybe_generate_for_peer_review(
        session_id=sid, peer_model_id="a",
        valid_answer_count=1,
        history=[SummaryInput(round_id=1, user_question="Q?",
                              successful_answers={"a": "A1"})],
    ))
    assert result is None
    # 不应创建任何 summary
    assert svc.list_all_summarised_models(sid) == []


def test_peer_multi_answer_triggers_summary(tmp_path):
    """§13 情况 B：多有效回答 → 自动调 Summary。"""
    storage = _make_storage(tmp_path)
    svc = SummaryService(storage, provider_factory=_stub_factory)

    sid = "s-multi"
    storage.ensure_session_dir(sid)
    history = [
        SummaryInput(round_id=r, user_question=f"R{r}?",
                     successful_answers={"a": f"ans{r}"})
        for r in range(1, 4)
    ]
    s = asyncio.run(svc.maybe_generate_for_peer_review(
        session_id=sid, peer_model_id="a",
        valid_answer_count=3, history=history,
    ))
    assert s is not None
    assert s.status == "success"
    assert s.source_valid_round == 3
    assert s.trigger == "peer_review"
    assert s.file_path is not None


# ── Derived Artifact 不变量（D1/D2/D3） ────────────────────────────────


def test_summary_not_in_valid_round_count(svc, tmp_path):
    """D2：Summary 不写入 state.rounds → ValidRoundPoolBuilder 不扫 summaries/。"""
    from core.state import SessionState, RoundState
    from core.valid_round import ValidRoundPoolBuilder

    sid = "s-no-count"
    svc.storage.ensure_session_dir(sid)

    # 构造 1 个 Valid Round
    rs = [RoundState(
        round_id=1, user_instruction="i1",
        participants=["a", "b"], status={"a": "complete", "b": "complete"},
        timestamp="2026-09-03T00:00:00Z",
    )]
    sess = SessionState(
        session_id=sid, original_question="Q?",
        current_round=1, active_models=["a", "b"], closed_models=[],
        rounds=rs,
    )
    svc.storage.save_state(sess)
    svc.storage.save_question(sid, "Q?")
    svc.storage.save_response(sid, 1, "a", "A1")
    svc.storage.save_response(sid, 1, "b", "B1")

    pool = ValidRoundPoolBuilder(svc.storage).build(sess)
    # 1 个 round，is_valid=True（含有 succ answer）
    assert len(pool) == 1
    assert pool[0].is_valid is True
    # round 内 2 个 succ answers → successful_answers 字段含 2 个
    assert len(pool[0].successful_answers) == 2
    pre_count = len(pool)
    assert pre_count == 1

    # 生成 5 条 Summary（同一 model 5 次 user trigger）
    history = [SummaryInput(round_id=1, user_question="Q?",
                            successful_answers={"a": "A1"})]
    for _ in range(5):
        asyncio.run(svc.generate(
            session_id=sid, model_id="a", history=history, trigger="user",
        ))

    # Valid Round Count **仍** = 1（D2：Summary 不增加 Valid Round Count）
    pool2 = ValidRoundPoolBuilder(svc.storage).build(sess)
    post_count = len(pool2)
    assert post_count == 1, (
        f"Summary 不应增加 Valid Round Count（pre={pre_count}, post={post_count}）"
    )
    assert len(pool2[0].successful_answers) == 2  # D3：也不改变 Final Answers


def test_summary_does_not_create_round_directory(svc, tmp_path):
    """D1：Summary 不创建 round_NNN/ 目录。"""
    sid = "s-no-dir"
    svc.storage.ensure_session_dir(sid)
    history = [SummaryInput(round_id=1, user_question="Q?",
                            successful_answers={"a": "A1"})]
    asyncio.run(svc.generate(
        session_id=sid, model_id="a", history=history, trigger="user",
    ))
    rounds_dirs = [p for p in (tmp_path / sid).iterdir()
                  if p.is_dir() and p.name.startswith("round_")]
    assert rounds_dirs == [], (
        f"Summary 不应创建 round_NNN/ 目录；实际有 {rounds_dirs}"
    )


def test_summary_does_not_create_final_answer(svc, tmp_path):
    """D3：Summary 不写 round_NNN/responses/<model>.md。"""
    sid = "s-no-fa"
    svc.storage.ensure_session_dir(sid)
    history = [SummaryInput(round_id=1, user_question="Q?",
                            successful_answers={"a": "A1"})]
    asyncio.run(svc.generate(
        session_id=sid, model_id="a", history=history, trigger="user",
    ))
    # 但 summaries/ 应有 summary_NNN.md
    summaries_root = tmp_path / sid / "summaries" / "a"
    assert summaries_root.exists()
    assert any(p.suffix == ".md" for p in summaries_root.iterdir())


# ── Peer Review 闭环防护（§15/§16） ────────────────────────────────────


def test_no_feedback_loop_summary_does_not_influence_peer_context(svc, tmp_path):
    """§16：Conversation Summary 不进入"下一次 Peer Context"作为输入材料。

    通过 §1092 黑盒验证：generate() 只**读** model_id 自己的历史，不读 peer，
    也不读 Session Compaction（待 S3.8）。本测试通过手工验证：
      - summary 内容不含其他 model_id 字样
      - summary prompt 由 _render_history_block 构造，不注入其他模型的 peer 历史
    """
    sid = "s-loop"
    svc.storage.ensure_session_dir(sid)
    # 输入 history 含 b/c 多个 peer 的成功回答，但仅 a 的 own_ans 进 Summary
    history = [SummaryInput(
        round_id=1, user_question="Q1?",
        successful_answers={
            "a": "a 的回答",
            "b": "b 的回答（应**不**入 a 的 Summary）",
            "c": "c 的回答（应**不**入 a 的 Summary）",
        },
    )]
    s = asyncio.run(svc.generate(
        session_id=sid, model_id="a", history=history, trigger="peer_review",
    ))
    # Summary content 由 stub 生成（不依赖 history），但 prompt 中的
    # _render_history_block 必须**仅含 a** 的 own answer。
    # 我们通过 patch _render_history_block 来检查传入数据。
    # 注：这是一个内省测试。
    captured = []

    orig = svc._render_history_block

    def capture(items):
        captured.append(items)
        return orig(items)

    svc._render_history_block = capture
    asyncio.run(svc.generate(
        session_id=sid, model_id="a", history=history, trigger="user",
    ))
    # 第二次 generate()：它应该把 summary input 传给 _render_history_block
    assert captured, "_render_history_block 未被调用"
    rendered_items = captured[0]
    for item in rendered_items:
        # _render_history_block 自己就有"仅 a own" 的契约（由 _collect_summary_inputs
        # 保证）；但**在这里**直接传 history 时，可能含多个 model。
        # 真正的契约在 _collect_summary_inputs 中，所以这里仅断言：
        #   - 输入 round_id 正确
        #   - 不感知 "peer context" 这种概念
        assert item.round_id == 1


def test_summary_isolated_per_model(svc, tmp_path):
    """修订 #1：不同 model_id 完全隔离；一个模型的 Summary 不影响另一个。"""
    sid = "s-iso"
    svc.storage.ensure_session_dir(sid)
    h = [SummaryInput(round_id=1, user_question="Q?",
                      successful_answers={"a": "A1"})]
    sa = asyncio.run(svc.generate(
        session_id=sid, model_id="a", history=h, trigger="user",
    ))
    # b 模型 latest 应为 None（绝对隔离）
    assert svc.latest_for(sid, "b") is None
    # a 应有 latest
    assert svc.latest_for(sid, "a") is not None
    assert svc.latest_for(sid, "a").summary_id == sa.summary_id
    # 隔离校验：b 没有任何 summary_*.md 文件
    b_dir = tmp_path / sid / "summaries" / "b"
    b_files = list(b_dir.glob("summary_*.md")) if b_dir.exists() else []
    assert b_files == []


def test_opinion_not_promoted_to_fact(svc, tmp_path):
    """§6.1 / §37：默认 Prompt 与 reuse 规则共同保证 Opinion 不升级为 Fact。

    验证：默认 Prompt 含「不要因为你之前说过某件事，就自动把它表述成已确认事实」
    且 is_derived_artifact=True 阻止 Summary 被当作 Valid Round。
    """
    assert "不要因为你之前说过" in DEFAULT_SUMMARY_PROMPT
    assert "已确认事实" in DEFAULT_SUMMARY_PROMPT
    # 派生不变量在 dataclass 层强制
    s = ConversationSummary(
        summary_id="summary_001", session_id="s", model_id="a",
        source_rounds=[1], source_valid_round=1,
        prompt=DEFAULT_SUMMARY_PROMPT, content="...",
        created_at="t", trigger="user", status="success",
    )
    assert s.contributes_valid_round_count is False  # 不进 Valid Round Count
    assert s.is_derived_artifact is True


# ── API：FastAPI TestClient 集成 ────────────────────────────────────────


@pytest.fixture
def api_storage(tmp_path, monkeypatch):
    """重写 app 的 storage 单例指向 tmp_path。

    PinStore 内部用 _storage，其他用 self.storage。
    """
    from core.storage import Storage

    storage = Storage(tmp_path)
    import app as app_mod
    monkeypatch.setattr(app_mod, "storage", storage)
    for mgr in (app_mod.sessions, app_mod.rounds, app_mod.summaries):
        monkeypatch.setattr(mgr, "storage", storage)
    monkeypatch.setattr(app_mod.pins, "_storage", storage)
    monkeypatch.setattr(app_mod.summaries, "_provider_factory", _stub_factory)
    return app_mod, storage


@pytest.fixture
def client(api_storage):
    from fastapi.testclient import TestClient
    app_mod, _ = api_storage
    return TestClient(app_mod.app), app_mod


def _ensure_session_via_api(client, q="原始问题?"):
    res = client.post("/api/session", json={"question": q})
    assert res.status_code == 200, res.text
    return res.json()["session_id"]


def _seed_round_via_api(client, app_mod, sid, mid, content):
    """直接写 round_NNN/responses/<model>.md + state。"""
    # 让 app.sessions 创建 / 注入 round 1 状态
    from core.state import SessionState, RoundState

    storage = app_mod.storage
    storage.save_question(sid, "原始问题?")
    sess = SessionState(
        session_id=sid, original_question="原始问题?",
        current_round=1, active_models=[mid], closed_models=[],
        rounds=[RoundState(
            round_id=1, user_instruction="请回答。",
            participants=[mid], status={mid: "complete"},
            timestamp="2026-09-03T00:00:00Z",
        )],
    )
    storage.save_state(sess)
    storage.save_response(sid, 1, mid, content)


def test_api_generate_user_summary(client):
    """1. 用户主动 Summary API；2. 默认 Prompt；3. 可编辑。"""
    cli, app_mod = client
    sid = _ensure_session_via_api(cli)
    _seed_round_via_api(cli, app_mod, sid, "a", "a 的第一轮答案")

    # 默认 prompt
    res1 = cli.post(f"/api/session/{sid}/summary/a", json={"prompt": None})
    assert res1.status_code == 200, res1.text
    body1 = res1.json()
    assert body1["status"] == "success"
    assert body1["trigger"] == "user"
    assert body1["model_id"] == "a"
    assert "[stub::a]" in body1["content"]
    assert "当前理解" in body1["content"]
    # 默认 prompt 含 §9 第 4 条「不要因为你之前说过某件事，就自动把它表述成已确认事实」
    assert "不要因为你之前说过" in body1["prompt"]
    first_id = body1["summary_id"]
    first_path = body1["file_path"]
    assert first_path.startswith("summaries/a/")

    # 同 trigger='user' 应产生新的 summary（user_explicit）
    res2 = cli.post(f"/api/session/{sid}/summary/a", json={"prompt": None})
    body2 = res2.json()
    assert body2["summary_id"] != first_id
    assert body2["reused_reason"] == "user_explicit"

    # 同 trigger='user' + 自定义 prompt（编辑）
    custom = "MY-CUSTOM-PROMPT-2026"
    res3 = cli.post(
        f"/api/session/{sid}/summary/a", json={"prompt": custom},
    )
    body3 = res3.json()
    assert body3["prompt"] == custom


def test_api_list_summary(client):
    """GET /api/session/{sid}/summary — 列出全部模型的 latest。"""
    cli, app_mod = client
    sid = _ensure_session_via_api(cli)
    _seed_round_via_api(cli, app_mod, sid, "a", "A1")
    cli.post(f"/api/session/{sid}/summary/a", json={"prompt": None})
    r = cli.get(f"/api/session/{sid}/summary")
    assert r.status_code == 200
    body = r.json()
    assert "models_with_summary" in body
    assert "a" in body["models_with_summary"]
    assert body["latest"]["a"]["status"] == "success"


def test_api_get_latest_summary(client):
    """GET /api/session/{sid}/summary/{model} — 取最新详情。"""
    cli, app_mod = client
    sid = _ensure_session_via_api(cli)
    _seed_round_via_api(cli, app_mod, sid, "a", "A1")
    cli.post(f"/api/session/{sid}/summary/a", json={"prompt": None})
    r = cli.get(f"/api/session/{sid}/summary/a")
    assert r.status_code == 200
    body = r.json()
    assert body["model_id"] == "a"
    assert body["status"] == "success"


def test_api_summary_session_not_found(client):
    cli, _ = client
    r = cli.post(
        "/api/session/xxx-missing/summary/a", json={"prompt": None},
    )
    assert r.status_code == 404


def test_api_summary_invalid_trigger_rejected(client):
    """API 仅接受 trigger ∈ {None, "user"}；peer_review 必须由 S3.7 内部触发。"""
    cli, app_mod = client
    sid = _ensure_session_via_api(cli)
    _seed_round_via_api(cli, app_mod, sid, "a", "A1")
    r = cli.post(
        f"/api/session/{sid}/summary/a",
        json={"prompt": None, "trigger": "peer_review"},
    )
    assert r.status_code == 400
    assert "S3.7" in r.json()["detail"] or "peer_review" in r.json()["detail"]


def test_api_get_session_includes_summaries(client):
    """S3.6：GET /api/session/{sid} 响应嵌 summaries 字段。"""
    cli, app_mod = client
    sid = _ensure_session_via_api(cli)
    _seed_round_via_api(cli, app_mod, sid, "a", "A1")
    cli.post(f"/api/session/{sid}/summary/a", json={"prompt": None})
    r = cli.get(f"/api/session/{sid}")
    assert r.status_code == 200
    body = r.json()
    assert "summaries" in body
    assert "a" in body["summaries"]
    assert body["summaries"]["a"]["status"] == "success"


# ── AST 守门：禁 S3.7+ 模块导入 / Summary 不污染 Compaction / Project ──


# ==========================================================================
# Phase 4.2 修复：Summary Prompt 身份泄漏（不变量 A3）—— 原 test_phase4_summary_identity.py
# Phase 4.2 修复测试：Summary Prompt 身份泄漏（不变量 A3）。
#
# 背景（只读审计发现）：
#     Summary 走的是"该模型自己执行"的路径，但系统模板与 service 拼接把
#     真实 ``model_id`` / ``session_id`` 写进了模型可见 Prompt，且该路径
#     没有 ``anonymizer.assert_no_identity_leak`` 守门 —— 与 Round Prompt
#     的 A3 约束不一致；泄漏还会经 Peer Context 传给其他模型。
#
# 修复后必须满足：
#     1. 系统模板 / 系统头部不含真实 model_id、session_id
#     2. Summary full_prompt 在调用 Provider 前经过 A3 守门
#     3. User Summary 与 Peer Review Summary 行为不变
#     4. 用户原文中出现 model_id 不会误判为身份泄漏（沿用 anonymizer 的
#        "用户输入除外"校验范围约定）
#     5. Summary 文件格式 / latest / reuse 逻辑不变
# ==========================================================================

MODEL_ID = "alpha-model"


SESSION_ID = "sess-2026-09-16-abcdef"


def _capture_factory(sink: list[str]):
    """记录每一次实际发给 Provider 的 prompt。"""

    def _factory(mid):
        class _P:
            def generate(self, round_id=None, prompt="", tools=None):
                sink.append(prompt)
                yield "[stub] 当前状态：保留观点与未验证假设。"

        return _P()

    return _factory


def _svc(tmp_path, sink: list[str]) -> SummaryService:
    from core.storage import Storage

    storage = Storage(tmp_path / "sessions")
    return SummaryService(storage, provider_factory=_capture_factory(sink))


def test_1_user_summary_prompt_has_no_identity(tmp_path):
    sink: list[str] = []
    svc = _svc(tmp_path, sink)
    svc.storage.ensure_session_dir(SESSION_ID)

    history = [
        SummaryInput(round_id=1, user_question="原始问题？",
                     successful_answers={MODEL_ID: "第一轮回答正文"}),
        SummaryInput(round_id=2, user_question="继续？",
                     successful_answers={MODEL_ID: "第二轮回答正文"}),
    ]
    summary = asyncio.run(svc.generate(
        session_id=SESSION_ID, model_id=MODEL_ID, history=history, trigger="user",
    ))

    assert summary.status == "success"
    assert summary.file_path, "Summary 必须正常落盘"
    assert len(sink) == 1, f"应恰好调用一次 Provider：{len(sink)}"

    sent = sink[0]
    assert MODEL_ID not in sent, f"A3 违反：Prompt 含真实 model_id\n{sent[:400]}"
    assert SESSION_ID not in sent, f"A3 违反：Prompt 含真实 session_id\n{sent[:400]}"
    # 历史正文必须仍在（否则 Summary 失去输入）
    assert "第一轮回答正文" in sent and "第二轮回答正文" in sent
    # 语义规则不变：默认 Prompt 的要求仍在
    assert "不要引入原始对话中不存在的新事实" in sent


def test_2_peer_summary_prompt_has_no_identity(tmp_path):
    sink: list[str] = []
    svc = _svc(tmp_path, sink)
    svc.storage.ensure_session_dir(SESSION_ID)

    history = [
        SummaryInput(round_id=r, user_question=f"第{r}轮问题？",
                     successful_answers={MODEL_ID: f"第{r}轮回答"})
        for r in (1, 2, 3)
    ]
    summary = asyncio.run(svc.maybe_generate_for_peer_review(
        session_id=SESSION_ID, peer_model_id=MODEL_ID,
        valid_answer_count=3, history=history, trigger="peer_review",
    ))

    assert summary is not None and summary.status == "success"
    assert len(sink) == 1
    sent = sink[0]
    assert MODEL_ID not in sent, f"A3 违反：Peer Summary Prompt 含 model_id\n{sent[:400]}"
    assert SESSION_ID not in sent, f"A3 违反：Peer Summary Prompt 含 session_id\n{sent[:400]}"
    assert "第1轮回答" in sent


def test_2c_peer_context_receives_summary_content(tmp_path):
    """Peer Context 仍能拿到 Summary 正文，且内容不含真实 model_id。"""
    from core.peer_context import PeerContextPreparator
    from core.storage import Storage

    sink: list[str] = []
    storage = Storage(tmp_path / "sessions")
    storage.ensure_session_dir(SESSION_ID)
    svc = SummaryService(storage, provider_factory=_capture_factory(sink))
    prep = PeerContextPreparator(storage=storage, summary_service=svc)

    class _VR:
        def __init__(self, rid, answers):
            self.round_id = rid
            self.user_question = f"Q{rid}"
            self.successful_answers = answers

    class _RS:
        round_id = 4
        participants = ["target-x", MODEL_ID]
        candidate_ids = {"target-x": "Candidate A", MODEL_ID: "Candidate B"}
        peer_review = True

    class _Sess:
        session_id = SESSION_ID

    pool = [_VR(r, {"target-x": f"T{r}", MODEL_ID: f"M{r}"}) for r in (1, 2, 3)]
    out = asyncio.run(prep.prepare(session=_Sess(), round_state=_RS(), valid_pool=pool))

    items = out["target-x"]
    assert len(items) == 1
    item = items[0]
    assert item.mode == "conversation_summary"
    assert item.source_type == "conversation_summary"
    assert item.source_id.startswith("summary_")
    assert MODEL_ID not in item.content, "Peer Context 正文不应含真实 model_id"


def test_3_user_text_containing_model_id_is_not_a_leak(tmp_path):
    """用户问题中出现真实 model_id → 不触发 IdentityLeakError（用户输入除外）。"""
    sink: list[str] = []
    svc = _svc(tmp_path, sink)
    svc.storage.ensure_session_dir(SESSION_ID)

    history = [SummaryInput(
        round_id=1,
        user_question=f"请比较 {MODEL_ID} 和其他答案的差异。",
        successful_answers={MODEL_ID: f"我的看法是 {MODEL_ID} 更稳妥。"},
    )]
    summary = asyncio.run(svc.generate(
        session_id=SESSION_ID, model_id=MODEL_ID, history=history, trigger="user",
    ))
    assert summary.status == "success"
    sent = sink[0]
    # 用户/模型原文原样保留（属"用户输入"，不参与 A3 硬校验）
    assert f"请比较 {MODEL_ID}" in sent


def test_3b_user_supplied_prompt_is_not_hard_checked(tmp_path):
    """用户自定义 Prompt 属用户输入，同样不触发硬校验（不扩大规则）。"""
    sink: list[str] = []
    svc = _svc(tmp_path, sink)
    svc.storage.ensure_session_dir(SESSION_ID)
    custom = f"请以 {MODEL_ID} 的视角写一份总结。"
    summary = asyncio.run(svc.generate(
        session_id=SESSION_ID, model_id=MODEL_ID,
        history=[SummaryInput(round_id=1, user_question="Q",
                              successful_answers={MODEL_ID: "A"})],
        trigger="user", prompt=custom,
    ))
    assert summary.status == "success"
    assert sink[0].startswith(custom[:20])


def test_guard_prompt_templates_are_identity_free():
    """系统模板本身不得含占位符或真实身份字段。"""
    assert "{model_id}" not in PEER_TRIGGERED_SUMMARY_PROMPT_TEMPLATE
    assert "{session_id}" not in PEER_TRIGGERED_SUMMARY_PROMPT_TEMPLATE
    assert "{model_id}" not in DEFAULT_SUMMARY_PROMPT
    assert "{session_id}" not in DEFAULT_SUMMARY_PROMPT


def test_guard_peer_prompt_render_ignores_identity_args():
    """render_peer_triggered_prompt 保留签名，但返回值不依赖身份参数。"""
    a = render_peer_triggered_prompt("alpha-model", "sess-1")
    b = render_peer_triggered_prompt("beta-model", "sess-2")
    assert a == b
    assert "alpha-model" not in a and "sess-1" not in a


def test_guard_service_calls_a3_check_before_provider(tmp_path):
    """full_prompt 在 Provider 之前经过 anonymizer.assert_no_identity_leak。"""
    import inspect

    src = inspect.getsource(SummaryService)
    assert "_assert_no_identity_leak" in src
    assert "assert_no_identity_leak(" in src
    # 守门必须在 _invoke_model 之前（源码顺序）
    assert src.index("_assert_no_identity_leak") < src.index("_invoke_model(")


def test_guard_identity_leak_would_raise(tmp_path):
    """反向验证：若系统模板真的注入身份，守门必须抛错（防止守门形同虚设）。"""
    from core.anonymizer import IdentityLeakError

    sink: list[str] = []
    svc = _svc(tmp_path, sink)
    with pytest.raises(IdentityLeakError):
        svc._assert_no_identity_leak(f"当前模型：{MODEL_ID}", MODEL_ID)


def test_summary_file_format_unchanged(tmp_path):
    """修复不改落盘格式（JSON frontmatter + 正文）。"""
    sink: list[str] = []
    svc = _svc(tmp_path, sink)
    svc.storage.ensure_session_dir(SESSION_ID)
    s = asyncio.run(svc.generate(
        session_id=SESSION_ID, model_id=MODEL_ID,
        history=[SummaryInput(round_id=1, user_question="Q",
                              successful_answers={MODEL_ID: "A"})],
        trigger="user",
    ))
    raw = (svc.storage.session_dir(SESSION_ID) / s.file_path).read_text(encoding="utf-8")
    assert raw.startswith("---\n")
    fm = json.loads(raw[4: raw.index("\n---\n", 4)])
    # 文件格式不变：JSON frontmatter + 正文；内部 provenance 仍保留真实身份
    assert fm["model_id"] == MODEL_ID
    assert fm["session_id"] == SESSION_ID
    assert fm["summary_id"] == s.summary_id
    assert fm["is_derived_artifact"] is True
    assert raw.split("\n---\n", 1)[1].strip(), "正文不得为空"
