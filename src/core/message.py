"""统一消息结构。

v1-mini 里所有跨模块流转的消息都经过这层结构化。
``role`` 收敛到四种：``user / assistant / system / tool``。
tool 调用在 assistant 消息上以 ``tool_calls`` 子结构出现，
tool 执行结果以 ``role="tool"`` 加 ``tool_call_id`` 关联。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal

MessageRole = Literal["user", "assistant", "system", "tool"]


@dataclass(frozen=True)
class ToolCall:
    """模型发起的一次工具调用请求。

    Attributes:
        call_id: provider 返回或内部生成的调用 id，后续工具回填要通过它关联。
        tool_name: 要调用的工具名，对应 :attr:`core.contracts.Tool.name`。
        arguments: 结构化参数，必须能 JSON 序列化。
    """

    call_id: str
    tool_name: str
    arguments: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class Message:
    """系统内部统一消息结构。

    - ``role="user"``：来自宿主/用户的输入，``content`` 必填。
    - ``role="assistant"``：模型响应，可能带 ``tool_calls``，也可能只有 ``content``。
    - ``role="system"``：来自 :class:`core.agent_spec.AgentSpec` 的指令或装配层注入的规则。
    - ``role="tool"``：工具执行结果，``tool_call_id`` 必填，``content`` 保存结果文本。

    Attributes:
        role: 消息角色。
        content: 文本内容。assistant 消息只有 tool_calls 时可以为 None。
        tool_calls: 仅 assistant 消息使用；一次响应可以并发发起多个 tool call。
        tool_call_id: 仅 tool 消息使用，关联对应的 ToolCall.call_id。
        name: 可选的发言者名，保留给 provider 需要区分同角色不同身份时用。
        metadata: 附加元信息，比如 provider 原始 id、token usage 等。不参与语义。

    Metadata 字段约定:
        以下 key 是 v0.1.6+ 跨模块共享的"软契约"，结构由调用方保证，
        Message 本身不做校验。缺失/空值都视为"无附件/无该字段"。

        ``attachments`` (``list[dict[str, Any]]``):
            仅在 ``role="user"`` 时有意义；其他角色应忽略此字段。
            结构与 Web wire 的 ``UserInputAttachment`` 对齐
            （asset_id / kind / mime_type / size_bytes / width / height /
            duration_ms / preview_url / status），下游 InputAssembler 读出后
            组装为多模态 provider content block。
            空列表或缺失 = 该消息无附件。
            详见 ``dev-pipeline/tasks/claude-image-paste-e2e/README.md`` §1。
    """

    role: MessageRole
    content: str | None = None
    tool_calls: tuple[ToolCall, ...] | None = None
    tool_call_id: str | None = None
    name: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)

    @staticmethod
    def user(content: str, *, metadata: dict[str, Any] | None = None) -> Message:
        """构造 user 消息的便捷方法。"""
        return Message(role="user", content=content, metadata=metadata or {})

    @staticmethod
    def system(content: str, *, metadata: dict[str, Any] | None = None) -> Message:
        """构造 system 消息的便捷方法。"""
        return Message(role="system", content=content, metadata=metadata or {})

    @staticmethod
    def assistant(
        content: str | None = None,
        *,
        tool_calls: list[ToolCall] | tuple[ToolCall, ...] | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> Message:
        """构造 assistant 消息的便捷方法。

        ``content`` 和 ``tool_calls`` 可以任选其一存在，也可以同时存在。
        """
        calls: tuple[ToolCall, ...] | None = None
        if tool_calls is not None:
            calls = tuple(tool_calls)
        return Message(
            role="assistant",
            content=content,
            tool_calls=calls,
            metadata=metadata or {},
        )

    @staticmethod
    def tool_result(
        tool_call_id: str,
        content: str,
        *,
        name: str | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> Message:
        """构造 tool 结果消息。

        Args:
            tool_call_id: 必须对应此前 assistant 消息中某个 ToolCall.call_id。
            content: 工具结果的文本表述（结构化结果由实现侧序列化）。
            name: 可选，工具名，便于下游观测。
            metadata: 附加信息，例如执行耗时、退出码等。
        """
        return Message(
            role="tool",
            content=content,
            tool_call_id=tool_call_id,
            name=name,
            metadata=metadata or {},
        )


__all__ = ["Message", "MessageRole", "ToolCall"]
