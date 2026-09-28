"""Phase 4.6 / M1 + M2：Project Memory（phase4_memory.md §0 / §49–§53 / §62 / §67）。

M1 范围（已完成）：
    ✅ 数据模型：MemoryEntry / MemoryCandidate
    ✅ 目录：projects/<project_id>/memory/memory_NNN.md + memory_index.jsonl
    ✅ 只读访问：list / get
    ✅ project_memory 事件日志（追加写）
    ✅ 反向保证：本模块**不提供任何自动写入路径**

M2 范围（本次）：
    ✅ 用户**显式**创建 Candidate（来源：round / conversation_summary /
       mcp_evidence / user_input）
    ✅ 用户**显式**确认 → 正式 MemoryEntry 落盘 + 索引 + 事件日志
    ✅ 同内容重复 → 提示去重（不新建重复条目）
    ✅ 语义冲突 → 提示冲突（**禁止**自动裁决 / 覆盖 / 删除）

M2 仍然禁止（后续阶段或永久禁止）：
    ❌ 自动 Candidate（无 Round / Summary / Compaction / Peer / Pin / MCP 触发器）
    ❌ 自动 Confirm
    ❌ 自动重要性评分（§67）
    ❌ 检索算法：embedding / vector DB / ranking（M3）
    ❌ Memory 注入 Context（M3；§63 最终 Context 结构未包含 Memory）
    ❌ 删除 / 修改（M4）

三条 Memory 的硬边界（Plan §20.3）：
    Conversation Summary  →  summaries/<model>/summary_NNN.md   （模型工作状态）
    Compaction            →  compactions/compact_NNN.md         （Context 压缩）
    Project Memory        →  projects/<pid>/memory/memory_NNN.md（跨 Session 长期知识）

    ⛔ 三者之间无任何自动写入路径。

project_id（M1 裁决）：
    数据模型保留真实 project_id；当前使用固定启动项目 ``default``。
    不新增 Project 实体，不做 Session → Project 映射。
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .storage import atomic_write_text

# ── 常量（§50 / §51 / §52）───────────────────────────────────────────────

#: §52：artifact_kind 三值互斥，Project Memory 既不是 Summary 也不是 Compaction
PROJECT_MEMORY_ARTIFACT_KIND = "project_memory"

#: §50：Project Memory 的信息类型（结构化字段只在这里出现，C11）
MEMORY_KINDS: tuple[str, ...] = (
    "fact",
    "decision",
    "experiment",
    "rejected",
    "open_question",
    "evidence_reference",
)

#: §51：可靠性 —— 必须保留来源语义，禁止把 Opinion 收敛成 Fact
SOURCE_SEMANTICS: tuple[str, ...] = (
    "opinion",
    "decision",
    "evidence",
    "result",
)

STATUS_CONFIRMED = "confirmed"
STATUS_CANDIDATE = "candidate"

#: M2：Candidate 的来源类型 —— 只能由用户显式指定，不存在自动推断
CANDIDATE_SOURCES: tuple[str, ...] = (
    "round",                  # 某个 Round 的 Q&A
    "conversation_summary",   # 某模型的最新 Conversation Summary
    "mcp_evidence",           # 某条 MCP Evidence（保留 tool_call_id 溯源）
    "user_input",             # 用户手输
)

#: M1 固定启动项目（project_id 真实存在于数据模型中，M1 只有这一个入口）
DEFAULT_PROJECT_ID = "default"

PROJECTS_DIRNAME = "projects"
MEMORY_DIRNAME = "memory"
MEMORY_INDEX_FILENAME = "memory_index.jsonl"
MEMORY_EVENTS_FILENAME = "memory_events.jsonl"
# M2：Candidate 单独子目录（规范 §62 未细化 candidate 存储，此处为 M2 必要约定）
CANDIDATES_DIRNAME = "candidates"
CANDIDATES_INDEX_FILENAME = "candidates_index.jsonl"


def _iso_now() -> str:
    from datetime import datetime, timezone

    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


# ── 数据模型 ─────────────────────────────────────────────────────────────


@dataclass
class MemoryEntry:
    """已确认的 Project Memory（§50/§51）。

    ``kind`` 回答"这是哪类知识"，``source_semantics`` 回答"它凭什么成立" ——
    两者都必须保留：§51 明确禁止把 Model Opinion / External Evidence 自动写成
    Fact，也禁止把"X 好 / X 不好 / 最终选 Y / Y 提升 18%"收敛成 "X = failed"。
    """

    memory_id: str
    project_id: str
    kind: str
    source_semantics: str
    content: str
    #: 溯源指针（如 "session:sid/round:003"、"evidence:call_A"），只存引用不存副本
    source_refs: list[str] = field(default_factory=list)
    created_at: str = ""
    status: str = STATUS_CONFIRMED

    def __post_init__(self) -> None:
        if self.kind not in MEMORY_KINDS:
            raise ValueError(f"未知 Memory kind: {self.kind!r}（应为 {MEMORY_KINDS}）")
        if self.source_semantics not in SOURCE_SEMANTICS:
            raise ValueError(
                f"未知 source_semantics: {self.source_semantics!r}"
                f"（应为 {SOURCE_SEMANTICS}）"
            )
        if not self.created_at:
            self.created_at = _iso_now()

    @property
    def artifact_kind(self) -> str:
        return PROJECT_MEMORY_ARTIFACT_KIND

    def to_dict(self) -> dict[str, Any]:
        return {
            "memory_id": self.memory_id,
            "project_id": self.project_id,
            "kind": self.kind,
            "source_semantics": self.source_semantics,
            "content": self.content,
            "source_refs": list(self.source_refs),
            "created_at": self.created_at,
            "status": self.status,
            "artifact_kind": self.artifact_kind,
        }

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "MemoryEntry":
        return cls(
            memory_id=d["memory_id"],
            project_id=d["project_id"],
            kind=d["kind"],
            source_semantics=d["source_semantics"],
            content=d.get("content", ""),
            source_refs=list(d.get("source_refs") or []),
            created_at=d.get("created_at", ""),
            status=d.get("status", STATUS_CONFIRMED),
        )

    def to_markdown(self) -> str:
        """文件内容 = JSON frontmatter + 正文（与 Summary / Compaction 同风格）。"""
        return (
            "---\n"
            + json.dumps(self.to_dict(), ensure_ascii=False, indent=2)
            + "\n---\n\n"
            + self.content
            + "\n"
        )

    @classmethod
    def parse_file(cls, path: Path) -> "MemoryEntry | None":
        try:
            raw = path.read_text(encoding="utf-8")
        except (FileNotFoundError, OSError):
            return None
        if not raw.startswith("---\n"):
            return None
        end = raw.find("\n---\n", 4)
        if end < 0:
            return None
        try:
            meta = json.loads(raw[4:end])
        except json.JSONDecodeError:
            return None
        body = raw[end + 5 :].lstrip("\n").rstrip("\n")
        entry = cls.from_dict(meta)
        entry.content = body
        return entry


@dataclass
class MemoryCandidate:
    """Candidate Memory（§49：历史 → Candidate → 用户确认 → Memory）。

    M1 **只定义数据模型**，不提供任何生成入口 —— §1.13 / §67 禁止自动写入，
    Candidate 的生成与确认属于 M2。
    """

    project_id: str
    kind: str
    source_semantics: str
    content: str
    source_refs: list[str] = field(default_factory=list)
    created_at: str = ""
    #: M2：来源类型（round / conversation_summary / mcp_evidence / user_input）
    source: str = "user_input"
    candidate_id: str = ""

    def __post_init__(self) -> None:
        if self.kind not in MEMORY_KINDS:
            raise ValueError(f"未知 Memory kind: {self.kind!r}")
        if self.source_semantics not in SOURCE_SEMANTICS:
            raise ValueError(f"未知 source_semantics: {self.source_semantics!r}")
        if self.source not in CANDIDATE_SOURCES:
            raise ValueError(
                f"未知 Candidate 来源: {self.source!r}（应为 {CANDIDATE_SOURCES}）"
            )
        if not self.created_at:
            self.created_at = _iso_now()

    @property
    def status(self) -> str:
        return STATUS_CANDIDATE

    def to_dict(self) -> dict[str, Any]:
        return {
            "candidate_id": self.candidate_id,
            "project_id": self.project_id,
            "kind": self.kind,
            "source_semantics": self.source_semantics,
            "content": self.content,
            "source_refs": list(self.source_refs),
            "created_at": self.created_at,
            "status": self.status,
            "source": self.source,
        }

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "MemoryCandidate":
        return cls(
            candidate_id=d.get("candidate_id", ""),
            project_id=d["project_id"],
            kind=d["kind"],
            source_semantics=d["source_semantics"],
            content=d.get("content", ""),
            source_refs=list(d.get("source_refs") or []),
            created_at=d.get("created_at", ""),
            source=d.get("source", "user_input"),
        )

    def to_markdown(self) -> str:
        return (
            "---\n"
            + json.dumps(self.to_dict(), ensure_ascii=False, indent=2)
            + "\n---\n\n"
            + self.content
            + "\n"
        )

    confirm_docstring = "用户确认 → MemoryEntry（M2：只由 confirm_candidate() 调用）。"

    def confirm(self, memory_id: str) -> MemoryEntry:
        return MemoryEntry(
            memory_id=memory_id,
            project_id=self.project_id,
            kind=self.kind,
            source_semantics=self.source_semantics,
            content=self.content,
            source_refs=list(self.source_refs),
            created_at=self.created_at,
            status=STATUS_CONFIRMED,
        )


@dataclass
class ConfirmResult:
    """确认结果（M2）。

    ``warnings`` 只做**提示**：重复与冲突都由用户判断 —— 系统绝不自动
    裁决、覆盖或删除任何已有 Memory（§51 / §67）。
    """

    entry: MemoryEntry | None
    status: str          # created / duplicate
    warnings: list[str] = field(default_factory=list)
    duplicate_of: str | None = None
    conflicts: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "entry": self.entry.to_dict() if self.entry else None,
            "status": self.status,
            "warnings": list(self.warnings),
            "duplicate_of": self.duplicate_of,
            "conflicts": list(self.conflicts),
        }


# ── 服务（M1 只读 + M2 显式 Candidate/确认）───────────────────────────


class ProjectMemoryService:
    """Project Memory 的目录 / 索引 / 访问。

    M2 写入边界：
      - ``create_candidate``：**用户显式调用**（HTTP POST），来源必须指明
      - ``confirm_candidate``：**用户显式调用**（HTTP POST），把候选转正
      - 仍然**没有** ``update`` / ``delete`` / ``upsert``（M4 才讨论）
      - 任何业务模块（Round/Summary/Compaction/Peer/Pin/Evidence）都拿不到
        本服务实例，也不存在对它们的 import —— 自动写入无从发生
    """

    def __init__(self, storage: Any) -> None:
        self.storage = storage

    # ── 路径（§62：projects/<project_id>/memory/）───────────────────────

    def projects_root(self) -> Path:
        return self.storage.projects_root()

    def memory_dir(self, project_id: str) -> Path:
        return self.storage.memory_dir(project_id)

    def memory_path(self, project_id: str, memory_id: str) -> Path:
        return self.memory_dir(project_id) / f"{memory_id}.md"

    def index_path(self, project_id: str) -> Path:
        return self.memory_dir(project_id) / MEMORY_INDEX_FILENAME

    def events_path(self, project_id: str) -> Path:
        return self.memory_dir(project_id) / MEMORY_EVENTS_FILENAME

    def candidates_dir(self, project_id: str) -> Path:
        return self.memory_dir(project_id) / CANDIDATES_DIRNAME

    def candidate_path(self, project_id: str, candidate_id: str) -> Path:
        return self.candidates_dir(project_id) / f"{candidate_id}.md"

    def candidates_index_path(self, project_id: str) -> Path:
        return self.candidates_dir(project_id) / CANDIDATES_INDEX_FILENAME

    # ── 只读访问 ────────────────────────────────────────────────────────

    def list_memories(self, project_id: str) -> list[MemoryEntry]:
        """按 memory_id 升序列出该 project 的全部 Memory；目录不存在返回 []。"""
        d = self.memory_dir(project_id)
        if not d.is_dir():
            return []
        out: list[MemoryEntry] = []
        for p in sorted(d.glob("memory_*.md")):
            entry = MemoryEntry.parse_file(p)
            if entry is not None:
                out.append(entry)
        return out

    def get_memory(self, project_id: str, memory_id: str) -> MemoryEntry | None:
        p = self.memory_path(project_id, memory_id)
        if not p.exists():
            return None
        return MemoryEntry.parse_file(p)

    def read_index(self, project_id: str) -> list[dict[str, Any]]:
        p = self.index_path(project_id)
        if not p.exists():
            return []
        rows: list[dict[str, Any]] = []
        for line in p.read_text(encoding="utf-8").splitlines():
            if line.strip():
                rows.append(json.loads(line))
        return rows

    def read_events(self, project_id: str) -> list[dict[str, Any]]:
        p = self.events_path(project_id)
        if not p.exists():
            return []
        rows: list[dict[str, Any]] = []
        for line in p.read_text(encoding="utf-8").splitlines():
            if line.strip():
                rows.append(json.loads(line))
        return rows

    # ── 日志（§61：关键行为必须机器可读）───────────────────────────────

    def append_event(self, project_id: str, event: dict[str, Any]) -> Path:
        """追加一条 project_memory 事件（§61：关键行为机器可读）。

        append-only JSONL，一行一事件。M2 起由 candidate 创建 / 确认触发。
        """
        d = self.memory_dir(project_id)
        d.mkdir(parents=True, exist_ok=True)
        p = self.events_path(project_id)
        payload = {"event": "project_memory", "project_id": project_id, **event}
        if not p.exists():
            atomic_write_text(p, json.dumps(payload, ensure_ascii=False) + "\n")
            return p
        with p.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(payload, ensure_ascii=False) + "\n")
        return p

    # ── M2：Candidate（用户显式触发）──────────────────────────────────

    def _next_candidate_index(self, project_id: str) -> int:
        d = self.candidates_dir(project_id)
        if not d.is_dir():
            return 1
        n = 0
        for p in d.glob("candidate_*.md"):
            try:
                n = max(n, int(p.stem.rsplit("_", 1)[1]))
            except (IndexError, ValueError):
                continue
        return n + 1

    def create_candidate(
        self,
        project_id: str,
        *,
        kind: str,
        source_semantics: str,
        content: str,
        source: str,
        source_refs: list[str] | None = None,
    ) -> MemoryCandidate:
        """用户显式创建 Candidate Memory（§49：历史 → Candidate → 确认 → Memory）。

        本方法**只能**由用户主动的 HTTP POST 触达；Round / Summary /
        Compaction / Peer / Pin / Evidence 均不持有本服务实例，因此不存在
        自动 Candidate。
        """
        candidate = MemoryCandidate(
            project_id=project_id,
            kind=kind,
            source_semantics=source_semantics,
            content=content,
            source_refs=list(source_refs or []),
            source=source,
        )
        idx = self._next_candidate_index(project_id)
        candidate.candidate_id = f"candidate_{idx:03d}"
        d = self.candidates_dir(project_id)
        d.mkdir(parents=True, exist_ok=True)
        atomic_write_text(
            self.candidate_path(project_id, candidate.candidate_id),
            candidate.to_markdown(),
        )
        with self.candidates_index_path(project_id).open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(candidate.to_dict(), ensure_ascii=False) + "\n")
        self.append_event(
            project_id,
            {
                "action": "candidate_created",
                "candidate_id": candidate.candidate_id,
                "source": source,
                "kind": kind,
                "source_semantics": source_semantics,
            },
        )
        return candidate

    def list_candidates(self, project_id: str) -> list[MemoryCandidate]:
        d = self.candidates_dir(project_id)
        if not d.is_dir():
            return []
        out: list[MemoryCandidate] = []
        for p in sorted(d.glob("candidate_*.md")):
            c = self._parse_candidate_file(p)
            if c is not None:
                out.append(c)
        return out

    def get_candidate(self, project_id: str, candidate_id: str) -> MemoryCandidate | None:
        p = self.candidate_path(project_id, candidate_id)
        if not p.exists():
            return None
        return self._parse_candidate_file(p)

    @staticmethod
    def _parse_candidate_file(path: Path) -> MemoryCandidate | None:
        try:
            raw = path.read_text(encoding="utf-8")
        except (FileNotFoundError, OSError):
            return None
        if not raw.startswith("---\n"):
            return None
        end = raw.find("\n---\n", 4)
        if end < 0:
            return None
        try:
            meta = json.loads(raw[4:end])
        except json.JSONDecodeError:
            return None
        return MemoryCandidate.from_dict(meta)

    # ── M2：确认（用户显式触发）──────────────────────────────────────

    @staticmethod
    def _normalized_content(text: str) -> str:
        """去重用的规范化：仅折叠空白与小写 —— 不做任何语义判断。"""
        return " ".join(text.split()).lower()

    def _detect_conflicts(
        self, project_id: str, candidate: MemoryCandidate
    ) -> list[str]:
        """保守冲突提示：同 kind 且引用了同一来源但内容不同。

        只报告，不裁决 —— §51：哪个是对的由用户判断，系统不替用户做决定。
        """
        conflicts: list[str] = []
        refs = {r for r in candidate.source_refs if r}
        for existing in self.list_memories(project_id):
            if existing.kind != candidate.kind:
                continue
            if refs & {r for r in existing.source_refs if r}:
                conflicts.append(existing.memory_id)
        return conflicts

    def confirm_candidate(
        self, project_id: str, candidate_id: str
    ) -> ConfirmResult:
        """用户显式确认 → 正式 Memory 落盘。

        - 内容与既有 Memory 规范化后相同 → status="duplicate"，**不新建**
          （去重提示；已有条目不受影响）
        - 与既有 Memory 语义冲突 → **仍然创建**，返回 conflicts 提示；
          绝不自动裁决 / 覆盖 / 删除（§51 / §67）
        """
        candidate = self.get_candidate(project_id, candidate_id)
        if candidate is None:
            raise FileNotFoundError(f"Candidate 不存在: {candidate_id}")

        warnings: list[str] = []
        duplicate_of: str | None = None
        conflicts = self._detect_conflicts(project_id, candidate)

        norm = self._normalized_content(candidate.content)
        for existing in self.list_memories(project_id):
            if self._normalized_content(existing.content) == norm:
                duplicate_of = existing.memory_id
                warnings.append(
                    f"内容与已有 Memory {existing.memory_id} 重复，未创建新条目"
                )
                break

        if duplicate_of is not None:
            self.append_event(
                project_id,
                {
                    "action": "duplicate_detected",
                    "candidate_id": candidate_id,
                    "duplicate_of": duplicate_of,
                },
            )
            return ConfirmResult(
                entry=None,
                status="duplicate",
                warnings=warnings,
                duplicate_of=duplicate_of,
                conflicts=conflicts,
            )

        # 正式 Memory：memory_NNN 递增编号，追加写 + 索引 + 事件日志
        n = 0
        for p in self.memory_dir(project_id).glob("memory_*.md"):
            try:
                n = max(n, int(p.stem.rsplit("_", 1)[1]))
            except (IndexError, ValueError):
                continue
        memory_id = f"memory_{n + 1:03d}"

        entry = candidate.confirm(memory_id)
        d = self.memory_dir(project_id)
        d.mkdir(parents=True, exist_ok=True)
        atomic_write_text(self.memory_path(project_id, memory_id), entry.to_markdown())
        with self.index_path(project_id).open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(entry.to_dict(), ensure_ascii=False) + "\n")

        if conflicts:
            warnings.append(
                "与以下已有 Memory 同 kind 同来源但内容不同，请人工核对（系统不裁决）："
                + ", ".join(conflicts)
            )
            self.append_event(
                project_id,
                {
                    "action": "conflict_detected",
                    "memory_id": memory_id,
                    "conflicts": conflicts,
                },
            )
        self.append_event(
            project_id,
            {
                "action": "memory_confirmed",
                "candidate_id": candidate_id,
                "memory_id": memory_id,
                "kind": entry.kind,
                "source_semantics": entry.source_semantics,
                "source": candidate.source,
            },
        )
        return ConfirmResult(
            entry=entry,
            status="created",
            warnings=warnings,
            conflicts=conflicts,
        )
