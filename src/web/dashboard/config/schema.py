"""manage 配置页字段元数据真源。

本文件是 manage 配置页面的**字段元数据真源**：枚举 ``config/setting.yaml`` 全部
16 个顶层模块（model / runner / session / trace / logging / host / approval /
tool / compactor / retry / cli / evolution / stream / safety / scheduler / web）
下的所有 leaf 字段，每字段产出 :class:`FieldMeta`（路径 / 类型 / 是否可编辑 /
枚举 / 描述 / 是否需要重启 / 所属 section group / 数值上下限）。

**真源关系**：

- 字段集合和类型来自 :mod:`infrastructure.config.models`（pydantic 模型）；
- 字段描述（``desc``）就近抄自 ``config/setting.yaml`` 行内注释或 pydantic
  ``Field`` description；
- **元数据只描述结构和编辑能力，不携带运行值**。运行值由 ``writer.py`` /
  ``manager.py`` 读 yaml 现取。

**漂移防护**：

- 16 个顶层模块若新增字段、改名、调类型，必须**同步**修改本文件——否则 manage
  UI 与真实 yaml 不一致；
- ``sitian`` / ``workflow`` 两顶层模块**不在本期范围**（一期 stage 1 只覆盖 5
  个 group），等后续 stage 再加。task #9 会写 pytest 检测漂移，覆盖范围一致性
  会在那里被强制断言。

**editable 默认策略**（README 已定）：

- 标量（bool/int/float/str/enum）默认 ``True``；
- 强约束标量锁定为 ``False``：``model.api_key`` / ``web.dev_mode`` / ``host.kind``；
- list 与嵌套 dict 一期统一 ``False``（``safety.*_commands`` / ``safety.sensitive_paths`` /
  ``model.reasoning_profiles`` / ``web.llm_presets`` / ``safety.trusted_workdirs`` /
  ``safety.allow_writes`` / ``safety.allow_tools_silent`` / ``model.provider_routing``）；
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
    """section group：``model`` / ``runtime`` / ``tool_approval`` / ``safety`` / ``host_observ``。"""

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
    # =======================================================================
    # group: model
    # =======================================================================
    FieldMeta(
        path="model.provider",
        type="enum",
        editable=True,
        enum=["openai_compatible", "anthropic"],
        desc="provider 协议。通常省略让 URL 决定；仅未知厂商 + URL 无强信号时显式写。下次对话生效。",
        group="model",
    ),
    FieldMeta(
        path="model.name",
        type="string",
        editable=True,
        desc="模型名，透传给 provider 的 model 字段。下次对话生效。",
        group="model",
    ),
    FieldMeta(
        path="model.base_url",
        type="string",
        editable=True,
        desc="provider HTTP 服务根地址。本地模型也走这个字段。下次对话生效。",
        group="model",
    ),
    FieldMeta(
        path="model.provider_routing",
        type="dict",
        editable=False,
        desc="host → provider 路由表（按协议分组）。一期只读，请在 yaml 内手工维护。",
        group="model",
    ),
    FieldMeta(
        path="model.api_key",
        type="string",
        editable=False,
        desc="鉴权密钥。锁定不可在 UI 编辑（防误改泄漏）；请走 .env 文件或环境变量。",
        group="model",
    ),
    FieldMeta(
        path="model.timeout",
        type="float",
        editable=True,
        desc="单次请求超时（秒）。下次对话生效。",
        min_value=0.0,
        group="model",
    ),
    FieldMeta(
        path="model.max_tokens",
        type="int",
        editable=True,
        desc="单次响应最大 token 数。下次对话生效。",
        min_value=1.0,
        group="model",
    ),
    FieldMeta(
        path="model.temperature",
        type="float",
        editable=True,
        desc="采样温度 [0, 2]。0 最确定，2 最随机。下次对话生效。",
        min_value=0.0,
        max_value=2.0,
        group="model",
    ),
    FieldMeta(
        path="model.reasoning_effort",
        type="enum",
        editable=True,
        enum=["low", "medium", "high"],
        desc="推理深度。仅当 reasoning_profiles 命中模型时生效；不命中静默忽略。下次对话生效。",
        group="model",
    ),
    FieldMeta(
        path="model.reasoning_profiles",
        type="dict",
        editable=False,
        desc="模型 reasoning 能力声明表（key=模型名/前缀）。一期只读，请在 yaml 内手工维护。",
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
        desc="sqlite 后端数据库文件路径；memory / file 后端忽略。需重启生效。",
        restart_required=True,
        group="runtime",
    ),
    FieldMeta(
        path="session.file_store_path",
        type="string",
        editable=True,
        desc="file 后端 session 目录父路径，每个 session 一个子目录。需重启生效。",
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
        desc="cron 数据根目录（tasks.json / audit.jsonl 落盘）。留空走 .kongming/cron。需重启生效。",
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
    # group: safety —— 整段一期只读
    # =======================================================================
    FieldMeta(
        path="safety.hard_deny_commands",
        type="list",
        editable=False,
        desc="用户追加的 hard-block 命令规则。一期只读，请在 yaml 内手工维护。",
        group="safety",
    ),
    FieldMeta(
        path="safety.approval_required_commands",
        type="list",
        editable=False,
        desc="用户追加的需审批命令规则。一期只读，请在 yaml 内手工维护。",
        group="safety",
    ),
    FieldMeta(
        path="safety.sensitive_paths",
        type="list",
        editable=False,
        desc="用户追加的路径风险规则。一期只读，请在 yaml 内手工维护。",
        group="safety",
    ),
    FieldMeta(
        path="safety.skill_call_rules",
        type="list",
        editable=False,
        desc="用户追加的 skill 调用规则（v0.1.6）。一期只读，请在 yaml 内手工维护。",
        group="safety",
    ),
    FieldMeta(
        path="safety.trusted_workdirs",
        type="list",
        editable=False,
        desc="默认作用域的低风险工作目录（项目相对路径）。一期只读，请在 yaml 内手工维护。",
        group="safety",
    ),
    FieldMeta(
        path="safety.allow_writes",
        type="list",
        editable=False,
        desc="静默放行的写路径（字符串字面前缀比对，写绝对路径）。一期只读。",
        group="safety",
    ),
    FieldMeta(
        path="safety.allow_tools_silent",
        type="list",
        editable=False,
        desc="静默放行的工具名集合。一期只读，请在 yaml 内手工维护。",
        group="safety",
    ),
    FieldMeta(
        path="safety.log_silent_reads",
        type="bool",
        editable=False,
        desc="silent_allow + read 是否写 trace（默认 false 防膨胀）。一期只读，需重启生效。",
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
        path="web.pending_approval_timeout_seconds",
        type="int",
        editable=True,
        desc="单次审批等待上限（秒，超时默认拒绝），下限 10。需重启生效。",
        restart_required=True,
        min_value=10.0,
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
        path="web.llm_presets",
        type="list",
        editable=False,
        desc="LLM preset 列表；浏览器创建 thread 时下拉选其一。一期只读，请在 yaml 内手工维护。需重启生效。",
        restart_required=True,
        group="host_observ",
    ),
    # ---- trace ----
    FieldMeta(
        path="trace.output_path",
        type="string",
        editable=True,
        desc="trace 落盘文件路径。下次对话生效。",
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
        desc="memory 根目录路径。需重启生效。",
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
        path="evolution.learning.model_name",
        type="string",
        editable=True,
        desc="reviewer 模型名（留空继承主 agent）。需重启生效。",
        restart_required=True,
        group="host_observ",
    ),
    FieldMeta(
        path="evolution.learning.base_url",
        type="string",
        editable=True,
        desc="reviewer 独立 API 地址（留空继承主 agent）。需重启生效。",
        restart_required=True,
        group="host_observ",
    ),
    FieldMeta(
        path="evolution.learning.api_key_env",
        type="string",
        editable=True,
        desc="reviewer API key 所在的环境变量名（填变量名不填 key 本身）。需重启生效。",
        restart_required=True,
        group="host_observ",
    ),
    FieldMeta(
        path="evolution.learning.provider",
        type="string",
        editable=True,
        desc="reviewer provider 类型：openai_compatible / anthropic（留空自动检测）。需重启生效。",
        restart_required=True,
        group="host_observ",
    ),
    FieldMeta(
        path="evolution.learning.reasoning_effort",
        type="enum",
        editable=True,
        enum=["low", "medium", "high"],
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
        desc="evolution 产物根目录（reviews / nutrients / state）。需重启生效。",
        restart_required=True,
        group="host_observ",
    ),
]


# ---------------------------------------------------------------------------
# 公共 API
# ---------------------------------------------------------------------------


def list_field_metas() -> list[FieldMeta]:
    """返回所有字段元数据（扁平 path 顺序按显示分组）。"""
    return list(_FIELD_METAS)


def list_groups() -> list[dict[str, str]]:
    """返回 5 个 group 的 ``id`` / ``label`` 列表（按显示顺序）。"""
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
