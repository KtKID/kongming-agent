"""Tool registry：工具注册、查找与分发。

:class:`ToolRegistry` 是 v1-mini 里**具体**的 tool 管理容器，
不是跨模块协议（跨模块协议面是 :class:`core.contracts.ToolLookup`）。

它同时实现了 ``__contains__`` 和 ``__getitem__``，因此天然满足
:class:`core.contracts.ToolLookup` Protocol——runner 只依赖 ``ToolLookup``
这一层抽象，并不需要知道具体注册容器的存在。
"""

from __future__ import annotations

from collections.abc import Iterable, Iterator

from core.contracts import Tool


class ToolRegistry:
    """最小 tool 注册表。

    - 以 ``name`` 为唯一主键，不允许重复注册。
    - 同时实现 ``__contains__`` / ``__getitem__``，直接兼容
      :class:`core.contracts.ToolLookup` Protocol。
    - 允许按名反注册、按名查找、全量枚举。

    构造时可以传入一批初始工具：

        >>> registry = ToolRegistry([read_tool, write_tool])

    或者先建空容器再逐个注册：

        >>> registry = ToolRegistry()
        >>> registry.register(read_tool)
    """

    def __init__(self, tools: Iterable[Tool] = ()) -> None:
        self._tools: dict[str, Tool] = {}
        for t in tools:
            self.register(t)

    # ---- 写入 -----------------------------------------------------------

    def register(self, tool: Tool) -> None:
        """注册一个工具。若同名工具已存在则抛 ``ValueError``。"""
        if not getattr(tool, "name", ""):
            raise ValueError("tool.name must be a non-empty string")
        if tool.name in self._tools:
            raise ValueError(f"tool '{tool.name}' already registered")
        self._tools[tool.name] = tool

    def unregister(self, name: str) -> None:
        """按名反注册；不存在时静默返回，不抛异常。"""
        self._tools.pop(name, None)

    # ---- 读取 -----------------------------------------------------------

    def get(self, name: str) -> Tool | None:
        """按名查询，找不到返回 ``None``。"""
        return self._tools.get(name)

    def all_tools(self) -> list[Tool]:
        """返回当前所有工具的快照列表。"""
        return list(self._tools.values())

    def names(self) -> list[str]:
        """返回当前所有工具名的快照列表。"""
        return list(self._tools.keys())

    # ---- ToolLookup Protocol 实现 --------------------------------------

    def __contains__(self, name: object) -> bool:
        return name in self._tools

    def __getitem__(self, name: str) -> Tool:
        return self._tools[name]

    # ---- 便捷 dunder -----------------------------------------------------

    def __len__(self) -> int:
        return len(self._tools)

    def __iter__(self) -> Iterator[Tool]:
        return iter(self._tools.values())


__all__ = ["ToolRegistry"]
