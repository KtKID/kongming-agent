"""REST API 请求 / 响应 DTO（v0.1.5 web 宿主壳）。

本文件定义 8 个 Pydantic v2 DTO，覆盖 web 宿主的 REST 端面：

- :class:`HistoryMessageDTO`：单条历史消息。本文件**首先**定义，因为
  :class:`web.protocol.ws_frames.ThreadHistoryFrame` 的 ``messages`` 字段
  会复用它（非典型的 REST 用法，但避免协议层引入第三个文件）。
- :class:`ThreadMetadataDTO`：thread 元数据，落 ``.kongming/web/threads/{id}/metadata.json``
  的形态，也是 ``GET /api/threads/{id}`` 的响应体。
- :class:`CreateThreadRequest` / :class:`RenameThreadRequest`：thread CRUD 请求体。
- :class:`LLMPresetDTO`：``GET /api/presets`` 返回元素，**不**含 api_key（脱敏）。
- :class:`LoginRequest`：``POST /api/auth/login`` 请求体。
- :class:`CellSummaryDTO`：``GET /api/manage/cells`` 返回元素，管理页用。
- :class:`ErrorResponseDTO`：REST 端通用错误响应（与 WS ``error`` 帧不同——
  WS 帧带 ``timestamp_ms``，REST 错误是请求-响应一对一，无需时序戳）。
- 白板 DTO：workspace 级 ``GET /api/whiteboard`` 快照与 card/layout 更新请求体。

所有 DTO 继承 :class:`web.protocol._base._FrameBase`（``frozen=True``、
``extra='forbid'``），从而：

- 一经构造不可变，避免下游误改后影响审计 / 持久化语义。
- 未知字段直接拒绝，让前后端协议漂移在 round-trip 测试里立刻爆出。

本文件**不**定义 WS 帧——那是 :mod:`web.protocol.ws_frames` 的职责。
"""

from __future__ import annotations

from typing import Annotated, Any, Literal

from pydantic import Field

from web.protocol._base import (
    ErrorCode,
    HistoryMessageRole,
    _FrameBase,
)


class HistoryMessageDTO(_FrameBase):
    """单条历史消息 DTO（user / assistant / tool 三类）。

    既用于 ``GET /api/threads/{id}/history`` REST 响应，也作为
    :class:`web.protocol.ws_frames.ThreadHistoryFrame` 的 ``messages`` 元素。

    ``tool_call_id`` / ``tool_name`` / ``ok`` / ``data`` / ``error_message``
    仅 ``role == "tool"`` 时有意义，其它角色应为 ``None``。

    v0.1.6 加 ``tool_name`` / ``ok`` / ``data`` / ``error_message`` 字段：
    之前 DTO 只透传 role/content/turn/tool_call_id，前端从历史重建 tool 卡片时
    没法显示 toolName / 结果状态 / 结构化数据，刷新页面后用户看不到任何
    工具产出。Message 内部 ``name`` 字段对应 tool_name，``metadata`` dict
    含 ok / data / error_message（见 ``runner._build_tool_result_message``），
    本次把这些信息暴露到 DTO。``arguments`` 仍在 assistant 消息的 tool_calls
    里，需要跨消息查找，留待后续。
    """

    role: HistoryMessageRole
    content: str
    turn: int
    timestamp_ms: int
    tool_call_id: str | None = None
    tool_name: str | None = None
    ok: bool | None = None
    data: dict[str, Any] | None = None
    error_message: str | None = None


class CellSummaryDTO(_FrameBase):
    """管理页单个 cell 的快照（``GET /api/manage/cells`` 返回元素）。"""

    thread_id: Annotated[str, Field(pattern=r"^thread-[a-f0-9]{12}$")]
    thread_name: str
    preset_id: str
    created_at: float
    last_active_at: float
    current_turn: int | None = None
    pending_approval_count: Annotated[int, Field(ge=0)]
    status: Literal["idle", "running", "awaiting_approval"]


class CreateThreadRequest(_FrameBase):
    """创建 thread 请求体（``POST /api/threads``）。"""

    name: Annotated[str, Field(max_length=200)]
    preset_id: str


class ErrorResponseDTO(_FrameBase):
    """REST 通用错误响应。

    与 WS ``error`` 帧的差异：REST 是请求-响应一对一，无需 ``timestamp_ms``；
    ``error_code`` 复用同一枚举集合便于前端统一文案表。
    """

    error_code: ErrorCode
    message: str
    details: dict[str, Any] | None = None


class LLMPresetDTO(_FrameBase):
    """LLM preset 摘要（``GET /api/presets`` 返回元素）。

    ``api_key`` 字段**故意省略**——preset 持久化文件里有 api_key，但 REST 出口
    必须脱敏，前端只通过 ``requires_api_key`` 知道该 preset 是否需要鉴权。
    ``base_url_summary`` 是 host 部分的精简展示（如 ``api.openai.com``），
    避免完整 URL（含路径 / query）暴露内部部署细节。
    """

    id: str
    display_name: str
    model: str
    base_url_summary: str
    requires_api_key: bool


class LoginRequest(_FrameBase):
    """登录请求体（``POST /api/auth/login``）。"""

    password: Annotated[str, Field(min_length=1)]


class ResetPasswordRequest(_FrameBase):
    """重置密码请求体（``POST /api/auth/reset-password``）。"""

    new_password: Annotated[str, Field(min_length=1)]


class RenameThreadRequest(_FrameBase):
    """重命名 thread 请求体（``PATCH /api/threads/{id}``）。"""

    name: Annotated[str, Field(max_length=200)]


class ThreadMetadataDTO(_FrameBase):
    """Thread 元数据，落盘形态 + ``GET /api/threads/{id}`` 响应体。

    ``schema_version`` 默认 ``1``，未来字段演进时 bump 该值，旧文件读入后由
    迁移层升级；``id`` 严格匹配 ``^thread-[a-f0-9]{12}$``，防止用户在 URL
    里手写 thread id 时绕过命名约束。
    """

    id: Annotated[str, Field(pattern=r"^thread-[a-f0-9]{12}$")]
    name: Annotated[str, Field(max_length=200)]
    preset_id: str
    created_at: float
    updated_at: float
    message_count: Annotated[int, Field(ge=0)]
    schema_version: Literal[1] = 1


class WhiteboardCardDTO(_FrameBase):
    """白板卡片完整 DTO。"""

    id: Annotated[str, Field(pattern=r"^card-[a-f0-9]{12}$")]
    title: Annotated[str, Field(min_length=1, max_length=200)]
    category: Annotated[str, Field(max_length=100)] = ""
    x: Annotated[int, Field(ge=0)]
    y: Annotated[int, Field(ge=0)]
    height: Annotated[int, Field(ge=120, le=4000)]
    collapsed: bool
    z_index: Annotated[int, Field(ge=0)]
    filename: Annotated[
        str,
        Field(pattern=r"^[a-z0-9][a-z0-9-]{0,120}-[a-f0-9]{12}\.md$"),
    ]
    content: str
    updated_at: float


class WhiteboardDTO(_FrameBase):
    """Workspace 级白板聚合快照。"""

    title: Annotated[str, Field(min_length=1, max_length=200)]
    cards: list[WhiteboardCardDTO] = Field(default_factory=list)
    schema_version: Literal[1] = 1


class CreateWhiteboardCardRequest(_FrameBase):
    """创建白板卡片请求体。"""

    title: Annotated[str, Field(min_length=1, max_length=200)] = "Untitled"
    category: Annotated[str, Field(max_length=100)] = ""
    content: str = ""
    x: Annotated[int, Field(ge=0)] = 24
    y: Annotated[int, Field(ge=0)] = 24
    height: Annotated[int, Field(ge=120, le=4000)] = 280
    collapsed: bool = False


class UpdateWhiteboardCardRequest(_FrameBase):
    """更新白板卡片正文与基础属性。"""

    content: str
    expected_updated_at: float | None = None
    title: Annotated[str, Field(min_length=1, max_length=200)] | None = None
    category: Annotated[str, Field(max_length=100)] | None = None


class WhiteboardCardLayoutDTO(_FrameBase):
    """单张卡片的布局更新 DTO。"""

    id: Annotated[str, Field(pattern=r"^card-[a-f0-9]{12}$")]
    x: Annotated[int, Field(ge=0)]
    y: Annotated[int, Field(ge=0)]
    height: Annotated[int, Field(ge=120, le=4000)]
    collapsed: bool
    z_index: Annotated[int, Field(ge=0)]


class UpdateWhiteboardLayoutRequest(_FrameBase):
    """更新白板布局请求体。"""

    title: Annotated[str, Field(min_length=1, max_length=200)] | None = None
    cards: list[WhiteboardCardLayoutDTO] = Field(default_factory=list)


__all__: list[str] = [
    "CellSummaryDTO",
    "CreateThreadRequest",
    "CreateWhiteboardCardRequest",
    "ErrorResponseDTO",
    "HistoryMessageDTO",
    "LLMPresetDTO",
    "LoginRequest",
    "RenameThreadRequest",
    "ThreadMetadataDTO",
    "UpdateWhiteboardCardRequest",
    "UpdateWhiteboardLayoutRequest",
    "WhiteboardCardDTO",
    "WhiteboardCardLayoutDTO",
    "WhiteboardDTO",
]
