"""unit：tools.base.BaseBuiltinTool 兜底契约。

B9 / CR 报告 cr-report-20260424-202744.md。
"""

from __future__ import annotations

from typing import Any

import pytest

from core.contracts import ToolContext, ToolResult
from tools.base import BaseBuiltinTool


def _ctx(call_id: str = "c1") -> ToolContext:
    return ToolContext(run_id="r", session_id="s", turn=1, call_id=call_id)


class _EchoTool(BaseBuiltinTool):
    name = "echo"
    description = "echo args"
    input_schema: dict[str, Any] = {
        "type": "object",
        "properties": {"msg": {"type": "string"}, "n": {"type": "integer"}},
        "required": ["msg"],
    }

    async def _run(self, args, ctx):  # type: ignore[override]
        return f"echo:{args['msg']}", {"msg_len": len(args["msg"])}


class _BoomTool(BaseBuiltinTool):
    name = "boom"
    description = "always fails"
    input_schema: dict[str, Any] = {"type": "object", "properties": {}, "required": []}

    async def _run(self, args, ctx):  # type: ignore[override]
        raise RuntimeError("boom inside")


@pytest.mark.asyncio
async def test_happy_path_returns_ok_result():
    tool = _EchoTool()
    result = await tool.execute({"msg": "hi"}, _ctx())
    assert isinstance(result, ToolResult)
    assert result.ok is True
    assert result.content == "echo:hi"
    assert result.data == {"msg_len": 2}


@pytest.mark.asyncio
async def test_missing_required_arg_returns_fail_not_exception():
    tool = _EchoTool()
    result = await tool.execute({}, _ctx())
    assert result.ok is False
    assert "missing required args" in (result.error_message or "")
    assert "msg" in (result.error_message or "")
    assert "工具执行失败：echo" in result.content
    assert "禁止声称工具已经成功执行" in result.content


@pytest.mark.asyncio
async def test_non_dict_args_returns_fail():
    tool = _EchoTool()
    result = await tool.execute("not-a-dict", _ctx())  # type: ignore[arg-type]
    assert result.ok is False
    assert "tool args must be dict" in (result.error_message or "")


@pytest.mark.asyncio
async def test_run_exception_wrapped_to_fail_result():
    """子类裸异常必须被 execute 包成 ToolResult(ok=False)，绝不冒泡。"""
    tool = _BoomTool()
    result = await tool.execute({}, _ctx())
    assert result.ok is False
    assert "工具执行失败：boom" in result.content
    assert "后续处理要求" in result.content
    assert "boom inside" in (result.error_message or "")


@pytest.mark.asyncio
async def test_validate_returns_original_args_when_valid():
    """_validate_args 默认不做类型校验；只确认 required 存在。额外字段应当透传。"""

    class _AllowExtra(_EchoTool):
        async def _run(self, args, ctx):  # type: ignore[override]
            return str(args), None

    tool = _AllowExtra()
    result = await tool.execute({"msg": "x", "extra": 42}, _ctx())
    assert result.ok is True
    assert "msg" in result.content
    assert "extra" in result.content


@pytest.mark.asyncio
async def test_no_required_list_never_fails_validation():
    """input_schema 无 required 时，空 args 也应通过。"""

    class _NoRequired(BaseBuiltinTool):
        name = "nr"
        description = ""
        input_schema: dict[str, Any] = {"type": "object", "properties": {}}

        async def _run(self, args, ctx):  # type: ignore[override]
            return "ok", None

    tool = _NoRequired()
    result = await tool.execute({}, _ctx())
    assert result.ok is True


@pytest.mark.asyncio
async def test_validate_args_with_custom_exception_type():
    """_validate_args 抛 TypeError 也应被兜底（当前实现用 except Exception）。"""
    tool = _EchoTool()
    # 通过传非 dict 触发 TypeError → 仍落到 ToolResult(ok=False)
    result = await tool.execute(None, _ctx())  # type: ignore[arg-type]
    assert result.ok is False
