"""FullLogger 单元测试（full-log-v0.1 阶段 1 任务 #8）。

覆盖：

1. enabled=False → 零写入
2. 时间戳 UTC+8 ISO8601 毫秒精度
3. 中文 payload 完整往返不被 escape
4. 队列满丢最旧 + 一次性 warning
5. writer 异常自愈（mock json.dumps 抛一次）
6. aclose() flush 剩余队列
7. 并发写入无行损坏
8. 写盘失败 → enabled 降级 False
9. init_full_logger / get_full_logger 单例语义
10. JSONL 行结构必含 6 个 key
"""

from __future__ import annotations

import asyncio
import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest

from devtools.full_logger import (
    FullLogger,
    _reset_for_tests,
    get_full_logger,
    init_full_logger,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


@dataclass
class _CfgStub:
    """鸭子类型 config 桩，模拟 WebFullLogConfig。"""

    enabled: bool
    path: str
    queue_size: int = 10000
    rotate_daily: bool = True


def _read_jsonl(path: Path) -> list[dict[str, Any]]:
    """读 JSONL → list[dict]；不存在时返回空列表。"""
    if not path.exists():
        return []
    lines = path.read_text(encoding="utf-8").splitlines()
    return [json.loads(line) for line in lines if line.strip()]


@pytest.fixture(autouse=True)
def _isolate_singleton() -> None:
    """每个测试前后 reset 模块级单例，确保测试隔离。"""
    _reset_for_tests()
    yield
    _reset_for_tests()


# ---------------------------------------------------------------------------
# 1. enabled=False 零写入
# ---------------------------------------------------------------------------


async def test_enabled_false_no_write(tmp_path: Path) -> None:
    log_path = tmp_path / "full_log.jsonl"
    logger = FullLogger(enabled=False, path=str(log_path), queue_size=100)
    await logger.start()
    await logger.log("s2c", "ws.threads", {"frame_type": "turn.start"})
    await logger.log("s2c", "ws.threads", {"frame_type": "turn.end"})
    await logger.aclose()

    # 文件根本不应被创建。
    assert not log_path.exists()


@pytest.mark.unit
async def test_kongming_relative_path_uses_kongming_home(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    home = tmp_path / "home"
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    monkeypatch.setenv("KONGMING_HOME", str(home))
    monkeypatch.chdir(workspace)

    logger = FullLogger(
        enabled=True,
        path=".kongming/logs/full_log.jsonl",
        queue_size=100,
    )

    assert logger.path == (home / "logs" / "full_log.jsonl").resolve()


# ---------------------------------------------------------------------------
# 2. 时间戳 UTC+8 ISO8601 毫秒精度
# ---------------------------------------------------------------------------


async def test_timestamp_utc8_format(tmp_path: Path) -> None:
    log_path = tmp_path / "full_log.jsonl"
    logger = FullLogger(enabled=True, path=str(log_path), queue_size=100)
    await logger.start()
    await logger.log("s2c", "ws.threads", {"frame_type": "turn.start"})
    await logger.aclose()

    records = _read_jsonl(log_path)
    assert len(records) == 1
    ts = records[0]["ts"]
    # 形如：2026-05-26T22:33:21.123+08:00
    assert re.match(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d{3}\+08:00$", ts), (
        f"ts {ts!r} 不符合 UTC+8 毫秒精度 ISO8601 格式"
    )


# ---------------------------------------------------------------------------
# 3. 中文 payload 完整往返
# ---------------------------------------------------------------------------


async def test_payload_roundtrip(tmp_path: Path) -> None:
    log_path = tmp_path / "full_log.jsonl"
    logger = FullLogger(enabled=True, path=str(log_path), queue_size=100)
    await logger.start()
    payload = {"frame_type": "turn.start", "中文": "测试", "nested": {"key": "值"}}
    await logger.log("s2c", "ws.threads", payload, thread_id="t1", client_id="c1")
    await logger.aclose()

    # 必须能直接 json.loads（ensure_ascii=False 保证），且中文不被 escape 成 \uXXXX
    raw = log_path.read_text(encoding="utf-8")
    assert "中文" in raw, "中文未保留原样（被 ensure_ascii 编码了）"
    assert "测试" in raw

    records = _read_jsonl(log_path)
    assert len(records) == 1
    assert records[0]["payload"] == payload
    assert records[0]["thread_id"] == "t1"
    assert records[0]["client_id"] == "c1"


# ---------------------------------------------------------------------------
# 4. 队列满丢最旧 + 一次性 warning
# ---------------------------------------------------------------------------


async def test_queue_overflow_drop_oldest(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    log_path = tmp_path / "full_log.jsonl"
    # queue_size=2，不 start writer task，让队列保持满状态。
    logger = FullLogger(enabled=True, path=str(log_path), queue_size=2)
    # 手动构造 queue，但不启动 writer，让队列堆积。
    # 走 start() 但让 writer 来不及消费 —— 用 monkeypatch 拦截 writer。
    await logger.start()
    # 关掉 writer task，让队列真的会满；后面再让 aclose 处理。
    assert logger._writer_task is not None
    logger._writer_task.cancel()
    try:
        await logger._writer_task
    except (asyncio.CancelledError, Exception):
        pass
    logger._writer_task = None

    # queue_size=2 → 灌 5 条 → 应只剩最后 2 条（前 3 条被丢）。
    for i in range(5):
        await logger.log("s2c", "ws.threads", {"i": i})

    # capsys 校验 stderr 只 emit 一次溢出 warning
    captured = capsys.readouterr()
    assert captured.err.count("queue full") == 1, (
        f"stderr 应只出现 1 次 'queue full' warning，实际：{captured.err!r}"
    )

    # 把 writer 重新拉起来 + aclose 让剩余记录落盘
    logger._writer_task = asyncio.create_task(
        logger._writer_loop(), name="full-logger-writer-resumed"
    )
    await logger.aclose()

    records = _read_jsonl(log_path)
    # 队列满后丢最旧：最终保留的应该是后入队的（i=3, i=4）。
    assert len(records) == 2
    assert [r["payload"]["i"] for r in records] == [3, 4]


# ---------------------------------------------------------------------------
# 5. writer 异常自愈
# ---------------------------------------------------------------------------


async def test_writer_exception_self_heal(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    log_path = tmp_path / "full_log.jsonl"
    logger = FullLogger(enabled=True, path=str(log_path), queue_size=100)

    # mock json.dumps，第一次抛异常，后续正常。
    real_dumps = json.dumps
    call_count = {"n": 0}

    def flaky_dumps(*args: Any, **kwargs: Any) -> str:
        call_count["n"] += 1
        if call_count["n"] == 1:
            raise RuntimeError("simulated json error")
        return real_dumps(*args, **kwargs)

    monkeypatch.setattr("devtools.full_logger.json.dumps", flaky_dumps)

    await logger.start()
    # 第一条：json.dumps 抛错 → 跳过这条 + emit serialize 失败 warning，记录被丢
    await logger.log("s2c", "ws.threads", {"i": 1})
    # 给 writer 时间消费
    await asyncio.sleep(0.05)
    # 第二条：应该正常落盘
    await logger.log("s2c", "ws.threads", {"i": 2})
    await logger.aclose()

    captured = capsys.readouterr()
    # 序列化失败 warning 应该 emit 过
    assert "serialize failed" in captured.err, f"序列化失败 warning 缺失，stderr: {captured.err!r}"

    # 第二条记录应该落盘
    records = _read_jsonl(log_path)
    assert len(records) == 1
    assert records[0]["payload"]["i"] == 2


async def test_writer_task_crash_restart(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """直接 mock _write_one 让 writer task 整个挂掉 —— 应自愈重启后继续写。"""
    log_path = tmp_path / "full_log.jsonl"
    logger = FullLogger(enabled=True, path=str(log_path), queue_size=100)

    real_write = logger._write_one
    call_count = {"n": 0}

    async def flaky_write(record: dict[str, Any]) -> None:
        call_count["n"] += 1
        if call_count["n"] == 1:
            # 模拟非 OSError 的崩溃，让外层 except Exception 捕获后重启
            raise RuntimeError("simulated writer crash")
        await real_write(record)

    logger._write_one = flaky_write  # type: ignore[method-assign]

    await logger.start()
    await logger.log("s2c", "ws.threads", {"i": 1})
    # 给 writer 崩溃 + 重启时间
    await asyncio.sleep(0.05)
    await logger.log("s2c", "ws.threads", {"i": 2})
    await logger.aclose()

    captured = capsys.readouterr()
    assert "writer task crashed" in captured.err, (
        f"writer crash warning 缺失，stderr: {captured.err!r}"
    )

    # 第二条应该落盘（第一条因 crash 丢失）
    records = _read_jsonl(log_path)
    assert len(records) == 1
    assert records[0]["payload"]["i"] == 2


# ---------------------------------------------------------------------------
# 6. aclose flush 剩余队列
# ---------------------------------------------------------------------------


async def test_aclose_flushes_queue(tmp_path: Path) -> None:
    log_path = tmp_path / "full_log.jsonl"
    logger = FullLogger(enabled=True, path=str(log_path), queue_size=100)
    await logger.start()
    for i in range(10):
        await logger.log("s2c", "ws.threads", {"i": i})
    await logger.aclose()

    records = _read_jsonl(log_path)
    assert len(records) == 10
    assert [r["payload"]["i"] for r in records] == list(range(10))


# ---------------------------------------------------------------------------
# 7. 并发写入无行损坏
# ---------------------------------------------------------------------------


async def test_concurrent_writers_no_corruption(tmp_path: Path) -> None:
    log_path = tmp_path / "full_log.jsonl"
    logger = FullLogger(enabled=True, path=str(log_path), queue_size=10000)
    await logger.start()

    async def writer(worker_id: int) -> None:
        for j in range(100):
            await logger.log("s2c", "ws.threads", {"worker": worker_id, "seq": j})

    await asyncio.gather(*(writer(i) for i in range(10)))
    await logger.aclose()

    # 每行都能合法 json.loads
    text = log_path.read_text(encoding="utf-8")
    lines = [ln for ln in text.splitlines() if ln.strip()]
    assert len(lines) == 1000, f"应 1000 行，实际 {len(lines)}"
    for ln in lines:
        # 不应抛 JSONDecodeError
        record = json.loads(ln)
        assert "ts" in record
        assert "payload" in record


# ---------------------------------------------------------------------------
# 8. 写盘失败 → enabled 降级 False
# ---------------------------------------------------------------------------


async def test_write_failure_disables_logger(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    # 父目录是个文件而不是目录 → mkdir 会 OSError。
    bad_parent = tmp_path / "not_a_dir"
    bad_parent.write_text("blocker", encoding="utf-8")
    log_path = bad_parent / "full_log.jsonl"

    logger = FullLogger(enabled=True, path=str(log_path), queue_size=100)
    assert logger.enabled is True
    await logger.start()
    # mkdir 失败 → enabled 应被降级
    assert logger.enabled is False

    # 降级后 log() 不抛 + 不写盘
    await logger.log("s2c", "ws.threads", {"i": 1})
    await logger.aclose()
    assert not log_path.exists()

    captured = capsys.readouterr()
    assert "disabling logger" in captured.err


# ---------------------------------------------------------------------------
# 9. 单例语义
# ---------------------------------------------------------------------------


async def test_init_get_singleton(tmp_path: Path) -> None:
    log_path = tmp_path / "full_log.jsonl"
    cfg = _CfgStub(enabled=True, path=str(log_path), queue_size=100)
    logger1 = init_full_logger(cfg)
    logger2 = get_full_logger()
    assert logger1 is logger2
    assert logger2.enabled is True
    assert logger2.path == log_path.resolve()


async def test_get_without_init_returns_dummy() -> None:
    """未 init 时 get 应返回 enabled=False 哑实例，不抛。"""
    dummy = get_full_logger()
    assert dummy.enabled is False
    # 哑实例的 log / start / aclose 都应是 no-op，不抛
    await dummy.start()
    await dummy.log("s2c", "ws.threads", {"i": 1})
    await dummy.aclose()


# ---------------------------------------------------------------------------
# 10. JSONL 行包含所有必需字段
# ---------------------------------------------------------------------------


async def test_module_dump_includes_required_fields(tmp_path: Path) -> None:
    log_path = tmp_path / "full_log.jsonl"
    logger = FullLogger(enabled=True, path=str(log_path), queue_size=100)
    await logger.start()
    await logger.log(
        "s2c",
        "ws.threads",
        {"frame_type": "turn.start"},
        thread_id="thread-abc",
        client_id="client-xyz",
    )
    await logger.aclose()

    records = _read_jsonl(log_path)
    assert len(records) == 1
    record = records[0]
    # 必须有 6 个 key（顺序无关）
    expected_keys = {"ts", "dir", "channel", "thread_id", "client_id", "payload"}
    assert set(record.keys()) == expected_keys, (
        f"JSONL 字段不符合 spec：期望 {expected_keys}，实际 {set(record.keys())}"
    )
    assert record["dir"] == "s2c"
    assert record["channel"] == "ws.threads"
    assert record["thread_id"] == "thread-abc"
    assert record["client_id"] == "client-xyz"
    assert record["payload"] == {"frame_type": "turn.start"}


async def test_thread_and_client_id_default_none(tmp_path: Path) -> None:
    """不传 thread_id / client_id 时字段为 None（不是缺失）。"""
    log_path = tmp_path / "full_log.jsonl"
    logger = FullLogger(enabled=True, path=str(log_path), queue_size=100)
    await logger.start()
    await logger.log("c2s", "ws.threads", {"frame_type": "user.input"})
    await logger.aclose()

    records = _read_jsonl(log_path)
    assert len(records) == 1
    assert records[0]["thread_id"] is None
    assert records[0]["client_id"] is None


async def test_start_idempotent(tmp_path: Path) -> None:
    """多次 start() 应幂等：同一个 writer task，不会泄漏。"""
    log_path = tmp_path / "full_log.jsonl"
    logger = FullLogger(enabled=True, path=str(log_path), queue_size=100)
    await logger.start()
    first_task = logger._writer_task
    await logger.start()
    second_task = logger._writer_task
    assert first_task is second_task
    await logger.aclose()


async def test_aclose_timeout_does_not_hang(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """P1 #1：writer task 死掉 + put sentinel 阻塞时，aclose 5s 超时强制返回。

    制造场景：cancel writer 让队列没消费者；再用 queue_size=1 + 提前塞满让
    put(sentinel) 永远阻塞（asyncio.Queue.put 在满时是 await）。aclose 必须
    在 5s 内（不耗 5s，等真的 wait_for 触发就 fail 慢测——我们 monkeypatch
    timeout 到极小值让测试快跑）。
    """
    # monkeypatch wait_for 的 timeout 到 0.05s，让测试不用真等 5s
    real_wait_for = asyncio.wait_for

    async def fast_wait_for(coro: Any, timeout: float) -> Any:
        return await real_wait_for(coro, timeout=0.05)

    monkeypatch.setattr("devtools.full_logger.asyncio.wait_for", fast_wait_for)

    log_path = tmp_path / "full_log.jsonl"
    logger = FullLogger(enabled=True, path=str(log_path), queue_size=1)
    await logger.start()

    # 杀掉 writer task，让队列没有消费者
    assert logger._writer_task is not None
    logger._writer_task.cancel()
    with contextlib_suppress():
        await logger._writer_task

    # 队列填满（1 个名额），put sentinel 时会 await 等空位 —— 但 writer 已死，永远不空
    assert logger._queue is not None
    await logger._queue.put({"sentinel-blocker": True})

    # aclose 应在 wait_for 触发时退出，不挂死
    start = asyncio.get_event_loop().time()
    await logger.aclose()
    elapsed = asyncio.get_event_loop().time() - start

    assert elapsed < 1.0, f"aclose 超时未生效，实际等了 {elapsed:.2f}s"
    captured = capsys.readouterr()
    assert "aclose timed out" in captured.err


async def test_write_one_early_returns_when_disabled(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """P1 #3：``_handle_write_failure`` 把 enabled=False 后，writer drain 残留时
    每条 _write_one 早返不再尝试写盘（不重复触发 OSError）。
    """
    log_path = tmp_path / "full_log.jsonl"
    logger = FullLogger(enabled=True, path=str(log_path), queue_size=100)
    await logger.start()

    # 入队一条 → 触发首次写盘 → mock _append_line_sync 抛 OSError 让 logger 降级
    fail_count = {"n": 0}

    def flaky_append(line: str) -> None:
        fail_count["n"] += 1
        raise OSError("disk full")

    monkeypatch.setattr(logger, "_append_line_sync", flaky_append)
    await logger.log("s2c", "ws.threads", {"frame_type": "turn.start"})

    # 等首次写盘失败 → 降级
    for _ in range(50):
        if not logger.enabled:
            break
        await asyncio.sleep(0.01)
    assert logger.enabled is False, "首次 OSError 应触发降级"

    # 此时再入队（log 应早返）；但已入队的 N 条会被 writer drain
    # 直接用 put_nowait 绕开 log() 早返，模拟"降级前已入队"的场景
    assert logger._queue is not None
    logger._queue.put_nowait({"frame_type": "turn.end"})
    logger._queue.put_nowait({"frame_type": "turn.start.2"})

    before = fail_count["n"]
    await logger.aclose()
    after = fail_count["n"]

    # _write_one 早返：drain 时不再触发 _append_line_sync（即不再 +1）
    assert after == before, (
        f"降级后 _write_one 应早返，但 _append_line_sync 又被调了 {after - before} 次"
    )


def contextlib_suppress() -> Any:
    """用 contextlib.suppress 兼容同步包装异步 cancel —— 测试辅助。"""
    import contextlib

    return contextlib.suppress(asyncio.CancelledError, Exception)
