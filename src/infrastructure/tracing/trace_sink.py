"""JSONL trace sink —— v1-mini 唯一的 :class:`core.contracts.EventSink` 实现。

设计要点（对齐 ``docs/kongming-agent-v1-minimal/10-contracts.md``
"infrastructure.tracing / EventSink 边界"）：

- :class:`JsonlTraceSink` **不是**一个新的 Protocol，而是
  :class:`core.contracts.EventSink` Protocol 的具体实现类。
  全部事件 kind（``run.start`` / ``turn.*`` / ``llm.*`` / ``tool.call.*`` /
  ``approval.*`` / ``error`` / …）都写进同一份 JSONL。
- fan-out 是 runner 的职责（runner 持有 ``list[EventSink]``）；
  本文件的 sink 实现不负责把事件再分发给别的 sink。
- v0.2+ 追加的 ``UsageSink`` / ``AuditSink`` 是**并列注册**的兄弟实现，
  不替换 :class:`JsonlTraceSink`，也不新增事件协议。

运行时契约：

- 全链路 async：:meth:`JsonlTraceSink.emit` 是 ``async``。
- 每次 ``emit`` 是一次独立的 ``open / append / flush / close``，
  避免长期持有 FD 在崩溃时丢事件。文件写入用 :func:`asyncio.to_thread`
  放到线程池，以免阻塞事件循环。
- 并发写入用 :class:`asyncio.Lock` 串行化，防止多协程交错写出半行 JSON。
- 序列化走 :func:`json.dumps`，``ensure_ascii=False`` 保中文可读；
  对 :class:`~pathlib.Path` / :class:`~datetime.datetime` / dataclass 等
  默认不可 JSON 序列化的值，用 :func:`_json_default` 做降级转换，
  保证任何 :class:`~core.contracts.Event` 都能落盘成一行合法 JSON。

零外部依赖：只用 stdlib（``asyncio`` / ``json`` / ``pathlib`` /
``dataclasses`` / ``datetime``）。
"""

from __future__ import annotations

import asyncio
import dataclasses
import json
from datetime import date, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any

from infrastructure.config.paths import resolve_kongming_path

if TYPE_CHECKING:
    # 仅用于类型标注；运行时不强制存在，避免触发循环 import。
    # ``JsonlTraceSink`` 通过鸭子类型实现 EventSink Protocol，
    # 不继承、不在本文件重定义该 Protocol。
    from core.contracts import Event
    from infrastructure.config.models import Config


_STREAM_DELTA_KINDS: frozenset[str] = frozenset({"content.delta", "reasoning.delta"})
"""流式增量事件 kind 集合，受 ``delta_sampling`` 策略约束。"""


class JsonlTraceSink:
    """Write every :class:`~core.contracts.Event` as one JSON line.

    首版是 v1 唯一的 :class:`~core.contracts.EventSink` 实现。所有事件 kind
    都写进同一份 append-only JSONL，作为 v0.2+ usage / audit 派生能力的上游
    事实源。

    本类通过**鸭子类型**实现 ``EventSink`` Protocol：只保证存在
    ``async def emit(event) -> None``，不继承也不在本模块重新定义该 Protocol。

    Attributes:
        output_path: JSONL 落盘路径。父目录会在首次 ``emit`` 时按需创建。
        auto_flush: 每次写入后是否立即 ``flush``。默认 ``True``，牺牲少量吞吐
            换取崩溃场景下最小丢行窗口。
        delta_sampling: 对 ``content.delta`` / ``reasoning.delta`` 的采样策略，
            其它事件全采。``"none"`` 不写 delta（默认，防爆磁盘）；
            ``"periodic"`` 按 ``periodic_batch_size`` 抽样（每 N 个取 1 个）；
            ``"full"`` 全写（仅 debug）。``EventSink`` Protocol 形状不变。
        periodic_batch_size: ``delta_sampling="periodic"`` 时的采样批大小，
            必须 ``> 0``。
    """

    def __init__(
        self,
        output_path: str | Path,
        *,
        auto_flush: bool = True,
        delta_sampling: str = "none",
        periodic_batch_size: int = 20,
    ) -> None:
        if delta_sampling not in ("none", "periodic", "full"):
            raise ValueError(
                f"delta_sampling must be one of none/periodic/full, got {delta_sampling!r}"
            )
        if periodic_batch_size <= 0:
            raise ValueError(f"periodic_batch_size must be > 0, got {periodic_batch_size}")
        self._output_path = resolve_kongming_path(output_path)
        self._auto_flush = auto_flush
        self._delta_sampling = delta_sampling
        self._periodic_batch_size = periodic_batch_size
        # 串行化文件写入，避免多协程交错写出半行 JSON。
        self._lock = asyncio.Lock()
        # lazy init：父目录与空文件在首次 emit 时创建，
        # 构造函数只做参数登记，不做任何 I/O。
        self._init_done = False
        # delta 采样计数：按 kind 分别计数；periodic 模式下每 N 取 1 写盘
        self._delta_seen: dict[str, int] = {}

    @property
    def output_path(self) -> Path:
        """当前 sink 的 JSONL 路径（只读）。"""
        return self._output_path

    # ------------------------------------------------------------------
    # EventSink Protocol 实现
    # ------------------------------------------------------------------

    async def emit(self, event: Event) -> None:
        """把一条事件写成一行 JSON 追加到 ``output_path``。

        对 ``content.delta`` / ``reasoning.delta`` 应用 ``delta_sampling`` 策略；
        其它事件 kind 全量写盘。

        失败策略：本方法内部不吞异常。runner 在 fan-out 层
        （``core/runner.py`` ``_emit``）已经做了"sink 异常不污染主链路"的兜底，
        所以这里允许抛出 ``OSError`` / ``TypeError`` 等，让 runner 统一处理。
        """
        if event.kind in _STREAM_DELTA_KINDS and not self._should_write_delta(event.kind):
            return
        await self._ensure_init()
        line = self._serialize(event)
        async with self._lock:
            await asyncio.to_thread(self._append_line_sync, line)

    def _should_write_delta(self, kind: str) -> bool:
        """按 ``delta_sampling`` 策略决定本条 delta 是否写盘。

        - ``none`` → 永远 False（默认；防爆磁盘）
        - ``full`` → 永远 True（仅 debug）
        - ``periodic`` → 从第 1 个起每 ``periodic_batch_size`` 个 delta 写一个
          （计数器按 kind 分别维护，``content.delta`` / ``reasoning.delta``
          互不影响）。``batch_size=1`` 时退化为"全写"（等价 ``full`` 模式）。
        """
        if self._delta_sampling == "none":
            return False
        if self._delta_sampling == "full":
            return True
        # periodic：用 (n-1) % batch_size == 0 表达"从第 1 个起每 N 取 1"。
        # 这样 batch_size=1 时所有 (n-1)%1=0 → 全写，符合"每 1 取 1=全取"直觉。
        # 对 batch_size>1 行为不变：n=1,1+N,1+2N,... 命中。
        n = self._delta_seen.get(kind, 0) + 1
        self._delta_seen[kind] = n
        return (n - 1) % self._periodic_batch_size == 0

    async def close(self) -> None:
        """Placeholder：当前实现每次 ``emit`` 独立开关文件，无需 close。

        保留此方法是为了未来切换到"长期持有 FD + 批量 flush"模式时，
        调用方（host / native_runtime / tests）可以统一调用，不必改接线。
        """
        return None

    # ------------------------------------------------------------------
    # 内部工具
    # ------------------------------------------------------------------

    async def _ensure_init(self) -> None:
        """首次 emit 时在磁盘上创建父目录和空文件（double-check + lock）。"""
        if self._init_done:
            return
        async with self._lock:
            if self._init_done:
                return
            await asyncio.to_thread(self._init_sync)
            self._init_done = True

    def _init_sync(self) -> None:
        """真正的文件系统初始化，在线程池里跑以免阻塞事件循环。"""
        self._output_path.parent.mkdir(parents=True, exist_ok=True)
        self._output_path.touch(exist_ok=True)

    def _append_line_sync(self, line: str) -> None:
        """在线程池里以 append 模式写一行 JSON。"""
        with self._output_path.open("a", encoding="utf-8") as f:
            f.write(line + "\n")
            if self._auto_flush:
                f.flush()

    def _serialize(self, event: Event) -> str:
        """把 :class:`~core.contracts.Event` 转成单行 JSON 字符串。

        - 优先 :func:`dataclasses.asdict` 把 Event 及其嵌套 dataclass 展开；
          如果传入的 event 不是 dataclass（比如测试用自定义对象），
          退化到读取公共字段。
        - 本地 trace 写盘前会裁剪 ``llm.request`` / ``llm.response`` 中的正文
          和 schema；完整 provider 调试数据由 PromptDebugDumpSink / raw dump 承担。
        - :func:`json.dumps` 走 ``ensure_ascii=False`` 保留中文；
          非默认可序列化的值通过 :func:`_json_default` 降级。
        """
        payload = _sanitize_local_trace_event(_event_to_dict(event))
        return json.dumps(payload, ensure_ascii=False, default=_json_default)


# ---------------------------------------------------------------------------
# Factory
# ---------------------------------------------------------------------------


def build_jsonl_trace_sink(config: Config) -> JsonlTraceSink:
    """按统一配置构造 :class:`JsonlTraceSink`。

    读取 :attr:`config.trace.output_path` + :attr:`config.trace.auto_flush`，
    并把 :attr:`config.stream.delta_sampling` / :attr:`config.stream.periodic_batch_size`
    传给 sink，让流式 delta 事件按策略采样落盘（防爆磁盘）。
    装配层（``native_runtime.build``）负责把返回的 sink 注册进 runner 的
    ``list[EventSink]``；本函数本身不做注册。
    """
    return JsonlTraceSink(
        resolve_kongming_path(config.trace.output_path),
        auto_flush=config.trace.auto_flush,
        delta_sampling=config.stream.delta_sampling,
        periodic_batch_size=config.stream.periodic_batch_size,
    )


# ---------------------------------------------------------------------------
# 序列化辅助
# ---------------------------------------------------------------------------


def _event_to_dict(event: Any) -> dict[str, Any]:
    """把 Event 对象转成 JSON-safe dict。

    三档兜底：

    1. dataclass → :func:`dataclasses.asdict`
    2. 有 ``__dict__`` 的普通对象 → 取实例字典
    3. 其它情况 → 按 Event Protocol 已知字段（kind / run_id / turn /
       payload / timestamp_ms）逐一 ``getattr``

    这里刻意**不**假设一定是 ``core.contracts.Event``，以便单元测试传轻量
    替身对象时也能落盘。
    """
    if dataclasses.is_dataclass(event) and not isinstance(event, type):
        return dataclasses.asdict(event)
    if hasattr(event, "__dict__"):
        # 过滤掉私有字段（下划线开头），避免把实现细节写进 trace。
        return {k: v for k, v in vars(event).items() if not k.startswith("_")}
    return {
        "kind": getattr(event, "kind", None),
        "run_id": getattr(event, "run_id", None),
        "turn": getattr(event, "turn", None),
        "payload": getattr(event, "payload", {}),
        "timestamp_ms": getattr(event, "timestamp_ms", None),
    }


def _sanitize_local_trace_event(event: dict[str, Any]) -> dict[str, Any]:
    """裁剪本地 trace 事件，避免默认 JSONL 落完整 prompt、正文和 tool schema。"""
    kind = event.get("kind")
    payload = event.get("payload")
    if kind == "llm.request" and isinstance(payload, dict):
        request = payload.get("request")
        if isinstance(request, dict):
            event = dict(event)
            event["payload"] = {
                **payload,
                "request": _summarize_llm_request_payload(request),
            }
    elif kind == "llm.response" and isinstance(payload, dict):
        response = payload.get("response")
        if isinstance(response, dict):
            event = dict(event)
            event["payload"] = {
                **payload,
                "response": _summarize_llm_response_payload(response),
            }
    return event


def _summarize_llm_request_payload(request: dict[str, Any]) -> dict[str, Any]:
    """把 provider request 压成本地 trace 摘要，保留索引字段。"""
    messages = request.get("messages")
    tools = request.get("tools")

    if isinstance(messages, list):
        message_roles = [
            item.get("role", "")
            for item in messages
            if isinstance(item, dict) and isinstance(item.get("role"), str)
        ]
        message_count: int | None = len(messages)
    else:
        raw_roles = request.get("message_roles")
        message_roles = list(raw_roles) if isinstance(raw_roles, list) else []
        raw_count = request.get("message_count")
        message_count = raw_count if isinstance(raw_count, int) else None

    if isinstance(tools, list):
        tool_names = [
            item.get("name", "")
            for item in tools
            if isinstance(item, dict) and isinstance(item.get("name"), str)
        ]
        tool_count: int | None = len(tools)
    else:
        raw_names = request.get("tool_names")
        tool_names = list(raw_names) if isinstance(raw_names, list) else []
        raw_count = request.get("tool_count")
        tool_count = raw_count if isinstance(raw_count, int) else None

    return {
        "model": request.get("model"),
        "message_count": message_count,
        "message_roles": message_roles,
        "tool_count": tool_count,
        "tool_names": tool_names,
        "metadata": dict(request.get("metadata", {}))
        if isinstance(request.get("metadata"), dict)
        else {},
        "reasoning_effort": request.get("reasoning_effort"),
        "temperature": request.get("temperature"),
        "max_tokens": request.get("max_tokens"),
        "timeout_seconds": request.get("timeout_seconds"),
    }


def _summarize_llm_response_payload(response: dict[str, Any]) -> dict[str, Any]:
    """把 provider response 压成本地 trace 摘要，保留 usage 和 provider metadata。"""
    message = response.get("message")
    message_summary: dict[str, Any] = {}
    if isinstance(message, dict):
        content = message.get("content")
        tool_calls = message.get("tool_calls")
        if isinstance(tool_calls, list):
            tool_names = [
                item.get("tool_name", "")
                for item in tool_calls
                if isinstance(item, dict) and isinstance(item.get("tool_name"), str)
            ]
            tool_call_count = len(tool_calls)
        else:
            raw_names = message.get("tool_names")
            tool_names = list(raw_names) if isinstance(raw_names, list) else []
            raw_count = message.get("tool_call_count")
            tool_call_count = raw_count if isinstance(raw_count, int) else 0
        message_summary = {
            "role": message.get("role"),
            "content_chars": len(content)
            if isinstance(content, str)
            else int(message.get("content_chars", 0) or 0),
            "tool_call_count": tool_call_count,
            "tool_names": tool_names,
        }

    return {
        "finish_reason": response.get("finish_reason"),
        "message": message_summary,
        "usage": dict(response.get("usage", {})) if isinstance(response.get("usage"), dict) else {},
        "provider_metadata": dict(response.get("provider_metadata", {}))
        if isinstance(response.get("provider_metadata"), dict)
        else {},
    }


def _json_default(o: Any) -> Any:
    """:func:`json.dumps` 的兜底序列化器。

    负责把 dataclass / :class:`~pathlib.Path` / :class:`~datetime.datetime` /
    :class:`~datetime.date` / ``set`` / ``bytes`` 等常见非 JSON 原生类型降级成
    可序列化值；无法识别的类型降级成 ``repr(o)``，保证 ``emit`` 永远不会因为
    payload 里冒出一个奇怪对象而抛 :class:`TypeError`。
    """
    if dataclasses.is_dataclass(o) and not isinstance(o, type):
        return dataclasses.asdict(o)
    if isinstance(o, Path):
        return o.as_posix()
    if isinstance(o, (datetime, date)):
        return o.isoformat()
    if isinstance(o, set):
        return sorted(o, key=repr)
    if isinstance(o, (bytes, bytearray)):
        try:
            return o.decode("utf-8")
        except UnicodeDecodeError:
            return o.hex()
    # 最后的降级：用 repr，不抛异常。
    return repr(o)


__all__ = [
    "JsonlTraceSink",
    "build_jsonl_trace_sink",
]
