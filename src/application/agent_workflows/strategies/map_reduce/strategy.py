"""map_reduce workflow 策略状态机。

本脚本负责把 map_reduce payload 转换为可执行的 planner -> mapper -> validator -> reducer 链路。
作用是让 AgentWorkflowStrategyManager 能以 mode=map_reduce 分发大工程同构代码分析任务，并产出公共 workflow 结果与 map_reduce 细节产物。
关键执行流程：解析 MapReduceWorkflowSpec，生成稳定 shards，物化 mapper 输入，派发子 agent，校验 mapper JSON，reducer 确定性归并，写入 result/report/audit。
关键类：MapReduceStrategy 提供策略说明和运行入口。
"""

from __future__ import annotations

import asyncio
from collections.abc import Mapping
from dataclasses import replace
from datetime import UTC, datetime
from typing import Any

from application.agent_workflows.context import WorkflowExecutionContext
from application.agent_workflows.strategies.description import (
    WorkflowStrategyCatalogEntry,
    WorkflowStrategyDescription,
    WorkflowStrategyInputField,
)
from application.agent_workflows.strategies.map_reduce.artifacts import (
    MapReduceArtifactWriter,
)
from application.agent_workflows.strategies.map_reduce.contracts import (
    CoverageSummary,
    FailedShardReport,
    MapperInputManifest,
    MapperOutputEnvelope,
    MapperValidationResult,
    ReducerOutput,
    parse_map_reduce_workflow_spec,
)
from application.agent_workflows.strategies.map_reduce.input_materializer import (
    MapperInputMaterializer,
)
from application.agent_workflows.strategies.map_reduce.mapper import MapperPromptBuilder
from application.agent_workflows.strategies.map_reduce.planner import MapReducePlanner
from application.agent_workflows.strategies.map_reduce.reducer import MapReduceReducer
from application.agent_workflows.strategies.map_reduce.validator import (
    MapReduceMapperOutputValidator,
)
from application.subagents.manager import SubAgentRun, SubAgentTask
from application.subagents.permissions import SubAgentPermissionSpec, to_jsonable


class MapReduceStrategy:
    """执行 map_reduce 编排，组织 helper 与子 agent 生命周期。"""

    mode = "map_reduce"

    def __init__(self, manager: Any) -> None:
        """初始化策略，输入为 AgentWorkflowManager facade，输出为可注册策略实例。"""
        self._manager = manager

    def catalog_entry(self) -> WorkflowStrategyCatalogEntry:
        """生成策略目录项，输入为当前策略说明，输出为父 agent 可查看的紧凑条目。"""
        return self.describe().catalog_entry()

    def describe(self) -> WorkflowStrategyDescription:
        """生成中文策略说明，输入为当前策略配置，输出为 payload 生成说明。"""
        return WorkflowStrategyDescription(
            mode=self.mode,
            title="Map-Reduce 代码分析",
            status="available",
            runnable=True,
            summary="把大工程代码分析拆成稳定分片，派发 mapper 子 agent，并用确定性 reducer 合并 code_findings。",
            when_to_use=(
                "输入文件数量较多且每个分片可以独立分析",
                "mapper 输出可以统一为 code_findings JSON 契约",
                "最终结果需要去重、排序、覆盖率和失败 shard 汇总",
            ),
            warnings=(
                "v0.1 支持 path_glob 和 file_list 输入来源",
                "v0.1 reducer 使用确定性合并逻辑",
                "xcodeatlas、dependency_graph 和 live smoke 进入后续 task",
            ),
            inputs=(
                WorkflowStrategyInputField(
                    name="objective",
                    required=True,
                    type_label="string",
                    description="本次代码分析目标。",
                    example="检查 agent workflow runtime 的边界风险。",
                ),
                WorkflowStrategyInputField(
                    name="input_source",
                    required=True,
                    type_label="object",
                    description="输入来源，v0.1 支持 path_glob 和 file_list。",
                    example={
                        "kind": "path_glob",
                        "root_dir": ".",
                        "include": ["src/**/*.py"],
                        "exclude": [".venv/**"],
                        "files": [],
                        "index_provider": "rg",
                        "input_digest": None,
                    },
                ),
                WorkflowStrategyInputField(
                    name="shard_strategy",
                    required=True,
                    type_label="object",
                    description="分片策略，v0.1 支持 by_file_count 和 by_directory。",
                    example={
                        "kind": "by_file_count",
                        "max_files_per_shard": 8,
                        "max_estimated_tokens_per_shard": 20000,
                        "min_shards": 1,
                        "max_shards": 12,
                        "preserve_directory_boundary": True,
                        "prefer_dependency_cohesion": False,
                    },
                ),
            ),
            outputs=(
                "AgentWorkflowResult",
                "root result.json",
                "reports/index.json",
                "map_reduce/reducer/result.json",
            ),
            examples=(
                {
                    "mode": "map_reduce",
                    "payload": {
                        "mode": "map_reduce",
                        "objective": "检查 workflow runtime 风险",
                        "input_source": {
                            "kind": "path_glob",
                            "root_dir": ".",
                            "include": ["src/runtime_assembly/**/*.py"],
                            "exclude": [".venv/**"],
                            "files": [],
                            "index_provider": "rg",
                            "input_digest": None,
                        },
                        "output_contract": "code_findings",
                    },
                },
            ),
        )

    async def run(
        self,
        context: WorkflowExecutionContext,
        payload: Mapping[str, object],
    ) -> Any:
        """执行 map_reduce，输入为 workflow 上下文和 JSON payload，输出为 AgentWorkflowResult。"""
        spec = parse_map_reduce_workflow_spec(payload)
        _validate_runtime_limits(spec)
        workflow_deadline = asyncio.get_running_loop().time() + spec.limits.workflow_timeout_seconds
        context.audit_writer.write_event(
            {
                "action": "map_reduce_started",
                "payload": {
                    "workflow_id": context.workflow_id,
                    "mode": self.mode,
                    "output_contract": spec.output_contract,
                    "audit_tags": spec.audit_tags,
                },
            }
        )

        planner = MapReducePlanner(workspace_root=self._manager.workspace_root)
        shards = planner.plan(spec)
        context.audit_writer.write_event(
            {
                "action": "map_reduce_shards_planned",
                "payload": {
                    "workflow_id": context.workflow_id,
                    "mode": self.mode,
                    "output_contract": spec.output_contract,
                    "shard_count": len(shards),
                    "shards": [to_jsonable(shard) for shard in shards],
                },
            }
        )

        raw_tasks = [
            SubAgentTask(
                task_id=shard.shard_id,
                task_name=f"{spec.mapper.name_prefix}-{shard.display_order:03d}",
                prompt="map_reduce mapper prompt pending materialization",
                context=shard.context,
                tool_names=spec.mapper.tool_names,
                skill_names=spec.mapper.skill_names,
                permission=SubAgentPermissionSpec(mode="scoped_workdir"),
                metadata={
                    "map_reduce_shard_id": shard.shard_id,
                    "map_reduce_files": shard.files,
                    "map_reduce_display_order": shard.display_order,
                    "max_turns": spec.mapper.max_turns,
                },
            )
            for shard in shards
        ]
        assigned_tasks = self._manager.prepare_subagent_tasks(
            workflow_dir=context.workflow_dir,
            tasks=raw_tasks,
        )
        manifests = self._materialize_inputs(context, spec, shards, assigned_tasks)
        prompt_builder = MapperPromptBuilder()
        assigned_tasks = [
            replace(
                task,
                prompt=prompt_builder.build_from_spec(
                    spec=spec,
                    shard=shards[index],
                    manifest=manifests[shards[index].shard_id],
                ),
            )
            for index, task in enumerate(assigned_tasks)
        ]
        self._manager.write_workflow_manifest(
            context=context,
            tasks=assigned_tasks,
            status="running",
        )
        context.audit_writer.write_event(
            {
                "action": "map_reduce_inputs_materialized",
                "payload": {
                    "workflow_id": context.workflow_id,
                    "mode": self.mode,
                    "output_contract": spec.output_contract,
                    "manifest_count": len(manifests),
                },
            }
        )

        outcomes = await self._run_mapper_tasks(
            context=context,
            tasks=assigned_tasks,
            max_concurrency=spec.limits.max_concurrency,
            mapper_timeout_seconds=spec.limits.mapper_timeout_seconds,
            mapper_retries=spec.limits.mapper_retries,
            workflow_deadline=workflow_deadline,
        )
        runs = tuple(outcome.run for outcome in outcomes)
        reports = tuple(outcome.report for outcome in outcomes)

        validator = MapReduceMapperOutputValidator()
        valid_outputs: list[MapperOutputEnvelope] = []
        failed_shards = self._failed_mapper_runs(shards, runs)
        validation_results: list[MapperValidationResult] = []
        shard_by_id = {shard.shard_id: shard for shard in shards}
        for run in runs:
            if run.status != "completed":
                continue
            shard_id = str(run.task.metadata.get("map_reduce_shard_id", ""))
            if len(run.content) > spec.mapper.max_output_chars:
                failed_shards.append(
                    _oversized_output_failed_shard(
                        shard_by_id[shard_id], spec.mapper.max_output_chars
                    )
                )
                context.audit_writer.write_event(
                    {
                        "action": "map_mapper_output_rejected",
                        "payload": {
                            "workflow_id": context.workflow_id,
                            "mode": self.mode,
                            "output_contract": spec.output_contract,
                            "shard_id": shard_id,
                            "valid": False,
                            "errors": [
                                {
                                    "error_type": "output_too_large",
                                    "message": "mapper output exceeds mapper.max_output_chars",
                                    "actual_chars": len(run.content),
                                    "max_output_chars": spec.mapper.max_output_chars,
                                }
                            ],
                        },
                    }
                )
                continue
            validation = validator.validate(run.content, expected_shard_id=shard_id)
            validation_results.append(validation)
            if validation.valid and validation.output is not None:
                valid_outputs.append(validation.output)
                action = "map_mapper_output_validated"
            else:
                failed_shards.append(_validation_failed_shard(shard_by_id[shard_id], validation))
                action = "map_mapper_output_rejected"
            context.audit_writer.write_event(
                {
                    "action": action,
                    "payload": {
                        "workflow_id": context.workflow_id,
                        "mode": self.mode,
                        "output_contract": spec.output_contract,
                        "shard_id": shard_id,
                        "valid": validation.valid,
                        "errors": [to_jsonable(error) for error in validation.errors],
                    },
                }
            )

        context.audit_writer.write_event(
            {
                "action": "map_reduce_reducer_started",
                "payload": {
                    "workflow_id": context.workflow_id,
                    "mode": self.mode,
                    "output_contract": spec.output_contract,
                    "valid_output_count": len(valid_outputs),
                    "failed_shard_count": len(failed_shards),
                },
            }
        )
        try:
            reducer_output = await asyncio.wait_for(
                asyncio.to_thread(
                    lambda: MapReduceReducer().reduce(
                        workflow_id=context.workflow_id,
                        spec=spec,
                        shards=shards,
                        valid_outputs=tuple(valid_outputs),
                        failed_shards=tuple(failed_shards),
                    )
                ),
                timeout=min(
                    spec.limits.reducer_timeout_seconds,
                    _remaining_timeout(workflow_deadline),
                ),
            )
        except TimeoutError:
            reducer_output = _reducer_timeout_output(
                workflow_id=context.workflow_id,
                spec=spec,
                shards=shards,
                valid_outputs=tuple(valid_outputs),
                failed_shards=tuple(failed_shards),
            )
            context.audit_writer.write_event(
                {
                    "action": "map_reduce_reducer_failed",
                    "payload": {
                        "workflow_id": context.workflow_id,
                        "mode": self.mode,
                        "output_contract": spec.output_contract,
                        "reason": "reducer timed out",
                    },
                }
            )
        artifact_writer = MapReduceArtifactWriter(workflow_dir=context.workflow_dir)
        artifact_paths = artifact_writer.write_all(
            shards=shards,
            mapper_records=_mapper_artifact_records(
                shards=shards,
                runs=runs,
                reports=reports,
                validation_results=tuple(validation_results),
                failed_shards=tuple(failed_shards),
            ),
            reducer_output=reducer_output,
        )
        context.audit_writer.write_event(
            {
                "action": "map_reduce_reducer_completed",
                "payload": {
                    "workflow_id": context.workflow_id,
                    "mode": self.mode,
                    "output_contract": spec.output_contract,
                    "status": reducer_output.status,
                    "top_finding_count": len(reducer_output.top_findings),
                    "reducer_result_path": str(artifact_paths.reducer_result_path),
                },
            }
        )

        finished_at = _now_iso()
        completed = reducer_output.status == "completed"
        report_index_path = self._manager.write_report_index(
            context=context,
            status="completed" if completed else "partial",
            reports=reports,
        )
        extra = {
            "map_reduce": {
                "reducer_output": to_jsonable(reducer_output),
                "artifact_paths": to_jsonable(artifact_paths),
            }
        }
        self._manager.write_workflow_result(
            context=context,
            finished_at=finished_at,
            completed=completed,
            report_index_path=report_index_path,
            reports=reports,
            runs=runs,
            extra=extra,
        )
        self._manager.write_workflow_manifest(
            context=context,
            tasks=assigned_tasks,
            status="completed" if completed else "partial",
            finished_at=finished_at,
        )
        context.audit_writer.write_event(
            {
                "action": "map_reduce_completed",
                "payload": {
                    "workflow_id": context.workflow_id,
                    "mode": self.mode,
                    "output_contract": spec.output_contract,
                    "completed": completed,
                    "report_index_path": str(report_index_path),
                },
            }
        )

        from application.agent_workflows.manager import AgentWorkflowResult

        return AgentWorkflowResult(
            workflow_id=context.workflow_id,
            mode=self.mode,
            parent_session_id=context.parent_session_id,
            workflow_dir=context.workflow_dir,
            started_at=context.started_at,
            finished_at=finished_at,
            runs=runs,
            reports=reports,
            report_index_path=report_index_path,
            data=extra,
            completed_override=completed,
        )

    def _materialize_inputs(
        self,
        context: WorkflowExecutionContext,
        spec: Any,
        shards: tuple[Any, ...],
        tasks: list[SubAgentTask],
    ) -> dict[str, MapperInputManifest]:
        """物化 mapper 输入，输入为 shards 和已绑定任务，输出为 shard_id 到 manifest 的映射。"""
        materializer = MapperInputMaterializer(workspace_root=self._manager.workspace_root)
        manifests: dict[str, MapperInputManifest] = {}
        for shard, task in zip(shards, tasks, strict=True):
            task_run_id = task.metadata.get("task_run_id")
            if not isinstance(task_run_id, str) or not task_run_id:
                raise ValueError(f"map_reduce task missing task_run_id for shard {shard.shard_id}")
            manifests[shard.shard_id] = materializer.materialize(
                workflow_dir=context.workflow_dir,
                task_run_id=task_run_id,
                shard=shard,
                spec=spec,
            )
            context.audit_writer.write_event(
                {
                    "action": "map_mapper_input_materialized",
                    "payload": {
                        "workflow_id": context.workflow_id,
                        "mode": self.mode,
                        "output_contract": spec.output_contract,
                        "shard_id": shard.shard_id,
                        "task_run_id": task.metadata.get("task_run_id"),
                        "input_manifest_path": str(task.metadata.get("working_dir", ""))
                        + "/input_manifest.json",
                    },
                }
            )
        return manifests

    async def _run_mapper_tasks(
        self,
        *,
        context: WorkflowExecutionContext,
        tasks: list[SubAgentTask],
        max_concurrency: int,
        mapper_timeout_seconds: int,
        mapper_retries: int,
        workflow_deadline: float,
    ) -> tuple[Any, ...]:
        """按并发预算运行 mapper，输入为任务列表，输出为运行结果集合。"""
        semaphore = asyncio.Semaphore(max_concurrency)

        async def _run(index: int, task: SubAgentTask) -> Any:
            """运行单个 mapper，输入为序号和任务，输出为 _RunOutcome。"""
            async with semaphore:
                latest_outcome = None
                for attempt in range(mapper_retries + 1):
                    context.audit_writer.write_event(
                        {
                            "action": "map_mapper_started",
                            "payload": {
                                "workflow_id": context.workflow_id,
                                "mode": self.mode,
                                "shard_id": task.metadata.get("map_reduce_shard_id"),
                                "task_run_id": task.metadata.get("task_run_id"),
                                "attempt": attempt + 1,
                            },
                        }
                    )
                    try:
                        timeout_seconds = min(
                            mapper_timeout_seconds,
                            _remaining_timeout(workflow_deadline),
                        )
                    except TimeoutError:
                        outcome = self._manager.record_subagent_failure(
                            context=context,
                            task=task,
                            display_order=index,
                            error=TimeoutError(
                                "map_reduce workflow timed out before mapper could start"
                            ),
                            elapsed_ms=0,
                        )
                        context.audit_writer.write_event(
                            {
                                "action": "map_mapper_timeout",
                                "payload": {
                                    "workflow_id": context.workflow_id,
                                    "mode": self.mode,
                                    "shard_id": task.metadata.get("map_reduce_shard_id"),
                                    "task_run_id": task.metadata.get("task_run_id"),
                                    "attempt": attempt + 1,
                                    "timeout_seconds": 0,
                                    "timeout_scope": "workflow",
                                },
                            }
                        )
                        return outcome
                    timeout_scope = (
                        "workflow" if timeout_seconds < mapper_timeout_seconds else "mapper"
                    )
                    try:
                        outcome = await asyncio.wait_for(
                            self._manager.run_subagent_task(
                                context=context,
                                task=task,
                                display_order=index,
                            ),
                            timeout=timeout_seconds,
                        )
                    except TimeoutError:
                        outcome = self._manager.record_subagent_failure(
                            context=context,
                            task=task,
                            display_order=index,
                            error=TimeoutError(
                                _mapper_timeout_message(
                                    scope=timeout_scope,
                                    timeout_seconds=timeout_seconds,
                                )
                            ),
                            elapsed_ms=int(timeout_seconds * 1000),
                        )
                        context.audit_writer.write_event(
                            {
                                "action": "map_mapper_timeout",
                                "payload": {
                                    "workflow_id": context.workflow_id,
                                    "mode": self.mode,
                                    "shard_id": task.metadata.get("map_reduce_shard_id"),
                                    "task_run_id": task.metadata.get("task_run_id"),
                                    "attempt": attempt + 1,
                                    "timeout_seconds": timeout_seconds,
                                    "timeout_scope": timeout_scope,
                                },
                            }
                        )
                    latest_outcome = outcome
                    context.audit_writer.write_event(
                        {
                            "action": (
                                "map_mapper_completed"
                                if outcome.run.status == "completed"
                                else "map_mapper_failed"
                            ),
                            "payload": {
                                "workflow_id": context.workflow_id,
                                "mode": self.mode,
                                "shard_id": task.metadata.get("map_reduce_shard_id"),
                                "task_run_id": task.metadata.get("task_run_id"),
                                "status": outcome.run.status,
                                "report_path": outcome.report.report_path,
                                "attempt": attempt + 1,
                            },
                        }
                    )
                    if (
                        timeout_scope == "workflow"
                        or outcome.run.status == "completed"
                        or attempt >= mapper_retries
                    ):
                        return outcome
                    context.audit_writer.write_event(
                        {
                            "action": "map_mapper_retry_scheduled",
                            "payload": {
                                "workflow_id": context.workflow_id,
                                "mode": self.mode,
                                "shard_id": task.metadata.get("map_reduce_shard_id"),
                                "task_run_id": task.metadata.get("task_run_id"),
                                "next_attempt": attempt + 2,
                            },
                        }
                    )
                if latest_outcome is None:
                    raise RuntimeError("map_reduce mapper produced no run outcome")
                return latest_outcome

        return tuple(
            await asyncio.gather(*[_run(index, task) for index, task in enumerate(tasks, 1)])
        )

    def _failed_mapper_runs(
        self,
        shards: tuple[Any, ...],
        runs: tuple[SubAgentRun, ...],
    ) -> list[FailedShardReport]:
        """收集 mapper 失败分片，输入为 shards 和 runs，输出为 FailedShardReport 列表。"""
        shard_by_id = {shard.shard_id: shard for shard in shards}
        failed: list[FailedShardReport] = []
        for run in runs:
            if run.status == "completed":
                continue
            shard_id = str(run.task.metadata.get("map_reduce_shard_id", ""))
            shard = shard_by_id[shard_id]
            failed.append(
                FailedShardReport(
                    shard_id=shard.shard_id,
                    shard_name=shard.shard_name,
                    failed_stage="mapper",
                    reason=run.error_message or "mapper run failed",
                    retryable=True,
                    retry_hint=f"重新运行 shard {shard.shard_id}",
                )
            )
        return failed


def _validate_runtime_limits(spec: Any) -> None:
    """校验运行期限制，输入为 workflow spec，输出为通过或明确异常。"""
    if spec.limits.validation_repair_retries:
        raise ValueError("map_reduce v0.1 does not support validation_repair_retries; set it to 0")
    if spec.mapper.max_turns < 1:
        raise ValueError("mapper.max_turns must be >= 1")
    if spec.mapper.max_output_chars < 1:
        raise ValueError("mapper.max_output_chars must be >= 1")


def _remaining_timeout(deadline: float) -> float:
    """计算 workflow 剩余秒数，输入为事件循环 deadline，输出为正数 timeout。"""
    remaining = deadline - asyncio.get_running_loop().time()
    if remaining <= 0:
        raise TimeoutError("map_reduce workflow timed out before next stage")
    return remaining


def _oversized_output_failed_shard(shard: Any, max_output_chars: int) -> FailedShardReport:
    """构造 mapper 输出过大报告，输入为 shard 和字符上限，输出为 FailedShardReport。"""
    return FailedShardReport(
        shard_id=shard.shard_id,
        shard_name=shard.shard_name,
        failed_stage="validation",
        reason=f"mapper output exceeds mapper.max_output_chars={max_output_chars}",
        retryable=True,
        retry_hint=f"缩短 mapper 输出后重新运行 shard {shard.shard_id}",
    )


def _validation_failed_shard(
    shard: Any,
    validation: MapperValidationResult,
) -> FailedShardReport:
    """构造校验失败报告，输入为 shard 和校验结果，输出为 FailedShardReport。"""
    reason = "; ".join(error.message for error in validation.errors) or "mapper output invalid"
    return FailedShardReport(
        shard_id=shard.shard_id,
        shard_name=shard.shard_name,
        failed_stage="validation",
        reason=reason,
        retryable=any(error.retryable for error in validation.errors),
        retry_hint=f"修正 mapper 输出后重新运行 shard {shard.shard_id}",
    )


def _mapper_timeout_message(*, scope: str, timeout_seconds: float) -> str:
    """生成 mapper 超时说明，输入为超时范围和秒数，输出为错误消息。"""
    if scope == "workflow":
        return f"map_reduce workflow timed out during mapper after {timeout_seconds:.3f}s"
    return f"map_reduce mapper timed out after {timeout_seconds:.3f}s"


def _reducer_timeout_output(
    *,
    workflow_id: str,
    spec: Any,
    shards: tuple[Any, ...],
    valid_outputs: tuple[MapperOutputEnvelope, ...],
    failed_shards: tuple[FailedShardReport, ...],
) -> ReducerOutput:
    """构造 reducer 超时输出，输入为 mapper 结果，输出为 failed ReducerOutput。"""
    reducer_failed = FailedShardReport(
        shard_id="__reducer__",
        shard_name="global-reducer",
        failed_stage="reducer",
        reason="reducer timed out",
        retryable=True,
        retry_hint="重新运行 reducer 或调高 limits.reducer_timeout_seconds",
    )
    all_failed = (*failed_shards, reducer_failed)
    per_shard = tuple(output.coverage for output in valid_outputs)
    total_assigned = sum(coverage.files_assigned for coverage in per_shard)
    total_seen = sum(coverage.files_seen_count for coverage in per_shard)
    total_symbols = sum(coverage.symbols_seen_count for coverage in per_shard)
    return ReducerOutput(
        status="failed",
        workflow_id=workflow_id,
        output_contract=spec.output_contract,
        total_shards=len(shards),
        completed_shards=len({output.shard_id for output in valid_outputs}),
        failed_shards=len(all_failed),
        deduped_findings=(),
        top_findings=(),
        coverage_summary=CoverageSummary(
            total_files_assigned=total_assigned,
            total_files_seen=total_seen,
            total_symbols_seen=total_symbols,
            per_shard=per_shard,
            notes=(
                f"reducer timed out after mapper completed "
                f"{len(valid_outputs)}/{len(shards)} shards."
            ),
        ),
        failed_shard_reports=all_failed,
        followups=("重新运行 reducer 或调高 limits.reducer_timeout_seconds",),
        reduced_at=_now_iso(),
    )


def _mapper_artifact_records(
    *,
    shards: tuple[Any, ...],
    runs: tuple[SubAgentRun, ...],
    reports: tuple[Any, ...],
    validation_results: tuple[MapperValidationResult, ...],
    failed_shards: tuple[FailedShardReport, ...],
) -> tuple[dict[str, object], ...]:
    """生成 mapper 索引记录，输入为 shard/run/report/校验结果，输出为每个 shard 的摘要。"""
    run_by_shard_id = {str(run.task.metadata.get("map_reduce_shard_id", "")): run for run in runs}
    report_by_task_id = {report.task_id: report for report in reports}
    validation_by_expected_shard_id = {
        result.expected_shard_id: result for result in validation_results
    }
    failed_by_shard_id: dict[str, list[FailedShardReport]] = {}
    for failed in failed_shards:
        failed_by_shard_id.setdefault(failed.shard_id, []).append(failed)

    records: list[dict[str, object]] = []
    for shard in shards:
        run = run_by_shard_id.get(shard.shard_id)
        report = report_by_task_id.get(shard.shard_id)
        validation = validation_by_expected_shard_id.get(shard.shard_id)
        records.append(
            {
                "shard_id": shard.shard_id,
                "shard_name": shard.shard_name,
                "display_order": shard.display_order,
                "task_run_id": (run.task.metadata.get("task_run_id") if run is not None else None),
                "run_status": run.status if run is not None else "missing",
                "error_message": run.error_message if run is not None else "mapper did not run",
                "report_path": report.report_path if report is not None else None,
                "validation_valid": validation.valid if validation is not None else False,
                "validation_error_count": (len(validation.errors) if validation is not None else 0),
                "raw_content_digest": (
                    validation.raw_content_digest if validation is not None else None
                ),
                "output_status": (
                    validation.output.status
                    if validation is not None and validation.output is not None
                    else None
                ),
                "failed_reports": to_jsonable(tuple(failed_by_shard_id.get(shard.shard_id, ()))),
            }
        )
    return tuple(records)


def _now_iso() -> str:
    """生成 UTC 时间字符串，输入为空，输出为 ISO 8601。"""
    return datetime.now(UTC).isoformat()


__all__ = ["MapReduceStrategy"]
