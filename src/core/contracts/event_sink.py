"""Event sink protocol and event value object."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal, Protocol, runtime_checkable

from core.clock import now_epoch_ms

# EventSink / Event
# ---------------------------------------------------------------------------


EventKind = Literal[
    "run.start",
    "run.end",
    # interrupt-run-v0.1：runner 顶层捕获到 ``asyncio.CancelledError``（典型 =
    # 用户在 web 端发 ``InterruptFrame`` → ``cell.current_run_task.cancel()``）后
    # emit；紧接着会 emit ``run.end``（status="cancelled"）。WSEventSink 会把
    # 这条转成 S2C ``RunInterruptedFrame`` 推给所有 attach 的 ws，多 tab 自动
    # 同步。payload={"cancelled_at_turn": int, "cancelled_tool_call_id": str | None,
    # "cancel_reason": str}。``cancelled_tool_call_id`` 非空时表示打断时正卡在
    # 某个 tool；为 None 表示打断在 LLM 阶段 / approval 阶段（pending tool 已被
    # ``_finalize_unpaired_call`` 写占位 tool_result，session jsonl 配对完整）。
    "run.cancelled",
    "turn.start",
    "turn.end",
    "llm.request",
    "llm.response",
    "tool.call.start",
    "tool.call.end",
    "approval.request",
    "approval.decision",
    "error",
    # v0.2 流式（llm-streaming-v1）新增：
    # - content.delta / reasoning.delta：runner 消费 LLMStreamChunk 后按每片增量
    #   emit，payload={"delta": str, "index": int?}
    # - llm.chunk.first：首个非空 chunk 抵达时 emit 一次，
    #   payload={"elapsed_ms": int, "model": str | None}；用于 TTFT 度量
    # - llm.stream.end：流结束汇总 emit 一次，
    #   payload={"chunk_count": int, "finish_reason": str, "content_chars": int,
    #            "reasoning_chars": int, "tool_call_count": int, "truncated_args": bool}
    "content.delta",
    "reasoning.delta",
    "llm.chunk.first",
    "llm.stream.end",
    # Memory 模块事件（self-evolution-memory v0.1.3+）：
    # - memory.write.success：safety_write.execute_write 成功落盘 (status="written")
    # - memory.write.rejected：内容扫描拦截（prompt injection / secret / 不可见 Unicode 等）
    # - memory.write.error：写入失败（error / not_found / skipped）
    # - memory.snapshot.refreshed：history.compact 后 MemoryRefreshSink 重新
    #   load_from_disk 得到新 snapshot 时 emit；和启动时一次性的 snapshot.captured
    #   并列（前者可多次，后者只在装配阶段出现）
    "memory.write.success",
    "memory.write.rejected",
    "memory.write.error",
    "memory.snapshot.refreshed",
    # safety v0.1.4 决策事件（safety-scope-v0.1.4 模块 8 DecisionTrace）：
    # - tool.denied：HardBlock 命中 / ConsentResolver 用户拒绝。payload 含
    #   decision_class / decision_source / matched_rule / reason / boundary_kind
    #   / tool_name / path_or_command / request_id / outcome
    # - tool.approval_required：ConsentResolver 进入 standard / elevated ask 时
    #   触发，发起 InteractiveApproval 之前 emit 一次（先于 decision）
    # - tool.silently_allowed：TrustResolver 命中 intrinsic / session / config
    #   或 ConsentResolver 用户允许时触发。read 类工具的 silently_allowed
    #   默认不写盘（受 config.safety.log_silent_reads 控制，避免 jsonl 膨胀）
    "tool.denied",
    "tool.approval_required",
    "tool.silently_allowed",
    # Skill loader 装配期事件（skill-loader-v0.1.6）：
    # - skill.discovered：每成功解析一个 SkillSpec
    # - skill.shadowed：workspace 覆盖 home 时
    # - skill.parse_failed：frontmatter YAML 解析失败 / 必填字段缺失
    # - skill.skipped：符号链接逃逸 skill 目录
    "skill.discovered",
    "skill.shadowed",
    "skill.parse_failed",
    "skill.skipped",
    # Skill 运行时事件（skill-tool-v0.1.6）：
    # - skill.invoked：SkillTool.execute 入口（unknown skill 不 emit）。
    #   payload={"name", "source", "body_path", "args", "body_chars": 0}
    # - skill.completed：body 读取 + 变量替换成功后 emit。
    #   payload={"name", "source", "body_chars", "elapsed_ms"}
    # - skill.failed：read 失败 / `!command` 拒绝 / 任何派生异常。
    #   payload={"name", "source", "error_kind", "message", "elapsed_ms"}
    "skill.invoked",
    "skill.completed",
    "skill.failed",
    # Token 用量（每轮 LLM 返回后 emit）：
    # payload={"prompt_tokens": int, "completion_tokens": int, "total_tokens": int}
    "usage",
    # 用户选择工具事件（user-choice-tool-v0.1）：
    # - choice.requested：present_choices 工具校验参数后发出。
    #   payload={"request_id": str, "title": str, "description": str,
    #            "questions": list[dict]}，WSEventSink 翻译成 choice.request。
    "choice.requested",
]


@dataclass(frozen=True)
class Event:
    """统一事件结构。

    runner 在关键节点构造 Event，fan-out 到所有注册的 :class:`EventSink`。
    v1-mini 只有一个 sink：``infrastructure.tracing/trace_sink.py`` 的 ``JsonlTraceSink``。
    v0.2+ 追加 usage / audit sink 时仍然走同一个协议，不新增事件协议。

    三坐标字段（agent-tree-v0.1 模块 G）用于多 agent 场景的事件归属：

    - ``agent_id``：产生该 event 的 agent（单 agent 场景默认 ``""``，由
      runner 填充真实值）；agent_loop 分发、cancel_subtree 编排、前端归属
      展示均依赖此字段。
    - ``task_id``：关联 :class:`TaskRecord`（单 agent 为空，留待 task-3/4 填充）。
    - ``conversation_id``：= tree_id / thread_id，标识所属会话树。

    三字段默认值均为 ``""``，保证现有构造点（如 ``Event(kind="run.start",
    run_id=...)``）不报错。
    """

    kind: str
    run_id: str
    turn: int | None = None
    payload: dict[str, Any] = field(default_factory=dict)
    timestamp_ms: int = field(default_factory=now_epoch_ms)
    agent_id: str = ""
    task_id: str = ""
    conversation_id: str = ""


@runtime_checkable
class EventSink(Protocol):
    """事件落地协议。

    实现方保证 ``emit`` 是幂等写入或至少不会抛异常污染主链路。
    fan-out 职责在 runner，不是 sink 自己的事。
    """

    async def emit(self, event: Event) -> None:
        """处理一个事件。"""
        ...


__all__ = [
    "Event",
    "EventKind",
    "EventSink",
]
