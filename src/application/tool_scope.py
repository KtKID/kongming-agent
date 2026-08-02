"""子 Agent 工具集裁剪。

本脚本负责把父 Agent 当前实际工具快照、子任务请求工具和 scope 允许工具收敛成
一份不可变有效工具快照。作用是保证子 Agent 的能力只能单调收紧，并让同一组
``Tool`` 对象同时驱动 LLM tool schemas 与 Runner 执行查找面。
关键执行流程：按父工具顺序去重，依次应用 requested 与 scope 两层名称过滤，最后
优先保留调用方提供的同名 execution wrapper。
关键函数：``clip_child_tool_snapshot`` 计算三方交集，
``resolve_tool_snapshot`` 从 ToolLookup 解析声明式工具名。
"""

from __future__ import annotations

from collections.abc import Collection, Sequence

from core.contracts import Tool, ToolLookup

_NON_INHERITABLE_TOOL_NAMES = frozenset({"request_evolution_review"})


def resolve_tool_snapshot(
    tool_lookup: ToolLookup,
    tool_names: Sequence[str],
) -> tuple[Tool, ...]:
    """解析工具名，输入为查找面与有序名称，输出为去重的不可变工具快照。"""
    resolved: list[Tool] = []
    seen: set[str] = set()
    for raw_name in tool_names:
        name = raw_name.strip()
        if not name or name in seen:
            continue
        if name not in tool_lookup:
            raise ValueError(f"tool {name!r} is not available in the parent ToolLookup")
        seen.add(name)
        resolved.append(tool_lookup[name])
    return tuple(resolved)


def clip_child_tool_snapshot(
    *,
    parent_tools: Sequence[Tool],
    requested_tool_names: Sequence[str] | None,
    scope_allowed_tool_names: Collection[str] | None = None,
    requested_tools: Sequence[Tool] | None = None,
) -> tuple[Tool, ...]:
    """计算子工具快照，输入为父快照、请求名和 scope，输出按父顺序去重的交集。"""
    requested_names = (
        None
        if requested_tool_names is None
        else {name.strip() for name in requested_tool_names if name.strip()}
    )
    scope_names = (
        None
        if scope_allowed_tool_names is None
        else {name.strip() for name in scope_allowed_tool_names if name.strip()}
    )
    requested_by_name = {tool.name: tool for tool in (requested_tools or ()) if tool.name.strip()}

    effective: list[Tool] = []
    seen: set[str] = set()
    for parent_tool in parent_tools:
        name = parent_tool.name
        if name in seen:
            continue
        seen.add(name)
        if name in _NON_INHERITABLE_TOOL_NAMES:
            continue
        if requested_names is not None and name not in requested_names:
            continue
        if scope_names is not None and name not in scope_names:
            continue
        effective.append(requested_by_name.get(name, parent_tool))
    return tuple(effective)


__all__ = ["clip_child_tool_snapshot", "resolve_tool_snapshot"]
