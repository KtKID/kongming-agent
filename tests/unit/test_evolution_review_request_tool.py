"""公开进化审查 Tool 与 Manager 注册/排队合同测试。"""

from __future__ import annotations

from pathlib import Path

import pytest

from core.contracts import ToolContext
from evolution.evolution_manager import EvolutionManager
from evolution.models import MAX_MANUAL_REVIEW_FOCUS_CHARS
from infrastructure.config.models import Config
from tests.support.tool_calls import execute_prepared_tool
from tools import ToolRegistry


def _config(tmp_path: Path, *, enabled: bool = True) -> Config:
    """构造隔离 evolution 目录的测试配置。"""
    from infrastructure.config import load_config

    cfg = load_config(None)
    return cfg.model_copy(
        update={
            "evolution": cfg.evolution.model_copy(
                update={
                    "learning": cfg.evolution.learning.model_copy(
                        update={
                            "enabled": enabled,
                            "root_path": str(tmp_path / "evolution"),
                        }
                    )
                }
            )
        }
    )


def _context(*, session_id: str = "thread-1", run_id: str = "run-thread-1-7") -> ToolContext:
    """构造当前主 run 的工具上下文。"""
    return ToolContext(
        session_id=session_id,
        run_id=run_id,
        turn=1,
        call_id="call-request-review",
    )


@pytest.mark.asyncio
async def test_public_tool_schema_and_structured_result(tmp_path: Path) -> None:
    manager = EvolutionManager(config=_config(tmp_path), kongming_home=tmp_path)
    registry = ToolRegistry()

    assert manager.register_runtime_tools(registry) is True
    assert registry.names() == ["request_evolution_review", "evolution_write"]
    tool = registry["request_evolution_review"]
    assert tool.name == "request_evolution_review"
    assert set(tool.input_schema["properties"]) == {"focus"}
    assert tool.input_schema["additionalProperties"] is False
    assert "required" not in tool.input_schema

    result = await execute_prepared_tool(tool, {"focus": "  提炼失败恢复流程  "}, _context())

    assert result.ok is True
    assert result.data == {
        "status": "queued",
        "session_id": "thread-1",
        "run_id": "run-thread-1-7",
        "trigger_reason": "manual_tool",
    }
    request = await manager._consume_manual_review(
        session_id="thread-1",
        run_id="run-thread-1-7",
    )
    assert request is not None
    assert request.focus == "提炼失败恢复流程"
    await manager.aclose()


@pytest.mark.asyncio
async def test_duplicate_request_keeps_first_focus_and_cross_run_isolated(tmp_path: Path) -> None:
    manager = EvolutionManager(config=_config(tmp_path), kongming_home=tmp_path)
    registry = ToolRegistry()
    manager.register_runtime_tools(registry)
    tool = registry["request_evolution_review"]

    first = await execute_prepared_tool(tool, {"focus": "first"}, _context())
    duplicate = await execute_prepared_tool(tool, {"focus": "second"}, _context())
    other_run = await execute_prepared_tool(
        tool,
        {"focus": "other"},
        _context(run_id="run-thread-1-8"),
    )

    assert first.data is not None
    assert first.data["status"] == "queued"
    assert duplicate.data is not None
    assert duplicate.data["status"] == "already_queued"
    assert other_run.data is not None
    assert other_run.data["status"] == "queued"
    request_first = await manager._consume_manual_review(
        session_id="thread-1",
        run_id="run-thread-1-7",
    )
    request_other = await manager._consume_manual_review(
        session_id="thread-1",
        run_id="run-thread-1-8",
    )
    assert request_first is not None
    assert request_first.focus == "first"
    assert request_other is not None
    assert request_other.focus == "other"
    await manager.aclose()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("arguments", "error_fragment"),
    [
        ({"target": "memory"}, "unknown arguments: target"),
        ({"focus": 7}, "focus must be a string"),
        (
            {"focus": "x" * (MAX_MANUAL_REVIEW_FOCUS_CHARS + 1)},
            f"focus must be at most {MAX_MANUAL_REVIEW_FOCUS_CHARS} characters",
        ),
    ],
)
async def test_invalid_arguments_do_not_queue_request(
    tmp_path: Path,
    arguments: dict[str, object],
    error_fragment: str,
) -> None:
    manager = EvolutionManager(config=_config(tmp_path), kongming_home=tmp_path)
    registry = ToolRegistry()
    manager.register_runtime_tools(registry)

    result = await execute_prepared_tool(
        registry["request_evolution_review"],
        arguments,
        _context(),
    )

    assert result.ok is False
    assert result.error_message is not None
    assert error_fragment in result.error_message
    assert (
        await manager._consume_manual_review(
            session_id="thread-1",
            run_id="run-thread-1-7",
        )
        is None
    )
    await manager.aclose()


@pytest.mark.asyncio
async def test_blank_focus_is_normalized_to_none(tmp_path: Path) -> None:
    manager = EvolutionManager(config=_config(tmp_path), kongming_home=tmp_path)
    registry = ToolRegistry()
    manager.register_runtime_tools(registry)

    result = await execute_prepared_tool(
        registry["request_evolution_review"],
        {"focus": " \n\t "},
        _context(),
    )

    assert result.ok is True
    request = await manager._consume_manual_review(
        session_id="thread-1",
        run_id="run-thread-1-7",
    )
    assert request is not None
    assert request.focus is None
    await manager.aclose()


@pytest.mark.asyncio
async def test_registration_and_enabled_names_follow_runtime_boundary(tmp_path: Path) -> None:
    manager = EvolutionManager(config=_config(tmp_path), kongming_home=tmp_path)
    registry = ToolRegistry()
    manager.register_runtime_tools(registry)

    assert manager.private_tool_names == frozenset({"evolution_write"})
    assert manager.enabled_tool_names(
        registry.names(),
        lifecycle_bound=True,
    ) == ["request_evolution_review"]
    assert (
        manager.enabled_tool_names(
            registry.names(),
            lifecycle_bound=False,
        )
        == []
    )

    disabled_manager = EvolutionManager(
        config=_config(tmp_path, enabled=False),
        kongming_home=tmp_path / "disabled",
    )
    disabled_registry = ToolRegistry()
    assert disabled_manager.register_runtime_tools(disabled_registry) is False
    assert disabled_registry.names() == []
    await manager.aclose()
    await disabled_manager.aclose()
