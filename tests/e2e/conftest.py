"""e2e 测试专用 fixture 仓库。

这里集中提供 e2e 需要的 stub / recorder 组件：

- :class:`StubLLMProvider`：脚本化的 :class:`core.contracts.LLMProvider` 实现。
  测试用 ``script(content=..., tool_calls=...)`` 排队多轮响应，由 runner 按顺序
  消费，完全不碰真实网络。
- :class:`MemoryEventSink`：把所有 Event 收集到内存里便于断言。
- :class:`RecordingApproval`：把所有 ApprovalRequest 记录下来便于断言；默认
  ``approved``，可切 ``rejected``。
- :func:`local_model_config`：构造一个指向本地模型基线
  (``http://127.0.0.1:1234`` + ``gemma-4-e4b-it`` + 空 ``api_key``) 的
  :class:`infrastructure.config.models.Config`。

所有 e2e 默认走 stub provider，测试不需要真实模型服务就能跑；真实模型路径
仅通过环境变量 ``KONGMING_E2E_REAL_MODEL=1`` 显式开启。
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Awaitable, Callable
from typing import TYPE_CHECKING

import pytest

from core.contracts import (
    ApprovalDecision,
    ApprovalOutcome,
    ApprovalRequest,
    Event,
    LLMRequest,
    LLMResponse,
    LLMStreamChunk,
)
from core.message import Message, ToolCall

# 注意：``hosts.shared`` / ``runtime_assembly`` 相关 import 一律**延迟到 fixture /
# 函数体内**（不放模块顶层）。原因：``import hosts.shared.base`` 会触发
# ``hosts/shared/__init__`` 执行，其 import 链在 collection 期就把
# ``tools.builtin.shell_tool`` 拉进 sys.modules——而同目录的
# ``test_setting_yaml_runtime_safety.py`` 在模块顶层断言 shell_tool **未**被加载
# （安全护栏：该测试只调 chain.decide，绝不执行 shell）。conftest 顶层预加载会破坏
# 那条护栏。因此这里只在实际用到时才 import，把副作用限制在需要 bridge 的用例进程段。
if TYPE_CHECKING:
    from core.result import Result
    from hosts.shared.host_dispatcher import HostDispatcher
    from runtime_assembly.session_engine import SessionEngine
from infrastructure.config.models import (
    ApprovalConfig,
    Config,
    ModelSelectionConfig,
    RunnerConfig,
)

# ---------------------------------------------------------------------------
# Stub LLM Provider
# ---------------------------------------------------------------------------


class StubLLMProvider:
    """脚本化 LLMProvider。

    用法：

        stub = StubLLMProvider()
        stub.script(content="hello")                                # 纯文本一轮
        stub.script(tool_calls=[ToolCall("c1", "read_file", {})])   # 工具调用一轮

    每次 :meth:`complete` 被调用时弹出队首一条响应。队列耗尽后默认返回一条空的
    assistant 消息以终止 runner（避免测试无限循环）。

    结构上满足 :class:`core.contracts.LLMProvider` Protocol（单个 async
    ``complete`` 方法）。
    """

    def __init__(
        self,
        responses: list[tuple[str | None, list[ToolCall] | None]] | None = None,
    ) -> None:
        self._responses: list[tuple[str | None, list[ToolCall] | None]] = list(responses or [])
        self.calls: list[LLMRequest] = []

    def script(
        self,
        content: str | None = None,
        tool_calls: list[ToolCall] | None = None,
    ) -> None:
        """排队一条将要由下一次 ``complete`` 返回的响应。"""
        self._responses.append((content, tool_calls))

    async def complete(self, request: LLMRequest) -> LLMResponse:
        self.calls.append(request)
        if not self._responses:
            # 默认终止：返回空 assistant 消息，runner 视为 stop。
            return LLMResponse(
                message=Message(role="assistant", content=""),
                finish_reason="stop",
            )
        content, tool_calls = self._responses.pop(0)
        tool_calls_tuple: tuple[ToolCall, ...] | None = tuple(tool_calls) if tool_calls else None
        msg = Message(
            role="assistant",
            content=content,
            tool_calls=tool_calls_tuple,
        )
        finish = "tool_calls" if tool_calls_tuple else "stop"
        return LLMResponse(message=msg, finish_reason=finish)


# ---------------------------------------------------------------------------
# Stub Streaming LLM Provider（v0.2 流式接入）
# ---------------------------------------------------------------------------


class StubLLMStreamProvider:
    """脚本化流式 :class:`SupportsLLMStream` 实现 + 同时满足 `LLMProvider`.

    用同一份 chunks 喂两路：
    - :meth:`stream` yield 整串 :class:`LLMStreamChunk`
    - :meth:`complete` 把 message.done chunk 的 message / finish_reason / usage /
      provider_metadata 拼成等价 :class:`LLMResponse`

    这样**同一个 stub** 可被流式与非流式双轨复用，等价性测试不需要构造两份脚本。

    用法：

        stub = StubLLMStreamProvider()
        stub.script_chunks([
            LLMStreamChunk(kind="content.delta", delta="Hi", index=0),
            LLMStreamChunk(kind="message.done", message=msg, finish_reason="stop"),
        ])

    可多次 ``script_chunks`` 排队多轮（每次 ``stream`` / ``complete`` 弹出队首一组）。
    """

    def __init__(self) -> None:
        self._scripts: list[list[LLMStreamChunk]] = []
        self.calls: list[LLMRequest] = []

    def script_chunks(self, chunks: list[LLMStreamChunk]) -> None:
        """排队一组 chunks，下一次 ``stream`` / ``complete`` 弹出消费。"""
        self._scripts.append(list(chunks))

    def _next_chunks(self, request: LLMRequest) -> list[LLMStreamChunk]:
        self.calls.append(request)
        if not self._scripts:
            # 默认终止：返回最小 message.done chunk
            return [
                LLMStreamChunk(
                    kind="message.done",
                    message=Message(role="assistant", content=""),
                    finish_reason="stop",
                )
            ]
        return self._scripts.pop(0)

    async def stream(self, request: LLMRequest) -> AsyncIterator[LLMStreamChunk]:
        chunks = self._next_chunks(request)
        for c in chunks:
            yield c

    async def complete(self, request: LLMRequest) -> LLMResponse:
        """从同一份脚本拿 chunks，找 message.done 拼装 LLMResponse。"""
        chunks = self._next_chunks(request)
        for c in chunks:
            if c.kind == "message.done":
                if c.message is None:
                    raise ValueError("message.done chunk missing message")
                return LLMResponse(
                    message=c.message,
                    finish_reason=c.finish_reason or "stop",
                    usage=c.usage,
                    provider_metadata=dict(c.provider_metadata),
                )
        raise ValueError("scripted chunks ended without message.done")


# ---------------------------------------------------------------------------
# Memory Event Sink
# ---------------------------------------------------------------------------


class MemoryEventSink:
    """把所有 Event 收集到内存，便于断言事件流。"""

    def __init__(self) -> None:
        self.events: list[Event] = []

    async def emit(self, event: Event) -> None:
        self.events.append(event)

    def kinds(self) -> list[str]:
        """返回事件 kind 序列，方便 ``assert "run.start" in sink.kinds()``。"""
        return [e.kind for e in self.events]

    def of_kind(self, kind: str) -> list[Event]:
        """返回指定 kind 的事件子集。"""
        return [e for e in self.events if e.kind == kind]


# ---------------------------------------------------------------------------
# Recording Approval
# ---------------------------------------------------------------------------


class RecordingApproval:
    """记录所有 approval 请求；按 ``outcome`` 返回固定决定。

    默认 ``"approved"``。需要反向测试时传 ``"rejected"`` / ``"cancelled"``。
    """

    def __init__(self, outcome: ApprovalOutcome = "approved") -> None:
        self._outcome: ApprovalOutcome = outcome
        self.requests: list[ApprovalRequest] = []

    async def decide(self, request: ApprovalRequest) -> ApprovalDecision:
        self.requests.append(request)
        return ApprovalDecision(
            outcome=self._outcome,
            reason=f"recording-{self._outcome}",
            metadata={"test": True, "tool_name": request.tool_name},
        )


# ---------------------------------------------------------------------------
# Pytest fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def stub_llm() -> StubLLMProvider:
    """一个空的 StubLLMProvider，测试自己 ``script(...)`` 排队响应。"""
    return StubLLMProvider()


@pytest.fixture
def stub_stream_llm() -> StubLLMStreamProvider:
    """一个空的 :class:`StubLLMStreamProvider`，测试自己 ``script_chunks(...)`` 排队。"""
    return StubLLMStreamProvider()


@pytest.fixture
def memory_sink() -> MemoryEventSink:
    return MemoryEventSink()


# RecordingEventSink 是 MemoryEventSink 的别名（语义对齐 plan.md 的命名约定）
RecordingEventSink = MemoryEventSink


@pytest.fixture
def recording_event_sink() -> MemoryEventSink:
    return MemoryEventSink()


@pytest.fixture
def recording_approval() -> RecordingApproval:
    return RecordingApproval()


# ---------------------------------------------------------------------------
# Recording Host Adapter（bridge fixture 用）
# ---------------------------------------------------------------------------


class RecordingAdapter:
    """最小 host adapter（结构鸭子满足 :class:`HostAdapter`，故意不子类化）。

    刻意**不** ``class RecordingAdapter(HostAdapter)``：子类化会在 class 定义（collection）
    时求值基类，从而在模块顶层 import ``hosts.shared.base`` —— 那会破坏同目录
    ``test_setting_yaml_runtime_safety`` 的 shell_tool 未加载护栏（见文件顶部注释）。
    HostDispatcher 对 adapter 是鸭子调用（只用 render_result / close），结构满足即可。

    发送链路 e2e 只关心 run 结果和 LLM 请求，不驱动 read_input 交互循环，因此这里只
    落地 write_output（收集 assistant 回显 + steer / tokens 提示行）与幂等 close；
    outputs 列表供需要断言回显的用例读取。
    """

    def __init__(self) -> None:
        self.outputs: list[str] = []

    async def write_output(self, text: str) -> None:
        # 把一行输出追加进内存,供用例断言 echo / steer 提示(不做真实 IO)。
        self.outputs.append(text)

    async def render_result(self, result: Result) -> None:
        """复刻 HostAdapter 基类默认 render_result(拆 content 走 write_output)。

        RecordingAdapter 故意不继承 HostAdapter(见类注释),拿不到基类默认实现。
        bridge 现在调 render_result 而非 _write_runtime_result,这里手写一份等价
        实现:把 final_message.content 走 write_output 落进 outputs 供用例断言。
        e2e 测试关心 run 结果与 LLM 请求,不关心 [error] / [tokens] 格式化
        (那是 CLIAdapter 的 unit 测试范围),所以这里只复刻 content 部分。
        """
        final = result.final_message
        if final is not None and final.content:
            await self.write_output(final.content)

    async def close(self) -> None:
        # 幂等 no-op:本 adapter 不持有需释放的资源。
        return None


# bridge 工厂类型别名：用例调 make_bridge(runtime=..., session_id=...) 拿 HostDispatcher。
# 函数名沿用 make_bridge（调用方多），但 host-dispatch-consolidation 后返回的已是
# HostDispatcher。用宽松签名避免在模块顶层引用真类型。
BridgeFactory = Callable[..., Awaitable["HostDispatcher"]]


@pytest.fixture
async def bridge_factory() -> AsyncIterator[BridgeFactory]:
    """发放 HostDispatcher 并在 teardown 统一关闭（谁创建谁回收）。

    用例体内**不出现任何关闭调用**——生命周期归本 fixture：每次
    ``make_bridge(...)`` 记录 (dispatcher, runtime) 对，fixture yield 结束后逐对
    ``await dispatcher.aclose(drain=True)`` + ``await runtime.aclose()``。用 drain=True
    是刻意选择：排空在途/排队消息再停 agent_loop，让"空闲 send 回落排队"这类用例
    投出的独立新 run 能在 teardown 时确定性跑完（对已收尾的 dispatcher 幂等）。

    关闭异常互不阻塞：单个 dispatcher/runtime 关闭抛异常只记不打断其余回收，避免一个
    坏实例拖垮整批清理。

    ``HostDispatcher`` 在此**延迟 import**（不放模块顶层，见文件顶部 shell_tool 护栏注释）。
    """
    from hosts.shared.host_dispatcher import HostDispatcher

    created: list[tuple[HostDispatcher, SessionEngine]] = []

    async def make_bridge(
        *,
        runtime: SessionEngine,
        session_id: str,
        adapter: object | None = None,
    ) -> HostDispatcher:
        # adapter 缺省用 RecordingAdapter（收集输出便于断言）；调用方可传自己的。
        # host-dispatch-consolidation：装配走 HostDispatcher。
        # 函数名仍叫 make_bridge（调用方多），但返回的是 HostDispatcher 实例；
        # queued_result_handler 取 adapter.render_result（与生产装配一致）。
        recording_adapter = adapter or RecordingAdapter()
        dispatcher = HostDispatcher(
            runtime=runtime,
            session_id=session_id,
            queued_result_handler=recording_adapter.render_result,  # type: ignore[arg-type]
        )
        created.append((dispatcher, runtime))
        return dispatcher

    yield make_bridge

    # teardown：逐对回收（dispatcher 先排空关闭，再关 runtime 释放 provider）。异常互不阻塞。
    for dispatcher, runtime in created:
        try:
            await dispatcher.aclose(drain=True)
        except Exception as exc:  # teardown 尽力清理，异常只记不打断
            print(f"⚠️ dispatcher.aclose 异常（忽略继续回收）: {exc!r}")
        try:
            await runtime.aclose()
        except Exception as exc:  # 同上：teardown 异常只记不打断
            print(f"⚠️ runtime.aclose 异常（忽略继续回收）: {exc!r}")


@pytest.fixture
def local_model_config() -> Config:
    """构造指向本地模型基线的 :class:`Config`。

    该 fixture 仅用于装配路径验证，不会发起真实 HTTP 请求。
    """
    return Config(
        model=ModelSelectionConfig(preset_id="local-gemma-4-e4b-it"),
        runner=RunnerConfig(max_turns=5),
        approval=ApprovalConfig(mode="auto_allow"),
    )


__all__ = [
    "BridgeFactory",
    "MemoryEventSink",
    "RecordingAdapter",
    "RecordingApproval",
    "RecordingEventSink",
    "StubLLMProvider",
    "StubLLMStreamProvider",
]
