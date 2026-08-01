# src/core/ — 核心

宿主无关的 agent 运行语义层，同时是整个系统的**协议真源单点**和唯一 run loop 所在地。

## 设计理念

| 决策 | 理由 |
|------|------|
| 跨模块共享协议只定义在 `core/contracts/` | 防止 `Session` / `Tool` / `Approval` 等抽象在多个模块各自长出一份，最后整合时全部重写（见 10-contracts.md「单一代码真源」）。 |
| `core` 不允许 import 任何 sibling 模块 | 强制依赖方向单向朝 `core` 汇聚；由 `.importlinter` Contract 1 背书。所谓「core 决定语言，其他模块提供实现」。 |
| 只依赖 Python 标准库 | 保证可替换、可测试、启动成本最低。v1-mini 的 `core` 不引入任何第三方库（含 pydantic / httpx / openai 等）。 |
| 唯一 run loop 在 `core.runner.Runner` | `session_engine.py` / `session.py` / `run_state.py` 不允许再长第二套 turn 循环。装配由 executors 完成，跑由 runner 完成。 |
| 请求级 LLM 工具合同由 Runner 执行 | 调用方通过 `LLMToolCallContract` 显式启用；Runner 以当次 `LLMRequest.tools` 为允许集合，在 assistant append、usage、approval 和 Tool 执行前完成整响应校验。流式违规会立即关闭当前 provider 响应迭代器，纠错消息只进入同一 run 的下一次 LLM 请求。 |
| 核心数据结构保留各自的唯一真定义文件 | `Message` / `RunState` / `Result` / `AgentError` 不塞进 `contracts.py`，只要全项目没有第二份同名定义即可，分文件更容易演进。 |
| `Runner` 本身无状态 | 所有 per-run 状态收口在 `RunState`（dataclass、可序列化），方便未来接中断 / 恢复 / 持久化，而不用给 `Runner` 加锁。 |

## 核心概念

- **协议真源**：`Session` / `Tool` / `ToolLookup` / `ToolCallPreparer` / `PreparedToolCall` / `ToolExecutionScope` / `ApprovalProvider` / `LLMProvider` / `SupportsLLMStream` / `EventSink` / `MessageCompactor` / `PromptAssembler` / `PromptSource` / `PromptDebugSink` / `AssetBytesReader` / `MediaPart` 全部收口在 `core.contracts`，多数是 `runtime_checkable` 的 `Protocol`。
- **统一消息形态**：`Message` 四种 role（user / assistant / system / tool），tool 调用以 `ToolCall` 子结构挂在 assistant 上，tool 结果以 `role="tool"` + `tool_call_id` 关联。
- **审批前 preparation**：`Runner` 先构造 `ToolContext`，再调用一次 `ToolCallPreparer.prepare()` 冻结参数和执行 scope；普通 Tool 由 Runner 构造空 scope 的 `PreparedToolCall`。审批与执行各消费一份等值深拷贝，审批侧修改嵌套参数不会进入执行；preparation 失败直接生成结构化 tool result，并跳过审批和执行。
- **流式增量契约**：`LLMStreamChunk` 是 provider-agnostic 的流式事件；`StreamChunkKind` 六种（`reasoning.delta` / `content.delta` / `tool_call.start` / `tool_call.arguments.delta` / `tool_call.end` / `message.done`）。一次完整流必须以且仅以一个 `message.done` 结束，runner 把它当等价 `LLMResponse` 消费。
- **流式能力正交**：`SupportsLLMStream` 是独立 Protocol，与 `LLMProvider` 正交；runner 用 `isinstance(llm, SupportsLLMStream)` 做能力探测，不靠 `NotImplementedError` 控制流。
- **Prompt 装配钩子**：`PromptAssembler` / `PromptSource` / `AssembledInput` 定义 runner 向 prompting 层 `InputAssembler` 请求 prompt build 的契约；`PromptDebugSink.dump(...)` 写出 `.kongming/debug/prompt-debug-*.json` 供调试。
- **运行态 vs 静态规格**：`AgentSpec` 描述「这个 agent 是什么」（含 `reasoning_effort`）；`RunState` 描述「这次 run 走到哪了」；两者严格分离。
- **统一事件面**：runner 持有 `list[EventSink]`，关键节点 fan-out `Event`（含 `run_id` 字段）给所有注册 sink；`EventSink` 是唯一的事件协议。`EventKind` 已覆盖：`run.*`（含 `run.cancelled`，v0.1 interrupt-run-v0.1）/ `turn.*` / `llm.request` / `llm.response` / `tool.call.*` / `approval.*` / `error` / `content.delta` / `reasoning.delta` / `llm.chunk.first` / `llm.stream.end` / `memory.write.{success,rejected,error}` / `memory.snapshot.refreshed` / `tool.{denied,approval_required,silently_allowed}` / `skill.{discovered,shadowed,parse_failed,skipped}`（v0.1.6 装配期）/ `skill.{invoked,completed,failed}`（v0.1.6 运行时）/ `usage`（每轮 token 用量）。
- **Lifecycle hook 收口**：`LifecycleHook.after_run` 在 `Result` 构造后、`run.end` 发出前由 `Runner` 调用；hook 异常转成 `error` 事件，保持 `Result` 稳定，`run.end` 继续作为单次 run 的最后主事件。`LIFECYCLE_HOOK_POINTS` 集中列出每个 hook 的名称、方法、触发时机和入参摘要。
- **interrupt 收口契约（interrupt-run-v0.1）**：runner 顶层 `except asyncio.CancelledError` 吞掉 cancel，统一收口到 `Result(status="cancelled")`，**不向外 raise** —— 上游（`HostDispatcher` / `CommandService` / Web `ws.py`）只看 Result，不需要自己 `except CancelledError`。被打断时 `_execute_tool_calls` 给"正在跑的 call" + "同 assistant 未起跑的 call" 全部写 `[interrupted by user]` 占位 `tool_result`（`is_error=True` + `interrupted=True` metadata），保证 Anthropic / OpenAI 要求的 `tool_use ↔ tool_result` 配对完整，下一轮 LLM 调用不会被服务端 400 拒掉。`Result.metadata` 含 `cancelled_at_turn` / `cancelled_tool_call_id` / `cancel_reason`。
- **错误分层**：`AgentError` 是唯一基类，按来源分 `ProviderError` / `ToolError` / `ToolPreparationError` / `ApprovalRejected` / `CapabilityDenied` / `PermissionDenied` / `MaxTurnsExceededError` / `ConfigError`。

## 代码索引

| 文件 | 导出/内容 | 说明 |
|------|-----------|------|
| `__init__.py` | 重新导出上述所有公共符号 | 包门面，给下游一个稳定的 `from core import X` 入口。 |
| `contracts/tool_runtime.py` / `contracts/approval.py` | `Tool` / `ToolLookup` / `ToolContext` / `ToolCallPreparer` / `PreparedToolCall` / `ToolExecutionScope` / `ToolResult` / `ApprovalProvider` / `ApprovalRequest` / `ApprovalDecision` | **工具与审批跨模块协议真源**。`Tool.execute` 只接收 `PreparedToolCall`；preparer 负责审批前完成校验、默认填充和语义归一化，Runner 负责单次 prepare、隔离副本和调用时序；`ApprovalRequest.execution_scope` 保存准备阶段冻结的执行边界。 |
| `contracts/llm_provider.py` | `LLMRequest` / `LLMResponse` / `LLMToolCallContract` / `LLMToolCallContractMode` / `LLMToolCallViolationKind` | LLM 请求、响应与 run 级工具调用合同真源。`DECLARED_EXACTLY_ONCE` 要求被接受的 assistant 响应恰好调用一次当次请求声明的工具。 |
| `contracts/media.py` | `AttachmentKind` / `AttachmentStatus` / `IMAGE_EXT_BY_MIME` / `AssetBytesReader` / `MediaPart` / `ImageMediaPart` / `build_media_part_from_metadata` / `collect_media_parts_from_messages` | 多模态媒体协议真源。provider 只消费 `MediaPart` 与 `AssetBytesReader`，Web 上传存储作为宿主实现注入。 |
| `runner.py` | `Runner` | 唯一 run loop。负责 turn 推进、请求级工具合同校验与瞬态纠错、审批前 tool preparation、tool_call 回填、停止条件、lifecycle hook 调度、事件 fan-out、异常包装；合同拒绝以整条 assistant 响应为原子单位，prepared arguments 会深拷贝到审批和执行出口。 |
| `run_state.py` | `RunState` / `RunStatus` | 可序列化运行态对象，承载 status / turn / messages / last_error 等。 |
| `message.py` | `Message` / `ToolCall` / `MessageRole` | 系统内部统一消息结构。frozen dataclass。`metadata` 字段支持 `attachments` key（v1 新增），存储 `UserInputAttachment` 列表引用（仅 user 消息有意义）。 |
| `result.py` | `Result` / `ResultStatus` | 一次 `Runner.run()` 的收口对象，host / cli 只读这个不读 `RunState`。 |
| `errors.py` | `AgentError` / `ProviderError` / `LLMToolCallContractError` / `ToolError` / `ToolPreparationError` / `ApprovalRejected` / `CapabilityDenied` / `PermissionDenied` / `MaxTurnsExceededError` / `ConfigError` | 核心错误模型；`LLMToolCallContractError` 表示纠错预算耗尽后的 LLM 响应协议失败，`ToolPreparationError.details.code` 承载审批前准备失败分类。 |
| `agent_spec.py` | `AgentSpec` | Agent 静态规格：`name` / `instructions` / `default_model` / `tool_names` / `max_turns` / `metadata` / `reasoning_effort`。 |
| `lifecycle.py` | `LifecycleHook` / `LifecycleHookBase` / `LifecycleHookPointSpec` / `LIFECYCLE_HOOK_POINTS` | core 内部可选的 before/after turn、before/after tool、after_run 钩子；保留在 runner 内部扩展面，由 `LIFECYCLE_HOOK_POINTS` 明确触发时机，`LifecycleHookBase` 给业务 hook 提供默认空实现。 |
| `session.py` | `InMemorySession` | 第一批默认 session 实现：纯内存、单协程、无锁。实现 `Session` Protocol 含 `advance_run_index()`（内存后端只更新实例字段）。持久化留给 `sessions/session_store.py`（SQLite / File）。 |

## 配置

`core` 本身不读任何配置文件或环境变量。所有运行参数通过 `AgentSpec` 和 `Runner.run(...)` 的参数传入；配置解析在 `infrastructure.config`，装配发生在 `runtime_assembly/session_engine.py`。

## 已知问题 / 待完成

- **`MessageCompactor` 接入历史**：早期版本 runner 直接透传 `session.history()` → `LLMRequest.messages`，未经过加工，长对话会撞模型 context 上限。已在 `docs/fixes/20260420-v1mini-3b-history-compactor-wiring/fix-report.md` 中修复，`Runner.__init__` 现接收可选 `message_compactor`。
- **`LifecycleHook` 覆盖度**：v1-mini 当前覆盖 turn / tool / run terminal 机制点；`after_run` 已覆盖 completed / failed / cancelled 终态和异常事件路径。
- **`RunState.messages` 与 `Session.history`**：前者是运行快照、后者是可持久化事实源，两者边界在 docstring 里已说明；未来接 SQLite session 时要再确认不出现双写冲突。

## 参考

- 项目定位与范围：[`docs/spec/kongming-agent-v1-minimal/README.md`](../../spec/kongming-agent-v1-minimal/README.md)
- 协议边界与依赖方向：[`docs/spec/kongming-agent-v1-minimal/10-contracts.md`](../../spec/kongming-agent-v1-minimal/10-contracts.md)
  - 「单一代码真源」「依赖方向」「Session 边界」「Approval 边界」「Observability / EventSink 边界」「Native Runtime 边界」几节均直接约束本模块。
- 文件级职责：[`docs/spec/kongming-agent-v1-minimal/11-v1-file-layout.md`](../../spec/kongming-agent-v1-minimal/11-v1-file-layout.md) 的 `core/` 小节。
- 架构边界 CI：[`.importlinter`](../../../.importlinter) Contract 1 `core-no-sibling-imports` 强制 `core` 不反向依赖任意 sibling。
