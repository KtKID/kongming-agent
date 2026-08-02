"""SessionEngine 审批单入口静态合同测试。

本脚本验证生产合同已经移除 run 级 ApprovalProvider 行为对象。作用是让后续重构无法
重新引入 ``SpawnAgentRequest → AgentCell → HostDispatcher`` 的审批透传链。
关键执行流程：检查公开签名与 dataclass 字段，再扫描限定生产文件中的旧符号。
"""

from __future__ import annotations

import inspect
from dataclasses import fields
from pathlib import Path

from mypy import api as mypy_api

from application.agents.cell import AgentCell
from application.agents.subagent_tools import SpawnAgentRequest
from runtime_assembly.session_engine import SessionEngine

_ROOT = Path(__file__).resolve().parents[2]


def test_approval_behavior_objects_are_absent_from_agent_run_contracts() -> None:
    """SessionEngine、SpawnAgentRequest 与 AgentCell 均不暴露审批覆盖字段。"""
    assert "approval" not in inspect.signature(SessionEngine.run).parameters
    assert "approval" not in {field.name for field in fields(SpawnAgentRequest)}
    assert "run_approval" not in {field.name for field in fields(AgentCell)}


def test_production_child_chain_contains_no_legacy_approval_propagation() -> None:
    """限定生产链源码不含旧字段、旧 provider 或 HostDispatcher approval keyword。"""
    production_files = (
        _ROOT / "src/application/agents/cell.py",
        _ROOT / "src/application/agents/manager.py",
        _ROOT / "src/application/agents/subagent_tools.py",
        _ROOT / "src/application/agent_workflows/manager.py",
        _ROOT / "src/application/subagents/manager.py",
        _ROOT / "src/application/subagents/permissions.py",
        _ROOT / "src/hosts/shared/host_dispatcher.py",
    )
    source = "\n".join(path.read_text(encoding="utf-8") for path in production_files)

    assert "run_approval" not in source
    assert "SubAgentApprovalProvider" not in source
    assert 'run_kwargs["approval"]' not in source


def test_mypy_rejects_session_engine_run_approval_keyword() -> None:
    """运行 mypy 负例 fixture，确认旧 approval keyword 产生 call-arg 诊断。"""
    fixture = _ROOT / "tests/typing/session_engine_approval_override_invalid.py"
    stdout, stderr, status = mypy_api.run(
        [
            str(fixture),
            "--config-file",
            str(_ROOT / "pyproject.toml"),
            "--no-incremental",
        ]
    )

    diagnostics = f"{stdout}\n{stderr}"
    assert status == 1
    assert 'Unexpected keyword argument "approval" for "run" of "SessionEngine"' in diagnostics
