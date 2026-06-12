"""未知 workflow mode 的 fallback adapter。"""

from __future__ import annotations

from hosts.web.workflow_viewer.models import (
    WorkflowArtifactBundle,
    WorkflowFlowEdgeDTO,
    WorkflowFlowNodeDTO,
    WorkflowModeProjection,
    WorkflowPanelDTO,
)


class UnknownWorkflowViewerAdapter:
    mode = "unknown"

    def project(self, bundle: WorkflowArtifactBundle) -> WorkflowModeProjection:
        mode = _mode(bundle)
        panels = (
            WorkflowPanelDTO(
                panel_id="raw-summary",
                mode=mode,
                kind="summary",
                title="公共产物",
                payload={
                    "workflow": bundle.workflow_json or {},
                    "result": bundle.result_json or {},
                    "report_count": len(bundle.reports),
                    "artifact_count": len(bundle.artifacts),
                },
            ),
            WorkflowPanelDTO(
                panel_id="artifact-browser",
                mode=mode,
                kind="table",
                title="Artifacts",
                payload={"artifacts": [artifact.model_dump() for artifact in bundle.artifacts]},
            ),
        )
        return WorkflowModeProjection(
            panels=panels,
            flow_nodes=tuple(_default_nodes(bundle)),
            flow_edges=tuple(_default_edges(bundle)),
            has_mode_panel=False,
        )


def _mode(bundle: WorkflowArtifactBundle) -> str:
    for payload in (bundle.workflow_json, bundle.result_json):
        if isinstance(payload, dict) and isinstance(payload.get("mode"), str):
            return str(payload["mode"])
    return "unknown"


def _default_nodes(bundle: WorkflowArtifactBundle) -> list[WorkflowFlowNodeDTO]:
    nodes = [
        WorkflowFlowNodeDTO(
            id="workflow-start", label="Workflow Start", kind="start", status="done"
        )
    ]
    for report in bundle.reports:
        task_id = str(report.get("task_id") or report.get("task_name") or len(nodes))
        nodes.append(
            WorkflowFlowNodeDTO(
                id=f"task-{task_id}",
                label=str(report.get("task_name") or task_id),
                kind="subagent",
                status=str(report.get("status") or "unknown"),
                metadata={"summary": report.get("summary")},
            )
        )
    nodes.append(
        WorkflowFlowNodeDTO(
            id="workflow-result",
            label="Workflow Result",
            kind="result",
            status=str((bundle.workflow_json or {}).get("status") or "unknown"),
        )
    )
    return nodes


def _default_edges(bundle: WorkflowArtifactBundle) -> list[WorkflowFlowEdgeDTO]:
    task_nodes = [node.id for node in _default_nodes(bundle) if node.kind == "subagent"]
    edges: list[WorkflowFlowEdgeDTO] = []
    for task_id in task_nodes:
        edges.append(
            WorkflowFlowEdgeDTO(
                id=f"workflow-start-{task_id}", source="workflow-start", target=task_id
            )
        )
        edges.append(
            WorkflowFlowEdgeDTO(
                id=f"{task_id}-workflow-result", source=task_id, target="workflow-result"
            )
        )
    if not task_nodes:
        edges.append(
            WorkflowFlowEdgeDTO(
                id="workflow-start-result", source="workflow-start", target="workflow-result"
            )
        )
    return edges
