"""CLI assembly integration tests."""

from __future__ import annotations

import asyncio
import hashlib
import json
import time
from pathlib import Path
from typing import Any

import hosts.cli.main as cli_main
from commands.models import CommandResult
from core.contracts import (
    ApprovalDecision,
    ApprovalRequest,
    LLMRequest,
    LLMResponse,
    PreparedToolCall,
    ToolContext,
    ToolResult,
)
from core.message import Message, ToolCall
from hosts.cli.interactive_loop import CLIInteractiveLoop, SendDelivery
from infrastructure.config.models import (
    ApprovalConfig,
    Config,
    ModelSelectionConfig,
    RunnerConfig,
    SchedulerConfig,
    SessionConfig,
    TraceConfig,
)
from infrastructure.tracing import JsonlTraceSink, PromptDebugDumpSink
from prompting import InstructionLoader
from runtime_assembly.session_engine import SessionEngine
from sessions import build_session
from sessions.session_bootstrap import SessionBootstrap

_REPO_ROOT = Path(__file__).resolve().parents[2]
# 测试 dump 与 CLI ``--debug`` 生产落盘 (.kongming/debug/prompt-debug-*.json)
# 物理分开，避免人工查看 debug 时被测试垃圾干扰。
_DUMP_DIR = _REPO_ROOT / ".kongming/debug/tests"


def _cli_loop(dispatcher):
    """构造绑定 HostDispatcher 的 CLI loop。

    host-dispatch-consolidation：bridge_factory 现发放 HostDispatcher。
    CLIInteractiveLoop 直接持有 dispatcher。
    """
    return CLIInteractiveLoop(host_dispatcher=dispatcher)


class _DummyMainCLIInteractiveLoop:
    """CLI main 装配测试用 loop 替身。"""

    def __init__(self, *, host_dispatcher, command_service=None, adapter=None) -> None:
        self.host_dispatcher = host_dispatcher
        self.command_service = command_service
        self.adapter = adapter

    async def run_loop(self) -> None:
        # _DummyHostDispatcher 把"写一条消息到 session"的行为挂在 run_loop 上，
        # 这里转发调用，模拟真实 CLIInteractiveLoop.run_loop 驱动交互。
        run_loop = getattr(self.host_dispatcher, "run_loop", None)
        if callable(run_loop):
            await run_loop()


def _build_cfg(
    tmp_path,
    *,
    backend: str = "memory",
    trace_file: str = "trace.jsonl",
    file_store_path: str | None = None,
) -> Config:
    """Build a config shaped like the CLI path, with storage under tmp_path."""
    return Config(
        model=ModelSelectionConfig(preset_id="local-gemma-4-e4b-it"),
        runner=RunnerConfig(max_turns=3),
        session=SessionConfig(
            backend=backend,
            store_path=str(tmp_path / "sessions.db"),
            file_store_path=file_store_path or str(tmp_path / "sessions"),
        ),
        trace=TraceConfig(output_path=str(tmp_path / trace_file)),
        approval=ApprovalConfig(mode="auto_allow"),
        scheduler=SchedulerConfig(enabled=False),
    )


def _bootstrap(tmp_path: Path) -> SessionBootstrap:
    return SessionBootstrap(
        agent_name="test-agent",
        model_name="stub-model",
        instruction_sources=["test"],
        instruction_text_hash="sha256:test",
        created_at=1000.0,
        cwd=str(tmp_path),
        app_version=None,
    )


def _message_to_dict(message: Message) -> dict:
    data = {"role": message.role, "content": message.content}
    if message.tool_calls:
        data["tool_calls"] = [
            {"id": call.id, "name": call.name, "arguments": call.arguments}
            for call in message.tool_calls
        ]
    return data


def _dump_prompt_flow_result(test_name: str, payload: dict) -> None:
    _DUMP_DIR.mkdir(parents=True, exist_ok=True)
    ts = time.strftime("%Y%m%d-%H%M%S")
    path = _DUMP_DIR / f"prompt-flow-{test_name}-{ts}.json"
    dump = {
        "timestamp": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "test": test_name,
        **payload,
    }
    path.write_text(json.dumps(dump, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\n📄 Prompt flow dump: {path.resolve()}")


async def test_cli_assembly_with_file_backend_persists_across_runtime_instances(
    stub_llm, recording_approval, tmp_path
):
    """CLI-style session_factory should preserve file history across runtime instances."""
    cfg = _build_cfg(tmp_path, backend="file", file_store_path=str(tmp_path / "file-sessions"))
    bootstrap = _bootstrap(tmp_path)

    stub_llm.script(content="ok-first")
    runtime1 = SessionEngine.build(
        cfg,
        approval=recording_approval,
        tools={},
        enabled_tool_names=[],
        session_factory=lambda sid: build_session(cfg, sid, bootstrap=bootstrap),
    )
    runtime1._llm = stub_llm  # type: ignore[attr-defined]

    result1 = await runtime1.run("remember banana", session_id="persist-1")
    assert result1.status == "completed"

    session_path = tmp_path / "file-sessions" / "persist-1" / "persist-1.jsonl"
    assert session_path.exists(), "FileSession 应创建 jsonl 文件"
    assert session_path.stat().st_size > 0

    stub_llm.script(content="ok-second")
    runtime2 = SessionEngine.build(
        cfg,
        approval=recording_approval,
        tools={},
        enabled_tool_names=[],
        session_factory=lambda sid: build_session(cfg, sid, bootstrap=bootstrap),
    )
    runtime2._llm = stub_llm  # type: ignore[attr-defined]

    result2 = await runtime2.run("what fruit?", session_id="persist-1")
    assert result2.status == "completed"

    last_request = stub_llm.calls[-1]
    contents = [m.content for m in last_request.messages if m.content]
    assert "remember banana" in contents
    assert "ok-first" in contents
    assert "what fruit?" in contents


async def test_cli_assembly_with_memory_backend_does_not_write_db(
    stub_llm, recording_approval, tmp_path
):
    """memory backend should not create sqlite files."""
    cfg = _build_cfg(tmp_path, backend="memory")

    stub_llm.script(content="ok")
    runtime = SessionEngine.build(
        cfg,
        approval=recording_approval,
        tools={},
        enabled_tool_names=[],
        session_factory=lambda sid: build_session(cfg, sid),
    )
    runtime._llm = stub_llm  # type: ignore[attr-defined]

    await runtime.run("hi", session_id="mem-1")

    db_path = tmp_path / "sessions.db"
    assert not db_path.exists(), "memory backend 不应创建 db 文件"


async def test_cli_assembly_with_jsonl_trace_sink_writes_jsonl_per_run(
    stub_llm, recording_approval, tmp_path
):
    """JsonlTraceSink should write a usable JSONL trace in a CLI-style assembly."""
    cfg = _build_cfg(tmp_path, trace_file="trace-cli.jsonl")
    trace_path = tmp_path / "trace-cli.jsonl"

    stub_llm.script(content="done")
    runtime = SessionEngine.build(
        cfg,
        event_sinks=[JsonlTraceSink(str(trace_path))],
        approval=recording_approval,
        tools={},
        enabled_tool_names=[],
        session_factory=lambda sid: build_session(cfg, sid),
    )
    runtime._llm = stub_llm  # type: ignore[attr-defined]

    result = await runtime.run("hello", session_id="trace-1")
    assert result.status == "completed"

    assert trace_path.exists(), "JsonlTraceSink 应创建 trace 文件"
    lines = [ln for ln in trace_path.read_text(encoding="utf-8").splitlines() if ln.strip()]
    assert lines, "trace 文件不应为空"

    events = [json.loads(ln) for ln in lines]
    kinds = [e["kind"] for e in events]
    assert "run.start" in kinds
    assert "run.end" in kinds
    assert any(k.startswith("llm.") for k in kinds)


async def test_cli_assembly_injects_instructions_from_loader_into_session(
    stub_llm, recording_approval, tmp_path, monkeypatch
):
    """InstructionLoader output should reach the first LLM request as one system message."""
    extra_file = tmp_path / "rules.md"
    extra_file.write_text(
        "只用中文回答，遇到 shell 命令先解释意图。",
        encoding="utf-8",
    )
    monkeypatch.setenv("KONGMING_EXTRA_INSTRUCTIONS", "本项目使用 Python 3.11。")

    loader = InstructionLoader(extra_files=[extra_file], include_env=True)
    sources = await loader.load(agent_instructions="You are kongming agent.")
    instructions = loader.render(sources)

    assert "You are kongming agent." in instructions
    assert "只用中文回答" in instructions
    assert "本项目使用 Python 3.11" in instructions
    assert "# agent_spec" in instructions
    assert "# file:rules.md" in instructions
    assert "# env:KONGMING_EXTRA_INSTRUCTIONS" in instructions

    cfg = _build_cfg(tmp_path)

    stub_llm.script(content="roger")
    runtime = SessionEngine.build(
        cfg,
        approval=recording_approval,
        tools={},
        enabled_tool_names=[],
        instructions=instructions,
    )
    runtime._llm = stub_llm  # type: ignore[attr-defined]

    await runtime.run("hi", session_id="inst-1")

    first_request = stub_llm.calls[0]
    system_msgs = [m for m in first_request.messages if m.role == "system"]
    assert len(system_msgs) == 1
    assert system_msgs[0].content == instructions


async def test_native_runtime_build_honors_explicit_instructions_param(
    stub_llm, recording_approval, tmp_path
):
    """SessionEngine.build(instructions=...) should override the default system text."""
    cfg = _build_cfg(tmp_path)

    stub_llm.script(content="ok")
    runtime = SessionEngine.build(
        cfg,
        approval=recording_approval,
        tools={},
        enabled_tool_names=[],
        instructions="CUSTOM-SYSTEM-PROMPT",
    )
    runtime._llm = stub_llm  # type: ignore[attr-defined]

    await runtime.run("hi", session_id="inst-ov-1")

    first_request = stub_llm.calls[0]
    system_content = next((m.content for m in first_request.messages if m.role == "system"), None)
    assert system_content == "CUSTOM-SYSTEM-PROMPT"


async def test_native_runtime_build_empty_instructions_falls_back_to_default(
    stub_llm, recording_approval, tmp_path
):
    """Blank instructions should fall back to the default system text."""
    cfg = _build_cfg(tmp_path)

    stub_llm.script(content="ok")
    runtime = SessionEngine.build(
        cfg,
        approval=recording_approval,
        tools={},
        enabled_tool_names=[],
        instructions="   ",
    )
    runtime._llm = stub_llm  # type: ignore[attr-defined]

    await runtime.run("hi", session_id="inst-fb-1")

    first_request = stub_llm.calls[0]
    system_content = next((m.content for m in first_request.messages if m.role == "system"), None)
    assert system_content == "You are kongming agent."


async def test_prompt_complete_flow_uses_production_assembly_and_dumps_json(
    stub_llm, recording_approval, tmp_path, monkeypatch
):
    """完整 prompt 流程调用生产组装链路，并落盘最终请求 JSON。"""
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("KONGMING_HOME", str(tmp_path / ".kongming"))
    monkeypatch.setenv("KONGMING_EXTRA_INSTRUCTIONS", "ENV-RULE: prefer concise answers.")

    extra_file = tmp_path / "rules.md"
    extra_file.write_text("FILE-RULE: answer in Chinese.", encoding="utf-8")

    memory_file = tmp_path / ".kongming" / "memory" / "MEMORY.md"
    memory_file.parent.mkdir(parents=True, exist_ok=True)
    memory_file.write_text("MEMORY-RULE: remember project prompt flow.", encoding="utf-8")

    rendered_prompt, origins, _memory_store = await cli_main._assemble_instructions(
        _build_cfg(tmp_path, backend="memory"), [extra_file]
    )

    # runtime 段由 _assemble_instructions 强制前置注入（cwd / kongming_home），
    # 让 LLM 知道自己跑在哪。不可关闭。
    assert origins == [
        "runtime",
        "workflow_catalog",
        "agent_spec",
        "file:rules.md",
        "env:KONGMING_EXTRA_INSTRUCTIONS",
        "memory",
    ]
    assert "# runtime" in rendered_prompt
    assert "# workflow_catalog" in rendered_prompt
    assert "# agent_spec" in rendered_prompt
    assert "# file:rules.md" in rendered_prompt
    assert "# env:KONGMING_EXTRA_INSTRUCTIONS" in rendered_prompt
    assert "# memory" in rendered_prompt
    assert "FILE-RULE: answer in Chinese." in rendered_prompt
    assert "ENV-RULE: prefer concise answers." in rendered_prompt
    assert "MEMORY-RULE: remember project prompt flow." in rendered_prompt

    cfg = _build_cfg(tmp_path, backend="memory")
    runtime = SessionEngine.build(
        cfg,
        approval=recording_approval,
        tools={},
        enabled_tool_names=[],
        instructions=rendered_prompt,
    )
    runtime._llm = stub_llm  # type: ignore[attr-defined]

    stub_llm.script(content="first-answer")
    first_result = await runtime.run("first user", session_id="prompt-flow")
    stub_llm.script(content="second-answer")
    second_result = await runtime.run("second user", session_id="prompt-flow")

    assert first_result.status == "completed"
    assert second_result.status == "completed"
    assert len(stub_llm.calls) == 2

    first_request, second_request = stub_llm.calls
    for request in (first_request, second_request):
        system_msgs = [m for m in request.messages if m.role == "system"]
        assert len(system_msgs) == 1
        assert system_msgs[0].content == rendered_prompt

    second_contents = [m.content for m in second_request.messages if m.content]
    assert "first user" in second_contents
    assert "first-answer" in second_contents
    assert "second user" in second_contents

    session = runtime._sessions["prompt-flow"]  # type: ignore[attr-defined]
    session_history = await session.history()
    assert [m.role for m in session_history] == ["user", "assistant", "user", "assistant"]

    _dump_prompt_flow_result(
        "production-assembly-two-runs",
        {
            "instruction_origins": origins,
            "rendered_prompt": rendered_prompt,
            "first_request_messages": [_message_to_dict(m) for m in first_request.messages],
            "second_request_messages": [_message_to_dict(m) for m in second_request.messages],
            "session_history": [_message_to_dict(m) for m in session_history],
            "results": [first_result.status, second_result.status],
        },
    )


async def test_prompt_debug_mode_dumps_runtime_prompt_snapshot(
    stub_llm, recording_approval, tmp_path
):
    """SessionEngine debug sink 应保存真实 Runner turn 的 prompt 快照。"""
    debug_dir = tmp_path / "debug"
    cfg = _build_cfg(tmp_path, backend="memory")
    runtime = SessionEngine.build(
        cfg,
        approval=recording_approval,
        tools={},
        enabled_tool_names=[],
        instructions="# agent_spec\nSYS",
        prompt_debug_sink=PromptDebugDumpSink(debug_dir),
        instruction_origins=["agent_spec"],
    )
    runtime._llm = stub_llm  # type: ignore[attr-defined]

    stub_llm.script(content="ok")
    result = await runtime.run("hello", session_id="debug-session")

    assert result.status == "completed"
    dump_files = list(debug_dir.glob("prompt-debug-debug-session-*-turn-1-*.json"))
    assert len(dump_files) == 1

    payload = json.loads(dump_files[0].read_text(encoding="utf-8"))
    assert payload["session_id"] == "debug-session"
    assert payload["turn"] == 1
    assert payload["instruction_origins"] == ["agent_spec"]
    assert payload["added_system_prompt"] == "# agent_spec\nSYS"
    assert payload["history_before_assemble"] == [{"role": "user", "content": "hello"}]
    assert payload["assembled_messages"][0] == {"role": "system", "content": "# agent_spec\nSYS"}
    assert payload["metadata"]["instruction_sources"] == [""]


def test_instruction_render_hash_is_reproducible() -> None:
    """The same rendered instruction text should produce a stable sha256."""
    from prompting.instructions.instruction_loader import InstructionSource

    sources = [
        InstructionSource(origin="agent_spec", content="Be helpful"),
        InstructionSource(origin="file:rules.md", content="Follow rules"),
    ]
    loader = InstructionLoader()
    rendered = loader.render(sources)

    hash1 = hashlib.sha256(rendered.encode()).hexdigest()
    hash2 = hashlib.sha256(rendered.encode()).hexdigest()
    assert hash1 == hash2
    assert rendered == "# agent_spec\nBe helpful\n\n# file:rules.md\nFollow rules"


def test_empty_instruction_sources_hash_equals_sha256_empty_string() -> None:
    """Empty instruction sources should hash like an empty string."""
    loader = InstructionLoader()
    rendered = loader.render([])

    assert rendered == ""
    expected = hashlib.sha256(b"").hexdigest()
    actual = hashlib.sha256(rendered.encode()).hexdigest()
    assert actual == expected


async def test_cli_run_persists_instruction_metadata_to_file_manifest(
    tmp_path, monkeypatch
) -> None:
    """_run should persist assembled instruction metadata into the file-session manifest."""
    cfg = _build_cfg(
        tmp_path,
        backend="file",
        file_store_path=str(tmp_path / "file-sessions"),
    )
    extra_file = tmp_path / "rules.md"
    extra_file.write_text("只用中文回答。", encoding="utf-8")
    monkeypatch.setenv("KONGMING_EXTRA_INSTRUCTIONS", "本项目使用 Python 3.11。")

    class _DummyRegistry:
        def register(self, _tool) -> None:
            return None

        def names(self) -> list[str]:
            return []

    class _FakeRuntime:
        def __init__(self, *, session_factory) -> None:  # type: ignore[no-untyped-def]
            self._session_factory = session_factory

        async def aclose(self) -> None:
            # 与真实 SessionEngine.aclose 签名保持一致：CLI 退出路径 await 该方法。
            return None

    class _DummyHostDispatcher:
        def __init__(
            self,
            *,
            runtime,
            session_id: str,
            queued_result_handler=None,
            agent_tree_runtime_router=None,
        ) -> None:
            del queued_result_handler, agent_tree_runtime_router
            self._runtime = runtime
            self.session_id = session_id

        async def run_loop(self) -> None:
            session = self._runtime._session_factory(self.session_id)
            await session.append(Message.user("hello"))

    def _fake_build(*args, **kwargs):  # type: ignore[no-untyped-def]
        del args
        return _FakeRuntime(session_factory=kwargs["session_factory"])

    monkeypatch.setattr(cli_main, "_load_config_or_exit", lambda _: cfg)
    monkeypatch.setattr(cli_main, "build_default_registry", lambda **_: _DummyRegistry())
    monkeypatch.setattr(cli_main, "build_default_approval", lambda *_, **__: object())

    async def _no_skill_specs(*_args, **_kwargs):
        return []

    monkeypatch.setattr(cli_main, "load_skill_specs", _no_skill_specs)
    monkeypatch.setattr(cli_main.SessionEngine, "build", staticmethod(_fake_build))
    monkeypatch.setattr(cli_main, "HostDispatcher", _DummyHostDispatcher)
    # host-dispatch-consolidation：_run 改走 build_default_command_service 装配命令面，
    # 替身为最小对象避免触达真实命令注册。
    monkeypatch.setattr(
        cli_main, "build_default_command_service", lambda *, adapter, runtime_delegate: object()
    )
    monkeypatch.setattr(cli_main, "CLIInteractiveLoop", _DummyMainCLIInteractiveLoop)
    monkeypatch.setattr(cli_main, "_generate_cli_session_id", lambda: "cli-meta")
    monkeypatch.setattr(cli_main, "_print_banner", lambda *_, **__: None)

    await cli_main._run(
        config_path=None,
        session_id=None,
        list_sessions=False,
        resume_last=False,
        verbose=False,
        smoke=False,
        instructions_files=[extra_file],
        trace_enabled=False,
        reasoning_effort=None,
    )

    expected_instructions, expected_origins, _memory_store = await cli_main._assemble_instructions(
        cfg, [extra_file]
    )
    expected_hash = f"sha256:{hashlib.sha256(expected_instructions.encode()).hexdigest()}"
    manifest_path = tmp_path / "file-sessions" / "cli-meta" / "manifest.json"
    system_prompt_path = tmp_path / "file-sessions" / "cli-meta" / "system_prompt.json"

    assert manifest_path.exists(), "CLI file backend 应写出 manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    assert manifest["instruction_sources"] == expected_origins
    assert manifest["instruction_text_hash"] == expected_hash
    assert system_prompt_path.exists(), "CLI file backend 应写出 system_prompt.json"
    system_prompt = json.loads(system_prompt_path.read_text(encoding="utf-8"))
    assert system_prompt["record_type"] == "system_prompt"
    assert system_prompt["instruction_sources"] == expected_origins
    assert system_prompt["instruction_text_hash"] == expected_hash
    assert system_prompt["content"] == expected_instructions


async def test_cli_run_lists_existing_file_sessions_before_runtime_start(
    tmp_path, monkeypatch, capsys
) -> None:
    cfg = _build_cfg(
        tmp_path,
        backend="file",
        file_store_path=str(tmp_path / "file-sessions"),
    )
    _write_file_session_record(
        tmp_path / "file-sessions",
        "demo-a",
        role="user",
        content="latest answer",
        created_at=200.0,
    )
    _write_file_session_record(
        tmp_path / "file-sessions",
        "demo-b",
        role="user",
        content="older question",
        created_at=100.0,
    )

    monkeypatch.setattr(cli_main, "_load_config_or_exit", lambda _: cfg)

    def _unexpected_build(*_args, **_kwargs):
        raise AssertionError("runtime should not build for --list-sessions")

    monkeypatch.setattr(cli_main.SessionEngine, "build", staticmethod(_unexpected_build))

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
    assert "demo-a" in output
    assert "latest an…" in output
    assert output.index("demo-a") < output.index("demo-b")


async def test_cli_run_resume_last_selects_latest_file_session(tmp_path, monkeypatch) -> None:
    cfg = _build_cfg(
        tmp_path,
        backend="file",
        file_store_path=str(tmp_path / "file-sessions"),
    )
    _write_file_session_record(
        tmp_path / "file-sessions",
        "older",
        role="user",
        content="old answer",
        created_at=100.0,
    )
    _write_file_session_record(
        tmp_path / "file-sessions",
        "latest",
        role="user",
        content="new answer",
        created_at=200.0,
    )

    captured: dict[str, str] = {}

    class _DummyRegistry:
        def register(self, _tool) -> None:
            return None

        def names(self) -> list[str]:
            return []

    class _FakeRuntime:
        def __init__(self, *, session_factory) -> None:  # type: ignore[no-untyped-def]
            self._session_factory = session_factory

        async def aclose(self) -> None:
            return None

    class _DummyHostDispatcher:
        def __init__(
            self,
            *,
            runtime,
            session_id: str,
            queued_result_handler=None,
            agent_tree_runtime_router=None,
        ) -> None:
            del runtime, queued_result_handler, agent_tree_runtime_router
            captured["session_id"] = session_id
            self.session_id = session_id

        async def run_loop(self) -> None:
            return None

    monkeypatch.setattr(cli_main, "_load_config_or_exit", lambda _: cfg)

    async def _fake_assemble_instructions(_cfg, _files, **_kwargs):
        return "system", [], None

    monkeypatch.setattr(cli_main, "_assemble_instructions", _fake_assemble_instructions)
    monkeypatch.setattr(cli_main, "build_default_registry", lambda **_: _DummyRegistry())
    monkeypatch.setattr(cli_main, "build_default_approval", lambda *_, **__: object())
    monkeypatch.setattr(
        cli_main.SessionEngine,
        "build",
        staticmethod(lambda *_, **kwargs: _FakeRuntime(session_factory=kwargs["session_factory"])),
    )
    monkeypatch.setattr(cli_main, "HostDispatcher", _DummyHostDispatcher)
    monkeypatch.setattr(
        cli_main, "build_default_command_service", lambda *, adapter, runtime_delegate: object()
    )
    monkeypatch.setattr(cli_main, "CLIInteractiveLoop", _DummyMainCLIInteractiveLoop)
    monkeypatch.setattr(cli_main, "_print_banner", lambda *_, **__: None)

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


def _write_file_session_record(
    root: Path,
    session_id: str,
    *,
    role: str,
    content: str,
    created_at: float,
) -> None:
    session_dir = root / session_id
    session_dir.mkdir(parents=True, exist_ok=True)
    record = {
        "schema_version": "0.1.1",
        "session_id": session_id,
        "message_id": f"msg-{session_id}",
        "parent_message_id": None,
        "created_at": created_at,
        "message": {
            "role": role,
            "content": content,
        },
    }
    (session_dir / f"{session_id}.jsonl").write_text(
        json.dumps(record, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )


# ===========================================================================
# 发送链路（mailbox）e2e —— cli-mailbox-steer-send #5
#
# 全部经真实 SessionEngine + Runner + agent_tree mailbox（只 stub LLM），入口只走
# 公开接口 ``bridge.run_once`` / ``bridge.send``；bridge 生命周期由 conftest 的
# ``bridge_factory`` fixture 统一 teardown（用例体内不出现任何关闭调用）。
# ===========================================================================


def _build_mailbox_cfg(tmp_path: Path) -> Config:
    """构造发送链路用例专用 Config（memory backend，approval=auto_allow）。

    与 ``_build_cfg`` 同款但显式命名，突出这批用例走 mailbox 全链路。max_turns 给足
    以容纳 tool_call → 下一 turn。输入 tmp_path 仅用于 session store 占位路径（memory
    backend 不落盘）；输出一份可直接喂 SessionEngine.build 的 Config。
    """
    return _build_cfg(tmp_path, backend="memory")


class _GatedLLM:
    """门控 stub LLM（发送链路用例专用，从公开 ``runtime._llm`` 注入点进入）。

    职责：让测试能在"一次 run 正跑到某个 turn"的确定时机介入（steer / send）。
    第一次 ``complete`` 挂在 ``gate`` event 上等测试放行；之后每次 complete 弹出
    预置响应列表 ``responses`` 的队首，用完返回终止 stop。每次 complete 都把收到的
    ``request.messages`` 记进 ``requests``，供断言注入文本是否出现在后续请求里。

    关键输入：``responses``（(content, tool_calls) 序列）+ ``gate``（第一次挂起的门）。
    关键输出：``complete`` 返回 LLMResponse；副作用是累计 ``complete_called`` /
    ``requests``。结构上满足 core.contracts.LLMProvider（单 async complete）。
    """

    def __init__(
        self,
        responses: list[tuple[str | None, list[ToolCall] | None]],
        *,
        gate: asyncio.Event,
    ) -> None:
        self._responses = list(responses)
        self._gate = gate
        self.complete_called = 0
        self.requests: list[tuple[Message, ...]] = []

    async def complete(self, request: LLMRequest) -> LLMResponse:
        self.complete_called += 1
        self.requests.append(tuple(request.messages))
        if self.complete_called == 1:
            # 第一次挂起，给测试代码在 run 进行中 send/steer 的时间窗口。
            await self._gate.wait()
        if self._responses:
            content, tool_calls = self._responses.pop(0)
        else:
            content, tool_calls = ("done", None)
        calls_tuple = tuple(tool_calls) if tool_calls else None
        msg = Message(role="assistant", content=content, tool_calls=calls_tuple)
        finish = "tool_calls" if calls_tuple else "stop"
        return LLMResponse(message=msg, finish_reason=finish)


class _QuickTool:
    """Tool stub：立即返回成功。

    用于让门控 LLM 第一轮发一个 tool_call、从而产生第二个 turn（steer 有 drain 时机）。
    execute 输入 (args, ctx) 忽略内容，输出恒定成功 ToolResult。
    """

    name = "quick"
    description = "returns immediately"
    input_schema: dict[str, Any] = {"type": "object", "properties": {}, "required": []}

    async def execute(self, prepared: PreparedToolCall, ctx: ToolContext) -> ToolResult:
        del prepared, ctx
        return ToolResult(ok=True, content="quick ok")


class _AllowApproval:
    """恒批准 approval：tool_call 一律放行（发送链路用例不测审批分支）。"""

    async def decide(self, request: ApprovalRequest) -> ApprovalDecision:
        return ApprovalDecision(outcome="approved")


async def _wait_until(predicate: Any, *, timeout: float = 5.0) -> None:
    """轮询等到 predicate() 为真或超时（公开可观察面，不摸 bridge/runtime 内脏）。

    输入 predicate（无参可调用返回 bool）+ 总超时秒数；到期未满足抛 AssertionError。
    用小步 sleep 轮询而非固定 sleep，避免时序赌博也不依赖内部事件。
    """
    deadline = asyncio.get_event_loop().time() + timeout
    while not predicate():
        if asyncio.get_event_loop().time() > deadline:
            raise AssertionError("predicate did not become true within timeout")
        await asyncio.sleep(0.01)


def _user_texts(messages: tuple[Message, ...]) -> list[str]:
    """从一次请求的 messages 里抽出所有 user 文本，供断言注入是否命中。"""
    return [m.content for m in messages if m.role == "user" and m.content]


def _build_stub_runtime(
    tmp_path: Path,
    llm: Any,
    *,
    recording_approval: Any,
) -> SessionEngine:
    """按 CLI 装配形态 build 一个 SessionEngine 并注入给定 stub llm（memory backend）。

    输入 tmp_path（store 占位）+ 任意满足 LLMProvider 的 llm + approval；输出装好的
    SessionEngine（``_llm`` 已替换为 stub，不发真实 HTTP）。tools 空、无白名单，用于
    纯文本 run 用例。
    """
    cfg = _build_mailbox_cfg(tmp_path)
    runtime = SessionEngine.build(
        cfg,
        approval=recording_approval,
        tools={},
        enabled_tool_names=[],
        session_factory=lambda sid: build_session(cfg, sid),
    )
    runtime._llm = llm  # type: ignore[attr-defined]
    return runtime


# ---------------------------------------------------------------------------
# 1. 单轮 run_once → completed Result
# ---------------------------------------------------------------------------


async def test_mailbox_single_run_once_returns_completed(
    stub_llm, recording_approval, tmp_path, bridge_factory
):
    """经 mailbox 全链路的一次 run_once 返回 completed Result，final content 对，LLM 一次。"""
    stub_llm.script(content="hello from stub")
    runtime = _build_stub_runtime(tmp_path, stub_llm, recording_approval=recording_approval)
    bridge = await bridge_factory(runtime=runtime, session_id="mbx-single")

    result = await bridge.run_once("hi")

    assert not isinstance(result, CommandResult)
    assert result.status == "completed"
    assert result.final_message is not None
    assert result.final_message.content == "hello from stub"
    assert len(stub_llm.calls) == 1


# ---------------------------------------------------------------------------
# 2. 多轮同 session：第二轮 LLM 请求看得到第一轮 history
# ---------------------------------------------------------------------------


async def test_mailbox_multi_run_once_history_continuity(
    stub_llm, recording_approval, tmp_path, bridge_factory
):
    """同一 bridge 连续两轮 run_once，第二轮 LLM 请求 messages 含第一轮 user + assistant。"""
    stub_llm.script(content="answer-1")
    stub_llm.script(content="answer-2")
    runtime = _build_stub_runtime(tmp_path, stub_llm, recording_approval=recording_approval)
    bridge = await bridge_factory(runtime=runtime, session_id="mbx-multi")

    result1 = await bridge.run_once("first user")
    result2 = await bridge.run_once("second user")

    assert not isinstance(result1, CommandResult)
    assert not isinstance(result2, CommandResult)
    assert result1.status == "completed"
    assert result2.status == "completed"
    assert len(stub_llm.calls) == 2

    second_contents = [m.content for m in stub_llm.calls[1].messages if m.content]
    assert "first user" in second_contents
    assert "answer-1" in second_contents
    assert "second user" in second_contents


# ---------------------------------------------------------------------------
# 3. "/斜杠命令"不进 mailbox：返回 CommandResult 且 LLM 零调用
# ---------------------------------------------------------------------------


async def test_mailbox_slash_command_bypasses_mailbox(
    stub_llm, recording_approval, tmp_path, bridge_factory
):
    """未知 /命令走命令控制面（不投 mailbox）：返回 CommandResult，stub LLM 零调用。"""
    runtime = _build_stub_runtime(tmp_path, stub_llm, recording_approval=recording_approval)
    bridge = await bridge_factory(runtime=runtime, session_id="mbx-cmd")

    result = await bridge.run_once("/nonexistent-command")

    assert isinstance(result, CommandResult)
    assert len(stub_llm.calls) == 0


# ---------------------------------------------------------------------------
# 4. send_now 立即发送（核心）：显式插入当前 run，不新建 run
# ---------------------------------------------------------------------------


async def test_mailbox_send_now_injects_into_active_run(
    recording_approval, tmp_path, bridge_factory
):
    """run 进行中 send_now("msg2") → delivery=="send_now"，msg2 注入当前 run（不新建）。

    多轮构造（见报告）：门控 LLM 第一轮返回一个 ``quick`` 的 tool_call（非终止形态，
    runner 只看 tool_calls 判终止），从而产生第二个 turn；第二个 turn 开头 runner drain
    steer buffer，把 send 进来的 msg2 注入成 user 消息，第二次 LLM 请求即可见。第二轮
    返回 content stop 终止。因此：run 只有一个（同一次 run_once），turn_count==2。

    流程：create_task 跑 run_once("msg1") → 等第一次 complete 挂在 gate（run 已开始）→
    send_now("msg2") 断言 send_now → 放行 gate → await run 完成断言 completed / turn_count==2 /
    第二次请求 messages 含 msg2。整体套 asyncio.wait_for 防挂死。
    """
    gate = asyncio.Event()
    llm = _GatedLLM(
        [
            # 第一轮：发 quick 的 tool_call（非终止）→ 制造第二个 turn 供 drain。
            (None, [ToolCall(call_id="c1", tool_name="quick", arguments={})]),
            # 第二轮：普通 content，stop 终止。
            ("done", None),
        ],
        gate=gate,
    )
    cfg = _build_mailbox_cfg(tmp_path)
    runtime = SessionEngine.build(
        cfg,
        approval=_AllowApproval(),
        tools={"quick": _QuickTool()},
        enabled_tool_names=["quick"],
        session_factory=lambda sid: build_session(cfg, sid),
    )
    runtime._llm = llm  # type: ignore[attr-defined]
    bridge = await bridge_factory(runtime=runtime, session_id="mbx-steer")

    async def _scenario() -> Any:
        task: asyncio.Task[Any] = asyncio.create_task(bridge.run_once("msg1"))
        # 等 run 走进第一次 complete（run 已开始、buffer 已注册、挂在 gate 上）。
        await _wait_until(lambda: llm.complete_called >= 1)
        # run 进行中 send_now：命中活跃 run → send_now。
        receipt = await _cli_loop(bridge).send_now("msg2")
        assert receipt.delivery == SendDelivery.SEND_NOW
        gate.set()  # 放行第一次 complete，run 继续到第二个 turn（drain msg2）。
        return await task

    result = await asyncio.wait_for(_scenario(), timeout=10.0)

    assert not isinstance(result, CommandResult)
    assert result.status == "completed"
    # 只有一个 run：turn1(tool_call) + turn2(stop) == 2 turns。
    assert result.turn_count == 2
    # 第二次 complete 的请求 messages 含注入的 msg2（drain 发生在 turn2 开头）。
    assert llm.complete_called >= 2
    assert "msg2" in _user_texts(llm.requests[1])


async def test_mailbox_send_defaults_to_queue_during_active_run(
    recording_approval, tmp_path, bridge_factory
):
    """run 进行中 send("msg2") → delivery=="queued"，msg2 作为下一轮独立 run。

    CLI 普通回车对应 Web user.input 普通排队语义；即使当前 run 有可 drain 的第二个
    turn，普通 send 也不插入当前 run。
    """
    gate = asyncio.Event()
    llm = _GatedLLM(
        [
            (None, [ToolCall(call_id="c1", tool_name="quick", arguments={})]),
            ("done-first", None),
            ("done-second", None),
        ],
        gate=gate,
    )
    cfg = _build_mailbox_cfg(tmp_path)
    runtime = SessionEngine.build(
        cfg,
        approval=_AllowApproval(),
        tools={"quick": _QuickTool()},
        enabled_tool_names=["quick"],
        session_factory=lambda sid: build_session(cfg, sid),
    )
    runtime._llm = llm  # type: ignore[attr-defined]
    bridge = await bridge_factory(runtime=runtime, session_id="mbx-default-queue")

    async def _scenario() -> Any:
        task: asyncio.Task[Any] = asyncio.create_task(bridge.run_once("msg1"))
        await _wait_until(lambda: llm.complete_called >= 1)
        receipt = await _cli_loop(bridge).send("msg2")
        assert receipt.delivery == SendDelivery.QUEUED
        gate.set()
        return await task

    result = await asyncio.wait_for(_scenario(), timeout=10.0)

    assert not isinstance(result, CommandResult)
    assert result.status == "completed"
    assert result.turn_count == 2
    assert llm.complete_called >= 2
    assert "msg2" not in _user_texts(llm.requests[1])
    await _wait_until(lambda: llm.complete_called >= 3, timeout=5.0)
    assert "msg2" in _user_texts(llm.requests[2])


# ---------------------------------------------------------------------------
# 5. 普通 send 默认排队 → delivery=="queued"
# ---------------------------------------------------------------------------


async def test_mailbox_idle_send_falls_back_to_queued(
    stub_llm, recording_approval, tmp_path, bridge_factory
):
    """空闲时 send("solo") → delivery=="queued"，排队的独立新 run 最终确实跑起来。

    等待取舍（见报告）：用例体内不能出现关闭调用，而排队 run 是异步 task 跑的。因此不
    调 aclose(drain=True)（那算关闭调用，违反约束），改为轮询**公开可观察面**——
    stub_llm.calls 在超时内变为 1（run 已被 agent_loop 消费执行）。这仍是公开接口观察，
    不摸 bridge 内部；剩余收尾交给 fixture 的 aclose(drain=True) teardown。
    """
    stub_llm.script(content="solo-answer")
    runtime = _build_stub_runtime(tmp_path, stub_llm, recording_approval=recording_approval)
    bridge = await bridge_factory(runtime=runtime, session_id="mbx-queued")

    receipt = await _cli_loop(bridge).send("solo")
    assert receipt.delivery == SendDelivery.QUEUED

    # 排队 run 是异步 task，轮询等它被 agent_loop 消费并调用 LLM（公开可观察面）。
    await _wait_until(lambda: len(stub_llm.calls) == 1, timeout=5.0)
    assert "solo" in _user_texts(tuple(stub_llm.calls[0].messages))


# ---------------------------------------------------------------------------
# 6. send_now 残留回投：run 收尾期插入未命中 drain → 残留回投成新 run
# ---------------------------------------------------------------------------


async def test_mailbox_send_leftover_reback_or_queued(recording_approval, tmp_path, bridge_factory):
    """run 收尾期 send_now("late-msg")：两种合法结局都接住（竞态天然二分）。

    构造：门控 LLM 第一轮即终止（stop，无 tool_call），且 complete 挂在 gate 上。等第一次
    complete 已开始（run 即将在这一 turn 结束、无第二个 turn 可 drain）→ send_now("late-msg")：

    - 若 steer 命中（buffer 尚未 close）：delivery=="send_now"，但这是终止 turn 注不进去，
      runner 把 late-msg 写进 result.metadata["steer_undelivered"]，bridge 收尾后按 queued
      路径回投成一条**新 run**（消息不丢）。
    - 若竞态落在 run 已收尾之后（buffer 已 close）：steer 返回 False，delivery=="queued"，
      直接排队成新 run。

    两种结局都要求：late-msg 最终获得独立新 run —— stub 被再次调起（总调用数≥2）且某次
    请求 messages 含 late-msg。整体套 asyncio.wait_for 防挂死。
    """
    gate = asyncio.Event()
    # 第一轮直接 stop（无 tool_call）→ 只有一个 turn，无第二 turn 可 drain。
    llm = _GatedLLM([("done", None)], gate=gate)
    cfg = _build_mailbox_cfg(tmp_path)
    runtime = SessionEngine.build(
        cfg,
        approval=_AllowApproval(),
        tools={},
        enabled_tool_names=[],
        session_factory=lambda sid: build_session(cfg, sid),
    )
    runtime._llm = llm  # type: ignore[attr-defined]
    bridge = await bridge_factory(runtime=runtime, session_id="mbx-leftover")

    async def _scenario() -> SendDelivery:
        task: asyncio.Task[Any] = asyncio.create_task(bridge.run_once("run1"))
        # 等第一次 complete 已开始（run 走进终止 turn，即将结束）。
        await _wait_until(lambda: llm.complete_called >= 1)
        receipt = await _cli_loop(bridge).send_now("late-msg")
        gate.set()  # 放行，run1 收尾（若 send_now → 残留回投；若 queued → 已排队）。
        await task
        return receipt.delivery

    delivery = await asyncio.wait_for(_scenario(), timeout=10.0)

    # 两种合法结局：send_now（残留回投）或 queued（直接排队）。
    assert delivery in (SendDelivery.SEND_NOW, SendDelivery.QUEUED)
    # 无论哪种，late-msg 最终都获得独立新 run：stub 被再次调起且某次请求含 late-msg。
    await _wait_until(lambda: llm.complete_called >= 2, timeout=5.0)
    assert any("late-msg" in _user_texts(req) for req in llm.requests)
