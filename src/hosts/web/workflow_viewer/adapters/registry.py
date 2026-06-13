"""Agent workflow viewer adapter registry。"""

from __future__ import annotations

from hosts.web.workflow_viewer.adapters.base import WorkflowViewerAdapter
from hosts.web.workflow_viewer.adapters.map_reduce import MapReduceWorkflowViewerAdapter
from hosts.web.workflow_viewer.adapters.parallel import ParallelWorkflowViewerAdapter
from hosts.web.workflow_viewer.adapters.roundtable_review import (
    RoundtableReviewWorkflowViewerAdapter,
)
from hosts.web.workflow_viewer.adapters.unknown import UnknownWorkflowViewerAdapter


class WorkflowViewerAdapterRegistry:
    """根据 workflow mode 分发 viewer adapter。"""

    def __init__(self) -> None:
        self._unknown = UnknownWorkflowViewerAdapter()
        self._adapters: dict[str, WorkflowViewerAdapter] = {
            "parallel": ParallelWorkflowViewerAdapter(),
            "map_reduce": MapReduceWorkflowViewerAdapter(),
            "roundtable_review": RoundtableReviewWorkflowViewerAdapter(),
        }

    def get(self, mode: str) -> WorkflowViewerAdapter:
        return self._adapters.get(mode, self._unknown)
