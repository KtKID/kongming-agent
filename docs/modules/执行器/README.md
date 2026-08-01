# src/infrastructure/llm_providers/ + src/runtime_assembly/ — 执行器

把 agent 跑起来的执行落地层：下沉 LLM provider 适配实现（含流式 / 推理 / 原始响应 dump），上抬进程内运行时装配。

## 设计理念

| 决策 | 理由 |
|------|------|
| 拆成 `infrastructure/llm_providers/` + `runtime_assembly/` 两个包 | provider 实现（外设适配）与运行时装配（跨层组合）职责不同，避免单文件变成"什么都往里堆" |
| `BaseLLMProvider` 不是 `LLMProvider` Protocol 真源 | Protocol 真源在 `core/contracts/`；这里只是给 provider 实现方共用的便利基类（重试 / 超时 / httpx 生命周期 / OpenAI chat 格式转换） |
| `MediaAdapter` Protocol 做运行时鸭子类型 | 多模态媒体转换由独立 Protocol 定义，Anthropic 实现 + OpenAI stub（Phase 1）；未知 kind / 缺字段返回 None + warning log（容错退化策略） |
| `SessionEngine` 只装不跑 | `core.runner.Runner` 是唯一 run loop；`session_engine` 只负责把 provider / tools / session / event sinks / 安全链装好再交给 Runner |
| `build()` 与 `__init__` 职责分离 | `__init__` 保持显式依赖注入（便于 host / cli / tests 完全控制）；`build()` 是"按 Config 装一份默认依赖"的工厂糖 |
| 装配层允许跨层 import `safety` 和 `prompting` | 默认装 `SafetyGatedApproval` 和 `HistoryCompactor` 是装配层本职；两条跨层在 `.importlinter` 显式白名单化 |
| `BaseLLMProvider` 懒加载并**复用** `httpx.AsyncClient` | 命中 TLS keepalive 省掉每次握手 ~100-300ms；timeout 不绑在 client 级而是 per-request 覆盖，保证不同 `LLMRequest.timeout_seconds` 自适应；`SessionEngine.aclose` 负责释放 |
| Provider 按 immutable snapshot 的 protocol 分派 | `ModelCatalogManager` 解析 `ResolvedModelConfig`，factory 按 `openai` / `anthropic` 构造 provider；credential 单独解析 |
| reasoning adapter 不感知厂商 | `infrastructure/llm_providers/reasoning.py` 只做 `effort → ResolvedReasoningPlan` 映射；provider 消费该结构注入 payload。provider 不自持 model 前缀白名单 |
| provider usage 只走统一门户 | `ProviderUsageManager` 按 Anthropic Messages、OpenAI Chat Completions、OpenAI Responses family 收集并归一化 raw usage；Runner、Session、trace、Web/CLI 只消费 `ProviderUsageSnapshot` |
| 流式 provider 通过独立 Protocol `SupportsLLMStream` 能力探测 | runner 用 `isinstance(llm, SupportsLLMStream)` 判断，不靠 `NotImplementedError` 控制流；现有不支流式的 provider/stub 不用改 |
| `AgentWorkflowManager` 做编排门面，内部持有策略注册表 | Tool/CLI 通过 manager 查看策略目录、获取中文说明和执行 workflow；策略注册、planned 标记、mode 分发由 `AgentWorkflowStrategyManager` 统一管理 |
| 应用编排迁移到 `application/` | `runtime_assembly/` 保持 composition root，workflow、strategies、subagent 和 scheduled run 执行桥统一归入应用编排层 |

## 核心概念

### provider 实现侧基类

`BaseLLMProvider.complete()` 承接 `LLMProvider.complete` Protocol 语义，子类只实现 `_do_complete()`，即可继承：

- 指数退避重试（`max_retries=3`，`retry_backoff=1.0`，带抖动）
- 顶层 `asyncio.wait_for` 超时兜底（优先 `LLMRequest.timeout_seconds`，否则 resolved snapshot 的 `timeout`）
- 错误归一化为 `core.errors.ProviderError`
- 懒加载共享 `httpx.AsyncClient`（`_ensure_client`）；`aclose()` 释放
- 静态 helper：`messages_to_openai_format` / `tools_to_openai_format` / `openai_choice_to_message`

`_is_retryable` 默认把 `ProviderError` 视为终态失败（不重试），其余 `Exception` 视为瞬时（可重试）。子类可覆盖加细规则。

### Reasoning adapter（provider-agnostic）

`infrastructure/llm_providers/reasoning.py` 提供三件套：

- `ReasoningConfig(enabled, effort)` —— 单次请求统一语义
- `ResolvedReasoningPlan(model_name, send_reasoning, normalized_effort, adapter_name, payload_patch)` —— resolver 输出
- `resolve_reasoning_plan(model_name, config, capability)` —— 以 resolved model snapshot 的直接 capability 为输入，翻译成厂商 payload patch

Catalog v2 支持 GLM thinking toggle/budget、DeepSeek OpenAI/Anthropic thinking、Anthropic toggle、兼容 reasoning 与 configurable patch。`none` 会生成显式关闭 patch；unsupported effort 在 HTTP I/O 前返回 typed error。

Provider 只消费 `ResolvedReasoningPlan.payload_patch`，不再维护自己的硬编码前缀白名单。

### 流式（v0.2 引入）

`core.contracts.LLMStreamChunk` + `SupportsLLMStream` Protocol 构成 provider-agnostic 的流式契约。本包下的落地件：

- `sse_reader.py::iter_sse_events(response)` — 共享 SSE 行缓冲 + JSON 解包，返回 `(event_name, data)` 二元组。OpenAI-compatible 流 `event_name` 为 None，Anthropic 流按生命周期事件名返回
- `openai_compat_stream_parser.py::OpenAICompatStreamParser` — OpenAI / GLM / DeepSeek / Kimi / Qwen / LM Studio / Ollama / vLLM 等 chat completions 流的归一化 parser，`delta.reasoning_content or delta.reasoning` 双字段探测、按 `index` 聚合 tool_call、终态校验 arguments JSON、yield `kind="message.done"` 终态 chunk

详细字段映射见 [`docs/spec/llm-provider-v0.2/streaming/`](../../spec/llm-provider-v0.2/streaming/README.md)。

### Raw LLM dump（debug）

`infrastructure/llm_providers/raw_dump.py::dump_raw_llm_interaction` 是 opt-in 调试钩子：

- 入口：`config.trace.raw_llm=True` 或 env `KONGMING_TRACE_RAW_LLM=1` 时启用
- 落盘：`.kongming/debug/raw-llm-<UTC>-<nonce>.json`，含完整 request / response / status / headers
- 安全：`Authorization` / `X-API-Key` / `Api-Key` header 写入前替换为 `<redacted>`
- 错误：任何异常静默 `return None`，永不污染主链路

### Provider usage 门户

`infrastructure/llm_providers/usage/provider_usage_manager.py::ProviderUsageManager`
是 token usage 归一化的唯一门户。公共不可变合同位于
`core/contracts/provider_usage.py`，包含 family、scope、completeness、字段来源、
异常证据与完整 `raw_usage`。

- Anthropic streaming 使用字段级 latest-present：`message_delta.usage` 只覆盖本次出现的字段。
- OpenAI Chat 与 Responses 使用各自正式字段路径；family 由 endpoint/wire adapter 显式选择。
- 缺失、非法或无法精确推导的指标保持 `None/unavailable`；provider 原报零保持 `0/provider_reported`。
- Runner 的 run 累计只对每个请求都已知的指标求和；FileSession、trace 和 usage event 保存同一 snapshot payload。

### 运行时装配层：SessionEngine

```
cli/main → SessionEngine.build(config, **kwargs) → SessionEngine → Runner.run(...)
```

`SessionEngine.build()` 按 `Config` 装一份默认依赖后再 `return cls(...)`：

1. **模型解析**：`ModelCatalogManager.resolve_runtime(config.model)` 生成 frozen `ResolvedModelConfig`，credential 在 provider 构造阶段独立解析
2. **provider 分派**：snapshot protocol 为 `anthropic` 时构造 `AnthropicMessagesProvider`，`openai` 时构造 `OpenAIResponsesProvider`
3. tools 缺省 → `_EmptyToolLookup`
4. approval 缺省 → `_AllowAllApproval()`（**只**作为安全链**底层** fallback）
5. DangerGuard + approval mode + thread permissions 三层 → `safety.build_safety_chain(...)` → `SafetyGatedApproval`
6. session_factory 缺省 → `lambda sid: InMemorySession(session_id=sid)`
7. agent_spec 缺省 → snapshot 的 remote model、preset ID 与默认 reasoning effort
8. compactor、InputAssembler、Runner 与 lifecycle hooks 按既有装配链构造
9. 每次 run 把 catalog source/preset/remote model 和 effective reasoning plan 写入 `llm.request.metadata` trace

**安全链始终启用**：底层 approval 只处理进入人工 Consent 的请求；普通静默放行由 `full_trust` 或当前 thread 的 allow 表达式产生，danger 始终进入人工审批。

### Session 历史任务级门户

`SessionEngine` 统一持有 Session factory 与进程内缓存，并向宿主公开四个异步任务级方法：

- `read_session_history(session_id)`：读取指定会话的结构化历史。
- `append_session_message(session_id, message, usage=None)`：追加单条消息及可选 usage 快照。
- `seed_empty_session_history(session_id, messages)`：只向空会话播种完整历史；目标已有消息时直接拒绝。播种中途失败会清空已写入前缀并重新抛出原始异常。
- `clear_session_history(session_id)`：为 fork 等跨资源事务提供补偿式清空入口。

Web 首条消息、history frame 与 thread fork 均通过这些门户操作历史。raw Session、factory 和缓存属于 `SessionEngine` 模块内部实现。

### agent workflow 编排（v0.1）

`AgentWorkflowManager` 位于 `src/application/agent_workflows/manager.py`，是编排 facade。它持有 `AgentWorkflowStrategyManager`，并通过父 `AgentManager` 派生所有 workflow child；缺少已 boot 的父 manager 或 parent identity 时在创建 workflow 目录前失败。对外提供 `list_workflow_strategies()`、`describe_workflow_strategy(mode)`、`run_workflow(...)`、`run_workflow_specs(...)`、`run_workflow_payload(...)`。

`src/application/agent_workflows/prompt_catalog.py` 提供 `WorkflowPromptCatalogManager` 和 `WorkflowPromptListingFormatter`。宿主装配 system prompt 时先从默认 strategy registry 读取全量 description，投影为只含 `mode`、`title`、`使用场景` 的短 listing，再通过 `assemble_instructions(workflow_catalog=...)` 注入到文件/env/skills/memory 之前。具体 payload 字段继续由 `describe_agent_workflow_strategy(mode=...)` 按需披露。

主链路：

```
AgentWorkflowTool / CLI smoke
  → AgentWorkflowManager.run_workflow_payload(mode, payload)
  → AgentWorkflowStrategyManager.run_strategy(...)
  → ParallelWorkflowStrategy / MapReduceStrategy
  → AgentManager.spawn(...) → TaskRegistry pending/running/terminal
  → Runner.run(...)
  → workflow audit + reports + AgentWorkflowResult
```

- `parallel`：当前可运行策略。策略层校验 payload 里 `task_specs` / `tasks`，再复用 `AgentWorkflowManager.run_parallel_specs(...)` 完成 fan-out / fan-in。
- `map_reduce`：当前可运行策略。`parse_map_reduce_workflow_spec(...)` 把 JSON payload 解析为 typed spec；`MapReduceStrategy` 调 planner 生成 shards，materializer 复制输入到 scoped workdir，mapper 子 agent 输出 `code_findings` JSON，validator 校验，reducer 确定性去重排序并写 `map_reduce/` 细节产物。
- 子 agent 权限：应用层先按父级实际工具、任务请求工具和 scope 工具求交，再由 `wrap_scoped_file_tools(...)` 把文件访问限定到 workflow 分配的 `work/`；最终审批固定进入当前 `SessionEngine` 的全局安全链。
- 运行产物：`<session.file_store_path>/<parent_session_id>/agent-workflows/<workflow_id>/workflow.json`、`audit.jsonl`、`result.json`、`reports/index.json`、`agents/<task_run_id>/subagent.json`。

### run 的多轮语义 & aclose 生命周期

- 同一 `session_id` 反复调用落到同一个 Session 实例（进程内缓存 `self._sessions: dict[str, Session]`）
- `session_id=None` 走匿名新 session，不缓存
- `aclose()` 委托底层 provider 的 `aclose`（释放 httpx 连接池）；幂等；由 CLI finally 触发

## 代码索引

| 文件 | 导出/内容 | 说明 |
|------|----------|------|
| `infrastructure/llm_providers/__init__.py` | `BaseLLMProvider` / `OpenAIResponsesProvider` | provider 侧公共入口（Anthropic 和流式组件需要从子模块直接 import） |
| `infrastructure/llm_providers/base.py` | `BaseLLMProvider` | provider 基类：重试 / 超时 / httpx client 生命周期 / OpenAI chat 格式转换 helper |
| `infrastructure/llm_providers/openai_responses.py` | `OpenAIResponsesProvider` / `FinishReasonStr` | OpenAI-compatible provider，走 `{base_url}/chat/completions`；api_key 为空时不带鉴权头；有 key 时按 `model.api_key_header` 写入；支持 `enable_raw_dump` |
| `infrastructure/llm_providers/anthropic_messages.py` | `AnthropicMessagesProvider` | 原生 Anthropic Messages API provider，走 `{base_url}/v1/messages`；system 消息提到请求顶层 `system` 字段；tool 调用走 `tool_use`/`tool_result` content block；headers 必带 `anthropic-version: 2023-06-01`；API key header 由 `model.api_key_header` 控制；v1 多模态路径消费 `media_adapter` + `AssetBytesReader` 协议，附件 ref → multi-block content 转换 |
| `infrastructure/config/api_key_headers.py` | `build_api_key_headers` / `api_key_header_label` | API key header 配置合同 helper；runtime provider 与 Web provider probe 复用同一规则，避免连接测试和真实请求漂移 |
| `infrastructure/llm_providers/reasoning.py` | `ReasoningConfig` / `ResolvedReasoningPlan` / `EffortLevel` / `resolve_reasoning_plan` | reasoning adapter：effort + catalog capability → payload patch；provider-agnostic |
| `infrastructure/llm_providers/sse_reader.py` | `iter_sse_events` | 共享 SSE 行缓冲 + JSON 解包，供 OpenAI / Anthropic 流 parser 复用 |
| `infrastructure/llm_providers/openai_compat_stream_parser.py` | `OpenAICompatStreamParser` / `_PartialCall` | OpenAI-compatible chat completions 流的 `LLMStreamChunk` 归一化 parser |
| `infrastructure/llm_providers/raw_dump.py` | `dump_raw_llm_interaction` | opt-in 原始 HTTP 交互落盘；env `KONGMING_TRACE_RAW_LLM=1` 或 `config.trace.raw_llm=True` 启用；secret header 自动 redact |
| `infrastructure/llm_providers/media_adapter.py` | `MediaAdapter` / `AnthropicMediaAdapter` / `OpenAIMediaAdapter` | provider 多模态 content block 适配层：消费 `core.contracts.MediaPart`，Anthropic 实现产出 `image` content block；OpenAI 为 stub（Phase 1） |
| `infrastructure/llm_providers/anthropic_stream_parser.py` | `AnthropicStreamParser` | Anthropic Messages API 流式 SSE 事件归一化为 `LLMStreamChunk` 序列；与 `OpenAICompatStreamParser` 对齐输出形态。`input_json_delta` 仅做字符串累积（避免 O(n^2) 解析），流结束一次性 `json.loads`；含流结束双守卫（无 `message_start` / 无 `stop_reason`）+ stall 检测（30s 间隔 warning） |
| `infrastructure/llm_providers/usage/provider_usage_manager.py` | `ProviderUsageManager` | provider usage 唯一归一化门户：按 family 聚合流式 raw fragment，生成 `ProviderUsageSnapshot`，保留未知值、来源证据、异常与完整 raw usage |
| `infrastructure/llm_providers/provider_factory.py` | `resolve_model_config` / `build_provider` | 统一工厂：消费 `ModelCatalogManager` 与 `ResolvedModelConfig`，按 snapshot protocol 分派 provider |
| `runtime_assembly/__init__.py` | `SessionEngine` | 运行时装配层公共入口 |
| `runtime_assembly/session_engine.py` | `SessionEngine` / `_AllowAllApproval` / `_EmptyToolLookup` / `_NoopCompactor` / `_NOOP_COMPACTOR` | 进程内装配层；绑定 catalog manager + immutable model snapshot，生成脱敏 trace metadata，再把依赖交给唯一 Runner |
| `application/agent_workflows/context.py` | `WorkflowExecutionContext` | 策略执行上下文：携带 workflow id、父 session、workflow 目录、审计 writer、超时字段 |
| `application/agent_workflows/manager.py` | `AgentWorkflowManager` / `AgentWorkflowResult` / `AgentWorkflowAuditWriter` / report dataclass | 编排 facade：注册默认策略、按 mode 分发 workflow、执行 parallel、提供通用 payload 入口、分配工作目录、写 `workflow.json` / `audit.jsonl` / reports / `result.json` |
| `application/agent_workflows/strategies/base.py` | `WorkflowRunRequest` / `WorkflowStrategy` / `WorkflowStrategyNotFound` / `WorkflowStrategyNotRunnable` | 策略运行协议：统一请求模型、异常模型和可执行策略接口 |
| `application/agent_workflows/strategies/description.py` | `WorkflowStrategyCatalogEntry` / `WorkflowStrategyInputField` / `WorkflowStrategyDescription` / `WorkflowStrategyStatus` | 面向父 agent / LLM 的中文策略目录、详细说明和输入字段模型 |
| `application/agent_workflows/strategies/manager.py` | `AgentWorkflowStrategyManager` / `WorkflowContextFactory` | 策略注册管理器：注册可运行策略和 planned 说明，提供 list / describe / run 分发 |
| `application/agent_workflows/strategies/parallel.py` | `ParallelWorkflowStrategy` | 可运行的并行策略：输出中文说明，校验 `task_specs` / `tasks`，委托 `AgentWorkflowManager.run_parallel_specs(...)` |
| `application/agent_workflows/strategies/map_reduce/` | `MapReduceStrategy` / `MapReducePlanner` / `MapperInputMaterializer` / `MapperPromptBuilder` / `MapReduceMapperOutputValidator` / `MapReduceReducer` / `MapReduceArtifactWriter` / contracts | 可运行的 `map_reduce` 策略包：分片、输入物化、mapper prompt、输出校验、确定性 reducer 和细节产物写入 |
| `application/agent_workflows/task_models.py` | `SubAgentTask` / `SubAgentRun` | workflow child 任务与终态结果值对象 |
| `application/agents/manager.py` / `registry.py` | `AgentManager` / `TaskRegistry` / `TaskProjection` | child spawn、独立 session、状态机与不可变宿主投影 |
| `application/subagents/permissions.py` | `SubAgentPermissionSpec` / `SubAgentGrant` / `SubAgentToolAuditHook` / `wrap_scoped_file_tools` | 子 agent scoped workdir 权限层：生成声明式 grant、包装 file tools、拒绝越界访问、写入 workflow 工具审计 |

## 配置

### `SessionEngine.build()` 参数表

| 参数 | 类型 | 默认 | 说明 |
|------|------|------|------|
| `config` | `Config` | 必填（位置参数） | 从 `infrastructure.config.load_config()` 拿到的整体配置 |
| `event_sinks` | `list[EventSink] \| None` | `None` → `[]` | 事件 sinks；host/cli 注入 `JsonlTraceSink` + 可选 `CLIEventSink` + 可选 `MemoryRefreshSink` |
| `approval` | `ApprovalProvider \| None` | `None` → `_AllowAllApproval()` | **底层** approval；始终包进 `SafetyGatedApproval` 后才交给 Runner |
| `tools` | `ToolLookup \| Mapping[str, Tool] \| None` | `None` → `_EmptyToolLookup()` | 工具查找面；dict 天然满足 |
| `enabled_tool_names` | `list[str] \| None` | `None` → `[]` | 启用工具白名单；用于默认 `AgentSpec.tool_names` |
| `session_factory` | `Callable[[str], Session] \| None` | `None` → `InMemorySession` | CLI 层可传 `build_session` 切 sqlite / file |
| `agent_spec` | `AgentSpec \| None` | `None` → 按 Config 合成最小 spec | 显式传时 `instructions` 参数被忽略 |
| `instructions` | `str \| None` | `None` → `"You are kongming agent."` | 系统指令文本；空白字符串回退默认；`agent_spec` 显式时忽略 |
| `permissions_manager` | `PermissionsManager \| None` | `None` → 按 Kongming home 构造 | thread permissions 唯一门户；Web 多 runtime 注入进程级共享实例。 |
| `message_compactor` | `MessageCompactor \| None` | `None` → 按 `cfg.compactor.enabled` 决定 `HistoryCompactor` 或 `_NOOP_COMPACTOR` | 每 turn 把 history 送 LLM 之前的加工钩子；**默认关闭** |
| `prompt_debug_sink` | `PromptDebugSink \| None` | `None` | CLI `--debug` flag 传入 `PromptDebugDumpSink()` |
| `instruction_origins` | `Sequence[str] \| None` | `None` | 真实 instruction 来源列表（仅给 prompt debug dump 用） |
除 `config` 外全部 **kw-only**（build 签名里 `*,` 之后）。

### Lifecycle hook 注册

`SessionEngine.build(...)` 返回 runtime 后，宿主在首个 run 前调用 `runtime.add_lifecycle_hook(hook)` 注册业务扩展。`runtime.remove_lifecycle_hook(hook)` 按对象身份移除。每次 `run()` / `continue_from_last_user_message()` 入口都会生成 tuple 快照，运行中的新增或移除只影响后续 run。

### 跨层依赖白名单

`.importlinter` Contract 3 `ignore_imports` 显式放行两条：

```
runtime_assembly.session_engine -> safety
runtime_assembly.session_engine -> prompting
```

其他任何 `runtime_assembly/*` 跨层 import 仍被 `layered-dependency-direction` 阻止。

## 已知问题 / 待完成

- **流式渲染已由事件 sink 接管**：`BaseLLMProvider` / parser / SSE reader 都就绪，runner 能通过 `SupportsLLMStream` 走流式路径；CLI 实时渲染入口是 `src/hosts/cli/stream_sink.py::CLIStreamSink`，`src/hosts/cli/repl.py::print_streaming_chunk` 保留为兼容占位。
- **5xx 重试策略未细化**：当前 HTTP `>=400` 全部落为 `ProviderError` 不走重试；若未来需要对 5xx 重试，覆盖 `_is_retryable` 即可。
- **compactor 默认关闭**：目前 FIFO 裁剪不符合预期，默认走 `_NOOP_COMPACTOR` 原样透传。LLM summarize 式压缩留给独立 task `compactor-v2-llm-summarize`。
- **`reasoning.py` / `anthropic_messages.py` 已覆盖在当前文件布局文档**：后续新增 provider 继续登记到 `infrastructure/llm_providers/`。
- **`map_reduce` live smoke 仍在后续任务**：当前仓库已有 runtime strategy、planner、input materializer、mapper fan-out、validator、reducer 和 artifact writer；MiniMax M3 真实模型 smoke 进入 `map-reduce-live-smoke-v0.1`。

## 参考

- [`docs/spec/kongming-agent-v1-minimal/10-contracts.md`](../../spec/kongming-agent-v1-minimal/10-contracts.md) — "Native Runtime 边界"
- [`docs/spec/kongming-agent-v1-minimal/11-v1-file-layout.md`](../../spec/kongming-agent-v1-minimal/11-v1-file-layout.md) — `infrastructure/llm_providers/` 与 `runtime_assembly/` 段
- [`docs/spec/llm-provider-v0.2/README.md`](../../spec/llm-provider-v0.2/README.md) — Anthropic 接入 / reasoning adapter / 流式设计
- [`docs/spec/llm-provider-v0.2/streaming/`](../../spec/llm-provider-v0.2/streaming/README.md) — 流式 V1 六份方案文档
- [`docs/agent-workflow/subagent-orchestration-v0.1/`](../../spec/agent-workflow/subagent-orchestration-v0.1/README.md) — 子 agent fan-out / fan-in 编排设计
- [`docs/agent-workflow/multi-strategy-orchestration-v0.1/`](../../spec/agent-workflow/multi-strategy-orchestration-v0.1/README.md) — 多策略注册与选择设计
- [`docs/agent-workflow/map-reduce-v0.1/`](../../spec/agent-workflow/map-reduce-v0.1/README.md) — `map_reduce` 编排规格设计
- [`docs/fixes/20260420-v1mini-doc-conformance/fix-report.md`](../../fixes/20260420-v1mini-doc-conformance/fix-report.md) — Finding 3a（instructions 接入）
- [`docs/fixes/20260420-v1mini-3b-history-compactor-wiring/fix-report.md`](../../fixes/20260420-v1mini-3b-history-compactor-wiring/fix-report.md) — Finding 3b（HistoryCompactor 接入 runner）
