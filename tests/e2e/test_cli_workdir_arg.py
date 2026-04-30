"""``--workdir / -C`` e2e：通过 subprocess 起真 CLI 进程验证。

不依赖 LLM。两个测试场景：

1. ``--workdir <invalid>`` → click 校验失败，进程退出且 stderr 含错误信息
2. ``--help`` 输出含 ``--workdir`` 帮助文案（验证 click 装配链路完整）
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

_TIMEOUT_SEC = 30


@pytest.mark.e2e
def test_subprocess_help_lists_workdir_option(tmp_path: Path) -> None:
    """真进程跑 ``kongming --help`` 应列出 ``--workdir`` 选项。"""
    result = subprocess.run(
        [sys.executable, "-m", "cli.main", "--help"],
        capture_output=True,
        text=True,
        timeout=_TIMEOUT_SEC,
        cwd=str(tmp_path),
    )
    assert result.returncode == 0, f"--help should succeed; stderr={result.stderr!r}"
    assert "--workdir" in result.stdout
    assert "-C" in result.stdout


@pytest.mark.e2e
def test_subprocess_rejects_nonexistent_workdir(tmp_path: Path) -> None:
    """真进程跑 ``kongming --workdir /no/such/dir`` 应非零退出 + 错误信息。"""
    bogus = tmp_path / "absolutely-not-here"
    result = subprocess.run(
        [sys.executable, "-m", "cli.main", "--workdir", str(bogus)],
        capture_output=True,
        text=True,
        timeout=_TIMEOUT_SEC,
        cwd=str(tmp_path),
    )
    assert result.returncode != 0
    combined = (result.stderr + result.stdout).lower()
    assert "does not exist" in combined, (
        f"expected 'does not exist' in output; got stderr={result.stderr!r} stdout={result.stdout!r}"
    )


@pytest.mark.e2e
def test_subprocess_rejects_workdir_that_is_a_file(tmp_path: Path) -> None:
    """``--workdir <file>`` 应非零退出（click ``file_okay=False`` 校验）。"""
    f = tmp_path / "i_am_a_file.txt"
    f.write_text("hi")
    result = subprocess.run(
        [sys.executable, "-m", "cli.main", "--workdir", str(f)],
        capture_output=True,
        text=True,
        timeout=_TIMEOUT_SEC,
        cwd=str(tmp_path),
    )
    assert result.returncode != 0
    combined = (result.stderr + result.stdout).lower()
    assert "is a file" in combined or "file" in combined, (
        f"expected file-related error; got stderr={result.stderr!r} stdout={result.stdout!r}"
    )
