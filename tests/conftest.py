from __future__ import annotations

import shutil
from pathlib import Path

import pytest

TMP_ROOT = Path(__file__).resolve().parent / ".tmp"


@pytest.fixture
def tmp_spec_dir() -> Path:
    """仓库内临时目录（沙箱不允许写系统 tmp）。"""
    d = TMP_ROOT / "spec"
    shutil.rmtree(d, ignore_errors=True)
    d.mkdir(parents=True, exist_ok=True)
    yield d
    shutil.rmtree(d, ignore_errors=True)
