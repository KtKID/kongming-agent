"""
司天建议引擎——observations 归并为 work_items，生成建议/阻塞/风险/摘要。

Role:
    接收扫描产出的 observations 列表，按 project + thread 归并为 work_items，
    计算 workspace 级建议（suggestions）、阻塞项（blockers）、风险（risks），
    并渲染 markdown 摘要。

Owns:
    - work_item 归并逻辑（claude_workspace / generic_chat per-project 展开；其他 1:1）
    - suggestion / blocker / risk 分类规则
    - latest_summary.md 格式

Does not own:
    - 扫描（scanners.py）
    - LLM 分析（analyzer.py）
    - 持久化（store.py）

Called by:
    - service.py（扫描后调用 SiTianMaterializeState）

Key outputs:
    - SiTianMaterializeState（归并 + 建议 + 阻塞 + 风险）
    - SiTianBuildSummaryMarkdown（markdown 摘要文本）

Change risks:
    - 归并逻辑变化会影响 work_item 数量和去重
    - suggestion 分类规则变化会影响前端展示
"""

from __future__ import annotations

from collections import defaultdict
from datetime import UTC, datetime, timedelta
from pathlib import Path

from sitian.config import SiTianConfig, SiTianSourceKind
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

        # 多项目 source 展开为 per-project work_items；其他 kind 保持 1:1
        source_work_items = _expand_source_work_items(
            source_id=source.id,
            source_kind=source.kind,
            project_path=source.path,
            observations=source_observations,
            runtime_state=runtime_state,
            updated_at=updated_at,
        )

        for work_item in source_work_items:
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


def _expand_source_work_items(
    *,
    source_id: str,
    source_kind: SiTianSourceKind,
    project_path: str,
    observations: list[SiTianObservation],
    runtime_state: SiTianSourceRuntimeState | None,
    updated_at: str,
) -> list[SiTianWorkItem]:
    """按 source kind 展开 work_items。

    claude_workspace / generic_chat → per-project（每个 entity_type=project 独立成一条 work_item）。
    其他 kind → 1 source = 1 work_item（保持原行为）。
    """
    if source_kind in (
        SiTianSourceKind.CLAUDE_WORKSPACE,
        SiTianSourceKind.GENERIC_CHAT,
    ):
        return _expand_workspace_work_items(
            source_id=source_id,
            source_kind=source_kind,
            observations=observations,
            runtime_state=runtime_state,
            updated_at=updated_at,
        )
    return [
        _materialize_work_item(
            source_id=source_id,
            source_kind=source_kind,
            project_path=project_path,
            observations=observations,
            runtime_state=runtime_state,
            updated_at=updated_at,
        )
    ]


def _expand_workspace_work_items(
    *,
    source_id: str,
    source_kind: SiTianSourceKind,
    observations: list[SiTianObservation],
    runtime_state: SiTianSourceRuntimeState | None,
    updated_at: str,
) -> list[SiTianWorkItem]:
    """多项目 source 展开：每个 project（按 cwd 去重）对应一个 work_item。

    observations.jsonl 是追加式写入，多次扫描会产生同一 cwd 的多条
    project observation。这里按 cwd 分组，取最新一条的 payload 作为
    display_name / projectDirName，threads 也按 cwd 汇聚。
    """
    project_obs_list = [obs for obs in observations if obs.entity_type == "project"]
    if not project_obs_list:
        return []

    # 按 cwd 分组，保留最新（observed_at 最大）的 project obs 代表该 project
    projects_by_cwd: dict[str, SiTianObservation] = {}
    for obs in project_obs_list:
        cwd = str(obs.payload.get("cwd", ""))
        existing = projects_by_cwd.get(cwd)
        if existing is None or obs.observed_at > existing.observed_at:
            projects_by_cwd[cwd] = obs

    items: list[SiTianWorkItem] = []
    for project_cwd, project_obs in projects_by_cwd.items():
        display_name = str(project_obs.payload.get("displayName", ""))
        project_dir_name = str(project_obs.payload.get("projectDirName", ""))

        # 关联该 project 的 threads（按 cwd 匹配）
        project_threads = [
            obs
            for obs in observations
            if obs.entity_type == "thread" and obs.payload.get("cwd") == project_cwd
        ]
        project_observations: list[SiTianObservation] = [project_obs, *project_threads]

        work_item = _materialize_work_item(
            source_id=source_id,
            source_kind=source_kind,
            project_path=project_cwd,
            observations=project_observations,
            runtime_state=runtime_state,
            updated_at=updated_at,
            work_item_id=f"{source_id}:{project_dir_name or display_name}",
            display_name=display_name,
        )
        items.append(work_item)
    return items


def _materialize_work_item(
    *,
    source_id: str,
    source_kind: SiTianSourceKind,
    project_path: str,
    observations: list[SiTianObservation],
    runtime_state: SiTianSourceRuntimeState | None,
    updated_at: str,
    work_item_id: str | None = None,
    display_name: str | None = None,
) -> SiTianWorkItem:
    threads = [obs for obs in observations if obs.entity_type == "thread"]
    artifacts = [obs for obs in observations if obs.entity_type == "artifact"]
    projects = [obs for obs in observations if obs.entity_type == "project"]
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

    # project obs 也算"有产物"（claude_workspace 不产 artifact，产 project）
    if not artifacts and not projects:
        risks.append("缺少最近产物")
        next_actions.append("检查项目目录是否有新的交付物")

    # fileCount=0 只对 generic_channel 判（claude_workspace 没有 fileCount 概念）
    if (
        source_kind != SiTianSourceKind.CLAUDE_WORKSPACE
        and latest_status is not None
        and int(latest_status.payload.get("fileCount", 0)) == 0
    ):
        blockers.append("目录中没有可观察文件")
        next_actions.append("确认观察路径是否正确")

    title = _work_item_title(
        source_id, project_path, latest_status, threads, display_name=display_name
    )
    status = _work_item_status(blockers=blockers, threads=threads, runtime_state=runtime_state)
    priority = _work_item_priority(status=status, threads=threads, blockers=blockers)
    if not next_actions:
        next_actions.append("查看最新线程并决定下一步")

    thread_ids = tuple(
        dict.fromkeys(
            str(thread.payload.get("threadId", thread.entity_key))
            for thread in threads[:_MAX_THREAD_IDS]
        )
    )
    project_paths = (project_path,)
    artifact_paths = tuple(
        str(item.payload.get("relativePath", item.entity_key))
        for item in artifacts[:_MAX_ARTIFACTS]
    )

    return SiTianWorkItem(
        id=work_item_id or source_id,
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
    *,
    display_name: str | None = None,
) -> str:
    # claude_workspace per-project 优先用 displayName（如 "kongming-agent"）
    if display_name and display_name.strip():
        return display_name.strip()
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
