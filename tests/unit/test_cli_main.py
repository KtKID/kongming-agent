from __future__ import annotations

from pathlib import Path

from click.testing import CliRunner

import hosts.cli.main as cli_main
from core.contracts import ApprovalDecision, ToolResult
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


class _WorkflowSmokeApproval:
    """记录 workflow smoke 审批请求，输入为请求，输出为批准结果。"""

    def __init__(self) -> None:
        self.requests = []

    async def decide(self, request):
        self.requests.append(request)
        return ApprovalDecision(outcome="approved", reason="test")


class _WorkflowSmokeTool:
    """记录 workflow smoke 工具调用，输入为参数和上下文，输出为 planner 失败结果。"""

    name = "run_agent_workflow"

    def __init__(self) -> None:
        self.calls = []

    async def execute(self, args, ctx):
        self.calls.append((args, ctx))
        return ToolResult(
            ok=False,
            content="工具执行失败：run_agent_workflow",
            error_message="map_reduce planner found no input files",
        )


class _WorkflowSmokeTools:
    """提供 workflow smoke 所需工具查找，输入为工具名，输出为工具实例。"""

    def __init__(self, tool: _WorkflowSmokeTool) -> None:
        self.tool = tool

    def __getitem__(self, name: str):
        if name != "run_agent_workflow":
            raise KeyError(name)
        return self.tool


class _WorkflowSmokeRuntime(_DummyRuntime):
    """提供 workflow smoke 所需 runtime 属性，输入为空，输出为测试 runtime。"""

    def __init__(self) -> None:
        self.approval = _WorkflowSmokeApproval()
        self.workflow_tool = _WorkflowSmokeTool()
        self.tools = _WorkflowSmokeTools(self.workflow_tool)


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


def test_cli_help_shows_workflow_smoke_option() -> None:
    runner = CliRunner()
    result = runner.invoke(cli_main.main, ["--help"])
    assert result.exit_code == 0
    assert "--workflow-smoke" in result.output


async def test_run_workflow_smoke_approves_and_executes_run_agent_workflow(capsys) -> None:
    runtime = _WorkflowSmokeRuntime()

    await cli_main._run_workflow_smoke(runtime, "workflow-smoke-test")

    assert len(runtime.approval.requests) == 1
    request = runtime.approval.requests[0]
    assert request.tool_name == "run_agent_workflow"
    assert request.arguments["mode"] == "map_reduce"
    assert len(runtime.workflow_tool.calls) == 1
    args, ctx = runtime.workflow_tool.calls[0]
    assert args["mode"] == "map_reduce"
    assert ctx.session_id == "workflow-smoke-test"
    assert "[workflow-smoke] ok" in capsys.readouterr().out


async def test_run_exposes_workflow_and_role_tools_to_cli_llm(monkeypatch) -> None:
    """CLI 正常装配时应把 workflow 工具和角色工具一起暴露给 LLM。"""
    captured: dict = {}

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
            self.session_id = session_id

        async def run_loop(self) -> None:
            return None

    async def _fake_assemble_instructions(_cfg, _files, *, skill_listing=""):
        del skill_listing
        return "system", ["agent_spec"], None

    async def _no_skills(*_args, **_kwargs):
        return []

    def _capture_build(_cfg, **kwargs):
        captured.update(kwargs)
        return _DummyRuntime()

    monkeypatch.setattr(cli_main, "_load_config_or_exit", lambda _: _build_cfg())
    monkeypatch.setattr(cli_main, "_assemble_instructions", _fake_assemble_instructions)
    monkeypatch.setattr(cli_main, "load_skill_specs", _no_skills)
    monkeypatch.setattr(cli_main, "format_skill_listing", lambda _specs: "")
    monkeypatch.setattr(cli_main, "build_default_approval", lambda *_, **__: object())
    monkeypatch.setattr(cli_main.NativeRuntime, "build", staticmethod(_capture_build))
    monkeypatch.setattr(cli_main, "SessionBridge", _DummyBridge)
    monkeypatch.setattr(cli_main, "_generate_cli_session_id", lambda: "cli-workflow-tools")
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
    )

    enabled_tool_names = captured["enabled_tool_names"]
    registry = captured["tools"]
    assert "run_agent_workflow" in enabled_tool_names
    assert "run_parallel_subagents" in enabled_tool_names
    assert "list_agent_roles" in enabled_tool_names
    assert "create_agent_role" in enabled_tool_names
    assert "run_agent_workflow" in registry
    assert "run_parallel_subagents" in registry
    assert "list_agent_roles" in registry
    assert "create_agent_role" in registry
    workflow_tool = registry["run_agent_workflow"]
    role_tool = registry["create_agent_role"]
    assert workflow_tool._handle.manager.role_manager is role_tool._manager  # type: ignore[attr-defined]


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


async def test_run_registers_mcp_runtime_tools_and_closes_manager(monkeypatch) -> None:
    captured: dict[str, object] = {}
    managers: list[object] = []

    class _FakeMcpWebSearchTool:
        name = "web_search"
        description = "fake mcp web search"
        input_schema = {"type": "object", "properties": {}}

        async def execute(self, _args, _ctx):
            return ToolResult(ok=True, content="ok")

    class _FakeMcpRuntimeRegistrationManager:
        def __init__(self, cfg, *, event_sinks=()):
            self.cfg = cfg
            self.event_sinks = tuple(event_sinks)
            self.register_calls = []
            self.closed = False
            managers.append(self)

        async def register(self, registry, *, excluded_tool_names=()):
            self.register_calls.append(tuple(excluded_tool_names))
            registry.register(_FakeMcpWebSearchTool())
            return object()

        async def aclose(self) -> None:
            self.closed = True

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
            self.session_id = session_id

        async def run_loop(self) -> None:
            return None

    async def _fake_assemble_instructions(_cfg, _files, *, skill_listing=""):
        del skill_listing
        return "system", ["agent_spec"], None

    async def _no_skills(*_args, **_kwargs):
        return []

    def _capture_build(_cfg, **kwargs):
        captured.update(kwargs)
        return _DummyRuntime()

    monkeypatch.setattr(cli_main, "_load_config_or_exit", lambda _: _build_cfg())
    monkeypatch.setattr(cli_main, "_assemble_instructions", _fake_assemble_instructions)
    monkeypatch.setattr(cli_main, "load_skill_specs", _no_skills)
    monkeypatch.setattr(cli_main, "format_skill_listing", lambda _specs: "")
    monkeypatch.setattr(cli_main, "build_default_approval", lambda *_, **__: object())
    monkeypatch.setattr(
        cli_main,
        "McpRuntimeRegistrationManager",
        _FakeMcpRuntimeRegistrationManager,
    )
    monkeypatch.setattr(cli_main.NativeRuntime, "build", staticmethod(_capture_build))
    monkeypatch.setattr(cli_main, "SessionBridge", _DummyBridge)
    monkeypatch.setattr(cli_main, "_generate_cli_session_id", lambda: "cli-mcp-tools")
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
    )

    assert len(managers) == 1
    manager = managers[0]
    assert manager.register_calls == [("evolution_write",)]  # type: ignore[attr-defined]
    assert manager.closed is True  # type: ignore[attr-defined]
    assert "web_search" in captured["enabled_tool_names"]  # type: ignore[operator]
    assert "web_search" in captured["tools"]  # type: ignore[operator]


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

    assert file_bound.session.file_store_path == str(Path("/tmp/file-sessions"))
    assert sqlite_bound.session.store_path == str(Path("/tmp/sessions.db"))


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
