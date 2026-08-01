# workflow: parallel

`parallel` 是通用并行扇出 / 收口 workflow。它把调用方给出的多个独立 `task_specs` 转成多个子 agent 任务，同时运行，最后把每个子 agent 的报告写入 workflow 产物目录。

## 入口

公开工具入口是 `run_agent_workflow`：

```json
{
  "mode": "parallel",
  "payload": {
    "task_specs": [
      {
        "task_name": "review-a",
        "prompt": "检查 A 模块",
        "context": "",
        "tool_names": ["read_file", "list_dir"],
        "skill_names": [],
        "permission": {"mode": "scoped_workdir"}
      }
    ]
  }
}
```

兼容字段：

- `payload.task_specs`
- `payload.tasks`

限制：

- `task_specs` 必须是非空数组。
- v1 最多 8 个子任务。
- 每个任务必须有非空 `task_name` 和 `prompt`。
- `permission.mode` 当前只支持 `scoped_workdir`。

## 执行流程

1. `RunAgentWorkflowTool._run()` 接收工具参数，按 `mode` 选择 workflow。
2. `_normalize_workflow_payload(mode="parallel")` 读取 `task_specs` 或 `tasks`。
3. `AgentWorkflowManager.run_workflow_payload()` 把 payload 交给策略注册表。
4. `ParallelWorkflowStrategy.run()` 校验任务列表，并调用 `AgentWorkflowManager.run_parallel_specs()`。
5. `run_parallel_specs()` 把每个 task spec 转成 `SubAgentTask`。
6. `run_parallel()` 创建 workflow 目录和 `agents/<task_run_id>/work/`。
7. 每个 `SubAgentTask` 通过 `_run_one()` 构造 `SpawnAgentRequest`，交给父 `AgentManager.spawn()`。
8. child 通过统一 `AgentCell` / `agent_loop` 使用共享 runtime、独立 session 执行，状态写入 `TaskRegistry`。
9. manager 写入 `workflow.json`、`audit.jsonl`、`reports/index.json`、`reports/<task_run_id>.json`、`result.json`。

## 框架生成的提示词

`parallel` 的任务主体 prompt 来自调用方 payload。框架只包两层固定文本：子 agent 的系统身份说明和 dispatch prompt。

### 子 agent AgentSpec instructions

生成位置：`src/application/agents/subagent_tools.py::build_child_agent_spec`

```text
你是 kongming 子 agent。只处理分派给你的任务。只使用本次派发的任务文本和必要上下文。如果任务给出工作目录，文件写入必须位于该目录内。输出包含：结论、关键依据、风险或未完成项。
```

### dispatch prompt 模板

生成位置：`src/application/subagents/manager.py::_build_dispatch_prompt`

```text
任务名称：{task.task_name}
任务 ID：{task.task_id}

任务：
{task.prompt}

必要上下文：
{task.context}

工作目录：
{working_dir}
所有文件写入都必须在这个工作目录内。
```

字段规则：

- `必要上下文` 只在 `task.context` 非空时出现。
- `工作目录` 只在 task metadata 里有 `working_dir` 时出现。
- `所有文件写入都必须在这个工作目录内。` 只在任务启用了工具时追加。

## 代码行为

`parallel` 不复制输入源码。它只为每个子 agent 创建运行目录：

```text
agent-workflows/<workflow_id>/
  workflow.json
  audit.jsonl
  result.json
  reports/
    index.json
    <task_run_id>.json
  agents/
    <task_run_id>/
      subagent.json
      result.json
      work/
```

子 agent 可读写范围由 `scoped_workdir` 权限控制：

- `read_file`、`write_file`、`list_dir` 会被 `ScopedFileTool` 包装。
- 工具 path 会 resolve 到 `working_dir` 内。
- 越界 path 会被拒绝，并写 `subagent_approval_decided` 审计事件。
- 允许工具集合来自 task spec 的 `tool_names`。

## 数据结构

### SubAgentTask

```text
task_id: str
task_name: str
prompt: str
context: str
tool_names: tuple[str, ...]
skill_names: tuple[str, ...]
permission: SubAgentPermissionSpec | None
metadata: dict[str, object]
```

### SubAgentRun

```text
task: SubAgentTask
session_id: str
run_id: str
status: str
content: str
error_message: str | None
turn_count: int
```

### SubAgentPermissionSpec

```text
mode: "scoped_workdir"
```

### AgentWorkflowResult

```text
workflow_id: str
mode: str
parent_session_id: str
workflow_dir: Path
started_at: str
finished_at: str
runs: tuple[SubAgentRun, ...]
reports: tuple[SubAgentReportProjection, ...]
report_index_path: Path
data: Mapping[str, object] | None
completed_override: bool | None
```

## 审计事件

典型事件顺序：

```text
workflow_started
agent_assigned
subagent_created
subagent_grant_bound
subagent_approval_decided
agent_completed / agent_failed
subagent_reported
workflow_completed
```

## 适用场景

- 多个互不依赖的代码审查任务。
- 多个候选方案并行探索。
- 多个文件或模块各自生成报告，父 agent 直接汇总。

## 已知边界

- 子任务之间没有实时通信。
- 强顺序依赖需要 pipeline / DAG 类策略。
- 大规模同构文件分析使用 `map_reduce` 更稳定。
