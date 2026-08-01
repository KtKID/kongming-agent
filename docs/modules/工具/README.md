# src/tools/ — 工具

最小 Tool Runtime：工具注册/查找/分发、三种 `ApprovalProvider` 默认实现、基础 builtin tool（读/写/列目录/shell/web_fetch）+ 一个长期记忆 tool（`memory`）+ v0.1.6 first-class `SkillTool` + agent workflow 编排入口。

## 设计理念

| 决策 | 理由 |
|------|------|
| 协议真源只在 `core.contracts`，本包**不**重定义 Protocol | 避免 `Tool` / `ApprovalProvider` 在 `tools/` 下再长出一份副本，整合时会冲突。`tools/runtime/base.py` 只是便利基类，不是协议。 |
| `ToolRegistry` 天然满足 `ToolLookup` Protocol | 同时实现 `__contains__` / `__getitem__`，runner 只依赖 `ToolLookup` 抽象，不感知具体注册容器；测试里也可以直接传 dict。 |
| tools 层**不做**任何安全判断（路径白名单 / 命令黑名单 / 审批决策） | 安全归 `safety/`，tool 只专注「能把事情做对」。违反会让职责跨层蔓延。 |
| 不 import `safety.capability_policy` / `safety.permission_policy` | 硬约束。由 `.importlinter` Contract 4 `tools-no-direct-safety-policy` 背书：tools 只消费装配层组装好的高层 `ApprovalProvider`。 |
| ApprovalProvider 的「是否进 ask」判断**不**在这里 | 本模块只负责「被问到时给出 yes/no」；「这次调用是否需要问」由 `safety.permission_policy` 决定。两者严格分工。 |
| builtin tool 必须返回结构化 `ToolResult`，可继承 `BaseBuiltinTool` 或在工具内自管异常映射 | 保证 runner 拿到稳定结果；复杂工具可以把安全、请求和失败映射收在本模块内部。 |
| `prepare` 是参数语义的唯一 owner，`execute` 只消费 `PreparedToolCall` | required 校验、默认填充、路径归一化和 execution scope 全部在审批前完成；审批事实与实际执行事实保持一致。 |
| `web_fetch` 把联网复杂度收在工具模块内部 | URL 安全、DNS 真实 IP、redirect 复查、正文抽取、垃圾页识别、关键词窗口和分页都由 `tools.builtin.web_fetch_tool` 编排，调用方只消费 `web_fetch(url, query?, offset?)`。 |
| `AgentWorkflowTool` 通过 `AgentWorkflowHandle` 延迟绑定 manager | CLI 先注册 tool，`SessionEngine.build()` 完成后再把 `AgentWorkflowManager` 绑定到 handle；tool 层负责 payload 校验、mode 透传和结果格式化。 |

## 核心概念

- **三种 ApprovalProvider 实现**：
  - `InteractiveApproval(prompt_fn)` —— 命中 ask 时调用外部注入的回调（CLI 读一行 y/n）。
  - `AutoAllowApproval` —— 全放行，服务自动化测试。
  - `AutoDenyApproval` —— 全拒绝，服务 deny 分支压测与紧急禁用。
  - 工厂 `build_default_approval(mode, prompt_fn=...)` 按 `config.approval.mode` 一把选定。
- **builtin tool**：`ReadFileTool` / `WriteFileTool` / `ListDirTool`（`builtin/file_tool.py`）+ `ShellTool`（`builtin/shell_tool.py`）+ `WebFetchTool`（`builtin/web_fetch_tool.py`）+ `MemoryTool`（`builtin/memory_tool.py`）。全部 async；`ShellTool.prepare()` 在审批前把参数 cwd 与 `ToolContext` cwd 解析为 canonical absolute effective cwd，执行阶段只消费 prepared cwd；subprocess 默认 30s 超时，stdout/stderr 各截断到 8KiB。
- **统一 preparation 边界**：File/Choice/TaskProgress/AgentRole/Memory/EvolutionWrite/AgentWorkflow/Schedule 各自的类型校验、默认填充和语义归一化都在 `prepare()` 完成。File 审批参数含 canonical absolute path；workflow 审批参数含规范化 payload；schedule create 审批参数含 trigger、默认策略和冻结的 `next_run_at`。map_reduce inline 输入在 prepare 只生成确定路径计划，批准后再按计划写盘。
- **MemoryTool 独立装配**：由 CLI / Web generic_chat 宿主在 `cfg.evolution.memory.enabled=True` 时创建并加载 `MemoryStore`，再通过 `build_memory_tool(store, view_max_chars=..., event_sinks=...)` 注入已有 `ToolRegistry`；不进 `build_default_registry`（避免 memory 模块被强绑到"默认工具集"）。Web 同时把 store 交给共享 instruction loader，按 `inject_prompt` 注入冻结快照。
- **Evolution 两级 Tool**：进化模块通过 `EvolutionManager.register_runtime_tools()` 向共享 registry 注册公开 `request_evolution_review` 与私有 `evolution_write`。主 Agent enabled names 只含前者；child reviewer 的 restricted registry 只含后者。公开 Tool schema 只含可选 `focus`，最终 memory/skill 去向继续由用户在 review 阶段决定。
- **SkillTool（v0.1.6 first-class tool）**：`tools/builtin/skill_tool.py`，progressive disclosure 入口——listing 进 system prompt 让模型发现，body 通过 `tool_call("skill", {"skill": name, "args"?: ...})` 按需读盘。`re.sub` 单遍变量替换（`$ARGUMENTS` / `${KONGMING_SKILL_DIR}` / `${KONGMING_SESSION_ID}`，**不递归**）；`!command` 永久禁用（仅做字面 token 检测）；emit 三事件 `skill.invoked` / `skill.completed` / `skill.failed`，per-sink try/except 隔离。`_SkillSpecLike` Protocol 做结构子类型，避开 `tools→prompting` 跨层 import。
- **AgentWorkflowTool**：`describe_agent_workflow_strategy` 查询已注册 workflow 的 payload 字段、示例、风险和输出；`run_agent_workflow` 是通用编排入口，输入开放的 `mode` + `payload`，调用 `AgentWorkflowManager.run_workflow_payload(...)`；`run_parallel_subagents` 是兼容入口，继续校验 `tasks`、`scoped_workdir` 权限和 file tool 白名单，并调用 `AgentWorkflowManager.run_workflow_specs(...)`。可用 mode 由 workflow strategy registry 和 system prompt 中的 workflow catalog 告知模型。
- **装配糖**：`build_default_registry(file_enabled, shell_enabled, ..., skill_specs=None, skill_event_sinks=())` 按配置开关一次装好默认 registry；非空 `skill_specs` 时 cast 注册 `SkillTool`；`build_file_tools` / `build_shell_tool` 各自按开关返回工具列表。

## 代码索引

| 文件 | 导出/内容 | 说明 |
|------|-----------|------|
| `__init__.py` | 公共 API + `build_default_registry`（v0.1.6 加 `skill_specs` / `skill_event_sinks` 入参） | 包门面：重新导出 runtime / builtin 公共能力 + 装配糖。`SkillTool` 仅在非空 `skill_specs` 时局部 import 装配（避免空场景下的运行期模块加载）。 |
| `runtime/base.py` | `BaseBuiltinTool` | builtin tool 便利基类：`prepare` 在审批前完成 required 校验并生成独立快照；`execute` 只消费 `PreparedToolCall`、调用 `_run` 并把异常包装为 `ToolResult(ok=False)`。**不是** `Tool` Protocol 真源。 |
| `runtime/registry.py` | `ToolRegistry` | 具体注册容器，`name` 为唯一主键，禁止重复注册；满足 `ToolLookup` Protocol。 |
| `runtime/approval.py` | `InteractiveApproval` / `AutoAllowApproval` / `AutoDenyApproval` / `PromptFn` / `PromptActionFn` / `mark_action_aware` / `build_default_approval` | `ApprovalProvider` 的三种底层实现 + 工厂。`InteractiveApproval` 把 bool 或 `ApprovalAction` prompt 结果投影为通用 `ApprovalDecision`；thread remember 由上层 `ApprovalManager + PermissionsManager` 编排。 |
| `builtin/file_tool.py` | `ReadFileTool` / `WriteFileTool` / `ListDirTool` / `build_file_tools` | 最小文件能力：读文本（默认 64KiB 截断）、写文本、列目录。全部 `Path.resolve()` 后落绝对路径。 |
| `builtin/shell_tool.py` | `ShellTool` / `build_shell_tool` | Shell 工具及 `ToolCallPreparer` 实现。effective cwd 必须来自绝对 ToolContext cwd或显式参数；相对路径、`..` 与软链接统一规范化，缺失/不存在/非目录在审批前失败。`_run()` 直接消费 prepared canonical cwd；cancel 时继续执行 `process.kill() + await wait()`。 |
| `builtin/web_fetch_tool.py` | `WebFetchTool` / `build_web_fetch_tool` | 联网正文读取工具。模块顶部常量集中声明 `_FETCH_TOKEN_BUDGET`、`_CHAR_PER_TOKEN`、`_HTTP_TIMEOUT_S`、`_USER_AGENT`、`_ALLOW_PRIVATE_NETWORK`、`_JUNK_MIN_CHARS`、关键词窗口和 redirect 上限；执行时先校验 URL 和 DNS 真实 IP，redirect 目标复查后再请求，`trafilatura` 抽 markdown，最后按预算和 offset 分页。 |
| `builtin/memory_tool.py` | `MemoryTool` / `build_memory_tool` | 长期记忆工具，4 个 action：`view` / `add` / `replace` / `remove`。target 只能是 `memory` / `user` / `errors`。写入走 `memory.safety_write.execute_write`（内容扫描 + 原子写入 + emit `memory.write.*` 事件）；写入成功后从磁盘重读刷新 `MemoryStore` 活态条目。 |
| `builtin/__init__.py` | 子包门面，re-export 所有 builtin tool 和工厂 | 内置工具聚合入口，供外部宿主或测试按需消费。内部实现继续优先使用精确模块路径。 |
| `builtin/skill_tool.py` | `SkillTool` / `SKILL_TOOL_SCHEMA` / `SkillSecurityError` / `substitute_vars` / `assert_no_command_substitution` / `_SkillSpecLike` Protocol | First-class skill tool。**不继承** `BaseBuiltinTool`（基类异常吞会让 `skill.failed` 拿不到 `error_kind`，自管 try/except）。`async execute` 7 步：emit invoked → spec lookup → `asyncio.to_thread` 读 body → `!command` 拒绝 → `re.sub` 单遍变量替换 → emit completed → 返回 `ToolResult`；任意失败走 `skill.failed` + `ToolResult.error`。`_emit` per-sink try/except 隔离 sink 异常。 |
| `builtin/evolution_write_tool.py` | `EvolutionWriteTool` / `build_evolution_write_tool` | 进化系统专用写工具。继承 `BaseBuiltinTool`，仅供 child reviewer agent 调用。接收 `review_result` / `transcript_window` / `trigger_reason` 三个参数，做 payload 过滤（confidence 阈值 + nutrient 数上限）、reviewer session fallback、统一状态更新，写入 `.kongming/evolution/`。 |
| `src/evolution/review_request_tool.py` | `RequestEvolutionReviewTool` / `build_request_evolution_review_tool` | 进化模块拥有的主 Agent 公开 Tool adapter。读取 `ToolContext.session_id/run_id`，规范化可选 focus，并只调用 `EvolutionManager.queue_manual_review()`。 |
| `builtin/schedule_tool.py` | `ScheduleTool` / `build_schedule_tool` | 定时任务 LLM Tool 主入口（v0.2）。6 个 action：`create`（自动解析自然语言/cron schedule）、`list`（默认仅 enabled）、`pause` / `resume`（切换 enabled+state）、`run_now`（立即触发 fresh agent run）、`remove`。不直接 import `safety.*`（import-linter Contract 5）；`run_now` 走注入的 `runtime_factory_fn`。 |
| `agent_workflow_tool.py` | `AgentWorkflowHandle` / `DescribeAgentWorkflowStrategyTool` / `RunAgentWorkflowTool` / `RunParallelSubagentsTool` / `build_describe_agent_workflow_strategy_tool` / `build_run_agent_workflow_tool` / `build_agent_workflow_tool` | agent workflow 编排 tool 入口。`AgentWorkflowHandle` 做装配期 late bind；`describe_agent_workflow_strategy` 按 mode 返回策略详情；`run_agent_workflow` 透传完整策略 payload；`run_parallel_subagents` 保留旧任务列表入口；run 工具统一格式化 workflow 报告。 |

## 配置

本包不直接读配置文件，但受以下 `infrastructure.config.models.Config` 字段驱动（由装配层翻译传入）：

- `config.approval.mode` → `build_default_approval` 的 `mode` 参数（`interactive` / `auto_allow` / `auto_deny`）。
- `config.tool.file.enabled` / `file.read_max_bytes` → `build_default_registry(file_enabled=..., file_read_max_bytes=...)`。
- `config.tool.shell.enabled` / `shell.timeout_seconds` / `shell.max_stream_bytes` / `shell.terminate_grace_seconds` → `build_default_registry(shell_enabled=..., shell_timeout_seconds=..., ...)`（v0.1.3 起 shell 运行参数已从文件内常量上升到配置层）。
- `config.evolution.memory.*` → CLI 用来决定是否构造 `MemoryStore` 并调 `build_memory_tool` 注册进已有 registry。
- `config.evolution.learning.enabled` → Manager 是否注册两级进化 Tool；`auto_trigger_enabled` 只控制 cadence，公开 Tool 保持可用。
- `web_fetch` 数值调参当前集中在 `src/tools/builtin/web_fetch_tool.py` 顶部常量；全局 Config 接入留给后续评估。
- agent workflow tool 由 CLI/Web 直接调用 `register_agent_workflow_tool(registry, handle)` 注册 `describe_agent_workflow_strategy` / `run_agent_workflow` / `run_parallel_subagents`，并在 `AgentWorkflowManager` 创建后 `handle.bind(manager)`；当前注册路径由宿主装配控制。

## 已知问题 / 待完成

- **`BaseBuiltinTool._validate_args` 只查 required 字段存在性**，不做类型或枚举校验；docstring 承诺后续可在同一钩子上升级到 jsonschema 而不动子类。
- **`ShellTool` 无 env 白名单**：cwd 的执行事实由 Tool preparation 固定，命令授权范围由 `safety/` 的 exact-cwd permission 规则负责。
- **更多联网能力**：当前落地 `web_fetch` 原子工具；组合式深度研究、rerank 和多源交叉验证继续放在应用/workflow 层。

## 参考

- 协议与边界：[`docs/spec/kongming-agent-v1-minimal/10-contracts.md`](../../spec/kongming-agent-v1-minimal/10-contracts.md) 「Approval 边界」「Capability 与 Permission」节明确限定 tools 只消费装配后的 `ApprovalProvider`。
- 文件职责：[`docs/spec/kongming-agent-v1-minimal/11-v1-file-layout.md`](../../spec/kongming-agent-v1-minimal/11-v1-file-layout.md) 的 `tools/` 小节。
- 架构边界 CI：[`.importlinter`](../../../.importlinter) Contract 4 `tools-no-direct-safety-policy` 强制 `tools` 不得 import `safety.capability_policy` / `safety.permission_policy`。
- 相关核心协议：[`docs/modules/核心/README.md`](../核心/README.md) 的 `Tool` / `ToolLookup` / `ApprovalProvider`。
- 编排执行层：[`docs/modules/执行器/README.md`](../执行器/README.md) 的 agent workflow 编排小节。
