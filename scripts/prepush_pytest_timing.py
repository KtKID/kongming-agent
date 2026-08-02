"""pytest item 级耗时日志插件。

本脚本用于 pre-push 门禁中的 pytest 运行：
1. 从 ``KONGMING_PRE_PUSH_PYTEST_TIMING_LOG`` 读取 JSONL 输出路径；
2. 每个测试开始时写入 ``test_start`` 事件；
3. 每个 setup / call / teardown 阶段结束时写入 ``test_phase`` 事件；
4. 每个测试结束时写入 ``test_finish`` 事件。

关键函数：
- ``_write_event``：把结构化事件追加写入 JSONL 并立即 flush。
- ``pytest_configure``：打开日志文件。
- ``pytest_unconfigure``：关闭日志文件。
- ``pytest_runtest_logstart``：记录测试开始。
- ``pytest_runtest_logreport``：记录测试阶段耗时。
- ``pytest_runtest_logfinish``：记录测试结束。
"""

from __future__ import annotations

import json
import os
import time
from pathlib import Path
from typing import Any, TextIO

_LOG_HANDLE: TextIO | None = None


def _now() -> float:
    """返回当前 wall-clock 时间戳，输出单位是秒。"""

    return time.time()


def _write_event(record: dict[str, Any]) -> None:
    """写入一条 JSONL 事件，关键输入是事件字典，关键输出是落盘记录。"""

    if _LOG_HANDLE is None:
        return
    enriched = {"time": _now(), **record}
    _LOG_HANDLE.write(json.dumps(enriched, ensure_ascii=True, sort_keys=True) + "\n")
    _LOG_HANDLE.flush()


def pytest_configure(config: Any) -> None:
    """pytest 启动时打开 timing 日志文件，关键输入来自环境变量。"""

    global _LOG_HANDLE
    raw_path = os.environ.get("KONGMING_PRE_PUSH_PYTEST_TIMING_LOG", "").strip()
    if not raw_path:
        return
    log_path = Path(raw_path)
    log_path.parent.mkdir(parents=True, exist_ok=True)
    _LOG_HANDLE = log_path.open("a", encoding="utf-8", newline="")
    _write_event({"event": "session_start", "rootpath": str(config.rootpath)})


def pytest_unconfigure(config: Any) -> None:
    """pytest 结束时写入 session 结束事件并关闭 timing 日志。"""

    global _LOG_HANDLE
    if _LOG_HANDLE is None:
        return
    _write_event({"event": "session_finish", "rootpath": str(config.rootpath)})
    _LOG_HANDLE.close()
    _LOG_HANDLE = None


def pytest_runtest_logstart(nodeid: str, location: tuple[str, int | None, str]) -> None:
    """测试开始时记录 nodeid 和源码位置，用于超时定位最后启动项。"""

    path, line, name = location
    _write_event(
        {
            "event": "test_start",
            "nodeid": nodeid,
            "path": path,
            "line": line,
            "name": name,
        }
    )


def pytest_runtest_logreport(report: Any) -> None:
    """测试阶段结束时记录耗时、阶段和结果，用于统计最慢项。"""

    _write_event(
        {
            "event": "test_phase",
            "nodeid": report.nodeid,
            "when": report.when,
            "outcome": report.outcome,
            "duration_s": report.duration,
        }
    )


def pytest_runtest_logfinish(nodeid: str, location: tuple[str, int | None, str]) -> None:
    """测试结束时记录 nodeid 和源码位置，用于核对开始/结束事件。"""

    path, line, name = location
    _write_event(
        {
            "event": "test_finish",
            "nodeid": nodeid,
            "path": path,
            "line": line,
            "name": name,
        }
    )
