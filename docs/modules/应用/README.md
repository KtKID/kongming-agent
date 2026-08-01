# src/application/ — 应用编排

承接跨核心运行时的任务级编排：agent workflow、子 agent 生命周期和定时任务执行桥。

## 设计理念

| 决策 | 理由 |
|------|------|
| workflow / subagent / scheduled run 放入 `application/` | 这些模块消费 `core.Runner`、`runtime_assembly.SessionEngine`、`scheduler` 和工具权限边界，属于应用编排层 |
| `AgentWorkflowManager` 作为 workflow 门面 | 对外提供策略列表、策略描述、payload 执行和审计产物收口，内部再委托策略注册表 |
| `AgentManager` 统一管理所有 child | workflow 和普通 spawn 都走父 `AgentManager.spawn()`；`TaskRegistry` 是 pending/running/terminal、Web 投影和 200 条终态 retention 的单一真源 |
| child 工具能力按父级单调收紧 | `tool_scope.clip_child_tool_snapshot()` 计算 `父级实际工具 ∩ 请求工具 ∩ scope 允许工具`；缺省请求继承父集合，显式空集合保持零工具 |
| `ScheduledRunManager` 统一 live ownership | reservation 经同一门户进入 HostDispatcher/AgentManager/TaskRegistry；Manager 持有并发准入、真实取消和 shutdown |
| `ExecutionBridge` 保持 fresh session 语义 | Manager 准入后生成独立 session，裁剪 schedule/cron 工具，通过条件终态写回 scheduler store |
| workflow/role/subagent 只传 preset ID | `ModelCatalogResolver` 为每个 child 解析独立 immutable snapshot；父 run 更新期间不会改写已经启动的 child snapshot |

## 核心流程

```text
AgentWorkflowTool / CLI
  -> AgentWorkflowManager.run_workflow_payload(...)
  -> AgentWorkflowStrategyManager.run_strategy(...)
  -> ParallelWorkflowStrategy / MapReduceStrategy
  -> AgentManager.spawn(...) -> TaskRegistry.register_pending(...)
  -> child AgentCell -> SessionEngine.run(...) -> Runner.run(...)
  -> workflow audit + reports + AgentWorkflowResult

Ticker reserve_due_tasks
  -> ScheduledRunManager.submit_scheduled_run(...)
  -> HostDispatcher -> AgentManager -> TaskRegistry.register_run(...)
  -> ExecutionBridge.execute_admitted(...)
  -> fresh session + filtered tools + Safety consent rebinding + approval wrapper
     + RunExecutionOverrides
  -> SessionEngine.run(...) -> Runner.run(...)  # TaskRegistry 已注册的同一 Task
  -> ScheduledRun + DeliveryDispatcher
  -> FINISHING / durable future / live 索引清理 -> cron.run.finished
```

## Workflow 详细文档

| workflow | 文档 | 当前状态 | 说明 |
|----------|------|----------|------|
| `parallel` | [workflow-parallel.md](workflow-parallel.md) | available | 通用并行子任务扇出 / 收口，prompt 主体来自调用方 `task_specs` |
| `map_reduce` | [workflow-map-reduce.md](workflow-map-reduce.md) | available | 文件分片、输入物化、mapper 子 agent、确定性 reducer、专属产物 |
| `roundtable_review` | [agent-workflow/agent-role-presets-v0.1](../../spec/agent-workflow/agent-role-presets-v0.1/README.md) | available | 代码模块设计圆桌评审：通过 `participants.select` 选择角色，独立分析、共享 ReviewBoard、交叉质询、arbiter 最终报告 |
| `deep_research` | [workflow-deep-research.md](workflow-deep-research.md) | available | 证据链调研：Plan / Search / Extract / Group / Crosscheck / Report 六阶段，支持来源 provider 注入和带引用报告 |

## 代码索引

| 文件 | 导出/内容 | 说明 |
|------|----------|------|
| `src/application/__init__.py` | 包入口 | 应用编排层包标记 |
| `src/application/agent_workflows/context.py` | `WorkflowExecutionContext` | workflow id、父 session、workflow 目录、审计 writer、超时字段 |
| `src/application/agent_workflows/manager.py` | `AgentWorkflowManager` / `AgentWorkflowResult` / `AgentWorkflowAuditWriter` | workflow 编排门面：策略分发、并行执行、目录分配、审计和报告写入 |
| `src/application/agent_workflows/strategies/base.py` | `WorkflowRunRequest` / `WorkflowStrategy` / strategy 异常 | 策略运行协议和异常模型 |
| `src/application/agent_workflows/strategies/description.py` | `WorkflowStrategyDescription` 等 | 面向父 agent / LLM 的策略说明模型 |
| `src/application/agent_workflows/strategies/manager.py` | `AgentWorkflowStrategyManager` | 策略注册、list / describe / run 分发 |
| `src/application/agent_workflows/strategies/parallel.py` | `ParallelWorkflowStrategy` | 并行子任务策略 |
| `src/application/agent_workflows/strategies/map_reduce/` | planner / materializer / mapper / validator / reducer / artifacts / strategy / contracts | `map_reduce` 分片、输入物化、mapper prompt、输出校验、确定性 reducer 和产物写入 |
| `src/application/agent_workflows/strategies/roundtable_review/` | contracts / board / prompts / strategy | `roundtable_review` payload 解析、ReviewBoard 产物、三阶段 prompt 和圆桌评审状态机 |
| `src/application/agent_workflows/strategies/deep_research/` | contracts / source_provider / dedupe / fact_board / jury / task_log / strategy | `deep_research` 来源检索、URL 去重、事实白板、jury 裁决、子任务日志和六阶段状态机 |
| `src/application/agent_workflows/task_models.py` | `SubAgentTask` / `SubAgentRun` | workflow child 的不可变任务规格和终态结果值对象 |
| `src/application/agents/manager.py` / `registry.py` | `AgentManager` / `TaskRegistry` / `TaskProjection` | child spawn、独立 session、状态机、终态幂等、live-safe retention 和宿主查询门户 |
| `src/application/scheduled_runs/manager.py` | `ScheduledRunManager` / `ScheduledRunDispatcherFactory` | 定时任务 live owner 门户：幂等提交、并发策略、Manager 限流、普通 thread dispatcher 启动与真实取消；submit receipt 真源位于 `scheduler.domain` |
| `src/application/tool_scope.py` | `resolve_tool_snapshot` / `clip_child_tool_snapshot` | Agent 树工具快照解析和三方求交入口；保持父级顺序与不可变快照 |
| `src/application/subagents/permissions.py` | `SubAgentPermissionSpec` / grant / audit hook / scoped file wrapper | 子 agent scoped workdir 声明式权限、执行期路径边界和工具审计记录 |
| `src/application/scheduled_runs/execution_bridge.py` | `ExecutionBridge` / `InactivityWatchdog` / filtered lookup / aggregate sink | 已准入 cron run 的执行 plan、工具裁剪、审批包装、watchdog、投递和条件终态写回 |

## 配置

| 配置 | 来源 | 说明 |
|------|------|------|
| `Config` | `src/infrastructure/config/models.py` | workflow manager 和 execution bridge 共用的全局配置 |
| `ModelCatalogResolver` | `src/core/contracts/model_catalog.py` | 应用层跨模块模型解析协议；实现由 composition root 注入 |
| `scheduler.default_max_turns` | `SchedulerConfig` | cron run 缺省 max_turns |
| workflow 运行目录 | session file store 下的 `agent-workflows/<workflow_id>/` | 写 `workflow.json`、`audit.jsonl`、`result.json`、`reports/`、`agents/` |

## 已知问题 / 待完成

| 项 | 状态 |
|----|------|
| `map_reduce` live smoke | 进入 `map-reduce-live-smoke-v0.1` 后续任务 |
| `map_reduce` raw_text 语义校验 | 当前只确认 mapper run completed 和文本长度，需补 report 有效性判断 |
| workflow 产物 UI 聚合 | 由 Web 侧后续视图接入 |
