"""Harness Eval fake 工具与状态化 store。

# 包含：RecordingEventSink（事件记录）、FixtureRuntimeLLM（fixture 模式伪 LLM）、
# 5 个 Eval*Tool（代码搜索/文件读取/MCP 相关 fake 工具）、
# StatefulToolStore（τ-bench 风格 per-task 世界状态）、
# 3 个 mini_retail 工具（get_order/cancel_order/process_return）、
# fixture 调用构造辅助、EvalNoopCompactor。
"""

from __future__ import annotations

import copy
from collections.abc import Sequence
from typing import Any, ClassVar

from core.contracts import (
    Event,
    LLMRequest,
    LLMResponse,
    ToolContext,
)
from core.message import Message, ToolCall
from tools.runtime.base import BaseBuiltinTool
from tools.runtime.registry import ToolRegistry

from .models import Task


class RecordingEventSink:
    """记录 Runner 事件，输入为 Event，输出为内存中的 JSON 友好列表。"""

    def __init__(self) -> None:
        self.events: list[dict[str, Any]] = []

    async def emit(self, event: Event) -> None:
        """接收单条事件，输入 Event，无返回值。"""

        self.events.append(
            {
                "kind": event.kind,
                "run_id": event.run_id,
                "turn": event.turn,
                "timestamp_ms": event.timestamp_ms,
                "payload": event.payload,
            }
        )


class FixtureRuntimeLLM:
    """fixture 模式 provider，输入 Task，输出可驱动真实 Runner 的 LLMResponse。"""

    def __init__(self, task: Task) -> None:
        self._task = task
        self.requests: list[LLMRequest] = []

    async def complete(self, request: LLMRequest) -> LLMResponse:
        """按 task fixture 生成响应，输入 LLMRequest，输出 LLMResponse。"""

        self.requests.append(request)
        scoring_type = self._task.scoring.get("type")
        if scoring_type == "tool_execution" and not _has_tool_result(request.messages):
            return LLMResponse(
                message=Message.assistant(tool_calls=_fixture_tool_calls(self._task)),
                finish_reason="tool_calls",
            )
        if (
            scoring_type == "tool_state"
            and self._task.fixture_calls
            and not _has_tool_result(request.messages)
        ):
            return LLMResponse(
                message=Message.assistant(tool_calls=_fixture_state_calls(self._task)),
                finish_reason="tool_calls",
            )
        return LLMResponse(
            message=Message.assistant(self._task.fixture_response or ""),
            finish_reason="stop",
        )


# ---------------------------------------------------------------------------
# Eval fake tools（fixture 模式专用）
# ---------------------------------------------------------------------------


class EvalSearchCodeTool(BaseBuiltinTool):
    """评测用代码搜索工具，输入 query，输出固定代码位置。"""

    name = "search_code"
    description = "Search indexed source code and return matching files and symbols."
    input_schema: ClassVar[dict[str, Any]] = {
        "type": "object",
        "properties": {"query": {"type": "string"}},
        "required": ["query"],
    }

    async def _run(
        self,
        args: dict[str, Any],
        ctx: ToolContext,
    ) -> tuple[str, dict[str, Any] | None]:
        """执行固定搜索，输入 query，输出 Runner.run 定义位置。"""

        query = str(args.get("query", ""))
        if "Runner.run" not in query:
            return (
                f"未找到精确匹配：{query}",
                {"matches": []},
            )
        return (
            "找到 1 个匹配：src/core/runner.py:135 async def run(...)",
            {
                "matches": [
                    {
                        "path": "src/core/runner.py",
                        "line": 135,
                        "symbol": "Runner.run",
                    }
                ]
            },
        )


class EvalReadFileTool(BaseBuiltinTool):
    """评测用文件读取工具，输入 path，输出固定源码片段。"""

    name = "read_file"
    description = "Read a repository file by path."
    input_schema: ClassVar[dict[str, Any]] = {
        "type": "object",
        "properties": {"path": {"type": "string"}},
        "required": ["path"],
    }

    async def _run(
        self,
        args: dict[str, Any],
        ctx: ToolContext,
    ) -> tuple[str, dict[str, Any] | None]:
        """执行固定文件读取，输入 path，输出 Runner.run 工具闭环片段。"""

        path = str(args.get("path", ""))
        if path != "src/core/runner.py":
            raise ValueError(f"unexpected path: {path}")
        return (
            "src/core/runner.py 摘要：Runner.run 调用 _drive_turns；"
            "_drive_turns 发现 assistant tool_calls 后执行工具；"
            "工具结果以 role='tool' 的 tool_result 写回 session，然后进入下一轮 LLM 请求。",
            {
                "path": path,
                "contains": ["Runner.run", "tool_calls", "tool_result", "session"],
            },
        )


class EvalListMcpServersTool(BaseBuiltinTool):
    """评测用 MCP server 列表工具，输入为空，输出 xcodeatlas server。"""

    name = "list_mcp_servers"
    description = "List connected MCP servers."
    input_schema: ClassVar[dict[str, Any]] = {"type": "object", "properties": {}, "required": []}

    async def _run(
        self,
        args: dict[str, Any],
        ctx: ToolContext,
    ) -> tuple[str, dict[str, Any] | None]:
        """返回固定 MCP server 列表，输入为空，输出 xcodeatlas。"""

        return (
            "可用 MCP server：xcodeatlas",
            {"servers": [{"id": "xcodeatlas", "description": "Code graph and dependency atlas"}]},
        )


class EvalListMcpToolsTool(BaseBuiltinTool):
    """评测用 MCP tool 列表工具，输入 server_id，输出 graph 工具。"""

    name = "list_mcp_tools"
    description = "List tools exposed by an MCP server."
    input_schema: ClassVar[dict[str, Any]] = {
        "type": "object",
        "properties": {"server_id": {"type": "string"}},
        "required": ["server_id"],
    }

    async def _run(
        self,
        args: dict[str, Any],
        ctx: ToolContext,
    ) -> tuple[str, dict[str, Any] | None]:
        """返回固定 MCP tool 列表，输入 server_id，输出 graph 工具。"""

        server_id = str(args.get("server_id", ""))
        if server_id != "xcodeatlas":
            raise ValueError(f"unknown server_id: {server_id}")
        return (
            "xcodeatlas 可用工具：graph(format: summary|json), find(query), read(path)",
            {"tools": [{"name": "graph", "args": {"format": "summary"}}]},
        )


class EvalCallMcpTool(BaseBuiltinTool):
    """评测用 MCP tool 调用工具，输入 server/tool/args，输出依赖图摘要。"""

    name = "call_mcp_tool"
    description = "Call a tool on an MCP server."
    input_schema: ClassVar[dict[str, Any]] = {
        "type": "object",
        "properties": {
            "server_id": {"type": "string"},
            "tool_name": {"type": "string"},
            "args": {"type": "object"},
        },
        "required": ["server_id", "tool_name", "args"],
    }

    async def _run(
        self,
        args: dict[str, Any],
        ctx: ToolContext,
    ) -> tuple[str, dict[str, Any] | None]:
        """执行固定 MCP graph 调用，输入调用参数，输出依赖图摘要。"""

        server_id = str(args.get("server_id", ""))
        tool_name = str(args.get("tool_name", ""))
        if server_id != "xcodeatlas" or tool_name != "graph":
            raise ValueError(f"unexpected MCP call: {server_id}.{tool_name}")
        return (
            "xcodeatlas dependency graph summary: core -> tools -> runtime_assembly; "
            "runtime_assembly -> infrastructure; hosts -> runtime_assembly。",
            {
                "server_id": server_id,
                "tool_name": tool_name,
                "modules": ["core", "tools", "runtime_assembly", "infrastructure", "hosts"],
            },
        )


def build_eval_tools() -> ToolRegistry:
    """构造评测 fake tools，输入为空，输出独立 ToolRegistry。"""

    return ToolRegistry(
        [
            EvalSearchCodeTool(),
            EvalReadFileTool(),
            EvalListMcpServersTool(),
            EvalListMcpToolsTool(),
            EvalCallMcpTool(),
        ]
    )


# ---------------------------------------------------------------------------
# τ-bench 风格状态化裁决（StatefulToolStore + mini_retail 工具）
# ---------------------------------------------------------------------------


class StatefulToolStore:
    """τ-bench 风格 per-task 状态 store，输入 yaml initial_state，输出可读写的世界状态。"""

    def __init__(self, initial_state: dict[str, Any]) -> None:
        self._initial = copy.deepcopy(initial_state)
        self._state = copy.deepcopy(initial_state)

    def snapshot(self) -> dict[str, Any]:
        """返回当前态深拷贝，输入为空，输出 JSON 友好的世界状态。"""

        return copy.deepcopy(self._state)

    def initial_snapshot(self) -> dict[str, Any]:
        """返回播种初值深拷贝，输入为空，输出 state_unchanged 比对基线。"""

        return copy.deepcopy(self._initial)

    def get_order(self, order_id: str) -> dict[str, Any] | None:
        """读取单个订单，输入 order_id，输出订单字典深拷贝或 None。"""

        orders = self._state.get("orders")
        if not isinstance(orders, dict):
            return None
        order = orders.get(order_id)
        return copy.deepcopy(order) if isinstance(order, dict) else None

    def set_order_status(self, order_id: str, status: str) -> dict[str, Any]:
        """写入订单状态，输入 order_id 和目标 status，输出更新后的订单字典深拷贝。"""

        orders = self._state.get("orders")
        if not isinstance(orders, dict) or order_id not in orders:
            raise ValueError(f"unknown order_id: {order_id}")
        order = orders[order_id]
        if not isinstance(order, dict):
            raise ValueError(f"order {order_id} is not an object")
        order["status"] = status
        return copy.deepcopy(order)


class EvalGetOrderTool(BaseBuiltinTool):
    """mini_retail 读工具：按 order_id 返回订单字典，输入 order_id，输出订单详情。"""

    name = "get_order"
    description = "Fetch a single order by its order_id from the retail store."
    input_schema: ClassVar[dict[str, Any]] = {
        "type": "object",
        "properties": {"order_id": {"type": "string"}},
        "required": ["order_id"],
    }

    def __init__(self, store: StatefulToolStore) -> None:
        self._store = store

    async def _run(
        self,
        args: dict[str, Any],
        ctx: ToolContext,
    ) -> tuple[str, dict[str, Any] | None]:
        """读取订单，输入 order_id，输出订单字典文本和结构化 data。"""

        order_id = str(args.get("order_id", ""))
        order = self._store.get_order(order_id)
        if order is None:
            return (f"未找到订单：{order_id}", {"order_id": order_id, "found": False})
        return (
            f"订单 {order_id}：status={order.get('status')}, item={order.get('item')}, "
            f"payment={order.get('payment')}, total={order.get('total')}",
            {"order_id": order_id, "found": True, "order": order},
        )


class EvalCancelOrderTool(BaseBuiltinTool):
    """mini_retail 写工具：把订单 status 置为 cancelled，输入 order_id，输出取消结果。"""

    name = "cancel_order"
    description = "Cancel an order, setting its status to cancelled."
    input_schema: ClassVar[dict[str, Any]] = {
        "type": "object",
        "properties": {"order_id": {"type": "string"}},
        "required": ["order_id"],
    }

    def __init__(self, store: StatefulToolStore) -> None:
        self._store = store

    async def _run(
        self,
        args: dict[str, Any],
        ctx: ToolContext,
    ) -> tuple[str, dict[str, Any] | None]:
        """取消订单，输入 order_id，输出更新后订单文本和 data。"""

        order_id = str(args.get("order_id", ""))
        order = self._store.set_order_status(order_id, "cancelled")
        return (
            f"订单 {order_id} 已取消（status=cancelled）。",
            {"order_id": order_id, "order": order},
        )


class EvalProcessReturnTool(BaseBuiltinTool):
    """mini_retail 写工具：把订单 status 置为 returned，输入 order_id，输出退货结果。"""

    name = "process_return"
    description = "Process a return for an order, setting its status to returned."
    input_schema: ClassVar[dict[str, Any]] = {
        "type": "object",
        "properties": {"order_id": {"type": "string"}},
        "required": ["order_id"],
    }

    def __init__(self, store: StatefulToolStore) -> None:
        self._store = store

    async def _run(
        self,
        args: dict[str, Any],
        ctx: ToolContext,
    ) -> tuple[str, dict[str, Any] | None]:
        """处理退货，输入 order_id，输出更新后订单文本和 data。"""

        order_id = str(args.get("order_id", ""))
        order = self._store.set_order_status(order_id, "returned")
        return (
            f"订单 {order_id} 已退货（status=returned）。",
            {"order_id": order_id, "order": order},
        )


def build_eval_retail_tools(store: StatefulToolStore) -> ToolRegistry:
    """构造 mini_retail 状态化工具，输入 per-task store，输出独立 ToolRegistry。"""

    return ToolRegistry(
        [
            EvalGetOrderTool(store),
            EvalCancelOrderTool(store),
            EvalProcessReturnTool(store),
        ]
    )


# ---------------------------------------------------------------------------
# fixture 辅助函数
# ---------------------------------------------------------------------------


def _has_tool_result(messages: tuple[Message, ...]) -> bool:
    """判断请求历史里是否已有 tool result，输入消息元组，输出布尔值。"""

    return any(message.role == "tool" for message in messages)


def _fixture_tool_calls(task: Task) -> list[ToolCall]:
    """按 scoring.expected_calls 构造 fixture tool calls，输入 Task，输出调用列表。"""

    calls: list[ToolCall] = []
    for index, expected in enumerate(task.scoring.get("expected_calls", []), start=1):
        tool_name = str(expected["name"])
        arguments = dict(_fixture_default_arguments(tool_name))
        arguments.update(dict(expected.get("arguments") or {}))
        if expected.get("name") == "call_mcp_tool":
            arguments.setdefault("args", {"format": "summary"})
        calls.append(
            ToolCall(
                call_id=f"fixture-call-{index}",
                tool_name=tool_name,
                arguments=arguments,
            )
        )
    return calls


def _fixture_state_calls(task: Task) -> list[ToolCall]:
    """按 task.fixture_calls 构造 tool_state fixture 调用，输入 Task，输出 ToolCall 列表。"""

    calls: list[ToolCall] = []
    for index, entry in enumerate(task.fixture_calls, start=1):
        calls.append(
            ToolCall(
                call_id=f"fixture-state-call-{index}",
                tool_name=str(entry["name"]),
                arguments=dict(entry.get("arguments") or {}),
            )
        )
    return calls


def _fixture_default_arguments(tool_name: str) -> dict[str, Any]:
    """返回 fixture fake tool 的最小可运行参数，输入工具名，输出参数字典。"""

    if tool_name == "search_code":
        return {"query": "Runner.run"}
    if tool_name == "read_file":
        return {"path": "src/core/runner.py"}
    if tool_name == "list_mcp_tools":
        return {"server_id": "xcodeatlas"}
    if tool_name == "call_mcp_tool":
        return {"server_id": "xcodeatlas", "tool_name": "graph", "args": {"format": "summary"}}
    return {}


class EvalNoopCompactor:
    """评测 profile 用 Noop compactor，输入消息历史，输出原样副本。"""

    async def compact(self, history: Sequence[Message]) -> list[Message]:
        """返回原样消息历史，输入消息序列，输出 list 副本。"""

        return list(history)
