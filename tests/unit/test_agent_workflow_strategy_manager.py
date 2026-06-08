"""智能体工作流策略管理器单元测试。

本脚本验证策略注册管理器的注册、目录展示、详情查询、运行分发、未知策略错误和 planned 策略拒绝执行行为。
作用是确保父 agent 看到的策略列表和运行期实际分发使用同一套注册表。
关键执行流程：构造 fake strategy，注册到 AgentWorkflowStrategyManager，断言 list/describe/run 结果，再覆盖重复注册、未知 mode 和 planned mode。
关键函数：_context_factory 构造测试上下文，_planned_description 构造只读策略说明，各 test_* 函数验证一个策略管理场景。
"""

from __future__ import annotations

from pathlib import Path

import pytest

from application.agent_workflows.context import WorkflowExecutionContext
from application.agent_workflows.manager import AgentWorkflowManager
from application.agent_workflows.strategies.base import (
    WorkflowRunRequest,
    WorkflowStrategyNotFound,
    WorkflowStrategyNotRunnable,
)
from application.agent_workflows.strategies.description import (
    WorkflowStrategyDescription,
    WorkflowStrategyInputField,
)
from application.agent_workflows.strategies.manager import (
    AgentWorkflowStrategyManager,
)
from infrastructure.config.models import Config, ModelConfig


class _AuditWriter:
    """测试用审计写入器，记录最后一次审计事件。"""

    def write_event(self, event: dict[str, object]) -> None:
        """记录审计事件，输入为事件字典，输出为实例上的 event 字段。"""
        self.event = event


class _FakeStrategy:
    """测试用可运行策略，返回固定 workflow 结果并记录入参。"""

    mode = "fake"

    def __init__(self) -> None:
        """初始化 fake strategy，输入为空，输出为可记录 context 和 payload 的策略实例。"""
        self.context: WorkflowExecutionContext | None = None
        self.payload: dict[str, object] | None = None

    def describe(self) -> WorkflowStrategyDescription:
        """生成测试策略详情，输入为当前策略，输出为中文策略说明。"""
        return WorkflowStrategyDescription(
            mode=self.mode,
            title="测试策略",
            status="available",
            runnable=True,
            summary="用于验证 strategy manager 的测试策略。",
            when_to_use=("需要验证 registry 分发时使用",),
            warnings=("仅用于单测",),
            inputs=(
                WorkflowStrategyInputField(
                    name="value",
                    required=True,
                    type_label="string",
                    description="测试输入值。",
                    example="ok",
                ),
            ),
            outputs=("测试结果",),
            examples=({"value": "ok"},),
        )

    def catalog_entry(self):  # type: ignore[no-untyped-def]
        """生成测试策略目录项，输入为策略详情，输出为紧凑目录条目。"""
        return self.describe().catalog_entry()

    async def run(
        self,
        context: WorkflowExecutionContext,
        payload: dict[str, object],
    ) -> dict[str, object]:
        """执行 fake strategy，输入为上下文和 payload，输出为可断言的结果字典。"""
        self.context = context
        self.payload = payload
        return {"workflow_id": context.workflow_id, "payload": payload}


def _context_factory(request: WorkflowRunRequest) -> WorkflowExecutionContext:
    """构造测试上下文，输入为运行请求，输出为固定 workflow context。"""
    return WorkflowExecutionContext(
        workflow_id="wf-test",
        parent_session_id=request.parent_session_id,
        mode=request.mode,
        workflow_dir=Path("/tmp/wf-test"),
        started_at="2026-06-07T00:00:00+00:00",
        audit_writer=_AuditWriter(),
    )


def _planned_description() -> WorkflowStrategyDescription:
    """构造 planned 策略说明，输入为空，输出为不可运行的策略详情。"""
    return WorkflowStrategyDescription(
        mode="planned",
        title="计划中策略",
        status="planned",
        runnable=False,
        summary="用于验证 planned catalog 的测试策略。",
        when_to_use=("后续依赖完成后使用",),
        warnings=("当前不能直接运行",),
        inputs=(),
        outputs=(),
        examples=(),
        depends_on=("future-task-v0.1",),
    )


@pytest.mark.asyncio
async def test_strategy_manager_lists_describes_and_runs_registered_strategy() -> None:
    """验证可运行策略完整链路，输入为 fake strategy，输出为目录、详情和运行结果断言。"""
    strategy = _FakeStrategy()
    manager = AgentWorkflowStrategyManager(context_factory=_context_factory)
    manager.register(strategy)

    catalog = manager.list_strategies()
    assert [entry.mode for entry in catalog] == ["fake"]
    assert catalog[0].title == "测试策略"
    assert catalog[0].runnable is True

    description = manager.describe_strategy("fake")
    assert description.summary.startswith("用于验证")
    assert description.inputs[0].description == "测试输入值。"

    result = await manager.run_strategy(
        WorkflowRunRequest(
            mode="fake",
            parent_session_id="parent-session",
            payload={"value": "ok"},
            source="unit-test",
        )
    )

    assert result == {"workflow_id": "wf-test", "payload": {"value": "ok"}}
    assert strategy.context is not None
    assert strategy.context.parent_session_id == "parent-session"
    assert strategy.payload == {"value": "ok"}


def test_strategy_manager_rejects_duplicate_modes() -> None:
    """验证重复 mode 拒绝注册，输入为两次相同策略注册，输出为 ValueError 断言。"""
    manager = AgentWorkflowStrategyManager(context_factory=_context_factory)
    manager.register(_FakeStrategy())

    with pytest.raises(ValueError, match="already registered"):
        manager.register(_FakeStrategy())


def test_strategy_manager_unknown_mode_reports_available_and_runnable_modes() -> None:
    """验证未知 mode 错误信息，输入为缺失策略 ID，输出为可用和可运行策略列表断言。"""
    manager = AgentWorkflowStrategyManager(context_factory=_context_factory)
    manager.register(_FakeStrategy())
    manager.register_planned(_planned_description())

    with pytest.raises(WorkflowStrategyNotFound) as exc_info:
        manager.describe_strategy("missing")

    error = exc_info.value
    assert error.mode == "missing"
    assert error.available_modes == ("fake", "planned")
    assert error.runnable_modes == ("fake",)
    assert error.operation == "describe"


@pytest.mark.asyncio
async def test_strategy_manager_describes_planned_strategy_but_refuses_run() -> None:
    """验证 planned 策略只提供说明，输入为 planned mode，输出为不可运行错误断言。"""
    manager = AgentWorkflowStrategyManager(context_factory=_context_factory)
    manager.register(_FakeStrategy())
    manager.register_planned(_planned_description())

    planned = manager.describe_strategy("planned")
    assert planned.status == "planned"
    assert planned.runnable is False
    assert planned.depends_on == ("future-task-v0.1",)

    with pytest.raises(WorkflowStrategyNotRunnable) as exc_info:
        await manager.run_strategy(
            WorkflowRunRequest(
                mode="planned",
                parent_session_id="parent-session",
                payload={},
                source="unit-test",
            )
        )

    error = exc_info.value
    assert error.status == "planned"
    assert error.depends_on == ("future-task-v0.1",)
    assert error.runnable_modes == ("fake",)


def test_agent_workflow_manager_registers_parallel_strategy_catalog(tmp_path: Path) -> None:
    """验证 workflow manager 默认注册 parallel 策略，输入为临时配置，输出为目录和详情断言。"""
    cfg = Config(
        model=ModelConfig(
            name="fake-model",
            base_url="http://127.0.0.1:1234/v1",
            api_key="",
        )
    )
    cfg.session.file_store_path = str(tmp_path / "sessions")
    manager = AgentWorkflowManager(
        subagents=object(),  # type: ignore[arg-type]
        config=cfg,
        workspace_root=tmp_path,
    )

    catalog = manager.list_workflow_strategies()
    assert [entry.mode for entry in catalog] == ["map_reduce", "parallel"]
    map_reduce = catalog[0]
    assert map_reduce.title == "Map-Reduce 代码分析"
    assert map_reduce.status == "available"
    assert map_reduce.runnable is True

    description = manager.describe_workflow_strategy("parallel")
    assert description.inputs[0].name == "task_specs"
    assert "互不依赖" in description.summary

    map_reduce_description = manager.describe_workflow_strategy("map_reduce")
    assert map_reduce_description.status == "available"
    assert map_reduce_description.runnable is True
