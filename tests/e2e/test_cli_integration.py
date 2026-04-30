"""CLI assembly integration tests."""

from __future__ import annotations

import hashlib
import json
import time
from pathlib import Path

import cli.main as cli_main
from config_loader.models import (
    ApprovalConfig,
    Config,
    ModelConfig,
    RunnerConfig,
    SessionConfig,
    TraceConfig,
)
from context import InstructionLoader, build_session
from core.message import Message
from executors.agent_runtime.native_runtime import NativeRuntime
from observability import JsonlTraceSink, PromptDebugDumpSink

_REPO_ROOT = Path(__file__).resolve().parents[2]
# 测试 dump 与 CLI ``--debug`` 生产落盘 (.kongming/debug/prompt-debug-*.json)
# 物理分开，避免人工查看 debug 时被测试垃圾干扰。
_DUMP_DIR = _REPO_ROOT / ".kongming/debug/tests"


def _build_cfg(
    tmp_path,
    *,
    backend: str = "memory",
    trace_file: str = "trace.jsonl",
    file_store_path: str | None = None,
) -> Config:
    """Build a config shaped like the CLI path, with storage under tmp_path."""
    return Config(
        model=ModelConfig(
            provider="openai_compatible",
            name="stub-model",
            base_url="http://127.0.0.1:1234",
            api_key="",
        ),
        runner=RunnerConfig(max_turns=3),
        session=SessionConfig(
            backend=backend,
            store_path=str(tmp_path / "sessions.db"),
            file_store_path=file_store_path or str(tmp_path / "sessions"),
        ),
        trace=TraceConfig(output_path=str(tmp_path / trace_file)),
        approval=ApprovalConfig(mode="auto_allow"),
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


async def test_cli_assembly_with_sqlite_persists_across_runtime_instances(
    stub_llm, recording_approval, tmp_path
):
    """CLI-style session_factory should preserve sqlite history across runtime instances."""
    cfg = _build_cfg(tmp_path, backend="sqlite")

    stub_llm.script(content="ok-first")
    runtime1 = NativeRuntime.build(
        cfg,
        approval=recording_approval,
        tools={},
        enabled_tool_names=[],
        session_factory=lambda sid: build_session(cfg, sid),
    )
    runtime1._llm = stub_llm  # type: ignore[attr-defined]

    result1 = await runtime1.run("remember banana", session_id="persist-1")
    assert result1.status == "completed"

    db_path = tmp_path / "sessions.db"
    assert db_path.exists(), "SQLiteSession 应创建 db 文件"
    assert db_path.stat().st_size > 0

    stub_llm.script(content="ok-second")
    runtime2 = NativeRuntime.build(
        cfg,
        approval=recording_approval,
        tools={},
        enabled_tool_names=[],
        session_factory=lambda sid: build_session(cfg, sid),
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
    runtime = NativeRuntime.build(
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
    runtime = NativeRuntime.build(
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
    runtime = NativeRuntime.build(
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
    """NativeRuntime.build(instructions=...) should override the default system text."""
    cfg = _build_cfg(tmp_path)

    stub_llm.script(content="ok")
    runtime = NativeRuntime.build(
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
    runtime = NativeRuntime.build(
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
        "agent_spec",
        "file:rules.md",
        "env:KONGMING_EXTRA_INSTRUCTIONS",
        "memory",
    ]
    assert "# runtime" in rendered_prompt
    assert "# agent_spec" in rendered_prompt
    assert "# file:rules.md" in rendered_prompt
    assert "# env:KONGMING_EXTRA_INSTRUCTIONS" in rendered_prompt
    assert "# memory" in rendered_prompt
    assert "FILE-RULE: answer in Chinese." in rendered_prompt
    assert "ENV-RULE: prefer concise answers." in rendered_prompt
    assert "MEMORY-RULE: remember project prompt flow." in rendered_prompt

    cfg = _build_cfg(tmp_path, backend="memory")
    runtime = NativeRuntime.build(
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
    """NativeRuntime debug sink 应保存真实 Runner turn 的 prompt 快照。"""
    debug_dir = tmp_path / "debug"
    cfg = _build_cfg(tmp_path, backend="memory")
    runtime = NativeRuntime.build(
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
    from context.instruction_loader import InstructionSource

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
            # 与真实 NativeRuntime.aclose 签名保持一致：CLI 退出路径 await 该方法。
            return None

    class _DummyBridge:
        def __init__(
            self,
            *,
            runtime,
            adapter,
            session_id: str,
            echo_final_content: bool = True,
        ) -> None:
            del adapter, echo_final_content
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
    monkeypatch.setattr(cli_main.NativeRuntime, "build", staticmethod(_fake_build))
    monkeypatch.setattr(cli_main, "SessionBridge", _DummyBridge)
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

    assert manifest_path.exists(), "CLI file backend 应写出 manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    assert manifest["instruction_sources"] == expected_origins
    assert manifest["instruction_text_hash"] == expected_hash


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

    monkeypatch.setattr(cli_main, "_load_config_or_exit", lambda _: cfg)

    async def _fake_assemble_instructions(_cfg, _files):
        return "system", [], None

    monkeypatch.setattr(cli_main, "_assemble_instructions", _fake_assemble_instructions)
    monkeypatch.setattr(cli_main, "build_default_registry", lambda **_: _DummyRegistry())
    monkeypatch.setattr(cli_main, "build_default_approval", lambda *_, **__: object())
    monkeypatch.setattr(
        cli_main.NativeRuntime,
        "build",
        staticmethod(lambda *_, **kwargs: _FakeRuntime(session_factory=kwargs["session_factory"])),
    )
    monkeypatch.setattr(cli_main, "SessionBridge", _DummyBridge)
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
