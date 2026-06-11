"""roundtable_review 工作流策略状态机。

本脚本实现 Multi-Agent Roundtable Review（多 Agent 圆桌评审）。
作用是以 mode=roundtable_review 接收模块设计 review payload，创建共享 ReviewBoard，
并按“独立评审 -> 交叉质询 -> 仲裁总结”三阶段调度子 agent。
关键执行流程：解析 payload，收集并物化输入，启动 reviewer 首轮并行分析，写 claims，
按轮次启动 rebuttal，写 rebuttals 和 consensus，最后启动 arbiter 或确定性 fallback 写 final_report。
关键函数：RoundtableReviewStrategy.run 执行主流程，_run_stage_tasks 执行并发子 agent，
_claims_from_runs/_comments_from_runs 提取结构化观点。
"""

from __future__ import annotations

import asyncio
import json
import re
from collections.abc import Mapping
from typing import Any

from application.agent_workflows.context import WorkflowExecutionContext
from application.agent_workflows.strategies.description import (
    WorkflowStrategyCatalogEntry,
    WorkflowStrategyDescription,
    WorkflowStrategyInputField,
)
from application.agent_workflows.strategies.roundtable_review.board import (
    ReviewBoardWriter,
    collect_source_files,
    materialize_for_task,
)
from application.agent_workflows.strategies.roundtable_review.contracts import (
    ReviewClaimRecord,
    ReviewCommentRecord,
    ReviewerSpec,
    RoundtableReviewSpec,
    estimate_tokens,
    normalize_comment_type,
    normalize_severity,
    parse_roundtable_review_spec,
)
from application.agent_workflows.strategies.roundtable_review.prompts import (
    build_arbiter_prompt,
    build_independent_prompt,
    build_rebuttal_prompt,
)
from application.subagents.manager import SubAgentRun, SubAgentTask
from application.subagents.permissions import SubAgentPermissionSpec


class RoundtableReviewStrategy:
    """执行共享白板辩论式多 Agent 代码设计评审。"""

    mode = "roundtable_review"

    def __init__(self, manager: Any) -> None:
        """初始化策略，输入为 AgentWorkflowManager，输出为可运行策略实例。"""
        self._manager = manager

    def catalog_entry(self) -> WorkflowStrategyCatalogEntry:
        """生成策略目录项，输入为当前策略说明，输出为紧凑条目。"""
        return self.describe().catalog_entry()

    def describe(self) -> WorkflowStrategyDescription:
        """生成中文策略说明，输入为当前策略配置，输出为 payload 生成参考。"""
        return WorkflowStrategyDescription(
            mode=self.mode,
            title="多 Agent 圆桌评审",
            status="available",
            runnable=True,
            summary="按 participants.select 选择子 agent 角色，并行审查代码模块设计，通过共享 ReviewBoard 进行质询和仲裁。",
            when_to_use=(
                "代码模块设计、架构边界、测试策略、性能和稳定性需要多视角审查",
                "评审结论需要绑定源码、文档、commit 或行号证据",
                "需要输出共识、分歧、风险和可交给开发 agent 的任务清单",
            ),
            warnings=(
                "输入范围过大时应限制 input_source.max_files 或先用 map_reduce 做初筛",
                "讨论轮次越多，子 agent 总预算消耗越快",
                "最终报告质量依赖 reviewer 输出的证据完整度",
            ),
            inputs=(
                WorkflowStrategyInputField(
                    name="topic",
                    required=True,
                    type_label="string",
                    description="本次圆桌评审主题。",
                    example="Session 模块设计是否合理",
                ),
                WorkflowStrategyInputField(
                    name="input_source",
                    required=True,
                    type_label="object",
                    description="评审输入范围，支持 paths 和 include glob。",
                    example={
                        "root_dir": ".",
                        "paths": ["src/sessions", "docs/modules/会话/README.md"],
                        "include": [],
                        "exclude": [".venv/**", "__pycache__/**"],
                        "max_files": 80,
                        "max_bytes_per_file": 80000,
                    },
                ),
                WorkflowStrategyInputField(
                    name="participants",
                    required=True,
                    type_label="object",
                    description="子 agent 角色选择，只支持 select 数组。",
                    example={
                        "select": ["architecture_reviewer", "test_reviewer"],
                    },
                ),
                WorkflowStrategyInputField(
                    name="limits",
                    required=False,
                    type_label="object",
                    description="子 agent 总预算、讨论轮次和超时控制。",
                    example={
                        "total_child_token_budget": 50000,
                        "discussion_rounds": 2,
                        "max_discussion_rounds": 6,
                        "max_concurrency": 5,
                    },
                ),
            ),
            outputs=(
                "AgentWorkflowResult",
                "review_board/context.md",
                "review_board/sources.md",
                "review_board/claims.jsonl",
                "review_board/rebuttals.jsonl",
                "review_board/consensus.md",
                "review_board/final_report.md",
            ),
            examples=(
                {
                    "mode": "roundtable_review",
                    "payload": {
                        "topic": "Session 模块设计是否合理",
                        "participants": {
                            "select": ["architecture_reviewer", "test_reviewer"],
                        },
                        "input_source": {
                            "root_dir": ".",
                            "paths": ["src/sessions"],
                            "include": ["tests/unit/test_session*.py"],
                            "exclude": [".venv/**", "__pycache__/**"],
                        },
                        "limits": {
                            "total_child_token_budget": 50000,
                            "discussion_rounds": 2,
                            "max_discussion_rounds": 6,
                        },
                    },
                },
            ),
        )

    async def run(
        self,
        context: WorkflowExecutionContext,
        payload: Mapping[str, object],
    ) -> Any:
        """执行 roundtable_review，输入为 workflow context 和 payload，输出为 AgentWorkflowResult。"""
        spec = parse_roundtable_review_spec(payload, role_manager=self._manager.role_manager)
        role_snapshot_path = self._manager.role_manager.write_workflow_snapshot(
            context.workflow_dir,
            self._manager.role_manager.resolve_participants(
                tuple(reviewer.agent_id for reviewer in spec.reviewers)
            ),
        )
        board = ReviewBoardWriter(workflow_dir=context.workflow_dir)
        input_root, source_records, source_paths = collect_source_files(
            workspace_root=self._manager.workspace_root,
            spec=spec,
        )
        if not source_records:
            raise ValueError("roundtable_review found no source files")

        board.write_context(spec=spec, workflow_id=context.workflow_id)
        board.write_sources(source_records)
        context.audit_writer.write_event(
            {
                "action": "roundtable_review_started",
                "payload": {
                    "workflow_id": context.workflow_id,
                    "mode": self.mode,
                    "topic": spec.topic,
                    "reviewer_count": len(spec.reviewers),
                    "source_file_count": len(source_records),
                    "discussion_rounds": spec.limits.discussion_rounds,
                    "total_child_token_budget": spec.limits.total_child_token_budget,
                    "audit_tags": spec.audit_tags,
                },
            }
        )

        all_tasks: list[SubAgentTask] = []
        all_runs: list[SubAgentRun] = []
        all_reports: list[Any] = []
        claims: tuple[ReviewClaimRecord, ...] = ()
        comments: tuple[ReviewCommentRecord, ...] = ()
        used_budget = 0

        independent_tasks = self._build_independent_tasks(spec)
        independent_tasks = self._manager.prepare_subagent_tasks(
            workflow_dir=context.workflow_dir,
            tasks=independent_tasks,
        )
        self._materialize_tasks(
            tasks=independent_tasks,
            input_root=input_root,
            source_paths=source_paths,
            source_records=source_records,
            board_snapshot=board.snapshot_text(claims=claims, comments=comments),
            max_bytes_per_file=spec.input_source.max_bytes_per_file,
        )
        all_tasks.extend(independent_tasks)
        self._manager.write_workflow_manifest(context=context, tasks=all_tasks, status="running")
        independent_outcomes = await self._run_stage_tasks(
            context=context,
            tasks=independent_tasks,
            stage="independent",
            max_concurrency=spec.limits.max_concurrency,
            timeout_seconds=spec.limits.agent_timeout_seconds,
        )
        all_runs.extend(outcome.run for outcome in independent_outcomes)
        all_reports.extend(outcome.report for outcome in independent_outcomes)
        used_budget += _estimated_run_tokens(tuple(outcome.run for outcome in independent_outcomes))
        claims = self._claims_from_runs(tuple(outcome.run for outcome in independent_outcomes))
        board.append_claims(claims)
        context.audit_writer.write_event(
            {
                "action": "roundtable_review_claims_recorded",
                "payload": {
                    "workflow_id": context.workflow_id,
                    "claim_count": len(claims),
                    "estimated_child_output_tokens": used_budget,
                },
            }
        )

        next_comment_index = 1
        for round_index in range(2, spec.limits.discussion_rounds + 1):
            if used_budget >= spec.limits.total_child_token_budget:
                context.audit_writer.write_event(
                    {
                        "action": "roundtable_review_budget_exhausted",
                        "payload": {
                            "workflow_id": context.workflow_id,
                            "round_index": round_index,
                            "estimated_child_output_tokens": used_budget,
                            "total_child_token_budget": spec.limits.total_child_token_budget,
                        },
                    }
                )
                break
            rebuttal_tasks = self._build_rebuttal_tasks(
                spec,
                round_index=round_index,
                remaining_budget=spec.limits.total_child_token_budget - used_budget,
            )
            rebuttal_tasks = self._manager.prepare_subagent_tasks(
                workflow_dir=context.workflow_dir,
                tasks=rebuttal_tasks,
            )
            self._materialize_tasks(
                tasks=rebuttal_tasks,
                input_root=input_root,
                source_paths=source_paths,
                source_records=source_records,
                board_snapshot=board.snapshot_text(claims=claims, comments=comments),
                max_bytes_per_file=spec.input_source.max_bytes_per_file,
            )
            all_tasks.extend(rebuttal_tasks)
            self._manager.write_workflow_manifest(
                context=context, tasks=all_tasks, status="running"
            )
            rebuttal_outcomes = await self._run_stage_tasks(
                context=context,
                tasks=rebuttal_tasks,
                stage=f"rebuttal-{round_index}",
                max_concurrency=spec.limits.max_concurrency,
                timeout_seconds=spec.limits.agent_timeout_seconds,
            )
            all_runs.extend(outcome.run for outcome in rebuttal_outcomes)
            all_reports.extend(outcome.report for outcome in rebuttal_outcomes)
            used_budget += _estimated_run_tokens(
                tuple(outcome.run for outcome in rebuttal_outcomes)
            )
            round_comments = self._comments_from_runs(
                tuple(outcome.run for outcome in rebuttal_outcomes),
                round_index=round_index,
                start_index=next_comment_index,
            )
            next_comment_index += len(round_comments)
            comments = (*comments, *round_comments)
            board.append_comments(round_comments)
            context.audit_writer.write_event(
                {
                    "action": "roundtable_review_rebuttals_recorded",
                    "payload": {
                        "workflow_id": context.workflow_id,
                        "round_index": round_index,
                        "comment_count": len(round_comments),
                        "estimated_child_output_tokens": used_budget,
                    },
                }
            )

        board.write_consensus(claims=claims, comments=comments)
        final_report_path, final_report_tokens = await self._write_final_report(
            context=context,
            spec=spec,
            board=board,
            input_root=input_root,
            source_paths=source_paths,
            source_records=source_records,
            claims=claims,
            comments=comments,
            used_budget=used_budget,
            all_tasks=all_tasks,
            all_runs=all_runs,
            all_reports=all_reports,
        )
        used_budget += final_report_tokens

        runs = tuple(all_runs)
        reports = tuple(all_reports)
        finished_at = _now_iso()
        completed = final_report_path.exists() and any(
            run.status == "completed"
            for run in runs
            if run.task.metadata.get("roundtable_stage") == "independent"
        )
        report_index_path = self._manager.write_report_index(
            context=context,
            status="completed" if completed else "partial",
            reports=reports,
        )
        extra = {
            "roundtable_review": {
                "topic": spec.topic,
                "claim_count": len(claims),
                "rebuttal_count": len(comments),
                "estimated_child_output_tokens": used_budget,
                "total_child_token_budget": spec.limits.total_child_token_budget,
                "review_board": {
                    "context_path": str(board.paths.context_path),
                    "sources_path": str(board.paths.sources_path),
                    "claims_path": str(board.paths.claims_path),
                    "rebuttals_path": str(board.paths.rebuttals_path),
                    "consensus_path": str(board.paths.consensus_path),
                    "final_report_path": str(board.paths.final_report_path),
                },
                "role_snapshot_path": str(role_snapshot_path),
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
            tasks=all_tasks,
            status="completed" if completed else "partial",
            finished_at=finished_at,
        )
        context.audit_writer.write_event(
            {
                "action": "roundtable_review_completed",
                "payload": {
                    "workflow_id": context.workflow_id,
                    "completed": completed,
                    "final_report_path": str(final_report_path),
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

    def _build_independent_tasks(self, spec: RoundtableReviewSpec) -> list[SubAgentTask]:
        """构造首轮 reviewer 任务，输入为 spec，输出为子任务列表。"""
        per_agent_budget = _per_agent_budget(spec, agent_count=len(spec.reviewers) + 1)
        return [
            SubAgentTask(
                task_id=f"ind-{_short_agent_id(reviewer.agent_id)}",
                task_name=f"independent-{reviewer.agent_id}",
                prompt=build_independent_prompt(
                    spec=spec,
                    reviewer=reviewer,
                    per_agent_token_budget=per_agent_budget,
                ),
                tool_names=("read_file", "list_dir"),
                permission=SubAgentPermissionSpec(mode="scoped_workdir"),
                metadata={
                    "roundtable_stage": "independent",
                    "roundtable_agent": reviewer.agent_id,
                    "max_turns": spec.limits.reviewer_max_turns,
                },
            )
            for reviewer in spec.reviewers
        ]

    def _build_rebuttal_tasks(
        self,
        spec: RoundtableReviewSpec,
        *,
        round_index: int,
        remaining_budget: int,
    ) -> list[SubAgentTask]:
        """构造交叉质询任务，输入为 spec/轮次/预算，输出为子任务列表。"""
        per_agent_budget = max(500, remaining_budget // (len(spec.reviewers) + 1))
        return [
            SubAgentTask(
                task_id=f"r{round_index:03d}-{_short_agent_id(reviewer.agent_id)}",
                task_name=f"round-{round_index:03d}-{reviewer.agent_id}",
                prompt=build_rebuttal_prompt(
                    spec=spec,
                    reviewer=reviewer,
                    round_index=round_index,
                    per_agent_token_budget=per_agent_budget,
                ),
                tool_names=("read_file", "list_dir"),
                permission=SubAgentPermissionSpec(mode="scoped_workdir"),
                metadata={
                    "roundtable_stage": "rebuttal",
                    "roundtable_round": round_index,
                    "roundtable_agent": reviewer.agent_id,
                    "max_turns": spec.limits.reviewer_max_turns,
                },
            )
            for reviewer in spec.reviewers
        ]

    async def _write_final_report(
        self,
        *,
        context: WorkflowExecutionContext,
        spec: RoundtableReviewSpec,
        board: ReviewBoardWriter,
        input_root: Any,
        source_paths: tuple[Any, ...],
        source_records: tuple[Any, ...],
        claims: tuple[ReviewClaimRecord, ...],
        comments: tuple[ReviewCommentRecord, ...],
        used_budget: int,
        all_tasks: list[SubAgentTask],
        all_runs: list[SubAgentRun],
        all_reports: list[Any],
    ) -> tuple[Any, int]:
        """写入最终报告，输入为白板和累计状态，输出为 final_report 路径和新增 token。"""
        remaining = spec.limits.total_child_token_budget - used_budget
        if remaining <= 0:
            path = board.write_final_report(
                _fallback_report(
                    spec=spec,
                    claims=claims,
                    comments=comments,
                    reason="child token budget exhausted",
                )
            )
            return path, 0
        arbiter = ReviewerSpec(
            agent_id="arbiter_agent",
            title="Arbiter Agent",
            focus="共识、分歧、风险和修改建议仲裁",
            instructions="汇总 Roundtable Review 结论。",
        )
        task = SubAgentTask(
            task_id="arbiter",
            task_name="arbiter-agent",
            prompt=build_arbiter_prompt(spec=spec, per_agent_token_budget=max(1000, remaining)),
            tool_names=("read_file", "list_dir"),
            permission=SubAgentPermissionSpec(mode="scoped_workdir"),
            metadata={
                "roundtable_stage": "arbiter",
                "roundtable_agent": arbiter.agent_id,
                "max_turns": spec.limits.arbiter_max_turns,
            },
        )
        assigned = self._manager.prepare_subagent_tasks(
            workflow_dir=context.workflow_dir,
            tasks=[task],
        )
        self._materialize_tasks(
            tasks=assigned,
            input_root=input_root,
            source_paths=source_paths,
            source_records=source_records,
            board_snapshot=board.snapshot_text(claims=claims, comments=comments),
            max_bytes_per_file=spec.input_source.max_bytes_per_file,
        )
        all_tasks.extend(assigned)
        self._manager.write_workflow_manifest(context=context, tasks=all_tasks, status="running")
        outcomes = await self._run_stage_tasks(
            context=context,
            tasks=assigned,
            stage="arbiter",
            max_concurrency=1,
            timeout_seconds=spec.limits.agent_timeout_seconds,
        )
        all_runs.extend(outcome.run for outcome in outcomes)
        all_reports.extend(outcome.report for outcome in outcomes)
        run = outcomes[0].run
        if run.status == "completed" and run.content.strip():
            return board.write_final_report(run.content), estimate_tokens(run.content)
        path = board.write_final_report(
            _fallback_report(
                spec=spec,
                claims=claims,
                comments=comments,
                reason="arbiter did not complete",
            )
        )
        return path, estimate_tokens(run.content)

    def _materialize_tasks(
        self,
        *,
        tasks: list[SubAgentTask],
        input_root: Any,
        source_paths: tuple[Any, ...],
        source_records: tuple[Any, ...],
        board_snapshot: str,
        max_bytes_per_file: int,
    ) -> None:
        """为一组子任务物化输入，输入为任务列表和白板快照，输出为工作目录文件。"""
        for task in tasks:
            materialize_for_task(
                task=task,
                input_root=input_root,
                source_paths=source_paths,
                source_records=source_records,
                board_snapshot=board_snapshot,
                max_bytes_per_file=max_bytes_per_file,
            )

    async def _run_stage_tasks(
        self,
        *,
        context: WorkflowExecutionContext,
        tasks: list[SubAgentTask],
        stage: str,
        max_concurrency: int,
        timeout_seconds: int,
    ) -> tuple[Any, ...]:
        """按并发限制运行阶段任务，输入为任务列表，输出为 outcome 集合。"""
        semaphore = asyncio.Semaphore(max(1, max_concurrency))

        async def _run(index: int, task: SubAgentTask) -> Any:
            """运行单个任务，输入为序号和任务，输出为 outcome。"""
            async with semaphore:
                context.audit_writer.write_event(
                    {
                        "action": "roundtable_review_agent_started",
                        "payload": {
                            "workflow_id": context.workflow_id,
                            "stage": stage,
                            "task_id": task.task_id,
                            "task_name": task.task_name,
                        },
                    }
                )
                try:
                    return await asyncio.wait_for(
                        self._manager.run_subagent_task(
                            context=context,
                            task=task,
                            display_order=index,
                        ),
                        timeout=timeout_seconds,
                    )
                except TimeoutError as exc:
                    return self._manager.record_subagent_failure(
                        context=context,
                        task=task,
                        display_order=index,
                        error=exc,
                        elapsed_ms=timeout_seconds * 1000,
                    )

        return tuple(
            await asyncio.gather(*[_run(index, task) for index, task in enumerate(tasks, 1)])
        )

    def _claims_from_runs(self, runs: tuple[SubAgentRun, ...]) -> tuple[ReviewClaimRecord, ...]:
        """从 reviewer 输出提取 claims，输入为 runs，输出为 claim 记录。"""
        claims: list[ReviewClaimRecord] = []
        for run in runs:
            agent = _agent_id(run)
            payload = _extract_json_object(run.content)
            findings = payload.get("findings") if isinstance(payload, dict) else None
            if not isinstance(findings, list):
                continue
            for item in findings:
                if not isinstance(item, Mapping):
                    continue
                claim_text = _string_value(item.get("claim"))
                if not claim_text:
                    continue
                claims.append(
                    ReviewClaimRecord(
                        claim_id=f"C-{len(claims) + 1:03d}",
                        agent=agent,
                        severity=normalize_severity(item.get("severity")),
                        claim=claim_text,
                        evidence=tuple(_list_value(item.get("evidence"))),
                        risk=_string_value(item.get("risk")),
                        suggestion=_string_value(item.get("suggestion")),
                        confidence=_confidence(item.get("confidence")),
                        raw=dict(item),
                    )
                )
        return tuple(claims)

    def _comments_from_runs(
        self,
        runs: tuple[SubAgentRun, ...],
        *,
        round_index: int,
        start_index: int,
    ) -> tuple[ReviewCommentRecord, ...]:
        """从 reviewer 输出提取 rebuttals，输入为 runs/轮次，输出为评论记录。"""
        comments: list[ReviewCommentRecord] = []
        for run in runs:
            agent = _agent_id(run)
            payload = _extract_json_object(run.content)
            raw_comments = payload.get("comments") if isinstance(payload, dict) else None
            if not isinstance(raw_comments, list):
                continue
            for item in raw_comments:
                if not isinstance(item, Mapping):
                    continue
                target = _string_value(item.get("target_claim_id"))
                comment_text = _string_value(item.get("comment"))
                if not target or not comment_text:
                    continue
                comments.append(
                    ReviewCommentRecord(
                        comment_id=f"R-{start_index + len(comments):03d}",
                        agent=agent,
                        round_index=round_index,
                        comment_type=normalize_comment_type(item.get("type")),
                        target_claim_id=target,
                        comment=comment_text,
                        evidence=tuple(_list_value(item.get("evidence"))),
                        severity_adjustment=_nullable_string(item.get("severity_adjustment")),
                        confidence=_confidence(item.get("confidence")),
                        raw=dict(item),
                    )
                )
        return tuple(comments)


def _per_agent_budget(spec: RoundtableReviewSpec, *, agent_count: int) -> int:
    """计算每个 agent 初始预算，输入为 spec 和 agent 数，输出为 token 估算。"""
    rounds = max(1, spec.limits.discussion_rounds)
    return max(500, spec.limits.total_child_token_budget // max(1, agent_count * rounds))


def _estimated_run_tokens(runs: tuple[SubAgentRun, ...]) -> int:
    """估算子 agent 输出 token，输入为 runs，输出为总估算。"""
    return sum(estimate_tokens(run.content) for run in runs)


def _extract_json_object(content: str) -> dict[str, Any]:
    """从模型输出中提取 JSON 对象，输入为原始文本，输出为 dict 或空 dict。"""
    stripped = content.strip()
    if not stripped:
        return {}
    for candidate in _json_candidates(stripped):
        try:
            parsed = json.loads(candidate)
        except json.JSONDecodeError:
            continue
        if isinstance(parsed, dict):
            return parsed
    return {}


def _json_candidates(content: str) -> list[str]:
    """生成 JSON 候选文本，输入为模型输出，输出为候选列表。"""
    candidates = [content]
    fenced = re.findall(r"```(?:json)?\s*(\{.*?\})\s*```", content, flags=re.DOTALL)
    candidates.extend(fenced)
    start = content.find("{")
    end = content.rfind("}")
    if start >= 0 and end > start:
        candidates.append(content[start : end + 1])
    return candidates


def _agent_id(run: SubAgentRun) -> str:
    """读取 agent id，输入为 run，输出为角色 ID。"""
    raw = run.task.metadata.get("roundtable_agent")
    return raw if isinstance(raw, str) and raw else run.task.task_id


def _short_agent_id(agent_id: str) -> str:
    """压缩 agent id，输入为完整角色 ID，输出为短 task_id 片段。"""
    mapping = {
        "architecture_reviewer": "arch",
        "code_quality_reviewer": "quality",
        "test_reviewer": "test",
        "performance_reviewer": "perf",
        "safety_stability_reviewer": "safety",
    }
    return mapping.get(agent_id, re.sub(r"[^A-Za-z0-9_-]+", "-", agent_id)[:16] or "agent")


def _string_value(value: Any) -> str:
    """读取字符串值，输入为任意值，输出为去空白字符串。"""
    return value.strip() if isinstance(value, str) else ""


def _nullable_string(value: Any) -> str | None:
    """读取可空字符串，输入为任意值，输出为字符串或 None。"""
    text = _string_value(value)
    return text or None


def _list_value(value: Any) -> list[Any]:
    """读取列表值，输入为任意值，输出为列表。"""
    return value if isinstance(value, list) else []


def _confidence(value: Any) -> float:
    """读取置信度，输入为任意值，输出为 0-1 浮点数。"""
    if isinstance(value, int | float) and not isinstance(value, bool):
        return max(0.0, min(1.0, float(value)))
    return 0.5


def _fallback_report(
    *,
    spec: RoundtableReviewSpec,
    claims: tuple[ReviewClaimRecord, ...],
    comments: tuple[ReviewCommentRecord, ...],
    reason: str,
) -> str:
    """生成确定性 fallback 报告，输入为白板记录和原因，输出为 Markdown。"""
    high = [claim for claim in claims if claim.severity in {"P0", "P1"}]
    disputed_ids = {
        comment.target_claim_id for comment in comments if comment.comment_type == "refute"
    }
    lines = [
        "# Roundtable Review Final Report",
        "",
        f"- topic: {spec.topic}",
        f"- fallback_reason: {reason}",
        "",
        "## 1. 共识问题",
        "",
    ]
    for claim in claims:
        if claim.claim_id not in disputed_ids:
            lines.append(f"- {claim.claim_id} [{claim.severity}] {claim.claim}")
    lines.extend(["", "## 2. 主要分歧", ""])
    for claim in claims:
        if claim.claim_id in disputed_ids:
            lines.append(f"- {claim.claim_id} [{claim.severity}] {claim.claim}")
    lines.extend(["", "## 3. 高优先级风险", ""])
    for claim in high:
        lines.append(f"- {claim.claim_id}: {claim.risk or claim.claim}")
    lines.extend(["", "## 4. 建议修改方案", ""])
    for claim in claims:
        if claim.suggestion:
            lines.append(f"- {claim.claim_id}: {claim.suggestion}")
    lines.extend(
        [
            "",
            "## 5. 需要人工确认的问题",
            "",
            "- Arbiter agent 未产出完整报告，需人工复核 fallback 结论。",
            "",
            "## 6. 可直接交给开发 Agent 的任务清单",
            "",
        ]
    )
    for claim in high[:10]:
        lines.append(f"- 修复 {claim.claim_id}: {claim.suggestion or claim.claim}")
    lines.append("")
    return "\n".join(lines)


def _now_iso() -> str:
    """生成当前 UTC 时间，输入为空，输出为 ISO 字符串。"""
    from datetime import UTC, datetime

    return datetime.now(UTC).isoformat()


__all__ = ["RoundtableReviewStrategy"]
