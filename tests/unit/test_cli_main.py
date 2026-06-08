from __future__ import annotations

from pathlib import Path

from click.testing import CliRunner

import cli.main as cli_main
from infrastructure.config.models import (
    ApprovalConfig,
    Config,
    ModelConfig,
    RunnerConfig,
    SchedulerConfig,
    SessionConfig,
    TraceConfig,
)


class _FakeUUID:
    hex = "1234567890abcdef1234567890abcdef"


class _DummyRuntime:
    """最小 runtime stub：满足 CLI `_run` try/finally 调 `await runtime.aclose()` 的合约。"""

    async def aclose(self) -> None:
        return None


def test_generate_cli_session_id_uses_12_hex_chars(monkeypatch) -> None:
    monkeypatch.setattr(cli_main.uuid, "uuid4", lambda: _FakeUUID())

    session_id = cli_main._generate_cli_session_id()

    assert session_id == "cli-1234567890ab"


def test_cli_help_mentions_hex12() -> None:
    runner = CliRunner()

    result = runner.invoke(cli_main.main, ["--help"])

    assert result.exit_code == 0
    assert "cli-<hex12>" in result.output


def test_cli_help_shows_session_listing_flags() -> None:
    runner = CliRunner()
    result = runner.invoke(cli_main.main, ["--help"])
    assert result.exit_code == 0
    assert "--list-sessions" in result.output
    assert "--resume-last" in result.output


def _build_cfg() -> Config:
    return Config(
        model=ModelConfig(
            provider="openai_compatible",
            name="stub-model",
            base_url="http://127.0.0.1:1234",
            api_key="",
        ),
        runner=RunnerConfig(max_turns=3),
        session=SessionConfig(backend="memory", store_path=".kongming/sessions.db"),
        trace=TraceConfig(output_path=".kongming/traces/test.jsonl"),
        approval=ApprovalConfig(mode="auto_allow"),
        # 显式关 scheduler：本测专注 session/runtime 装配，cron ticker 由
        # test_cli_main_scheduler.py 单独覆盖。
        scheduler=SchedulerConfig(enabled=False),
    )


async def test_run_prefers_explicit_session_id(monkeypatch) -> None:
    captured: dict[str, str] = {}

    class _DummyRegistry:
        def register(self, _tool) -> None:
            return None

        def names(self) -> list[str]:
            return []

    class _DummyBridge:
        def __init__(
            self,
            *,
            runtime,
            adapter,
            session_id: str,
            echo_final_content: bool = True,
        ) -> None:
            del runtime, adapter, echo_final_content
            captured["session_id"] = session_id
            self.session_id = session_id

        async def run_loop(self) -> None:
            return None

    monkeypatch.setattr(cli_main, "_load_config_or_exit", lambda _: _build_cfg())

    async def _fake_assemble_instructions(_cfg, _files, *, skill_listing=""):
        return "system", [], None

    monkeypatch.setattr(cli_main, "_assemble_instructions", _fake_assemble_instructions)
    monkeypatch.setattr(cli_main, "build_default_registry", lambda **_: _DummyRegistry())
    monkeypatch.setattr(cli_main, "build_default_approval", lambda *_, **__: object())
    monkeypatch.setattr(
        cli_main.NativeRuntime, "build", staticmethod(lambda *_, **__: _DummyRuntime())
    )
    monkeypatch.setattr(cli_main, "SessionBridge", _DummyBridge)
    monkeypatch.setattr(cli_main, "_generate_cli_session_id", lambda: "cli-generated")
    monkeypatch.setattr(cli_main, "_print_banner", lambda *_, **__: None)
    monkeypatch.setattr(cli_main, "_discover_persistent_sessions", lambda _cfg: ([], None))

    await cli_main._run(
        config_path=None,
        session_id="manual-session",
        list_sessions=False,
        resume_last=False,
        verbose=False,
        smoke=False,
        instructions_files=[],
        trace_enabled=False,
        reasoning_effort=None,
    )

    assert captured["session_id"] == "manual-session"


async def test_run_reasoning_effort_overrides_config(monkeypatch) -> None:
    """--reasoning-effort CLI 参数应覆盖 cfg.model.reasoning_effort。"""
    captured: dict = {}

    class _DummyRegistry:
        def register(self, _tool) -> None:
            return None

        def names(self) -> list[str]:
            return []

    class _DummyBridge:
        def __init__(
            self,
            *,
            runtime,
            adapter,
            session_id: str,
            echo_final_content: bool = True,
        ) -> None:
            del runtime, adapter, session_id, echo_final_content
            self.session_id = "x"

        async def run_loop(self) -> None:
            return None

    monkeypatch.setattr(cli_main, "_load_config_or_exit", lambda _: _build_cfg())

    async def _fake_assemble_instructions(_cfg, _files, *, skill_listing=""):
        return "system", [], None

    monkeypatch.setattr(cli_main, "_assemble_instructions", _fake_assemble_instructions)
    monkeypatch.setattr(cli_main, "build_default_registry", lambda **_: _DummyRegistry())
    monkeypatch.setattr(cli_main, "build_default_approval", lambda *_, **__: object())

    def _capture_build(cfg, **kwargs):
        captured["reasoning_effort"] = cfg.model.reasoning_effort
        return _DummyRuntime()

    monkeypatch.setattr(cli_main.NativeRuntime, "build", staticmethod(_capture_build))
    monkeypatch.setattr(cli_main, "SessionBridge", _DummyBridge)
    monkeypatch.setattr(cli_main, "_generate_cli_session_id", lambda: "cli-x")
    monkeypatch.setattr(cli_main, "_print_banner", lambda *_, **__: None)
    monkeypatch.setattr(cli_main, "_discover_persistent_sessions", lambda _cfg: ([], None))

    await cli_main._run(
        config_path=None,
        session_id=None,
        list_sessions=False,
        resume_last=False,
        verbose=False,
        smoke=False,
        instructions_files=[],
        trace_enabled=False,
        reasoning_effort="low",
    )

    assert captured["reasoning_effort"] == "low"


def test_cli_help_shows_reasoning_effort_option() -> None:
    """--help 应展示 --reasoning-effort 选项。"""
    runner = CliRunner()
    result = runner.invoke(cli_main.main, ["--help"])
    assert result.exit_code == 0
    assert "reasoning-effort" in result.output


def test_cli_help_shows_debug_option() -> None:
    """--help 应展示 --debug 选项。"""
    runner = CliRunner()
    result = runner.invoke(cli_main.main, ["--help"])
    assert result.exit_code == 0
    assert "--debug" in result.output


def test_validate_session_selection_rejects_conflicting_flags() -> None:
    try:
        cli_main._validate_session_selection_or_exit(
            session_id="demo",
            list_sessions=False,
            resume_last=True,
            smoke=False,
        )
    except SystemExit as exc:
        assert exc.code == 2
    else:  # pragma: no cover - defensive
        raise AssertionError("expected SystemExit")


def test_bind_discovered_session_path_updates_persistent_backend() -> None:
    file_cfg = _build_cfg().model_copy(
        update={
            "session": _build_cfg().session.model_copy(
                update={"backend": "file", "file_store_path": ".kongming/sessions"}
            )
        }
    )
    sqlite_cfg = _build_cfg().model_copy(
        update={
            "session": _build_cfg().session.model_copy(
                update={"backend": "sqlite", "store_path": ".kongming/sessions.db"}
            )
        }
    )

    file_bound = cli_main._bind_discovered_session_path(file_cfg, Path("/tmp/file-sessions"))
    sqlite_bound = cli_main._bind_discovered_session_path(sqlite_cfg, Path("/tmp/sessions.db"))

    assert file_bound.session.file_store_path == "/tmp/file-sessions"
    assert sqlite_bound.session.store_path == "/tmp/sessions.db"


async def test_run_list_sessions_prints_and_skips_runtime_build(monkeypatch, capsys) -> None:
    cfg = _build_cfg().model_copy(
        update={
            "session": _build_cfg().session.model_copy(
                update={"backend": "file", "file_store_path": ".kongming/sessions"}
            )
        }
    )
    monkeypatch.setattr(cli_main, "_load_config_or_exit", lambda _: cfg)
    monkeypatch.setattr(
        cli_main,
        "_discover_persistent_sessions",
        lambda _cfg: (
            [
                cli_main.SessionSummary(
                    session_id="demo-1",
                    updated_at=1710000000.0,
                    last_role="user",
                    preview="hello world",
                    message_count=3,
                    backend="file",
                )
            ],
            None,
        ),
    )

    def _unexpected_build(*_args, **_kwargs):
        raise AssertionError("runtime should not build for --list-sessions")

    monkeypatch.setattr(cli_main.NativeRuntime, "build", staticmethod(_unexpected_build))

    await cli_main._run(
        config_path=None,
        session_id=None,
        list_sessions=True,
        resume_last=False,
        verbose=False,
        smoke=False,
        instructions_files=[],
        trace_enabled=False,
        reasoning_effort=None,
    )

    output = capsys.readouterr().out
    assert "demo-1" in output
    assert "hello world" in output


async def test_run_resume_last_uses_most_recent_session(monkeypatch) -> None:
    captured: dict[str, str] = {}

    class _DummyRegistry:
        def register(self, _tool) -> None:
            return None

        def names(self) -> list[str]:
            return []

    class _DummyBridge:
        def __init__(
            self,
            *,
            runtime,
            adapter,
            session_id: str,
            echo_final_content: bool = True,
        ) -> None:
            del runtime, adapter, echo_final_content
            captured["session_id"] = session_id
            self.session_id = session_id

        async def run_loop(self) -> None:
            return None

    monkeypatch.setattr(
        cli_main,
        "_load_config_or_exit",
        lambda _: _build_cfg().model_copy(
            update={
                "session": _build_cfg().session.model_copy(
                    update={"backend": "file", "file_store_path": ".kongming/sessions"}
                )
            }
        ),
    )

    async def _fake_assemble_instructions(_cfg, _files, *, skill_listing=""):
        return "system", [], None

    monkeypatch.setattr(cli_main, "_assemble_instructions", _fake_assemble_instructions)
    monkeypatch.setattr(cli_main, "build_default_registry", lambda **_: _DummyRegistry())
    monkeypatch.setattr(cli_main, "build_default_approval", lambda *_, **__: object())
    monkeypatch.setattr(
        cli_main.NativeRuntime, "build", staticmethod(lambda *_, **__: _DummyRuntime())
    )
    monkeypatch.setattr(cli_main, "SessionBridge", _DummyBridge)
    monkeypatch.setattr(cli_main, "_print_banner", lambda *_, **__: None)
    monkeypatch.setattr(
        cli_main,
        "_discover_persistent_sessions",
        lambda _cfg: (
            [
                cli_main.SessionSummary(
                    session_id="latest",
                    updated_at=200.0,
                    last_role="user",
                    preview="newer",
                    message_count=2,
                    backend="file",
                ),
                cli_main.SessionSummary(
                    session_id="older",
                    updated_at=100.0,
                    last_role="user",
                    preview="older",
                    message_count=2,
                    backend="file",
                ),
            ],
            None,
        ),
    )

    await cli_main._run(
        config_path=None,
        session_id=None,
        list_sessions=False,
        resume_last=True,
        verbose=False,
        smoke=False,
        instructions_files=[],
        trace_enabled=False,
        reasoning_effort=None,
    )

    assert captured["session_id"] == "latest"


async def test_run_rejects_missing_explicit_session(monkeypatch) -> None:
    cfg = _build_cfg().model_copy(
        update={
            "session": _build_cfg().session.model_copy(
                update={"backend": "file", "file_store_path": ".kongming/sessions"}
            )
        }
    )
    monkeypatch.setattr(cli_main, "_load_config_or_exit", lambda _: cfg)
    monkeypatch.setattr(cli_main, "_discover_persistent_sessions", lambda _cfg: ([], None))

    try:
        await cli_main._run(
            config_path=None,
            session_id="missing",
            list_sessions=False,
            resume_last=False,
            verbose=False,
            smoke=False,
            instructions_files=[],
            trace_enabled=False,
            reasoning_effort=None,
        )
    except SystemExit as exc:
        assert exc.code == 2
    else:  # pragma: no cover - defensive
        raise AssertionError("expected SystemExit")


async def test_run_debug_passes_prompt_debug_sink(monkeypatch) -> None:
    """--debug 进入 _run 后应给 NativeRuntime.build 注入 prompt debug sink。"""
    captured: dict = {}

    class _DummyRegistry:
        def register(self, _tool) -> None:
            return None

        def names(self) -> list[str]:
            return []

    class _DummyBridge:
        def __init__(
            self,
            *,
            runtime,
            adapter,
            session_id: str,
            echo_final_content: bool = True,
        ) -> None:
            del runtime, adapter, session_id, echo_final_content
            self.session_id = "x"

        async def run_loop(self) -> None:
            return None

    monkeypatch.setattr(cli_main, "_load_config_or_exit", lambda _: _build_cfg())

    async def _fake_assemble_instructions(_cfg, _files, *, skill_listing=""):
        return "system", ["agent_spec"], None

    monkeypatch.setattr(cli_main, "_assemble_instructions", _fake_assemble_instructions)
    monkeypatch.setattr(cli_main, "build_default_registry", lambda **_: _DummyRegistry())
    monkeypatch.setattr(cli_main, "build_default_approval", lambda *_, **__: object())

    def _capture_build(_cfg, **kwargs):
        captured.update(kwargs)
        return _DummyRuntime()

    monkeypatch.setattr(cli_main.NativeRuntime, "build", staticmethod(_capture_build))
    monkeypatch.setattr(cli_main, "SessionBridge", _DummyBridge)
    monkeypatch.setattr(cli_main, "_generate_cli_session_id", lambda: "cli-debug")
    monkeypatch.setattr(cli_main, "_print_banner", lambda *_, **__: None)
    monkeypatch.setattr(cli_main, "_discover_persistent_sessions", lambda _cfg: ([], None))

    await cli_main._run(
        config_path=None,
        session_id=None,
        list_sessions=False,
        resume_last=False,
        verbose=False,
        smoke=False,
        instructions_files=[],
        trace_enabled=False,
        reasoning_effort=None,
        prompt_debug=True,
    )

    assert captured["prompt_debug_sink"] is not None
    assert captured["instruction_origins"] == ["agent_spec"]
