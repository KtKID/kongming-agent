# src/hosts/shared/ - 宿主

宿主抽象与桥接层：把终端、HTTP、桌面壳等异构宿主收敛成 runtime 能稳定消费的调用面。

## 设计理念

| 决策 | 理由 |
|------|------|
| `HostAdapter` 用 concrete base class | v1-mini 在 host 侧保留实现骨架，跨模块协议真源只在 `core/contracts/`（`EventSink` / `ApprovalProvider` / `Session`）。 |
| adapter 只做 UI 翻译 | 读输入、写输出、拉审批属于宿主 UI 胶水；turn 推进、session 历史、事件 fan-out 属于 runner / runtime。 |
| 事件流走 EventSink fan-out | `core.runner` 持有 `list[EventSink]` 分发事件，具体宿主的 EventSink 作为并列 sink 注册。 |
| HostDispatcher 消费已装配对象 | `HostDispatcher` 消费已装配的 `SessionEngine` 和可选 result handler，负责 root 文本投递、send-now、Result future FIFO 和生命周期收口。 |
| root agent 生命周期集中在 `HostDispatcher` | `HostDispatcher` 持有 root `AgentManager`、root mailbox、Result future FIFO、普通排队、显式 send-now、中断和关闭流程。 |
| child 只传声明式运行数据 | `HostDispatcher` 从 `AgentCell` 读取模型参数、工具快照和 lifecycle hooks；root 与 child 都调用同一个 `SessionEngine.run()`，最终审批固定使用 runtime 装配实例。 |
| Agent 取消联动审批清理 | CLI 与 Web 装配时向 `HostDispatcher` 注入 `ApprovalManager.cancel_by_agent`，subtree cancel 同步清理 child pending consent。 |
| scheduled root 复用普通启动链 | `HostDispatcher` 接收可选 root run bridge、稳定 thread ID 和 `TaskRegistrationContext`；AgentManager 继续创建真实 asyncio Task 并登记 TaskRegistry。 |
| CLI 进程生命周期集中在 `CLIInteractiveLoop` | CLI 的读输入、投递即回、Ctrl-C、EOF drain 属于终端进程壳职责。 |
| 依赖方向 | `hosts/shared/` 可以 import `core / runtime_assembly / commands / memory`；具体宿主实现放在 `hosts/cli/`、`hosts/web/` 等子域。 |

## 核心概念

- **HostAdapter**：读输入、写输出、通知事件、请求审批、释放资源的统一基类。
- **HostDispatcher**：持有 runtime、fresh session、稳定 thread、可选 root run bridge 和注册上下文，统一 root/child 文本投递、send-now、Result future FIFO、中断、reset 和关闭生命周期。
- **CLIInteractiveLoop**：CLI 读输入、普通文本排队、send-now、slash command 分流、Ctrl-C 和 EOF drain 的进程壳。
- **ThreadManager**：Web thread 状态 owner，维护 pending input 队列、active run 观察 task、thread metadata 和 cell 生命周期。
- **具体宿主适配器**：`hosts/cli/adapter.py`、`hosts/web/app_support/host_adapter.py` 负责终端和 Web 的 UI 翻译。
- **MemoryRefreshSink**：独立 `EventSink`，订阅 `kind=history.compact` 后重载 memory 快照并向 downstream sinks emit `memory.snapshot.refreshed`。

## 核心流程

```
CLI 用户输入 ──► CLIAdapter.read_input
                 │
                 ▼
            CLIInteractiveLoop.send(text)
                 │
                 ├──► HostDispatcher.submit(text, QUEUE / IMMEDIATE)
                 │         │
                 │         └──► SessionEngine.run(text, session_id)   # 内部驱动 runner
                 │
                 └──► CommandService.handle_command(command)

Web user.input ─► ThreadManager.submit_user_input(text)
                 │
                 ▼
            pending input queue / active run gate
                 │
                 ▼
            HostDispatcher.run_text(text)

Scheduled reservation ─► ScheduledRunManager.submit_scheduled_run
                          └──► HostDispatcher.run_text
                               └──► AgentManager → TaskRegistry
                                    └──► ExecutionBridge → SessionEngine.run
```

CLI 交互循环由 `hosts.cli.interactive_loop.CLIInteractiveLoop` 承接：读输入 -> send 普通排队 / send_now 显式立即发送 -> EOF drain / Ctrl-C interrupt。审批命中时 runtime 的全局安全链进入 Consent 叶子；CLI 由 `ApprovalManager` 驱动终端确认，Web generic_chat 由同一 manager 驱动全局 inbox。child 请求沿用该 manager，并携带 `agent_id` 供取消清理。

## 代码索引

| 文件 | 导出/内容 | 说明 |
|------|-----------|------|
| `base.py` | `HostAdapter` | 所有宿主适配器的 concrete base。五个方法：`read_input` / `write_output` / `notify_event` / `prompt_approval` / `close`。 |
| `host_dispatcher.py` | `HostDispatcher` / `ScheduledRunHostDispatcher` / `build_scheduled_run_dispatcher_factory` | root agent 生命周期 owner：普通与 scheduled root 共用 AgentManager/TaskRegistry 启动、业务身份附着、Result FIFO、中断、drain 和关闭；scheduled adapter 实现 application dispatcher Protocol。 |
| `memory_refresh_sink.py` | `MemoryRefreshSink` | `EventSink` 实现，构造时持 `MemoryStore` + `downstream_sinks`；只对 `kind=history.compact` 触发 `load_from_disk()` 并向下游 emit `memory.snapshot.refreshed`。 |
| `mcp_runtime_registration.py` | `McpRuntimeRegistrationManager` / `McpRuntimeRegistrationResult` | 宿主共享的 MCP / Web Search 工具注册胶水。 |
| `__init__.py` | `HostAdapter` / `HostDispatcher` / `MemoryRefreshSink` / `McpRuntimeRegistrationManager` | 模块公共 API。 |

## 配置

`hosts/shared` 模块消费 `infrastructure.config.Config`（通过装配层转递），具体宿主的配置入口由 `hosts/cli`、`hosts/web` 负责。

## 已知问题 / 待完成

- **具体宿主分域维护**：CLI 适配在 `hosts/cli/`，Web 适配在 `hosts/web/`。新增宿主子类化 `HostAdapter` 后复用 `HostDispatcher`。
- **`HostDispatcher` runtime 测试隔离度**：当前测试通过 `SessionEngine` + stub_llm 覆盖，后续可考虑抽 `RuntimeProtocol` 提高测试隔离度。

## 参考

- [`10-contracts.md` · Host 与 CLI](../../spec/kongming-agent-v1-minimal/10-contracts.md) - 宿主边界
- [`10-contracts.md` · Observability / EventSink 边界](../../spec/kongming-agent-v1-minimal/10-contracts.md) - 事件 fan-out 边界
- [`11-v1-file-layout.md` · host/ 段](../../spec/kongming-agent-v1-minimal/11-v1-file-layout.md) - 文件级职责
- [`src/core/contracts/`](../../../src/core/contracts/) - `EventSink` / `ApprovalProvider` / `ApprovalRequest` / `Event` 真源
- [`src/hosts/cli/main.py`](../../../src/hosts/cli/main.py) - CLI 装配入口
