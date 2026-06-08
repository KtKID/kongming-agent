"""CodexService 主路径单测（v0.1 最小覆盖）。

mock ``asyncio.create_subprocess_exec`` 验证：

- spawn args + stdin 装配（permission_mode 三档 / resume / model / 中文 cwd）
- 主路径 query：thread.started → text → complete 序列
- abort：SIGTERM → 2s 超时 → SIGKILL
- 错误处理：FileNotFoundError / 认证失败 / exit_code != 0 / jsonl parse failed

完整 25 边界用例在 T4。
"""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from web.integrations.codex.service import CodexService
from web.shared.session_manager import SessionManager

# ---------------------------------------------------------------------------
# Fixtures + helpers
# ---------------------------------------------------------------------------


class _MockStdout:
    """模拟 ``asyncio.subprocess.PIPE`` 的异步行迭代。"""

    def __init__(self, lines: list[bytes]) -> None:
        self._lines = lines

    def __aiter__(self) -> Any:
        return self._iter()

    async def _iter(self) -> Any:
        for line in self._lines:
            yield line


class _MockStderr:
    """空 stderr，配合 ``async for`` 迭代。"""

    def __init__(self, lines: list[bytes] | None = None) -> None:
        self._lines = lines or []

    def __aiter__(self) -> Any:
        return self._iter()

    async def _iter(self) -> Any:
        for line in self._lines:
            yield line


class _MockStdin:
    """模拟 subprocess stdin：记录写入内容，并支持 drain/close/wait_closed。"""

    def __init__(self) -> None:
        self.buffer = bytearray()
        self.closed = False

    def write(self, data: bytes) -> None:
        self.buffer.extend(data)

    async def drain(self) -> None:
        return None

    def close(self) -> None:
        self.closed = True

    async def wait_closed(self) -> None:
        return None


def _make_mock_proc(
    stdout_lines: list[bytes],
    stderr_lines: list[bytes] | None = None,
    exit_code: int = 0,
) -> MagicMock:
    """构造 mock subprocess：stdout 异步迭代 + wait/terminate/kill。"""
    proc = MagicMock()
    proc.stdin = _MockStdin()
    proc.stdout = _MockStdout(stdout_lines)
    proc.stderr = _MockStderr(stderr_lines)
    proc.wait = AsyncMock(return_value=exit_code)
    proc.returncode = exit_code
    proc.terminate = MagicMock()
    proc.kill = MagicMock()
    return proc


@pytest.fixture
def session_manager() -> SessionManager:
    return SessionManager()


@pytest.fixture
def codex_service(session_manager: SessionManager) -> CodexService:
    return CodexService(session_manager)


class _FakeWriter:
    def __init__(self) -> None:
        self.sent: list[dict[str, Any]] = []

    async def send_json(self, msg: dict[str, Any]) -> None:
        self.sent.append(msg)


@pytest.fixture
def writer() -> _FakeWriter:
    return _FakeWriter()


# ---------------------------------------------------------------------------
# 1. spawn args 构造
# ---------------------------------------------------------------------------


class TestSpawnArgs:
    """验证 ``_build_invocation`` 构造的 codex 启动参数与 stdin。"""

    def test_default_mode(self, codex_service: CodexService) -> None:
        invocation = codex_service._build_invocation(
            session_id="pending-1",
            command="hi",
            cwd="/tmp",
            permission_mode="default",
            model=None,
            resume=False,
        )
        assert invocation.argv == [
            "codex",
            "exec",
            "--json",
            "--skip-git-repo-check",
            "--cd",
            "/tmp",
            "--sandbox",
            "workspace-write",
            "--config",
            'approval_policy="untrusted"',
        ]
        assert invocation.stdin_text == "hi"

    def test_accept_edits_mode(self, codex_service: CodexService) -> None:
        invocation = codex_service._build_invocation(
            session_id="pending-1",
            command="hi",
            cwd="/tmp",
            permission_mode="acceptEdits",
            model=None,
            resume=False,
        )
        assert "--sandbox" in invocation.argv
        assert invocation.argv[invocation.argv.index("--sandbox") + 1] == "workspace-write"
        assert "--config" in invocation.argv
        assert 'approval_policy="never"' in invocation.argv

    def test_bypass_permissions_mode(self, codex_service: CodexService) -> None:
        invocation = codex_service._build_invocation(
            session_id="pending-1",
            command="hi",
            cwd="/tmp",
            permission_mode="bypassPermissions",
            model=None,
            resume=False,
        )
        assert invocation.argv[invocation.argv.index("--sandbox") + 1] == "danger-full-access"
        assert 'approval_policy="never"' in invocation.argv

    def test_resume_inserts_session_id_after_exec(
        self,
        codex_service: CodexService,
    ) -> None:
        invocation = codex_service._build_invocation(
            session_id="abc",
            command="hi",
            cwd="/tmp",
            permission_mode="default",
            model=None,
            resume=True,
        )
        assert invocation.argv[-2:] == ["resume", "abc"]

    def test_model_flag_appended(self, codex_service: CodexService) -> None:
        invocation = codex_service._build_invocation(
            session_id="pending-1",
            command="hi",
            cwd="/tmp",
            permission_mode="default",
            model="o3",
            resume=False,
        )
        assert "--model" in invocation.argv
        assert invocation.argv[invocation.argv.index("--model") + 1] == "o3"
        assert invocation.stdin_text == "hi"

    def test_command_is_sent_via_stdin(
        self,
        codex_service: CodexService,
    ) -> None:
        invocation = codex_service._build_invocation(
            session_id="pending-1",
            command="--ask-for-approval",
            cwd="/tmp",
            permission_mode="default",
            model="o3",
            resume=False,
        )
        assert invocation.stdin_text == "--ask-for-approval"
        assert "--ask-for-approval" not in invocation.argv

    def test_chinese_cwd_passes_through(self, codex_service: CodexService) -> None:
        invocation = codex_service._build_invocation(
            session_id="pending-1",
            command="hi",
            cwd="/tmp/中文目录",
            permission_mode="default",
            model=None,
            resume=False,
        )
        # subprocess.exec 自动处理 quote，参数原样
        assert invocation.argv[invocation.argv.index("--cd") + 1] == "/tmp/中文目录"

    def test_resume_keeps_image_flags_in_argv(self, codex_service: CodexService) -> None:
        invocation = codex_service._build_invocation(
            session_id="abc",
            command="hi",
            cwd="/tmp",
            permission_mode="default",
            model=None,
            resume=True,
            image_args=["--image", "/tmp/a.png"],
        )
        assert invocation.argv[-4:] == ["resume", "abc", "--image", "/tmp/a.png"]
        assert invocation.stdin_text == "hi"

    def test_windows_prefers_cmd_wrapper(self, codex_service: CodexService) -> None:
        with (
            patch("web.integrations.codex.service.sys.platform", "win32"),
            patch(
                "web.integrations.codex.service.shutil.which",
                side_effect=lambda name: (
                    r"C:\Users\Administrator\AppData\Roaming\npm\codex.cmd"
                    if name == "codex.cmd"
                    else None
                ),
            ),
        ):
            invocation = codex_service._build_invocation(
                session_id="pending-1",
                command="hi",
                cwd="/tmp",
                permission_mode="default",
                model=None,
                resume=False,
            )
        assert invocation.argv[0].endswith("codex.cmd")

    def test_windows_falls_back_to_exe(self, codex_service: CodexService) -> None:
        with (
            patch("web.integrations.codex.service.sys.platform", "win32"),
            patch(
                "web.integrations.codex.service.shutil.which",
                side_effect=lambda name: (
                    r"C:\Program Files\WindowsApps\OpenAI.Codex\app\resources\codex.exe"
                    if name == "codex.exe"
                    else None
                ),
            ),
        ):
            invocation = codex_service._build_invocation(
                session_id="pending-1",
                command="hi",
                cwd="/tmp",
                permission_mode="default",
                model=None,
                resume=False,
            )
        assert invocation.argv[0].endswith("codex.exe")

    def test_windows_unresolved_keeps_plain_codex(self, codex_service: CodexService) -> None:
        with (
            patch("web.integrations.codex.service.sys.platform", "win32"),
            patch("web.integrations.codex.service.shutil.which", return_value=None),
        ):
            invocation = codex_service._build_invocation(
                session_id="pending-1",
                command="hi",
                cwd="/tmp",
                permission_mode="default",
                model=None,
                resume=False,
            )
        assert invocation.argv[0] == "codex"


class TestQueryHappyPath:
    """主路径：spawn → 4 行 jsonl → writer 收到 session_created/text/complete。"""

    async def test_full_flow(
        self,
        codex_service: CodexService,
        writer: _FakeWriter,
    ) -> None:
        stdout_lines = [
            b'{"type":"thread.started","thread_id":"019dee"}\n',
            b'{"type":"turn.started"}\n',
            b'{"type":"item.completed","item":{"id":"i0","type":"agent_message","text":"hello"}}\n',
            b'{"type":"turn.completed","usage":{"input_tokens":10,"cached_input_tokens":5,"output_tokens":3,"reasoning_output_tokens":1}}\n',
        ]
        proc = _make_mock_proc(stdout_lines, exit_code=0)

        with patch(
            "web.integrations.codex.service.asyncio.create_subprocess_exec",
            new=AsyncMock(return_value=proc),
        ):
            await codex_service.query(
                session_id="pending-1",
                command="hi",
                cwd="/tmp",
                writer=writer,
            )

        kinds = [m.get("frame_type") for m in writer.sent]
        # 至少包含 session_created / text / complete
        assert "session_created" in kinds
        assert "text" in kinds
        assert "complete" in kinds
        # 最后一条是 complete（turn.completed normalize 出来的，service 不补发）
        assert kinds[-1] == "complete"
        # session_created 携带 newSessionId == thread_id
        sc = next(m for m in writer.sent if m["frame_type"] == "session_created")
        assert sc.get("newSessionId") == "019dee"
        # complete 含 tokenBudget
        complete = next(m for m in writer.sent if m["frame_type"] == "complete")
        assert "tokenBudget" in complete
        assert proc.stdin.buffer == b"hi"
        assert proc.stdin.closed is True

    async def test_thread_started_renames_session(
        self,
        codex_service: CodexService,
        session_manager: SessionManager,
        writer: _FakeWriter,
    ) -> None:
        """thread.started 后 SessionManager 中的 session_id 被替换。"""
        stdout_lines = [
            b'{"type":"thread.started","thread_id":"real-019dee"}\n',
            b'{"type":"turn.completed","usage":{}}\n',
        ]
        proc = _make_mock_proc(stdout_lines, exit_code=0)

        with patch(
            "web.integrations.codex.service.asyncio.create_subprocess_exec",
            new=AsyncMock(return_value=proc),
        ):
            await codex_service.query(
                session_id="pending-1",
                command="hi",
                cwd="/tmp",
                writer=writer,
            )

        # query 完成后两个 id 都已 unregister
        assert session_manager.is_active("pending-1") is False
        assert session_manager.is_active("real-019dee") is False


# ---------------------------------------------------------------------------
# 3. abort 路径
# ---------------------------------------------------------------------------


class TestAbort:
    async def test_abort_sigterm_then_sigkill_on_timeout(
        self,
        codex_service: CodexService,
        session_manager: SessionManager,
    ) -> None:
        """terminate 后 wait_for 超时 → 调 kill。"""
        proc = MagicMock()
        proc.returncode = None
        proc.terminate = MagicMock()
        proc.kill = MagicMock()
        # proc.wait 返回 0；fake_wait_for 第一次抛 TimeoutError，第二次正常 await
        proc.wait = AsyncMock(return_value=0)

        codex_service._processes["sid-1"] = proc
        await session_manager.register("sid-1", _FakeWriter())

        call_count = 0

        async def _fake_wait_for(coro: Any, *args: Any, **kwargs: Any) -> Any:
            nonlocal call_count
            call_count += 1
            # 第一次（SIGTERM 后等 2s）抛 TimeoutError
            # 第二次（kill 后的 await proc.wait()，不经 wait_for）不会进这里
            # 关掉 coro 避免 "never awaited" 警告
            coro.close()
            raise TimeoutError

        with patch("web.integrations.codex.service.asyncio.wait_for", new=_fake_wait_for):
            ok = await codex_service.abort("sid-1")

        assert ok is True
        assert call_count == 1
        proc.terminate.assert_called_once()
        proc.kill.assert_called_once()

    async def test_abort_sigterm_succeeds_within_timeout(
        self,
        codex_service: CodexService,
        session_manager: SessionManager,
    ) -> None:
        """SIGTERM 后子进程及时退出 → 不调 kill。"""
        proc = MagicMock()
        proc.returncode = None
        proc.terminate = MagicMock()
        proc.kill = MagicMock()
        proc.wait = AsyncMock(return_value=0)

        codex_service._processes["sid-2"] = proc
        await session_manager.register("sid-2", _FakeWriter())

        async def _fake_wait_for(coro: Any, *args: Any, **kwargs: Any) -> Any:
            return await coro

        with patch("web.integrations.codex.service.asyncio.wait_for", new=_fake_wait_for):
            ok = await codex_service.abort("sid-2")

        assert ok is True
        proc.terminate.assert_called_once()
        proc.kill.assert_not_called()

    async def test_abort_unknown_session_returns_false(
        self,
        codex_service: CodexService,
    ) -> None:
        assert await codex_service.abort("nope") is False


# ---------------------------------------------------------------------------
# 4. 错误处理
# ---------------------------------------------------------------------------


class TestErrorHandling:
    async def test_codex_not_installed_emits_error(
        self,
        codex_service: CodexService,
        writer: _FakeWriter,
    ) -> None:
        with patch(
            "web.integrations.codex.service.asyncio.create_subprocess_exec",
            new=AsyncMock(side_effect=FileNotFoundError("codex")),
        ):
            await codex_service.query(
                session_id="pending-1",
                command="hi",
                cwd="/tmp",
                writer=writer,
            )

        errors = [m for m in writer.sent if m.get("frame_type") == "error"]
        assert len(errors) >= 1
        assert "codex CLI not installed" in errors[0]["error"]

    async def test_auth_error_translates_to_login_hint(
        self,
        codex_service: CodexService,
        writer: _FakeWriter,
    ) -> None:
        """stderr 含 'Not authenticated' → emit codex login 引导。"""
        stdout_lines: list[bytes] = []
        stderr_lines = [b"Error: Not authenticated. Please run codex login.\n"]
        proc = _make_mock_proc(stdout_lines, stderr_lines, exit_code=1)

        with patch(
            "web.integrations.codex.service.asyncio.create_subprocess_exec",
            new=AsyncMock(return_value=proc),
        ):
            await codex_service.query(
                session_id="pending-1",
                command="hi",
                cwd="/tmp",
                writer=writer,
            )

        errors = [m for m in writer.sent if m.get("frame_type") == "error"]
        assert len(errors) == 1
        assert "codex login" in errors[0]["error"]

    async def test_nonzero_exit_without_complete_emits_error_with_stderr(
        self,
        codex_service: CodexService,
        writer: _FakeWriter,
    ) -> None:
        """子进程 exit_code != 0 且没收到 turn.completed → emit error 含 stderr 末尾。"""
        stdout_lines: list[bytes] = []
        stderr_lines = [b"some random failure happened\n"]
        proc = _make_mock_proc(stdout_lines, stderr_lines, exit_code=42)

        with patch(
            "web.integrations.codex.service.asyncio.create_subprocess_exec",
            new=AsyncMock(return_value=proc),
        ):
            await codex_service.query(
                session_id="pending-1",
                command="hi",
                cwd="/tmp",
                writer=writer,
            )

        errors = [m for m in writer.sent if m.get("frame_type") == "error"]
        assert len(errors) == 1
        assert "exited with code 42" in errors[0]["error"]
        assert "some random failure" in errors[0]["error"]

    async def test_jsonl_parse_failure_emits_error_continues_loop(
        self,
        codex_service: CodexService,
        writer: _FakeWriter,
    ) -> None:
        """jsonl 单行 parse 失败 → emit error 但主循环继续读下一行。"""
        stdout_lines = [
            b"not-a-json-line\n",
            b'{"type":"thread.started","thread_id":"019dee"}\n',
            b'{"type":"turn.completed","usage":{}}\n',
        ]
        proc = _make_mock_proc(stdout_lines, exit_code=0)

        with patch(
            "web.integrations.codex.service.asyncio.create_subprocess_exec",
            new=AsyncMock(return_value=proc),
        ):
            await codex_service.query(
                session_id="pending-1",
                command="hi",
                cwd="/tmp",
                writer=writer,
            )

        kinds = [m.get("frame_type") for m in writer.sent]
        # parse 失败 emit 一条 error 但后续 thread.started / complete 仍然正常处理
        assert "error" in kinds
        assert "session_created" in kinds
        assert "complete" in kinds
        # 验证错误消息含 jsonl parse 提示
        errors = [m for m in writer.sent if m.get("frame_type") == "error"]
        assert any("jsonl parse failed" in e["error"] for e in errors)


# ---------------------------------------------------------------------------
# 5. 重复 session 保护（v0.1 service 未实现保护，标 skip）
# ---------------------------------------------------------------------------


class TestDuplicateSession:
    @pytest.mark.skip(reason="not implemented in v0.1 — SessionManager.register 直接覆盖")
    async def test_duplicate_query_rejected(self) -> None:
        """v0.2+ 若加保护再启用。"""
