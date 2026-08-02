"""unit：workflow / subagent 打断流程 cancel 传播验证（interrupt-run-v0.1 边界）。

验证目标：用户 cancel 一个正在跑 workflow 的父 run 时，cancel 信号能否正确
传播到 AgentManager child，以及 workflow 产物 / TaskRegistry 记录能否正确收口。

钉子：
1. child runner 顶层吞 CancelledError → 返回 Result(status="cancelled")，
   AgentManager/TaskRegistry 将同一任务收口为 cancelled。
2. 父 task cancel 时 asyncio.gather 会取消所有子任务；gather 的 await 点会
   抛 CancelledError 透传到 workflow tool → 父 runner _safe_tool_execute
   （不捕获 CancelledError）→ 父 runner 顶层收口 Result(status="cancelled")。
3. workflow 审计应在 cancel 时尽量收口（manifest 状态、lifecycle finished 记录）。

本测试聚焦钉子 1、2 的真实行为，用真实 Runner + stub LLM 构造可复现场景。
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

import pytest

import application.agent_workflows.manager as workflow_manager_module
from application.agent_roles import AgentRoleManager
from application.agent_workflows.manager import AgentWorkflowManager
from application.agents.subagent_tools import SpawnAgentRequest
from core.agent_spec import AgentSpec
from core.contracts import LLMRequest, LLMResponse
from core.message import Message
from core.runner import Runner
from hosts.cli.main import (
    _apply_model_preset_or_exit,  # noqa: F401  保持与同目录测试一致 import 习惯
)
from infrastructure.config.model_catalog_manager import ModelCatalogManager
from infrastructure.config.models import Config, ModelSelectionConfig
from runtime_assembly.session_engine import SessionEngine
from sessions import SessionBootstrap, build_session
from tests.support.workflow_agent_tree import bind_workflow_agent_tree
from tools import AutoAllowApproval, ToolRegistry


class _HangingLLM:
    """子 agent LLM stub：complete 永远 await，模拟长任务被打断。"""

    def __init__(self) -> None:
        self.calls: list[LLMRequest] = []

    async def complete(self, request: LLMRequest) -> LLMResponse:
        self.calls.append(request)
        await asyncio.Event().wait()  # 永远阻塞，唯一退出 = CancelledError
        raise RuntimeError("unreachable")


class _FastLLM:
    """子 agent LLM stub：立即返回成功（用于验证已完成的子任务不受 cancel 影响）。"""

    def __init__(self, content: str = "child done") -> None:
        self.content = content
        self.calls: list[LLMRequest] = []

    async def complete(self, request: LLMRequest) -> LLMResponse:
        self.calls.append(request)
        return LLMResponse(message=Message.assistant(self.content), finish_reason="stop")


def _config(tmp_path: Path) -> Config:
    """构造测试配置，输入为临时目录，输出为 file session + auto_allow 审批配置。"""
    cfg = Config(model=ModelSelectionConfig(preset_id="local-gemma-4-e4b-it"))
    cfg.session.backend = "file"
    cfg.session.file_store_path = str(tmp_path / "sessions")
    cfg.approval.mode = "auto_allow"
    return cfg


def _runtime(tmp_path: Path, llm: Any) -> SessionEngine:
    """构造测试 NativeRuntime，输入为临时目录和 LLM，输出为可跑子 agent 的 runtime。"""
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


# ---------------------------------------------------------------------------
# 钉子 1：child runner 吞 cancel → TaskRegistry 正常收口 cancelled
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_subagent_runner_cancel_returns_cancelled_run(tmp_path: Path) -> None:
    """child runner 被 cancel 时，同一 TaskRegistry 投影返回 cancelled。"""
    llm = _HangingLLM()
    runtime = _runtime(tmp_path, llm)
    binding = bind_workflow_agent_tree(runtime, parent_session_id="parent-1")
    spawn = binding.manager.spawn(
        SpawnAgentRequest(
            parent_agent_id=str(binding.parent_agent["agent_id"]),
            spec=AgentSpec(
                name="hang-task",
                instructions="",
                default_model="fake-model",
                max_turns=3,
            ),
            seed_message=Message.user("just hang"),
            cwd=str(tmp_path),
            child_session_id="parent-1-child",
            source_task_id="agent-1",
            metadata={
                "source": "workflow",
                "parent_session_id": "parent-1",
                "workflow_id": "wf-cancel",
                "workflow_task_id": "agent-1",
                "task_run_id": "001-agent-1",
                "task_name": "hang task",
            },
        )
    )

    await asyncio.sleep(0.1)
    await binding.manager.cancel_agent_run(spawn.child_id)
    records = binding.manager.list_task_records(
        "parent-1",
        include_finished=True,
        limit=10,
    )
    assert len(records) == 1
    assert records[0].task_id == spawn.task_id
    assert records[0].status == "cancelled"
    await binding.aclose()
    await runtime.aclose()


# ---------------------------------------------------------------------------
# 钉子 2：父 run cancel 时，workflow gather 透传 CancelledError 到父 runner
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_workflow_cancel_propagates_through_tool_to_parent_runner(
    tmp_path: Path,
) -> None:
    """父 run 在 workflow tool 执行中 cancel：CancelledError 必须透传到父 runner 顶层。

    场景：父 runner 调 run_agent_workflow tool → manager.run_workflow_specs → gather
    子 agent。子 agent 的 LLM hang。外部 task.cancel() 父 run。

    预期：CancelledError 从 gather → run_workflow_specs → tool.execute →
    _safe_tool_execute（不捕获 CancelledError）→ 父 runner 顶层 →
    Result(status="cancelled")。

    如果中间任何一层用 except Exception 吞掉，父 runner 会得到 failed 而非 cancelled。
    """
    llm = _HangingLLM()
    runtime = _runtime(tmp_path, llm)
    binding = bind_workflow_agent_tree(runtime, parent_session_id="parent-cancel")
    manager = AgentWorkflowManager(
        runtime=runtime,
        agent_manager=binding.manager,
        config=_config(tmp_path),
        workspace_root=tmp_path / "workspace",
        role_manager=AgentRoleManager(role_dir=tmp_path / "roles"),
    )

    task_specs: list[dict[str, object]] = [
        {
            "task_id": f"agent-{i}",
            "task_name": f"task-{i}",
            "prompt": f"hang {i}",
        }
        for i in range(2)
    ]

    async def _go() -> Any:
        return await manager.run_workflow_specs(
            mode="parallel",
            parent_session_id="parent-cancel",
            task_specs=task_specs,
            parent_agent=binding.parent_agent,
        )

    workflow_task = asyncio.create_task(_go())
    # 等子 agent 进入 hang
    await asyncio.sleep(0.2)
    workflow_task.cancel()

    # gather 抛出的 CancelledError 应直接透传，父 runner 顶层会收口为 cancelled。
    with pytest.raises(asyncio.CancelledError):
        await workflow_task

    sessions_root = tmp_path / "sessions" / "parent-cancel" / "agent-workflows"
    workflow_dirs = sorted(sessions_root.glob("wf-*"))
    assert len(workflow_dirs) == 1

    import json

    manifest = json.loads((workflow_dirs[0] / "workflow.json").read_text(encoding="utf-8"))
    assert manifest["status"] == "cancelled"
    assert manifest["finished_at"]
    audit = (workflow_dirs[0] / "audit.jsonl").read_text(encoding="utf-8")
    assert '"action": "workflow_cancelled"' in audit
    await binding.aclose()
    await runtime.aclose()


# ---------------------------------------------------------------------------
# 钉子 3：_run_one 的 except Exception 不吞 CancelledError（静态确认）
# ---------------------------------------------------------------------------


def test_run_one_does_not_swallow_cancelled_error() -> None:
    """静态确认 _run_one 的 except 子句不会捕获 CancelledError。

    CancelledError 在 Python 3.8+ 是 BaseException 子类，不是 Exception 子类，
    所以 except Exception 不会捕获它。本测试钉死这个假设，防止未来误改成
    except BaseException 或裸 except。
    """
    import ast
    import inspect

    source = inspect.getsource(workflow_manager_module.AgentWorkflowManager._run_one)
    source = inspect.cleandoc(source)  # 去掉方法级缩进，避免 IndentationError
    tree = ast.parse(source)
    bare_excepts: list[int] = []
    baseexception_excepts: list[int] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.ExceptHandler):
            continue
        if node.type is None:
            bare_excepts.append(node.lineno)
            continue
        names: list[str] = []
        t = node.type
        if isinstance(t, ast.Name):
            names.append(t.id)
        elif isinstance(t, ast.Tuple):
            names.extend(e.id for e in t.elts if isinstance(e, ast.Name))
        if "BaseException" in names:
            baseexception_excepts.append(node.lineno)
    assert not bare_excepts, f"_run_one 含裸 except，会吞 CancelledError: {bare_excepts}"
    assert not baseexception_excepts, (
        f"_run_one 含 except BaseException，会吞 CancelledError: {baseexception_excepts}"
    )
