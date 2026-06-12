"""parallel workflow viewer adapter。"""

from __future__ import annotations

from hosts.web.workflow_viewer.adapters.unknown import UnknownWorkflowViewerAdapter
from hosts.web.workflow_viewer.models import (
    WorkflowArtifactBundle,
    WorkflowModeProjection,
    WorkflowPanelDTO,
)


class ParallelWorkflowViewerAdapter(UnknownWorkflowViewerAdapter):
    mode = "parallel"

    def project(self, bundle: WorkflowArtifactBundle) -> WorkflowModeProjection:
        base = super().project(bundle)
        panel = WorkflowPanelDTO(
            panel_id="parallel-tasks",
            mode=self.mode,
            kind="table",
            title="Parallel Tasks",
            payload={
                "tasks": [
                    {
                        "task_id": report.get("task_id"),
                        "task_name": report.get("task_name"),
                        "status": report.get("status"),
                        "summary": report.get("summary"),
                        "usage": report.get("usage") or {},
                    }
                    for report in bundle.reports
                ]
            },
        )
        return WorkflowModeProjection(
            panels=(panel, *base.panels),
            flow_nodes=base.flow_nodes,
            flow_edges=base.flow_edges,
            diagnostics=base.diagnostics,
            has_mode_panel=True,
        )
