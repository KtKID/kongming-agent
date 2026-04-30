"""unit：``scripts/test-with-log.sh`` wrapper 脚本的行为验证。

覆盖：

1. 跑通 → 日志文件按 ``<stem>-YYYYMMDD-HHMMSS.log`` 命名落到 ``.kongming/test-logs/``
2. 日志内容含 pytest stdout（如 ``passed`` / 测试名）
3. 日志头/尾含 command / started / ended / exit_code 元数据
4. exit code 透传（pytest pass=0；不存在的测试文件 → 非 0）
5. 入参不含 tests/... 路径时 stem 退化为 "pytest"
"""

from __future__ import annotations

import os
import re
import subprocess
import sys
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parents[2]
_SCRIPT = _REPO_ROOT / "scripts" / "test-with-log.sh"
_LOG_DIR = _REPO_ROOT / ".kongming" / "test-logs"


def _run(args: list[str]) -> tuple[int, str]:
    """运行 wrapper 脚本，返回 (exit_code, combined_output)。"""
    proc = subprocess.run(
        ["bash", str(_SCRIPT), *args],
        cwd=_REPO_ROOT,
        capture_output=True,
        text=True,
        timeout=120,
    )
    return proc.returncode, proc.stdout + proc.stderr


def _latest_log(stem: str) -> Path | None:
    """返回 .kongming/test-logs 下最新的 <stem>-*.log；不存在返回 None。"""
    if not _LOG_DIR.exists():
        return None
    candidates = sorted(_LOG_DIR.glob(f"{stem}-*.log"))
    return candidates[-1] if candidates else None


@pytest.fixture(autouse=True)
def _skip_on_windows() -> None:
    if sys.platform == "win32":
        pytest.skip("bash wrapper not supported on Windows")


def test_script_writes_log_for_passing_test() -> None:
    """跑一个真实存在的简单单测 → 日志文件落盘 + 含 PASSED 字样 + exit 0。"""
    # 用一个本身存在的轻量测试做 smoke 对象
    target = "tests/unit/test_setting_yaml_safety_rules.py::test_setting_yaml_loads_without_validation_error"
    exit_code, output = _run([target, "-q"])
    assert exit_code == 0, f"wrapper failed: {output[-500:]}"

    # 日志文件存在且新鲜
    log = _latest_log("test_setting_yaml_safety_rules")
    assert log is not None and log.is_file(), f"log not created in {_LOG_DIR}"
    # mtime 在最近 60 秒内（防止误命中老日志）
    age = abs(log.stat().st_mtime - os.path.getmtime(log))
    assert age < 60

    text = log.read_text(encoding="utf-8")
    # 头：含 command / started / cwd
    assert "=== test-with-log.sh ===" in text
    assert "command: uv run pytest" in text
    assert "started:" in text
    # pytest 输出：含 passed
    assert "passed" in text.lower()
    # 尾：含 ended / exit_code: 0
    assert "ended:" in text
    assert "exit_code: 0" in text


def test_script_filename_format_uses_test_stem_and_timestamp() -> None:
    """日志文件名形如 ``<test_stem>-YYYYMMDD-HHMMSS.log``，stem 来自第一个 tests/... 入参。"""
    target = "tests/unit/test_setting_yaml_safety_rules.py"
    _run([target, "-q", "-k", "test_setting_yaml_loads_without_validation_error"])

    log = _latest_log("test_setting_yaml_safety_rules")
    assert log is not None
    # 文件名严格匹配 <stem>-YYYYMMDD-HHMMSS.log
    assert re.match(
        r"^test_setting_yaml_safety_rules-\d{8}-\d{6}\.log$",
        log.name,
    ), f"bad filename: {log.name}"


def test_script_propagates_exit_code_on_failure() -> None:
    """不存在的测试节点 → pytest exit code 非 0，wrapper 透传。"""
    exit_code, output = _run(
        ["tests/unit/test_setting_yaml_safety_rules.py::test_does_not_exist", "-q"]
    )
    assert exit_code != 0, f"expected non-zero exit, got 0; output:\n{output}"

    log = _latest_log("test_setting_yaml_safety_rules")
    assert log is not None
    text = log.read_text(encoding="utf-8")
    # 失败也要写完整尾部 + exit_code 非 0
    assert "ended:" in text
    assert "exit_code: 0" not in text  # 不能误写成 0


def test_script_falls_back_to_pytest_stem_when_no_test_path() -> None:
    """入参不含 tests/... 路径（仅 -h 等 flag）→ stem 退化为 "pytest"。"""
    # `-h` 只打印 pytest 帮助，exit 0，不跑测试
    exit_code, _ = _run(["-h"])
    assert exit_code == 0

    # 落到 pytest-*.log
    log = _latest_log("pytest")
    assert log is not None
    assert re.match(r"^pytest-\d{8}-\d{6}\.log$", log.name), f"bad filename: {log.name}"


def test_script_handles_pytest_node_id_with_double_colon() -> None:
    """node id 形如 tests/x.py::test_y → stem 取自 x（去掉 .py 和 ::test_y）。"""
    target = "tests/unit/test_setting_yaml_safety_rules.py::test_setting_yaml_loads_without_validation_error"
    _run([target, "-q"])

    log = _latest_log("test_setting_yaml_safety_rules")
    assert log is not None
    # stem 不应含 :: 或 .py
    assert "::" not in log.name
    assert ".py" not in log.name.replace(".log", "")
