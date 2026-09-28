"""pytest 会话级配置。

主要目的：避免 pytest 默认的 tmp_path 清理触发沙箱 safe-delete 策略，
导致退出码为 1 但所有断言实际通过的情况。
"""

from __future__ import annotations

import os
import shutil
import tempfile
from pathlib import Path

import pytest


@pytest.fixture
def tmp_path() -> Path:
    """在项目内 .pytest_tmp/ 下创建测试专用临时目录，测试结束后保留供人工检查。"""
    base = Path(__file__).resolve().parent / ".pytest_tmp"
    base.mkdir(exist_ok=True)
    prefix = f"tmp-{os.getpid()}-"
    d = tempfile.mkdtemp(prefix=prefix, dir=base)
    path = Path(d)
    try:
        yield path
    finally:
        # 保留目录，避免触发沙箱批量删除确认；
        # 如需清理可手动删除 .pytest_tmp/。
        pass
