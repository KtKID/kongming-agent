"""Deep Research workflow strategy helpers.

本包承载 deep_research 策略的来源检索、读取、去重和子任务审计日志能力。
作用是先固定来源输入 contract，后续完整策略可以直接消费结构化 source record。
关键执行流程：构造 ResearchSourceQuery，调用 ResearchSourceManager 收集来源，使用 SourceDeduper 去重并写入审计事件。
关键函数：ResearchSourceManager.collect_sources 收集来源，SourceDeduper.select 选择唯一 URL，DeepResearchTaskLogWriter 记录子 agent task 日志。
"""

from application.agent_workflows.strategies.deep_research.contracts import (
    ResearchSourceCandidate,
    ResearchSourceProvider,
    ResearchSourceQuery,
    ResearchSourceRecord,
)
from application.agent_workflows.strategies.deep_research.dedupe import (
    SourceDeduper,
    SourceDedupeResult,
    canonicalize_url,
    stable_source_id,
)
from application.agent_workflows.strategies.deep_research.source_provider import (
    FakeResearchSourceProvider,
    ResearchSourceManager,
)
from application.agent_workflows.strategies.deep_research.task_log import (
    DeepResearchTaskLogWriter,
)

__all__ = [
    "DeepResearchTaskLogWriter",
    "FakeResearchSourceProvider",
    "ResearchSourceCandidate",
    "ResearchSourceManager",
    "ResearchSourceProvider",
    "ResearchSourceQuery",
    "ResearchSourceRecord",
    "SourceDedupeResult",
    "SourceDeduper",
    "canonicalize_url",
    "stable_source_id",
]
