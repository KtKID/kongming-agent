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

设计约束：

- 协议真源只在 :mod:`core.contracts`，本包里**不**重定义 Protocol。
- 本包不 import ``safety/`` / ``host/`` / ``cli/`` / ``executors/`` / ``context/`` /
  ``observability/`` 下任何模块，硬约束由 import-linter 背书。
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any, cast

from core.contracts import EventSink, Tool
from tools.approval import (
    AutoAllowApproval,
    AutoDenyApproval,
    InteractiveApproval,
    PromptFn,
    build_default_approval,
)
from tools.base import BaseBuiltinTool
from tools.file_tools import (
    ListDirTool,
    ReadFileTool,
    WriteFileTool,
    build_file_tools,
)
from tools.registry import ToolRegistry
from tools.shell_tool import ShellTool, build_shell_tool


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

    装配层（例如 ``executors/agent_runtime/native_runtime.py``）只需要读配置
    里的 ``tool.file.enabled`` / ``tool.shell.enabled`` 以及运行参数，
    然后把它们喂进来即可，不用在别的地方再写第二份"默认工具清单"。

    v0.1.6 扩展：``skill_specs`` 为非空 ``Mapping`` 时注册 :class:`SkillTool`；
    ``None`` 表示当前装配未启用 skill 系统，不注册（保持 v0.1.5 行为）。

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


__all__ = [
    "AutoAllowApproval",
    "AutoDenyApproval",
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
]
