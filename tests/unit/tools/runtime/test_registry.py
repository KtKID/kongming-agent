"""unit：tools.runtime.registry CRUD + ToolLookup Protocol 兼容性。"""

from __future__ import annotations

from typing import Any

import pytest

from core.contracts import PreparedToolCall, ToolContext, ToolResult
from tools import ToolRegistry


class _FakeTool:
    """最小 Tool 实现，用于 registry 单测。"""

    def __init__(self, name: str) -> None:
        self.name = name
        self.description = f"fake {name}"
        self.input_schema: dict[str, Any] = {"type": "object"}

    async def execute(
        self, prepared: PreparedToolCall, ctx: ToolContext
    ) -> ToolResult:  # pragma: no cover - 本测试不会触发
        del prepared, ctx
        return ToolResult(ok=True, content="")


@pytest.mark.unit
def test_registry_register_and_get() -> None:
    reg = ToolRegistry()
    t = _FakeTool("t1")
    reg.register(t)

    assert reg.get("t1") is t
    assert "t1" in reg
    assert reg["t1"] is t


@pytest.mark.unit
def test_registry_constructor_accepts_iterable() -> None:
    reg = ToolRegistry([_FakeTool("a"), _FakeTool("b")])
    assert sorted(reg.names()) == ["a", "b"]
    assert len(reg) == 2


@pytest.mark.unit
def test_registry_register_duplicate_raises() -> None:
    reg = ToolRegistry([_FakeTool("a")])
    with pytest.raises(ValueError):
        reg.register(_FakeTool("a"))


@pytest.mark.unit
def test_registry_register_unnamed_tool_raises() -> None:
    reg = ToolRegistry()
    t = _FakeTool("")
    with pytest.raises(ValueError):
        reg.register(t)


@pytest.mark.unit
def test_registry_unregister_existing_and_missing() -> None:
    reg = ToolRegistry([_FakeTool("a")])
    reg.unregister("a")
    assert "a" not in reg
    # 不存在的反注册应该静默通过
    reg.unregister("never")


@pytest.mark.unit
def test_registry_all_tools_and_names_are_snapshots() -> None:
    reg = ToolRegistry([_FakeTool("a"), _FakeTool("b")])
    tools = reg.all_tools()
    names = reg.names()
    # 修改返回的快照不应影响 registry 内部
    tools.clear()
    names.clear()
    assert len(reg) == 2


@pytest.mark.unit
def test_registry_get_missing_returns_none() -> None:
    reg = ToolRegistry()
    assert reg.get("no") is None


@pytest.mark.unit
def test_registry_getitem_missing_raises_keyerror() -> None:
    reg = ToolRegistry()
    with pytest.raises(KeyError):
        _ = reg["no"]


@pytest.mark.unit
def test_registry_satisfies_tool_lookup_structurally() -> None:
    """``ToolRegistry`` 应该结构上满足 ``ToolLookup`` Protocol。"""
    reg = ToolRegistry([_FakeTool("a")])
    # 不显式 isinstance，只要 __contains__ 和 __getitem__ 到位即可。
    assert hasattr(reg, "__contains__")
    assert hasattr(reg, "__getitem__")
    assert "a" in reg
    assert reg["a"].name == "a"
