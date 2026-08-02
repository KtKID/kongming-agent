"""Web slash catalog providers。"""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Sequence
from pathlib import Path
from typing import Any

from application.agent_workflows.manager import AgentWorkflowManager
from application.agent_workflows.strategies.description import WorkflowStrategyCatalogEntry
from commands.registry import CommandRegistry, build_builtin_registry
from hosts.web.protocol.conversation_references import ConversationReferenceTemplate
from hosts.web.slash_catalog.models import (
    SlashCatalogContext,
    SlashCatalogDiagnosticDTO,
    SlashCatalogItemDTO,
    SlashCatalogProvider,
    SlashCatalogProviderResult,
)
from hosts.web.workflow_viewer import WorkflowRunViewerManager
from prompting.skills.skill_loader import SkillSpec, load_skill_specs

WorkflowStrategyCatalogLoader = Callable[[], Sequence[WorkflowStrategyCatalogEntry]]
WorkflowViewerFactory = Callable[[SlashCatalogContext], WorkflowRunViewerManager]
SkillSpecLoader = Callable[[Path, Path | None], Awaitable[Sequence[SkillSpec]]]


class WorkflowCatalogProvider:
    """读取 workflow strategy 和 completed workflow run 的 catalog provider。"""

    group_id = "workflow"
    title = "Workflow"
    description = "Registered workflow strategies and completed runs"
    order = 10

    def __init__(
        self,
        *,
        strategy_catalog_loader: WorkflowStrategyCatalogLoader | None = None,
        viewer_factory: WorkflowViewerFactory | None = None,
    ) -> None:
        """初始化 provider，输入为可替换依赖，输出为 workflow catalog provider。"""
        self._strategy_catalog_loader = (
            strategy_catalog_loader or AgentWorkflowManager.list_default_workflow_strategies
        )
        self._viewer_factory = viewer_factory or _default_workflow_viewer_factory

    async def list_items(self, context: SlashCatalogContext) -> SlashCatalogProviderResult:
        """读取 workflow 候选项，输入为上下文，输出为策略和 completed run 列表。"""
        items: list[SlashCatalogItemDTO] = []
        diagnostics: list[SlashCatalogDiagnosticDTO] = []
        try:
            for index, entry in enumerate(self._strategy_catalog_loader()):
                items.append(_workflow_strategy_item(entry, order=index))
        except Exception as exc:
            diagnostics.append(
                SlashCatalogDiagnosticDTO(
                    code="workflow_strategy.load_failed",
                    severity="error",
                    message=f"{type(exc).__name__}: {exc}",
                )
            )

        if context.thread_id:
            try:
                viewer = self._viewer_factory(context)
                workflow_list = viewer.list_workflows(context.thread_id)
                for index, workflow in enumerate(workflow_list.workflows):
                    if workflow.status != "completed":
                        continue
                    items.append(_workflow_run_item(workflow, order=1000 + index))
            except Exception as exc:
                diagnostics.append(
                    SlashCatalogDiagnosticDTO(
                        code="workflow_history.load_failed",
                        severity="warning",
                        message=f"{type(exc).__name__}: {exc}",
                    )
                )
        return SlashCatalogProviderResult(items=tuple(items), diagnostics=tuple(diagnostics))


class CommandCatalogProvider:
    """读取 Web slash command 注册表的 catalog provider。"""

    group_id = "command"
    title = "Command"
    description = "Web slash commands"
    order = 20

    def __init__(self, registry: CommandRegistry | None = None) -> None:
        """初始化 provider，输入为命令注册表，输出为 command catalog provider。"""
        self._registry = registry or build_builtin_registry()

    async def list_items(self, context: SlashCatalogContext) -> SlashCatalogProviderResult:
        """读取 Web 可见 command，输入为上下文，输出为 command items。"""
        items = [
            SlashCatalogItemDTO(
                id=f"command:{command.slash}",
                group_id=self.group_id,
                kind="command",
                title=command.title,
                description=command.description,
                source_ref=f"command:{command.id}",
                order=index,
                slash=command.slash,
                insert_text=f"{command.slash} ",
                action=(
                    "insert_text"
                    if command.executor_key == "evolution_review"
                    else "bind_reference"
                ),
                reference_template=(
                    None
                    if command.executor_key == "evolution_review"
                    else ConversationReferenceTemplate(
                        kind="command",
                        ref=f"command:{command.id}",
                        label=command.title,
                        activation="execute_command",
                        source_ref=f"command:{command.id}",
                        args={"slash": command.slash},
                        metadata={
                            "kind": command.kind,
                            "accepts_args": command.accepts_args,
                            "executor_key": command.executor_key,
                        },
                    )
                ),
                metadata={
                    "command_id": command.id,
                    "kind": command.kind,
                    "accepts_args": command.accepts_args,
                    "executor_key": command.executor_key,
                },
            )
            for index, command in enumerate(self._registry.list_commands("web"))
            if command.executor_key != "evolution_review" or context.backend_kind == "generic_chat"
        ]
        return SlashCatalogProviderResult(items=tuple(items))


class SkillCatalogProvider:
    """读取 home/workspace skill specs 的 catalog provider。"""

    group_id = "skill"
    title = "Skill"
    description = "Loaded skills"
    order = 30

    def __init__(self, loader: SkillSpecLoader | None = None) -> None:
        """初始化 provider，输入为 skill loader，输出为 skill catalog provider。"""
        self._loader = loader or _default_skill_loader

    async def list_items(self, context: SlashCatalogContext) -> SlashCatalogProviderResult:
        """读取 skill specs，输入为上下文，输出为 skill items 和诊断。"""
        try:
            specs = await self._loader(context.home, context.workspace)
        except Exception as exc:
            return SlashCatalogProviderResult(
                diagnostics=(
                    SlashCatalogDiagnosticDTO(
                        code="skill.load_failed",
                        severity="warning",
                        message=f"{type(exc).__name__}: {exc}",
                    ),
                )
            )
        items = [
            SlashCatalogItemDTO(
                id=f"skill:{spec.name}",
                group_id=self.group_id,
                kind="skill",
                title=spec.name,
                description=spec.description,
                source_ref=f"skill:{spec.source}:{spec.name}",
                order=index,
                slash=f"/{spec.name}",
                insert_text=f"/{spec.name} ",
                action="bind_reference",
                reference_template=ConversationReferenceTemplate(
                    kind="skill",
                    ref=f"skill:{spec.name}",
                    label=spec.name,
                    activation="inject_context",
                    source_ref=str(spec.body_path),
                    metadata={
                        "name": spec.name,
                        "source": spec.source,
                        "body_path": str(spec.body_path),
                    },
                ),
                metadata={
                    "name": spec.name,
                    "source": spec.source,
                    "body_path": str(spec.body_path),
                    "allowed_tools": spec.allowed_tools,
                    "paths": spec.paths,
                },
            )
            for index, spec in enumerate(specs)
        ]
        return SlashCatalogProviderResult(items=tuple(items))


def build_default_providers() -> tuple[SlashCatalogProvider, ...]:
    """构建默认 provider 列表，输入为空，输出为 workflow/command/skill provider。"""
    return (
        WorkflowCatalogProvider(),
        CommandCatalogProvider(),
        SkillCatalogProvider(),
    )


async def _default_skill_loader(home: Path, workspace: Path | None) -> Sequence[SkillSpec]:
    """调用真实 skill loader，输入为 home/workspace，输出为 SkillSpec 列表。"""
    return await load_skill_specs(home, workspace=workspace, event_sinks=[])


def _default_workflow_viewer_factory(context: SlashCatalogContext) -> WorkflowRunViewerManager:
    """构建 workflow viewer，输入为 catalog context，输出为只读 viewer manager。"""
    if context.config is None:
        raise ValueError("slash catalog context requires config for workflow history")
    return WorkflowRunViewerManager(config=context.config, workspace_root=context.workspace)


def _workflow_strategy_item(
    entry: WorkflowStrategyCatalogEntry,
    *,
    order: int,
) -> SlashCatalogItemDTO:
    """投影 workflow strategy，输入为策略目录项，输出为 catalog item。"""
    return SlashCatalogItemDTO(
        id=f"workflow:{entry.mode}",
        group_id="workflow",
        kind="workflow_strategy",
        title=entry.title,
        description=entry.summary,
        source_ref=f"workflow_strategy:{entry.mode}",
        order=order,
        section_id="registered",
        insert_text=f"/workflow {entry.mode} " if entry.runnable else None,
        action="bind_reference",
        reference_template=ConversationReferenceTemplate(
            kind="workflow_strategy",
            ref=f"workflow_strategy:{entry.mode}",
            label=entry.title,
            activation="start_workflow" if entry.runnable else "guide_payload",
            source_ref=f"workflow_strategy:{entry.mode}",
            args={"mode": entry.mode},
            metadata={
                "mode": entry.mode,
                "status": entry.status,
                "runnable": entry.runnable,
            },
        ),
        enabled=entry.runnable,
        metadata={
            "mode": entry.mode,
            "status": entry.status,
            "runnable": entry.runnable,
        },
    )


def _workflow_run_item(workflow: Any, *, order: int) -> SlashCatalogItemDTO:
    """投影 completed workflow run，输入为 viewer item，输出为 catalog item。"""
    return SlashCatalogItemDTO(
        id=f"workflow-run:{workflow.workflow_id}",
        group_id="workflow",
        kind="workflow_run",
        title=workflow.title,
        description=workflow.desc or f"{workflow.mode} completed workflow",
        source_ref=f"workflow_run:{workflow.thread_id}:{workflow.workflow_id}",
        order=order,
        section_id="completed",
        insert_text=f"/workflow-run {workflow.workflow_id} ",
        action="open_viewer",
        reference_template=ConversationReferenceTemplate(
            kind="workflow_run",
            ref=f"workflow_run:{workflow.workflow_id}",
            label=workflow.title,
            activation="open_viewer",
            source_ref=f"workflow_run:{workflow.thread_id}:{workflow.workflow_id}",
            args={
                "thread_id": workflow.thread_id,
                "workflow_id": workflow.workflow_id,
            },
            metadata={
                "mode": workflow.mode,
                "status": workflow.status,
            },
        ),
        metadata={
            "workflow_id": workflow.workflow_id,
            "thread_id": workflow.thread_id,
            "mode": workflow.mode,
            "status": workflow.status,
            "started_at": workflow.started_at,
            "finished_at": workflow.finished_at,
            "report_count": workflow.report_count,
        },
    )


__all__ = [
    "CommandCatalogProvider",
    "SkillCatalogProvider",
    "SlashCatalogProvider",
    "SlashCatalogProviderResult",
    "WorkflowCatalogProvider",
    "build_default_providers",
]
