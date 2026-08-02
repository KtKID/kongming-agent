"""unit：ReadFileTool / WriteFileTool / ListDirTool 基本行为。

所有测试都用 ``tmp_path`` fixture，不污染项目目录。
"""

from __future__ import annotations

from pathlib import Path

import pytest

from core.contracts import ToolContext
from tests.support.tool_calls import execute_prepared_tool
from tools import ListDirTool, ReadFileTool, WriteFileTool


def _ctx(cwd: Path | None = None) -> ToolContext:
    metadata = {"cwd": str(cwd)} if cwd is not None else {}
    return ToolContext(run_id="r", session_id="s", turn=1, call_id="c", metadata=metadata)


# ---------------------------------------------------------------------------
# ReadFileTool
# ---------------------------------------------------------------------------


@pytest.mark.unit
async def test_read_file_returns_content(tmp_path: Path) -> None:
    target = tmp_path / "a.txt"
    target.write_text("hello world")

    tool = ReadFileTool()
    result = await execute_prepared_tool(tool, {"path": str(target)}, _ctx())

    assert result.ok is True
    assert result.content == "hello world"
    assert result.data is not None
    assert result.data["total_bytes"] == len("hello world")
    assert result.data["truncated"] is False


@pytest.mark.unit
async def test_read_file_missing_path_returns_error(tmp_path: Path) -> None:
    tool = ReadFileTool()
    result = await execute_prepared_tool(tool, {"path": str(tmp_path / "no.txt")}, _ctx())

    assert result.ok is False
    assert result.error_message is not None
    assert "not found" in result.error_message.lower()


@pytest.mark.unit
async def test_read_file_missing_required_arg_returns_error() -> None:
    tool = ReadFileTool()
    result = await execute_prepared_tool(tool, {}, _ctx())
    assert result.ok is False
    assert "path" in (result.error_message or "")


@pytest.mark.unit
async def test_read_file_respects_max_bytes(tmp_path: Path) -> None:
    target = tmp_path / "big.txt"
    target.write_text("abcdef")
    tool = ReadFileTool()
    result = await execute_prepared_tool(tool, {"path": str(target), "max_bytes": 3}, _ctx())
    assert result.ok is True
    assert result.data is not None
    assert result.data["truncated"] is True
    assert "truncated" in result.content.lower()


@pytest.mark.unit
async def test_read_file_relative_path_uses_context_cwd(tmp_path: Path) -> None:
    target = tmp_path / "note.txt"
    target.write_text("from cwd")

    tool = ReadFileTool()
    result = await execute_prepared_tool(tool, {"path": "note.txt"}, _ctx(tmp_path))

    assert result.ok is True
    assert result.content == "from cwd"


# ---------------------------------------------------------------------------
# WriteFileTool
# ---------------------------------------------------------------------------


@pytest.mark.unit
async def test_write_file_creates_file_and_content(tmp_path: Path) -> None:
    target = tmp_path / "out.txt"
    tool = WriteFileTool()
    result = await execute_prepared_tool(
        tool,
        {"path": str(target), "content": "payload"},
        _ctx(),
    )

    assert result.ok is True
    assert target.exists()
    assert target.read_text() == "payload"
    assert result.data is not None
    assert result.data["mode"] == "write"


@pytest.mark.unit
async def test_write_file_append_mode(tmp_path: Path) -> None:
    target = tmp_path / "out.txt"
    target.write_text("start-")
    tool = WriteFileTool()
    result = await execute_prepared_tool(
        tool,
        {"path": str(target), "content": "more", "append": True},
        _ctx(),
    )

    assert result.ok is True
    assert target.read_text() == "start-more"
    assert result.data is not None
    assert result.data["mode"] == "append"


@pytest.mark.unit
async def test_write_file_creates_parent_dirs(tmp_path: Path) -> None:
    target = tmp_path / "nested" / "deep" / "out.txt"
    tool = WriteFileTool()
    result = await execute_prepared_tool(
        tool,
        {"path": str(target), "content": "x"},
        _ctx(),
    )
    assert result.ok is True
    assert target.exists()


@pytest.mark.unit
async def test_write_file_missing_content_arg_fails(tmp_path: Path) -> None:
    target = tmp_path / "o.txt"
    tool = WriteFileTool()
    result = await execute_prepared_tool(tool, {"path": str(target)}, _ctx())
    assert result.ok is False


@pytest.mark.unit
async def test_write_file_relative_path_uses_context_cwd(tmp_path: Path) -> None:
    tool = WriteFileTool()
    result = await execute_prepared_tool(
        tool,
        {"path": "nested/out.txt", "content": "payload"},
        _ctx(tmp_path),
    )

    assert result.ok is True
    assert (tmp_path / "nested" / "out.txt").read_text() == "payload"


# ---------------------------------------------------------------------------
# ListDirTool
# ---------------------------------------------------------------------------


@pytest.mark.unit
async def test_list_dir_enumerates_entries(tmp_path: Path) -> None:
    (tmp_path / "a.txt").write_text("x")
    (tmp_path / "sub").mkdir()

    tool = ListDirTool()
    result = await execute_prepared_tool(tool, {"path": str(tmp_path)}, _ctx())

    assert result.ok is True
    assert result.data is not None
    names = {e["name"] for e in result.data["entries"]}
    assert names == {"a.txt", "sub"}
    types_by_name = {e["name"]: e["type"] for e in result.data["entries"]}
    assert types_by_name["a.txt"] == "file"
    assert types_by_name["sub"] == "dir"


@pytest.mark.unit
async def test_list_dir_non_directory_returns_error(tmp_path: Path) -> None:
    f = tmp_path / "a.txt"
    f.write_text("x")
    tool = ListDirTool()
    result = await execute_prepared_tool(tool, {"path": str(f)}, _ctx())
    assert result.ok is False
    assert "not a directory" in (result.error_message or "").lower()


@pytest.mark.unit
async def test_list_dir_missing_path_returns_error(tmp_path: Path) -> None:
    tool = ListDirTool()
    result = await execute_prepared_tool(tool, {"path": str(tmp_path / "no")}, _ctx())
    assert result.ok is False


@pytest.mark.unit
async def test_list_dir_relative_path_uses_context_cwd(tmp_path: Path) -> None:
    (tmp_path / "sub").mkdir()
    (tmp_path / "sub" / "a.txt").write_text("x")

    tool = ListDirTool()
    result = await execute_prepared_tool(tool, {"path": "sub"}, _ctx(tmp_path))

    assert result.ok is True
    assert result.data is not None
    assert [entry["name"] for entry in result.data["entries"]] == ["a.txt"]
