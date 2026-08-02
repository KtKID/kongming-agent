"""runner + MessageCompactor 主循环接入 e2e (Finding 3b 修复验证)。

这些测试验证三件事：

1. runner 构造时不传 ``message_compactor`` 的语义（原样透传 history）没被改变
2. runner 构造时传入符合 ``core.contracts.MessageCompactor`` Protocol 的对象
   后，每 turn 发给 LLM 的 messages 真的是 compactor 加工后的结果
3. SessionEngine.build 默认注入 prompting.HistoryCompactor，用户什么都不传也能
   防止长对话撞 context 上限

辅助组件用结构化鸭子类型满足 Protocol，避免触碰真实 OpenAI provider。
"""

from __future__ import annotations

from collections.abc import Sequence

from core import Runner
from core.agent_spec import AgentSpec
from core.message import Message
from core.session import InMemorySession
from infrastructure.config.models import (
    ApprovalConfig,
    Config,
    ModelSelectionConfig,
    RunnerConfig,
    SessionConfig,
    TraceConfig,
)
from prompting import HistoryCompactor
from runtime_assembly.session_engine import SessionEngine


class _CountingCompactor:
    """计数 + 截断到最近 N 条的 MessageCompactor。

    结构上满足 ``core.contracts.MessageCompactor`` Protocol。
    """

    def __init__(self, keep_recent: int = 2) -> None:
        self._keep = keep_recent
        self.calls: int = 0
        self.last_seen: int = 0

    async def compact(self, history: Sequence[Message]) -> list[Message]:
        self.calls += 1
        self.last_seen = len(history)
        if len(history) <= self._keep:
            return list(history)
        return list(history[-self._keep :])


class _FailingCompactor:
    """总是抛异常的 compactor，用来验证 runner 的 fallback。"""

    async def compact(self, history: Sequence[Message]) -> list[Message]:
        raise RuntimeError("compactor blew up")


def _build_stub_cfg(tmp_path) -> Config:
    return Config(
        model=ModelSelectionConfig(preset_id="local-gemma-4-e4b-it"),
        runner=RunnerConfig(max_turns=3),
        session=SessionConfig(backend="memory", store_path=str(tmp_path / "sessions.db")),
        trace=TraceConfig(output_path=str(tmp_path / "trace.jsonl")),
        approval=ApprovalConfig(mode="auto_allow"),
    )


# ---------------------------------------------------------------------------
# Runner 层：compactor 注入 & 不注入 & 失败 fallback
# ---------------------------------------------------------------------------


async def test_runner_without_compactor_passes_history_as_is(
    stub_llm, recording_approval, memory_sink
):
    """不注入 compactor 时，runner 原样把 history 塞给 LLM（回归守卫）。"""
    stub_llm.script(content="ok")
    runner = Runner(event_sinks=[memory_sink])

    session = InMemorySession(session_id="no-compact")
    # 预先塞一段"很长"的 history
    for i in range(10):
        await session.append(Message.user(f"u{i}"))

    spec = AgentSpec(
        name="t",
        instructions="",  # 避免 _seed_messages 额外插 system
        default_model="stub",
        tool_names=(),
        max_turns=2,
    )
    await runner.run(
        "trigger",
        session=session,
        agent_spec=spec,
        llm=stub_llm,
        tools={},
        approval=recording_approval,
    )

    # LLM 应该看到全部 11 条（10 预塞 + 1 本次 user）
    first_request = stub_llm.calls[0]
    assert len(first_request.messages) == 11
    # 不 emit history.compact 事件
    assert "history.compact" not in memory_sink.kinds()


async def test_runner_with_compactor_applies_compaction_and_emits_event(
    stub_llm, recording_approval, memory_sink
):
    """注入 compactor 后，runner 每 turn 都调用它；真正压缩时 emit 事件。"""
    compactor = _CountingCompactor(keep_recent=3)
    stub_llm.script(content="done")
    runner = Runner(event_sinks=[memory_sink], message_compactor=compactor)

    session = InMemorySession(session_id="compact")
    for i in range(10):
        await session.append(Message.user(f"u{i}"))

    spec = AgentSpec(
        name="t",
        instructions="",
        default_model="stub",
        tool_names=(),
        max_turns=2,
    )
    await runner.run(
        "trigger",
        session=session,
        agent_spec=spec,
        llm=stub_llm,
        tools={},
        approval=recording_approval,
    )

    # compactor 被调了至少 1 次（每 turn 调一次，这里是 1 turn）
    assert compactor.calls >= 1
    # LLM 看到的 messages 数量 == compactor 返回的数量
    first_request = stub_llm.calls[0]
    assert len(first_request.messages) == 3  # keep_recent=3

    # 发生了真实压缩 → emit history.compact
    compact_events = memory_sink.of_kind("history.compact")
    assert len(compact_events) >= 1
    payload = compact_events[0].payload
    assert payload["original_count"] == 11  # 10 预塞 + 1 本次 user
    assert payload["compacted_count"] == 3
    assert payload["dropped_count"] == 8


async def test_runner_with_compactor_does_not_emit_event_when_no_change(
    stub_llm, recording_approval, memory_sink
):
    """history 长度未超阈值，compactor 原样返回，不该 emit history.compact。"""
    compactor = _CountingCompactor(keep_recent=100)  # 阈值远大于真实 history
    stub_llm.script(content="ok")
    runner = Runner(event_sinks=[memory_sink], message_compactor=compactor)

    session = InMemorySession(session_id="nochange")
    spec = AgentSpec(
        name="t",
        instructions="",
        default_model="stub",
        tool_names=(),
        max_turns=2,
    )
    await runner.run(
        "hi",
        session=session,
        agent_spec=spec,
        llm=stub_llm,
        tools={},
        approval=recording_approval,
    )

    # compactor 被调了（说明确实过了这一层）
    assert compactor.calls >= 1
    # 但 len 没变 → 不 emit history.compact
    assert "history.compact" not in memory_sink.kinds()


async def test_runner_compactor_failure_falls_back_to_raw_history(
    stub_llm, recording_approval, memory_sink
):
    """compactor 抛异常时 runner 继续用原始 history，并 emit error 事件。"""
    failing = _FailingCompactor()
    stub_llm.script(content="still-ok")
    runner = Runner(event_sinks=[memory_sink], message_compactor=failing)

    session = InMemorySession(session_id="fail")
    await session.append(Message.user("prelude"))
    spec = AgentSpec(
        name="t",
        instructions="",
        default_model="stub",
        tool_names=(),
        max_turns=2,
    )
    result = await runner.run(
        "go",
        session=session,
        agent_spec=spec,
        llm=stub_llm,
        tools={},
        approval=recording_approval,
    )

    # 主链路没被污染
    assert result.status == "completed"
    # LLM 看到的是原始未裁剪 history（prelude + go = 2 条）
    first_request = stub_llm.calls[0]
    assert len(first_request.messages) == 2
    # 错误事件被 emit
    error_events = memory_sink.of_kind("error")
    assert any(e.payload.get("source") == "message_compactor" for e in error_events)


# ---------------------------------------------------------------------------
# SessionEngine 层：默认注入 + 显式覆盖
# ---------------------------------------------------------------------------


async def test_native_runtime_build_disables_compactor_by_default(
    stub_llm, recording_approval, tmp_path
):
    """SessionEngine.build 默认（cfg.compactor.enabled=False）装 NoopCompactor，
    不做 FIFO 压缩。显式设 enabled=True 才装 HistoryCompactor（见下一条测试）。

    这是 memory-module5-completion task 定下的新默认行为：现有 FIFO 压缩语义
    与 LLM summarize 式压缩差距大，默认关闭、留给后续 task compactor-v2-llm-summarize。
    """
    from runtime_assembly.session_engine import _NoopCompactor

    cfg = _build_stub_cfg(tmp_path)
    runtime = SessionEngine.build(
        cfg,
        approval=recording_approval,
        tools={},
        enabled_tool_names=[],
    )
    runtime._llm = stub_llm  # type: ignore[attr-defined]

    runner = runtime._runner  # type: ignore[attr-defined]
    assert isinstance(runner._message_compactor, _NoopCompactor)


async def test_native_runtime_build_enables_history_compactor_when_configured(
    stub_llm, recording_approval, tmp_path
):
    """cfg.compactor.enabled=True 时装 HistoryCompactor 作为兜底 FIFO 压缩。"""
    cfg = _build_stub_cfg(tmp_path)
    cfg.compactor.enabled = True  # 显式启用 FIFO 压缩
    runtime = SessionEngine.build(
        cfg,
        approval=recording_approval,
        tools={},
        enabled_tool_names=[],
    )
    runtime._llm = stub_llm  # type: ignore[attr-defined]

    runner = runtime._runner  # type: ignore[attr-defined]
    assert isinstance(runner._message_compactor, HistoryCompactor)


async def test_native_runtime_build_honors_explicit_message_compactor(
    stub_llm, recording_approval, tmp_path
):
    """显式传 message_compactor 覆盖默认。"""
    cfg = _build_stub_cfg(tmp_path)
    custom = _CountingCompactor(keep_recent=1)
    runtime = SessionEngine.build(
        cfg,
        approval=recording_approval,
        tools={},
        enabled_tool_names=[],
        message_compactor=custom,
    )
    runtime._llm = stub_llm  # type: ignore[attr-defined]

    runner = runtime._runner  # type: ignore[attr-defined]
    assert runner._message_compactor is custom

    # 跑一轮，确认 custom.calls 至少为 1
    stub_llm.script(content="done")
    await runtime.run("hi", session_id="custom-compact")
    assert custom.calls >= 1
