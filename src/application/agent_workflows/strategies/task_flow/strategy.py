"""Task Flow workflow 策略实现。

本脚本负责把 LLM 生成的任务计划 payload 转换为可视化 workflow 产物和 session 进度快照。
作用是为通用任务提供轻量计划创建入口，让复杂任务、多方案任务和 Progress task 弹窗共享同一条 workflow 策略链路。
关键执行流程：解析 objective 与 plan.nodes，写入 workflow manifest 和 task_flow artifact，同步 pending 进度项，返回 AgentWorkflowResult。
关键函数：parse_task_flow_spec 校验模型 payload，TaskFlowArtifactWriter.write_all 写入计划产物，TaskFlowStrategy.run 执行策略。
"""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal

from application.agent_workflows.context import WorkflowExecutionContext
from application.agent_workflows.strategies.description import (
    WorkflowStrategyCatalogEntry,
    WorkflowStrategyDescription,
    WorkflowStrategyInputField,
)
from application.subagents.manager import SubAgentTask
from application.subagents.permissions import to_jsonable

TaskFlowInteractionMode = Literal["auto", "guided", "choice_required", "llm_decide"]
TaskFlowNodeStatus = Literal["pending", "in_progress", "completed"]

_INTERACTION_MODES = frozenset({"auto", "guided", "choice_required", "llm_decide"})
_NODE_STATUSES = frozenset({"pending", "in_progress", "completed"})
_MAX_NODES = 128


@dataclass(frozen=True)
class TaskFlowPlanNode:
    """Task Flow 计划节点。"""

    node_id: str
    title: str
    description: str
    depends_on: tuple[str, ...]
    status: TaskFlowNodeStatus
    metadata: dict[str, object]


@dataclass(frozen=True)
class TaskFlowSpec:
    """Task Flow 运行规格。"""

    objective: str
    title: str
    nodes: tuple[TaskFlowPlanNode, ...]
    planning: dict[str, object]
    execution: dict[str, object]
    audit_tags: tuple[str, ...]


@dataclass(frozen=True)
class TaskFlowArtifactPaths:
    """Task Flow 产物路径集合。"""

    spec_path: Path
    plan_path: Path
    progress_path: Path
    nodes_path: Path

    def as_payload(self) -> dict[str, str]:
        """序列化产物路径，输入为路径集合，输出为 JSON 友好的字符串字典。"""
        return {
            "spec_path": str(self.spec_path),
            "plan_path": str(self.plan_path),
            "progress_path": str(self.progress_path),
            "nodes_path": str(self.nodes_path),
        }


class TaskFlowArtifactWriter:
    """写入 task_flow 专属 artifact。"""

    def __init__(self, workflow_dir: Path) -> None:
        """初始化写入器，输入为 workflow 目录，输出为绑定 task_flow 子目录的 writer。"""
        self.root = workflow_dir / "task_flow"

    def write_all(
        self,
        *,
        context: WorkflowExecutionContext,
        spec: TaskFlowSpec,
        tasks: Sequence[SubAgentTask],
        created_at: str,
    ) -> TaskFlowArtifactPaths:
        """写入全部 artifact，输入为上下文、规格和进度任务，输出为产物路径集合。"""
        self.root.mkdir(parents=True, exist_ok=True)
        paths = TaskFlowArtifactPaths(
            spec_path=self.root / "spec.json",
            plan_path=self.root / "plan.json",
            progress_path=self.root / "progress.json",
            nodes_path=self.root / "nodes.jsonl",
        )
        plan_payload = _plan_payload(
            context=context,
            spec=spec,
            tasks=tasks,
            created_at=created_at,
        )
        _write_json(paths.spec_path, _spec_payload(context=context, spec=spec))
        _write_json(paths.plan_path, plan_payload)
        _write_json(paths.progress_path, _progress_payload(plan_payload))
        _write_jsonl(paths.nodes_path, plan_payload["nodes"])
        return paths


class TaskFlowStrategy:
    """通用任务计划创建策略。"""

    mode = "task_flow"

    def __init__(self, manager: Any) -> None:
        """初始化策略，输入为 AgentWorkflowManager facade，输出为可注册策略实例。"""
        self._manager = manager

    def catalog_entry(self) -> WorkflowStrategyCatalogEntry:
        """生成策略目录项，输入为当前策略说明，输出为父 agent 可查看的紧凑条目。"""
        return self.describe().catalog_entry()

    def describe(self) -> WorkflowStrategyDescription:
        """生成中文策略说明，输入为当前策略配置，输出为 LLM 选择和生成 payload 所需的详情。"""
        return WorkflowStrategyDescription(
            mode=self.mode,
            title="任务流 Task Flow",
            status="available",
            runnable=True,
            summary=(
                "通用计划执行 workflow。适合把用户目标拆成可视化步骤并逐步完成；parallel、"
                "map_reduce、deep_research、roundtable_review 均无法精准覆盖时选择它。"
            ),
            when_to_use=(
                "用户给出一个需要多步完成的通用任务目标",
                "任务存在多个可行方案，LLM 需要先向用户询问并等待确认",
                "任务需要 Progress task 弹窗展示简单 workflow",
                "专用 workflow 无法覆盖当前任务形态，需要一个通用计划执行承载层",
            ),
            warnings=(
                "简单任务直接填充 plan.nodes 并调用 run_agent_workflow 创建计划",
                "多方案任务先向用户提问，用户确认后再把所选方案转换为 plan.nodes",
                "计划创建后由主 LLM 按步骤执行，每完成一步调用 update_task_progress",
                "执行中出现意料之外的严重问题时停止推进并询问用户",
            ),
            inputs=(
                WorkflowStrategyInputField(
                    name="objective",
                    required=True,
                    type_label="string",
                    description="用户任务目标，作为计划和进度弹窗的主目标。",
                    example="把当前 Web 界面改造成任务流可视化体验。",
                ),
                WorkflowStrategyInputField(
                    name="planning",
                    required=False,
                    type_label="object",
                    description=(
                        "LLM 规划策略，interaction_mode 支持 auto、guided、"
                        "choice_required、llm_decide。"
                    ),
                    example={
                        "interaction_mode": "llm_decide",
                        "choice_policy": "ask_when_multiple_viable_paths",
                    },
                ),
                WorkflowStrategyInputField(
                    name="plan.nodes",
                    required=True,
                    type_label="array<object>",
                    description="计划节点数组，每个节点包含 id、title、description 和可选 depends_on。",
                    example=[
                        {
                            "id": "step-1",
                            "title": "确认方案",
                            "description": "把用户确认的方案整理为执行计划。",
                        }
                    ],
                ),
                WorkflowStrategyInputField(
                    name="execution",
                    required=False,
                    type_label="object",
                    description="执行约束，默认严重意外问题走 ask_user，进度工具为 update_task_progress。",
                    example={
                        "on_unexpected_severe_issue": "ask_user",
                        "progress_tool": "update_task_progress",
                    },
                ),
            ),
            outputs=(
                "AgentWorkflowResult",
                "root workflow.json",
                "root result.json",
                "reports/index.json",
                "task_flow/spec.json",
                "task_flow/plan.json",
                "task_flow/progress.json",
                "task_flow/nodes.jsonl",
            ),
            examples=(
                {
                    "mode": "task_flow",
                    "payload": {
                        "objective": "改造 Web 任务流可视化体验",
                        "planning": {
                            "interaction_mode": "llm_decide",
                            "choice_policy": "ask_when_multiple_viable_paths",
                        },
                        "plan": {
                            "title": "任务流可视化改造",
                            "nodes": [
                                {
                                    "id": "step-1",
                                    "title": "梳理现有 workflow 入口",
                                    "description": "确认已有策略和工具 schema。",
                                },
                                {
                                    "id": "step-2",
                                    "title": "接入 task_flow",
                                    "description": "注册策略并更新提示词。",
                                    "depends_on": ["step-1"],
                                },
                            ],
                        },
                        "execution": {"on_unexpected_severe_issue": "ask_user"},
                        "audit_tags": ["task_flow"],
                    },
                },
            ),
        )

    async def run(
        self,
        context: WorkflowExecutionContext,
        payload: Mapping[str, object],
    ) -> Any:
        """执行 task_flow，输入为 workflow 上下文和 JSON payload，输出为 AgentWorkflowResult。"""
        spec = parse_task_flow_spec(payload)
        context.audit_writer.write_event(
            {
                "action": "task_flow.workflow_started",
                "payload": {
                    "workflow_id": context.workflow_id,
                    "mode": self.mode,
                    "objective": spec.objective,
                    "node_count": len(spec.nodes),
                    "audit_tags": list(spec.audit_tags),
                },
            }
        )

        tasks = _tasks_from_nodes(spec.nodes)
        self._manager.write_workflow_manifest(context=context, tasks=tasks, status="running")

        finished_at = _now_iso()
        artifact_paths = TaskFlowArtifactWriter(context.workflow_dir).write_all(
            context=context,
            spec=spec,
            tasks=tasks,
            created_at=finished_at,
        )
        report_index_path = self._manager.write_report_index(
            context=context,
            status="plan_created",
            reports=(),
        )
        extra = {
            "task_flow": {
                "objective": spec.objective,
                "title": spec.title,
                "node_count": len(spec.nodes),
                "progress_tool": "update_task_progress",
                "status": "plan_created",
                "artifact_paths": artifact_paths.as_payload(),
                "plan_path": str(artifact_paths.plan_path),
                "progress_path": str(artifact_paths.progress_path),
            }
        }
        self._manager.write_workflow_result(
            context=context,
            finished_at=finished_at,
            completed=True,
            report_index_path=report_index_path,
            reports=(),
            runs=(),
            extra=extra,
        )
        self._manager.write_workflow_manifest(
            context=context,
            tasks=tasks,
            status="plan_created",
            finished_at=finished_at,
        )
        context.audit_writer.write_event(
            {
                "action": "task_flow.plan_created",
                "payload": {
                    "workflow_id": context.workflow_id,
                    "mode": self.mode,
                    "completed": True,
                    "node_count": len(spec.nodes),
                    "plan_path": str(artifact_paths.plan_path),
                    "progress_path": str(artifact_paths.progress_path),
                    "report_index_path": str(report_index_path),
                },
            }
        )

        from application.agent_workflows.manager import AgentWorkflowResult

        return AgentWorkflowResult(
            workflow_id=context.workflow_id,
            mode=self.mode,
            parent_session_id=context.parent_session_id,
            workflow_dir=context.workflow_dir,
            started_at=context.started_at,
            finished_at=finished_at,
            runs=(),
            reports=(),
            report_index_path=report_index_path,
            desc=context.desc,
            data=extra,
            completed_override=True,
        )


def parse_task_flow_spec(payload: Mapping[str, object]) -> TaskFlowSpec:
    """解析 task_flow payload，输入为模型 JSON 参数，输出为已校验的 TaskFlowSpec。"""
    objective = _non_empty_string(
        payload.get("objective", payload.get("goal")),
        "task_flow requires non-empty objective",
    )
    plan = _object_copy(payload.get("plan"))
    if plan is None:
        raise ValueError("task_flow requires plan.nodes")
    title = _optional_string(plan.get("title")) or _optional_string(payload.get("title"))
    if title is None:
        title = "任务执行计划"
    nodes = _parse_nodes(plan.get("nodes", plan.get("steps")))
    planning = _parse_planning(payload.get("planning"))
    execution = _parse_execution(payload.get("execution"))
    audit_tags = tuple(_coerce_string_array(payload.get("audit_tags")))
    return TaskFlowSpec(
        objective=objective,
        title=title,
        nodes=nodes,
        planning=planning,
        execution=execution,
        audit_tags=audit_tags,
    )


def _parse_nodes(value: object) -> tuple[TaskFlowPlanNode, ...]:
    """解析计划节点数组，输入为 plan.nodes 原值，输出为节点元组。"""
    if not isinstance(value, list) or not value:
        raise ValueError("task_flow plan.nodes must be a non-empty array")
    if len(value) > _MAX_NODES:
        raise ValueError(f"task_flow plan.nodes supports at most {_MAX_NODES} items")
    nodes = tuple(_parse_node(item, index=index) for index, item in enumerate(value, 1))
    seen: set[str] = set()
    for node in nodes:
        if node.node_id in seen:
            raise ValueError(f"task_flow plan.nodes contains duplicate id: {node.node_id}")
        seen.add(node.node_id)
    return nodes


def _parse_node(value: object, *, index: int) -> TaskFlowPlanNode:
    """解析单个计划节点，输入为节点 JSON 和序号，输出为 TaskFlowPlanNode。"""
    if not isinstance(value, dict):
        raise ValueError(f"task_flow plan.nodes[{index}] must be an object")
    node_id = _optional_string(value.get("id")) or f"step-{index}"
    title = _optional_string(value.get("title")) or _optional_string(value.get("name"))
    description = _optional_string(value.get("description")) or _optional_string(value.get("desc"))
    if title is None:
        raise ValueError(f"task_flow plan.nodes[{index}].title must be a non-empty string")
    if description is None:
        description = title
    status = _optional_string(value.get("status")) or "pending"
    if status not in _NODE_STATUSES:
        raise ValueError(
            f"task_flow plan.nodes[{index}].status must be pending, in_progress, or completed"
        )
    metadata = _object_copy(value.get("metadata")) or {}
    return TaskFlowPlanNode(
        node_id=node_id,
        title=title,
        description=description,
        depends_on=tuple(_coerce_string_array(value.get("depends_on"))),
        status=status,  # type: ignore[arg-type]
        metadata=metadata,
    )


def _parse_planning(value: object) -> dict[str, object]:
    """解析 planning 配置，输入为任意对象，输出为带默认值的字典。"""
    planning = _object_copy(value) or {}
    interaction_mode = _optional_string(planning.get("interaction_mode")) or "llm_decide"
    if interaction_mode not in _INTERACTION_MODES:
        raise ValueError(
            "task_flow planning.interaction_mode must be auto, guided, "
            "choice_required, or llm_decide"
        )
    planning["interaction_mode"] = interaction_mode
    planning.setdefault("choice_policy", "ask_when_multiple_viable_paths")
    return planning


def _parse_execution(value: object) -> dict[str, object]:
    """解析 execution 配置，输入为任意对象，输出为带默认值的字典。"""
    execution = _object_copy(value) or {}
    execution.setdefault("on_unexpected_severe_issue", "ask_user")
    execution.setdefault("progress_tool", "update_task_progress")
    return execution


def _tasks_from_nodes(nodes: Sequence[TaskFlowPlanNode]) -> list[SubAgentTask]:
    """把计划节点转换为 workflow 进度任务，输入为节点列表，输出为 SubAgentTask 列表。"""
    tasks: list[SubAgentTask] = []
    for index, node in enumerate(nodes, 1):
        task_run_id = _node_task_run_id(index=index, node_id=node.node_id)
        tasks.append(
            SubAgentTask(
                task_id=node.node_id,
                task_name=node.title,
                prompt=node.description,
                context=node.description,
                metadata={
                    "task_run_id": task_run_id,
                    "task_flow_node_id": node.node_id,
                    "task_flow_display_order": index - 1,
                    "task_flow_depends_on": list(node.depends_on),
                },
            )
        )
    return tasks


def _spec_payload(
    *,
    context: WorkflowExecutionContext,
    spec: TaskFlowSpec,
) -> dict[str, object]:
    """生成 spec.json 内容，输入为上下文和规格，输出为 JSON payload。"""
    return {
        "schema_version": 1,
        "workflow_id": context.workflow_id,
        "parent_session_id": context.parent_session_id,
        "mode": context.mode,
        "objective": spec.objective,
        "title": spec.title,
        "planning": to_jsonable(spec.planning),
        "execution": to_jsonable(spec.execution),
        "audit_tags": list(spec.audit_tags),
    }


def _plan_payload(
    *,
    context: WorkflowExecutionContext,
    spec: TaskFlowSpec,
    tasks: Sequence[SubAgentTask],
    created_at: str,
) -> dict[str, object]:
    """生成 plan.json 内容，输入为上下文、规格和任务，输出为 JSON payload。"""
    nodes = [
        _node_payload(node=node, task=tasks[index], display_order=index)
        for index, node in enumerate(spec.nodes)
    ]
    return {
        "schema_version": 1,
        "workflow_id": context.workflow_id,
        "parent_session_id": context.parent_session_id,
        "mode": context.mode,
        "objective": spec.objective,
        "title": spec.title,
        "created_at": created_at,
        "planning": to_jsonable(spec.planning),
        "execution": to_jsonable(spec.execution),
        "nodes": nodes,
    }


def _node_payload(
    *,
    node: TaskFlowPlanNode,
    task: SubAgentTask,
    display_order: int,
) -> dict[str, object]:
    """生成单个节点 payload，输入为节点、任务和展示序号，输出为 JSON payload。"""
    return {
        "id": node.node_id,
        "title": node.title,
        "description": node.description,
        "depends_on": list(node.depends_on),
        "status": node.status,
        "display_order": display_order,
        "task_run_id": task.metadata.get("task_run_id"),
        "metadata": to_jsonable(node.metadata),
    }


def _progress_payload(plan_payload: dict[str, object]) -> dict[str, object]:
    """生成 progress.json 内容，输入为 plan payload，输出为进度 JSON payload。"""
    nodes = plan_payload["nodes"]
    node_items = nodes if isinstance(nodes, list) else []
    counts = {
        "pending": sum(1 for item in node_items if _node_status(item) == "pending"),
        "in_progress": sum(1 for item in node_items if _node_status(item) == "in_progress"),
        "completed": sum(1 for item in node_items if _node_status(item) == "completed"),
        "total": len(node_items),
    }
    return {
        "schema_version": 1,
        "workflow_id": plan_payload["workflow_id"],
        "parent_session_id": plan_payload["parent_session_id"],
        "mode": plan_payload["mode"],
        "objective": plan_payload["objective"],
        "title": plan_payload["title"],
        "progress_tool": "update_task_progress",
        "status": "plan_created",
        "counts": counts,
        "nodes": node_items,
    }


def _node_status(value: object) -> str:
    """读取节点状态，输入为任意节点 payload，输出为状态字符串。"""
    if not isinstance(value, dict):
        return "pending"
    status = value.get("status")
    if isinstance(status, str) and status in _NODE_STATUSES:
        return status
    return "pending"


def _node_task_run_id(*, index: int, node_id: str) -> str:
    """生成节点运行 ID，输入为序号和节点 ID，输出为稳定 task_run_id。"""
    return f"{index:03d}-{_slug(node_id)}"


def _slug(value: str) -> str:
    """生成安全路径片段，输入为任意 ID，输出为 ASCII slug。"""
    safe = "".join(ch if ch.isalnum() or ch in "-_" else "-" for ch in value.strip())
    safe = safe.strip("-_").lower()
    return safe or "step"


def _object_copy(value: object) -> dict[str, object] | None:
    """复制对象字段，输入为任意值，输出为 dict 副本或 None。"""
    if isinstance(value, dict):
        return dict(value)
    return None


def _optional_string(value: object) -> str | None:
    """读取可选字符串，输入为任意值，输出为裁剪后的字符串或 None。"""
    if not isinstance(value, str):
        return None
    stripped = value.strip()
    return stripped or None


def _non_empty_string(value: object, message: str) -> str:
    """读取必填字符串，输入为任意值和错误消息，输出为裁剪后的字符串。"""
    stripped = _optional_string(value)
    if stripped is None:
        raise ValueError(message)
    return stripped


def _coerce_string_array(value: object) -> list[str]:
    """归一化字符串数组，输入为任意模型参数，输出为字符串列表。"""
    if value is None:
        return []
    if isinstance(value, str):
        return [value.strip()] if value.strip() else []
    if isinstance(value, tuple):
        value = list(value)
    if not isinstance(value, list):
        return []
    return [item.strip() for item in value if isinstance(item, str) and item.strip()]


def _write_json(path: Path, payload: dict[str, object]) -> None:
    """原子写入 JSON，输入为路径和 payload，输出为目标文件内容更新。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, default=str),
        encoding="utf-8",
    )
    tmp.replace(path)


def _write_jsonl(path: Path, items: object) -> None:
    """写入 JSONL，输入为路径和条目列表，输出为目标文件内容更新。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    values = items if isinstance(items, list) else []
    tmp = path.with_suffix(path.suffix + ".tmp")
    with open(tmp, "w", encoding="utf-8") as handle:
        for item in values:
            handle.write(json.dumps(item, ensure_ascii=False, default=str) + "\n")
    tmp.replace(path)


def _now_iso() -> str:
    """生成当前时间，输入为空，输出为 UTC ISO 8601 字符串。"""
    return datetime.now(UTC).isoformat()


__all__ = [
    "TaskFlowArtifactWriter",
    "TaskFlowPlanNode",
    "TaskFlowSpec",
    "TaskFlowStrategy",
    "parse_task_flow_spec",
]
