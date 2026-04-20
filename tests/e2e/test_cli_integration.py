"""CLI 层装配集成 e2e —— 覆盖 fix 修复后的三条新接通通道。

这些测试不启真实 CLI 进程、不发真实模型请求，而是直接验证"CLI 层选择用
:func:`context.build_session` / :class:`observability.JsonlTraceSink` /
:class:`context.InstructionLoader` 装配 :class:`NativeRuntime` 时，对应的
可观察副作用是否正确发生"。

三条通道对应 fix-report 里的三条修复：

1. session.backend=sqlite 时跨进程能恢复历史
2. JsonlTraceSink 真的落盘 JSONL
3. InstructionLoader 合成的 system prompt 真的被注入 session 第一条消息

所有场景都通过 ``stub_llm`` 避开真实 LLM；测试结束后 tmp_path 自动清理。
"""

from __future__ import annotations

import json

from config_loader.models import (
    ApprovalConfig,
    Config,
    ModelConfig,
    RunnerConfig,
    SessionConfig,
    TraceConfig,
)
from context import InstructionLoader, build_session
from executors.agent_runtime.native_runtime import NativeRuntime
from observability import JsonlTraceSink


def _build_cfg(tmp_path, *, backend: str = "memory", trace_file: str = "trace.jsonl") -> Config:
    """跟 cli/main.py 装配用的 Config 形状一致，但 store_path / trace 路径落到 tmp_path。"""
    return Config(
        model=ModelConfig(
            provider="openai_compatible",
            name="stub-model",
            base_url="http://127.0.0.1:1234",
            api_key="",
        ),
        runner=RunnerConfig(max_turns=3),
        session=SessionConfig(backend=backend, store_path=str(tmp_path / "sessions.db")),  # type: ignore[arg-type]
        trace=TraceConfig(output_path=str(tmp_path / trace_file)),
        approval=ApprovalConfig(mode="auto_allow"),
    )


# ---------------------------------------------------------------------------
# 通道 1：session_factory + build_session(config, sid) 让 sqlite 生效
# ---------------------------------------------------------------------------


async def test_cli_assembly_with_sqlite_persists_across_runtime_instances(
    stub_llm, recording_approval, tmp_path
):
    """CLI 层用 ``build_session(cfg, sid)`` 作为 session_factory 时，sqlite 的
    `跨进程恢复` 语义在 NativeRuntime 级别就成立：销毁旧 runtime、重建同
    config + 同 session_id，能读到上一轮的 history。
    """
    cfg = _build_cfg(tmp_path, backend="sqlite")

    # 第一个 runtime：跑一轮把 "remember banana" 写进 sqlite
    stub_llm.script(content="ok-first")
    runtime1 = NativeRuntime.build(
        cfg,
        approval=recording_approval,
        tools={},
        enabled_tool_names=[],
        session_factory=lambda sid: build_session(cfg, sid),
    )
    # 注意：NativeRuntime.build 默认会装一个 OpenAIResponsesProvider，测试里
    # 直接替换为 stub（和 test_native_runtime 同手法）。
    runtime1._llm = stub_llm  # type: ignore[attr-defined]

    result1 = await runtime1.run("remember banana", session_id="persist-1")
    assert result1.status == "completed"

    # db 文件真的被创建了
    db_path = tmp_path / "sessions.db"
    assert db_path.exists(), "SQLiteSession 应该创建 db 文件"
    assert db_path.stat().st_size > 0

    # 第二个 runtime：重新装配，拿同一个 session_id，历史应该还在
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

    # 第二次请求发给 LLM 的 messages 里能看到第一次的用户输入和回复
    last_request = stub_llm.calls[-1]
    contents = [m.content for m in last_request.messages if m.content]
    assert "remember banana" in contents
    assert "ok-first" in contents
    assert "what fruit?" in contents


async def test_cli_assembly_with_memory_backend_does_not_write_db(
    stub_llm, recording_approval, tmp_path
):
    """backend=memory 时不应该创建 db 文件，即便 store_path 指向一个合法位置。"""
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
    assert not db_path.exists(), "backend=memory 不应该创建 db 文件"


# ---------------------------------------------------------------------------
# 通道 2：event_sinks 包含 JsonlTraceSink 真的落盘 JSONL
# ---------------------------------------------------------------------------


async def test_cli_assembly_with_jsonl_trace_sink_writes_jsonl_per_run(
    stub_llm, recording_approval, tmp_path
):
    """CLI 层往 event_sinks 注入 JsonlTraceSink 后，一次 run 结束 JSONL 应该
    存在、每行合法 JSON、至少覆盖 run.start / run.end。
    """
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

    assert trace_path.exists(), "JsonlTraceSink 应该创建 trace 文件"
    lines = [ln for ln in trace_path.read_text(encoding="utf-8").splitlines() if ln.strip()]
    assert lines, "trace 文件不应为空"

    events = [json.loads(ln) for ln in lines]
    kinds = [e["kind"] for e in events]
    assert "run.start" in kinds
    assert "run.end" in kinds
    # 至少有 llm.request + llm.response
    assert any(k.startswith("llm.") for k in kinds)


# ---------------------------------------------------------------------------
# 通道 3：InstructionLoader 合成的 system prompt 真被注入为 session 第一条消息
# ---------------------------------------------------------------------------


async def test_cli_assembly_injects_instructions_from_loader_into_session(
    stub_llm, recording_approval, tmp_path, monkeypatch
):
    """InstructionLoader 合并 agent_spec 基础文本 + extra_files +
    KONGMING_EXTRA_INSTRUCTIONS env 之后渲染出的 system prompt，在 runner
    首次触发 ``_seed_messages`` 时会以 role=system 消息落到 session 里。
    """
    # 额外指令文件
    extra_file = tmp_path / "rules.md"
    extra_file.write_text("只用中文回答，遇到 shell 命令先解释意图。", encoding="utf-8")

    # 环境变量来源
    monkeypatch.setenv("KONGMING_EXTRA_INSTRUCTIONS", "本项目用 Python 3.11。")

    loader = InstructionLoader(extra_files=[extra_file], include_env=True)
    sources = await loader.load(agent_instructions="You are kongming agent.")
    instructions = loader.render(sources)

    # 三个来源都应该被渲染进 instructions
    assert "You are kongming agent." in instructions
    assert "只用中文回答" in instructions
    assert "本项目用 Python 3.11" in instructions
    # 每段带来源标注
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

    # 第一次 LLM 请求应该看到 role=system 且 content == instructions
    first_request = stub_llm.calls[0]
    system_msgs = [m for m in first_request.messages if m.role == "system"]
    assert len(system_msgs) == 1
    assert system_msgs[0].content == instructions


async def test_native_runtime_build_honors_explicit_instructions_param(
    stub_llm, recording_approval, tmp_path
):
    """直接验证 ``NativeRuntime.build(instructions=...)`` 覆盖默认
    "You are kongming agent."，而显式传入的 ``agent_spec`` 仍然优先。
    """
    cfg = _build_cfg(tmp_path)

    # 不传 agent_spec，走 instructions 覆盖
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
    """空字符串 / 空白 instructions 应回退到默认 "You are kongming agent."。"""
    cfg = _build_cfg(tmp_path)

    stub_llm.script(content="ok")
    runtime = NativeRuntime.build(
        cfg,
        approval=recording_approval,
        tools={},
        enabled_tool_names=[],
        instructions="   ",  # 纯空白
    )
    runtime._llm = stub_llm  # type: ignore[attr-defined]
    await runtime.run("hi", session_id="inst-fb-1")

    first_request = stub_llm.calls[0]
    system_content = next((m.content for m in first_request.messages if m.role == "system"), None)
    assert system_content == "You are kongming agent."
