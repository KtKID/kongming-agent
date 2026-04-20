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
) -> ToolRegistry:
    """按 v1-mini 默认工具集组装一个 :class:`ToolRegistry`。

    装配层（例如 ``executors/agent_runtime/native_runtime.py``）只需要读配置
    里的 ``tool.file.enabled`` / ``tool.shell.enabled``，然后把这两个布尔值
    喂进来即可，不用在别的地方再写第二份"默认工具清单"。

    Args:
        file_enabled: 是否注册文件类工具。对应 ``config.tool.file.enabled``。
        shell_enabled: 是否注册 shell 工具。对应 ``config.tool.shell.enabled``。

    Returns:
        已经注册好 builtin 工具的 :class:`ToolRegistry`。
    """
    tools = [
        *build_file_tools(enabled=file_enabled),
        *build_shell_tool(enabled=shell_enabled),
    ]
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
