"""roundtable_review workflow viewer adapter。"""

from __future__ import annotations

from typing_extensions import override

from hosts.web.workflow_viewer.adapters.unknown import UnknownWorkflowViewerAdapter
from hosts.web.workflow_viewer.artifact_reader import WorkflowArtifactReader
from hosts.web.workflow_viewer.models import (
    WorkflowArtifactBundle,
    WorkflowDiagnosticDTO,
    WorkflowModeProjection,
    WorkflowPanelDTO,
)


class RoundtableReviewWorkflowViewerAdapter(UnknownWorkflowViewerAdapter):
    mode = "roundtable_review"

    @override
    def project(self, bundle: WorkflowArtifactBundle) -> WorkflowModeProjection:
        base = super().project(bundle)
        reader = WorkflowArtifactReader(bundle.workflow_dir)
        diagnostics: list[WorkflowDiagnosticDTO] = list(base.diagnostics)
        context, _, diag = reader.read_text("review_board/context.md")
        diagnostics.extend(diag)
        sources, _, diag = reader.read_text("review_board/sources.md")
        diagnostics.extend(diag)
        claims, diag = reader.read_jsonl("review_board/claims.jsonl")
        diagnostics.extend(diag)
        rebuttals, diag = reader.read_jsonl("review_board/rebuttals.jsonl")
        diagnostics.extend(diag)
        consensus, _, diag = reader.read_text("review_board/consensus.md")
        diagnostics.extend(diag)
        final_report, _, diag = reader.read_text("review_board/final_report.md")
        diagnostics.extend(diag)
        roundtable = {}
        if isinstance(bundle.result_json, dict) and isinstance(
            bundle.result_json.get("roundtable_review"), dict
        ):
            roundtable = bundle.result_json["roundtable_review"]
        panel = WorkflowPanelDTO(
            panel_id="roundtable-review-board",
            mode=self.mode,
            kind="review_board",
            title="Review Board",
            payload={
                "topic": roundtable.get("topic"),
                "claim_count": roundtable.get("claim_count", len(claims)),
                "rebuttal_count": roundtable.get("rebuttal_count", len(rebuttals)),
                "context": context,
                "sources": sources,
                "claims": claims,
                "rebuttals": rebuttals,
                "consensus": consensus,
                "final_report": final_report,
            },
            available=bool(claims or final_report or consensus),
            missing_reason=None if claims or final_report or consensus else "missing_review_board",
        )
        return WorkflowModeProjection(
            panels=(panel, *base.panels),
            flow_nodes=base.flow_nodes,
            flow_edges=base.flow_edges,
            diagnostics=tuple(diagnostics),
            has_mode_panel=True,
        )
