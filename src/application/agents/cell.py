"""AgentCell — agent 实例（模块 C · Mailbox + agent_loop 的数据载体）。

功能：定义 agent 树里单个 agent 实例的运行时态 :class:`AgentCell`，承载该
agent 的标识、树结构坐标、lifecycle、串行 mailbox（``asyncio.Queue[Mail]``）
与 cancel 靶子 ``run_task``。
作用：让 agent_loop 有一个明确的消费主体（``cell.mailbox`` / ``cell.state`` /
``cell.run_task``）；mailbox 把外部世界并发压成该 agent 的严格串行输入流，
``run_task`` 是 cancel_subtree 的砍靶子。
关键设计要点：
- **单 agent 退化形态（task-4）**：``parent_id=None`` / ``depth=0`` /
  ``lifecycle="persistent"`` / ``child_ids=[]``。spawn（task-5）才填充子字段。
- **不持有 tree 反向引用**：AgentCell 不指向 :class:`ThreadCell`，避免循环依赖
  （``ThreadCell.root_agent -> AgentCell`` 若 AgentCell 又指 ThreadCell 则成环）。
  epoch 经 :class:`Mail.epoch` 携带 + agent_loop 启动参数 ``current_tree_epoch`` 传入；
  registry 操作走 AgentManager 门户（task-5），task-4 单 agent 退化直接用 ThreadCell.registry。
- mailbox 用 ``asyncio.Queue[Mail]``：每 agent 一个，结构必然单 run（run 只能
  从队头取一条消息开始），无需锁。
- ``state`` idle↔running↔closed：persistent agent 任何终态回 idle 不进 closed。

事实源：``docs/spec/agent-tree-v0.1/04-data-and-state.md``（AgentCell 定义 +
状态流转）。
"""

from __future__ import annotations

import asyncio
import os
import uuid
from dataclasses import dataclass, field
from typing import Any, Literal

from core.agent_spec import AgentSpec
from core.contracts import Tool
from core.lifecycle import LifecycleHook
from core.mail import Mail

# AgentCell.state 的封闭枚举。
AgentState = Literal["idle", "running", "closed"]

# AgentCell.lifecycle 的封闭枚举。
AgentLifecycle = Literal["persistent", "single_shot"]


def _new_agent_id() -> str:
    """生成唯一 agent_id（``uuid4().hex[:8]``），输入为空，输出为 8 位 hex。"""
    return uuid.uuid4().hex[:8]


@dataclass
class AgentCell:
    """agent 实例（运行时树态，内存瞬态，进程重启即重建）。

    单 agent 退化形态（task-4）：``parent_id=None`` / ``depth=0`` /
    ``lifecycle="persistent"`` / ``child_ids=[]``。spawn（task-5）才变非退化。

    Attributes:
        agent_id: agent 唯一标识（``uuid4().hex[:8]``）。AgentManager.register
            校验树内唯一（task-5）。
        parent_id: 父 agent_id；主 agent = ``None``，子 = 父 agent_id。
        child_ids: 子 agent_id 列表。task-4 单 agent 退化 = 空列表。
        depth: 树深度。v1 ≤ 1；主 agent = 0。
        spec: 复用 :class:`AgentSpec`（name / instructions / default_model /
            tool_names / max_turns / metadata / reasoning_effort）。
        skill_names: 该 agent 启用的技能名元组（透传给装配层）。
        session_id: 该 agent 独立的 session id（主 agent = thread_id）。
        cwd: 工作目录。主 agent = 进程 cwd；子 agent = ``agents/<task_run_id>/work/``。
        role_id: 角色身份查询键（主 agent = ``None``；子 = AgentRolePreset 查询键）。
        lifecycle: ``persistent``（主 agent，常驻不销毁）/ ``single_shot``（子 agent，
            终态 + 无存活子孙 → close_cell）。
        state: ``idle`` ↔ ``running`` ↔ ``closed``。mailbox.get() 取到消息 → running；
            run 处理完 → idle；single_shot 终态且无子孙 → closed。persistent 不进 closed。
        mailbox: 该 agent 的串行输入队列（``asyncio.Queue[Mail]``）。每 agent 一个，
            结构必然单 run，无需锁。
        run_task: 当前正在执行的 run asyncio.Task（cancel 靶子）。``None`` = idle。
        run_enabled_tools: 本 agent 的 run 级工具覆盖；``None`` 表示按 ``spec.tool_names``
            解析，普通子 agent 使用该模式，scoped workflow 子 agent 使用 wrapped tools。
        run_lifecycle_hooks: 本 agent 的 run 级生命周期 hook；scoped workflow 子 agent
            用于工具审计，其它场景为空。
        run_llm_request_metadata: 本 agent 的 provider 级请求 metadata 覆盖。
    """

    agent_id: str
    parent_id: str | None
    child_ids: list[str]
    depth: int
    spec: AgentSpec
    skill_names: tuple[str, ...]
    session_id: str
    cwd: str
    role_id: str | None
    lifecycle: AgentLifecycle
    state: AgentState = "idle"
    mailbox: asyncio.Queue[Mail] = field(default_factory=asyncio.Queue)
    run_task: asyncio.Task[object] | None = None
    run_enabled_tools: tuple[Tool, ...] | None = None
    run_lifecycle_hooks: tuple[LifecycleHook, ...] = ()
    run_max_tokens: int | None = None
    run_temperature: float | None = None
    run_timeout_seconds: float | None = None
    run_llm_request_metadata: dict[str, Any] = field(default_factory=dict)
    # 不持有 tree 反向引用（避免循环依赖）：
    # epoch 经 Mail.epoch 携带 + agent_loop 启动参数 current_tree_epoch 传入；
    # registry 操作走 AgentManager 门户（task-5）。


def make_root_agent_cell(
    *,
    spec: AgentSpec,
    session_id: str,
    skill_names: tuple[str, ...] = (),
    agent_id: str | None = None,
    cwd: str | None = None,
    enabled_tools: tuple[Tool, ...] | None = None,
) -> AgentCell:
    """构造主 agent（树根）AgentCell（单 agent 退化形态）。

    主 agent 固定退化值：``parent_id=None`` / ``depth=0`` / ``lifecycle="persistent"``
    / ``role_id=None`` / ``child_ids=[]``。spawn 子 agent（task-5）由 AgentManager
    构造非退化 AgentCell。

    Args:
        spec: 主 agent 的 :class:`AgentSpec`（装配层从 runtime 拿）。
        session_id: 主 agent 的 session id（= thread_id）。
        skill_names: 启用的技能名元组，默认空。
        agent_id: 可选 agent_id；None 时自动生成 ``uuid4().hex[:8]``。
        cwd: 工作目录；None 时取进程 cwd（``os.getcwd()``）。
        enabled_tools: 主 Agent 默认 run 的实际工具快照；None 时由查找面按 spec 解析。

    Returns:
        退化形态的主 agent AgentCell（state=idle，空 mailbox，run_task=None）。
    """
    return AgentCell(
        agent_id=agent_id or _new_agent_id(),
        parent_id=None,
        child_ids=[],
        depth=0,
        spec=spec,
        skill_names=tuple(skill_names),
        session_id=session_id,
        cwd=cwd if cwd is not None else os.getcwd(),
        role_id=None,
        lifecycle="persistent",
        state="idle",
        run_enabled_tools=enabled_tools,
    )


__all__ = ["AgentCell", "AgentLifecycle", "AgentState", "make_root_agent_cell"]
