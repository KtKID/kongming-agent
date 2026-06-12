"""Task progress tool 单元测试。"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from core.contracts import ToolContext
from infrastructure.config.models import Config
from safety.approval.default_rules import DEFAULT_ALLOW_TOOLS_SILENT
from sessions import TASK_PROGRESS_MAX_ITEMS
from tools import ToolRegistry, register_task_progress_tool
from tools.builtin.task_progress_tool import build_task_progress_tool_from_config


def _cfg(session_root: Path) -> Config:
    return Config.model_validate(
        {
            "model": {
                "name": "fake",
                "base_url": "http://127.0.0.1:1234/v1",
                "api_key": "",
            },
            "session": {
                "backend": "file",
                "file_store_path": str(session_root),
            },
        }
    )


def _ctx(session_id: str = "thread-abc123abc123") -> ToolContext:
    return ToolContext(run_id="r", session_id=session_id, turn=1, call_id="c")


def test_register_task_progress_tool_adds_update_task_progress(tmp_path: Path) -> None:
    registry = ToolRegistry()

    register_task_progress_tool(registry, _cfg(tmp_path / "sessions"))

    assert "update_task_progress" in registry.names()


def test_update_task_progress_is_silent_allowed_by_default() -> None:
    assert "update_task_progress" in DEFAULT_ALLOW_TOOLS_SILENT


@pytest.mark.asyncio
async def test_tool_writes_current_session_with_llm_source(tmp_path: Path) -> None:
    tool = build_task_progress_tool_from_config(_cfg(tmp_path / "sessions"))

    result = await tool.execute(
        {
            "tasks": [
                {
                    "orchestration_task_id": "manual:run-1",
                    "task_id": "task-1",
                    "task_run_id": "run-1",
                    "desc": "实现 LLM tool",
                    "status": "completed",
                }
            ]
        },
        _ctx(),
    )

    assert result.ok is True
    assert result.data is not None
    assert result.data["source"] == "llm"
    assert result.data["session_id"] == "thread-abc123abc123"
    assert result.data["counts"] == {
        "pending": 0,
        "in_progress": 0,
        "completed": 1,
        "total": 1,
    }
    path = tmp_path / "sessions" / "thread-abc123abc123" / "task_progress.json"
    data = json.loads(path.read_text(encoding="utf-8"))
    assert data["tasks"][0]["orchestration_task_id"] == "manual:run-1"
    assert data["tasks"][0]["display_order"] == 0


@pytest.mark.asyncio
async def test_tool_rejects_session_id_argument(tmp_path: Path) -> None:
    tool = build_task_progress_tool_from_config(_cfg(tmp_path / "sessions"))

    result = await tool.execute(
        {
            "session_id": "thread-ffffffffffff",
            "tasks": [
                {
                    "orchestration_task_id": "manual:run-1",
                    "task_id": "task-1",
                    "task_run_id": "run-1",
                    "desc": "x",
                }
            ],
        },
        _ctx(),
    )

    assert result.ok is False
    assert "session_id is not accepted" in (result.error_message or "")
    assert not (tmp_path / "sessions" / "thread-ffffffffffff").exists()


@pytest.mark.asyncio
async def test_tool_requires_current_session(tmp_path: Path) -> None:
    tool = build_task_progress_tool_from_config(_cfg(tmp_path / "sessions"))

    result = await tool.execute(
        {
            "tasks": [
                {
                    "orchestration_task_id": "manual:run-1",
                    "task_id": "task-1",
                    "task_run_id": "run-1",
                    "desc": "x",
                }
            ]
        },
        _ctx(""),
    )

    assert result.ok is False
    assert "ToolContext.session_id is required" in (result.error_message or "")


@pytest.mark.asyncio
async def test_tool_rejects_invalid_task_field(tmp_path: Path) -> None:
    tool = build_task_progress_tool_from_config(_cfg(tmp_path / "sessions"))

    result = await tool.execute(
        {
            "tasks": [
                {
                    "orchestration_task_id": "manual:run-1",
                    "task_id": "task-1",
                    "task_run_id": "run-1",
                    "desc": "",
                    "status": "failed",
                }
            ]
        },
        _ctx(),
    )

    assert result.ok is False
    assert "desc must be a non-empty string" in (result.error_message or "")


@pytest.mark.asyncio
async def test_tool_rejects_unknown_task_field(tmp_path: Path) -> None:
    tool = build_task_progress_tool_from_config(_cfg(tmp_path / "sessions"))

    result = await tool.execute(
        {
            "tasks": [
                {
                    "task_id": "task-1",
                    "task_run_id": "run-1",
                    "desc": "x",
                    "orchestration_task_id": "manual:run-1",
                    "rogue": "field",
                }
            ]
        },
        _ctx(),
    )

    assert result.ok is False
    assert "unknown fields" in (result.error_message or "")


@pytest.mark.asyncio
async def test_tool_requires_orchestration_task_id(tmp_path: Path) -> None:
    tool = build_task_progress_tool_from_config(_cfg(tmp_path / "sessions"))

    result = await tool.execute(
        {"tasks": [{"task_id": "task-1", "task_run_id": "run-1", "desc": "x"}]},
        _ctx(),
    )

    assert result.ok is False
    assert "orchestration_task_id must be a non-empty string" in (result.error_message or "")


@pytest.mark.asyncio
async def test_tool_rejects_too_many_tasks(tmp_path: Path) -> None:
    tool = build_task_progress_tool_from_config(_cfg(tmp_path / "sessions"))
    tasks = [
        {
            "orchestration_task_id": f"manual:run-{index}",
            "task_id": f"task-{index}",
            "task_run_id": f"run-{index}",
            "desc": "x",
        }
        for index in range(TASK_PROGRESS_MAX_ITEMS + 1)
    ]

    result = await tool.execute({"tasks": tasks}, _ctx())

    assert result.ok is False
    assert "at most 128 items" in (result.error_message or "")
