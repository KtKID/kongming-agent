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

import re
from pathlib import Path
from typing import Annotated, Any, Literal
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

# URL 末尾段是 ``vN``（v1/v2/.../v999 等）→ openai_compatible 强信号。
# 项目内既有约定：anthropic base_url **不带版本段**（provider 自己拼 /v1/messages），
# openai 协议 base_url **带版本段**。所以"末尾 vN"是 OpenAI 协议的强 signal。
_VERSION_SEGMENT_RE: re.Pattern[str] = re.compile(r"^v\d+$")


# Provider 路由表默认值。按协议分组：每个协议名映射到该协议家族的 host list。
# 用户可在 yaml 里写 ``model.provider_routing`` 扩展此表（合并语义，不替换）。
#
# host 完全匹配（不递归子域名、不正则）。``localhost`` / ``127.0.0.1`` 覆盖
# 所有本地服务（LM Studio / Ollama / 自建）。MiniMax 默认是 OpenAI 端点；其
# Anthropic 兼容端点（``/anthropic`` 路径）会被 path 启发式优先升级，不依赖
# 这张表。
_DEFAULT_PROVIDER_ROUTING: dict[Literal["openai_compatible", "anthropic"], list[str]] = {
    "openai_compatible": [
        "api.openai.com",
        "open.bigmodel.cn",
        "api.deepseek.com",
        "api.moonshot.cn",
        "api.minimaxi.com",
        "localhost",
        "127.0.0.1",
    ],
    "anthropic": [
        "api.anthropic.com",
    ],
}


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
        provider: provider 名。``openai_compatible`` / ``anthropic`` / ``None``。
            **可选**：通常省略，让 :attr:`effective_provider` 通过 URL 启发式 +
            host 路由表自动判定。仅当未知厂商 + URL 无强信号时才需显式写。
        name: 模型名，透传给 provider 的 ``model`` 字段。
        base_url: provider HTTP 服务根地址。本地模型（LM Studio / Ollama /
            vLLM 兼容层）也走这个字段，不区分特殊分支。
        provider_routing: host → provider 路由表（按协议分组）。pydantic 加载
            后与 :data:`_DEFAULT_PROVIDER_ROUTING` **合并去重**（用户写则是
            扩展，不是替换）。在 :attr:`effective_provider` 里用作 path 启发式
            无信号时的二级判定。
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

    provider: Literal["openai_compatible", "anthropic"] | None = None
    name: str
    base_url: str
    provider_routing: dict[Literal["openai_compatible", "anthropic"], list[str]] = Field(
        default_factory=lambda: {k: list(v) for k, v in _DEFAULT_PROVIDER_ROUTING.items()}
    )
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
    def _merge_provider_routing(self) -> ModelConfig:
        """把用户 yaml 里的 ``provider_routing`` 与默认表合并（不替换）。

        Why:
            yaml 里写 ``provider_routing`` 的目的是"我新加一两个 host"，
            而不是"我要重写整张默认表"。合并语义让用户心智负担最低。

        实现：每个协议组的 list = 默认 list + 用户 list，``dict.fromkeys``
        顺序去重。
        """
        merged: dict[Literal["openai_compatible", "anthropic"], list[str]] = {
            protocol: list(hosts) for protocol, hosts in _DEFAULT_PROVIDER_ROUTING.items()
        }
        for protocol, hosts in self.provider_routing.items():
            existing = merged.setdefault(protocol, [])
            seen = set(existing)
            for host in hosts:
                if host not in seen:
                    existing.append(host)
                    seen.add(host)
        # pydantic v2 默认非 frozen，可直接赋值
        self.provider_routing = merged
        return self

    @model_validator(mode="after")
    def _check_remote_requires_key(self) -> ModelConfig:
        """远端模型必须带 api_key；本地模型（回环地址）允许为空。

        语义选择：不在代码里用 ``if is_local`` 做"本地特殊分支"，
        仅在这里作为校验约束体现。真正 provider 调用时是否携带
        Authorization 头由 provider 实现根据 ``api_key`` 是否为空决定。

        注意：本 validator **只看 base_url 是否本地**（``_is_local_base_url``），
        与 :attr:`effective_provider` / :attr:`provider` 无关。这是刻意的：
        "需不需要 key" 是网络可达性问题，与协议格式（OpenAI vs Anthropic）
        正交。effective_provider 切换不会让原本需要 key 的远端突然豁免，
        反之亦然。
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

    def _path_based_provider(self) -> Literal["openai_compatible", "anthropic"] | None:
        """URL path 强信号判定：升级到 anthropic / 降级到 openai_compatible / 无结论。

        判定（优先级从高到低）：

        1. ``host == api.anthropic.com``（Anthropic 官方）→ anthropic
        2. URL 路径中**任一独立段为 "anthropic"** → anthropic
           （第三方兼容端点约定，如 ``https://api.minimaxi.com/anthropic``）
        3. URL 路径**末尾段命中 ``^v\\d+$``**（v1, v2, ..., v999）→ openai_compatible
           （项目内约定：anthropic base_url 不带版本段、openai base_url 带版本段，
           所以"末尾 vN"是 OpenAI 协议的强信号）

        不命中以上任一规则时返回 None，由上层走 host 路由表 → declare → 默认。

        故意**不**用子串匹配：避免 ``/my-anthropic-proxy/v1`` 这种路径误命中；
        只接受独立路径段。
        """
        parsed = urlparse(self.base_url)
        host = (parsed.hostname or "").lower()
        if host == "api.anthropic.com":
            return "anthropic"
        segments = [s.lower() for s in parsed.path.rstrip("/").split("/") if s]
        if "anthropic" in segments:
            return "anthropic"
        if segments and _VERSION_SEGMENT_RE.match(segments[-1]):
            return "openai_compatible"
        return None

    @property
    def _host_to_protocol(self) -> dict[str, Literal["openai_compatible", "anthropic"]]:
        """把分组形态的 ``provider_routing`` flatten 成 ``{host: protocol}`` 便于 O(1) 查表。

        每次访问都重算（小 dict，开销可忽略），避免 cached state 与 pydantic
        re-validation 同步问题。
        """
        result: dict[str, Literal["openai_compatible", "anthropic"]] = {}
        for protocol, hosts in self.provider_routing.items():
            for host in hosts:
                result[host.lower()] = protocol
        return result

    @property
    def effective_provider(self) -> Literal["openai_compatible", "anthropic"]:
        """按 4 级优先级判定真实 provider，覆盖 declare 错填场景。

        优先级（高 → 低）：

        1. **URL path 强信号**（:meth:`_path_based_provider`）
           - host = api.anthropic.com → anthropic
           - path 含独立 ``anthropic`` 段 → anthropic（升级）
           - path 末尾段是 ``v\\d+`` → openai_compatible（降级）
        2. **Host 路由表**（:attr:`provider_routing` 内置 8 项 + 用户扩展）
        3. **用户 declare**（:attr:`provider`，可选）
        4. **默认** ``openai_compatible``

        Why URL > declare：
            URL 是协议本身的特征（host、path 段），客观；declare 是用户手填，
            错的概率高。当两者冲突时优先信 URL，能救起"用户切端点忘改 yaml"
            的常见手抖（v1 task 验证过 anthropic 升级；v2 task 加 openai 降级
            修复 GLM declare anthropic + URL `/v4` → 404 故障）。

        Why 同 host 双协议（如 MiniMax）：
            path 启发式比 host 表优先。``api.minimaxi.com/anthropic`` 走 path
            规则升级到 anthropic；``api.minimaxi.com/v1`` 走 path 规则降级到
            openai_compatible。host 表里的"minimax 默认 openai" 只在 ``api.minimaxi.com``
            裸 host（无路径）时起作用。
        """
        # 1. path 强信号
        path_result = self._path_based_provider()
        if path_result is not None:
            return path_result
        # 2. host 路由表
        host = (urlparse(self.base_url).hostname or "").lower()
        host_result = self._host_to_protocol.get(host)
        if host_result is not None:
            return host_result
        # 3. user declared
        if self.provider is not None:
            return self.provider
        # 4. 默认
        return "openai_compatible"


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


class EvolutionLearningConfig(BaseModel):
    """Self evolution learning 配置。"""

    model_config = ConfigDict(extra="forbid")

    enabled: bool = False
    mode: Literal["child_agent"] = "child_agent"
    background: bool = True
    model_name: str | None = None
    reasoning_effort: str | None = None
    every_n_runs: int = Field(default=5, ge=1)
    min_user_turns: int = Field(default=3, ge=1)
    max_history_messages: int = Field(default=20, ge=1)
    max_nutrients: int = Field(default=2, ge=1)
    nutrient_confidence_threshold: float = Field(default=0.75, ge=0.0, le=1.0)
    review_timeout_seconds: float = Field(default=10.0, gt=0)
    drain_on_close_seconds: float = Field(default=3.0, gt=0)
    root_path: str = ".kongming/evolution"


class EvolutionConfig(BaseModel):
    """Self Evolution 配置。

    Attributes:
        memory: memory 子模块配置。
        learning: review / nutrient 存储子模块配置。
    """

    model_config = ConfigDict(extra="forbid")

    memory: EvolutionMemoryConfig = Field(default_factory=EvolutionMemoryConfig)
    learning: EvolutionLearningConfig = Field(default_factory=EvolutionLearningConfig)


# ---------------------------------------------------------------------------
# Safety v0.1.4 — 三张规则表 + 信任配置
# ---------------------------------------------------------------------------

# v0.1.4 boundary_scope 运行时仅接受 host / any。sandbox 字段保留为 schema 预付，
# yaml 配置加载阶段命中 sandbox 直接抛异常，不静默忽略。
_ALLOWED_BOUNDARY_SCOPE_RUNTIME: frozenset[str] = frozenset({"host", "any"})


class SafetyHardDenyConfig(BaseModel):
    """``safety.hard_deny_commands`` 单条规则的 yaml schema。"""

    model_config = ConfigDict(extra="forbid")

    name: str
    matcher: str
    match_mode: Literal["segment_regex", "profile"]
    boundary_scope: Literal["any", "host", "sandbox"]
    reason: str

    @model_validator(mode="after")
    def _reject_sandbox_scope(self) -> SafetyHardDenyConfig:
        if self.boundary_scope not in _ALLOWED_BOUNDARY_SCOPE_RUNTIME:
            raise ValueError(
                f"safety.hard_deny_commands[{self.name!r}].boundary_scope="
                f"{self.boundary_scope!r} is not supported in v0.1.4 (host-only); "
                "remove the rule or set boundary_scope to 'host' or 'any'."
            )
        return self


class SafetyApprovalRequiredConfig(BaseModel):
    """``safety.approval_required_commands`` 单条规则的 yaml schema。"""

    model_config = ConfigDict(extra="forbid")

    name: str
    matcher: str
    match_mode: Literal["segment_regex", "profile"]
    severity: Literal["standard", "elevated"]
    boundary_scope: Literal["any", "host", "sandbox"]
    reason: str

    @model_validator(mode="after")
    def _reject_sandbox_scope(self) -> SafetyApprovalRequiredConfig:
        if self.boundary_scope not in _ALLOWED_BOUNDARY_SCOPE_RUNTIME:
            raise ValueError(
                f"safety.approval_required_commands[{self.name!r}].boundary_scope="
                f"{self.boundary_scope!r} is not supported in v0.1.4 (host-only); "
                "remove the rule or set boundary_scope to 'host' or 'any'."
            )
        return self


class SafetySensitivePathConfig(BaseModel):
    """``safety.sensitive_paths`` 单条规则的 yaml schema。"""

    model_config = ConfigDict(extra="forbid")

    name: str
    matcher: str
    match_mode: Literal["path_prefix", "project_relative", "glob"]
    ops: list[Literal["read", "write", "exec"]]
    effect: Literal["block", "elevated", "allow"]
    boundary_scope: Literal["any", "host", "sandbox"]
    reason: str

    @field_validator("ops")
    @classmethod
    def _ops_not_empty(
        cls, value: list[Literal["read", "write", "exec"]]
    ) -> list[Literal["read", "write", "exec"]]:
        if not value:
            raise ValueError(
                "safety.sensitive_paths[*].ops must contain at least one of read/write/exec"
            )
        return value

    @model_validator(mode="after")
    def _reject_sandbox_scope(self) -> SafetySensitivePathConfig:
        if self.boundary_scope not in _ALLOWED_BOUNDARY_SCOPE_RUNTIME:
            raise ValueError(
                f"safety.sensitive_paths[{self.name!r}].boundary_scope="
                f"{self.boundary_scope!r} is not supported in v0.1.4 (host-only); "
                "remove the rule or set boundary_scope to 'host' or 'any'."
            )
        return self


class SafetySkillCallConfig(BaseModel):
    """yaml ``safety.skill_call_rules`` 单条配置（v0.1.6 skill-system 模块 C）。

    与 :class:`safety.types.SkillCallRule` 一一对应；BoundaryScope 字段以小写
    字符串配置，运行时由 ``SkillCallRule`` 转换为 ``BoundaryScope`` enum。
    """

    model_config = ConfigDict(extra="forbid")

    name: str
    matcher: str
    match_mode: Literal["exact", "prefix"]
    effect: Literal["block", "elevated", "allow"]
    boundary_scope: Literal["any", "host", "sandbox"] = "any"
    reason: str = ""

    @field_validator("name", "matcher")
    @classmethod
    def _reject_empty(cls, value: str) -> str:
        """``name`` / ``matcher`` 必须非空，否则 prefix 匹配会退化为 match-all 黑洞。"""
        if not value or not value.strip():
            raise ValueError("name and matcher must be non-empty strings")
        return value

    @model_validator(mode="after")
    def _reject_sandbox_scope(self) -> SafetySkillCallConfig:
        """与同包其他 SafetyXxxConfig 一致：v0.1.4 起仅接受 host / any。"""
        if self.boundary_scope not in _ALLOWED_BOUNDARY_SCOPE_RUNTIME:
            raise ValueError(
                f"safety.skill_call_rules[{self.name!r}].boundary_scope="
                f"{self.boundary_scope!r} is not supported (host-only); "
                "remove the rule or set boundary_scope to 'host' or 'any'."
            )
        return self


class SafetyConfig(BaseModel):
    """``safety`` section yaml schema（v0.1.4 引入）。

    与 ``docs/safety-scope-v0.1.4/04-data-and-state.md`` § 配置 schema 完全一致。
    用户配置 **追加** 默认规则集（参考 ``safety/default_rules.py``），不替换。

    Attributes:
        hard_deny_commands: 用户追加的 hard-block 命令规则。
        approval_required_commands: 用户追加的需审批命令规则。
        sensitive_paths: 用户追加的路径风险规则。
        skill_call_rules: 用户追加的 skill 调用规则（v0.1.6）。
        trusted_workdirs: 默认作用域的低风险工作目录（项目相对路径）。
        allow_writes: 配置静默放行的写路径（path prefix）。
        allow_tools_silent: 配置静默放行的工具名集合。
    """

    model_config = ConfigDict(extra="forbid")

    hard_deny_commands: list[SafetyHardDenyConfig] = Field(default_factory=list)
    approval_required_commands: list[SafetyApprovalRequiredConfig] = Field(default_factory=list)
    sensitive_paths: list[SafetySensitivePathConfig] = Field(default_factory=list)
    skill_call_rules: list[SafetySkillCallConfig] = Field(default_factory=list)
    trusted_workdirs: list[str] = Field(default_factory=list)
    allow_writes: list[str] = Field(default_factory=list)
    allow_tools_silent: list[str] = Field(default_factory=list)
    log_silent_reads: bool = False
    """是否将 silent_allow + read 类工具的事件写入 trace。

    默认关闭以避免 jsonl 膨胀；可通过 ``KONGMING_SAFETY_LOG_SILENT_READS=true`` 开启。
    write 类的 silent_allow 总是写盘，不受此开关影响。
    """

    @field_validator(
        "trusted_workdirs",
        "allow_writes",
        "allow_tools_silent",
        mode="before",
    )
    @classmethod
    def _coerce_list_from_env(cls, value: Any) -> Any:
        """允许 env 用逗号分隔字符串覆盖 list[str] 字段。

        示例：``KONGMING_SAFETY_ALLOW_WRITES="~/scratch/,/tmp/kongming-*/"``
        当 yaml 已经写成 list 时此 validator 不动。
        """
        if isinstance(value, str):
            return [item.strip() for item in value.split(",") if item.strip()]
        return value


# ---------------------------------------------------------------------------
# Scheduler / cron module (v0.2)
# ---------------------------------------------------------------------------


class SchedulerConfig(BaseModel):
    """cron 模块运行配置（v0.2 引入）。

    v0.2 替换 v0.1 的 CLI/Web 主入口为 LLM Tool（``schedule_tool``）；本配置
    控制是否启用、ticker 扫描间隔、并发上限和任务 GC 策略。

    Attributes:
        enabled: 是否启用 cron 模块。``False`` 时 registry 不注册
            ``schedule_tool`` 也不在 cli/web 入口装配 ticker。
        home: cron 数据根目录（``tasks.json`` / ``audit.jsonl`` 落盘位置）。
            ``None`` 时由调用方走 ``get_kongming_home() / "cron"``。
        interval: ticker 扫描间隔（秒），下限 ``0.1s`` 防误配把 CPU 烧穿。
        max_inflight: 同时并发跑的 cron 任务上限，由 ``asyncio.Semaphore`` 在
            ticker 内限流。下限 ``1``。
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


# ---------------------------------------------------------------------------
# Web 宿主壳（v0.1.5+）
# ---------------------------------------------------------------------------


class LLMPresetConfig(BaseModel):
    """单条 LLM preset 配置；浏览器在创建 thread 时下拉选择其一。

    与 :class:`ModelConfig` 的差异：

    - ``ModelConfig`` 是 cli 单一模型的运行参数（含 timeout / max_tokens / 温度 /
      reasoning_profiles），用于现有 cli 路径。
    - ``LLMPresetConfig`` 是"web 端可选模型清单"——只描述足以构造一个
      :class:`ModelConfig` 实例的 5 个核心字段。具体如何把 preset 翻译成
      ``ModelConfig`` 由 :mod:`web.thread_manager` 在装配 cell 时决定（v0.1.5
      最简单做法：保留 ``Config.model`` 的其他字段不变，仅替换 name / base_url
      / api_key）。

    Attributes:
        id: preset 唯一标识；浏览器创建 thread 时回传。``thread-<hex12>``
            约束的是 thread_id，不是 preset_id；preset_id 允许字母 / 数字 / -_。
        display_name: UI 下拉显示名（如 ``"Claude Sonnet 4 / Anthropic"``）。
        provider: 与 ``ModelConfig.provider`` 一致；``None`` 表示由 base_url
            自动判定（推荐省略）。
        base_url: provider HTTP 服务根地址。
        model: 模型名（透传给 provider 的 ``model`` 字段）。
        api_key_env: 鉴权密钥所在的环境变量名（如 ``OPENAI_API_KEY``）。
            ``None`` 表示本地 endpoint 不需要 key —— ``ModelConfig`` 同样的
            ``_is_local_base_url`` 校验逻辑负责兜底。
            **不**直接存 api_key 字符串：避免明文 key 落进 yaml 文件。
        reasoning_effort: 该 preset 的默认 reasoning effort。
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    id: Annotated[str, Field(min_length=1, max_length=64, pattern=r"^[A-Za-z0-9_\-]+$")]
    display_name: Annotated[str, Field(min_length=1, max_length=200)]
    provider: Literal["openai_compatible", "anthropic"] | None = None
    base_url: Annotated[str, Field(min_length=1)]
    model: Annotated[str, Field(min_length=1)]
    api_key_env: str | None = None
    reasoning_effort: Literal["low", "medium", "high"] | None = None


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
        dev_mode: 跳过登录鉴权（仅本地开发）；上线必须 False。
        cors_origins: 允许的浏览器 Origin；空列表 = 拒绝所有跨域。
        idle_timeout_seconds: cell 空闲多久后被 ``_idle_eviction_loop`` 自动
            evict（默认 30 分钟）。下限 60s 防误配。
        idle_check_interval_seconds: 后台扫盘周期（默认 60s）。下限 10s。
        pending_approval_timeout_seconds: 单次审批等待上限（默认 60s）。下限 10s。
        ws_heartbeat_interval_ms: 前端发 ping 的间隔（毫秒）。默认 30s。
            下限 5s 防过频。
        ws_heartbeat_timeout_ms: 单次 pong 等待超时（毫秒）。默认 10s。
            下限 3s。
        ws_heartbeat_max_missed: 连续丢失几次 pong 判定连接死亡。默认 3 次。
        llm_presets: 可用的 LLM preset 列表；浏览器创建 thread 时下拉选其一。
            v0.1.5 schema 留空，由 web-app-shell 任务实际填默认 preset。
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    enabled: bool = False
    host: str = "0.0.0.0"
    port: Annotated[int, Field(ge=1, le=65535)] = 8080
    dev_mode: bool = False
    cors_origins: list[str] = Field(default_factory=list)
    idle_timeout_seconds: Annotated[int, Field(ge=60)] = 1800
    idle_check_interval_seconds: Annotated[int, Field(ge=10)] = 60
    pending_approval_timeout_seconds: Annotated[int, Field(ge=10)] = 60

    # 心跳配置（前端读取，不硬编码）
    ws_heartbeat_interval_ms: Annotated[int, Field(ge=5000)] = 30000
    """前端发 ping 的间隔（毫秒）。默认 30s。下限 5s 防过频。"""
    ws_heartbeat_timeout_ms: Annotated[int, Field(ge=3000)] = 10000
    """单次 pong 等待超时（毫秒）。默认 10s。"""
    ws_heartbeat_max_missed: Annotated[int, Field(ge=1)] = 3
    """连续丢失几次 pong 判定连接死亡。默认 3 次。"""

    llm_presets: list[LLMPresetConfig] = Field(default_factory=list)

    @model_validator(mode="after")
    def _check_unique_preset_ids(self) -> WebConfig:
        """preset id 必须全局唯一，否则后端按 id 路由 thread 时会歧义。"""
        seen: set[str] = set()
        for preset in self.llm_presets:
            if preset.id in seen:
                raise ValueError(
                    f"web.llm_presets contains duplicate id={preset.id!r}; preset id must be unique"
                )
            seen.add(preset.id)
        return self


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
    safety: SafetyConfig = Field(default_factory=SafetyConfig)
    scheduler: SchedulerConfig = Field(default_factory=SchedulerConfig)
    web: WebConfig = Field(default_factory=WebConfig)


__all__ = [
    "ApprovalConfig",
    "CliConfig",
    "CompactorConfig",
    "Config",
    "EvolutionConfig",
    "EvolutionMemoryConfig",
    "FileToolConfig",
    "HostConfig",
    "LLMPresetConfig",
    "LoggingConfig",
    "ModelConfig",
    "ReasoningProfile",
    "RetryConfig",
    "RunnerConfig",
    "SafetyApprovalRequiredConfig",
    "SafetyConfig",
    "SafetyHardDenyConfig",
    "SafetySensitivePathConfig",
    "SafetySkillCallConfig",
    "SchedulerConfig",
    "SessionConfig",
    "ShellToolConfig",
    "StreamConfig",
    "ToolConfig",
    "TraceConfig",
    "WebConfig",
]
