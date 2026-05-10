from __future__ import annotations

from collections import defaultdict
from datetime import UTC, datetime, timedelta
from pathlib import Path

from sitian.config import SiTianConfig
from sitian.models import (
    JsonValue,
    SiTianObservation,
    SiTianPendingApproval,
    SiTianSourceRuntimeState,
    SiTianWorkItem,
    SiTianWorkspaceBlocker,
    SiTianWorkspaceRisk,
    SiTianWorkspaceSourcesSummary,
    SiTianWorkspaceState,
)


def SiTianMaterializeState(
    config: SiTianConfig,
    *,
    runtime_states: tuple[SiTianSourceRuntimeState, ...],
    observations: tuple[SiTianObservation, ...],
    updated_at: str,
) -> tuple[SiTianWorkspaceState, tuple[SiTianWorkItem, ...], dict[str, JsonValue], str]:
    observations_by_source: dict[str, list[SiTianObservation]] = defaultdict(list)
    for observation in observations:
        observations_by_source[observation.source_id].append(observation)

    state_by_source = {state.source_id: state for state in runtime_states}
    work_items: list[SiTianWorkItem] = []
    blockers: list[SiTianWorkspaceBlocker] = []
    approvals: list[SiTianPendingApproval] = []
    risks: list[SiTianWorkspaceRisk] = []
    recommendation_items: list[dict[str, JsonValue]] = []

    active_sources = 0
    for source in config.sources:
        source_observations = sorted(
            observations_by_source.get(source.id, []),
            key=lambda item: (item.observed_at, item.entity_type, item.entity_key),
            reverse=True,
        )
        runtime_state = state_by_source.get(source.id)
        if runtime_state is not None and runtime_state.status != "error":
            active_sources += 1

        work_item = _materialize_work_item(
            source_id=source.id,
            source_kind=source.kind,
            project_path=source.path,
            observations=source_observations,
            runtime_state=runtime_state,
            updated_at=updated_at,
        )
        work_items.append(work_item)

        for blocker_summary in work_item.blockers:
            blockers.append(
                SiTianWorkspaceBlocker(
                    work_item_id=work_item.id,
                    summary=blocker_summary,
                    severity="high" if work_item.status == "blocked" else "medium",
                )
            )
        if (
            runtime_state is not None
            and runtime_state.status == "error"
            and runtime_state.last_error
        ):
            risks.append(
                SiTianWorkspaceRisk(
                    category="scan_error",
                    summary=f"{source.id}: {runtime_state.last_error}",
                    evidence_refs=(source.path,),
                )
            )

        if work_item.status == "waiting":
            for thread_id in work_item.thread_ids or (work_item.id,):
                approvals.append(
                    SiTianPendingApproval(
                        thread_id=thread_id,
                        summary=f"{work_item.title} 等待人工确认或下一步决策",
                    )
                )

        recommendation_items.append(
            {
                "workItemId": work_item.id,
                "title": work_item.title,
                "priority": work_item.priority,
                "status": work_item.status,
                "reason": _recommendation_reason(work_item),
                "nextAction": work_item.next_actions[0]
                if work_item.next_actions
                else "同步最新状态",
            }
        )

        if _is_stale(work_item.updated_at):
            risks.append(
                SiTianWorkspaceRisk(
                    category="stale_activity",
                    summary=f"{work_item.title} 最近缺少新活动",
                    evidence_refs=tuple(work_item.project_paths or (source.path,)),
                )
            )

    recommendation_items.sort(key=_recommendation_sort_key)
    workspace_state = SiTianWorkspaceState(
        version="v1",
        updated_at=updated_at,
        sources=SiTianWorkspaceSourcesSummary(
            total=len(config.sources),
            active=active_sources,
        ),
        work_items=tuple(work_items),
        blockers=tuple(blockers),
        pending_approvals=tuple(approvals),
        risks=tuple(risks),
    )
    suggestions = {
        "generatedAt": updated_at,
        "items": recommendation_items,
    }
    summary = SiTianBuildSummaryMarkdown(workspace_state, suggestions)
    return workspace_state, tuple(work_items), suggestions, summary


def SiTianBuildSummaryMarkdown(
    workspace_state: SiTianWorkspaceState,
    suggestions: dict[str, JsonValue],
) -> str:
    lines = [
        "# SiTian Summary",
        "",
        f"- Updated: {workspace_state.updated_at}",
        f"- Sources: {workspace_state.sources.active}/{workspace_state.sources.total} active",
        f"- Work items: {len(workspace_state.work_items)}",
        "",
        "## Next Actions",
    ]
    for item in suggestions.get("items", [])[:5]:
        if not isinstance(item, dict):
            continue
        lines.append(
            f"- [{item.get('priority', 'medium')}] {item.get('title', 'Unknown')}: "
            f"{item.get('nextAction', '同步最新状态')}"
        )

    if workspace_state.blockers:
        lines.extend(["", "## Blockers"])
        for blocker in workspace_state.blockers[:5]:
            lines.append(f"- {blocker.summary}")

    if workspace_state.risks:
        lines.extend(["", "## Risks"])
        for risk in workspace_state.risks[:5]:
            lines.append(f"- {risk.summary}")

    return "\n".join(lines) + "\n"


def _materialize_work_item(
    *,
    source_id: str,
    source_kind: str,
    project_path: str,
    observations: list[SiTianObservation],
    runtime_state: SiTianSourceRuntimeState | None,
    updated_at: str,
) -> SiTianWorkItem:
    threads = [obs for obs in observations if obs.entity_type == "thread"]
    artifacts = [obs for obs in observations if obs.entity_type == "artifact"]
    latest_status = next((obs for obs in observations if obs.entity_type == "status"), None)

    blockers: list[str] = []
    risks: list[str] = []
    next_actions: list[str] = []

    if runtime_state is not None and runtime_state.status == "error" and runtime_state.last_error:
        blockers.append(runtime_state.last_error)
        next_actions.append("修复扫描路径或权限，恢复观察")

    if not threads:
        risks.append("缺少线程级进展")
        next_actions.append("补充最近进展或同步黑板摘要")

    if not artifacts:
        risks.append("缺少最近产物")
        next_actions.append("检查项目目录是否有新的交付物")

    if latest_status is not None and int(latest_status.payload.get("fileCount", 0)) == 0:
        blockers.append("目录中没有可观察文件")
        next_actions.append("确认观察路径是否正确")

    title = _work_item_title(source_id, project_path, latest_status, threads)
    status = _work_item_status(blockers=blockers, threads=threads, runtime_state=runtime_state)
    priority = _work_item_priority(status=status, threads=threads, blockers=blockers)
    if not next_actions:
        next_actions.append("查看最新线程并决定下一步")

    thread_ids = tuple(
        str(thread.payload.get("threadId", thread.entity_key))
        for thread in threads[:_MAX_THREAD_IDS]
    )
    project_paths = (project_path,)
    artifact_paths = tuple(
        str(item.payload.get("relativePath", item.entity_key))
        for item in artifacts[:_MAX_ARTIFACTS]
    )

    return SiTianWorkItem(
        id=source_id,
        title=title,
        status=status,
        priority=priority,
        source_ids=(source_id,),
        thread_ids=thread_ids,
        project_paths=project_paths,
        blockers=tuple(dict.fromkeys(blockers)),
        artifacts=artifact_paths,
        next_actions=tuple(dict.fromkeys(next_actions)),
        risks=tuple(dict.fromkeys(risks)),
        updated_at=_latest_observed_at(observations) or updated_at,
    )


_MAX_THREAD_IDS = 5
_MAX_ARTIFACTS = 5


def _work_item_title(
    source_id: str,
    project_path: str,
    latest_status: SiTianObservation | None,
    threads: list[SiTianObservation],
) -> str:
    if threads:
        title = threads[0].payload.get("title")
        if isinstance(title, str) and title.strip():
            return title.strip()
    if latest_status is not None:
        path_value = latest_status.payload.get("path")
        if isinstance(path_value, str) and path_value.strip():
            return Path(path_value).name or path_value
    return Path(project_path).name or source_id


def _work_item_status(
    *,
    blockers: list[str],
    threads: list[SiTianObservation],
    runtime_state: SiTianSourceRuntimeState | None,
) -> str:
    if blockers:
        return "blocked"
    if runtime_state is not None and runtime_state.status == "running":
        return "active"
    if threads and _is_stale(threads[0].observed_at, hours=24):
        return "waiting"
    if threads:
        return "active"
    return "uncertain"


def _work_item_priority(
    *,
    status: str,
    threads: list[SiTianObservation],
    blockers: list[str],
) -> str:
    if status == "blocked":
        return "high"
    if blockers or not threads:
        return "medium"
    return "low"


def _recommendation_reason(work_item: SiTianWorkItem) -> str:
    if work_item.status == "blocked" and work_item.blockers:
        return work_item.blockers[0]
    if work_item.risks:
        return work_item.risks[0]
    return "有新的线程或产物可继续推进"


def _recommendation_sort_key(item: dict[str, JsonValue]) -> tuple[int, int]:
    priority_order = {"high": 0, "medium": 1, "low": 2}
    status_order = {"blocked": 0, "active": 1, "waiting": 2, "uncertain": 3, "done": 4}
    return (
        priority_order.get(str(item.get("priority", "medium")), 9),
        status_order.get(str(item.get("status", "uncertain")), 9),
    )


def _latest_observed_at(observations: list[SiTianObservation]) -> str | None:
    if not observations:
        return None
    return max(item.observed_at for item in observations)


def _is_stale(timestamp: str, *, hours: int = 6) -> bool:
    try:
        moment = datetime.fromisoformat(timestamp.replace("Z", "+00:00"))
    except ValueError:
        return False
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=UTC)
    return datetime.now(tz=UTC) - moment > timedelta(hours=hours)


__all__ = ["SiTianBuildSummaryMarkdown", "SiTianMaterializeState"]
