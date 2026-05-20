"""unit：host.session_bridge 覆盖。

B2 / CR 报告 cr-report-20260424-202744.md。覆盖：

- ``__init__`` 空 session_id 报错
- ``run_once`` happy path：final_message.content → adapter.write_output
- ``run_once`` result.error 非空 → 额外 write_output `[error] ...`
- ``run_once`` metadata.usage 存在 → 额外 write_output `[tokens ...]`
- ``run_once`` final_message=None 不崩溃
- ``run_loop`` 读到 None → 正常退出并 close adapter
- ``run_loop`` read_input 抛 KeyboardInterrupt → 干净退出
- ``run_loop`` read_input 抛 EOFError → 干净退出
- ``run_loop`` 空串跳过，不调 runtime.run
- ``run_loop`` run_once 抛 KeyboardInterrupt → 写 [interrupted] 并继续
- ``run_loop`` finally 保证 adapter.close 被调用
"""

from __future__ import annotations

from typing import Any

import pytest

from commands.models import CommandResult
from core.errors import ProviderError
from core.message import Message
from core.result import Result
from host.base import HostAdapter
from host.session_bridge import SessionBridge


class _StubAdapter(HostAdapter):
    """可控的 HostAdapter 测试替身。"""

    def __init__(
        self,
        *,
        inputs: list[str | None] | None = None,
        read_error: Exception | None = None,
    ) -> None:
        self._inputs = list(inputs or [])
        self._read_error = read_error
        self.outputs: list[str] = []
        self.closed: int = 0

    async def read_input(self) -> str | None:
        if self._read_error is not None:
            err = self._read_error
            self._read_error = None
            raise err
        if not self._inputs:
            return None
        return self._inputs.pop(0)

    async def write_output(self, text: str) -> None:
        self.outputs.append(text)

    async def close(self) -> None:
        self.closed += 1


class _StubRuntime:
    """只实现 run() 的 NativeRuntime 替身。"""

    def __init__(
        self,
        *,
        result_factory=None,
        run_error: Exception | None = None,
    ) -> None:
        self._factory = result_factory or (lambda sid, user_input: _ok_result(sid))
        self._run_error = run_error
        self.calls: list[tuple[str, str]] = []

    async def run(
        self,
        user_input: str,
        *,
        session_id: str | None = None,
        reasoning_effort: str | None = None,
        attachments: list[dict[str, Any]] | None = None,
    ) -> Result:
        self.calls.append((user_input, session_id or ""))
        if self._run_error is not None:
            err = self._run_error
            self._run_error = None
            raise err
        return self._factory(session_id, user_input)


def _ok_result(sid: str | None, content: str = "hi", **extra: Any) -> Result:
    return Result(
        run_id="r-1",
        session_id=sid or "",
        status="completed",
        final_message=Message.assistant(content=content),
        turn_count=1,
        metadata=extra.get("metadata", {}),
    )


def _bridge(runtime, adapter, sid: str = "sid-1") -> SessionBridge:
    # 通过 typing.cast 式的 duck 注入，绕开 NativeRuntime 强类型。
    return SessionBridge(runtime=runtime, adapter=adapter, session_id=sid)  # type: ignore[arg-type]


class _StubCommandService:
    def __init__(self, result: CommandResult) -> None:
        self._result = result
        self.calls: list[str] = []

    async def handle_input(
        self,
        raw_input: str,
        *,
        context: Any,
        attachments: list[dict[str, Any]] | None = None,
    ) -> CommandResult:
        self.calls.append(raw_input)
        assert context.session_id == "sid-1"
        return self._result


# ---------------------------------------------------------------------------
# 构造约束
# ---------------------------------------------------------------------------


def test_init_rejects_empty_session_id():
    with pytest.raises(ValueError, match="session_id must not be empty"):
        SessionBridge(runtime=_StubRuntime(), adapter=_StubAdapter(), session_id="")  # type: ignore[arg-type]


def test_properties_exposed():
    rt, ad = _StubRuntime(), _StubAdapter()
    b = _bridge(rt, ad, sid="sid-x")
    assert b.session_id == "sid-x"
    assert b.runtime is rt
    assert b.adapter is ad


# ---------------------------------------------------------------------------
# run_once
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_run_once_writes_final_content():
    rt, ad = _StubRuntime(), _StubAdapter()
    b = _bridge(rt, ad)
    result = await b.run_once("hello")
    assert result.status == "completed"
    assert "hi" in ad.outputs
    # 无 error / 无 usage → 只有一条输出
    assert len(ad.outputs) == 1


@pytest.mark.asyncio
async def test_run_once_writes_error_line():
    def factory(sid, ui):
        return Result(
            run_id="r",
            session_id=sid or "",
            status="failed",
            final_message=None,
            turn_count=0,
            error=ProviderError("boom"),
        )

    rt, ad = _StubRuntime(result_factory=factory), _StubAdapter()
    b = _bridge(rt, ad)
    await b.run_once("hello")
    assert any(out.startswith("[error] ProviderError: boom") for out in ad.outputs)


@pytest.mark.asyncio
async def test_run_once_writes_usage_line():
    def factory(sid, ui):
        return _ok_result(
            sid,
            metadata={"usage": {"prompt_tokens": 10, "completion_tokens": 20, "total_tokens": 30}},
        )

    rt, ad = _StubRuntime(result_factory=factory), _StubAdapter()
    b = _bridge(rt, ad)
    await b.run_once("hello")
    assert any("[tokens ↑10 ↓20 =30]" in out for out in ad.outputs)


@pytest.mark.asyncio
async def test_run_once_skips_usage_line_when_empty():
    def factory(sid, ui):
        return _ok_result(sid, metadata={"usage": {}})

    rt, ad = _StubRuntime(result_factory=factory), _StubAdapter()
    b = _bridge(rt, ad)
    await b.run_once("hello")
    assert not any("[tokens" in out for out in ad.outputs)


@pytest.mark.asyncio
async def test_run_once_handles_no_final_message():
    def factory(sid, ui):
        return Result(
            run_id="r",
            session_id=sid or "",
            status="failed",
            final_message=None,
            turn_count=0,
        )

    rt, ad = _StubRuntime(result_factory=factory), _StubAdapter()
    b = _bridge(rt, ad)
    result = await b.run_once("hello")
    # 无 final content / 无 error / 无 usage → adapter 无输出
    assert result.final_message is None
    assert ad.outputs == []


@pytest.mark.asyncio
async def test_run_once_handles_final_empty_content():
    """final_message.content 为 None 或空串时不写输出。"""

    def factory(sid, ui):
        return Result(
            run_id="r",
            session_id=sid or "",
            status="completed",
            final_message=Message.assistant(content=""),
            turn_count=1,
        )

    rt, ad = _StubRuntime(result_factory=factory), _StubAdapter()
    b = _bridge(rt, ad)
    await b.run_once("hi")
    assert ad.outputs == []


@pytest.mark.asyncio
async def test_run_once_passes_session_id_to_runtime():
    rt, ad = _StubRuntime(), _StubAdapter()
    b = _bridge(rt, ad, sid="sid-42")
    await b.run_once("x")
    assert rt.calls == [("x", "sid-42")]


@pytest.mark.asyncio
async def test_run_once_routes_command_result_without_runtime():
    rt, ad = _StubRuntime(), _StubAdapter()
    cmd_service = _StubCommandService(
        CommandResult(
            status="completed",
            command_name="/review",
            output_text="/review completed.",
            invocation_id="cmd-1",
        )
    )
    b = SessionBridge(
        runtime=rt,  # type: ignore[arg-type]
        adapter=ad,
        session_id="sid-1",
        command_service=cmd_service,  # type: ignore[arg-type]
    )

    result = await b.run_once("/review")

    assert isinstance(result, CommandResult)
    assert rt.calls == []
    assert ad.outputs == ["/review completed."]


# ---------------------------------------------------------------------------
# run_loop
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_run_loop_exits_on_none_input():
    rt, ad = _StubRuntime(), _StubAdapter(inputs=["hello", None])
    b = _bridge(rt, ad)
    await b.run_loop()
    assert rt.calls == [("hello", "sid-1")]
    assert ad.closed == 1


@pytest.mark.asyncio
async def test_run_loop_exits_on_read_keyboard_interrupt():
    rt = _StubRuntime()
    ad = _StubAdapter(read_error=KeyboardInterrupt())
    b = _bridge(rt, ad)
    await b.run_loop()
    assert rt.calls == []
    assert ad.closed == 1


@pytest.mark.asyncio
async def test_run_loop_exits_on_read_eof():
    rt = _StubRuntime()
    ad = _StubAdapter(read_error=EOFError())
    b = _bridge(rt, ad)
    await b.run_loop()
    assert rt.calls == []
    assert ad.closed == 1


@pytest.mark.asyncio
async def test_run_loop_skips_blank_input():
    rt, ad = _StubRuntime(), _StubAdapter(inputs=["", "   ", "\t\n", "hi", None])
    b = _bridge(rt, ad)
    await b.run_loop()
    assert rt.calls == [("hi", "sid-1")]


@pytest.mark.asyncio
async def test_run_loop_recovers_from_keyboard_interrupt_during_run():
    rt = _StubRuntime(run_error=KeyboardInterrupt())
    ad = _StubAdapter(inputs=["first", "second", None])
    b = _bridge(rt, ad)
    await b.run_loop()
    # 第一轮 run_once 抛 KeyboardInterrupt → 写 [interrupted]，循环继续到 second
    assert "[interrupted]" in ad.outputs
    assert ("second", "sid-1") in rt.calls
    assert ad.closed == 1


@pytest.mark.asyncio
async def test_run_loop_closes_adapter_even_on_exception():
    """任意非预期异常仍会走 finally 关 adapter。"""

    class _BadAdapter(_StubAdapter):
        async def read_input(self) -> str | None:
            raise RuntimeError("unexpected")

    rt, ad = _StubRuntime(), _BadAdapter()
    b = _bridge(rt, ad)
    with pytest.raises(RuntimeError, match="unexpected"):
        await b.run_loop()
    assert ad.closed == 1
