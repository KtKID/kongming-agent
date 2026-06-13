"""full-log-v0.1 阶段 1 端到端冒烟（dev-checklist #10）。

不启动真实 web server，但走完整链路：

1. ``init_full_logger`` 装配单例
2. ``WSEventSink(ws, thread_id=...)`` 构造
3. 模拟 runner emit ``turn.start`` / ``turn.end`` Event
4. 验证 ``.kongming/logs/full_log.jsonl`` 出现 2 条记录
5. 验证记录 schema 严格符合 ``docs/full-log-v0.1/README.md`` §2

冒烟测试用项目根下的 ``.kongming/logs/full_log.smoke.jsonl``（跟生产路径
``.kongming/logs/full_log.jsonl`` 同目录但加 ``.smoke`` 后缀，避免覆盖）——
便于开发者 ``cat`` 真实样本审视格式。测试结束**不删除**该文件（留给用户审）。
"""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import AsyncMock

import pytest

from core.contracts import Event
from devtools.full_logger import _reset_for_tests, init_full_logger
from hosts.web.websocket.event_sink import WSEventSink

# 项目 .kongming/logs/ 下的固定输出位置——跟生产 full_log.jsonl 同 home，
# dev 可直接 cat 这个文件审视格式。
_REPO_ROOT = Path(__file__).resolve().parents[2]
_SMOKE_LOG_PATH = _REPO_ROOT / ".kongming" / "logs" / "full_log.smoke.jsonl"


class _StubConfig:
    def __init__(self, *, enabled: bool, path: str) -> None:
        self.enabled = enabled
        self.path = path
        self.queue_size = 100
        self.rotate_daily = False


@pytest.fixture
def smoke_log_path() -> Path:
    """每次跑前清空 smoke 日志，让样本是这一次跑的真实输出。"""
    _SMOKE_LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
    if _SMOKE_LOG_PATH.exists():
        _SMOKE_LOG_PATH.unlink()
    return _SMOKE_LOG_PATH


@pytest.fixture(autouse=True)
def _reset() -> None:
    _reset_for_tests()
    yield
    _reset_for_tests()


async def test_smoke_turn_start_end_pair_writes_full_log(smoke_log_path: Path) -> None:
    """完整链路冒烟：装配 → emit → 读盘 → 校验 schema。"""
    # 1. 装配
    cfg = _StubConfig(enabled=True, path=str(smoke_log_path))
    flogger = init_full_logger(cfg)
    await flogger.start()
    assert flogger.enabled is True

    # 2. 模拟 sink（真实 WSEventSink）
    ws = AsyncMock()
    ws.send_json = AsyncMock(return_value=None)
    sink = WSEventSink(ws, thread_id="thread-abc123def456")

    # 3. 模拟一次完整 turn：runner emit turn.start → ...（中间帧不记录）→ turn.end
    await sink.emit(Event(kind="turn.start", run_id="run-001-turn-0", turn=0))
    # 阶段 1 白名单：content.delta / tool.* / usage 等不写 full_log，
    # 但 send_json 仍会被调用（保证浏览器流式渲染不破坏）。
    await sink.emit(
        Event(kind="content.delta", run_id="run-001-turn-0", turn=0, payload={"delta": "hi"})
    )
    await sink.emit(Event(kind="turn.end", run_id="run-001-turn-0", turn=0))

    # 4. flush 队列
    await flogger.aclose()

    # 5. 读盘 + 校验
    assert smoke_log_path.exists()
    raw = smoke_log_path.read_text(encoding="utf-8")
    lines = [json.loads(line) for line in raw.strip().splitlines()]

    # 应该只有 turn.start + turn.end 两行（content.delta 不在阶段 1 白名单）
    assert len(lines) == 2, f"期望 2 行（turn.start + turn.end），实际 {len(lines)}: {lines}"

    # schema 校验：每行必须有 6 个 key
    expected_keys = {"ts", "dir", "channel", "thread_id", "client_id", "payload"}
    for line in lines:
        assert set(line.keys()) == expected_keys

    # turn.start
    assert lines[0]["dir"] == "s2c"
    assert lines[0]["channel"] == "ws.threads"
    assert lines[0]["thread_id"] == "thread-abc123def456"
    assert lines[0]["client_id"] is None
    assert lines[0]["payload"]["frame_type"] == "turn.start"
    assert lines[0]["payload"]["turn"] == 0
    assert lines[0]["payload"]["run_id"] == "run-001-turn-0"
    # ts 必须是 UTC+8 ISO8601 毫秒精度
    assert lines[0]["ts"].endswith("+08:00")
    # 形如 2026-05-26T22:33:21.123+08:00
    import re

    assert re.match(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d{3}\+08:00$", lines[0]["ts"]), (
        f"ts 格式错误: {lines[0]['ts']}"
    )

    # turn.end
    assert lines[1]["payload"]["frame_type"] == "turn.end"
    assert lines[1]["payload"]["turn"] == 0

    # 把样本路径打印出来方便 dev 直接 cat（用 -s 跑可见）
    print(f"\n=== full_log smoke sample written to: {smoke_log_path} ===")
    print(raw)
