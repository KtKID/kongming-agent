"""Agent workflow token usage 投影。"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from hosts.web.workflow_viewer.models import (
    WorkflowArtifactBundle,
    WorkflowDiagnosticDTO,
    WorkflowUsageDTO,
    WorkflowUsageRecordDTO,
)


class UsageProjector:
    """从 workflow 产物中投影 workflow 内 token 用量。"""

    def project(self, bundle: WorkflowArtifactBundle) -> WorkflowUsageDTO:
        mode_total = self._mode_usage_total(bundle)
        mode_records = self._mode_usage_records(bundle)
        diagnostics: list[WorkflowDiagnosticDTO] = []
        records = mode_records
        source = "result.mode"
        if not records:
            records = self._report_usage_records(bundle)
            source = "reports"
        existing_keys = (
            set().union(*(_record_keys(record) for record in records)) if records else set()
        )
        for record in self._subagent_usage_records(bundle):
            keys = _record_keys(record)
            if keys.isdisjoint(existing_keys):
                records.append(record)
                existing_keys.update(keys)
        totals = mode_total if mode_total else _sum_usage(record.usage for record in records)
        if mode_total and records:
            diagnostics.append(
                WorkflowDiagnosticDTO(
                    code="usage.mode_total_selected",
                    severity="info",
                    message="workflow total 使用 result.json mode 汇总，子 agent 明细用于钻取展示",
                )
            )
        provider_totals: dict[str, dict[str, int]] = {}
        for record in records:
            provider = record.provider or "unknown"
            provider_totals[provider] = _merge_usage(
                provider_totals.get(provider, {}), record.usage
            )
        return WorkflowUsageDTO(
            source=source if records or totals else "none",
            totals=totals,
            provider_totals=provider_totals,
            records=records,
            diagnostics=diagnostics,
        )

    @staticmethod
    def _mode_usage_total(bundle: WorkflowArtifactBundle) -> dict[str, int]:
        result = bundle.result_json or {}
        mode = _read_mode(bundle)
        section = result.get(mode)
        if isinstance(section, Mapping):
            raw = section.get("child_agent_usage_totals")
            if isinstance(raw, Mapping):
                return _numeric_usage(raw)
        return {}

    @staticmethod
    def _mode_usage_records(bundle: WorkflowArtifactBundle) -> list[WorkflowUsageRecordDTO]:
        result = bundle.result_json or {}
        mode = _read_mode(bundle)
        section = result.get(mode)
        if not isinstance(section, Mapping):
            return []
        raw_records = section.get("child_agent_usages")
        if not isinstance(raw_records, list):
            return []
        records: list[WorkflowUsageRecordDTO] = []
        for item in raw_records:
            if not isinstance(item, Mapping):
                continue
            usage = _numeric_usage(item.get("usage"))
            records.append(
                WorkflowUsageRecordDTO(
                    task_id=_str_or_none(item.get("task_id")),
                    task_name=_str_or_none(item.get("task_name")),
                    session_id=_str_or_none(item.get("session_id")),
                    run_id=_str_or_none(item.get("run_id")),
                    status=_str_or_none(item.get("status")),
                    provider=_provider_for_usage(usage),
                    source="result.mode.child_agent_usages",
                    usage=usage,
                )
            )
        return records

    @staticmethod
    def _report_usage_records(bundle: WorkflowArtifactBundle) -> list[WorkflowUsageRecordDTO]:
        records: list[WorkflowUsageRecordDTO] = []
        for report in bundle.reports:
            usage = _numeric_usage(report.get("usage"))
            if not usage:
                continue
            records.append(
                WorkflowUsageRecordDTO(
                    task_id=_str_or_none(report.get("task_id")),
                    task_name=_str_or_none(report.get("task_name")),
                    session_id=_str_or_none(report.get("session_id")),
                    run_id=_str_or_none(report.get("run_id")),
                    status=_str_or_none(report.get("status")),
                    provider=_provider_for_usage(usage),
                    source="reports",
                    usage=usage,
                )
            )
        return records

    @staticmethod
    def _subagent_usage_records(bundle: WorkflowArtifactBundle) -> list[WorkflowUsageRecordDTO]:
        records: list[WorkflowUsageRecordDTO] = []
        for item in bundle.subagent_records:
            usage = _numeric_usage(item.subagent_json.get("usage"))
            if not usage:
                continue
            records.append(
                WorkflowUsageRecordDTO(
                    task_run_id=item.task_run_id,
                    task_id=_str_or_none(item.subagent_json.get("task_id")),
                    task_name=_str_or_none(item.subagent_json.get("task_name")),
                    session_id=item.child_session_id,
                    run_id=_str_or_none(item.subagent_json.get("completed_run_id")),
                    status=_str_or_none(item.subagent_json.get("completed_status")),
                    provider=_provider_for_usage(usage),
                    source="subagent.json",
                    usage=usage,
                )
            )
        return records


def _read_mode(bundle: WorkflowArtifactBundle) -> str:
    for payload in (bundle.workflow_json, bundle.result_json):
        if isinstance(payload, Mapping) and isinstance(payload.get("mode"), str):
            return str(payload["mode"])
    return "unknown"


def _record_keys(record: WorkflowUsageRecordDTO) -> set[str]:
    keys = {
        value
        for value in (record.task_run_id, record.run_id, record.session_id, record.task_id)
        if value
    }
    if not keys:
        keys.add(str(id(record)))
    return keys


def _sum_usage(items: list[dict[str, int]] | Any) -> dict[str, int]:
    totals: dict[str, int] = {}
    for usage in items:
        totals = _merge_usage(totals, usage)
    return totals


def _merge_usage(left: dict[str, int], right: dict[str, int]) -> dict[str, int]:
    merged = dict(left)
    for key, value in right.items():
        merged[key] = merged.get(key, 0) + value
    return merged


def _numeric_usage(raw: Any) -> dict[str, int]:
    if not isinstance(raw, Mapping):
        return {}
    return {
        str(key): int(value)
        for key, value in raw.items()
        if isinstance(key, str) and isinstance(value, int) and not isinstance(value, bool)
    }


def _provider_for_usage(usage: Mapping[str, int]) -> str:
    provider_kind = usage.get("provider_kind")
    if isinstance(provider_kind, str):
        return provider_kind
    if "cache_read_input_tokens" in usage or "cache_creation_input_tokens" in usage:
        return "claude"
    if "reasoning_output_tokens" in usage or "cached_input_tokens" in usage:
        return "openai"
    return "unknown"


def _str_or_none(value: Any) -> str | None:
    return value if isinstance(value, str) else None
