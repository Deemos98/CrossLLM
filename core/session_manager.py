"""Session 生命周期（architecture_v0.1.md §21）。

职责：创建 Session、读取 Session、切换模型的 ACTIVE / CLOSED。
不负责 Round 编排（那是 core/round_manager.py 的事）。
"""

from __future__ import annotations

import uuid
from datetime import datetime

from .model_manager import ModelRegistry
from .state import SessionState
from .storage import Storage


def _now() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


def _new_session_id() -> str:
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    return f"{stamp}-{uuid.uuid4().hex[:6]}"


class SessionManager:
    def __init__(self, storage: Storage, registry: ModelRegistry):
        self.storage = storage
        self.registry = registry

    def create(self, question: str) -> SessionState:
        """创建 Session。默认全部模型 ACTIVE。"""
        question = (question or "").strip()
        if not question:
            raise ValueError("question 不能为空")

        now = _now()
        session = SessionState(
            session_id=_new_session_id(),
            original_question=question,
            current_round=0,
            active_models=self.registry.ids(),
            closed_models=[],
            rounds=[],
            created_at=now,
            updated_at=now,
        )
        self.storage.save_state(session)
        self.storage.save_question(session.session_id, question)
        return session

    def get(self, session_id: str) -> SessionState:
        """读取 Session。用于页面刷新后的状态恢复（phase1_mock.md §16）。"""
        return self.storage.load_state(session_id)

    def list_sessions(self) -> list[dict]:
        """列出全部已持久化 Session 的摘要（只读导航用）。

        边界（与 Project Memory / Retrieval / Context Injection 无关，只是列表）：
        - 只读取已有 state.json，不写任何 Session 数据
        - 按 updated_at 倒序
        - 单个 Session 缺失 / 损坏（JSON 解析失败、字段缺失）一律跳过，
          不影响整个列表；目录根不存在时返回空列表
        - 不触发 Summary / Compaction / Evidence / Memory
        """
        root = self.storage.root
        if not root.is_dir():
            return []

        summaries: list[dict] = []
        for entry in root.iterdir():
            if not entry.is_dir():
                continue
            try:
                state = self.storage.load_state(entry.name)
            except (OSError, ValueError, KeyError, TypeError):
                # 单个 Session 损坏不影响整体：静默跳过
                continue
            summaries.append(
                {
                    "session_id": state.session_id,
                    "created_at": state.created_at,
                    "updated_at": state.updated_at,
                    "original_question": state.original_question,
                    "current_round": state.current_round,
                }
            )

        summaries.sort(key=lambda s: s["updated_at"] or "", reverse=True)
        return summaries

    def set_selection(self, session_id: str, active_models: list[str]) -> SessionState:
        """切换 ACTIVE / CLOSED（phase1_mock.md §12）。

        语义（architecture_v0.1.md P6）：
        - CLOSED 只代表"当前停止参与后续 Round"，**不代表该模型已被证明错误**
        - CLOSED 模型的历史回答永久保留，不从磁盘删除（D7）
        """
        session = self.storage.load_state(session_id)

        unknown = [m for m in active_models if not self.registry.has(m)]
        if unknown:
            raise KeyError(f"未知 model_id: {unknown}")

        active = [m for m in self.registry.ids() if m in set(active_models)]
        closed = [m for m in self.registry.ids() if m not in set(active_models)]

        session.active_models = active
        session.closed_models = closed
        session.updated_at = _now()
        self.storage.save_state(session)
        return session
