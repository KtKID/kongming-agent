"""Agent workflow viewer adapter 基础协议。"""

from __future__ import annotations

from typing import Protocol

from hosts.web.workflow_viewer.models import WorkflowArtifactBundle, WorkflowModeProjection


class WorkflowViewerAdapter(Protocol):
    mode: str

    def project(self, bundle: WorkflowArtifactBundle) -> WorkflowModeProjection:
        """把 mode 专属产物投影为前端 panel 和 flow。"""
        ...
