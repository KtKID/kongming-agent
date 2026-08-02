"""Slash catalog manager/provider 单元测试。"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from application.agent_workflows.strategies.description import WorkflowStrategyCatalogEntry
from commands.models import CommandDefinition
from commands.registry import CommandRegistry
from hosts.web.slash_catalog.manager import SlashCatalogManager
from hosts.web.slash_catalog.models import SlashCatalogBackendKind, SlashCatalogContext
from hosts.web.slash_catalog.providers import (
    CommandCatalogProvider,
    SkillCatalogProvider,
    WorkflowCatalogProvider,
)
from infrastructure.config.models import Config, ModelSelectionConfig
from prompting.skills.skill_loader import SkillSpec


def _context(
    tmp_path: Path,
    *,
    thread_id: str | None = None,
    backend_kind: SlashCatalogBackendKind | None = "generic_chat",
) -> SlashCatalogContext:
    """构造测试上下文，输入为临时目录，输出为 SlashCatalogContext。"""
    cfg = Config(model=ModelSelectionConfig(preset_id="local-gemma-4-e4b-it"))
    cfg.session.file_store_path = str(tmp_path / "sessions")
    return SlashCatalogContext(
        home=tmp_path / "home",
        workspace=tmp_path,
        config=cfg,
        thread_id=thread_id,
        backend_kind=backend_kind,
    )


@pytest.mark.asyncio
async def test_manager_lists_groups_in_provider_order(tmp_path: Path) -> None:
    """验证首层分组顺序，输入为默认 providers，输出为 workflow/command/skill。"""
    manager = SlashCatalogManager(
        providers=(
            SkillCatalogProvider(loader=_empty_skill_loader),
            WorkflowCatalogProvider(strategy_catalog_loader=_fake_strategy_catalog),
            CommandCatalogProvider(registry=CommandRegistry([_command("/clear", "web")])),
        )
    )

    groups = await manager.list_groups(_context(tmp_path))

    assert [group.id for group in groups] == ["workflow", "command", "skill"]
    assert groups[0].item_count == 1


@pytest.mark.asyncio
async def test_workflow_provider_projects_strategy_and_completed_runs(tmp_path: Path) -> None:
    """验证 workflow provider 读取策略和已完成 run，输入为真实 viewer 文件，输出为两个 item。"""
    thread_id = "thread-aaaaaaaaaaaa"
    _write_workflow(
        tmp_path, thread_id=thread_id, workflow_id="wf-20260618T000000-abcd", status="completed"
    )
    _write_workflow(
        tmp_path, thread_id=thread_id, workflow_id="wf-20260618T000001-efgh", status="running"
    )
    provider = WorkflowCatalogProvider(strategy_catalog_loader=_fake_strategy_catalog)

    result = await provider.list_items(_context(tmp_path, thread_id=thread_id))

    assert [(item.kind, item.section_id) for item in result.items] == [
        ("workflow_strategy", "registered"),
        ("workflow_run", "completed"),
    ]
    run_item = result.items[1]
    assert run_item.metadata["workflow_id"] == "wf-20260618T000000-abcd"
    assert run_item.insert_text == "/workflow-run wf-20260618T000000-abcd "
    assert run_item.reference_template is not None
    assert run_item.reference_template.kind == "workflow_run"
    assert run_item.reference_template.activation == "open_viewer"


@pytest.mark.asyncio
async def test_command_provider_uses_web_visibility(tmp_path: Path) -> None:
    """验证 command provider 只读取 Web 可见命令，输入为三类 command，输出为 web/both。"""
    provider = CommandCatalogProvider(
        registry=CommandRegistry(
            [
                _command("/web", "web"),
                _command("/both", "both"),
                _command("/cli", "cli"),
            ]
        )
    )

    result = await provider.list_items(_context(tmp_path))

    assert [item.slash for item in result.items] == ["/web", "/both"]
    assert all(item.kind == "command" for item in result.items)
    assert result.items[0].reference_template is not None
    assert result.items[0].reference_template.activation == "execute_command"


@pytest.mark.asyncio
async def test_evolve_command_is_visible_and_inserts_control_command_text(tmp_path: Path) -> None:
    """默认 command catalog 暴露 /evolve，选择后写入命令文本供 Web 控制面执行。"""
    provider = CommandCatalogProvider()

    result = await provider.list_items(_context(tmp_path))

    evolve = next(item for item in result.items if item.slash == "/evolve")
    assert evolve.title == "进化复盘"
    assert evolve.action == "insert_text"
    assert evolve.insert_text == "/evolve "
    assert evolve.reference_template is None
    assert evolve.metadata["executor_key"] == "evolution_review"
    assert evolve.metadata["accepts_args"] is False


@pytest.mark.asyncio
@pytest.mark.parametrize("backend_kind", [None, "claude_code", "codex"])
async def test_evolve_command_is_hidden_outside_generic_chat(
    tmp_path: Path,
    backend_kind: SlashCatalogBackendKind | None,
) -> None:
    """`/evolve` 只投影到具备控制面拦截的 generic_chat Composer。"""
    provider = CommandCatalogProvider()

    result = await provider.list_items(
        _context(tmp_path, thread_id="thread-aabbccddeeff", backend_kind=backend_kind)
    )

    assert all(item.slash != "/evolve" for item in result.items)


@pytest.mark.asyncio
async def test_skill_provider_uses_loader_result(tmp_path: Path) -> None:
    """验证 skill provider 读取 loader 输出，输入为 fake skill，输出为 skill item。"""
    provider = SkillCatalogProvider(loader=_fake_skill_loader)

    result = await provider.list_items(_context(tmp_path))

    assert [item.id for item in result.items] == ["skill:review"]
    assert result.items[0].insert_text == "/review "
    assert result.items[0].action == "bind_reference"
    assert result.items[0].reference_template is not None
    assert result.items[0].reference_template.ref == "skill:review"
    assert result.items[0].reference_template.activation == "inject_context"
    assert result.items[0].reference_template.source_ref == str(
        tmp_path / ".kongming" / "skills" / "review" / "SKILL.md"
    )
    assert result.items[0].reference_template.metadata["body_path"] == str(
        tmp_path / ".kongming" / "skills" / "review" / "SKILL.md"
    )
    assert result.items[0].metadata["source"] == "workspace"


@pytest.mark.asyncio
async def test_legacy_projection_keeps_command_priority_on_conflict(tmp_path: Path) -> None:
    """验证 legacy 投影冲突优先级，输入为同名 command/skill，输出为 command 条目。"""
    manager = SlashCatalogManager(
        providers=(
            CommandCatalogProvider(registry=CommandRegistry([_command("/review", "web")])),
            SkillCatalogProvider(loader=_fake_skill_loader),
        )
    )

    candidates = await manager.list_legacy_candidates(_context(tmp_path))

    assert candidates == [
        {
            "slash": "/review",
            "title": "Review",
            "description": "Review command",
            "source": "command",
        }
    ]


def _fake_strategy_catalog() -> tuple[WorkflowStrategyCatalogEntry, ...]:
    """构造 fake workflow strategy，输入为空，输出为策略目录。"""
    return (
        WorkflowStrategyCatalogEntry(
            mode="fake",
            title="Fake Workflow",
            summary="Fake workflow summary",
            status="available",
            runnable=True,
        ),
    )


def _command(slash: str, visibility: str) -> CommandDefinition:
    """构造 command definition，输入为 slash/visibility，输出为 CommandDefinition。"""
    name = slash.lstrip("/")
    return CommandDefinition(
        id=name,
        slash=slash,
        title=f"{name.title()}",
        description=f"{name.title()} command",
        kind="action",
        host_visibility=visibility,  # type: ignore[arg-type]
        accepts_args=False,
        executor_key=name,
    )


async def _empty_skill_loader(
    _home: Path,
    _workspace: Path | None,
) -> tuple[SkillSpec, ...]:
    """返回空 skill 列表，输入为路径，输出为空元组。"""
    return ()


async def _fake_skill_loader(
    _home: Path,
    workspace: Path | None,
) -> tuple[SkillSpec, ...]:
    """返回 fake skill，输入为路径，输出为 SkillSpec。"""
    root = workspace or Path(".")
    return (
        SkillSpec(
            name="review",
            description="Review skill",
            when_to_use=None,
            allowed_tools=None,
            paths=None,
            body_path=root / ".kongming" / "skills" / "review" / "SKILL.md",
            source="workspace",
        ),
    )


def _write_workflow(
    tmp_path: Path,
    *,
    thread_id: str,
    workflow_id: str,
    status: str,
) -> None:
    """写入 viewer 可读取的 workflow 产物，输入为状态，输出为临时文件树。"""
    workflow_dir = tmp_path / "sessions" / thread_id / "agent-workflows" / workflow_id
    workflow_dir.mkdir(parents=True)
    workflow_payload = {
        "workflow_id": workflow_id,
        "mode": "parallel",
        "parent_session_id": thread_id,
        "started_at": "2026-06-18T00:00:00+00:00",
        "finished_at": "2026-06-18T00:01:00+00:00" if status == "completed" else None,
        "desc": f"{status} workflow",
        "status": status,
        "assigned_agents": [],
    }
    result_payload = {
        "workflow_id": workflow_id,
        "mode": "parallel",
        "parent_session_id": thread_id,
        "completed": status == "completed",
    }
    index_payload = {
        "workflow_id": workflow_id,
        "parent_session_id": thread_id,
        "mode": "parallel",
        "status": status,
        "reports": [],
    }
    (workflow_dir / "workflow.json").write_text(
        json.dumps(workflow_payload),
        encoding="utf-8",
    )
    (workflow_dir / "result.json").write_text(json.dumps(result_payload), encoding="utf-8")
    (workflow_dir / "audit.jsonl").write_text("", encoding="utf-8")
    (workflow_dir / "reports").mkdir()
    (workflow_dir / "reports" / "index.json").write_text(
        json.dumps(index_payload),
        encoding="utf-8",
    )
