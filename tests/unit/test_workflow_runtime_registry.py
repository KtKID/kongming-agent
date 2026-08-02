"""workflow 运行时注册表与 cancel 收口 TDD 测试。

验证目标：
1. manager 维护运行中 workflow 的注册表（list_active_workflows）
2. cancel_workflow(id) 能从外部停掉指定 workflow，不存在的 id 返回 False
3. cancel 后 manifest 收口为 cancelled（不再卡 running），写 workflow_cancelled 审计
4. workflow 结束（正常或 cancel）后从注册表清理

本文件是 TDD 红灯起点：先描述目标行为，实现前应全红。
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

import pytest

from application.agent_roles import AgentRoleManager
from application.agent_workflows.manager import AgentWorkflowManager
from application.agent_workflows.models import ActiveWorkflowHandle
from application.agent_workflows.strategies.base import WorkflowRunRequest
from core.agent_spec import AgentSpec
from core.contracts import LLMRequest, LLMResponse
from core.runner import Runner
from infrastructure.config.model_catalog_manager import ModelCatalogManager
from infrastructure.config.models import Config, ModelSelectionConfig
from runtime_assembly.session_engine import SessionEngine
from sessions import SessionBootstrap, build_session
from tests.support.workflow_agent_tree import (
    WorkflowAgentTreeBinding,
    bind_workflow_agent_tree,
)
from tools import AutoAllowApproval, ToolRegistry


class _HangingLLM:
    """子 agent LLM stub：complete 永远 await，模拟长任务，唯一退出 = CancelledError。"""

    def __init__(self) -> None:
        self.calls: list[LLMRequest] = []

    async def complete(self, request: LLMRequest) -> LLMResponse:
        self.calls.append(request)
        await asyncio.Event().wait()
        raise RuntimeError("unreachable")


def _config(tmp_path: Path) -> Config:
    """构造测试配置，输入为临时目录，输出为 file session + auto_allow 配置。"""
    cfg = Config(model=ModelSelectionConfig(preset_id="local-gemma-4-e4b-it"))
    cfg.session.backend = "file"
    cfg.session.file_store_path = str(tmp_path / "sessions")
    cfg.approval.mode = "auto_allow"
    return cfg


def _runtime(tmp_path: Path, llm: Any) -> SessionEngine:
    """构造测试 SessionEngine，输入为临时目录和 LLM，输出为可跑子 agent 的 runtime。"""
    cfg = _config(tmp_path)
    bootstrap = SessionBootstrap(
        agent_name="test-agent",
        model_name="gemma-4-e4b-it",
        instruction_sources=[],
        instruction_text_hash="test",
        created_at=1.0,
        cwd=str(tmp_path),
    )

    def session_factory(sid: str) -> Any:
        return build_session(cfg, sid, bootstrap=bootstrap)

    catalog_manager = ModelCatalogManager()
    model_config = catalog_manager.resolve_runtime(cfg.model)
    return SessionEngine(
        config=cfg,
        runner=Runner(),
        llm=llm,
        tools=ToolRegistry(),
        enabled_tool_names=[],
        approval=AutoAllowApproval(),
        session_factory=session_factory,
        event_sinks=[],
        model_catalog_manager=catalog_manager,
        model_config=model_config,
        agent_spec=AgentSpec(
            name="parent",
            instructions="parent instructions",
            default_model="gemma-4-e4b-it",
        ),
    )


def _manager(
    tmp_path: Path,
    llm: Any,
) -> tuple[AgentWorkflowManager, WorkflowAgentTreeBinding, SessionEngine]:
    """构造真实 AgentManager workflow 树，输入为目录/LLM，输出 manager/binding/runtime。"""
    runtime = _runtime(tmp_path, llm)
    binding = bind_workflow_agent_tree(runtime, parent_session_id="parent-active")
    return (
        AgentWorkflowManager(
            runtime=runtime,
            agent_manager=binding.manager,
            config=_config(tmp_path),
            workspace_root=tmp_path / "workspace",
            role_manager=AgentRoleManager(role_dir=tmp_path / "roles"),
        ),
        binding,
        runtime,
    )


def _parallel_request(
    parent_agent: dict[str, object],
    parent_session_id: str = "parent-active",
) -> WorkflowRunRequest:
    """构造并行 workflow 请求，输入为父会话，输出为最小 parallel WorkflowRunRequest。"""
    return WorkflowRunRequest(
        mode="parallel",
        parent_session_id=parent_session_id,
        payload={
            "task_specs": [
                {
                    "task_id": "hang-1",
                    "task_name": "hang task",
                    "prompt": "hang forever",
                }
            ]
        },
        source="unit-test",
        parent_agent=parent_agent,
    )


# ---------------------------------------------------------------------------
# 1. list_active_workflows：运行中可见，结束后不可见
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_active_workflow_visible_during_run_then_cleared(tmp_path: Path) -> None:
    """workflow 运行中 list_active_workflows 能查到，结束后查不到。"""
    manager, binding, runtime = _manager(tmp_path, _HangingLLM())

    # 运行中：注册表应非空
    run_task = asyncio.create_task(manager.run_workflow(_parallel_request(binding.parent_agent)))
    await asyncio.sleep(0.3)  # 等子 agent 进入 hang

    active = manager.list_active_workflows()
    assert len(active) == 1, f"运行中应登记 1 个 workflow，实际 {len(active)}"
    handle = active[0]
    assert isinstance(handle, ActiveWorkflowHandle)
    assert handle.mode == "parallel"
    assert handle.parent_session_id == "parent-active"
    workflow_id = handle.workflow_id
    assert workflow_id, "workflow_id 必须在 task 发起时就可用"

    # cancel 收尾
    await manager.cancel_workflow(workflow_id)
    with pytest.raises(asyncio.CancelledError):
        await run_task

    # 结束后：注册表应清空
    assert manager.list_active_workflows() == (), "结束后应从注册表清理"
    await binding.aclose()
    await runtime.aclose()


# ---------------------------------------------------------------------------
# 2. cancel_workflow：存在返回 True，不存在返回 False
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_cancel_workflow_returns_true_for_active_false_for_unknown(
    tmp_path: Path,
) -> None:
    """cancel_workflow 命中运行中 workflow 返回 True，未知 id 返回 False。"""
    manager, binding, runtime = _manager(tmp_path, _HangingLLM())
    run_task = asyncio.create_task(manager.run_workflow(_parallel_request(binding.parent_agent)))
    await asyncio.sleep(0.3)

    active = manager.list_active_workflows()
    assert len(active) == 1
    workflow_id = active[0].workflow_id

    ok = await manager.cancel_workflow(workflow_id)
    assert ok is True, "cancel 运行中 workflow 应返回 True"

    # 不存在的 id
    ok2 = await manager.cancel_workflow("wf-does-not-exist")
    assert ok2 is False, "cancel 不存在的 id 应返回 False"

    with pytest.raises(asyncio.CancelledError):
        await run_task
    await binding.aclose()
    await runtime.aclose()


# ---------------------------------------------------------------------------
# 3. cancel 后 manifest 收口为 cancelled + 审计事件
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_cancel_finalizes_manifest_as_cancelled(tmp_path: Path) -> None:
    """cancel 后 workflow.json 状态为 cancelled，不再卡 running，并写 workflow_cancelled 审计。"""
    manager, binding, runtime = _manager(tmp_path, _HangingLLM())
    run_task = asyncio.create_task(manager.run_workflow(_parallel_request(binding.parent_agent)))
    await asyncio.sleep(0.3)

    active = manager.list_active_workflows()
    assert len(active) == 1
    workflow_id = active[0].workflow_id

    await manager.cancel_workflow(workflow_id)
    with pytest.raises(asyncio.CancelledError):
        await run_task

    # 定位 workflow 目录并断言 manifest 收口
    workflow_dir = tmp_path / "sessions" / "parent-active" / "agent-workflows" / workflow_id
    manifest_path = workflow_dir / "workflow.json"
    assert manifest_path.is_file(), f"manifest 应存在: {manifest_path}"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    assert manifest["status"] == "cancelled", (
        f"cancel 后 manifest 应为 cancelled，实际 {manifest['status']!r}"
    )

    # 审计应含 workflow_cancelled 事件
    audit_path = workflow_dir / "audit.jsonl"
    assert audit_path.is_file(), "audit.jsonl 应存在"
    audit_records = [
        json.loads(line) for line in audit_path.read_text(encoding="utf-8").splitlines()
    ]
    actions = [record["action"] for record in audit_records]
    assert "workflow_cancelled" in actions, (
        f"cancel 应写 workflow_cancelled 审计事件，实际 actions={actions}"
    )

    # manifest.finished_at 与审计 cancelled_at 必须是同一时刻（收口时间戳一致性）
    cancelled_record = next(r for r in audit_records if r["action"] == "workflow_cancelled")
    assert manifest["finished_at"] == cancelled_record["payload"]["cancelled_at"], (
        "cancel 收口的 manifest.finished_at 与审计 cancelled_at 应共用同一时间戳，"
        f"实际 manifest={manifest['finished_at']!r} audit={cancelled_record['payload']['cancelled_at']!r}"
    )
    await binding.aclose()
    await runtime.aclose()


# ---------------------------------------------------------------------------
# 4. 正常完成后也从注册表清理
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_completed_workflow_cleared_from_registry(tmp_path: Path) -> None:
    """正常完成的 workflow 也应从注册表清理（不止 cancel 路径）。"""
    manager, binding, runtime = _manager(tmp_path, _HangingLLM())
    run_task = asyncio.create_task(manager.run_workflow(_parallel_request(binding.parent_agent)))
    await asyncio.sleep(0.3)
    workflow_id = manager.list_active_workflows()[0].workflow_id

    await manager.cancel_workflow(workflow_id)
    with pytest.raises(asyncio.CancelledError):
        await run_task

    assert manager.list_active_workflows() == ()
    await binding.aclose()
    await runtime.aclose()
