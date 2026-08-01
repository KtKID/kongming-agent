# src/scheduler/ — 定时任务

v0.2 cron 定时任务模块：agent 一等公民工具，用户在对话中用自然语言描述定时需求，agent 调 `schedule_tool` 落地；后台 1s tick loop 触发执行，每次触发走 fresh session + Runner.run。

## 设计理念

| 决策 | 理由 |
|------|------|
| LLM Tool 为主入口（非 CLI / REST） | v0.2 把 cron 从"运维管理面"重构为 agent 工具；用户说"每天 9 点提醒喝水"，agent 自调 `schedule` tool 落地，无需人工操作 CLI |
| 1s ticker + Manager Semaphore(8) | ticker 只做 reserve+submit；`ScheduledRunManager` 持有并发闸门、live Task 和 shutdown，支持秒级触发并控制资源上限 |
| 观测记录分层 | 空转 tick 覆盖写 `ticker_status.json`；有 due/spawned 时写 `audits.jsonl` 的 `tick_dispatched`；异常写 `incidents.jsonl`；记录展示时间按 `scheduler.default_timezone` 输出 |
| fresh session + 工具裁剪 | 每次 cron fire 走独立 session，不复用旧会话；裁掉 `schedule.*` / `cron.*` 工具防止 cron run 自我繁殖 |
| 定时任务 thread + run 上下文隔离 | 每个定时任务绑定一个 `scheduled_task` thread；每次执行仍是独立 run session，聊天页通过 `taskId/runId` 查询参数切换 run 历史，避免多个执行时间共用同一上下文 |
| trust 模式审批（v0.5 默认） | cron 任务只对 `matched_rule=default:ask` 应用 trust 自动放行；builtin ask、destructive、rule error 与 HardBlock 全部失败关闭。trust 自动放行写 `approval.cron.auto_allow` event + `run_approval_auto_allow` audit 双份证据。 |
| `[SILENT]` 投递抑制 | final message 命中 `[SILENT]` 标记时跳过对外投递，状态标 `SILENT`，仍正常落盘审计 |
| 手动试运行与正式日程分离 | Web `run_now` 写入 `manual_run_requested_at`，ticker 原子领取后执行一次；`next_run_at` 始终保存 cron 的正式下一匹配时刻 |
| domain 层 frozen dataclass + zero runtime 判定 | 领域模型只承载数据形态和不变量校验，不放持久化 / trigger / approval 逻辑 |
| 装配与执行分离 | `runtime_factory` 造进程内唯一 `ScheduledRunManager`；ticker、手动试运行和 lifespan 共享同一个提交门户 |
| task 模型选择解析为独立 snapshot | task `preset_id` 优先于全局 selection；`ModelCatalogManager` 为每次执行解析 immutable snapshot，session manifest 与 audit 记录 preset 和 remote model |
| task / run 状态正交（v0.6） | `ScheduledTask.lifecycle` 只表达调度生命周期；`ScheduledRun.status` 表达每次执行结果；Web 通过 `SchedulerManager` 组合展示 |
| v5 数据确定性迁移 | v5 的 `enabled + state` 先保存唯一备份，再映射到 v6 `lifecycle`；`last_run_at` 取最新 terminal run 的 `finished_at` |

## 核心概念

### 领域模型（domain.py）

8 个 frozen dataclass + 9 个 StrEnum 构成数据真源：

- **ScheduledTask**：任务定义，`lifecycle` 是任务状态唯一真源，取值为 `SCHEDULED / PAUSED / DISABLED / EXHAUSTED / DELETED`；另含 `trigger`、`policy`、`delivery`、`target` 和独立的 `manual_run_requested_at`
- **ScheduleTrigger**：调度触发器，`TriggerType`（`CRON` / `ONCE` / `INTERVAL` / `SECONDS`）+ 表达式 + 可选 `run_at`
- **ScheduledRun**：一次执行记录，含 `reservation_id`、`run_id`、fresh `session_id`、稳定 `thread_id`、执行/投递状态与 `cancel_reason`；Web run 历史用 durable terminal 结果展示
- **TaskRuntimeStatus**：Manager 派生的 live 投影，取值为 `IDLE / RUNNING`；任务持久化文件不保存该字段
- **DueTaskReservation**：tick 扫描产出的待执行预留；`reservation_id` 是提交幂等主键
- **TaskExecutionPolicy**：并发策略（`skip` / `replace` / `allow`）+ misfire 策略（`skip` / `catch_up_once` / `fire_now`）+ `approval_mode: ApprovalMode | None`（v0.5 任务级审批模式）
- **ApprovalMode**（v0.5 新增）：`TRUST` / `FAIL_CLOSED`，`resolve_effective_mode(task_mode, global_mode)` 辅助函数解析优先级

### 执行流程

```
[user 对话] → agent 调 schedule_tool → Store.create_task → tasks.json
                                              ↓
后台 ticker（每秒） → Store.reserve_due_tasks → due 列表
                    recurring 保持 SCHEDULED；one-shot 原子转 EXHAUSTED
                                              ↓
                    ScheduledRunManager.submit_scheduled_run(reservation)
                                              ↓
                    HostDispatcher → AgentManager → TaskRegistry（真实取消句柄）
                                              ↓
                    ExecutionBridge.execute_admitted → RunExecutionOverrides
                                              ↓
                    SessionEngine.run → Runner.run（同一 TaskRegistry Task）
                    Store.finish_run_if_running → terminal ScheduledRun 写回
                    task.last_run_at = terminal run.finished_at
                                              ↓
                    DeliveryDispatcher → web/cli sink（或 [SILENT] 跳过）
```

### 调度表达式（schedule_parser.py）

| 格式 | 例 | 含义 |
|------|-----|------|
| 一次性 duration | `10s` / `30m` / `2h` / `1d` | N 时间后跑一次 |
| 周期 | `every 10s` / `every 30m` | 每 N 时间跑 |
| 5 字段 cron | `0 9 * * *` | 标准 cron |
| 6 字段 cron | `*/30 * * * * *` | 含秒位 |
| ISO8601 | `2026-05-03T09:00:00+08:00` | 一次性时间戳 |

### 定时任务 thread 与 run 历史入口

定时任务创建后会绑定一个专属 `scheduled_task` thread。thread metadata 写入 `thread_kind="scheduled_task"`、`source_kind="scheduled_task"`、`source_id=<task_id>`，前端进入该 thread 时据此找到任务。

聊天页 run 历史规则：

1. 进入定时任务 thread 且 URL 没有 `taskId/runId` 时，`ChatPage` 调 `listTaskRuns(taskId, 1)` 选择最新 run，并用 `replace` 跳到 `/chat/{threadId}?taskId={taskId}&runId={runId}`。
2. 有 `taskId/runId` 时，消息列表读取独立 timeline key：`makeCronTimelineKey(threadId, runId)`。
3. 历史消息由 `loadRunMessages(taskId, runId)` 从 run 对应 fresh session 读取；加载前会 `resetThread(cronTimelineId)`，以磁盘历史作为最终真源，避免实时帧和历史补拉产生重复消息。
4. run 历史页人工继续对话时，前端连接 `/ws/cron/tasks/{taskId}/runs/{runId}`；后端按 `ScheduledRun.session_id` 临时装配 generic runtime，用户输入和助手回复继续写入该 `sched-*` session 文件。
5. `cron.message.appended` 实时帧必须带 `thread_id/task_id/run_id`。当前主 thread 收到新 run 帧时，前端跳转到该 run URL，并把帧写入对应 run timeline。
6. 右上角 `ThreadCronRunsPopover` 是 thread 页专用入口，点击 run 只导航到对应 `taskId/runId`，不复用定时任务侧栏的全局 selected task/run 状态。

运行记录左侧图标规则：

| 条件 | 图标 | 语义 |
|------|------|------|
| `status in {completed, success, silent}` 且投递成功 | 绿色 `CheckCircle2` | 执行完成 |
| `status=running` | 蓝色 `LoaderCircle` | 执行中 |
| `failure_reason=needs_approval` 或 `status in {inactivity_timeout, abandoned, cancelled}` | 黄色 `AlertTriangle` | 有问题，需要用户判断或补跑 |
| `status in {failed, error}` 且非审批类原因，或 `delivery_status=failed` / `delivery_error` 非空 | 红色 `XCircle` | 运行或投递错误 |

`CronRunDTO.failure_reason` 是前后端协议字段，当前主要用于把 `failed + needs_approval` 映射为黄色感叹号。

## 代码索引

| 文件 | 导出/内容 | 说明 |
|------|----------|------|
| `__init__.py` | 包占位 | 模块定位注释；后续模块按 dev-checklist 顺序逐步加入 |
| `domain.py` | frozen dataclass + StrEnum + `SCHEMA_VERSION=6` + `TaskLifecycleState` + `TaskRuntimeStatus` + 常量 + `ApprovalMode` | 数据真源。task lifecycle 与 run status 正交；`__post_init__` 只做不变量校验，不触发 IO |
| `store.py` | `Store` | 文件持久化层：append-only run 日志、reservation/run 读取、`finish_run_if_running` 条件终态、全部 stale RUNNING 恢复、audits/incidents/ticker status 与进程锁 |
| `manager.py` | `SchedulerManager` / `SchedulerTaskProjection` | scheduler 数据门户；组合 task lifecycle、durable latest result 与 `ScheduledRunManager` live runtime status，管理专属 thread 创建与回滚 |
| `run_portal.py` | `ScheduledRunSubmitter` / `ScheduledRunPortal` | ticker 与 ScheduleTool 消费的跨模块最小协议，避免直接依赖 application live owner |
| `ticker.py` | `tick` / `run_ticker_loop` | 触发循环：默认 1s reserve due reservation，再调用 `submit_scheduled_run`；模块内零 live Task、Semaphore 和 drain 状态 |
| `execution_bridge.py` | `ExecutionBridge` / `InactivityWatchdog` | Manager 准入后的 cron 执行 plan：fresh session、工具裁剪、审批包装、watchdog、投递和条件终态写回 |
| `schedule_parser.py` | `parse_schedule` | 自然语言/cron 解析：5/6 字段 cron + `every Ns/m/h/d` + 一次性 duration + ISO8601；依赖 `croniter` |
| `delivery.py` | `DeliverySink`(ABC) / `DeliveryResult` / `DeliveryDispatcher` | v0.3 投递层：按 `delivery.channel` 路由到 web/cli sink；silent_marker 命中跳过；投递失败不污染 run.status |
| `policy.py` | `apply_concurrency_policy` / `get_misfire_support` / `assert_misfire_policy_supported` / `ConcurrencyAction` | 历史 Store 并发策略 helper + misfire 三策略应用器；生产 scheduled run 的 live ALLOW/FORBID/REPLACE 由 `ScheduledRunManager` 判定 |
| `runtime_factory.py` | `build_scheduled_run_manager` / `build_cron_execution_bridge` | 生产入口装配进程内 `ScheduledRunManager`；bridge builder 服务执行 plan 的集中装配和针对性测试 |
| `safety_wrapper.py` | `ScheduleApprovalProvider` | mode 包装器：HardBlock 透传拒绝；bypass-immune ask 失败关闭；仅 `default:ask` 进入 trust 自动放行或 fail_closed/write_file-create 处置。 |
| `silent.py` | `is_silent` / `strip_silent_prefix` | `[SILENT]` 标记纯函数检测与前缀剥离 |
| `timing.py` | `grace_seconds_for_period` / `is_within_oneshot_grace` / `is_stale_recurring` / `parse_iso` / `to_iso` / `utc_now` / `compute_first_run_at` / `compute_next_run_at` | 时间与窗口纯函数：grace 算法 + ISO8601 解析 + 首次触发与 recurring 下一匹配时刻计算 |

### 关联文件（非 src/scheduler/ 内）

| 文件 | 说明 |
|------|------|
| `src/application/scheduled_runs/manager.py` | `ScheduledRunManager` / dispatcher Protocol / live cell | live owner 门户：reservation 全 payload 幂等、submission sequence、per-task ALLOW/FORBID/REPLACE、Manager Semaphore、注入普通 thread dispatcher、真实取消、`FINISHING` durable 发布、live 清理、lifecycle finished 与 shutdown |
| `src/core/contracts/run_execution.py`、`approval.py` | `RunExecutionOverrides` / `InteractiveApprovalRebinder` | scheduled run 通过 `SessionEngine.run` 传入 frozen 单次依赖快照；Web 只重绑 SafetyGatedApproval 的人工 Consent 终点，DangerGuard、模式、本子权限和事件链继续由 runtime 安全门户持有 |
| `src/tools/builtin/schedule_tool.py` | LLM Tool 主入口；`run_now` 复用进程内 `ScheduledRunManager.submit_scheduled_run` |
| `src/infrastructure/config/models.py` | `SchedulerConfig`（enabled/home/interval/max_inflight/max_task_age_seconds）+ env 覆盖 |
| `src/hosts/cli/main.py` lifespan | startup 起 ticker，shutdown 优雅退出 |
| `src/hosts/web/app.py` lifespan | 同上 |
| `src/hosts/web/routers/cron.py` | Web cron REST：任务 DTO 只暴露 `lifecycle`、`latest_run_status`、`live_runtime_status` 三个正交字段；run DTO 透出执行与投递结果 |
| `src/hosts/cli/cron_delivery.py` | CLI 投递 sink（buffer + drain_pending） |
| `src/hosts/web/websocket/routes.py` | Web generic WS；新增 `/ws/cron/tasks/{task_id}/runs/{run_id}`，按 run `session_id` 接入人工对话 |
| `src/safety/approval/default_rules.py` | capability=`schedule` 锚点常量 |
| `web/src/pages/Chat.tsx` | 定时任务 thread 默认跳最新 run、加载独立 run history、处理 `cron.message.appended` 实时跳转 |
| `web/src/modules/scheduler/api.ts` / `types.ts` | 前端 cron REST client 与 `SchedulerRunVM`；run DTO 映射 `failure_reason → failureReason` |
| `web/src/modules/scheduler/components/ThreadCronRunsPopover.tsx` | 聊天页右上角 run 历史入口；按 run 状态和 `failureReason` 渲染绿勾/黄感叹号/红叉 |

## 配置

`config.scheduler.*` 由 `infrastructure.config.models.SchedulerConfig` 定义，支持 `KONGMING_*` 环境变量覆盖：

| 配置项 | 默认值 | 环境变量 | 说明 |
|--------|--------|----------|------|
| `enabled` | `false` | `KONGMING_SCHEDULER_ENABLED` | 总开关 |
| `home` | `.kongming/cron` | `KONGMING_SCHEDULER_HOME` | 数据目录 |
| `interval` | `1` (秒) | `KONGMING_SCHEDULER_INTERVAL` | tick 间隔 |
| `max_inflight` | `8` | `KONGMING_SCHEDULER_MAX_INFLIGHT` | `ScheduledRunManager` Semaphore 最大并行任务数 |
| `default_timezone` | `UTC` | `KONGMING_SCHEDULER_DEFAULT_TIMEZONE` | 任务默认时区与 cron 观测记录展示时区 |
| `max_task_age_seconds` | — | `KONGMING_SCHEDULER_MAX_TASK_AGE_SECONDS` | 过期任务清理阈值 |
| `approval.mode` | `trust` | `KONGMING_SCHEDULER_APPROVAL_MODE` | 全局 cron 审批模式（v0.5） |
| `default_max_turns` | `90` | `KONGMING_SCHEDULER_DEFAULT_MAX_TURNS` | cron task.policy.max_turns 缺省时的兜底（v0.5.1） |

## 已知问题 / 待完成

- **v4 及更早数据采用重置式迁移**：启动时保留备份并生成空 v6 文件；v5 数据采用保留式字段迁移。
- **lease + persistent waiting_approval 未实现**：当前 consent 命中直接 fail，不挂起等待人工授权；Web run 历史用 `failed + failure_reason=needs_approval` 展示为黄色感叹号。
- **投递无重试**：v0.3 投递层失败仅记录，重试留给 v0.4。
- **SQLite 持久化未实现**：任务量上千时需从 JSON 文件切换到 SQLite。
- **wall timeout 未实现**：当前执行 plan 使用 inactivity watchdog。
- **TrustResolver silent_allow gap**：schedule/memory 的 known gap 待第二轮修复。

## 参考

- [docs/spec/agent-cron-module-v0.2/README.md](../../spec/agent-cron-module-v0.2/README.md) — v0.2 设计文档
- [docs/spec/agent-cron-module-v0.1/README.md](../../spec/agent-cron-module-v0.1/README.md) — v0.1 设计文档
- [docs/spec/agent-cron-module-v0.1/05-hermes-feature-tradeoffs.md](../../spec/agent-cron-module-v0.1/05-hermes-feature-tradeoffs.md) — Hermes 取舍参考

## 前端 Spec

- `scheduler-frontend/`：Web 定时任务前端模块 spec，覆盖 `web/src/modules/scheduler/` 的入口、抽屉、列表、创建弹窗、状态模型和演进路线。
  - [README.md](../../spec/modules/定时任务/scheduler-frontend/README.md)
  - [architecture.html](../../spec/modules/定时任务/scheduler-frontend/architecture.html)
- `scheduled-task-thread/`：定时任务专属 thread spec，覆盖 cron 创建时 thread provisioning、`ScheduledTask.thread_id`、执行 session 绑定、thread metadata 业务类型和前端定时任务列表入口。
  - [README.md](../../spec/modules/定时任务/scheduled-task-thread/README.md)
  - [diagrams.md](../../spec/modules/定时任务/scheduled-task-thread/diagrams.md)

### 2026-05 前端刷新策略更新

- `scheduler-frontend` 已切到按需刷新方案
- 前端主链路不再要求全局常驻 `/ws/cron`
- 默认触发器是：打开抽屉、任务操作成功、窗口重新聚焦
- `/ws/cron` 保留为后端可选能力，后续只有在抽屉内确实需要秒级同步时再评估

## 任务级审批模式（v0.5）

每个 cron task 可声明 `approval_mode`：
- `trust`（**v0.5 默认**）：自动放行 `default:ask`，保留原始 `matched_rule + source` 并写审计；builtin/destructive/rule-error ask 直接拒绝。
- `fail_closed`：拒绝 `default:ask`；`write_file` 在 cwd 内创建新文件的白名单仍可放行。bypass-immune ask 始终拒绝。

优先级：`task.policy.approval_mode` > `cfg.scheduler.approval.mode` > 默认 `trust`。

LLM 创建任务通常**不需要**带 `approval_mode`，自动走 trust。要严格审批显式 `approval_mode="fail_closed"` 或全局 `cfg.scheduler.approval.mode: "fail_closed"`。

详细设计：[../../spec/scheduler-approval-task-level-v0.5/](../../spec/scheduler-approval-task-level-v0.5/)
