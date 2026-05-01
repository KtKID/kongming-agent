"""main.py 单元 + 集成测试。

单元测试用 ``io.StringIO`` 注入 stdin/stdout 直接调 :func:`run_sidecar`；
集成测试用 ``subprocess`` 启 ``python -m claude_sidecar`` 验证：

- 第一行 stdout 是 ``claude_sidecar_ready``
- EOF 后退出码 0
- shutdown 命令后退出码 0
"""

from __future__ import annotations

import io
import json
import subprocess
import sys
from pathlib import Path

import pytest

from claude_sidecar.main import run_sidecar

_REPO_ROOT = Path(__file__).resolve().parents[3]
_SRC_DIR = _REPO_ROOT / "src"


# --- 单元测试（注入 stdin/stdout） --------------------------------------------


@pytest.mark.asyncio
async def test_first_event_is_ready_then_eof_exits_zero() -> None:
    stdin = io.StringIO("")  # 立即 EOF
    stdout = io.StringIO()
    code = await run_sidecar(stdin=stdin, stdout=stdout)
    assert code == 0
    lines = stdout.getvalue().strip().splitlines()
    assert len(lines) == 1
    first = json.loads(lines[0])
    assert first["type"] == "claude_sidecar_ready"
    assert first["protocolVersion"] == "1"


@pytest.mark.asyncio
async def test_shutdown_command_exits_zero() -> None:
    cmd = json.dumps({"protocolVersion": "1", "type": "shutdown", "threadId": "th_1"})
    stdin = io.StringIO(cmd + "\n")
    stdout = io.StringIO()
    code = await run_sidecar(stdin=stdin, stdout=stdout)
    assert code == 0
    lines = stdout.getvalue().strip().splitlines()
    # 至少 2 行：ready + shutdown ack
    assert len(lines) >= 2
    assert json.loads(lines[0])["type"] == "claude_sidecar_ready"
    last = json.loads(lines[-1])
    assert last["type"] == "claude_transport_error"
    assert last["threadId"] == "th_1"
    assert "shutdown" in last["message"].lower()


@pytest.mark.asyncio
async def test_unknown_command_returns_transport_error_keeps_running() -> None:
    cmds = [
        # 第一条故意非法
        '{"protocolVersion": "2", "type": "start"}',
        # 第二条合法 shutdown 让循环退出
        json.dumps({"protocolVersion": "1", "type": "shutdown", "threadId": "th_1"}),
    ]
    stdin = io.StringIO("\n".join(cmds) + "\n")
    stdout = io.StringIO()
    code = await run_sidecar(stdin=stdin, stdout=stdout)
    assert code == 0
    lines = [json.loads(line) for line in stdout.getvalue().strip().splitlines()]
    # ready / 第一条解析错误 transport_error / shutdown ack
    assert len(lines) >= 3
    assert lines[0]["type"] == "claude_sidecar_ready"
    # 第一条命令的 protocolVersion=2 → transport_error(recoverable=false)
    assert lines[1]["type"] == "claude_transport_error"
    assert lines[1]["recoverable"] is False
    assert "protocol_version" in lines[1]["message"] or "1" in lines[1]["message"]
    # 第一条解析失败时 threadId / runId 不写字段
    assert "threadId" not in lines[1]
    assert "runId" not in lines[1]


@pytest.mark.asyncio
async def test_start_command_fallback_when_translator_unavailable() -> None:
    """fallback 路径：translator_factory 抛异常时，start 命令回 not_implemented。

    P1 phase 接通 SDK Bridge 后，start 默认走真 SDK。但 main.py 设计了 fallback：
    如果 translator 不可用（例如 #3 task 还没合并、或工厂抛异常），
    start 命令应该回 ``claude_transport_error("...not implemented...")``。

    本测试验证这个 fallback 设计——保护"#3 缺席时 sidecar 仍可启动"的能力。
    """

    def failing_translator_factory(_writer: object) -> object:
        raise RuntimeError("translator unavailable for this test")

    start = json.dumps(
        {
            "protocolVersion": "1",
            "type": "start",
            "workspaceId": "ws_1",
            "threadId": "th_1",
            "runId": "run_1",
            "cwd": "/tmp",
            "prompt": "hi",
        }
    )
    shutdown = json.dumps({"protocolVersion": "1", "type": "shutdown", "threadId": "th_1"})
    stdin = io.StringIO(start + "\n" + shutdown + "\n")
    stdout = io.StringIO()
    code = await run_sidecar(
        stdin=stdin,
        stdout=stdout,
        translator_factory=failing_translator_factory,  # type: ignore[arg-type]
    )
    assert code == 0
    lines = [json.loads(line) for line in stdout.getvalue().strip().splitlines()]
    # ready / start 的 not_implemented fallback / shutdown ack
    not_impl = lines[1]
    assert not_impl["type"] == "claude_transport_error"
    assert "not implemented" in not_impl["message"].lower()
    assert not_impl["recoverable"] is True
    assert not_impl["threadId"] == "th_1"
    assert not_impl["runId"] == "run_1"


@pytest.mark.asyncio
async def test_resolve_approval_no_run_id() -> None:
    """resolve_approval 没有 runId 字段（关联是 requestId），不应该硬塞 runId。"""
    cmd = json.dumps(
        {
            "protocolVersion": "1",
            "type": "resolve_approval",
            "threadId": "th_1",
            "requestId": "req_1",
            "approved": True,
        }
    )
    shutdown = json.dumps({"protocolVersion": "1", "type": "shutdown", "threadId": "th_1"})
    stdin = io.StringIO(cmd + "\n" + shutdown + "\n")
    stdout = io.StringIO()
    await run_sidecar(stdin=stdin, stdout=stdout)
    lines = [json.loads(line) for line in stdout.getvalue().strip().splitlines()]
    not_impl = lines[1]
    assert not_impl["type"] == "claude_transport_error"
    assert not_impl["threadId"] == "th_1"
    assert "runId" not in not_impl  # resolve_approval 没 runId


@pytest.mark.asyncio
async def test_empty_lines_tolerated() -> None:
    """空行不报错也不响应。"""
    shutdown = json.dumps({"protocolVersion": "1", "type": "shutdown", "threadId": "th_1"})
    stdin = io.StringIO("\n\n   \n" + shutdown + "\n")
    stdout = io.StringIO()
    code = await run_sidecar(stdin=stdin, stdout=stdout)
    assert code == 0
    lines = stdout.getvalue().strip().splitlines()
    # 空行不产生输出，所以只有 ready + shutdown ack
    assert len(lines) == 2


@pytest.mark.asyncio
async def test_invalid_json_returns_transport_error() -> None:
    stdin = io.StringIO(
        "{this is not json\n"
        + json.dumps({"protocolVersion": "1", "type": "shutdown", "threadId": "th_1"})
        + "\n"
    )
    stdout = io.StringIO()
    await run_sidecar(stdin=stdin, stdout=stdout)
    lines = [json.loads(line) for line in stdout.getvalue().strip().splitlines()]
    assert lines[1]["type"] == "claude_transport_error"
    assert lines[1]["recoverable"] is False
    assert "json_decode" in lines[1]["message"]


# --- 集成测试（真 subprocess 启 sidecar） ---------------------------------------


def _spawn_sidecar() -> subprocess.Popen[str]:
    """启 ``python -m claude_sidecar`` 子进程，注入 PYTHONPATH。"""
    env = {
        "PYTHONPATH": str(_SRC_DIR),
        "PATH": "/usr/bin:/bin:/usr/local/bin:/opt/homebrew/bin",
    }
    return subprocess.Popen(
        [sys.executable, "-m", "claude_sidecar"],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        env=env,
        text=True,
        bufsize=1,  # 行缓冲，确保第一行能立即被读到
    )


def test_subprocess_first_line_is_ready() -> None:
    """关键集成测试：真 subprocess 启 sidecar，第一行 stdout 是 claude_sidecar_ready。

    这条测试守护契约 §7.2 + 03 §1 启动流程的核心约定。
    """
    with _spawn_sidecar() as proc:
        assert proc.stdout is not None
        assert proc.stdin is not None
        try:
            first_line = proc.stdout.readline()
            assert first_line, "sidecar should emit at least one line on startup"
            first = json.loads(first_line.strip())
            assert first["type"] == "claude_sidecar_ready"
            assert first["protocolVersion"] == "1"
            assert "ts" in first
        finally:
            # 关 stdin 触发 sidecar 走 EOF→shutdown 路径
            proc.stdin.close()
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                proc.kill()
                raise


def test_subprocess_eof_exits_zero() -> None:
    """关 stdin 后 sidecar 应该退出码 0。"""
    with _spawn_sidecar() as proc:
        assert proc.stdin is not None
        proc.stdin.close()
        rc = proc.wait(timeout=5)
        assert rc == 0


def test_subprocess_shutdown_command_exits_zero() -> None:
    """显式 shutdown 命令后退出码 0。"""
    with _spawn_sidecar() as proc:
        assert proc.stdin is not None
        assert proc.stdout is not None
        try:
            # 先吃掉 ready
            ready_line = proc.stdout.readline()
            assert json.loads(ready_line.strip())["type"] == "claude_sidecar_ready"
            # 发 shutdown
            cmd = json.dumps({"protocolVersion": "1", "type": "shutdown", "threadId": "th_1"})
            proc.stdin.write(cmd + "\n")
            proc.stdin.flush()
            # 读 shutdown ack
            ack_line = proc.stdout.readline()
            ack = json.loads(ack_line.strip())
            assert ack["type"] == "claude_transport_error"
            assert ack["threadId"] == "th_1"
            rc = proc.wait(timeout=5)
            assert rc == 0
        finally:
            if proc.poll() is None:
                proc.kill()
