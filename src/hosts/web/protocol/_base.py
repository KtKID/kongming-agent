"""WS 帧公共基类 + 公共枚举。

本文件不导出对外符号——所有对外消费应走 :mod:`web.protocol.__init__` 的 re-export。
内部命名前缀 ``_`` 表示"协议层内部基类，业务代码不应直接 import"。

设计要点：

- 所有帧用 :class:`pydantic.BaseModel` + ``model_config = ConfigDict(frozen=True,
  extra='forbid')``。``frozen`` 让帧不可变（避免传递过程中被消费方误改）；
  ``extra='forbid'`` 让协议升级期间未知字段直接拒绝，发现漂移更早。
- 帧的 ``frame_type`` 字段用 :class:`typing.Literal`，让 Pydantic v2 在 union
  上能用 ``Field(discriminator='frame_type')`` 自动分派；TS 侧 discriminated
  union 与之对应。protocol-frame-type-unify-v0.2 统一从 ``kind`` 切到
  ``frame_type``，避免与业务字段（``UserInputAttachment.kind`` 等）字面值
  冲突。
- S2C 帧默认带 ``timestamp_ms``（必填），方便前端按时序重排；某些 S2C 帧
  （如 ``thread.history``）也含 ``messages`` 列表，列表内每条自带 ``timestamp_ms``。
- C2S 帧通常不需要 ``timestamp_ms`` / ``turn`` / ``seq``——浏览器发出时刻和
  网络抖动后端不关心，关心的是接收顺序。
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict

# ---------------------------------------------------------------------------
# 公共枚举
# ---------------------------------------------------------------------------

#: ``error`` 帧的错误分类（精确 5 枚举，TS 侧同名 union）。
#:
#: - ``network``：连接 LLM endpoint / 工具远端失败（DNS / TCP / TLS 层）
#: - ``llm_error``：LLM 端返回 4xx / 5xx 或解析模型响应失败
#: - ``tool_error``：工具执行抛异常（不含 ok=False 的业务失败，那个走 tool.call.end）
#: - ``approval_timeout``：ApprovalManager 等待人工审批超时
#: - ``internal``：runtime 内部 panic / 不在以上四类的兜底
#:
#: 新增 code 视为破坏性变更，必须 bump 协议小版本（v0.1.6+）。
ErrorCode = Literal[
    "network",
    "llm_error",
    "tool_error",
    "approval_timeout",
    "internal",
]

#: ``cell.evicted`` 帧的回收原因（精确 4 枚举）。
#:
#: - ``idle``：超过 ``idle_timeout_seconds`` 无活动，被 ThreadManager 自动回收
#: - ``manual_stop``：用户在管理页点了"停止"
#: - ``server_shutdown``：uvicorn 收到 SIGTERM，所有 cell 一并清理
#: - ``error``：runtime 内部异常无法继续
#:
#: 浏览器据此选择不同 toast 文案；伴随的 ``message`` 字段允许携带自由文本细节。
EvictReason = Literal[
    "idle",
    "manual_stop",
    "server_shutdown",
    "error",
]

#: ``approval.decision`` 帧的审批结局（与 :class:`core.contracts.ApprovalOutcome`
#: 三态一致）。
#:
#: - ``approved``：用户允许
#: - ``rejected``：用户拒绝
#: - ``cancelled``：超时 / cell 销毁等系统侧取消
ApprovalOutcome = Literal["approved", "rejected", "cancelled"]


#: 历史角色字面量，保留给旧文档与辅助类型引用。
HistoryMessageRole = Literal["user", "assistant", "tool"]


# ---------------------------------------------------------------------------
# 帧基类
# ---------------------------------------------------------------------------


class _FrameBase(BaseModel):
    """所有 WS 帧的根基类。

    ``frozen=True`` + ``extra='forbid'`` 是协议层硬约束：
    - frozen：帧一经构造不可变，避免下游误改
    - extra='forbid'：未知字段直接拒绝，让漂移在 round-trip 测试里立刻爆出

    注意：本基类**不**含 ``frame_type`` 字段——具体帧类各自声明
    ``frame_type: Literal[...]``，Pydantic v2 才能基于 ``frame_type`` 做
    discriminator 分派。
    """

    model_config = ConfigDict(
        frozen=True,
        extra="forbid",
    )


class _C2SFrameBase(_FrameBase):
    """C2S 帧基类（浏览器 → 后端）。

    C2S 帧通常无 ``turn`` / ``seq`` / ``timestamp_ms``——这些是后端 emit 给前端
    时的服务端语义；浏览器发出帧时由后端按收到顺序处理。
    """


class _S2CFrameBase(_FrameBase):
    """S2C 帧基类（后端 → 浏览器）。

    所有 S2C 帧必带 ``timestamp_ms`` 服务端时间戳，让前端按时序重排（TCP 不
    能完全保序的场景，比如多 sink fan-out 后回到主链路）。

    某些 S2C 帧带 ``turn`` 和 ``seq``（流式增量类）；某些不带（控制类如
    ``pong`` / ``cell.evicted``）。具体帧自行声明。

    ``agent_id``（agent-tree-v0.1 模块 G）：产生该帧的 agent 归属，默认 ``""``
    兼容现有构造点（非 Event 源帧如 ``pong`` / ``cell.evicted`` 不携带）。
    WSEventSink 把 runtime :class:`Event` 翻成帧时透传 ``event.agent_id``，
    让前端能按 agent 归属展示流式帧（多 agent 场景）。TS 侧对应为可选
    ``agent_id?: string``。
    """

    timestamp_ms: int
    agent_id: str = ""


__all__: list[str] = [
    "ApprovalOutcome",
    "ErrorCode",
    "EvictReason",
    "HistoryMessageRole",
    "_C2SFrameBase",
    "_FrameBase",
    "_S2CFrameBase",
]
