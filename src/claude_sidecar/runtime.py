"""sidecar 运行期协议接口。

定义两个核心 Protocol，让 SDK Bridge（#2）和 Message Translator（#3）能**并行开发**：

- :class:`SdkBridge` —— SDK 客户端封装，由 #2 task 提供具体实现（``sdk_bridge.py``）
- :class:`MessageTranslator` —— SDK Message → SidecarEvent 翻译器，由 #3 task 提供具体实现（``translator.py``）

主循环（``main.py``）拿到的是这两个 Protocol 的实现实例，只通过接口交互。
SDK Bridge 拿到的 Translator 也只通过 :class:`MessageTranslator` 接口交互。

这样 #2 和 #3 不需要互相 import 具体实现，只 import 这个文件即可。

设计取舍：

- ``MessageTranslator.handle`` 直接收 SDK 原始 ``Message``（``claude_agent_sdk.types.Message``）
  —— 不在 SDK Bridge 那一层做"中间事件"包装。Translator 跟 SDK 强耦合，但少一层适配；
  SDK 升级时本来 Translator 也要 verify smoke，加一层中间事件并不会让 Translator 隔离。
- 用 ``Any`` 标注 SDK message 类型 —— 避免本文件 import ``claude_agent_sdk``，
  让 ``runtime.py`` 在没装 SDK 的环境（如纯单测 mock）也能 import。
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Protocol

if TYPE_CHECKING:
    from claude_sidecar.protocol import ReconnectRequest, StartRequest


# can_use_tool 回调签名。具体函数由 #4 approval task 提供，#2 通过
# :meth:`SdkBridge.set_can_use_tool` 接收并注入到 SDK options。
# 这里用 ``Any`` 避免跟 ``claude_agent_sdk.types.CanUseTool`` 强耦合。
CanUseToolCallback = Callable[..., Awaitable[Any]]


@dataclass(frozen=True, slots=True)
class RunContext:
    """一次 run 的产品级身份。

    SDK Bridge 在收到 ``start`` 时构造 RunContext，贯穿整个 run 期间，
    每次调 :meth:`MessageTranslator.handle` 都把它传过去——
    Translator 用它生成 ``threadId`` / ``runId`` 字段。
    """

    workspace_id: str
    thread_id: str
    run_id: str
    runtime_session_id: str | None = None


class MessageTranslator(Protocol):
    """SDK Message → SidecarEvent 翻译器接口。

    具体实现见 :mod:`claude_sidecar.translator`（#3 task 产出）。

    职责：

    - 类型分发：按 SDK message 类型 + content block 类型走不同翻译路径
    - 状态身份提取：``responseId`` / ``toolCallId`` / ``runtimeSessionId``
    - 过滤：``SystemMessage`` 仅消费 ``subtype='init'``，其他丢弃
    - 去重：deny 后 SDK 自动产生的 ToolResultBlock 必须丢弃
    - 终态分类：``ResultMessage`` → ``finished`` / ``failed`` / ``interrupted``
    """

    async def handle(self, sdk_message: Any, context: RunContext) -> None:
        """处理一条 SDK message。

        翻译并通过自己持有的 :class:`~claude_sidecar.output.OutputWriter`
        输出 SidecarEvent JSON。
        """
        ...

    def add_pending_deny(self, request_id: str) -> None:
        """approval deny 后调用：把 ``request_id`` 加进 deny 集合。

        Translator 后续收到 ``UserMessage(content=[ToolResultBlock(tool_use_id==request_id)])``
        时丢弃，不映射成 ``claude_tool_finished``（E8 决议）。
        """
        ...

    def reset_for_new_run(self, context: RunContext) -> None:
        """新 run 开始时调用。

        重置 ``current_run_id`` 和 ``pending_deny_request_ids`` 等 run 级状态；
        ``last_session_id`` 等跨 run 状态保留。
        """
        ...


class SdkBridge(Protocol):
    """``claude_agent_sdk.ClaudeSDKClient`` 封装接口。

    具体实现见 :mod:`claude_sidecar.sdk_bridge`（#2 task 产出）。

    职责：

    - 收 ``start`` 时建立 ``ClaudeSDKClient``（强制 ``include_partial_messages=True``）
    - 多 run 复用同一 client（不断开重连）
    - SDK 流出的 message 喂给 :class:`MessageTranslator`
    - 暴露 ``set_can_use_tool`` 给 #4 task 注入审批回调
    - 暴露 ``interrupt`` / ``reconnect`` 给 #5 lifecycle task 调（本 task 可留
      ``NotImplementedError`` 占位，但接口形态要稳定）
    """

    async def handle_start(self, request: StartRequest) -> None:
        """处理 ``start`` 命令。

        - 首次：建 ``ClaudeSDKClient``，``include_partial_messages=True`` 必开
        - 后续：复用同一 client，调 ``client.query(prompt=...)``
        - SDK message 流喂给 ``translator.handle``
        - run 完成 / 失败由 translator 自己输出终态事件
        """
        ...

    def set_can_use_tool(self, callback: CanUseToolCallback | None) -> None:
        """注入 #4 task 提供的 can_use_tool 回调。

        必须在首次 ``handle_start`` 之前调用。``None`` 表示走默认行为。
        """
        ...

    async def interrupt(self) -> None:
        """中断当前 run（#5 lifecycle task 调）。

        本 task（#2）可暂留 ``NotImplementedError``，等 #5 task 实现。
        """
        ...

    async def reconnect(self, request: ReconnectRequest) -> None:
        """用 ``runtimeSessionId`` 续接 SDK 会话（#5 lifecycle task 调）。

        本 task（#2）可暂留 ``NotImplementedError``，等 #5 task 实现。
        """
        ...

    async def shutdown(self) -> None:
        """关闭 SDK 客户端 + 释放资源。"""
        ...
