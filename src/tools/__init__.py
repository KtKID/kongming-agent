"""v1-mini Tool Runtime 公共 API。

本包提供：

- :class:`BaseBuiltinTool`：builtin tool 的便利基类（不是 ``Tool`` Protocol 真源）。
- :class:`ToolRegistry`：tool 注册与分发容器，天然兼容
  :class:`core.contracts.ToolLookup` Protocol。
- 三种 :class:`core.contracts.ApprovalProvider` 默认实现：
  :class:`InteractiveApproval` / :class:`AutoAllowApproval` /
  :class:`AutoDenyApproval`，以及工厂 :func:`build_default_approval`。
- 三个最小 builtin tool：:class:`ReadFileTool` / :class:`WriteFileTool` /
  :class:`ListDirTool`，配合工厂 :func:`build_file_tools`。
- 一个最小 shell tool：:class:`ShellTool`，配合工厂 :func:`build_shell_tool`。
- :func:`build_default_registry`：一把梭地按配置开关装好整套 builtin 工具。
- :func:`register_schedule_tool_if_enabled`：可选 helper，按 ``cfg.scheduler.enabled``
  外部 register schedule_tool；与 ``build_memory_tool`` 同款"外部 register"模式。

设计约束：

- 协议真源只在 :mod:`core.contracts`，本包里**不**重定义 Protocol。
- 本包不 import ``safety/`` / ``host/`` / ``cli/`` / ``executors/`` / ``sessions/`` / ``prompting/`` /
  ``infrastructure.tracing/`` 下任何模块，硬约束由 import-linter 背书。
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import TYPE_CHECKING, Any, cast

from core.contracts import EventSink, Tool
from tools.agent_role_tool import AgentRoleManagerLike, build_agent_role_tools
from tools.agent_workflow_tool import (
    AgentWorkflowHandle,
    build_agent_workflow_tool,
    build_run_agent_workflow_tool,
)
from tools.builtin.file_tool import (
    ListDirTool,
    ReadFileTool,
    WriteFileTool,
    build_file_tools,
)
from tools.builtin.shell_tool import ShellTool, build_shell_tool
from tools.runtime.approval import (
    AutoAllowApproval,
    AutoDenyApproval,
    InteractiveApproval,
    PromptFn,
    build_default_approval,
)
from tools.runtime.base import BaseBuiltinTool
from tools.runtime.registry import ToolRegistry

if TYPE_CHECKING:
    from evolution.store import EvolutionStore
    from infrastructure.config.models import Config
    from scheduler.store import Store


def build_default_registry(
    *,
    file_enabled: bool = True,
    shell_enabled: bool = True,
    shell_timeout_seconds: float = 30.0,
    shell_max_stream_bytes: int = 8000,
    shell_terminate_grace_seconds: float = 2.0,
    file_read_max_bytes: int = 65536,
    skill_specs: Mapping[str, Any] | None = None,
    skill_event_sinks: Sequence[EventSink] = (),
) -> ToolRegistry:
    """按 v1-mini 默认工具集组装一个 :class:`ToolRegistry`。

    装配层（例如 ``runtime_assembly/native_runtime.py``）只需要读配置
    里的 ``tool.file.enabled`` / ``tool.shell.enabled`` 以及运行参数，
    然后把它们喂进来即可，不用在别的地方再写第二份"默认工具清单"。

    v0.1.6 扩展：``skill_specs`` 为非空 ``Mapping`` 时注册 :class:`SkillTool`；
    ``None`` 表示当前装配未启用 skill 系统，不注册（保持 v0.1.5 行为）。

    v0.2 起本函数**不再**直接装配 schedule_tool / memory_tool。两者改走外部
    register 模式：调用方在 ``build_default_registry`` 之后用
    :func:`register_schedule_tool_if_enabled` / :func:`tools.builtin.memory_tool.build_memory_tool`
    把工具 register 到 registry 上。这样 cli/web 装配链路单一、调用方不需要为
    每个可选工具拖一串 kwargs。

    Args:
        file_enabled: 是否注册文件类工具。对应 ``config.tool.file.enabled``。
        shell_enabled: 是否注册 shell 工具。对应 ``config.tool.shell.enabled``。
        shell_timeout_seconds: shell 命令默认超时秒数。
        shell_max_stream_bytes: stdout/stderr 各自最大字节数。
        shell_terminate_grace_seconds: 超时后 terminate 到 kill 之间等待秒数。
        file_read_max_bytes: ReadFileTool 默认读取上限字节数。
        skill_specs: 由 ``skill-loader-v0.1.6`` 装载得到的 ``name → SkillSpec``
            映射。``None`` 不注册；空 ``dict`` 也不注册（避免暴露空工具给模型）。
        skill_event_sinks: SkillTool 装配期 fan-out 用的事件 sink 序列；与
            runner 的 ``event_sinks`` 共享同一组 sink 即可。

    Returns:
        已经注册好 builtin 工具的 :class:`ToolRegistry`。
    """
    tools: list[Tool] = [
        *build_file_tools(
            enabled=file_enabled,
            read_max_bytes=file_read_max_bytes,
        ),
        *build_shell_tool(
            enabled=shell_enabled,
            default_timeout=shell_timeout_seconds,
            max_stream_bytes=shell_max_stream_bytes,
            terminate_grace_seconds=shell_terminate_grace_seconds,
        ),
    ]
    if skill_specs:
        from tools.builtin.skill_tool import SkillTool

        # SkillTool 用 ``Final[str]`` 锁定 name/description；mypy 据此不认其
        # 满足 ``Tool`` Protocol 的可变 attribute 约定（runtime 行为正确，
        # 仅类型层差异），这里用 cast 显式承担该类型转换语义。
        tools.append(cast(Tool, SkillTool(specs=skill_specs, event_sinks=skill_event_sinks)))

    return ToolRegistry(tools)


def register_schedule_tool_if_enabled(
    registry: ToolRegistry,
    cfg: Config,
    *,
    runtime_factory_fn: Any | None = None,
) -> Store | None:
    """按 ``cfg.scheduler.enabled`` 外部 register schedule_tool。

    与 :func:`tools.builtin.memory_tool.build_memory_tool` 同款"外部 register"模式：
    调用方先用 :func:`build_default_registry` 拿到 registry，再调本 helper
    决定是否补一个 schedule_tool。这样 cli / web 装配代码可以共用同一行调用，
    避免每条入口都重复写 if-cfg.scheduler.enabled 分支。

    注意：本 helper **lazy import** ``scheduler.*`` 与
    :func:`tools.builtin.schedule_tool.build_schedule_tool`；``cfg.scheduler.enabled=False``
    时不会触碰 cron 模块，启动开销保持原样。

    Args:
        registry: 已由 ``build_default_registry`` 装配好的注册表。
        cfg: 全局 :class:`Config`。
        runtime_factory_fn: 可选 callable，签名 ``(store) -> (runtime, bridge)``；
            ``run_now`` action 用。``None`` 时 ``run_now`` 直接报错。

    Returns:
        构造的 :class:`scheduler.store.Store` 实例；``None`` 表示 cron 关闭未注册。
        调用方需保留该引用供 ticker / lifespan 复用同一份 store
        （若 home 一致，文件落盘 + file lock 也能保证一致性）。
    """
    if not cfg.scheduler.enabled:
        return None

    from infrastructure.config.paths import get_kongming_home
    from scheduler.store import Store
    from tools.builtin.schedule_tool import build_schedule_tool

    home = cfg.scheduler.home if cfg.scheduler.home is not None else (get_kongming_home() / "cron")
    store = Store(home)
    # v0.3：把 cfg.scheduler 的默认 timezone / delivery channel 透传给 schedule_tool，
    # 让 LLM 创建任务时不必（也不应）猜时区，dispatcher 也不会因 delivery=None SKIPPED。
    registry.register(
        cast(
            Tool,
            build_schedule_tool(
                store,
                runtime_factory_fn=runtime_factory_fn,
                default_timezone=cfg.scheduler.default_timezone,
                default_delivery_channel=cfg.scheduler.default_delivery_channel,
            ),
        )
    )
    return store


def register_evolution_write_tool_if_enabled(
    registry: ToolRegistry,
    cfg: Config,
    *,
    event_sinks: Sequence[EventSink] = (),
) -> EvolutionStore | None:
    """按 ``cfg.evolution.learning.enabled`` 外部 register evolution_write。"""
    if not cfg.evolution.learning.enabled:
        return None

    from evolution.state_store import EvolutionStateStore
    from evolution.store import EvolutionStore, resolve_evolution_root
    from tools.builtin.evolution_write_tool import build_evolution_write_tool

    root_dir = resolve_evolution_root(cfg.evolution.learning.root_path)
    state_store = EvolutionStateStore(root_dir)
    store = EvolutionStore(
        root_dir=root_dir,
        state_store=state_store,
        event_sinks=tuple(event_sinks),
    )
    registry.register(
        cast(
            Tool,
            build_evolution_write_tool(
                store,
                min_confidence=cfg.evolution.learning.nutrient_confidence_threshold,
                max_nutrients=cfg.evolution.learning.max_nutrients,
                event_sinks=event_sinks,
            ),
        )
    )
    return store


def register_agent_workflow_tool(
    registry: ToolRegistry,
    handle: AgentWorkflowHandle,
) -> None:
    """Register the agent workflow tool with a late-bound manager handle."""
    registry.register(cast(Tool, build_run_agent_workflow_tool(handle)))
    registry.register(cast(Tool, build_agent_workflow_tool(handle)))


def register_agent_role_tool(
    registry: ToolRegistry,
    manager: AgentRoleManagerLike,
) -> None:
    """Register agent role list/create tools with a shared manager."""
    for tool in build_agent_role_tools(manager):
        registry.register(cast(Tool, tool))


__all__ = [
    "AutoAllowApproval",
    "AutoDenyApproval",
    "AgentWorkflowHandle",
    "BaseBuiltinTool",
    "InteractiveApproval",
    "ListDirTool",
    "PromptFn",
    "ReadFileTool",
    "ShellTool",
    "ToolRegistry",
    "WriteFileTool",
    "build_default_approval",
    "build_default_registry",
    "build_file_tools",
    "build_shell_tool",
    "register_evolution_write_tool_if_enabled",
    "register_agent_workflow_tool",
    "register_agent_role_tool",
    "register_schedule_tool_if_enabled",
]
