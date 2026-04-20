"""统一配置加载层。

kongming-agent 的**唯一**配置入口。其他模块（executors / host / cli / tests）
通过 :func:`load_config` 拿 :class:`Config`，不允许自己读 YAML 或环境变量。

为什么独立成顶层包而不是放在 `core/` 里：

- `core/` 是跨模块共享语言真源，只能定义协议和核心数据结构，不包含具体实现能力。
- 配置加载本质上是 I/O（读文件、读环境变量），和 provider / tool / observability
  并列，属于"把 agent 跑起来"所需的基础设施。
- 其它批次的装配层（executors.agent_runtime / host / cli）都会消费这里的
  :class:`Config`，如果塞进 core 就会让 core 变成一个事实上的应用层。

本包唯一对外暴露的符号是 :func:`load_config` 和 :class:`Config`（以及若干
子模型和 :class:`ConfigLoadError` / :class:`ConfigValidationError`）。
"""

from __future__ import annotations

from config_loader.errors import ConfigLoadError, ConfigValidationError
from config_loader.loader import load_config
from config_loader.models import (
    ApprovalConfig,
    Config,
    FileToolConfig,
    HostConfig,
    LoggingConfig,
    ModelConfig,
    RunnerConfig,
    SessionConfig,
    ShellToolConfig,
    ToolConfig,
    TraceConfig,
)

__all__ = [
    "ApprovalConfig",
    "Config",
    "ConfigLoadError",
    "ConfigValidationError",
    "FileToolConfig",
    "HostConfig",
    "LoggingConfig",
    "ModelConfig",
    "RunnerConfig",
    "SessionConfig",
    "ShellToolConfig",
    "ToolConfig",
    "TraceConfig",
    "load_config",
]
