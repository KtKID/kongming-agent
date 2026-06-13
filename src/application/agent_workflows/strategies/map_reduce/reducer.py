"""MapReduce reducer 确定性归并实现。

本脚本负责把多个 mapper 的 `code_findings` 输出归并为最终 ReducerOutput。
作用是提供无模型参与的稳定 reducer：按 spec 去重、排序、截断重点 finding，并汇总覆盖率、失败 shard 和后续建议。
关键执行流程：reduce 接收 workflow、spec、shards、有效 mapper outputs 和失败 shard 报告，输出可写入 artifact 的 ReducerOutput。
关键函数：MapReduceReducer.reduce 执行主归并，_dedupe_findings 去重，_rank_findings 稳定排序，_build_coverage_summary 汇总覆盖率。
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import UTC, datetime

from application.agent_workflows.strategies.map_reduce.contracts import (
    CodeFinding,
    CoverageSummary,
    FailedShardReport,
    MapperOutputEnvelope,
    MapperStatus,
    MapReduceWorkflowSpec,
    MapShard,
    ReducerOutput,
)

_SEVERITY_RANK: dict[str, int] = {"P0": 0, "P1": 1, "P2": 2, "P3": 3}


class MapReduceReducer:
    """MapReduce code_findings reducer。"""

    def reduce(
        self,
        *,
        workflow_id: str,
        spec: MapReduceWorkflowSpec,
        shards: Sequence[MapShard],
        valid_outputs: Sequence[MapperOutputEnvelope],
        failed_shard_reports: Sequence[FailedShardReport] = (),
        failed_shards: Sequence[FailedShardReport] | None = None,
    ) -> ReducerOutput:
        """归并 mapper 输出，输入为 workflow 上下文和 shard 结果，输出 ReducerOutput。"""
        output_tuple = tuple(valid_outputs)
        report_source = failed_shard_reports if failed_shards is None else failed_shards
        failed_report_tuple = _sort_failed_shard_reports(report_source, shards)
        findings = tuple(finding for output in output_tuple for finding in output.findings)
        deduped_findings = _dedupe_findings(findings, strategy=spec.reducer.dedupe_strategy)
        ranked_findings = _rank_findings(deduped_findings, strategy=spec.reducer.ranking_strategy)
        max_findings = max(spec.reducer.max_findings, 0)
        included_failed_reports = failed_report_tuple if spec.reducer.include_failed_shards else ()

        return ReducerOutput(
            status=_reducer_status(output_tuple, failed_report_tuple),
            workflow_id=workflow_id,
            output_contract=spec.output_contract,
            total_shards=len(shards),
            completed_shards=len({output.shard_id for output in output_tuple}),
            failed_shards=len(failed_report_tuple),
            deduped_findings=ranked_findings,
            top_findings=ranked_findings[:max_findings],
            coverage_summary=_build_coverage_summary(output_tuple, shards, failed_report_tuple),
            failed_shard_reports=included_failed_reports,
            followups=_build_followups(
                total_findings=len(ranked_findings),
                max_findings=max_findings,
                coverage_outputs=output_tuple,
                failed_reports=failed_report_tuple,
                duplicate_count=len(findings) - len(deduped_findings),
            ),
            reduced_at=_utc_now_iso(),
        )


def _dedupe_findings(
    findings: Sequence[CodeFinding],
    *,
    strategy: str,
) -> tuple[CodeFinding, ...]:
    """按 reducer 策略去重，输入为 finding 序列，输出为保留首个稳定命中的 finding。"""
    seen: set[tuple[object, ...]] = set()
    deduped: list[CodeFinding] = []
    for finding in findings:
        key = _dedupe_key(finding, strategy=strategy)
        if key in seen:
            continue
        seen.add(key)
        deduped.append(finding)
    return tuple(deduped)


def _dedupe_key(finding: CodeFinding, *, strategy: str) -> tuple[object, ...]:
    """生成去重 key，输入为 finding 和策略，输出为可哈希 key。"""
    if strategy == "file_line_title":
        location = finding.locations[0] if finding.locations else None
        if location is not None:
            return (
                "file_line_title",
                _normalize_text(location.path),
                location.line_start,
                _normalize_text(finding.title),
            )
    return ("exact_dedupe_key", finding.dedupe_key)


def _rank_findings(
    findings: Sequence[CodeFinding],
    *,
    strategy: str,
) -> tuple[CodeFinding, ...]:
    """按 reducer 策略稳定排序，输入为去重 finding，输出为排序后的 finding。"""
    if strategy == "confidence_first":
        return tuple(sorted(findings, key=lambda finding: -finding.confidence))
    if strategy == "impact_first":
        return tuple(sorted(findings, key=lambda finding: -len(finding.impact_area)))
    return tuple(sorted(findings, key=lambda finding: _SEVERITY_RANK[finding.severity]))


def _build_coverage_summary(
    outputs: Sequence[MapperOutputEnvelope],
    shards: Sequence[MapShard],
    failed_reports: Sequence[FailedShardReport],
) -> CoverageSummary:
    """汇总覆盖率，输入为有效输出、shard 计划和失败报告，输出 CoverageSummary。"""
    per_shard = tuple(output.coverage for output in outputs)
    total_assigned = sum(coverage.files_assigned for coverage in per_shard)
    total_seen = sum(coverage.files_seen_count for coverage in per_shard)
    total_symbols = sum(coverage.symbols_seen_count for coverage in per_shard)
    notes = (
        f"已检查 {total_seen}/{total_assigned} 个已分配文件，"
        f"覆盖 {total_symbols} 个符号，失败 shard {len(failed_reports)}/{len(shards)}。"
    )
    return CoverageSummary(
        total_files_assigned=total_assigned,
        total_files_seen=total_seen,
        total_symbols_seen=total_symbols,
        per_shard=per_shard,
        notes=notes,
    )


def _sort_failed_shard_reports(
    reports: Sequence[FailedShardReport],
    shards: Sequence[MapShard],
) -> tuple[FailedShardReport, ...]:
    """按 shard 顺序汇总失败报告，输入为失败报告和计划 shard，输出稳定排序后的报告。"""
    order_by_shard_id = {shard.shard_id: shard.display_order for shard in shards}
    indexed_reports = tuple(enumerate(reports))
    return tuple(
        report
        for _, report in sorted(
            indexed_reports,
            key=lambda item: (order_by_shard_id.get(item[1].shard_id, len(shards)), item[0]),
        )
    )


def _build_followups(
    *,
    total_findings: int,
    max_findings: int,
    coverage_outputs: Sequence[MapperOutputEnvelope],
    failed_reports: Sequence[FailedShardReport],
    duplicate_count: int,
) -> tuple[str, ...]:
    """生成后续建议，输入为归并统计，输出为面向主 agent 的 followup 列表。"""
    followups: list[str] = []
    if failed_reports:
        shard_ids = ", ".join(report.shard_id for report in failed_reports)
        followups.append(f"补跑失败 shard：{shard_ids}")
    skipped_files = tuple(
        skipped for output in coverage_outputs for skipped in output.coverage.skipped_files
    )
    if skipped_files:
        followups.append(f"补充检查被跳过文件：{len(skipped_files)} 个")
    if duplicate_count > 0:
        followups.append(f"复核已合并重复 finding：{duplicate_count} 条")
    if max_findings < total_findings:
        followups.append(
            f"继续处理未进入 top_findings 的 finding：{total_findings - max_findings} 条"
        )
    return tuple(followups)


def _reducer_status(
    outputs: Sequence[MapperOutputEnvelope],
    failed_reports: Sequence[FailedShardReport],
) -> MapperStatus:
    """计算 reducer 状态，输入为有效输出和失败报告，输出 completed、partial 或 failed。"""
    if not outputs and failed_reports:
        return "failed"
    if failed_reports or any(output.status == "partial" for output in outputs):
        return "partial"
    return "completed"


def _normalize_text(value: str) -> str:
    """规范化去重文本，输入为原始字符串，输出为大小写和空白归一后的文本。"""
    return " ".join(value.casefold().split())


def _utc_now_iso() -> str:
    """生成 reducer 时间戳，输入为空，输出 UTC ISO8601 字符串。"""
    return datetime.now(UTC).isoformat().replace("+00:00", "Z")


__all__ = ["MapReduceReducer"]
