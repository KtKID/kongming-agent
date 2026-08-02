"""单次 run 的显式执行覆盖合同。

该值对象只承载一次 ``SessionEngine.run`` 调用的已装配依赖。普通 thread 使用
SessionEngine 默认依赖；scheduled thread 通过本合同传入 fresh session、preset
provider、任务级审批、工具裁剪、run-scoped sinks 与稳定 run ID。
"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from core.contracts.approval import ApprovalProvider
from core.contracts.event_sink import EventSink
from core.contracts.llm_provider import LLMProvider
from core.contracts.session import Session
from core.contracts.tool_runtime import ToolLookup


@dataclass(frozen=True)
class RunExecutionOverrides:
    """一次 SessionEngine run 的 immutable 依赖快照。"""

    session: Session | None = None
    llm: LLMProvider | None = None
    tools: ToolLookup | None = None
    approval_transform: Callable[[ApprovalProvider], ApprovalProvider] | None = None
    run_id: str | None = None
    event_sinks: Sequence[EventSink] = ()
    tool_context_metadata: Mapping[str, Any] | None = None


__all__ = ["RunExecutionOverrides"]
