"""Phase 4.6 / M1：Project Memory 测试（phase4_memory.md §1.13 / §49–§53 / §62 / §67）。

M1 只证明两件事：

  1. **容器与数据模型成立**
     - MemoryEntry / MemoryCandidate 的 kind + source_semantics 合法域
     - projects/<project_id>/memory/ 目录 + memory_NNN.md + memory_index.jsonl
     - 只读列表 / 详情 API
     - project_memory 追加日志

  2. **没有任何自动写入路径（反向证明，核心验收）**
     - Round / Summary / Compaction / Peer Review / Pin / MCP Evidence
       六条真实链路跑完后，projects/ 下**不产生任何文件**
     - SummaryService 不会把 Project Memory 当作 Conversation Summary
     - ProjectMemoryService 本身不提供 write / create / delete / update 方法

M1 明确不覆盖（后续阶段）：Candidate 生成（M2）、检索（M3）、删除/修改（M4）。
"""

from __future__ import annotations

import asyncio
import inspect
import json
from pathlib import Path

import pytest

from core.project_memory import (
    DEFAULT_PROJECT_ID,
    MEMORY_KINDS,
    MEMORY_DIRNAME,
    PROJECTS_DIRNAME,
    PROJECT_MEMORY_ARTIFACT_KIND,
    SOURCE_SEMANTICS,
    MemoryCandidate,
    MemoryEntry,
    ProjectMemoryService,
)
from core.storage import Storage, atomic_write_text


# ── helpers ─────────────────────────────────────────────────────────────


def _svc(tmp_path) -> tuple[ProjectMemoryService, Storage]:
    # 真实布局：sessions/ 与 projects/ 同为数据根的子目录
    # （Storage.root = sessions 根，projects_root = root.parent / "projects"）
    storage = Storage(tmp_path / "sessions")
    return ProjectMemoryService(storage), storage


def _write_memory(svc: ProjectMemoryService, project_id: str, idx: int, **kw) -> MemoryEntry:
    """模拟（未来 M2 的）落盘：M1 无写入 API，测试直接写文件以验证读取侧。"""
    mid = f"memory_{idx:03d}"
    entry = MemoryEntry(
        memory_id=mid,
        project_id=project_id,
        kind=kw.get("kind", "decision"),
        source_semantics=kw.get("source_semantics", "decision"),
        content=kw.get("content", "最终选择方案 Y。"),
        source_refs=kw.get("source_refs", ["session:s1/round:003"]),
    )
    p = svc.memory_path(project_id, mid)
    p.parent.mkdir(parents=True, exist_ok=True)
    atomic_write_text(p, entry.to_markdown())
    # 索引行（§62）
    with svc.index_path(project_id).open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(entry.to_dict(), ensure_ascii=False) + "\n")
    return entry


def _snapshot_projects(tmp_path: Path) -> set[str]:
    """projects/ 下所有文件的相对路径集合（目录不存在 → 空集）。"""
    root = tmp_path / PROJECTS_DIRNAME
    if not root.exists():
        return set()
    return {str(p.relative_to(root)) for p in root.rglob("*") if p.is_file()}


# ── 1. 数据模型 ─────────────────────────────────────────────────────────


def test_memory_kinds_match_spec_50():
    """§50：六种信息类型。"""
    assert MEMORY_KINDS == (
        "fact",
        "decision",
        "experiment",
        "rejected",
        "open_question",
        "evidence_reference",
    )


def test_source_semantics_match_spec_51():
    """§51：来源语义必须保留（Opinion / Decision / Evidence / Result）。"""
    assert SOURCE_SEMANTICS == ("opinion", "decision", "evidence", "result")


def test_entry_rejects_unknown_kind_or_semantics():
    with pytest.raises(ValueError, match="未知 Memory kind"):
        MemoryEntry("memory_001", "p", "vibe", "decision", "x")
    with pytest.raises(ValueError, match="未知 source_semantics"):
        MemoryEntry("memory_001", "p", "fact", "gut_feeling", "x")


def test_entry_artifact_kind_is_project_memory():
    """§52：artifact_kind 三值互斥。"""
    e = MemoryEntry("memory_001", "p", "fact", "result", "Y 提升 18%")
    assert e.artifact_kind == PROJECT_MEMORY_ARTIFACT_KIND
    assert e.to_dict()["artifact_kind"] == PROJECT_MEMORY_ARTIFACT_KIND


def test_entry_roundtrip_preserves_source_refs():
    e = MemoryEntry(
        "memory_002",
        "p",
        "evidence_reference",
        "evidence",
        "GitHub Issue #456 描述 P99 问题",
        source_refs=["session:s1/round:001", "evidence:call_A"],
    )
    d = e.to_dict()
    assert MemoryEntry.from_dict(d).source_refs == [
        "session:s1/round:001",
        "evidence:call_A",
    ]


def test_candidate_is_not_confirmed_and_can_be_confirmed():
    """§49：Candidate →（用户确认，M2）→ Memory。M1 只定义模型。"""
    c = MemoryCandidate("p", "decision", "decision", "选 Y")
    assert c.status == "candidate"
    e = c.confirm("memory_003")
    assert e.status == "confirmed"
    assert e.content == "选 Y"


def test_opinion_is_not_silently_promoted_to_fact():
    """§51：Opinion 与 Fact 不得自动混同（数据模型层即强制区分）。"""
    opinion = MemoryEntry("memory_004", "p", "open_question", "opinion", "X 可能更好")
    fact = MemoryEntry("memory_005", "p", "fact", "result", "Y 提升 18%")
    assert opinion.kind != fact.kind
    assert opinion.source_semantics != fact.source_semantics


# ── 2. 目录与只读访问 ───────────────────────────────────────────────────


def test_memory_dir_layout_follows_spec_62(tmp_path):
    svc, storage = _svc(tmp_path)
    d = svc.memory_dir("proj-a")
    assert d == storage.projects_root() / "proj-a" / MEMORY_DIRNAME
    assert d.name == MEMORY_DIRNAME


def test_list_and_get_read_existing_files(tmp_path):
    svc, _ = _svc(tmp_path)
    _write_memory(svc, "proj-a", 1, content="第一条")
    _write_memory(svc, "proj-a", 2, kind="fact", source_semantics="result", content="第二条")

    entries = svc.list_memories("proj-a")
    assert [e.memory_id for e in entries] == ["memory_001", "memory_002"]
    assert entries[0].content == "第一条"

    one = svc.get_memory("proj-a", "memory_002")
    assert one is not None and one.kind == "fact"
    assert svc.get_memory("proj-a", "memory_999") is None


def test_list_empty_when_project_absent(tmp_path):
    svc, _ = _svc(tmp_path)
    assert svc.list_memories("nope") == []
    assert svc.read_index("nope") == []


def test_index_file_is_jsonl(tmp_path):
    svc, _ = _svc(tmp_path)
    _write_memory(svc, "proj-a", 1)
    rows = svc.read_index("proj-a")
    assert len(rows) == 1
    assert rows[0]["memory_id"] == "memory_001"


def test_default_project_id_is_fixed_entry():
    """M1 裁决：project_id 真实存在于数据模型，M1 只有 default 一个入口。"""
    assert DEFAULT_PROJECT_ID == "default"
    e = MemoryEntry("memory_001", DEFAULT_PROJECT_ID, "fact", "result", "x")
    assert e.project_id == "default"


# ── 3. 日志 ─────────────────────────────────────────────────────────────


def test_append_event_writes_project_memory_jsonl(tmp_path):
    svc, _ = _svc(tmp_path)
    svc.append_event("proj-a", {"action": "listed", "count": 0})
    svc.append_event("proj-a", {"action": "read", "memory_id": "memory_001"})
    events = svc.read_events("proj-a")
    assert [e["action"] for e in events] == ["listed", "read"]
    assert all(e["event"] == "project_memory" for e in events)
    assert all(e["project_id"] == "proj-a" for e in events)


# ── 4. 服务不提供写入能力（结构性保证）────────────────────────────────


def test_service_has_no_delete_or_update_methods():
    """M2 起 create_candidate / confirm_candidate 是**唯一**两个显式写入口；
    仍然禁止 delete / update / 覆盖类方法（M4 才讨论修订/删除）。"""
    for name in ("write", "save", "delete", "update", "upsert", "add",
                 "remove", "modify", "replace"):
        assert not hasattr(ProjectMemoryService, name), (
            f"M1/M2 不应暴露方法 {name}()"
        )
    # 唯一的显式写入口（用户主动调用；自动触发由其它反向测试排除）
    assert hasattr(ProjectMemoryService, "create_candidate")
    assert hasattr(ProjectMemoryService, "confirm_candidate")


# ── 5. 只读 API ─────────────────────────────────────────────────────────


def test_api_list_and_get(tmp_path, monkeypatch):
    from fastapi.testclient import TestClient

    import app as app_module

    svc, _ = _svc(tmp_path)
    _write_memory(svc, "proj-a", 1, content="决策：选 Y")

    monkeypatch.setattr(app_module, "project_memory", svc, raising=False)
    with TestClient(app_module.app) as c:
        r = c.get("/api/project/proj-a/memory")
        assert r.status_code == 200
        body = r.json()
        assert body["project_id"] == "proj-a"
        assert body["total"] == 1
        assert body["entries"][0]["content"] == "决策：选 Y"
        # §51：不提供 is_fact / confidence
        assert "is_fact" not in body["entries"][0]
        assert "confidence" not in body["entries"][0]

        r2 = c.get("/api/project/proj-a/memory/memory_001")
        assert r2.status_code == 200
        assert r2.json()["memory_id"] == "memory_001"

        assert c.get("/api/project/proj-a/memory/memory_404").status_code == 404
        assert c.get("/api/project/empty-proj/memory").json()["total"] == 0


# ── 6. 反向证明：六条链路都不自动写 Memory ─────────────────────────────


def test_round_does_not_write_memory(tmp_path):
    """Round 执行 → projects/ 不产生任何文件。"""
    from core.model_manager import ProviderConfig
    from core.round_manager import RoundManager
    from core.state import ModelParticipant, RoundState, SessionState

    storage = Storage(tmp_path / "sessions")
    sid = "s-round"
    storage.ensure_session_dir(sid)
    storage.save_question(sid, "原始问题")
    sess = SessionState(
        session_id=sid,
        original_question="原始问题",
        current_round=0,
        active_models=["alpha"],
        closed_models=[],
        rounds=[],
    )
    storage.save_state(sess)

    class _Registry:
        def __init__(self):
            self._cfg = ProviderConfig(
                model_id="alpha", display_name="Alpha", model="alpha-v1",
                context_limit=8000
            )

        def has(self, model_id: str) -> bool:
            return model_id == "alpha"

        @property
        def participants(self):
            return [
                ModelParticipant(
                    id="alpha",
                    display_name="Alpha",
                    provider="openai_compatible",
                    model="alpha-v1",
                    enabled=True,
                )
            ]

        def config_for(self, model_id):
            return self._cfg

    class _P:
        async def generate(self, round_id, prompt, tools=None, max_output_tokens=None):
            yield "回答正文"

    rm = RoundManager(storage, _Registry(), lambda mid: _P())
    rs, bus = rm.prepare_round(sess, "问题", selected_models=["alpha"])
    asyncio.run(rm.execute_round(sid, rs, bus))

    assert _snapshot_projects(tmp_path) == set(), "Round 不应写 Project Memory"


def test_summary_does_not_write_memory(tmp_path):
    """Summary 生成 → projects/ 不产生任何文件（Plan §19.3 同款断言）。"""
    from core.summary_service import SummaryInput, SummaryService

    storage = Storage(tmp_path)
    sid = "s-summary"
    storage.ensure_session_dir(sid)

    def _factory(mid):
        class _P:
            def generate(self, round_id=None, prompt="", tools=None):
                yield "[stub] 当前理解：观点 / 假设 / 争议均在。\n"

        return _P()

    svc = SummaryService(storage, provider_factory=_factory)
    history = [SummaryInput(round_id=1, user_question="Q?", successful_answers={"a": "A1"})]
    s = asyncio.run(
        svc.generate(session_id=sid, model_id="a", history=history, trigger="user")
    )
    assert s.status == "success"
    assert _snapshot_projects(tmp_path) == set(), "Summary 不应写 Project Memory"


def test_compaction_does_not_write_memory(tmp_path):
    """Compaction → projects/ 不产生任何文件（§52）。"""
    from core.compaction import CompactionService
    from core.valid_round import ValidRound

    storage = Storage(tmp_path / "sessions")
    sid = "s-compact"
    storage.ensure_session_dir(sid)

    def _factory(mid):
        class _P:
            def generate(self, round_id=None, prompt="", tools=None):
                yield "参与者 qwen 认为：方案 X 更稳妥。\n"

        return _P()

    svc = CompactionService(storage, provider_factory=_factory)
    rounds = [
        ValidRound(
            round_id=r,
            user_question=f"Q{r}",
            successful_answers={"a": f"A{r}"},
            failed_models=[],
            is_valid=True,
            token_estimate=None,
        )
        for r in (1, 2)
    ]
    blk = asyncio.run(
        svc.compact(session_id=sid, rounds=rounds, compaction_budget=1000)
    )
    assert blk.records
    assert _snapshot_projects(tmp_path) == set(), "Compaction 不应写 Project Memory"


def test_peer_review_does_not_write_memory(tmp_path):
    """Peer Context 准备 → projects/ 不产生任何文件（§16 / §67）。"""
    from core.peer_context import PeerContextPreparator

    storage = Storage(tmp_path / "sessions")
    sid = "s-peer"
    storage.ensure_session_dir(sid)

    class _VR:
        def __init__(self, rid, answers):
            self.round_id = rid
            self.successful_answers = answers
            self.user_question = f"Q{rid}"

    class _RS:
        def __init__(self):
            self.round_id = 2
            self.participants = ["a", "b"]
            self.candidate_ids = {"a": "Candidate-A", "b": "Candidate-B"}
            self.peer_review = True

    class _Sess:
        def __init__(self, sid):
            self.session_id = sid

    calls = []

    class _SummarySvc:
        def latest_for(self, session_id, model_id):
            return None

        async def generate(self, **kw):
            calls.append(kw["model_id"])

    prep = PeerContextPreparator(storage=storage, summary_service=_SummarySvc())
    pool = [_VR(1, {"a": "A1", "b": "B1"})]
    out = asyncio.run(
        prep.prepare(session=_Sess(sid), round_state=_RS(), valid_pool=pool)
    )
    assert out
    assert _snapshot_projects(tmp_path) == set(), "Peer Review 不应写 Project Memory"


def test_pin_does_not_write_memory(tmp_path):
    """Pin / Unpin → projects/ 不产生任何文件（§18.3：Pin ≠ Memory）。"""
    from core.pin_store import PinStore
    from core.state import SessionState

    storage = Storage(tmp_path / "sessions")
    sid = "s-pin"
    storage.ensure_session_dir(sid)
    storage.save_state(
        SessionState(
            session_id=sid,
            original_question="Q",
            current_round=1,
            active_models=["a"],
            closed_models=[],
            rounds=[],
        )
    )
    pins = PinStore(storage)
    pins.pin(sid, 1)
    pins.unpin(sid, 1)
    assert _snapshot_projects(tmp_path) == set(), "Pin 不应写 Project Memory"


def test_mcp_evidence_does_not_write_memory(tmp_path):
    """Evidence Index 构建 → projects/ 不产生任何文件（§47：Evidence ≠ Fact）。"""
    from core.evidence_index import EvidenceIndexService

    storage = Storage(tmp_path / "sessions")
    sid = "s-ev"
    storage.ensure_session_dir(sid)
    svc = EvidenceIndexService(storage)
    svc.build_index(
        session_id=sid, round_id=1, model_id="a", final_answer="引用了某些结果"
    )
    assert _snapshot_projects(tmp_path) == set(), "MCP Evidence 不应写 Project Memory"


def _code_identifiers(module) -> set[str]:
    """提取模块**代码**（不含 docstring / 注释）里出现的标识符。

    守门断言必须基于 AST —— 否则 docstring 里"不做 embedding"这类说明会
    把自己误判成违规。
    """
    import ast

    tree = ast.parse(inspect.getsource(module))
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Name):
            names.add(node.id)
        elif isinstance(node, ast.Attribute):
            names.add(node.attr)
        elif isinstance(node, (ast.Import, ast.ImportFrom)):
            for a in node.names:
                names.add(a.name)
        elif isinstance(node, ast.Constant) and isinstance(node.value, str):
            names.add(node.value)
    return names


# ── 7. 反向证明：Summary Service 不把 Memory 当 Summary ─────────────────


def test_summary_service_never_reads_project_memory(tmp_path):
    """§53：Summary 不能自动变成 Memory，反之亦然 —— 读取侧也不互通。

    即便 projects/default/memory/ 下存在 memory 文件：
      - SummaryService.latest_for / list_summaries_for_model 都看不到它
      - summary_service 代码中不存在 project_memory / memory_dir 等引用
    """
    from core import summary_service as ss_mod
    from core.summary_service import SummaryService

    storage = Storage(tmp_path / "sessions")
    sid = "s-iso"
    storage.ensure_session_dir(sid)
    mem_svc = ProjectMemoryService(storage)
    _write_memory(mem_svc, DEFAULT_PROJECT_ID, 1, content="长期决策：选 Y")

    svc = SummaryService(storage, provider_factory=lambda mid: None)
    assert svc.latest_for(sid, "a") is None
    assert svc.list_summaries_for_model(sid, "a") == []
    assert svc.latest_all_models(sid) == {"a": None}

    names = _code_identifiers(ss_mod)
    for banned in ("project_memory", "ProjectMemoryService", "projects_root", "memory_dir"):
        assert banned not in names, f"SummaryService 出现越界引用 {banned!r}"


def test_project_memory_module_has_no_automatic_triggers():
    """M1 模块不得引用任何业务触发源（否则就形成自动写入路径）。"""
    from core import project_memory as pm

    names = _code_identifiers(pm)
    for banned in (
        "SummaryService",
        "CompactionService",
        "PeerContextPreparator",
        "ContextSelector",
        "round_manager",
        "summary_service",
        "compaction",
        "peer_context",
    ):
        assert banned not in names, f"project_memory 不应引用 {banned!r}"


def test_no_ranking_or_vector_db_in_project_memory():
    """§67：第一版不做 embedding / vector DB / ranking / Memory Agent。"""
    from core import project_memory as pm

    names = {n.lower() for n in _code_identifiers(pm)}
    for banned in ("embedding", "vector", "faiss", "cosine", "rank", "score", "similarity"):
        assert banned not in names, f"project_memory 出现越界语义 {banned!r}"


# ── 8. 守门：S3.1~S3.11 核心模块未改动 ──────────────────────────────────


# =========================================================================
# M2：Candidate → 用户确认 → 正式 Memory（phase4_memory.md §49 / §51 / §67）
# =========================================================================

from core.project_memory import (  # noqa: E402
    CANDIDATE_SOURCES,
    ConfirmResult,
)


# ── M2.1 Candidate 创建（用户显式）──────────────────────────────────────


def test_candidate_sources_match_m2_spec():
    """M2 裁决：来源仅四种，全部需要用户显式指定。"""
    assert CANDIDATE_SOURCES == (
        "round",
        "conversation_summary",
        "mcp_evidence",
        "user_input",
    )


def test_create_candidate_persists_and_requires_explicit_source(tmp_path):
    svc, _ = _svc(tmp_path)
    c = svc.create_candidate(
        "proj-m2",
        kind="decision",
        source_semantics="decision",
        content="最终选择方案 Y。",
        source="round",
        source_refs=["session:s1/round:003"],
    )
    assert c.candidate_id == "candidate_001"
    assert c.source == "round"
    # candidate 落盘在 candidates/ 子目录，**不产生** memory_NNN.md
    assert svc.candidate_path("proj-m2", "candidate_001").exists()
    assert svc.list_memories("proj-m2") == []
    # 事件日志
    events = svc.read_events("proj-m2")
    assert events[-1]["action"] == "candidate_created"


def test_create_candidate_rejects_invalid_source(tmp_path):
    svc, _ = _svc(tmp_path)
    with pytest.raises(ValueError, match="未知 Candidate 来源"):
        svc.create_candidate(
            "p", kind="fact", source_semantics="result",
            content="x", source="auto",   # 伪造的自动来源必须被拒绝
        )


def test_candidates_increment_ids(tmp_path):
    svc, _ = _svc(tmp_path)
    for i in range(1, 4):
        c = svc.create_candidate(
            "p", kind="fact", source_semantics="result",
            content=f"实验结果 {i}", source="user_input",
        )
        assert c.candidate_id == f"candidate_{i:03d}"
    assert len(svc.list_candidates("p")) == 3


def test_all_four_candidate_sources_accepted(tmp_path):
    svc, _ = _svc(tmp_path)
    for i, src in enumerate(CANDIDATE_SOURCES, start=1):
        c = svc.create_candidate(
            "p", kind="open_question", source_semantics="opinion",
            content=f"来自 {src} 的候选", source=src,
        )
        assert c.source == src
        assert c.candidate_id == f"candidate_{i:03d}"


# ── M2.2 确认 → 正式 Memory（用户显式）────────────────────────────────


def test_confirm_creates_memory_file_index_and_event(tmp_path):
    svc, _ = _svc(tmp_path)
    c = svc.create_candidate(
        "proj-m2", kind="decision", source_semantics="decision",
        content="最终选择方案 Y。", source="round",
        source_refs=["session:s1/round:003"],
    )
    result = svc.confirm_candidate("proj-m2", c.candidate_id)

    assert result.status == "created"
    assert result.entry is not None
    assert result.entry.memory_id == "memory_001"
    assert result.entry.status == "confirmed"

    # 文件 + 索引 + 事件
    assert svc.memory_path("proj-m2", "memory_001").exists()
    rows = svc.read_index("proj-m2")
    assert rows and rows[0]["memory_id"] == "memory_001"
    actions = [e["action"] for e in svc.read_events("proj-m2")]
    assert actions == ["candidate_created", "memory_confirmed"]

    # 读取侧可见
    assert svc.get_memory("proj-m2", "memory_001").content == "最终选择方案 Y。"


def test_confirm_requires_existing_candidate(tmp_path):
    svc, _ = _svc(tmp_path)
    with pytest.raises(FileNotFoundError):
        svc.confirm_candidate("proj-m2", "candidate_999")


def test_confirm_twice_with_same_content_gives_duplicate_not_new_entry(tmp_path):
    """同内容重复 → 提示去重，不新建重复条目；已有条目不受影响。"""
    svc, _ = _svc(tmp_path)
    c1 = svc.create_candidate(
        "p", kind="fact", source_semantics="result",
        content="Y 提升 18%。", source="user_input",
    )
    r1 = svc.confirm_candidate("p", c1.candidate_id)
    assert r1.status == "created"

    c2 = svc.create_candidate(
        "p", kind="fact", source_semantics="result",
        content="  y  提升 18%。  ", source="conversation_summary",  # 仅空白/大小写差异
    )
    r2 = svc.confirm_candidate("p", c2.candidate_id)
    assert r2.status == "duplicate"
    assert r2.entry is None
    assert r2.duplicate_of == "memory_001"
    assert any("重复" in w for w in r2.warnings)
    # 全目录仍只有一条正式 Memory
    assert [e.memory_id for e in svc.list_memories("p")] == ["memory_001"]
    actions = [e["action"] for e in svc.read_events("p")]
    assert actions[-1] == "duplicate_detected"


def test_conflict_reported_but_never_auto_resolved(tmp_path):
    """语义冲突 → 提示冲突并创建新条目；禁止自动裁决/覆盖/删除。"""
    svc, _ = _svc(tmp_path)
    # 第一条：来源 s1/round:003 的 decision
    c1 = svc.create_candidate(
        "p", kind="decision", source_semantics="decision",
        content="方案 X 采用分层缓存。", source="round",
        source_refs=["session:s1/round:003"],
    )
    r1 = svc.confirm_candidate("p", c1.candidate_id)
    assert r1.status == "created"
    first_bytes = svc.memory_path("p", "memory_001").read_bytes()

    # 第二条：同一来源、同 kind、不同结论 → 冲突
    c2 = svc.create_candidate(
        "p", kind="decision", source_semantics="decision",
        content="方案 X 改用全量重算。", source="round",
        source_refs=["session:s1/round:003"],
    )
    r2 = svc.confirm_candidate("p", c2.candidate_id)

    # 仍然创建（不静默丢弃），并给出冲突提示
    assert r2.status == "created"
    assert r2.entry.memory_id == "memory_002"
    assert "memory_001" in r2.conflicts
    assert any("人工核对" in w for w in r2.warnings)
    # 旧条目**字节级不变**（不覆盖、不修改、不删除）
    assert svc.memory_path("p", "memory_001").read_bytes() == first_bytes
    assert len(svc.list_memories("p")) == 2
    # 冲突事件落日志
    actions = [e["action"] for e in svc.read_events("p")]
    assert "conflict_detected" in actions


def test_different_kind_same_refs_is_not_conflict(tmp_path):
    """冲突只对同 kind 判定：fact 与 decision 各自独立，不互相干扰。"""
    svc, _ = _svc(tmp_path)
    c1 = svc.create_candidate(
        "p", kind="decision", source_semantics="decision",
        content="采用方案 X。", source="round",
        source_refs=["session:s1/round:001"],
    )
    svc.confirm_candidate("p", c1.candidate_id)
    c2 = svc.create_candidate(
        "p", kind="fact", source_semantics="evidence",
        content="GitHub Issue #456 存在。", source="mcp_evidence",
        source_refs=["session:s1/round:001"],
    )
    r2 = svc.confirm_candidate("p", c2.candidate_id)
    assert r2.status == "created"
    assert r2.conflicts == []


# ── M2.3 API：显式 POST 触发（无任何自动路径）──────────────────────────


def test_api_candidate_create_and_confirm(tmp_path, monkeypatch):
    from fastapi.testclient import TestClient

    import app as app_module

    svc, _ = _svc(tmp_path)
    monkeypatch.setattr(app_module, "project_memory", svc, raising=False)
    with TestClient(app_module.app) as c:
        # 创建（显式 POST）
        r = c.post(
            "/api/project/proj-m2/memory/candidate",
            json={
                "kind": "decision",
                "source_semantics": "decision",
                "content": "API 决策：选 Y。",
                "source": "user_input",
                "source_refs": [],
            },
        )
        assert r.status_code == 201
        cid = r.json()["candidate_id"]

        # 列表可见，正式 Memory 仍为空
        assert c.get("/api/project/proj-m2/memory/candidates").json()["total"] == 1
        assert c.get("/api/project/proj-m2/memory").json()["total"] == 0

        # 确认（显式 POST）
        r2 = c.post(f"/api/project/proj-m2/memory/candidate/{cid}/confirm")
        assert r2.status_code == 200
        body = r2.json()
        assert body["status"] == "created"
        assert body["entry"]["memory_id"] == "memory_001"
        assert c.get("/api/project/proj-m2/memory").json()["total"] == 1

        # 不存在的 candidate → 404
        assert c.post(
            "/api/project/proj-m2/memory/candidate/candidate_404/confirm"
        ).status_code == 404

        # 非法 kind / source → 400
        assert c.post(
            "/api/project/proj-m2/memory/candidate",
            json={"kind": "vibe", "source_semantics": "decision",
                  "content": "x", "source": "user_input"},
        ).status_code == 400
        assert c.post(
            "/api/project/proj-m2/memory/candidate",
            json={"kind": "fact", "source_semantics": "decision",
                  "content": "x", "source": "auto"},
        ).status_code == 400


def test_m2_module_still_has_no_business_triggers():
    """M2 后模块仍不得引用 Round/Summary/Compaction/Peer/Pin/Evidence。"""
    from core import project_memory as pm

    names = _code_identifiers(pm)
    for banned in (
        "SummaryService", "CompactionService", "PeerContextPreparator",
        "ContextSelector", "PinStore", "EvidenceIndexService",
        "round_manager", "summary_service", "compaction", "peer_context",
        "pin_store", "evidence_index",
    ):
        assert banned not in names, f"project_memory 不应引用 {banned!r}"
