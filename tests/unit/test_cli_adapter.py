"""unit：host.cli_adapter 渲染函数。

B5 / CR 报告 cr-report-20260424-202744.md。覆盖：

- ``_render_event_line`` 对每种已知 kind 产出预期单行格式
- 未知 kind 返回 None
- ``_render_reasoning`` 只在 payload.provider_metadata.reasoning_content 非空时打印
- ``_format_arguments`` 空 / 超长 / 多键分支
- ``_blocking_readline`` 可被 monkeypatch 替换
- ``CLIEventSink(verbose=False, show_reasoning=False)`` 完全静默
- ``CLIEventSink(verbose=True)`` 已知 kind → stderr 单行
- ``CLIEventSink(show_reasoning=True)`` 对 llm.response 打印 reasoning
- ``CLIAdapter.prompt_approval`` 命中 "y"/"n"/"yes" 的结果
- ``CLIAdapter.close`` 幂等
"""

from __future__ import annotations

import pytest

from core.contracts import ApprovalRequest, Event
from host.cli_adapter import (
    CLIAdapter,
    CLIEventSink,
    _format_arguments,
    _render_event_line,
    _render_reasoning,
)

# ---------------------------------------------------------------------------
# _render_event_line
# ---------------------------------------------------------------------------


def test_render_turn_start():
    line = _render_event_line(Event(kind="turn.start", run_id="r", turn=1))
    assert line == "[turn.start] turn=1"


def test_render_turn_end():
    line = _render_event_line(Event(kind="turn.end", run_id="r", turn=2))
    assert line == "[turn.end] turn=2"


def test_render_tool_call_start():
    line = _render_event_line(
        Event(
            kind="tool.call.start",
            run_id="r",
            turn=3,
            payload={"tool_name": "read_file", "call_id": "c-7"},
        )
    )
    assert line == "[tool.call.start] turn=3 tool=read_file call_id=c-7"


def test_render_tool_call_end_with_reason():
    line = _render_event_line(
        Event(
            kind="tool.call.end",
            run_id="r",
            turn=3,
            payload={"tool_name": "run_shell", "ok": False, "reason": "denied"},
        )
    )
    assert line == "[tool.call.end] turn=3 tool=run_shell ok=False reason=denied"


def test_render_tool_call_end_without_reason():
    line = _render_event_line(
        Event(kind="tool.call.end", run_id="r", turn=3, payload={"tool_name": "x", "ok": True})
    )
    assert line == "[tool.call.end] turn=3 tool=x ok=True"


def test_render_approval_request():
    line = _render_event_line(
        Event(kind="approval.request", run_id="r", turn=1, payload={"tool_name": "shell"})
    )
    assert line == "[approval.request] turn=1 tool=shell"


def test_render_approval_decision():
    line = _render_event_line(
        Event(
            kind="approval.decision",
            run_id="r",
            turn=1,
            payload={"tool_name": "shell", "outcome": "approved"},
        )
    )
    assert line == "[approval.decision] turn=1 tool=shell outcome=approved"


def test_render_error():
    line = _render_event_line(
        Event(
            kind="error",
            run_id="r",
            turn=2,
            payload={"type": "ProviderError", "message": "boom"},
        )
    )
    assert line == "[error] turn=2 type=ProviderError msg=boom"


def test_render_unknown_kind_returns_none():
    """llm.request / llm.response / 未知 kind 不在白名单 → None。"""
    assert _render_event_line(Event(kind="llm.request", run_id="r")) is None
    assert _render_event_line(Event(kind="custom.kind", run_id="r")) is None


# ---------------------------------------------------------------------------
# _render_reasoning
# ---------------------------------------------------------------------------


def test_render_reasoning_silent_when_no_content(capsys):
    _render_reasoning(
        Event(kind="llm.response", run_id="r", turn=1, payload={"provider_metadata": {}})
    )
    out = capsys.readouterr()
    assert out.out == ""


def test_render_reasoning_prints_content(capsys):
    _render_reasoning(
        Event(
            kind="llm.response",
            run_id="r",
            turn=1,
            payload={"provider_metadata": {"reasoning_content": "I think..."}},
        )
    )
    out = capsys.readouterr().out
    assert "[thinking turn=1]" in out
    assert "I think..." in out


def test_render_reasoning_marks_truncated(capsys):
    _render_reasoning(
        Event(
            kind="llm.response",
            run_id="r",
            turn=1,
            payload={
                "provider_metadata": {
                    "reasoning_content": "first-part",
                    "reasoning_content_length": 500,
                }
            },
        )
    )
    out = capsys.readouterr().out
    assert "截断" in out
    assert "500" in out


# ---------------------------------------------------------------------------
# _format_arguments
# ---------------------------------------------------------------------------


def test_format_arguments_empty():
    assert _format_arguments({}) == "{}"


def test_format_arguments_short():
    s = _format_arguments({"a": 1, "b": "x"})
    assert s.startswith("{") and s.endswith("}")
    assert "a=1" in s
    assert "b='x'" in s


def test_format_arguments_truncates_long_value():
    long = "x" * 200
    s = _format_arguments({"k": long})
    assert "..." in s
    # 保留 k= 前缀 + ~80 字符截断
    assert len(s) < 120


# ---------------------------------------------------------------------------
# CLIEventSink
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_cli_event_sink_silent_when_verbose_false(capsys):
    sink = CLIEventSink(verbose=False, show_reasoning=False)
    await sink.emit(Event(kind="turn.start", run_id="r", turn=1))
    out = capsys.readouterr()
    assert out.out == ""
    assert out.err == ""


@pytest.mark.asyncio
async def test_cli_event_sink_verbose_writes_stderr(capsys):
    sink = CLIEventSink(verbose=True)
    await sink.emit(Event(kind="turn.start", run_id="r", turn=1))
    out = capsys.readouterr()
    assert "[turn.start] turn=1" in out.err
    # verbose 时走 stderr，不走 stdout
    assert out.out == ""


@pytest.mark.asyncio
async def test_cli_event_sink_show_reasoning_only(capsys):
    sink = CLIEventSink(verbose=False, show_reasoning=True)
    await sink.emit(
        Event(
            kind="llm.response",
            run_id="r",
            turn=1,
            payload={"provider_metadata": {"reasoning_content": "思考..."}},
        )
    )
    out = capsys.readouterr()
    assert "[thinking turn=1]" in out.out
    assert "思考..." in out.out


@pytest.mark.asyncio
async def test_cli_event_sink_verbose_silently_ignores_unknown_kind(capsys):
    sink = CLIEventSink(verbose=True)
    await sink.emit(Event(kind="llm.request", run_id="r", turn=1))  # 非白名单
    out = capsys.readouterr()
    assert out.err == ""


# ---------------------------------------------------------------------------
# CLIAdapter.prompt_approval（monkeypatch _blocking_readline）
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_prompt_approval_yes(monkeypatch, capsys):
    def fake_readline(prompt_text: str) -> str:
        return "y"

    monkeypatch.setattr("host.cli_adapter._blocking_readline", fake_readline)
    adapter = CLIAdapter()
    decision = await adapter.prompt_approval(
        ApprovalRequest(
            run_id="r",
            session_id="s",
            turn=1,
            call_id="c",
            tool_name="shell",
            arguments={"cmd": "ls"},
        )
    )
    assert decision is True


@pytest.mark.asyncio
async def test_prompt_approval_no(monkeypatch):
    monkeypatch.setattr("host.cli_adapter._blocking_readline", lambda _: "n")
    adapter = CLIAdapter()
    decision = await adapter.prompt_approval(
        ApprovalRequest(
            run_id="r",
            session_id="s",
            turn=1,
            call_id="c",
            tool_name="shell",
        )
    )
    assert decision is False


@pytest.mark.asyncio
async def test_prompt_approval_empty_is_default_no(monkeypatch):
    monkeypatch.setattr("host.cli_adapter._blocking_readline", lambda _: "")
    adapter = CLIAdapter()
    decision = await adapter.prompt_approval(
        ApprovalRequest(run_id="r", session_id="s", turn=1, call_id="c", tool_name="x")
    )
    assert decision is False


@pytest.mark.asyncio
async def test_prompt_approval_keyboard_interrupt_returns_false(monkeypatch):
    def _raise(*_a, **_k):
        raise KeyboardInterrupt

    monkeypatch.setattr("host.cli_adapter._blocking_readline", _raise)
    adapter = CLIAdapter()
    decision = await adapter.prompt_approval(
        ApprovalRequest(run_id="r", session_id="s", turn=1, call_id="c", tool_name="x")
    )
    assert decision is False


# ---------------------------------------------------------------------------
# CLIAdapter.close 幂等
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_close_idempotent():
    adapter = CLIAdapter()
    await adapter.close()
    await adapter.close()  # 多次调用不报错
