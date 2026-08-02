"""map_reduce workflow viewer adapter。"""

from __future__ import annotations

from typing import Any

from typing_extensions import override

from hosts.web.workflow_viewer.adapters.unknown import UnknownWorkflowViewerAdapter
from hosts.web.workflow_viewer.artifact_reader import WorkflowArtifactReader
from hosts.web.workflow_viewer.models import (
    WorkflowArtifactBundle,
    WorkflowDiagnosticDTO,
    WorkflowModeProjection,
    WorkflowPanelDTO,
)


class MapReduceWorkflowViewerAdapter(UnknownWorkflowViewerAdapter):
    mode = "map_reduce"

    @override
    def project(self, bundle: WorkflowArtifactBundle) -> WorkflowModeProjection:
        base = super().project(bundle)
        reader = WorkflowArtifactReader(bundle.workflow_dir)
        panels: list[WorkflowPanelDTO] = []
        diagnostics: list[WorkflowDiagnosticDTO] = list(base.diagnostics)
        shards, diag = reader.read_json("map_reduce/shards.json")
        diagnostics.extend(diag)
        mapper_index, diag = reader.read_json("map_reduce/mappers/index.json")
        diagnostics.extend(diag)
        reducer_result, diag = reader.read_json_any("map_reduce/reducer/result.json")
        diagnostics.extend(diag)
        panels.append(
            WorkflowPanelDTO(
                panel_id="map-reduce-plan",
                mode=self.mode,
                kind="map_reduce",
                title="Map Reduce",
                payload={
                    "shards": _safe_payload(shards),
                    "mapper_index": _safe_payload(mapper_index),
                    "reducer_result": _safe_payload(reducer_result),
                    "reports": bundle.reports,
                },
                available=bool(shards or mapper_index or reducer_result),
                missing_reason=None
                if shards or mapper_index or reducer_result
                else "missing_map_reduce_artifacts",
            )
        )
        return WorkflowModeProjection(
            panels=(*panels, *base.panels),
            flow_nodes=base.flow_nodes,
            flow_edges=base.flow_edges,
            diagnostics=tuple(diagnostics),
            has_mode_panel=True,
        )


def _safe_payload(value: Any) -> Any:
    return value if value is not None else {}
