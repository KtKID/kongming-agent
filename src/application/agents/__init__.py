"""agent 树态 owner 域：TaskRegistry 账本 + AgentCell + AgentManager（task-5）。

本包是 agent-tree-v0.1 的树态 owner 落点：
- ``registry.py``：TaskRegistry 边界类（资源/生命周期域账本），登记 agent_run
  asyncio.Task 与 external_process PID，提供 ``cancel_subtree`` 后序砍靶 +
  关门标志防漏杀；单 agent 退化形态下等价于原 Web InterruptFrame 直接 cancel。
- ``cell.py``：AgentCell dataclass（agent 实例运行时态）+ ``make_root_agent_cell``
  主 agent 工厂；承载 mailbox（``asyncio.Queue[Mail]``）与 run_task（cancel 靶）。
- ``manager.py``（task-5）：AgentManager 树态门户（spawn / cancel_subtree 编排）。

设计决策：agent 生命周期全部收敛在 ``agents/``，workflow 只持有策略和值对象。
"""

from __future__ import annotations

from application.agents.cell import AgentCell, make_root_agent_cell
from application.agents.loop import DeliverSink, MailRunBridge, RootAgentDeliverSink, agent_loop
from application.agents.registry import (
    PidHandle,
    TaskIdentity,
    TaskProjection,
    TaskRecord,
    TaskRegistry,
    TaskStatus,
)
from application.agents.subagent_tools import (
    SpawnAgentRequest,
    build_child_agent_spec,
    build_spawn_request_from_tool_args,
    build_spawn_request_from_workflow_task,
    parent_agent_id_from_snapshot,
)

__all__ = [
    "AgentCell",
    "DeliverSink",
    "MailRunBridge",
    "PidHandle",
    "RootAgentDeliverSink",
    "SpawnAgentRequest",
    "TaskIdentity",
    "TaskProjection",
    "TaskRecord",
    "TaskRegistry",
    "TaskStatus",
    "agent_loop",
    "build_child_agent_spec",
    "build_spawn_request_from_tool_args",
    "build_spawn_request_from_workflow_task",
    "make_root_agent_cell",
    "parent_agent_id_from_snapshot",
]
