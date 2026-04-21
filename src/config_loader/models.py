"""统一配置模型。

对应 :file:`config/setting.yaml` 的全部字段。

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
        provider: provider 名。``openai_compatible``（默认）或 ``anthropic``。
        name: 模型名，透传给 provider 的 ``model`` 字段。
        base_url: provider HTTP 服务根地址。本地模型（LM Studio / Ollama /
            vLLM 兼容层）也走这个字段，不区分特殊分支。
        api_key: 鉴权密钥。远端模型必须非空；本地模型允许空字符串。
        timeout: 单次请求超时秒数。
        max_tokens: 单次响应最大 token 数。
        temperature: 采样温度，``[0, 2]``。
    """

    model_config = ConfigDict(extra="forbid")

    provider: Literal["openai_compatible", "anthropic"] = "openai_compatible"
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
            ``sqlite`` 交给 ``context/session_store.py`` 的工程化实现承接；
            ``file`` 使用 append-only JSONL 文件持久化（v0.1.1 新增）。
        store_path: sqlite 后端的持久化数据库文件路径。
            memory 和 file 后端忽略此项。
        file_store_path: file 后端的 session 目录父路径。
            每个 session 会在该目录下创建 ``<session_id>/`` 子目录。
            仅 file 后端使用。
    """

    model_config = ConfigDict(extra="forbid")

    backend: Literal["memory", "sqlite", "file"] = "memory"
    store_path: str = ".kongming/sessions.db"
    file_store_path: str = ".kongming/sessions"


# ---------------------------------------------------------------------------
# Trace / Logging
# ---------------------------------------------------------------------------


class TraceConfig(BaseModel):
    """trace 落盘配置。"""

    model_config = ConfigDict(extra="forbid")

    output_path: str = ".kongming/trace.jsonl"
    auto_flush: bool = True
    # 是否 dump 每次 LLM provider 的完整 request/response 到 .kongming/debug/raw-llm-*.json。
    # 调试用；默认关。开启后磁盘会随对话持续增长，生产不建议常开。
    # 环境变量 KONGMING_TRACE_RAW_LLM=1 可临时覆盖本配置。
    raw_llm: bool = False


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
# Compactor
# ---------------------------------------------------------------------------


class CompactorConfig(BaseModel):
    """历史压缩策略参数。

    对应 :mod:`context.history_compactor` 的同名 dataclass，这里用 pydantic
    模型做校验，装配层按需转成 dataclass 传给 HistoryCompactor。
    """

    model_config = ConfigDict(extra="forbid")

    max_messages: int = Field(default=50, gt=0)
    keep_recent: int = Field(default=20, gt=0)
    keep_system: bool = True
    tool_result_max_chars: int = Field(default=2000, gt=0)


# ---------------------------------------------------------------------------
# LLM Retry
# ---------------------------------------------------------------------------


class RetryConfig(BaseModel):
    """LLM provider 重试与退避参数。"""

    model_config = ConfigDict(extra="forbid")

    max_retries: int = Field(default=3, ge=0)
    retry_backoff: float = Field(default=1.0, gt=0)


# ---------------------------------------------------------------------------
# Tool
# ---------------------------------------------------------------------------


class ShellToolConfig(BaseModel):
    """shell builtin tool 启用开关与运行限制。"""

    model_config = ConfigDict(extra="forbid")

    enabled: bool = True
    timeout_seconds: float = Field(default=30.0, gt=0)
    max_stream_bytes: int = Field(default=8000, gt=0)
    terminate_grace_seconds: float = Field(default=2.0, ge=0)


class FileToolConfig(BaseModel):
    """file builtin tool 启用开关与运行限制。"""

    model_config = ConfigDict(extra="forbid")

    enabled: bool = True
    read_max_bytes: int = Field(default=65536, gt=0)


class ToolConfig(BaseModel):
    """builtin tool 集合开关与运行参数。"""

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
    compactor: CompactorConfig = Field(default_factory=CompactorConfig)
    retry: RetryConfig = Field(default_factory=RetryConfig)


__all__ = [
    "ApprovalConfig",
    "CompactorConfig",
    "Config",
    "FileToolConfig",
    "HostConfig",
    "LoggingConfig",
    "ModelConfig",
    "RetryConfig",
    "RunnerConfig",
    "SessionConfig",
    "ShellToolConfig",
    "ToolConfig",
    "TraceConfig",
]
