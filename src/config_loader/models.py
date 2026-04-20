"""统一配置模型。

对应 :file:`config/default.yaml` 与 :file:`config/local-model.yaml` 的全部字段。

**严格约束**：

- 所有默认值都集中在这里。业务代码不允许再写死 ``timeout=60`` / ``max_turns=10``
  这类"可调参数"。
- ``ModelConfig`` 区分本地模型与远端模型只通过 ``base_url`` 指向的主机判定，
  不允许在代码其它位置再出现一份"本地 vs 远端"的特殊分支。
- pydantic v2 ``model_validator`` 负责跨字段校验；校验失败由 loader.py 再包
  成 :class:`config_loader.errors.ConfigValidationError` 向外抛。
"""

from __future__ import annotations

from typing import Literal
from urllib.parse import urlparse

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

# ---------------------------------------------------------------------------
# Model
# ---------------------------------------------------------------------------


# 被视为"本地"的主机名集合。纯 IPv4/IPv6 回环地址也包含在内。
_LOCAL_HOSTS: frozenset[str] = frozenset(
    {
        "localhost",
        "127.0.0.1",
        "0.0.0.0",
        "::1",
        "host.docker.internal",
    }
)


def _is_local_base_url(base_url: str) -> bool:
    """判定 ``base_url`` 是否指向本地服务。

    本地判定规则（满足任意一条即为本地）：

    - URL 解析后的 hostname 命中 :data:`_LOCAL_HOSTS`
    - hostname 是典型的本地 IPv4 段：``127.*``

    判定失败（例如 URL 非法解析不出 hostname）时按远端处理，保守要求鉴权。
    """
    try:
        parsed = urlparse(base_url)
    except Exception:
        return False
    host = (parsed.hostname or "").lower()
    if not host:
        return False
    if host in _LOCAL_HOSTS:
        return True
    if host.startswith("127."):
        return True
    return False


class ModelConfig(BaseModel):
    """模型 / provider 配置。

    Attributes:
        provider: provider 名。第一版仅支持 ``openai_compatible``。
        name: 模型名，透传给 provider 的 ``model`` 字段。
        base_url: provider HTTP 服务根地址。本地模型（LM Studio / Ollama /
            vLLM 兼容层）也走这个字段，不区分特殊分支。
        api_key: 鉴权密钥。远端模型必须非空；本地模型允许空字符串。
        timeout: 单次请求超时秒数。
        max_tokens: 单次响应最大 token 数。
        temperature: 采样温度，``[0, 2]``。
    """

    model_config = ConfigDict(extra="forbid")

    provider: Literal["openai_compatible"] = "openai_compatible"
    name: str
    base_url: str
    api_key: str = ""
    timeout: float = Field(default=60.0, gt=0)
    max_tokens: int = Field(default=4096, gt=0)
    temperature: float = Field(default=0.7, ge=0.0, le=2.0)

    @field_validator("name")
    @classmethod
    def _name_not_blank(cls, value: str) -> str:
        if not value or not value.strip():
            raise ValueError("model.name must not be empty")
        return value

    @field_validator("base_url")
    @classmethod
    def _base_url_not_blank(cls, value: str) -> str:
        if not value or not value.strip():
            raise ValueError("model.base_url must not be empty")
        return value.rstrip("/")

    @model_validator(mode="after")
    def _check_remote_requires_key(self) -> ModelConfig:
        """远端模型必须带 api_key；本地模型（回环地址）允许为空。

        语义选择：不在代码里用 ``if is_local`` 做"本地特殊分支"，
        仅在这里作为校验约束体现。真正 provider 调用时是否携带
        Authorization 头由 provider 实现根据 ``api_key`` 是否为空决定。
        """
        if not self.api_key and not _is_local_base_url(self.base_url):
            raise ValueError(
                "model.api_key must be set for non-local base_url "
                f"({self.base_url!r}); only local hosts (127.0.0.1 / localhost / ::1) "
                "are allowed to leave api_key empty."
            )
        return self

    @property
    def is_local(self) -> bool:
        """便于装配层在打印诊断信息时区分本地/远端（不承担业务分支职责）。"""
        return _is_local_base_url(self.base_url)


# ---------------------------------------------------------------------------
# Runner
# ---------------------------------------------------------------------------


class RunnerConfig(BaseModel):
    """Runner 运行参数。"""

    model_config = ConfigDict(extra="forbid")

    max_turns: int = Field(default=10, gt=0)


# ---------------------------------------------------------------------------
# Session
# ---------------------------------------------------------------------------


class SessionConfig(BaseModel):
    """会话存储后端配置。

    Attributes:
        backend: ``memory`` 使用 :class:`core.session.InMemorySession`；
            ``sqlite`` 交给 ``context/session_store.py`` 的工程化实现承接（v1
            后续批次落地）。
        store_path: sqlite 后端的持久化路径。memory 后端会忽略此项，
            但仍保留默认值，便于"切到 sqlite 不改配置"。
    """

    model_config = ConfigDict(extra="forbid")

    backend: Literal["memory", "sqlite"] = "memory"
    store_path: str = ".kongming/sessions.db"


# ---------------------------------------------------------------------------
# Trace / Logging
# ---------------------------------------------------------------------------


class TraceConfig(BaseModel):
    """trace 落盘配置。"""

    model_config = ConfigDict(extra="forbid")

    output_path: str = ".kongming/trace.jsonl"


class LoggingConfig(BaseModel):
    """日志级别配置。"""

    model_config = ConfigDict(extra="forbid")

    level: Literal["DEBUG", "INFO", "WARNING", "ERROR"] = "INFO"


# ---------------------------------------------------------------------------
# Host / Approval
# ---------------------------------------------------------------------------


class HostConfig(BaseModel):
    """宿主类型配置。v1 仅支持 cli。"""

    model_config = ConfigDict(extra="forbid")

    kind: Literal["cli"] = "cli"


class ApprovalConfig(BaseModel):
    """审批模式配置。

    ``interactive`` 走 ``tools/approval.py`` 的交互实现；``auto_allow`` /
    ``auto_deny`` 预留给自动化测试。具体策略由装配层按此配置选择实现，
    核心协议只认 :class:`core.contracts.ApprovalProvider`。
    """

    model_config = ConfigDict(extra="forbid")

    mode: Literal["interactive", "auto_allow", "auto_deny"] = "interactive"


# ---------------------------------------------------------------------------
# Tool
# ---------------------------------------------------------------------------


class ShellToolConfig(BaseModel):
    """shell builtin tool 启用开关。"""

    model_config = ConfigDict(extra="forbid")

    enabled: bool = True


class FileToolConfig(BaseModel):
    """file builtin tool 启用开关。"""

    model_config = ConfigDict(extra="forbid")

    enabled: bool = True


class ToolConfig(BaseModel):
    """builtin tool 集合开关。"""

    model_config = ConfigDict(extra="forbid")

    shell: ShellToolConfig = Field(default_factory=ShellToolConfig)
    file: FileToolConfig = Field(default_factory=FileToolConfig)


# ---------------------------------------------------------------------------
# 总配置
# ---------------------------------------------------------------------------


class Config(BaseModel):
    """整体配置。

    所有子节都给默认值（``model`` 除外——模型信息是最小必填项），
    以便 loader 在缺省 section 时仍可 hydrate 一份合法 Config。
    """

    model_config = ConfigDict(extra="forbid")

    model: ModelConfig
    runner: RunnerConfig = Field(default_factory=RunnerConfig)
    session: SessionConfig = Field(default_factory=SessionConfig)
    trace: TraceConfig = Field(default_factory=TraceConfig)
    logging: LoggingConfig = Field(default_factory=LoggingConfig)
    host: HostConfig = Field(default_factory=HostConfig)
    approval: ApprovalConfig = Field(default_factory=ApprovalConfig)
    tool: ToolConfig = Field(default_factory=ToolConfig)


__all__ = [
    "ApprovalConfig",
    "Config",
    "FileToolConfig",
    "HostConfig",
    "LoggingConfig",
    "ModelConfig",
    "RunnerConfig",
    "SessionConfig",
    "ShellToolConfig",
    "ToolConfig",
    "TraceConfig",
]
