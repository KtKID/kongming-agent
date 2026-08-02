"""unit：host.cli_adapter 渲染函数。

B5 / CR 报告 cr-report-20260424-202744.md。覆盖：

- ``_render_event_line`` 对每种已知 kind 产出预期单行格式
- 未知 kind 返回 None
- ``_render_reasoning`` 只在 payload.provider_metadata.reasoning_content 非空时打印
- ``_format_arguments`` 空 / 超长 / 多键分支
- ``CLIEventSink(verbose=False, show_reasoning=False)`` 完全静默
- ``CLIEventSink(verbose=True)`` 已知 kind → stderr 单行
- ``CLIEventSink(show_reasoning=True)`` 对 llm.response 打印 reasoning
- ``CLIAdapter.close`` 幂等
"""

from __future__ import annotations

import pytest

from core.contracts import Event
from core.errors import ProviderError
from core.message import Message
from core.result import Result
from hosts.cli.adapter import (
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
# CLIAdapter.close 幂等
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_close_idempotent():
    adapter = CLIAdapter()
    await adapter.close()
    await adapter.close()  # 多次调用不报错


# ---------------------------------------------------------------------------
# v0.3 cron-delivery M5：pre_prompt_hook 钩子在 prompt 之前调
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_pre_prompt_hook_called_before_prompt(monkeypatch):
    """v0.3 M5：read_input 在 prompt_async 之前 await 注入的 hook。

    典型场景：CliDeliverySink.drain_pending → click.echo flush cron 投递 buffer。
    """
    call_log: list[str] = []

    async def hook() -> None:
        call_log.append("hook")

    class _FakePromptSession:
        async def prompt_async(self, prompt: str) -> str:
            call_log.append(f"prompt({prompt!r})")
            return "user-input"

    adapter = CLIAdapter(pre_prompt_hook=hook)
    monkeypatch.setattr(adapter, "_ensure_session", lambda: _FakePromptSession())

    text = await adapter.read_input()

    assert text == "user-input"
    # hook 在 prompt_async 之前被调
    assert call_log == ["hook", "prompt('kongming > ')"]


@pytest.mark.asyncio
async def test_pre_prompt_hook_exception_does_not_block_prompt(monkeypatch, caplog):
    """v0.3 M5（R2 fix P1-2）：hook 抛异常不阻塞 prompt；
    用 logger.warning 留诊断线索（exc_info=True）。"""
    import logging

    async def bad_hook() -> None:
        raise RuntimeError("hook boom")

    class _FakePromptSession:
        async def prompt_async(self, prompt: str) -> str:
            return "still-works"

    adapter = CLIAdapter(pre_prompt_hook=bad_hook)
    monkeypatch.setattr(adapter, "_ensure_session", lambda: _FakePromptSession())

    with caplog.at_level(logging.WARNING):
        text = await adapter.read_input()

    # hook 抛错不阻塞：用户仍能输入
    assert text == "still-works"
    # 但 logger.warning 记下来：避免 silent loss
    assert any("pre_prompt_hook" in record.getMessage() for record in caplog.records), (
        f"expected pre_prompt_hook warning in logs, got {[r.getMessage() for r in caplog.records]}"
    )


@pytest.mark.asyncio
async def test_pre_prompt_hook_none_skips_call(monkeypatch):
    """pre_prompt_hook=None 时 read_input 直接进 prompt_async，不调任何 hook。"""
    call_log: list[str] = []

    class _FakePromptSession:
        async def prompt_async(self, prompt: str) -> str:
            call_log.append(prompt)
            return "ok"

    adapter = CLIAdapter()  # 默认 pre_prompt_hook=None
    monkeypatch.setattr(adapter, "_ensure_session", lambda: _FakePromptSession())

    text = await adapter.read_input()

    assert text == "ok"
    assert call_log == ["kongming > "]


# ---------------------------------------------------------------------------
# CLIAdapter.render_result（cli-bridge-render-result-debt #6）
#
# render_result 三段渲染:content（受 streaming 门控）/ error（永远打）/ token
# （永远打）。决策点全在 adapter 内部。下面 6 个用例覆盖
# streaming × {有/无 content} × {有/无 error} × {有/无 usage} 的
# 关键组合，保留 token/error 渲染语义。
# ---------------------------------------------------------------------------


def _usage_result(content: str | None = "hello", *, error: ProviderError | None = None) -> Result:
    """带 usage + content 的 Result 工厂（streaming 用例的通用底料）。"""
    from core.contracts import ProviderUsageFamily, ProviderUsageScope
    from infrastructure.llm_providers.usage import ProviderUsageManager

    usage = ProviderUsageManager().normalize(
        family=ProviderUsageFamily.OPENAI_CHAT_COMPLETIONS,
        raw_usage={"prompt_tokens": 10, "completion_tokens": 20, "total_tokens": 30},
        scope=ProviderUsageScope.RUN,
    )
    return Result(
        run_id="r-1",
        session_id="sid-1",
        status="failed" if error is not None else "completed",
        final_message=Message.assistant(content=content) if content is not None else None,
        turn_count=1,
        error=error,
        metadata={"usage": usage.to_payload()},
    )


@pytest.mark.asyncio
async def test_render_result_non_streaming_prints_content_and_tokens(capsys):
    """streaming=False + 完整 Result：打 content + token，无 error 行。"""
    adapter = CLIAdapter(streaming=False)  # 显式非流式(CLIAdapter 默认 streaming=True)
    await adapter.render_result(_usage_result("hello"))

    out = capsys.readouterr().out
    assert "hello" in out  # content
    assert "[tokens ↑10 ↓20 =30]" in out  # token 汇总
    assert "[error]" not in out  # 无 error → 不打 error 行


@pytest.mark.asyncio
async def test_render_result_non_streaming_with_error_prints_error_line(capsys):
    """streaming=False + 含 error：打 content + [error] 行。"""
    adapter = CLIAdapter(streaming=False)  # 显式非流式
    await adapter.render_result(_usage_result("oops", error=ProviderError("boom")))

    out = capsys.readouterr().out
    assert "oops" in out  # content 仍打
    assert "[error] ProviderError: boom" in out


@pytest.mark.asyncio
async def test_render_result_streaming_skips_content_keeps_tokens(capsys):
    """streaming=True + 完整 Result：跳过 content（CLIStreamSink 已打），只打 token。"""
    adapter = CLIAdapter(streaming=True)
    await adapter.render_result(_usage_result("hello"))

    out = capsys.readouterr().out
    assert "hello" not in out  # streaming → 不二次打印 content
    assert "[tokens ↑10 ↓20 =30]" in out  # token 仍打


@pytest.mark.asyncio
async def test_render_result_streaming_with_error_prints_error_and_tokens(capsys):
    """streaming=True + 含 error：打 [error] 行 + token（error 不走流式通道）。"""
    adapter = CLIAdapter(streaming=True)
    await adapter.render_result(_usage_result("ignored", error=ProviderError("boom")))

    out = capsys.readouterr().out
    assert "ignored" not in out  # streaming → 跳过 content
    assert "[error] ProviderError: boom" in out
    assert "[tokens ↑10 ↓20 =30]" in out


@pytest.mark.asyncio
async def test_render_result_handles_none_final_message(capsys):
    """final_message=None（status=failed 典型）不崩，只打 token（若有 usage）。"""
    adapter = CLIAdapter(
        streaming=False
    )  # 显式非流式(content=None 时 streaming 与否都跳过,显式传保持用例意图清晰)
    await adapter.render_result(_usage_result(content=None))

    out = capsys.readouterr().out
    # 无 content 无 error → 只有 token 行
    assert "[tokens ↑10 ↓20 =30]" in out
    assert "[error]" not in out


@pytest.mark.asyncio
async def test_render_result_skips_token_line_when_no_usage(capsys):
    """metadata 无 usage → 不打 token 行（content 仍按 streaming 门控）。"""
    adapter = CLIAdapter(
        streaming=False
    )  # 显式非流式:本用例断言 content "hello" 被打印,需 streaming=False
    result = Result(
        run_id="r-1",
        session_id="sid-1",
        status="completed",
        final_message=Message.assistant(content="hello"),
        turn_count=1,
    )  # metadata 默认 {} → 无 usage
    await adapter.render_result(result)

    out = capsys.readouterr().out
    assert "hello" in out
    assert "[tokens" not in out  # 无 usage → 不打 token 行
