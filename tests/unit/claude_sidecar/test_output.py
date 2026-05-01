"""output.py 单元测试。

覆盖：
- emit 自动注入 protocolVersion + ts
- emit_ready 输出格式
- emit_transport_error 在 thread_id / run_id 缺省时不写字段（不是写 null）
- 并发多协程 emit 不交错（每行严格一个 JSON 对象）
"""

from __future__ import annotations

import asyncio
import io
import json

import pytest

from claude_sidecar.output import OutputWriter


@pytest.mark.asyncio
async def test_emit_auto_injects_protocol_version_and_ts() -> None:
    buf = io.StringIO()
    writer = OutputWriter(buf)
    await writer.emit({"type": "claude_assistant_delta", "delta": "hi"})
    line = buf.getvalue().strip()
    payload = json.loads(line)
    assert payload["protocolVersion"] == "1"
    assert payload["type"] == "claude_assistant_delta"
    assert payload["delta"] == "hi"
    # ts 自动填了 ISO8601 UTC（含时区后缀 +00:00 或 Z）
    assert "ts" in payload
    assert "T" in payload["ts"]
    assert payload["ts"].endswith("+00:00") or payload["ts"].endswith("Z")


@pytest.mark.asyncio
async def test_emit_caller_provided_ts_preserved() -> None:
    buf = io.StringIO()
    writer = OutputWriter(buf)
    custom_ts = "2026-01-01T00:00:00+00:00"
    await writer.emit({"type": "claude_run_finished", "ts": custom_ts})
    payload = json.loads(buf.getvalue().strip())
    assert payload["ts"] == custom_ts


@pytest.mark.asyncio
async def test_emit_protocol_version_is_overwritten() -> None:
    """即使调用方传了别的 protocolVersion，OutputWriter 强制覆盖为 '1'。

    这是为了防止 v2 协议尚未就位时上游误传 — sidecar 这一阶段必须输出 '1'。
    """
    buf = io.StringIO()
    writer = OutputWriter(buf)
    await writer.emit({"type": "claude_assistant_final", "protocolVersion": "999"})
    payload = json.loads(buf.getvalue().strip())
    assert payload["protocolVersion"] == "1"


@pytest.mark.asyncio
async def test_emit_ready() -> None:
    buf = io.StringIO()
    writer = OutputWriter(buf)
    await writer.emit_ready()
    payload = json.loads(buf.getvalue().strip())
    assert payload == {
        "protocolVersion": "1",
        "type": "claude_sidecar_ready",
        "ts": payload["ts"],  # 自动填的，只验证存在
    }
    # ready 事件不带 threadId / runId
    assert "threadId" not in payload
    assert "runId" not in payload


@pytest.mark.asyncio
async def test_emit_transport_error_omits_optional_fields_when_absent() -> None:
    """run 未建立时 threadId / runId 缺省，应该不写字段（不是写 null）。"""
    buf = io.StringIO()
    writer = OutputWriter(buf)
    await writer.emit_transport_error("startup failed", recoverable=False)
    payload = json.loads(buf.getvalue().strip())
    assert payload["type"] == "claude_transport_error"
    assert payload["message"] == "startup failed"
    assert payload["recoverable"] is False
    assert "threadId" not in payload
    assert "runId" not in payload


@pytest.mark.asyncio
async def test_emit_transport_error_with_thread_and_run() -> None:
    buf = io.StringIO()
    writer = OutputWriter(buf)
    await writer.emit_transport_error(
        "broken",
        recoverable=True,
        thread_id="th_1",
        run_id="run_1",
    )
    payload = json.loads(buf.getvalue().strip())
    assert payload["threadId"] == "th_1"
    assert payload["runId"] == "run_1"
    assert payload["recoverable"] is True


@pytest.mark.asyncio
async def test_emit_transport_error_only_thread_id() -> None:
    """sidecar 启动期错误：threadId 已知（命令解析能拿到）但 runId 还没（首次 start 失败）。"""
    buf = io.StringIO()
    writer = OutputWriter(buf)
    await writer.emit_transport_error(
        "start payload missing cwd",
        recoverable=False,
        thread_id="th_1",
    )
    payload = json.loads(buf.getvalue().strip())
    assert payload["threadId"] == "th_1"
    assert "runId" not in payload


@pytest.mark.asyncio
async def test_emit_unicode_chinese() -> None:
    """ensure_ascii=False — 中文 message 不被转义。"""
    buf = io.StringIO()
    writer = OutputWriter(buf)
    await writer.emit({"type": "claude_run_failed", "message": "请求超时"})
    line = buf.getvalue()
    assert "请求超时" in line  # 不是 请求超时
    payload = json.loads(line.strip())
    assert payload["message"] == "请求超时"


@pytest.mark.asyncio
async def test_concurrent_emit_no_interleave() -> None:
    """多协程并发 emit，每行必须严格一个完整 JSON。

    没 lock 保护的话写入可能交错（比如 ``{"a":1}\n{"b"...{"c":...``）。
    用 lock 后每行都能 json.loads 成功。
    """
    buf = io.StringIO()
    writer = OutputWriter(buf)
    n = 50

    async def worker(i: int) -> None:
        await writer.emit({"type": "claude_assistant_delta", "delta": f"msg_{i}"})

    await asyncio.gather(*(worker(i) for i in range(n)))

    lines = buf.getvalue().strip().splitlines()
    assert len(lines) == n
    seen = set()
    for line in lines:
        payload = json.loads(line)  # 不会抛 → 说明没交错
        assert payload["protocolVersion"] == "1"
        assert payload["type"] == "claude_assistant_delta"
        seen.add(payload["delta"])
    assert seen == {f"msg_{i}" for i in range(n)}


@pytest.mark.asyncio
async def test_each_emit_one_newline() -> None:
    """每次 emit 严格一行（一个 \\n），不多不少。"""
    buf = io.StringIO()
    writer = OutputWriter(buf)
    await writer.emit({"type": "a"})
    await writer.emit({"type": "b"})
    out = buf.getvalue()
    assert out.count("\n") == 2
    lines = out.split("\n")
    assert lines[-1] == ""  # 最后一个 \n 后面是空（不是又一行）
    json.loads(lines[0])
    json.loads(lines[1])
