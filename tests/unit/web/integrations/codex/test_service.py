from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from hosts.web.integrations.codex.service import CodexService
from hosts.web.shared.session_manager import SessionManager


class _MockStdout:
    def __init__(self, lines: list[bytes]) -> None:
        self._lines = lines

    def __aiter__(self) -> Any:
        return self._iter()

    async def _iter(self) -> Any:
        for line in self._lines:
            yield line


class _MockStderr:
    def __init__(self, lines: list[bytes] | None = None) -> None:
        self._lines = lines or []

    def __aiter__(self) -> Any:
        return self._iter()

    async def _iter(self) -> Any:
        for line in self._lines:
            yield line


class _MockStdin:
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


class TestSpawnArgs:
    def test_default_mode(self, codex_service: CodexService) -> None:
        invocation = codex_service._build_invocation(
            session_id="pending-1",
            command="hi",
            cwd="/tmp",
            permission_mode="default",
            model=None,
            resume=False,
        )
        assert invocation.argv[1:] == [
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
        assert invocation.argv[invocation.argv.index("--sandbox") + 1] == "workspace-write"
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

    def test_resume_inserts_session_id_after_exec(self, codex_service: CodexService) -> None:
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

    def test_command_is_sent_via_stdin(self, codex_service: CodexService) -> None:
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
            patch("hosts.web.integrations.codex.service.sys.platform", "win32"),
            patch(
                "hosts.web.integrations.codex.service.shutil.which",
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
            patch("hosts.web.integrations.codex.service.sys.platform", "win32"),
            patch(
                "hosts.web.integrations.codex.service.shutil.which",
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
            patch("hosts.web.integrations.codex.service.sys.platform", "win32"),
            patch("hosts.web.integrations.codex.service.shutil.which", return_value=None),
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
            "hosts.web.integrations.codex.service.asyncio.create_subprocess_exec",
            new=AsyncMock(return_value=proc),
        ):
            await codex_service.query(
                session_id="pending-1",
                command="hi",
                cwd="/tmp",
                writer=writer,
            )

        kinds = [message.get("frame_type") for message in writer.sent]
        assert "session_created" in kinds
        assert "text" in kinds
        assert "complete" in kinds
        assert kinds[-1] == "complete"
        session_created = next(
            message for message in writer.sent if message["frame_type"] == "session_created"
        )
        assert session_created.get("newSessionId") == "019dee"
        complete = next(message for message in writer.sent if message["frame_type"] == "complete")
        assert "tokenBudget" in complete
        assert proc.stdin.buffer == b"hi"
        assert proc.stdin.closed is True

    async def test_thread_started_renames_session(
        self,
        codex_service: CodexService,
        session_manager: SessionManager,
        writer: _FakeWriter,
    ) -> None:
        stdout_lines = [
            b'{"type":"thread.started","thread_id":"real-019dee"}\n',
            b'{"type":"turn.completed","usage":{}}\n',
        ]
        proc = _make_mock_proc(stdout_lines, exit_code=0)

        with patch(
            "hosts.web.integrations.codex.service.asyncio.create_subprocess_exec",
            new=AsyncMock(return_value=proc),
        ):
            await codex_service.query(
                session_id="pending-1",
                command="hi",
                cwd="/tmp",
                writer=writer,
            )

        assert session_manager.is_active("pending-1") is False
        assert session_manager.is_active("real-019dee") is False


class TestAbort:
    async def test_abort_sigterm_then_sigkill_on_timeout(
        self,
        codex_service: CodexService,
        session_manager: SessionManager,
    ) -> None:
        proc = MagicMock()
        proc.returncode = None
        proc.terminate = MagicMock()
        proc.kill = MagicMock()
        proc.wait = AsyncMock(return_value=0)

        codex_service._processes["sid-1"] = proc
        await session_manager.register("sid-1", _FakeWriter())

        call_count = 0

        async def _fake_wait_for(coro: Any, *args: Any, **kwargs: Any) -> Any:
            nonlocal call_count
            call_count += 1
            coro.close()
            raise TimeoutError

        with patch("hosts.web.integrations.codex.service.asyncio.wait_for", new=_fake_wait_for):
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
        proc = MagicMock()
        proc.returncode = None
        proc.terminate = MagicMock()
        proc.kill = MagicMock()
        proc.wait = AsyncMock(return_value=0)

        codex_service._processes["sid-2"] = proc
        await session_manager.register("sid-2", _FakeWriter())

        async def _fake_wait_for(coro: Any, *args: Any, **kwargs: Any) -> Any:
            return await coro

        with patch("hosts.web.integrations.codex.service.asyncio.wait_for", new=_fake_wait_for):
            ok = await codex_service.abort("sid-2")

        assert ok is True
        proc.terminate.assert_called_once()
        proc.kill.assert_not_called()

    async def test_abort_unknown_session_returns_false(self, codex_service: CodexService) -> None:
        assert await codex_service.abort("nope") is False


class TestErrorHandling:
    async def test_codex_not_installed_emits_error(
        self,
        codex_service: CodexService,
        writer: _FakeWriter,
    ) -> None:
        with patch(
            "hosts.web.integrations.codex.service.asyncio.create_subprocess_exec",
            new=AsyncMock(side_effect=FileNotFoundError("codex")),
        ):
            await codex_service.query(
                session_id="pending-1",
                command="hi",
                cwd="/tmp",
                writer=writer,
            )

        errors = [message for message in writer.sent if message.get("frame_type") == "error"]
        assert len(errors) >= 1
        assert "codex CLI not installed" in errors[0]["error"]

    async def test_auth_error_translates_to_login_hint(
        self,
        codex_service: CodexService,
        writer: _FakeWriter,
    ) -> None:
        proc = _make_mock_proc([], [b"Error: Not authenticated. Please run codex login.\n"], 1)

        with patch(
            "hosts.web.integrations.codex.service.asyncio.create_subprocess_exec",
            new=AsyncMock(return_value=proc),
        ):
            await codex_service.query(
                session_id="pending-1",
                command="hi",
                cwd="/tmp",
                writer=writer,
            )

        errors = [message for message in writer.sent if message.get("frame_type") == "error"]
        assert len(errors) == 1
        assert "codex login" in errors[0]["error"]

    async def test_nonzero_exit_without_complete_emits_error_with_stderr(
        self,
        codex_service: CodexService,
        writer: _FakeWriter,
    ) -> None:
        proc = _make_mock_proc([], [b"some random failure happened\n"], 42)

        with patch(
            "hosts.web.integrations.codex.service.asyncio.create_subprocess_exec",
            new=AsyncMock(return_value=proc),
        ):
            await codex_service.query(
                session_id="pending-1",
                command="hi",
                cwd="/tmp",
                writer=writer,
            )

        errors = [message for message in writer.sent if message.get("frame_type") == "error"]
        assert len(errors) == 1
        assert "exited with code 42" in errors[0]["error"]
        assert "some random failure" in errors[0]["error"]

    async def test_jsonl_parse_failure_emits_error_continues_loop(
        self,
        codex_service: CodexService,
        writer: _FakeWriter,
    ) -> None:
        stdout_lines = [
            b"not-a-json-line\n",
            b'{"type":"thread.started","thread_id":"019dee"}\n',
            b'{"type":"turn.completed","usage":{}}\n',
        ]
        proc = _make_mock_proc(stdout_lines, exit_code=0)

        with patch(
            "hosts.web.integrations.codex.service.asyncio.create_subprocess_exec",
            new=AsyncMock(return_value=proc),
        ):
            await codex_service.query(
                session_id="pending-1",
                command="hi",
                cwd="/tmp",
                writer=writer,
            )

        kinds = [message.get("frame_type") for message in writer.sent]
        assert "error" in kinds
        assert "session_created" in kinds
        assert "complete" in kinds
        errors = [message for message in writer.sent if message.get("frame_type") == "error"]
        assert any("jsonl parse failed" in message["error"] for message in errors)


class TestDuplicateSession:
    @pytest.mark.skip(reason="not implemented in v0.1 - SessionManager.register overwrites")
    async def test_duplicate_query_rejected(self) -> None:
        pass
