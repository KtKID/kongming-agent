"""Workflow 测试用真实 AgentManager 装配。

本模块把测试 SessionEngine 接入生产 AgentManager、AgentCell、agent_loop 和
TaskRegistry，只省略常驻 root loop，避免它与 workflow demux 竞争消费父 mailbox。
关键执行流程：创建 root cell → 注入 child mail bridge → 真实 spawn child →
SessionEngine.run → ChildDeliverSink 回灌父 mailbox。
关键函数：bind_workflow_agent_tree 构造绑定；WorkflowAgentTreeBinding.aclose
收口仍存活的 child loop。
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import Any

from application.agents.cell import AgentCell, make_root_agent_cell
from application.agents.loop import MailRunBridge
from application.agents.manager import AgentManager, ChildDeliverSink, SpawnContext
from application.agents.registry import TaskRegistry
from core.mail import Mail
from core.result import Result
from runtime_assembly.session_engine import SessionEngine


@dataclass(frozen=True)
class WorkflowAgentTreeBinding:
    """保存 workflow 测试使用的真实 AgentManager 与父 agent 快照。"""

    manager: AgentManager
    parent_agent: dict[str, object]

    async def aclose(self) -> None:
        """收口残留 child loop，输入为空，输出为所有后台任务结束。"""
        tasks = tuple(self.manager._loop_tasks)
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)


def bind_workflow_agent_tree(
    runtime: SessionEngine,
    *,
    parent_session_id: str = "parent-session",
) -> WorkflowAgentTreeBinding:
    """把测试 runtime 绑定到真实 AgentManager，输入为 runtime，输出为 workflow 树。"""
    registry = TaskRegistry()
    root = make_root_agent_cell(
        spec=runtime.agent_spec,
        session_id=parent_session_id,
        enabled_tools=runtime.enabled_tools_snapshot,
    )

    def mail_run_bridge_builder(child: AgentCell) -> MailRunBridge:
        """构造 child run 闭包，输入为 child cell，输出为 SessionEngine 调用桥。"""

        async def run_child(mail_text: str, *, mail: Mail) -> Result:
            """执行单条 child mail，输入为文本和信封，输出为 runtime Result。"""
            return await runtime.run(
                mail_text,
                session_id=child.session_id,
                agent_spec=child.spec,
                max_turns=child.spec.max_turns,
                enabled_tools=child.run_enabled_tools,
                lifecycle_hooks=child.run_lifecycle_hooks,
                max_tokens=child.run_max_tokens,
                temperature=child.run_temperature,
                timeout_seconds=child.run_timeout_seconds,
                llm_request_metadata=child.run_llm_request_metadata,
                event_context={
                    "mail_kind": mail.kind,
                    "mail_task_id": mail.task_id,
                    "mail_epoch": mail.epoch,
                },
                thread_id=parent_session_id,
                agent_id=child.agent_id,
            )

        return run_child

    def deliver_sink_builder(
        child: AgentCell,
        task_id: str,
        parent_mailbox: asyncio.Queue[Any],
    ) -> ChildDeliverSink:
        """构造 child 上投 sink，输入为 child/任务/父 mailbox，输出为生产 sink。"""
        return ChildDeliverSink(
            child=child,
            task_id=task_id,
            parent_mailbox=parent_mailbox,
            parent_agent_id=root.agent_id,
        )

    manager = AgentManager(
        SpawnContext(
            mail_run_bridge_builder=mail_run_bridge_builder,
            deliver_sink_builder=deliver_sink_builder,
            current_epoch_getter=lambda: 0,
            registry=registry,
            tool_lookup=runtime.tools,
        )
    )
    manager._cells[root.agent_id] = root
    manager._root_agent_id = root.agent_id
    return WorkflowAgentTreeBinding(
        manager=manager,
        parent_agent={
            "agent_id": root.agent_id,
            "session_id": root.session_id,
            "model": root.spec.default_model,
        },
    )


__all__ = ["WorkflowAgentTreeBinding", "bind_workflow_agent_tree"]
