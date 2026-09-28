"""文件系统持久化（architecture_v0.1.md §20/§21、phase1_mock.md §10）。

目录布局：

    sessions/
    └── <session_id>/
        ├── state.json          SessionState（含每轮 candidate_ids）
        ├── question.md
        └── round_001/
            ├── qwen.md             回答正文（唯一存储）
            └── qwen.prompt.md      该模型实际收到的 prompt

原子写模式沿用 reference/ensemble 的实现思路（tempfile.mkstemp + os.replace），
保证轮询的读者永远看不到写了一半的文件。
  ensemble — MIT License, Copyright (c) 2026 Raiyan Yahya

不变量 A4 的结构性保障：
  本模块所有写回答 / 写 prompt 的方法都只接收正文字符串，签名里根本拿不到
  candidate_ids，因此映射不可能被写进 round_NNN/ 下的任何文件。
"""

from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path

from .state import SessionState


def atomic_write_text(path: Path, text: str, *, encoding: str = "utf-8") -> None:
    """原子写文本：先写同目录临时文件，再 os.replace 覆盖。

    思路来自 reference/ensemble（MIT）。同目录是必须的 —— 跨设备 os.replace 会失败。
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(dir=str(path.parent), prefix=".tmp-", suffix=".part")
    try:
        with os.fdopen(fd, "w", encoding=encoding) as fh:
            fh.write(text)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp_name, path)
    except BaseException:
        if os.path.exists(tmp_name):
            os.unlink(tmp_name)
        raise


class Storage:
    def __init__(self, root: str | Path):
        self.root = Path(root)

    # ---------- 路径 ----------

    def session_dir(self, session_id: str) -> Path:
        return self.root / session_id

    def round_dir(self, session_id: str, round_id: int) -> Path:
        return self.session_dir(session_id) / f"round_{round_id:03d}"

    def state_path(self, session_id: str) -> Path:
        return self.session_dir(session_id) / "state.json"

    def response_path(self, session_id: str, round_id: int, model_id: str) -> Path:
        return self.round_dir(session_id, round_id) / f"{model_id}.md"

    def prompt_path(self, session_id: str, round_id: int, model_id: str) -> Path:
        return self.round_dir(session_id, round_id) / f"{model_id}.prompt.md"

    # ---------- Project Memory（Phase 4.6 / M1；§62）----------
    #
    # 目录与 sessions/ 平级：
    #     <root>/sessions/<session_id>/...
    #     <root>/projects/<project_id>/memory/...
    #
    # 这里只提供**路径与目录**能力，不提供任何把 Session 内容搬进 projects/
    # 的方法 —— Plan §20.3：Summary / Compaction / Project Memory 三者之间
    # 无自动写入路径。

    def projects_root(self) -> Path:
        return self.root.parent / "projects"

    def project_dir(self, project_id: str) -> Path:
        return self.projects_root() / project_id

    def memory_dir(self, project_id: str) -> Path:
        return self.project_dir(project_id) / "memory"

    # ---------- Session ----------

    def ensure_session_dir(self, session_id: str) -> Path:
        d = self.session_dir(session_id)
        d.mkdir(parents=True, exist_ok=True)
        return d

    def save_state(self, session: SessionState) -> None:
        """保存 SessionState。candidate_ids 只会出现在这个文件里（A4）。"""
        self.ensure_session_dir(session.session_id)
        atomic_write_text(
            self.state_path(session.session_id),
            json.dumps(session.to_dict(), ensure_ascii=False, indent=2),
        )

    def load_state(self, session_id: str) -> SessionState:
        p = self.state_path(session_id)
        if not p.exists():
            raise FileNotFoundError(f"Session 不存在: {session_id}")
        data = json.loads(p.read_text(encoding="utf-8"))
        return SessionState.from_dict(data)

    def session_exists(self, session_id: str) -> bool:
        return self.state_path(session_id).exists()

    def save_question(self, session_id: str, question: str) -> None:
        atomic_write_text(self.session_dir(session_id) / "question.md", question)

    # ---------- Round 内容 ----------

    def save_response(self, session_id: str, round_id: int, model_id: str, content: str) -> Path:
        """写回答正文。**不得覆盖已有文件**（architecture_v0.1.md D5）。"""
        p = self.response_path(session_id, round_id, model_id)
        if p.exists():
            raise FileExistsError(f"拒绝覆盖历史回答: {p}")
        atomic_write_text(p, content)
        return p

    def save_prompt(self, session_id: str, round_id: int, model_id: str, prompt: str) -> Path:
        """写该模型实际收到的 prompt，供人工核对匿名化结果。

        同样不允许覆盖 —— prompt 是本轮的历史事实。
        """
        p = self.prompt_path(session_id, round_id, model_id)
        if p.exists():
            raise FileExistsError(f"拒绝覆盖历史 prompt: {p}")
        atomic_write_text(p, prompt)
        return p

    def load_responses(self, session_id: str, round_id: int) -> dict[str, str]:
        """读取某轮全部回答正文。CLOSED / 未参与的模型自然没有文件，也就不会出现在结果里。"""
        d = self.round_dir(session_id, round_id)
        if not d.is_dir():
            return {}
        out: dict[str, str] = {}
        for p in sorted(d.glob("*.md")):
            if p.name.endswith(".prompt.md"):
                continue
            out[p.stem] = p.read_text(encoding="utf-8")
        return out

    def load_prompt(self, session_id: str, round_id: int, model_id: str) -> str:
        p = self.prompt_path(session_id, round_id, model_id)
        if not p.exists():
            raise FileNotFoundError(f"prompt 不存在: {p}")
        return p.read_text(encoding="utf-8")

    def round_files(self, session_id: str, round_id: int) -> list[Path]:
        d = self.round_dir(session_id, round_id)
        if not d.is_dir():
            return []
        return sorted(d.iterdir())
