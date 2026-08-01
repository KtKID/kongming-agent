# workflow: map_reduce

`map_reduce` 是面向大工程同构代码分析的分片 workflow。它把输入文件规划成稳定 shard，给每个 shard 创建 mapper 子 agent，再把 mapper 输出交给确定性 reducer 收口。

## 入口

公开工具入口是 `run_agent_workflow`：

```json
{
  "mode": "map_reduce",
  "payload": {
    "objective": "检查 workflow runtime 风险",
    "input_source": {
      "kind": "path_glob",
      "root_dir": ".",
      "include": ["src/**/*.py"],
      "exclude": [".venv/**"],
      "files": [],
      "index_provider": "rg",
      "input_digest": null
    },
    "shard_strategy": {
      "kind": "by_directory",
      "max_files_per_shard": 8,
      "max_estimated_tokens_per_shard": 20000,
      "min_shards": 1,
      "max_shards": 8,
      "preserve_directory_boundary": true,
      "prefer_dependency_cohesion": false
    },
    "mapper": {
      "name_prefix": "map",
      "prompt_template": "检查每个文件的 bug、边界和风险。",
      "tool_names": ["read_file", "list_dir"],
      "skill_names": [],
      "permission_mode": "scoped_workdir",
      "max_turns": 3,
      "max_output_chars": 60000
    },
    "reducer": {
      "kind": "deterministic",
      "dedupe_strategy": "exact_dedupe_key",
      "ranking_strategy": "severity_first",
      "max_findings": 50,
      "include_failed_shards": true,
      "reducer_prompt_template": null
    },
    "limits": {
      "max_concurrency": 4,
      "workflow_timeout_seconds": 1800,
      "mapper_timeout_seconds": 300,
      "reducer_timeout_seconds": 300,
      "mapper_retries": 0,
      "validation_repair_retries": 0
    },
    "output_contract": "code_findings",
    "audit_tags": ["code_review"]
  }
}
```

payload 顶层直接使用 `MapReduceWorkflowSpec` 字段。工具失败提示也会给出同一套骨架，用来修正模型常见的外层包裹、绝对路径和缺省字段问题。

## 执行流程

1. `RunAgentWorkflowTool._run()` 接收工具调用。
2. `_normalize_map_reduce_payload()` 补齐默认值并规范化字段。
3. `MapReduceStrategy.run()` 解析 `MapReduceWorkflowSpec`。
4. `_validate_runtime_limits()` 校验运行限制。
5. `MapReducePlanner.plan()` 解析输入根目录、发现文件、按策略生成 `MapShard`。
6. `AgentWorkflowManager.prepare_subagent_tasks()` 为每个 shard 绑定 `agents/<task_run_id>/work/`。
7. `MapperInputMaterializer.materialize()` 把每个 shard 的输入文件复制到 mapper workdir 的 `input/`。
8. `MapperPromptBuilder.build_from_spec()` 生成 mapper 子 agent prompt。
9. `_run_mapper_tasks()` 按 `limits.max_concurrency` 并发运行子 agent，按 mapper timeout 和 retries 收口。
10. `code_findings` 输出经过 `MapReduceMapperOutputValidator` 校验。
11. `raw_text` 输出只收集文本长度和完成状态。
12. `MapReduceReducer.reduce()` 对 `code_findings` 做确定性去重、排序和覆盖率汇总。
13. `MapReduceArtifactWriter.write_all()` 写 `map_reduce/` 细节产物。
14. manager 写公共 workflow 产物和审计事件。

## 输入文件物化

输入物化是框架代码行为。LLM 只提供 payload，复制动作由 `MapperInputMaterializer` 执行。

代码路径：

- `src/application/agent_workflows/strategies/map_reduce/strategy.py::_materialize_inputs`
- `src/application/agent_workflows/strategies/map_reduce/input_materializer.py::MapperInputMaterializer.materialize`
- `src/application/agent_workflows/strategies/map_reduce/input_materializer.py::_materialize_one_file`

核心动作：

```text
source = _resolve_input_file(input_root, raw_file_path)
original_path = source.relative_to(input_root).as_posix()
content = source.read_bytes()
digest = sha256(content)
materialized_content = maybe_truncate(content)
materialized_path = "input/" + original_path
destination = input_dir / original_path
destination.write_bytes(materialized_content)
```

产物形态：

```text
agents/<task_run_id>/work/
  input_manifest.json
  input/
    <original_path>
```

`input_manifest.json` 记录：

```json
{
  "shard_id": "shard-...",
  "task_run_id": "001-shard-...",
  "input_dir": "input",
  "files": [
    {
      "original_path": "src/infrastructure/config/loader.py",
      "materialized_path": "input/src/infrastructure/config/loader.py",
      "content_digest": "sha256:...",
      "truncated": false,
      "truncation_reason": null
    }
  ],
  "materialized_at": "2026-06-09T13:13:10.808719+00:00"
}
```

这个设计让 mapper 只通过 scoped workdir 读取自己的输入快照，同时保留原始路径、内容摘要和截断状态。

## 框架生成的 mapper 提示词

生成位置：`src/application/agent_workflows/strategies/map_reduce/mapper.py`

### code_findings mapper prompt

当 `output_contract="code_findings"` 时使用：

```text
你是 map_reduce mapper 子 agent，只分析当前 shard。

任务目标：
{objective}

shard 信息：
{shard_json}

输入文件映射：
{manifest_json}

执行要求：
- 只读取输入文件映射中的 materialized_path。
- 在 files_seen 中写 original_path，locations.path 也使用 original_path。
- 每条 finding 必须至少包含一个 locations 条目，locations.line_start 和 locations.line_end 必须是原始文件行号。
- evidence 必须包含可核验的文件路径和行号，excerpt 使用短摘录。
- 文件被截断时，把无法确认的风险写入 errors 或 coverage.skipped_files。
- 没有发现问题时，findings 输出空数组，并完整填写 coverage。

只输出一个 JSON 对象，禁止输出 Markdown、解释文字或代码块。
JSON 顶层结构必须满足 code_findings 契约：
{example_payload_json}
```

`example_payload_json` 的结构：

```json
{
  "output_contract": "code_findings",
  "shard_id": "shard-...",
  "status": "completed",
  "summary": "用一句中文概括本 shard 的检查结果。",
  "files_seen": ["path/to/file.py"],
  "findings": [
    {
      "dedupe_key": "shard-...:path/to/file.py:10:示例问题标题",
      "title": "示例问题标题",
      "category": "bug",
      "severity": "P1",
      "confidence": 0.8,
      "locations": [
        {
          "path": "path/to/file.py",
          "line_start": 10,
          "line_end": 12,
          "symbol": "ExampleSymbol",
          "excerpt": "短证据摘录，保留关键代码。"
        }
      ],
      "evidence": "path/to/file.py:10-12 显示该问题的直接证据。",
      "rationale": "说明为什么这是问题。",
      "recommendation": "给出可执行修复建议。",
      "impact_area": ["runtime"],
      "source_shard_id": "shard-..."
    }
  ],
  "coverage": {
    "files_assigned": 1,
    "files_seen_count": 1,
    "symbols_seen_count": 0,
    "skipped_files": [],
    "skip_reasons": []
  },
  "errors": [
    {
      "error_type": "tool_error",
      "message": "只在真实发生错误时填写。",
      "file_path": "path/to/file.py",
      "retryable": true
    }
  ]
}
```

### raw_text mapper prompt

当 `output_contract="raw_text"` 时使用：

```text
你是 map_reduce mapper 子 agent，只完成当前 shard 的任务。

任务目标：
{objective}

shard 信息：
{shard_json}

输入文件映射：
{manifest_json}

mapper 任务：
{mapper.prompt_template}

输出要求：
- 直接输出 mapper 任务要求的最终文本。
- 保持简洁，禁止输出 Markdown 代码块。
```

`raw_text` 会把调用方的 `mapper.prompt_template` 原样嵌入 prompt。当前实现只对完成状态和文本长度做收集，缺少语义有效性校验。

### child spawn seed

所有 mapper prompt 由 `build_spawn_request_from_workflow_task()` 转成 child seed：

```text
任务名称：{task.task_name}
任务 ID：{task.task_id}

任务：
{mapper_prompt}

必要上下文：
{shard.context}

工作目录：
{working_dir}
所有文件写入都必须在这个工作目录内。
```

同时子 agent 的 AgentSpec instructions 固定为：

```text
你是 kongming 子 agent。只处理分派给你的任务。只使用本次派发的任务文本和必要上下文。如果任务给出工作目录，文件写入必须位于该目录内。输出包含：结论、关键依据、风险或未完成项。
```

## reducer 行为

`reducer.kind` 当前只支持 `deterministic`。`reducer_prompt_template` 会进入数据结构，但 v0.1 reducer 由 Python 代码确定性执行，没有 LLM reducer prompt。

`code_findings` reducer：

- 收集所有 `MapperOutputEnvelope.findings`。
- 按 `reducer.dedupe_strategy` 去重：
  - `exact_dedupe_key`
  - `file_line_title`
- 按 `reducer.ranking_strategy` 排序：
  - `severity_first`
  - `confidence_first`
  - `impact_first`
- 截断到 `reducer.max_findings` 形成 `top_findings`。
- 汇总 `CoverageSummary`。
- 按 `include_failed_shards` 决定是否附带失败分片。

`raw_text` reducer：

- 统计 completed shard。
- `deduped_findings` 和 `top_findings` 固定为空。
- `coverage_summary.notes` 指示父 agent 读取 reports 的 `summary/content` 做最终汇总。

## 数据结构

### MapReduceWorkflowSpec

```text
mode: "map_reduce"
objective: str
input_source: MapReduceInputSource
shard_strategy: ShardStrategy
mapper: MapperSpec
reducer: ReducerSpec
limits: MapReduceLimits
output_contract: "code_findings" | "raw_text"
audit_tags: tuple[str, ...]
```

### MapReduceInputSource

```text
kind: "path_glob" | "file_list"
root_dir: str
include: tuple[str, ...]
exclude: tuple[str, ...]
files: tuple[str, ...]
index_provider: str | None
input_digest: str | None
```

规则：

- `root_dir` 必须是 workspace 内相对目录。
- `file_list.files[]` 必须是相对路径。
- `path_glob.include/exclude` 使用相对 glob。
- planner 只收集普通文件。

### ShardStrategy

```text
kind: "by_directory" | "by_file_count"
max_files_per_shard: int
max_estimated_tokens_per_shard: int
min_shards: int
max_shards: int
preserve_directory_boundary: bool
prefer_dependency_cohesion: bool
```

当前 `prefer_dependency_cohesion=true` 会被归一化回 `false`，依赖图分片进入后续能力。

### MapShard

```text
shard_id: str
shard_name: str
display_order: int
files: tuple[str, ...]
module_hint: str
shard_reason: str
estimated_tokens: int
shard_digest: str
context: str
```

`shard_id` 来自 shard 文件集合摘要，`context` 会写入 mapper prompt。

### MapperSpec

```text
name_prefix: str
prompt_template: str
tool_names: tuple[str, ...]
skill_names: tuple[str, ...]
permission_mode: "scoped_workdir"
max_turns: int
max_output_chars: int
```

### MapperOutputEnvelope

```text
output_contract: "code_findings" | "raw_text"
shard_id: str
status: "completed" | "partial" | "failed"
summary: str
files_seen: tuple[str, ...]
findings: tuple[CodeFinding, ...]
coverage: MapperCoverage
errors: tuple[MapperError, ...]
```

### CodeFinding

```text
dedupe_key: str
title: str
category: "bug" | "security" | "error_handling" | "resource" | "design" | ...
severity: "P0" | "P1" | "P2" | "P3"
confidence: float
locations: tuple[CodeLocation, ...]
evidence: str
rationale: str
recommendation: str
impact_area: tuple[str, ...]
source_shard_id: str
```

### ReducerOutput

```text
status: "completed" | "partial" | "failed"
workflow_id: str
output_contract: "code_findings" | "raw_text"
total_shards: int
completed_shards: int
failed_shards: int
deduped_findings: tuple[CodeFinding, ...]
top_findings: tuple[CodeFinding, ...]
coverage_summary: CoverageSummary
failed_shard_reports: tuple[FailedShardReport, ...]
followups: tuple[str, ...]
reduced_at: str
```

## 产物

公共 workflow 产物：

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
        input_manifest.json
        input/
```

map_reduce 专属产物：

```text
map_reduce/
  shards.json
  mappers/
    index.json
  reducer/
    result.json
```

## 审计事件

典型事件顺序：

```text
map_reduce_started
map_reduce_shards_planned
map_mapper_input_materialized
map_reduce_inputs_materialized
map_mapper_started
subagent_created
subagent_grant_bound
subagent_approval_decided
map_mapper_completed / map_mapper_failed / map_mapper_timeout
map_mapper_output_validated / map_mapper_output_rejected / map_mapper_output_collected
map_reduce_reducer_started
map_reduce_reducer_completed / map_reduce_reducer_failed
map_reduce_completed
```

## 已知边界

- `raw_text` 输出缺少语义校验，可能出现“状态 completed、内容只回显 prompt”的假完成。
- `validation_repair_retries` v0.1 必须为 0。
- 输入模型以文件为中心，非文件型 synthetic shards 需要后续扩展。
- 子 agent 工具权限由 `scoped_workdir` 限定，mapper 需要读取 `work/input/` 下的物化副本。
