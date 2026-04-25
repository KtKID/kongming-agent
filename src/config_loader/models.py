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


class ReasoningProfile(BaseModel):
    """单个模型的 reasoning 能力描述。

    每条 profile 以模型名（或前缀）为 key 存放在 :attr:`ModelConfig.reasoning_profiles`
    中；resolver 拿最终请求模型名去匹配，命中后用 adapter 生成 payload patch。

    Attributes:
        match: ``exact`` 要求模型名完全相等；``prefix`` 检查模型名是否以 key 开头。
            当 ``exact`` 和 ``prefix`` 同时命中时，``exact`` 优先。
        adapter: 命中后使用的 payload 翻译策略。V1 支持三种：
            - ``none``：明确不发送 reasoning 参数（如 gemma 本地模型）
            - ``glm_thinking_budget``：智谱 GLM 的 ``thinking`` 格式
            - ``anthropic_compatible_reasoning``：Anthropic 兼容 reasoning 格式
        supported_efforts: 该模型支持的 effort 档位。``None`` 表示不限制。
            ``anthropic_compatible_reasoning`` adapter 必须声明此字段。
        effort_map: 统一 effort 值到厂商特定数值的映射。``glm_thinking_budget``
            adapter 必须声明此字段。
    """

    model_config = ConfigDict(extra="forbid")

    match: Literal["exact", "prefix"] = "exact"
    adapter: Literal[
        "none",
        "glm_thinking_budget",
        "anthropic_compatible_reasoning",
    ]
    supported_efforts: list[Literal["low", "medium", "high"]] | None = None
    effort_map: dict[Literal["low", "medium", "high"], int] | None = None

    @model_validator(mode="after")
    def _validate_adapter_requirements(self) -> ReasoningProfile:
        """adapter 必需字段校验。"""
        if self.adapter == "glm_thinking_budget" and not self.effort_map:
            raise ValueError(
                "reasoning profile with adapter='glm_thinking_budget' must include effort_map"
            )
        if self.adapter == "anthropic_compatible_reasoning" and not self.supported_efforts:
            raise ValueError(
                "reasoning profile with adapter='anthropic_compatible_reasoning' "
                "must include supported_efforts"
            )
        return self


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
        reasoning_effort: 控制模型推理深度。``None`` 表示不发此参数（使用模型默认）；
            provider 层负责把统一枚举映射到各厂商格式（OpenAI o1 系用
            ``reasoning_effort``，GLM 系用 ``thinking``，不支持的 provider skip）。
        reasoning_profiles: 模型 reasoning 能力声明。key 为模型名或前缀，
            value 为 :class:`ReasoningProfile`。默认空 dict，不声明时不影响现有行为。
            只走 YAML 配置，不支持 env 覆盖。
    """

    model_config = ConfigDict(extra="forbid")

    provider: Literal["openai_compatible", "anthropic"] = "openai_compatible"
    name: str
    base_url: str
    api_key: str = ""
    timeout: float = Field(default=60.0, gt=0)
    max_tokens: int = Field(default=4096, gt=0)
    temperature: float = Field(default=0.7, ge=0.0, le=2.0)
    reasoning_effort: Literal["low", "medium", "high"] | None = None
    reasoning_profiles: dict[str, ReasoningProfile] = {}

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
# CLI
# ---------------------------------------------------------------------------


class CliConfig(BaseModel):
    """CLI 交互行为配置。"""

    model_config = ConfigDict(extra="forbid")

    # 每轮 llm.response 后是否在终端打印模型的思考内容（reasoning_content）。
    # 仅当模型确实返回了思考内容时才输出；无内容时静默。
    show_reasoning: bool = False


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
# Stream
# ---------------------------------------------------------------------------


class StreamConfig(BaseModel):
    """LLM 流式响应配置。

    顶层挂在 :class:`Config.stream`（**非** :class:`ModelConfig` 子节），因为
    流式跨 observability / runtime / provider 三层职责，不归属单一模块。

    Attributes:
        enabled: 是否启用流式。装配层用此开关 + ``isinstance(llm, SupportsLLMStream)``
            能力探测共同决定是否走流式路径。默认 ``True``：出厂即用打字机效果，
            可通过 ``--no-stream`` flag 或 ``KONGMING_STREAM_ENABLED=0`` 关闭。
        read_timeout: 流式响应的读超时（秒）。流式心跳期间不计入此超时；
            本地 endpoint 由 provider 装配层根据 :attr:`ModelConfig.is_local`
            自动上调（详见 B#4）。
        suppress_content_after_tool_call: 流中出现 tool_call 后，runner 是否
            屏蔽继续到达的 ``content.delta``（避免 CLI 在 tool 调用前夹带乱文本）。
            默认 ``True``，可在 reasoning-style 模型场景下关闭。
        delta_sampling: ``JsonlTraceSink`` 对 ``content.delta`` / ``reasoning.delta``
            的采样策略：``none`` 不写（默认，防爆磁盘）/ ``periodic`` 按
            ``periodic_batch_size`` 抽样 / ``full`` 全写（仅 debug）。
        periodic_batch_size: ``delta_sampling="periodic"`` 时的采样批大小，
            每 N 个 delta 取 1 个落盘。
    """

    model_config = ConfigDict(extra="forbid")

    enabled: bool = True
    read_timeout: float = Field(default=120.0, gt=0)
    suppress_content_after_tool_call: bool = True
    delta_sampling: Literal["none", "periodic", "full"] = "none"
    periodic_batch_size: int = Field(default=20, gt=0)


# ---------------------------------------------------------------------------
# Compactor
# ---------------------------------------------------------------------------


class CompactorConfig(BaseModel):
    """历史压缩策略参数。

    对应 :mod:`context.history_compactor` 的同名 dataclass，这里用 pydantic
    模型做校验，装配层按需转成 dataclass 传给 HistoryCompactor。

    **默认关闭**：当前压缩仅做消息数 FIFO，语义和 LLM summarize 式压缩（参考
    ``other/claude-code-main/src/services/compact/``）差距大。v0.1.3 默认 ``enabled=False``
    不装配 compactor，直接把 history 原样传给 provider；待后续独立 task
    ``compactor-v2-llm-summarize`` 实施 token + summary 式压缩后再打开默认。
    """

    model_config = ConfigDict(extra="forbid")

    enabled: bool = False
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
# Evolution / Memory
# ---------------------------------------------------------------------------


class EvolutionMemoryConfig(BaseModel):
    """Memory 模块配置。

    对应 ``config/setting.yaml`` 的 ``evolution.memory`` 节。

    Attributes:
        enabled: 是否启用 memory 加载和注入。
        root_path: memory 根目录路径（相对于项目根目录）。
        inject_prompt: 是否将 memory snapshot 注入 system prompt。
        read_max_chars: 单文件读取最大字符数。
        view_max_chars: memory tool view 返回的最大字符数。
    """

    model_config = ConfigDict(extra="forbid")

    enabled: bool = True
    root_path: str = ".kongming/memory"
    inject_prompt: bool = True
    read_max_chars: int = 65536
    view_max_chars: int = 8000


class EvolutionConfig(BaseModel):
    """Self Evolution 配置。

    Attributes:
        memory: memory 子模块配置。
    """

    model_config = ConfigDict(extra="forbid")

    memory: EvolutionMemoryConfig = Field(default_factory=EvolutionMemoryConfig)


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
    cli: CliConfig = Field(default_factory=CliConfig)
    evolution: EvolutionConfig = Field(default_factory=EvolutionConfig)
    stream: StreamConfig = Field(default_factory=StreamConfig)


__all__ = [
    "ApprovalConfig",
    "CliConfig",
    "CompactorConfig",
    "Config",
    "EvolutionConfig",
    "EvolutionMemoryConfig",
    "FileToolConfig",
    "HostConfig",
    "LoggingConfig",
    "ModelConfig",
    "ReasoningProfile",
    "RetryConfig",
    "RunnerConfig",
    "SessionConfig",
    "ShellToolConfig",
    "StreamConfig",
    "ToolConfig",
    "TraceConfig",
]
