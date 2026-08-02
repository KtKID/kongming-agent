"""
司天包公共入口——统一 re-export 所有公共符号。

Role:
    包门面，让下游 ``from sitian import X`` 一行搞定，不需要知道内部模块拆分。

Owns:
    - re-export 列表（即"哪些符号算公共 API"的唯一真源）

Does not own:
    - 任何业务逻辑、数据模型、IO 操作

Called by:
    - sitian 外部消费者（cli.py、store.py 延迟引用等）

Key outputs:
    - SiTianConfig, SiTianSourceConfig, SiTianSourceKind
    - 11 个 frozen dataclass（SiTianObservation, SiTianReport, …）
    - SiTianScanSource, SiTianScanBatch
    - SiTianRunOnce, SiTianRunLoop, SiTianReadState, SiTianRunResult
    - SiTianRecordsStore, resolve_sitian_root
    - SiTianMaterializeState, SiTianBuildSummaryMarkdown
    - sitian_analyze, report_to_markdown
    - read_claude_session_tail
    - list_claude_projects_global, list_codex_projects_global

Change risks:
    - 漏 re-export 会让外部 import 报错
    - 循环引用风险：此文件 import 几乎所有兄弟模块
"""

from sitian.config import SiTianConfig, SiTianSourceConfig, SiTianSourceKind
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
from sitian.scanners import SiTianScanBatch, SiTianScanSource
from sitian.service import (
    SiTianReadState,
    SiTianRunLoop,
    SiTianRunOnce,
    SiTianRunResult,
)
from sitian.store import SiTianRecordsStore, decode_work_item_filename, resolve_sitian_root
from sitian.suggestions import SiTianBuildSummaryMarkdown, SiTianMaterializeState

__all__ = [
    "JsonValue",
    "SiTianBuildSummaryMarkdown",
    "SiTianConfig",
    "SiTianMaterializeState",
    "SiTianObservation",
    "SiTianPendingApproval",
    "SiTianRecordsStore",
    "SiTianReadState",
    "SiTianRunLoop",
    "SiTianRunOnce",
    "SiTianRunResult",
    "SiTianScanBatch",
    "SiTianScanSource",
    "SiTianSourceConfig",
    "SiTianSourceKind",
    "SiTianSourceRuntimeState",
    "SiTianWorkItem",
    "SiTianWorkspaceBlocker",
    "SiTianWorkspaceRisk",
    "SiTianWorkspaceSourcesSummary",
    "SiTianWorkspaceState",
    "decode_work_item_filename",
    "resolve_sitian_root",
]
