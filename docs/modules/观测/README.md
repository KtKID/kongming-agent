# src/infrastructure/tracing/ — 观测层

观测基线：把 runner fan-out 的所有事件追加到 JSONL trace（唯一事实源），并在 `--debug` 时把每轮 prompt build 快照单独写成 JSON。作为 v0.2+ usage / audit 派生能力的根。

## 设计理念

| 决策 | 理由 |
|------|------|
| `EventSink` / `PromptDebugSink` Protocol 真源只在 `core.contracts`，本模块**只实现、不重定义** | 单 Protocol + fan-out 多 sink 模式。禁止在 `infrastructure/tracing/` 或 `safety/` 里新建 `TraceSink` / `UsageSink` / `AuditSink` Protocol |
| 全部事件 kind（`run.*` / `turn.*` / `llm.*` / `tool.call.*` / `approval.*` / `history.compact` / `error` / `content.delta` / `reasoning.delta` / `llm.chunk.first` / `llm.stream.end` / `memory.write.*` / `memory.snapshot.refreshed` / …）进同一份 JSONL | v1 只做一个 sink。usage / audit 等分析靠从同一份 JSONL 派生，不通过新协议拆流 |
| fan-out 是 `core.runner.Runner` 持有 `list[EventSink]` 的职责 | `JsonlTraceSink` 不再分发给别的 sink；它是叶子实现类 |
| PromptDebug 走独立 Protocol，不挤进 EventSink | `PromptDebugSink.dump(...)` 是同步返回路径字符串的一次性写入；与 async 事件流语义不同，硬塞 EventSink 反而纠缠。runner 按 `prompt_debug_sink is not None` 能力探测 |
| 每次事件 `emit` 独立 `open / append / flush / close` | 不长期持有 FD，崩溃时最多丢**正在写的那一行**，代价换崩溃安全。v0.2+ 可切换到"长期持有 + 批量 flush"，接口预留了 `close()` 方法 |
| `asyncio.Lock` 串行化文件写入；`asyncio.to_thread` 把同步写 I/O 送到线程池 | 防多协程交错写出半行 JSON，同时不阻塞事件循环 |
| `_json_default` 三档兜底（dataclass / Path / datetime / set / bytes / 最终 `repr(o)`） | `emit` 永不因 payload 里冒出奇怪对象抛 `TypeError`；观测层失败不能污染主链 |
| sink 内部不吞异常 | runner 的 fan-out 已经做了"sink 异常不污染主链路"的兜底，这里保持透明便于排查 |

## 核心流程

一次事件 emit 的完整路径：

1. `core.runner.Runner._emit(event)` 遍历其 `list[EventSink]`，调用每个 sink 的 `async emit(event)`，异常被 runner 吞并降级（不影响主链）。
2. `JsonlTraceSink.emit` 走 `_ensure_init`（懒建父目录 + 空文件，双检锁）→ `_serialize(event)`（`dataclasses.asdict` 优先，降级到 `vars(event)` 或已知字段）→ 拿 `self._lock` → `asyncio.to_thread(self._append_line_sync, line)`。
3. `_append_line_sync` 以 `"a"` 模式打开 `output_path`、写入一行 JSON + `\n`、`auto_flush=True` 时立即 flush、退出 `with` 关闭文件。
4. 结果：`cfg.trace.output_path`（默认 `.kongming/trace.jsonl`）每行一条合法 JSON，可用 `tail -f` 实时观察，也可用 `jq` / pandas 事后分析。

`build_jsonl_trace_sink(config)` 只读 `config.trace.output_path`；注册到 runner 由装配层（`SessionEngine.build` / `hosts/cli/main.py`）负责。

## 代码索引

| 文件 | 导出/内容 | 说明 |
|------|----------|------|
| `__init__.py` | `JsonlTraceSink` / `PromptDebugDumpSink` / `build_jsonl_trace_sink` | 包门面。docstring 写死边界：不在本包重定义 `EventSink` / `Event` / `PromptDebugSink` 任何 Protocol；v0.2+ `UsageSink` / `AuditSink` 是并列兄弟实现，不替换本类 |
| `trace_sink.py` | `JsonlTraceSink` 实现类、`build_jsonl_trace_sink(config)` 工厂、`_event_to_dict` / `_json_default` 序列化辅助 | 零外部依赖（stdlib only）。通过鸭子类型满足 `core.contracts.EventSink`，无显式继承。提供占位 `async close()` 方法给未来"长期持有 FD"模式用 |
| `prompt_debug_dump.py` | `PromptDebugDumpSink` | `PromptDebugSink` 实现：`dump(session_id, run_id, turn, model, instruction_origins, history_before_assemble, assembled_messages, metadata, added_system_prompt)` → 写 `.kongming/debug/prompt-debug-<sid>-<rid>-turn-<n>-<ts>.json`。由 CLI `--debug` flag 启用 |
| `src/devtools/full_logger.py` | `FullLogger` / `init_full_logger` / `get_full_logger` | 开发期前后端通信全量日志。写 `{ts, dir, channel, thread_id, client_id, payload}` JSONL，走后台 writer task、队列限流和 `.kongming/*` 路径解析 |

## 配置

| 配置项 | 默认值 | 谁消费 |
|--------|--------|--------|
| `trace.output_path` | `".kongming/trace.jsonl"` | `build_jsonl_trace_sink` 构造 sink 的落盘路径；父目录首次 emit 时按需创建 |
| `trace.auto_flush` | `true` | CLI 传给 `JsonlTraceSink(..., auto_flush=...)`；true 牺牲少量吞吐换崩溃安全 |
| `trace.raw_llm` | `false` | `SessionEngine.build` 传给 `OpenAIResponsesProvider(enable_raw_dump=...)`；落盘到 `.kongming/debug/raw-llm-*.json`（headers 自动 redact） |
| CLI `--no-trace` flag | trace 默认**开启** | `hosts/cli/main.py` 根据此 flag 决定是否 append `JsonlTraceSink` 到 `event_sinks` |
| CLI `--debug` flag | 默认关 | 开启时构造 `PromptDebugDumpSink()` 传给 `SessionEngine.build(prompt_debug_sink=...)` |
| env `KONGMING_TRACE_RAW_LLM=1` | 覆盖 `trace.raw_llm` | `infrastructure/llm_providers/raw_dump.py` 运行时检查 |

参考：`src/infrastructure/config/models.py` 的 `TraceConfig`、`src/hosts/cli/main.py` 的 event_sinks 装配段。

## 已知问题 / 待完成

- **v0.2+ 规划**：追加 `UsageSink`（从 JSONL 派生 token / cost 聚合）和 `AuditSink`（派生 approval / guardrail 审计），都是 `EventSink` 的并列实现类，**不替换** `JsonlTraceSink`，也**不新增事件协议**。参考 `docs/spec/kongming-agent-v1-minimal/10-contracts.md` "Observability / EventSink 边界"。
- **不做 JSONL 轮转 / 压缩 / 尺寸上限**：v1-mini 阶段默认单文件无限追加。长期运行可能把 `.kongming/trace.jsonl` 撑到 GB 级；生产场景需要由外部 logrotate 或定期归档脚本处理。
- **每次 emit 重新 open/close 文件**有 syscall 开销：高 QPS 场景不适合。v0.2+ 若压测发现瓶颈，可切换"长期持有 FD + 周期 flush"模式（`close()` 方法已占位）。
- **非 dataclass Event 对象走降级分支**：当前允许测试传轻量替身对象（`__dict__` 或按 Event Protocol 已知字段 `getattr` 兜底）。真实 Event 类型由 `core.contracts.Event` 定义，本模块不假设实现细节。
- **历史规格文档待归档**：`docs/spec/kongming-agent-v1-minimal/11-v1-file-layout.md` 仍保留早期 host bridge 表述。当前代码选择 10-contracts 路线：事件走 `list[EventSink]` 直接 fan-out，HostDispatcher 负责宿主投递与 root agent 生命周期。

## 参考

- [`docs/spec/kongming-agent-v1-minimal/10-contracts.md`](../../spec/kongming-agent-v1-minimal/10-contracts.md) — "Observability / EventSink 边界" 段，含"**不要做的事**"清单
- [`docs/spec/kongming-agent-v1-minimal/11-v1-file-layout.md`](../../spec/kongming-agent-v1-minimal/11-v1-file-layout.md) — `infrastructure/tracing/` 目录职责、`not-in-v1` 清单（`usage_meter.py` / `audit_log.py` / `event_log.py`）
- [`docs/fixes/20260420-v1mini-doc-conformance/fix-report.md`](../../fixes/20260420-v1mini-doc-conformance/fix-report.md) — Finding 2：CLI 装配 `event_sinks` 默认挂 `JsonlTraceSink`
- `src/core/contracts/` — `EventSink` / `Event` Protocol 真源
- `src/core/runner.py` — `_emit` fan-out 入口与"sink 异常不污染主链"兜底

---

## v0.1.4 决策事件 schema（safety-scope-v0.1.4 模块 8 DecisionTrace）

v0.1.4 在 `core.contracts.EventKind` 增加 3 个 safety 决策事件 kind，由 `safety.chain.build_safety_chain(...)` 装配的 `SafetyDecisionEngine` 在每次决策完成后通过 `trace_emitter` 异步 fan-out 到 `event_sinks`，落盘到与其它事件相同的 `.kongming/trace.jsonl`。

### EventKind 与触发时机

| EventKind | 触发时机 |
|---|---|
| `tool.denied` | thread permissions deny 命中，或 `ConsentResolver` 用户拒绝 / 取消后 emit。 |
| `tool.approval_required` | DangerGuard 命中或普通请求未命中 permissions、进入人工 Consent 时 emit。 |
| `tool.silently_allowed` | `full_trust` 普通放行或 thread permissions allow 命中时 emit。 |

### Payload schema

所有 3 个事件 kind 共享同一份 payload schema（`safety.chain._build_event_payload`）：

| 字段 | 类型 | 说明 |
|---|---|---|
| `decision_class` | `str \| None` | `hard_block` / `silent_allow` / `explicit_consent` |
| `decision_source` | `str \| None` | `intrinsic` / `session` / `config` / `standard` / `elevated`；`hard_block` 缺省 |
| `matched_rule` | `str \| None` | 命中规则名（如 `ssh-material` / `git-push`）或 grant key 字符串 |
| `reason` | `str \| None` | 人读理由（用于 trace + UI） |
| `boundary_kind` | `str` | v0.1.4 恒为 `host` |
| `grant_scope` | `str \| None` | `session` / `config`；仅 silent_allow 来自 grant 时携带 |
| `tool_name` | `str` | 触发本次决策的工具名 |
| `path_or_command` | `str \| None` | request.arguments 里的 path 或 command 字段 |
| `request_id` | `str` | request.call_id（运行时唯一标识） |
| `outcome` | `str` | `approved` / `rejected` / `cancelled` |

### 写盘控制：read 类工具的 silently_allowed 默认不写

`tool.silently_allowed` 在 read 类工具（`read_file` / `list_dir`）上默认**不**写盘，避免 jsonl 在大量 tracked 文件读取时膨胀。开关：

- `config.safety.log_silent_reads = true` 或环境变量 `KONGMING_SAFETY_LOG_SILENT_READS=true` 开启
- write 类工具的 silently_allowed **总是写盘**，不受此开关影响
- `tool.denied` / `tool.approval_required` 总是写盘

### CLI verbose 显示

CLI `--verbose` 模式下 `hosts.cli.adapter.CLIEventSink` 会把 3 个新事件翻译成单行进度（`stderr`）：

- `[hard_block:<rule>] x <tool> <path_or_command> - REJECTED (<reason>)`
- `[<severity>] ? <tool> <path_or_command>`
- `[silent_allow:<source>] v <tool> <path_or_command>`

参考：`src/hosts/cli/adapter.py::_render_event_line` 的 v0.1.4 决策事件分支。
