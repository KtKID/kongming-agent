# src/hosts/cli/ — 命令行

kongming-agent 的第一个真实宿主产品入口：`python -m hosts.cli.main`。负责解析参数、装配五条通道（session / trace / instructions / memory / approval）、创建 `HostDispatcher` / `AgentTreeRuntimeRouter` / `CommandService` / `CLIInteractiveLoop`，最后把终端交互生命周期交给 `CLIInteractiveLoop`。

## 设计理念

| 决策 | 理由 |
|------|------|
| CLI 负责 **解析 + 装配 + 进程生命周期接线** | 对齐 `10-contracts.md` 的 "runner 负责跑，session_engine 负责装，cli/main 负责进"。runner / provider / safety 装配统一走 `SessionEngine.build`。|
| 不写死 model / api_key / max_turns / timeout | 全部从 `infrastructure.config.Config` 读；CLI 只把 Config 转发给 `SessionEngine.build`，不做第二份默认值 |
| click 同步入口 + `asyncio.run` 驱动 async 主链路 | click 不支持 native async，保持最简解。顶层 Ctrl-C 捕获后 `SystemExit(130)` 干净退出，不打 traceback。|
| `hosts/cli/__init__.py` 故意不 re-export `main` | 若 `from hosts.cli.main import main` 出现在 `hosts/cli/__init__.py`，`python -m hosts.cli.main` 会触发 "found in sys.modules after import of package 'cli'" 的 RuntimeWarning。详见 [`docs/fixes/20260420-cli-smoke-runtime-warning/fix-report.md`](../../fixes/20260420-cli-smoke-runtime-warning/fix-report.md)。 |
| `CLIInteractiveLoop` 持有终端交互生命周期 | `interactive_loop.py` 负责读输入、投递即回、普通排队、显式 `send_now`、命令 task、Ctrl-C interrupt 和 EOF drain；普通文本统一调 `HostDispatcher.submit(text, mode)`，slash command 调 `CommandService.handle_command`。 |
| `repl.py` 刻意保持薄 | `repl.py` 只放跨 CLI 子命令可能复用的小 helper（`AsyncPrompt` / `confirm_yn`）。 |
| interactive 审批统一走状态与交互门户 | CLI 和 Web 共用 `PermissionsManager`、`SafetyDecisionEngine` 与 `ApprovalManager`；CLI 只新增 `CLIApprovalEventSink`。 |

## CLI 装配的五条通道（instructions 通道内含 prompts 三段）

CLI 作为**分层最顶端**，合法 import 下层任何模块，所以这些接线直接写在 `_run()` 里：

1. **session_factory**：先构造 `SessionBootstrap(agent_name, model_name, instruction_sources, instruction_text_hash, created_at, cwd, app_version)`，再包成 `lambda sid: build_session(cfg, sid, bootstrap=bootstrap)` — 让 `config.session.backend` 生效。memory 走 `InMemorySession`，sqlite 走 `SQLiteSession` 可跨进程恢复，file 走 `FileSession`（append-only JSONL + manifest + message_id chain）。
2. **event_sinks**：默认挂 `JsonlTraceSink(cfg.trace.output_path, auto_flush=cfg.trace.auto_flush)`；`--no-trace` 可关；`--verbose` 或 `--show-reasoning` 追加 `CLIEventSink(verbose=..., show_reasoning=...)`；memory 启用时再尾随 `MemoryRefreshSink`（downstream 指向此时的全部 sinks）。runner 对 sink 顺序不敏感，fan-out 广播。
3. **instructions**：`_assemble_instructions` 准备 workflow listing、skill listing 和已加载的 MemoryStore，然后调用 `prompting.assemble_instructions(...)` 统一生成 system prompt。公共装配器会物化 `<home>/prompts/{AGENT,TOOLS,USER}.md`，读取 `--instructions-file`、`KONGMING_EXTRA_INSTRUCTIONS`、可选 sitian、skills、memory，渲染顺序为 `runtime -> workflow_catalog -> agent_spec/prompts -> files -> env -> sitian -> skills -> memory`。最终文本注入 `SessionEngine.build(instructions=...)`，`instruction_origins` 传给 prompt debug sink 便于归因。
4. **memory**：`cfg.evolution.memory.enabled=True` 时由 `_assemble_instructions` 返回 `MemoryStore`；CLI 随后 `registry.register(build_memory_tool(...))`、emit `memory.snapshot.captured` 事件、装 `MemoryRefreshSink(memory_store, downstream_sinks=event_sinks)` 进 event_sinks。enabled=False 时完全跳过。
5. **approval**：CLI 构造共享 `PermissionsManager + ApprovalManager + CLIApprovalEventSink`，以稳定 session id 作为无 Web thread 时的本子键；`SessionEngine` 复用同一 PermissionsManager。旧 cwd 倒计时 policy 注入已断开并保留 `TODO(auto-mode)` 插槽。

进化能力作为 Tool + lifecycle 接入：CLI 创建单一 `EvolutionManager`，调用 `register_runtime_tools()` 注册 `request_evolution_review` 与 `evolution_write`，再用 `enabled_tool_names(..., lifecycle_bound=True)` 只向主 Agent 暴露公开 Tool。`SessionEngine.build()` 后注册 evolution lifecycle，确保公开 Tool 在当前 run 内登记的请求由对应 after-run 消费。MCP 注册保留 Manager 提供的私有 Tool 名，避免同名外部工具覆盖 reviewer 边界。

## 核心流程

当前普通文本发送、排队、插队和接收侧下行见 [`message-flow-sequence.md`](message-flow-sequence.md)。

```
click 解析 ─► [--workdir? os.chdir] ─► load_config ─► [override] --reasoning-effort ─► CLIAdapter(verbose)
                                │
                                ▼
                   event_sinks  = [JsonlTraceSink?, CLIEventSink?]
                   registry     = build_default_registry(file, shell, ...)
                   approval     = build_default_approval(mode, prompt_fn=?)
                                  └─ interactive: prompt_fn = ApprovalManager + CLIApprovalEventSink
                                                   + PermissionsManager shared instance
                   ├─ materialize_and_load_prompts(get_kongming_home())    ← v0.1.3: .kongming/prompts/{AGENT,TOOLS,USER}.md 物化 + 装配
                   instructions, origins, memory_store
                                = await _assemble_instructions(cfg, files)
                   if memory_store:
                       registry.register(build_memory_tool(memory_store, ...))
                       emit memory.snapshot.captured
                       event_sinks.append(MemoryRefreshSink(memory_store, event_sinks))
                   evolution_manager = EvolutionManager(cfg, kongming_home)
                   evolution_manager.register_runtime_tools(registry, event_sinks)
                   enabled_tool_names = evolution_manager.enabled_tool_names(
                       registry.names(), lifecycle_bound=True
                   )
                   bootstrap    = SessionBootstrap(agent/model/instr_hash/...)
                   session_factory = λ sid: build_session(cfg, sid, bootstrap=bootstrap)
                                │
                                ▼
                   SessionEngine.build(cfg, event_sinks, approval, tools, enabled_tool_names,
                                       session_factory, instructions,
                                       prompt_debug_sink=PromptDebugDumpSink() if --debug else None,
                                       instruction_origins=origins)
                   register_evolution_lifecycle_hook(runtime, evolution_manager)
                                │
                                ├─ --smoke ─► runtime.run("hello...")  → 打印 reply → 退出
                                │
                                └─► HostDispatcher(runtime, sid, queued_result_handler=adapter.render_result,
                                                   agent_tree_runtime_router=AgentTreeRuntimeRouter)
                                      ├─ CommandService(runtime_delegate=host_dispatcher.run_text)
                                      ├─ AgentTreeRuntimeRouter.bind_dispatcher(host_dispatcher, sid)
                                      └─ CLIInteractiveLoop(host_dispatcher, command_service, adapter).run_loop()
                                │
                            finally: await runtime.aclose()   # 释放 provider httpx client
```

## 代码索引

| 文件 | 导出/内容 | 说明 |
|------|-----------|------|
| `main.py` | `main` / `_run` / `_assemble_instructions` / `_build_cli_manager_prompt_fn` / `_resolve_config_path` | 进程入口。构造共享 Permissions/Approval Manager；通过 `EvolutionManager` 注册两级进化 Tool、过滤主 Agent enabled names 并安装 after-run lifecycle；interactive 增加终端交互 sink。 |
| `interactive_loop.py` | `CLIInteractiveLoop` / `SendReceipt` | CLI 交互生命周期 owner：读输入、`send` 普通排队、`send_now` 显式立即发送、命令 task、Ctrl-C interrupt、EOF drain。 |
| `repl.py` | `AsyncPrompt` / `confirm_yn` / `print_streaming_chunk` (占位) | 薄 helper。`AsyncPrompt` 是 `PromptSession` 的封装供未来子命令复用；`confirm_yn` 是纯 stdin y/N 确认，EOF / Ctrl-C 等同 `False`。流式输出目前为空实现。 |
| `approval.py` | `build_cli_action_prompt` / `_print_request_summary` / `_format_arguments` | CLI action-aware prompt；普通请求展示允许一次/允许并记住/拒绝一次/拒绝并记住，danger 只允许显式一次性决定。历史 deadline 解析仅保留兼容能力，v0.6 运行链不生成倒计时字段。 |
| `approval_manager_sink.py` | `CLIApprovalEventSink` | 只处理 `channel="cli"` 的 pending：投影请求、调用 action prompt，再把结果映射为 `allow/remember`。投影或 prompt 异常时失败关闭。 |
| `cron_delivery.py` | `CliDeliverySink` | v0.3 cron 投递 CLI sink。cron 触发完成后消息排队到 `deque(maxlen=100)` buffer，不打断用户当前 prompt 输入；REPL 主循环在下一次 prompt 前调 `drain_pending` 一次性 flush 到 stdout。`asyncio.Lock` 保护并发读写；每条格式 `\n[cron:<task.name>] <final_message>\n`。 |
| `__init__.py` | `__all__ = []`（刻意空） | 不 re-export `main`，避免 `python -m hosts.cli.main` 的 runpy warning。 |

## 配置

### click 选项

| 选项 | 类型 | 默认 | 作用 |
|------|------|------|------|
| `--config / -c` | Path | `KONGMING_CONFIG` env 或 `config/setting.yaml` | 配置文件路径 |
| `--session-id` | str | `cli-<hex12>` 随机 | 复用会话 ID（配合 `session.backend=sqlite`/`file` 跨进程恢复） |
| `--verbose / -v` | flag | False | 追加 `CLIEventSink(verbose=True)` 到 event_sinks，事件写 stderr |
| `--smoke` | flag | False | 不进交互，跑一次 hello run 验证 provider 可达；失败 `exit 1` |
| `--instructions-file` | Path (可重复) | `()` | 通过 `InstructionLoader.extra_files` 注入 markdown / 文本规则 |
| `--no-trace` | flag | False | 不挂 `JsonlTraceSink`（默认挂，写到 `cfg.trace.output_path`） |
| `--model-preset` | str | None | 按合并 catalog 的全局 preset ID 覆盖本次 CLI 模型选择 |
| `--reasoning-effort` | Choice[none/low/medium/high/max] | None | 覆盖 `model.reasoning_effort`；Manager 在 provider I/O 前验证 capability |
| `--show-reasoning` | flag (3-state) | None | 覆盖 `cli.show_reasoning`；未传时沿用 config；开启时追加 `CLIEventSink(show_reasoning=True)` 打印 reasoning_content |
| `--debug` | flag | False | 构造 `PromptDebugDumpSink()` 传给 `SessionEngine.build(prompt_debug_sink=...)`，每轮 prompt 写 `.kongming/debug/prompt-debug-*.json` |
| `--workdir / -C` | Path | None | 启动早期 `os.chdir(<path>)` 切到指定目录；之后 config 文件相对路径、工具相对路径、`run_shell` 子进程 cwd 和 thread 默认工作区都基于新目录。`kongming_home` 由 `get_kongming_home()` 决定，默认 `Path.home() / ".kongming"`。等同于先 `cd <path>` 再启动 CLI。click 自带校验（`exists=True, file_okay=False, dir_okay=True, readable=True`），不存在 / 不是目录立即非零退出。|
| `-h / --help` | — | — | click 帮助 |

### 环境变量影响的行为

| 变量 | 谁读 | 效果 |
|------|------|------|
| `KONGMING_CONFIG` | `infrastructure.config.load_config` | 指定 yaml 路径 |
| `KONGMING_HOME` | `infrastructure.config.paths.get_kongming_home` | 显式覆盖 `kongming_home`（绝对/相对/`~`）；默认 `Path.home() / ".kongming"` |
| `KONGMING_MODEL_PRESET_ID` / `KONGMING_MODEL_REASONING_EFFORT` | `infrastructure.config` + `ModelCatalogManager` | 默认 preset 与 reasoning 深度覆盖 |
| catalog 声明的 provider-specific key（如 `GLM_API_KEY`） | `ModelCatalogManager` | provider 构造阶段解析 credential；值不会进入 snapshot 或 trace |
| `KONGMING_SESSION_BACKEND` / `KONGMING_SESSION_STORE_PATH` / `KONGMING_SESSION_FILE_STORE_PATH` | `infrastructure.config` → `build_session` | `memory` / `sqlite` / `file` |
| `KONGMING_APPROVAL_MODE` | `infrastructure.config` → `build_default_approval` | `interactive` / `auto_allow` / `auto_deny` |
| `KONGMING_TOOL_FILE_*` / `KONGMING_TOOL_SHELL_*` | `infrastructure.config` → `build_default_registry` | 工具开关与运行参数 |
| `KONGMING_EVOLUTION_MEMORY_*` | `infrastructure.config` → `_assemble_instructions` | memory 启用 / 路径 / 注入 prompt / 读取上限 |
| `KONGMING_COMPACTOR_*` / `KONGMING_RETRY_*` / `KONGMING_TRACE_RAW_LLM` | `infrastructure.config` | compactor / 重试 / raw LLM dump |
| `KONGMING_EXTRA_INSTRUCTIONS` | `InstructionLoader(include_env=True)` | 追加一段 system 指令 |

完整列表见 `docs/modules/配置加载/README.md`。项目根 `.env` 文件由 `load_config` 自动加载（真 env 优先）。

退出码：`0` 正常；`1` smoke 失败；`2` 配置加载/校验失败；`130` 顶层 Ctrl-C。

## 已知问题 / 待完成

- **流式输出 CLI 端未接入**：runner 已具备 `SupportsLLMStream` 消费能力，但 `repl.print_streaming_chunk` / `CLIAdapter.write_output` 仍整段打印；实时 render 待后续接入。
- **CLI 自动处置只覆盖 default ask**：per-cwd mode 可以为 `default:ask` 创建自动同意倒计时；builtin/destructive/rule-error ask 维持人工确认或失败关闭。
- **`runtime._llm = stub_llm` 私有属性 mock**：CLI 集成 e2e 借用 `SessionEngine` 内部属性，更优雅方案需要 `SessionEngine.for_testing()` classmethod。
- **MemoryRefreshSink 动态 import**：CLI 按能力探测方式 `from hosts.shared.memory_refresh_sink import MemoryRefreshSink`；host 模块已固化该类，当前 try/except 仅是保险。
- **`InstructionLoader` 读不存在文件静默跳过**：v1-mini 刻意设计；未来加告警需要 observability 事件钩子。
- **`hosts/cli/__init__.py` 的空 `__all__` 是刻意的**：不要"看起来更整洁"就去 `from hosts.cli.main import main`；会重新引入 runpy warning。

## 参考

- [`10-contracts.md` · Native Runtime 边界](../../spec/kongming-agent-v1-minimal/10-contracts.md) — "runner 跑 / session_engine 装 / cli/main 进"
- [`11-v1-file-layout.md` · cli/ 段](../../spec/kongming-agent-v1-minimal/11-v1-file-layout.md)
- [`docs/fixes/20260420-v1mini-doc-conformance/fix-report.md`](../../fixes/20260420-v1mini-doc-conformance/fix-report.md) — session_factory / JsonlTraceSink / InstructionLoader 三条通道的装配修复
- [`docs/fixes/20260420-cli-smoke-runtime-warning/fix-report.md`](../../fixes/20260420-cli-smoke-runtime-warning/fix-report.md) — 为什么 `hosts/cli/__init__.py` 不 re-export `main`
- [`message-flow-sequence.md`](message-flow-sequence.md) — CLI 普通发送、排队、插队和接收侧完整时序
- [`README.md` · 如何使用](../../../README.md) — 用户视角的使用场景对照
- [`src/hosts/shared/`](../宿主/README.md) — CLI 消费的 adapter / dispatcher 层
