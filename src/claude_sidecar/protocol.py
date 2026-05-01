"""sidecar wire 协议数据模型。

定义 5 类 SidecarRequest（XSpace → sidecar）的 Pydantic 模型，以及
顶层 :func:`parse_request` 函数把单行 JSONL 输入解析成强类型对象。

事件协议（sidecar → XSpace）由 :mod:`claude_sidecar.output` 负责构造，
这里不定义事件模型（事件是字典级 emit，由 OutputWriter 自动注入
``protocolVersion`` 和 ``ts``）。

字段名约定：

- wire 协议字段是 camelCase（如 ``workspaceId`` / ``threadId``）
- Python 字段是 snake_case（如 ``workspace_id``）
- Pydantic ``populate_by_name=True`` + ``Field(alias=...)`` 实现双向映射
- ``protocolVersion`` 必须为字符串 ``"1"``，否则直接拒绝
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any, Final, Literal, cast

from pydantic import BaseModel, ConfigDict, Field, ValidationError

PROTOCOL_VERSION: Final[Literal["1"]] = "1"


class _BaseRequest(BaseModel):
    """所有 SidecarRequest 共享的基类。

    ``protocolVersion`` 字段在 :func:`parse_request` 顶层先校验（不过 Pydantic
    再校验一次也无害），保证字段层确定性。
    """

    model_config = ConfigDict(populate_by_name=True, extra="ignore")
    protocol_version: Literal["1"] = Field(alias="protocolVersion")


class StartRequest(_BaseRequest):
    """启动一次新 run 的请求。"""

    type: Literal["start"]
    workspace_id: str = Field(alias="workspaceId")
    thread_id: str = Field(alias="threadId")
    run_id: str = Field(alias="runId")
    cwd: str
    prompt: str
    resume: str | None = None
    model: str | None = None
    permission_mode: str | None = Field(default=None, alias="permissionMode")
    allowed_tools: list[str] | None = Field(default=None, alias="allowedTools")
    disallowed_tools: list[str] | None = Field(default=None, alias="disallowedTools")
    context_pack_path: str | None = Field(default=None, alias="contextPackPath")
    continue_point_snapshot: str | None = Field(default=None, alias="continuePointSnapshot")
    metadata: dict[str, Any] | None = None


class ResolveApprovalRequest(_BaseRequest):
    """回应一次 approval 请求（allow / deny）。"""

    type: Literal["resolve_approval"]
    thread_id: str = Field(alias="threadId")
    request_id: str = Field(alias="requestId")
    # strict=True 拒绝 "yes" / "true" 等字符串到 bool 的隐式转换；
    # XSpace driver 必须发原生 JSON bool，避免下游误判。
    approved: bool = Field(strict=True)
    message: str | None = None
    remember_entry: str | None = Field(default=None, alias="rememberEntry")
    updated_input: Any | None = Field(default=None, alias="updatedInput")


class InterruptRequest(_BaseRequest):
    """用户主动中断当前 run。"""

    type: Literal["interrupt"]
    thread_id: str = Field(alias="threadId")


class ReconnectRequest(_BaseRequest):
    """sidecar 崩溃重拉后用 ``runtimeSessionId`` 续接 SDK 会话。"""

    type: Literal["reconnect"]
    workspace_id: str = Field(alias="workspaceId")
    thread_id: str = Field(alias="threadId")
    run_id: str = Field(alias="runId")
    runtime_session_id: str = Field(alias="runtimeSessionId")


class ShutdownRequest(_BaseRequest):
    """显式关闭 sidecar 进程。"""

    type: Literal["shutdown"]
    thread_id: str = Field(alias="threadId")
    reason: str | None = None


SidecarRequest = (
    StartRequest | ResolveApprovalRequest | InterruptRequest | ReconnectRequest | ShutdownRequest
)


_REQUEST_TYPES: Final[dict[str, type[_BaseRequest]]] = {
    "start": StartRequest,
    "resolve_approval": ResolveApprovalRequest,
    "interrupt": InterruptRequest,
    "reconnect": ReconnectRequest,
    "shutdown": ShutdownRequest,
}


@dataclass(frozen=True, slots=True)
class ParsedRequest:
    """解析成功的结果。"""

    request: SidecarRequest


@dataclass(frozen=True, slots=True)
class ParseError:
    """解析失败的结果。

    ``reason`` 用枚举值方便上游路由（unknown_type / protocol_version / json_decode /
    schema），``detail`` 是给人读的诊断信息（写到 transport_error.message 里）。
    """

    reason: Literal["json_decode", "protocol_version", "schema", "unknown_type"]
    detail: str


def parse_request(raw: str) -> ParsedRequest | ParseError:
    """把一行 JSONL 输入解析成 SidecarRequest 之一，或返回 ParseError。

    顶层校验顺序：
    1. JSON 是否合法
    2. 顶层是否是 object（不是 array / 标量）
    3. ``protocolVersion`` 是否为字符串 ``"1"``
    4. ``type`` 是否在已知 5 类里
    5. Pydantic 校验各类型的字段
    """
    try:
        payload = json.loads(raw)
    except (json.JSONDecodeError, ValueError) as e:
        return ParseError(reason="json_decode", detail=str(e))

    if not isinstance(payload, dict):
        return ParseError(
            reason="schema",
            detail="expected JSON object at top level",
        )

    pv = payload.get("protocolVersion")
    if pv != PROTOCOL_VERSION:
        return ParseError(
            reason="protocol_version",
            detail=f"expected protocolVersion='1', got {pv!r}",
        )

    type_ = payload.get("type")
    if not isinstance(type_, str) or type_ not in _REQUEST_TYPES:
        return ParseError(
            reason="unknown_type",
            detail=f"unknown request type: {type_!r}",
        )

    cls = _REQUEST_TYPES[type_]
    try:
        request = cls.model_validate(payload)
    except ValidationError as e:
        return ParseError(reason="schema", detail=str(e))

    # cast 提示 mypy：_REQUEST_TYPES 的 value 类型是 type[_BaseRequest]，
    # 但 type_ 已经被前面 in 检查约束到 5 类之一，所以 request 实际上一定是
    # SidecarRequest 之一。pre-commit mypy 严格模式下需要这个 cast。
    return ParsedRequest(request=cast(SidecarRequest, request))
