"""manage 配置页字段元数据真源。

本文件是 manage 配置页面的**字段元数据真源**：枚举 ``config/setting.yaml`` 全部
20 个顶层模块（model / runner / session / trace / logging / host / approval /
tool / mcp / web_search / compactor / retry / cli / evolution / stream / safety /
scheduler / web / workflow / sitian）
下的所有 leaf 字段，每字段产出 :class:`FieldMeta`（路径 / 类型 / 是否可编辑 /
枚举 / 描述 / 是否需要重启 / 所属 section group / 数值上下限）。

**真源关系**：

- 字段集合和类型来自 :mod:`infrastructure.config.models`（pydantic 模型）；
- 字段描述（``desc``）就近抄自 ``config/setting.yaml`` 行内注释或 pydantic
  ``Field`` description；
- **元数据只描述结构和编辑能力，不携带运行值**。运行值由 ``writer.py`` /
  ``manager.py`` 读 yaml 现取。

**漂移防护**：

- 20 个顶层模块若新增字段、改名、调类型，必须**同步**修改本文件——否则 manage
  UI 与真实 yaml 不一致；
- pytest 漂移测试会对比 pydantic ``Config`` 扁平 leaf path 与本文件 ``_FIELD_METAS``，
  覆盖范围一致性由测试强制断言。

**editable 默认策略**（README 已定）：

- 标量（bool/int/float/str/enum）默认 ``True``；
- 强约束标量锁定为 ``False``：``web.dev_mode`` / ``host.kind``；
- list 与嵌套 dict 一期统一 ``False``；
  ``scheduler.approval`` 拆为两个独立子字段（``mode`` / ``allow_write_file_create_in_cwd``）
  暴露给 UI 编辑——安全关键字段必须可见。

**restart_required 策略**：

- ``True``：启动期才装配的字段，运行期改无效——所有 ``web.*`` / ``scheduler.enabled`` /
  ``session.backend`` / ``host.kind`` / ``evolution.learning.*`` / ``safety.*``；
- 其余标量大部分对**当前对话**不生效但**下次对话生效**，标 ``restart_required=False``
  并在 ``desc`` 里说明"下次对话生效"。
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel

# ---------------------------------------------------------------------------
# 数据结构
# ---------------------------------------------------------------------------


FieldType = Literal["string", "int", "float", "bool", "enum", "list", "dict"]
FieldSource = Literal["yaml", "env", "default"]


class FieldMeta(BaseModel):
    """单个配置字段的元数据。"""

    path: str
    """点分路径，如 ``"model.temperature"``。"""

    type: FieldType
    """字段类型枚举。"""

    editable: bool
    """是否允许 UI 编辑。锁定字段（如 api_key / dev_mode / host.kind）= False。"""

    enum: list[str] | None = None
    """``type=enum`` 时必填的可选值列表。"""

    desc: str = ""
    """字段说明（从 yaml 注释或 pydantic Field 提取）。"""

    restart_required: bool = False
    """是否需要重启才生效。True = 启动期才装配的字段。"""

    group: str
    """section group id；必须出现在 ``list_groups()`` 返回值中。"""

    min_value: float | None = None
    """数值字段下限（来自 pydantic ``Field(ge=...)``）。"""

    max_value: float | None = None
    """数值字段上限（来自 pydantic ``Field(le=...)``）。"""


# ---------------------------------------------------------------------------
# Group 定义（显示顺序）
# ---------------------------------------------------------------------------

_GROUPS: list[dict[str, str]] = [
    {"id": "model", "label": "模型"},
    {"id": "runtime", "label": "运行"},
    {"id": "tool_approval", "label": "工具与审批"},
    {"id": "safety", "label": "安全"},
    {"id": "host_observ", "label": "宿主与观测"},
    {"id": "workflow", "label": "工作流"},
    {"id": "sitian", "label": "司天"},
]


# ---------------------------------------------------------------------------
# 字段元数据清单
# ---------------------------------------------------------------------------
#
# 字段顺序按"顶层模块 → 子字段在 pydantic 模型内的声明顺序"组织，便于人工对照
# ``src/infrastructure.config/models.py`` 排查漂移。
#
# 一致 desc 中常见后缀语义：
#   "下次对话生效" —— 改了对当前对话无影响，但下次 runner 装配会拾起新值
#   "需重启生效"   —— restart_required=True 的字段，必须重启 web server


_FIELD_METAS: list[FieldMeta] = [
    FieldMeta(
        path="config_schema_version",
        type="string",
        editable=False,
        desc="配置结构版本，由配置迁移器维护。只读字段。",
        group="runtime",
    ),
    # =======================================================================
    # group: model
    # =======================================================================
    FieldMeta(
        path="model.preset_id",
        type="string",
        editable=True,
        desc="默认模型 preset ID；静态模型定义由 model-providers.yaml 管理。下次运行生效。",
        group="model",
    ),
    FieldMeta(
        path="model.reasoning_effort",
        type="enum",
        editable=True,
        enum=["none", "low", "medium", "high", "max"],
        desc="默认推理档位；留空时采用 catalog 模型默认值。下次运行生效。",
        group="model",
    ),
    # =======================================================================
    # group: runtime —— runner / session / compactor / retry / stream / cli
    # =======================================================================
    # ---- runner ----
    FieldMeta(
        path="runner.max_turns",
        type="int",
        editable=True,
        desc="单次 run 的最大 turn 数。下次对话生效。",
        min_value=1.0,
        group="runtime",
    ),
    # ---- session ----
    FieldMeta(
        path="session.backend",
        type="enum",
        editable=True,
        enum=["memory", "sqlite", "file"],
        desc="会话存储后端：memory（内存）/ sqlite / file（JSONL，推荐）。需重启生效。",
        restart_required=True,
        group="runtime",
    ),
    FieldMeta(
        path="session.store_path",
        type="string",
        editable=True,
        desc="sqlite 后端数据库文件路径；.kongming/* 派生到 kongming_home；memory / file 后端忽略。需重启生效。",
        restart_required=True,
        group="runtime",
    ),
    FieldMeta(
        path="session.file_store_path",
        type="string",
        editable=True,
        desc="file 后端 session 目录父路径；.kongming/* 派生到 kongming_home；每个 session 一个子目录。需重启生效。",
        restart_required=True,
        group="runtime",
    ),
    # ---- compactor ----
    FieldMeta(
        path="compactor.enabled",
        type="bool",
        editable=True,
        desc="是否启用历史压缩。当前实现仅 FIFO 消息数压缩，默认关闭。下次对话生效。",
        group="runtime",
    ),
    FieldMeta(
        path="compactor.max_messages",
        type="int",
        editable=True,
        desc="送 LLM 之前历史消息超此阈值才触发压缩。下次对话生效。",
        min_value=1.0,
        group="runtime",
    ),
    FieldMeta(
        path="compactor.keep_recent",
        type="int",
        editable=True,
        desc="压缩时保留最近 N 条原样不动。下次对话生效。",
        min_value=1.0,
        group="runtime",
    ),
    FieldMeta(
        path="compactor.keep_system",
        type="bool",
        editable=True,
        desc="压缩时是否保留首条 system 指令（通常都是 True）。下次对话生效。",
        group="runtime",
    ),
    FieldMeta(
        path="compactor.tool_result_max_chars",
        type="int",
        editable=True,
        desc="单条 tool 结果被压缩时保留的最大字符数，超出截断。下次对话生效。",
        min_value=1.0,
        group="runtime",
    ),
    # ---- retry ----
    FieldMeta(
        path="retry.max_retries",
        type="int",
        editable=True,
        desc="LLM provider 单次请求的重试次数。下次对话生效。",
        min_value=0.0,
        group="runtime",
    ),
    FieldMeta(
        path="retry.retry_backoff",
        type="float",
        editable=True,
        desc="重试间隔基数（秒）。下次对话生效。",
        min_value=0.0,
        group="runtime",
    ),
    # ---- stream ----
    FieldMeta(
        path="stream.enabled",
        type="bool",
        editable=True,
        desc="是否启用流式响应。下次对话生效。",
        group="runtime",
    ),
    FieldMeta(
        path="stream.read_timeout",
        type="float",
        editable=True,
        desc="流式 read 超时（秒）；心跳期间不计入。下次对话生效。",
        min_value=0.0,
        group="runtime",
    ),
    FieldMeta(
        path="stream.suppress_content_after_tool_call",
        type="bool",
        editable=True,
        desc="流中出现 tool_call 后是否屏蔽后续 content.delta（避免乱文本）。下次对话生效。",
        group="runtime",
    ),
    FieldMeta(
        path="stream.delta_sampling",
        type="enum",
        editable=True,
        enum=["none", "periodic", "full"],
        desc="trace 对 content.delta / reasoning.delta 的采样：none（防爆磁盘）/ periodic / full。下次对话生效。",
        group="runtime",
    ),
    FieldMeta(
        path="stream.periodic_batch_size",
        type="int",
        editable=True,
        desc="delta_sampling=periodic 时的采样批大小，每 N 个 delta 取 1 个落盘。下次对话生效。",
        min_value=1.0,
        group="runtime",
    ),
    # ---- cli ----
    FieldMeta(
        path="cli.show_reasoning",
        type="bool",
        editable=True,
        desc="每轮响应后是否在终端打印模型 reasoning_content（仅当模型实际返回时输出）。下次对话生效。",
        group="runtime",
    ),
    # =======================================================================
    # group: tool_approval —— tool / approval / scheduler
    # =======================================================================
    # ---- tool.shell ----
    FieldMeta(
        path="tool.shell.enabled",
        type="bool",
        editable=True,
        desc="是否注册 shell builtin tool。下次对话生效。",
        group="tool_approval",
    ),
    FieldMeta(
        path="tool.shell.timeout_seconds",
        type="float",
        editable=True,
        desc="shell 命令单次执行超时（秒）。下次对话生效。",
        min_value=0.0,
        group="tool_approval",
    ),
    FieldMeta(
        path="tool.shell.max_stream_bytes",
        type="int",
        editable=True,
        desc="shell 命令 stdout/stderr 单次返回最大字节数。下次对话生效。",
        min_value=1.0,
        group="tool_approval",
    ),
    FieldMeta(
        path="tool.shell.terminate_grace_seconds",
        type="float",
        editable=True,
        desc="cancel 时给子进程的优雅退出宽限（秒），超时强杀。下次对话生效。",
        min_value=0.0,
        group="tool_approval",
    ),
    # ---- tool.file ----
    FieldMeta(
        path="tool.file.enabled",
        type="bool",
        editable=True,
        desc="是否注册 file builtin tool（read_file / list_dir / write_file 等）。下次对话生效。",
        group="tool_approval",
    ),
    FieldMeta(
        path="tool.file.read_max_bytes",
        type="int",
        editable=True,
        desc="read_file 单次最大读取字节数。下次对话生效。",
        min_value=1.0,
        group="tool_approval",
    ),
    # ---- mcp ----
    FieldMeta(
        path="mcp.servers",
        type="list",
        editable=False,
        desc="stdio MCP server 配置列表，包含 command、args、env、secret_env_keys、timeout 和 aliases。请在 yaml 内手工维护。",
        restart_required=True,
        group="tool_approval",
    ),
    # ---- web_search ----
    FieldMeta(
        path="web_search.enabled",
        type="bool",
        editable=True,
        desc="是否启用通用 Web Search provider 装配。需重启生效。",
        restart_required=True,
        group="tool_approval",
    ),
    FieldMeta(
        path="web_search.provider_name",
        type="string",
        editable=True,
        desc="Web Search provider 名称，会写入搜索结果 diagnostics。需重启生效。",
        restart_required=True,
        group="tool_approval",
    ),
    FieldMeta(
        path="web_search.search_tool_name",
        type="string",
        editable=True,
        desc="显式指定底层搜索工具名；为空时按候选列表自动探测。需重启生效。",
        restart_required=True,
        group="tool_approval",
    ),
    FieldMeta(
        path="web_search.search_tool_names",
        type="list",
        editable=False,
        desc="底层搜索工具自动探测候选名列表。请在 yaml 或环境变量内维护。需重启生效。",
        restart_required=True,
        group="tool_approval",
    ),
    FieldMeta(
        path="web_search.max_results",
        type="int",
        editable=True,
        desc="单次 Web Search 默认返回结果数。需重启生效。",
        restart_required=True,
        min_value=1.0,
        group="tool_approval",
    ),
    # ---- approval ----
    FieldMeta(
        path="approval.mode",
        type="enum",
        editable=True,
        enum=["interactive", "auto_allow", "auto_deny"],
        desc="审批模式：interactive（人工）/ auto_allow（自动放行，测试用）/ auto_deny（自动拒绝）。下次对话生效。",
        group="tool_approval",
    ),
    # ---- scheduler ----
    FieldMeta(
        path="scheduler.enabled",
        type="bool",
        editable=True,
        desc="是否启用 cron 模块。False 时不注册 schedule_tool 也不装配 ticker。需重启生效。",
        restart_required=True,
        group="tool_approval",
    ),
    FieldMeta(
        path="scheduler.home",
        type="string",
        editable=True,
        desc="cron 数据根目录（tasks.json / audit.jsonl 落盘）。留空走 kongming_home/cron；.kongming/* 派生到 kongming_home。需重启生效。",
        restart_required=True,
        group="tool_approval",
    ),
    FieldMeta(
        path="scheduler.interval",
        type="float",
        editable=True,
        desc="ticker 扫描间隔（秒），下限 0.1。需重启生效。",
        restart_required=True,
        min_value=0.1,
        group="tool_approval",
    ),
    FieldMeta(
        path="scheduler.max_inflight",
        type="int",
        editable=True,
        desc="同时并发跑的 cron 任务上限（asyncio.Semaphore 限流）。需重启生效。",
        restart_required=True,
        min_value=1.0,
        group="tool_approval",
    ),
    FieldMeta(
        path="scheduler.max_task_age_seconds",
        type="int",
        editable=True,
        desc="任务多久没跑就被 GC（秒）；留空表示永不 GC。需重启生效。",
        restart_required=True,
        group="tool_approval",
    ),
    FieldMeta(
        path="scheduler.default_timezone",
        type="string",
        editable=True,
        desc="LLM 创建 cron 任务不显式带 timezone 时的默认值（IANA 名，如 Asia/Shanghai）。下次任务创建生效。",
        group="tool_approval",
    ),
    FieldMeta(
        path="scheduler.default_delivery_channel",
        type="enum",
        editable=True,
        enum=["web", "cli"],
        desc="LLM 创建 cron 任务默认 delivery channel：web（浏览器）/ cli（终端）。下次任务创建生效。",
        group="tool_approval",
    ),
    FieldMeta(
        path="scheduler.default_max_turns",
        type="int",
        editable=True,
        desc="cron task.policy.max_turns 缺省时的兜底；司天扫描类重型任务建议 ≥60。下次任务执行生效。",
        min_value=1.0,
        group="tool_approval",
    ),
    FieldMeta(
        path="scheduler.approval.mode",
        type="enum",
        editable=True,
        enum=["trust", "fail_closed"],
        desc="cron 任务的审批模式：trust=预批准自动放行（默认）；fail_closed=严格审批。HardBlock 在 trust 下仍不可绕。",
        restart_required=True,
        group="tool_approval",
    ),
    FieldMeta(
        path="scheduler.approval.allow_write_file_create_in_cwd",
        type="bool",
        editable=True,
        desc="允许在 cwd 内创建文件而不弹审批。",
        restart_required=True,
        group="tool_approval",
    ),
    # =======================================================================
    # group: safety —— LLM 审批复核器
    # =======================================================================
    FieldMeta(
        path="safety.approval.llm",
        type="dict",
        editable=False,
        desc="llm 处置模式的 default:ask 复核模型。模型故障时保留人工审批；请在 setting.yaml 配置。需重启生效。",
        restart_required=True,
        group="safety",
    ),
    # =======================================================================
    # group: host_observ —— host / web / trace / logging / evolution
    # =======================================================================
    # ---- host ----
    FieldMeta(
        path="host.kind",
        type="enum",
        editable=False,
        enum=["cli"],
        desc="宿主类型。v1 仅支持 cli；锁定不可在 UI 编辑（防误改导致装配失败）。",
        restart_required=True,
        group="host_observ",
    ),
    # ---- web ----
    FieldMeta(
        path="web.enabled",
        type="bool",
        editable=True,
        desc="是否启用 web 宿主壳。False 时整个 web 路径不装配。需重启生效。",
        restart_required=True,
        group="host_observ",
    ),
    FieldMeta(
        path="web.host",
        type="string",
        editable=True,
        desc="uvicorn bind IP。仅本机用 127.0.0.1；外网访问填 0.0.0.0。需重启生效。",
        restart_required=True,
        group="host_observ",
    ),
    FieldMeta(
        path="web.port",
        type="int",
        editable=True,
        desc="HTTP / WS 端口。需重启生效。",
        restart_required=True,
        min_value=1.0,
        max_value=65535.0,
        group="host_observ",
    ),
    FieldMeta(
        path="web.server_origin",
        type="string",
        editable=True,
        desc="扫码登录和移动端 handoff 使用的服务器 origin；公网填 https://域名，局域网填 http://私网IP:端口。需重启生效。",
        restart_required=True,
        group="host_observ",
    ),
    FieldMeta(
        path="web.host_environment",
        type="enum",
        editable=False,
        enum=["browser", "xspace"],
        desc="Web sidecar 宿主环境。普通浏览器为 browser；XSpace 运行态由启动流程设置。",
        restart_required=True,
        group="host_observ",
    ),
    FieldMeta(
        path="web.dev_mode",
        type="bool",
        editable=False,
        desc="跳过登录鉴权（仅本地开发！）。锁定不可在 UI 编辑（防误开放裸 API）。需重启生效。",
        restart_required=True,
        group="host_observ",
    ),
    FieldMeta(
        path="web.initial_password",
        type="string",
        editable=True,
        desc="首次部署的明文初始密码；仅在 password.hash 缺失时生效。需重启生效。",
        restart_required=True,
        group="host_observ",
    ),
    FieldMeta(
        path="web.cors_origins",
        type="list",
        editable=False,
        desc="浏览器 Origin 白名单。空 = 拒绝所有跨域。一期只读，需重启生效。",
        restart_required=True,
        group="host_observ",
    ),
    FieldMeta(
        path="web.idle_timeout_seconds",
        type="int",
        editable=True,
        desc="cell 空闲多久后自动 evict（秒），下限 60。需重启生效。",
        restart_required=True,
        min_value=60.0,
        group="host_observ",
    ),
    FieldMeta(
        path="web.idle_check_interval_seconds",
        type="int",
        editable=True,
        desc="后台扫盘检查周期（秒），下限 10。需重启生效。",
        restart_required=True,
        min_value=10.0,
        group="host_observ",
    ),
    FieldMeta(
        path="web.dashboard_poll_interval_seconds",
        type="int",
        editable=True,
        desc="dashboard 状态页轮询周期（秒），代码层最小按 3s 生效。需重启生效。",
        restart_required=True,
        min_value=1.0,
        group="host_observ",
    ),
    FieldMeta(
        path="web.ws_heartbeat_interval_ms",
        type="int",
        editable=True,
        desc="前端发 ping 的间隔（毫秒），下限 5000。需重启生效。",
        restart_required=True,
        min_value=5000.0,
        group="host_observ",
    ),
    FieldMeta(
        path="web.ws_heartbeat_background_interval_ms",
        type="int",
        editable=True,
        desc="浏览器后台 tab 时 ping 间隔（毫秒），下限 10000。需重启生效。",
        restart_required=True,
        min_value=10000.0,
        group="host_observ",
    ),
    FieldMeta(
        path="web.ws_heartbeat_timeout_ms",
        type="int",
        editable=True,
        desc="单次 pong 等待超时（毫秒），下限 3000。需重启生效。",
        restart_required=True,
        min_value=3000.0,
        group="host_observ",
    ),
    FieldMeta(
        path="web.ws_heartbeat_max_missed",
        type="int",
        editable=True,
        desc="连续丢失几次 pong 判定连接死亡。需重启生效。",
        restart_required=True,
        min_value=1.0,
        group="host_observ",
    ),
    FieldMeta(
        path="web.full_log.enabled",
        type="bool",
        editable=True,
        desc="是否启用前后端通信全量日志。默认关闭；开启后写 WS/REST 链路日志。需重启生效。",
        restart_required=True,
        group="host_observ",
    ),
    FieldMeta(
        path="web.full_log.path",
        type="string",
        editable=True,
        desc="全量日志 JSONL 文件路径；.kongming/* 派生到 kongming_home。父目录由 FullLogger 装配阶段创建。需重启生效。",
        restart_required=True,
        group="host_observ",
    ),
    FieldMeta(
        path="web.full_log.rotate_daily",
        type="bool",
        editable=True,
        desc="是否按自然日切分全量日志文件，避免长期运行时单文件无限增长。需重启生效。",
        restart_required=True,
        group="host_observ",
    ),
    FieldMeta(
        path="web.full_log.include_http_body",
        type="bool",
        editable=True,
        desc="是否记录 REST 请求/响应 body。当前为预留字段，middleware 接入后生效。需重启生效。",
        restart_required=True,
        group="host_observ",
    ),
    FieldMeta(
        path="web.full_log.queue_size",
        type="int",
        editable=True,
        desc="全量日志异步队列容量；满时丢最旧记录并告警，防止日志写入拖垮事件循环。需重启生效。",
        restart_required=True,
        min_value=100.0,
        max_value=1_000_000.0,
        group="host_observ",
    ),
    FieldMeta(
        path="web.deep_research_source_provider.enabled",
        type="bool",
        editable=True,
        desc="是否启用 deep_research Web 来源 provider 自动装配。需重启生效。",
        restart_required=True,
        group="host_observ",
    ),
    FieldMeta(
        path="web.deep_research_source_provider.provider_name",
        type="string",
        editable=True,
        desc="deep_research 来源 provider 名称，会写入 workflow artifact。需重启生效。",
        restart_required=True,
        group="host_observ",
    ),
    FieldMeta(
        path="web.deep_research_source_provider.search_tool_name",
        type="string",
        editable=True,
        desc="显式指定 deep_research 搜索工具名；为空时按候选列表自动探测。需重启生效。",
        restart_required=True,
        group="host_observ",
    ),
    FieldMeta(
        path="web.deep_research_source_provider.fetch_tool_name",
        type="string",
        editable=True,
        desc="显式指定 deep_research URL 读取工具名；为空时按候选列表自动探测。需重启生效。",
        restart_required=True,
        group="host_observ",
    ),
    FieldMeta(
        path="web.deep_research_source_provider.search_tool_names",
        type="list",
        editable=False,
        desc="deep_research 搜索工具自动探测候选名列表。需重启生效。",
        restart_required=True,
        group="host_observ",
    ),
    FieldMeta(
        path="web.deep_research_source_provider.fetch_tool_names",
        type="list",
        editable=False,
        desc="deep_research URL 读取工具自动探测候选名列表。需重启生效。",
        restart_required=True,
        group="host_observ",
    ),
    # ---- trace ----
    FieldMeta(
        path="trace.output_path",
        type="string",
        editable=True,
        desc="trace 落盘文件路径；.kongming/* 派生到 kongming_home。下次对话生效。",
        group="host_observ",
    ),
    FieldMeta(
        path="trace.auto_flush",
        type="bool",
        editable=True,
        desc="trace 是否在每次 emit 后立即 flush。下次对话生效。",
        group="host_observ",
    ),
    FieldMeta(
        path="trace.raw_llm",
        type="bool",
        editable=True,
        desc="是否 dump 每次 LLM 请求/响应到 .kongming/debug/raw-llm-*.json（调试用，磁盘会涨）。下次对话生效。",
        group="host_observ",
    ),
    # ---- logging ----
    FieldMeta(
        path="logging.level",
        type="enum",
        editable=True,
        enum=["DEBUG", "INFO", "WARNING", "ERROR"],
        desc="日志级别。下次对话生效。",
        group="host_observ",
    ),
    # ---- evolution.memory ----
    FieldMeta(
        path="evolution.memory.enabled",
        type="bool",
        editable=True,
        desc="是否启用 memory 模块（跨会话长期记忆）。需重启生效。",
        restart_required=True,
        group="host_observ",
    ),
    FieldMeta(
        path="evolution.memory.root_path",
        type="string",
        editable=True,
        desc="memory 根目录路径；.kongming/* 派生到 kongming_home。需重启生效。",
        restart_required=True,
        group="host_observ",
    ),
    FieldMeta(
        path="evolution.memory.inject_prompt",
        type="bool",
        editable=True,
        desc="是否将 memory snapshot 注入 system prompt。需重启生效。",
        restart_required=True,
        group="host_observ",
    ),
    FieldMeta(
        path="evolution.memory.read_max_chars",
        type="int",
        editable=True,
        desc="单个 memory 文件读取的最大字符数上限。需重启生效。",
        restart_required=True,
        min_value=1.0,
        group="host_observ",
    ),
    FieldMeta(
        path="evolution.memory.view_max_chars",
        type="int",
        editable=True,
        desc="memory tool view 动作返回的最大字符数。需重启生效。",
        restart_required=True,
        min_value=1.0,
        group="host_observ",
    ),
    # ---- evolution.learning ----
    FieldMeta(
        path="evolution.learning.enabled",
        type="bool",
        editable=True,
        desc="是否启用自我进化。False 时 EvolutionManager 全 no-op。需重启生效。",
        restart_required=True,
        group="host_observ",
    ),
    FieldMeta(
        path="evolution.learning.mode",
        type="enum",
        editable=False,
        enum=["child_agent"],
        desc="进化模式。当前仅支持 child_agent；锁定。需重启生效。",
        restart_required=True,
        group="host_observ",
    ),
    FieldMeta(
        path="evolution.learning.background",
        type="bool",
        editable=True,
        desc="reviewer 是否后台异步执行（不阻塞主对话）。需重启生效。",
        restart_required=True,
        group="host_observ",
    ),
    FieldMeta(
        path="evolution.learning.auto_trigger_enabled",
        type="bool",
        editable=True,
        desc="是否按 cadence 自动触发 reviewer；关闭后仍可通过公开 Tool 显式触发。需重启生效。",
        restart_required=True,
        group="host_observ",
    ),
    FieldMeta(
        path="evolution.learning.preset_id",
        type="string",
        editable=True,
        desc="reviewer 专用 preset ID；留空继承主模型。需重启生效。",
        restart_required=True,
        group="host_observ",
    ),
    FieldMeta(
        path="evolution.learning.reasoning_effort",
        type="enum",
        editable=True,
        enum=["none", "low", "medium", "high", "max"],
        desc="reviewer 推理深度（留空继承主 agent）。需重启生效。",
        restart_required=True,
        group="host_observ",
    ),
    FieldMeta(
        path="evolution.learning.every_n_runs",
        type="int",
        editable=True,
        desc="每 N 轮用户消息触发一次 reviewer。需重启生效。",
        restart_required=True,
        min_value=1.0,
        group="host_observ",
    ),
    FieldMeta(
        path="evolution.learning.min_user_turns",
        type="int",
        editable=True,
        desc="累计不足 N 轮时不触发 reviewer（冷启动保护）。需重启生效。",
        restart_required=True,
        min_value=1.0,
        group="host_observ",
    ),
    FieldMeta(
        path="evolution.learning.max_history_messages",
        type="int",
        editable=True,
        desc="reviewer 看到的最近消息条数。需重启生效。",
        restart_required=True,
        min_value=1.0,
        group="host_observ",
    ),
    FieldMeta(
        path="evolution.learning.max_nutrients",
        type="int",
        editable=True,
        desc="单次 review 最多提炼几条养料（nutrient）。需重启生效。",
        restart_required=True,
        min_value=1.0,
        group="host_observ",
    ),
    FieldMeta(
        path="evolution.learning.nutrient_confidence_threshold",
        type="float",
        editable=True,
        desc="低于此置信度的养料直接丢弃，不进候选队列。需重启生效。",
        restart_required=True,
        min_value=0.0,
        max_value=1.0,
        group="host_observ",
    ),
    FieldMeta(
        path="evolution.learning.review_timeout_seconds",
        type="float",
        editable=True,
        desc="reviewer 单次执行超时（秒）。需重启生效。",
        restart_required=True,
        min_value=0.0,
        group="host_observ",
    ),
    FieldMeta(
        path="evolution.learning.drain_on_close_seconds",
        type="float",
        editable=True,
        desc="app 关闭时等待后台 reviewer 完成的最大时间（秒）。需重启生效。",
        restart_required=True,
        min_value=0.0,
        group="host_observ",
    ),
    FieldMeta(
        path="evolution.learning.root_path",
        type="string",
        editable=True,
        desc="evolution 产物根目录（reviews / nutrients / state）；.kongming/* 派生到 kongming_home。需重启生效。",
        restart_required=True,
        group="host_observ",
    ),
    # =======================================================================
    # group: workflow
    # =======================================================================
    FieldMeta(
        path="workflow.enabled",
        type="bool",
        editable=True,
        desc="是否启用 agent workflow 能力。需重启生效。",
        restart_required=True,
        group="workflow",
    ),
    FieldMeta(
        path="workflow.home",
        type="string",
        editable=True,
        desc="workflow 数据根目录；留空走 kongming_home/workflows。需重启生效。",
        restart_required=True,
        group="workflow",
    ),
    FieldMeta(
        path="workflow.scan_interval",
        type="float",
        editable=True,
        desc="workflow scanner 扫描间隔（秒）。需重启生效。",
        restart_required=True,
        min_value=0.0,
        group="workflow",
    ),
    # =======================================================================
    # group: sitian
    # =======================================================================
    FieldMeta(
        path="sitian.version",
        type="enum",
        editable=False,
        enum=["v1"],
        desc="司天配置结构版本。只读字段。",
        restart_required=True,
        group="sitian",
    ),
    FieldMeta(
        path="sitian.default_scan_interval_sec",
        type="int",
        editable=True,
        desc="source 未声明 scan_interval_sec 时使用的默认扫描间隔（秒）。需重启生效。",
        restart_required=True,
        min_value=1.0,
        group="sitian",
    ),
    FieldMeta(
        path="sitian.idle_sleep_sec",
        type="int",
        editable=True,
        desc="司天循环空闲时的 sleep 时长（秒）。需重启生效。",
        restart_required=True,
        min_value=1.0,
        group="sitian",
    ),
    FieldMeta(
        path="sitian.output_subdir",
        type="string",
        editable=True,
        desc="司天产物相对子目录；留空直接写入 root_dir。需重启生效。",
        restart_required=True,
        group="sitian",
    ),
    FieldMeta(
        path="sitian.scanner.recent_session_window_days",
        type="int",
        editable=True,
        desc="最近活动窗口（天）；0 表示读取所有 session。需重启生效。",
        restart_required=True,
        min_value=0,
        group="sitian",
    ),
    FieldMeta(
        path="sitian.scanner.session_recent_user_messages",
        type="int",
        editable=True,
        desc="每个 session 取最后 N 条 user 消息；0 表示不取。需重启生效。",
        restart_required=True,
        min_value=0,
        group="sitian",
    ),
    FieldMeta(
        path="sitian.scanner.session_recent_assistant_messages",
        type="int",
        editable=True,
        desc="每个 session 取最后 N 条 assistant 消息；0 表示不取。需重启生效。",
        restart_required=True,
        min_value=0,
        group="sitian",
    ),
    FieldMeta(
        path="sitian.scanner.session_message_max_chars",
        type="int",
        editable=True,
        desc="单条消息最大字符数；0 表示不截断。需重启生效。",
        restart_required=True,
        min_value=0,
        group="sitian",
    ),
    FieldMeta(
        path="sitian.analyzer.enabled",
        type="bool",
        editable=True,
        desc="是否启用司天 LLM 分析层。需重启生效。",
        restart_required=True,
        group="sitian",
    ),
    FieldMeta(
        path="sitian.analyzer.preset_id",
        type="string",
        editable=True,
        desc="司天分析层专用 preset ID；留空继承主模型。需重启生效。",
        restart_required=True,
        group="sitian",
    ),
    FieldMeta(
        path="sitian.analyzer.reasoning_effort",
        type="enum",
        editable=True,
        enum=["none", "low", "medium", "high", "max"],
        desc="司天分析层默认推理档位；留空采用 catalog 默认。需重启生效。",
        restart_required=True,
        group="sitian",
    ),
    FieldMeta(
        path="sitian.analyzer.max_context_chars",
        type="int",
        editable=True,
        desc="司天分析层单次提示词最大上下文字符数。需重启生效。",
        restart_required=True,
        min_value=1.0,
        group="sitian",
    ),
    FieldMeta(
        path="sitian.analyzer.skip_if_unchanged",
        type="bool",
        editable=True,
        desc="输入上下文未变化时跳过重复分析。需重启生效。",
        restart_required=True,
        group="sitian",
    ),
    FieldMeta(
        path="sitian.analyzer.full_log_enabled",
        type="bool",
        editable=True,
        desc="是否记录完整 LLM 提示词和回复到司天 full-log。需重启生效。",
        restart_required=True,
        group="sitian",
    ),
    FieldMeta(
        path="sitian.interests.projects",
        type="list",
        editable=False,
        desc="司天关注项目列表。请在 yaml 内手工维护。需重启生效。",
        restart_required=True,
        group="sitian",
    ),
    FieldMeta(
        path="sitian.interests.focus",
        type="string",
        editable=True,
        desc="司天关注重点，会注入分析层 prompt。需重启生效。",
        restart_required=True,
        group="sitian",
    ),
    FieldMeta(
        path="sitian.sources",
        type="list",
        editable=False,
        desc="司天 source 配置列表。请在 yaml 内手工维护。需重启生效。",
        restart_required=True,
        group="sitian",
    ),
]


# ---------------------------------------------------------------------------
# 公共 API
# ---------------------------------------------------------------------------


def list_field_metas() -> list[FieldMeta]:
    """返回所有字段元数据（扁平 path 顺序按显示分组）。"""
    return list(_FIELD_METAS)


def list_groups() -> list[dict[str, str]]:
    """返回 group 的 ``id`` / ``label`` 列表（按显示顺序）。"""
    return [dict(g) for g in _GROUPS]


def get_field_meta(path: str) -> FieldMeta | None:
    """按 ``path`` 查单字段元数据；找不到返回 ``None``。"""
    for meta in _FIELD_METAS:
        if meta.path == path:
            return meta
    return None


__all__ = [
    "FieldMeta",
    "FieldSource",
    "FieldType",
    "get_field_meta",
    "list_field_metas",
    "list_groups",
]
