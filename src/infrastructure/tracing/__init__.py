"""infrastructure.tracing Baseline —— v1-mini 第一版观测层。

对外只暴露一个 :class:`~core.contracts.EventSink` 的实现类
:class:`JsonlTraceSink`，以及对应的工厂函数 :func:`build_jsonl_trace_sink`。

设计边界（详见 ``docs/kongming-agent-v1-minimal/10-contracts.md``）：

- **不**在本包下重新定义 ``EventSink`` / ``Event`` / ``TraceSink`` /
  ``UsageSink`` / ``AuditSink`` 任何 Protocol。
  跨模块协议真源始终是 :mod:`core.contracts`。
- v0.2+ 追加的 ``UsageSink`` / ``AuditSink`` 都是
  :class:`~core.contracts.EventSink` 的并列实现类，不替换本类。
- fan-out 由 :class:`core.runner.Runner` 持有 ``list[EventSink]`` 完成，
  本包下的 sink 不负责再分发。
"""

from __future__ import annotations

from infrastructure.tracing.prompt_debug_dump import PromptDebugDumpSink
from infrastructure.tracing.trace_sink import JsonlTraceSink, build_jsonl_trace_sink

__all__ = [
    "JsonlTraceSink",
    "PromptDebugDumpSink",
    "build_jsonl_trace_sink",
]
