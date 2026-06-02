"""manage-config-tab dev-checklist #4 — restart.py 单元测试。

覆盖三件事：

1. ``find_start_script`` 对"不存在 / 存在但无执行位 / 存在且可执行"三态的反应。
2. ``trigger_restart`` 通过 mock 验证 ``subprocess.Popen`` 调用参数（detached
   session + DEVNULL fds + 正确 argv + cwd），不真的拉起 ``start.sh``。
3. ``trigger_restart`` 在脚本缺失时透传 ``RestartScriptNotFoundError``。
"""

from __future__ import annotations

import os
import stat
import subprocess
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from web.dashboard.config.restart import (
    RestartScriptNotFoundError,
    find_start_script,
    trigger_restart,
)


def _write_executable_start_script(repo_root: Path) -> Path:
    script = repo_root / "start.sh"
    script.write_text("#!/bin/sh\necho stub\n")
    mode = script.stat().st_mode
    script.chmod(mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    return script


# ---------------------------------------------------------------------------
# find_start_script
# ---------------------------------------------------------------------------


def test_find_start_script_returns_path_when_executable(tmp_path: Path) -> None:
    script = _write_executable_start_script(tmp_path)

    found = find_start_script(tmp_path)

    assert found == script
    assert os.access(found, os.X_OK)


def test_find_start_script_raises_when_missing(tmp_path: Path) -> None:
    with pytest.raises(RestartScriptNotFoundError) as excinfo:
        find_start_script(tmp_path)

    assert "not found" in str(excinfo.value)


def test_find_start_script_raises_when_not_executable(tmp_path: Path) -> None:
    script = tmp_path / "start.sh"
    script.write_text("#!/bin/sh\n")
    script.chmod(0o644)  # 显式去掉执行位

    with pytest.raises(RestartScriptNotFoundError) as excinfo:
        find_start_script(tmp_path)

    assert "not executable" in str(excinfo.value)


# ---------------------------------------------------------------------------
# trigger_restart
# ---------------------------------------------------------------------------


def test_trigger_restart_spawns_detached_with_devnull(tmp_path: Path) -> None:
    script = _write_executable_start_script(tmp_path)

    fake_proc = MagicMock()
    fake_proc.pid = 4242

    with patch("web.dashboard.config.restart.subprocess.Popen", return_value=fake_proc) as popen:
        pid = trigger_restart(tmp_path)

    assert pid == 4242
    popen.assert_called_once()

    args, kwargs = popen.call_args
    argv = args[0]
    assert argv == ["nohup", str(script), "web", "restart"]
    assert kwargs["cwd"] == str(tmp_path)
    assert kwargs["stdin"] is subprocess.DEVNULL
    assert kwargs["stdout"] is subprocess.DEVNULL
    assert kwargs["stderr"] is subprocess.DEVNULL
    assert kwargs["start_new_session"] is True
    assert kwargs["close_fds"] is True


def test_trigger_restart_raises_when_script_missing(tmp_path: Path) -> None:
    # tmp_path 故意不放 start.sh
    with patch("web.dashboard.config.restart.subprocess.Popen") as popen:
        with pytest.raises(RestartScriptNotFoundError):
            trigger_restart(tmp_path)

    popen.assert_not_called()


def test_trigger_restart_raises_when_script_not_executable(tmp_path: Path) -> None:
    script = tmp_path / "start.sh"
    script.write_text("#!/bin/sh\n")
    script.chmod(0o644)

    with patch("web.dashboard.config.restart.subprocess.Popen") as popen:
        with pytest.raises(RestartScriptNotFoundError):
            trigger_restart(tmp_path)

    popen.assert_not_called()
