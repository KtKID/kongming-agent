"""统一配置模型。

对应 :file:`config/setting.yaml` 的全部字段。

**严格约束**：所有持久化默认值都集中在这里；provider/model 静态定义由
``ModelCatalogManager`` 从 catalog 解析为不可变运行快照。
"""

from __future__ import annotations

from pathlib import Path
from typing import Annotated, Any, Literal
from urllib.parse import urlparse

from pydantic import BaseModel, ConfigDict, Field, field_validator

from sitian.config import SiTianConfig

CURRENT_CONFIG_SCHEMA_VERSION: Literal["v0.6"] = "v0.6"
ConfigSchemaVersion = Literal["v0.6"]
ApiKeyHeader = Literal["x-api-key", "authorization-bearer"]
ReasoningLevel = Literal["low", "medium", "high", "max"]
ReasoningEffortInput = Literal["none", "low", "medium", "high", "max"]

# ---------------------------------------------------------------------------
# Model
# ---------------------------------------------------------------------------


class ModelSelectionConfig(BaseModel):
    """持久化的默认模型选择。

    Provider 协议、endpoint、credential 引用、请求参数和 reasoning 能力全部由
    model catalog 持有。这里仅保存下一次运行采用的 preset 与可选默认档位。
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    preset_id: Annotated[str, Field(min_length=1, max_length=128)]
    reasoning_effort: ReasoningEffortInput | None = None

    @field_validator("preset_id")
    @classmethod
    def _preset_id_not_blank(cls, value: str) -> str:
        """拒绝空白 preset id，并返回去除首尾空白后的稳定值。"""
        normalized = value.strip()
        if not normalized:
            raise ValueError("model.preset_id must not be empty")
        return normalized


# ---------------------------------------------------------------------------
# Runner
# ---------------------------------------------------------------------------


class RunnerConfig(BaseModel):
    """Runner 运行参数。"""

    model_config = ConfigDict(extra="forbid")

    # 兜底默认值：仅在用户配置文件（~/.kongming/setting.yaml）未指定 runner.max_turns
    # 时生效。生产环境必须通过配置文件显式设置，不要依赖此默认值。
    max_turns: int = Field(default=50, gt=0)


# ---------------------------------------------------------------------------
# Session
# ---------------------------------------------------------------------------


class SessionConfig(BaseModel):
    """会话存储后端配置。

    Attributes:
        backend: ``memory`` 使用 :class:`core.session.InMemorySession`；
            ``sqlite`` 交给 ``sessions/session_store.py`` 的工程化实现承接；
            ``file`` 使用 append-only JSONL 文件持久化（v0.1.1 新增）。
        store_path: sqlite 后端的持久化数据库文件路径；`.kongming/*` 派生到
            `kongming_home`。
            memory 和 file 后端忽略此项。
        file_store_path: file 后端的 session 目录父路径；`.kongming/*` 派生到
            `kongming_home`。
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
    """trace 落盘配置；`.kongming/*` 派生到 `kongming_home`。"""

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

    ``interactive`` 走 ``tools/runtime/approval.py`` 的交互实现；``auto_allow`` /
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

    顶层挂在 :class:`Config.stream`，因为
    流式跨 infrastructure.tracing / runtime / provider 三层职责，不归属单一模块。

    Attributes:
        enabled: 是否启用流式。装配层用此开关 + ``isinstance(llm, SupportsLLMStream)``
            能力探测共同决定是否走流式路径。默认 ``True``：出厂即用打字机效果，
            可通过 ``--no-stream`` flag 或 ``KONGMING_STREAM_ENABLED=0`` 关闭。
        read_timeout: 流式响应的读超时（秒）。流式心跳期间不计入此超时；
            本地 endpoint 由 provider 装配层根据运行快照的 ``is_local``
            自动上调。
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

    对应 :mod:`prompting.compaction.history_compactor` 的同名 dataclass，这里用 pydantic
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
# MCP / Web Search
# ---------------------------------------------------------------------------


def _coerce_csv_tuple(value: Any) -> Any:
    """把逗号分隔字符串转为 tuple，输入为原始配置值，输出为 pydantic 兼容值。"""
    if isinstance(value, str):
        return tuple(item.strip() for item in value.split(",") if item.strip())
    return value


class McpToolAliasConfig(BaseModel):
    """MCP tool 到 Kongming Tool alias 的显式映射。"""

    model_config = ConfigDict(frozen=True, extra="forbid")

    tool_name: Annotated[str, Field(min_length=1)]
    alias: Annotated[str, Field(min_length=1)]
    enabled: bool = True

    @field_validator("tool_name", "alias")
    @classmethod
    def _non_blank_name(cls, value: str) -> str:
        stripped = value.strip()
        if not stripped:
            raise ValueError("MCP alias tool_name and alias must not be empty")
        return stripped


class McpServerConfig(BaseModel):
    """单个 stdio MCP server 配置。"""

    model_config = ConfigDict(frozen=True, extra="forbid")

    server_id: Annotated[str, Field(min_length=1, pattern=r"^[A-Za-z0-9_-]+$")]
    enabled: bool = True
    command: Annotated[str, Field(min_length=1)]
    args: tuple[str, ...] = ()
    env: dict[str, str] = Field(default_factory=dict)
    secret_env_keys: tuple[str, ...] = ()
    initialize_timeout_ms: Annotated[int, Field(gt=0)] = 10_000
    call_timeout_ms: Annotated[int, Field(gt=0)] = 60_000
    aliases: tuple[McpToolAliasConfig, ...] = ()

    @field_validator("command")
    @classmethod
    def _command_not_blank(cls, value: str) -> str:
        stripped = value.strip()
        if not stripped:
            raise ValueError("mcp.servers[].command must not be empty")
        return stripped

    @field_validator("args", "secret_env_keys", mode="before")
    @classmethod
    def _coerce_string_tuple(cls, value: Any) -> Any:
        return _coerce_csv_tuple(value)

    @field_validator("aliases", mode="before")
    @classmethod
    def _coerce_alias_tuple(cls, value: Any) -> Any:
        if isinstance(value, str):
            aliases: list[dict[str, object]] = []
            for item in _coerce_csv_tuple(value):
                separator = "=" if "=" in item else ":"
                if separator in item:
                    tool_name, alias = item.split(separator, 1)
                else:
                    tool_name = alias = item
                aliases.append(
                    {
                        "tool_name": tool_name.strip(),
                        "alias": alias.strip(),
                        "enabled": True,
                    }
                )
            return tuple(aliases)
        return value


class McpConfig(BaseModel):
    """MCP client 配置段。"""

    model_config = ConfigDict(frozen=True, extra="forbid")

    servers: tuple[McpServerConfig, ...] = ()


class WebSearchConfig(BaseModel):
    """通用 Web Search provider 选择配置。"""

    model_config = ConfigDict(frozen=True, extra="forbid")

    enabled: bool = True
    provider_name: Annotated[str, Field(min_length=1)] = "minimax_web_search"
    search_tool_name: str | None = None
    search_tool_names: tuple[str, ...] = ("web_search", "mcp__minimax__web_search")
    max_results: Annotated[int, Field(gt=0)] = 5

    @field_validator("search_tool_names", mode="before")
    @classmethod
    def _coerce_search_tool_names(cls, value: Any) -> Any:
        return _coerce_csv_tuple(value)

    @field_validator("provider_name")
    @classmethod
    def _provider_name_not_blank(cls, value: str) -> str:
        stripped = value.strip()
        if not stripped:
            raise ValueError("web_search.provider_name must not be empty")
        return stripped

    @field_validator("search_tool_name")
    @classmethod
    def _blank_search_tool_name_to_none(cls, value: str | None) -> str | None:
        if value is None:
            return None
        stripped = value.strip()
        return stripped or None


# ---------------------------------------------------------------------------
# Evolution / Memory
# ---------------------------------------------------------------------------


class EvolutionMemoryConfig(BaseModel):
    """Memory 模块配置。

    对应 ``config/setting.yaml`` 的 ``evolution.memory`` 节。

    Attributes:
        enabled: 是否启用 memory 加载和注入。
        root_path: memory 根目录路径；`.kongming/*` 派生到 `kongming_home`。
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


class EvolutionLearningConfig(BaseModel):
    """自我进化 learning 配置——控制 reviewer 子 agent 的触发、模型、窗口和产物。"""

    model_config = ConfigDict(extra="forbid")

    # --- 总开关 ---
    enabled: bool = False  # 是否启用自我进化。False 时所有 API no-op
    mode: Literal["child_agent"] = "child_agent"  # 进化模式，当前仅支持 child_agent
    background: bool = True  # reviewer 是否在后台异步执行（不阻塞主对话）
    auto_trigger_enabled: bool = True  # 是否按 cadence 自动触发；False 时仍保留显式 Tool

    # --- reviewer 独立模型选择（留空继承主 agent）---
    preset_id: str | None = None
    reasoning_effort: ReasoningEffortInput | None = None

    # --- 触发节律 ---
    every_n_runs: int = Field(default=5, ge=1)  # 每 N 轮用户消息触发一次 reviewer
    min_user_turns: int = Field(default=3, ge=1)  # 累计不足 N 轮时不触发（冷启动保护）

    # --- reviewer 证据窗口 ---
    max_history_messages: int = Field(default=20, ge=1)  # reviewer 看到的最近消息条数

    # --- 养料提炼 ---
    max_nutrients: int = Field(default=2, ge=1)  # 单次 review 最多提炼几条养料
    nutrient_confidence_threshold: float = Field(
        default=0.75, ge=0.0, le=1.0
    )  # 低于此置信度的养料丢弃

    # --- 超时与生命周期 ---
    review_timeout_seconds: float = Field(default=120.0, gt=0)  # reviewer 单次执行超时（秒）
    drain_on_close_seconds: float = Field(
        default=3.0, gt=0
    )  # app 关闭时等待后台 reviewer 的最大时间

    # --- 存储 ---
    root_path: str = ".kongming/evolution"  # `.kongming/*` 派生到 kongming_home


class EvolutionConfig(BaseModel):
    """Self Evolution 配置。

    Attributes:
        memory: memory 子模块配置。
        learning: review / nutrient 存储子模块配置。
    """

    model_config = ConfigDict(extra="forbid")

    memory: EvolutionMemoryConfig = Field(default_factory=EvolutionMemoryConfig)
    learning: EvolutionLearningConfig = Field(default_factory=EvolutionLearningConfig)


class SafetyApprovalLlmConfig(BaseModel):
    """``safety.approval.llm`` 的独立复核模型配置。"""

    model_config = ConfigDict(frozen=True, extra="forbid")

    provider: Literal["openai_compatible", "anthropic"] | None = None
    model: str
    base_url: str
    api_key: str = ""
    api_key_header: ApiKeyHeader | None = None
    timeout_seconds: float = Field(default=15.0, gt=0)
    prompt_template_path: Path | None = None

    @field_validator("model", "base_url")
    @classmethod
    def _reject_blank_model_fields(cls, value: str) -> str:
        """收敛模型名和端点为非空规范值。"""
        normalized = value.strip().rstrip("/")
        if not normalized:
            raise ValueError("safety.approval.llm model and base_url must not be blank")
        return normalized


class SafetyApprovalConfig(BaseModel):
    """安全审批的全局模型配置。"""

    model_config = ConfigDict(frozen=True, extra="forbid")

    llm: SafetyApprovalLlmConfig | None = None


class SafetyConfig(BaseModel):
    """全局 safety 配置；thread permissions 与处置模式独立持久化。"""

    model_config = ConfigDict(extra="forbid")

    approval: SafetyApprovalConfig = Field(default_factory=SafetyApprovalConfig)


# ---------------------------------------------------------------------------
# Scheduler / cron module (v0.2)
# ---------------------------------------------------------------------------


class SchedulerApprovalConfig(BaseModel):
    """Scheduler-triggered approval policy."""

    model_config = ConfigDict(extra="forbid")

    mode: Literal["fail_closed", "trust"] = "trust"
    """v0.5 新增：cron 全局默认审批模式。任务未声明 approval_mode 时走此值。

    **默认 trust**（v0.5 调整）：cron 任务本质是用户主动 schedule 的预批准
    自动任务，创建时已经过用户判断；执行时再每个工具调用都要求 consent 等于
    重复批准、毫无意义。HardBlock 兜底真高危（rm -rf / 写 ~/.ssh/ / 系统级
    不可逆动作），剩下 explicit_consent 自动放行。

    想要严格审批的部署可显式覆盖 ``mode="fail_closed"``（yaml 或
    ``KONGMING_SCHEDULER_APPROVAL_MODE=fail_closed`` env）。
    """

    allow_write_file_create_in_cwd: bool = True
    """v0.2 老字段：仅在 mode='fail_closed' 下生效。
    trust 模式下 write_file 已整体放开，此字段无意义。
    """


class SchedulerConfig(BaseModel):
    """cron 模块运行配置（v0.2 引入）。

    v0.2 替换 v0.1 的 CLI/Web 主入口为 LLM Tool（``schedule_tool``）；本配置
    控制是否启用、ticker 扫描间隔、并发上限和任务 GC 策略。

    Attributes:
        enabled: 是否启用 cron 模块。``False`` 时 registry 不注册
            ``schedule_tool`` 也不在 cli/web 入口装配 ticker。
        home: cron 数据根目录（``tasks.json`` / ``audit.jsonl`` 落盘位置）。
            ``None`` 时由调用方走 ``get_kongming_home() / "cron"``；
            `.kongming/*` 派生到 `kongming_home`。
        interval: ticker 扫描间隔（秒），下限 ``0.1s`` 防误配把 CPU 烧穿。
        max_inflight: 同时并发跑的 cron 任务上限，由 ``ScheduledRunManager``
            的 ``asyncio.Semaphore`` 限流。下限 ``1``。
        max_task_age_seconds: 任务多久没跑就被 GC 掉；``None`` 表示永不 GC。
        default_timezone: v0.3 新增。LLM 通过 ``schedule_tool`` 创建任务时，
            若没有显式指定 timezone，用此默认值（IANA timezone name，如
            ``"Asia/Shanghai"``）。**应当反映用户当前的 wall-clock 时区**，
            避免 cron 表达式被错误地按 UTC 解释。默认 ``"UTC"`` 是保守选择，
            用户应在 ``config/setting.yaml`` 中按本地时区覆盖。
        default_delivery_channel: v0.3 新增。LLM 通过 ``schedule_tool`` 创建
            任务时默认填的 delivery channel；``"web"`` 适合主要在浏览器使用的
            场景，``"cli"`` 适合主要在终端使用的场景。
    """

    model_config = ConfigDict(extra="forbid")

    enabled: bool = True
    home: Path | None = None
    interval: float = Field(default=1.0, ge=0.1)
    max_inflight: int = Field(default=8, ge=1)
    max_task_age_seconds: int | None = None
    default_timezone: str = "UTC"
    default_delivery_channel: Literal["web", "cli"] = "web"
    default_max_turns: int = Field(default=90, gt=0)
    """v0.5.1 新增：cron task.policy.max_turns 缺省时的兜底 max_turns。

    替换 v0.2 ~ v0.5 期间在 ``execution_bridge._DEFAULT_MAX_TURNS=20`` 的硬编码
    （违反"默认值集中在 infrastructure.config.models"约束）。默认 ``90`` 是经验值——
    司天扫描类重型 cron 任务通常需要 40~80 turn 才能稳定完成；20 太低反复
    触发 max_turns 强制终止。

    任务级 ``task.policy.max_turns`` 仍优先（None 时才走此值）。可通过
    ``KONGMING_SCHEDULER_DEFAULT_MAX_TURNS`` env 覆盖。
    """
    approval: SchedulerApprovalConfig = Field(default_factory=SchedulerApprovalConfig)


# ---------------------------------------------------------------------------
# Workflow Dashboard
# ---------------------------------------------------------------------------


class WorkflowConfig(BaseModel):
    """工作流看板配置；`.kongming/*` home 派生到 `kongming_home`。"""

    model_config = ConfigDict(extra="forbid")

    enabled: bool = True
    home: Path | None = None
    scan_interval: float = Field(default=30.0, ge=5.0)


# ---------------------------------------------------------------------------
# Web 宿主壳（v0.1.5+）
# ---------------------------------------------------------------------------


class WebFullLogConfig(BaseModel):
    """前后端通信全量日志配置（full-log-v0.1）。

    为开发期可观测性提供"组装后的最终格式"全链路日志：WS 三条通道 C2S/S2C
    帧 + REST 元数据写到 ``.kongming/logs/full_log.jsonl``（跟 ``trace.jsonl``
    同 ``.kongming/`` home 体系），用于离线分析、bug 回溯、链路审计。

    **默认关闭**——只有显式 ``enabled=true`` 或 env ``KONGMING_WEB_FULL_LOG_ENABLED=1``
    才装配 :class:`devtools.full_logger.FullLogger`；关闭时所有 ``log()`` 调用
    走 no-op 早返，对 event loop 零影响。

    Attributes:
        enabled: 是否启用全量日志。默认 ``False``，开发期通过 env 临时开启。
        path: 日志文件路径。默认走 ``.kongming/logs/`` 与
            ``trace.jsonl`` 同 `kongming_home`；父目录不存在时由 FullLogger 装配阶段
            ``mkdir -p``；无写权限时降级为 ``enabled=False`` + warning。
        rotate_daily: 是否按 UTC+8 自然日切分日志文件（追加日期后缀）。默认
            ``True``——长期开启时避免单文件无限增长。
        include_http_body: 是否在 REST middleware 记录请求/响应 body。
            **预留字段，阶段 1 不实现，硬编码默认 ``False``**。阶段 2 引入
            middleware 后由其消费此字段；即便用户显式开启，middleware 实现
            层仍可能整体拒绝（敏感字段泄漏防御）。
        queue_size: 异步 queue 容量。满则丢最旧 + 一次性 warning（防刷屏）。
            下限 ``100`` 防误配把队列调成无意义的小值；上限 ``1_000_000`` 防
            手抖配成 ``10**10`` 这种值导致 ``asyncio.Queue`` 占用无限内存
            （把"丢最旧"防爆设计变成 OOM）。
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    enabled: bool = False
    path: str = ".kongming/logs/full_log.jsonl"
    rotate_daily: bool = True
    include_http_body: bool = False
    queue_size: Annotated[int, Field(ge=100, le=1_000_000)] = 10000


class WebDeepResearchSourceProviderConfig(BaseModel):
    """Web Deep Research 来源 provider 配置。

    Web 宿主在 thread runtime 装配时读取本配置，从已注册工具中挑选用户提供的
    search/fetch 能力，并适配为 ``ResearchSourceProvider`` 注入 workflow
    manager。默认开启自动探测；未找到搜索工具时装配层返回空 provider，策略继续
    使用 payload fixture 或 deterministic fallback。

    Attributes:
        enabled: 是否启用 Web 搜索 provider 自动装配。
        provider_name: 写入 deep_research artifact 的 provider 名。
        search_tool_name: 显式指定搜索工具名；为空时按 search_tool_names 自动探测。
        fetch_tool_name: 显式指定读取工具名；为空时按 fetch_tool_names 自动探测。
        search_tool_names: 自动探测搜索工具名列表，按顺序匹配。
        fetch_tool_names: 自动探测读取工具名列表，按顺序匹配。
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    enabled: bool = True
    provider_name: Annotated[str, Field(min_length=1)] = "web_user_tool_research_source"
    search_tool_name: str | None = None
    fetch_tool_name: str | None = None
    search_tool_names: tuple[str, ...] = (
        "deep_research_search",
        "web_search",
        "search_web",
        "browser_search",
    )
    fetch_tool_names: tuple[str, ...] = (
        "deep_research_fetch",
        "web_fetch",
        "fetch_url",
        "browser_fetch",
    )

    @field_validator("search_tool_names", "fetch_tool_names", mode="before")
    @classmethod
    def _coerce_tool_name_tuple_from_env(cls, value: Any) -> Any:
        """把 env 逗号分隔字符串转为工具名元组，输入为原始配置值，输出为 tuple 兼容值。"""
        if isinstance(value, str):
            return tuple(item.strip() for item in value.split(",") if item.strip())
        return value


WebHostEnvironment = Literal["browser", "xspace"]


class WebConfig(BaseModel):
    """v0.1.5 web 宿主壳配置段。

    为 v0.1.5 task 链路（thread-manager / web-host-adapter / web-app-shell /
    frontend）共同消费的配置；当 ``enabled=False``（默认）时整个 web 路径不
    装配，现有 cli 链路零影响。

    Attributes:
        enabled: 是否启用 web 宿主。默认 ``False``——v0.1.5 的 cli 路径仍是
            主流，只有 ``make web`` / 显式启用时才装配。
        host: uvicorn bind 的 IP；``"0.0.0.0"`` 接受外网访问，仅本机用
            ``"127.0.0.1"`` 更安全。
        port: HTTP / WS 端口。
        server_origin: 扫码登录和移动端 handoff 使用的服务器 origin。公网
            场景填 ``https://domain``，局域网扫码填 ``http://192.168.x.x:port``。
        host_environment: Web sidecar 宿主环境。``browser`` 表示普通浏览器，
            ``xspace`` 表示由 XSpace 桌面宿主启动。
        dev_mode: 跳过登录鉴权（仅本地开发）；上线必须 False。
        initial_password: 首次部署启动时使用的明文初始密码。仅在
            ``password.hash`` 缺失时生效；落盘后长期以文件为准。
        cors_origins: 允许的浏览器 Origin；空列表 = 拒绝所有跨域。
        idle_timeout_seconds: cell 空闲多久后被 ``_idle_eviction_loop`` 自动
            evict（默认 30 分钟）。下限 60s 防误配。
        idle_check_interval_seconds: 后台扫盘周期（默认 60s）。下限 10s。
        dashboard_poll_interval_seconds: dashboard 状态页轮询周期（秒）。
            默认 5s；运行时最小按 3s 归一化。
        ws_heartbeat_interval_ms: 前端发 ping 的间隔（毫秒）。默认 30s。
            下限 5s 防过频。
        ws_heartbeat_background_interval_ms: 浏览器 tab 切到后台时
            前端发 ping 的间隔（毫秒）。默认 60s。下限 10s。
            原因：Chrome 后台 tab 把 setInterval 节流到 ≥1min/次，
            前台 30s 心跳在后台变成噪音 + 测出来 latency 是节流延迟不是真 RTT。
            后台用更稀疏的间隔（60s）减少误导。
        ws_heartbeat_timeout_ms: 单次 pong 等待超时（毫秒）。默认 10s。
            下限 3s。
        ws_heartbeat_max_missed: 连续丢失几次 pong 判定连接死亡。默认 3 次。
        full_log: 前后端通信全量日志子配置（full-log-v0.1）。默认 ``enabled=False``，
            开发期通过 env ``KONGMING_WEB_FULL_LOG_ENABLED=1`` 启用；详见
            :class:`WebFullLogConfig`。
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    enabled: bool = False
    host: str = "0.0.0.0"
    port: Annotated[int, Field(ge=1, le=65535)] = 8080
    server_origin: str | None = None
    host_environment: WebHostEnvironment = "browser"
    dev_mode: bool = False
    initial_password: str | None = None
    cors_origins: list[str] = Field(default_factory=list)
    idle_timeout_seconds: Annotated[int, Field(ge=60)] = 1800
    idle_check_interval_seconds: Annotated[int, Field(ge=10)] = 60
    dashboard_poll_interval_seconds: Annotated[int, Field(ge=1)] = 5

    # 心跳配置（前端读取，不硬编码）
    ws_heartbeat_interval_ms: Annotated[int, Field(ge=5000)] = 30000
    """前端发 ping 的间隔（毫秒）。默认 30s。下限 5s 防过频。"""
    ws_heartbeat_background_interval_ms: Annotated[int, Field(ge=10000)] = 60000
    """浏览器 tab 切到后台时 ping 间隔（毫秒）。默认 60s，下限 10s。

    Chrome 后台 tab 节流 setInterval 到 ≥1min，前台 30s 在后台被压缩
    成噪音，且 latency 测出来是节流延迟而非真 RTT。后台用更稀疏间隔
    减少误导（visibilitychange 时切换）。
    """
    ws_heartbeat_timeout_ms: Annotated[int, Field(ge=3000)] = 10000
    """单次 pong 等待超时（毫秒）。默认 10s。"""
    ws_heartbeat_max_missed: Annotated[int, Field(ge=1)] = 3
    """连续丢失几次 pong 判定连接死亡。默认 3 次。"""

    full_log: WebFullLogConfig = Field(default_factory=WebFullLogConfig)
    deep_research_source_provider: WebDeepResearchSourceProviderConfig = Field(
        default_factory=WebDeepResearchSourceProviderConfig
    )

    @field_validator("server_origin")
    @classmethod
    def _normalize_origin(cls, value: str | None) -> str | None:
        """外部访问 origin 只接受 http(s) origin，返回去尾斜杠后的标准值。"""
        if value is None:
            return None
        origin = value.strip().rstrip("/")
        if not origin:
            return None
        parsed = urlparse(origin)
        if parsed.scheme not in {"http", "https"} or not parsed.netloc:
            raise ValueError("web origin must be an http(s) origin")
        if parsed.path not in {"", "/"} or parsed.params or parsed.query or parsed.fragment:
            raise ValueError("web origin must not include path, query, or fragment")
        return f"{parsed.scheme}://{parsed.netloc}"

    @property
    def normalized_dashboard_poll_interval_seconds(self) -> int:
        """dashboard 轮询周期的运行时真值。"""
        return max(self.dashboard_poll_interval_seconds, 3)


# ---------------------------------------------------------------------------
# 总配置
# ---------------------------------------------------------------------------


class Config(BaseModel):
    """整体配置。

    所有子节都给默认值（``model`` 除外——默认 preset 是最小必填项），
    以便 loader 在缺省 section 时仍可 hydrate 一份合法 Config。
    """

    model_config = ConfigDict(extra="forbid")

    config_schema_version: ConfigSchemaVersion = CURRENT_CONFIG_SCHEMA_VERSION
    model: ModelSelectionConfig
    runner: RunnerConfig = Field(default_factory=RunnerConfig)
    session: SessionConfig = Field(default_factory=SessionConfig)
    trace: TraceConfig = Field(default_factory=TraceConfig)
    logging: LoggingConfig = Field(default_factory=LoggingConfig)
    host: HostConfig = Field(default_factory=HostConfig)
    approval: ApprovalConfig = Field(default_factory=ApprovalConfig)
    tool: ToolConfig = Field(default_factory=ToolConfig)
    mcp: McpConfig = Field(default_factory=McpConfig)
    web_search: WebSearchConfig = Field(default_factory=WebSearchConfig)
    compactor: CompactorConfig = Field(default_factory=CompactorConfig)
    retry: RetryConfig = Field(default_factory=RetryConfig)
    cli: CliConfig = Field(default_factory=CliConfig)
    evolution: EvolutionConfig = Field(default_factory=EvolutionConfig)
    stream: StreamConfig = Field(default_factory=StreamConfig)
    safety: SafetyConfig = Field(default_factory=SafetyConfig)
    scheduler: SchedulerConfig = Field(default_factory=SchedulerConfig)
    web: WebConfig = Field(default_factory=WebConfig)
    workflow: WorkflowConfig = Field(default_factory=WorkflowConfig)
    sitian: SiTianConfig = Field(default_factory=SiTianConfig)


__all__ = [
    "CURRENT_CONFIG_SCHEMA_VERSION",
    "ApiKeyHeader",
    "ApprovalConfig",
    "CliConfig",
    "CompactorConfig",
    "Config",
    "ConfigSchemaVersion",
    "EvolutionConfig",
    "EvolutionMemoryConfig",
    "FileToolConfig",
    "HostConfig",
    "LoggingConfig",
    "McpConfig",
    "McpServerConfig",
    "McpToolAliasConfig",
    "ModelSelectionConfig",
    "ReasoningEffortInput",
    "ReasoningLevel",
    "RetryConfig",
    "RunnerConfig",
    "SafetyApprovalConfig",
    "SafetyApprovalLlmConfig",
    "SafetyConfig",
    "SchedulerApprovalConfig",
    "SchedulerConfig",
    "SessionConfig",
    "ShellToolConfig",
    "SiTianConfig",
    "StreamConfig",
    "ToolConfig",
    "TraceConfig",
    "WebConfig",
    "WebDeepResearchSourceProviderConfig",
    "WebFullLogConfig",
    "WebSearchConfig",
    "WorkflowConfig",
]
